# db_audit.py — Startup DB consistency checks (orphan legs, zombie trades, baselines)

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import func

from backend.config import IST, TradeStatus
from backend.core.time_utils import get_ist_now, get_utc_now, as_utc
from backend.models import Leg, SlaveAccount, SlaveTrade, Trade

logger = logging.getLogger(__name__)

# Exit reason written when we close a zombie ACTIVE trade with zero open legs.
DB_CONSISTENCY_FIX_NO_LEGS = "DB_CONSISTENCY_FIX_NO_LEGS"
_EXIT_TIME_GRACE_SECONDS = 60.0


def _as_ist(dt: Any) -> Any:
    """Normalize DB datetime to IST (naive = UTC wall-clock)."""
    from backend.core.time_utils import _as_ist as _shared_as_ist

    return _shared_as_ist(dt)


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
                # Race guard: a live exit may still be committing.
                exit_time = getattr(trade, "exit_time", None)
                if exit_time is not None:
                    try:
                        et = _as_ist(exit_time)
                        age_s = (get_utc_now() - as_utc(et)).total_seconds() if as_utc(et) else 0.0
                        if age_s < _EXIT_TIME_GRACE_SECONDS:
                            logger.info(
                                "[DB_AUDIT_SKIP] trade_id=%s recent "
                                "exit_time age_s=%.1f — grace period",
                                trade.id,
                                age_s,
                            )
                            try:
                                from backend.core.bot_logger import log_and_buffer

                                log_and_buffer(
                                    "DB_AUDIT_SKIP",
                                    int(trade.id),
                                    {
                                        "reason": "exit_time_grace",
                                        "age_s": round(age_s, 1),
                                        "existing_reason": str(
                                            getattr(trade, "exit_reason", None)
                                            or ""
                                        ),
                                    },
                                )
                            except Exception:
                                pass
                            continue
                    except Exception as grace_exc:
                        logger.warning(
                            "[DB_AUDIT] grace check failed trade=%s: %s",
                            trade.id,
                            grace_exc,
                        )

                existing_reason = str(
                    getattr(trade, "exit_reason", None) or ""
                ).strip()
                existing_pnl = getattr(trade, "realized_pnl", None)

                logger.critical(
                    "[DB_AUDIT] Trade %s is 'active' but has NO open legs! "
                    "Marking closed.",
                    trade.id,
                )
                trade.status = TradeStatus.CLOSED.value
                if existing_reason:
                    logger.info(
                        "[DB_AUDIT_SKIP] trade_id=%s existing_reason=%s "
                        "— preserving exit_reason",
                        trade.id,
                        existing_reason,
                    )
                    try:
                        from backend.core.bot_logger import log_and_buffer

                        log_and_buffer(
                            "DB_AUDIT_SKIP",
                            int(trade.id),
                            {
                                "reason": "existing_exit_reason",
                                "existing_reason": existing_reason,
                            },
                        )
                    except Exception:
                        pass
                else:
                    trade.exit_reason = DB_CONSISTENCY_FIX_NO_LEGS
                if trade.exit_time is None:
                    trade.exit_time = get_utc_now()
                # Never overwrite a non-null realized_pnl
                if existing_pnl is None:
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
            now = get_utc_now()
            for leg in orphan_legs:
                leg.status = "closed"
                leg.exit_time = now
                if leg.exit_premium is None:
                    # Never invent a free exit — leave NULL for manual repair
                    leg.exit_premium = None
                    leg.realized_pnl = None
                    logger.critical(
                        "[DB_AUDIT] Trade %s orphan leg %s closed with "
                        "exit_premium=NULL (was open under non-ACTIVE trade)",
                        trade.id,
                        leg.symbol,
                    )
            db.commit()
            fixes += 1

        # CHECK 4: Non-closed SlaveTrades under a non-ACTIVE master.
        # NEVER flip status in DB alone — verify/close live Delta positions first.
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
                    SlaveTrade.status != "closed",
                )
                .all()
            )
            from backend.core.delta_client import DeltaClient
            from backend.core.encryption import decrypt
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
                        "leave status=%s until intentional exit",
                        st.id,
                        st.master_trade_id,
                        st.status,
                    )
                    warnings += 1
                    continue
                if slave is None:
                    logger.critical(
                        "[DB_AUDIT] SlaveTrade %s has no SlaveAccount — "
                        "leaving status=%s untouched",
                        st.id,
                        st.status,
                    )
                    warnings += 1
                    continue

                client = DeltaClient(
                    decrypt(slave.api_key_encrypted),
                    decrypt(slave.api_secret_encrypted),
                )
                try:
                    try:
                        positions = await client.get_option_positions()
                    except Exception as api_exc:
                        logger.critical(
                            "[DB_AUDIT] Slave '%s' SlaveTrade %s — Delta "
                            "unreachable at startup (%s). Leaving status=%s "
                            "untouched (will not erase live-position record).",
                            slave.name,
                            st.id,
                            api_exc,
                            st.status,
                        )
                        warnings += 1
                        continue

                    live: list[dict[str, Any]] = []
                    for pos in positions or []:
                        try:
                            pid = int(pos.get("product_id") or 0)
                            size = float(pos.get("size") or 0)
                        except (TypeError, ValueError):
                            continue
                        if pid > 0 and abs(size) > 0:
                            live.append(pos)

                    if live:
                        logger.warning(
                            "[DB_AUDIT] Slave '%s' SlaveTrade %s has %s live "
                            "option positions under closed master %s — "
                            "attempting reduce_only close",
                            slave.name,
                            st.id,
                            len(live),
                            st.master_trade_id,
                        )
                        for pos in live:
                            pid = int(pos.get("product_id") or 0)
                            size = float(pos.get("size") or 0)
                            close_size = max(1, abs(int(size)))
                            is_long = size > 0
                            try:
                                await client.close_position(
                                    product_id=pid,
                                    size=close_size,
                                    is_long=is_long,
                                )
                            except Exception as close_exc:
                                side = "sell" if is_long else "buy"
                                try:
                                    await client.place_order(
                                        product_id=pid,
                                        size=close_size,
                                        side=side,
                                        reduce_only=True,
                                    )
                                except Exception as retry_exc:
                                    logger.critical(
                                        "[DB_AUDIT] Slave '%s' close FAILED "
                                        "pid=%s: %s / %s — leaving "
                                        "status=exit_failed",
                                        slave.name,
                                        pid,
                                        close_exc,
                                        retry_exc,
                                    )
                                    st.status = "exit_failed"
                                    st.last_error = (
                                        f"db_audit_close_failed: {retry_exc}"
                                    )[:500]
                                    st.error_count = (
                                        int(st.error_count or 0) + 1
                                    )
                                    st.last_updated = get_utc_now()
                                    db.commit()
                                    warnings += 1
                                    break
                        else:
                            # All closes attempted — re-verify
                            try:
                                verify = await client.get_option_positions()
                            except Exception as verify_exc:
                                logger.critical(
                                    "[DB_AUDIT] Slave '%s' verify unreachable "
                                    "after close (%s) — leaving status=%s",
                                    slave.name,
                                    verify_exc,
                                    st.status,
                                )
                                warnings += 1
                                continue
                            still = [
                                p
                                for p in (verify or [])
                                if abs(float(p.get("size") or 0)) > 0
                            ]
                            if still:
                                logger.critical(
                                    "[DB_AUDIT] Slave '%s' still has %s "
                                    "positions after close — status=exit_failed",
                                    slave.name,
                                    len(still),
                                )
                                st.status = "exit_failed"
                                st.last_error = (
                                    f"db_audit_still_open: {len(still)} positions"
                                )[:500]
                                st.error_count = int(st.error_count or 0) + 1
                                st.last_updated = get_utc_now()
                                db.commit()
                                warnings += 1
                            else:
                                st.status = "closed"
                                st.last_error = None
                                st.last_updated = get_utc_now()
                                closed_orphans += 1
                                fixes += 1
                                db.commit()
                                logger.warning(
                                    "[DB_AUDIT] SlaveTrade %s closed after "
                                    "verified Delta flat (master %s)",
                                    st.id,
                                    st.master_trade_id,
                                )
                    else:
                        # Verified empty book — safe to mark closed
                        logger.warning(
                            "[DB_AUDIT] SlaveTrade %s status=%s under closed "
                            "master %s — Delta book empty, marking closed",
                            st.id,
                            st.status,
                            st.master_trade_id,
                        )
                        st.status = "closed"
                        st.last_error = None
                        st.last_updated = get_utc_now()
                        closed_orphans += 1
                        fixes += 1
                        db.commit()
                finally:
                    await client.close()

            if closed_orphans:
                logger.info(
                    "[DB_AUDIT] CHECK 4 closed %s orphan SlaveTrade rows "
                    "after Delta verification",
                    closed_orphans,
                )

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
