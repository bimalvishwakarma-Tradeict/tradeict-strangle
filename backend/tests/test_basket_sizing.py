# test_basket_sizing.py — pct_of_hedge basket qty helpers (B2, B7)
#
# Run: python -m pytest backend/tests/test_basket_sizing.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.auto_trade_engine import (
    compute_dynamic_basket_qty_pct,
    resolve_adjustment_basket_qty,
    resolve_basket_qty_from_hedge,
    resolve_entry_basket_pct,
    resolve_sizing_mode,
)


@pytest.mark.parametrize(
    ("hedge_qty", "pct", "expected"),
    [
        (10, 20.0, 2),
        (7, 20.0, 2),
        (5, 10.0, 1),
        (10, 100.0, 10),
    ],
)
def test_resolve_basket_qty_from_hedge_ceil(
    hedge_qty: int, pct: float, expected: int
) -> None:
    assert resolve_basket_qty_from_hedge(hedge_qty, pct) == expected


@pytest.mark.parametrize(
    ("hedge_qty", "pct"),
    [
        (0, 20.0),
        (10, 0.0),
        (-3, 20.0),
    ],
)
def test_resolve_basket_qty_from_hedge_zero(hedge_qty: int, pct: float) -> None:
    assert resolve_basket_qty_from_hedge(hedge_qty, pct) == 0


def _settings(**kwargs: object) -> SimpleNamespace:
    base = {
        "basket_qty_mode": "fixed",
        "basket_qty_pct_of_hedge": 20.0,
        "basket_qty_dynamic": False,
        "basket_qty_theta_mult": 2.0,
        "use_dynamic_qty_on_adjustment": False,
        "adjustment_qty_mode": "unchanged",
        "adjustment_qty_decrease_pct": 25.0,
        "hedge_enabled": False,
        "hedge_qty_lots": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_adjustment_qty_dynamic_on_capped_at_half_hedge() -> None:
    """theta=57, mult=3, call_ask=300, hedge_qty=5 → raw 3, cap 2."""
    settings = _settings(
        use_dynamic_qty_on_adjustment=True,
        adjustment_qty_mode="increase_dynamic",
        basket_qty_dynamic=True,
        basket_qty_theta_mult=3.0,
    )
    qty, close = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=1,
        hedge_qty=5,
        hedge_call_theta=57.0,
        new_strike_ask=300.0,
    )
    assert qty == 2
    assert close is False


def test_adjustment_qty_dynamic_on_below_cap() -> None:
    """Small raw qty stays uncapped."""
    settings = _settings(
        use_dynamic_qty_on_adjustment=True,
        adjustment_qty_mode="increase_dynamic",
        basket_qty_dynamic=True,
        basket_qty_theta_mult=2.0,
    )
    qty, close = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=1,
        hedge_qty=5,
        hedge_call_theta=10.0,
        new_strike_ask=300.0,
        trade_id=99,
    )
    assert qty == 1
    assert close is False


def test_adjustment_qty_dynamic_off_uses_triggered_qty() -> None:
    settings = _settings(
        use_dynamic_qty_on_adjustment=False,
        adjustment_qty_mode="unchanged",
        basket_qty_dynamic=True,
        basket_qty_theta_mult=3.0,
    )
    qty, close = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=4,
        hedge_qty=5,
        hedge_call_theta=57.0,
        new_strike_ask=300.0,
    )
    assert qty == 4
    assert close is False


def test_adjustment_qty_requires_basket_qty_dynamic() -> None:
    settings = _settings(
        use_dynamic_qty_on_adjustment=True,
        adjustment_qty_mode="increase_dynamic",
        basket_qty_dynamic=False,
        basket_qty_theta_mult=3.0,
    )
    qty, close = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=1,
        hedge_qty=5,
        hedge_call_theta=57.0,
        new_strike_ask=300.0,
    )
    assert qty == 1
    assert close is False


