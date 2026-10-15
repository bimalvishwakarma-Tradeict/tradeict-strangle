"""ULP — Untouched Liquidity Pools. Pine v6 port (ta.pivothigh / pivotlow)."""

from __future__ import annotations

from typing import Any

from indicators import register_indicator

BTCUSD_MINTICK = 0.5
DUP_TICKS = 10
DUP_EPS = DUP_TICKS * BTCUSD_MINTICK  # 5.0


def _num(v: Any, fb: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(fb)
    return x if x == x else float(fb)


def _int(v: Any, fb: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return int(fb)


def _bool(v: Any, fb: bool) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return bool(fb)


def _color(v: Any, hex_fb: str, tr_fb: int) -> dict[str, Any]:
    if isinstance(v, dict):
        hx = str(v.get("hex") or hex_fb)
        tr = int(_num(v.get("transparency"), tr_fb))
        return {"hex": hx if hx.startswith("#") else "#" + hx, "transparency": max(0, min(100, tr))}
    if isinstance(v, str) and v.startswith("#"):
        return {"hex": v[:7], "transparency": int(tr_fb)}
    return {"hex": hex_fb, "transparency": int(tr_fb)}


def _rgba(
    col: dict[str, Any],
    tr_override: int | None = None,
    alpha_scale: float = 1.0,
) -> str:
    hx = str(col.get("hex") or "#089981").lstrip("#")
    if len(hx) < 6:
        hx = "089981"
    r = int(hx[0:2], 16)
    g = int(hx[2:4], 16)
    b = int(hx[4:6], 16)
    tr = int(tr_override if tr_override is not None else col.get("transparency", 70))
    a = max(0.0, min(1.0, (100 - tr) / 100.0 * float(alpha_scale)))
    return f"rgba({r},{g},{b},{a:.3f})"


def is_pivot_high(highs: list[float], i: int, left: int, right: int) -> bool:
    """Confirmed at bar i; pivot at i-right. Pivot high if >= all L left and L right (window max)."""
    p = i - int(right)
    L = int(left)
    R = int(right)
    if p < L or i >= len(highs):
        return False
    hp = float(highs[p])
    for j in range(1, L + 1):
        if hp < float(highs[p - j]):
            return False
    for j in range(1, R + 1):
        if hp < float(highs[p + j]):
            return False
    return True


def is_pivot_low(lows: list[float], i: int, left: int, right: int) -> bool:
    p = i - int(right)
    L = int(left)
    R = int(right)
    if p < L or i >= len(lows):
        return False
    lp = float(lows[p])
    for j in range(1, L + 1):
        if lp > float(lows[p - j]):
            return False
    for j in range(1, R + 1):
        if lp > float(lows[p + j]):
            return False
    return True


def compute_ulp(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    L = max(2, min(50, _int(params.get("lookback"), 10)))
    max_lv = max(5, min(200, _int(params.get("maxLevels"), 100)))
    full = _bool(params.get("fullRange"), False)
    show_lab = _bool(params.get("showLabels"), True)
    show_exp = _bool(params.get("showExpired"), True)
    exp_pct = max(10, min(100, _int(params.get("expiredOpacityPct"), 40)))
    max_exp = max(0, min(500, _int(params.get("maxExpired"), 100)))
    hi_col = _color(params.get("highColor"), "#089981", 70)
    lo_col = _color(params.get("lowColor"), "#F23645", 70)
    if not bars:
        return {"boxes": [], "labels": [], "lines": [], "markers": [], "series": []}

    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    zones_h: list[dict[str, Any]] = []
    zones_l: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    n_add = 0

    def pop_oldest(zones: list[dict[str, Any]]) -> None:
        if len(zones) > max_lv:
            del zones[0]

    for i, bar in enumerate(bars):
        if i >= L + L:
            if is_pivot_high(highs, i, L, L):
                p = i - L
                pb = bars[p]
                top = float(pb["high"])
                bottom = float(pb["low"]) if full else max(float(pb["open"]), float(pb["close"]))
                if top > bottom:
                    dup = any(abs(float(z["top"]) - top) < DUP_EPS for z in zones_h)
                    if not dup:
                        n_add += 1
                        zones_h.append(
                            {
                                "id": f"h-{int(pb['time'])}-{n_add}",
                                "kind": "high",
                                "t1": int(pb["time"]),
                                "pivot_i": p,
                                "top": top,
                                "bottom": bottom,
                            }
                        )
                        pop_oldest(zones_h)
            if is_pivot_low(lows, i, L, L):
                p = i - L
                pb = bars[p]
                bottom = float(pb["low"])
                top = float(pb["high"]) if full else min(float(pb["open"]), float(pb["close"]))
                if top > bottom:
                    dup = any(abs(float(z["bottom"]) - bottom) < DUP_EPS for z in zones_l)
                    if not dup:
                        n_add += 1
                        zones_l.append(
                            {
                                "id": f"l-{int(pb['time'])}-{n_add}",
                                "kind": "low",
                                "t1": int(pb["time"]),
                                "pivot_i": p,
                                "top": top,
                                "bottom": bottom,
                            }
                        )
                        pop_oldest(zones_l)
        hi = float(bar["high"])
        lo = float(bar["low"])
        if show_exp:
            nxt_h: list[dict[str, Any]] = []
            for z in zones_h:
                if hi < float(z["top"]):
                    nxt_h.append(z)
                else:
                    expired.append({**z, "expired_t": int(bar["time"])})
            zones_h = nxt_h
            nxt_l: list[dict[str, Any]] = []
            for z in zones_l:
                if lo > float(z["bottom"]):
                    nxt_l.append(z)
                else:
                    expired.append({**z, "expired_t": int(bar["time"])})
            zones_l = nxt_l
        else:
            zones_h = [z for z in zones_h if hi < float(z["top"])]
            zones_l = [z for z in zones_l if lo > float(z["bottom"])]

    fill_h = _rgba(hi_col)
    fill_l = _rgba(lo_col)
    border_h = _rgba({**hi_col, "hex": "#089981"}, 60)
    border_l = _rgba({**lo_col, "hex": "#F23645"}, 60)
    boxes: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    last_t = int(bars[-1]["time"])
    for z in zones_h + zones_l:
        hi_z = z["kind"] == "high"
        boxes.append(
            {
                "id": z["id"],
                "t1": int(z["t1"]),
                "t2": last_t,
                "top": float(z["top"]),
                "bottom": float(z["bottom"]),
                "extend_right": True,
                "fill": fill_h if hi_z else fill_l,
                "border": border_h if hi_z else border_l,
                "kind": z["kind"],
            }
        )
        if show_lab:
            price = float(z["top"] if hi_z else z["bottom"])
            labels.append(
                {
                    "t": int(z["t1"]),
                    "price": price,
                    "text": f"{price:.2f}",
                    "color": fill_h if hi_z else fill_l,
                    "side": "right",
                    "kind": z["kind"],
                }
            )
    if show_exp and max_exp > 0:
        scale = float(exp_pct) / 100.0
        fill_h_e = _rgba(hi_col, alpha_scale=scale)
        fill_l_e = _rgba(lo_col, alpha_scale=scale)
        border_h_e = _rgba({**hi_col, "hex": "#089981"}, 60, alpha_scale=scale)
        border_l_e = _rgba({**lo_col, "hex": "#F23645"}, 60, alpha_scale=scale)
        tail = expired[-max_exp:] if len(expired) > max_exp else expired
        for z in tail:
            hi_z = z["kind"] == "high"
            boxes.append(
                {
                    "id": z["id"] + "-exp",
                    "t1": int(z["t1"]),
                    "t2": int(z["expired_t"]),
                    "top": float(z["top"]),
                    "bottom": float(z["bottom"]),
                    "extend_right": False,
                    "fill": fill_h_e if hi_z else fill_l_e,
                    "border": border_h_e if hi_z else border_l_e,
                    "kind": z["kind"],
                    "expired": True,
                }
            )
    return {"boxes": boxes, "labels": labels, "lines": [], "markers": [], "series": []}


@register_indicator("ULP")
class UlpIndicator:
    name = "Untouched Liquidity Pools"
    description = "Swing high/low zones that vanish on first touch"
    overlay = True
    inputs = [
        {
            "key": "lookback", "label": "Lookback", "type": "int", "default": 10,
            "min": 2, "max": 50, "step": 1, "tab": "inputs",
        },
        {
            "key": "maxLevels", "label": "Max Levels", "type": "int", "default": 100,
            "min": 5, "max": 200, "step": 1, "tab": "inputs",
        },
        {
            "key": "fullRange", "label": "Full candle range (High→Low)", "type": "bool",
            "default": False, "tab": "inputs",
        },
        {
            "key": "showLabels", "label": "Show Labels", "type": "bool", "default": True,
            "tab": "inputs",
        },
        {
            "key": "showExpired", "label": "Show expired zones", "type": "bool",
            "default": True, "tab": "inputs",
        },
        {
            "key": "expiredOpacityPct", "label": "Expired opacity % of fresh", "type": "int",
            "default": 40, "min": 10, "max": 100, "step": 1, "tab": "style",
        },
        {
            "key": "maxExpired", "label": "Max expired boxes", "type": "int",
            "default": 100, "min": 0, "max": 500, "step": 1, "tab": "inputs",
        },
        {
            "key": "highColor", "label": "Resistance Zone Color", "type": "color",
            "default": {"hex": "#089981", "transparency": 70}, "tab": "style",
        },
        {
            "key": "lowColor", "label": "Support Zone Color", "type": "color",
            "default": {"hex": "#F23645", "transparency": 70}, "tab": "style",
        },
    ]

    def short_name(self, params: dict[str, Any]) -> str:
        L = _int((params or {}).get("lookback"), 10)
        m = _int((params or {}).get("maxLevels"), 100)
        name = f"ULP {L} {m}"
        if _bool((params or {}).get("fullRange"), False):
            name += " F"
        return name

    def compute(self, bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        return compute_ulp(bars, params or {})
