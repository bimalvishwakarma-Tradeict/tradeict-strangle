#!/usr/bin/env python3
"""
S001 final wing distance × roll on new baseline.

Baseline (all configs): dte2 B_only trig70 maker B25 qty=8 11:00 IST
  hedge OFF, dec%=40, profit target k=1.0

PART A: wing ∈ {1500,2000,2500,3000} × roll OFF/ON (FIX_1 wing-follows) = 8
PART B: sizing numbers for winner config

Output: backtest/results/s001_wing_final.txt
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
import s001_income_engine as eng  # noqa: E402
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)

logger = logging.getLogger("s001_wing_final")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_wing_final.txt"

BASKET_QTY = 8
TRIGGER = 70.0
DEC_PCT = 40.0
PROFIT_K = 1.0
ENTRY_HHMM = "11:00"
WING_DISTS = (1500.0, 2000.0, 2500.0, 3000.0)
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE


@dataclass
class StageSnap:
    label: str
    call_gap: float | None
    put_gap: float | None
    theo_max_loss: float
    net_credit: float
    short_qty: int


@dataclass
class StageRun:
    entry_date: date
    net: float
    fees: float
    n_adjustments: int
    stages: list[StageSnap]
    entry_theo: float
    entry_net_credit: float
    short_prem_usd: float
    wing_cost_usd: float
    roll_cost: float
    entry_ts: int = 0


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
    wing_fallback: float,
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
    return float(wing_fallback) * qb - nc, call_gap, put_gap


def make_stage(
    label: str,
    sc_k: float,
    sp_k: float,
    wc_k: float | None,
    wp_k: float | None,
    qty: int,
    sc_px: float,
    sp_px: float,
    wc_px: float | None,
    wp_px: float | None,
    wing_pts: float,
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
        qty=qty,
        wing_fallback=wing_pts,
    )
    return StageSnap(
        label=label,
        call_gap=cg,
        put_gap=pg,
        theo_max_loss=theo,
        net_credit=net_credit_usd(sc_px, sp_px, wc_px, wp_px, qty),
        short_qty=qty,
    )


def filter_base_shorts(obs: list[eng.CycleObs]) -> list[eng.CycleObs]:
    """B25 11:00 dte2 maker — one row per entry (wing rebuilt per config)."""
    best: dict[tuple, eng.CycleObs] = {}
    for o in obs:
        if o.short_dte != 2:
            continue
        if o.fill_package != "maker":
            continue
        if o.strike_mode != "B25":
            continue
        if o.entry_hhmm != ENTRY_HHMM:
            continue
        key = (
            o.entry_date,
            o.entry_hhmm,
            o.basket_expiry,
            float(o.short_call_k),
            float(o.short_put_k),
        )
        # Prefer cache row that already had wings=2000 when available
        prev = best.get(key)
        if prev is None:
            best[key] = o
        elif o.wing_points == 2000.0 and prev.wing_points != 2000.0:
            best[key] = o
    return list(best.values())


def rebuild_with_wings(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    surface: Any,
    wing_pts: float,
) -> eng.CycleObs | None:
    exp = o.basket_expiry
    sc_k = float(o.short_call_k)
    sp_k = float(o.short_put_k)
    wk = eng.pick_wing_strikes(idx, exp, sc_k, sp_k, wing_pts)
    if wk is None:
        return None
    wc_k, wp_k = wk
    long_role = sweep.long_role()
    wc = eng.wing_fill_or_surface(
        idx, surface, exp, wc_k, "C", o.entry_utc, long_role
    )
    wp = eng.wing_fill_or_surface(
        idx, surface, exp, wp_k, "P", o.entry_utc, long_role
    )
    if wc is None or wp is None:
        return None
    # Entry wings: print preferred, surface allowed (same as income engine).
    # Roll path still requires real prints at re-establish time.
    return eng.CycleObs(
        entry_date=o.entry_date,
        entry_hhmm=o.entry_hhmm,
        entry_utc=o.entry_utc,
        basket_expiry=exp,
        short_dte=o.short_dte,
        fill_package=o.fill_package,
        strike_mode=o.strike_mode,
        wing_points=wing_pts,
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
        wing_used_surface=(wc.source == "surface" or wp.source == "surface"),
        basket_pnl=0.0,
        wings_pnl=0.0,
        entry_fees=0.0,
        settle_fees=0.0,
        net_no_settle=0.0,
        net_with_settle=0.0,
        spot_move_abs=float(o.spot_move_abs),
    )


def entry_cost_usd(o: eng.CycleObs, qty: int = BASKET_QTY) -> float:
    spot = float(o.spot_entry)
    sc = float(o.short_call.price)
    sp = float(o.short_put.price)
    fees = eng.option_fee(sc, spot, qty) + eng.option_fee(sp, spot, qty)
    wing = 0.0
    if o.wing_call is not None and o.wing_put is not None:
        wc = float(o.wing_call.price)
        wp = float(o.wing_put.price)
        fees += eng.option_fee(wc, spot, qty) + eng.option_fee(wp, spot, qty)
        wing = (wc + wp) * qty * CV
    return fees + wing


def simulate(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    wing_pts: float,
    roll_on: bool,
) -> StageRun:
    adj_b_trig = sweep.adj_b_pct_from_trigger(TRIGGER)
    exp = o.basket_expiry
    short_role_s = sweep.short_role()
    long_role_s = sweep.long_role()
    original_qty = BASKET_QTY
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

    short_prem_usd = (sc_entry + sp_entry) * qty_btc(qty)
    wing_cost_usd = (
        (float(wc_entry) + float(wp_entry)) * qty_btc(qty)
        if wc_entry is not None and wp_entry is not None
        else 0.0
    )
    entry_nc = net_credit_usd(sc_entry, sp_entry, wc_entry, wp_entry, qty)
    stages = [
        make_stage(
            "entry",
            sc_k,
            sp_k,
            wc_k,
            wp_k,
            qty,
            sc_entry,
            sp_entry,
            wc_entry,
            wp_entry,
            wing_pts,
        )
    ]
    entry_theo = stages[0].theo_max_loss
    profit_target = entry_cost_usd(o) * PROFIT_K

    realized = 0.0
    roll_cost = 0.0
    adj_count = 0
    closed_early = False
    t0 = int(o.entry_utc.timestamp())
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    if t_end <= t0:
        return StageRun(
            entry_date=o.entry_date,
            net=float("nan"),
            fees=fees,
            n_adjustments=0,
            stages=stages,
            entry_theo=entry_theo,
            entry_net_credit=entry_nc,
            short_prem_usd=short_prem_usd,
            wing_cost_usd=wing_cost_usd,
            roll_cost=0.0,
            entry_ts=t0,
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

        net_now = sweep.mtm_net(
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
        if net_now >= profit_target - 1e-12:
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False)
            exit_pnl += eng.cash_pnl(sp_entry, sp_now, qty, is_long=False)
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

        def _force_close() -> None:
            nonlocal realized, fees, closed_early
            stages.append(
                make_stage(
                    "force_exit",
                    sc_k,
                    sp_k,
                    wc_k,
                    wp_k,
                    qty,
                    sc_entry,
                    sp_entry,
                    wc_entry,
                    wp_entry,
                    wing_pts,
                )
            )
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False)
            exit_pnl += eng.cash_pnl(sp_entry, sp_now, qty, is_long=False)
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

        if adj_count >= sweep.MAX_ADJUSTMENTS_PER_BASKET:
            _force_close()
            break

        next_n = adj_count + 1
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=next_n,
            decrease_pct=DEC_PCT,
        )
        if close_basket or new_qty is None:
            _force_close()
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
        new_k = float(res.strike)
        fill = eng.nearest_print_prefer(
            idx,
            eng.format_symbol("C" if leg == "call" else "P", new_k, exp),
            when,
            eng.PRINT_WINDOW_SEC,
            short_role_s,
        )
        if fill is None or fill.price <= 0:
            t += sweep.MONITOR_STEP_SEC
            continue
        new_fill_px = float(fill.price)

        # FIX_1 roll: pre-resolve new wings (real prints only)
        fix1: tuple[float, float, float, float] | None = None
        if roll_on:
            if wc_now is None or wp_now is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            sc_after = new_k if leg == "call" else sc_k
            sp_after = new_k if leg == "put" else sp_k
            wk = eng.pick_wing_strikes(idx, exp, sc_after, sp_after, wing_pts)
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
            fix1 = (nwc_k, nwp_k, float(wcf.price), float(wpf.price))

        if leg == "call":
            realized += eng.cash_pnl(sc_entry, sc_now, qty, is_long=False)
            fees += eng.option_fee(sc_now, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            realized += eng.cash_pnl(sp_entry, sp_now, qty, is_long=False)
            fees += eng.option_fee(sp_now, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        if wc_k is not None and wp_k is not None and wc_entry is not None and wp_entry is not None:
            if roll_on and fix1 is not None and wc_now is not None and wp_now is not None:
                nwc_k, nwp_k, nwc_px, nwp_px = fix1
                realized += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                realized += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                fee_wx = eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
                fees += fee_wx
                fee_we = eng.option_fee(nwc_px, spot, int(new_qty)) + eng.option_fee(
                    nwp_px, spot, int(new_qty)
                )
                fees += fee_we
                buy_new = (nwc_px + nwp_px) * qty_btc(int(new_qty))
                keep_val = (wc_now + wp_now) * qty_btc(int(new_qty))
                closed = qty - int(new_qty)
                fee_partial = (
                    eng.option_fee(wc_now, spot, closed)
                    + eng.option_fee(wp_now, spot, closed)
                    if closed > 0
                    else 0.0
                )
                roll_cost += (buy_new - keep_val) + fee_we + (fee_wx - fee_partial)
                wc_k, wp_k = nwc_k, nwp_k
                wc_entry, wp_entry = nwc_px, nwp_px
                wing_qty = int(new_qty)
            else:
                closed = qty - int(new_qty)
                if closed > 0 and wc_now is not None and wp_now is not None:
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
                f"adj_{adj_count}",
                sc_k,
                sp_k,
                wc_k,
                wp_k,
                qty,
                sc_entry,
                sp_entry,
                wc_entry,
                wp_entry,
                wing_pts,
            )
        )
        t += sweep.MONITOR_STEP_SEC

    if not closed_early:
        spot_s = float(o.spot_settle)
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
        net = realized - fees
    else:
        net = realized - fees

    return StageRun(
        entry_date=o.entry_date,
        net=net,
        fees=fees,
        n_adjustments=adj_count,
        stages=stages,
        entry_theo=entry_theo,
        entry_net_credit=entry_nc,
        short_prem_usd=short_prem_usd,
        wing_cost_usd=wing_cost_usd,
        roll_cost=roll_cost,
        entry_ts=t0,
    )


def daily_series(
    runs: list[StageRun], day_span: int, d0: date, d1: date
) -> list[float]:
    by: dict[date, float] = {}
    for r in runs:
        if not math.isfinite(r.net):
            continue
        by[r.entry_date] = by.get(r.entry_date, 0.0) + r.net
    from datetime import timedelta

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


def stage_of(r: StageRun, label: str) -> StageSnap | None:
    for s in r.stages:
        if s.label == label:
            return s
    return None


def summarize_config(
    runs: list[StageRun], day_span: int, d0: date, d1: date, *, seed: int
) -> dict[str, Any]:
    ok = [r for r in runs if math.isfinite(r.net)]
    daily = daily_series(ok, day_span, d0, d1)
    mean, lo, hi = eng.bootstrap_mean_ci(daily, BOOTSTRAP_N, seed)
    mdd = eng.max_drawdown(daily) if daily else float("nan")
    ncs = [r.entry_net_credit for r in ok]
    wing_pcts = [
        100.0 * r.wing_cost_usd / r.short_prem_usd
        for r in ok
        if r.short_prem_usd > 1e-12
    ]
    rolls = [r.roll_cost for r in ok]
    widen_n = 0
    worst_theo_all: list[float] = []
    for r in ok:
        entry = stage_of(r, "entry")
        if entry is None:
            continue
        post = [
            s.theo_max_loss
            for s in r.stages
            if s.label in ("adj_1", "adj_2", "force_exit")
        ]
        worst_theo_all.append(max([entry.theo_max_loss] + post))
        if post and max(post) > entry.theo_max_loss + 1e-12:
            widen_n += 1
    return {
        "n": len(ok),
        "mean_day": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "worst": min((r.net for r in ok), default=float("nan")),
        "mdd": mdd,
        "avg_nc": statistics.mean(ncs) if ncs else float("nan"),
        "wing_pct": statistics.mean(wing_pcts) if wing_pcts else float("nan"),
        "avg_roll": statistics.mean(rolls) if rolls else 0.0,
        "widen_pct": 100.0 * widen_n / float(max(1, len(ok))),
        "worst_theo": max(worst_theo_all) if worst_theo_all else float("nan"),
        "avg_entry_theo": (
            statistics.mean([r.entry_theo for r in ok]) if ok else float("nan")
        ),
        "runs": ok,
    }


def gap_report(lines: list[str], runs: list[StageRun]) -> None:
    labels = ("entry", "adj_1", "adj_2", "force_exit")
    for lab in labels:
        cg: list[float] = []
        pg: list[float] = []
        th: list[float] = []
        n = 0
        for r in runs:
            s = stage_of(r, lab)
            if s is None:
                continue
            n += 1
            if s.call_gap is not None:
                cg.append(s.call_gap)
            if s.put_gap is not None:
                pg.append(s.put_gap)
            th.append(s.theo_max_loss)
        if n == 0:
            emit(lines, f"  {lab}: (no cycles)")
            continue
        emit(
            lines,
            f"  {lab} (n={n}): call_gap avg={statistics.mean(cg):.1f} "
            f"worst(min)={min(cg):.1f} | put_gap avg={statistics.mean(pg):.1f} "
            f"worst(min)={min(pg):.1f} | theo_max avg={statistics.mean(th):.4f} "
            f"worst(max)={max(th):.4f}",
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 FINAL WING DISTANCE × ROLL (new baseline)")
    emit(lines, "=" * 100)
    emit(
        lines,
        "BASELINE FIXED: dte2 B_only trig70 maker B25 qty=8 11:00 IST "
        f"HEDGE=OFF dec%={DEC_PCT:g} profit_k={PROFIT_K:g}",
    )
    emit(
        lines,
        "Entry wings: print preferred, surface OK (income-engine parity). "
        "ROLL re-establish: real prints only.",
    )
    emit(lines, "Qty path dec%40 max_adj2: [8,4,1]")
    emit(lines, "")

    logger.info("Loading cache + index...")
    all_obs, day_span = sweep.load_cycles()
    base_shorts = filter_base_shorts(all_obs)
    logger.info("base short cycles=%s day_span=%s", len(base_shorts), day_span)
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    surface = eng.load_surface_optional()
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()

    # Prebuild cycles per wing distance
    by_wing: dict[float, list[eng.CycleObs]] = {}
    for wp in WING_DISTS:
        cyc: list[eng.CycleObs] = []
        for o in base_shorts:
            co = rebuild_with_wings(o, idx, surface, wp)
            if co is not None:
                cyc.append(co)
        by_wing[wp] = cyc
        logger.info("wing=%s cycles with print wings=%s", wp, len(cyc))

    emit(lines, "===== PART A: WING DISTANCE × ROLL =====")
    emit(lines, "")
    emit(
        lines,
        f"{'wing':>6} {'roll':>4} {'n':>5} {'mean/day':>10} {'ci_lo':>9} "
        f"{'ci_hi':>9} {'worst':>9} {'mdd':>9} {'netCred':>8} {'wing%':>7} "
        f"{'roll/c':>8} {'widen%':>7} {'wTheo':>8}",
    )
    emit(lines, "-" * 120)

    baseline_ci = float("nan")
    results: list[tuple[float, bool, dict[str, Any]]] = []
    cfg_i = 0
    for wp in WING_DISTS:
        for roll in (False, True):
            cfg_i += 1
            label = f"w{int(wp)}_{'ON' if roll else 'OFF'}"
            logger.info("Config %s", label)
            runs: list[StageRun] = []
            cycles = by_wing[wp]
            for j, o in enumerate(cycles):
                if (j + 1) % 100 == 0:
                    logger.info("  %s %s/%s", label, j + 1, len(cycles))
                runs.append(
                    simulate(
                        o, idx, times, closes, wing_pts=wp, roll_on=roll
                    )
                )
            sm = summarize_config(
                runs, day_span, d0, d1, seed=BOOTSTRAP_SEED + cfg_i
            )
            if abs(wp - 2000.0) < 1e-9 and not roll:
                baseline_ci = sm["ci_lo"]
            results.append((wp, roll, sm))
            emit(
                lines,
                f"{int(wp):6d} {'ON' if roll else 'OFF':>4} {sm['n']:5d} "
                f"{sm['mean_day']:10.4f} {sm['ci_lo']:9.4f} {sm['ci_hi']:9.4f} "
                f"{sm['worst']:9.4f} {sm['mdd']:9.4f} {sm['avg_nc']:8.4f} "
                f"{sm['wing_pct']:7.1f} {sm['avg_roll']:8.4f} "
                f"{sm['widen_pct']:7.1f} {sm['worst_theo']:8.4f}",
            )

    better = sum(1 for _wp, _r, sm in results if sm["ci_lo"] > baseline_ci)
    emit(lines, "")
    emit(
        lines,
        f"configs with ci_lo > baseline ci_lo: {better} / 8 (chance exp 0.40)",
    )
    emit(lines, "")

    # Gap detail blocks
    for wp, roll, sm in results:
        emit(
            lines,
            f"--- gaps wing={int(wp)} roll={'ON' if roll else 'OFF'} ---",
        )
        gap_report(lines, sm["runs"])
        emit(
            lines,
            f"  %cycles post-adj theo_max > entry: {sm['widen_pct']:.1f}%  "
            f"| WORST theo_max sample: {sm['worst_theo']:.4f}  "
            f"| avg entry theo: {sm['avg_entry_theo']:.4f}",
        )
        emit(lines, "")

    # =====================================================================
    # PART B — winner sizing
    # =====================================================================
    emit(lines, "===== PART B: SIZING NUMBER (C6) =====")
    winner = max(results, key=lambda x: x[2]["mean_day"])
    w_wp, w_roll, w_sm = winner
    emit(
        lines,
        f"Winner by mean/day: wing={int(w_wp)} roll={'ON' if w_roll else 'OFF'} "
        f"(mean/day={w_sm['mean_day']:.4f}, ci_lo={w_sm['ci_lo']:.4f})",
    )
    runs_w: list[StageRun] = w_sm["runs"]
    entry_theos = [r.entry_theo for r in runs_w]
    stage_theos: list[float] = []
    for r in runs_w:
        for s in r.stages:
            stage_theos.append(s.theo_max_loss)
    nets = [r.net for r in runs_w]
    avg_entry_theo = statistics.mean(entry_theos) if entry_theos else float("nan")
    worst_entry_theo = max(entry_theos) if entry_theos else float("nan")
    worst_stage_theo = max(stage_theos) if stage_theos else float("nan")
    actual_worst = min(nets) if nets else float("nan")
    p1 = eng.pctile(nets, 1.0)
    p5 = eng.pctile(nets, 5.0)

    # Same-day combined
    by_day: dict[date, list[float]] = {}
    for r in runs_w:
        by_day.setdefault(r.entry_date, []).append(r.net)
    multi = {d: v for d, v in by_day.items() if len(v) >= 2}
    if multi:
        worst_2_same = min(sum(sorted(v)[:2]) for v in multi.values() if len(v) >= 2)
        worst_3_same = min(
            sum(sorted(v)[:3]) for v in multi.values() if len(v) >= 3
        ) if any(len(v) >= 3 for v in multi.values()) else float("nan")
    else:
        worst_2_same = float("nan")
        worst_3_same = float("nan")

    # Hypothetical stress: sum of 2 / 3 worst cycles
    sorted_nets = sorted(nets)
    hyp2 = sum(sorted_nets[:2]) if len(sorted_nets) >= 2 else float("nan")
    hyp3 = sum(sorted_nets[:3]) if len(sorted_nets) >= 3 else float("nan")

    emit(lines, "")
    emit(lines, f"  n cycles: {len(runs_w)}")
    emit(lines, f"  theoretical max loss at entry (avg): {avg_entry_theo:.4f} USD")
    emit(lines, f"  theoretical max loss at entry (worst cycle): {worst_entry_theo:.4f} USD")
    emit(
        lines,
        f"  WORST theoretical max loss any stage (sample): {worst_stage_theo:.4f} USD",
    )
    emit(lines, f"  actual worst cycle P&L: {actual_worst:.4f} USD")
    emit(lines, f"  p1 cycle P&L: {p1:.4f}  |  p5 cycle P&L: {p5:.4f}")
    emit(
        lines,
        f"  same-day multi baskets: days_with_2+={len(multi)}  "
        f"worst_sum2={worst_2_same:.4f}  worst_sum3={worst_3_same:.4f}",
    )
    emit(
        lines,
        f"  hypothetical worst 2-cycle sum: {hyp2:.4f}  |  "
        f"worst 3-cycle sum: {hyp3:.4f}",
    )
    emit(lines, "")

    # Sizing recommendation — prefer worst-stage theo (conservative)
    sizing = worst_stage_theo
    cap_kind = "worst-stage theo_max (not entry-only)"
    if not math.isfinite(sizing) or sizing <= 0:
        sizing = worst_entry_theo
        cap_kind = "entry theo_max (worst cycle)"
    emit(
        lines,
        f'sizing ke liye max loss per basket = {sizing:.4f} USD maano '
        f"— ye {cap_kind} hai.",
    )
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
