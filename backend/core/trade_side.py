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


def long_trade_public_fields(trade: Any) -> dict[str, Any]:
    """Fields pushed on TRADE_UPDATE / INITIAL_STATE / /api/trade/active."""
    adj = getattr(trade, "adjustment_enabled", True)
    return {
        "position_side": str(getattr(trade, "position_side", None) or "short"),
        "target_usd": getattr(trade, "target_usd", None),
        "sl_usd": getattr(trade, "sl_usd", None),
        "trail_step_usd": getattr(trade, "trail_step_usd", None),
        "trail_amt_usd": getattr(trade, "trail_amt_usd", None),
        "peak_gross_mtm": getattr(trade, "peak_gross_mtm", None),
        "trail_sl_level": getattr(trade, "trail_sl_level", None),
        "adjustment_enabled": False if adj is False else bool(adj if adj is not None else True),
    }
