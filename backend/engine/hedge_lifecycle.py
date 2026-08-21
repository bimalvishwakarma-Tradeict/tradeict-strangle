# hedge_lifecycle.py — Master hedge open/close with verify + real fills

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.bot_logger import log_and_buffer
from backend.core.chain_utils import annotate_atm
from backend.core.delta_client import DeltaAPIError, DeltaClient, _extract_live_quote
from backend.core.fees import estimate_option_trading_fee
from backend.core.spread_utils import estimate_and_log_exit_spread_usd
from backend.core.hedge_theta import (
    ExpiryNotAvailableError,
    HedgeThetaError,
    get_hedge_theta,
    migrate_hedge_expiry_mode,
    resolve_hedge_expiry_date,
)
from backend.core.time_utils import get_hours_to_expiry, get_ist_now, is_pre_expiry_window
from backend.engine.order_executor import OrderExecutor
from backend.models import Account, AutoTradeSettings, HedgePosition, HedgeThetaLog, Trade

logger = logging.getLogger(__name__)

CONTRACT_SIZE = float(OPTIONS_CONTRACT_VALUE)
VERIFY_PAUSE_SECONDS = 0.5
UNWIND_VERIFY_ATTEMPTS = 3
# Require 15% headroom above ask-based premium cost before buying either leg
HEDGE_AFFORD_BUFFER = 1.15

VALID_HEDGE_EXIT_REASONS = frozenset(
    {
        "HEDGE_TARGET",
        "HEDGE_STOPLOSS",
        "HEDGE_EXPIRY",
        "HEDGE_MANUAL",
    }
)

# Per-hedge close locks (same pattern as bot_engine._exit_locks)
_hedge_close_locks: dict[int, asyncio.Lock] = {}


def _get_hedge_close_lock(hedge_id: int) -> asyncio.Lock:
    hid = int(hedge_id)
    lock = _hedge_close_locks.get(hid)
    if lock is None:
        lock = asyncio.Lock()
        _hedge_close_locks[hid] = lock
    return lock


def _hedge_log(
    event_type: str,
    hedge_id: int,
    details: dict[str, Any],
    *,
    critical: bool = False,
) -> None:
    """All hedge tags go through log_and_buffer → bot_activity.log."""
    try:
        log_and_buffer(event_type, int(hedge_id), details)
    except Exception as exc:
        logger.warning("log_and_buffer failed for %s: %s", event_type, exc)
    msg = f"[{event_type}] hedge_id={hedge_id} {details}"
    if critical:
        logger.critical(msg)
    else:
        logger.info(msg)


class HedgeOpenError(Exception):
    """Hedge open failed after cleanup — caller should surface to user."""

    def __init__(self, stage: str, reason: str, hedge: HedgePosition | None = None):
        self.stage = stage
        self.reason = reason
        self.hedge = hedge
        super().__init__(f"[{stage}] {reason}")


