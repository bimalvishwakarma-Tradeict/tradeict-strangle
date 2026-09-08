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


async def attach_position_bracket_sl(
    delta_client: Any,
    *,
    product_id: int,
    stop_price: float,
    stop_limit_price: float,
    leg: str = "",
    trade_id: int | None = None,
    quantity: int | None = None,
    max_attempts: int = 3,
) -> None:
    """
    Attach exchange bracket SL to the open POSITION (not the entry order).

    Mirrors the stop/limit pairing used by OrderExecutor.sell_option, but uses
    POST /v2/orders/bracket so market/IOC fills (parent order already gone)
    still receive an exchange-side stop. Retries with backoff; on total failure
    logs BRACKET_SL_FAILED as CRITICAL and broadcasts to the frontend — never
    closes or alters the position.
    """
    import asyncio

    pid = int(product_id or 0)
    stop_px = round(float(stop_price or 0.0), 2)
    limit_px = round(float(stop_limit_price or 0.0), 2)
    if limit_px <= 0 and stop_px > 0:
        limit_px = round(stop_px * 1.05, 2)
    if delta_client is None or pid <= 0 or stop_px <= 0:
        raise ValueError(
            f"attach_position_bracket_sl invalid args "
            f"product_id={pid} stop={stop_px}"
        )

    last_err: Exception | None = None
    attempts = max(1, int(max_attempts))
    for attempt in range(1, attempts + 1):
        try:
            await delta_client.place_position_bracket(
                product_id=pid,
                bracket_stop_loss_price=stop_px,
                bracket_stop_loss_limit_price=limit_px,
            )
            logger.info(
                "[BRACKET_SL] position bracket attached leg=%s "
                "product_id=%s stop=%.2f limit=%.2f attempt=%s",
                leg or "?",
                pid,
                stop_px,
                limit_px,
                attempt,
            )
            return
        except Exception as exc:
            last_err = exc
            err_text = str(exc).lower()
            # Already bracketed (e.g. inline entry attach succeeded) — OK.
            if any(
                token in err_text
                for token in (
                    "already",
                    "duplicate",
                    "bracket_order_exists",
                    "existing_bracket",
                )
            ):
                logger.info(
                    "[BRACKET_SL] position already has bracket leg=%s "
                    "product_id=%s — treating as success (%s)",
                    leg or "?",
                    pid,
                    exc,
                )
                return
            logger.warning(
                "[BRACKET_SL] position attach failed leg=%s product_id=%s "
                "attempt=%s/%s: %s",
                leg or "?",
                pid,
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

    err_msg = str(last_err) if last_err is not None else "unknown"
    payload = {
        "leg": leg or "?",
        "product_id": pid,
        "qty": int(quantity) if quantity is not None else None,
        "stop_price": stop_px,
        "stop_limit_price": limit_px,
        "delta_error": err_msg[:500],
        "summary": (
            f"[BRACKET_SL_FAILED] CRITICAL leg={leg or '?'} "
            f"product_id={pid} qty={quantity} err={err_msg[:200]}"
        ),
    }
    logger.critical(
        "[BRACKET_SL_FAILED] leg=%s product_id=%s qty=%s err=%s",
        leg or "?",
        pid,
        quantity,
        err_msg,
    )
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer("BRACKET_SL_FAILED", int(trade_id or 0), payload)
    except Exception:
        pass
    try:
        from backend.core.ws_manager import ws_manager

        await ws_manager.broadcast(
            {
                "type": "ERROR",
                "trade_id": int(trade_id or 0),
                "message": (
                    f"Exchange bracket SL FAILED for {leg or 'leg'} "
                    f"(product {pid}): {err_msg[:300]}. "
                    f"Position left open — software stop still active. "
                    f"Place SL manually on Delta if needed."
                ),
                "requires_manual_action": True,
                "severity": "CRITICAL",
                "event": "BRACKET_SL_FAILED",
                "leg": leg or "?",
                "product_id": pid,
                "qty": quantity,
            }
        )
    except Exception as push_exc:
        logger.error(
            "Failed to broadcast BRACKET_SL_FAILED: %s", push_exc, exc_info=True
        )
    # Do not raise — caller must keep the position; software SL continues.


async def finalize_bracket_sl_after_fill(
    delta_client: Any,
    *,
    entry_order_id: str | int | None,
    product_id: int,
    mark_price: float,
    fill_price: float,
    universal_sl_pct: float,
    provisional_stop: float,
    provisional_limit: float,
    leg: str = "",
    trade_id: int | None = None,
    quantity: int | None = None,
) -> tuple[float, float]:
    """
    After a short-leg fill, ensure exchange bracket SL is on the POSITION.

    Chicken-and-egg: callers may ship mark × uni_sl on the entry order
    (provisional_*). Once fill is known we compute fill-derived prices and
    attach via POST /v2/orders/bracket (same stop/limit pairing as
    OrderExecutor.sell_option). We never PUT-amend the entry order id —
    market/IOC parents are already gone (open_order_not_found).

    entry_order_id is retained for call-site compatibility only (unused).
    """
    _ = entry_order_id  # legacy kw; position attach does not need it
    fill_stop, fill_limit = compute_bracket_sl(
        float(fill_price or 0.0),
        float(universal_sl_pct or 200.0),
        master_mark=float(mark_price or 0.0),
        leg=leg,
        trade_id=trade_id,
    )
    prov_stop = float(provisional_stop or 0.0)
    prov_limit = float(provisional_limit or 0.0)

    stop_px = fill_stop if fill_stop > 0 else prov_stop
    limit_px = fill_limit if fill_limit > 0 else prov_limit
    if stop_px <= 0:
        return 0.0, 0.0
    if limit_px <= 0:
        limit_px = round(stop_px * 1.05, 2)

    if int(product_id or 0) <= 0 or delta_client is None:
        logger.critical(
            "[BRACKET_SL_FAILED] cannot attach leg=%s — missing client/product",
            leg,
        )
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "BRACKET_SL_FAILED",
                int(trade_id or 0),
                {
                    "leg": leg or "?",
                    "product_id": int(product_id or 0),
                    "qty": quantity,
                    "delta_error": "missing_client_or_product_id",
                    "summary": (
                        f"[BRACKET_SL_FAILED] CRITICAL leg={leg or '?'} "
                        "missing client/product_id"
                    ),
                },
            )
        except Exception:
            pass
        return stop_px, limit_px

    await attach_position_bracket_sl(
        delta_client,
        product_id=int(product_id),
        stop_price=stop_px,
        stop_limit_price=limit_px,
        leg=leg,
        trade_id=trade_id,
        quantity=quantity,
    )
    return stop_px, limit_px


