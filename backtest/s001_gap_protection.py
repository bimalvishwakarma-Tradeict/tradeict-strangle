#!/usr/bin/env python3
"""
S001 gap-widening fixes — FIX_NONE / FIX_1 (wing follows) / FIX_2 (short inward limit).

PART A: document min_short_gap_points (read-only)
PART B: three-fix comparison @ wing=2000
PART C: side-by-side ledger for 2026-02-09

Output: backtest/results/s001_gap_fixes.txt
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_income_engine as eng  # noqa: E402
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)

logger = logging.getLogger("s001_gap_protection")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_gap_fixes.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
BASKET_QTY = 8
WING_POINTS = 2000.0
LEDGER_DATE = date(2026, 2, 9)
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE

FIX_NONE = "FIX_NONE"
FIX_1 = "FIX_1"  # wing follows short
FIX_2 = "FIX_2"  # short inward limit vs wing
FIXES = (FIX_NONE, FIX_1, FIX_2)


@dataclass
class LedgerRow:
    symbol: str
    side: str
    strike: float
    qty: int
    price: float
    fee: float
    note: str = ""


@dataclass
class StageSnap:
    label: str
    sc_k: float
    sp_k: float
    wc_k: float | None
    wp_k: float | None
    call_gap: float | None
    put_gap: float | None
    short_qty: int
    wing_qty: int
    sc_px: float
    sp_px: float
    wc_px: float | None
    wp_px: float | None
    net_credit: float
    theo_max_loss: float


@dataclass
class StageRun:
    entry_date: date
    net: float
    n_adjustments: int
    closed_early: bool
    stages: list[StageSnap]
    entry_theo: float
    entry_net_credit: float
    extra_wing_cost: float = 0.0  # FIX_1: net debit from wing re-establish
    premium_foregone: float = 0.0  # FIX_2: credit lost vs unconstrained
    ledger: list[LedgerRow] = field(default_factory=list)


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def qty_btc(qty: int) -> float:
    return abs(int(qty)) * CV


def net_credit_usd(
    sc: float, sp: float, wc: float | None, wp: float | None, qty: int
) -> float:
    short = (float(sc) + float(sp)) * qty_btc(qty)
    wing = 0.0
    if wc is not None and wp is not None:
        wing = (float(wc) + float(wp)) * qty_btc(qty)
    return short - wing


def theo_max_loss_arith(
    *,
    sc_k: float,
    sp_k: float,
    wc_k: float | None,
    wp_k: float | None,
    sc_px: float,
    sp_px: float,
    wc_px: float | None,
    wp_px: float | None,
    qty: int,
    wing_distance_fallback: float | None = None,
) -> tuple[float, float | None, float | None]:
    qb = qty_btc(qty)
    call_gap = (float(wc_k) - float(sc_k)) if wc_k is not None else None
    put_gap = (float(sp_k) - float(wp_k)) if wp_k is not None else None
    if (
        call_gap is not None
        and put_gap is not None
        and wc_px is not None
        and wp_px is not None
        and call_gap > 0
        and put_gap > 0
    ):
        call_max = call_gap * qb - (float(sc_px) - float(wc_px)) * qb
        put_max = put_gap * qb - (float(sp_px) - float(wp_px)) * qb
        return max(call_max, put_max), call_gap, put_gap
    nc = net_credit_usd(sc_px, sp_px, wc_px, wp_px, qty)
    dist = float(wing_distance_fallback or 0.0)
    return dist * qb - nc, call_gap, put_gap


def theo_max_simple(wing_distance: float, net_credit: float, qty: int) -> float:
    return float(wing_distance) * qty_btc(qty) - float(net_credit)


def make_stage(
    label: str,
    sc_k: float,
    sp_k: float,
    wc_k: float | None,
    wp_k: float | None,
    short_qty: int,
    wing_qty: int,
    sc_px: float,
    sp_px: float,
    wc_px: float | None,
    wp_px: float | None,
) -> StageSnap:
    theo, cg, pg = theo_max_loss_arith(
        sc_k=sc_k,
        sp_k=sp_k,
        wc_k=wc_k,
        wp_k=wp_k,
        sc_px=sc_px,
        sp_px=sp_px,
        wc_px=wc_px,
        wp_px=wp_px,
        qty=short_qty,
        wing_distance_fallback=WING_POINTS,
    )
    return StageSnap(
        label=label,
        sc_k=sc_k,
        sp_k=sp_k,
        wc_k=wc_k,
        wp_k=wp_k,
        call_gap=cg,
        put_gap=pg,
        short_qty=short_qty,
        wing_qty=wing_qty,
        sc_px=sc_px,
        sp_px=sp_px,
        wc_px=wc_px,
        wp_px=wp_px,
        net_credit=net_credit_usd(sc_px, sp_px, wc_px, wp_px, short_qty),
        theo_max_loss=theo,
    )


def filter_base(obs: list[eng.CycleObs]) -> list[eng.CycleObs]:
    out: list[eng.CycleObs] = []
    for o in obs:
        if o.short_dte != 2:
            continue
        if o.fill_package != "maker":
            continue
        if o.strike_mode != "B25":
            continue
        if o.entry_hhmm != "11:00":
            continue
        if o.wing_points != WING_POINTS:
            continue
        if o.wing_call is None or o.wing_put is None:
            continue
        out.append(o)
    return out


def append_led(
    ledger: list[LedgerRow] | None,
    *,
    symbol: str,
    side: str,
    strike: float,
    qty: int,
    price: float,
    fee: float,
    note: str = "",
) -> None:
    if ledger is None:
        return
    ledger.append(
        LedgerRow(
            symbol=symbol,
            side=side,
            strike=float(strike),
            qty=int(qty),
            price=float(price),
            fee=float(fee),
            note=note,
        )
    )


def clamp_short_vs_wing(
    leg: str,
    wanted_k: float,
    wc_k: float | None,
    wp_k: float | None,
    min_gap: float,
    idx: eng.TradeIndex,
    exp: date,
) -> float:
    """
    FIX_2: new short may not come closer than min_gap points to same-side wing.
      call: new_k <= wing_call - min_gap
      put:  new_k >= wing_put + min_gap
    Clamp to nearest available strike on the allowed side of the bound.
    """
    strikes = sorted(idx.strikes_by_expiry.get(exp) or set())
    if not strikes:
        return wanted_k
    if leg == "call" and wc_k is not None:
        limit = float(wc_k) - float(min_gap)
        if wanted_k <= limit + 1e-9:
            return wanted_k
        # too close to wing — pick highest strike <= limit
        allowed = [k for k in strikes if k <= limit + 1e-9 and k > 0]
        if not allowed:
            return wanted_k
        return float(max(allowed))
    if leg == "put" and wp_k is not None:
        limit = float(wp_k) + float(min_gap)
        if wanted_k >= limit - 1e-9:
            return wanted_k
        allowed = [k for k in strikes if k >= limit - 1e-9]
        if not allowed:
            return wanted_k
        return float(min(allowed))
    return wanted_k


def premium_at_strike(
    idx: eng.TradeIndex,
    exp: date,
    leg: str,
    strike: float,
    when: datetime,
    role: str,
) -> float | None:
    fill = eng.nearest_print_prefer(
        idx,
        eng.format_symbol("C" if leg == "call" else "P", strike, exp),
        when,
        eng.PRINT_WINDOW_SEC,
        role,
    )
    if fill is None or fill.price <= 0:
        return None
    return float(fill.price)


def simulate_fix(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    fix_mode: str,
    basket_qty: int = BASKET_QTY,
    collect_ledger: bool = False,
) -> StageRun:
    cfg = WINNER_CFG
    assert cfg.trigger_pct is not None
    adj_b_trig = sweep.adj_b_pct_from_trigger(float(cfg.trigger_pct))
    exp = o.basket_expiry
    short_role_s = sweep.short_role()
    long_role_s = sweep.long_role()
    original_qty = basket_qty
    qty = original_qty
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
    wing_qty = qty if wc_k is not None else 0

    ledger: list[LedgerRow] | None = [] if collect_ledger else None
    spot_e = float(o.spot_entry)
    fees = eng.option_fee(sc_entry, spot_e, qty) + eng.option_fee(sp_entry, spot_e, qty)
    if wc_entry is not None and wp_entry is not None:
        fees += eng.option_fee(wc_entry, spot_e, qty) + eng.option_fee(
            wp_entry, spot_e, qty
        )

    append_led(
        ledger,
        symbol=o.short_call.symbol,
        side="SELL",
        strike=sc_k,
        qty=qty,
        price=sc_entry,
        fee=eng.option_fee(sc_entry, spot_e, qty),
        note="entry short call",
    )
    append_led(
        ledger,
        symbol=o.short_put.symbol,
        side="SELL",
        strike=sp_k,
        qty=qty,
        price=sp_entry,
        fee=eng.option_fee(sp_entry, spot_e, qty),
        note="entry short put",
    )
    if o.wing_call is not None and o.wing_put is not None:
        append_led(
            ledger,
            symbol=o.wing_call.symbol,
            side="BUY",
            strike=float(wc_k or 0),
            qty=qty,
            price=float(wc_entry or 0),
            fee=eng.option_fee(float(wc_entry or 0), spot_e, qty),
            note="entry wing call",
        )
        append_led(
            ledger,
            symbol=o.wing_put.symbol,
            side="BUY",
            strike=float(wp_k or 0),
            qty=qty,
            price=float(wp_entry or 0),
            fee=eng.option_fee(float(wp_entry or 0), spot_e, qty),
            note="entry wing put",
        )

    stages = [
        make_stage(
            "entry",
            sc_k,
            sp_k,
            wc_k,
            wp_k,
            qty,
            wing_qty,
            sc_entry,
            sp_entry,
            wc_entry,
            wp_entry,
        )
    ]
    entry_nc = stages[0].net_credit
    entry_theo = theo_max_simple(WING_POINTS, entry_nc, qty)

    realized = 0.0
    extra_wing_cost = 0.0
    premium_foregone = 0.0
    adj_count = 0
    closed_early = False
    t0 = int(o.entry_utc.timestamp())
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    if t_end <= t0:
        return StageRun(
            entry_date=o.entry_date,
            net=float("nan"),
            n_adjustments=0,
            closed_early=False,
            stages=stages,
            entry_theo=entry_theo,
            entry_net_credit=entry_nc,
            ledger=ledger or [],
        )

    t = t0 + sweep.MONITOR_STEP_SEC
    while t < t_end and not closed_early:
        when = datetime.fromtimestamp(t, tz=UTC)
        spot = ot.spot_at(times, closes, t)
        if spot is None or spot <= 0:
            t += sweep.MONITOR_STEP_SEC
            continue
        sc_now = sweep.premium_at(idx, exp, "call", sc_k, when, for_short_exit=True)
        sp_now = sweep.premium_at(idx, exp, "put", sp_k, when, for_short_exit=True)
        if sc_now is None or sp_now is None:
            t += sweep.MONITOR_STEP_SEC
            continue
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

        action: str | None = None
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

        _kind, leg = action.split(":")
        if adj_count >= sweep.MAX_ADJUSTMENTS_PER_BASKET:
            stages.append(
                make_stage(
                    "force_exit",
                    sc_k,
                    sp_k,
                    wc_k,
                    wp_k,
                    qty,
                    wing_qty,
                    sc_entry,
                    sp_entry,
                    wc_entry,
                    wp_entry,
                )
            )
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
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            append_led(
                ledger,
                symbol="FORCE_EXIT",
                side="CLOSE_ALL",
                strike=0,
                qty=qty,
                price=0,
                fee=exit_fee,
                note=f"force exit net_leg≈{exit_pnl:.4f}",
            )
            closed_early = True
            break

        next_n = adj_count + 1
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=next_n,
            decrease_pct=sweep.ADJUSTMENT_QTY_DECREASE_PCT,
        )
        if close_basket or new_qty is None:
            stages.append(
                make_stage(
                    "force_exit",
                    sc_k,
                    sp_k,
                    wc_k,
                    wp_k,
                    qty,
                    wing_qty,
                    sc_entry,
                    sp_entry,
                    wc_entry,
                    wp_entry,
                )
            )
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
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            break

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
        if not res.success or res.strike is None or res.premium is None:
            t += sweep.MONITOR_STEP_SEC
            continue

        wanted_k = float(res.strike)
        unconstrained_px = float(res.premium)
        fill0 = eng.nearest_print_prefer(
            idx,
            eng.format_symbol("C" if leg == "call" else "P", wanted_k, exp),
            when,
            eng.PRINT_WINDOW_SEC,
            short_role_s,
        )
        if fill0 is not None and fill0.price > 0:
            unconstrained_px = float(fill0.price)

        new_k = wanted_k
        new_fill_px = unconstrained_px
        if fix_mode == FIX_2:
            clamped = clamp_short_vs_wing(
                leg, wanted_k, wc_k, wp_k, WING_POINTS, idx, exp
            )
            if abs(clamped - wanted_k) > 1e-9:
                px_c = premium_at_strike(
                    idx, exp, leg, clamped, when, short_role_s
                )
                if px_c is None:
                    t += sweep.MONITOR_STEP_SEC
                    continue
                # foregone = credit we would have at unconstrained vs clamped
                # (higher premium usually nearer ATM)
                premium_foregone += max(
                    0.0,
                    (unconstrained_px - px_c) * qty_btc(int(new_qty)),
                )
                new_k = clamped
                new_fill_px = px_c
                append_led(
                    ledger,
                    symbol="FIX2_CLAMP",
                    side="LIMIT",
                    strike=new_k,
                    qty=int(new_qty),
                    price=new_fill_px,
                    fee=0.0,
                    note=f"wanted={wanted_k:.0f}→clamp={new_k:.0f}",
                )

        # FIX_1: pre-resolve new wings BEFORE mutating (avoid half-adj)
        fix1_wings: tuple[float, float, float, float] | None = None
        if fix_mode == FIX_1:
            if wc_now is None or wp_now is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            sc_after = new_k if leg == "call" else sc_k
            sp_after = new_k if leg == "put" else sp_k
            wk = eng.pick_wing_strikes(idx, exp, sc_after, sp_after, WING_POINTS)
            if wk is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            nwc_k, nwp_k = wk
            wcf = eng.nearest_print_prefer(
                idx,
                eng.format_symbol("C", nwc_k, exp),
                when,
                eng.PRINT_WINDOW_SEC,
                long_role_s,
            )
            wpf = eng.nearest_print_prefer(
                idx,
                eng.format_symbol("P", nwp_k, exp),
                when,
                eng.PRINT_WINDOW_SEC,
                long_role_s,
            )
            if wcf is None or wpf is None or wcf.price <= 0 or wpf.price <= 0:
                t += sweep.MONITOR_STEP_SEC
                continue
            fix1_wings = (nwc_k, nwp_k, float(wcf.price), float(wpf.price))

        # Exit old short + enter new
        if leg == "call":
            exit_px = sc_now
            realized += eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            fee_x = eng.option_fee(exit_px, spot, qty)
            fee_e = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_x + fee_e
            append_led(
                ledger,
                symbol=eng.format_symbol("C", sc_k, exp),
                side="BUY_TO_CLOSE",
                strike=sc_k,
                qty=qty,
                price=exit_px,
                fee=fee_x,
                note=f"adj{adj_count + 1} exit call",
            )
            append_led(
                ledger,
                symbol=eng.format_symbol("C", new_k, exp),
                side="SELL",
                strike=new_k,
                qty=int(new_qty),
                price=new_fill_px,
                fee=fee_e,
                note=f"adj{adj_count + 1} enter call",
            )
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            exit_px = sp_now
            realized += eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            fee_x = eng.option_fee(exit_px, spot, qty)
            fee_e = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_x + fee_e
            append_led(
                ledger,
                symbol=eng.format_symbol("P", sp_k, exp),
                side="BUY_TO_CLOSE",
                strike=sp_k,
                qty=qty,
                price=exit_px,
                fee=fee_x,
                note=f"adj{adj_count + 1} exit put",
            )
            append_led(
                ledger,
                symbol=eng.format_symbol("P", new_k, exp),
                side="SELL",
                strike=new_k,
                qty=int(new_qty),
                price=new_fill_px,
                fee=fee_e,
                note=f"adj{adj_count + 1} enter put",
            )
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        # Wings
        if wc_k is not None and wp_k is not None and wc_entry is not None and wp_entry is not None:
            if fix_mode == FIX_1 and fix1_wings is not None and wc_now is not None and wp_now is not None:
                nwc_k, nwp_k, nwc_px, nwp_px = fix1_wings
                realized += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                realized += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                fee_wx = eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
                fees += fee_wx
                append_led(
                    ledger,
                    symbol=eng.format_symbol("C", wc_k, exp),
                    side="SELL_TO_CLOSE",
                    strike=wc_k,
                    qty=qty,
                    price=wc_now,
                    fee=eng.option_fee(wc_now, spot, qty),
                    note="FIX1 exit old wing call",
                )
                append_led(
                    ledger,
                    symbol=eng.format_symbol("P", wp_k, exp),
                    side="SELL_TO_CLOSE",
                    strike=wp_k,
                    qty=qty,
                    price=wp_now,
                    fee=eng.option_fee(wp_now, spot, qty),
                    note="FIX1 exit old wing put",
                )
                fee_we = eng.option_fee(nwc_px, spot, int(new_qty)) + eng.option_fee(
                    nwp_px, spot, int(new_qty)
                )
                fees += fee_we
                buy_new = (nwc_px + nwp_px) * qty_btc(int(new_qty))
                keep_val = (wc_now + wp_now) * qty_btc(int(new_qty))
                fee_partial = eng.option_fee(wc_now, spot, qty - int(new_qty)) + eng.option_fee(
                    wp_now, spot, qty - int(new_qty)
                )
                extra_wing_cost += (buy_new - keep_val) + fee_we + (fee_wx - fee_partial)
                append_led(
                    ledger,
                    symbol=eng.format_symbol("C", nwc_k, exp),
                    side="BUY",
                    strike=nwc_k,
                    qty=int(new_qty),
                    price=nwc_px,
                    fee=eng.option_fee(nwc_px, spot, int(new_qty)),
                    note="FIX1 new wing call",
                )
                append_led(
                    ledger,
                    symbol=eng.format_symbol("P", nwp_k, exp),
                    side="BUY",
                    strike=nwp_k,
                    qty=int(new_qty),
                    price=nwp_px,
                    fee=eng.option_fee(nwp_px, spot, int(new_qty)),
                    note="FIX1 new wing put",
                )
                wc_k, wp_k = nwc_k, nwp_k
                wc_entry, wp_entry = nwc_px, nwp_px
                wing_qty = int(new_qty)
            else:
                # FIX_NONE / FIX_2: SELL_PARTIAL only
                closed = qty - int(new_qty)
                if closed > 0 and wc_now is not None and wp_now is not None:
                    realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
                    fees += eng.option_fee(wc_now, spot, closed) + eng.option_fee(
                        wp_now, spot, closed
                    )
                    append_led(
                        ledger,
                        symbol=eng.format_symbol("C", wc_k, exp),
                        side="SELL_PARTIAL",
                        strike=wc_k,
                        qty=closed,
                        price=wc_now,
                        fee=eng.option_fee(wc_now, spot, closed),
                        note="partial wing call",
                    )
                    append_led(
                        ledger,
                        symbol=eng.format_symbol("P", wp_k, exp),
                        side="SELL_PARTIAL",
                        strike=wp_k,
                        qty=closed,
                        price=wp_now,
                        fee=eng.option_fee(wp_now, spot, closed),
                        note="partial wing put",
                    )
                wing_qty = int(new_qty)

        qty = int(new_qty)
        adj_count += 1
        stages.append(
            make_stage(
                f"after_adj_{adj_count}",
                sc_k,
                sp_k,
                wc_k,
                wp_k,
                qty,
                wing_qty,
                sc_entry,
                sp_entry,
                wc_entry,
                wp_entry,
            )
        )
        t += sweep.MONITOR_STEP_SEC

    if not closed_early:
        spot_s = float(o.spot_settle)
        pnl, settle_fee = sweep.settle_legs(
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
        _ = settle_fee
        net = realized - fees
    else:
        net = realized - fees

    return StageRun(
        entry_date=o.entry_date,
        net=net,
        n_adjustments=adj_count,
        closed_early=closed_early,
        stages=stages,
        entry_theo=entry_theo,
        entry_net_credit=entry_nc,
        extra_wing_cost=extra_wing_cost,
        premium_foregone=premium_foregone,
        ledger=ledger or [],
    )


def stage_of(r: StageRun, label: str) -> StageSnap | None:
    for s in r.stages:
        if s.label == label:
            return s
    return None


def widen_stats(runs: list[StageRun]) -> tuple[float, float, float]:
    widen_n = 0
    extras: list[float] = []
    worst_all: list[float] = []
    for r in runs:
        by_lab = {s.label: s for s in r.stages}
        entry = by_lab.get("entry")
        if entry is None:
            continue
        post = [
            by_lab[lab].theo_max_loss
            for lab in ("after_adj_1", "after_adj_2", "force_exit")
            if lab in by_lab
        ]
        worst_all.append(max([entry.theo_max_loss] + post))
        if post and max(post) > entry.theo_max_loss + 1e-12:
            widen_n += 1
            extras.append(max(post) - entry.theo_max_loss)
    n = max(1, len(runs))
    pct = 100.0 * widen_n / n
    worst_theo = max(worst_all) if worst_all else float("nan")
    return pct, worst_theo, statistics.mean(extras) if extras else 0.0


def gap_block(lines: list[str], runs: list[StageRun]) -> None:
    for label in ("entry", "after_adj_1", "after_adj_2", "force_exit"):
        cg: list[float] = []
        pg: list[float] = []
        th: list[float] = []
        n = 0
        for r in runs:
            s = stage_of(r, label)
            if s is None:
                continue
            n += 1
            if s.call_gap is not None:
                cg.append(s.call_gap)
            if s.put_gap is not None:
                pg.append(s.put_gap)
            th.append(s.theo_max_loss)
        if not cg:
            emit(lines, f"  {label}: NOT AVAILABLE")
            continue
        emit(
            lines,
            f"  {label} (n={n}): call_gap avg={statistics.mean(cg):.1f} "
            f"worst(min)={min(cg):.1f} | put_gap avg={statistics.mean(pg):.1f} "
            f"worst(min)={min(pg):.1f} | theo avg={statistics.mean(th):.4f} "
            f"worst(max)={max(th):.4f}",
        )


def emit_part_a(lines: list[str]) -> None:
    emit(lines, "===== PART A: min_short_gap_points (code read) =====")
    emit(lines, "")
    emit(
        lines,
        "Verdict: NOT related to wing gap. It is the minimum distance between",
    )
    emit(lines, "the TWO SHORT strikes (call short vs put short) after Adj B.")
    emit(lines, "")
    emit(lines, "Evidence:")
    emit(
        lines,
        "  models.py:510-512 — comment: 'Min points between the two short",
    )
    emit(lines, "  strikes after Adj B (0 = one strike step)'")
    emit(
        lines,
        "  adj_b.py:120-133, 213-273 — select_adj_b_strike gap guard vs",
    )
    emit(
        lines,
        "  other_short_strike; required_gap = max(strike_step, min_gap)",
    )
    emit(
        lines,
        "  when min_gap>0, else one strike step. call must sit ABOVE put short",
    )
    emit(lines, "  by required_gap; put BELOW call short.")
    emit(
        lines,
        "  adjustment.py:2998-3072 — read at Adj B plan time only (not entry).",
    )
    emit(lines, "")
    emit(lines, "When does it apply? Adjustment (Adj B) only — not entry.")
    emit(lines, "")
    emit(
        lines,
        "If set 0→2000: Adj B refuses new untested shorts closer than 2000 pts",
    )
    emit(
        lines,
        "to the OTHER short (may skip adj or pick farther OTM). Does NOT keep",
    )
    emit(lines, "short↔wing distance at 2000.")
    emit(lines, "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 GAP FIXES — FIX_NONE / FIX_1 wing-follows / FIX_2 inward-limit")
    emit(lines, "=" * 100)
    emit(
        lines,
        "Baseline: dte2 B_only trig70 maker B25 wing=2000 qty=8 11:00 IST HEDGE=OFF",
    )
    emit(lines, "")

    emit_part_a(lines)

    logger.info("Loading cache + index...")
    all_obs, day_span = sweep.load_cycles()
    base = filter_base(all_obs)
    logger.info("base cycles=%s day_span=%s", len(base), day_span)
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()

    emit(lines, "===== PART B: THREE FIXES =====")
    emit(lines, "")

    table_rows: list[dict[str, float | str]] = []
    ledger_by_fix: dict[str, list[LedgerRow]] = {}
    sample_o = next((o for o in base if o.entry_date == LEDGER_DATE), None)

    for fi, fix in enumerate(FIXES):
        logger.info("Running %s ...", fix)
        runs: list[StageRun] = []
        for i, o in enumerate(base):
            if (i + 1) % 50 == 0:
                logger.info("  %s %s/%s", fix, i + 1, len(base))
            want_led = sample_o is not None and o.entry_date == LEDGER_DATE
            runs.append(
                simulate_fix(
                    o,
                    idx,
                    times,
                    closes,
                    fix_mode=fix,
                    collect_ledger=want_led,
                )
            )
        if sample_o is not None:
            for r in runs:
                if r.entry_date == LEDGER_DATE and r.ledger:
                    ledger_by_fix[fix] = r.ledger
                    break

        nets = [r.net for r in runs if math.isfinite(r.net)]
        dates = [r.entry_date for r in runs if math.isfinite(r.net)]
        mean, lo, hi = eng.bootstrap_mean_ci(
            nets, BOOTSTRAP_N, BOOTSTRAP_SEED + fi * 19
        )
        cpd = len(nets) / float(max(1, day_span))
        chron = [n for _, n in sorted(zip(dates, nets), key=lambda z: z[0])]
        mdd = eng.max_drawdown(chron)
        credits = [r.entry_net_credit for r in runs]
        extra = [r.extra_wing_cost for r in runs]
        forgone = [r.premium_foregone for r in runs]
        entry_theo = statistics.mean([r.entry_theo for r in runs])
        pct_w, worst_theo, _avg_ex = widen_stats(runs)

        emit(lines, f"----- {fix}  n={len(nets)} -----")
        emit(
            lines,
            f"  mean/day={mean * cpd:.4f}  median={statistics.median(nets):.4f}  "
            f"ci_lo={lo * cpd:.4f}  ci_hi={hi * cpd:.4f}",
        )
        emit(
            lines,
            f"  worst={min(nets):.4f}  mdd={mdd:.4f}  "
            f"net_credit/cycle={statistics.mean(credits):.4f}",
        )
        emit(
            lines,
            f"  extra_wing_cost/cycle={statistics.mean(extra):.4f}  "
            f"premium_foregone/cycle={statistics.mean(forgone):.4f}",
        )
        emit(
            lines,
            f"  %widen={pct_w:.1f}%  WORST_theo_sample={worst_theo:.4f}  "
            f"theo_entry_avg={entry_theo:.4f}",
        )
        gap_block(lines, runs)
        emit(lines, "")

        table_rows.append(
            {
                "fix": fix,
                "mean_day": mean * cpd,
                "ci_lo": lo * cpd,
                "worst": min(nets),
                "theo_entry": entry_theo,
                "theo_worst": worst_theo,
                "pct_widen": pct_w,
                "extra": statistics.mean(extra)
                if fix == FIX_1
                else (
                    statistics.mean(forgone) if fix == FIX_2 else 0.0
                ),
            }
        )

    emit(lines, "===== FINAL TABLE =====")
    emit(
        lines,
        f"{'fix':<10} {'mean/day':>10} {'ci_lo':>9} {'worst':>9} "
        f"{'theo_entry':>11} {'theo_WORST':>11} {'%widen':>8} {'extra_cost':>11}",
    )
    emit(lines, "-" * 95)
    for r in table_rows:
        emit(
            lines,
            f"{str(r['fix']):<10} {float(r['mean_day']):10.4f} "
            f"{float(r['ci_lo']):9.4f} {float(r['worst']):9.4f} "
            f"{float(r['theo_entry']):11.4f} {float(r['theo_worst']):11.4f} "
            f"{float(r['pct_widen']):7.1f}% {float(r['extra']):11.4f}",
        )
    emit(
        lines,
        "extra_cost column: FIX_1 = avg extra wing roll cost; "
        "FIX_2 = avg premium foregone; FIX_NONE = 0",
    )
    emit(lines, "")

    emit(lines, "===== PART C: LEDGER 2026-02-09 (three fixes side-by-side) =====")
    if sample_o is None or len(ledger_by_fix) < 3:
        emit(
            lines,
            f"NOT AVAILABLE — no cycle on {LEDGER_DATE} or ledger missing "
            f"(found={list(ledger_by_fix.keys())})",
        )
    else:
        emit(
            lines,
            f"entry_date={LEDGER_DATE}  "
            f"shorts={sample_o.short_call_k:.0f}/{sample_o.short_put_k:.0f}  "
            f"wings={sample_o.wing_call_k}/{sample_o.wing_put_k}",
        )
        emit(lines, "")
        max_rows = max(len(ledger_by_fix[f]) for f in FIXES)
        emit(
            lines,
            f"{'#':>3} | "
            f"{'NONE side/k/q/px/fee':<42} | "
            f"{'FIX1 side/k/q/px/fee':<42} | "
            f"{'FIX2 side/k/q/px/fee':<42}",
        )
        emit(lines, "-" * 140)

        def fmt(rows: list[LedgerRow], i: int) -> str:
            if i >= len(rows):
                return f"{'—':<42}"
            r = rows[i]
            return (
                f"{r.side[:12]:<12} {r.strike:7.0f} q{r.qty:<2d} "
                f"{r.price:7.1f} f{r.fee:5.3f}"
            )[:42].ljust(42)

        for i in range(max_rows):
            emit(
                lines,
                f"{i:3d} | {fmt(ledger_by_fix[FIX_NONE], i)} | "
                f"{fmt(ledger_by_fix[FIX_1], i)} | "
                f"{fmt(ledger_by_fix[FIX_2], i)}",
            )
        emit(lines, "")
        emit(lines, "Notes (FIX_NONE / FIX_1 / FIX_2):")
        for fix in FIXES:
            rows = ledger_by_fix[fix]
            emit(lines, f"  {fix}: {len(rows)} rows")
            for r in rows:
                if r.note:
                    emit(
                        lines,
                        f"    {r.side} k={r.strike:.0f} q={r.qty} "
                        f"px={r.price:.2f} fee={r.fee:.4f} [{r.note}]",
                    )
    emit(lines, "")
    emit(lines, "END")

    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
