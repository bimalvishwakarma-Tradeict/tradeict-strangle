# routes_auto_trade.py — /api/auto-trade/* settings + enable/disable

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from backend.core.bot_logger import log_and_buffer
from backend.core.time_utils import get_ist_now, get_utc_now, to_utc_for_db
from backend.database import get_db, get_or_create_auto_settings
from backend.models import AutoTradeSettings
from backend.strategies.s001_short_strangle.config import SUPPORTED_UNDERLYINGS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auto-trade", tags=["auto-trade"])

_NEXT_ENTRY_SOURCE_LABELS = {
    "reentry_delay": "re-entry delay",
    "cooldown_after_loss": "cooldown after loss",
    "retry": "retry backoff",
    "hedge_gate": "hedge gate backoff",
    "expiry_too_close": "expiry too close",
}


class AutoTradeSettingsSchema(BaseModel):
    underlying: str = "BTC"
    expiry_dte: int = Field(default=1, ge=0, le=90)
    expiry_date_override: str | None = None
    quantity: int = Field(default=1, ge=1, le=1000)
    re_entry_delay_minutes: int = Field(default=1, ge=0, le=1440)
    entry_settling_seconds: int = Field(default=60, ge=0, le=300)
    adjustment_settling_seconds: int = Field(default=20, ge=0, le=300)

    # Risk
    tp_pct: float = Field(default=50.0, gt=0, le=500)
    sl_pct: float = Field(default=100.0, gt=0, le=1000)
    universal_sl_pct: float = Field(default=200.0, ge=100, le=1000)
    slippage_pct: float = Field(default=2.0, ge=0, le=10)

    # Trigger
    trigger_mode: str = "slab"
    combined_trigger_mode: bool = False
    flat_trigger_pct: float = Field(default=150.0, ge=100, le=500)
    slab_24h: float = Field(default=200.0, ge=100, le=500)
    slab_12h: float = Field(default=175.0, ge=100, le=500)
    slab_6h: float = Field(default=150.0, ge=100, le=500)
    slab_lt6h: float = Field(default=150.0, ge=100, le=500)
    premium_slab_300: float = Field(default=150.0, ge=100, le=500)
    premium_slab_200: float = Field(default=160.0, ge=100, le=500)
    premium_slab_100: float = Field(default=180.0, ge=100, le=500)
    premium_slab_lt100: float = Field(default=200.0, ge=100, le=500)

    # Trade structure
    trade_type: str = "straddle"  # 'straddle' or 'strangle'
    target_premium_per_side: float = Field(default=150.0, gt=0, le=10000)
    strangle_premium_mode: str = "fixed"  # fixed | pct_of_hedge
    strangle_premium_pct_of_hedge: float = Field(default=3.0, gt=0, le=100)

    # Low-premium adjustment exit
    adj_low_premium_exit_enabled: bool = False
    adj_low_premium_min_usd: float = Field(default=150.0, ge=10, le=500)
    # Conversion mode + adjustment limit
    conversion_mode_enabled: bool = True
    max_adjustments_per_basket: int | None = Field(default=None, ge=1, le=50)
    premium_cover_loss_enabled: bool | None = None
    is_demo: bool | None = None

    # Hedge mode (config surface only — engine ignores until later steps)
    hedge_enabled: bool = False
    # Relative label key: month_1 | week_2 | 1dte | … (legacy: monthly|date|dte)
    hedge_expiry_mode: str = "month_1"
    hedge_expiry_date_override: str | None = None  # resolved date display only
    hedge_expiry_dte: int | None = Field(default=None, ge=0, le=365)
    min_hedge_dte: int = Field(default=15, ge=0, le=60)
    min_hedge_dte_enabled: bool = True
    hedge_roll_enabled: bool = True
    hedge_force_roll_enabled: bool = True
    hedge_close_at_expiry_enabled: bool = True
    hedge_target_usd: float | None = Field(default=None, gt=0)
    hedge_stoploss_usd: float | None = Field(default=None, gt=0)
    hedge_fixed_sl_usd: float = Field(default=2.0, ge=0.1, le=1000)
    hedge_sl_floor_pct: float = Field(default=25.0, ge=0, le=100)
    hedge_roll_dte: int = Field(default=10, ge=1, le=60)
    hedge_roll_hard_dte: int = Field(default=5, ge=1, le=60)
    hedge_auto_reopen_after_roll: bool = True
    hedge_target_multiple: float = Field(default=3.0, ge=0.5, le=20)
    hedge_expected_monthly_pct: float = Field(default=30.0, ge=1, le=200)
    hedge_min_hold_days: int = Field(default=10, ge=0, le=60)
    # Exit-spread estimation (AUTO from L2 / MANUAL / capped)
    spread_mode: str = "MANUAL"
    basket_exit_spread_pct: float = Field(default=4.0, ge=0, le=20)
    hedge_exit_spread_pct: float = Field(default=4.0, ge=0, le=20)
    spread_cap_pct: float = Field(default=8.0, ge=0, le=20)
    margin_buffer_pct: float = Field(default=50.0, ge=0, le=200)
    strike_selection_mode: str = "fixed_premium"  # fixed_premium | theta_based
    theta_multiplier: float = Field(default=3.0, gt=0, le=20)
    target_mode: str = "payoff_pct"  # payoff_pct | theta_multiplier
    target_theta_pct: float = Field(default=150.0, ge=10, le=1000)
    basket_target_mode: str = "THETA"  # THETA | PCT
    basket_target_multiple: float = Field(default=1.5, ge=0.1, le=10)
    basket_qty_mode: str = "fixed"  # fixed | pct_of_hedge
    basket_qty_pct_of_hedge: float = Field(default=20.0, gt=0, le=1000)
    hedge_qty_lots: int | None = Field(default=None, ge=1, le=10000)
    basket_qty_dynamic: bool = False
    basket_qty_theta_mult: float = Field(default=2.0, ge=0.1, le=10.0)
    use_dynamic_qty_on_adjustment: bool = False  # deprecated → adjustment_qty_mode
    adjustment_qty_mode: str = "unchanged"  # unchanged | increase_dynamic | decrease_step
    adjustment_qty_decrease_pct: float = Field(default=25.0, gt=0, lt=100)
    basket_decay_exit_enabled: bool = False
    basket_decay_exit_pct: float = Field(default=50.0, gt=0, lt=100)
    basket_decay_exit_mode: str = "both_legs"
    cooldown_after_loss_minutes: int = Field(default=120, ge=0, le=1440)
    adjustment_premium_tolerance_pct: float = Field(
        default=40.0, ge=5, le=200
    )
    entry_premium_match_tolerance_pct: float = Field(
        default=25.0, ge=5, le=100
    )
    # Basket wings (condor) — settings only; engine ignores until later steps
    basket_wings_enabled: bool = False
    wing_strike_mode: str = "points"
    wing_points_away: float = Field(default=2000.0, gt=0)
    wing_delta_min: float = Field(default=0.05, gt=0, lt=1)
    wing_delta_max: float = Field(default=0.07, gt=0, lt=1)
    wing_pct_of_premium: float = Field(default=20.0, gt=0, lt=100)
    # Mid-price execution (default OFF)
    midprice_enabled: bool = False
    midprice_chase_max_seconds: int = Field(default=120, ge=10, le=600)
    midprice_hold_seconds: int = Field(default=30, ge=5, le=120)
    midprice_partner_window_seconds: int = Field(default=5, ge=2, le=30)

    @field_validator("trade_type")
    @classmethod
    def validate_trade_type(cls, v: str) -> str:
        normalized = str(v or "straddle").lower().strip()
        if normalized not in {"straddle", "strangle"}:
            raise ValueError("trade_type must be 'straddle' or 'strangle'")
        return normalized

    @field_validator("target_premium_per_side")
    @classmethod
    def validate_premium(cls, v: float) -> float:
        if v <= 0 or v > 10000:
            raise ValueError("target_premium must be between 1 and 10000")
        return float(v)

    @field_validator("strangle_premium_mode")
    @classmethod
    def validate_strangle_premium_mode(cls, v: str) -> str:
        normalized = str(v or "fixed").lower().strip()
        if normalized not in {"fixed", "pct_of_hedge"}:
            return "fixed"
        return normalized

    @field_validator("hedge_expiry_mode")
    @classmethod
    def validate_hedge_expiry_mode(cls, v: str) -> str:
        from backend.core.hedge_theta import (
            LEGACY_HEDGE_EXPIRY_MODES,
            is_relative_expiry_key,
            migrate_hedge_expiry_mode,
        )

        normalized = str(v or "month_1").lower().strip()
        if is_relative_expiry_key(normalized):
            return normalized
        if normalized in LEGACY_HEDGE_EXPIRY_MODES:
            migrated, _ = migrate_hedge_expiry_mode(normalized)
            return migrated if migrated != "date" else "date"
        raise ValueError(
            "hedge_expiry_mode must be a relative label key "
            "(e.g. month_1, week_2, 1dte)"
        )

    @field_validator("strike_selection_mode")
    @classmethod
    def validate_strike_selection_mode(cls, v: str) -> str:
        normalized = str(v or "fixed_premium").lower().strip()
        if normalized not in {"fixed_premium", "theta_based"}:
            raise ValueError(
                "strike_selection_mode must be 'fixed_premium' or 'theta_based'"
            )
        return normalized

    @field_validator("target_mode")
    @classmethod
    def validate_target_mode(cls, v: str) -> str:
        normalized = str(v or "payoff_pct").lower().strip()
        if normalized not in {"payoff_pct", "theta_multiplier"}:
            raise ValueError(
                "target_mode must be 'payoff_pct' or 'theta_multiplier'"
            )
        return normalized

    @field_validator("basket_target_mode")
    @classmethod
    def validate_basket_target_mode(cls, v: str) -> str:
        normalized = str(v or "THETA").upper().strip()
        if normalized not in {"THETA", "PCT"}:
            raise ValueError("basket_target_mode must be 'THETA' or 'PCT'")
        return normalized

    @field_validator("basket_qty_mode")
    @classmethod
    def validate_basket_qty_mode(cls, v: str) -> str:
        normalized = str(v or "fixed").lower().strip()
        if normalized not in {"fixed", "pct_of_hedge"}:
            raise ValueError(
                "basket_qty_mode must be 'fixed' or 'pct_of_hedge'"
            )
        return normalized

    @field_validator("basket_decay_exit_mode")
    @classmethod
    def validate_basket_decay_exit_mode(cls, v: str) -> str:
        normalized = str(v or "both_legs").lower().strip()
        if normalized not in {"both_legs", "combined"}:
            return "both_legs"
        return normalized

    @field_validator("spread_mode")
    @classmethod
    def validate_spread_mode(cls, v: str) -> str:
        normalized = str(v or "AUTO").upper().strip()
        if normalized not in {"AUTO", "MANUAL"}:
            raise ValueError("spread_mode must be 'AUTO' or 'MANUAL'")
        return normalized

    @field_validator("wing_strike_mode")
    @classmethod
    def validate_wing_strike_mode(cls, v: str) -> str:
        from backend.strategies.s001_short_strangle.wing_select import (
            normalize_wing_mode,
        )

        return normalize_wing_mode(v)

    @field_validator("adjustment_qty_mode")
    @classmethod
    def validate_adjustment_qty_mode(cls, v: str) -> str:
        normalized = str(v or "unchanged").lower().strip()
        if normalized not in {
            "unchanged",
            "increase_dynamic",
            "decrease_step",
        }:
            return "unchanged"
        return normalized

    @model_validator(mode="after")
    def validate_hedge_money_when_enabled(self) -> AutoTradeSettingsSchema:
        if float(self.wing_delta_max) < float(self.wing_delta_min):
            raise ValueError(
                "wing_delta_max must be >= wing_delta_min "
                f"(got min={self.wing_delta_min}, max={self.wing_delta_max})"
            )
        if (
            self.min_hedge_dte_enabled
            and self.hedge_roll_enabled
            and self.hedge_force_roll_enabled
        ):
            hard = int(self.hedge_roll_hard_dte)
            roll = int(self.hedge_roll_dte)
            min_dte = int(self.min_hedge_dte)
            if not (hard < roll < min_dte):
                raise ValueError(
                    "Require hedge_roll_hard_dte < hedge_roll_dte < min_hedge_dte "
                    f"(got hard={hard}, roll={roll}, min={min_dte}). "
                    "Roll DTE must be below Minimum hedge DTE, otherwise a newly "
                    "opened hedge would immediately start rolling."
                )
        if not self.hedge_enabled:
            return self
        # hedge_target_usd / hedge_stoploss_usd are legacy display defaults only —
        # live triggers use structure multiple + fixed SL budget.
        return self


