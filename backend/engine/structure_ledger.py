# structure_ledger.py — Record bot-placed legs with per-leg time windows (no P&L)
#
# opened_at / closed_at MUST be supplied by callers — captured immediately BEFORE
# the Delta order is placed, never after fill confirmation. The earner attributes
# wallet cashflow rows using [opened_at, closed_at]; a post-fill timestamp
# excludes the entry transaction.

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.core.bot_logger import log_and_buffer
from backend.core.time_utils import as_utc
from backend.models import Structure, StructureLeg

logger = logging.getLogger(__name__)

ROLE_HEDGE_CALL = "HEDGE_CALL"
ROLE_HEDGE_PUT = "HEDGE_PUT"
ROLE_BASKET_CALL = "BASKET_CALL"
ROLE_BASKET_PUT = "BASKET_PUT"

KIND_MASTER = "MASTER"
KIND_SLAVE = "SLAVE"


def _require_ts(value: Any, *, field: str) -> datetime:
    if value is None:
        raise ValueError(f"structure ledger {field} is required")
    aware = as_utc(value)
    if aware is None:
        raise ValueError(f"structure ledger {field} is invalid")
    return aware


def _iso(dt: Any) -> str:
    if dt is None:
        return "NA"
    aware = as_utc(dt)
    return aware.isoformat() if aware is not None else "NA"


def _log_leg(
    *,
    structure: Structure,
    leg: StructureLeg,
    action: str,
    order_placed_at: datetime,
    fill_at: Any = None,
) -> None:
    kind = str(structure.account_kind or "")
    details = {
        "structure": int(structure.id),
        "kind": kind,
        "slave": structure.slave_account_id,
        "role": leg.leg_role,
        "product_id": int(leg.product_id),
        "symbol": leg.symbol,
        "side": leg.side,
        "qty": int(leg.quantity),
        "action": action,
        "at": _iso(order_placed_at),
        "order_placed_at": _iso(order_placed_at),
        "fill_at": _iso(fill_at),
    }
    try:
        log_and_buffer("STRUCTURE_LEG", int(structure.hedge_position_id or 0), details)
    except Exception as exc:
        logger.warning("STRUCTURE_LEG buffer failed: %s", exc)
    logger.info(
        "[STRUCTURE_LEG] structure=%s | kind=%s | slave=%s | role=%s | "
        "product_id=%s | symbol=%s | side=%s | qty=%s | action=%s | at=%s | "
        "order_placed_at=%s | fill_at=%s",
        structure.id,
        kind,
        structure.slave_account_id,
        leg.leg_role,
        leg.product_id,
        leg.symbol,
        leg.side,
        leg.quantity,
        action,
        details["at"],
        details["order_placed_at"],
        details["fill_at"],
    )


def get_active_structure(
    db: Session,
    *,
    hedge_position_id: int,
    account_kind: str,
    slave_account_id: int | None = None,
) -> Structure | None:
    q = (
        db.query(Structure)
        .filter(
            Structure.hedge_position_id == int(hedge_position_id),
            Structure.account_kind == str(account_kind),
            Structure.status == "active",
        )
    )
    if account_kind == KIND_SLAVE:
        q = q.filter(Structure.slave_account_id == int(slave_account_id or 0))
    else:
        q = q.filter(Structure.slave_account_id.is_(None))
    return q.order_by(Structure.id.desc()).first()


def create_master_structure(
    db: Session,
    *,
    hedge_position_id: int,
    underlying: str,
    opened_at: Any,
) -> Structure:
    existing = get_active_structure(
        db,
        hedge_position_id=int(hedge_position_id),
        account_kind=KIND_MASTER,
    )
    if existing is not None:
        return existing
    at = _require_ts(opened_at, field="structure.opened_at")
    row = Structure(
        account_kind=KIND_MASTER,
        slave_account_id=None,
        earner_user_id=None,
        hedge_position_id=int(hedge_position_id),
        underlying=str(underlying or "").upper(),
        status="active",
        opened_at=at,
    )
    db.add(row)
    db.flush()
    return row


