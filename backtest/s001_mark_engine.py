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
            "G1 premium selection: --premium-mode fixed|b25 (default fixed). "
            "fixed = --target-premium-per-side (default 150) matching live "
            "auto_trade_engine.py:279-356 _fallback('hedge_disabled') when hedge off. "
            "b25 = ATM straddle mark × --premium-pct-of-hedge% (synthetic ATM×25%)."
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
            "G9 profit target: --profit-mode pct_of_credit|cost_k|none "
            "(default pct_of_credit). pct_of_credit locks at entry: "
            "NET=(short credit − wing debit − entry fees)×tp_pct/100 "
            "(hedge_theta.py:449-505 PCT). cost_k = entry_cost×k (legacy). "
            "none = no TP. Locked once at entry — not recomputed each tick."
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


def resolve_slip_frac(
    premium: float,
    dte: float,
    slip_model: str,
    slip_mult: float = 1.0,
) -> float:
    if slip_model == SLIP_MODEL_FLAT:
        base = SLIP_FLAT
    else:
        base = float(slip_pct(premium, int(max(0, round(dte))))) / 100.0
    return max(0.0, float(base) * float(slip_mult))


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


def lock_profit_target_usd(
    *,
    call_fill: float,
    put_fill: float,
    wing_c_fill: float,
    wing_p_fill: float,
    entry_fees: float,
    qty: int,
    cfg: dict[str, Any],
) -> float | None:
    """
    Lock profit target at entry (not recomputed each tick).

    pct_of_credit (live PCT):
      net_credit = (short fills − wing fills) × qty × CV
      NET = net_credit − entry_fees   # task: fees in NET
      target = max(0, NET) × tp_pct / 100
      Mirror hedge_theta.py:449-505 PCT multiply; fees included per task KAAM 2.

    cost_k: entry_cost × k (legacy compare)
    none: no TP
    """
    mode = str(cfg.get("profit_mode") or "pct_of_credit").lower()
    if mode in ("none", "off", ""):
        return None
    if mode == "cost_k":
        wing_debit = (wing_c_fill + wing_p_fill) * qty_btc(qty)
        entry_cost = entry_fees + wing_debit
        return max(0.0, entry_cost * float(cfg.get("profit_k") or 1.0))
    # pct_of_credit
    short_credit = (call_fill + put_fill) * qty_btc(qty)
    wing_debit = (wing_c_fill + wing_p_fill) * qty_btc(qty)
    net_credit = short_credit - wing_debit
    net_after_fees = net_credit - float(entry_fees)
    tp_pct = float(cfg.get("tp_pct") or 50.0)
    return max(0.0, net_after_fees * tp_pct / 100.0)


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
    premium_mode: str = ""
    target_premium: float = 0.0
    profit_mode: str = ""
    profit_target_usd: float = 0.0
    avg_applied_slip_pct: float = 0.0
    arm: str = ""
    window: str = ""
    tp_touched: bool = False
    exit_mtm: float = 0.0
    hours_to_adj1: float | None = None
    hours_to_adj2: float | None = None


SKIP_NO_CHAIN = "skipped_no_chain"
SKIP_NO_STRIKE = "skipped_no_strike_at_target"
SKIP_NO_WING = "skipped_no_wing"
SKIP_NO_MARK = "skipped_no_mark"
SKIP_OTHER = "skipped_other"
SKIP_REASONS = (
    SKIP_NO_CHAIN,
    SKIP_NO_STRIKE,
    SKIP_NO_WING,
    SKIP_NO_MARK,
    SKIP_OTHER,
)


@dataclass
class SkipAccount:
    days_in_window: int = 0
    cycles_entered: int = 0
    skipped_no_chain: int = 0
    skipped_no_strike_at_target: int = 0
    skipped_no_wing: int = 0
    skipped_no_mark: int = 0
    skipped_other: int = 0
    examples: dict[str, list[str]] = field(default_factory=dict)

    def record_skip(self, reason: str, day: date) -> None:
        if reason not in SKIP_REASONS:
            reason = SKIP_OTHER
        cur = int(getattr(self, reason, 0))
        setattr(self, reason, cur + 1)
        ex = self.examples.setdefault(reason, [])
        ds = day.isoformat()
        if ds not in ex and len(ex) < 5:
            ex.append(ds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "days_in_window": self.days_in_window,
            "cycles_entered": self.cycles_entered,
            "skipped_no_chain": self.skipped_no_chain,
            "skipped_no_strike_at_target": self.skipped_no_strike_at_target,
            "skipped_no_wing": self.skipped_no_wing,
            "skipped_no_mark": self.skipped_no_mark,
            "skipped_other": self.skipped_other,
            "skip_examples": {k: list(v) for k, v in self.examples.items()},
        }


def arm_label(cfg: dict[str, Any]) -> str:
    if cfg.get("arm_name"):
        return str(cfg["arm_name"])
    pm = str(cfg.get("premium_mode") or "fixed")
    tp = float(cfg.get("tp_pct") or 0.0)
    sm = float(cfg.get("slip_mult", 1.0))
    return f"{pm}_tp{tp:.0f}_slip{sm:g}"


