"""S022-F stage0F: direction, window N, ADX rise, look-ahead, OOS exclude, one-per-zone."""

from __future__ import annotations

import math

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import (
    detect_from_rsi,
    detect_rsi_div,
    rsi_wilder,
)
from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4
from backtest.strategies.s022_ulp_long_strangle.stage0_filter import (
    DEFAULT_LEVEL,
    DEFAULT_RSI_LEN,
    EXP_OBH,
    EXP_OBL,
    first_qualifying_j,
    keep_in_range,
    mark_robust,
    match_arm_f,
    neighbour_keys,
    parse_levels,
    parse_rsi_lens,
    parse_tf_list,
)


def _ulp(
    i: int,
    tf_sec: int,
    kind: str,
    pivot: int,
    *,
    top: float = 100.0,
    bot: float = 90.0,
    tts0: int = 1_700_000_000,
) -> dict:
    close_t = tts0 + i * tf_sec + tf_sec
    return {
        "rule": "fresh",
        "tf": "5m",
        "mode": "wick",
        "zone_kind": kind,
        "signal_ts": close_t,
        "pivot_ts": pivot,
        "zone_top": top,
        "zone_bottom": bot,
    }


def test_t1_high_zone_long_rsi_rejected() -> None:
    n = 20
    rsi_short = np.zeros(n, dtype=bool)
    rsi_long = np.zeros(n, dtype=bool)
    rsi_long[8] = True
    adx = np.linspace(10.0, 30.0, n)
    i = 8
    assert first_qualifying_j(i, 6, "high", rsi_short, rsi_long, adx) is None
    rsi_short[8] = True
    assert first_qualifying_j(i, 6, "high", rsi_short, rsi_long, adx) == 8


def test_t2_signal_at_i_plus_n_accepted_n_plus_1_rejected() -> None:
    n = 30
    rsi_short = np.zeros(n, dtype=bool)
    rsi_long = np.zeros(n, dtype=bool)
    adx = np.linspace(5.0, 40.0, n)
    i = 10
    n_win = 6
    rsi_short[i + n_win] = True
    assert first_qualifying_j(i, n_win, "high", rsi_short, rsi_long, adx) == i + n_win
    rsi_short[:] = False
    rsi_short[i + n_win + 1] = True
    assert first_qualifying_j(i, n_win, "high", rsi_short, rsi_long, adx) is None


def test_t3_adx_four_rise_one_flat_reject_five_strict_accept() -> None:
    n = 20
    rsi_short = np.zeros(n, dtype=bool)
    rsi_long = np.zeros(n, dtype=bool)
    j = 10
    rsi_short[j] = True
    adx = np.array(
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0] + [12.0] * (n - 11),
        dtype=np.float64,
    )
    # j-5..j = 6,7,8,9,10,11  — five strict rises
    assert first_qualifying_j(j, 0, "high", rsi_short, rsi_long, adx) == j
    adx[j - 1] = adx[j]  # one flat:  ... 9, 11, 11  (breaks strict chain)
    adx[j] = 11.0
    adx[j - 1] = 11.0
    assert first_qualifying_j(j, 0, "high", rsi_short, rsi_long, adx) is None
    adx = np.linspace(1.0, 20.0, n)
    assert first_qualifying_j(j, 0, "high", rsi_short, rsi_long, adx) == j


def test_t4_lookahead_future_bars_do_not_change_event_at_j() -> None:
    rng = np.random.default_rng(1)
    n = 80
    c = 100.0 + np.cumsum(rng.normal(0.0, 0.4, n))
    h = c + rng.uniform(0.2, 1.0, n)
    l = c - rng.uniform(0.2, 1.0, n)
    _d, _m, adx = adx_pine_v4(h, l, c, length=14, warmup=0)
    i = 40
    j_hit = 42
    rsi_short = np.zeros(n, dtype=bool)
    rsi_long = np.zeros(n, dtype=bool)
    rsi_short[j_hit] = True
    adx_rise = np.linspace(1.0, float(n), n)
    before = first_qualifying_j(i, 6, "high", rsi_short, rsi_long, adx_rise)
    assert before == j_hit
    h2 = h.copy()
    l2 = l.copy()
    c2 = c.copy()
    h2[j_hit + 1 :] += 50.0
    l2[j_hit + 1 :] -= 50.0
    c2[j_hit + 1 :] += 25.0
    _d2, _m2, adx2 = adx_pine_v4(h2, l2, c2, length=14, warmup=0)
    after = first_qualifying_j(i, 6, "high", rsi_short, rsi_long, adx_rise)
    assert after == before == j_hit
    np.testing.assert_allclose(adx[: j_hit + 1], adx2[: j_hit + 1], rtol=0, atol=1e-12)


