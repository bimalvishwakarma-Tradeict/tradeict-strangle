# routes_slave.py — /api/slave/* CRUD + overview for master-slave copy trading

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.encryption import decrypt, encrypt
from backend.core.time_utils import get_utc_now
from backend.database import get_db, get_or_create_auto_settings, get_usd_inr_rate
from backend.models import (
    Account,
    HedgePosition,
    SlaveAccount,
    SlaveHedgePosition,
    SlaveTrade,
    Structure,
    Trade,
)
from backend.schemas import (
    SlaveAccountCreate,
    SlaveAccountResponse,
    SlaveAccountUpdate,
    SlaveForceCloseRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/slave", tags=["slave-accounts"])

# Unfillable by construction: product 0 does not exist, size 0 cannot fill,
# IOC market cannot rest. Used only as a trading-permission probe.
TRADING_PROBE_PRODUCT_ID = 0
TRADING_PROBE_SIZE = 0

_READ_ONLY_ERROR = (
    "Delta API key is read-only — enable Trading permission. (read_only)"
)


def _is_read_only_permission_error(exc: DeltaAPIError) -> bool:
    """True when Delta rejected the order because the key cannot trade."""
    msg = str(getattr(exc, "message", "") or exc).lower()
    if (
        "ip_not_whitelisted" in msg
        or "ip whitelist" in msg
        or "not whitelisted" in msg
    ):
        return False
    if (
        "invalid_api_key" in msg
        or "invalid api key" in msg
        or "signature" in msg
    ):
        return False
    permission_needles = (
        "permission",
        "unauthorized",
        "unauthorised",
        "forbidden",
        "read only",
        "read-only",
        "readonly",
        "not authorized",
        "not authorised",
        "not allowed to trade",
        "trading is disabled",
        "insufficient permission",
    )
    if any(n in msg for n in permission_needles):
        return True
    code = int(getattr(exc, "status_code", 0) or 0)
    return code in (401, 403)


def _is_order_validation_error(exc: DeltaAPIError) -> bool:
    """True when the probe reached POST /v2/orders and Delta rejected the bad fields."""
    code = int(getattr(exc, "status_code", 0) or 0)
    if code in (400, 404, 422):
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    return any(
        n in msg
        for n in (
            "invalid product",
            "invalid_product",
            "product_id",
            "unknown product",
            "does not exist",
            "invalid size",
            "size must",
            "must be greater",
            "out of range",
        )
    )


async def assert_trading_permission(client: DeltaClient) -> None:
    """
    Prove the key can hit POST /v2/orders without placing a fillable order.

    Delta does not expose key scopes on GET /v2/profile. A wallet read succeeds
    for read-only keys, so we probe with product_id=0 and size=0 (IOC market).
    Permission/authorisation errors refuse registration; validation errors
    mean trading permission is present.
    """
    try:
        result = await client.place_order(
            product_id=TRADING_PROBE_PRODUCT_ID,
            size=TRADING_PROBE_SIZE,
            side="buy",
            order_type="market_order",
            time_in_force="ioc",
        )
    except DeltaAPIError as exc:
        if _is_read_only_permission_error(exc):
            logger.error("Slave key trading probe: read_only — %s", exc)
            raise HTTPException(status_code=403, detail=_READ_ONLY_ERROR) from exc
        if _is_order_validation_error(exc):
            logger.info(
                "Slave key trading probe: validation reject (permission ok) %s",
                exc,
            )
            return
        logger.error("Slave key trading probe inconclusive: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Must never happen for product_id=0 / size=0 — cancel if Delta accepted.
    order_id = None
    if isinstance(result, dict):
        order_id = result.get("order_id") or (result.get("raw") or {}).get("id")
    logger.critical(
        "trading probe unexpectedly accepted order_id=%s — cancelling",
        order_id,
    )
    if order_id is not None:
        try:
            await client.cancel_order(int(order_id))
        except Exception as cancel_exc:
            logger.critical(
                "trading probe cancel failed order_id=%s: %s",
                order_id,
                cancel_exc,
            )
    raise HTTPException(
        status_code=502,
        detail="Trading probe returned an order — registration refused",
    )


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _live_px(bot_engine: Any, symbol: str | None, fallback: float = 0.0) -> float:
    """Current mark from bot_engine._live_prices, else fallback."""
    sym = str(symbol or "").strip()
    if not sym:
        return float(fallback or 0.0)
    try:
        prices = getattr(bot_engine, "_live_prices", None) or {}
        px = float(prices.get(sym) or 0.0)
        if px > 0:
            return px
    except (TypeError, ValueError):
        pass
    return float(fallback or 0.0)


def _leg_pnl_usd(
    *,
    entry: float,
    current: float,
    quantity: int,
    is_long: bool,
) -> float:
    """Same signed UPNL as master UPL@Offer (short size negative, long positive)."""
    from backend.core.delta_client import compute_signed_upnl

    entry_f = float(entry or 0.0)
    curr_f = float(current or 0.0)
    qty = abs(int(quantity or 0))
    if entry_f <= 0 or curr_f <= 0 or qty <= 0:
        return 0.0
    size = float(qty) if is_long else -float(qty)
    return round(float(compute_signed_upnl(entry_f, curr_f, size)), 4)


def _split_basket_net(
    basket_net: float,
    short_gross: float,
    wing_gross: float,
) -> tuple[float, float]:
    """
    Partition basket_net into short_net + wing_net (= basket_net exactly).

    Uses gross leg contribution as weights; no new fee math.
    """
    basket = float(basket_net or 0.0)
    sg = float(short_gross or 0.0)
    wg = float(wing_gross or 0.0)
    total = sg + wg
    if abs(total) < 1e-12:
        return round(basket, 4), 0.0
    short_net = round(basket * (sg / total), 4)
    wing_net = round(basket - short_net, 4)
    return short_net, wing_net


def _build_pnl_block(
    *,
    hedge_net: float | None,
    short_gross: float,
    wing_gross: float,
    basket_net: float,
    closed_basket: float,
    computed_at: str | None,
    stale_seconds: float | None,
) -> dict[str, Any]:
    short_net, wing_net = _split_basket_net(basket_net, short_gross, wing_gross)
    h_net = float(hedge_net) if hedge_net is not None else 0.0
    closed = float(closed_basket or 0.0)
    structure_net = round(h_net + closed + float(basket_net or 0.0), 4)
    return {
        "hedge_net": round(h_net, 4) if hedge_net is not None else None,
        "short_net": short_net,
        "wing_net": wing_net,
        "basket_net": round(float(basket_net or 0.0), 4),
        "structure_net": structure_net,
        "computed_at": computed_at,
        "stale_seconds": stale_seconds,
    }


def _leg_dict(
    *,
    role: str,
    strike: float | None,
    entry_price: float | None,
    current_price: float | None,
    quantity: int,
    leg_pnl: float,
    status: str,
) -> dict[str, Any]:
    return {
        "role": role,
        "strike": float(strike) if strike is not None else None,
        "entry_price": float(entry_price) if entry_price is not None else None,
        "current_price": float(current_price) if current_price is not None else None,
        "quantity": int(quantity),
        "leg_pnl": round(float(leg_pnl), 4),
        "status": str(status or "open"),
    }


def _master_basket_legs(state: Any, bot_engine: Any) -> list[dict[str, Any]]:
    """short_call, short_put, then wings only when present — no placeholders."""
    legs: list[dict[str, Any]] = []
    call_leg = state.call_leg
    put_leg = state.put_leg
    call_curr = float(getattr(state, "last_call_premium", 0) or 0)
    put_curr = float(getattr(state, "last_put_premium", 0) or 0)
    if call_curr <= 0:
        call_curr = _live_px(
            bot_engine,
            getattr(call_leg, "symbol", None),
            float(getattr(call_leg, "initial_premium", 0) or 0),
        )
    if put_curr <= 0:
        put_curr = _live_px(
            bot_engine,
            getattr(put_leg, "symbol", None),
            float(getattr(put_leg, "initial_premium", 0) or 0),
        )

    call_entry = float(getattr(call_leg, "initial_premium", 0) or 0)
    put_entry = float(getattr(put_leg, "initial_premium", 0) or 0)
    call_qty = abs(int(getattr(call_leg, "quantity", 1) or 1))
    put_qty = abs(int(getattr(put_leg, "quantity", 1) or 1))

    legs.append(
        _leg_dict(
            role="short_call",
            strike=getattr(call_leg, "strike", None),
            entry_price=call_entry,
            current_price=call_curr if call_curr > 0 else None,
            quantity=call_qty,
            leg_pnl=_leg_pnl_usd(
                entry=call_entry, current=call_curr, quantity=call_qty, is_long=False
            ),
            status=str(getattr(call_leg, "status", "open") or "open"),
        )
    )
    legs.append(
        _leg_dict(
            role="short_put",
            strike=getattr(put_leg, "strike", None),
            entry_price=put_entry,
            current_price=put_curr if put_curr > 0 else None,
            quantity=put_qty,
            leg_pnl=_leg_pnl_usd(
                entry=put_entry, current=put_curr, quantity=put_qty, is_long=False
            ),
            status=str(getattr(put_leg, "status", "open") or "open"),
        )
    )

    wc = getattr(state, "wing_call_leg", None)
    if wc is not None and str(getattr(wc, "status", "") or "").lower() == "open":
        wc_entry = float(getattr(wc, "initial_premium", 0) or 0)
        wc_curr = _live_px(bot_engine, getattr(wc, "symbol", None), wc_entry)
        wc_qty = abs(int(getattr(wc, "quantity", 1) or 1))
        legs.append(
            _leg_dict(
                role="wing_call",
                strike=getattr(wc, "strike", None),
                entry_price=wc_entry if wc_entry > 0 else None,
                current_price=wc_curr if wc_curr > 0 else None,
                quantity=wc_qty,
                leg_pnl=_leg_pnl_usd(
                    entry=wc_entry, current=wc_curr, quantity=wc_qty, is_long=True
                ),
                status=str(getattr(wc, "status", "open") or "open"),
            )
        )

    wp = getattr(state, "wing_put_leg", None)
    if wp is not None and str(getattr(wp, "status", "") or "").lower() == "open":
        wp_entry = float(getattr(wp, "initial_premium", 0) or 0)
        wp_curr = _live_px(bot_engine, getattr(wp, "symbol", None), wp_entry)
        wp_qty = abs(int(getattr(wp, "quantity", 1) or 1))
        legs.append(
            _leg_dict(
                role="wing_put",
                strike=getattr(wp, "strike", None),
                entry_price=wp_entry if wp_entry > 0 else None,
                current_price=wp_curr if wp_curr > 0 else None,
                quantity=wp_qty,
                leg_pnl=_leg_pnl_usd(
                    entry=wp_entry, current=wp_curr, quantity=wp_qty, is_long=True
                ),
                status=str(getattr(wp, "status", "open") or "open"),
            )
        )
    return legs


def _slave_basket_legs(
    slave_trade: SlaveTrade,
    *,
    call_now: float | None,
    put_now: float | None,
    wing_call_now: float | None,
    wing_put_now: float | None,
) -> list[dict[str, Any]]:
    """Slave legs from SlaveTrade fills + live prices (usually master's marks)."""
    qty = abs(int(slave_trade.actual_quantity or 1))
    legs: list[dict[str, Any]] = []

    call_fill = float(slave_trade.call_fill_price or 0)
    put_fill = float(slave_trade.put_fill_price or 0)
    call_px = float(call_now or 0)
    put_px = float(put_now or 0)

    legs.append(
        _leg_dict(
            role="short_call",
            strike=getattr(slave_trade, "call_strike", None),
            entry_price=call_fill if call_fill > 0 else None,
            current_price=call_px if call_px > 0 else None,
            quantity=qty,
            leg_pnl=_leg_pnl_usd(
                entry=call_fill, current=call_px, quantity=qty, is_long=False
            ),
            status="open",
        )
    )
    legs.append(
        _leg_dict(
            role="short_put",
            strike=getattr(slave_trade, "put_strike", None),
            entry_price=put_fill if put_fill > 0 else None,
            current_price=put_px if put_px > 0 else None,
            quantity=qty,
            leg_pnl=_leg_pnl_usd(
                entry=put_fill, current=put_px, quantity=qty, is_long=False
            ),
            status="open",
        )
    )

    wc_pid = getattr(slave_trade, "wing_call_product_id", None)
    wc_fill = float(getattr(slave_trade, "wing_call_fill_price", None) or 0)
    if wc_pid and wc_fill > 0:
        wc_px = float(wing_call_now or 0)
        legs.append(
            _leg_dict(
                role="wing_call",
                strike=getattr(slave_trade, "wing_call_strike", None),
                entry_price=wc_fill,
                current_price=wc_px if wc_px > 0 else None,
                quantity=qty,
                leg_pnl=_leg_pnl_usd(
                    entry=wc_fill, current=wc_px, quantity=qty, is_long=True
                ),
                status="open",
            )
        )

    wp_pid = getattr(slave_trade, "wing_put_product_id", None)
    wp_fill = float(getattr(slave_trade, "wing_put_fill_price", None) or 0)
    if wp_pid and wp_fill > 0:
        wp_px = float(wing_put_now or 0)
        legs.append(
            _leg_dict(
                role="wing_put",
                strike=getattr(slave_trade, "wing_put_strike", None),
                entry_price=wp_fill,
                current_price=wp_px if wp_px > 0 else None,
                quantity=qty,
                leg_pnl=_leg_pnl_usd(
                    entry=wp_fill, current=wp_px, quantity=qty, is_long=True
                ),
                status="open",
            )
        )
    return legs


def _role_gross(legs: list[dict[str, Any]], prefix: str) -> float:
    total = 0.0
    for leg in legs:
        role = str(leg.get("role") or "")
        if role.startswith(prefix):
            total += float(leg.get("leg_pnl") or 0.0)
    return round(total, 4)


def _hedge_payload(
    *,
    hedge: HedgePosition | None = None,
    slave_hedge: SlaveHedgePosition | None = None,
    bot_engine: Any | None = None,
    hedge_net_mtm: float | None = None,
) -> dict[str, Any] | None:
    """Build hedge block from master HedgePosition or SlaveHedgePosition."""
    row: Any = slave_hedge if slave_hedge is not None else hedge
    if row is None:
        return None
    call_entry = float(getattr(row, "call_fill_price", None) or 0) or None
    put_entry = float(getattr(row, "put_fill_price", None) or 0) or None
    call_sym = getattr(row, "call_symbol", None)
    put_sym = getattr(row, "put_symbol", None)
    call_now = (
        _live_px(bot_engine, call_sym, float(call_entry or 0))
        if bot_engine is not None
        else None
    )
    put_now = (
        _live_px(bot_engine, put_sym, float(put_entry or 0))
        if bot_engine is not None
        else None
    )
    if call_now is not None and call_now <= 0:
        call_now = None
    if put_now is not None and put_now <= 0:
        put_now = None
    expiry = getattr(row, "expiry_date", None)
    return {
        "hedge_id": int(row.id),
        "strike": float(row.strike) if row.strike is not None else None,
        "expiry_date": str(expiry) if expiry is not None else None,
        "quantity": int(row.quantity or 0),
        "call_entry": call_entry,
        "put_entry": put_entry,
        "call_now": call_now,
        "put_now": put_now,
        "hedge_net_mtm": (
            round(float(hedge_net_mtm), 4) if hedge_net_mtm is not None else None
        ),
    }


def _active_trade_count(db: Session, slave_id: int) -> int:
    return (
        db.query(SlaveTrade)
        .filter(
            SlaveTrade.slave_account_id == slave_id,
            SlaveTrade.status == "active",
        )
        .count()
    )


def _latest_slave_structure_close(db: Session, slave_id: int) -> dict[str, Any]:
    """Most recent closed SLAVE structure row for overview display."""
    row = (
        db.query(Structure)
        .filter(
            Structure.slave_account_id == int(slave_id),
            Structure.account_kind == "SLAVE",
            Structure.status == "closed",
        )
        .order_by(Structure.closed_at.desc(), Structure.id.desc())
        .first()
    )
    if row is None:
        return {"structure_close_reason": None, "structure_closed_at": None}
    return {
        "structure_close_reason": row.close_reason,
        "structure_closed_at": _iso(row.closed_at),
    }


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
        "capital_based_qty": bool(
            getattr(slave, "capital_based_qty", False)
        ),
        "user_allocated_capital": (
            float(slave.user_allocated_capital)
            if getattr(slave, "user_allocated_capital", None) is not None
            else None
        ),
        "earner_user_id": getattr(slave, "earner_user_id", None),
        "earner_subscription_id": getattr(
            slave, "earner_subscription_id", None
        ),
        "is_virtual": bool(getattr(slave, "is_virtual", False)),
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
            if not bool(payload.is_virtual):
                await assert_trading_permission(client)
        except HTTPException:
            raise
        except DeltaAPIError as exc:
            logger.error("Slave connect failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()

    rate = get_usd_inr_rate(db)
    bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
    now = get_utc_now()
    slave = SlaveAccount(
        name=payload.name.strip(),
        api_key_encrypted=encrypt(payload.api_key.strip()),
        api_secret_encrypted=encrypt(payload.api_secret.strip()),
        qty_multiplier=float(payload.qty_multiplier),
        capital_based_qty=bool(payload.capital_based_qty),
        user_allocated_capital=payload.user_allocated_capital,
        earner_user_id=payload.earner_user_id,
        earner_subscription_id=payload.earner_subscription_id,
        is_virtual=bool(payload.is_virtual),
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
            if not bool(getattr(slave, "is_virtual", False)):
                await assert_trading_permission(client)
        except HTTPException as exc:
            slave.connection_status = "error"
            slave.last_error = str(exc.detail)[:500]
            slave.updated_at = get_utc_now()
            db.commit()
            raise
        except DeltaAPIError as exc:
            slave.connection_status = "error"
            slave.last_error = str(exc)[:500]
            slave.updated_at = get_utc_now()
            db.commit()
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        rate = get_usd_inr_rate(db)
        bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
        slave.balance_usd = bal_usd
        slave.balance_inr = round(bal_usd * rate, 2)
        slave.connection_status = "connected"
        slave.last_connected_at = get_utc_now()
        slave.last_error = None
        slave.updated_at = get_utc_now()
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

    if "capital_based_qty" in updates and updates["capital_based_qty"] is not None:
        slave.capital_based_qty = bool(updates["capital_based_qty"])

    if "user_allocated_capital" in updates:
        slave.user_allocated_capital = (
            float(updates["user_allocated_capital"])
            if updates["user_allocated_capital"] is not None
            else None
        )

    if "earner_user_id" in updates:
        slave.earner_user_id = (
            str(updates["earner_user_id"]).strip()
            if updates["earner_user_id"] is not None
            else None
        )

    if "earner_subscription_id" in updates:
        slave.earner_subscription_id = (
            str(updates["earner_subscription_id"]).strip()
            if updates["earner_subscription_id"] is not None
            else None
        )

    if "is_virtual" in updates and updates["is_virtual"] is not None:
        slave.is_virtual = bool(updates["is_virtual"])

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
                if not bool(getattr(slave, "is_virtual", False)):
                    await assert_trading_permission(client)
            except HTTPException as exc:
                slave.connection_status = "error"
                slave.last_error = str(exc.detail)[:500]
                slave.updated_at = get_utc_now()
                db.commit()
                raise
            except DeltaAPIError as exc:
                slave.connection_status = "error"
                slave.last_error = str(exc)[:500]
                slave.updated_at = get_utc_now()
                db.commit()
                raise HTTPException(status_code=502, detail=str(exc)) from exc

            rate = get_usd_inr_rate(db)
            bal_usd = float(wallet.get("balance_usdt", 0.0) or 0.0)
            slave.balance_usd = bal_usd
            slave.balance_inr = round(bal_usd * rate, 2)
            slave.connection_status = "connected"
            slave.last_connected_at = get_utc_now()
            slave.last_error = None
        finally:
            await client.close()

    slave.updated_at = get_utc_now()
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
    slave.updated_at = get_utc_now()
    db.commit()
    msg = "enabled" if slave.is_active else "paused"
    return {
        "id": slave.id,
        "is_active": slave.is_active,
        "message": f"Slave '{slave.name}' {msg}",
    }


@router.post("/{slave_id}/close-structure")
async def close_slave_structure(
    slave_id: int,
    payload: SlaveForceCloseRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Force-close ONE slave's structure: baskets (verified) → hedge → ledger.

    Accepts earner_user_id in the body when the caller does not know slave_id.
    Idempotent when already flat. Does not touch master or other slaves.
    """
    from backend.engine.mirror_engine import FORCE_CLOSE_REASONS, mirror_engine

    reason_norm = str(payload.reason or "").upper().strip()
    if reason_norm not in FORCE_CLOSE_REASONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid reason '{payload.reason}'. "
                f"Use one of: {', '.join(sorted(FORCE_CLOSE_REASONS))}"
            ),
        )

    slave: SlaveAccount | None = None
    # Prefer path slave_id when supplied — never close a different account
    # that shares the same earner_user_id.
    if int(slave_id) > 0:
        slave = (
            db.query(SlaveAccount)
            .filter(SlaveAccount.id == int(slave_id))
            .first()
        )
        if slave is None:
            raise HTTPException(
                status_code=404,
                detail=f"Slave account {int(slave_id)} not found",
            )
    elif payload.earner_user_id:
        matches = (
            db.query(SlaveAccount)
            .filter(
                SlaveAccount.earner_user_id == str(payload.earner_user_id)
            )
            .order_by(SlaveAccount.id.asc())
            .all()
        )
        if not matches:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Slave account not found for "
                    f"earner_user_id={payload.earner_user_id}"
                ),
            )
        if len(matches) > 1:
            ids = [int(s.id) for s in matches]
            raise HTTPException(
                status_code=409,
                detail=(
                    "Multiple slave accounts share this earner_user_id; "
                    "pass slave_id in the path to choose one. "
                    f"matching_slave_ids={ids}"
                ),
            )
        slave = matches[0]
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide slave_id in the path or earner_user_id in the body",
        )

    if slave is None:
        raise HTTPException(
            status_code=404,
            detail="Slave account not found (check slave_id or earner_user_id)",
        )

    if mirror_engine is None:
        raise HTTPException(
            status_code=503,
            detail="Mirror engine not initialized",
        )

    try:
        outcome = await mirror_engine.force_close_slave_structure(
            slave_id=int(slave.id),
            reason=reason_norm,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.critical(
            "force_close_slave_structure failed slave=%s: %s",
            slave.id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Force close failed — see server logs",
        ) from exc

    db.expire(slave)
    db.refresh(slave)

    if not outcome.get("success"):
        failed = outcome.get("failed_baskets") or []
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Basket close failed — hedge not closed to avoid "
                    "naked short strangle"
                ),
                "slave_id": int(slave.id),
                "failed_baskets": failed,
            },
        )

    return {
        "success": True,
        "slave_id": int(slave.id),
        "earner_user_id": getattr(slave, "earner_user_id", None),
        "reason": reason_norm,
        "already_closed": bool(outcome.get("already_closed")),
        "baskets_found": int(outcome.get("baskets_found") or 0),
        "baskets_closed": int(outcome.get("baskets_closed") or 0),
        "hedge_closed": bool(outcome.get("hedge_closed")),
        "structures_closed": int(outcome.get("structures_closed") or 0),
        "is_active": bool(slave.is_active),
    }


@router.post("/accounts/{slave_id}/copy-master-trade")
async def copy_master_trade_to_slave(
    slave_id: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    One-click: place the master's current open strangle on this slave.

    Delegates to MirrorEngine._mirror_entry_to_slave so the same hedge
    guard, unwind, product_id fill, and structure-ledger recording apply
    as automatic mirror entry (Accounts UI still calls this endpoint).
    """
    from backend.engine.bot_engine import bot_engine
    from backend.engine import mirror_engine as mirror_module

    slave = db.query(SlaveAccount).filter(SlaveAccount.id == slave_id).first()
    if slave is None:
        raise HTTPException(status_code=404, detail="Slave account not found")

    if not slave.is_active:
        raise HTTPException(status_code=400, detail="Slave account is paused")

    me = mirror_module.mirror_engine
    if me is None:
        raise HTTPException(
            status_code=503,
            detail="Mirror engine not initialized",
        )

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
        return {
            "success": False,
            "message": "Master trade missing open call/put legs",
        }

    # Hedge invariant + entry under per-slave lock (same as mirror_trade_entry)
    master_qty = max(1, int(getattr(call_leg, "quantity", 1) or 1))
    uni_sl = float(getattr(state.trade, "universal_sl_pct", None) or 200.0)
    from backend.core.delta_sl import compute_bracket_sl

    call_sl = getattr(call_leg, "sl_trigger_price", None)
    put_sl = getattr(put_leg, "sl_trigger_price", None)
    if call_sl is None or float(call_sl) <= 0:
        call_base = float(getattr(call_leg, "initial_premium", 0) or 0)
        call_sl, _ = compute_bracket_sl(
            call_base, uni_sl, leg="call", trade_id=int(master_trade_id)
        )
        call_sl = call_sl if call_sl > 0 else None
    else:
        call_sl = round(float(call_sl), 2)
    if put_sl is None or float(put_sl) <= 0:
        put_base = float(getattr(put_leg, "initial_premium", 0) or 0)
        put_sl, _ = compute_bracket_sl(
            put_base, uni_sl, leg="put", trade_id=int(master_trade_id)
        )
        put_sl = put_sl if put_sl > 0 else None
    else:
        put_sl = round(float(put_sl), 2)

    underlying = str(getattr(state.trade, "underlying", "") or "")
    call_symbol = str(getattr(call_leg, "symbol", "") or "")
    put_symbol = str(getattr(put_leg, "symbol", "") or "")
    expiry = getattr(state.trade, "expiry_date", None)
    call_fill = float(getattr(call_leg, "initial_premium", 0) or 0)
    put_fill = float(getattr(put_leg, "initial_premium", 0) or 0)

    try:
        async with me._slave_op_lock(
            int(slave.id), "copy_master_trade"
        ) as acquired:
            if not acquired:
                return {
                    "success": False,
                    "message": "Slave busy (lock timeout) — retry later",
                    "status": "lock_timeout",
                }
            master_hedge_id = me._resolve_master_hedge_id_for_trade(
                db, int(master_trade_id)
            )
            ok_hedge, hedge_reason = me._assert_hedge_before_basket(
                db, int(slave.id), master_hedge_id
            )
            if not ok_hedge:
                me._skip_basket_no_hedge(
                    db,
                    slave=slave,
                    master_trade_id=int(master_trade_id),
                    master_hedge_id=master_hedge_id,
                    reason=hedge_reason,
                )
                return {
                    "success": False,
                    "message": (
                        f"Basket entry skipped: {hedge_reason} "
                        "(short basket requires a live hedge)"
                    ),
                    "status": "skipped_no_hedge",
                    "reason": hedge_reason,
                }
            await me._mirror_entry_to_slave(
                slave=slave,
                master_trade_id=int(master_trade_id),
                call_product_id=int(call_leg.product_id),
                put_product_id=int(put_leg.product_id),
                master_call_qty=master_qty,
                master_put_qty=max(
                    1,
                    int(
                        getattr(put_leg, "quantity", master_qty) or master_qty
                    ),
                ),
                master_call_strike=float(getattr(call_leg, "strike", 0) or 0),
                master_put_strike=float(getattr(put_leg, "strike", 0) or 0),
                master_call_symbol=call_symbol,
                master_put_symbol=put_symbol,
                master_call_fill=call_fill,
                master_put_fill=put_fill,
                expiry_date=expiry,
                underlying=underlying,
                db=db,
                master_bracket_sl_call=(
                    float(call_sl) if call_sl is not None else None
                ),
                master_bracket_sl_put=(
                    float(put_sl) if put_sl is not None else None
                ),
            )
    except Exception as exc:
        logger.error(
            "Copy trade to slave failed slave=%s master=%s: %s",
            slave.name,
            master_trade_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to copy trade: {exc}",
        ) from exc

    db.expire_all()
    st = (
        db.query(SlaveTrade)
        .filter(
            SlaveTrade.slave_account_id == int(slave.id),
            SlaveTrade.master_trade_id == int(master_trade_id),
        )
        .order_by(SlaveTrade.id.desc())
        .first()
    )
    if st is None:
        return {
            "success": False,
            "message": "Copy finished but no SlaveTrade row was created",
        }
    st_status = str(st.status or "")
    if st_status == "active":
        logger.info(
            "Master trade #%s copied to slave '%s' via mirror_entry "
            "qty=%s call=%s put=%s",
            master_trade_id,
            slave.name,
            st.actual_quantity,
            st.call_product_id,
            st.put_product_id,
        )
        return {
            "success": True,
            "message": f"Trade #{master_trade_id} copied to {slave.name}",
            "slave_qty": int(st.actual_quantity or 0),
            "call_fill": float(st.call_fill_price or 0),
            "put_fill": float(st.put_fill_price or 0),
            "call_order_id": st.call_order_id,
            "put_order_id": st.put_order_id,
            "call_product_id": st.call_product_id,
            "put_product_id": st.put_product_id,
            "master_trade_id": master_trade_id,
            "underlying": underlying,
            "status": st_status,
        }
    return {
        "success": False,
        "message": (
            st.last_error
            or f"Copy finished with status={st_status}"
        ),
        "status": st_status,
        "master_trade_id": master_trade_id,
    }


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
    from backend.core.balance_snapshots import build_balance_detail
    from backend.core.balance_utils import wallet_to_balance_fields
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
    master_blocked_usd = 0.0
    master_wallet: dict[str, float] | None = None
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
                master_wallet = wallet
                master_fields = wallet_to_balance_fields(wallet, usd_inr_rate=rate)
                master_balance_usd = float(master_fields.get("actual_balance") or 0.0)
                master_available_usd = float(
                    master_fields.get("available_balance") or 0.0
                )
                master_blocked_usd = float(master_fields.get("blocked_amount") or 0.0)
            finally:
                await client.close()
        except Exception as exc:
            logger.warning("Master balance fetch failed for overview: %s", exc)
            master_error = str(exc)
            master_connected = False

    master_balance_detail = (
        build_balance_detail(
            db,
            master_wallet,
            account_id=int(master_account.id),
            account_type="master",
            usd_inr_rate=rate,
        )
        if master_account is not None and master_wallet is not None
        else build_balance_detail(
            db,
            None,
            account_id=int(master_account.id) if master_account else 0,
            account_type="master",
            usd_inr_rate=rate,
        )
    )

    active_hedge_row = (
        db.query(HedgePosition)
        .filter(
            HedgePosition.account_id == int(master_account.id),
            HedgePosition.status == "active",
        )
        .order_by(HedgePosition.id.desc())
        .first()
        if master_account is not None
        else None
    )
    structure_net_mtm = (
        round(float(getattr(active_hedge_row, "structure_pnl", 0.0) or 0.0), 4)
        if active_hedge_row is not None
        else None
    )
    structure_target_usd = (
        round(float(getattr(active_hedge_row, "target_usd", 0.0) or 0.0), 4)
        if active_hedge_row is not None
        else None
    )

    # Master active trades from in-memory tracker (keyed by trade id)
    master_states = bot_engine.position_tracker.get_all_active()
    master_by_id: dict[int, Any] = {
        int(s.trade_id): s for s in master_states
    }

    def _tracker_mtm_fields(state: Any) -> dict[str, Any]:
        """Net/gross from basket_net_mtm_snapshot stored on the tracker."""
        from backend.core.fees import basket_net_mtm_snapshot
        from backend.core.time_utils import get_utc_now

        snap = getattr(state, "last_mtm_snapshot", None)
        if snap is not None:
            refreshed = basket_net_mtm_snapshot(
                gross_mtm=float(snap.get("gross_mtm", state.last_pnl) or 0),
                fees_paid=float(snap.get("fees_paid", 0) or 0),
                est_exit_fees=float(snap.get("est_exit_fees", 0) or 0),
                slippage_pct=float(snap.get("slippage_pct", 0) or 0),
                expected_exit_spread_usd=float(
                    snap.get("expected_exit_spread_usd", 0) or 0
                ),
                computed_at=getattr(state, "last_net_mtm_computed_at", None)
                or get_utc_now(),
            )
            return {
                "gross_mtm": float(refreshed["gross_mtm"]),
                "net_mtm": float(refreshed["net_mtm"]),
                "computed_at": refreshed.get("computed_at_iso"),
                "stale_seconds": refreshed.get("stale_seconds", 0.0),
            }
        gross_mtm = float(getattr(state, "last_pnl", 0) or 0)
        _raw_net = getattr(state, "last_net_mtm", None)
        net_mtm = (
            float(_raw_net)
            if _raw_net is not None and _raw_net != 0.0
            else gross_mtm
        )
        at = getattr(state, "last_net_mtm_computed_at", None)
        stale = None
        computed_iso = None
        if at is not None:
            computed_iso = at.isoformat()
            stale = round(max(0.0, (get_utc_now() - at).total_seconds()), 1)
        return {
            "gross_mtm": gross_mtm,
            "net_mtm": net_mtm,
            "computed_at": computed_iso,
            "stale_seconds": stale,
        }

    master_trade_data: dict[str, Any] | None = None
    if master_states:
        state = master_states[0]
        call_leg = state.call_leg
        put_leg = state.put_leg
        call_entry = float(getattr(call_leg, "initial_premium", 0) or 0)
        put_entry = float(getattr(put_leg, "initial_premium", 0) or 0)
        call_curr = float(getattr(state, "last_call_premium", 0) or 0)
        put_curr = float(getattr(state, "last_put_premium", 0) or 0)
        mtm_fields = _tracker_mtm_fields(state)
        basket_legs = _master_basket_legs(state, bot_engine)
        short_gross = _role_gross(basket_legs, "short_")
        wing_gross = _role_gross(basket_legs, "wing_")
        master_hedge_net = (
            float(getattr(active_hedge_row, "hedge_net_mtm", 0.0) or 0.0)
            if active_hedge_row is not None
            else None
        )
        master_closed = (
            float(getattr(active_hedge_row, "cum_closed_basket_pnl", 0.0) or 0.0)
            if active_hedge_row is not None
            else 0.0
        )
        master_hedge_block = _hedge_payload(
            hedge=active_hedge_row,
            bot_engine=bot_engine,
            hedge_net_mtm=master_hedge_net,
        )
        master_trade_data = {
            "trade_id": state.trade_id,
            "underlying": getattr(state.trade, "underlying", None),
            "basket_number": getattr(state.trade, "basket_number", None),
            "is_demo": bool(getattr(state.trade, "is_demo", False)),
            "net_mtm": mtm_fields["net_mtm"],
            "gross_mtm": mtm_fields["gross_mtm"],
            "computed_at": mtm_fields.get("computed_at"),
            "stale_seconds": mtm_fields.get("stale_seconds"),
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
            "legs": basket_legs,
            "hedge": master_hedge_block,
            "pnl": _build_pnl_block(
                hedge_net=master_hedge_net,
                short_gross=short_gross,
                wing_gross=wing_gross,
                basket_net=float(mtm_fields["net_mtm"]),
                closed_basket=master_closed,
                computed_at=mtm_fields.get("computed_at"),
                stale_seconds=mtm_fields.get("stale_seconds"),
            ),
        }

    def _master_snapshot(trade_id: int) -> dict[str, Any] | None:
        """Build master trade snapshot for a specific trade_id from tracker."""
        state = master_by_id.get(int(trade_id))
        if state is None:
            return None
        call_leg = state.call_leg
        put_leg = state.put_leg
        mtm_fields = _tracker_mtm_fields(state)
        return {
            "trade_id": state.trade_id,
            "underlying": getattr(state.trade, "underlying", None),
            "basket_number": getattr(state.trade, "basket_number", None),
            "is_demo": bool(getattr(state.trade, "is_demo", False)),
            "net_mtm": mtm_fields["net_mtm"],
            "gross_mtm": mtm_fields["gross_mtm"],
            "computed_at": mtm_fields.get("computed_at"),
            "stale_seconds": mtm_fields.get("stale_seconds"),
            "profit_target_usd": float(
                getattr(state.trade, "profit_target_usd", 0) or 0
            ),
            "call_strike": float(getattr(call_leg, "strike", 0) or 0),
            "put_strike": float(getattr(put_leg, "strike", 0) or 0),
            "call_premium": float(getattr(state, "last_call_premium", 0) or 0),
            "put_premium": float(getattr(state, "last_put_premium", 0) or 0),
            "expiry_date": str(getattr(state.trade, "expiry_date", "") or ""),
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
        is_virtual = bool(getattr(slave, "is_virtual", False))
        # Refresh balance opportunistically (skip for virtual — no real Delta)
        bal_usd = float(slave.balance_usd or 0.0)
        avail_usd: float | None = None
        blocked_usd: float | None = None
        slave_wallet: dict[str, float] | None = None
        if (
            not is_virtual
            and slave.is_active
            and slave.connection_status != "error"
        ):
            try:
                client = DeltaClient(
                    decrypt(slave.api_key_encrypted),
                    decrypt(slave.api_secret_encrypted),
                )
                try:
                    wallet = await client.get_wallet_balance()
                    slave_wallet = wallet
                    slave_fields = wallet_to_balance_fields(wallet, usd_inr_rate=rate)
                    bal_usd = float(slave_fields.get("actual_balance") or 0.0)
                    avail_usd = float(slave_fields.get("available_balance") or 0.0)
                    blocked_usd = float(slave_fields.get("blocked_amount") or 0.0)
                    slave.balance_usd = bal_usd
                    slave.balance_inr = round(bal_usd * rate, 2)
                    slave.connection_status = "connected"
                    slave.last_connected_at = get_utc_now()
                    slave.last_error = None
                    db.commit()
                finally:
                    await client.close()
            except Exception as exc:
                logger.warning(
                    "Slave %s balance refresh failed: %s", slave.name, exc
                )
                try:
                    db.rollback()
                except Exception:
                    pass
        elif is_virtual:
            # Prefer allocated capital for display when set
            allocated = getattr(slave, "user_allocated_capital", None)
            if allocated is not None and float(allocated) > 0:
                bal_usd = float(allocated)
                avail_usd = float(allocated)

        # Active mirrored trade — include VIRTUAL order_ids (paper slaves)
        active_st = (
            db.query(SlaveTrade)
            .filter(
                SlaveTrade.slave_account_id == int(slave.id),
                SlaveTrade.status == "active",
            )
            .order_by(SlaveTrade.id.desc())
            .first()
        )

        slave_trade_data: dict[str, Any] | None = None
        if active_st is not None:
            master_row = (
                db.query(Trade)
                .filter(Trade.id == active_st.master_trade_id)
                .first()
            )
            mult = float(slave.qty_multiplier or 1.0)
            last_updated_iso = _iso(active_st.last_updated)

            # Prefer the master snapshot that matches THIS slave's master trade
            # (strikes / target / live premiums for display only — NOT for MTM)
            linked_master = _master_snapshot(int(active_st.master_trade_id))
            pnl_source = linked_master or master_trade_data
            target_src = (
                float(linked_master["profit_target_usd"])
                if linked_master
                else master_target
            )

            # Own computed MTM from update_all_slave_mtm — never copy master
            # Convention: last_mtm stores gross when mtm_source=computed;
            # net_mtm stores net. Copied legacy rows set both to the same value.
            mtm_source = str(
                getattr(active_st, "mtm_source", None) or "copied"
            )
            slave_net_mtm = float(getattr(active_st, "net_mtm", None) or 0.0)
            slave_gross_mtm = float(active_st.last_mtm or 0.0)
            if mtm_source == "copied":
                slave_gross_mtm = slave_net_mtm

            slave_trade_data = {
                "slave_trade_id": active_st.id,
                "master_trade_id": active_st.master_trade_id,
                "actual_quantity": int(active_st.actual_quantity or 1),
                "call_fill_price": active_st.call_fill_price,
                "put_fill_price": active_st.put_fill_price,
                "call_order_id": active_st.call_order_id,
                "put_order_id": active_st.put_order_id,
                "is_virtual": is_virtual
                or str(active_st.call_order_id or "").upper() == "VIRTUAL",
                "status": active_st.status,
                "gross_mtm": round(slave_gross_mtm, 4),
                "last_mtm": round(slave_net_mtm, 4),
                "net_mtm": round(slave_net_mtm, 4),
                "mtm_source": mtm_source,
                "realized_pnl": (
                    float(active_st.realized_pnl)
                    if getattr(active_st, "realized_pnl", None) is not None
                    else None
                ),
                "last_updated": last_updated_iso,
                "last_mtm_updated": last_updated_iso,
                "net_mtm_updated": last_updated_iso,
                "last_error": active_st.last_error,
                "profit_target_usd": round(target_src * mult, 2)
                if target_src
                else None,
                "call_strike": (
                    float(getattr(active_st, "call_strike", None))
                    if getattr(active_st, "call_strike", None) is not None
                    else (
                        float(pnl_source["call_strike"])
                        if pnl_source and pnl_source.get("call_strike") is not None
                        else None
                    )
                ),
                "put_strike": (
                    float(getattr(active_st, "put_strike", None))
                    if getattr(active_st, "put_strike", None) is not None
                    else (
                        float(pnl_source["put_strike"])
                        if pnl_source and pnl_source.get("put_strike") is not None
                        else None
                    )
                ),
                "call_premium": (
                    float(pnl_source["call_premium"])
                    if pnl_source and pnl_source.get("call_premium") is not None
                    else None
                ),
                "put_premium": (
                    float(pnl_source["put_premium"])
                    if pnl_source and pnl_source.get("put_premium") is not None
                    else None
                ),
                "underlying": master_row.underlying if master_row else None,
                "basket_number": (
                    getattr(master_row, "basket_number", None)
                    if master_row
                    else None
                ),
                "is_demo_master": bool(
                    getattr(master_row, "is_demo", False) if master_row else False
                ),
                "expiry_date": (
                    str(getattr(master_row, "expiry_date", "") or "")
                    if master_row
                    else ""
                ),
            }

            # Full structure: legs + own slave hedge + per-role pnl
            master_state = master_by_id.get(int(active_st.master_trade_id))
            wc_now = None
            wp_now = None
            if master_state is not None:
                wc_leg = getattr(master_state, "wing_call_leg", None)
                wp_leg = getattr(master_state, "wing_put_leg", None)
                if wc_leg is not None:
                    wc_now = _live_px(
                        bot_engine,
                        getattr(wc_leg, "symbol", None),
                        float(getattr(wc_leg, "initial_premium", 0) or 0),
                    )
                if wp_leg is not None:
                    wp_now = _live_px(
                        bot_engine,
                        getattr(wp_leg, "symbol", None),
                        float(getattr(wp_leg, "initial_premium", 0) or 0),
                    )
            # Prefer slave's own wing symbols for live px when set
            if getattr(active_st, "wing_call_symbol", None):
                wc_now = _live_px(
                    bot_engine,
                    active_st.wing_call_symbol,
                    float(wc_now or 0),
                )
            if getattr(active_st, "wing_put_symbol", None):
                wp_now = _live_px(
                    bot_engine,
                    active_st.wing_put_symbol,
                    float(wp_now or 0),
                )

            slave_legs = _slave_basket_legs(
                active_st,
                call_now=(
                    float(slave_trade_data["call_premium"])
                    if slave_trade_data.get("call_premium") is not None
                    else None
                ),
                put_now=(
                    float(slave_trade_data["put_premium"])
                    if slave_trade_data.get("put_premium") is not None
                    else None
                ),
                wing_call_now=wc_now,
                wing_put_now=wp_now,
            )
            slave_short_gross = _role_gross(slave_legs, "short_")
            slave_wing_gross = _role_gross(slave_legs, "wing_")

            slave_hedge_row = (
                db.query(SlaveHedgePosition)
                .filter(
                    SlaveHedgePosition.slave_account_id == int(slave.id),
                    SlaveHedgePosition.status.in_(("active", "pending_close")),
                )
                .order_by(SlaveHedgePosition.id.desc())
                .first()
            )
            slave_hedge_net: float | None = None
            slave_closed = 0.0
            if slave_hedge_row is not None and active_hedge_row is not None:
                mq = max(1, int(active_hedge_row.quantity or 1))
                sq = max(1, int(slave_hedge_row.quantity or 1))
                # Live hedge MTM not stored on SlaveHedgePosition — scale master
                # hedge_net by lot ratio (same contracts, different size).
                slave_hedge_net = round(
                    float(getattr(active_hedge_row, "hedge_net_mtm", 0) or 0)
                    * (sq / mq),
                    4,
                )
                slave_closed = round(
                    float(
                        getattr(active_hedge_row, "cum_closed_basket_pnl", 0) or 0
                    )
                    * (sq / mq),
                    4,
                )
            elif slave_hedge_row is not None:
                slave_hedge_net = 0.0

            slave_hedge_block = _hedge_payload(
                slave_hedge=slave_hedge_row,
                bot_engine=bot_engine,
                hedge_net_mtm=slave_hedge_net,
            )
            slave_stale = None
            slave_computed = last_updated_iso
            if linked_master:
                slave_stale = linked_master.get("stale_seconds")
                slave_computed = linked_master.get("computed_at") or slave_computed

            slave_trade_data["legs"] = slave_legs
            slave_trade_data["hedge"] = slave_hedge_block
            slave_trade_data["pnl"] = _build_pnl_block(
                hedge_net=slave_hedge_net,
                short_gross=slave_short_gross,
                wing_gross=slave_wing_gross,
                basket_net=float(slave_net_mtm),
                closed_basket=slave_closed,
                computed_at=slave_computed,
                stale_seconds=slave_stale,
            )

        slaves_data.append(
            {
                "id": int(slave.id),
                "name": slave.name,
                "qty_multiplier": float(slave.qty_multiplier or 1.0),
                "is_active": bool(slave.is_active),
                "is_virtual": is_virtual,
                "earner_user_id": getattr(slave, "earner_user_id", None),
                "earner_subscription_id": getattr(
                    slave, "earner_subscription_id", None
                ),
                "connection_status": str(slave.connection_status or "unknown"),
                "balance_usd": bal_usd,
                "balance_inr": round(bal_usd * rate, 2),
                "available_usd": avail_usd,
                "available_inr": (
                    round(avail_usd * rate, 2) if avail_usd is not None else None
                ),
                "blocked_usd": blocked_usd,
                "blocked_inr": (
                    round(blocked_usd * rate, 2) if blocked_usd is not None else None
                ),
                **build_balance_detail(
                    db,
                    slave_wallet
                    if slave_wallet is not None
                    else (
                        {
                            "wallet_balance": bal_usd,
                            "available_balance": float(avail_usd or 0.0),
                            "position_margin": float(blocked_usd or 0.0),
                        }
                        if not is_virtual and slave.is_active and bal_usd > 0
                        else None
                    ),
                    account_id=int(slave.id),
                    account_type="slave",
                    usd_inr_rate=rate,
                ),
                "last_error": slave.last_error,
                "last_connected_at": _iso(slave.last_connected_at),
                "active_slave_trade": slave_trade_data,
                **_latest_slave_structure_close(db, int(slave.id)),
            }
        )

    combined_mtm = 0.0
    if structure_net_mtm is not None:
        combined_mtm += float(structure_net_mtm)
    elif master_trade_data:
        combined_mtm += float(master_trade_data.get("net_mtm") or 0)
    for s in slaves_data:
        st = s.get("active_slave_trade")
        if st:
            combined_mtm += float(st.get("net_mtm") or 0)

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
        "combined_structure_mtm": round(combined_mtm, 4),
        "master": {
            "account_id": int(master_account.id) if master_account else None,
            "name": master_name,
            "connected": master_connected,
            "balance_usd": master_balance_usd,
            "balance_inr": round(master_balance_usd * rate, 2),
            "available_usd": master_available_usd,
            "available_inr": round(master_available_usd * rate, 2),
            "blocked_usd": master_blocked_usd,
            "blocked_inr": round(master_blocked_usd * rate, 2),
            **master_balance_detail,
            "structure_net_mtm": structure_net_mtm,
            "target": structure_target_usd
            or (
                float(master_trade_data["profit_target_usd"])
                if master_trade_data
                else None
            ),
            "active_trade_count": master_active_count,
            "active_trade": master_trade_data,
            "last_error": master_error,
        },
        "slaves": slaves_data,
    }
