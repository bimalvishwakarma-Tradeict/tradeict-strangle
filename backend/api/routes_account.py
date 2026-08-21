# routes_account.py — /api/account/* endpoints for connect, status, disconnect

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.encryption import decrypt, encrypt
from backend.core.time_utils import get_ist_now, get_utc_now
from backend.database import get_db, get_or_create_auto_settings
from backend.models import Account, Leg, Trade
from backend.schemas import (
    AccountConnectRequest,
    AccountConnectResponse,
    AccountDisconnectResponse,
    AccountSettingsUpdate,
    AccountStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

async def _protected_product_ids_for_master(db: Session, client: DeltaClient) -> set[int]:
    """
    Product ids that still have a live short/bot position — do NOT cancel their
    standalone stop orders yet (e.g. Trade#65 while still open).
    """
    protected: set[int] = {
        int(row[0])
        for row in (
            db.query(Leg.product_id)
            .filter(Leg.status == "open", Leg.is_bot_managed.is_(True))
            .all()
        )
        if row and row[0]
    }
    try:
        for pos in await client.get_positions():
            size = int(pos.get("size") or 0)
            pid = int(pos.get("product_id") or 0)
            if size != 0 and pid > 0:
                protected.add(pid)
    except Exception as exc:
        logger.warning("Orphan SL: master positions fetch failed: %s", exc)
    return protected


async def _protected_product_ids_for_slave(
    db: Session,
    client: DeltaClient,
    slave_id: int,
) -> set[int]:
    """Keep stops for products still open on the slave or linked via active SlaveTrade."""
    from backend.models import SlaveTrade

    protected: set[int] = set()
    try:
        for pos in await client.get_positions():
            size = int(pos.get("size") or 0)
            pid = int(pos.get("product_id") or 0)
            if size != 0 and pid > 0:
                protected.add(pid)
    except Exception as exc:
        logger.warning(
            "Orphan SL: slave %s positions fetch failed: %s", slave_id, exc
        )

    active_master_ids = [
        int(row[0])
        for row in (
            db.query(SlaveTrade.master_trade_id)
            .filter(
                SlaveTrade.slave_account_id == int(slave_id),
                SlaveTrade.status == "active",
            )
            .all()
        )
        if row and row[0]
    ]
    if active_master_ids:
        for row in (
            db.query(Leg.product_id)
            .filter(
                Leg.trade_id.in_(active_master_ids),
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .all()
        ):
            if row and row[0]:
                protected.add(int(row[0]))
    return protected


async def _cancel_orphan_stops_on_client(
    client: DeltaClient,
    *,
    account_label: str,
    protected_product_ids: set[int],
    trade_id: int = 0,
) -> dict[str, int]:
    """Cancel open standalone stop_loss orders whose product is flat."""
    cancelled = 0
    already_gone = 0
    kept = 0
    errors = 0
    try:
        orders = await client.get_open_stop_orders()
    except Exception as exc:
        logger.warning(
            "Orphan SL: list open stops failed for %s: %s", account_label, exc
        )
        return {
            "cancelled": 0,
            "already_gone": 0,
            "kept": 0,
            "errors": 1,
        }

    for order in orders:
        product_id = order.get("product_id")
        order_id = order.get("id") or order.get("order_id")
        symbol = (
            order.get("product_symbol")
            or order.get("symbol")
            or ""
        )
        try:
            pid = int(product_id) if product_id is not None else 0
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0 or order_id is None:
            kept += 1
            continue

        if pid in protected_product_ids:
            kept += 1
            logger.info(
                "Keeping live-position SL: account=%s %s order=%s",
                account_label,
                symbol,
                order_id,
            )
            continue

        try:
            order_id_int = int(order_id)
            await client.cancel_order(order_id_int)
            cancelled += 1
            logger.info(
                "[ORPHAN_SL_CANCELLED] order_id=%s symbol=%s account=%s",
                order_id_int,
                symbol,
                account_label,
            )
            try:
                from backend.core.bot_logger import log_and_buffer

                log_and_buffer(
                    "ORPHAN_SL_CANCELLED",
                    int(trade_id or 0),
                    {
                        "order_id": order_id_int,
                        "symbol": symbol,
                        "account": account_label,
                        "product_id": pid,
                    },
                )
            except Exception:
                pass
        except DeltaAPIError as e:
            status_code = int(getattr(e, "status_code", 0) or 0)
            if status_code == 404:
                already_gone += 1
            else:
                errors += 1
                logger.warning(
                    "Could not cancel orphan SL %s order=%s: status=%s msg=%s",
                    symbol,
                    order_id,
                    status_code,
                    str(e),
                )
        except Exception as exc:
            errors += 1
            logger.warning(
                "Cancel error %s order=%s: %s",
                symbol,
                order_id,
                exc,
            )

    return {
        "cancelled": cancelled,
        "already_gone": already_gone,
        "kept": kept,
        "errors": errors,
    }


async def cleanup_orphan_sl_orders(
    db: Session,
    *,
    trade_id: int = 0,
) -> dict[str, Any]:
    """
    Cancel open standalone stop-loss orders that are NOT tied to a live position.

    Sweeps the master account and every non-virtual slave. Stops whose product
    still has an open position (or open bot-managed leg) are kept — e.g.
    Trade#65's standalone SLs stay until that position is flat.
    """
    total_cancelled = 0
    total_kept = 0
    total_errors = 0
    total_gone = 0
    accounts_swept = 0

    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is not None:
        client = DeltaClient(
            decrypt(account.api_key_encrypted),
            decrypt(account.api_secret_encrypted),
        )
        try:
            protected = await _protected_product_ids_for_master(db, client)
            stats = await _cancel_orphan_stops_on_client(
                client,
                account_label=f"master:{account.name}",
                protected_product_ids=protected,
                trade_id=trade_id,
            )
            accounts_swept += 1
            total_cancelled += stats["cancelled"]
            total_kept += stats["kept"]
            total_errors += stats["errors"]
            total_gone += stats["already_gone"]
            # Clear stale delta_sl_order_id on closed legs
            if stats["cancelled"] > 0:
                closed_legs = (
                    db.query(Leg)
                    .filter(
                        Leg.status != "open",
                        Leg.delta_sl_order_id.isnot(None),
                    )
                    .all()
                )
                for leg in closed_legs:
                    leg.delta_sl_order_id = None
                db.commit()
        finally:
            await client.close()

    try:
        from backend.models import SlaveAccount, SlaveTrade

        slaves = (
            db.query(SlaveAccount)
            .filter(
                SlaveAccount.is_active.is_(True),
                SlaveAccount.is_virtual.is_(False),
            )
            .order_by(SlaveAccount.id.asc())
            .all()
        )
    except Exception as exc:
        logger.warning("Orphan SL: slave list failed: %s", exc)
        slaves = []

    for slave in slaves:
        client = DeltaClient(
            decrypt(slave.api_key_encrypted),
            decrypt(slave.api_secret_encrypted),
        )
        try:
            protected = await _protected_product_ids_for_slave(
                db, client, int(slave.id)
            )
            stats = await _cancel_orphan_stops_on_client(
                client,
                account_label=f"slave:{slave.name}",
                protected_product_ids=protected,
                trade_id=trade_id,
            )
            accounts_swept += 1
            total_cancelled += stats["cancelled"]
            total_kept += stats["kept"]
            total_errors += stats["errors"]
            total_gone += stats["already_gone"]
            if stats["cancelled"] > 0:
                for st in (
                    db.query(SlaveTrade)
                    .filter(
                        SlaveTrade.slave_account_id == int(slave.id),
                        SlaveTrade.status != "active",
                    )
                    .all()
                ):
                    # Clear legacy standalone ids and ABS: audit fakes
                    for field in ("call_sl_order_id", "put_sl_order_id"):
                        oid = getattr(st, field, None)
                        if oid:
                            setattr(st, field, None)
                db.commit()
        except Exception as exc:
            total_errors += 1
            logger.warning(
                "Orphan SL sweep failed for slave %s: %s", slave.name, exc
            )
        finally:
            await client.close()

    if account is None and accounts_swept == 0:
        return {
            "success": True,
            "skipped": True,
            "reason": "No active account",
            "cancelled": 0,
            "kept": 0,
        }

    return {
        "success": True,
        "skipped": False,
        "cancelled": total_cancelled,
        "already_gone": total_gone,
        "kept": total_kept,
        "errors": total_errors,
        "accounts_swept": accounts_swept,
        "active_product_count": total_kept,
    }


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
        now_utc = get_utc_now()
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

        account.last_connected_at = get_utc_now()
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


@router.post("/cleanup-sl-orders")
async def cleanup_sl_orders(db: Session = Depends(get_db)) -> dict[str, Any]:
    """One-time orphan stop-loss cleanup."""
    return await cleanup_orphan_sl_orders(db)
