# test_long_detached.py — tracker objects stay readable after session close

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import ExitReason, TradeStatus
from backend.database import Base
from backend.engine.long_settlement import handle_long_trade_tick
from backend.engine.position_tracker import PositionTracker, TradeState
from backend.models import Account, Leg, Trade
from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy


def _make_engine() -> object:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _seed_long_trade(db: Session, *, name: str = "det-acc") -> tuple[Trade, Leg, Leg]:
    acc = Account(
        name=name,
        api_key_encrypted="x",
        api_secret_encrypted="y",
    )
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
        position_side="long",
        adjustment_enabled=False,
        target_usd=150.0,
        sl_usd=100.0,
        peak_gross_mtm=0.0,
        trail_sl_level=-100.0,
    )
    db.add(trade)
    db.flush()
    now = datetime.now(timezone.utc)
    call = Leg(
        trade_id=trade.id,
        leg_type="call",
        strike=110000.0,
        symbol="C-BTC-110000-311299",
        product_id=11,
        initial_premium=50.0,
        quantity=1,
        entry_time=now,
        status="open",
        is_bot_managed=True,
        is_long=True,
        side="BUY",
    )
    put = Leg(
        trade_id=trade.id,
        leg_type="put",
        strike=100000.0,
        symbol="P-BTC-100000-311299",
        product_id=12,
        initial_premium=40.0,
        quantity=1,
        entry_time=now,
        status="open",
        is_bot_managed=True,
        is_long=True,
        side="BUY",
    )
    db.add_all([call, put])
    db.commit()
    db.refresh(trade)
    db.refresh(call)
    db.refresh(put)
    return trade, call, put


@pytest.mark.asyncio
async def test_handle_long_tick_tracker_readable_after_session_close() -> None:
    engine = _make_engine()
    SessionLocal = sessionmaker(bind=engine)
    tracker = PositionTracker()
    with SessionLocal() as db:
        trade, call, put = _seed_long_trade(db)
        db.expunge(trade)
        db.expunge(call)
        db.expunge(put)
        tracker.add(trade, call, put)
        trade_id = trade.id

    @contextmanager
    def db_factory() -> object:
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    client = AsyncMock()
    client.get_positions = AsyncMock(
        return_value=[
            {"product_id": 11, "size": 1},
            {"product_id": 12, "size": 1},
        ]
    )
    client.get_position_size = AsyncMock(return_value=1.0)

    state = tracker.get(trade_id)
    assert state is not None
    outcome = await handle_long_trade_tick(
        db_factory=db_factory,
        delta_client=client,
        position_tracker=tracker,
        wait_since={},
        trade_state=state,
        close_funnel=None,
        flat_since={},
    )
    assert outcome == "continue"
    live = tracker.get(trade_id)
    assert live is not None
    assert live.trade.profit_target_usd == 150.0
    assert live.trade.peak_gross_mtm == 0.0
    assert live.call_leg.initial_premium == 50.0


@pytest.mark.asyncio
async def test_get_active_trades_includes_long_after_tick() -> None:
    engine = _make_engine()
    SessionLocal = sessionmaker(bind=engine)
    tracker = PositionTracker()
    with SessionLocal() as db:
        trade, call, put = _seed_long_trade(db, name="det-acc-active")
        db.expunge(trade)
        db.expunge(call)
        db.expunge(put)
        tracker.add(trade, call, put)
        trade_id = int(trade.id)

    @contextmanager
    def db_factory() -> object:
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    client = AsyncMock()
    client.get_positions = AsyncMock(
        return_value=[
            {"product_id": 11, "size": 1},
            {"product_id": 12, "size": 1},
        ]
    )
    client.get_position_size = AsyncMock(return_value=1.0)
    await handle_long_trade_tick(
        db_factory=db_factory,
        delta_client=client,
        position_tracker=tracker,
        wait_since={},
        trade_state=tracker.get(trade_id),
        close_funnel=None,
        flat_since={},
    )

    from backend.api import routes_trade

    bot = MagicMock()
    bot.position_tracker = tracker
    bot.delta_client = None
    bot._refresh_delta_client = MagicMock()
    bot._refresh_btc_spot = AsyncMock()
    bot.maybe_close_orphaned_wings = AsyncMock()

    with SessionLocal() as db:
        with (
            patch.object(routes_trade, "bot_engine", bot),
            patch.object(routes_trade, "ws_manager", MagicMock()),
        ):
            payload = await routes_trade.get_active_trades(db=db)
    assert isinstance(payload, dict)
    ids = [t.get("trade_id") for t in payload.get("trades") or []]
    assert trade_id in ids
    long_row = next(t for t in payload["trades"] if t.get("trade_id") == trade_id)
    assert long_row.get("error") != "state_unreadable"


