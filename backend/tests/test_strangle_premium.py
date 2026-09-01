# test_strangle_premium.py — strangle target premium % of hedge (marks)
#
# Run: python -m pytest backend/tests/test_strangle_premium.py -q

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.auto_trade_engine import resolve_strangle_target_premium


def _settings(**kwargs: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "target_premium_per_side": 150.0,
        "strangle_premium_mode": "fixed",
        "strangle_premium_pct_of_hedge": 3.0,
        "hedge_enabled": True,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_fixed_mode_ignores_marks() -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(strangle_premium_mode="fixed"),
        hedge_call_mark=4973.0,
        hedge_put_mark=4297.0,
    )
    assert target == 150.0
    assert used is False


def test_pct_of_hedge_live_example_ceil() -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(strangle_premium_mode="pct_of_hedge", strangle_premium_pct_of_hedge=3.0),
        hedge_call_mark=4973.0,
        hedge_put_mark=4297.0,
    )
    # avg=4635, 3% = 139.05 → ceil 140
    assert target == 140.0
    assert used is True


def test_pct_of_hedge_ceil_rounds_up() -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(
            strangle_premium_mode="pct_of_hedge",
            strangle_premium_pct_of_hedge=3.5,
        ),
        hedge_call_mark=100.0,
        hedge_put_mark=100.0,
    )
    assert target == 4.0  # ceil(100 * 0.035) = ceil(3.5) = 4
    assert used is True


@patch("backend.core.bot_logger.log_and_buffer")
def test_fallback_when_mark_zero(log_mock) -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(strangle_premium_mode="pct_of_hedge"),
        hedge_call_mark=0.0,
        hedge_put_mark=4297.0,
    )
    assert target == 150.0
    assert used is False
    log_mock.assert_called_once()
    assert log_mock.call_args.args[0] == "STRANGLE_PREMIUM_FALLBACK"


@patch("backend.core.bot_logger.log_and_buffer")
def test_fallback_when_mark_none(log_mock) -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(strangle_premium_mode="pct_of_hedge"),
        hedge_call_mark=None,
        hedge_put_mark=None,
    )
    assert target == 150.0
    assert used is False
    log_mock.assert_called_once()


@patch("backend.core.bot_logger.log_and_buffer")
def test_fallback_when_hedge_disabled(log_mock) -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(
            strangle_premium_mode="pct_of_hedge",
            hedge_enabled=False,
        ),
        hedge_call_mark=4973.0,
        hedge_put_mark=4297.0,
    )
    assert target == 150.0
    assert used is False
    log_mock.assert_called_once()
    assert log_mock.call_args.args[1] == 0


def test_high_pct_computes_large_target_without_crash() -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(strangle_premium_mode="pct_of_hedge", strangle_premium_pct_of_hedge=50.0),
        hedge_call_mark=4973.0,
        hedge_put_mark=4297.0,
    )
    assert used is True
    assert target == pytest.approx(2318.0)  # ceil(4635 * 0.5)


@patch("backend.engine.auto_trade_engine.math.ceil", return_value=0)
@patch("backend.core.bot_logger.log_and_buffer")
def test_fallback_when_computed_non_positive(log_mock, _ceil_mock) -> None:
    target, used = resolve_strangle_target_premium(
        settings=_settings(strangle_premium_mode="pct_of_hedge"),
        hedge_call_mark=100.0,
        hedge_put_mark=100.0,
    )
    assert target == 150.0
    assert used is False
    log_mock.assert_called_once()
    assert log_mock.call_args.args[0] == "STRANGLE_PREMIUM_FALLBACK"
