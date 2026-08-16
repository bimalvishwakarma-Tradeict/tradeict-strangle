# trade_reconcile.py — Keep DB trade/leg status aligned with Delta open sizes

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.config import ExitReason, TradeStatus
from backend.core.delta_client import short_leg_realized_pnl
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
    exit_premium: float | None,
    exit_time: datetime | None = None,
    exit_fee_usd: float | None = None,
    exit_order_id: str | None = None,
) -> float:
    """
    Mark leg closed and set leg.realized_pnl.

    Does NOT mutate trade.realized_pnl — callers must run
    recompute_trade_realized_pnl() once after all legs are booked.

    Never overwrites an already-closed leg. Never writes exit_premium=0.0
    (that reads as a free buyback); pass None when the fill is unresolved.
    """
    from backend.config import OPTIONS_CONTRACT_VALUE

    leg_id = int(getattr(leg, "id", 0) or 0)
    existing_status = str(getattr(leg, "status", "") or "").lower()
    if existing_status == "closed":
        existing_px = getattr(leg, "exit_premium", None)
        attempted = exit_premium
        logger.warning(
            "[LEG_BOOK_SKIP] leg_id=%s existing_exit_premium=%s "
            "attempted_exit_premium=%s — leaving row untouched",
            leg_id,
            existing_px,
            attempted,
        )
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "LEG_BOOK_SKIP",
                int(getattr(trade, "id", 0) or 0),
                {
                    "leg_id": leg_id,
                    "existing_exit_premium": existing_px,
                    "attempted_exit_premium": attempted,
                },
            )
        except Exception:
            pass
        return float(getattr(leg, "realized_pnl", None) or 0.0)

    now = exit_time or datetime.now(timezone.utc)

    # Unresolved / invalid fill — close the leg but do not invent a free exit
    if exit_premium is None or float(exit_premium) <= 0.0:
        leg.status = "closed"
        leg.exit_time = now
        leg.exit_premium = None
        leg.realized_pnl = None
        if exit_order_id is not None:
            leg.exit_order_id = str(exit_order_id)
        if exit_fee_usd is not None:
            leg.exit_fee_usd = abs(float(exit_fee_usd))
        note_tag = f"PNL_UNRESOLVED_{getattr(leg, 'leg_type', 'leg')}"
        prior_notes = str(getattr(trade, "notes", None) or "")
        if note_tag not in prior_notes:
            trade.notes = (
                f"{prior_notes};{note_tag}".strip(";")
                if prior_notes
                else note_tag
            )
        logger.critical(
            "[LEG_BOOK_UNRESOLVED] trade=%s leg_id=%s %s — exit_premium "
            "unavailable; booked NULL (not 0.0)",
            getattr(trade, "id", "?"),
            leg_id,
            getattr(leg, "leg_type", "?"),
        )
        return 0.0

    exit_px = float(exit_premium)
    entry_px = float(leg.initial_premium or 0.0)
    qty = abs(int(leg.quantity or 0))
    cv = float(OPTIONS_CONTRACT_VALUE)
    if bool(getattr(leg, "is_long", False)):
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
    return realized


def recompute_trade_realized_pnl(db: Any, trade: Any) -> float:
    """
    Set trade.realized_pnl = sum of closed bot-managed legs' realized_pnl.

    Legs with exit_premium/realized still unresolved (NULL) contribute 0.
    """
    legs = (
        db.query(Leg)
        .filter(
            Leg.trade_id == int(trade.id),
            Leg.is_bot_managed.is_(True),
        )
        .all()
    )
    total = 0.0
    for leg in legs:
        if str(getattr(leg, "status", "") or "").lower() != "closed":
            continue
        rp = getattr(leg, "realized_pnl", None)
        if rp is None:
            continue
        total += float(rp)
    trade.realized_pnl = total
    return total


def pnl_sanity_check(
    *,
    trade_id: int,
    realized_pnl: float,
    last_gross_mtm: float | None,
    legs: list[Any] | None = None,
) -> bool:
    """
    Return True if OK. Log CRITICAL [PNL_SANITY_FAIL] when signs disagree.
    """
    from backend.config import PNL_SANITY_ABS_TOLERANCE_USD

    tol = float(PNL_SANITY_ABS_TOLERANCE_USD)
    gross = float(last_gross_mtm) if last_gross_mtm is not None else None
    realized = float(realized_pnl)
    if gross is None:
        return True
    if abs(gross) < tol or abs(realized) < tol:
        return True
    if (gross > 0 and realized > 0) or (gross < 0 and realized < 0):
        return True

    breakdown = []
    for leg in legs or []:
        breakdown.append(
            {
                "leg_id": int(getattr(leg, "id", 0) or 0),
                "leg_type": str(getattr(leg, "leg_type", "")),
                "entry": getattr(leg, "initial_premium", None),
                "exit": getattr(leg, "exit_premium", None),
                "realized": getattr(leg, "realized_pnl", None),
                "status": getattr(leg, "status", None),
            }
        )
    logger.critical(
        "[PNL_SANITY_FAIL] trade_id=%s realized_pnl=%.6f "
        "last_gross_mtm=%.6f legs=%s",
        trade_id,
        realized,
        gross,
        breakdown,
    )
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "PNL_SANITY_FAIL",
            int(trade_id),
            {
                "realized_pnl": round(realized, 6),
                "last_gross_mtm": round(gross, 6),
                "legs": breakdown,
            },
        )
    except Exception:
        pass
    return False


