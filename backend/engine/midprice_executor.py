# midprice_executor.py — Mid-price chase + urgent ladder (double-fill guarded)
#
# Profiles:
#   chase  — first leg of each entry group; post-only mid, refresh until fill
#            or midprice_chase_max_seconds, then MARKET (never abort)
#   urgent — second leg; mid → best → best → market (max ~9s)
#
# DOUBLE-FILL GUARD (non-negotiable):
#   Always read Delta order status after every hold. Never place a new order
#   until the previous is confirmed cancelled or treated as filled.
#   Cancel "already filled" → treat as FILL and STOP.

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaAPIError
from backend.strategies.base_strategy import OrderResult

logger = logging.getLogger(__name__)

HOLD_SECONDS = 5.0
POST_CANCEL_SETTLE_SECONDS = 2.0
DEFAULT_CHASE_MAX_SECONDS = 120
CHASE_MAX_SECONDS_MIN = 10
CHASE_MAX_SECONDS_MAX = 600

# Allow-list ONLY — unknown reasons stay market (safe default for new exits)
MIDPRICE_ALLOWED_REASONS = frozenset(
    {
        "HEDGE_ENTRY",
        "BASKET_ENTRY",
        "CONDOR_ENTRY",
        "HEDGE_TARGET",
        "HEDGE_ROLL",
        "HEDGE_MANUAL",
    }
)

_FILLED_STATES = frozenset(
    {"filled", "closed", "complete", "completed", "done"}
)
_OPEN_STATES = frozenset(
    {"open", "pending", "accepted", "untriggered", "partially_filled"}
)
_IN_FLIGHT_ORDER_COUNT = 0
_IN_FLIGHT_ORDER_LOCK = asyncio.Lock()


def should_use_midprice(*, enabled: bool, reason: str) -> bool:
    """Allow-list gate — deny by default for any new/unknown reason."""
    if not bool(enabled):
        return False
    return str(reason or "").upper().strip() in MIDPRICE_ALLOWED_REASONS


def is_placing_order() -> bool:
    """True when a mid-price execution flow currently has orders in flight."""
    return _IN_FLIGHT_ORDER_COUNT > 0


