# wing_exit.py — Centralised basket leg close + wing cross-guard (Wings 3/4)
#
# Exit order (non-negotiable): SHORTS first, then WINGS.
# Entry was the reverse (wings first) for margin; exit is shorts-first for risk.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.core.time_utils import get_utc_now
from backend.strategies.base_strategy import OrderResult

logger = logging.getLogger(__name__)

CLOSE_LEG_MAX_ATTEMPTS = 3

SHORT_LEG_TYPES = frozenset({"call", "put"})
WING_LEG_TYPES = frozenset({"wing_call", "wing_put"})
HEDGE_LEG_TYPES = frozenset({"hedge_call", "hedge_put"})


@dataclass
class ClosedLegResult:
    leg_id: int | None
    leg_type: str
    symbol: str
    product_id: int
    success: bool
    fill_price: float | None = None
    order_id: str | None = None
    commission: float | None = None
    error: str | None = None
    attempts: int = 0
    skipped: bool = False
    is_wing: bool = False
    is_short: bool = False
    # Pre-placement clock for ledger closed_at (structure_ledger contract)
    closed_at: Any = None
    fill_at: Any = None


@dataclass
class CloseResult:
    legs: list[ClosedLegResult] = field(default_factory=list)
    shorts_closed: int = 0
    wings_closed: int = 0
    hedges_closed: int = 0
    wings_failed: list[ClosedLegResult] = field(default_factory=list)
    any_wing_fail: bool = False

    def result_for_leg_id(self, leg_id: int) -> ClosedLegResult | None:
        for row in self.legs:
            if row.leg_id is not None and int(row.leg_id) == int(leg_id):
                return row
        return None


def is_wing_leg(leg: Any) -> bool:
    lt = str(getattr(leg, "leg_type", "") or "").lower()
    if lt in WING_LEG_TYPES:
        return True
    # Defensive: long + not hedge → treat as wing for close ordering
    if bool(getattr(leg, "is_long", False)) and lt not in HEDGE_LEG_TYPES:
        if lt.startswith("wing"):
            return True
    return False


def is_short_basket_leg(leg: Any) -> bool:
    lt = str(getattr(leg, "leg_type", "") or "").lower()
    if lt in SHORT_LEG_TYPES and not bool(getattr(leg, "is_long", False)):
        return True
    return False


def is_conversion_hedge_leg(leg: Any) -> bool:
    lt = str(getattr(leg, "leg_type", "") or "").lower()
    if lt in HEDGE_LEG_TYPES:
        return True
    return bool(getattr(leg, "is_long", False)) and not is_wing_leg(leg) and (
        lt.startswith("hedge")
    )


def _sort_key(leg: Any) -> tuple[int, int, str]:
    """Shorts (0) → wings (1) → conversion hedges (2). Call before put."""
    lt = str(getattr(leg, "leg_type", "") or "").lower()
    if is_short_basket_leg(leg):
        group = 0
    elif is_wing_leg(leg):
        group = 1
    else:
        group = 2
    side = 0 if "call" in lt else 1 if "put" in lt else 9
    return (group, side, lt)


def filter_legs_for_close(
    legs: list[Any],
    legs_to_close: str = "all",
) -> list[Any]:
    mode = str(legs_to_close or "all").lower().strip()
    open_legs = [
        leg
        for leg in legs
        if str(getattr(leg, "status", "open")).lower() == "open"
    ]
    if mode == "shorts_only":
        selected = [leg for leg in open_legs if is_short_basket_leg(leg)]
    elif mode == "wings_only":
        selected = [leg for leg in open_legs if is_wing_leg(leg)]
    else:
        # all = shorts + wings + conversion hedges (ordered later)
        selected = list(open_legs)
    return sorted(selected, key=_sort_key)


