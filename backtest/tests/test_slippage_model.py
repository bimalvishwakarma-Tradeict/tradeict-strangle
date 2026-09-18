# Unit tests for backtest/slippage_model.py

from __future__ import annotations

from pathlib import Path

import pytest

from backtest.slippage_model import (
    DEFAULT_CSV,
    SlippageTableError,
    load_slip_table,
    slip_pct,
)


def test_premium_850_dte0_near_s004() -> None:
    load_slip_table(DEFAULT_CSV)
    s = slip_pct(850.0, 0)
    assert abs(s - 0.63) < 0.05, f"got {s}"


def test_premium_50_cheap_bucket() -> None:
    load_slip_table(DEFAULT_CSV)
    s = slip_pct(50.0, 0)
    assert abs(s - 5.66) < 0.05, f"got {s}"


def test_premium_200_dte2_s001_entry() -> None:
    load_slip_table(DEFAULT_CSV)
    s = slip_pct(200.0, 2)
    assert abs(s - 1.39) < 0.05, f"got {s}"


def test_csv_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_calibrate_slippage.csv"
    with pytest.raises(SlippageTableError):
        load_slip_table(missing)
