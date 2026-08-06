# routes_slave.py — /api/slave/* CRUD + overview for master-slave copy trading

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.encryption import decrypt, encrypt
from backend.core.fees import basket_fees_paid_from_legs, compute_net_mtm
from backend.core.time_utils import get_ist_now
from backend.database import get_db, get_or_create_auto_settings, get_usd_inr_rate
from backend.models import Account, Leg, SlaveAccount, SlaveTrade, Trade
from backend.schemas import (
    SlaveAccountCreate,
    SlaveAccountResponse,
    SlaveAccountUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/slave", tags=["slave-accounts"])


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _active_trade_count(db: Session, slave_id: int) -> int:
    return (
        db.query(SlaveTrade)
        .filter(
            SlaveTrade.slave_account_id == slave_id,
            SlaveTrade.status == "active",
        )
        .count()
    )


def _to_response(slave: SlaveAccount, db: Session, rate: float) -> dict[str, Any]:
    bal_usd = float(slave.balance_usd or 0.0)
    bal_inr = float(slave.balance_inr or 0.0)
    if bal_inr <= 0 and bal_usd > 0:
        bal_inr = round(bal_usd * rate, 2)
    active_count = _active_trade_count(db, int(slave.id))
    return {
        "id": int(slave.id),
        "name": slave.name,
        "qty_multiplier": float(slave.qty_multiplier or 1.0),
        "is_active": bool(slave.is_active),
        "connection_status": str(slave.connection_status or "unknown"),
        "balance_usd": bal_usd,
        "balance_inr": bal_inr,
        "last_connected_at": _iso(slave.last_connected_at),
        "last_error": slave.last_error,
        "active_trade_count": active_count,
        "has_active_trade": active_count > 0,
    }


@router.get("/accounts")
async def list_slave_accounts(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Return all slave accounts (no decrypted keys)."""
    rate = get_usd_inr_rate(db)
    slaves = db.query(SlaveAccount).order_by(SlaveAccount.id.asc()).all()
    return [_to_response(s, db, rate) for s in slaves]


@router.post("/accounts", response_model=SlaveAccountResponse)
async def create_slave_account(
    payload: SlaveAccountCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create slave account after verifying Delta API keys."""
    existing = (
        db.query(SlaveAccount).filter(SlaveAccount.name == payload.name.strip()).first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=400, detail=f"Slave account name '{payload.name}' already exists"
        )

    client = DeltaClient(payload.api_key.strip(), payload.api_secret.strip())
    try:
        try:
            profile = await client.test_connection()
            wallet = await client.get_wallet_balance()
        except DeltaAPIError as exc:
            logger.error("Slave connect failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()

    rate = get_usd_inr_rate(db)
    bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
    now = get_ist_now()
    slave = SlaveAccount(
        name=payload.name.strip(),
        api_key_encrypted=encrypt(payload.api_key.strip()),
        api_secret_encrypted=encrypt(payload.api_secret.strip()),
        qty_multiplier=float(payload.qty_multiplier),
        is_active=bool(payload.is_active),
        connection_status="connected",
        last_connected_at=now,
        last_error=None,
        balance_usd=bal_usd,
        balance_inr=round(bal_usd * rate, 2),
        created_at=now,
        updated_at=now,
    )
    db.add(slave)
    db.commit()
    db.refresh(slave)
    logger.info(
        "Slave account created id=%s name=%s delta_account=%s",
        slave.id,
        slave.name,
        profile.get("account_name"),
    )
    return _to_response(slave, db, rate)


@router.post("/accounts/{slave_id}/test")
async def test_slave_account(
    slave_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Decrypt keys and test Delta connection for a slave."""
    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail=f"Slave {slave_id} not found")

    client = DeltaClient(
        decrypt(slave.api_key_encrypted),
        decrypt(slave.api_secret_encrypted),
    )
    try:
        try:
            profile = await client.test_connection()
            wallet = await client.get_wallet_balance()
        except DeltaAPIError as exc:
            slave.connection_status = "error"
            slave.last_error = str(exc)[:500]
            slave.updated_at = get_ist_now()
            db.commit()
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        rate = get_usd_inr_rate(db)
        bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
        slave.balance_usd = bal_usd
        slave.balance_inr = round(bal_usd * rate, 2)
        slave.connection_status = "connected"
        slave.last_connected_at = get_ist_now()
        slave.last_error = None
        slave.updated_at = get_ist_now()
        db.commit()

        return {
            "connected": True,
            "account_name": profile.get("account_name") or slave.name,
            "balance_usd": bal_usd,
            "balance_inr": slave.balance_inr,
            "usd_inr_rate": rate,
        }
    finally:
        await client.close()


@router.patch("/accounts/{slave_id}")
async def update_slave_account(
    slave_id: int,
    payload: SlaveAccountUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update slave fields; re-encrypt keys if provided and re-test connection."""
    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail=f"Slave {slave_id} not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "name" in updates and updates["name"] is not None:
        new_name = str(updates["name"]).strip()
        clash = (
            db.query(SlaveAccount)
            .filter(SlaveAccount.name == new_name, SlaveAccount.id != slave_id)
            .first()
        )
        if clash is not None:
            raise HTTPException(status_code=400, detail=f"Name '{new_name}' already exists")
        slave.name = new_name

    if "qty_multiplier" in updates and updates["qty_multiplier"] is not None:
        slave.qty_multiplier = float(updates["qty_multiplier"])

    if "is_active" in updates and updates["is_active"] is not None:
        slave.is_active = bool(updates["is_active"])

    keys_changed = False
    api_key = updates.get("api_key")
    api_secret = updates.get("api_secret")
    if api_key:
        slave.api_key_encrypted = encrypt(str(api_key).strip())
        keys_changed = True
    if api_secret:
        slave.api_secret_encrypted = encrypt(str(api_secret).strip())
        keys_changed = True

    if keys_changed:
        client = DeltaClient(
            decrypt(slave.api_key_encrypted),
            decrypt(slave.api_secret_encrypted),
        )
        try:
            try:
                await client.test_connection()
                wallet = await client.get_wallet_balance()
            except DeltaAPIError as exc:
                slave.connection_status = "error"
                slave.last_error = str(exc)[:500]
                slave.updated_at = get_ist_now()
                db.commit()
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            rate = get_usd_inr_rate(db)
            bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
            slave.balance_usd = bal_usd
            slave.balance_inr = round(bal_usd * rate, 2)
            slave.connection_status = "connected"
            slave.last_connected_at = get_ist_now()
            slave.last_error = None
        finally:
            await client.close()

    slave.updated_at = get_ist_now()
    db.commit()
    db.refresh(slave)
    return _to_response(slave, db, get_usd_inr_rate(db))


@router.delete("/accounts/{slave_id}")
async def delete_slave_account(
    slave_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete slave if no active mirrored trades remain."""
    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail=f"Slave {slave_id} not found")

    active = _active_trade_count(db, slave_id)
    if active > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Close all trades first ({active} active slave trade(s))",
        )

    # Remove historical slave trade rows for this account
    db.query(SlaveTrade).filter(SlaveTrade.slave_account_id == slave_id).delete()
    db.delete(slave)
    db.commit()
    logger.info("Slave account deleted id=%s", slave_id)
    return {"success": True, "message": f"Slave {slave_id} deleted"}


@router.post("/accounts/{slave_id}/toggle")
async def toggle_slave_account(
    slave_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Toggle is_active for a slave account."""
    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail=f"Slave {slave_id} not found")

    slave.is_active = not bool(slave.is_active)
    slave.updated_at = get_ist_now()
    db.commit()
    msg = "enabled" if slave.is_active else "paused"
    return {
        "id": slave.id,
        "is_active": slave.is_active,
        "message": f"Slave '{slave.name}' {msg}",
    }


@router.post("/accounts/{slave_id}/copy-master-trade")
async def copy_master_trade_to_slave(
    slave_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    One-click: place the master's current open strangle on this slave.

    Uses bracket SL attached to entry orders (auto-cancels on close).
    """
    from backend.engine.bot_engine import bot_engine

    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail="Slave account not found")

    if not slave.is_active:
        raise HTTPException(status_code=400, detail="Slave account is paused")

    active_states = bot_engine.position_tracker.get_all_active()
    if not active_states:
        return {"success": False, "message": "No active master trade to copy"}

    state = active_states[0]
    master_trade_id = int(state.trade_id)

    existing = (
        db.query(SlaveTrade)
        .filter(
            SlaveTrade.slave_account_id == slave_id,
            SlaveTrade.master_trade_id == master_trade_id,
            SlaveTrade.status == "active",
        )
        .first()
    )
    if existing is not None:
        return {
            "success": False,
            "message": f"Slave already has trade #{master_trade_id}",
        }

    call_leg = state.call_leg
    put_leg = state.put_leg
    if call_leg is None or put_leg is None:
        return {"success": False, "message": "Master trade missing open call/put legs"}

    master_qty = max(1, int(getattr(call_leg, "quantity", 1) or 1))
    mult = float(slave.qty_multiplier or 1.0)
    slave_qty = max(1, int(round(master_qty * mult)))

    uni_sl = float(getattr(state.trade, "universal_sl_pct", None) or 200.0)
    call_base = float(
        getattr(call_leg, "trigger_baseline_premium", None)
        or getattr(call_leg, "initial_premium", 0)
        or 0
    )
    put_base = float(
        getattr(put_leg, "trigger_baseline_premium", None)
        or getattr(put_leg, "initial_premium", 0)
        or 0
    )
    call_sl = round(call_base * (uni_sl / 100.0), 2) if call_base > 0 else None
    put_sl = round(put_base * (uni_sl / 100.0), 2) if put_base > 0 else None

    underlying = str(getattr(state.trade, "underlying", "") or "")
    call_symbol = str(getattr(call_leg, "symbol", "") or "")
    put_symbol = str(getattr(put_leg, "symbol", "") or "")

    client = DeltaClient(
        decrypt(slave.api_key_encrypted),
        decrypt(slave.api_secret_encrypted),
    )
    call_order_id: str | None = None
    try:
        # Bracket SL confirmed working on Delta Exchange India
        # Format: bracket_stop_loss_price + bracket_stop_loss_limit_price
        call_order = await client.place_order(
            product_id=int(call_leg.product_id),
            size=slave_qty,
            side="sell",
            bracket_stop_loss_price=call_sl,
            bracket_stop_loss_limit_price=(
                round(call_sl * 1.05, 2) if call_sl else None
            ),
        )
        call_fill = float(
            await client.resolve_fill_price(
                call_order, symbol_for_fallback=call_symbol or None
            )
            or 0.0
        )
        if call_fill <= 0:
            call_fill = call_base
        call_order_id = str(
            call_order.get("order_id") or call_order.get("id") or ""
        ) or None

        put_order = await client.place_order(
            product_id=int(put_leg.product_id),
            size=slave_qty,
            side="sell",
            bracket_stop_loss_price=put_sl,
            bracket_stop_loss_limit_price=(
                round(put_sl * 1.05, 2) if put_sl else None
            ),
        )
        put_fill = float(
            await client.resolve_fill_price(
                put_order, symbol_for_fallback=put_symbol or None
            )
            or 0.0
        )
        if put_fill <= 0:
            put_fill = put_base
        put_order_id = str(
            put_order.get("order_id") or put_order.get("id") or ""
        ) or None

        slave_trade = SlaveTrade(
            slave_account_id=int(slave.id),
            master_trade_id=master_trade_id,
            call_order_id=call_order_id,
            put_order_id=put_order_id,
            call_sl_order_id=None,
            put_sl_order_id=None,
            actual_quantity=slave_qty,
            call_fill_price=call_fill,
            put_fill_price=put_fill,
            status="active",
        )
        db.add(slave_trade)

        slave.connection_status = "connected"
        slave.last_connected_at = get_ist_now()
        slave.last_error = None
        slave.updated_at = get_ist_now()
        db.commit()

        logger.info(
            "✅ Master trade #%s copied to slave '%s': qty=%s "
            "call_fill=%s put_fill=%s",
            master_trade_id,
            slave.name,
            slave_qty,
            call_fill,
            put_fill,
        )

        return {
            "success": True,
            "message": f"Trade #{master_trade_id} copied to {slave.name}",
            "slave_qty": slave_qty,
            "call_fill": call_fill,
            "put_fill": put_fill,
            "call_order_id": call_order_id,
            "put_order_id": put_order_id,
            "master_trade_id": master_trade_id,
            "underlying": underlying,
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Copy trade to slave failed slave=%s master=%s call_order=%s: %s",
            slave.name,
            master_trade_id,
            call_order_id,
            exc,
            exc_info=True,
        )
        slave.last_error = str(exc)[:500]
        slave.connection_status = "error"
        slave.updated_at = get_ist_now()
        try:
            db.commit()
        except Exception:
            db.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"Failed to copy trade: {exc}",
        ) from exc
    finally:
        await client.close()


@router.get("/accounts/{slave_id}/trades")
async def list_slave_trades(
    slave_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return all SlaveTrade rows for a slave, joined with master trade info."""
    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail=f"Slave {slave_id} not found")

    rows = (
        db.query(SlaveTrade)
        .filter(SlaveTrade.slave_account_id == slave_id)
        .order_by(SlaveTrade.id.desc())
        .all()
    )
    trades_out: list[dict[str, Any]] = []
    for st in rows:
        master = db.query(Trade).filter(Trade.id == st.master_trade_id).first()
        trades_out.append(
            {
                "id": st.id,
                "master_trade_id": st.master_trade_id,
                "status": st.status,
                "actual_quantity": st.actual_quantity,
                "call_fill_price": st.call_fill_price,
                "put_fill_price": st.put_fill_price,
                "call_order_id": st.call_order_id,
                "put_order_id": st.put_order_id,
                "call_sl_order_id": st.call_sl_order_id,
                "put_sl_order_id": st.put_sl_order_id,
                "last_mtm": float(st.last_mtm or 0.0),
                "last_updated": _iso(st.last_updated),
                "last_error": st.last_error,
                "error_count": int(st.error_count or 0),
                "created_at": _iso(st.created_at),
                "underlying": master.underlying if master else None,
                "expiry_date": (
                    master.expiry_date.isoformat()
                    if master and master.expiry_date
                    else None
                ),
                "master_status": master.status if master else None,
                "basket_number": getattr(master, "basket_number", None) if master else None,
            }
        )
    return {"slave_id": slave_id, "name": slave.name, "trades": trades_out}


@router.get("/overview")
async def slave_overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Aggregate dashboard view: master + each slave with balances, MTM, targets.
    """
    from backend.config import TradeStatus
    from backend.engine.bot_engine import bot_engine

    rate = get_usd_inr_rate(db)
    settings = get_or_create_auto_settings(db)

    master_account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )

    master_balance_usd = 0.0
    master_available_usd = 0.0
    master_name = "Not connected"
    master_connected = False
    master_error: str | None = None

    if master_account is not None:
        master_name = master_account.name
        master_connected = True
        try:
            client = DeltaClient(
                decrypt(master_account.api_key_encrypted),
                decrypt(master_account.api_secret_encrypted),
            )
            try:
                wallet = await client.get_wallet_balance()
                master_balance_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
                master_available_usd = float(
                    wallet.get("available_balance", master_balance_usd) or 0.0
                )
            finally:
                await client.close()
        except Exception as exc:
            logger.warning("Master balance fetch failed for overview: %s", exc)
            master_error = str(exc)

    # Master active trade from in-memory tracker
    master_trade_data: dict[str, Any] | None = None
    master_states = bot_engine.position_tracker.get_all_active()
    if master_states:
        state = master_states[0]
        call_leg = state.call_leg
        put_leg = state.put_leg
        call_entry = float(getattr(call_leg, "initial_premium", 0) or 0)
        put_entry = float(getattr(put_leg, "initial_premium", 0) or 0)
        call_curr = float(getattr(state, "last_call_premium", 0) or 0)
        put_curr = float(getattr(state, "last_put_premium", 0) or 0)
        gross_mtm = float(getattr(state, "last_pnl", 0) or 0)
        net_mtm = float(
            getattr(state, "last_net_mtm", None)
            or getattr(state, "last_delta_mtm", None)
            or gross_mtm
            or 0
        )
        master_trade_data = {
            "trade_id": state.trade_id,
            "underlying": getattr(state.trade, "underlying", None),
            "basket_number": getattr(state.trade, "basket_number", None),
            "net_mtm": net_mtm,
            "gross_mtm": gross_mtm,
            "realized_pnl": float(getattr(state.trade, "realized_pnl", 0) or 0),
            "profit_target_usd": float(
                getattr(state.trade, "profit_target_usd", 0) or 0
            ),
            "stoploss_usd": float(getattr(state.trade, "stoploss_usd", 0) or 0),
            "call_strike": float(getattr(call_leg, "strike", 0) or 0),
            "put_strike": float(getattr(put_leg, "strike", 0) or 0),
            "call_entry": call_entry,
            "put_entry": put_entry,
            "call_premium": call_curr,
            "put_premium": put_curr,
            "call_quantity": int(getattr(call_leg, "quantity", 1) or 1),
            "put_quantity": int(getattr(put_leg, "quantity", 1) or 1),
            "call_status": str(getattr(call_leg, "status", "open") or "open"),
            "put_status": str(getattr(put_leg, "status", "open") or "open"),
            "expiry_date": str(getattr(state.trade, "expiry_date", "") or ""),
            "slippage_pct": float(getattr(state.trade, "slippage_pct", 2) or 2),
            "status": "live",
        }

    slaves = (
        db.query(SlaveAccount).order_by(SlaveAccount.id.asc()).all()
    )
    slaves_data: list[dict[str, Any]] = []
    master_target = (
        float(master_trade_data["profit_target_usd"])
        if master_trade_data
        else 0.0
    )

    for slave in slaves:
        # Refresh balance opportunistically (best-effort, non-fatal)
        bal_usd = float(slave.balance_usd or 0.0)
        avail_usd: float | None = None
        if slave.is_active and slave.connection_status != "error":
            try:
                client = DeltaClient(
                    decrypt(slave.api_key_encrypted),
                    decrypt(slave.api_secret_encrypted),
                )
                try:
                    wallet = await client.get_wallet_balance()
                    bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
                    avail_usd = float(
                        wallet.get("available_balance", bal_usd) or 0.0
                    )
                    slave.balance_usd = bal_usd
                    slave.balance_inr = round(bal_usd * rate, 2)
                    slave.connection_status = "connected"
                    slave.last_connected_at = get_ist_now()
                    slave.last_error = None
                    db.commit()
                finally:
                    await client.close()
            except Exception as exc:
                logger.warning(
                    "Slave %s balance refresh failed: %s", slave.name, exc
                )

        active_st = (
            db.query(SlaveTrade)
            .filter(
                SlaveTrade.slave_account_id == slave.id,
                SlaveTrade.status == "active",
            )
            .order_by(SlaveTrade.id.desc())
            .first()
        )

        slave_trade_data: dict[str, Any] | None = None
        if active_st is not None:
            master_row = (
                db.query(Trade).filter(Trade.id == active_st.master_trade_id).first()
            )
            gross_mtm = float(active_st.last_mtm or 0.0)

            # Best-effort live MTM from slave positions.
            # NEVER use API unrealized_pnl — it's entry cashflow on Delta India.
            # Correct: (entry - mark) × abs(size) × contract_size for shorts.
            try:
                client = DeltaClient(
                    decrypt(slave.api_key_encrypted),
                    decrypt(slave.api_secret_encrypted),
                )
                try:
                    upnl_map = await client.get_positions_upnl()
                    total_mtm = 0.0
                    for row in upnl_map.values():
                        try:
                            size = float(row.get("size") or 0)
                        except (TypeError, ValueError):
                            size = 0.0
                        if size == 0:
                            continue
                        total_mtm += float(row.get("upnl") or 0.0)
                    gross_mtm = round(total_mtm, 4)
                    active_st.last_mtm = gross_mtm
                    active_st.last_updated = get_ist_now()
                    db.commit()
                finally:
                    await client.close()
            except Exception as exc:
                logger.warning("Slave %s MTM fetch failed: %s", slave.name, exc)

            # Apply fees + slippage to get net MTM
            slave_slip_pct = float(
                getattr(master_row, "slippage_pct", None) or 2.0
            ) if master_row else 2.0
            slave_call_fill = float(active_st.call_fill_price or 0.0)
            slave_put_fill = float(active_st.put_fill_price or 0.0)
            slave_qty = int(active_st.actual_quantity or 1)
            FEE_RATE = 0.0005
            slave_fees_paid = round(
                (slave_call_fill + slave_put_fill) * slave_qty * 0.001 * FEE_RATE * 2, 4
            )
            master_call_curr = float(master_trade_data["call_premium"]) if master_trade_data else 0.0
            master_put_curr = float(master_trade_data["put_premium"]) if master_trade_data else 0.0
            slave_est_exit = round(
                (master_call_curr + master_put_curr) * slave_qty * 0.001 * FEE_RATE * 2, 4
            )
            slave_net_fields = compute_net_mtm(
                gross_mtm=gross_mtm,
                fees_paid=slave_fees_paid,
                est_exit_fees=slave_est_exit,
                slippage_pct=slave_slip_pct,
            )
            slave_net_mtm = float(slave_net_fields["net_mtm"])

            mult = float(slave.qty_multiplier or 1.0)
            last_updated_iso = _iso(active_st.last_updated)
            slave_trade_data = {
                "slave_trade_id": active_st.id,
                "master_trade_id": active_st.master_trade_id,
                "actual_quantity": int(active_st.actual_quantity or 1),
                "call_fill_price": active_st.call_fill_price,
                "put_fill_price": active_st.put_fill_price,
                "status": active_st.status,
                "gross_mtm": gross_mtm,
                "last_mtm": slave_net_mtm,
                "net_mtm": slave_net_mtm,
                "last_updated": last_updated_iso,
                "last_mtm_updated": last_updated_iso,
                "net_mtm_updated": last_updated_iso,
                "last_error": active_st.last_error,
                "profit_target_usd": round(master_target * mult, 2)
                if master_target
                else None,
                "call_strike": (
                    float(master_trade_data["call_strike"])
                    if master_trade_data
                    else None
                ),
                "put_strike": (
                    float(master_trade_data["put_strike"])
                    if master_trade_data
                    else None
                ),
                "call_premium": float(master_trade_data["call_premium"]) if master_trade_data else None,
                "put_premium": float(master_trade_data["put_premium"]) if master_trade_data else None,
                "expiry_date": str(getattr(master_row, "expiry_date", "") or "") if master_row else "",
                "underlying": master_row.underlying if master_row else None,
                "basket_number": (
                    getattr(master_row, "basket_number", None) if master_row else None
                ),
            }

        slaves_data.append(
            {
                "id": int(slave.id),
                "name": slave.name,
                "qty_multiplier": float(slave.qty_multiplier or 1.0),
                "is_active": bool(slave.is_active),
                "connection_status": str(slave.connection_status or "unknown"),
                "balance_usd": bal_usd,
                "balance_inr": round(bal_usd * rate, 2),
                "available_usd": avail_usd,
                "available_inr": (
                    round(avail_usd * rate, 2) if avail_usd is not None else None
                ),
                "last_error": slave.last_error,
                "last_connected_at": _iso(slave.last_connected_at),
                "active_slave_trade": slave_trade_data,
            }
        )

    combined_mtm = 0.0
    if master_trade_data:
        combined_mtm += float(master_trade_data.get("net_mtm") or 0)
    for s in slaves_data:
        st = s.get("active_slave_trade")
        if st:
            combined_mtm += float(st.get("last_mtm") or 0)

    master_active_count = 0
    if master_account is not None:
        master_active_count = (
            db.query(Trade)
            .filter(
                Trade.account_id == master_account.id,
                Trade.status == TradeStatus.ACTIVE.value,
            )
            .count()
        )

    return {
        "usd_inr_rate": rate,
        "auto_trade_enabled": bool(settings.is_enabled),
        "has_slaves": len(slaves) > 0,
        "combined_mtm": round(combined_mtm, 4),
        "master": {
            "name": master_name,
            "connected": master_connected,
            "balance_usd": master_balance_usd,
            "balance_inr": round(master_balance_usd * rate, 2),
            "available_usd": master_available_usd,
            "available_inr": round(master_available_usd * rate, 2),
            "active_trade_count": master_active_count,
            "active_trade": master_trade_data,
            "last_error": master_error,
        },
        "slaves": slaves_data,
    }
