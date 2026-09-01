# hedge_theta.py — Live / hypothetical hedge theta reader (read-only, no trading)

from __future__ import annotations

import logging
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


LEGACY_HEDGE_EXPIRY_MODES = frozenset({"monthly", "date", "dte"})


def is_relative_expiry_key(key: str | None) -> bool:
    """True for keys like 0dte, week_2, month_1."""
    k = str(key or "").lower().strip()
    if not k or k in LEGACY_HEDGE_EXPIRY_MODES:
        return False
    if k.endswith("dte") and k[:-3].isdigit():
        return True
    if k.startswith("month_") and k.split("_", 1)[-1].isdigit():
        return True
    if k.startswith("week_") and k.split("_", 1)[-1].isdigit():
        return True
    return False


def migrate_hedge_expiry_mode(
    mode: str | None,
    *,
    expiry_dte: int | None = None,
) -> tuple[str, bool]:
    """
    Map legacy hedge_expiry_mode to a relative label key.

    Returns (mode_or_key, needs_repick).
    - monthly -> month_1
    - dte -> Ndte when N is known, else needs_repick
    - date -> stays 'date' with needs_repick=True
    """
    raw = str(mode or "month_1").lower().strip()
    if is_relative_expiry_key(raw):
        return raw, False
    if raw == "monthly":
        return "month_1", False
    if raw == "dte":
        if expiry_dte is None:
            return "dte", True
        n = int(expiry_dte)
        return f"{n}dte", False
    if raw == "date":
        return "date", True
    # Unknown — ask user to re-pick
    return raw, True


async def resolve_hedge_expiry_date(
    client: DeltaClient,
    underlying: str,
    *,
    expiry_mode: str,
    expiry_date_override: str | None = None,
    expiry_dte: int | None = None,
) -> date:
    """
    Resolve hedge expiry from a relative label key (preferred) or legacy modes.

    Label keys (month_1, week_2, 1dte, …) are resolved fresh against the live
    Delta expiry list. Never silently substitute a different key's date.
    """
    mode = str(expiry_mode or "month_1").lower().strip()
    migrated, needs_repick = migrate_hedge_expiry_mode(
        mode, expiry_dte=expiry_dte
    )
    if needs_repick and migrated == "date":
        # Legacy fixed date — still honour the stored calendar date if present,
        # but callers should surface needs_repick to the UI.
        if not expiry_date_override:
            raise ExpiryNotAvailableError(
                "Hedge expiry is a fixed calendar date that must be re-picked "
                "as a relative label (e.g. Month 2). No date is stored."
            )
        return _as_date(expiry_date_override)
    if needs_repick:
        raise ExpiryNotAvailableError(
            f"Hedge expiry setting '{mode}' is stale — re-pick a labelled "
            "expiry (e.g. Month 1, Week 2, 1DTE)."
        )

    mode = migrated

    # Legacy paths kept for any unmigrated callers
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
    if mode == "monthly":
        mode = "month_1"

    product_u = underlying.upper().strip()
    if product_u.endswith("USD") and len(product_u) > 3:
        product_u = product_u[:-3]

    try:
        rows = await client.get_available_expiries(product_u, limit=60)
    except DeltaAPIError as exc:
        raise HedgeThetaError(str(exc)) from exc

    if not rows:
        raise ExpiryNotAvailableError(
            f"No option expiries available on Delta for {product_u}"
        )

    match = next((r for r in rows if str(r.get("key") or "") == mode), None)
    if match is None:
        available = ", ".join(
            f"{r.get('key')} ({r.get('label')})" for r in rows[:15]
        )
        raise ExpiryNotAvailableError(
            f"Hedge expiry label '{mode}' is not available on Delta for "
            f"{product_u} right now. Available: {available}"
            f"{'…' if len(rows) > 15 else ''}"
        )
    return _as_date(match["date"])


