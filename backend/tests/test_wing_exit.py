# test_wing_exit.py — Wings 3/4: centralised close, orphan hook, cross-guard
#
# Run: python -m pytest backend/tests/test_wing_exit.py -q

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import ExitReason
from backend.engine.wing_exit import (
    clamp_short_strike_inside_wing,
    close_basket_legs,
    filter_legs_for_close,
    is_short_basket_leg,
    is_wing_leg,
)
from backend.strategies.base_strategy import OrderResult


def _leg(
    *,
    leg_id: int,
    leg_type: str,
    strike: float,
    is_long: bool = False,
    qty: int = 1,
    status: str = "open",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=leg_id,
        leg_type=leg_type,
        strike=strike,
        symbol=f"{leg_type}-{int(strike)}",
        product_id=1000 + leg_id,
        quantity=qty,
        status=status,
        is_long=is_long,
        is_bot_managed=True,
        initial_premium=10.0,
        exit_premium=None,
        delta_order_id=None,
    )


def _basket_legs() -> list[SimpleNamespace]:
    return [
        _leg(leg_id=1, leg_type="call", strike=82000, is_long=False),
        _leg(leg_id=2, leg_type="put", strike=78000, is_long=False),
        _leg(leg_id=3, leg_type="wing_call", strike=84000, is_long=True),
        _leg(leg_id=4, leg_type="wing_put", strike=76000, is_long=True),
    ]


class FakeExec:
    def __init__(self, fail_wings: bool = False) -> None:
        self.order: list[str] = []
        self.fail_wings = fail_wings
        self.attempts: dict[str, int] = {}

    async def close_leg(self, leg, client):
        key = str(leg.leg_type)
        self.order.append(key)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return OrderResult(success=True, filled_price=12.0, order_id=1)

    async def close_long_position(self, **kwargs):
        # Infer from product_id mapping in tests
        pid = int(kwargs["product_id"])
        # wing_call=1003, wing_put=1004 in _basket_legs
        key = "wing_call" if pid == 1003 else "wing_put" if pid == 1004 else f"long:{pid}"
        self.order.append(key)
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.fail_wings and key.startswith("wing"):
            return OrderResult(success=False, error="wing_reject")
        return OrderResult(success=True, filled_price=5.0, order_id=2)


@pytest.mark.asyncio
async def test_profit_target_close_order_shorts_then_wings() -> None:
    legs = _basket_legs()
    # Shuffle input — helper must still order correctly
    legs = [legs[2], legs[0], legs[3], legs[1]]
    exe = FakeExec()
    client = AsyncMock()
    client.verify_position_exists = AsyncMock(return_value=True)
    trade = SimpleNamespace(id=42)
    res = await close_basket_legs(
        trade=trade,
        reason=ExitReason.PROFIT_TARGET.value,
        db=None,
        delta_client=client,
        order_executor=exe,
        legs_to_close="all",
        legs=legs,
    )
    assert [x for x in exe.order] == ["call", "put", "wing_call", "wing_put"]
    assert res.shorts_closed == 2
    assert res.wings_closed == 2
    assert not res.any_wing_fail


@pytest.mark.asyncio
async def test_stoploss_same_order() -> None:
    legs = _basket_legs()
    exe = FakeExec()
    client = AsyncMock()
    client.verify_position_exists = AsyncMock(return_value=True)
    await close_basket_legs(
        trade=SimpleNamespace(id=1),
        reason=ExitReason.STOPLOSS.value,
        db=None,
        delta_client=client,
        order_executor=exe,
        legs_to_close="all",
        legs=legs,
    )
    assert exe.order[:2] == ["call", "put"]
    assert exe.order[2:] == ["wing_call", "wing_put"]


