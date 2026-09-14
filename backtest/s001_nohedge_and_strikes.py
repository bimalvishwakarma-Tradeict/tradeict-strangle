#!/usr/bin/env python3
"""
S001 missing-day audit + qty strike diagnostic + no-hedge variant.

PART A: why only ~300/456 days have WINNER basket cycles
PART B: strike/premium scaling across qty ratios + live premium-target confirm
PART C: standalone basket (no hedge cost) vs with-hedge (0.6255/day)
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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

logger = logging.getLogger("s001_nohedge_and_strikes")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_nohedge_and_strikes.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
HEDGE_COST_PER_DAY = 0.6255
HEDGE_QTY = 4
QTY_PCTS = (200, 250, 300, 350, 400)
BASKET_QTY_LIVE = 8
WING_POINTS = 2000.0
ENTRY_HH, ENTRY_MM = 11, 0
CRASH_DAY = date(2026, 6, 2)
CRASH_DAY_ALT = date(2025, 11, 19)
CRASH_MONTH_START = date(2026, 1, 27)
CRASH_MONTH_END = date(2026, 2, 24)  # inclusive of hedge cycle window end
HEDGE_DEBIT_PROXY = 32.0
SHORT_MARGIN_MULT = 5.0
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def diagnose_day(
    day: date,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    surface: object | None,
) -> str:
    """
    First-failure reason for WINNER filter on this calendar day.
    Mirrors s001_income_engine.measure_cycles for:
      11:00 IST, dte=2, maker, B25, wing=2000.
    """
    entry_utc = eng.ist_to_utc(day, ENTRY_HH, ENTRY_MM)
    ts = int(entry_utc.timestamp())
    spot_lo, spot_hi = times[0], times[-1]
    if ts < spot_lo or ts > spot_hi:
        return "entry_outside_spot_range (candle/data edge)"
    spot_e = ot.spot_at(times, closes, ts)
    if spot_e is None or spot_e <= 0:
        return "no_spot_at_11:00_IST (candle/data gap)"

    exp = day + timedelta(days=2)
    if exp not in idx.expiries:
        return "2DTE_expiry_does_not_exist_that_day"

    spot_s = eng.settle_spot_1200_utc(times, closes, exp)
    if spot_s is None or spot_s <= 0:
        return "no_settle_spot_at_12:00_UTC (candle/data gap)"

    settle_ts = int(
        datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp()
    )
    if ts >= settle_ts:
        return "entry_after_settlement"

    short_role, long_role = eng.roles_for_package("maker")
    atm = eng.pick_atm_straddle(idx, exp, float(spot_e), entry_utc, long_role)
    if atm is None:
        atm = eng.pick_atm_straddle(idx, exp, float(spot_e), entry_utc, short_role)
    if atm is None:
        return "no_ATM_trade_prints_at_11:00_IST"
    _atm_k, ac, ap = atm
    atm_prem = ac.price + ap.price
    if atm_prem <= 0:
        return "ATM_straddle_premium_non_positive"
    target = 25.0 / 100.0 * atm_prem
    if target < 5.0:
        return "B25_target_premium_too_small"

    strangle = eng.pick_strangle_by_premium(
        idx, exp, float(spot_e), target, entry_utc, short_role
    )
    if strangle is None:
        return "no_strike_near_B25_target_premium"
    sc_k, sp_k, sc, sp = strangle
    if sc.price <= 0 or sp.price <= 0:
        return "short_leg_print_price_non_positive"

    wk = eng.pick_wing_strikes(idx, exp, sc_k, sp_k, WING_POINTS)
    if wk is None:
        return "no_wing_strikes_2000_points_away"
    wing_c_k, wing_p_k = wk
    wing_c = eng.wing_fill_or_surface(
        idx, surface, exp, wing_c_k, "C", entry_utc, long_role
    )
    wing_p = eng.wing_fill_or_surface(
        idx, surface, exp, wing_p_k, "P", entry_utc, long_role
    )
    if wing_c is None or wing_p is None:
        return "no_wing_price (print+surface both failed)"

    return "OK"


def cluster_ranges(days: list[date]) -> list[tuple[date, date, int]]:
    if not days:
        return []
    days = sorted(days)
    ranges: list[tuple[date, date, int]] = []
    start = prev = days[0]
    for d in days[1:]:
        if (d - prev).days == 1:
            prev = d
            continue
        ranges.append((start, prev, (prev - start).days + 1))
        start = prev = d
    ranges.append((start, prev, (prev - start).days + 1))
    return ranges


def approx_margin_no_hedge(o: eng.CycleObs, qty: int) -> float:
    sc = float(o.short_call.price)
    sp = float(o.short_put.price)
    short_credit = (sc + sp) * qty * eng.CONTRACT_VALUE
    wing_debit = 0.0
    if o.wing_call is not None and o.wing_put is not None:
        wing_debit = (
            float(o.wing_call.price) + float(o.wing_put.price)
        ) * qty * eng.CONTRACT_VALUE
    return SHORT_MARGIN_MULT * short_credit + wing_debit


def approx_margin_with_hedge(o: eng.CycleObs, qty: int) -> float:
    return approx_margin_no_hedge(o, qty) + HEDGE_DEBIT_PROXY


def decile_lines(moves: list[float], pnls: list[float]) -> list[str]:
    pairs = sorted(zip(moves, pnls), key=lambda x: x[0])
    n = len(pairs)
    out = [
        f"  {'dec':>4}  {'|move|_lo':>10}  {'|move|_hi':>10}  {'n':>5}  {'mean_pnl':>10}"
    ]
    if n < 10:
        out.append("  NOT AVAILABLE — need >=10 cycles")
        return out
    for d in range(10):
        lo = int(d * n / 10)
        hi = int((d + 1) * n / 10)
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        out.append(
            f"  D{d:<3}  {chunk[0][0]:10.1f}  {chunk[-1][0]:10.1f}  "
            f"{len(chunk):5d}  {statistics.mean([p for _, p in chunk]):10.4f}"
        )
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 MISSING-DAY AUDIT + QTY STRIKES + NO-HEDGE VARIANT")
    emit(lines, "=" * 100)
    emit(lines, "")

    logger.info("Loading cycle cache...")
    all_obs, day_span = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)  # maker B25 wing2000 11:00 dte2
    have_days = {o.entry_date for o in base}
    logger.info("Winner-filter cycles: %s unique days=%s", len(base), len(have_days))

    logger.info("Building trade index + spot + surface...")
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    surface = eng.load_surface_optional()
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()
    cal_inclusive = (d1 - d0).days + 1
    # Match prior reports: day_span = (t_last - t_first) // 86400
    cal_days = int(day_span)
    emit(lines, "===== PART A: MISSING DAYS =====")
    emit(
        lines,
        f"Official denominator day_span={cal_days} (cache meta). "
        f"Inclusive calendar dates {d0}→{d1} = {cal_inclusive} "
        f"(+{cal_inclusive - cal_days} edge-day difference vs day_span).",
    )
    emit(
        lines,
        f"winner-filter days with cycle={len(have_days)}  "
        f"missing vs day_span={cal_days - len(have_days)}  "
        f"missing vs inclusive={cal_inclusive - len(have_days)}",
    )
    emit(
        lines,
        "Note: income engine does NOT gate on live hedge activity — "
        "'hedge inactive' is NOT a backtest drop reason for these cycles.",
    )
    emit(lines, f"IV surface loaded for wing fallback: {surface is not None}")
    emit(lines, "")

    reason_by_day: dict[date, str] = {}
    day = d0
    n_checked = 0
    while day <= d1:
        if day in have_days:
            reason_by_day[day] = "HAS_CYCLE"
        else:
            reason = diagnose_day(day, idx, times, closes, surface)
            if reason == "OK":
                reason = (
                    "diagnose_OK_but_absent_from_cache "
                    "(cache/measure mismatch — FIXABLE)"
                )
            reason_by_day[day] = reason
        n_checked += 1
        if n_checked % 60 == 0:
            logger.info("  diagnosed through %s", day)
        day += timedelta(days=1)

    counts = Counter(reason_by_day.values())
    n_has = counts.pop("HAS_CYCLE", 0)
    missing_inclusive = cal_inclusive - n_has
    missing_vs_span = cal_days - n_has
    emit(lines, f"Days WITH winner cycle: {n_has}")
    emit(
        lines,
        f"Days MISSING: {missing_vs_span} (vs day_span={cal_days}) / "
        f"{missing_inclusive} (vs inclusive={cal_inclusive})",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'reason':<70} {'count':>6} {'% of 456':>8} {'% of incl':>9}",
    )
    emit(lines, "-" * 100)
    emit(
        lines,
        f"{'HAS_CYCLE (winner filter present)':<70} {n_has:6d} "
        f"{100.0 * n_has / cal_days:7.1f}% {100.0 * n_has / cal_inclusive:8.1f}%",
    )
    for reason, cnt in sorted(
        ((r, c) for r, c in counts.items()), key=lambda x: -x[1]
    ):
        emit(
            lines,
            f"{reason:<70} {cnt:6d} {100.0 * cnt / cal_days:7.1f}% "
            f"{100.0 * cnt / cal_inclusive:8.1f}%",
        )
    emit(lines, "")

    # Fixable vs genuine
    FIXABLE_PREFIXES = (
        "no_spot_at_11:00",
        "entry_outside_spot",
        "no_ATM_trade_prints",
        "no_strike_near_B25",
        "no_wing_price",
        "diagnose_OK_but_absent",
        "no_settle_spot_at_12:00",
    )
    GENUINE_PREFIXES = (
        "2DTE_expiry_does_not_exist",
        "entry_after_settlement",
        "ATM_straddle_premium_non",
        "B25_target_premium_too_small",
        "no_wing_strikes_2000",
        "short_leg_print_price",
    )
    fixable_n = genuine_n = other_n = 0
    for reason, cnt in counts.items():
        if any(reason.startswith(p) for p in FIXABLE_PREFIXES):
            fixable_n += cnt
        elif any(reason.startswith(p) for p in GENUINE_PREFIXES):
            genuine_n += cnt
        else:
            other_n += cnt
    emit(lines, "Fixable vs genuine (classification):")
    emit(
        lines,
        f"  FIXABLE (backtest data/print gap; live bot might still trade): "
        f"{fixable_n}",
    )
    emit(
        lines,
        "    includes: no spot/ATM/strangle/wing PRINTS at 11:00, cache mismatch",
    )
    emit(
        lines,
        f"  GENUINE (structure/expiry/settlement impossible that day): {genuine_n}",
    )
    emit(
        lines,
        "    includes: no 2DTE expiry in index, no settle spot, entry after settle, "
        "no wing strikes 2000pts away",
    )
    emit(lines, f"  OTHER / uncategorized: {other_n}")
    emit(
        lines,
        "  NOTE: 'hedge inactive' does not apply — s001_income_engine never "
        "requires an active hedge to form B25 cycles (uses ATM straddle proxy).",
    )
    emit(lines, "")

    missing_days = sorted(d for d, r in reason_by_day.items() if r != "HAS_CYCLE")
    ranges = cluster_ranges(missing_days)
    isolated = sum(1 for _a, _b, n in ranges if n == 1)
    clustered = [r for r in ranges if r[2] >= 3]
    emit(lines, "Missing-day geography:")
    emit(
        lines,
        f"  contiguous ranges: {len(ranges)}  "
        f"isolated single days: {isolated}  "
        f"clusters (>=3 consecutive): {len(clustered)}",
    )
    if clustered:
        emit(lines, "  Largest clusters:")
        for a, b, n in sorted(clustered, key=lambda x: -x[2])[:15]:
            emit(lines, f"    {a} → {b}  ({n} days)")
    else:
        emit(lines, "  No clusters of >=3 consecutive missing days.")
    emit(lines, "")

    # ----- PART B -----
    emit(lines, "===== PART B: QTY SCALING STRIKE DIAGNOSTIC =====")
    emit(
        lines,
        "Strikes come from CycleObs (B25 selection) — same strikes for all qty%; "
        "qty only scales lots. Distances/premiums averaged over n=300 winner days.",
    )
    emit(lines, "")

    # Live code confirm
    emit(lines, "CODE CONFIRM — strangle_premium_pct_of_hedge target:")
    emit(
        lines,
        "  File: backend/engine/auto_trade_engine.py",
    )
    emit(
        lines,
        "  Function: resolve_strangle_target_premium  lines 279–356",
    )
    emit(
        lines,
        "  Formula (lines 334–336):",
    )
    emit(
        lines,
        "    avg = (hedge_call_mark + hedge_put_mark) / 2.0",
    )
    emit(
        lines,
        "    computed = ceil(avg × pct / 100.0)",
    )
    emit(
        lines,
        "  Returns: target_premium_PER_SIDE (docstring line 286–291, return line 356).",
    )
    emit(
        lines,
        "  Verdict: PER SIDE premium level for strike matching — NOT total USD "
        "across lots. Qty is applied later; this sets the $/BTC target per short leg.",
    )
    emit(
        lines,
        "  Backtest B25 note: income engine uses 25% of ATM straddle (call+put sum), "
        "which is a PROXY — not identical to live avg(hedge marks)×25%.",
    )
    emit(lines, "")

    emit(
        lines,
        f"{'qty%':>5} {'qty':>4} {'call_dist':>10} {'put_dist':>10} "
        f"{'c_prem/lot':>10} {'p_prem/lot':>10} {'tot_prem':>10} "
        f"{'hedge_px':>10} {'ratio':>8}",
    )
    emit(lines, "-" * 100)

    # Hedge premium proxy at entry: ATM straddle on month_1 is heavy;
    # use CycleObs.atm_straddle_prem as available proxy labeled clearly,
    # OR reconstruct hedge ATM if we can quickly.
    # User asked hedge premium at that time — best available on CycleObs is
    # atm_straddle_prem (daily ATM), not monthly hedge. For monthly hedge
    # we'd need reconstruct. Use atm as NOT the live hedge; try month_1 ATM
    # from index when possible.

    from s001_hedge_integration import (  # noqa: E402
        fill_pair,
        pick_atm,
        resolve_month_1,
        MIN_HEDGE_DTE_LIVE,
        HEDGE_ENTRY_WINDOW_SEC,
    )

    for pct in QTY_PCTS:
        qty = int(math.ceil(HEDGE_QTY * pct / 100.0))
        call_dists: list[float] = []
        put_dists: list[float] = []
        c_prems: list[float] = []
        p_prems: list[float] = []
        tot_prems: list[float] = []
        hedge_prems: list[float] = []
        ratios: list[float] = []
        for o in base:
            spot = float(o.spot_entry)
            call_dists.append(float(o.short_call_k) - spot)
            put_dists.append(spot - float(o.short_put_k))
            c_prems.append(float(o.short_call.price))
            p_prems.append(float(o.short_put.price))
            tot = (
                float(o.short_call.price) + float(o.short_put.price)
            ) * qty * eng.CONTRACT_VALUE
            tot_prems.append(tot)
            # Monthly hedge ATM long debit at entry if prints exist
            exp_h = resolve_month_1(
                o.entry_date, idx.expiries, min_hedge_dte=MIN_HEDGE_DTE_LIVE
            )
            h_prem = float("nan")
            if exp_h is not None:
                strikes = idx.strikes_by_expiry.get(exp_h) or set()
                atm = pick_atm(strikes, spot)
                if atm is not None:
                    fills = fill_pair(
                        idx, exp_h, atm, o.entry_utc, HEDGE_ENTRY_WINDOW_SEC
                    )
                    if fills is not None:
                        h_prem = (
                            fills[0].price + fills[1].price
                        ) * HEDGE_QTY * eng.CONTRACT_VALUE
            hedge_prems.append(h_prem)
            if math.isfinite(h_prem) and h_prem > 1e-9:
                ratios.append(tot / h_prem)

        def avg(xs: list[float]) -> float:
            ok = [x for x in xs if math.isfinite(x)]
            return statistics.mean(ok) if ok else float("nan")

        emit(
            lines,
            f"{pct:5d} {qty:4d} {avg(call_dists):10.1f} {avg(put_dists):10.1f} "
            f"{avg(c_prems):10.2f} {avg(p_prems):10.2f} {avg(tot_prems):10.4f} "
            f"{avg(hedge_prems):10.4f} {avg(ratios):8.3f}",
        )
    emit(
        lines,
        "hedge_px = 4-lot ATM monthly (month_1, min_dte=6) long-straddle debit "
        "from real prints at entry when available; else excluded from avg.",
    )
    emit(
        lines,
        "ratio = total_basket_premium_collected_at_entry / hedge_premium. "
        "Strikes identical across qty% (only lots change).",
    )
    emit(lines, "")

    # ----- PART C -----
    emit(lines, "===== PART C: NO-HEDGE VARIANT =====")
    emit(
        lines,
        "Config: dte2 B_only trig70 maker B25 wings2000 qty=8 entry11:00 — "
        "NO hedge cost in standalone metrics.",
    )
    emit(lines, "")

    logger.info("Simulating WINNER basket (8 lots) for PART C...")
    nets: list[float] = []
    moves: list[float] = []
    dates: list[date] = []
    margins_nh: list[float] = []
    margins_wh: list[float] = []
    by_date: dict[date, float] = {}
    for i, o in enumerate(base):
        if (i + 1) % 50 == 0:
            logger.info("  sim %s/%s", i + 1, len(base))
        r = sweep.simulate_with_adjustments(
            o, WINNER_CFG, idx, times, closes, basket_qty=BASKET_QTY_LIVE
        )
        nets.append(r.net)
        moves.append(float(o.spot_move_abs))
        dates.append(o.entry_date)
        by_date[o.entry_date] = r.net
        margins_nh.append(approx_margin_no_hedge(o, BASKET_QTY_LIVE))
        margins_wh.append(approx_margin_with_hedge(o, BASKET_QTY_LIVE))

    mean, ci_lo, ci_hi = eng.bootstrap_mean_ci(nets, BOOTSTRAP_N, BOOTSTRAP_SEED)
    chron = [n for _, n in sorted(zip(dates, nets), key=lambda x: x[0])]
    mdd = eng.max_drawdown(chron)
    cpd = len(nets) / float(max(1, day_span))
    margin_nh = statistics.mean(margins_nh)
    margin_wh = statistics.mean(margins_wh)

    mean_day_nh = mean * cpd
    mean_day_wh = mean * cpd - HEDGE_COST_PER_DAY
    ci_lo_day_nh = ci_lo * cpd
    ci_lo_day_wh = ci_lo * cpd - HEDGE_COST_PER_DAY
    rom_nh = (mean_day_nh * 365.25 / margin_nh) if margin_nh > 1e-9 else float("nan")
    rom_wh = (mean_day_wh * 365.25 / margin_wh) if margin_wh > 1e-9 else float("nan")

    emit(lines, "Standalone basket (WITHOUT hedge cost):")
    emit(lines, f"  n={len(nets)}")
    emit(
        lines,
        f"  mean/cycle={mean:.4f}  median={statistics.median(nets):.4f}  "
        f"std={statistics.stdev(nets):.4f}",
    )
    emit(lines, f"  ci_lo={ci_lo:.4f}  ci_hi={ci_hi:.4f}")
    emit(
        lines,
        f"  worst={min(nets):.4f}  p1={eng.pctile(nets, 1):.4f}  "
        f"p5={eng.pctile(nets, 5):.4f}  p10={eng.pctile(nets, 10):.4f}",
    )
    emit(lines, f"  max_drawdown_running_sum={mdd:.4f}")
    emit(
        lines,
        f"  margin_APPROX (shorts+wings only)={margin_nh:.2f}  "
        f"RoM_ann={rom_nh:.4f}",
    )
    emit(lines, "")
    emit(lines, "|move| deciles (WITHOUT hedge — basket P&L only):")
    for ln in decile_lines(moves, nets):
        emit(lines, ln)
    emit(lines, "")

    def day_pnl(d: date) -> str:
        if d not in by_date:
            return "NOT AVAILABLE (no cycle that entry_date)"
        return f"{by_date[d]:.4f}"

    emit(lines, f"Crash day {CRASH_DAY}: P&L = {day_pnl(CRASH_DAY)}")
    emit(lines, f"Crash day {CRASH_DAY_ALT}: P&L = {day_pnl(CRASH_DAY_ALT)}")
    crash_month_nets = [
        by_date[d]
        for d in sorted(by_date)
        if CRASH_MONTH_START <= d <= CRASH_MONTH_END
    ]
    crash_month_total = sum(crash_month_nets) if crash_month_nets else float("nan")
    emit(
        lines,
        f"Crash-month window {CRASH_MONTH_START} → {CRASH_MONTH_END}: "
        f"n_cycles={len(crash_month_nets)}  "
        f"total_pnl={crash_month_total if crash_month_nets else 'NOT AVAILABLE'}",
    )
    emit(lines, "")

    # WITH hedge crash-month: basket total - 0.6255 * days in window with active...
    # User asked crash-month total for no-hedge; for side-by-side use:
    # with hedge: basket sum - 0.6255 * n_calendar_days in window (deterministic)
    n_crash_days = (CRASH_MONTH_END - CRASH_MONTH_START).days + 1
    crash_month_with = (
        crash_month_total - HEDGE_COST_PER_DAY * n_crash_days
        if crash_month_nets
        else float("nan")
    )

    emit(lines, "===== FINAL SIDE-BY-SIDE =====")
    emit(
        lines,
        f"{'metric':<28} {'WITH hedge (200%)':>20} {'WITHOUT hedge':>20}",
    )
    emit(lines, "-" * 72)
    emit(
        lines,
        f"{'mean/day':<28} {mean_day_wh:20.4f} {mean_day_nh:20.4f}",
    )
    emit(
        lines,
        f"{'ci_lo (of mean/day)':<28} {ci_lo_day_wh:20.4f} {ci_lo_day_nh:20.4f}",
    )
    emit(
        lines,
        f"{'worst cycle':<28} {min(nets):20.4f} {min(nets):20.4f}",
    )
    emit(
        lines,
        f"{'max drawdown':<28} {mdd:20.4f} {mdd:20.4f}",
    )
    emit(
        lines,
        f"{'margin APPROX':<28} {margin_wh:20.2f} {margin_nh:20.2f}",
    )
    emit(
        lines,
        f"{'return on margin ann':<28} {rom_wh:20.4f} {rom_nh:20.4f}",
    )
    emit(
        lines,
        f"{'crash-month total':<28} {crash_month_with:20.4f} {crash_month_total:20.4f}",
    )
    emit(lines, "")
    emit(
        lines,
        f"WITH hedge uses −{HEDGE_COST_PER_DAY}/day on mean/day and "
        f"−{HEDGE_COST_PER_DAY}×{n_crash_days} on crash-month "
        f"({CRASH_MONTH_START}..{CRASH_MONTH_END}).",
    )
    emit(
        lines,
        "worst cycle / max drawdown identical: hedge cost is a flat daily "
        "constant, not path-dependent in this column.",
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