async def enforce_min_hedge_dte(
    client: DeltaClient,
    underlying: str,
    requested: date,
    min_hedge_dte: int,
    *,
    opened_via: str = "auto",
    log_hedge_id: int = 0,
) -> date:
    """
    If requested expiry is closer than min_hedge_dte, advance to the next
    monthly expiry that satisfies the floor (then further monthlies if needed).

    Logs [HEDGE_EXPIRY_SKIP] via log_and_buffer when a skip occurs.
    """
    from backend.core.bot_logger import log_and_buffer

    min_dte = int(min_hedge_dte if min_hedge_dte is not None else 15)
    min_dte = max(0, min(60, min_dte))
    today = get_ist_now().date()
    req = _as_date(requested)
    dte = (req - today).days
    if dte >= min_dte:
        return req

    product_u = underlying.upper().strip()
    if product_u.endswith("USD") and len(product_u) > 3:
        product_u = product_u[:-3]

    try:
        rows = await client.get_available_expiries(product_u, limit=60)
    except DeltaAPIError as exc:
        raise HedgeThetaError(
            f"Could not list expiries for min-DTE guard: {exc}"
        ) from exc

    monthly_dates = sorted(
        {
            _as_date(r["date"])
            for r in rows
            if str(r.get("key") or "").startswith("month_")
        }
    )
    later = [d for d in monthly_dates if d > req]
    selected: date | None = None
    for d in later:
        if (d - today).days >= min_dte:
            selected = d
            break
    if selected is None and later:
        # No monthly meets the floor — still take the furthest available monthly
        selected = later[-1]
    if selected is None:
        logger.warning(
            "[HEDGE_EXPIRY_SKIP] no later monthly after %s (dte=%s min=%s) — "
            "keeping requested",
            req.isoformat(),
            dte,
            min_dte,
        )
        return req

    new_dte = (selected - today).days
    via = str(opened_via or "auto").lower().strip()
    details = {
        "requested": req.isoformat(),
        "dte": int(dte),
        "min_required": int(min_dte),
        "selected": selected.isoformat(),
        "new_dte": int(new_dte),
        "opened_via": via,
        "manual": via == "manual",
        "summary": (
            f"[HEDGE_EXPIRY_SKIP] requested={req.isoformat()} dte={dte} "
            f"min_required={min_dte} selected={selected.isoformat()} "
            f"new_dte={new_dte}"
            + (f" | opened_via=manual" if via == "manual" else "")
        ),
    }
    try:
        log_and_buffer("HEDGE_EXPIRY_SKIP", int(log_hedge_id or 0), details)
    except Exception as exc:
        logger.warning("HEDGE_EXPIRY_SKIP log_and_buffer failed: %s", exc)
    return selected


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


