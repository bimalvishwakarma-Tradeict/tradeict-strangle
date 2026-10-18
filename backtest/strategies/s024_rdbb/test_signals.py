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
    parse_filter,
    passes_move_filter,
    weekend_bucket,
)
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    SEED,
    ist_hod,
    ist_ym,
)
from backtest.strategies.s024_rdbb.stage0 import (  # noqa: E402
    filter_pass_close_ts,
    filtctrl_events,
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


def _ist_unix(y: int, m: int, d: int, hh: int, mm: int) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=IST).timestamp())


def _range_bars() -> list[dict[str, Any]]:
    """5m bars, IST hour 10, N=4 X=1% true (high-low)/close = 1%."""
    days = (
        (2025, 6, 2), (2025, 6, 3), (2025, 6, 4),
        (2025, 7, 2), (2025, 7, 3), (2025, 7, 4),
    )
    bars: list[dict[str, Any]] = []
    for y, mo, d in days:
        for mm in (55, 0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50):
            hh = 9 if mm == 55 else 10
            o_ts = _ist_unix(y, mo, d, hh, mm)
            bars.append(
                {
                    "time": o_ts,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 100.0,
                    "close": 100.0,
                }
            )
    return bars


def test_t_filtctrl() -> None:
    bars = _range_bars()
    tf_sec = 300
    filt = "N4_X1"
    assert parse_filter(filt) == (4, 1.0)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    pool = filter_pass_close_ts(bars, filt, tf_sec)
    assert pool.size > 8
    close_to_j = {int(b["time"]) + tf_sec: i for i, b in enumerate(bars)}
    # F: one June, one July, both IST hour 10, on filter-true bars.
    f_june_ts = int(bars[5]["time"]) + tf_sec
    f_july_ts = int(bars[5 + 36]["time"]) + tf_sec
    assert ist_hod(f_june_ts) == 10
    assert ist_hod(f_july_ts) == 10
    assert ist_ym(f_june_ts) == 202506
    assert ist_ym(f_july_ts) == 202507
    extra_sig = int(bars[20]["time"]) + tf_sec  # June 3, not an F event
    signal_ts = np.array([f_june_ts, extra_sig, f_july_ts], dtype=np.int64)
    f_june = [{"signal_ts": f_june_ts, "event_id": "f_june", "hod": 10}]
    f_july = [{"signal_ts": f_july_ts, "event_id": "f_july", "hod": 10}]
    rng = np.random.default_rng(SEED)
    jun_lo = _ist_unix(2025, 6, 1, 0, 0)
    jun_hi = _ist_unix(2025, 7, 1, 0, 0)
    jul_lo = _ist_unix(2025, 7, 1, 0, 0)
    jul_hi = _ist_unix(2025, 8, 1, 0, 0)
    fc_j, miss_j = filtctrl_events(
        f_june, pool, signal_ts, rng, jun_lo, jun_hi, "5m", "R3", filt,
    )
    fc_u, miss_u = filtctrl_events(
        f_july, pool, signal_ts, rng, jul_lo, jul_hi, "5m", "R3", filt,
    )
    assert miss_j == 0 and miss_u == 0
    assert len(fc_j) == len(f_june) == 1
    assert len(fc_u) == len(f_july) == 1
    assert ist_ym(int(fc_j[0]["signal_ts"])) == 202506
    assert ist_ym(int(fc_u[0]["signal_ts"])) == 202507
    f_all = [f_june_ts, f_july_ts]
    sig_set = {int(x) for x in signal_ts.tolist()}
    for ev in fc_j + fc_u:
        ct = int(ev["signal_ts"])
        j = close_to_j[ct]
        assert passes_move_filter(h, l, c, j, filt) is True
        assert ct not in sig_set
        for ft in f_all:
            assert abs(ct - int(ft)) > 3600
        assert int(ev["hod"]) == 10