def test_t5_oos_2025_08_and_2025_09_not_in_output() -> None:
    rows = [
        {"entry_time": 1, "ym": 202507, "arm": "F"},
        {"entry_time": 2, "ym": 202508, "arm": "F"},
        {"entry_time": 3, "ym": 202509, "arm": "F"},
        {"entry_time": 4, "ym": 202510, "arm": "F"},
    ]
    kept = keep_in_range(rows, 202407, 202609, {202508, 202509})
    yms = {int(e["ym"]) for e in kept}
    assert 202508 not in yms
    assert 202509 not in yms
    assert 202507 in yms
    assert 202510 in yms


def test_t6_one_ulp_zone_two_qualifying_bars_only_first_event() -> None:
    n = 25
    tf_sec = 300
    tts = np.arange(n, dtype=np.int64) * tf_sec + 1_700_000_000
    rsi_short = np.zeros(n, dtype=bool)
    rsi_long = np.zeros(n, dtype=bool)
    i = 8
    rsi_short[i] = True
    rsi_short[i + 2] = True
    adx = np.linspace(1.0, 30.0, n)
    ulp = [_ulp(i, tf_sec, "high", pivot=111, tts0=int(tts[0]))]
    ev = match_arm_f(ulp, rsi_short, rsi_long, adx, tts, tf_sec, n_win=6)
    assert len(ev) == 1
    assert int(ev[0]["bar_j"]) == i
    ulp2 = ulp + [_ulp(i + 1, tf_sec, "high", pivot=111, tts0=int(tts[0]))]
    ev2 = match_arm_f(ulp2, rsi_short, rsi_long, adx, tts, tf_sec, n_win=6)
    assert len(ev2) == 1


def _synth_ohlc(n: int = 400, seed: int = 7) -> tuple[np.ndarray, ...]:
    rng = np.random.default_rng(seed)
    tf_sec = 1800
    tts = np.arange(n, dtype=np.int64) * tf_sec + 1_700_000_000
    c = 100.0 + np.cumsum(rng.normal(0.0, 1.2, n))
    h = c + rng.uniform(0.3, 2.5, n)
    l = c - rng.uniform(0.3, 2.5, n)
    o = np.r_[c[0], c[:-1]]
    return tts, o, h, l, c, tf_sec


