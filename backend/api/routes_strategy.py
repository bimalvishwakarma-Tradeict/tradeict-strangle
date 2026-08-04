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
    db: Session = Depends(get_db),
) -> list[ExpiryItem]:
    """Return nearest future option expiries for the underlying."""
    product_symbol = _resolve_product_underlying(underlying)
    client = _get_delta_client(db)
    try:
        try:
            rows = await client.get_available_expiries(product_symbol)
        except DeltaAPIError as exc:
            logger.error("expiries fetch failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [
            ExpiryItem(
                date=str(row["date"]),
                label=str(row["label"]),
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
) -> PayoffResponse:
    """
    Calculate short-strangle expiry payoff curve.

    Does not require Delta account — pure math from query params.
    """
    if current_price <= 0:
        raise HTTPException(status_code=400, detail="current_price must be > 0")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    # ±25% range, ~100 points
    price_min = current_price * 0.75
    price_max = current_price * 1.25
    step = current_price / 100.0
    if step <= 0:
        raise HTTPException(status_code=400, detail="invalid step size")

    premium_per_unit = call_premium + put_premium
    total_premium = premium_per_unit * quantity

    price_points: list[float] = []
    expiry_pnl: list[float] = []

    price = price_min
    # Include upper bound with small epsilon tolerance
    while price <= price_max + (step * 0.001):
        p = float(price)
        if put_strike <= p <= call_strike:
            pnl = total_premium
        elif p > call_strike:
            pnl = total_premium - (p - call_strike) * quantity
        else:
            pnl = total_premium - (put_strike - p) * quantity
        price_points.append(round(p, 4))
        expiry_pnl.append(round(float(pnl), 4))
        price += step

    return PayoffResponse(
        price_points=price_points,
        expiry_pnl=expiry_pnl,
        breakeven_upper=call_strike + premium_per_unit,
        breakeven_lower=put_strike - premium_per_unit,
    )
