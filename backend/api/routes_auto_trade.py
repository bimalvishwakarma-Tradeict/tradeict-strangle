# routes_auto_trade.py — /api/auto-trade/* settings + enable/disable

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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
    expiry_dte: int = Field(default=1, ge=0, le=30)
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
    settings.expiry_dte = int(payload.expiry_dte)
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
    settings.updated_at = get_ist_now()
    # Do NOT change is_enabled here

    db.commit()
    db.refresh(settings)
    logger.info(
        "Auto trade settings updated: underlying=%s dte=%s",
        settings.underlying,
        settings.expiry_dte,
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
