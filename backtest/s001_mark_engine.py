#!/usr/bin/env python3
"""
S001 mark-data backtest engine.

Runs the locked short-strangle + Adj B + wings cycle on option MARK bars
(backtest/cache/option_marks/marks_YYYY-MM.sqlite), not trade prints.

Does NOT modify live bot code or s001_income_engine.py.
Imports pure helpers from live adj_b / wing_entry for G3/G4/G7 parity.

No print() — logging + file writes only.
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
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s001_income_engine as eng  # noqa: E402
from slippage_model import load_slip_table, slip_pct  # noqa: E402

# Live pure helpers — read-only imports (do not modify backend/)
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    is_adj_b_no_strike_inside_wing,
    resolve_adj_b_wing_strike,
    select_adj_b_strike,
)

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
MARKS_DIR = _BACKTEST / "cache" / "option_marks"
DATA_1M_DIR = _BACKTEST / "data_1m"
RESULTS_DIR = _BACKTEST / "results"
OUT_CSV = RESULTS_DIR / "s001_mark_cycles.csv"
OUT_TXT = RESULTS_DIR / "s001_mark_engine.txt"

SKIP_EXPIRY = date(2025, 4, 26)
CONTRACT_VALUE = eng.CONTRACT_VALUE
EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30
PRE_EXPIRY_MINUTE_IST = 15  # 17:15
MARK_TOL_SEC = 60
MONITOR_STEP_SEC = 60
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260918
MAX_ENTRIES_PER_DAY = 3

SLIP_FLAT = 0.0165
SLIP_MODEL_FLAT = "flat165"
SLIP_MODEL_BUCKETED = "bucketed"

logger = logging.getLogger("s001_mark_engine")


# ---------------------------------------------------------------------------
# Parity checklist (G1..G10) — citations from live / locked CFG
# ---------------------------------------------------------------------------
def parity_checklist_lines(*, wing_roll: bool) -> list[str]:
    """Printed at top of every report. Divergences must be explicit."""
    lines = [
        "===== PARITY CHECKLIST G1..G10 =====",
        (
            "G1 entry 11:00 IST / 2DTE / B25: "
            "backtest/s001_final_validation.py:48-52; "
            "pick_strangle_by_premium income_engine.py:322-356; "
            "ATM target = 0.25*(ATM_C+ATM_P) synthetic (hedge off). "
            "DIVERGENCE: live resolve_strangle_target_premium "
            "(auto_trade_engine.py:279-356) falls back to fixed $150 when "
            "hedge_enabled=False; this engine keeps synthetic ATM×25% to match "
            "locked CFG B25 with hedge off."
        ),
        (
            "G2 Adj B trigger: logic.py:119-210 _try_adj_b_action — "
            "tested leg premium >= 100% baseline AND untested < trigger%×baseline "
            "(NOT single-leg >= trigger%). Default CLI adj-b-trigger=70 "
            "(locked CFG); live DB default 50 (config.py:24)."
        ),
        (
            "G3 select_adj_b_strike: adj_b.py:173-345 — premium STRICTLY < P_target, "
            "then highest premium. Imported live function."
        ),
        (
            "G4 D4 wing filter: adj_b.py:17-40 resolve_adj_b_wing_strike "
            "(status=open AND qty>0); reject call strike>=wing / put<=wing "
            "(adj_b.py:319-334). Imported live helpers."
        ),
        (
            "G5 D4 no strike inside wing: is_adj_b_no_strike_inside_wing "
            "adj_b.py:43-66 → exit ADJ_B_NO_STRIKE_INSIDE_WING "
            "(adjustment.py:576-598). Mirrored in this engine."
        ),
        (
            "G6 max 2 adj: logic.py:1284-1372 _check_max_adjustments_exit; "
            "gate before Adj B (~1036/1091/1138). 3rd trigger → "
            "MAX_ADJUSTMENTS_REACHED force-exit (no 3rd adj placed)."
        ),
        (
            "G7 compute_decrease_step_qty: wing_entry.py:332-353 floor() as-is. "
            "Imported live function (D2 fix NOT applied)."
        ),
        (
            f"G8 wings 2000 pts + wing_roll={'ON' if wing_roll else 'OFF'}: "
            "wing_select.py points mode; models default wing_roll=1. "
            "DIVERGENCE: locked CFG in s001_final_validation.py:57 has "
            "wing_roll_with_short_enabled=0; this engine CLI default --wing-roll on "
            "per task G8."
        ),
        (
            "G9 profit target = entry_cost × k (default 1.0): "
            "s001_final_validation.py:146-157,507-510 (fees+wing debit, not "
            "short credit). Same-day re-entry only after profit_target "
            "(final_validation.py:1101-1119). "
            "DIVERGENCE: live uses THETA/PCT profit engine (hedge_theta.py), "
            "not cost×k."
        ),
        (
            "G10 pre-expiry 17:15 IST: time_utils is_pre_expiry_window; "
            "expiry 17:30 IST, PRE_EXPIRY 15 min (config.py:35). "
            "Priority after TP before adj (logic.py:716-722)."
        ),
        (
            "P_target for Adj B = tested leg CURRENT mark "
            "(adj_b.py:188; adjustment.py:548-554 uses offer). "
            "DIVERGENCE: live P_target from Best Offer (_resolve_offer_price, "
            "never mark); mark engine uses mark close as proxy (no L2). "
            "DIVERGENCE: trigger baseline = entry MARK (not slipped fill) so "
            "sell-side slip does not auto-fire 'pressured' on the next tick; "
            "live stores fill as trigger_baseline_premium."
        ),
        "",
    ]
    return lines


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
@dataclass
class MarkBar:
    open: float
    high: float
    low: float
    close: float


class MarksStore:
    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self.available_months: list[str] = sorted(
            p.stem.replace("marks_", "")
            for p in MARKS_DIR.glob("marks_*.sqlite")
        )

    def conn(self, d: date) -> sqlite3.Connection | None:
        ym = f"{d.year:04d}-{d.month:02d}"
        if ym not in self.available_months:
            return None
        if ym not in self._conns:
            path = MARKS_DIR / f"marks_{ym}.sqlite"
            self._conns[ym] = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro", uri=True
            )
        return self._conns[ym]

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()


def find_spot_csv() -> Path | None:
    files = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    return files[-1] if files else None


def load_spot_map(path: Path) -> dict[int, float]:
    """open_time_unix → close (and open available via separate if needed)."""
    out: dict[int, float] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            out[ts] = float(row["close"])
    return out


def load_spot_ohlc(path: Path) -> dict[int, tuple[float, float, float, float]]:
    out: dict[int, tuple[float, float, float, float]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            out[ts] = (
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
            )
    return out


def ist_dt(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def resolve_slip_frac(premium: float, dte: float, slip_model: str) -> float:
    if slip_model == SLIP_MODEL_FLAT:
        return SLIP_FLAT
    return float(slip_pct(premium, int(max(0, round(dte))))) / 100.0


def sell_fill(mark: float, slip_frac: float) -> float:
    return float(mark) * (1.0 - slip_frac)


def buy_fill(mark: float, slip_frac: float) -> float:
    return float(mark) * (1.0 + slip_frac)


def hours_to_expiry(now_ts: int, expiry: date) -> float:
    exp_dt = ist_dt(expiry, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST)
    return (to_unix(exp_dt) - now_ts) / 3600.0


def is_pre_expiry(now_ts: int, expiry: date) -> bool:
    h = hours_to_expiry(now_ts, expiry)
    return 0.0 < h <= (PRE_EXPIRY_MINUTE_IST / 60.0) or h <= 0.0


# ---------------------------------------------------------------------------
# Marks queries
# ---------------------------------------------------------------------------
def resolve_mark_ts(conn: sqlite3.Connection, expiry: date, ts: int) -> int | None:
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


def load_chain(
    conn: sqlite3.Connection, expiry: date, chain_ts: int, opt_type: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, strike, close FROM marks
        WHERE expiry=? AND ts=? AND opt_type=?
          AND close IS NOT NULL AND close > 0
        """,
        (expiry.isoformat(), chain_ts, opt_type),
    ).fetchall()
    return [
        {
            "symbol": str(s),
            "strike": float(k),
            "mark_price": float(c),
            "mark": float(c),
            "premium": float(c),
            "option_type": opt_type,
        }
        for s, k, c in rows
    ]


