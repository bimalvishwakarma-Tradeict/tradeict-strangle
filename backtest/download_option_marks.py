#!/usr/bin/env python3
"""
Download MARK:1m candles for BTC options (Delta India).

Final settings:
  resolution   = 1m
  strike band  = ±6000
  order        = OLDEST FIRST (from 2024-09)

Flags:
  --month YYYY-MM   single calendar expiry-month
  --months N        lookback range, oldest-first shards
  --sleep SECONDS   rate control between candle requests
  --tail-days N     short-dated options: only last N days (default 5);
                    monthlies (life > 14d) still download full life
  --dry-run / --save-products  (existing)

Rule: every API response is written to disk BEFORE processing.
No print(). Completeness: console + backtest/results/completeness_YYYY-MM.txt
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

# --- FINAL SETTINGS (locked) ---
RESOLUTION = "1m"
STRIKE_BAND = 6000.0
OLDEST_START = date(2024, 9, 1)
ENTRY_DTE = 2
ENTRY_HH = 11
ENTRY_MM = 0
DEFAULT_SLEEP_S = 0.75
SLEEP_S = DEFAULT_SLEEP_S
MAX_CANDLES = 4000
PAGE_SIZE = 500
CONTRACT_LIFE_DAYS = 10
SHORT_DATED_MAX_LIFE_DAYS = 14
DEFAULT_TAIL_DAYS = 5
BYTES_PER_MARK_ROW = 173.0
MINUTES_PER_REQUEST = float(MAX_CANDLES)
RATE_TEST_SLEEP = 0.35
RATE_TEST_N = 300

CACHE_DIR = _BACKTEST / "cache" / "option_marks"
PRODUCTS_DB = _BACKTEST / "cache" / "products_btc_options.sqlite"
SPOT_CACHE_DB = _BACKTEST / "cache" / "spot_btc_entry.sqlite"
RESULTS_DIR = _BACKTEST / "results"
DRYRUN_OUT = RESULTS_DIR / "dryrun_symbols.txt"
RAW_CANDLES_DIR = CACHE_DIR / "raw_candles"

CHECK_SYM_MAY13 = "P-BTC-78000-150526"
CHECK_EXP_JUN01 = date(2026, 6, 1)

logger = logging.getLogger("download_option_marks")

# Mutable rate-limit counters (reset per run section)
_429_COUNT = 0
_HTTP_GETS = 0


def reset_rate_counters() -> None:
    global _429_COUNT, _HTTP_GETS
    _429_COUNT = 0
    _HTTP_GETS = 0


def set_sleep(seconds: float) -> None:
    global SLEEP_S
    SLEEP_S = float(seconds)


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
    launch_ts: int | None = None  # unix seconds; None → life unknown


@dataclass
class ProductsFetchResult:
    products: list[OptProduct]
    total_count: int | None
    pages: int
    unique: int
    raw_rows_seen: int
    complete: bool


@dataclass
class SymbolDownloadResult:
    symbol: str
    status: str  # done | empty | error
    n_rows: int
    expected: int
    detail: str
    has_gap: bool


def shard_path(year: int, month: int) -> Path:
    return CACHE_DIR / f"marks_{year:04d}-{month:02d}.sqlite"


def completeness_path(year: int, month: int) -> Path:
    return RESULTS_DIR / f"completeness_{year:04d}-{month:02d}.txt"


def raw_candle_dir(year: int, month: int) -> Path:
    d = RAW_CANDLES_DIR / f"{year:04d}-{month:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parse_year_month(s: str) -> tuple[int, int]:
    parts = s.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"expected YYYY-MM, got {s!r}")
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12):
        raise ValueError(f"invalid month in {s!r}")
    return year, month


def month_utc_bounds(year: int, month: int) -> tuple[int, int]:
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp()) - 1


def range_bounds_months(months: int, *, now: datetime | None = None) -> tuple[int, int]:
    """UTC unix [start, end] for last `months`, floored at OLDEST_START."""
    now_utc = now or datetime.now(tz=UTC)
    end = int(now_utc.timestamp())
    start_dt = now_utc - timedelta(days=months * 365.25 / 12.0)
    floor = datetime(
        OLDEST_START.year, OLDEST_START.month, OLDEST_START.day, 0, 0, 0, tzinfo=UTC
    )
    start = int(max(start_dt, floor).timestamp())
    return start, end


def settle_ts(expiry: date) -> int:
    return int(
        datetime(
            expiry.year, expiry.month, expiry.day, 12, 0, 0, tzinfo=UTC
        ).timestamp()
    )


def live_window(expiry: date) -> tuple[int, int]:
    """Legacy full-assumption window [life_start, settle] (CONTRACT_LIFE_DAYS)."""
    settle = settle_ts(expiry)
    life_start = settle - CONTRACT_LIFE_DAYS * 24 * 3600
    return life_start, settle


def parse_launch_ts(value: Any) -> int | None:
    """Parse product launch/listing time to unix seconds (UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        # ms vs s heuristic
        if ts > 10_000_000_000:
            ts //= 1000
        return ts if ts > 0 else None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.isdigit():
            return parse_launch_ts(int(s))
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return None


def product_life_seconds(product: OptProduct) -> int:
    """
    Listed life in seconds (launch → settle).
    Unknown launch → CONTRACT_LIFE_DAYS (short-dated assumption).
    """
    settle = settle_ts(product.expiry)
    if product.launch_ts is not None and int(product.launch_ts) < settle:
        return max(0, settle - int(product.launch_ts))
    return int(CONTRACT_LIFE_DAYS * 24 * 3600)


def is_short_dated_option(product: OptProduct) -> bool:
    """Daily/weekly: life <= 14d. Monthly/hedge: life > 14d."""
    return product_life_seconds(product) <= int(SHORT_DATED_MAX_LIFE_DAYS * 86400)


def download_window(
    product: OptProduct,
    *,
    tail_days: int,
    range_start: int | None = None,
    range_end: int | None = None,
) -> tuple[int, int]:
    """
    Candle download [start, end] for one symbol.

    Short-dated (life <= 14d): only last `tail_days` before settle.
    Longer life (monthlies): full listing life (launch → settle).
    """
    settle = settle_ts(product.expiry)
    life_sec = product_life_seconds(product)
    if life_sec <= int(SHORT_DATED_MAX_LIFE_DAYS * 86400):
        n = max(1, int(tail_days))
        life0 = settle - n * 24 * 3600
        life1 = settle
    else:
        life0 = settle - life_sec
        life1 = settle
    start = life0 if range_start is None else max(int(range_start), life0)
    end = life1 if range_end is None else min(int(range_end), life1)
    return start, end


def expected_candles_for_window(w0: int, w1: int) -> int:
    """1m bars in [w0, w1) roughly — live minutes."""
    if w1 <= w0:
        return 0
    return int((w1 - w0) // 60)


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
    launch_raw = (
        row.get("launch_time")
        or row.get("auction_start_time")
        or row.get("created_at")
    )
    return OptProduct(
        symbol=sym,
        expiry=exp,
        opt_type=opt,
        strike=strike,
        launch_ts=parse_launch_ts(launch_raw),
    )


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
    global _429_COUNT, _HTTP_GETS
    q = urlencode({k: str(v) for k, v in params.items()})
    url = f"{BASE_URL}{path}?{q}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Tradeict-MarkDownloader/1.0",
    }
    _HTTP_GETS += 1
    try:
        resp = client.get(url, headers=headers, timeout=60.0)
    except httpx.HTTPError as exc:
        return 0, None, f"HTTPError: {exc}"
    if resp.status_code == 429:
        _429_COUNT += 1
        wait = min(60.0, max(SLEEP_S, 0.35) * (2**attempt))
        logger.warning("429 rate limit — sleep %.1fs (count=%s)", wait, _429_COUNT)
        time.sleep(wait)
        if attempt < 6:
            return http_get_json(client, path, params, attempt=attempt + 1)
        return 429, None, resp.text[:500]
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    return resp.status_code, payload, resp.text[:500]


def write_json_disk(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )


def load_products_from_db(path: Path = PRODUCTS_DB) -> list[OptProduct]:
    if not path.is_file():
        return []
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT symbol, contract_type, strike, expiry_date, "
            "launch_time, raw_json FROM products"
        ).fetchall()
    finally:
        conn.close()
    out: list[OptProduct] = []
    for symbol, ctype, strike, expiry_date, launch_time, raw_json in rows:
        if raw_json:
            try:
                row = json.loads(raw_json)
                if isinstance(row, dict):
                    p = parse_product_row(row)
                    if p is not None:
                        out.append(p)
                        continue
            except json.JSONDecodeError:
                pass
        # fallback columns
        try:
            exp = date.fromisoformat(str(expiry_date))
            k = float(strike)
        except (TypeError, ValueError):
            continue
        ct = str(ctype or "").lower()
        if ct == "call_options":
            opt = "call"
        elif ct == "put_options":
            opt = "put"
        else:
            continue
        sym = str(symbol or "").strip()
        if not sym:
            continue
        out.append(
            OptProduct(
                symbol=sym,
                expiry=exp,
                opt_type=opt,
                strike=k,
                launch_ts=parse_launch_ts(launch_time),
            )
        )
    return out


