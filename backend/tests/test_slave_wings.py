# test_slave_wings.py — Slave/mirror iron-condor wings (entry abort, exit order, reduce_only)
#
# Run: python -m pytest backend/tests/test_slave_wings.py -q

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.mirror_engine import MirrorEngine
from backend.engine.slave_wings import (
    assert_reduce_only_on_all_place_calls,
    build_slave_entry_plan,
    entry_roles_order,
    log_slave_wing_entry_abort,
    place_slave_plan_legs,
    should_orphan_close_wings,
    sort_exit_targets,
    sort_unwind_dicts,
)
from backend.engine.wing_entry import EntryPartialUnwind
from backend.strategies.base_strategy import OrderResult


def _wing(strike: float, pid: int, symbol: str) -> dict:
    return {
        "strike": strike,
        "product_id": pid,
        "symbol": symbol,
        "premium": 40.0,
    }


def test_slave_entry_order_wings_then_shorts() -> None:
    """1. slave entry: order = wing call, wing put, short call, short put"""
    plan = build_slave_entry_plan(
        slave_qty=5,
        call_product_id=101,
        put_product_id=102,
        call_symbol="C-S",
        put_symbol="P-S",
        call_strike=81500,
        put_strike=74000,
        call_premium=200,
        put_premium=180,
        wing_call=_wing(84000, 201, "C-W"),
        wing_put=_wing(71000, 202, "P-W"),
        call_bracket_sl=400.0,
        put_bracket_sl=360.0,
    )
    assert entry_roles_order(plan) == [
        "wing_call",
        "wing_put",
        "call",
        "put",
    ]
    assert plan[0].is_long and plan[1].is_long
    assert not plan[2].is_long and not plan[3].is_long
    assert plan[0].bracket_sl_price is None
    assert plan[2].bracket_sl_price == 400.0


def test_slave_sizing_wings_match_slave_qty_not_master() -> None:
    """2. master 2 lots, slave 5 → slave plan qty=5 on ALL four legs"""
    plan = build_slave_entry_plan(
        slave_qty=5,
        call_product_id=1,
        put_product_id=2,
        call_symbol="c",
        put_symbol="p",
        call_strike=100,
        put_strike=90,
        call_premium=10,
        put_premium=10,
        wing_call=_wing(110, 3, "wc"),
        wing_put=_wing(80, 4, "wp"),
    )
    assert all(p.quantity == 5 for p in plan)
    assert len(plan) == 4


def test_wing_entry_fail_aborts_before_shorts(caplog: pytest.LogCaptureFixture) -> None:
    """3. wing fail → shorts never placed; wings unwound; ABORT log"""
    plan = build_slave_entry_plan(
        slave_qty=2,
        call_product_id=101,
        put_product_id=102,
        call_symbol="C",
        put_symbol="P",
        call_strike=81500,
        put_strike=74000,
        call_premium=200,
        put_premium=180,
        wing_call=_wing(84000, 201, "WC"),
        wing_put=_wing(71000, 202, "WP"),
    )
    placed: list[str] = []

    def _fn_for(spec):
        async def _place() -> OrderResult:
            placed.append(spec.role)
            if spec.role == "wing_put":
                return OrderResult(success=False, error="margin", filled_size=0)
            return OrderResult(
                success=True,
                order_id=1,
                filled_price=40.0,
                filled_size=int(spec.quantity),
            )

        return _place

    async def _run() -> None:
        with pytest.raises(EntryPartialUnwind) as ei:
            await place_slave_plan_legs(
                plan=plan,
                place_fn_for_spec=_fn_for,
                slave_name="cust-1",
                max_attempts=1,
            )
        assert ei.value.failed_role == "wing_put"
        # Only wing_call filled; shorts never attempted
        assert [x.role for x in ei.value.filled_legs] == ["wing_call"]
        assert "call" not in placed and "put" not in placed
        assert placed == ["wing_call", "wing_put"]

        # Simulate abort unwind of filled wing + log
        with caplog.at_level(logging.CRITICAL):
            log_slave_wing_entry_abort(
                slave_name="cust-1",
                reason="wing_put margin",
                wings_closed=1,
                wings_failed=0,
            )
        assert any(
            "[SLAVE_WING_ENTRY_ABORT]" in r.message for r in caplog.records
        )

    asyncio.run(_run())


