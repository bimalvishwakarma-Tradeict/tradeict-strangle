# routes_hedge.py — /api/hedge/* manual hedge lifecycle endpoints

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaClient
from backend.core.encryption import decrypt
from backend.database import get_db, get_or_create_auto_settings
from backend.engine.hedge_lifecycle import (
    HedgeCloseError,
    HedgeOpenError,
    VALID_HEDGE_EXIT_REASONS,
    build_active_hedge_live,
    close_hedge,
    get_active_hedge,
    get_hedge_theta_log_payload,
    get_live_hedge,
    hedge_to_dict,
    open_hedge,
)
from backend.models import Account, HedgePosition

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hedge", tags=["hedge"])

NO_ACCOUNT_DETAIL = "No account connected. Please add API keys in Settings."


class HedgeOpenRequest(BaseModel):
    """Optional body for POST /api/hedge/open."""

    quantity: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="Override lot size for a small live test (default: settings.quantity)",
    )


def _get_active_account(db: Session) -> Account:
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        raise HTTPException(status_code=401, detail=NO_ACCOUNT_DETAIL)
    return account


def _get_delta_client(account: Account) -> DeltaClient:
    return DeltaClient(
        decrypt(account.api_key_encrypted),
        decrypt(account.api_secret_encrypted),
    )


def _ist_iso(dt: Any) -> str | None:
    if dt is None:
        return None
    from datetime import datetime, timezone

    from backend.core.time_utils import get_ist_now

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(get_ist_now().tzinfo).isoformat()
    return str(dt)


