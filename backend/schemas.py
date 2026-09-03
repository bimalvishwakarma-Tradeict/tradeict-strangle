# schemas.py — Pydantic request/response schemas for all API endpoints

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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
    key: str | None = None


class OptionChainResponse(BaseModel):
    current_price: float
    chain: list[dict[str, Any]]


class PayoffResponse(BaseModel):
    price_points: list[float]
    expiry_pnl: list[float]
    breakeven_upper: float
    breakeven_lower: float
    max_profit_usd: float | None = None
    max_loss_usd: float | None = None  # None = unlimited (wings off)
    risk_reward: float | None = None
    wings_on: bool = False
    net_credit_points: float | None = None


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
    # Virtual/demo trade — no real Delta orders
    is_demo: bool = False
    # Combined call+put premium trigger (vs per-leg)
    combined_trigger_mode: bool = False


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
    combined_trigger_mode: bool | None = None


class TradeExitRequest(BaseModel):
    reason: str = "MANUAL_EMERGENCY"


# --- Auto Trade settings (hedge budget fields; full schema in routes_auto_trade) ---


class AutoTradeHedgeBudgetSettings(BaseModel):
    """Hedge fixed SL + floor % + structure target multiple + roll DTE."""

    hedge_fixed_sl_usd: float = Field(default=2.0, ge=0.1, le=1000)
    hedge_sl_floor_pct: float = Field(default=25.0, ge=0, le=100)
    hedge_target_multiple: float = Field(default=3.0, ge=0.5, le=20)
    hedge_expected_monthly_pct: float = Field(default=30.0, ge=1, le=200)
    hedge_min_hold_days: int = Field(default=10, ge=0, le=60)
    min_hedge_dte: int = Field(default=15, ge=0, le=60)
    hedge_roll_dte: int = Field(default=10, ge=1, le=60)
    hedge_roll_hard_dte: int = Field(default=5, ge=1, le=60)

    @model_validator(mode="after")
    def validate_dte_ordering(self) -> AutoTradeHedgeBudgetSettings:
        hard = int(self.hedge_roll_hard_dte)
        roll = int(self.hedge_roll_dte)
        min_dte = int(self.min_hedge_dte)
        if not (hard < roll < min_dte):
            raise ValueError(
                "Require hedge_roll_hard_dte < hedge_roll_dte < min_hedge_dte "
                f"(got hard={hard}, roll={roll}, min={min_dte}). "
                "Roll DTE must be below Minimum hedge DTE, otherwise a newly "
                "opened hedge would immediately start rolling."
            )
        return self


class AutoTradeSpreadSettings(BaseModel):
    """Exit-spread estimation mode + manual % + hard cap."""

    spread_mode: str = Field(default="MANUAL")
    basket_exit_spread_pct: float = Field(default=4.0, ge=0, le=20)
    hedge_exit_spread_pct: float = Field(default=4.0, ge=0, le=20)
    spread_cap_pct: float = Field(default=8.0, ge=0, le=20)

    @field_validator("spread_mode")
    @classmethod
    def validate_spread_mode(cls, v: str) -> str:
        normalized = str(v or "AUTO").upper().strip()
        if normalized not in {"AUTO", "MANUAL"}:
            raise ValueError("spread_mode must be 'AUTO' or 'MANUAL'")
        return normalized


# --- Slave account schemas ---


class SlaveAccountCreate(BaseModel):
    name: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    api_secret: str = Field(..., min_length=1)
    qty_multiplier: float = Field(default=1.0, gt=0, le=100)
    capital_based_qty: bool = False
    user_allocated_capital: float | None = None
    earner_user_id: str | None = None
    earner_subscription_id: str | None = None
    is_virtual: bool = False
    is_active: bool = True


class SlaveAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    api_key: str | None = None
    api_secret: str | None = None
    qty_multiplier: float | None = Field(default=None, gt=0, le=100)
    capital_based_qty: bool | None = None
    user_allocated_capital: float | None = None
    earner_user_id: str | None = None
    earner_subscription_id: str | None = None
    is_virtual: bool | None = None
    is_active: bool | None = None


class SlaveAccountResponse(BaseModel):
    id: int
    name: str
    qty_multiplier: float
    capital_based_qty: bool = False
    user_allocated_capital: float | None = None
    earner_user_id: str | None = None
    earner_subscription_id: str | None = None
    is_virtual: bool = False
    is_active: bool
    connection_status: str
    balance_usd: float
    balance_inr: float
    last_connected_at: str | None = None
    last_error: str | None = None
    active_trade_count: int = 0


class SlaveForceCloseRequest(BaseModel):
    reason: str = Field(
        ...,
        description=(
            "SUBSCRIPTION_CANCELLED | API_DISCONNECTED | ADMIN_FORCE"
        ),
    )
    earner_user_id: str | None = Field(
        default=None,
        description="Alternative lookup when bot slave_id is unknown",
    )