def mark_close_at(
    store: MarksStore,
    symbol: str,
    ts: int,
    cache: dict[str, dict[int, float]],
) -> float | None:
    """Nearest mark close within MARK_TOL_SEC."""
    series = cache.get(symbol)
    if series is None:
        series = {}
        d0 = datetime.fromtimestamp(ts - 86400, tz=UTC).date()
        d1 = datetime.fromtimestamp(ts + 86400, tz=UTC).date()
        day = d0
        while day <= d1:
            conn = store.conn(day)
            if conn is not None:
                rows = conn.execute(
                    "SELECT ts, close FROM marks WHERE symbol=? AND ts BETWEEN ? AND ?",
                    (symbol, ts - 2 * 86400, ts + 2 * 86400),
                ).fetchall()
                for t, c in rows:
                    if c is not None and float(c) > 0:
                        series[int(t)] = float(c)
            day += timedelta(days=1)
        cache[symbol] = series
    minute = (ts // 60) * 60
    if minute in series:
        return series[minute]
    best = None
    best_abs = MARK_TOL_SEC + 1
    for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
        if minute + d in series:
            ad = abs(d)
            if ad < best_abs:
                best_abs = ad
                best = series[minute + d]
    return best


def preload_symbol(
    store: MarksStore, symbol: str, ts0: int, ts1: int
) -> dict[int, float]:
    out: dict[int, float] = {}
    d0 = datetime.fromtimestamp(ts0, tz=UTC).date()
    d1 = datetime.fromtimestamp(ts1, tz=UTC).date()
    day = d0
    while day <= d1:
        conn = store.conn(day)
        if conn is not None:
            rows = conn.execute(
                "SELECT ts, close FROM marks WHERE symbol=? AND ts BETWEEN ? AND ?",
                (symbol, ts0, ts1),
            ).fetchall()
            for t, c in rows:
                if c is not None and float(c) > 0:
                    out[int(t)] = float(c)
        day += timedelta(days=1)
    return out


def nearest_strike(strikes: list[float], spot: float) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - spot), k))


def put_call_parity_forward(
    conn: sqlite3.Connection, expiry: date, chain_ts: int
) -> float | None:
    """F = median(K + C - P) over strikes with both call and put marks."""
    calls = {
        float(k): float(c)
        for _, k, c in conn.execute(
            "SELECT symbol, strike, close FROM marks WHERE expiry=? AND ts=? AND opt_type='call' AND close>0",
            (expiry.isoformat(), chain_ts),
        )
    }
    puts = {
        float(k): float(c)
        for _, k, c in conn.execute(
            "SELECT symbol, strike, close FROM marks WHERE expiry=? AND ts=? AND opt_type='put' AND close>0",
            (expiry.isoformat(), chain_ts),
        )
    }
    vals: list[float] = []
    for k, c in calls.items():
        p = puts.get(k)
        if p is None:
            continue
        vals.append(k + c - p)
    if not vals:
        return None
    return float(statistics.median(vals))


