"""Pine v4 ADX port: hand values, SMA window, warmup, rising, no look-ahead."""

from __future__ import annotations

import numpy as np

from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4, adx_rising


def test_adx_pine_hand_values_first_three_bars() -> None:
    h = np.array([101.0, 103.0, 102.0])
    l = np.array([99.0, 100.0, 98.0])
    c = np.array([100.0, 102.0, 99.0])
    dip, dim, _adx, dbg = adx_pine_v4(h, l, c, length=14, warmup=0, return_debug=True)
    assert abs(float(dbg["s_tr"][0]) - 101.0) < 1e-9
    assert abs(float(dip[0]) - 100.0) < 1e-9
    assert abs(float(dim[0]) - 0.0) < 1e-9
    assert abs(float(dbg["dx"][0]) - 100.0) < 1e-9
    assert abs(float(dbg["s_tr"][1]) - 96.78571428571429) < 1e-9
    assert abs(float(dip[1]) - 98.96678966789668) < 1e-9
    assert abs(float(dim[1]) - 0.0) < 1e-9
    assert abs(float(dbg["dx"][1]) - 100.0) < 1e-9
    assert abs(float(dbg["s_tr"][2]) - 93.87244897959184) < 1e-9
    assert abs(float(dip[2]) - 94.74971465840537) < 1e-9
    assert abs(float(dim[2]) - 2.1305505734007286) < 1e-9
    assert abs(float(dbg["dx"][2]) - 95.60168302945303) < 1e-9


def test_adx_nan_before_13_then_sma_of_dx() -> None:
    rng = np.random.default_rng(0)
    n = 40
    c = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    h = c + rng.uniform(0.5, 2.0, n)
    l = c - rng.uniform(0.5, 2.0, n)
    _dip, _dim, adx, dbg = adx_pine_v4(h, l, c, length=14, warmup=0, return_debug=True)
    for i in range(13):
        assert not np.isfinite(adx[i])
    assert np.isfinite(adx[13])
    expect = float(np.mean(dbg["dx"][0:14]))
    assert abs(float(adx[13]) - expect) < 1e-9


def test_warmup_500_nan_prefix() -> None:
    rng = np.random.default_rng(1)
    n = 600
    c = 100.0 + np.cumsum(rng.normal(0.0, 0.5, n))
    h = c + 1.0
    l = c - 1.0
    _dip, _dim, adx = adx_pine_v4(h, l, c, length=14, warmup=500)
    assert np.all(~np.isfinite(adx[:500]))
    assert np.any(np.isfinite(adx[500:]))


def test_adx_rising_strict_five_then_flat_and_nan() -> None:
    a = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0], dtype=np.float64)
    assert adx_rising(a, 5, k=5) is True
    a2 = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 5.0, 6.0], dtype=np.float64)
    assert adx_rising(a2, 5, k=5) is False
    a3 = np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0], dtype=np.float64)
    assert adx_rising(a3, 5, k=5) is False


def test_adx_no_lookahead() -> None:
    rng = np.random.default_rng(2)
    n = 80
    c = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    h = c + rng.uniform(0.4, 1.5, n)
    l = c - rng.uniform(0.4, 1.5, n)
    j = 40
    full_p, full_m, full_a = adx_pine_v4(h, l, c, length=14, warmup=0)
    h2 = h.copy()
    l2 = l.copy()
    c2 = c.copy()
    h2[j + 1 :] += 50.0
    l2[j + 1 :] += 40.0
    c2[j + 1 :] += 45.0
    p2, m2, a2 = adx_pine_v4(h2, l2, c2, length=14, warmup=0)
    np.testing.assert_allclose(full_p[: j + 1], p2[: j + 1], atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(full_m[: j + 1], m2[: j + 1], atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(full_a[: j + 1], a2[: j + 1], atol=1e-12, equal_nan=True)
    trunc_p, trunc_m, trunc_a = adx_pine_v4(h[: j + 1], l[: j + 1], c[: j + 1], length=14, warmup=0)
    np.testing.assert_allclose(full_p[: j + 1], trunc_p, atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(full_m[: j + 1], trunc_m, atol=1e-12, equal_nan=True)
    np.testing.assert_allclose(full_a[: j + 1], trunc_a, atol=1e-12, equal_nan=True)
