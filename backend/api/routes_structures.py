# routes_structures.py — Structure ledger read API (identifiers + windows ONLY)
#
# CRITICAL: This module must NEVER return realized_pnl, net_mtm, premiums as P&L,
# or any derived money value. The earner app computes every P&L figure from the
# customer's own Delta wallet (cashflow + commission) using product_id + time
# windows from this API. Adding a P&L field here would silently reintroduce the
# mismatch bug this redesign exists to remove.

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from backend.core.time_utils import as_utc
from backend.database import get_db
from backend.engine.ledger_reconcile import counts_by_kind, reconcile_ledger
from backend.models import Structure, StructureLeg

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/structures", tags=["structures"])


class LegAttributionFromUpdate(BaseModel):
    attribution_from: str = Field(
        ...,
        description="ISO8601 UTC — earner attribution window start (must be <= opened_at)",
    )


def _parse_utc_iso(value: str, *, param: str = "since") -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise HTTPException(
            status_code=422,
            detail=f"{param} is required (ISO8601 UTC)",
        )
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid ISO8601 timestamp for {param}: {value}",
        ) from exc
    aware = as_utc(dt)
    if aware is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid ISO8601 timestamp for {param}: {value}",
        )
    return aware


def _utc_iso(dt: datetime | None) -> str | None:
    """Serialize DB timestamp as ISO8601 with explicit UTC offset."""
    if dt is None:
        return None
    aware = as_utc(dt)
    if aware is None:
        return None
    return aware.isoformat()


def _serialize_leg(leg: StructureLeg) -> dict[str, Any]:
    return {
        "id": int(leg.id),
        "leg_role": str(leg.leg_role or ""),
        "basket_seq": leg.basket_seq,
        "adj_seq": int(leg.adj_seq or 0),
        "product_id": int(leg.product_id),
        "symbol": leg.symbol,
        "strike": leg.strike,
        "side": str(leg.side or ""),
        "quantity": int(leg.quantity or 0),
        "entry_order_id": leg.entry_order_id,
        "opened_at": _utc_iso(leg.opened_at),
        "attribution_from": _utc_iso(leg.attribution_from),
        "closed_at": _utc_iso(leg.closed_at),
        "close_reason": leg.close_reason,
    }


def _serialize_structure(row: Structure) -> dict[str, Any]:
    legs = sorted(
        list(row.legs or []),
        key=lambda lg: (
            lg.basket_seq if lg.basket_seq is not None else -1,
            str(lg.leg_role or ""),
            int(lg.adj_seq or 0),
            int(lg.id or 0),
        ),
    )
    return {
        "id": int(row.id),
        "account_kind": str(row.account_kind or ""),
        "slave_account_id": row.slave_account_id,
        "earner_user_id": row.earner_user_id,
        "hedge_position_id": int(row.hedge_position_id),
        "underlying": str(row.underlying or ""),
        "status": str(row.status or ""),
        "opened_at": _utc_iso(row.opened_at),
        "closed_at": _utc_iso(row.closed_at),
        "close_reason": row.close_reason,
        "attribution_warning": getattr(row, "attribution_warning", None),
        "legs": [_serialize_leg(lg) for lg in legs],
    }


def _apply_structure_filters(
    q: Any,
    *,
    account_kind: str | None,
    slave_id: int | None,
    earner_user_id: str | None,
    since: datetime | None,
    status: str | None,
) -> Any:
    if account_kind:
        kind = str(account_kind).upper().strip()
        if kind not in {"MASTER", "SLAVE"}:
            raise HTTPException(
                status_code=422,
                detail="account_kind must be MASTER or SLAVE",
            )
        q = q.filter(Structure.account_kind == kind)
    if slave_id is not None:
        q = q.filter(Structure.slave_account_id == int(slave_id))
    if earner_user_id:
        q = q.filter(Structure.earner_user_id == str(earner_user_id).strip())
    if since is not None:
        q = q.filter(Structure.opened_at >= since)
    if status:
        st = str(status).lower().strip()
        if st not in {"active", "closed"}:
            raise HTTPException(
                status_code=422,
                detail="status must be active or closed",
            )
        q = q.filter(Structure.status == st)
    return q