async def compute_basket_profit_target_at_entry(
    *,
    settings: Any,
    client: DeltaClient,
    hedge: Any | None,
    quantity: int,
    credit_usd: float,
    call_fill: float,
    put_fill: float,
    call_fee: float | None,
    put_fee: float | None,
    call_symbol: str,
    put_symbol: str,
    tp_pct: float,
    btc_index: float,
) -> dict[str, Any]:
    """
    Lock basket profit_target_usd at entry.

    THETA mode (and active hedge):
      target = basket_target_multiple × hedge_total_theta × qty × CONTRACT_SIZE
    PCT mode (or no hedge):
      target = credit_usd × tp_pct / 100

    Caps at 90% of max_achievable when the raw target is unreachable.
    """
    from backend.core.fees import estimate_option_trading_fee
    from backend.core.spread_utils import estimate_and_log_exit_spread_usd

    qty = max(1, int(quantity))
    credit = max(0.0, float(credit_usd or 0.0))
    mode_raw = str(
        getattr(settings, "basket_target_mode", None) or "THETA"
    ).upper().strip()
    mode = "THETA" if mode_raw == "THETA" else "PCT"
    multiple = float(
        getattr(settings, "basket_target_multiple", None)
        if getattr(settings, "basket_target_multiple", None) is not None
        else 1.5
    )
    multiple = max(0.1, min(10.0, multiple))

    entry_fees = max(0.0, float(call_fee or 0.0)) + max(
        0.0, float(put_fee or 0.0)
    )
    est_exit_fees = 0.0
    btc = float(btc_index or 0.0)
    if btc > 0:
        if float(call_fill) > 0:
            est_exit_fees += estimate_option_trading_fee(
                option_price=float(call_fill),
                quantity_lots=qty,
                btc_index_price=btc,
            )
        if float(put_fill) > 0:
            est_exit_fees += estimate_option_trading_fee(
                option_price=float(put_fill),
                quantity_lots=qty,
                btc_index_price=btc,
            )

    est_exit_spread = 0.0
    try:
        if call_symbol and float(call_fill) > 0:
            est_exit_spread += await estimate_and_log_exit_spread_usd(
                symbol=str(call_symbol),
                offer_price=float(call_fill),
                quantity=qty,
                settings=settings,
                kind="basket",
                client=client,
                log_id=0,
            )
        if put_symbol and float(put_fill) > 0:
            est_exit_spread += await estimate_and_log_exit_spread_usd(
                symbol=str(put_symbol),
                offer_price=float(put_fill),
                quantity=qty,
                settings=settings,
                kind="basket",
                client=client,
                log_id=0,
            )
    except Exception as exc:
        logger.warning(
            "basket target exit-spread estimate failed: %s", exc, exc_info=True
        )
        # Fallback: manual % of credit
        spread_pct = float(
            getattr(settings, "basket_exit_spread_pct", None)
            if getattr(settings, "basket_exit_spread_pct", None) is not None
            else 4.0
        )
        est_exit_spread = credit * (max(0.0, spread_pct) / 100.0)

    friction = entry_fees + est_exit_fees + max(0.0, float(est_exit_spread))
    max_achievable = max(0.0, credit - friction)

    hedge_theta: float | None = None
    target_source = "PCT"
    raw_target = round(credit * float(tp_pct) / 100.0, 6)

    if mode == "THETA" and hedge is not None:
        try:
            theta_info = await get_hedge_theta(client, hedge)
            hedge_theta = abs(float(theta_info.get("total_theta") or 0.0))
            if hedge_theta > 0:
                raw_target = round(
                    multiple * hedge_theta * qty * CONTRACT_SIZE, 6
                )
                target_source = "THETA"
            else:
                logger.warning(
                    "basket THETA target: hedge total_theta is 0 — falling "
                    "back to PCT"
                )
        except Exception as exc:
            logger.warning(
                "basket THETA target: get_hedge_theta failed (%s) — "
                "falling back to PCT",
                exc,
            )

    capped = False
    profit_target = float(raw_target)
    capture_before_cap = (
        (raw_target / max_achievable * 100.0) if max_achievable > 0 else 0.0
    )
    if max_achievable > 0 and profit_target > max_achievable:
        capped = True
        profit_target = round(max_achievable * 0.9, 6)
    profit_target = max(0.0, float(profit_target))

    capture_required_pct = (
        (profit_target / max_achievable * 100.0) if max_achievable > 0 else 0.0
    )

    return {
        "profit_target_usd": round(profit_target, 6),
        "raw_target_usd": round(float(raw_target), 6),
        "target_source": target_source,
        "hedge_theta_at_entry": (
            round(float(hedge_theta), 6) if hedge_theta is not None else None
        ),
        "basket_target_multiple": round(multiple, 4),
        "credit_usd": round(credit, 6),
        "entry_fees": round(entry_fees, 6),
        "est_exit_fees": round(est_exit_fees, 6),
        "est_exit_spread": round(float(est_exit_spread), 6),
        "friction": round(friction, 6),
        "max_achievable": round(max_achievable, 6),
        "capture_required_pct": round(capture_required_pct, 2),
        "capture_required_pct_raw": round(float(capture_before_cap), 2),
        "capped": bool(capped),
        "mode": mode,
    }


