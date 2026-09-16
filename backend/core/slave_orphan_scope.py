# slave_orphan_scope.py — Shared live-position filter for closed-master recovery
#
# Used by SLAVE_SWEEP (_recover_slave_under_closed_master) and db_audit CHECK 4.
# Pure filter only — no Delta / encryption imports.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrphanCloseScope:
    """Result of scoping live option positions for closed-master recovery."""

    empty_master_pids: bool
    master_pids: frozenset[int]
    bot_owned: frozenset[int]
    protected_hedge_pids: frozenset[int]
    closable: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]


def collect_master_bot_managed_pids(db: Any, master_trade_id: int) -> set[int]:
    """Bot-managed Leg product_ids for a master trade (sweep / CHECK 4)."""
    from backend.models import Leg

    master_pids: set[int] = set()
    for lg in (
        db.query(Leg)
        .filter(
            Leg.trade_id == int(master_trade_id),
            Leg.is_bot_managed.is_(True),
        )
        .all()
    ):
        try:
            pid = int(getattr(lg, "product_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            master_pids.add(pid)
    return master_pids


def scope_live_positions_for_closed_master_recovery(
    live_positions: list[dict[str, Any]] | None,
    *,
    master_pids: set[int],
    bot_owned: set[int],
    protected_hedge_pids: set[int],
) -> OrphanCloseScope:
    """
    Shared filter used by SLAVE_SWEEP recover + db_audit CHECK 4.

    Closable only when:
      - master has bot-managed pids (else empty_master_pids, close nothing)
      - position is bot-owned
      - not a protected structure-hedge long
      - product_id is on this master trade (else different_trade)
    """
    mp = {int(x) for x in (master_pids or set()) if int(x) > 0}
    owned = {int(x) for x in (bot_owned or set()) if int(x) > 0}
    hedges = {int(x) for x in (protected_hedge_pids or set()) if int(x) > 0}

    if not mp:
        return OrphanCloseScope(
            empty_master_pids=True,
            master_pids=frozenset(),
            bot_owned=frozenset(owned),
            protected_hedge_pids=frozenset(hedges),
            closable=tuple(),
            skipped=tuple(),
        )

    closable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for pos in live_positions or []:
        if not isinstance(pos, dict):
            continue
        try:
            pid = int(pos.get("product_id") or 0)
            size = float(pos.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or abs(size) <= 0:
            continue
        symbol = str(pos.get("product_symbol") or pos.get("symbol") or "")
        if pid not in owned:
            skipped.append(
                {
                    "product_id": pid,
                    "symbol": symbol,
                    "size": size,
                    "reason": "foreign",
                    "position": pos,
                }
            )
            continue
        if pid in hedges and size > 0:
            skipped.append(
                {
                    "product_id": pid,
                    "symbol": symbol,
                    "size": size,
                    "reason": "hedge",
                    "position": pos,
                }
            )
            continue
        if pid not in mp:
            skipped.append(
                {
                    "product_id": pid,
                    "symbol": symbol,
                    "size": size,
                    "reason": "different_trade",
                    "position": pos,
                }
            )
            continue
        closable.append(pos)

    return OrphanCloseScope(
        empty_master_pids=False,
        master_pids=frozenset(mp),
        bot_owned=frozenset(owned),
        protected_hedge_pids=frozenset(hedges),
        closable=tuple(closable),
        skipped=tuple(skipped),
    )
