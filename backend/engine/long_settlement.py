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


def _session_has_writes(db: Any) -> bool:
    return bool(getattr(db, "new", None) or getattr(db, "dirty", None) or getattr(db, "deleted", None))


def _commit_if_dirty(db: Any) -> None:
    if _session_has_writes(db):
        db.commit()


def _refresh_expunge_for_tracker(db: Any, *objs: Any) -> None:
    """Load column values, then detach so they stay readable after session close."""
    seen: set[int] = set()
    for obj in objs:
        if obj is None:
            continue
        ident = id(obj)
        if ident in seen:
            continue
        seen.add(ident)
        try:
            db.refresh(obj)
        except Exception as exc:
            logger.warning("tracker refresh failed %s: %s", type(obj).__name__, exc)
        try:
            db.expunge(obj)
        except Exception as exc:
            logger.warning("tracker expunge failed %s: %s", type(obj).__name__, exc)

LONG_EXPIRY_FLAT_WAIT_SECONDS = 30 * 60
LONG_EXTERNAL_FLAT_CONFIRM_SECONDS = 60.0


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


async def resolve_settlement_spot(
    client: Any,
    legs: list[Any],
) -> tuple[float, str]:
    """
    Official product ``settlement_price``, else BTC index.

    Returns (spot, settlement_src) where src is official | index_fallback.
    """
    if client is not None:
        getter = getattr(client, "get_official_settlement_price", None)
        if callable(getter):
            for leg in legs or []:
                symbol = str(getattr(leg, "symbol", "") or "") or None
                try:
                    pid_raw = getattr(leg, "product_id", None)
                    pid = int(pid_raw) if pid_raw else None
                except (TypeError, ValueError):
                    pid = None
                if not symbol and not pid:
                    continue
                try:
                    px = await getter(symbol=symbol, product_id=pid)
                except Exception as exc:
                    logger.warning(
                        "official settlement_price fetch failed %s: %s",
                        symbol or pid,
                        exc,
                    )
                    px = None
                try:
                    val = float(px or 0)
                except (TypeError, ValueError):
                    val = 0.0
                if val > 0:
                    return val, "official"
        logger.warning("settlement fallback to index")
        try:
            idx = float(await client.get_btc_index_price() or 0)
        except Exception as exc:
            logger.error("settlement index fallback failed: %s", exc, exc_info=True)
            idx = 0.0
        return idx, "index_fallback"
    return 0.0, "index_fallback"


