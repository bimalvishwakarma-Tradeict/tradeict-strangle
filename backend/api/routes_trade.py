# routes_trade.py — /api/trade/* endpoints for initiate, exit, settings, adjustments

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.config import (
    ExitReason,
    OPTIONS_CONTRACT_VALUE,
    SETTLING_PERIOD_AFTER_PLACE_MINUTES,
    TradeStatus,
    TriggerMode,
)
from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.encryption import decrypt
from backend.core.time_utils import (
    get_dte_label,
    get_hours_to_expiry,
    get_ist_now,
    get_settling_info_for_trade,
    get_utc_now,
    settling_ends_at_after_place,
    to_utc_for_db,
)
from backend.core.ws_manager import ws_manager
from backend.database import get_db, get_or_create_auto_settings
from backend.engine.bot_engine import bot_engine
from backend.engine.trade_reconcile import (
    book_leg_close,
    count_open_bot_legs,
    finalize_trade_if_flat,
    heal_zombie_active_trades,
    next_basket_number,
    next_basket_seq_in_structure,
    pick_call_put_legs,
    recompute_trade_realized_pnl,
    reconcile_open_legs_with_delta,
    resolve_external_exit_fill,
)
from backend.models import Account, Adjustment, Leg, Setting, Trade
from backend.schemas import (
    TradeExitRequest,
    TradeInitiateRequest,
    TradeRegisterExistingRequest,
    TradeSettingsUpdate,
)
from backend.strategies.s001_short_strangle.config import SUPPORTED_UNDERLYINGS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trade", tags=["trade"])


def _get_active_account(db: Session) -> Account:
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        raise HTTPException(
            status_code=401,
            detail="No account connected. Please add API keys in Settings.",
        )
    return account


def _build_delta_client(account: Account) -> DeltaClient:
    return DeltaClient(
        decrypt(account.api_key_encrypted),
        decrypt(account.api_secret_encrypted),
    )


def _to_ist_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_ist_now().tzinfo).isoformat()


def _upsert_setting(db: Session, trade_id: int, key: str, value: Any) -> None:
    row = (
        db.query(Setting)
        .filter(Setting.trade_id == trade_id, Setting.key == key)
        .first()
    )
    if row is None:
        db.add(Setting(trade_id=trade_id, key=key, value=str(value)))
    else:
        row.value = str(value)


def _leg_snapshot(leg: Any, current_premium: float) -> dict[str, Any]:
    from backend.config import OPTIONS_CONTRACT_VALUE
    from backend.core.delta_client import compute_signed_upnl

    initial = float(leg.initial_premium)
    is_closed = str(getattr(leg, "status", "") or "").lower() == "closed"
    is_long = bool(getattr(leg, "is_long", False))
    side = str(getattr(leg, "side", None) or ("BUY" if is_long else "SELL")).upper()
    qty = abs(int(leg.quantity or 0))
    display_prem = (
        float(leg.exit_premium)
        if is_closed and leg.exit_premium is not None
        else float(current_premium)
    )
    change_pct = ((display_prem / initial) - 1.0) * 100.0 if initial else 0.0
    if is_closed:
        leg_pnl = float(getattr(leg, "realized_pnl", None) or 0.0)
    else:
        size = float(qty) if is_long else -float(qty)
        leg_pnl = compute_signed_upnl(
            initial,
            display_prem,
            size=size,
            contract_value=OPTIONS_CONTRACT_VALUE,
        )
    entry_fee = getattr(leg, "entry_fee_usd", None)
    exit_fee = getattr(leg, "exit_fee_usd", None)
    return {
        "id": int(leg.id),
        "leg_type": str(getattr(leg, "leg_type", "") or ""),
        "strike": float(leg.strike),
        "symbol": leg.symbol,
        "quantity": qty,
        "initial_premium": initial,
        "trigger_baseline_premium": float(
            getattr(leg, "trigger_baseline_premium", None)
            or getattr(leg, "trigger_premium", None)
            or initial
        ),
        "current_premium": display_prem,
        "exit_premium": float(leg.exit_premium) if leg.exit_premium is not None else None,
        "entry_time": _to_ist_iso(getattr(leg, "entry_time", None)),
        "exit_time": _to_ist_iso(getattr(leg, "exit_time", None)),
        "change_pct": round(change_pct, 2),
        "leg_pnl": round(leg_pnl, 4),
        "realized_pnl": (
            round(float(leg.realized_pnl), 4)
            if getattr(leg, "realized_pnl", None) is not None
            else None
        ),
        "entry_fee_usd": round(float(entry_fee), 6) if entry_fee is not None else None,
        "exit_fee_usd": round(float(exit_fee), 6) if exit_fee is not None else None,
        "status": str(leg.status or "open"),
        "side": side,
        "is_long": is_long,
    }


def _basket_leg_history(db: Session, trade_id: int) -> list[dict[str, Any]]:
    """All bot-managed legs for a basket (open + closed), oldest first."""
    legs = (
        db.query(Leg)
        .filter(Leg.trade_id == trade_id, Leg.is_bot_managed.is_(True))
        .order_by(Leg.id.asc())
        .all()
    )
    rows: list[dict[str, Any]] = []
    for leg in legs:
        entry_fee = getattr(leg, "entry_fee_usd", None)
        exit_fee = getattr(leg, "exit_fee_usd", None)
        rows.append(
            {
                "id": int(leg.id),
                "leg_type": leg.leg_type,
                "strike": float(leg.strike),
                "symbol": leg.symbol,
                "quantity": int(leg.quantity),
                "entry_premium": float(leg.initial_premium),
                "exit_premium": (
                    float(leg.exit_premium) if leg.exit_premium is not None else None
                ),
                "entry_time": _to_ist_iso(leg.entry_time),
                "exit_time": _to_ist_iso(leg.exit_time),
                "status": leg.status,
                "realized_pnl": (
                    round(float(leg.realized_pnl), 4)
                    if leg.realized_pnl is not None
                    else None
                ),
                "entry_fee_usd": (
                    round(float(entry_fee), 6) if entry_fee is not None else None
                ),
                "exit_fee_usd": (
                    round(float(exit_fee), 6) if exit_fee is not None else None
                ),
                "fees_paid": round(
                    float(entry_fee or 0) + float(exit_fee or 0),
                    6,
                ),
            }
        )
    return rows


async def _backfill_leg_fees(client: Any, db: Session, legs: list[Any]) -> bool:
    """Fetch missing actual fees from Delta order/fills. Returns True if DB changed."""
    from backend.core.fees import leg_fees_paid  # noqa: F401 — keep import local unused ok

    changed = False
    if client is None:
        return False
    for leg in legs:
        if (
            getattr(leg, "entry_fee_usd", None) is None
            and getattr(leg, "delta_order_id", None)
        ):
            try:
                fee = await client.get_order_commission(leg.delta_order_id)
                if fee > 0:
                    leg.entry_fee_usd = float(fee)
                    changed = True
            except Exception as exc:
                logger.warning(
                    "entry fee backfill failed leg=%s: %s",
                    getattr(leg, "id", "?"),
                    exc,
                )
        if (
            str(getattr(leg, "status", "")).lower() == "closed"
            and getattr(leg, "exit_fee_usd", None) is None
            and getattr(leg, "exit_order_id", None)
        ):
            try:
                fee = await client.get_order_commission(leg.exit_order_id)
                if fee > 0:
                    leg.exit_fee_usd = float(fee)
                    changed = True
            except Exception as exc:
                logger.warning(
                    "exit fee backfill failed leg=%s: %s",
                    getattr(leg, "id", "?"),
                    exc,
                )
    if changed:
        db.commit()
    return changed


async def _fee_fields_for_basket(
    *,
    client: Any,
    db: Session,
    trade: Any,
    all_legs: list[Any],
    open_call: Any | None,
    open_put: Any | None,
    call_offer: float,
    put_offer: float,
) -> dict[str, Any]:
    """Build fee payload: paid (actual) + est exit (formula) + net MTM inputs."""
    from backend.core.fees import build_fee_summary, estimate_option_trading_fee

    await _backfill_leg_fees(client, db, all_legs)

    basket_closed = str(getattr(trade, "status", "")).lower() != TradeStatus.ACTIVE.value
    est_map: dict[int, float] = {}
    call_est = 0.0
    put_est = 0.0
    btc_index = 0.0
    if client is not None and not basket_closed:
        try:
            btc_index = float(await client.get_btc_index_price())
        except Exception as exc:
            logger.warning("BTC index for fee estimate failed: %s", exc)

    if not basket_closed and btc_index > 0:
        if open_call is not None and str(open_call.status).lower() == "open":
            call_est = estimate_option_trading_fee(
                option_price=float(call_offer or open_call.initial_premium or 0),
                quantity_lots=int(open_call.quantity),
                btc_index_price=btc_index,
            )
            est_map[int(open_call.id)] = call_est
        if open_put is not None and str(open_put.status).lower() == "open":
            put_est = estimate_option_trading_fee(
                option_price=float(put_offer or open_put.initial_premium or 0),
                quantity_lots=int(open_put.quantity),
                btc_index_price=btc_index,
            )
            est_map[int(open_put.id)] = put_est

    summary = build_fee_summary(
        legs=all_legs,
        open_leg_estimates=est_map,
        basket_closed=basket_closed,
    )
    return {
        "fees_paid": summary["fees_paid"],
        "est_exit_fees": summary["est_exit_fees"],
        "total_expected_fees": summary["total_expected_fees"],
        "call_entry_fee": (
            float(open_call.entry_fee_usd)
            if open_call is not None and open_call.entry_fee_usd is not None
            else None
        ),
        "put_entry_fee": (
            float(open_put.entry_fee_usd)
            if open_put is not None and open_put.entry_fee_usd is not None
            else None
        ),
        "call_est_exit_fee": call_est if not basket_closed else 0.0,
        "put_est_exit_fee": put_est if not basket_closed else 0.0,
        "btc_index_for_fees": btc_index or None,
    }


def _validate_initiate_common(payload: TradeInitiateRequest) -> tuple[str, date]:
    underlying = payload.underlying.upper().strip()
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported underlying. Use one of {SUPPORTED_UNDERLYINGS}",
        )
    if payload.trigger_mode not in {
        TriggerMode.FLAT.value,
        TriggerMode.SLAB.value,
        TriggerMode.PREMIUM.value,
    }:
        raise HTTPException(
            status_code=400,
            detail="trigger_mode must be 'flat', 'slab', or 'premium'",
        )
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    if float(getattr(payload, "tp_pct", 0) or 0) <= 0:
        raise HTTPException(status_code=400, detail="tp_pct must be > 0")
    if float(getattr(payload, "sl_pct", 0) or 0) <= 0:
        raise HTTPException(status_code=400, detail="sl_pct must be > 0")
    try:
        expiry = date.fromisoformat(payload.expiry_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expiry_date must be YYYY-MM-DD") from exc
    return underlying, expiry


async def _ensure_no_active_trade(
    db: Session, account: Account, underlying: str
) -> None:
    """
    Block new basket only if an ACTIVE trade still has open bot legs.
    Reconciles with Delta first so externally closed positions don't block.
    """
    client = _build_delta_client(account)
    try:
        recon = await reconcile_open_legs_with_delta(
            db=db,
            client=client,
            position_tracker=bot_engine.position_tracker,
        )
        for tid in recon.get("fully_closed") or []:
            bot_engine.position_tracker.mark_closed(tid)
            await ws_manager.broadcast(
                {
                    "type": "TRADE_CLOSED",
                    "trade_id": tid,
                    "reason": ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value,
                    "message": "Basket closed after Delta reconcile (no open size)",
                }
            )
        for alert in recon.get("naked_risk") or []:
            tid = int(alert["trade_id"])
            state = bot_engine.position_tracker.get(tid)
            if state is not None:
                await bot_engine._emergency_close_remaining_leg(
                    state, str(alert["remaining"])
                )
    except Exception as exc:
        logger.warning("Pre-initiate reconcile failed: %s", exc)
        heal_zombie_active_trades(db)
    finally:
        await client.close()

    # Any ACTIVE with ≥1 open bot-managed leg still blocks
    actives = (
        db.query(Trade)
        .filter(
            Trade.account_id == account.id,
            Trade.underlying == underlying,
            Trade.status == TradeStatus.ACTIVE.value,
        )
        .all()
    )
    for trade in actives:
        if count_open_bot_legs(db, trade.id) > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Active basket #{getattr(trade, 'basket_number', trade.id)} "
                    f"still has open legs for {underlying}. "
                    "Close remaining legs before opening a new one."
                ),
            )
        # Flat zombie — close it
        trade.status = TradeStatus.CLOSED.value
        if trade.exit_time is None:
            trade.exit_time = get_utc_now()
        if not trade.exit_reason:
            trade.exit_reason = ExitReason.MANUAL_LEG_CLOSE.value
        bot_engine.position_tracker.mark_closed(int(trade.id))
    db.commit()