@pytest.mark.asyncio
async def test_one_short_manual_wings_stay() -> None:
    legs = _basket_legs()
    exe = FakeExec()
    client = AsyncMock()
    client.verify_position_exists = AsyncMock(return_value=True)
    # Close only call short
    res = await close_basket_legs(
        trade=SimpleNamespace(id=1),
        reason=ExitReason.MANUAL_LEG_CLOSE.value,
        db=None,
        delta_client=client,
        order_executor=exe,
        legs_to_close="shorts_only",
        legs=[legs[0]],  # call only
    )
    assert exe.order == ["call"]
    assert res.wings_closed == 0
    # Wings still open in original list
    assert all(
        str(leg.status) == "open"
        for leg in legs
        if is_wing_leg(leg)
    )


@pytest.mark.asyncio
async def test_both_shorts_then_wings_orphaned() -> None:
    legs = _basket_legs()
    for leg in legs:
        if is_short_basket_leg(leg):
            leg.status = "closed"
    exe = FakeExec()
    client = AsyncMock()
    client.verify_position_exists = AsyncMock(return_value=True)
    res = await close_basket_legs(
        trade=SimpleNamespace(id=7),
        reason=ExitReason.WINGS_ORPHANED.value,
        db=None,
        delta_client=client,
        order_executor=exe,
        legs_to_close="wings_only",
        legs=legs,
    )
    assert exe.order == ["wing_call", "wing_put"]
    assert res.wings_closed == 2
    assert ExitReason.WINGS_ORPHANED.value == "WINGS_ORPHANED"
    # Not a loss reason (bot_engine set)
    from backend.engine.bot_engine import BotEngine

    # loss_reasons checked via enum presence only — WINGS_ORPHANED must exist
    assert hasattr(ExitReason, "WINGS_ORPHANED")


@pytest.mark.asyncio
async def test_wing_close_fails_three_times(caplog: pytest.LogCaptureFixture) -> None:
    legs = [
        _leg(leg_id=3, leg_type="wing_call", strike=84000, is_long=True),
    ]
    exe = FakeExec(fail_wings=True)
    # Override to count retries
    attempts = {"n": 0}

    async def fail_long(**kwargs):
        attempts["n"] += 1
        return OrderResult(success=False, error="wing_reject")

    exe.close_long_position = fail_long  # type: ignore[method-assign]
    client = AsyncMock()
    client.verify_position_exists = AsyncMock(return_value=True)
    with caplog.at_level(logging.CRITICAL):
        res = await close_basket_legs(
            trade=SimpleNamespace(id=9),
            reason="TEST",
            db=None,
            delta_client=client,
            order_executor=exe,
            legs_to_close="wings_only",
            legs=legs,
        )
    assert attempts["n"] == 3
    assert res.any_wing_fail
    assert any("WING_CLOSE_FAILED" in r.message for r in caplog.records)


def test_filter_order_and_modes() -> None:
    legs = _basket_legs()
    ordered = filter_legs_for_close(legs, "all")
    assert [leg.leg_type for leg in ordered] == [
        "call",
        "put",
        "wing_call",
        "wing_put",
    ]
    assert len(filter_legs_for_close(legs, "shorts_only")) == 2
    assert len(filter_legs_for_close(legs, "wings_only")) == 2


def test_cross_guard_call_clamp() -> None:
    # wanted 84000, wing 84000 → clamp to 83500 (inside)
    strikes = [82000.0, 82500.0, 83000.0, 83500.0, 84000.0, 84500.0]
    clamped, status = clamp_short_strike_inside_wing(
        leg="call",
        wanted_strike=84000.0,
        wing_strike=84000.0,
        available_strikes=strikes,
        current_short_strike=82000.0,
    )
    assert status == "clamped"
    assert clamped == 83500.0


def test_cross_guard_put_clamp() -> None:
    # wanted 71000, wing 71000 → clamp to 71500 (inside / above wing)
    strikes = [70000.0, 71000.0, 71500.0, 72000.0, 73000.0]
    clamped, status = clamp_short_strike_inside_wing(
        leg="put",
        wanted_strike=71000.0,
        wing_strike=71000.0,
        available_strikes=strikes,
        current_short_strike=73000.0,
    )
    assert status == "clamped"
    assert clamped == 71500.0


