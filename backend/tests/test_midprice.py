# test_midprice.py — Mid-price chase / urgent ladder + double-fill guard

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.engine.midprice_executor import (
    execute_with_midprice,
    is_already_filled_cancel_error,
    is_post_only_reject,
    log_entry_drift,
    profiles_for_paired_sequence,
    should_use_midprice,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class FakeDelta:
    """Controllable Delta client for mid-price tests."""

    def __init__(self) -> None:
        self.bid = 100.0
        self.ask = 102.0
        self.orders: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self.place_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[int] = []
        self.get_order_calls: list[int] = []
        # Scripted behaviors per place call index
        self.place_behavior: list[Any] = []
        # After place, order state evolution on each get_order
        self.fills_on_get: dict[int, list[dict[str, Any]]] = {}
        self.cancel_behavior: dict[int, Any] = {}
        self.position_size: float = 0.0
        self.resolve_fill_price = AsyncMock(return_value=101.0)

    async def get_l2_top_of_book(self, symbol: str) -> tuple[float, float]:
        return self.bid, self.ask

    async def place_order(self, **kwargs: Any) -> dict[str, Any]:
        idx = len(self.place_calls)
        self.place_calls.append(dict(kwargs))
        if idx < len(self.place_behavior):
            beh = self.place_behavior[idx]
            if isinstance(beh, Exception):
                raise beh
            if callable(beh):
                return beh(kwargs)
            if isinstance(beh, dict):
                oid = int(beh.get("order_id") or self._next_id)
                self._next_id = max(self._next_id, oid + 1)
                order = {
                    "order_id": oid,
                    "id": oid,
                    "state": beh.get("state", "open"),
                    "filled_size": beh.get("filled_size", 0),
                    "size": kwargs.get("size"),
                    "average_fill_price": beh.get(
                        "average_fill_price", 0
                    ),
                    "raw": beh.get("raw", {}),
                }
                self.orders[oid] = order
                return order
        # default: open limit / filled market
        oid = self._next_id
        self._next_id += 1
        ot = str(kwargs.get("order_type") or "")
        if ot == "market_order":
            order = {
                "order_id": oid,
                "id": oid,
                "state": "filled",
                "filled_size": kwargs.get("size"),
                "size": kwargs.get("size"),
                "average_fill_price": self.ask
                if kwargs.get("side") == "buy"
                else self.bid,
                "raw": {"filled_size": kwargs.get("size")},
            }
        else:
            order = {
                "order_id": oid,
                "id": oid,
                "state": "open",
                "filled_size": 0,
                "size": kwargs.get("size"),
                "average_fill_price": 0,
                "raw": {},
            }
        self.orders[oid] = order
        return order

    async def get_order(self, order_id: int | str) -> dict[str, Any]:
        oid = int(order_id)
        self.get_order_calls.append(oid)
        script = self.fills_on_get.get(oid)
        if script:
            step = min(
                len(script) - 1,
                self.get_order_calls.count(oid) - 1,
            )
            upd = script[step]
            base = dict(self.orders.get(oid) or {"order_id": oid})
            base.update(upd)
            if "raw" in upd:
                base["raw"] = upd["raw"]
            self.orders[oid] = base
            return base
        return dict(self.orders.get(oid) or {"order_id": oid, "state": "open"})

    async def cancel_order(
        self, order_id: int, product_id: int | None = None
    ) -> dict[str, Any]:
        oid = int(order_id)
        self.cancel_calls.append(oid)
        beh = self.cancel_behavior.get(oid)
        if isinstance(beh, Exception):
            raise beh
        od = self.orders.get(oid)
        if od and str(od.get("state")).lower() not in {"filled", "closed"}:
            od["state"] = "cancelled"
            # Subsequent get_order must not keep scripting "open" — that was
            # the live bug (treat open as cancelled) and hangs the new poll.
            self.fills_on_get[oid] = [
                {"state": "cancelled", "filled_size": int(od.get("filled_size") or 0)}
            ]
        return od or {"order_id": oid, "state": "cancelled"}

    async def get_option_positions(self) -> list[dict[str, Any]]:
        # product_id from last place
        pid = 1
        if self.place_calls:
            pid = int(self.place_calls[-1].get("product_id") or 1)
        return [{"product_id": pid, "size": self.position_size}]


async def _noop_sleep(_s: float = 0) -> None:
    return None


@pytest.mark.asyncio
async def test_chase_fills_on_a1_stops():
    client = FakeDelta()
    oid = 10
    client.place_behavior = [
        {
            "order_id": oid,
            "state": "open",
            "filled_size": 0,
        }
    ]
    client.fills_on_get[oid] = [
        {
            "state": "filled",
            "filled_size": 2,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 2},
        }
    ]
    client.position_size = 2.0

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=2,
        profile="chase",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert len(client.place_calls) == 1
    assert client.place_calls[0].get("post_only") is True
    assert res.fill_type == "mid"


