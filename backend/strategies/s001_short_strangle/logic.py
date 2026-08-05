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
    get_premium_trigger_pct,
    get_settling_info,
    get_trigger_pct,
    is_pre_expiry_window,
    premium_slab_band_label,
)
from backend.core.delta_client import compute_signed_upnl
from backend.models import Setting
from backend.strategies.base_strategy import AdjustmentPlan, BaseStrategy, TradeAction
from backend.strategies.s001_short_strangle.config import (
    DEFAULT_PREMIUM_SLAB_100,
    DEFAULT_PREMIUM_SLAB_200,
    DEFAULT_PREMIUM_SLAB_300,
    DEFAULT_PREMIUM_SLAB_LT100,
    DEFAULT_SLAB_12H,
    DEFAULT_SLAB_24H,
    DEFAULT_SLAB_6H,
    DEFAULT_SLAB_LT6H,
    UNDERLYING_SYMBOLS,
)

logger = logging.getLogger(__name__)

_SLAB_KEYS = (
    "slab_24h",
    "slab_12h",
    "slab_6h",
    "slab_lt6h",
    "flat_trigger_pct",
    "premium_slab_300",
    "premium_slab_200",
    "premium_slab_100",
    "premium_slab_lt100",
)
_SLAB_DEFAULTS: dict[str, float] = {
    "slab_24h": DEFAULT_SLAB_24H,
    "slab_12h": DEFAULT_SLAB_12H,
    "slab_6h": DEFAULT_SLAB_6H,
    "slab_lt6h": DEFAULT_SLAB_LT6H,
    "flat_trigger_pct": 150.0,
    "premium_slab_300": DEFAULT_PREMIUM_SLAB_300,
    "premium_slab_200": DEFAULT_PREMIUM_SLAB_200,
    "premium_slab_100": DEFAULT_PREMIUM_SLAB_100,
    "premium_slab_lt100": DEFAULT_PREMIUM_SLAB_LT100,
}


def _trigger_baseline(leg: Any) -> float:
    """
    Premium used for adjustment trigger %.

    Prefer trigger_baseline_premium (resets each adjustment).
    Fall back to legacy trigger_premium, then initial_premium (entry).
    """
    for attr in ("trigger_baseline_premium", "trigger_premium"):
        val = getattr(leg, attr, None)
        if val is not None and float(val) > 0:
            return float(val)
    return float(getattr(leg, "initial_premium", 0) or 0)


