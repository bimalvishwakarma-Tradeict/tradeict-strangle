#!/usr/bin/env python3
"""
S001 adjustment sweep — 30 pre-registered configs (maker / B25 / wings 2000).

Reuses s001_income_engine cycle construction + cache
(backtest/cache/s001_income_cycles.pkl). No new market-data download.

Adjustment rules are ported from / imported from live bot code:
  - Adj A / Adj B gates: logic.py (_try_adj_b_action, allow_adj_a/b)
  - Target premium: adjustment.compute_adjustment_target_premium
  - Farther-OTM strike pick: delta_client.find_strike_by_premium
    (require_farther_otm=True) — print-based port below
  - Adj B strike: adj_b.select_adj_b_strike
  - Qty step: wing_entry.compute_decrease_step_qty
"""

from __future__ import annotations

import logging
import math
import pickle
import statistics
import sys
from dataclasses import dataclass
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

# Live imports (pure / light modules only — adjustment.py pulls SQLAlchemy
# which breaks on this Python 3.14 host, so target formula is ported below).
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)


def compute_adjustment_target_premium(
    untouched_leg_offer: float,
    short_baselines: list[float] | tuple[float, ...],
    short_offers: list[float] | tuple[float, ...],
) -> tuple[float, float, float, float]:
    """
    Exact port of backend/strategies/s001_short_strangle/adjustment.py
    compute_adjustment_target_premium (lines 172–211). Live module not
    imported here because adjustment.py → models → SQLAlchemy fails on
    Python 3.14 in this environment.
    """
    unt = float(untouched_leg_offer or 0.0)
    bases = [float(b or 0.0) for b in short_baselines]
    offers = [float(o or 0.0) for o in short_offers]
    if len(bases) != len(offers):
        raise ValueError(
            "short_baselines and short_offers must be the same length"
        )
    combined_baseline = sum(bases)
    combined_current = sum(offers)
    loss = max(0.0, combined_current - combined_baseline)
    return unt + loss, loss, combined_baseline, combined_current

logger = logging.getLogger("s001_adjustment_sweep")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_adjustment_sweep.txt"
CYCLES_CACHE = _BACKTEST / "cache" / "s001_income_cycles.pkl"

# Fixed (confirmed against claude/S001_CONFIG_SEMANTICS.md §1, §4, §7
# and s001_engine_measure.py live constants — NOT guessed):
FILL_PACKAGE = "maker"
STRIKE_MODE = "B25"
WING_POINTS = 2000.0
# auto_trade_settings has no wall-clock entry_time; continuous re-entry.
# Representative slot used across S001 hand-audit / engine_measure LIVE rows:
ENTRY_HHMM = "11:00"
HEDGE_QTY_LOTS = 4
BASKET_QTY_PCT_OF_HEDGE = 200.0  # live pct_of_hedge → ceil(4*2)=8
ORIGINAL_BASKET_QTY = int(math.ceil(HEDGE_QTY_LOTS * BASKET_QTY_PCT_OF_HEDGE / 100.0))
CACHE_BASKET_QTY = eng.BASKET_QTY_LOTS  # income engine measured at 4 lots
QTY_SCALE = ORIGINAL_BASKET_QTY / float(CACHE_BASKET_QTY)

MAX_ADJUSTMENTS_PER_BASKET = 2  # live setting name: max_adjustments_per_basket
ADJUSTMENT_QTY_DECREASE_PCT = 20.0  # live: adjustment_qty_decrease_pct
MONITOR_STEP_SEC = 5 * 60  # print window cadence; live monitor is 30s
ADJ_A_TOLERANCE_PCT = 40.0  # adjustment_premium_tolerance_pct default

BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED

DTES = (0, 1, 2)
ADJ_MODES = ("none", "A_only", "B_only", "BOTH")
TRIGGER_PCTS = (70.0, 90.0, 110.0)


@dataclass(frozen=True)
class SweepCfg:
    dte: int
    adjustment: str  # none | A_only | B_only | BOTH
    trigger_pct: float | None  # None when adjustment == none


