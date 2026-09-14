#!/usr/bin/env python3
"""
S001 gap protection — wing distance sweep, post-adjustment gap widening,
big-move days, overlapping baskets.

Hedge OFF. Basket: dte2 B_only trig70 maker B25 8 lots 11:00 IST.
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
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
OUT_PATH = RESULTS_DIR / "s001_gap_protection.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
BASKET_QTY = 8
WING_SWEEP = (3000.0, 2000.0, 1500.0, 1000.0, 750.0)
BIG_MOVE_PTS = 4000.0
SPECIAL_PERIODS = (
    (date(2025, 8, 19), date(2025, 8, 21)),
    (date(2025, 8, 28), date(2025, 8, 29)),
    (date(2025, 9, 3), date(2025, 9, 4)),
    (date(2025, 9, 11), date(2025, 9, 13)),
)
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE


@dataclass
class StageSnap:
    label: str  # entry | after_adj_1 | after_adj_2 | force_exit
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
    entry_wing_pct: float


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def qty_btc(qty: int) -> float:
    return abs(int(qty)) * CV


def net_credit_usd(
    sc: float,
    sp: float,
    wc: float | None,
    wp: float | None,
    qty: int,
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
    """
    Deterministic one-sided defined-risk max:
      side_max = gap_points × qtyBTC − side_net_credit
      theo = max(call_side, put_side)
    If wings missing, fallback: wing_distance × qtyBTC − total net credit.
    """
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
        call_credit = (float(sc_px) - float(wc_px)) * qb
        put_credit = (float(sp_px) - float(wp_px)) * qb
        call_max = call_gap * qb - call_credit
        put_max = put_gap * qb - put_credit
        return max(call_max, put_max), call_gap, put_gap
    # Fallback (user formula)
    nc = net_credit_usd(sc_px, sp_px, wc_px, wp_px, qty)
    dist = float(wing_distance_fallback or 0.0)
    return dist * qb - nc, call_gap, put_gap


def theo_max_simple(wing_distance: float, net_credit: float, qty: int) -> float:
    """User formula: (wing_distance × qtyBTC) − net credit."""
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
    wing_distance_fallback: float | None,
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
        wing_distance_fallback=wing_distance_fallback,
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


def filter_wing(obs: list[eng.CycleObs], wing: float) -> list[eng.CycleObs]:
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
        if o.wing_points != wing:
            continue
        if o.wing_call is None or o.wing_put is None:
            continue
        out.append(o)
    return out


def synthesize_wing_750(
    base_2000: list[eng.CycleObs],
    idx: eng.TradeIndex,
    surface: object | None,
) -> list[eng.CycleObs]:
    """750 not in income cache — rebuild wings from same shorts."""
    out: list[eng.CycleObs] = []
    long_role = eng.roles_for_package("maker")[1]
    for o in base_2000:
        wk = eng.pick_wing_strikes(
            idx, o.basket_expiry, float(o.short_call_k), float(o.short_put_k), 750.0
        )
        if wk is None:
            continue
        wc_k, wp_k = wk
        wc = eng.wing_fill_or_surface(
            idx, surface, o.basket_expiry, wc_k, "C", o.entry_utc, long_role
        )
        wp = eng.wing_fill_or_surface(
            idx, surface, o.basket_expiry, wp_k, "P", o.entry_utc, long_role
        )
        if wc is None or wp is None:
            continue
        out.append(
            replace(
                o,
                wing_points=750.0,
                wing_call_k=wc_k,
                wing_put_k=wp_k,
                wing_call=wc,
                wing_put=wp,
                wing_used_surface=(wc.source == "surface" or wp.source == "surface"),
            )
        )
    return out


def simulate_with_stages(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    wing_points: float,
    basket_qty: int = BASKET_QTY,
) -> StageRun:
    """
    Mirror of sweep.simulate_with_adjustments + stage snapshots.
    Uses local wing_points for wing rolls (not module global).
    """
    cfg = WINNER_CFG
    assert cfg.trigger_pct is not None
    allow_adj_b = True
    flat_trigger = float(cfg.trigger_pct)
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

    spot_e = float(o.spot_entry)
    fees = eng.option_fee(sc_entry, spot_e, qty) + eng.option_fee(sp_entry, spot_e, qty)
    if wc_entry is not None and wp_entry is not None:
        fees += eng.option_fee(wc_entry, spot_e, qty) + eng.option_fee(
            wp_entry, spot_e, qty
        )

    stages: list[StageSnap] = [
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
            wing_points,
        )
    ]
    entry_nc = stages[0].net_credit
    short_prem = (sc_entry + sp_entry) * qty_btc(qty)
    wing_cost = short_prem - entry_nc
    entry_wing_pct = (wing_cost / short_prem * 100.0) if short_prem > 1e-12 else float("nan")
    entry_theo = theo_max_simple(wing_points, entry_nc, qty)

    realized = 0.0
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
            entry_wing_pct=entry_wing_pct,
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
        net_dec = sweep.mtm_net(
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
        _ = net_dec  # B_only — profit-at-trigger only for Adj A

        action: str | None = None
        if allow_adj_b:
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
                    wing_points,
                )
            )
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
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
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
                    wing_points,
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

        new_k: float | None = None
        new_fill_px: float | None = None
        if kind == "A":
            if leg == "call":
                target, _, _, _ = sweep.compute_adjustment_target_premium(
                    sp_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            else:
                target, _, _, _ = sweep.compute_adjustment_target_premium(
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
            new_fill_px = float(new_fill.price)
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
            if not res.success or res.strike is None or res.premium is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_k = float(res.strike)
            fill = eng.nearest_print_prefer(
                idx,
                eng.format_symbol("C" if leg == "call" else "P", new_k, exp),
                when,
                eng.PRINT_WINDOW_SEC,
                short_role_s,
            )
            new_fill_px = float(fill.price) if fill is not None else float(res.premium)

        assert new_k is not None and new_fill_px is not None
        if leg == "call":
            exit_px = sc_now
            realized += eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            fee_exit = eng.option_fee(exit_px, spot, qty)
            fee_entry = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_exit + fee_entry
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            exit_px = sp_now
            realized += eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            fee_exit = eng.option_fee(exit_px, spot, qty)
            fee_entry = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_exit + fee_entry
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        if wc_k is not None and wp_k is not None and wc_entry is not None and wp_entry is not None:
            roll = False
            if leg == "call" and new_k >= wc_k - 1e-9:
                roll = True
            if leg == "put" and new_k <= wp_k + 1e-9:
                roll = True
            if roll:
                if wc_now is not None and wp_now is not None:
                    realized += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                    fees += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                        wp_now, spot, qty
                    )
                wk = eng.pick_wing_strikes(idx, exp, sc_k, sp_k, wing_points)
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
                        fees += eng.option_fee(wc_entry, spot, int(new_qty))
                        fees += eng.option_fee(wp_entry, spot, int(new_qty))
                        wing_qty = int(new_qty)
                    else:
                        wc_k = wp_k = None
                        wc_entry = wp_entry = None
                        wing_qty = 0
                else:
                    wc_k = wp_k = None
                    wc_entry = wp_entry = None
                    wing_qty = 0
            elif int(new_qty) < qty:
                closed = qty - int(new_qty)
                if wc_now is not None and wp_now is not None and closed > 0:
                    realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
                    fees += eng.option_fee(wc_now, spot, closed) + eng.option_fee(
                        wp_now, spot, closed
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
                wing_points,
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
        entry_wing_pct=entry_wing_pct,
    )


def find_big_move_days(
    times: list[int], closes: list[float]
) -> list[tuple[date, float, datetime, datetime]]:
    """
    Days where some 24h window starting that calendar day (UTC date of t)
    has |spot(t+24h)-spot(t)| >= 4000. Returns (day, max_abs_move, t0, t1).
    Sampled every 60 minutes for speed.
    """
    by_day: dict[date, tuple[float, datetime, datetime]] = {}
    step = 60  # sample every 60 bars (~1h on 1m data)
    n = len(times)
    j = 0
    for i in range(0, n, step):
        t0 = times[i]
        t1_target = t0 + 86400
        while j < n and times[j] < t1_target:
            j += 1
        if j >= n:
            break
        if j > 0 and abs(times[j - 1] - t1_target) <= abs(times[j] - t1_target):
            j_use = j - 1
        else:
            j_use = j
        if abs(times[j_use] - t1_target) > 5 * 60:
            continue
        move = abs(float(closes[j_use]) - float(closes[i]))
        if move < BIG_MOVE_PTS:
            continue
        day = datetime.fromtimestamp(t0, tz=UTC).date()
        dt0 = datetime.fromtimestamp(t0, tz=UTC)
        dt1 = datetime.fromtimestamp(times[j_use], tz=UTC)
        prev = by_day.get(day)
        if prev is None or move > prev[0]:
            by_day[day] = (move, dt0, dt1)
    return [(d, v[0], v[1], v[2]) for d, v in sorted(by_day.items())]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 GAP PROTECTION — wing sweep / post-adj gap / big-moves / overlap")
    emit(lines, "=" * 100)
    emit(
        lines,
        "FIXED: dte2 B_only trig70 maker B25 qty=8 entry=11:00 IST  HEDGE=OFF",
    )
    emit(lines, "")

    logger.info("Loading cache...")
    all_obs, day_span = sweep.load_cycles()
    base_2000 = filter_wing(all_obs, 2000.0)
    logger.info("wing=2000 cycles: %s  day_span=%s", len(base_2000), day_span)

    logger.info("Building trade index + spot + surface...")
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    surface = eng.load_surface_optional()

    # =====================================================================
    # PART A
    # =====================================================================
    emit(lines, "===== PART A: WING DISTANCE SWEEP =====")
    emit(
        lines,
        "theo_max_simple = (wing_distance × qtyBTC) − net_credit   "
        f"[qtyBTC={BASKET_QTY}×{CV}={qty_btc(BASKET_QTY)}]",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'wing':>6} {'n':>4} {'mean/day':>10} {'median':>9} {'ci_lo':>9} "
        f"{'ci_hi':>9} {'worst':>9} {'theo_max':>9} {'mdd':>9} "
        f"{'net_cred':>9} {'wing%':>7}",
    )
    emit(lines, "-" * 110)

    part_a_runs: dict[float, list[StageRun]] = {}
    for wi, wing in enumerate(WING_SWEEP):
        logger.info("PART A wing=%s ...", int(wing))
        if wing == 750.0:
            cycles = synthesize_wing_750(base_2000, idx, surface)
            note = f"synthesized from wing=2000 shorts (n={len(cycles)})"
        else:
            cycles = filter_wing(all_obs, wing)
            note = "from cache"
        logger.info("  cycles=%s (%s)", len(cycles), note)
        runs: list[StageRun] = []
        # Temporarily align module WING_POINTS for any helper that still reads it
        old_w = sweep.WING_POINTS
        sweep.WING_POINTS = wing
        try:
            for i, o in enumerate(cycles):
                if (i + 1) % 50 == 0:
                    logger.info("  sim %s/%s wing=%s", i + 1, len(cycles), int(wing))
                runs.append(
                    simulate_with_stages(
                        o, idx, times, closes, wing_points=wing, basket_qty=BASKET_QTY
                    )
                )
        finally:
            sweep.WING_POINTS = old_w

        part_a_runs[wing] = runs
        nets = [r.net for r in runs if math.isfinite(r.net)]
        if not nets:
            emit(
                lines,
                f"{int(wing):6d} {0:4d}  NOT AVAILABLE",
            )
            continue
        mean, lo, hi = eng.bootstrap_mean_ci(
            nets, BOOTSTRAP_N, BOOTSTRAP_SEED + wi * 13
        )
        cpd = len(nets) / float(max(1, day_span))
        mean_day = mean * cpd
        dates = [r.entry_date for r in runs if math.isfinite(r.net)]
        chron = [n for _, n in sorted(zip(dates, nets), key=lambda z: z[0])]
        mdd = eng.max_drawdown(chron)
        theo_vals = [r.entry_theo for r in runs]
        credits = [r.entry_net_credit for r in runs]
        wing_pcts = [r.entry_wing_pct for r in runs if math.isfinite(r.entry_wing_pct)]
        emit(
            lines,
            f"{int(wing):6d} {len(nets):4d} {mean_day:10.4f} "
            f"{statistics.median(nets):9.4f} {lo * cpd:9.4f} {hi * cpd:9.4f} "
            f"{min(nets):9.4f} {statistics.mean(theo_vals):9.4f} {mdd:9.4f} "
            f"{statistics.mean(credits):9.4f} {statistics.mean(wing_pcts):7.2f}",
        )
        emit(
            lines,
            f"       note={note}  "
            f"actual_worst={min(nets):.4f}  "
            f"mean_theo_max_simple={statistics.mean(theo_vals):.4f}  "
            f"max_theo_max_simple={max(theo_vals):.4f}",
        )
    emit(lines, "")
    emit(
        lines,
        "Columns: mean/day = mean/cycle × n/day_span; "
        "theo_max = mean of (wing_dist×qtyBTC − net_credit) at ENTRY; "
        "wing% = wing debit / short premium × 100.",
    )
    emit(lines, "")

    # =====================================================================
    # PART B — live wing 2000 stages
    # =====================================================================
    emit(lines, "===== PART B: POST-ADJUSTMENT GAP (wing=2000 live) =====")
    runs2000 = part_a_runs.get(2000.0) or []
    if not runs2000:
        # ensure we have them
        old_w = sweep.WING_POINTS
        sweep.WING_POINTS = 2000.0
        try:
            runs2000 = [
                simulate_with_stages(
                    o, idx, times, closes, wing_points=2000.0, basket_qty=BASKET_QTY
                )
                for o in base_2000
            ]
        finally:
            sweep.WING_POINTS = old_w

    def stage_of(r: StageRun, label: str) -> StageSnap | None:
        for s in r.stages:
            if s.label == label:
                return s
        return None

    def summarize_gaps(label: str) -> None:
        call_gaps: list[float] = []
        put_gaps: list[float] = []
        theos: list[float] = []
        n_have = 0
        for r in runs2000:
            s = stage_of(r, label)
            if s is None:
                continue
            n_have += 1
            if s.call_gap is not None:
                call_gaps.append(s.call_gap)
            if s.put_gap is not None:
                put_gaps.append(s.put_gap)
            theos.append(s.theo_max_loss)
        emit(lines, f"  --- {label} (n={n_have}) ---")
        if not call_gaps:
            emit(lines, "    NOT AVAILABLE")
            return
        emit(
            lines,
            f"    call_gap avg={statistics.mean(call_gaps):.1f}  "
            f"worst(min)={min(call_gaps):.1f}  "
            f"put_gap avg={statistics.mean(put_gaps):.1f}  "
            f"worst(min)={min(put_gaps):.1f}",
        )
        emit(
            lines,
            f"    theo_max_loss avg={statistics.mean(theos):.4f}  "
            f"worst(max)={max(theos):.4f}",
        )

    emit(lines, f"n_cycles={len(runs2000)}")
    summarize_gaps("entry")
    summarize_gaps("after_adj_1")
    summarize_gaps("after_adj_2")
    summarize_gaps("force_exit")
    emit(lines, "")

    # Where is theo max largest across stages?
    stage_labels = ("entry", "after_adj_1", "after_adj_2", "force_exit")
    peak_counts = {lab: 0 for lab in stage_labels}
    widen_n = 0
    widen_amt: list[float] = []
    for r in runs2000:
        by_lab = {s.label: s for s in r.stages}
        entry = by_lab.get("entry")
        if entry is None:
            continue
        best_lab = "entry"
        best_theo = entry.theo_max_loss
        for lab in stage_labels[1:]:
            s = by_lab.get(lab)
            if s is not None and s.theo_max_loss > best_theo + 1e-12:
                best_theo = s.theo_max_loss
                best_lab = lab
        peak_counts[best_lab] += 1

        # After any adj, is theo > entry theo?
        post_theos = [
            by_lab[lab].theo_max_loss
            for lab in ("after_adj_1", "after_adj_2", "force_exit")
            if lab in by_lab
        ]
        if post_theos and max(post_theos) > entry.theo_max_loss + 1e-12:
            widen_n += 1
            widen_amt.append(max(post_theos) - entry.theo_max_loss)

    emit(lines, "Max theoretical loss — which stage is largest (count of cycles):")
    for lab in stage_labels:
        emit(
            lines,
            f"  {lab:<14} {peak_counts[lab]:4d}  "
            f"({100.0 * peak_counts[lab] / max(1, len(runs2000)):.1f}%)",
        )
    emit(lines, "")
    pct_widen = 100.0 * widen_n / max(1, len(runs2000))
    emit(
        lines,
        f"Cycles where post-adj theo_max > entry theo_max: "
        f"{widen_n} / {len(runs2000)} = {pct_widen:.1f}%",
    )
    if widen_amt:
        emit(
            lines,
            f"  Among those, extra theo loss: avg=+{statistics.mean(widen_amt):.4f}  "
            f"median=+{statistics.median(widen_amt):.4f}  "
            f"max=+{max(widen_amt):.4f}",
        )
    else:
        emit(lines, "  (no widening observed)")
    emit(lines, "")
    emit(
        lines,
        "theo_max at each stage = max(call_gap, put_gap side defined-risk): "
        "gap×qtyBTC − side_net_credit. "
        "Entry also reports simple (wing_dist×qtyBTC − total net_credit).",
    )
    emit(lines, "")

    # Sample one cycle detail
    sample = runs2000[len(runs2000) // 2] if runs2000 else None
    if sample is not None:
        emit(lines, f"Example cycle stages entry_date={sample.entry_date}:")
        emit(
            lines,
            f"  {'stage':<14} {'sc':>8} {'wc':>8} {'cgap':>7} "
            f"{'sp':>8} {'wp':>8} {'pgap':>7} {'sq':>3} {'wq':>3} "
            f"{'net_cred':>9} {'theo_max':>9}",
        )
        for s in sample.stages:
            emit(
                lines,
                f"  {s.label:<14} {s.sc_k:8.0f} "
                f"{(s.wc_k or 0):8.0f} {(s.call_gap or 0):7.0f} "
                f"{s.sp_k:8.0f} {(s.wp_k or 0):8.0f} {(s.put_gap or 0):7.0f} "
                f"{s.short_qty:3d} {s.wing_qty:3d} "
                f"{s.net_credit:9.4f} {s.theo_max_loss:9.4f}",
            )
        emit(lines, "")

    # =====================================================================
    # PART C
    # =====================================================================
    emit(lines, "===== PART C: BIG-MOVE DAYS (|Δ|≥4000 in 24h) =====")
    by_date = {r.entry_date: r for r in runs2000}
    big = find_big_move_days(times, closes)
    emit(lines, f"Big-move days found: {len(big)}")
    emit(
        lines,
        f"{'day':<12} {'|move|':>8} {'bsk_pnl':>10} {'theo_cap':>10} "
        f"{'act/cap%':>10}",
    )
    emit(lines, "-" * 60)
    for day, move, _a, _b in big:
        # basket open if entry_date in [day-2, day] roughly — cycle covering day
        hit: StageRun | None = None
        for r in runs2000:
            # open from entry to expiry settle
            if r.entry_date <= day <= r.entry_date + timedelta(days=2):
                hit = r
                break
        if hit is None:
            emit(
                lines,
                f"{day.isoformat():<12} {move:8.1f} {'NO_CYCLE':>10} "
                f"{'N/A':>10} {'N/A':>10}",
            )
            continue
        cap = hit.entry_theo
        # actual loss = -min(0, net) relative to cap; or if net negative, |net|/cap
        if hit.net < 0 and cap > 1e-12:
            pct = abs(hit.net) / cap * 100.0
        elif hit.net >= 0:
            pct = 0.0
        else:
            pct = float("nan")
        emit(
            lines,
            f"{day.isoformat():<12} {move:8.1f} {hit.net:10.4f} "
            f"{cap:10.4f} {pct:9.1f}%",
        )
    emit(lines, "")
    emit(lines, "Special periods:")
    for a, b in SPECIAL_PERIODS:
        emit(lines, f"  {a} → {b}:")
        d = a
        any_row = False
        while d <= b:
            r = by_date.get(d)
            if r is not None:
                any_row = True
                pct = (
                    abs(r.net) / r.entry_theo * 100.0
                    if r.net < 0 and r.entry_theo > 1e-12
                    else 0.0 if r.net >= 0 else float("nan")
                )
                emit(
                    lines,
                    f"    entry={d}  pnl={r.net:.4f}  "
                    f"theo_cap={r.entry_theo:.4f}  act/cap={pct:.1f}%",
                )
            d += timedelta(days=1)
        if not any_row:
            emit(lines, "    NO CYCLES with entry_date in this window")
    emit(lines, "")

    # =====================================================================
    # PART D
    # =====================================================================
    emit(lines, "===== PART D: OVERLAPPING BASKETS =====")
    intervals: list[tuple[int, int, StageRun]] = []
    for r, o in zip(runs2000, base_2000):
        t0 = int(o.entry_utc.timestamp())
        exp = o.basket_expiry
        t1 = int(
            datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp()
        )
        if t1 > t0:
            intervals.append((t0, t1, r))

    # Sweep line for max concurrency
    events: list[tuple[int, int]] = []  # (ts, +1 open / -1 close)
    for t0, t1, _r in intervals:
        events.append((t0, +1))
        events.append((t1, -1))
    events.sort(key=lambda x: (x[0], -x[1]))  # opens before closes at same ts
    cur = 0
    max_open = 0
    n_with_overlap = 0  # cycles that share time with another
    # For each cycle, count others overlapping
    for i, (a0, a1, _ra) in enumerate(intervals):
        others = 0
        for j, (b0, b1, _rb) in enumerate(intervals):
            if i == j:
                continue
            if a0 < b1 and b0 < a1:
                others += 1
        if others > 0:
            n_with_overlap += 1
    for ts, delta in events:
        cur += delta
        if cur > max_open:
            max_open = cur
        _ = ts

    emit(
        lines,
        f"Cycles with ≥1 overlapping peer: {n_with_overlap} / {len(intervals)} "
        f"({100.0 * n_with_overlap / max(1, len(intervals)):.1f}%)",
    )
    emit(lines, f"Max baskets open at once: {max_open}")

    # Worst-case combined cap: at the timestamp of max concurrency, sum entry theo
    # Re-scan to find a peak set
    opens: list[StageRun] = []
    peak_set: list[StageRun] = []
    # rebuild with run identity
    ev2: list[tuple[int, int, StageRun]] = []
    for t0, t1, r in intervals:
        ev2.append((t0, +1, r))
        ev2.append((t1, -1, r))
    ev2.sort(key=lambda x: (x[0], -x[1]))
    active: list[StageRun] = []
    for _ts, delta, r in ev2:
        if delta == +1:
            active.append(r)
            if len(active) > len(peak_set):
                peak_set = list(active)
        else:
            # remove one matching entry_date
            for k, a in enumerate(active):
                if a.entry_date == r.entry_date and abs(a.net - r.net) < 1e-9:
                    active.pop(k)
                    break
    if peak_set:
        combined_cap = sum(r.entry_theo for r in peak_set)
        emit(
            lines,
            f"Worst-case combined loss cap at max overlap: {combined_cap:.4f} "
            f"(sum of entry theo_max_simple for {len(peak_set)} open baskets)",
        )
        emit(
            lines,
            "  open entry_dates: "
            + ", ".join(sorted(r.entry_date.isoformat() for r in peak_set)),
        )
    else:
        emit(lines, "Worst-case combined cap: NOT AVAILABLE")
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
