# delta_sl.py — Place / cancel Delta Exchange per-leg stop-loss safety orders

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.core.trade_side import is_long_trade
from backend.schemas import UNIVERSAL_SL_PCT_MIN

logger = logging.getLogger(__name__)

# Master fill vs mark: beyond this relative gap, fall back to mark × uni_sl.
_BRACKET_SL_ANOMALY_RATIO = 0.35
# Short buy-stop must sit at least 10% above mark or Delta rejects / is unsafe.
SHORT_STOP_MIN_MARK_MULT = 1.10
UNSAFE_STOP_ALERT_SECONDS = 300.0
# trade_id → monotonic ts of last unsafe-stop CRITICAL/banner
_unsafe_stop_alert_at: dict[int, float] = {}


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


def extract_bracket_order_id(result: Any) -> str | None:
    """Pull a stop/bracket order id from a Delta place_position_bracket payload."""
    if not isinstance(result, dict):
        return None
    for key in ("id", "order_id"):
        val = result.get(key)
        if val is not None and str(val).strip():
            return str(val)
    for nested_key in ("stop_loss_order", "result", "order", "bracket_order"):
        inner = result.get(nested_key)
        found = extract_bracket_order_id(inner)
        if found:
            return found
    for nested_key in ("orders", "stop_loss_orders"):
        inner = result.get(nested_key)
        if isinstance(inner, list):
            for item in inner:
                found = extract_bracket_order_id(item)
                if found:
                    return found
    return None


def reset_unsafe_stop_alert_throttle() -> None:
    _unsafe_stop_alert_at.clear()


def short_stop_is_safe(stop_px: float, mark: float) -> bool:
    """True when a short-leg buy-stop is ≥ 10% above current mark."""
    stop = float(stop_px or 0.0)
    mk = float(mark or 0.0)
    if stop <= 0 or mk <= 0:
        return False
    return stop >= mk * SHORT_STOP_MIN_MARK_MULT


def unsafe_stop_message(
    trade_id: int | None,
    leg: str,
    stop_px: float,
    mark: float,
) -> str:
    return (
        f"⚠ Trade #{int(trade_id or 0)} leg {leg or '?'}: saved stop "
        f"${float(stop_px):.2f} current mark ${float(mark):.2f} se "
        f"neeche/paas hai — Delta app me manually stop lagao ya Modify se "
        f"SL % badlo."
    )


def _should_emit_unsafe_alert(trade_id: int | None) -> bool:
    tid = int(trade_id or 0)
    now = time.monotonic()
    last = float(_unsafe_stop_alert_at.get(tid) or 0.0)
    if now - last < UNSAFE_STOP_ALERT_SECONDS:
        return False
    _unsafe_stop_alert_at[tid] = now
    return True


async def _alert_unsafe_stop(
    *,
    trade_id: int | None,
    leg: str,
    stop_px: float,
    mark: float,
    reason: str,
) -> None:
    banner = unsafe_stop_message(trade_id, leg, stop_px, mark)
    if not _should_emit_unsafe_alert(trade_id):
        logger.info(
            "[BRACKET_SL] unsafe-stop alert throttled trade=%s leg=%s reason=%s",
            trade_id,
            leg,
            reason,
        )
        return
    logger.critical("%s reason=%s", banner, reason)
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "BRACKET_SL_UNSAFE",
            int(trade_id or 0),
            {
                "leg": leg or "?",
                "stop_price": round(float(stop_px), 4),
                "mark": round(float(mark), 4),
                "reason": reason,
                "summary": banner,
            },
        )
    except Exception:
        pass
    try:
        from backend.core.ws_manager import ws_manager

        await ws_manager.broadcast(
            {
                "type": "ERROR",
                "trade_id": int(trade_id or 0),
                "message": banner,
                "requires_manual_action": True,
                "sticky": True,
                "severity": "CRITICAL",
                "event": "DELTA_SL_UNSAFE",
                "leg": leg or "?",
                "stop_price": round(float(stop_px), 4),
                "mark": round(float(mark), 4),
                "reason": reason,
            }
        )
    except Exception as push_exc:
        logger.error(
            "Failed to broadcast DELTA_SL_UNSAFE: %s", push_exc, exc_info=True
        )


async def _fetch_mark(delta_client: Any, symbol: str | None) -> float:
    if delta_client is None or not symbol:
        return 0.0
    getter = getattr(delta_client, "get_mark_price", None)
    if getter is None:
        return 0.0
    try:
        return float(await getter(str(symbol)) or 0.0)
    except Exception as exc:
        logger.warning("get_mark_price %s failed: %s", symbol, exc)
        return 0.0


