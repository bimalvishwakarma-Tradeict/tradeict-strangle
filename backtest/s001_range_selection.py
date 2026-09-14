#!/usr/bin/env python3
"""
S001 range selection diagnostics.

PART A: adjustment_qty_decrease_pct plateau (20/30/35/40/50)
PART B: 50% retracement claim on 4h swings vs random null
PART C: PREMIUM vs FIXED_DISTANCE vs ULP_RANGE (+ matched-width control)

Baseline: dte2 B_only trig70 maker B25 wings2000 qty=8 11:00 IST
          hedge OFF, decrease_pct=35

Output: backtest/results/s001_range_selection.txt
"""

from __future__ import annotations

import logging
import math
import random
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
import s001_exit_rules as exit_rules  # noqa: E402
import s001_income_engine as eng  # noqa: E402
import ulp_zones as uz  # noqa: E402
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402

logger = logging.getLogger("s001_range_selection")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_range_selection.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
BASKET_QTY = 8
WING_POINTS = 2000.0
ENTRY_HHMM = "11:00"
BASE_DEC_PCT = 35.0
DEC_PCTS = (20.0, 30.0, 35.0, 40.0, 50.0)
FIXED_DISTS = (1300.0, 2000.0, 3000.0, 4000.0)
ULP_X = (0.0, 500.0, 1000.0)
ULP_TF = "15m"
ULP_LOOKBACK = 5
ULP_SHRINK = False  # confirmation-lag safe default from ulp_zones
RETRACE_LOOKBACK = 3
RETRACE_NS = (6, 12, 24, 48)
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
FOUR_H_SEC = 4 * 3600


@dataclass
class SimRow:
    net: float
    fees: float
    entry_date: date
    strike_width: float
    range_broke: bool


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def qty_sequence(original: int, decrease_pct: float, max_adj: int = 2) -> list[int]:
    path = [int(original)]
    for n in range(1, max_adj + 1):
        nq, close = compute_decrease_step_qty(
            original_qty=original,
            adjustment_number=n,
            decrease_pct=decrease_pct,
        )
        if close or nq is None:
            break
        path.append(int(nq))
    return path


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


def daily_nets(
    rows: list[SimRow], day_span: int, d0: date, d1: date
) -> list[float]:
    by: dict[date, float] = {}
    for r in rows:
        if not math.isfinite(r.net):
            continue
        by[r.entry_date] = by.get(r.entry_date, 0.0) + r.net
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
    rows: list[SimRow], day_span: int, d0: date, d1: date, *, seed: int
) -> dict[str, float]:
    ok = [r for r in rows if math.isfinite(r.net)]
    daily = daily_nets(ok, day_span, d0, d1)
    mean, lo, hi = eng.bootstrap_mean_ci(daily, BOOTSTRAP_N, seed)
    mdd = eng.max_drawdown(daily) if daily else float("nan")
    fees_tot = sum(r.fees for r in ok)
    widths = [r.strike_width for r in ok if r.strike_width > 0]
    broke = sum(1 for r in ok if r.range_broke)
    return {
        "n": float(len(ok)),
        "mean_day": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "worst": min((r.net for r in ok), default=float("nan")),
        "mdd": mdd,
        "cpd": len(ok) / float(max(1, day_span)),
        "fees_per_day": fees_tot / float(max(1, day_span)),
        "avg_width": statistics.mean(widths) if widths else float("nan"),
        "break_pct": 100.0 * broke / float(max(1, len(ok))),
    }


def range_broke_path(
    o: eng.CycleObs,
    times: list[int],
    closes: list[float],
    *,
    sc_k: float,
    sp_k: float,
    exit_ts: int,
) -> bool:
    t0 = int(o.entry_utc.timestamp())
    t = t0
    step = sweep.MONITOR_STEP_SEC
    while t <= exit_ts:
        spot = ot.spot_at(times, closes, t)
        if spot is not None and spot > 0:
            if spot >= sc_k or spot <= sp_k:
                return True
        t += step
    return False