def create_slave_structure(
    db: Session,
    *,
    hedge_position_id: int,
    underlying: str,
    slave_account_id: int,
    earner_user_id: str | None,
    opened_at: Any,
) -> Structure:
    existing = get_active_structure(
        db,
        hedge_position_id=int(hedge_position_id),
        account_kind=KIND_SLAVE,
        slave_account_id=int(slave_account_id),
    )
    if existing is not None:
        return existing
    at = _require_ts(opened_at, field="structure.opened_at")
    row = Structure(
        account_kind=KIND_SLAVE,
        slave_account_id=int(slave_account_id),
        earner_user_id=(str(earner_user_id)[:64] if earner_user_id else None),
        hedge_position_id=int(hedge_position_id),
        underlying=str(underlying or "").upper(),
        status="active",
        opened_at=at,
    )
    db.add(row)
    db.flush()
    return row


def open_leg(
    db: Session,
    *,
    structure: Structure,
    leg_role: str,
    product_id: int,
    side: str,
    quantity: int,
    opened_at: Any,
    symbol: str | None = None,
    strike: float | None = None,
    basket_seq: int | None = None,
    adj_seq: int = 0,
    entry_order_id: str | None = None,
    fill_at: Any = None,
) -> StructureLeg:
    at = _require_ts(opened_at, field="leg.opened_at")
    leg = StructureLeg(
        structure_id=int(structure.id),
        leg_role=str(leg_role),
        basket_seq=int(basket_seq) if basket_seq is not None else None,
        adj_seq=int(adj_seq or 0),
        product_id=int(product_id),
        symbol=str(symbol)[:100] if symbol else None,
        strike=float(strike) if strike is not None else None,
        side=str(side).upper()[:4],
        quantity=abs(int(quantity or 0)),
        entry_order_id=str(entry_order_id)[:100] if entry_order_id else None,
        opened_at=at,
    )
    db.add(leg)
    db.flush()
    _log_leg(
        structure=structure,
        leg=leg,
        action="open",
        order_placed_at=at,
        fill_at=fill_at,
    )
    return leg


def _next_adj_seq(
    db: Session,
    *,
    structure_id: int,
    leg_role: str,
    basket_seq: int | None,
) -> int:
    q = (
        db.query(StructureLeg)
        .filter(
            StructureLeg.structure_id == int(structure_id),
            StructureLeg.leg_role == str(leg_role),
        )
    )
    if basket_seq is None:
        q = q.filter(StructureLeg.basket_seq.is_(None))
    else:
        q = q.filter(StructureLeg.basket_seq == int(basket_seq))
    rows = q.all()
    if not rows:
        return 0
    return max(int(r.adj_seq or 0) for r in rows) + 1


def find_open_leg(
    db: Session,
    *,
    structure_id: int,
    leg_role: str,
    basket_seq: int | None = None,
    product_id: int | None = None,
) -> StructureLeg | None:
    q = (
        db.query(StructureLeg)
        .filter(
            StructureLeg.structure_id == int(structure_id),
            StructureLeg.leg_role == str(leg_role),
            StructureLeg.closed_at.is_(None),
        )
    )
    if basket_seq is None:
        q = q.filter(StructureLeg.basket_seq.is_(None))
    else:
        q = q.filter(StructureLeg.basket_seq == int(basket_seq))
    if product_id is not None:
        q = q.filter(StructureLeg.product_id == int(product_id))
    return q.order_by(StructureLeg.adj_seq.desc(), StructureLeg.id.desc()).first()


def close_leg(
    db: Session,
    leg: StructureLeg,
    *,
    reason: str,
    closed_at: Any,
    structure: Structure | None = None,
    fill_at: Any = None,
) -> None:
    if leg.closed_at is not None:
        return
    at = _require_ts(closed_at, field="leg.closed_at")
    leg.closed_at = at
    leg.close_reason = str(reason or "")[:50]
    db.flush()
    struct = structure
    if struct is None:
        struct = (
            db.query(Structure)
            .filter(Structure.id == int(leg.structure_id))
            .first()
        )
    if struct is not None:
        _log_leg(
            structure=struct,
            leg=leg,
            action="close",
            order_placed_at=at,
            fill_at=fill_at,
        )