@pytest.mark.asyncio
async def test_chase_third_fail_fourth_fill_single_position():
    client = FakeDelta()
    # 3 open (no fill) then 4th fills
    client.place_behavior = [
        {"order_id": 1, "state": "open"},
        {"order_id": 2, "state": "open"},
        {"order_id": 3, "state": "open"},
        {"order_id": 4, "state": "open"},
    ]
    for oid in (1, 2, 3):
        client.fills_on_get[oid] = [{"state": "open", "filled_size": 0}]
    client.fills_on_get[4] = [
        {
            "state": "filled",
            "filled_size": 1,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 1},
        }
    ]
    client.position_size = 1.0

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="chase",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert len(client.place_calls) == 4
    assert len([c for c in client.place_calls if c.get("order_type") == "limit_order"]) == 4


@pytest.mark.asyncio
async def test_chase_ceiling_goes_market_not_abort():
    clock = FakeClock()
    client = FakeDelta()
    # First place opens; hold returns unfilled; cancel; then clock past ceiling
    client.place_behavior = [{"order_id": 1, "state": "open"}]
    client.fills_on_get[1] = [{"state": "open", "filled_size": 0}]

    async def sleep_adv(s: float) -> None:
        clock.advance(float(s) or 0)

    # Force elapsed >= chase_max on 2nd loop iteration
    orig_call = clock.__call__
    n = {"i": 0}

    def clock_fn() -> float:
        n["i"] += 1
        # after first attempt setup, jump past chase max
        if n["i"] >= 3:
            return clock.t + 200.0
        return clock.t

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="chase",
        delta_client=client,
        reason="BASKET_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=True,
        max_chase_seconds=10,
        hold_seconds=0,
        sleep_fn=sleep_adv,
        monotonic_fn=clock_fn,
        check_position_size=False,
    )
    assert res.success
    assert any(
        c.get("order_type") == "market_order" for c in client.place_calls
    )
    assert res.fill_type == "market"


@pytest.mark.asyncio
async def test_urgent_all_fail_then_market():
    client = FakeDelta()
    # A1 mid, A2 best, A3 best — all unfilled; A4 market
    client.place_behavior = [
        {"order_id": 1, "state": "open"},
        {"order_id": 2, "state": "open"},
        {"order_id": 3, "state": "open"},
    ]
    for oid in (1, 2, 3):
        client.fills_on_get[oid] = [{"state": "open", "filled_size": 0}]

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="urgent",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="put",
        symbol="P",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert client.place_calls[-1].get("order_type") == "market_order"
    # First is mid post-only
    assert client.place_calls[0].get("post_only") is True
    # Second is best (no post_only)
    assert client.place_calls[1].get("order_type") == "limit_order"
    assert not client.place_calls[1].get("post_only")
    assert float(client.place_calls[1]["limit_price"]) == pytest.approx(
        client.ask
    )


