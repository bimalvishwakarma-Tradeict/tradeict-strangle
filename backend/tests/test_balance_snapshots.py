"""Tests for balance snapshot helpers (B14+)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import anyio
import pytest
import time

from backend.core.balance_utils import compute_daily_growth_pct, wallet_to_balance_fields
from backend.core.delta_client import DeltaClient, _parse_wallet_asset
from backend.core.delta_ws import client_cache_key, get_ws_margins, _ws_margins_cache


def test_compute_daily_growth_pct_positive() -> None:
    assert compute_daily_growth_pct(10.0, 9.0) == 11.11


def test_compute_daily_growth_pct_negative() -> None:
    assert compute_daily_growth_pct(8.77, 9.5) == -7.68


def test_compute_daily_growth_pct_no_yesterday() -> None:
    assert compute_daily_growth_pct(8.77, None) is None
    assert compute_daily_growth_pct(8.77, 0.0) is None


def test_wallet_to_balance_fields_mapping() -> None:
    out = wallet_to_balance_fields(
        {
            "wallet_balance": 8.77,
            "position_margin": 1.75,
            "available_balance": 7.03,
            "unrealised_pnl": 44.44,
            "available_margin": 51.46,
        },
        usd_inr_rate=85.0,
    )
    assert out["actual_balance"] == 8.77
    assert out["blocked_amount"] == 1.75
    assert out["free_cash"] == 7.03
    assert out["available_balance"] == 7.03
    assert out["available_margin"] == 51.46
    assert out["unrealised_pnl"] == 44.44
    assert out["actual_balance_inr"] == 745.0


def test_wallet_available_margin_and_free_cash() -> None:
    wallet = _parse_wallet_asset(
        {
            "balance": "8.78",
            "available_balance": "7.03",
            "blocked_margin": "1.75",
            "unrealized_cashflow": "44.44",
        }
    )
    fields = wallet_to_balance_fields(wallet, usd_inr_rate=85.0)
    assert fields["available_margin"] == pytest.approx(51.47, abs=0.01)
    assert fields["free_cash"] == pytest.approx(7.03)


def test_available_margin_zero_unrealised() -> None:
    wallet = _parse_wallet_asset(
        {
            "balance": "10.0",
            "available_balance": "8.0",
            "blocked_margin": "2.0",
            "unrealized_cashflow": "0",
        }
    )
    assert wallet["available_margin"] == pytest.approx(8.0)
    assert wallet["unrealised_pnl"] == pytest.approx(0.0)


def test_wallet_enriches_unrealised_from_positions() -> None:
    async def _run() -> None:
        client = DeltaClient("test-key", "test-secret")
        parsed = _parse_wallet_asset(
            {
                "balance": "8.78",
                "available_balance": "7.03",
                "blocked_margin": "1.75",
                "asset_symbol": "USD",
            }
        )
        assert parsed["unrealised_pnl_pending"] == 1.0
        with patch(
            "backend.core.delta_client.get_ws_margins",
            return_value=None,
        ):
            with patch.object(
                client,
                "_sum_open_positions_unrealised",
                new_callable=AsyncMock,
                return_value=44.44,
            ):
                enriched = await client._apply_ws_margins(parsed)
        assert enriched["unrealised_pnl"] == pytest.approx(44.44)
        assert enriched["available_margin"] == pytest.approx(51.47, abs=0.01)
        assert enriched["balance_source"] == "rest_computed"
        assert enriched["free_cash"] == pytest.approx(7.03)
        assert "unrealised_pnl_pending" not in enriched

    anyio.run(_run)


def test_ws_margins_cache_hit_uses_ws_available_margin() -> None:
    async def _run() -> None:
        client = DeltaClient("test-key", "test-secret")
        ck = client_cache_key("test-key")
        _ws_margins_cache[ck] = {
            "USD": {
                "available_balance": 45.99,
                "balance": 8.77,
                "blocked_margin": 1.75,
                "ts": time.time(),
            }
        }
        parsed = _parse_wallet_asset(
            {
                "balance": "8.78",
                "available_balance": "7.03",
                "blocked_margin": "1.75",
                "asset_symbol": "USD",
            }
        )
        with patch.object(
            client,
            "_sum_open_positions_unrealised",
            new_callable=AsyncMock,
        ) as mock_sum:
            result = await client._apply_ws_margins(parsed)
            mock_sum.assert_not_called()
        assert result["available_margin"] == pytest.approx(45.99)
        assert result["available_balance"] == pytest.approx(7.03)
        assert result["free_cash"] == pytest.approx(7.03)
        assert result["balance_source"] == "websocket"
        fields = wallet_to_balance_fields(result, usd_inr_rate=85.0)
        assert fields["balance_source"] == "websocket"
        assert fields["free_cash"] == pytest.approx(7.03)

    anyio.run(_run)


def test_ws_margins_cache_stale_falls_back_to_rest() -> None:
    async def _run() -> None:
        client = DeltaClient("test-key", "test-secret")
        ck = client_cache_key("test-key")
        _ws_margins_cache[ck] = {
            "USD": {
                "available_balance": 45.99,
                "balance": 8.77,
                "blocked_margin": 1.75,
                "ts": time.time() - 120.0,
            }
        }
        assert get_ws_margins("USD", cache_key=ck) is None
        parsed = _parse_wallet_asset(
            {
                "balance": "8.78",
                "available_balance": "7.03",
                "blocked_margin": "1.75",
                "asset_symbol": "USD",
            }
        )
        with patch.object(
            client,
            "_sum_open_positions_unrealised",
            new_callable=AsyncMock,
            return_value=44.44,
        ):
            result = await client._apply_ws_margins(parsed)
        assert result["balance_source"] == "rest_computed"
        assert result["available_margin"] == pytest.approx(51.47, abs=0.01)

    anyio.run(_run)


def test_ws_margins_cache_miss_falls_back_to_rest() -> None:
    async def _run() -> None:
        client = DeltaClient("other-key", "other-secret")
        parsed = _parse_wallet_asset(
            {
                "balance": "10.0",
                "available_balance": "8.0",
                "blocked_margin": "2.0",
                "unrealized_cashflow": "0",
            }
        )
        with patch(
            "backend.core.delta_client.get_ws_margins",
            return_value=None,
        ):
            result = await client._apply_ws_margins(parsed)
        assert result["balance_source"] == "rest_computed"
        assert result["available_margin"] == pytest.approx(8.0)

    anyio.run(_run)


def test_seed_margins_cache_from_rest() -> None:
    async def _run() -> None:
        from backend.core.delta_ws import (
            DeltaMarginsWebSocket,
            get_ws_margins,
            _ws_margins_cache,
        )

        ws = DeltaMarginsWebSocket("test-key", "test-secret", "master")
        mock_wallet = [
            {
                "asset_symbol": "USD",
                "balance": "8.78",
                "available_balance": "7.03",
                "blocked_margin": "1.75",
            }
        ]
        with patch(
            "backend.core.delta_client.DeltaClient._request",
            new_callable=AsyncMock,
            return_value=mock_wallet,
        ):
            with patch.object(
                DeltaClient,
                "_sum_open_positions_unrealised",
                new_callable=AsyncMock,
                return_value=44.44,
            ):
                with patch.object(
                    DeltaClient,
                    "close",
                    new_callable=AsyncMock,
                ):
                    await ws._seed_margins_cache_from_rest()

        cached = get_ws_margins("USD", cache_key="master")
        assert cached is not None
        assert cached["available_balance"] == pytest.approx(51.47, abs=0.01)
        assert cached.get("source") == "rest_seed"
        _ws_margins_cache.pop("master", None)

    anyio.run(_run)


def test_parse_wallet_asset_reads_blocked_margin_cross_mode() -> None:
    asset = {
        "balance": "8.7799",
        "available_balance": "7.0278",
        "blocked_margin": "1.7520",
        "position_margin": "0",
        "cross_position_margin": "1.6346",
        "cross_commission": "0.1174",
    }
    out = _parse_wallet_asset(asset)
    assert out["wallet_balance"] == pytest.approx(8.7799)
    assert out["available_balance"] == pytest.approx(7.0278)
    assert out["position_margin"] == pytest.approx(1.7520)


def test_parse_wallet_asset_isolated_mode_fallback() -> None:
    asset = {
        "balance": "10.0",
        "available_balance": "8.0",
        "position_margin": "2.0",
    }
    out = _parse_wallet_asset(asset)
    assert out["position_margin"] == pytest.approx(2.0)
