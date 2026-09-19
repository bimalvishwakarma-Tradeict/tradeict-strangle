# routes_strategies.py — read-only registry for Strategies UI
# Does NOT touch live trading path / DB / order placement.

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

_ROOT = Path(__file__).resolve().parent.parent.parent
_REGISTRY = _ROOT / "backtest" / "results" / "registry.json"
_LEARNINGS = _ROOT / "backtest" / "LEARNINGS.md"


def _load_registry() -> dict[str, Any]:
    if not _REGISTRY.exists():
        # Best-effort rebuild if harness is importable
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        try:
            from backtest.harness.registry import write_registry

            write_registry()
        except Exception as e:
            logger.error("registry missing and rebuild failed: %s", e)
            raise HTTPException(
                status_code=404,
                detail="registry.json not found — run harness registry builder",
            ) from e
    try:
        return json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error("registry read failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to read registry") from e


@router.get("/registry")
async def get_strategies_registry() -> dict[str, Any]:
    """Return backtest/results/registry.json (read-only)."""
    data = _load_registry()
    learnings = list(data.get("learnings") or [])
    if _LEARNINGS.exists() and not learnings:
        # fallback: first non-heading lines from LEARNINGS.md
        for line in _LEARNINGS.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("- "):
                learnings.append(s[2:])
    return {
        "success": True,
        "data": {
            "generated_utc": data.get("generated_utc"),
            "strategies": data.get("strategies") or [],
            "learnings": learnings,
        },
    }
