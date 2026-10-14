#!/usr/bin/env python3
"""Local Delta India BTCUSD chart server. Public market data only. Port 8700.

python chart_app\\server.py
"""

from __future__ import annotations

import csv
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delta_rest import chart_now, fetch_history_candles  # noqa: E402
from indicators import (  # noqa: E402
    INDICATORS,
    compute_indicator,
    list_indicators,
    load_indicators,
    short_name_for,
)
from indicators.htf import aggregate_1m  # noqa: E402
from live_hub import HUB, client_loop  # noqa: E402
from strategies import PLUGINS, compute, list_strategies, load_plugins  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "BTCUSD"
MAX_BARS = 1500
# STEP 1: 1000/2000/3000 returned in full; 6000-min window returned 4000 from the newest end.
DELTA_PAGE_CAP = 4000
_CANDLE_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}
_OVERLAY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_IND_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_CACHE_CAP = 48
_LAST_CANDLE_META: dict[str, Any] = {"pages": 0, "fetch": 0, "ms": 0.0, "tf": ""}
RES_SEC = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}
logger = logging.getLogger("chart_app")


def _build_id() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass
    try:
        return str(int((APP_DIR / "static" / "app.js").stat().st_mtime))
    except OSError:
        return "dev"


BUILD_ID = _build_id()

app = FastAPI(title="chart_app")
load_plugins()
load_indicators()


@app.on_event("startup")
async def _start_live() -> None:
    await HUB.start()


@app.on_event("shutdown")
async def _stop_live() -> None:
    await HUB.stop()
STATIC = APP_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index() -> HTMLResponse:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("__BUILD__", BUILD_ID)
    return HTMLResponse(html)


def _norm_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    if "time" not in raw:
        return None
    return {
        "time": int(raw["time"]),
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
        "volume": float(raw.get("volume") or 0.0),
    }


def _cache_put(store: dict[tuple[Any, ...], dict[str, Any]], key: tuple[Any, ...], val: dict[str, Any]) -> None:
    store[key] = val
    extra = len(store) - _CACHE_CAP
    if extra <= 0:
        return
    for old in list(store.keys())[:extra]:
        store.pop(old, None)


def _slice_payload_to(data: dict[str, Any], to_u: int) -> dict[str, Any]:
    out = dict(data)

    def _cap_pts(pts: Any) -> Any:
        if not isinstance(pts, list):
            return pts
        return [p for p in pts if not isinstance(p, dict) or p.get("time") is None or int(p["time"]) <= to_u]

    if isinstance(out.get("series"), list):
        series = []
        for sp in out["series"]:
            if not isinstance(sp, dict):
                series.append(sp)
                continue
            item = dict(sp)
            item["points"] = _cap_pts(item.get("points"))
            series.append(item)
        out["series"] = series
    if isinstance(out.get("lines"), list):
        lines = []
        for ln in out["lines"]:
            if not isinstance(ln, dict):
                lines.append(ln)
                continue
            item = dict(ln)
            item["points"] = _cap_pts(item.get("points"))
            lines.append(item)
        out["lines"] = lines
    for key in ("markers", "rsi", "boxes", "labels", "bands"):
        if isinstance(out.get(key), list):
            out[key] = _cap_pts(out[key])
    return out


