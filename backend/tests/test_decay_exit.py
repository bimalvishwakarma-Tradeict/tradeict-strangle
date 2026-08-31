# test_decay_exit.py — premium decay exit + blended entry (B25 fix, B26)
#
# Run: python -m pytest backend/tests/test_decay_exit.py -q

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.entry_basis import blend_entry_premium
from backend.strategies.s001_short_strangle.premium_decay import (
    evaluate_premium_decay_exit,
)


def _leg(*, leg_type: str, entry: float, current: float, qty: int = 1, status: str = "open"):
    return SimpleNamespace(
        leg_type=leg_type,
        initial_premium=entry,
        quantity=qty,
        status=status,
    )


def test_blend_entry_premium_weighted_average() -> None:
    assert blend_entry_premium(
        old_entry=214.0,
        old_qty=1,
        extra_fill=44.0,
        extra_qty=1,
    ) == 129.0


def test_blend_entry_premium_zero_extra_fill_unchanged() -> None:
    assert blend_entry_premium(
        old_entry=214.0,
        old_qty=1,
        extra_fill=0.0,
        extra_qty=1,
    ) == 214.0


def test_both_legs_mode_partial_decay_no_exit() -> None:
    call = _leg(leg_type="call", entry=181.0, current=131.0)
    put = _leg(leg_type="put", entry=214.0, current=42.8)
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=131.0,
        put_premium=42.8,
        enabled=True,
        decay_pct=50.0,
        mode="both_legs",
    )
    assert should_exit is False
    assert detail["block_reason"] == "above_threshold"


def test_both_legs_mode_both_at_threshold_exits() -> None:
    call = _leg(leg_type="call", entry=200.0, current=90.0)
    put = _leg(leg_type="put", entry=200.0, current=90.0)
    should_exit, _detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=90.0,
        put_premium=90.0,
        enabled=True,
        decay_pct=50.0,
        mode="both_legs",
    )
    assert should_exit is True


def test_combined_mode_aggregate_below_threshold_exits() -> None:
    call = _leg(leg_type="call", entry=181.0, current=131.0)
    put = _leg(leg_type="put", entry=214.0, current=44.0)
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=131.0,
        put_premium=44.0,
        enabled=True,
        decay_pct=50.0,
        mode="combined",
    )
    assert should_exit is True
    assert detail["combined_remaining_pct"] == 44.3038


def test_zero_current_premium_blocks_exit() -> None:
    call = _leg(leg_type="call", entry=100.0, current=0.0)
    put = _leg(leg_type="put", entry=100.0, current=20.0)
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=0.0,
        put_premium=20.0,
        enabled=True,
        decay_pct=50.0,
        mode="both_legs",
    )
    assert should_exit is False
    assert detail["block_reason"] == "no_live_premium"


def test_zero_entry_basis_blocks_exit() -> None:
    call = _leg(leg_type="call", entry=0.0, current=10.0)
    put = _leg(leg_type="put", entry=100.0, current=20.0)
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=10.0,
        put_premium=20.0,
        enabled=True,
        decay_pct=50.0,
        mode="both_legs",
    )
    assert should_exit is False
    assert detail["block_reason"] == "no_entry_basis"


def test_one_leg_closed_blocks_exit() -> None:
    call = _leg(leg_type="call", entry=100.0, current=40.0, status="open")
    put = _leg(leg_type="put", entry=100.0, current=40.0, status="closed")
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=40.0,
        put_premium=40.0,
        enabled=True,
        decay_pct=50.0,
        mode="both_legs",
    )
    assert should_exit is False
    assert detail["block_reason"] == "not_both_legs_open"


def test_disabled_never_exits() -> None:
    call = _leg(leg_type="call", entry=100.0, current=10.0)
    put = _leg(leg_type="put", entry=100.0, current=10.0)
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=10.0,
        put_premium=10.0,
        enabled=False,
        decay_pct=50.0,
        mode="both_legs",
    )
    assert should_exit is False
    assert detail["block_reason"] == "disabled"
