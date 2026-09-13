#!/usr/bin/env python3
"""
Options trade zip loader — parse, timezone-verify, inventory, reusable query API.

Reads Delta India BTC options trade prints from backtest/data_raw/*.zip
without extracting to disk. Builds optional monthly SQLite shards under
backtest/cache/options_trades/ for fast symbol/time queries.

stdlib only.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import math
import re
import sqlite3
import statistics
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

_BACKTEST = Path(__file__).resolve().parent
RAW_DIR = _BACKTEST / "data_raw"
CACHE_DIR = _BACKTEST / "cache" / "options_trades"
RESULTS_DIR = _BACKTEST / "results"
DATA_1M_DIR = _BACKTEST / "data_1m"

SYMBOL_RE = re.compile(
    r"^(?P<typ>[CP])-BTC-(?P<strike>\d+)-(?P<ddmmyy>\d{6})$"
)

ENTRY_TIMES_IST = ((9, 0), (11, 0), (13, 0), (15, 0))
WINDOWS_MIN = (5, 15, 30)


@dataclass(frozen=True)
class ParsedSymbol:
    symbol: str
    option_type: str  # C | P
    strike: float
    expiry_date: date


@dataclass(frozen=True)
class Trade:
    symbol: str
    price: float
    size: float
    ts_utc: datetime
    buyer_role: str  # maker | taker
    option_type: str
    strike: float
    expiry_date: date


def parse_symbol(symbol: str) -> ParsedSymbol | None:
    m = SYMBOL_RE.match(symbol.strip())
    if not m:
        return None
    ddmmyy = m.group("ddmmyy")
    dd = int(ddmmyy[0:2])
    mm = int(ddmmyy[2:4])
    yy = 2000 + int(ddmmyy[4:6])
    try:
        exp = date(yy, mm, dd)
    except ValueError:
        return None
    return ParsedSymbol(
        symbol=symbol.strip(),
        option_type=m.group("typ"),
        strike=float(m.group("strike")),
        expiry_date=exp,
    )


def parse_timestamp_naive(ts: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS[.ffffff]' as naive (timezone undecided until Gate 1)."""
    ts = ts.strip()
    if "." in ts:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")


def as_utc(naive: datetime) -> datetime:
    return naive.replace(tzinfo=UTC)


def as_ist_from_utc(utc_dt: datetime) -> datetime:
    return utc_dt.astimezone(IST)


def ist_to_utc(d: date, hour: int, minute: int = 0) -> datetime:
    local = datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)
    return local.astimezone(UTC)


# ---------------------------------------------------------------------------
# Source discovery (monthly preferred; skip daily if month covered)
# ---------------------------------------------------------------------------