def baseline_old_live_cfg(base_cfg: dict[str, Any]) -> dict[str, Any]:
    """Purana live config arm for P4 paired comparison."""
    c = dict(base_cfg)
    c.update(
        {
            "arm_name": "BASELINE_OLD_LIVE",
            "adj_mode": "BOTH",
            "adj_b_trigger": 90.0,
            "dec_pct": 20.0,
            "hedge": "off",
            "profit_mode": "none",
            "premium_mode": "fixed",
            "target_premium_per_side": 150.0,
            "wing_points": 2000.0,
            "wing_roll": True,
            "qty_lots": 8,
            "slip_mult": 1.0,
            "tp_pct": 0.0,
        }
    )
    return c


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
) -> tuple[CycleResult | None, str | None]:
    """
    Run one basket from entry_ts until exit.
    Returns (CycleResult, None) on entry, or (None, skip_reason) if cannot enter.
    """
    if expiry == SKIP_EXPIRY:
        logger.info("skip expiry %s", expiry)
        return None, SKIP_OTHER

    conn = store.conn(day)
    if conn is None:
        return None, SKIP_NO_MARK
    cts = resolve_mark_ts(conn, expiry, entry_ts)
    if cts is None:
        return None, SKIP_NO_MARK

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
        return None, SKIP_OTHER

    calls = load_chain(conn, expiry, cts, "call")
    puts = load_chain(conn, expiry, cts, "put")
    if not calls or not puts:
        return None, SKIP_NO_CHAIN

    atm = pick_atm_straddle_marks(calls, puts, spot)
    if atm is None:
        return None, SKIP_NO_CHAIN
    _atm_k, atm_c, atm_p = atm
    atm_straddle = atm_c + atm_p
    premium_mode = str(cfg.get("premium_mode") or "fixed").lower()
    if premium_mode == "b25":
        pct = float(cfg["premium_pct_of_hedge"])
        target_prem = (pct / 100.0) * atm_straddle
    else:
        # fixed — live hedge_disabled fallback (auto_trade_engine.py:279-356)
        premium_mode = "fixed"
        target_prem = float(cfg.get("target_premium_per_side") or 150.0)
    logger.info(
        "premium_mode=%s target_premium_per_side=%.4f atm_straddle=%.4f",
        premium_mode,
        target_prem,
        atm_straddle,
    )
    picked = pick_strangle_by_premium_marks(calls, puts, spot, target_prem)
    if picked is None:
        return None, SKIP_NO_STRIKE
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
        return None, SKIP_NO_WING
    wing_c_k, wing_p_k = wings
    wing_c_row = find_symbol(calls, wing_c_k)
    wing_p_row = find_symbol(puts, wing_p_k)
    if wing_c_row is None or wing_p_row is None:
        return None, SKIP_NO_WING

    dte_entry = max(0.0, hours_to_expiry(entry_ts, expiry) / 24.0)
    qty0 = int(cfg["qty_lots"])
    slip_model = str(cfg.get("slip_model") or slip_model)
    slip_mult = float(cfg.get("slip_mult", 1.0))

    def _enter_short(row: dict[str, Any]) -> LegState:
        m = float(row["mark_price"])
        sf = resolve_slip_frac(m, dte_entry, slip_model, slip_mult)
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
        sf = resolve_slip_frac(m, dte_entry, slip_model, slip_mult)
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

    entry_fees = (
        call_leg.entry_fee
        + put_leg.entry_fee
        + wing_c.entry_fee
        + wing_p.entry_fee
    )
    profit_mode = str(cfg.get("profit_mode") or "pct_of_credit").lower()
    profit_target = lock_profit_target_usd(
        call_fill=call_leg.entry_fill,
        put_fill=put_leg.entry_fill,
        wing_c_fill=wing_c.entry_fill,
        wing_p_fill=wing_p.entry_fill,
        entry_fees=entry_fees,
        qty=qty0,
        cfg=cfg,
    )
    logger.info(
        "profit_mode=%s locked_profit_target_usd=%s target_premium=%.4f",
        profit_mode,
        profit_target,
        target_prem,
    )

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
    exit_mtm = 0.0
    tp_touched = False
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
        if profit_target is not None and net_mtm >= profit_target:
            tp_touched = True

        # --- exits priority: TP → pre-expiry → max-adj gate on trigger → Adj B ---
        if profit_target is not None and net_mtm >= profit_target:
            exit_reason = "PROFIT_TARGET"
            exit_ts = ts
            exit_mtm = net_mtm
            break

        if is_pre_expiry(ts, expiry):
            exit_reason = "PRE_EXPIRY"
            exit_ts = ts
            exit_mtm = net_mtm
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
                    exit_mtm = net_mtm
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
                    exit_mtm = net_mtm
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
                    exit_mtm = net_mtm
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
                sf_ex = resolve_slip_frac(old_mark, dte_now, slip_model, slip_mult)
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
                            sf_w = resolve_slip_frac(wm, dte_now, slip_model, slip_mult)
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
                                    sf_n = resolve_slip_frac(nm, dte_now, slip_model, slip_mult)
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
                sf_in = resolve_slip_frac(nm, dte_now, slip_model, slip_mult)
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
            sf = resolve_slip_frac(m, dte_now, slip_model, slip_mult)
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
    avg_applied = (
        float(statistics.fmean(entry_slips + slip_exit_samples))
        if (entry_slips or slip_exit_samples)
        else 0.0
    )
    h_adj1 = (
        (adj_events[0].ts - entry_ts) / 3600.0 if len(adj_events) >= 1 else None
    )
    h_adj2 = (
        (adj_events[1].ts - entry_ts) / 3600.0 if len(adj_events) >= 2 else None
    )

    return (
        CycleResult(
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
            premium_mode=premium_mode,
            target_premium=target_prem,
            profit_mode=profit_mode,
            profit_target_usd=float(profit_target or 0.0),
            avg_applied_slip_pct=avg_applied,
            arm=arm_label(cfg),
            window=str(cfg.get("window") or ""),
            tp_touched=tp_touched,
            exit_mtm=float(exit_mtm),
            hours_to_adj1=h_adj1,
            hours_to_adj2=h_adj2,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Synthetic cycle (unit tests / exit-path proofs)
# ---------------------------------------------------------------------------
def simulate_synthetic_cycle(
    *,
    entry_ts: int,
    expiry: date,
    spot: float,
    call_leg: LegState,
    put_leg: LegState,
    wing_c: LegState,
    wing_p: LegState,
    series: dict[str, dict[int, float]],
    chain_by_ts: dict[int, dict[str, list[dict[str, Any]]]],
    all_strikes: list[float],
    cfg: dict[str, Any],
    profit_target: float | None,
) -> CycleResult:
    """
    Monitor loop only — used by exit-path unit tests.
    chain_by_ts[ts]['call'|'put'] = option chain rows for Adj B.
    """
    qty0 = int(call_leg.qty)
    slip_model = str(cfg.get("slip_model") or SLIP_MODEL_FLAT)
    slip_mult = float(cfg.get("slip_mult", 1.0))
    exp_ts = to_unix(ist_dt(expiry, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))
    adj_count = 0
    adj_events: list[AdjEvent] = []
    worst_mtm = 0.0
    exit_reason = ""
    exit_ts = entry_ts
    slip_exit_samples: list[float] = []

    def mark_of(leg: LegState, ts: int) -> float | None:
        s = series.get(leg.symbol) or {}
        minute = (ts // 60) * 60
        if minute in s:
            return s[minute]
        for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
            if minute + d in s:
                return s[minute + d]
        return None

    ts = entry_ts + MONITOR_STEP_SEC
    while ts <= exp_ts:
        fwd = spot
        mc = mark_of(call_leg, ts) if call_leg.status == "open" else None
        mp = mark_of(put_leg, ts) if put_leg.status == "open" else None
        mwc = mark_of(wing_c, ts) if wing_c.status == "open" else None
        mwp = mark_of(wing_p, ts) if wing_p.status == "open" else None
        if call_leg.status == "open" and mc is None:
            ts += MONITOR_STEP_SEC
            continue
        if put_leg.status == "open" and mp is None:
            ts += MONITOR_STEP_SEC
            continue

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

        if profit_target is not None and net_mtm >= profit_target:
            exit_reason = "PROFIT_TARGET"
            exit_ts = ts
            break
        if is_pre_expiry(ts, expiry):
            exit_reason = "PRE_EXPIRY"
            exit_ts = ts
            break

        if (
            str(cfg.get("adj_mode") or "B_only").upper() in ("B_ONLY", "BOTH", "B")
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
            tested = untested = None
            if call_pressured and put_decayed:
                tested, untested = "call", "put"
            elif put_pressured and call_decayed:
                tested, untested = "put", "call"
            if tested is not None and untested is not None:
                if adj_count >= int(cfg["max_adj"]):
                    exit_reason = "MAX_ADJUSTMENTS_REACHED"
                    exit_ts = ts
                    break
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
                untested_leg = put_leg if untested == "put" else call_leg
                other_leg = call_leg if untested == "put" else put_leg
                wing_leg_obj = wing_c if untested == "call" else wing_p

                class _W:
                    pass

                wp = _W()
                wp.status = wing_leg_obj.status
                wp.quantity = wing_leg_obj.qty
                wp.strike = wing_leg_obj.strike
                wing_k = resolve_adj_b_wing_strike(wp)
                chain = (chain_by_ts.get(ts) or {}).get(untested) or []
                res = select_adj_b_strike(
                    leg_type=untested,
                    p_target=float(tested_prem),
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
                dte_now = max(0.0, hours_to_expiry(ts, expiry) / 24.0)
                old_mark = mp if untested == "put" else mc
                assert old_mark is not None
                sf_ex = resolve_slip_frac(old_mark, dte_now, slip_model, slip_mult)
                ex_fill = buy_fill(old_mark, sf_ex)
                fee_ex = eng.option_fee(ex_fill, float(fwd), untested_leg.qty)
                slip_exit_samples.append(sf_ex * 100.0)
                untested_leg.realized += signed_short_upnl(
                    untested_leg.entry_fill, ex_fill, untested_leg.qty
                )
                untested_leg.exit_fee += fee_ex
                untested_leg.status = "closed"
                old_strike = float(untested_leg.strike)
                nm = float(new_row["mark_price"])
                sf_in = resolve_slip_frac(nm, dte_now, slip_model, slip_mult)
                nfill = sell_fill(nm, sf_in)
                nfee = eng.option_fee(nfill, float(fwd), int(new_qty))
                new_leg = LegState(
                    symbol=str(new_row["symbol"]),
                    strike=new_strike,
                    opt_type=untested,
                    qty=int(new_qty),
                    entry_fill=nfill,
                    entry_mark=nm,
                    baseline=nm,
                    entry_fee=nfee,
                    entry_slip=sf_in * 100.0,
                )
                other_leg.baseline = float(nm)
                other_leg.qty = int(new_qty)
                if untested == "call":
                    call_leg = new_leg
                else:
                    put_leg = new_leg
                adj_count += 1
                adj_events.append(
                    AdjEvent(
                        ts=ts,
                        leg=untested,
                        old_strike=old_strike,
                        new_strike=new_strike,
                        reason="ADJ_B",
                        new_qty=int(new_qty),
                    )
                )
        ts += MONITOR_STEP_SEC
    else:
        exit_reason = exit_reason or "EXPIRY"
        exit_ts = min(ts, exp_ts)

    # flatten open legs at exit
    for leg, is_short in (
        (call_leg, True),
        (put_leg, True),
        (wing_c, False),
        (wing_p, False),
    ):
        if leg.status != "open":
            continue
        m = mark_of(leg, exit_ts) or leg.entry_mark
        dte_now = max(0.0, hours_to_expiry(exit_ts, expiry) / 24.0)
        sf = resolve_slip_frac(m, dte_now, slip_model, slip_mult)
        if is_short:
            fill = buy_fill(m, sf)
            leg.realized += signed_short_upnl(leg.entry_fill, fill, leg.qty)
        else:
            fill = sell_fill(m, sf)
            leg.realized += signed_long_upnl(leg.entry_fill, fill, leg.qty)
        leg.exit_fee += eng.option_fee(fill, spot, leg.qty)
        leg.status = "closed"

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
    entry_day = datetime.fromtimestamp(entry_ts, tz=UTC).astimezone(IST).date()
    return CycleResult(
        entry_date=entry_day,
        entry_ts=entry_ts,
        call_strike=call_leg.strike,
        put_strike=put_leg.strike,
        entry_prem_c=call_leg.entry_mark,
        entry_prem_p=put_leg.entry_mark,
        qty=qty0,
        wing_c=wing_c.strike,
        wing_p=wing_p.strike,
        n_adjustments=adj_count,
        adj_events=adj_events,
        exit_ts=exit_ts,
        exit_reason=exit_reason,
        hold_hours=max(0.0, (exit_ts - entry_ts) / 3600.0),
        gross_pnl=gross,
        fees=fees,
        slippage_cost=0.0,
        net_pnl=gross - fees,
        worst_mtm=worst_mtm,
        applied_slip_entry=0.0,
        applied_slip_exit=0.0,
        spot_source="synthetic",
        profit_target_usd=float(profit_target or 0.0),
    )


# ---------------------------------------------------------------------------
# Runner / stats
# ---------------------------------------------------------------------------
def summarize_cycles(
    cycles: list[CycleResult],
    d0: date,
    d1: date,
    skips: SkipAccount | None = None,
) -> dict[str, Any]:
    n = len(cycles)
    day_span = max(1, (d1 - d0).days + 1)
    by_day: dict[date, float] = defaultdict(float)
    for c in cycles:
        by_day[c.entry_date] += c.net_pnl
    daily = [by_day.get(d0 + timedelta(days=i), 0.0) for i in range(day_span)]
    mean_day = float(statistics.fmean(daily)) if daily else float("nan")
    reason_counts: dict[str, int] = defaultdict(int)
    for c in cycles:
        reason_counts[c.exit_reason] += 1
    mix = {
        k: (100.0 * v / n if n else float("nan"))
        for k, v in sorted(reason_counts.items())
    }
    holds = [c.hold_hours for c in cycles]
    mean_p, lo, hi = day_clustered_ci_daily(daily, BOOTSTRAP_N, BOOTSTRAP_SEED)
    worst = min(cycles, key=lambda c: c.net_pnl) if cycles else None

    adj1_h = [c.hours_to_adj1 for c in cycles if c.hours_to_adj1 is not None]
    adj2_h = [c.hours_to_adj2 for c in cycles if c.hours_to_adj2 is not None]
    tp_touch_pct = (
        100.0 * sum(1 for c in cycles if c.tp_touched) / n if n else float("nan")
    )
    force2 = [
        c.exit_mtm
        for c in cycles
        if c.n_adjustments >= 2 and c.exit_reason == "MAX_ADJUSTMENTS_REACHED"
    ]

    out: dict[str, Any] = {
        "n_cycles": n,
        "mean_day": mean_day,
        "ci_lo": lo,
        "ci_hi": hi,
        "bootstrap_mean": mean_p,
        "worst_net": float(worst.net_pnl) if worst else float("nan"),
        "worst_date": worst.entry_date.isoformat() if worst else "",
        "max_dd": max_drawdown(daily) if daily else float("nan"),
        "adj_per": float(statistics.fmean([c.n_adjustments for c in cycles]))
        if cycles
        else float("nan"),
        "exit_mix": mix,
        "hold_med": float(statistics.median(holds)) if holds else float("nan"),
        "fees_day": sum(c.fees for c in cycles) / day_span,
        "slip_day": sum(c.slippage_cost for c in cycles) / day_span,
        "avg_slip_pct": float(
            statistics.fmean([c.avg_applied_slip_pct for c in cycles])
        )
        if cycles
        else float("nan"),
        "med_hours_to_adj1": float(statistics.median(adj1_h))
        if adj1_h
        else float("nan"),
        "med_hours_to_adj2": float(statistics.median(adj2_h))
        if adj2_h
        else float("nan"),
        "tp_touched_pct": tp_touch_pct,
        "med_exit_mtm_after_2adj": float(statistics.median(force2))
        if force2
        else float("nan"),
    }
    if skips is not None:
        out.update(skips.as_dict())
    return out


def paired_cycle_diff_ci(
    arm_cycles: list[CycleResult],
    baseline_cycles: list[CycleResult],
    n: int,
    seed: int,
) -> tuple[float, float, float, int]:
    """
    Same-date cycle-by-cycle (arm − baseline) diffs.
    Day-clustered bootstrap of the mean of those diffs.
    Returns (mean, ci_lo, ci_hi, n_paired).
    """
    arm_by: dict[date, list[float]] = defaultdict(list)
    base_by: dict[date, list[float]] = defaultdict(list)
    for c in arm_cycles:
        arm_by[c.entry_date].append(c.net_pnl)
    for c in baseline_cycles:
        base_by[c.entry_date].append(c.net_pnl)

    by_day_diffs: dict[date, list[float]] = {}
    all_diffs: list[float] = []
    for d in sorted(set(arm_by) & set(base_by)):
        a = arm_by[d]
        b = base_by[d]
        k = min(len(a), len(b))
        diffs = [a[i] - b[i] for i in range(k)]
        if diffs:
            by_day_diffs[d] = diffs
            all_diffs.extend(diffs)
    if not all_diffs:
        return float("nan"), float("nan"), float("nan"), 0
    mean = float(statistics.fmean(all_diffs))
    days = sorted(by_day_diffs)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n):
        sample: list[float] = []
        for _d in days:
            day = days[rng.randrange(len(days))]
            sample.extend(by_day_diffs[day])
        if sample:
            means.append(float(statistics.fmean(sample)))
    if not means:
        return mean, float("nan"), float("nan"), len(all_diffs)
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return mean, float(lo), float(hi), len(all_diffs)


def day_clustered_ci_daily(
    daily: list[float], n: int, seed: int
) -> tuple[float, float, float]:
    """Bootstrap mean of the daily PnL series (includes zero days)."""
    if not daily:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    means: list[float] = []
    m = len(daily)
    for _ in range(n):
        sample = [daily[rng.randrange(m)] for _ in range(m)]
        means.append(float(statistics.fmean(sample)))
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return float(statistics.fmean(means)), float(lo), float(hi)


def day_clustered_ci(
    cycles: list[CycleResult], n: int, seed: int
) -> tuple[float, float, float]:
    """Legacy: day-clustered bootstrap of mean daily PnL from cycle entry days."""
    by_day: dict[date, float] = defaultdict(float)
    for c in cycles:
        by_day[c.entry_date] += c.net_pnl
    days = sorted(by_day)
    if not days:
        return float("nan"), float("nan"), float("nan")
    return day_clustered_ci_daily([by_day[d] for d in days], n, seed)


def max_drawdown(daily: list[float]) -> float:
    peak = 0.0
    eq = 0.0
    dd = 0.0
    for x in daily:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return float(dd)


def run(
    cfg: dict[str, Any],
) -> tuple[list[str], list[CycleResult], SkipAccount]:
    lines: list[str] = []
    lines.extend(parity_checklist_lines(wing_roll=bool(cfg["wing_roll"])))

    if cfg["slip_model"] == SLIP_MODEL_BUCKETED:
        load_slip_table()

    lines.append("===== S001 MARK ENGINE =====")
    lines.append(f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    lines.append(f"arm={arm_label(cfg)} window={cfg.get('window')}")
    lines.append(f"config={cfg}")
    lines.append(f"window_dates={cfg['from_date']} .. {cfg['to_date']}")
    pm = str(cfg.get("premium_mode") or "fixed")
    if pm == "b25":
        lines.append(
            f"premium_mode=b25  "
            f"target=ATM_straddle×{float(cfg.get('premium_pct_of_hedge') or 25.0):.1f}% "
            f"(resolved per cycle at entry)"
        )
    else:
        lines.append(
            f"premium_mode=fixed  "
            f"target_premium_per_side="
            f"{float(cfg.get('target_premium_per_side') or 150.0):.2f} "
            f"(live hedge_disabled fallback)"
        )
    lines.append(
        f"profit_mode={cfg.get('profit_mode')}  "
        f"tp_pct={cfg.get('tp_pct')}  profit_k={cfg.get('profit_k')}  "
        f"slip_mult={cfg.get('slip_mult')}  slip_model={cfg.get('slip_model')}"
    )
    lines.append("")

    spot_path = find_spot_csv()
    if spot_path is None:
        lines.append("ERROR: no BTCUSD_1m_*.csv")
        return lines, [], SkipAccount()
    spot_map = load_spot_map(spot_path)
    lines.append(f"spot_csv={spot_path.name} bars={len(spot_map)}")

    store = MarksStore()
    lines.append(f"marks_months={store.available_months}")

    d0: date = cfg["from_date"]
    d1: date = cfg["to_date"]
    cycles: list[CycleResult] = []
    skips = SkipAccount(days_in_window=max(0, (d1 - d0).days + 1))

    day = d0
    while day <= d1:
        entries_today = 0
        entry_hh = int(cfg["entry_hour"])
        entry_mm = int(cfg["entry_minute"])
        entry_ts = to_unix(ist_dt(day, entry_hh, entry_mm))
        expiry = day + timedelta(days=int(cfg["dte"]))

        pending_ts: int | None = entry_ts
        while pending_ts is not None and entries_today < MAX_ENTRIES_PER_DAY:
            cyc, skip_reason = simulate_cycle(
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
                skips.record_skip(skip_reason or SKIP_OTHER, day)
                break
            cycles.append(cyc)
            skips.cycles_entered += 1
            entries_today += 1
            logger.info(
                "day=%s cycle#%d exit=%s net=%.4f adj=%d prem_mode=%s "
                "target_prem=%.2f profit_tgt=%s tp_touched=%s",
                day,
                entries_today,
                cyc.exit_reason,
                cyc.net_pnl,
                cyc.n_adjustments,
                cyc.premium_mode,
                cyc.target_premium,
                cyc.profit_target_usd,
                cyc.tp_touched,
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
    lines.append(
        f"skips days={skips.days_in_window} entered={skips.cycles_entered} "
        f"no_chain={skips.skipped_no_chain} "
        f"no_strike={skips.skipped_no_strike_at_target} "
        f"no_wing={skips.skipped_no_wing} "
        f"no_mark={skips.skipped_no_mark} "
        f"other={skips.skipped_other}"
    )
    for reason, ex in skips.examples.items():
        lines.append(f"  examples_{reason}={','.join(ex)}")
    if n == 0:
        lines.append("No cycles completed.")
        return lines, cycles, skips

    stats = summarize_cycles(cycles, d0, d1, skips)
    nets = [c.net_pnl for c in cycles]
    median_net = float(statistics.median(nets))
    holds = [c.hold_hours for c in cycles]

    lines.append(f"mean_net_per_day={stats['mean_day']:.6f}")
    lines.append(f"median_cycle_net={median_net:.6f}")
    lines.append(
        f"bootstrap_mean_day={stats['bootstrap_mean']:.6f} "
        f"95%CI=[{stats['ci_lo']:.6f},{stats['ci_hi']:.6f}] "
        f"n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED}"
    )
    lines.append(
        f"worst_cycle date={stats['worst_date']} net={stats['worst_net']:.6f}"
    )
    lines.append(f"max_drawdown_daily_equity={stats['max_dd']:.6f}")
    lines.append(f"adj_per_cycle={stats['adj_per']:.4f}")
    mix = ", ".join(f"{k}={v:.1f}%" for k, v in stats["exit_mix"].items())
    lines.append(f"exit_reason_mix%={mix}")
    lines.append(
        f"hold_hours mean={statistics.fmean(holds):.2f} "
        f"median={stats['hold_med']:.2f} "
        f"min={min(holds):.2f} max={max(holds):.2f}"
    )
    lines.append(
        f"med_hours_to_adj1={stats['med_hours_to_adj1']:.2f} "
        f"med_hours_to_adj2={stats['med_hours_to_adj2']:.2f}"
    )
    lines.append(f"tp_touched_pct={stats['tp_touched_pct']:.1f}")
    lines.append(
        f"med_exit_mtm_after_2adj={stats['med_exit_mtm_after_2adj']:.6f}"
    )
    lines.append(f"fees_per_day={stats['fees_day']:.6f}")
    lines.append(f"slippage_cost_per_day={stats['slip_day']:.6f}")
    lines.append(f"avg_applied_slip_pct={stats['avg_slip_pct']:.4f}")
    if cycles:
        lines.append(
            f"sample_cycle premium_mode={cycles[0].premium_mode} "
            f"target_premium={cycles[0].target_premium:.4f} "
            f"profit_mode={cycles[0].profit_mode} "
            f"locked_tp_usd={cycles[0].profit_target_usd:.4f}"
        )
    lines.append("")
    return lines, cycles, skips


def _fmt_num(x: Any, width: int = 8, prec: int = 4) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return f"{'nan':>{width}}"
    if math.isnan(v):
        return f"{'nan':>{width}}"
    return f"{v:{width}.{prec}f}"


def format_matrix_table(rows: list[dict[str, Any]]) -> list[str]:
    rows_sorted = sorted(
        rows, key=lambda r: r.get("mean_day", float("-inf")), reverse=True
    )
    out: list[str] = ["===== MATRIX (sorted by mean/day desc) ====="]
    hdr = (
        f"{'arm':<28} {'n':>3} {'ent':>3} {'mean/day':>9} "
        f"{'ci_lo':>8} {'ci_hi':>8} {'vsB_mn':>8} {'vsB_lo':>8} {'vsB_hi':>8} "
        f"{'skipS':>5} {'tp%':>5} {'adj1h':>6} {'adj2h':>6} {'exMTM2':>8}  exit_mix"
    )
    out.append(hdr)
    for r in rows_sorted:
        mix = ",".join(
            f"{k[:8]}={v:.0f}%" for k, v in (r.get("exit_mix") or {}).items()
        )
        skip_strike = int(r.get("skipped_no_strike_at_target") or 0)
        out.append(
            f"{str(r.get('arm', '')):<28} "
            f"{int(r.get('n_cycles') or 0):3d} "
            f"{int(r.get('cycles_entered') or 0):3d} "
            f"{_fmt_num(r.get('mean_day'), 9, 4)} "
            f"{_fmt_num(r.get('ci_lo'), 8, 4)} "
            f"{_fmt_num(r.get('ci_hi'), 8, 4)} "
            f"{_fmt_num(r.get('paired_mean'), 8, 4)} "
            f"{_fmt_num(r.get('paired_ci_lo'), 8, 4)} "
            f"{_fmt_num(r.get('paired_ci_hi'), 8, 4)} "
            f"{skip_strike:5d} "
            f"{_fmt_num(r.get('tp_touched_pct'), 5, 1)} "
            f"{_fmt_num(r.get('med_hours_to_adj1'), 6, 2)} "
            f"{_fmt_num(r.get('med_hours_to_adj2'), 6, 2)} "
            f"{_fmt_num(r.get('med_exit_mtm_after_2adj'), 8, 4)}  "
            f"{mix}"
        )
    out.append("")
    out.append("===== SKIP ACCOUNTING =====")
    out.append(
        f"{'arm':<28} {'days':>4} {'ent':>3} "
        f"{'no_ch':>5} {'no_st':>5} {'no_wg':>5} {'no_mk':>5} {'other':>5}"
    )
    for r in rows_sorted:
        out.append(
            f"{str(r.get('arm', '')):<28} "
            f"{int(r.get('days_in_window') or 0):4d} "
            f"{int(r.get('cycles_entered') or 0):3d} "
            f"{int(r.get('skipped_no_chain') or 0):5d} "
            f"{int(r.get('skipped_no_strike_at_target') or 0):5d} "
            f"{int(r.get('skipped_no_wing') or 0):5d} "
            f"{int(r.get('skipped_no_mark') or 0):5d} "
            f"{int(r.get('skipped_other') or 0):5d}"
        )
        ex = r.get("skip_examples") or {}
        for reason in SKIP_REASONS:
            dates = ex.get(reason) or []
            if dates:
                out.append(f"  {reason}: {', '.join(dates)}")
    out.append("")
    out.append(
        f"{len(rows)} combos tested — best-of-N bias; "
        "final selection OOS window pe hi honi chahiye"
    )
    out.append("P4 note: paired vs BASELINE_OLD_LIVE needs paired_ci_lo > 0")
    return out


def write_cycles_csv(cycles: list[CycleResult]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "arm",
        "window",
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
        "avg_applied_slip_pct",
        "premium_mode",
        "target_premium",
        "profit_mode",
        "profit_target_usd",
        "tp_touched",
        "exit_mtm",
        "hours_to_adj1",
        "hours_to_adj2",
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
                    "arm": c.arm,
                    "window": c.window,
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
                    "avg_applied_slip_pct": f"{c.avg_applied_slip_pct:.4f}",
                    "premium_mode": c.premium_mode,
                    "target_premium": f"{c.target_premium:.4f}",
                    "profit_mode": c.profit_mode,
                    "profit_target_usd": f"{c.profit_target_usd:.6f}",
                    "tp_touched": int(c.tp_touched),
                    "exit_mtm": f"{c.exit_mtm:.6f}",
                    "hours_to_adj1": (
                        f"{c.hours_to_adj1:.4f}" if c.hours_to_adj1 is not None else ""
                    ),
                    "hours_to_adj2": (
                        f"{c.hours_to_adj2:.4f}" if c.hours_to_adj2 is not None else ""
                    ),
                    "spot_source": c.spot_source,
                }
            )
    logger.info("wrote %s (%d rows)", OUT_CSV, len(cycles))


def parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().replace("IST", "").strip().split(":")
    return int(parts[0]), int(parts[1])


def build_cfg(args: argparse.Namespace) -> dict[str, Any]:
    eh, em = parse_hhmm(args.entry_time)
    return {
        "dec_pct": float(args.dec_pct),
        "adj_b_trigger": float(args.adj_b_trigger),
        "adj_mode": str(args.adj_mode),
        "hedge": str(args.hedge),
        "profit_k": float(args.profit_k),
        "profit_mode": str(args.profit_mode),
        "tp_pct": float(args.tp_pct),
        "wing_points": float(args.wing_points),
        "wing_roll": args.wing_roll == "on",
        "qty_lots": int(args.qty_lots),
        "entry_hour": eh,
        "entry_minute": em,
        "dte": int(args.dte),
        "premium_mode": str(args.premium_mode),
        "target_premium_per_side": float(args.target_premium_per_side),
        "premium_pct_of_hedge": float(args.premium_pct_of_hedge),
        "max_adj": int(args.max_adj),
        "from_date": date.fromisoformat(args.from_date),
        "to_date": date.fromisoformat(args.to_date),
        "slip_model": str(args.slip_model),
        "slip_mult": float(args.slip_mult),
        "window": str(args.window),
        "matrix_include_baseline": bool(
            getattr(args, "matrix_include_baseline", True)
        ),
    }


def run_matrix(
    base_cfg: dict[str, Any],
) -> tuple[list[str], list[dict[str, Any]], list[CycleResult]]:
    """16 locked combos (+ optional BASELINE_OLD_LIVE)."""
    include_baseline = bool(base_cfg.get("matrix_include_baseline", True))
    combos = [
        (pm, tp, sm)
        for pm in ("fixed", "b25")
        for tp in (25.0, 35.0, 50.0, 65.0)
        for sm in (1.0, 1.5)
    ]
    rows: list[dict[str, Any]] = []
    all_cycles: list[CycleResult] = []
    baseline_cycles: list[CycleResult] = []
    lines: list[str] = [
        "===== S001 MARK ENGINE MATRIX =====",
        f"generated_utc={datetime.now(tz=UTC).isoformat()}",
        f"window_tag={base_cfg.get('window')} "
        f"dates={base_cfg['from_date']} .. {base_cfg['to_date']}",
        "locked arms: dec=40 adj_b=70 B_only hedge=off wing=2000 wing_roll=on "
        "qty=8 entry=11:00 IST dte=2 profit_mode=pct_of_credit",
        f"include_baseline={include_baseline}",
        f"n_locked_combos={len(combos)}",
        "",
    ]

    if include_baseline:
        bcfg = baseline_old_live_cfg(base_cfg)
        t0 = time.time()
        _bl, baseline_cycles, bskips = run(bcfg)
        elapsed = time.time() - t0
        st = summarize_cycles(
            baseline_cycles, bcfg["from_date"], bcfg["to_date"], bskips
        )
        row = {
            "arm": "BASELINE_OLD_LIVE",
            "premium_mode": "fixed",
            "tp_pct": 0.0,
            "slip_mult": 1.0,
            "paired_mean": float("nan"),
            "paired_ci_lo": float("nan"),
            "paired_ci_hi": float("nan"),
            "paired_n": 0,
            **st,
            "elapsed_sec": elapsed,
        }
        rows.append(row)
        all_cycles.extend(baseline_cycles)
        logger.info(
            "matrix baseline BASELINE_OLD_LIVE n=%d mean/day=%.4f elapsed=%.1fs",
            st["n_cycles"],
            st["mean_day"],
            elapsed,
        )

    for pm, tp, sm in combos:
        cfg = dict(base_cfg)
        cfg["premium_mode"] = pm
        cfg["tp_pct"] = tp
        cfg["slip_mult"] = sm
        cfg["profit_mode"] = "pct_of_credit"
        cfg["dec_pct"] = 40.0
        cfg["adj_b_trigger"] = 70.0
        cfg["adj_mode"] = "B_only"
        cfg["hedge"] = "off"
        cfg["wing_points"] = 2000.0
        cfg["wing_roll"] = True
        cfg["qty_lots"] = 8
        cfg.pop("arm_name", None)
        name = arm_label(cfg)
        t0 = time.time()
        _combo_lines, cycles, skips = run(cfg)
        elapsed = time.time() - t0
        st = summarize_cycles(cycles, cfg["from_date"], cfg["to_date"], skips)
        p_mean = p_lo = p_hi = float("nan")
        p_n = 0
        if include_baseline and baseline_cycles:
            p_mean, p_lo, p_hi, p_n = paired_cycle_diff_ci(
                cycles, baseline_cycles, BOOTSTRAP_N, BOOTSTRAP_SEED
            )
        row = {
            "arm": name,
            "premium_mode": pm,
            "tp_pct": tp,
            "slip_mult": sm,
            "paired_mean": p_mean,
            "paired_ci_lo": p_lo,
            "paired_ci_hi": p_hi,
            "paired_n": p_n,
            **st,
            "elapsed_sec": elapsed,
        }
        rows.append(row)
        all_cycles.extend(cycles)
        logger.info(
            "matrix combo %s n=%d mean/day=%.4f paired_mean=%.4f "
            "paired_ci=[%.4f,%.4f] elapsed=%.1fs",
            name,
            st["n_cycles"],
            st["mean_day"],
            p_mean,
            p_lo,
            p_hi,
            elapsed,
        )
    lines.extend(format_matrix_table(rows))
    return lines, rows, all_cycles


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
    ap.add_argument(
        "--profit-mode",
        type=str,
        default="pct_of_credit",
        choices=("pct_of_credit", "cost_k", "none"),
    )
    ap.add_argument("--tp-pct", type=float, default=50.0)
    ap.add_argument("--wing-points", type=float, default=2000.0)
    ap.add_argument("--wing-roll", type=str, default="on", choices=("on", "off"))
    ap.add_argument("--qty-lots", type=int, default=8)
    ap.add_argument("--entry-time", type=str, default="11:00")
    ap.add_argument("--dte", type=int, default=2)
    ap.add_argument(
        "--premium-mode",
        type=str,
        default="fixed",
        choices=("fixed", "b25"),
    )
    ap.add_argument("--target-premium-per-side", type=float, default=150.0)
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
    ap.add_argument("--slip-mult", type=float, default=1.0)
    ap.add_argument(
        "--window",
        type=str,
        default="IS",
        choices=("IS", "OOS"),
        help="Tag written to CSV (IS/OOS)",
    )
    ap.add_argument(
        "--matrix",
        action="store_true",
        help="Run 16-combo matrix (premium×tp_pct×slip_mult)",
    )
    ap.add_argument(
        "--matrix-include-baseline",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include BASELINE_OLD_LIVE arm + paired CI (default: on)",
    )
    args = ap.parse_args()
    cfg = build_cfg(args)

    t0 = time.time()
    if args.matrix:
        lines, rows, all_cycles = run_matrix(cfg)
        elapsed = time.time() - t0
        lines.append(f"elapsed_sec={elapsed:.1f}")
        write_cycles_csv(all_cycles)
        OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
        OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("wrote %s", OUT_TXT)
        logger.info("MATRIX %d combos elapsed=%.1fs", len(rows), elapsed)
        for ln in format_matrix_table(rows):
            logger.info("%s", ln)
        return

    lines, cycles, _skips = run(cfg)
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
