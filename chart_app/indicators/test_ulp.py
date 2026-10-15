"""ULP pivot / order unit tests (no live API)."""

from __future__ import annotations

from indicators.ulp import compute_ulp, is_pivot_high, is_pivot_low


def _bar(i: int, o: float, h: float, l: float, c: float) -> dict:
    return {"time": 1_000_000 + i * 60, "open": o, "high": h, "low": l, "close": c}


def test_pivot_high_strict_center() -> None:
    # 21 bars, peak at index 10
    highs = [10.0] * 21
    highs[10] = 20.0
    assert is_pivot_high(highs, 20, 10, 10)
    assert not is_pivot_high(highs, 19, 10, 10)


def test_pivot_high_tie_still_window_max() -> None:
    highs = [10.0] * 21
    highs[10] = 20.0
    highs[11] = 20.0  # equal on the right
    assert is_pivot_high(highs, 20, 10, 10)


def test_pivot_low_center() -> None:
    lows = [20.0] * 21
    lows[10] = 5.0
    assert is_pivot_low(lows, 20, 10, 10)


def test_add_then_touch_removes() -> None:
    bars = []
    for i in range(25):
        h, l_ = 100.0, 90.0
        if i == 10:
            h = 120.0
        bars.append(_bar(i, 95.0, h, l_, 96.0))
    # confirmation at i=20; bar 20 high is 100 < 120 so zone lives
    out = compute_ulp(bars[:21], {"lookback": 10, "maxLevels": 100, "showLabels": True})
    tops = [round(b["top"], 2) for b in out["boxes"] if b["kind"] == "high"]
    assert 120.0 in tops
    # later bar trades through the top
    bars.append(_bar(25, 119.0, 121.0, 118.0, 120.0))
    out2 = compute_ulp(bars, {"lookback": 10})
    tops2 = [round(b["top"], 2) for b in out2["boxes"] if b["kind"] == "high"]
    assert 120.0 not in tops2


def test_duplicate_near_mintick() -> None:
    bars = []
    for i in range(45):
        h = 100.0
        if i == 10:
            h = 130.0
        if i == 30:
            h = 129.5  # |129.5-130| < 5.0 dup eps; high stays below existing top so no touch
        bars.append(_bar(i, 99.0, h, 90.0, 99.5))
    out = compute_ulp(bars, {"lookback": 10})
    htops = [b["top"] for b in out["boxes"] if b["kind"] == "high"]
    assert len(htops) == 1
    assert abs(htops[0] - 130.0) < 1e-9


def _pivot_high_bars() -> list[dict]:
    bars = []
    for i in range(21):
        h, l_ = 100.0, 90.0
        o, c = 95.0, 96.0
        if i == 10:
            h = 120.0
        bars.append(_bar(i, o, h, l_, c))
    return bars


def _pivot_low_bars() -> list[dict]:
    bars = []
    for i in range(21):
        h, l_ = 100.0, 90.0
        o, c = 96.0, 95.0
        if i == 10:
            l_ = 70.0
        bars.append(_bar(i, o, h, l_, c))
    return bars


def test_full_range_high_zone_is_pivot_high_to_low() -> None:
    bars = _pivot_high_bars()
    pb = bars[10]
    out = compute_ulp(bars, {"lookback": 10, "fullRange": True})
    highs = [b for b in out["boxes"] if b["kind"] == "high"]
    assert highs
    z = highs[0]
    assert abs(float(z["top"]) - float(pb["high"])) < 1e-9
    assert abs(float(z["bottom"]) - float(pb["low"])) < 1e-9


def test_full_range_low_zone_is_pivot_low_to_high() -> None:
    bars = _pivot_low_bars()
    pb = bars[10]
    out = compute_ulp(bars, {"lookback": 10, "fullRange": True})
    lows = [b for b in out["boxes"] if b["kind"] == "low"]
    assert lows
    z = lows[0]
    assert abs(float(z["bottom"]) - float(pb["low"])) < 1e-9
    assert abs(float(z["top"]) - float(pb["high"])) < 1e-9


def test_full_range_default_matches_body_edge() -> None:
    bars = []
    for i in range(45):
        h, l_ = 100.0, 90.0
        o, c = 95.0, 96.0
        if i == 10:
            h = 120.0
        if i == 30:
            l_ = 70.0
        bars.append(_bar(i, o, h, l_, c))
    omitted = compute_ulp(bars, {"lookback": 10})
    explicit = compute_ulp(bars, {"lookback": 10, "fullRange": False})
    assert omitted["boxes"] == explicit["boxes"]
    by_time = {int(x["time"]): x for x in bars}
    for z in omitted["boxes"]:
        pb = by_time[int(z["t1"])]
        if z["kind"] == "high":
            assert abs(float(z["top"]) - float(pb["high"])) < 1e-9
            assert abs(float(z["bottom"]) - max(float(pb["open"]), float(pb["close"]))) < 1e-9
        else:
            assert abs(float(z["bottom"]) - float(pb["low"])) < 1e-9
            assert abs(float(z["top"]) - min(float(pb["open"]), float(pb["close"]))) < 1e-9
    kinds = {z["kind"] for z in omitted["boxes"]}
    assert "high" in kinds and "low" in kinds
