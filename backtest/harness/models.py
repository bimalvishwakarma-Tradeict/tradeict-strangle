"""Shared dataclasses for the harness strategy interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal


@dataclass
class StrategyMeta:
    id: str
    name: str
    version: str
    description: str
    status: str = "TESTING"  # LIVE | TESTING | PARKED | CLOSED


@dataclass
class Leg:
    symbol: str
    strike: float
    opt_type: Literal["call", "put"]
    side: Literal["buy", "sell"]
    qty: int
    mark: float = 0.0
    fill: float = 0.0
    fee: float = 0.0
    slip_pct: float = 0.0
    status: str = "open"
    realized: float = 0.0
    baseline: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    """Returned by strategy.manage()."""

    kind: Literal["hold", "exit", "adjust", "skip"]
    reason: str = ""
    legs_to_close: list[str] = field(default_factory=list)  # symbols
    legs_to_open: list[Leg] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionState:
    entry_ts: int
    entry_date: date
    legs: list[Leg] = field(default_factory=list)
    n_adjustments: int = 0
    realized: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0
    worst_mtm: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CycleResult:
    strategy_id: str
    entry_date: date
    entry_ts: int
    exit_ts: int
    exit_reason: str
    hold_hours: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    worst_mtm: float
    n_adjustments: int = 0
    arm: str = ""
    window: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkipAccount:
    days_in_window: int = 0
    cycles_entered: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def record(self, reason: str, day: date) -> None:
        self.counts[reason] = int(self.counts.get(reason, 0)) + 1
        ex = self.examples.setdefault(reason, [])
        ds = day.isoformat()
        if ds not in ex and len(ex) < 5:
            ex.append(ds)
