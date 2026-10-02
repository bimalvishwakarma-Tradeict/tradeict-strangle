#!/usr/bin/env python3
"""Hand-checked Supertrend (TV RMA) + truncation look-ahead test."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    Bar1m,
    Candle,
    detect_signals,
    lookahead_ok,
    rma,
    supertrend,
)


def test_rma_hand() -> None:
    # TR series from the 8-bar fixture below (see comments).
    tr = np.array([1.0, 1.7, 2.5, 1.5, 1.5, 3.5, 1.2, 1.2], dtype=np.float64)
    got = rma(tr, 3)
    assert np.isnan(got[0]) and np.isnan(got[1])
    assert abs(got[2] - 1.7333333333) < 1e-8
    assert abs(got[3] - 1.6555555555) < 1e-8
    assert abs(got[4] - 1.6037037037) < 1e-8
    assert abs(got[5] - 2.2358024691) < 1e-8
    assert abs(got[6] - 1.8905349794) < 1e-8
    assert abs(got[7] - 1.6603566529) < 1e-8


def test_supertrend_hand_flip() -> None:
    """Length-3, mult=2. First valid bar i=2 is UP; a sell-off flips DOWN at i=11.

    Bars 0–7 match the RMA fixture. Bars 8–12 are a controlled dump so close
    crosses below the prior final-upper band (TradingView flip-on-close).
    """
    h = np.array(
        [10.5, 11.2, 12.5, 11.5, 10.8, 13.5, 14.2, 13.5, 12.0, 10.5, 9.0, 8.0, 8.5],
        dtype=np.float64,
    )
    l = np.array(
        [9.5, 10.8, 11.5, 10.5, 9.5, 12.5, 13.5, 12.8, 10.0, 8.5, 7.5, 6.5, 7.0],
        dtype=np.float64,
    )
    c = np.array(
        [9.5, 10.0, 12.0, 11.0, 10.0, 13.0, 14.0, 13.0, 10.2, 8.8, 7.8, 7.0, 8.2],
        dtype=np.float64,
    )
    trend, st = supertrend(h, l, c, 3, 2.0)
    assert int(trend[2]) == 1
    assert abs(float(st[2]) - 8.5333333333) < 1e-6
    assert int(trend[3]) == 1
    assert abs(float(st[3]) - 8.5333333333) < 1e-6
    # Hand: at i=11 close=7.0 is below prior final-up; trend flips to -1.
    flipped = [i for i in range(1, len(trend)) if int(trend[i]) == -1 and int(trend[i - 1]) == 1]
    assert flipped, "expected a DOWN flip on the dump"
    assert flipped[0] >= 8
    assert int(trend[-1]) == -1
    assert not np.isnan(st[flipped[0]])


def _bars_to_spot(candles: list[Candle]) -> dict[int, Bar1m]:
    spot: dict[int, Bar1m] = {}
    for c in candles:
        for ts in range(c.ts_open, c.ts_close_bar + 60, 60):
            px = c.close if ts == c.ts_close_bar else c.open
            spot[ts] = Bar1m(ts, c.open, c.high, c.low, px, 1.0)
        # entry bar after close
        nxt = c.ts_close_bar + 60
        if nxt not in spot:
            spot[nxt] = Bar1m(nxt, c.close, c.close, c.close, c.close, 1.0)
    return spot


def test_truncation_lookahead() -> None:
    """A signal at index k must also appear when the series is truncated at k.

    If detection used future candles, the truncated run would miss it.
    """
    # 5m UTC candles: 300s. Construct LP then break / retrace / confirm.
    # ST is forced via a long UP trend then we only test detect_signals
    # with a synthetic trend array (not live ST), to isolate look-ahead.
    n = 12
    candles: list[Candle] = []
    ts = 1_735_689_600  # 2025-01-01 00:00 UTC (Wed)
    ohlc = [
        (100, 101, 99, 100.2),
        (100.2, 102, 100, 101.5),
        (101.5, 103, 101, 102.8),  # LP ends here (3 green rising)
        (104.0, 106, 102, 103.0),  # (a) red; high > LPH; not a new LP
        (103.0, 104, 101.5, 102.2),  # (b) close in (LPL, LPH)
        (102.2, 107, 102, 105.5),  # (c) close > LPH  SIGNAL
        (105.5, 107, 105, 106.0),
        (106.0, 108, 105.5, 107.0),
        (107.0, 109, 106, 108.0),
        (108.0, 110, 107, 109.0),
        (109.0, 111, 108, 110.0),
        (110.0, 112, 109, 111.0),
    ]
    for i, (o, h, l, cl) in enumerate(ohlc):
        t0 = ts + i * 300
        candles.append(
            Candle(ts_open=t0, ts_close_bar=t0 + 240, open=o, high=h, low=l, close=cl)
        )
    trend = np.ones(n, dtype=np.int8)
    trend[0] = -1  # flip to UP on candle 1, which is inside the 3 LP bars (0,1,2)
    st = np.full(n, 90.0)
    spot = _bars_to_spot(candles)
    start = 0
    full, _ = detect_signals(candles, trend, st, spot, start, None)
    assert full, "expected at least one long signal"
    sig = full[0]
    assert sig.side == "long"
    assert lookahead_ok(candles, trend, st, spot, start, sig)
    # Truncation: no candles after the signal index.
    trunc_c = candles[: sig.index + 1]
    trunc_t = trend[: sig.index + 1]
    trunc_s = st[: sig.index + 1]
    found, _ = detect_signals(trunc_c, trunc_t, trunc_s, spot, start, None)
    assert found, "truncation dropped the signal — look-ahead leak"
    assert found[-1].index == sig.index
    assert found[-1].entry_ts == sig.signal_ts + 60


def main() -> None:
    test_rma_hand()
    test_supertrend_hand_flip()
    test_truncation_lookahead()
    print("test_supertrend: PASS")


if __name__ == "__main__":
    main()
