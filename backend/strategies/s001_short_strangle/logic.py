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
from backend.strategies.s001_short_strangle.premium_decay import (
    evaluate_premium_decay_exit,
)
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

# adjustment_count is PER BASKET (one Trade row), shared across BOTH legs.
# Each successful adjustment — call or put — increments Trade.adjustment_count
# by 1. It is NOT a per-leg counter.

_ADJUSTMENT_LIMIT_UNSET_LOGGED: set[int] = set()

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


def _fees_from_legs(
    call_leg: Any,
    put_leg: Any,
    trade: Any,
    db_session: Any,
    wing_call_leg: Any | None = None,
    wing_put_leg: Any | None = None,
) -> float:
    """Sum actual fees paid on basket legs (DB when available — includes wings)."""
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
        for x in (call_leg, put_leg, wing_call_leg, wing_put_leg)
        if x is not None and not isinstance(x, Mock)
    ]
    # MagicMock legs: only use explicit numeric fee attrs
    if not fallback:
        total = 0.0
        for leg in (call_leg, put_leg, wing_call_leg, wing_put_leg):
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
    wing_call_leg: Any | None = None,
    wing_put_leg: Any | None = None,
) -> float:
    """
    Net MTM for exit/adjust decisions — same formula as frontend display.

    net = gross − fees_paid − est_exit_fees − slippage
    """
    from backend.core.fees import compute_net_mtm

    fees_paid = _fees_from_legs(
        call_leg,
        put_leg,
        trade,
        db_session,
        wing_call_leg=wing_call_leg,
        wing_put_leg=wing_put_leg,
    )
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
        *,
        wing_call_leg: Any | None = None,
        wing_put_leg: Any | None = None,
        wing_call_premium: float | None = None,
        wing_put_premium: float | None = None,
    ) -> float:
        """
        Total P&L USD = realized (closed legs) + unrealized (open shorts + wings).

        Uses compute_signed_upnl:
          SHORT size = −qty  → (entry − mark) × qty × CV
          WING  size = +qty  → (mark − entry) × qty × CV
        """
        call_upnl = 0.0
        put_upnl = 0.0
        wing_upnl = 0.0
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
        if (
            wing_call_leg is not None
            and str(getattr(wing_call_leg, "status", "open")).lower() == "open"
            and wing_call_premium is not None
        ):
            wing_upnl += compute_signed_upnl(
                float(wing_call_leg.initial_premium),
                float(wing_call_premium),
                size=+abs(int(wing_call_leg.quantity)),
                contract_value=OPTIONS_CONTRACT_VALUE,
            )
        if (
            wing_put_leg is not None
            and str(getattr(wing_put_leg, "status", "open")).lower() == "open"
            and wing_put_premium is not None
        ):
            wing_upnl += compute_signed_upnl(
                float(wing_put_leg.initial_premium),
                float(wing_put_premium),
                size=+abs(int(wing_put_leg.quantity)),
                contract_value=OPTIONS_CONTRACT_VALUE,
            )
        return float(realized_pnl or 0.0) + call_upnl + put_upnl + wing_upnl

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

    def _read_combined_trigger_flags(
        self, trade: Any, db_session: Any
    ) -> tuple[bool, bool]:
        """
        Fresh SQL read of combined_trigger_mode from trades + auto_trade_settings.

        Do NOT trust in-memory getattr alone — detached TradeState.trade stays
        stale after AutoTrade UI toggles the flag in SQLite.
        Returns (trade_flag, settings_flag).
        """
        trade_flag = bool(getattr(trade, "combined_trigger_mode", False))
        settings_flag = False
        trade_id = int(getattr(trade, "id", 0) or 0)
        if db_session is None:
            return trade_flag, settings_flag
        try:
            from backend.models import AutoTradeSettings, Trade

            if trade_id > 0:
                db_trade_val = (
                    db_session.query(Trade.combined_trigger_mode)
                    .filter(Trade.id == trade_id)
                    .scalar()
                )
                if db_trade_val is not None:
                    trade_flag = bool(db_trade_val)
            db_settings_val = (
                db_session.query(AutoTradeSettings.combined_trigger_mode)
                .filter(AutoTradeSettings.id == 1)
                .scalar()
            )
            if db_settings_val is not None:
                settings_flag = bool(db_settings_val)
        except Exception as exc:
            logger.warning(
                "[COMBINED_TRIGGER_MODE] DB flag read failed trade=%s: %s",
                trade_id or "?",
                exc,
                exc_info=True,
            )
            # Fallback: settings via helper (may still work)
            try:
                from backend.database import get_or_create_auto_settings

                _ats = get_or_create_auto_settings(db_session)
                settings_flag = bool(
                    getattr(_ats, "combined_trigger_mode", False)
                )
            except Exception as exc2:
                logger.warning(
                    "[COMBINED_TRIGGER_MODE] settings fallback failed: %s",
                    exc2,
                )
        return trade_flag, settings_flag

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
        gross_mtm_for_sl: float | None = None,
        wing_call_leg: Any | None = None,
        wing_put_leg: Any | None = None,
        wing_call_premium: float | None = None,
        wing_put_premium: float | None = None,
    ) -> TradeAction:
        """
        Evaluate exits then adjustment triggers (exact priority):

        a. STOPLOSS — ALWAYS evaluated (settling never suppresses it)
        b. settling → skip PROFIT_TARGET and adjustment only
        c. Net MTM >= profit_target → PROFIT_TARGET
        d. pre-expiry window → PRE_EXPIRY
        d2. premium decay remaining <= threshold → PREMIUM_DECAY
        e. adjustment trigger + decision (Net MTM > 0 → close, else adjust)
        f. HOLD

        When ``net_mtm`` is provided (from bot_engine), use it for TP / adjust.
        Otherwise compute via compute_net_mtm (gross − fees − slip).
        """
        # Resolve wings from DB when not passed (single source: basket_legs)
        if wing_call_leg is None or wing_put_leg is None:
            try:
                from backend.core.basket_legs import basket_legs as _basket_legs

                bl = _basket_legs(trade, db_session)
                if wing_call_leg is None:
                    wing_call_leg = bl.get("wing_call")
                if wing_put_leg is None:
                    wing_put_leg = bl.get("wing_put")
            except Exception:
                pass

        logger.info(
            "[ON_TICK_PARAMS] trade_id=%s combined_trigger_mode_trade=%s",
            getattr(trade, "id", "?"),
            getattr(trade, "combined_trigger_mode", "MISSING"),
        )
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "ON_TICK_PARAMS",
                int(getattr(trade, "id", 0) or 0),
                {
                    "combined_trigger_mode_trade": getattr(
                        trade, "combined_trigger_mode", "MISSING"
                    ),
                },
            )
        except Exception:
            pass

        calculated_pnl = self.calculate_pnl(
            trade,
            call_leg,
            put_leg,
            call_premium,
            put_premium,
            realized_pnl=float(realized_pnl or 0.0),
            wing_call_leg=wing_call_leg,
            wing_put_leg=wing_put_leg,
            wing_call_premium=wing_call_premium,
            wing_put_premium=wing_put_premium,
        )
        # Primary: realized + Delta UPNL. Fallback: calculated short+wing PnL.
        if delta_mtm is not None:
            total_pnl = float(realized_pnl or 0.0) + float(delta_mtm)
        else:
            total_pnl = calculated_pnl

        settling = get_settling_info(
            getattr(trade, "monitoring_starts_at", None),
            getattr(trade, "adjust_settling_until", None),
        )
        is_settling = bool(settling.get("is_settling"))
        trade_id = int(getattr(trade, "id", 0) or 0)

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
                wing_call_leg=wing_call_leg,
                wing_put_leg=wing_put_leg,
            )

        # Gross MTM for SL: MUST use gross_mtm_for_sl from bot_engine
        # (gross + latest entry-event spread). NEVER use net_mtm / decision_pnl
        # for stop-loss — fees + entry spread would false-trigger immediately.
        if gross_mtm_for_sl is not None:
            sl_mtm = float(gross_mtm_for_sl)
        else:
            try:
                from backend.core.fees import get_entry_spread_for_sl

                entry_spread_for_sl = get_entry_spread_for_sl(trade)
            except Exception:
                entry_spread_for_sl = 0.0
            sl_mtm = float(total_pnl) + entry_spread_for_sl
            logger.warning(
                "Trade %s: gross_mtm_for_sl not passed — fallback "
                "sl_mtm=total_pnl+entry_spread_for_sl=%.4f (still NOT using net_mtm)",
                getattr(trade, "id", "?"),
                sl_mtm,
            )

        sl_limit = abs(float(trade.stoploss_usd or 0.0))
        tp_limit = float(trade.profit_target_usd or 0.0)

        # Profit target (Net MTM) — skipped while settling
        should_exit_profit = (not is_settling) and decision_pnl >= tp_limit
        # Stop loss (Gross MTM for SL) — NEVER suppressed by settling
        should_exit_sl = sl_limit > 0 and sl_mtm <= -sl_limit

        # Explicit audit: prove SL decision is not based on net
        if sl_limit > 0 and decision_pnl <= -sl_limit and not should_exit_sl:
            logger.info(
                "Trade %s: net_mtm=%.4f would trip SL (limit=%.4f) but "
                "gross_mtm_for_sl=%.4f has breathing room — HOLD on SL",
                getattr(trade, "id", "?"),
                decision_pnl,
                sl_limit,
                sl_mtm,
            )

        # STOPLOSS first and always — settling cannot block it
        if should_exit_sl:
            if is_settling:
                logger.warning(
                    "[SETTLING_BYPASS] trade_id=%s check=stoploss "
                    "gross_mtm_for_sl=%.4f limit=%.4f",
                    trade_id,
                    sl_mtm,
                    sl_limit,
                )
                try:
                    from backend.core.bot_logger import log_and_buffer

                    log_and_buffer(
                        "SETTLING_BYPASS",
                        trade_id,
                        {
                            "check": "stoploss",
                            "gross_mtm_for_sl": round(sl_mtm, 4),
                            "stoploss": round(sl_limit, 4),
                            "minutes_left": settling.get("settling_minutes_left"),
                        },
                    )
                except Exception:
                    pass
            logger.info(
                "Trade %s: gross_mtm=%.2f gross_mtm_for_sl=%.2f net_mtm=%.2f | "
                "target=%s | sl=%s | action=EXIT STOPLOSS "
                "(gross_mtm_for_sl<=-stoploss; settling=%s)",
                getattr(trade, "id", "?"),
                total_pnl,
                sl_mtm,
                decision_pnl,
                trade.profit_target_usd,
                sl_limit,
                is_settling,
            )
            return TradeAction(
                should_exit=True,
                exit_reason="STOPLOSS",
                current_pnl=decision_pnl,
            )

        # Entry / adjustment settling: skip TP and adjustment only
        if is_settling:
            logger.info(
                "Trade %s settling (%s)... %sm remaining — "
                "TP/adjust skipped (SL still active)",
                getattr(trade, "id", "?"),
                settling.get("settling_source", "?"),
                settling["settling_minutes_left"],
            )
            return TradeAction(current_pnl=total_pnl)

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

        # d. Pre-expiry
        hours_left = get_hours_to_expiry(trade.expiry_date)
        if hours_left == 0 or is_pre_expiry_window(trade.expiry_date):
            return TradeAction(
                should_exit=True,
                exit_reason="PRE_EXPIRY",
                current_pnl=decision_pnl,
            )

        # d2. Premium decay exit (B26) — after TP/pre-expiry, before adjustment
        decay_enabled = False
        decay_pct = 50.0
        decay_mode = "both_legs"
        if db_session is not None:
            try:
                from backend.database import get_or_create_auto_settings

                decay_settings = get_or_create_auto_settings(db_session)
                decay_enabled = bool(
                    getattr(decay_settings, "basket_decay_exit_enabled", False)
                )
                decay_pct = float(
                    getattr(decay_settings, "basket_decay_exit_pct", None) or 50.0
                )
                decay_mode = str(
                    getattr(decay_settings, "basket_decay_exit_mode", None)
                    or "both_legs"
                ).lower().strip()
            except Exception as exc:
                logger.warning(
                    "Premium decay settings load failed trade=%s: %s",
                    trade_id,
                    exc,
                )

        should_decay, decay_detail = evaluate_premium_decay_exit(
            call_leg=call_leg,
            put_leg=put_leg,
            call_premium=float(call_premium),
            put_premium=float(put_premium),
            enabled=decay_enabled,
            decay_pct=decay_pct,
            mode=decay_mode,
            trade_id=trade_id,
            wing_call_leg=wing_call_leg,
            wing_put_leg=wing_put_leg,
            wing_call_premium=wing_call_premium,
            wing_put_premium=wing_put_premium,
        )
        if decay_enabled:
            logger.info(
                "[DECAY_EXIT] trade=%s enabled=%s mode=%s threshold=%.2f "
                "should_exit=%s block=%s detail=%s",
                trade_id,
                decay_enabled,
                decay_detail.get("mode"),
                float(decay_detail.get("threshold_pct") or 0),
                should_decay,
                decay_detail.get("block_reason"),
                decay_detail.get("legs"),
            )
            try:
                from backend.core.bot_logger import log_and_buffer

                log_and_buffer(
                    "DECAY_EXIT",
                    trade_id,
                    {
                        **decay_detail,
                        "combined_remaining_pct": decay_detail.get(
                            "combined_remaining_pct"
                        ),
                    },
                )
            except Exception:
                pass
        if should_decay:
            return TradeAction(
                should_exit=True,
                exit_reason="PREMIUM_DECAY",
                current_pnl=decision_pnl,
            )

        # ── Adjustment trigger (SHORT legs only — intentional) ──────────────
        # Wings (long OTM) never enter trigger/baseline math. Wing premium
        # growth is a loss on the long side; adjusting on it would fire in the
        # wrong direction. Triggers use call_leg / put_leg (shorts) only.
        # Locked by backend/tests/test_wing_trigger_shorts_only.py.
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

        # Validate baselines before trigger calc (hot path — no DB commit)
        if call_open:
            call_baseline = float(
                getattr(call_leg, "trigger_baseline_premium", None) or 0
            )
            if call_baseline <= 0:
                call_baseline = float(getattr(call_leg, "initial_premium", 0) or 0)
                logger.warning(
                    "Trade %s CALL has invalid baseline. Resetting to initial: %s",
                    getattr(trade, "id", "?"),
                    call_baseline,
                )
                call_leg.trigger_baseline_premium = call_baseline
                if hasattr(call_leg, "trigger_premium"):
                    call_leg.trigger_premium = call_baseline
        else:
            call_baseline = 0.0

        if put_open:
            put_baseline = float(
                getattr(put_leg, "trigger_baseline_premium", None) or 0
            )
            if put_baseline <= 0:
                put_baseline = float(getattr(put_leg, "initial_premium", 0) or 0)
                logger.warning(
                    "Trade %s PUT has invalid baseline. Resetting to initial: %s",
                    getattr(trade, "id", "?"),
                    put_baseline,
                )
                put_leg.trigger_baseline_premium = put_baseline
                if hasattr(put_leg, "trigger_premium"):
                    put_leg.trigger_premium = put_baseline
        else:
            put_baseline = 0.0

        # Prefer validated baselines; fall back through _trigger_baseline
        if call_baseline <= 0 and call_open:
            call_baseline = _trigger_baseline(call_leg)
        if put_baseline <= 0 and put_open:
            put_baseline = _trigger_baseline(put_leg)

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
        logger.debug(
            "[TRIGGER_CHECK] Trade %s | "
            "CALL: offer=%.2f baseline=%.2f trigger_at=%.2f (%.1f%%) | "
            "PUT: offer=%.2f baseline=%.2f trigger_at=%.2f (%.1f%%)",
            getattr(trade, "id", "?"),
            call_premium,
            call_baseline,
            call_trigger_price,
            call_ratio,
            put_premium,
            put_baseline,
            put_trigger_price,
            put_ratio,
        )
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "TRIGGER_CALC",
                int(getattr(trade, "id", 0) or 0),
                {
                    "call_offer": round(float(call_premium), 2),
                    "call_baseline": round(float(call_baseline), 2),
                    "call_trigger_at": round(float(call_trigger_price), 2),
                    "call_ratio_pct": round(float(call_ratio), 1),
                    "put_offer": round(float(put_premium), 2),
                    "put_baseline": round(float(put_baseline), 2),
                    "put_trigger_at": round(float(put_trigger_price), 2),
                    "put_ratio_pct": round(float(put_ratio), 1),
                },
            )
        except Exception:
            pass

        # --- Combined vs individual trigger (MUST run before per-leg adjust) ---
        # Fresh DB read every tick — in-memory trade / settings objects go stale
        # after AutoTrade UI toggle; getattr on detached ORM lied as False.
        trade_flag, settings_flag = self._read_combined_trigger_flags(
            trade, db_session
        )
        combined_trigger_mode = bool(settings_flag or trade_flag)
        # Keep in-memory trade in sync for the rest of this tick / next push
        try:
            trade.combined_trigger_mode = combined_trigger_mode
        except Exception:
            pass

        logger.info(
            "[COMBINED_TRIGGER_MODE] Trade#%s active=%s "
            "(settings=%s trade=%s)",
            getattr(trade, "id", "?"),
            combined_trigger_mode,
            settings_flag,
            trade_flag,
        )
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "COMBINED_TRIGGER_MODE",
                int(getattr(trade, "id", 0) or 0),
                {
                    "active": combined_trigger_mode,
                    "settings": settings_flag,
                    "trade": trade_flag,
                },
            )
        except Exception:
            pass

        if combined_trigger_mode and call_open and put_open:
            call_entry = float(getattr(call_leg, "initial_premium", 0) or 0)
            put_entry = float(getattr(put_leg, "initial_premium", 0) or 0)
            combined_current = float(call_premium) + float(put_premium)
            combined_entry = call_entry + put_entry
            if mode == "premium":
                trigger_pct = (call_trigger_pct + put_trigger_pct) / 2.0
            else:
                trigger_pct = float(call_trigger_pct)
            combined_threshold = (
                combined_entry * (trigger_pct / 100.0)
                if combined_entry > 0
                else 0.0
            )
            logger.info(
                "[COMBINED_CALC] combined_current=%.2f threshold=%.2f "
                "active=%s entry=%.2f pct=%.1f",
                combined_current,
                combined_threshold,
                True,
                combined_entry,
                trigger_pct,
            )
            try:
                from backend.core.bot_logger import log_and_buffer

                log_and_buffer(
                    "COMBINED_CALC",
                    int(getattr(trade, "id", 0) or 0),
                    {
                        "combined_current": round(combined_current, 2),
                        "threshold": round(combined_threshold, 2),
                        "combined_entry": round(combined_entry, 2),
                        "trigger_pct": round(trigger_pct, 1),
                        "active": True,
                    },
                )
            except Exception:
                pass

            if combined_entry > 0 and combined_current >= combined_threshold:
                call_pct = (
                    (call_premium / call_entry) if call_entry > 0 else 0.0
                )
                put_pct = (put_premium / put_entry) if put_entry > 0 else 0.0
                triggered_leg = "call" if call_pct >= put_pct else "put"
                trig_pct_hit = (
                    call_trigger_pct
                    if triggered_leg == "call"
                    else put_trigger_pct
                )
                logger.info(
                    "[COMBINED_TRIGGER] Trade#%s combined=%.2f threshold=%.2f "
                    "call_pct=%.1f%% put_pct=%.1f%% triggered=%s",
                    getattr(trade, "id", "?"),
                    combined_current,
                    combined_threshold,
                    call_pct * 100.0,
                    put_pct * 100.0,
                    triggered_leg,
                )
                try:
                    from backend.core.bot_logger import log_and_buffer

                    log_and_buffer(
                        "COMBINED_TRIGGER",
                        int(getattr(trade, "id", 0) or 0),
                        {
                            "combined": round(combined_current, 2),
                            "threshold": round(combined_threshold, 2),
                            "triggered_leg": triggered_leg,
                            "call_pct": round(call_pct * 100.0, 1),
                            "put_pct": round(put_pct * 100.0, 1),
                        },
                    )
                except Exception:
                    pass
                if net_for_decision > 0:
                    logger.info(
                        "DECISION: Net MTM profitable at combined trigger — "
                        "closing basket | Trade %s Net MTM=%.2f",
                        getattr(trade, "id", "?"),
                        net_for_decision,
                    )
                    return TradeAction(
                        should_exit=True,
                        exit_reason="DECISION_PROFIT_AT_TRIGGER",
                        current_pnl=net_for_decision,
                        triggered_leg=triggered_leg,
                        trigger_pct_hit=trig_pct_hit,
                        trigger_pct_used=trigger_pct,
                        call_trigger_pct=call_trigger_pct,
                        put_trigger_pct=put_trigger_pct,
                    )
                max_exit = self._check_max_adjustments_exit(
                    trade,
                    db_session,
                    triggered_leg=triggered_leg,
                    trigger_pct=trig_pct_hit,
                    net_for_decision=net_for_decision,
                    call_trigger_pct=call_trigger_pct,
                    put_trigger_pct=put_trigger_pct,
                )
                if max_exit is not None:
                    return max_exit
                return TradeAction(
                    should_adjust=True,
                    adjust_leg=triggered_leg,
                    current_pnl=net_for_decision,
                    triggered_leg=triggered_leg,
                    trigger_pct_hit=trig_pct_hit,
                    trigger_pct_used=trigger_pct,
                    call_trigger_pct=call_trigger_pct,
                    put_trigger_pct=put_trigger_pct,
                )
            # Combined mode ON but under threshold — never fall through to
            # individual leg triggers
            logger.info(
                "Trade %s decision: combined mode HOLD | "
                "combined=%.2f threshold=%.2f | net_mtm=%.2f",
                getattr(trade, "id", "?"),
                combined_current,
                combined_threshold,
                decision_pnl,
            )
            return TradeAction(
                current_pnl=decision_pnl,
                trigger_pct_used=trigger_pct,
                call_trigger_pct=call_trigger_pct,
                put_trigger_pct=put_trigger_pct,
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
                # Max-adjustments gate — must run before any ADJUST decision
                max_exit = self._check_max_adjustments_exit(
                    trade, db_session, triggered_leg="call",
                    trigger_pct=call_trigger_pct,
                    net_for_decision=net_for_decision,
                    call_trigger_pct=call_trigger_pct,
                    put_trigger_pct=put_trigger_pct,
                )
                if max_exit is not None:
                    return max_exit
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
                max_exit = self._check_max_adjustments_exit(
                    trade, db_session, triggered_leg="put",
                    trigger_pct=put_trigger_pct,
                    net_for_decision=net_for_decision,
                    call_trigger_pct=call_trigger_pct,
                    put_trigger_pct=put_trigger_pct,
                )
                if max_exit is not None:
                    return max_exit
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

    def _check_max_adjustments_exit(
        self,
        trade: Any,
        db_session: Any,
        *,
        triggered_leg: str,
        trigger_pct: float,
        net_for_decision: float,
        call_trigger_pct: float,
        put_trigger_pct: float,
    ) -> TradeAction | None:
        """
        Gate BEFORE choosing ADJUST_*: exit whole basket when the per-basket
        adjustment limit is reached.

        adjustment_count is read fresh from the DB row — never from cached
        trade_state / position_tracker copies.
        """
        if db_session is None:
            return None
        try:
            from backend.core.bot_logger import log_and_buffer
            from backend.database import get_or_create_auto_settings
            from backend.models import Trade as TradeModel

            trade_id = int(getattr(trade, "id", 0) or 0)
            cfg = get_or_create_auto_settings(db_session)
            raw_max = getattr(cfg, "max_adjustments_per_basket", None)
            max_allowed: int | None = (
                int(raw_max) if raw_max is not None else None
            )

            count = 0
            if trade_id > 0:
                scalar = (
                    db_session.query(TradeModel.adjustment_count)
                    .filter(TradeModel.id == trade_id)
                    .scalar()
                )
                try:
                    count = int(scalar or 0)
                except (TypeError, ValueError):
                    count = 0

            if max_allowed is None:
                if trade_id > 0 and trade_id not in _ADJUSTMENT_LIMIT_UNSET_LOGGED:
                    _ADJUSTMENT_LIMIT_UNSET_LOGGED.add(trade_id)
                    logger.warning(
                        "[ADJUSTMENT_LIMIT_UNSET] trade=%s",
                        trade_id,
                    )
                    log_and_buffer("ADJUSTMENT_LIMIT_UNSET", trade_id, {})
                return None

            if count < max_allowed:
                return None

            logger.warning(
                "[MAX_ADJUSTMENTS_REACHED] trade=%s | count=%s | max=%s | "
                "triggered_leg=%s | net_mtm=%s",
                trade_id,
                count,
                max_allowed,
                triggered_leg,
                round(float(net_for_decision), 4),
            )
            log_and_buffer(
                "MAX_ADJUSTMENTS_REACHED",
                trade_id,
                {
                    "count": count,
                    "max": max_allowed,
                    "triggered_leg": triggered_leg,
                    "net_mtm": round(float(net_for_decision), 4),
                },
            )
            return TradeAction(
                should_exit=True,
                exit_reason="MAX_ADJUSTMENTS_REACHED",
                current_pnl=net_for_decision,
                triggered_leg=triggered_leg,
                trigger_pct_hit=trigger_pct,
                trigger_pct_used=trigger_pct,
                call_trigger_pct=call_trigger_pct,
                put_trigger_pct=put_trigger_pct,
            )
        except Exception as exc:
            logger.warning("max-adjustments check failed: %s", exc)
            return None

    async def find_adjustment_strike(
        self,
        delta_client: Any,
        trade: Any,
        triggered_leg_type: str,
        other_leg_current_premium: float,
        current_strike: float | None = None,
        target_premium_override: float | None = None,
        untouched_leg_offer: float | None = None,
    ) -> AdjustmentPlan:
        """Find replacement strike with premium >= basket target.

        ``other_leg_current_premium`` is the final target_new_premium
        (untouched offer + basket net loss). ``untouched_leg_offer`` is the
        live offer of the non-triggered short leg (for audit logs only).

        ``target_premium_override`` is ignored — basket formula is the single
        rule (kept as a kwarg for call-site compatibility).
        """
        leg = triggered_leg_type.lower().strip()
        underlying_key = str(trade.underlying).upper()
        underlying_symbol = UNDERLYING_SYMBOLS.get(underlying_key, underlying_key)

        expiry = trade.expiry_date
        if isinstance(expiry, date):
            expiry_str = expiry.isoformat()
        else:
            expiry_str = str(expiry)

        # Basket formula already computed by caller — never replace it.
        if target_premium_override is not None:
            logger.warning(
                "find_adjustment_strike: ignoring target_premium_override=%.4f "
                "(basket formula is the single target rule)",
                float(target_premium_override),
            )
        final_target = float(other_leg_current_premium)
        untouched_for_log = (
            float(untouched_leg_offer)
            if untouched_leg_offer is not None
            else None
        )

        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "ADJUSTMENT_TARGET_PREMIUM",
                int(getattr(trade, "id", 0) or 0),
                {
                    "final_target": round(final_target, 2),
                    "override_used": False,
                    "other_leg_offer": (
                        round(untouched_for_log, 2)
                        if untouched_for_log is not None
                        else None
                    ),
                    "target_new_premium": round(final_target, 2),
                },
            )
            logger.info(
                "[ADJUSTMENT_TARGET_PREMIUM] trade_id=%s final_target=%.2f "
                "other_leg_offer=%s",
                int(getattr(trade, "id", 0) or 0),
                final_target,
                (
                    round(untouched_for_log, 2)
                    if untouched_for_log is not None
                    else None
                ),
            )
        except Exception:
            pass

        # Directional farther-OTM search only (never roll toward the money).
        # Closest premium to target among UP calls / DOWN puts.
        from backend.core.bot_logger import log_and_buffer
        from backend.core.delta_client import DeltaAPIError

        trade_id = int(getattr(trade, "id", 0) or 0)
        old_strike = (
            float(current_strike) if current_strike is not None else None
        )
        direction = "UP" if leg == "call" else "DOWN"

        tolerance_pct = 40.0
        try:
            from backend.database import SessionLocal, get_or_create_auto_settings

            with SessionLocal() as _db:
                _settings = get_or_create_auto_settings(_db)
                tolerance_pct = float(
                    getattr(_settings, "adjustment_premium_tolerance_pct", None)
                    or 40.0
                )
        except Exception as exc:
            logger.warning(
                "find_adjustment_strike: tolerance setting read failed: %s",
                exc,
            )
        tolerance_pct = max(5.0, min(200.0, tolerance_pct))

        try:
            row = await delta_client.find_strike_by_premium(
                underlying=underlying_symbol,
                expiry_date=expiry_str,
                leg_type=leg,
                target_premium=float(final_target),
                exclude_strike=old_strike,
                require_farther_otm=True,
            )
        except DeltaAPIError as exc:
            best_candidate = None
            try:
                log_and_buffer(
                    "ADJUSTMENT_ABORT",
                    trade_id,
                    {
                        "trade": trade_id,
                        "leg": leg,
                        "reason": "no_valid_strike",
                        "old_strike": old_strike,
                        "target_premium": round(final_target, 4),
                        "best_candidate": best_candidate,
                        "error": str(exc),
                        "summary": (
                            f"[ADJUSTMENT_ABORT] trade={trade_id} leg={leg} "
                            f"reason=no_valid_strike | old_strike={old_strike} "
                            f"target_premium={round(final_target, 4)} "
                            f"best_candidate={best_candidate}"
                        ),
                    },
                )
            except Exception:
                pass
            raise ValueError(
                f"ADJUSTMENT_ABORT: no valid {leg} strike {direction} from "
                f"{old_strike} for target={final_target:.2f}: {exc}"
            ) from exc

        new_strike = float(row.get("strike") or 0)
        mark_key = "call_mark_price" if leg == "call" else "put_mark_price"
        new_premium = float(row.get(mark_key) or 0)
        method = str(row.get("_match_method") or "closest_farther_otm")
        candidates_scanned = int(row.get("_candidates_scanned") or 0)
        deviation_pct = float(
            row.get("_deviation_pct")
            if row.get("_deviation_pct") is not None
            else (
                abs(new_premium - final_target) / final_target * 100.0
                if final_target > 0
                else 0.0
            )
        )
        wing_clamp_bypass_tolerance = False

        # ── Wing cross-guard: short must stay STRICTLY inside wing ──
        if old_strike is not None:
            try:
                from backend.database import SessionLocal
                from backend.engine.wing_exit import (
                    clamp_short_strike_inside_wing,
                    find_chain_row_for_strike,
                    get_open_wing_strikes,
                )
                from backend.models import Leg as _Leg

                with SessionLocal() as _wdb:
                    _wlegs = (
                        _wdb.query(_Leg)
                        .filter(
                            _Leg.trade_id == trade_id,
                            _Leg.status == "open",
                            _Leg.is_bot_managed.is_(True),
                        )
                        .all()
                    )
                    wing_call_k, wing_put_k = get_open_wing_strikes(_wlegs)
                wing_k = wing_call_k if leg == "call" else wing_put_k
                if wing_k is not None:
                    chain = await delta_client.get_option_chain(
                        underlying_symbol, expiry_str
                    )
                    avail = []
                    for cr in chain or []:
                        try:
                            avail.append(float(cr.get("strike") or 0))
                        except (TypeError, ValueError):
                            continue
                    clamped, clamp_status = clamp_short_strike_inside_wing(
                        leg=leg,
                        wanted_strike=new_strike,
                        wing_strike=float(wing_k),
                        available_strikes=avail,
                        current_short_strike=float(old_strike),
                    )
                    if clamp_status == "dead_end":
                        try:
                            log_and_buffer(
                                "ADJUSTMENT_ABORT",
                                trade_id,
                                {
                                    "trade": trade_id,
                                    "leg": leg,
                                    "reason": "CHAIN_EXHAUSTED",
                                    "old_strike": old_strike,
                                    "wanted": new_strike,
                                    "wing": wing_k,
                                    "summary": (
                                        f"[CHAIN_EXHAUSTED] wing cross-guard "
                                        f"dead_end trade={trade_id} leg={leg} "
                                        f"wanted={new_strike} wing={wing_k}"
                                    ),
                                },
                            )
                        except Exception:
                            pass
                        raise ValueError(
                            f"ADJUSTMENT_ABORT: CHAIN_EXHAUSTED wing cross-guard "
                            f"dead_end for {leg} wanted={new_strike} wing={wing_k} "
                            f"from {old_strike}"
                        )
                    if clamp_status == "clamped" and clamped is not None:
                        wanted_prem = new_premium
                        crow = find_chain_row_for_strike(
                            chain or [], leg=leg, strike=float(clamped)
                        )
                        if crow is None:
                            raise ValueError(
                                f"ADJUSTMENT_ABORT: CHAIN_EXHAUSTED clamped "
                                f"strike {clamped} missing from chain"
                            )
                        row = crow
                        new_strike = float(clamped)
                        new_premium = float(row.get(mark_key) or 0)
                        method = "wing_cross_guard_clamp"
                        deviation_pct = (
                            abs(new_premium - final_target) / final_target * 100.0
                            if final_target > 0
                            else 0.0
                        )
                        if deviation_pct > tolerance_pct:
                            wing_clamp_bypass_tolerance = True
                            logger.warning(
                                "[WING_CROSS_GUARD_TOLERANCE_BYPASS] "
                                "wanted_prem=%.4f picked_prem=%.4f pct_off=%.2f",
                                wanted_prem,
                                new_premium,
                                deviation_pct,
                            )
                            try:
                                log_and_buffer(
                                    "WING_CROSS_GUARD_TOLERANCE_BYPASS",
                                    trade_id,
                                    {
                                        "wanted_prem": round(wanted_prem, 4),
                                        "picked_prem": round(new_premium, 4),
                                        "pct_off": round(deviation_pct, 2),
                                        "wanted_strike": float(
                                            row.get("_wanted_strike") or 0
                                        )
                                        or None,
                                        "clamped_to": new_strike,
                                        "wing": wing_k,
                                    },
                                )
                            except Exception:
                                pass
            except ValueError:
                raise
            except Exception as wing_exc:
                logger.warning(
                    "wing cross-guard failed (non-fatal continue): %s",
                    wing_exc,
                )

        other_offer = (
            float(untouched_for_log)
            if untouched_for_log is not None
            else float(other_leg_current_premium)
        )

        # Hard directional guard — never roll toward the money
        invalid_direction = False
        if old_strike is not None:
            if leg == "call" and new_strike <= old_strike:
                invalid_direction = True
            if leg == "put" and new_strike >= old_strike:
                invalid_direction = True
        if invalid_direction:
            try:
                log_and_buffer(
                    "ADJUSTMENT_ABORT",
                    trade_id,
                    {
                        "trade": trade_id,
                        "leg": leg,
                        "reason": "no_valid_strike",
                        "old_strike": old_strike,
                        "target_premium": round(final_target, 4),
                        "best_candidate": new_strike,
                        "selected_premium": round(new_premium, 4),
                        "summary": (
                            f"[ADJUSTMENT_ABORT] trade={trade_id} leg={leg} "
                            f"reason=no_valid_strike | old_strike={old_strike} "
                            f"target_premium={round(final_target, 4)} "
                            f"best_candidate={new_strike}"
                        ),
                    },
                )
            except Exception:
                pass
            raise ValueError(
                f"ADJUSTMENT_ABORT: {leg} candidate {new_strike} not {direction} "
                f"from {old_strike} (target={final_target:.2f})"
            )

        if old_strike is not None and abs(new_strike - old_strike) < 0.01:
            raise ValueError(
                f"SAME_STRIKE_HOLD: nearest match is still {new_strike} "
                f"(no alternate strike for {leg})"
            )

        if deviation_pct > tolerance_pct and not wing_clamp_bypass_tolerance:
            try:
                log_and_buffer(
                    "ADJUSTMENT_PREMIUM_MISS",
                    trade_id,
                    {
                        "trade": trade_id,
                        "target": round(final_target, 4),
                        "selected": round(new_premium, 4),
                        "deviation_pct": round(deviation_pct, 2),
                        "tolerance_pct": tolerance_pct,
                        "summary": (
                            f"[ADJUSTMENT_PREMIUM_MISS] trade={trade_id} "
                            f"target={round(final_target, 4)} "
                            f"selected={round(new_premium, 4)} "
                            f"deviation_pct={round(deviation_pct, 2)}"
                        ),
                    },
                )
            except Exception:
                pass
            logger.warning(
                "[ADJUSTMENT_PREMIUM_MISS] trade=%s target=%.2f selected=%.2f "
                "deviation_pct=%.1f (tolerance=%.1f) — proceeding (direction OK)",
                trade_id,
                final_target,
                new_premium,
                deviation_pct,
                tolerance_pct,
            )

        try:
            log_and_buffer(
                "NEW_STRIKE_SELECTED",
                trade_id,
                {
                    "selected_premium": round(new_premium, 4),
                    "other_leg_offer": round(other_offer, 4),
                    "target_new_premium": round(final_target, 4),
                    "method": method,
                    "selected_strike": new_strike,
                    "old_strike": old_strike,
                    "direction": direction,
                    "candidates_scanned": candidates_scanned,
                    "deviation_pct": round(deviation_pct, 2),
                },
            )
        except Exception:
            pass
        logger.info(
            "NEW_STRIKE_SELECTED | old_strike=%s | selected_strike=%s | "
            "direction=%s | selected_premium=%.1f | target_new_premium=%.1f | "
            "deviation_pct=%.1f | candidates_scanned=%s | method=%s",
            old_strike,
            new_strike,
            direction,
            new_premium,
            final_target,
            deviation_pct,
            candidates_scanned,
            method,
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
