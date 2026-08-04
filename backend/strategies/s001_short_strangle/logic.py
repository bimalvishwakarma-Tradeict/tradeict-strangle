# logic.py — Core S001 P&L calculation, target/SL checks, and on_tick decisions

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Allow `python backend/strategies/s001_short_strangle/logic.py` from trading-bot/ root
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.time_utils import (
    get_hours_to_expiry,
    get_settling_info,
    get_trigger_pct,
    is_pre_expiry_window,
)
from backend.core.delta_client import compute_signed_upnl
from backend.models import Setting
from backend.strategies.base_strategy import AdjustmentPlan, BaseStrategy, TradeAction
from backend.strategies.s001_short_strangle.config import (
    DEFAULT_SLAB_12H,
    DEFAULT_SLAB_24H,
    DEFAULT_SLAB_6H,
    DEFAULT_SLAB_LT6H,
    UNDERLYING_SYMBOLS,
)

logger = logging.getLogger(__name__)

_SLAB_KEYS = ("slab_24h", "slab_12h", "slab_6h", "slab_lt6h")
_SLAB_DEFAULTS: dict[str, float] = {
    "slab_24h": DEFAULT_SLAB_24H,
    "slab_12h": DEFAULT_SLAB_12H,
    "slab_6h": DEFAULT_SLAB_6H,
    "slab_lt6h": DEFAULT_SLAB_LT6H,
}


def _trigger_baseline(leg: Any) -> float:
    """Premium used for adjustment trigger % (may differ from fill after adj)."""
    tp = getattr(leg, "trigger_premium", None)
    if tp is not None and float(tp) > 0:
        return float(tp)
    return float(leg.initial_premium)


