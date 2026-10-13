"""EMA overlay — TradingView ta.ema."""

from __future__ import annotations

from typing import Any

import numpy as np

from indicators import register_indicator
from indicators.calc import (
    SOURCE_OPTS,
    bar_sec,
    color,
    ema,
    points,
    rgba,
    source_arr,
    to_int,
)


def compute_ema(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    length = max(1, min(5000, to_int(params.get("length"), 9)))
    src_key = str(params.get("source") or "close")
    offset = to_int(params.get("offset"), 0)
    col = color(params.get("lineColor"), "#2962FF", 0)
    width = max(1, min(4, to_int(params.get("lineWidth"), 1)))
    if not bars:
        return {"series": [], "bands": [], "fills": [], "markers": []}
    src = source_arr(bars, src_key)
    vals = ema(src, length)
    times = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    to_ts = int(bars[-1]["time"])
    pts = points(times, vals, offset=offset, sec=bar_sec(bars), to_ts=to_ts)
    return {
        "series": [
            {
                "key": "ema",
                "kind": "line",
                "color": rgba(col, 0),
                "width": width,
                "style": "solid",
                "lastValueVisible": True,
                "points": pts,
            }
        ],
        "bands": [],
        "fills": [],
        "markers": [],
    }


@register_indicator("EMA")
class EmaIndicator:
    name = "Moving Average Exponential"
    description = "EMA overlay on price"
    overlay = True
    inputs = [
        {"key": "length", "label": "Length", "type": "int", "default": 9, "min": 1, "max": 5000, "step": 1, "tab": "inputs"},
        {"key": "source", "label": "Source", "type": "select", "default": "close", "options": SOURCE_OPTS, "tab": "inputs"},
        {"key": "offset", "label": "Offset", "type": "int", "default": 0, "min": -500, "max": 500, "step": 1, "tab": "inputs"},
        {"key": "lineColor", "label": "Color", "type": "color", "default": {"hex": "#2962FF", "transparency": 0}, "tab": "style"},
        {"key": "lineWidth", "label": "Width", "type": "int", "default": 1, "min": 1, "max": 4, "step": 1, "tab": "style"},
    ]

    def short_name(self, params: dict[str, Any]) -> str:
        n = to_int((params or {}).get("length"), 9)
        return f"EMA {n}"

    def compute(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        return compute_ema(bars, params or {})