@router.get("")
async def list_structures(
    db: Session = Depends(get_db),
    account_kind: str | None = Query(None, description="MASTER or SLAVE"),
    slave_id: int | None = Query(None, ge=1),
    earner_user_id: str | None = Query(None, max_length=64),
    since: str | None = Query(
        None, description="ISO8601 UTC — structures with opened_at >= since"
    ),
    status: str | None = Query(None, description="active or closed"),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """
    Structure ledger rows newest-first.

    Identifiers and per-leg time windows only — no P&L or money fields.
    """
    since_dt: datetime | None = None
    if since:
        since_dt = _parse_utc_iso(since, param="since")

    q = (
        db.query(Structure)
        .options(joinedload(Structure.legs))
        .order_by(Structure.opened_at.desc(), Structure.id.desc())
    )
    q = _apply_structure_filters(
        q,
        account_kind=account_kind,
        slave_id=slave_id,
        earner_user_id=earner_user_id,
        since=since_dt,
        status=status,
    )
    rows = q.limit(int(limit)).all()
    return {
        "success": True,
        "structures": [_serialize_structure(r) for r in rows],
    }


@router.get("/changes")
async def list_structure_changes(
    db: Session = Depends(get_db),
    since: str = Query(..., description="ISO8601 UTC — required poll watermark"),
    account_kind: str | None = Query(None, description="MASTER or SLAVE"),
    slave_id: int | None = Query(None, ge=1),
    earner_user_id: str | None = Query(None, max_length=64),
    status: str | None = Query(None, description="active or closed"),
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    """
    Incremental sync: structures with any leg opened or closed after ``since``.

    Same payload shape as GET /api/structures — identifiers and windows only.
    """
    since_dt = _parse_utc_iso(since, param="since")

    changed_ids = (
        db.query(StructureLeg.structure_id)
        .filter(
            or_(
                StructureLeg.opened_at >= since_dt,
                StructureLeg.closed_at >= since_dt,
            )
        )
        .distinct()
        .subquery()
    )

    q = (
        db.query(Structure)
        .options(joinedload(Structure.legs))
        .filter(Structure.id.in_(changed_ids))
        .order_by(Structure.opened_at.desc(), Structure.id.desc())
    )
    q = _apply_structure_filters(
        q,
        account_kind=account_kind,
        slave_id=slave_id,
        earner_user_id=earner_user_id,
        since=None,
        status=status,
    )
    rows = q.limit(int(limit)).all()
    return {
        "success": True,
        "since": since_dt.astimezone(timezone.utc).isoformat(),
        "structures": [_serialize_structure(r) for r in rows],
    }


@router.get("/reconcile")
async def reconcile_structures(db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Admin: fresh scan for ledger gaps billing cannot see.

    Identifiers only — no P&L. Read-only; nothing is persisted.
    """
    try:
        findings = reconcile_ledger(db)
    except Exception as exc:
        logger.critical(
            "structure reconcile endpoint failed: %s", exc, exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Internal server error"
        ) from exc
    return {
        "success": True,
        "findings": findings,
        "counts": counts_by_kind(findings),
        "total": len(findings),
    }


@router.patch("/legs/{leg_id}/attribution-from")
async def patch_leg_attribution_from(
    leg_id: int,
    payload: LegAttributionFromUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Admin: set earner attribution window start for a closed leg.

    Does not modify opened_at or closed_at. New value must not be after
    opened_at (window may only shift backward).
    """
    leg = db.query(StructureLeg).filter(StructureLeg.id == int(leg_id)).first()
    if leg is None:
        raise HTTPException(status_code=404, detail=f"Structure leg {leg_id} not found")

    if leg.closed_at is None:
        raise HTTPException(
            status_code=422,
            detail="Leg must be closed before setting attribution_from",
        )

    new_ts = _parse_utc_iso(payload.attribution_from, param="attribution_from")
    opened = as_utc(leg.opened_at)
    if opened is None:
        raise HTTPException(status_code=422, detail="Leg opened_at is invalid")

    if new_ts > opened:
        raise HTTPException(
            status_code=422,
            detail=(
                "attribution_from must not be after opened_at "
                "(window may only shift backward)"
            ),
        )

    old_raw = leg.attribution_from
    old_iso = _utc_iso(old_raw)
    new_iso = _utc_iso(new_ts)

    leg.attribution_from = new_ts
    db.commit()
    db.refresh(leg)

    logger.warning(
        "structure leg attribution_from updated leg_id=%s old=%s new=%s",
        int(leg_id),
        old_iso,
        new_iso,
    )

    return {
        "success": True,
        "leg": _serialize_leg(leg),
    }