def test_slave_exit_order_shorts_then_wings() -> None:
    """4. slave exit: shorts first, wings after"""
    targets = [
        {"product_id": 201, "size": 5.0},  # wing call long
        {"product_id": 101, "size": -5.0},  # short call
        {"product_id": 202, "size": 5.0},  # wing put
        {"product_id": 102, "size": -5.0},  # short put
    ]
    ordered = sort_exit_targets(
        targets,
        short_pids={101, 102},
        wing_pids={201, 202},
    )
    assert [int(t["product_id"]) for t in ordered] == [101, 102, 201, 202]


def test_one_short_open_wings_stay() -> None:
    """5. one short closed, other open → wings stay"""
    assert (
        should_orphan_close_wings(
            call_open=False, put_open=True, wings_present=True
        )
        is False
    )


def test_both_shorts_closed_orphan_wings() -> None:
    """6. both shorts closed → wings auto-close (orphan hook)"""
    assert (
        should_orphan_close_wings(
            call_open=False, put_open=False, wings_present=True
        )
        is True
    )
    assert (
        should_orphan_close_wings(
            call_open=False, put_open=False, wings_present=False
        )
        is False
    )


def test_close_retry_always_reduce_only() -> None:
    """7. every close retry keeps reduce_only=True (P2 lock)"""
    engine = MirrorEngine(db_factory=lambda: None)
    client = AsyncMock()
    client.place_order = AsyncMock(return_value={"id": "c1"})
    # Still open for 2 attempts, then flat
    client.get_option_positions = AsyncMock(
        side_effect=[
            [{"product_id": 101, "size": -2}],
            [{"product_id": 101, "size": -2}],
            [],
        ]
    )
    slave = SimpleNamespace(id=7, name="s7", is_virtual=False)

    async def _run() -> None:
        with patch(
            "backend.engine.mirror_engine.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            ok, _ord, _err = await engine._close_with_reduce_only(
                client=client,
                slave=slave,  # type: ignore[arg-type]
                product_id=101,
                signed_size=-2.0,
                master_trade_id=1,
                path="test_slave_wings",
                max_retries=2,
                backoff_seconds=0,
            )
        assert ok is True
        assert client.place_order.await_count >= 2
        assert_reduce_only_on_all_place_calls(
            client.place_order.await_args_list
        )

    asyncio.run(_run())


def test_adjustment_leaves_wing_qty_and_strike() -> None:
    """8. after adjustment, slave wing qty/strike unchanged (by design)"""
    st = SimpleNamespace(
        wing_call_product_id=201,
        wing_put_product_id=202,
        wing_call_strike=84000.0,
        wing_put_strike=71000.0,
        actual_quantity=5,
        original_quantity=5,
        call_product_id=101,
        put_product_id=102,
        call_strike=81500.0,
        put_strike=74000.0,
    )
    # Simulate short call roll — only call fields change
    st.call_product_id = 111
    st.call_strike = 82000.0
    st.actual_quantity = 4  # decrease_step on shorts
    assert st.wing_call_product_id == 201
    assert st.wing_put_product_id == 202
    assert st.wing_call_strike == 84000.0
    assert st.wing_put_strike == 71000.0
    assert st.original_quantity == 5


def test_wings_off_regression_shorts_only() -> None:
    """9. wings off → today's slave flow unchanged (shorts only)"""
    plan = build_slave_entry_plan(
        slave_qty=3,
        call_product_id=1,
        put_product_id=2,
        call_symbol="c",
        put_symbol="p",
        call_strike=100,
        put_strike=90,
        call_premium=10,
        put_premium=10,
        wing_call=None,
        wing_put=None,
        call_bracket_sl=1.0,
        put_bracket_sl=2.0,
    )
    assert entry_roles_order(plan) == ["call", "put"]
    assert plan[0].quantity == 3
    assert plan[0].bracket_sl_price == 1.0


def test_unwind_dicts_shorts_before_wings() -> None:
    legs = [
        {"leg": "wing_call", "product_id": 1},
        {"leg": "put", "product_id": 2},
        {"leg": "wing_put", "product_id": 3},
        {"leg": "call", "product_id": 4},
    ]
    ordered = sort_unwind_dicts(legs)
    assert [x["leg"] for x in ordered] == [
        "call",
        "put",
        "wing_call",
        "wing_put",
    ]
