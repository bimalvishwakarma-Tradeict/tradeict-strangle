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
) -> tuple[float, float]:
    """
    After an entry fill, prefer fill-derived bracket SL; amend if needed.

    Chicken-and-egg: bracket must ship WITH the opening order before any fill
    exists, so callers attach mark × uni_sl at place time (provisional_*).
    Once the fill is known we compute fill-derived via compute_bracket_sl and
    try PUT /v2/orders/bracket to amend. IOC parents are often already filled
    and not editable — on amend failure we KEEP the mark-derived provisional
    as the canonical absolute price for master AND slaves so they still match.
    """
    fill_stop, fill_limit = compute_bracket_sl(
        float(fill_price or 0.0),
        float(universal_sl_pct or 200.0),
        master_mark=float(mark_price or 0.0),
        leg=leg,
        trade_id=trade_id,
    )
    prov_stop = float(provisional_stop or 0.0)
    prov_limit = float(provisional_limit or 0.0)
    if fill_stop <= 0:
        return prov_stop, prov_limit
    if prov_stop <= 0 or abs(fill_stop - prov_stop) < 0.01:
        return fill_stop, fill_limit

    if entry_order_id is None or int(product_id or 0) <= 0 or delta_client is None:
        logger.warning(
            "[BRACKET_SL] cannot amend leg=%s — keeping mark-derived %.2f",
            leg,
            prov_stop,
        )
        return prov_stop, prov_limit

    try:
        await delta_client.edit_bracket_order(
            order_id=entry_order_id,
            product_id=int(product_id),
            bracket_stop_loss_price=fill_stop,
            bracket_stop_loss_limit_price=fill_limit,
        )
        logger.info(
            "[BRACKET_SL] amended leg=%s order=%s mark_stop=%.2f → fill_stop=%.2f",
            leg,
            entry_order_id,
            prov_stop,
            fill_stop,
        )
        return fill_stop, fill_limit
    except Exception as amend_exc:
        logger.warning(
            "[BRACKET_SL] amend failed leg=%s order=%s (%s) — "
            "keeping mark-derived %.2f as canonical for master+slaves",
            leg,
            entry_order_id,
            amend_exc,
            prov_stop,
        )
        return prov_stop, prov_limit


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
