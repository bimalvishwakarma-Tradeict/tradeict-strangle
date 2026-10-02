#!/usr/bin/env python3
"""S013 sanity: imported Supertrend is S012's; truncation look-ahead."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest.strategies.s012_trend_follow.engine as s012e  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import Bar1m, Candle  # noqa: E402
from backtest.strategies.s013_smith_basket.engine import (  # noqa: E402
    detect_smith,
    lookahead_ok,
    supertrend as s013_st,
)


def test_supertrend_is_s012() -> None:
    assert s013_st is s012e.supertrend
    h = np.array([10.5, 11.2, 12.5, 11.5, 10.8, 13.5, 14.2, 13.5], dtype=np.float64)
    l = np.array([9.5, 10.8, 11.5, 10.5, 9.5, 12.5, 13.5, 12.8], dtype=np.float64)
    c = np.array([9.5, 10.0, 12.0, 11.0, 10.0, 13.0, 14.0, 13.0], dtype=np.float64)
    t1, st1 = s012e.supertrend(h, l, c, 3, 2.0)
    t2, st2 = s013_st(h, l, c, 3, 2.0)
    assert np.array_equal(t1, t2)
    np.testing.assert_allclose(st1, st2, equal_nan=True)


def test_truncation_lookahead() -> None:
    n = 8
    ts0 = 1_735_689_600  # 2025-01-01 00:00 UTC Wednesday
    ohlc = [
        (100, 101, 99, 100.5),
        (100.5, 102, 100, 101.0),  # prev close >= lower; next breaks down
        (101.0, 101.2, 90, 91.0),  # close < lower, RSI forced < 30
        (91.0, 95, 90, 94.0),
        (94.0, 96, 93, 95.0),
        (95.0, 97, 94, 96.0),
        (96.0, 98, 95, 97.0),
        (97.0, 99, 96, 98.0),
    ]
    candles: list[Candle] = []
    for i, (o, h, l, cl) in enumerate(ohlc):
        t0 = ts0 + i * 300
        candles.append(Candle(t0, t0 + 240, o, h, l, cl))
    lower = np.array([95, 95, 96, 90, 90, 90, 90, 90], dtype=np.float64)
    upper = np.array([110, 110, 110, 110, 110, 110, 110, 110], dtype=np.float64)
    rsi = np.array([50, 50, 20, 40, 40, 40, 40, 40], dtype=np.float64)
    spot: dict[int, Bar1m] = {}
    for c in candles:
        for t in range(c.ts_open, c.ts_close_bar + 120, 60):
            spot[t] = Bar1m(t, c.open, c.high, c.low, c.close, 1.0)
    found = detect_smith(candles, lower, upper, rsi, spot, 0, None)
    assert found, "expected a long Smith signal"
    sig = found[0]
    assert sig.side == "long"
    assert lookahead_ok(candles, lower, upper, rsi, spot, 0, sig)
    trunc = detect_smith(
        candles[: sig.index + 1],
        lower[: sig.index + 1],
        upper[: sig.index + 1],
        rsi[: sig.index + 1],
        spot,
        0,
        None,
    )
    assert trunc, "truncation dropped the signal — look-ahead leak"
    assert trunc[-1].index == sig.index
    assert trunc[-1].entry_ts == sig.signal_ts + 60


def main() -> None:
    test_supertrend_is_s012()
    test_truncation_lookahead()
    print("s013 sanity: PASS")


if __name__ == "__main__":
    main()
