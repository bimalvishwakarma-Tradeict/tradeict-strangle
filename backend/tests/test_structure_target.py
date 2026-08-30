# test_structure_target.py — structure-wide hedge target (B9)
#
# Run: python -m pytest backend/tests/test_structure_target.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.hedge_lifecycle import (
    compute_structure_pnl,
    compute_structure_pnl_live,
    hedge_target_should_fire,
)


def test_structure_pnl_live_sum() -> None:
    live = compute_structure_pnl_live(
        hedge_net_mtm=-2.6661,
        booked_closed_pnl=-0.259,
        open_basket_net_mtm=0.968,
    )
    assert live == pytest.approx(-1.9571, rel=1e-4)


def test_structure_pnl_sum_fires_target() -> None:
    hedge_net = 10.0
    entry_spread = 0.13
    booked = 12.0
    open_gross = 5.0
    structure = compute_structure_pnl(
        hedge_net_mtm=hedge_net,
        entry_spread_usd=entry_spread,
        booked_closed_pnl=booked,
        open_basket_gross_mtm=open_gross,
    )
    target = 26.59
    assert structure == pytest.approx(27.13, rel=1e-9)
    assert hedge_target_should_fire(
        structure_pnl=structure,
        target_usd=target,
    ) is True


def test_hedge_net_alone_misses_but_structure_fires() -> None:
    hedge_net = 8.0
    entry_spread = 0.5
    booked = 15.0
    open_gross = 4.0
    target = 26.59
    structure = compute_structure_pnl(
        hedge_net_mtm=hedge_net,
        entry_spread_usd=entry_spread,
        booked_closed_pnl=booked,
        open_basket_gross_mtm=open_gross,
    )
    assert hedge_net < target
    assert structure >= target
    assert hedge_target_should_fire(
        structure_pnl=structure,
        target_usd=target,
    ) is True


def test_structure_below_target_does_not_fire() -> None:
    structure = compute_structure_pnl(
        hedge_net_mtm=-1.8,
        entry_spread_usd=0.13,
        booked_closed_pnl=-0.13,
        open_basket_gross_mtm=0.20,
    )
    target = 26.59
    assert structure < target
    assert hedge_target_should_fire(
        structure_pnl=structure,
        target_usd=target,
    ) is False


def test_negative_booked_raises_effective_target() -> None:
    hedge_net = 20.0
    entry_spread = 0.5
    open_gross = 8.0
    target = 26.59
    without_booked = compute_structure_pnl(
        hedge_net_mtm=hedge_net,
        entry_spread_usd=entry_spread,
        booked_closed_pnl=0.0,
        open_basket_gross_mtm=open_gross,
    )
    with_loss = compute_structure_pnl(
        hedge_net_mtm=hedge_net,
        entry_spread_usd=entry_spread,
        booked_closed_pnl=-5.0,
        open_basket_gross_mtm=open_gross,
    )
    assert without_booked >= target
    assert with_loss < target
    assert hedge_target_should_fire(
        structure_pnl=without_booked,
        target_usd=target,
    ) is True
    assert hedge_target_should_fire(
        structure_pnl=with_loss,
        target_usd=target,
    ) is False
