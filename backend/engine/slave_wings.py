# slave_wings.py — Slave/mirror wing helpers (reuse master wing_entry / wing_exit)
#
# NIYAM 0: never leave slave with shorts and no wings when master is a condor.
# Entry order: wing BUY → wing BUY → short SELL → short SELL
# Exit / unwind order: shorts first, then wings.
# Strikes always copied from master — never re-select on slave.

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from backend.core.time_utils import get_utc_now
from backend.engine.wing_entry import (
    ENTRY_LEG_MAX_ATTEMPTS,
    EntryGuardBlock,
    EntryLegSpec,
    EntryPartialUnwind,
    FilledEntryLeg,
    build_entry_order_plan,
    is_full_fill,
    place_leg_with_retries,
)
from backend.strategies.base_strategy import OrderResult

logger = logging.getLogger(__name__)

PlaceOrderFn = Callable[..., Awaitable[OrderResult]]


@dataclass
class SlaveWingAbort:
    """Wing entry failed — shorts must NOT be placed; filled wings unwound."""

    reason: str
    failed_role: str
    filled_wings: list[FilledEntryLeg] = field(default_factory=list)
    wings_closed: int = 0
    wings_failed: int = 0


def wings_enabled_from_master_picks(
    wing_call: dict[str, Any] | None,
    wing_put: dict[str, Any] | None,
) -> bool:
    """True only when BOTH wing picks have product_id (master condor)."""
    if wing_call is None or wing_put is None:
        return False
    try:
        return int(wing_call.get("product_id") or 0) > 0 and int(
            wing_put.get("product_id") or 0
        ) > 0
    except (TypeError, ValueError):
        return False


def build_slave_entry_plan(
    *,
    slave_qty: int,
    call_product_id: int,
    put_product_id: int,
    call_symbol: str,
    put_symbol: str,
    call_strike: float,
    put_strike: float,
    call_premium: float,
    put_premium: float,
    wing_call: dict[str, Any] | None,
    wing_put: dict[str, Any] | None,
    call_bracket_sl: float | None = None,
    call_bracket_limit: float | None = None,
    put_bracket_sl: float | None = None,
    put_bracket_limit: float | None = None,
) -> list[EntryLegSpec]:
    """
    Ordered entry plan for one slave. Qty = slave_qty (1:1 wings:shorts).
    Wings use master's strikes/product_ids — never reselected.
    """
    enabled = wings_enabled_from_master_picks(wing_call, wing_put)
    straddle = {
        "call_product_id": int(call_product_id),
        "put_product_id": int(put_product_id),
        "call_symbol": str(call_symbol),
        "put_symbol": str(put_symbol),
        "call_strike": float(call_strike),
        "put_strike": float(put_strike),
        "call_premium": float(call_premium or 0.0),
        "put_premium": float(put_premium or 0.0),
    }
    return build_entry_order_plan(
        qty=max(1, int(slave_qty)),
        straddle=straddle,
        wing_call=wing_call if enabled else None,
        wing_put=wing_put if enabled else None,
        wings_enabled=enabled,
        call_bracket_sl=call_bracket_sl,
        call_bracket_limit=call_bracket_limit,
        put_bracket_sl=put_bracket_sl,
        put_bracket_limit=put_bracket_limit,
    )


def entry_roles_order(plan: list[EntryLegSpec]) -> list[str]:
    return [p.role for p in plan]


def sort_unwind_filled_legs(
    filled: list[FilledEntryLeg],
) -> list[FilledEntryLeg]:
    """Shorts first, then wings (same as wing_entry.unwind_partial_entry)."""

    def _key(leg: FilledEntryLeg) -> tuple[int, int, str]:
        group = 0 if not leg.is_long else 1
        side = 0 if "call" in leg.role else 1 if "put" in leg.role else 9
        return (group, side, leg.role)

    return sorted(filled, key=_key)