@dataclass
class CycleResult:
    net: float
    n_adjustments: int
    adjustment_fees: float
    # Stress / leverage metrics (USD notional of short premium sold;
    # total_fees includes entry + adj exit/entry + wing fees when present)
    premium_sold_usd: float = 0.0
    total_fees_usd: float = 0.0
    entry_date: date | None = None


def premium_notional_usd(premium: float, qty_lots: int) -> float:
    """USD credit from selling `qty_lots` at `premium` ($/BTC)."""
    return float(premium) * abs(int(qty_lots)) * eng.CONTRACT_VALUE


def build_configs() -> list[SweepCfg]:
    """Exactly 3 × (1 none + 3 modes × 3 triggers) = 30. No more."""
    out: list[SweepCfg] = []
    for dte in DTES:
        for mode in ADJ_MODES:
            if mode == "none":
                out.append(SweepCfg(dte=dte, adjustment=mode, trigger_pct=None))
            else:
                for t in TRIGGER_PCTS:
                    out.append(SweepCfg(dte=dte, adjustment=mode, trigger_pct=float(t)))
    return out


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def load_cycles() -> tuple[list[eng.CycleObs], int]:
    if not CYCLES_CACHE.is_file():
        raise FileNotFoundError(
            f"Missing cycle cache {CYCLES_CACHE} — run s001_income_decisive "
            "first; do not download new data in this sweep."
        )
    with CYCLES_CACHE.open("rb") as f:
        obj = pickle.load(f)
    return obj["obs"], int(obj["day_span"])


def filter_base(obs: list[eng.CycleObs], dte: int) -> list[eng.CycleObs]:
    out: list[eng.CycleObs] = []
    for o in obs:
        if o.short_dte != dte:
            continue
        if o.fill_package != FILL_PACKAGE:
            continue
        if o.strike_mode != STRIKE_MODE:
            continue
        if o.wing_points != WING_POINTS:
            continue
        if o.entry_hhmm != ENTRY_HHMM:
            continue
        out.append(o)
    return out


def short_role() -> str:
    return eng.roles_for_package(FILL_PACKAGE)[0]


def long_role() -> str:
    return eng.roles_for_package(FILL_PACKAGE)[1]


def adj_b_pct_from_trigger(trigger_pct: float) -> float:
    """
    Live Adj B uses adj_b_trigger_pct clamped to [10, 90]
    (logic.py _resolve_adj_engine_settings / routes_trade.py).
    Sweep trigger_pct maps onto that knob for B_only / BOTH decay side.
    """
    return max(10.0, min(90.0, float(trigger_pct)))


def find_farther_otm_by_premium(
    idx: eng.TradeIndex,
    exp: date,
    leg: str,
    exclude_strike: float,
    target: float,
    when: datetime,
) -> tuple[float, eng.PrintFill] | None:
    """
    Port of delta_client.find_strike_by_premium(..., require_farther_otm=True)
    (delta_client.py ~2026–2111): closest abs(prem−target) among UP calls /
    DOWN puts; tie-break farther OTM. Uses trade prints instead of chain marks.
    """
    strikes = sorted(idx.strikes_by_expiry.get(exp) or set())
    if not strikes or target <= 0:
        return None
    role = short_role()
    pool: list[tuple[float, eng.PrintFill]] = []
    for k in strikes:
        if leg == "call" and k <= exclude_strike + 1e-9:
            continue
        if leg == "put" and k >= exclude_strike - 1e-9:
            continue
        opt = "C" if leg == "call" else "P"
        fill = eng.nearest_print_prefer(
            idx, eng.format_symbol(opt, k, exp), when, eng.PRINT_WINDOW_SEC, role
        )
        if fill is None or fill.price <= 0:
            continue
        pool.append((k, fill))
    if not pool:
        return None

    def _key(item: tuple[float, eng.PrintFill]) -> tuple[float, float]:
        k, fill = item
        prem_diff = abs(fill.price - target)
        otm_rank = -k if leg == "call" else k
        return (prem_diff, otm_rank)

    best_k, best_fill = min(pool, key=_key)
    return best_k, best_fill


