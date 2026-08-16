# test_adjustment_target.py — Basket net-loss adjustment target
#
# Run: python backend/tests/test_adjustment_target.py
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


def test_owner_example_adjustment_1() -> None:
    """baselines (200, 200), offers (300, 110), triggered=call → loss 10, target 120."""
    target, loss, cbase, ccur = compute_adjustment_target_premium(
        untouched_leg_offer=110.0,
        short_baselines=(200.0, 200.0),
        short_offers=(300.0, 110.0),
    )
    assert round(cbase, 1) == 400.0
    assert round(ccur, 1) == 410.0
    assert round(loss, 1) == 10.0
    assert round(target, 1) == 120.0


def test_trade66_adjustment_1() -> None:
    """Trade#66: baselines (9, 9), offers (12, 7), triggered=call → loss 1, target 8."""
    target, loss, cbase, ccur = compute_adjustment_target_premium(
        untouched_leg_offer=7.0,
        short_baselines=(9.0, 9.0),
        short_offers=(12.0, 7.0),
    )
    assert round(cbase, 1) == 18.0
    assert round(ccur, 1) == 19.0
    assert round(loss, 1) == 1.0
    assert round(target, 1) == 8.0


def test_owner_example_adjustment_2_put_triggered() -> None:
    """baselines (120, 110), offers (100, 165), triggered=put → loss 35, target 135."""
    target, loss, cbase, ccur = compute_adjustment_target_premium(
        untouched_leg_offer=100.0,
        short_baselines=(120.0, 110.0),
        short_offers=(100.0, 165.0),
    )
    assert round(cbase, 1) == 230.0
    assert round(ccur, 1) == 265.0
    assert round(loss, 1) == 35.0
    assert round(target, 1) == 135.0


def test_gross_profit_loss_is_zero() -> None:
    """Basket in gross profit → loss 0, target == untouched_offer."""
    target, loss, cbase, ccur = compute_adjustment_target_premium(
        untouched_leg_offer=90.0,
        short_baselines=(200.0, 200.0),
        short_offers=(150.0, 90.0),
    )
    assert round(cbase, 1) == 400.0
    assert round(ccur, 1) == 240.0
    assert round(loss, 1) == 0.0
    assert round(target, 1) == 90.0


if __name__ == "__main__":
    test_owner_example_adjustment_1()
    test_trade66_adjustment_1()
    test_owner_example_adjustment_2_put_triggered()
    test_gross_profit_loss_is_zero()
    print("ALL PASSED")
