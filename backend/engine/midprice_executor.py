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
from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.delta_client import DeltaAPIError
from backend.strategies.base_strategy import OrderResult

logger = logging.getLogger(__name__)

HOLD_SECONDS = 3.0
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


def should_use_midprice(*, enabled: bool, reason: str) -> bool:
    """Allow-list gate — deny by default for any new/unknown reason."""
    if not bool(enabled):
        return False
    return str(reason or "").upper().strip() in MIDPRICE_ALLOWED_REASONS


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
) -> dict[str, Any]:
    """Hold up to hold_seconds, polling Delta order status (source of truth)."""
    deadline = time.monotonic() + float(hold_seconds)
    last: dict[str, Any] = {}
    while True:
        try:
            last = await delta_client.get_order(order_id)
        except Exception as exc:
            logger.warning(
                "[MIDPRICE_ATTEMPT] get_order failed id=%s: %s",
                order_id,
                exc,
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
            return "filled", od
        logger.warning(
            "[MIDPRICE_ATTEMPT] cancel failed id=%s: %s — re-checking status",
            order_id,
            exc,
        )

    try:
        od = await delta_client.get_order(order_id)
    except Exception:
        return "unknown", None

    state = _order_state(od)
    if state in _FILLED_STATES:
        return "filled", od
    filled = _filled_size_from_order(od, requested=0)
    if filled > 0 and state not in _OPEN_STATES:
        return "filled", od
    return "cancelled", od


async def _place_market(
    delta_client: Any,
    *,
    product_id: int,
    side: str,
    quantity: int,
    reduce_only: bool,
    bracket_sl_price: float | None = None,
    bracket_sl_limit: float | None = None,
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
) -> None:
    logger.critical(
        "[MIDPRICE_SIZE_MISMATCH] leg=%s intended=%s actual=%s attempts=%s",
        leg_label,
        intended,
        actual,
        attempts,
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
) -> OrderResult:
    """
    Place one leg via chase or urgent ladder. Falls back to market when
    midprice is disabled or reason is not allow-listed.
    """
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
        )
        res.fill_type = "market"
        return res

    chase_max = clamp_chase_max_seconds(max_chase_seconds)
    started = now()
    remaining = qty_total
    filled_total = 0
    fill_price_sum = 0.0  # weighted
    last_oid: int | None = None
    attempt = 0
    mid_at_start: float | None = None
    fill_type_used = "market"

    # urgent: attempt types cycle mid → best → best → market
    urgent_types = ("mid", "best", "best", "market")

    while remaining > 0:
        elapsed = now() - started
        attempt += 1

        if prof == "chase" and elapsed >= chase_max:
            logger.warning(
                "[MIDPRICE_CHASE_TIMEOUT] leg=%s elapsed=%.1f attempts=%s "
                "action=market",
                leg_label,
                elapsed,
                attempt,
            )
            order_kind = "market"
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
            # safety — should have market on 4th
            break

        try:
            bid, ask = await _fetch_book(delta_client, symbol)
        except Exception as exc:
            logger.warning(
                "[MIDPRICE_ATTEMPT] book fetch failed leg=%s: %s",
                leg_label,
                exc,
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
            logger.info(
                "[MIDPRICE_ATTEMPT] leg=%s profile=%s attempt=%s type=market "
                "bid=%s ask=%s mid=%s limit=.. qty=%s filled=0",
                leg_label,
                prof,
                attempt,
                bid,
                ask,
                mid,
                remaining,
            )
            mkt = await _place_market(
                delta_client,
                product_id=product_id,
                side=side_l,
                quantity=remaining,
                reduce_only=reduce_only,
                bracket_sl_price=bracket_sl_price,
                bracket_sl_limit=bracket_sl_limit,
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

        # Limit path: mid (post-only) or best (crossing, no post-only)
        use_post_only = order_kind == "mid"
        if order_kind == "mid":
            limit = mid
        else:
            limit = best_price_for_side(side_l, bid, ask)

        if limit is None or limit <= 0:
            # No book — skip to next / market
            if prof == "urgent" and attempt >= 3:
                order_kind = "market"
                continue
            await sleep(0)  # yield
            continue

        logger.info(
            "[MIDPRICE_ATTEMPT] leg=%s profile=%s attempt=%s type=%s "
            "bid=%s ask=%s mid=%s limit=%s qty=%s filled=0",
            leg_label,
            prof,
            attempt,
            order_kind,
            bid,
            ask,
            mid,
            round(float(limit), 4),
            remaining,
        )
        if prof == "chase" and attempt % 5 == 0:
            logger.info(
                "[MIDPRICE_CHASE] leg=%s attempt=%s elapsed=%.1f mid=%s "
                "bid=%s ask=%s",
                leg_label,
                attempt,
                elapsed,
                mid,
                bid,
                ask,
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
                logger.warning(
                    "[MIDPRICE_POSTONLY_REJECT] leg=%s attempt=%s mid=%s "
                    "bid=%s ask=%s",
                    leg_label,
                    attempt,
                    mid,
                    bid,
                    ask,
                )
                # No 3s wait — immediate next attempt
                continue
            logger.warning(
                "[MIDPRICE_ATTEMPT] place failed leg=%s: %s",
                leg_label,
                exc,
            )
            if prof == "urgent" and attempt >= 3:
                continue  # fall through to market on next loop
            await sleep(0.2)
            continue

        oid = placed.get("order_id") or placed.get("id")
        if oid is None:
            continue
        last_oid = int(oid)

        # Hold + poll Delta (source of truth — never trust local state alone)
        after = await _poll_order_until_hold(
            delta_client,
            last_oid,
            hold_seconds=float(hold_seconds),
        )
        got = _filled_size_from_order(after, requested=remaining)
        state = _order_state(after)

        if state in _FILLED_STATES or got >= remaining:
            # FULL FILL — STOP immediately, no new order
            px = _avg_fill_from_order(after) or float(limit)
            filled_total += remaining
            fill_price_sum += px * remaining
            fill_type_used = order_kind
            logger.info(
                "[MIDPRICE_FILL] leg=%s attempt=%s fill=%s mid_at_start=%s "
                "market_would_be=%s saved_usd=%s",
                leg_label,
                attempt,
                px,
                mid_at_start,
                mkt_ref,
                _saved_usd(side_l, px, mkt_ref, remaining),
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
            # PARTIAL — only remainder on next order
            px = _avg_fill_from_order(after) or float(limit)
            filled_total += got
            fill_price_sum += px * got
            remaining = max(0, remaining - got)
            fill_type_used = order_kind
            logger.info(
                "[MIDPRICE_ATTEMPT] leg=%s profile=%s attempt=%s type=%s "
                "bid=%s ask=%s mid=%s limit=%s qty=%s filled=%s (partial)",
                leg_label,
                prof,
                attempt,
                order_kind,
                bid,
                ask,
                mid,
                round(float(limit), 4),
                remaining + got,
                got,
            )

        # Cancel before placing anything else
        outcome, after_cancel = await _cancel_confirm(delta_client, last_oid)
        if outcome == "filled":
            # Filled during cancel race — STOP
            extra = _filled_size_from_order(
                after_cancel, requested=remaining
            )
            if remaining > 0 and extra >= remaining:
                px = _avg_fill_from_order(after_cancel) or float(limit)
                filled_total += remaining
                fill_price_sum += px * remaining
                remaining = 0
                fill_type_used = order_kind
                logger.info(
                    "[MIDPRICE_FILL] leg=%s attempt=%s fill=%s "
                    "(cancel-already-filled) mid_at_start=%s "
                    "market_would_be=%s saved_usd=%s",
                    leg_label,
                    attempt,
                    px,
                    mid_at_start,
                    mkt_ref,
                    _saved_usd(side_l, px, mkt_ref, qty_total),
                )
                break
            if remaining > 0 and extra > 0:
                px = _avg_fill_from_order(after_cancel) or float(limit)
                filled_total += extra
                fill_price_sum += px * extra
                remaining = max(0, remaining - extra)

        # chase continues with FRESH mid; urgent next type
        if prof == "urgent" and attempt >= 4:
            break

    avg_fill = (
        fill_price_sum / filled_total if filled_total > 0 else None
    )
    success = filled_total >= qty_total
    saved = None
    if avg_fill and mid_at_start is not None:
        # compare fill vs market-crossing price at start if we have book
        pass
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

    # Final position size check (opens only — closes skip)
    if check_position_size and success and not reduce_only:
        try:
            actual = await get_live_position_size(delta_client, product_id)
            if abs(abs(float(actual)) - float(qty_total)) > 0.5:
                log_size_mismatch(
                    leg_label=leg_label,
                    intended=qty_total,
                    actual=actual,
                    attempts=attempt,
                )
        except Exception as exc:
            logger.warning(
                "[MIDPRICE_SIZE_MISMATCH] verify failed leg=%s: %s",
                leg_label,
                exc,
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
    results: list[OrderResult] = []
    profiles = profiles_for_paired_sequence(len(legs))
    ts = selection_ts if selection_ts is not None else time.monotonic()
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
