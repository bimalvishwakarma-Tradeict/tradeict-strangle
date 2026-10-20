# test_long_engine.py — long entry, close_leg side, $ TP/SL trail, settlement

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_trade import _persist_strangle_trade, initiate_trade
from backend.config import ExitReason, TradeStatus
from backend.engine.long_settlement import (
    apply_expiry_settlement,
    apply_pre_expiry_external_close,
    option_intrinsic,
    resolve_settlement_spot,
)
from backend.engine.order_executor import OrderExecutor
from backend.schemas import TradeInitiateRequest
from backend.strategies.base_strategy import OrderResult
from backend.strategies.s001_short_strangle.logic import (
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
