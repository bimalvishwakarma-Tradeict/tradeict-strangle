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

from backend.engine.trade_reconcile import (
    book_leg_close,
    pnl_sanity_check,
    recompute_trade_realized_pnl,
)


def _leg(**kwargs: object) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "leg_type": "call",
        "status": "open",
        "initial_premium": 9.0,
        "quantity": 1,
        "is_long": False,
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
    class FakeQuery:
        def __init__(self, legs: list) -> None:
            self._legs = legs

        def filter(self, *args: object, **kwargs: object) -> "FakeQuery":
            return self

        def all(self) -> list:
            return self._legs

    class FakeDb:
        def __init__(self, legs: list) -> None:
            self._legs = legs

        def query(self, model: object) -> FakeQuery:
            return FakeQuery(self._legs)

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
    total = recompute_trade_realized_pnl(FakeDb(legs), trade)
    assert round(total, 4) == round(-0.0029 + 0.006, 4)
    assert trade.realized_pnl == total


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
    test_pnl_sanity_fail_on_sign_mismatch()
    print("ALL PASSED")
