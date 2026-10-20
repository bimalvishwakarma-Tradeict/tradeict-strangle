# long_settlement.py — Long-strangle external close + expiry intrinsic (no orders)

from __future__ import annotations

import logging
from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE, ExitReason, TradeStatus
from backend.core.delta_client import compute_signed_upnl
from backend.core.time_utils import get_hours_to_expiry, get_utc_now
from backend.engine.trade_reconcile import (
    book_leg_close,
    pick_call_put_legs,
    recompute_trade_realized_pnl,
    resolve_external_exit_fill,
)
from backend.models import Leg, Trade

logger = logging.getLogger(__name__)

LONG_EXPIRY_FLAT_WAIT_SECONDS = 30 * 60


def option_intrinsic(leg_type: str, strike: float, spot: float) -> float:
    """Call max(0, S−K); put max(0, K−S). Zero is a valid settlement price."""
    lt = str(leg_type or "").lower()
    k = float(strike)
    s = float(spot)
    if "call" in lt:
        return max(0.0, s - k)
    return max(0.0, k - s)


def _is_open(leg: Any) -> bool:
    return str(getattr(leg, "status", "") or "").lower() == "open"


def _pid_size_map(positions: list[dict[str, Any]] | None) -> dict[int, float]:
    out: dict[int, float] = {}
    for pos in positions or []:
        try:
            pid = int(pos.get("product_id"))
        except (TypeError, ValueError):
            continue
        try:
            size = abs(float(pos.get("size") or 0))
        except (TypeError, ValueError):
            size = 0.0
        if pid > 0:
            out[pid] = size
    return out


def _main_open_legs(legs: list[Any]) -> list[Any]:
    return [
        leg
        for leg in legs
        if _is_open(leg)
        and str(getattr(leg, "leg_type", "") or "").lower() in ("call", "put")
    ]


def book_long_settlement_leg(
    *,
    leg: Any,
    trade: Any,
    exit_premium: float,
    exit_time: Any = None,
) -> float:
    """Book a long-leg close; exit_premium 0.0 is allowed (OTM expiry)."""
    now = exit_time or get_utc_now()
    px = float(exit_premium)
    entry = float(getattr(leg, "initial_premium", 0) or 0.0)
    qty = abs(int(getattr(leg, "quantity", 0) or 0))
    cv = float(OPTIONS_CONTRACT_VALUE)
    realized = compute_signed_upnl(entry, px, size=float(qty), contract_value=cv)
    if getattr(leg, "is_long", False) is not True:
        realized = (entry - px) * qty * cv
    leg.status = "closed"
    leg.exit_time = now
    leg.exit_premium = px
    leg.exit_fee_usd = 0.0
    leg.realized_pnl = realized
    return realized


def apply_expiry_settlement(
    *,
    trade: Any,
    legs: list[Any],
    spot: float,
    finalize: bool = True,
) -> str:
    """Intrinsic-settle all open call/put legs. Never places orders."""
    now = get_utc_now()
    for leg in _main_open_legs(legs):
        px = option_intrinsic(
            str(leg.leg_type),
            float(getattr(leg, "strike", 0) or 0),
            float(spot),
        )
        book_long_settlement_leg(
            leg=leg, trade=trade, exit_premium=px, exit_time=now
        )
    realized_sum = 0.0
    for leg in legs:
        realized_sum += float(getattr(leg, "realized_pnl", None) or 0.0)
    trade.realized_pnl = realized_sum
    trade.exit_reason = ExitReason.EXPIRY_SETTLED.value
    if finalize:
        trade.status = TradeStatus.CLOSED.value
        trade.exit_time = now
    logger.info(
        "[EXPIRY_SETTLED] trade=%s spot=%s realized=%s (no orders)",
        getattr(trade, "id", "?"),
        spot,
        realized_sum,
    )
    return ExitReason.EXPIRY_SETTLED.value


def _all_main_legs_flat(
    open_legs: list[Any], size_by_pid: dict[int, float]
) -> bool:
    if not open_legs:
        return True
    for leg in open_legs:
        pid = int(getattr(leg, "product_id", 0) or 0)
        if pid <= 0:
            return False
        if size_by_pid.get(pid, 0.0) > 0:
            return False
    return True