def _month_key_from_name(name: str) -> str | None:
    m = re.search(r"options-trades-monthly-BTC-(\d{4})-(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    m = re.search(r"options-trades-daily-BTC-(\d{4})-(\d{2})-(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def _is_daily(name: str) -> bool:
    return "options-trades-daily-BTC-" in name


def _prefer_cleaner(paths: list[Path]) -> Path:
    """Prefer names without ' (1)' / duplicate suffixes."""
    scored = sorted(
        paths,
        key=lambda p: (
            " (1)" in p.name,
            p.name.count(" "),
            len(p.name),
            p.name,
        ),
    )
    return scored[0]


def discover_zip_sources(raw_dir: Path = RAW_DIR) -> list[Path]:
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Missing raw dir: {raw_dir}")
    monthly: dict[str, list[Path]] = defaultdict(list)
    daily: list[Path] = []
    for p in sorted(raw_dir.glob("*.zip")):
        if _is_daily(p.name):
            daily.append(p)
        else:
            mk = _month_key_from_name(p.name)
            if mk:
                monthly[mk].append(p)
    chosen: list[Path] = []
    months_covered: set[str] = set()
    for mk in sorted(monthly.keys()):
        c = _prefer_cleaner(monthly[mk])
        chosen.append(c)
        months_covered.add(mk)
        for dup in monthly[mk]:
            if dup != c:
                pass  # skipped duplicate
    for p in daily:
        mk = _month_key_from_name(p.name)
        if mk and mk in months_covered:
            continue  # month already covered
        chosen.append(p)
    return chosen


def iter_zip_rows(zp: Path) -> Iterator[dict[str, str]]:
    with zipfile.ZipFile(zp) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            reader = csv.DictReader(text)
            for row in reader:
                yield row


# ---------------------------------------------------------------------------
# Spot (1m) loader
# ---------------------------------------------------------------------------


def load_spot_1m(data_dir: Path = DATA_1M_DIR) -> tuple[list[int], list[float]]:
    matches = sorted(data_dir.glob("BTCUSD_1m_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_1m_*.csv in {data_dir}")
    path = matches[-1]
    times: list[int] = []
    closes: list[float] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            times.append(int(row["open_time_unix"]))
            closes.append(float(row["close"]))
    return times, closes


def spot_at(
    times: list[int], closes: list[float], ts_unix: int
) -> float | None:
    """Close of the 1m bar at or before ts_unix."""
    i = bisect.bisect_right(times, ts_unix) - 1
    if i < 0:
        return None
    return closes[i]


# ---------------------------------------------------------------------------
# Monthly SQLite cache + query API
# ---------------------------------------------------------------------------


def shard_path(month_key: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"opt_trades_{month_key}.sqlite"


class OptionsTradeStore:
    """
    Query API for the next task.

    trades_for_symbol(symbol, start_utc, end_utc) -> list[Trade]
    Uses monthly SQLite shards built by build_cache_and_inventory().
    """

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = cache_dir

    def _month_keys_overlapping(
        self, start: datetime, end: datetime
    ) -> list[str]:
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        keys: list[str] = []
        cur = date(start.year, start.month, 1)
        end_d = end.date()
        while cur <= end_d:
            keys.append(f"{cur.year:04d}-{cur.month:02d}")
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)
        return keys

    def trades_for_symbol(
        self,
        symbol: str,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[Trade]:
        start_ts = start_utc.timestamp()
        end_ts = end_utc.timestamp()
        parsed = parse_symbol(symbol)
        if parsed is None:
            return []
        out: list[Trade] = []
        for mk in self._month_keys_overlapping(start_utc, end_utc):
            path = shard_path(mk, self.cache_dir)
            if not path.is_file():
                continue
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                cur = conn.execute(
                    "SELECT ts, price, size, role FROM trades "
                    "WHERE symbol=? AND ts>=? AND ts<=? ORDER BY ts",
                    (symbol, start_ts, end_ts),
                )
                for ts, price, size, role in cur:
                    out.append(
                        Trade(
                            symbol=symbol,
                            price=float(price),
                            size=float(size),
                            ts_utc=datetime.fromtimestamp(float(ts), tz=UTC),
                            buyer_role="maker" if int(role) == 0 else "taker",
                            option_type=parsed.option_type,
                            strike=parsed.strike,
                            expiry_date=parsed.expiry_date,
                        )
                    )
            finally:
                conn.close()
        return out

    def nearest_trade(
        self,
        symbol: str,
        when_utc: datetime,
        max_seconds: float,
    ) -> Trade | None:
        start = when_utc - timedelta(seconds=max_seconds)
        end = when_utc + timedelta(seconds=max_seconds)
        trades = self.trades_for_symbol(symbol, start, end)
        if not trades:
            return None
        target = when_utc.timestamp()
        best = min(trades, key=lambda t: abs(t.ts_utc.timestamp() - target))
        if abs(best.ts_utc.timestamp() - target) > max_seconds:
            return None
        return best


def _init_shard(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(
        """
        CREATE TABLE trades (
            symbol TEXT NOT NULL,
            ts REAL NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            role INTEGER NOT NULL,
            expiry TEXT NOT NULL,
            opt_type TEXT NOT NULL,
            strike REAL NOT NULL
        )
        """
    )
    return conn


def _finalize_shard(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_sym_ts ON trades(symbol, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_exp_ks ON trades(expiry, strike, opt_type, ts)"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Main inventory + cache build
# ---------------------------------------------------------------------------


def build_cache_and_inventory(
    *,
    raw_dir: Path = RAW_DIR,
    cache_dir: Path = CACHE_DIR,
    rebuild_cache: bool = True,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    sources = discover_zip_sources(raw_dir)

    # --- Gate 1 accumulators ---
    last_trade_on_expiry_day: dict[date, tuple[str, str]] = {}
    # expiry -> (timestamp_str, symbol) where trade calendar day == expiry

    # --- Inventory ---
    files_processed: list[str] = []
    total_trades = 0
    parse_fail_n = 0
    parse_fail_examples: list[str] = []
    days_counter: Counter[date] = Counter()
    # expiry presence per calendar day (UTC date of timestamp, interpreted as UTC after gate)
    expiries_per_day: dict[date, set[date]] = defaultdict(set)
    expiry_set: set[date] = set()
    # DTE at trade: (expiry - trade_date).days
    dte_counter: Counter[int] = Counter()
    # strikes per expiry
    strikes_by_expiry: dict[date, set[float]] = defaultdict(set)
    role_all = Counter()
    role_atm = Counter()  # filled after spot available, during stream when possible

    spot_times, spot_closes = load_spot_1m()
    spot_start = spot_times[0]
    spot_end = spot_times[-1]

    # Effective spread samples: (moneyness_bucket, spread)
    spread_samples: list[tuple[str, float]] = []
    # minute flush state
    minute_bucket: dict[str, dict[str, list[float]]] = {}
    current_minute: int | None = None

    # ATM coverage helpers: for each expiry, strikes seen + trade times by symbol
    # Too heavy to keep all — for coverage we query sqlite after build.
    # During stream also collect: expiry -> sorted unique strikes
    # and for coverage dates with spot: we'll query shards.

    # Shard writing
    shard_conn: sqlite3.Connection | None = None
    shard_month: str | None = None
    batch: list[tuple[Any, ...]] = []
    BATCH_N = 20_000

    def flush_batch() -> None:
        nonlocal batch
        if shard_conn is not None and batch:
            shard_conn.executemany(
                "INSERT INTO trades(symbol,ts,price,size,role,expiry,opt_type,strike) "
                "VALUES (?,?,?,?,?,?,?,?)",
                batch,
            )
            batch = []

    def flush_minute() -> None:
        nonlocal minute_bucket
        if not minute_bucket or current_minute is None:
            minute_bucket = {}
            return
        # spot at minute
        spot = spot_at(spot_times, spot_closes, current_minute * 60)
        for sym, roles in minute_bucket.items():
            makers = roles.get("maker") or []
            takers = roles.get("taker") or []
            if not makers or not takers:
                continue
            # median maker vs median taker in the minute
            m_px = statistics.median(makers)
            t_px = statistics.median(takers)
            spread = t_px - m_px
            parsed = parse_symbol(sym)
            if parsed is None or spot is None or spot <= 0:
                bucket = "unknown"
            else:
                mny = abs(parsed.strike - spot) / spot
                if mny <= 0.005:
                    bucket = "ATM_|m|<=0.5%"
                elif mny <= 0.02:
                    bucket = "near_|m|0.5-2%"
                elif parsed.option_type == "C":
                    # call ITM if spot > strike
                    if spot > parsed.strike * 1.02:
                        bucket = "call_ITM_m>2%"
                    else:
                        bucket = "call_OTM_m>2%"
                else:
                    if spot < parsed.strike * 0.98:
                        bucket = "put_ITM_m>2%"
                    else:
                        bucket = "put_OTM_m>2%"
            # Only record ATM / near for the "ATM options" spread headline;
            # still keep all buckets
            spread_samples.append((bucket, spread))
        minute_bucket = {}

    hand_audit_raw: str | None = None
    hand_audit_parsed: dict[str, Any] | None = None

    for zp in sources:
        files_processed.append(zp.name)
        mk = _month_key_from_name(zp.name) or "unknown"
        if rebuild_cache:
            if shard_conn is not None and shard_month != mk:
                flush_batch()
                _finalize_shard(shard_conn)
                shard_conn = None
            if shard_conn is None:
                shard_month = mk
                shard_conn = _init_shard(shard_path(mk, cache_dir))

        print(f"Processing {zp.name} ...", flush=True)
        n_file = 0
        for row in iter_zip_rows(zp):
            n_file += 1
            total_trades += 1
            sym = (row.get("product_symbol") or "").strip()
            parsed = parse_symbol(sym)
            if parsed is None:
                parse_fail_n += 1
                if len(parse_fail_examples) < 10:
                    parse_fail_examples.append(sym)
                continue

            ts_raw = (row.get("timestamp") or "").strip()
            try:
                naive = parse_timestamp_naive(ts_raw)
            except ValueError:
                parse_fail_n += 1
                if len(parse_fail_examples) < 10:
                    parse_fail_examples.append(f"BAD_TS:{sym}:{ts_raw}")
                continue

            # Gate 1 evidence uses naive clock; interpret as UTC after confirmation
            trade_day = naive.date()
            if trade_day == parsed.expiry_date:
                prev = last_trade_on_expiry_day.get(parsed.expiry_date)
                if prev is None or ts_raw > prev[0]:
                    last_trade_on_expiry_day[parsed.expiry_date] = (ts_raw, sym)

            price = float(row["price"])
            size = float(row["size"])
            role = (row.get("buyer_role") or "").strip().lower()
            role_all[role] += 1

            days_counter[trade_day] += 1
            expiries_per_day[trade_day].add(parsed.expiry_date)
            expiry_set.add(parsed.expiry_date)
            dte = (parsed.expiry_date - trade_day).days
            dte_counter[dte] += 1
            strikes_by_expiry[parsed.expiry_date].add(parsed.strike)

            # Treat as UTC for cache (Gate 1 will confirm)
            utc_dt = as_utc(naive)
            ts_unix = utc_dt.timestamp()
            minute = int(ts_unix) // 60

            if current_minute is None:
                current_minute = minute
            elif minute != current_minute:
                flush_minute()
                current_minute = minute
            if role in ("maker", "taker"):
                minute_bucket.setdefault(sym, {}).setdefault(role, []).append(price)

            # ATM role split (spot-based moneyness)
            sp_now = spot_at(spot_times, spot_closes, int(ts_unix))
            if (
                sp_now
                and sp_now > 0
                and abs(parsed.strike - sp_now) / sp_now <= 0.005
                and role in ("maker", "taker")
            ):
                role_atm[role] += 1

            if rebuild_cache and shard_conn is not None:
                batch.append(
                    (
                        sym,
                        ts_unix,
                        price,
                        size,
                        0 if role == "maker" else 1,
                        parsed.expiry_date.isoformat(),
                        parsed.option_type,
                        parsed.strike,
                    )
                )
                if len(batch) >= BATCH_N:
                    flush_batch()

            if hand_audit_raw is None and role == "taker":
                hand_audit_raw = (
                    f"{sym},{row['price']},{row['size']},{ts_raw},{row['buyer_role']}"
                )
                hand_audit_parsed = {
                    "symbol": sym,
                    "option_type": parsed.option_type,
                    "strike": parsed.strike,
                    "expiry_date": parsed.expiry_date.isoformat(),
                    "price": price,
                    "size": size,
                    "buyer_role": role,
                    "timestamp_raw": ts_raw,
                    "as_utc": utc_dt.isoformat(),
                    "as_ist": as_ist_from_utc(utc_dt).isoformat(),
                }

        print(f"  rows={n_file:,}", flush=True)

    flush_minute()
    if rebuild_cache and shard_conn is not None:
        flush_batch()
        _finalize_shard(shard_conn)

    runtime_s = time.perf_counter() - t0

    # --- Gate 1 decision ---
    hhmm_counter: Counter[str] = Counter()
    evidence_rows: list[str] = []
    for exp in sorted(last_trade_on_expiry_day.keys()):
        ts_raw, sym = last_trade_on_expiry_day[exp]
        hhmm = ts_raw[11:16]
        hhmm_counter[hhmm] += 1
        if len(evidence_rows) < 12:
            evidence_rows.append(f"  expiry={exp} last_trade={ts_raw} symbol={sym}")

    near_1200 = sum(c for h, c in hhmm_counter.items() if h.startswith("12:"))
    near_1730 = sum(
        c
        for h, c in hhmm_counter.items()
        if h.startswith("17:2") or h.startswith("17:3") or h.startswith("17:4")
    )
    n_ev = sum(hhmm_counter.values())
    if n_ev == 0:
        tz_answer = "AMBIGUOUS"
        tz_note = "No expiry-day last trades found."
    elif near_1200 >= 0.8 * n_ev and near_1200 > near_1730:
        tz_answer = "UTC"
        tz_note = (
            f"{near_1200}/{n_ev} expiry-day last trades cluster near 12:00 "
            f"(= 17:30 IST). Near 17:30 clock: {near_1730}/{n_ev}."
        )
    elif near_1730 >= 0.8 * n_ev and near_1730 > near_1200:
        tz_answer = "IST"
        tz_note = (
            f"{near_1730}/{n_ev} expiry-day last trades cluster near 17:30. "
            f"Near 12:00 clock: {near_1200}/{n_ev}."
        )
    else:
        tz_answer = "AMBIGUOUS"
        tz_note = (
            f"near_12:00={near_1200}/{n_ev} near_17:30={near_1730}/{n_ev} "
            f"top={hhmm_counter.most_common(8)}"
        )

    # --- Calendar gaps ---
    if days_counter:
        d0 = min(days_counter.keys())
        d1 = max(days_counter.keys())
        all_days = set()
        cur = d0
        while cur <= d1:
            all_days.add(cur)
            cur += timedelta(days=1)
        missing_days = sorted(all_days - set(days_counter.keys()))
    else:
        d0 = d1 = None
        missing_days = []

    tpd = sorted(days_counter.values()) if days_counter else []

    # Expiries per day stats
    n_exp_day = [len(v) for v in expiries_per_day.values()]
    # DTE distribution summary
    dte_items = sorted(dte_counter.items())

    # Strikes per expiry + range vs spot (when spot available on expiry day 12:00 UTC)
    strike_counts: list[int] = []
    strike_range_pct: list[float] = []
    for exp, strikes in strikes_by_expiry.items():
        strike_counts.append(len(strikes))
        # spot at 09:00 IST on expiry (= 03:30 UTC) if available
        entry = ist_to_utc(exp, 9, 0)
        sp = spot_at(spot_times, spot_closes, int(entry.timestamp()))
        if sp and sp > 0 and strikes:
            lo, hi = min(strikes), max(strikes)
            strike_range_pct.append(100.0 * (hi - lo) / sp)

    # --- Gate 2: ATM coverage via shards ---
    coverage = _atm_coverage(
        cache_dir=cache_dir,
        spot_times=spot_times,
        spot_closes=spot_closes,
        expiry_dates=sorted(
            e
            for e in expiry_set
            if spot_start <= int(ist_to_utc(e, 9, 0).timestamp()) <= spot_end
        ),
        strikes_by_expiry=strikes_by_expiry,
    )

    # Spread summary
    spread_by_bucket: dict[str, list[float]] = defaultdict(list)
    for b, s in spread_samples:
        spread_by_bucket[b].append(s)

    return {
        "runtime_s": runtime_s,
        "files_processed": files_processed,
        "total_trades": total_trades,
        "parse_fail_n": parse_fail_n,
        "parse_fail_examples": parse_fail_examples,
        "tz_answer": tz_answer,
        "tz_note": tz_note,
        "tz_evidence_rows": evidence_rows,
        "tz_hhmm_top": hhmm_counter.most_common(10),
        "date_from": d0,
        "date_to": d1,
        "n_calendar_days": len(days_counter),
        "missing_days": missing_days,
        "trades_per_day": {
            "min": min(tpd) if tpd else None,
            "median": statistics.median(tpd) if tpd else None,
            "max": max(tpd) if tpd else None,
        },
        "expiries_per_day": {
            "min": min(n_exp_day) if n_exp_day else None,
            "median": statistics.median(n_exp_day) if n_exp_day else None,
            "max": max(n_exp_day) if n_exp_day else None,
        },
        "n_distinct_expiries": len(expiry_set),
        "dte_counter": dte_items,
        "strike_counts": {
            "min": min(strike_counts) if strike_counts else None,
            "median": statistics.median(strike_counts) if strike_counts else None,
            "max": max(strike_counts) if strike_counts else None,
        },
        "strike_range_pct_median": (
            statistics.median(strike_range_pct) if strike_range_pct else None
        ),
        "role_all": dict(role_all),
        "role_atm": dict(role_atm),
        "spread_by_bucket": {
            b: {
                "n": len(vs),
                "median": statistics.median(vs) if vs else None,
                "p25": _pctile(vs, 25) if vs else None,
                "p75": _pctile(vs, 75) if vs else None,
            }
            for b, vs in sorted(spread_by_bucket.items())
        },
        "coverage": coverage,
        "hand_audit_raw": hand_audit_raw,
        "hand_audit_parsed": hand_audit_parsed,
        "spot_range_utc": (
            datetime.fromtimestamp(spot_start, tz=UTC).isoformat(),
            datetime.fromtimestamp(spot_end, tz=UTC).isoformat(),
        ),
        "cache_dir": str(cache_dir),
    }


def _pctile(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        return float("nan")
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _format_symbol(opt: str, strike: float, exp: date) -> str:
    return f"{opt}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def _atm_coverage(
    *,
    cache_dir: Path,
    spot_times: list[int],
    spot_closes: list[float],
    expiry_dates: list[date],
    strikes_by_expiry: dict[date, set[float]],
) -> dict[str, Any]:
    """
    For each expiry with spot coverage and each entry time, check whether
    ATM call AND put trades exist within +/- W minutes.
    """
    store = OptionsTradeStore(cache_dir)
    results: dict[tuple[int, int], dict[str, Any]] = {}
    # (hour, minute) -> stats

    for hour, minute in ENTRY_TIMES_IST:
        key = (hour, minute)
        stats = {
            w: {"both": 0, "total": 0, "gaps_sec": []}
            for w in WINDOWS_MIN
        }
        # also track missing spot / missing strikes
        skipped_no_spot = 0
        skipped_no_strikes = 0

        for exp in expiry_dates:
            when = ist_to_utc(exp, hour, minute)
            sp = spot_at(spot_times, spot_closes, int(when.timestamp()))
            if sp is None:
                skipped_no_spot += 1
                continue
            strikes = strikes_by_expiry.get(exp) or set()
            if not strikes:
                skipped_no_strikes += 1
                continue
            atm = min(strikes, key=lambda k: abs(k - sp))
            call_sym = _format_symbol("C", atm, exp)
            put_sym = _format_symbol("P", atm, exp)

            for w in WINDOWS_MIN:
                stats[w]["total"] += 1
                max_sec = w * 60
                ct = store.nearest_trade(call_sym, when, max_sec)
                pt = store.nearest_trade(put_sym, when, max_sec)
                if ct is not None and pt is not None:
                    stats[w]["both"] += 1
                    gap = max(
                        abs(ct.ts_utc.timestamp() - when.timestamp()),
                        abs(pt.ts_utc.timestamp() - when.timestamp()),
                    )
                    stats[w]["gaps_sec"].append(gap)

        results[key] = {
            "skipped_no_spot": skipped_no_spot,
            "skipped_no_strikes": skipped_no_strikes,
            "windows": {
                w: {
                    "total": stats[w]["total"],
                    "both": stats[w]["both"],
                    "pct": (
                        100.0 * stats[w]["both"] / stats[w]["total"]
                        if stats[w]["total"]
                        else None
                    ),
                    "median_gap_sec": (
                        statistics.median(stats[w]["gaps_sec"])
                        if stats[w]["gaps_sec"]
                        else None
                    ),
                }
                for w in WINDOWS_MIN
            },
        }
    return {
        "n_expiries_tested": len(expiry_dates),
        "by_entry": {
            f"{h:02d}:{m:02d}_IST": results[(h, m)] for h, m in ENTRY_TIMES_IST
        },
    }


def format_report(inv: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=== Options trades inventory ===")
    lines.append("")
    lines.append("=== GATE 1 — TIMESTAMP TIMEZONE ===")
    lines.append(f"ANSWER: timestamps are {inv['tz_answer']}")
    lines.append(inv["tz_note"])
    lines.append("Evidence (expiry-day last trades):")
    lines.extend(inv["tz_evidence_rows"])
    lines.append(f"HH:MM histogram (top): {inv['tz_hhmm_top']}")
    if inv["tz_answer"] == "AMBIGUOUS":
        lines.append("STOP — timezone ambiguous; do not proceed.")
        return "\n".join(lines) + "\n"

    lines.append("")
    lines.append("=== GATE 2 — ATM CALL+PUT COVERAGE NEAR ENTRY ===")
    lines.append(
        f"Spot 1m range: {inv['spot_range_utc'][0]} .. {inv['spot_range_utc'][1]}"
    )
    lines.append(
        f"Expiries tested (overlap with spot): {inv['coverage']['n_expiries_tested']}"
    )
    lines.append(
        "(Jul–early Sep 2025 options exist in zips but have NO spot overlap — excluded.)"
    )
    for entry, block in inv["coverage"]["by_entry"].items():
        lines.append(f"  entry {entry}:")
        lines.append(
            f"    skipped_no_spot={block['skipped_no_spot']}  "
            f"skipped_no_strikes={block['skipped_no_strikes']}"
        )
        for w, st in block["windows"].items():
            lines.append(
                f"    +/-{w}m: both={st['both']}/{st['total']} "
                f"({_fmt(st['pct'])}%)  median_gap_sec={_fmt(st['median_gap_sec'])}"
            )

    lines.append("")
    lines.append("=== 1. FILES / VOLUME / RUNTIME ===")
    lines.append(f"files_processed: {len(inv['files_processed'])}")
    for name in inv["files_processed"]:
        lines.append(f"  - {name}")
    lines.append(f"total_trades_parsed: {inv['total_trades']:,}")
    lines.append(
        f"symbol_parse_failures: {inv['parse_fail_n']:,}  "
        f"examples={inv['parse_fail_examples']}"
    )
    lines.append(f"runtime_s: {_fmt(inv['runtime_s'])}")
    lines.append(f"cache_dir: {inv['cache_dir']}")
    lines.append("Read zips in-stream (zipfile) — not extracted to disk.")

    lines.append("")
    lines.append("=== 2. DATE RANGE ===")
    lines.append(f"from: {inv['date_from']}  to: {inv['date_to']}")
    lines.append(f"distinct_calendar_days: {inv['n_calendar_days']}")
    miss = inv["missing_days"]
    lines.append(f"missing_days: {len(miss)}")
    if miss:
        # show up to 30
        lines.append("  " + ", ".join(str(d) for d in miss[:30]))
        if len(miss) > 30:
            lines.append(f"  ... and {len(miss) - 30} more")

    lines.append("")
    lines.append("=== 3. TRADES PER DAY ===")
    tpd = inv["trades_per_day"]
    lines.append(
        f"min={tpd['min']:,}  median={tpd['median']:,.0f}  max={tpd['max']:,}"
        if tpd["min"] is not None
        else "n/a"
    )

    lines.append("")
    lines.append("=== 4. EXPIRIES / DTE ===")
    epd = inv["expiries_per_day"]
    lines.append(
        f"distinct expiries per calendar day: "
        f"min={epd['min']} median={epd['median']} max={epd['max']}"
    )
    lines.append(f"distinct expiries overall: {inv['n_distinct_expiries']}")
    lines.append("DTE distribution (expiry_date - trade_UTC_date), trade counts:")
    # summarize buckets
    dte = dict(inv["dte_counter"])
    buckets = [
        ("0DTE", sum(c for d, c in dte.items() if d == 0)),
        ("1DTE", sum(c for d, c in dte.items() if d == 1)),
        ("2-7 DTE", sum(c for d, c in dte.items() if 2 <= d <= 7)),
        ("8-31 DTE", sum(c for d, c in dte.items() if 8 <= d <= 31)),
        (">31 DTE", sum(c for d, c in dte.items() if d > 31)),
        ("negative DTE", sum(c for d, c in dte.items() if d < 0)),
    ]
    for label, c in buckets:
        lines.append(f"  {label}: {c:,}")
    has_weekly = buckets[2][1] > 0 or buckets[3][1] > 0
    has_monthly = buckets[4][1] > 0
    lines.append(
        f"Interpretation: daily 0DTE dominates; "
        f"weeklies/short-dated present={has_weekly}; "
        f"longer (>31d) present={has_monthly}."
    )

    lines.append("")
    lines.append("=== 5. STRIKES PER EXPIRY ===")
    sc = inv["strike_counts"]
    lines.append(
        f"strikes/expiry: min={sc['min']} median={sc['median']} max={sc['max']}"
    )
    lines.append(
        f"median strike-range as % of spot (when spot available): "
        f"{_fmt(inv['strike_range_pct_median'])}%"
    )

    lines.append("")
    lines.append("=== 6. BUYER_ROLE ===")
    ra = inv["role_all"]
    tot = sum(ra.values()) or 1
    lines.append("overall:")
    for k, v in sorted(ra.items()):
        lines.append(f"  {k}: {v:,} ({100.0 * v / tot:.1f}%)")
    ratm = inv["role_atm"]
    tot_a = sum(ratm.values()) or 1
    lines.append("ATM (|moneyness|<=0.5%) trade prints in 1m windows with role counts:")
    if ratm:
        for k, v in sorted(ratm.items()):
            lines.append(f"  {k}: {v:,} ({100.0 * v / tot_a:.1f}%)")
    else:
        lines.append("  (no ATM role samples — spot overlap may be limited in stream)")

    lines.append("")
    lines.append("=== 7. EFFECTIVE SPREAD (taker_px - maker_px) ===")
    lines.append(
        "Per symbol, per UTC minute with BOTH maker-buyer and taker-buyer prints; "
        "median(taker) - median(maker)."
    )
    lines.append(
        "Live chain observation was ~$4 at 0.2 delta and ~$12 at 0.8 delta — "
        "compare ATM/near vs ITM buckets below."
    )
    for b, st in inv["spread_by_bucket"].items():
        lines.append(
            f"  {b}: n={st['n']:,}  median={_fmt(st['median'])}  "
            f"p25={_fmt(st['p25'])}  p75={_fmt(st['p75'])}"
        )

    lines.append("")
    lines.append("=== HAND AUDIT — one trade ===")
    lines.append(f"raw CSV line: {inv['hand_audit_raw']}")
    hp = inv["hand_audit_parsed"]
    if hp:
        for k, v in hp.items():
            lines.append(f"  {k}: {v}")

    lines.append("")
    lines.append("Timezone used for cache + coverage: UTC (Gate 1).")
    return "\n".join(lines) + "\n"


def _fmt(v: Any, d: int = 2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Options trades inventory + cache")
    p.add_argument(
        "--no-rebuild-cache",
        action="store_true",
        help="Skip rewriting SQLite shards (still streams zips for inventory)",
    )
    args = p.parse_args(argv)

    inv = build_cache_and_inventory(rebuild_cache=not args.no_rebuild_cache)
    text = format_report(inv)

    # Print Gate 1 first prominently
    print(text)

    if inv["tz_answer"] == "AMBIGUOUS":
        return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"options_trades_inventory_{stamp}.txt"
    out.write_text(text, encoding="utf-8")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
