#!/usr/bin/env python3
"""
S001 FINAL VALIDATION — locked config, full report (parts 1–9).

Output: backtest/results/s001_final_validation.txt
Real prints only (no IV surface fills). No print().
"""

from __future__ import annotations

import logging
import math
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

import options_trades as ot  # noqa: E402
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_hedge_integration as hedge  # noqa: E402
import s001_income_engine as eng  # noqa: E402
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)

logger = logging.getLogger("s001_final_validation")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_final_validation.txt"

# ---------------------------------------------------------------------------
# FINAL CONFIG (locked) — ACTUAL values used by this script
# ---------------------------------------------------------------------------
CFG: dict[str, Any] = {
    "expiry_dte": 2,
    "entry_time": "11:00 IST",
    "strangle_premium_mode": "pct_of_hedge",
    "strangle_premium_pct_of_hedge": 25,
    "basket_qty": 8,
    "basket_wings_enabled": 1,
    "wing_strike_mode": "points",
    "wing_points_away": 2000.0,
    "wing_roll_with_short_enabled": 0,
    "adjustment_mode": "B_only",
    "adj_b_trigger_pct": 70.0,
    "max_adjustments_per_basket": 2,
    "adjustment_qty_decrease_pct": 40.0,
    "profit_target": "total_cost_x_1.0",
    "profit_target_k": 1.0,
    "same_day_reentry": True,
    "max_entries_per_day": 3,
    "hedge_enabled": 0,
    "fills": "maker",
    "fee_model": "min(index*qtyBTC*0.0001, prem*qtyBTC*0.035)*1.18",
    "prints_only": True,
}

EXPECTED: dict[str, Any] = {
    "expiry_dte": 2,
    "entry_time": "11:00 IST",
    "strangle_premium_mode": "pct_of_hedge",
    "strangle_premium_pct_of_hedge": 25,
    "basket_qty": 8,
    "basket_wings_enabled": 1,
    "wing_strike_mode": "points",
    "wing_points_away": 2000,
    "wing_roll_with_short_enabled": 0,
    "adjustment_mode": "B_only",
    "adj_b_trigger_pct": 70,
    "max_adjustments_per_basket": 2,
    "adjustment_qty_decrease_pct": 40,
    "profit_target": "total cost × 1.0",
    "same_day_reentry": "enabled (max 3 per day)",
    "hedge_enabled": 0,
    "fills": "maker only",
}

BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE
ENTRY_HH = 11
ENTRY_MM = 0


@dataclass
class LedRow:
    ts: int
    symbol: str
    side: str
    qty: int
    price: float
    price_source: str
    fee: float
    running_pnl: float
    note: str = ""


@dataclass
class SimOut:
    net: float
    fees: float
    gross: float
    hold_hours: float
    n_adjustments: int
    entry_date: date
    entry_ts: int
    exit_ts: int
    exit_reason: str
    ledger: list[LedRow] = field(default_factory=list)
    ledger_manual_total: float = float("nan")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def qty_btc(qty: int) -> float:
    return abs(int(qty)) * CV


def apply_slip(price: float, *, side: str, slip: float) -> float:
    """side=sell → worse (lower); side=buy → worse (higher)."""
    p = float(price)
    s = float(slip)
    if s <= 0:
        return p
    if side == "sell":
        return p * (1.0 - s)
    return p * (1.0 + s)


def entry_cost_usd(o: eng.CycleObs, qty: int, slip: float = 0.0) -> float:
    spot = float(o.spot_entry)
    sc = apply_slip(float(o.short_call.price), side="sell", slip=slip)
    sp = apply_slip(float(o.short_put.price), side="sell", slip=slip)
    fees = eng.option_fee(sc, spot, qty) + eng.option_fee(sp, spot, qty)
    wing = 0.0
    if o.wing_call is not None and o.wing_put is not None:
        wc = apply_slip(float(o.wing_call.price), side="buy", slip=slip)
        wp = apply_slip(float(o.wing_put.price), side="buy", slip=slip)
        fees += eng.option_fee(wc, spot, qty) + eng.option_fee(wp, spot, qty)
        wing = (wc + wp) * qty_btc(qty)
    return fees + wing


def fill_source(fill: eng.PrintFill | None) -> str:
    if fill is None:
        return "NOT AVAILABLE"
    return str(fill.source)


def require_print(fill: eng.PrintFill | None) -> eng.PrintFill | None:
    if fill is None or fill.price <= 0:
        return None
    if fill.source != "print":
        return None
    return fill


def filter_and_rebuild_print_cycles(
    obs: list[eng.CycleObs],
    idx: eng.TradeIndex,
) -> tuple[list[eng.CycleObs], int]:
    """Keep B25/maker/11:00/dte2/wing2000; rebuild wings with prints only."""
    skipped = 0
    best: dict[tuple, eng.CycleObs] = {}
    long_role = sweep.long_role()
    for o in obs:
        if o.short_dte != CFG["expiry_dte"]:
            continue
        if o.fill_package != "maker":
            continue
        if o.strike_mode != "B25":
            continue
        if o.entry_hhmm != "11:00":
            continue
        if require_print(o.short_call) is None or require_print(o.short_put) is None:
            skipped += 1
            continue
        exp = o.basket_expiry
        sc_k = float(o.short_call_k)
        sp_k = float(o.short_put_k)
        wk = eng.pick_wing_strikes(
            idx, exp, sc_k, sp_k, float(CFG["wing_points_away"])
        )
        if wk is None:
            skipped += 1
            continue
        wc_k, wp_k = wk
        wc = require_print(
            eng.nearest_print_prefer(
                idx,
                eng.format_symbol("C", wc_k, exp),
                o.entry_utc,
                eng.PRINT_WINDOW_SEC,
                long_role,
            )
        )
        wp = require_print(
            eng.nearest_print_prefer(
                idx,
                eng.format_symbol("P", wp_k, exp),
                o.entry_utc,
                eng.PRINT_WINDOW_SEC,
                long_role,
            )
        )
        if wc is None or wp is None:
            skipped += 1
            continue
        co = eng.CycleObs(
            entry_date=o.entry_date,
            entry_hhmm=o.entry_hhmm,
            entry_utc=o.entry_utc,
            basket_expiry=exp,
            short_dte=o.short_dte,
            fill_package="maker",
            strike_mode="B25",
            wing_points=float(CFG["wing_points_away"]),
            spot_entry=float(o.spot_entry),
            spot_settle=float(o.spot_settle),
            atm_straddle_prem=float(o.atm_straddle_prem),
            target_premium=float(o.target_premium),
            short_call_k=sc_k,
            short_put_k=sp_k,
            short_call=o.short_call,
            short_put=o.short_put,
            wing_call_k=wc_k,
            wing_put_k=wp_k,
            wing_call=wc,
            wing_put=wp,
            wing_used_surface=False,
            basket_pnl=0.0,
            wings_pnl=0.0,
            entry_fees=0.0,
            settle_fees=0.0,
            net_no_settle=0.0,
            net_with_settle=0.0,
            spot_move_abs=float(o.spot_move_abs),
        )
        key = (co.entry_date, co.basket_expiry, sc_k, sp_k)
        best[key] = co
    return list(best.values()), skipped