def close_open_legs(
    db: Session,
    *,
    structure: Structure,
    leg_roles: list[str] | None = None,
    basket_seq: int | None = None,
    reason: str,
    closed_at: Any,
    fill_at: Any = None,
) -> int:
    """Close matching open legs with one shared closed_at (same close batch)."""
    at = _require_ts(closed_at, field="closed_at")
    q = (
        db.query(StructureLeg)
        .filter(
            StructureLeg.structure_id == int(structure.id),
            StructureLeg.closed_at.is_(None),
        )
    )
    if leg_roles:
        q = q.filter(StructureLeg.leg_role.in_(list(leg_roles)))
    if basket_seq is not None:
        q = q.filter(StructureLeg.basket_seq == int(basket_seq))
    n = 0
    for leg in q.all():
        close_leg(
            db,
            leg,
            reason=reason,
            closed_at=at,
            structure=structure,
            fill_at=fill_at,
        )
        n += 1
    return n


def close_structure(
    db: Session,
    structure: Structure,
    *,
    reason: str,
    closed_at: Any,
) -> None:
    """
    Mark structure closed (caller should close legs first).

    Hard check: any leg still missing closed_at is force-closed with this
    batch timestamp and attribution_warning is set so earner can mark
    the structure SUSPECT.
    """
    if str(structure.status or "").lower() == "closed":
        return
    at = _require_ts(closed_at, field="structure.closed_at")
    open_legs = (
        db.query(StructureLeg)
        .filter(
            StructureLeg.structure_id == int(structure.id),
            StructureLeg.closed_at.is_(None),
        )
        .order_by(StructureLeg.id.asc())
        .all()
    )
    if open_legs:
        leg_bits = [
            (
                f"id={int(lg.id)} role={lg.leg_role} "
                f"product_id={int(lg.product_id)}"
            )
            for lg in open_legs
        ]
        warning = (
            "open_legs_at_structure_close: " + "; ".join(leg_bits)
        )[:2000]
        structure.attribution_warning = warning
        logger.error(
            "[LEDGER_MISS] slave=%s structure=%s reason=open_legs_at_close "
            "legs=%s -- force-closing windows; attribution_warning set",
            getattr(structure, "slave_account_id", None) or 0,
            int(structure.id),
            leg_bits,
        )
        log_and_buffer(
            "LEDGER_MISS",
            0,
            {
                "slave": getattr(structure, "slave_account_id", None) or 0,
                "structure": int(structure.id),
                "reason": "open_legs_at_close",
                "legs": leg_bits,
            },
        )
        for lg in open_legs:
            close_leg(
                db,
                lg,
                reason="STRUCTURE_CLOSE_OPEN_LEG",
                closed_at=at,
                structure=structure,
            )
    structure.status = "closed"
    structure.closed_at = at
    structure.close_reason = str(reason or "")[:50]
    db.flush()


def _close_role_leg(
    db: Session,
    *,
    structure: Structure,
    leg_role: str,
    basket_seq: int | None,
    reason: str,
    closed_at: Any,
    fill_at: Any = None,
    product_id: int | None = None,
) -> None:
    row = find_open_leg(
        db,
        structure_id=int(structure.id),
        leg_role=leg_role,
        basket_seq=basket_seq,
        product_id=product_id,
    )
    if closed_at is None:
        if row is not None:
            logger.error(
                "[LEDGER_MISS] slave=%s structure=%s leg=%s product_id=%s "
                "reason=no_closed_at -- leg window left OPEN",
                getattr(structure, "slave_account_id", None) or 0,
                int(structure.id),
                str(leg_role),
                int(row.product_id),
            )
            log_and_buffer(
                "LEDGER_MISS",
                0,
                {
                    "slave": getattr(structure, "slave_account_id", None) or 0,
                    "structure": int(structure.id),
                    "leg": str(leg_role),
                    "product_id": int(row.product_id),
                    "reason": "no_closed_at",
                    "note": "leg_window_left_OPEN",
                },
            )
        return
    if row is not None:
        close_leg(
            db,
            row,
            reason=reason,
            closed_at=closed_at,
            structure=structure,
            fill_at=fill_at,
        )


