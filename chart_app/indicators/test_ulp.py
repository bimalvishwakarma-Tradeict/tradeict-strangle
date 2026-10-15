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
    tops2 = [
        round(b["top"], 2)
        for b in out2["boxes"]
        if b["kind"] == "high" and not b.get("expired")
    ]
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


def _rgba_alpha(s: str) -> float:
    inner = s[s.find("(") + 1 : s.rfind(")")]
    return float(inner.split(",")[3])


def test_t_exp1_show_expired_false_matches_legacy() -> None:
    bars = []
    for i in range(25):
        h, l_ = 100.0, 90.0
        if i == 10:
            h = 120.0
        bars.append(_bar(i, 95.0, h, l_, 96.0))
    bars.append(_bar(25, 119.0, 121.0, 118.0, 120.0))
    params = {"lookback": 10, "maxLevels": 100, "showLabels": True, "showExpired": False}
    out = compute_ulp(bars, params)
    assert out == compute_ulp(bars, dict(params))
    assert not any(b.get("expired") for b in out["boxes"])
    assert all(b.get("extend_right") is True for b in out["boxes"])
    last_t = int(bars[-1]["time"])
    assert all(int(b["t2"]) == last_t for b in out["boxes"])
    assert all(not str(b["id"]).endswith("-exp") for b in out["boxes"])
    on = compute_ulp(bars, {**params, "showExpired": True})
    fresh = [b for b in on["boxes"] if not b.get("expired")]
    assert fresh == out["boxes"]
    assert on["labels"] == out["labels"]
    assert 120.0 not in [round(b["top"], 2) for b in out["boxes"] if b["kind"] == "high"]


def test_t_exp2_expired_box_t2_is_touch_bar() -> None:
    bars = []
    for i in range(25):
        h, l_ = 100.0, 90.0
        if i == 10:
            h = 120.0
        bars.append(_bar(i, 95.0, h, l_, 96.0))
    touch = _bar(25, 119.0, 121.0, 118.0, 120.0)
    bars.append(touch)
    out = compute_ulp(bars, {"lookback": 10, "showExpired": True})
    exp = [b for b in out["boxes"] if b.get("expired") and b["kind"] == "high" and abs(b["top"] - 120.0) < 1e-9]
    assert len(exp) == 1
    z = exp[0]
    assert int(z["t2"]) == int(touch["time"])
    assert z.get("expired") is True
    assert z.get("extend_right") is False
    assert int(z["t1"]) == int(bars[10]["time"])
    active = [b for b in out["boxes"] if not b.get("expired") and b["kind"] == "high"]
    assert 120.0 not in [round(b["top"], 2) for b in active]


def test_t_exp3_expired_alpha_is_40pct_of_fresh() -> None:
    bars = []
    for i in range(25):
        h, l_ = 100.0, 90.0
        if i == 10:
            h = 120.0
        bars.append(_bar(i, 95.0, h, l_, 96.0))
    live = compute_ulp(bars[:21], {"lookback": 10, "showExpired": True})
    fresh = next(b for b in live["boxes"] if b["kind"] == "high" and abs(b["top"] - 120.0) < 1e-9)
    bars.append(_bar(25, 119.0, 121.0, 118.0, 120.0))
    out = compute_ulp(bars, {"lookback": 10, "showExpired": True})
    exp = next(b for b in out["boxes"] if b.get("expired") and abs(b["top"] - 120.0) < 1e-9)
    fa = _rgba_alpha(str(fresh["fill"]))
    ea = _rgba_alpha(str(exp["fill"]))
    ba = _rgba_alpha(str(fresh["border"]))
    be = _rgba_alpha(str(exp["border"]))
    assert abs(ea - fa * 0.40) < 1e-9
    assert abs(be - ba * 0.40) < 1e-9


def test_t_exp4_max_expired_keeps_latest_n() -> None:
    bars = []
    n_touch = 8
    n = 0
    for k in range(n_touch):
        for i in range(21):
            # rising lows: no pivot-low, so expired list is high-zone touches only
            l_ = 50.0 + n * 0.01
            h = 100.0 + n * 0.01
            if i == 10:
                h = 200.0 + k * 10.0
            bars.append(_bar(n, 95.0, h, l_, 96.0))
            n += 1
        h_touch = 201.0 + k * 10.0
        l_touch = 50.0 + n * 0.01
        bars.append(_bar(n, 199.0 + k * 10, h_touch, l_touch, 200.0))
        n += 1
    params = {"lookback": 10, "showExpired": True}
    all_exp = [b for b in compute_ulp(bars, {**params, "maxExpired": 500})["boxes"] if b.get("expired")]
    assert len(all_exp) >= 5
    capped = [b for b in compute_ulp(bars, {**params, "maxExpired": 5})["boxes"] if b.get("expired")]
    assert len(capped) == 5
    assert capped == all_exp[-5:]
    assert all(b.get("extend_right") is False for b in capped)
    t2s = [int(b["t2"]) for b in capped]
    assert t2s == sorted(t2s)