async def _manual_entry_triple_guard(
    db: Session,
    account: Account,
    underlying: str,
    delta_client: DeltaClient,
) -> None:
    """
    Triple guard before manual order placement.

    1) DB active trade  2) position tracker  3) Delta positions (warn only)
    """
    # Guard 1: DB
    existing_db = (
        db.query(Trade)
        .filter(
            Trade.account_id == account.id,
            Trade.underlying == underlying,
            Trade.status == TradeStatus.ACTIVE.value,
        )
        .first()
    )
    if existing_db is not None and count_open_bot_legs(db, existing_db.id) > 0:
        log_and_buffer(
            "ENTRY_GUARD_BLOCK",
            int(existing_db.id),
            {"source": "manual", "guard": "db", "underlying": underlying},
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Active {underlying} trade already exists in DB "
                f"(id={existing_db.id})"
            ),
        )

    # Guard 2: Position tracker
    for state in bot_engine.position_tracker.get_all_active():
        state_underlying = str(
            getattr(getattr(state, "trade", None), "underlying", "") or ""
        ).upper()
        if state_underlying == underlying.upper():
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                int(state.trade_id),
                {"source": "manual", "guard": "tracker", "underlying": underlying},
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Active {underlying} trade in position tracker "
                    f"(id={state.trade_id})"
                ),
            )

    # Guard 3: Delta — warn only for manual (user may know about positions)
    logger.info("Checking Delta for existing %s positions...", underlying)
    delta_positions = await delta_client.get_option_positions()
    und = underlying.upper()
    matching = [
        p
        for p in delta_positions
        if und in str(p.get("product_symbol") or p.get("symbol") or "").upper()
    ]
    if matching:
        symbols = [p.get("product_symbol") or p.get("symbol") for p in matching]
        logger.warning(
            "Delta has %s open %s option positions: %s. "
            "Proceeding with caution — user confirmed manual entry.",
            len(matching),
            underlying,
            symbols,
        )
        log_and_buffer(
            "POSITION_WARNING",
            0,
            {
                "source": "manual_pre_entry",
                "underlying": underlying,
                "symbols": symbols,
                "action": "warn_continue",
            },
        )
    else:
        log_and_buffer(
            "ENTRY_GUARD_PASS",
            0,
            {"source": "manual", "underlying": underlying, "delta_open": 0},
        )
        logger.info("Step 1: All guards passed, placing CALL order")


async def _verify_leg_on_delta(
    client: DeltaClient,
    *,
    product_id: int,
    leg_label: str,
    order_id: str | None,
    trade_id: int = 0,
) -> None:
    """Best-effort verify position visible on Delta after fill."""
    await asyncio.sleep(1)
    exists = await client.verify_position_exists(int(product_id))
    if not exists:
        logger.warning(
            "%s order placed (id=%s) but position product_id=%s "
            "not yet visible on Delta. Proceeding anyway.",
            leg_label,
            order_id,
            product_id,
        )
        log_and_buffer(
            "POSITION_WARNING",
            trade_id,
            {
                "leg": leg_label.lower(),
                "product_id": int(product_id),
                "order_id": order_id,
            },
        )
    else:
        logger.info("%s position verified on Delta", leg_label)
        log_and_buffer(
            "POSITION_VERIFIED",
            trade_id,
            {"leg": leg_label.lower(), "product_id": int(product_id)},
        )


async def _persist_strangle_trade(
    *,
    db: Session,
    account: Account,
    payload: TradeInitiateRequest,
    underlying: str,
    expiry: date,
    call_fill_price: float,
    put_fill_price: float,
    call_order_id: str | None,
    put_order_id: str | None,
    call_sl_trigger_price: float | None = None,
    put_sl_trigger_price: float | None = None,
    monitoring_starts: datetime,
    call_entry_fee: float | None = None,
    put_entry_fee: float | None = None,
    call_sent_price: float | None = None,
    put_sent_price: float | None = None,
    call_opened_at: Any = None,
    put_opened_at: Any = None,
    call_fill_at: Any = None,
    put_fill_at: Any = None,
) -> tuple[Trade, Leg, Leg]:
    now_utc = get_utc_now()
    qty = int(payload.quantity)
    # Premium points collected (display / accounting)
    total_premium = (call_fill_price + put_fill_price) * qty
    # Locked USD max profit from INITIAL fills only
    # TP/SL locked to initial deployment premium
    # initial_max_profit never changes after trade entry
    # adjustments do NOT affect TP/SL
    initial_max_profit = round(
        (float(call_fill_price) + float(put_fill_price))
        * qty
        * float(OPTIONS_CONTRACT_VALUE),
        6,
    )
    tp_pct = float(getattr(payload, "tp_pct", None) or 50.0)
    sl_pct = float(getattr(payload, "sl_pct", None) or 100.0)
    profit_target_usd = round(initial_max_profit * tp_pct / 100.0, 2)
    stoploss_usd = round(initial_max_profit * sl_pct / 100.0, 2)
    basket_no = next_basket_number(db, account.id)
    # Manual initiate has no hedge link unless caller stamped one later
    hedge_id = getattr(payload, "hedge_position_id", None)
    seq_in_structure = next_basket_seq_in_structure(
        db, int(hedge_id) if hedge_id is not None else None
    )

    trade = Trade(
        account_id=account.id,
        underlying=underlying,
        expiry_date=expiry,
        status=TradeStatus.ACTIVE.value,
        entry_time=now_utc,
        total_premium_collected=total_premium,
        initial_max_profit=initial_max_profit,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        profit_target_usd=profit_target_usd,
        stoploss_usd=stoploss_usd,
        slippage_pct=float(getattr(payload, "slippage_pct", None) or 2.0),
        universal_sl_pct=float(getattr(payload, "universal_sl_pct", None) or 200.0),
        trigger_mode=payload.trigger_mode,
        combined_trigger_mode=bool(
            getattr(payload, "combined_trigger_mode", False)
        ),
        notes=None,
        realized_pnl=0.0,
        monitoring_starts_at=to_utc_for_db(monitoring_starts, context="trades.monitoring_starts_at"),
        basket_number=basket_no,
        basket_seq_in_structure=seq_in_structure,
        entry_spread_for_sl_usd=0.0,
        is_demo=bool(getattr(payload, "is_demo", False)),
        hedge_position_id=(
            int(hedge_id) if hedge_id is not None else None
        ),
    )
    db.add(trade)
    db.flush()

    from backend.core.bot_logger import log_tp_sl_locked
    from backend.core.fees import (
        compute_entry_spread_usd,
        reset_entry_spread_for_sl,
    )

    log_tp_sl_locked(
        trade_id=int(trade.id),
        initial_max_profit=initial_max_profit,
        profit_target_usd=profit_target_usd,
        stoploss_usd=stoploss_usd,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
    )

    call_sent = float(
        call_sent_price
        if call_sent_price is not None and float(call_sent_price) > 0
        else call_fill_price
    )
    put_sent = float(
        put_sent_price
        if put_sent_price is not None and float(put_sent_price) > 0
        else put_fill_price
    )
    call_entry_spread = compute_entry_spread_usd(
        sent_price=call_sent,
        fill_price=float(call_fill_price),
        quantity=qty,
        is_long=False,
    )
    put_entry_spread = compute_entry_spread_usd(
        sent_price=put_sent,
        fill_price=float(put_fill_price),
        quantity=qty,
        is_long=False,
    )

    call_leg = Leg(
        trade_id=trade.id,
        leg_type="call",
        strike=float(payload.call_strike),
        symbol=payload.call_symbol,
        product_id=int(payload.call_product_id),
        initial_premium=float(call_fill_price),
        trigger_baseline_premium=float(call_fill_price),
        trigger_premium=float(call_fill_price),
        quantity=qty,
        entry_time=now_utc,
        status="open",
        delta_at_entry=payload.call_delta_at_entry,
        is_bot_managed=True,
        delta_order_id=call_order_id,
        sl_trigger_price=(
            float(call_sl_trigger_price)
            if call_sl_trigger_price is not None
            else None
        ),
        delta_sl_order_id=None,
        entry_fee_usd=(
            abs(float(call_entry_fee)) if call_entry_fee is not None else None
        ),
        order_sent_price=call_sent,
        entry_spread_usd=call_entry_spread,
    )
    put_leg = Leg(
        trade_id=trade.id,
        leg_type="put",
        strike=float(payload.put_strike),
        symbol=payload.put_symbol,
        product_id=int(payload.put_product_id),
        initial_premium=float(put_fill_price),
        trigger_baseline_premium=float(put_fill_price),
        trigger_premium=float(put_fill_price),
        quantity=qty,
        entry_time=now_utc,
        status="open",
        delta_at_entry=payload.put_delta_at_entry,
        is_bot_managed=True,
        delta_order_id=put_order_id,
        sl_trigger_price=(
            float(put_sl_trigger_price)
            if put_sl_trigger_price is not None
            else None
        ),
        delta_sl_order_id=None,  # bracket has no separate stop-order ID
        entry_fee_usd=(
            abs(float(put_entry_fee)) if put_entry_fee is not None else None
        ),
        order_sent_price=put_sent,
        entry_spread_usd=put_entry_spread,
    )
    # Newest entry event = both opening legs together
    reset_entry_spread_for_sl(
        trade,
        abs(float(call_entry_spread or 0.0)) + abs(float(put_entry_spread or 0.0)),
        reason="trade_entry",
        leg="call+put",
    )
    db.add(call_leg)
    db.add(put_leg)

    settings_map: dict[str, Any] = {
        "trigger_mode": payload.trigger_mode,
        "slab_24h": payload.slab_24h,
        "slab_12h": payload.slab_12h,
        "slab_6h": payload.slab_6h,
        "slab_lt6h": payload.slab_lt6h,
    }
    if payload.flat_trigger_pct is not None:
        settings_map["flat_trigger_pct"] = payload.flat_trigger_pct
    if payload.trigger_mode == TriggerMode.PREMIUM.value:
        settings_map.update(
            {
                "premium_slab_300": getattr(payload, "premium_slab_300", 150.0),
                "premium_slab_200": getattr(payload, "premium_slab_200", 160.0),
                "premium_slab_100": getattr(payload, "premium_slab_100", 180.0),
                "premium_slab_lt100": getattr(payload, "premium_slab_lt100", 200.0),
            }
        )
    for key, value in settings_map.items():
        db.add(Setting(trade_id=trade.id, key=key, value=str(value)))

    db.commit()
    db.refresh(trade)
    db.refresh(call_leg)
    db.refresh(put_leg)
    try:
        from backend.engine.structure_ledger import record_master_basket_entry

        record_master_basket_entry(
            db,
            trade,
            call_leg,
            put_leg,
            call_opened_at=call_opened_at,
            put_opened_at=put_opened_at,
            call_fill_at=call_fill_at,
            put_fill_at=put_fill_at,
        )
        db.commit()
    except Exception as ledger_exc:
        logger.error(
            "structure ledger master basket entry failed: %s",
            ledger_exc,
            exc_info=True,
        )
    return trade, call_leg, put_leg