def resolve_forward(
    store: MarksStore,
    spot_map: dict[int, float],
    expiry: date,
    ts: int,
) -> tuple[float | None, str]:
    minute = (ts // 60) * 60
    if minute in spot_map:
        return spot_map[minute], "spot_1m"
    # try nearby spot
    for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
        if minute + d in spot_map:
            return spot_map[minute + d], "spot_1m_near"
    conn = store.conn(datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date())
    if conn is None:
        return None, "none"
    cts = resolve_mark_ts(conn, expiry, ts)
    if cts is None:
        return None, "none"
    f = put_call_parity_forward(conn, expiry, cts)
    if f is None or f <= 0:
        return None, "none"
    return f, "put_call_parity"


# ---------------------------------------------------------------------------
# Entry selection
# ---------------------------------------------------------------------------
def pick_atm_straddle_marks(
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    spot: float,
) -> tuple[float, float, float] | None:
    """Return (atm_k, call_prem, put_prem)."""
    c_by = {float(r["strike"]): float(r["mark_price"]) for r in calls}
    p_by = {float(r["strike"]): float(r["mark_price"]) for r in puts}
    common = sorted(set(c_by) & set(p_by))
    atm = nearest_strike(common, spot)
    if atm is None:
        return None
    return atm, c_by[atm], p_by[atm]


def pick_strangle_by_premium_marks(
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    spot: float,
    target: float,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Mirror income_engine.pick_strangle_by_premium on marks (OTM only)."""
    if target <= 0:
        return None
    best_c: tuple[float, dict[str, Any], float] | None = None
    best_p: tuple[float, dict[str, Any], float] | None = None
    for r in calls:
        k = float(r["strike"])
        if k <= spot:
            continue
        prem = float(r["mark_price"])
        if prem <= 0:
            continue
        diff = abs(prem - target)
        if best_c is None or diff < best_c[0] or (diff == best_c[0] and k < best_c[2]):
            best_c = (diff, r, k)
    for r in puts:
        k = float(r["strike"])
        if k >= spot:
            continue
        prem = float(r["mark_price"])
        if prem <= 0:
            continue
        diff = abs(prem - target)
        if best_p is None or diff < best_p[0] or (diff == best_p[0] and k > best_p[2]):
            best_p = (diff, r, k)
    if best_c is None or best_p is None:
        return None
    return best_c[1], best_p[1]


def pick_wing_strikes(
    strikes: list[float], short_call_k: float, short_put_k: float, points: float
) -> tuple[float, float] | None:
    call_target = short_call_k + points
    put_target = short_put_k - points
    at_or_beyond_c = [k for k in strikes if k >= call_target]
    at_or_beyond_p = [k for k in strikes if k <= put_target]
    beyond_c = [k for k in strikes if k > short_call_k]
    beyond_p = [k for k in strikes if k < short_put_k]
    if not beyond_c or not beyond_p:
        return None
    wing_c_k = min(at_or_beyond_c) if at_or_beyond_c else max(beyond_c)
    wing_p_k = max(at_or_beyond_p) if at_or_beyond_p else min(beyond_p)
    if wing_c_k <= short_call_k or wing_p_k >= short_put_k:
        return None
    return wing_c_k, wing_p_k


def find_symbol(
    chain: list[dict[str, Any]], strike: float
) -> dict[str, Any] | None:
    for r in chain:
        if abs(float(r["strike"]) - strike) < 1e-6:
            return r
    return None


# ---------------------------------------------------------------------------
# Position / cycle
# ---------------------------------------------------------------------------
@dataclass
class LegState:
    symbol: str
    strike: float
    opt_type: str  # call|put
    qty: int
    entry_fill: float
    entry_mark: float
    baseline: float
    status: str = "open"  # open|closed
    realized: float = 0.0
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    entry_slip: float = 0.0
    exit_slip: float = 0.0


@dataclass
class AdjEvent:
    ts: int
    leg: str
    old_strike: float
    new_strike: float
    reason: str
    new_qty: int


@dataclass
class CycleResult:
    entry_date: date
    entry_ts: int
    call_strike: float
    put_strike: float
    entry_prem_c: float
    entry_prem_p: float
    qty: int
    wing_c: float
    wing_p: float
    n_adjustments: int
    adj_events: list[AdjEvent]
    exit_ts: int
    exit_reason: str
    hold_hours: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    worst_mtm: float
    applied_slip_entry: float
    applied_slip_exit: float
    spot_source: str


def qty_btc(qty: int) -> float:
    return abs(int(qty)) * CONTRACT_VALUE


def signed_short_upnl(entry_fill: float, mark: float, qty: int) -> float:
    # short: profit when mark drops
    return (entry_fill - mark) * qty_btc(qty)


def signed_long_upnl(entry_fill: float, mark: float, qty: int) -> float:
    return (mark - entry_fill) * qty_btc(qty)


def simulate_cycle(
    store: MarksStore,
    spot_map: dict[int, float],
    *,
    day: date,
    entry_ts: int,
    expiry: date,
    cfg: dict[str, Any],
    slip_model: str,
) -> CycleResult | None:
    """Run one basket from entry_ts until exit. None if cannot enter."""
    if expiry == SKIP_EXPIRY:
        logger.info("skip expiry %s", expiry)
        return None

    conn = store.conn(day)
    if conn is None:
        return None
    cts = resolve_mark_ts(conn, expiry, entry_ts)
    if cts is None:
        return None

    spot, spot_src = resolve_forward(store, spot_map, expiry, entry_ts)
    logger.info(
        "cycle entry day=%s ts=%s expiry=%s forward_source=%s forward=%s",
        day,
        entry_ts,
        expiry,
        spot_src,
        spot,
    )
    if spot is None or spot <= 0:
        return None

    calls = load_chain(conn, expiry, cts, "call")
    puts = load_chain(conn, expiry, cts, "put")
    if not calls or not puts:
        return None

    atm = pick_atm_straddle_marks(calls, puts, spot)
    if atm is None:
        return None
    _atm_k, atm_c, atm_p = atm
    atm_straddle = atm_c + atm_p
    pct = float(cfg["premium_pct_of_hedge"])
    target = (pct / 100.0) * atm_straddle
    picked = pick_strangle_by_premium_marks(calls, puts, spot, target)
    if picked is None:
        return None
    call_row, put_row = picked

    all_strikes = sorted(
        {float(r["strike"]) for r in calls} | {float(r["strike"]) for r in puts}
    )
    wings = pick_wing_strikes(
        all_strikes,
        float(call_row["strike"]),
        float(put_row["strike"]),
        float(cfg["wing_points"]),
    )
    if wings is None:
        return None
    wing_c_k, wing_p_k = wings
    wing_c_row = find_symbol(calls, wing_c_k)
    wing_p_row = find_symbol(puts, wing_p_k)
    if wing_c_row is None or wing_p_row is None:
        return None

    dte_entry = max(0.0, hours_to_expiry(entry_ts, expiry) / 24.0)
    qty0 = int(cfg["qty_lots"])

    def _enter_short(row: dict[str, Any]) -> LegState:
        m = float(row["mark_price"])
        sf = resolve_slip_frac(m, dte_entry, slip_model)
        fill = sell_fill(m, sf)
        fee = eng.option_fee(fill, spot, qty0)
        return LegState(
            symbol=str(row["symbol"]),
            strike=float(row["strike"]),
            opt_type=str(row["option_type"]),
            qty=qty0,
            entry_fill=fill,
            entry_mark=m,
            # Trigger baseline = entry MARK (not slipped fill). Using fill would
            # make mark>=baseline true on the next tick for every short sell.
            baseline=m,
            entry_fee=fee,
            entry_slip=sf * 100.0,
        )

    def _enter_long(row: dict[str, Any]) -> LegState:
        m = float(row["mark_price"])
        sf = resolve_slip_frac(m, dte_entry, slip_model)
        fill = buy_fill(m, sf)
        fee = eng.option_fee(fill, spot, qty0)
        return LegState(
            symbol=str(row["symbol"]),
            strike=float(row["strike"]),
            opt_type=str(row["option_type"]),
            qty=qty0,
            entry_fill=fill,
            entry_mark=m,
            baseline=m,
            entry_fee=fee,
            entry_slip=sf * 100.0,
        )

    call_leg = _enter_short(call_row)
    put_leg = _enter_short(put_row)
    wing_c = _enter_long(wing_c_row)
    wing_p = _enter_long(wing_p_row)

    entry_cost = (
        call_leg.entry_fee
        + put_leg.entry_fee
        + wing_c.entry_fee
        + wing_p.entry_fee
        + (wing_c.entry_fill + wing_p.entry_fill) * qty_btc(qty0)
    )
    profit_target = entry_cost * float(cfg["profit_k"])

    # Preload marks through expiry
    exp_ts = to_unix(ist_dt(expiry, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))
    series: dict[str, dict[int, float]] = {
        call_leg.symbol: preload_symbol(store, call_leg.symbol, entry_ts, exp_ts),
        put_leg.symbol: preload_symbol(store, put_leg.symbol, entry_ts, exp_ts),
        wing_c.symbol: preload_symbol(store, wing_c.symbol, entry_ts, exp_ts),
        wing_p.symbol: preload_symbol(store, wing_p.symbol, entry_ts, exp_ts),
    }

    def mark_of(leg: LegState, ts: int) -> float | None:
        s = series.get(leg.symbol) or {}
        minute = (ts // 60) * 60
        if minute in s:
            return s[minute]
        for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
            if minute + d in s:
                return s[minute + d]
        return None

    adj_count = 0
    adj_events: list[AdjEvent] = []
    worst_mtm = 0.0
    exit_reason = ""
    exit_ts = entry_ts
    slip_exit_samples: list[float] = []

    ts = entry_ts + MONITOR_STEP_SEC
    while ts <= exp_ts:
        # refresh forward for adj strike selection
        fwd, _src = resolve_forward(store, spot_map, expiry, ts)
        if fwd is None:
            fwd = spot

        mc = mark_of(call_leg, ts) if call_leg.status == "open" else None
        mp = mark_of(put_leg, ts) if put_leg.status == "open" else None
        mwc = mark_of(wing_c, ts) if wing_c.status == "open" else None
        mwp = mark_of(wing_p, ts) if wing_p.status == "open" else None

        # If any open short missing mark, skip tick
        if call_leg.status == "open" and mc is None:
            ts += MONITOR_STEP_SEC
            continue
        if put_leg.status == "open" and mp is None:
            ts += MONITOR_STEP_SEC
            continue

        # MTM (marks, no slip)
        mtm = call_leg.realized + put_leg.realized + wing_c.realized + wing_p.realized
        fees_so_far = (
            call_leg.entry_fee
            + put_leg.entry_fee
            + wing_c.entry_fee
            + wing_p.entry_fee
            + call_leg.exit_fee
            + put_leg.exit_fee
            + wing_c.exit_fee
            + wing_p.exit_fee
        )
        if call_leg.status == "open" and mc is not None:
            mtm += signed_short_upnl(call_leg.entry_fill, mc, call_leg.qty)
        if put_leg.status == "open" and mp is not None:
            mtm += signed_short_upnl(put_leg.entry_fill, mp, put_leg.qty)
        if wing_c.status == "open" and mwc is not None:
            mtm += signed_long_upnl(wing_c.entry_fill, mwc, wing_c.qty)
        if wing_p.status == "open" and mwp is not None:
            mtm += signed_long_upnl(wing_p.entry_fill, mwp, wing_p.qty)
        net_mtm = mtm - fees_so_far
        worst_mtm = min(worst_mtm, net_mtm)

        # --- exits priority: TP → pre-expiry → max-adj gate on trigger → Adj B ---
        if net_mtm >= profit_target:
            exit_reason = "PROFIT_TARGET"
            exit_ts = ts
            break

        if is_pre_expiry(ts, expiry):
            exit_reason = "PRE_EXPIRY"
            exit_ts = ts
            break

        # Adj B only when both shorts open
        if (
            str(cfg["adj_mode"]).upper() in ("B_ONLY", "BOTH", "B")
            and call_leg.status == "open"
            and put_leg.status == "open"
            and mc is not None
            and mp is not None
        ):
            trig = float(cfg["adj_b_trigger"]) / 100.0
            call_pressured = call_leg.baseline > 0 and mc >= call_leg.baseline * 1.0
            put_pressured = put_leg.baseline > 0 and mp >= put_leg.baseline * 1.0
            call_decayed = call_leg.baseline > 0 and mc < call_leg.baseline * trig
            put_decayed = put_leg.baseline > 0 and mp < put_leg.baseline * trig

            tested: str | None = None
            untested: str | None = None
            if call_pressured and put_decayed:
                tested, untested = "call", "put"
            elif put_pressured and call_decayed:
                tested, untested = "put", "call"

            if tested is not None and untested is not None:
                # G6 max adj gate BEFORE placing adj
                max_adj = int(cfg["max_adj"])
                if adj_count >= max_adj:
                    exit_reason = "MAX_ADJUSTMENTS_REACHED"
                    exit_ts = ts
                    break

                # decrease step qty for this upcoming adj number
                adj_n = adj_count + 1
                new_qty, close_basket = compute_decrease_step_qty(
                    original_qty=qty0,
                    adjustment_number=adj_n,
                    decrease_pct=float(cfg["dec_pct"]),
                )
                if close_basket or new_qty is None:
                    exit_reason = "QTY_DECREASE_EXHAUSTED"
                    exit_ts = ts
                    break

                tested_prem = mc if tested == "call" else mp
                p_target = float(tested_prem)
                untested_leg = put_leg if untested == "put" else call_leg
                other_leg = call_leg if untested == "put" else put_leg

                # wing strike filter
                wing_leg_obj = wing_c if untested == "call" else wing_p

                class _WingProxy:
                    pass

                wp = _WingProxy()
                wp.status = wing_leg_obj.status
                wp.quantity = wing_leg_obj.qty
                wp.strike = wing_leg_obj.strike
                wing_k = resolve_adj_b_wing_strike(wp)

                # chain for untested type at this ts
                day_ist = datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date()
                conn_t = store.conn(day_ist) or conn
                cts_t = resolve_mark_ts(conn_t, expiry, ts)
                if cts_t is None:
                    ts += MONITOR_STEP_SEC
                    continue
                chain = load_chain(conn_t, expiry, cts_t, untested)
                res = select_adj_b_strike(
                    leg_type=untested,
                    p_target=p_target,
                    chain=chain,
                    spot=float(fwd),
                    other_short_strike=float(other_leg.strike),
                    wing_strike=wing_k,
                )
                if is_adj_b_no_strike_inside_wing(res, wing_k):
                    exit_reason = "ADJ_B_NO_STRIKE_INSIDE_WING"
                    exit_ts = ts
                    break
                if not res.success or res.strike is None:
                    ts += MONITOR_STEP_SEC
                    continue

                new_strike = float(res.strike)
                new_row = find_symbol(chain, new_strike)
                if new_row is None:
                    ts += MONITOR_STEP_SEC
                    continue

                # Close old untested short
                dte_now = max(0.0, hours_to_expiry(ts, expiry) / 24.0)
                old_mark = mp if untested == "put" else mc
                assert old_mark is not None
                sf_ex = resolve_slip_frac(old_mark, dte_now, slip_model)
                ex_fill = buy_fill(old_mark, sf_ex)
                fee_ex = eng.option_fee(ex_fill, float(fwd), untested_leg.qty)
                slip_exit_samples.append(sf_ex * 100.0)
                realized = signed_short_upnl(
                    untested_leg.entry_fill, ex_fill, untested_leg.qty
                )
                untested_leg.realized += realized
                untested_leg.exit_fee += fee_ex
                untested_leg.exit_slip = sf_ex * 100.0
                untested_leg.status = "closed"

                # Wing roll if enabled and new short crosses wing
                if bool(cfg["wing_roll"]) and wing_k is not None:
                    crosses = (
                        (untested == "call" and new_strike >= wing_k - 1e-9)
                        or (untested == "put" and new_strike <= wing_k + 1e-9)
                    )
                    if crosses:
                        # close old wing, open new at short ± points
                        wleg = wing_c if untested == "call" else wing_p
                        wm = mark_of(wleg, ts)
                        if wm is not None and wleg.status == "open":
                            sf_w = resolve_slip_frac(wm, dte_now, slip_model)
                            w_ex = sell_fill(wm, sf_w)  # sell long wing
                            fee_w = eng.option_fee(w_ex, float(fwd), wleg.qty)
                            wleg.realized += signed_long_upnl(
                                wleg.entry_fill, w_ex, wleg.qty
                            )
                            wleg.exit_fee += fee_w
                            wleg.status = "closed"
                            # new wing strike
                            if untested == "call":
                                nk = new_strike + float(cfg["wing_points"])
                                cand = [k for k in all_strikes if k >= nk]
                                nw = min(cand) if cand else None
                                ch = load_chain(conn_t, expiry, cts_t, "call")
                            else:
                                nk = new_strike - float(cfg["wing_points"])
                                cand = [k for k in all_strikes if k <= nk]
                                nw = max(cand) if cand else None
                                ch = load_chain(conn_t, expiry, cts_t, "put")
                            if nw is not None:
                                nrow = find_symbol(ch, nw)
                                if nrow is not None:
                                    nm = float(nrow["mark_price"])
                                    sf_n = resolve_slip_frac(nm, dte_now, slip_model)
                                    nfill = buy_fill(nm, sf_n)
                                    nfee = eng.option_fee(nfill, float(fwd), int(new_qty))
                                    new_wing = LegState(
                                        symbol=str(nrow["symbol"]),
                                        strike=float(nrow["strike"]),
                                        opt_type=untested,
                                        qty=int(new_qty),
                                        entry_fill=nfill,
                                        entry_mark=nm,
                                        baseline=nfill,
                                        entry_fee=nfee,
                                        entry_slip=sf_n * 100.0,
                                    )
                                    series[new_wing.symbol] = preload_symbol(
                                        store, new_wing.symbol, ts, exp_ts
                                    )
                                    if untested == "call":
                                        wing_c = new_wing
                                    else:
                                        wing_p = new_wing

                # Enter new untested short
                nm = float(new_row["mark_price"])
                sf_in = resolve_slip_frac(nm, dte_now, slip_model)
                nfill = sell_fill(nm, sf_in)
                nfee = eng.option_fee(nfill, float(fwd), int(new_qty))
                new_leg = LegState(
                    symbol=str(new_row["symbol"]),
                    strike=new_strike,
                    opt_type=untested,
                    qty=int(new_qty),
                    entry_fill=nfill,
                    entry_mark=nm,
                    baseline=nm,  # mark baseline for trigger parity under slip
                    entry_fee=nfee,
                    entry_slip=sf_in * 100.0,
                )
                series[new_leg.symbol] = preload_symbol(
                    store, new_leg.symbol, ts, exp_ts
                )

                # Resize other short + wings qty to new_qty (decrease step)
                other_leg.qty = int(new_qty)
                if wing_c.status == "open":
                    wing_c.qty = int(new_qty)
                if wing_p.status == "open":
                    wing_p.qty = int(new_qty)

                # Reset other leg baseline to new short's entry MARK
                other_leg.baseline = float(nm)

                if untested == "call":
                    call_leg = new_leg
                else:
                    put_leg = new_leg

                adj_count += 1
                adj_events.append(
                    AdjEvent(
                        ts=ts,
                        leg=untested,
                        old_strike=float(untested_leg.strike),
                        new_strike=new_strike,
                        reason="ADJ_B",
                        new_qty=int(new_qty),
                    )
                )
                logger.info(
                    "adj_b #%d untested=%s %.0f->%.0f qty=%d",
                    adj_count,
                    untested,
                    untested_leg.strike,
                    new_strike,
                    new_qty,
                )

        ts += MONITOR_STEP_SEC
    else:
        exit_reason = exit_reason or "EXPIRY"
        exit_ts = min(ts, exp_ts)

    # Close all open legs at exit_ts
    def close_all(at: int) -> None:
        nonlocal call_leg, put_leg, wing_c, wing_p
        fwd, _ = resolve_forward(store, spot_map, expiry, at)
        if fwd is None:
            fwd = spot
        dte_now = max(0.0, hours_to_expiry(at, expiry) / 24.0)
        for leg, is_short in (
            (call_leg, True),
            (put_leg, True),
            (wing_c, False),
            (wing_p, False),
        ):
            if leg.status != "open":
                continue
            m = mark_of(leg, at)
            if m is None:
                # last known
                s = series.get(leg.symbol) or {}
                m = s[max(s)] if s else leg.entry_mark
            sf = resolve_slip_frac(m, dte_now, slip_model)
            slip_exit_samples.append(sf * 100.0)
            if is_short:
                fill = buy_fill(m, sf)
                leg.realized += signed_short_upnl(leg.entry_fill, fill, leg.qty)
            else:
                fill = sell_fill(m, sf)
                leg.realized += signed_long_upnl(leg.entry_fill, fill, leg.qty)
            fee = eng.option_fee(fill, float(fwd), leg.qty)
            leg.exit_fee += fee
            leg.exit_slip = sf * 100.0
            leg.status = "closed"

    close_all(exit_ts)

    fees = (
        call_leg.entry_fee
        + put_leg.entry_fee
        + wing_c.entry_fee
        + wing_p.entry_fee
        + call_leg.exit_fee
        + put_leg.exit_fee
        + wing_c.exit_fee
        + wing_p.exit_fee
    )
    gross = (
        call_leg.realized
        + put_leg.realized
        + wing_c.realized
        + wing_p.realized
    )
    # slippage cost ≈ |fill-mark| * qty_btc aggregated at entries/exits
    slip_cost = 0.0
    for leg, is_short in (
        (call_leg, True),
        (put_leg, True),
        (wing_c, False),
        (wing_p, False),
    ):
        # entry
        if is_short:
            slip_cost += abs(leg.entry_mark - leg.entry_fill) * qty_btc(qty0)
        else:
            slip_cost += abs(leg.entry_fill - leg.entry_mark) * qty_btc(qty0)

    net = gross - fees
    hold_h = max(0.0, (exit_ts - entry_ts) / 3600.0)
    entry_slips = [
        call_leg.entry_slip,
        put_leg.entry_slip,
        wing_c.entry_slip,
        wing_p.entry_slip,
    ]
    avg_entry_slip = float(statistics.fmean(entry_slips)) if entry_slips else 0.0
    avg_exit_slip = (
        float(statistics.fmean(slip_exit_samples)) if slip_exit_samples else 0.0
    )

    return CycleResult(
        entry_date=day,
        entry_ts=entry_ts,
        call_strike=float(call_row["strike"]),
        put_strike=float(put_row["strike"]),
        entry_prem_c=float(call_row["mark_price"]),
        entry_prem_p=float(put_row["mark_price"]),
        qty=qty0,
        wing_c=wing_c_k,
        wing_p=wing_p_k,
        n_adjustments=adj_count,
        adj_events=adj_events,
        exit_ts=exit_ts,
        exit_reason=exit_reason,
        hold_hours=hold_h,
        gross_pnl=gross,
        fees=fees,
        slippage_cost=slip_cost,
        net_pnl=net,
        worst_mtm=worst_mtm,
        applied_slip_entry=avg_entry_slip,
        applied_slip_exit=avg_exit_slip,
        spot_source=spot_src,
    )


# ---------------------------------------------------------------------------
# Runner / stats
# ---------------------------------------------------------------------------
def day_clustered_ci(
    cycles: list[CycleResult], n: int, seed: int
) -> tuple[float, float, float]:
    by_day: dict[date, list[float]] = defaultdict(list)
    for c in cycles:
        by_day[c.entry_date].append(c.net_pnl)
    days = sorted(by_day)
    if not days:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n):
        sample: list[float] = []
        for _d in days:
            day = days[rng.randrange(len(days))]
            sample.extend(by_day[day])
        if sample:
            means.append(float(statistics.fmean(sample)))
    if not means:
        return float("nan"), float("nan"), float("nan")
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return float(statistics.fmean(means)), float(lo), float(hi)


def max_drawdown(daily: list[float]) -> float:
    peak = 0.0
    eq = 0.0
    dd = 0.0
    for x in daily:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return float(dd)


def run(cfg: dict[str, Any]) -> tuple[list[str], list[CycleResult]]:
    lines: list[str] = []
    lines.extend(parity_checklist_lines(wing_roll=bool(cfg["wing_roll"])))

    if cfg["slip_model"] == SLIP_MODEL_BUCKETED:
        load_slip_table()

    lines.append("===== S001 MARK ENGINE =====")
    lines.append(f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    lines.append(f"config={cfg}")
    lines.append(f"window={cfg['from_date']} .. {cfg['to_date']}")
    lines.append("")

    spot_path = find_spot_csv()
    if spot_path is None:
        lines.append("ERROR: no BTCUSD_1m_*.csv")
        return lines, []
    spot_map = load_spot_map(spot_path)
    lines.append(f"spot_csv={spot_path.name} bars={len(spot_map)}")

    store = MarksStore()
    lines.append(f"marks_months={store.available_months}")

    d0: date = cfg["from_date"]
    d1: date = cfg["to_date"]
    cycles: list[CycleResult] = []

    day = d0
    while day <= d1:
        entries_today = 0
        entry_hh = int(cfg["entry_hour"])
        entry_mm = int(cfg["entry_minute"])
        entry_ts = to_unix(ist_dt(day, entry_hh, entry_mm))
        expiry = day + timedelta(days=int(cfg["dte"]))

        pending_ts: int | None = entry_ts
        while pending_ts is not None and entries_today < MAX_ENTRIES_PER_DAY:
            cyc = simulate_cycle(
                store,
                spot_map,
                day=day,
                entry_ts=pending_ts,
                expiry=expiry,
                cfg=cfg,
                slip_model=str(cfg["slip_model"]),
            )
            pending_ts = None
            if cyc is None:
                break
            cycles.append(cyc)
            entries_today += 1
            logger.info(
                "day=%s cycle#%d exit=%s net=%.4f adj=%d",
                day,
                entries_today,
                cyc.exit_reason,
                cyc.net_pnl,
                cyc.n_adjustments,
            )
            # same-day reentry only after profit target
            if cyc.exit_reason != "PROFIT_TARGET":
                break
            re_ts = cyc.exit_ts + MONITOR_STEP_SEC
            re_ist = datetime.fromtimestamp(re_ts, tz=UTC).astimezone(IST)
            if re_ist.date() != day:
                break
            pending_ts = re_ts

        day += timedelta(days=1)

    store.close()

    # --- summary ---
    n = len(cycles)
    lines.append(f"n_cycles={n}")
    if n == 0:
        lines.append("No cycles completed.")
        return lines, cycles

    nets = [c.net_pnl for c in cycles]
    by_day: dict[date, float] = defaultdict(float)
    for c in cycles:
        by_day[c.entry_date] += c.net_pnl
    day_span = (d1 - d0).days + 1
    daily = [by_day.get(d0 + timedelta(days=i), 0.0) for i in range(day_span)]
    mean_day = float(statistics.fmean(daily)) if daily else float("nan")
    median_net = float(statistics.median(nets))
    mean_p, lo, hi = day_clustered_ci(cycles, BOOTSTRAP_N, BOOTSTRAP_SEED)
    worst = min(cycles, key=lambda c: c.net_pnl)
    adj_per = float(statistics.fmean([c.n_adjustments for c in cycles]))
    reason_counts: dict[str, int] = defaultdict(int)
    for c in cycles:
        reason_counts[c.exit_reason] += 1
    holds = [c.hold_hours for c in cycles]
    fees_total = sum(c.fees for c in cycles)
    slip_total = sum(c.slippage_cost for c in cycles)
    fees_per_day = fees_total / day_span
    slip_per_day = slip_total / day_span

    lines.append(f"mean_net_per_day={mean_day:.6f}")
    lines.append(f"median_cycle_net={median_net:.6f}")
    lines.append(
        f"bootstrap_mean_cycle={mean_p:.6f} 95%CI=[{lo:.6f},{hi:.6f}] "
        f"n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED}"
    )
    lines.append(
        f"worst_cycle date={worst.entry_date} net={worst.net_pnl:.6f} "
        f"reason={worst.exit_reason}"
    )
    lines.append(f"max_drawdown_daily_equity={max_drawdown(daily):.6f}")
    lines.append(f"adj_per_cycle={adj_per:.4f}")
    mix = ", ".join(
        f"{k}={100.0 * v / n:.1f}%" for k, v in sorted(reason_counts.items())
    )
    lines.append(f"exit_reason_mix%={mix}")
    lines.append(
        f"hold_hours mean={statistics.fmean(holds):.2f} "
        f"median={statistics.median(holds):.2f} "
        f"min={min(holds):.2f} max={max(holds):.2f}"
    )
    lines.append(f"fees_per_day={fees_per_day:.6f}")
    lines.append(f"slippage_cost_per_day={slip_per_day:.6f}")
    lines.append("")
    return lines, cycles


def write_cycles_csv(cycles: list[CycleResult]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "date",
        "entry_ts",
        "call_strike",
        "put_strike",
        "entry_premium_c",
        "entry_premium_p",
        "qty",
        "wing_c",
        "wing_p",
        "n_adjustments",
        "adj_times_strikes_reasons",
        "exit_ts",
        "exit_reason",
        "hold_hours",
        "gross_pnl",
        "fees",
        "slippage_cost",
        "net_pnl",
        "worst_intracycle_mtm",
        "applied_slip_pct_entry",
        "applied_slip_pct_exit",
        "spot_source",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in cycles:
            adj_s = ";".join(
                f"{a.ts}:{a.leg}:{a.old_strike:.0f}->{a.new_strike:.0f}:{a.reason}:q{a.new_qty}"
                for a in c.adj_events
            )
            w.writerow(
                {
                    "date": c.entry_date.isoformat(),
                    "entry_ts": c.entry_ts,
                    "call_strike": c.call_strike,
                    "put_strike": c.put_strike,
                    "entry_premium_c": c.entry_prem_c,
                    "entry_premium_p": c.entry_prem_p,
                    "qty": c.qty,
                    "wing_c": c.wing_c,
                    "wing_p": c.wing_p,
                    "n_adjustments": c.n_adjustments,
                    "adj_times_strikes_reasons": adj_s,
                    "exit_ts": c.exit_ts,
                    "exit_reason": c.exit_reason,
                    "hold_hours": f"{c.hold_hours:.4f}",
                    "gross_pnl": f"{c.gross_pnl:.6f}",
                    "fees": f"{c.fees:.6f}",
                    "slippage_cost": f"{c.slippage_cost:.6f}",
                    "net_pnl": f"{c.net_pnl:.6f}",
                    "worst_intracycle_mtm": f"{c.worst_mtm:.6f}",
                    "applied_slip_pct_entry": f"{c.applied_slip_entry:.4f}",
                    "applied_slip_pct_exit": f"{c.applied_slip_exit:.4f}",
                    "spot_source": c.spot_source,
                }
            )
    logger.info("wrote %s (%d rows)", OUT_CSV, len(cycles))


def parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().replace("IST", "").strip().split(":")
    return int(parts[0]), int(parts[1])


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S001 mark-data backtest engine")
    ap.add_argument("--dec-pct", type=float, default=40.0)
    ap.add_argument("--adj-b-trigger", type=float, default=70.0)
    ap.add_argument("--adj-mode", type=str, default="B_only")
    ap.add_argument("--hedge", type=str, default="off", choices=("off", "on"))
    ap.add_argument("--profit-k", type=float, default=1.0)
    ap.add_argument("--wing-points", type=float, default=2000.0)
    ap.add_argument("--wing-roll", type=str, default="on", choices=("on", "off"))
    ap.add_argument("--qty-lots", type=int, default=8)
    ap.add_argument("--entry-time", type=str, default="11:00")
    ap.add_argument("--dte", type=int, default=2)
    ap.add_argument("--premium-pct-of-hedge", type=float, default=25.0)
    ap.add_argument("--max-adj", type=int, default=2)
    ap.add_argument("--from", dest="from_date", type=str, required=True)
    ap.add_argument("--to", dest="to_date", type=str, required=True)
    ap.add_argument(
        "--slip-model",
        type=str,
        default=SLIP_MODEL_BUCKETED,
        choices=(SLIP_MODEL_BUCKETED, SLIP_MODEL_FLAT),
    )
    args = ap.parse_args()

    eh, em = parse_hhmm(args.entry_time)
    cfg: dict[str, Any] = {
        "dec_pct": float(args.dec_pct),
        "adj_b_trigger": float(args.adj_b_trigger),
        "adj_mode": str(args.adj_mode),
        "hedge": str(args.hedge),
        "profit_k": float(args.profit_k),
        "wing_points": float(args.wing_points),
        "wing_roll": args.wing_roll == "on",
        "qty_lots": int(args.qty_lots),
        "entry_hour": eh,
        "entry_minute": em,
        "dte": int(args.dte),
        "premium_pct_of_hedge": float(args.premium_pct_of_hedge),
        "max_adj": int(args.max_adj),
        "from_date": date.fromisoformat(args.from_date),
        "to_date": date.fromisoformat(args.to_date),
        "slip_model": str(args.slip_model),
    }

    t0 = time.time()
    lines, cycles = run(cfg)
    elapsed = time.time() - t0
    lines.append(f"elapsed_sec={elapsed:.1f}")
    write_cycles_csv(cycles)
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("wrote %s", OUT_TXT)
    logger.info(
        "SMOKE n_cycles=%d elapsed=%.1fs",
        len(cycles),
        elapsed,
    )


if __name__ == "__main__":
    main()