def _safe_dump(obj: Any) -> Any:
    """JSON-ish dump of a Delta payload for audit logs."""
    if isinstance(obj, dict):
        return {str(k): _safe_dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_dump(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _mid_log(
    event_type: str,
    trade_id: int | None,
    details: dict[str, Any],
) -> None:
    log_and_buffer(event_type, int(trade_id or 0), details)


@asynccontextmanager
async def order_placement_guard(
    *,
    trade_id: int | None = None,
    leg_label: str = "",
    phase: str = "",
) -> Any:
    """Reference-counted in-flight flag for reconciliation clash guard."""
    global _IN_FLIGHT_ORDER_COUNT
    async with _IN_FLIGHT_ORDER_LOCK:
        _IN_FLIGHT_ORDER_COUNT += 1
        log_and_buffer(
            "ORDER_PLACEMENT_STATE",
            trade_id or 0,
            {
                "leg": leg_label,
                "phase": phase,
                "is_placing_order": True,
                "in_flight_count": _IN_FLIGHT_ORDER_COUNT,
            },
        )
    try:
        yield
    finally:
        async with _IN_FLIGHT_ORDER_LOCK:
            _IN_FLIGHT_ORDER_COUNT = max(0, _IN_FLIGHT_ORDER_COUNT - 1)
            log_and_buffer(
                "ORDER_PLACEMENT_STATE",
                trade_id or 0,
                {
                    "leg": leg_label,
                    "phase": phase,
                    "is_placing_order": _IN_FLIGHT_ORDER_COUNT > 0,
                    "in_flight_count": _IN_FLIGHT_ORDER_COUNT,
                },
            )


def profile_for_group_leg(leg_index_in_group: int) -> str:
    """Within a 2-leg group: first = chase, second = urgent."""
    return "chase" if int(leg_index_in_group) % 2 == 0 else "urgent"


def profiles_for_paired_sequence(n_legs: int) -> list[str]:
    """hedge/wings/shorts as pairs → [chase, urgent, chase, urgent, ...]."""
    return [profile_for_group_leg(i) for i in range(max(0, int(n_legs)))]


def clamp_chase_max_seconds(value: int | None) -> int:
    raw = (
        DEFAULT_CHASE_MAX_SECONDS
        if value is None
        else int(value)
    )
    return max(CHASE_MAX_SECONDS_MIN, min(CHASE_MAX_SECONDS_MAX, raw))


def compute_mid(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (float(bid) + float(ask)) / 2.0


def best_price_for_side(side: str, bid: float, ask: float) -> float | None:
    """BUY → ask (crosses), SELL → bid (crosses). Never post-only."""
    s = str(side).lower()
    if s == "buy":
        return float(ask) if ask > 0 else None
    if s == "sell":
        return float(bid) if bid > 0 else None
    return None


def market_would_be_price(side: str, bid: float, ask: float) -> float | None:
    return best_price_for_side(side, bid, ask)


def _order_state(order: dict[str, Any] | None) -> str:
    if not order:
        return ""
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    state = (
        order.get("state")
        or order.get("status")
        or raw.get("state")
        or raw.get("status")
        or ""
    )
    return str(state).lower().strip()


def _filled_size_from_order(
    order: dict[str, Any] | None, *, requested: int
) -> int:
    if not order:
        return 0
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    for src in (order, raw):
        for key in ("filled_size", "size_filled"):
            try:
                val = src.get(key)
                if val is not None:
                    return max(0, int(float(val)))
            except (TypeError, ValueError):
                continue
        try:
            unfilled = src.get("unfilled_size")
            size = src.get("size")
            if unfilled is not None and size is not None:
                return max(0, int(float(size)) - int(float(unfilled)))
        except (TypeError, ValueError):
            pass
    state = _order_state(order)
    if state in _FILLED_STATES:
        return max(0, int(requested))
    return 0


def _avg_fill_from_order(order: dict[str, Any] | None) -> float:
    if not order:
        return 0.0
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    for src in (order, raw):
        for key in (
            "average_fill_price",
            "avg_fill_price",
            "fill_price",
        ):
            try:
                val = float(src.get(key) or 0)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                continue
    return 0.0


def is_post_only_reject(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "post_only",
        "post-only",
        "post only",
        "would take",
        "crosses the book",
        "immediate execution",
        "maker",
    )
    return any(n in msg for n in needles)


def is_already_filled_cancel_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "already filled",
        "already_filled",
        "order filled",
        "filled order",
        "cannot cancel",
        "not open",
        "order not found",  # sometimes deleted after fill
    )
    return any(n in msg for n in needles)


async def _fetch_book(
    delta_client: Any, symbol: str
) -> tuple[float, float]:
    bid, ask = await delta_client.get_l2_top_of_book(str(symbol))
    return float(bid or 0), float(ask or 0)


async def _poll_order_until_hold(
    delta_client: Any,
    order_id: int | str,
    *,
    hold_seconds: float = HOLD_SECONDS,
    poll_every: float = 0.5,
    trade_id: int | None = None,
    leg_label: str = "",
) -> dict[str, Any]:
    """Hold up to hold_seconds, polling Delta order status (source of truth)."""
    deadline = time.monotonic() + float(hold_seconds)
    last: dict[str, Any] = {}
    while True:
        try:
            last = await delta_client.get_order(order_id)
        except Exception as exc:
            _mid_log(
                "MIDPRICE_ATTEMPT",
                trade_id,
                {
                    "phase": "get_order_failed",
                    "order_id": order_id,
                    "leg": leg_label,
                    "error": str(exc),
                },
            )
            last = last or {}
        state = _order_state(last)
        if state in _FILLED_STATES:
            return last
        # partially_filled — keep waiting until hold ends
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last
        await asyncio.sleep(min(float(poll_every), remaining))


async def _cancel_confirm(
    delta_client: Any,
    order_id: int | str,
    *,
    trade_id: int | None = None,
    leg_label: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """
    Cancel and verify. Returns (outcome, order_after):
      cancelled | filled | unknown
    """
    try:
        await delta_client.cancel_order(int(order_id))
    except Exception as exc:
        if is_already_filled_cancel_error(exc):
            # Re-read to confirm
            try:
                od = await delta_client.get_order(order_id)
            except Exception:
                od = None
            _mid_log(
                "ORDER_RESTING_CLEARED",
                trade_id,
                {
                    "order_id": order_id,
                    "leg": leg_label,
                    "outcome": "filled",
                    "note": "cancel_already_filled",
                },
            )
            return "filled", od
        _mid_log(
            "MIDPRICE_ATTEMPT",
            trade_id,
            {
                "phase": "cancel_failed",
                "order_id": order_id,
                "leg": leg_label,
                "error": str(exc),
                "action": "recheck_status",
            },
        )

    try:
        od = await delta_client.get_order(order_id)
    except Exception:
        _mid_log(
            "ORDER_RESTING_CLEARED",
            trade_id,
            {
                "order_id": order_id,
                "leg": leg_label,
                "outcome": "unknown",
            },
        )
        return "unknown", None

    state = _order_state(od)
    if state in _FILLED_STATES:
        outcome = "filled"
    else:
        filled = _filled_size_from_order(od, requested=0)
        if filled > 0 and state not in _OPEN_STATES:
            outcome = "filled"
        else:
            outcome = "cancelled"
    _mid_log(
        "ORDER_RESTING_CLEARED",
        trade_id,
        {
            "order_id": order_id,
            "leg": leg_label,
            "outcome": outcome,
            "state": state,
        },
    )
    return outcome, od


async def _place_market(
    delta_client: Any,
    *,
    product_id: int,
    side: str,
    quantity: int,
    reduce_only: bool,
    bracket_sl_price: float | None = None,
    bracket_sl_limit: float | None = None,
    trade_id: int | None = None,
    leg_label: str = "",
) -> OrderResult:
    try:
        raw = await delta_client.place_order(
            product_id=int(product_id),
            size=int(quantity),
            side=str(side),
            order_type="market_order",
            time_in_force="ioc",
            reduce_only=bool(reduce_only),
            bracket_stop_loss_price=bracket_sl_price,
            bracket_stop_loss_limit_price=bracket_sl_limit,
        )
        fill_px = float(
            await delta_client.resolve_fill_price(raw) or 0.0
        )
        oid = raw.get("order_id") or raw.get("id")
        filled = int(raw.get("size") or quantity)
        try:
            r = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
            if r.get("filled_size") is not None:
                filled = int(float(r["filled_size"]))
        except (TypeError, ValueError):
            pass
        _mid_log(
            "ORDER_RESTING",
            trade_id,
            {
                "order_id": oid,
                "leg": leg_label,
                "product_id": int(product_id),
                "side": str(side),
                "size": int(quantity),
                "order_type": "market_order",
                "time_in_force": "ioc",
                "limit_price": None,
                "post_only": False,
                "reduce_only": bool(reduce_only),
            },
        )
        return OrderResult(
            success=True,
            order_id=int(oid) if oid is not None else None,
            filled_price=fill_px if fill_px > 0 else None,
            filled_size=filled,
            fill_type="market",
        )
    except Exception as exc:
        return OrderResult(success=False, error=str(exc), fill_type="market")


async def get_live_position_size(
    delta_client: Any, product_id: int
) -> float:
    try:
        positions = await delta_client.get_option_positions()
    except Exception:
        return 0.0
    pid = int(product_id)
    for p in positions or []:
        try:
            if int(p.get("product_id") or 0) == pid:
                return float(p.get("size") or 0)
        except (TypeError, ValueError):
            continue
    return 0.0


def log_size_mismatch(
    *,
    leg_label: str,
    intended: int,
    actual: float,
    attempts: int,
    trade_id: int | None = None,
) -> None:
    _mid_log(
        "MIDPRICE_SIZE_MISMATCH",
        trade_id,
        {
            "leg": leg_label,
            "intended": intended,
            "actual": actual,
            "attempts": attempts,
        },
    )


def log_entry_drift(
    *,
    leg_label: str,
    selected_premium: float,
    fill_premium: float,
    seconds_since_selection: float,
    tolerance_pct: float,
) -> float:
    """Returns drift_pct. Logs WARNING if above tolerance — never blocks."""
    sel = float(selected_premium or 0)
    fill = float(fill_premium or 0)
    if sel <= 0 or fill <= 0:
        drift = 0.0
    else:
        drift = abs(fill - sel) / sel * 100.0
    logger.info(
        "[ENTRY_DRIFT] leg=%s selected_premium=%s fill_premium=%s "
        "drift_pct=%.2f seconds_since_selection=%.1f",
        leg_label,
        round(sel, 4),
        round(fill, 4),
        drift,
        seconds_since_selection,
    )
    if drift > float(tolerance_pct):
        logger.warning(
            "[ENTRY_DRIFT_HIGH] leg=%s selected_premium=%s fill_premium=%s "
            "drift_pct=%.2f tolerance_pct=%.2f seconds_since_selection=%.1f",
            leg_label,
            round(sel, 4),
            round(fill, 4),
            drift,
            float(tolerance_pct),
            seconds_since_selection,
        )
    return drift


def _fire_exec_event(
    *,
    trade_id: int | None,
    hedge_position_id: int | None,
    phase: str,
    leg: str,
    side: str,
    qty: int,
    attempt: int,
    profile: str,
    order_kind: str,
    bid: float,
    ask: float,
    mid: float | None,
    limit: float | None,
    status: str,
    fill_price: float | None = None,
) -> None:
    """Fire-and-forget: schedule broadcast on the running event loop.
    Never raises — errors are swallowed."""
    from backend.core.ws_manager import ws_manager
    from backend.core.time_utils import get_utc_now

    if trade_id is None:
        return  # no context — skip silently

    try:
        payload = {
            "type": "EXEC_EVENT",
            "ts": get_utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trade_id": trade_id,
            "hedge_position_id": hedge_position_id,
            "phase": phase,
            "leg": leg,
            "side": side,
            "qty": qty,
            "attempt": attempt,
            "profile": profile,
            "order_kind": order_kind,
            "bid": round(float(bid or 0), 2),
            "ask": round(float(ask or 0), 2),
            "mid": round(float(mid), 2) if mid else None,
            "limit": round(float(limit), 2) if limit else None,
            "status": status,
            "fill_price": (
                round(float(fill_price), 2) if fill_price else None
            ),
        }
        asyncio.get_event_loop().create_task(ws_manager.broadcast(payload))
    except Exception:
        pass  # never disrupt order flow


async def _pre_place_position_check(
    delta_client: Any,
    *,
    product_id: int,
    qty_intended: int,
    leg_label: str,
    attempt: int,
    reduce_only: bool,
    trade_id: int | None,
    check_position_size: bool = True,
) -> bool:
    """Check live position BEFORE placing a new order.

    Returns True if position is already filled (skip order).
    For reduce_only (exits) or when check_position_size is False, this
    check is skipped — only applies to opens.
    Logs via log_and_buffer. Never raises.
    """
    if reduce_only or not check_position_size:
        return False
    try:
        actual = abs(await get_live_position_size(delta_client, product_id))
        if actual >= float(qty_intended):
            log_and_buffer(
                "PREPLACE_SKIP",
                trade_id or 0,
                {
                    "leg": leg_label,
                    "attempt": attempt,
                    "intended": qty_intended,
                    "actual_position": actual,
                    "reason": "position_already_filled",
                },
            )
            return True
        log_and_buffer(
            "PREPLACE_CHECK",
            trade_id or 0,
            {
                "leg": leg_label,
                "attempt": attempt,
                "intended": qty_intended,
                "actual_position": actual,
            },
        )
    except Exception as exc:
        _mid_log(
            "PREPLACE_CHECK",
            trade_id,
            {
                "leg": leg_label,
                "attempt": attempt,
                "error": str(exc),
                "phase": "failed",
            },
        )
    return False


async def execute_with_midprice(
    *,
    product_id: int,
    side: str,
    quantity: int,
    profile: str,  # "chase" | "urgent"
    delta_client: Any,
    reason: str,
    leg_label: str,
    symbol: str,
    max_chase_seconds: int | None = None,
    reduce_only: bool = False,
    midprice_enabled: bool = True,
    bracket_sl_price: float | None = None,
    bracket_sl_limit: float | None = None,
    selected_premium: float | None = None,
    selection_ts: float | None = None,
    entry_premium_match_tolerance_pct: float = 15.0,
    hold_seconds: float = HOLD_SECONDS,
    sleep_fn: Any = None,
    monotonic_fn: Any = None,
    check_position_size: bool = True,
    trade_id: int | None = None,
    hedge_position_id: int | None = None,
    phase: str = "",
    leg_type: str = "",
    partner_filled_event: asyncio.Event | None = None,
) -> OrderResult:
    """
    Place one leg via chase or urgent ladder. Falls back to market when
    midprice is disabled or reason is not allow-listed.
    """
    async with order_placement_guard(
        trade_id=trade_id,
        leg_label=leg_label,
        phase=phase or leg_type,
    ):
        sleep = sleep_fn or asyncio.sleep
        now = monotonic_fn or time.monotonic
        qty_total = max(1, abs(int(quantity)))
        side_l = str(side).lower().strip()
        prof = str(profile or "urgent").lower().strip()
        if prof not in {"chase", "urgent"}:
            prof = "urgent"

        if not should_use_midprice(enabled=midprice_enabled, reason=reason):
            res = await _place_market(
                delta_client,
                product_id=product_id,
                side=side_l,
                quantity=qty_total,
                reduce_only=reduce_only,
                bracket_sl_price=bracket_sl_price,
                bracket_sl_limit=bracket_sl_limit,
                trade_id=trade_id,
                leg_label=leg_label,
            )
            res.fill_type = "market"
            return res

        chase_max = clamp_chase_max_seconds(max_chase_seconds)
        started = now()
        remaining = qty_total
        filled_total = 0
        fill_price_sum = 0.0
        last_oid: int | None = None
        attempt = 0
        mid_at_start: float | None = None
        fill_type_used = "market"
        urgent_types = ("mid", "best", "best", "market")
        partner_mode_started_at: float | None = None
        partner_final_mid_attempt_armed = False
        partner_market_fallback_done = False
        last_order_was_partner_final_attempt = False

        while remaining > 0:
            elapsed = now() - started
            attempt += 1
            last_order_was_partner_final_attempt = False

            if (
                partner_filled_event is not None
                and partner_mode_started_at is None
                and partner_filled_event.is_set()
            ):
                partner_mode_started_at = float(now())
                log_and_buffer(
                    "PARTNER_SIGNAL_RECEIVED",
                    trade_id or 0,
                    {"leg": leg_label, "attempt": attempt},
                )
                partner_final_mid_attempt_armed = False

            if (
                partner_mode_started_at is not None
                and not partner_final_mid_attempt_armed
                and (now() - partner_mode_started_at) >= float(HOLD_SECONDS)
            ):
                partner_final_mid_attempt_armed = True
                log_and_buffer(
                    "PARTNER_FINAL_ATTEMPT_ARMED",
                    trade_id or 0,
                    {
                        "leg": leg_label,
                        "attempt": attempt,
                        "window_elapsed": round(
                            float(now() - partner_mode_started_at),
                            3,
                        ),
                    },
                )

            if (
                prof == "chase"
                and elapsed >= chase_max
                and partner_mode_started_at is None
            ):
                _mid_log(
                    "MIDPRICE_CHASE_TIMEOUT",
                    trade_id,
                    {
                        "leg": leg_label,
                        "elapsed": round(float(elapsed), 1),
                        "attempts": attempt,
                        "action": "market",
                    },
                )
                order_kind = "market"
                if await _pre_place_position_check(
                    delta_client,
                    product_id=product_id,
                    qty_intended=qty_total,
                    leg_label=leg_label,
                    attempt=attempt,
                    reduce_only=reduce_only,
                    trade_id=trade_id,
                    check_position_size=check_position_size,
                ):
                    remaining = 0
                    filled_total = qty_total
                    fill_type_used = "position_already_filled"
                    break
                _fire_exec_event(
                    trade_id=trade_id,
                    hedge_position_id=hedge_position_id,
                    phase=phase,
                    leg=leg_label,
                    side=side_l,
                    qty=remaining,
                    attempt=attempt,
                    profile=prof,
                    order_kind=order_kind,
                    bid=0.0,
                    ask=0.0,
                    mid=None,
                    limit=None,
                    status="timeout_market",
                    fill_price=None,
                )
                mkt = await _place_market(
                    delta_client,
                    product_id=product_id,
                    side=side_l,
                    quantity=remaining,
                    reduce_only=reduce_only,
                    bracket_sl_price=bracket_sl_price,
                    bracket_sl_limit=bracket_sl_limit,
                    trade_id=trade_id,
                    leg_label=leg_label,
                )
                if mkt.success:
                    fsz = int(mkt.filled_size or remaining)
                    filled_total += fsz
                    if mkt.filled_price:
                        fill_price_sum += float(mkt.filled_price) * fsz
                    last_oid = mkt.order_id
                    fill_type_used = "market"
                    remaining = 0
                else:
                    return OrderResult(
                        success=False,
                        error=mkt.error or "chase_timeout_market_failed",
                        order_id=last_oid,
                        filled_size=filled_total if filled_total else None,
                        fill_attempt=attempt,
                        fill_type="market",
                        mid_at_start=mid_at_start,
                    )
                break

            if prof == "urgent" and attempt > len(urgent_types):
                break

            try:
                bid, ask = await _fetch_book(delta_client, symbol)
            except Exception as exc:
                _mid_log(
                    "MIDPRICE_ATTEMPT",
                    trade_id,
                    {
                        "phase": "book_fetch_failed",
                        "leg": leg_label,
                        "error": str(exc),
                    },
                )
                bid, ask = 0.0, 0.0

            mid = compute_mid(bid, ask)
            if mid_at_start is None and mid is not None:
                mid_at_start = mid
            mkt_ref = market_would_be_price(side_l, bid, ask)

            if prof == "chase":
                order_kind = "mid"
            else:
                idx = min(attempt - 1, len(urgent_types) - 1)
                order_kind = urgent_types[idx]

            if order_kind == "market":
                if await _pre_place_position_check(
                    delta_client,
                    product_id=product_id,
                    qty_intended=qty_total,
                    leg_label=leg_label,
                    attempt=attempt,
                    reduce_only=reduce_only,
                    trade_id=trade_id,
                    check_position_size=check_position_size,
                ):
                    remaining = 0
                    filled_total = qty_total
                    fill_type_used = "position_already_filled"
                    break
                _mid_log(
                    "MIDPRICE_ATTEMPT",
                    trade_id,
                    {
                        "leg": leg_label,
                        "profile": prof,
                        "attempt": attempt,
                        "type": "market",
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "limit": None,
                        "qty": remaining,
                        "filled": 0,
                    },
                )
                mkt = await _place_market(
                    delta_client,
                    product_id=product_id,
                    side=side_l,
                    quantity=remaining,
                    reduce_only=reduce_only,
                    bracket_sl_price=bracket_sl_price,
                    bracket_sl_limit=bracket_sl_limit,
                    trade_id=trade_id,
                    leg_label=leg_label,
                )
                if mkt.success:
                    fsz = int(mkt.filled_size or remaining)
                    filled_total += fsz
                    if mkt.filled_price:
                        fill_price_sum += float(mkt.filled_price) * fsz
                    last_oid = mkt.order_id
                    fill_type_used = "market"
                    remaining = 0
                else:
                    return OrderResult(
                        success=False,
                        error=mkt.error or "urgent_market_failed",
                        order_id=last_oid,
                        filled_size=filled_total if filled_total else None,
                        fill_attempt=attempt,
                        fill_type="market",
                        mid_at_start=mid_at_start,
                    )
                break

            use_post_only = order_kind == "mid"
            if order_kind == "mid":
                limit = mid
            else:
                limit = best_price_for_side(side_l, bid, ask)

            if limit is None or limit <= 0:
                if prof == "urgent" and attempt >= 3:
                    order_kind = "market"
                    continue
                await sleep(0)
                continue

            _mid_log(
                "MIDPRICE_ATTEMPT",
                trade_id,
                {
                    "leg": leg_label,
                    "profile": prof,
                    "attempt": attempt,
                    "type": order_kind,
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "limit": round(float(limit), 4),
                    "qty": remaining,
                    "filled": 0,
                },
            )
            if prof == "chase" and attempt % 5 == 0:
                _mid_log(
                    "MIDPRICE_CHASE",
                    trade_id,
                    {
                        "leg": leg_label,
                        "attempt": attempt,
                        "elapsed": round(float(elapsed), 1),
                        "mid": mid,
                        "bid": bid,
                        "ask": ask,
                    },
                )

            if await _pre_place_position_check(
                delta_client,
                product_id=product_id,
                qty_intended=qty_total,
                leg_label=leg_label,
                attempt=attempt,
                reduce_only=reduce_only,
                trade_id=trade_id,
                check_position_size=check_position_size,
            ):
                remaining = 0
                filled_total = qty_total
                fill_type_used = "position_already_filled"
                break

            last_order_was_partner_final_attempt = (
                partner_final_mid_attempt_armed is True
            )
            _fire_exec_event(
                trade_id=trade_id,
                hedge_position_id=hedge_position_id,
                phase=phase,
                leg=leg_label,
                side=side_l,
                qty=remaining,
                attempt=attempt,
                profile=prof,
                order_kind=order_kind,
                bid=bid,
                ask=ask,
                mid=mid,
                limit=limit,
                status="pending",
                fill_price=None,
            )
            try:
                placed = await delta_client.place_order(
                    product_id=int(product_id),
                    size=int(remaining),
                    side=side_l,
                    order_type="limit_order",
                    time_in_force="gtc",
                    limit_price=float(limit),
                    post_only=bool(use_post_only),
                    reduce_only=bool(reduce_only),
                    bracket_stop_loss_price=bracket_sl_price,
                    bracket_stop_loss_limit_price=bracket_sl_limit,
                )
            except Exception as exc:
                if use_post_only and is_post_only_reject(exc):
                    _mid_log(
                        "MIDPRICE_POSTONLY_REJECT",
                        trade_id,
                        {
                            "leg": leg_label,
                            "attempt": attempt,
                            "mid": mid,
                            "bid": bid,
                            "ask": ask,
                        },
                    )
                    continue
                _mid_log(
                    "MIDPRICE_ATTEMPT",
                    trade_id,
                    {
                        "phase": "place_failed",
                        "leg": leg_label,
                        "error": str(exc),
                    },
                )
                if prof == "urgent" and attempt >= 3:
                    continue
                await sleep(0.2)
                continue

            oid = placed.get("order_id") or placed.get("id")
            if oid is None:
                _mid_log(
                    "ORDER_ID_LOST",
                    trade_id,
                    {
                        "leg": leg_label,
                        "product_id": int(product_id),
                        "side": side_l,
                        "size": remaining,
                        "order_type": "limit_order",
                        "time_in_force": "gtc",
                        "limit_price": float(limit),
                        "post_only": bool(use_post_only),
                        "reduce_only": bool(reduce_only),
                        "delta_response": _safe_dump(placed),
                    },
                )
                continue
            last_oid = int(oid)
            _mid_log(
                "ORDER_RESTING",
                trade_id,
                {
                    "order_id": last_oid,
                    "leg": leg_label,
                    "product_id": int(product_id),
                    "side": side_l,
                    "size": remaining,
                    "order_type": "limit_order",
                    "time_in_force": "gtc",
                    "limit_price": float(limit),
                    "post_only": bool(use_post_only),
                    "reduce_only": bool(reduce_only),
                },
            )

            after = await _poll_order_until_hold(
                delta_client,
                last_oid,
                hold_seconds=float(hold_seconds),
                trade_id=trade_id,
                leg_label=leg_label,
            )
            got = _filled_size_from_order(after, requested=remaining)
            state = _order_state(after)

            if state in _FILLED_STATES or got >= remaining:
                px = _avg_fill_from_order(after) or float(limit)
                filled_total += remaining
                fill_price_sum += px * remaining
                fill_type_used = order_kind
                _mid_log(
                    "MIDPRICE_FILL",
                    trade_id,
                    {
                        "leg": leg_label,
                        "attempt": attempt,
                        "fill": px,
                        "mid_at_start": mid_at_start,
                        "market_would_be": mkt_ref,
                        "saved_usd": _saved_usd(
                            side_l, px, mkt_ref, remaining
                        ),
                    },
                )
                _fire_exec_event(
                    trade_id=trade_id,
                    hedge_position_id=hedge_position_id,
                    phase=phase,
                    leg=leg_label,
                    side=side_l,
                    qty=remaining,
                    attempt=attempt,
                    profile=prof,
                    order_kind=order_kind,
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    limit=limit,
                    status="filled",
                    fill_price=px,
                )
                remaining = 0
                break

            if got > 0:
                px = _avg_fill_from_order(after) or float(limit)
                filled_total += got
                fill_price_sum += px * got
                remaining = max(0, remaining - got)
                fill_type_used = order_kind
                _mid_log(
                    "MIDPRICE_ATTEMPT",
                    trade_id,
                    {
                        "leg": leg_label,
                        "profile": prof,
                        "attempt": attempt,
                        "type": order_kind,
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "limit": round(float(limit), 4),
                        "qty": remaining + got,
                        "filled": got,
                        "partial": True,
                    },
                )

            outcome, after_cancel = await _cancel_confirm(
                delta_client,
                last_oid,
                trade_id=trade_id,
                leg_label=leg_label,
            )
            if outcome == "filled":
                extra = _filled_size_from_order(after_cancel, requested=remaining)
                if remaining > 0 and extra >= remaining:
                    px = _avg_fill_from_order(after_cancel) or float(limit)
                    filled_total += remaining
                    fill_price_sum += px * remaining
                    remaining = 0
                    fill_type_used = order_kind
                    _mid_log(
                        "MIDPRICE_FILL",
                        trade_id,
                        {
                            "leg": leg_label,
                            "attempt": attempt,
                            "fill": px,
                            "note": "cancel_already_filled",
                            "mid_at_start": mid_at_start,
                            "market_would_be": mkt_ref,
                            "saved_usd": _saved_usd(
                                side_l, px, mkt_ref, qty_total
                            ),
                        },
                    )
                    break
                if remaining > 0 and extra > 0:
                    px = _avg_fill_from_order(after_cancel) or float(limit)
                    filled_total += extra
                    fill_price_sum += px * extra
                    remaining = max(0, remaining - extra)

            log_and_buffer(
                "POST_CANCEL_SETTLE",
                trade_id or 0,
                {
                    "leg": leg_label,
                    "attempt": attempt,
                    "settle_seconds": POST_CANCEL_SETTLE_SECONDS,
                },
            )
            await sleep(POST_CANCEL_SETTLE_SECONDS)

            if (
                partner_mode_started_at is not None
                and last_order_was_partner_final_attempt
                and remaining > 0
                and not partner_market_fallback_done
            ):
                partner_market_fallback_done = True
                log_and_buffer(
                    "PARTNER_MARKET_FALLBACK_TRIGGERED",
                    trade_id or 0,
                    {
                        "leg": leg_label,
                        "attempt": attempt,
                        "remaining": remaining,
                    },
                )
                if await _pre_place_position_check(
                    delta_client,
                    product_id=product_id,
                    qty_intended=qty_total,
                    leg_label=leg_label,
                    attempt=attempt,
                    reduce_only=reduce_only,
                    trade_id=trade_id,
                    check_position_size=check_position_size,
                ):
                    remaining = 0
                    filled_total = qty_total
                    fill_type_used = "position_already_filled"
                    break

                mkt = await _place_market(
                    delta_client,
                    product_id=product_id,
                    side=side_l,
                    quantity=remaining,
                    reduce_only=reduce_only,
                    bracket_sl_price=bracket_sl_price,
                    bracket_sl_limit=bracket_sl_limit,
                    trade_id=trade_id,
                    leg_label=leg_label,
                )
                if mkt.success:
                    fsz = int(mkt.filled_size or remaining)
                    filled_total += fsz
                    if mkt.filled_price:
                        fill_price_sum += float(mkt.filled_price) * fsz
                    last_oid = mkt.order_id
                    fill_type_used = "market"
                    remaining = 0
                    break

                return OrderResult(
                    success=False,
                    error=mkt.error or "partner_final_market_failed",
                    order_id=last_oid,
                    filled_size=filled_total if filled_total else None,
                    fill_attempt=attempt,
                    fill_type="market",
                    mid_at_start=mid_at_start,
                )

            if prof == "urgent" and attempt >= 4:
                break

        avg_fill = fill_price_sum / filled_total if filled_total > 0 else None
        success = filled_total >= qty_total
        saved = None
        if avg_fill is not None:
            try:
                bid0, ask0 = await _fetch_book(delta_client, symbol)
                mkt0 = market_would_be_price(side_l, bid0, ask0)
                saved = _saved_usd(side_l, avg_fill, mkt0, filled_total)
            except Exception:
                saved = None

        result = OrderResult(
            success=success,
            order_id=last_oid,
            filled_price=avg_fill,
            filled_size=filled_total if filled_total else None,
            error=None if success else "incomplete_fill",
            fill_attempt=attempt,
            fill_type=fill_type_used,
            mid_at_start=mid_at_start,
            saved_usd=saved,
            selected_premium=(
                float(selected_premium) if selected_premium is not None else None
            ),
        )

        if (
            success
            and selected_premium is not None
            and avg_fill is not None
            and selection_ts is not None
        ):
            drift = log_entry_drift(
                leg_label=leg_label,
                selected_premium=float(selected_premium),
                fill_premium=float(avg_fill),
                seconds_since_selection=max(
                    0.0, time.monotonic() - float(selection_ts)
                ),
                tolerance_pct=float(entry_premium_match_tolerance_pct),
            )
            result.drift_pct = drift
            result.seconds_since_selection = max(
                0.0, time.monotonic() - float(selection_ts)
            )

        if check_position_size and success and not reduce_only:
            try:
                actual = await get_live_position_size(delta_client, product_id)
                if abs(abs(float(actual)) - float(qty_total)) > 0.5:
                    log_size_mismatch(
                        leg_label=leg_label,
                        intended=qty_total,
                        actual=actual,
                        attempts=attempt,
                        trade_id=trade_id,
                    )
            except Exception as exc:
                _mid_log(
                    "MIDPRICE_SIZE_MISMATCH",
                    trade_id,
                    {
                        "phase": "verify_failed",
                        "leg": leg_label,
                        "error": str(exc),
                    },
                )

        return result


def _saved_usd(
    side: str,
    fill: float,
    market_ref: float | None,
    qty: int,
) -> float | None:
    if market_ref is None or market_ref <= 0 or fill <= 0:
        return None
    cv = float(OPTIONS_CONTRACT_VALUE)
    # BUY: saved if fill < market ask; SELL: saved if fill > market bid
    if str(side).lower() == "buy":
        per = float(market_ref) - float(fill)
    else:
        per = float(fill) - float(market_ref)
    return round(per * abs(int(qty)) * cv, 6)


async def execute_paired_legs(
    *,
    legs: list[dict[str, Any]],
    delta_client: Any,
    reason: str,
    midprice_enabled: bool,
    max_chase_seconds: int | None = None,
    entry_premium_match_tolerance_pct: float = 15.0,
    selection_ts: float | None = None,
    trade_id: int | None = None,
    hedge_position_id: int | None = None,
    phase: str = "",
    leg_type: str = "",
) -> list[OrderResult]:
    """
    Execute legs in order with chase/urgent pairing.
    Each leg dict: product_id, side, quantity, symbol, leg_label,
                   reduce_only?, bracket_sl_price?, selected_premium?
    """
    ts = selection_ts if selection_ts is not None else time.monotonic()
    if len(legs) != 2:
        # Safety fallback: keep existing sequential behavior for non-2-leg inputs.
        results: list[OrderResult] = []
        profiles = profiles_for_paired_sequence(len(legs))
        for i, leg in enumerate(legs):
            res = await execute_with_midprice(
                product_id=int(leg["product_id"]),
                side=str(leg["side"]),
                quantity=int(leg["quantity"]),
                profile=profiles[i],
                delta_client=delta_client,
                reason=reason,
                leg_label=str(leg.get("leg_label") or f"leg{i}"),
                symbol=str(leg.get("symbol") or ""),
                max_chase_seconds=max_chase_seconds,
                reduce_only=bool(leg.get("reduce_only", False)),
                midprice_enabled=midprice_enabled,
                bracket_sl_price=leg.get("bracket_sl_price"),
                bracket_sl_limit=leg.get("bracket_sl_limit"),
                selected_premium=leg.get("selected_premium"),
                selection_ts=ts,
                entry_premium_match_tolerance_pct=entry_premium_match_tolerance_pct,
                trade_id=trade_id,
                hedge_position_id=hedge_position_id,
                phase=phase,
                leg_type=leg_type,
            )
            results.append(res)
            if not res.success:
                break
        return results

    is_stoploss = "STOPLOSS" in str(reason or "").upper()

    async def _exec_one(
        *,
        _leg: dict[str, Any],
        _idx: int,
        partner_event: asyncio.Event | None,
    ) -> OrderResult:
        return await execute_with_midprice(
            product_id=int(_leg["product_id"]),
            side=str(_leg["side"]),
            quantity=int(_leg["quantity"]),
            # Parallel system: both legs start mid-price chase together.
            profile="chase",
            delta_client=delta_client,
            reason=reason,
            leg_label=str(_leg.get("leg_label") or f"leg{_idx}"),
            symbol=str(_leg.get("symbol") or ""),
            max_chase_seconds=max_chase_seconds,
            reduce_only=bool(_leg.get("reduce_only", False)),
            midprice_enabled=False if is_stoploss else midprice_enabled,
            bracket_sl_price=_leg.get("bracket_sl_price"),
            bracket_sl_limit=_leg.get("bracket_sl_limit"),
            selected_premium=_leg.get("selected_premium"),
            selection_ts=ts,
            entry_premium_match_tolerance_pct=entry_premium_match_tolerance_pct,
            trade_id=trade_id,
            hedge_position_id=hedge_position_id,
            phase=phase,
            leg_type=leg_type,
            partner_filled_event=partner_event,
        )

    # STOP-LOSS: no partner-signal logic; place both market orders concurrently.
    if is_stoploss:
        results = await asyncio.gather(
            _exec_one(_leg=legs[0], _idx=0, partner_event=None),
            _exec_one(_leg=legs[1], _idx=1, partner_event=None),
        )
        return list(results)

    partner_event = asyncio.Event()

    async def _exec_and_signal(
        *,
        _leg: dict[str, Any],
        _idx: int,
    ) -> OrderResult:
        res = await _exec_one(
            _leg=_leg,
            _idx=_idx,
            partner_event=partner_event,
        )
        if res.success and not partner_event.is_set():
            # Signal other leg to enter partner-shortened mode.
            partner_event.set()
        return res

    res0, res1 = await asyncio.gather(
        _exec_and_signal(_leg=legs[0], _idx=0),
        _exec_and_signal(_leg=legs[1], _idx=1),
    )

    log_and_buffer(
        "PARALLEL_PAIR_COMPLETED",
        trade_id or 0,
        {
            "leg0": str(legs[0].get("leg_label") or "leg0"),
            "leg1": str(legs[1].get("leg_label") or "leg1"),
            "success0": bool(res0.success),
            "success1": bool(res1.success),
        },
    )

    return [res0, res1]
