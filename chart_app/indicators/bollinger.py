"""Bollinger Bands overlay — TradingView ta.bb (population stdev)."""

from __future__ import annotations

from typing import Any

import numpy as np

from indicators import register_indicator
from indicators.calc import (
    SOURCE_OPTS,
    bar_sec,
    color,
    ema,
    num,
    points,
    rgba,
    sma,
    source_arr,
    stdev_pop,
    to_int,
)


def compute_bb(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    length = max(1, min(5000, to_int(params.get("length"), 20)))
    ma_type = str(params.get("ma_type") or "SMA")
    src_key = str(params.get("source") or "close")
    mult = num(params.get("mult"), 2.0)
    offset = to_int(params.get("offset"), 0)
    basis_c = color(params.get("basisColor"), "#FF6D00", 0)
    up_c = color(params.get("upperColor"), "#2196F3", 0)
    lo_c = color(params.get("lowerColor"), "#2196F3", 0)
    fill_on = bool(params.get("fillOn")) if params.get("fillOn") is not None else True
    fill_c = color(params.get("fillColor"), "#2196F3", 90)
    if not bars:
        return {"series": [], "bands": [], "fills": [], "markers": []}
    src = source_arr(bars, src_key)
    basis = ema(src, length) if ma_type == "EMA" else sma(src, length)
    sd = stdev_pop(src, length)
    upper = basis + mult * sd
    lower = basis - mult * sd
    times = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    sec = bar_sec(bars)
    to_ts = int(bars[-1]["time"])
    up_pts = points(times, upper, offset=offset, sec=sec, to_ts=to_ts)
    mid_pts = points(times, basis, offset=offset, sec=sec, to_ts=to_ts)
    lo_pts = points(times, lower, offset=offset, sec=sec, to_ts=to_ts)
    series = [
        {"key": "basis", "kind": "line", "color": rgba(basis_c, 0), "width": 1, "style": "solid", "lastValueVisible": True, "points": mid_pts},
        {"key": "upper", "kind": "line", "color": rgba(up_c, 0), "width": 1, "style": "solid", "lastValueVisible": False, "points": up_pts},
        {"key": "lower", "kind": "line", "color": rgba(lo_c, 0), "width": 1, "style": "solid", "lastValueVisible": False, "points": lo_pts},
    ]
    bands = []
    if fill_on:
        bands.append({"upper": up_pts, "lower": lo_pts, "fill": rgba(fill_c)})
    return {"series": series, "bands": bands, "fills": [], "markers": []}


@register_indicator("BB")
class BollingerIndicator:
    name = "Bollinger Bands"
    description = "Basis +/- StdDev (population) overlay"
    overlay = True
    inputs = [
        {"key": "length", "label": "Length", "type": "int", "default": 20, "min": 1, "max": 5000, "step": 1, "tab": "inputs"},
        {
            "key": "ma_type", "label": "Basis MA", "type": "select", "default": "SMA",
            "options": [{"value": "SMA", "label": "SMA"}, {"value": "EMA", "label": "EMA"}],
            "tab": "inputs",
        },
        {"key": "source", "label": "Source", "type": "select", "default": "close", "options": SOURCE_OPTS, "tab": "inputs"},
        {"key": "mult", "label": "StdDev", "type": "float", "default": 2.0, "min": 0.1, "max": 10, "step": 0.1, "tab": "inputs"},
        {"key": "offset", "label": "Offset", "type": "int", "default": 0, "min": -500, "max": 500, "step": 1, "tab": "inputs"},
        {"key": "basisColor", "label": "Basis", "type": "color", "default": {"hex": "#FF6D00", "transparency": 0}, "tab": "style"},
        {"key": "upperColor", "label": "Upper", "type": "color", "default": {"hex": "#2196F3", "transparency": 0}, "tab": "style"},
        {"key": "lowerColor", "label": "Lower", "type": "color", "default": {"hex": "#2196F3", "transparency": 0}, "tab": "style"},
        {"key": "fillOn", "label": "Background fill", "type": "bool", "default": True, "tab": "style"},
        {"key": "fillColor", "label": "Fill", "type": "color", "default": {"hex": "#2196F3", "transparency": 90}, "tab": "style"},
    ]

    def short_name(self, params: dict[str, Any]) -> str:
        n = to_int((params or {}).get("length"), 20)
        m = num((params or {}).get("mult"), 2.0)
        return f"BB {n} {m:g}"

    def compute(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        return compute_bb(bars, params or {})