def _as_ist(dt: datetime | None) -> datetime | None:
    """Normalize DB datetime to IST (naive = UTC wall-clock)."""
    from backend.core.time_utils import _as_ist as _shared_as_ist

    return _shared_as_ist(dt)


def _reschedule_reentry_if_delay_changed(
    settings: AutoTradeSettings,
    *,
    old_delay: int,
    new_delay: int,
) -> None:
    """
    If a user-preference re-entry wait is pending, recompute from last_exit.
    Safety sources (cooldown / retry / hedge_gate / expiry) are left alone.
    """
    if int(old_delay) == int(new_delay):
        return

    now = get_ist_now()
    next_entry = _as_ist(getattr(settings, "next_entry_time", None))
    if next_entry is None or next_entry <= now:
        return

    source = str(getattr(settings, "next_entry_source", None) or "").strip()
    # Pre-migration NULL → treat as user re-entry delay (the common case)
    if source and source != "reentry_delay":
        logger.info(
            "[REENTRY_NOT_RESCHEDULED] source=%s | next_entry=%s",
            source or "unknown",
            next_entry.isoformat(),
        )
        log_and_buffer(
            "REENTRY_NOT_RESCHEDULED",
            0,
            {
                "source": source or "unknown",
                "next_entry": next_entry.isoformat(),
                "old_delay": int(old_delay),
                "new_delay": int(new_delay),
            },
        )
        return

    last_exit = _as_ist(getattr(settings, "last_exit_time", None))
    if last_exit is None:
        last_exit = next_entry - timedelta(minutes=max(int(old_delay), 1))

    effective = max(int(new_delay), 1)
    new_next = last_exit + timedelta(minutes=effective)
    old_iso = next_entry.isoformat()

    if new_next <= now:
        settings.next_entry_time = None
        settings.next_entry_source = None
        new_iso: str | None = None
    else:
        settings.next_entry_time = to_utc_for_db(
            new_next, context="auto_trade_settings.next_entry_time"
        )
        settings.next_entry_source = "reentry_delay"
        new_iso = new_next.isoformat()

    logger.info(
        "[REENTRY_RESCHEDULED] old=%s | new=%s | old_delay=%s | new_delay=%s",
        old_iso,
        new_iso,
        int(old_delay),
        int(new_delay),
    )
    log_and_buffer(
        "REENTRY_RESCHEDULED",
        0,
        {
            "old": old_iso,
            "new": new_iso,
            "old_delay": int(old_delay),
            "new_delay": int(new_delay),
        },
    )


