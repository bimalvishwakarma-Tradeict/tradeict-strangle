# routes_strategy3.py — S003 LSR4 signal engine API (no order placement)

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.core.time_utils import as_utc, get_utc_now
from backend.database import get_db
from backend.models import Strategy3ArmState, Strategy3Signal
from backend.strategies.s003_lsr4.config import (
    RESET_KEYS,
    config_from_row,
    get_or_create_strategy3_config,
    get_or_create_strategy3_engine_state,
    validate_strategy3_config_payload,
)
from backend.strategies.s003_lsr4.worker import (
    get_live_engine,
    run_backfill,
    run_chart,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy3", tags=["strategy3"])


def _utc_iso(dt: Any) -> str | None:
    if dt is None:
        return None
    aware = as_utc(dt)
    return aware.isoformat() if aware is not None else None


class Strategy3ConfigUpdate(BaseModel):
    """Partial update — unknown keys rejected in validate_strategy3_config_payload."""

    model_config = {"extra": "allow"}


@router.get("/config")
async def get_config(db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_or_create_strategy3_config(db)
    cfg = config_from_row(row)
    data = cfg.to_dict()
    data["updated_at"] = _utc_iso(row.updated_at)
    data["created_at"] = _utc_iso(row.created_at)
    return {"success": True, "data": data}


@router.post("/config")
async def update_config(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        updates = validate_strategy3_config_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = get_or_create_strategy3_config(db)
    before = {k: getattr(row, k) for k in RESET_KEYS if hasattr(row, k)}
    for key, val in updates.items():
        if hasattr(row, key):
            setattr(row, key, val)
    row.updated_at = get_utc_now()
    db.commit()
    db.refresh(row)

    after = {k: getattr(row, k) for k in RESET_KEYS if hasattr(row, k)}
    needs_reset = before != after
    return {
        "success": True,
        "data": config_from_row(row).to_dict(),
        "engine_reset_required": needs_reset,
    }


@router.get("/signals")
async def list_signals(
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = (
        db.query(Strategy3Signal)
        .order_by(Strategy3Signal.confirm_candle_time.desc(), Strategy3Signal.id.desc())
        .limit(limit)
        .all()
    )
    data = [
        {
            "id": r.id,
            "created_at": _utc_iso(r.created_at),
            "direction": r.direction,
            "mode": r.mode,
            "score": r.score,
            "signal_candle_time": _utc_iso(r.signal_candle_time),
            "confirm_candle_time": _utc_iso(r.confirm_candle_time),
            "confirm_price": r.confirm_price,
            "signal_extreme": r.signal_extreme,
            "atr_at_arm": r.atr_at_arm,
            "atr_at_confirm": r.atr_at_confirm,
            "adx_at_signal": r.adx_at_signal,
            "acted_on": bool(r.acted_on),
            "notes": r.notes,
        }
        for r in rows
    ]
    return {"success": True, "data": data}


@router.get("/state")
async def get_state(db: Session = Depends(get_db)) -> dict[str, Any]:
    estate = get_or_create_strategy3_engine_state(db)
    arms = db.query(Strategy3ArmState).all()
    live = get_live_engine()
    arm_data = {
        a.side: {
            "armed": bool(a.armed),
            "signal_extreme": a.signal_extreme,
            "confirm_level": a.confirm_level,
            "arm_candle_time": _utc_iso(a.arm_candle_time),
            "bars_elapsed": a.bars_elapsed,
            "mode": a.mode,
            "score": a.score,
            "atr_at_arm": a.atr_at_arm,
            "adx_at_arm": a.adx_at_arm,
            "updated_at": _utc_iso(a.updated_at),
        }
        for a in arms
    }
    return {
        "success": True,
        "data": {
            "arm_state": arm_data,
            "live_arm_snapshot": live.arm_snapshot() if live else None,
            "last_processed_candle_time": _utc_iso(estate.last_processed_candle_time),
            "last_signal_candle_time": _utc_iso(estate.last_signal_candle_time),
            "last_signal_price": estate.last_signal_price,
            "candles_processed": estate.candles_processed,
            "warm": bool(estate.warm),
            "vwap_session_date": estate.vwap_session_date,
            "last_error": estate.last_error,
            "updated_at": _utc_iso(estate.updated_at),
            "engine_health": "live" if live is not None else "idle_or_disabled",
        },
    }


class BackfillRequest(BaseModel):
    """
    candles: how many closed candles to fetch (100..4000).
    Previously this field was named `n` with default 500 — clients sending
    `candles` were silently ignored. `n` is still accepted as an alias.
    """

    candles: int | None = Field(default=None, ge=100, le=4000)
    n: int | None = Field(default=None, ge=100, le=4000)
    ignore_warmup: bool = False


@router.post("/backfill")
async def backfill(body: BackfillRequest | None = None) -> dict[str, Any]:
    if body is None:
        candles = 500
        ignore_warmup = False
    else:
        # Prefer explicit `candles`; fall back to legacy `n`; else default 500
        if body.candles is not None:
            candles = int(body.candles)
        elif body.n is not None:
            candles = int(body.n)
        else:
            candles = 500
        ignore_warmup = bool(body.ignore_warmup)

    if candles < 100 or candles > 4000:
        raise HTTPException(
            status_code=422,
            detail=f"candles must be in [100, 4000], got {candles}",
        )

    try:
        data = await run_backfill(candles, ignore_warmup=ignore_warmup)
    except Exception as exc:
        logger.error("S003 backfill failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"success": True, "data": data}


@router.get("/chart")
async def chart_data(
    candles: int = Query(1500, ge=100, le=4000),
    timeframe: str | None = Query(None),
    overrides: str | None = Query(
        None,
        description="Optional JSON object of Strategy3Config field overrides (preview only)",
    ),
) -> dict[str, Any]:
    """
    Engine's own candles + indicator series + signals + arm events for charting.

    `overrides` never writes strategy3_config and never touches live engine state.
    """
    import json

    overrides_obj: dict[str, Any] | None = None
    if overrides is not None and str(overrides).strip():
        try:
            parsed = json.loads(overrides)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"overrides must be valid JSON: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=422,
                detail="overrides must be a JSON object",
            )
        overrides_obj = parsed

    if timeframe is not None and timeframe not in {"1m", "3m", "5m", "15m"}:
        raise HTTPException(
            status_code=422,
            detail="timeframe must be one of 1m/3m/5m/15m",
        )

    try:
        if overrides_obj is not None:
            # Validate before running so bad keys get 422, not 502
            validate_strategy3_config_payload(overrides_obj)
        data = await run_chart(
            candles,
            timeframe=timeframe,
            overrides=overrides_obj,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("S003 chart failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"success": True, "data": data}
