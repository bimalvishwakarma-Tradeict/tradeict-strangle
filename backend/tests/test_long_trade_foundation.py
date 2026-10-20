# test_long_trade_foundation.py — position_side / guards; shorts unchanged
#
# Run: python -m pytest backend/tests/test_long_trade_foundation.py -q

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.delta_client import compute_signed_upnl
from backend.core.trade_side import is_long_trade
from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy

CV = float(OPTIONS_CONTRACT_VALUE)


def pytest_approx(a: float, b: float, eps: float = 1e-12) -> bool:
    return abs(float(a) - float(b)) <= eps


def _leg(*, entry: float, is_long: bool, qty: int = 1, status: str = "open"):
    return SimpleNamespace(
        id=1,
        leg_type="call",
        initial_premium=entry,
        quantity=qty,
        status=status,
        is_long=is_long,
        entry_fee_usd=0.0,
        exit_fee_usd=0.0,
        entry_spread_usd=0.0,
        symbol="C-TEST",
        product_id=1,
        trigger_baseline_premium=entry,
        trigger_premium=entry,
    )


def _trade(**kwargs):
    base = dict(
        id=7,
        profit_target_usd=10_000.0,
        stoploss_usd=10_000.0,
        trigger_mode="flat",
        combined_trigger_mode=False,
        expiry_date=date.today() + timedelta(days=2),
        monitoring_starts_at=None,
        adjust_settling_until=None,
        slippage_pct=0.0,
        entry_spread_for_sl_usd=0.0,
        position_side="short",
        adjustment_enabled=True,
        is_demo=False,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_long_leg_pnl_opposite_of_short() -> None:
    strat = ShortStrangleStrategy()
    trade = _trade()
    long_leg = _leg(entry=100.0, is_long=True)
    short_leg = _leg(entry=100.0, is_long=False)
    put = _leg(entry=100.0, is_long=False)
    put.status = "closed"
    long_pnl = strat.calculate_pnl(trade, long_leg, put, 150.0, 100.0)
    short_pnl = strat.calculate_pnl(trade, short_leg, put, 150.0, 100.0)
    expect_long = compute_signed_upnl(100.0, 150.0, size=+1, contract_value=CV)
    expect_short = compute_signed_upnl(100.0, 150.0, size=-1, contract_value=CV)
    assert long_pnl > 0
    assert pytest_approx(long_pnl, expect_long)
    assert pytest_approx(short_pnl, expect_short)
    assert pytest_approx(long_pnl, -short_pnl)


def test_short_call_put_pnl_unchanged_vs_negative_size() -> None:
    strat = ShortStrangleStrategy()
    trade = _trade()
    call = _leg(entry=357.0, is_long=False)
    put = _leg(entry=285.0, is_long=False)
    got = strat.calculate_pnl(trade, call, put, 300.0, 250.0)
    expect = (
        compute_signed_upnl(357.0, 300.0, size=-1, contract_value=CV)
        + compute_signed_upnl(285.0, 250.0, size=-1, contract_value=CV)
    )
    assert pytest_approx(got, expect)


def _bot_engine():
    from backend.engine.bot_engine import BotEngine

    @contextmanager
    def db_factory():
        yield MagicMock()

    client = MagicMock()
    client.verify_position_exists = AsyncMock(return_value=True)
    client.get_positions = AsyncMock(return_value=[{"product_id": 1, "size": -1}])
    engine = BotEngine(
        db_factory=db_factory,
        strategy=MagicMock(),
        position_tracker=MagicMock(),
        order_executor=MagicMock(),
        adjustment_executor=MagicMock(),
        delta_client=client,
    )
    engine.position_tracker.get.return_value = None
    engine.order_executor.buy_option = AsyncMock()
    engine.order_executor.sell_option = AsyncMock()
    return engine, client


def test_long_trade_integrity_no_alert_no_order() -> None:
    engine, client = _bot_engine()
    state = MagicMock()
    state.trade_id = 9
    state.is_adjusting = False
    state.trade = _trade(id=9, position_side="long")
    state.call_leg = _leg(entry=100.0, is_long=True)
    state.put_leg = _leg(entry=100.0, is_long=True)
    status = asyncio.run(engine._check_position_integrity(state))
    assert status == "ok"
    client.verify_position_exists.assert_not_called()
    engine.order_executor.buy_option.assert_not_called()
    engine.order_executor.sell_option.assert_not_called()


def test_long_trade_leg_recovery_places_no_order() -> None:
    engine, client = _bot_engine()
    state = MagicMock()
    state.trade_id = 11
    state.is_adjusting = False
    state.trade = _trade(id=11, position_side="long")
    asyncio.run(engine._maybe_recover_legs(state))
    client.get_positions.assert_not_called()
    engine.order_executor.buy_option.assert_not_called()
    engine.order_executor.sell_option.assert_not_called()


def test_mirror_trade_entry_skips_long_master() -> None:
    from backend.engine.mirror_engine import MirrorEngine

    @contextmanager
    def db_factory():
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = _trade(
            id=3, position_side="long"
        )
        yield db

    engine = MirrorEngine(db_factory=db_factory)
    with patch(
        "backend.engine.mirror_engine.get_active_slave_accounts",
        return_value=[MagicMock()],
    ) as slaves:
        asyncio.run(
            engine.mirror_trade_entry(
                3, 1, 2, 1, 1, 100.0, 90.0, "C", "P", 10.0, 10.0,
                date.today(), "BTC",
            )
        )
        slaves.assert_not_called()


def test_pre_expiry_skips_long_exits_short() -> None:
    strat = ShortStrangleStrategy()
    call = _leg(entry=100.0, is_long=False)
    put = _leg(entry=100.0, is_long=False)
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    with patch(
        "backend.strategies.s001_short_strangle.logic.is_pre_expiry_window",
        return_value=True,
    ), patch(
        "backend.strategies.s001_short_strangle.logic.get_hours_to_expiry",
        return_value=0.2,
    ):
        long_action = asyncio.run(
            strat.on_tick(
                _trade(position_side="long"),
                call,
                put,
                100.0,
                100.0,
                db,
                net_mtm=0.0,
                gross_mtm_for_sl=0.0,
            )
        )
        short_action = asyncio.run(
            strat.on_tick(
                _trade(position_side="short"),
                call,
                put,
                100.0,
                100.0,
                db,
                net_mtm=0.0,
                gross_mtm_for_sl=0.0,
            )
        )
    assert long_action.should_exit is False
    assert short_action.should_exit is True
    assert short_action.exit_reason == "PRE_EXPIRY"


def test_adjustment_disabled_does_not_fire_on_trigger() -> None:
    strat = ShortStrangleStrategy()
    call = _leg(entry=100.0, is_long=False)
    call.leg_type = "call"
    put = _leg(entry=100.0, is_long=False)
    put.leg_type = "put"
    action = asyncio.run(
        strat.on_tick(
            _trade(adjustment_enabled=False),
            call,
            put,
            160.0,
            90.0,
            None,
            realized_pnl=0.0,
            delta_mtm=-0.05,
            net_mtm=-0.05,
            slippage_pct=0.0,
            gross_mtm_for_sl=-0.05,
        )
    )
    assert action.should_adjust is False
    assert action.should_exit is False


def test_new_trade_row_defaults_short_and_adjustment_on() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from backend.database import Base
    from backend.models import Account, Trade

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        acc = Account(
            name="ltf-acc",
            api_key_encrypted="x",
            api_secret_encrypted="y",
        )
        db.add(acc)
        db.commit()
        row = Trade(
            account_id=acc.id,
            underlying="BTC",
            expiry_date=date(2026, 5, 1),
            status="active",
            entry_time=datetime.now(timezone.utc),
            total_premium_collected=1.0,
            profit_target_usd=10.0,
            stoploss_usd=10.0,
            trigger_mode="flat",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.position_side == "short"
        assert row.adjustment_enabled is True
        assert is_long_trade(row) is False


def test_is_long_trade_helper() -> None:
    assert is_long_trade(_trade(position_side="long")) is True
    assert is_long_trade(_trade(position_side="short")) is False
    assert is_long_trade(_trade(position_side="LONG")) is True
    assert is_long_trade(SimpleNamespace()) is False