@router.get("/structures")
async def list_structures(
    db: Session = Depends(get_db),
    limit: int = Query(40, ge=1, le=200),
) -> dict[str, Any]:
    """
    Hedge structures newest-first with linked baskets and adjustments.

    Uses stored structure_pnl / hedge_net_mtm / cum_closed_basket_pnl —
    does not recompute structure P&L here.
    """
    from backend.config import TradeStatus
    from backend.engine.bot_engine import bot_engine
    from backend.engine.hedge_lifecycle import (
        _open_basket_net_mtm,
        compute_structure_pnl_live,
    )
    from backend.engine.trade_reconcile import basket_realized_breakdown
    from backend.models import Adjustment, Leg, Trade

    account = _get_active_account(db)
    hedges = (
        db.query(HedgePosition)
        .filter(HedgePosition.account_id == int(account.id))
        .order_by(HedgePosition.id.desc())
        .limit(int(limit))
        .all()
    )

    structures: list[dict[str, Any]] = []
    for h in hedges:
        hid = int(h.id)
        hedge_status = str(h.status or "").lower().strip()
        hedge_net = float(getattr(h, "hedge_net_mtm", 0.0) or 0.0)
        # Closed structures: show booked realized (structure_pnl uses this too)
        if hedge_status == "closed" and h.realized_pnl is not None:
            hedge_net = float(h.realized_pnl)
        cum_closed = float(getattr(h, "cum_closed_basket_pnl", 0.0) or 0.0)

        baskets_orm = (
            db.query(Trade)
            .filter(Trade.hedge_position_id == hid)
            .order_by(Trade.id.asc())
            .all()
        )
        has_active_basket = any(
            str(t.status or "").lower() == TradeStatus.ACTIVE.value
            for t in baskets_orm
        )
        # Closed structure (or no live baskets): open bucket must be 0 —
        # never count a closed basket's stale MTM alongside its realized_pnl.
        if hedge_status == "closed" or not has_active_basket:
            open_basket = 0.0
        else:
            open_basket = _open_basket_net_mtm(
                db, hid, bot_engine.position_tracker
            )
        structure = compute_structure_pnl_live(
            hedge_net_mtm=hedge_net,
            booked_closed_pnl=cum_closed,
            open_basket_net_mtm=open_basket,
        )

        entry_cost = None
        if h.call_fill_price is not None and h.put_fill_price is not None:
            from backend.core.hedge_theta import CONTRACT_SIZE

            entry_cost = round(
                (
                    float(h.call_fill_price or 0)
                    + float(h.put_fill_price or 0)
                )
                * int(h.quantity)
                * float(CONTRACT_SIZE),
                4,
            )

        baskets_out: list[dict[str, Any]] = []
        for trade in baskets_orm:
            legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == int(trade.id),
                    Leg.is_bot_managed.is_(True),
                )
                .order_by(Leg.id.asc())
                .all()
            )
            call_leg = next(
                (
                    lg
                    for lg in legs
                    if str(lg.leg_type).lower() == "call"
                    and not bool(getattr(lg, "is_long", False))
                ),
                None,
            )
            put_leg = next(
                (
                    lg
                    for lg in legs
                    if str(lg.leg_type).lower() == "put"
                    and not bool(getattr(lg, "is_long", False))
                ),
                None,
            )
            # Prefer currently open short legs if multiple generations exist
            open_call = next(
                (
                    lg
                    for lg in legs
                    if str(lg.leg_type).lower() == "call"
                    and str(lg.status).lower() == "open"
                    and not bool(getattr(lg, "is_long", False))
                ),
                call_leg,
            )
            open_put = next(
                (
                    lg
                    for lg in legs
                    if str(lg.leg_type).lower() == "put"
                    and str(lg.status).lower() == "open"
                    and not bool(getattr(lg, "is_long", False))
                ),
                put_leg,
            )

            adjs = (
                db.query(Adjustment)
                .filter(Adjustment.trade_id == int(trade.id))
                .order_by(Adjustment.timestamp.asc())
                .all()
            )
            adj_rows = [
                {
                    "leg": a.leg_type,
                    "old_strike": float(a.old_strike),
                    "new_strike": float(a.new_strike),
                    "old_premium": float(a.old_exit_premium),
                    "new_premium": float(a.new_entry_premium),
                    "timestamp": _ist_iso(a.timestamp),
                }
                for a in adjs
            ]

            is_active = str(trade.status).lower() == TradeStatus.ACTIVE.value
            state = bot_engine.position_tracker.get(int(trade.id))
            net_mtm = None
            computed_at = None
            stale_seconds = None
            if state is not None and getattr(state, "last_mtm_snapshot", None):
                from backend.core.fees import basket_net_mtm_snapshot

                snap = state.last_mtm_snapshot
                refreshed = basket_net_mtm_snapshot(
                    gross_mtm=float(snap.get("gross_mtm", state.last_pnl) or 0),
                    fees_paid=float(snap.get("fees_paid", 0) or 0),
                    est_exit_fees=float(snap.get("est_exit_fees", 0) or 0),
                    slippage_pct=float(snap.get("slippage_pct", 0) or 0),
                    expected_exit_spread_usd=float(
                        snap.get("expected_exit_spread_usd", 0) or 0
                    ),
                    computed_at=getattr(state, "last_net_mtm_computed_at", None),
                )
                net_mtm = float(refreshed["net_mtm"])
                computed_at = refreshed.get("computed_at_iso")
                stale_seconds = refreshed.get("stale_seconds")
            elif state is not None and getattr(state, "last_net_mtm", None) is not None:
                net_mtm = float(state.last_net_mtm)
                at = getattr(state, "last_net_mtm_computed_at", None)
                if at is not None:
                    from backend.core.time_utils import get_utc_now

                    computed_at = at.isoformat()
                    stale_seconds = round(
                        max(0.0, (get_utc_now() - at).total_seconds()), 1
                    )
            elif not is_active and trade.realized_pnl is not None:
                from backend.core.fees import basket_net_mtm_snapshot

                closed_snap = basket_net_mtm_snapshot(
                    gross_mtm=float(trade.realized_pnl),
                    fees_paid=0.0,
                    est_exit_fees=0.0,
                    slippage_pct=0.0,
                    expected_exit_spread_usd=0.0,
                )
                net_mtm = float(closed_snap["net_mtm"])
                computed_at = closed_snap.get("computed_at_iso")
                stale_seconds = closed_snap.get("stale_seconds")

            seq = getattr(trade, "basket_seq_in_structure", None)
            pnl_breakdown = basket_realized_breakdown(legs, trade)
            baskets_out.append(
                {
                    "basket_seq_in_structure": (
                        int(seq) if seq is not None else None
                    ),
                    "trade_id": int(trade.id),
                    "status": trade.status,
                    "exit_reason": trade.exit_reason,
                    "entry_time": _ist_iso(trade.entry_time),
                    "exit_time": _ist_iso(trade.exit_time),
                    "call_strike": (
                        float(open_call.strike) if open_call is not None else None
                    ),
                    "put_strike": (
                        float(open_put.strike) if open_put is not None else None
                    ),
                    "call_entry_premium": (
                        float(open_call.initial_premium)
                        if open_call is not None
                        else None
                    ),
                    "put_entry_premium": (
                        float(open_put.initial_premium)
                        if open_put is not None
                        else None
                    ),
                    "realized_pnl": (
                        float(trade.realized_pnl)
                        if trade.realized_pnl is not None
                        else None
                    ),
                    "net_mtm": net_mtm,
                    "computed_at": computed_at,
                    "stale_seconds": stale_seconds,
                    **pnl_breakdown,
                    "legs": [
                        {
                            "id": int(lg.id),
                            "leg_type": lg.leg_type,
                            "strike": float(lg.strike),
                            "quantity": int(lg.quantity),
                            "entry_premium": float(lg.initial_premium),
                            "exit_premium": (
                                float(lg.exit_premium)
                                if lg.exit_premium is not None
                                else None
                            ),
                            "status": lg.status,
                            "realized_pnl": (
                                float(lg.realized_pnl)
                                if lg.realized_pnl is not None
                                else None
                            ),
                        }
                        for lg in legs
                    ],
                    "adjustments": adj_rows,
                }
            )

        open_basket_computed_at = None
        open_basket_stale_seconds = None
        if hedge_status != "closed" and has_active_basket:
            from backend.core.time_utils import get_utc_now

            now_utc = get_utc_now()
            for t in baskets_orm:
                if str(t.status or "").lower() != TradeStatus.ACTIVE.value:
                    continue
                st = bot_engine.position_tracker.get(int(t.id))
                at = getattr(st, "last_net_mtm_computed_at", None) if st else None
                if at is None:
                    continue
                open_basket_computed_at = at.isoformat()
                open_basket_stale_seconds = round(
                    max(0.0, (now_utc - at).total_seconds()), 1
                )
                break

        structures.append(
            {
                "hedge": {
                    "id": hid,
                    "strike": float(h.strike) if h.strike is not None else None,
                    "expiry": (
                        h.expiry_date.isoformat() if h.expiry_date else None
                    ),
                    "entry_cost": entry_cost,
                    "hedge_net_mtm": hedge_net,
                    "hedge_net_source": str(
                        getattr(h, "hedge_net_source", None) or "live"
                    ),
                    "realized_pnl": (
                        float(h.realized_pnl)
                        if h.realized_pnl is not None
                        else None
                    ),
                    "status": h.status,
                    "entry_time": _ist_iso(h.entry_time),
                    "exit_time": _ist_iso(h.exit_time),
                    "exit_reason": h.exit_reason,
                    "underlying": h.underlying,
                    "quantity": int(h.quantity),
                    "call_fill_price": h.call_fill_price,
                    "put_fill_price": h.put_fill_price,
                    "call_symbol": h.call_symbol,
                    "put_symbol": h.put_symbol,
                },
                "cum_closed_basket_pnl": cum_closed,
                "open_basket_net_mtm": open_basket,
                "open_basket_computed_at": open_basket_computed_at,
                "open_basket_stale_seconds": open_basket_stale_seconds,
                "structure_pnl": structure,
                "basket_count": len(baskets_out),
                "baskets": baskets_out,
            }
        )

    return {"success": True, "structures": structures}