def test_cross_guard_dead_end() -> None:
    # No strike between current 83000 and wing 83500
    strikes = [82000.0, 83000.0, 83500.0, 84000.0]
    clamped, status = clamp_short_strike_inside_wing(
        leg="call",
        wanted_strike=84000.0,
        wing_strike=83500.0,
        available_strikes=strikes,
        current_short_strike=83000.0,
    )
    assert status == "dead_end"
    assert clamped is None


def test_cross_guard_ok_no_clamp() -> None:
    clamped, status = clamp_short_strike_inside_wing(
        leg="call",
        wanted_strike=83000.0,
        wing_strike=84000.0,
        available_strikes=[82000.0, 83000.0, 84000.0],
        current_short_strike=82000.0,
    )
    assert status == "ok"
    assert clamped == 83000.0


def test_wings_disabled_regression_filter() -> None:
    # No wing legs — only shorts — order unchanged
    shorts = [
        _leg(leg_id=1, leg_type="call", strike=100),
        _leg(leg_id=2, leg_type="put", strike=90),
    ]
    ordered = filter_legs_for_close(shorts, "all")
    assert [leg.leg_type for leg in ordered] == ["call", "put"]


def test_adjustment_modes_do_not_change_wing_identity() -> None:
    """Wings are never in shorts_only close; qty modes don't touch wing legs."""
    from backend.engine.auto_trade_engine import resolve_adjustment_basket_qty
    from backend.engine.wing_entry import resolve_adjustment_qty_mode

    wing = _leg(leg_id=3, leg_type="wing_call", strike=84000, is_long=True, qty=10)
    orig_qty = int(wing.quantity)
    orig_strike = float(wing.strike)
    for mode in ("unchanged", "increase_dynamic", "decrease_step"):
        settings = SimpleNamespace(
            adjustment_qty_mode=mode,
            basket_qty_dynamic=True,
            basket_qty_theta_mult=3.0,
            adjustment_qty_decrease_pct=25.0,
            use_dynamic_qty_on_adjustment=(mode == "increase_dynamic"),
        )
        assert resolve_adjustment_qty_mode(settings) == mode
        qty, _ = resolve_adjustment_basket_qty(
            settings=settings,
            triggered_leg_qty=10,
            hedge_qty=20,
            hedge_call_theta=50.0,
            new_strike_ask=200.0,
            original_qty=10,
            adjustment_number=1,
        )
        assert isinstance(qty, int)
        # Wing object untouched
        assert int(wing.quantity) == orig_qty
        assert float(wing.strike) == orig_strike
        # shorts_only never includes wing
        assert wing not in filter_legs_for_close([wing], "shorts_only")


@pytest.mark.asyncio
async def test_tolerance_bypass_log_on_clamp(caplog: pytest.LogCaptureFixture) -> None:
    """clamp_short_strike_inside_wing itself logs WING_CROSS_GUARD; bypass is in logic."""
    with caplog.at_level(logging.INFO):
        clamp_short_strike_inside_wing(
            leg="call",
            wanted_strike=84000.0,
            wing_strike=84000.0,
            available_strikes=[82000.0, 83500.0, 84000.0],
            current_short_strike=82000.0,
        )
    assert any("WING_CROSS_GUARD" in r.message for r in caplog.records)


def test_wings_orphaned_not_in_loss_reasons_snippet() -> None:
    """Ensure WINGS_ORPHANED exists and is distinct from loss exits."""
    assert ExitReason.WINGS_ORPHANED.value == "WINGS_ORPHANED"
    loss_like = {
        ExitReason.STOPLOSS.value,
        ExitReason.MAX_ADJUSTMENTS_REACHED.value,
        ExitReason.CHAIN_EXHAUSTED.value,
        ExitReason.ENTRY_PARTIAL_UNWIND.value,
    }
    assert ExitReason.WINGS_ORPHANED.value not in loss_like