def _side_premium(row: dict[str, Any], side: str) -> float:
    """
    Premium for strike matching — prefer mark (same as adjustment / strangle).

    Preferring ask caused far-OTM puts to win: near-ATM puts often have wide
    asks, so abs(ask − target) looked worse than a cheap far put's tight ask.
    """
    if side == "call":
        return float(
            row.get("call_mark_price")
            or row.get("call_bid")
            or row.get("call_ask")
            or 0
        )
    return float(
        row.get("put_mark_price")
        or row.get("put_bid")
        or row.get("put_ask")
        or 0
    )


def _side_theta(row: dict[str, Any], side: str) -> float:
    key = "call_theta" if side == "call" else "put_theta"
    return abs(float(row.get(key) or 0))


def _resolve_entry_premium_tolerance(explicit: float | None) -> float:
    """Clamp entry_premium_match_tolerance_pct to [5, 100]; default 25."""
    if explicit is not None:
        return max(5.0, min(100.0, float(explicit)))
    try:
        from backend.database import SessionLocal, get_or_create_auto_settings

        with SessionLocal() as _db:
            _settings = get_or_create_auto_settings(_db)
            raw = getattr(_settings, "entry_premium_match_tolerance_pct", None)
            if raw is not None:
                return max(5.0, min(100.0, float(raw)))
    except Exception as exc:
        logger.warning(
            "entry_premium_match_tolerance_pct read failed: %s", exc
        )
    return 25.0


