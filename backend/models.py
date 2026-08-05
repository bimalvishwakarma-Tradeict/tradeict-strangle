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
    # Delta Exchange per-leg stop-loss safety net (% of entry / baseline premium)
    universal_sl_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=200.0
    )
    trigger_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P&L / adjustment checks only run after this timestamp (settling period)
    monitoring_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Human-facing basket id (sequential per account); clubs all legs/adjustments
    basket_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    leg_type: Mapped[str] = mapped_column(String(10), nullable=False)
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
