"""S020 VWAP Swing overlay — imports backtest swing/VWAP/touch, does not reimplement."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s020_vwap_touch import run_s020 as s020
from backtest.strategies.s020_vwap_touch import run_s020_dev as s020_dev
from strategies import register

LINE_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}


def _extreme_ts(
    ln: Any,
    tts: np.ndarray,
    th: np.ndarray,
    tl: np.ndarray,
    tc: np.ndarray,
    vwap: np.ndarray,
    raw: Any,
) -> int:
    ci = int(raw.cross_i) if raw is not None else -1
    if ci <= 0 or ci >= len(tts):
        return int(ln.cross_ts or ln.active_from_ts)
    day = int(tts[ci]) // 86400
    kind = str(ln.kind)
    level = float(ln.level)
    run: list[int] = []
    i = ci - 1
    while i >= 0 and int(tts[i]) // 86400 == day:
        vw = float(vwap[i])
        cl = float(tc[i])
        if not np.isfinite(vw):
            break
        if kind == "low" and not (cl < vw):
            break
        if kind == "high" and not (cl > vw):
            break
        run.append(i)
        i -= 1
    run.reverse()
    if not run:
        return int(ln.cross_ts or ln.active_from_ts)
    for j in run:
        px = float(tl[j]) if kind == "low" else float(th[j])
        if abs(px - level) <= 1e-6:
            return int(tts[j])
    if kind == "low":
        j = min(run, key=lambda k: float(tl[k]))
    else:
        j = max(run, key=lambda k: float(th[k]))
    return int(tts[j])


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
    if params.get("from") is not None:
        cutoff = int(params.get("from"))
    else:
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
    raw_by_cross: dict[int, Any] = {}
    for rl in raw_lines:
        if 0 <= int(rl.cross_i) < len(tts):
            raw_by_cross[int(tts[int(rl.cross_i)])] = rl
    out_lines: list[dict[str, Any]] = [
        {"id": "vwap", "kind": "vwap", "active": True, "points": vwap_pts}
    ]
    spot_ref = float(c[-1])
    band = abs(spot_ref) * 0.02 if np.isfinite(spot_ref) and spot_ref else 0.0
    n_drawn = 0
    for i, ln in enumerate(work):
        raw = raw_by_cross.get(int(ln.cross_ts))
        t0 = _extreme_ts(ln, tts, th, tl, tc, vwap_tf, raw)
        t1 = int(ln.expire_ts) if ln.expire_ts is not None else last_ts
        if t1 < cutoff:
            continue
        if t0 > to_ts:
            continue
        if band > 0 and abs(float(ln.level) - spot_ref) > band:
            continue
        n_drawn += 1
        if n_drawn > 500:
            break
        out_lines.append(
            {
                "id": f"{ln.kind}_{i}_{int(ln.create_ts)}",
                "kind": ln.kind,
                "active": ln.expire_ts is None,
                "level": float(ln.level),
                "create_ts": int(ln.create_ts),
                "points": [
                    {"time": int(t0), "value": float(ln.level)},
                    {"time": max(int(t0), int(t1)), "value": float(ln.level)},
                ],
            }
        )
    markers: list[dict[str, Any]] = []
    for s in sigs:
        t = int(s["ts"])
        if t < cutoff or t > to_ts:
            continue
        allowed = bool(s020_dev.entry_allowed(t))
        reason = ""
        if s020_dev.skip_lunch_ist(t):
            reason = "skip_lunch"
        elif s020_dev.skip_thu_sat_window(t):
            reason = "skip_thu_sat"
        create_ts = 0
        for ln in work:
            if str(ln.kind) != str(s.get("kind") or ln.kind):
                continue
            if abs(float(ln.level) - float(s.get("level") or 0.0)) > 1e-6:
                continue
            if int(ln.active_from_ts) <= t:
                create_ts = int(ln.create_ts)
                break
        markers.append(
            {
                "time": t,
                "side": str(s["side"]),
                "text": "L" if s["side"] == "long" else "S",
                "level": float(s.get("level") or 0.0),
                "create_ts": create_ts,
                "entry_allowed": allowed,
                "reason": reason,
            }
        )
    return {"lines": out_lines, "markers": markers, "series": []}


def register_plugin() -> None:
    """Import side-effect: @register already bound compute."""
    return
