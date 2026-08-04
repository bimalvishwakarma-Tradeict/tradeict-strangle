# routes_logs.py — Bot activity log API

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from backend.core.bot_logger import get_recent_logs, read_log_file
from backend.core.time_utils import get_ist_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(
    trade_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    level: str = Query(default="all"),
) -> dict[str, Any]:
    """
    Return recent in-memory bot activity logs (newest first).

    level: 'all' | 'important' (adjustments, exits, errors)
    """
    level_arg = None if level.lower() in {"all", ""} else level
    logs = get_recent_logs(trade_id=trade_id, limit=limit, level=level_arg)
    return {
        "success": True,
        "count": len(logs),
        "logs": logs,
    }


@router.get("/file")
async def download_log_file(
    date: str | None = Query(default=None, description="YYYY-MM-DD (IST)"),
) -> PlainTextResponse:
    """Return raw log file content for the given date (default: today IST)."""
    date_str = date or get_ist_now().strftime("%Y-%m-%d")
    try:
        content = read_log_file(date_str)
    except OSError as exc:
        logger.error("Failed reading log file for %s: %s", date_str, exc)
        raise HTTPException(status_code=500, detail="Failed to read log file") from exc

    if not content:
        raise HTTPException(
            status_code=404,
            detail=f"No log file found for date {date_str}",
        )
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="bot_activity_{date_str}.log"'
        },
    )