# --- High-level recording helpers (never raise into trading paths) ---


def record_master_hedge_open(
    db: Session,
    hedge: Any,
    *,
    structure_opened_at: Any,
    call_opened_at: Any,
    put_opened_at: Any,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> Structure | None:
    try:
        hid = int(getattr(hedge, "id", 0) or 0)
        if hid <= 0:
            return None
        call_pid = int(getattr(hedge, "call_product_id", 0) or 0)
        put_pid = int(getattr(hedge, "put_product_id", 0) or 0)
        if call_pid <= 0 or put_pid <= 0:
            return None
        qty = abs(int(getattr(hedge, "quantity", 1) or 1))
        struct = create_master_structure(
            db,
            hedge_position_id=hid,
            underlying=str(getattr(hedge, "underlying", "") or ""),
            opened_at=structure_opened_at,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_CALL,
            product_id=call_pid,
            side="BUY",
            quantity=qty,
            symbol=getattr(hedge, "call_symbol", None),
            strike=getattr(hedge, "strike", None),
            entry_order_id=getattr(hedge, "call_order_id", None),
            opened_at=call_opened_at,
            fill_at=call_fill_at,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_PUT,
            product_id=put_pid,
            side="BUY",
            quantity=qty,
            symbol=getattr(hedge, "put_symbol", None),
            strike=getattr(hedge, "strike", None),
            entry_order_id=getattr(hedge, "put_order_id", None),
            opened_at=put_opened_at,
            fill_at=put_fill_at,
        )
        db.flush()
        return struct
    except Exception as exc:
        logger.error(
            "structure ledger master hedge open failed: %s",
            exc,
            exc_info=True,
        )
        return None


def record_slave_hedge_open(
    db: Session,
    *,
    slave_hedge: Any,
    slave_account: Any,
    structure_opened_at: Any,
    call_opened_at: Any,
    put_opened_at: Any,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> Structure | None:
    try:
        master_hid = int(getattr(slave_hedge, "master_hedge_id", 0) or 0)
        slave_id = int(getattr(slave_account, "id", 0) or 0)
        if master_hid <= 0 or slave_id <= 0:
            return None
        call_pid = int(getattr(slave_hedge, "call_product_id", 0) or 0)
        put_pid = int(getattr(slave_hedge, "put_product_id", 0) or 0)
        if call_pid <= 0 or put_pid <= 0:
            return None
        qty = abs(int(getattr(slave_hedge, "quantity", 1) or 1))
        struct = create_slave_structure(
            db,
            hedge_position_id=master_hid,
            underlying=str(getattr(slave_hedge, "underlying", "") or ""),
            slave_account_id=slave_id,
            earner_user_id=getattr(slave_account, "earner_user_id", None),
            opened_at=structure_opened_at,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_CALL,
            product_id=call_pid,
            side="BUY",
            quantity=qty,
            symbol=getattr(slave_hedge, "call_symbol", None),
            strike=getattr(slave_hedge, "strike", None),
            entry_order_id=getattr(slave_hedge, "call_order_id", None),
            opened_at=call_opened_at,
            fill_at=call_fill_at,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_PUT,
            product_id=put_pid,
            side="BUY",
            quantity=qty,
            symbol=getattr(slave_hedge, "put_symbol", None),
            strike=getattr(slave_hedge, "strike", None),
            entry_order_id=getattr(slave_hedge, "put_order_id", None),
            opened_at=put_opened_at,
            fill_at=put_fill_at,
        )
        db.flush()
        return struct
    except Exception as exc:
        logger.error(
            "structure ledger slave hedge open failed: %s",
            exc,
            exc_info=True,
        )
        return None


def record_master_hedge_close(
    db: Session,
    hedge: Any,
    *,
    reason: str,
    call_closed_at: Any,
    put_closed_at: Any,
    structure_closed_at: Any,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> None:
    try:
        hid = int(getattr(hedge, "id", 0) or 0)
        if hid <= 0:
            return
        struct = get_active_structure(
            db, hedge_position_id=hid, account_kind=KIND_MASTER
        )
        if struct is None:
            return
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_CALL,
            basket_seq=None,
            reason=reason,
            closed_at=call_closed_at,
            fill_at=call_fill_at,
        )
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_PUT,
            basket_seq=None,
            reason=reason,
            closed_at=put_closed_at,
            fill_at=put_fill_at,
        )
        close_structure(
            db, struct, reason=reason, closed_at=structure_closed_at
        )
    except Exception as exc:
        logger.error(
            "structure ledger master hedge close failed: %s",
            exc,
            exc_info=True,
        )


def record_slave_hedge_close(
    db: Session,
    *,
    slave_hedge: Any,
    slave_account_id: int,
    reason: str,
    call_closed_at: Any,
    put_closed_at: Any,
    structure_closed_at: Any,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> None:
    try:
        master_hid = int(getattr(slave_hedge, "master_hedge_id", 0) or 0)
        if master_hid <= 0:
            return
        struct = get_active_structure(
            db,
            hedge_position_id=master_hid,
            account_kind=KIND_SLAVE,
            slave_account_id=int(slave_account_id),
        )
        if struct is None:
            return
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_CALL,
            basket_seq=None,
            reason=reason,
            closed_at=call_closed_at,
            fill_at=call_fill_at,
        )
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_HEDGE_PUT,
            basket_seq=None,
            reason=reason,
            closed_at=put_closed_at,
            fill_at=put_fill_at,
        )
        close_structure(
            db, struct, reason=reason, closed_at=structure_closed_at
        )
    except Exception as exc:
        logger.error(
            "structure ledger slave hedge close failed: %s",
            exc,
            exc_info=True,
        )


