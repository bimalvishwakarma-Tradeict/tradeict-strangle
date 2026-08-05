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
    balance_inr: float = 0.0
    usd_inr_rate: float = 85.0
    last_checked: str = ""


class AccountSettingsUpdate(BaseModel):
    usd_inr_rate: float | None = Field(default=None, gt=0, le=500)


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
    # % of initial max profit — $ targets are computed at initiate and locked
    tp_pct: float = Field(default=50.0, gt=0, le=500)
    sl_pct: float = Field(default=100.0, gt=0, le=500)
    slippage_pct: float = Field(default=2.0, ge=0, le=10)
    # Delta Exchange per-leg stop-market safety net (% of entry premium)
    universal_sl_pct: float = Field(default=200.0, ge=100, le=1000)
    trigger_mode: str  # "flat", "slab", or "premium"
    flat_trigger_pct: float | None = Field(default=None, ge=1, le=500)
    slab_24h: float = Field(default=200.0, ge=1, le=500)
    slab_12h: float = Field(default=175.0, ge=1, le=500)
    slab_6h: float = Field(default=150.0, ge=1, le=500)
    slab_lt6h: float = Field(default=150.0, ge=1, le=500)
    premium_slab_300: float = Field(default=150.0, ge=1, le=500)
    premium_slab_200: float = Field(default=160.0, ge=1, le=500)
    premium_slab_100: float = Field(default=180.0, ge=1, le=500)
    premium_slab_lt100: float = Field(default=200.0, ge=1, le=500)
    call_delta_at_entry: float | None = None
    put_delta_at_entry: float | None = None


class TradeRegisterExistingRequest(TradeInitiateRequest):
    """Emergency: register an already-open Delta strangle (no new orders)."""

    call_entry_premium: float
    put_entry_premium: float


class TradeSettingsUpdate(BaseModel):
    # Prefer % — recalculates locked $ from initial_max_profit
    tp_pct: float | None = Field(default=None, gt=0, le=500)
    sl_pct: float | None = Field(default=None, gt=0, le=500)
    slippage_pct: float | None = Field(default=None, ge=0, le=10)
    universal_sl_pct: float | None = Field(default=None, ge=100, le=1000)
    # Legacy direct $ (discouraged; kept for compatibility)
    profit_target_usd: float | None = None
    stoploss_usd: float | None = None
    trigger_mode: str | None = None
    slab_24h: float | None = Field(default=None, ge=1, le=500)
    slab_12h: float | None = Field(default=None, ge=1, le=500)
    slab_6h: float | None = Field(default=None, ge=1, le=500)
    slab_lt6h: float | None = Field(default=None, ge=1, le=500)
    flat_trigger_pct: float | None = Field(default=None, ge=1, le=500)
    premium_slab_300: float | None = Field(default=None, ge=1, le=500)
    premium_slab_200: float | None = Field(default=None, ge=1, le=500)
    premium_slab_100: float | None = Field(default=None, ge=1, le=500)
    premium_slab_lt100: float | None = Field(default=None, ge=1, le=500)


class TradeExitRequest(BaseModel):
    reason: str = "MANUAL_EMERGENCY"


# --- Slave account schemas ---


class SlaveAccountCreate(BaseModel):
    name: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)
    qty_multiplier: float = Field(default=1.0, gt=0, le=100)
    is_active: bool = True


class SlaveAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    api_key: str | None = None
    api_secret: str | None = None
    qty_multiplier: float | None = Field(default=None, gt=0, le=100)
    is_active: bool | None = None


class SlaveAccountResponse(BaseModel):
    id: int
    name: str
    qty_multiplier: float
    is_active: bool
    connection_status: str
    balance_usd: float
    balance_inr: float
    last_connected_at: str | None = None
    last_error: str | None = None
    active_trade_count: int = 0

