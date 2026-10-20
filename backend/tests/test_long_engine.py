# test_long_engine.py — long entry, close_leg side, $ TP/SL trail, settlement

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_trade import (
    _leg_snapshot,
    _persist_strangle_trade,
    initiate_trade,
    register_existing_trade,
)
from backend.config import ExitReason, TradeStatus
from backend.engine.long_settlement import (
    apply_expiry_settlement,
    apply_pre_expiry_external_close,
    option_intrinsic,
    resolve_settlement_spot,
)
from backend.engine.order_executor import OrderExecutor
from backend.schemas import TradeInitiateRequest, TradeRegisterExistingRequest
from backend.strategies.base_strategy import OrderResult
from backend.strategies.s001_short_strangle.logic import (
    ShortStrangleStrategy,
    apply_long_gross_exits,
    compute_long_trail_sl_level,
    is_stoploss_exit_reason,
)


def _payload(**kwargs: object) -> TradeInitiateRequest:
    data: dict[str, object] = {
        "underlying": "BTC",
        "expiry_date": "2099-12-31",
        "call_strike": 110000,
        "call_product_id": 11,
        "call_symbol": "C-BTC-110000-311299",
        "put_strike": 100000,
        "put_product_id": 12,
        "put_symbol": "P-BTC-100000-311299",
        "quantity": 1,
        "trigger_mode": "flat",
        "flat_trigger_pct": 150,
        "position_side": "long",
        "target_usd": 150,
        "sl_usd": 100,
    }
    data.update(kwargs)
    return TradeInitiateRequest(**data)  # type: ignore[arg-type]


def _leg(
    *,
    is_long: bool,
    leg_type: str = "call",
    strike: float = 109000.0,
    entry: float = 50.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        is_bot_managed=True,
        status="open",
        is_long=is_long,
        leg_type=leg_type,
        symbol=f"{leg_type}-SYM",
        product_id=11 if leg_type == "call" else 12,
        quantity=1,
        strike=strike,
        initial_premium=entry,
        exit_premium=None,
        delta_order_id="1",
        realized_pnl=None,
        exit_fee_usd=None,
    )


