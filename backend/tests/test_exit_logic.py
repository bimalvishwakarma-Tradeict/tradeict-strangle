# test_exit_logic.py — Unit tests for TP / SL / Decision / Hold exit paths
#
# Run: python backend/tests/test_exit_logic.py
# (from trading-bot/ root, with venv active)

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy


def make_trade(
    tp_usd: float = 30,
    sl_usd: float = 60,
    slippage_pct: float = 2.0,
    trigger_mode: str = "flat",
) -> MagicMock:
    trade = MagicMock()
    trade.id = 1
    trade.profit_target_usd = tp_usd
    trade.stoploss_usd = sl_usd
    trade.slippage_pct = slippage_pct
    trade.expiry_date = date.today() + timedelta(days=3)
    trade.trigger_mode = trigger_mode
    trade.monitoring_starts_at = None
    trade.entry_spread_for_sl_usd = 0.0
    return trade


def make_leg(initial: float = 150, baseline: float = 150, qty: int = 1) -> MagicMock:
    leg = MagicMock()
    leg.initial_premium = initial
    leg.trigger_baseline_premium = baseline
    leg.trigger_premium = baseline
    leg.quantity = qty
    leg.status = "open"
    leg.entry_fee_usd = 0.0
    leg.exit_fee_usd = 0.0
    return leg


def make_db() -> MagicMock:
    """Mock DB: empty settings / legs so defaults apply (flat 150%)."""
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    return db


def main() -> None:
    strategy = ShortStrangleStrategy()
    db = make_db()

    # Test 1: Profit target hit (passed net_mtm above target)
    trade = make_trade(tp_usd=30)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            make_leg(150),
            make_leg(150),
            50.0,
            50.0,
            db,
            net_mtm=35.0,
        )
    )
    assert action.should_exit is True
    assert action.exit_reason == "PROFIT_TARGET"
    print("Test 1 PASSED: Profit target exit ✅")

    # Test 2: Stop loss hit — MUST use gross_mtm_for_sl (not net alone)
    trade = make_trade(sl_usd=60)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            make_leg(150),
            make_leg(150),
            250.0,
            180.0,
            db,
            net_mtm=-10.0,  # net alone would NOT trip $60 SL
            gross_mtm_for_sl=-65.0,  # gross trips SL
        )
    )
    assert action.should_exit is True
    assert action.exit_reason == "STOPLOSS"
    print("Test 2 PASSED: Stop loss exit ✅")

    # Test 2b: Net deep in loss but gross inside SL → must HOLD (Trade #41)
    trade = make_trade(tp_usd=1.0, sl_usd=0.03)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            make_leg(100),
            make_leg(100),
            100.0,
            100.0,
            db,
            realized_pnl=0.0,
            delta_mtm=-0.013,
            net_mtm=-0.0951,
            gross_mtm_for_sl=-0.003,
        )
    )
    assert action.should_exit is False, (
        f"False SL on net_mtm: exit={action.should_exit} reason={action.exit_reason}"
    )
    print("Test 2b PASSED: net_mtm cannot false-trigger SL ✅")

    # Test 3: Decision trigger — profitable → close
    # flat 150%: call baseline 150 → trigger at 225; 262 hits
    trade = make_trade(tp_usd=200, sl_usd=400, trigger_mode="flat")
    call_leg = make_leg(initial=150, baseline=150)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            call_leg,
            make_leg(150, 150),
            262.0,
            50.0,
            db,
            net_mtm=15.0,
        )
    )
    assert action.should_exit is True
    assert action.exit_reason == "DECISION_PROFIT_AT_TRIGGER"
    assert action.triggered_leg == "call"
    print("Test 3 PASSED: Decision trigger close ✅")

    # Test 4: Decision trigger — negative → adjust
    trade = make_trade(tp_usd=200, sl_usd=400, trigger_mode="flat")
    call_leg = make_leg(initial=150, baseline=150)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            call_leg,
            make_leg(150, 150),
            262.0,
            50.0,
            db,
            net_mtm=-5.0,
        )
    )
    assert action.should_adjust is True
    assert action.adjust_leg == "call"
    print("Test 4 PASSED: Decision trigger adjust ✅")

    # Test 5: No action — all within limits
    trade = make_trade(tp_usd=200, sl_usd=400, trigger_mode="flat")
    action = asyncio.run(
        strategy.on_tick(
            trade,
            make_leg(150, 150),
            make_leg(150, 150),
            100.0,
            100.0,
            db,
            net_mtm=10.0,
        )
    )
    assert action.should_exit is False
    assert action.should_adjust is False
    print("Test 5 PASSED: Hold — no action ✅")

    # Test 6: Settling skips TP / adjust, but NOT stop loss
    trade = make_trade(tp_usd=30, sl_usd=60)
    from backend.core.time_utils import get_ist_now
    from datetime import timedelta as td

    trade.monitoring_starts_at = get_ist_now() + td(minutes=5)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            make_leg(150),
            make_leg(150),
            50.0,
            50.0,
            db,
            net_mtm=35.0,  # would hit TP if not settling
            gross_mtm_for_sl=0.0,
        )
    )
    assert action.should_exit is False
    assert action.should_adjust is False
    print("Test 6 PASSED: Settling skips TP/adjust ✅")

    # Test 6b: STOPLOSS fires even while settling
    trade = make_trade(tp_usd=30, sl_usd=0.01)
    trade.monitoring_starts_at = get_ist_now() + td(minutes=5)
    action = asyncio.run(
        strategy.on_tick(
            trade,
            make_leg(150),
            make_leg(150),
            50.0,
            50.0,
            db,
            net_mtm=-0.02,
            gross_mtm_for_sl=-0.051,
        )
    )
    assert action.should_exit is True
    assert action.exit_reason == "STOPLOSS"
    print("Test 6b PASSED: Settling does NOT suppress STOPLOSS ✅")

    print("\n✅ ALL EXIT LOGIC TESTS PASSED")


if __name__ == "__main__":
    main()