async def resolve_external_exit_fill(
    client: Any,
    leg: Any,
) -> float | None:
    """
    Fetch a real exit fill for a leg closed outside our order flow.

    Order: exit_order_id → product fills since entry_time → None.
    Never returns 0.0 as a stand-in for 'unknown'.
    """
    if client is None:
        return None

    oid = getattr(leg, "exit_order_id", None) or getattr(leg, "delta_order_id", None)
    # Prefer a dedicated exit order id when present
    exit_oid = getattr(leg, "exit_order_id", None)
    if exit_oid:
        try:
            px = float(await client.resolve_fill_price({"id": exit_oid}) or 0.0)
            if px > 0:
                return px
        except Exception as exc:
            logger.warning(
                "resolve_external_exit_fill order %s failed: %s", exit_oid, exc
            )

    pid = int(getattr(leg, "product_id", 0) or 0)
    if pid <= 0:
        return None

    entry_time = getattr(leg, "entry_time", None)
    try:
        px = await client.get_product_exit_fill_since(
            product_id=pid,
            since=entry_time,
            side="buy",  # buy-to-close short (or sell-to-close long handled inside)
            is_long=bool(getattr(leg, "is_long", False)),
        )
        if px is not None and float(px) > 0:
            return float(px)
    except Exception as exc:
        logger.warning(
            "resolve_external_exit_fill product %s failed: %s", pid, exc
        )
    return None


def finalize_trade_if_flat(
    *,
    db: Any,
    trade: Any,
    exit_reason: str = ExitReason.MANUAL_LEG_CLOSE.value,
) -> bool:
    """
    If no open bot legs remain, signal that the trade should be closed.

    Does NOT set Trade.status / exit_time / exit_reason — the caller must run
    close_master_trade (or equivalent funnel). Ensures realized_pnl is non-null.
    Returns True when the basket is flat and ready for funnel close.
    """
    if count_open_bot_legs(db, trade.id) > 0:
        return False
    if trade.realized_pnl is None:
        trade.realized_pnl = 0.0
    # Stash intended reason on the row only if empty — funnel will set it
    if not getattr(trade, "exit_reason", None):
        trade.exit_reason = exit_reason
    return True


def heal_zombie_active_trades(
    db: Any,
    position_tracker: Any | None = None,
) -> list[int]:
    """
    Detect ACTIVE trades with zero open bot-managed legs.

    Does NOT set Trade.status or remove from the tracker — the caller must run
    close_master_trade for each id. Returns trade ids pending funnel close.
    """
    pending_ids: list[int] = []
    actives = (
        db.query(Trade).filter(Trade.status == TradeStatus.ACTIVE.value).all()
    )
    for trade in actives:
        if count_open_bot_legs(db, trade.id) > 0:
            continue
        if trade.realized_pnl is None:
            trade.realized_pnl = 0.0
        if not trade.exit_reason:
            trade.exit_reason = ExitReason.MANUAL_LEG_CLOSE.value
        pending_ids.append(int(trade.id))
        logger.warning(
            "Detected zombie ACTIVE trade id=%s (no open bot legs) — "
            "pending funnel close (not marking CLOSED here)",
            trade.id,
        )
    if pending_ids:
        db.commit()
    return pending_ids