def select_theta_based_strikes(
    chain: list[dict[str, Any]],
    spot: float,
    required_theta: float,
    *,
    hedge_call_theta: float | None = None,
    theta_multiplier: float | None = None,
    log_hedge_id: int = 0,
    log_trade_id: int = 0,
    entry_premium_match_tolerance_pct: float | None = None,
) -> dict[str, Any]:
    """
    Call-by-premium / put-by-premium-match on the SHORT expiry chain.

    required_theta is the numeric target from abs(hedge_call_theta) × multiplier;
    it is applied as required_call_premium (not as a theta floor).

    CALL: OTM only (strike > spot); scan full call chain, pick the
          highest strike whose premium >= required_call_premium (furthest OTM).
    PUT:  OTM only (strike < spot); scan full put chain, minimise
          abs(put_premium − call_premium). Tie-break: nearer ATM (higher strike).

    When no OTM call premium meets the floor, falls back to the nearest OTM
    call and logs [PREMIUM_TARGET_UNREACHABLE]. [THETA_FALLBACK] still fires
    when the numeric target exceeds max |theta| on the chain (observability).
    """
    from backend.core.bot_logger import log_and_buffer

    if not chain or spot <= 0 or required_theta <= 0:
        raise HedgeThetaError(
            "Invalid chain or required_theta for strike selection"
        )

    sorted_rows = sorted(chain, key=lambda r: float(r["strike"]))
    # Strictly OTM only — never ATM/ITM to satisfy the premium floor
    call_rows = [r for r in sorted_rows if float(r["strike"]) > float(spot)]
    put_rows = [r for r in sorted_rows if float(r["strike"]) < float(spot)]

    if not call_rows:
        raise HedgeThetaError("No OTM call strikes (strike > spot) on short chain")
    if not put_rows:
        raise HedgeThetaError("No OTM put strikes (strike < spot) on short chain")

    # Highest |theta| anywhere on the short chain (call or put) — observability
    max_available_theta = 0.0
    for row in sorted_rows:
        max_available_theta = max(
            max_available_theta,
            _side_theta(row, "call"),
            _side_theta(row, "put"),
        )

    # Same numeric as abs(hedge_call_theta)*multiplier — applied as premium floor
    required_call_premium = float(required_theta)
    req = required_call_premium
    theta_fallback_used = bool(req > max_available_theta)

    hct = abs(float(hedge_call_theta)) if hedge_call_theta is not None else 0.0
    mult = float(theta_multiplier) if theta_multiplier is not None else (
        (req / hct) if hct > 0 else 0.0
    )
    max_usable_multiplier = (
        (max_available_theta / hct) if hct > 0 else 0.0
    )

    # Full OTM call scan — furthest (highest strike) with premium >= required.
    qualifying_calls: list[dict[str, Any]] = [
        row
        for row in call_rows
        if _side_premium(row, "call") >= required_call_premium
    ]
    qualifying_strikes_count = len(qualifying_calls)
    highest_qualifying_strike: float | None = None
    premium_fallback_used = False
    if qualifying_calls:
        highest_row = max(
            qualifying_calls, key=lambda r: float(r["strike"])
        )
        highest_qualifying_strike = float(highest_row["strike"])
        call_row = highest_row
        chain_limit = highest_row is call_rows[-1] or (
            abs(float(highest_row["strike"]) - float(call_rows[-1]["strike"]))
            < 0.01
        )
    else:
        # Required exceeds even nearest-OTM premium — take nearest OTM
        call_row = call_rows[0]
        chain_limit = False
        premium_fallback_used = True

    call_premium = _side_premium(call_row, "call")
    if call_premium <= 0:
        raise HedgeThetaError("Chosen call premium unavailable")

    # Full put-chain closest premium match (never stop at first below/above)
    put_candidates: list[tuple[float, float, dict[str, Any]]] = []
    for row in put_rows:
        prem = _side_premium(row, "put")
        if prem <= 0:
            continue
        # Sort key: abs diff, then nearer ATM (higher put strike → lower -strike)
        put_candidates.append(
            (abs(prem - call_premium), -float(row["strike"]), row)
        )
    candidates_scanned = len(put_candidates)
    if not put_candidates:
        raise HedgeThetaError("No OTM put strikes with usable premium on chain")
    put_candidates.sort(key=lambda t: (t[0], t[1]))
    put_row = put_candidates[0][2]

    call_theta = _side_theta(call_row, "call")
    put_theta = _side_theta(put_row, "put")
    call_strike = float(call_row["strike"])
    put_strike = float(put_row["strike"])
    put_premium = _side_premium(put_row, "put")
    selected_is_highest = (
        highest_qualifying_strike is not None
        and abs(call_strike - highest_qualifying_strike) < 0.01
    )
    total_basket_theta = call_theta + put_theta
    premium_deviation_pct = (
        abs(put_premium - call_premium) / call_premium * 100.0
        if call_premium > 0
        else 0.0
    )
    theta_vs_required_pct = (
        (total_basket_theta / req * 100.0) if req > 0 else 0.0
    )
    premium_margin_pct = (
        ((call_premium - required_call_premium) / required_call_premium * 100.0)
        if required_call_premium > 0
        else 0.0
    )
    strikes_above_selected = sum(
        1 for r in call_rows if float(r["strike"]) > call_strike + 0.01
    )
    tolerance_pct = _resolve_entry_premium_tolerance(
        entry_premium_match_tolerance_pct
    )
    trade_ref = int(log_trade_id or log_hedge_id or 0)

    if premium_fallback_used:
        details = {
            "hedge": int(log_hedge_id or 0),
            "required": round(required_call_premium, 4),
            "best_available": round(call_premium, 4),
            "strike": call_strike,
            "spot": round(float(spot), 2),
            "summary": (
                f"[PREMIUM_TARGET_UNREACHABLE] "
                f"required={round(required_call_premium, 4)} | "
                f"best_available={round(call_premium, 4)} | "
                f"strike={call_strike} | spot={round(float(spot), 2)}"
            ),
        }
        try:
            log_and_buffer(
                "PREMIUM_TARGET_UNREACHABLE",
                int(log_hedge_id or 0),
                details,
            )
        except Exception as exc:
            logger.warning(
                "PREMIUM_TARGET_UNREACHABLE log_and_buffer failed: %s", exc
            )
        logger.info(
            "[PREMIUM_TARGET_UNREACHABLE] required=%s best_available=%s "
            "strike=%s spot=%s",
            round(required_call_premium, 4),
            round(call_premium, 4),
            call_strike,
            round(float(spot), 2),
        )

    # Observability: numeric target still exceeds max chain |theta|
    fallback_used = bool(theta_fallback_used)
    if fallback_used:
        shortfall_pct = (
            ((req - max_available_theta) / req * 100.0) if req > 0 else 0.0
        )
        details = {
            "hedge": int(log_hedge_id or 0),
            "required": round(req, 4),
            "max_available": round(float(max_available_theta), 4),
            "shortfall_pct": round(float(shortfall_pct), 2),
            "max_usable_multiplier": round(float(max_usable_multiplier), 4),
            "selected_call": call_strike,
            "selected_put": put_strike,
            "summary": (
                f"[THETA_FALLBACK] hedge={int(log_hedge_id or 0)} | "
                f"required={round(req, 4)} | "
                f"max_available={round(float(max_available_theta), 4)} | "
                f"shortfall_pct={round(float(shortfall_pct), 2)} | "
                f"max_usable_multiplier={round(float(max_usable_multiplier), 4)} | "
                f"selected_call={call_strike} | selected_put={put_strike}"
            ),
        }
        try:
            log_and_buffer("THETA_FALLBACK", int(log_hedge_id or 0), details)
        except Exception as exc:
            logger.warning("THETA_FALLBACK log_and_buffer failed: %s", exc)

    if (
        qualifying_strikes_count > 0
        and highest_qualifying_strike is not None
        and not selected_is_highest
    ):
        highest_prem = next(
            (
                _side_premium(r, "call")
                for r in qualifying_calls
                if abs(float(r["strike"]) - highest_qualifying_strike) < 0.01
            ),
            0.0,
        )
        suboptimal_summary = (
            f"[THETA_SELECT_SUBOPTIMAL] trade={trade_ref} | "
            f"selected_strike={call_strike} "
            f"selected_premium={round(call_premium, 4)} | "
            f"highest_qualifying_strike={highest_qualifying_strike} "
            f"highest_premium={round(highest_prem, 4)} | "
            f"required_call_premium={round(required_call_premium, 4)}"
        )
        try:
            log_and_buffer(
                "THETA_SELECT_SUBOPTIMAL",
                trade_ref,
                {
                    "trade": trade_ref,
                    "selected_strike": call_strike,
                    "selected_premium": round(call_premium, 4),
                    "highest_qualifying_strike": highest_qualifying_strike,
                    "highest_premium": round(highest_prem, 4),
                    "required_call_premium": round(required_call_premium, 4),
                    "summary": suboptimal_summary,
                },
            )
        except Exception as exc:
            logger.warning(
                "THETA_SELECT_SUBOPTIMAL log_and_buffer failed: %s", exc
            )

    select_summary = (
        f"[ENTRY_STRIKE_SELECT] trade={trade_ref} | method=premium | "
        f"spot={round(float(spot), 2)} | "
        f"hedge_call_theta={round(hct, 4)} | "
        f"multiplier={round(mult, 4)} | "
        f"required_call_premium={round(required_call_premium, 4)} | "
        f"selected_call_premium={round(call_premium, 2)} | "
        f"premium_margin_pct={round(premium_margin_pct, 2)} | "
        f"strikes_above_selected={strikes_above_selected} | "
        f"call_strike={call_strike} call_theta={round(call_theta, 4)} | "
        f"put_strike={put_strike} put_premium={round(put_premium, 2)} "
        f"put_theta={round(put_theta, 4)} | "
        f"premium_deviation_pct={round(premium_deviation_pct, 2)} | "
        f"total_basket_theta={round(total_basket_theta, 4)} | "
        f"candidates_scanned={candidates_scanned} | "
        f"qualifying_strikes_count={qualifying_strikes_count} | "
        f"highest_qualifying_strike={highest_qualifying_strike} | "
        f"selected_is_highest={selected_is_highest} | "
        f"premium_fallback_used={premium_fallback_used}"
    )
    try:
        log_and_buffer(
            "ENTRY_STRIKE_SELECT",
            trade_ref,
            {
                "trade": trade_ref,
                "method": "premium",
                "spot": round(float(spot), 2),
                "hedge_call_theta": round(hct, 4),
                "multiplier": round(mult, 4),
                "required_call_premium": round(required_call_premium, 4),
                "required_theta": round(req, 4),  # alias — same numeric
                "selected_call_premium": round(call_premium, 2),
                "premium_margin_pct": round(premium_margin_pct, 2),
                "strikes_above_selected": strikes_above_selected,
                "call_strike": call_strike,
                "call_premium": round(call_premium, 2),
                "call_theta": round(call_theta, 4),
                "put_strike": put_strike,
                "put_premium": round(put_premium, 2),
                "put_theta": round(put_theta, 4),
                "premium_deviation_pct": round(premium_deviation_pct, 2),
                "total_basket_theta": round(total_basket_theta, 4),
                "theta_vs_required_pct": round(theta_vs_required_pct, 2),
                "candidates_scanned": candidates_scanned,
                "qualifying_strikes_count": qualifying_strikes_count,
                "highest_qualifying_strike": highest_qualifying_strike,
                "selected_is_highest": bool(selected_is_highest),
                "premium_fallback_used": bool(premium_fallback_used),
                "summary": select_summary,
            },
        )
    except Exception as exc:
        logger.warning("ENTRY_STRIKE_SELECT log_and_buffer failed: %s", exc)

    if premium_deviation_pct > tolerance_pct:
        miss_summary = (
            f"[ENTRY_PREMIUM_MISS] trade={trade_ref} | "
            f"target={round(call_premium, 2)} | "
            f"selected={round(put_premium, 2)} | "
            f"deviation_pct={round(premium_deviation_pct, 2)} | "
            f"tolerance_pct={round(tolerance_pct, 2)} | "
            f"call_strike={call_strike} | put_strike={put_strike}"
        )
        try:
            log_and_buffer(
                "ENTRY_PREMIUM_MISS",
                trade_ref,
                {
                    "trade": trade_ref,
                    "target": round(call_premium, 2),
                    "selected": round(put_premium, 2),
                    "deviation_pct": round(premium_deviation_pct, 2),
                    "tolerance_pct": round(tolerance_pct, 2),
                    "call_strike": call_strike,
                    "put_strike": put_strike,
                    "summary": miss_summary,
                },
            )
        except Exception as exc:
            logger.warning("ENTRY_PREMIUM_MISS log_and_buffer failed: %s", exc)

    return {
        "call": {
            "strike": call_strike,
            "premium": round(call_premium, 2),
            "theta": round(call_theta, 4),
            "chain_limit": bool(chain_limit),
            "symbol": str(call_row.get("call_symbol") or ""),
            "product_id": int(call_row.get("call_product_id") or 0),
        },
        "put": {
            "strike": put_strike,
            "premium": round(put_premium, 2),
            "theta": round(put_theta, 4),
            "premium_matched": True,
            "symbol": str(put_row.get("put_symbol") or ""),
            "product_id": int(put_row.get("put_product_id") or 0),
        },
        "combined_theta": round(total_basket_theta, 4),
        "required_theta": round(req, 4),
        "required_call_premium": round(required_call_premium, 4),
        "selected_call_premium": round(call_premium, 2),
        "premium_margin_pct": round(premium_margin_pct, 2),
        "strikes_above_selected": strikes_above_selected,
        "max_available_theta": round(float(max_available_theta), 4),
        "fallback_used": bool(fallback_used),
        "premium_fallback_used": bool(premium_fallback_used),
        "max_usable_multiplier": round(float(max_usable_multiplier), 4),
        "premium_deviation_pct": round(premium_deviation_pct, 2),
        "candidates_scanned": candidates_scanned,
        "theta_vs_required_pct": round(theta_vs_required_pct, 2),
        "qualifying_strikes_count": qualifying_strikes_count,
        "highest_qualifying_strike": highest_qualifying_strike,
        "selected_is_highest": bool(selected_is_highest),
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
