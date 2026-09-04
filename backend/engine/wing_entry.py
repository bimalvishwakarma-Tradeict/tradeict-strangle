# wing_entry.py — Master basket wing entry sequencing + partial-fill unwind
#
# Order when basket_wings_enabled:
#   1) BUY wing call, BUY wing put  (no bracket SL)
#   2) SELL short call, SELL short put  (bracket SL as today)
#
# Partial = order failed OR filled_size < requested. Max 3 attempts per leg.
# Unwind SAFE ORDER: shorts first, then wings (never reverse).

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from backend.strategies.base_strategy import OrderResult

logger = logging.getLogger(__name__)

ENTRY_LEG_MAX_ATTEMPTS = 3


@dataclass
class EntryLegSpec:
    """One leg in the entry order sequence."""

    role: str  # wing_call | wing_put | call | put
    product_id: int
    symbol: str
    strike: float
    quantity: int
    is_long: bool
    mark_premium: float
    bracket_sl_price: float | None = None
    bracket_sl_limit: float | None = None


@dataclass
class FilledEntryLeg:
    """Successfully filled (or partially filled) entry leg awaiting DB persist / unwind."""

    role: str
    product_id: int
    symbol: str
    strike: float
    requested_qty: int
    filled_size: int
    fill_price: float
    order_id: str | None
    commission: float | None
    is_long: bool
    mark_premium: float
    sl_trigger_price: float | None = None
    # Captured immediately BEFORE place_order — used as ledger opened_at
    # (post-fill timestamps break earner attribution windows). Optional so
    # existing callers that omit it keep compiling.
    opened_at: datetime | None = None


@dataclass
class UnwindResult:
    legs_closed: int = 0
    legs_failed: int = 0
    failures: list[str] = field(default_factory=list)


class EntryGuardBlock(Exception):
    """Structural guard — no orders must be sent."""

    def __init__(self, guard: str, leg: str | None = None) -> None:
        self.guard = guard
        self.leg = leg
        msg = f"guard={guard}"
        if leg:
            msg += f" leg={leg}"
        super().__init__(msg)


class EntryPartialUnwind(Exception):
    """Raised after all retries exhausted; caller must unwind filled legs."""

    def __init__(
        self,
        message: str,
        *,
        filled_legs: list[FilledEntryLeg],
        failed_role: str,
    ) -> None:
        self.filled_legs = filled_legs
        self.failed_role = failed_role
        super().__init__(message)


def build_entry_order_plan(
    *,
    qty: int,
    straddle: dict[str, Any],
    wing_call: dict[str, Any] | None,
    wing_put: dict[str, Any] | None,
    wings_enabled: bool,
    call_bracket_sl: float | None = None,
    call_bracket_limit: float | None = None,
    put_bracket_sl: float | None = None,
    put_bracket_limit: float | None = None,
) -> list[EntryLegSpec]:
    """
    Build ordered entry legs.

    Wings first (both BUY), then shorts (both SELL). When wings disabled,
    only shorts — identical to pre-wings flow.
    """
    plan: list[EntryLegSpec] = []
    q = max(1, int(qty))

    if wings_enabled:
        if wing_call is None:
            raise EntryGuardBlock("no_wing_strike", "call")
        if wing_put is None:
            raise EntryGuardBlock("no_wing_strike", "put")
        plan.append(
            EntryLegSpec(
                role="wing_call",
                product_id=int(wing_call["product_id"]),
                symbol=str(wing_call["symbol"]),
                strike=float(wing_call["strike"]),
                quantity=q,
                is_long=True,
                mark_premium=float(wing_call.get("premium") or 0.0),
            )
        )
        plan.append(
            EntryLegSpec(
                role="wing_put",
                product_id=int(wing_put["product_id"]),
                symbol=str(wing_put["symbol"]),
                strike=float(wing_put["strike"]),
                quantity=q,
                is_long=True,
                mark_premium=float(wing_put.get("premium") or 0.0),
            )
        )

    plan.append(
        EntryLegSpec(
            role="call",
            product_id=int(straddle["call_product_id"]),
            symbol=str(straddle["call_symbol"]),
            strike=float(straddle.get("call_strike", straddle.get("strike"))),
            quantity=q,
            is_long=False,
            mark_premium=float(straddle["call_premium"]),
            bracket_sl_price=call_bracket_sl,
            bracket_sl_limit=call_bracket_limit,
        )
    )
    plan.append(
        EntryLegSpec(
            role="put",
            product_id=int(straddle["put_product_id"]),
            symbol=str(straddle["put_symbol"]),
            strike=float(straddle.get("put_strike", straddle.get("strike"))),
            quantity=q,
            is_long=False,
            mark_premium=float(straddle["put_premium"]),
            bracket_sl_price=put_bracket_sl,
            bracket_sl_limit=put_bracket_limit,
        )
    )
    return plan


def is_full_fill(result: OrderResult, requested: int) -> bool:
    """True when order succeeded and filled size covers the request."""
    if not result.success:
        return False
    filled = getattr(result, "filled_size", None)
    if filled is None:
        # Legacy / mock results without filled_size — treat success as full
        return True
    try:
        return int(filled) >= int(requested)
    except (TypeError, ValueError):
        return False


