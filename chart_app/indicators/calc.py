"""Shared indicator math (TradingView-compatible SMA/EMA/RMA/stdev/source)."""

from __future__ import annotations

from typing import Any

import numpy as np

SOURCE_OPTS = [
    {"value": "close", "label": "close"},
    {"value": "open", "label": "open"},
    {"value": "high", "label": "high"},
    {"value": "low", "label": "low"},
    {"value": "hl2", "label": "hl2"},
    {"value": "hlc3", "label": "hlc3"},
    {"value": "ohlc4", "label": "ohlc4"},
]

STYLE_OPTS = [
    {"value": "solid", "label": "solid"},
    {"value": "dashed", "label": "dashed"},
    {"value": "dotted", "label": "dotted"},
]


def num(v: Any, fb: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(fb)
    return x if x == x else float(fb)


def to_int(v: Any, fb: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return int(fb)


def to_bool(v: Any, fb: bool) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return bool(fb)


def color(v: Any, hex_fb: str, tr_fb: int = 0) -> dict[str, Any]:
    if isinstance(v, dict):
        hx = str(v.get("hex") or hex_fb)
        tr = int(num(v.get("transparency"), tr_fb))
        if not hx.startswith("#"):
            hx = "#" + hx
        return {"hex": hx[:7], "transparency": max(0, min(100, tr))}
    if isinstance(v, str) and v.startswith("#"):
        return {"hex": v[:7], "transparency": int(tr_fb)}
    return {"hex": hex_fb, "transparency": int(tr_fb)}


def rgba(col: dict[str, Any], tr_override: int | None = None) -> str:
    hx = str(col.get("hex") or "#2962ff").lstrip("#")
    if len(hx) < 6:
        hx = "2962ff"
    r = int(hx[0:2], 16)
    g = int(hx[2:4], 16)
    b = int(hx[4:6], 16)
    tr = int(tr_override if tr_override is not None else col.get("transparency", 0))
    a = max(0.0, min(1.0, (100 - tr) / 100.0))
    return f"rgba({r},{g},{b},{a:.3f})"


def source_arr(bars: list[dict[str, Any]], src: str) -> np.ndarray:
    key = str(src or "close").lower()
    o = np.array([float(b["open"]) for b in bars], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    if key == "open":
        return o
    if key == "high":
        return h
    if key == "low":
        return l
    if key == "hl2":
        return (h + l) / 2.0
    if key == "hlc3":
        return (h + l + c) / 3.0
    if key == "ohlc4":
        return (o + h + l + c) / 4.0
    return c


def sma(src: np.ndarray, length: int) -> np.ndarray:
    n = int(length)
    out = np.full(src.shape[0], np.nan, dtype=np.float64)
    if n < 1 or src.size < n:
        return out
    csum = np.cumsum(src, dtype=np.float64)
    out[n - 1] = csum[n - 1] / n
    for i in range(n, src.size):
        out[i] = (csum[i] - csum[i - n]) / n
    return out


def ema(src: np.ndarray, length: int) -> np.ndarray:
    """TradingView ta.ema: alpha=2/(len+1), seed = SMA(len)."""
    n = int(length)
    out = np.full(src.shape[0], np.nan, dtype=np.float64)
    if n < 1 or src.size < n:
        return out
    alpha = 2.0 / (n + 1.0)
    seed = float(np.mean(src[:n]))
    out[n - 1] = seed
    prev = seed
    for i in range(n, src.size):
        prev = alpha * float(src[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def rma(src: np.ndarray, length: int) -> np.ndarray:
    """TradingView ta.rma (Wilder): alpha=1/len, seed = SMA(len)."""
    n = int(length)
    out = np.full(src.shape[0], np.nan, dtype=np.float64)
    if n < 1 or src.size < n:
        return out
    alpha = 1.0 / n
    seed = float(np.mean(src[:n]))
    out[n - 1] = seed
    prev = seed
    for i in range(n, src.size):
        prev = alpha * float(src[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def stdev_pop(src: np.ndarray, length: int) -> np.ndarray:
    """TradingView ta.stdev: population stdev (divide by n, not n-1)."""
    n = int(length)
    out = np.full(src.shape[0], np.nan, dtype=np.float64)
    if n < 1 or src.size < n:
        return out
    for i in range(n - 1, src.size):
        w = src[i - n + 1 : i + 1]
        mean = float(np.mean(w))
        var = float(np.mean((w - mean) ** 2))
        out[i] = float(np.sqrt(var)) if var > 0.0 else 0.0
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = high.shape[0]
    tr = np.empty(n, dtype=np.float64)
    tr[0] = float(high[0] - low[0])
    for i in range(1, n):
        hl = float(high[i] - low[i])
        hc = abs(float(high[i] - close[i - 1]))
        lc = abs(float(low[i] - close[i - 1]))
        tr[i] = max(hl, hc, lc)
    return tr


def bar_sec(bars: list[dict[str, Any]]) -> int:
    if len(bars) < 2:
        return 60
    d = int(bars[-1]["time"]) - int(bars[-2]["time"])
    return d if d > 0 else 60


def points(
    times: np.ndarray, values: np.ndarray, offset: int = 0, sec: int = 60, to_ts: int | None = None
) -> list[dict[str, float]]:
    off = int(offset)
    out: list[dict[str, float]] = []
    for i in range(times.shape[0]):
        v = float(values[i])
        if v != v:
            continue
        t = int(times[i]) + off * int(sec)
        if to_ts is not None and t > int(to_ts):
            continue
        out.append({"time": t, "value": v})
    return out
