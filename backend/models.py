# models.py — SQLAlchemy ORM models for accounts, trades, legs, adjustments, settings

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base
from backend.core.time_utils import get_utc_now


def _utc_now() -> datetime:
    """ORM default — always UTC via shared helper."""
    return get_utc_now()


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
    hedge_positions: Mapped[list[HedgePosition]] = relationship(
        "HedgePosition", back_populates="account"
    )


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
    # Entry settling end (set once at entry). Never pushed on adjust/conversion.
    # STOPLOSS ignores this; TP / adjust triggers respect it.
    monitoring_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Short post-adjust/conversion settling end (UTC). Independent of entry window.
    adjust_settling_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Human-facing basket id (sequential per account); clubs all legs/adjustments
    basket_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-structure basket index (1..N under one hedge_position_id). Never renumbered.
    basket_seq_in_structure: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
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
    # Optional link to a long-lived hedge (monthly ATM straddle). NULL = no hedge.
    hedge_position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("hedge_positions.id"), nullable=True
    )
    # How profit_target_usd was set at entry: THETA | PCT
    target_source: Mapped[str | None] = mapped_column(
        String(10), nullable=True, default=None
    )
    # Hedge TOTAL straddle theta per BTC locked at basket entry (THETA mode)
    hedge_theta_at_entry: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None
    )
    # pct_of_hedge + dynamic: formula result used at entry (audit)
    basket_qty_computed_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None
    )
    # strangle pct_of_hedge mode: computed target $/side at entry (audit)
    strangle_premium_computed_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None
    )
    # Deploy qty at first entry — fixed base for decrease_step adjustment mode
    original_basket_qty: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    # Wings debit paid at entry (USD) — audit vs net initial_max_profit
    wing_premium_paid_usd: Mapped[float | None] = mapped_column(
        Float, nullable=True, default=None
    )

    account: Mapped[Account] = relationship("Account", back_populates="trades")
    hedge_position: Mapped[HedgePosition | None] = relationship(
        "HedgePosition", back_populates="baskets"
    )
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
    # True = we BOUGHT this leg (hedge / wing); False = we SOLD (normal short option)
    # leg_type: 'call' | 'put' | 'hedge_call' | 'hedge_put' | 'wing_call' | 'wing_put'
    is_long: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # BUY | SELL — wings and structure hedges are BUY; short basket legs are SELL
    side: Mapped[str] = mapped_column(
        String(4), nullable=False, default="SELL", server_default="SELL"
    )

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
    # Settling windows (seconds). 0 = disabled for that window.
    entry_settling_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60
    )
    adjustment_settling_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20
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
    # strangle: fixed $ per side vs % of live hedge premium (mark, not entry fill)
    strangle_premium_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fixed"
    )
    strangle_premium_pct_of_hedge: Mapped[float] = mapped_column(
        Float, nullable=False, default=3.0
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

    # --- Hedge mode (config only until engine steps land) ---
    # Defaults preserve today's behaviour: hedge off, fixed premium, payoff %.
    hedge_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # monthly | date | dte  (legacy) — now stores relative keys: month_1, week_2, 1dte
    hedge_expiry_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default="month_1"
    )
    hedge_expiry_date_override: Mapped[str | None] = mapped_column(
        String(10), nullable=True, default=None
    )
    hedge_expiry_dte: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # If resolved hedge expiry has fewer days than this, advance to next monthly
    min_hedge_dte: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    hedge_target_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    hedge_stoploss_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Fixed $ SL for budget formula (floor = fixed × floor_pct / 100)
    hedge_fixed_sl_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=2.0
    )
    # Minimum budget as % of fixed SL — prevents negative budget after basket losses
    hedge_sl_floor_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=25.0
    )
    # Start roll countdown when calendar DTE <= this (mark pending_close)
    hedge_roll_dte: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # Force-close hedge (cascade) when calendar DTE <= this while pending_close
    hedge_roll_hard_dte: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5
    )
    min_hedge_dte_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    hedge_roll_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    hedge_force_roll_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    hedge_close_at_expiry_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # After HEDGE_ROLL close, open the next monthly automatically
    hedge_auto_reopen_after_roll: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    hedge_target_multiple: Mapped[float] = mapped_column(
        Float, nullable=False, default=3.0
    )
    # Expected monthly earnings as % of hedge entry cost (for target_usd)
    hedge_expected_monthly_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=30.0
    )
    # Do not book target until held at least this many days (spread amortisation)
    hedge_min_hold_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10
    )
    # Exit-spread estimate: MANUAL default (AUTO under-estimates on thin books)
    spread_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="MANUAL"
    )
    basket_exit_spread_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=4.0
    )
    hedge_exit_spread_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=4.0
    )
    spread_cap_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=8.0
    )
    margin_buffer_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=50.0
    )
    # fixed_premium | theta_based
    strike_selection_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default="fixed_premium"
    )
    theta_multiplier: Mapped[float] = mapped_column(
        Float, nullable=False, default=3.0
    )
    # payoff_pct | theta_multiplier  (legacy basket target UI / preview)
    target_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default="payoff_pct"
    )
    target_theta_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=150.0
    )
    # Live basket profit target: THETA (hedge total theta × multiple) | PCT
    basket_target_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="THETA"
    )
    basket_target_multiple: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.5
    )
    # hedge lots = short basket qty × this ratio (default 1:1)
    hedge_qty_ratio: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )
    # 'fixed' = use quantity + hedge_qty_ratio (current behaviour)
    # 'pct_of_hedge' = basket qty derived from hedge_qty_lots × pct
    basket_qty_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fixed"
    )
    # Used when basket_qty_mode='pct_of_hedge': basket lots = ceil(hedge × pct / 100)
    basket_qty_pct_of_hedge: Mapped[float] = mapped_column(
        Float, nullable=False, default=20.0
    )
    # Used when basket_qty_mode='pct_of_hedge': fixed hedge long-straddle lot count
    hedge_qty_lots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When True + pct_of_hedge: derive basket % from hedge call theta at entry
    basket_qty_dynamic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )
    basket_qty_theta_mult: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="2.0", default=2.0
    )
    # Deprecated: migrated to adjustment_qty_mode; keep for rollback safety
    use_dynamic_qty_on_adjustment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )
    # unchanged | increase_dynamic | decrease_step
    adjustment_qty_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="unchanged",
        default="unchanged",
    )
    # Used when adjustment_qty_mode='decrease_step' (pct of original each adj)
    adjustment_qty_decrease_pct: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="25.0", default=25.0
    )
    basket_decay_exit_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )
    basket_decay_exit_pct: Mapped[float] = mapped_column(
        Float, nullable=False, server_default="50.0", default=50.0
    )
    basket_decay_exit_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="both_legs", default="both_legs"
    )
    cooldown_after_loss_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120
    )
    # Max allowed |selected − target| / target × 100 on adjustment strike pick
    adjustment_premium_tolerance_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=40.0
    )
    # Max |put − call| / call × 100 at theta entry; warn only, still enter
    entry_premium_match_tolerance_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=25.0
    )
    # Basket wings (long far-OTM legs → iron condor). Default OFF — no engine wiring yet.
    basket_wings_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    # points | delta | pct_of_premium
    wing_strike_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="points", server_default="points"
    )
    wing_points_away: Mapped[float] = mapped_column(
        Float, nullable=False, default=2000.0, server_default="2000.0"
    )
    wing_delta_min: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.05, server_default="0.05"
    )
    wing_delta_max: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.07, server_default="0.07"
    )
    wing_pct_of_premium: Mapped[float] = mapped_column(
        Float, nullable=False, default=20.0, server_default="20.0"
    )
    # Mid-price chase / urgent ladder (default OFF — safe rollout)
    midprice_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    midprice_chase_max_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120"
    )
    midprice_hold_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    midprice_partner_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
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
    # Why next_entry_time was set — only 'reentry_delay' is recomputed on
    # settings save. Values: reentry_delay | cooldown_after_loss | retry |
    # hedge_gate | expiry_too_close
    next_entry_source: Mapped[str | None] = mapped_column(
        String(40), nullable=True
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
    hedge_positions: Mapped[list[SlaveHedgePosition]] = relationship(
        "SlaveHedgePosition", back_populates="slave_account"
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

    # Live contract identity — required for per-slave P&L (next task)
    call_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    put_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_strike: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Iron-condor wings (mirrored from master strikes; qty = actual_quantity)
    wing_call_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wing_put_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wing_call_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wing_put_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wing_call_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    wing_put_strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    wing_call_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wing_put_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wing_call_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    wing_put_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Deploy qty at first entry — fixed base for decrease_step on THIS slave
    original_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when a wing close failed (CRITICAL — surface on frontend)
    wing_close_failed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    call_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_exit_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_exit_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_spread_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_mtm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 'computed' | 'copied' — UI can tell which figure it is during transition
    mtm_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="copied"
    )

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


class HedgePosition(Base):
    """
    Permanent long ATM straddle (typically monthly) held alongside daily short baskets.

    Lives outside trades/legs so basket exits cannot close the hedge.
    """

    __tablename__ = "hedge_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    underlying: Mapped[str] = mapped_column(String(20), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # active | pending_close | closed | partial | error | exit_failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    call_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    put_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    put_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    entry_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    stoploss_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Snapshot at purchase — reporting only
    entry_total_theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_call_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_put_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Master margin per lot — used later for slave sizing
    order_margin_per_lot: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Long-hedge MTM mirrors short-basket net_mtm / gross_mtm_for_stoploss
    # (calculation + logging only — no exit triggers use these yet)
    entry_spread_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hedge_net_mtm: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hedge_mtm_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    hedge_gross_for_sl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    hedge_est_exit_slippage_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    structure_gross_for_sl: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    cum_closed_basket_pnl: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    structure_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 'live' = open/estimated; 'realized' = closed from actual fills;
    # 'reconstructed' = backfilled from fill/exit prices (pre-instrumentation)
    hedge_net_source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="live"
    )

    is_bot_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    account: Mapped[Account] = relationship(
        "Account", back_populates="hedge_positions"
    )
    baskets: Mapped[list[Trade]] = relationship(
        "Trade", back_populates="hedge_position"
    )
    slave_hedges: Mapped[list[SlaveHedgePosition]] = relationship(
        "SlaveHedgePosition", back_populates="master_hedge"
    )
    theta_logs: Mapped[list[HedgeThetaLog]] = relationship(
        "HedgeThetaLog", back_populates="hedge"
    )