async def _candles(tf: str, start: int, end: int) -> list[dict[str, Any]]:
    if tf not in RES_SEC:
        raise HTTPException(400, f"bad tf {tf}")
    res = RES_SEC[tf]
    want_start = int(start)
    want_end = int(end)
    if want_end <= want_start:
        logger.info("tf=%s range=%s..%s pages=0 rows=0", tf, want_start, want_end)
        return []
    t0 = time.perf_counter()
    now = chart_now()
    closed_before = now - res
    by_ts: dict[int, dict[str, Any]] = {}
    pages = 0
    fetched = 0
    empty_streak = 0
    span = DELTA_PAGE_CAP * res
    # Align pages to a fixed grid so replay stepping reuses the same closed-page cache.
    aligned_end = ((want_end // span) + 1) * span
    page_end = aligned_end
    while page_end > want_start:
        page_start = page_end - span
        pages += 1
        cache_key = (tf, int(page_start))
        chunk_closed = page_end <= closed_before
        rows_chunk: list[dict[str, Any]] = []
        hit = chunk_closed and cache_key in _CANDLE_CACHE
        if hit:
            rows_chunk = _CANDLE_CACHE[cache_key]
        else:
            fetched += 1
            raw = await fetch_history_candles(
                SYMBOL,
                tf,
                int(page_start),
                int(page_end),
            )
            for item in raw:
                row = _norm_row(item) if isinstance(item, dict) else item
                if row is not None:
                    rows_chunk.append(row)
            if chunk_closed:
                closed_rows = [r for r in rows_chunk if int(r["time"]) < closed_before]
                _CANDLE_CACHE[cache_key] = closed_rows
                rows_chunk = closed_rows
        got = 0
        for row in rows_chunk:
            ts = int(row["time"])
            if ts < want_start or ts > want_end:
                continue
            by_ts[ts] = row
            got += 1
        if got == 0 and page_end < want_start + span:
            empty_streak += 1
        else:
            empty_streak = 0
        if empty_streak >= 3:
            break
        if page_start <= want_start:
            break
        page_end = page_start
    rows = [by_ts[k] for k in sorted(by_ts)]
    ms = (time.perf_counter() - t0) * 1000.0
    _LAST_CANDLE_META.update({"pages": pages, "fetch": fetched, "ms": round(ms, 1), "tf": tf})
    logger.info(
        "tf=%s range=%s..%s pages=%s fetch=%s rows=%s ms=%.1f",
        tf,
        want_start,
        want_end,
        pages,
        fetched,
        len(rows),
        ms,
    )
    return rows


async def _candles_replay(tf: str, start: int, to_u: int) -> list[dict[str, Any]]:
    """Closed native HTF bars + last partial bucket from 1m (no lookahead)."""
    cap = int(to_u)
    want_start = int(start)
    if tf == "1m" or RES_SEC.get(tf, 60) <= 60:
        rows = await _candles("1m", want_start, cap)
        return [b for b in rows if want_start <= int(b["time"]) <= cap]
    res = RES_SEC[tf]
    bucket = (cap // res) * res
    closed: list[dict[str, Any]] = []
    if bucket > want_start:
        native = await _candles(tf, want_start, bucket - 1)
        closed = [b for b in native if want_start <= int(b["time"]) < bucket]
    m1 = await _candles("1m", bucket, cap)
    m1 = [b for b in m1 if bucket <= int(b["time"]) <= cap]
    partial = aggregate_1m(m1, tf, cap)
    extra = [p for p in partial if int(p["time"]) >= bucket]
    logger.info(
        "replay-htf tf=%s closed=%s partial_1m=%s last_partial=%s",
        tf,
        len(closed),
        len(m1),
        extra[-1]["time"] if extra else None,
    )
    return closed + extra


async def _candles_no_lookahead(tf: str, start: int, to_u: int) -> list[dict[str, Any]]:
    """HTF bars with no future 1m inside the last bucket. Uses native closed + 1m tail."""
    return await _candles_replay(tf, start, to_u)


def _is_live_to(to_u: int) -> bool:
    return int(to_u) >= chart_now() - 5


@app.get("/api/candles")
async def api_candles(
    tf: str = Query("1m"),
    end: int | None = None,
    start: int | None = None,
    limit: int = Query(MAX_BARS),
    replay: bool = Query(False),
) -> dict[str, Any]:
    limit = max(50, min(int(limit), 4000))
    res = RES_SEC[tf] if tf in RES_SEC else 60
    now = chart_now()
    use_replay = bool(replay)
    if start is not None:
        start_ts = int(start)
        end_ts = int(end) if end is not None else min(start_ts + limit * res, now)
        if not replay:
            end_ts = min(end_ts, now)
        if replay or not _is_live_to(end_ts):
            use_replay = True
            rows = await _candles_replay(tf, start_ts, end_ts)
        else:
            rows = await _candles(tf, start_ts, end_ts)
        if len(rows) > limit:
            rows = rows[:limit]
    else:
        end_ts = int(end) if end is not None else now
        start_ts = end_ts - limit * res
        if replay or not _is_live_to(end_ts):
            use_replay = True
            rows = await _candles_replay(tf, start_ts, end_ts)
        else:
            rows = await _candles(tf, start_ts, end_ts)
        if len(rows) > limit:
            rows = rows[-limit:]
    first = int(rows[0]["time"]) if rows else None
    last = int(rows[-1]["time"]) if rows else None
    reached_now = bool(rows) and int(rows[-1]["time"]) >= now - 2 * res
    return {
        "symbol": SYMBOL,
        "tf": tf,
        "candles": rows,
        "count": len(rows),
        "first": first,
        "last": last,
        "reached_now": reached_now,
        "replay": use_replay,
        "pages": _LAST_CANDLE_META.get("pages"),
        "server_ms": _LAST_CANDLE_META.get("ms"),
        "server_now": now,
    }


@app.get("/api/overlay")
async def api_overlay(
    strategy: str = Query("S020"),
    tf: str = Query("1m"),
    line_tf: str = Query("1m"),
    variant: str = Query("V0"),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    hours: float = Query(24),
    signal_tf: str = Query("15m"),
    rsi_len: int = Query(14),
    ob: float = Query(70),
    os: float = Query(30),
    exp_obh: float = Query(40),
    exp_obl: float = Query(60),
    show_obh: bool = Query(True),
    show_obl: bool = Query(True),
    show_levels: bool = Query(True),
    show_signals: bool = Query(True),
    params: str = Query("{}"),
) -> dict[str, Any]:
    if strategy not in PLUGINS:
        raise HTTPException(404, f"unknown strategy {strategy}")
    t0 = time.perf_counter()
    extra: dict[str, Any] = {}
    try:
        raw_p = json.loads(params) if params else {}
        if isinstance(raw_p, dict):
            extra = raw_p
    except json.JSONDecodeError:
        extra = {}
    now = chart_now()
    to_u = int(to_ts) if to_ts is not None else now
    hrs = float(extra.get("hours", hours))
    from_u = int(from_ts) if from_ts is not None else to_u - int(hrs * 3600)
    cache_key = (strategy, tf, json.dumps(extra, sort_keys=True), from_u, to_u)
    hit = _OVERLAY_CACHE.get(cache_key)
    if hit is not None:
        ms = (time.perf_counter() - t0) * 1000.0
        logger.info("overlay %s cache-hit ms=%.1f", strategy, ms)
        out_hit = dict(hit)
        out_hit["server_ms"] = round(ms, 2)
        out_hit["cache_hit"] = True
        return out_hit
    sess0 = (int(from_u) // 86400) * 86400
    warmup = 2 * 86400
    fetch_from = min(sess0, int(from_u) - warmup)
    bars = await _candles("1m", fetch_from, to_u)
    bars = [b for b in bars if fetch_from <= int(b["time"]) <= to_u]
    merged = {
        "line_tf": extra.get("line_tf", line_tf),
        "hours": hrs,
        "from": from_u,
        "to": to_u,
        "chart_tf": tf,
        "variant": extra.get("variant", variant),
        "signal_tf": extra.get("signal_tf", signal_tf),
        "rsi_len": extra.get("rsi_len", rsi_len),
        "ob": extra.get("ob", ob),
        "os": extra.get("os", os),
        "exp_obh": extra.get("exp_obh", exp_obh),
        "exp_obl": extra.get("exp_obl", exp_obl),
        "show_obh": extra.get("show_obh", show_obh),
        "show_obl": extra.get("show_obl", show_obl),
        "show_levels": extra.get("show_levels", show_levels),
        "show_signals": extra.get("show_signals", show_signals),
    }
    data = compute(strategy, bars, merged)
    data = _slice_payload_to(data, to_u)
    htf = aggregate_1m(bars, tf, to_u)
    out = {
        "strategy": strategy,
        "bars_used": len(bars),
        "last_bar": htf[-1] if htf else None,
        **data,
    }
    _cache_put(_OVERLAY_CACHE, cache_key, out)
    ms = (time.perf_counter() - t0) * 1000.0
    logger.info("overlay %s bars=%s ms=%.1f", strategy, len(bars), ms)
    timed = dict(out)
    timed["server_ms"] = round(ms, 2)
    timed["cache_hit"] = False
    return timed


@app.get("/api/strategies")
async def api_strategies() -> dict[str, Any]:
    return {"strategies": list_strategies()}


@app.get("/api/indicators")
async def api_indicators() -> dict[str, Any]:
    return {"indicators": list_indicators()}


@app.get("/api/indicator/{ind_id}")
async def api_indicator(
    ind_id: str,
    tf: str = Query("1m"),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    params: str = Query("{}"),
) -> dict[str, Any]:
    if ind_id not in INDICATORS:
        raise HTTPException(404, f"unknown indicator {ind_id}")
    if tf not in RES_SEC:
        raise HTTPException(400, f"bad tf {tf}")
    t0 = time.perf_counter()
    res = RES_SEC[tf]
    now = chart_now()
    to_u = int(to_ts) if to_ts is not None else now
    from_u = int(from_ts) if from_ts is not None else to_u - 24 * 3600
    try:
        raw_p = json.loads(params) if params else {}
        if not isinstance(raw_p, dict):
            raw_p = {}
    except json.JSONDecodeError:
        raw_p = {}
    pkey = json.dumps(raw_p, sort_keys=True)
    cache_key = (ind_id, tf, pkey, from_u, to_u)
    hit = _IND_CACHE.get(cache_key)
    if hit is not None:
        ms = (time.perf_counter() - t0) * 1000.0
        logger.info("indicator %s cache-hit ms=%.1f", ind_id, ms)
        out_hit = dict(hit)
        out_hit["server_ms"] = round(ms, 2)
        out_hit["cache_hit"] = True
        return out_hit
    warmup = 500 * res
    fetch_from = from_u - warmup
    if _is_live_to(to_u) and not (to_ts is not None and int(to_ts) < chart_now() - 5):
        bars = await _candles(tf, fetch_from, to_u)
    else:
        bars = await _candles_replay(tf, fetch_from, to_u)
    data = compute_indicator(ind_id, bars, raw_p)
    data = _slice_payload_to(data, to_u)
    out = {
        "id": ind_id,
        "tf": tf,
        "from": from_u,
        "to": to_u,
        "short_name": short_name_for(ind_id, raw_p),
        "bars_used": len(bars),
        "last_bar": bars[-1] if bars else None,
        **data,
    }
    _cache_put(_IND_CACHE, cache_key, out)
    ms = (time.perf_counter() - t0) * 1000.0
    logger.info("indicator %s bars=%s ms=%.1f", ind_id, len(bars), ms)
    timed = dict(out)
    timed["server_ms"] = round(ms, 2)
    timed["cache_hit"] = False
    return timed


def _runs_root() -> Path:
    return ROOT / "backtest" / "strategies"


@app.get("/api/trades/files")
async def api_trade_files() -> dict[str, Any]:
    root = _runs_root()
    files: list[str] = []
    if root.is_dir():
        found: list[Path] = []
        found.extend(root.glob("*/runs/*_trades.csv"))
        found.extend(root.glob("s020_vwap_touch/runs/s020_dev_*_trades.csv"))
        uniq = sorted({p.resolve() for p in found if p.is_file()})
        uniq.sort(key=lambda p: (0 if "s020" in p.name else 1, str(p)))
        for p in uniq:
            files.append(str(p.relative_to(ROOT)).replace("\\", "/"))
    return {"files": files}


@app.get("/api/trades")
async def api_trades(file: str = Query(...)) -> dict[str, Any]:
    rel = file.replace("\\", "/").lstrip("/")
    path = (ROOT / rel).resolve()
    root = _runs_root().resolve()
    if root not in path.parents or not path.name.endswith("_trades.csv"):
        raise HTTPException(400, "file not allowed")
    if not path.is_file():
        raise HTTPException(404, "missing csv")
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            entry_s = str(raw.get("entry_ts_ist") or raw.get("entry") or "")
            exit_s = str(raw.get("exit_ts_ist") or raw.get("exit") or "")
            side = str(raw.get("side") or "").lower()
            try:
                entry_dt = datetime.strptime(entry_s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                entry_ts = int(entry_dt.timestamp())
            except ValueError:
                continue
            exit_ts = None
            try:
                exit_dt = datetime.strptime(exit_s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                exit_ts = int(exit_dt.timestamp())
            except ValueError:
                pass
            net = raw.get("net")
            try:
                net_f = float(net) if net not in (None, "") else None
            except ValueError:
                net_f = None
            rows.append(
                {
                    "side": side,
                    "entry_ts": entry_ts,
                    "exit_ts": exit_ts,
                    "entry_ist": entry_s,
                    "exit_ist": exit_s,
                    "reason": raw.get("reason"),
                    "net": net_f,
                }
            )
    return {"file": rel, "trades": rows}


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await client_loop(ws)


class TickIn(BaseModel):
    price: float = Field(..., gt=0)
    ts: int | None = None


@app.post("/api/test/tick")
async def api_test_tick(body: TickIn) -> dict[str, Any]:
    out = await HUB.inject_tick(body.price, body.ts)
    return {"ok": True, **out}


@app.get("/api/ws/status")
async def api_ws_status() -> dict[str, Any]:
    st = HUB._status()
    st["clients"] = len(HUB.clients)
    return st


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import os
    import uvicorn

    port = int(os.environ.get("CHART_APP_PORT", "8700"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
