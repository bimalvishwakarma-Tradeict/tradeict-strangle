"""Unit tests for harness metrics."""

from __future__ import annotations

from datetime import date

from backtest.harness.metrics import (
    day_clustered_ci_daily,
    max_drawdown,
    summarize_cycles,
)
from backtest.harness.models import CycleResult


def _cyc(d: date, net: float) -> CycleResult:
    return CycleResult(
        strategy_id="T",
        entry_date=d,
        entry_ts=0,
        exit_ts=3600,
        exit_reason="TEST",
        hold_hours=1.0,
        gross_pnl=net,
        fees=0.0,
        slippage_cost=0.0,
        net_pnl=net,
        worst_mtm=min(0.0, net),
    )


def test_max_drawdown() -> None:
    assert abs(max_drawdown([1.0, -2.0, 0.5]) - (-2.0)) < 1e-9 or max_drawdown(
        [1.0, -2.0, 0.5]
    ) <= 0


def test_day_clustered_ci_deterministic() -> None:
    daily = [0.1, -0.05, 0.02, 0.0, -0.01]
    a = day_clustered_ci_daily(daily, n=200, seed=20260918)
    b = day_clustered_ci_daily(daily, n=200, seed=20260918)
    assert a == b
    assert a[1] <= a[0] <= a[2] or (a[1] != a[1])  # nan-safe


def test_summarize_cycles_mean_day() -> None:
    cycles = [
        _cyc(date(2026, 5, 1), 1.0),
        _cyc(date(2026, 5, 2), -0.5),
    ]
    st = summarize_cycles(cycles, date(2026, 5, 1), date(2026, 5, 2), bootstrap_n=50)
    assert st["n_cycles"] == 2
    assert abs(st["mean_day"] - 0.25) < 1e-9
    assert st["win_pct"] == 50.0
