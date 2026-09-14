#!/usr/bin/env python3
"""
S001 basket qty ratio sweep — 5 pre-registered pct_of_hedge configs.

WINNER fixed: dte2, B_only, trig70, maker, B25, wings 2000, 11:00 IST,
hedge 4 lots/leg, roll 3/hard 2/min_hold 10/min_dte 6.

Only sweep: basket_qty_pct_of_hedge ∈ {200, 250, 300, 350, 400}
  → basket_qty = ceil(4 × pct / 100)

Hedge as deterministic daily cost = 0.6255 USD/day (median bleed from
hedge integration v2, crash-winner skew removed).
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_income_engine as eng  # noqa: E402

logger = logging.getLogger("s001_qty_ratio_sweep")

RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_qty_ratio_sweep.txt"

HEDGE_QTY_LOTS = 4
QTY_PCTS = (200, 250, 300, 350, 400)  # exactly 5 — do not expand
assert len(QTY_PCTS) == 5

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
HEDGE_COST_PER_DAY = 0.6255  # median bleed/day from hedge integration v2 Fix B
# APPROX margin stand-in (NOT Delta SPAN) — documented in report:
#   shorts: 5 × initial short credit USD
#   wings:  initial wing debit USD
#   hedge:  fixed 4-lot ATM straddle debit proxy (median from hedge v2 OK cycles)
HEDGE_DEBIT_PROXY_USD = 32.0  # ~median entry$ from hedge integration OK cycles
SHORT_CREDIT_MARGIN_MULT = 5.0

BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
CONTRACT_VALUE = eng.CONTRACT_VALUE


@dataclass
class QtyRow:
    pct: int
    basket_qty: int
    n: int
    n_skipped: int
    mean: float
    median: float
    std: float
    ci_lo: float
    ci_hi: float
    worst: float
    p1: float
    p5: float
    mdd: float
    mean_prem_sold: float
    mean_fees: float
    cycles_per_day: float
    combined_per_day: float
    combined_ci_lo: float
    combined_ci_hi: float
    margin_approx: float
    return_on_margin_ann: float
    worst_pct_margin: float


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def basket_qty_from_pct(pct: int) -> int:
    return int(math.ceil(HEDGE_QTY_LOTS * float(pct) / 100.0))


def approx_margin_usd(o: eng.CycleObs, qty: int) -> float:
    """
    Transparent APPROX — not exchange SPAN.
    margin ≈ 5×short_credit + wing_debit + hedge_debit_proxy
    """
    sc = float(o.short_call.price)
    sp = float(o.short_put.price)
    short_credit = (sc + sp) * qty * CONTRACT_VALUE
    wing_debit = 0.0
    if o.wing_call is not None and o.wing_put is not None:
        wing_debit = (
            float(o.wing_call.price) + float(o.wing_put.price)
        ) * qty * CONTRACT_VALUE
    return (
        SHORT_CREDIT_MARGIN_MULT * short_credit
        + wing_debit
        + HEDGE_DEBIT_PROXY_USD
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if len(QTY_PCTS) != 5:
        sys.stderr.write(f"ABORT: expected 5 configs, got {len(QTY_PCTS)}\n")
        return 2

    lines: list[str] = []
    emit(lines, "S001 BASKET QTY RATIO SWEEP — 5 pre-registered configs")
    emit(lines, "=" * 100)
    emit(
        lines,
        "FIXED: dte=2 B_only trig70 maker B25 wings=2000 entry=11:00 IST "
        f"hedge={HEDGE_QTY_LOTS}/leg long ATM straddle "
        "roll=3 hard=2 min_hold=10 min_dte=6",
    )
    emit(
        lines,
        f"SWEEP: basket_qty_pct_of_hedge = {list(QTY_PCTS)} "
        f"→ qty = ceil({HEDGE_QTY_LOTS} × pct/100)",
    )
    emit(
        lines,
        f"Hedge cost (deterministic): {HEDGE_COST_PER_DAY} USD/day "
        "(median bleed from hedge integration v2 Fix B)",
    )
    emit(lines, f"bootstrap_n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED}")
    emit(lines, "")

    logger.info("Loading cycles + trade index...")
    all_obs, day_span = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)
    logger.info("dte=2 base cycles: %s  day_span=%s", len(base), day_span)
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()

    rows: list[QtyRow] = []
    for i, pct in enumerate(QTY_PCTS):
        qty = basket_qty_from_pct(pct)
        logger.info("Config %s/5 pct=%s → basket_qty=%s", i + 1, pct, qty)
        nets: list[float] = []
        prems: list[float] = []
        fees: list[float] = []
        margins: list[float] = []
        dates: list[date] = []
        n_skipped = 0
        for o in base:
            # Wings required ON — CycleObs already filtered wing=2000; if missing fills skip
            if (
                o.wing_call is None
                or o.wing_put is None
                or o.wing_call_k is None
                or o.wing_put_k is None
            ):
                n_skipped += 1
                continue
            if o.short_call.price <= 0 or o.short_put.price <= 0:
                n_skipped += 1
                continue
            try:
                r = sweep.simulate_with_adjustments(
                    o,
                    WINNER_CFG,
                    idx,
                    times,
                    closes,
                    basket_qty=qty,
                )
            except Exception as exc:  # noqa: BLE001 — skip unusable cycle
                logger.warning("skip cycle %s: %s", o.entry_date, exc)
                n_skipped += 1
                continue
            if not math.isfinite(r.net):
                n_skipped += 1
                continue
            nets.append(r.net)
            prems.append(r.premium_sold_usd)
            fees.append(r.total_fees_usd)
            margins.append(approx_margin_usd(o, qty))
            dates.append(o.entry_date)

        if not nets:
            sys.stderr.write(f"ABORT: no cycles for pct={pct}\n")
            return 2

        mean, ci_lo, ci_hi = eng.bootstrap_mean_ci(
            nets, BOOTSTRAP_N, BOOTSTRAP_SEED + i * 97
        )
        # cycles per day: entries / calendar day_span
        cycles_per_day = len(nets) / float(max(1, day_span))
        comb_day = mean * cycles_per_day - HEDGE_COST_PER_DAY
        comb_lo = ci_lo * cycles_per_day - HEDGE_COST_PER_DAY
        comb_hi = ci_hi * cycles_per_day - HEDGE_COST_PER_DAY
        margin = statistics.mean(margins)
        # Annualise: combined_per_day * 365 / margin
        rom = (
            (comb_day * 365.25 / margin) if margin > 1e-9 else float("nan")
        )
        chron = [n for _, n in sorted(zip(dates, nets), key=lambda x: x[0])]
        worst = min(nets)
        rows.append(
            QtyRow(
                pct=pct,
                basket_qty=qty,
                n=len(nets),
                n_skipped=n_skipped,
                mean=mean,
                median=statistics.median(nets),
                std=statistics.stdev(nets) if len(nets) > 1 else float("nan"),
                ci_lo=ci_lo,
                ci_hi=ci_hi,
                worst=worst,
                p1=eng.pctile(nets, 1),
                p5=eng.pctile(nets, 5),
                mdd=eng.max_drawdown(chron),
                mean_prem_sold=statistics.mean(prems),
                mean_fees=statistics.mean(fees),
                cycles_per_day=cycles_per_day,
                combined_per_day=comb_day,
                combined_ci_lo=comb_lo,
                combined_ci_hi=comb_hi,
                margin_approx=margin,
                return_on_margin_ann=rom,
                worst_pct_margin=(
                    100.0 * worst / margin if margin > 1e-9 else float("nan")
                ),
            )
        )

    # Detail blocks
    for r in rows:
        emit(lines, f"----- pct={r.pct}%  basket_qty={r.basket_qty} -----")
        emit(lines, f"  n={r.n}  skipped={r.n_skipped}")
        emit(
            lines,
            f"  basket mean/cycle={r.mean:.4f}  median={r.median:.4f}  "
            f"std={r.std:.4f}",
        )
        emit(lines, f"  ci_lo={r.ci_lo:.4f}  ci_hi={r.ci_hi:.4f}")
        emit(
            lines,
            f"  worst={r.worst:.4f}  p1={r.p1:.4f}  p5={r.p5:.4f}  mdd={r.mdd:.4f}",
        )
        emit(
            lines,
            f"  premium_sold/cycle={r.mean_prem_sold:.4f}  "
            f"fees/cycle={r.mean_fees:.4f}",
        )
        emit(
            lines,
            f"  cycles_per_day={r.cycles_per_day:.6f}  "
            f"(n/day_span, day_span={day_span})",
        )
        emit(
            lines,
            f"  combined/day = mean×cpd − {HEDGE_COST_PER_DAY} = "
            f"{r.combined_per_day:.4f}  "
            f"(ci_lo={r.combined_ci_lo:.4f} ci_hi={r.combined_ci_hi:.4f})",
        )
        emit(
            lines,
            f"  margin_APPROX={r.margin_approx:.2f}  "
            f"RoM_ann={r.return_on_margin_ann:.4f}  "
            f"worst/margin={r.worst_pct_margin:.2f}%",
        )
        emit(lines, "")

    emit(lines, "MARGIN APPROX formula (NOT Delta SPAN):")
    emit(
        lines,
        f"  5×initial_short_credit + wing_debit + hedge_debit_proxy "
        f"({HEDGE_DEBIT_PROXY_USD:.1f} USD for 4-lot ATM straddle)",
    )
    emit(lines, "")

    # Summary table
    emit(lines, "===== SUMMARY =====")
    emit(
        lines,
        f"{'qty%':>5} {'qty':>4} {'bsk_mean':>9} {'comb/day':>9} {'ci_lo':>9} "
        f"{'worst':>9} {'margin':>8} {'RoM_ann':>8} {'n':>5}",
    )
    emit(lines, "-" * 80)
    for r in rows:
        emit(
            lines,
            f"{r.pct:5d} {r.basket_qty:4d} {r.mean:9.4f} {r.combined_per_day:9.4f} "
            f"{r.combined_ci_lo:9.4f} {r.worst:9.4f} {r.margin_approx:8.2f} "
            f"{r.return_on_margin_ann:8.4f} {r.n:5d}",
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
