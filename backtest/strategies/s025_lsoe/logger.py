#!/usr/bin/env python3
"""Forward snapshot logger: Delta options + Deribit books + Delta BTCUSD perp.

Interval 5 minutes. Raw gzip first, then sqlite. Never crash.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from backtest.strategies.s025_lsoe.parsers import (
    SNAPSHOT_COLUMNS,
    delta_ticker_to_row,
    deribit_summary_to_row,
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "s025_snapshots.sqlite"
HEARTBEAT = DATA / "logger_heartbeat.txt"
GAPS_LOG = DATA / "logger_gaps.log"
INTERVAL_S = 300.0
RETRY_WAIT_S = 30.0
GAP_THRESHOLD_S = 420.0
TIMEOUT = 45.0
UTC = timezone.utc
DELTA = "https://api.india.delta.exchange"
DERIBIT = "https://www.deribit.com/api/v2"
USER_AGENT = "Tradeict-S025-Logger/1.0"

logger = logging.getLogger("s025.logger")


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _ts_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            ts TEXT NOT NULL,
            src TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strike REAL,
            expiry TEXT,
            type TEXT,
            oi REAL,
            mark REAL,
            mark_iv REAL,
            bid REAL,
            ask REAL,
            bid_iv REAL,
            ask_iv REAL,
            delta REAL,
            gamma REAL,
            underlying REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_ts_src ON snapshots (ts, src)"
    )
    conn.commit()
    return conn


def write_raw(src: str, payload: Any, when: datetime) -> Path:
    day = when.strftime("%Y-%m-%d")
    hhmm = when.strftime("%H%M")
    folder = RAW / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{hhmm}_{src}.json.gz"
    blob = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    with gzip.open(path, "wb") as fh:
        fh.write(blob)
    return path


def insert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in SNAPSHOT_COLUMNS)
    sql = f"INSERT INTO snapshots ({','.join(SNAPSHOT_COLUMNS)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(row.get(col) for col in SNAPSHOT_COLUMNS) for row in rows])
    conn.commit()
    return len(rows)


def _unwrap(body: Any) -> Any:
    if isinstance(body, dict) and "result" in body:
        return body["result"]
    return body


def fetch_json(client: httpx.Client, url: str) -> Any:
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_exc = exc
            logger.warning("fetch failed attempt=%s url=%s err=%s", attempt, url, exc)
            if attempt == 1:
                time.sleep(RETRY_WAIT_S)
    raise RuntimeError(f"skip after retry: {url} ({last_exc})")


def snapshot_delta_options(client: httpx.Client, ts: str, when: datetime) -> list[dict[str, Any]]:
    qs = urlencode(
        {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": "BTC",
        }
    )
    url = f"{DELTA}/v2/tickers?{qs}"
    body = fetch_json(client, url)
    write_raw("delta", body, when)
    result = _unwrap(body)
    rows_out: list[dict[str, Any]] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                row = delta_ticker_to_row(ts, item)
                if row:
                    rows_out.append(row)
    return rows_out


def snapshot_deribit(client: httpx.Client, ts: str, when: datetime) -> list[dict[str, Any]]:
    url = f"{DERIBIT}/public/get_book_summary_by_currency?currency=BTC&kind=option"
    body = fetch_json(client, url)
    write_raw("deribit", body, when)
    result = _unwrap(body)
    rows_out: list[dict[str, Any]] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                row = deribit_summary_to_row(ts, item)
                if row:
                    rows_out.append(row)
    return rows_out


def snapshot_delta_perp(client: httpx.Client, ts: str, when: datetime) -> list[dict[str, Any]]:
    url = f"{DELTA}/v2/tickers/BTCUSD"
    body = fetch_json(client, url)
    write_raw("delta_perp", body, when)
    result = _unwrap(body)
    ticker = result if isinstance(result, dict) else {}
    if isinstance(result, list) and result and isinstance(result[0], dict):
        ticker = result[0]
    row = delta_ticker_to_row(ts, ticker if ticker else {"symbol": "BTCUSD"})
    if row is None:
        return []
    row["src"] = "delta_perp"
    row["symbol"] = str(ticker.get("symbol") or "BTCUSD")
    row["type"] = "perp"
    row["oi"] = row["oi"] if row["oi"] is not None else None
    # funding lives on ticker; keep as mark if missing already parsed
    if ticker.get("funding_rate") is not None:
        row["mark_iv"] = row["mark_iv"]  # unchanged; funding not an IV column
    return [row]


def read_heartbeat() -> datetime | None:
    if not HEARTBEAT.exists():
        return None
    text = HEARTBEAT.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_heartbeat(when: datetime) -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(_ts_iso(when), encoding="utf-8")


def record_gap(prev: datetime | None, now: datetime, reason: str) -> None:
    if prev is None:
        return
    gap = (now - prev).total_seconds()
    if gap < GAP_THRESHOLD_S:
        return
    GAPS_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = (
        f"{_ts_iso(prev)} -> {_ts_iso(now)} gap_sec={gap:.0f} reason={reason}\n"
    )
    with GAPS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    logger.warning("gap recorded %s", line.strip())


def run_cycle(client: httpx.Client, conn: sqlite3.Connection, when: datetime) -> None:
    ts = _ts_iso(when)
    jobs = (
        ("delta", snapshot_delta_options),
        ("deribit", snapshot_deribit),
        ("delta_perp", snapshot_delta_perp),
    )
    for name, fn in jobs:
        try:
            rows = fn(client, ts, when)
            n = insert_rows(conn, rows)
            logger.info("src=%s rows=%s", name, n)
        except Exception as exc:  # noqa: BLE001 — skip source, keep loop alive
            logger.error("src=%s skipped: %s", name, exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    DATA.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    conn = ensure_db(DB_PATH)
    client = httpx.Client(
        timeout=TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    logger.info("S025 logger start interval=%ss db=%s", INTERVAL_S, DB_PATH)
    try:
        while True:
            now = _now()
            prev = read_heartbeat()
            record_gap(prev, now, "pc_off_or_logger_stopped")
            try:
                run_cycle(client, conn, now)
            except Exception:
                logger.exception("cycle failed — continuing")
            write_heartbeat(_now())
            time.sleep(INTERVAL_S)
    except KeyboardInterrupt:
        logger.info("logger stopped by KeyboardInterrupt")
        return 0
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
