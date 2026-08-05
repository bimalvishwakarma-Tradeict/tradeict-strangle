# routes_account.py — /api/account/* endpoints for connect, status, disconnect

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.encryption import decrypt, encrypt
from backend.core.time_utils import get_ist_now
from backend.database import get_db, get_or_create_auto_settings
from backend.models import Account
from backend.schemas import (
    AccountConnectRequest,
    AccountConnectResponse,
    AccountDisconnectResponse,
    AccountSettingsUpdate,
    AccountStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


@router.post("/connect", response_model=AccountConnectResponse)
async def connect_account(
    payload: AccountConnectRequest,
    db: Session = Depends(get_db),
) -> AccountConnectResponse:
    """
    Verify Delta API keys, then encrypt and persist.

    Keys are only saved if test_connection() succeeds.
    Same account name → UPDATE (no duplicate insert).
    """
    client = DeltaClient(payload.api_key, payload.api_secret)
    try:
        try:
            profile = await client.test_connection()
        except DeltaAPIError as exc:
            logger.error("Account connect failed: Delta API error %s", exc.status_code)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        try:
            wallet = await client.get_wallet_balance()
        except DeltaAPIError as exc:
            logger.error("Wallet balance fetch failed after connect: %s", exc.status_code)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        encrypted_key = encrypt(payload.api_key)
        encrypted_secret = encrypt(payload.api_secret)
        now_utc = datetime.now(timezone.utc)
        account_name = profile.get("account_name") or payload.name

        existing = db.query(Account).filter(Account.name == payload.name).first()
        if existing is not None:
            existing.api_key_encrypted = encrypted_key
            existing.api_secret_encrypted = encrypted_secret
            existing.is_active = True
            existing.last_connected_at = now_utc
            db.commit()
            db.refresh(existing)
            account = existing
            logger.info("Updated existing account id=%s name=%s", account.id, payload.name)
        else:
            account = Account(
                name=payload.name,
                api_key_encrypted=encrypted_key,
                api_secret_encrypted=encrypted_secret,
                strategy_id="S001",
                is_active=True,
                created_at=now_utc,
                last_connected_at=now_utc,
            )
            db.add(account)
            db.commit()
            db.refresh(account)
            logger.info("Created account id=%s name=%s", account.id, payload.name)

        return AccountConnectResponse(
            success=True,
            account_id=account.id,
            account_name=account_name,
            balance_usdt=float(wallet.get("balance_usdt", 0.0)),
        )
    finally:
        await client.close()


@router.get("/status", response_model=AccountStatusResponse)
async def account_status(db: Session = Depends(get_db)) -> AccountStatusResponse:
    """
    Return connection status for the first active account.

    If none: always return connected=false shape (never 404).
    """
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        app_settings = get_or_create_auto_settings(db)
        return AccountStatusResponse(
            connected=False,
            account_name="",
            balance_usdt=0.0,
            balance_inr=0.0,
            usd_inr_rate=float(app_settings.usd_inr_rate or 85.0),
            last_checked="",
        )

    app_settings = get_or_create_auto_settings(db)
    rate = float(app_settings.usd_inr_rate or 85.0)
    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
    client = DeltaClient(api_key, api_secret)
    try:
        try:
            profile = await client.test_connection()
            wallet = await client.get_wallet_balance()
        except DeltaAPIError as exc:
            logger.error("Account status check failed: %s", exc.status_code)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        account.last_connected_at = datetime.now(timezone.utc)
        db.commit()

        balance_usd = float(wallet.get("balance_usdt", 0.0))
        return AccountStatusResponse(
            connected=True,
            account_name=profile.get("account_name") or account.name,
            balance_usdt=balance_usd,
            balance_inr=round(balance_usd * rate, 2),
            usd_inr_rate=rate,
            last_checked=get_ist_now().isoformat(),
        )
    finally:
        await client.close()


@router.patch("/settings")
async def update_account_settings(
    payload: AccountSettingsUpdate,
    db: Session = Depends(get_db),
) -> dict:
    """Update global account display settings (USD→INR rate)."""
    settings = get_or_create_auto_settings(db)
    if payload.usd_inr_rate is not None:
        settings.usd_inr_rate = float(payload.usd_inr_rate)
        db.commit()
        db.refresh(settings)
        logger.info("USD/INR rate updated to %s", settings.usd_inr_rate)
    return {"success": True, "usd_inr_rate": float(settings.usd_inr_rate or 85.0)}


@router.delete("/disconnect", response_model=AccountDisconnectResponse)
async def disconnect_account(db: Session = Depends(get_db)) -> AccountDisconnectResponse:
    """Remove all stored accounts (encrypted keys wiped from DB)."""
    deleted = db.query(Account).delete()
    db.commit()
    logger.info("Disconnected accounts deleted=%s", deleted)
    return AccountDisconnectResponse(success=True, message="Disconnected")
