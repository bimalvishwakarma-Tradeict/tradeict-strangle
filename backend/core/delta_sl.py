# delta_sl.py — Place / cancel Delta Exchange per-leg stop-loss safety orders

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Master fill vs mark: beyond this relative gap, fall back to mark × uni_sl.
_BRACKET_SL_ANOMALY_RATIO = 0.35


def compute_bracket_sl(
    master_fill_price: float,
    universal_sl_pct: float,
    *,
    master_mark: float | None = None,
    leg: str = "",
    trade_id: int | None = None,
) -> tuple[float, float]:
    """
    Canonical exchange bracket SL from the MASTER'S actual fill.

    stop = master_fill × (universal_sl_pct / 100)
    limit = stop × 1.05

    If |fill − mark| / mark > 35%, log CRITICAL and fall back to mark × uni_sl
    so one bad fill cannot push a wrong stop onto every slave.

    Returns (stop_price, stop_limit_price). Either may be 0.0 when invalid.
    """
    fill = float(master_fill_price or 0.0)
    mark = float(master_mark or 0.0) if master_mark is not None else 0.0
    pct = float(universal_sl_pct or 200.0)
    use_price = fill
    anomaly = False

    if fill > 0 and mark > 0 and abs(fill - mark) / mark > _BRACKET_SL_ANOMALY_RATIO:
        anomaly = True
        use_price = mark
        msg = (
            f"[BRACKET_SL_ANOMALY] trade_id={trade_id} leg={leg} "
            f"master_fill={fill:.4f} master_mark={mark:.4f} "
            f"ratio={abs(fill - mark) / mark:.3f} — falling back to mark × "
            f"{pct:.1f}%"
        )
        logger.critical(msg)
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "BRACKET_SL_ANOMALY",
                int(trade_id or 0),
                {
                    "leg": leg,
                    "master_fill": round(fill, 4),
                    "master_mark": round(mark, 4),
                    "uni_sl_pct": pct,
                    "fallback": "mark",
                },
            )
        except Exception:
            pass

    if use_price <= 0 or pct <= 0:
        return 0.0, 0.0

    stop = round(use_price * (pct / 100.0), 2)
    limit = round(stop * 1.05, 2) if stop > 0 else 0.0

    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "BRACKET_SL",
            int(trade_id or 0),
            {
                "leg": leg or "?",
                "master_fill": round(fill, 4),
                "master_mark": round(mark, 4) if mark > 0 else None,
                "uni_sl_pct": pct,
                "stop_price": stop,
                "stop_limit_price": limit,
                "anomaly_fallback": anomaly,
            },
        )
    except Exception:
        pass
    logger.info(
        "[BRACKET_SL] leg=%s master_fill=%.4f uni_sl_pct=%.1f "
        "stop_price=%.2f stop_limit_price=%.2f",
        leg or "?",
        fill,
        pct,
        stop,
        limit,
    )
    return stop, limit


def compute_sl_trigger_price(baseline_premium: float, universal_sl_pct: float) -> float:
    """SL trigger = baseline × (universal_sl_pct / 100). Delegates to compute_bracket_sl."""
    stop, _limit = compute_bracket_sl(
        float(baseline_premium or 0.0),
        float(universal_sl_pct or 200.0),
    )
    return stop


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
