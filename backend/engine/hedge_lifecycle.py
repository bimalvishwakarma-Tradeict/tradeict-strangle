# hedge_lifecycle.py — Master hedge open/close with verify + real fills

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.bot_logger import log_and_buffer
from backend.core.chain_utils import annotate_atm
from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.hedge_theta import (
    ExpiryNotAvailableError,
    HedgeThetaError,
    migrate_hedge_expiry_mode,
    resolve_hedge_expiry_date,
)
from backend.engine.order_executor import OrderExecutor
from backend.models import Account, AutoTradeSettings, HedgePosition

logger = logging.getLogger(__name__)

CONTRACT_SIZE = float(OPTIONS_CONTRACT_VALUE)
VERIFY_PAUSE_SECONDS = 0.5
UNWIND_VERIFY_ATTEMPTS = 3

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
) -> bool:
    """Close a long leg with reduce_only and verify flat. Returns True if flat."""
    executor = OrderExecutor()
    for attempt in range(1, UNWIND_VERIFY_ATTEMPTS + 1):
        try:
            await client.close_position(
                product_id=int(product_id),
                size=int(quantity),
                is_long=True,
            )
        except Exception as exc:
            logger.warning(
                "Unwind close_position attempt %s failed product=%s: %s — "
                "retrying via OrderExecutor",
                attempt,
                product_id,
                exc,
            )
            await executor.close_long_position(
                product_id=int(product_id),
                quantity=int(quantity),
                delta_client=client,
                symbol_for_fallback=symbol,
            )
        await asyncio.sleep(VERIFY_PAUSE_SECONDS)
        exists, size = await _verify_leg(
            client,
            leg="call_unwind",
            product_id=int(product_id),
            hedge_id=hedge_id,
        )
        if not exists or abs(size) < 1e-9:
            return True
        logger.warning(
            "Unwind still open after attempt %s product=%s size=%s",
            attempt,
            product_id,
            size,
        )
    return False


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
            flat = await _unwind_long(
                client,
                product_id=call_pid,
                quantity=qty,
                symbol=call_symbol,
                hedge_id=0,
            )
            err_msg = put_fail_reason
            if not flat:
                err_msg = (
                    f"{put_fail_reason}; CRITICAL: call unwind incomplete "
                    f"(product_id={call_pid}) — check Delta manually"
                )
                _hedge_log(
                    "HEDGE_OPEN_FAIL",
                    0,
                    {
                        "stage": "unwind_call",
                        "reason": "still_open",
                        "product_id": call_pid,
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
                "entry_total_theta": entry_total_theta,
                "call_size": call_size,
                "expiry": expiry.isoformat(),
                "strike": float(atm),
                "quantity": qty,
            },
        )
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


async def close_hedge(
    hedge_id: int,
    reason: str,
    db: Session,
    *,
    client: DeltaClient,
) -> HedgePosition:
    """
    Close both long straddle legs with verify-before / reduce_only / verify-after.

    Real exit fills only — never books 0.0. Unverified flat → status=exit_failed.
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