@pytest.mark.asyncio
async def test_close_leg_long_sells_reduce_only() -> None:
    exe = OrderExecutor()
    client = AsyncMock()
    client.place_order = AsyncMock(
        return_value={"order_id": 7, "avg_fill_price": 9.0, "state": "closed"}
    )
    client.resolve_fill_price = AsyncMock(return_value=9.0)
    await exe.close_leg(_leg(is_long=True), client)
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_close_leg_short_buys_reduce_only() -> None:
    exe = OrderExecutor()
    client = AsyncMock()
    client.place_order = AsyncMock(
        return_value={"order_id": 8, "avg_fill_price": 9.0, "state": "closed"}
    )
    client.resolve_fill_price = AsyncMock(return_value=9.0)
    await exe.close_leg(_leg(is_long=False), client)
    kwargs = client.place_order.await_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_long_initiate_flag_off_403() -> None:
    with pytest.raises(HTTPException) as ei:
        await initiate_trade(_payload(), db=MagicMock())
    assert ei.value.status_code == 403
    assert "disabled" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_long_initiate_bad_trail_422() -> None:
    with patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True):
        with pytest.raises(HTTPException) as ei:
            await initiate_trade(
                _payload(trail_step_usd=10, trail_amt_usd=11),
                db=MagicMock(),
            )
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_long_entry_buy_twice_no_bracket_no_mirror() -> None:
    account = SimpleNamespace(id=1)
    db = MagicMock()
    client = AsyncMock()
    client.get_mark_price = AsyncMock(return_value=20.0)
    client.resolve_fill_price = AsyncMock(return_value=20.0)
    buy = AsyncMock(
        return_value=OrderResult(success=True, filled_price=20.0, order_id=101)
    )
    sell = AsyncMock()
    close = AsyncMock()
    persisted: list[Any] = []

    async def fake_persist(**kwargs: object) -> tuple[Any, Any, Any]:
        trade = SimpleNamespace(
            id=5,
            position_side="long",
            is_demo=False,
            expiry_date=date(2099, 12, 31),
            underlying="BTC",
            basket_number=5,
            monitoring_starts_at=None,
            profit_target_usd=150.0,
            stoploss_usd=100.0,
            initial_max_profit=0.0,
            tp_pct=50.0,
            sl_pct=100.0,
        )
        call = SimpleNamespace(id=1, is_long=True, side="BUY")
        put = SimpleNamespace(id=2, is_long=True, side="BUY")
        persisted.extend([trade, call, put])
        return trade, call, put

    tracker = MagicMock()
    tracker.add = MagicMock()
    tracker.get = MagicMock(return_value=MagicMock())
    tracker.get_all_active = MagicMock(return_value=[1])
    mirror_mod = MagicMock()
    mirror_mod.mirror_engine = MagicMock()
    mirror_mod.mirror_engine.mirror_trade_entry = AsyncMock()

    with (
        patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True),
        patch("backend.api.routes_trade._get_active_account", return_value=account),
        patch(
            "backend.api.routes_trade._ensure_no_active_trade",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade._build_delta_client", return_value=client),
        patch(
            "backend.api.routes_trade._manual_entry_triple_guard",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.api.routes_trade._verify_leg_on_delta",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.api.routes_trade._persist_strangle_trade",
            new=fake_persist,
        ),
        patch("backend.core.delta_sl.compute_bracket_sl") as bracket,
        patch("backend.api.routes_trade.bot_engine") as eng,
        patch.dict("sys.modules", {"backend.engine.mirror_engine": mirror_mod}),
    ):
        eng.order_executor.buy_option = buy
        eng.order_executor.sell_option = sell
        eng.order_executor.close_leg = close
        eng.position_tracker = tracker
        eng._push_error = AsyncMock()
        await initiate_trade(
            _payload(trail_step_usd=10, trail_amt_usd=5),
            db=db,
        )

    assert buy.await_count == 2
    assert sell.await_count == 0
    assert close.await_count == 0
    assert bracket.call_count == 0
    assert mirror_mod.mirror_engine.mirror_trade_entry.await_count == 0
    assert persisted[1].is_long is True
    assert persisted[1].side == "BUY"
    assert persisted[2].is_long is True
    assert persisted[2].side == "BUY"


@pytest.mark.asyncio
async def test_long_put_fail_unwinds_call_sell_marks_failed() -> None:
    account = SimpleNamespace(id=1)
    db = MagicMock()
    client = AsyncMock()
    client.get_mark_price = AsyncMock(return_value=20.0)
    buy = AsyncMock(
        side_effect=[
            OrderResult(success=True, filled_price=20.0, order_id=101),
            OrderResult(success=False, error="put rejected"),
        ]
    )
    close = AsyncMock(return_value=OrderResult(success=True, filled_price=19.0))
    added: list[Any] = []
    db.add.side_effect = lambda obj: added.append(obj)
    db.commit = MagicMock()

    with (
        patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True),
        patch("backend.api.routes_trade._get_active_account", return_value=account),
        patch(
            "backend.api.routes_trade._ensure_no_active_trade",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade._build_delta_client", return_value=client),
        patch(
            "backend.api.routes_trade._manual_entry_triple_guard",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.api.routes_trade._verify_leg_on_delta",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade.bot_engine") as eng,
    ):
        eng.order_executor.buy_option = buy
        eng.order_executor.sell_option = AsyncMock()
        eng.order_executor.close_leg = close
        eng._push_error = AsyncMock()
        with pytest.raises(HTTPException) as ei:
            await initiate_trade(_payload(), db=db)

    assert ei.value.status_code == 502
    assert close.await_count == 1
    unwind_leg = close.await_args.args[0]
    assert unwind_leg.is_long is True
    assert eng.order_executor.sell_option.await_count == 0
    failed = [x for x in added if getattr(x, "status", None) == TradeStatus.FAILED.value]
    assert failed
    assert eng._push_error.await_count == 1


