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
        )

    async def sell_option(
        self,
        product_id: int,
        quantity: int,
        delta_client: Any,
        symbol_for_fallback: str | None = None,
        bracket_sl_price: float | None = None,
    ) -> OrderResult:
        """Place a SELL market IOC order to open a new short option."""
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
        sl_limit_px: float | None = (
            round(sl_px * 1.05, 2) if sl_px is not None and sl_px > 0 else None
        )

        return await self._execute_with_retry(
            delta_client=delta_client,
            product_id=int(product_id),
            size=int(quantity),
            side="sell",
            symbol_for_fallback=symbol_for_fallback,
            bracket_stop_loss_price=sl_px,
            bracket_stop_loss_limit_price=sl_limit_px,
        )

    async def _execute_with_retry(
        self,
        delta_client: Any,
        product_id: int,
        size: int,
        side: str,
        symbol_for_fallback: str | None = None,
        bracket_stop_loss_price: float | None = None,
        bracket_stop_loss_limit_price: float | None = None,
    ) -> OrderResult:
        """
        Attempt order once; on DeltaAPIError wait 2s and retry once only.
        """
        max_attempts = self.MAX_RETRIES + 1
        last_error = "unknown error"

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Attempt %s/%s: placing order side=%s product_id=%s size=%s",
                attempt,
                max_attempts,
                side,
                product_id,
                size,
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
                return OrderResult(
                    success=True,
                    order_id=int(order_id) if order_id is not None else None,
                    filled_price=filled_price,
                    commission=float(commission or 0.0),
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
