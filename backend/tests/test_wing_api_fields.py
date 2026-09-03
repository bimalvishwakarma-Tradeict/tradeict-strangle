# test_wing_api_fields.py — wing aggregates for /active + TRADE_UPDATE
#
# Run: python -m pytest backend/tests/test_wing_api_fields.py -q

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.basket_legs import (
    build_wing_credit_fields,
    compute_max_loss_usd,
    compute_net_credit_usd,
)

CV = float(OPTIONS_CONTRACT_VALUE)


def test_max_loss_iron_condor_formula() -> None:
    # shorts 81500/74000, wings 84000/71000 → widths 2500/3000 → max width 3000
    # net credit 0.389 → max loss = 3000*0.001 - 0.389 = 2.611
    net = 0.389
    ml = compute_max_loss_usd(
        short_call_strike=81500,
        short_put_strike=74000,
        wing_call_strike=84000,
        wing_put_strike=71000,
        net_credit_usd=net,
        quantity=1,
    )
    assert ml == pytest.approx(3000 * CV - net)


def test_max_loss_none_without_wings() -> None:
    assert (
        compute_max_loss_usd(
            short_call_strike=81500,
            short_put_strike=74000,
            wing_call_strike=None,
            wing_put_strike=71000,
            net_credit_usd=0.4,
            quantity=1,
        )
        is None
    )


def test_build_wing_credit_fields() -> None:
    trade = SimpleNamespace(
        initial_max_profit=0.389,
        wing_premium_paid_usd=0.253,
    )
    sc = SimpleNamespace(initial_premium=357.0, quantity=1, strike=81500)
    sp = SimpleNamespace(initial_premium=285.0, quantity=1, strike=74000)
    wc = SimpleNamespace(initial_premium=157.0, quantity=1, strike=84000)
    wp = SimpleNamespace(initial_premium=96.0, quantity=1, strike=71000)
    fields = build_wing_credit_fields(
        trade=trade,
        short_call=sc,
        short_put=sp,
        wing_call=wc,
        wing_put=wp,
        call_premium=340.0,
        put_premium=270.0,
        wing_call_premium=160.0,
        wing_put_premium=100.0,
    )
    assert fields["wings_present"] is True
    assert fields["net_credit_entry"] == pytest.approx(0.389)
    assert fields["wing_premium_paid_usd"] == pytest.approx(0.253)
    now = compute_net_credit_usd(
        short_call_premium=340.0,
        short_put_premium=270.0,
        short_qty=1,
        wing_call_premium=160.0,
        wing_put_premium=100.0,
    )
    assert fields["net_credit_now"] == pytest.approx(now)
    assert fields["max_loss_usd"] == pytest.approx(3000 * CV - 0.389)


def test_build_wing_credit_fields_no_wings_regression() -> None:
    trade = SimpleNamespace(initial_max_profit=0.642, wing_premium_paid_usd=None)
    sc = SimpleNamespace(initial_premium=357.0, quantity=1, strike=81500)
    sp = SimpleNamespace(initial_premium=285.0, quantity=1, strike=74000)
    fields = build_wing_credit_fields(
        trade=trade,
        short_call=sc,
        short_put=sp,
        wing_call=None,
        wing_put=None,
        call_premium=357.0,
        put_premium=285.0,
    )
    assert fields["wings_present"] is False
    assert fields["wing_premium_paid_usd"] is None
    assert fields["max_loss_usd"] is None
    assert fields["net_credit_entry"] == pytest.approx(0.642)