def build_adj_b_chain(
    idx: eng.TradeIndex, exp: date, leg: str, when: datetime
) -> list[dict[str, Any]]:
    """Print-based chain rows for select_adj_b_strike (adj_b.py)."""
    strikes = sorted(idx.strikes_by_expiry.get(exp) or set())
    role = short_role()
    opt = "C" if leg == "call" else "P"
    rows: list[dict[str, Any]] = []
    for k in strikes:
        fill = eng.nearest_print_prefer(
            idx, eng.format_symbol(opt, k, exp), when, eng.PRINT_WINDOW_SEC, role
        )
        if fill is None or fill.price <= 0:
            continue
        rows.append(
            {
                "option_type": leg,
                "strike": k,
                "mark_price": fill.price,
                "best_bid": fill.price,
                "symbol": fill.symbol,
            }
        )
    return rows


def premium_at(
    idx: eng.TradeIndex,
    exp: date,
    leg: str,
    strike: float,
    when: datetime,
    *,
    for_short_exit: bool,
) -> float | None:
    opt = "C" if leg == "call" else "P"
    role = long_role() if for_short_exit else short_role()
    fill = eng.nearest_print_prefer(
        idx, eng.format_symbol(opt, strike, exp), when, eng.PRINT_WINDOW_SEC, role
    )
    if fill is None or fill.price <= 0:
        return None
    return float(fill.price)


def wing_premium_at(
    idx: eng.TradeIndex,
    exp: date,
    leg: str,
    strike: float,
    when: datetime,
) -> float | None:
    """Long wing mark — prefer long_role (taker under maker package)."""
    opt = "C" if leg == "call" else "P"
    fill = eng.nearest_print_prefer(
        idx, eng.format_symbol(opt, strike, exp), when, eng.PRINT_WINDOW_SEC, long_role()
    )
    if fill is None or fill.price <= 0:
        return None
    return float(fill.price)


def mtm_net(
    *,
    sc_entry: float,
    sp_entry: float,
    sc_now: float,
    sp_now: float,
    sc_k: float,
    sp_k: float,
    qty: int,
    wc_entry: float | None,
    wp_entry: float | None,
    wc_now: float | None,
    wp_now: float | None,
    realized: float,
    fees: float,
) -> float:
    """Approx Net MTM for DECISION_PROFIT_AT_TRIGGER (logic.py ~1119)."""
    _ = (sc_k, sp_k)  # retained for audit clarity
    gross = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
        sp_entry, sp_now, qty, is_long=False
    )
    if (
        wc_entry is not None
        and wp_entry is not None
        and wc_now is not None
        and wp_now is not None
    ):
        gross += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True) + eng.cash_pnl(
            wp_entry, wp_now, qty, is_long=True
        )
    return realized + gross - fees


def settle_legs(
    spot: float,
    sc_k: float,
    sp_k: float,
    sc_entry: float,
    sp_entry: float,
    qty: int,
    wc_k: float | None,
    wp_k: float | None,
    wc_entry: float | None,
    wp_entry: float | None,
) -> tuple[float, float]:
    """Returns (pnl_cash, settle_fees) excluding prior realized/fees."""
    sc_i = eng.call_intrinsic(spot, sc_k)
    sp_i = eng.put_intrinsic(spot, sp_k)
    pnl = eng.cash_pnl(sc_entry, sc_i, qty, is_long=False) + eng.cash_pnl(
        sp_entry, sp_i, qty, is_long=False
    )
    fees = eng.option_fee(sc_i, spot, qty) + eng.option_fee(sp_i, spot, qty)
    if (
        wc_k is not None
        and wp_k is not None
        and wc_entry is not None
        and wp_entry is not None
    ):
        wc_i = eng.call_intrinsic(spot, wc_k)
        wp_i = eng.put_intrinsic(spot, wp_k)
        pnl += eng.cash_pnl(wc_entry, wc_i, qty, is_long=True) + eng.cash_pnl(
            wp_entry, wp_i, qty, is_long=True
        )
        fees += eng.option_fee(wc_i, spot, qty) + eng.option_fee(wp_i, spot, qty)
    return pnl, fees


