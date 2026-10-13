"""Supertrend overlay — TradingView ta.supertrend (RMA ATR, carry-forward bands)."""

from __future__ import annotations

from typing import Any

import numpy as np

from indicators import register_indicator
from indicators.calc import color, num, rgba, rma, to_bool, to_int, true_range


def compute_supertrend(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    atr_len = max(1, min(500, to_int(params.get("atr_length"), 10)))
    factor = num(params.get("factor"), 3.0)
    up_c = color(params.get("upColor"), "#089981", 0)
    dn_c = color(params.get("downColor"), "#F23645", 0)
    width = max(1, min(4, to_int(params.get("lineWidth"), 2)))
    show_lab = to_bool(params.get("showLabels"), True)
    show_fill = to_bool(params.get("showFill"), True)
    fill_up = color(params.get("fillUpColor"), "#089981", 85)
    fill_dn = color(params.get("fillDownColor"), "#F23645", 85)
    if not bars:
        return {"series": [], "bands": [], "fills": [], "markers": [], "st_fills": []}

    high = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    low = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    close = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    times = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    hl2 = (high + low) / 2.0
    atr = rma(true_range(high, low, close), atr_len)
    n = close.shape[0]
    basic_up = hl2 - factor * atr
    basic_dn = hl2 + factor * atr
    final_up = np.full(n, np.nan)
    final_dn = np.full(n, np.nan)
    trend = np.ones(n, dtype=np.int8)
    st = np.full(n, np.nan)

    for i in range(n):
        if atr[i] != atr[i]:
            continue
        bu = float(basic_up[i])
        bd = float(basic_dn[i])
        if i == 0 or final_up[i - 1] != final_up[i - 1]:
            final_up[i] = bu
            final_dn[i] = bd
            trend[i] = 1
            st[i] = bu
            continue
        prev_up = float(final_up[i - 1])
        prev_dn = float(final_dn[i - 1])
        prev_c = float(close[i - 1])
        final_up[i] = max(bu, prev_up) if prev_c > prev_up else bu
        final_dn[i] = min(bd, prev_dn) if prev_c < prev_dn else bd
        trn = int(trend[i - 1])
        if trn == -1 and float(close[i]) > prev_dn:
            trn = 1
        elif trn == 1 and float(close[i]) < prev_up:
            trn = -1
        trend[i] = trn
        st[i] = float(final_up[i] if trn == 1 else final_dn[i])

    up_pts: list[dict[str, Any]] = []
    dn_pts: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    st_fills: list[dict[str, Any]] = []
    last_up = True
    for i in range(n):
        if st[i] != st[i]:
            continue
        t = int(times[i])
        val = float(st[i])
        is_up = int(trend[i]) == 1
        last_up = is_up
        if is_up:
            up_pts.append({"time": t, "value": val})
            dn_pts.append({"time": t})
        else:
            dn_pts.append({"time": t, "value": val})
            up_pts.append({"time": t})
        if show_fill:
            st_fills.append(
                {
                    "time": t,
                    "close": float(close[i]),
                    "line": val,
                    "up": is_up,
                }
            )
        if show_lab and i > 0 and st[i - 1] == st[i - 1] and int(trend[i]) != int(trend[i - 1]):
            markers.append(
                {
                    "time": t,
                    "position": "belowBar" if is_up else "aboveBar",
                    "color": rgba(up_c, 0) if is_up else rgba(dn_c, 0),
                    "shape": "arrowUp" if is_up else "arrowDown",
                    "text": "Buy" if is_up else "Sell",
                }
            )

    series = [
        {
            "key": "st_up",
            "kind": "line",
            "color": rgba(up_c, 0),
            "width": width,
            "style": "solid",
            "lastValueVisible": last_up,
            "points": up_pts,
        },
        {
            "key": "st_dn",
            "kind": "line",
            "color": rgba(dn_c, 0),
            "width": width,
            "style": "solid",
            "lastValueVisible": not last_up,
            "points": dn_pts,
            "markers": markers,
        },
    ]
    return {
        "series": series,
        "bands": [],
        "fills": [],
        "markers": markers,
        "st_fills": st_fills if show_fill else [],
        "st_fill_up": rgba(fill_up),
        "st_fill_dn": rgba(fill_dn),
    }


@register_indicator("ST")
class SupertrendIndicator:
    name = "Supertrend"
    description = "ATR Supertrend (active side only)"
    overlay = True
    inputs = [
        {"key": "atr_length", "label": "ATR Length", "type": "int", "default": 10, "min": 1, "max": 100, "step": 1, "tab": "inputs"},
        {"key": "factor", "label": "Factor", "type": "float", "default": 3.0, "min": 0.1, "max": 20, "step": 0.1, "tab": "inputs"},
        {"key": "upColor", "label": "Up trend", "type": "color", "default": {"hex": "#089981", "transparency": 0}, "tab": "style"},
        {"key": "downColor", "label": "Down trend", "type": "color", "default": {"hex": "#F23645", "transparency": 0}, "tab": "style"},
        {"key": "lineWidth", "label": "Width", "type": "int", "default": 2, "min": 1, "max": 4, "step": 1, "tab": "style"},
        {"key": "showLabels", "label": "Buy/Sell labels", "type": "bool", "default": True, "tab": "style"},
        {"key": "showFill", "label": "Highlight fill", "type": "bool", "default": True, "tab": "style"},
        {"key": "fillUpColor", "label": "Up fill", "type": "color", "default": {"hex": "#089981", "transparency": 85}, "tab": "style"},
        {"key": "fillDownColor", "label": "Down fill", "type": "color", "default": {"hex": "#F23645", "transparency": 85}, "tab": "style"},
    ]

    def short_name(self, params: dict[str, Any]) -> str:
        a = to_int((params or {}).get("atr_length"), 10)
        f = num((params or {}).get("factor"), 3.0)
        return f"ST {a} {f:g}"

    def compute(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        return compute_supertrend(bars, params or {})