def _hedge_expiry_fields(s: AutoTradeSettings) -> dict[str, Any]:
    """Migrate legacy hedge expiry modes for API responses."""
    from backend.core.hedge_theta import migrate_hedge_expiry_mode

    raw_mode = str(getattr(s, "hedge_expiry_mode", None) or "month_1")
    dte = getattr(s, "hedge_expiry_dte", None)
    mode, needs_repick = migrate_hedge_expiry_mode(
        raw_mode,
        expiry_dte=int(dte) if dte is not None else None,
    )
    return {
        "hedge_expiry_mode": mode,
        "hedge_expiry_date_override": getattr(
            s, "hedge_expiry_date_override", None
        ),
        "hedge_expiry_dte": (
            int(dte) if dte is not None else None
        ),
        "hedge_expiry_needs_repick": bool(needs_repick),
    }


def settings_to_dict(s: AutoTradeSettings) -> dict[str, Any]:
    now = get_ist_now()
    next_entry = _as_ist(s.next_entry_time)
    seconds_until_entry: int | None = None
    if next_entry is not None:
        seconds_until_entry = max(0, int((next_entry - now).total_seconds()))

    last_exit = _as_ist(s.last_exit_time)
    return {
        "is_enabled": bool(s.is_enabled),
        "underlying": s.underlying,
        "expiry_dte": int(s.expiry_dte),
        "expiry_date_override": getattr(s, "expiry_date_override", None),
        "quantity": int(s.quantity),
        "re_entry_delay_minutes": int(s.re_entry_delay_minutes),
        "entry_settling_seconds": int(
            getattr(s, "entry_settling_seconds", 60)
            if getattr(s, "entry_settling_seconds", None) is not None
            else 60
        ),
        "adjustment_settling_seconds": int(
            getattr(s, "adjustment_settling_seconds", 20)
            if getattr(s, "adjustment_settling_seconds", None) is not None
            else 20
        ),
        "tp_pct": float(s.tp_pct),
        "sl_pct": float(s.sl_pct),
        "universal_sl_pct": float(s.universal_sl_pct),
        "slippage_pct": float(s.slippage_pct),
        "trigger_mode": s.trigger_mode,
        "combined_trigger_mode": bool(
            getattr(s, "combined_trigger_mode", False)
        ),
        "flat_trigger_pct": float(s.flat_trigger_pct),
        "slab_24h": float(s.slab_24h),
        "slab_12h": float(s.slab_12h),
        "slab_6h": float(s.slab_6h),
        "slab_lt6h": float(s.slab_lt6h),
        "premium_slab_300": float(s.premium_slab_300),
        "premium_slab_200": float(s.premium_slab_200),
        "premium_slab_100": float(s.premium_slab_100),
        "premium_slab_lt100": float(s.premium_slab_lt100),
        "trade_type": getattr(s, "trade_type", None) or "straddle",
        "target_premium_per_side": float(
            getattr(s, "target_premium_per_side", None) or 150.0
        ),
        "strangle_premium_mode": str(
            getattr(s, "strangle_premium_mode", None) or "fixed"
        ).lower(),
        "strangle_premium_pct_of_hedge": float(
            getattr(s, "strangle_premium_pct_of_hedge", None)
            if getattr(s, "strangle_premium_pct_of_hedge", None) is not None
            else 3.0
        ),
        "adj_low_premium_exit_enabled": bool(
            getattr(s, "adj_low_premium_exit_enabled", False)
        ),
        "adj_low_premium_min_usd": float(
            getattr(s, "adj_low_premium_min_usd", None) or 150.0
        ),
        "conversion_mode_enabled": bool(
            getattr(s, "conversion_mode_enabled", True)
        ),
        "max_adjustments_per_basket": (
            int(s.max_adjustments_per_basket)
            if getattr(s, "max_adjustments_per_basket", None) is not None
            else None
        ),
        "premium_cover_loss_enabled": bool(
            getattr(s, "premium_cover_loss_enabled", False)
        ),
        "is_demo": bool(getattr(s, "is_demo", False)),
        "hedge_enabled": bool(getattr(s, "hedge_enabled", False)),
        **_hedge_expiry_fields(s),
        "min_hedge_dte": int(
            getattr(s, "min_hedge_dte", None)
            if getattr(s, "min_hedge_dte", None) is not None
            else 15
        ),
        "min_hedge_dte_enabled": bool(
            getattr(s, "min_hedge_dte_enabled", True)
        ),
        "hedge_roll_enabled": bool(getattr(s, "hedge_roll_enabled", True)),
        "hedge_force_roll_enabled": bool(
            getattr(s, "hedge_force_roll_enabled", True)
        ),
        "hedge_close_at_expiry_enabled": bool(
            getattr(s, "hedge_close_at_expiry_enabled", True)
        ),
        "hedge_target_usd": (
            float(s.hedge_target_usd)
            if getattr(s, "hedge_target_usd", None) is not None
            else None
        ),
        "hedge_stoploss_usd": (
            float(s.hedge_stoploss_usd)
            if getattr(s, "hedge_stoploss_usd", None) is not None
            else None
        ),
        "hedge_fixed_sl_usd": float(
            getattr(s, "hedge_fixed_sl_usd", None)
            if getattr(s, "hedge_fixed_sl_usd", None) is not None
            else 2.0
        ),
        "hedge_sl_floor_pct": float(
            getattr(s, "hedge_sl_floor_pct", None)
            if getattr(s, "hedge_sl_floor_pct", None) is not None
            else 25.0
        ),
        "hedge_roll_dte": int(
            getattr(s, "hedge_roll_dte", None)
            if getattr(s, "hedge_roll_dte", None) is not None
            else 10
        ),
        "hedge_roll_hard_dte": int(
            getattr(s, "hedge_roll_hard_dte", None)
            if getattr(s, "hedge_roll_hard_dte", None) is not None
            else 5
        ),
        "hedge_auto_reopen_after_roll": bool(
            getattr(s, "hedge_auto_reopen_after_roll", True)
        ),
        "hedge_target_multiple": float(
            getattr(s, "hedge_target_multiple", None)
            if getattr(s, "hedge_target_multiple", None) is not None
            else 3.0
        ),
        "hedge_expected_monthly_pct": float(
            getattr(s, "hedge_expected_monthly_pct", None)
            if getattr(s, "hedge_expected_monthly_pct", None) is not None
            else 30.0
        ),
        "hedge_min_hold_days": int(
            getattr(s, "hedge_min_hold_days", None)
            if getattr(s, "hedge_min_hold_days", None) is not None
            else 10
        ),
        "spread_mode": str(getattr(s, "spread_mode", None) or "MANUAL").upper(),
        "basket_exit_spread_pct": float(
            getattr(s, "basket_exit_spread_pct", None)
            if getattr(s, "basket_exit_spread_pct", None) is not None
            else 4.0
        ),
        "hedge_exit_spread_pct": float(
            getattr(s, "hedge_exit_spread_pct", None)
            if getattr(s, "hedge_exit_spread_pct", None) is not None
            else 4.0
        ),
        "spread_cap_pct": float(
            getattr(s, "spread_cap_pct", None)
            if getattr(s, "spread_cap_pct", None) is not None
            else 8.0
        ),
        "margin_buffer_pct": float(
            getattr(s, "margin_buffer_pct", None)
            if getattr(s, "margin_buffer_pct", None) is not None
            else 50.0
        ),
        "strike_selection_mode": str(
            getattr(s, "strike_selection_mode", None) or "fixed_premium"
        ),
        "theta_multiplier": float(
            getattr(s, "theta_multiplier", None)
            if getattr(s, "theta_multiplier", None) is not None
            else 3.0
        ),
        "target_mode": str(getattr(s, "target_mode", None) or "payoff_pct"),
        "target_theta_pct": float(
            getattr(s, "target_theta_pct", None)
            if getattr(s, "target_theta_pct", None) is not None
            else 150.0
        ),
        "basket_target_mode": str(
            getattr(s, "basket_target_mode", None) or "THETA"
        ).upper(),
        "basket_target_multiple": float(
            getattr(s, "basket_target_multiple", None)
            if getattr(s, "basket_target_multiple", None) is not None
            else 1.5
        ),
        "basket_qty_mode": str(
            getattr(s, "basket_qty_mode", None) or "fixed"
        ).lower(),
        "basket_qty_pct_of_hedge": float(
            getattr(s, "basket_qty_pct_of_hedge", None)
            if getattr(s, "basket_qty_pct_of_hedge", None) is not None
            else 20.0
        ),
        "hedge_qty_lots": (
            int(getattr(s, "hedge_qty_lots"))
            if getattr(s, "hedge_qty_lots", None) is not None
            else None
        ),
        "basket_qty_dynamic": bool(
            getattr(s, "basket_qty_dynamic", False)
        ),
        "basket_qty_theta_mult": float(
            getattr(s, "basket_qty_theta_mult", None)
            if getattr(s, "basket_qty_theta_mult", None) is not None
            else 2.0
        ),
        "use_dynamic_qty_on_adjustment": bool(
            getattr(s, "use_dynamic_qty_on_adjustment", False)
        ),
        "adjustment_qty_mode": (
            str(getattr(s, "adjustment_qty_mode", None) or "").lower().strip()
            if str(getattr(s, "adjustment_qty_mode", None) or "").lower().strip()
            in {"unchanged", "increase_dynamic", "decrease_step"}
            else (
                "increase_dynamic"
                if bool(getattr(s, "use_dynamic_qty_on_adjustment", False))
                else "unchanged"
            )
        ),
        "adjustment_qty_decrease_pct": float(
            getattr(s, "adjustment_qty_decrease_pct", None)
            if getattr(s, "adjustment_qty_decrease_pct", None) is not None
            else 25.0
        ),
        "basket_decay_exit_enabled": bool(
            getattr(s, "basket_decay_exit_enabled", False)
        ),
        "basket_decay_exit_pct": float(
            getattr(s, "basket_decay_exit_pct", None)
            if getattr(s, "basket_decay_exit_pct", None) is not None
            else 50.0
        ),
        "basket_decay_exit_mode": str(
            getattr(s, "basket_decay_exit_mode", None) or "both_legs"
        ).lower(),
        "cooldown_after_loss_minutes": int(
            getattr(s, "cooldown_after_loss_minutes", None)
            if getattr(s, "cooldown_after_loss_minutes", None) is not None
            else 120
        ),
        "adjustment_premium_tolerance_pct": float(
            getattr(s, "adjustment_premium_tolerance_pct", None)
            if getattr(s, "adjustment_premium_tolerance_pct", None) is not None
            else 40.0
        ),
        "entry_premium_match_tolerance_pct": float(
            getattr(s, "entry_premium_match_tolerance_pct", None)
            if getattr(s, "entry_premium_match_tolerance_pct", None) is not None
            else 25.0
        ),
        "basket_wings_enabled": bool(
            getattr(s, "basket_wings_enabled", False)
        ),
        "wing_strike_mode": str(
            getattr(s, "wing_strike_mode", None) or "points"
        ).lower(),
        "wing_points_away": float(
            getattr(s, "wing_points_away", None)
            if getattr(s, "wing_points_away", None) is not None
            else 2000.0
        ),
        "wing_delta_min": float(
            getattr(s, "wing_delta_min", None)
            if getattr(s, "wing_delta_min", None) is not None
            else 0.05
        ),
        "wing_delta_max": float(
            getattr(s, "wing_delta_max", None)
            if getattr(s, "wing_delta_max", None) is not None
            else 0.07
        ),
        "wing_pct_of_premium": float(
            getattr(s, "wing_pct_of_premium", None)
            if getattr(s, "wing_pct_of_premium", None) is not None
            else 20.0
        ),
        "midprice_enabled": bool(getattr(s, "midprice_enabled", False)),
        "midprice_chase_max_seconds": int(
            getattr(s, "midprice_chase_max_seconds", None)
            if getattr(s, "midprice_chase_max_seconds", None) is not None
            else 120
        ),
        "midprice_hold_seconds": int(
            getattr(s, "midprice_hold_seconds", None)
            if getattr(s, "midprice_hold_seconds", None) is not None
            else 30
        ),
        "midprice_partner_window_seconds": int(
            getattr(s, "midprice_partner_window_seconds", None)
            if getattr(s, "midprice_partner_window_seconds", None) is not None
            else 5
        ),
        # Placeholder until Step 4 supplies live order margin
        "order_margin_per_lot": None,
        "capital_per_lot": None,
        "last_trade_id": s.last_trade_id,
        "last_exit_time": last_exit.isoformat() if last_exit else None,
        "next_entry_time": next_entry.isoformat() if next_entry else None,
        "next_entry_source": getattr(s, "next_entry_source", None),
        "next_entry_reason": _NEXT_ENTRY_SOURCE_LABELS.get(
            str(getattr(s, "next_entry_source", None) or ""),
            None,
        ),
        "seconds_until_entry": seconds_until_entry,
        "retry_count": int(s.retry_count or 0),
        "last_error": s.last_error,
    }


