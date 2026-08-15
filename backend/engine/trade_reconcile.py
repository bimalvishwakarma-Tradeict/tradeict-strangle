# trade_reconcile.py — Keep DB trade/leg status aligned with Delta open sizes

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.config import ExitReason, TradeStatus
from backend.core.delta_client import short_leg_realized_pnl
from backend.core.time_utils import get_ist_now
from backend.models import Leg, Trade

logger = logging.getLogger(__name__)


def _is_open(leg: Any) -> bool:
    return str(getattr(leg, "status", "") or "").lower() == "open"


def pick_call_put_legs(legs: list[Any]) -> tuple[Any | None, Any | None]:
    """
    Prefer open call/put; else latest closed of each type.
    Returns (call_leg, put_leg) — either may be None.
    """
    calls = [leg for leg in legs if leg.leg_type == "call"]
    puts = [leg for leg in legs if leg.leg_type == "put"]
    call_open = next((leg for leg in calls if _is_open(leg)), None)
    put_open = next((leg for leg in puts if _is_open(leg)), None)
    call_leg = call_open or (max(calls, key=lambda x: int(x.id)) if calls else None)
    put_leg = put_open or (max(puts, key=lambda x: int(x.id)) if puts else None)
    return call_leg, put_leg


def count_open_bot_legs(db: Any, trade_id: int) -> int:
    return (
        db.query(Leg)
        .filter(
            Leg.trade_id == trade_id,
            Leg.status == "open",
            Leg.is_bot_managed.is_(True),
        )
        .count()
    )


def book_leg_close(
    *,
    leg: Any,
    trade: Any,
    exit_premium: float,
    exit_time: datetime | None = None,
    exit_fee_usd: float | None = None,
    exit_order_id: str | None = None,
) -> float:
    """Mark leg closed and add realized USD to leg + trade. Returns leg realized."""
    from backend.config import OPTIONS_CONTRACT_VALUE

    now = exit_time or datetime.now(timezone.utc)
    exit_px = float(exit_premium or 0.0)
    entry_px = float(leg.initial_premium or 0.0)
    qty = abs(int(leg.quantity or 0))
    cv = float(OPTIONS_CONTRACT_VALUE)
    if bool(getattr(leg, "is_long", False)):
        # Long hedge: profit = (sell_exit - buy_entry) * qty * cv
        realized = (exit_px - entry_px) * qty * cv
    else:
        realized = short_leg_realized_pnl(
            entry_fill=entry_px,
            exit_fill=exit_px,
            quantity=qty,
        )
    leg.status = "closed"
    leg.exit_time = now
    leg.exit_premium = exit_px
    leg.realized_pnl = realized
    if exit_order_id is not None:
        leg.exit_order_id = str(exit_order_id)
    if exit_fee_usd is not None:
        leg.exit_fee_usd = abs(float(exit_fee_usd))
    prior = float(getattr(trade, "realized_pnl", None) or 0.0)
    trade.realized_pnl = prior + realized
    return realized


def finalize_trade_if_flat(
    *,
    db: Any,
    trade: Any,
    exit_reason: str = ExitReason.MANUAL_LEG_CLOSE.value,
) -> bool:
    """If no open bot legs remain, mark trade closed. Returns True if closed."""
    if count_open_bot_legs(db, trade.id) > 0:
        return False
    trade.status = TradeStatus.CLOSED.value
    trade.exit_time = get_ist_now()
    trade.exit_reason = exit_reason
    if trade.realized_pnl is None:
        trade.realized_pnl = 0.0
    return True


