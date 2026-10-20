# test_long_live_panel.py — PATCH long settings + /active long fields

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_trade import get_active_trades, update_trade_settings
from backend.config import TradeStatus
from backend.database import Base
from backend.engine.position_tracker import PositionTracker
from backend.models import Account, Leg, Trade
from backend.schemas import TradeSettingsUpdate


def _engine() -> object:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _seed(
    db: Session,
    *,
    name: str,
    side: str = "long",
    peak: float = 40.0,
) -> tuple[Trade, Leg, Leg]:
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
        profit_target_usd=150.0 if side == "long" else 50.0,
        stoploss_usd=100.0,
        trigger_mode="flat",
        position_side=side,
        adjustment_enabled=side != "long",
        target_usd=150.0 if side == "long" else None,
        sl_usd=100.0 if side == "long" else None,
        peak_gross_mtm=peak if side == "long" else None,
        trail_sl_level=-100.0 if side == "long" else None,
        initial_max_profit=100.0,
        tp_pct=50.0,
        sl_pct=100.0,
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
        quantity=1,
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
        quantity=1,
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


@pytest.mark.asyncio
async def test_long_patch_recomputes_trail_sl() -> None:
    SessionLocal = sessionmaker(bind=_engine())
    with SessionLocal() as db:
        trade, _, _ = _seed(db, name="live-patch")
        bot = MagicMock()
        bot.position_tracker.get.return_value = None
        with patch("backend.api.routes_trade.bot_engine", bot):
            out = await update_trade_settings(
                trade.id,
                TradeSettingsUpdate(
                    target_usd=150,
                    sl_usd=100,
                    trail_step_usd=10,
                    trail_amt_usd=5,
                ),
                db=db,
            )
        db.refresh(trade)
        assert out["success"] is True
        assert trade.trail_sl_level == -80.0
        assert trade.target_usd == 150.0
        assert trade.sl_usd == 100.0
        assert trade.trail_step_usd == 10.0
        assert trade.trail_amt_usd == 5.0


@pytest.mark.asyncio
async def test_long_patch_bad_trail_422() -> None:
    SessionLocal = sessionmaker(bind=_engine())
    with SessionLocal() as db:
        trade, _, _ = _seed(db, name="live-bad-trail")
        bot = MagicMock()
        bot.position_tracker.get.return_value = None
        with patch("backend.api.routes_trade.bot_engine", bot):
            with pytest.raises(HTTPException) as ei:
                await update_trade_settings(
                    trade.id,
                    TradeSettingsUpdate(trail_step_usd=5, trail_amt_usd=10),
                    db=db,
                )
        assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_long_patch_slab_422() -> None:
    SessionLocal = sessionmaker(bind=_engine())
    with SessionLocal() as db:
        trade, _, _ = _seed(db, name="live-slab")
        bot = MagicMock()
        bot.position_tracker.get.return_value = None
        with patch("backend.api.routes_trade.bot_engine", bot):
            with pytest.raises(HTTPException) as ei:
                await update_trade_settings(
                    trade.id,
                    TradeSettingsUpdate(slab_24h=200),
                    db=db,
                )
        assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_short_patch_profit_target_unchanged() -> None:
    SessionLocal = sessionmaker(bind=_engine())
    with SessionLocal() as db:
        trade, _, _ = _seed(db, name="live-short", side="short", peak=0)
        bot = MagicMock()
        bot.position_tracker.get.return_value = None
        with patch("backend.api.routes_trade.bot_engine", bot):
            out = await update_trade_settings(
                trade.id,
                TradeSettingsUpdate(profit_target_usd=77.5),
                db=db,
            )
        db.refresh(trade)
        assert out["success"] is True
        assert trade.profit_target_usd == 77.5
        assert "profit_target_usd" in out["updated"]


@pytest.mark.asyncio
async def test_active_long_has_live_fields() -> None:
    SessionLocal = sessionmaker(bind=_engine())
    tracker = PositionTracker()
    with SessionLocal() as db:
        trade, call, put = _seed(db, name="live-active")
        db.expunge(trade)
        db.expunge(call)
        db.expunge(put)
        tracker.add(trade, call, put)
        trade_id = int(trade.id)

    bot = MagicMock()
    bot.position_tracker = tracker
    bot.delta_client = None
    bot._refresh_delta_client = MagicMock()
    bot._refresh_btc_spot = AsyncMock()
    bot.maybe_close_orphaned_wings = AsyncMock()

    with SessionLocal() as db:
        with (
            patch("backend.api.routes_trade.bot_engine", bot),
            patch("backend.api.routes_trade.ws_manager", MagicMock()),
        ):
            payload = await get_active_trades(db=db)

    row = next(t for t in payload["trades"] if t.get("trade_id") == trade_id)
    for key in (
        "gross_mtm",
        "net_mtm",
        "fees_paid",
        "est_exit_fees",
        "target_usd",
        "sl_usd",
        "trail_step_usd",
        "trail_amt_usd",
        "peak_gross_mtm",
        "trail_sl_level",
        "hours_to_expiry",
        "call_breakeven",
        "put_breakeven",
    ):
        assert key in row, key
    assert "leg_pnl" in row["call_leg"]
    assert "initial_premium" in row["call_leg"]
    assert "current_bid" in row["call_leg"]
    assert "current_mark" in row["call_leg"]
    assert "leg_pnl" in row["put_leg"]
    assert row["call_breakeven"] == 82200.0 + 50.0 + 40.0
    assert row["put_breakeven"] == 80000.0 - 50.0 - 40.0