def _validate_payload(payload: AutoTradeSettingsSchema) -> None:
    underlying = payload.underlying.upper().strip()
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported underlying. Use one of {SUPPORTED_UNDERLYINGS}",
        )
    mode = payload.trigger_mode.lower().strip()
    if mode not in {"flat", "slab", "premium"}:
        raise HTTPException(
            status_code=400,
            detail="trigger_mode must be flat, slab, or premium",
        )


@router.get("/settings")
async def get_auto_trade_settings(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return current auto-trade settings (creates defaults if missing)."""
    settings = get_or_create_auto_settings(db)
    return settings_to_dict(settings)


@router.post("/settings")
async def update_auto_trade_settings(
    payload: AutoTradeSettingsSchema,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update auto-trade parameters. Does not change is_enabled."""
    _validate_payload(payload)
    settings = get_or_create_auto_settings(db)

    settings.underlying = payload.underlying.upper().strip()
    settings.expiry_dte = max(0, min(int(payload.expiry_dte), 90))
    if hasattr(payload, "expiry_date_override"):
        override = payload.expiry_date_override
        if override:
            # Derive DTE from selected calendar date; pin override only for
            # weekly/monthly (DTE > 2). Daily 0/1/2DTE stays relative to NOW.
            from datetime import date as _date

            try:
                selected_date = _date.fromisoformat(str(override).strip()[:10])
                today_ist = get_ist_now().date()
                dte_value = (selected_date - today_ist).days
                dte_value = max(0, min(dte_value, 90))
                settings.expiry_dte = dte_value
                if dte_value <= 2:
                    settings.expiry_date_override = None
                else:
                    settings.expiry_date_override = str(override).strip()[:10]
            except (ValueError, TypeError):
                pass  # keep existing expiry_dte / override
        else:
            settings.expiry_date_override = None
    settings.quantity = int(payload.quantity)
    old_reentry_delay = int(settings.re_entry_delay_minutes or 1)
    new_reentry_delay = int(payload.re_entry_delay_minutes)
    settings.re_entry_delay_minutes = new_reentry_delay
    settings.entry_settling_seconds = int(payload.entry_settling_seconds)
    settings.adjustment_settling_seconds = int(
        payload.adjustment_settling_seconds
    )
    settings.tp_pct = float(payload.tp_pct)
    settings.sl_pct = float(payload.sl_pct)
    settings.universal_sl_pct = float(payload.universal_sl_pct)
    settings.slippage_pct = float(payload.slippage_pct)
    settings.trigger_mode = payload.trigger_mode.lower().strip()
    settings.combined_trigger_mode = bool(payload.combined_trigger_mode)
    settings.flat_trigger_pct = float(payload.flat_trigger_pct)
    settings.slab_24h = float(payload.slab_24h)
    settings.slab_12h = float(payload.slab_12h)
    settings.slab_6h = float(payload.slab_6h)
    settings.slab_lt6h = float(payload.slab_lt6h)
    settings.premium_slab_300 = float(payload.premium_slab_300)
    settings.premium_slab_200 = float(payload.premium_slab_200)
    settings.premium_slab_100 = float(payload.premium_slab_100)
    settings.premium_slab_lt100 = float(payload.premium_slab_lt100)
    settings.trade_type = payload.trade_type.lower().strip()
    settings.target_premium_per_side = float(payload.target_premium_per_side)
    settings.strangle_premium_mode = str(
        payload.strangle_premium_mode or "fixed"
    ).lower().strip()
    if settings.strangle_premium_mode not in {"fixed", "pct_of_hedge"}:
        settings.strangle_premium_mode = "fixed"
    settings.strangle_premium_pct_of_hedge = float(
        payload.strangle_premium_pct_of_hedge
    )
    settings.adj_low_premium_exit_enabled = bool(
        payload.adj_low_premium_exit_enabled
    )
    settings.adj_low_premium_min_usd = float(payload.adj_low_premium_min_usd)
    settings.conversion_mode_enabled = bool(payload.conversion_mode_enabled)
    if settings.conversion_mode_enabled:
        # Limit only applies when conversion is OFF
        settings.max_adjustments_per_basket = None
    else:
        settings.max_adjustments_per_basket = (
            int(payload.max_adjustments_per_basket)
            if payload.max_adjustments_per_basket is not None
            else None
        )
    if payload.premium_cover_loss_enabled is not None:
        settings.premium_cover_loss_enabled = payload.premium_cover_loss_enabled
    if payload.is_demo is not None:
        settings.is_demo = bool(payload.is_demo)

    settings.hedge_enabled = bool(payload.hedge_enabled)

    from backend.core.hedge_theta import (
        ExpiryNotAvailableError,
        HedgeThetaError,
        is_relative_expiry_key,
        migrate_hedge_expiry_mode,
        resolve_hedge_expiry_date,
        resolve_short_expiry_date,
    )

    raw_hedge_mode = str(payload.hedge_expiry_mode).lower().strip()
    migrated_mode, needs_repick = migrate_hedge_expiry_mode(
        raw_hedge_mode,
        expiry_dte=(
            int(payload.hedge_expiry_dte)
            if payload.hedge_expiry_dte is not None
            else None
        ),
    )
    if payload.hedge_enabled and (needs_repick or migrated_mode == "date"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Hedge expiry uses a stale fixed date. Re-pick a labelled "
                "relative expiry (e.g. Month 2, Week 2) before enabling hedge mode."
            ),
        )
    if payload.hedge_enabled and not is_relative_expiry_key(migrated_mode):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid hedge_expiry_mode '{raw_hedge_mode}'. "
                "Choose a labelled expiry from the dropdown."
            ),
        )

    settings.hedge_expiry_mode = migrated_mode
    settings.hedge_expiry_dte = None  # relative keys replace fixed DTE
    settings.min_hedge_dte = max(0, min(60, int(payload.min_hedge_dte)))
    settings.min_hedge_dte_enabled = bool(payload.min_hedge_dte_enabled)
    settings.hedge_roll_enabled = bool(payload.hedge_roll_enabled)
    settings.hedge_force_roll_enabled = bool(payload.hedge_force_roll_enabled)
    settings.hedge_close_at_expiry_enabled = bool(
        payload.hedge_close_at_expiry_enabled
    )

    # Resolve live dates and enforce hedge expiry > short basket expiry
    resolved_hedge_date: str | None = None
    if payload.hedge_enabled or is_relative_expiry_key(migrated_mode):
        try:
            from backend.core.delta_client import DeltaClient
            from backend.core.encryption import decrypt
            from backend.models import Account

            account = (
                db.query(Account)
                .filter(Account.is_active.is_(True))
                .order_by(Account.id.asc())
                .first()
            )
            if account is None:
                if payload.hedge_enabled:
                    raise HTTPException(
                        status_code=401,
                        detail="No account connected. Connect API keys to resolve hedge expiry.",
                    )
                client = None
            else:
                client = DeltaClient(
                    decrypt(account.api_key_encrypted),
                    decrypt(account.api_secret_encrypted),
                )

            if client is not None:
                try:
                    und = settings.underlying.upper().strip()
                    short_exp = await resolve_short_expiry_date(
                        expiry_dte=int(settings.expiry_dte or 1),
                        expiry_date_override=getattr(
                            settings, "expiry_date_override", None
                        ),
                    )
                    hedge_exp = await resolve_hedge_expiry_date(
                        client,
                        und,
                        expiry_mode=migrated_mode,
                        expiry_date_override=None,
                        expiry_dte=None,
                    )
                    if hedge_exp <= short_exp and bool(payload.min_hedge_dte_enabled):
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"Hedge expiry ({hedge_exp.isoformat()}) must be "
                                f"later than the short basket expiry "
                                f"({short_exp.isoformat()}). Pick a farther labelled "
                                "expiry (e.g. Month 2)."
                            ),
                        )
                    resolved_hedge_date = hedge_exp.isoformat()
                finally:
                    await client.close()
        except HTTPException:
            raise
        except ExpiryNotAvailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (HedgeThetaError, Exception) as exc:
            logger.warning("hedge expiry resolve on save failed: %s", exc)
            if payload.hedge_enabled:
                raise HTTPException(
                    status_code=400,
                    detail=f"Could not resolve hedge expiry: {exc}",
                ) from exc

    settings.hedge_expiry_date_override = (
        resolved_hedge_date
        or (
            str(payload.hedge_expiry_date_override).strip()[:10]
            if payload.hedge_expiry_date_override
            else None
        )
    )
    settings.hedge_target_usd = (
        float(payload.hedge_target_usd)
        if payload.hedge_target_usd is not None
        else None
    )
    settings.hedge_stoploss_usd = (
        float(payload.hedge_stoploss_usd)
        if payload.hedge_stoploss_usd is not None
        else None
    )
    settings.hedge_sl_floor_pct = float(payload.hedge_sl_floor_pct)
    settings.hedge_fixed_sl_usd = float(payload.hedge_fixed_sl_usd)
    settings.hedge_roll_dte = int(payload.hedge_roll_dte)
    settings.hedge_roll_hard_dte = int(payload.hedge_roll_hard_dte)
    settings.hedge_auto_reopen_after_roll = bool(
        payload.hedge_auto_reopen_after_roll
    )
    settings.hedge_target_multiple = float(payload.hedge_target_multiple)
    settings.hedge_expected_monthly_pct = float(
        payload.hedge_expected_monthly_pct
    )
    settings.hedge_min_hold_days = int(payload.hedge_min_hold_days)
    settings.spread_mode = str(payload.spread_mode or "MANUAL").upper().strip()
    settings.basket_exit_spread_pct = float(payload.basket_exit_spread_pct)
    settings.hedge_exit_spread_pct = float(payload.hedge_exit_spread_pct)
    settings.spread_cap_pct = float(payload.spread_cap_pct)
    settings.margin_buffer_pct = float(payload.margin_buffer_pct)
    # When hedge is off, force modes that require a live hedge back to defaults
    if settings.hedge_enabled:
        settings.strike_selection_mode = str(
            payload.strike_selection_mode
        ).lower().strip()
        settings.target_mode = str(payload.target_mode).lower().strip()
    else:
        settings.strike_selection_mode = "fixed_premium"
        settings.target_mode = "payoff_pct"
    settings.theta_multiplier = float(payload.theta_multiplier)
    settings.target_theta_pct = float(payload.target_theta_pct)
    settings.basket_target_mode = str(
        payload.basket_target_mode or "THETA"
    ).upper().strip()
    settings.basket_target_multiple = float(payload.basket_target_multiple)
    settings.basket_qty_mode = str(payload.basket_qty_mode or "fixed").lower().strip()
    settings.basket_qty_pct_of_hedge = float(payload.basket_qty_pct_of_hedge)
    settings.hedge_qty_lots = (
        int(payload.hedge_qty_lots)
        if payload.hedge_qty_lots is not None
        else None
    )
    settings.basket_qty_dynamic = bool(payload.basket_qty_dynamic)
    settings.basket_qty_theta_mult = float(payload.basket_qty_theta_mult)
    adj_mode = str(payload.adjustment_qty_mode or "unchanged").lower().strip()
    if adj_mode not in {"unchanged", "increase_dynamic", "decrease_step"}:
        adj_mode = "unchanged"
    # increase_dynamic still requires basket_qty_dynamic
    if adj_mode == "increase_dynamic" and not payload.basket_qty_dynamic:
        adj_mode = "unchanged"
    settings.adjustment_qty_mode = adj_mode
    settings.adjustment_qty_decrease_pct = float(
        payload.adjustment_qty_decrease_pct
    )
    # Keep deprecated bool in sync for rollback / old readers
    settings.use_dynamic_qty_on_adjustment = adj_mode == "increase_dynamic"
    settings.basket_decay_exit_enabled = bool(payload.basket_decay_exit_enabled)
    settings.basket_decay_exit_pct = float(payload.basket_decay_exit_pct)
    decay_mode = str(payload.basket_decay_exit_mode or "both_legs").lower().strip()
    settings.basket_decay_exit_mode = (
        decay_mode if decay_mode in {"both_legs", "combined"} else "both_legs"
    )
    settings.cooldown_after_loss_minutes = int(
        payload.cooldown_after_loss_minutes
    )
    settings.adjustment_premium_tolerance_pct = float(
        payload.adjustment_premium_tolerance_pct
    )
    settings.entry_premium_match_tolerance_pct = float(
        payload.entry_premium_match_tolerance_pct
    )
    settings.basket_wings_enabled = bool(payload.basket_wings_enabled)
    settings.wing_strike_mode = str(payload.wing_strike_mode or "points").lower()
    settings.wing_points_away = float(payload.wing_points_away)
    settings.wing_delta_min = float(payload.wing_delta_min)
    settings.wing_delta_max = float(payload.wing_delta_max)
    settings.wing_pct_of_premium = float(payload.wing_pct_of_premium)
    settings.midprice_enabled = bool(payload.midprice_enabled)
    chase_sec = int(payload.midprice_chase_max_seconds)
    settings.midprice_chase_max_seconds = max(10, min(600, chase_sec))
    hold_sec = int(payload.midprice_hold_seconds)
    settings.midprice_hold_seconds = max(5, min(120, hold_sec))
    partner_win = int(payload.midprice_partner_window_seconds)
    settings.midprice_partner_window_seconds = max(2, min(30, partner_win))

    settings.updated_at = get_utc_now()
    # Do NOT change is_enabled here

    _reschedule_reentry_if_delay_changed(
        settings,
        old_delay=old_reentry_delay,
        new_delay=new_reentry_delay,
    )

    db.commit()
    db.refresh(settings)

    # Propagate combined_trigger_mode to ALL active trades + in-memory tracker.
    # Otherwise trades opened before the toggle stay on individual triggers
    # if settings read ever fails or trade flag is treated as sole source.
    try:
        from backend.config import TradeStatus
        from backend.models import Trade

        combined_val = bool(settings.combined_trigger_mode)
        active_trades = (
            db.query(Trade)
            .filter(Trade.status == TradeStatus.ACTIVE.value)
            .all()
        )
        synced_ids: list[int] = []
        for t in active_trades:
            t.combined_trigger_mode = combined_val
            synced_ids.append(int(t.id))
        if synced_ids:
            db.commit()
            logger.info(
                "[COMBINED_TRIGGER_MODE] Synced to active trades %s → %s",
                synced_ids,
                combined_val,
            )
            try:
                from backend.engine.bot_engine import bot_engine

                for tid in synced_ids:
                    state = bot_engine.position_tracker.get(tid)
                    if state is not None and hasattr(
                        state.trade, "combined_trigger_mode"
                    ):
                        state.trade.combined_trigger_mode = combined_val
            except Exception as sync_exc:
                logger.warning(
                    "[COMBINED_TRIGGER_MODE] In-memory sync failed: %s",
                    sync_exc,
                )
    except Exception as prop_exc:
        logger.warning(
            "[COMBINED_TRIGGER_MODE] Propagate to trades failed: %s",
            prop_exc,
            exc_info=True,
        )

    logger.info(
        "Auto trade settings updated: underlying=%s dte=%s type=%s "
        "combined_trigger_mode=%s",
        settings.underlying,
        settings.expiry_dte,
        settings.trade_type,
        bool(settings.combined_trigger_mode),
    )
    return settings_to_dict(settings)