def heal_zombie_active_trades(
    db: Any,
    position_tracker: Any | None = None,
) -> list[int]:
    """
    ACTIVE trades with zero open bot-managed legs → mark closed.
    Also drops them from the in-memory tracker when provided.
    Returns closed trade ids.
    """
    closed_ids: list[int] = []
    actives = (
        db.query(Trade).filter(Trade.status == TradeStatus.ACTIVE.value).all()
    )
    for trade in actives:
        if count_open_bot_legs(db, trade.id) > 0:
            continue
        trade.status = TradeStatus.CLOSED.value
        if trade.exit_time is None:
            trade.exit_time = get_ist_now()
        if not trade.exit_reason:
            trade.exit_reason = ExitReason.MANUAL_LEG_CLOSE.value
        if trade.realized_pnl is None:
            trade.realized_pnl = 0.0
        closed_ids.append(int(trade.id))
        if position_tracker is not None:
            position_tracker.mark_closed(int(trade.id))
        logger.warning(
            "Healed zombie ACTIVE trade id=%s (no open bot legs) → closed",
            trade.id,
        )
    if closed_ids:
        db.commit()
    return closed_ids


async def reconcile_open_legs_with_delta(
    *,
    db: Any,
    client: Any,
    position_tracker: Any | None = None,
) -> dict[str, Any]:
    """
    Align DB open legs with Delta sizes.

    Returns:
      {
        "fully_closed": [trade_id, ...],
        "naked_risk": [
          {"trade_id": int, "remaining": "call"|"put", "missing": "call"|"put"},
          ...
        ],
      }

    IMPORTANT: If a 2-leg basket has exactly one leg flat on Delta, we do NOT
    mark it closed here and leave a naked remaining leg. Instead we return
    naked_risk so the bot can emergency-close the remaining leg + cancel SLs.
    """
    fully_closed = heal_zombie_active_trades(db, position_tracker)
    naked_risk: list[dict[str, Any]] = []

    # Drop tracker entries already CLOSED in DB (stale after external close)
    if position_tracker is not None:
        for state in list(position_tracker.get_all_active()):
            row = db.query(Trade).filter(Trade.id == state.trade_id).first()
            if row is None:
                position_tracker.mark_closed(state.trade_id)
                continue
            open_n = count_open_bot_legs(db, state.trade_id)
            if (
                str(row.status).lower() != TradeStatus.ACTIVE.value
                or open_n == 0
            ):
                if str(row.status).lower() == TradeStatus.ACTIVE.value and open_n == 0:
                    row.status = TradeStatus.CLOSED.value
                    if row.exit_time is None:
                        row.exit_time = get_ist_now()
                    if not row.exit_reason:
                        row.exit_reason = ExitReason.MANUAL_LEG_CLOSE.value
                    db.commit()
                if state.trade_id not in fully_closed:
                    fully_closed.append(int(state.trade_id))
                position_tracker.mark_closed(int(state.trade_id))
                logger.info(
                    "Removed flat/closed trade %s from tracker",
                    state.trade_id,
                )

    if client is None:
        return {"fully_closed": fully_closed, "naked_risk": naked_risk}

    try:
        positions = await client.get_positions()
    except Exception as exc:
        logger.warning("Delta positions fetch failed during reconcile: %s", exc)
        return {"fully_closed": fully_closed, "naked_risk": naked_risk}

    size_by_pid: dict[int, int] = {}
    mark_by_pid: dict[int, float] = {}
    for pos in positions:
        try:
            pid = int(pos.get("product_id"))
        except (TypeError, ValueError):
            continue
        size_by_pid[pid] = abs(int(pos.get("size") or 0))
        mark_by_pid[pid] = float(pos.get("mark_price") or 0.0)

    actives = (
        db.query(Trade).filter(Trade.status == TradeStatus.ACTIVE.value).all()
    )
    now = datetime.now(timezone.utc)

    for trade in actives:
        # Demo/virtual trades have no real Delta size — never reconcile-close them
        if bool(getattr(trade, "is_demo", False)):
            logger.debug(
                "Reconcile skip trade=%s (is_demo=True)",
                trade.id,
            )
            continue

        open_legs = (
            db.query(Leg)
            .filter(
                Leg.trade_id == trade.id,
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .all()
        )
        if not open_legs:
            continue

        flat_legs: list[Any] = []
        live_legs: list[Any] = []
        for leg in open_legs:
            pid = int(leg.product_id or 0)
            if pid <= 0:
                live_legs.append(leg)
                continue
            if size_by_pid.get(pid, 0) > 0:
                live_legs.append(leg)
            else:
                flat_legs.append(leg)

        if not flat_legs:
            continue

        # AUDIT-6: naked-risk uses short call/put only — hedge_* is an extra long
        def _is_short_leg(leg: Any) -> bool:
            return str(getattr(leg, "leg_type", "") or "").lower() in ("call", "put")

        short_open = [leg for leg in open_legs if _is_short_leg(leg)]
        short_flat = [leg for leg in flat_legs if _is_short_leg(leg)]
        short_live = [leg for leg in live_legs if _is_short_leg(leg)]

        # Two open shorts in DB, exactly one flat on Delta → naked risk
        if len(short_open) >= 2 and len(short_flat) == 1 and len(short_live) == 1:
            missing = short_flat[0]
            remaining = short_live[0]
            naked_risk.append(
                {
                    "trade_id": int(trade.id),
                    "remaining": str(remaining.leg_type).lower(),
                    "missing": str(missing.leg_type).lower(),
                }
            )
            logger.critical(
                "Reconcile NAKED RISK trade=%s: %s flat on Delta, %s still open",
                trade.id,
                missing.leg_type,
                remaining.leg_type,
            )
            continue

        # All flat tracked legs (shorts + hedge) → book closes + cancel SLs
        changed = False
        for leg in flat_legs:
            pid = int(leg.product_id or 0)
            exit_px = mark_by_pid.get(pid, 0.0)
            if exit_px <= 0:
                try:
                    exit_px = float(await client.get_mark_price(str(leg.symbol)))
                except Exception:
                    exit_px = float(leg.initial_premium or 0.0)
            # Cancel orphan SL for this externally closed leg
            oid = getattr(leg, "delta_sl_order_id", None)
            if oid:
                try:
                    await client.cancel_order(int(oid))
                    logger.info(
                        "Reconcile cancelled orphan SL %s for trade=%s %s",
                        oid,
                        trade.id,
                        leg.leg_type,
                    )
                except Exception as exc:
                    logger.warning(
                        "Reconcile could not cancel SL %s: %s", oid, exc
                    )
                leg.delta_sl_order_id = None
            realized = book_leg_close(
                leg=leg, trade=trade, exit_premium=exit_px, exit_time=now
            )
            changed = True
            logger.warning(
                "Reconcile: trade=%s %s strike=%s closed externally "
                "(Delta size=0) exit=%.2f realized=%.4f",
                trade.id,
                leg.leg_type,
                leg.strike,
                exit_px,
                realized,
            )

        if changed:
            reason = (
                ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value
                if len(flat_legs) == len(open_legs)
                else ExitReason.MANUAL_LEG_CLOSE.value
            )
            if finalize_trade_if_flat(db=db, trade=trade, exit_reason=reason):
                if int(trade.id) not in fully_closed:
                    fully_closed.append(int(trade.id))
                if position_tracker is not None:
                    position_tracker.mark_closed(int(trade.id))
            elif position_tracker is not None:
                all_legs = (
                    db.query(Leg)
                    .filter(
                        Leg.trade_id == trade.id,
                        Leg.is_bot_managed.is_(True),
                    )
                    .all()
                )
                call_leg, put_leg = pick_call_put_legs(all_legs)
                state = position_tracker.get(int(trade.id))
                if state is not None and call_leg is not None and put_leg is not None:
                    state.call_leg = call_leg
                    state.put_leg = put_leg
                    state.trade = trade
            db.commit()

    return {"fully_closed": fully_closed, "naked_risk": naked_risk}


def next_basket_number(db: Any, account_id: int) -> int:
    from sqlalchemy import func

    current = (
        db.query(func.max(Trade.basket_number))
        .filter(Trade.account_id == account_id)
        .scalar()
    )
    return int(current or 0) + 1
