#!/usr/bin/env python3
"""
S001 winner stress — WINNER vs CONTROL only (no new sweep).

WINNER  = dte2, B_only, trigger 70
CONTROL = dte2, adjustment none

Reuses s001_adjustment_sweep engine + s001_income_cycles.pkl.
No new data download.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s001_adjustment_sweep as sweep  # noqa: E402
import s001_income_engine as eng  # noqa: E402
import options_trades as ot  # noqa: E402

logger = logging.getLogger("s001_winner_stress")

RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_winner_stress.txt"

BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
BLOCK_DAYS = 3
CRASH_DAYS = (date(2025, 11, 19), date(2026, 6, 2))

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
CONTROL_CFG = sweep.SweepCfg(dte=2, adjustment="none", trigger_pct=None)


@dataclass
class StressRow:
    entry_date: date
    net: float
    n_adjustments: int
    premium_sold_usd: float
    total_fees_usd: float


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def require_entry_date(o: eng.CycleObs) -> date:
    """Abort rather than guess if entry_date is missing."""
    d = getattr(o, "entry_date", None)
    if d is None:
        raise RuntimeError(
            "CycleObs.entry_date missing — cannot run overlap/crash tests. "
            "Stopping (no guess)."
        )
    if not isinstance(d, date):
        raise RuntimeError(
            f"CycleObs.entry_date has unexpected type {type(d)!r} — stopping."
        )
    return d


def run_config(
    cfg: sweep.SweepCfg,
    cycles: list[eng.CycleObs],
    idx: eng.TradeIndex | None,
    times: list[int],
    closes: list[float],
) -> list[StressRow]:
    out: list[StressRow] = []
    for o in cycles:
        ed = require_entry_date(o)
        if cfg.adjustment == "none":
            r = sweep.simulate_none(o)
        else:
            assert idx is not None
            r = sweep.simulate_with_adjustments(o, cfg, idx, times, closes)
        if r.entry_date is None:
            raise RuntimeError(
                "CycleResult.entry_date missing after simulate — stopping."
            )
        out.append(
            StressRow(
                entry_date=r.entry_date if r.entry_date is not None else ed,
                net=r.net,
                n_adjustments=r.n_adjustments,
                premium_sold_usd=r.premium_sold_usd,
                total_fees_usd=r.total_fees_usd,
            )
        )
    out.sort(key=lambda r: r.entry_date)
    return out


def iid_bootstrap_ci(nets: list[float], *, seed: int) -> tuple[float, float, float]:
    return eng.bootstrap_mean_ci(nets, BOOTSTRAP_N, seed)


def block_bootstrap_ci(
    rows: list[StressRow],
    *,
    block_days: int,
    seed: int,
    n_boot: int = BOOTSTRAP_N,
) -> tuple[float, float, float, int]:
    """
    Block bootstrap over consecutive entry-date days (length=block_days).
    Respects multi-day overlap: each resampled unit is a contiguous
    block of `block_days` entry dates.
    Returns (mean, ci_lo, ci_hi, n_blocks).
    """
    if not rows:
        return float("nan"), float("nan"), float("nan"), 0

    by_day: dict[date, list[float]] = defaultdict(list)
    for r in rows:
        by_day[r.entry_date].append(r.net)
    days = sorted(by_day.keys())
    if len(days) < block_days:
        # Degenerate: fall back to iid on available days
        nets = [r.net for r in rows]
        m, lo, hi = iid_bootstrap_ci(nets, seed=seed)
        return m, lo, hi, 0

    blocks: list[list[float]] = []
    for i in range(len(days) - block_days + 1):
        chunk: list[float] = []
        for j in range(block_days):
            chunk.extend(by_day[days[i + j]])
        if chunk:
            blocks.append(chunk)

    n_blocks = len(blocks)
    if n_blocks == 0:
        nets = [r.net for r in rows]
        m, lo, hi = iid_bootstrap_ci(nets, seed=seed)
        return m, lo, hi, 0

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        for _b in range(n_blocks):
            sample.extend(blocks[rng.randrange(n_blocks)])
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[min(n_boot - 1, int(0.975 * n_boot))]
    overall = statistics.mean([r.net for r in rows])
    return overall, lo, hi, n_blocks


def every_nth_day_subset(rows: list[StressRow], offset: int, step: int = 3) -> list[StressRow]:
    days = sorted({r.entry_date for r in rows})
    keep = {d for i, d in enumerate(days) if i % step == offset}
    return [r for r in rows if r.entry_date in keep]


def pctile(vals: list[float], p: float) -> float:
    return eng.pctile(vals, p)


def write_report(
    winner: list[StressRow],
    control: list[StressRow],
    *,
    day_span: int,
) -> list[str]:
    lines: list[str] = []
    emit(lines, "S001 WINNER STRESS — WINNER vs CONTROL")
    emit(lines, "=" * 90)
    emit(
        lines,
        "fixed: fill=maker  strike=B25  wing=2000  entry_IST=11:00  "
        f"basket_qty={sweep.ORIGINAL_BASKET_QTY}",
    )
    emit(lines, "WINNER  = dte=2  adjustment=B_only  trigger_pct=70")
    emit(lines, "CONTROL = dte=2  adjustment=none")
    emit(
        lines,
        f"bootstrap_n={BOOTSTRAP_N}  seed={BOOTSTRAP_SEED}  "
        f"block_days={BLOCK_DAYS}  day_span={day_span}",
    )
    emit(lines, f"n_winner={len(winner)}  n_control={len(control)}")
    emit(lines, "")

    # ----- TEST 1: OVERLAP -----
    emit(lines, "===== TEST 1: OVERLAP =====")
    emit(lines, "")
    emit(lines, "(a) Adjustment distribution — WINNER (B_only / 70)")
    hist = {0: 0, 1: 0, 2: 0}
    for r in winner:
        k = int(r.n_adjustments)
        if k < 0:
            k = 0
        if k > 2:
            k = 2
        hist[k] = hist.get(k, 0) + 1
    n_w = max(1, len(winner))
    emit(lines, f"  adjustments=0: {hist[0]:4d}  ({100.0 * hist[0] / n_w:5.1f}%)")
    emit(lines, f"  adjustments=1: {hist[1]:4d}  ({100.0 * hist[1] / n_w:5.1f}%)")
    emit(lines, f"  adjustments=2: {hist[2]:4d}  ({100.0 * hist[2] / n_w:5.1f}%)")
    emit(
        lines,
        f"  % cycles at cap (=2): {100.0 * hist[2] / n_w:.1f}%",
    )
    emit(lines, "")

    emit(lines, "(b) Non-overlapping subsets — every 3rd entry-day (3 offsets)")
    emit(
        lines,
        f"  {'offset':>6}  {'n':>5}  {'mean':>9}  {'ci_lo':>9}  {'ci_hi':>9}  label",
    )
    for offset in (0, 1, 2):
        sub = every_nth_day_subset(winner, offset, step=3)
        nets = [r.net for r in sub]
        mean, lo, hi = iid_bootstrap_ci(
            nets, seed=BOOTSTRAP_SEED + 1000 + offset
        )
        label = f"days {offset},{offset + 3},{offset + 6},..."
        emit(
            lines,
            f"  {offset:>6}  {len(sub):5d}  {mean:9.4f}  {lo:9.4f}  {hi:9.4f}  {label}",
        )
    emit(lines, "")

    emit(lines, "(c) Block bootstrap (block_days=3) vs original iid ci_lo")
    w_nets = [r.net for r in winner]
    iid_mean, iid_lo, iid_hi = iid_bootstrap_ci(w_nets, seed=BOOTSTRAP_SEED)
    blk_mean, blk_lo, blk_hi, n_blocks = block_bootstrap_ci(
        winner, block_days=BLOCK_DAYS, seed=BOOTSTRAP_SEED + 77
    )
    emit(
        lines,
        f"  original sweep cited ci_lo ~ 0.76  "
        f"(this recompute iid ci_lo={iid_lo:.4f})",
    )
    emit(
        lines,
        f"  iid  bootstrap: mean={iid_mean:.4f}  ci_lo={iid_lo:.4f}  ci_hi={iid_hi:.4f}",
    )
    emit(
        lines,
        f"  block bootstrap (len={BLOCK_DAYS}d, n_blocks={n_blocks}): "
        f"mean={blk_mean:.4f}  ci_lo={blk_lo:.4f}  ci_hi={blk_hi:.4f}",
    )
    emit(
        lines,
        f"  side-by-side ci_lo:  original~0.7557 | iid={iid_lo:.4f} | "
        f"block3={blk_lo:.4f}",
    )
    emit(lines, "")

    # ----- TEST 2: LEVERAGE vs EDGE -----
    emit(lines, "===== TEST 2: LEVERAGE vs EDGE =====")
    emit(
        lines,
        f"  {'config':<10}  {'n':>5}  {'mean_prem_sold':>14}  "
        f"{'mean_fees':>10}  {'mean_pnl':>10}  {'pnl_per_$prem':>13}",
    )

    def lev_line(name: str, rows: list[StressRow]) -> None:
        if not rows:
            emit(lines, f"  {name:<10}  (no cycles)")
            return
        mp = statistics.mean([r.premium_sold_usd for r in rows])
        mf = statistics.mean([r.total_fees_usd for r in rows])
        mn = statistics.mean([r.net for r in rows])
        # per-cycle ratio then mean, and also aggregate ratio
        ratios = [
            (r.net / r.premium_sold_usd)
            if r.premium_sold_usd > 1e-12
            else float("nan")
            for r in rows
        ]
        ratios_f = [x for x in ratios if math.isfinite(x)]
        mean_ratio = statistics.mean(ratios_f) if ratios_f else float("nan")
        emit(
            lines,
            f"  {name:<10}  {len(rows):5d}  {mp:14.4f}  {mf:10.4f}  "
            f"{mn:10.4f}  {mean_ratio:13.6f}",
        )
        emit(
            lines,
            f"             aggregate pnl/prem = "
            f"{(sum(r.net for r in rows) / sum(r.premium_sold_usd for r in rows)):.6f}"
            f"  (sum_pnl / sum_prem)",
        )

    lev_line("WINNER", winner)
    lev_line("CONTROL", control)
    emit(lines, "")
    emit(
        lines,
        "  Interpretation aid: if WINNER pnl_per_$prem ~ CONTROL, "
        "gain is mostly leverage (more premium sold);",
    )
    emit(
        lines,
        "  if WINNER pnl_per_$prem >> CONTROL, gain includes true edge.",
    )
    emit(lines, "")

    # ----- TEST 3: TAIL -----
    emit(lines, "===== TEST 3: TAIL =====")

    def tail_block(name: str, rows: list[StressRow]) -> None:
        nets = [r.net for r in rows]
        chron = [r.net for r in sorted(rows, key=lambda x: x.entry_date)]
        mdd = eng.max_drawdown(chron)
        worst = min(nets) if nets else float("nan")
        emit(lines, f"  {name}:")
        emit(lines, f"    worst={worst:.4f}")
        emit(lines, f"    p1={pctile(nets, 1):.4f}")
        emit(lines, f"    p5={pctile(nets, 5):.4f}")
        emit(lines, f"    p10={pctile(nets, 10):.4f}")
        emit(lines, f"    max_drawdown_running_sum={mdd:.4f}")

    tail_block("WINNER", winner)
    tail_block("CONTROL", control)
    emit(lines, "")
    emit(lines, "  Crash-day cycles (by entry_date):")
    for crash in CRASH_DAYS:
        emit(lines, f"  --- {crash.isoformat()} ---")
        w_hits = [r for r in winner if r.entry_date == crash]
        c_hits = [r for r in control if r.entry_date == crash]
        if not w_hits and not c_hits:
            emit(lines, "    (no cycles with this entry_date in either config)")
            continue
        for r in w_hits:
            emit(
                lines,
                f"    WINNER  entry={r.entry_date.isoformat()}  "
                f"net={r.net:.4f}  adj={r.n_adjustments}  "
                f"prem_sold={r.premium_sold_usd:.4f}",
            )
        if not w_hits:
            emit(lines, "    WINNER  (no cycle)")
        for r in c_hits:
            emit(
                lines,
                f"    CONTROL entry={r.entry_date.isoformat()}  "
                f"net={r.net:.4f}  adj={r.n_adjustments}  "
                f"prem_sold={r.premium_sold_usd:.4f}",
            )
        if not c_hits:
            emit(lines, "    CONTROL (no cycle)")
    emit(lines, "")

    # ----- TEST 4: CLAMP SOURCE -----
    emit(lines, "===== TEST 4: CLAMP SOURCE (report only — no code change) =====")
    emit(
        lines,
        "  adj_b_trigger_pct is clamped to [10, 90] in multiple places:",
    )
    emit(lines, "")
    emit(
        lines,
        "  1) LIVE RUNTIME (hardcoded clamp after DB read):",
    )
    emit(
        lines,
        "     backend/strategies/s001_short_strangle/logic.py",
    )
    emit(
        lines,
        "     function _load_adj_engine_settings, line 113:",
    )
    emit(lines, "       pct = max(10.0, min(90.0, pct))")
    emit(
        lines,
        "     Kind: hardcoded limit in strategy code (not a DB CHECK).",
    )
    emit(lines, "")
    emit(
        lines,
        "  2) TRADE API snapshot path (hardcoded clamp):",
    )
    emit(lines, "     backend/api/routes_trade.py  lines 1639–1646:")
    emit(lines, "       adj_b_trigger_pct = max(10.0, min(90.0, adj_b_trigger_pct))")
    emit(lines, "     Kind: hardcoded limit when exposing settings to client.")
    emit(lines, "")
    emit(
        lines,
        "  3) PYDANTIC / API validation (schema bounds):",
    )
    emit(
        lines,
        "     backend/schemas.py  line 147:",
    )
    emit(
        lines,
        "       adj_b_trigger_pct: float = Field(default=50.0, ge=10, le=90)",
    )
    emit(
        lines,
        "     backend/api/routes_auto_trade.py  line 118:",
    )
    emit(
        lines,
        "       adj_b_trigger_pct: float = Field(default=50.0, ge=10, le=90)",
    )
    emit(
        lines,
        "     Kind: request validation — rejects values outside [10, 90] "
        "before persist.",
    )
    emit(lines, "")
    emit(
        lines,
        "  Sweep mapping note: s001_adjustment_sweep.adj_b_pct_from_trigger",
    )
    emit(
        lines,
        "  mirrors the live [10,90] clamp so trigger=110 → adj_b=90 "
        "(why B_only 90 and 110 matched).",
    )
    emit(lines, "")
    emit(lines, "END")
    return lines


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    logger.info("Loading cycle cache %s", sweep.CYCLES_CACHE)
    all_obs, day_span = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)
    logger.info("dte=2 base cycles: %s", len(base))
    if not base:
        sys.stderr.write("ABORT: no dte=2 base cycles after filter.\n")
        return 2

    # Confirm entry_date present before any heavy work
    require_entry_date(base[0])

    logger.info("Building trade index (WINNER needs path sim)...")
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()

    logger.info("Simulating CONTROL (none)...")
    control = run_config(CONTROL_CFG, base, None, times, closes)
    logger.info("Simulating WINNER (B_only / 70)...")
    winner = run_config(WINNER_CFG, base, idx, times, closes)

    lines = write_report(winner, control, day_span=day_span)
    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    # Windows consoles may be cp1252 — avoid UnicodeEncodeError on stdout
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