@pytest.mark.asyncio
async def test_long_on_tick_target_uses_tracker_trade() -> None:
    engine = _make_engine()
    SessionLocal = sessionmaker(bind=engine)
    tracker = PositionTracker()
    with SessionLocal() as db:
        trade, call, put = _seed_long_trade(db, name="det-acc-tp")
        db.expunge(trade)
        db.expunge(call)
        db.expunge(put)
        tracker.add(trade, call, put)
        trade_id = int(trade.id)

    @contextmanager
    def db_factory() -> object:
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    client = AsyncMock()
    client.get_positions = AsyncMock(
        return_value=[
            {"product_id": 11, "size": 1},
            {"product_id": 12, "size": 1},
        ]
    )
    client.get_position_size = AsyncMock(return_value=1.0)
    await handle_long_trade_tick(
        db_factory=db_factory,
        delta_client=client,
        position_tracker=tracker,
        wait_since={},
        trade_state=tracker.get(trade_id),
        close_funnel=None,
        flat_since={},
    )
    live = tracker.get(trade_id)
    assert live is not None
    action = await ShortStrangleStrategy().on_tick(
        live.trade,
        live.call_leg,
        live.put_leg,
        80.0,
        80.0,
        db_session=None,
        realized_pnl=0.0,
        delta_mtm=150.0,
        net_mtm=150.0,
        slippage_pct=0.0,
        gross_mtm_for_sl=150.0,
    )
    assert action.should_exit is True
    assert action.exit_reason == ExitReason.LONG_TARGET.value


@pytest.mark.asyncio
async def test_get_active_trades_isolates_unreadable_trade() -> None:
    engine = _make_engine()
    SessionLocal = sessionmaker(bind=engine)
    tracker = PositionTracker()
    with SessionLocal() as db:
        good, call, put = _seed_long_trade(db, name="det-acc-ok")
        other = Trade(
            account_id=good.account_id,
            underlying="BTC",
            expiry_date=date(2099, 12, 31),
            status=TradeStatus.ACTIVE.value,
            entry_time=datetime.now(timezone.utc),
            total_premium_collected=0.0,
            profit_target_usd=10.0,
            stoploss_usd=10.0,
            trigger_mode="flat",
            position_side="short",
        )
        db.add(other)
        db.flush()
        now = datetime.now(timezone.utc)
        oc = Leg(
            trade_id=other.id,
            leg_type="call",
            strike=1.0,
            symbol="C-OK",
            product_id=21,
            initial_premium=10.0,
            quantity=1,
            entry_time=now,
            status="open",
            is_bot_managed=True,
            is_long=False,
            side="SELL",
        )
        op = Leg(
            trade_id=other.id,
            leg_type="put",
            strike=1.0,
            symbol="P-OK",
            product_id=22,
            initial_premium=10.0,
            quantity=1,
            entry_time=now,
            status="open",
            is_bot_managed=True,
            is_long=False,
            side="SELL",
        )
        db.add_all([oc, op])
        db.commit()
        db.refresh(good)
        db.refresh(call)
        db.refresh(put)
        db.refresh(other)
        db.refresh(oc)
        db.refresh(op)
        for obj in (good, call, put, other, oc, op):
            db.expunge(obj)
        tracker.add(good, call, put)
        tracker.add(other, oc, op)
        good_id = int(good.id)
        bad_id = 999001

    class Boom:
        def __getattribute__(self, name: str) -> object:
            raise RuntimeError("unreadable")

    tracker._positions[bad_id] = TradeState(
        trade_id=bad_id,
        trade=Boom(),
        call_leg=Boom(),
        put_leg=Boom(),
    )

    from backend.api import routes_trade

    bot = MagicMock()
    bot.position_tracker = tracker
    bot.delta_client = None
    bot._refresh_delta_client = MagicMock()
    bot._refresh_btc_spot = AsyncMock()
    bot.maybe_close_orphaned_wings = AsyncMock()

    with SessionLocal() as db:
        with (
            patch.object(routes_trade, "bot_engine", bot),
            patch.object(routes_trade, "ws_manager", MagicMock()),
            patch.object(routes_trade, "_sync_tracker_from_db", lambda _db: None),
        ):
            payload = await routes_trade.get_active_trades(db=db)

    assert isinstance(payload, dict)
    rows = payload.get("trades") or []
    by_id = {r.get("trade_id"): r for r in rows}
    assert bad_id in by_id
    assert by_id[bad_id].get("error") == "state_unreadable"
    assert good_id in by_id
    assert by_id[good_id].get("error") != "state_unreadable"
    assert "underlying" in by_id[good_id] or "call_leg" in by_id[good_id]