async def cancel_leg_sl_order(
    delta_client: Any,
    leg: Any,
    *,
    clear_fields: bool = True,
) -> bool:
    """Cancel legacy standalone Delta SL order for a leg (brackets have none)."""
    oid = getattr(leg, "delta_sl_order_id", None)
    if not oid:
        return True
    # ABS: audit tags from a prior regression — not real order ids
    if str(oid).startswith("ABS:"):
        if clear_fields:
            leg.delta_sl_order_id = None
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
    Legacy hook — standalone stop orders are FORBIDDEN.

    Bracket SL must be attached on the entry order. This never calls
    place_stop_order. If the leg already has a bracket (sl_trigger_price set,
    no delta_sl_order_id), returns success for display refresh only.
    """
    stop_px = compute_sl_trigger_price(baseline_premium, universal_sl_pct)
    leg_type = str(getattr(leg, "leg_type", "?") or "?")

    if (
        getattr(leg, "sl_trigger_price", None) is not None
        and not getattr(leg, "delta_sl_order_id", None)
    ):
        return {
            "success": True,
            "order_id": None,
            "stop_price": float(getattr(leg, "sl_trigger_price") or stop_px),
            "error": None,
        }

    msg = (
        f"place_leg_sl_order refused for {leg_type}: "
        "use bracket SL on the entry order (no standalone stops)"
    )
    logger.warning(msg)
    if stop_px > 0:
        leg.sl_trigger_price = float(stop_px)
    leg.delta_sl_order_id = None
    return {
        "success": False,
        "order_id": None,
        "stop_price": stop_px,
        "error": msg,
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
