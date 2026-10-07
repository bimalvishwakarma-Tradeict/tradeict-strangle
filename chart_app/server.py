#!/usr/bin/env python3
"""Local Delta India BTCUSD chart server. Public market data only. Port 8700.

python chart_app\\server.py
"""

from __future__ import annotations

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


async def _candles(tf: str, start: int, end: int) -> list[dict[str, Any]]:
    if tf not in RES_SEC:
        raise HTTPException(400, f"bad tf {tf}")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Tradeict-chart-app/1.0",
    }
    by_ts: dict[int, dict[str, Any]] = {}
    async with httpx.AsyncClient() as client:
        # reuse download_candles page fetch (same URL, params, retries)
        raw = await _fetch_page(
            client, symbol=SYMBOL, resolution=tf, start=int(start), end=int(end)
        )
    for r in raw:
        if "time" not in r:
            continue
        ts = int(r["time"])
        by_ts[ts] = {
            "time": ts,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r.get("volume") or 0.0),
        }
    return [by_ts[k] for k in sorted(by_ts)]


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
    return {"symbol": SYMBOL, "tf": tf, "candles": rows}


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
        {"line_tf": line_tf, "hours": hours, "to": to_u, "chart_tf": tf, "variant": variant},
    )
    return {"strategy": strategy, **data}


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
