# trade_side.py — Single helper for master-trade long vs short

from __future__ import annotations

from typing import Any


def is_long_trade(trade: Any) -> bool:
    """True when Trade.position_side is the string 'long'."""
    side = getattr(trade, "position_side", None)
    if not isinstance(side, str):
        return False
    return side.strip().lower() == "long"


def is_adjustment_enabled(trade: Any) -> bool:
    """False only when adjustment_enabled is explicitly False; else True."""
    val = getattr(trade, "adjustment_enabled", True)
    return val is not False