def delta_sl_missing_message(trade_id: int | None, leg: str) -> str:
    return (
        f"⚠ DELTA STOP-LOSS NAHI LAGA — Trade #{int(trade_id or 0)}, "
        f"leg {leg or '?'}. Delta app me manually lagao."
    )


def _already_bracketed(exc: BaseException) -> bool:
    err_text = str(exc).lower()
    return any(
        token in err_text
        for token in (
            "already",
            "duplicate",
            "bracket_order_exists",
            "existing_bracket",
        )
    )


async def stop_order_exists_on_delta(
    delta_client: Any,
    *,
    product_id: int,
    order_id: str | None = None,
) -> bool:
    """True when Delta still has a stop/bracket for this product (or order id)."""
    if order_id:
        try:
            row = await delta_client.get_order(order_id)
            if isinstance(row, dict) and row:
                state = str(row.get("state") or "").lower()
                if state not in {
                    "cancelled",
                    "canceled",
                    "rejected",
                    "closed",
                }:
                    return True
        except Exception:
            pass
    try:
        opens = await delta_client.get_open_stop_orders()
    except Exception:
        opens = []
    pid = int(product_id or 0)
    if not isinstance(opens, list):
        return False
    for row in opens:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("product_id") or 0) == pid:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def _broadcast_delta_sl_missing(
    *,
    trade_id: int | None,
    leg: str,
    product_id: int,
    quantity: int | None,
    err_msg: str,
    stop_px: float,
    limit_px: float,
) -> None:
    banner = delta_sl_missing_message(trade_id, leg)
    payload = {
        "leg": leg or "?",
        "product_id": product_id,
        "qty": int(quantity) if quantity is not None else None,
        "stop_price": stop_px,
        "stop_limit_price": limit_px,
        "delta_error": err_msg[:500],
        "summary": banner,
    }
    logger.critical(
        "[BRACKET_SL_FAILED] %s product_id=%s qty=%s err=%s",
        banner,
        product_id,
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
                "message": banner,
                "requires_manual_action": True,
                "sticky": True,
                "severity": "CRITICAL",
                "event": "DELTA_SL_MISSING",
                "leg": leg or "?",
                "product_id": product_id,
                "qty": quantity,
            }
        )
    except Exception as push_exc:
        logger.error(
            "Failed to broadcast DELTA_SL_MISSING: %s", push_exc, exc_info=True
        )


async def attach_position_bracket_sl(
    delta_client: Any,
    *,
    product_id: int,
    stop_price: float,
    stop_limit_price: float,
    leg: str = "",
    trade_id: int | None = None,
    quantity: int | None = None,
    max_attempts: int = 2,
    trade: Any | None = None,
    leg_obj: Any | None = None,
    confirm_wait_seconds: float = 5.0,
    mark_price: float | None = None,
) -> bool:
    """
    Attach exchange bracket SL to the open POSITION (not the entry order).

    After place: require an order id, wait, then confirm the stop exists on
    Delta. One retry if missing. Total failure → CRITICAL + sticky banner.
    Never closes the position.
    """
    if is_long_trade(trade):
        logger.info(
            "[BRACKET_SL] skip long trade_id=%s leg=%s",
            trade_id,
            leg or "?",
        )
        return False

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

    mark = float(mark_price or 0.0)
    if mark <= 0:
        mark = await _fetch_mark(
            delta_client,
            getattr(leg_obj, "symbol", None) if leg_obj is not None else None,
        )
    if mark > 0 and not short_stop_is_safe(stop_px, mark):
        await _alert_unsafe_stop(
            trade_id=trade_id,
            leg=leg or "?",
            stop_px=stop_px,
            mark=mark,
            reason="entry_or_attach_too_close",
        )
        return False

    last_err = "unknown"
    attempts = max(1, int(max_attempts))
    wait_s = max(0.0, float(confirm_wait_seconds))
    for attempt in range(1, attempts + 1):
        oid: str | None = None
        already = False
        try:
            result = await delta_client.place_position_bracket(
                product_id=pid,
                bracket_stop_loss_price=stop_px,
                bracket_stop_loss_limit_price=limit_px,
            )
            oid = extract_bracket_order_id(result)
            logger.info(
                "[BRACKET_SL] position bracket attached leg=%s "
                "product_id=%s stop=%.2f limit=%.2f attempt=%s order_id=%s",
                leg or "?",
                pid,
                stop_px,
                limit_px,
                attempt,
                oid,
            )
        except Exception as exc:
            last_err = str(exc)
            if _already_bracketed(exc):
                already = True
                logger.info(
                    "[BRACKET_SL] position already has bracket leg=%s "
                    "product_id=%s — confirming on Delta (%s)",
                    leg or "?",
                    pid,
                    exc,
                )
            else:
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
                    continue
                break

        if not already and not oid:
            last_err = "no_order_id"
            logger.warning(
                "[BRACKET_SL] no order id after place leg=%s product_id=%s "
                "attempt=%s/%s",
                leg or "?",
                pid,
                attempt,
                attempts,
            )

        if wait_s > 0:
            await asyncio.sleep(wait_s)
        exists = await stop_order_exists_on_delta(
            delta_client, product_id=pid, order_id=oid
        )
        if exists or already:
            if leg_obj is not None:
                if oid:
                    leg_obj.delta_sl_order_id = oid
                leg_obj.delta_sl_confirmed = True
            return True
        last_err = "stop_not_found_on_delta"
        if attempt < attempts:
            logger.warning(
                "[BRACKET_SL] confirm miss leg=%s product_id=%s — retrying",
                leg or "?",
                pid,
            )

    if leg_obj is not None:
        leg_obj.delta_sl_confirmed = False
    await _broadcast_delta_sl_missing(
        trade_id=trade_id,
        leg=leg or "?",
        product_id=pid,
        quantity=quantity,
        err_msg=last_err,
        stop_px=stop_px,
        limit_px=limit_px,
    )
    return False


