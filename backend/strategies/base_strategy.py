# base_strategy.py — Abstract base class that all strategies must implement

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class TradeAction:
    """Decision returned by strategy.on_tick for one monitoring cycle."""

    should_exit: bool = False
    should_adjust: bool = False
    exit_reason: str | None = None  # ExitReason enum value
    adjust_leg: str | None = None  # "call" or "put"
    current_pnl: float = 0.0
    trigger_pct_used: float = 0.0  # slab % that applied this tick (for logs)
    triggered_leg: str | None = None  # which leg hit trigger (decision path)
    trigger_pct_hit: float = 0.0  # trigger % when decision fired
    call_trigger_pct: float = 0.0  # per-leg (premium mode may differ)
    put_trigger_pct: float = 0.0


@dataclass
class AdjustmentPlan:
    """Plan to exit one leg and re-enter at a premium-matched strike."""

    exit_leg_type: str  # "call" or "put"
    exit_leg_symbol: str
    new_strike: float
    new_product_id: int
    new_symbol: str
    target_premium: float  # premium we expect to collect on new leg
    other_leg_premium: float  # opposite leg mark used for matching


@dataclass
class OrderResult:
    """Result of a single order placement attempt."""

    success: bool
    order_id: int | None = None
    filled_price: float | None = None
    # Actual Delta commission for this order (inc. GST), sum of fills when partial
    commission: float | None = None
    # Contracts filled this attempt (None = unknown / treat success as full)
    filled_size: int | None = None
    error: str | None = None
    # Mid-price executor metadata (optional — market path leaves None)
    fill_attempt: int | None = None
    fill_type: str | None = None  # mid | best | market
    mid_at_start: float | None = None
    saved_usd: float | None = None
    selected_premium: float | None = None
    drift_pct: float | None = None
    seconds_since_selection: float | None = None


@dataclass
class AdjustmentResult:
    """Outcome of an atomic adjustment (exit + enter)."""

    success: bool
    old_strike: float | None = None
    new_strike: float | None = None
    premium_collected: float | None = None
    error_message: str | None = None
    is_partial: bool = False  # True if exit succeeded but entry failed
    close_basket: bool = False  # True = close entire trade instead of adjusting
    requires_basket_exit: bool = False  # True = no strike available → force exit
    exit_reason: str | None = None
    conversion_mode: bool = False  # True = entered conversion mode
    hedge_order_id: str | None = None
    hedge_product_id: int | None = None
    hedge_entry_price: float | None = None
    hedge_symbol: str | None = None
    # For slave mirror_adjustment (normal adjust only)
    old_product_id: int | None = None
    new_product_id: int | None = None
    new_symbol: str | None = None
    quantity: int | None = None
    # Absolute bracket SL from master fill — slaves must use verbatim
    master_bracket_sl: float | None = None
    master_bracket_sl_limit: float | None = None


class BaseStrategy(ABC):
    """Abstract strategy interface — all strategies must implement these methods."""

    @abstractmethod
    def calculate_pnl(
        self,
        trade: Any,
        call_leg: Any,
        put_leg: Any,
        call_premium: float,
        put_premium: float,
        realized_pnl: float = 0.0,
    ) -> float:
        """Return total P&L = realized (closed legs) + unrealized (open legs)."""

    @abstractmethod
    async def on_tick(
        self,
        trade: Any,
        call_leg: Any,
        put_leg: Any,
        call_premium: float,
        put_premium: float,
        db_session: Any = None,
        realized_pnl: float = 0.0,
        delta_mtm: float | None = None,
        net_mtm: float | None = None,
        slippage_pct: float | None = None,
    ) -> TradeAction:
        """Evaluate exit / adjustment triggers for one monitoring tick.

        Prefer passed ``net_mtm`` (gross − fees − slip) for TP/SL/decision.
        When delta_mtm is provided without net_mtm, gross = realized + delta_mtm
        then net is computed inside the strategy.
        """

    @abstractmethod
    async def find_adjustment_strike(
        self,
        delta_client: Any,
        trade: Any,
        triggered_leg_type: str,
        other_leg_current_premium: float,
        current_strike: float | None = None,
    ) -> AdjustmentPlan:
        """Find a replacement strike by premium-matching the other leg."""

    def get_pnl_percentage(self, pnl: float, total_initial_premium: float) -> float:
        """
        Return PnL as a percentage of total initial premium collected.

        Useful for frontend progress bars (target / loss display).
        Returns 0.0 if total_initial_premium is zero to avoid division errors.
        """
        if total_initial_premium == 0:
            return 0.0
        return (pnl / total_initial_premium) * 100.0
