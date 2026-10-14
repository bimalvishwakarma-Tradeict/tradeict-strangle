#!/usr/bin/env python3
"""Record Delta candles into chart_app/tests/fixtures for offline e2e."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.pop("CHART_APP_FIXTURES", None)

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(ROOT))

from delta_rest import fetch_history_candles  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
OUT = APP / "tests" / "fixtures"
SYMBOL = "BTCUSD"
PAGE = 4000
RES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

# 1m covers tests 12–16 (Oct 3–6 IST). HTF covers LIVE TF cycle n>40.
RANGES = {
    "1m": (datetime(2026, 9, 25, 0, 0, tzinfo=IST), datetime(2026, 10, 7, 0, 0, tzinfo=IST)),
    "5m": (datetime(2026, 8, 15, 0, 0, tzinfo=IST), datetime(2026, 10, 7, 0, 0, tzinfo=IST)),
    "15m": (datetime(2026, 8, 15, 0, 0, tzinfo=IST), datetime(2026, 10, 7, 0, 0, tzinfo=IST)),
    "1h": (datetime(2026, 8, 15, 0, 0, tzinfo=IST), datetime(2026, 10, 7, 0, 0, tzinfo=IST)),
    "4h": (datetime(2026, 8, 1, 0, 0, tzinfo=IST), datetime(2026, 10, 7, 0, 0, tzinfo=IST)),
}


async def _one_tf(tf: str) -> None:
    start_dt, end_dt = RANGES[tf]
    start = int(start_dt.timestamp())
    end = int(end_dt.timestamp()) - 1
    res = RES[tf]
    span = PAGE * res
    page_end = ((end // span) + 1) * span
    by_ts: dict[int, list] = {}
    while page_end > start:
        page_start = page_end - span
        rows = await fetch_history_candles(SYMBOL, tf, page_start, page_end)
        for r in rows:
            ts = int(r["time"])
            if start <= ts <= end:
                by_ts[ts] = [ts, r["open"], r["high"], r["low"], r["close"], r["volume"]]
        if page_start <= start:
            break
        page_end = page_start
    bars = [by_ts[k] for k in sorted(by_ts)]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{SYMBOL}_{tf}.json.gz"
    payload = {"symbol": SYMBOL, "tf": tf, "start": start, "end": end, "bars": bars}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"{tf} n={len(bars)} -> {path}")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for tf in ("1m", "5m", "15m", "1h", "4h"):
        await _one_tf(tf)


if __name__ == "__main__":
    asyncio.run(main())
