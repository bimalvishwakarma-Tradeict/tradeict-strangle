# models.py — SQLAlchemy ORM models for accounts, trades, legs, adjustments, settings

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(50), nullable=False, default="S001")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    trades: Mapped[list[Trade]] = relationship("Trade", back_populates="account")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    underlying: Mapped[str] = mapped_column(String(20), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    total_premium_collected: Mapped[float] = mapped_column(Float, nullable=False)
    profit_target_usd: Mapped[float] = mapped_column(Float, nullable=False)
    stoploss_usd: Mapped[float] = mapped_column(Float, nullable=False)
    # Locked at initiation — TP/SL $ are derived from these; never change on adjust
    initial_max_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_pct: Mapped[float | None] = mapped_column(Float, nullable=True, default=50.0)
    sl_pct: Mapped[float | None] = mapped_column(Float, nullable=True, default=100.0)
    # Est. execution slippage applied to Net MTM (and exit decisions)
    slippage_pct: Mapped[float | None] = mapped_column(Float, nullable=True, default=2.0)
    # Entry spread of the *latest* entry event only — added back into
    # gross_mtm_for_stoploss. Reset on each adjustment/conversion (not cumulative).
    entry_spread_for_sl_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=0.0
    )
    # Delta Exchange per-leg stop-loss safety net (% of entry / baseline premium)
    universal_sl_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=200.0
    )
    trigger_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    # False = per-leg trigger; True = combined call+put premium vs combined entry
    combined_trigger_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P&L / adjustment checks only run after this timestamp (settling period)
    monitoring_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Human-facing basket id (sequential per account); clubs all legs/adjustments
    basket_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Incremented on each successful normal adjustment
    adjustment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Conversion Mode State ---
    # True when bot has entered conversion mode (hedge leg is active)
    in_conversion_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # product_id of the hedge leg bought during conversion
    conversion_hedge_product_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # order_id of the hedge leg on Delta Exchange
    conversion_hedge_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    # Entry price paid for the hedge leg
    conversion_hedge_entry_price: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    # Symbol of the hedge leg (e.g. "C-BTC-64800-080826")
    conversion_hedge_symbol: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    # Which leg was triggered when conversion started ("call" or "put")
    conversion_triggered_leg: Mapped[str | None] = mapped_column(
        String(10), nullable=True
    )
    # True = virtual/demo trade — no real Delta orders placed
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    account: Mapped[Account] = relationship("Account", back_populates="trades")
    legs: Mapped[list[Leg]] = relationship("Leg", back_populates="trade")
    adjustments: Mapped[list[Adjustment]] = relationship(
        "Adjustment", back_populates="trade"
    )
    settings: Mapped[list[Setting]] = relationship("Setting", back_populates="trade")


class Leg(Base):
    __tablename__ = "legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), nullable=False)
    # 'call' | 'put' | 'hedge_call' | 'hedge_put'
    leg_type: Mapped[str] = mapped_column(String(20), nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_premium: Mapped[float] = mapped_column(Float, nullable=False)
    # Trigger % baseline — resets after each adjustment (untouched = offer at adj time).
    # initial_premium is the accounting entry price and never changes for that leg row.
    trigger_baseline_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Legacy alias kept in sync with trigger_baseline_premium for older rows/code.
    trigger_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # Realized USD when this leg was closed (adjustment or manual)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Actual Delta trading fees (inc. GST) from fill/order commission — never estimated
    entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Price bot sent to Delta vs actual fill — captures entry execution spread
    order_sent_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_spread_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    # BOT TRADE ISOLATION: order ID from Delta when bot placed this leg
    delta_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Delta stop-loss safety order (buy-to-close when premium spikes)
    delta_sl_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sl_trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Exit order id (buy-to-close / adjust exit) for fee lookup
    exit_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # BOT TRADE ISOLATION: True = bot-placed; never manage manual account positions
    is_bot_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # True = we BOUGHT this leg (hedge); False = we SOLD (normal short option)
    # leg_type: 'call' | 'put' | 'hedge_call' | 'hedge_put'
    is_long: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    trade: Mapped[Trade] = relationship("Trade", back_populates="legs")


class Adjustment(Base):
    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), nullable=False)
    leg_type: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger_pct_reached: Mapped[float] = mapped_column(Float, nullable=False)
    old_strike: Mapped[float] = mapped_column(Float, nullable=False)
    old_exit_premium: Mapped[float] = mapped_column(Float, nullable=False)
    new_strike: Mapped[float] = mapped_column(Float, nullable=False)
    new_entry_premium: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    time_remaining_hours: Mapped[float] = mapped_column(Float, nullable=False)
    slab_used: Mapped[str] = mapped_column(String(50), nullable=False)
    # Audit: ADJUSTED | CLOSED_PROFITABLE (nullable for legacy rows)
    decision_type: Mapped[str | None] = mapped_column(String(40), nullable=True)

    trade: Mapped[Trade] = relationship("Trade", back_populates="adjustments")


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    trade: Mapped[Trade] = relationship("Trade", back_populates="settings")