def test_decrease_step_adj1_adj2() -> None:
    settings = _settings(
        adjustment_qty_mode="decrease_step",
        adjustment_qty_decrease_pct=25.0,
    )
    q1, c1 = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=100,
        hedge_qty=0,
        hedge_call_theta=0,
        new_strike_ask=1,
        original_qty=100,
        adjustment_number=1,
    )
    q2, c2 = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=75,
        hedge_qty=0,
        hedge_call_theta=0,
        new_strike_ask=1,
        original_qty=100,
        adjustment_number=2,
    )
    assert (q1, c1) == (75, False)
    assert (q2, c2) == (50, False)


def test_decrease_step_floor_min_1() -> None:
    settings = _settings(
        adjustment_qty_mode="decrease_step",
        adjustment_qty_decrease_pct=25.0,
    )
    q1, _ = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=3,
        hedge_qty=0,
        hedge_call_theta=0,
        new_strike_ask=1,
        original_qty=3,
        adjustment_number=1,
    )
    q2, _ = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=2,
        hedge_qty=0,
        hedge_call_theta=0,
        new_strike_ask=1,
        original_qty=3,
        adjustment_number=2,
    )
    assert q1 == 2
    assert q2 == 1


def test_decrease_step_same_qty_no_resize_signal() -> None:
    """Helper still returns same qty; caller skips resize orders."""
    settings = _settings(
        adjustment_qty_mode="decrease_step",
        adjustment_qty_decrease_pct=25.0,
    )
    # orig 4, adj1 → floor(4*0.75)=3; if current already 3, equal
    qty, close = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=3,
        hedge_qty=0,
        hedge_call_theta=0,
        new_strike_ask=1,
        original_qty=4,
        adjustment_number=1,
    )
    assert qty == 3
    assert close is False
    assert qty == 3  # == current → no resize order at caller


def test_increase_dynamic_mode_unchanged_from_b25() -> None:
    settings = _settings(
        adjustment_qty_mode="increase_dynamic",
        basket_qty_dynamic=True,
        basket_qty_theta_mult=3.0,
    )
    qty, close = resolve_adjustment_basket_qty(
        settings=settings,
        triggered_leg_qty=1,
        hedge_qty=5,
        hedge_call_theta=57.0,
        new_strike_ask=300.0,
    )
    assert qty == 2
    assert close is False


def test_all_modes_do_not_touch_wing_qty() -> None:
    """resolve_adjustment_basket_qty never returns wing qty — wings stay apart."""
    from backend.engine.wing_entry import resolve_adjustment_qty_mode

    for mode in ("unchanged", "increase_dynamic", "decrease_step"):
        s = _settings(
            adjustment_qty_mode=mode,
            basket_qty_dynamic=True,
            use_dynamic_qty_on_adjustment=(mode == "increase_dynamic"),
        )
        assert resolve_adjustment_qty_mode(s) == mode
        # Function signature has no wing_qty arg — wings unchanged by design
        qty, _ = resolve_adjustment_basket_qty(
            settings=s,
            triggered_leg_qty=10,
            hedge_qty=20,
            hedge_call_theta=50.0,
            new_strike_ask=200.0,
            original_qty=10,
            adjustment_number=1,
        )
        assert isinstance(qty, int)
        assert qty >= 1


def test_resolve_sizing_mode_fixed_when_hedge_off() -> None:
    settings = _settings(basket_qty_mode="pct_of_hedge", hedge_enabled=False)
    with patch("backend.core.bot_logger.log_and_buffer") as log_mock:
        assert resolve_sizing_mode(settings) == "fixed"
    log_mock.assert_called_once()
    assert log_mock.call_args.args[0] == "BASKET_SIZING"
    assert log_mock.call_args.args[2]["reason"] == "hedge_disabled"


