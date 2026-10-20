# test_long_exit.py — long SELL reduce_only exit; abs(size) verify

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_trade import close_single_leg, exit_trade
from backend.config import ExitReason, TradeStatus
from backend.core.delta_client import signed_size_is_open
from backend.database import Base
from backend.engine.bot_engine import BotEngine
from backend.engine.order_executor import OrderExecutor
from backend.engine.position_tracker import PositionTracker
from backend.models import Account, Leg, Trade
from backend.schemas import TradeExitRequest
from backend.strategies.s001_short_strangle.adjustment import AdjustmentExecutor
from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy


def test_signed_size_is_open() -> None:
    assert signed_size_is_open(10) is True
    assert signed_size_is_open(-10) is True
    assert signed_size_is_open(0) is False
    assert signed_size_is_open(None) is False


def _engine() -> object:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _seed(db: object, *, name: str, side: str = "long") -> tuple[Trade, Leg, Leg]:
    acc = Account(name=name, api_key_encrypted="x", api_secret_encrypted="y")
    db.add(acc)
    db.flush()
    trade = Trade(
        account_id=acc.id,
        underlying="BTC",
        expiry_date=date(2099, 12, 31),
        status=TradeStatus.ACTIVE.value,
        entry_time=datetime.now(timezone.utc),
        total_premium_collected=0.0,
        profit_target_usd=150.0,
        stoploss_usd=100.0,
        trigger_mode="flat",
        position_side=side,
        adjustment_enabled=side != "long",
        target_usd=150.0 if side == "long" else None,
        sl_usd=100.0 if side == "long" else None,
        peak_gross_mtm=0.0 if side == "long" else None,
        trail_sl_level=-100.0 if side == "long" else None,
        initial_max_profit=100.0,
        tp_pct=50.0,
        sl_pct=100.0,
        slippage_pct=0.0,
        is_demo=False,
    )
    db.add(trade)
    db.flush()
    now = datetime.now(timezone.utc)
    call = Leg(
        trade_id=trade.id,
        leg_type="call",
        strike=82200.0,
        symbol="C-BTC-82200-311299",
        product_id=11,
        initial_premium=50.0,
        quantity=10,
        entry_time=now,
        status="open",
        is_bot_managed=True,
        is_long=side == "long",
        side="BUY" if side == "long" else "SELL",
    )
    put = Leg(
        trade_id=trade.id,
        leg_type="put",
        strike=80000.0,
        symbol="P-BTC-80000-311299",
        product_id=12,
        initial_premium=40.0,
        quantity=10,
        entry_time=now,
        status="open",
        is_bot_managed=True,
        is_long=side == "long",
        side="BUY" if side == "long" else "SELL",
    )
    db.add_all([call, put])
    db.commit()
    db.refresh(trade)
    db.refresh(call)
    db.refresh(put)
    return trade, call, put


def _harness(*, size_after: float = 0.0, side: str = "long"):
    engine_sql = _engine()
    SessionLocal = sessionmaker(bind=engine_sql, expire_on_commit=False)

    @contextmanager
    def db_factory():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={
            "order_id": 77,
            "avg_fill_price": 12.5,
            "state": "closed",
            "status": "closed",
        }
    )
    client.resolve_fill_price = AsyncMock(return_value=12.5)
    client.verify_position_exists = AsyncMock(return_value=True)
    client.get_position_size = AsyncMock(return_value=abs(float(size_after)))
    client.get_order_commission = AsyncMock(return_value=0.0)

    bot = BotEngine(
        db_factory=db_factory,
        strategy=ShortStrangleStrategy(),
        position_tracker=PositionTracker(),
        order_executor=OrderExecutor(),
        adjustment_executor=AdjustmentExecutor(),
        delta_client=client,
    )
    bot._run_post_exit_orphan_sl_sweep = AsyncMock()
    bot._push_error = AsyncMock()
    bot._refresh_delta_client = MagicMock()
    db = SessionLocal()
    trade, call, put = _seed(db, name=f"exit-{side}-{id(bot)}", side=side)
    bot.position_tracker.add(trade, call, put)
    return bot, client, db, trade, call, put


