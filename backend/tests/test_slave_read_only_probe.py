# test_slave_read_only_probe.py — refuse read-only keys via an unfillable order
#
# Run: python -m pytest backend/tests/test_slave_read_only_probe.py -q
# (from trading-bot/ root)

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.api.routes_slave import (
    TRADING_PROBE_PRODUCT_ID,
    TRADING_PROBE_SIZE,
    assert_trading_permission,
    create_slave_account,
)
from backend.core.delta_client import DeltaAPIError
from backend.schemas import SlaveAccountCreate


def _payload(*, virtual: bool = False) -> SlaveAccountCreate:
    return SlaveAccountCreate(
        name="earner-readonly-probe",
        api_key="delta-key",
        api_secret="delta-secret",
        is_virtual=virtual,
        is_active=True,
    )


def _db() -> MagicMock:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.count.return_value = 0

    def _refresh(obj: object) -> None:
        setattr(obj, "id", 1)

    db.refresh.side_effect = _refresh
    return db


def _connected_client(
    place_order: AsyncMock,
) -> AsyncMock:
    client = AsyncMock()
    client.test_connection = AsyncMock(
        return_value={"account_name": "probe-acct", "email": "", "id": 1}
    )
    client.get_wallet_balance = AsyncMock(
        return_value={"balance_usdt": 100.0, "available_balance": 100.0}
    )
    client.place_order = place_order
    client.close = AsyncMock()
    client.cancel_order = AsyncMock()
    return client


def _assert_unfillable_probe(place_order: AsyncMock) -> None:
    assert place_order.await_count == 1
    kwargs = place_order.await_args.kwargs
    assert kwargs["product_id"] == TRADING_PROBE_PRODUCT_ID
    assert kwargs["product_id"] == 0
    assert kwargs["size"] == TRADING_PROBE_SIZE
    assert kwargs["size"] == 0
    assert kwargs["side"] == "buy"
    assert kwargs["order_type"] == "market_order"
    assert kwargs["time_in_force"] == "ioc"
    assert kwargs.get("reduce_only") in (None, False)


def test_permission_error_refuses_registration() -> None:
    """Read-only / permission reject → 403 with read_only; slave not persisted."""
    db = _db()
    place_order = AsyncMock(
        side_effect=DeltaAPIError(
            401,
            '{"error":"UnauthorizedApiAccess","message":"Api Key not authorised to access this endpoint"}',
        )
    )
    client = _connected_client(place_order)

    with patch("backend.api.routes_slave.DeltaClient", return_value=client):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(create_slave_account(_payload(), db))

    assert caught.value.status_code == 403
    detail = str(caught.value.detail)
    assert "read_only" in detail
    assert "permission" in detail.lower()
    db.add.assert_not_called()
    db.commit.assert_not_called()
    _assert_unfillable_probe(place_order)


def test_validation_error_allows_registration() -> None:
    """Invalid product/size reject proves trading permission — register."""
    db = _db()
    place_order = AsyncMock(
        side_effect=DeltaAPIError(400, "invalid product_id: 0, size must be greater than 0")
    )
    client = _connected_client(place_order)

    with (
        patch("backend.api.routes_slave.DeltaClient", return_value=client),
        patch("backend.api.routes_slave.encrypt", return_value="enc"),
        patch("backend.api.routes_slave.get_usd_inr_rate", return_value=83.0),
        patch(
            "backend.api.routes_slave.get_utc_now",
            return_value=datetime(2026, 8, 26, tzinfo=timezone.utc),
        ),
    ):
        result = asyncio.run(create_slave_account(_payload(), db))

    db.add.assert_called_once()
    db.commit.assert_called()
    assert result["id"] == 1
    assert result["name"] == "earner-readonly-probe"
    _assert_unfillable_probe(place_order)


def test_probe_args_are_unfillable() -> None:
    """Probe is product_id=0 + size=0 IOC market — cannot fill by construction."""
    place_order = AsyncMock(
        side_effect=DeltaAPIError(400, "invalid product_id")
    )
    client = _connected_client(place_order)
    asyncio.run(assert_trading_permission(client))
    _assert_unfillable_probe(place_order)


def test_probe_forbidden_is_read_only() -> None:
    client = _connected_client(
        AsyncMock(side_effect=DeltaAPIError(403, "Forbidden: trading permission denied"))
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(assert_trading_permission(client))
    assert caught.value.status_code == 403
    assert "read_only" in str(caught.value.detail)
    _assert_unfillable_probe(client.place_order)