def simulate_one(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    decrease_pct: float,
) -> SimRow:
    s = exit_rules.simulate_rules(
        o, idx, times, closes, decrease_pct=decrease_pct
    )
    width = abs(float(o.short_call_k) - float(o.short_put_k))
    broke = False
    if math.isfinite(s.net):
        # Approximate with entry strikes over hold (Adj B moves inward →
        # understates breaks slightly; consistent across methods).
        broke = range_broke_path(
            o,
            times,
            closes,
            sc_k=float(o.short_call_k),
            sp_k=float(o.short_put_k),
            exit_ts=s.exit_ts,
        )
    return SimRow(
        net=s.net,
        fees=s.fees,
        entry_date=o.entry_date,
        strike_width=width,
        range_broke=broke,
    )


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
) -> eng.CycleObs | None:
    if sc_k <= spot_e or sp_k >= spot_e:
        return None
    if sc_k <= sp_k:
        return None
    settle_ts = int(
        datetime(expiry.year, expiry.month, expiry.day, 12, 0, tzinfo=UTC).timestamp()
    )
    ts = int(entry_utc.timestamp())
    if ts >= settle_ts:
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
        short_dte=2,
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


def snap_otm_pair(
    strikes: set[float], spot: float, call_tgt: float, put_tgt: float
) -> tuple[float, float] | None:
    sc = eng.nearest_strike(strikes, call_tgt)
    sp = eng.nearest_strike(strikes, put_tgt)
    if sc is None or sp is None:
        return None
    # Ensure OTM sides; if snap crossed spot, walk outward on available strikes
    call_side = sorted(k for k in strikes if k > spot)
    put_side = sorted((k for k in strikes if k < spot), reverse=True)
    if not call_side or not put_side:
        return None
    if sc <= spot:
        sc = call_side[0]
    if sp >= spot:
        sp = put_side[0]
    # Prefer nearest at-or-beyond target when possible
    beyond_c = [k for k in call_side if k >= call_tgt]
    beyond_p = [k for k in put_side if k <= put_tgt]
    if beyond_c:
        sc = min(beyond_c)
    else:
        sc = min(call_side, key=lambda k: (abs(k - call_tgt), k))
    if beyond_p:
        sp = max(beyond_p)
    else:
        sp = min(put_side, key=lambda k: (abs(k - put_tgt), -k))
    if sc <= spot or sp >= spot or sc <= sp:
        return None
    return float(sc), float(sp)


# ---------------------------------------------------------------------------
# PART B — 4h swings + null
# ---------------------------------------------------------------------------


@dataclass
class OHLC:
    time: int
    open: float
    high: float
    low: float
    close: float


