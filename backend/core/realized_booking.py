"""Pure leg-close booking and trade realized_pnl recompute (no ORM)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from backend.core.delta_client import short_leg_realized_pnl
from backend.core.time_utils import get_utc_now

logger = logging.getLogger(__name__)


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


def pnl_sanity_check(
    *,
    trade_id: int,
    realized_pnl: float,
    last_gross_mtm: float | None,
    legs: list[Any] | None = None,
) -> bool:
    """
    Return True if OK. Log CRITICAL [PNL_SANITY_FAIL] when signs disagree.
    """
    from backend.config import PNL_SANITY_ABS_TOLERANCE_USD

    tol = float(PNL_SANITY_ABS_TOLERANCE_USD)
    gross = float(last_gross_mtm) if last_gross_mtm is not None else None
    realized = float(realized_pnl)
    if gross is None:
        return True
    if abs(gross) < tol or abs(realized) < tol:
        return True
    if (gross > 0 and realized > 0) or (gross < 0 and realized < 0):
        return True

    breakdown = []
    for leg in legs or []:
        breakdown.append(
            {
                "leg_id": int(getattr(leg, "id", 0) or 0),
                "leg_type": str(getattr(leg, "leg_type", "")),
                "entry": getattr(leg, "initial_premium", None),
                "exit": getattr(leg, "exit_premium", None),
                "realized": getattr(leg, "realized_pnl", None),
                "status": getattr(leg, "status", None),
            }
        )
    logger.critical(
        "[PNL_SANITY_FAIL] trade_id=%s realized_pnl=%.6f "
        "last_gross_mtm=%.6f legs=%s",
        trade_id,
        realized,
        gross,
        breakdown,
    )
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "PNL_SANITY_FAIL",
            int(trade_id),
            {
                "realized_pnl": round(realized, 6),
                "last_gross_mtm": round(gross, 6),
                "legs": breakdown,
            },
        )
    except Exception:
        pass
    return False