@router.post("/enable")
async def enable_auto_trade(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enable auto-trade and schedule immediate next entry attempt."""
    settings = get_or_create_auto_settings(db)
    now = get_utc_now()
    settings.is_enabled = True
    settings.next_entry_time = now  # place ASAP if no active trade
    settings.next_entry_source = None
    settings.last_error = None
    settings.updated_at = now
    db.commit()
    db.refresh(settings)

    logger.info(
        "Auto trade ENABLED: %s %sDTE qty=%s",
        settings.underlying,
        settings.expiry_dte,
        settings.quantity,
    )
    return {
        "success": True,
        "message": "Auto trade enabled",
        "settings": settings_to_dict(settings),
    }


@router.post("/disable")
async def disable_auto_trade(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Disable auto-trade and clear pending re-entry.

    Does NOT close any active trade — only stops re-entry.
    """
    settings = get_or_create_auto_settings(db)
    settings.is_enabled = False
    settings.next_entry_time = None
    settings.next_entry_source = None
    settings.updated_at = get_utc_now()
    db.commit()
    db.refresh(settings)

    logger.info("Auto trade DISABLED")
    return {
        "success": True,
        "message": "Auto trade disabled",
        "settings": settings_to_dict(settings),
    }


@router.get("/status")
async def get_auto_trade_status(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Pollable status panel payload (same shape as GET /settings)."""
    settings = get_or_create_auto_settings(db)
    return settings_to_dict(settings)
