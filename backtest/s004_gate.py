#!/usr/bin/env python3
"""
S004 signal-agnostic cost gate.

No signal logic. Measures option+BTC exit economics and breakeven hit-rate
needed given observed RIGHT/WRONG/NEITHER labels from the BTC path alone.

Data: option marks (ro) + BTC 1m OHLC. Intersection days only.
No print(). Output: console + backtest/results/s004_gate.txt
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
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

import s001_income_engine as eng  # noqa: E402
from slippage_model import load_slip_table, slip_pct  # noqa: E402

# ---------------------------------------------------------------------------
# Spec constants (CAPS)
# ---------------------------------------------------------------------------
PREMIUM_MIN = 800.0
PREMIUM_MAX = 900.0
DELTA_MIN = 0.6
DELTA_MAX = 0.8
BTC_TARGET = 150.0
BTC_STOP = 50.0
BRACKET_SL = 100.0
BRACKET_TP = 100.0
SLIP = 0.0165  # flat165 baseline (fraction)
SLIP_MODEL_FLAT = "flat165"
SLIP_MODEL_BUCKETED = "bucketed"
QTY_LOTS = 1
CONTRACT_VALUE = eng.CONTRACT_VALUE
SETTLE_HOUR_UTC = 12
CUTOFF_HOUR_IST = 17
CUTOFF_MINUTE_IST = 25
GRID_START_HOUR_IST = 0
GRID_START_MINUTE_IST = 0
GRID_END_HOUR_IST = 16
GRID_END_MINUTE_IST = 30
SPOT_FRESH_SEC = 60
MARK_TOL_SEC = 60
IV_LO = 0.01
IV_HI = 5.0
SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260916

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

MARKS_DIR = _BACKTEST / "cache" / "option_marks"
DATA_1M_DIR = _BACKTEST / "data_1m"
OUT_PATH = _BACKTEST / "results" / "s004_gate.txt"

logger = logging.getLogger("s004_gate")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def ist_dt(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def settle_unix(expiry: date) -> int:
    return int(
        datetime(
            expiry.year, expiry.month, expiry.day, SETTLE_HOUR_UTC, tzinfo=UTC
        ).timestamp()
    )


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76_price(f: float, k: float, t: float, sigma: float, is_call: bool) -> float:
    if f <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return max(f - k, 0.0) if is_call else max(k - f, 0.0)
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    if is_call:
        return f * norm_cdf(d1) - k * norm_cdf(d2)
    return k * norm_cdf(-d2) - f * norm_cdf(-d1)


def black76_abs_delta(f: float, k: float, t: float, sigma: float, is_call: bool) -> float:
    if f <= 0 or k <= 0:
        return 0.0
    if t <= 1e-12 or sigma <= 1e-12:
        if is_call:
            return 1.0 if f > k else (0.5 if f == k else 0.0)
        return 1.0 if f < k else (0.5 if f == k else 0.0)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    call_d = norm_cdf(d1)
    return float(call_d if is_call else (1.0 - call_d))


def implied_vol_bisection(
    premium: float, f: float, k: float, t: float, is_call: bool
) -> float | None:
    """Black-76 IV via bisection on mark premium. No fixed IV."""
    if premium <= 0 or f <= 0 or k <= 0 or t <= 1e-12:
        return None
    intrinsic = max(f - k, 0.0) if is_call else max(k - f, 0.0)
    if premium < intrinsic - 1e-6:
        return None
    lo, hi = IV_LO, IV_HI
    flo = black76_price(f, k, t, lo, is_call) - premium
    fhi = black76_price(f, k, t, hi, is_call) - premium
    if flo * fhi > 0:
        # expand hi once for very rich premiums
        for _ in range(8):
            hi *= 1.5
            if hi > 20.0:
                break
            fhi = black76_price(f, k, t, hi, is_call) - premium
            if flo * fhi <= 0:
                break
        else:
            return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        fm = black76_price(f, k, t, mid, is_call) - premium
        if abs(fm) < 1e-4 or (hi - lo) < 1e-10:
            return mid
        if flo * fm <= 0:
            hi = mid
            fhi = fm
        else:
            lo = mid
            flo = fm
    return 0.5 * (lo + hi)


@dataclass
class SpotBar:
    ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class MarkBar:
    open: float
    high: float
    low: float
    close: float


@dataclass
class TradeResult:
    day: date
    side: str  # call | put
    dte: int
    rule: str
    entry_hour_ist: int
    label: str  # RIGHT | WRONG | NEITHER
    exit_reason: str  # a|b|c|d|e
    pnl: float
    entry_slip_pct: float = 0.0  # percent units (1.65 = 1.65%)
    exit_slip_pct: float = 0.0


@dataclass
class SkipCounts:
    no_spot_bar: int = 0
    stale_spot: int = 0
    no_strike: int = 0
    no_entry_mark: int = 0
    no_exit_path: int = 0


@dataclass
class RuleCounts:
    rule1_0dte: int = 0
    rule1_1dte: int = 0
    rule3_1dte_nearest: int = 0


class MarksStore:
    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self.available_months: list[str] = sorted(
            p.stem.replace("marks_", "")
            for p in MARKS_DIR.glob("marks_*.sqlite")
        )

    def conn(self, d: date) -> sqlite3.Connection | None:
        ym = month_key(d)
        path = MARKS_DIR / f"marks_{ym}.sqlite"
        if not path.is_file():
            return None
        if ym not in self._conns:
            self._conns[ym] = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return self._conns[ym]

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()


def load_spot_ohlc(path: Path) -> dict[int, SpotBar]:
    out: dict[int, SpotBar] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            out[ts] = SpotBar(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
    return out


def spot_bar_fresh(
    bars: dict[int, SpotBar], entry_ts: int
) -> tuple[SpotBar | None, str]:
    """
    Exact entry-minute bar preferred. Reject if bar.ts not within SPOT_FRESH_SEC
    of the entry minute (guards ot.spot_at-style stale last-bar behaviour).
    Returns (bar, skip_reason) where skip_reason is '' | 'no_spot_bar' | 'stale_spot'.
    """
    minute = (entry_ts // 60) * 60
    bar = bars.get(minute)
    if bar is not None and abs(bar.ts - minute) <= SPOT_FRESH_SEC:
        return bar, ""
    # nearest bar (any distance) to classify stale vs missing
    nearest_ts: int | None = None
    nearest_abs = None
    # probe ±2h then fall back to full scan only if needed — keep cheap
    for d in range(-7200, 7201, 60):
        ts = minute + d
        if ts in bars:
            ad = abs(ts - minute)
            if nearest_abs is None or ad < nearest_abs:
                nearest_abs = ad
                nearest_ts = ts
    if nearest_ts is None:
        return None, "no_spot_bar"
    if nearest_abs is not None and nearest_abs <= SPOT_FRESH_SEC:
        b = bars[nearest_ts]
        if abs(b.ts - minute) <= SPOT_FRESH_SEC:
            return b, ""
    # CSV ended or gap: last known bar too old relative to entry minute
    if nearest_ts < minute:
        return None, "stale_spot"
    return None, "no_spot_bar"


def marks_days_in_month(store: MarksStore, ym: str) -> set[date]:
    y, m = int(ym[:4]), int(ym[5:7])
    conn = store.conn(date(y, m, 1))
    if conn is None:
        return set()
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM marks").fetchone()
    if row is None or row[0] is None:
        return set()
    d0 = datetime.fromtimestamp(int(row[0]), tz=UTC).astimezone(IST).date()
    d1 = datetime.fromtimestamp(int(row[1]), tz=UTC).astimezone(IST).date()
    out: set[date] = set()
    cur = d0
    while cur <= d1:
        if month_key(cur) == ym:
            out.add(cur)
        cur += timedelta(days=1)
    return out


def spot_days(bars: dict[int, SpotBar]) -> set[date]:
    out: set[date] = set()
    for ts in bars:
        out.add(datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date())
    return out


def entry_grid_slots(day: date, grid_min: int) -> list[datetime]:
    slots: list[datetime] = []
    start = ist_dt(day, GRID_START_HOUR_IST, GRID_START_MINUTE_IST)
    end = ist_dt(day, GRID_END_HOUR_IST, GRID_END_MINUTE_IST)
    cur = start
    step = timedelta(minutes=grid_min)
    while cur <= end:
        slots.append(cur)
        cur += step
    return slots


def resolve_mark_ts(
    conn: sqlite3.Connection, expiry: date, ts: int
) -> int | None:
    minute = (ts // 60) * 60
    exp = expiry.isoformat()
    row = conn.execute(
        "SELECT ts FROM marks WHERE expiry=? AND ts=? LIMIT 1",
        (exp, minute),
    ).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.execute(
        """
        SELECT ts FROM marks
        WHERE expiry=? AND ts BETWEEN ? AND ?
        ORDER BY ABS(ts - ?) LIMIT 1
        """,
        (exp, minute - MARK_TOL_SEC, minute + MARK_TOL_SEC, minute),
    ).fetchone()
    return int(row[0]) if row is not None else None


def load_chain_at(
    conn: sqlite3.Connection, expiry: date, chain_ts: int, opt_type: str
) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT symbol, strike, close FROM marks
        WHERE expiry=? AND ts=? AND opt_type=?
          AND close IS NOT NULL AND close > 0
        """,
        (expiry.isoformat(), chain_ts, opt_type),
    ).fetchall()
    return [(str(s), float(k), float(c)) for s, k, c in rows]


