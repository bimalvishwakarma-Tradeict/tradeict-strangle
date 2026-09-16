#!/usr/bin/env python3
"""
Perfect-signal upper bound for directional option buying.

Not a strategy. Ceiling: if direction is always correct, how much is left
after full mark-slip + fees? If this is still negative, no indicator works.

Data: backtest/cache/option_marks/marks_YYYY-MM.sqlite only.
No print(). Output: console + backtest/results/perfect_signal_bound.txt
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402
import s001_income_engine as eng  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

MARKS_DIR = _BACKTEST / "cache" / "option_marks"
OUT_PATH = _BACKTEST / "results" / "perfect_signal_bound.txt"

ENTRY_HOUR_IST = 11
ENTRY_MINUTE_IST = 0
SETTLE_HOUR_UTC = 12  # 17:30 IST
EOD_SIGNAL_HOUR_IST = 17
EOD_SIGNAL_MINUTE_IST = 30

DELTAS = (0.15, 0.20, 0.30, 0.50, 0.80)
DTES = (0, 1, 2)
EXIT_RULES = ("settle", "h4", "h8", "best")

SLIP = 0.0165  # +1.65% buy / -1.65% sell
CONTRACT_VALUE = eng.CONTRACT_VALUE
# Equal-risk convention: each trade targets this USD premium debit
# (max loss for a long option ≈ premium paid). qty floored at 1 lot.
TARGET_RISK_USD = 10.0
MIN_ENTRY_MARK = 5.0  # skip illiquid/micro premiums (qty would explode)
MAX_QTY_LOTS = 2000  # hard cap even if risk target wants more
# Reject clear mark-data garbage (option mark >> underlying / entry)
MAX_EXIT_OVER_ENTRY = 50.0
MAX_EXIT_OVER_SPOT = 1.5

# Strike selection only (P&L still from marks). Fixed IV for Black-76 |delta|.
DELTA_SELECT_IV = 0.55
MARK_TOL_SEC = 90
CHAIN_TS_TOL_SEC = 120

S003_HITRATE_REF = 0.35

logger = logging.getLogger("perfect_signal_bound")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def shard_path_for_date(d: date) -> Path:
    return MARKS_DIR / f"marks_{month_key(d)}.sqlite"


def ist_dt(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def settle_unix(expiry: date) -> int:
    return int(datetime(expiry.year, expiry.month, expiry.day, SETTLE_HOUR_UTC, tzinfo=UTC).timestamp())


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76_abs_delta(
    forward: float,
    strike: float,
    t_years: float,
    iv: float,
    is_call: bool,
) -> float:
    """Absolute Black-76 delta for strike selection (not for P&L)."""
    if forward <= 0 or strike <= 0:
        return 0.0
    if t_years <= 1e-12 or iv <= 1e-12:
        if is_call:
            return 1.0 if forward > strike else (0.5 if forward == strike else 0.0)
        return 1.0 if forward < strike else (0.5 if forward == strike else 0.0)
    vol_sqrt_t = iv * math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * iv * iv * t_years) / vol_sqrt_t
    call_delta = _norm_cdf(d1)
    return float(call_delta if is_call else (1.0 - call_delta))


def option_fee(premium: float, index: float, qty_lots: int) -> float:
    return eng.option_fee(premium, index, qty_lots)


def buy_fill(mark: float) -> float:
    return mark * (1.0 + SLIP)


def sell_fill(mark: float) -> float:
    return mark * (1.0 - SLIP)


def qty_for_equal_risk(entry_fill: float) -> int:
    """Lots so premium debit ≈ TARGET_RISK_USD (equal risk per trade)."""
    cost_per_lot = entry_fill * CONTRACT_VALUE
    if cost_per_lot <= 1e-12:
        return 1
    raw = int(round(TARGET_RISK_USD / cost_per_lot))
    return max(1, min(MAX_QTY_LOTS, raw))


@dataclass
class SkipCounts:
    no_shard: int = 0
    no_spot_entry: int = 0
    no_spot_eod: int = 0
    flat_day: int = 0
    no_chain: int = 0
    no_delta_match: int = 0
    no_entry_mark: int = 0
    no_exit_mark: int = 0
    bad_premium: int = 0
    micro_premium: int = 0
    outlier_exit: int = 0
    spot_src_csv: int = 0
    spot_src_pcp: int = 0


@dataclass
class TradePnL:
    pnl: float
    premium_paid: float
    win: bool


@dataclass
class VariantBucket:
    key: tuple[float, int, str]
    pnls: list[float] = field(default_factory=list)
    premiums: list[float] = field(default_factory=list)


class MarksStore:
    """Lazy read-only connections keyed by YYYY-MM."""

    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self.available_months: list[str] = []
        for p in sorted(MARKS_DIR.glob("marks_*.sqlite")):
            ym = p.stem.replace("marks_", "")
            self.available_months.append(ym)

    def conn(self, d: date) -> sqlite3.Connection | None:
        ym = month_key(d)
        path = shard_path_for_date(d)
        if not path.is_file():
            return None
        if ym not in self._conns:
            self._conns[ym] = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return self._conns[ym]

    def conn_for_ts(self, ts: int) -> sqlite3.Connection | None:
        d = datetime.fromtimestamp(ts, tz=UTC).date()
        return self.conn(d)

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()


def load_mark_close(
    store: MarksStore, symbol: str, ts: int, tol_sec: int = MARK_TOL_SEC
) -> float | None:
    minute = (ts // 60) * 60
    conn = store.conn_for_ts(minute)
    if conn is None:
        return None
    row = conn.execute(
        "SELECT close FROM marks WHERE symbol=? AND ts=?",
        (symbol, minute),
    ).fetchone()
    if row is not None and row[0] is not None and float(row[0]) > 0:
        return float(row[0])
    row = conn.execute(
        """
        SELECT close FROM marks
        WHERE symbol=? AND ts BETWEEN ? AND ?
        ORDER BY ABS(ts - ?) LIMIT 1
        """,
        (symbol, minute - tol_sec, minute + tol_sec, minute),
    ).fetchone()
    if row is None or row[0] is None or float(row[0]) <= 0:
        return None
    return float(row[0])


def load_mark_high_max(
    store: MarksStore, symbol: str, ts0: int, ts1: int
) -> float | None:
    """Best long exit proxy: max(high) across shards covering [ts0, ts1]."""
    if ts1 < ts0:
        return None
    d0 = datetime.fromtimestamp(ts0, tz=UTC).date()
    d1 = datetime.fromtimestamp(ts1, tz=UTC).date()
    best: float | None = None
    day = d0
    while day <= d1:
        conn = store.conn(day)
        if conn is not None:
            row = conn.execute(
                """
                SELECT MAX(high) FROM marks
                WHERE symbol=? AND ts BETWEEN ? AND ?
                  AND high IS NOT NULL AND high > 0
                """,
                (symbol, ts0, ts1),
            ).fetchone()
            if row is not None and row[0] is not None and float(row[0]) > 0:
                v = float(row[0])
                best = v if best is None else max(best, v)
        day += timedelta(days=1)
    return best


def resolve_chain_ts(
    conn: sqlite3.Connection, expiry: date, entry_ts: int
) -> int | None:
    """Nearest marks minute for this expiry around entry."""
    exp = expiry.isoformat()
    minute = (entry_ts // 60) * 60
    row = conn.execute(
        """
        SELECT ts FROM marks
        WHERE expiry=? AND ts BETWEEN ? AND ?
        ORDER BY ABS(ts - ?) LIMIT 1
        """,
        (exp, minute - CHAIN_TS_TOL_SEC, minute + CHAIN_TS_TOL_SEC, minute),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def load_chain(
    conn: sqlite3.Connection, expiry: date, chain_ts: int, opt_type: str
) -> list[tuple[str, float, float]]:
    """Return [(symbol, strike, close), ...] at chain_ts."""
    rows = conn.execute(
        """
        SELECT symbol, strike, close FROM marks
        WHERE expiry=? AND ts=? AND opt_type=?
          AND close IS NOT NULL AND close > 0
        """,
        (expiry.isoformat(), chain_ts, opt_type),
    ).fetchall()
    out: list[tuple[str, float, float]] = []
    for sym, strike, close in rows:
        out.append((str(sym), float(strike), float(close)))
    return out


_FORWARD_CACHE: dict[tuple[str, int], float | None] = {}


def forward_from_marks(
    store: MarksStore, day: date, ts: int
) -> float | None:
    """
    BTC forward proxy from mark put-call parity only:
      F ≈ median_K (K + C - P) on the nearest available expiry at ts.
    Used when 1m spot CSV has no coverage (marks extend earlier than spot).
    """
    minute = (ts // 60) * 60
    cache_key = (day.isoformat(), minute)
    if cache_key in _FORWARD_CACHE:
        return _FORWARD_CACHE[cache_key]

    conn = store.conn_for_ts(minute)
    if conn is None:
        conn = store.conn(day)
    if conn is None:
        _FORWARD_CACHE[cache_key] = None
        return None

    chain_ts: int | None = None
    expiry_used: date | None = None
    for dte_try in (0, 1, 2, 3, 4, 5, 6, 7):
        exp = day + timedelta(days=dte_try)
        # Fast exact-minute probe (uses expiry index + pk path)
        row = conn.execute(
            """
            SELECT ts FROM marks
            WHERE expiry=? AND ts=? AND close > 0
            LIMIT 1
            """,
            (exp.isoformat(), minute),
        ).fetchone()
        if row is not None:
            chain_ts = int(row[0])
            expiry_used = exp
            break
        cts = resolve_chain_ts(conn, exp, minute)
        if cts is None:
            continue
        row2 = conn.execute(
            """
            SELECT 1 FROM marks
            WHERE expiry=? AND ts=? AND close > 0
            LIMIT 1
            """,
            (exp.isoformat(), cts),
        ).fetchone()
        if row2 is not None:
            chain_ts = cts
            expiry_used = exp
            break

    if chain_ts is None or expiry_used is None:
        _FORWARD_CACHE[cache_key] = None
        return None

    rows = conn.execute(
        """
        SELECT opt_type, strike, close FROM marks
        WHERE expiry=? AND ts=? AND close > 0
          AND opt_type IN ('call', 'put')
        """,
        (expiry_used.isoformat(), chain_ts),
    ).fetchall()
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    for opt_type, strike, close in rows:
        k = float(strike)
        px = float(close)
        if opt_type == "call":
            calls[k] = px
        elif opt_type == "put":
            puts[k] = px
    fs: list[float] = []
    for k, cpx in calls.items():
        ppx = puts.get(k)
        if ppx is None:
            continue
        fs.append(k + cpx - ppx)
    if not fs:
        _FORWARD_CACHE[cache_key] = None
        return None
    fs.sort()
    mid = len(fs) // 2
    if len(fs) % 2 == 1:
        val = float(fs[mid])
    else:
        val = float(0.5 * (fs[mid - 1] + fs[mid]))
    _FORWARD_CACHE[cache_key] = val
    return val


def spot_or_forward(
    times: list[int],
    closes: list[float],
    store: MarksStore,
    day: date,
    ts: int,
) -> tuple[float | None, str]:
    """Prefer CSV spot; else mark put-call-parity forward."""
    s = ot.spot_at(times, closes, ts)
    if s is not None and s > 0:
        return float(s), "spot_csv"
    f = forward_from_marks(store, day, ts)
    if f is not None and f > 0:
        return float(f), "mark_pcp"
    return None, "none"


def pick_by_delta(
    chain: list[tuple[str, float, float]],
    spot: float,
    t_years: float,
    target_abs_delta: float,
    is_call: bool,
) -> tuple[str, float, float] | None:
    if not chain:
        return None
    best: tuple[str, float, float] | None = None
    best_err = float("inf")
    for sym, strike, close in chain:
        d = black76_abs_delta(spot, strike, t_years, DELTA_SELECT_IV, is_call)
        err = abs(d - target_abs_delta)
        better = err < best_err
        if (
            not better
            and abs(err - best_err) < 1e-12
            and best is not None
        ):
            better = (abs(strike - spot), strike) < (abs(best[1] - spot), best[1])
        if better:
            best_err = err
            best = (sym, strike, close)
    return best


def exit_ts_for_rule(rule: str, entry_ts: int, expiry: date) -> int:
    settle = settle_unix(expiry)
    if rule == "settle":
        return settle
    if rule == "h4":
        return min(entry_ts + 4 * 3600, settle)
    if rule == "h8":
        return min(entry_ts + 8 * 3600, settle)
    # best: window end = settle (same-day ceiling for long mark high)
    return settle


def calendar_days_covered(store: MarksStore) -> list[date]:
    days: list[date] = []
    for ym in store.available_months:
        y, m = int(ym[:4]), int(ym[5:7])
        # first/last day of month; clip via shard min/max ts
        conn = store.conn(date(y, m, 1))
        if conn is None:
            continue
        row = conn.execute("SELECT MIN(ts), MAX(ts) FROM marks").fetchone()
        if row is None or row[0] is None:
            continue
        d0 = datetime.fromtimestamp(int(row[0]), tz=UTC).astimezone(IST).date()
        d1 = datetime.fromtimestamp(int(row[1]), tz=UTC).astimezone(IST).date()
        # only days whose month matches this shard (avoid double-count edges)
        cur = d0
        while cur <= d1:
            if month_key(cur) == ym:
                days.append(cur)
            cur += timedelta(days=1)
    days = sorted(set(days))
    return days


def summarize(pnls: list[float], premiums: list[float]) -> dict[str, Any]:
    n = len(pnls)
    empty = {
        "n": 0,
        "mean": float("nan"),
        "median": float("nan"),
        "std": float("nan"),
        "win_rate": float("nan"),
        "avg_win": float("nan"),
        "avg_loss": float("nan"),
        "biggest_win": float("nan"),
        "total": float("nan"),
        "mean_pct_premium": float("nan"),
        "breakeven_hitrate": float("nan"),
    }
    if n == 0:
        return empty
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = float(statistics.fmean(wins)) if wins else 0.0
    # avg_loss as positive magnitude for breakeven formula
    avg_loss_mag = float(statistics.fmean([-p for p in losses])) if losses else 0.0
    mean_pnl = float(statistics.fmean(pnls))
    pcts = [
        (p / prem * 100.0) if prem > 1e-12 else float("nan")
        for p, prem in zip(pnls, premiums)
    ]
    pcts_ok = [x for x in pcts if not math.isnan(x)]
    denom = avg_win + avg_loss_mag
    be = (avg_loss_mag / denom) if denom > 1e-12 else float("nan")
    return {
        "n": n,
        "mean": mean_pnl,
        "median": float(statistics.median(pnls)),
        "std": float(statistics.pstdev(pnls)) if n > 1 else 0.0,
        "win_rate": len(wins) / n,
        "avg_win": avg_win,
        "avg_loss": -avg_loss_mag if losses else 0.0,  # signed for table
        "avg_loss_mag": avg_loss_mag,
        "biggest_win": float(max(pnls)),
        "total": float(sum(pnls)),
        "mean_pct_premium": float(statistics.fmean(pcts_ok)) if pcts_ok else float("nan"),
        "breakeven_hitrate": be,
    }


def run() -> list[str]:
    lines: list[str] = []
    _FORWARD_CACHE.clear()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = MarksStore()
    skips = SkipCounts()
    buckets: dict[tuple[float, int, str], VariantBucket] = {}
    for dlt in DELTAS:
        for dte in DTES:
            for rule in EXIT_RULES:
                buckets[(dlt, dte, rule)] = VariantBucket(key=(dlt, dte, rule))

    emit(lines, "===== PERFECT-SIGNAL UPPER BOUND =====")
    emit(lines, f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    emit(lines, f"marks_dir={MARKS_DIR}")
    emit(
        lines,
        f"months_available={len(store.available_months)} "
        f"{store.available_months}",
    )
    emit(lines, "entry=11:00 IST  signal=EOD underlying 17:30 IST vs entry")
    emit(
        lines,
        "underlying: BTCUSD 1m CSV when available; else mark put-call-parity "
        "forward F=median(K+C-P) (mark-only, no surface)",
    )
    emit(lines, f"deltas={list(DELTAS)}  dtes={list(DTES)}  exits={list(EXIT_RULES)}")
    emit(lines, f"slip=±{SLIP * 100:.2f}%  fee=min(index*qtyBTC*1e-4, prem*qtyBTC*0.035)*1.18")
    emit(
        lines,
        f"equal_risk_convention: TARGET_RISK_USD={TARGET_RISK_USD:.2f} "
        f"(qty_lots = max(1, min({MAX_QTY_LOTS}, round(risk / (entry_fill * {CONTRACT_VALUE}))))); "
        f"skip if entry_mark < {MIN_ENTRY_MARK}",
    )
    emit(
        lines,
        f"delta_select_iv={DELTA_SELECT_IV:.2f} (Black-76 |delta| for strike pick only)",
    )
    emit(lines, f"S003_reference_hitrate≈{S003_HITRATE_REF * 100:.0f}%")
    emit(lines)

    if not store.available_months:
        emit(lines, "ERROR: no marks_*.sqlite found — abort")
        return lines

    times, closes = ot.load_spot_1m()
    days = calendar_days_covered(store)
    emit(lines, f"calendar_days_in_shards={len(days)}")
    if days:
        emit(lines, f"day_range={days[0].isoformat()} .. {days[-1].isoformat()}")
    emit(lines)

    days_tried = 0
    days_with_signal = 0

    for day in days:
        days_tried += 1
        entry_dt = ist_dt(day, ENTRY_HOUR_IST, ENTRY_MINUTE_IST)
        entry_ts = to_unix(entry_dt)
        eod_ts = to_unix(ist_dt(day, EOD_SIGNAL_HOUR_IST, EOD_SIGNAL_MINUTE_IST))

        conn_entry = store.conn(day)
        if conn_entry is None:
            skips.no_shard += 1
            continue

        spot_entry, src_e = spot_or_forward(times, closes, store, day, entry_ts)
        if spot_entry is None or spot_entry <= 0:
            skips.no_spot_entry += 1
            continue
        spot_eod, src_x = spot_or_forward(times, closes, store, day, eod_ts)
        if spot_eod is None or spot_eod <= 0:
            skips.no_spot_eod += 1
            continue
        if src_e == "spot_csv" or src_x == "spot_csv":
            skips.spot_src_csv += 1
        if src_e == "mark_pcp" or src_x == "mark_pcp":
            skips.spot_src_pcp += 1

        if spot_eod > spot_entry:
            is_call = True
            opt_type = "call"
        elif spot_eod < spot_entry:
            is_call = False
            opt_type = "put"
        else:
            skips.flat_day += 1
            continue

        days_with_signal += 1

        for dte in DTES:
            expiry = day + timedelta(days=dte)
            # chain lives in entry-day shard (candle month)
            chain_ts = resolve_chain_ts(conn_entry, expiry, entry_ts)
            if chain_ts is None:
                skips.no_chain += 1
                continue
            chain = load_chain(conn_entry, expiry, chain_ts, opt_type)
            if not chain:
                skips.no_chain += 1
                continue

            settle_ts = settle_unix(expiry)
            t_years = max(0.0, (settle_ts - entry_ts) / (365.25 * 24 * 3600))

            for dlt in DELTAS:
                picked = pick_by_delta(chain, float(spot_entry), t_years, dlt, is_call)
                if picked is None:
                    skips.no_delta_match += 1
                    continue
                symbol, strike, entry_close = picked
                if entry_close <= 0:
                    skips.bad_premium += 1
                    continue

                entry_mark = load_mark_close(store, symbol, entry_ts)
                if entry_mark is None:
                    entry_mark = entry_close
                if entry_mark is None or entry_mark <= 0:
                    skips.no_entry_mark += 1
                    continue
                if entry_mark < MIN_ENTRY_MARK:
                    skips.micro_premium += 1
                    continue

                entry_px = buy_fill(entry_mark)
                qty = qty_for_equal_risk(entry_px)
                qty_btc = qty * CONTRACT_VALUE
                premium_paid = entry_px * qty_btc
                fee_in = option_fee(entry_px, float(spot_entry), qty)

                for rule in EXIT_RULES:
                    xts = exit_ts_for_rule(rule, entry_ts, expiry)
                    if rule == "best":
                        exit_mark = load_mark_high_max(store, symbol, entry_ts, xts)
                    else:
                        exit_mark = load_mark_close(store, symbol, xts)
                        if exit_mark is None and rule == "settle":
                            for back in (60, 120, 300, 600):
                                exit_mark = load_mark_close(store, symbol, xts - back)
                                if exit_mark is not None:
                                    break

                    if exit_mark is None or exit_mark <= 0:
                        skips.no_exit_mark += 1
                        continue

                    # Sanity: reject garbage marks (wicks / bad candles)
                    if exit_mark > entry_mark * MAX_EXIT_OVER_ENTRY:
                        skips.outlier_exit += 1
                        continue
                    if is_call and exit_mark > float(spot_entry) * MAX_EXIT_OVER_SPOT + max(
                        0.0, float(spot_entry) - strike
                    ):
                        # call mark roughly <= intrinsic + loose premium room
                        if exit_mark > float(spot_entry) + entry_mark * 5:
                            skips.outlier_exit += 1
                            continue
                    if (not is_call) and exit_mark > strike + entry_mark * 5:
                        skips.outlier_exit += 1
                        continue

                    exit_px = sell_fill(exit_mark)
                    spot_exit = float(spot_entry)
                    fee_out = option_fee(exit_px, float(spot_exit), qty)
                    gross = (exit_px - entry_px) * qty_btc
                    pnl = gross - fee_in - fee_out

                    bucket = buckets[(dlt, dte, rule)]
                    bucket.pnls.append(pnl)
                    bucket.premiums.append(premium_paid)

        if days_tried % 20 == 0:
            logger.info("progress day=%s tried=%d signal_days=%d", day, days_tried, days_with_signal)

    emit(lines, "----- SKIP COUNTS -----")
    emit(lines, f"days_tried={days_tried}  days_with_directional_signal={days_with_signal}")
    emit(lines, f"skip_no_shard={skips.no_shard}")
    emit(lines, f"skip_no_spot_entry={skips.no_spot_entry}")
    emit(lines, f"skip_no_spot_eod={skips.no_spot_eod}")
    emit(lines, f"skip_flat_day={skips.flat_day}")
    emit(lines, f"skip_no_chain={skips.no_chain}")
    emit(lines, f"skip_no_delta_match={skips.no_delta_match}")
    emit(lines, f"skip_no_entry_mark={skips.no_entry_mark}")
    emit(lines, f"skip_no_exit_mark={skips.no_exit_mark}")
    emit(lines, f"skip_bad_premium={skips.bad_premium}")
    emit(lines, f"skip_micro_premium={skips.micro_premium}")
    emit(lines, f"skip_outlier_exit={skips.outlier_exit}")
    emit(lines, f"days_using_spot_csv={skips.spot_src_csv}")
    emit(lines, f"days_using_mark_pcp_forward={skips.spot_src_pcp}")
    emit(lines)

    emit(lines, "----- PER-VARIANT DETAIL -----")
    header = (
        f"{'delta':>5} {'dte':>3} {'exit':>6} {'n':>5} {'mean':>10} {'median':>10} "
        f"{'std':>10} {'win%':>7} {'avg_win':>10} {'avg_loss':>10} "
        f"{'biggest':>10} {'total':>12} {'mean%prem':>9} {'BE_hit%':>8} {'vsS003':>8}"
    )
    emit(lines, header)
    emit(lines, "-" * len(header))

    rows_out: list[tuple[float, int, str, dict[str, Any]]] = []
    for dlt in DELTAS:
        for dte in DTES:
            for rule in EXIT_RULES:
                s = summarize(buckets[(dlt, dte, rule)].pnls, buckets[(dlt, dte, rule)].premiums)
                rows_out.append((dlt, dte, rule, s))
                be = s["breakeven_hitrate"]
                if math.isnan(be):
                    vs = "n/a"
                elif be > S003_HITRATE_REF:
                    vs = "above_S003_need"
                else:
                    vs = "below_S003_ok"
                emit(
                    lines,
                    f"{dlt:5.2f} {dte:3d} {rule:>6} {s['n']:5d} "
                    f"{s['mean']:10.4f} {s['median']:10.4f} {s['std']:10.4f} "
                    f"{100.0 * s['win_rate'] if s['n'] else float('nan'):6.1f}% "
                    f"{s['avg_win']:10.4f} {s['avg_loss']:10.4f} "
                    f"{s['biggest_win']:10.4f} {s['total']:12.2f} "
                    f"{s['mean_pct_premium']:8.2f}% "
                    f"{100.0 * be if not math.isnan(be) else float('nan'):7.1f}% "
                    f"{vs:>14}",
                )

    emit(lines)
    emit(lines, "----- FINAL TABLE -----")
    emit(
        lines,
        f"{'delta':>5} {'dte':>3} {'exit':>6} {'n':>5} {'mean':>10} "
        f"{'win%':>7} {'avg_win':>10} {'avg_loss':>10} {'total':>12} "
        f"{'BE_hit%':>8}",
    )
    emit(lines, "-" * 90)
    for dlt, dte, rule, s in rows_out:
        be = s["breakeven_hitrate"]
        emit(
            lines,
            f"{dlt:5.2f} {dte:3d} {rule:>6} {s['n']:5d} {s['mean']:10.4f} "
            f"{100.0 * s['win_rate'] if s['n'] else float('nan'):6.1f}% "
            f"{s['avg_win']:10.4f} {s['avg_loss']:10.4f} {s['total']:12.2f} "
            f"{100.0 * be if not math.isnan(be) else float('nan'):7.1f}%",
        )

    emit(lines)
    emit(lines, "NOTE: breakeven_hitrate = avg_loss_mag / (avg_win + avg_loss_mag)")
    emit(
        lines,
        "  = minimum correct-direction rate needed for zero expectancy "
        f"(compare to S003 ~{S003_HITRATE_REF * 100:.0f}%).",
    )
    emit(
        lines,
        "  If BE_hit% > 35%, a S003-class signal is not enough for that variant.",
    )
    emit(
        lines,
        "  'best' exit is doubly perfect (direction + best mark high) — "
        "strict upper ceiling only.",
    )

    store.close()
    return lines


def main() -> None:
    lines = run()
    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()
    logger.info("wrote %s", OUT_PATH)


if __name__ == "__main__":
    main()