@router.post("/initiate")
async def initiate_trade(
    payload: TradeInitiateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Place short call + put on Delta Exchange, then register for bot monitoring.

    Uses ACTUAL fill prices from Delta (mark price fallback). Settling: 2 minutes.
    """
    underlying, expiry = _validate_initiate_common(payload)
    account = _get_active_account(db)
    await _ensure_no_active_trade(db, account, underlying)

    client = _build_delta_client(account)
    call_order_id: str | None = None
    put_order_id: str | None = None
    call_fill_price = 0.0
    put_fill_price = 0.0
    uni_sl = float(getattr(payload, "universal_sl_pct", None) or 200.0)
    call_sl_trigger_price: float | None = None
    put_sl_trigger_price: float | None = None

    try:
        await _manual_entry_triple_guard(db, account, underlying, client)

        import time as _time

        is_demo = bool(getattr(payload, "is_demo", False))

        # Chicken-and-egg: bracket must ship WITH the entry order before fill.
        # Attach mark × uni_sl at place; after fill try amend to fill-derived.
        # If amend fails, mark-derived stays canonical for master AND slaves.
        from backend.core.delta_sl import (
            compute_bracket_sl,
            finalize_bracket_sl_after_fill,
        )

        call_mark_for_sl = float(getattr(payload, "call_entry_premium", 0) or 0)
        put_mark_for_sl = float(getattr(payload, "put_entry_premium", 0) or 0)
        if call_mark_for_sl <= 0:
            call_mark_for_sl = float(await client.get_mark_price(payload.call_symbol))
        if put_mark_for_sl <= 0:
            put_mark_for_sl = float(await client.get_mark_price(payload.put_symbol))

        call_prov_sl, call_prov_limit = compute_bracket_sl(
            call_mark_for_sl,
            uni_sl,
            master_mark=call_mark_for_sl,
            leg="call",
            trade_id=None,
        )
        put_prov_sl, put_prov_limit = compute_bracket_sl(
            put_mark_for_sl,
            uni_sl,
            master_mark=put_mark_for_sl,
            leg="put",
            trade_id=None,
        )
        call_sl_trigger_price = float(call_prov_sl)
        put_sl_trigger_price = float(put_prov_sl)
        call_sl_limit = float(call_prov_limit)
        put_sl_limit = float(put_prov_limit)

        call_result = None
        put_result = None
        call_open_ts = None
        call_fill_ts = None
        put_open_ts = None
        put_fill_ts = None

        if is_demo:
            # Virtual trade: live mark prices instead of placing real orders
            call_open_ts = get_utc_now()
            logger.info(
                "[DEMO] Virtual master trade — fetching live prices "
                "instead of placing orders"
            )
            try:
                call_ticker = await client.get_ticker(payload.call_symbol)
                put_ticker = await client.get_ticker(payload.put_symbol)
                call_fill_price = float(
                    call_ticker.get("mark_price")
                    or call_ticker.get("close")
                    or 0
                )
                put_fill_price = float(
                    put_ticker.get("mark_price")
                    or put_ticker.get("close")
                    or 0
                )
            except Exception as e:
                logger.warning(
                    "[DEMO] Could not fetch live prices: %s — using marks",
                    e,
                )
                call_fill_price = float(
                    await client.get_mark_price(payload.call_symbol)
                )
                put_fill_price = float(
                    await client.get_mark_price(payload.put_symbol)
                )
            if call_fill_price <= 0:
                call_fill_price = float(call_mark_for_sl or 0)
            if put_fill_price <= 0:
                put_fill_price = float(put_mark_for_sl or 0)
            if call_fill_price <= 0 or put_fill_price <= 0:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "[DEMO] Could not resolve mark prices for virtual "
                        "trade fills"
                    ),
                )
            call_fill_ts = get_utc_now()
            put_open_ts = call_open_ts
            put_fill_ts = call_fill_ts
            ts = int(_time.time())
            call_order_id = f"DEMO-CALL-{ts}"
            put_order_id = f"DEMO-PUT-{ts}"
            # Demo: resolve canonical from fill vs mark without exchange amend
            call_sl_trigger_price, call_sl_limit = await finalize_bracket_sl_after_fill(
                None,
                entry_order_id=None,
                product_id=0,
                mark_price=call_mark_for_sl,
                fill_price=call_fill_price,
                universal_sl_pct=uni_sl,
                provisional_stop=call_prov_sl,
                provisional_limit=call_prov_limit,
                leg="call",
                trade_id=None,
            )
            put_sl_trigger_price, put_sl_limit = await finalize_bracket_sl_after_fill(
                None,
                entry_order_id=None,
                product_id=0,
                mark_price=put_mark_for_sl,
                fill_price=put_fill_price,
                universal_sl_pct=uni_sl,
                provisional_stop=put_prov_sl,
                provisional_limit=put_prov_limit,
                leg="put",
                trade_id=None,
            )
            logger.info(
                "[DEMO] Virtual fills: call=%s put=%s call_id=%s put_id=%s "
                "bracket_sl call=%s put=%s",
                call_fill_price,
                put_fill_price,
                call_order_id,
                put_order_id,
                call_sl_trigger_price,
                put_sl_trigger_price,
            )
        else:
            # --- Step 1: Place CALL with inline bracket (mark-derived) ---
            logger.info("Step 1: Placing call order %s", payload.call_symbol)
            call_open_ts = get_utc_now()
            call_result = await bot_engine.order_executor.sell_option(
                product_id=int(payload.call_product_id),
                quantity=int(payload.quantity),
                delta_client=client,
                symbol_for_fallback=payload.call_symbol,
                bracket_sl_price=call_prov_sl if call_prov_sl > 0 else None,
                bracket_sl_limit=call_prov_limit if call_prov_sl > 0 else None,
            )
            if not call_result.success:
                raise HTTPException(
                    status_code=502,
                    detail=f"Call order failed: {call_result.error or 'unknown error'}",
                )
            call_fill_ts = get_utc_now()
            call_order_id = (
                str(call_result.order_id) if call_result.order_id is not None else None
            )
            logger.info(
                "Step 2: Call order response order_id=%s filled_price=%s success=%s",
                call_order_id,
                call_result.filled_price,
                call_result.success,
            )

            call_fill_price = float(call_result.filled_price or 0.0)
            if call_fill_price <= 0 and call_order_id:
                call_fill_price = await client.resolve_fill_price(
                    {"order_id": call_order_id},
                    symbol_for_fallback=None,
                )
            if call_fill_price <= 0:
                call_fill_price = float(await client.get_mark_price(payload.call_symbol))
                logger.warning("Call fill unavailable, using mark: %s", call_fill_price)
            if call_fill_price <= 0:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Call order placed but fill/mark price could not be resolved. "
                        f"Call order ID: {call_order_id}"
                    ),
                )
            logger.info("Step 3: Call fill price: %s", call_fill_price)
            call_sl_trigger_price, call_sl_limit = await finalize_bracket_sl_after_fill(
                client,
                entry_order_id=call_order_id,
                product_id=int(payload.call_product_id),
                mark_price=call_mark_for_sl,
                fill_price=call_fill_price,
                universal_sl_pct=uni_sl,
                provisional_stop=call_prov_sl,
                provisional_limit=call_prov_limit,
                leg="call",
                trade_id=None,
            )
            await _verify_leg_on_delta(
                client,
                product_id=int(payload.call_product_id),
                leg_label="Call",
                order_id=call_order_id,
            )

            # --- Step 4: Place PUT with inline bracket (mark-derived) ---
            logger.info("Step 4: Placing put order %s", payload.put_symbol)
            put_open_ts = get_utc_now()
            put_result = await bot_engine.order_executor.sell_option(
                product_id=int(payload.put_product_id),
                quantity=int(payload.quantity),
                delta_client=client,
                symbol_for_fallback=payload.put_symbol,
                bracket_sl_price=put_prov_sl if put_prov_sl > 0 else None,
                bracket_sl_limit=put_prov_limit if put_prov_sl > 0 else None,
            )
            if not put_result.success:
                logger.critical(
                    "PUT order FAILED after call was placed! "
                    "Call order_id=%s at %s. Manual intervention needed!",
                    call_order_id,
                    call_fill_price,
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"PARTIAL FILL: Call placed @ {call_fill_price} "
                        f"but Put order failed ({put_result.error or 'unknown'}). "
                        f"Check Delta Exchange manually. Call order ID: {call_order_id}"
                    ),
                )
            put_fill_ts = get_utc_now()

            put_order_id = (
                str(put_result.order_id) if put_result.order_id is not None else None
            )
            logger.info(
                "Step 5: Put order response order_id=%s filled_price=%s success=%s",
                put_order_id,
                put_result.filled_price,
                put_result.success,
            )

            put_fill_price = float(put_result.filled_price or 0.0)
            if put_fill_price <= 0 and put_order_id:
                put_fill_price = await client.resolve_fill_price(
                    {"order_id": put_order_id},
                    symbol_for_fallback=None,
                )
            if put_fill_price <= 0:
                put_fill_price = float(await client.get_mark_price(payload.put_symbol))
                logger.warning("Put fill unavailable, using mark: %s", put_fill_price)
            if put_fill_price <= 0:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "PARTIAL FILL RISK: Put may have filled but price unknown. "
                        f"Call order ID: {call_order_id}, Put order ID: {put_order_id}"
                    ),
                )
            logger.info("Step 6: Put fill price: %s", put_fill_price)
            put_sl_trigger_price, put_sl_limit = await finalize_bracket_sl_after_fill(
                client,
                entry_order_id=put_order_id,
                product_id=int(payload.put_product_id),
                mark_price=put_mark_for_sl,
                fill_price=put_fill_price,
                universal_sl_pct=uni_sl,
                provisional_stop=put_prov_sl,
                provisional_limit=put_prov_limit,
                leg="put",
                trade_id=None,
            )
            await _verify_leg_on_delta(
                client,
                product_id=int(payload.put_product_id),
                leg_label="Put",
                order_id=put_order_id,
            )

        # --- Step 7–8: Save to DB ---
        logger.info("Step 7: Saving to DB%s...", " (DEMO)" if is_demo else "")
        from backend.config import ENTRY_SETTLING_SECONDS
        from backend.database import get_or_create_auto_settings

        _entry_secs = int(ENTRY_SETTLING_SECONDS)
        try:
            _ats = get_or_create_auto_settings(db)
            _raw = getattr(_ats, "entry_settling_seconds", None)
            if _raw is not None:
                _entry_secs = int(_raw)
        except Exception:
            pass
        _entry_secs = max(0, min(300, _entry_secs))
        monitoring_starts = settling_ends_at_after_place(seconds=_entry_secs)
        try:
            trade, call_leg, put_leg = await _persist_strangle_trade(
                db=db,
                account=account,
                payload=payload,
                underlying=underlying,
                expiry=expiry,
                call_fill_price=call_fill_price,
                put_fill_price=put_fill_price,
                call_order_id=call_order_id,
                put_order_id=put_order_id,
                call_sl_trigger_price=call_sl_trigger_price,
                put_sl_trigger_price=put_sl_trigger_price,
                monitoring_starts=monitoring_starts,
                call_entry_fee=(
                    float(call_result.commission)
                    if call_result is not None and call_result.commission is not None
                    else (0.0 if is_demo else None)
                ),
                put_entry_fee=(
                    float(put_result.commission)
                    if put_result is not None and put_result.commission is not None
                    else (0.0 if is_demo else None)
                ),
                call_sent_price=call_mark_for_sl,
                put_sent_price=put_mark_for_sl,
                call_opened_at=call_open_ts,
                put_opened_at=put_open_ts,
                call_fill_at=call_fill_ts,
                put_fill_at=put_fill_ts,
            )
            logger.info(
                "Step 8: Trade saved, id=%s is_demo=%s",
                trade.id,
                is_demo,
            )
        except Exception as exc:
            db.rollback()
            logger.critical(
                "DB save failed after orders placed! "
                "Call order: %s Put order: %s Error: %s",
                call_order_id,
                put_order_id,
                exc,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "Orders placed on Delta but DB save failed! "
                    f"Call ID: {call_order_id}, Put ID: {put_order_id}. "
                    "Check Delta Exchange manually."
                ),
            ) from exc

        # Soft warning: bracket SL trigger should be above adjustment trigger %.
        # (For SHORT strangles, Delta SL triggers when premium spikes.)
        max_trig = 150.0
        if payload.trigger_mode == "flat" and payload.flat_trigger_pct:
            max_trig = float(payload.flat_trigger_pct)
        elif payload.trigger_mode == "slab":
            max_trig = max(
                float(payload.slab_24h or 0),
                float(payload.slab_12h or 0),
                float(payload.slab_6h or 0),
                float(payload.slab_lt6h or 0),
            )
        elif payload.trigger_mode == "premium":
            max_trig = max(
                float(payload.premium_slab_300 or 0),
                float(payload.premium_slab_200 or 0),
                float(payload.premium_slab_100 or 0),
                float(payload.premium_slab_lt100 or 0),
            )
        if uni_sl <= max_trig:
            logger.warning(
                "universal_sl_pct=%.1f <= max trigger %.1f — Delta may "
                "close before bot can adjust",
                uni_sl,
                max_trig,
            )

        # With bracket orders, SL attachment is part of the entry order.
        # If entry succeeded, both bracket SL triggers were attached.
        delta_sl_ok = bool(
            (call_sl_trigger_price is not None and call_sl_trigger_price > 0)
            and (put_sl_trigger_price is not None and put_sl_trigger_price > 0)
        )
        delta_sl_warning = None

        db.expunge(trade)
        db.expunge(call_leg)
        db.expunge(put_leg)
        bot_engine.position_tracker.add(trade, call_leg, put_leg)
        state = bot_engine.position_tracker.get(trade.id)
        if state is None:
            logger.critical(
                "Trade %s NOT in position tracker after add()! "
                "This is a bug — trade will not be monitored!",
                trade.id,
            )
            log_and_buffer(
                "ERROR",
                int(trade.id),
                {"stage": "tracker_add", "error": "missing_after_add"},
            )
        else:
            logger.info(
                "Trade %s confirmed in position tracker (is_demo=%s)",
                trade.id,
                bool(getattr(trade, "is_demo", False)),
            )
            log_and_buffer(
                "ENTRY_GUARD_PASS",
                int(trade.id),
                {
                    "stage": "tracker_confirmed",
                    "source": "manual_demo" if is_demo else "manual",
                    "is_demo": is_demo,
                },
            )
        logger.info(
            "Step 9: Added to position tracker trade_id=%s active_count=%s "
            "(P&L checks start at %s IST) delta_sl_ok=%s",
            trade.id,
            len(bot_engine.position_tracker.get_all_active()),
            monitoring_starts.isoformat(),
            delta_sl_ok,
        )

        # Mirror to slave accounts (non-fatal)
        try:
            import backend.engine.mirror_engine as mirror_module

            if mirror_module.mirror_engine is not None:
                asyncio.create_task(
                    mirror_module.mirror_engine.mirror_trade_entry(
                        master_trade_id=int(trade.id),
                        call_product_id=int(payload.call_product_id),
                        put_product_id=int(payload.put_product_id),
                        master_call_qty=int(payload.quantity),
                        master_put_qty=int(payload.quantity),
                        master_call_strike=float(payload.call_strike),
                        master_put_strike=float(payload.put_strike),
                        master_call_symbol=str(payload.call_symbol),
                        master_put_symbol=str(payload.put_symbol),
                        master_call_fill=float(call_fill_price),
                        master_put_fill=float(put_fill_price),
                        expiry_date=trade.expiry_date,
                        underlying=str(underlying),
                        master_bracket_sl_call=(
                            float(call_sl_trigger_price)
                            if call_sl_trigger_price and call_sl_trigger_price > 0
                            else None
                        ),
                        master_bracket_sl_put=(
                            float(put_sl_trigger_price)
                            if put_sl_trigger_price and put_sl_trigger_price > 0
                            else None
                        ),
                    )
                )
                logger.info("Mirror task queued for trade %s", trade.id)
        except Exception as exc:
            logger.warning("Mirror queue failed (non-fatal): %s", exc)

        await ws_manager.broadcast(
            {
                "type": "TRADE_UPDATE",
                "trade_id": trade.id,
                "underlying": underlying,
                "status": TradeStatus.ACTIVE.value,
                "delta_sl_active": delta_sl_ok,
                "call_sl_trigger_price": getattr(call_leg, "sl_trigger_price", None),
                "put_sl_trigger_price": getattr(put_leg, "sl_trigger_price", None),
                **get_settling_info_for_trade(trade),
            }
        )

        total_prem = call_fill_price + put_fill_price
        return {
            "success": True,
            "trade_id": trade.id,
            "is_demo": bool(getattr(trade, "is_demo", False)),
            "message": (
                "[DEMO] Virtual strangle registered for monitoring — "
                "no real Delta orders placed"
                if bool(getattr(trade, "is_demo", False))
                else "Strangle placed on Delta Exchange and registered for monitoring"
            ),
            "call_filled_at": call_fill_price,
            "put_filled_at": put_fill_price,
            "call_order_id": call_order_id,
            "put_order_id": put_order_id,
            "total_premium": total_prem,
            "delta_sl_active": delta_sl_ok,
            "delta_sl_warning": delta_sl_warning,
            "call_sl_order_id": getattr(call_leg, "delta_sl_order_id", None),
            "put_sl_order_id": getattr(put_leg, "delta_sl_order_id", None),
            "call_sl_trigger_price": getattr(call_leg, "sl_trigger_price", None),
            "put_sl_trigger_price": getattr(put_leg, "sl_trigger_price", None),
            "universal_sl_pct": uni_sl,
            "monitoring_starts_at": (
                trade.monitoring_starts_at.isoformat()
                if trade.monitoring_starts_at
                else None
            ),
            "settling_period_minutes": SETTLING_PERIOD_AFTER_PLACE_MINUTES,
            "summary": {
                "underlying": underlying,
                "expiry_date": expiry.isoformat(),
                "expiry_label": get_dte_label(expiry),
                "call_strike": payload.call_strike,
                "put_strike": payload.put_strike,
                "quantity": payload.quantity,
                "total_premium_collected": total_prem * int(payload.quantity),
                "initial_max_profit": float(getattr(trade, "initial_max_profit", 0) or 0),
                "tp_pct": float(getattr(trade, "tp_pct", 50) or 50),
                "sl_pct": float(getattr(trade, "sl_pct", 100) or 100),
                "universal_sl_pct": uni_sl,
                "profit_target_usd": float(trade.profit_target_usd),
                "stoploss_usd": float(trade.stoploss_usd),
            },
        }
    except HTTPException:
        raise
    except DeltaAPIError as exc:
        logger.error("Delta API error during initiate: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@router.post("/register-existing")
async def register_existing_trade(
    payload: TradeRegisterExistingRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Emergency only: register an already-open Delta strangle (no order placement).
    """
    underlying, expiry = _validate_initiate_common(payload)
    if payload.call_entry_premium <= 0 or payload.put_entry_premium <= 0:
        raise HTTPException(status_code=400, detail="Entry premiums must be > 0")

    account = _get_active_account(db)
    await _ensure_no_active_trade(db, account, underlying)

    from backend.config import ENTRY_SETTLING_SECONDS
    from backend.database import get_or_create_auto_settings

    _entry_secs = int(ENTRY_SETTLING_SECONDS)
    try:
        _ats = get_or_create_auto_settings(db)
        _raw = getattr(_ats, "entry_settling_seconds", None)
        if _raw is not None:
            _entry_secs = int(_raw)
    except Exception:
        pass
    _entry_secs = max(0, min(300, _entry_secs))
    monitoring_starts = settling_ends_at_after_place(
        get_ist_now(), seconds=_entry_secs
    )
    trade, call_leg, put_leg = await _persist_strangle_trade(
        db=db,
        account=account,
        payload=payload,
        underlying=underlying,
        expiry=expiry,
        call_fill_price=float(payload.call_entry_premium),
        put_fill_price=float(payload.put_entry_premium),
        call_order_id=None,
        put_order_id=None,
        monitoring_starts=monitoring_starts,
    )

    # Emergency register: positions already open — cannot attach bracket SL
    # to existing shorts. Store display trigger prices only (no separate
    # stop_loss_order placement — those orphan after close).
    from backend.core.delta_sl import compute_bracket_sl

    uni_sl = float(getattr(trade, "universal_sl_pct", None) or 200.0)
    call_sl, _ = compute_bracket_sl(
        float(payload.call_entry_premium),
        uni_sl,
        leg="call",
        trade_id=int(trade.id),
    )
    put_sl, _ = compute_bracket_sl(
        float(payload.put_entry_premium),
        uni_sl,
        leg="put",
        trade_id=int(trade.id),
    )
    call_leg.sl_trigger_price = call_sl if call_sl > 0 else None
    put_leg.sl_trigger_price = put_sl if put_sl > 0 else None
    call_leg.delta_sl_order_id = None
    put_leg.delta_sl_order_id = None
    try:
        db.add(call_leg)
        db.add(put_leg)
        db.commit()
        db.refresh(call_leg)
        db.refresh(put_leg)
    except Exception as exc:
        logger.warning("Could not persist SL display prices (register): %s", exc)
        db.rollback()

    delta_sl_ok = bool(call_sl > 0 and put_sl > 0)
    delta_sl_warning = (
        "Registered without bracket SL — existing shorts cannot attach "
        "bracket on entry. Place SL manually on Delta if needed."
        if delta_sl_ok
        else None
    )

    db.expunge(trade)
    db.expunge(call_leg)
    db.expunge(put_leg)
    bot_engine.position_tracker.add(trade, call_leg, put_leg)
    logger.info(
        "Emergency register trade id=%s (no entry orders) — monitoring at %s "
        "bracket_display_sl=%s",
        trade.id,
        monitoring_starts.isoformat(),
        delta_sl_ok,
    )

    await ws_manager.broadcast(
        {
            "type": "TRADE_UPDATE",
            "trade_id": trade.id,
            "underlying": underlying,
            "status": TradeStatus.ACTIVE.value,
            "delta_sl_active": delta_sl_ok,
            "call_sl_trigger_price": getattr(call_leg, "sl_trigger_price", None),
            "put_sl_trigger_price": getattr(put_leg, "sl_trigger_price", None),
            "universal_sl_pct": uni_sl,
            **get_settling_info_for_trade(trade),
        }
    )

    return {
        "success": True,
        "trade_id": trade.id,
        "message": "Existing strangle registered for monitoring (no entry orders placed)",
        "call_filled_at": float(payload.call_entry_premium),
        "put_filled_at": float(payload.put_entry_premium),
        "monitoring_starts_at": (
            trade.monitoring_starts_at.isoformat()
            if trade.monitoring_starts_at
            else None
        ),
        "delta_sl_active": delta_sl_ok,
        "delta_sl_warning": delta_sl_warning,
        "call_sl_trigger_price": getattr(call_leg, "sl_trigger_price", None),
        "put_sl_trigger_price": getattr(put_leg, "sl_trigger_price", None),
        "universal_sl_pct": uni_sl,
    }


def _sync_tracker_from_db(db: Session) -> None:
    """Ensure ACTIVE DB trades with ≥1 open bot-managed leg are in the tracker."""
    heal_zombie_active_trades(db, bot_engine.position_tracker)
    active_states = bot_engine.position_tracker.get_all_active()

    # Drop stale tracker rows (closed / flat) before syncing
    for state in list(active_states):
        row = db.query(Trade).filter(Trade.id == state.trade_id).first()
        open_n = count_open_bot_legs(db, state.trade_id)
        if (
            row is None
            or str(row.status).lower() != TradeStatus.ACTIVE.value
            or open_n == 0
        ):
            bot_engine.position_tracker.mark_closed(state.trade_id)
            logger.info(
                "Sync removed flat/closed trade %s from tracker",
                state.trade_id,
            )

    active_states = bot_engine.position_tracker.get_all_active()
    tracked_ids = {s.trade_id for s in active_states}

    active_trades = (
        db.query(Trade).filter(Trade.status == TradeStatus.ACTIVE.value).all()
    )
    # Refresh mutable trade fields (cooldown / realized) on already-tracked rows
    for state in active_states:
        row = next((t for t in active_trades if t.id == state.trade_id), None)
        if row is None:
            bot_engine.position_tracker.mark_closed(state.trade_id)
            continue
        state.trade.monitoring_starts_at = row.monitoring_starts_at
        state.trade.adjust_settling_until = getattr(
            row, "adjust_settling_until", None
        )
        state.trade.realized_pnl = float(row.realized_pnl or 0.0)
        state.trade.status = row.status
        if hasattr(row, "is_demo"):
            state.trade.is_demo = bool(row.is_demo)

    for trade in active_trades:
        if trade.id in tracked_ids:
            continue
        if count_open_bot_legs(db, trade.id) == 0:
            continue
        legs = (
            db.query(Leg)
            .filter(
                Leg.trade_id == trade.id,
                Leg.is_bot_managed.is_(True),
            )
            .all()
        )
        open_legs = [leg for leg in legs if str(leg.status).lower() == "open"]
        if not open_legs:
            continue
        call_leg, put_leg = pick_call_put_legs(legs)
        if call_leg is None or put_leg is None:
            continue
        db.expunge(trade)
        db.expunge(call_leg)
        db.expunge(put_leg)
        bot_engine.position_tracker.add(trade, call_leg, put_leg)
        logger.info(
            "Synced trade id=%s from DB into position tracker (was missing)",
            trade.id,
        )

    if bot_engine.position_tracker.get_all_active():
        return

    db_count = (
        db.query(Trade)
        .filter(Trade.status == TradeStatus.ACTIVE.value)
        .count()
    )
    # Only reload if DB still has baskets with open legs
    open_active = 0
    for t in (
        db.query(Trade).filter(Trade.status == TradeStatus.ACTIVE.value).all()
    ):
        if count_open_bot_legs(db, t.id) > 0:
            open_active += 1
    if open_active == 0:
        return
    logger.warning(
        "Position tracker empty but %s active trade(s) in DB — reloading tracker",
        db_count,
    )
    count = bot_engine.position_tracker.load_from_db(db)
    logger.info("Reloaded %s trades into tracker", count)


@router.get("/active")
async def get_active_trades(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return active trades with live premiums / Delta MTM / bot plan fields."""
    bot_engine._refresh_delta_client()
    client = bot_engine.delta_client
    if client is not None:
        try:
            recon = await reconcile_open_legs_with_delta(
                db=db,
                client=client,
                position_tracker=bot_engine.position_tracker,
            )
            for tid in recon.get("fully_closed") or []:
                await ws_manager.broadcast(
                    {
                        "type": "TRADE_CLOSED",
                        "trade_id": tid,
                        "reason": ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value,
                        "message": "Basket closed — no open size on Delta",
                    }
                )
            for alert in recon.get("naked_risk") or []:
                tid = int(alert["trade_id"])
                state = bot_engine.position_tracker.get(tid)
                if state is not None:
                    await bot_engine._emergency_close_remaining_leg(
                        state, str(alert["remaining"])
                    )
        except Exception as exc:
            logger.warning("Active-trades reconcile failed: %s", exc)

    _sync_tracker_from_db(db)
    trades_out: list[dict[str, Any]] = []
    auto_settings = get_or_create_auto_settings(db)
    conversion_equality_pct = float(
        getattr(auto_settings, "conversion_equality_pct", 10.0) or 10.0
    )
    conversion_min_premium = float(
        getattr(auto_settings, "adj_low_premium_min_usd", 150.0) or 150.0
    )
    conversion_enabled = bool(
        getattr(auto_settings, "adj_low_premium_exit_enabled", False)
    ) and bool(getattr(auto_settings, "conversion_mode_enabled", True))
    max_adjustments_per_basket = getattr(
        auto_settings, "max_adjustments_per_basket", None
    )
    if max_adjustments_per_basket is not None:
        try:
            max_adjustments_per_basket = int(max_adjustments_per_basket)
        except (TypeError, ValueError):
            max_adjustments_per_basket = None
    conversion_mode_enabled = bool(
        getattr(auto_settings, "conversion_mode_enabled", True)
    )

    try:
        await bot_engine._refresh_btc_spot()
    except Exception:
        pass

    for state in list(bot_engine.position_tracker.get_all_active()):
        call_open = str(state.call_leg.status).lower() == "open"
        put_open = str(state.put_leg.status).lower() == "open"
        # Safety: both shorts flat → close orphan wings then drop from tracker
        if not call_open and not put_open:
            try:
                await bot_engine.maybe_close_orphaned_wings(
                    trade_id=int(state.trade_id),
                    db=db,
                    reason=ExitReason.WINGS_ORPHANED.value,
                )
            except Exception as orphan_exc:
                logger.warning(
                    "orphan wing hook on active-list failed trade=%s: %s",
                    state.trade_id,
                    orphan_exc,
                )
            # Re-check: if wings still open, keep showing basket
            wing_open = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == state.trade_id,
                    Leg.status == "open",
                    Leg.leg_type.in_(("wing_call", "wing_put")),
                )
                .count()
            )
            if wing_open > 0:
                continue
            bot_engine.position_tracker.mark_closed(state.trade_id)
            await ws_manager.broadcast(
                {
                    "type": "TRADE_CLOSED",
                    "trade_id": state.trade_id,
                    "reason": ExitReason.MANUAL_LEG_CLOSE.value,
                    "message": "Basket closed — no open short legs",
                }
            )
            continue
        if str(getattr(state.trade, "status", "")).lower() != TradeStatus.ACTIVE.value:
            bot_engine.position_tracker.mark_closed(state.trade_id)
            continue

        call_prem = float(state.last_call_premium or state.call_leg.initial_premium)
        put_prem = float(state.last_put_premium or state.put_leg.initial_premium)
        calculated = float(state.last_pnl)
        delta_mtm = float(state.last_delta_mtm)
        call_mtm = 0.0
        put_mtm = 0.0
        wing_call_mtm = 0.0
        wing_put_mtm = 0.0
        wing_call_prem = 0.0
        wing_put_prem = 0.0

        from backend.core.basket_legs import basket_legs as _basket_legs

        bl = _basket_legs(state.trade, db)
        wing_call_leg = bl.get("wing_call")
        wing_put_leg = bl.get("wing_put")
        wing_call_open = (
            wing_call_leg is not None
            and str(getattr(wing_call_leg, "status", "")).lower() == "open"
        )
        wing_put_open = (
            wing_put_leg is not None
            and str(getattr(wing_put_leg, "status", "")).lower() == "open"
        )
        if wing_call_leg is not None:
            wing_call_prem = float(wing_call_leg.initial_premium or 0)
        if wing_put_leg is not None:
            wing_put_prem = float(wing_put_leg.initial_premium or 0)

        if client is not None:
            try:
                pids = []
                if call_open and int(state.call_leg.product_id) > 0:
                    pids.append(int(state.call_leg.product_id))
                if put_open and int(state.put_leg.product_id) > 0:
                    pids.append(int(state.put_leg.product_id))
                if wing_call_open and int(wing_call_leg.product_id) > 0:
                    pids.append(int(wing_call_leg.product_id))
                if wing_put_open and int(wing_put_leg.product_id) > 0:
                    pids.append(int(wing_put_leg.product_id))
                if pids:
                    upnl_data = await client.get_positions_upnl(pids)
                    call_pid = int(state.call_leg.product_id)
                    put_pid = int(state.put_leg.product_id)
                    call_row = upnl_data.get(call_pid) or {}
                    put_row = upnl_data.get(put_pid) or {}
                    if call_open and call_row:
                        call_mtm = float(call_row.get("upnl") or 0.0)
                        if float(call_row.get("best_offer") or 0) > 0:
                            call_prem = float(call_row["best_offer"])
                    elif call_open:
                        call_prem = float(
                            await client.get_short_exit_price(
                                str(state.call_leg.symbol)
                            )
                        )
                    else:
                        call_prem = float(
                            state.call_leg.exit_premium
                            or state.call_leg.initial_premium
                            or 0.0
                        )
                    if put_open and put_row:
                        put_mtm = float(put_row.get("upnl") or 0.0)
                        if float(put_row.get("best_offer") or 0) > 0:
                            put_prem = float(put_row["best_offer"])
                    elif put_open:
                        put_prem = float(
                            await client.get_short_exit_price(
                                str(state.put_leg.symbol)
                            )
                        )
                    else:
                        put_prem = float(
                            state.put_leg.exit_premium
                            or state.put_leg.initial_premium
                            or 0.0
                        )
                    if wing_call_open:
                        wc_pid = int(wing_call_leg.product_id)
                        wc_row = upnl_data.get(wc_pid) or {}
                        if wc_row:
                            wing_call_mtm = float(wc_row.get("upnl") or 0.0)
                            best = float(wc_row.get("best_bid") or wc_row.get("best_offer") or 0)
                            if best > 0:
                                wing_call_prem = best
                        else:
                            try:
                                wing_call_prem = float(
                                    await client.get_long_exit_price(
                                        str(wing_call_leg.symbol)
                                    )
                                    or wing_call_prem
                                )
                            except Exception:
                                pass
                    if wing_put_open:
                        wp_pid = int(wing_put_leg.product_id)
                        wp_row = upnl_data.get(wp_pid) or {}
                        if wp_row:
                            wing_put_mtm = float(wp_row.get("upnl") or 0.0)
                            best = float(wp_row.get("best_bid") or wp_row.get("best_offer") or 0)
                            if best > 0:
                                wing_put_prem = best
                        else:
                            try:
                                wing_put_prem = float(
                                    await client.get_long_exit_price(
                                        str(wing_put_leg.symbol)
                                    )
                                    or wing_put_prem
                                )
                            except Exception:
                                pass
                    delta_mtm = call_mtm + put_mtm + wing_call_mtm + wing_put_mtm
                    bot_engine.position_tracker.update_premiums(
                        state.trade_id, call_prem, put_prem, calculated
                    )
                    bot_engine.position_tracker.update_delta_mtm(
                        state.trade_id, delta_mtm
                    )
            except Exception as exc:
                logger.warning("Live UPL@offer refresh failed trade=%s: %s", state.trade_id, exc)

        realized = float(getattr(state.trade, "realized_pnl", None) or 0.0)
        target = float(state.trade.profit_target_usd or 0)
        display_total = realized + delta_mtm
        pnl_pct = (display_total / target * 100.0) if target else 0.0
        settling = get_settling_info_for_trade(state.trade)

        trigger_pct = bot_engine.strategy.get_current_trigger_pct(state.trade, db)
        call_trig_pct = bot_engine.strategy.get_trigger_for_leg(
            call_prem, state.trade, db
        )
        put_trig_pct = bot_engine.strategy.get_trigger_for_leg(
            put_prem, state.trade, db
        )
        premium_slabs = None
        if str(getattr(state.trade, "trigger_mode", "") or "").lower() == "premium":
            premium_slabs = bot_engine.strategy.get_slabs(state.trade.id, db)
        if client is not None and call_open and put_open:
            try:
                await bot_engine._estimate_replacements(state, call_prem, put_prem)
            except Exception:
                pass

        adj_count = (
            db.query(Adjustment).filter(Adjustment.trade_id == state.trade_id).count()
        )
        last_adj = (
            db.query(Adjustment)
            .filter(Adjustment.trade_id == state.trade_id)
            .order_by(Adjustment.timestamp.desc())
            .first()
        )
        last_adjustment = None
        if last_adj is not None:
            last_adjustment = {
                "timestamp": _to_ist_iso(last_adj.timestamp),
                "leg_type": last_adj.leg_type,
                "old_strike": last_adj.old_strike,
                "new_strike": last_adj.new_strike,
                "trigger_pct_reached": last_adj.trigger_pct_reached,
                "slab_used": last_adj.slab_used,
            }

        call_snap = _leg_snapshot(state.call_leg, call_prem)
        put_snap = _leg_snapshot(state.put_leg, put_prem)
        if call_open:
            call_snap["leg_pnl"] = round(call_mtm, 4)
        if put_open:
            put_snap["leg_pnl"] = round(put_mtm, 4)

        all_legs = (
            db.query(Leg)
            .filter(
                Leg.trade_id == state.trade_id,
                Leg.is_bot_managed.is_(True),
            )
            .all()
        )
        fee_fields = await _fee_fields_for_basket(
            client=client,
            db=db,
            trade=state.trade,
            all_legs=all_legs,
            open_call=state.call_leg if call_open else None,
            open_put=state.put_leg if put_open else None,
            call_offer=call_prem,
            put_offer=put_prem,
        )
        call_snap = _leg_snapshot(state.call_leg, call_prem)
        put_snap = _leg_snapshot(state.put_leg, put_prem)
        if call_open:
            call_snap["leg_pnl"] = round(call_mtm, 4)
            call_snap["est_exit_fee_usd"] = round(float(fee_fields["call_est_exit_fee"]), 6)
        else:
            call_snap["est_exit_fee_usd"] = 0.0
        if put_open:
            put_snap["leg_pnl"] = round(put_mtm, 4)
            put_snap["est_exit_fee_usd"] = round(float(fee_fields["put_est_exit_fee"]), 6)
        else:
            put_snap["est_exit_fee_usd"] = 0.0

        wing_call_snap = None
        wing_put_snap = None
        if wing_call_leg is not None:
            wing_call_snap = _leg_snapshot(wing_call_leg, wing_call_prem)
            if wing_call_open:
                wing_call_snap["leg_pnl"] = round(wing_call_mtm, 4)
        if wing_put_leg is not None:
            wing_put_snap = _leg_snapshot(wing_put_leg, wing_put_prem)
            if wing_put_open:
                wing_put_snap["leg_pnl"] = round(wing_put_mtm, 4)

        from backend.core.basket_legs import build_wing_credit_fields

        wing_fields = build_wing_credit_fields(
            trade=state.trade,
            short_call=state.call_leg,
            short_put=state.put_leg,
            wing_call=wing_call_leg,
            wing_put=wing_put_leg,
            call_premium=call_prem,
            put_premium=put_prem,
            wing_call_premium=wing_call_prem if wing_call_leg else None,
            wing_put_premium=wing_put_prem if wing_put_leg else None,
        )

        # Gross MTM = realized + Delta UPNL (premium-only; fees NOT included)
        gross_mtm = display_total
        fees_paid = float(fee_fields["fees_paid"])
        est_exit = float(fee_fields["est_exit_fees"])
        total_fees = float(fee_fields["total_expected_fees"])
        from backend.core.fees import (
            compute_net_mtm,
            estimate_expected_exit_spread_usd,
            get_entry_spread_for_sl,
        )

        expected_exit_spread = 0.0
        for leg in all_legs:
            if str(getattr(leg, "status", "") or "").lower() != "open":
                continue
            lt = str(leg.leg_type or "").lower()
            if lt == "call":
                offer = float(call_prem)
            elif lt == "put":
                offer = float(put_prem)
            elif lt == "wing_call":
                offer = float(wing_call_prem or getattr(leg, "initial_premium", 0) or 0)
            elif lt == "wing_put":
                offer = float(wing_put_prem or getattr(leg, "initial_premium", 0) or 0)
            else:
                offer = float(getattr(leg, "initial_premium", 0) or 0)
            if offer > 0:
                expected_exit_spread += estimate_expected_exit_spread_usd(
                    offer_price=offer,
                    quantity=int(leg.quantity or 0),
                )

        entry_spread_for_sl = get_entry_spread_for_sl(state.trade)
        gross_mtm_for_stoploss = float(gross_mtm) + entry_spread_for_sl

        slip_fields = compute_net_mtm(
            gross_mtm=gross_mtm,
            fees_paid=fees_paid,
            est_exit_fees=est_exit,
            slippage_pct=getattr(state.trade, "slippage_pct", None) or 2.0,
            expected_exit_spread_usd=expected_exit_spread,
        )
        net_mtm = float(slip_fields["net_mtm"])

        plan = bot_engine.build_bot_plan_fields(
            state,
            call_prem,
            put_prem,
            float(trigger_pct),
            call_trigger_pct=float(call_trig_pct),
            put_trigger_pct=float(put_trig_pct),
            premium_slabs=premium_slabs,
            net_mtm=net_mtm,
            gross_mtm_for_stoploss=gross_mtm_for_stoploss,
            conversion_min_premium=conversion_min_premium,
            conversion_equality_pct=conversion_equality_pct,
            conversion_enabled=conversion_enabled,
        )

        leg_history = _basket_leg_history(db, state.trade_id)
        open_count = sum(1 for x in (call_open, put_open) if x)

        row: dict[str, Any] = {
            "trade_id": state.trade_id,
            "basket_number": getattr(state.trade, "basket_number", None)
            or state.trade_id,
            "underlying": state.trade.underlying,
            "expiry_date": str(state.trade.expiry_date),
            "expiry_label": get_dte_label(state.trade.expiry_date),
            "status": state.trade.status,
            "is_demo": bool(getattr(state.trade, "is_demo", False)),
            "open_leg_count": open_count,
            "call_leg": call_snap,
            "put_leg": put_snap,
            "wing_call": wing_call_snap,
            "wing_put": wing_put_snap,
            "net_credit_entry": wing_fields["net_credit_entry"],
            "net_credit_now": wing_fields["net_credit_now"],
            "wing_premium_paid_usd": wing_fields["wing_premium_paid_usd"],
            "max_loss_usd": wing_fields["max_loss_usd"],
            "leg_history": leg_history,
            "call_premium": call_prem,
            "put_premium": put_prem,
            "wing_call_premium": wing_call_prem if wing_call_leg else None,
            "wing_put_premium": wing_put_prem if wing_put_leg else None,
            "call_offer": call_prem,
            "put_offer": put_prem,
            "call_change_pct": call_snap["change_pct"],
            "put_change_pct": put_snap["change_pct"],
            "calculated_pnl": round(float(calculated), 4),
            "delta_mtm_pnl": round(float(delta_mtm), 4),
            "delta_upnl": round(float(delta_mtm), 4),
            "call_delta_mtm": round(float(call_mtm), 4),
            "put_delta_mtm": round(float(put_mtm), 4),
            "wing_call_upnl": round(float(wing_call_mtm), 4),
            "wing_put_upnl": round(float(wing_put_mtm), 4),
            "call_upnl": round(float(call_mtm), 4),
            "put_upnl": round(float(put_mtm), 4),
            "realized_pnl": round(float(realized), 4),
            "unrealized_pnl": round(float(delta_mtm), 4),
            "total_pnl": display_total,
            "gross_mtm": gross_mtm,
            "gross_mtm_for_stoploss": round(gross_mtm_for_stoploss, 4),
            "entry_spread_for_sl": round(entry_spread_for_sl, 4),
            "expected_exit_spread_usd": round(expected_exit_spread, 4),
            "fees_paid": fees_paid,
            "est_exit_fees": est_exit,
            "total_expected_fees": total_fees,
            "underlying_price": (
                float(fee_fields.get("btc_index_for_fees") or 0)
                or float(getattr(bot_engine, "_btc_spot", 0) or 0)
                or None
            ),
            "call_entry_fee": fee_fields.get("call_entry_fee"),
            "put_entry_fee": fee_fields.get("put_entry_fee"),
            "call_est_exit_fee": fee_fields.get("call_est_exit_fee"),
            "put_est_exit_fee": fee_fields.get("put_est_exit_fee"),
            "profit_target_usd": target,
            "stoploss_usd": float(state.trade.stoploss_usd),
            "initial_max_profit": float(
                getattr(state.trade, "initial_max_profit", None) or 0
            )
            or None,
            "tp_pct": float(getattr(state.trade, "tp_pct", None) or 50.0),
            "sl_pct": float(getattr(state.trade, "sl_pct", None) or 100.0),
            "target_source": getattr(state.trade, "target_source", None),
            "hedge_theta_at_entry": (
                float(getattr(state.trade, "hedge_theta_at_entry", None))
                if getattr(state.trade, "hedge_theta_at_entry", None) is not None
                else None
            ),
            "pnl_pct_of_target": round(pnl_pct, 2),
            "hours_to_expiry": get_hours_to_expiry(state.trade.expiry_date),
            "adjustment_count": int(
                getattr(state.trade, "adjustment_count", None) or adj_count or 0
            ),
            "max_adjustments_per_basket": max_adjustments_per_basket,
            "adjustments_remaining": (
                max(
                    0,
                    int(max_adjustments_per_basket)
                    - int(
                        getattr(state.trade, "adjustment_count", None)
                        or adj_count
                        or 0
                    ),
                )
                if max_adjustments_per_basket is not None
                else None
            ),
            "conversion_mode_enabled": conversion_mode_enabled,
            "last_adjustment": last_adjustment,
            "is_settling": settling["is_settling"],
            "settling_ends_at": settling["settling_ends_at"],
            "settling_minutes_left": settling["settling_minutes_left"],
            "settling_source": settling.get("settling_source", "none"),
            "last_mtm_update": get_ist_now().strftime("%H:%M:%S IST"),
            "in_conversion_mode": bool(
                getattr(state.trade, "in_conversion_mode", False)
            ),
            "conversion_hedge_symbol": getattr(
                state.trade, "conversion_hedge_symbol", None
            ),
            "conversion_hedge_entry_price": float(
                getattr(state.trade, "conversion_hedge_entry_price", 0) or 0
            ),
            "conversion_triggered_leg": getattr(
                state.trade, "conversion_triggered_leg", None
            ),
            "conversion_equality_pct": conversion_equality_pct,
            **plan,
            # Always last — never overwritten by plan
            "slippage_pct": float(slip_fields["slippage_pct"]),
            "slippage_amount": float(slip_fields["slippage_amount"]),
            "total_deductions": float(slip_fields["total_deductions"]),
            "net_mtm": net_mtm,
        }
        logger.info(
            "/active trade=%s slippage_pct=%s slippage_amount=%s net_mtm=%s",
            state.trade_id,
            row["slippage_pct"],
            row["slippage_amount"],
            row["net_mtm"],
        )
        trades_out.append(row)
    return {"trades": trades_out}


@router.get("/history")
async def get_trade_history(
    db: Session = Depends(get_db),
    limit: int = 30,
) -> dict[str, Any]:
    """
    Closed + active baskets with full leg history for dashboard.
    Newest baskets first.
    """
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        return {"baskets": []}

    lim = max(1, min(int(limit or 30), 100))
    trades = (
        db.query(Trade)
        .filter(Trade.account_id == account.id)
        .order_by(Trade.id.desc())
        .limit(lim)
        .all()
    )
    client = None
    try:
        client = _build_delta_client(account)
    except Exception:
        client = None

    baskets: list[dict[str, Any]] = []
    try:
        for trade in trades:
            legs = _basket_leg_history(db, trade.id)
            adjs = (
                db.query(Adjustment)
                .filter(Adjustment.trade_id == trade.id)
                .order_by(Adjustment.timestamp.asc())
                .all()
            )
            adj_rows = [
                {
                    "timestamp": _to_ist_iso(a.timestamp),
                    "leg_type": a.leg_type,
                    "old_strike": a.old_strike,
                    "old_exit_premium": a.old_exit_premium,
                    "new_strike": a.new_strike,
                    "new_entry_premium": a.new_entry_premium,
                    "trigger_pct_reached": a.trigger_pct_reached,
                    "slab_used": a.slab_used,
                }
                for a in adjs
            ]
            closed_realized = sum(
                float(row["realized_pnl"] or 0.0)
                for row in legs
                if row["status"] == "closed"
            )
            orm_legs = (
                db.query(Leg)
                .filter(Leg.trade_id == trade.id, Leg.is_bot_managed.is_(True))
                .all()
            )
            if client is not None:
                try:
                    await _backfill_leg_fees(client, db, orm_legs)
                    legs = _basket_leg_history(db, trade.id)
                except Exception as exc:
                    logger.warning("History fee backfill trade=%s: %s", trade.id, exc)

            from backend.core.fees import basket_fees_paid_from_legs

            fees_paid = basket_fees_paid_from_legs(orm_legs)
            is_closed = str(trade.status).lower() != TradeStatus.ACTIVE.value
            gross = float(trade.realized_pnl or closed_realized or 0.0)
            baskets.append(
                {
                    "trade_id": trade.id,
                    "basket_number": trade.basket_number or trade.id,
                    "underlying": trade.underlying,
                    "expiry_date": str(trade.expiry_date),
                    "status": trade.status,
                    "entry_time": _to_ist_iso(trade.entry_time),
                    "exit_time": _to_ist_iso(trade.exit_time),
                    "exit_reason": trade.exit_reason,
                    "realized_pnl": gross,
                    "total_premium_collected": float(trade.total_premium_collected or 0.0),
                    "fees_paid": fees_paid,
                    "est_exit_fees": 0.0 if is_closed else None,
                    "total_expected_fees": fees_paid if is_closed else None,
                    "gross_mtm": gross,
                    "net_mtm": gross - fees_paid,
                    "legs": legs,
                    "adjustments": adj_rows,
                }
            )
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    return {"baskets": baskets}


@router.get("/{trade_id}")
async def get_trade(trade_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Full trade detail from DB plus live tracker premiums when available."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    legs = db.query(Leg).filter(Leg.trade_id == trade_id).all()
    adj_count = db.query(Adjustment).filter(Adjustment.trade_id == trade_id).count()
    state = bot_engine.position_tracker.get(trade_id)

    return {
        "trade_id": trade.id,
        "underlying": trade.underlying,
        "expiry_date": str(trade.expiry_date),
        "status": trade.status,
        "profit_target_usd": trade.profit_target_usd,
        "stoploss_usd": trade.stoploss_usd,
        "trigger_mode": trade.trigger_mode,
        "total_premium_collected": trade.total_premium_collected,
        "realized_pnl": trade.realized_pnl,
        "exit_reason": trade.exit_reason,
        "entry_time": _to_ist_iso(trade.entry_time),
        "exit_time": _to_ist_iso(trade.exit_time) if trade.exit_time else None,
        "legs": [
            {
                "id": leg.id,
                "leg_type": leg.leg_type,
                "strike": leg.strike,
                "symbol": leg.symbol,
                "product_id": leg.product_id,
                "initial_premium": leg.initial_premium,
                "quantity": leg.quantity,
                "status": leg.status,
                "delta_order_id": leg.delta_order_id,
                "is_bot_managed": leg.is_bot_managed,
            }
            for leg in legs
        ],
        "live": {
            "call_premium": state.last_call_premium if state else None,
            "put_premium": state.last_put_premium if state else None,
            "calculated_pnl": state.last_pnl if state else None,
            "delta_mtm_pnl": state.last_delta_mtm if state else None,
        },
        "adjustment_count": adj_count,
    }


@router.get("/{trade_id}/adjustments")
async def get_trade_adjustments(
    trade_id: int, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    rows = (
        db.query(Adjustment)
        .filter(Adjustment.trade_id == trade_id)
        .order_by(Adjustment.timestamp.desc())
        .all()
    )
    return [
        {
            "timestamp": _to_ist_iso(row.timestamp),
            "leg_type": row.leg_type,
            "old_strike": row.old_strike,
            "new_strike": row.new_strike,
            "trigger_pct_reached": row.trigger_pct_reached,
            "slab_used": row.slab_used,
            "old_exit_premium": row.old_exit_premium,
            "new_entry_premium": row.new_entry_premium,
            "time_remaining_hours": row.time_remaining_hours,
        }
        for row in rows
    ]


@router.post("/{trade_id}/exit")
async def exit_trade(
    trade_id: int,
    payload: TradeExitRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Emergency / manual exit — close both bot-managed legs."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    if trade.status != TradeStatus.ACTIVE.value:
        raise HTTPException(status_code=400, detail="Trade is not active")

    state = bot_engine.position_tracker.get(trade_id)
    reason = payload.reason or ExitReason.MANUAL_EMERGENCY.value

    if state is not None:
        bot_engine._refresh_delta_client()
        if bot_engine.delta_client is None:
            raise HTTPException(status_code=502, detail="Delta client unavailable")
        await bot_engine._exit_trade(state, reason)
        refreshed = db.query(Trade).filter(Trade.id == trade_id).first()
        call_leg = (
            db.query(Leg)
            .filter(Leg.trade_id == trade_id, Leg.leg_type == "call")
            .order_by(Leg.id.desc())
            .first()
        )
        put_leg = (
            db.query(Leg)
            .filter(Leg.trade_id == trade_id, Leg.leg_type == "put")
            .order_by(Leg.id.desc())
            .first()
        )
        return {
            "success": refreshed is not None
            and refreshed.status != TradeStatus.ACTIVE.value,
            "final_pnl": refreshed.realized_pnl if refreshed else None,
            "call_closed_at": call_leg.exit_premium if call_leg else None,
            "put_closed_at": put_leg.exit_premium if put_leg else None,
        }

    # Fallback if not in tracker — close open bot-managed legs directly
    account = _get_active_account(db)
    client = _build_delta_client(account)
    try:
        call_leg = (
            db.query(Leg)
            .filter(
                Leg.trade_id == trade_id,
                Leg.leg_type == "call",
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .first()
        )
        put_leg = (
            db.query(Leg)
            .filter(
                Leg.trade_id == trade_id,
                Leg.leg_type == "put",
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .first()
        )

        # Resolve hedge early — needed for hoisted mirror above leftovers branch
        hedge_leg = (
            db.query(Leg)
            .filter(
                Leg.trade_id == trade_id,
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
                Leg.is_long.is_(True),
            )
            .first()
        )
        hedge_pid = (
            int(hedge_leg.product_id)
            if hedge_leg is not None
            else getattr(trade, "conversion_hedge_product_id", None)
        )

        # HOISTED: every fallback return path mirrors slaves first
        # (leftovers-only used to return before the old mirror_exit call).
        try:
            import backend.engine.mirror_engine as mirror_module

            me = mirror_module.mirror_engine
            if me is not None:
                await me.mirror_exit(
                    master_trade_id=int(trade_id),
                    call_product_id=int(
                        (call_leg.product_id if call_leg else 0) or 0
                    ),
                    put_product_id=int(
                        (put_leg.product_id if put_leg else 0) or 0
                    ),
                    reason=reason,
                    hedge_product_id=(
                        int(hedge_pid) if hedge_pid else None
                    ),
                )
                logger.info(
                    "[MIRROR_EXIT] Awaited complete for trade %s "
                    "(fallback path, hoisted)",
                    trade_id,
                )
        except Exception as exc:
            logger.warning(
                "[MIRROR_EXIT] Failed trade %s (fallback hoist): %s",
                trade_id,
                exc,
                exc_info=True,
            )

        # Allow one-legged emergency exit (may still have open hedge/wings)
        if call_leg is None and put_leg is None:
            leftovers = (
                db.query(Leg)
                .filter(Leg.trade_id == trade_id, Leg.status == "open")
                .all()
            )
            from backend.engine.wing_exit import close_basket_legs

            if leftovers:
                bundle = await close_basket_legs(
                    trade=trade,
                    reason=reason,
                    db=db,
                    delta_client=client,
                    order_executor=bot_engine.order_executor,
                    legs_to_close="all",
                    legs=leftovers,
                    verify_on_delta=True,
                )
                for row in bundle.legs:
                    if row.leg_id is None:
                        continue
                    leftover = (
                        db.query(Leg).filter(Leg.id == int(row.leg_id)).first()
                    )
                    if leftover is None or str(leftover.status).lower() != "open":
                        continue
                    if row.is_wing and not row.success:
                        note = f"WING_CLOSE_FAILED: {row.leg_type}:{row.error}"
                        prior = str(trade.notes or "")
                        if "WING_CLOSE_FAILED" not in prior:
                            trade.notes = (
                                f"{prior} | {note}" if prior else note
                            )
                        continue
                    book_leg_close(
                        leg=leftover,
                        trade=trade,
                        exit_premium=row.fill_price,
                        exit_time=get_utc_now(),
                        exit_fee_usd=row.commission,
                        exit_order_id=row.order_id,
                        db=db,
                    )
                if bundle.any_wing_fail:
                    await ws_manager.broadcast(
                        {
                            "type": "WING_CLOSE_FAILED",
                            "trade_id": trade_id,
                            "reason": reason,
                            "message": (
                                f"Wing close failed on emergency leftovers "
                                f"trade={trade_id}"
                            ),
                        }
                    )
            recompute_trade_realized_pnl(db, trade)
            db.commit()

            # Funnel owns Trade.status / mark_closed (slaves already mirrored)
            funnel = await bot_engine.close_master_trade(
                trade_id=int(trade_id),
                reason=reason,
                db=db,
                skip_master_legs=True,
                trade_state=None,
            )
            slaves_closed = int(funnel.get("slaves_closed") or 0)
            slaves_failed = int(funnel.get("slaves_failed") or 0)
            if slaves_failed > 0:
                logger.critical(
                    "[EXIT_FUNNEL] leftovers-only emergency trade=%s "
                    "slaves_failed=%s slaves_closed=%s",
                    trade_id,
                    slaves_failed,
                    slaves_closed,
                )
            refreshed = db.query(Trade).filter(Trade.id == trade_id).first()
            return {
                "success": True,
                "final_pnl": (
                    refreshed.realized_pnl if refreshed else trade.realized_pnl
                ),
                "call_closed_at": None,
                "put_closed_at": None,
                "message": "No open short legs — leftovers closed",
                "slaves_closed": slaves_closed,
                "slaves_failed": slaves_failed,
            }

        # Full emergency with shorts present — route through funnel
        # (shorts→wings order via close_basket_legs inside close_master_trade)
        state = bot_engine.position_tracker.get(trade_id)
        if state is None and (call_leg is not None or put_leg is not None):
            # Build minimal tracker state if missing
            if call_leg is not None and put_leg is not None:
                bot_engine.position_tracker.add(trade, call_leg, put_leg)
                state = bot_engine.position_tracker.get(trade_id)

        funnel = await bot_engine.close_master_trade(
            trade_id=int(trade_id),
            reason=reason,
            db=db,
            skip_master_legs=False,
            trade_state=state,
        )
        slaves_closed = int(funnel.get("slaves_closed") or 0)
        slaves_failed = int(funnel.get("slaves_failed") or 0)
        db.refresh(trade)
        return {
            "success": True,
            "final_pnl": float(trade.realized_pnl or 0.0),
            "call_closed_at": None,
            "put_closed_at": None,
            "message": "Emergency exit via close_master_trade funnel",
            "slaves_closed": slaves_closed,
            "slaves_failed": slaves_failed,
            "master_legs_closed": int(funnel.get("master_legs_closed") or 0),
        }

    finally:
        await client.close()


@router.post("/{trade_id}/leg/{leg_type}/close")
async def close_single_leg(
    trade_id: int,
    leg_type: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    UI "Close Call" / "Close Put" — closes ONLY that short leg.

    Wings stay open while the other short is still open. When both shorts
    are closed, ``maybe_close_orphaned_wings`` auto-closes wings
    (reason WINGS_ORPHANED). Emergency full exit uses POST /exit instead.
    """
    from backend.core.bot_logger import log_and_buffer
    from backend.engine.wing_exit import close_basket_legs
    from backend.engine.trade_reconcile import (
        book_leg_close,
        recompute_trade_realized_pnl,
    )

    leg_key = leg_type.lower().strip()
    if leg_key not in {"call", "put"}:
        raise HTTPException(status_code=400, detail="leg_type must be call or put")

    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    status = str(trade.status or "").lower()
    if status != TradeStatus.ACTIVE.value:
        msg = "This basket was already closed"
        return {
            "success": True,
            "already_closed": True,
            "message": msg,
            "clicked_leg": leg_key,
            "basket_closed": True,
            "exit_reason": getattr(trade, "exit_reason", None),
            "final_pnl": float(trade.realized_pnl or 0.0),
        }

    target = (
        db.query(Leg)
        .filter(
            Leg.trade_id == trade_id,
            Leg.leg_type == leg_key,
            Leg.status == "open",
            Leg.is_bot_managed.is_(True),
            Leg.is_long.is_(False),
        )
        .order_by(Leg.id.desc())
        .first()
    )
    if target is None:
        # Already closed — still run orphan wing hook in case other short gone
        orphan = await bot_engine.maybe_close_orphaned_wings(
            trade_id=int(trade_id),
            db=db,
            reason=ExitReason.WINGS_ORPHANED.value,
        )
        db.refresh(trade)
        return {
            "success": True,
            "already_closed": True,
            "message": f"{leg_key} already closed",
            "clicked_leg": leg_key,
            "basket_closed": str(trade.status).lower() != TradeStatus.ACTIVE.value,
            "orphan_wings": orphan,
            "exit_reason": getattr(trade, "exit_reason", None),
            "final_pnl": float(trade.realized_pnl or 0.0),
        }

    bot_engine._refresh_delta_client()
    client = bot_engine.delta_client
    if client is None:
        raise HTTPException(status_code=502, detail="Delta client unavailable")

    logger.info(
        "[LEG_CLOSE] trade=%s clicked_leg=%s — closing single short only "
        "(wings stay if other short open)",
        trade_id,
        leg_key,
    )

    bundle = await close_basket_legs(
        trade=trade,
        reason=ExitReason.MANUAL_LEG_CLOSE.value,
        db=db,
        delta_client=client,
        order_executor=bot_engine.order_executor,
        legs_to_close="shorts_only",
        legs=[target],
        verify_on_delta=True,
    )
    row = bundle.legs[0] if bundle.legs else None
    if row is None or not row.success:
        err = row.error if row else "no_result"
        log_and_buffer(
            "LEG_CLOSE_RESULT",
            int(trade_id),
            {"clicked_leg": leg_key, "ok": False, "message": str(err)},
        )
        raise HTTPException(
            status_code=502,
            detail=f"Failed to close {leg_key}: {err}",
        )

    book_leg_close(
        leg=target,
        trade=trade,
        exit_premium=row.fill_price,
        exit_time=get_utc_now(),
        exit_fee_usd=row.commission,
        exit_order_id=row.order_id,
        db=db,
    )
    recompute_trade_realized_pnl(db, trade)
    db.commit()

    # Refresh tracker short leg status
    state = bot_engine.position_tracker.get(trade_id)
    if state is not None:
        if leg_key == "call":
            state.call_leg.status = "closed"
            if row.fill_price is not None:
                state.call_leg.exit_premium = row.fill_price
        else:
            state.put_leg.status = "closed"
            if row.fill_price is not None:
                state.put_leg.exit_premium = row.fill_price

    orphan = await bot_engine.maybe_close_orphaned_wings(
        trade_id=int(trade_id),
        db=db,
        reason=ExitReason.WINGS_ORPHANED.value,
    )

    db.refresh(trade)
    other_key = "put" if leg_key == "call" else "call"
    other_open = (
        db.query(Leg)
        .filter(
            Leg.trade_id == trade_id,
            Leg.leg_type == other_key,
            Leg.status == "open",
            Leg.is_bot_managed.is_(True),
            Leg.is_long.is_(False),
        )
        .count()
    )
    basket_closed = str(trade.status or "").lower() != TradeStatus.ACTIVE.value

    # If both shorts closed but trade still active (no wings / wings failed),
    # close trade via funnel with MANUAL_LEG_CLOSE
    if other_open == 0 and not basket_closed:
        open_wings = (
            db.query(Leg)
            .filter(
                Leg.trade_id == trade_id,
                Leg.status == "open",
                Leg.leg_type.in_(("wing_call", "wing_put")),
            )
            .count()
        )
        if open_wings == 0:
            funnel = await bot_engine.close_master_trade(
                trade_id=int(trade_id),
                reason=ExitReason.MANUAL_LEG_CLOSE.value,
                db=db,
                skip_master_legs=True,
                trade_state=bot_engine.position_tracker.get(trade_id),
            )
            db.refresh(trade)
            basket_closed = True
            logger.info(
                "[LEG_CLOSE] trade=%s both shorts flat, no wings — "
                "funnel closed (skip_master_legs) slaves=%s",
                trade_id,
                funnel.get("slaves_closed"),
            )

    msg = (
        f"Closed {leg_key}."
        + (
            " Other short still open — wings kept."
            if other_open > 0
            else (
                f" Both shorts closed; wings handled "
                f"(orphan={orphan.get('closed')})."
            )
        )
    )
    log_and_buffer(
        "LEG_CLOSE_RESULT",
        int(trade_id),
        {
            "clicked_leg": leg_key,
            "ok": True,
            "message": msg,
            "basket_closed": basket_closed,
            "orphan_wings": orphan,
        },
    )
    await ws_manager.broadcast(
        {
            "type": "LEG_CLOSED" if not basket_closed else "TRADE_CLOSED",
            "trade_id": trade_id,
            "clicked_leg": leg_key,
            "reason": (
                getattr(trade, "exit_reason", None)
                or ExitReason.MANUAL_LEG_CLOSE.value
            ),
            "basket_closed": basket_closed,
            "message": msg,
            "final_pnl": float(trade.realized_pnl or 0.0),
        }
    )
    return {
        "success": True,
        "message": msg,
        "clicked_leg": leg_key,
        "basket_closed": basket_closed,
        "orphan_wings": orphan,
        "exit_reason": getattr(trade, "exit_reason", None),
        "final_pnl": float(trade.realized_pnl or 0.0),
        "basket_number": trade.basket_number,
    }


@router.patch("/{trade_id}/settings")
async def update_trade_settings(
    trade_id: int,
    payload: TradeSettingsUpdate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Partial settings update — takes effect next monitoring cycle via get_slabs()."""
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if trade is None:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    updated: dict[str, Any] = {}
    state = bot_engine.position_tracker.get(trade_id)

    def _sync_tracker_money() -> None:
        if state is None:
            return
        state.trade.profit_target_usd = trade.profit_target_usd
        state.trade.stoploss_usd = trade.stoploss_usd
        if hasattr(state.trade, "tp_pct"):
            state.trade.tp_pct = trade.tp_pct
        if hasattr(state.trade, "sl_pct"):
            state.trade.sl_pct = trade.sl_pct
        if hasattr(state.trade, "initial_max_profit"):
            state.trade.initial_max_profit = trade.initial_max_profit
        if hasattr(state.trade, "slippage_pct"):
            state.trade.slippage_pct = trade.slippage_pct

    if "tp_pct" in updates and updates["tp_pct"] is not None:
        # TP/SL locked to initial deployment premium
        # initial_max_profit never changes after trade entry
        # adjustments do NOT affect TP/SL — mid-trade % edits still use locked base
        tp = float(updates["tp_pct"])
        if tp <= 0:
            raise HTTPException(status_code=400, detail="tp_pct must be > 0")
        max_p = float(getattr(trade, "initial_max_profit", None) or 0)
        if max_p <= 0:
            raise HTTPException(
                status_code=400,
                detail="initial_max_profit missing — cannot derive target from %",
            )
        trade.tp_pct = tp
        trade.profit_target_usd = round(max_p * tp / 100.0, 2)
        updated["tp_pct"] = trade.tp_pct
        updated["profit_target_usd"] = trade.profit_target_usd
        _sync_tracker_money()

    if "sl_pct" in updates and updates["sl_pct"] is not None:
        # Derive SL $ from locked initial_max_profit — never from live premiums
        sl = float(updates["sl_pct"])
        if sl <= 0:
            raise HTTPException(status_code=400, detail="sl_pct must be > 0")
        max_p = float(getattr(trade, "initial_max_profit", None) or 0)
        if max_p <= 0:
            raise HTTPException(
                status_code=400,
                detail="initial_max_profit missing — cannot derive stop from %",
            )
        trade.sl_pct = sl
        trade.stoploss_usd = round(max_p * sl / 100.0, 2)
        updated["sl_pct"] = trade.sl_pct
        updated["stoploss_usd"] = trade.stoploss_usd
        _sync_tracker_money()

    if "profit_target_usd" in updates and updates["profit_target_usd"] is not None:
        # Legacy path — only if tp_pct not also sent
        if "tp_pct" not in updates:
            trade.profit_target_usd = float(updates["profit_target_usd"])
            updated["profit_target_usd"] = trade.profit_target_usd
            _sync_tracker_money()

    if "stoploss_usd" in updates and updates["stoploss_usd"] is not None:
        if "sl_pct" not in updates:
            trade.stoploss_usd = float(updates["stoploss_usd"])
            updated["stoploss_usd"] = trade.stoploss_usd
            _sync_tracker_money()

    if "slippage_pct" in updates and updates["slippage_pct"] is not None:
        sp = float(updates["slippage_pct"])
        if sp < 0 or sp > 10:
            raise HTTPException(
                status_code=400, detail="slippage_pct must be between 0 and 10"
            )
        trade.slippage_pct = sp
        updated["slippage_pct"] = trade.slippage_pct
        _sync_tracker_money()

    if "universal_sl_pct" in updates and updates["universal_sl_pct"] is not None:
        usp = float(updates["universal_sl_pct"])
        if usp < 100 or usp > 1000:
            raise HTTPException(
                status_code=400, detail="universal_sl_pct must be between 100 and 1000"
            )
        trade.universal_sl_pct = usp
        updated["universal_sl_pct"] = trade.universal_sl_pct
        if state is not None and hasattr(state.trade, "universal_sl_pct"):
            state.trade.universal_sl_pct = trade.universal_sl_pct
        # Note: live Delta SL orders are not auto-moved on PATCH —
        # next adjustment / re-deploy refreshes them.

    for key in (
        "slab_24h",
        "slab_12h",
        "slab_6h",
        "slab_lt6h",
        "flat_trigger_pct",
        "premium_slab_300",
        "premium_slab_200",
        "premium_slab_100",
        "premium_slab_lt100",
    ):
        if key in updates and updates[key] is not None:
            _upsert_setting(db, trade_id, key, updates[key])
            updated[key] = updates[key]

    if "trigger_mode" in updates and updates["trigger_mode"] is not None:
        mode = str(updates["trigger_mode"]).lower()
        if mode not in {
            TriggerMode.FLAT.value,
            TriggerMode.SLAB.value,
            TriggerMode.PREMIUM.value,
        }:
            raise HTTPException(
                status_code=400,
                detail="trigger_mode must be 'flat', 'slab', or 'premium'",
            )
        trade.trigger_mode = mode
        _upsert_setting(db, trade_id, "trigger_mode", trade.trigger_mode)
        updated["trigger_mode"] = trade.trigger_mode
        if state is not None and hasattr(state.trade, "trigger_mode"):
            state.trade.trigger_mode = trade.trigger_mode

    if "combined_trigger_mode" in updates and updates["combined_trigger_mode"] is not None:
        trade.combined_trigger_mode = bool(updates["combined_trigger_mode"])
        updated["combined_trigger_mode"] = trade.combined_trigger_mode
        if state is not None and hasattr(state.trade, "combined_trigger_mode"):
            state.trade.combined_trigger_mode = trade.combined_trigger_mode

    db.commit()
    return {
        "success": True,
        "updated": {
            **updated,
            "initial_max_profit": float(
                getattr(trade, "initial_max_profit", None) or 0
            )
            or None,
            "tp_pct": float(getattr(trade, "tp_pct", None) or 50.0),
            "sl_pct": float(getattr(trade, "sl_pct", None) or 100.0),
            "slippage_pct": float(getattr(trade, "slippage_pct", None) or 2.0),
            "profit_target_usd": float(trade.profit_target_usd or 0),
            "stoploss_usd": float(trade.stoploss_usd or 0),
        },
    }
