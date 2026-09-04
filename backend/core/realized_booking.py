"""Pure leg-close booking and trade realized_pnl recompute (no ORM)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.core.delta_client import short_leg_realized_pnl
from backend.core.time_utils import get_utc_now

logger = logging.getLogger(__name__)

# Phased basket exit closes shorts then wings a few seconds apart (live ~3s).
# Legs within this window of the latest exit_time are treated as the final
# exit cohort that last_gross_mtm covered. Earlier closes = adjustments.
# Why exit_time (not adjustment_count alone): Leg has no adjustment marker;
# multiple Leg rows of the same type appear after adjustment. Clustering by
# max(exit_time) is the reliable way to separate prior closes from the
# final-exit set that gross MTM actually included.
_FINAL_EXIT_COHORT_SECONDS = 120.0


def recompute_trade_realized_from_legs(
    legs: list[Any],
    trade: Any,
    *,
    warn_unresolved: bool = True,
) -> float:
    """
    Set trade.realized_pnl = sum of closed bot-managed legs' realized_pnl.

    Legs with realized still unresolved (NULL) contribute 0.
    """
    trade_id = int(getattr(trade, "id", 0) or 0)
    total = 0.0
    for leg in legs:
        if not bool(getattr(leg, "is_bot_managed", True)):
            continue
        if str(getattr(leg, "status", "") or "").lower() != "closed":
            continue
        rp = getattr(leg, "realized_pnl", None)
        if rp is None:
            if warn_unresolved:
                logger.warning(
                    "[REALIZED_LEG_UNRESOLVED] trade=%s leg=%s leg_id=%s",
                    trade_id,
                    getattr(leg, "leg_type", "?"),
                    getattr(leg, "id", "?"),
                )
            continue
        total += float(rp)
    trade.realized_pnl = total
    return total


def basket_realized_breakdown(
    legs: list[Any], trade: Any
) -> dict[str, float | int]:
    """
    Per-basket gross-to-net realized breakdown for structure history.

    ``short_leg_realized_pnl`` is gross (fees not deducted). Net subtracts
    entry/exit fees and entry spread separately.
    """
    closed_bot = [
        lg
        for lg in legs
        if str(getattr(lg, "status", "") or "").lower() == "closed"
        and bool(getattr(lg, "is_bot_managed", True))
    ]
    gross = 0.0
    entry_fees = 0.0
    exit_fees = 0.0
    legs_unresolved = 0
    for lg in closed_bot:
        rp = getattr(lg, "realized_pnl", None)
        if rp is None:
            legs_unresolved += 1
        else:
            gross += float(rp)
        entry_fees += abs(float(getattr(lg, "entry_fee_usd", 0) or 0))
        exit_fees += abs(float(getattr(lg, "exit_fee_usd", 0) or 0))
    entry_spread = float(
        getattr(trade, "cumulative_entry_spread_usd", None) or 0.0
    )
    if entry_spread <= 0.0:
        entry_spread = sum(
            abs(float(getattr(lg, "entry_spread_usd", 0) or 0))
            for lg in legs
            if bool(getattr(lg, "is_bot_managed", True))
        )
    net = gross - entry_fees - exit_fees - entry_spread
    return {
        "gross_realized": round(gross, 4),
        "entry_fees_usd": round(entry_fees, 4),
        "exit_fees_usd": round(exit_fees, 4),
        "entry_spread_usd": round(entry_spread, 4),
        "net_realized": round(net, 4),
        "legs_unresolved": int(legs_unresolved),
    }


def book_leg_close(
    *,
    leg: Any,
    trade: Any,
    exit_premium: float | None,
    exit_time: datetime | None = None,
    exit_fee_usd: float | None = None,
    exit_order_id: str | None = None,
    recompute_fn: Callable[[list[Any], Any], float] | None = None,
    all_legs: list[Any] | None = None,
) -> float:
    """
    Mark leg closed and set leg.realized_pnl.

    When ``recompute_fn`` and ``all_legs`` are provided, recomputes
    ``trade.realized_pnl`` immediately after booking.

    Never overwrites an already-closed leg. Never writes exit_premium=0.0.
    """
    from backend.config import OPTIONS_CONTRACT_VALUE

    leg_id = int(getattr(leg, "id", 0) or 0)
    existing_status = str(getattr(leg, "status", "") or "").lower()
    if existing_status == "closed":
        existing_px = getattr(leg, "exit_premium", None)
        attempted = exit_premium
        logger.warning(
            "[LEG_BOOK_SKIP] leg_id=%s existing_exit_premium=%s "
            "attempted_exit_premium=%s — leaving row untouched",
            leg_id,
            existing_px,
            attempted,
        )
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "LEG_BOOK_SKIP",
                int(getattr(trade, "id", 0) or 0),
                {
                    "leg_id": leg_id,
                    "existing_exit_premium": existing_px,
                    "attempted_exit_premium": attempted,
                },
            )
        except Exception:
            pass
        return float(getattr(leg, "realized_pnl", None) or 0.0)

    now = exit_time or get_utc_now()

    def _maybe_recompute() -> None:
        if recompute_fn is not None and all_legs is not None:
            recompute_fn(all_legs, trade)

    if exit_premium is None or float(exit_premium) <= 0.0:
        leg.status = "closed"
        leg.exit_time = now
        leg.exit_premium = None
        leg.realized_pnl = None
        if exit_order_id is not None:
            leg.exit_order_id = str(exit_order_id)
        if exit_fee_usd is not None:
            leg.exit_fee_usd = abs(float(exit_fee_usd))
        note_tag = f"PNL_UNRESOLVED_{getattr(leg, 'leg_type', 'leg')}"
        prior_notes = str(getattr(trade, "notes", None) or "")
        if note_tag not in prior_notes:
            trade.notes = (
                f"{prior_notes};{note_tag}".strip(";")
                if prior_notes
                else note_tag
            )
        logger.critical(
            "[LEG_BOOK_UNRESOLVED] trade=%s leg_id=%s %s — exit_premium "
            "unavailable; booked NULL (not 0.0)",
            getattr(trade, "id", "?"),
            leg_id,
            getattr(leg, "leg_type", "?"),
        )
        _maybe_recompute()
        return 0.0

    exit_px = float(exit_premium)
    entry_px = float(getattr(leg, "initial_premium", 0) or 0.0)
    qty = abs(int(getattr(leg, "quantity", 0) or 0))
    cv = float(OPTIONS_CONTRACT_VALUE)
    if bool(getattr(leg, "is_long", False)):
        realized = (exit_px - entry_px) * qty * cv
    else:
        realized = short_leg_realized_pnl(
            entry_fill=entry_px,
            exit_fill=exit_px,
            quantity=qty,
        )
    leg.status = "closed"
    leg.exit_time = now
    leg.exit_premium = exit_px
    leg.realized_pnl = realized
    if exit_order_id is not None:
        leg.exit_order_id = str(exit_order_id)
    if exit_fee_usd is not None:
        leg.exit_fee_usd = abs(float(exit_fee_usd))
    _maybe_recompute()
    return realized


def _as_aware_utc(dt: Any) -> datetime | None:
    if dt is None or not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _split_legs_for_gross_mtm_compare(
    legs: list[Any],
) -> tuple[list[Any], list[Any]]:
    """
    Split legs into (final_exit_cohort, prior_closed).

    last_gross_mtm only includes legs that were OPEN when it was sampled
    (just before / during final exit). Adjustment/conversion closes create
    separate Leg rows with earlier exit_time — those realized dollars are
    in trade.realized_pnl but never in last_gross_mtm.

    Identification: no Adjustment marker on Leg. Use exit_time clustering —
    max(exit_time) is the final-exit anchor; legs within
    _FINAL_EXIT_COHORT_SECONDS of that anchor are the like-for-like set.
    Missing exit_time → treat as final cohort (just booked / still open).
    """
    if not legs:
        return [], []
    stamped: list[tuple[Any, datetime]] = []
    unstamped: list[Any] = []
    for leg in legs:
        et = _as_aware_utc(getattr(leg, "exit_time", None))
        if et is None:
            unstamped.append(leg)
        else:
            stamped.append((leg, et))
    if not stamped:
        return list(legs), []
    anchor = max(et for _, et in stamped)
    window = timedelta(seconds=float(_FINAL_EXIT_COHORT_SECONDS))
    cohort: list[Any] = list(unstamped)
    prior: list[Any] = []
    for leg, et in stamped:
        if anchor - et <= window:
            cohort.append(leg)
        else:
            prior.append(leg)
    return cohort, prior


def _sum_leg_realized(legs: list[Any]) -> float:
    total = 0.0
    for leg in legs:
        try:
            total += float(getattr(leg, "realized_pnl", None) or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def pnl_sanity_check(
    *,
    trade_id: int,
    realized_pnl: float,
    last_gross_mtm: float | None,
    legs: list[Any] | None = None,
    adjustment_count: int | None = None,
) -> bool:
    """
    Return True if OK. Log CRITICAL [PNL_SANITY_FAIL] when signs disagree.

    Compares last_gross_mtm to the realized sum of legs that were still open
    at final exit (exit_time cohort), NOT the full trade.realized_pnl which
    also includes earlier adjustment closes. Sign-disagreement on that
    like-for-like pair still fails — this does not weaken the check.
    """
    from backend.config import PNL_SANITY_ABS_TOLERANCE_USD

    tol = float(PNL_SANITY_ABS_TOLERANCE_USD)
    gross = float(last_gross_mtm) if last_gross_mtm is not None else None
    total_realized = float(realized_pnl)
    if gross is None:
        return True

    legs_list = list(legs or [])
    cohort, prior = _split_legs_for_gross_mtm_compare(legs_list)
    prior_realized = _sum_leg_realized(prior)
    if legs_list:
        # Like-for-like: only legs that shared the gross MTM snapshot
        compare_realized = _sum_leg_realized(cohort)
    else:
        # No leg detail — fall back to trade total (legacy callers/tests)
        compare_realized = total_realized

    adj_count = int(adjustment_count) if adjustment_count is not None else 0

    if abs(gross) < tol or abs(compare_realized) < tol:
        return True
    if (gross > 0 and compare_realized > 0) or (
        gross < 0 and compare_realized < 0
    ):
        return True

    breakdown = []
    prior_ids = {int(getattr(leg, "id", 0) or 0) for leg in prior}
    for leg in legs_list:
        lid = int(getattr(leg, "id", 0) or 0)
        breakdown.append(
            {
                "leg_id": lid,
                "leg_type": str(getattr(leg, "leg_type", "")),
                "entry": getattr(leg, "initial_premium", None),
                "exit": getattr(leg, "exit_premium", None),
                "realized": getattr(leg, "realized_pnl", None),
                "status": getattr(leg, "status", None),
                "exit_time": str(getattr(leg, "exit_time", None) or ""),
                "cohort": "prior_closed" if lid in prior_ids else "final_exit",
            }
        )
    logger.critical(
        "[PNL_SANITY_FAIL] trade_id=%s total_realized=%.6f "
        "compare_realized=%.6f last_gross_mtm=%.6f "
        "prior_closed_legs=%s prior_realized=%.6f "
        "final_cohort_legs=%s adjustment_count=%s legs=%s",
        trade_id,
        total_realized,
        compare_realized,
        gross,
        len(prior),
        prior_realized,
        len(cohort),
        adj_count,
        breakdown,
    )
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "PNL_SANITY_FAIL",
            int(trade_id),
            {
                "total_realized_pnl": round(total_realized, 6),
                "compare_realized": round(compare_realized, 6),
                "last_gross_mtm": round(gross, 6),
                "prior_closed_legs": len(prior),
                "prior_realized": round(prior_realized, 6),
                "final_cohort_legs": len(cohort),
                "adjustment_count": adj_count,
                "legs": breakdown,
            },
        )
    except Exception:
        pass
    return False
