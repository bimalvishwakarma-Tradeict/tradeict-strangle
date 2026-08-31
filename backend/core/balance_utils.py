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
            "available_margin": None,
            "unrealised_pnl": None,
            "free_cash": None,
            "actual_balance_inr": None,
            "available_balance_inr": None,
            "blocked_amount_inr": None,
            "available_margin_inr": None,
            "unrealised_pnl_inr": None,
            "free_cash_inr": None,
            "balance_source": None,
        }
    actual = float(wallet.get("wallet_balance") or wallet.get("balance_usdt") or 0.0)
    blocked = float(wallet.get("position_margin") or 0.0)
    # REST settled cash for bot sizing — not WS available_margin.
    free_cash = float(
        wallet.get("free_cash") or wallet.get("available_balance") or 0.0
    )
    unrealised = float(wallet.get("unrealised_pnl") or 0.0)
    available_margin = float(
        wallet.get("available_margin") or (actual + unrealised - blocked)
    )
    rate = float(usd_inr_rate or 85.0)
    return {
        "actual_balance": round(actual, 4),
        "blocked_amount": round(blocked, 4),
        "available_balance": round(free_cash, 4),
        "available_margin": round(available_margin, 4),
        "unrealised_pnl": round(unrealised, 4),
        "free_cash": round(free_cash, 4),
        "balance_source": str(wallet.get("balance_source") or "rest_computed"),
        "actual_balance_inr": round(actual * rate, 0),
        "available_balance_inr": round(free_cash * rate, 0),
        "blocked_amount_inr": round(blocked * rate, 0),
        "available_margin_inr": round(available_margin * rate, 0),
        "unrealised_pnl_inr": round(unrealised * rate, 0),
        "free_cash_inr": round(free_cash * rate, 0),
    }
