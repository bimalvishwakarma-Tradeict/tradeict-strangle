"""Pure-numpy indicators for S007-B. No external deps beyond numpy."""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def smith_vwap(
    close: np.ndarray,
    volume: np.ndarray,
    length: int = 60,
    k: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Volume-weighted band on log price.

    For each rolling `length` window:
        x    = log(close)
        mean = sum(x*v) / sum(v)                 (volume-weighted mean)
        dev  = sum(|x - mean| * v) / sum(v)      (volume-weighted MEAN ABSOLUTE
                                                  deviation — NOT std dev)
        lower = exp(mean - k*dev)
        upper = exp(mean + k*dev)

    First (length-1) bars are NaN. Vectorized via sliding_window_view.
    Returns (lower_band, upper_band), both aligned to `close`.
    """
    close = np.asarray(close, dtype=np.float64)
    volume = np.asarray(volume, dtype=np.float64)
    n = close.size
    lower = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    if n < length or length < 1:
        return lower, upper

    x = np.log(close)
    xw = sliding_window_view(x, length)        # (n-length+1, length)
    vw = sliding_window_view(volume, length)
    vsum = vw.sum(axis=1)
    safe = vsum > 0

    mean = np.full(xw.shape[0], np.nan)
    mean[safe] = (xw[safe] * vw[safe]).sum(axis=1) / vsum[safe]

    dev = np.full(xw.shape[0], np.nan)
    absdev = np.abs(xw - mean[:, None]) * vw
    dev[safe] = absdev[safe].sum(axis=1) / vsum[safe]

    lower[length - 1:] = np.exp(mean - k * dev)
    upper[length - 1:] = np.exp(mean + k * dev)
    return lower, upper


def rsi_wilder(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder RSI with RMA smoothing (alpha = 1/period).

    Seed = simple average of the first `period` price changes. When average
    loss is 0 the RSI is defined as 100. First valid value lands at index
    `period`; earlier indices are NaN.
    """
    close = np.asarray(close, dtype=np.float64)
    n = close.size
    rsi = np.full(n, np.nan)
    if n < period + 1 or period < 1:
        return rsi

    delta = np.diff(close)                      # length n-1, delta[i] -> close[i+1]
    gain = np.where(delta > 0.0, delta, 0.0)
    loss = np.where(delta < 0.0, -delta, 0.0)

    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    alpha = 1.0 / period

    def _rsi(ag: float, al: float) -> float:
        if al == 0.0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    # first value at close index `period`
    rsi[period] = _rsi(avg_gain, avg_loss)
    for i in range(period, n - 1):
        avg_gain = avg_gain + alpha * (gain[i] - avg_gain)
        avg_loss = avg_loss + alpha * (loss[i] - avg_loss)
        rsi[i + 1] = _rsi(avg_gain, avg_loss)
    return rsi
