"""S022-F stage0F: direction, window N, ADX rise, look-ahead, OOS exclude, one-per-zone."""

from __future__ import annotations

import numpy as np

from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4
from backtest.strategies.s022_ulp_long_strangle.stage0_filter import (
    first_qualifying_j,
    keep_in_range,
    match_arm_f,
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
