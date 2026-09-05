# position_tracker.py — In-memory real-time position state manager synced with DB

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import TradeStatus
from backend.engine.trade_reconcile import pick_call_put_legs
from backend.models import Leg, Trade

logger = logging.getLogger(__name__)


def _is_open(leg: Any) -> bool:
    return str(getattr(leg, "status", "") or "").lower() == "open"


@dataclass
class TradeState:
    """In-memory state for one bot-managed active trade (may have one closed leg)."""

    trade_id: int
    trade: Any  # Trade ORM model
    call_leg: Any  # Leg ORM model (open preferred; may be closed)
    put_leg: Any  # Leg ORM model (open preferred; may be closed)
    hedge_leg: Any | None = None  # Long hedge leg when in conversion mode
    wing_call_leg: Any | None = None  # Long far-OTM call wing (iron condor)
    wing_put_leg: Any | None = None  # Long far-OTM put wing
    last_call_premium: float = 0.0
    last_put_premium: float = 0.0
    last_pnl: float = 0.0  # calculated PnL / gross MTM (logic only)
    last_delta_mtm: float = 0.0  # Delta official gross UPNL (frontend display)
    last_net_mtm: float = 0.0   # Net MTM after fees + slippage (overview display)
    last_net_mtm_computed_at: datetime | None = None
    last_mtm_snapshot: dict[str, Any] | None = None
    last_updated: datetime | None = None
    is_adjusting: bool = False  # lock — monitoring loop must skip when True


