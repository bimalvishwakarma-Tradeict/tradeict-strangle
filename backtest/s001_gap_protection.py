#!/usr/bin/env python3
"""
S001 gap protection + live wing_roll ON/OFF compare.

PART A: document live wing_roll path (read-only from adjustment.py / logic.py)
PART B: backtest sim copies live wing_roll behaviour
PART C: wing_dist × roll OFF/ON (8 configs)

Output: backtest/results/s001_wing_roll.txt
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass
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
OUT_PATH = RESULTS_DIR / "s001_wing_roll.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
BASKET_QTY = 8
WING_DISTS = (3000.0, 2000.0, 1500.0, 1000.0)
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE


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
    n_wing_rolls: int = 0


@dataclass
class CfgRow:
    wing: float
    roll_on: bool
    n: int
    mean_day: float
    ci_lo: float
    ci_hi: float
    worst: float
    mdd: float
    net_credit: float
    entry_theo_avg: float
    worst_stage_theo_avg: float
    pct_widened: float
    avg_extra: float
    max_extra: float
    n_wing_rolls: int
    gap_lines: list[str]


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


def would_cross_wing(
    leg: str, new_k: float, wc_k: float | None, wp_k: float | None
) -> bool:
    """
    Live detect — adjustment.py:3166-3169 (Adj B) and logic.py:1564-1567 (Adj A):
      call: new_k >= wing_k ; put: new_k <= wing_k
    """
    if leg == "call" and wc_k is not None:
        return float(new_k) >= float(wc_k) - 1e-9
    if leg == "put" and wp_k is not None:
        return float(new_k) <= float(wp_k) + 1e-9
    return False


def partial_reduce_wings(
    *,
    qty: int,
    new_qty: int,
    wc_entry: float | None,
    wp_entry: float | None,
    wc_now: float | None,
    wp_now: float | None,
    spot: float,
    realized: float,
    fees: float,
) -> tuple[float, float]:
    """decrease_step wing qty cut — adjustment.py ~2374/_reduce_open_wings_to_qty."""
    closed = qty - int(new_qty)
    if closed <= 0:
        return realized, fees
    if wc_now is not None and wp_now is not None and wc_entry is not None and wp_entry is not None:
        realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
        realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
        fees += eng.option_fee(wc_now, spot, closed) + eng.option_fee(
            wp_now, spot, closed
        )
    return realized, fees


def simulate_with_stages(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    wing_points: float,
    wing_roll_enabled: bool,
    basket_qty: int = BASKET_QTY,
) -> StageRun:
    """
    Mirror sweep.simulate_with_adjustments + live wing_roll path.

    wing_roll_enabled=False → WING_ROLL_OFF: only SELL_PARTIAL on qty step
      (prior backtest baseline for Adj B; never re-strikes wings).
    wing_roll_enabled=True → WING_ROLL_ON: when short would cross same-side
      wing, roll that wing only (adjustment.py 1559-1847).
    """
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
    entry_theo = theo_max_simple(wing_points, entry_nc, qty)

    realized = 0.0
    adj_count = 0
    n_wing_rolls = 0
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

        # Adj B strike — same as sweep
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

        # --- Live wing_roll detect (adjustment.py:3150-3171) ---
        crosses = would_cross_wing(leg, new_k, wc_k, wp_k)
        do_wing_roll = bool(wing_roll_enabled and crosses)

        # WING_ROLL_ON but no same-side wing open → live sets wing_roll_active=False
        # (adjustment.py:1588-1593). Treat as no roll.
        if do_wing_roll:
            if leg == "call" and (wc_k is None or wc_entry is None):
                do_wing_roll = False
            if leg == "put" and (wp_k is None or wp_entry is None):
                do_wing_roll = False

        # If roll ON and crosses: pre-select new wing via pick_wing_strikes
        # (live: resolve_wing_strikes points mode — adjustment.py:1634-1662)
        new_wing_k: float | None = None
        new_wing_px: float | None = None
        if do_wing_roll:
            # After this adj, shorts will be (new_k on triggered, other unchanged)
            sc_after = new_k if leg == "call" else sc_k
            sp_after = new_k if leg == "put" else sp_k
            wk = eng.pick_wing_strikes(idx, exp, sc_after, sp_after, wing_points)
            if wk is None:
                # live WING_ROLL_ABORT: no wing beyond new short — skip adj
                # (adjustment.py:1663-1684)
                t += sweep.MONITOR_STEP_SEC
                continue
            wc_pick_k, wp_pick_k = wk
            new_wing_k = wc_pick_k if leg == "call" else wp_pick_k
            wfill = eng.nearest_print_prefer(
                idx,
                eng.format_symbol("C" if leg == "call" else "P", new_wing_k, exp),
                when,
                eng.PRINT_WINDOW_SEC,
                long_role_s,
            )
            if wfill is None or wfill.price <= 0:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_wing_px = float(wfill.price)

        # --- Execute short exit + entry ---
        if leg == "call":
            exit_px = sc_now
            realized += eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            fees += eng.option_fee(exit_px, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            exit_px = sp_now
            realized += eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            fees += eng.option_fee(exit_px, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        # --- Wings ---
        if wc_k is not None and wp_k is not None and wc_entry is not None and wp_entry is not None:
            if do_wing_roll and new_wing_k is not None and new_wing_px is not None:
                # Live sequence after short flat (adjustment.py:1795-1862):
                # 1) close OLD same-side wing (qty = wing_leg.quantity or new_qty)
                # 2) buy NEW wing at wing_qty = new_qty
                # Other wing: later reduced to new_qty (2374-2391)
                n_wing_rolls += 1
                old_wing_qty = qty  # before decrease; live uses wing_leg.quantity
                if leg == "call":
                    if wc_now is not None:
                        realized += eng.cash_pnl(
                            wc_entry, wc_now, old_wing_qty, is_long=True
                        )
                        fees += eng.option_fee(wc_now, spot, old_wing_qty)
                    wc_k = float(new_wing_k)
                    wc_entry = float(new_wing_px)
                    fees += eng.option_fee(wc_entry, spot, int(new_qty))
                    # other wing (put) partial to new_qty
                    closed = qty - int(new_qty)
                    if closed > 0 and wp_now is not None:
                        realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
                        fees += eng.option_fee(wp_now, spot, closed)
                else:
                    if wp_now is not None:
                        realized += eng.cash_pnl(
                            wp_entry, wp_now, old_wing_qty, is_long=True
                        )
                        fees += eng.option_fee(wp_now, spot, old_wing_qty)
                    wp_k = float(new_wing_k)
                    wp_entry = float(new_wing_px)
                    fees += eng.option_fee(wp_entry, spot, int(new_qty))
                    closed = qty - int(new_qty)
                    if closed > 0 and wc_now is not None:
                        realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
                        fees += eng.option_fee(wc_now, spot, closed)
                wing_qty = int(new_qty)
            else:
                # WING_ROLL_OFF or no cross: SELL_PARTIAL only (baseline)
                # Live with roll disabled clamps Adj A; Adj B rarely crosses.
                realized, fees = partial_reduce_wings(
                    qty=qty,
                    new_qty=int(new_qty),
                    wc_entry=wc_entry,
                    wp_entry=wp_entry,
                    wc_now=wc_now,
                    wp_now=wp_now,
                    spot=float(spot),
                    realized=realized,
                    fees=fees,
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
        n_wing_rolls=n_wing_rolls,
    )


def stage_of(r: StageRun, label: str) -> StageSnap | None:
    for s in r.stages:
        if s.label == label:
            return s
    return None


def gap_summary_lines(runs: list[StageRun]) -> list[str]:
    out: list[str] = []
    for label in ("entry", "after_adj_1", "after_adj_2", "force_exit"):
        call_gaps: list[float] = []
        put_gaps: list[float] = []
        theos: list[float] = []
        n_have = 0
        for r in runs:
            s = stage_of(r, label)
            if s is None:
                continue
            n_have += 1
            if s.call_gap is not None:
                call_gaps.append(s.call_gap)
            if s.put_gap is not None:
                put_gaps.append(s.put_gap)
            theos.append(s.theo_max_loss)
        if not call_gaps:
            out.append(f"  {label}: n=0 NOT AVAILABLE")
            continue
        out.append(
            f"  {label} (n={n_have}): "
            f"call_gap avg={statistics.mean(call_gaps):.1f} "
            f"worst(min)={min(call_gaps):.1f} | "
            f"put_gap avg={statistics.mean(put_gaps):.1f} "
            f"worst(min)={min(put_gaps):.1f} | "
            f"theo avg={statistics.mean(theos):.4f} "
            f"worst(max)={max(theos):.4f}"
        )
    return out


def widen_stats(runs: list[StageRun]) -> tuple[float, float, float, float]:
    """Returns pct_widened, avg_extra, max_extra, avg_worst_stage_theo."""
    widen_n = 0
    extras: list[float] = []
    worst_stage_theos: list[float] = []
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
        all_t = [entry.theo_max_loss] + post
        worst_stage_theos.append(max(all_t))
        if post and max(post) > entry.theo_max_loss + 1e-12:
            widen_n += 1
            extras.append(max(post) - entry.theo_max_loss)
    n = max(1, len(runs))
    pct = 100.0 * widen_n / n
    avg_ex = statistics.mean(extras) if extras else 0.0
    max_ex = max(extras) if extras else 0.0
    avg_w = statistics.mean(worst_stage_theos) if worst_stage_theos else float("nan")
    return pct, avg_ex, max_ex, avg_w


def emit_part_a_report(lines: list[str]) -> None:
    emit(lines, "===== PART A: LIVE wing_roll CODE READ (no changes) =====")
    emit(lines, "")
    emit(lines, "1) plan.wing_roll TRUE kab hota hai?")
    emit(
        lines,
        "   Adj B planner — adjustment.py:3150-3185:",
    )
    emit(
        lines,
        "     wing_roll_with_short_enabled (default True) AND open same-side wing",
    )
    emit(
        lines,
        "     AND new short would CROSS wing:",
    )
    emit(
        lines,
        "       call: new_k >= wing_k  (lines 3166-3168)",
    )
    emit(
        lines,
        "       put:  new_k <= wing_k  (lines 3167-3169)",
    )
    emit(
        lines,
        "   Adj A path — logic.py:1532-1572 (same cross test; skip clamp when roll on).",
    )
    emit(lines, "")
    emit(lines, "2) Naya wing strike kaise chuna jata hai?")
    emit(
        lines,
        "   adjustment.py:1634-1662 → resolve_wing_strikes (wing_select.py)",
    )
    emit(
        lines,
        "   Inputs: NEW short_call_k / short_put_k after adj (plan.new_strike on",
    )
    emit(
        lines,
        "   triggered side), mode=wing_strike_mode (default 'points'),",
    )
    emit(
        lines,
        "   points_away=wing_points_away (default 2000).",
    )
    emit(
        lines,
        "   Points pick: call = nearest strike >= short+points; put <= short-points;",
    )
    emit(
        lines,
        "   else chain_end farthest OTM (wing_select.py:148-196).",
    )
    emit(
        lines,
        "   Only SAME-SIDE pick used: wing_call if triggered call else wing_put",
    )
    emit(lines, "   (adjustment.py:1658-1662).")
    emit(lines, "")
    emit(lines, "3) Wing qty on roll?")
    emit(
        lines,
        "   Old wing close: quantity=wing_leg.quantity or new_qty",
    )
    emit(lines, "   (adjustment.py:1808-1810).")
    emit(
        lines,
        "   New wing buy: wing_qty = int(new_qty)  (adjustment.py:1847-1859).",
    )
    emit(
        lines,
        "   Other wing later cut to new_qty via _reduce_open_wings_to_qty",
    )
    emit(lines, "   (adjustment.py:2374-2391; rolled wing id skipped).")
    emit(lines, "")
    emit(lines, "4) wing_roll_active → FALSE kab?")
    emit(
        lines,
        "   NOTE: user cited ~line 1395; current file sets False at",
    )
    emit(
        lines,
        "   adjustment.py:1588-1593 — plan.wing_roll was True but no open",
    )
    emit(
        lines,
        "   same-side wing leg (wing_call/wing_put) found → skip roll,",
    )
    emit(lines, "   continue short-only adjustment.")
    emit(lines, "")
    emit(lines, "5) wing_roll=True around former ~1720?")
    emit(
        lines,
        "   Current line 1720 is legacy SL cancel (not wing_roll).",
    )
    emit(
        lines,
        "   wing_roll=True on AdjustmentResult at adjustment.py:1918 —",
    )
    emit(
        lines,
        "   WING_ROLL_ABORT after old wing closed but NEW wing entry failed;",
    )
    emit(
        lines,
        "   triggered side left flat (is_partial=True). Also returned on",
    )
    emit(
        lines,
        "   success path as wing_roll=wing_roll_active (e.g. :2186, :2949).",
    )
    emit(lines, "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 WING ROLL — live path report + OFF/ON wing-distance compare")
    emit(lines, "=" * 100)
    emit(
        lines,
        "FIXED: dte2 B_only trig70 maker B25 qty=8 entry=11:00 IST  HEDGE=OFF",
    )
    emit(lines, "Configs: wing ∈ {3000,2000,1500,1000} × roll OFF/ON = 8")
    emit(lines, "")

    emit_part_a_report(lines)

    logger.info("Loading cache + index...")
    all_obs, day_span = sweep.load_cycles()
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()

    emit(lines, "===== PART B: BACKTEST wing_roll IMPLEMENTATION =====")
    emit(
        lines,
        "WING_ROLL_OFF: qty decrease → SELL_PARTIAL wings only (no re-strike).",
    )
    emit(
        lines,
        "WING_ROLL_ON: if new short crosses same-side wing → close that wing,",
    )
    emit(
        lines,
        "  pick new wing via pick_wing_strikes(=points resolve), buy at new_qty;",
    )
    emit(lines, "  other wing SELL_PARTIAL to new_qty. Comments cite live lines.")
    emit(lines, "")

    emit(lines, "===== PART C: COMPARE (8 configs) =====")
    emit(lines, "")

    rows: list[CfgRow] = []
    cfg_i = 0
    for wing in WING_DISTS:
        cycles = filter_wing(all_obs, wing)
        for roll_on in (False, True):
            label = "ON" if roll_on else "OFF"
            logger.info(
                "Config wing=%s roll=%s cycles=%s", int(wing), label, len(cycles)
            )
            runs: list[StageRun] = []
            for i, o in enumerate(cycles):
                if (i + 1) % 50 == 0:
                    logger.info(
                        "  sim %s/%s wing=%s roll=%s",
                        i + 1,
                        len(cycles),
                        int(wing),
                        label,
                    )
                runs.append(
                    simulate_with_stages(
                        o,
                        idx,
                        times,
                        closes,
                        wing_points=wing,
                        wing_roll_enabled=roll_on,
                        basket_qty=BASKET_QTY,
                    )
                )
            nets = [r.net for r in runs if math.isfinite(r.net)]
            dates = [r.entry_date for r in runs if math.isfinite(r.net)]
            if not nets:
                emit(lines, f"wing={int(wing)} roll={label}: NOT AVAILABLE")
                continue
            mean, lo, hi = eng.bootstrap_mean_ci(
                nets, BOOTSTRAP_N, BOOTSTRAP_SEED + cfg_i * 17
            )
            cfg_i += 1
            cpd = len(nets) / float(max(1, day_span))
            chron = [n for _, n in sorted(zip(dates, nets), key=lambda z: z[0])]
            mdd = eng.max_drawdown(chron)
            credits = [r.entry_net_credit for r in runs]
            entry_theos = [r.entry_theo for r in runs]
            pct_w, avg_ex, max_ex, worst_stage_avg = widen_stats(runs)
            n_rolls = sum(r.n_wing_rolls for r in runs)
            g_lines = gap_summary_lines(runs)

            row = CfgRow(
                wing=wing,
                roll_on=roll_on,
                n=len(nets),
                mean_day=mean * cpd,
                ci_lo=lo * cpd,
                ci_hi=hi * cpd,
                worst=min(nets),
                mdd=mdd,
                net_credit=statistics.mean(credits),
                entry_theo_avg=statistics.mean(entry_theos),
                worst_stage_theo_avg=worst_stage_avg,
                pct_widened=pct_w,
                avg_extra=avg_ex,
                max_extra=max_ex,
                n_wing_rolls=n_rolls,
                gap_lines=g_lines,
            )
            rows.append(row)

            emit(
                lines,
                f"----- wing={int(wing)}  WING_ROLL_{label}  n={row.n}  "
                f"wing_roll_events={n_rolls} -----",
            )
            emit(
                lines,
                f"  mean/day={row.mean_day:.4f}  ci_lo={row.ci_lo:.4f}  "
                f"ci_hi={row.ci_hi:.4f}",
            )
            emit(
                lines,
                f"  worst={row.worst:.4f}  mdd={row.mdd:.4f}  "
                f"net_credit/cycle={row.net_credit:.4f}",
            )
            emit(
                lines,
                f"  post-adj theo>entry: {row.pct_widened:.1f}%  "
                f"avg_extra=+{row.avg_extra:.4f}  max_extra=+{row.max_extra:.4f}",
            )
            for gl in g_lines:
                emit(lines, gl)
            emit(lines, "")

    emit(lines, "===== FINAL TABLE =====")
    emit(
        lines,
        f"{'wing':>6} {'roll':>4} {'mean/day':>10} {'ci_lo':>9} {'worst':>9} "
        f"{'theo_entry':>11} {'theo_wstage':>11} {'%widen':>8}",
    )
    emit(lines, "-" * 85)
    for r in rows:
        emit(
            lines,
            f"{int(r.wing):6d} {'ON' if r.roll_on else 'OFF':>4} "
            f"{r.mean_day:10.4f} {r.ci_lo:9.4f} {r.worst:9.4f} "
            f"{r.entry_theo_avg:11.4f} {r.worst_stage_theo_avg:11.4f} "
            f"{r.pct_widened:7.1f}%",
        )
    emit(lines, "")
    emit(
        lines,
        "Note: B_only rolls the untested short INWARD → rarely crosses the wing,",
    )
    emit(
        lines,
        "so WING_ROLL_ON events may be near zero; gap widening is mostly from",
    )
    emit(
        lines,
        "keeping old wings while short moves in (SELL_PARTIAL path).",
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