def load_symbol_marks(
    store: MarksStore,
    symbol: str,
    ts0: int,
    ts1: int,
    cache: dict[tuple[str, int, int], dict[int, MarkBar]],
) -> dict[int, MarkBar]:
    """Load mark OHLC for symbol across [ts0, ts1], spanning month shards."""
    key = (symbol, ts0, ts1)
    if key in cache:
        return cache[key]
    out: dict[int, MarkBar] = {}
    d0 = datetime.fromtimestamp(ts0, tz=UTC).date()
    d1 = datetime.fromtimestamp(ts1, tz=UTC).date()
    day = d0
    while day <= d1:
        conn = store.conn(day)
        if conn is not None:
            rows = conn.execute(
                """
                SELECT ts, open, high, low, close FROM marks
                WHERE symbol=? AND ts BETWEEN ? AND ?
                """,
                (symbol, ts0, ts1),
            ).fetchall()
            for ts, o, h, l, c in rows:
                if c is None or float(c) <= 0:
                    continue
                out[int(ts)] = MarkBar(
                    open=float(o) if o is not None else float(c),
                    high=float(h) if h is not None else float(c),
                    low=float(l) if l is not None else float(c),
                    close=float(c),
                )
        day += timedelta(days=1)
    cache[key] = out
    return out


