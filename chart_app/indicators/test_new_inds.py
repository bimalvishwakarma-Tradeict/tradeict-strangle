"""Unit tests for EMA / BB / Supertrend / RSI (no live API)."""

from __future__ import annotations

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import rsi_wilder
from indicators.bollinger import compute_bb
from indicators.calc import ema, sma, stdev_pop
from indicators.ema import compute_ema
from indicators.rsi import compute_rsi
from indicators.supertrend import compute_supertrend


def test_ema_seed_is_sma() -> None:
    src = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], dtype=np.float64)
    out = ema(src, 3)
    assert np.isnan(out[1])
    assert abs(out[2] - 2.0) < 1e-12  # SMA(1,2,3)
    alpha = 2.0 / 4.0
    expect = alpha * 4.0 + (1 - alpha) * 2.0
    assert abs(out[3] - expect) < 1e-12


def test_stdev_is_population() -> None:
    src = np.array([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0], dtype=np.float64)
    sd = stdev_pop(src, 8)
    # population: mean 5, var 4, stdev 2
    assert abs(float(sd[-1]) - 2.0) < 1e-12
    sample = float(np.std(src, ddof=1))
    assert abs(sample - 2.0) > 0.1


def test_sma_window() -> None:
    src = np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float64)
    out = sma(src, 2)
    assert np.isnan(out[0])
    assert abs(out[1] - 15.0) < 1e-12
    assert abs(out[3] - 35.0) < 1e-12


def test_rsi_uses_wilder() -> None:
    bars = []
    c = 100.0
    for i in range(40):
        c += 1.0 if i % 3 else -0.5
        bars.append({"time": 1_000_000 + i * 60, "open": c, "high": c + 1, "low": c - 1, "close": c})
    src = np.array([float(b["close"]) for b in bars])
    ref = rsi_wilder(src, 14)
    out = compute_rsi(bars, {"length": 14, "source": "close"})
    pts = {int(p["time"]): p["value"] for p in out["series"][0]["points"]}
    last_t = int(bars[-1]["time"])
    assert abs(pts[last_t] - float(ref[-1])) < 1e-9


def test_ema_indicator_offset_clips() -> None:
    bars = [
        {"time": 1000 + i * 60, "open": 10 + i, "high": 11 + i, "low": 9 + i, "close": 10 + i}
        for i in range(20)
    ]
    out = compute_ema(bars, {"length": 9, "offset": 2})
    pts = out["series"][0]["points"]
    last_t = int(bars[-1]["time"])
    assert all(int(p["time"]) <= last_t for p in pts)


def test_bb_upper_above_lower() -> None:
    bars = [
        {"time": 1000 + i * 60, "open": 100, "high": 101 + (i % 3), "low": 99 - (i % 2), "close": 100 + (i % 5) * 0.2}
        for i in range(40)
    ]
    out = compute_bb(bars, {"length": 20, "mult": 2.0, "ma_type": "SMA"})
    keys = {s["key"]: s["points"] for s in out["series"]}
    last = {k: v[-1]["value"] for k, v in keys.items()}
    assert last["upper"] > last["basis"] > last["lower"]
    assert out["bands"]


def test_supertrend_has_direction() -> None:
    bars = []
    c = 100.0
    for i in range(80):
        c += 0.8
        bars.append({"time": 1000 + i * 60, "open": c - 0.2, "high": c + 0.5, "low": c - 0.5, "close": c})
    out = compute_supertrend(bars, {"atr_length": 10, "factor": 3.0, "showLabels": True})
    up = [p for p in out["series"][0]["points"] if "value" in p]
    assert up, "uptrend climb should plot lower green ST"
    last_up = up[-1]["value"]
    assert last_up < float(bars[-1]["close"])
