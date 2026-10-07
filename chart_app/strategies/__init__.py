"""Overlay plugin registry. Each plugin: compute(bars, params) -> {lines, markers, series}."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ComputeFn = Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]

PLUGINS: dict[str, ComputeFn] = {}


def register(name: str) -> Callable[[ComputeFn], ComputeFn]:
    def deco(fn: ComputeFn) -> ComputeFn:
        PLUGINS[name] = fn
        return fn

    return deco


def compute(name: str, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    fn = PLUGINS.get(name)
    if fn is None:
        raise KeyError(f"unknown strategy plugin: {name}")
    return fn(bars, params)


def load_plugins() -> None:
    from strategies import s020_vwap  # noqa: F401

    s020_vwap.register_plugin()