async def _close_one_leg_with_retries(
    *,
    leg: Any,
    order_executor: Any,
    delta_client: Any,
    reason: str,
    trade_id: int | None,
    max_attempts: int = CLOSE_LEG_MAX_ATTEMPTS,
) -> ClosedLegResult:
    lt = str(getattr(leg, "leg_type", "") or "").lower()
    is_wing = is_wing_leg(leg)
    is_short = is_short_basket_leg(leg)
    is_long = bool(getattr(leg, "is_long", False)) or is_wing or lt in HEDGE_LEG_TYPES
    pid = int(getattr(leg, "product_id", 0) or 0)
    sym = str(getattr(leg, "symbol", "") or "")
    leg_id = getattr(leg, "id", None)
    qty = max(1, int(getattr(leg, "quantity", 1) or 1))

    last_error = "unknown"
    last_res: OrderResult | None = None
    # Ledger closed_at MUST be captured immediately BEFORE place (not post-fill).
    place_closed_at = get_utc_now()
    for attempt in range(1, max_attempts + 1):
        try:
            if is_long:
                last_res = await order_executor.close_long_position(
                    product_id=pid,
                    quantity=qty,
                    delta_client=delta_client,
                    symbol_for_fallback=sym,
                )
            else:
                last_res = await order_executor.close_leg(leg, delta_client)
            if last_res.success:
                fill = float(last_res.filled_price or 0.0) or None
                oid = (
                    str(last_res.order_id)
                    if last_res.order_id is not None
                    else None
                )
                fee = (
                    abs(float(last_res.commission))
                    if last_res.commission is not None
                    else None
                )
                if is_wing:
                    side = "call" if "call" in lt else "put"
                    logger.info(
                        "[WING_EXIT] trade=%s leg=%s qty=%s fill=%s reason=%s",
                        trade_id if trade_id is not None else "?",
                        side,
                        qty,
                        fill,
                        reason,
                    )
                return ClosedLegResult(
                    leg_id=int(leg_id) if leg_id is not None else None,
                    leg_type=lt,
                    symbol=sym,
                    product_id=pid,
                    success=True,
                    fill_price=fill,
                    order_id=oid,
                    commission=fee,
                    attempts=attempt,
                    is_wing=is_wing,
                    is_short=is_short,
                    closed_at=place_closed_at,
                    fill_at=get_utc_now(),
                )
            last_error = str(last_res.error or "close_failed")
        except Exception as exc:
            last_error = str(exc)
            logger.error(
                "close_basket_legs attempt %s/%s failed trade=%s leg=%s: %s",
                attempt,
                max_attempts,
                trade_id,
                lt,
                exc,
                exc_info=True,
            )
        if attempt < max_attempts:
            import asyncio

            await asyncio.sleep(0.5)
            # Next attempt is a new place — refresh closed_at bound
            place_closed_at = get_utc_now()

    result = ClosedLegResult(
        leg_id=int(leg_id) if leg_id is not None else None,
        leg_type=lt,
        symbol=sym,
        product_id=pid,
        success=False,
        error=last_error,
        attempts=max_attempts,
        is_wing=is_wing,
        is_short=is_short,
        fill_price=(
            float(last_res.filled_price)
            if last_res is not None and last_res.filled_price is not None
            else None
        ),
        order_id=(
            str(last_res.order_id)
            if last_res is not None and last_res.order_id is not None
            else None
        ),
        closed_at=place_closed_at,
    )
    if is_wing:
        side = "call" if "call" in lt else "put"
        logger.critical(
            "[WING_CLOSE_FAILED] trade=%s leg=%s attempts=%s error=%s",
            trade_id if trade_id is not None else "?",
            side,
            max_attempts,
            last_error,
        )
    return result


