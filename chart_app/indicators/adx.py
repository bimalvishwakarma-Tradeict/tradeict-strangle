"""ADX pane. Pine v4 ADX/DI via adx_pine_v4 (not copied)."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4
from indicators import register_indicator
from indicators.calc import STYLE_OPTS, color, num, points, rgba, to_bool, to_int


def compute_adx(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    length = max(2, min(100, to_int(params.get("length"), 14)))
    th = num(params.get("th"), 20.0)
    show_di = to_bool(params.get("show_di"), True)
    dip_col = color(params.get("diPlusColor"), "#4CAF50", 0)
    dim_col = color(params.get("diMinusColor"), "#F44336", 0)
    adx_col = color(params.get("adxColor"), "#1E3A8A", 0)
    th_col = color(params.get("thColor"), "#787B86", 0)
    th_st = str(params.get("thStyle") or "dashed")
    if not bars:
        return {"series": [], "bands": [], "fills": [], "markers": []}

    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    times = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    di_p, di_m, adx = adx_pine_v4(h, l, c, length=length, warmup=500)

    series: list[dict[str, Any]] = []
    if show_di:
        series.append(
            {
                "key": "di_plus",
                "kind": "line",
                "color": rgba(dip_col, 0),
                "width": 1,
                "style": "solid",
                "lastValueVisible": True,
                "points": points(times, di_p),
            }
        )
        series.append(
            {
                "key": "di_minus",
                "kind": "line",
                "color": rgba(dim_col, 0),
                "width": 1,
                "style": "solid",
                "lastValueVisible": True,
                "points": points(times, di_m),
            }
        )
    series.append(
        {
            "key": "adx",
            "kind": "line",
            "color": rgba(adx_col, 0),
            "width": 2,
            "style": "solid",
            "lastValueVisible": True,
            "points": points(times, adx),
        }
    )
    series.append(
        {
            "key": "th",
            "kind": "line",
            "color": rgba(th_col, 0),
            "width": 1,
            "style": th_st,
            "lastValueVisible": False,
            "points": [{"time": int(t), "value": float(th)} for t in times],
        }
    )
    return {"series": series, "bands": [], "fills": [], "markers": []}


@register_indicator("ADX")
class AdxIndicator:
    name = "ADX and DI (Pine v4)"
    description = "TradingView Pine v4 ADX and DI (Wilder sum-smoothing)"
    overlay = False
    inputs = [
        {
            "key": "length", "label": "Length", "type": "int", "default": 14,
            "min": 2, "max": 100, "step": 1, "tab": "inputs",
        },
        {
            "key": "th", "label": "ADX threshold", "type": "float", "default": 20,
            "min": 0, "max": 100, "step": 1, "tab": "inputs",
        },
        {"key": "show_di", "label": "Show DI+/DI-", "type": "bool", "default": True, "tab": "inputs"},
        {
            "key": "diPlusColor", "label": "DI+", "type": "color",
            "default": {"hex": "#4CAF50", "transparency": 0}, "tab": "style",
        },
        {
            "key": "diMinusColor", "label": "DI-", "type": "color",
            "default": {"hex": "#F44336", "transparency": 0}, "tab": "style",
        },
        {
            "key": "adxColor", "label": "ADX", "type": "color",
            "default": {"hex": "#1E3A8A", "transparency": 0}, "tab": "style",
        },
        {
            "key": "thColor", "label": "Threshold", "type": "color",
            "default": {"hex": "#787B86", "transparency": 0}, "tab": "style",
        },
        {
            "key": "thStyle", "label": "Threshold style", "type": "select",
            "default": "dashed", "options": STYLE_OPTS, "tab": "style",
        },
    ]

    def short_name(self, params: dict[str, Any]) -> str:
        n = to_int((params or {}).get("length"), 14)
        th = num((params or {}).get("th"), 20.0)
        if abs(th - round(th)) < 1e-9:
            return f"ADX {n} {int(round(th))}"
        return f"ADX {n} {th:g}"

    def compute(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        return compute_adx(bars, params or {})
