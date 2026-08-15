# routes_auto_trade.py — /api/auto-trade/* settings + enable/disable

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from backend.config import IST
from backend.core.time_utils import get_ist_now
from backend.database import get_db, get_or_create_auto_settings
from backend.models import AutoTradeSettings
from backend.strategies.s001_short_strangle.config import SUPPORTED_UNDERLYINGS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auto-trade", tags=["auto-trade"])


class AutoTradeSettingsSchema(BaseModel):
    underlying: str = "BTC"
    expiry_dte: int = Field(default=1, ge=0, le=90)
    expiry_date_override: str | None = None
    quantity: int = Field(default=1, ge=1, le=1000)
    re_entry_delay_minutes: int = Field(default=1, ge=0, le=1440)

    # Risk
    tp_pct: float = Field(default=50.0, gt=0, le=500)
    sl_pct: float = Field(default=100.0, gt=0, le=1000)
    universal_sl_pct: float = Field(default=200.0, ge=100, le=1000)
    slippage_pct: float = Field(default=2.0, ge=0, le=10)

    # Trigger
    trigger_mode: str = "slab"
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

    # Low-premium adjustment exit
    adj_low_premium_exit_enabled: bool = False
    adj_low_premium_min_usd: float = Field(default=150.0, ge=10, le=500)
    # Conversion mode + adjustment limit
    conversion_mode_enabled: bool = True
    max_adjustments_per_basket: int | None = Field(default=None, ge=1, le=50)
    premium_cover_loss_enabled: bool | None = None
    is_demo: bool | None = None

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


def _as_ist(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return IST.localize(dt)
    return dt.astimezone(IST)


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
        "tp_pct": float(s.tp_pct),
        "sl_pct": float(s.sl_pct),
        "universal_sl_pct": float(s.universal_sl_pct),
        "slippage_pct": float(s.slippage_pct),
        "trigger_mode": s.trigger_mode,
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
        "last_trade_id": s.last_trade_id,
        "last_exit_time": last_exit.isoformat() if last_exit else None,
        "next_entry_time": next_entry.isoformat() if next_entry else None,
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
    settings.re_entry_delay_minutes = int(payload.re_entry_delay_minutes)
    settings.tp_pct = float(payload.tp_pct)
    settings.sl_pct = float(payload.sl_pct)
    settings.universal_sl_pct = float(payload.universal_sl_pct)
    settings.slippage_pct = float(payload.slippage_pct)
    settings.trigger_mode = payload.trigger_mode.lower().strip()
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
    settings.updated_at = get_ist_now()
    # Do NOT change is_enabled here

    db.commit()
    db.refresh(settings)
    logger.info(
        "Auto trade settings updated: underlying=%s dte=%s type=%s",
        settings.underlying,
        settings.expiry_dte,
        settings.trade_type,
    )
    return settings_to_dict(settings)


@router.post("/enable")
async def enable_auto_trade(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Enable auto-trade and schedule immediate next entry attempt."""
    settings = get_or_create_auto_settings(db)
    now = get_ist_now()
    settings.is_enabled = True
    settings.next_entry_time = now  # place ASAP if no active trade
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
    settings.updated_at = get_ist_now()
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
