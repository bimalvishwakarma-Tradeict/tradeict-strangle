#!/usr/bin/env python3
"""
S001 qty scaling anomaly diagnostic + fair 3-way hedge comparison
+ hedge size/roll bleed sweep.

DIAGNOSTIC ONLY — no strategy / engine fixes.
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from datetime import date, timedelta, timezone
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
import s001_hedge_integration as hedge  # noqa: E402
import s001_income_engine as eng  # noqa: E402
import s001_winner_forensic as forensic  # noqa: E402
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402

logger = logging.getLogger("s001_hedge_fair_and_qty_check")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_hedge_fair_and_qty_check.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
HEDGE_COST_MEDIAN = 0.6255
HEDGE_QTY_LIVE = 4
QTY_200 = 8
QTY_250 = 10
BASKET_MEAN_DAY_NO_HEDGE = 0.5905  # from prior no-hedge run (fixed for Part C)
CRASH_MONTH_START = date(2026, 1, 27)
CRASH_MONTH_END = date(2026, 2, 24)
CRASH_DAY = date(2026, 6, 2)
HEDGE_DEBIT_PROXY = 32.0
SHORT_MARGIN_MULT = 5.0
DECREASE_PCT = sweep.ADJUSTMENT_QTY_DECREASE_PCT
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def na_f(v: float | None, nd: int = 4) -> str:
    if v is None or not math.isfinite(float(v)):
        return "NOT AVAILABLE"
    return f"{float(v):.{nd}f}"


def cash_impact(side: str, price: float, qty: int) -> float:
    """Signed cash for one ledger fill (fees excluded)."""
    notional = float(price) * abs(int(qty)) * eng.CONTRACT_VALUE
    s = side.upper()
    if s in {"SELL"}:
        return notional
    if s in {"BUY", "BUY_TO_CLOSE"}:
        return -notional
    if s in {"SELL_TO_CLOSE", "SELL_PARTIAL"}:
        return notional
    if s in {"CLOSE_ALL", "MARK", "NOT AVAILABLE"}:
        return 0.0
    return 0.0


def adj_qty_path(original_qty: int, n_adjustments: int) -> list[int]:
    path = [int(original_qty)]
    q = int(original_qty)
    for i in range(1, max(0, int(n_adjustments)) + 1):
        nq, close = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=i,
            decrease_pct=DECREASE_PCT,
        )
        if close or nq is None:
            break
        path.append(int(nq))
        q = int(nq)
        _ = q
    return path


def approx_margin(o: eng.CycleObs, basket_qty: int, *, with_hedge: bool) -> float:
    sc = float(o.short_call.price)
    sp = float(o.short_put.price)
    short_credit = (sc + sp) * basket_qty * eng.CONTRACT_VALUE
    wing_debit = 0.0
    if o.wing_call is not None and o.wing_put is not None:
        wing_debit = (
            float(o.wing_call.price) + float(o.wing_put.price)
        ) * basket_qty * eng.CONTRACT_VALUE
    m = SHORT_MARGIN_MULT * short_credit + wing_debit
    if with_hedge:
        m += HEDGE_DEBIT_PROXY
    return m


def scale_hedge_cycle(c: hedge.HedgeCycle, qty: int) -> hedge.HedgeCycle:
    """Bleed/PnL linear in hedge lots (same fills)."""
    factor = float(qty) / float(HEDGE_QTY_LIVE)
    if c.status != "OK":
        return c
    return hedge.HedgeCycle(
        status=c.status,
        entry_date=c.entry_date,
        exit_date=c.exit_date,
        expiry=c.expiry,
        entry_dte=c.entry_dte,
        atm=c.atm,
        entry_prem_usd=(
            None if c.entry_prem_usd is None else float(c.entry_prem_usd) * factor
        ),
        exit_prem_usd=(
            None if c.exit_prem_usd is None else float(c.exit_prem_usd) * factor
        ),
        days_held=c.days_held,
        realized_pnl_usd=(
            None
            if c.realized_pnl_usd is None
            else float(c.realized_pnl_usd) * factor
        ),
        bleed_per_day=(
            None if c.bleed_per_day is None else float(c.bleed_per_day) * factor
        ),
        index_entry=c.index_entry,
        index_exit=c.index_exit,
        index_move=c.index_move,
        reason=c.reason,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 QTY SCALING ANOMALY + FAIR HEDGE COMPARISON + BLEED SWEEP")
    emit(lines, "=" * 100)
    emit(lines, "DIAGNOSTIC ONLY — no fixes applied.")
    emit(lines, "")

    logger.info("Loading cycles + index...")
    all_obs, day_span = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)
    logger.info("winner cycles n=%s day_span=%s", len(base), day_span)
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    surface = eng.load_surface_optional()

    # =====================================================================
    # PART A
    # =====================================================================
    emit(lines, "===== PART A: QTY SCALING ANOMALY =====")
    emit(lines, "")
    emit(lines, "--- (a) Size-dependent fill model? ---")
    emit(
        lines,
        "NO SIZE-DEPENDENT FILL MODEL in the S001 adjustment backtest path.",
    )
    emit(
        lines,
        "Evidence (code, not guess):",
    )
    emit(
        lines,
        "  • Fill price = nearest trade print via eng.nearest_print_prefer —",
    )
    emit(
        lines,
        "    s001_adjustment_sweep.py premium_at() lines ~272–277;",
    )
    emit(
        lines,
        "    find_farther_otm_by_premium ~216–232; adj entry ~643–650.",
    )
    emit(
        lines,
        "  • Quantity is NEVER passed into the print/fill lookup — only symbol,",
    )
    emit(
        lines,
        "    timestamp window, and preferred role. Larger qty does not walk an",
    )
    emit(
        lines,
        "    order book or worsen the fill price.",
    )
    emit(
        lines,
        "  • Fees ARE qty-linear (both branches of min(...)):",
    )
    emit(
        lines,
        "    s001_income_engine.py option_fee() lines 69–78.",
    )
    emit(
        lines,
        "  • Cash P&L IS qty-linear: cash_pnl() lines 81–85.",
    )
    emit(
        lines,
        "NON-LINEARITY SOURCE (not fills): adjustment_qty_decrease_pct=20% via",
    )
    emit(
        lines,
        "  backend/engine/wing_entry.py compute_decrease_step_qty lines 332–353:",
    )
    emit(
        lines,
        "    new_qty = max(1, floor(original_qty × (1 − 0.20 × adj_n)))",
    )
    emit(
        lines,
        "  Called from s001_adjustment_sweep.py ~565–571.",
    )
    emit(
        lines,
        "  Example paths:",
    )
    emit(
        lines,
        f"    qty={QTY_200}: {adj_qty_path(QTY_200, 2)}  "
        f"(remaining ratios {[round(q / QTY_200, 3) for q in adj_qty_path(QTY_200, 2)]})",
    )
    emit(
        lines,
        f"    qty={QTY_250}: {adj_qty_path(QTY_250, 2)}  "
        f"(remaining ratios {[round(q / QTY_250, 3) for q in adj_qty_path(QTY_250, 2)]})",
    )
    emit(
        lines,
        "  Absolute lots dropped per adj step are BOTH 2 (8→6→4 and 10→8→6).",
    )
    emit(
        lines,
        "  That absolute (not proportional) step size is why P&L cannot scale",
    )
    emit(
        lines,
        "  1.25× even though entry lots do.",
    )
    emit(lines, "")

    # --- (b) side-by-side ledger for one cycle ---
    emit(lines, "--- (b) Side-by-side ledger qty 200 vs 250 (one cycle) ---")
    sample = sorted(base, key=lambda o: o.entry_date)[len(base) // 2]
    sample_date = sample.entry_date
    emit(lines, f"Sample cycle entry_date={sample_date}  (mid-span pick)")
    emit(lines, "")

    old_qty = sweep.ORIGINAL_BASKET_QTY
    try:
        sweep.ORIGINAL_BASKET_QTY = QTY_200
        led_200 = forensic.simulate_winner_ledger(
            sample, idx, times, closes, surface, collect_ledger=True
        )
        sweep.ORIGINAL_BASKET_QTY = QTY_250
        led_250 = forensic.simulate_winner_ledger(
            sample, idx, times, closes, surface, collect_ledger=True
        )
    finally:
        sweep.ORIGINAL_BASKET_QTY = old_qty

    emit(
        lines,
        f"  qty200 net={led_200.net:.4f} adj={led_200.n_adjustments}  |  "
        f"qty250 net={led_250.net:.4f} adj={led_250.n_adjustments}  |  "
        f"net_ratio={led_250.net / led_200.net if abs(led_200.net) > 1e-12 else float('nan'):.4f} "
        f"(lots ratio={QTY_250 / QTY_200:.4f})",
    )
    emit(lines, "")

    rows_a = led_200.ledger
    rows_b = led_250.ledger
    n_rows = max(len(rows_a), len(rows_b))
    emit(
        lines,
        f"{'#':>3} {'symbol':<28} {'side':<14} "
        f"{'q200':>4} {'px200':>10} {'fee200':>8} {'pnl200':>9} | "
        f"{'q250':>4} {'px250':>10} {'fee250':>8} {'pnl250':>9}  FLAG",
    )
    emit(lines, "-" * 140)
    n_highlight = 0
    for i in range(n_rows):
        a = rows_a[i] if i < len(rows_a) else None
        b = rows_b[i] if i < len(rows_b) else None
        if a is None or b is None:
            emit(
                lines,
                f"{i:3d} ROW COUNT MISMATCH — "
                f"len200={len(rows_a)} len250={len(rows_b)}",
            )
            n_highlight += 1
            continue
        pnl_a = cash_impact(a.side, a.price, a.qty_lots)
        pnl_b = cash_impact(b.side, b.price, b.qty_lots)
        # Proportional check: same symbol/side/price; qty & fee & pnl scale by 10/8
        scale = QTY_250 / QTY_200
        qty_ok = abs(b.qty_lots - a.qty_lots * scale) < 0.51  # allow int
        # Stricter: exact expected from formula if same step index
        price_ok = abs(b.price - a.price) < 1e-6
        sym_ok = a.symbol == b.symbol and a.side == b.side
        # fee/pnl should scale with THIS row's qty ratio if prices match
        if a.qty_lots > 0 and price_ok:
            qratio = b.qty_lots / a.qty_lots
            fee_ok = abs(b.fee - a.fee * qratio) < 1e-4 or (
                a.fee == 0 and b.fee == 0
            )
            pnl_ok = abs(pnl_b - pnl_a * qratio) < 1e-4 or (
                abs(pnl_a) < 1e-12 and abs(pnl_b) < 1e-12
            )
        else:
            fee_ok = abs(b.fee - a.fee * scale) < 1e-4
            pnl_ok = abs(pnl_b - pnl_a * scale) < 1e-4
        # Highlight if qty not entry-proportional (8→10) OR fee/pnl not matching actual qty ratio
        entry_prop = abs(b.qty_lots - round(a.qty_lots * scale)) < 1e-9
        proportional = sym_ok and price_ok and entry_prop and fee_ok and pnl_ok
        # Also flag when qty ratio != 1.25 even if internally consistent
        if a.qty_lots > 0:
            actual_qratio = b.qty_lots / a.qty_lots
            if abs(actual_qratio - scale) > 1e-9:
                proportional = False
        flag = "" if proportional else "<< NOT PROPORTIONAL"
        if flag:
            n_highlight += 1
        emit(
            lines,
            f"{i:3d} {a.symbol:<28} {a.side:<14} "
            f"{a.qty_lots:4d} {a.price:10.4f} {a.fee:8.4f} {pnl_a:9.4f} | "
            f"{b.qty_lots:4d} {b.price:10.4f} {b.fee:8.4f} {pnl_b:9.4f}  {flag}",
        )
    emit(lines, f"Highlighted non-proportional rows: {n_highlight} / {n_rows}")
    emit(lines, "")

    # --- (c) aggregate 300 cycles ---
    emit(lines, "--- (c) Aggregate qty 200 vs 250 over all 300 cycles ---")
    logger.info("Simulating qty 200 and 250 over %s cycles...", len(base))
    tot_prem_200 = tot_fees_200 = 0.0
    tot_prem_250 = tot_fees_250 = 0.0
    tot_adj_200 = tot_adj_250 = 0
    n_adj_qty_diff = 0
    nets_200: list[float] = []
    nets_250: list[float] = []
    dates: list[date] = []
    margins_nh: list[float] = []
    margins_wh: list[float] = []
    by_date: dict[date, float] = {}

    for i, o in enumerate(base):
        if (i + 1) % 50 == 0:
            logger.info("  sim %s/%s", i + 1, len(base))
        r200 = sweep.simulate_with_adjustments(
            o, WINNER_CFG, idx, times, closes, basket_qty=QTY_200
        )
        r250 = sweep.simulate_with_adjustments(
            o, WINNER_CFG, idx, times, closes, basket_qty=QTY_250
        )
        tot_prem_200 += r200.premium_sold_usd
        tot_prem_250 += r250.premium_sold_usd
        tot_fees_200 += r200.total_fees_usd
        tot_fees_250 += r250.total_fees_usd
        tot_adj_200 += r200.n_adjustments
        tot_adj_250 += r250.n_adjustments
        nets_200.append(r200.net)
        nets_250.append(r250.net)
        dates.append(o.entry_date)
        by_date[o.entry_date] = r200.net
        margins_nh.append(approx_margin(o, QTY_200, with_hedge=False))
        margins_wh.append(approx_margin(o, QTY_200, with_hedge=True))

        path200 = adj_qty_path(QTY_200, r200.n_adjustments)
        path250 = adj_qty_path(QTY_250, r250.n_adjustments)
        # Compare step-by-step: is 250 path == proportional to 200 path?
        diff = False
        if r200.n_adjustments != r250.n_adjustments:
            diff = True
        else:
            for qa, qb in zip(path200, path250):
                if abs(qb - qa * (QTY_250 / QTY_200)) > 1e-9:
                    diff = True
                    break
        if diff:
            n_adj_qty_diff += 1

    n = len(base)
    emit(lines, f"n_cycles={n}")
    emit(
        lines,
        f"  total premium collected at entry+adj sells:  "
        f"qty200={tot_prem_200:.4f}  qty250={tot_prem_250:.4f}  "
        f"ratio={tot_prem_250 / tot_prem_200:.4f}  (lots ratio=1.2500)",
    )
    emit(
        lines,
        f"  total fees:  qty200={tot_fees_200:.4f}  qty250={tot_fees_250:.4f}  "
        f"ratio={tot_fees_250 / tot_fees_200:.4f}",
    )
    emit(
        lines,
        f"  total adjustment count (sum):  qty200={tot_adj_200}  "
        f"qty250={tot_adj_250}  "
        f"mean/cycle 200={tot_adj_200 / n:.3f}  250={tot_adj_250 / n:.3f}",
    )
    emit(
        lines,
        f"  cycles where adj qty path NOT proportional to 1.25×: "
        f"{n_adj_qty_diff} / {n}",
    )
    emit(
        lines,
        f"  mean net/cycle: qty200={statistics.mean(nets_200):.4f}  "
        f"qty250={statistics.mean(nets_250):.4f}  "
        f"ratio={statistics.mean(nets_250) / statistics.mean(nets_200):.4f}",
    )
    emit(
        lines,
        "  interaction: decrease_pct=20% uses floor(orig×remaining), so",
    )
    emit(
        lines,
        "  absolute step size is 2 lots for BOTH 8 and 10 — not 20% of current.",
    )
    emit(
        lines,
        "  After adj1: 6 vs 8 (ratio 1.333). After adj2: 4 vs 6 (ratio 1.500).",
    )
    emit(
        lines,
        "  Entry ratio 1.25 never preserved mid-cycle → sub-linear net scale.",
    )
    emit(lines, "")

    # =====================================================================
    # PART B
    # =====================================================================
    emit(lines, "===== PART B: FAIR HEDGE COMPARISON (3 columns) =====")
    mean200, ci_lo200, ci_hi200 = eng.bootstrap_mean_ci(
        nets_200, BOOTSTRAP_N, BOOTSTRAP_SEED
    )
    cpd = n / float(max(1, day_span))
    mean_day_nh = mean200 * cpd
    mean_day_med = mean_day_nh - HEDGE_COST_MEDIAN
    ci_lo_nh = ci_lo200 * cpd
    ci_hi_nh = ci_hi200 * cpd
    ci_lo_med = ci_lo_nh - HEDGE_COST_MEDIAN
    ci_hi_med = ci_hi_nh - HEDGE_COST_MEDIAN
    chron = [x for _, x in sorted(zip(dates, nets_200), key=lambda z: z[0])]
    mdd_basket = eng.max_drawdown(chron)
    worst_b = min(nets_200)
    margin_nh = statistics.mean(margins_nh)
    margin_wh = statistics.mean(margins_wh)
    rom_nh = (mean_day_nh * 365.25 / margin_nh) if margin_nh > 1e-9 else float("nan")
    rom_med = (mean_day_med * 365.25 / margin_wh) if margin_wh > 1e-9 else float("nan")

    crash_days = (CRASH_MONTH_END - CRASH_MONTH_START).days + 1
    crash_baskets = [
        by_date[d]
        for d in sorted(by_date)
        if CRASH_MONTH_START <= d <= CRASH_MONTH_END
    ]
    crash_tot_nh = sum(crash_baskets)
    crash_tot_med = crash_tot_nh - HEDGE_COST_MEDIAN * crash_days

    logger.info("Reconstructing live hedge cycles (qty=4, roll=3, min_dte=6)...")
    raw_hedge = hedge.reconstruct_hedge_cycles(
        idx,
        times,
        closes,
        min_hedge_dte=hedge.MIN_HEDGE_DTE_LIVE,
        roll_dte=hedge.ROLL_DTE_LIVE,
    )
    ok_hedge = [c for c in raw_hedge if c.status == "OK"]
    combined = hedge.attach_baskets(ok_hedge, by_date)
    # Per-hedge-cycle daily combined
    daily_comb: list[float] = []
    cycle_comb: list[float] = []
    for cc in combined:
        days = max(1, int(cc.hedge.days_held or 1))
        comb = cc.combined_as_basket_minus_bleed
        cycle_comb.append(comb)
        daily_comb.append(comb / float(days))

    emit(
        lines,
        f"col3 effective n = {len(combined)} hedge cycles with real prints "
        f"(OK={len(ok_hedge)}).",
    )
    if len(daily_comb) >= 2:
        mean_d3, lo_d3, hi_d3 = eng.bootstrap_mean_ci(
            daily_comb, BOOTSTRAP_N, BOOTSTRAP_SEED + 7
        )
    elif len(daily_comb) == 1:
        mean_d3 = daily_comb[0]
        lo_d3 = hi_d3 = float("nan")
    else:
        mean_d3 = lo_d3 = hi_d3 = float("nan")

    worst_c3 = min(cycle_comb) if cycle_comb else float("nan")
    # Max drawdown of running sum of hedge-cycle combined (chronological)
    comb_chron = [
        cc.combined_as_basket_minus_bleed
        for cc in sorted(
            combined,
            key=lambda x: x.hedge.entry_date or date.min,
        )
    ]
    mdd_c3 = eng.max_drawdown(comb_chron) if comb_chron else float("nan")

    # Crash-month for col3: basket in window + pro-rata hedge PnL overlapping window
    hedge_crash = 0.0
    for c in ok_hedge:
        if c.entry_date is None or c.exit_date is None or c.realized_pnl_usd is None:
            continue
        # overlap of [entry, exit) with [crash_start, crash_end]
        lo = max(c.entry_date, CRASH_MONTH_START)
        hi = min(c.exit_date, CRASH_MONTH_END + timedelta(days=1))
        if hi <= lo:
            continue
        overlap = (hi - lo).days
        held = max(1, int(c.days_held or 1))
        hedge_crash += float(c.realized_pnl_usd) * (overlap / held)
    crash_tot_c3 = crash_tot_nh + hedge_crash

    rom_c3 = (
        (mean_d3 * 365.25 / margin_wh) if margin_wh > 1e-9 and math.isfinite(mean_d3) else float("nan")
    )

    emit(lines, "")
    emit(
        lines,
        f"{'metric':<28} {'NO HEDGE':>16} {'HEDGE med bleed':>16} "
        f"{'HEDGE actual n=8':>18}",
    )
    emit(lines, "-" * 82)
    emit(
        lines,
        f"{'mean/day':<28} {mean_day_nh:16.4f} {mean_day_med:16.4f} "
        f"{mean_d3:18.4f}",
    )
    emit(
        lines,
        f"{'ci_lo':<28} {ci_lo_nh:16.4f} {ci_lo_med:16.4f} "
        f"{lo_d3:18.4f}",
    )
    emit(
        lines,
        f"{'ci_hi':<28} {ci_hi_nh:16.4f} {ci_hi_med:16.4f} "
        f"{hi_d3:18.4f}",
    )
    emit(
        lines,
        f"{'worst cycle':<28} {worst_b:16.4f} {worst_b:16.4f} "
        f"{worst_c3:18.4f}",
    )
    emit(
        lines,
        f"{'max drawdown':<28} {mdd_basket:16.4f} {mdd_basket:16.4f} "
        f"{mdd_c3:18.4f}",
    )
    emit(
        lines,
        f"{'crash-month total':<28} {crash_tot_nh:16.4f} {crash_tot_med:16.4f} "
        f"{crash_tot_c3:18.4f}",
    )
    emit(
        lines,
        f"{'margin approx':<28} {margin_nh:16.2f} {margin_wh:16.2f} "
        f"{margin_wh:18.2f}",
    )
    emit(
        lines,
        f"{'return on margin ann':<28} {rom_nh:16.4f} {rom_med:16.4f} "
        f"{rom_c3:18.4f}",
    )
    emit(lines, "")
    emit(
        lines,
        "col1/col2 CI = basket bootstrap (n=300) × cycles_per_day; "
        f"col2 subtracts {HEDGE_COST_MEDIAN}/day.",
    )
    emit(
        lines,
        "col3 CI = bootstrap of per-hedge-cycle (combined/days_held); "
        f"effective n = {len(daily_comb)}.",
    )
    emit(
        lines,
        "col3 worst/mdd are over the n hedge-cycle combined lump sums "
        "(not per basket day).",
    )
    emit(lines, "")

    # Break-even
    emit(lines, "--- Break-even crashes / year ---")
    if ok_hedge:
        crash_h = max(ok_hedge, key=lambda c: float(c.realized_pnl_usd or -1e18))
        # Prefer cycle covering CRASH_DAY if present
        covering = [
            c
            for c in ok_hedge
            if c.entry_date
            and c.exit_date
            and c.entry_date <= CRASH_DAY < c.exit_date
        ]
        if covering:
            crash_h = covering[0]
        normals = [c for c in ok_hedge if c is not crash_h]
        bleeds_ex = [
            float(c.bleed_per_day)
            for c in normals
            if c.bleed_per_day is not None
        ]
        bleed_year = (
            statistics.mean(bleeds_ex) * 365.25 if bleeds_ex else float("nan")
        )
        normal_pnl = (
            statistics.mean(
                [float(c.realized_pnl_usd or 0.0) for c in normals]
            )
            if normals
            else float("nan")
        )
        crash_pnl = float(crash_h.realized_pnl_usd or 0.0)
        crash_benefit = crash_pnl - normal_pnl
        be = (
            bleed_year / crash_benefit
            if math.isfinite(bleed_year)
            and math.isfinite(crash_benefit)
            and abs(crash_benefit) > 1e-12
            else float("nan")
        )
        years = day_span / 365.25
        observed = 1.0 / years  # one crash day (2026-06-02) in sample
        emit(
            lines,
            f"  crash hedge cycle: entry={crash_h.entry_date} "
            f"exit={crash_h.exit_date} pnl={na_f(crash_h.realized_pnl_usd)} "
            f"bleed/day={na_f(crash_h.bleed_per_day)}",
        )
        emit(
            lines,
            f"  hedge bleed per year (ex-crash cycles, mean bleed/day×365.25): "
            f"{na_f(bleed_year)}",
        )
        emit(
            lines,
            f"  crash event net fayda (crash_pnl − mean_normal_pnl): "
            f"{na_f(crash_benefit)}  "
            f"(crash={na_f(crash_pnl)}, normal_mean={na_f(normal_pnl)})",
        )
        emit(lines, f"  break-even crashes/year = bleed_year / fayda = {na_f(be)}")
        emit(
            lines,
            f"  observed crashes/year in sample: 1 crash day / "
            f"{years:.3f}y = {observed:.3f}/yr  "
            f"(crash day={CRASH_DAY}, day_span={day_span})",
        )
    else:
        emit(lines, "  NOT AVAILABLE — no OK hedge cycles")
    emit(lines, "")

    # =====================================================================
    # PART C
    # =====================================================================
    emit(lines, "===== PART C: HEDGE SIZE × ROLL BLEED SENSITIVITY =====")
    emit(
        lines,
        "Bleed only (real prints). Combined/day = "
        f"{BASKET_MEAN_DAY_NO_HEDGE} − median_bleed/day (col2 method).",
    )
    emit(lines, "")
    emit(
        lines,
        f"{'hqty':>5} {'roll':>5} {'n_ok':>5} {'bleed_mean':>12} "
        f"{'bleed_med':>12} {'comb/day':>12}",
    )
    emit(lines, "-" * 60)

    for roll in (3, 7, 10):
        logger.info("Hedge reconstruct roll_dte=%s ...", roll)
        cyc = hedge.reconstruct_hedge_cycles(
            idx,
            times,
            closes,
            min_hedge_dte=hedge.MIN_HEDGE_DTE_LIVE,
            roll_dte=roll,
        )
        ok4 = [c for c in cyc if c.status == "OK"]
        for hq in (4, 3, 2):
            scaled = [scale_hedge_cycle(c, hq) for c in ok4]
            bleeds = [
                float(c.bleed_per_day)
                for c in scaled
                if c.bleed_per_day is not None
            ]
            if not bleeds:
                emit(
                    lines,
                    f"{hq:5d} {roll:5d} {0:5d} {'N/A':>12} {'N/A':>12} {'N/A':>12}",
                )
                continue
            bmean = statistics.mean(bleeds)
            bmed = statistics.median(bleeds)
            comb_day = BASKET_MEAN_DAY_NO_HEDGE - bmed
            emit(
                lines,
                f"{hq:5d} {roll:5d} {len(scaled):5d} {bmean:12.6f} "
                f"{bmed:12.6f} {comb_day:12.4f}",
            )

    emit(lines, "")
    emit(
        lines,
        "Note: qty≠4 rows are exact linear rescales of the same print fills "
        f"(factor = hqty/{HEDGE_QTY_LIVE}); roll changes cycle set.",
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
