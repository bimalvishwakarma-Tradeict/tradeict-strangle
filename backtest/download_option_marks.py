#!/usr/bin/env python3
"""
Download MARK:1m candles for BTC options (Delta India).

Fixes:
  - Full meta.after pagination (verify unique == meta.total_count)
  - Scope by LIVE WINDOW overlap with [--months] lookback (not calendar month)
  - --dry-run: symbol list + checks only, NO candle download

No print(). Output (dry-run): console + backtest/results/dryrun_symbols.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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

STRIKE_BAND = 6000.0
ENTRY_DTE = 2
ENTRY_HH = 11
ENTRY_MM = 0
SLEEP_S = 0.75
MAX_CANDLES = 4000
PAGE_SIZE = 500
# Daily options: ~10 calendar days of life before 12:00 UTC settlement
CONTRACT_LIFE_DAYS = 10
# Bytes/row observed from May pilot (~1.24GB / 7.7M rows)
BYTES_PER_MARK_ROW = 173.0
# ~1 request per MAX_CANDLES minutes of life
MINUTES_PER_REQUEST = float(MAX_CANDLES)

CACHE_DIR = _BACKTEST / "cache" / "option_marks"
PRODUCTS_DB = _BACKTEST / "cache" / "products_btc_options.sqlite"
RESULTS_DIR = _BACKTEST / "results"
DRYRUN_OUT = RESULTS_DIR / "dryrun_symbols.txt"

CHECK_SYM_MAY13 = "P-BTC-78000-150526"
CHECK_EXP_JUN01 = date(2026, 6, 1)

logger = logging.getLogger("download_option_marks")


def emit(lines: list[str] | None, line: str = "") -> None:
    if lines is not None:
        lines.append(line)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class OptProduct:
    symbol: str
    expiry: date
    opt_type: str  # call | put
    strike: float


@dataclass
class ProductsFetchResult:
    products: list[OptProduct]
    total_count: int | None
    pages: int
    unique: int
    raw_rows_seen: int
    complete: bool


def shard_path(year: int, month: int) -> Path:
    return CACHE_DIR / f"marks_{year:04d}-{month:02d}.sqlite"


def range_bounds_months(months: int, *, now: datetime | None = None) -> tuple[int, int]:
    """UTC unix [start, end] for last `months` (rolling from now)."""
    now_utc = now or datetime.now(tz=UTC)
    end = int(now_utc.timestamp())
    start = int((now_utc - timedelta(days=months * 365.25 / 12.0)).timestamp())
    return start, end


def settle_ts(expiry: date) -> int:
    return int(
        datetime(
            expiry.year, expiry.month, expiry.day, 12, 0, 0, tzinfo=UTC
        ).timestamp()
    )


def live_window(expiry: date) -> tuple[int, int]:
    """Contract live [life_start, settle] in unix seconds."""
    settle = settle_ts(expiry)
    life_start = settle - CONTRACT_LIFE_DAYS * 24 * 3600
    return life_start, settle


def windows_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def was_live_in_range(expiry: date, range_start: int, range_end: int) -> bool:
    life0, life1 = live_window(expiry)
    return windows_overlap(life0, life1, range_start, range_end)


def parse_product_row(row: dict[str, Any]) -> OptProduct | None:
    sett = str(row.get("settlement_time") or "")
    day = sett[:10]
    if len(day) != 10:
        return None
    sym = str(row.get("symbol") or "").strip()
    if not sym:
        return None
    try:
        strike = float(row.get("strike_price"))
    except (TypeError, ValueError):
        return None
    ctype = str(row.get("contract_type") or "").lower()
    if ctype == "call_options":
        opt = "call"
    elif ctype == "put_options":
        opt = "put"
    else:
        return None
    try:
        exp = date.fromisoformat(day)
    except ValueError:
        return None
    return OptProduct(symbol=sym, expiry=exp, opt_type=opt, strike=strike)


def init_products_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            symbol TEXT PRIMARY KEY,
            product_id INTEGER,
            contract_type TEXT,
            strike REAL,
            expiry_date TEXT,
            launch_time TEXT,
            state TEXT,
            raw_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def upsert_product_page(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """Write API page to disk immediately (before further processing)."""
    n = 0
    for row in rows:
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        sett = str(row.get("settlement_time") or "")
        expiry_date = sett[:10] if len(sett) >= 10 else None
        try:
            strike = float(row.get("strike_price")) if row.get("strike_price") is not None else None
        except (TypeError, ValueError):
            strike = None
        pid = row.get("id")
        try:
            product_id = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            product_id = None
        launch = row.get("launch_time") or row.get("auction_start_time") or row.get("created_at")
        conn.execute(
            """
            INSERT INTO products(
                symbol, product_id, contract_type, strike, expiry_date,
                launch_time, state, raw_json
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol) DO UPDATE SET
                product_id=excluded.product_id,
                contract_type=excluded.contract_type,
                strike=excluded.strike,
                expiry_date=excluded.expiry_date,
                launch_time=excluded.launch_time,
                state=excluded.state,
                raw_json=excluded.raw_json
            """,
            (
                sym,
                product_id,
                str(row.get("contract_type") or "") or None,
                strike,
                expiry_date,
                str(launch) if launch is not None else None,
                str(row.get("state") or "") or None,
                json.dumps(row, separators=(",", ":"), ensure_ascii=False),
            ),
        )
        n += 1
    conn.commit()
    return n


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


def products_db_stats(path: Path = PRODUCTS_DB) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "n_rows": 0, "size_bytes": 0, "path": str(path)}
    conn = sqlite3.connect(str(path))
    try:
        n = int(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0])
    finally:
        conn.close()
    return {
        "exists": True,
        "n_rows": n,
        "size_bytes": int(path.stat().st_size),
        "path": str(path),
    }


def fetch_all_btc_option_products(
    client: httpx.Client,
    lines: list[str] | None,
    *,
    products_conn: sqlite3.Connection | None = None,
) -> ProductsFetchResult:
    """
    Paginate /v2/products until meta.after is exhausted.
    FIX 1: do NOT early-stop on calendar month / oldest_on_page.
    Rule: each API page is written to disk BEFORE in-memory processing.
    """
    by_sym: dict[str, OptProduct] = {}
    after: str | None = None
    pages = 0
    total_count: int | None = None
    raw_rows_seen = 0
    rows_persisted = 0
    max_pages = 500  # safety; 500*500 = 250k rows
    complete = False

    if products_conn is None:
        products_conn = init_products_db(PRODUCTS_DB)
        own_conn = True
    else:
        own_conn = False

    try:
        while pages < max_pages:
            params: dict[str, Any] = {
                "contract_types": "call_options,put_options",
                "states": "expired,settled,live",
                "underlying_asset_symbols": "BTC",
                "page_size": PAGE_SIZE,
            }
            if after:
                params["after"] = after
            st, payload, raw = http_get_json(client, PRODUCTS_PATH, params)
            time.sleep(SLEEP_S)
            pages += 1
            if st != 200 or not isinstance(payload, dict):
                emit(lines, f"products page={pages} FAIL status={st} raw={raw}")
                break
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            if total_count is None and meta.get("total_count") is not None:
                try:
                    total_count = int(meta["total_count"])
                except (TypeError, ValueError):
                    total_count = None
            rows = payload.get("result") or []
            if not isinstance(rows, list) or not rows:
                emit(lines, f"products page={pages} empty result — stop")
                complete = True
                break
            raw_rows_seen += len(rows)

            # Disk first — never keep API product rows memory-only.
            dict_rows = [r for r in rows if isinstance(r, dict)]
            n_wrote = upsert_product_page(products_conn, dict_rows)
            rows_persisted += n_wrote

            n_new = 0
            oldest_on_page: str | None = None
            newest_on_page: str | None = None
            for row in dict_rows:
                sett = str(row.get("settlement_time") or "")
                day = sett[:10]
                if len(day) == 10:
                    if oldest_on_page is None or day < oldest_on_page:
                        oldest_on_page = day
                    if newest_on_page is None or day > newest_on_page:
                        newest_on_page = day
                prod = parse_product_row(row)
                if prod is None:
                    continue
                if prod.symbol not in by_sym:
                    n_new += 1
                by_sym[prod.symbol] = prod
            emit(
                lines,
                f"products page={pages} rows={len(rows)} persisted={n_wrote} "
                f"new={n_new} unique_so_far={len(by_sym)} "
                f"oldest={oldest_on_page} newest={newest_on_page} "
                f"total_count={total_count}",
            )
            after_val = meta.get("after")
            if not after_val:
                emit(lines, f"products pagination complete at page={pages} (no after)")
                complete = True
                break
            after = str(after_val)
        else:
            emit(lines, f"WARNING: hit max_pages={max_pages} — pagination may be incomplete")
            complete = False
    finally:
        if own_conn:
            products_conn.close()

    result = ProductsFetchResult(
        products=list(by_sym.values()),
        total_count=total_count,
        pages=pages,
        unique=len(by_sym),
        raw_rows_seen=raw_rows_seen,
        complete=complete,
    )
    # Attach persist stats for callers via emit (dataclass unchanged).
    emit(
        lines,
        f"products persisted_upserts={rows_persisted} db={PRODUCTS_DB}",
    )
    return result


def filter_live_window(
    products: list[OptProduct], range_start: int, range_end: int
) -> list[OptProduct]:
    """FIX 2: keep symbols whose live window overlaps the lookback range."""
    return [
        p
        for p in products
        if was_live_in_range(p.expiry, range_start, range_end)
    ]


def spot_at_entry(
    times: list[int], closes: list[float], expiry: date
) -> float | None:
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


def estimate_download(kept: list[OptProduct], range_start: int, range_end: int) -> dict[str, float]:
    """Rough request / time / GB estimates (no download)."""
    n_req = 0.0
    n_rows = 0.0
    for p in kept:
        life0, life1 = live_window(p.expiry)
        w0 = max(life0, range_start)
        w1 = min(life1, range_end)
        if w1 <= w0:
            continue
        minutes = (w1 - w0) / 60.0
        n_rows += minutes  # 1m bars
        n_req += max(1.0, math.ceil(minutes / MINUTES_PER_REQUEST))
    # product pages already done; candle requests dominate
    hours = (n_req * SLEEP_S) / 3600.0
    gb = (n_rows * BYTES_PER_MARK_ROW) / (1024.0**3)
    return {
        "n_symbols": float(len(kept)),
        "est_requests": n_req,
        "est_hours": hours,
        "est_gb": gb,
        "est_rows": n_rows,
    }


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
    expiry: date, range_start: int, range_end: int
) -> tuple[int, int]:
    life0, life1 = live_window(expiry)
    start = max(range_start, life0)
    end = min(range_end, life1)
    return start, end


def fetch_mark_candles(
    client: httpx.Client, symbol: str, start: int, end: int
) -> tuple[list[dict[str, Any]], str]:
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


def run_dry_run(months: int) -> int:
    lines: list[str] = []
    emit(lines, "OPTION MARK DOWNLOADER — DRY RUN")
    emit(lines, "=" * 80)
    emit(lines, f"--months={months}  --dry-run (NO candle download)")
    emit(lines, f"contract life assumption: {CONTRACT_LIFE_DAYS}d before 12:00 UTC settle")
    emit(lines, f"strike band: entry_spot ± {STRIKE_BAND:g}")
    emit(lines, "")

    range_start, range_end = range_bounds_months(months)
    emit(
        lines,
        f"lookback range UTC: "
        f"{datetime.fromtimestamp(range_start, tz=UTC)} -> "
        f"{datetime.fromtimestamp(range_end, tz=UTC)}",
    )
    emit(lines, "")

    with httpx.Client() as client:
        fetch = fetch_all_btc_option_products(client, lines)

    emit(lines, "")
    emit(lines, "===== PAGINATION VERIFY (FIX 1) =====")
    emit(lines, f"meta.total_count (API): {fetch.total_count}")
    emit(lines, f"unique symbols fetched: {fetch.unique}")
    emit(lines, f"pages: {fetch.pages}  raw_rows_seen: {fetch.raw_rows_seen}")
    emit(lines, f"pagination_complete (no after left): {fetch.complete}")
    if fetch.total_count is None:
        emit(lines, "MATCH / MISMATCH: UNKNOWN (API did not return total_count) — RUK JAO")
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        DRYRUN_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2
    if fetch.unique == fetch.total_count and fetch.complete:
        emit(lines, "MATCH / MISMATCH: MATCH (unique == total_count, cursor exhausted)")
        mismatch_stop = False
    elif (
        fetch.unique > fetch.total_count
        and fetch.complete
        and fetch.total_count == 10000
    ):
        # Delta India meta.total_count is capped at 10000 while after-cursor
        # continues — treat exhausted pagination as MATCH with CAP note.
        emit(
            lines,
            "MATCH / MISMATCH: MATCH "
            f"(unique={fetch.unique} > total_count={fetch.total_count}; "
            "API total_count appears CAPPED at 10000; after-cursor exhausted)",
        )
        mismatch_stop = False
    elif fetch.unique < fetch.total_count or not fetch.complete:
        emit(
            lines,
            f"MATCH / MISMATCH: MISMATCH "
            f"(unique={fetch.unique} total_count={fetch.total_count} "
            f"complete={fetch.complete}) — RUK JAO",
        )
        mismatch_stop = True
    else:
        emit(
            lines,
            f"MATCH / MISMATCH: MISMATCH "
            f"(unique={fetch.unique} != total_count={fetch.total_count}) — RUK JAO",
        )
        mismatch_stop = True

    # FIX 2 live-window scope
    live_list = filter_live_window(fetch.products, range_start, range_end)
    emit(lines, "")
    emit(lines, "===== LIVE-WINDOW SCOPE (FIX 2) =====")
    emit(lines, f"symbols after live-window overlap filter: {len(live_list)}")

    times, closes = ot.load_spot_1m()
    kept, band_meta = filter_band(live_list, times, closes)
    emit(lines, f"after strike band ±{STRIKE_BAND:g}: {len(kept)}")
    if band_meta["no_spot"]:
        emit(
            lines,
            f"expiries with no spot (skipped): {len(band_meta['no_spot'])} "
            f"e.g. {band_meta['no_spot'][:5]}",
        )

    if live_list:
        exps = sorted({p.expiry for p in live_list})
        oldest_exp = exps[0]
        newest_exp = exps[-1]
    else:
        oldest_exp = newest_exp = None
    emit(
        lines,
        f"date range (expiries in live-window list): "
        f"oldest={oldest_exp}  newest={newest_exp}",
    )

    est = estimate_download(kept, range_start, range_end)
    emit(lines, "")
    emit(lines, "===== ESTIMATES (band-filtered, no download) =====")
    emit(lines, f"estimated requests: {est['est_requests']:.0f}")
    emit(lines, f"estimated hours (@ {SLEEP_S}s sleep): {est['est_hours']:.2f}")
    emit(lines, f"estimated GB: {est['est_gb']:.2f}")
    emit(lines, f"estimated 1m rows: {est['est_rows']:.0f}")

    # Specific checks
    emit(lines, "")
    emit(lines, "===== SPECIFIC CHECKS =====")
    syms_live = {p.symbol for p in live_list}
    check1 = CHECK_SYM_MAY13 in syms_live
    emit(
        lines,
        f"CHECK 1: {CHECK_SYM_MAY13} in live-window list? "
        f"{'PASS' if check1 else 'FAIL'}",
    )

    jun01 = [p for p in live_list if p.expiry == CHECK_EXP_JUN01]
    # Also verify they overlap May 2026
    may_start = int(datetime(2026, 5, 1, 0, 0, tzinfo=UTC).timestamp())
    may_end = int(datetime(2026, 6, 1, 0, 0, tzinfo=UTC).timestamp()) - 1
    jun01_live_in_may = [
        p for p in jun01 if was_live_in_range(p.expiry, may_start, may_end)
    ]
    check2 = len(jun01_live_in_may) > 0
    emit(
        lines,
        f"CHECK 2: any expiry={CHECK_EXP_JUN01.isoformat()} live in 2026-05 "
        f"in list? {'PASS' if check2 else 'FAIL'}  "
        f"(n={len(jun01_live_in_may)}; e.g. "
        f"{[p.symbol for p in jun01_live_in_may[:5]]})",
    )

    if oldest_exp is not None:
        # ~24 months before "now"
        target = (datetime.now(tz=UTC) - timedelta(days=24 * 365.25 / 12.0)).date()
        delta_days = abs((oldest_exp - target).days)
        # PASS if within ~45 days of 24m lookback floor
        check3 = delta_days <= 45
        emit(
            lines,
            f"CHECK 3: oldest expiry={oldest_exp.isoformat()} "
            f"(target≈{target.isoformat()}, |Δ|={delta_days}d) "
            f"{'PASS' if check3 else 'FAIL'}",
        )
    else:
        check3 = False
        emit(lines, "CHECK 3: oldest expiry=n/a FAIL (empty list)")

    emit(lines, "")
    emit(lines, "DONE (dry-run).")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    DRYRUN_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote %s", DRYRUN_OUT)

    if mismatch_stop or not (check1 and check2 and check3):
        return 1
    return 0


def run_download(months: int) -> int:
    """Non-dry path: download marks for live-window ∩ band (resumable by month shard)."""
    lines: list[str] | None = None
    emit(lines, "OPTION MARK DOWNLOADER — DOWNLOAD")
    emit(lines, "=" * 80)
    range_start, range_end = range_bounds_months(months)
    emit(
        lines,
        f"lookback months={months} UTC: "
        f"{datetime.fromtimestamp(range_start, tz=UTC)} -> "
        f"{datetime.fromtimestamp(range_end, tz=UTC)}",
    )

    times, closes = ot.load_spot_1m()
    with httpx.Client() as client:
        fetch = fetch_all_btc_option_products(client, lines)
        if fetch.total_count is not None:
            capped_ok = (
                fetch.complete
                and fetch.unique > fetch.total_count
                and fetch.total_count == 10000
            )
            exact_ok = fetch.complete and fetch.unique == fetch.total_count
            if not (exact_ok or capped_ok):
                emit(
                    None,
                    f"ERROR: pagination MISMATCH unique={fetch.unique} "
                    f"total_count={fetch.total_count} complete={fetch.complete} "
                    "— abort download",
                )
                return 2
        live_list = filter_live_window(fetch.products, range_start, range_end)
        kept, meta = filter_band(live_list, times, closes)
        emit(lines, f"live-window={len(live_list)} after band={len(kept)}")

        # Shard by expiry month
        by_ym: dict[tuple[int, int], list[OptProduct]] = {}
        for p in kept:
            key = (p.expiry.year, p.expiry.month)
            by_ym.setdefault(key, []).append(p)

        for (year, month), prods in sorted(by_ym.items()):
            path = shard_path(year, month)
            conn = init_db(path)
            emit(lines, f"shard {year:04d}-{month:02d} symbols={len(prods)} -> {path}")
            total = len(prods)
            for i, prod in enumerate(
                sorted(prods, key=lambda x: (x.expiry, x.strike, x.opt_type)), 1
            ):
                prev = progress_status(conn, prod.symbol)
                if prev is not None and prev[0] in {"done", "empty"}:
                    continue
                w0, w1 = contract_window(prod.expiry, range_start, range_end)
                if w1 <= w0:
                    mark_done(conn, prod.symbol, "empty", 0, "window_empty")
                    continue
                rows, detail = fetch_mark_candles(client, prod.symbol, w0, w1)
                if detail != "ok" and not rows:
                    mark_done(conn, prod.symbol, "error", 0, detail)
                    emit(lines, f"[{i}/{total}] ERROR {prod.symbol}: {detail}")
                    continue
                n = upsert_candles(conn, prod, rows)
                if n == 0:
                    mark_done(conn, prod.symbol, "empty", 0, detail)
                else:
                    mark_done(conn, prod.symbol, "done", n, detail)
                if i % 25 == 0 or i == total:
                    emit(lines, f"progress {i}/{total} last={prod.symbol} rows={n}")
            conn.close()
    emit(lines, "DOWNLOAD DONE.")
    return 0


def run_save_products() -> int:
    """PART A: paginate products and permanently persist full API rows."""
    lines: list[str] = []
    emit(lines, "=== SAVE PRODUCTS ===")
    emit(lines, f"db={PRODUCTS_DB}")
    emit(lines, "NO candle download — products metadata only")
    emit(lines, "")
    with httpx.Client() as client:
        conn = init_products_db(PRODUCTS_DB)
        try:
            fetch = fetch_all_btc_option_products(client, lines, products_conn=conn)
        finally:
            conn.close()
    stats = products_db_stats(PRODUCTS_DB)
    size_mb = stats["size_bytes"] / (1024 * 1024) if stats["size_bytes"] else 0.0
    emit(lines, "")
    emit(lines, "===== PERSIST VERIFY =====")
    emit(lines, f"pages={fetch.pages}")
    emit(lines, f"raw_rows_seen={fetch.raw_rows_seen}")
    emit(lines, f"unique_symbols={fetch.unique}")
    emit(lines, f"api_total_count={fetch.total_count}")
    emit(lines, f"pagination_complete={fetch.complete}")
    emit(lines, f"n_rows_saved={stats['n_rows']}")
    emit(lines, f"file_size_bytes={stats['size_bytes']}")
    emit(lines, f"file_size_mb={size_mb:.2f}")
    logger.info(
        "SAVE PRODUCTS n_rows=%s size_mb=%.2f",
        stats["n_rows"],
        size_mb,
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Delta India option MARK candle downloader")
    p.add_argument(
        "--months",
        type=int,
        default=1,
        help="Lookback months for live-window overlap (default 1)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build symbol list + checks only; do not download candles",
    )
    p.add_argument(
        "--save-products",
        action="store_true",
        help="Paginate all BTC option products and save full rows to SQLite (no candles)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(argv)
    if args.save_products:
        return run_save_products()
    if args.months < 1:
        emit(None, "ERROR: --months must be >= 1")
        return 2
    if args.dry_run:
        return run_dry_run(int(args.months))
    return run_download(int(args.months))


if __name__ == "__main__":
    raise SystemExit(main())
