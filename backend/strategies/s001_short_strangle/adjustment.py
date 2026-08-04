# adjustment.py — Atomic adjustment trigger detection and execution for S001

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow import when run as a script
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.time_utils import get_hours_to_expiry
from backend.core.delta_client import short_leg_realized_pnl
from backend.models import Adjustment, Leg, Trade
from backend.strategies.base_strategy import AdjustmentResult, OrderResult

logger = logging.getLogger(__name__)


class AdjustmentError(Exception):
    """Raised for adjustment precondition failures (missing legs, etc.)."""


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
        """
        try:
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

            logger.info(
                "Adjustment start trade=%s leg=%s old_strike=%s product_id=%s",
                trade.id,
                triggered_leg_type,
                triggered_leg.strike,
                triggered_leg.product_id,
            )

            other_premium = await delta_client.get_mark_price(other_leg.symbol)
            try:
                plan = await strategy.find_adjustment_strike(
                    delta_client,
                    trade,
                    triggered_leg_type,
                    other_premium,
                    current_strike=float(triggered_leg.strike),
                )
            except Exception as exc:
                msg = str(exc)
                # Only HOLD when literally no other strike exists on the chain.
                # Trigger = must adjust; never skip for "imperfect" premium.
                if "SAME_STRIKE_HOLD" in msg and "no other" in msg.lower():
                    logger.warning(
                        "Adjustment HOLD trade=%s leg=%s — %s",
                        trade.id,
                        triggered_leg_type,
                        msg,
                    )
                    return AdjustmentResult(
                        success=False,
                        old_strike=float(triggered_leg.strike),
                        error_message=f"ADJUSTMENT_HOLD: {msg}",
                    )
                raise

            plan.exit_leg_symbol = triggered_leg.symbol

            # Guard: never adjust into the same strike / product
            if (
                abs(float(plan.new_strike) - float(triggered_leg.strike)) < 0.01
                or int(plan.new_product_id) == int(triggered_leg.product_id)
            ):
                msg = (
                    f"SAME_STRIKE_HOLD: replacement {plan.new_strike} "
                    f"equals current {triggered_leg.strike} — skipping to avoid "
                    f"brokerage/slippage with no strike change"
                )
                logger.warning("Adjustment HOLD trade=%s — %s", trade.id, msg)
                return AdjustmentResult(
                    success=False,
                    old_strike=float(triggered_leg.strike),
                    new_strike=float(plan.new_strike),
                    error_message=msg,
                )

            # Step 4: Close triggered leg
            exit_result: OrderResult = await order_executor.close_leg(
                triggered_leg, delta_client
            )
            if not exit_result.success:
                msg = (
                    f"Failed to exit {triggered_leg_type} leg: "
                    f"{exit_result.error or 'unknown error'}"
                )
                logger.error("Adjustment abort trade=%s — %s", trade.id, msg)
                return AdjustmentResult(success=False, error_message=msg)

            # Step 5: Enter new leg
            entry_result: OrderResult = await order_executor.sell_option(
                plan.new_product_id,
                int(triggered_leg.quantity),
                delta_client,
            )
            if not entry_result.success:
                self._log_partial_error(trade, triggered_leg_type, exit_result)
                self._mark_leg_closed_partial(
                    triggered_leg, exit_result, db_session
                )
                return AdjustmentResult(
                    success=False,
                    is_partial=True,
                    old_strike=float(triggered_leg.strike),
                    error_message=(
                        "PARTIAL: Old leg closed but new entry failed. "
                        f"{entry_result.error or ''}"
                    ).strip(),
                )

            # Steps 6–8: Update DB on full success
            # Triggered leg → new fill as trigger baseline.
            # Other leg → keep existing trigger_premium (no mark reset).
            now_utc = datetime.now(timezone.utc)
            old_strike = float(triggered_leg.strike)
            old_baseline = float(triggered_leg.initial_premium)
            old_exit_premium = float(exit_result.filled_price or 0.0)
            new_entry_premium = float(entry_result.filled_price or 0.0)

            triggered_leg.exit_premium = old_exit_premium
            triggered_leg.exit_time = now_utc
            triggered_leg.status = "closed"
            if exit_result.order_id is not None:
                triggered_leg.exit_order_id = str(exit_result.order_id)
            if exit_result.commission is not None:
                triggered_leg.exit_fee_usd = abs(float(exit_result.commission))

            # New leg: fill = accounting entry AND fresh trigger baseline
            new_leg = Leg(
                trade_id=trade.id,
                leg_type=triggered_leg.leg_type,
                strike=float(plan.new_strike),
                symbol=plan.new_symbol,
                product_id=int(plan.new_product_id),
                initial_premium=new_entry_premium,
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
                delta_order_id=(
                    str(entry_result.order_id)
                    if entry_result.order_id is not None
                    else None
                ),
                is_bot_managed=True,
            )
            db_session.add(new_leg)

            # Other leg keeps its existing trigger baseline (avoids 103% cascades)
            other_baseline = float(
                getattr(other_leg, "trigger_premium", None)
                or other_leg.initial_premium
            )

            # Realized from TRUE fill premium (initial_premium), not trigger baseline
            # USD = (entry - exit) * qty * contract_value  (matches Delta scale)
            leg_realized = short_leg_realized_pnl(
                entry_fill=old_baseline,
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
            trigger_pct = (
                (old_exit_premium / old_baseline) * 100.0 if old_baseline > 0 else 0.0
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
            )
            db_session.add(adjustment)
            db_session.commit()

            logger.info(
                "Adjustment success trade=%s %s %s→%s premium_collected=%s "
                "delta_order_id=%s baselines reset triggered=%s other=%s "
                "leg_realized=%s trade_realized_pnl=%s",
                trade.id,
                triggered_leg_type,
                old_strike,
                plan.new_strike,
                new_entry_premium,
                new_leg.delta_order_id,
                new_entry_premium,
                other_baseline,
                leg_realized,
                trade_row.realized_pnl,
            )
            return AdjustmentResult(
                success=True,
                old_strike=old_strike,
                new_strike=float(plan.new_strike),
                premium_collected=new_entry_premium,
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
        legs = (
            db_session.query(Leg)
            .filter(
                Leg.trade_id == trade.id,
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .all()
        )
        call_leg = next((leg for leg in legs if leg.leg_type == "call"), None)
        put_leg = next((leg for leg in legs if leg.leg_type == "put"), None)
        if call_leg is None or put_leg is None:
            raise AdjustmentError(
                f"Open bot-managed call/put legs not found for trade {trade.id}"
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
        """Persist partial state: old leg closed, new leg not opened."""
        triggered_leg.exit_premium = float(exit_result.filled_price or 0.0)
        triggered_leg.exit_time = datetime.now(timezone.utc)
        triggered_leg.status = "closed"
        db_session.commit()
        logger.critical(
            "Partial adjustment DB updated: leg_id=%s marked closed (one-legged)",
            triggered_leg.id,
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
