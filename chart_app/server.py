#!/usr/bin/env python3
"""Local Delta India BTCUSD chart server. Public market data only. Port 8700.

python chart_app\\server.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
for _p in (str(APP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.download_candles import _fetch_page  # noqa: E402
from indicators import (  # noqa: E402
    INDICATORS,
    compute_indicator,
    list_indicators,
    load_indicators,
    short_name_for,
)
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

app = FastAPI(title="chart_app")
load_plugins()
load_indicators()
STATIC = APP_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


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
    now = int(time.time())
    closed_before = now - res
    by_ts: dict[int, dict[str, Any]] = {}
    pages = 0
    fetched = 0
    empty_streak = 0
    span = DELTA_PAGE_CAP * res
    # Align pages to a fixed grid so replay stepping reuses the same closed-page cache.
    aligned_end = ((want_end // span) + 1) * span
    page_end = aligned_end
    async with httpx.AsyncClient() as client:
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
                raw = await _fetch_page(
                    client,
                    symbol=SYMBOL,
                    resolution=tf,
                    start=int(page_start),
                    end=int(page_end),
                )
                for item in raw:
                    row = _norm_row(item) if isinstance(item, dict) else None
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
            if not hit:
                await asyncio.sleep(0.15)
    rows = [by_ts[k] for k in sorted(by_ts)]
    logger.info(
        "tf=%s range=%s..%s pages=%s fetch=%s rows=%s ms=%.1f",
        tf,
        want_start,
        want_end,
        pages,
        fetched,
        len(rows),
        (time.perf_counter() - t0) * 1000.0,
    )
    return rows


@app.get("/api/candles")
async def api_candles(
    tf: str = Query("1m"),
    end: int | None = None,
    start: int | None = None,
    limit: int = Query(MAX_BARS),
) -> dict[str, Any]:
    limit = max(50, min(int(limit), 4000))
    res = RES_SEC[tf] if tf in RES_SEC else 60
    now = int(time.time())
    if start is not None:
        start_ts = int(start)
        end_ts = min(start_ts + limit * res, now)
        rows = await _candles(tf, start_ts, end_ts)
        if len(rows) > limit:
            rows = rows[:limit]
    else:
        end_ts = int(end) if end is not None else now
        start_ts = end_ts - limit * res
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
    now = int(time.time())
    to_u = int(to_ts) if to_ts is not None else now
    hrs = float(extra.get("hours", hours))
    from_u = int(from_ts) if from_ts is not None else to_u - int(hrs * 3600)
    cache_key = (strategy, tf, json.dumps(extra, sort_keys=True), from_u, to_u)
    hit = _OVERLAY_CACHE.get(cache_key)
    if hit is not None:
        logger.info("overlay %s cache-hit ms=%.1f", strategy, (time.perf_counter() - t0) * 1000.0)
        return hit
    for (sk, stf, sparams, sfrom, sto), cached in list(_OVERLAY_CACHE.items()):
        if sk == strategy and stf == tf and sparams == json.dumps(extra, sort_keys=True) and sfrom == from_u and sto >= to_u:
            sliced = _slice_payload_to(cached, to_u)
            logger.info("overlay %s cache-slice to=%s ms=%.1f", strategy, to_u, (time.perf_counter() - t0) * 1000.0)
            return sliced
    warmup = 10 * 86400
    bars = await _candles("1m", from_u - warmup, to_u)
    bars = [b for b in bars if from_u - warmup <= int(b["time"]) <= to_u]
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
    out = {"strategy": strategy, "bars_used": len(bars), **data}
    _cache_put(_OVERLAY_CACHE, cache_key, out)
    logger.info(
        "overlay %s bars=%s ms=%.1f",
        strategy,
        len(bars),
        (time.perf_counter() - t0) * 1000.0,
    )
    return out


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
    now = int(time.time())
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
        logger.info("indicator %s cache-hit ms=%.1f", ind_id, (time.perf_counter() - t0) * 1000.0)
        return hit
    for (iid, itf, ip, ifr, ito), cached in list(_IND_CACHE.items()):
        if iid == ind_id and itf == tf and ip == pkey and ifr == from_u and ito >= to_u:
            sliced = _slice_payload_to(cached, to_u)
            logger.info("indicator %s cache-slice to=%s ms=%.1f", ind_id, to_u, (time.perf_counter() - t0) * 1000.0)
            return sliced
    warmup = max(500, 3000) * res
    bars = await _candles(tf, from_u - warmup, to_u)
    bars = [b for b in bars if int(b["time"]) <= to_u]
    data = compute_indicator(ind_id, bars, raw_p)
    out = {
        "id": ind_id,
        "tf": tf,
        "from": from_u,
        "to": to_u,
        "short_name": short_name_for(ind_id, raw_p),
        "bars_used": len(bars),
        **data,
    }
    _cache_put(_IND_CACHE, cache_key, out)
    logger.info(
        "indicator %s bars=%s ms=%.1f",
        ind_id,
        len(bars),
        (time.perf_counter() - t0) * 1000.0,
    )
    return out


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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8700, log_level="info")
