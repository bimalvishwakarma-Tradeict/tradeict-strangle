# test_wing_select.py — pure wing strike selection (no DB / network)
#
# Run: python -m pytest backend/tests/test_wing_select.py -q

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.strategies.s001_short_strangle.wing_select import (
    normalize_wing_mode,
    resolve_wing_strikes,
)


def _chain() -> list[dict]:
    """Synthetic chain around the user's reference trade."""
    rows = []
    # Puts below 74k, calls above 81500, plus ATM-ish
    for strike in range(69000, 86000, 500):
        otm_call = max(0.0, 81500 - strike)
        otm_put = max(0.0, strike - 74000)
        # Rough premium / delta so modes have something to pick
        call_prem = max(5.0, 357 - otm_call * 0.08)
        put_prem = max(5.0, 285 - otm_put * 0.06)
        call_delta = max(0.01, min(0.99, 0.50 - (strike - 77799) / 20000))
        put_delta = max(0.01, min(0.99, 0.50 + (strike - 77799) / 20000))
        # Tune known targets for assertions
        if strike == 83500:
            call_prem = 180.0
            call_delta = 0.09
        if strike == 84000:
            call_prem = 157.0
            call_delta = 0.06
        if strike == 84500:
            call_prem = 140.0
            call_delta = 0.045
        if strike == 72000:
            put_prem = 120.0
            put_delta = 0.09
        if strike == 71000:
            put_prem = 96.0
            put_delta = 0.06
        if strike == 70500:
            put_prem = 88.0
            put_delta = 0.045
        if strike == 81500:
            call_prem = 357.0
            call_delta = 0.28
        if strike == 74000:
            put_prem = 285.0
            put_delta = 0.26
        rows.append(
            {
                "strike": float(strike),
                "call_symbol": f"C-{strike}",
                "call_product_id": 1000 + strike // 100,
                "call_mark_price": call_prem,
                "call_delta": call_delta,
                "put_symbol": f"P-{strike}",
                "put_product_id": 2000 + strike // 100,
                "put_mark_price": put_prem,
                "put_delta": put_delta,
            }
        )
    return rows


