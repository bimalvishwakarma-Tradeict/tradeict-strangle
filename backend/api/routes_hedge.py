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
    hedge = get_active_hedge(
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
