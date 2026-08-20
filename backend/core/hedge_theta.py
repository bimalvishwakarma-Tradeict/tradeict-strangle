# hedge_theta.py — Live / hypothetical hedge theta reader (read-only, no trading)

from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, timezone
from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.chain_utils import annotate_atm
from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.time_utils import get_expiry_date_for_dte, get_ist_now

logger = logging.getLogger(__name__)

CONTRACT_SIZE = float(OPTIONS_CONTRACT_VALUE)


class HedgeThetaError(Exception):
    """Chain fetch or strike resolution failed — UI must show unavailable."""


class ExpiryNotAvailableError(Exception):
    """Requested expiry does not exist on Delta — map to HTTP 400."""


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value).strip()[:10])


def _last_friday_of_month(year: int, month: int) -> date:
    last_day = date(year, month, monthrange(year, month)[1])
    offset = (last_day.weekday() - 4) % 7
    return last_day.fromordinal(last_day.toordinal() - offset)


def _is_monthly_expiry(d: date) -> bool:
    return d.weekday() == 4 and d == _last_friday_of_month(d.year, d.month)


def _pack_theta_result(
    *,
    strike: float,
    expiry_date: date,
    call_theta: float,
    put_theta: float,
    call_ask: float,
    put_ask: float,
    call_iv: float,
    put_iv: float,
    spot: float,
    quantity: int,
    source: str,
) -> dict[str, Any]:
    total_theta = abs(float(call_theta)) + abs(float(put_theta))
    qty = max(1, int(quantity))
    cost_usd = (float(call_ask) + float(put_ask)) * qty * CONTRACT_SIZE
    daily_theta_usd = total_theta * qty * CONTRACT_SIZE
    result = {
        "strike": float(strike),
        "expiry_date": expiry_date.isoformat(),
        "call_theta": float(call_theta),
        "put_theta": float(put_theta),
        "total_theta": float(total_theta),
        "call_ask": float(call_ask),
        "put_ask": float(put_ask),
        "call_iv": float(call_iv),
        "put_iv": float(put_iv),
        "spot": float(spot),
        "cost_usd": round(cost_usd, 4),
        "daily_theta_usd": round(daily_theta_usd, 4),
        "quantity": qty,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "[HEDGE_THETA] strike=%s call_theta=%.4f put_theta=%.4f "
        "total_theta=%.4f source=%s",
        strike,
        call_theta,
        put_theta,
        total_theta,
        source,
    )
    return result


async def resolve_hedge_expiry_date(
    client: DeltaClient,
    underlying: str,
    *,
    expiry_mode: str,
    expiry_date_override: str | None = None,
    expiry_dte: int | None = None,
) -> date:
    """Resolve hedge expiry from settings (monthly | date | dte)."""
    mode = str(expiry_mode or "monthly").lower().strip()
    if mode == "date":
        if not expiry_date_override:
            raise HedgeThetaError(
                "hedge_expiry_date_override required for date mode"
            )
        return _as_date(expiry_date_override)
    if mode == "dte":
        if expiry_dte is None:
            raise HedgeThetaError("hedge_expiry_dte required for dte mode")
        return get_expiry_date_for_dte(int(expiry_dte))

    rows = await client.get_available_expiries(underlying, limit=60)
    monthlies = [
        _as_date(r["date"])
        for r in rows
        if _is_monthly_expiry(_as_date(r["date"]))
    ]
    if not monthlies:
        if not rows:
            raise HedgeThetaError("No option expiries available for hedge")
        return _as_date(rows[-1]["date"])
    today = get_ist_now().date()
    future = [d for d in monthlies if d >= today]
    if not future:
        return monthlies[-1]
    return future[0]


async def resolve_short_expiry_date(
    *,
    expiry_dte: int,
    expiry_date_override: str | None = None,
) -> date:
    """
    Resolve the short-basket expiry — same rules as auto_trade_engine.

    Daily 0/1/2 DTE: always compute from expiry_dte (ignore calendar override).
    Weekly/monthly (dte > 2): use expiry_date_override when it is still valid.
    """
    dte = int(expiry_dte if expiry_dte is not None else 1)
    override = (str(expiry_date_override).strip()[:10] if expiry_date_override else None)

    if override and dte > 2:
        try:
            parsed = _as_date(override)
            today = get_ist_now().date()
            if parsed >= today:
                return parsed
        except ValueError as exc:
            raise HedgeThetaError(
                f"Invalid short expiry_date_override: {override}"
            ) from exc

    return get_expiry_date_for_dte(dte)


