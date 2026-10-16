"""S023 OBPS signal filters: zone age, expiry, overlap, MATCH vs ANY, no look-ahead."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.strategies.s023_obps.signals import (  # noqa: E402
    MIN_ZONE_AGE,
    arm_allows,
    fresh_boxes,
    iter_obps_signals,
    overlapping_fresh_zones,
    range_overlap,
)

TF = "5m"
TF_SEC = 300


def _bar(i: int, o: float, h: float, l: float, c: float) -> dict[str, Any]:
    return {
        "time": 1_700_000_000 + i * TF_SEC,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
    }


def _flat(n: int, px: float = 100.0) -> list[dict[str, Any]]:
    return [_bar(i, px, px + 0.5, px - 0.5, px) for i in range(n)]


def _t_index(bars: list[dict[str, Any]]) -> dict[int, int]:
    return {int(b["time"]): i for i, b in enumerate(bars)}


def _fake_rsi(bars: list[dict[str, Any]], j: int, side: str) -> list[dict[str, Any]]:
    close_ts = int(bars[j]["time"]) + TF_SEC
    return [
        {
            "ts": close_ts,
            "side": side,
            "ob_level": float(bars[j]["high"]),
            "ob_rsi": 85.0,
            "sig_rsi": 60.0,
            "ob_candle_ts": int(bars[j]["time"]),
        }
    ]


def _ulp_high_zone_bars() -> list[dict[str, Any]]:
    """Pivot high at i=10, confirmed at i=20. Wick zone ~[110, 120]."""
    bars = _flat(36, 100.0)
    bars[10] = _bar(10, 110.0, 120.0, 109.0, 110.0)
    for i in range(11, 21):
        bars[i] = _bar(i, 100.0, 101.0, 99.0, 100.0)
    return bars


def test_t1_zone_age_9_reject_10_accept() -> None:
    bars = _ulp_high_zone_bars()
    t_index = _t_index(bars)
    j10 = 20
    boxes = fresh_boxes(bars[: j10 + 1], "wick")
    assert boxes, "expected live ULP high zone at confirm bar"
    bar = _bar(20, 112.0, 113.0, 111.0, 112.0)
    hits10 = overlapping_fresh_zones(bar, boxes, j10, t_index, min_age=MIN_ZONE_AGE)
    assert hits10, "age 10 must accept"
    hits9 = overlapping_fresh_zones(bar, boxes, j10, t_index, min_age=11)
    assert hits9 == []
    fake = {
        "t1": int(bars[11]["time"]),
        "top": 113.0,
        "bottom": 111.0,
        "kind": "high",
        "expired": False,
    }
    age9 = overlapping_fresh_zones(bar, [fake], j10, t_index, min_age=MIN_ZONE_AGE)
    assert int(j10 - t_index[int(fake["t1"])]) == 9
    assert age9 == []


def test_t2_expired_zone_rejects() -> None:
    bars = _ulp_high_zone_bars()
    bars[21] = _bar(21, 100.0, 125.0, 99.0, 100.0)
    boxes = fresh_boxes(bars[:22], "wick")
    kinds = {str(z.get("kind")) for z in boxes}
    assert "high" not in kinds
    t_index = _t_index(bars)
    dead = {
        "t1": int(bars[10]["time"]),
        "top": 120.0,
        "bottom": 110.0,
        "kind": "high",
        "expired": True,
    }
    bar = _bar(22, 112.0, 113.0, 111.0, 112.0)
    live = [z for z in boxes if not z.get("expired")]
    hits = overlapping_fresh_zones(bar, live, 22, t_index)
    assert hits == []
    assert range_overlap(111.0, 113.0, 110.0, 120.0)
    assert dead.get("expired") is True


def test_t3_no_overlap_rejects() -> None:
    bars = _ulp_high_zone_bars()
    t_index = _t_index(bars)
    boxes = fresh_boxes(bars[:21], "wick")
    bar = _bar(20, 50.0, 51.0, 49.0, 50.0)
    hits = overlapping_fresh_zones(bar, boxes, 20, t_index)
    assert hits == []


def test_t4_match_high_long_reject_any_accept() -> None:
    hits = [{"kind": "high", "top": 120.0, "bottom": 110.0, "age_bars": 12}]
    assert arm_allows("ANY", "long", hits) == hits
    assert arm_allows("MATCH", "long", hits) == []
    assert arm_allows("MATCH", "short", hits) == hits


def test_t5_lookahead_future_bars_do_not_change_j() -> None:
    bars = _ulp_high_zone_bars()
    bars[20] = _bar(20, 112.0, 113.0, 111.0, 112.0)
    j = 20

    def fake_detect(*_a: Any, **_k: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _fake_rsi(bars, j, "short"), []

    with patch("backtest.strategies.s023_obps.signals.detect_rsi_div", fake_detect):
        a = iter_obps_signals(bars, TF, "wick", "ANY")
        mutated = [dict(b) for b in bars]
        for k in range(j + 1, len(mutated)):
            mutated[k] = _bar(k, 9.0, 999.0, 1.0, 9.0)
        b = iter_obps_signals(mutated, TF, "wick", "ANY")
    close_j = int(bars[j]["time"]) + TF_SEC
    sa = [s for s in a if int(s["signal_ts"]) == close_j]
    sb = [s for s in b if int(s["signal_ts"]) == close_j]
    assert sa, "expected signal at confirm bar"
    assert [(s["signal_ts"], s["side"], s["age_bars"], s["zone_kind"]) for s in sa] == [
        (s["signal_ts"], s["side"], s["age_bars"], s["zone_kind"]) for s in sb
    ]
    truncated = bars[: j + 1]
    with patch("backtest.strategies.s023_obps.signals.detect_rsi_div", fake_detect):
        c = iter_obps_signals(truncated, TF, "wick", "ANY")
    sc = [s for s in c if int(s["signal_ts"]) == close_j]
    assert len(sc) == len(sa)


def test_rsi_params_accepted() -> None:
    from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div

    ts = np.arange(40, dtype=np.int64) * 300
    o = np.full(40, 100.0)
    h = np.full(40, 101.0)
    l = np.full(40, 99.0)
    c = np.linspace(100.0, 80.0, 40)
    sigs, _ = detect_rsi_div(ts, o, h, l, c, 300, n=3, ob=80, os=20, exp_obh=50, exp_obl=50)
    assert isinstance(sigs, list)
