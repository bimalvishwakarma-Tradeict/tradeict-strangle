"""S023 OBPS: RSI(3) 80/20 divergence inside a fresh ULP zone (age >= 10)."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div
from backtest.strategies.s022_ulp_long_strangle.signals import (
    DUP_EPS,
    LOOKBACK,
    MAX_LEVELS,
    compute_ulp,
    is_full_mode,
    is_pivot_high,
    is_pivot_low,
    ist_str,
    tf_seconds,
    ulp_params,
)

RSI_LEN = 3
RSI_OB = 80.0
RSI_OS = 20.0
RSI_EXP_OBH = 50.0
RSI_EXP_OBL = 50.0
MIN_ZONE_AGE = 10
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
ARMS = ("ANY", "MATCH")


def range_overlap(lo: float, hi: float, bot: float, top: float) -> bool:
    return float(hi) >= float(bot) and float(lo) <= float(top)


def _time_index(bars: list[dict[str, Any]]) -> dict[int, int]:
    return {int(b["time"]): i for i, b in enumerate(bars)}


def fresh_boxes(bars_prefix: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    """Live (untouched) ULP boxes at the last bar of prefix. Chart compute_ulp."""
    if not bars_prefix:
        return []
    out = compute_ulp(bars_prefix, ulp_params(mode))
    boxes = list(out.get("boxes") or [])
    return [b for b in boxes if not b.get("expired")]


def zone_age_bars(z: dict[str, Any], j: int, t_index: dict[int, int]) -> int | None:
    p = t_index.get(int(z["t1"]))
    if p is None:
        return None
    return int(j) - int(p)


def live_zones_by_bar(bars: list[dict[str, Any]], mode: str) -> list[list[dict[str, Any]]]:
    """Per closed bar: live ULP boxes after that bar's add + expire (imported pivot math)."""
    L = LOOKBACK
    max_lv = MAX_LEVELS
    full = is_full_mode(mode)
    n = len(bars)
    snaps: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    if not bars:
        return snaps
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    zones_h: list[dict[str, Any]] = []
    zones_l: list[dict[str, Any]] = []
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
                                "top": top,
                                "bottom": bottom,
                            }
                        )
                        pop_oldest(zones_l)
        hi = float(bar["high"])
        lo = float(bar["low"])
        zones_h = [z for z in zones_h if hi < float(z["top"])]
        zones_l = [z for z in zones_l if lo > float(z["bottom"])]
        snaps[i] = list(zones_h) + list(zones_l)
    return snaps


def overlapping_fresh_zones(
    bar: dict[str, Any],
    boxes: list[dict[str, Any]],
    j: int,
    t_index: dict[int, int],
    min_age: int = MIN_ZONE_AGE,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    lo = float(bar["low"])
    hi = float(bar["high"])
    for z in boxes:
        age = zone_age_bars(z, j, t_index)
        if age is None or age < int(min_age):
            continue
        if range_overlap(lo, hi, float(z["bottom"]), float(z["top"])):
            hits.append({**z, "age_bars": int(age)})
    return hits


def arm_allows(arm: str, side: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a = str(arm).strip().upper()
    if a == "ANY":
        return hits
    if a != "MATCH":
        raise ValueError(f"unsupported arm {arm!r}")
    want = "high" if str(side).lower() == "short" else "low"
    return [z for z in hits if str(z.get("kind")) == want]


def iter_obps_signals(
    bars: list[dict[str, Any]],
    tf: str,
    mode: str,
    arm: str,
) -> list[dict[str, Any]]:
    """Closed-bar RSI div filtered by fresh ULP overlap. No look-ahead past bar j."""
    tf_sec = tf_seconds(tf)
    if not bars:
        return []
    ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    o = np.array([float(b["open"]) for b in bars], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    rsi_sigs, _zones = detect_rsi_div(
        ts, o, h, l, c, tf_sec,
        n=RSI_LEN, ob=RSI_OB, os=RSI_OS,
        exp_obh=RSI_EXP_OBH, exp_obl=RSI_EXP_OBL,
    )
    t_index = _time_index(bars)
    snaps = live_zones_by_bar(bars, mode)
    out: list[dict[str, Any]] = []
    for sig in rsi_sigs:
        close_ts = int(sig["ts"])
        open_ts = close_ts - tf_sec
        j = t_index.get(open_ts)
        if j is None:
            continue
        boxes = snaps[j]
        bar = bars[j]
        hits = overlapping_fresh_zones(bar, boxes, j, t_index)
        hits = arm_allows(arm, str(sig["side"]), hits)
        if not hits:
            continue
        z0 = hits[0]
        out.append(
            {
                "signal_ts": close_ts,
                "signal_ist": ist_str(close_ts),
                "entry_ts": close_ts + 60,
                "tf": tf,
                "mode": "full" if str(mode).strip().lower() in ("full", "fullrange", "true", "1") else "wick",
                "arm": str(arm).strip().upper(),
                "side": str(sig["side"]),
                "signal_price": float(bar["close"]),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "zone_kind": str(z0.get("kind")),
                "zone_top": float(z0["top"]),
                "zone_bottom": float(z0["bottom"]),
                "age_bars": int(z0["age_bars"]),
                "pivot_ts": int(z0["t1"]),
                "n_hit_zones": len(hits),
                "fresh_zones": [
                    {
                        "kind": str(z.get("kind")),
                        "top": float(z["top"]),
                        "bottom": float(z["bottom"]),
                        "age_bars": int(z["age_bars"]),
                        "t1": int(z["t1"]),
                    }
                    for z in hits
                ],
                "ob_level": float(sig.get("ob_level", 0.0) or 0.0),
                "sig_rsi": float(sig.get("sig_rsi", float("nan"))),
            }
        )
    return out