class ShortStrangleStrategy(BaseStrategy):
    """S001 — Short Strangle with Dynamic Adjustment decision logic."""

    def calculate_pnl(
        self,
        trade: Any,
        call_leg: Any,
        put_leg: Any,
        call_premium: float,
        put_premium: float,
        realized_pnl: float = 0.0,
    ) -> float:
        """
        Total P&L USD = realized (closed legs) + unrealized (open shorts).

        Unrealized uses Delta-matching formula:
          (mark - entry) * size * contract_value  with size = -quantity
        """
        call_upnl = 0.0
        put_upnl = 0.0
        if str(getattr(call_leg, "status", "open")).lower() == "open":
            call_upnl = compute_signed_upnl(
                float(call_leg.initial_premium),
                float(call_premium),
                size=-abs(int(call_leg.quantity)),
                contract_value=OPTIONS_CONTRACT_VALUE,
            )
        if str(getattr(put_leg, "status", "open")).lower() == "open":
            put_upnl = compute_signed_upnl(
                float(put_leg.initial_premium),
                float(put_premium),
                size=-abs(int(put_leg.quantity)),
                contract_value=OPTIONS_CONTRACT_VALUE,
            )
        return float(realized_pnl or 0.0) + call_upnl + put_upnl

    def get_slabs(self, trade_id: int, db_session: Any) -> dict[str, float]:
        """
        Load time-based trigger slabs for a trade from settings table.

        Missing keys fall back to S001 config defaults.
        """
        slabs = dict(_SLAB_DEFAULTS)
        rows = (
            db_session.query(Setting)
            .filter(
                Setting.trade_id == trade_id,
                Setting.key.in_(list(_SLAB_KEYS)),
            )
            .all()
        )
        for setting in rows:
            try:
                slabs[setting.key] = float(setting.value)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid slab value for trade %s key=%s value=%s — using default",
                    trade_id,
                    setting.key,
                    setting.value,
                )
        return slabs

    def get_current_trigger_pct(self, trade: Any, db_session: Any) -> float:
        """Resolve active trigger % (flat setting or time-based slab)."""
        mode = str(getattr(trade, "trigger_mode", "slab") or "slab").lower()
        if mode == "flat":
            row = (
                db_session.query(Setting)
                .filter(
                    Setting.trade_id == trade.id,
                    Setting.key == "flat_trigger_pct",
                )
                .first()
            )
            if row is not None:
                try:
                    return float(row.value)
                except (TypeError, ValueError):
                    pass
            return 150.0
        hours_left = get_hours_to_expiry(trade.expiry_date)
        return float(get_trigger_pct(hours_left, self.get_slabs(trade.id, db_session)))

    async def on_tick(
        self,
        trade: Any,
        call_leg: Any,
        put_leg: Any,
        call_premium: float,
        put_premium: float,
        db_session: Any = None,
        realized_pnl: float = 0.0,
        delta_mtm: float | None = None,
    ) -> TradeAction:
        """
        Evaluate profit target, stop loss, pre-expiry, then adjustment triggers.

        Exit decisions prefer Delta official MTM (realized + unrealized).
        Adjustment triggers still use live premiums vs entry × trigger %.
        Settling period: no exit/adjust until trade.monitoring_starts_at.
        """
        calculated_pnl = self.calculate_pnl(
            trade,
            call_leg,
            put_leg,
            call_premium,
            put_premium,
            realized_pnl=float(realized_pnl or 0.0),
        )
        # Primary: realized + Delta UPNL. Fallback: calculated short-option PnL.
        if delta_mtm is not None:
            total_pnl = float(realized_pnl or 0.0) + float(delta_mtm)
        else:
            total_pnl = calculated_pnl

        # SETTLING PERIOD: Don't check P&L / adjust for first N minutes
        settling = get_settling_info(getattr(trade, "monitoring_starts_at", None))
        if settling["is_settling"]:
            logger.info(
                "Trade %s settling... %sm remaining before P&L checks",
                getattr(trade, "id", "?"),
                settling["settling_minutes_left"],
            )
            return TradeAction(current_pnl=total_pnl)

        should_exit_profit = total_pnl >= float(trade.profit_target_usd)
        should_exit_sl = total_pnl <= -float(trade.stoploss_usd)

        if should_exit_profit:
            logger.info(
                "Trade %s decision: realized=%.2f + upnl=%.2f = total=%.2f | "
                "target=%s | sl=%s | action=EXIT PROFIT_TARGET",
                getattr(trade, "id", "?"),
                float(realized_pnl or 0.0),
                float(delta_mtm if delta_mtm is not None else 0.0),
                total_pnl,
                trade.profit_target_usd,
                trade.stoploss_usd,
            )
            return TradeAction(
                should_exit=True,
                exit_reason="PROFIT_TARGET",
                current_pnl=total_pnl,
            )

        if should_exit_sl:
            logger.info(
                "Trade %s decision: realized=%.2f + upnl=%.2f = total=%.2f | "
                "target=%s | sl=%s | action=EXIT STOPLOSS",
                getattr(trade, "id", "?"),
                float(realized_pnl or 0.0),
                float(delta_mtm if delta_mtm is not None else 0.0),
                total_pnl,
                trade.profit_target_usd,
                trade.stoploss_usd,
            )
            return TradeAction(
                should_exit=True,
                exit_reason="STOPLOSS",
                current_pnl=total_pnl,
            )

        hours_left = get_hours_to_expiry(trade.expiry_date)
        # Edge case: expiry already past (bot late) → force pre-expiry exit
        if hours_left == 0 or is_pre_expiry_window(trade.expiry_date):
            return TradeAction(
                should_exit=True,
                exit_reason="PRE_EXPIRY",
                current_pnl=total_pnl,
            )

        trigger_pct = self.get_current_trigger_pct(trade, db_session)

        call_open = str(getattr(call_leg, "status", "open")).lower() == "open"
        put_open = str(getattr(put_leg, "status", "open")).lower() == "open"

        if call_open:
            call_trigger_price = _trigger_baseline(call_leg) * (trigger_pct / 100.0)
            if call_premium >= call_trigger_price:
                return TradeAction(
                    should_adjust=True,
                    adjust_leg="call",
                    current_pnl=total_pnl,
                    trigger_pct_used=trigger_pct,
                )

        if put_open:
            put_trigger_price = _trigger_baseline(put_leg) * (trigger_pct / 100.0)
            if put_premium >= put_trigger_price:
                return TradeAction(
                    should_adjust=True,
                    adjust_leg="put",
                    current_pnl=total_pnl,
                    trigger_pct_used=trigger_pct,
                )

        logger.info(
            "Trade %s decision: realized=%.2f + upnl=%.2f = total=%.2f | "
            "target=%s | sl=%s | action=HOLD",
            getattr(trade, "id", "?"),
            float(realized_pnl or 0.0),
            float(delta_mtm if delta_mtm is not None else 0.0),
            total_pnl,
            trade.profit_target_usd,
            trade.stoploss_usd,
        )
        return TradeAction(current_pnl=total_pnl, trigger_pct_used=trigger_pct)

    async def find_adjustment_strike(
        self,
        delta_client: Any,
        trade: Any,
        triggered_leg_type: str,
        other_leg_current_premium: float,
        current_strike: float | None = None,
    ) -> AdjustmentPlan:
        """Find replacement strike by matching the other leg's current mark premium.

        Never returns the same strike as current_strike — same-strike adjust
        only burns brokerage/slippage.
        """
        leg = triggered_leg_type.lower().strip()
        underlying_key = str(trade.underlying).upper()
        underlying_symbol = UNDERLYING_SYMBOLS.get(underlying_key, underlying_key)

        expiry = trade.expiry_date
        if isinstance(expiry, date):
            expiry_str = expiry.isoformat()
        else:
            expiry_str = str(expiry)

        # Core rule: nearest premium to other-leg — NOT forced farther OTM
        row = await delta_client.find_strike_by_premium(
            underlying=underlying_symbol,
            expiry_date=expiry_str,
            leg_type=leg,
            target_premium=float(other_leg_current_premium),
            exclude_strike=float(current_strike) if current_strike is not None else None,
            require_farther_otm=False,
        )

        new_strike = float(row.get("strike") or 0)
        if current_strike is not None and abs(new_strike - float(current_strike)) < 0.01:
            raise ValueError(
                f"SAME_STRIKE_HOLD: nearest match is still {new_strike} "
                f"(no alternate strike for {leg})"
            )

        if leg == "call":
            new_symbol = str(row.get("call_symbol", ""))
            new_product_id = int(row.get("call_product_id") or 0)
        else:
            new_symbol = str(row.get("put_symbol", ""))
            new_product_id = int(row.get("put_product_id") or 0)

        return AdjustmentPlan(
            exit_leg_type=leg,
            exit_leg_symbol="",  # filled by adjustment executor from DB leg
            new_strike=new_strike,
            new_product_id=new_product_id,
            new_symbol=new_symbol,
            target_premium=float(other_leg_current_premium),
            other_leg_premium=float(other_leg_current_premium),
        )