def build_cycle_at(
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    day: date,
    hh: int,
    mm: int,
    expiry: date,
) -> eng.CycleObs | None:
    """Print-only maker/B25/wing2000 cycle at IST time (for re-entry)."""
    entry_utc = eng.ist_to_utc(day, hh, mm)
    ts = int(entry_utc.timestamp())
    if ts < times[0] or ts > times[-1]:
        return None
    spot_e = ot.spot_at(times, closes, ts)
    if spot_e is None or spot_e <= 0:
        return None
    if expiry not in idx.expiries:
        return None
    spot_s = eng.settle_spot_1200_utc(times, closes, expiry)
    if spot_s is None or spot_s <= 0:
        return None
    settle_ts = int(
        datetime(expiry.year, expiry.month, expiry.day, 12, 0, tzinfo=UTC).timestamp()
    )
    if ts >= settle_ts:
        return None
    short_role, long_role = eng.roles_for_package("maker")
    atm = eng.pick_atm_straddle(idx, expiry, float(spot_e), entry_utc, long_role)
    if atm is None:
        atm = eng.pick_atm_straddle(idx, expiry, float(spot_e), entry_utc, short_role)
    if atm is None:
        return None
    _ak, ac, ap = atm
    if require_print(ac) is None or require_print(ap) is None:
        return None
    atm_prem = ac.price + ap.price
    if atm_prem <= 0:
        return None
    target = 0.25 * atm_prem
    if target < 5.0:
        return None
    strangle = eng.pick_strangle_by_premium(
        idx, expiry, float(spot_e), target, entry_utc, short_role
    )
    if strangle is None:
        return None
    sc_k, sp_k, sc, sp = strangle
    if require_print(sc) is None or require_print(sp) is None:
        return None
    wk = eng.pick_wing_strikes(
        idx, expiry, sc_k, sp_k, float(CFG["wing_points_away"])
    )
    if wk is None:
        return None
    wc_k, wp_k = wk
    wc = require_print(
        eng.nearest_print_prefer(
            idx,
            eng.format_symbol("C", wc_k, expiry),
            entry_utc,
            eng.PRINT_WINDOW_SEC,
            long_role,
        )
    )
    wp = require_print(
        eng.nearest_print_prefer(
            idx,
            eng.format_symbol("P", wp_k, expiry),
            entry_utc,
            eng.PRINT_WINDOW_SEC,
            long_role,
        )
    )
    if wc is None or wp is None:
        return None
    return eng.CycleObs(
        entry_date=day,
        entry_hhmm=f"{hh:02d}:{mm:02d}",
        entry_utc=entry_utc,
        basket_expiry=expiry,
        short_dte=2,
        fill_package="maker",
        strike_mode="B25",
        wing_points=float(CFG["wing_points_away"]),
        spot_entry=float(spot_e),
        spot_settle=float(spot_s),
        atm_straddle_prem=atm_prem,
        target_premium=target,
        short_call_k=sc_k,
        short_put_k=sp_k,
        short_call=sc,
        short_put=sp,
        wing_call_k=wc_k,
        wing_put_k=wp_k,
        wing_call=wc,
        wing_put=wp,
        wing_used_surface=False,
        basket_pnl=0.0,
        wings_pnl=0.0,
        entry_fees=0.0,
        settle_fees=0.0,
        net_no_settle=0.0,
        net_with_settle=0.0,
        spot_move_abs=abs(float(spot_s) - float(spot_e)),
    )


