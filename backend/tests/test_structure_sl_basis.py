# test_structure_sl_basis.py — structure-wide SL basis (B4a, log-only)
#
# Run: python -m pytest backend/tests/test_structure_sl_basis.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.hedge_lifecycle import (
    _live_sl_budget_fields,
    _open_basket_gross_mtm,
    _open_basket_net_mtm,
    compute_structure_gross_for_sl,
    hedge_sl_room,
    hedge_sl_should_fire,
)


def test_structure_gross_for_sl_live_example() -> None:
    result = compute_structure_gross_for_sl(
        hedge_net_mtm=-1.854735,
        entry_spread_usd=0.13,
        hedge_est_exit_slippage_usd=0.176,
        open_basket_gross_mtm=0.20,
    )
    assert result == pytest.approx(0.20 + (-1.854735 + 0.13 + 0.176), rel=1e-6)
    assert result == pytest.approx(-1.348735, rel=1e-6)


def test_structure_gross_for_sl_zero_exit_slip_equals_hedge_gross_plus_open_basket() -> None:
    hedge_net = -2.5
    entry_spread = 0.4
    open_gross = 0.75
    hedge_gross = hedge_net + entry_spread
    result = compute_structure_gross_for_sl(
        hedge_net_mtm=hedge_net,
        entry_spread_usd=entry_spread,
        hedge_est_exit_slippage_usd=0.0,
        open_basket_gross_mtm=open_gross,
    )
    assert result == pytest.approx(hedge_gross + open_gross, rel=1e-9)


def test_structure_gross_for_sl_no_open_baskets() -> None:
    result = compute_structure_gross_for_sl(
        hedge_net_mtm=-1.0,
        entry_spread_usd=0.2,
        hedge_est_exit_slippage_usd=0.15,
        open_basket_gross_mtm=0.0,
    )
    assert result == pytest.approx(-0.65, rel=1e-9)


def test_open_basket_gross_uses_last_pnl_not_last_net_mtm() -> None:
    trade = SimpleNamespace(id=101)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [trade]

    state = SimpleNamespace(last_pnl=1.25, last_net_mtm=-3.50)
    tracker = MagicMock()
    tracker.get.return_value = state

    gross = _open_basket_gross_mtm(db, hedge_id=20, position_tracker=tracker)
    net = _open_basket_net_mtm(db, hedge_id=20, position_tracker=tracker)

    assert gross == 1.25
    assert net == -3.50
    db.query.return_value.filter.assert_called()


def test_open_basket_gross_returns_zero_without_tracker() -> None:
    db = MagicMock()
    assert _open_basket_gross_mtm(db, hedge_id=1, position_tracker=None) == 0.0
    db.query.assert_not_called()


def test_sl_fires_only_when_structure_room_non_positive() -> None:
    budget = 1.8664
    structure = -1.337502
    room = hedge_sl_room(budget, structure)
    assert room == pytest.approx(0.528898, rel=1e-4)
    assert room > 0
    assert hedge_sl_should_fire(
        budget=budget,
        structure_gross_for_sl=structure,
    ) is False
    assert hedge_sl_should_fire(
        budget=budget,
        structure_gross_for_sl=-budget,
    ) is True
    assert hedge_sl_should_fire(
        budget=budget,
        structure_gross_for_sl=-(budget + 0.01),
    ) is True


def test_old_basis_fires_but_structure_basis_does_not() -> None:
    """Live Hedge#20 shape: old rule near stop, structure rule has more room."""
    budget = 1.8664
    gross_for_sl = -1.9
    structure_gross_for_sl = -1.5
    room_old = hedge_sl_room(budget, gross_for_sl)
    room = hedge_sl_room(budget, structure_gross_for_sl)
    assert room_old <= 0
    assert room > 0
    assert hedge_sl_should_fire(
        budget=budget,
        structure_gross_for_sl=structure_gross_for_sl,
    ) is False
    assert room_old <= 0  # old hedge-only basis would have fired


def test_both_bases_fire_when_structure_deeply_negative() -> None:
    budget = 1.8664
    gross_for_sl = -2.5
    structure_gross_for_sl = -2.2
    assert hedge_sl_room(budget, gross_for_sl) <= 0
    assert hedge_sl_room(budget, structure_gross_for_sl) <= 0
    assert hedge_sl_should_fire(
        budget=budget,
        structure_gross_for_sl=structure_gross_for_sl,
    ) is True


def test_live_sl_budget_fields_zero_budget_pct_to_stop_safe() -> None:
    hedge = SimpleNamespace(
        cum_closed_basket_pnl=-5.0,
        hedge_gross_for_sl=-1.0,
        structure_gross_for_sl=-0.8,
    )
    db = MagicMock()
    with patch(
        "backend.database.get_or_create_auto_settings",
        return_value=SimpleNamespace(
            hedge_fixed_sl_usd=0.0,
            hedge_sl_floor_pct=25.0,
        ),
    ):
        fields = _live_sl_budget_fields(db, hedge)  # type: ignore[arg-type]
    assert fields["sl_budget"] == 0.0
    assert fields["pct_to_stop"] == 0.0
    assert fields["sl_basis_usd"] == -0.8
    assert fields["hedge_only_for_sl"] == -1.0
