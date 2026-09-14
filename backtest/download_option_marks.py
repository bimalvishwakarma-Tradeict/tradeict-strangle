#!/usr/bin/env python3
"""
Pilot: download MARK:1m candles for BTC options — May 2026 only.

Resumable SQLite store under backtest/cache/option_marks/marks_YYYY-MM.sqlite
No print(). No full 2y download.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402

BASE_URL = "https://api.india.delta.exchange"
CANDLES_PATH = "/v2/history/candles"
PRODUCTS_PATH = "/v2/products"
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

PILOT_YEAR = 2026
PILOT_MONTH = 5
STRIKE_BAND = 6000.0
ENTRY_DTE = 2
ENTRY_HH = 11
ENTRY_MM = 0
SLEEP_S = 0.75
MAX_CANDLES = 4000
PAGE_SIZE = 500

CACHE_DIR = _BACKTEST / "cache" / "option_marks"
RESULTS_DIR = _BACKTEST / "results"
# Downloader progress also lands in the shared pilot report via calibrate;
# this script emits its own short summary to stdout.

logger = logging.getLogger("download_option_marks")


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class OptProduct:
    symbol: str
    expiry: date
    opt_type: str  # call | put
    strike: float


def month_bounds_ist(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=IST)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=IST)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=IST)
    return int(start.timestamp()), int(end.timestamp()) - 1


def shard_path(year: int, month: int) -> Path:
    return CACHE_DIR / f"marks_{year:04d}-{month:02d}.sqlite"


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS marks (
            symbol TEXT NOT NULL,
            ts INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            expiry TEXT NOT NULL,
            opt_type TEXT NOT NULL,
            strike REAL NOT NULL,
            PRIMARY KEY (symbol, ts)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS download_progress (
            symbol TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            n_rows INTEGER NOT NULL DEFAULT 0,
            detail TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_marks_expiry ON marks(expiry)"
    )
    conn.commit()
    return conn


def http_get_json(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
    *,
    attempt: int = 0,
) -> tuple[int, Any, str]:
    q = urlencode({k: str(v) for k, v in params.items()})
    url = f"{BASE_URL}{path}?{q}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Tradeict-MarkDownloader/1.0",
    }
    try:
        resp = client.get(url, headers=headers, timeout=60.0)
    except httpx.HTTPError as exc:
        return 0, None, f"HTTPError: {exc}"
    if resp.status_code == 429:
        wait = min(60.0, SLEEP_S * (2**attempt))
        logger.warning("429 rate limit — sleep %.1fs", wait)
        time.sleep(wait)
        if attempt < 6:
            return http_get_json(client, path, params, attempt=attempt + 1)
        return 429, None, resp.text[:500]
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    return resp.status_code, payload, resp.text[:500]


def fetch_may_products(client: httpx.Client) -> list[OptProduct]:
    """Paginate expired BTC options; keep settlement in pilot month."""
    out: list[OptProduct] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {
            "contract_types": "call_options,put_options",
            "states": "expired",
            "underlying_asset_symbols": "BTC",
            "page_size": PAGE_SIZE,
        }
        if after:
            params["after"] = after
        st, payload, raw = http_get_json(client, PRODUCTS_PATH, params)
        time.sleep(SLEEP_S)
        pages += 1
        if st != 200 or not isinstance(payload, dict):
            emit(f"products page={pages} FAIL status={st} raw={raw}")
            break
        rows = payload.get("result") or []
        if not isinstance(rows, list) or not rows:
            break
        oldest_on_page: str | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            sett = str(row.get("settlement_time") or "")
            day = sett[:10]
            if len(day) != 10:
                continue
            if oldest_on_page is None or day < oldest_on_page:
                oldest_on_page = day
            if not day.startswith(f"{PILOT_YEAR:04d}-{PILOT_MONTH:02d}"):
                continue
            sym = str(row.get("symbol") or "")
            try:
                strike = float(row.get("strike_price"))
            except (TypeError, ValueError):
                continue
            ctype = str(row.get("contract_type") or "").lower()
            if ctype == "call_options":
                opt = "call"
            elif ctype == "put_options":
                opt = "put"
            else:
                continue
            try:
                exp = date.fromisoformat(day)
            except ValueError:
                continue
            out.append(OptProduct(symbol=sym, expiry=exp, opt_type=opt, strike=strike))
        emit(
            f"products page={pages} rows={len(rows)} "
            f"may_so_far={len(out)} oldest_on_page={oldest_on_page}"
        )
        meta = payload.get("meta") or {}
        after = meta.get("after") if isinstance(meta, dict) else None
        if not after:
            break
        # Past April → May window fully scanned
        if oldest_on_page and oldest_on_page < f"{PILOT_YEAR:04d}-{PILOT_MONTH:02d}-01":
            break
        if pages > 40:
            emit("products pagination safety stop at 40 pages")
            break
    # dedupe by symbol
    by_sym = {p.symbol: p for p in out}
    return list(by_sym.values())


def spot_at_entry(times: list[int], closes: list[float], expiry: date) -> float | None:
    entry_day = expiry - timedelta(days=ENTRY_DTE)
    for delta in (0, -1, 1, -2, 2):
        d = entry_day + timedelta(days=delta)
        when = ot.ist_to_utc(d, ENTRY_HH, ENTRY_MM)
        sp = ot.spot_at(times, closes, int(when.timestamp()))
        if sp is not None and sp > 0:
            return float(sp)
    return None


def filter_band(
    products: list[OptProduct], times: list[int], closes: list[float]
) -> tuple[list[OptProduct], dict[str, Any]]:
    by_exp: dict[date, list[OptProduct]] = {}
    for p in products:
        by_exp.setdefault(p.expiry, []).append(p)
    kept: list[OptProduct] = []
    meta: dict[str, Any] = {"expiries": [], "no_spot": []}
    for exp in sorted(by_exp):
        spot = spot_at_entry(times, closes, exp)
        if spot is None:
            meta["no_spot"].append(exp.isoformat())
            continue
        lo, hi = spot - STRIKE_BAND, spot + STRIKE_BAND
        n_keep = 0
        for p in by_exp[exp]:
            if lo <= p.strike <= hi:
                kept.append(p)
                n_keep += 1
        meta["expiries"].append(
            {
                "expiry": exp.isoformat(),
                "spot_entry": spot,
                "band": [lo, hi],
                "n_products": len(by_exp[exp]),
                "n_kept": n_keep,
            }
        )
    return kept, meta


def progress_status(conn: sqlite3.Connection, symbol: str) -> tuple[str, int] | None:
    row = conn.execute(
        "SELECT status, n_rows FROM download_progress WHERE symbol=?",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), int(row[1])


def mark_done(
    conn: sqlite3.Connection, symbol: str, status: str, n_rows: int, detail: str = ""
) -> None:
    conn.execute(
        """
        INSERT INTO download_progress(symbol, status, n_rows, detail, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
          status=excluded.status,
          n_rows=excluded.n_rows,
          detail=excluded.detail,
          updated_at=excluded.updated_at
        """,
        (
            symbol,
            status,
            n_rows,
            detail,
            datetime.now(tz=UTC).isoformat(),
        ),
    )
    conn.commit()


def contract_window(
    expiry: date, month_start: int, month_end: int
) -> tuple[int, int]:
    """
    Download only within pilot month ∩ contract life.
    Daily options: keep ~10d before expiry through settlement 12:00 UTC.
    """
    settle = int(
        datetime(expiry.year, expiry.month, expiry.day, 12, 0, tzinfo=UTC).timestamp()
    )
    life_start = settle - 10 * 24 * 3600
    start = max(month_start, life_start)
    end = min(month_end, settle)
    return start, end


def fetch_mark_candles(
    client: httpx.Client, symbol: str, start: int, end: int
) -> tuple[list[dict[str, Any]], str]:
    """Fetch MARK:symbol 1m candles for [start, end], paging older chunks."""
    if end <= start:
        return [], "ok_empty_window"
    mark_sym = f"MARK:{symbol}"
    all_rows: dict[int, dict[str, Any]] = {}
    cursor_end = end
    pages = 0
    while cursor_end > start:
        pages += 1
        if pages > 40:
            return list(all_rows.values()), "page_safety_stop"
        chunk_start = max(start, cursor_end - MAX_CANDLES * 60 + 60)
        params = {
            "symbol": mark_sym,
            "resolution": "1m",
            "start": chunk_start,
            "end": cursor_end,
        }
        attempt = 0
        while True:
            st, payload, raw = http_get_json(
                client, CANDLES_PATH, params, attempt=attempt
            )
            if st == 429:
                attempt += 1
                if attempt > 6:
                    return list(all_rows.values()), f"429 exhausted: {raw}"
                continue
            break
        time.sleep(SLEEP_S)
        if st != 200 or not isinstance(payload, dict):
            return list(all_rows.values()), f"status={st} raw={raw}"
        result = payload.get("result")
        if not isinstance(result, list) or not result:
            # step back anyway — do not abort whole contract on one empty chunk
            cursor_end = chunk_start - 1
            continue
        oldest = None
        for row in result:
            if not isinstance(row, dict) or "time" not in row:
                continue
            ts = int(row["time"])
            if ts < start or ts > end:
                continue
            all_rows[ts] = row
            if oldest is None or ts < oldest:
                oldest = ts
        if oldest is None:
            cursor_end = chunk_start - 1
            continue
        if oldest <= start:
            break
        cursor_end = oldest - 1
    return list(all_rows.values()), "ok"


def upsert_candles(
    conn: sqlite3.Connection, product: OptProduct, rows: list[dict[str, Any]]
) -> int:
    batch = []
    for row in rows:
        try:
            ts = int(row["time"])
            o = float(row["open"])
            h = float(row["high"])
            low = float(row["low"])
            c = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        batch.append(
            (
                product.symbol,
                ts,
                o,
                h,
                low,
                c,
                product.expiry.isoformat(),
                product.opt_type,
                float(product.strike),
            )
        )
    if not batch:
        return 0
    conn.executemany(
        """
        INSERT INTO marks(symbol, ts, open, high, low, close, expiry, opt_type, strike)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, ts) DO UPDATE SET
          open=excluded.open,
          high=excluded.high,
          low=excluded.low,
          close=excluded.close
        """,
        batch,
    )
    conn.commit()
    return len(batch)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    emit("OPTION MARK DOWNLOADER — PILOT 2026-05")
    emit("=" * 80)
    start_ts, end_ts = month_bounds_ist(PILOT_YEAR, PILOT_MONTH)
    emit(
        f"window IST month: {datetime.fromtimestamp(start_ts, tz=IST)} -> "
        f"{datetime.fromtimestamp(end_ts, tz=IST)}"
    )
    emit(f"strike band: entry_spot ± {STRIKE_BAND:g} (entry dte={ENTRY_DTE} @ {ENTRY_HH:02d}:{ENTRY_MM:02d} IST)")

    times, closes = ot.load_spot_1m()
    path = shard_path(PILOT_YEAR, PILOT_MONTH)
    conn = init_db(path)
    emit(f"sqlite: {path}")

    with httpx.Client() as client:
        products = fetch_may_products(client)
        emit(f"May products from /v2/products: {len(products)}")
        kept, meta = filter_band(products, times, closes)
        emit(f"after ±{STRIKE_BAND:g} filter: {len(kept)} symbols")
        emit(f"expiries detail: {json.dumps(meta['expiries'][:5])} ... total_exp={len(meta['expiries'])}")
        if meta["no_spot"]:
            emit(f"expiries with no spot (skipped): {meta['no_spot']}")

        total = len(kept)
        done_n = 0
        skip_n = 0
        empty_n = 0
        err_n = 0
        for i, prod in enumerate(sorted(kept, key=lambda p: (p.expiry, p.strike, p.opt_type)), 1):
            prev = progress_status(conn, prod.symbol)
            if prev is not None and prev[0] in {"done", "empty"}:
                skip_n += 1
                done_n += 1
                if i % 25 == 0 or i == total:
                    emit(f"progress {done_n}/{total} (skipped cached) symbol={prod.symbol}")
                continue

            w0, w1 = contract_window(prod.expiry, start_ts, end_ts)
            if w1 <= w0:
                empty_n += 1
                mark_done(conn, prod.symbol, "empty", 0, "window_empty")
                emit(f"[{i}/{total}] EMPTY_WINDOW {prod.symbol}")
                done_n += 1
                continue

            rows, detail = fetch_mark_candles(client, prod.symbol, w0, w1)
            if detail != "ok" and not rows:
                err_n += 1
                mark_done(conn, prod.symbol, "error", 0, detail)
                emit(f"[{i}/{total}] ERROR {prod.symbol}: {detail}")
                done_n += 1
                continue
            n = upsert_candles(conn, prod, rows)
            if n == 0:
                empty_n += 1
                mark_done(conn, prod.symbol, "empty", 0, detail)
                emit(f"[{i}/{total}] EMPTY {prod.symbol}")
            else:
                mark_done(conn, prod.symbol, "done", n, detail)
                emit(f"[{i}/{total}] DONE {prod.symbol} rows={n}")
            done_n += 1

    emit("")
    emit(
        f"SUMMARY: total={total} skip_cached={skip_n} empty={empty_n} "
        f"error={err_n} newly_ok={total - skip_n - empty_n - err_n}"
    )
    n_marks = conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    n_sym = conn.execute("SELECT COUNT(DISTINCT symbol) FROM marks").fetchone()[0]
    emit(f"DB: symbols_with_data={n_sym} total_mark_rows={n_marks}")
    conn.close()
    emit("DOWNLOAD DONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