class SlaveHedgePosition(Base):
    """
    Mirrored hedge on a slave account.

    Separate from slave_trades: a basket may close many times while its hedge
    stays open — mixing lifecycles would close the hedge on basket exit.
    """

    __tablename__ = "slave_hedge_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=False
    )
    slave_account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("slave_accounts.id"), nullable=False
    )
    master_hedge_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("hedge_positions.id"), nullable=False
    )

    underlying: Mapped[str] = mapped_column(String(20), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # active | closed | partial | error | exit_failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    call_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    call_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    put_product_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    put_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    put_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_entry_fee_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    entry_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    stoploss_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    entry_total_theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_call_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_put_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_margin_per_lot: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Half-spread at open (scaled from master) + total debit paid
    entry_spread_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    entry_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Live hedge net MTM — persisted on master hedge monitor cadence (same
    # compute_hedge_net_mtm_fields as HedgePosition.hedge_net_mtm). Never copy master.
    hedge_net_mtm: Mapped[float | None] = mapped_column(Float, nullable=True)
    hedge_mtm_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_bot_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    allocated_capital: Mapped[float | None] = mapped_column(Float, nullable=True)
    capital_per_lot: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    slave_account: Mapped[SlaveAccount] = relationship(
        "SlaveAccount", back_populates="hedge_positions"
    )
    master_hedge: Mapped[HedgePosition] = relationship(
        "HedgePosition", back_populates="slave_hedges"
    )


class HedgeThetaLog(Base):
    """Daily theta / IV snapshot for a hedge (one row per hedge per IST day)."""

    __tablename__ = "hedge_theta_log"
    __table_args__ = (
        UniqueConstraint("hedge_id", "log_date", name="uq_hedge_theta_log_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hedge_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("hedge_positions.id"), nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    call_theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_theta: Mapped[float | None] = mapped_column(Float, nullable=True)
    spot_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    call_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    put_iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    hedge: Mapped[HedgePosition] = relationship(
        "HedgePosition", back_populates="theta_logs"
    )


class Structure(Base):
    """
    One structure per account per hedge (MASTER + each funded SLAVE).

    Recording-only ledger for earner attribution — no P&L stored here.
    """

    __tablename__ = "structures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # MASTER | SLAVE
    account_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    slave_account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("slave_accounts.id"), nullable=True
    )
    # Copied from slave_accounts at SLAVE structure creation
    earner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Master hedge this structure belongs to (same id for master + slaves)
    hedge_position_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("hedge_positions.id"), nullable=False
    )
    underlying: Mapped[str] = mapped_column(String(20), nullable=False)
    # active | closed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    close_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Set when structure is closed while one or more legs lacked a proper closed_at
    # (earner treats as SUSPECT attribution). NULL = clean close.
    attribution_warning: Mapped[str | None] = mapped_column(Text, nullable=True)

    legs: Mapped[list["StructureLeg"]] = relationship(
        "StructureLeg", back_populates="structure"
    )


class StructureLeg(Base):
    """
    One bot-placed leg window. Attribution key = (product_id, opened_at..closed_at).
    """

    __tablename__ = "structure_legs"
    __table_args__ = (
        Index("ix_structure_legs_product_opened", "product_id", "opened_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    structure_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("structures.id"), nullable=False
    )
    # HEDGE_CALL | HEDGE_PUT | BASKET_CALL | BASKET_PUT
    # | BASKET_WING_CALL | BASKET_WING_PUT
    leg_role: Mapped[str] = mapped_column(String(20), nullable=False)
    basket_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 0 = original, 1..n = adjustments
    adj_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    # BUY | SELL
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Earner attribution window start — NULL means use opened_at (earner-side fallback)
    attribution_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    close_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    structure: Mapped[Structure] = relationship("Structure", back_populates="legs")


class BalanceSnapshot(Base):
    """Daily noon IST wallet_balance snapshot for daily growth % on dashboard."""

    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # accounts.id for master, slave_accounts.id for slaves
    account_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # "master" or "slave"
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    wallet_balance: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_balance_snapshots_account_type_id", "account_type", "account_id"),
    )