async def _run_close(bot: BotEngine, trade: Trade, reason: str) -> dict:
    with (
        patch("backend.engine.bot_engine.asyncio.sleep", new_callable=AsyncMock),
        patch("backend.engine.bot_engine.ws_manager.broadcast", new_callable=AsyncMock),
        patch("backend.core.ws_manager.ws_manager.broadcast", new_callable=AsyncMock),
        patch(
            "backend.api.routes_account.cleanup_orphan_sl_orders",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        with bot.db_factory() as db:
            return await bot.close_master_trade(
                trade_id=int(trade.id),
                reason=reason,
                db=db,
                skip_master_legs=False,
                trade_state=bot.position_tracker.get(int(trade.id)),
                total_pnl=1.05,
                gross_mtm=1.05,
                fees_paid=0.0,
                est_exit_fees=0.0,
                slippage_amount=0.0,
                net_mtm=1.05,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        ExitReason.LONG_TARGET.value,
        ExitReason.LONG_SL.value,
        ExitReason.LONG_TRAIL_SL.value,
        ExitReason.MANUAL_EMERGENCY.value,
    ],
)
async def test_long_exit_places_two_sell_reduce_only(reason: str) -> None:
    bot, client, db, trade, call, put = _harness(size_after=0.0)
    try:
        await _run_close(bot, trade, reason)
        db.expire_all()
        row = db.query(Trade).filter(Trade.id == trade.id).first()
        assert row is not None
        expect = (
            TradeStatus.EMERGENCY_CLOSED.value
            if reason == ExitReason.MANUAL_EMERGENCY.value
            else TradeStatus.CLOSED.value
        )
        assert str(row.status).lower() == expect
        assert client.place_order.await_count == 2
        sides = [c.kwargs["side"] for c in client.place_order.await_args_list]
        assert sides == ["sell", "sell"]
        assert all(c.kwargs["reduce_only"] is True for c in client.place_order.await_args_list)
        call_db = db.query(Leg).filter(Leg.id == call.id).first()
        put_db = db.query(Leg).filter(Leg.id == put.id).first()
        assert float(call_db.exit_premium) == 12.5
        assert float(put_db.exit_premium) == 12.5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_long_exit_stays_active_when_size_still_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, client, db, trade, _, _ = _harness(size_after=10.0)
    try:
        with caplog.at_level("CRITICAL"):
            await _run_close(bot, trade, ExitReason.LONG_TARGET.value)
        db.expire_all()
        row = db.query(Trade).filter(Trade.id == trade.id).first()
        assert str(row.status).lower() == TradeStatus.ACTIVE.value
        assert client.place_order.await_count >= 4
        assert any("still_open" in r.message or "LONG_EXIT_RETRY" in r.message or "Exit order failure" in r.message for r in caplog.records)
        bot._push_error.assert_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_manual_exit_all_long_sells() -> None:
    bot, client, db, trade, call, put = _harness(size_after=0.0)
    try:
        with (
            patch("backend.api.routes_trade.bot_engine", bot),
            patch("backend.engine.bot_engine.asyncio.sleep", new_callable=AsyncMock),
            patch("backend.engine.bot_engine.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            out = await exit_trade(
                int(trade.id),
                TradeExitRequest(reason=ExitReason.MANUAL_EMERGENCY.value),
                db=db,
            )
        assert out["success"] is True
        assert client.place_order.await_count == 2
        assert all(c.kwargs["side"] == "sell" for c in client.place_order.await_args_list)
        db.expire_all()
        assert str(db.query(Trade).filter(Trade.id == trade.id).first().status).lower() == TradeStatus.EMERGENCY_CLOSED.value
        assert float(db.query(Leg).filter(Leg.id == call.id).first().exit_premium) == 12.5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_close_long_call_leg_sells_reduce_only() -> None:
    bot, client, db, trade, call, _ = _harness(size_after=0.0)
    try:
        with (
            patch("backend.api.routes_trade.bot_engine", bot),
            patch("backend.api.routes_trade.asyncio.sleep", new_callable=AsyncMock),
        ):
            await close_single_leg(int(trade.id), "call", db=db)
        assert client.place_order.await_count >= 1
        assert client.place_order.await_args.kwargs["side"] == "sell"
        assert client.place_order.await_args.kwargs["reduce_only"] is True
        db.refresh(call)
        assert float(call.exit_premium) == 12.5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_short_exit_still_uses_phased_buy() -> None:
    bot, client, db, trade, _, _ = _harness(size_after=0.0, side="short")
    try:
        phased = bot.order_executor.close_basket_legs_phased
        with patch.object(
            bot.order_executor,
            "close_basket_legs_phased",
            wraps=phased,
        ) as spy:
            await _run_close(bot, trade, ExitReason.PROFIT_TARGET.value)
            assert spy.await_count == 1
        assert all(c.kwargs["side"] == "buy" for c in client.place_order.await_args_list)
        assert all(c.kwargs["reduce_only"] is True for c in client.place_order.await_args_list)
    finally:
        db.close()
