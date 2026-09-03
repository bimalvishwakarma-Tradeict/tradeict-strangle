# test_wing_trigger_shorts_only.py — adjustment triggers ignore wings (intentional)
#
# Run: python -m pytest backend/tests/test_wing_trigger_shorts_only.py -q

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from datetime import date, timedelta
from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy


def _trade(**kwargs):
    base = dict(
        id=99,
        profit_target_usd=100.0,
        stoploss_usd=100.0,
        trigger_mode="flat",
        combined_trigger_mode=False,
        expiry_date=date.today() + timedelta(days=2),
        monitoring_starts_at=None,
        adjust_settling_until=None,
        slippage_pct=0.0,
        entry_spread_for_sl_usd=0.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _short(*, entry: float, status: str = "open"):
    leg = MagicMock()
    leg.initial_premium = entry
    leg.trigger_baseline_premium = entry
    leg.trigger_premium = entry
    leg.quantity = 1
    leg.status = status
    leg.entry_fee_usd = 0.0
    leg.exit_fee_usd = 0.0
    leg.is_long = False
    leg.leg_type = "call"
    return leg


def test_adjustment_trigger_ignores_wing_premium_spike() -> None:
    """
    Wings may spike in premium without causing adjustment.
    Trigger math uses short call/put only (intentional).
    """
    trade = _trade(id=99)
    call = _short(entry=100.0)
    call.leg_type = "call"
    put = _short(entry=100.0)
    put.leg_type = "put"

    wing_call = MagicMock()
    wing_call.initial_premium = 50.0
    wing_call.trigger_baseline_premium = 50.0
    wing_call.quantity = 1
    wing_call.status = "open"
    wing_call.is_long = True
    wing_call.leg_type = "wing_call"
    wing_call.entry_fee_usd = 0.0
    wing_call.exit_fee_usd = 0.0

    wing_put = MagicMock()
    wing_put.initial_premium = 40.0
    wing_put.trigger_baseline_premium = 40.0
    wing_put.quantity = 1
    wing_put.status = "open"
    wing_put.is_long = True
    wing_put.leg_type = "wing_put"
    wing_put.entry_fee_usd = 0.0
    wing_put.exit_fee_usd = 0.0

    strat = ShortStrangleStrategy()
    # Shorts calm (below 150% flat trigger); wings exploded
    action = asyncio.run(
        strat.on_tick(
            trade,
            call,
            put,
            110.0,
            105.0,
            None,
            realized_pnl=0.0,
            delta_mtm=0.0,
            net_mtm=0.0,
            slippage_pct=0.0,
            gross_mtm_for_sl=0.0,
            wing_call_leg=wing_call,
            wing_put_leg=wing_put,
            wing_call_premium=500.0,
            wing_put_premium=400.0,
        )
    )
    assert action.should_adjust is False
    assert action.should_exit is False


def test_adjustment_still_fires_on_short_leg() -> None:
    trade = _trade(id=100)
    call = _short(entry=100.0)
    call.leg_type = "call"
    put = _short(entry=100.0)
    put.leg_type = "put"

    strat = ShortStrangleStrategy()
    action = asyncio.run(
        strat.on_tick(
            trade,
            call,
            put,
            160.0,  # 160% of baseline → adjust call
            90.0,
            None,
            realized_pnl=0.0,
            delta_mtm=-0.05,
            net_mtm=-0.05,
            slippage_pct=0.0,
            gross_mtm_for_sl=-0.05,
        )
    )
    assert action.should_adjust is True
    assert action.triggered_leg == "call"
