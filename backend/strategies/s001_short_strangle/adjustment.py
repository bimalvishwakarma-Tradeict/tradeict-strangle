# adjustment.py — Atomic adjustment trigger detection and execution for S001

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow import when run as a script
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.bot_logger import log_and_buffer
from backend.core.time_utils import get_hours_to_expiry, get_utc_now
from backend.core.delta_client import short_leg_realized_pnl
from backend.models import Adjustment, Leg, Trade
from backend.strategies.base_strategy import (
    AdjustmentPlan,
    AdjustmentResult,
    OrderResult,
)
from backend.strategies.s001_short_strangle.config import UNDERLYING_SYMBOLS

logger = logging.getLogger(__name__)


class AdjustmentError(Exception):
    """Raised for adjustment precondition failures (missing legs, etc.)."""


def compute_adjustment_target_premium(
    untouched_leg_offer: float,
    short_baselines: list[float] | tuple[float, ...],
    short_offers: list[float] | tuple[float, ...],
) -> tuple[float, float, float, float]:
    """
    Basket net-loss adjustment target (offer prices only; never mark).

    BASIS RULE: Use Leg.trigger_baseline_premium — NOT Leg.initial_premium,
    and NOT the original trade entry. The baselines already carry the correct
    history:
      - at trade entry, baseline == entry fill
      - after each adjustment, the triggered leg's baseline becomes its NEW
        fill and the untouched leg's baseline is reset to its OFFER at that
        moment
    So combined_baseline is automatically correct for the 1st adjustment and
    every one after it. Realized losses from previous adjustments are NOT
    carried forward.

    Formula:
      combined_baseline = sum(trigger_baseline of ALL open SHORT legs)
      combined_current  = sum(current OFFER of those same legs)
      loss              = max(0, combined_current - combined_baseline)
      target_new_premium = untouched_leg_offer + loss

    Hedge legs (is_long) must be excluded by the caller before passing lists.

    Returns (target_new_premium, loss, combined_baseline, combined_current).
    """
    unt = float(untouched_leg_offer or 0.0)
    bases = [float(b or 0.0) for b in short_baselines]
    offers = [float(o or 0.0) for o in short_offers]
    if len(bases) != len(offers):
        raise ValueError(
            "short_baselines and short_offers must be the same length"
        )
    combined_baseline = sum(bases)
    combined_current = sum(offers)
    loss = max(0.0, combined_current - combined_baseline)
    return unt + loss, loss, combined_baseline, combined_current


def _leg_trigger_baseline(leg: Any) -> float:
    """Prefer trigger_baseline_premium; never invent from scratch."""
    return float(
        getattr(leg, "trigger_baseline_premium", None)
        or getattr(leg, "trigger_premium", None)
        or getattr(leg, "initial_premium", None)
        or 0.0
    )


async def _demo_mark_order_result(
    delta_client: Any,
    symbol: str,
    fallback: float = 0.0,
) -> OrderResult:
    """Synthetic fill for demo/virtual trades (no real Delta order)."""
    try:
        px = float(await delta_client.get_mark_price(str(symbol)))
    except Exception:
        px = 0.0
    if px <= 0:
        px = float(fallback or 0.0)
    return OrderResult(
        success=True,
        order_id=None,
        filled_price=px,
        commission=0.0,
    )


async def _resolve_offer_price(
    delta_client: Any,
    symbol: str,
    *,
    keep_if_missing: float | None = None,
) -> float:
    """
    Best offer (ask) for baseline / strike-match. Never uses mark_price.

    Order: L2/ticker ask via get_short_exit_price → ticker best_ask → mid → keep.
    """
    try:
        offer = float(await delta_client.get_short_exit_price(symbol))
        if offer > 0:
            return offer
    except Exception as exc:
        logger.debug("get_short_exit_price failed for %s: %s", symbol, exc)

    try:
        ticker = await delta_client.get_ticker(symbol)
        quotes = ticker.get("quotes") if isinstance(ticker.get("quotes"), dict) else {}
        ask = float(
            quotes.get("best_ask")
            or ticker.get("best_ask")
            or ticker.get("ask")
            or 0
        )
        bid = float(
            quotes.get("best_bid")
            or ticker.get("best_bid")
            or ticker.get("bid")
            or 0
        )
        if ask > 0:
            return ask
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            logger.warning(
                "Using mid price for %s baseline: %.4f (bid=%.4f ask=%.4f)",
                symbol,
                mid,
                bid,
                ask,
            )
            return mid
        # If ask missing but both legs of book exist under alternate keys
        if bid > 0:
            ask2 = float(quotes.get("ask") or ticker.get("ask") or 0)
            if ask2 > 0:
                mid = (bid + ask2) / 2.0
                logger.warning(
                    "Using mid price for %s baseline: %.4f",
                    symbol,
                    mid,
                )
                return mid
    except Exception as exc:
        logger.error("Cannot get offer price for %s: %s", symbol, exc)

    if keep_if_missing is not None and float(keep_if_missing) > 0:
        return float(keep_if_missing)
    return 0.0


