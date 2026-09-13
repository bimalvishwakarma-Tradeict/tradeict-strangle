# indicators.py — Pure incremental indicators for S003 LSR4
#
# No pandas, no numpy, no DB / FastAPI / delta_client imports.
# Safe to import from the Phase-2 backtest harness.

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque

import pytz


class RMA:
    """
    Wilder's RMA (Pine ta.rma).

    Seed: SMA of the first `length` values, then:
        rma = rma_prev + (value - rma_prev) / length
    """

    def __init__(self, length: int) -> None:
        if length < 1:
            raise ValueError("RMA length must be >= 1")
        self.length = int(length)
        self._seed: list[float] = []
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, value: float) -> float | None:
        v = float(value)
        if self._value is None:
            self._seed.append(v)
            if len(self._seed) < self.length:
                return None
            self._value = sum(self._seed) / float(self.length)
            self._seed.clear()
            return self._value
        self._value = self._value + (v - self._value) / float(self.length)
        return self._value


class SMA:
    """Simple moving average over a fixed window."""

    def __init__(self, length: int) -> None:
        if length < 1:
            raise ValueError("SMA length must be >= 1")
        self.length = int(length)
        self._buf: Deque[float] = deque(maxlen=self.length)
        self._sum = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, value: float) -> float | None:
        v = float(value)
        if len(self._buf) == self.length:
            self._sum -= self._buf[0]
        self._buf.append(v)
        self._sum += v
        if len(self._buf) < self.length:
            self._value = None
            return None
        self._value = self._sum / float(self.length)
        return self._value


class Rolling:
    """Fixed-window highest / lowest tracker."""

    def __init__(self, length: int) -> None:
        if length < 1:
            raise ValueError("Rolling length must be >= 1")
        self.length = int(length)
        self._buf: Deque[float] = deque(maxlen=self.length)

    def update(self, value: float) -> None:
        self._buf.append(float(value))

    def highest(self) -> float | None:
        if len(self._buf) < self.length:
            return None
        return max(self._buf)

    def lowest(self) -> float | None:
        if len(self._buf) < self.length:
            return None
        return min(self._buf)

    def ready(self) -> bool:
        return len(self._buf) >= self.length


class ATR:
    """Average True Range — True Range smoothed by RMA (Pine ta.atr)."""

    def __init__(self, length: int) -> None:
        self.length = int(length)
        self._rma = RMA(length)
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, high: float, low: float, prev_close: float) -> float | None:
        h = float(high)
        l = float(low)
        pc = float(prev_close)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        self._value = self._rma.update(tr)
        return self._value


class RSI:
    """Wilder RSI — RMA of gains / losses (Pine ta.rsi)."""

    def __init__(self, length: int) -> None:
        self.length = int(length)
        self._avg_gain = RMA(length)
        self._avg_loss = RMA(length)
        self._prev_close: float | None = None
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, close: float) -> float | None:
        c = float(close)
        if self._prev_close is None:
            self._prev_close = c
            return None
        change = c - self._prev_close
        self._prev_close = c
        gain = change if change > 0.0 else 0.0
        loss = -change if change < 0.0 else 0.0
        ag = self._avg_gain.update(gain)
        al = self._avg_loss.update(loss)
        if ag is None or al is None:
            self._value = None
            return None
        if al == 0.0:
            self._value = 100.0
            return self._value
        rs = ag / al
        self._value = 100.0 - (100.0 / (1.0 + rs))
        return self._value


class DMI_ADX:
    """
    Pine ta.dmi(len, len) — +DI, -DI, ADX.

    +DM/-DM and TR each smoothed with RMA(length).
    DX = 100 * abs(+DI - -DI) / (+DI + -DI)
    ADX = RMA(DX, length)  → double RMA warmup (~2 * length bars after first diff).
    """

    def __init__(self, length: int) -> None:
        self.length = int(length)
        self._plus_dm = RMA(length)
        self._minus_dm = RMA(length)
        self._tr = RMA(length)
        self._adx = RMA(length)
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_close: float | None = None
        self._plus_di: float | None = None
        self._minus_di: float | None = None
        self._adx_value: float | None = None

    @property
    def plus_di(self) -> float | None:
        return self._plus_di

    @property
    def minus_di(self) -> float | None:
        return self._minus_di

    @property
    def adx(self) -> float | None:
        return self._adx_value

    def update(
        self, high: float, low: float, close: float
    ) -> tuple[float, float, float] | None:
        h = float(high)
        l = float(low)
        c = float(close)
        if (
            self._prev_high is None
            or self._prev_low is None
            or self._prev_close is None
        ):
            self._prev_high = h
            self._prev_low = l
            self._prev_close = c
            return None

        up_move = h - self._prev_high
        down_move = self._prev_low - l
        plus_dm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0.0) else 0.0
        tr = max(
            h - l,
            abs(h - self._prev_close),
            abs(l - self._prev_close),
        )

        self._prev_high = h
        self._prev_low = l
        self._prev_close = c

        s_plus = self._plus_dm.update(plus_dm)
        s_minus = self._minus_dm.update(minus_dm)
        s_tr = self._tr.update(tr)
        if s_plus is None or s_minus is None or s_tr is None or s_tr == 0.0:
            return None

        plus_di = 100.0 * s_plus / s_tr
        minus_di = 100.0 * s_minus / s_tr
        di_sum = plus_di + minus_di
        if di_sum == 0.0:
            dx = 0.0
        else:
            dx = 100.0 * abs(plus_di - minus_di) / di_sum
        adx = self._adx.update(dx)
        if adx is None:
            return None
        self._plus_di = plus_di
        self._minus_di = minus_di
        self._adx_value = adx
        return plus_di, minus_di, adx


class SessionVWAP:
    """
    Session-anchored VWAP (NOT a rolling window).

    Accumulators reset when the local calendar date in `anchor_tz` changes.
    """

    def __init__(self, anchor_tz: str) -> None:
        self.anchor_tz = str(anchor_tz)
        self._tz = pytz.timezone(self.anchor_tz)
        self._session_date = None
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._value: float | None = None
        self._session_boundary_crossed = False

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def session_boundary_crossed(self) -> bool:
        """True once at least one session date change has been observed."""
        return self._session_boundary_crossed

    @property
    def session_date(self):
        return self._session_date

    def update(
        self,
        candle_open_utc: datetime,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> float:
        if candle_open_utc.tzinfo is None:
            raise ValueError("candle_open_utc must be timezone-aware UTC")
        local_dt = candle_open_utc.astimezone(self._tz)
        local_date = local_dt.date()
        if self._session_date is None:
            self._session_date = local_date
        elif local_date != self._session_date:
            self._session_date = local_date
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._session_boundary_crossed = True

        hlc3 = (float(high) + float(low) + float(close)) / 3.0
        vol = float(volume)
        self._cum_pv += hlc3 * vol
        self._cum_vol += vol
        if self._cum_vol <= 0.0:
            self._value = hlc3
        else:
            self._value = self._cum_pv / self._cum_vol
        return float(self._value)