@router.post("/open")
async def hedge_open(
    payload: HedgeOpenRequest = HedgeOpenRequest(),
    quantity: int | None = Query(
        None,
        ge=1,
        le=1000,
        description="Optional quantity override (also accepted in JSON body)",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually open the long ATM straddle hedge (test trigger — not auto-trade).

    Refuses if hedge_enabled is False or an active hedge already exists.
    """
    settings = get_or_create_auto_settings(db)
    if not bool(getattr(settings, "hedge_enabled", False)):
        raise HTTPException(
            status_code=400,
            detail=(
                "hedge_enabled is False. Enable Hedge Mode in Auto Trade "
                "settings before opening a hedge."
            ),
        )

    account = _get_active_account(db)
    und = str(settings.underlying or "BTC")
    existing = get_active_hedge(
        db, account_id=int(account.id), underlying=und
    )
    if existing is not None:
        return {
            "success": True,
            "created": False,
            "message": (
                f"Active hedge #{existing.id} already exists for {und} — "
                "refusing to open a second."
            ),
            "hedge": hedge_to_dict(existing),
        }

    qty_override = None
    if payload.quantity is not None:
        qty_override = int(payload.quantity)
    elif quantity is not None:
        qty_override = int(quantity)

    if (
        getattr(settings, "hedge_target_usd", None) is None
        or float(settings.hedge_target_usd or 0) <= 0
        or getattr(settings, "hedge_stoploss_usd", None) is None
        or float(settings.hedge_stoploss_usd or 0) <= 0
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "hedge_target_usd and hedge_stoploss_usd must be set (> 0) "
                "before opening a hedge."
            ),
        )

    client = _get_delta_client(account)
    try:
        try:
            hedge = await open_hedge(
                account,
                settings,
                db,
                client=client,
                quantity_override=qty_override,
                opened_via="manual",
            )
        except HedgeOpenError as exc:
            logger.critical(
                "[HEDGE_OPEN_FAIL] stage=%s reason=%s",
                exc.stage,
                exc.reason,
            )
            body: dict[str, Any] = {
                "success": False,
                "created": False,
                "message": str(exc),
                "stage": exc.stage,
                "detail": exc.reason,
            }
            if exc.hedge is not None:
                body["hedge"] = hedge_to_dict(exc.hedge)
            raise HTTPException(status_code=502, detail=body) from exc
        except Exception as exc:
            logger.critical(
                "[HEDGE_OPEN_FAIL] stage=api reason=%s",
                exc,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected hedge open failure: {exc}",
            ) from exc
    finally:
        await client.close()

    return {
        "success": True,
        "created": True,
        "message": (
            f"Hedge #{hedge.id} opened: {hedge.underlying} "
            f"{hedge.strike} straddle {hedge.expiry_date} × {hedge.quantity} lot(s)."
        ),
        "hedge": hedge_to_dict(hedge),
    }


class HedgeCloseRequest(BaseModel):
    """Optional body for POST /api/hedge/{id}/close."""

    reason: str = Field(
        default="HEDGE_MANUAL",
        description="HEDGE_TARGET | HEDGE_STOPLOSS | HEDGE_EXPIRY | HEDGE_MANUAL",
    )


class HedgeSettingsUpdate(BaseModel):
    """Partial update for an open hedge's target / stoploss (USD)."""

    target_usd: float | None = Field(default=None, gt=0)
    stoploss_usd: float | None = Field(default=None, gt=0)


@router.patch("/{hedge_id}/settings")
async def hedge_update_settings(
    hedge_id: int,
    payload: HedgeSettingsUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Update target_usd and/or stoploss_usd on hedge_positions.

    Takes effect on the next hedge monitor cycle (reads the row each tick).
    Auto Trade settings are defaults for the *next* hedge open only — they do
    not retro-apply here.
    """
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    hedge_row = (
        db.query(HedgePosition).filter(HedgePosition.id == int(hedge_id)).first()
    )
    if hedge_row is None:
        raise HTTPException(status_code=404, detail=f"Hedge #{hedge_id} not found")

    account = _get_active_account(db)
    if int(hedge_row.account_id) != int(account.id):
        raise HTTPException(
            status_code=403,
            detail="Hedge does not belong to the active account",
        )

    status = str(hedge_row.status or "").lower()
    if status not in {"active", "exit_failed", "partial", "error"}:
        raise HTTPException(
            status_code=400,
            detail=f"Hedge #{hedge_id} status={status} cannot update settings",
        )

    updated: dict[str, Any] = {}

    if "target_usd" in updates and updates["target_usd"] is not None:
        new_val = float(updates["target_usd"])
        if new_val <= 0:
            raise HTTPException(status_code=400, detail="target_usd must be > 0")
        old_val = hedge_row.target_usd
        hedge_row.target_usd = new_val
        updated["target_usd"] = new_val
        log_and_buffer(
            "HEDGE_SETTINGS_UPDATE",
            int(hedge_id),
            {
                "field": "target_usd",
                "old_value": old_val,
                "new_value": new_val,
            },
        )

    if "stoploss_usd" in updates and updates["stoploss_usd"] is not None:
        new_val = float(updates["stoploss_usd"])
        if new_val <= 0:
            raise HTTPException(status_code=400, detail="stoploss_usd must be > 0")
        old_val = hedge_row.stoploss_usd
        hedge_row.stoploss_usd = new_val
        updated["stoploss_usd"] = new_val
        log_and_buffer(
            "HEDGE_SETTINGS_UPDATE",
            int(hedge_id),
            {
                "field": "stoploss_usd",
                "old_value": old_val,
                "new_value": new_val,
            },
        )

    if not updated:
        raise HTTPException(status_code=400, detail="No settings provided")

    db.commit()
    db.refresh(hedge_row)

    return {
        "success": True,
        "hedge_id": int(hedge_row.id),
        "updated": updated,
        "hedge": hedge_to_dict(hedge_row),
        "message": (
            f"Hedge #{hedge_row.id} settings updated — "
            "monitor will use new values next cycle"
        ),
    }


@router.post("/{hedge_id}/close")
async def hedge_close(
    hedge_id: int,
    payload: HedgeCloseRequest = HedgeCloseRequest(),
    reason: str | None = Query(
        None,
        description="Override exit reason (default HEDGE_MANUAL)",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually close a hedge (both legs, verified flat, real fills).

    Default reason: HEDGE_MANUAL. Idempotent if already closed (HEDGE_CLOSE_SKIP).
    """
    exit_reason = str(reason or payload.reason or "HEDGE_MANUAL").upper().strip()
    if exit_reason not in VALID_HEDGE_EXIT_REASONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid reason '{exit_reason}'. "
                f"Valid: {', '.join(sorted(VALID_HEDGE_EXIT_REASONS))}"
            ),
        )

    hedge_row = (
        db.query(HedgePosition).filter(HedgePosition.id == int(hedge_id)).first()
    )
    if hedge_row is None:
        raise HTTPException(status_code=404, detail=f"Hedge #{hedge_id} not found")

    account = _get_active_account(db)
    if int(hedge_row.account_id) != int(account.id):
        raise HTTPException(
            status_code=403,
            detail="Hedge does not belong to the active account",
        )

    client = _get_delta_client(account)
    try:
        try:
            closed = await close_hedge(
                int(hedge_id),
                exit_reason,
                db,
                client=client,
            )
        except HedgeCloseError as exc:
            body: dict[str, Any] = {
                "success": False,
                "message": str(exc),
                "stage": exc.stage,
                "detail": exc.reason,
            }
            if exc.hedge is not None:
                body["hedge"] = hedge_to_dict(exc.hedge)
            raise HTTPException(status_code=502, detail=body) from exc
        except Exception as exc:
            logger.critical(
                "[HEDGE_CLOSE_FAIL] stage=api reason=%s",
                exc,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected hedge close failure: {exc}",
            ) from exc
    finally:
        await client.close()

    return {
        "success": True,
        "message": (
            f"Hedge #{closed.id} closed ({closed.exit_reason}). "
            f"realized_pnl={closed.realized_pnl}"
        ),
        "hedge": hedge_to_dict(closed),
        "realized_pnl": closed.realized_pnl,
        "exit_reason": closed.exit_reason,
    }


@router.get("/{hedge_id}/theta-log")
async def hedge_theta_log(
    hedge_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Daily theta/IV snapshots for a hedge plus accrued-theta ESTIMATE.

    theta_accrued_estimate is a sum of daily snapshots — not cash P&L.
    """
    hedge_row = (
        db.query(HedgePosition).filter(HedgePosition.id == int(hedge_id)).first()
    )
    if hedge_row is None:
        raise HTTPException(status_code=404, detail=f"Hedge #{hedge_id} not found")

    account = _get_active_account(db)
    if int(hedge_row.account_id) != int(account.id):
        raise HTTPException(
            status_code=403,
            detail="Hedge does not belong to the active account",
        )

    try:
        return get_hedge_theta_log_payload(db, hedge_id=int(hedge_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/active")
async def hedge_active(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Active hedge live panel payload, or hedge=null when none.

    P&L matches the monitor (long bids + same fee deduction).
    """
    settings = get_or_create_auto_settings(db)
    account = _get_active_account(db)
    hedge = get_live_hedge(
        db,
        account_id=int(account.id),
        underlying=str(settings.underlying or "BTC"),
    )
    if hedge is None:
        return {"success": True, "hedge": None, "message": "No active hedge"}

    client = _get_delta_client(account)
    try:
        live = await build_active_hedge_live(hedge, db, client=client)
    except Exception as exc:
        logger.critical(
            "[HEDGE_ACTIVE] live payload failed hedge=%s: %s",
            hedge.id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to build live hedge payload: {exc}",
        ) from exc
    finally:
        await client.close()

    return {
        "success": True,
        "hedge": live,
        "message": f"Active hedge #{live.get('id')}",
    }
