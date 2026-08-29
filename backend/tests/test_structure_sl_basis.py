# test_structure_sl_basis.py — structure-wide SL basis (B4a, log-only)
#
# Run: python -m pytest backend/tests/test_structure_sl_basis.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.hedge_lifecycle import (
    _open_basket_gross_mtm,
    _open_basket_net_mtm,
    compute_structure_gross_for_sl,
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