def mark_at(series: dict[int, MarkBar], ts: int) -> MarkBar | None:
    minute = (ts // 60) * 60
    if minute in series:
        return series[minute]
    best: MarkBar | None = None
    best_abs = MARK_TOL_SEC + 1
    for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
        cand = series.get(minute + d)
        if cand is None:
            continue
        ad = abs(d)
        if ad < best_abs:
            best_abs = ad
            best = cand
    return best


def pick_strike(
    conn: sqlite3.Connection,
    day: date,
    entry_ts: int,
    forward: float,
    is_call: bool,
    rules: RuleCounts,
) -> tuple[str, float, float, int, str] | None:
    """
    Returns (symbol, strike, entry_mark, dte, rule_name) or None.
    Rules: (1) 0DTE prem+delta, (2) 1DTE prem+delta, (3) 1DTE nearest |delta| to 0.6.
    """
    opt_type = "call" if is_call else "put"
    t_settle_0 = settle_unix(day)
    t_years_0 = max((t_settle_0 - entry_ts) / SECONDS_PER_YEAR, 1e-12)

    def scored(
        expiry: date, t_years: float
    ) -> list[tuple[str, float, float, float]]:
        """(symbol, strike, mark, abs_delta) in premium band with valid IV."""
        cts = resolve_mark_ts(conn, expiry, entry_ts)
        if cts is None:
            return []
        chain = load_chain_at(conn, expiry, cts, opt_type)
        out: list[tuple[str, float, float, float]] = []
        for sym, strike, mark in chain:
            if mark < PREMIUM_MIN or mark > PREMIUM_MAX:
                continue
            iv = implied_vol_bisection(mark, forward, strike, t_years, is_call)
            if iv is None:
                continue
            dlt = black76_abs_delta(forward, strike, t_years, iv, is_call)
            out.append((sym, strike, mark, dlt))
        return out

    # Rule 1: 0DTE
    cands = scored(day, t_years_0)
    band = [x for x in cands if DELTA_MIN <= x[3] <= DELTA_MAX]
    if band:
        best = max(band, key=lambda x: x[2])
        rules.rule1_0dte += 1
        return best[0], best[1], best[2], 0, "rule1_0dte"

    # Rule 2: 1DTE same prem+delta
    exp1 = day + timedelta(days=1)
    t_years_1 = max((settle_unix(exp1) - entry_ts) / SECONDS_PER_YEAR, 1e-12)
    cands1 = scored(exp1, t_years_1)
    band1 = [x for x in cands1 if DELTA_MIN <= x[3] <= DELTA_MAX]
    if band1:
        best = max(band1, key=lambda x: x[2])
        rules.rule1_1dte += 1
        return best[0], best[1], best[2], 1, "rule1_1dte"

    # Rule 3: 1DTE nearest |delta| to DELTA_MIN (0.6)
    cts = resolve_mark_ts(conn, exp1, entry_ts)
    if cts is None:
        return None
    chain = load_chain_at(conn, exp1, cts, opt_type)
    best3: tuple[str, float, float, float] | None = None
    best_err = float("inf")
    for sym, strike, mark in chain:
        if mark <= 0:
            continue
        iv = implied_vol_bisection(mark, forward, strike, t_years_1, is_call)
        if iv is None:
            continue
        dlt = black76_abs_delta(forward, strike, t_years_1, iv, is_call)
        err = abs(dlt - DELTA_MIN)
        if err < best_err:
            best_err = err
            best3 = (sym, strike, mark, dlt)
    if best3 is None:
        return None
    rules.rule3_1dte_nearest += 1
    return best3[0], best3[1], best3[2], 1, "rule3_1dte_nearest"


def btc_label(
    bars: dict[int, SpotBar],
    entry_btc: float,
    is_call: bool,
    start_ts: int,
    cutoff_ts: int,
) -> str:
    """RIGHT / WRONG / NEITHER from BTC path only (not option exits)."""
    ts = ((start_ts // 60) * 60) + 60  # first full minute after entry bar
    while ts <= cutoff_ts:
        bar = bars.get(ts)
        if bar is None:
            ts += 60
            continue
        if is_call:
            hit_tgt = bar.high >= entry_btc + BTC_TARGET
            hit_stp = bar.close <= entry_btc - BTC_STOP
        else:
            hit_tgt = bar.low <= entry_btc - BTC_TARGET
            hit_stp = bar.close >= entry_btc + BTC_STOP
        if hit_tgt and hit_stp:
            # same bar: target tagged intrabar before close → RIGHT
            return "RIGHT"
        if hit_tgt:
            return "RIGHT"
        if hit_stp:
            return "WRONG"
        ts += 60
    return "NEITHER"


def resolve_slip_pct(premium: float, dte: int, slip_model: str) -> float:
    """Return slippage in percent units (1.65 = 1.65%)."""
    if slip_model == SLIP_MODEL_FLAT:
        return SLIP * 100.0
    return float(slip_pct(premium, dte))


def simulate_trade(
    bars: dict[int, SpotBar],
    marks: dict[int, MarkBar],
    entry_ts: int,
    entry_mark: float,
    entry_btc: float,
    is_call: bool,
    cutoff_ts: int,
    dte: int = 0,
    slip_model: str = SLIP_MODEL_FLAT,
) -> tuple[str, float, float, float] | None:
    """
    Returns (exit_reason a-e, pnl_usd, entry_slip_pct, exit_slip_pct)
    or None if path incomplete.
    Exit precedence per minute: a → b → c → d → e.
    """
    entry_slip_pct = resolve_slip_pct(entry_mark, dte, slip_model)
    entry_slip = entry_slip_pct / 100.0
    entry_fill = entry_mark * (1.0 + entry_slip)
    fee_in = eng.option_fee(entry_fill, entry_btc, QTY_LOTS)
    qty_btc = QTY_LOTS * CONTRACT_VALUE
    sl_level = entry_mark - BRACKET_SL
    tp_level = entry_mark + BRACKET_TP

    ts = ((entry_ts // 60) * 60) + 60
    while ts <= cutoff_ts:
        mbar = mark_at(marks, ts)
        sbar = bars.get(ts)
        if mbar is None:
            ts += 60
            continue

        # (a) max-loss bracket on option mark low
        if mbar.low <= sl_level:
            exit_slip_pct = resolve_slip_pct(mbar.close, dte, slip_model)
            exit_px = sl_level * (1.0 - exit_slip_pct / 100.0)
            fee_out = eng.option_fee(exit_px, entry_btc, QTY_LOTS)
            pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
            return "a", pnl, entry_slip_pct, exit_slip_pct

        # (b) target bracket on option mark high (limit, no slip)
        if mbar.high >= tp_level:
            exit_px = tp_level
            fee_out = eng.option_fee(exit_px, entry_btc, QTY_LOTS)
            pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
            return "b", pnl, entry_slip_pct, 0.0

        if sbar is not None:
            # (c) BTC target
            if is_call and sbar.high >= entry_btc + BTC_TARGET:
                exit_slip_pct = resolve_slip_pct(mbar.close, dte, slip_model)
                exit_px = mbar.close * (1.0 - exit_slip_pct / 100.0)
                fee_out = eng.option_fee(exit_px, sbar.close, QTY_LOTS)
                pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
                return "c", pnl, entry_slip_pct, exit_slip_pct
            if (not is_call) and sbar.low <= entry_btc - BTC_TARGET:
                exit_slip_pct = resolve_slip_pct(mbar.close, dte, slip_model)
                exit_px = mbar.close * (1.0 - exit_slip_pct / 100.0)
                fee_out = eng.option_fee(exit_px, sbar.close, QTY_LOTS)
                pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
                return "c", pnl, entry_slip_pct, exit_slip_pct

            # (d) BTC stop on close
            if is_call and sbar.close <= entry_btc - BTC_STOP:
                exit_slip_pct = resolve_slip_pct(mbar.close, dte, slip_model)
                exit_px = mbar.close * (1.0 - exit_slip_pct / 100.0)
                fee_out = eng.option_fee(exit_px, sbar.close, QTY_LOTS)
                pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
                return "d", pnl, entry_slip_pct, exit_slip_pct
            if (not is_call) and sbar.close >= entry_btc + BTC_STOP:
                exit_slip_pct = resolve_slip_pct(mbar.close, dte, slip_model)
                exit_px = mbar.close * (1.0 - exit_slip_pct / 100.0)
                fee_out = eng.option_fee(exit_px, sbar.close, QTY_LOTS)
                pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
                return "d", pnl, entry_slip_pct, exit_slip_pct

        # (e) cutoff at end of loop body when ts == cutoff
        if ts >= cutoff_ts:
            exit_slip_pct = resolve_slip_pct(mbar.close, dte, slip_model)
            exit_px = mbar.close * (1.0 - exit_slip_pct / 100.0)
            fee_out = eng.option_fee(exit_px, entry_btc, QTY_LOTS)
            pnl = (exit_px - entry_fill) * qty_btc - fee_in - fee_out
            return "e", pnl, entry_slip_pct, exit_slip_pct

        ts += 60

    # no mark at cutoff
    return None


def mean_or_nan(vals: list[float]) -> float:
    return float(statistics.fmean(vals)) if vals else float("nan")


def median_or_nan(vals: list[float]) -> float:
    return float(statistics.median(vals)) if vals else float("nan")


def label_stats(trades: list[TradeResult]) -> dict[str, Any]:
    by: dict[str, list[TradeResult]] = defaultdict(list)
    for t in trades:
        by[t.label].append(t)
    n = len(trades)
    out: dict[str, Any] = {
        "n": n,
        "p_right": (len(by["RIGHT"]) / n) if n else float("nan"),
        "p_wrong": (len(by["WRONG"]) / n) if n else float("nan"),
        "p_neither": (len(by["NEITHER"]) / n) if n else float("nan"),
        "by_label": {},
    }
    for lab in ("RIGHT", "WRONG", "NEITHER"):
        rows = by[lab]
        pnls = [x.pnl for x in rows]
        reasons = [x.exit_reason for x in rows]
        mix = {
            r: (reasons.count(r) / len(reasons) * 100.0) if reasons else float("nan")
            for r in ("a", "b", "c", "d", "e")
        }
        out["by_label"][lab] = {
            "n": len(rows),
            "mean": mean_or_nan(pnls),
            "median": median_or_nan(pnls),
            "exit_mix_pct": mix,
            "avg_entry_slip_pct": mean_or_nan([x.entry_slip_pct for x in rows]),
            "avg_exit_slip_pct": mean_or_nan([x.exit_slip_pct for x in rows]),
            "avg_applied_slip_pct": mean_or_nan(
                [0.5 * (x.entry_slip_pct + x.exit_slip_pct) for x in rows]
            ),
        }
    return out


def breakeven_p(stats: dict[str, Any]) -> tuple[float, float]:
    """
    Solve p·E_right + (1−r−p)·E_wrong + r·E_neither = 0
    with r = P(NEITHER) fixed.
    Returns (p, p/(1-r)).
    """
    r = float(stats["p_neither"])
    er = float(stats["by_label"]["RIGHT"]["mean"])
    ew = float(stats["by_label"]["WRONG"]["mean"])
    en = float(stats["by_label"]["NEITHER"]["mean"])
    if stats["n"] == 0 or math.isnan(r) or math.isnan(er) or math.isnan(ew):
        return float("nan"), float("nan")
    if math.isnan(en):
        en = 0.0
    denom = er - ew
    if abs(denom) < 1e-12:
        return float("nan"), float("nan")
    # p*er + (1-r-p)*ew + r*en = 0
    # p*(er-ew) = -r*en - (1-r)*ew
    p = (-r * en - (1.0 - r) * ew) / denom
    cond = p / (1.0 - r) if (1.0 - r) > 1e-12 else float("nan")
    return float(p), float(cond)


def fmt_mix(mix: dict[str, float]) -> str:
    parts = []
    for k in ("a", "b", "c", "d", "e"):
        v = mix.get(k, float("nan"))
        parts.append(f"{k}={v:.1f}%" if not math.isnan(v) else f"{k}=n/a")
    return " ".join(parts)


def report_block(lines: list[str], title: str, trades: list[TradeResult]) -> None:
    emit(lines, f"===== {title} =====")
    st = label_stats(trades)
    n = st["n"]
    emit(
        lines,
        f"n={n}  P(RIGHT)={100.0 * st['p_right']:.2f}%  "
        f"P(WRONG)={100.0 * st['p_wrong']:.2f}%  "
        f"P(NEITHER)={100.0 * st['p_neither']:.2f}%",
    )
    for lab in ("RIGHT", "WRONG", "NEITHER"):
        b = st["by_label"][lab]
        emit(
            lines,
            f"  {lab}: n={b['n']} mean={b['mean']:.4f} median={b['median']:.4f} "
            f"avg_slip%={b['avg_applied_slip_pct']:.4f} "
            f"(entry={b['avg_entry_slip_pct']:.4f} exit={b['avg_exit_slip_pct']:.4f}) "
            f"exits[{fmt_mix(b['exit_mix_pct'])}]",
        )
    p, p_cond = breakeven_p(st)
    edge = (
        (p - st["p_right"]) * 100.0
        if not math.isnan(p) and not math.isnan(st["p_right"])
        else float("nan")
    )
    emit(
        lines,
        f"  breakeven_p={p:.4f}  breakeven_p/(1-r)={p_cond:.4f}  "
        f"required_edge_pp={edge:.2f}",
    )
    emit(lines)


def day_clustered_bootstrap_p(
    trades: list[TradeResult], n: int, seed: int
) -> tuple[float, float, float]:
    """95% CI for breakeven p via day-level resample."""
    by_day: dict[date, list[TradeResult]] = defaultdict(list)
    for t in trades:
        by_day[t.day].append(t)
    days = sorted(by_day.keys())
    if not days:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    ps: list[float] = []
    for _ in range(n):
        sample: list[TradeResult] = []
        for _d in days:
            day = days[rng.randrange(len(days))]
            sample.extend(by_day[day])
        st = label_stats(sample)
        p, _ = breakeven_p(st)
        if not math.isnan(p):
            ps.append(p)
    if not ps:
        return float("nan"), float("nan"), float("nan")
    ps.sort()
    lo = ps[int(0.025 * (len(ps) - 1))]
    hi = ps[int(0.975 * (len(ps) - 1))]
    return float(statistics.fmean(ps)), float(lo), float(hi)


def run(month: str | None, grid_min: int, slip_model: str) -> list[str]:
    lines: list[str] = []
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if slip_model == SLIP_MODEL_BUCKETED:
        load_slip_table()

    emit(lines, "===== S004 SIGNAL-AGNOSTIC COST GATE =====")
    emit(lines, f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    emit(lines, "premium = quoted USD per 1 BTC")
    emit(
        lines,
        f"PREMIUM=[{PREMIUM_MIN},{PREMIUM_MAX}] DELTA=[{DELTA_MIN},{DELTA_MAX}] "
        f"BTC_TARGET={BTC_TARGET} BTC_STOP={BTC_STOP} "
        f"BRACKET_SL={BRACKET_SL} BRACKET_TP={BRACKET_TP} SLIP={SLIP} "
        f"slip_model={slip_model}",
    )
    emit(lines, f"qty_lots={QTY_LOTS} contract_value={CONTRACT_VALUE} grid_min={grid_min}")
    emit(lines, f"month_filter={month or 'ALL_INTERSECTION'}")
    emit(lines)

    spot_files = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    if not spot_files:
        emit(lines, f"ERROR: no BTCUSD_1m_*.csv in {DATA_1M_DIR}")
        return lines
    spot_path = spot_files[-1]
    emit(lines, f"spot_csv={spot_path}")
    bars = load_spot_ohlc(spot_path)
    emit(lines, f"spot_bars={len(bars)}")

    store = MarksStore()
    emit(lines, f"marks_months={store.available_months}")

    s_days = spot_days(bars)
    m_days: set[date] = set()
    months = [month] if month else list(store.available_months)
    for ym in months:
        if ym not in store.available_months:
            emit(lines, f"WARN: marks shard missing for {ym}")
            continue
        m_days |= marks_days_in_month(store, ym)

    days = sorted(s_days & m_days)
    if month:
        y, m = int(month[:4]), int(month[5:7])
        days = [d for d in days if d.year == y and d.month == m]

    emit(lines, f"days_used_count={len(days)}")
    emit(lines, "days_used_list:")
    for d in days:
        emit(lines, f"  {d.isoformat()}")
    emit(lines)

    skips = SkipCounts()
    rules = RuleCounts()
    trades: list[TradeResult] = []
    entries_attempted = 0
    marks_cache: dict[tuple[str, int, int], dict[int, MarkBar]] = {}

    for day in days:
        conn = store.conn(day)
        if conn is None:
            continue
        slots = entry_grid_slots(day, grid_min)
        for slot in slots:
            entry_dt = slot + timedelta(minutes=1)
            entry_ts = to_unix(entry_dt)
            entries_attempted += 2  # call + put

            sbar, spot_skip = spot_bar_fresh(bars, entry_ts)
            if sbar is None:
                if spot_skip == "stale_spot":
                    skips.stale_spot += 2
                else:
                    skips.no_spot_bar += 2
                continue

            entry_btc = sbar.open
            forward = sbar.close  # forward for IV/delta = BTC 1m close

            for is_call in (True, False):
                side = "call" if is_call else "put"
                picked = pick_strike(
                    conn, day, entry_ts, forward, is_call, rules
                )
                if picked is None:
                    skips.no_strike += 1
                    continue
                symbol, _strike, entry_mark, dte, rule = picked
                expiry = day + timedelta(days=dte)
                cutoff_ts = to_unix(
                    ist_dt(expiry, CUTOFF_HOUR_IST, CUTOFF_MINUTE_IST)
                )

                series = load_symbol_marks(
                    store, symbol, entry_ts, cutoff_ts, marks_cache
                )
                if not series:
                    skips.no_entry_mark += 1
                    continue
                # refresh entry mark from series if present
                m0 = mark_at(series, entry_ts)
                if m0 is not None and m0.close > 0:
                    entry_mark = m0.close

                label = btc_label(bars, entry_btc, is_call, entry_ts, cutoff_ts)
                sim = simulate_trade(
                    bars,
                    series,
                    entry_ts,
                    entry_mark,
                    entry_btc,
                    is_call,
                    cutoff_ts,
                    dte=dte,
                    slip_model=slip_model,
                )
                if sim is None:
                    skips.no_exit_path += 1
                    continue
                reason, pnl, entry_slip_pct, exit_slip_pct = sim
                trades.append(
                    TradeResult(
                        day=day,
                        side=side,
                        dte=dte,
                        rule=rule,
                        entry_hour_ist=slot.hour,
                        label=label,
                        exit_reason=reason,
                        pnl=pnl,
                        entry_slip_pct=entry_slip_pct,
                        exit_slip_pct=exit_slip_pct,
                    )
                )

        logger.info(
            "day=%s trades_so_far=%d", day.isoformat(), len(trades)
        )

    emit(lines, "----- SKIPS / COUNTS -----")
    emit(lines, f"entries_attempted_call_put_slots={entries_attempted}")
    emit(lines, f"trades_completed={len(trades)}")
    emit(lines, f"skip_no_spot_bar={skips.no_spot_bar}")
    emit(lines, f"skip_stale_spot={skips.stale_spot}")
    emit(lines, f"skip_no_strike={skips.no_strike}")
    emit(lines, f"skip_no_entry_mark={skips.no_entry_mark}")
    emit(lines, f"skip_no_exit_path={skips.no_exit_path}")
    emit(lines, f"strike_rule1_0dte={rules.rule1_0dte}")
    emit(lines, f"strike_rule1_1dte={rules.rule1_1dte}")
    emit(lines, f"strike_rule3_1dte_nearest={rules.rule3_1dte_nearest}")
    emit(lines)

    report_block(lines, "ALL TRADES", trades)
    report_block(lines, "CALL ONLY", [t for t in trades if t.side == "call"])
    report_block(lines, "PUT ONLY", [t for t in trades if t.side == "put"])
    report_block(lines, "0DTE ONLY", [t for t in trades if t.dte == 0])
    report_block(lines, "1DTE ONLY", [t for t in trades if t.dte == 1])

    # IST hour buckets
    hours = sorted({t.entry_hour_ist for t in trades})
    for hh in hours:
        report_block(
            lines,
            f"IST HOUR {hh:02d}:xx",
            [t for t in trades if t.entry_hour_ist == hh],
        )

    mean_p, lo, hi = day_clustered_bootstrap_p(trades, BOOTSTRAP_N, BOOTSTRAP_SEED)
    emit(lines, "===== DAY-CLUSTERED BOOTSTRAP (breakeven p) =====")
    emit(
        lines,
        f"n_resamples={BOOTSTRAP_N} seed={BOOTSTRAP_SEED} "
        f"mean_p={mean_p:.4f} 95%CI=[{lo:.4f}, {hi:.4f}]",
    )
    emit(lines)
    emit(lines, "premium = quoted USD per 1 BTC")

    store.close()
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="S004 signal-agnostic cost gate")
    ap.add_argument("--month", type=str, default=None, help="YYYY-MM optional")
    ap.add_argument("--grid-min", type=int, default=15)
    ap.add_argument(
        "--slip-model",
        type=str,
        default=SLIP_MODEL_BUCKETED,
        choices=(SLIP_MODEL_BUCKETED, SLIP_MODEL_FLAT),
        help="bucketed=calibrate_slippage.csv lookup; flat165=0.0165 baseline",
    )
    args = ap.parse_args()
    if args.month is not None and len(args.month) != 7:
        raise SystemExit("--month must be YYYY-MM")

    lines = run(args.month, args.grid_min, args.slip_model)
    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()
    logger.info("wrote %s", OUT_PATH)


if __name__ == "__main__":
    main()