def load_1m_ohlc() -> list[OHLC]:
    matches = sorted(ot.DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_1m_*.csv in {ot.DATA_1M_DIR}")
    path = matches[-1]
    out: list[OHLC] = []
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(
                OHLC(
                    time=int(row["open_time_unix"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    return out


def aggregate_4h(bars_1m: list[OHLC]) -> list[OHLC]:
    if not bars_1m:
        return []
    buckets: dict[int, list[OHLC]] = {}
    for b in bars_1m:
        key = (b.time // FOUR_H_SEC) * FOUR_H_SEC
        buckets.setdefault(key, []).append(b)
    out: list[OHLC] = []
    for key in sorted(buckets):
        chunk = buckets[key]
        out.append(
            OHLC(
                time=key,
                open=chunk[0].open,
                high=max(x.high for x in chunk),
                low=min(x.low for x in chunk),
                close=chunk[-1].close,
            )
        )
    return out


def find_swings_4h(
    bars: list[OHLC], lookback: int = RETRACE_LOOKBACK
) -> tuple[list[int], list[int]]:
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    sh: list[int] = []
    sl: list[int] = []
    n = len(bars)
    # Confirm at pivot + lookback (no look-ahead), same spirit as ULP
    for confirm in range(2 * lookback, n):
        pivot = confirm - lookback
        if uz.is_pivot_high(highs, pivot, lookback, lookback):
            sh.append(pivot)
        if uz.is_pivot_low(lows, pivot, lookback, lookback):
            sl.append(pivot)
    return sh, sl


def prior_opposite(pivots: list[int], before: int) -> int | None:
    prev = [p for p in pivots if p < before]
    return prev[-1] if prev else None


def retrace_stats(
    bars: list[OHLC],
    events: list[tuple[int, str, float, float]],
    n_forward: int,
) -> dict[str, float]:
    """
    events: (pivot_index, 'H'|'L', extreme, opposite)
    50% level from impulse extreme→opposite.
    """
    hits = 0
    total = 0
    retrace_pcts: list[float] = []
    times_to: list[float] = []
    for piv, side, extreme, opposite in events:
        confirm = piv + RETRACE_LOOKBACK
        if confirm >= len(bars):
            continue
        impulse = abs(extreme - opposite)
        if impulse <= 1e-9:
            continue
        if side == "H":
            target = extreme - 0.5 * impulse
        else:
            target = extreme + 0.5 * impulse
        end = min(len(bars) - 1, confirm + n_forward)
        hit_i: int | None = None
        max_retrace = 0.0
        for j in range(confirm + 1, end + 1):
            if side == "H":
                retr = (extreme - bars[j].low) / impulse
                max_retrace = max(max_retrace, retr)
                if bars[j].low <= target and hit_i is None:
                    hit_i = j
            else:
                retr = (bars[j].high - extreme) / impulse
                max_retrace = max(max_retrace, retr)
                if bars[j].high >= target and hit_i is None:
                    hit_i = j
        total += 1
        retrace_pcts.append(100.0 * max_retrace)
        if hit_i is not None:
            hits += 1
            times_to.append(float(hit_i - confirm))  # 4h bars
    return {
        "n": float(total),
        "hit_pct": 100.0 * hits / float(max(1, total)),
        "avg_retrace_pct": statistics.mean(retrace_pcts) if retrace_pcts else float("nan"),
        "med_bars_to_50": (
            float(statistics.median(times_to)) if times_to else float("nan")
        ),
    }


def build_swing_events(
    bars: list[OHLC], sh: list[int], sl: list[int]
) -> list[tuple[int, str, float, float]]:
    events: list[tuple[int, str, float, float]] = []
    for p in sh:
        opp_i = prior_opposite(sl, p)
        if opp_i is None:
            continue
        events.append((p, "H", bars[p].high, bars[opp_i].low))
    for p in sl:
        opp_i = prior_opposite(sh, p)
        if opp_i is None:
            continue
        events.append((p, "L", bars[p].low, bars[opp_i].high))
    return events


def build_random_events(
    bars: list[OHLC],
    sh: list[int],
    sl: list[int],
    n_events: int,
    seed: int,
) -> list[tuple[int, str, float, float]]:
    """
    Null: random 4h bars (not restricted to swings), same opposite-swing
    impulse definition when a prior opposite swing exists.
    """
    rng = random.Random(seed)
    n = len(bars)
    lb = RETRACE_LOOKBACK
    candidates_h: list[int] = []
    candidates_l: list[int] = []
    for i in range(lb, n - lb - max(RETRACE_NS) - 1):
        if prior_opposite(sl, i) is not None:
            candidates_h.append(i)
        if prior_opposite(sh, i) is not None:
            candidates_l.append(i)
    # Exclude actual swing pivots from null set
    sh_set, sl_set = set(sh), set(sl)
    candidates_h = [i for i in candidates_h if i not in sh_set]
    candidates_l = [i for i in candidates_l if i not in sl_set]
    events: list[tuple[int, str, float, float]] = []
    # Match swing event mix roughly 50/50 H/L up to n_events
    need = max(1, n_events)
    for _ in range(need):
        if candidates_h and (not candidates_l or rng.random() < 0.5):
            i = candidates_h[rng.randrange(len(candidates_h))]
            opp = prior_opposite(sl, i)
            assert opp is not None
            events.append((i, "H", bars[i].high, bars[opp].low))
        elif candidates_l:
            i = candidates_l[rng.randrange(len(candidates_l))]
            opp = prior_opposite(sh, i)
            assert opp is not None
            events.append((i, "L", bars[i].low, bars[opp].high))
        else:
            break
    return events


# ---------------------------------------------------------------------------
# PART C — ULP levels
# ---------------------------------------------------------------------------


def resolve_tf_csv(tf: str) -> Path:
    matches = sorted(ot.DATA_1M_DIR.glob(f"BTCUSD_{tf}_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_{tf}_*.csv in {ot.DATA_1M_DIR}")
    return matches[-1]


def build_ulp_snapshot(
    bars: list[uz.Bar],
    *,
    lookback: int,
    shrink_during_confirmation: bool,
) -> tuple[list[int], list[list[float]], list[list[float]]]:
    """
    Per-bar active HIGH zone_tops and LOW zone_bots after applying that bar.
    Uses ulp_zones primitives only (confirm at pivot+lookback — no lag bias).
    """
    n = len(bars)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    opens = [b.open for b in bars]
    closes = [b.close for b in bars]
    high_set = uz.ActiveHighSet()
    low_set = uz.ActiveLowSet()
    next_id = 0
    times: list[int] = [b.time for b in bars]
    tops_series: list[list[float]] = [[] for _ in range(n)]
    bots_series: list[list[float]] = [[] for _ in range(n)]

    for i in range(n):
        pivot = i - lookback
        if pivot >= lookback:
            if uz.is_pivot_high(highs, pivot, lookback, lookback):
                top = highs[pivot]
                bot = max(opens[pivot], closes[pivot])
                z = uz.Zone(
                    zone_id=next_id,
                    side="HIGH",
                    pivot_index=pivot,
                    confirm_index=i,
                    zone_top=top,
                    zone_bot=bot,
                    top_birth=top,
                    bot_birth=bot,
                    zero_height=(top == bot),
                )
                next_id += 1
                if shrink_during_confirmation:
                    uz.replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                if z.still_alive:
                    high_set.insert(z)
            if uz.is_pivot_low(lows, pivot, lookback, lookback):
                top = min(opens[pivot], closes[pivot])
                bot = lows[pivot]
                z = uz.Zone(
                    zone_id=next_id,
                    side="LOW",
                    pivot_index=pivot,
                    confirm_index=i,
                    zone_top=top,
                    zone_bot=bot,
                    top_birth=top,
                    bot_birth=bot,
                    zero_height=(top == bot),
                )
                next_id += 1
                if shrink_during_confirmation:
                    uz.replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                if z.still_alive:
                    low_set.insert(z)

        bar = bars[i]
        for z in list(high_set.candidates_touching(bar.high)):
            action = uz.apply_bar_to_high_zone(z, bar, i)
            if action == "remove":
                high_set.remove(z)
            elif action == "moved":
                high_set.remove(z)
                if z.still_alive:
                    high_set.insert(z)
        for z in list(low_set.candidates_touching(bar.low)):
            action = uz.apply_bar_to_low_zone(z, bar, i)
            if action == "remove":
                low_set.remove(z)
            elif action == "moved":
                low_set.remove(z)
                if z.still_alive:
                    low_set.insert(z)

        tops_series[i] = [z.zone_top for z in high_set.zones]
        bots_series[i] = [z.zone_bot for z in low_set.zones]

    return times, tops_series, bots_series


def ulp_range_at(
    ts: int,
    spot: float,
    bar_times: list[int],
    tops_series: list[list[float]],
    bots_series: list[list[float]],
    x_out: float,
) -> tuple[float, float] | None:
    import bisect

    i = bisect.bisect_right(bar_times, ts) - 1
    if i < 0:
        return None
    tops = [t for t in tops_series[i] if t > spot]
    bots = [b for b in bots_series[i] if b < spot]
    if not tops or not bots:
        return None
    ulp_high = min(tops)
    ulp_low = max(bots)
    return ulp_high + x_out, ulp_low - x_out


def paired_z(a_by_day: dict[date, float], b_by_day: dict[date, float]) -> float:
    common = sorted(set(a_by_day) & set(b_by_day))
    if len(common) < 2:
        return float("nan")
    diffs = [a_by_day[d] - b_by_day[d] for d in common]
    mu = statistics.mean(diffs)
    if len(diffs) < 2:
        return float("nan")
    sd = statistics.stdev(diffs)
    if sd <= 1e-12:
        return float("nan") if abs(mu) < 1e-12 else math.copysign(float("inf"), mu)
    return mu / (sd / math.sqrt(len(diffs)))


def run_part_a(
    lines: list[str],
    base: list[eng.CycleObs],
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    day_span: int,
    d0: date,
    d1: date,
) -> dict[float, dict[str, float]]:
    emit(lines, "===== PART A: dec% PLATEAU =====")
    emit(lines, f"Baseline reference dec%={BASE_DEC_PCT:g}; qty={BASKET_QTY}")
    emit(lines, "")
    emit(lines, "Qty sequences (max_adjustments=2):")
    for pct in DEC_PCTS:
        seq = qty_sequence(BASKET_QTY, pct, max_adj=2)
        emit(lines, f"  dec%={pct:>4.0f} → {seq}")
    emit(lines, "")
    emit(
        lines,
        f"{'dec%':>6} {'n':>5} {'mean/day':>10} {'ci_lo':>9} {'worst':>9} "
        f"{'mdd':>9} {'fees/day':>9} {'qty_seq':>14}",
    )
    emit(lines, "-" * 80)

    results: dict[float, dict[str, float]] = {}
    mean20: float | None = None
    for i, pct in enumerate(DEC_PCTS):
        logger.info("PART A dec%%=%s", pct)
        rows: list[SimRow] = []
        for j, o in enumerate(base):
            if (j + 1) % 100 == 0:
                logger.info("  A %s %s/%s", pct, j + 1, len(base))
            rows.append(
                simulate_one(o, idx, times, closes, decrease_pct=pct)
            )
        sm = summarize(rows, day_span, d0, d1, seed=BOOTSTRAP_SEED + 10 + i)
        results[pct] = sm
        if abs(pct - 20.0) < 1e-9:
            mean20 = sm["mean_day"]
        seq = qty_sequence(BASKET_QTY, pct, max_adj=2)
        emit(
            lines,
            f"{pct:6.0f} {int(sm['n']):5d} {sm['mean_day']:10.4f} {sm['ci_lo']:9.4f} "
            f"{sm['worst']:9.4f} {sm['mdd']:9.4f} {sm['fees_per_day']:9.4f} "
            f"{str(seq):>14}",
        )

    better_than_20 = []
    if mean20 is not None:
        for pct in (30.0, 35.0, 40.0):
            if results[pct]["mean_day"] > mean20:
                better_than_20.append(pct)
    emit(lines, "")
    if set(better_than_20) >= {30.0, 40.0}:
        verdict = (
            "PLATEAU: 30 and 40 both beat 20 on mean/day "
            "(not only 35 — not a pure rounding artifact)."
        )
    elif better_than_20 == [35.0]:
        verdict = (
            "ROUNDING ARTIFACT LIKELY: only 35 beats 20; 30/40 do not — "
            "check qty sequence floors."
        )
    elif 35.0 in better_than_20:
        verdict = (
            f"PARTIAL: better-than-20 among {{30,35,40}} = {better_than_20} "
            "(35 not unique; inspect sequences)."
        )
    else:
        verdict = (
            f"NO CLEAR 35-ONLY EDGE vs 20 among {{30,35,40}}: {better_than_20}"
        )
    emit(lines, f"VERDICT: {verdict}")
    emit(lines, "")
    return results


def run_part_b(lines: list[str]) -> None:
    emit(lines, "===== PART B: 50% RETRACEMENT CLAIM (4h) =====")
    emit(
        lines,
        f"Pivot lookback={RETRACE_LOOKBACK} both sides; confirm at pivot+lb "
        "(no look-ahead).",
    )
    emit(lines, "Null = random 4h bars with prior opposite swing (not swing pivots).")
    emit(lines, "")

    bars_1m = load_1m_ohlc()
    bars4 = aggregate_4h(bars_1m)
    sh, sl = find_swings_4h(bars4, RETRACE_LOOKBACK)
    swing_ev = build_swing_events(bars4, sh, sl)
    rand_ev = build_random_events(
        bars4, sh, sl, n_events=len(swing_ev), seed=BOOTSTRAP_SEED + 77
    )
    emit(
        lines,
        f"4h bars={len(bars4)}  swing_highs={len(sh)}  swing_lows={len(sl)}  "
        f"swing_events={len(swing_ev)}  random_events={len(rand_ev)}",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'N':>4} {'grp':>6} {'n':>6} {'hit%':>8} {'avg_retr%':>10} "
        f"{'med_bars50':>11}",
    )
    emit(lines, "-" * 55)

    for n_fwd in RETRACE_NS:
        sw = retrace_stats(bars4, swing_ev, n_fwd)
        rn = retrace_stats(bars4, rand_ev, n_fwd)
        emit(
            lines,
            f"{n_fwd:4d} {'swing':>6} {int(sw['n']):6d} {sw['hit_pct']:8.1f} "
            f"{sw['avg_retrace_pct']:10.1f} {sw['med_bars_to_50']:11.1f}",
        )
        emit(
            lines,
            f"{n_fwd:4d} {'random':>6} {int(rn['n']):6d} {rn['hit_pct']:8.1f} "
            f"{rn['avg_retrace_pct']:10.1f} {rn['med_bars_to_50']:11.1f}",
        )
        diff = sw["hit_pct"] - rn["hit_pct"]
        emit(lines, f"     delta_hit_pp={diff:+.1f}")

    # Summary judgment on N=12 (2 days) as representative
    sw12 = retrace_stats(bars4, swing_ev, 12)
    rn12 = retrace_stats(bars4, rand_ev, 12)
    emit(lines, "")
    if abs(sw12["hit_pct"] - rn12["hit_pct"]) < 5.0:
        emit(
            lines,
            "NULL COMPARISON (N=12): swing ≈ random (|Δhit|<5pp) → "
            "50% retrace looks like generic BTC behaviour, not a swing property.",
        )
    else:
        emit(
            lines,
            f"NULL COMPARISON (N=12): swing hit%={sw12['hit_pct']:.1f} vs "
            f"random={rn12['hit_pct']:.1f} (Δ={sw12['hit_pct']-rn12['hit_pct']:+.1f}pp).",
        )
    emit(lines, "")


def run_part_c(
    lines: list[str],
    base: list[eng.CycleObs],
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    surface: Any,
    day_span: int,
    d0: date,
    d1: date,
) -> None:
    emit(lines, "===== PART C: ULP RANGE vs MATCHED CONTROL =====")
    emit(
        lines,
        f"ULP tf={ULP_TF} lookback={ULP_LOOKBACK} shrink={ULP_SHRINK} "
        f"dec%={BASE_DEC_PCT:g} hedge=OFF",
    )
    emit(lines, "")

    logger.info("Building ULP snapshots on %s...", ULP_TF)
    ulp_path = resolve_tf_csv(ULP_TF)
    ulp_bars = uz.load_bars(ulp_path)
    bar_times, tops_series, bots_series = build_ulp_snapshot(
        ulp_bars,
        lookback=ULP_LOOKBACK,
        shrink_during_confirmation=ULP_SHRINK,
    )
    logger.info("ULP bars=%s", len(ulp_bars))

    table_rows: list[dict[str, Any]] = []
    ulp_vs_ctrl: list[tuple[float, float, dict[date, float], dict[date, float]]] = []

    def add_table(
        method: str,
        width_label: str,
        rows: list[SimRow],
        seed: int,
    ) -> dict[str, float]:
        sm = summarize(rows, day_span, d0, d1, seed=seed)
        table_rows.append(
            {
                "method": method,
                "width": width_label,
                "mean_day": sm["mean_day"],
                "ci_lo": sm["ci_lo"],
                "worst": sm["worst"],
                "break_pct": sm["break_pct"],
                "n": sm["n"],
                "cpd": sm["cpd"],
                "mdd": sm["mdd"],
                "avg_width": sm["avg_width"],
            }
        )
        return sm

    # (1) PREMIUM / B25
    logger.info("PART C PREMIUM...")
    prem_rows: list[SimRow] = []
    for j, o in enumerate(base):
        if (j + 1) % 100 == 0:
            logger.info("  PREMIUM %s/%s", j + 1, len(base))
        prem_rows.append(
            simulate_one(o, idx, times, closes, decrease_pct=BASE_DEC_PCT)
        )
    add_table("PREMIUM", "B25", prem_rows, BOOTSTRAP_SEED + 200)

    # (2) FIXED_DISTANCE
    for di, dist in enumerate(FIXED_DISTS):
        logger.info("PART C FIXED_DISTANCE=%s", dist)
        rows: list[SimRow] = []
        for j, o in enumerate(base):
            strikes = idx.strikes_by_expiry.get(o.basket_expiry) or set()
            pair = snap_otm_pair(
                strikes,
                float(o.spot_entry),
                float(o.spot_entry) + dist,
                float(o.spot_entry) - dist,
            )
            if pair is None:
                continue
            sc_k, sp_k = pair
            co = build_cycle_strikes(
                idx,
                times,
                closes,
                surface,
                day=o.entry_date,
                expiry=o.basket_expiry,
                entry_utc=o.entry_utc,
                spot_e=float(o.spot_entry),
                sc_k=sc_k,
                sp_k=sp_k,
                strike_mode=f"FIXED_{int(dist)}",
            )
            if co is None:
                continue
            rows.append(
                simulate_one(co, idx, times, closes, decrease_pct=BASE_DEC_PCT)
            )
        add_table(
            "FIXED_DISTANCE",
            f"{int(dist)}",
            rows,
            BOOTSTRAP_SEED + 300 + di,
        )

    # (3) ULP_RANGE + matched control
    for xi, x_out in enumerate(ULP_X):
        logger.info("PART C ULP_RANGE X=%s", x_out)
        ulp_rows: list[SimRow] = []
        ctrl_rows: list[SimRow] = []
        ulp_by: dict[date, float] = {}
        ctrl_by: dict[date, float] = {}
        for j, o in enumerate(base):
            if (j + 1) % 100 == 0:
                logger.info("  ULP X=%s %s/%s", x_out, j + 1, len(base))
            ts = int(o.entry_utc.timestamp())
            spot = float(o.spot_entry)
            rng = ulp_range_at(
                ts, spot, bar_times, tops_series, bots_series, x_out
            )
            if rng is None:
                continue
            call_tgt, put_tgt = rng
            strikes = idx.strikes_by_expiry.get(o.basket_expiry) or set()
            pair = snap_otm_pair(strikes, spot, call_tgt, put_tgt)
            if pair is None:
                continue
            sc_k, sp_k = pair
            width = sc_k - sp_k
            if width <= 0:
                continue
            co_ulp = build_cycle_strikes(
                idx,
                times,
                closes,
                surface,
                day=o.entry_date,
                expiry=o.basket_expiry,
                entry_utc=o.entry_utc,
                spot_e=spot,
                sc_k=sc_k,
                sp_k=sp_k,
                strike_mode=f"ULP_X{int(x_out)}",
            )
            if co_ulp is None:
                continue
            # Matched control: same width, symmetric about spot
            half = width / 2.0
            pair_c = snap_otm_pair(
                strikes, spot, spot + half, spot - half
            )
            if pair_c is None:
                continue
            csc, csp = pair_c
            co_ctrl = build_cycle_strikes(
                idx,
                times,
                closes,
                surface,
                day=o.entry_date,
                expiry=o.basket_expiry,
                entry_utc=o.entry_utc,
                spot_e=spot,
                sc_k=csc,
                sp_k=csp,
                strike_mode=f"CTRL_X{int(x_out)}",
            )
            if co_ctrl is None:
                continue
            ru = simulate_one(
                co_ulp, idx, times, closes, decrease_pct=BASE_DEC_PCT
            )
            rc = simulate_one(
                co_ctrl, idx, times, closes, decrease_pct=BASE_DEC_PCT
            )
            if not (math.isfinite(ru.net) and math.isfinite(rc.net)):
                continue
            ulp_rows.append(ru)
            ctrl_rows.append(rc)
            ulp_by[o.entry_date] = ulp_by.get(o.entry_date, 0.0) + ru.net
            ctrl_by[o.entry_date] = ctrl_by.get(o.entry_date, 0.0) + rc.net

        sm_u = add_table(
            "ULP_RANGE",
            f"X={int(x_out)}",
            ulp_rows,
            BOOTSTRAP_SEED + 400 + xi,
        )
        sm_c = add_table(
            "MATCHED_CTRL",
            f"X={int(x_out)}",
            ctrl_rows,
            BOOTSTRAP_SEED + 450 + xi,
        )
        z = paired_z(ulp_by, ctrl_by)
        ulp_vs_ctrl.append((x_out, z, ulp_by, ctrl_by))
        _ = sm_u, sm_c

    emit(
        lines,
        f"{'method':<14} {'width':>8} {'n':>5} {'mean/day':>10} {'ci_lo':>9} "
        f"{'worst':>9} {'break%':>8} {'avg_w':>8} {'cpd':>7} {'mdd':>9}",
    )
    emit(lines, "-" * 100)
    for r in table_rows:
        emit(
            lines,
            f"{r['method']:<14} {r['width']:>8} {int(r['n']):5d} "
            f"{r['mean_day']:10.4f} {r['ci_lo']:9.4f} {r['worst']:9.4f} "
            f"{r['break_pct']:8.1f} {r['avg_width']:8.1f} {r['cpd']:7.3f} "
            f"{r['mdd']:9.4f}",
        )

    emit(lines, "")
    emit(lines, "----- ULP vs matched-control (paired daily z-score) -----")
    emit(
        lines,
        f"{'X':>6} {'z':>8} {'ulp_mean/day':>14} {'ctrl_mean/day':>14} "
        f"{'paired_days':>12}",
    )
    for x_out, z, ub, cb in ulp_vs_ctrl:
        common = set(ub) & set(cb)
        u_daily = [ub[d] for d in sorted(common)]
        c_daily = [cb[d] for d in sorted(common)]
        um = statistics.mean(u_daily) if u_daily else float("nan")
        cm = statistics.mean(c_daily) if c_daily else float("nan")
        emit(
            lines,
            f"{int(x_out):6d} {z:8.3f} {um:14.4f} {cm:14.4f} {len(common):12d}",
        )
    emit(lines, "")
    emit(
        lines,
        "If |z| small and ULP≈control mean/day → zones add no info beyond width.",
    )
    emit(lines, "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 RANGE SELECTION — dec% plateau + 50% retrace null + ULP vs control")
    emit(lines, "=" * 100)
    emit(
        lines,
        "BASELINE: dte2 B_only trig70 maker B25 wings2000 qty=8 entry=11:00 "
        f"HEDGE=OFF dec%={BASE_DEC_PCT:g}",
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

    run_part_a(lines, base, idx, times, closes, day_span, d0, d1)
    run_part_b(lines)
    run_part_c(lines, base, idx, times, closes, surface, day_span, d0, d1)

    emit(lines, "DONE.")
    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