def ensure_products(client: httpx.Client, lines: list[str] | None) -> list[OptProduct]:
    products = load_products_from_db()
    if products:
        emit(lines, f"products loaded from disk: n={len(products)} db={PRODUCTS_DB}")
        return products
    emit(lines, "products DB missing — fetching from API (will persist)")
    conn = init_products_db(PRODUCTS_DB)
    try:
        fetch = fetch_all_btc_option_products(client, lines, products_conn=conn)
    finally:
        conn.close()
    return fetch.products


def init_spot_cache(path: Path = SPOT_CACHE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS spot_entry (
            expiry_date TEXT PRIMARY KEY,
            entry_ts INTEGER NOT NULL,
            spot REAL NOT NULL,
            raw_json TEXT
        )
        """
    )
    conn.commit()
    return conn


def fetch_spot_at_ts(
    client: httpx.Client,
    ts: int,
    *,
    raw_dir: Path,
) -> tuple[float | None, str]:
    """Fetch BTCUSD 1m close near ts; write API response to disk first."""
    start = ts - 3600
    end = ts + 60
    params = {
        "symbol": "BTCUSD",
        "resolution": RESOLUTION,
        "start": start,
        "end": end,
    }
    st, payload, raw = http_get_json(client, CANDLES_PATH, params)
    time.sleep(SLEEP_S)
    raw_path = raw_dir / f"BTCUSD_{start}_{end}.json"
    write_json_disk(
        raw_path,
        {
            "http_status": st,
            "params": params,
            "payload": payload,
            "raw_text": raw if payload is None else None,
        },
    )
    if st != 200 or not isinstance(payload, dict):
        return None, f"spot_http_{st}"
    result = payload.get("result")
    if not isinstance(result, list) or not result:
        return None, "spot_empty"
    best: tuple[int, float] | None = None
    for row in result:
        if not isinstance(row, dict) or "time" not in row:
            continue
        try:
            t = int(row["time"])
            c = float(row["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if c <= 0:
            continue
        d = abs(t - ts)
        if best is None or d < best[0]:
            best = (d, c)
    if best is None:
        return None, "spot_no_close"
    return best[1], "ok"


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
    times: list[int],
    closes: list[float],
    expiry: date,
    *,
    client: httpx.Client | None = None,
    spot_conn: sqlite3.Connection | None = None,
    raw_dir: Path | None = None,
) -> float | None:
    entry_day = expiry - timedelta(days=ENTRY_DTE)
    for delta in (0, -1, 1, -2, 2):
        d = entry_day + timedelta(days=delta)
        when = ot.ist_to_utc(d, ENTRY_HH, ENTRY_MM)
        ts = int(when.timestamp())
        sp = ot.spot_at(times, closes, ts)
        if sp is not None and sp > 0:
            return float(sp)

    # Cache / API fallback (local spot CSV may not cover 2024)
    if spot_conn is not None:
        row = spot_conn.execute(
            "SELECT spot FROM spot_entry WHERE expiry_date=?",
            (expiry.isoformat(),),
        ).fetchone()
        if row is not None and float(row[0]) > 0:
            return float(row[0])

    if client is None or raw_dir is None:
        return None

    when = ot.ist_to_utc(entry_day, ENTRY_HH, ENTRY_MM)
    ts = int(when.timestamp())
    spot, detail = fetch_spot_at_ts(client, ts, raw_dir=raw_dir)
    if spot is None or spot <= 0:
        logger.warning("spot API miss expiry=%s detail=%s", expiry, detail)
        return None
    if spot_conn is not None:
        spot_conn.execute(
            """
            INSERT INTO spot_entry(expiry_date, entry_ts, spot, raw_json)
            VALUES (?,?,?,?)
            ON CONFLICT(expiry_date) DO UPDATE SET
              entry_ts=excluded.entry_ts,
              spot=excluded.spot,
              raw_json=excluded.raw_json
            """,
            (expiry.isoformat(), ts, float(spot), json.dumps({"detail": detail})),
        )
        spot_conn.commit()
    return float(spot)


def filter_band(
    products: list[OptProduct],
    times: list[int],
    closes: list[float],
    *,
    client: httpx.Client | None = None,
    spot_conn: sqlite3.Connection | None = None,
    raw_dir: Path | None = None,
) -> tuple[list[OptProduct], dict[str, Any]]:
    by_exp: dict[date, list[OptProduct]] = {}
    for p in products:
        by_exp.setdefault(p.expiry, []).append(p)
    kept: list[OptProduct] = []
    meta: dict[str, Any] = {"expiries": [], "no_spot": []}
    for exp in sorted(by_exp):
        spot = spot_at_entry(
            times, closes, exp, client=client, spot_conn=spot_conn, raw_dir=raw_dir
        )
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
    # OLDEST FIRST
    kept.sort(key=lambda x: (x.expiry, x.strike, x.opt_type, x.symbol))
    return kept, meta


def estimate_download(
    kept: list[OptProduct],
    range_start: int,
    range_end: int,
    *,
    tail_days: int | None = None,
) -> dict[str, float]:
    """
    Rough request / time / GB estimates (no download).

    tail_days=None → legacy full CONTRACT_LIFE_DAYS window for every symbol.
    tail_days=N    → short-dated last N days; monthlies full listing life.
    """
    n_req = 0.0
    n_rows = 0.0
    n_short = 0.0
    n_long = 0.0
    for p in kept:
        if tail_days is None:
            life0, life1 = live_window(p.expiry)
            w0 = max(life0, range_start)
            w1 = min(life1, range_end)
        else:
            if is_short_dated_option(p):
                n_short += 1.0
            else:
                n_long += 1.0
            w0, w1 = download_window(
                p,
                tail_days=int(tail_days),
                range_start=range_start,
                range_end=range_end,
            )
        if w1 <= w0:
            continue
        minutes = (w1 - w0) / 60.0
        n_rows += minutes  # 1m bars
        n_req += max(1.0, math.ceil(minutes / MINUTES_PER_REQUEST))
    hours = (n_req * SLEEP_S) / 3600.0
    gb = (n_rows * BYTES_PER_MARK_ROW) / (1024.0**3)
    return {
        "n_symbols": float(len(kept)),
        "n_short": n_short,
        "n_long": n_long,
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


def progress_status(
    conn: sqlite3.Connection, symbol: str
) -> tuple[str, int, str] | None:
    row = conn.execute(
        "SELECT status, n_rows, detail FROM download_progress WHERE symbol=?",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), int(row[1]), str(row[2] or "")


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
    expiry: date, range_start: int | None = None, range_end: int | None = None
) -> tuple[int, int]:
    """Legacy helper — full CONTRACT_LIFE_DAYS window (no tail)."""
    life0, life1 = live_window(expiry)
    start = life0 if range_start is None else max(range_start, life0)
    end = life1 if range_end is None else min(range_end, life1)
    return start, end


def symbol_covers_window(
    conn: sqlite3.Connection, symbol: str, w0: int, w1: int
) -> bool:
    """
    True when the REQUIRED window [w0, w1] is densely covered.

    Extra candles outside the window do not count as incomplete.
    """
    if w1 <= w0:
        return True
    row = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM marks "
        "WHERE symbol=? AND ts>=? AND ts<=?",
        (symbol, int(w0), int(w1)),
    ).fetchone()
    if row is None or row[0] is None or int(row[0]) <= 0:
        return False
    n, mn, mx = int(row[0]), int(row[1]), int(row[2])
    expected = expected_candles_for_window(w0, w1)
    if expected <= 0:
        return True
    if n < int(expected * 0.98):
        return False
    span_min = (int(mx) - int(mn)) // 60 + 1
    if span_min < int(expected * 0.95):
        return False
    return True


def symbol_has_gap(conn: sqlite3.Connection, symbol: str, w0: int, w1: int) -> bool:
    """True if required window is not covered (extra history is OK)."""
    return not symbol_covers_window(conn, symbol, w0, w1)


def fetch_mark_candles(
    client: httpx.Client,
    symbol: str,
    start: int,
    end: int,
    *,
    raw_dir: Path,
) -> tuple[list[dict[str, Any]], str]:
    """
    Fetch MARK:1m candles. Each HTTP payload is written to disk BEFORE parse/merge.
    """
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
            "resolution": RESOLUTION,
            "start": chunk_start,
            "end": cursor_end,
        }
        attempt = 0
        st = 0
        payload: Any = None
        raw = ""
        while True:
            st, payload, raw = http_get_json(
                client, CANDLES_PATH, params, attempt=attempt
            )
            if st == 429:
                attempt += 1
                if attempt > 6:
                    write_json_disk(
                        raw_dir / f"{symbol}_{chunk_start}_{cursor_end}_429.json",
                        {"http_status": 429, "params": params, "raw_text": raw},
                    )
                    return list(all_rows.values()), f"429 exhausted: {raw}"
                continue
            break
        time.sleep(SLEEP_S)

        write_json_disk(
            raw_dir / f"{symbol}_{chunk_start}_{cursor_end}.json",
            {
                "http_status": st,
                "params": params,
                "payload": payload,
                "raw_text": raw if payload is None else None,
            },
        )

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


def run_dry_run(months: int, *, tail_days: int = DEFAULT_TAIL_DAYS) -> int:
    lines: list[str] = []
    emit(lines, "OPTION MARK DOWNLOADER — DRY RUN")
    emit(lines, "=" * 80)
    emit(lines, f"--months={months}  --dry-run (NO candle download)")
    emit(lines, f"--tail-days={tail_days}")
    emit(lines, f"contract life assumption (short unk.): {CONTRACT_LIFE_DAYS}d before 12:00 UTC settle")
    emit(lines, f"short-dated if life <= {SHORT_DATED_MAX_LIFE_DAYS}d → last {tail_days}d only")
    emit(lines, f"longer life (monthlies) → full listing life")
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
        f"{oldest_exp} -> {newest_exp}",
    )

    emit(lines, "")
    emit(lines, "===== DOWNLOAD SCOPE ESTIMATE (tail-days) =====")
    est_old = estimate_download(kept, range_start, range_end, tail_days=None)
    est_new = estimate_download(
        kept, range_start, range_end, tail_days=int(tail_days)
    )
    req_old = float(est_old["est_requests"])
    req_new = float(est_new["est_requests"])
    hrs_old = float(est_old["est_hours"])
    hrs_new = float(est_new["est_hours"])
    req_saved = max(0.0, req_old - req_new)
    hrs_saved = max(0.0, hrs_old - hrs_new)
    pct = (100.0 * req_saved / req_old) if req_old > 0 else 0.0
    emit(lines, f"symbols after band: {int(est_new['n_symbols'])}")
    emit(
        lines,
        f"short-dated (life<={SHORT_DATED_MAX_LIFE_DAYS}d): "
        f"{int(est_new['n_short'])}  "
        f"monthlies/long (full life): {int(est_new['n_long'])}",
    )
    emit(lines, f"OLD scope (full {CONTRACT_LIFE_DAYS}d life each):")
    emit(
        lines,
        f"  requests≈{req_old:.0f}  hours≈{hrs_old:.1f}  "
        f"rows≈{est_old['est_rows']:.0f}  GB≈{est_old['est_gb']:.2f}",
    )
    emit(lines, f"NEW scope (tail_days={tail_days}, monthlies full):")
    emit(
        lines,
        f"  requests≈{req_new:.0f}  hours≈{hrs_new:.1f}  "
        f"rows≈{est_new['est_rows']:.0f}  GB≈{est_new['est_gb']:.2f}",
    )
    emit(
        lines,
        f"ESTIMATED SAVINGS: requests≈{req_saved:.0f} ({pct:.1f}%)  "
        f"hours≈{hrs_saved:.1f}",
    )
    emit(lines, f"(sleep={SLEEP_S}s between candle requests)")
    emit(lines, f"estimated requests (new): {est_new['est_requests']:.0f}")
    emit(lines, f"estimated hours (new): {est_new['est_hours']:.2f}")
    emit(lines, f"estimated GB (new): {est_new['est_gb']:.3f}")

    emit(lines, "")
    emit(lines, "===== SPOT / CHECKS =====")

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


def download_one_symbol(
    client: httpx.Client,
    conn: sqlite3.Connection,
    prod: OptProduct,
    *,
    raw_dir: Path,
    force: bool = False,
    tail_days: int = DEFAULT_TAIL_DAYS,
) -> SymbolDownloadResult:
    w0, w1 = download_window(prod, tail_days=int(tail_days))
    expected = expected_candles_for_window(w0, w1)
    prev = progress_status(conn, prod.symbol)
    if not force and prev is not None and prev[0] in {"done", "empty"}:
        # Resume: required window covered (extra history is fine, not incomplete)
        if prev[0] == "empty":
            return SymbolDownloadResult(
                symbol=prod.symbol,
                status=prev[0],
                n_rows=prev[1],
                expected=expected,
                detail=f"skipped_existing:{prev[2]}",
                has_gap=True,
            )
        if symbol_covers_window(conn, prod.symbol, w0, w1):
            return SymbolDownloadResult(
                symbol=prod.symbol,
                status=prev[0],
                n_rows=prev[1],
                expected=expected,
                detail=f"skipped_existing_covers_window:{prev[2]}",
                has_gap=False,
            )
    if w1 <= w0:
        mark_done(conn, prod.symbol, "empty", 0, "window_empty")
        return SymbolDownloadResult(
            prod.symbol, "empty", 0, expected, "window_empty", True
        )
    rows, detail = fetch_mark_candles(
        client, prod.symbol, w0, w1, raw_dir=raw_dir
    )
    if detail != "ok" and not rows:
        mark_done(conn, prod.symbol, "error", 0, detail)
        return SymbolDownloadResult(
            prod.symbol, "error", 0, expected, detail, True
        )
    n = upsert_candles(conn, prod, rows)
    if n == 0:
        mark_done(conn, prod.symbol, "empty", 0, detail)
        return SymbolDownloadResult(
            prod.symbol, "empty", 0, expected, detail, True
        )
    mark_done(conn, prod.symbol, "done", n, detail)
    has_gap = symbol_has_gap(conn, prod.symbol, w0, w1)
    return SymbolDownloadResult(
        prod.symbol, "done", n, expected, detail, has_gap
    )


def emit_completeness_report(
    lines: list[str],
    *,
    year: int,
    month: int,
    results: list[SymbolDownloadResult],
    shard: Path,
    band_meta: dict[str, Any],
) -> None:
    emit(lines, "")
    emit(lines, f"===== COMPLETENESS REPORT {year:04d}-{month:02d} =====")
    attempted = len(results)
    ok = [r for r in results if r.status == "done" and r.n_rows > 0]
    failed = [r for r in results if r.status != "done" or r.n_rows <= 0]
    gap_syms = [r for r in results if r.has_gap]
    emit(lines, f"total symbols attempted: {attempted}")
    emit(lines, f"successful: {len(ok)}")
    emit(lines, f"failed: {len(failed)}")
    emit(lines, "")
    emit(lines, "----- FAILED SYMBOLS (full list) -----")
    if not failed:
        emit(lines, "(none)")
    else:
        for r in failed:
            emit(
                lines,
                f"{r.symbol}  status={r.status}  actual={r.n_rows}  "
                f"expected={r.expected}  reason={r.detail}",
            )
    emit(lines, "")
    emit(lines, "----- PER SYMBOL expected vs actual -----")
    for r in results:
        pct = (100.0 * r.n_rows / r.expected) if r.expected > 0 else float("nan")
        emit(
            lines,
            f"{r.symbol}  expected={r.expected}  actual={r.n_rows}  "
            f"pct={pct:.2f}  gap={'Y' if r.has_gap else 'N'}  "
            f"status={r.status}",
        )
    total_exp = sum(r.expected for r in results)
    total_act = sum(r.n_rows for r in results)
    overall = (100.0 * total_act / total_exp) if total_exp > 0 else float("nan")
    size_b = shard.stat().st_size if shard.is_file() else 0
    size_mb = size_b / (1024 * 1024)
    emit(lines, "")
    emit(lines, f"overall completeness % (sum actual / sum expected): {overall:.4f}")
    emit(lines, f"symbols with gap: {len(gap_syms)}")
    emit(lines, f"file: {shard}")
    emit(lines, f"file_size_bytes={size_b}")
    emit(lines, f"file_size_mb={size_mb:.2f}")
    if band_meta.get("no_spot"):
        emit(lines, f"expiries skipped (no spot): {band_meta['no_spot']}")


def run_rate_test(
    lines: list[str],
    client: httpx.Client,
    conn: sqlite3.Connection,
    prods: list[OptProduct],
    *,
    year: int,
    month: int,
    tail_days: int = DEFAULT_TAIL_DAYS,
) -> None:
    raw_dir = raw_candle_dir(year, month) / "rate_test"
    raw_dir.mkdir(parents=True, exist_ok=True)
    subset = prods[-RATE_TEST_N:] if len(prods) > RATE_TEST_N else list(prods)
    emit(lines, "")
    emit(lines, f"===== RATE TEST (last {len(subset)} symbols, sleep={RATE_TEST_SLEEP}) =====")
    reset_rate_counters()
    set_sleep(RATE_TEST_SLEEP)
    t0 = time.time()
    n_ok = 0
    n_err = 0
    for i, prod in enumerate(subset, 1):
        r = download_one_symbol(
            client,
            conn,
            prod,
            raw_dir=raw_dir,
            force=True,
            tail_days=tail_days,
        )
        if r.status == "done":
            n_ok += 1
        else:
            n_err += 1
        if i % 50 == 0 or i == len(subset):
            emit(
                lines,
                f"rate_test progress {i}/{len(subset)} last={prod.symbol} "
                f"status={r.status} 429s={_429_COUNT}",
            )
    elapsed = time.time() - t0
    emit(lines, f"rate_test done: ok={n_ok} err={n_err} elapsed_s={elapsed:.1f}")
    emit(lines, f"429_count={_429_COUNT}")
    emit(lines, f"http_gets={_HTTP_GETS}")
    if _429_COUNT == 0:
        emit(lines, f"verdict: sleep={RATE_TEST_SLEEP} SAFE (no 429)")
        n_month = max(1, len(prods))
        n_months_full = 24
        req_per_sym = max(
            1.0, (max(1, int(tail_days)) * 1440) / MINUTES_PER_REQUEST
        )
        est_req = n_month * n_months_full * req_per_sym
        est_hours = (est_req * RATE_TEST_SLEEP) / 3600.0
        emit(
            lines,
            f"full_run_estimate (@ sleep={RATE_TEST_SLEEP}): "
            f"~{est_req:.0f} requests, ~{est_hours:.1f} hours "
            f"(assumes ~{n_month} symbols/month × {n_months_full} months × "
            f"~{req_per_sym:.1f} req/symbol @ tail_days={tail_days})",
        )
        if elapsed > 0 and subset:
            per_sym = elapsed / len(subset)
            est_hours_emp = (n_month * n_months_full * per_sym) / 3600.0
            emit(
                lines,
                f"full_run_estimate_empirical: ~{est_hours_emp:.1f} hours "
                f"({per_sym:.2f}s/symbol observed in rate test)",
            )
    else:
        emit(
            lines,
            f"verdict: sleep={RATE_TEST_SLEEP} NOT SAFE "
            f"({_429_COUNT} × 429 seen) — keep DEFAULT_SLEEP_S={DEFAULT_SLEEP_S}",
        )


def run_month_download(
    year: int, month: int, *, sleep_s: float, tail_days: int = DEFAULT_TAIL_DAYS
) -> int:
    """Download one expiry-calendar month: oldest-first, completeness + rate test."""
    lines: list[str] = []
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    set_sleep(sleep_s)
    reset_rate_counters()
    emit(lines, "OPTION MARK DOWNLOADER — SINGLE MONTH")
    emit(lines, "=" * 80)
    emit(lines, f"month={year:04d}-{month:02d}")
    emit(lines, f"resolution={RESOLUTION}  strike_band=±{STRIKE_BAND:g}")
    emit(lines, f"order=OLDEST_FIRST  sleep={SLEEP_S}  tail_days={tail_days}")
    emit(lines, f"oldest_allowed_start={OLDEST_START.isoformat()}")
    emit(lines, "")

    if date(year, month, 1) < date(OLDEST_START.year, OLDEST_START.month, 1):
        emit(lines, f"ERROR: month before OLDEST_START={OLDEST_START}")
        completeness_path(year, month).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2

    times, closes = ot.load_spot_1m()
    shard = shard_path(year, month)
    raw_dir = raw_candle_dir(year, month)
    spot_raw = raw_dir / "spot"
    spot_raw.mkdir(parents=True, exist_ok=True)

    with httpx.Client() as client:
        products = ensure_products(client, lines)
        month_prods = [
            p
            for p in products
            if p.expiry.year == year and p.expiry.month == month
        ]
        emit(lines, f"products in expiry-month: {len(month_prods)}")
        spot_conn = init_spot_cache()
        try:
            kept, band_meta = filter_band(
                month_prods,
                times,
                closes,
                client=client,
                spot_conn=spot_conn,
                raw_dir=spot_raw,
            )
        finally:
            spot_conn.close()
        emit(lines, f"after strike band ±{STRIKE_BAND:g}: {len(kept)}")
        if band_meta["no_spot"]:
            emit(
                lines,
                f"no_spot expiries skipped: {len(band_meta['no_spot'])} "
                f"{band_meta['no_spot'][:10]}",
            )
        if not kept:
            emit(lines, "ERROR: no symbols after band filter")
            out = completeness_path(year, month)
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")
            sys.stdout.write("\n".join(lines) + "\n")
            return 1

        # Scope estimate before download
        m0, m1 = month_utc_bounds(year, month)
        est_old = estimate_download(kept, m0, m1, tail_days=None)
        est_new = estimate_download(kept, m0, m1, tail_days=int(tail_days))
        emit(lines, "")
        emit(lines, "===== DOWNLOAD SCOPE ESTIMATE =====")
        emit(
            lines,
            f"OLD requests≈{est_old['est_requests']:.0f} hours≈{est_old['est_hours']:.1f}",
        )
        emit(
            lines,
            f"NEW requests≈{est_new['est_requests']:.0f} hours≈{est_new['est_hours']:.1f} "
            f"(short={int(est_new['n_short'])} long={int(est_new['n_long'])})",
        )
        emit(
            lines,
            f"ESTIMATED SAVINGS: requests≈"
            f"{max(0.0, est_old['est_requests'] - est_new['est_requests']):.0f}  "
            f"hours≈{max(0.0, est_old['est_hours'] - est_new['est_hours']):.1f}",
        )

        conn = init_db(shard)
        results: list[SymbolDownloadResult] = []
        total = len(kept)
        emit(lines, f"downloading {total} symbols -> {shard}")
        t0 = time.time()
        for i, prod in enumerate(kept, 1):
            r = download_one_symbol(
                client,
                conn,
                prod,
                raw_dir=raw_dir,
                force=False,
                tail_days=tail_days,
            )
            results.append(r)
            if r.status == "error":
                emit(lines, f"[{i}/{total}] ERROR {prod.symbol}: {r.detail}")
            elif i % 25 == 0 or i == total:
                emit(
                    lines,
                    f"progress {i}/{total} last={prod.symbol} "
                    f"rows={r.n_rows} status={r.status} 429s={_429_COUNT}",
                )
        main_elapsed = time.time() - t0
        emit(lines, f"main download elapsed_s={main_elapsed:.1f} 429s={_429_COUNT}")

        emit_completeness_report(
            lines,
            year=year,
            month=month,
            results=results,
            shard=shard,
            band_meta=band_meta,
        )

        run_rate_test(
            lines,
            client,
            conn,
            kept,
            year=year,
            month=month,
            tail_days=tail_days,
        )
        conn.close()

    out_path = completeness_path(year, month)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Wrote completeness report %s", out_path)
    return 0


def run_download(
    months: int, *, sleep_s: float, tail_days: int = DEFAULT_TAIL_DAYS
) -> int:
    """Multi-month lookback, OLDEST FIRST shards (from OLDEST_START)."""
    lines: list[str] = []
    set_sleep(sleep_s)
    reset_rate_counters()
    emit(lines, "OPTION MARK DOWNLOADER — RANGE (oldest first)")
    emit(lines, "=" * 80)
    range_start, range_end = range_bounds_months(months)
    emit(
        lines,
        f"lookback months={months} UTC: "
        f"{datetime.fromtimestamp(range_start, tz=UTC)} -> "
        f"{datetime.fromtimestamp(range_end, tz=UTC)}",
    )
    emit(
        lines,
        f"resolution={RESOLUTION} band=±{STRIKE_BAND:g} "
        f"sleep={SLEEP_S} tail_days={tail_days}",
    )

    times, closes = ot.load_spot_1m()
    with httpx.Client() as client:
        products = ensure_products(client, lines)
        live_list = filter_live_window(products, range_start, range_end)
        spot_conn = init_spot_cache()
        spot_raw = RAW_CANDLES_DIR / "spot_range"
        spot_raw.mkdir(parents=True, exist_ok=True)
        try:
            kept, _meta = filter_band(
                live_list,
                times,
                closes,
                client=client,
                spot_conn=spot_conn,
                raw_dir=spot_raw,
            )
        finally:
            spot_conn.close()
        emit(lines, f"live-window={len(live_list)} after band={len(kept)}")

        est_old = estimate_download(
            kept, range_start, range_end, tail_days=None
        )
        est_new = estimate_download(
            kept, range_start, range_end, tail_days=int(tail_days)
        )
        emit(lines, "")
        emit(lines, "===== DOWNLOAD SCOPE ESTIMATE =====")
        emit(
            lines,
            f"OLD requests≈{est_old['est_requests']:.0f} hours≈{est_old['est_hours']:.1f}",
        )
        emit(
            lines,
            f"NEW requests≈{est_new['est_requests']:.0f} hours≈{est_new['est_hours']:.1f} "
            f"(short={int(est_new['n_short'])} long={int(est_new['n_long'])})",
        )
        emit(
            lines,
            f"ESTIMATED SAVINGS: requests≈"
            f"{max(0.0, est_old['est_requests'] - est_new['est_requests']):.0f}  "
            f"hours≈{max(0.0, est_old['est_hours'] - est_new['est_hours']):.1f}",
        )

        by_ym: dict[tuple[int, int], list[OptProduct]] = {}
        for p in kept:
            key = (p.expiry.year, p.expiry.month)
            by_ym.setdefault(key, []).append(p)

        for (year, month), prods in sorted(by_ym.items()):  # oldest first
            path = shard_path(year, month)
            raw_dir = raw_candle_dir(year, month)
            conn = init_db(path)
            prods_sorted = sorted(
                prods, key=lambda x: (x.expiry, x.strike, x.opt_type, x.symbol)
            )
            emit(lines, f"shard {year:04d}-{month:02d} symbols={len(prods_sorted)} -> {path}")
            total = len(prods_sorted)
            for i, prod in enumerate(prods_sorted, 1):
                r = download_one_symbol(
                    client,
                    conn,
                    prod,
                    raw_dir=raw_dir,
                    force=False,
                    tail_days=tail_days,
                )
                if r.status == "error":
                    emit(lines, f"[{i}/{total}] ERROR {prod.symbol}: {r.detail}")
                elif i % 25 == 0 or i == total:
                    emit(
                        lines,
                        f"progress {i}/{total} last={prod.symbol} rows={r.n_rows}",
                    )
            conn.close()
    emit(lines, "DOWNLOAD DONE.")
    return 0


def run_save_products() -> int:
    """Paginate products and permanently persist full API rows."""
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
        "--month",
        type=str,
        default=None,
        help="Single expiry month YYYY-MM (e.g. 2024-09)",
    )
    p.add_argument(
        "--months",
        type=int,
        default=None,
        help="Lookback months (oldest-first shards from 2024-09)",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_S,
        help=f"Seconds between candle API calls (default {DEFAULT_SLEEP_S})",
    )
    p.add_argument(
        "--tail-days",
        type=int,
        default=DEFAULT_TAIL_DAYS,
        help=(
            f"Short-dated options (life<={SHORT_DATED_MAX_LIFE_DAYS}d): "
            f"download only last N days before expiry (default {DEFAULT_TAIL_DAYS}). "
            "Monthlies (life>14d) still download full listing life."
        ),
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
    if args.sleep <= 0:
        emit(None, "ERROR: --sleep must be > 0")
        return 2
    if int(args.tail_days) < 1:
        emit(None, "ERROR: --tail-days must be >= 1")
        return 2
    set_sleep(float(args.sleep))
    tail_days = int(args.tail_days)

    if args.save_products:
        return run_save_products()

    if args.dry_run:
        months = int(args.months) if args.months is not None else 24
        if months < 1:
            emit(None, "ERROR: --months must be >= 1")
            return 2
        return run_dry_run(months, tail_days=tail_days)

    if args.month:
        try:
            year, month = parse_year_month(args.month)
        except ValueError as exc:
            emit(None, f"ERROR: {exc}")
            return 2
        return run_month_download(
            year, month, sleep_s=float(args.sleep), tail_days=tail_days
        )

    if args.months is not None:
        if args.months < 1:
            emit(None, "ERROR: --months must be >= 1")
            return 2
        return run_download(
            int(args.months), sleep_s=float(args.sleep), tail_days=tail_days
        )

    emit(None, "ERROR: provide --month YYYY-MM or --months N (or --dry-run / --save-products)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