if __name__ == "__main__":
    import asyncio
    from datetime import timedelta
    from unittest.mock import MagicMock

    strategy = ShortStrangleStrategy()

    trade = MagicMock()
    trade.id = 1
    trade.profit_target_usd = 200.0
    trade.stoploss_usd = 300.0
    trade.expiry_date = date.today() + timedelta(days=1)
    trade.underlying = "BTC"

    call_leg = MagicMock()
    call_leg.initial_premium = 150.0
    call_leg.quantity = 1

    put_leg = MagicMock()
    put_leg.initial_premium = 150.0
    put_leg.quantity = 1

    # Test 1: P&L calculation (equal qty) — USD with contract_value
    from backend.config import OPTIONS_CONTRACT_VALUE as CV

    pnl = strategy.calculate_pnl(trade, call_leg, put_leg, 100.0, 80.0)
    # shorts: (mark-entry)*(-qty)*cv = (entry-mark)*qty*cv
    expected = ((150 - 100) + (150 - 80)) * 1 * CV
    assert abs(pnl - expected) < 1e-9, f"Expected {expected}, got {pnl}"
    print(f"Test 1 PnL: {pnl} ✅")

    # Test 1b: unequal quantities
    call_leg.quantity = 2
    put_leg.quantity = 1
    pnl_unequal = strategy.calculate_pnl(trade, call_leg, put_leg, 100.0, 80.0)
    expected_unequal = (150 - 100) * 2 * CV + (150 - 80) * 1 * CV
    assert abs(pnl_unequal - expected_unequal) < 1e-9
    print(f"Test 1b Unequal qty PnL: {pnl_unequal} ✅")
    call_leg.quantity = 1

    # Test 1c: realized + unrealized combined
    unreal = ((150 - 100) + (150 - 80)) * CV
    combined = strategy.calculate_pnl(
        trade, call_leg, put_leg, 100.0, 80.0, realized_pnl=-0.08
    )
    assert abs(combined - (unreal - 0.08)) < 1e-9
    print(f"Test 1c Combined PnL: {combined} ✅")

    # Test 2: Profit target (large premium collapse → hits target in USD)
    trade.profit_target_usd = 0.05  # $0.05 USD ≈ 50 premium points * 0.001
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    action = asyncio.run(strategy.on_tick(trade, call_leg, put_leg, 30.0, 20.0, db))
    assert action.should_exit is True
    assert action.exit_reason == "PROFIT_TARGET"
    print(f"Test 2 Profit Target: {action.exit_reason} ✅")

    # Test 2b: realized loss keeps total below target
    trade.profit_target_usd = 0.20
    action = asyncio.run(
        strategy.on_tick(
            trade, call_leg, put_leg, 100.0, 80.0, db, realized_pnl=-0.15,
            delta_mtm=unreal,
        )
    )
    assert action.should_exit is False, "Should NOT exit when total < target"
    print(f"Test 2b Realized blocks false profit exit: pnl={action.current_pnl} ✅")
    trade.profit_target_usd = 200.0

    # Test 3: Adjustment trigger (call at 200% of initial)
    trade.profit_target_usd = 10000  # high so doesn't trigger
    action = asyncio.run(strategy.on_tick(trade, call_leg, put_leg, 300.0, 100.0, db))
    assert action.should_adjust is True
    assert action.adjust_leg == "call"
    assert action.trigger_pct_used > 0
    print(
        f"Test 3 Adjustment: adjust {action.adjust_leg} "
        f"(trigger_pct_used={action.trigger_pct_used}) ✅"
    )

    print("✅ STRATEGY LOGIC TEST PASSED")
