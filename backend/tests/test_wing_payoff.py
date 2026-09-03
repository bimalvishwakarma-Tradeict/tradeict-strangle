# test_wing_payoff.py — Iron-condor expiry payoff (Part C reference cases)
#
# Reference: shorts 81500/74000, wings 84000/71000, net credit 389 pts,
# qty 2, contract_value 0.001
#
# Run: python -m pytest backend/tests/test_wing_payoff.py -q

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.basket_legs import compute_max_loss_usd
from backend.core.payoff import (
    breakevens,
    build_payoff_curve,
    expiry_pnl_usd,
    net_credit_points,
)

SC = 81500.0
SP = 74000.0
WC = 84000.0
WP = 71000.0
NC = 389.0  # premium points
QTY = 2
CV = 0.001


def _usd(spot: float, *, wings: bool = True) -> float:
    return expiry_pnl_usd(
        spot,
        short_call_strike=SC,
        short_put_strike=SP,
        net_credit=NC,
        quantity=QTY,
        wing_call_strike=WC if wings else None,
        wing_put_strike=WP if wings else None,
        contract_value=CV,
    )


def test_mid_zone_equals_net_credit() -> None:
    # 1. Between shorts → max profit = $0.778
    assert _usd(77800) == pytest.approx(0.778)


def test_far_above_wing_call_capped() -> None:
    # 2. Call side capped → −$4.222
    assert _usd(90000) == pytest.approx(-4.222)


def test_far_below_wing_put_capped() -> None:
    # 3. Put side capped (worse) → −$5.222
    assert _usd(60000) == pytest.approx(-5.222)


def test_upper_breakeven_zero() -> None:
    # 4. upper BE = 81500 + 389 = 81889
    assert _usd(81889) == pytest.approx(0.0, abs=1e-9)


def test_lower_breakeven_zero() -> None:
    # 5. lower BE = 74000 − 389 = 73611
    assert _usd(73611) == pytest.approx(0.0, abs=1e-9)


def test_capped_flat_beyond_wing_call() -> None:
    # 6. 90k and 200k same (capped, does not keep sloping)
    assert _usd(90000) == pytest.approx(_usd(200000))


def test_wings_off_unbounded_at_90k() -> None:
    # 7. Wings off → steeper loss than capped condor at 90k (keeps sloping)
    naked = _usd(90000, wings=False)
    capped = _usd(90000, wings=True)
    assert naked < capped
    # pts = 389 − 8500 = −8111 → USD = −8111 × 2 × 0.001
    assert naked == pytest.approx(-16.222)
    assert _usd(200000, wings=False) < naked


def test_graph_max_loss_matches_active_formula() -> None:
    # 8. Graph max_loss matches compute_max_loss_usd (/active)
    # Reconstruct premiums so net credit = 389 pts
    # Use sc=250, sp=250, wc=55.5, wp=55.5 → 500 - 111 = 389
    sc_p, sp_p, wc_p, wp_p = 250.0, 250.0, 55.5, 55.5
    assert net_credit_points(
        short_call_premium=sc_p,
        short_put_premium=sp_p,
        wing_call_premium=wc_p,
        wing_put_premium=wp_p,
    ) == pytest.approx(389.0)

    curve = build_payoff_curve(
        current_price=77800,
        short_call_strike=SC,
        short_put_strike=SP,
        short_call_premium=sc_p,
        short_put_premium=sp_p,
        quantity=QTY,
        wing_call_strike=WC,
        wing_put_strike=WP,
        wing_call_premium=wc_p,
        wing_put_premium=wp_p,
        contract_value=CV,
    )
    max_profit = NC * QTY * CV  # 0.778
    active_ml = compute_max_loss_usd(
        short_call_strike=SC,
        short_put_strike=SP,
        wing_call_strike=WC,
        wing_put_strike=WP,
        net_credit_usd=max_profit,
        quantity=QTY,
        contract_value=CV,
    )
    assert curve["max_loss_usd"] == pytest.approx(active_ml)
    assert curve["max_loss_usd"] == pytest.approx(5.222)
    assert curve["max_profit_usd"] == pytest.approx(0.778)
    assert curve["wings_on"] is True
    be_u, be_l = breakevens(
        short_call_strike=SC,
        short_put_strike=SP,
        net_credit_per_lot=NC,
    )
    assert be_u == pytest.approx(81889)
    assert be_l == pytest.approx(73611)
    assert curve["breakeven_upper"] == pytest.approx(81889)
    assert curve["breakeven_lower"] == pytest.approx(73611)


def test_wings_off_max_loss_none() -> None:
    curve = build_payoff_curve(
        current_price=77800,
        short_call_strike=SC,
        short_put_strike=SP,
        short_call_premium=200.0,
        short_put_premium=189.0,
        quantity=QTY,
        contract_value=CV,
    )
    assert curve["wings_on"] is False
    assert curve["max_loss_usd"] is None