async def close_basket_legs(
    *,
    trade: Any,
    reason: str,
    db: Any,
    delta_client: Any,
    order_executor: Any,
    legs_to_close: str = "all",
    legs: list[Any] | None = None,
    verify_on_delta: bool = True,
) -> CloseResult:
    """
    Close basket legs in SAFE order: shorts → wings → conversion hedges.

    legs_to_close: "all" | "shorts_only" | "wings_only"
    Caller is responsible for booking DB closes from CloseResult.
    """
    trade_id = int(getattr(trade, "id", 0) or 0) or None
    if legs is None:
        if db is None:
            raise ValueError("close_basket_legs requires db or legs=")
        from backend.models import Leg

        legs = (
            db.query(Leg)
            .filter(
                Leg.trade_id == int(trade.id),
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .all()
        )

    ordered = filter_legs_for_close(legs, legs_to_close)
    out = CloseResult()

    for leg in ordered:
        if str(getattr(leg, "status", "open")).lower() != "open":
            continue

        pid = int(getattr(leg, "product_id", 0) or 0)
        on_delta = True
        if verify_on_delta and delta_client is not None and pid > 0:
            try:
                on_delta = bool(
                    await delta_client.verify_position_exists(pid)
                )
            except Exception as exc:
                logger.warning(
                    "close_basket_legs verify failed %s: %s — assume exists",
                    getattr(leg, "symbol", "?"),
                    exc,
                )
                on_delta = True

        if not on_delta:
            lt = str(getattr(leg, "leg_type", "") or "").lower()
            skipped = ClosedLegResult(
                leg_id=int(leg.id) if getattr(leg, "id", None) is not None else None,
                leg_type=lt,
                symbol=str(getattr(leg, "symbol", "") or ""),
                product_id=pid,
                success=True,
                skipped=True,
                attempts=0,
                is_wing=is_wing_leg(leg),
                is_short=is_short_basket_leg(leg),
            )
            out.legs.append(skipped)
            continue

        row = await _close_one_leg_with_retries(
            leg=leg,
            order_executor=order_executor,
            delta_client=delta_client,
            reason=reason,
            trade_id=trade_id,
        )
        out.legs.append(row)
        if row.success and not row.skipped:
            if row.is_short:
                out.shorts_closed += 1
            elif row.is_wing:
                out.wings_closed += 1
            else:
                out.hedges_closed += 1
        if row.is_wing and not row.success:
            out.wings_failed.append(row)
            out.any_wing_fail = True

    return out


def both_shorts_closed(legs: list[Any]) -> bool:
    """True when no open short call/put remain (wings ignored)."""
    for leg in legs:
        if is_short_basket_leg(leg) and str(
            getattr(leg, "status", "open")
        ).lower() == "open":
            return False
    # Need at least evidence of short legs existing historically OR none open
    return True


def open_wing_legs(legs: list[Any]) -> list[Any]:
    return [
        leg
        for leg in legs
        if is_wing_leg(leg)
        and str(getattr(leg, "status", "open")).lower() == "open"
    ]


def get_open_wing_strikes(legs: list[Any]) -> tuple[float | None, float | None]:
    """Return (wing_call_strike, wing_put_strike) from open wing legs."""
    call_k: float | None = None
    put_k: float | None = None
    for leg in open_wing_legs(legs):
        lt = str(getattr(leg, "leg_type", "") or "").lower()
        try:
            k = float(getattr(leg, "strike", 0) or 0)
        except (TypeError, ValueError):
            continue
        if k <= 0:
            continue
        if "call" in lt:
            call_k = k
        elif "put" in lt:
            put_k = k
    return call_k, put_k


# ─── Cross-guard (adjustment) ───────────────────────────────────────────────


def clamp_short_strike_inside_wing(
    *,
    leg: str,
    wanted_strike: float,
    wing_strike: float | None,
    available_strikes: list[float],
    current_short_strike: float,
) -> tuple[float | None, str]:
    """
    Ensure adjusted short stays STRICTLY inside the wing (toward ATM).

    call: new_short < wing_call
    put:  new_short > wing_put

    Returns (strike_or_None, status) where status is:
      'ok' | 'clamped' | 'dead_end' | 'no_wing'
    """
    side = str(leg or "").lower().strip()
    wanted = float(wanted_strike)
    current = float(current_short_strike)
    if wing_strike is None:
        return wanted, "no_wing"
    wing = float(wing_strike)

    crosses = False
    if side == "call":
        crosses = wanted >= wing
    else:
        crosses = wanted <= wing

    if not crosses:
        return wanted, "ok"

    # Candidates strictly between current short and wing (ATM side of wing)
    cands: list[float] = []
    for s in available_strikes:
        k = float(s)
        if side == "call":
            # farther OTM than current, but strictly below wing
            if k > current and k < wing:
                cands.append(k)
        else:
            if k < current and k > wing:
                cands.append(k)

    if not cands:
        return None, "dead_end"

    # Closest to wanted while still inside wing
    if side == "call":
        # Prefer highest strike still < wing (closest to wing from inside)
        clamped = max(cands)
    else:
        clamped = min(cands)

    if abs(clamped - current) < 0.01:
        return None, "dead_end"

    logger.info(
        "[WING_CROSS_GUARD] leg=%s wanted=%s wing=%s clamped_to=%s",
        side,
        wanted,
        wing,
        clamped,
    )
    return float(clamped), "clamped"


def find_chain_row_for_strike(
    chain: list[dict[str, Any]],
    *,
    leg: str,
    strike: float,
) -> dict[str, Any] | None:
    mark_key = "call_mark_price" if leg == "call" else "put_mark_price"
    best = None
    best_diff = None
    for row in chain or []:
        try:
            k = float(row.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        diff = abs(k - float(strike))
        if diff < 0.01:
            return row
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = row
    if best is not None and best_diff is not None and best_diff < 1.0:
        return best
    return None
