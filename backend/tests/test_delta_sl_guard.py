# test_delta_sl_guard.py — min 120% SL, confirm/retry, 60s replace

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_auto_trade import AutoTradeSettingsSchema
from backend.api.routes_trade import update_trade_settings
from backend.config import TradeStatus
from backend.core.delta_sl import (
    attach_position_bracket_sl,
    compute_bracket_sl,
    delta_sl_missing_message,
    ensure_leg_delta_stop,
)
from backend.database import Base
from backend.models import Account, Leg, Trade
from backend.schemas import (
    UNIVERSAL_SL_PCT_ERROR,
    TradeInitiateRequest,
    TradeSettingsUpdate,
)


def _engine() -> object:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _initiate(**kw: object) -> TradeInitiateRequest:
    data: dict[str, object] = {
        "underlying": "BTC",
        "expiry_date": "2099-12-31",
        "call_strike": 83000,
        "call_product_id": 1,
        "call_symbol": "C-BTC",
        "put_strike": 80000,
        "put_product_id": 2,
        "put_symbol": "P-BTC",
        "quantity": 1,
        "trigger_mode": "flat",
        "flat_trigger_pct": 150,
        "universal_sl_pct": 200,
    }
    data.update(kw)
    return TradeInitiateRequest(**data)  # type: ignore[arg-type]


def test_universal_sl_pct_100_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        _initiate(universal_sl_pct=100)
    assert "120" in str(ei.value)
    assert "premium double" in str(ei.value)


def test_universal_sl_pct_120_ok() -> None:
    req = _initiate(universal_sl_pct=120)
    assert req.universal_sl_pct == 120


def test_universal_sl_pct_200_stop_is_fill_times_two() -> None:
    stop, limit = compute_bracket_sl(50.0, 200.0)
    assert stop == 100.0
    assert limit == round(100.0 * 1.05, 2)


def test_auto_trade_settings_100_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        AutoTradeSettingsSchema(universal_sl_pct=100)
    assert "120" in str(ei.value)


@pytest.mark.asyncio
async def test_patch_universal_sl_pct_100_http_422() -> None:
    SessionLocal = sessionmaker(bind=_engine())
    with SessionLocal() as db:
        acc = Account(name="sl-patch", api_key_encrypted="x", api_secret_encrypted="y")
        db.add(acc)
        db.flush()
        trade = Trade(
            account_id=acc.id,
            underlying="BTC",
            expiry_date=date(2099, 12, 31),
            status=TradeStatus.ACTIVE.value,
            entry_time=datetime.now(timezone.utc),
            total_premium_collected=0.0,
            profit_target_usd=50.0,
            stoploss_usd=100.0,
            trigger_mode="flat",
            position_side="short",
            universal_sl_pct=200.0,
        )
        db.add(trade)
        db.commit()
        bot = MagicMock()
        bot.position_tracker.get.return_value = None
        payload = TradeSettingsUpdate.model_construct(universal_sl_pct=100)
        with patch("backend.api.routes_trade.bot_engine", bot):
            with pytest.raises(HTTPException) as ei:
                await update_trade_settings(trade.id, payload, db=db)
        assert ei.value.status_code == 422
        assert UNIVERSAL_SL_PCT_ERROR in str(ei.value.detail)


@pytest.mark.asyncio
async def test_bracket_fail_no_order_id_retries_then_critical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = MagicMock()
    client.place_position_bracket = AsyncMock(return_value={})
    client.get_open_stop_orders = AsyncMock(return_value=[])
    client.get_order = AsyncMock(side_effect=Exception("missing"))
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    with (
        caplog.at_level(logging.CRITICAL),
        patch("backend.core.delta_sl.asyncio.sleep", new_callable=AsyncMock),
        patch("backend.core.ws_manager.ws_manager", ws),
    ):
        ok = await attach_position_bracket_sl(
            client,
            product_id=11,
            stop_price=20.0,
            stop_limit_price=21.0,
            leg="call",
            trade_id=42,
            confirm_wait_seconds=0,
        )
    assert ok is False
    assert client.place_position_bracket.await_count == 2
    assert any("BRACKET_SL_FAILED" in r.message for r in caplog.records)
    assert ws.broadcast.await_count >= 1
    sent = ws.broadcast.await_args.args[0]
    assert sent["event"] == "DELTA_SL_MISSING"
    assert sent["sticky"] is True
    assert "NAHI LAGA" in sent["message"]
    assert "Trade #42" in sent["message"]
    assert "call" in sent["message"]


@pytest.mark.asyncio
async def test_ensure_leg_stop_replaces_when_missing() -> None:
    client = MagicMock()
    client.get_order = AsyncMock(side_effect=Exception("gone"))
    client.get_open_stop_orders = AsyncMock(
        side_effect=[[], [{"product_id": 9, "id": 77}]]
    )
    client.place_position_bracket = AsyncMock(return_value={"id": 77})
    leg = SimpleNamespace(
        id=1,
        status="open",
        product_id=9,
        sl_trigger_price=20.0,
        delta_sl_order_id="old",
        quantity=1,
        leg_type="call",
        delta_sl_confirmed=True,
    )
    with patch("backend.core.delta_sl.asyncio.sleep", new_callable=AsyncMock):
        ok = await ensure_leg_delta_stop(
            client,
            leg,
            trade=SimpleNamespace(position_side="short"),
            trade_id=5,
            confirm_wait_seconds=0,
        )
    assert ok is True
    assert client.place_position_bracket.await_count == 1
    assert leg.delta_sl_confirmed is True
    assert str(leg.delta_sl_order_id) == "77"


@pytest.mark.asyncio
async def test_long_trade_skips_bracket() -> None:
    client = MagicMock()
    client.place_position_bracket = AsyncMock()
    ok = await attach_position_bracket_sl(
        client,
        product_id=11,
        stop_price=20.0,
        stop_limit_price=21.0,
        trade=SimpleNamespace(position_side="long"),
        confirm_wait_seconds=0,
    )
    assert ok is False
    client.place_position_bracket.assert_not_called()


def test_missing_banner_text() -> None:
    msg = delta_sl_missing_message(9, "put")
    assert "Trade #9" in msg
    assert "put" in msg
    assert "manually" in msg
