#!/usr/bin/env python3
"""
S001 trigger boundary + adaptive range (realized-vol) tests.

TEST 1: adj_b_trigger_pct ∈ {30,40,50,60,70,90}
TEST 2: strike_distance = k × spot × σ × √T ; k∈{0.5,0.75,1.0,1.25}
         × (fixed 2DTE | expiry matched to B25 premium)

Baseline: dte2 B_only maker B25 wings2000 qty=8 11:00 IST
          hedge OFF, dec%=40, profit target k=1.0, trigger=70

Output: backtest/results/s001_trigger_and_adaptive.txt
"""

from __future__ import annotations

import bisect
import logging
import math
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
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_income_engine as eng  # noqa: E402
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)

logger = logging.getLogger("s001_trigger_and_adaptive")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_trigger_and_adaptive.txt"

BASKET_QTY = 8
WING_POINTS = 2000.0
ENTRY_HHMM = "11:00"
DEC_PCT = 40.0
PROFIT_K = 1.0
BASE_TRIGGER = 70.0
TRIGGERS = (30.0, 40.0, 50.0, 60.0, 70.0, 90.0)
ADAPT_KS = (0.5, 0.75, 1.0, 1.25)
RV_DAYS = 20
MINUTES_PER_YEAR = 365.25 * 24.0 * 60.0
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CV = eng.CONTRACT_VALUE


