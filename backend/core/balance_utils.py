"""Pure balance field helpers (no DB / SQLAlchemy deps)."""

from __future__ import annotations

from typing import Any


def compute_daily_growth_pct(
    actual_balance: float | None,
    yesterday_balance: float | None,
) -> float | None:
    if actual_balance is None or yesterday_balance is None:
        return None
    if yesterday_balance == 0:
        return None
    return round(
        (float(actual_balance) - float(yesterday_balance))
        / float(yesterday_balance)
        * 100.0,
        2,
    )


def wallet_to_balance_fields(
    wallet: dict[str, float] | None,
    *,
    usd_inr_rate: float,
) -> dict[str, Any]:
    if not wallet:
        return {
            "actual_balance": None,
            "blocked_amount": None,
            "available_balance": None,
            "actual_balance_inr": None,
            "available_balance_inr": None,
            "blocked_amount_inr": None,
        }
    actual = float(wallet.get("wallet_balance") or wallet.get("balance_usdt") or 0.0)
    blocked = float(wallet.get("position_margin") or 0.0)
    available = float(wallet.get("available_balance") or 0.0)
    rate = float(usd_inr_rate or 85.0)
    return {
        "actual_balance": round(actual, 4),
        "blocked_amount": round(blocked, 4),
        "available_balance": round(available, 4),
        "actual_balance_inr": round(actual * rate, 0),
        "available_balance_inr": round(available * rate, 0),
        "blocked_amount_inr": round(blocked * rate, 0),
    }
