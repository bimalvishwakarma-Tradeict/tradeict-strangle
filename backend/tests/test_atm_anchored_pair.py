# test_atm_anchored_pair.py — ATM-anchored straddle strike selection (B3)
#
# Run: python -m pytest backend/tests/test_atm_anchored_pair.py -q
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.delta_client import DeltaAPIError, select_atm_anchored_pair


def _row(
    strike: float,
    *,
    call_mark: float,
    put_mark: float,
) -> dict:
    return {
        "strike": strike,
        "call_mark_price": call_mark,
        "put_mark_price": put_mark,
        "call_symbol": f"C-{strike}",
        "put_symbol": f"P-{strike}",
        "call_product_id": int(strike),
        "put_product_id": int(strike) + 1,
    }


def test_picks_best_put_two_strikes_below_atm_not_atm_put() -> None:
    """PUT two strikes below ATM matches call premium better than ATM put."""
    spot = 100_000.0
    chain = [
        _row(98_000, call_mark=120.0, put_mark=149.0),
        _row(99_000, call_mark=130.0, put_mark=150.0),
        _row(100_000, call_mark=150.0, put_mark=140.0),
        _row(101_000, call_mark=160.0, put_mark=130.0),
    ]
    result = select_atm_anchored_pair(chain, spot)
    assert float(result["call_row"]["strike"]) == 100_000.0
    assert float(result["put_row"]["strike"]) == 99_000.0
    assert result["candidates_scanned"] == 3


def test_call_always_at_atm_strike() -> None:
    """Even when a non-ATM row has a closer put match, CALL stays at ATM."""
    spot = 100_000.0
    chain = [
        _row(99_000, call_mark=140.0, put_mark=150.0),
        _row(100_000, call_mark=200.0, put_mark=180.0),
        _row(101_000, call_mark=120.0, put_mark=120.0),
    ]
    result = select_atm_anchored_pair(chain, spot)
    assert float(result["call_row"]["strike"]) == 100_000.0
    assert float(result["put_row"]["strike"]) == 100_000.0


def test_never_picks_put_above_atm_even_if_better_premium_match() -> None:
    spot = 100_000.0
    chain = [
        _row(99_000, call_mark=100.0, put_mark=160.0),
        _row(100_000, call_mark=150.0, put_mark=145.0),
        _row(101_000, call_mark=100.0, put_mark=150.0),
    ]
    result = select_atm_anchored_pair(chain, spot)
    assert float(result["call_row"]["strike"]) == 100_000.0
    assert float(result["put_row"]["strike"]) <= 100_000.0
    assert float(result["put_row"]["strike"]) == 100_000.0


def test_tolerance_warning_still_returns_pair() -> None:
    spot = 100_000.0
    chain = [
        _row(100_000, call_mark=150.0, put_mark=100.0),
    ]
    with patch("backend.core.bot_logger.log_and_buffer") as log_mock:
        result = select_atm_anchored_pair(chain, spot, tolerance_pct=5.0)
    assert float(result["call_row"]["strike"]) == 100_000.0
    assert float(result["put_row"]["strike"]) == 100_000.0
    assert result["premium_diff_pct"] > 5.0
    log_mock.assert_called_once()
    assert log_mock.call_args.args[0] == "ENTRY_PREMIUM_MISMATCH"


def test_empty_chain_raises() -> None:
    with pytest.raises(DeltaAPIError, match="Empty option chain"):
        select_atm_anchored_pair([], 100_000.0)


def test_atm_call_without_premium_raises() -> None:
    spot = 100_000.0
    chain = [_row(100_000, call_mark=0.0, put_mark=150.0)]
    with pytest.raises(DeltaAPIError, match="no call premium"):
        select_atm_anchored_pair(chain, spot)


def test_no_valid_put_candidates_raises() -> None:
    spot = 100_000.0
    chain = [_row(100_000, call_mark=150.0, put_mark=0.0)]
    with pytest.raises(DeltaAPIError, match="No put candidates"):
        select_atm_anchored_pair(chain, spot)