async def apply_pre_expiry_external_close(
    *,
    client: Any,
    trade: Any,
    legs: list[Any],
    positions: list[dict[str, Any]] | None,
    db: Any | None = None,
) -> str | None:
    """
    If a long call/put is already flat on Delta, book via fill history.
    No orders. Both closed → EXTERNAL_CLOSE.
    """
    size_by_pid = _pid_size_map(positions)
    now = get_utc_now()
    for leg in _main_open_legs(legs):
        pid = int(getattr(leg, "product_id", 0) or 0)
        if pid <= 0:
            continue
        if size_by_pid.get(pid, 0.0) > 0:
            continue
        fill = await resolve_external_exit_fill(client, leg)
        book_leg_close(
            leg=leg,
            trade=trade,
            exit_premium=fill,
            exit_time=now,
            db=db,
        )
        logger.info(
            "[EXTERNAL_CLOSE] trade=%s %s booked fill=%s (no orders)",
            getattr(trade, "id", "?"),
            getattr(leg, "leg_type", "?"),
            fill,
        )
    still_open = _main_open_legs(legs)
    if still_open:
        return None
    trade.status = TradeStatus.CLOSED.value
    trade.exit_reason = ExitReason.EXTERNAL_CLOSE.value
    trade.exit_time = now
    if db is not None:
        recompute_trade_realized_pnl(db, trade)
    return ExitReason.EXTERNAL_CLOSE.value


async def handle_long_trade_tick(
    *,
    db_factory: Any,
    delta_client: Any,
    position_tracker: Any,
    wait_since: dict[int, float],
    trade_state: Any,
    close_funnel: Any | None = None,
) -> str:
    """
    Returns 'stop' when the monitor should not run on_tick / place orders,
    'continue' to proceed to TP/SL.
    """
    import time

    trade = trade_state.trade
    trade_id = int(trade_state.trade_id)
    hours_left = get_hours_to_expiry(trade.expiry_date)

    with db_factory() as db:
        db_trade = db.query(Trade).filter(Trade.id == trade_id).first()
        if db_trade is None:
            return "stop"
        legs = (
            db.query(Leg)
            .filter(Leg.trade_id == trade_id, Leg.is_bot_managed.is_(True))
            .all()
        )
        open_main = _main_open_legs(legs)

        positions: list[dict[str, Any]] = []
        if delta_client is not None:
            try:
                positions = await delta_client.get_positions()
            except Exception as exc:
                logger.warning(
                    "Long trade %s positions fetch failed: %s", trade_id, exc
                )
                return "stop" if hours_left == 0 else "continue"

        size_by_pid = _pid_size_map(positions)

        if hours_left == 0:
            if _all_main_legs_flat(open_main, size_by_pid):
                spot = 0.0
                if delta_client is not None:
                    try:
                        spot = float(await delta_client.get_btc_index_price() or 0)
                    except Exception as exc:
                        logger.error(
                            "Expiry settlement index fetch failed trade=%s: %s",
                            trade_id,
                            exc,
                            exc_info=True,
                        )
                if spot <= 0:
                    logger.critical(
                        "[EXPIRY_SETTLED] trade=%s missing 17:30 index — leaving open",
                        trade_id,
                    )
                    return "stop"
                apply_expiry_settlement(
                    trade=db_trade, legs=legs, spot=spot, finalize=False
                )
                db.commit()
                wait_since.pop(trade_id, None)
                if close_funnel is not None:
                    await close_funnel(
                        trade_id,
                        ExitReason.EXPIRY_SETTLED.value,
                        skip_master_legs=True,
                    )
                else:
                    db_trade.status = TradeStatus.CLOSED.value
                    db_trade.exit_time = get_utc_now()
                    db.commit()
                    if position_tracker is not None:
                        position_tracker.mark_closed(trade_id)
                return "stop"

            started = wait_since.get(trade_id)
            now_m = time.monotonic()
            if started is None:
                wait_since[trade_id] = now_m
                logger.warning(
                    "[EXPIRY_WAIT] trade=%s positions not flat after 17:30 IST",
                    trade_id,
                )
            elif (now_m - float(started)) >= LONG_EXPIRY_FLAT_WAIT_SECONDS:
                logger.critical(
                    "[EXPIRY_WAIT] trade=%s still not flat 30 min after expiry — "
                    "leaving trade open, no orders",
                    trade_id,
                )
            return "stop"

        wait_since.pop(trade_id, None)
        reason = await apply_pre_expiry_external_close(
            client=delta_client,
            trade=db_trade,
            legs=legs,
            positions=positions,
            db=db,
        )
        if reason is not None:
            db.commit()
            if close_funnel is not None:
                await close_funnel(
                    trade_id,
                    ExitReason.EXTERNAL_CLOSE.value,
                    skip_master_legs=True,
                )
            elif position_tracker is not None:
                position_tracker.mark_closed(trade_id)
            return "stop"
        db.commit()
        call_leg, put_leg = pick_call_put_legs(legs)
        if (
            position_tracker is not None
            and call_leg is not None
            and put_leg is not None
        ):
            position_tracker.update_legs(trade_id, call_leg, put_leg, db_trade)
        return "continue"
