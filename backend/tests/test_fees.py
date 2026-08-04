# test_fees.py — Delta India options fee estimate unit tests

from __future__ import annotations

from backend.core.fees import estimate_option_trading_fee


def test_estimate_matches_delta_inc_gst_example() -> None:
    """
    Fill: 10 lots @ 391, BTC spot 63826.1
    Delta commission (inc GST) ≈ 0.0753148
    """
    fee = estimate_option_trading_fee(
        option_price=391.0,
        quantity_lots=10,
        btc_index_price=63826.1,
    )
    assert abs(fee - 0.0753148) < 1e-6


def test_premium_cap_applies_when_cheaper() -> None:
    # Deep OTM: premium tiny → cap binds
    fee = estimate_option_trading_fee(
        option_price=1.0,
        quantity_lots=10,
        btc_index_price=100_000.0,
    )
    # notional = 100000*0.01*0.0001=0.1; cap=1*0.01*0.035=0.00035; +GST
    assert abs(fee - 0.00035 * 1.18) < 1e-9