@dataclass
class SimOut:
    net: float
    fees: float
    hold_hours: float
    n_adjustments: int
    mins_to_first_adj: float  # nan if none
    entry_premium: float
    strike_dist: float
    dte: float
    range_broke: bool
    entry_date: date
    exit_ts: int


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def chance_line(lines: list[str], better: int, total: int) -> None:
    emit(
        lines,
        f"configs with ci_lo > baseline ci_lo: {better} / {total} "
        f"(chance exp: {total * 0.05:.2f})",
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
        if o.wing_points != WING_POINTS:
            continue
        if o.entry_hhmm != ENTRY_HHMM:
            continue
        if o.wing_call is None or o.wing_put is None:
            continue
        out.append(o)
    return out


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


def years_to_expiry(entry_utc: datetime, exp: date) -> float:
    settle = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    sec = max(0.0, (settle - entry_utc).total_seconds())
    return sec / (365.25 * 24.0 * 3600.0)


def trailing_rv_ann(
    times: list[int], closes: list[float], entry_ts: int, days: int = RV_DAYS
) -> float | None:
    """Annualized log-return vol from 1m closes over trailing `days` calendar days."""
    t0 = entry_ts - int(days * 86400)
    i0 = bisect.bisect_left(times, t0)
    i1 = bisect.bisect_right(times, entry_ts) - 1
    if i1 - i0 < 100:
        return None
    rets: list[float] = []
    prev = closes[i0]
    for i in range(i0 + 1, i1 + 1):
        c = closes[i]
        if prev > 0 and c > 0:
            rets.append(math.log(c / prev))
        prev = c
    if len(rets) < 50:
        return None
    return statistics.stdev(rets) * math.sqrt(MINUTES_PER_YEAR)


def snap_otm_pair(
    strikes: set[float], spot: float, call_tgt: float, put_tgt: float
) -> tuple[float, float] | None:
    call_side = sorted(k for k in strikes if k > spot)
    put_side = sorted((k for k in strikes if k < spot), reverse=True)
    if not call_side or not put_side:
        return None
    beyond_c = [k for k in call_side if k >= call_tgt]
    beyond_p = [k for k in put_side if k <= put_tgt]
    sc = min(beyond_c) if beyond_c else min(call_side, key=lambda k: (abs(k - call_tgt), k))
    sp = max(beyond_p) if beyond_p else min(put_side, key=lambda k: (abs(k - put_tgt), -k))
    if sc <= spot or sp >= spot or sc <= sp:
        return None
    return float(sc), float(sp)


def build_cycle_strikes(
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    surface: object | None,
    *,
    day: date,
    expiry: date,
    entry_utc: datetime,
    spot_e: float,
    sc_k: float,
    sp_k: float,
    strike_mode: str,
    short_dte: int,
) -> eng.CycleObs | None:
    if sc_k <= spot_e or sp_k >= spot_e or sc_k <= sp_k:
        return None
    settle_ts = int(
        datetime(expiry.year, expiry.month, expiry.day, 12, 0, tzinfo=UTC).timestamp()
    )
    if int(entry_utc.timestamp()) >= settle_ts:
        return None
    spot_s = eng.settle_spot_1200_utc(times, closes, expiry)
    if spot_s is None or spot_s <= 0:
        return None
    short_role, long_role = eng.roles_for_package("maker")
    sc = eng.nearest_print_prefer(
        idx,
        eng.format_symbol("C", sc_k, expiry),
        entry_utc,
        eng.PRINT_WINDOW_SEC,
        short_role,
    )
    sp = eng.nearest_print_prefer(
        idx,
        eng.format_symbol("P", sp_k, expiry),
        entry_utc,
        eng.PRINT_WINDOW_SEC,
        short_role,
    )
    if sc is None or sp is None:
        return None
    wk = eng.pick_wing_strikes(idx, expiry, sc_k, sp_k, WING_POINTS)
    if wk is None:
        return None
    wc_k, wp_k = wk
    wc = eng.wing_fill_or_surface(
        idx, surface, expiry, wc_k, "C", entry_utc, long_role
    )
    wp = eng.wing_fill_or_surface(
        idx, surface, expiry, wp_k, "P", entry_utc, long_role
    )
    if wc is None or wp is None:
        return None
    atm = eng.pick_atm_straddle(idx, expiry, float(spot_e), entry_utc, long_role)
    if atm is None:
        atm = eng.pick_atm_straddle(idx, expiry, float(spot_e), entry_utc, short_role)
    atm_prem = (atm[1].price + atm[2].price) if atm is not None else 0.0
    return eng.CycleObs(
        entry_date=day,
        entry_hhmm=ENTRY_HHMM,
        entry_utc=entry_utc,
        basket_expiry=expiry,
        short_dte=int(short_dte),
        fill_package="maker",
        strike_mode=strike_mode,
        wing_points=WING_POINTS,
        spot_entry=float(spot_e),
        spot_settle=float(spot_s),
        atm_straddle_prem=atm_prem,
        target_premium=0.0,
        short_call_k=float(sc_k),
        short_put_k=float(sp_k),
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


def simulate(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    trigger_pct: float,
    decrease_pct: float = DEC_PCT,
    profit_target: float | None = None,
) -> SimOut:
    adj_b_trig = sweep.adj_b_pct_from_trigger(float(trigger_pct))
    exp = o.basket_expiry
    short_role_s = sweep.short_role()
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
    strike_dist = (abs(sc_k - spot_e) + abs(sp_k - spot_e)) / 2.0
    dte = float(o.short_dte)

    realized = 0.0
    adj_count = 0
    closed_early = False
    exit_reason = "settle"
    active_target = profit_target
    mins_to_first = float("nan")
    range_broke = False
    entry_sc_k = sc_k
    entry_sp_k = sp_k

    t0 = int(o.entry_utc.timestamp())
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    exit_ts = t_end

    if t_end <= t0:
        return SimOut(
            net=float("nan"),
            fees=fees,
            hold_hours=0.0,
            n_adjustments=0,
            mins_to_first_adj=float("nan"),
            entry_premium=entry_prem,
            strike_dist=strike_dist,
            dte=dte,
            range_broke=False,
            entry_date=o.entry_date,
            exit_ts=t0,
        )

    t = t0 + sweep.MONITOR_STEP_SEC
    while t < t_end and not closed_early:
        when = datetime.fromtimestamp(t, tz=UTC)
        spot = ot.spot_at(times, closes, t)
        if spot is None or spot <= 0:
            t += sweep.MONITOR_STEP_SEC
            continue
        if spot >= entry_sc_k or spot <= entry_sp_k:
            range_broke = True

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
            exit_reason = "profit_target"
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
        if adj_count == 1:
            mins_to_first = (t - t0) / 60.0
        t += sweep.MONITOR_STEP_SEC

    if not closed_early:
        spot_s = float(o.spot_settle)
        if spot_s >= entry_sc_k or spot_s <= entry_sp_k:
            range_broke = True
        pnl, _settle_fee = sweep.settle_legs(
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
        exit_ts = t_end
        _ = exit_reason
    else:
        net = realized - fees

    hold_h = max(0.0, (exit_ts - t0) / 3600.0)
    return SimOut(
        net=net,
        fees=fees,
        hold_hours=hold_h,
        n_adjustments=adj_count,
        mins_to_first_adj=mins_to_first,
        entry_premium=entry_prem,
        strike_dist=strike_dist,
        dte=dte,
        range_broke=range_broke,
        entry_date=o.entry_date,
        exit_ts=exit_ts,
    )


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


def summarize(
    outs: list[SimOut], day_span: int, d0: date, d1: date, *, seed: int
) -> dict[str, float]:
    ok = [s for s in outs if math.isfinite(s.net)]
    daily = daily_series(ok, day_span, d0, d1)
    mean, lo, hi = eng.bootstrap_mean_ci(daily, BOOTSTRAP_N, seed)
    mdd = eng.max_drawdown(daily) if daily else float("nan")
    holds = [s.hold_hours for s in ok]
    fees_tot = sum(s.fees for s in ok)
    adjs = [float(s.n_adjustments) for s in ok]
    firsts = [
        s.mins_to_first_adj
        for s in ok
        if math.isfinite(s.mins_to_first_adj)
    ]
    dists = [s.strike_dist for s in ok]
    dtes = [s.dte for s in ok]
    prems = [s.entry_premium for s in ok]
    broke = sum(1 for s in ok if s.range_broke)
    return {
        "n": float(len(ok)),
        "mean_day": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "worst": min((s.net for s in ok), default=float("nan")),
        "mdd": mdd,
        "avg_adj": statistics.mean(adjs) if adjs else float("nan"),
        "avg_hold": statistics.mean(holds) if holds else float("nan"),
        "fees_per_day": fees_tot / float(max(1, day_span)),
        "avg_mins_1st": statistics.mean(firsts) if firsts else float("nan"),
        "avg_dist": statistics.mean(dists) if dists else float("nan"),
        "avg_dte": statistics.mean(dtes) if dtes else float("nan"),
        "avg_prem": statistics.mean(prems) if prems else float("nan"),
        "break_pct": 100.0 * broke / float(max(1, len(ok))),
    }


def run_one_config(
    cycles: list[eng.CycleObs],
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    trigger: float,
    label: str,
) -> list[SimOut]:
    outs: list[SimOut] = []
    for i, o in enumerate(cycles):
        if (i + 1) % 100 == 0:
            logger.info("  %s %s/%s", label, i + 1, len(cycles))
        target = entry_cost_usd(o) * PROFIT_K
        outs.append(
            simulate(
                o,
                idx,
                times,
                closes,
                trigger_pct=trigger,
                decrease_pct=DEC_PCT,
                profit_target=target,
            )
        )
    return outs


def available_expiries_after(
    idx: eng.TradeIndex, entry_utc: datetime, entry_day: date
) -> list[date]:
    ts = int(entry_utc.timestamp())
    out: list[date] = []
    for exp in sorted(idx.expiries):
        if exp < entry_day:
            continue
        settle_ts = int(
            datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp()
        )
        if settle_ts <= ts:
            continue
        # Cap search to ~45 DTE to keep premium matching tractable
        if (exp - entry_day).days > 45:
            continue
        out.append(exp)
    return out


def build_adaptive_cycle(
    base: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    surface: Any,
    *,
    k_mult: float,
    sigma: float,
    mode: str,  # fixed2 | match_prem
    b25_target: float,
) -> eng.CycleObs | None:
    spot = float(base.spot_entry)
    entry_utc = base.entry_utc
    day = base.entry_date

    if mode == "fixed2":
        exp = base.basket_expiry
        t_yr = years_to_expiry(entry_utc, exp)
        if t_yr <= 0:
            return None
        dist = k_mult * spot * sigma * math.sqrt(t_yr)
        strikes = idx.strikes_by_expiry.get(exp) or set()
        pair = snap_otm_pair(strikes, spot, spot + dist, spot - dist)
        if pair is None:
            return None
        sc_k, sp_k = pair
        return build_cycle_strikes(
            idx,
            times,
            closes,
            surface,
            day=day,
            expiry=exp,
            entry_utc=entry_utc,
            spot_e=spot,
            sc_k=sc_k,
            sp_k=sp_k,
            strike_mode=f"AVOL_k{k_mult}_2DTE",
            short_dte=2,
        )

    # match_prem: pick expiry whose strangle premium nearest B25 target
    best: tuple[float, eng.CycleObs] | None = None
    for exp in available_expiries_after(idx, entry_utc, day):
        t_yr = years_to_expiry(entry_utc, exp)
        if t_yr <= 0:
            continue
        dist = k_mult * spot * sigma * math.sqrt(t_yr)
        strikes = idx.strikes_by_expiry.get(exp) or set()
        pair = snap_otm_pair(strikes, spot, spot + dist, spot - dist)
        if pair is None:
            continue
        sc_k, sp_k = pair
        dte = (exp - day).days
        co = build_cycle_strikes(
            idx,
            times,
            closes,
            surface,
            day=day,
            expiry=exp,
            entry_utc=entry_utc,
            spot_e=spot,
            sc_k=sc_k,
            sp_k=sp_k,
            strike_mode=f"AVOL_k{k_mult}_MATCH",
            short_dte=dte,
        )
        if co is None:
            continue
        prem = float(co.short_call.price) + float(co.short_put.price)
        score = abs(prem - b25_target)
        if best is None or score < best[0]:
            best = (score, co)
    return best[1] if best is not None else None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 TRIGGER BOUNDARY + ADAPTIVE RANGE (realized vol)")
    emit(lines, "=" * 100)
    emit(
        lines,
        "BASELINE: dte2 B_only maker B25 wings2000 qty=8 11:00 IST "
        f"HEDGE=OFF dec%={DEC_PCT:g} profit_k={PROFIT_K:g} trigger={BASE_TRIGGER:g}",
    )
    emit(
        lines,
        "expected_move = spot × σ_ann × √T ; σ = 20d trailing 1m log-return vol",
    )
    emit(lines, "")

    logger.info("Loading cache + index...")
    all_obs, day_span = sweep.load_cycles()
    base = filter_base(all_obs)
    logger.info("base cycles=%s day_span=%s", len(base), day_span)
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    surface = eng.load_surface_optional()
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()

    # =====================================================================
    # TEST 1
    # =====================================================================
    emit(lines, "===== TEST 1: TRIGGER BOUNDARY (adj_b_trigger_pct) =====")
    emit(
        lines,
        "Note: Adj B decayed when prem < base×(trig/100). "
        "Higher trig = easier/earlier adj; lower = stricter/later.",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'trig':>6} {'n':>5} {'mean/day':>10} {'ci_lo':>9} {'ci_hi':>9} "
        f"{'worst':>9} {'mdd':>9} {'adj/c':>6} {'hold_h':>7} {'fees/d':>8} "
        f"{'mins1st':>8}",
    )
    emit(lines, "-" * 110)

    baseline_ci = float("nan")
    t1_better = 0
    below70_better_mean = 0
    results_t1: dict[float, dict[str, float]] = {}

    for ti, trig in enumerate(TRIGGERS):
        logger.info("TEST1 trigger=%s", trig)
        outs = run_one_config(
            base, idx, times, closes, trigger=trig, label=f"trig{trig:g}"
        )
        sm = summarize(outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 10 + ti)
        results_t1[trig] = sm
        if abs(trig - BASE_TRIGGER) < 1e-9:
            baseline_ci = sm["ci_lo"]
        emit(
            lines,
            f"{trig:6.0f} {int(sm['n']):5d} {sm['mean_day']:10.4f} {sm['ci_lo']:9.4f} "
            f"{sm['ci_hi']:9.4f} {sm['worst']:9.4f} {sm['mdd']:9.4f} "
            f"{sm['avg_adj']:6.2f} {sm['avg_hold']:7.2f} {sm['fees_per_day']:8.4f} "
            f"{sm['avg_mins_1st']:8.1f}",
        )

    for trig in TRIGGERS:
        if results_t1[trig]["ci_lo"] > baseline_ci:
            t1_better += 1
        if trig < BASE_TRIGGER and results_t1[trig]["mean_day"] > results_t1[BASE_TRIGGER][
            "mean_day"
        ]:
            below70_better_mean += 1

    chance_line(lines, t1_better, len(TRIGGERS))
    emit(lines, "")
    # Verdict on "improvement continues below 70"
    below = [t for t in TRIGGERS if t < BASE_TRIGGER]
    below_beats = [
        t
        for t in below
        if results_t1[t]["ci_lo"] > baseline_ci
        or results_t1[t]["mean_day"] >= results_t1[BASE_TRIGGER]["mean_day"]
    ]
    if len(below_beats) >= 2:
        emit(
            lines,
            "VERDICT: sudhaar 70 se neeche bhi dikhta hai "
            f"({[int(t) for t in below_beats]}) — sharp optimum nahi; "
            "trigger zyada 'adjust sooner vs later' continuum jaisa behave karta hai "
            "(yaad: neeche trig = zyada strict/late Adj B).",
        )
    elif len(below_beats) == 0:
        emit(
            lines,
            "VERDICT: 70 se neeche koi config baseline se behtar nahi — "
            "70 local optimum / floor dikhta hai; 'jitna jaldi adjust' proxy confirm "
            "nahi hota (lower trig = later Adj B).",
        )
    else:
        emit(
            lines,
            f"VERDICT: mixed — below-70 that match/beat 70: {[int(t) for t in below_beats]}.",
        )
    emit(lines, "")

    # =====================================================================
    # TEST 2
    # =====================================================================
    emit(lines, "===== TEST 2: ADAPTIVE RANGE (realized vol) =====")
    emit(lines, "Variants: FIXED_2DTE | MATCH_PREM (expiry ≈ B25 premium)")
    emit(lines, "")
    emit(
        lines,
        f"{'cfg':<22} {'n':>5} {'mean/day':>10} {'ci_lo':>9} {'worst':>9} "
        f"{'mdd':>9} {'dist':>8} {'dte':>6} {'prem':>8} {'break%':>7}",
    )
    emit(lines, "-" * 110)

    # Baseline B25 under same sim knobs
    logger.info("TEST2 baseline B25...")
    base_outs = run_one_config(
        base, idx, times, closes, trigger=BASE_TRIGGER, label="B25"
    )
    base_sm = summarize(base_outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 100)
    baseline_ci_t2 = base_sm["ci_lo"]
    emit(
        lines,
        f"{'B25_BASE':<22} {int(base_sm['n']):5d} {base_sm['mean_day']:10.4f} "
        f"{base_sm['ci_lo']:9.4f} {base_sm['worst']:9.4f} {base_sm['mdd']:9.4f} "
        f"{base_sm['avg_dist']:8.1f} {base_sm['avg_dte']:6.2f} "
        f"{base_sm['avg_prem']:8.1f} {base_sm['break_pct']:7.1f}",
    )

    t2_better = 0
    cfg_i = 0
    for k_mult in ADAPT_KS:
        for mode in ("fixed2", "match_prem"):
            cfg_i += 1
            label = f"k={k_mult:g}_{'2DTE' if mode == 'fixed2' else 'MATCH'}"
            logger.info("TEST2 %s", label)
            cycles: list[eng.CycleObs] = []
            for j, o in enumerate(base):
                if (j + 1) % 100 == 0:
                    logger.info("  build %s %s/%s", label, j + 1, len(base))
                ts = int(o.entry_utc.timestamp())
                sigma = trailing_rv_ann(times, closes, ts)
                if sigma is None or sigma <= 0:
                    continue
                b25_tgt = float(o.target_premium)
                if b25_tgt <= 0:
                    b25_tgt = 0.25 * float(o.atm_straddle_prem)
                co = build_adaptive_cycle(
                    o,
                    idx,
                    times,
                    closes,
                    surface,
                    k_mult=k_mult,
                    sigma=sigma,
                    mode=mode,
                    b25_target=b25_tgt,
                )
                if co is not None:
                    cycles.append(co)
            outs = run_one_config(
                cycles, idx, times, closes, trigger=BASE_TRIGGER, label=label
            )
            sm = summarize(
                outs, day_span, d0, d1, seed=BOOTSTRAP_SEED + 200 + cfg_i
            )
            if sm["ci_lo"] > baseline_ci_t2:
                t2_better += 1
            emit(
                lines,
                f"{label:<22} {int(sm['n']):5d} {sm['mean_day']:10.4f} "
                f"{sm['ci_lo']:9.4f} {sm['worst']:9.4f} {sm['mdd']:9.4f} "
                f"{sm['avg_dist']:8.1f} {sm['avg_dte']:6.2f} "
                f"{sm['avg_prem']:8.1f} {sm['break_pct']:7.1f}",
            )

    chance_line(lines, t2_better, 8)
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
