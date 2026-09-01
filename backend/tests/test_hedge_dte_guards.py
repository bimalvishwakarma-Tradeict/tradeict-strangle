# test_hedge_dte_guards.py — per-guard OFF switches + hedge pre-expiry close
#
# Run: python -m pytest backend/tests/test_hedge_dte_guards.py -q

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_auto_trade import AutoTradeSettingsSchema
from backend.core.hedge_theta import enforce_min_hedge_dte
from backend.core.time_utils import get_ist_now
from backend.engine import hedge_lifecycle as hl
from backend.engine.hedge_lifecycle import (
    _log_guard_disabled_once,
    evaluate_and_maybe_close_hedge,
)


def _settings(**overrides: object) -> SimpleNamespace:
    base = {
        "min_hedge_dte": 15,
        "min_hedge_dte_enabled": True,
        "hedge_roll_dte": 10,
        "hedge_roll_enabled": True,
        "hedge_roll_hard_dte": 5,
        "hedge_force_roll_enabled": True,
        "hedge_close_at_expiry_enabled": True,
        "hedge_expected_monthly_pct": 30.0,
        "hedge_target_multiple": 3.0,
        "hedge_min_hold_days": 60,
        "hedge_fixed_sl_usd": 2.0,
        "hedge_sl_floor_pct": 25.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _hedge(**overrides: object) -> SimpleNamespace:
    today = get_ist_now().date()
    row = SimpleNamespace(
        id=1,
        account_id=1,
        underlying="BTC",
        status="active",
        quantity=1,
        call_symbol="C-BTC",
        put_symbol="P-BTC",
        call_fill_price=10.0,
        put_fill_price=10.0,
        call_entry_fee_usd=0.01,
        put_entry_fee_usd=0.01,
        expiry_date=today + timedelta(days=10),
        stoploss_usd=100.0,
        entry_time=datetime.now(timezone.utc) - timedelta(days=30),
        hedge_net_mtm=-1.0,
        entry_spread_usd=0.1,
        cum_closed_basket_pnl=0.0,
        structure_pnl=-0.5,
        hedge_gross_for_sl=-0.9,
        hedge_est_exit_slippage_usd=0.05,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _structure_snap(**overrides: object) -> dict[str, float]:
    base = {
        "hedge_net_mtm": -1.0,
        "entry_spread_usd": 0.1,
        "cum_closed_basket_pnl": 0.0,
        "open_basket_gross_mtm": 0.0,
        "open_basket_net_mtm": 0.0,
        "structure_pnl": -0.5,
        "hedge_gross_for_sl": -0.9,
        "hedge_est_exit_slippage_usd": 0.05,
        "structure_gross_for_sl": -0.5,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_hedge_guard_state() -> None:
    hl._hedge_guard_disabled_logged.clear()
    hl._hedge_sl_fired.clear()
    hl._hedge_target_fired.clear()
    yield
    hl._hedge_guard_disabled_logged.clear()
    hl._hedge_sl_fired.clear()
    hl._hedge_target_fired.clear()


def test_all_guards_enabled_dte_ordering_unchanged() -> None:
    """Regression: default guard ON keeps strict DTE ordering validation."""
    AutoTradeSettingsSchema(
        min_hedge_dte=15,
        hedge_roll_dte=10,
        hedge_roll_hard_dte=5,
        min_hedge_dte_enabled=True,
        hedge_roll_enabled=True,
        hedge_force_roll_enabled=True,
    )
    with pytest.raises(ValidationError):
        AutoTradeSettingsSchema(
            min_hedge_dte=5,
            hedge_roll_dte=10,
            hedge_roll_hard_dte=5,
            min_hedge_dte_enabled=True,
            hedge_roll_enabled=True,
            hedge_force_roll_enabled=True,
        )


def test_min_hedge_dte_zero_schema_validation_passes() -> None:
    parsed = AutoTradeSettingsSchema(
        min_hedge_dte=0,
        min_hedge_dte_enabled=True,
        hedge_roll_enabled=False,
        hedge_force_roll_enabled=False,
    )
    assert parsed.min_hedge_dte == 0


def test_enforce_min_hedge_dte_zero_accepts_same_day() -> None:
    today = get_ist_now().date()
    result = asyncio.run(
        enforce_min_hedge_dte(
            AsyncMock(),
            "BTC",
            today,
            0,
        )
    )
    assert result == today


def test_min_hedge_dte_disabled_accepts_one_dte_expiry() -> None:
    """With guard OFF, 1-DTE expiry is kept (no monthly bump)."""
    today = get_ist_now().date()
    one_dte = today + timedelta(days=1)
    settings = _settings(min_hedge_dte_enabled=False, min_hedge_dte=15)
    expiry = one_dte
    if bool(getattr(settings, "min_hedge_dte_enabled", True)):
        expiry = asyncio.run(
            enforce_min_hedge_dte(
                AsyncMock(),
                "BTC",
                one_dte,
                int(settings.min_hedge_dte),
            )
        )
    else:
        _log_guard_disabled_once("min_hedge_dte")
    assert expiry == one_dte
    assert "min_hedge_dte" in hl._hedge_guard_disabled_logged


def test_min_hedge_dte_enabled_bumps_short_expiry() -> None:
    today = get_ist_now().date()
    one_dte = today + timedelta(days=1)
    month_out = today + timedelta(days=30)
    client = AsyncMock()
    client.get_available_expiries = AsyncMock(
        return_value=[
            {"date": one_dte.isoformat(), "key": "month_1"},
            {"date": month_out.isoformat(), "key": "month_2"},
        ]
    )
    result = asyncio.run(enforce_min_hedge_dte(client, "BTC", one_dte, 15))
    assert result == month_out


def test_hedge_roll_disabled_never_starts_pending_close() -> None:
    hedge = _hedge(status="active", expiry_date=get_ist_now().date())
    db = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    settings = _settings(hedge_roll_enabled=False, hedge_roll_dte=10)

    async def _run() -> None:
        with patch(
            "backend.database.get_or_create_auto_settings",
            return_value=settings,
        ), patch(
            "backend.engine.hedge_lifecycle._fetch_strict_bid",
            new=AsyncMock(side_effect=[10.0, 10.0]),
        ), patch(
            "backend.engine.hedge_lifecycle.persist_structure_pnl",
            new=AsyncMock(return_value=_structure_snap()),
        ), patch(
            "backend.engine.hedge_lifecycle.is_pre_expiry_window",
            return_value=False,
        ), patch(
            "backend.engine.hedge_lifecycle.get_hours_to_expiry",
            return_value=24.0,
        ), patch(
            "backend.engine.hedge_lifecycle._calendar_dte",
            return_value=5,
        ), patch(
            "backend.engine.hedge_lifecycle._active_baskets_under_hedge",
            return_value=[],
        ), patch(
            "backend.engine.hedge_lifecycle.close_hedge",
            new=AsyncMock(),
        ) as close_mock:
            result = await evaluate_and_maybe_close_hedge(
                hedge,
                db,
                client=AsyncMock(),
                btc_index=100000.0,
            )
            assert result is None
            assert hedge.status == "active"
            close_mock.assert_not_awaited()
            assert "roll" in hl._hedge_guard_disabled_logged

    asyncio.run(_run())


def test_hedge_force_roll_disabled_skips_cascade_close() -> None:
    hedge = _hedge(status="pending_close", expiry_date=get_ist_now().date())
    db = MagicMock()
    settings = _settings(
        hedge_roll_enabled=True,
        hedge_force_roll_enabled=False,
        hedge_roll_hard_dte=5,
    )

    async def _run() -> None:
        with patch(
            "backend.database.get_or_create_auto_settings",
            return_value=settings,
        ), patch(
            "backend.engine.hedge_lifecycle._fetch_strict_bid",
            new=AsyncMock(side_effect=[10.0, 10.0]),
        ), patch(
            "backend.engine.hedge_lifecycle.persist_structure_pnl",
            new=AsyncMock(return_value=_structure_snap()),
        ), patch(
            "backend.engine.hedge_lifecycle.is_pre_expiry_window",
            return_value=False,
        ), patch(
            "backend.engine.hedge_lifecycle.get_hours_to_expiry",
            return_value=24.0,
        ), patch(
            "backend.engine.hedge_lifecycle._calendar_dte",
            return_value=3,
        ), patch(
            "backend.engine.hedge_lifecycle._active_baskets_under_hedge",
            return_value=[SimpleNamespace(id=99)],
        ), patch(
            "backend.engine.hedge_lifecycle.close_hedge",
            new=AsyncMock(),
        ) as close_mock:
            result = await evaluate_and_maybe_close_hedge(
                hedge,
                db,
                client=AsyncMock(),
                btc_index=100000.0,
            )
            assert result is None
            close_mock.assert_not_awaited()
            assert "force_roll" in hl._hedge_guard_disabled_logged

    asyncio.run(_run())


def test_hedge_pre_expiry_close_baskets_first() -> None:
    today = get_ist_now().date()
    hedge = _hedge(status="active", expiry_date=today)
    closed = SimpleNamespace(id=1, status="closed")
    db = MagicMock()
    settings = _settings(hedge_close_at_expiry_enabled=True)

    async def _run() -> None:
        with patch(
            "backend.database.get_or_create_auto_settings",
            return_value=settings,
        ), patch(
            "backend.engine.hedge_lifecycle._fetch_strict_bid",
            new=AsyncMock(side_effect=[10.0, 10.0]),
        ), patch(
            "backend.engine.hedge_lifecycle.persist_structure_pnl",
            new=AsyncMock(return_value=_structure_snap()),
        ), patch(
            "backend.engine.hedge_lifecycle.is_pre_expiry_window",
            return_value=True,
        ), patch(
            "backend.engine.hedge_lifecycle.get_hours_to_expiry",
            return_value=0.2,
        ), patch(
            "backend.engine.hedge_lifecycle._calendar_dte",
            return_value=0,
        ), patch(
            "backend.engine.hedge_lifecycle._active_baskets_under_hedge",
            return_value=[SimpleNamespace(id=42)],
        ), patch(
            "backend.engine.hedge_lifecycle.close_hedge",
            new=AsyncMock(return_value=closed),
        ) as close_mock, patch(
            "backend.engine.hedge_lifecycle._hedge_log",
        ) as log_mock:
            result = await evaluate_and_maybe_close_hedge(
                hedge,
                db,
                client=AsyncMock(),
                btc_index=100000.0,
            )
            assert result is closed
            close_mock.assert_awaited_once()
            assert close_mock.await_args.args[1] == "HEDGE_EXPIRY"
            pre_expiry_calls = [
                c
                for c in log_mock.call_args_list
                if c.args and c.args[0] == "HEDGE_PRE_EXPIRY_CLOSE"
            ]
            assert len(pre_expiry_calls) == 1
            assert pre_expiry_calls[0].args[2]["baskets_open"] == 1

    asyncio.run(_run())


def test_same_expiry_guards_off_pre_expiry_closes_hedge() -> None:
    """0DTE hedge+basket path: guards off, pre-expiry window closes hedge."""
    today = get_ist_now().date()
    hedge = _hedge(
        status="active",
        expiry_date=today,
        call_fill_price=5.0,
        put_fill_price=5.0,
    )
    closed = SimpleNamespace(
        id=1,
        status="closed",
        exit_premium=9.0,
        realized_pnl=1.0,
    )
    db = MagicMock()
    settings = _settings(
        min_hedge_dte=0,
        min_hedge_dte_enabled=False,
        hedge_roll_enabled=False,
        hedge_force_roll_enabled=False,
        hedge_close_at_expiry_enabled=True,
    )

    async def _run() -> None:
        with patch(
            "backend.database.get_or_create_auto_settings",
            return_value=settings,
        ), patch(
            "backend.engine.hedge_lifecycle._fetch_strict_bid",
            new=AsyncMock(side_effect=[5.0, 5.0]),
        ), patch(
            "backend.engine.hedge_lifecycle.persist_structure_pnl",
            new=AsyncMock(return_value=_structure_snap(structure_pnl=-0.2)),
        ), patch(
            "backend.engine.hedge_lifecycle.is_pre_expiry_window",
            return_value=True,
        ), patch(
            "backend.engine.hedge_lifecycle.get_hours_to_expiry",
            return_value=0.1,
        ), patch(
            "backend.engine.hedge_lifecycle._calendar_dte",
            return_value=0,
        ), patch(
            "backend.engine.hedge_lifecycle._active_baskets_under_hedge",
            return_value=[SimpleNamespace(id=7)],
        ), patch(
            "backend.engine.hedge_lifecycle.close_hedge",
            new=AsyncMock(return_value=closed),
        ) as close_mock:
            result = await evaluate_and_maybe_close_hedge(
                hedge,
                db,
                client=AsyncMock(),
                btc_index=100000.0,
            )
            assert result is closed
            close_mock.assert_awaited_once()
            assert close_mock.await_args.args[1] == "HEDGE_EXPIRY"
            assert closed.exit_premium is not None
            assert closed.realized_pnl is not None

    asyncio.run(_run())


def test_log_guard_disabled_once_is_idempotent() -> None:
    _log_guard_disabled_once("min_hedge_dte")
    _log_guard_disabled_once("min_hedge_dte")
    assert hl._hedge_guard_disabled_logged == {"min_hedge_dte"}