@pytest.mark.asyncio
async def test_urgent_a2_is_best_not_mid():
    client = FakeDelta()
    client.place_behavior = [
        {"order_id": 1, "state": "open"},
        {"order_id": 2, "state": "open"},
    ]
    client.fills_on_get[1] = [{"state": "open", "filled_size": 0}]
    client.fills_on_get[2] = [
        {
            "state": "filled",
            "filled_size": 1,
            "average_fill_price": 102.0,
            "raw": {"filled_size": 1},
        }
    ]

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="urgent",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="put",
        symbol="P",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert len(client.place_calls) == 2
    assert client.place_calls[1].get("post_only") in (False, None)
    assert float(client.place_calls[1]["limit_price"]) == pytest.approx(102.0)
    assert res.fill_type == "best"


@pytest.mark.asyncio
async def test_partial_fill_next_order_remainder_only():
    client = FakeDelta()
    client.place_behavior = [
        {"order_id": 1, "state": "open"},
        {"order_id": 2, "state": "open"},
    ]
    client.fills_on_get[1] = [
        {
            "state": "partially_filled",
            "filled_size": 1,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 1},
        }
    ]
    client.fills_on_get[2] = [
        {
            "state": "filled",
            "filled_size": 1,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 1},
        }
    ]

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=2,
        profile="chase",
        delta_client=client,
        reason="BASKET_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert int(client.place_calls[0]["size"]) == 2
    assert int(client.place_calls[1]["size"]) == 1


@pytest.mark.asyncio
async def test_double_fill_guard_delta_filled_stops():
    """After hold, Delta says filled → must NOT place another order."""
    client = FakeDelta()
    client.place_behavior = [{"order_id": 7, "state": "open"}]
    # First poll still open; but our poll with hold_seconds=0 returns once —
    # script filled on get so we stop.
    client.fills_on_get[7] = [
        {
            "state": "filled",
            "filled_size": 1,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 1},
        }
    ]

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="chase",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert len(client.place_calls) == 1
    assert len(client.cancel_calls) == 0  # full fill — no cancel/new order


@pytest.mark.asyncio
async def test_cancel_already_filled_treated_as_fill():
    client = FakeDelta()
    client.place_behavior = [{"order_id": 9, "state": "open"}]
    # Hold sees open/partial 0
    client.fills_on_get[9] = [{"state": "open", "filled_size": 0}]
    client.cancel_behavior[9] = Exception("cannot cancel: already filled")

    # After cancel fail, get_order returns filled
    async def get_order_filled(order_id: int | str) -> dict[str, Any]:
        client.get_order_calls.append(int(order_id))
        if len(client.cancel_calls) > 0:
            return {
                "order_id": 9,
                "state": "filled",
                "filled_size": 1,
                "average_fill_price": 101.0,
                "raw": {"filled_size": 1},
            }
        return {"order_id": 9, "state": "open", "filled_size": 0, "raw": {}}

    client.get_order = get_order_filled  # type: ignore[method-assign]

    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="chase",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
        check_position_size=False,
    )
    assert res.success
    assert len(client.place_calls) == 1


@pytest.mark.asyncio
async def test_postonly_reject_no_hold_wait(caplog):
    client = FakeDelta()
    client.place_behavior = [
        Exception("post_only order would take liquidity"),
        {"order_id": 2, "state": "open"},
    ]
    client.fills_on_get[2] = [
        {
            "state": "filled",
            "filled_size": 1,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 1},
        }
    ]
    slept: list[float] = []

    async def track_sleep(s: float) -> None:
        slept.append(float(s))

    with caplog.at_level(logging.WARNING):
        res = await execute_with_midprice(
            product_id=1,
            side="buy",
            quantity=1,
            profile="chase",
            delta_client=client,
            reason="HEDGE_ENTRY",
            leg_label="call",
            symbol="C",
            midprice_enabled=True,
            hold_seconds=3,
            sleep_fn=track_sleep,
            check_position_size=False,
        )
    assert res.success
    assert any("MIDPRICE_POSTONLY_REJECT" in r.message for r in caplog.records)
    # Must not sleep full 3s between post-only reject and next attempt
    assert not any(s >= 2.5 for s in slept)