class AutoTradeSettings(Base):
    """
    Singleton (id=1) config for automatic strangle re-entry after exits.

    Created on first access via get_or_create_auto_settings().
    """

    __tablename__ = "auto_trade_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    underlying: Mapped[str] = mapped_column(String(20), nullable=False, default="BTC")
    expiry_dte: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Weekly/monthly only (DTE>2). Daily 0/1/2DTE uses expiry_dte relative to NOW.
    expiry_date_override: Mapped[str | None] = mapped_column(
        String(10), nullable=True, default=None
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    re_entry_delay_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    # Risk settings
    tp_pct: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    sl_pct: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    universal_sl_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=200.0
    )
    slippage_pct: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)

    # Trigger settings
    trigger_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="slab")
    # False = individual leg trigger (default); True = combined premium trigger
    combined_trigger_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    flat_trigger_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=150.0
    )
    slab_24h: Mapped[float] = mapped_column(Float, nullable=False, default=200.0)
    slab_12h: Mapped[float] = mapped_column(Float, nullable=False, default=175.0)
    slab_6h: Mapped[float] = mapped_column(Float, nullable=False, default=150.0)
    slab_lt6h: Mapped[float] = mapped_column(Float, nullable=False, default=150.0)
    premium_slab_300: Mapped[float] = mapped_column(
        Float, nullable=False, default=150.0
    )
    premium_slab_200: Mapped[float] = mapped_column(
        Float, nullable=False, default=160.0
    )
    premium_slab_100: Mapped[float] = mapped_column(
        Float, nullable=False, default=180.0
    )
    premium_slab_lt100: Mapped[float] = mapped_column(
        Float, nullable=False, default=200.0
    )

    # Trade structure: 'straddle' (ATM) or 'strangle' (OTM premium match)
    trade_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="straddle"
    )
    # Used only when trade_type = 'strangle'
    target_premium_per_side: Mapped[float] = mapped_column(
        Float, nullable=False, default=150.0
    )

    # Conversion Mode settings (low replacement premium → hedge instead of close)
    # When replacement premium < this, enter conversion mode instead of closing
    adj_low_premium_exit_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    adj_low_premium_min_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=150.0
    )
    # Conversion mode reversal detection: close hedge when premiums within X%
    conversion_equality_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=10.0
    )
    # True = conversion allowed when other leg too cheap; False = exit basket instead
    conversion_mode_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # Max normal adjustments per basket when conversion_mode_enabled=False
    # None = unlimited. Ignored entirely when conversion_mode_enabled=True.
    max_adjustments_per_basket: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    premium_cover_loss_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Demo/virtual mode — places virtual trades without real Delta orders
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Status tracking
    last_trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_entry_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Global app config — USD→INR for balance display in Navbar
    usd_inr_rate: Mapped[float] = mapped_column(Float, nullable=False, default=85.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )


class SlaveAccount(Base):
    """
    Secondary Delta account that mirrors master trades (master-slave copy trading).

    qty_multiplier scales master lot size (1.0 = same, 2.0 = double, 0.5 = half).
    is_active=False pauses new mirrored entries without deleting the account.
    """

    __tablename__ = "slave_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    api_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # Trading settings — 1.0 = same qty as master
    qty_multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    # Capital-based qty mode (replaces fixed qty_multiplier when True)
    # When True: qty is calculated dynamically from capital ratio, not qty_multiplier
    capital_based_qty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # User's allocated capital for this strategy (USD) — set by Earner on registration
    user_allocated_capital: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Earner user identifier — links this slave to a Tradeict Earner user
    earner_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Earner subscription identifier — for webhook callbacks
    earner_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Virtual/paper trading mode — no real orders placed on Delta Exchange
    # P&L tracked internally using live market prices
    is_virtual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # True = mirror all trades; False = paused
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Connection status (updated on each API call)
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # "connected" | "error" | "unknown"
    connection_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown"
    )

    # Balance cached from last check
    balance_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    balance_inr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    trades: Mapped[list[SlaveTrade]] = relationship(
        "SlaveTrade", back_populates="slave_account"
    )


class SlaveTrade(Base):
    """Links a master Trade to the mirrored position on a SlaveAccount."""

    __tablename__ = "slave_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slave_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("slave_accounts.id"), nullable=False
    )
    master_trade_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=False
    )

    # Slave-specific order details
    call_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_sl_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_sl_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Actual qty placed on this slave
    actual_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Fill prices on slave (may differ from master)
    call_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # "active" | "closed" | "error" | "partial"
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    # MTM for this slave (from slave's Delta account)
    last_mtm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_updated: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    slave_account: Mapped[SlaveAccount] = relationship(
        "SlaveAccount", back_populates="trades"
    )
    master_trade: Mapped[Trade] = relationship("Trade")