async def ensure_leg_delta_stop(
    delta_client: Any,
    leg: Any,
    *,
    trade: Any | None = None,
    trade_id: int | None = None,
    confirm_wait_seconds: float = 5.0,
) -> bool:
    """60s watchdog: if Delta stop is gone, re-attach once — never if flat/unsafe."""
    if is_long_trade(trade):
        return False
    if str(getattr(leg, "status", "") or "").lower() != "open":
        return True
    pid = int(getattr(leg, "product_id", 0) or 0)
    stop_px = float(getattr(leg, "sl_trigger_price", 0) or 0)
    if pid <= 0 or stop_px <= 0 or delta_client is None:
        return False
    oid = getattr(leg, "delta_sl_order_id", None)
    exists = await stop_order_exists_on_delta(
        delta_client, product_id=pid, order_id=str(oid) if oid else None
    )
    if exists:
        leg.delta_sl_confirmed = True
        return True

    size_getter = getattr(delta_client, "get_position_size", None)
    if size_getter is not None:
        try:
            size = await size_getter(pid)
        except Exception as exc:
            logger.warning(
                "get_position_size failed pid=%s: %s", pid, exc
            )
            size = None
        if size is not None and abs(float(size)) == 0.0:
            logger.info("position flat, stop not needed")
            return False

    mark = await _fetch_mark(delta_client, getattr(leg, "symbol", None))
    uni_sl = float(getattr(trade, "universal_sl_pct", None) or 0.0)
    if uni_sl > 0 and uni_sl < float(UNIVERSAL_SL_PCT_MIN):
        await _alert_unsafe_stop(
            trade_id=trade_id,
            leg=str(getattr(leg, "leg_type", "") or ""),
            stop_px=stop_px,
            mark=mark,
            reason="legacy_uni_sl_pct",
        )
        return False
    if mark > 0 and not short_stop_is_safe(stop_px, mark):
        await _alert_unsafe_stop(
            trade_id=trade_id,
            leg=str(getattr(leg, "leg_type", "") or ""),
            stop_px=stop_px,
            mark=mark,
            reason="stop_too_close_to_mark",
        )
        return False

    logger.critical(
        "[BRACKET_SL] stop missing on Delta trade=%s leg=%s product_id=%s "
        "— re-attaching",
        trade_id,
        getattr(leg, "leg_type", "?"),
        pid,
    )
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "BRACKET_SL_REPLACED",
            int(trade_id or 0),
            {
                "leg": str(getattr(leg, "leg_type", "?")),
                "product_id": pid,
                "stop_price": stop_px,
                "reason": "missing_on_delta",
            },
        )
    except Exception:
        pass
    limit_px = round(stop_px * 1.05, 2)
    return await attach_position_bracket_sl(
        delta_client,
        product_id=pid,
        stop_price=stop_px,
        stop_limit_price=limit_px,
        leg=str(getattr(leg, "leg_type", "") or ""),
        trade_id=trade_id,
        quantity=int(getattr(leg, "quantity", 0) or 0),
        trade=trade,
        leg_obj=leg,
        confirm_wait_seconds=confirm_wait_seconds,
        mark_price=mark,
    )


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
    trade: Any | None = None,
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
    if is_long_trade(trade):
        logger.info(
            "[BRACKET_SL] skip long trade_id=%s leg=%s",
            trade_id,
            leg or "?",
        )
        return 0.0, 0.0
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
        trade=trade,
        mark_price=float(mark_price or 0.0),
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
