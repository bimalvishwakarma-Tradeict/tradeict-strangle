"""RSI pane indicator. Wilder RSI via rsi_div.rsi_wilder (not copied)."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import rsi_wilder
from indicators import register_indicator
from indicators.calc import (
    SOURCE_OPTS,
    STYLE_OPTS,
    color,
    ema,
    num,
    points,
    rgba,
    sma,
    source_arr,
    to_int,
)


def compute_rsi(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    length = max(1, min(500, to_int(params.get("length"), 14)))
    src_key = str(params.get("source") or "close")
    upper = num(params.get("upper"), 70)
    middle = num(params.get("middle"), 50)
    lower = num(params.get("lower"), 30)
    ma_type = str(params.get("ma_type") or "None")
    ma_len = max(1, min(500, to_int(params.get("ma_length"), 14)))
    rsi_col = color(params.get("rsiColor"), "#7E57C2", 0)
    rsi_w = max(1, min(4, to_int(params.get("rsiWidth"), 1)))
    up_col = color(params.get("upperColor"), "#787B86", 0)
    mid_col = color(params.get("midColor"), "#787B86", 0)
    lo_col = color(params.get("lowerColor"), "#787B86", 0)
    up_st = str(params.get("upperStyle") or "dashed")
    mid_st = str(params.get("midStyle") or "dashed")
    lo_st = str(params.get("lowerStyle") or "dashed")
    fill_on = bool(params.get("fillOn")) if params.get("fillOn") is not None else True
    fill_col = color(params.get("fillColor"), "#7E57C2", 85)
    if not bars:
        return {"series": [], "bands": [], "fills": [], "markers": []}

    src = source_arr(bars, src_key)
    rsi = rsi_wilder(src, length)
    times = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    rsi_pts = points(times, rsi)
    series: list[dict[str, Any]] = [
        {
            "key": "rsi",
            "kind": "line",
            "color": rgba(rsi_col, 0),
            "width": rsi_w,
            "style": "solid",
            "lastValueVisible": True,
            "points": rsi_pts,
        },
        {
            "key": "upper",
            "kind": "line",
            "color": rgba(up_col, 0),
            "width": 1,
            "style": up_st,
            "lastValueVisible": False,
            "points": [{"time": int(t), "value": upper} for t in times],
        },
        {
            "key": "middle",
            "kind": "line",
            "color": rgba(mid_col, 0),
            "width": 1,
            "style": mid_st,
            "lastValueVisible": False,
            "points": [{"time": int(t), "value": middle} for t in times],
        },
        {
            "key": "lower",
            "kind": "line",
            "color": rgba(lo_col, 0),
            "width": 1,
            "style": lo_st,
            "lastValueVisible": False,
            "points": [{"time": int(t), "value": lower} for t in times],
        },
    ]
    if ma_type in ("SMA", "EMA"):
        ma = sma(rsi, ma_len) if ma_type == "SMA" else ema(rsi, ma_len)
        series.append(
            {
                "key": "rsi_ma",
                "kind": "line",
                "color": rgba(color(params.get("maColor"), "#FF9800", 0), 0),
                "width": 1,
                "style": "solid",
                "lastValueVisible": False,
                "points": points(times, ma),
            }
        )
    fills = []
    if fill_on:
        fills.append(
            {
                "kind": "hband",
                "top": max(upper, lower),
                "bottom": min(upper, lower),
                "fill": rgba(fill_col),
            }
        )
    return {"series": series, "bands": [], "fills": fills, "markers": [], "pane_min": 0, "pane_max": 100}


@register_indicator("RSI")
class RsiIndicator:
    name = "Relative Strength Index"
    description = "Wilder RSI in a separate pane"
    overlay = False
    inputs = [
        {"key": "length", "label": "Length", "type": "int", "default": 14, "min": 1, "max": 500, "step": 1, "tab": "inputs"},
        {"key": "source", "label": "Source", "type": "select", "default": "close", "options": SOURCE_OPTS, "tab": "inputs"},
        {"key": "upper", "label": "Upper", "type": "float", "default": 70, "min": 0, "max": 100, "step": 1, "tab": "inputs"},
        {"key": "middle", "label": "Middle", "type": "float", "default": 50, "min": 0, "max": 100, "step": 1, "tab": "inputs"},
        {"key": "lower", "label": "Lower", "type": "float", "default": 30, "min": 0, "max": 100, "step": 1, "tab": "inputs"},
        {
            "key": "ma_type", "label": "MA on RSI", "type": "select", "default": "None",
            "options": [{"value": "None", "label": "None"}, {"value": "SMA", "label": "SMA"}, {"value": "EMA", "label": "EMA"}],
            "tab": "inputs",
        },
        {"key": "ma_length", "label": "MA Length", "type": "int", "default": 14, "min": 1, "max": 500, "step": 1, "tab": "inputs"},
        {"key": "rsiColor", "label": "RSI", "type": "color", "default": {"hex": "#7E57C2", "transparency": 0}, "tab": "style"},
        {"key": "rsiWidth", "label": "RSI Width", "type": "int", "default": 1, "min": 1, "max": 4, "step": 1, "tab": "style"},
        {"key": "upperColor", "label": "Upper line", "type": "color", "default": {"hex": "#787B86", "transparency": 0}, "tab": "style"},
        {"key": "upperStyle", "label": "Upper style", "type": "select", "default": "dashed", "options": STYLE_OPTS, "tab": "style"},
        {"key": "midColor", "label": "Middle line", "type": "color", "default": {"hex": "#787B86", "transparency": 0}, "tab": "style"},
        {"key": "midStyle", "label": "Middle style", "type": "select", "default": "dashed", "options": STYLE_OPTS, "tab": "style"},
        {"key": "lowerColor", "label": "Lower line", "type": "color", "default": {"hex": "#787B86", "transparency": 0}, "tab": "style"},
        {"key": "lowerStyle", "label": "Lower style", "type": "select", "default": "dashed", "options": STYLE_OPTS, "tab": "style"},
        {"key": "fillOn", "label": "Fill 30-70", "type": "bool", "default": True, "tab": "style"},
        {"key": "fillColor", "label": "Fill", "type": "color", "default": {"hex": "#7E57C2", "transparency": 85}, "tab": "style"},
        {"key": "maColor", "label": "MA color", "type": "color", "default": {"hex": "#FF9800", "transparency": 0}, "tab": "style"},
    ]

    def short_name(self, params: dict[str, Any]) -> str:
        n = to_int((params or {}).get("length"), 14)
        return f"RSI {n}"

    def compute(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        return compute_rsi(bars, params or {})
