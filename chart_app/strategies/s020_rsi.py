"""S020 RSI Div overlay. Imports rsi_div; does not reimplement Wilder/OBH rules."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s020_vwap_touch import run_s020_dev as s020_dev
from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div, rsi_wilder
from strategies import register

LINE_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800}


def _snap_bar_start(t: int, sec: int) -> int:
    """First chart-bar open >= t (unix aligned)."""
    t = int(t)
    sec = int(sec)
    if sec <= 1:
        return t
    rem = t % sec
    return t if rem == 0 else t + (sec - rem)


def _marker_rank(text: str) -> int:
    if text in ("OBH", "OBL"):
        return 0
    return 1


def _arrays(bars: list[dict[str, Any]]) -> tuple[np.ndarray, ...]:
    ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    o = np.array([float(b["open"]) for b in bars], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    v = np.array([float(b.get("volume") or 0.0) for b in bars], dtype=np.float64)
    return ts, o, h, l, c, v


@register("S020_RSI")
def compute(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    if not bars:
        return {"lines": [], "markers": [], "series": [], "rsi": []}
    sig_tf = str(params.get("signal_tf") or params.get("line_tf") or "15m")
    n = int(params.get("rsi_len") or 14)
    ob = float(params.get("ob") or 70)
    os_ = float(params.get("os") or 30)
    show_obh = bool(params.get("show_obh", True))
    show_obl = bool(params.get("show_obl", True))
    show_levels = bool(params.get("show_levels", True))
    show_sigs = bool(params.get("show_signals", True))
    hours = float(params.get("hours") or 24)
    to_ts = int(params.get("to") or bars[-1]["time"])
    if params.get("from") is not None:
        cutoff = int(params.get("from"))
    else:
        cutoff = to_ts - int(hours * 3600)

    ts, o, h, l, c, vol = _arrays(bars)
    tf_sec = LINE_TF_SEC.get(sig_tf, 900)
    chart_tf = str(params.get("chart_tf") or params.get("tf") or "1m")
    chart_sec = LINE_TF_SEC.get(chart_tf, 60)
    tts, to_, th, tl, tc, tv = s020_dev.resample_tf(ts, o, h, l, c, vol, tf_sec)
    if len(tts) == 0:
        return {"lines": [], "markers": [], "series": [], "rsi": []}
    sigs, zones = detect_rsi_div(tts, to_, th, tl, tc, tf_sec, n=n, ob=ob, os=os_)
    rsi = rsi_wilder(tc, n)
    rsi_pts = [
        {"time": int(tts[i]) + int(tf_sec), "value": float(rsi[i])}
        for i in range(len(tts))
        if np.isfinite(rsi[i])
    ]
    last_ts = int(ts[-1])
    lines: list[dict[str, Any]] = []
    if show_levels:
        for i, z in enumerate(zones):
            kind = str(z["kind"])
            if kind == "OBH" and not show_obh:
                continue
            if kind == "OBL" and not show_obl:
                continue
            t0 = int(z.get("confirm_ts") or z.get("start_ts") or 0)
            t1 = int(z.get("end_ts") or last_ts)
            if t1 < cutoff or t0 > to_ts or t1 <= t0:
                continue
            lines.append(
                {
                    "id": f"{kind}_{i}",
                    "kind": "high" if kind == "OBH" else "low",
                    "active": not bool(z.get("end_reason")),
                    "level": float(z["level"]),
                    "text": kind,
                    "points": [
                        {"time": t0, "value": float(z["level"])},
                        {"time": t1, "value": float(z["level"])},
                    ],
                }
            )
    markers: list[dict[str, Any]] = []
    if show_obh or show_obl:
        seen: set[int] = set()
        for z in zones:
            kind = str(z["kind"])
            if kind == "OBH" and not show_obh:
                continue
            if kind == "OBL" and not show_obl:
                continue
            t = _snap_bar_start(int(z.get("ob_candle_ts") or 0), chart_sec)
            if t < cutoff or t > to_ts or t in seen:
                continue
            seen.add(t)
            markers.append(
                {
                    "time": t,
                    "side": "short" if kind == "OBH" else "long",
                    "text": kind,
                    "size": "small",
                    "level": float(z["level"]),
                    "entry_allowed": True,
                    "reason": "",
                    "ob_rsi": float(z.get("rsi") or 0.0),
                    "sig_rsi": "",
                }
            )
    if show_sigs:
        for s in sigs:
            close_ts = int(s["ts"])
            open_ts = close_ts - int(tf_sec)
            t = _snap_bar_start(open_ts, chart_sec)
            if t < cutoff or t > to_ts:
                continue
            entry_ts = close_ts + 60
            reason = ""
            if s020_dev.skip_lunch_ist(entry_ts):
                reason = "skip_lunch"
            elif s020_dev.skip_thu_sat_window(entry_ts):
                reason = "skip_thu_sat"
            allowed = reason == ""
            markers.append(
                {
                    "time": t,
                    "side": str(s["side"]),
                    "text": "SHORT" if s["side"] == "short" else "LONG",
                    "size": "large",
                    "level": float(s.get("ob_level") or 0.0),
                    "entry_allowed": allowed,
                    "reason": reason,
                    "ob_rsi": float(s.get("ob_rsi") or 0.0),
                    "sig_rsi": float(s.get("sig_rsi") or 0.0),
                    "signal_close_ts": close_ts,
                }
            )
    markers.sort(key=lambda m: (int(m["time"]), _marker_rank(str(m.get("text") or ""))))
    return {
        "lines": lines,
        "markers": markers,
        "series": [],
        "rsi": rsi_pts,
        "rsi_ob": ob,
        "rsi_os": os_,
    }


def register_plugin() -> None:
    return