@pytest.mark.asyncio
async def test_stoploss_reason_skips_ladder():
    client = FakeDelta()
    res = await execute_with_midprice(
        product_id=1,
        side="sell",
        quantity=1,
        profile="chase",
        delta_client=client,
        reason="HEDGE_STOPLOSS",
        leg_label="exit",
        symbol="C",
        midprice_enabled=True,
        reduce_only=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
    )
    assert res.success
    assert len(client.place_calls) == 1
    assert client.place_calls[0].get("order_type") == "market_order"
    assert res.fill_type == "market"


@pytest.mark.asyncio
async def test_manual_emergency_skips_ladder():
    client = FakeDelta()
    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="urgent",
        delta_client=client,
        reason="MANUAL_EMERGENCY",
        leg_label="exit",
        symbol="C",
        midprice_enabled=True,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
    )
    assert res.success
    assert client.place_calls[0].get("order_type") == "market_order"


@pytest.mark.asyncio
async def test_midprice_disabled_is_market_regression():
    client = FakeDelta()
    res = await execute_with_midprice(
        product_id=1,
        side="buy",
        quantity=1,
        profile="chase",
        delta_client=client,
        reason="HEDGE_ENTRY",
        leg_label="call",
        symbol="C",
        midprice_enabled=False,
        hold_seconds=0,
        sleep_fn=_noop_sleep,
    )
    assert res.success
    assert client.place_calls[0].get("order_type") == "market_order"
    assert should_use_midprice(enabled=False, reason="HEDGE_ENTRY") is False


def test_sequence_profiles_hedge_wings_shorts():
    # 6 legs: hedge pair + wings + shorts
    assert profiles_for_paired_sequence(6) == [
        "chase",
        "urgent",
        "chase",
        "urgent",
        "chase",
        "urgent",
    ]
    # Under existing hedge: wings + shorts only
    assert profiles_for_paired_sequence(4) == [
        "chase",
        "urgent",
        "chase",
        "urgent",
    ]


@pytest.mark.asyncio
async def test_size_mismatch_critical(caplog):
    client = FakeDelta()
    client.place_behavior = [
        {
            "order_id": 1,
            "state": "open",
        }
    ]
    client.fills_on_get[1] = [
        {
            "state": "filled",
            "filled_size": 1,
            "average_fill_price": 101.0,
            "raw": {"filled_size": 1},
        }
    ]
    client.position_size = 3.0  # intended 1

    with caplog.at_level(logging.CRITICAL):
        res = await execute_with_midprice(
            product_id=1,
            side="buy",
            quantity=1,
            profile="chase",
            delta_client=client,
            reason="HEDGE_ENTRY",
            leg_label="call",
            symbol="C",
            midprice_enabled=True,
            hold_seconds=0,
            sleep_fn=_noop_sleep,
            check_position_size=True,
        )
    assert res.success
    assert any("MIDPRICE_SIZE_MISMATCH" in r.message for r in caplog.records)


def test_entry_drift_high_warns_does_not_block(caplog):
    with caplog.at_level(logging.WARNING):
        drift = log_entry_drift(
            leg_label="call",
            selected_premium=100.0,
            fill_premium=130.0,
            seconds_since_selection=90.0,
            tolerance_pct=15.0,
        )
    assert drift == pytest.approx(30.0)
    assert any("ENTRY_DRIFT_HIGH" in r.message for r in caplog.records)


def test_helpers_postonly_and_cancel():
    assert is_post_only_reject(Exception("post_only would take"))
    assert is_already_filled_cancel_error(Exception("already filled"))
    assert should_use_midprice(enabled=True, reason="HEDGE_TARGET")
    assert not should_use_midprice(enabled=True, reason="PRE_EXPIRY")
    assert not should_use_midprice(enabled=True, reason="ENTRY_PARTIAL_UNWIND")
