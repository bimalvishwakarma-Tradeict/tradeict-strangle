# test_adjustment_target.py — Unit tests for adjustment target premium arithmetic
#
# Run: python -m pytest backend/tests/test_adjustment_target.py -v
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.strategies.s001_short_strangle.adjustment import (
    compute_adjustment_target_premium,
)


def test_trade61_first_adjustment_target() -> None:
    """Trade#61 case: untouched=6.8, baseline=9.1, offer=10.9 → 8.6."""
    target, loss = compute_adjustment_target_premium(
        untouched_leg_offer=6.8,
        triggered_baseline=9.1,
        triggered_offer=10.9,
    )
    assert round(loss, 1) == 1.8
    assert round(target, 1) == 8.6


def test_trade61_second_adjustment_target() -> None:
    """Trade#61 case: untouched=6.0, baseline=14.0, offer=17.0 → 9.0."""
    target, loss = compute_adjustment_target_premium(
        untouched_leg_offer=6.0,
        triggered_baseline=14.0,
        triggered_offer=17.0,
    )
    assert round(loss, 1) == 3.0
    assert round(target, 1) == 9.0


def test_zero_loss_when_offer_at_or_below_baseline() -> None:
    target, loss = compute_adjustment_target_premium(
        untouched_leg_offer=6.8,
        triggered_baseline=10.0,
        triggered_offer=9.5,
    )
    assert round(loss, 1) == 0.0
    assert round(target, 1) == 6.8


if __name__ == "__main__":
    test_trade61_first_adjustment_target()
    test_trade61_second_adjustment_target()
    test_zero_loss_when_offer_at_or_below_baseline()
    print("ALL PASSED")