def simulate_basket(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    trigger_pct: float,
    decrease_pct: float,
    profit_k: float | None,
    adj_mode: str,
    wing_roll: bool,
    slip: float = 0.0,
    collect_ledger: bool = False,
) -> SimOut:
    """Core sim. Prints only. slip as fraction (0.04 = 4%)."""
    allow_a = adj_mode.upper() in {"A_ONLY", "BOTH"}
    allow_b = adj_mode.upper() in {"B_ONLY", "BOTH"}
    adj_b_trig = sweep.adj_b_pct_from_trigger(float(trigger_pct))
    flat_trig = float(trigger_pct)
    exp = o.basket_expiry
    short_role_s = sweep.short_role()
    long_role_s = sweep.long_role()
    original_qty = int(CFG["basket_qty"])
    qty = original_qty
    sc_k = float(o.short_call_k)
    sp_k = float(o.short_put_k)
    sc_entry = apply_slip(float(o.short_call.price), side="sell", slip=slip)
    sp_entry = apply_slip(float(o.short_put.price), side="sell", slip=slip)
    sc_base = sc_entry
    sp_base = sp_entry
    wc_k = float(o.wing_call_k) if o.wing_call_k is not None else None
    wp_k = float(o.wing_put_k) if o.wing_put_k is not None else None
    wc_entry = (
        apply_slip(float(o.wing_call.price), side="buy", slip=slip)
        if o.wing_call is not None
        else None
    )
    wp_entry = (
        apply_slip(float(o.wing_put.price), side="buy", slip=slip)
        if o.wing_put is not None
        else None
    )

    spot_e = float(o.spot_entry)
    fees = eng.option_fee(sc_entry, spot_e, qty) + eng.option_fee(sp_entry, spot_e, qty)
    if wc_entry is not None and wp_entry is not None:
        fees += eng.option_fee(wc_entry, spot_e, qty) + eng.option_fee(
            wp_entry, spot_e, qty
        )

    ledger: list[LedRow] = []
    realized = 0.0
    # Entry credit already "in" via cash_pnl at exit; track fees + closed legs
    # Manual: sum of (sell credits - buy debits) - fees using signed cash
    manual = 0.0

    def led(
        ts: int,
        symbol: str,
        side: str,
        q: int,
        px: float,
        src: str,
        fee: float,
        note: str,
        *,
        cash_delta: float,
    ) -> None:
        nonlocal manual
        if not collect_ledger:
            return
        manual += cash_delta - fee
        ledger.append(
            LedRow(
                ts=ts,
                symbol=symbol,
                side=side,
                qty=q,
                price=px,
                price_source=src,
                fee=fee,
                running_pnl=manual,
                note=note,
            )
        )

    t0 = int(o.entry_utc.timestamp())
    # Entry ledger rows
    fee_sc = eng.option_fee(sc_entry, spot_e, qty)
    fee_sp = eng.option_fee(sp_entry, spot_e, qty)
    led(
        t0,
        o.short_call.symbol,
        "SELL",
        qty,
        sc_entry,
        fill_source(o.short_call),
        fee_sc,
        "entry short call",
        cash_delta=sc_entry * qty_btc(qty),
    )
    led(
        t0,
        o.short_put.symbol,
        "SELL",
        qty,
        sp_entry,
        fill_source(o.short_put),
        fee_sp,
        "entry short put",
        cash_delta=sp_entry * qty_btc(qty),
    )
    if o.wing_call is not None and o.wing_put is not None and wc_entry and wp_entry:
        fee_wc = eng.option_fee(wc_entry, spot_e, qty)
        fee_wp = eng.option_fee(wp_entry, spot_e, qty)
        led(
            t0,
            o.wing_call.symbol,
            "BUY",
            qty,
            wc_entry,
            fill_source(o.wing_call),
            fee_wc,
            "entry wing call",
            cash_delta=-(wc_entry * qty_btc(qty)),
        )
        led(
            t0,
            o.wing_put.symbol,
            "BUY",
            qty,
            wp_entry,
            fill_source(o.wing_put),
            fee_wp,
            "entry wing put",
            cash_delta=-(wp_entry * qty_btc(qty)),
        )

    profit_target = (
        entry_cost_usd(o, original_qty, slip) * float(profit_k)
        if profit_k is not None
        else None
    )

    adj_count = 0
    closed_early = False
    exit_reason = "settle"
    exit_ts = t0
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    if t_end <= t0:
        return SimOut(
            net=float("nan"),
            fees=fees,
            gross=0.0,
            hold_hours=0.0,
            n_adjustments=0,
            entry_date=o.entry_date,
            entry_ts=t0,
            exit_ts=t0,
            exit_reason="invalid",
            ledger=ledger,
            ledger_manual_total=manual,
        )

    t = t0 + sweep.MONITOR_STEP_SEC
    while t < t_end and not closed_early:
        when = datetime.fromtimestamp(t, tz=UTC)
        spot = ot.spot_at(times, closes, t)
        if spot is None or spot <= 0:
            t += sweep.MONITOR_STEP_SEC
            continue

        sc_raw = sweep.premium_at(idx, exp, "call", sc_k, when, for_short_exit=True)
        sp_raw = sweep.premium_at(idx, exp, "put", sp_k, when, for_short_exit=True)
        if sc_raw is None or sp_raw is None:
            t += sweep.MONITOR_STEP_SEC
            continue
        # Marks for MTM / triggers use mid-ish print; exit buys get slip
        sc_now = float(sc_raw)
        sp_now = float(sp_raw)
        wc_now = (
            sweep.wing_premium_at(idx, exp, "call", wc_k, when)
            if wc_k is not None
            else None
        )
        wp_now = (
            sweep.wing_premium_at(idx, exp, "put", wp_k, when)
            if wp_k is not None
            else None
        )

        net_now = sweep.mtm_net(
            sc_entry=sc_entry,
            sp_entry=sp_entry,
            sc_now=apply_slip(sc_now, side="buy", slip=slip),
            sp_now=apply_slip(sp_now, side="buy", slip=slip),
            sc_k=sc_k,
            sp_k=sp_k,
            qty=qty,
            wc_entry=wc_entry,
            wp_entry=wp_entry,
            wc_now=(
                apply_slip(float(wc_now), side="sell", slip=slip)
                if wc_now is not None
                else None
            ),
            wp_now=(
                apply_slip(float(wp_now), side="sell", slip=slip)
                if wp_now is not None
                else None
            ),
            realized=realized,
            fees=fees,
        )

        if profit_target is not None and net_now >= profit_target - 1e-12:
            sc_x = apply_slip(sc_now, side="buy", slip=slip)
            sp_x = apply_slip(sp_now, side="buy", slip=slip)
            exit_pnl = eng.cash_pnl(sc_entry, sc_x, qty, is_long=False)
            exit_pnl += eng.cash_pnl(sp_entry, sp_x, qty, is_long=False)
            exit_fee = eng.option_fee(sc_x, spot, qty) + eng.option_fee(sp_x, spot, qty)
            if collect_ledger:
                led(
                    t,
                    eng.format_symbol("C", sc_k, exp),
                    "BUY_TO_CLOSE",
                    qty,
                    sc_x,
                    "print",
                    eng.option_fee(sc_x, spot, qty),
                    "PT close call",
                    cash_delta=-sc_x * qty_btc(qty),
                )
                led(
                    t,
                    eng.format_symbol("P", sp_k, exp),
                    "BUY_TO_CLOSE",
                    qty,
                    sp_x,
                    "print",
                    eng.option_fee(sp_x, spot, qty),
                    "PT close put",
                    cash_delta=-sp_x * qty_btc(qty),
                )
            if (
                wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                wc_x = apply_slip(float(wc_now), side="sell", slip=slip)
                wp_x = apply_slip(float(wp_now), side="sell", slip=slip)
                exit_pnl += eng.cash_pnl(wc_entry, wc_x, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_x, qty, is_long=True)
                exit_fee += eng.option_fee(wc_x, spot, qty) + eng.option_fee(
                    wp_x, spot, qty
                )
                if collect_ledger:
                    led(
                        t,
                        eng.format_symbol("C", float(wc_k or 0), exp),
                        "SELL_TO_CLOSE",
                        qty,
                        wc_x,
                        "print",
                        eng.option_fee(wc_x, spot, qty),
                        "PT close wing call",
                        cash_delta=wc_x * qty_btc(qty),
                    )
                    led(
                        t,
                        eng.format_symbol("P", float(wp_k or 0), exp),
                        "SELL_TO_CLOSE",
                        qty,
                        wp_x,
                        "print",
                        eng.option_fee(wp_x, spot, qty),
                        "PT close wing put",
                        cash_delta=wp_x * qty_btc(qty),
                    )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            exit_reason = "profit_target"
            exit_ts = t
            break

        action: str | None = None
        if allow_a:
            call_hit = sc_base > 0 and sc_now >= sc_base * (flat_trig / 100.0)
            put_hit = sp_base > 0 and sp_now >= sp_base * (flat_trig / 100.0)
            if call_hit:
                action = "A:call"
            elif put_hit:
                action = "A:put"
        if action is None and allow_b:
            call_pressured = sc_base > 0 and sc_now >= sc_base * 1.0
            put_pressured = sp_base > 0 and sp_now >= sp_base * 1.0
            thresh = adj_b_trig / 100.0
            call_decayed = sc_base > 0 and sc_now < sc_base * thresh
            put_decayed = sp_base > 0 and sp_now < sp_base * thresh
            if call_pressured and put_decayed:
                action = "B:put"
            elif put_pressured and call_decayed:
                action = "B:call"
        if action is None:
            t += sweep.MONITOR_STEP_SEC
            continue

        kind, leg = action.split(":")

        def force_exit() -> None:
            nonlocal realized, fees, closed_early, exit_reason, exit_ts
            sc_x = apply_slip(sc_now, side="buy", slip=slip)
            sp_x = apply_slip(sp_now, side="buy", slip=slip)
            exit_pnl = eng.cash_pnl(sc_entry, sc_x, qty, is_long=False)
            exit_pnl += eng.cash_pnl(sp_entry, sp_x, qty, is_long=False)
            exit_fee = eng.option_fee(sc_x, spot, qty) + eng.option_fee(sp_x, spot, qty)
            if collect_ledger:
                led(
                    t,
                    eng.format_symbol("C", sc_k, exp),
                    "BUY_TO_CLOSE",
                    qty,
                    sc_x,
                    "print",
                    eng.option_fee(sc_x, spot, qty),
                    "force close call",
                    cash_delta=-sc_x * qty_btc(qty),
                )
                led(
                    t,
                    eng.format_symbol("P", sp_k, exp),
                    "BUY_TO_CLOSE",
                    qty,
                    sp_x,
                    "print",
                    eng.option_fee(sp_x, spot, qty),
                    "force close put",
                    cash_delta=-sp_x * qty_btc(qty),
                )
            if (
                wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                wc_x = apply_slip(float(wc_now), side="sell", slip=slip)
                wp_x = apply_slip(float(wp_now), side="sell", slip=slip)
                exit_pnl += eng.cash_pnl(wc_entry, wc_x, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_x, qty, is_long=True)
                exit_fee += eng.option_fee(wc_x, spot, qty) + eng.option_fee(
                    wp_x, spot, qty
                )
                if collect_ledger:
                    led(
                        t,
                        eng.format_symbol("C", float(wc_k or 0), exp),
                        "SELL_TO_CLOSE",
                        qty,
                        wc_x,
                        "print",
                        eng.option_fee(wc_x, spot, qty),
                        "force close wing call",
                        cash_delta=wc_x * qty_btc(qty),
                    )
                    led(
                        t,
                        eng.format_symbol("P", float(wp_k or 0), exp),
                        "SELL_TO_CLOSE",
                        qty,
                        wp_x,
                        "print",
                        eng.option_fee(wp_x, spot, qty),
                        "force close wing put",
                        cash_delta=wp_x * qty_btc(qty),
                    )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            exit_reason = "force_adj"
            exit_ts = t

        if adj_count >= int(CFG["max_adjustments_per_basket"]):
            force_exit()
            break

        # Adj A: decision profit at trigger
        if kind == "A" and net_now > 0:
            force_exit()
            exit_reason = "adj_a_profit"
            break

        next_n = adj_count + 1
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=next_n,
            decrease_pct=float(decrease_pct),
        )
        if close_basket or new_qty is None:
            force_exit()
            break

        new_k: float | None = None
        new_fill_px: float | None = None
        new_src = "print"
        if kind == "A":
            if leg == "call":
                target, _a, _b, _c = sweep.compute_adjustment_target_premium(
                    sp_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            else:
                target, _a, _b, _c = sweep.compute_adjustment_target_premium(
                    sc_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            old_k = sc_k if leg == "call" else sp_k
            hit = sweep.find_farther_otm_by_premium(
                idx, exp, leg, old_k, float(target), when
            )
            if hit is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_k, new_fill = hit
            if require_print(new_fill) is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_fill_px = apply_slip(float(new_fill.price), side="sell", slip=slip)
        else:
            tested = "put" if leg == "call" else "call"
            p_target = sp_now if tested == "put" else sc_now
            other_k = sp_k if leg == "call" else sc_k
            chain = sweep.build_adj_b_chain(idx, exp, leg, when)
            res = select_adj_b_strike(
                leg_type=leg,
                p_target=float(p_target),
                chain=chain,
                spot=float(spot),
                other_short_strike=float(other_k),
                min_short_gap_points=0.0,
            )
            if not res.success or res.strike is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_k = float(res.strike)
            fill = require_print(
                eng.nearest_print_prefer(
                    idx,
                    eng.format_symbol("C" if leg == "call" else "P", new_k, exp),
                    when,
                    eng.PRINT_WINDOW_SEC,
                    short_role_s,
                )
            )
            if fill is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_fill_px = apply_slip(float(fill.price), side="sell", slip=slip)

        # Wing roll pre-resolve
        fix1: tuple[float, float, float, float] | None = None
        if wing_roll:
            if wc_now is None or wp_now is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            sc_after = new_k if leg == "call" else sc_k
            sp_after = new_k if leg == "put" else sp_k
            wk = eng.pick_wing_strikes(
                idx, exp, sc_after, sp_after, float(CFG["wing_points_away"])
            )
            if wk is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            nwc_k, nwp_k = wk
            wcf = require_print(
                eng.nearest_print_prefer(
                    idx,
                    eng.format_symbol("C", nwc_k, exp),
                    when,
                    eng.PRINT_WINDOW_SEC,
                    long_role_s,
                )
            )
            wpf = require_print(
                eng.nearest_print_prefer(
                    idx,
                    eng.format_symbol("P", nwp_k, exp),
                    when,
                    eng.PRINT_WINDOW_SEC,
                    long_role_s,
                )
            )
            if wcf is None or wpf is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            fix1 = (
                nwc_k,
                nwp_k,
                apply_slip(float(wcf.price), side="buy", slip=slip),
                apply_slip(float(wpf.price), side="buy", slip=slip),
            )

        assert new_k is not None and new_fill_px is not None
        if leg == "call":
            exit_px = apply_slip(sc_now, side="buy", slip=slip)
            realized += eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            fees += eng.option_fee(exit_px, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            if collect_ledger:
                led(
                    t,
                    eng.format_symbol("C", sc_k, exp),
                    "BUY_TO_CLOSE",
                    qty,
                    exit_px,
                    "print",
                    eng.option_fee(exit_px, spot, qty),
                    f"adj{next_n} exit call",
                    cash_delta=-exit_px * qty_btc(qty),
                )
                led(
                    t,
                    eng.format_symbol("C", new_k, exp),
                    "SELL",
                    int(new_qty),
                    new_fill_px,
                    new_src,
                    eng.option_fee(new_fill_px, spot, int(new_qty)),
                    f"adj{next_n} enter call",
                    cash_delta=new_fill_px * qty_btc(int(new_qty)),
                )
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            exit_px = apply_slip(sp_now, side="buy", slip=slip)
            realized += eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            fees += eng.option_fee(exit_px, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            if collect_ledger:
                led(
                    t,
                    eng.format_symbol("P", sp_k, exp),
                    "BUY_TO_CLOSE",
                    qty,
                    exit_px,
                    "print",
                    eng.option_fee(exit_px, spot, qty),
                    f"adj{next_n} exit put",
                    cash_delta=-exit_px * qty_btc(qty),
                )
                led(
                    t,
                    eng.format_symbol("P", new_k, exp),
                    "SELL",
                    int(new_qty),
                    new_fill_px,
                    new_src,
                    eng.option_fee(new_fill_px, spot, int(new_qty)),
                    f"adj{next_n} enter put",
                    cash_delta=new_fill_px * qty_btc(int(new_qty)),
                )
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        if wc_k is not None and wp_k is not None and wc_entry is not None and wp_entry is not None:
            if wing_roll and fix1 is not None and wc_now is not None and wp_now is not None:
                nwc_k, nwp_k, nwc_px, nwp_px = fix1
                wc_x = apply_slip(float(wc_now), side="sell", slip=slip)
                wp_x = apply_slip(float(wp_now), side="sell", slip=slip)
                realized += eng.cash_pnl(wc_entry, wc_x, qty, is_long=True)
                realized += eng.cash_pnl(wp_entry, wp_x, qty, is_long=True)
                fees += eng.option_fee(wc_x, spot, qty) + eng.option_fee(wp_x, spot, qty)
                fees += eng.option_fee(nwc_px, spot, int(new_qty)) + eng.option_fee(
                    nwp_px, spot, int(new_qty)
                )
                wc_k, wp_k = nwc_k, nwp_k
                wc_entry, wp_entry = nwc_px, nwp_px
            else:
                closed = qty - int(new_qty)
                if closed > 0 and wc_now is not None and wp_now is not None:
                    wc_x = apply_slip(float(wc_now), side="sell", slip=slip)
                    wp_x = apply_slip(float(wp_now), side="sell", slip=slip)
                    realized += eng.cash_pnl(wc_entry, wc_x, closed, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_x, closed, is_long=True)
                    fees += eng.option_fee(wc_x, spot, closed) + eng.option_fee(
                        wp_x, spot, closed
                    )
                    if collect_ledger:
                        led(
                            t,
                            eng.format_symbol("C", float(wc_k), exp),
                            "SELL_PARTIAL",
                            closed,
                            wc_x,
                            "print",
                            eng.option_fee(wc_x, spot, closed),
                            f"adj{next_n} partial wing call",
                            cash_delta=wc_x * qty_btc(closed),
                        )
                        led(
                            t,
                            eng.format_symbol("P", float(wp_k), exp),
                            "SELL_PARTIAL",
                            closed,
                            wp_x,
                            "print",
                            eng.option_fee(wp_x, spot, closed),
                            f"adj{next_n} partial wing put",
                            cash_delta=wp_x * qty_btc(closed),
                        )

        qty = int(new_qty)
        adj_count += 1
        t += sweep.MONITOR_STEP_SEC

    if not closed_early:
        spot_s = float(o.spot_settle)
        # Settlement intrinsic — no slip on settle marks
        pnl, _sf = sweep.settle_legs(
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
        realized += pnl
        exit_ts = t_end
        exit_reason = "settle"
        if collect_ledger:
            led(
                t_end,
                "SETTLE",
                "SETTLE",
                qty,
                spot_s,
                "intrinsic",
                0.0,
                f"settle pnl={pnl:.4f}",
                cash_delta=pnl,
            )

    net = realized - fees
    # Ledger manual: entry credits - entry/exit debits - fees should ≈ net
    # Recompute manual from ledger cash_deltas already tracked as `manual`
    # But entry credits were added and closes subtract exit prices; realized
    # uses cash_pnl which nets entry vs exit. Align manual to net for audit:
    # Use sum of (signed notional) - fees from ledger rows.
    if collect_ledger:
        # Rebuild manual as: all SELL notionals - all BUY notionals - all fees
        m2 = 0.0
        for row in ledger:
            notional = row.price * qty_btc(row.qty)
            if row.side in {"SELL", "SELL_TO_CLOSE", "SELL_PARTIAL"}:
                m2 += notional
            elif row.side in {"BUY", "BUY_TO_CLOSE"}:
                m2 -= notional
            elif row.side == "SETTLE":
                m2 += row.price  # we stuffed pnl into price field — fix:
                # actually settle led uses cash_delta=pnl; recompute from deltas
        # Prefer running from cash_delta path stored in running_pnl end
        manual_final = ledger[-1].running_pnl if ledger else float("nan")
    else:
        manual_final = float("nan")

    hold_h = max(0.0, (exit_ts - t0) / 3600.0)
    gross = realized  # before fees
    return SimOut(
        net=net,
        fees=fees,
        gross=gross,
        hold_hours=hold_h,
        n_adjustments=adj_count,
        entry_date=o.entry_date,
        entry_ts=t0,
        exit_ts=exit_ts,
        exit_reason=exit_reason,
        ledger=ledger,
        ledger_manual_total=manual_final,
    )


def run_with_reentry(
    seed_cycles: list[eng.CycleObs],
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    trigger_pct: float,
    decrease_pct: float,
    profit_k: float | None,
    adj_mode: str,
    wing_roll: bool,
    slip: float = 0.0,
    allow_reentry: bool = True,
    collect_ledgers_for: set[date] | None = None,
) -> list[SimOut]:
    by_date: dict[date, eng.CycleObs] = {}
    for o in seed_cycles:
        by_date[o.entry_date] = o
    outs: list[SimOut] = []
    max_e = int(CFG["max_entries_per_day"]) if allow_reentry else 1
    for day in sorted(by_date):
        o0 = by_date[day]
        entries = 0
        cur: eng.CycleObs | None = o0
        exp = o0.basket_expiry
        while cur is not None and entries < max_e:
            want_led = collect_ledgers_for is not None and day in collect_ledgers_for
            s = simulate_basket(
                cur,
                idx,
                times,
                closes,
                trigger_pct=trigger_pct,
                decrease_pct=decrease_pct,
                profit_k=profit_k,
                adj_mode=adj_mode,
                wing_roll=wing_roll,
                slip=slip,
                collect_ledger=want_led,
            )
            outs.append(s)
            entries += 1
            if (
                not allow_reentry
                or profit_k is None
                or s.exit_reason != "profit_target"
            ):
                break
            re_ts = s.exit_ts + sweep.MONITOR_STEP_SEC
            re_ist = datetime.fromtimestamp(re_ts, tz=UTC).astimezone(IST)
            if re_ist.date() != day:
                break
            cur = build_cycle_at(
                idx,
                times,
                closes,
                day=day,
                hh=re_ist.hour,
                mm=(re_ist.minute // 5) * 5,
                expiry=exp,
            )
    return outs


def daily_series(
    outs: list[SimOut], day_span: int, d0: date, d1: date
) -> list[float]:
    by: dict[date, float] = {}
    for s in outs:
        if not math.isfinite(s.net):
            continue
        by[s.entry_date] = by.get(s.entry_date, 0.0) + s.net
    days: list[float] = []
    d = d0
    n = 0
    while d <= d1 and n < max(day_span, (d1 - d0).days + 1):
        days.append(by.get(d, 0.0))
        d += timedelta(days=1)
        n += 1
    while len(days) < day_span:
        days.append(0.0)
    return days[:day_span]


def summarize_daily(
    outs: list[SimOut],
    days: list[float],
    *,
    seed: int,
) -> dict[str, float]:
    ok = [s for s in outs if math.isfinite(s.net)]
    mean, lo, hi = eng.bootstrap_mean_ci(days, BOOTSTRAP_N, seed)
    return {
        "n": float(len(ok)),
        "mean_day": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "worst": min((s.net for s in ok), default=float("nan")),
        "best": max((s.net for s in ok), default=float("nan")),
        "mdd": eng.max_drawdown(days) if days else float("nan"),
        "total": sum(days),
        "median_day": statistics.median(days) if days else float("nan"),
        "std_day": statistics.pstdev(days) if len(days) > 1 else float("nan"),
        "avg_hold": (
            statistics.mean([s.hold_hours for s in ok]) if ok else float("nan")
        ),
        "avg_adj": (
            statistics.mean([float(s.n_adjustments) for s in ok])
            if ok
            else float("nan")
        ),
        "fees_day": sum(s.fees for s in ok) / float(max(1, len(days))),
        "gross_tot": sum(s.gross for s in ok),
        "fees_tot": sum(s.fees for s in ok),
        "cpd": len(ok) / float(max(1, len(days))),
    }


def part1_config_echo(lines: list[str]) -> bool:
    emit(lines, "===== PART 1: CONFIG ECHO =====")
    emit(lines, f"{'param':<36} {'EXPECTED':<28} {'ACTUAL':<28} {'OK?':>4}")
    emit(lines, "-" * 100)
    mismatches: list[str] = []
    # Map expected keys to CFG checks
    checks: list[tuple[str, Any, Any]] = [
        ("expiry_dte", EXPECTED["expiry_dte"], CFG["expiry_dte"]),
        ("entry_time", EXPECTED["entry_time"], CFG["entry_time"]),
        (
            "strangle_premium_mode",
            EXPECTED["strangle_premium_mode"],
            CFG["strangle_premium_mode"],
        ),
        (
            "strangle_premium_pct_of_hedge",
            EXPECTED["strangle_premium_pct_of_hedge"],
            CFG["strangle_premium_pct_of_hedge"],
        ),
        ("basket_qty", EXPECTED["basket_qty"], CFG["basket_qty"]),
        (
            "basket_wings_enabled",
            EXPECTED["basket_wings_enabled"],
            CFG["basket_wings_enabled"],
        ),
        ("wing_strike_mode", EXPECTED["wing_strike_mode"], CFG["wing_strike_mode"]),
        (
            "wing_points_away",
            float(EXPECTED["wing_points_away"]),
            float(CFG["wing_points_away"]),
        ),
        (
            "wing_roll_with_short_enabled",
            EXPECTED["wing_roll_with_short_enabled"],
            CFG["wing_roll_with_short_enabled"],
        ),
        ("adjustment_mode", EXPECTED["adjustment_mode"], CFG["adjustment_mode"]),
        (
            "adj_b_trigger_pct",
            float(EXPECTED["adj_b_trigger_pct"]),
            float(CFG["adj_b_trigger_pct"]),
        ),
        (
            "max_adjustments_per_basket",
            EXPECTED["max_adjustments_per_basket"],
            CFG["max_adjustments_per_basket"],
        ),
        (
            "adjustment_qty_decrease_pct",
            float(EXPECTED["adjustment_qty_decrease_pct"]),
            float(CFG["adjustment_qty_decrease_pct"]),
        ),
        (
            "profit_target",
            "total cost × 1.0",
            f"total_cost_x_{CFG['profit_target_k']}",
        ),
        (
            "same_day_reentry",
            "enabled (max 3 per day)",
            f"enabled (max {CFG['max_entries_per_day']} per day)"
            if CFG["same_day_reentry"]
            else "disabled",
        ),
        ("hedge_enabled", EXPECTED["hedge_enabled"], CFG["hedge_enabled"]),
        ("fills", "maker only", CFG["fills"] + " only"),
    ]
    for name, exp, act in checks:
        ok = str(exp).replace(" ", "").lower() == str(act).replace(" ", "").lower()
        # numeric soft match
        try:
            ok = abs(float(exp) - float(act)) < 1e-9
        except (TypeError, ValueError):
            pass
        if name == "profit_target":
            ok = CFG["profit_target_k"] == 1.0
        if name == "same_day_reentry":
            ok = CFG["same_day_reentry"] is True and CFG["max_entries_per_day"] == 3
        if name == "fills":
            ok = CFG["fills"] == "maker"
        mark = "OK" if ok else "FAIL"
        if not ok:
            mismatches.append(name)
        emit(lines, f"{name:<36} {str(exp):<28} {str(act):<28} {mark:>4}")
    emit(lines, "")
    emit(lines, f"Fee model ACTUAL: {CFG['fee_model']}")
    emit(lines, f"Prints only ACTUAL: {CFG['prints_only']}")
    emit(lines, "")
    if mismatches:
        emit(lines, f"CONFIG MISMATCH — STOPPING. Fields: {mismatches}")
        return False
    emit(lines, "CONFIG MATCH — proceeding.")
    emit(lines, "")
    return True


def half_cycle_metrics(
    outs: list,
    *,
    half: str,
    is_end: date,
    d0: date,
    d1: date,
    split: int,
    cal_days: int,
    seed: int,
) -> dict[str, float]:
    """IS/OOS cycle-level + daily metrics for one trigger run."""
    if half == "IS":
        sub = [s for s in outs if s.entry_date <= is_end and math.isfinite(s.net)]
        days = daily_series(sub, split, d0, is_end)
        seed_off = 11
    else:
        oos_d0 = is_end + timedelta(days=1)
        sub = [s for s in outs if s.entry_date > is_end and math.isfinite(s.net)]
        days = daily_series(sub, cal_days - split, oos_d0, d1)
        seed_off = 22
    nets = [float(s.net) for s in sub]
    holds = [float(s.hold_hours) for s in sub if math.isfinite(s.hold_hours)]
    adjs = [float(s.n_adjustments) for s in sub]
    mean_c = statistics.fmean(nets) if nets else float("nan")
    worst = min(nets) if nets else float("nan")
    p5 = float(eng.pctile(nets, 5.0)) if nets else float("nan")
    sm = summarize_daily(sub, days, seed=seed + seed_off)
    risk_adj = (
        mean_c / abs(worst)
        if math.isfinite(mean_c) and math.isfinite(worst) and abs(worst) > 1e-12
        else float("nan")
    )
    return {
        "n": float(len(sub)),
        "mean_cycle": mean_c,
        "mean_day": float(sm["mean_day"]),
        "ci_lo": float(sm["ci_lo"]),
        "worst": worst,
        "mdd": float(sm["mdd"]),
        "avg_adj": statistics.fmean(adjs) if adjs else float("nan"),
        "avg_hold": statistics.fmean(holds) if holds else float("nan"),
        "p5": p5,
        "risk_adj": risk_adj,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 TRIGGER FINAL + DATA EXTENSION PATHS")
    emit(lines, "=" * 100)
    emit(lines, "")

    logger.info("Loading cycles + spot (print-only)...")
    all_obs, day_span = sweep.load_cycles()
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()
    base, skipped_prints = filter_and_rebuild_print_cycles(all_obs, idx)
    emit(
        lines,
        f"print-only cycles: {len(base)}  (skipped non-print: {skipped_prints})  "
        f"day_span={day_span}",
    )
    if not base:
        emit(lines, "ERROR: no print-only cycles")
        text = "\n".join(lines) + "\n"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        return 1

    # Calendar span for IS/OOS uses candle date range (same as prior validation)
    full_placeholder = daily_series([], day_span, d0, d1)
    cal_days = len(full_placeholder)
    split = int(cal_days * 0.60)
    is_end = d0 + timedelta(days=split - 1)
    oos_d0 = is_end + timedelta(days=1)
    emit(lines, f"calendar: {d0} -> {d1}  cal_days={cal_days}  day_span={day_span}")
    emit(lines, f"IN-SAMPLE  = first 60% days ({d0} -> {is_end})  n_days={split}")
    emit(
        lines,
        f"OUT-SAMPLE = last 40% days ({oos_d0} -> {d1})  n_days={cal_days - split}",
    )
    emit(
        lines,
        "config: print-only, dec%=40, B_only, wings=2000 roll OFF, "
        "PT k=1.0, qty=8, maker, hedge OFF",
    )
    emit(lines, "")

    # =====================================================================
    # PART A — TRIGGER FINAL
    # =====================================================================
    emit(lines, "===== PART A: TRIGGER FINAL (print-only, dec%=40) =====")
    emit(lines, "")
    TRIGGERS = [30.0, 40.0, 50.0, 60.0, 70.0]
    by_trig: dict[float, dict[str, dict[str, float]]] = {}
    full_meta: dict[float, dict[str, float]] = {}

    for trig in TRIGGERS:
        logger.info("PART A trigger=%s", trig)
        outs = run_with_reentry(
            base,
            idx,
            times,
            closes,
            trigger_pct=trig,
            decrease_pct=40.0,
            profit_k=1.0,
            adj_mode="B_only",
            wing_roll=False,
            slip=0.0,
        )
        is_m = half_cycle_metrics(
            outs,
            half="IS",
            is_end=is_end,
            d0=d0,
            d1=d1,
            split=split,
            cal_days=cal_days,
            seed=BOOTSTRAP_SEED + int(trig),
        )
        oos_m = half_cycle_metrics(
            outs,
            half="OOS",
            is_end=is_end,
            d0=d0,
            d1=d1,
            split=split,
            cal_days=cal_days,
            seed=BOOTSTRAP_SEED + int(trig),
        )
        by_trig[trig] = {"IS": is_m, "OOS": oos_m}
        nets = [float(s.net) for s in outs if math.isfinite(s.net)]
        adjs = [float(s.n_adjustments) for s in outs]
        holds = [float(s.hold_hours) for s in outs if math.isfinite(s.hold_hours)]
        mean_c = statistics.fmean(nets) if nets else float("nan")
        worst = min(nets) if nets else float("nan")
        full_meta[trig] = {
            "avg_adj": statistics.fmean(adjs) if adjs else float("nan"),
            "avg_hold": statistics.fmean(holds) if holds else float("nan"),
            "mean_cycle": mean_c,
            "worst": worst,
            "risk_adj": (
                mean_c / abs(worst)
                if math.isfinite(mean_c) and math.isfinite(worst) and abs(worst) > 1e-12
                else float("nan")
            ),
        }

        for half_name, m in (("IN-SAMPLE", is_m), ("OUT-SAMPLE", oos_m)):
            emit(lines, f"--- trigger={trig:g}  {half_name} ---")
            emit(lines, f"  n cycles          = {int(m['n'])}")
            emit(lines, f"  mean per CYCLE    = {m['mean_cycle']:.6f}")
            emit(lines, f"  mean/day          = {m['mean_day']:.6f}")
            emit(lines, f"  ci_lo             = {m['ci_lo']:.6f}")
            emit(lines, f"  worst cycle       = {m['worst']:.6f}")
            emit(lines, f"  max drawdown      = {m['mdd']:.6f}")
            emit(lines, f"  avg adj/cycle     = {m['avg_adj']:.4f}")
            emit(lines, f"  avg hold hours    = {m['avg_hold']:.4f}")
            emit(lines, f"  p5 cycle PnL      = {m['p5']:.6f}")
            emit(lines, f"  risk-adj (mean/|worst|) = {m['risk_adj']:.6f}")
            emit(lines, "")

    emit(lines, "FINAL TABLE")
    emit(
        lines,
        f"{'trigger':>8} {'IS mean/cyc':>12} {'OOS mean/cyc':>13} "
        f"{'IS worst':>10} {'OOS worst':>10} {'adj/cyc':>8} {'hold_h':>8} "
        f"{'risk-adj IS|OOS':>18}",
    )
    emit(lines, "-" * 100)
    for trig in TRIGGERS:
        is_m = by_trig[trig]["IS"]
        oos_m = by_trig[trig]["OOS"]
        fm = full_meta[trig]
        emit(
            lines,
            f"{trig:8.0f} {is_m['mean_cycle']:12.4f} {oos_m['mean_cycle']:13.4f} "
            f"{is_m['worst']:10.2f} {oos_m['worst']:10.2f} "
            f"{fm['avg_adj']:8.3f} {fm['avg_hold']:8.2f} "
            f"{is_m['risk_adj']:8.4f}|{oos_m['risk_adj']:7.4f}",
        )
    emit(lines, "")

    mean_is_win = max(TRIGGERS, key=lambda t: by_trig[t]["IS"]["mean_cycle"])
    mean_oos_win = max(TRIGGERS, key=lambda t: by_trig[t]["OOS"]["mean_cycle"])
    risk_is_win = max(TRIGGERS, key=lambda t: by_trig[t]["IS"]["risk_adj"])
    risk_oos_win = max(TRIGGERS, key=lambda t: by_trig[t]["OOS"]["risk_adj"])

    def rank_score(t: float) -> float:
        metrics = [
            sorted(TRIGGERS, key=lambda x: by_trig[x]["IS"]["mean_cycle"], reverse=True),
            sorted(TRIGGERS, key=lambda x: by_trig[x]["OOS"]["mean_cycle"], reverse=True),
            sorted(TRIGGERS, key=lambda x: by_trig[x]["IS"]["risk_adj"], reverse=True),
            sorted(TRIGGERS, key=lambda x: by_trig[x]["OOS"]["risk_adj"], reverse=True),
        ]
        return float(sum(m.index(t) + 1 for m in metrics))

    best_compromise = min(TRIGGERS, key=rank_score)
    both_mean = mean_is_win == mean_oos_win
    both_risk = risk_is_win == risk_oos_win
    if both_mean and both_risk and mean_is_win == risk_is_win:
        verdict = (
            f"VERDICT: trigger={mean_is_win:g} — dono halves pe mean AUR "
            f"risk-adjusted dono pe best."
        )
    elif both_mean and both_risk:
        verdict = (
            f"VERDICT: mean pe trigger={mean_is_win:g} dono halves; "
            f"risk-adj pe trigger={risk_is_win:g} dono halves — alag winners. "
            f"Compromise (rank-sum): trigger={best_compromise:g}."
        )
    else:
        verdict = (
            f"VERDICT: ek hi trigger dono halves + dono metrics pe clear nahi. "
            f"mean IS={mean_is_win:g}/OOS={mean_oos_win:g}; "
            f"risk-adj IS={risk_is_win:g}/OOS={risk_oos_win:g}; "
            f"best compromise (rank-sum of mean+risk IS/OOS): "
            f"trigger={best_compromise:g}."
        )
    emit(lines, verdict)
    emit(lines, "")

    # =====================================================================
    # PART B — DATA EXTENSION PATHS (report only)
    # =====================================================================
    emit(lines, "===== PART B: DATA EXTENSION KA RASTA (report only — no download) =====")
    emit(lines, "")
    emit(lines, "1) CANDLES")
    emit(lines, f"   folder: {(_BACKTEST / 'data_1m').resolve()}")
    emit(lines, "   file format: {SYMBOL}_{resolution}_{YYYYMMDD}_{YYYYMMDD}.csv")
    emit(lines, "   example: BTCUSD_1m_20250613_20260913.csv")
    emit(lines, "   schema columns:")
    emit(
        lines,
        "     open_time_unix, open_time_utc, open_time_ist, "
        "open, high, low, close, volume",
    )
    emit(lines, "   downloader: backtest/download_candles.py")
    emit(
        lines,
        "   API: GET https://api.india.delta.exchange/v2/history/candles "
        "(public, no HMAC)",
    )
    emit(lines, "")
    emit(lines, "2) OPTIONS TRADE PRINTS")
    emit(lines, f"   raw zips folder: {(_BACKTEST / 'data_raw').resolve()}")
    emit(lines, "   zip name examples: options-trades-monthly-BTC-YYYY-MM.csv.zip")
    emit(lines, "                      options-trades-daily-BTC-YYYY-MM-DD.csv.zip")
    emit(
        lines,
        f"   SQLite shards: {(_BACKTEST / 'cache' / 'options_trades').resolve()}",
    )
    emit(lines, "   shard name format: opt_trades_YYYY-MM.sqlite")
    emit(lines, "   table: trades")
    emit(lines, "     symbol TEXT NOT NULL")
    emit(lines, "     ts REAL NOT NULL")
    emit(lines, "     price REAL NOT NULL")
    emit(lines, "     size REAL NOT NULL")
    emit(lines, "     role INTEGER NOT NULL   -- 0=maker, 1=taker")
    emit(lines, "     expiry TEXT NOT NULL")
    emit(lines, "     opt_type TEXT NOT NULL")
    emit(lines, "     strike REAL NOT NULL")
    emit(lines, "")
    emit(lines, "3) download_candles.py DATE RANGE")
    emit(
        lines,
        "   CLI: --months (default=12), --symbol (default BTCUSD), "
        "--resolution (default 1m)",
    )
    emit(lines, "   start: target_start = now_utc - months * (365.25/12) days")
    emit(lines, "   end: now (only closed candles; forming bar skipped)")
    emit(
        lines,
        "   set in: backtest/download_candles.py -> download(months=...) "
        "+ _parse_args --months",
    )
    emit(lines, "   NOT a fixed calendar start/end — rolling lookback from run time.")
    emit(lines, "")
    emit(lines, "4) OPTIONS TRADES SOURCE")
    emit(lines, "   Script: backtest/options_trades.py")
    emit(
        lines,
        "   Role: reads existing backtest/data_raw/*.zip -> builds SQLite shards",
    )
    emit(
        lines,
        "   There is NO live Delta options-trade history downloader in this repo.",
    )
    emit(
        lines,
        "   Zip CSV columns used: product_symbol, timestamp, price, size, buyer_role",
    )
    emit(
        lines,
        "   Delta REST endpoint for options trade history download: "
        "NONE in code (UNKNOWN source of zips)",
    )
    emit(lines, "")
    emit(lines, "5) BIMAL — 2 SAAL AUR PURANA DATA (exact steps)")
    emit(lines, "   A. Candles (~24+ months extra / ~36 months lookback):")
    emit(lines, "      cd trading-bot")
    emit(
        lines,
        "      python backtest/download_candles.py --months 36 "
        "--symbol BTCUSD --resolution 1m",
    )
    emit(lines, "   B. Options prints:")
    emit(
        lines,
        "      1. Obtain monthly/daily BTC options trade zip CSVs for older months",
    )
    emit(
        lines,
        "         (same naming as data_raw — Delta India export / account dump).",
    )
    emit(lines, "      2. Place zips into: backtest/data_raw/")
    emit(lines, "      3. Rebuild shards (default rebuilds; use flag only to skip):")
    emit(lines, "         python backtest/options_trades.py")
    emit(lines, "         # optional: python backtest/options_trades.py --no-rebuild-cache")
    emit(lines, "   C. Invalidate cycle cache then re-run studies:")
    emit(lines, "      Remove or rename: backtest/cache/s001_income_cycles.pkl")
    emit(lines, "      python backtest/s001_final_validation.py")
    emit(lines, "")
    emit(lines, "6) Delta India options history API depth")
    emit(
        lines,
        "   Candles history API used in code: /v2/history/candles — "
        "no explicit max-lookback documented in repo.",
    )
    emit(lines, "   Options trade prints history API: not implemented in repo.")
    emit(lines, "   Stated limit in code/docs: UNKNOWN")
    emit(lines, "")
    emit(lines, "DONE.")

    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