def record_master_basket_entry(
    db: Session,
    trade: Any,
    call_leg: Any,
    put_leg: Any,
    *,
    call_opened_at: Any,
    put_opened_at: Any,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> None:
    try:
        hid = getattr(trade, "hedge_position_id", None)
        if hid is None:
            return
        struct = get_active_structure(
            db, hedge_position_id=int(hid), account_kind=KIND_MASTER
        )
        if struct is None:
            return
        basket_seq = getattr(trade, "basket_seq_in_structure", None)
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_CALL,
            product_id=int(call_leg.product_id),
            side="SELL",
            quantity=abs(int(call_leg.quantity or 0)),
            symbol=getattr(call_leg, "symbol", None),
            strike=getattr(call_leg, "strike", None),
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            adj_seq=0,
            entry_order_id=getattr(call_leg, "delta_order_id", None),
            opened_at=call_opened_at,
            fill_at=call_fill_at,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_PUT,
            product_id=int(put_leg.product_id),
            side="SELL",
            quantity=abs(int(put_leg.quantity or 0)),
            symbol=getattr(put_leg, "symbol", None),
            strike=getattr(put_leg, "strike", None),
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            adj_seq=0,
            entry_order_id=getattr(put_leg, "delta_order_id", None),
            opened_at=put_opened_at,
            fill_at=put_fill_at,
        )
        db.flush()
    except Exception as exc:
        logger.error(
            "structure ledger master basket entry failed: %s",
            exc,
            exc_info=True,
        )


def record_slave_basket_entry(
    db: Session,
    *,
    slave_trade: Any,
    slave_account_id: int,
    master_trade: Any,
    call_opened_at: Any,
    put_opened_at: Any,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> None:
    try:
        hid = getattr(master_trade, "hedge_position_id", None)
        if hid is None:
            logger.error(
                "[LEDGER_MISS] slave=%s reason=no_hedge_position_id -- "
                "basket entry NOT recorded",
                slave_account_id,
            )
            return
        struct = get_active_structure(
            db,
            hedge_position_id=int(hid),
            account_kind=KIND_SLAVE,
            slave_account_id=int(slave_account_id),
        )
        if struct is None:
            logger.error(
                "[LEDGER_MISS] slave=%s reason=no_active_structure -- "
                "basket entry NOT recorded",
                slave_account_id,
            )
            return
        basket_seq = getattr(master_trade, "basket_seq_in_structure", None)
        call_pid = int(getattr(slave_trade, "call_product_id", 0) or 0)
        put_pid = int(getattr(slave_trade, "put_product_id", 0) or 0)
        if call_pid <= 0 or put_pid <= 0:
            return
        qty = abs(int(getattr(slave_trade, "actual_quantity", 1) or 1))
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_CALL,
            product_id=call_pid,
            side="SELL",
            quantity=qty,
            symbol=getattr(slave_trade, "call_symbol", None),
            strike=getattr(slave_trade, "call_strike", None),
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            adj_seq=0,
            entry_order_id=getattr(slave_trade, "call_order_id", None),
            opened_at=call_opened_at,
            fill_at=call_fill_at,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_PUT,
            product_id=put_pid,
            side="SELL",
            quantity=qty,
            symbol=getattr(slave_trade, "put_symbol", None),
            strike=getattr(slave_trade, "put_strike", None),
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            adj_seq=0,
            entry_order_id=getattr(slave_trade, "put_order_id", None),
            opened_at=put_opened_at,
            fill_at=put_fill_at,
        )
        db.flush()
    except Exception as exc:
        logger.error(
            "structure ledger slave basket entry failed: %s",
            exc,
            exc_info=True,
        )


