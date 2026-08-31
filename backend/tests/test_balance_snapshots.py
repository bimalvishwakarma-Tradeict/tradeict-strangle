"""Tests for balance snapshot helpers (B14)."""

from __future__ import annotations

from backend.core.balance_utils import compute_daily_growth_pct, wallet_to_balance_fields


def test_compute_daily_growth_pct_positive() -> None:
    assert compute_daily_growth_pct(10.0, 9.0) == 11.11


def test_compute_daily_growth_pct_negative() -> None:
    assert compute_daily_growth_pct(8.77, 9.5) == -7.68


def test_compute_daily_growth_pct_no_yesterday() -> None:
    assert compute_daily_growth_pct(8.77, None) is None
    assert compute_daily_growth_pct(8.77, 0.0) is None


def test_wallet_to_balance_fields_mapping() -> None:
    out = wallet_to_balance_fields(
        {
            "wallet_balance": 8.77,
            "position_margin": 1.75,
            "available_balance": 45.99,
        },
        usd_inr_rate=85.0,
    )
    assert out["actual_balance"] == 8.77
    assert out["blocked_amount"] == 1.75
    assert out["available_balance"] == 45.99
    assert out["actual_balance_inr"] == 745.0
