"""S022F overlay: ULP fresh + RSI-div + Pine ADX rising. Imports backtest; no copied math."""

from __future__ import annotations

from typing import Any

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div
from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4
from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals
from backtest.strategies.s022_ulp_long_strangle.stage0_filter import (
    ist_str,
    match_arm_f,
    rsi_masks,
)
from indicators.calc import to_bool, to_int
from indicators.htf import resample_tf_partial, split_htf_complete
from strategies import register

LINE_TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800}


def _snap_bar_start(t: int, sec: int) -> int:
    t = int(t)
    sec = int(sec)
    if sec <= 1:
        return t
    rem = t % sec
    return t if rem == 0 else t + (sec - rem)


def _arrays(bars: list[dict[str, Any]]) -> tuple[np.ndarray, ...]:
    ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    o = np.array([float(b["open"]) for b in bars], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    v = np.array([float(b.get("volume") or 0.0) for b in bars], dtype=np.float64)
    return ts, o, h, l, c, v


def _ohlc_bars(
    tts: np.ndarray, to_: np.ndarray, th: np.ndarray, tl: np.ndarray, tc: np.ndarray,
) -> list[dict[str, Any]]:
    return [
        {
            "time": int(t),
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
        }
        for t, o, h, lo, c in zip(tts, to_, th, tl, tc)
    ]


def _run_closed(
    tts: np.ndarray,
    to_: np.ndarray,
    th: np.ndarray,
    tl: np.ndarray,
    tc: np.ndarray,
    tf_sec: int,
    sig_tf: str,
    mode: str,
    n_win: int,
    rsi_len: int,
    ob: float,
    os_: float,
    exp_obh: float,
    exp_obl: float,
    adx_len: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(tts) == 0:
        return [], []
    bars = _ohlc_bars(tts, to_, th, tl, tc)
    raw = [s for s in iter_ulp_signals(bars, mode, sig_tf) if s["rule"] == "fresh"]
    sigs, _z = detect_rsi_div(
        tts, to_, th, tl, tc, tf_sec, n=rsi_len, ob=ob, os=os_,
        exp_obh=exp_obh, exp_obl=exp_obl,
    )
    rsi_short, rsi_long = rsi_masks(sigs, tts, tf_sec)
    _dip, _dim, adx = adx_pine_v4(th, tl, tc, length=adx_len, warmup=500)
    ev = match_arm_f(raw, rsi_short, rsi_long, adx, tts, tf_sec, n_win)
    return raw, ev


@register("S022F")
def compute(bars: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
    if not bars:
        return {"lines": [], "markers": [], "series": []}
    sig_tf = str(params.get("signal_tf") or "15m")
    mode = str(params.get("ulp_mode") or "full")
    n_win = max(1, min(24, to_int(params.get("n_win"), 6)))
    rsi_len = max(2, min(50, to_int(params.get("rsi_len"), 14)))
    ob = float(params.get("ob") if params.get("ob") is not None else 70)
    os_ = float(params.get("os") if params.get("os") is not None else 30)
    exp_obh = float(params.get("exp_obh") if params.get("exp_obh") is not None else 40)
    exp_obl = float(params.get("exp_obl") if params.get("exp_obl") is not None else 60)
    adx_len = max(2, min(100, to_int(params.get("adx_len"), 14)))
    show_touches = to_bool(params.get("show_touches"), True)
    show_entries = to_bool(params.get("show_entries"), True)
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
    cap_1m = int(to_ts)
    tts_all, to_all, th_all, tl_all, tc_all, tv_all = resample_tf_partial(
        ts, o, h, l, c, vol, tf_sec,
    )
    if len(tts_all) == 0:
        return {"lines": [], "markers": [], "series": []}
    (tts, to_, th, tl, tc, tv), partial = split_htf_complete(
        tts_all, to_all, th_all, tl_all, tc_all, tv_all, tf_sec, cap_1m,
    )
    raw_c, ev_c = _run_closed(
        tts, to_, th, tl, tc, tf_sec, sig_tf, mode, n_win,
        rsi_len, ob, os_, exp_obh, exp_obl, adx_len,
    )
    pending_ulp: list[dict[str, Any]] = []
    pending_ev: list[dict[str, Any]] = []
    if partial is not None:
        raw_a, ev_a = _run_closed(
            tts_all, to_all, th_all, tl_all, tc_all, tf_sec, sig_tf, mode, n_win,
            rsi_len, ob, os_, exp_obh, exp_obl, adx_len,
        )
        conf_sig = {int(e["signal_time"]) for e in ev_c}
        pending_ev = [e for e in ev_a if int(e["signal_time"]) not in conf_sig]
        conf_touch = {int(s["signal_ts"]) for s in raw_c}
        pending_ulp = [s for s in raw_a if int(s["signal_ts"]) not in conf_touch]

    markers: list[dict[str, Any]] = []
    if show_touches:
        for s in raw_c:
            close_ts = int(s["signal_ts"])
            open_ts = close_ts - int(tf_sec)
            t = _snap_bar_start(open_ts, chart_sec)
            if t < cutoff or t > to_ts:
                continue
            kind = str(s.get("zone_kind") or "")
            side = "short" if kind == "high" else "long"
            markers.append(
                {
                    "time": t,
                    "side": side,
                    "text": "T",
                    "size": "small",
                    "entry_allowed": True,
                    "pending": False,
                    "zone_kind": kind,
                    "touch_time_ist": ist_str(close_ts),
                    "signal_close_ts": close_ts,
                }
            )
        for s in pending_ulp:
            close_ts = int(s["signal_ts"])
            open_ts = close_ts - int(tf_sec)
            t = _snap_bar_start(open_ts, chart_sec)
            if t < cutoff or t > to_ts:
                continue
            kind = str(s.get("zone_kind") or "")
            side = "short" if kind == "high" else "long"
            markers.append(
                {
                    "time": t,
                    "side": side,
                    "text": "T",
                    "size": "small",
                    "entry_allowed": False,
                    "reason": "pending_bar",
                    "pending": True,
                    "zone_kind": kind,
                    "touch_time_ist": ist_str(close_ts),
                    "signal_close_ts": close_ts,
                }
            )
    if show_entries:
        for e in ev_c:
            close_ts = int(e["signal_time"])
            open_ts = close_ts - int(tf_sec)
            t = _snap_bar_start(open_ts, chart_sec)
            if t < cutoff or t > to_ts:
                continue
            kind = str(e.get("zone_kind") or "")
            letter = "H" if kind == "high" or str(e.get("match_side") or "") == "short" else "L"
            if kind == "low":
                letter = "L"
            side = "short" if letter == "H" else "long"
            markers.append(
                {
                    "time": t,
                    "side": side,
                    "text": f"S022F {letter}",
                    "size": "large",
                    "entry_allowed": True,
                    "pending": False,
                    "zone_kind": kind,
                    "touch_time_ist": ist_str(int(e["touch_time"])),
                    "signal_time_ist": ist_str(close_ts),
                    "adx_j": e.get("adx_j"),
                    "rsi_side": e.get("match_side"),
                    "N": n_win,
                    "signal_close_ts": close_ts,
                }
            )
        for e in pending_ev:
            close_ts = int(e["signal_time"])
            open_ts = close_ts - int(tf_sec)
            t = _snap_bar_start(open_ts, chart_sec)
            if t < cutoff or t > to_ts:
                continue
            kind = str(e.get("zone_kind") or "")
            letter = "H" if kind == "high" or str(e.get("match_side") or "") == "short" else "L"
            if kind == "low":
                letter = "L"
            side = "short" if letter == "H" else "long"
            markers.append(
                {
                    "time": t,
                    "side": side,
                    "text": f"S022F {letter}",
                    "size": "large",
                    "entry_allowed": False,
                    "reason": "pending_bar",
                    "pending": True,
                    "zone_kind": kind,
                    "touch_time_ist": ist_str(int(e["touch_time"])),
                    "signal_time_ist": ist_str(close_ts),
                    "adx_j": e.get("adx_j"),
                    "rsi_side": e.get("match_side"),
                    "N": n_win,
                    "signal_close_ts": close_ts,
                }
            )
    markers.sort(key=lambda m: (int(m["time"]), 0 if m.get("text") == "T" else 1))
    return {"lines": [], "markers": markers, "series": []}


META: dict[str, Any] = {
    "name": "S022F ULP+RSIDiv+ADX",
    "description": "Research REJECTED (Stage 0F-2, 0/504) — manual observation only",
    "short_name": "S022F",
    "overlay": True,
    "inputs": [
        {
            "key": "signal_tf", "label": "Signal TF", "type": "select", "default": "15m",
            "options": [
                {"value": "5m", "label": "5m"},
                {"value": "15m", "label": "15m"},
                {"value": "30m", "label": "30m"},
            ],
            "tab": "inputs",
        },
        {
            "key": "ulp_mode", "label": "ULP mode", "type": "select", "default": "full",
            "options": [
                {"value": "wick", "label": "wick"},
                {"value": "full", "label": "full"},
            ],
            "tab": "inputs",
        },
        {
            "key": "n_win", "label": "N window", "type": "int", "default": 6,
            "min": 1, "max": 24, "step": 1, "tab": "inputs",
        },
        {
            "key": "rsi_len", "label": "RSI Length", "type": "int", "default": 14,
            "min": 2, "max": 50, "step": 1, "tab": "inputs",
        },
        {
            "key": "ob", "label": "OB", "type": "float", "default": 70,
            "min": 50, "max": 90, "step": 1, "tab": "inputs",
        },
        {
            "key": "os", "label": "OS", "type": "float", "default": 30,
            "min": 10, "max": 50, "step": 1, "tab": "inputs",
        },
        {
            "key": "exp_obh", "label": "OBH expire RSI", "type": "float", "default": 40,
            "min": 10, "max": 70, "step": 1, "tab": "inputs",
        },
        {
            "key": "exp_obl", "label": "OBL expire RSI", "type": "float", "default": 60,
            "min": 30, "max": 90, "step": 1, "tab": "inputs",
        },
        {
            "key": "adx_len", "label": "ADX Length", "type": "int", "default": 14,
            "min": 2, "max": 100, "step": 1, "tab": "inputs",
        },
        {
            "key": "adx_k", "label": "ADX rise bars", "type": "int", "default": 5,
            "min": 1, "max": 10, "step": 1, "tab": "inputs",
        },
        {"key": "show_touches", "label": "Show ULP touches", "type": "bool", "default": True, "tab": "inputs"},
        {"key": "show_entries", "label": "Show entries", "type": "bool", "default": True, "tab": "inputs"},
        {
            "key": "max_days", "label": "1m window days", "type": "int", "default": 7,
            "min": 1, "max": 30, "step": 1, "tab": "inputs",
        },
    ],
}


def register_plugin() -> None:
    from strategies import register_meta

    register_meta("S022F", META)