class HedgeCloseError(Exception):
    """Hedge close failed — may leave status=exit_failed."""

    def __init__(self, stage: str, reason: str, hedge: HedgePosition | None = None):
        self.stage = stage
        self.reason = reason
        self.hedge = hedge
        super().__init__(f"[{stage}] {reason}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_underlying(underlying: str) -> str:
    u = str(underlying or "BTC").upper().strip()
    if u.endswith("USD") and len(u) > 3:
        return u[:-3]
    return u


async def _position_size(client: DeltaClient, product_id: int) -> float:
    """Signed size for product_id from option positions (0 if flat/missing)."""
    wanted = int(product_id)
    try:
        positions = await client.get_option_positions()
    except Exception:
        return 0.0
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        try:
            if int(pos.get("product_id") or 0) != wanted:
                continue
            return float(pos.get("size") or 0)
        except (TypeError, ValueError):
            continue
    return 0.0


async def _verify_leg(
    client: DeltaClient,
    *,
    leg: str,
    product_id: int,
    hedge_id: int = 0,
) -> tuple[bool, float]:
    exists = await client.verify_position_exists(int(product_id))
    size = await _position_size(client, int(product_id)) if exists else 0.0
    _hedge_log(
        "HEDGE_VERIFY",
        int(hedge_id),
        {
            "leg": leg,
            "product_id": int(product_id),
            "exists": bool(exists),
            "size": float(size),
        },
    )
    return bool(exists), float(size)


async def _unwind_long(
    client: DeltaClient,
    *,
    product_id: int,
    quantity: int,
    symbol: str,
    hedge_id: int = 0,
    leg: str = "call",
) -> tuple[bool, str | None]:
    """
    Close a long leg with reduce_only=True and verify flat.

    Returns (verified_flat, last_order_id). Always emits [HEDGE_UNWIND].
    """
    executor = OrderExecutor()
    last_order_id: str | None = None
    for attempt in range(1, UNWIND_VERIFY_ATTEMPTS + 1):
        try:
            result = await client.close_position(
                product_id=int(product_id),
                size=int(quantity),
                is_long=True,
            )
            if isinstance(result, dict):
                oid = result.get("id") or result.get("order_id")
                if oid is not None:
                    last_order_id = str(oid)
        except Exception as exc:
            logger.warning(
                "Unwind close_position attempt %s failed product=%s: %s — "
                "retrying via OrderExecutor",
                attempt,
                product_id,
                exc,
            )
            res = await executor.close_long_position(
                product_id=int(product_id),
                quantity=int(quantity),
                delta_client=client,
                symbol_for_fallback=symbol,
            )
            if res.order_id is not None:
                last_order_id = str(res.order_id)
        await asyncio.sleep(VERIFY_PAUSE_SECONDS)
        exists, size = await _verify_leg(
            client,
            leg=f"{leg}_unwind",
            product_id=int(product_id),
            hedge_id=hedge_id,
        )
        flat = (not exists) or abs(size) < 1e-9
        if flat:
            _hedge_log(
                "HEDGE_UNWIND",
                int(hedge_id),
                {
                    "leg": leg,
                    "product_id": int(product_id),
                    "symbol": symbol,
                    "quantity": int(quantity),
                    "order_id": last_order_id,
                    "verified_flat": True,
                    "attempt": attempt,
                },
            )
            return True, last_order_id
        logger.warning(
            "Unwind still open after attempt %s product=%s size=%s",
            attempt,
            product_id,
            size,
        )

    _hedge_log(
        "HEDGE_UNWIND",
        int(hedge_id),
        {
            "leg": leg,
            "product_id": int(product_id),
            "symbol": symbol,
            "quantity": int(quantity),
            "order_id": last_order_id,
            "verified_flat": False,
            "attempt": UNWIND_VERIFY_ATTEMPTS,
        },
        critical=True,
    )
    return False, last_order_id


async def _resolve_long_exit_fill(
    client: DeltaClient,
    *,
    order_result: dict[str, Any] | None,
    product_id: int,
    symbol: str | None,
    entry_time: datetime | None,
) -> float | None:
    """
    Real exit fill for a long leg. Never returns 0.0 as a stand-in.
    """
    if order_result is not None:
        try:
            px = float(
                await client.resolve_fill_price(
                    order_result, symbol_for_fallback=symbol
                )
                or 0.0
            )
            if px > 0:
                return px
        except Exception as exc:
            logger.warning("resolve_fill_price on close order failed: %s", exc)

    try:
        px = await client.get_product_exit_fill_since(
            product_id=int(product_id),
            since=entry_time,
            side="sell",
            is_long=True,
        )
        if px is not None and float(px) > 0:
            return float(px)
    except Exception as exc:
        logger.warning(
            "get_product_exit_fill_since product=%s failed: %s", product_id, exc
        )
    return None


def get_active_hedge(
    db: Session,
    *,
    account_id: int,
    underlying: str,
) -> HedgePosition | None:
    und = _normalize_underlying(underlying)
    return (
        db.query(HedgePosition)
        .filter(
            HedgePosition.account_id == int(account_id),
            HedgePosition.underlying == und,
            HedgePosition.status == "active",
        )
        .order_by(HedgePosition.id.desc())
        .first()
    )


async def open_hedge(
    account: Account,
    settings: AutoTradeSettings,
    db: Session,
    *,
    client: DeltaClient,
    quantity_override: int | None = None,
) -> HedgePosition:
    """
    Buy a long ATM straddle (call then put), verify both legs, persist hedge_positions.

    If the put fails after the call is live, unwind the call with reduce_only and
    persist status='error'. Never leaves a one-legged hedge unmarked.
    """
    und = _normalize_underlying(str(settings.underlying or "BTC"))
    qty = max(
        1,
        int(
            quantity_override
            if quantity_override is not None
            else (settings.quantity or 1)
        ),
    )

    existing = get_active_hedge(db, account_id=int(account.id), underlying=und)
    if existing is not None:
        _hedge_log(
            "HEDGE_OPEN_START",
            int(existing.id),
            {
                "skipped": True,
                "reason": "active_hedge_exists",
                "underlying": und,
            },
        )
        return existing

    raw_mode = str(getattr(settings, "hedge_expiry_mode", None) or "month_1")
    dte = getattr(settings, "hedge_expiry_dte", None)
    mode, needs_repick = migrate_hedge_expiry_mode(
        raw_mode,
        expiry_dte=int(dte) if dte is not None else None,
    )
    if needs_repick or mode == "date":
        raise HedgeOpenError(
            "resolve_expiry",
            "Hedge expiry label is stale or a fixed date — re-pick a labelled "
            "relative expiry (e.g. Month 2) in settings before opening.",
        )

    executor = OrderExecutor()
    hedge_row: HedgePosition | None = None

    try:
        try:
            expiry = await resolve_hedge_expiry_date(
                client,
                und,
                expiry_mode=mode,
                expiry_date_override=getattr(
                    settings, "hedge_expiry_date_override", None
                ),
                expiry_dte=dte,
            )
        except ExpiryNotAvailableError as exc:
            raise HedgeOpenError("resolve_expiry", str(exc)) from exc
        except HedgeThetaError as exc:
            raise HedgeOpenError("resolve_expiry", str(exc)) from exc

        price_symbol = f"{und}USD"
        try:
            spot = float(await client.get_underlying_price(price_symbol))
            chain = await client.get_option_chain(und, expiry.isoformat())
        except DeltaAPIError as exc:
            raise HedgeOpenError("resolve_atm", str(exc)) from exc

        if not chain:
            raise HedgeOpenError(
                "resolve_atm",
                f"Empty option chain for {und} {expiry.isoformat()}",
            )
        atm = annotate_atm(chain, spot)
        if atm is None:
            raise HedgeOpenError("resolve_atm", "Could not resolve ATM strike")
        row = next((r for r in chain if float(r["strike"]) == float(atm)), None)
        if row is None:
            raise HedgeOpenError("resolve_atm", f"ATM strike {atm} missing from chain")

        call_pid = int(row.get("call_product_id") or 0)
        put_pid = int(row.get("put_product_id") or 0)
        call_symbol = str(row.get("call_symbol") or "")
        put_symbol = str(row.get("put_symbol") or "")
        if call_pid <= 0 or put_pid <= 0:
            raise HedgeOpenError(
                "resolve_atm",
                "ATM call/put product_id missing from chain",
            )

        call_theta = abs(float(row.get("call_theta") or 0))
        put_theta = abs(float(row.get("put_theta") or 0))
        entry_total_theta = call_theta + put_theta
        call_iv = float(row.get("call_iv") or 0)
        put_iv = float(row.get("put_iv") or 0)

        call_ask = float(
            row.get("call_ask") or row.get("call_mark_price") or 0
        )
        put_ask = float(
            row.get("put_ask") or row.get("put_mark_price") or 0
        )
        call_bid_entry = float(row.get("call_bid") or 0)
        put_bid_entry = float(row.get("put_bid") or 0)
        if call_ask <= 0 or put_ask <= 0:
            raise HedgeOpenError(
                "afford",
                "Cannot estimate hedge cost — missing call/put ask on chain",
            )

        # Half-spread paper loss at open (ask − bid) × qty × CV — both legs
        entry_spread_usd = 0.0
        if call_bid_entry > 0:
            entry_spread_usd += (call_ask - call_bid_entry) * qty * CONTRACT_SIZE
        if put_bid_entry > 0:
            entry_spread_usd += (put_ask - put_bid_entry) * qty * CONTRACT_SIZE
        entry_spread_usd = max(0.0, float(entry_spread_usd))

        est_cost_per_lot = (call_ask + put_ask) * CONTRACT_SIZE
        est_cost = est_cost_per_lot * qty
        required = est_cost * HEDGE_AFFORD_BUFFER

        try:
            wallet = await client.get_wallet_balance()
            available = float(wallet.get("available_balance") or 0)
        except Exception as exc:
            raise HedgeOpenError(
                "afford",
                f"Could not fetch wallet balance for affordability check: {exc}",
            ) from exc

        if available < required:
            shortfall = round(required - available, 4)
            max_affordable_qty = int(
                available // (est_cost_per_lot * HEDGE_AFFORD_BUFFER)
            )
            if max_affordable_qty < 1:
                qty_hint = "Add funds — even 1 lot is not affordable right now."
            else:
                qty_hint = (
                    f"Reduce quantity to {max_affordable_qty} or add funds."
                )
            msg = (
                f"Hedge needs ${required:.2f} for {qty} lot(s) "
                f"(asks ${call_ask:.2f}+${put_ask:.2f} × {qty} × "
                f"{CONTRACT_SIZE} × {HEDGE_AFFORD_BUFFER:.0%} buffer), "
                f"${available:.2f} available (shortfall ${shortfall:.2f}). "
                f"{qty_hint}"
            )
            _hedge_log(
                "HEDGE_AFFORD_BLOCK",
                0,
                {
                    "required": round(required, 4),
                    "available": round(available, 4),
                    "shortfall": shortfall,
                    "quantity": qty,
                    "est_cost": round(est_cost, 4),
                    "call_ask": call_ask,
                    "put_ask": put_ask,
                    "max_affordable_qty": max_affordable_qty,
                },
                critical=True,
            )
            raise HedgeOpenError("afford", msg)

        _hedge_log(
            "HEDGE_OPEN_START",
            0,
            {
                "underlying": und,
                "expiry": expiry.isoformat(),
                "strike": float(atm),
                "quantity": qty,
                "spot": round(spot, 2),
                "mode": mode,
                "est_cost": round(est_cost, 4),
                "required_with_buffer": round(required, 4),
                "available": round(available, 4),
            },
        )

        # --- BUY CALL ---
        call_result = await executor.buy_option(
            product_id=call_pid,
            quantity=qty,
            delta_client=client,
            symbol_for_fallback=call_symbol,
        )
        if not call_result.success:
            _hedge_log(
                "HEDGE_OPEN_FAIL",
                0,
                {"stage": "buy_call", "reason": call_result.error or "Call buy failed"},
                critical=True,
            )
            raise HedgeOpenError(
                "buy_call",
                call_result.error or "Call buy order failed",
            )

        await asyncio.sleep(VERIFY_PAUSE_SECONDS)
        call_ok, call_size = await _verify_leg(
            client, leg="call", product_id=call_pid, hedge_id=0
        )
        if not call_ok:
            _hedge_log(
                "HEDGE_OPEN_FAIL",
                0,
                {
                    "stage": "verify_call",
                    "reason": f"Call not on Delta product_id={call_pid}",
                },
                critical=True,
            )
            raise HedgeOpenError(
                "verify_call",
                f"Call buy filled but position not found on Delta "
                f"(product_id={call_pid})",
            )

        call_fill = float(call_result.filled_price or 0)
        call_fee = float(call_result.commission or 0)
        call_order_id = (
            str(call_result.order_id) if call_result.order_id is not None else None
        )

        # --- BUY PUT ---
        put_result = await executor.buy_option(
            product_id=put_pid,
            quantity=qty,
            delta_client=client,
            symbol_for_fallback=put_symbol,
        )
        put_ok = False
        put_fill = 0.0
        put_fee = 0.0
        put_order_id: str | None = None
        put_fail_reason = ""

        if not put_result.success:
            put_fail_reason = put_result.error or "Put buy order failed"
        else:
            await asyncio.sleep(VERIFY_PAUSE_SECONDS)
            put_ok, _put_size = await _verify_leg(
                client, leg="put", product_id=put_pid, hedge_id=0
            )
            if not put_ok:
                put_fail_reason = (
                    f"Put buy filled but position not found on Delta "
                    f"(product_id={put_pid})"
                )
            else:
                put_fill = float(put_result.filled_price or 0)
                put_fee = float(put_result.commission or 0)
                put_order_id = (
                    str(put_result.order_id)
                    if put_result.order_id is not None
                    else None
                )

        if not put_ok:
            _hedge_log(
                "HEDGE_OPEN_FAIL",
                0,
                {"stage": "buy_put", "reason": put_fail_reason},
                critical=True,
            )
            flat, unwind_oid = await _unwind_long(
                client,
                product_id=call_pid,
                quantity=qty,
                symbol=call_symbol,
                hedge_id=0,
                leg="call",
            )
            err_msg = put_fail_reason
            if not flat:
                err_msg = (
                    f"{put_fail_reason}; CRITICAL: call unwind incomplete "
                    f"(product_id={call_pid}, order_id={unwind_oid}) — "
                    f"check Delta manually"
                )
                _hedge_log(
                    "HEDGE_OPEN_FAIL",
                    0,
                    {
                        "stage": "unwind_call",
                        "reason": "still_open",
                        "product_id": call_pid,
                        "order_id": unwind_oid,
                        "verified_flat": False,
                    },
                    critical=True,
                )

            hedge_row = HedgePosition(
                account_id=int(account.id),
                underlying=und,
                expiry_date=expiry,
                strike=float(atm),
                quantity=qty,
                status="error",
                call_product_id=call_pid,
                call_symbol=call_symbol,
                call_order_id=call_order_id,
                call_fill_price=call_fill if call_fill > 0 else None,
                call_entry_fee_usd=call_fee if call_fee > 0 else None,
                put_product_id=put_pid,
                put_symbol=put_symbol,
                put_order_id=put_order_id,
                entry_time=_utc_now(),
                target_usd=(
                    float(settings.hedge_target_usd)
                    if getattr(settings, "hedge_target_usd", None) is not None
                    else None
                ),
                stoploss_usd=(
                    float(settings.hedge_stoploss_usd)
                    if getattr(settings, "hedge_stoploss_usd", None) is not None
                    else None
                ),
                entry_total_theta=entry_total_theta,
                entry_call_iv=call_iv if call_iv > 0 else None,
                entry_put_iv=put_iv if put_iv > 0 else None,
                order_margin_per_lot=None,
                is_bot_managed=True,
                last_error=err_msg[:500],
            )
            db.add(hedge_row)
            db.commit()
            db.refresh(hedge_row)
            raise HedgeOpenError("buy_put", err_msg, hedge=hedge_row)

        cost_usd = (call_fill + put_fill) * qty * CONTRACT_SIZE
        hedge_row = HedgePosition(
            account_id=int(account.id),
            underlying=und,
            expiry_date=expiry,
            strike=float(atm),
            quantity=qty,
            status="active",
            call_product_id=call_pid,
            call_symbol=call_symbol,
            call_order_id=call_order_id,
            call_fill_price=call_fill if call_fill > 0 else None,
            call_entry_fee_usd=call_fee if call_fee > 0 else None,
            put_product_id=put_pid,
            put_symbol=put_symbol,
            put_order_id=put_order_id,
            put_fill_price=put_fill if put_fill > 0 else None,
            put_entry_fee_usd=put_fee if put_fee > 0 else None,
            entry_time=_utc_now(),
            # Snapshot Auto Trade defaults at open only. Changing
            # hedge_target_usd / hedge_stoploss_usd in settings does NOT
            # retro-apply — edit the live hedge via PATCH /api/hedge/{id}/settings
            # (Dashboard hedge panel).
            target_usd=(
                float(settings.hedge_target_usd)
                if getattr(settings, "hedge_target_usd", None) is not None
                else None
            ),
            stoploss_usd=(
                float(settings.hedge_stoploss_usd)
                if getattr(settings, "hedge_stoploss_usd", None) is not None
                else None
            ),
            entry_total_theta=entry_total_theta,
            entry_call_iv=call_iv if call_iv > 0 else None,
            entry_put_iv=put_iv if put_iv > 0 else None,
            order_margin_per_lot=None,
            entry_spread_usd=float(entry_spread_usd),
            hedge_net_mtm=0.0,
            hedge_gross_for_sl=0.0,
            cum_closed_basket_pnl=0.0,
            structure_pnl=0.0,
            is_bot_managed=True,
            last_error=None,
        )
        db.add(hedge_row)
        # Keep resolved date on settings for display (label stays the source of truth)
        try:
            settings.hedge_expiry_date_override = expiry.isoformat()
        except Exception:
            pass
        db.commit()
        db.refresh(hedge_row)

        _hedge_log(
            "HEDGE_OPEN_DONE",
            int(hedge_row.id),
            {
                "call_fill": call_fill,
                "put_fill": put_fill,
                "cost_usd": round(cost_usd, 4),
                "entry_spread_usd": round(float(entry_spread_usd), 6),
                "entry_total_theta": entry_total_theta,
                "call_size": call_size,
                "expiry": expiry.isoformat(),
                "strike": float(atm),
                "quantity": qty,
            },
        )
        # Day-1 snapshot so accrual/IV history starts even if bot restarts later today
        await maybe_log_hedge_theta_snapshot(hedge_row, db, client=client)
        return hedge_row

    except HedgeOpenError:
        raise
    except Exception as exc:
        _hedge_log(
            "HEDGE_OPEN_FAIL",
            0,
            {"stage": "unexpected", "reason": str(exc)},
            critical=True,
        )
        raise HedgeOpenError("unexpected", str(exc), hedge=hedge_row) from exc


async def _cascade_close_baskets_under_hedge(
    hedge_id: int,
    db: Session,
    *,
    reason: str,
) -> dict[str, Any]:
    """
    Close every active short basket linked to this hedge via close_master_trade.

    Baskets close FIRST so shorts are never live without their hedge.
    Returns counts + failed trade ids. Caller must NOT close the hedge if
    baskets_failed > 0.
    """
    from backend.config import ExitReason, TradeStatus
    from backend.engine.bot_engine import bot_engine

    hid = int(hedge_id)
    baskets = (
        db.query(Trade)
        .filter(
            Trade.hedge_position_id == hid,
            Trade.status == TradeStatus.ACTIVE.value,
        )
        .order_by(Trade.id.asc())
        .all()
    )
    trade_ids = [int(t.id) for t in baskets]
    closed_ids: list[int] = []
    failed_ids: list[int] = []

    _hedge_log(
        "HEDGE_CASCADE",
        hid,
        {"hedge": hid, "baskets_found": len(trade_ids), "phase": "start"},
    )

    for tid in trade_ids:
        try:
            await bot_engine.close_master_trade(
                trade_id=tid,
                reason=ExitReason.HEDGE_CLOSED.value,
                db=db,
                trade_state=bot_engine.position_tracker.get(tid),
            )
        except Exception as exc:
            _hedge_log(
                "HEDGE_CASCADE",
                hid,
                {
                    "hedge": hid,
                    "trade_id": tid,
                    "error": str(exc),
                    "phase": "basket_exception",
                },
                critical=True,
            )
            failed_ids.append(tid)
            continue

        # close_master_trade uses its own sessions — re-query status
        db.expire_all()
        row = db.query(Trade).filter(Trade.id == tid).first()
        st = str(row.status or "").lower() if row is not None else "missing"
        if st == TradeStatus.ACTIVE.value:
            failed_ids.append(tid)
            _hedge_log(
                "HEDGE_CASCADE",
                hid,
                {
                    "hedge": hid,
                    "trade_id": tid,
                    "status": st,
                    "phase": "still_active",
                },
                critical=True,
            )
        else:
            closed_ids.append(tid)

    result = {
        "hedge": hid,
        "reason": reason,
        "baskets_found": len(trade_ids),
        "baskets_closed": len(closed_ids),
        "baskets_failed": len(failed_ids),
        "closed_trade_ids": closed_ids,
        "failed_trade_ids": failed_ids,
        "phase": "done",
    }
    _hedge_log("HEDGE_CASCADE", hid, result)
    return result


def count_active_baskets_for_hedge(db: Session, hedge_id: int) -> int:
    """Active short baskets stamped with this hedge_position_id."""
    from backend.config import TradeStatus

    return (
        db.query(Trade)
        .filter(
            Trade.hedge_position_id == int(hedge_id),
            Trade.status == TradeStatus.ACTIVE.value,
        )
        .count()
    )


async def close_hedge(
    hedge_id: int,
    reason: str,
    db: Session,
    *,
    client: DeltaClient,
) -> HedgePosition:
    """
    Close linked short baskets first, then both long hedge legs.

    Real exit fills only — never books 0.0. Unverified flat → status=exit_failed.
    If any basket fails to close, the hedge is NOT closed (HEDGE_CLOSE_BLOCKED).
    Protected by a per-hedge asyncio.Lock.
    """
    hid = int(hedge_id)
    reason_norm = str(reason or "HEDGE_MANUAL").upper().strip()
    if reason_norm not in VALID_HEDGE_EXIT_REASONS:
        raise HedgeCloseError(
            "reason",
            f"Invalid exit reason '{reason}'. "
            f"Valid: {', '.join(sorted(VALID_HEDGE_EXIT_REASONS))}",
        )

    lock = _get_hedge_close_lock(hid)
    async with lock:
        hedge = db.query(HedgePosition).filter(HedgePosition.id == hid).first()
        if hedge is None:
            raise HedgeCloseError("lookup", f"Hedge #{hid} not found")

        status = str(hedge.status or "").lower()
        if status == "closed":
            _hedge_log(
                "HEDGE_CLOSE_SKIP",
                hid,
                {
                    "reason": reason_norm,
                    "existing_reason": hedge.exit_reason,
                    "status": status,
                },
            )
            return hedge

        if status not in {"active", "exit_failed", "partial", "error"}:
            raise HedgeCloseError(
                "status",
                f"Hedge #{hid} status={status} cannot be closed",
                hedge=hedge,
            )

        call_pid = int(hedge.call_product_id or 0)
        put_pid = int(hedge.put_product_id or 0)
        qty = max(1, int(hedge.quantity or 1))
        call_symbol = str(hedge.call_symbol or "")
        put_symbol = str(hedge.put_symbol or "")

        if call_pid <= 0 or put_pid <= 0:
            raise HedgeCloseError(
                "legs",
                "Hedge missing call/put product_id",
                hedge=hedge,
            )

        _hedge_log(
            "HEDGE_CLOSE_START",
            hid,
            {
                "reason": reason_norm,
                "underlying": hedge.underlying,
                "expiry": (
                    hedge.expiry_date.isoformat() if hedge.expiry_date else None
                ),
                "strike": hedge.strike,
                "quantity": qty,
                "call_product_id": call_pid,
                "put_product_id": put_pid,
            },
        )

        # Baskets FIRST — never leave shorts live without their hedge
        cascade = await _cascade_close_baskets_under_hedge(
            hid, db, reason=reason_norm
        )
        if int(cascade.get("baskets_failed") or 0) > 0:
            failed_ids = list(cascade.get("failed_trade_ids") or [])
            err = (
                f"Cannot close hedge #{hid}: baskets still open "
                f"{failed_ids} — will retry next cycle"
            )
            _hedge_log(
                "HEDGE_CLOSE_BLOCKED",
                hid,
                {
                    "hedge": hid,
                    "failed_baskets": failed_ids,
                    "reason": reason_norm,
                    "baskets_found": cascade.get("baskets_found"),
                    "baskets_closed": cascade.get("baskets_closed"),
                },
                critical=True,
            )
            raise HedgeCloseError("cascade", err, hedge=hedge)

        call_exists, call_size = await _verify_leg(
            client, leg="call_pre", product_id=call_pid, hedge_id=hid
        )
        put_exists, put_size = await _verify_leg(
            client, leg="put_pre", product_id=put_pid, hedge_id=hid
        )

        executor = OrderExecutor()
        call_order: dict[str, Any] | None = None
        put_order: dict[str, Any] | None = None
        call_exit_fee: float | None = None
        put_exit_fee: float | None = None

        if call_exists and abs(call_size) >= 1e-9:
            close_size = max(1, int(round(abs(call_size)))) or qty
            try:
                call_order = await client.close_position(
                    product_id=call_pid,
                    size=close_size,
                    is_long=True,
                )
            except Exception as exc:
                logger.warning(
                    "close_position call failed hedge=%s: %s — OrderExecutor",
                    hid,
                    exc,
                )
                res = await executor.close_long_position(
                    product_id=call_pid,
                    quantity=close_size,
                    delta_client=client,
                    symbol_for_fallback=call_symbol,
                )
                if res.success:
                    call_order = {
                        "order_id": res.order_id,
                        "avg_fill_price": res.filled_price,
                    }
                    if res.commission:
                        call_exit_fee = float(res.commission)
                else:
                    _hedge_log(
                        "HEDGE_CLOSE_FAIL",
                        hid,
                        {"stage": "close_call", "reason": res.error or str(exc)},
                        critical=True,
                    )

        if put_exists and abs(put_size) >= 1e-9:
            close_size = max(1, int(round(abs(put_size)))) or qty
            try:
                put_order = await client.close_position(
                    product_id=put_pid,
                    size=close_size,
                    is_long=True,
                )
            except Exception as exc:
                logger.warning(
                    "close_position put failed hedge=%s: %s — OrderExecutor",
                    hid,
                    exc,
                )
                res = await executor.close_long_position(
                    product_id=put_pid,
                    quantity=close_size,
                    delta_client=client,
                    symbol_for_fallback=put_symbol,
                )
                if res.success:
                    put_order = {
                        "order_id": res.order_id,
                        "avg_fill_price": res.filled_price,
                    }
                    if res.commission:
                        put_exit_fee = float(res.commission)
                else:
                    _hedge_log(
                        "HEDGE_CLOSE_FAIL",
                        hid,
                        {"stage": "close_put", "reason": res.error or str(exc)},
                        critical=True,
                    )

        await asyncio.sleep(VERIFY_PAUSE_SECONDS)

        call_still, call_sz2 = await _verify_leg(
            client, leg="call_post", product_id=call_pid, hedge_id=hid
        )
        put_still, put_sz2 = await _verify_leg(
            client, leg="put_post", product_id=put_pid, hedge_id=hid
        )
        call_flat = (not call_still) or abs(call_sz2) < 1e-9
        put_flat = (not put_still) or abs(put_sz2) < 1e-9

        if not call_flat or not put_flat:
            err = (
                f"Not flat after close: call_flat={call_flat} size={call_sz2}, "
                f"put_flat={put_flat} size={put_sz2}"
            )
            hedge.status = "exit_failed"
            hedge.last_error = err[:500]
            hedge.exit_reason = reason_norm
            db.commit()
            db.refresh(hedge)
            _hedge_log(
                "HEDGE_CLOSE_FAIL",
                hid,
                {
                    "stage": "verify_flat",
                    "reason": err,
                    "call_flat": call_flat,
                    "put_flat": put_flat,
                    "call_size": call_sz2,
                    "put_size": put_sz2,
                },
                critical=True,
            )
            raise HedgeCloseError("verify_flat", err, hedge=hedge)

        call_exit = await _resolve_long_exit_fill(
            client,
            order_result=call_order,
            product_id=call_pid,
            symbol=call_symbol,
            entry_time=hedge.entry_time,
        )
        put_exit = await _resolve_long_exit_fill(
            client,
            order_result=put_order,
            product_id=put_pid,
            symbol=put_symbol,
            entry_time=hedge.entry_time,
        )

        unresolved: list[str] = []
        if call_exit is None or call_exit <= 0:
            call_exit = None
            unresolved.append("PNL_UNRESOLVED_call")
            _hedge_log(
                "HEDGE_CLOSE_FAIL",
                hid,
                {"stage": "fill_call", "reason": "PNL_UNRESOLVED_call"},
                critical=True,
            )
        if put_exit is None or put_exit <= 0:
            put_exit = None
            unresolved.append("PNL_UNRESOLVED_put")
            _hedge_log(
                "HEDGE_CLOSE_FAIL",
                hid,
                {"stage": "fill_put", "reason": "PNL_UNRESOLVED_put"},
                critical=True,
            )

        if call_exit_fee is None and call_order is not None:
            raw = (
                call_order.get("raw")
                if isinstance(call_order.get("raw"), dict)
                else {}
            )
            for src in (call_order, raw):
                try:
                    fee = abs(
                        float(src.get("paid_commission") or src.get("commission") or 0)
                    )
                except (TypeError, ValueError):
                    fee = 0.0
                if fee > 0:
                    call_exit_fee = fee
                    break
        if put_exit_fee is None and put_order is not None:
            raw = (
                put_order.get("raw")
                if isinstance(put_order.get("raw"), dict)
                else {}
            )
            for src in (put_order, raw):
                try:
                    fee = abs(
                        float(src.get("paid_commission") or src.get("commission") or 0)
                    )
                except (TypeError, ValueError):
                    fee = 0.0
                if fee > 0:
                    put_exit_fee = fee
                    break

        hedge.call_exit_price = call_exit
        hedge.put_exit_price = put_exit

        entry_call = float(hedge.call_fill_price or 0)
        entry_put = float(hedge.put_fill_price or 0)
        fees = (
            float(hedge.call_entry_fee_usd or 0)
            + float(hedge.put_entry_fee_usd or 0)
            + float(call_exit_fee or 0)
            + float(put_exit_fee or 0)
        )

        realized: float | None = None
        if (
            call_exit is not None
            and put_exit is not None
            and entry_call > 0
            and entry_put > 0
        ):
            gross = (
                (call_exit - entry_call) + (put_exit - entry_put)
            ) * qty * CONTRACT_SIZE
            realized = round(gross - fees, 6)
            hedge.realized_pnl = realized
        else:
            hedge.realized_pnl = None

        now = _utc_now()
        hedge.status = "closed"
        hedge.exit_time = now
        hedge.exit_reason = reason_norm
        if unresolved:
            prior = str(hedge.last_error or "")
            tag = ";".join(unresolved)
            hedge.last_error = (
                f"{prior};{tag}".strip(";") if prior else tag
            )[:500]
        else:
            hedge.last_error = None

        db.commit()
        db.refresh(hedge)

        _hedge_log(
            "HEDGE_CLOSE_DONE",
            hid,
            {
                "reason": reason_norm,
                "call_exit": call_exit,
                "put_exit": put_exit,
                "realized_pnl": realized,
                "fees": round(fees, 6),
                "unresolved": unresolved,
            },
        )
        return hedge


async def _fetch_strict_bid(client: DeltaClient, symbol: str) -> float | None:
    """
    Best bid to sell a long. No mark/ask fallback — None if bid unavailable.
    """
    sym = str(symbol or "").strip()
    if not sym:
        return None
    try:
        book = await client._request("GET", f"/v2/l2orderbook/{sym}")
        if isinstance(book, dict):
            buys = book.get("buy") or book.get("bids") or []
            if buys and isinstance(buys[0], dict):
                try:
                    l2_bid = float(buys[0].get("price") or 0)
                except (TypeError, ValueError):
                    l2_bid = 0.0
                if l2_bid > 0:
                    return l2_bid
    except Exception as exc:
        logger.warning("L2 bid fetch failed for %s: %s", sym, exc)

    try:
        ticker = await client.get_ticker(sym)
        bid, _ask, _mark, _delta = _extract_live_quote(ticker)
        if bid > 0:
            return float(bid)
    except Exception as exc:
        logger.warning("Ticker bid fetch failed for %s: %s", sym, exc)
    return None


def compute_long_hedge_pnl(
    *,
    call_bid: float,
    put_bid: float,
    call_entry: float,
    put_entry: float,
    quantity: int,
    entry_fees: float,
    estimated_exit_fees: float,
) -> dict[str, float]:
    """
    Unrealized P&L for a LONG straddle (buy call + buy put).

    LONG vs SHORT sign convention (CRITICAL):
      Short basket: P&L = (entry - current) × qty × CV  → premium rise hurts
      Long hedge:   P&L = (current - entry) × qty × CV  → premium rise HELPS

    If both legs are worth MORE than entry, gross P&L is POSITIVE.
    We sell to close → mark-to-market at BID (exit proceeds).
    """
    qty = max(1, int(quantity or 1))
    call_pnl = (float(call_bid) - float(call_entry)) * qty * CONTRACT_SIZE
    put_pnl = (float(put_bid) - float(put_entry)) * qty * CONTRACT_SIZE
    gross = call_pnl + put_pnl
    fees = max(0.0, float(entry_fees or 0.0)) + max(
        0.0, float(estimated_exit_fees or 0.0)
    )
    net = gross - fees
    return {
        "call_pnl": float(call_pnl),
        "put_pnl": float(put_pnl),
        "gross": float(gross),
        "net": float(net),
        "fees": float(fees),
    }


def _cum_closed_basket_pnl(db: Session, hedge_id: int) -> float:
    """
    Sum realized_pnl of closed baskets stamped to this hedge.

    ALREADY REALIZED — do not apply spread, fee, or slippage adjustments.
    """
    from backend.config import TradeStatus
    from sqlalchemy import func

    raw = (
        db.query(func.coalesce(func.sum(Trade.realized_pnl), 0.0))
        .filter(
            Trade.hedge_position_id == int(hedge_id),
            Trade.status == TradeStatus.CLOSED.value,
        )
        .scalar()
    )
    return float(raw or 0.0)


def _open_basket_net_mtm(
    db: Session,
    hedge_id: int,
    position_tracker: Any | None,
) -> float:
    """Sum last_net_mtm of active baskets under this hedge (0 if none / unknown)."""
    from backend.config import TradeStatus

    if position_tracker is None:
        return 0.0
    active = (
        db.query(Trade)
        .filter(
            Trade.hedge_position_id == int(hedge_id),
            Trade.status == TradeStatus.ACTIVE.value,
        )
        .all()
    )
    total = 0.0
    for trade in active:
        state = position_tracker.get(int(trade.id))
        if state is None:
            continue
        total += float(getattr(state, "last_net_mtm", 0.0) or 0.0)
    return float(total)


def compute_hedge_net_mtm_fields(
    *,
    call_bid: float,
    put_bid: float,
    call_entry: float,
    put_entry: float,
    quantity: int,
    entry_fees: float,
    estimated_exit_fees: float,
    entry_spread_usd: float,
    hedge_est_exit_slippage_usd: float = 0.0,
) -> dict[str, float]:
    """
    Long-hedge net_mtm / gross_for_sl (mirror of short-basket convention).

    hedge_net_mtm = Σ(bid − ask_at_entry)×qty×CV − fees − est_exit − est_slip
    hedge_gross_for_sl = hedge_net_mtm + entry_spread_usd

    hedge_est_exit_slippage_usd is the exit-spread estimate (AUTO/MANUAL);
    application to net_mtm is unchanged (subtract the USD amount).
    """
    qty = max(1, int(quantity or 1))
    # ask_at_entry stored as fill_price (bought at ask)
    gross = (
        (float(call_bid) - float(call_entry)) * qty * CONTRACT_SIZE
        + (float(put_bid) - float(put_entry)) * qty * CONTRACT_SIZE
    )
    fees_paid = max(0.0, float(entry_fees or 0.0))
    est_exit = max(0.0, float(estimated_exit_fees or 0.0))
    est_slip = max(0.0, float(hedge_est_exit_slippage_usd or 0.0))
    hedge_net = gross - fees_paid - est_exit - est_slip
    entry_spread = max(0.0, float(entry_spread_usd or 0.0))
    hedge_gross_sl = hedge_net + entry_spread
    return {
        "gross_upnl": float(gross),
        "hedge_fees_paid": float(fees_paid),
        "hedge_est_exit_fees": float(est_exit),
        "hedge_est_exit_slippage_usd": float(est_slip),
        "hedge_net_mtm": float(hedge_net),
        "entry_spread_usd": float(entry_spread),
        "hedge_gross_for_sl": float(hedge_gross_sl),
    }


async def persist_structure_pnl(
    hedge: HedgePosition,
    db: Session,
    *,
    call_bid: float,
    put_bid: float,
    est_exit_fees: float,
    position_tracker: Any | None = None,
    client: Any | None = None,
) -> dict[str, float]:
    """
    Compute + persist hedge_net_mtm / structure_pnl columns (no exit triggers).

    Logs [STRUCTURE_PNL] once per successful cycle via log_and_buffer.
    """
    from backend.database import get_or_create_auto_settings

    hid = int(hedge.id)
    qty = max(1, int(hedge.quantity or 1))
    call_entry = float(hedge.call_fill_price or 0)
    put_entry = float(hedge.put_fill_price or 0)
    entry_fees = float(hedge.call_entry_fee_usd or 0) + float(
        hedge.put_entry_fee_usd or 0
    )
    entry_spread = float(getattr(hedge, "entry_spread_usd", 0.0) or 0.0)

    # Exit-spread USD for both legs (replaces gross×slippage_pct for this field)
    est_slip_usd = 0.0
    try:
        spread_settings = get_or_create_auto_settings(db)
        call_sym = str(hedge.call_symbol or "")
        put_sym = str(hedge.put_symbol or "")
        if call_sym and float(call_bid) > 0:
            est_slip_usd += await estimate_and_log_exit_spread_usd(
                symbol=call_sym,
                offer_price=float(call_bid),
                quantity=qty,
                settings=spread_settings,
                kind="hedge",
                client=client,
                log_id=hid,
            )
        if put_sym and float(put_bid) > 0:
            est_slip_usd += await estimate_and_log_exit_spread_usd(
                symbol=put_sym,
                offer_price=float(put_bid),
                quantity=qty,
                settings=spread_settings,
                kind="hedge",
                client=client,
                log_id=hid,
            )
    except Exception as exc:
        logger.warning(
            "[SPREAD_EST] hedge_id=%s exit-spread estimate failed: %s",
            hid,
            exc,
            exc_info=True,
        )
        est_slip_usd = 0.0

    mtm = compute_hedge_net_mtm_fields(
        call_bid=float(call_bid),
        put_bid=float(put_bid),
        call_entry=call_entry,
        put_entry=put_entry,
        quantity=qty,
        entry_fees=entry_fees,
        estimated_exit_fees=float(est_exit_fees),
        entry_spread_usd=entry_spread,
        hedge_est_exit_slippage_usd=float(est_slip_usd),
    )
    cum_closed = _cum_closed_basket_pnl(db, hid)
    open_basket = _open_basket_net_mtm(db, hid, position_tracker)
    structure = (
        float(mtm["hedge_net_mtm"]) + float(cum_closed) + float(open_basket)
    )

    hedge.hedge_net_mtm = float(mtm["hedge_net_mtm"])
    hedge.hedge_gross_for_sl = float(mtm["hedge_gross_for_sl"])
    hedge.cum_closed_basket_pnl = float(cum_closed)
    hedge.structure_pnl = float(structure)
    db.commit()
    db.refresh(hedge)

    details = {
        "hedge": hid,
        "hedge_net": round(float(mtm["hedge_net_mtm"]), 6),
        "hedge_gross_sl": round(float(mtm["hedge_gross_for_sl"]), 6),
        "entry_spread": round(float(entry_spread), 6),
        "cum_closed": round(float(cum_closed), 6),
        "open_basket": round(float(open_basket), 6),
        "structure": round(float(structure), 6),
        "summary": (
            f"[STRUCTURE_PNL] hedge={hid} | "
            f"hedge_net={round(float(mtm['hedge_net_mtm']), 6)} | "
            f"hedge_gross_sl={round(float(mtm['hedge_gross_for_sl']), 6)} | "
            f"entry_spread={round(float(entry_spread), 6)} | "
            f"cum_closed={round(float(cum_closed), 6)} | "
            f"open_basket={round(float(open_basket), 6)} | "
            f"structure={round(float(structure), 6)}"
        ),
    }
    _hedge_log("STRUCTURE_PNL", hid, details)
    return {
        "hedge_net_mtm": float(mtm["hedge_net_mtm"]),
        "hedge_gross_for_sl": float(mtm["hedge_gross_for_sl"]),
        "entry_spread_usd": float(entry_spread),
        "cum_closed_basket_pnl": float(cum_closed),
        "open_basket_net_mtm": float(open_basket),
        "structure_pnl": float(structure),
    }


async def evaluate_and_maybe_close_hedge(
    hedge: HedgePosition,
    db: Session,
    *,
    client: DeltaClient,
    btc_index: float,
    position_tracker: Any | None = None,
) -> HedgePosition | None:
    """
    One monitoring cycle for a single active hedge.

    Returns the closed row if a close was triggered, else None.
    Skips evaluation (no close) if either bid cannot be fetched.
    Structure P&L columns are updated every successful cycle (no new triggers).
    """
    hid = int(hedge.id)
    call_sym = str(hedge.call_symbol or "")
    put_sym = str(hedge.put_symbol or "")
    call_entry = float(hedge.call_fill_price or 0)
    put_entry = float(hedge.put_fill_price or 0)
    qty = max(1, int(hedge.quantity or 1))
    target = float(hedge.target_usd or 0)
    stoploss = float(hedge.stoploss_usd or 0)

    if call_entry <= 0 or put_entry <= 0 or not call_sym or not put_sym:
        logger.warning(
            "[HEDGE_PNL] hedge_id=%s skip — missing entry fills or symbols",
            hid,
        )
        return None

    call_bid = await _fetch_strict_bid(client, call_sym)
    put_bid = await _fetch_strict_bid(client, put_sym)
    if call_bid is None or put_bid is None or call_bid <= 0 or put_bid <= 0:
        logger.warning(
            "[HEDGE_PNL] hedge_id=%s bid fetch failed "
            "call_bid=%s put_bid=%s — skipping evaluation this cycle",
            hid,
            call_bid,
            put_bid,
        )
        _hedge_log(
            "HEDGE_PNL",
            hid,
            {
                "skipped": True,
                "reason": "bid_fetch_failed",
                "call_bid": call_bid,
                "put_bid": put_bid,
                "call_symbol": call_sym,
                "put_symbol": put_sym,
            },
        )
        return None

    entry_fees = float(hedge.call_entry_fee_usd or 0) + float(
        hedge.put_entry_fee_usd or 0
    )
    est_exit = 0.0
    btc = float(btc_index or 0)
    if btc > 0:
        est_exit += estimate_option_trading_fee(
            option_price=float(call_bid),
            quantity_lots=qty,
            btc_index_price=btc,
        )
        est_exit += estimate_option_trading_fee(
            option_price=float(put_bid),
            quantity_lots=qty,
            btc_index_price=btc,
        )

    # Calculation only — does NOT change target/SL/expiry triggers below
    try:
        await persist_structure_pnl(
            hedge,
            db,
            call_bid=float(call_bid),
            put_bid=float(put_bid),
            est_exit_fees=est_exit,
            position_tracker=position_tracker,
            client=client,
        )
    except Exception as exc:
        logger.warning(
            "[STRUCTURE_PNL] persist failed hedge_id=%s: %s",
            hid,
            exc,
            exc_info=True,
        )

    pnl = compute_long_hedge_pnl(
        call_bid=float(call_bid),
        put_bid=float(put_bid),
        call_entry=call_entry,
        put_entry=put_entry,
        quantity=qty,
        entry_fees=entry_fees,
        estimated_exit_fees=est_exit,
    )
    net = float(pnl["net"])
    gross = float(pnl["gross"])
    hours_left = (
        get_hours_to_expiry(hedge.expiry_date) if hedge.expiry_date else 0.0
    )
    # Same pre-expiry window as short basket (never allow settlement)
    will_close_expiry = bool(
        hedge.expiry_date
        and (hours_left == 0 or is_pre_expiry_window(hedge.expiry_date))
    )
    will_close_target = bool(target > 0 and net >= target)
    will_close_stop = bool(stoploss > 0 and net <= -stoploss)

    pct_to_target = (net / target * 100.0) if target > 0 else 0.0
    pct_to_stop = (-net / stoploss * 100.0) if stoploss > 0 else 0.0

    _hedge_log(
        "HEDGE_PNL",
        hid,
        {
            "call_bid": round(float(call_bid), 4),
            "put_bid": round(float(put_bid), 4),
            "call_entry": round(call_entry, 4),
            "put_entry": round(put_entry, 4),
            "gross": round(gross, 6),
            "net": round(net, 6),
            "entry_fees": round(entry_fees, 6),
            "est_exit_fees": round(est_exit, 6),
            "target_usd": target,
            "stoploss_usd": stoploss,
            "pct_to_target": round(pct_to_target, 2),
            "pct_to_stop": round(pct_to_stop, 2),
            "hours_to_expiry": round(hours_left, 4),
            "will_close_target": will_close_target,
            "will_close_stop": will_close_stop,
            "will_close_expiry": will_close_expiry,
        },
    )

    close_reason: str | None = None
    if will_close_target:
        close_reason = "HEDGE_TARGET"
    elif will_close_stop:
        close_reason = "HEDGE_STOPLOSS"
    elif will_close_expiry:
        close_reason = "HEDGE_EXPIRY"

    if close_reason is None:
        return None

    try:
        closed = await close_hedge(hid, close_reason, db, client=client)
        return closed
    except HedgeCloseError as exc:
        _hedge_log(
            "HEDGE_CLOSE_FAIL",
            hid,
            {
                "stage": f"monitor_{exc.stage}",
                "reason": str(exc.reason),
                "trigger": close_reason,
                "net": round(net, 6),
            },
            critical=True,
        )
        return None
    except Exception as exc:
        _hedge_log(
            "HEDGE_CLOSE_FAIL",
            hid,
            {
                "stage": "monitor_unexpected",
                "reason": str(exc),
                "trigger": close_reason,
            },
            critical=True,
        )
        return None


async def maybe_log_hedge_theta_snapshot(
    hedge: HedgePosition,
    db: Session,
    *,
    client: DeltaClient,
) -> HedgeThetaLog | None:
    """
    Write at most one hedge_theta_log row per hedge per IST calendar day.

    Upsert/skip via unique (hedge_id, log_date). Failures are logged and never
    raise into the open/monitor path.
    """
    hid = int(hedge.id)
    log_date = get_ist_now().date()

    existing = (
        db.query(HedgeThetaLog)
        .filter(
            HedgeThetaLog.hedge_id == hid,
            HedgeThetaLog.log_date == log_date,
        )
        .first()
    )
    if existing is not None:
        return existing

    try:
        theta = await get_hedge_theta(client, hedge)
    except HedgeThetaError as exc:
        logger.warning(
            "[HEDGE_THETA_LOG] hedge_id=%s fetch failed: %s — skip today",
            hid,
            exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "[HEDGE_THETA_LOG] hedge_id=%s unexpected fetch error: %s",
            hid,
            exc,
            exc_info=True,
        )
        return None

    call_theta = float(theta.get("call_theta") or 0)
    put_theta = float(theta.get("put_theta") or 0)
    total_theta = float(theta.get("total_theta") or 0)
    spot = float(theta.get("spot") or 0) or None
    call_iv = float(theta.get("call_iv") or 0) or None
    put_iv = float(theta.get("put_iv") or 0) or None

    row = HedgeThetaLog(
        hedge_id=hid,
        log_date=log_date,
        call_theta=call_theta,
        put_theta=put_theta,
        total_theta=total_theta,
        spot_price=spot,
        call_iv=call_iv if call_iv and call_iv > 0 else None,
        put_iv=put_iv if put_iv and put_iv > 0 else None,
        created_at=_utc_now(),
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(HedgeThetaLog)
            .filter(
                HedgeThetaLog.hedge_id == hid,
                HedgeThetaLog.log_date == log_date,
            )
            .first()
        )
        return existing
    except Exception as exc:
        db.rollback()
        logger.warning(
            "[HEDGE_THETA_LOG] hedge_id=%s commit failed: %s",
            hid,
            exc,
            exc_info=True,
        )
        return None

    _hedge_log(
        "HEDGE_THETA_LOG",
        hid,
        {
            "log_date": log_date.isoformat(),
            "total_theta": round(total_theta, 4),
            "call_theta": round(call_theta, 4),
            "put_theta": round(put_theta, 4),
            "spot": round(float(spot), 2) if spot else None,
            "call_iv": call_iv,
            "put_iv": put_iv,
        },
    )
    return row


def get_hedge_theta_log_payload(
    db: Session,
    *,
    hedge_id: int,
) -> dict[str, Any]:
    """
    Rows + accrual ESTIMATE for GET /api/hedge/{id}/theta-log.

    theta_accrued_estimate = sum(total_theta) × quantity × CONTRACT_SIZE.
    This is a sum of daily snapshots — not a measurable cash flow.
    """
    hedge = (
        db.query(HedgePosition).filter(HedgePosition.id == int(hedge_id)).first()
    )
    if hedge is None:
        raise KeyError(f"Hedge #{hedge_id} not found")

    rows = (
        db.query(HedgeThetaLog)
        .filter(HedgeThetaLog.hedge_id == int(hedge_id))
        .order_by(HedgeThetaLog.log_date.asc(), HedgeThetaLog.id.asc())
        .all()
    )
    qty = max(1, int(hedge.quantity or 1))
    sum_total = float(
        sum(float(r.total_theta or 0) for r in rows)
    )
    accrued = round(sum_total * qty * CONTRACT_SIZE, 6)
    first_date = rows[0].log_date.isoformat() if rows else None
    last_date = rows[-1].log_date.isoformat() if rows else None

    return {
        "success": True,
        "hedge_id": int(hedge.id),
        "quantity": qty,
        "days_logged": len(rows),
        "first_log_date": first_date,
        "last_log_date": last_date,
        "theta_accrued_estimate": accrued,
        "theta_accrued_is_estimate": True,
        "theta_accrued_note": (
            "ESTIMATE only — sum of daily theta snapshots × qty × contract size. "
            "Not a measurable cash flow. Real hedge P&L combines theta, vega, and delta."
        ),
        "contract_size": CONTRACT_SIZE,
        "sum_total_theta": round(sum_total, 4),
        "rows": [
            {
                "id": int(r.id),
                "hedge_id": int(r.hedge_id),
                "log_date": r.log_date.isoformat() if r.log_date else None,
                "call_theta": r.call_theta,
                "put_theta": r.put_theta,
                "total_theta": r.total_theta,
                "spot_price": r.spot_price,
                "call_iv": r.call_iv,
                "put_iv": r.put_iv,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


async def build_active_hedge_live(
    hedge: HedgePosition,
    db: Session,
    *,
    client: DeltaClient,
    btc_index: float | None = None,
) -> dict[str, Any]:
    """
    Full live panel payload for GET /api/hedge/active.

    P&L uses the SAME formula as the hedge monitor (long bids + compute_long_hedge_pnl)
    so the dashboard and [HEDGE_PNL] logs cannot disagree.
    """
    base = hedge_to_dict(hedge)
    hid = int(hedge.id)
    qty = max(1, int(hedge.quantity or 1))
    call_entry = float(hedge.call_fill_price or 0)
    put_entry = float(hedge.put_fill_price or 0)
    call_sym = str(hedge.call_symbol or "")
    put_sym = str(hedge.put_symbol or "")
    target = float(hedge.target_usd or 0)
    stoploss = float(hedge.stoploss_usd or 0)

    hours_left = (
        get_hours_to_expiry(hedge.expiry_date) if hedge.expiry_date else 0.0
    )
    days_to_expiry = round(hours_left / 24.0, 4)

    days_since_entry: float | None = None
    if hedge.entry_time is not None:
        et = hedge.entry_time
        if et.tzinfo is None:
            et = et.replace(tzinfo=timezone.utc)
        days_since_entry = round(
            max(0.0, (_utc_now() - et.astimezone(timezone.utc)).total_seconds())
            / 86400.0,
            4,
        )

    call_bid = await _fetch_strict_bid(client, call_sym) if call_sym else None
    put_bid = await _fetch_strict_bid(client, put_sym) if put_sym else None

    btc = float(btc_index or 0)
    if btc <= 0:
        try:
            btc = float(await client.get_btc_index_price())
        except Exception:
            btc = 0.0

    entry_fees = float(hedge.call_entry_fee_usd or 0) + float(
        hedge.put_entry_fee_usd or 0
    )
    est_exit = 0.0
    call_upl: float | None = None
    put_upl: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    current_value_usd: float | None = None
    cost_usd = base.get("cost_usd")

    if (
        call_bid is not None
        and put_bid is not None
        and call_bid > 0
        and put_bid > 0
        and call_entry > 0
        and put_entry > 0
    ):
        if btc > 0:
            est_exit += estimate_option_trading_fee(
                option_price=float(call_bid),
                quantity_lots=qty,
                btc_index_price=btc,
            )
            est_exit += estimate_option_trading_fee(
                option_price=float(put_bid),
                quantity_lots=qty,
                btc_index_price=btc,
            )
        pnl = compute_long_hedge_pnl(
            call_bid=float(call_bid),
            put_bid=float(put_bid),
            call_entry=call_entry,
            put_entry=put_entry,
            quantity=qty,
            entry_fees=entry_fees,
            estimated_exit_fees=est_exit,
        )
        call_upl = round(float(pnl["call_pnl"]), 6)
        put_upl = round(float(pnl["put_pnl"]), 6)
        gross_pnl = round(float(pnl["gross"]), 6)
        net_pnl = round(float(pnl["net"]), 6)
        current_value_usd = round(
            (float(call_bid) + float(put_bid)) * qty * CONTRACT_SIZE, 4
        )

    pct_to_target = (
        round((net_pnl / target) * 100.0, 2)
        if net_pnl is not None and target > 0
        else None
    )
    pct_to_stop = (
        round((-net_pnl / stoploss) * 100.0, 2)
        if net_pnl is not None and stoploss > 0
        else None
    )

    today_theta: float | None = None
    today_theta_usd: float | None = None
    current_call_iv: float | None = None
    current_put_iv: float | None = None
    try:
        theta = await get_hedge_theta(client, hedge)
        today_theta = float(theta.get("total_theta") or 0)
        today_theta_usd = float(theta.get("daily_theta_usd") or 0)
        civ = float(theta.get("call_iv") or 0)
        piv = float(theta.get("put_iv") or 0)
        current_call_iv = civ if civ > 0 else None
        current_put_iv = piv if piv > 0 else None
    except Exception as exc:
        logger.warning(
            "build_active_hedge_live theta fetch failed hedge=%s: %s",
            hid,
            exc,
        )

    accrual = {
        "days_logged": 0,
        "theta_accrued_estimate": 0.0,
        "theta_accrued_is_estimate": True,
        "theta_accrued_note": (
            "ESTIMATE only — sum of daily theta snapshots × qty × contract size. "
            "Not a measurable cash flow. Real hedge P&L combines theta, vega, and delta."
        ),
    }
    try:
        log_payload = get_hedge_theta_log_payload(db, hedge_id=hid)
        accrual = {
            "days_logged": int(log_payload.get("days_logged") or 0),
            "theta_accrued_estimate": float(
                log_payload.get("theta_accrued_estimate") or 0
            ),
            "theta_accrued_is_estimate": True,
            "theta_accrued_note": log_payload.get("theta_accrued_note"),
            "first_log_date": log_payload.get("first_log_date"),
            "last_log_date": log_payload.get("last_log_date"),
        }
    except Exception as exc:
        logger.warning(
            "build_active_hedge_live theta-log failed hedge=%s: %s",
            hid,
            exc,
        )

    return {
        **base,
        "days_to_expiry": days_to_expiry,
        "hours_to_expiry": round(hours_left, 4),
        "days_since_entry": days_since_entry,
        "call": {
            "symbol": call_sym or None,
            "entry_fill": call_entry if call_entry > 0 else None,
            "current_bid": (
                round(float(call_bid), 4)
                if call_bid is not None and call_bid > 0
                else None
            ),
            "upl": call_upl,
        },
        "put": {
            "symbol": put_sym or None,
            "entry_fill": put_entry if put_entry > 0 else None,
            "current_bid": (
                round(float(put_bid), 4)
                if put_bid is not None and put_bid > 0
                else None
            ),
            "upl": put_upl,
        },
        "cost_usd": cost_usd,
        "current_value_usd": current_value_usd,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "entry_fees_usd": round(entry_fees, 6),
        "est_exit_fees_usd": round(est_exit, 6),
        "today_theta": today_theta,
        "today_theta_usd": today_theta_usd,
        "theta_accrued_estimate": accrual["theta_accrued_estimate"],
        "theta_accrued_is_estimate": True,
        "theta_accrued_note": accrual.get("theta_accrued_note"),
        "days_logged": accrual["days_logged"],
        "target_usd": target if target > 0 else hedge.target_usd,
        "stoploss_usd": stoploss if stoploss > 0 else hedge.stoploss_usd,
        "pct_to_target": pct_to_target,
        "pct_to_stop": pct_to_stop,
        "entry_call_iv": hedge.entry_call_iv,
        "entry_put_iv": hedge.entry_put_iv,
        "current_call_iv": current_call_iv,
        "current_put_iv": current_put_iv,
        "open_basket_count": count_active_baskets_for_hedge(db, hid),
    }


async def monitor_active_hedges(
    db: Session,
    *,
    client: DeltaClient,
    btc_index: float,
    position_tracker: Any | None = None,
) -> list[HedgePosition]:
    """
    Evaluate every active hedge this cycle.

    Independent of short baskets — call even when no trade is open.
    Never gated by basket settling windows or the adjusting guard.
    """
    hedges = (
        db.query(HedgePosition)
        .filter(HedgePosition.status == "active")
        .order_by(HedgePosition.id.asc())
        .all()
    )
    closed: list[HedgePosition] = []
    for hedge in hedges:
        try:
            # Once per IST day — before close evaluation so day N is captured
            await maybe_log_hedge_theta_snapshot(hedge, db, client=client)
        except Exception as exc:
            logger.warning(
                "[HEDGE_THETA_LOG] monitor hook failed hedge_id=%s: %s",
                getattr(hedge, "id", "?"),
                exc,
                exc_info=True,
            )
        try:
            result = await evaluate_and_maybe_close_hedge(
                hedge,
                db,
                client=client,
                btc_index=btc_index,
                position_tracker=position_tracker,
            )
            if result is not None and str(result.status or "").lower() == "closed":
                closed.append(result)
        except Exception as exc:
            logger.critical(
                "[HEDGE_PNL] unexpected error hedge_id=%s: %s",
                getattr(hedge, "id", "?"),
                exc,
                exc_info=True,
            )
    return closed


def hedge_to_dict(h: HedgePosition) -> dict[str, Any]:
    """Serialize a hedge_positions row for API responses."""
    return {
        "id": int(h.id),
        "account_id": int(h.account_id),
        "underlying": h.underlying,
        "expiry_date": h.expiry_date.isoformat() if h.expiry_date else None,
        "strike": float(h.strike) if h.strike is not None else None,
        "quantity": int(h.quantity),
        "status": h.status,
        "call_product_id": h.call_product_id,
        "call_symbol": h.call_symbol,
        "call_order_id": h.call_order_id,
        "call_fill_price": h.call_fill_price,
        "call_entry_fee_usd": h.call_entry_fee_usd,
        "call_exit_price": h.call_exit_price,
        "put_product_id": h.put_product_id,
        "put_symbol": h.put_symbol,
        "put_order_id": h.put_order_id,
        "put_fill_price": h.put_fill_price,
        "put_entry_fee_usd": h.put_entry_fee_usd,
        "put_exit_price": h.put_exit_price,
        "entry_time": h.entry_time.isoformat() if h.entry_time else None,
        "exit_time": h.exit_time.isoformat() if h.exit_time else None,
        "exit_reason": h.exit_reason,
        "realized_pnl": h.realized_pnl,
        "target_usd": h.target_usd,
        "stoploss_usd": h.stoploss_usd,
        "entry_total_theta": h.entry_total_theta,
        "entry_call_iv": h.entry_call_iv,
        "entry_put_iv": h.entry_put_iv,
        "order_margin_per_lot": h.order_margin_per_lot,
        "entry_spread_usd": float(getattr(h, "entry_spread_usd", 0.0) or 0.0),
        "hedge_net_mtm": float(getattr(h, "hedge_net_mtm", 0.0) or 0.0),
        "hedge_gross_for_sl": float(getattr(h, "hedge_gross_for_sl", 0.0) or 0.0),
        "cum_closed_basket_pnl": float(
            getattr(h, "cum_closed_basket_pnl", 0.0) or 0.0
        ),
        "structure_pnl": float(getattr(h, "structure_pnl", 0.0) or 0.0),
        "is_bot_managed": bool(h.is_bot_managed),
        "last_error": h.last_error,
        "cost_usd": (
            round(
                (
                    float(h.call_fill_price or 0)
                    + float(h.put_fill_price or 0)
                )
                * int(h.quantity)
                * CONTRACT_SIZE,
                4,
            )
            if h.call_fill_price is not None and h.put_fill_price is not None
            else None
        ),
    }
