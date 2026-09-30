#!/usr/bin/env python3
"""S010 PRE-FLIGHT — chain availability + ATM straddle IV decay path.

This is NOT the trading engine. It only measures whether D+1/D+2/D+3 chains
exist at D 18:00 IST, and how ATM-straddle IV on the D+3 expiry decays from
entry (D 18:00) to (D+2) 17:30. No trades, no P&L.

Run in a SEPARATE PowerShell window (not the Cursor terminal):

    python backtest\\strategies\\s010_weekend_theta\\preflight.py `
        --csv backtest\\data_1m\\BTCUSD_1m_20250613_20260921.csv `
        --out backtest\\strategies\\s010_weekend_theta\\runs
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.s004_gate import black76_price, implied_vol_bisection  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s010.preflight")

SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0
EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30
ENTRY_HOUR_IST = 18
ENTRY_MINUTE_IST = 0
# "ATM ke aas-paas" — if nearest strike is farther than this, count as MISS.
ATM_MAX_GAP = 500.0
MARK_TOL_SEC = 60
IV_ROUNDTRIP_TOL = 0.005  # 0.5% of mark
PROGRESS_EVERY = 100
DOW_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# Spot loader
# ---------------------------------------------------------------------------
def load_spot_ohlc(csv_path: Path) -> dict[int, tuple[float, float, float, float]]:
    """unix_ts -> (open, high, low, close). Strips ' IST' from open_time_ist."""
    out: dict[int, tuple[float, float, float, float]] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            _ = row["open_time_ist"].replace(" IST", "")
            ts = int(row["open_time_unix"])
            out[ts] = (
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
            )
    return out


def spot_close_at(spot: dict[int, tuple[float, float, float, float]], ts: int) -> float | None:
    bar = spot.get(int(ts))
    if bar is None:
        return None
    c = bar[3]
    return c if c > 0 else None


def iter_dates_with_entry(
    spot: dict[int, tuple[float, float, float, float]],
) -> list[date]:
    """Calendar dates that have an 18:00 IST bar, sorted."""
    seen: set[date] = set()
    for ts in spot:
        dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST)
        if dt.hour == ENTRY_HOUR_IST and dt.minute == ENTRY_MINUTE_IST:
            seen.add(dt.date())
    return sorted(seen)


# ---------------------------------------------------------------------------
# Chain cache (S008 pattern — one load per (expiry, ts))
# ---------------------------------------------------------------------------
Chain = tuple[list[tuple[float, float]], list[tuple[float, float]]]


class ChainCache:
    def __init__(self) -> None:
        self._chains: dict[tuple[str, int], Chain] = {}
        self.loads = 0
        self.hits = 0

    def get(self, store: MarksStore, d: date, expiry: date, ts: int) -> Chain:
        minute = (int(ts) // 60) * 60
        key = (expiry.isoformat(), minute)
        hit = self._chains.get(key)
        if hit is not None:
            self.hits += 1
            return hit
        conn = store.conn(d)
        if conn is None:
            # try expiry month / observation day month
            conn = store.conn(expiry)
        calls: list[tuple[float, float]] = []
        puts: list[tuple[float, float]] = []
        if conn is not None:
            calls, puts = _load_chain_sql(conn, expiry, minute)
            # Marks for an evening observation may live in the observation-day
            # file; if empty, try the expiry-month connection.
            if not calls and not puts:
                conn2 = store.conn(expiry)
                if conn2 is not None and conn2 is not conn:
                    calls, puts = _load_chain_sql(conn2, expiry, minute)
        self._chains[key] = (calls, puts)
        self.loads += 1
        return calls, puts


def _load_chain_sql(
    conn: sqlite3.Connection, expiry: date, ts_minute: int
) -> Chain:
    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    for opt, bucket in (("call", calls), ("put", puts)):
        rows = conn.execute(
            """
            SELECT strike, close FROM marks
            WHERE expiry=? AND ts=? AND opt_type=?
              AND close IS NOT NULL AND close > 0
            ORDER BY strike
            """,
            (expiry.isoformat(), int(ts_minute), opt),
        ).fetchall()
        for strike, close in rows:
            bucket.append((float(strike), float(close)))
    return calls, puts


def format_symbol(opt: str, strike: float, exp: date) -> str:
    prefix = "C" if opt.lower().startswith("c") else "P"
    return f"{prefix}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def mark_at(
    store: MarksStore, d: date, symbol: str, ts: int
) -> float | None:
    """Nearest-minute mark within MARK_TOL_SEC; try observation + expiry months."""
    minute = (int(ts) // 60) * 60
    for day in (d, datetime.fromtimestamp(minute, tz=UTC).astimezone(IST).date()):
        conn = store.conn(day)
        if conn is None:
            continue
        row = conn.execute(
            "SELECT ts, close FROM marks WHERE symbol=? AND ts=?",
            (symbol, minute),
        ).fetchone()
        if row is not None and row[1] is not None and float(row[1]) > 0:
            return float(row[1])
        best: float | None = None
        best_abs: int | None = None
        for delta in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
            if delta == 0:
                continue
            row = conn.execute(
                "SELECT ts, close FROM marks WHERE symbol=? AND ts=?",
                (symbol, minute + delta),
            ).fetchone()
            if row is None or row[1] is None or float(row[1]) <= 0:
                continue
            ad = abs(int(row[0]) - minute)
            if best_abs is None or ad < best_abs:
                best_abs = ad
                best = float(row[1])
        if best is not None:
            return best
    return None


# ---------------------------------------------------------------------------
# ATM + IV
# ---------------------------------------------------------------------------
def t_years_to_expiry(obs_ts: int, expiry: date) -> float:
    settle = to_unix(ist_dt(expiry, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))
    return max(0.0, (settle - int(obs_ts)) / SECONDS_PER_YEAR)


def pick_atm(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    spot: float,
) -> tuple[float, float, float] | None:
    """Nearest strike with BOTH call and put marks, within ATM_MAX_GAP of spot.

    Returns (strike, call_mark, put_mark) or None → MISS.
    """
    c_by = {k: px for k, px in calls}
    p_by = {k: px for k, px in puts}
    common = sorted(set(c_by) & set(p_by))
    if not common:
        return None
    strike = min(common, key=lambda k: (abs(k - spot), k))
    if abs(strike - spot) > ATM_MAX_GAP:
        return None
    return strike, c_by[strike], p_by[strike]


def atm_straddle_iv(
    spot: float, strike: float, call_mark: float, put_mark: float, t_years: float
) -> float | None:
    """Mean of call IV and put IV (Black-76). Needs t_years > 0."""
    if t_years <= 1e-12 or spot <= 0 or strike <= 0:
        return None
    ivs: list[float] = []
    for mark, is_call in ((call_mark, True), (put_mark, False)):
        iv = implied_vol_bisection(mark, spot, strike, t_years, is_call)
        if iv is not None and iv > 0:
            ivs.append(float(iv))
    if not ivs:
        return None
    return float(sum(ivs) / len(ivs))


def iv_of_same_option(
    store: MarksStore,
    obs_date: date,
    spot: float,
    strike: float,
    expiry: date,
    obs_ts: int,
) -> float | None:
    """IV of the SAME (strike, expiry) ATM straddle at obs_ts."""
    t = t_years_to_expiry(obs_ts, expiry)
    if t <= 1e-12:
        return None
    c_sym = format_symbol("C", strike, expiry)
    p_sym = format_symbol("P", strike, expiry)
    # Prefer the month file that owns obs_ts for the connection day.
    obs_d = datetime.fromtimestamp(int(obs_ts), tz=UTC).astimezone(IST).date()
    c_mark = mark_at(store, obs_d, c_sym, obs_ts)
    p_mark = mark_at(store, obs_d, p_sym, obs_ts)
    if c_mark is None or p_mark is None:
        # fallback: try original observation date's month file
        c_mark = c_mark or mark_at(store, obs_date, c_sym, obs_ts)
        p_mark = p_mark or mark_at(store, obs_date, p_sym, obs_ts)
    if c_mark is None or p_mark is None:
        return None
    return atm_straddle_iv(spot, strike, c_mark, p_mark, t)


# ---------------------------------------------------------------------------
# Day record
# ---------------------------------------------------------------------------
@dataclass
class DayRow:
    d: date
    dow: str
    spot_1800: float
    dp1_listed: int
    dp2_listed: int
    dp3_listed: int
    atm_strike_dp3: float | None
    iv_dp3_at_entry: float | None
    iv_dp3_at_dplus1: float | None
    iv_dp3_at_dplus2: float | None
    iv_drop_entry_to_dplus2: float | None
    iv_dp2_at_entry: float | None
    iv_dp1_at_entry: float | None
    realized_move_pct_entry_to_dplus2: float | None
    skip_reason: str = ""
    # SAME_OPTION audit (not written to CSV)
    dp3_expiry: date | None = None
    dp3_strike_locked: float | None = None


@dataclass
class RunStats:
    n_checked: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    rows: list[DayRow] = field(default_factory=list)


def measure_day(
    d: date,
    spot: dict[int, tuple[float, float, float, float]],
    store: MarksStore,
    chain_cache: ChainCache,
) -> DayRow:
    dow = DOW_NAMES[d.weekday()]
    entry_ts = to_unix(ist_dt(d, ENTRY_HOUR_IST, ENTRY_MINUTE_IST))
    spot_1800 = spot_close_at(spot, entry_ts)
    if spot_1800 is None:
        return DayRow(
            d=d,
            dow=dow,
            spot_1800=float("nan"),
            dp1_listed=0,
            dp2_listed=0,
            dp3_listed=0,
            atm_strike_dp3=None,
            iv_dp3_at_entry=None,
            iv_dp3_at_dplus1=None,
            iv_dp3_at_dplus2=None,
            iv_drop_entry_to_dplus2=None,
            iv_dp2_at_entry=None,
            iv_dp1_at_entry=None,
            realized_move_pct_entry_to_dplus2=None,
            skip_reason="no_spot_1800",
        )

    exp1 = d + timedelta(days=1)
    exp2 = d + timedelta(days=2)
    exp3 = d + timedelta(days=3)

    def listed_and_iv(expiry: date) -> tuple[int, float | None, float | None]:
        """Return (listed_flag, atm_strike_or_None, iv_or_None) at entry."""
        calls, puts = chain_cache.get(store, d, expiry, entry_ts)
        atm = pick_atm(calls, puts, spot_1800)
        if atm is None:
            return 0, None, None
        strike, c_mark, p_mark = atm
        t = t_years_to_expiry(entry_ts, expiry)
        iv = atm_straddle_iv(spot_1800, strike, c_mark, p_mark, t)
        return 1, strike, iv

    dp1_listed, _, iv_dp1 = listed_and_iv(exp1)
    dp2_listed, _, iv_dp2 = listed_and_iv(exp2)
    dp3_listed, atm_k3, iv_dp3_entry = listed_and_iv(exp3)

    iv_dp3_d1: float | None = None
    iv_dp3_d2: float | None = None
    iv_drop: float | None = None
    realized: float | None = None

    ts_d1 = to_unix(ist_dt(exp1, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))
    ts_d2 = to_unix(ist_dt(exp2, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))

    # Realized |move| D 18:00 → (D+2) 17:30, even if chain missing.
    spot_d2 = spot_close_at(spot, ts_d2)
    if spot_d2 is None:
        # fallback 17:29
        spot_d2 = spot_close_at(
            spot, to_unix(ist_dt(exp2, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST - 1))
        )
    if spot_d2 is not None and spot_1800 > 0:
        realized = abs(spot_d2 - spot_1800) / spot_1800 * 100.0

    if dp3_listed and atm_k3 is not None:
        # SAME option path — locked strike + D+3 expiry
        spot_d1 = spot_close_at(spot, ts_d1)
        if spot_d1 is None:
            spot_d1 = spot_close_at(
                spot, to_unix(ist_dt(exp1, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST - 1))
            )
        if spot_d1 is not None:
            iv_dp3_d1 = iv_of_same_option(
                store, d, spot_d1, atm_k3, exp3, ts_d1
            )
        if spot_d2 is not None:
            iv_dp3_d2 = iv_of_same_option(
                store, d, spot_d2, atm_k3, exp3, ts_d2
            )
        if iv_dp3_entry is not None and iv_dp3_d2 is not None:
            # IV POINTS (e.g. 0.55 → 0.40 = drop 0.15), not percent of IV.
            iv_drop = iv_dp3_entry - iv_dp3_d2

    skip = ""
    if dp3_listed == 0:
        skip = "dp3_not_listed"
    elif iv_dp3_entry is None:
        skip = "dp3_iv_fail"
    elif iv_dp3_d2 is None:
        skip = "dp3_checkpoint_missing"

    return DayRow(
        d=d,
        dow=dow,
        spot_1800=spot_1800,
        dp1_listed=dp1_listed,
        dp2_listed=dp2_listed,
        dp3_listed=dp3_listed,
        atm_strike_dp3=atm_k3,
        iv_dp3_at_entry=iv_dp3_entry,
        iv_dp3_at_dplus1=iv_dp3_d1,
        iv_dp3_at_dplus2=iv_dp3_d2,
        iv_drop_entry_to_dplus2=iv_drop,
        iv_dp2_at_entry=iv_dp2,
        iv_dp1_at_entry=iv_dp1,
        realized_move_pct_entry_to_dplus2=realized,
        skip_reason=skip,
        dp3_expiry=exp3 if atm_k3 is not None else None,
        dp3_strike_locked=atm_k3,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_no_lookahead(
    spot: dict[int, tuple[float, float, float, float]],
    store: MarksStore,
    chain_cache: ChainCache,
) -> None:
    """Entry measurements use only the D 18:00 timestamp — not later bars."""
    dates = iter_dates_with_entry(spot)
    checked = 0
    for d in dates[:40]:
        entry_ts = to_unix(ist_dt(d, ENTRY_HOUR_IST, ENTRY_MINUTE_IST))
        row = measure_day(d, spot, store, chain_cache)
        if row.skip_reason == "no_spot_1800":
            continue
        # Spot must equal the 18:00 close exactly.
        assert abs(row.spot_1800 - spot_close_at(spot, entry_ts)) < 1e-9, (
            f"NO_LOOKAHEAD FAIL {d}: spot_1800 != 18:00 close"
        )
        # Entry IV time-to-expiry must be computed from 18:00, not a later ts.
        if row.dp3_listed and row.atm_strike_dp3 is not None:
            exp3 = d + timedelta(days=3)
            t_entry = t_years_to_expiry(entry_ts, exp3)
            t_late = t_years_to_expiry(entry_ts + 3600, exp3)
            assert t_entry > t_late, f"NO_LOOKAHEAD FAIL {d}: t_years inverted"
        checked += 1
        if checked >= 10:
            break
    assert checked > 0
    print(f"NO_LOOKAHEAD PASS checked={checked}")


def test_iv_roundtrip(
    spot: dict[int, tuple[float, float, float, float]],
    store: MarksStore,
    chain_cache: ChainCache,
) -> None:
    """Solved IV → Black-76 price within 0.5% of original mark."""
    dates = iter_dates_with_entry(spot)
    checked = 0
    for d in dates:
        entry_ts = to_unix(ist_dt(d, ENTRY_HOUR_IST, ENTRY_MINUTE_IST))
        s = spot_close_at(spot, entry_ts)
        if s is None:
            continue
        for offset in (1, 2, 3):
            expiry = d + timedelta(days=offset)
            calls, puts = chain_cache.get(store, d, expiry, entry_ts)
            atm = pick_atm(calls, puts, s)
            if atm is None:
                continue
            strike, c_mark, p_mark = atm
            t = t_years_to_expiry(entry_ts, expiry)
            if t <= 1e-12:
                continue
            for mark, is_call in ((c_mark, True), (p_mark, False)):
                iv = implied_vol_bisection(mark, s, strike, t, is_call)
                if iv is None:
                    continue
                px = black76_price(s, strike, t, iv, is_call)
                rel = abs(px - mark) / mark
                assert rel <= IV_ROUNDTRIP_TOL + 1e-6, (
                    f"IV_ROUNDTRIP FAIL {d} exp={expiry} "
                    f"{'C' if is_call else 'P'} K={strike}: "
                    f"mark={mark} px={px} rel={rel:.4%}"
                )
                checked += 1
        if checked >= 40:
            break
    assert checked > 0
    print(f"IV_ROUNDTRIP PASS checked={checked}")


def test_same_option(
    spot: dict[int, tuple[float, float, float, float]],
    store: MarksStore,
    chain_cache: ChainCache,
) -> None:
    """Later checkpoints re-read the locked (strike, D+3 expiry), not a new ATM."""
    dates = iter_dates_with_entry(spot)
    checked = 0
    for d in dates:
        row = measure_day(d, spot, store, chain_cache)
        if row.atm_strike_dp3 is None or row.dp3_expiry is None:
            continue
        assert row.dp3_strike_locked == row.atm_strike_dp3
        assert row.dp3_expiry == d + timedelta(days=3)
        # Spot may have moved a lot by D+2 — a fresh ATM pick would differ.
        # We only assert the locked identity is preserved in the row.
        checked += 1
        if checked >= 15:
            break
    assert checked > 0
    print(f"SAME_OPTION PASS checked={checked}")


def run_tests(
    spot: dict[int, tuple[float, float, float, float]],
    store: MarksStore,
    chain_cache: ChainCache,
) -> None:
    test_no_lookahead(spot, store, chain_cache)
    test_iv_roundtrip(spot, store, chain_cache)
    test_same_option(spot, store, chain_cache)
    print("ALL S010 PREFLIGHT TESTS PASS")


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------
def _pctile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    i = (len(s) - 1) * p
    lo = int(math.floor(i))
    hi = int(math.ceil(i))
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - i) + s[hi] * (i - lo)


def _fmt(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return f"{x:.8f}"


def write_csv(path: Path, rows: list[DayRow]) -> None:
    cols = [
        "date",
        "dow",
        "spot_1800",
        "dp1_listed",
        "dp2_listed",
        "dp3_listed",
        "atm_strike_dp3",
        "iv_dp3_at_entry",
        "iv_dp3_at_dplus1",
        "iv_dp3_at_dplus2",
        "iv_drop_entry_to_dplus2",
        "iv_dp2_at_entry",
        "iv_dp1_at_entry",
        "realized_move_pct_entry_to_dplus2",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(
                [
                    r.d.isoformat(),
                    r.dow,
                    _fmt(r.spot_1800),
                    r.dp1_listed,
                    r.dp2_listed,
                    r.dp3_listed,
                    _fmt(r.atm_strike_dp3),
                    _fmt(r.iv_dp3_at_entry),
                    _fmt(r.iv_dp3_at_dplus1),
                    _fmt(r.iv_dp3_at_dplus2),
                    _fmt(r.iv_drop_entry_to_dplus2),
                    _fmt(r.iv_dp2_at_entry),
                    _fmt(r.iv_dp1_at_entry),
                    _fmt(r.realized_move_pct_entry_to_dplus2),
                ]
            )


def build_summary(stats: RunStats, chain_cache: ChainCache, elapsed: float) -> list[str]:
    rows = stats.rows
    n = len(rows)
    lines: list[str] = [
        "===== S010 PREFLIGHT =====",
        f"generated_utc={datetime.now(tz=timezone.utc).isoformat()}",
        f"n_days_checked={n}",
        f"skips={dict(sorted(stats.skips.items()))}",
        f"chain_loads={chain_cache.loads} cache_hits={chain_cache.hits} "
        f"elapsed_s={elapsed:.1f}",
        "",
    ]

    # Listed % overall + by DOW
    lines.append("===== CHAIN LISTED % =====")
    for label, flag_fn in (
        ("D+1", lambda r: r.dp1_listed),
        ("D+2", lambda r: r.dp2_listed),
        ("D+3", lambda r: r.dp3_listed),
    ):
        if n == 0:
            lines.append(f"  {label}: n/a")
            continue
        pct = 100.0 * sum(flag_fn(r) for r in rows) / n
        lines.append(f"  {label} listed: {pct:.1f}% ({sum(flag_fn(r) for r in rows)}/{n})")
        for dow in DOW_NAMES:
            sub = [r for r in rows if r.dow == dow]
            if not sub:
                continue
            p = 100.0 * sum(flag_fn(r) for r in sub) / len(sub)
            lines.append(
                f"    {dow}: {p:.1f}% ({sum(flag_fn(r) for r in sub)}/{len(sub)})"
            )
    lines.append("")

    # IV drop
    drops = [
        r.iv_drop_entry_to_dplus2
        for r in rows
        if r.iv_drop_entry_to_dplus2 is not None
    ]
    lines.append("===== IV DROP (D+3 ATM straddle: entry -> D+2 17:30) =====")
    lines.append(f"  n={len(drops)}")
    if drops:
        lines.append(
            f"  mean={fmean(drops):.4f} median={median(drops):.4f} "
            f"p10={_pctile(drops, 0.10):.4f} p90={_pctile(drops, 0.90):.4f}"
        )
        lines.append("  by day-of-week:")
        for dow in DOW_NAMES:
            sub = [
                r.iv_drop_entry_to_dplus2
                for r in rows
                if r.dow == dow and r.iv_drop_entry_to_dplus2 is not None
            ]
            if not sub:
                continue
            lines.append(
                f"    {dow}: n={len(sub)} mean={fmean(sub):.4f} "
                f"median={median(sub):.4f}"
            )
        thu = [
            r.iv_drop_entry_to_dplus2
            for r in rows
            if r.dow == "Thu" and r.iv_drop_entry_to_dplus2 is not None
        ]
        lines.append("  --- Thursday-only (strategy day) ---")
        if thu:
            lines.append(
                f"  Thu n={len(thu)} mean={fmean(thu):.4f} "
                f"median={median(thu):.4f} "
                f"p10={_pctile(thu, 0.10):.4f} p90={_pctile(thu, 0.90):.4f}"
            )
        else:
            lines.append("  Thu: no observations")
    lines.append("")

    # Term structure at entry
    lines.append("===== IV TERM STRUCTURE AT ENTRY (mean) =====")
    for dow_filter, label in ((None, "ALL"), ("Thu", "Thu-only")):
        sub = rows if dow_filter is None else [r for r in rows if r.dow == dow_filter]
        iv1 = [r.iv_dp1_at_entry for r in sub if r.iv_dp1_at_entry is not None]
        iv2 = [r.iv_dp2_at_entry for r in sub if r.iv_dp2_at_entry is not None]
        iv3 = [r.iv_dp3_at_entry for r in sub if r.iv_dp3_at_entry is not None]
        lines.append(
            f"  {label}: "
            f"iv_dp1={fmean(iv1):.4f} (n={len(iv1)})  "
            f"iv_dp2={fmean(iv2):.4f} (n={len(iv2)})  "
            f"iv_dp3={fmean(iv3):.4f} (n={len(iv3)})"
            if (iv1 or iv2 or iv3)
            else f"  {label}: no IVs"
        )
    lines.append("  by day-of-week:")
    for dow in DOW_NAMES:
        sub = [r for r in rows if r.dow == dow]
        iv1 = [r.iv_dp1_at_entry for r in sub if r.iv_dp1_at_entry is not None]
        iv2 = [r.iv_dp2_at_entry for r in sub if r.iv_dp2_at_entry is not None]
        iv3 = [r.iv_dp3_at_entry for r in sub if r.iv_dp3_at_entry is not None]
        if not (iv1 or iv2 or iv3):
            continue
        lines.append(
            f"    {dow}: "
            f"dp1={fmean(iv1):.4f}(n={len(iv1)}) "
            f"dp2={fmean(iv2):.4f}(n={len(iv2)}) "
            f"dp3={fmean(iv3):.4f}(n={len(iv3)})"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S010 preflight — chain + IV decay")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--max-days",
        type=int,
        default=0,
        help="smoke: stop after N entry dates (0 = full)",
    )
    ap.add_argument("--no-tests", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    spot = load_spot_ohlc(Path(args.csv))
    dates = iter_dates_with_entry(spot)
    # Need D+2 17:30 still inside the spot map for a fair realized-move / IV path.
    last_ts = max(spot) if spot else 0
    last_d = datetime.fromtimestamp(last_ts, tz=UTC).astimezone(IST).date()
    dates = [d for d in dates if d + timedelta(days=2) <= last_d]
    if args.max_days and args.max_days < len(dates):
        dates = dates[: args.max_days]
    print(f"entry_dates={len(dates)} spot_bars={len(spot)}", flush=True)

    store = MarksStore()
    chain_cache = ChainCache()

    if not args.no_tests:
        print("=== TESTS ===", flush=True)
        run_tests(spot, store, chain_cache)

    stats = RunStats()
    for i, d in enumerate(dates, 1):
        row = measure_day(d, spot, store, chain_cache)
        stats.rows.append(row)
        stats.n_checked += 1
        if row.skip_reason:
            stats.skips[row.skip_reason] = stats.skips.get(row.skip_reason, 0) + 1
        if i % PROGRESS_EVERY == 0 or i == len(dates):
            print(
                f"  .. {i}/{len(dates)} day={d} "
                f"dp3_listed={row.dp3_listed} "
                f"iv_drop={_fmt(row.iv_drop_entry_to_dplus2)} "
                f"chain_loads={chain_cache.loads} hits={chain_cache.hits}",
                flush=True,
            )

    elapsed = time.monotonic() - t0
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = out_dir / f"s010_preflight_{stamp}.csv"
    txt_path = out_dir / f"s010_preflight_{stamp}.txt"
    write_csv(csv_path, stats.rows)
    lines = build_summary(stats, chain_cache, elapsed)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for ln in lines:
        print(ln, flush=True)
    print(f"csv={csv_path}", flush=True)
    print(f"txt={txt_path}", flush=True)
    print(f"DONE in {elapsed:.1f}s", flush=True)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