@pytest.mark.asyncio
async def test_persist_long_legs_is_long_buy() -> None:
    payload = _payload(trail_step_usd=10, trail_amt_usd=5)
    account = SimpleNamespace(id=1)
    stored: list[Any] = []
    db = MagicMock()

    def add(obj: Any) -> None:
        stored.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = len(stored)

    db.add.side_effect = add
    db.flush = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    q = MagicMock()
    q.filter.return_value.scalar.return_value = 0
    db.query.return_value = q

    trade, call, put = await _persist_strangle_trade(
        db=db,
        account=account,
        payload=payload,
        underlying="BTC",
        expiry=date(2099, 12, 31),
        call_fill_price=21.0,
        put_fill_price=19.0,
        call_order_id="c1",
        put_order_id="p1",
        monitoring_starts=MagicMock(),
    )
    assert trade.position_side == "long"
    assert trade.target_usd == 150
    assert trade.sl_usd == 100
    assert trade.peak_gross_mtm == 0
    assert trade.trail_sl_level == -100
    assert call.is_long is True
    assert put.is_long is True
    assert call.side == "BUY"
    assert put.side == "BUY"


def test_trail_levels_and_ratchet() -> None:
    sl = 100.0
    step = 10.0
    amt = 5.0
    lv37 = compute_long_trail_sl_level(
        sl_usd=sl, peak=37, trail_step_usd=step, trail_amt_usd=amt, prev_sl_level=None
    )
    assert lv37 == -85
    lv140 = compute_long_trail_sl_level(
        sl_usd=sl, peak=140, trail_step_usd=step, trail_amt_usd=amt, prev_sl_level=lv37
    )
    assert lv140 == -30
    trade = SimpleNamespace(
        id=0,
        sl_usd=100,
        target_usd=1000,
        trail_step_usd=10,
        trail_amt_usd=5,
        peak_gross_mtm=140,
        trail_sl_level=-30,
        position_side="long",
    )
    hold = apply_long_gross_exits(trade, 20.0)
    assert hold is None
    assert trade.trail_sl_level == -30
    assert trade.peak_gross_mtm == 140
    hit = apply_long_gross_exits(trade, -31.0)
    assert hit is not None
    assert hit.exit_reason == ExitReason.LONG_TRAIL_SL.value
    assert is_stoploss_exit_reason(hit.exit_reason)


def test_no_trail_sl_and_target() -> None:
    trade = SimpleNamespace(
        id=0,
        sl_usd=100,
        target_usd=150,
        trail_step_usd=None,
        trail_amt_usd=None,
        peak_gross_mtm=0,
        trail_sl_level=-100,
        position_side="long",
    )
    sl_hit = apply_long_gross_exits(trade, -100.0)
    assert sl_hit is not None
    assert sl_hit.exit_reason == ExitReason.LONG_SL.value
    assert is_stoploss_exit_reason(sl_hit.exit_reason)
    trade2 = SimpleNamespace(
        id=0,
        sl_usd=100,
        target_usd=150,
        trail_step_usd=None,
        trail_amt_usd=None,
        peak_gross_mtm=0,
        trail_sl_level=-100,
        position_side="long",
    )
    tp = apply_long_gross_exits(trade2, 150.0)
    assert tp is not None
    assert tp.exit_reason == ExitReason.LONG_TARGET.value
    assert tp.exit_reason == ExitReason.LONG_TARGET.value
    assert not is_stoploss_exit_reason(tp.exit_reason)


def test_restart_loads_peak_140_sl_minus_30() -> None:
    trade = SimpleNamespace(
        id=0,
        sl_usd=100,
        target_usd=1000,
        trail_step_usd=10,
        trail_amt_usd=5,
        peak_gross_mtm=140,
        trail_sl_level=-30,
        position_side="long",
    )
    act = apply_long_gross_exits(trade, 20.0)
    assert act is None
    assert trade.trail_sl_level == -30