def record_master_basket_exit(
    db: Session,
    trade: Any,
    *,
    reason: str,
    call_closed_at: Any = None,
    put_closed_at: Any = None,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> None:
    try:
        hid = getattr(trade, "hedge_position_id", None)
        if hid is None:
            return
        struct = get_active_structure(
            db, hedge_position_id=int(hid), account_kind=KIND_MASTER
        )
        if struct is None:
            struct = (
                db.query(Structure)
                .filter(
                    Structure.hedge_position_id == int(hid),
                    Structure.account_kind == KIND_MASTER,
                )
                .order_by(Structure.id.desc())
                .first()
            )
            if struct is None:
                return
        basket_seq = getattr(trade, "basket_seq_in_structure", None)
        bs = int(basket_seq) if basket_seq is not None else None
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_CALL,
            basket_seq=bs,
            reason=reason,
            closed_at=call_closed_at,
            fill_at=call_fill_at,
        )
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_PUT,
            basket_seq=bs,
            reason=reason,
            closed_at=put_closed_at,
            fill_at=put_fill_at,
        )
    except Exception as exc:
        logger.error(
            "structure ledger master basket exit failed: %s",
            exc,
            exc_info=True,
        )


def record_slave_basket_exit(
    db: Session,
    *,
    slave_trade: Any,
    slave_account_id: int,
    master_trade: Any | None,
    reason: str,
    call_closed_at: Any = None,
    put_closed_at: Any = None,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> None:
    try:
        hid = None
        basket_seq = None
        if master_trade is not None:
            hid = getattr(master_trade, "hedge_position_id", None)
            basket_seq = getattr(master_trade, "basket_seq_in_structure", None)
        if hid is None:
            return
        struct = get_active_structure(
            db,
            hedge_position_id=int(hid),
            account_kind=KIND_SLAVE,
            slave_account_id=int(slave_account_id),
        )
        if struct is None:
            struct = (
                db.query(Structure)
                .filter(
                    Structure.hedge_position_id == int(hid),
                    Structure.account_kind == KIND_SLAVE,
                    Structure.slave_account_id == int(slave_account_id),
                )
                .order_by(Structure.id.desc())
                .first()
            )
            if struct is None:
                return
        bs = int(basket_seq) if basket_seq is not None else None
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_CALL,
            basket_seq=bs,
            reason=reason,
            closed_at=call_closed_at,
            fill_at=call_fill_at,
        )
        _close_role_leg(
            db,
            structure=struct,
            leg_role=ROLE_BASKET_PUT,
            basket_seq=bs,
            reason=reason,
            closed_at=put_closed_at,
            fill_at=put_fill_at,
        )
    except Exception as exc:
        logger.error(
            "structure ledger slave basket exit failed: %s",
            exc,
            exc_info=True,
        )


