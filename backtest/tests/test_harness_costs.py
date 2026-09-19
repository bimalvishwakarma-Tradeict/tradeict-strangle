"""Unit tests for harness costs helpers."""

from __future__ import annotations

from backtest.harness.costs import fill_price, option_fee, qty_btc, slip_frac
from backtest.harness.metrics import lots_at_risk_cap


def test_slip_frac_flat_mult() -> None:
    s = slip_frac(200.0, 2, slip_model="flat165", slip_mult=1.5)
    assert abs(s - 0.0165 * 1.5) < 1e-9


def test_fill_price_sell_worse() -> None:
    fill, sf = fill_price(100.0, "sell", dte=2, slip_model="flat165", slip_mult=1.0)
    assert sf > 0
    assert fill < 100.0


def test_fill_price_buy_worse() -> None:
    fill, sf = fill_price(100.0, "buy", dte=2, slip_model="flat165", slip_mult=1.0)
    assert fill > 100.0


def test_option_fee_positive() -> None:
    fee = option_fee(150.0, 100_000.0, 8)
    assert fee > 0


def test_qty_btc() -> None:
    assert abs(qty_btc(8) - 0.008) < 1e-12


def test_lots_at_risk_cap() -> None:
    # $3 budget, $1.5 max loss → 2 lots
    assert lots_at_risk_cap(1.5, capital=100.0, cap_pct=3.0) == 2