async def reconcile_open_legs_with_delta(
    *,
    db: Any,
    client: Any,
    position_tracker: Any | None = None,
) -> dict[str, Any]:
    """
    Align DB open legs with Delta sizes (detector only).

    Returns:
      {
        "fully_closed": [trade_id, ...],   # pending funnel close — NOT yet CLOSED
        "close_reasons": {trade_id: reason_str, ...},
        "naked_risk": [
          {"trade_id": int, "remaining": "call"|"put", "missing": "call"|"put"},
          ...
        ],
      }

    Does NOT set Trade.status. Caller must run close_master_trade for each id
    in fully_closed. Does NOT remove trades from the position tracker.

    IMPORTANT: If a 2-leg basket has exactly one leg flat on Delta, we do NOT
    mark it closed here and leave a naked remaining leg. Instead we return
    naked_risk so the bot can emergency-close the remaining leg + cancel SLs.
    """
    fully_closed: list[int] = []
    close_reasons: dict[int, str] = {}

    for tid in heal_zombie_active_trades(db, position_tracker):
        tid_i = int(tid)
        if tid_i not in fully_closed:
            fully_closed.append(tid_i)
        row = db.query(Trade).filter(Trade.id == tid_i).first()
        close_reasons[tid_i] = str(
            getattr(row, "exit_reason", None)
            or ExitReason.MANUAL_LEG_CLOSE.value
        )

    naked_risk: list[dict[str, Any]] = []

    # Detect tracker entries that are already flat / non-ACTIVE in DB —
    # queue for funnel; do not mark CLOSED or drop from tracker here.
    if position_tracker is not None:
        for state in list(position_tracker.get_all_active()):
            row = db.query(Trade).filter(Trade.id == state.trade_id).first()
            if row is None:
                # Orphan tracker entry — drop only; nothing to funnel
                position_tracker.mark_closed(state.trade_id)
                continue
            open_n = count_open_bot_legs(db, state.trade_id)
            status_l = str(row.status).lower()
            if status_l != TradeStatus.ACTIVE.value or open_n == 0:
                if status_l == TradeStatus.ACTIVE.value and open_n == 0:
                    if row.realized_pnl is None:
                        row.realized_pnl = 0.0
                    if not row.exit_reason:
                        row.exit_reason = ExitReason.MANUAL_LEG_CLOSE.value
                    db.commit()
                    tid_i = int(state.trade_id)
                    if tid_i not in fully_closed:
                        fully_closed.append(tid_i)
                    close_reasons[tid_i] = str(
                        row.exit_reason
                        or ExitReason.MANUAL_LEG_CLOSE.value
                    )
                    logger.info(
                        "Reconcile: trade %s ACTIVE+flat — pending funnel close",
                        state.trade_id,
                    )
                elif status_l != TradeStatus.ACTIVE.value:
                    # Already closed in DB somehow — still ensure funnel mirrors
                    tid_i = int(state.trade_id)
                    if tid_i not in fully_closed:
                        fully_closed.append(tid_i)
                    close_reasons[tid_i] = str(
                        getattr(row, "exit_reason", None)
                        or ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value
                    )
                    logger.info(
                        "Reconcile: trade %s already %s in DB — "
                        "pending funnel for slave mirror",
                        state.trade_id,
                        row.status,
                    )

    if client is None:
        return {
            "fully_closed": fully_closed,
            "close_reasons": close_reasons,
            "naked_risk": naked_risk,
        }

    try:
        positions = await client.get_positions()
    except Exception as exc:
        logger.warning("Delta positions fetch failed during reconcile: %s", exc)
        return {
            "fully_closed": fully_closed,
            "close_reasons": close_reasons,
            "naked_risk": naked_risk,
        }

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
        # Skip trades mid-adjustment — Delta shows temporary one-leg-missing
        if position_tracker is not None:
            state = position_tracker.get(int(trade.id))
            if state is not None and getattr(state, "is_adjusting", False):
                logger.info(
                    "[RECONCILE_SKIP] Trade#%s — is_adjusting=True, "
                    "skipping reconcile for this trade",
                    trade.id,
                )
                continue

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
            return str(getattr(leg, "leg_type", "") or "").lower() in (
                "call",
                "put",
            )

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
            exit_px = await resolve_external_exit_fill(client, leg)
            if exit_px is None:
                # Prefer live mark only as last resort for reconcile booking
                mark = mark_by_pid.get(pid, 0.0)
                if mark <= 0:
                    try:
                        mark = float(
                            await client.get_mark_price(str(leg.symbol))
                        )
                    except Exception:
                        mark = 0.0
                if mark > 0:
                    exit_px = mark
                    logger.warning(
                        "Reconcile trade=%s %s: using mark=%.4f "
                        "(no fill history)",
                        trade.id,
                        leg.leg_type,
                        mark,
                    )
                else:
                    logger.critical(
                        "Reconcile trade=%s %s: no fill and no mark — "
                        "booking exit_premium=NULL",
                        trade.id,
                        leg.leg_type,
                    )
            realized = book_leg_close(
                leg=leg, trade=trade, exit_premium=exit_px, exit_time=now
            )
            changed = True
            logger.warning(
                "Reconcile: trade=%s %s strike=%s closed externally "
                "(Delta size=0) exit=%s realized=%.4f",
                trade.id,
                leg.leg_type,
                leg.strike,
                exit_px,
                realized,
            )

        if changed:
            recompute_trade_realized_pnl(db, trade)
            reason = (
                ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value
                if len(flat_legs) == len(open_legs)
                else ExitReason.MANUAL_LEG_CLOSE.value
            )
            if finalize_trade_if_flat(db=db, trade=trade, exit_reason=reason):
                tid_i = int(trade.id)
                if tid_i not in fully_closed:
                    fully_closed.append(tid_i)
                close_reasons[tid_i] = reason
                logger.info(
                    "Reconcile: trade %s flat after booking — "
                    "pending funnel close reason=%s",
                    trade.id,
                    reason,
                )
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
                if (
                    state is not None
                    and call_leg is not None
                    and put_leg is not None
                ):
                    state.call_leg = call_leg
                    state.put_leg = put_leg
                    state.trade = trade
            db.commit()

    return {
        "fully_closed": fully_closed,
        "close_reasons": close_reasons,
        "naked_risk": naked_risk,
    }


def next_basket_number(db: Any, account_id: int) -> int:
    from sqlalchemy import func

    current = (
        db.query(func.max(Trade.basket_number))
        .filter(Trade.account_id == account_id)
        .scalar()
    )
    return int(current or 0) + 1
