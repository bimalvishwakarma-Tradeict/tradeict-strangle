"""Overlay plugin registry. Each plugin: compute(bars, params) -> {lines, markers, series}."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ComputeFn = Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]

PLUGINS: dict[str, ComputeFn] = {}
STRATEGY_META: dict[str, dict[str, Any]] = {}


def register(name: str) -> Callable[[ComputeFn], ComputeFn]:
    def deco(fn: ComputeFn) -> ComputeFn:
        PLUGINS[name] = fn
        return fn

    return deco


def register_meta(ind_id: str, meta: dict[str, Any]) -> None:
    STRATEGY_META[str(ind_id)] = dict(meta)
    STRATEGY_META[str(ind_id)]["id"] = str(ind_id)


def list_strategies() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sid, meta in STRATEGY_META.items():
        out.append(
            {
                "id": sid,
                "name": str(meta.get("name") or sid),
                "description": str(meta.get("description") or ""),
                "short_name": str(meta.get("short_name") or sid),
                "overlay": bool(meta.get("overlay", True)),
                "inputs": list(meta.get("inputs") or []),
            }
        )
    return out


def compute(name: str, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    fn = PLUGINS.get(name)
    if fn is None:
        raise KeyError(f"unknown strategy plugin: {name}")
    return fn(bars, params)


def load_plugins() -> None:
    from strategies import s020_rsi  # noqa: F401
    from strategies import s020_vwap  # noqa: F401
    from strategies import s022f  # noqa: F401

    s020_vwap.register_plugin()
    s020_rsi.register_plugin()
    s022f.register_plugin()
