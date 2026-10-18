"""S024 RDBB signal unit tests (t1–t5)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np

from backtest.strategies.s024_rdbb.signals import (
    bb_side_ok,
    bollinger,
    iter_rdbb_signals,
    passes_move_filter,
    weekend_bucket,
)

IST = ZoneInfo("Asia/Kolkata")
TF_SEC = 300


def _bar(i: int, o: float, h: float, l: float, c: float) -> dict[str, Any]:
    return {"time": i * TF_SEC, "open": o, "high": h, "low": l, "close": c}


def _bars_flat(n: int, px: float = 100.0) -> list[dict[str, Any]]:
    return [_bar(i, px, px, px, px) for i in range(n)]


def _fake_rsi(bars: list[dict[str, Any]], j: int, side: str) -> list[dict[str, Any]]:
    return [
        {
            "ts": int(bars[j]["time"]) + TF_SEC,
            "side": side,
            "ob_level": 0.0,
            "sig_rsi": 50.0,
        }
    ]


def test_t1_bb_parity_sma_stdev_ddof0() -> None:
    close = np.arange(1.0, 21.0)
    sma, upper, lower = bollinger(close, 20, 2.0)
    for i in range(19):
        assert np.isnan(sma[i]) and np.isnan(upper[i]) and np.isnan(lower[i])
    # 1..20: SMA = 10.5; population var = 33.25; stdev = sqrt(33.25)
    mean = 10.5
    stdev = 5.766281297335398
    assert abs(float(sma[19]) - mean) < 1e-9
    assert abs(float(upper[19]) - (mean + 2.0 * stdev)) < 1e-9
    assert abs(float(lower[19]) - (mean - 2.0 * stdev)) < 1e-9
    close2 = np.concatenate([close, np.array([21.0])])
    sma2, u2, l2 = bollinger(close2, 20, 2.0)
    mean2 = 11.5
    assert abs(float(sma2[20]) - mean2) < 1e-9
    assert abs(float(u2[20]) - (mean2 + 2.0 * stdev)) < 1e-9
    assert abs(float(l2[20]) - (mean2 - 2.0 * stdev)) < 1e-9


def test_t2_bb_break_strict() -> None:
    assert bb_side_ok("short", 101.0, 99.0, 100.0, 90.0) is True
    assert bb_side_ok("short", 100.0, 99.0, 100.0, 90.0) is False
    assert bb_side_ok("long", 101.0, 89.0, 110.0, 90.0) is True
    assert bb_side_ok("long", 101.0, 90.0, 110.0, 90.0) is False
    bars = _bars_flat(25, 100.0)
    j = 24
    bars[j] = _bar(j, 100.0, 101.0, 100.0, 100.0)

    def fake_short(*_a: Any, **_k: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _fake_rsi(bars, j, "short"), []

    with patch("backtest.strategies.s024_rdbb.signals.detect_rsi_div", fake_short):
        got = iter_rdbb_signals(bars, "5m", "R3", "nofilter")
    assert len(got) == 1
    bars_eq = list(bars)
    bars_eq[j] = _bar(j, 100.0, 100.0, 100.0, 100.0)
    with patch("backtest.strategies.s024_rdbb.signals.detect_rsi_div", fake_short):
        got_eq = iter_rdbb_signals(bars_eq, "5m", "R3", "nofilter")
    assert got_eq == []

    bars_l = _bars_flat(25, 100.0)
    bars_l[j] = _bar(j, 100.0, 100.0, 99.0, 100.0)

    def fake_long(*_a: Any, **_k: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _fake_rsi(bars_l, j, "long"), []

    with patch("backtest.strategies.s024_rdbb.signals.detect_rsi_div", fake_long):
        got_l = iter_rdbb_signals(bars_l, "5m", "R3", "nofilter")
    assert len(got_l) == 1
    bars_l_eq = list(bars_l)
    bars_l_eq[j] = _bar(j, 100.0, 100.0, 100.0, 100.0)
    with patch("backtest.strategies.s024_rdbb.signals.detect_rsi_div", fake_long):
        got_l_eq = iter_rdbb_signals(bars_l_eq, "5m", "R3", "nofilter")
    assert got_l_eq == []


def test_t3_move_filter_n4_x1_boundary() -> None:
    close = np.array([100.0, 100.0, 100.0, 100.0])
    high = np.array([100.0, 100.0, 100.0, 101.0])
    low = np.array([100.0, 100.0, 100.0, 100.0])
    assert abs(float((101.0 - 100.0) / 100.0) - 0.01) < 1e-12
    assert passes_move_filter(high, low, close, 3, "N4_X1") is True
    high2 = np.array([100.0, 100.0, 100.0, 100.99])
    assert abs(float((100.99 - 100.0) / 100.0) - 0.0099) < 1e-12
    assert passes_move_filter(high2, low, close, 3, "N4_X1") is False


def test_t4_lookahead_future_bars_do_not_change_j() -> None:
    bars = _bars_flat(30, 100.0)
    j = 24
    bars[j] = _bar(j, 100.0, 105.0, 100.0, 100.0)

    def fake(*_a: Any, **_k: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _fake_rsi(bars, j, "short"), []

    with patch("backtest.strategies.s024_rdbb.signals.detect_rsi_div", fake):
        a = iter_rdbb_signals(bars, "5m", "R3", "nofilter")
    bars2 = list(bars)
    for k in range(j + 1, len(bars2)):
        bars2[k] = _bar(k, 200.0, 250.0, 50.0, 180.0)
    with patch("backtest.strategies.s024_rdbb.signals.detect_rsi_div", fake):
        b = iter_rdbb_signals(bars2, "5m", "R3", "nofilter")
    assert len(a) == 1 and len(b) == 1
    assert a[0]["signal_ts"] == b[0]["signal_ts"] == int(bars[j]["time"]) + TF_SEC
    assert a[0]["bar_j"] == b[0]["bar_j"] == j
    assert a[0]["signal_price"] == b[0]["signal_price"] == 100.0


def test_t5_weekend_bucket_ist() -> None:
    def unix(y: int, m: int, d: int, hh: int, mm: int) -> int:
        return int(datetime(y, m, d, hh, mm, tzinfo=IST).timestamp())

    assert weekend_bucket(unix(2025, 6, 20, 11, 59)) == "WEEKDAY"
    assert weekend_bucket(unix(2025, 6, 20, 12, 0)) == "WEEKEND"
    assert weekend_bucket(unix(2025, 6, 22, 3, 59)) == "WEEKEND"
    assert weekend_bucket(unix(2025, 6, 22, 4, 0)) == "WEEKDAY"
