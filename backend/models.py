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
    # Trigger baseline (may reset after opposite-leg adjustment). Accounting uses initial_premium.
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

    trade: Mapped[Trade] = relationship("Trade", back_populates="adjustments")


class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(Integer, ForeignKey("trades.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    trade: Mapped[Trade] = relationship("Trade", back_populates="settings")
