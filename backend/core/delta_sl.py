# delta_sl.py — Place / cancel Delta Exchange per-leg stop-loss safety orders

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def compute_sl_trigger_price(baseline_premium: float, universal_sl_pct: float) -> float:
    """SL trigger = baseline × (universal_sl_pct / 100)."""
    base = float(baseline_premium or 0.0)
    pct = float(universal_sl_pct or 200.0)
    if base <= 0 or pct <= 0:
        return 0.0
    return round(base * (pct / 100.0), 2)


async def cancel_leg_sl_order(
    delta_client: Any,
    leg: Any,
    *,
    clear_fields: bool = True,
) -> bool:
    """Cancel Delta SL order for a leg. Returns True if cancel attempted successfully."""
    oid = getattr(leg, "delta_sl_order_id", None)
    if not oid:
        return True
    try:
        await delta_client.cancel_order(int(oid))
        logger.info(
            "Cancelled SL order %s for %s leg",
            oid,
            getattr(leg, "leg_type", "?"),
        )
        if clear_fields:
            leg.delta_sl_order_id = None
            # keep sl_trigger_price until replaced (caller may overwrite)
        return True
    except Exception as exc:
        logger.warning(
            "Could not cancel SL order %s for %s: %s",
            oid,
            getattr(leg, "leg_type", "?"),
            exc,
        )
        return False


async def place_leg_sl_order(
    delta_client: Any,
    leg: Any,
    *,
    baseline_premium: float,
    universal_sl_pct: float,
    quantity: int | None = None,
) -> dict[str, Any]:
    """
    Place buy-to-close stop-market SL on Delta for a short option leg.

    Never raises — returns {success, order_id, stop_price, error}.
    On success updates leg.delta_sl_order_id and leg.sl_trigger_price.
    """
    stop_px = compute_sl_trigger_price(baseline_premium, universal_sl_pct)
    qty = abs(int(quantity if quantity is not None else getattr(leg, "quantity", 0) or 0))
    product_id = int(getattr(leg, "product_id", 0) or 0)
    leg_type = str(getattr(leg, "leg_type", "?") or "?")

    # Bracket-based mode: when the leg was placed with bracket SL attached,
    # we already persist `sl_trigger_price` for display and there is no
    # separate stop-loss order to place/refresh.
    if (
        getattr(leg, "delta_order_id", None)
        and getattr(leg, "sl_trigger_price", None) is not None
        and not getattr(leg, "delta_sl_order_id", None)
    ):
        return {
            "success": True,
            "order_id": None,
            "stop_price": float(getattr(leg, "sl_trigger_price") or stop_px),
            "error": None,
        }

    if stop_px <= 0 or qty <= 0 or product_id <= 0:
        msg = (
            f"Invalid SL params leg={leg_type} stop={stop_px} "
            f"qty={qty} product_id={product_id}"
        )
        logger.warning(msg)
        return {"success": False, "order_id": None, "stop_price": stop_px, "error": msg}

    try:
        result = await delta_client.place_stop_order(
            product_id=product_id,
            size=qty,
            side="buy",
            stop_price=stop_px,
        )
        order_id = result.get("order_id")
        if order_id is None:
            msg = f"Delta SL place returned no order_id for {leg_type}"
            logger.warning(msg)
            return {
                "success": False,
                "order_id": None,
                "stop_price": stop_px,
                "error": msg,
            }
        leg.delta_sl_order_id = str(order_id)
        leg.sl_trigger_price = float(stop_px)
        logger.info(
            "Delta SL order placed: leg=%s trigger=%s order_id=%s "
            "baseline=%s pct=%s",
            leg_type,
            stop_px,
            order_id,
            baseline_premium,
            universal_sl_pct,
        )
        return {
            "success": True,
            "order_id": str(order_id),
            "stop_price": float(stop_px),
            "error": None,
        }
    except Exception as exc:
        logger.warning(
            "Delta SL order failed for %s (trade continues): %s",
            leg_type,
            exc,
            exc_info=True,
        )
        return {
            "success": False,
            "order_id": None,
            "stop_price": stop_px,
            "error": str(exc),
        }


async def refresh_leg_sl_order(
    delta_client: Any,
    leg: Any,
    *,
    baseline_premium: float,
    universal_sl_pct: float,
) -> dict[str, Any]:
    """Cancel existing SL (if any) then place a new one from baseline."""
    await cancel_leg_sl_order(delta_client, leg, clear_fields=True)
    return await place_leg_sl_order(
        delta_client,
        leg,
        baseline_premium=baseline_premium,
        universal_sl_pct=universal_sl_pct,
    )


async def place_basket_sl_orders(
    delta_client: Any,
    call_leg: Any,
    put_leg: Any,
    *,
    universal_sl_pct: float,
    call_baseline: float | None = None,
    put_baseline: float | None = None,
) -> dict[str, Any]:
    """Place SL on both legs. Failures are non-fatal."""
    call_base = float(
        call_baseline
        if call_baseline is not None
        else getattr(call_leg, "initial_premium", 0) or 0
    )
    put_base = float(
        put_baseline
        if put_baseline is not None
        else getattr(put_leg, "initial_premium", 0) or 0
    )
    call_res = await place_leg_sl_order(
        delta_client,
        call_leg,
        baseline_premium=call_base,
        universal_sl_pct=universal_sl_pct,
    )
    put_res = await place_leg_sl_order(
        delta_client,
        put_leg,
        baseline_premium=put_base,
        universal_sl_pct=universal_sl_pct,
    )
    return {
        "call": call_res,
        "put": put_res,
        "all_ok": bool(call_res.get("success") and put_res.get("success")),
        "any_failed": bool(
            (not call_res.get("success")) or (not put_res.get("success"))
        ),
    }
