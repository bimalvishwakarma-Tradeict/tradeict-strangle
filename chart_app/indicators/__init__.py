"""Chart indicator registry — separate from strategy overlay plugins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ComputeFn = Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]

INDICATORS: dict[str, dict[str, Any]] = {}


def register_indicator(ind_id: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        inst = cls()
        INDICATORS[str(ind_id)] = {
            "id": str(ind_id),
            "name": str(getattr(inst, "name", ind_id)),
            "description": str(getattr(inst, "description", "")),
            "overlay": bool(getattr(inst, "overlay", True)),
            "inputs": list(getattr(inst, "inputs", [])),
            "compute": inst.compute,
            "short_name": getattr(inst, "short_name", None),
            "instance": inst,
        }
        return cls

    return deco


def list_indicators() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in INDICATORS.values():
        out.append(
            {
                "id": rec["id"],
                "name": rec["name"],
                "description": rec["description"],
                "overlay": rec["overlay"],
                "inputs": rec["inputs"],
                "short_name": short_name_for(rec["id"], {}),
            }
        )
    return out


def short_name_for(ind_id: str, params: dict[str, Any]) -> str:
    rec = INDICATORS.get(str(ind_id))
    if rec is None:
        return str(ind_id)
    fn = rec.get("short_name")
    if callable(fn):
        return str(fn(params or {}))
    return str(rec["name"])


def compute_indicator(
    ind_id: str, bars: list[dict[str, Any]], params: dict[str, Any]
) -> dict[str, Any]:
    rec = INDICATORS.get(str(ind_id))
    if rec is None:
        raise KeyError(f"unknown indicator {ind_id}")
    return rec["compute"](bars, params or {})


def load_indicators() -> None:
    from indicators import bollinger  # noqa: F401
    from indicators import ema  # noqa: F401
    from indicators import rsi  # noqa: F401
    from indicators import supertrend  # noqa: F401
    from indicators import ulp  # noqa: F401
