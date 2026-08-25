# test_slave_sizing.py — capital-based qty must never fall through to multiplier
#
# Run: python -m pytest backend/tests/test_slave_sizing.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import MAX_SLAVE_QTY
from backend.engine.mirror_engine import MirrorEngine


def _capital_slave(
    *,
    allocated: float,
    multiplier: float = 1.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        capital_based_qty=True,
        user_allocated_capital=allocated,
        is_virtual=True,
        qty_multiplier=multiplier,
    )


def _fixed_slave(*, multiplier: float = 2.0) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        capital_based_qty=False,
        user_allocated_capital=0,
        is_virtual=True,
        qty_multiplier=multiplier,
    )


def test_capital_based_skips_when_master_capital_unreadable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fetch failure / None capital → qty 0; multiplier branch must not run."""
    import logging

    engine = MirrorEngine(db_factory=lambda: None)
    slave = _capital_slave(allocated=300.0)

    with caplog.at_level(logging.WARNING, logger="backend.engine.mirror_engine"):
        qty = engine._calc_qty(
            10,
            1.0,
            slave=slave,
            master_margin_used_usd=None,
            master_total_capital_usd=None,
            slave_available_usd=20_000.0,
            master_capital_fetch_failed=True,
        )
    assert qty == 0
    assert qty != 10  # would be master×1.0 if multiplier fallthrough ran
    assert any(
        "never fall through to multiplier" in r.message for r in caplog.records
    )
    assert not any("mode=multiplier" in r.message for r in caplog.records)


def test_capital_based_none_capital_without_flag_still_skips() -> None:
    engine = MirrorEngine(db_factory=lambda: None)
    slave = _capital_slave(allocated=300.0)
    qty = engine._calc_qty(
        10,
        1.0,
        slave=slave,
        master_margin_used_usd=None,
        master_total_capital_usd=None,
        slave_available_usd=20_000.0,
        master_capital_fetch_failed=False,
    )
    assert qty == 0


def test_capital_based_floors_half_lot_to_zero() -> None:
    """Allocation supporting 0.51 lots → 0, never round up to 1."""
    engine = MirrorEngine(db_factory=lambda: None)
    # per_lot = 1000/10 = 100; ratio = 0.1; margin_to_use = 510*0.1 = 51 → 0.51 lots
    slave = _capital_slave(allocated=510.0)
    qty = engine._calc_qty(
        10,
        1.0,
        slave=slave,
        master_margin_used_usd=1000.0,
        master_total_capital_usd=10_000.0,
        slave_available_usd=510.0,
        master_capital_fetch_failed=False,
    )
    assert qty == 0


def test_capital_based_floors_two_point_nine_to_two() -> None:
    """Allocation supporting 2.9 lots → 2, never round up to 3."""
    engine = MirrorEngine(db_factory=lambda: None)
    # per_lot=100; ratio=0.1; need margin_to_use=290 → effective=2900
    slave = _capital_slave(allocated=2900.0)
    qty = engine._calc_qty(
        10,
        1.0,
        slave=slave,
        master_margin_used_usd=1000.0,
        master_total_capital_usd=10_000.0,
        slave_available_usd=2900.0,
        master_capital_fetch_failed=False,
    )
    assert qty == 2


def test_fixed_multiplier_unaffected_by_missing_master_capital() -> None:
    engine = MirrorEngine(db_factory=lambda: None)
    slave = _fixed_slave(multiplier=2.0)
    qty = engine._calc_qty(
        5,
        2.0,
        slave=slave,
        master_margin_used_usd=None,
        master_total_capital_usd=None,
        slave_available_usd=None,
        master_capital_fetch_failed=True,
    )
    assert qty == 10


def test_qty_never_exceeds_max_slave_qty() -> None:
    engine = MirrorEngine(db_factory=lambda: None)
    slave = _capital_slave(allocated=1_000_000.0)
    qty = engine._calc_qty(
        10,
        1.0,
        slave=slave,
        master_margin_used_usd=1000.0,
        master_total_capital_usd=10_000.0,
        slave_available_usd=1_000_000.0,
        master_capital_fetch_failed=False,
    )
    assert qty <= int(MAX_SLAVE_QTY)
    assert qty == int(MAX_SLAVE_QTY)
