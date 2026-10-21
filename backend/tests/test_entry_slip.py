# test_entry_slip.py — note-once entry slip + long net without pct slippage

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.entry_slip import (
    build_live_progress_fields,
    compute_entry_slip_ratio,
    compute_entry_slip_usd,
    compute_slip_now_usd,
    long_net_mtm_kwargs,
    maybe_note_entry_slip,
)
from backend.core.fees import compute_net_mtm


def test_entry_slip_first_pnl_notes_ratio() -> None:
    slip = compute_entry_slip_usd(-0.06)
    assert slip == 0.06
    ratio = compute_entry_slip_ratio(0.06, 500.0, 100)
    assert ratio == pytest_approx_0012()
    now = compute_slip_now_usd(ratio, 600.0, 100)
    assert abs(now - 0.072) < 1e-9


def pytest_approx_0012() -> float:
    return 0.06 / (500.0 * 100 * 0.001)


def test_entry_slip_note_once_no_overwrite() -> None:
    now = datetime.now(timezone.utc)
    leg = SimpleNamespace(
        id=None,
        entry_time=now - timedelta(seconds=10),
        initial_premium=500.0,
        quantity=100,
        entry_slip_usd=None,
        entry_slip_ratio=None,
    )
    maybe_note_entry_slip(leg, -0.06, now=now)
    assert abs(leg.entry_slip_usd - 0.06) < 1e-9
    assert abs(leg.entry_slip_ratio - 0.0012) < 1e-9
    maybe_note_entry_slip(leg, -0.10, now=now)
    assert abs(leg.entry_slip_usd - 0.06) < 1e-9
    assert abs(leg.entry_slip_ratio - 0.0012) < 1e-9


def test_entry_slip_outside_window_stays_empty() -> None:
    now = datetime.now(timezone.utc)
    leg = SimpleNamespace(
        id=None,
        entry_time=now - timedelta(seconds=90),
        initial_premium=500.0,
        quantity=100,
        entry_slip_usd=None,
        entry_slip_ratio=None,
    )
    maybe_note_entry_slip(leg, -0.06, now=now)
    assert leg.entry_slip_usd is None
    assert leg.entry_slip_ratio is None


def test_long_net_is_gross_minus_fees_only() -> None:
    out = compute_net_mtm(
        gross_mtm=1.0,
        fees_paid=0.2,
        est_exit_fees=0.2,
        **long_net_mtm_kwargs(),
    )
    assert out["net_mtm"] == 0.6
    assert out["slippage_amount"] == 0.0


def test_short_net_still_deducts_slippage_pct() -> None:
    out = compute_net_mtm(
        gross_mtm=1.0,
        fees_paid=0.2,
        est_exit_fees=0.2,
        slippage_pct=2.0,
        expected_exit_spread_usd=0.0,
    )
    assert out["slippage_amount"] == 0.02
    assert abs(out["net_mtm"] - 0.58) < 1e-9


def test_progress_fields_market_move() -> None:
    trade = SimpleNamespace(
        position_side="long",
        target_usd=10.0,
        sl_usd=5.0,
        profit_target_usd=10.0,
        stoploss_usd=5.0,
        trail_sl_level=None,
    )
    snaps = [
        {
            "quantity": 100,
            "current_premium": 600.0,
            "entry_slip_usd": 0.06,
            "entry_slip_ratio": 0.0012,
        }
    ]
    fields = build_live_progress_fields(trade, gross=1.0, net=0.6, snaps=snaps)
    assert fields["lots"] == 100
    assert abs(fields["slip_now"] - 0.072) < 1e-9
    assert abs(fields["market_move"] - (1.0 + 0.072)) < 1e-9
    assert fields["target_remaining"] == 9.4
    assert fields["sl_distance"] == 6.0
