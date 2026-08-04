# chain_utils.py — Shared option-chain helpers (ATM tagging, etc.)

from __future__ import annotations

from typing import Any


def annotate_atm(chain: list[dict[str, Any]], current_price: float) -> float | None:
    """
    Mark the strike nearest to current_price with atm=True.

    Returns the ATM strike, or None if chain empty / invalid price.
    """
    if not chain or current_price <= 0:
        for row in chain:
            row["atm"] = False
        return None
    atm_strike = min(
        (float(row["strike"]) for row in chain),
        key=lambda s: abs(s - current_price),
    )
    for row in chain:
        row["atm"] = float(row["strike"]) == atm_strike
    return atm_strike