def test_expiry_settlement_intrinsic_no_orders() -> None:
    call = _leg(is_long=True, leg_type="call", strike=109000, entry=50)
    put = _leg(is_long=True, leg_type="put", strike=109000, entry=50)
    put.product_id = 12
    trade = SimpleNamespace(
        id=3, status=TradeStatus.ACTIVE.value, exit_reason=None, realized_pnl=0
    )
    client = AsyncMock()
    client.place_order = AsyncMock()
    apply_expiry_settlement(trade=trade, legs=[call, put], spot=110000)
    assert call.exit_premium == 1000
    assert put.exit_premium == 0
    assert call.exit_fee_usd == 0
    assert trade.exit_reason == ExitReason.EXPIRY_SETTLED.value
    assert trade.status == TradeStatus.CLOSED.value
    assert client.place_order.await_count == 0
    assert option_intrinsic("call", 109000, 110000) == 1000
    assert option_intrinsic("put", 109000, 110000) == 0


def _flat_client() -> AsyncMock:
    client = AsyncMock()
    client.place_order = AsyncMock()
    client.resolve_fill_price = AsyncMock(return_value=12.0)
    client.get_product_exit_fill_since = AsyncMock(return_value=12.0)
    client.get_position_size = AsyncMock(return_value=0.0)
    return client


def _long_pair() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    call = _leg(is_long=True, leg_type="call", strike=109000)
    put = _leg(is_long=True, leg_type="put", strike=109000)
    put.product_id = 12
    trade = SimpleNamespace(
        id=4, status=TradeStatus.ACTIVE.value, exit_reason=None, realized_pnl=0
    )
    return call, put, trade


_FLAT_POS = [
    {"product_id": 11, "size": 0},
    {"product_id": 12, "size": 0},
]


@pytest.mark.asyncio
async def test_empty_positions_list_does_not_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since={},
        now_monotonic=1000.0,
    )
    assert reason is None
    assert str(call.status).lower() == "open"
    assert str(put.status).lower() == "open"
    assert client.place_order.await_count == 0


@pytest.mark.asyncio
async def test_empty_list_two_tick_size_zero_external_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    timers: dict[tuple[int, int], float] = {}
    first = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    assert first is None
    assert str(call.status).lower() == "open"
    assert str(put.status).lower() == "open"
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since=timers,
        now_monotonic=1060.0,
    )
    assert reason == ExitReason.EXTERNAL_CLOSE.value
    assert trade.status == TradeStatus.CLOSED.value
    assert client.place_order.await_count == 0
    assert str(call.status).lower() == "closed"
    assert str(put.status).lower() == "closed"


@pytest.mark.asyncio
async def test_empty_list_size_none_does_not_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    client.get_position_size = AsyncMock(return_value=None)
    timers: dict[tuple[int, int], float] = {}
    first = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    second = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since=timers,
        now_monotonic=1060.0,
    )
    assert first is None
    assert second is None
    assert str(call.status).lower() == "open"
    assert str(put.status).lower() == "open"
    assert trade.status == TradeStatus.ACTIVE.value
    assert client.place_order.await_count == 0


@pytest.mark.asyncio
async def test_empty_list_one_flat_one_open_partial_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()

    async def _size(pid: int) -> float:
        return 0.0 if int(pid) == 11 else 1.0

    client.get_position_size = AsyncMock(side_effect=_size)
    timers: dict[tuple[int, int], float] = {}
    await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[],
        db=None,
        flat_since=timers,
        now_monotonic=1060.0,
    )
    assert reason is None
    assert str(call.status).lower() == "closed"
    assert str(put.status).lower() == "open"
    assert trade.status == TradeStatus.ACTIVE.value
    assert client.place_order.await_count == 0


