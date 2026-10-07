#!/usr/bin/env python3
"""Local Delta India BTCUSD chart server. Public market data only. Port 8700.

python chart_app\\server.py
"""

from __future__ import annotations

import asyncio
import csv
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
from strategies import PLUGINS, compute, load_plugins  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "BTCUSD"
MAX_BARS = 1500
# STEP 1: 1000/2000/3000 returned in full; 6000-min window returned 4000 from the newest end.
DELTA_PAGE_CAP = 4000
_CANDLE_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}
RES_SEC = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}
logger = logging.getLogger("chart_app")

app = FastAPI(title="chart_app")
load_plugins()
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


async def _candles(tf: str, start: int, end: int) -> list[dict[str, Any]]:
    if tf not in RES_SEC:
        raise HTTPException(400, f"bad tf {tf}")
    res = RES_SEC[tf]
    want_start = int(start)
    page_end = int(end)
    if page_end <= want_start:
        logger.info("tf=%s range=%s..%s pages=0 rows=0", tf, want_start, page_end)
        return []
    now = int(time.time())
    closed_before = now - res
    by_ts: dict[int, dict[str, Any]] = {}
    pages = 0
    empty_streak = 0
    span = DELTA_PAGE_CAP * res
    async with httpx.AsyncClient() as client:
        while page_end > want_start:
            page_start = max(want_start, page_end - span)
            pages += 1
            cache_key = (tf, int(page_start))
            chunk_closed = page_end < closed_before
            rows_chunk: list[dict[str, Any]] = []
            if chunk_closed and cache_key in _CANDLE_CACHE:
                rows_chunk = _CANDLE_CACHE[cache_key]
            else:
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
                closed_rows = [r for r in rows_chunk if int(r["time"]) < closed_before]
                if chunk_closed:
                    _CANDLE_CACHE[cache_key] = closed_rows
            got = 0
            for row in rows_chunk:
                ts = int(row["time"])
                if ts < want_start or ts > int(end):
                    continue
                by_ts[ts] = row
                got += 1
            if got == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            if empty_streak >= 3:
                break
            if page_start <= want_start:
                break
            page_end = page_start
            await asyncio.sleep(0.15)
    rows = [by_ts[k] for k in sorted(by_ts)]
    logger.info(
        "tf=%s range=%s..%s pages=%s rows=%s",
        tf,
        want_start,
        int(end),
        pages,
        len(rows),
    )
    return rows


@app.get("/api/candles")
async def api_candles(
    tf: str = Query("1m"),
    end: int | None = None,
    limit: int = Query(MAX_BARS),
) -> dict[str, Any]:
    limit = max(50, min(int(limit), 4000))
    res = RES_SEC[tf] if tf in RES_SEC else 60
    now = int(time.time())
    end_ts = int(end) if end is not None else now
    start_ts = end_ts - limit * res
    rows = await _candles(tf, start_ts, end_ts)
    if len(rows) > limit:
        rows = rows[-limit:]
    first = int(rows[0]["time"]) if rows else None
    last = int(rows[-1]["time"]) if rows else None
    return {
        "symbol": SYMBOL,
        "tf": tf,
        "candles": rows,
        "count": len(rows),
        "first": first,
        "last": last,
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
) -> dict[str, Any]:
    if strategy not in PLUGINS:
        raise HTTPException(404, f"unknown strategy {strategy}")
    now = int(time.time())
    to_u = int(to_ts) if to_ts is not None else now
    from_u = int(from_ts) if from_ts is not None else to_u - int(hours * 3600)
    warmup = 3 * 86400
    bars = await _candles("1m", from_u - warmup, to_u)
    bars = [b for b in bars if from_u - warmup <= int(b["time"]) <= to_u]
    data = compute(
        strategy,
        bars,
        {
            "line_tf": line_tf,
            "hours": hours,
            "from": from_u,
            "to": to_u,
            "chart_tf": tf,
            "variant": variant,
        },
    )
    return {"strategy": strategy, "bars_used": len(bars), **data}


def _runs_root() -> Path:
    return ROOT / "backtest" / "strategies"


@app.get("/api/trades/files")
async def api_trade_files() -> dict[str, Any]:
    root = _runs_root()
    files: list[str] = []
    if root.is_dir():
        for p in sorted(root.glob("*/runs/*_trades.csv")):
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
