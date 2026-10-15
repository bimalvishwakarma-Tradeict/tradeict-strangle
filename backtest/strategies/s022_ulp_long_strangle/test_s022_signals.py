"""S022 ULP signal parity with chart compute_ulp and no look-ahead."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.signals import (  # noqa: E402
    boxes_to_keys,
    compute_ulp,
    iter_ulp_signals,
    run_ulp,
    tf_seconds,
    ulp_params,
    zone_key,
)


def _bar(i: int, o: float, h: float, l: float, c: float, tf_sec: int = 300) -> dict[str, Any]:
    return {
        "time": 1_700_000_000 + i * tf_sec,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
    }


def _arrays_to_bars(
    tts: np.ndarray,
    to_: np.ndarray,
    th: np.ndarray,
    tl: np.ndarray,
    tc: np.ndarray,
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


def _sig_tuple(s: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(s["signal_ts"]),
        str(s["rule"]),
        str(s["zone_kind"]),
        int(s["pivot_ts"]),
        float(s["zone_top"]),
        float(s["zone_bottom"]),
        int(s["age_bars"]),
    )


@pytest.fixture(scope="module")
def june_15m_bars() -> list[dict[str, Any]]:
    csv_path = Path(s018.SPOT_CSV)
    if not csv_path.is_file():
        pytest.skip(f"missing spot csv {csv_path}")
    spot = s018.load_spot_1m(csv_path)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    win_from, win_to = d.month_bounds("2025-06")
    from backtest.harness.data import ist_dt, to_unix
    from datetime import timedelta

    start_ts = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    lo = start_ts - 3 * 86400
    hi = cutoff + 1 * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    tf_sec = int(d.LINE_TF["15m"])
    tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
    bars = _arrays_to_bars(tts, to_, th, tl, tc)
    assert len(bars) >= 200
    return bars[:1200]


def test_chart_parity_no_lookahead_random_cuts(june_15m_bars: list[dict[str, Any]]) -> None:
    bars = june_15m_bars
    n = len(bars)
    rng = np.random.default_rng(20251015)
    ks = sorted({int(x) for x in rng.integers(0, n, size=80)})
    while len(ks) < 50:
        ks.append(int(rng.integers(0, n)))
        ks = sorted(set(ks))
    ks = ks[: max(50, min(len(ks), n))]
    assert len(ks) >= 50
    for mode in ("wick", "full"):
        params = ulp_params(mode)
        for k in ks:
            prefix = bars[: k + 1]
            _sigs, remaining = run_ulp(prefix, mode, "15m")
            chart = compute_ulp(prefix, params)
            got = {zone_key(z) for z in remaining}
            want = boxes_to_keys(chart["boxes"])
            assert got == want, f"mode={mode} k={k} extra={got - want} missing={want - got}"


def test_truncation_no_lookahead(june_15m_bars: list[dict[str, Any]]) -> None:
    bars = june_15m_bars
    tf = "15m"
    tf_sec = tf_seconds(tf)
    rng = np.random.default_rng(7)
    cuts = sorted({int(x) for x in rng.integers(40, len(bars) - 1, size=12)})
    for mode in ("wick", "full"):
        full_sigs = iter_ulp_signals(bars, mode, tf)
        for k in cuts:
            prefix = bars[: k + 1]
            close_k = int(bars[k]["time"]) + tf_sec
            from_prefix = [_sig_tuple(s) for s in iter_ulp_signals(prefix, mode, tf)]
            from_full = [
                _sig_tuple(s) for s in full_sigs if int(s["signal_ts"]) <= close_k
            ]
            assert from_prefix == from_full, f"mode={mode} k={k}"


def _inside_high_series() -> list[dict[str, Any]]:
    """Pivot high at 10, sit inside until age 20, leave, re-enter (wick zone)."""
    bars: list[dict[str, Any]] = []
    for i in range(45):
        o, h, l, c = 102.0, 108.0, 100.0, 103.0
        if i == 10:
            o, h, l, c = 105.0, 120.0, 100.0, 106.0
        elif 20 <= i <= 30:
            o, h, l, c = 108.0, 110.0, 101.0, 108.0
        elif i == 31:
            o, h, l, c = 100.0, 90.0, 80.0, 99.0
        elif i == 32:
            o, h, l, c = 108.0, 110.0, 100.0, 109.0
        bars.append(_bar(i, o, h, l, c, tf_sec=300))
    return bars


def test_fresh_vs_any_sit_then_reenter() -> None:
    bars = _inside_high_series()
    sigs = iter_ulp_signals(bars, "wick", "5m")
    highs_any = [s for s in sigs if s["rule"] == "any" and s["zone_kind"] == "high"]
    highs_fresh = [s for s in sigs if s["rule"] == "fresh" and s["zone_kind"] == "high"]
    assert highs_any, "expected any-touch on high zone"
    first_any = highs_any[0]
    assert first_any["age_bars"] == 20
    assert abs(float(first_any["zone_top"]) - 120.0) < 1e-9
    assert abs(float(first_any["zone_bottom"]) - 106.0) < 1e-9
    any_ts = int(first_any["signal_ts"])
    fresh_at_any = [s for s in highs_fresh if int(s["signal_ts"]) == any_ts]
    assert fresh_at_any == [], "sitting inside at age 20 must not be fresh"
    assert highs_fresh, "re-entry should fire fresh"
    first_fresh = highs_fresh[0]
    assert int(first_fresh["signal_ts"]) > any_ts
    assert int(first_fresh["pivot_ts"]) == int(first_any["pivot_ts"])
    assert first_fresh["age_bars"] > 20