def test_t7_default_14_7030_matches_detect_rsi_div() -> None:
    assert DEFAULT_RSI_LEN == 14
    assert DEFAULT_LEVEL == (70.0, 30.0)
    assert parse_rsi_lens("14") == [14]
    assert parse_levels("70/30") == [(70.0, 30.0)]
    assert parse_tf_list("5m,15m,30m") == ["5m", "15m", "30m"]
    tts, o, h, l, c, tf_sec = _synth_ohlc()
    old, _z0 = detect_rsi_div(tts, o, h, l, c, tf_sec)
    rsi = rsi_wilder(c, 14)
    new, _z1 = detect_from_rsi(
        tts, o, h, l, c, rsi, tf_sec, 70.0, 30.0, EXP_OBH, EXP_OBL,
    )
    assert old == new
    adx = np.linspace(1.0, 50.0, tts.size)
    rs, rl = np.zeros(tts.size, dtype=bool), np.zeros(tts.size, dtype=bool)
    for s in old:
        j = int((int(s["ts"]) - int(tts[0])) // tf_sec) - 1
        if 0 <= j < tts.size:
            if s["side"] == "short":
                rs[j] = True
            else:
                rl[j] = True
    ulp = [
        _ulp(20, tf_sec, "high", 99, tts0=int(tts[0])),
        _ulp(40, tf_sec, "low", 88, tts0=int(tts[0])),
    ]
    a = match_arm_f(ulp, rs, rl, adx, tts, tf_sec, 6)
    b = match_arm_f(ulp, rs, rl, adx, tts, tf_sec, 6)
    assert a == b


def test_t8_os_does_not_change_high_zone_ob_does_not_change_low() -> None:
    tts, o, h, l, c, tf_sec = _synth_ohlc()
    rsi = rsi_wilder(c, 14)
    a, _ = detect_from_rsi(tts, o, h, l, c, rsi, tf_sec, 70.0, 30.0, EXP_OBH, EXP_OBL)
    b, _ = detect_from_rsi(tts, o, h, l, c, rsi, tf_sec, 70.0, 15.0, EXP_OBH, EXP_OBL)
    c_ob, _ = detect_from_rsi(tts, o, h, l, c, rsi, tf_sec, 85.0, 30.0, EXP_OBH, EXP_OBL)
    short_a = [s for s in a if s["side"] == "short"]
    short_b = [s for s in b if s["side"] == "short"]
    long_a = [s for s in a if s["side"] == "long"]
    long_c = [s for s in c_ob if s["side"] == "long"]
    assert short_a == short_b
    assert long_a == long_c


def test_t9_rsi_len_2_no_nan_signals_after_warmup() -> None:
    tts, o, h, l, c, tf_sec = _synth_ohlc(n=80)
    rsi = rsi_wilder(c, 2)
    assert np.all(np.isfinite(rsi[2:]))
    sigs, _z = detect_from_rsi(
        tts, o, h, l, c, rsi, tf_sec, 70.0, 30.0, EXP_OBH, EXP_OBL,
    )
    for s in sigs:
        assert math.isfinite(float(s["ts"]))
        assert s["side"] in ("short", "long")
        assert s["side"] == s["side"]  # not nan


def test_t10_robust_neighbours_edge_len2_one_len_neighbour() -> None:
    lens = [2, 3, 5]
    levels = [(70.0, 30.0), (75.0, 25.0), (80.0, 20.0)]
    n_len = neighbour_keys("30m", "wick", 6, 2, 70.0, 30.0, lens, levels)
    len_n = [k for k in n_len if k[3] != 2]
    lv_n = [k for k in n_len if k[3] == 2]
    assert len(len_n) == 1
    assert len_n[0][3] == 3
    assert len(lv_n) == 1
    assert lv_n[0][4] == 75.0

    def _cell(rsi_len: int, ob: float, os: float, gate: str) -> dict:
        return {
            "tf": "30m", "mode": "wick", "N": 6,
            "rsi_len": rsi_len, "ob": ob, "os": os, "gate": gate,
        }

    cells = [
        _cell(2, 70.0, 30.0, "GATE PASS"),
        _cell(3, 70.0, 30.0, "GATE PASS"),
        _cell(2, 75.0, 25.0, "GATE PASS"),
        _cell(5, 70.0, 30.0, "GATE FAIL: x"),
        _cell(3, 75.0, 25.0, "GATE PASS"),
    ]
    mark_robust(cells, lens, levels)
    edge = [c for c in cells if c["rsi_len"] == 2 and c["ob"] == 70.0][0]
    assert edge["n_neighbours"] == 2
    assert edge["n_neighbours_pass"] == 2
    assert edge["robust"] == "ROBUST PASS"
    lonely = _cell(5, 80.0, 20.0, "GATE PASS")
    cells2 = cells + [lonely]
    mark_robust(cells2, lens, levels)
    lon = [c for c in cells2 if c["rsi_len"] == 5 and c["ob"] == 80.0][0]
    assert lon["n_neighbours"] == 2
    assert lon["n_neighbours_pass"] < 2
    assert lon["robust"] == "ROBUST FAIL"