class PositionTracker:
    """Tracks bot-managed ACTIVE trades with ≥1 open leg."""

    def __init__(self) -> None:
        self._positions: dict[int, TradeState] = {}

    def load_from_db(self, db_session: Any) -> int:
        """
        Load ACTIVE trades that still have at least one open bot-managed leg.

        Keeps closed sibling legs in state so the UI can show the full basket
        until everything is flat.
        """
        self._positions.clear()
        trades = (
            db_session.query(Trade)
            .filter(Trade.status == TradeStatus.ACTIVE.value)
            .all()
        )
        loaded = 0
        for trade in trades:
            legs = (
                db_session.query(Leg)
                .filter(
                    Leg.trade_id == trade.id,
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            open_legs = [leg for leg in legs if _is_open(leg)]
            if not open_legs:
                logger.warning(
                    "Trade %s skipped: ACTIVE but no open bot-managed legs",
                    trade.id,
                )
                continue

            call_leg, put_leg = pick_call_put_legs(legs)
            if call_leg is None or put_leg is None:
                logger.warning(
                    "Trade %s skipped: incomplete call/put history "
                    "(call=%s, put=%s)",
                    trade.id,
                    call_leg is not None,
                    put_leg is not None,
                )
                continue

            from backend.core.basket_legs import classify_legs

            bl = classify_legs(legs)
            wing_call_leg = bl.get("wing_call")
            wing_put_leg = bl.get("wing_put")

            hedge_leg = next(
                (
                    leg
                    for leg in open_legs
                    if str(getattr(leg, "leg_type", "") or "")
                    .lower()
                    .startswith("hedge")
                    or (
                        bool(getattr(leg, "is_long", False))
                        and str(getattr(leg, "leg_type", "") or "").lower()
                        not in ("wing_call", "wing_put", "call", "put")
                    )
                ),
                None,
            )

            self._positions[trade.id] = TradeState(
                trade_id=trade.id,
                trade=trade,
                call_leg=call_leg,
                put_leg=put_leg,
                hedge_leg=hedge_leg,
                wing_call_leg=wing_call_leg,
                wing_put_leg=wing_put_leg,
                last_call_premium=float(
                    getattr(call_leg, "exit_premium", None)
                    or call_leg.initial_premium
                    or 0.0
                ),
                last_put_premium=float(
                    getattr(put_leg, "exit_premium", None)
                    or put_leg.initial_premium
                    or 0.0
                ),
            )
            loaded += 1
            logger.info(
                "Loaded trade %s: %s call=%s(%s) put=%s(%s) hedge=%s open=%s",
                trade.id,
                trade.underlying,
                call_leg.symbol,
                call_leg.status,
                put_leg.symbol,
                put_leg.status,
                getattr(hedge_leg, "symbol", None),
                len(open_legs),
            )

        logger.info("Loaded %s active trades into position tracker", loaded)
        return loaded

    def get_all_active(self) -> list[TradeState]:
        """Return all tracked TradeState objects."""
        return list(self._positions.values())

    def get(self, trade_id: int) -> TradeState | None:
        return self._positions.get(trade_id)

    def add(self, trade: Any, call_leg: Any, put_leg: Any) -> None:
        """Register a new trade in the in-memory tracker."""
        self._positions[trade.id] = TradeState(
            trade_id=trade.id,
            trade=trade,
            call_leg=call_leg,
            put_leg=put_leg,
            last_call_premium=float(getattr(call_leg, "initial_premium", 0.0) or 0.0),
            last_put_premium=float(getattr(put_leg, "initial_premium", 0.0) or 0.0),
        )

    def update_legs(self, trade_id: int, call_leg: Any, put_leg: Any, trade: Any | None = None) -> None:
        """Refresh call/put leg objects after partial close or adjustment."""
        state = self._positions.get(trade_id)
        if state is None:
            return
        state.call_leg = call_leg
        state.put_leg = put_leg
        if trade is not None:
            state.trade = trade
        state.last_updated = datetime.now(timezone.utc)

    def update_premiums(
        self,
        trade_id: int,
        call_premium: float,
        put_premium: float,
        pnl: float,
    ) -> None:
        """Update calculated premiums / PnL for a trade (logic values)."""
        state = self._positions.get(trade_id)
        if state is None:
            logger.warning("update_premiums: trade_id=%s not in tracker", trade_id)
            return
        state.last_call_premium = call_premium
        state.last_put_premium = put_premium
        state.last_pnl = pnl
        state.last_updated = datetime.now(timezone.utc)

    def update_delta_mtm(self, trade_id: int, delta_mtm: float) -> None:
        """Store Delta Exchange official gross UPNL for frontend display."""
        state = self._positions.get(trade_id)
        if state is None:
            logger.warning("update_delta_mtm: trade_id=%s not in tracker", trade_id)
            return
        state.last_delta_mtm = delta_mtm
        state.last_updated = datetime.now(timezone.utc)

    def update_net_mtm(
        self,
        trade_id: int,
        net_mtm: float,
        *,
        computed_at: datetime | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Store Net MTM from basket_net_mtm_snapshot (one source for all UIs)."""
        state = self._positions.get(trade_id)
        if state is None:
            logger.warning("update_net_mtm: trade_id=%s not in tracker", trade_id)
            return
        at = computed_at
        if at is None and snapshot is not None:
            at = snapshot.get("computed_at")
        if at is None:
            at = datetime.now(timezone.utc)
        elif getattr(at, "tzinfo", None) is None:
            at = at.replace(tzinfo=timezone.utc)
        state.last_net_mtm = float(net_mtm)
        state.last_net_mtm_computed_at = at
        if snapshot is not None:
            state.last_mtm_snapshot = dict(snapshot)
        state.last_updated = datetime.now(timezone.utc)

    def mark_closed(self, trade_id: int) -> None:
        """Remove a trade from the tracker after exit."""
        self._positions.pop(trade_id, None)

    def set_adjusting(self, trade_id: int, value: bool) -> None:
        """
        Set adjustment lock.

        When True, monitoring loop MUST skip this trade (race prevention).
        Always clear to False in a finally block after adjustment completes/fails.
        """
        state = self._positions.get(trade_id)
        if state is None:
            logger.warning("set_adjusting: trade_id=%s not in tracker", trade_id)
            return
        state.is_adjusting = value
