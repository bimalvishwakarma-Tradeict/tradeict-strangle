# test_slave_close_and_naked.py — verify close fill + naked/partial repair
#
# Run: python -m pytest backend/tests/test_slave_close_and_naked.py -q
# (from trading-bot/ root)

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.mirror_engine import MirrorEngine


def _slave(*, sid: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=sid,
        name=f"slave-{sid}",
        is_virtual=False,
        is_active=True,
        connection_status="connected",
        last_error=None,
    )


def _engine() -> MirrorEngine:
    return MirrorEngine(db_factory=lambda: None)


def test_close_accepted_but_still_open_returns_false() -> None:
    """IOC accept without fill must not report success."""
    engine = _engine()
    client = AsyncMock()
    client.place_order = AsyncMock(return_value={"id": "ord-1"})
    client.get_option_positions = AsyncMock(
        return_value=[{"product_id": 101, "size": -1}]
    )
    slave = _slave()

    async def _run() -> tuple[bool, object, str]:
        with patch(
            "backend.engine.mirror_engine.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            return await engine._close_with_reduce_only(
                client=client,
                slave=slave,  # type: ignore[arg-type]
                product_id=101,
                signed_size=-1.0,
                master_trade_id=9,
                path="test",
                max_retries=1,
                backoff_seconds=0,
            )

    ok, order, err = asyncio.run(_run())
    assert ok is False
    assert order is not None
    assert "not_flat" in err or "live_size" in err
    assert client.place_order.await_count >= 1
    for call in client.place_order.await_args_list:
        assert call.kwargs.get("reduce_only") is True


def test_close_accepted_and_flat_returns_true() -> None:
    engine = _engine()
    client = AsyncMock()
    client.place_order = AsyncMock(return_value={"id": "ord-2"})
    client.get_option_positions = AsyncMock(return_value=[])
    slave = _slave()

    async def _run() -> tuple[bool, object, str]:
        with patch(
            "backend.engine.mirror_engine.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            return await engine._close_with_reduce_only(
                client=client,
                slave=slave,  # type: ignore[arg-type]
                product_id=202,
                signed_size=-2.0,
                master_trade_id=9,
                path="test",
                max_retries=1,
                backoff_seconds=0,
            )

    ok, order, err = asyncio.run(_run())
    assert ok is True
    assert err == ""
    assert order == {"id": "ord-2"}
    assert client.place_order.await_args.kwargs.get("reduce_only") is True


def test_close_reduce_only_unsupported_refuses_and_untouched() -> None:
    engine = _engine()
    client = AsyncMock()
    client.place_order = AsyncMock(
        side_effect=RuntimeError("reduce_only not supported for this account")
    )
    client.get_option_positions = AsyncMock(
        return_value=[{"product_id": 303, "size": -1}]
    )
    slave = _slave()

    async def _run() -> tuple[bool, object, str]:
        with patch(
            "backend.engine.mirror_engine.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            return await engine._close_with_reduce_only(
                client=client,
                slave=slave,  # type: ignore[arg-type]
                product_id=303,
                signed_size=-1.0,
                master_trade_id=9,
                path="test",
                max_retries=2,
                backoff_seconds=0,
            )

    ok, order, err = asyncio.run(_run())
    assert ok is False
    assert order is None
    assert err == "reduce_only_unsupported"
    assert client.place_order.await_count == 1
    assert client.place_order.await_args.kwargs.get("reduce_only") is True


def test_partial_entry_open_in_both_sweep_status_sets() -> None:
    engine = _engine()
    assert "partial_entry_open" in engine._ORPHAN_BASKET_OPEN_STATUSES
    assert "partial_entry_open" in engine._INTEGRITY_PROBLEM_STATUSES


def test_naked_one_leg_bot_owned_attempts_reduce_only_close() -> None:
    engine = _engine()
    slave = _slave(sid=11)
    slave_trade = SimpleNamespace(
        id=55,
        slave_account_id=11,
        master_trade_id=77,
        status="active",
        call_product_id=501,
        put_product_id=502,
        error_count=0,
        last_error=None,
        last_updated=None,
    )
    db = MagicMock()

    client = AsyncMock()
    client.get_option_positions = AsyncMock(
        return_value=[{"product_id": 501, "size": -1}]
    )
    client.close = AsyncMock()
    engine._get_slave_client = MagicMock(return_value=client)  # type: ignore[method-assign]
    engine._bot_owned_product_ids = MagicMock(return_value={501, 502})  # type: ignore[method-assign]
    engine._close_with_reduce_only = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, {"id": "c1"}, "")
    )
    engine._close_slave_trade = MagicMock(return_value=True)  # type: ignore[method-assign]

    asyncio.run(
        engine._check_slave_active_book_integrity(
            db=db,
            slave=slave,  # type: ignore[arg-type]
            slave_trade=slave_trade,  # type: ignore[arg-type]
            master_open_legs=2,
        )
    )

    engine._close_with_reduce_only.assert_awaited_once()
    kwargs = engine._close_with_reduce_only.await_args.kwargs
    assert kwargs["product_id"] == 501
    assert kwargs["signed_size"] == -1.0
    assert kwargs["path"] == "naked_one_leg"
    engine._close_slave_trade.assert_called_once()


def test_naked_one_leg_foreign_short_closes_nothing() -> None:
    engine = _engine()
    slave = _slave(sid=12)
    slave_trade = SimpleNamespace(
        id=56,
        slave_account_id=12,
        master_trade_id=78,
        status="active",
        call_product_id=601,
        put_product_id=602,
        error_count=0,
        last_error=None,
        last_updated=None,
    )
    db = MagicMock()

    client = AsyncMock()
    client.get_option_positions = AsyncMock(
        return_value=[{"product_id": 999, "size": -1}]
    )
    client.close = AsyncMock()
    engine._get_slave_client = MagicMock(return_value=client)  # type: ignore[method-assign]
    engine._bot_owned_product_ids = MagicMock(return_value={601, 602})  # type: ignore[method-assign]
    engine._close_with_reduce_only = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, {"id": "should-not"}, "")
    )
    engine._close_slave_trade = MagicMock(return_value=True)  # type: ignore[method-assign]

    asyncio.run(
        engine._check_slave_active_book_integrity(
            db=db,
            slave=slave,  # type: ignore[arg-type]
            slave_trade=slave_trade,  # type: ignore[arg-type]
            master_open_legs=2,
        )
    )

    engine._close_with_reduce_only.assert_not_awaited()
    engine._close_slave_trade.assert_not_called()
    assert slave_trade.status == "partial_adjustment"
    assert "FOREIGN" in str(slave_trade.last_error)
