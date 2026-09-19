"""Cycle / portfolio metrics for harness runs."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from backtest.harness.config import BOOTSTRAP_N, BOOTSTRAP_SEED
from backtest.harness.models import CycleResult


def max_drawdown(daily: list[float]) -> float:
    peak = 0.0
    eq = 0.0
    dd = 0.0
    for x in daily:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return float(dd)


def day_clustered_ci_daily(
    daily: list[float], n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float, float]:
    if not daily:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    means: list[float] = []
    m = len(daily)
    for _ in range(n):
        sample = [daily[rng.randrange(m)] for _ in range(m)]
        means.append(float(statistics.fmean(sample)))
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return float(statistics.fmean(means)), float(lo), float(hi)


def lots_at_risk_cap(
    max_loss_per_lot: float, capital: float = 100.0, cap_pct: float = 3.0
) -> int:
    budget = capital * (cap_pct / 100.0)
    if max_loss_per_lot <= 1e-12:
        return 0
    return max(0, int(math.floor(budget / max_loss_per_lot)))


def daily_ceiling_pct(
    lots: int, net_credit_per_lot: float, capital: float = 100.0
) -> float:
    if capital <= 0:
        return float("nan")
    return 100.0 * (lots * net_credit_per_lot) / capital


def summarize_cycles(
    cycles: list[CycleResult],
    d0: date,
    d1: date,
    *,
    bootstrap_n: int = BOOTSTRAP_N,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    n = len(cycles)
    day_span = max(1, (d1 - d0).days + 1)
    by_day: dict[date, float] = defaultdict(float)
    for c in cycles:
        by_day[c.entry_date] += c.net_pnl
    daily = [by_day.get(d0 + timedelta(days=i), 0.0) for i in range(day_span)]
    mean_day = float(statistics.fmean(daily)) if daily else float("nan")
    mean_p, lo, hi = day_clustered_ci_daily(daily, bootstrap_n, bootstrap_seed)

    wins = sum(1 for c in cycles if c.net_pnl > 0)
    reason_counts: dict[str, int] = defaultdict(int)
    for c in cycles:
        reason_counts[c.exit_reason] += 1
    mix = {
        k: (100.0 * v / n if n else float("nan"))
        for k, v in sorted(reason_counts.items())
    }
    holds = [c.hold_hours for c in cycles]
    worst = min(cycles, key=lambda c: c.net_pnl) if cycles else None

    return {
        "n_cycles": n,
        "win_pct": (100.0 * wins / n) if n else float("nan"),
        "mean_gross": float(statistics.fmean([c.gross_pnl for c in cycles]))
        if cycles
        else float("nan"),
        "mean_fees": float(statistics.fmean([c.fees for c in cycles]))
        if cycles
        else float("nan"),
        "mean_slippage": float(statistics.fmean([c.slippage_cost for c in cycles]))
        if cycles
        else float("nan"),
        "mean_net": float(statistics.fmean([c.net_pnl for c in cycles]))
        if cycles
        else float("nan"),
        "mean_day": mean_day,
        "ci_lo": lo,
        "ci_hi": hi,
        "bootstrap_mean": mean_p,
        "worst_net": float(worst.net_pnl) if worst else float("nan"),
        "worst_date": worst.entry_date.isoformat() if worst else "",
        "max_dd": max_drawdown(daily) if daily else float("nan"),
        "exit_mix": mix,
        "hold_med": float(statistics.median(holds)) if holds else float("nan"),
        "adj_per": float(statistics.fmean([c.n_adjustments for c in cycles]))
        if cycles
        else float("nan"),
        "fees_day": sum(c.fees for c in cycles) / day_span,
        "slip_day": sum(c.slippage_cost for c in cycles) / day_span,
    }


def matched_control_hook(
    signal_days: list[date],
    all_day_pnls: dict[date, float],
    *,
    seed: int = BOOTSTRAP_SEED,
    n_draws: int = 200,
) -> dict[str, Any]:
    """
    Random/matched control: sample |signal_days| random days and compare mean PnL.
    Hook for strategies that have an explicit signal set.
    """
    if not signal_days or not all_day_pnls:
        return {"signal_mean": float("nan"), "control_mean": float("nan")}
    sig = [all_day_pnls.get(d, 0.0) for d in signal_days]
    pool = list(all_day_pnls.values())
    rng = random.Random(seed)
    k = len(sig)
    ctrl_means: list[float] = []
    for _ in range(n_draws):
        sample = [pool[rng.randrange(len(pool))] for _ in range(k)]
        ctrl_means.append(float(statistics.fmean(sample)))
    return {
        "signal_mean": float(statistics.fmean(sig)),
        "control_mean": float(statistics.fmean(ctrl_means)) if ctrl_means else float("nan"),
        "control_ci_lo": sorted(ctrl_means)[int(0.025 * (len(ctrl_means) - 1))]
        if ctrl_means
        else float("nan"),
        "control_ci_hi": sorted(ctrl_means)[int(0.975 * (len(ctrl_means) - 1))]
        if ctrl_means
        else float("nan"),
        "n_signal_days": k,
    }
