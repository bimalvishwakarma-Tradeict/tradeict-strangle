# hedge_lifecycle.py — Master hedge open/close (open only in this step)

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.config import OPTIONS_CONTRACT_VALUE
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


class HedgeOpenError(Exception):
    """Hedge open failed after cleanup — caller should surface to user."""

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
) -> tuple[bool, float]:
    exists = await client.verify_position_exists(int(product_id))
    size = await _position_size(client, int(product_id)) if exists else 0.0
    logger.info(
        "[HEDGE_VERIFY] leg=%s product_id=%s exists=%s size=%s",
        leg,
        product_id,
        exists,
        size,
    )
    return bool(exists), float(size)


async def _unwind_long(
    client: DeltaClient,
    *,
    product_id: int,
    quantity: int,
    symbol: str,
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
            client, leg="call_unwind", product_id=int(product_id)
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
        logger.info(
            "[HEDGE_OPEN_START] skipped — active hedge_id=%s underlying=%s",
            existing.id,
            und,
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

        logger.info(
            "[HEDGE_OPEN_START] underlying=%s expiry=%s strike=%s quantity=%s spot=%.2f "
            "mode=%s",
            und,
            expiry.isoformat(),
            atm,
            qty,
            spot,
            mode,
        )

        # --- BUY CALL ---
        call_result = await executor.buy_option(
            product_id=call_pid,
            quantity=qty,
            delta_client=client,
            symbol_for_fallback=call_symbol,
        )
        if not call_result.success:
            raise HedgeOpenError(
                "buy_call",
                call_result.error or "Call buy order failed",
            )

        await asyncio.sleep(VERIFY_PAUSE_SECONDS)
        call_ok, call_size = await _verify_leg(
            client, leg="call", product_id=call_pid
        )
        if not call_ok:
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
                client, leg="put", product_id=put_pid
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
            logger.critical(
                "[HEDGE_OPEN_FAIL] stage=buy_put reason=%s — unwinding call",
                put_fail_reason,
            )
            flat = await _unwind_long(
                client,
                product_id=call_pid,
                quantity=qty,
                symbol=call_symbol,
            )
            err_msg = put_fail_reason
            if not flat:
                err_msg = (
                    f"{put_fail_reason}; CRITICAL: call unwind incomplete "
                    f"(product_id={call_pid}) — check Delta manually"
                )
                logger.critical(
                    "[HEDGE_OPEN_FAIL] stage=unwind_call reason=still_open "
                    "product_id=%s",
                    call_pid,
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

        logger.info(
            "[HEDGE_OPEN_DONE] hedge_id=%s call_fill=%.4f put_fill=%.4f "
            "cost_usd=%.4f entry_total_theta=%.4f call_size=%s",
            hedge_row.id,
            call_fill,
            put_fill,
            cost_usd,
            entry_total_theta,
            call_size,
        )
        return hedge_row

    except HedgeOpenError:
        raise
    except Exception as exc:
        logger.critical(
            "[HEDGE_OPEN_FAIL] stage=unexpected reason=%s",
            exc,
            exc_info=True,
        )
        raise HedgeOpenError("unexpected", str(exc), hedge=hedge_row) from exc


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
        "put_product_id": h.put_product_id,
        "put_symbol": h.put_symbol,
        "put_order_id": h.put_order_id,
        "put_fill_price": h.put_fill_price,
        "put_entry_fee_usd": h.put_entry_fee_usd,
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