def sort_unwind_dicts(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dict form used by MirrorEngine._unwind_slave_entry_legs."""

    def _key(item: dict[str, Any]) -> tuple[int, int, str]:
        leg = str(item.get("leg") or "").lower()
        is_wing = leg.startswith("wing")
        group = 1 if is_wing else 0
        side = 0 if "call" in leg else 1 if "put" in leg else 9
        return (group, side, leg)

    return sorted(legs, key=_key)


def sort_exit_targets(
    targets: list[dict[str, Any]],
    *,
    short_pids: set[int],
    wing_pids: set[int],
) -> list[dict[str, Any]]:
    """
    Close order: shorts first, then wings, then other longs.
    Protects against naked shorts during exit (entry was wings-first).
    """

    def _key(pos: dict[str, Any]) -> tuple[int, int]:
        try:
            pid = int(pos.get("product_id") or 0)
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            return (9, 0)
        if pid in short_pids or size < 0:
            return (0, pid)
        if pid in wing_pids:
            return (1, pid)
        if size > 0:
            return (2, pid)
        return (3, pid)

    return sorted(targets, key=_key)


def both_shorts_flat_on_book(
    *,
    call_pid: int,
    put_pid: int,
    positions: list[dict[str, Any]],
) -> bool:
    """True when neither short product has non-zero size on the book."""

    def _size(pid: int) -> float:
        for p in positions:
            try:
                if int(p.get("product_id") or 0) == int(pid):
                    return float(p.get("size") or 0)
            except (TypeError, ValueError):
                continue
        return 0.0

    if call_pid <= 0 or put_pid <= 0:
        return False
    return abs(_size(call_pid)) <= 1e-9 and abs(_size(put_pid)) <= 1e-9


def should_orphan_close_wings(
    *,
    call_open: bool,
    put_open: bool,
    wings_present: bool,
) -> bool:
    """Orphan hook: both shorts closed → close wings. One short open → leave."""
    if not wings_present:
        return False
    return (not call_open) and (not put_open)


def filled_leg_to_unwind_dict(leg: FilledEntryLeg) -> dict[str, Any]:
    """Convert FilledEntryLeg → _unwind_slave_entry_legs item."""
    qty = max(1, int(leg.filled_size or leg.requested_qty or 1))
    # Long wing → positive size; short → negative
    signed = float(qty) if leg.is_long else -float(qty)
    return {
        "leg": str(leg.role),
        "product_id": int(leg.product_id),
        "signed_size": signed,
        "opened_at": None,
        "fill_at": None,
        "order_id": leg.order_id,
        "symbol": str(leg.symbol or ""),
        "strike": float(leg.strike or 0),
        "quantity": qty,
    }


def ledger_role_for_slave_leg(leg_name: str) -> tuple[str, str]:
    """
    Map leg name → (structure_ledger role, side).
    Returns ("", "") for unknown.
    """
    from backend.engine.structure_ledger import (
        ROLE_BASKET_CALL,
        ROLE_BASKET_PUT,
        ROLE_BASKET_WING_CALL,
        ROLE_BASKET_WING_PUT,
    )

    name = str(leg_name or "").lower()
    if name == "call":
        return ROLE_BASKET_CALL, "SELL"
    if name == "put":
        return ROLE_BASKET_PUT, "SELL"
    if name == "wing_call":
        return ROLE_BASKET_WING_CALL, "BUY"
    if name == "wing_put":
        return ROLE_BASKET_WING_PUT, "BUY"
    return "", ""


def _append_filled_or_partial(
    *,
    filled: list[FilledEntryLeg],
    spec: EntryLegSpec,
    result: OrderResult,
    opened_at: Any,
) -> None:
    """Append FilledEntryLeg when result has any size; used before unwind raise."""
    partial_size = 0
    if result.success:
        try:
            partial_size = int(getattr(result, "filled_size", 0) or 0)
        except (TypeError, ValueError):
            partial_size = 0
    if partial_size <= 0 and is_full_fill(result, int(spec.quantity)):
        partial_size = int(spec.quantity)
    if partial_size <= 0:
        return
    filled.append(
        FilledEntryLeg(
            role=spec.role,
            product_id=int(spec.product_id),
            symbol=str(spec.symbol),
            strike=float(spec.strike),
            requested_qty=int(spec.quantity),
            filled_size=partial_size,
            fill_price=float(result.filled_price or spec.mark_premium),
            order_id=(
                str(result.order_id) if result.order_id is not None else None
            ),
            commission=(
                abs(float(result.commission))
                if result.commission is not None
                else None
            ),
            is_long=bool(spec.is_long),
            mark_premium=float(spec.mark_premium),
            opened_at=opened_at,
        )
    )


async def place_slave_plan_legs(
    *,
    plan: list[EntryLegSpec],
    place_fn_for_spec: Callable[[EntryLegSpec], PlaceOrderFn],
    slave_name: str,
    max_attempts: int = ENTRY_LEG_MAX_ATTEMPTS,
    delta_client: Any | None = None,
    master_trade_id: int | None = None,
) -> list[FilledEntryLeg]:
    """
    Place plan legs in role-based consecutive groups (master 090ce05 pattern):
      wings ON  -> [[wing_call, wing_put], [call, put]]
      wings OFF -> [[call, put]]
    Groups are SEQUENTIAL (margin); legs inside a group are paired via
    execute_paired_legs when mid-price is allowed. Market path keeps
    place_leg_with_retries per leg.

    On incomplete fill after retries / pair: raise EntryPartialUnwind with
    filled_legs so far. Logs [SLAVE_WING_ENTRY] for each successful wing fill.
    """
    from backend.database import SessionLocal, get_or_create_auto_settings
    from backend.engine.midprice_executor import (
        clamp_chase_max_seconds,
        clamp_hold_seconds,
        clamp_partner_window_seconds,
        execute_paired_legs,
        should_use_midprice,
    )

    filled: list[FilledEntryLeg] = []
    wings_on = any(str(s.role).startswith("wing") for s in plan)
    entry_reason = (
        "SLAVE_CONDOR_ENTRY" if wings_on else "SLAVE_BASKET_ENTRY"
    )

    mp_on = False
    chase_max = None
    hold_s = None
    partner_win_s = None
    entry_tol = 15.0
    try:
        with SessionLocal() as _db:
            settings = get_or_create_auto_settings(_db)
            mp_on = bool(getattr(settings, "midprice_enabled", False))
            chase_max = clamp_chase_max_seconds(
                getattr(settings, "midprice_chase_max_seconds", None)
            )
            hold_s = clamp_hold_seconds(
                getattr(settings, "midprice_hold_seconds", None)
            )
            partner_win_s = clamp_partner_window_seconds(
                getattr(settings, "midprice_partner_window_seconds", None)
            )
            entry_tol = float(
                getattr(settings, "entry_premium_match_tolerance_pct", None)
                or 15.0
            )
    except Exception as exc:
        logger.warning(
            "slave midprice settings load failed slave=%s: %s — market path",
            slave_name,
            exc,
        )
        mp_on = False

    use_mp = bool(
        delta_client is not None
        and should_use_midprice(enabled=mp_on, reason=entry_reason)
    )
    selection_ts = time.monotonic()

    # Role-based consecutive groups (never by bare index)
    _wing_roles = frozenset({"wing_call", "wing_put"})
    entry_groups: list[tuple[str, list[EntryLegSpec]]] = []
    for _spec in plan:
        _gkey = "wing" if str(_spec.role) in _wing_roles else "short"
        if not entry_groups or entry_groups[-1][0] != _gkey:
            entry_groups.append((_gkey, [_spec]))
        else:
            entry_groups[-1][1].append(_spec)

    for phase, group_specs in entry_groups:
        placements: list[tuple[EntryLegSpec, OrderResult, Any]] = []

        if use_mp:
            # RULE 8: opened_at MUST be pre-placement for the whole group
            group_open_ts = get_utc_now()
            pair_results = await execute_paired_legs(
                legs=[
                    {
                        "product_id": int(s.product_id),
                        "side": "buy" if s.is_long else "sell",
                        "quantity": int(s.quantity),
                        "symbol": str(s.symbol),
                        "leg_label": str(s.role),
                        "selected_premium": float(s.mark_premium or 0) or None,
                        "bracket_sl_price": s.bracket_sl_price,
                        "bracket_sl_limit": s.bracket_sl_limit,
                    }
                    for s in group_specs
                ],
                delta_client=delta_client,
                reason=entry_reason,
                midprice_enabled=True,
                max_chase_seconds=chase_max,
                hold_seconds=hold_s,
                partner_window_seconds=partner_win_s,
                entry_premium_match_tolerance_pct=entry_tol,
                selection_ts=selection_ts,
                trade_id=master_trade_id,
                phase=f"slave_{phase}",
            )
            if len(pair_results) != len(group_specs):
                raise EntryPartialUnwind(
                    f"Slave entry {phase} pair returned "
                    f"{len(pair_results)} results for {len(group_specs)} legs",
                    filled_legs=list(filled),
                    failed_role=str(group_specs[0].role),
                )
            for s, res in zip(group_specs, pair_results):
                placements.append((s, res, group_open_ts))
        else:
            # Market path — sequential per leg (unchanged behaviour)
            for spec in group_specs:
                place_fn = place_fn_for_spec(spec)
                # RULE 8 / structure_ledger: opened_at MUST be pre-placement
                leg_opened_at = get_utc_now()
                result = await place_leg_with_retries(
                    role=spec.role,
                    requested=int(spec.quantity),
                    place_fn=place_fn,
                    max_attempts=max_attempts,
                )
                placements.append((spec, result, leg_opened_at))

        # Commit every leg in this group before the next group starts.
        for spec, result, opened_at in placements:
            if not is_full_fill(result, int(spec.quantity)):
                _append_filled_or_partial(
                    filled=filled,
                    spec=spec,
                    result=result,
                    opened_at=opened_at,
                )
                raise EntryPartialUnwind(
                    f"Slave entry leg {spec.role} incomplete: "
                    f"{result.error or 'partial_fill'}",
                    filled_legs=list(filled),
                    failed_role=spec.role,
                )

            fill_px = float(result.filled_price or 0.0) or float(
                spec.mark_premium
            )
            oid = (
                str(result.order_id) if result.order_id is not None else None
            )
            fee = (
                abs(float(result.commission))
                if result.commission is not None
                else None
            )
            filled_size = int(
                getattr(result, "filled_size", None) or spec.quantity
            )
            filled.append(
                FilledEntryLeg(
                    role=spec.role,
                    product_id=int(spec.product_id),
                    symbol=str(spec.symbol),
                    strike=float(spec.strike),
                    requested_qty=int(spec.quantity),
                    filled_size=filled_size,
                    fill_price=fill_px,
                    order_id=oid,
                    commission=fee,
                    is_long=bool(spec.is_long),
                    mark_premium=float(spec.mark_premium),
                    opened_at=opened_at,
                )
            )
            if spec.role.startswith("wing"):
                logger.info(
                    "[SLAVE_WING_ENTRY] slave=%s leg=%s strike=%s qty=%s "
                    "fill=%s profile=%s",
                    slave_name,
                    spec.role,
                    spec.strike,
                    filled_size,
                    fill_px,
                    "paired_mid" if use_mp else "market",
                )

    return filled


def log_slave_wing_entry_abort(
    *,
    slave_name: str,
    reason: str,
    wings_closed: int,
    wings_failed: int = 0,
) -> None:
    logger.critical(
        "[SLAVE_WING_ENTRY_ABORT] slave=%s reason=%s wings_closed=%s "
        "wings_failed=%s",
        slave_name,
        reason,
        wings_closed,
        wings_failed,
    )


def wing_dicts_from_slave_trade(st: Any) -> tuple[set[int], set[int]]:
    """(short_pids, wing_pids) from SlaveTrade columns."""
    shorts: set[int] = set()
    wings: set[int] = set()
    for attr, bucket in (
        ("call_product_id", shorts),
        ("put_product_id", shorts),
        ("wing_call_product_id", wings),
        ("wing_put_product_id", wings),
    ):
        try:
            pid = int(getattr(st, attr, 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            bucket.add(pid)
    return shorts, wings


def assert_reduce_only_on_all_place_calls(place_order_calls: list[Any]) -> None:
    """Test helper — every close retry must pass reduce_only=True."""
    for call in place_order_calls:
        kwargs = getattr(call, "kwargs", None) or {}
        if not kwargs and len(getattr(call, "args", ())) >= 1:
            continue
        assert kwargs.get("reduce_only") is True, (
            f"reduce_only missing/dropped on place_order: {kwargs}"
        )


# Re-export for callers / tests
__all__ = [
    "EntryGuardBlock",
    "EntryPartialUnwind",
    "FilledEntryLeg",
    "SlaveWingAbort",
    "assert_reduce_only_on_all_place_calls",
    "both_shorts_flat_on_book",
    "build_slave_entry_plan",
    "entry_roles_order",
    "filled_leg_to_unwind_dict",
    "ledger_role_for_slave_leg",
    "log_slave_wing_entry_abort",
    "place_slave_plan_legs",
    "should_orphan_close_wings",
    "sort_exit_targets",
    "sort_unwind_dicts",
    "sort_unwind_filled_legs",
    "wing_dicts_from_slave_trade",
    "wings_enabled_from_master_picks",
]
