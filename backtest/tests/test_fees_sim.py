# Unit tests for backtest/fees_sim.py — hand-calculated expected values

from __future__ import annotations

from backtest.fees_sim import (
    OPTION_FEE_RATE,
    OPTIONS_CONTRACT_VALUE,
    PREMIUM_CAP_RATE,
    estimate_option_fee,
)


def test_fee_premium_cap_binds() -> None:
    # index=100_000, qty=1, premium=200
    # notional = 100000 * 1 * 0.001 * 0.00010 = 0.01
    # cap      = 200 * 1 * 0.001 * 0.035     = 0.007
    # fee = min = 0.007
    fee = estimate_option_fee(premium=200.0, qty_lots=1, btc_index=100_000.0)
    assert abs(fee - 0.007) < 1e-12


def test_fee_notional_binds() -> None:
    # index=100_000, qty=1, premium=5000
    # notional = 0.01
    # cap      = 5000 * 1 * 0.001 * 0.035 = 0.175
    # fee = min = 0.01
    fee = estimate_option_fee(premium=5000.0, qty_lots=1, btc_index=100_000.0)
    assert abs(fee - 0.01) < 1e-12


def test_fee_scales_with_lots() -> None:
    # index=90_000, qty=100, premium=150
    # notional = 90000 * 100 * 0.001 * 0.00010 = 0.9
    # cap      = 150 * 100 * 0.001 * 0.035     = 0.525
    # fee = 0.525
    fee = estimate_option_fee(premium=150.0, qty_lots=100, btc_index=90_000.0)
    assert abs(fee - 0.525) < 1e-12


def test_fee_zero_on_bad_inputs() -> None:
    assert estimate_option_fee(premium=0, qty_lots=1, btc_index=100_000) == 0.0
    assert estimate_option_fee(premium=100, qty_lots=0, btc_index=100_000) == 0.0
    assert estimate_option_fee(premium=100, qty_lots=1, btc_index=0) == 0.0


def test_constants_match_copied_live_defaults() -> None:
    assert OPTIONS_CONTRACT_VALUE == 0.001
    assert OPTION_FEE_RATE == 0.00010
    assert PREMIUM_CAP_RATE == 0.035