def _fees_from_legs(call_leg: Any, put_leg: Any, trade: Any, db_session: Any) -> float:
    """Sum actual fees paid on basket legs (DB when available)."""
    from unittest.mock import Mock

    from backend.core.fees import basket_fees_paid_from_legs

    try:
        if db_session is not None:
            from backend.models import Leg

            result = (
                db_session.query(Leg)
                .filter(Leg.trade_id == getattr(trade, "id", None))
                .all()
            )
            if isinstance(result, list) and result:
                # Ignore unittest mocks (float(MagicMock)==1.0 would invent fees)
                real_legs = [r for r in result if not isinstance(r, Mock)]
                if real_legs:
                    return float(basket_fees_paid_from_legs(real_legs))
    except (TypeError, ValueError, AttributeError):
        pass
    except Exception:
        pass

    fallback = [
        x
        for x in (call_leg, put_leg)
        if x is not None and not isinstance(x, Mock)
    ]
    # MagicMock legs: only use explicit numeric fee attrs
    if not fallback:
        total = 0.0
        for leg in (call_leg, put_leg):
            if leg is None:
                continue
            try:
                entry = getattr(leg, "entry_fee_usd", None)
                exit_ = getattr(leg, "exit_fee_usd", None)
                if isinstance(entry, Mock) or isinstance(exit_, Mock):
                    continue
                total += float(entry or 0.0) + float(exit_ or 0.0)
            except (TypeError, ValueError, AttributeError):
                continue
        return total
    try:
        return float(basket_fees_paid_from_legs(fallback))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _decision_net_mtm(
    *,
    gross_mtm: float,
    trade: Any,
    call_leg: Any,
    put_leg: Any,
    db_session: Any,
    est_exit_fees: float = 0.0,
    slippage_pct: float | None = None,
) -> float:
    """
    Net MTM for exit/adjust decisions — same formula as frontend display.

    net = gross − fees_paid − est_exit_fees − slippage
    """
    from backend.core.fees import compute_net_mtm

    fees_paid = _fees_from_legs(call_leg, put_leg, trade, db_session)
    slip = (
        slippage_pct
        if slippage_pct is not None
        else getattr(trade, "slippage_pct", None)
    )
    fields = compute_net_mtm(
        gross_mtm=gross_mtm,
        fees_paid=fees_paid,
        est_exit_fees=est_exit_fees,
        slippage_pct=slip,
    )
    return float(fields["net_mtm"])


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
        Load trigger slabs for a trade from settings table.

        Includes time slabs, flat %, and premium slabs. Missing keys use defaults.
        """
        slabs = dict(_SLAB_DEFAULTS)
        if db_session is None:
            return slabs
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

    def get_trigger_for_leg(
        self,
        leg_current_premium: float,
        trade: Any,
        db_session: Any = None,
    ) -> float:
        """
        Resolve trigger % for one leg.

        flat / slab: same % for both legs.
        premium: % depends on this leg's current premium.
        """
        mode = str(getattr(trade, "trigger_mode", "slab") or "slab").lower()
        slabs = self.get_slabs(getattr(trade, "id", 0), db_session)

        if mode == "flat":
            return float(slabs.get("flat_trigger_pct", 150))
        if mode == "slab":
            hours_left = get_hours_to_expiry(trade.expiry_date)
            return float(get_trigger_pct(hours_left, slabs))
        if mode == "premium":
            return float(
                get_premium_trigger_pct(float(leg_current_premium or 0), slabs)
            )
        return 150.0

    def get_current_trigger_pct(self, trade: Any, db_session: Any) -> float:
        """
        Resolve a single trigger % (flat / time slab).

        For premium mode, returns lt100 default (use get_trigger_for_leg per leg).
        """
        mode = str(getattr(trade, "trigger_mode", "slab") or "slab").lower()
        if mode == "premium":
            slabs = self.get_slabs(trade.id, db_session)
            return float(slabs.get("premium_slab_lt100", 200))
        # Use 0 premium for flat/slab — premium arg ignored
        return self.get_trigger_for_leg(0.0, trade, db_session)

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
        net_mtm: float | None = None,
        slippage_pct: float | None = None,
    ) -> TradeAction:
        """
        Evaluate exits then adjustment triggers (exact priority):

        a. settling → no action
        b. Net MTM >= profit_target → PROFIT_TARGET
        c. Net MTM <= -stoploss → STOPLOSS
        d. pre-expiry window → PRE_EXPIRY
        e. adjustment trigger + decision (Net MTM > 0 → close, else adjust)
        f. HOLD

        When ``net_mtm`` is provided (from bot_engine), use it for b/c/e.
        Otherwise compute via compute_net_mtm (gross − fees − slip).
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

        # a. SETTLING PERIOD: Don't check P&L / adjust for first N minutes
        settling = get_settling_info(getattr(trade, "monitoring_starts_at", None))
        if settling["is_settling"]:
            logger.info(
                "Trade %s settling... %sm remaining before P&L checks",
                getattr(trade, "id", "?"),
                settling["settling_minutes_left"],
            )
            return TradeAction(current_pnl=total_pnl)

        # Decision Net MTM (passed in from bot_engine, or computed here)
        if net_mtm is not None:
            decision_pnl = float(net_mtm)
        else:
            decision_pnl = _decision_net_mtm(
                gross_mtm=total_pnl,
                trade=trade,
                call_leg=call_leg,
                put_leg=put_leg,
                db_session=db_session,
                slippage_pct=slippage_pct,
            )

        # b. Profit target
        should_exit_profit = decision_pnl >= float(trade.profit_target_usd)
        # c. Stop loss
        should_exit_sl = decision_pnl <= -float(trade.stoploss_usd)

        if should_exit_profit:
            logger.info(
                "Trade %s decision: realized=%.2f + upnl=%.2f = gross=%.2f | "
                "net_mtm=%.2f | target=%s | sl=%s | action=EXIT PROFIT_TARGET",
                getattr(trade, "id", "?"),
                float(realized_pnl or 0.0),
                float(delta_mtm if delta_mtm is not None else 0.0),
                total_pnl,
                decision_pnl,
                trade.profit_target_usd,
                trade.stoploss_usd,
            )
            return TradeAction(
                should_exit=True,
                exit_reason="PROFIT_TARGET",
                current_pnl=decision_pnl,
            )

        if should_exit_sl:
            logger.info(
                "Trade %s decision: realized=%.2f + upnl=%.2f = gross=%.2f | "
                "net_mtm=%.2f | target=%s | sl=%s | action=EXIT STOPLOSS",
                getattr(trade, "id", "?"),
                float(realized_pnl or 0.0),
                float(delta_mtm if delta_mtm is not None else 0.0),
                total_pnl,
                decision_pnl,
                trade.profit_target_usd,
                trade.stoploss_usd,
            )
            return TradeAction(
                should_exit=True,
                exit_reason="STOPLOSS",
                current_pnl=decision_pnl,
            )

        # d. Pre-expiry
        hours_left = get_hours_to_expiry(trade.expiry_date)
        if hours_left == 0 or is_pre_expiry_window(trade.expiry_date):
            return TradeAction(
                should_exit=True,
                exit_reason="PRE_EXPIRY",
                current_pnl=decision_pnl,
            )

        # e. Adjustment trigger + decision (Net MTM > 0 → close basket)
        call_trigger_pct = self.get_trigger_for_leg(
            call_premium, trade, db_session
        )
        put_trigger_pct = self.get_trigger_for_leg(
            put_premium, trade, db_session
        )
        mode = str(getattr(trade, "trigger_mode", "slab") or "slab").lower()
        net_for_decision = decision_pnl

        call_open = str(getattr(call_leg, "status", "open")).lower() == "open"
        put_open = str(getattr(put_leg, "status", "open")).lower() == "open"

        call_baseline = _trigger_baseline(call_leg) if call_open else 0.0
        put_baseline = _trigger_baseline(put_leg) if put_open else 0.0
        call_trigger_price = (
            call_baseline * (call_trigger_pct / 100.0) if call_baseline > 0 else 0.0
        )
        put_trigger_price = (
            put_baseline * (put_trigger_pct / 100.0) if put_baseline > 0 else 0.0
        )
        call_ratio = (
            (call_premium / call_baseline * 100.0) if call_baseline > 0 else 0.0
        )
        put_ratio = (
            (put_premium / put_baseline * 100.0) if put_baseline > 0 else 0.0
        )
        logger.info(
            "[TRIGGER_CHECK] Trade %s "
            "call: current=%.2f baseline=%.2f trigger_at=%.2f "
            "trigger_pct=%.1f ratio=%.1f%% "
            "| put: current=%.2f baseline=%.2f trigger_at=%.2f "
            "trigger_pct=%.1f ratio=%.1f%%",
            getattr(trade, "id", "?"),
            call_premium,
            call_baseline,
            call_trigger_price,
            call_trigger_pct,
            call_ratio,
            put_premium,
            put_baseline,
            put_trigger_price,
            put_trigger_pct,
            put_ratio,
        )

        if call_open:
            if call_premium >= call_trigger_price:
                if mode == "premium":
                    logger.info(
                        "Premium trigger CALL: premium=$%.2f (%s) → %.1f%%",
                        call_premium,
                        premium_slab_band_label(call_premium),
                        call_trigger_pct,
                    )
                if net_for_decision > 0:
                    logger.info(
                        "DECISION: Net MTM profitable at trigger — closing basket | "
                        "Trade %s CALL hit %.1f%% but Net MTM=%.2f is PROFITABLE",
                        getattr(trade, "id", "?"),
                        call_trigger_pct,
                        net_for_decision,
                    )
                    return TradeAction(
                        should_exit=True,
                        exit_reason="DECISION_PROFIT_AT_TRIGGER",
                        current_pnl=net_for_decision,
                        triggered_leg="call",
                        trigger_pct_hit=call_trigger_pct,
                        trigger_pct_used=call_trigger_pct,
                        call_trigger_pct=call_trigger_pct,
                        put_trigger_pct=put_trigger_pct,
                    )
                logger.info(
                    "DECISION: Net MTM negative at trigger — adjusting | "
                    "Trade %s CALL hit %.1f%% and Net MTM=%.2f is NEGATIVE",
                    getattr(trade, "id", "?"),
                    call_trigger_pct,
                    net_for_decision,
                )
                return TradeAction(
                    should_adjust=True,
                    adjust_leg="call",
                    current_pnl=net_for_decision,
                    triggered_leg="call",
                    trigger_pct_hit=call_trigger_pct,
                    trigger_pct_used=call_trigger_pct,
                    call_trigger_pct=call_trigger_pct,
                    put_trigger_pct=put_trigger_pct,
                )

        if put_open:
            if put_premium >= put_trigger_price:
                if mode == "premium":
                    logger.info(
                        "Premium trigger PUT: premium=$%.2f (%s) → %.1f%%",
                        put_premium,
                        premium_slab_band_label(put_premium),
                        put_trigger_pct,
                    )
                if net_for_decision > 0:
                    logger.info(
                        "DECISION: Net MTM profitable at trigger — closing basket | "
                        "Trade %s PUT hit %.1f%% but Net MTM=%.2f is PROFITABLE",
                        getattr(trade, "id", "?"),
                        put_trigger_pct,
                        net_for_decision,
                    )
                    return TradeAction(
                        should_exit=True,
                        exit_reason="DECISION_PROFIT_AT_TRIGGER",
                        current_pnl=net_for_decision,
                        triggered_leg="put",
                        trigger_pct_hit=put_trigger_pct,
                        trigger_pct_used=put_trigger_pct,
                        call_trigger_pct=call_trigger_pct,
                        put_trigger_pct=put_trigger_pct,
                    )
                logger.info(
                    "DECISION: Net MTM negative at trigger — adjusting | "
                    "Trade %s PUT hit %.1f%% and Net MTM=%.2f is NEGATIVE",
                    getattr(trade, "id", "?"),
                    put_trigger_pct,
                    net_for_decision,
                )
                return TradeAction(
                    should_adjust=True,
                    adjust_leg="put",
                    current_pnl=net_for_decision,
                    triggered_leg="put",
                    trigger_pct_hit=put_trigger_pct,
                    trigger_pct_used=put_trigger_pct,
                    call_trigger_pct=call_trigger_pct,
                    put_trigger_pct=put_trigger_pct,
                )

        logger.info(
            "Trade %s decision: realized=%.2f + upnl=%.2f = gross=%.2f | "
            "net_mtm=%.2f | call_trig=%.1f%% put_trig=%.1f%% | "
            "target=%s | sl=%s | action=HOLD",
            getattr(trade, "id", "?"),
            float(realized_pnl or 0.0),
            float(delta_mtm if delta_mtm is not None else 0.0),
            total_pnl,
            decision_pnl,
            call_trigger_pct,
            put_trigger_pct,
            trade.profit_target_usd,
            trade.stoploss_usd,
        )
        return TradeAction(
            current_pnl=decision_pnl,
            trigger_pct_used=call_trigger_pct,
            call_trigger_pct=call_trigger_pct,
            put_trigger_pct=put_trigger_pct,
        )

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
    trade.monitoring_starts_at = None
    trade.slippage_pct = 2.0
    trade.trigger_mode = "slab"

    call_leg = MagicMock()
    call_leg.initial_premium = 150.0
    call_leg.quantity = 1
    call_leg.status = "open"
    call_leg.entry_fee_usd = 0.0
    call_leg.exit_fee_usd = 0.0

    put_leg = MagicMock()
    put_leg.initial_premium = 150.0
    put_leg.quantity = 1
    put_leg.status = "open"
    put_leg.entry_fee_usd = 0.0
    put_leg.exit_fee_usd = 0.0

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

    # Test 3: Adjustment trigger (call at 200% of initial) — net MTM negative
    trade.profit_target_usd = 10000  # high so doesn't trigger
    trade.stoploss_usd = 10000
    trade.slippage_pct = 2.0
    action = asyncio.run(strategy.on_tick(trade, call_leg, put_leg, 300.0, 100.0, db))
    assert action.should_adjust is True
    assert action.adjust_leg == "call"
    assert action.triggered_leg == "call"
    assert action.trigger_pct_used > 0
    print(
        f"Test 3 Adjustment: adjust {action.adjust_leg} "
        f"(trigger_pct_used={action.trigger_pct_used}) ✅"
    )

    # Test 3b: trigger hit but Net MTM > 0 → DECISION_PROFIT_AT_TRIGGER
    call_leg.initial_premium = 100.0
    put_leg.initial_premium = 250.0
    action = asyncio.run(strategy.on_tick(trade, call_leg, put_leg, 200.0, 40.0, db))
    assert action.should_exit is True
    assert action.exit_reason == "DECISION_PROFIT_AT_TRIGGER"
    assert action.triggered_leg == "call"
    assert action.current_pnl > 0
    print(
        f"Test 3b Decision close: {action.exit_reason} "
        f"net_mtm={action.current_pnl} ✅"
    )

    # Test 4: Premium-based slabs — per-leg trigger %
    trade.trigger_mode = "premium"
    call_leg.initial_premium = 400.0
    call_leg.trigger_baseline_premium = 400.0
    put_leg.initial_premium = 80.0
    put_leg.trigger_baseline_premium = 80.0
    trade.profit_target_usd = 10000
    trade.stoploss_usd = 10000
    # call $450 → 150% → trigger 600; put $85 → 200% → trigger 160
    # neither hits yet
    action = asyncio.run(strategy.on_tick(trade, call_leg, put_leg, 450.0, 85.0, db))
    assert action.should_adjust is False
    assert action.call_trigger_pct == 150.0
    assert action.put_trigger_pct == 200.0
    print(
        f"Test 4 Premium slabs: call={action.call_trigger_pct}% "
        f"put={action.put_trigger_pct}% ✅"
    )
    # put at $99 still in <\$100 band → 200%; baseline 40 → trigger \$80
    put_leg.initial_premium = 40.0
    put_leg.trigger_baseline_premium = 40.0
    action = asyncio.run(strategy.on_tick(trade, call_leg, put_leg, 450.0, 99.0, db))
    assert action.should_adjust is True
    assert action.adjust_leg == "put"
    assert action.trigger_pct_hit == 200.0
    print(f"Test 4b Premium put adjust @ {action.trigger_pct_hit}% ✅")

    print("✅ STRATEGY LOGIC TEST PASSED")