@pytest.mark.asyncio
async def test_one_tick_flat_does_not_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    timers: dict[tuple[int, int], float] = {}
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    assert reason is None
    assert str(call.status).lower() == "open"
    assert str(put.status).lower() == "open"
    assert timers


@pytest.mark.asyncio
async def test_two_tick_flat_60s_external_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    timers: dict[tuple[int, int], float] = {}
    first = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    assert first is None
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1060.0,
    )
    assert reason == ExitReason.EXTERNAL_CLOSE.value
    assert trade.status == TradeStatus.CLOSED.value
    assert client.place_order.await_count == 0
    assert str(call.status).lower() == "closed"
    assert str(put.status).lower() == "closed"


@pytest.mark.asyncio
async def test_flat_open_flat_under_60s_does_not_close() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    timers: dict[tuple[int, int], float] = {}
    await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    client.get_position_size = AsyncMock(return_value=1.0)
    await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=[
            {"product_id": 11, "size": 1},
            {"product_id": 12, "size": 1},
        ],
        db=None,
        flat_since=timers,
        now_monotonic=1030.0,
    )
    client.get_position_size = AsyncMock(return_value=0.0)
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1050.0,
    )
    assert reason is None
    assert str(call.status).lower() == "open"
    assert str(put.status).lower() == "open"


@pytest.mark.asyncio
async def test_pre_expiry_external_close_no_orders() -> None:
    call, put, trade = _long_pair()
    client = _flat_client()
    timers: dict[tuple[int, int], float] = {}
    await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1000.0,
    )
    reason = await apply_pre_expiry_external_close(
        client=client,
        trade=trade,
        legs=[call, put],
        positions=_FLAT_POS,
        db=None,
        flat_since=timers,
        now_monotonic=1060.0,
    )
    assert reason == ExitReason.EXTERNAL_CLOSE.value
    assert client.place_order.await_count == 0


@pytest.mark.asyncio
async def test_official_settlement_price_used() -> None:
    call, put, trade = _long_pair()
    client = AsyncMock()
    client.get_official_settlement_price = AsyncMock(return_value=110000.0)
    client.get_btc_index_price = AsyncMock(return_value=99999.0)
    spot, src = await resolve_settlement_spot(client, [call, put])
    assert spot == 110000.0
    assert src == "official"
    apply_expiry_settlement(
        trade=trade, legs=[call, put], spot=spot, settlement_src=src
    )
    assert trade.settlement_src == "official"
    client.get_btc_index_price.assert_not_called()


@pytest.mark.asyncio
async def test_settlement_index_fallback_warns(caplog: pytest.LogCaptureFixture) -> None:
    call, put, _trade = _long_pair()
    client = AsyncMock()
    client.get_official_settlement_price = AsyncMock(return_value=None)
    client.get_btc_index_price = AsyncMock(return_value=110000.0)
    with caplog.at_level("WARNING"):
        spot, src = await resolve_settlement_spot(client, [call, put])
    assert spot == 110000.0
    assert src == "index_fallback"
    assert any("settlement fallback to index" in r.message for r in caplog.records)


def test_stoploss_markers_include_long() -> None:
    assert is_stoploss_exit_reason("LONG_SL")
    assert is_stoploss_exit_reason("LONG_TRAIL_SL")
    assert is_stoploss_exit_reason("STOPLOSS")


def _register_payload(**kwargs: object) -> TradeRegisterExistingRequest:
    data: dict[str, object] = {
        "underlying": "BTC",
        "expiry_date": "2099-12-31",
        "call_strike": 110000,
        "call_product_id": 11,
        "call_symbol": "C-BTC-110000-311299",
        "put_strike": 100000,
        "put_product_id": 12,
        "put_symbol": "P-BTC-100000-311299",
        "quantity": 1,
        "trigger_mode": "flat",
        "flat_trigger_pct": 150,
        "position_side": "long",
        "target_usd": 150,
        "sl_usd": 100,
        "call_entry_premium": 20.0,
        "put_entry_premium": 18.0,
    }
    data.update(kwargs)
    return TradeRegisterExistingRequest(**data)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_long_initiate_adjustment_enabled_422() -> None:
    with patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True):
        with pytest.raises(HTTPException) as ei:
            await initiate_trade(
                _payload(adjustment_enabled=True),
                db=MagicMock(),
            )
    assert ei.value.status_code == 422
    assert "Adjustment not supported for long trades yet" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_register_existing_long_flag_off_403() -> None:
    with pytest.raises(HTTPException) as ei:
        await register_existing_trade(_register_payload(), db=MagicMock())
    assert ei.value.status_code == 403
    assert "disabled" in str(ei.value.detail).lower()


