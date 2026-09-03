# routes_strategy.py — /api/strategy/* endpoints for option chain, expiries, payoff

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.core.chain_utils import annotate_atm
from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.encryption import decrypt
from backend.database import get_db
from backend.models import Account
from backend.schemas import ExpiryItem, OptionChainResponse, PayoffResponse
from backend.strategies.s001_short_strangle.config import (
    SUPPORTED_UNDERLYINGS,
    UNDERLYING_SYMBOLS,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

NO_ACCOUNT_DETAIL = "No account connected. Please add API keys in Settings."


def _active_hedge_id(db: Session) -> int:
    """Best-effort active hedge id for THETA_FALLBACK logs (0 if none)."""
    try:
        from backend.models import HedgePosition

        row = (
            db.query(HedgePosition)
            .filter(HedgePosition.status == "active")
            .order_by(HedgePosition.id.desc())
            .first()
        )
        return int(row.id) if row is not None else 0
    except Exception:
        return 0


def _resolve_underlying_symbol(underlying: str) -> str:
    """Map UI underlying (BTC) to perpetual ticker symbol (BTCUSD)."""
    key = underlying.upper().strip()
    if key not in SUPPORTED_UNDERLYINGS and key not in UNDERLYING_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported underlying '{underlying}'. Use one of {SUPPORTED_UNDERLYINGS}",
        )
    return UNDERLYING_SYMBOLS.get(key, key)


def _resolve_product_underlying(underlying: str) -> str:
    """
    Map UI underlying to options product filter symbol.

    Delta India options use base asset (BTC/ETH/XAU), while perpetual
    tickers use BTCUSD/ETHUSD/XAUUSD from UNDERLYING_SYMBOLS.
    """
    mapped = _resolve_underlying_symbol(underlying)
    if mapped.endswith("USD") and len(mapped) > 3:
        return mapped[:-3]
    return mapped


def _get_delta_client(db: Session) -> DeltaClient:
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        raise HTTPException(status_code=401, detail=NO_ACCOUNT_DETAIL)
    api_key = decrypt(account.api_key_encrypted)
    api_secret = decrypt(account.api_secret_encrypted)
    return DeltaClient(api_key, api_secret)


@router.get("/expiries", response_model=list[ExpiryItem])
async def get_expiries(
    underlying: str = Query(..., description="BTC / ETH / XAU"),
    limit: int | None = Query(
        None,
        ge=1,
        le=90,
        description="Max expiries to return (default unchanged for Trade page)",
    ),
    db: Session = Depends(get_db),
) -> list[ExpiryItem]:
    """Return nearest future option expiries for the underlying."""
    product_symbol = _resolve_product_underlying(underlying)
    client = _get_delta_client(db)
    try:
        try:
            rows = await client.get_available_expiries(
                product_symbol, limit=limit
            )
        except DeltaAPIError as exc:
            logger.error("expiries fetch failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [
            ExpiryItem(
                date=str(row["date"]),
                label=str(row["label"]),
                key=str(row["key"]) if row.get("key") else None,
                unix_ts=int(row.get("unix_ts") or row.get("timestamp") or 0),
            )
            for row in rows
        ]
    finally:
        await client.close()


