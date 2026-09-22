"""Package init for S008 regime gate."""

from backtest.strategies.s008_regime_gate.strategy import (
    IS_FROM,
    IS_TO,
    OOS_FROM,
    OOS_TO,
    S008RegimeGateStrategy,
)

__all__ = [
    "IS_FROM",
    "IS_TO",
    "OOS_FROM",
    "OOS_TO",
    "S008RegimeGateStrategy",
]