@pytest.mark.asyncio
async def test_register_existing_long_target_zero_422() -> None:
    with patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True):
        with pytest.raises(HTTPException) as ei:
            await register_existing_trade(
                _register_payload(target_usd=0),
                db=MagicMock(),
            )
    assert ei.value.status_code == 422
    assert "target_usd" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_long_adj_enabled_trigger_does_not_call_executor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strat = ShortStrangleStrategy()
    call = _leg(is_long=True, leg_type="call", entry=100.0)
    call.status = "open"
    call.trigger_baseline_premium = 100.0
    call.trigger_premium = 100.0
    put = _leg(is_long=True, leg_type="put", entry=100.0)
    put.status = "open"
    put.trigger_baseline_premium = 100.0
    put.trigger_premium = 100.0
    trade = SimpleNamespace(
        id=9,
        profit_target_usd=10_000.0,
        stoploss_usd=10_000.0,
        sl_usd=10_000.0,
        target_usd=10_000.0,
        trail_step_usd=None,
        trail_amt_usd=None,
        peak_gross_mtm=0.0,
        trail_sl_level=None,
        trigger_mode="flat",
        combined_trigger_mode=False,
        expiry_date=date.today() + timedelta(days=2),
        monitoring_starts_at=None,
        adjust_settling_until=None,
        slippage_pct=0.0,
        entry_spread_for_sl_usd=0.0,
        position_side="long",
        adjustment_enabled=True,
        is_demo=False,
        universal_sl_pct=200,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    with caplog.at_level("ERROR"):
        with patch(
            "backend.strategies.s001_short_strangle.adjustment.AdjustmentExecutor.execute",
            new_callable=AsyncMock,
        ) as exe:
            action = await strat.on_tick(
                trade,
                call,
                put,
                250.0,
                90.0,
                db,
                realized_pnl=0.0,
                delta_mtm=-0.05,
                net_mtm=-0.05,
                slippage_pct=0.0,
                gross_mtm_for_sl=-0.05,
            )
    assert action.should_adjust is False
    assert action.adjust_leg is None
    assert exe.await_count == 0
    assert any(
        "adjustment_enabled=True on long trade" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_long_initiate_tracker_readable_without_session() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.database import Base
    from backend.engine.position_tracker import PositionTracker
    from backend.models import Account

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    acc = Account(name="long-init", api_key_encrypted="x", api_secret_encrypted="y")
    db.add(acc)
    db.commit()
    db.refresh(acc)

    client = AsyncMock()
    client.get_mark_price = AsyncMock(return_value=20.0)
    client.resolve_fill_price = AsyncMock(return_value=20.0)
    client.close = AsyncMock()
    buy = AsyncMock(
        return_value=OrderResult(success=True, filled_price=20.0, order_id=101)
    )
    tracker = PositionTracker()
    eng = MagicMock()
    eng.order_executor.buy_option = buy
    eng.order_executor.sell_option = AsyncMock()
    eng.order_executor.close_leg = AsyncMock()
    eng.position_tracker = tracker
    eng._push_error = AsyncMock()

    with (
        patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True),
        patch("backend.api.routes_trade._get_active_account", return_value=acc),
        patch(
            "backend.api.routes_trade._ensure_no_active_trade",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade._build_delta_client", return_value=client),
        patch(
            "backend.api.routes_trade._manual_entry_triple_guard",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.api.routes_trade._verify_leg_on_delta",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade.bot_engine", eng),
        patch(
            "backend.api.routes_trade.ws_manager.broadcast",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade.log_and_buffer"),
        patch(
            "backend.api.routes_trade.get_or_create_auto_settings",
            return_value=SimpleNamespace(entry_settling_seconds=0),
        ),
    ):
        result = await initiate_trade(
            _payload(trail_step_usd=10, trail_amt_usd=5),
            db=db,
        )

    assert result["success"] is True
    assert "warning" not in result
    tid = int(result["trade_id"])
    state = tracker.get(tid)
    assert state is not None
    db.close()
    assert state.call_leg.symbol
    assert state.put_leg.initial_premium > 0
    assert str(state.trade.position_side).lower() == "long"


@pytest.mark.asyncio
async def test_long_initiate_tracker_add_fail_returns_200_warning() -> None:
    account = SimpleNamespace(id=1)
    db = MagicMock()
    client = AsyncMock()
    client.get_mark_price = AsyncMock(return_value=20.0)
    client.resolve_fill_price = AsyncMock(return_value=20.0)
    client.close = AsyncMock()
    buy = AsyncMock(
        return_value=OrderResult(success=True, filled_price=20.0, order_id=101)
    )

    async def fake_persist(**kwargs: object) -> tuple[Any, Any, Any]:
        trade = SimpleNamespace(
            id=16,
            position_side="long",
            is_demo=False,
            expiry_date=date(2099, 12, 31),
            underlying="BTC",
            basket_number=16,
            monitoring_starts_at=None,
            profit_target_usd=150.0,
            stoploss_usd=100.0,
            initial_max_profit=0.0,
            tp_pct=50.0,
            sl_pct=100.0,
        )
        call = SimpleNamespace(id=1, is_long=True, side="BUY", sl_trigger_price=None)
        put = SimpleNamespace(id=2, is_long=True, side="BUY", sl_trigger_price=None)
        return trade, call, put

    tracker = MagicMock()
    tracker.add.side_effect = RuntimeError("boom")
    tracker.get = MagicMock(return_value=None)
    tracker.get_all_active = MagicMock(return_value=[])
    eng = MagicMock()
    eng.order_executor.buy_option = buy
    eng.order_executor.sell_option = AsyncMock()
    eng.order_executor.close_leg = AsyncMock()
    eng.position_tracker = tracker
    eng._push_error = AsyncMock()

    with (
        patch("backend.api.routes_trade.LONG_TRADES_ENABLED", True),
        patch("backend.api.routes_trade._get_active_account", return_value=account),
        patch(
            "backend.api.routes_trade._ensure_no_active_trade",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade._build_delta_client", return_value=client),
        patch(
            "backend.api.routes_trade._manual_entry_triple_guard",
            new_callable=AsyncMock,
        ),
        patch(
            "backend.api.routes_trade._verify_leg_on_delta",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade._persist_strangle_trade", new=fake_persist),
        patch("backend.api.routes_trade.bot_engine", eng),
        patch(
            "backend.api.routes_trade.ws_manager.broadcast",
            new_callable=AsyncMock,
        ),
        patch("backend.api.routes_trade.log_and_buffer"),
    ):
        result = await initiate_trade(
            _payload(trail_step_usd=10, trail_amt_usd=5),
            db=db,
        )

    assert result["success"] is True
    assert "warning" in result
    assert "boom" in str(result["warning"])


def test_long_leg_snapshot_pnl_from_bid() -> None:
    leg = SimpleNamespace(
        id=1,
        leg_type="put",
        strike=100000.0,
        symbol="P-BTC",
        quantity=10,
        initial_premium=569.0,
        trigger_baseline_premium=569.0,
        trigger_premium=569.0,
        status="open",
        is_long=True,
        side="BUY",
        exit_premium=None,
        entry_time=None,
        exit_time=None,
        realized_pnl=None,
        entry_fee_usd=None,
        exit_fee_usd=None,
    )
    snap = _leg_snapshot(leg, 539.0)
    assert snap["leg_pnl"] == -0.30
    short = SimpleNamespace(
        id=2,
        leg_type="put",
        strike=100000.0,
        symbol="P-BTC",
        quantity=10,
        initial_premium=569.0,
        trigger_baseline_premium=569.0,
        trigger_premium=569.0,
        status="open",
        is_long=False,
        side="SELL",
        exit_premium=None,
        entry_time=None,
        exit_time=None,
        realized_pnl=None,
        entry_fee_usd=None,
        exit_fee_usd=None,
    )
    short_snap = _leg_snapshot(short, 548.0)
    assert short_snap["leg_pnl"] == pytest.approx((569.0 - 548.0) * 10 * 0.001)


@pytest.mark.asyncio
async def test_get_positions_upnl_long_uses_bid_not_ask() -> None:
    from backend.core.delta_client import DeltaClient

    client = DeltaClient("k", "s")
    client._request = AsyncMock(
        return_value=[
            {
                "product_id": 1,
                "size": 10,
                "entry_price": 569,
                "mark_price": 545,
                "best_ask_price": 548,
                "best_bid_price": 539,
                "contract_value": 0.001,
                "product_symbol": "P-BTC-1",
            },
            {
                "product_id": 2,
                "size": -10,
                "entry_price": 569,
                "mark_price": 545,
                "best_ask_price": 548,
                "best_bid_price": 539,
                "contract_value": 0.001,
                "product_symbol": "C-BTC-2",
            },
        ]
    )
    try:
        out = await client.get_positions_upnl([1, 2])
        assert out[1]["upnl"] == -0.30
        assert out[1]["best_bid"] == 539.0
        assert out[2]["upnl"] == pytest.approx(0.21)
        assert out[2]["best_offer"] == 548.0
    finally:
        await client.client.aclose()


def test_long_target_on_net_sl_on_gross() -> None:
    from backend.core.fees import compute_net_mtm

    net_hold = compute_net_mtm(
        gross_mtm=1.2,
        fees_paid=0.2,
        est_exit_fees=0.2,
        slippage_pct=0.0,
    )["net_mtm"]
    assert net_hold == pytest.approx(0.8)
    trade = SimpleNamespace(
        id=0,
        sl_usd=2.0,
        target_usd=1.0,
        trail_step_usd=None,
        trail_amt_usd=None,
        peak_gross_mtm=0.0,
        trail_sl_level=-2.0,
        position_side="long",
    )
    hold = apply_long_gross_exits(trade, 1.2, net_mtm=net_hold)
    assert hold is None
    net_tp = compute_net_mtm(
        gross_mtm=1.5,
        fees_paid=0.2,
        est_exit_fees=0.2,
        slippage_pct=0.0,
    )["net_mtm"]
    assert net_tp == pytest.approx(1.1)
    trade_tp = SimpleNamespace(
        id=0,
        sl_usd=2.0,
        target_usd=1.0,
        trail_step_usd=None,
        trail_amt_usd=None,
        peak_gross_mtm=0.0,
        trail_sl_level=-2.0,
        position_side="long",
    )
    tp = apply_long_gross_exits(trade_tp, 1.5, net_mtm=net_tp)
    assert tp is not None
    assert tp.exit_reason == ExitReason.LONG_TARGET.value
    trade_sl = SimpleNamespace(
        id=0,
        sl_usd=2.0,
        target_usd=10.0,
        trail_step_usd=None,
        trail_amt_usd=None,
        peak_gross_mtm=0.0,
        trail_sl_level=-2.0,
        position_side="long",
    )
    sl = apply_long_gross_exits(trade_sl, -2.0, net_mtm=-2.4)
    assert sl is not None
    assert sl.exit_reason == ExitReason.LONG_SL.value
