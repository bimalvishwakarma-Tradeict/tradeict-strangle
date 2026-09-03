# test_wing_entry.py — Wings 2/4 entry sequencing + partial unwind
#
# Run: python -m pytest backend/tests/test_wing_entry.py -q

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import ExitReason
from backend.engine.wing_entry import (
    EntryGuardBlock,
    EntryPartialUnwind,
    FilledEntryLeg,
    build_entry_order_plan,
    is_full_fill,
    place_leg_with_retries,
    unwind_partial_entry,
)
from backend.strategies.base_strategy import OrderResult


def _straddle() -> dict:
    return {
        "call_product_id": 101,
        "put_product_id": 102,
        "call_symbol": "C-BTC-70000",
        "put_symbol": "P-BTC-65000",
        "call_strike": 70000.0,
        "put_strike": 65000.0,
        "strike": 67500.0,
        "call_premium": 200.0,
        "put_premium": 180.0,
    }


def _wing(strike: float, pid: int, symbol: str) -> dict:
    return {
        "strike": strike,
        "product_id": pid,
        "symbol": symbol,
        "premium": 40.0,
        "delta": 0.06,
    }


def test_wings_enabled_order_sequence() -> None:
    plan = build_entry_order_plan(
        qty=2,
        straddle=_straddle(),
        wing_call=_wing(72000, 201, "C-W"),
        wing_put=_wing(63000, 202, "P-W"),
        wings_enabled=True,
        call_bracket_sl=400.0,
        call_bracket_limit=420.0,
        put_bracket_sl=360.0,
        put_bracket_limit=380.0,
    )
    assert [p.role for p in plan] == [
        "wing_call",
        "wing_put",
        "call",
        "put",
    ]
    assert plan[0].is_long and plan[1].is_long
    assert not plan[2].is_long and not plan[3].is_long
    assert plan[0].bracket_sl_price is None
    assert plan[1].bracket_sl_price is None
    assert plan[2].bracket_sl_price == 400.0
    assert plan[3].bracket_sl_price == 360.0


def test_wing_strike_none_blocks_no_orders() -> None:
    with pytest.raises(EntryGuardBlock) as ei:
        build_entry_order_plan(
            qty=1,
            straddle=_straddle(),
            wing_call=None,
            wing_put=_wing(63000, 202, "P-W"),
            wings_enabled=True,
        )
    assert ei.value.guard == "no_wing_strike"
    assert ei.value.leg == "call"


def test_wings_disabled_regression_shorts_only() -> None:
    plan = build_entry_order_plan(
        qty=1,
        straddle=_straddle(),
        wing_call=None,
        wing_put=None,
        wings_enabled=False,
        call_bracket_sl=1.0,
        put_bracket_sl=2.0,
    )
    assert [p.role for p in plan] == ["call", "put"]
    assert plan[0].bracket_sl_price == 1.0
    assert plan[1].bracket_sl_price == 2.0


def test_wings_no_bracket_shorts_have_bracket() -> None:
    plan = build_entry_order_plan(
        qty=1,
        straddle=_straddle(),
        wing_call=_wing(72000, 201, "C-W"),
        wing_put=_wing(63000, 202, "P-W"),
        wings_enabled=True,
        call_bracket_sl=111.0,
        put_bracket_sl=222.0,
    )
    wings = [p for p in plan if p.is_long]
    shorts = [p for p in plan if not p.is_long]
    assert all(w.bracket_sl_price is None for w in wings)
    assert shorts[0].bracket_sl_price == 111.0
    assert shorts[1].bracket_sl_price == 222.0


@pytest.mark.asyncio
async def test_short_fails_three_times_then_unwind_shorts_first() -> None:
    attempts = {"n": 0}

    async def fail_fn() -> OrderResult:
        attempts["n"] += 1
        return OrderResult(success=False, error="rejected", filled_size=0)

    last = await place_leg_with_retries(
        role="call",
        requested=5,
        place_fn=fail_fn,
        max_attempts=3,
    )
    assert attempts["n"] == 3
    assert not is_full_fill(last, 5)

    close_order: list[str] = []

    class FakeExec:
        async def close_leg(self, leg, client):
            close_order.append(f"short:{leg.leg_type}")
            return OrderResult(success=True, filled_price=1.0)

        async def close_long_position(self, **kwargs):
            close_order.append(f"long:{kwargs['product_id']}")
            return OrderResult(success=True, filled_price=1.0)

    filled = [
        FilledEntryLeg(
            role="wing_call",
            product_id=201,
            symbol="CW",
            strike=72000,
            requested_qty=5,
            filled_size=5,
            fill_price=40,
            order_id="1",
            commission=0,
            is_long=True,
            mark_premium=40,
        ),
        FilledEntryLeg(
            role="wing_put",
            product_id=202,
            symbol="PW",
            strike=63000,
            requested_qty=5,
            filled_size=5,
            fill_price=35,
            order_id="2",
            commission=0,
            is_long=True,
            mark_premium=35,
        ),
        FilledEntryLeg(
            role="call",
            product_id=101,
            symbol="C",
            strike=70000,
            requested_qty=5,
            filled_size=5,
            fill_price=200,
            order_id="3",
            commission=0,
            is_long=False,
            mark_premium=200,
        ),
    ]
    res = await unwind_partial_entry(
        order_executor=FakeExec(),
        delta_client=MagicMock(),
        filled_legs=filled,
        trade_id=99,
    )
    assert res.legs_closed == 3
    assert res.legs_failed == 0
    # Shorts before wings
    assert close_order[0].startswith("short:")
    assert close_order[1].startswith("long:")
    assert close_order[2].startswith("long:")


def test_exit_reason_entry_partial_unwind_exists() -> None:
    assert ExitReason.ENTRY_PARTIAL_UNWIND.value == "ENTRY_PARTIAL_UNWIND"


def test_entry_partial_unwind_exception_carries_legs() -> None:
    legs = [
        FilledEntryLeg(
            role="wing_call",
            product_id=1,
            symbol="x",
            strike=1,
            requested_qty=1,
            filled_size=1,
            fill_price=1,
            order_id=None,
            commission=None,
            is_long=True,
            mark_premium=1,
        )
    ]
    exc = EntryPartialUnwind("fail", filled_legs=legs, failed_role="call")
    assert exc.failed_role == "call"
    assert len(exc.filled_legs) == 1
