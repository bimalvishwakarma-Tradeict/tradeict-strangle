#!/usr/bin/env python3
"""
S001 exit/entry rules — one-at-a-time vs baseline (28 configs).

TEST 1: profit target × k + same-day re-entry (max 3/day)
TEST 2: time stop hours
TEST 3: entry time IST
TEST 4: post-adj target × m  ×  decrease_pct

Output: backtest/results/s001_exit_rules.txt
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
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

logger = logging.getLogger("s001_exit_rules")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_exit_rules.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
BASKET_QTY = 8
WING_POINTS = 2000.0
MAX_ENTRIES_PER_DAY = 3
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE


@dataclass
class SimOut:
    net: float
    fees: float
    hold_hours: float
    exit_reason: str  # settle | force_adj | profit_target | time_stop | post_adj_target
    n_adjustments: int
    entry_premium: float  # short call+put per lot
    strike_dist: float  # avg |short-spot|
    entry_ts: int
    exit_ts: int
    entry_date: date


@dataclass
class DayAgg:
    day: date
    nets: list[float] = field(default_factory=list)
    fees: list[float] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    n_entries: int = 0


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def filter_base(
    obs: list[eng.CycleObs], *, hhmm: str = "11:00"
) -> list[eng.CycleObs]:
    out: list[eng.CycleObs] = []
    for o in obs:
        if o.short_dte != 2:
            continue
        if o.fill_package != "maker":
            continue
        if o.strike_mode != "B25":
            continue
        if o.wing_points != WING_POINTS:
            continue
        if o.entry_hhmm != hhmm:
            continue
        if o.wing_call is None or o.wing_put is None:
            continue
        out.append(o)
    return out


def build_cycle_at(
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    surface: object | None,
    *,
    day: date,
    hh: int,
    mm: int,
    expiry: date | None = None,
) -> eng.CycleObs | None:
    """Construct a maker/B25/wing2000 CycleObs at given IST time (prints/surface)."""
    entry_utc = eng.ist_to_utc(day, hh, mm)
    ts = int(entry_utc.timestamp())
    if ts < times[0] or ts > times[-1]:
        return None
    spot_e = ot.spot_at(times, closes, ts)
    if spot_e is None or spot_e <= 0:
        return None
    exp = expiry if expiry is not None else day + timedelta(days=2)
    if exp not in idx.expiries:
        return None
    spot_s = eng.settle_spot_1200_utc(times, closes, exp)
    if spot_s is None or spot_s <= 0:
        return None
    settle_ts = int(
        datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp()
    )
    if ts >= settle_ts:
        return None
    short_role, long_role = eng.roles_for_package("maker")
    atm = eng.pick_atm_straddle(idx, exp, float(spot_e), entry_utc, long_role)
    if atm is None:
        atm = eng.pick_atm_straddle(idx, exp, float(spot_e), entry_utc, short_role)
    if atm is None:
        return None
    _ak, ac, ap = atm
    atm_prem = ac.price + ap.price
    if atm_prem <= 0:
        return None
    target = 0.25 * atm_prem
    if target < 5.0:
        return None
    strangle = eng.pick_strangle_by_premium(
        idx, exp, float(spot_e), target, entry_utc, short_role
    )
    if strangle is None:
        return None
    sc_k, sp_k, sc, sp = strangle
    wk = eng.pick_wing_strikes(idx, exp, sc_k, sp_k, WING_POINTS)
    if wk is None:
        return None
    wc_k, wp_k = wk
    wc = eng.wing_fill_or_surface(
        idx, surface, exp, wc_k, "C", entry_utc, long_role
    )
    wp = eng.wing_fill_or_surface(
        idx, surface, exp, wp_k, "P", entry_utc, long_role
    )
    if wc is None or wp is None:
        return None
    return eng.CycleObs(
        entry_date=day,
        entry_hhmm=f"{hh:02d}:{mm:02d}",
        entry_utc=entry_utc,
        basket_expiry=exp,
        short_dte=2,
        fill_package="maker",
        strike_mode="B25",
        wing_points=WING_POINTS,
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
        wing_used_surface=(wc.source == "surface" or wp.source == "surface"),
        basket_pnl=0.0,
        wings_pnl=0.0,
        entry_fees=0.0,
        settle_fees=0.0,
        net_no_settle=0.0,
        net_with_settle=0.0,
        spot_move_abs=abs(float(spot_s) - float(spot_e)),
    )


def simulate_rules(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    profit_target: float | None = None,
    time_stop_hours: float | None = None,
    decrease_pct: float = 20.0,
    post_adj_mult: float | None = None,
) -> SimOut:
    """
    B_only WINNER sim with optional early exits.
    profit_target: absolute USD on mtm_net (gross marks − fees so far).
    post_adj_mult: after each adj, target = (adj_loss + fees) * m
    """
    cfg = WINNER_CFG
    assert cfg.trigger_pct is not None
    adj_b_trig = sweep.adj_b_pct_from_trigger(float(cfg.trigger_pct))
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

    spot_e = float(o.spot_entry)
    fees = eng.option_fee(sc_entry, spot_e, qty) + eng.option_fee(sp_entry, spot_e, qty)
    if wc_entry is not None and wp_entry is not None:
        fees += eng.option_fee(wc_entry, spot_e, qty) + eng.option_fee(
            wp_entry, spot_e, qty
        )

    entry_prem = sc_entry + sp_entry
    strike_dist = (
        abs(sc_k - spot_e) + abs(sp_k - spot_e)
    ) / 2.0

    realized = 0.0
    adj_count = 0
    closed_early = False
    exit_reason = "settle"
    active_target = profit_target  # may update after adj

    t0 = int(o.entry_utc.timestamp())
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    t_stop = (
        t0 + int(float(time_stop_hours) * 3600)
        if time_stop_hours is not None
        else None
    )
    exit_ts = t_end

    if t_end <= t0:
        return SimOut(
            net=float("nan"),
            fees=fees,
            hold_hours=0.0,
            exit_reason="invalid",
            n_adjustments=0,
            entry_premium=entry_prem,
            strike_dist=strike_dist,
            entry_ts=t0,
            exit_ts=t0,
            entry_date=o.entry_date,
        )

    t = t0 + sweep.MONITOR_STEP_SEC
    while t < t_end and not closed_early:
        when = datetime.fromtimestamp(t, tz=UTC)
        spot = ot.spot_at(times, closes, t)
        if spot is None or spot <= 0:
            t += sweep.MONITOR_STEP_SEC
            continue

        # Time stop
        if t_stop is not None and t >= t_stop:
            sc_now = sweep.premium_at(idx, exp, "call", sc_k, when, for_short_exit=True)
            sp_now = sweep.premium_at(idx, exp, "put", sp_k, when, for_short_exit=True)
            if sc_now is not None and sp_now is not None:
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
                exit_reason = "time_stop"
                exit_ts = t
                break

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

        # Profit / post-adj target
        if active_target is not None and net_now >= active_target - 1e-12:
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
            exit_reason = (
                "post_adj_target"
                if post_adj_mult is not None and adj_count > 0
                else "profit_target"
            )
            exit_ts = t
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
        if adj_count >= sweep.MAX_ADJUSTMENTS_PER_BASKET:
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
            exit_reason = "force_adj"
            exit_ts = t
            break

        next_n = adj_count + 1
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=next_n,
            decrease_pct=decrease_pct,
        )
        if close_basket or new_qty is None:
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
            exit_reason = "force_adj"
            exit_ts = t
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
        new_fill_px = float(fill.price) if fill is not None else float(res.premium)

        # Capture loss on this adj exit (positive when short loses)
        if leg == "call":
            exit_px = sc_now
            adj_leg_pnl = eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            realized += adj_leg_pnl
            fees += eng.option_fee(exit_px, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            exit_px = sp_now
            adj_leg_pnl = eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            realized += adj_leg_pnl
            fees += eng.option_fee(exit_px, spot, qty)
            fees += eng.option_fee(new_fill_px, spot, int(new_qty))
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        adj_loss = max(0.0, -adj_leg_pnl)

        # Wing partial
        if (
            wc_k is not None
            and wp_k is not None
            and wc_entry is not None
            and wp_entry is not None
            and wc_now is not None
            and wp_now is not None
        ):
            closed = qty - int(new_qty)
            if closed > 0:
                realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
                realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
                fees += eng.option_fee(wc_now, spot, closed) + eng.option_fee(
                    wp_now, spot, closed
                )

        qty = int(new_qty)
        adj_count += 1

        if post_adj_mult is not None:
            # naya target = (adj realized loss + saari fees) × m
            active_target = (adj_loss + fees) * float(post_adj_mult)

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
        exit_ts = t_end
        exit_reason = "settle" if adj_count < sweep.MAX_ADJUSTMENTS_PER_BASKET else exit_reason
    else:
        net = realized - fees

    hold_h = max(0.0, (exit_ts - t0) / 3600.0)
    return SimOut(
        net=net,
        fees=fees,
        hold_hours=hold_h,
        exit_reason=exit_reason,
        n_adjustments=adj_count,
        entry_premium=entry_prem,
        strike_dist=strike_dist,
        entry_ts=t0,
        exit_ts=exit_ts,
        entry_date=o.entry_date,
    )


def entry_cost_usd(o: eng.CycleObs, qty: int = BASKET_QTY) -> float:
    """entry fees + wing debit (USD)."""
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


def daily_series(
    outs: list[SimOut], day_span: int, d0: date, d1: date
) -> list[float]:
    by: dict[date, float] = {}
    for s in outs:
        if not math.isfinite(s.net):
            continue
        by[s.entry_date] = by.get(s.entry_date, 0.0) + s.net
    # Fill calendar from d0..d1 clipped to day_span length preference
    days: list[float] = []
    d = d0
    n = 0
    while d <= d1 and n < max(day_span, (d1 - d0).days + 1):
        days.append(by.get(d, 0.0))
        d += timedelta(days=1)
        n += 1
    # If shorter than day_span, pad with zeros at end
    while len(days) < day_span:
        days.append(0.0)
    return days[:day_span]


def summarize_daily(
    outs: list[SimOut],
    day_span: int,
    d0: date,
    d1: date,
    *,
    seed: int,
) -> dict[str, float]:
    ok = [s for s in outs if math.isfinite(s.net)]
    daily = daily_series(ok, day_span, d0, d1)
    mean, lo, hi = eng.bootstrap_mean_ci(daily, BOOTSTRAP_N, seed)
    chron = daily  # already calendar order
    mdd = eng.max_drawdown(chron) if chron else float("nan")
    holds = [s.hold_hours for s in ok]
    fees_tot = sum(s.fees for s in ok)
    return {
        "n_cycles": float(len(ok)),
        "mean_day": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "worst": min((s.net for s in ok), default=float("nan")),
        "mdd": mdd,
        "cpd": len(ok) / float(max(1, day_span)),
        "avg_hold": statistics.mean(holds) if holds else float("nan"),
        "fees_per_day": fees_tot / float(max(1, day_span)),
        "median_cycle": statistics.median([s.net for s in ok]) if ok else float("nan"),
    }


def chance_line(lines: list[str], better: int, total: int) -> None:
    emit(
        lines,
        f"configs with ci_lo > baseline ci_lo: {better} / {total} "
        f"(chance expectation: {total * 0.05:.2f})",
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 EXIT / ENTRY RULES — one-at-a-time vs baseline (28 configs)")
    emit(lines, "=" * 100)
    emit(
        lines,
        "BASELINE: dte2 B_only trig70 maker B25 wings2000 qty=8 entry=11:00 HEDGE=OFF",
    )
    emit(lines, "")

    logger.info("Loading cache + index...")
    all_obs, day_span = sweep.load_cycles()
    base11 = filter_base(all_obs, hhmm="11:00")
    logger.info("baseline 11:00 cycles=%s day_span=%s", len(base11), day_span)
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    surface = eng.load_surface_optional()
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()

    # ----- BASELINE -----
    logger.info("Baseline sims...")
    base_outs: list[SimOut] = []
    for i, o in enumerate(base11):
        if (i + 1) % 50 == 0:
            logger.info("  baseline %s/%s", i + 1, len(base11))
        base_outs.append(simulate_rules(o, idx, times, closes))
    base_sum = summarize_daily(base_outs, day_span, d0, d1, seed=BOOTSTRAP_SEED)
    baseline_ci_lo = base_sum["ci_lo"]
    emit(lines, "===== BASELINE =====")
    emit(
        lines,
        f"  n={int(base_sum['n_cycles'])}  mean/day={base_sum['mean_day']:.4f}  "
        f"ci_lo={base_sum['ci_lo']:.4f}  ci_hi={base_sum['ci_hi']:.4f}  "
        f"worst={base_sum['worst']:.4f}  mdd={base_sum['mdd']:.4f}  "
        f"cpd={base_sum['cpd']:.4f}  avg_hold_h={base_sum['avg_hold']:.2f}  "
        f"fees/day={base_sum['fees_per_day']:.4f}",
    )
    emit(lines, "")

    # =====================================================================
    # TEST 1
    # =====================================================================
    emit(lines, "===== TEST 1: PROFIT TARGET + RE-ENTRY (max 3/day) =====")
    emit(
        lines,
        "target = (entry_fees + wing_debit) × k ; exit when mtm_net >= target",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'k':>8} {'n':>5} {'mean/day':>10} {'cpd':>7} {'hold_h':>7} "
        f"{'ci_lo':>9} {'worst':>9} {'mdd':>9} {'fees/day':>9}",
    )
    emit(lines, "-" * 90)

    k_vals: list[float | None] = [1.0, 1.5, 2.0, 3.0, None]
    t1_better = 0
    by_date_obs = {o.entry_date: o for o in base11}

    for ki, k in enumerate(k_vals):
        label = "NO_TARGET" if k is None else f"{k:.1f}"
        logger.info("TEST1 k=%s", label)
        outs: list[SimOut] = []
        if k is None:
            outs = list(base_outs)
        else:
            for day, o0 in sorted(by_date_obs.items()):
                entries = 0
                cur: eng.CycleObs | None = o0
                exp = o0.basket_expiry
                cursor_ts = int(o0.entry_utc.timestamp())
                while cur is not None and entries < MAX_ENTRIES_PER_DAY:
                    cost = entry_cost_usd(cur)
                    target = cost * float(k)
                    s = simulate_rules(
                        cur, idx, times, closes, profit_target=target
                    )
                    outs.append(s)
                    entries += 1
                    if s.exit_reason != "profit_target":
                        break
                    # Re-enter same day after exit
                    exit_dt = datetime.fromtimestamp(s.exit_ts, tz=UTC).astimezone(IST)
                    # next slot at least one monitor step later
                    re_ts = s.exit_ts + sweep.MONITOR_STEP_SEC
                    re_ist = datetime.fromtimestamp(re_ts, tz=UTC).astimezone(IST)
                    if re_ist.date() != day:
                        break
                    cur = build_cycle_at(
                        idx,
                        times,
                        closes,
                        surface,
                        day=day,
                        hh=re_ist.hour,
                        mm=(re_ist.minute // 5) * 5,  # snap to 5m
                        expiry=exp,
                    )
                    if cur is None:
                        # try exact minute
                        cur = build_cycle_at(
                            idx,
                            times,
                            closes,
                            surface,
                            day=day,
                            hh=re_ist.hour,
                            mm=re_ist.minute,
                            expiry=exp,
                        )
                    _ = cursor_ts
        sm = summarize_daily(
            outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 100 + ki
        )
        if sm["ci_lo"] > baseline_ci_lo:
            t1_better += 1
        emit(
            lines,
            f"{label:>8} {int(sm['n_cycles']):5d} {sm['mean_day']:10.4f} "
            f"{sm['cpd']:7.3f} {sm['avg_hold']:7.2f} {sm['ci_lo']:9.4f} "
            f"{sm['worst']:9.4f} {sm['mdd']:9.4f} {sm['fees_per_day']:9.4f}",
        )
    chance_line(lines, t1_better, len(k_vals))
    emit(lines, "")

    # =====================================================================
    # TEST 2
    # =====================================================================
    emit(lines, "===== TEST 2: TIME STOP =====")
    emit(lines, "")
    emit(
        lines,
        f"{'N_h':>8} {'n':>5} {'mean/day':>10} {'cpd':>7} {'hold_h':>7} "
        f"{'ci_lo':>9} {'worst':>9} {'mdd':>9} {'fees/day':>9} "
        f"{'%time':>7} {'%trig':>7}",
    )
    emit(lines, "-" * 110)

    n_vals: list[float | None] = [4.0, 6.0, 8.0, 12.0, None]
    t2_better = 0
    for ni, nh in enumerate(n_vals):
        label = "NO_STOP" if nh is None else f"{nh:.0f}"
        logger.info("TEST2 N=%s", label)
        outs = []
        for i, o in enumerate(base11):
            if (i + 1) % 100 == 0:
                logger.info("  time_stop %s %s/%s", label, i + 1, len(base11))
            outs.append(
                simulate_rules(
                    o, idx, times, closes, time_stop_hours=nh
                )
            )
        sm = summarize_daily(
            outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 200 + ni
        )
        n_ok = [s for s in outs if math.isfinite(s.net)]
        pct_time = (
            100.0 * sum(1 for s in n_ok if s.exit_reason == "time_stop") / max(1, len(n_ok))
        )
        # trigger-ish: force_adj (hit max adj) or settle after adjs — user asked trigger
        pct_trig = (
            100.0
            * sum(1 for s in n_ok if s.exit_reason in ("force_adj", "settle") and s.n_adjustments > 0)
            / max(1, len(n_ok))
        )
        # Also count force_adj as adjustment-driven exit
        pct_force = (
            100.0 * sum(1 for s in n_ok if s.exit_reason == "force_adj") / max(1, len(n_ok))
        )
        if sm["ci_lo"] > baseline_ci_lo:
            t2_better += 1
        emit(
            lines,
            f"{label:>8} {int(sm['n_cycles']):5d} {sm['mean_day']:10.4f} "
            f"{sm['cpd']:7.3f} {sm['avg_hold']:7.2f} {sm['ci_lo']:9.4f} "
            f"{sm['worst']:9.4f} {sm['mdd']:9.4f} {sm['fees_per_day']:9.4f} "
            f"{pct_time:6.1f}% {pct_force:6.1f}%",
        )
        _ = pct_trig
    emit(lines, "  %time = closed by time_stop; %trig = closed by force_adj (max adj)")
    chance_line(lines, t2_better, len(n_vals))
    emit(lines, "")

    # =====================================================================
    # TEST 3
    # =====================================================================
    emit(lines, "===== TEST 3: ENTRY TIME =====")
    emit(lines, "")
    emit(
        lines,
        f"{'IST':>6} {'n':>5} {'mean/day':>10} {'cpd':>7} {'hold_h':>7} "
        f"{'ci_lo':>9} {'worst':>9} {'mdd':>9} {'fees/day':>9} "
        f"{'prem':>8} {'dist':>8} {'adj/c':>6}",
    )
    emit(lines, "-" * 120)

    entry_slots = [(9, 0), (11, 0), (13, 0), (15, 0), (17, 0), (21, 0)]
    t3_better = 0
    dates_11 = sorted({o.entry_date for o in base11})

    for ei, (hh, mm) in enumerate(entry_slots):
        hhmm = f"{hh:02d}:{mm:02d}"
        logger.info("TEST3 entry=%s", hhmm)
        cached = filter_base(all_obs, hhmm=hhmm)
        if cached:
            cycles = cached
            note = "cache"
        else:
            # synthesize for same calendar days as 11:00 set
            cycles = []
            for day in dates_11:
                cyc = build_cycle_at(
                    idx, times, closes, surface, day=day, hh=hh, mm=mm
                )
                if cyc is not None:
                    cycles.append(cyc)
            note = f"synth n={len(cycles)}"
        logger.info("  entry=%s cycles=%s (%s)", hhmm, len(cycles), note)
        outs = []
        for i, o in enumerate(cycles):
            if (i + 1) % 100 == 0:
                logger.info("  entry %s %s/%s", hhmm, i + 1, len(cycles))
            outs.append(simulate_rules(o, idx, times, closes))
        sm = summarize_daily(
            outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 300 + ei
        )
        ok = [s for s in outs if math.isfinite(s.net)]
        avg_prem = statistics.mean([s.entry_premium for s in ok]) if ok else float("nan")
        avg_dist = statistics.mean([s.strike_dist for s in ok]) if ok else float("nan")
        avg_adj = statistics.mean([float(s.n_adjustments) for s in ok]) if ok else float("nan")
        if sm["ci_lo"] > baseline_ci_lo:
            t3_better += 1
        emit(
            lines,
            f"{hhmm:>6} {int(sm['n_cycles']):5d} {sm['mean_day']:10.4f} "
            f"{sm['cpd']:7.3f} {sm['avg_hold']:7.2f} {sm['ci_lo']:9.4f} "
            f"{sm['worst']:9.4f} {sm['mdd']:9.4f} {sm['fees_per_day']:9.4f} "
            f"{avg_prem:8.1f} {avg_dist:8.1f} {avg_adj:6.2f}",
        )
    chance_line(lines, t3_better, len(entry_slots))
    emit(lines, "")

    # =====================================================================
    # TEST 4
    # =====================================================================
    emit(lines, "===== TEST 4: POST-ADJ TARGET × QTY DECREASE =====")
    emit(
        lines,
        "after adj: target = (adj_leg_loss + all_fees) × m ; "
        "m=NO_CHANGE means no post-adj target",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'m':>10} {'dec%':>5} {'n':>5} {'mean/day':>10} {'cpd':>7} "
        f"{'hold_h':>7} {'ci_lo':>9} {'worst':>9} {'mdd':>9} {'fees/day':>9}",
    )
    emit(lines, "-" * 100)

    m_vals: list[float | None] = [1.0, 1.2, 1.5, None]
    dec_vals = [20.0, 35.0, 50.0]
    t4_better = 0
    t4_total = 0
    cfg_i = 0
    for m in m_vals:
        for dec in dec_vals:
            t4_total += 1
            mlab = "NO_CHANGE" if m is None else f"{m:.1f}"
            logger.info("TEST4 m=%s dec=%s", mlab, dec)
            outs = []
            for i, o in enumerate(base11):
                if (i + 1) % 100 == 0:
                    logger.info(
                        "  postadj m=%s dec=%s %s/%s",
                        mlab,
                        dec,
                        i + 1,
                        len(base11),
                    )
                outs.append(
                    simulate_rules(
                        o,
                        idx,
                        times,
                        closes,
                        decrease_pct=dec,
                        post_adj_mult=m,
                    )
                )
            sm = summarize_daily(
                outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 400 + cfg_i
            )
            cfg_i += 1
            if sm["ci_lo"] > baseline_ci_lo:
                t4_better += 1
            emit(
                lines,
                f"{mlab:>10} {dec:5.0f} {int(sm['n_cycles']):5d} "
                f"{sm['mean_day']:10.4f} {sm['cpd']:7.3f} {sm['avg_hold']:7.2f} "
                f"{sm['ci_lo']:9.4f} {sm['worst']:9.4f} {sm['mdd']:9.4f} "
                f"{sm['fees_per_day']:9.4f}",
            )
    chance_line(lines, t4_better, t4_total)
    emit(lines, "")
    emit(lines, f"Total configs: {5 + 5 + 6 + 12} = 28")
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