def apply_expiry_settlement(
    *,
    trade: Any,
    legs: list[Any],
    spot: float,
    finalize: bool = True,
    settlement_src: str | None = None,
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
    if settlement_src:
        trade.settlement_src = settlement_src
        prior = str(getattr(trade, "notes", None) or "")
        tag = f"settlement_src={settlement_src}"
        if tag not in prior:
            trade.notes = f"{prior};{tag}".strip(";") if prior else tag
    if finalize:
        trade.status = TradeStatus.CLOSED.value
        trade.exit_time = now
    logger.info(
        "[EXPIRY_SETTLED] trade=%s spot=%s settlement_src=%s realized=%s (no orders)",
        getattr(trade, "id", "?"),
        spot,
        settlement_src or "?",
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


async def _single_position_size(client: Any, product_id: int) -> float | None:
    """Size from GET /v2/positions?product_id=. None = unconfirmed."""
    if client is None:
        return None
    getter = getattr(client, "get_position_size", None)
    if not callable(getter):
        logger.warning(
            "delta_client has no get_position_size — cannot confirm flat"
        )
        return None
    try:
        raw = await getter(int(product_id))
    except Exception as exc:
        logger.warning(
            "get_position_size product_id=%s failed: %s", product_id, exc
        )
        return None
    if raw is None:
        return None
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return None


async def apply_pre_expiry_external_close(
    *,
    client: Any,
    trade: Any,
    legs: list[Any],
    positions: list[dict[str, Any]] | None,
    db: Any | None = None,
    flat_since: dict[tuple[int, int], float] | None = None,
    now_monotonic: float | None = None,
) -> str | None:
    """
    If a long call/put is already flat on Delta, book via fill history.
    No orders. Both closed → EXTERNAL_CLOSE.

    Flat requires: non-empty positions list with size 0 or missing pid,
    plus get_position_size == 0, confirmed on two ticks ≥ 60s apart.
    """
    import time

    open_main = _main_open_legs(legs)
    if not open_main:
        now = get_utc_now()
        trade.status = TradeStatus.CLOSED.value
        trade.exit_reason = ExitReason.EXTERNAL_CLOSE.value
        trade.exit_time = now
        if db is not None:
            recompute_trade_realized_pnl(db, trade)
        return ExitReason.EXTERNAL_CLOSE.value

    if positions is None:
        return None
    if len(positions) == 0:
        logger.warning(
            "[EXTERNAL_CLOSE] trade=%s empty positions list with %s open legs "
            "— skipping close this tick",
            getattr(trade, "id", "?"),
            len(open_main),
        )
        return None

    size_by_pid = _pid_size_map(positions)
    now = get_utc_now()
    now_m = float(now_monotonic if now_monotonic is not None else time.monotonic())
    trade_id = int(getattr(trade, "id", 0) or 0)
    timers = flat_since if flat_since is not None else {}

    for leg in open_main:
        pid = int(getattr(leg, "product_id", 0) or 0)
        if pid <= 0:
            continue
        key = (trade_id, pid)
        list_size = size_by_pid.get(pid, 0.0)
        if list_size > 0:
            timers.pop(key, None)
            continue
        single = await _single_position_size(client, pid)
        if single is None or single > 0:
            timers.pop(key, None)
            continue
        first = timers.get(key)
        if first is None:
            timers[key] = now_m
            logger.info(
                "[EXTERNAL_CLOSE] trade=%s %s pid=%s first flat tick — waiting 60s",
                trade_id,
                getattr(leg, "leg_type", "?"),
                pid,
            )
            continue
        if (now_m - float(first)) < LONG_EXTERNAL_FLAT_CONFIRM_SECONDS:
            logger.info(
                "[EXTERNAL_CLOSE] trade=%s %s pid=%s flat but <60s since first tick",
                trade_id,
                getattr(leg, "leg_type", "?"),
                pid,
            )
            continue
        fill = await resolve_external_exit_fill(client, leg)
        book_leg_close(
            leg=leg,
            trade=trade,
            exit_premium=fill,
            exit_time=now,
            db=db,
        )
        timers.pop(key, None)
        logger.info(
            "[EXTERNAL_CLOSE] trade=%s %s booked fill=%s (no orders, 2-tick confirm)",
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
    flat_since: dict[tuple[int, int], float] | None = None,
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
            if open_main and not positions:
                logger.warning(
                    "[EXPIRY_SETTLED] trade=%s empty positions list with open "
                    "legs — skipping settlement this tick",
                    trade_id,
                )
                return "stop"
            if _all_main_legs_flat(open_main, size_by_pid):
                spot, settlement_src = await resolve_settlement_spot(
                    delta_client, legs
                )
                if spot <= 0:
                    logger.critical(
                        "[EXPIRY_SETTLED] trade=%s missing settlement/index — leaving open",
                        trade_id,
                    )
                    return "stop"
                apply_expiry_settlement(
                    trade=db_trade,
                    legs=legs,
                    spot=spot,
                    finalize=False,
                    settlement_src=settlement_src,
                )
                wait_since.pop(trade_id, None)
                if close_funnel is not None:
                    _commit_if_dirty(db)
                    call_leg, put_leg = pick_call_put_legs(legs)
                    _refresh_expunge_for_tracker(db, db_trade, call_leg, put_leg)
                    await close_funnel(
                        trade_id,
                        ExitReason.EXPIRY_SETTLED.value,
                        skip_master_legs=True,
                    )
                else:
                    db_trade.status = TradeStatus.CLOSED.value
                    db_trade.exit_time = get_utc_now()
                    _commit_if_dirty(db)
                    call_leg, put_leg = pick_call_put_legs(legs)
                    _refresh_expunge_for_tracker(db, db_trade, call_leg, put_leg)
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
            flat_since=flat_since,
        )
        if reason is not None:
            _commit_if_dirty(db)
            call_leg, put_leg = pick_call_put_legs(legs)
            _refresh_expunge_for_tracker(db, db_trade, call_leg, put_leg)
            if close_funnel is not None:
                await close_funnel(
                    trade_id,
                    ExitReason.EXTERNAL_CLOSE.value,
                    skip_master_legs=True,
                )
            elif position_tracker is not None:
                position_tracker.mark_closed(trade_id)
            return "stop"
        _commit_if_dirty(db)
        call_leg, put_leg = pick_call_put_legs(legs)
        _refresh_expunge_for_tracker(db, db_trade, call_leg, put_leg)
        if (
            position_tracker is not None
            and call_leg is not None
            and put_leg is not None
        ):
            position_tracker.update_legs(trade_id, call_leg, put_leg, db_trade)
        return "continue"
