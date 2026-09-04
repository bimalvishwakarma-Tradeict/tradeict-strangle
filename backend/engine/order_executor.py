# order_executor.py — Atomic order execution wrapper with retry for Delta orders

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.delta_client import DeltaAPIError
from backend.strategies.base_strategy import OrderResult

logger = logging.getLogger(__name__)


class OrderExecutor:
    """
    Places market orders via DeltaClient with a single retry.

    NEVER retries more than once — market-order duplicates are dangerous.
    """

    MAX_RETRIES = 1  # one retry → max 2 attempts total
    RETRY_DELAY_SECONDS = 2.0

    async def close_leg(self, leg: Any, delta_client: Any) -> OrderResult:
        """
        Close a short option leg with a BUY market IOC order.

        ISOLATION: Only call this for bot-managed legs from our DB
        (Leg.is_bot_managed=True, product_id + quantity from our DB —
         never size from Delta positions API).
        """
        if not getattr(leg, "is_bot_managed", False):
            msg = f"Refusing close_leg: leg_id={getattr(leg, 'id', '?')} not bot-managed"
            logger.error(msg)
            return OrderResult(success=False, error=msg)

        if str(getattr(leg, "status", "open")).lower() == "closed":
            logger.info(
                "close_leg skipped — already closed leg_id=%s %s",
                getattr(leg, "id", "?"),
                getattr(leg, "symbol", ""),
            )
            return OrderResult(
                success=True,
                filled_price=float(getattr(leg, "exit_premium", None) or 0.0),
                order_id=getattr(leg, "delta_order_id", None),
            )

        logger.info(
            "Closing %s leg at %s, qty=%s product_id=%s",
            leg.leg_type,
            leg.symbol,
            leg.quantity,
            leg.product_id,
        )
        return await self._execute_with_retry(
            delta_client=delta_client,
            product_id=int(leg.product_id),
            size=int(leg.quantity),
            side="buy",
            symbol_for_fallback=str(leg.symbol),
            reduce_only=True,  # CRITICAL: prevents position flip on exit
        )

    async def sell_option(
        self,
        product_id: int,
        quantity: int,
        delta_client: Any,
        symbol_for_fallback: str | None = None,
        bracket_sl_price: float | None = None,
        bracket_sl_limit: float | None = None,
    ) -> OrderResult:
        """
        Place a SELL market IOC order to open a new short option.

        Bracket SL confirmed working on Delta Exchange India.
        Format: bracket_stop_loss_price + bracket_stop_loss_limit_price
        Bracket auto-cancels when position is closed (any reason).
        No orphan stop orders remain after trade exit.
        """
        logger.info(
            "Selling option product_id=%s, qty=%s",
            product_id,
            quantity,
        )

        sl_px: float | None = (
            round(float(bracket_sl_price), 2)
            if bracket_sl_price is not None
            else None
        )
        if sl_px is not None and sl_px <= 0:
            sl_px = None

        if bracket_sl_limit is not None and sl_px is not None:
            sl_limit_px: float | None = round(float(bracket_sl_limit), 2)
        elif sl_px is not None:
            # Default: limit = stop × 1.05 for buy bracket on short entry
            sl_limit_px = round(sl_px * 1.05, 2)
        else:
            sl_limit_px = None

        return await self._execute_with_retry(
            delta_client=delta_client,
            product_id=int(product_id),
            size=int(quantity),
            side="sell",
            symbol_for_fallback=symbol_for_fallback,
            bracket_stop_loss_price=sl_px,
            bracket_stop_loss_limit_price=sl_limit_px,
        )

    async def buy_option(
        self,
        product_id: int,
        quantity: int,
        delta_client: Any,
        symbol_for_fallback: str | None = None,
    ) -> OrderResult:
        """
        Place a BUY market IOC order (e.g. conversion-mode hedge).

        No bracket SL — long hedge is closed manually when premiums equalize.
        """
        logger.info(
            "Buying option product_id=%s, qty=%s",
            product_id,
            quantity,
        )
        return await self._execute_with_retry(
            delta_client=delta_client,
            product_id=int(product_id),
            size=int(quantity),
            side="buy",
            symbol_for_fallback=symbol_for_fallback,
        )

    async def close_long_position(
        self,
        product_id: int,
        quantity: int,
        delta_client: Any,
        symbol_for_fallback: str | None = None,
    ) -> OrderResult:
        """
        Close a long option (hedge/wing) with a SELL market IOC order.

        No bracket_stop_loss — hedge is a long buy; SL brackets apply only
        to short sells.
        """
        logger.info(
            "Closing long position product_id=%s qty=%s",
            product_id,
            quantity,
        )
        try:
            result = await delta_client.place_order(
                product_id=int(product_id),
                size=int(quantity),
                side="sell",
                order_type="market_order",
                time_in_force="ioc",
                reduce_only=True,  # CRITICAL: prevents position flip
            )
            fill = float(result.get("avg_fill_price") or 0)
            if fill <= 0 and symbol_for_fallback:
                try:
                    fill = float(
                        await delta_client.resolve_fill_price(
                            result, symbol_for_fallback=symbol_for_fallback
                        )
                        or 0
                    )
                except Exception:
                    pass
            status = str(result.get("status") or "").lower()
            oid = result.get("order_id")

            # Same commission pattern as _execute_with_retry: place response
            # first, then get_order_commission. Leave None when unknown —
            # do not write 0.0 (fee-unknown ≠ fee-zero).
            commission: float | None = None
            raw = (
                result.get("raw")
                if isinstance(result.get("raw"), dict)
                else {}
            )
            for src in (result, raw):
                for field in ("paid_commission", "commission"):
                    try:
                        val = abs(float(src.get(field) or 0))
                    except (TypeError, ValueError):
                        val = 0.0
                    if val > 0:
                        commission = val
                        break
                if commission is not None:
                    break
            if commission is None and oid is not None:
                try:
                    commission = abs(
                        float(await delta_client.get_order_commission(oid))
                    )
                except Exception as fee_exc:
                    logger.warning(
                        "Could not fetch commission for long-close "
                        "order %s: %s",
                        oid,
                        fee_exc,
                    )
                    commission = None

            return OrderResult(
                success=(
                    status in ("closed", "filled", "open") or oid is not None
                ),
                filled_price=fill if fill > 0 else None,
                order_id=int(oid) if oid is not None else None,
                commission=commission,
            )
        except Exception as exc:
            logger.error("close_long_position failed: %s", exc)
            return OrderResult(success=False, error=str(exc))

    async def _execute_with_retry(
        self,
        delta_client: Any,
        product_id: int,
        size: int,
        side: str,
        symbol_for_fallback: str | None = None,
        bracket_stop_loss_price: float | None = None,
        bracket_stop_loss_limit_price: float | None = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """
        Attempt order once; on DeltaAPIError wait 2s and retry once only.
        """
        max_attempts = self.MAX_RETRIES + 1
        last_error = "unknown error"

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Attempt %s/%s: placing order side=%s product_id=%s size=%s "
                "reduce_only=%s",
                attempt,
                max_attempts,
                side,
                product_id,
                size,
                reduce_only,
            )
            try:
                result = await delta_client.place_order(
                    product_id=product_id,
                    size=size,
                    side=side,
                    order_type="market_order",
                    time_in_force="ioc",
                    bracket_stop_loss_price=bracket_stop_loss_price,
                    bracket_stop_loss_limit_price=bracket_stop_loss_limit_price,
                    reduce_only=reduce_only,
                )
                filled_price = await delta_client.resolve_fill_price(
                    result,
                    symbol_for_fallback=symbol_for_fallback,
                )
                order_id = result.get("order_id")
                commission = 0.0
                # Prefer commission on place response, else paid_commission from order/fills
                raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
                for src in (result, raw):
                    for field in ("paid_commission", "commission"):
                        try:
                            val = abs(float(src.get(field) or 0))
                        except (TypeError, ValueError):
                            val = 0.0
                        if val > 0:
                            commission = val
                            break
                    if commission > 0:
                        break
                if commission <= 0 and order_id is not None:
                    try:
                        commission = float(
                            await delta_client.get_order_commission(order_id)
                        )
                    except Exception as fee_exc:
                        logger.warning(
                            "Could not fetch commission for order %s: %s",
                            order_id,
                            fee_exc,
                        )
                logger.info(
                    "Order success attempt=%s order_id=%s filled_price=%s commission=%s",
                    attempt,
                    order_id,
                    filled_price,
                    commission,
                )
                filled_size = size
                try:
                    raw_size = result.get("size")
                    if raw_size is not None:
                        filled_size = int(raw_size)
                except (TypeError, ValueError):
                    filled_size = size
                if isinstance(raw, dict):
                    try:
                        if raw.get("filled_size") is not None:
                            filled_size = int(float(raw["filled_size"]))
                        elif raw.get("unfilled_size") is not None:
                            filled_size = max(
                                0, int(size) - int(float(raw["unfilled_size"]))
                            )
                    except (TypeError, ValueError):
                        pass
                return OrderResult(
                    success=True,
                    order_id=int(order_id) if order_id is not None else None,
                    filled_price=filled_price,
                    commission=float(commission or 0.0),
                    filled_size=int(filled_size),
                )
            except DeltaAPIError as exc:
                last_error = str(exc)
                logger.error(
                    "Order attempt %s/%s failed: %s",
                    attempt,
                    max_attempts,
                    last_error,
                )
                if attempt <= self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS)
            except Exception as exc:
                last_error = str(exc)
                logger.error(
                    "Order attempt %s/%s unexpected error: %s",
                    attempt,
                    max_attempts,
                    last_error,
                    exc_info=True,
                )
                if attempt <= self.MAX_RETRIES:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS)

        return OrderResult(success=False, error=last_error)

    async def close_basket_legs_phased(
        self,
        *,
        trade: Any,
        reason: str,
        db: Any,
        delta_client: Any,
        legs: list[Any],
        verify_on_delta: bool = True,
        include_structure_hedge: bool = False,
    ) -> Any:
        """
        Close basket legs in enforced phases: Shorts → Wings → Hedges.

        Stop-loss exits skip mid-price sequencing — market close via wing_exit.
        """
        from backend.engine.wing_exit import (
            CloseResult,
            both_shorts_closed,
            close_basket_legs,
            is_conversion_hedge_leg,
            is_short_basket_leg,
            is_wing_leg,
        )
        from backend.strategies.s001_short_strangle.logic import (
            exit_phases_for_trade,
            is_stoploss_exit_reason,
            log_sequence_step,
        )

        trade_id = int(getattr(trade, "id", 0) or 0)
        has_wings = any(is_wing_leg(leg) for leg in legs)
        phases = exit_phases_for_trade(
            include_hedge=include_structure_hedge or any(
                is_conversion_hedge_leg(leg) for leg in legs
            ),
            condor_only=has_wings and not include_structure_hedge,
        )

        if is_stoploss_exit_reason(reason):
            log_sequence_step(
                trade_id=trade_id,
                action="exit_parallel_market",
                phase="all",
                position=0,
                reason=reason,
            )
            return await close_basket_legs(
                trade=trade,
                reason=reason,
                db=db,
                delta_client=delta_client,
                order_executor=self,
                legs_to_close="all",
                legs=legs,
                verify_on_delta=verify_on_delta,
            )

        combined = CloseResult()
        open_legs = list(legs)

        for pos, phase in enumerate(phases, start=1):
            log_sequence_step(
                trade_id=trade_id,
                action="exit_phase_start",
                phase=phase,
                position=pos,
                reason=reason,
            )

            if phase == "short":
                if not both_shorts_closed(open_legs):
                    bundle = await close_basket_legs(
                        trade=trade,
                        reason=reason,
                        db=db,
                        delta_client=delta_client,
                        order_executor=self,
                        legs_to_close="shorts_only",
                        legs=open_legs,
                        verify_on_delta=verify_on_delta,
                    )
                else:
                    bundle = CloseResult()
            elif phase == "wing":
                if not both_shorts_closed(open_legs):
                    log_sequence_step(
                        trade_id=trade_id,
                        action="exit_phase_blocked",
                        phase=phase,
                        position=pos,
                        reason="shorts_not_fully_closed",
                    )
                    continue
                bundle = await close_basket_legs(
                    trade=trade,
                    reason=reason,
                    db=db,
                    delta_client=delta_client,
                    order_executor=self,
                    legs_to_close="wings_only",
                    legs=open_legs,
                    verify_on_delta=verify_on_delta,
                )
            else:
                hedge_legs = [
                    leg
                    for leg in open_legs
                    if is_conversion_hedge_leg(leg)
                    and str(getattr(leg, "status", "open")).lower() == "open"
                ]
                if not hedge_legs:
                    log_sequence_step(
                        trade_id=trade_id,
                        action="exit_phase_skip",
                        phase=phase,
                        position=pos,
                        reason="no_conversion_hedges",
                    )
                    continue
                if has_wings and not both_shorts_closed(open_legs):
                    log_sequence_step(
                        trade_id=trade_id,
                        action="exit_phase_blocked",
                        phase=phase,
                        position=pos,
                        reason="shorts_not_fully_closed",
                    )
                    continue
                bundle = await close_basket_legs(
                    trade=trade,
                    reason=reason,
                    db=db,
                    delta_client=delta_client,
                    order_executor=self,
                    legs_to_close="all",
                    legs=hedge_legs,
                    verify_on_delta=verify_on_delta,
                )

            combined.legs.extend(bundle.legs)
            combined.shorts_closed += bundle.shorts_closed
            combined.wings_closed += bundle.wings_closed
            combined.hedges_closed += bundle.hedges_closed
            if bundle.any_wing_fail:
                combined.any_wing_fail = True
                combined.wings_failed.extend(bundle.wings_failed)

            for leg in open_legs:
                lid = getattr(leg, "id", None)
                for row in bundle.legs:
                    if row.success and row.leg_id is not None and int(row.leg_id) == int(lid):
                        leg.status = "closed"

            log_sequence_step(
                trade_id=trade_id,
                action="exit_phase_complete",
                phase=phase,
                position=pos,
                shorts_closed=bundle.shorts_closed,
                wings_closed=bundle.wings_closed,
                hedges_closed=bundle.hedges_closed,
            )

        log_sequence_step(
            trade_id=trade_id,
            action="parallel_fill_completed",
            phase="exit",
            position=len(phases),
            reason=reason,
        )
        return combined

    async def execute_entry_phase(
        self,
        *,
        trade_id: int,
        phase: str,
        position: int,
        place_fn: Any,
        roles: list[str],
    ) -> list[Any]:
        """
        Execute one entry phase sequentially. One-way dependency: failure in
        this phase does not unwind prior phases (caller handles retry).
        """
        from backend.strategies.s001_short_strangle.logic import log_sequence_step

        log_sequence_step(
            trade_id=trade_id,
            action="entry_phase_start",
            phase=phase,
            position=position,
            roles=roles,
        )
        results: list[Any] = []
        for role in roles:
            log_sequence_step(
                trade_id=trade_id,
                action="entry_leg_start",
                phase=phase,
                position=position,
                leg_type=role,
            )
            result = await place_fn(role)
            results.append(result)
            if not getattr(result, "success", False):
                log_sequence_step(
                    trade_id=trade_id,
                    action="entry_phase_failed",
                    phase=phase,
                    position=position,
                    leg_type=role,
                    error=str(getattr(result, "error", "") or "failed"),
                )
                return results
        log_sequence_step(
            trade_id=trade_id,
            action="entry_phase_complete",
            phase=phase,
            position=position,
            roles=roles,
        )
        return results
