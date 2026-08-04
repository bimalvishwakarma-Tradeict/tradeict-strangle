# schemas.py — Pydantic request/response schemas for all API endpoints

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AccountConnectRequest(BaseModel):
    name: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)


class AccountConnectResponse(BaseModel):
    success: bool
    account_id: int
    account_name: str
    balance_usdt: float


class AccountStatusResponse(BaseModel):
    connected: bool
    account_name: str = ""
    balance_usdt: float = 0.0
    last_checked: str = ""


class AccountDisconnectResponse(BaseModel):
    success: bool
    message: str


# --- Strategy schemas ---


class ExpiryItem(BaseModel):
    date: str
    label: str
    unix_ts: int


class OptionChainResponse(BaseModel):
    current_price: float
    chain: list[dict[str, Any]]


class PayoffResponse(BaseModel):
    price_points: list[float]
    expiry_pnl: list[float]
    breakeven_upper: float
    breakeven_lower: float


# --- Trade schemas ---


class TradeInitiateRequest(BaseModel):
    """Place a short strangle on Delta, then register for monitoring."""

    underlying: str  # BTC/ETH/XAU
    expiry_date: str  # YYYY-MM-DD
    call_strike: float
    call_product_id: int
    call_symbol: str
    put_strike: float
    put_product_id: int
    put_symbol: str
    quantity: int
    profit_target_usd: float
    stoploss_usd: float
    trigger_mode: str  # "flat" or "slab"
    flat_trigger_pct: float | None = Field(default=None, ge=1, le=500)
    slab_24h: float = Field(default=200.0, ge=1, le=500)
    slab_12h: float = Field(default=175.0, ge=1, le=500)
    slab_6h: float = Field(default=150.0, ge=1, le=500)
    slab_lt6h: float = Field(default=150.0, ge=1, le=500)
    call_delta_at_entry: float | None = None
    put_delta_at_entry: float | None = None


class TradeRegisterExistingRequest(TradeInitiateRequest):
    """Emergency: register an already-open Delta strangle (no new orders)."""

    call_entry_premium: float
    put_entry_premium: float


class TradeSettingsUpdate(BaseModel):
    profit_target_usd: float | None = None
    stoploss_usd: float | None = None
    trigger_mode: str | None = None
    slab_24h: float | None = Field(default=None, ge=1, le=500)
    slab_12h: float | None = Field(default=None, ge=1, le=500)
    slab_6h: float | None = Field(default=None, ge=1, le=500)
    slab_lt6h: float | None = Field(default=None, ge=1, le=500)
    flat_trigger_pct: float | None = Field(default=None, ge=1, le=500)


class TradeExitRequest(BaseModel):
    reason: str = "MANUAL_EMERGENCY"