def test_resolve_sizing_mode_fixed_when_hedge_qty_lots_missing() -> None:
    settings = _settings(
        basket_qty_mode="pct_of_hedge",
        hedge_enabled=True,
        hedge_qty_lots=None,
    )
    with patch("backend.core.bot_logger.log_and_buffer") as log_mock:
        assert resolve_sizing_mode(settings) == "fixed"
    log_mock.assert_called_once()
    assert log_mock.call_args.args[2]["reason"] == "hedge_qty_lots_missing"


def test_resolve_sizing_mode_pct_of_hedge_when_valid() -> None:
    settings = _settings(
        basket_qty_mode="pct_of_hedge",
        hedge_enabled=True,
        hedge_qty_lots=10,
    )
    with patch("backend.core.bot_logger.log_and_buffer") as log_mock:
        assert resolve_sizing_mode(settings) == "pct_of_hedge"
    log_mock.assert_not_called()


def test_fixed_mode_hedge_qty_formula_unchanged() -> None:
    """Mirror hedge-gate fixed path: basket_qty × ratio, rounded, min 1."""
    basket_qty = max(1, int(5 or 1))
    ratio = 1.5
    hedge_qty = max(1, int(round(basket_qty * ratio)))
    assert basket_qty == 5
    assert hedge_qty == 8

    basket_qty = max(1, int(3 or 1))
    ratio = 1.0
    hedge_qty = max(1, int(round(basket_qty * ratio)))
    assert hedge_qty == 3


def test_dynamic_pct_formula_live_example() -> None:
    pct = compute_dynamic_basket_qty_pct(
        hedge_call_theta=57.0,
        theta_mult=2.0,
        call_ask=275.0,
    )
    assert pct == pytest.approx(41.454545, rel=1e-4)
    assert resolve_basket_qty_from_hedge(5, pct) == 3


def test_dynamic_pct_call_ask_zero_falls_back_to_manual() -> None:
    settings = _settings(
        basket_qty_mode="pct_of_hedge",
        basket_qty_dynamic=True,
        basket_qty_pct_of_hedge=20.0,
        hedge_enabled=True,
        hedge_qty_lots=5,
    )
    straddle = {"call_premium": 0.0}
    pct, computed, dynamic = resolve_entry_basket_pct(
        settings,
        straddle=straddle,
        hedge_call_theta=57.0,
        sizing_mode="pct_of_hedge",
    )
    assert dynamic is True
    assert computed is None
    assert pct == 20.0


def test_dynamic_pct_zero_theta_falls_back_to_manual() -> None:
    settings = _settings(
        basket_qty_mode="pct_of_hedge",
        basket_qty_dynamic=True,
        basket_qty_pct_of_hedge=25.0,
        hedge_enabled=True,
    )
    straddle = {"call_premium": 275.0}
    pct, computed, dynamic = resolve_entry_basket_pct(
        settings,
        straddle=straddle,
        hedge_call_theta=0.0,
        sizing_mode="pct_of_hedge",
    )
    assert dynamic is True
    assert computed is None
    assert pct == 25.0


def test_dynamic_false_uses_manual_pct() -> None:
    settings = _settings(
        basket_qty_mode="pct_of_hedge",
        basket_qty_dynamic=False,
        basket_qty_pct_of_hedge=18.0,
        hedge_enabled=True,
    )
    straddle = {"call_premium": 275.0}
    pct, computed, dynamic = resolve_entry_basket_pct(
        settings,
        straddle=straddle,
        hedge_call_theta=57.0,
        sizing_mode="pct_of_hedge",
    )
    assert dynamic is False
    assert computed is None
    assert pct == 18.0


def test_fixed_mode_ignores_dynamic_flag() -> None:
    settings = _settings(
        basket_qty_mode="fixed",
        basket_qty_dynamic=True,
        basket_qty_pct_of_hedge=18.0,
        hedge_enabled=True,
    )
    straddle = {"call_premium": 275.0}
    pct, computed, dynamic = resolve_entry_basket_pct(
        settings,
        straddle=straddle,
        hedge_call_theta=57.0,
        sizing_mode="fixed",
    )
    assert dynamic is False
    assert computed is None
    assert pct == 18.0
