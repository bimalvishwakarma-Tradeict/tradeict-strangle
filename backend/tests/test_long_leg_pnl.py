# test_long_leg_pnl.py — long bid-entry leg PnL matches gross

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_trade import _leg_snapshot, check_open_leg_pnl_vs_gross


def _leg(
    *,
    initial: float,
    qty: int,
    is_long: bool,
    status: str = "open",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        initial_premium=initial,
        quantity=qty,
        is_long=is_long,
        side="BUY" if is_long else "SELL",
        status=status,
        strike=80000.0,
        symbol="X",
        leg_type="put",
        exit_premium=None,
        realized_pnl=None,
        entry_time=None,
        exit_time=None,
        trigger_baseline_premium=None,
        trigger_premium=None,
        entry_fee_usd=None,
        exit_fee_usd=None,
        entry_slip_usd=None,
        entry_slip_ratio=None,
        sl_trigger_price=None,
        delta_sl_order_id=None,
        delta_sl_confirmed=None,
    )


def test_long_put_bid_entry_pnl() -> None:
    snap = _leg_snapshot(_leg(initial=636.0, qty=300, is_long=True), 512.0)
    assert snap["leg_pnl"] == -37.2


def test_long_call_and_put_sum_equals_gross() -> None:
    call = _leg_snapshot(
        _leg(initial=624.0, qty=300, is_long=True),
        621.0,
        force_long=True,
    )
    put = _leg_snapshot(
        _leg(initial=636.0, qty=300, is_long=True),
        512.0,
        force_long=True,
    )
    assert call["leg_pnl"] == -0.9
    assert put["leg_pnl"] == -37.2
    assert round(call["leg_pnl"] + put["leg_pnl"], 2) == -38.1


def test_short_leg_entry_minus_ask_unchanged() -> None:
    snap = _leg_snapshot(_leg(initial=544.0, qty=200, is_long=False), 654.0)
    assert snap["leg_pnl"] == -22.0


def test_invariant_mismatch_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    snaps = [
        {"status": "open", "leg_pnl": -37.2},
        {"status": "open", "leg_pnl": -0.9},
    ]
    with caplog.at_level(logging.WARNING):
        check_open_leg_pnl_vs_gross(20, snaps, realized=0.0, gross=1.0)
    assert any("leg_pnl/gross mismatch trade_id=20" in r.message for r in caplog.records)


def test_invariant_match_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    snaps = [
        {"status": "open", "leg_pnl": -37.2},
        {"status": "open", "leg_pnl": -0.9},
    ]
    with caplog.at_level(logging.WARNING):
        check_open_leg_pnl_vs_gross(20, snaps, realized=0.0, gross=-38.1)
    assert not any("mismatch" in r.message for r in caplog.records)