PlaceFn = Callable[..., Awaitable[OrderResult]]


async def place_leg_with_retries(
    *,
    role: str,
    requested: int,
    place_fn: PlaceFn,
    max_attempts: int = ENTRY_LEG_MAX_ATTEMPTS,
) -> OrderResult:
    """
    Place one entry leg up to max_attempts times.

    Partial = failure OR filled_size < requested. Logs [ENTRY_PARTIAL_RETRY].
    """
    last: OrderResult | None = None
    for attempt in range(1, max_attempts + 1):
        last = await place_fn()
        filled = 0
        if last is not None:
            raw_filled = getattr(last, "filled_size", None)
            if raw_filled is not None:
                try:
                    filled = int(raw_filled)
                except (TypeError, ValueError):
                    filled = 0
            elif last.success:
                filled = int(requested)

        if last is not None and is_full_fill(last, requested):
            return last

        err = (last.error if last else None) or "partial_or_failed"
        logger.warning(
            "[ENTRY_PARTIAL_RETRY] leg=%s attempt=%s requested=%s filled=%s error=%s",
            role,
            attempt,
            requested,
            filled,
            err,
        )
    assert last is not None
    return last


def _unwind_sort_key(leg: FilledEntryLeg) -> tuple[int, str]:
    # Shorts (is_long=False) first, then wings. Within group: call before put.
    group = 0 if not leg.is_long else 1
    role_order = {"call": 0, "put": 1, "wing_call": 0, "wing_put": 1}
    return (group, role_order.get(leg.role, 9), leg.role)


async def unwind_partial_entry(
    *,
    order_executor: Any,
    delta_client: Any,
    filled_legs: list[FilledEntryLeg],
    trade_id: int | None = None,
) -> UnwindResult:
    """
    Close every filled entry leg. SAFE ORDER: shorts first, then wings.
    """
    result = UnwindResult()
    ordered = sorted(filled_legs, key=_unwind_sort_key)
    for leg in ordered:
        qty = max(0, int(leg.filled_size or 0))
        if qty <= 0:
            continue
        try:
            if leg.is_long:
                close_res = await order_executor.close_long_position(
                    product_id=int(leg.product_id),
                    quantity=qty,
                    delta_client=delta_client,
                    symbol_for_fallback=str(leg.symbol),
                )
            else:
                # Synthetic leg object for close_leg
                synth = type(
                    "SynthLeg",
                    (),
                    {
                        "is_bot_managed": True,
                        "status": "open",
                        "id": None,
                        "leg_type": leg.role,
                        "symbol": leg.symbol,
                        "quantity": qty,
                        "product_id": leg.product_id,
                        "exit_premium": None,
                        "delta_order_id": leg.order_id,
                    },
                )()
                close_res = await order_executor.close_leg(synth, delta_client)

            if close_res.success:
                result.legs_closed += 1
            else:
                result.legs_failed += 1
                msg = (
                    f"{leg.role} product={leg.product_id}: "
                    f"{close_res.error or 'unknown'}"
                )
                result.failures.append(msg)
                logger.critical(
                    "[ENTRY_PARTIAL_UNWIND] close FAILED trade=%s leg=%s: %s",
                    trade_id if trade_id is not None else "?",
                    leg.role,
                    msg,
                )
        except Exception as exc:
            result.legs_failed += 1
            msg = f"{leg.role} product={leg.product_id}: {exc}"
            result.failures.append(msg)
            logger.critical(
                "[ENTRY_PARTIAL_UNWIND] close EXCEPTION trade=%s leg=%s: %s",
                trade_id if trade_id is not None else "?",
                leg.role,
                exc,
                exc_info=True,
            )

    logger.warning(
        "[ENTRY_PARTIAL_UNWIND] trade=%s legs_closed=%s legs_failed=%s",
        trade_id if trade_id is not None else "?",
        result.legs_closed,
        result.legs_failed,
    )
    return result


def resolve_adjustment_qty_mode(settings: Any) -> str:
    """
    Three-way mode with migration from deprecated use_dynamic_qty_on_adjustment.
    """
    raw = getattr(settings, "adjustment_qty_mode", None)
    if raw is not None:
        mode = str(raw).lower().strip()
        if mode in {"unchanged", "increase_dynamic", "decrease_step"}:
            return mode
    if bool(getattr(settings, "use_dynamic_qty_on_adjustment", False)):
        return "increase_dynamic"
    return "unchanged"


def compute_decrease_step_qty(
    *,
    original_qty: int,
    adjustment_number: int,
    decrease_pct: float,
) -> tuple[int | None, bool]:
    """
    remaining = 1 − (decrease_pct/100 × adjustment_number)
    new_qty   = max(1, floor(original_qty × remaining))

    Returns (new_qty, close_basket). close_basket when remaining <= 0.
    """
    import math

    orig = max(1, int(original_qty))
    adj_n = max(1, int(adjustment_number))
    pct = float(decrease_pct)
    remaining = 1.0 - (pct / 100.0) * float(adj_n)
    if remaining <= 0:
        return None, True
    new_qty = max(1, int(math.floor(orig * remaining)))
    return new_qty, False