async def assert_expiry_available(
    client: DeltaClient,
    underlying: str,
    expiry: date,
) -> None:
    """Raise ExpiryNotAvailableError if expiry is not listed on Delta."""
    product_u = underlying.upper().strip()
    if product_u.endswith("USD") and len(product_u) > 3:
        product_u = product_u[:-3]
    try:
        rows = await client.get_available_expiries(product_u, limit=90)
    except DeltaAPIError as exc:
        raise HedgeThetaError(str(exc)) from exc
    dates = {_as_date(r["date"]) for r in rows}
    if expiry not in dates:
        raise ExpiryNotAvailableError(
            f"Expiry {expiry.isoformat()} is not available on Delta "
            f"for {product_u}. Available: "
            f"{', '.join(sorted(d.isoformat() for d in dates)[:12])}"
            f"{'…' if len(dates) > 12 else ''}"
        )


async def get_hypothetical_hedge_theta(
    client: DeltaClient,
    underlying: str,
    expiry_date: date | str,
    quantity: int,
    *,
    spot: float | None = None,
) -> dict[str, Any]:
    """
    Resolve ATM strike from current spot and read call+put theta from the chain.

    Mandatory for settings preview before any hedge exists.
    """
    exp = _as_date(expiry_date)
    product_u = underlying.upper().strip()
    if product_u.endswith("USD") and len(product_u) > 3:
        product_u = product_u[:-3]

    try:
        if spot is None or float(spot) <= 0:
            price_symbol = f"{product_u}USD"
            spot = float(await client.get_underlying_price(price_symbol))
        chain = await client.get_option_chain(product_u, exp.isoformat())
    except DeltaAPIError as exc:
        raise HedgeThetaError(str(exc)) from exc
    except Exception as exc:
        raise HedgeThetaError(f"Chain fetch failed: {exc}") from exc

    if not chain:
        raise HedgeThetaError(f"Empty option chain for {product_u} {exp}")

    atm = annotate_atm(chain, float(spot))
    if atm is None:
        raise HedgeThetaError("Could not resolve ATM strike")

    row = next((r for r in chain if float(r["strike"]) == float(atm)), None)
    if row is None:
        raise HedgeThetaError(f"ATM strike {atm} missing from chain")

    call_ask = float(row.get("call_ask") or row.get("call_mark_price") or 0)
    put_ask = float(row.get("put_ask") or row.get("put_mark_price") or 0)
    call_theta = float(row.get("call_theta") or 0)
    put_theta = float(row.get("put_theta") or 0)
    if call_ask <= 0 or put_ask <= 0:
        raise HedgeThetaError("ATM ask prices unavailable")
    if call_theta == 0 and put_theta == 0:
        raise HedgeThetaError("ATM theta unavailable from chain")

    return _pack_theta_result(
        strike=float(atm),
        expiry_date=exp,
        call_theta=call_theta,
        put_theta=put_theta,
        call_ask=call_ask,
        put_ask=put_ask,
        call_iv=float(row.get("call_iv") or 0),
        put_iv=float(row.get("put_iv") or 0),
        spot=float(spot),
        quantity=quantity,
        source="hypothetical",
    )


async def get_hedge_theta(
    client: DeltaClient,
    hedge_position: Any,
) -> dict[str, Any]:
    """Read live theta for an existing hedge_positions row."""
    underlying = str(getattr(hedge_position, "underlying", "") or "").upper()
    expiry = _as_date(getattr(hedge_position, "expiry_date"))
    strike = float(getattr(hedge_position, "strike") or 0)
    quantity = int(getattr(hedge_position, "quantity") or 1)
    if not underlying or strike <= 0:
        raise HedgeThetaError("Invalid hedge_position")

    product_u = underlying[:-3] if underlying.endswith("USD") else underlying
    try:
        spot = float(await client.get_underlying_price(f"{product_u}USD"))
        chain = await client.get_option_chain(product_u, expiry.isoformat())
    except DeltaAPIError as exc:
        raise HedgeThetaError(str(exc)) from exc
    except Exception as exc:
        raise HedgeThetaError(f"Chain fetch failed: {exc}") from exc

    row = next(
        (r for r in chain if abs(float(r["strike"]) - strike) < 0.01),
        None,
    )
    if row is None:
        return await get_hypothetical_hedge_theta(
            client, product_u, expiry, quantity, spot=spot
        )

    return _pack_theta_result(
        strike=float(row["strike"]),
        expiry_date=expiry,
        call_theta=float(row.get("call_theta") or 0),
        put_theta=float(row.get("put_theta") or 0),
        call_ask=float(row.get("call_ask") or row.get("call_mark_price") or 0),
        put_ask=float(row.get("put_ask") or row.get("put_mark_price") or 0),
        call_iv=float(row.get("call_iv") or 0),
        put_iv=float(row.get("put_iv") or 0),
        spot=float(spot),
        quantity=quantity,
        source="live",
    )