@router.get("/option-chain", response_model=OptionChainResponse)
async def get_option_chain(
    underlying: str = Query(..., description="BTC / ETH / XAU"),
    expiry: str = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
) -> OptionChainResponse:
    """Return option chain rows plus current underlying price for payoff centering."""
    product_symbol = _resolve_product_underlying(underlying)
    price_symbol = _resolve_underlying_symbol(underlying)
    client = _get_delta_client(db)
    try:
        try:
            current_price = await client.get_underlying_price(price_symbol)
            chain = await client.get_option_chain(product_symbol, expiry)
        except DeltaAPIError as exc:
            logger.error("option-chain fetch failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        annotate_atm(chain, float(current_price))
        return OptionChainResponse(current_price=current_price, chain=chain)
    finally:
        await client.close()


@router.get("/payoff", response_model=PayoffResponse)
async def get_payoff(
    call_strike: float = Query(...),
    put_strike: float = Query(...),
    call_premium: float = Query(...),
    put_premium: float = Query(...),
    quantity: float = Query(...),
    current_price: float = Query(...),
    wing_call_strike: float | None = Query(default=None),
    wing_put_strike: float | None = Query(default=None),
    wing_call_premium: float | None = Query(default=None),
    wing_put_premium: float | None = Query(default=None),
) -> PayoffResponse:
    """
    Expiry payoff curve: short strangle, or iron condor when wing strikes set.

    USD = premium-point PnL × qty × OPTIONS_CONTRACT_VALUE.
    Does not require Delta account — pure math from query params.
    """
    from backend.core.payoff import build_payoff_curve

    if current_price <= 0:
        raise HTTPException(status_code=400, detail="current_price must be > 0")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    try:
        curve = build_payoff_curve(
            current_price=float(current_price),
            short_call_strike=float(call_strike),
            short_put_strike=float(put_strike),
            short_call_premium=float(call_premium),
            short_put_premium=float(put_premium),
            quantity=int(quantity),
            wing_call_strike=wing_call_strike,
            wing_put_strike=wing_put_strike,
            wing_call_premium=wing_call_premium,
            wing_put_premium=wing_put_premium,
            range_pct=0.20,
            points=101,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PayoffResponse(
        price_points=curve["price_points"],
        expiry_pnl=curve["expiry_pnl"],
        breakeven_upper=curve["breakeven_upper"],
        breakeven_lower=curve["breakeven_lower"],
        max_profit_usd=curve["max_profit_usd"],
        max_loss_usd=curve["max_loss_usd"],
        risk_reward=curve["risk_reward"],
        wings_on=curve["wings_on"],
        net_credit_points=curve["net_credit_points"],
    )


def _iv_history_from_log(db: Session) -> list[float]:
    """Mid IV samples from hedge_theta_log for percentile (all hedges)."""
    from backend.models import HedgeThetaLog

    rows = db.query(HedgeThetaLog).order_by(HedgeThetaLog.log_date.asc()).all()
    samples: list[float] = []
    for row in rows:
        call_iv = float(getattr(row, "call_iv", None) or 0)
        put_iv = float(getattr(row, "put_iv", None) or 0)
        if call_iv > 0 and put_iv > 0:
            samples.append((call_iv + put_iv) / 2.0)
        elif call_iv > 0:
            samples.append(call_iv)
        elif put_iv > 0:
            samples.append(put_iv)
    return samples


@router.get("/hedge-preview")
async def hedge_preview(
    db: Session = Depends(get_db),
    hedge_expiry_mode: str | None = Query(None),
    hedge_expiry_date_override: str | None = Query(None),
    hedge_expiry_dte: int | None = Query(None),
    quantity: int | None = Query(None, ge=1, le=1000),
    underlying: str | None = Query(None),
    margin_buffer_pct: float | None = Query(None, ge=0, le=200),
) -> dict[str, Any]:
    """
    Hypothetical long ATM straddle from saved hedge settings.

    Optional query params override saved settings for live form previews.
    Read-only — never places orders. Errors → unavailable (no stale data).
    """
    from backend.core.hedge_theta import (
        ExpiryNotAvailableError,
        HedgeThetaError,
        compute_iv_percentile,
        get_hypothetical_hedge_theta,
        migrate_hedge_expiry_mode,
        resolve_hedge_expiry_date,
    )
    from backend.database import get_or_create_auto_settings

    settings = get_or_create_auto_settings(db)
    und = (underlying or str(settings.underlying or "BTC")).upper()
    qty = max(1, int(quantity if quantity is not None else (settings.quantity or 1)))
    raw_mode = str(
        hedge_expiry_mode
        or getattr(settings, "hedge_expiry_mode", None)
        or "month_1"
    )
    dte_override = (
        hedge_expiry_dte
        if hedge_expiry_dte is not None
        else getattr(settings, "hedge_expiry_dte", None)
    )
    mode, _ = migrate_hedge_expiry_mode(
        raw_mode,
        expiry_dte=int(dte_override) if dte_override is not None else None,
    )
    date_override = (
        hedge_expiry_date_override
        if hedge_expiry_date_override is not None
        else getattr(settings, "hedge_expiry_date_override", None)
    )
    buffer = float(
        margin_buffer_pct
        if margin_buffer_pct is not None
        else (getattr(settings, "margin_buffer_pct", None) or 50.0)
    )
    client = _get_delta_client(db)
    try:
        try:
            expiry = await resolve_hedge_expiry_date(
                client,
                und,
                expiry_mode=mode,
                expiry_date_override=date_override,
                expiry_dte=dte_override,
            )
            theta = await get_hypothetical_hedge_theta(
                client, und, expiry, qty
            )
        except ExpiryNotAvailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HedgeThetaError as exc:
            logger.warning("hedge-preview unavailable: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }
        except DeltaAPIError as exc:
            logger.warning("hedge-preview Delta error: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }

        mid_iv = (float(theta["call_iv"]) + float(theta["put_iv"])) / 2.0
        iv_pct = compute_iv_percentile(mid_iv, _iv_history_from_log(db))
        order_margin_per_lot = None
        capital_per_lot = None
        if order_margin_per_lot is not None:
            capital_per_lot = float(order_margin_per_lot) * (1.0 + buffer / 100.0)

        iv_ok = mid_iv > 0 and (
            iv_pct.get("percentile") is None or float(iv_pct["percentile"]) < 70
        )
        return {
            "success": True,
            "unavailable": False,
            "underlying": und,
            "strike": theta["strike"],
            "expiry_date": theta["expiry_date"],
            "hedge_expiry_mode": mode,
            "quantity": qty,
            "cost_usd": theta["cost_usd"],
            "daily_theta_usd": theta["daily_theta_usd"],
            "total_theta": theta["total_theta"],
            "call_theta": theta["call_theta"],
            "put_theta": theta["put_theta"],
            "call_iv": theta["call_iv"],
            "put_iv": theta["put_iv"],
            "call_mark_price": float(theta.get("call_ask") or 0),
            "put_mark_price": float(theta.get("put_ask") or 0),
            "mid_iv": round(mid_iv, 4),
            "iv_percentile": iv_pct,
            "iv_ok": iv_ok,
            "spot": theta["spot"],
            "order_margin_per_lot": order_margin_per_lot,
            "capital_per_lot": capital_per_lot,
            "margin_buffer_pct": buffer,
            "fetched_at": theta["fetched_at"],
            "source": theta["source"],
        }
    finally:
        await client.close()


@router.get("/theta-preview")
async def theta_preview(
    db: Session = Depends(get_db),
    theta_multiplier: float | None = Query(None, gt=0, le=20),
    quantity: int | None = Query(None, ge=1, le=1000),
    underlying: str | None = Query(None),
    hedge_expiry_mode: str | None = Query(None),
    hedge_expiry_date_override: str | None = Query(None),
    hedge_expiry_dte: int | None = Query(None),
    expiry_dte: int | None = Query(None, ge=0, le=90),
    expiry_date_override: str | None = Query(None),
    expiry_date: str | None = Query(
        None,
        description="Explicit short-basket expiry YYYY-MM-DD (must exist on Delta)",
    ),
) -> dict[str, Any]:
    """
    What theta_based strike selection would pick right now (no orders).

    Hedge supplies CALL-leg theta only. Short strikes come from the SHORT
    basket expiry chain: call-by-premium (floor = hedge_call_theta ×
    multiplier), put-by-premium-match.
    Works with no query params (reads saved auto-trade settings).
    """
    from datetime import date as date_cls

    from backend.core.hedge_theta import (
        CONTRACT_SIZE,
        ExpiryNotAvailableError,
        HedgeThetaError,
        assert_expiry_available,
        get_hypothetical_hedge_theta,
        resolve_hedge_expiry_date,
        resolve_short_expiry_date,
        select_theta_based_strikes,
    )
    from backend.database import get_or_create_auto_settings

    settings = get_or_create_auto_settings(db)
    und = (underlying or str(settings.underlying or "BTC")).upper()
    qty = max(1, int(quantity if quantity is not None else (settings.quantity or 1)))
    multiplier = float(
        theta_multiplier
        if theta_multiplier is not None
        else (getattr(settings, "theta_multiplier", None) or 3.0)
    )
    short_dte = int(
        expiry_dte
        if expiry_dte is not None
        else (settings.expiry_dte if settings.expiry_dte is not None else 1)
    )
    short_override = (
        expiry_date_override
        if expiry_date_override is not None
        else getattr(settings, "expiry_date_override", None)
    )

    client = _get_delta_client(db)
    try:
        try:
            hedge_exp = await resolve_hedge_expiry_date(
                client,
                und,
                expiry_mode=str(
                    hedge_expiry_mode
                    or getattr(settings, "hedge_expiry_mode", None)
                    or "month_1"
                ),
                expiry_date_override=(
                    hedge_expiry_date_override
                    if hedge_expiry_date_override is not None
                    else getattr(settings, "hedge_expiry_date_override", None)
                ),
                expiry_dte=(
                    hedge_expiry_dte
                    if hedge_expiry_dte is not None
                    else getattr(settings, "hedge_expiry_dte", None)
                ),
            )
            hedge = await get_hypothetical_hedge_theta(
                client, und, hedge_exp, qty
            )

            # Explicit ?expiry_date=… always wins and must exist on Delta
            if expiry_date is not None:
                try:
                    short_exp = date_cls.fromisoformat(
                        str(expiry_date).strip()[:10]
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid expiry date: {expiry_date}",
                    ) from exc
            else:
                short_exp = await resolve_short_expiry_date(
                    expiry_dte=short_dte,
                    expiry_date_override=short_override,
                )

            await assert_expiry_available(client, und, short_exp)

            product_u = _resolve_product_underlying(und)
            price_symbol = _resolve_underlying_symbol(und)
            spot = float(await client.get_underlying_price(price_symbol))
            short_chain = await client.get_option_chain(
                product_u, short_exp.isoformat()
            )
            if not short_chain:
                raise HedgeThetaError(
                    f"Empty option chain for short expiry {short_exp.isoformat()}"
                )
        except ExpiryNotAvailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except HedgeThetaError as exc:
            logger.warning("theta-preview unavailable: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }
        except DeltaAPIError as exc:
            logger.warning("theta-preview Delta error: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }

        hedge_call_theta = abs(float(hedge["call_theta"]))
        hedge_total = float(hedge["total_theta"])
        required = hedge_call_theta * multiplier
        if required <= 0:
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": "hedge call_theta is zero",
            }

        try:
            picks = select_theta_based_strikes(
                short_chain,
                spot,
                required,
                hedge_call_theta=hedge_call_theta,
                theta_multiplier=multiplier,
                log_hedge_id=_active_hedge_id(db),
            )
        except HedgeThetaError as exc:
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }

        combined = float(picks["combined_theta"])
        coverage = (combined / hedge_total) if hedge_total > 0 else 0.0
        short_expiry_str = short_exp.isoformat()

        logger.info(
            "[STRIKE_SELECT_PREMIUM] hedge_call_theta=%.4f theta_multiplier=%.4f "
            "required_call_premium=%.4f short_expiry=%s spot=%.2f "
            "call=%s put=%s coverage=%.2f hedge_total_theta=%.4f "
            "premium_fallback_used=%s strikes_above_selected=%s "
            "premium_margin_pct=%.2f",
            hedge_call_theta,
            multiplier,
            required,
            short_expiry_str,
            spot,
            picks["call"],
            picks["put"],
            coverage,
            hedge_total,
            picks.get("premium_fallback_used"),
            picks.get("strikes_above_selected") or 0,
            picks.get("premium_margin_pct") or 0,
        )

        return {
            "success": True,
            "unavailable": False,
            "hedge_call_theta": round(hedge_call_theta, 4),
            "hedge_total_theta": round(hedge_total, 4),
            "theta_multiplier": multiplier,
            "multiplier": multiplier,
            "required_theta": round(required, 4),
            "required_call_premium": round(required, 4),
            "selected_call_premium": picks.get("selected_call_premium"),
            "premium_margin_pct": picks.get("premium_margin_pct"),
            "strikes_above_selected": picks.get("strikes_above_selected"),
            "max_available_theta": picks.get("max_available_theta"),
            "fallback_used": bool(picks.get("fallback_used")),
            "premium_fallback_used": bool(picks.get("premium_fallback_used")),
            "max_usable_multiplier": picks.get("max_usable_multiplier"),
            "short_expiry": short_expiry_str,
            "short_expiry_date": short_expiry_str,
            "hedge_expiry_date": hedge["expiry_date"],
            "call": picks["call"],
            "put": picks["put"],
            "coverage": round(coverage, 2),
            "spot": spot,
            "contract_size": CONTRACT_SIZE,
            "fetched_at": hedge["fetched_at"],
        }
    finally:
        await client.close()


@router.get("/target-preview")
async def target_preview(
    db: Session = Depends(get_db),
    target_theta_pct: float | None = Query(None, ge=10, le=1000),
    theta_multiplier: float | None = Query(None, gt=0, le=20),
    quantity: int | None = Query(None, ge=1, le=1000),
    underlying: str | None = Query(None),
    hedge_expiry_mode: str | None = Query(None),
    hedge_expiry_date_override: str | None = Query(None),
    hedge_expiry_dte: int | None = Query(None),
    expiry_dte: int | None = Query(None, ge=0, le=90),
    expiry_date_override: str | None = Query(None),
    expiry_date: str | None = Query(
        None,
        description="Explicit short-basket expiry YYYY-MM-DD (must exist on Delta)",
    ),
) -> dict[str, Any]:
    """
    Theta-multiplier target vs max profit of the SAME strikes as theta-preview.

    Strike selection uses hedge CALL theta × multiplier as a premium floor
    (shared with theta-preview). Target USD uses hedge TOTAL theta (both legs).
    """
    from datetime import date as date_cls

    from backend.core.hedge_theta import (
        CONTRACT_SIZE,
        ExpiryNotAvailableError,
        HedgeThetaError,
        assert_expiry_available,
        get_hypothetical_hedge_theta,
        resolve_hedge_expiry_date,
        resolve_short_expiry_date,
        select_theta_based_strikes,
    )
    from backend.database import get_or_create_auto_settings

    settings = get_or_create_auto_settings(db)
    und = (underlying or str(settings.underlying or "BTC")).upper()
    qty = max(1, int(quantity if quantity is not None else (settings.quantity or 1)))
    tgt_pct = float(
        target_theta_pct
        if target_theta_pct is not None
        else (getattr(settings, "target_theta_pct", None) or 150.0)
    )
    multiplier = float(
        theta_multiplier
        if theta_multiplier is not None
        else (getattr(settings, "theta_multiplier", None) or 3.0)
    )
    short_dte = int(
        expiry_dte
        if expiry_dte is not None
        else (settings.expiry_dte if settings.expiry_dte is not None else 1)
    )
    short_override = (
        expiry_date_override
        if expiry_date_override is not None
        else getattr(settings, "expiry_date_override", None)
    )

    client = _get_delta_client(db)
    try:
        try:
            hedge_exp = await resolve_hedge_expiry_date(
                client,
                und,
                expiry_mode=str(
                    hedge_expiry_mode
                    or getattr(settings, "hedge_expiry_mode", None)
                    or "month_1"
                ),
                expiry_date_override=(
                    hedge_expiry_date_override
                    if hedge_expiry_date_override is not None
                    else getattr(settings, "hedge_expiry_date_override", None)
                ),
                expiry_dte=(
                    hedge_expiry_dte
                    if hedge_expiry_dte is not None
                    else getattr(settings, "hedge_expiry_dte", None)
                ),
            )
            hedge = await get_hypothetical_hedge_theta(
                client, und, hedge_exp, qty
            )

            if expiry_date is not None:
                try:
                    short_exp = date_cls.fromisoformat(
                        str(expiry_date).strip()[:10]
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid expiry date: {expiry_date}",
                    ) from exc
            else:
                short_exp = await resolve_short_expiry_date(
                    expiry_dte=short_dte,
                    expiry_date_override=short_override,
                )

            await assert_expiry_available(client, und, short_exp)

            product_u = _resolve_product_underlying(und)
            price_symbol = _resolve_underlying_symbol(und)
            spot = float(await client.get_underlying_price(price_symbol))
            short_chain = await client.get_option_chain(
                product_u, short_exp.isoformat()
            )
            if not short_chain:
                raise HedgeThetaError(
                    f"Empty option chain for short expiry {short_exp.isoformat()}"
                )
        except ExpiryNotAvailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except HedgeThetaError as exc:
            logger.warning("target-preview unavailable: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }
        except DeltaAPIError as exc:
            logger.warning("target-preview Delta error: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }

        # Same selection input as theta-preview: CALL leg theta × multiplier
        # applied as required_call_premium
        hedge_call_theta = abs(float(hedge["call_theta"]))
        required = hedge_call_theta * multiplier
        if required <= 0:
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": "hedge call_theta is zero",
            }

        try:
            picks = select_theta_based_strikes(
                short_chain,
                spot,
                required,
                hedge_call_theta=hedge_call_theta,
                theta_multiplier=multiplier,
                log_hedge_id=_active_hedge_id(db),
            )
        except HedgeThetaError as exc:
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }

        # Target covers FULL hedge daily cost (both legs)
        total_theta = float(hedge["total_theta"])
        call_premium = float(picks["call"]["premium"])
        put_premium = float(picks["put"]["premium"])
        call_strike = float(picks["call"]["strike"])
        put_strike = float(picks["put"]["strike"])
        short_expiry_str = short_exp.isoformat()

        target_usd = total_theta * (tgt_pct / 100.0) * qty * CONTRACT_SIZE
        max_profit_usd = (call_premium + put_premium) * qty * CONTRACT_SIZE
        pct_of_max = (
            (target_usd / max_profit_usd * 100.0) if max_profit_usd > 0 else 0.0
        )
        if pct_of_max <= 60:
            reachability = "reachable"
            band_label = "reachable"
        elif pct_of_max <= 80:
            reachability = "tight"
            band_label = "tight — may be hard to hit"
        else:
            reachability = "rarely_reached"
            band_label = (
                "rarely reached - lower the target or raise the strike multiplier"
            )

        logger.info(
            "[TARGET_THETA] hedge_total_theta=%.4f target_theta_pct=%.2f "
            "quantity=%s contract_size=%s target_usd=%.4f max_profit_usd=%.4f "
            "pct_of_max=%.1f reachability=%s short_expiry=%s "
            "call_strike=%s call_premium=%.2f put_strike=%s put_premium=%.2f "
            "required_call_premium=%.4f premium_fallback_used=%s "
            "strikes_above_selected=%s",
            total_theta,
            tgt_pct,
            qty,
            CONTRACT_SIZE,
            target_usd,
            max_profit_usd,
            pct_of_max,
            reachability,
            short_expiry_str,
            call_strike,
            call_premium,
            put_strike,
            put_premium,
            required,
            picks.get("premium_fallback_used"),
            picks.get("strikes_above_selected") or 0,
        )

        return {
            "success": True,
            "unavailable": False,
            "hedge_total_theta": round(total_theta, 4),
            "hedge_call_theta": round(hedge_call_theta, 4),
            "target_theta_pct": tgt_pct,
            "quantity": qty,
            "contract_size": CONTRACT_SIZE,
            "target_usd": round(target_usd, 4),
            "max_profit_usd": round(max_profit_usd, 4),
            "pct_of_max": round(pct_of_max, 1),
            "reachability": reachability,
            "band": reachability,
            "band_label": band_label,
            "required_theta": round(required, 4),
            "required_call_premium": round(required, 4),
            "selected_call_premium": picks.get("selected_call_premium"),
            "premium_margin_pct": picks.get("premium_margin_pct"),
            "strikes_above_selected": picks.get("strikes_above_selected"),
            "max_available_theta": picks.get("max_available_theta"),
            "fallback_used": bool(picks.get("fallback_used")),
            "premium_fallback_used": bool(picks.get("premium_fallback_used")),
            "max_usable_multiplier": picks.get("max_usable_multiplier"),
            "short_expiry": short_expiry_str,
            "call_strike": call_strike,
            "call_premium": round(call_premium, 2),
            "put_strike": put_strike,
            "put_premium": round(put_premium, 2),
            "call": picks["call"],
            "put": picks["put"],
            "theta_multiplier": multiplier,
            "spot": spot,
            "fetched_at": hedge["fetched_at"],
        }
    finally:
        await client.close()


def _pick_short_legs_for_wing_preview(
    chain: list[dict[str, Any]],
    spot: float,
    *,
    trade_type: str,
    target_premium: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """ATM straddle or nearest-premium strangle from a live chain (preview only)."""
    atm = annotate_atm(chain, float(spot))
    if atm is None:
        raise ValueError("Could not resolve ATM strike from chain")

    trade = str(trade_type or "straddle").lower().strip()
    if trade != "strangle":
        row = next(
            (r for r in chain if float(r.get("strike") or 0) == float(atm)),
            None,
        )
        if row is None:
            raise ValueError(f"ATM strike {atm} missing from chain")
        return (
            {
                "strike": float(row["strike"]),
                "premium": float(row.get("call_mark_price") or 0),
                "symbol": str(row.get("call_symbol") or ""),
                "product_id": int(row.get("call_product_id") or 0),
                "delta": float(row.get("call_delta") or 0),
            },
            {
                "strike": float(row["strike"]),
                "premium": float(row.get("put_mark_price") or 0),
                "symbol": str(row.get("put_symbol") or ""),
                "product_id": int(row.get("put_product_id") or 0),
                "delta": float(row.get("put_delta") or 0),
            },
        )

    target = max(1.0, float(target_premium or 150.0))
    best_call: dict[str, Any] | None = None
    best_put: dict[str, Any] | None = None
    best_call_diff = float("inf")
    best_put_diff = float("inf")
    for row in chain:
        strike = float(row.get("strike") or 0)
        call_px = float(row.get("call_mark_price") or 0)
        put_px = float(row.get("put_mark_price") or 0)
        if strike >= float(spot) and call_px > 0:
            diff = abs(call_px - target)
            if diff < best_call_diff:
                best_call_diff = diff
                best_call = {
                    "strike": strike,
                    "premium": call_px,
                    "symbol": str(row.get("call_symbol") or ""),
                    "product_id": int(row.get("call_product_id") or 0),
                    "delta": float(row.get("call_delta") or 0),
                }
        if strike <= float(spot) and put_px > 0:
            diff = abs(put_px - target)
            if diff < best_put_diff:
                best_put_diff = diff
                best_put = {
                    "strike": strike,
                    "premium": put_px,
                    "symbol": str(row.get("put_symbol") or ""),
                    "product_id": int(row.get("put_product_id") or 0),
                    "delta": float(row.get("put_delta") or 0),
                }
    if best_call is None or best_put is None:
        raise ValueError("Could not resolve strangle shorts near target premium")
    return best_call, best_put


async def _hedge_marks_for_strangle_premium(
    db: Session,
    client: DeltaClient,
    settings: Any,
    underlying: str,
    *,
    quantity: int,
    hedge_expiry_mode: str | None = None,
    hedge_expiry_date_override: str | None = None,
    hedge_expiry_dte: int | None = None,
) -> tuple[float | None, float | None, str | None]:
    """
    Same mark sources as Trade Setup / entry strangle premium.

    1) Active hedge via get_hedge_theta (entry path)
    2) Else hypothetical hedge-preview marks

    Returns (call_mark, put_mark, source) where source is
    'active_hedge' | 'hedge_preview' | None.
    """
    from backend.core.hedge_theta import (
        get_hedge_theta,
        get_hypothetical_hedge_theta,
        resolve_hedge_expiry_date,
    )
    from backend.engine.hedge_lifecycle import get_active_hedge

    und = str(underlying or "BTC").upper()
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is not None:
        hedge = get_active_hedge(
            db,
            account_id=int(account.id),
            underlying=und,
        )
        if hedge is not None:
            try:
                theta = await get_hedge_theta(client, hedge)
                call_m = float(theta.get("call_ask") or 0)
                put_m = float(theta.get("put_ask") or 0)
                if call_m > 0 and put_m > 0:
                    return call_m, put_m, "active_hedge"
            except Exception as exc:
                logger.warning(
                    "wing-preview: active hedge marks failed: %s",
                    exc,
                )

    try:
        hedge_exp = await resolve_hedge_expiry_date(
            client,
            und,
            expiry_mode=str(
                hedge_expiry_mode
                or getattr(settings, "hedge_expiry_mode", None)
                or "month_1"
            ),
            expiry_date_override=(
                hedge_expiry_date_override
                if hedge_expiry_date_override is not None
                else getattr(settings, "hedge_expiry_date_override", None)
            ),
            expiry_dte=(
                hedge_expiry_dte
                if hedge_expiry_dte is not None
                else getattr(settings, "hedge_expiry_dte", None)
            ),
        )
        hypo = await get_hypothetical_hedge_theta(
            client, und, hedge_exp, max(1, int(quantity))
        )
        call_m = float(hypo.get("call_ask") or 0)
        put_m = float(hypo.get("put_ask") or 0)
        if call_m > 0 and put_m > 0:
            return call_m, put_m, "hedge_preview"
    except Exception as exc:
        logger.warning(
            "wing-preview: hedge-preview marks failed: %s",
            exc,
        )

    return None, None, None


@router.get("/wing-preview")
async def wing_preview(
    db: Session = Depends(get_db),
    underlying: str | None = Query(None),
    quantity: int | None = Query(None, ge=1, le=1000),
    expiry_dte: int | None = Query(None, ge=0, le=90),
    expiry_date_override: str | None = Query(None),
    expiry_date: str | None = Query(None),
    trade_type: str | None = Query(None),
    target_premium_per_side: float | None = Query(None, gt=0),
    wing_strike_mode: str | None = Query(None),
    wing_points_away: float | None = Query(None, gt=0),
    wing_delta_min: float | None = Query(None, gt=0, lt=1),
    wing_delta_max: float | None = Query(None, gt=0, lt=1),
    wing_pct_of_premium: float | None = Query(None, gt=0, lt=100),
    strike_selection_mode: str | None = Query(None),
    theta_multiplier: float | None = Query(None, gt=0, le=20),
    hedge_expiry_mode: str | None = Query(None),
    hedge_expiry_date_override: str | None = Query(None),
    hedge_expiry_dte: int | None = Query(None),
) -> dict[str, Any]:
    """
    Live wing strike preview for basket condor settings.

    Read-only — never places orders. informational only (no minimum credit rule).
    """
    from datetime import date as date_cls

    from backend.config import OPTIONS_CONTRACT_VALUE
    from backend.core.fees import estimate_option_trading_fee
    from backend.core.hedge_theta import (
        ExpiryNotAvailableError,
        HedgeThetaError,
        assert_expiry_available,
        get_hypothetical_hedge_theta,
        resolve_hedge_expiry_date,
        resolve_short_expiry_date,
        select_theta_based_strikes,
    )
    from backend.database import get_or_create_auto_settings
    from backend.strategies.s001_short_strangle.wing_select import (
        normalize_wing_mode,
        resolve_wing_strikes,
    )

    settings = get_or_create_auto_settings(db)
    und = (underlying or str(settings.underlying or "BTC")).upper()
    qty = max(1, int(quantity if quantity is not None else (settings.quantity or 1)))
    mode = normalize_wing_mode(
        wing_strike_mode
        if wing_strike_mode is not None
        else getattr(settings, "wing_strike_mode", None)
    )
    points_away = float(
        wing_points_away
        if wing_points_away is not None
        else (getattr(settings, "wing_points_away", None) or 2000.0)
    )
    d_min = float(
        wing_delta_min
        if wing_delta_min is not None
        else (getattr(settings, "wing_delta_min", None) or 0.05)
    )
    d_max = float(
        wing_delta_max
        if wing_delta_max is not None
        else (getattr(settings, "wing_delta_max", None) or 0.07)
    )
    if d_max < d_min:
        d_min, d_max = d_max, d_min
    pct = float(
        wing_pct_of_premium
        if wing_pct_of_premium is not None
        else (getattr(settings, "wing_pct_of_premium", None) or 20.0)
    )
    trade = str(
        trade_type
        if trade_type is not None
        else (getattr(settings, "trade_type", None) or "straddle")
    ).lower().strip()
    # Resolve short target via the SAME helper as entry (never raw setting alone).
    # Query target_premium_per_side only overlays the fixed fallback value.
    from types import SimpleNamespace

    from backend.engine.auto_trade_engine import resolve_strangle_target_premium

    prem_settings = SimpleNamespace(
        target_premium_per_side=float(
            target_premium_per_side
            if target_premium_per_side is not None
            else (getattr(settings, "target_premium_per_side", None) or 150.0)
        ),
        strangle_premium_mode=str(
            getattr(settings, "strangle_premium_mode", None) or "fixed"
        )
        .lower()
        .strip(),
        strangle_premium_pct_of_hedge=float(
            getattr(settings, "strangle_premium_pct_of_hedge", None) or 3.0
        ),
        hedge_enabled=bool(getattr(settings, "hedge_enabled", False)),
    )
    sel_mode = str(
        strike_selection_mode
        if strike_selection_mode is not None
        else (getattr(settings, "strike_selection_mode", None) or "fixed_premium")
    ).lower().strip()
    multiplier = float(
        theta_multiplier
        if theta_multiplier is not None
        else (getattr(settings, "theta_multiplier", None) or 3.0)
    )
    short_dte = int(
        expiry_dte
        if expiry_dte is not None
        else (settings.expiry_dte if settings.expiry_dte is not None else 1)
    )
    short_override = (
        expiry_date_override
        if expiry_date_override is not None
        else getattr(settings, "expiry_date_override", None)
    )

    client = _get_delta_client(db)
    try:
        hedge_call_mark, hedge_put_mark, marks_source = (
            await _hedge_marks_for_strangle_premium(
                db,
                client,
                settings,
                und,
                quantity=qty,
                hedge_expiry_mode=hedge_expiry_mode,
                hedge_expiry_date_override=hedge_expiry_date_override,
                hedge_expiry_dte=hedge_expiry_dte,
            )
        )
        target_prem, used_dynamic = resolve_strangle_target_premium(
            settings=prem_settings,
            hedge_call_mark=hedge_call_mark,
            hedge_put_mark=hedge_put_mark,
        )
        prem_mode = str(prem_settings.strangle_premium_mode or "fixed")
        premium_fallback = prem_mode == "pct_of_hedge" and not used_dynamic
        fixed_fallback = float(prem_settings.target_premium_per_side)
        if used_dynamic:
            pct_label = float(prem_settings.strangle_premium_pct_of_hedge)
            short_target_label = (
                f"Short target: ${int(target_prem)}/side "
                f"({pct_label:g}% of hedge)"
            )
        elif premium_fallback:
            short_target_label = (
                f"⚠ Hedge marks unavailable — falling back to fixed "
                f"${fixed_fallback:g}"
            )
        else:
            short_target_label = (
                f"Short target: ${fixed_fallback:g}/side (fixed)"
            )

        try:
            if expiry_date is not None:
                try:
                    short_exp = date_cls.fromisoformat(
                        str(expiry_date).strip()[:10]
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid expiry date: {expiry_date}",
                    ) from exc
            else:
                short_exp = await resolve_short_expiry_date(
                    expiry_dte=short_dte,
                    expiry_date_override=short_override,
                )
            await assert_expiry_available(client, und, short_exp)

            product_u = _resolve_product_underlying(und)
            price_symbol = _resolve_underlying_symbol(und)
            spot = float(await client.get_underlying_price(price_symbol))
            short_chain = await client.get_option_chain(
                product_u, short_exp.isoformat()
            )
            if not short_chain:
                raise HedgeThetaError(
                    f"Empty option chain for short expiry {short_exp.isoformat()}"
                )

            short_call: dict[str, Any]
            short_put: dict[str, Any]
            if sel_mode == "theta_based" and bool(
                getattr(settings, "hedge_enabled", False)
            ):
                hedge_exp = await resolve_hedge_expiry_date(
                    client,
                    und,
                    expiry_mode=str(
                        hedge_expiry_mode
                        or getattr(settings, "hedge_expiry_mode", None)
                        or "month_1"
                    ),
                    expiry_date_override=(
                        hedge_expiry_date_override
                        if hedge_expiry_date_override is not None
                        else getattr(settings, "hedge_expiry_date_override", None)
                    ),
                    expiry_dte=(
                        hedge_expiry_dte
                        if hedge_expiry_dte is not None
                        else getattr(settings, "hedge_expiry_dte", None)
                    ),
                )
                hedge = await get_hypothetical_hedge_theta(
                    client, und, hedge_exp, qty
                )
                required = abs(float(hedge["call_theta"])) * multiplier
                picks = select_theta_based_strikes(
                    short_chain,
                    spot,
                    required,
                    hedge_call_theta=abs(float(hedge["call_theta"])),
                    theta_multiplier=multiplier,
                    log_hedge_id=_active_hedge_id(db),
                )
                short_call = {
                    "strike": float(picks["call"]["strike"]),
                    "premium": float(picks["call"]["premium"]),
                    "symbol": str(picks["call"].get("symbol") or ""),
                    "product_id": int(picks["call"].get("product_id") or 0),
                    "delta": float(picks["call"].get("delta") or 0),
                }
                short_put = {
                    "strike": float(picks["put"]["strike"]),
                    "premium": float(picks["put"]["premium"]),
                    "symbol": str(picks["put"].get("symbol") or ""),
                    "product_id": int(picks["put"].get("product_id") or 0),
                    "delta": float(picks["put"].get("delta") or 0),
                }
            else:
                short_call, short_put = _pick_short_legs_for_wing_preview(
                    short_chain,
                    spot,
                    trade_type=trade,
                    target_premium=target_prem,
                )
        except ExpiryNotAvailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException:
            raise
        except (HedgeThetaError, ValueError) as exc:
            logger.warning("wing-preview unavailable: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }
        except DeltaAPIError as exc:
            logger.warning("wing-preview Delta error: %s", exc)
            return {
                "success": False,
                "unavailable": True,
                "message": "unavailable - chain fetch failed",
                "detail": str(exc),
            }

        wing_call, wing_put = resolve_wing_strikes(
            chain=short_chain,
            short_call_strike=float(short_call["strike"]),
            short_put_strike=float(short_put["strike"]),
            short_call_premium=float(short_call["premium"]),
            short_put_premium=float(short_put["premium"]),
            mode=mode,
            points_away=points_away,
            delta_min=d_min,
            delta_max=d_max,
            pct_of_premium=pct,
        )

        cv = float(OPTIONS_CONTRACT_VALUE)
        short_credit_pts = float(short_call["premium"]) + float(short_put["premium"])
        wing_debit_pts = 0.0
        if wing_call is not None:
            wing_debit_pts += float(wing_call["premium"])
        if wing_put is not None:
            wing_debit_pts += float(wing_put["premium"])
        net_credit_pts = short_credit_pts - wing_debit_pts
        net_credit_usd = net_credit_pts * cv

        fee_total = 0.0
        for prem in (
            float(short_call["premium"]),
            float(short_put["premium"]),
            float(wing_call["premium"]) if wing_call else 0.0,
            float(wing_put["premium"]) if wing_put else 0.0,
        ):
            if prem <= 0:
                continue
            # Entry + exit estimate per leg
            one = estimate_option_trading_fee(
                option_price=prem,
                quantity_lots=qty,
                btc_index_price=spot,
            )
            fee_total += 2.0 * one

        fee_per_lot = fee_total / float(qty) if qty > 0 else fee_total
        net_after = net_credit_usd - fee_per_lot
        consumed_pct = (
            (fee_per_lot / net_credit_usd * 100.0) if net_credit_usd > 0 else 0.0
        )

        def _gap(short_k: float, wing: dict[str, Any] | None, leg: str) -> float | None:
            if wing is None:
                return None
            if leg == "call":
                return float(wing["strike"]) - float(short_k)
            return float(short_k) - float(wing["strike"])

        return {
            "success": True,
            "unavailable": False,
            "underlying": und,
            "spot": spot,
            "short_expiry": short_exp.isoformat(),
            "quantity": qty,
            "wing_strike_mode": mode,
            "short_call": short_call,
            "short_put": short_put,
            "wing_call": wing_call,
            "wing_put": wing_put,
            "call_gap_points": _gap(float(short_call["strike"]), wing_call, "call"),
            "put_gap_points": _gap(float(short_put["strike"]), wing_put, "put"),
            "short_credit_pts": round(short_credit_pts, 4),
            "wing_debit_pts": round(wing_debit_pts, 4),
            "net_credit_pts": round(net_credit_pts, 4),
            "net_credit_usd_per_lot": round(net_credit_usd, 6),
            "est_round_trip_cost_usd_per_lot": round(fee_per_lot, 6),
            "net_credit_after_cost_usd_per_lot": round(net_after, 6),
            "cost_consumed_pct": round(consumed_pct, 2),
            "contract_value": cv,
            # Same-source short target as entry (resolve_strangle_target_premium)
            "short_target_premium": float(target_prem),
            "short_target_used_dynamic": bool(used_dynamic),
            "short_target_premium_fallback": bool(premium_fallback),
            "short_target_label": short_target_label,
            "short_target_mode": prem_mode,
            "hedge_marks_source": marks_source,
            "hedge_call_mark": (
                float(hedge_call_mark) if hedge_call_mark else None
            ),
            "hedge_put_mark": (
                float(hedge_put_mark) if hedge_put_mark else None
            ),
        }
    finally:
        await client.close()
