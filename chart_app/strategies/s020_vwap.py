"""S020 VWAP Swing overlay — imports backtest swing/VWAP/touch, does not reimplement."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s020_vwap_touch import run_s020 as s020
from backtest.strategies.s020_vwap_touch import run_s020_dev as s020_dev
from strategies import register

LINE_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}


def _arrays(bars: list[dict[str, Any]]) -> tuple[np.ndarray, ...]:
    ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    o = np.array([float(b["open"]) for b in bars], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    v = np.array([float(b.get("volume") or 0.0) for b in bars], dtype=np.float64)
    return ts, o, h, l, c, v


@register("S020")
def compute(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    if not bars:
        return {"lines": [], "markers": [], "series": []}
    line_tf = str(params.get("line_tf") or "1m")
    variant = str(params.get("variant") or "V0")
    if variant not in ("V0", "V1", "V2", "V3", "V4", "V5"):
        variant = "V0"
    hours = float(params.get("hours") or 24)
    to_ts = int(params.get("to") or bars[-1]["time"])
    cutoff = to_ts - int(hours * 3600)

    ts, o, h, l, c, vol = _arrays(bars)
    vwap_1m = s020.session_vwap(ts, h, l, c, vol)
    vwap_pts = [
        {"time": int(ts[i]), "value": float(vwap_1m[i])}
        for i in range(len(ts))
        if np.isfinite(vwap_1m[i])
    ]

    tf_sec = LINE_TF_SEC.get(line_tf, 60)
    tts, to_, th, tl, tc, tv = s020_dev.resample_tf(ts, o, h, l, c, vol, tf_sec)
    if len(tts) == 0:
        return {
            "lines": [{"id": "vwap", "kind": "vwap", "active": True, "points": vwap_pts}],
            "markers": [],
            "series": [],
        }
    vwap_tf = s020.session_vwap(tts, th, tl, tc, tv)
    raw_lines, _, _ = s020.detect_swings(tts, to_, th, tl, tc, vwap_tf)
    dlines = s020_dev.tf_lines_to_dev(tts, raw_lines, tf_sec)
    work = [
        s020_dev.DevLine(
            kind=x.kind,
            level=x.level,
            extreme=x.extreme,
            create_ts=x.create_ts,
            active_from_ts=x.active_from_ts,
            session_utc=x.session_utc,
            cross_ts=x.cross_ts,
        )
        for x in dlines
    ]
    sigs, _ = s020_dev.collect_signals(ts, o, h, l, c, work, variant)
    last_ts = int(ts[-1])
    out_lines: list[dict[str, Any]] = [
        {"id": "vwap", "kind": "vwap", "active": True, "points": vwap_pts}
    ]
    n_drawn = 0
    for i, ln in enumerate(work):
        t0 = int(ln.active_from_ts)
        t1 = int(ln.expire_ts) if ln.expire_ts is not None else last_ts
        if t1 < cutoff:
            continue
        if t0 > to_ts:
            continue
        n_drawn += 1
        if n_drawn > 120:
            break
        out_lines.append(
            {
                "id": f"{ln.kind}_{i}_{t0}",
                "kind": ln.kind,
                "active": ln.expire_ts is None,
                "level": float(ln.level),
                "points": [
                    {"time": t0, "value": float(ln.level)},
                    {"time": max(t0, t1), "value": float(ln.level)},
                ],
            }
        )
    markers: list[dict[str, Any]] = []
    for s in sigs:
        t = int(s["ts"])
        if t < cutoff or t > to_ts:
            continue
        markers.append(
            {
                "time": t,
                "side": str(s["side"]),
                "text": "L" if s["side"] == "long" else "S",
            }
        )
    return {"lines": out_lines, "markers": markers, "series": []}


def register_plugin() -> None:
    """Import side-effect: @register already bound compute."""
    return