def _side_premium(row: dict[str, Any], side: str) -> float:
    if side == "call":
        return float(
            row.get("call_ask")
            or row.get("call_mark_price")
            or row.get("call_bid")
            or 0
        )
    return float(
        row.get("put_ask") or row.get("put_mark_price") or row.get("put_bid") or 0
    )


def _side_theta(row: dict[str, Any], side: str) -> float:
    key = "call_theta" if side == "call" else "put_theta"
    return abs(float(row.get(key) or 0))


def select_theta_based_strikes(
    chain: list[dict[str, Any]],
    spot: float,
    required_theta: float,
) -> dict[str, Any]:
    """
    Call-by-theta / put-by-premium-match on the SHORT expiry chain.

    CALL: OTM only (strike > spot); furthest whose |theta| >= required_theta.
    PUT:  OTM only (strike < spot); closest premium to the chosen call.
    """
    if not chain or spot <= 0 or required_theta <= 0:
        raise HedgeThetaError(
            "Invalid chain or required_theta for strike selection"
        )

    sorted_rows = sorted(chain, key=lambda r: float(r["strike"]))
    call_rows = [r for r in sorted_rows if float(r["strike"]) > float(spot)]
    put_rows = [r for r in sorted_rows if float(r["strike"]) < float(spot)]

    if not call_rows:
        raise HedgeThetaError("No OTM call strikes (strike > spot) on short chain")
    if not put_rows:
        raise HedgeThetaError("No OTM put strikes (strike < spot) on short chain")

    # Walk outward (ascending strike); keep last that still meets required_theta
    last_ok: dict[str, Any] | None = None
    for row in call_rows:
        if _side_theta(row, "call") >= required_theta:
            last_ok = row
        else:
            break

    if last_ok is None:
        # Nearest OTM already below floor — stay there (not a chain-limit case)
        call_row = call_rows[0]
        chain_limit = False
    else:
        call_row = last_ok
        # Ran out of strikes while still meeting the floor
        chain_limit = last_ok is call_rows[-1]

    call_premium = _side_premium(call_row, "call")
    if call_premium <= 0:
        raise HedgeThetaError("Chosen call premium unavailable")

    put_row = min(
        put_rows,
        key=lambda r: abs(_side_premium(r, "put") - call_premium),
    )

    call_theta = _side_theta(call_row, "call")
    put_theta = _side_theta(put_row, "put")
    return {
        "call": {
            "strike": float(call_row["strike"]),
            "premium": round(call_premium, 2),
            "theta": round(call_theta, 4),
            "chain_limit": bool(chain_limit),
        },
        "put": {
            "strike": float(put_row["strike"]),
            "premium": round(_side_premium(put_row, "put"), 2),
            "theta": round(put_theta, 4),
            "premium_matched": True,
        },
        "combined_theta": round(call_theta + put_theta, 4),
    }


def compute_iv_percentile(
    current_iv: float,
    historical_ivs: list[float],
) -> dict[str, Any]:
    """IV percentile from hedge_theta_log; needs >= 30 samples."""
    samples = [float(x) for x in historical_ivs if x is not None and float(x) > 0]
    if len(samples) < 30:
        return {
            "percentile": None,
            "status": "collecting_data",
            "sample_count": len(samples),
            "message": "percentile: collecting data",
        }
    cur = float(current_iv)
    below = sum(1 for x in samples if x <= cur)
    pct = round(100.0 * below / len(samples), 1)
    return {
        "percentile": pct,
        "status": "ok",
        "sample_count": len(samples),
        "message": f"{pct:.0f}th",
    }
