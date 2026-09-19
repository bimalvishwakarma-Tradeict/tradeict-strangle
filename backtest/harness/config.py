"""Harness defaults — stage windows, bootstrap, skip list."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent.parent
_ROOT = _BACKTEST.parent

# Fixed KILL window for ALL strategies (do not change per strategy)
KILL_FROM = date(2026, 5, 1)
KILL_TO = date(2026, 5, 31)

# DESIGN = IS, CONFIRM = OOS (defaults; override in config)
DESIGN_FROM = date(2025, 7, 1)
DESIGN_TO = date(2026, 6, 30)
CONFIRM_FROM = date(2026, 7, 1)
CONFIRM_TO = date(2026, 9, 13)

SKIP_EXPIRIES = (date(2025, 4, 26),)
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260918
MARK_TOL_SEC = 60
STALE_BAR_SEC = 60
CONTRACT_VALUE = 0.001
EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30

MARKS_DIR = _BACKTEST / "cache" / "option_marks"
DATA_1M_DIR = _BACKTEST / "data_1m"
RESULTS_DIR = _BACKTEST / "results"
RUNS_DIR = RESULTS_DIR / "runs"
REGISTRY_JSON = RESULTS_DIR / "registry.json"
LEARNINGS_MD = _BACKTEST / "LEARNINGS.md"
STRATEGIES_DIR = _BACKTEST / "strategies"


@dataclass
class HarnessConfig:
    from_date: date
    to_date: date
    stage: str = "DESIGN"  # KILL | DESIGN | CONFIRM
    slip_mult: float = 1.0
    slip_model: str = "bucketed"
    bootstrap_n: int = BOOTSTRAP_N
    bootstrap_seed: int = BOOTSTRAP_SEED
    skip_expiries: tuple[date, ...] = SKIP_EXPIRIES
    mark_tol_sec: int = MARK_TOL_SEC
    stale_bar_sec: int = STALE_BAR_SEC
    capital_usd: float = 100.0
    risk_cap_pct: float = 3.0
    strategy_params: dict[str, Any] = field(default_factory=dict)
    window_tag: str = "IS"  # IS | OOS


def default_config(stage: str = "DESIGN") -> HarnessConfig:
    stage_u = stage.upper()
    if stage_u == "KILL":
        return HarnessConfig(
            from_date=KILL_FROM,
            to_date=KILL_TO,
            stage="KILL",
            window_tag="KILL",
        )
    if stage_u == "CONFIRM":
        return HarnessConfig(
            from_date=CONFIRM_FROM,
            to_date=CONFIRM_TO,
            stage="CONFIRM",
            window_tag="OOS",
        )
    return HarnessConfig(
        from_date=DESIGN_FROM,
        to_date=DESIGN_TO,
        stage="DESIGN",
        window_tag="IS",
    )
