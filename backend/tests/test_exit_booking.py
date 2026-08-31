# test_exit_booking.py — Exit lock booking guards + no zero overwrite
#
# Run: python backend/tests/test_exit_booking.py

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.realized_booking import (
    book_leg_close,
    pnl_sanity_check,
    recompute_trade_realized_from_legs,
)


def _leg(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "leg_type": "call",
        "status": "open",
        "initial_premium": 9.0,
        "quantity": 1,
        "is_long": False,
        "is_bot_managed": True,
        "exit_premium": None,
        "realized_pnl": None,
        "exit_order_id": None,
        "exit_fee_usd": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_book_leg_close_skips_already_closed() -> None:
    trade = SimpleNamespace(id=66, notes=None, realized_pnl=0.0)
    leg = _leg(status="closed", exit_premium=9.9, realized_pnl=-0.0029)
    out = book_leg_close(
        leg=leg,
        trade=trade,
        exit_premium=0.0,
        exit_time=datetime.now(timezone.utc),
    )
    assert float(leg.exit_premium) == 9.9
    assert round(float(leg.realized_pnl), 4) == -0.0029
    assert round(out, 4) == -0.0029


def test_book_leg_close_never_writes_zero() -> None:
    trade = SimpleNamespace(id=66, notes=None, realized_pnl=0.0)
    leg = _leg(id=198, leg_type="put", initial_premium=9.0)
    book_leg_close(leg=leg, trade=trade, exit_premium=0.0)
    assert leg.exit_premium is None
    assert leg.realized_pnl is None
    assert "PNL_UNRESOLVED_put" in str(trade.notes)


def test_recompute_trade_realized_from_legs() -> None:
    legs = [
        _leg(id=1, status="closed", exit_premium=9.9, realized_pnl=-0.0029),
        _leg(
            id=2,
            leg_type="put",
            status="closed",
            exit_premium=3.0,
            realized_pnl=0.006,
        ),
        _leg(id=3, status="closed", exit_premium=None, realized_pnl=None),
    ]
    trade = SimpleNamespace(id=66, realized_pnl=0.11)
    total = recompute_trade_realized_from_legs(legs, trade)
    assert round(total, 4) == round(-0.0029 + 0.006, 4)
    assert trade.realized_pnl == total


def test_emergency_close_all_legs_sum_to_trade_total() -> None:
    """Simulate trade #121: four closed legs must sum into trade.realized_pnl."""
    trade = SimpleNamespace(id=121, notes=None, realized_pnl=-0.047)
    legs = [
        _leg(
            id=1,
            leg_type="call",
            status="closed",
            initial_premium=200.0,
            realized_pnl=-0.429,
        ),
        _leg(
            id=2,
            leg_type="put",
            status="closed",
            initial_premium=180.0,
            realized_pnl=0.339,
        ),
        _leg(
            id=3,
            leg_type="hedge_call",
            status="closed",
            initial_premium=50.0,
            realized_pnl=0.043,
        ),
        _leg(
            id=4,
            leg_type="put",
            status="open",
            initial_premium=214.0,
            quantity=1,
        ),
    ]
    last_leg = legs[3]
    book_leg_close(
        leg=last_leg,
        trade=trade,
        exit_premium=44.0,
        exit_time=datetime.now(timezone.utc),
        recompute_fn=recompute_trade_realized_from_legs,
        all_legs=legs,
    )
    expected = round(-0.429 + 0.339 + 0.043 + last_leg.realized_pnl, 4)
    assert round(float(trade.realized_pnl), 4) == expected


def test_unresolved_leg_excluded_and_warns(caplog) -> None:
    import logging

    legs = [
        _leg(id=1, status="closed", realized_pnl=0.1),
        _leg(id=2, leg_type="put", status="closed", realized_pnl=None),
    ]
    trade = SimpleNamespace(id=77, realized_pnl=0.0)
    with caplog.at_level(logging.WARNING):
        total = recompute_trade_realized_from_legs(legs, trade)
    assert round(total, 4) == 0.1
    assert any("[REALIZED_LEG_UNRESOLVED]" in r.message for r in caplog.records)


def test_recompute_is_idempotent() -> None:
    legs = [
        _leg(id=1, status="closed", realized_pnl=0.05),
        _leg(id=2, leg_type="put", status="closed", realized_pnl=-0.02),
    ]
    trade = SimpleNamespace(id=88, realized_pnl=0.0)
    first = recompute_trade_realized_from_legs(legs, trade)
    second = recompute_trade_realized_from_legs(legs, trade)
    assert first == second
    assert trade.realized_pnl == first


def test_pnl_sanity_fail_on_sign_mismatch() -> None:
    # Trade#66 pattern: gross negative, booked positive
    ok = pnl_sanity_check(
        trade_id=66,
        realized_pnl=0.11,
        last_gross_mtm=-0.015,
        legs=[],
    )
    assert ok is False
    ok2 = pnl_sanity_check(
        trade_id=66,
        realized_pnl=-0.019,
        last_gross_mtm=-0.015,
        legs=[],
    )
    assert ok2 is True


if __name__ == "__main__":
    test_book_leg_close_skips_already_closed()
    test_book_leg_close_never_writes_zero()
    test_recompute_trade_realized_from_legs()
    test_emergency_close_all_legs_sum_to_trade_total()
    test_recompute_is_idempotent()
    test_pnl_sanity_fail_on_sign_mismatch()
    print("ALL PASSED")