def simulate_none(o: eng.CycleObs) -> CycleResult:
    """Scale cached 4-lot net to live 8-lot (fees+cash both linear in qty)."""
    qty = ORIGINAL_BASKET_QTY
    sc = float(o.short_call.price)
    sp = float(o.short_put.price)
    prem = premium_notional_usd(sc, qty) + premium_notional_usd(sp, qty)
    # entry_fees in cache is 4-lot; scale linearly to 8
    fees = float(o.entry_fees) * QTY_SCALE
    return CycleResult(
        net=float(o.net_no_settle) * QTY_SCALE,
        n_adjustments=0,
        adjustment_fees=0.0,
        premium_sold_usd=prem,
        total_fees_usd=fees,
        entry_date=o.entry_date,
    )


def simulate_with_adjustments(
    o: eng.CycleObs,
    cfg: SweepCfg,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
) -> CycleResult:
    assert cfg.trigger_pct is not None
    mode = cfg.adjustment.upper()  # A_ONLY | B_ONLY | BOTH
    # logic.py:812–814 allow_adj_a / allow_adj_b
    allow_adj_a = mode in {"A_ONLY", "BOTH"}
    allow_adj_b = mode in {"B_ONLY", "BOTH"}
    flat_trigger = float(cfg.trigger_pct)
    adj_b_trig = adj_b_pct_from_trigger(float(cfg.trigger_pct))

    exp = o.basket_expiry
    short_role_s = short_role()
    long_role_s = long_role()

    qty = ORIGINAL_BASKET_QTY
    original_qty = ORIGINAL_BASKET_QTY
    sc_k = float(o.short_call_k)
    sp_k = float(o.short_put_k)
    sc_entry = float(o.short_call.price)
    sp_entry = float(o.short_put.price)
    sc_base = sc_entry
    sp_base = sp_entry

    wc_k = float(o.wing_call_k) if o.wing_call_k is not None else None
    wp_k = float(o.wing_put_k) if o.wing_put_k is not None else None
    wc_entry = float(o.wing_call.price) if o.wing_call is not None else None
    wp_entry = float(o.wing_put.price) if o.wing_put is not None else None

    spot_e = float(o.spot_entry)
    fees = (
        eng.option_fee(sc_entry, spot_e, qty)
        + eng.option_fee(sp_entry, spot_e, qty)
    )
    if wc_entry is not None and wp_entry is not None:
        fees += eng.option_fee(wc_entry, spot_e, qty) + eng.option_fee(
            wp_entry, spot_e, qty
        )

    premium_sold = premium_notional_usd(sc_entry, qty) + premium_notional_usd(
        sp_entry, qty
    )
    realized = 0.0
    adj_fees = 0.0
    adj_count = 0
    closed_early = False

    t0 = int(o.entry_utc.timestamp())
    # Settlement instant used by income engine (12:00 UTC on expiry)
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    if t_end <= t0:
        # 0DTE after settle — fall back to scaled none
        return simulate_none(o)

    t = t0 + MONITOR_STEP_SEC
    while t < t_end and not closed_early:
        when = datetime.fromtimestamp(t, tz=UTC)
        spot = ot.spot_at(times, closes, t)
        if spot is None or spot <= 0:
            t += MONITOR_STEP_SEC
            continue

        sc_now = premium_at(idx, exp, "call", sc_k, when, for_short_exit=True)
        sp_now = premium_at(idx, exp, "put", sp_k, when, for_short_exit=True)
        if sc_now is None or sp_now is None:
            t += MONITOR_STEP_SEC
            continue

        wc_now = (
            wing_premium_at(idx, exp, "call", wc_k, when)
            if wc_k is not None
            else None
        )
        wp_now = (
            wing_premium_at(idx, exp, "put", wp_k, when)
            if wp_k is not None
            else None
        )

        net_dec = mtm_net(
            sc_entry=sc_entry,
            sp_entry=sp_entry,
            sc_now=sc_now,
            sp_now=sp_now,
            sc_k=sc_k,
            sp_k=sp_k,
            qty=qty,
            wc_entry=wc_entry,
            wp_entry=wp_entry,
            wc_now=wc_now,
            wp_now=wp_now,
            realized=realized,
            fees=fees,
        )

        # Adj A triggers — logic.py ~1110+: premium >= baseline * (pct/100)
        call_hit = sc_base > 0 and sc_now >= sc_base * (flat_trigger / 100.0)
        put_hit = sp_base > 0 and sp_now >= sp_base * (flat_trigger / 100.0)

        action: str | None = None  # "A:call" | "A:put" | "B:call" | "B:put"
        if allow_adj_a:
            if call_hit:
                action = "A:call"
            elif put_hit:
                action = "A:put"

        if action is None and allow_adj_b:
            # Port of logic.py _try_adj_b_action :142–153
            call_pressured = sc_base > 0 and sc_now >= sc_base * 1.0
            put_pressured = sp_base > 0 and sp_now >= sp_base * 1.0
            thresh = adj_b_trig / 100.0
            call_decayed = sc_base > 0 and sc_now < sc_base * thresh
            put_decayed = sp_base > 0 and sp_now < sp_base * thresh
            if call_pressured and put_decayed:
                action = "B:put"  # roll untested put IN
            elif put_pressured and call_decayed:
                action = "B:call"

        if action is None:
            t += MONITOR_STEP_SEC
            continue

        kind, leg = action.split(":")
        # Max-adjustments gate — logic.py _check_max_adjustments_exit
        if adj_count >= MAX_ADJUSTMENTS_PER_BASKET:
            # Force exit remaining structure at current marks
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
                sp_entry, sp_now, qty, is_long=False
            )
            exit_fee = eng.option_fee(sc_now, spot, qty) + eng.option_fee(
                sp_now, spot, qty
            )
            if (
                wc_k is not None
                and wp_k is not None
                and wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True) + eng.cash_pnl(
                    wp_entry, wp_now, qty, is_long=True
                )
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            break

        # DECISION_PROFIT_AT_TRIGGER — logic.py ~1119–1136
        if kind == "A" and net_dec > 0:
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
                sp_entry, sp_now, qty, is_long=False
            )
            exit_fee = eng.option_fee(sc_now, spot, qty) + eng.option_fee(
                sp_now, spot, qty
            )
            if (
                wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True) + eng.cash_pnl(
                    wp_entry, wp_now, qty, is_long=True
                )
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            break

        # Qty for next adjustment number (1-based) — wing_entry.compute_decrease_step_qty
        next_n = adj_count + 1
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=next_n,
            decrease_pct=ADJUSTMENT_QTY_DECREASE_PCT,
        )
        if close_basket or new_qty is None:
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
                sp_entry, sp_now, qty, is_long=False
            )
            exit_fee = eng.option_fee(sc_now, spot, qty) + eng.option_fee(
                sp_now, spot, qty
            )
            if (
                wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True) + eng.cash_pnl(
                    wp_entry, wp_now, qty, is_long=True
                )
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            break

        # Resolve new strike
        new_k: float | None = None
        new_fill_px: float | None = None
        if kind == "A":
            # Target from adjustment.compute_adjustment_target_premium (:172–211)
            if leg == "call":
                target, _loss, _cb, _cc = compute_adjustment_target_premium(
                    sp_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            else:
                target, _loss, _cb, _cc = compute_adjustment_target_premium(
                    sc_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            old_k = sc_k if leg == "call" else sp_k
            hit = find_farther_otm_by_premium(
                idx, exp, leg, old_k, float(target), when
            )
            if hit is None:
                t += MONITOR_STEP_SEC
                continue
            new_k, new_fill = hit
            new_fill_px = float(new_fill.price)
            # Soft tolerance check (default 40%) — still take closest if within pool
            if target > 0:
                dev = abs(new_fill_px - target) / target * 100.0
                if dev > ADJ_A_TOLERANCE_PCT * 5:  # only reject absurd outliers
                    t += MONITOR_STEP_SEC
                    continue
        else:
            # Adj B: select_adj_b_strike — P_target = tested side premium
            tested = "put" if leg == "call" else "call"
            p_target = sp_now if tested == "put" else sc_now
            other_k = sp_k if leg == "call" else sc_k
            chain = build_adj_b_chain(idx, exp, leg, when)
            res = select_adj_b_strike(
                leg_type=leg,
                p_target=float(p_target),
                chain=chain,
                spot=float(spot),
                other_short_strike=float(other_k),
                min_short_gap_points=0.0,
            )
            if not res.success or res.strike is None or res.premium is None:
                t += MONITOR_STEP_SEC
                continue
            new_k = float(res.strike)
            # Prefer maker short fill at new strike
            fill = eng.nearest_print_prefer(
                idx,
                eng.format_symbol("C" if leg == "call" else "P", new_k, exp),
                when,
                eng.PRINT_WINDOW_SEC,
                short_role_s,
            )
            new_fill_px = float(fill.price) if fill is not None else float(res.premium)

        assert new_k is not None and new_fill_px is not None

        # --- Execute: exit triggered short (full qty) + enter new (new_qty) ---
        # Fees on BOTH legs (exit + entry) — user verified fee model via eng.option_fee
        if leg == "call":
            exit_px = sc_now
            realized += eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            fee_exit = eng.option_fee(exit_px, spot, qty)
            fee_entry = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_exit + fee_entry
            adj_fees += fee_exit + fee_entry
            premium_sold += premium_notional_usd(new_fill_px, int(new_qty))
            sc_k = new_k
            sc_entry = new_fill_px
            # Baseline reset rules from compute_adjustment_target_premium docstring:
            # triggered baseline → new fill; untouched baseline → new fill too
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            exit_px = sp_now
            realized += eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            fee_exit = eng.option_fee(exit_px, spot, qty)
            fee_entry = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_exit + fee_entry
            adj_fees += fee_exit + fee_entry
            premium_sold += premium_notional_usd(new_fill_px, int(new_qty))
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        # Wing cross / qty resize
        if wc_k is not None and wp_k is not None and wc_entry is not None and wp_entry is not None:
            roll = False
            if leg == "call" and new_k >= wc_k - 1e-9:
                roll = True
            if leg == "put" and new_k <= wp_k + 1e-9:
                roll = True
            if roll:
                # Exit old wings full qty; enter at short ± WING_POINTS with new_qty
                if wc_now is not None and wp_now is not None:
                    realized += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                    wf = eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                        wp_now, spot, qty
                    )
                    fees += wf
                    adj_fees += wf
                wk = eng.pick_wing_strikes(idx, exp, sc_k, sp_k, WING_POINTS)
                if wk is not None:
                    wc_k, wp_k = wk
                    wcf = eng.nearest_print_prefer(
                        idx,
                        eng.format_symbol("C", wc_k, exp),
                        when,
                        eng.PRINT_WINDOW_SEC,
                        long_role_s,
                    )
                    wpf = eng.nearest_print_prefer(
                        idx,
                        eng.format_symbol("P", wp_k, exp),
                        when,
                        eng.PRINT_WINDOW_SEC,
                        long_role_s,
                    )
                    if wcf is not None and wpf is not None:
                        wc_entry = float(wcf.price)
                        wp_entry = float(wpf.price)
                        wf_in = eng.option_fee(wc_entry, spot, int(new_qty)) + eng.option_fee(
                            wp_entry, spot, int(new_qty)
                        )
                        fees += wf_in
                        adj_fees += wf_in
                    else:
                        wc_k = wp_k = None
                        wc_entry = wp_entry = None
                else:
                    wc_k = wp_k = None
                    wc_entry = wp_entry = None
            elif int(new_qty) < qty:
                # Partial wing reduce — close (qty - new_qty) lots
                closed = qty - int(new_qty)
                if wc_now is not None and wp_now is not None and closed > 0:
                    realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
                    wf = eng.option_fee(wc_now, spot, closed) + eng.option_fee(
                        wp_now, spot, closed
                    )
                    fees += wf
                    adj_fees += wf

        qty = int(new_qty)
        adj_count += 1
        t += MONITOR_STEP_SEC

    if not closed_early:
        spot_s = float(o.spot_settle)
        pnl, settle_fee = settle_legs(
            spot_s,
            sc_k,
            sp_k,
            sc_entry,
            sp_entry,
            qty,
            wc_k,
            wp_k,
            wc_entry,
            wp_entry,
        )
        # Income engine reports net_no_settle (excludes settle fee)
        realized += pnl
        # entry+adj fees already in `fees`; do not add settle_fee for net_no_settle
        _ = settle_fee
        net = realized - fees
    else:
        net = realized - fees

    return CycleResult(
        net=net,
        n_adjustments=adj_count,
        adjustment_fees=adj_fees,
        premium_sold_usd=premium_sold,
        total_fees_usd=fees,
        entry_date=o.entry_date,
    )


def summarize(
    nets: list[float],
    adj_ns: list[float],
    adj_fees: list[float],
    *,
    cfg_i: int,
) -> dict[str, float]:
    if not nets:
        return {
            "n": 0,
            "mean": float("nan"),
            "median": float("nan"),
            "ci_lo": float("nan"),
            "ci_hi": float("nan"),
            "sortino_per_cycle": float("nan"),
            "avg_adjustments_per_cycle": float("nan"),
            "total_adjustment_fees_per_cycle": float("nan"),
        }
    mean, lo, hi = eng.bootstrap_mean_ci(
        nets, BOOTSTRAP_N, BOOTSTRAP_SEED + cfg_i * 97
    )
    return {
        "n": float(len(nets)),
        "mean": mean,
        "median": float(statistics.median(nets)),
        "ci_lo": lo,
        "ci_hi": hi,
        "sortino_per_cycle": eng.sortino(nets),
        "avg_adjustments_per_cycle": float(statistics.mean(adj_ns)),
        "total_adjustment_fees_per_cycle": float(statistics.mean(adj_fees)),
    }


def fmt_row(cfg: SweepCfg, s: dict[str, float]) -> str:
    trig = f"{cfg.trigger_pct:.0f}" if cfg.trigger_pct is not None else "-"
    so = s["sortino_per_cycle"]
    so_s = "inf" if so == float("inf") else f"{so:.4f}"
    return (
        f"{cfg.dte:>3}  {cfg.adjustment:<7}  {trig:>7}  "
        f"{int(s['n']):>5}  {s['mean']:9.4f}  {s['median']:9.4f}  "
        f"{s['ci_lo']:9.4f}  {s['ci_hi']:9.4f}  {so_s:>10}  "
        f"{s['avg_adjustments_per_cycle']:8.3f}  "
        f"{s['total_adjustment_fees_per_cycle']:10.4f}"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    configs = build_configs()
    if len(configs) != 30:
        sys.stderr.write(
            f"ABORT: expected 30 configs, got {len(configs)}. Not running.\n"
        )
        return 2

    logger.info("Loading cycle cache %s", CYCLES_CACHE)
    all_obs, day_span = load_cycles()
    logger.info("Loaded %s cycle-rows, day_span=%s", f"{len(all_obs):,}", day_span)

    need_idx = any(c.adjustment != "none" for c in configs)
    idx: eng.TradeIndex | None = None
    times: list[int] = []
    closes: list[float] = []
    if need_idx:
        logger.info("Building trade index (no new download)...")
        idx = eng.build_trade_index()
        times, closes = ot.load_spot_1m()

    by_dte: dict[int, list[eng.CycleObs]] = {
        d: filter_base(all_obs, d) for d in DTES
    }
    for d, rows in by_dte.items():
        logger.info("dte=%s base cycles (maker/B25/wing2000/%s): %s", d, ENTRY_HHMM, len(rows))

    rows_out: list[tuple[SweepCfg, dict[str, float]]] = []
    for i, cfg in enumerate(configs):
        cycles = by_dte[cfg.dte]
        nets: list[float] = []
        adj_ns: list[float] = []
        adj_fees: list[float] = []
        logger.info(
            "Config %s/30 dte=%s adj=%s trigger=%s n_cycles=%s",
            i + 1,
            cfg.dte,
            cfg.adjustment,
            cfg.trigger_pct,
            len(cycles),
        )
        for o in cycles:
            if cfg.adjustment == "none":
                r = simulate_none(o)
            else:
                assert idx is not None
                r = simulate_with_adjustments(o, cfg, idx, times, closes)
            nets.append(r.net)
            adj_ns.append(float(r.n_adjustments))
            adj_fees.append(r.adjustment_fees)
        rows_out.append((cfg, summarize(nets, adj_ns, adj_fees, cfg_i=i)))

    rows_out.sort(key=lambda x: (x[1]["mean"] if math.isfinite(x[1]["mean"]) else -1e99), reverse=True)

    lines: list[str] = []
    emit(lines, "S001 ADJUSTMENT SWEEP — 30 pre-registered configs")
    emit(lines, "=" * 100)
    emit(
        lines,
        f"fixed: fill={FILL_PACKAGE}  strike={STRIKE_MODE}  wing={WING_POINTS:.0f}  "
        f"entry_IST={ENTRY_HHMM}  basket_qty={ORIGINAL_BASKET_QTY} "
        f"(hedge={HEDGE_QTY_LOTS} x {BASKET_QTY_PCT_OF_HEDGE:.0f}%)",
    )
    emit(
        lines,
        f"adj: max_adjustments_per_basket={MAX_ADJUSTMENTS_PER_BASKET}  "
        f"adjustment_qty_decrease_pct={ADJUSTMENT_QTY_DECREASE_PCT}  "
        f"monitor_step_sec={MONITOR_STEP_SEC}",
    )
    emit(
        lines,
        "note: auto_trade has no wall-clock entry_time; "
        f"{ENTRY_HHMM} IST is the S001 hand-audit / engine_measure representative slot.",
    )
    emit(
        lines,
        "note: for B_only/BOTH, trigger_pct maps to adj_b_trigger_pct "
        "clamped [10,90]; Adj A uses flat_trigger_pct = trigger_pct unclamped.",
    )
    emit(lines, f"bootstrap_n={BOOTSTRAP_N}  seed={BOOTSTRAP_SEED}  day_span={day_span}")
    emit(lines, f"cache={CYCLES_CACHE}")
    emit(lines, "")
    emit(
        lines,
        f"{'dte':>3}  {'adj':<7}  {'trig%':>7}  {'n':>5}  {'mean':>9}  {'median':>9}  "
        f"{'ci_lo':>9}  {'ci_hi':>9}  {'sortino':>10}  {'avg_adj':>8}  {'adj_fees':>10}",
    )
    emit(lines, "-" * 110)
    for cfg, s in rows_out:
        emit(lines, fmt_row(cfg, s))

    n_ci = sum(1 for _c, s in rows_out if s["ci_lo"] > 0)
    emit(lines, "")
    emit(lines, f"Configs with ci_lo > 0: {n_ci} / 30 (chance expectation: 1.5)")

    # Paired comparison: none vs BOTH per (dte, trigger)
    emit(lines, "")
    emit(lines, "PAIRED: none mean vs BOTH mean by (dte, trigger_pct)")
    emit(
        lines,
        f"{'dte':>3}  {'trig%':>7}  {'none_mean':>10}  {'BOTH_mean':>10}  {'diff':>10}",
    )
    emit(lines, "-" * 50)
    lookup = {(c.dte, c.adjustment, c.trigger_pct): s for c, s in rows_out}
    for dte in DTES:
        none_s = lookup.get((dte, "none", None))
        none_mean = none_s["mean"] if none_s else float("nan")
        for trig in TRIGGER_PCTS:
            both_s = lookup.get((dte, "BOTH", float(trig)))
            both_mean = both_s["mean"] if both_s else float("nan")
            diff = both_mean - none_mean
            emit(
                lines,
                f"{dte:>3}  {trig:>7.0f}  {none_mean:10.4f}  {both_mean:10.4f}  {diff:10.4f}",
            )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