def record_master_adjustment(
    db: Session,
    trade: Any,
    *,
    old_leg: Any,
    new_leg: Any,
    reason: str = "ADJUSTMENT",
    old_leg_closed_at: Any,
    new_leg_opened_at: Any,
    old_leg_fill_at: Any = None,
    new_leg_fill_at: Any = None,
) -> None:
    try:
        hid = getattr(trade, "hedge_position_id", None)
        if hid is None:
            return
        struct = get_active_structure(
            db, hedge_position_id=int(hid), account_kind=KIND_MASTER
        )
        if struct is None:
            return
        basket_seq = getattr(trade, "basket_seq_in_structure", None)
        lt = str(getattr(old_leg, "leg_type", "") or "").lower()
        role = ROLE_BASKET_CALL if lt == "call" else ROLE_BASKET_PUT
        old_pid = int(getattr(old_leg, "product_id", 0) or 0)
        open_row = find_open_leg(
            db,
            structure_id=int(struct.id),
            leg_role=role,
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            product_id=old_pid if old_pid > 0 else None,
        )
        if open_row is not None:
            close_leg(
                db,
                open_row,
                reason=reason,
                closed_at=old_leg_closed_at,
                structure=struct,
                fill_at=old_leg_fill_at,
            )
        adj = _next_adj_seq(
            db,
            structure_id=int(struct.id),
            leg_role=role,
            basket_seq=int(basket_seq) if basket_seq is not None else None,
        )
        open_leg(
            db,
            structure=struct,
            leg_role=role,
            product_id=int(new_leg.product_id),
            side="SELL",
            quantity=abs(int(getattr(new_leg, "quantity", 0) or 0)),
            symbol=getattr(new_leg, "symbol", None),
            strike=getattr(new_leg, "strike", None),
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            adj_seq=adj,
            entry_order_id=getattr(new_leg, "delta_order_id", None),
            opened_at=new_leg_opened_at,
            fill_at=new_leg_fill_at,
        )
        db.flush()
    except Exception as exc:
        logger.error(
            "structure ledger master adjustment failed: %s",
            exc,
            exc_info=True,
        )


def record_slave_adjustment(
    db: Session,
    *,
    slave_trade: Any,
    slave_account_id: int,
    master_trade: Any | None,
    triggered_leg: str,
    new_product_id: int,
    new_symbol: str,
    new_strike: float,
    new_order_id: str | None,
    reason: str = "ADJUSTMENT",
    old_leg_closed_at: Any,
    new_leg_opened_at: Any,
    old_leg_fill_at: Any = None,
    new_leg_fill_at: Any = None,
) -> None:
    try:
        if master_trade is None:
            return
        hid = getattr(master_trade, "hedge_position_id", None)
        if hid is None:
            return
        struct = get_active_structure(
            db,
            hedge_position_id=int(hid),
            account_kind=KIND_SLAVE,
            slave_account_id=int(slave_account_id),
        )
        if struct is None:
            return
        basket_seq = getattr(master_trade, "basket_seq_in_structure", None)
        leg = str(triggered_leg or "").lower()
        role = ROLE_BASKET_CALL if leg == "call" else ROLE_BASKET_PUT
        open_row = find_open_leg(
            db,
            structure_id=int(struct.id),
            leg_role=role,
            basket_seq=int(basket_seq) if basket_seq is not None else None,
        )
        if open_row is not None:
            close_leg(
                db,
                open_row,
                reason=reason,
                closed_at=old_leg_closed_at,
                structure=struct,
                fill_at=old_leg_fill_at,
            )
        adj = _next_adj_seq(
            db,
            structure_id=int(struct.id),
            leg_role=role,
            basket_seq=int(basket_seq) if basket_seq is not None else None,
        )
        qty = abs(int(getattr(slave_trade, "actual_quantity", 1) or 1))
        open_leg(
            db,
            structure=struct,
            leg_role=role,
            product_id=int(new_product_id),
            side="SELL",
            quantity=qty,
            symbol=new_symbol,
            strike=float(new_strike) if new_strike is not None else None,
            basket_seq=int(basket_seq) if basket_seq is not None else None,
            adj_seq=adj,
            entry_order_id=new_order_id,
            opened_at=new_leg_opened_at,
            fill_at=new_leg_fill_at,
        )
        db.flush()
    except Exception as exc:
        logger.error(
            "structure ledger slave adjustment failed: %s",
            exc,
            exc_info=True,
        )
