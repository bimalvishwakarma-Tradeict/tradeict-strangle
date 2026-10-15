"""Streaming ULP zones + touch signals. Zone math is chart_app compute_ulp."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[3]
_CHART = _ROOT / "chart_app"
for _p in (str(_ROOT), str(_CHART)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from indicators.ulp import DUP_EPS, compute_ulp, is_pivot_high, is_pivot_low  # noqa: E402

__all__ = [
    "DUP_EPS",
    "compute_ulp",
    "iter_ulp_signals",
    "run_ulp",
    "ulp_params",
]

LOOKBACK = 10
MAX_LEVELS = 100
MIN_AGE = 20
IST = ZoneInfo("Asia/Kolkata")
TF_SEC: dict[str, int] = {"5m": 300, "15m": 900, "30m": 1800}

def ulp_params(mode: str) -> dict[str, Any]:
    return {
        "lookback": LOOKBACK,
        "maxLevels": MAX_LEVELS,
        "fullRange": is_full_mode(mode),
        "showLabels": False,
        "showExpired": False,
    }


def is_full_mode(mode: str) -> bool:
    s = str(mode).strip().lower()
    return s in ("full", "fullrange", "true", "1")


def tf_seconds(tf: str) -> int:
    if tf not in TF_SEC:
        raise ValueError(f"unsupported signal tf {tf!r}; expected {sorted(TF_SEC)}")
    return int(TF_SEC[tf])


def ist_str(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def zone_key(z: dict[str, Any]) -> tuple[int, float, float, str]:
    return (int(z["t1"]), float(z["top"]), float(z["bottom"]), str(z["kind"]))


def boxes_to_keys(boxes: list[dict[str, Any]]) -> set[tuple[int, float, float, str]]:
    return {zone_key(b) for b in boxes if not b.get("expired")}


def _touches(bar: dict[str, Any], z: dict[str, Any]) -> bool:
    if z["kind"] == "high":
        return float(bar["high"]) >= float(z["bottom"])
    return float(bar["low"]) <= float(z["top"])


def _fresh_ok(prev: dict[str, Any] | None, z: dict[str, Any]) -> bool:
    if prev is None:
        return True
    if z["kind"] == "high":
        return float(prev["high"]) < float(z["bottom"])
    return float(prev["low"]) > float(z["top"])


def _emit(
    *,
    bar: dict[str, Any],
    z: dict[str, Any],
    i: int,
    tf: str,
    mode: str,
    rule: str,
    tf_sec: int,
    n_active: int,
) -> dict[str, Any]:
    top = float(z["top"])
    bot = float(z["bottom"])
    open_t = int(bar["time"])
    close_t = open_t + tf_sec
    confirm_open = int(z["confirm_t"])
    return {
        "signal_ts": close_t,
        "signal_ist": ist_str(close_t),
        "tf": tf,
        "mode": "full" if is_full_mode(mode) else "wick",
        "rule": rule,
        "zone_kind": str(z["kind"]),
        "pivot_ts": int(z["t1"]),
        "confirm_ts": confirm_open + tf_sec,
        "age_bars": int(i - int(z["pivot_i"])),
        "zone_top": top,
        "zone_bottom": bot,
        "zone_height": top - bot,
        "open": float(bar["open"]),
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "close": float(bar["close"]),
        "spot_close": float(bar["close"]),
        "n_active_zones": int(n_active),
    }


def run_ulp(
    bars: list[dict[str, Any]],
    mode: str,
    tf: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bar-by-bar ULP (same add/dup/delete as compute_ulp) plus touch signals.

    Touch is evaluated after zone adds and before deletion on the same bar.
    """
    L = LOOKBACK
    max_lv = MAX_LEVELS
    full = is_full_mode(mode)
    tf_sec = tf_seconds(tf)
    if not bars:
        return [], []

    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    zones_h: list[dict[str, Any]] = []
    zones_l: list[dict[str, Any]] = []
    n_add = 0
    signals: list[dict[str, Any]] = []

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
                                "confirm_t": int(bar["time"]),
                                "top": top,
                                "bottom": bottom,
                                "used_fresh": False,
                                "used_any": False,
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
                                "confirm_t": int(bar["time"]),
                                "top": top,
                                "bottom": bottom,
                                "used_fresh": False,
                                "used_any": False,
                            }
                        )
                        pop_oldest(zones_l)

        n_active = len(zones_h) + len(zones_l)
        prev = bars[i - 1] if i > 0 else None
        for z in zones_h + zones_l:
            age = i - int(z["pivot_i"])
            if age < MIN_AGE:
                continue
            if not _touches(bar, z):
                continue
            if not z["used_any"]:
                z["used_any"] = True
                signals.append(
                    _emit(
                        bar=bar, z=z, i=i, tf=tf, mode=mode, rule="any",
                        tf_sec=tf_sec, n_active=n_active,
                    )
                )
            if (not z["used_fresh"]) and _fresh_ok(prev, z):
                z["used_fresh"] = True
                signals.append(
                    _emit(
                        bar=bar, z=z, i=i, tf=tf, mode=mode, rule="fresh",
                        tf_sec=tf_sec, n_active=n_active,
                    )
                )

        hi = float(bar["high"])
        lo = float(bar["low"])
        zones_h = [z for z in zones_h if hi < float(z["top"])]
        zones_l = [z for z in zones_l if lo > float(z["bottom"])]

    remaining = [
        {"t1": int(z["t1"]), "top": float(z["top"]), "bottom": float(z["bottom"]), "kind": str(z["kind"])}
        for z in zones_h + zones_l
    ]
    return signals, remaining


def iter_ulp_signals(
    bars: list[dict[str, Any]],
    mode: str,
    tf: str,
) -> list[dict[str, Any]]:
    """Primary stream: all signals (rule=fresh and rule=any). Fresh is the trade rule."""
    sigs, _ = run_ulp(bars, mode, tf)
    return sigs
