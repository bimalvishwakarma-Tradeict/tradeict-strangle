# test_structure_sl_basis.py — structure-wide SL basis (B4a, log-only)
#
# Run: python -m pytest backend/tests/test_structure_sl_basis.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import TERMINAL_TRADE_STATUSES, TradeStatus
from backend.database import Base
from backend.engine.hedge_lifecycle import (
    _cum_closed_basket_pnl,
    _live_sl_budget_fields,
    _open_basket_gross_mtm,
    _open_basket_net_mtm,
    compute_hedge_sl_budget,
    compute_structure_gross_for_sl,
    hedge_sl_room,
    hedge_sl_should_fire,
)
from backend.models import Account, HedgePosition, Trade


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


def test_sl_basis_excludes_booked_closed_budget_only() -> None:
    """cum_closed = -0.26 tightens budget only — not counted in SL basis (B10)."""
    fixed_sl = 3.0
    cum_closed = -0.26
    budget = compute_hedge_sl_budget(fixed_sl, 25.0, cum_closed)["budget"]
    assert budget == pytest.approx(2.74)

    hedge_net = -2.0
    entry_spread = 0.13
    open_gross = 0.10
    basis = compute_structure_gross_for_sl(
        hedge_net_mtm=hedge_net,
        entry_spread_usd=entry_spread,
        hedge_est_exit_slippage_usd=0.0,
        open_basket_gross_mtm=open_gross,
    )
    assert basis == pytest.approx(hedge_net + entry_spread + open_gross)
    assert basis != pytest.approx(hedge_net + entry_spread + cum_closed + open_gross)
    assert hedge_sl_should_fire(
        budget=budget,
        structure_gross_for_sl=basis,
    ) == (basis <= -budget)


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


@pytest.fixture
def cum_pnl_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    acc = Account(
        name="test-acct",
        api_key_encrypted="enc",
        api_secret_encrypted="enc",
    )
    db.add(acc)
    db.flush()
    hedge = HedgePosition(
        account_id=acc.id,
        underlying="BTC",
        expiry_date=date(2026, 12, 26),
        strike=100000.0,
        quantity=1,
        status="active",
    )
    db.add(hedge)
    db.commit()
    yield db, hedge
    db.close()


def _add_basket_trade(
    db,
    hedge: HedgePosition,
    acc: Account,
    *,
    status: str,
    realized_pnl: float | None,
) -> Trade:
    trade = Trade(
        account_id=acc.id,
        underlying="BTC",
        expiry_date=date(2026, 9, 1),
        status=status,
        entry_time=datetime.now(timezone.utc),
        total_premium_collected=300.0,
        profit_target_usd=50.0,
        stoploss_usd=100.0,
        trigger_mode="flat",
        hedge_position_id=hedge.id,
        realized_pnl=realized_pnl,
    )
    db.add(trade)
    db.commit()
    return trade


def test_terminal_trade_statuses_include_all_exit_paths() -> None:
    assert TradeStatus.CLOSED.value in TERMINAL_TRADE_STATUSES
    assert TradeStatus.EMERGENCY_CLOSED.value in TERMINAL_TRADE_STATUSES
    assert TradeStatus.EXPIRED.value in TERMINAL_TRADE_STATUSES
    assert TradeStatus.ACTIVE.value not in TERMINAL_TRADE_STATUSES


def test_cum_closed_basket_pnl_sums_all_terminal_statuses(cum_pnl_db) -> None:
    db, hedge = cum_pnl_db
    acc = db.query(Account).one()
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.CLOSED.value, realized_pnl=-0.25
    )
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.CLOSED.value, realized_pnl=-0.25
    )
    _add_basket_trade(
        db,
        hedge,
        acc,
        status=TradeStatus.EMERGENCY_CLOSED.value,
        realized_pnl=0.135,
    )
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.EXPIRED.value, realized_pnl=0.10
    )

    cum = _cum_closed_basket_pnl(db, int(hedge.id))
    assert cum == pytest.approx(-0.265)


def test_cum_closed_basket_pnl_excludes_active_basket(cum_pnl_db) -> None:
    db, hedge = cum_pnl_db
    acc = db.query(Account).one()
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.CLOSED.value, realized_pnl=-0.5
    )
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.ACTIVE.value, realized_pnl=999.0
    )

    cum = _cum_closed_basket_pnl(db, int(hedge.id))
    assert cum == pytest.approx(-0.5)


def test_cum_closed_basket_pnl_null_realized_contributes_zero(cum_pnl_db) -> None:
    db, hedge = cum_pnl_db
    acc = db.query(Account).one()
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.CLOSED.value, realized_pnl=0.5
    )
    _add_basket_trade(
        db,
        hedge,
        acc,
        status=TradeStatus.EMERGENCY_CLOSED.value,
        realized_pnl=None,
    )

    cum = _cum_closed_basket_pnl(db, int(hedge.id))
    assert cum == pytest.approx(0.5)


def test_sl_budget_reflects_emergency_and_expired_cum(cum_pnl_db) -> None:
    """Hedge #22 shape: baskets 119+120 closed (-0.5) + 121 emergency (+0.135)."""
    db, hedge = cum_pnl_db
    acc = db.query(Account).one()
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.CLOSED.value, realized_pnl=-0.25
    )
    _add_basket_trade(
        db, hedge, acc, status=TradeStatus.CLOSED.value, realized_pnl=-0.25
    )
    _add_basket_trade(
        db,
        hedge,
        acc,
        status=TradeStatus.EMERGENCY_CLOSED.value,
        realized_pnl=0.135,
    )

    cum = _cum_closed_basket_pnl(db, int(hedge.id))
    assert cum == pytest.approx(-0.365)

    fixed_sl = 2.0
    budget_parts = compute_hedge_sl_budget(fixed_sl, 25.0, cum)
    assert budget_parts["cum_closed"] == pytest.approx(-0.365)
    assert budget_parts["budget"] == pytest.approx(max(0.5, fixed_sl + cum))