class AdjustmentExecutor:
    """
    Executes atomic leg adjustments for S001.

    Uses calculated premiums / strategy logic for strike selection.
    After completion, the next monitoring cycle pushes fresh Delta MTM
    for frontend display (see MTM P&L DISPLAY RULE).
    """

    async def execute(
        self,
        trade: Any,
        triggered_leg_type: str,
        strategy: Any,
        delta_client: Any,
        order_executor: Any,
        db_session: Any,
    ) -> AdjustmentResult:
        """
        ATOMIC EXECUTION — exit triggered leg, then enter replacement.

        NEVER raises to caller — always returns AdjustmentResult.
        BOT ISOLATION: only operates on is_bot_managed legs in our DB.

        TP/SL locked to initial deployment premium.
        initial_max_profit never changes after trade entry.
        adjustments do NOT affect TP/SL
        (only leg baselines + trade.realized_pnl change).

        LOCK OWNERSHIP: is_adjusting is owned by BotEngine._adjust_trade,
        which sets True before calling execute() and ALWAYS clears False in
        a finally block. execute() must NOT clear the lock early — that would
        reopen the reconcile naked_risk race during post-execute reload.
        """
        # BUG-1: Keep ORM objects usable after commit() — without this,
        # post-commit attribute access raises DetachedInstanceError and can
        # abort mid-adjustment (naked position → emergency integrity close).
        db_session.expire_on_commit = False
        try:
            # Trade arrives from an outside session (position_tracker cache).
            # Merge it into THIS session so commit/refresh never detach it.
            trade_id_lookup = int(getattr(trade, "id", 0) or 0)
            try:
                trade = db_session.merge(trade)
            except Exception:
                trade = (
                    db_session.query(Trade)
                    .filter(Trade.id == trade_id_lookup)
                    .first()
                )
                if trade is None:
                    return AdjustmentResult(
                        success=False,
                        error_message="Trade not found in DB",
                    )

            call_leg, put_leg = self._get_legs(trade, db_session)
            triggered_leg, other_leg = self._resolve_legs(
                triggered_leg_type, call_leg, put_leg
            )

            if not triggered_leg.is_bot_managed:
                msg = (
                    f"Refusing adjustment: {triggered_leg_type} leg "
                    f"id={triggered_leg.id} is not bot-managed"
                )
                logger.error(msg)
                return AdjustmentResult(success=False, error_message=msg)

            # Untouched leg's Best Offer at adjust time — strike match base.
            # NEVER use mark — depressed mark resets baseline too low.
            other_premium = await _resolve_offer_price(
                delta_client,
                str(other_leg.symbol),
                keep_if_missing=None,
            )
            if other_premium <= 0:
                raise AdjustmentError(
                    f"Could not fetch Best Offer for untouched "
                    f"{other_leg.symbol} (no mark fallback)"
                )

            # Keep other_premium as LIVE offer for conversion checks.
            other_leg_current_offer = float(other_premium)

            triggered_baseline = _leg_trigger_baseline(triggered_leg)
            other_old_baseline = _leg_trigger_baseline(other_leg)

            # Triggered leg Best Offer — basket loss component (never mark).
            triggered_current_offer = await _resolve_offer_price(
                delta_client,
                str(triggered_leg.symbol),
                keep_if_missing=None,
            )
            if triggered_current_offer <= 0:
                triggered_current_offer = float(triggered_baseline or 0.0)

            # Basket net loss from trigger baselines of ALL open SHORT legs.
            # Hedge (is_long) excluded. Order: triggered then untouched.
            short_baselines = [triggered_baseline, other_old_baseline]
            short_offers = [
                float(triggered_current_offer),
                float(other_leg_current_offer),
            ]
            (
                target_premium,
                loss_premium,
                combined_baseline,
                combined_current,
            ) = compute_adjustment_target_premium(
                untouched_leg_offer=other_leg_current_offer,
                short_baselines=short_baselines,
                short_offers=short_offers,
            )
            logger.info(
                "[ADJUSTMENT_TARGET] trade_id=%s leg=%s "
                "combined_baseline=%.4f combined_current=%.4f "
                "loss=%.4f untouched_offer=%.4f target_new_premium=%.4f",
                trade.id,
                triggered_leg_type,
                combined_baseline,
                combined_current,
                loss_premium,
                other_leg_current_offer,
                target_premium,
            )
            log_and_buffer(
                "ADJUSTMENT_TARGET",
                trade.id,
                {
                    "leg": triggered_leg_type,
                    "combined_baseline": round(combined_baseline, 4),
                    "combined_current": round(combined_current, 4),
                    "loss": round(loss_premium, 4),
                    "untouched_offer": round(other_leg_current_offer, 4),
                    "target_new_premium": round(target_premium, 4),
                },
            )

            logger.info(
                "[ADJUSTMENT_START] Trade %s "
                "triggered_leg=%s strike=%s product_id=%s "
                "trigger_baseline=%s other_leg_offer=%s "
                "other_leg_old_baseline=%s target_new_premium=%s "
                "loss=%s combined_baseline=%s combined_current=%s",
                trade.id,
                triggered_leg_type,
                triggered_leg.strike,
                triggered_leg.product_id,
                triggered_baseline,
                other_leg_current_offer,
                other_old_baseline,
                target_premium,
                loss_premium,
                combined_baseline,
                combined_current,
            )
            log_and_buffer(
                "ADJUSTMENT_START",
                trade.id,
                {
                    "triggered_leg": triggered_leg_type,
                    "strike": float(triggered_leg.strike),
                    "other_leg_offer": round(other_leg_current_offer, 4),
                    "loss": round(loss_premium, 4),
                    "combined_baseline": round(combined_baseline, 4),
                    "combined_current": round(combined_current, 4),
                    "target_new_premium": round(target_premium, 4),
                },
            )

            # Load AutoTradeSettings early (conversion thresholds)
            try:
                from backend.database import get_or_create_auto_settings, SessionLocal
                with SessionLocal() as _sdb:
                    _cfg = get_or_create_auto_settings(_sdb)
                    _conv_feature = bool(
                        getattr(_cfg, "adj_low_premium_exit_enabled", False)
                    )
                    _conv_min = float(
                        getattr(_cfg, "adj_low_premium_min_usd", 150.0) or 150.0
                    )
                    _conversion_mode_enabled = bool(
                        getattr(_cfg, "conversion_mode_enabled", True)
                    )
            except Exception:
                _conv_feature = False
                _conv_min = 150.0
                _conversion_mode_enabled = True

            # Basket formula is the single target rule — no premium_cover_loss
            # override (that path previously replaced target with entry-based
            # loss and mis-logged other_leg_offer as the full target).
            try:
                plan = await strategy.find_adjustment_strike(
                    delta_client,
                    trade,
                    triggered_leg_type,
                    float(target_premium),
                    current_strike=float(triggered_leg.strike),
                    untouched_leg_offer=float(other_leg_current_offer),
                )
            except Exception as exc:
                msg = str(exc)
                # ADJUSTMENT_ABORT / reason=no_valid_strike → close basket.
                # find_adjustment_strike only raises ADJUSTMENT_ABORT for
                # no_valid_strike (chain exhausted or wrong-direction guard).
                if "ADJUSTMENT_ABORT" in msg:
                    spot_px = 0.0
                    try:
                        und_key = str(
                            getattr(trade, "underlying", None) or "BTC"
                        ).upper()
                        price_sym = UNDERLYING_SYMBOLS.get(und_key, und_key)
                        spot_px = float(
                            await delta_client.get_underlying_price(price_sym)
                        )
                    except Exception:
                        spot_px = 0.0
                    old_strike_abort = float(triggered_leg.strike)
                    chain_details = {
                        "trade": int(trade.id),
                        "leg": triggered_leg_type,
                        "old_strike": old_strike_abort,
                        "target_premium": round(float(target_premium), 4),
                        "spot": round(spot_px, 2),
                        "summary": (
                            f"[CHAIN_EXHAUSTED] trade={int(trade.id)} | "
                            f"leg={triggered_leg_type} | "
                            f"old_strike={old_strike_abort} | "
                            f"target_premium="
                            f"{round(float(target_premium), 4)} | "
                            f"spot={round(spot_px, 2)}"
                        ),
                    }
                    try:
                        log_and_buffer(
                            "CHAIN_EXHAUSTED",
                            int(trade.id),
                            chain_details,
                        )
                    except Exception:
                        pass
                    logger.warning(
                        "[CHAIN_EXHAUSTED] trade=%s | leg=%s | "
                        "old_strike=%s | target_premium=%s | spot=%s",
                        int(trade.id),
                        triggered_leg_type,
                        old_strike_abort,
                        round(float(target_premium), 4),
                        round(spot_px, 2),
                    )
                    return AdjustmentResult(
                        success=False,
                        requires_basket_exit=True,
                        close_basket=True,
                        exit_reason="CHAIN_EXHAUSTED",
                        old_strike=old_strike_abort,
                        error_message=msg[:500],
                    )
                # No alternate strike on chain → EXIT basket (do not HOLD)
                if "SAME_STRIKE_HOLD" in msg and (
                    "no other" in msg.lower() or "no alternate" in msg.lower()
                ):
                    logger.info(
                        "[NO_STRIKE_AVAILABLE] Trade %s leg=%s — no farther OTM "
                        "strike on chain. EXITING BASKET. triggered_strike=%s",
                        trade.id,
                        triggered_leg_type,
                        triggered_leg.strike,
                    )
                    log_and_buffer(
                        "NO_STRIKE_AVAILABLE",
                        int(trade.id),
                        {
                            "triggered_leg": triggered_leg_type,
                            "triggered_strike": float(triggered_leg.strike),
                            "reason": (
                                "no farther OTM strike exists on "
                                "Delta Exchange chain"
                            ),
                            "action": "EXIT_BASKET",
                        },
                    )
                    return AdjustmentResult(
                        success=False,
                        requires_basket_exit=True,
                        close_basket=True,
                        exit_reason="NO_STRIKE_AVAILABLE",
                        old_strike=float(triggered_leg.strike),
                        error_message=(
                            f"NO_STRIKE_AVAILABLE: no farther OTM "
                            f"{triggered_leg_type} strike on Delta chain "
                            f"— exiting basket"
                        ),
                    )
                raise

            plan.exit_leg_symbol = triggered_leg.symbol

            # Guard: never adjust into the same strike / product
            if (
                abs(float(plan.new_strike) - float(triggered_leg.strike)) < 0.01
                or int(plan.new_product_id) == int(triggered_leg.product_id)
            ):
                logger.info(
                    "[NO_STRIKE_AVAILABLE] Trade %s leg=%s — replacement equals "
                    "current strike. EXITING BASKET. triggered_strike=%s",
                    trade.id,
                    triggered_leg_type,
                    triggered_leg.strike,
                )
                log_and_buffer(
                    "NO_STRIKE_AVAILABLE",
                    int(trade.id),
                    {
                        "triggered_leg": triggered_leg_type,
                        "triggered_strike": float(triggered_leg.strike),
                        "reason": (
                            "replacement strike equals current — "
                            "no usable farther OTM strike"
                        ),
                        "action": "EXIT_BASKET",
                    },
                )
                return AdjustmentResult(
                    success=False,
                    requires_basket_exit=True,
                    close_basket=True,
                    exit_reason="NO_STRIKE_AVAILABLE",
                    old_strike=float(triggered_leg.strike),
                    new_strike=float(plan.new_strike),
                    error_message=(
                        f"NO_STRIKE_AVAILABLE: no farther OTM "
                        f"{triggered_leg_type} strike on Delta chain "
                        f"— exiting basket"
                    ),
                )

            # --- CONVERSION MODE CHECK ---
            # If replacement premium < configured minimum, enter conversion mode
            # instead of normal adjustment or closing the basket.
            # Settings already loaded above (_conv_feature / _conv_min /
            # _conversion_mode_enabled).

            if _conv_feature and other_premium < _conv_min:
                if not _conversion_mode_enabled:
                    logger.warning(
                        "[CONVERSION_DISABLED_EXIT] Trade %s: other_premium=%.2f "
                        "< min=%.2f — conversion_mode_enabled=False, exiting basket",
                        trade.id,
                        other_premium,
                        _conv_min,
                    )
                    log_and_buffer(
                        "CONVERSION_DISABLED_EXIT",
                        int(trade.id),
                        {
                            "reason": (
                                "conversion_mode_enabled=False, "
                                "exiting basket instead"
                            ),
                            "other_leg_offer": round(float(other_premium), 2),
                            "conversion_min": round(float(_conv_min), 2),
                        },
                    )
                    return AdjustmentResult(
                        success=False,
                        close_basket=True,
                        old_strike=float(triggered_leg.strike),
                        error_message=(
                            "CONVERSION_DISABLED_EXIT: conversion mode off, "
                            f"other leg offer {other_premium:.2f} < min {_conv_min:.2f}"
                        ),
                    )
                logger.warning(
                    "[CONVERSION_MODE] Trade %s: other_premium=%.2f < min=%.2f "
                    "— entering conversion mode instead of adjusting",
                    trade.id,
                    other_premium,
                    _conv_min,
                )
                # Find hedge leg: one strike INSIDE triggered leg (toward ATM)
                # PUT: toward ATM = higher strike (+$200)
                # CALL: toward ATM = lower strike (-$200)
                try:
                    _STRIKE_INCREMENT = 200.0
                    triggered_strike = float(triggered_leg.strike)
                    leg_lower = str(triggered_leg_type).lower()
                    if leg_lower == "put":
                        hedge_target_strike = (
                            triggered_strike + _STRIKE_INCREMENT
                        )
                    else:
                        hedge_target_strike = (
                            triggered_strike - _STRIKE_INCREMENT
                        )

                    expiry_date = trade.expiry_date
                    if hasattr(expiry_date, "isoformat"):
                        expiry_str = expiry_date.isoformat()
                    else:
                        expiry_str = str(expiry_date)

                    underlying_key = str(trade.underlying).upper()
                    underlying_symbol = UNDERLYING_SYMBOLS.get(
                        underlying_key, underlying_key
                    )

                    chain = await delta_client.get_option_chain(
                        underlying=underlying_symbol,
                        expiry_date=expiry_str,
                    )
                    hedge_chain_row = None
                    for row in chain:
                        if (
                            abs(
                                float(row.get("strike", 0))
                                - hedge_target_strike
                            )
                            < 0.01
                        ):
                            hedge_chain_row = row
                            break

                    if hedge_chain_row is None:
                        raise ValueError(
                            f"Hedge strike {hedge_target_strike} not found "
                            f"on chain (triggered={triggered_strike})"
                        )

                    hedge_symbol_key = f"{leg_lower}_symbol"
                    hedge_pid_key = f"{leg_lower}_product_id"
                    hedge_pid = int(hedge_chain_row.get(hedge_pid_key) or 0)
                    hedge_sym = str(
                        hedge_chain_row.get(hedge_symbol_key) or ""
                    )
                    if hedge_pid <= 0 or not hedge_sym:
                        raise ValueError(
                            f"Hedge strike {hedge_target_strike} missing "
                            f"product_id/symbol on chain"
                        )

                    hedge_plan = AdjustmentPlan(
                        exit_leg_type=leg_lower,
                        exit_leg_symbol="",
                        new_strike=hedge_target_strike,
                        new_product_id=hedge_pid,
                        new_symbol=hedge_sym,
                        target_premium=float(
                            hedge_chain_row.get(f"{leg_lower}_mark_price")
                            or hedge_chain_row.get(f"{leg_lower}_ask")
                            or 0
                        ),
                        other_leg_premium=float(
                            triggered_leg.trigger_baseline_premium
                            or triggered_leg.initial_premium
                            or 0
                        ),
                    )
                    logger.info(
                        "[CONVERSION_MODE] Hedge strike: %s → %s "
                        "(toward ATM %s) symbol=%s product_id=%s",
                        triggered_strike,
                        hedge_target_strike,
                        "+200" if leg_lower == "put" else "-200",
                        hedge_sym,
                        hedge_pid,
                    )
                except Exception as exc:
                    logger.info(
                        "[NO_HEDGE_STRIKE_AVAILABLE] Trade %s — no hedge strike "
                        "on chain. EXITING BASKET. triggered_strike=%s err=%s",
                        trade.id,
                        triggered_leg.strike,
                        exc,
                    )
                    log_and_buffer(
                        "NO_HEDGE_STRIKE_AVAILABLE",
                        int(trade.id),
                        {
                            "triggered_leg": triggered_leg_type,
                            "triggered_strike": float(triggered_leg.strike),
                            "reason": f"no hedge strike on chain — {exc}",
                            "action": "EXIT_BASKET",
                        },
                    )
                    return AdjustmentResult(
                        success=False,
                        requires_basket_exit=True,
                        close_basket=True,
                        exit_reason="NO_HEDGE_STRIKE_AVAILABLE",
                        old_strike=float(triggered_leg.strike),
                        error_message=(
                            "NO_HEDGE_STRIKE_AVAILABLE: no hedge strike on "
                            f"Delta chain — exiting basket ({exc})"
                        ),
                    )

                # Place BUY order for hedge leg
                try:
                    if bool(getattr(trade, "is_demo", False)):
                        logger.info(
                            "[DEMO] Virtual conversion hedge buy — no real order"
                        )
                        hedge_result = await _demo_mark_order_result(
                            delta_client,
                            str(hedge_plan.new_symbol),
                            float(hedge_plan.target_premium or 0),
                        )
                    else:
                        hedge_result = await order_executor.buy_option(
                            product_id=int(hedge_plan.new_product_id),
                            quantity=int(triggered_leg.quantity),
                            delta_client=delta_client,
                            symbol_for_fallback=str(hedge_plan.new_symbol),
                        )
                except Exception as exc:
                    logger.error(
                        "[CONVERSION_MODE] Hedge buy failed for trade %s: %s",
                        trade.id,
                        exc,
                    )
                    return AdjustmentResult(
                        success=False,
                        old_strike=float(triggered_leg.strike),
                        error_message=(
                            f"CONVERSION_MODE_FAILED: hedge buy error — {exc}"
                        ),
                    )

                if not hedge_result.success:
                    logger.error(
                        "[CONVERSION_MODE] Hedge buy order failed trade %s: %s",
                        trade.id,
                        hedge_result.error,
                    )
                    return AdjustmentResult(
                        success=False,
                        old_strike=float(triggered_leg.strike),
                        error_message=(
                            "CONVERSION_MODE_FAILED: hedge buy order rejected"
                        ),
                    )

                hedge_fill = float(
                    hedge_result.filled_price or hedge_plan.target_premium or 0.0
                )
                logger.info(
                    "[CONVERSION_MODE] Hedge bought: symbol=%s fill=%.2f",
                    hedge_plan.new_symbol,
                    hedge_fill,
                )

                # Replace the other (untouched) leg with better premium
                # Target = hedge fill / 2 (not triggered current / 2)
                triggered_current_premium = float(
                    await _resolve_offer_price(
                        delta_client, str(triggered_leg.symbol)
                    )
                    or triggered_leg.trigger_baseline_premium
                    or triggered_leg.initial_premium
                    or 0.0
                )
                old_method_target = triggered_current_premium / 2.0
                new_other_target = float(hedge_fill) / 2.0
                logger.info(
                    "CONVERSION_OTHER_TARGET | hedge_fill=%.1f | target=%.1f | "
                    "old_method_would_have_given=%.1f",
                    hedge_fill,
                    new_other_target,
                    old_method_target,
                )
                log_and_buffer(
                    "CONVERSION_OTHER_TARGET",
                    int(trade.id),
                    {
                        "hedge_fill": round(float(hedge_fill), 4),
                        "target": round(new_other_target, 4),
                        "old_method_would_have_given": round(old_method_target, 4),
                    },
                )
                logger.info(
                    "[CONVERSION_MODE] Replacing other leg %s: "
                    "current_premium=%.2f target_new_premium=%.2f",
                    other_leg.leg_type,
                    other_premium,
                    new_other_target,
                )

                # Close existing other leg
                if bool(getattr(trade, "is_demo", False)):
                    logger.info(
                        "[DEMO] Virtual conversion other-leg close — no real order"
                    )
                    other_close_result = await _demo_mark_order_result(
                        delta_client,
                        str(other_leg.symbol),
                        float(other_premium or 0),
                    )
                else:
                    other_close_result = await order_executor.close_leg(
                        other_leg, delta_client
                    )
                if not other_close_result.success:
                    # Hedge already placed — critical partial state
                    logger.critical(
                        "[CONVERSION_MODE] PARTIAL: hedge placed but other leg "
                        "close failed for trade %s",
                        trade.id,
                    )
                    return AdjustmentResult(
                        success=False,
                        conversion_mode=True,
                        hedge_order_id=str(hedge_result.order_id or ""),
                        hedge_product_id=int(hedge_plan.new_product_id),
                        hedge_entry_price=hedge_fill,
                        hedge_symbol=str(hedge_plan.new_symbol),
                        error_message=(
                            "CONVERSION_MODE_PARTIAL: hedge ok but other close failed"
                        ),
                    )

                # Find new other leg at target_premium
                try:
                    new_other_plan = await strategy.find_adjustment_strike(
                        delta_client,
                        trade,
                        other_leg.leg_type,
                        new_other_target,
                        current_strike=float(other_leg.strike),
                    )
                except Exception as exc:
                    logger.critical(
                        "[NO_OTHER_STRIKE_IN_CONVERSION] Trade %s — no new other "
                        "strike. EXITING BASKET. err=%s",
                        trade.id,
                        exc,
                    )
                    log_and_buffer(
                        "NO_OTHER_STRIKE_IN_CONVERSION",
                        int(trade.id),
                        {
                            "triggered_leg": triggered_leg_type,
                            "other_leg": str(other_leg.leg_type),
                            "reason": f"new other strike not found — {exc}",
                            "action": "EXIT_BASKET",
                            "hedge_symbol": str(hedge_plan.new_symbol),
                        },
                    )
                    return AdjustmentResult(
                        success=False,
                        requires_basket_exit=True,
                        close_basket=True,
                        exit_reason="NO_OTHER_STRIKE_IN_CONVERSION",
                        conversion_mode=True,
                        hedge_order_id=str(hedge_result.order_id or ""),
                        hedge_product_id=int(hedge_plan.new_product_id),
                        hedge_entry_price=hedge_fill,
                        hedge_symbol=str(hedge_plan.new_symbol),
                        error_message=(
                            "NO_OTHER_STRIKE_IN_CONVERSION: new other strike "
                            f"not found — exiting basket ({exc})"
                        ),
                    )

                # Short new other leg
                if bool(getattr(trade, "is_demo", False)):
                    logger.info(
                        "[DEMO] Virtual conversion new other short — no real order"
                    )
                    new_other_result = await _demo_mark_order_result(
                        delta_client,
                        str(new_other_plan.new_symbol),
                        float(new_other_target or 0),
                    )
                else:
                    # Inline bracket from target (mark proxy); amend to fill after.
                    from backend.core.delta_sl import compute_bracket_sl

                    conv_uni_sl = float(
                        getattr(trade, "universal_sl_pct", None) or 200.0
                    )
                    conv_prov_sl, conv_prov_limit = compute_bracket_sl(
                        float(new_other_target or 0),
                        conv_uni_sl,
                        master_mark=float(new_other_target or 0),
                        leg=str(other_leg.leg_type),
                        trade_id=int(trade.id),
                    )
                    new_other_result = await order_executor.sell_option(
                        product_id=int(new_other_plan.new_product_id),
                        quantity=int(other_leg.quantity),
                        delta_client=delta_client,
                        symbol_for_fallback=str(new_other_plan.new_symbol),
                        bracket_sl_price=(
                            conv_prov_sl if conv_prov_sl > 0 else None
                        ),
                        bracket_sl_limit=(
                            conv_prov_limit if conv_prov_sl > 0 else None
                        ),
                    )
                if not new_other_result.success:
                    logger.critical(
                        "[CONVERSION_MODE] PARTIAL: other closed but new short "
                        "failed for trade %s",
                        trade.id,
                    )
                    return AdjustmentResult(
                        success=False,
                        conversion_mode=True,
                        hedge_order_id=str(hedge_result.order_id or ""),
                        hedge_product_id=int(hedge_plan.new_product_id),
                        hedge_entry_price=hedge_fill,
                        hedge_symbol=str(hedge_plan.new_symbol),
                        error_message=(
                            "CONVERSION_MODE_PARTIAL: new short leg failed"
                        ),
                    )

                new_other_fill = float(
                    new_other_result.filled_price
                    or new_other_plan.target_premium
                    or 0.0
                )

                from backend.core.delta_sl import (
                    compute_bracket_sl,
                    finalize_bracket_sl_after_fill,
                )

                conv_uni_sl = float(
                    getattr(trade, "universal_sl_pct", None) or 200.0
                )
                if bool(getattr(trade, "is_demo", False)):
                    conv_prov_sl, conv_prov_limit = compute_bracket_sl(
                        float(new_other_target or 0),
                        conv_uni_sl,
                        master_mark=float(new_other_target or 0),
                        leg=str(other_leg.leg_type),
                        trade_id=int(trade.id),
                    )
                conv_sl, conv_sl_limit = await finalize_bracket_sl_after_fill(
                    None if bool(getattr(trade, "is_demo", False)) else delta_client,
                    entry_order_id=(
                        None
                        if bool(getattr(trade, "is_demo", False))
                        else new_other_result.order_id
                    ),
                    product_id=int(new_other_plan.new_product_id),
                    mark_price=float(new_other_target or 0),
                    fill_price=new_other_fill,
                    universal_sl_pct=conv_uni_sl,
                    provisional_stop=float(conv_prov_sl or 0),
                    provisional_limit=float(conv_prov_limit or 0),
                    leg=str(other_leg.leg_type),
                    trade_id=int(trade.id),
                )

                now_utc = get_utc_now()

                # Close old other leg in DB
                other_leg.status = "closed"
                other_leg.exit_time = now_utc
                other_leg.exit_premium = float(
                    other_close_result.filled_price or other_premium
                )
                other_leg.exit_order_id = str(other_close_result.order_id or "")
                other_leg.realized_pnl = short_leg_realized_pnl(
                    float(other_leg.initial_premium),
                    float(other_leg.exit_premium),
                    int(other_leg.quantity),
                )

                # Create new other leg in DB
                from backend.core.fees import (
                    compute_entry_spread_usd,
                    reset_entry_spread_for_sl,
                )

                new_other_sent = float(
                    getattr(new_other_plan, "target_premium", 0) or new_other_fill
                )
                new_other_spread = compute_entry_spread_usd(
                    sent_price=new_other_sent,
                    fill_price=new_other_fill,
                    quantity=int(other_leg.quantity),
                    is_long=False,
                )
                new_other_leg = Leg(
                    trade_id=int(trade.id),
                    leg_type=str(other_leg.leg_type),
                    strike=float(new_other_plan.new_strike),
                    symbol=str(new_other_plan.new_symbol),
                    product_id=int(new_other_plan.new_product_id),
                    initial_premium=new_other_fill,
                    trigger_baseline_premium=new_other_fill,
                    trigger_premium=new_other_fill,
                    quantity=int(other_leg.quantity),
                    entry_time=now_utc,
                    status="open",
                    is_bot_managed=True,
                    is_long=False,
                    delta_order_id=str(new_other_result.order_id or ""),
                    order_sent_price=new_other_sent,
                    entry_spread_usd=new_other_spread,
                    sl_trigger_price=float(conv_sl) if conv_sl > 0 else None,
                    delta_sl_order_id=None,  # bracket — no separate stop id
                )
                db_session.add(new_other_leg)

                # Hedge is a first-class basket leg (long) — store in Leg table
                hedge_leg_type = (
                    "hedge_put"
                    if str(triggered_leg_type).lower() == "put"
                    else "hedge_call"
                )
                hedge_sent = float(
                    getattr(hedge_plan, "target_premium", 0) or hedge_fill
                )
                hedge_spread = compute_entry_spread_usd(
                    sent_price=hedge_sent,
                    fill_price=hedge_fill,
                    quantity=int(triggered_leg.quantity),
                    is_long=True,
                )
                hedge_leg_row = Leg(
                    trade_id=int(trade.id),
                    leg_type=hedge_leg_type,
                    strike=float(hedge_plan.new_strike),
                    symbol=str(hedge_plan.new_symbol),
                    product_id=int(hedge_plan.new_product_id),
                    initial_premium=hedge_fill,
                    trigger_baseline_premium=hedge_fill,
                    trigger_premium=hedge_fill,
                    quantity=int(triggered_leg.quantity),
                    entry_time=now_utc,
                    status="open",
                    is_bot_managed=True,
                    is_long=True,
                    delta_order_id=str(hedge_result.order_id or ""),
                    entry_fee_usd=(
                        abs(float(hedge_result.commission))
                        if getattr(hedge_result, "commission", None) is not None
                        else None
                    ),
                    order_sent_price=hedge_sent,
                    entry_spread_usd=hedge_spread,
                )
                db_session.add(hedge_leg_row)
                # Newest conversion entry event = legs opened in this step only
                reset_entry_spread_for_sl(
                    trade,
                    abs(float(new_other_spread or 0.0))
                    + abs(float(hedge_spread or 0.0)),
                    reason="conversion",
                    leg=f"{other_leg.leg_type}+hedge",
                )

                # Conversion fields kept for backward compat + quick lookup
                trade.in_conversion_mode = True
                trade.conversion_hedge_product_id = int(hedge_plan.new_product_id)
                trade.conversion_hedge_order_id = str(hedge_result.order_id or "")
                trade.conversion_hedge_entry_price = hedge_fill
                trade.conversion_hedge_symbol = str(hedge_plan.new_symbol)
                trade.conversion_triggered_leg = triggered_leg_type

                db_session.commit()
                db_session.refresh(trade)
                db_session.refresh(triggered_leg)
                db_session.refresh(other_leg)
                try:
                    db_session.refresh(hedge_leg_row)
                    db_session.refresh(new_other_leg)
                except Exception:
                    pass

                log_and_buffer(
                    "CONVERSION_MODE_ENTERED",
                    int(trade.id),
                    {
                        "triggered_leg": triggered_leg_type,
                        "hedge_symbol": hedge_plan.new_symbol,
                        "hedge_fill": round(hedge_fill, 2),
                        "hedge_leg_id": int(hedge_leg_row.id),
                        "hedge_leg_type": hedge_leg_type,
                        "old_other_leg": other_leg.leg_type,
                        "old_other_premium": round(other_premium, 2),
                        "new_other_symbol": new_other_plan.new_symbol,
                        "new_other_fill": round(new_other_fill, 2),
                        "target_new_other_premium": round(new_other_target, 2),
                    },
                )

                # AUDIT-7: mirror hedge buy + other-leg replace to slaves
                try:
                    import backend.engine.mirror_engine as mirror_module

                    if mirror_module.mirror_engine is not None:
                        asyncio.create_task(
                            mirror_module.mirror_engine.mirror_conversion(
                                master_trade_id=int(trade.id),
                                hedge_product_id=int(hedge_plan.new_product_id),
                                hedge_symbol=str(hedge_plan.new_symbol),
                                old_other_product_id=int(other_leg.product_id),
                                new_other_product_id=int(
                                    new_other_plan.new_product_id
                                ),
                                new_other_symbol=str(new_other_plan.new_symbol),
                                new_other_strike=float(new_other_plan.new_strike),
                                other_leg_type=str(other_leg.leg_type),
                                master_qty=int(triggered_leg.quantity),
                                master_bracket_sl=(
                                    float(conv_sl) if conv_sl > 0 else None
                                ),
                            )
                        )
                except Exception as exc:
                    logger.warning(
                        "Mirror conversion queue failed (non-fatal): %s", exc
                    )

                return AdjustmentResult(
                    success=True,
                    conversion_mode=True,
                    old_strike=float(triggered_leg.strike),
                    new_strike=float(new_other_plan.new_strike),
                    premium_collected=new_other_fill,
                    hedge_order_id=str(hedge_result.order_id or ""),
                    hedge_product_id=int(hedge_plan.new_product_id),
                    hedge_entry_price=hedge_fill,
                    hedge_symbol=str(hedge_plan.new_symbol),
                )

            # --- AUDIT: verify triggered leg still on Delta before close ---
            trade_is_demo = bool(getattr(trade, "is_demo", False))
            if trade_is_demo:
                logger.info(
                    "[DEMO] Virtual adjustment — skipping Delta pre-close audit"
                )
            else:
                logger.info(
                    "[AUDIT] Verifying triggered leg on Delta before close..."
                )
                try:
                    leg_exists = await delta_client.verify_position_exists(
                        int(triggered_leg.product_id)
                    )
                except Exception as exc:
                    logger.warning(
                        "[AUDIT] verify_position_exists failed before close: %s",
                        exc,
                    )
                    leg_exists = True  # proceed cautiously if check unavailable
                log_and_buffer(
                    "ADJUSTMENT_DELTA_VERIFY",
                    int(trade.id),
                    {
                        "stage": "pre_close",
                        "leg": triggered_leg_type,
                        "product_id": int(triggered_leg.product_id),
                        "exists": bool(leg_exists),
                    },
                )
                if not leg_exists:
                    logger.warning(
                        "[AUDIT] Triggered leg %s NOT found on Delta. "
                        "May have been closed already. Skipping adjustment.",
                        triggered_leg.symbol,
                    )
                    return AdjustmentResult(
                        success=False,
                        is_partial=False,
                        error_message=(
                            "Triggered leg not found on Delta — already closed?"
                        ),
                    )
                logger.info("[AUDIT] Triggered leg confirmed on Delta")

            # Step 3→4: Close triggered leg
            # If this leg was protected by a legacy *separate* SL order
            # (delta_sl_order_id exists), cancel it before/around the close so
            # we don't leave orphan stop orders behind.
            legacy_sl_oid = getattr(triggered_leg, "delta_sl_order_id", None)
            if legacy_sl_oid and not trade_is_demo:
                try:
                    await delta_client.cancel_order(int(legacy_sl_oid))
                    triggered_leg.delta_sl_order_id = None
                except Exception as exc:
                    logger.warning(
                        "Could not cancel legacy SL before adjustment "
                        "trade=%s leg=%s sl_order_id=%s: %s",
                        trade.id,
                        triggered_leg_type,
                        legacy_sl_oid,
                        exc,
                    )

            if trade_is_demo:
                logger.info(
                    "[DEMO] Virtual adjustment exit+entry — no real orders"
                )
                exit_result = await _demo_mark_order_result(
                    delta_client,
                    str(triggered_leg.symbol),
                    float(
                        triggered_leg.trigger_baseline_premium
                        or triggered_leg.initial_premium
                        or 0
                    ),
                )
            else:
                exit_result = await order_executor.close_leg(
                    triggered_leg, delta_client
                )
            if not exit_result.success:
                msg = (
                    f"Failed to exit {triggered_leg_type} leg: "
                    f"{exit_result.error or 'unknown error'}"
                )
                logger.error("Adjustment abort trade=%s — %s", trade.id, msg)
                return AdjustmentResult(success=False, error_message=msg)

            # AUDIT: verify close registered on Delta
            if not trade_is_demo:
                await asyncio.sleep(2)
                try:
                    still_exists = await delta_client.verify_position_exists(
                        int(triggered_leg.product_id)
                    )
                except Exception as exc:
                    logger.warning(
                        "[AUDIT] post-close verify failed: %s", exc
                    )
                    still_exists = False
                log_and_buffer(
                    "ADJUSTMENT_DELTA_VERIFY",
                    int(trade.id),
                    {
                        "stage": "post_close",
                        "leg": triggered_leg_type,
                        "product_id": int(triggered_leg.product_id),
                        "still_exists": bool(still_exists),
                    },
                )
                if still_exists:
                    logger.warning(
                        "[AUDIT] Triggered leg %s still visible on Delta after "
                        "close order. Order may be pending. Proceeding anyway.",
                        triggered_leg.symbol,
                    )
                else:
                    logger.info("[AUDIT] Triggered leg closed on Delta")

            # Step 5: Enter new leg WITH inline bracket (mark/offer provisional).
            # Chicken-and-egg: attach expected × uni_sl now; after fill try amend
            # to fill-derived. If amend fails, provisional stays canonical.
            from backend.core.delta_sl import (
                compute_bracket_sl,
                finalize_bracket_sl_after_fill,
            )

            uni_sl = float(getattr(trade, "universal_sl_pct", None) or 200.0)
            try:
                expected_new_entry = float(
                    await delta_client.get_short_exit_price(plan.new_symbol)
                )
            except Exception:
                expected_new_entry = float(other_premium)
            if expected_new_entry <= 0:
                expected_new_entry = float(other_premium)
            adj_prov_sl, adj_prov_limit = compute_bracket_sl(
                expected_new_entry,
                uni_sl,
                master_mark=expected_new_entry,
                leg=str(triggered_leg_type),
                trade_id=int(trade.id),
            )
            if trade_is_demo:
                entry_result = await _demo_mark_order_result(
                    delta_client,
                    str(plan.new_symbol),
                    float(expected_new_entry or other_premium or 0),
                )
            else:
                entry_result = await order_executor.sell_option(
                    product_id=int(plan.new_product_id),
                    quantity=int(triggered_leg.quantity),
                    delta_client=delta_client,
                    symbol_for_fallback=str(plan.new_symbol),
                    bracket_sl_price=adj_prov_sl if adj_prov_sl > 0 else None,
                    bracket_sl_limit=adj_prov_limit if adj_prov_sl > 0 else None,
                )
            if not entry_result.success:
                other_leg_type = (
                    "put" if triggered_leg_type.lower() == "call" else "call"
                )
                self._log_partial_error(trade, triggered_leg_type, exit_result)
                self._mark_leg_closed_partial(
                    triggered_leg, exit_result, db_session
                )
                logger.critical(
                    "PARTIAL ADJUSTMENT: %s closed at %s but new entry FAILED. "
                    "Trade %s now ONE-LEGGED. Other leg (%s) still open. "
                    "Manual intervention required!",
                    triggered_leg_type,
                    exit_result.filled_price,
                    trade.id,
                    other_leg_type,
                )
                return AdjustmentResult(
                    success=False,
                    is_partial=True,
                    old_strike=float(triggered_leg.strike),
                    error_message=(
                        f"PARTIAL: {triggered_leg_type} closed at "
                        f"{exit_result.filled_price}, new entry failed. "
                        "One-legged position remains."
                    ),
                )

            # AUDIT: verify new leg on Delta
            await asyncio.sleep(1)
            try:
                new_exists = await delta_client.verify_position_exists(
                    int(plan.new_product_id)
                )
            except Exception as exc:
                logger.warning("[AUDIT] new-leg verify failed: %s", exc)
                new_exists = False
            log_and_buffer(
                "ADJUSTMENT_DELTA_VERIFY",
                int(trade.id),
                {
                    "stage": "post_entry",
                    "symbol": str(plan.new_symbol),
                    "product_id": int(plan.new_product_id),
                    "exists": bool(new_exists),
                },
            )
            if not new_exists:
                logger.warning(
                    "[AUDIT] New leg %s not yet visible on Delta. "
                    "Order may be settling.",
                    plan.new_symbol,
                )
            else:
                logger.info("[AUDIT] New leg confirmed on Delta")

            # Re-attach legs/trade after potential cross-session commits
            # (e.g. reconcile emergency close on another SessionLocal).
            try:
                db_session.refresh(triggered_leg)
            except Exception:
                triggered_leg = db_session.merge(triggered_leg)
                db_session.refresh(triggered_leg)

            try:
                db_session.refresh(other_leg)
            except Exception:
                other_leg = db_session.merge(other_leg)
                db_session.refresh(other_leg)

            try:
                db_session.refresh(trade)
            except Exception:
                trade = db_session.merge(trade)
                db_session.refresh(trade)

            new_entry_premium = float(entry_result.filled_price or 0.0)

            bracket_sl_price, bracket_sl_limit = await finalize_bracket_sl_after_fill(
                None if trade_is_demo else delta_client,
                entry_order_id=(
                    None if trade_is_demo else entry_result.order_id
                ),
                product_id=int(plan.new_product_id),
                mark_price=expected_new_entry,
                fill_price=(
                    new_entry_premium if new_entry_premium > 0 else expected_new_entry
                ),
                universal_sl_pct=uni_sl,
                provisional_stop=adj_prov_sl,
                provisional_limit=adj_prov_limit,
                leg=str(triggered_leg_type),
                trade_id=int(trade.id),
            )
            display_sl = bracket_sl_price

            # Steps 6–8: Update DB on full success
            now_utc = get_utc_now()
            old_strike = float(triggered_leg.strike)
            old_entry_fill = float(triggered_leg.initial_premium)
            old_exit_premium = float(exit_result.filled_price or 0.0)

            triggered_leg.exit_premium = old_exit_premium
            triggered_leg.exit_time = now_utc
            triggered_leg.status = "closed"
            if exit_result.order_id is not None:
                triggered_leg.exit_order_id = str(exit_result.order_id)
            if exit_result.commission is not None:
                triggered_leg.exit_fee_usd = abs(float(exit_result.commission))

            # New leg: entry fill stays forever; baseline starts at fill
            from backend.core.fees import (
                compute_entry_spread_usd,
                reset_entry_spread_for_sl,
            )

            new_leg_spread = compute_entry_spread_usd(
                sent_price=float(expected_new_entry),
                fill_price=new_entry_premium,
                quantity=int(triggered_leg.quantity),
                is_long=False,
            )
            new_leg = Leg(
                trade_id=trade.id,
                leg_type=triggered_leg.leg_type,
                strike=float(plan.new_strike),
                symbol=plan.new_symbol,
                product_id=int(plan.new_product_id),
                initial_premium=new_entry_premium,
                trigger_baseline_premium=new_entry_premium,
                trigger_premium=new_entry_premium,
                quantity=int(triggered_leg.quantity),
                entry_time=now_utc,
                status="open",
                delta_at_entry=None,
                entry_fee_usd=(
                    abs(float(entry_result.commission))
                    if entry_result.commission is not None
                    else None
                ),
                order_sent_price=float(expected_new_entry),
                entry_spread_usd=new_leg_spread,
                delta_order_id=(
                    str(entry_result.order_id)
                    if entry_result.order_id is not None
                    else None
                ),
                sl_trigger_price=float(display_sl) if display_sl > 0 else None,
                delta_sl_order_id=None,  # bracket — no separate stop id
                is_bot_managed=True,
            )
            db_session.add(new_leg)
            # SL add-back = this new leg only (do NOT accumulate prior spreads)
            reset_entry_spread_for_sl(
                trade,
                new_leg_spread,
                reason="adjustment",
                leg=str(triggered_leg.leg_type),
            )

            # Untouched leg: KEEP original entry; ONLY reset trigger baseline
            # to Best Offer (ask). Soft fallback: mid, then keep existing.
            refreshed_offer = await _resolve_offer_price(
                delta_client,
                str(other_leg.symbol),
                keep_if_missing=other_old_baseline,
            )
            if refreshed_offer > 0:
                other_leg.trigger_baseline_premium = float(refreshed_offer)
                other_leg.trigger_premium = float(refreshed_offer)
                other_premium = float(refreshed_offer)
                logger.info(
                    "[BASELINE_RESET] %s baseline: %.2f → %.2f "
                    "(using offer price at adjustment time)",
                    other_leg.leg_type,
                    other_old_baseline,
                    refreshed_offer,
                )
                log_and_buffer(
                    "BASELINE_RESET",
                    int(trade.id),
                    {
                        "leg": str(other_leg.leg_type),
                        "old": round(other_old_baseline, 4),
                        "new": round(float(refreshed_offer), 4),
                        "source": "offer",
                    },
                )
            else:
                logger.warning(
                    "[BASELINE_RESET] Could not get offer for %s. "
                    "Keeping existing baseline: %s",
                    other_leg.symbol,
                    other_leg.trigger_baseline_premium,
                )

            # Realized from TRUE fill premium of closed leg (not trigger baseline)
            # USD = (entry - exit) * qty * contract_value  (matches Delta scale)
            leg_realized = short_leg_realized_pnl(
                entry_fill=old_entry_fill,
                exit_fill=old_exit_premium,
                quantity=int(triggered_leg.quantity),
            )
            triggered_leg.realized_pnl = leg_realized
            trade_row = (
                db_session.query(Trade).filter(Trade.id == trade.id).first()
            )
            if trade_row is None:
                raise AdjustmentError(f"Trade {trade.id} not found while updating realized_pnl")
            prior_realized = float(trade_row.realized_pnl or 0.0)
            trade_row.realized_pnl = prior_realized + leg_realized

            hours_left = get_hours_to_expiry(trade.expiry_date)
            # CORRECT: exit_fill / trigger_baseline (NOT exit / initial_premium)
            trigger_pct = (
                (old_exit_premium / triggered_baseline) * 100.0
                if triggered_baseline > 0
                else 0.0
            )
            adjustment = Adjustment(
                trade_id=trade.id,
                leg_type=triggered_leg.leg_type,
                trigger_pct_reached=trigger_pct,
                old_strike=old_strike,
                old_exit_premium=old_exit_premium,
                new_strike=float(plan.new_strike),
                new_entry_premium=new_entry_premium,
                timestamp=now_utc,
                time_remaining_hours=hours_left,
                slab_used=self._slab_label(hours_left),
                decision_type="ADJUSTED",
            )
            db_session.add(adjustment)

            # Increment per-trade adjustment counter
            try:
                prior_count = int(getattr(trade, "adjustment_count", 0) or 0)
            except (TypeError, ValueError):
                prior_count = 0
            trade.adjustment_count = prior_count + 1
            committed_adj_count = int(trade.adjustment_count)
            max_allowed = None
            try:
                from backend.database import get_or_create_auto_settings

                _lim = get_or_create_auto_settings(db_session)
                raw_max = getattr(_lim, "max_adjustments_per_basket", None)
                if raw_max is not None:
                    max_allowed = int(raw_max)
            except Exception:
                max_allowed = None
            log_and_buffer(
                "ADJUSTMENT_COUNT_UPDATED",
                int(trade.id),
                {
                    "new_count": committed_adj_count,
                    "max_allowed": max_allowed,
                },
            )
            logger.info(
                "ADJUSTMENT_COUNT_UPDATED | trade_id=%s | new_count=%s | max_allowed=%s",
                trade.id,
                committed_adj_count,
                max_allowed,
            )

            # Capture scalars BEFORE commit — never rely on ORM attrs after
            # commit if refresh fails (DetachedInstanceError → false FAIL).
            committed_trade_id = int(trade.id)
            committed_new_symbol = str(plan.new_symbol)
            committed_new_product_id = int(plan.new_product_id)
            committed_new_strike = float(plan.new_strike)
            committed_new_entry = float(new_entry_premium)
            committed_bracket_sl = float(display_sl) if display_sl > 0 else None
            committed_bracket_sl_limit = (
                float(bracket_sl_limit) if bracket_sl_limit > 0 else None
            )
            committed_old_strike = float(old_strike)
            committed_order_id = (
                str(entry_result.order_id)
                if entry_result.order_id is not None
                else None
            )
            committed_leg_realized = float(leg_realized)
            committed_trigger_pct = float(trigger_pct)
            committed_other_premium = float(other_premium)
            committed_trade_realized = float(
                getattr(trade_row, "realized_pnl", None) or 0.0
            )
            committed_triggered_qty = int(triggered_leg.quantity)
            committed_old_product_id = int(triggered_leg.product_id)

            # Commit with one retry on session errors (new leg already on Delta)
            def _commit_adjustment_db() -> None:
                db_session.expire_on_commit = False
                db_session.commit()
                try:
                    db_session.refresh(trade)
                except Exception as refresh_exc:
                    logger.warning(
                        "refresh(trade) after adjust commit: %s — merge retry",
                        refresh_exc,
                    )
                    try:
                        merged = db_session.merge(trade)
                        db_session.refresh(merged)
                    except Exception:
                        pass
                try:
                    db_session.refresh(new_leg)
                except Exception as refresh_exc:
                    logger.warning(
                        "refresh(new_leg) after adjust commit: %s",
                        refresh_exc,
                    )
                for _leg in (triggered_leg, other_leg):
                    try:
                        db_session.refresh(_leg)
                    except Exception:
                        pass

            try:
                _commit_adjustment_db()
            except Exception as commit_exc:
                logger.error(
                    "Adjustment DB commit failed trade=%s (new leg already "
                    "on Delta product=%s): %s — rollback + retry once",
                    committed_trade_id,
                    committed_new_product_id,
                    commit_exc,
                    exc_info=True,
                )
                try:
                    db_session.rollback()
                except Exception:
                    logger.exception("Rollback failed before commit retry")
                # Re-attach objects and retry persist once
                try:
                    db_session.expire_on_commit = False
                    trade = db_session.merge(trade)
                    triggered_leg = db_session.merge(triggered_leg)
                    other_leg = db_session.merge(other_leg)
                    # Re-apply closed state + new leg if rollback wiped them
                    triggered_leg.exit_premium = old_exit_premium
                    triggered_leg.exit_time = now_utc
                    triggered_leg.status = "closed"
                    if exit_result.order_id is not None:
                        triggered_leg.exit_order_id = str(exit_result.order_id)
                    if exit_result.commission is not None:
                        triggered_leg.exit_fee_usd = abs(
                            float(exit_result.commission)
                        )
                    triggered_leg.realized_pnl = leg_realized

                    # Ensure new_leg is in this session
                    existing_new = (
                        db_session.query(Leg)
                        .filter(
                            Leg.trade_id == committed_trade_id,
                            Leg.product_id == committed_new_product_id,
                            Leg.status == "open",
                            Leg.is_bot_managed.is_(True),
                        )
                        .first()
                    )
                    if existing_new is None:
                        new_leg = Leg(
                            trade_id=committed_trade_id,
                            leg_type=triggered_leg.leg_type,
                            strike=committed_new_strike,
                            symbol=committed_new_symbol,
                            product_id=committed_new_product_id,
                            initial_premium=committed_new_entry,
                            trigger_baseline_premium=committed_new_entry,
                            trigger_premium=committed_new_entry,
                            quantity=committed_triggered_qty,
                            entry_time=now_utc,
                            status="open",
                            delta_at_entry=None,
                            entry_fee_usd=(
                                abs(float(entry_result.commission))
                                if entry_result.commission is not None
                                else None
                            ),
                            order_sent_price=float(expected_new_entry),
                            entry_spread_usd=new_leg_spread,
                            delta_order_id=committed_order_id,
                            sl_trigger_price=(
                                float(display_sl) if display_sl > 0 else None
                            ),
                            delta_sl_order_id=None,
                            is_bot_managed=True,
                        )
                        db_session.add(new_leg)
                        from backend.core.fees import reset_entry_spread_for_sl

                        reset_entry_spread_for_sl(
                            trade,
                            new_leg_spread,
                            reason="adjustment",
                            leg=str(triggered_leg.leg_type),
                        )
                    else:
                        new_leg = existing_new

                    trade_row = (
                        db_session.query(Trade)
                        .filter(Trade.id == committed_trade_id)
                        .first()
                    )
                    if trade_row is not None:
                        trade_row.realized_pnl = float(committed_trade_realized)

                    other_leg.trigger_baseline_premium = committed_other_premium
                    other_leg.trigger_premium = committed_other_premium
                    trade.adjustment_count = committed_adj_count

                    adj_exists = (
                        db_session.query(Adjustment)
                        .filter(
                            Adjustment.trade_id == committed_trade_id,
                            Adjustment.new_strike == committed_new_strike,
                            Adjustment.old_strike == committed_old_strike,
                        )
                        .order_by(Adjustment.id.desc())
                        .first()
                    )
                    if adj_exists is None:
                        db_session.add(
                            Adjustment(
                                trade_id=committed_trade_id,
                                leg_type=triggered_leg.leg_type,
                                trigger_pct_reached=committed_trigger_pct,
                                old_strike=committed_old_strike,
                                old_exit_premium=old_exit_premium,
                                new_strike=committed_new_strike,
                                new_entry_premium=committed_new_entry,
                                timestamp=now_utc,
                                time_remaining_hours=hours_left,
                                slab_used=self._slab_label(hours_left),
                                decision_type="ADJUSTED",
                            )
                        )

                    _commit_adjustment_db()
                    logger.info(
                        "Adjustment DB commit RETRY succeeded trade=%s",
                        committed_trade_id,
                    )
                except Exception as retry_exc:
                    logger.critical(
                        "Adjustment DB commit RETRY failed trade=%s: %s. "
                        "Delta has new leg product=%s — manual DB repair may "
                        "be required.",
                        committed_trade_id,
                        retry_exc,
                        committed_new_product_id,
                        exc_info=True,
                    )
                    try:
                        db_session.rollback()
                    except Exception:
                        pass
                    return AdjustmentResult(
                        success=False,
                        error_message=(
                            f"DB commit failed after new leg entry on Delta "
                            f"(product={committed_new_product_id}): {retry_exc}"
                        ),
                    )

            # With bracket SLs attached to entry orders, there is nothing to
            # "refresh" as part of adjustment. The new leg's bracket SL was
            # attached at order placement time above.

            new_leg_id = getattr(new_leg, "id", None)
            logger.info(
                "Adjustment DB committed: new_leg_id=%s symbol=%s "
                "product_id=%s status=open entry=%s baseline=%s",
                new_leg_id,
                committed_new_symbol,
                committed_new_product_id,
                committed_new_entry,
                committed_new_entry,
            )
            logger.info(
                "Adjustment baseline reset: "
                "triggered_leg entry=%s baseline=%s "
                "other_leg entry(kept)=baseline reset to %s",
                committed_new_entry,
                committed_new_entry,
                committed_other_premium,
            )
            logger.info(
                "Adjustment success trade=%s %s %s→%s premium_collected=%s "
                "delta_order_id=%s baselines reset triggered=%s other=%s "
                "trigger_pct_reached=%.2f (vs baseline %.2f) "
                "leg_realized=%s trade_realized_pnl=%s",
                committed_trade_id,
                triggered_leg_type,
                committed_old_strike,
                committed_new_strike,
                committed_new_entry,
                committed_order_id,
                committed_new_entry,
                committed_other_premium,
                committed_trigger_pct,
                triggered_baseline,
                committed_leg_realized,
                committed_trade_realized,
            )

            # Mirror is invoked by BotEngine._adjust_trade after success
            # (awaited there so exceptions are not lost on create_task).
            return AdjustmentResult(
                success=True,
                old_strike=committed_old_strike,
                new_strike=committed_new_strike,
                premium_collected=committed_new_entry,
                old_product_id=committed_old_product_id,
                new_product_id=committed_new_product_id,
                new_symbol=committed_new_symbol,
                quantity=committed_triggered_qty,
                master_bracket_sl=committed_bracket_sl,
                master_bracket_sl_limit=committed_bracket_sl_limit,
            )
        except AdjustmentError as exc:
            logger.error("Adjustment failed trade=%s: %s", getattr(trade, "id", "?"), exc)
            try:
                db_session.rollback()
            except Exception:
                logger.exception("Rollback failed after AdjustmentError")
            return AdjustmentResult(success=False, error_message=str(exc))
        except Exception as exc:
            logger.critical(
                "Unexpected adjustment failure trade=%s: %s",
                getattr(trade, "id", "?"),
                exc,
                exc_info=True,
            )
            try:
                db_session.rollback()
            except Exception:
                logger.exception("Rollback failed after unexpected error")
            return AdjustmentResult(success=False, error_message=str(exc))

    def _get_legs(self, trade: Any, db_session: Any) -> tuple[Any, Any]:
        """
        Return (call_leg, put_leg) open bot-managed legs for this trade.

        BOT ISOLATION: only legs from our DB with is_bot_managed=True.
        """
        try:
            trade_id = int(getattr(trade, "id", 0) or 0)
        except Exception:
            trade_id = 0
        if trade_id <= 0:
            raise AdjustmentError("Invalid trade id when loading legs")

        legs = (
            db_session.query(Leg)
            .filter(
                Leg.trade_id == trade_id,
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
                Leg.leg_type.in_(("call", "put")),
            )
            .all()
        )
        call_leg = next((leg for leg in legs if leg.leg_type == "call"), None)
        put_leg = next((leg for leg in legs if leg.leg_type == "put"), None)
        if call_leg is None or put_leg is None:
            raise AdjustmentError(
                f"Open bot-managed call/put legs not found for trade {trade_id}"
            )
        return call_leg, put_leg

    def _resolve_legs(
        self,
        triggered_leg_type: str,
        call_leg: Any,
        put_leg: Any,
    ) -> tuple[Any, Any]:
        leg = triggered_leg_type.lower().strip()
        if leg == "call":
            return call_leg, put_leg
        if leg == "put":
            return put_leg, call_leg
        raise AdjustmentError(f"Invalid triggered_leg_type: {triggered_leg_type}")

    def _mark_leg_closed_partial(
        self,
        triggered_leg: Any,
        exit_result: OrderResult,
        db_session: Any,
    ) -> None:
        """
        Persist partial state: old leg closed, new leg not opened.

        Trade stays ACTIVE with the remaining open leg. Caller must sync
        position_tracker so integrity checks do NOT emergency-close.
        """
        from backend.models import Trade as TradeModel

        exit_px = float(exit_result.filled_price or 0.0)
        entry_px = float(triggered_leg.initial_premium or 0.0)
        triggered_leg.exit_premium = exit_px
        triggered_leg.exit_time = get_utc_now()
        triggered_leg.status = "closed"
        if exit_result.order_id is not None:
            triggered_leg.exit_order_id = str(exit_result.order_id)
        if exit_result.commission is not None:
            triggered_leg.exit_fee_usd = abs(float(exit_result.commission))

        leg_realized = short_leg_realized_pnl(
            entry_fill=entry_px,
            exit_fill=exit_px,
            quantity=int(triggered_leg.quantity or 0),
        )
        triggered_leg.realized_pnl = leg_realized
        trade_row = (
            db_session.query(TradeModel)
            .filter(TradeModel.id == triggered_leg.trade_id)
            .first()
        )
        if trade_row is not None:
            # Keep trade ACTIVE — one-legged until user closes remaining
            prior = float(trade_row.realized_pnl or 0.0)
            trade_row.realized_pnl = prior + leg_realized
            if str(trade_row.status).lower() == "closed":
                logger.critical(
                    "Partial adjustment: trade %s was CLOSED — forcing ACTIVE "
                    "so remaining leg stays monitored",
                    trade_row.id,
                )
                trade_row.status = "active"
                trade_row.exit_reason = None
                trade_row.exit_time = None

        db_session.expire_on_commit = False
        db_session.commit()
        try:
            db_session.refresh(triggered_leg)
        except Exception:
            pass
        if trade_row is not None:
            try:
                db_session.refresh(trade_row)
            except Exception:
                pass
        logger.critical(
            "Partial adjustment DB updated: leg_id=%s marked closed "
            "(one-legged). Trade stays ACTIVE for manual close of remaining leg.",
            getattr(triggered_leg, "id", "?"),
        )

    def _log_partial_error(
        self,
        trade: Any,
        triggered_leg_type: str,
        exit_result: OrderResult,
    ) -> None:
        logger.critical(
            "PARTIAL ADJUSTMENT on trade %s: "
            "%s leg closed at %s "
            "but new entry FAILED. Position is now one-legged! "
            "Manual intervention required.",
            trade.id,
            triggered_leg_type,
            exit_result.filled_price,
        )

    @staticmethod
    def _slab_label(hours_left: float) -> str:
        if hours_left > 24:
            return "slab_24h"
        if hours_left > 12:
            return "slab_12h"
        if hours_left > 6:
            return "slab_6h"
        return "slab_lt6h"
