# db_audit.py — Startup DB consistency checks (orphan legs, zombie trades, baselines)

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import func

from backend.config import TradeStatus
from backend.core.time_utils import get_ist_now
from backend.models import Leg, SlaveAccount, SlaveTrade, Trade

logger = logging.getLogger(__name__)

# Exit reason written when we close a zombie ACTIVE trade with zero open legs.
DB_CONSISTENCY_FIX_NO_LEGS = "DB_CONSISTENCY_FIX_NO_LEGS"


async def verify_db_consistency(
    db_factory: Callable[[], Any],
) -> tuple[int, int]:
    """
    One-shot startup audit. Auto-fixes safe inconsistencies; warns on others.

    Returns:
        (fixes_applied, warnings_for_manual_review)
    """
    logger.info("Running DB consistency audit...")
    fixes = 0
    warnings = 0

    with db_factory() as db:
        # CHECK 1: Active trades with NO open legs (or only one = partial orphan)
        active_trades = (
            db.query(Trade)
            .filter(Trade.status == TradeStatus.ACTIVE.value)
            .all()
        )

        for trade in active_trades:
            open_legs = (
                db.query(Leg)
                .filter(Leg.trade_id == trade.id, Leg.status == "open")
                .all()
            )

            if len(open_legs) == 0:
                logger.critical(
                    "[DB_AUDIT] Trade %s is 'active' but has NO open legs! "
                    "Marking closed.",
                    trade.id,
                )
                trade.status = TradeStatus.CLOSED.value
                trade.exit_reason = DB_CONSISTENCY_FIX_NO_LEGS
                trade.exit_time = get_ist_now()
                if trade.realized_pnl is None:
                    trade.realized_pnl = 0.0
                db.commit()
                fixes += 1
                continue

            if len(open_legs) == 1:
                logger.warning(
                    "[DB_AUDIT] Trade %s has only 1 open leg: %s %s. "
                    "Possible partial adjustment orphan.",
                    trade.id,
                    open_legs[0].leg_type,
                    open_legs[0].symbol,
                )
                warnings += 1

            # CHECK 2: Open legs — invalid product_id / trigger baseline
            for leg in open_legs:
                if not leg.product_id or int(leg.product_id) == 0:
                    logger.error(
                        "[DB_AUDIT] Leg %s (%s) has invalid product_id=%s!",
                        leg.id,
                        leg.symbol,
                        leg.product_id,
                    )
                    warnings += 1

                baseline = getattr(leg, "trigger_baseline_premium", None)
                if baseline is None or float(baseline) <= 0:
                    initial = float(leg.initial_premium or 0.0)
                    logger.warning(
                        "[DB_AUDIT] Leg %s (%s) has invalid "
                        "trigger_baseline=%s. Resetting to "
                        "initial_premium=%s",
                        leg.id,
                        leg.symbol,
                        baseline,
                        initial,
                    )
                    leg.trigger_baseline_premium = initial
                    # Keep legacy alias in sync when present
                    if hasattr(leg, "trigger_premium"):
                        leg.trigger_premium = initial
                    db.commit()
                    fixes += 1

        # CHECK 3: Non-active trades with OPEN legs (orphan legs)
        closed_trades = (
            db.query(Trade)
            .filter(Trade.status != TradeStatus.ACTIVE.value)
            .all()
        )

        for trade in closed_trades:
            orphan_legs = (
                db.query(Leg)
                .filter(Leg.trade_id == trade.id, Leg.status == "open")
                .all()
            )
            if not orphan_legs:
                continue

            logger.warning(
                "[DB_AUDIT] Trade %s (%s) has %s orphan open legs: %s",
                trade.id,
                trade.status,
                len(orphan_legs),
                [leg.symbol for leg in orphan_legs],
            )
            now = get_ist_now()
            for leg in orphan_legs:
                leg.status = "closed"
                leg.exit_time = now
                if leg.exit_premium is None:
                    leg.exit_premium = 0.0
            db.commit()
            fixes += 1

        # CHECK 4: SlaveTrades still active for closed master trades
        closed_trade_ids = [
            int(t.id)
            for t in db.query(Trade)
            .filter(Trade.status != TradeStatus.ACTIVE.value)
            .all()
        ]

        if closed_trade_ids:
            orphan_slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id.in_(closed_trade_ids),
                    SlaveTrade.status == "active",
                )
                .all()
            )
            from backend.engine.mirror_engine import is_virtual_slave_trade

            closed_orphans = 0
            for st in orphan_slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == st.slave_account_id)
                    .first()
                )
                if is_virtual_slave_trade(slave, st):
                    logger.warning(
                        "[DB_AUDIT] Skipping auto-close of virtual "
                        "SlaveTrade %s (master Trade %s closed) — "
                        "leave status=active until intentional exit",
                        st.id,
                        st.master_trade_id,
                    )
                    warnings += 1
                    continue
                logger.warning(
                    "[DB_AUDIT] SlaveTrade %s is 'active' but master "
                    "Trade %s is closed. Fixing.",
                    st.id,
                    st.master_trade_id,
                )
                st.status = "closed"
                closed_orphans += 1
                fixes += 1
            if closed_orphans:
                db.commit()

        # CHECK 5: Duplicate active trades for same underlying
        duplicates = (
            db.query(Trade.underlying, func.count(Trade.id).label("count"))
            .filter(Trade.status == TradeStatus.ACTIVE.value)
            .group_by(Trade.underlying)
            .having(func.count(Trade.id) > 1)
            .all()
        )
        for dup in duplicates:
            logger.critical(
                "[DB_AUDIT] DUPLICATE active trades for %s! Count: %s. "
                "Manual review needed.",
                dup.underlying,
                dup.count,
            )
            warnings += 1

        # CHECK 6: Active slave trades with repeated errors
        error_slave_trades = (
            db.query(SlaveTrade)
            .filter(
                SlaveTrade.status == "active",
                SlaveTrade.error_count >= 3,
            )
            .all()
        )
        for st in error_slave_trades:
            slave = (
                db.query(SlaveAccount)
                .filter(SlaveAccount.id == st.slave_account_id)
                .first()
            )
            logger.warning(
                "[DB_AUDIT] SlaveTrade %s for slave '%s' has %s errors: %s",
                st.id,
                slave.name if slave else "?",
                st.error_count,
                st.last_error,
            )
            warnings += 1

    logger.info(
        "DB audit complete: %s fixes applied, %s warnings (manual review needed)",
        fixes,
        warnings,
    )
    return fixes, warnings