def test_points_mode_call_picks_at_or_beyond_target() -> None:
    wing_c, _wing_p = resolve_wing_strikes(
        chain=_chain(),
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode="points",
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    assert wing_c is not None
    assert wing_c["strike"] >= 83500
    assert wing_c["strike"] > 81500
    assert wing_c["picked_by"] in {"points", "chain_end"}


def test_points_mode_put_picks_at_or_beyond_target() -> None:
    _wing_c, wing_p = resolve_wing_strikes(
        chain=_chain(),
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode="points",
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    assert wing_p is not None
    assert wing_p["strike"] <= 72000
    assert wing_p["strike"] < 74000


def test_delta_mode_picks_inside_band() -> None:
    wing_c, wing_p = resolve_wing_strikes(
        chain=_chain(),
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode="delta",
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    assert wing_c is not None
    assert wing_p is not None
    assert 0.05 <= abs(wing_c["delta"]) <= 0.07
    assert 0.05 <= abs(wing_p["delta"]) <= 0.07
    assert wing_c["picked_by"] == "delta_band"
    assert wing_p["picked_by"] == "delta_band"


def test_delta_mode_band_miss_nearest_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Narrow impossible band — force nearest outside
    with caplog.at_level(logging.WARNING):
        wing_c, wing_p = resolve_wing_strikes(
            chain=_chain(),
            short_call_strike=81500,
            short_put_strike=74000,
            short_call_premium=357,
            short_put_premium=285,
            mode="delta",
            points_away=2000,
            delta_min=0.001,
            delta_max=0.002,
            pct_of_premium=20,
        )
    assert wing_c is not None
    assert wing_p is not None
    assert wing_c["picked_by"] == "delta_nearest"
    assert wing_p["picked_by"] == "delta_nearest"
    assert any("delta band miss" in r.message for r in caplog.records)


def test_pct_mode_nearest_premium_to_target() -> None:
    # 20% of 357 = 71.4 — chain has ~88 at 70500 put side; call side ~140 at 84500
    # Use a denser premium ladder via dedicated mini-chain
    chain = [
        {
            "strike": 81500.0,
            "call_mark_price": 357.0,
            "call_delta": 0.3,
            "call_symbol": "C-81500",
            "call_product_id": 1,
            "put_mark_price": 10.0,
            "put_delta": 0.01,
            "put_symbol": "P-81500",
            "put_product_id": 2,
        },
        {
            "strike": 83000.0,
            "call_mark_price": 90.0,
            "call_delta": 0.12,
            "call_symbol": "C-83000",
            "call_product_id": 3,
            "put_mark_price": 5.0,
            "put_delta": 0.01,
            "put_symbol": "P-83000",
            "put_product_id": 4,
        },
        {
            "strike": 84000.0,
            "call_mark_price": 71.0,
            "call_delta": 0.08,
            "call_symbol": "C-84000",
            "call_product_id": 5,
            "put_mark_price": 3.0,
            "put_delta": 0.01,
            "put_symbol": "P-84000",
            "put_product_id": 6,
        },
        {
            "strike": 85000.0,
            "call_mark_price": 40.0,
            "call_delta": 0.04,
            "call_symbol": "C-85000",
            "call_product_id": 7,
            "put_mark_price": 2.0,
            "put_delta": 0.01,
            "put_symbol": "P-85000",
            "put_product_id": 8,
        },
        {
            "strike": 74000.0,
            "call_mark_price": 10.0,
            "call_delta": 0.01,
            "call_symbol": "C-74000",
            "call_product_id": 9,
            "put_mark_price": 285.0,
            "put_delta": 0.3,
            "put_symbol": "P-74000",
            "put_product_id": 10,
        },
        {
            "strike": 72000.0,
            "call_mark_price": 5.0,
            "call_delta": 0.01,
            "call_symbol": "C-72000",
            "call_product_id": 11,
            "put_mark_price": 100.0,
            "put_delta": 0.12,
            "put_symbol": "P-72000",
            "put_product_id": 12,
        },
        {
            "strike": 71000.0,
            "call_mark_price": 4.0,
            "call_delta": 0.01,
            "call_symbol": "C-71000",
            "call_product_id": 13,
            "put_mark_price": 71.4,
            "put_delta": 0.08,
            "put_symbol": "P-71000",
            "put_product_id": 14,
        },
        {
            "strike": 70000.0,
            "call_mark_price": 3.0,
            "call_delta": 0.01,
            "call_symbol": "C-70000",
            "call_product_id": 15,
            "put_mark_price": 40.0,
            "put_delta": 0.04,
            "put_symbol": "P-70000",
            "put_product_id": 16,
        },
    ]
    wing_c, wing_p = resolve_wing_strikes(
        chain=chain,
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode="pct_of_premium",
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    assert wing_c is not None
    assert wing_p is not None
    # 20% of 357 ≈ 71.4 → 84000 @ 71
    assert wing_c["strike"] == 84000.0
    assert abs(wing_c["premium"] - 71.0) < 0.01
    # 20% of 285 = 57 — nearest among 100/71.4/40 is 71.4 at 71000
    assert wing_p["strike"] == 71000.0


@pytest.mark.parametrize("mode", ["points", "delta", "pct_of_premium"])
def test_wing_never_at_or_inside_short(mode: str) -> None:
    wing_c, wing_p = resolve_wing_strikes(
        chain=_chain(),
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode=mode,
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    if wing_c is not None:
        assert wing_c["strike"] > 81500
    if wing_p is not None:
        assert wing_p["strike"] < 74000


def test_chain_end_logs_and_picks_farthest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Only one strike beyond short, closer than points_away
    chain = [
        {
            "strike": 81500.0,
            "call_mark_price": 357.0,
            "call_delta": 0.3,
            "call_symbol": "C-81500",
            "call_product_id": 1,
            "put_mark_price": 10.0,
            "put_delta": 0.01,
            "put_symbol": "P-81500",
            "put_product_id": 2,
        },
        {
            "strike": 82000.0,
            "call_mark_price": 300.0,
            "call_delta": 0.25,
            "call_symbol": "C-82000",
            "call_product_id": 3,
            "put_mark_price": 8.0,
            "put_delta": 0.01,
            "put_symbol": "P-82000",
            "put_product_id": 4,
        },
        {
            "strike": 74000.0,
            "call_mark_price": 10.0,
            "call_delta": 0.01,
            "call_symbol": "C-74000",
            "call_product_id": 5,
            "put_mark_price": 285.0,
            "put_delta": 0.3,
            "put_symbol": "P-74000",
            "put_product_id": 6,
        },
        {
            "strike": 73500.0,
            "call_mark_price": 8.0,
            "call_delta": 0.01,
            "call_symbol": "C-73500",
            "call_product_id": 7,
            "put_mark_price": 250.0,
            "put_delta": 0.25,
            "put_symbol": "P-73500",
            "put_product_id": 8,
        },
    ]
    with caplog.at_level(logging.WARNING):
        wing_c, wing_p = resolve_wing_strikes(
            chain=chain,
            short_call_strike=81500,
            short_put_strike=74000,
            short_call_premium=357,
            short_put_premium=285,
            mode="points",
            points_away=5000,
            delta_min=0.05,
            delta_max=0.07,
            pct_of_premium=20,
        )
    assert wing_c is not None and wing_c["strike"] == 82000.0
    assert wing_p is not None and wing_p["strike"] == 73500.0
    assert wing_c["picked_by"] == "chain_end"
    assert wing_p["picked_by"] == "chain_end"
    assert any("WING_SELECT_CHAIN_END" in r.message for r in caplog.records)


def test_no_strike_beyond_short_returns_none() -> None:
    chain = [
        {
            "strike": 81500.0,
            "call_mark_price": 357.0,
            "call_delta": 0.3,
            "call_symbol": "C-81500",
            "call_product_id": 1,
            "put_mark_price": 10.0,
            "put_delta": 0.01,
            "put_symbol": "P-81500",
            "put_product_id": 2,
        },
        {
            "strike": 74000.0,
            "call_mark_price": 10.0,
            "call_delta": 0.01,
            "call_symbol": "C-74000",
            "call_product_id": 3,
            "put_mark_price": 285.0,
            "put_delta": 0.3,
            "put_symbol": "P-74000",
            "put_product_id": 4,
        },
    ]
    wing_c, wing_p = resolve_wing_strikes(
        chain=chain,
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode="points",
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    assert wing_c is None
    assert wing_p is None


def test_invalid_mode_defaults_to_points() -> None:
    assert normalize_wing_mode("nope") == "points"
    assert normalize_wing_mode(None) == "points"
    wing_c, wing_p = resolve_wing_strikes(
        chain=_chain(),
        short_call_strike=81500,
        short_put_strike=74000,
        short_call_premium=357,
        short_put_premium=285,
        mode="garbage",
        points_away=2000,
        delta_min=0.05,
        delta_max=0.07,
        pct_of_premium=20,
    )
    assert wing_c is not None and wing_c["strike"] >= 83500
    assert wing_p is not None and wing_p["strike"] <= 72000
