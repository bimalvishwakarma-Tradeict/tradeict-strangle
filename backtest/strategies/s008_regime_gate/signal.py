"""S008 expanding-window regime signal (no look-ahead)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def ist_dt(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def session_log_stdev(
    spot_close: dict[int, float],
    d: date,
    h0: int,
    m0: int,
    h1: int,
    m1: int,
    *,
    min_bars: int = 30,
    min_rets: int = 20,
) -> float | None:
    t0 = to_unix(ist_dt(d, h0, m0))
    t1 = to_unix(ist_dt(d, h1, m1))
    closes: list[float] = []
    t = t0
    while t <= t1:
        px = spot_close.get(t)
        if px is not None and px > 0:
            closes.append(float(px))
        t += 60
    if len(closes) < min_bars:
        return None
    rets: list[float] = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < min_rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var)


def morning_range_pct(
    spot_ohlc: dict[int, tuple[float, float, float, float]],
    d: date,
    h0: int = 9,
    m0: int = 0,
    h1: int = 11,
    m1: int = 0,
) -> float | None:
    """(high-low)/open over [09:00, 11:00] inclusive on available bars."""
    t0 = to_unix(ist_dt(d, h0, m0))
    t1 = to_unix(ist_dt(d, h1, m1))
    open_px: float | None = None
    hi = -1.0
    lo = float("inf")
    t = t0
    while t <= t1:
        bar = spot_ohlc.get(t)
        if bar is not None:
            o, h, l, _c = bar
            if open_px is None and o > 0:
                open_px = float(o)
            if h > 0:
                hi = max(hi, float(h))
            if l > 0:
                lo = min(lo, float(l))
        t += 60
    if open_px is None or open_px <= 0 or hi < 0 or lo == float("inf"):
        return None
    return (hi - lo) / open_px


def expanding_rank_pct(history: list[float], value: float) -> float:
    """Fraction of history (incl. value) <= value. Expanding — no future."""
    n = len(history)
    if n <= 0:
        return 1.0
    return sum(1 for v in history if v <= value) / n


@dataclass
class DaySignal:
    d: date
    prev_rvol: float
    overnight: float
    morn_rvol: float | None
    morn_rng: float | None
    sig: float
    rank_prev_rvol: float
    rank_overnight: float


class ExpandingSignalComputer:
    """
    Stateful expanding-window signal builder.

    Call `update(d, ...)` in chronological order. History only grows
    with past observations — never uses future days.
    """

    def __init__(self) -> None:
        self._hist_rvol: list[float] = []
        self._hist_onn: list[float] = []
        self._by_day: dict[date, DaySignal] = {}

    def reset(self) -> None:
        self._hist_rvol.clear()
        self._hist_onn.clear()
        self._by_day.clear()

    def compute_raw(
        self,
        *,
        d: date,
        spot_close: dict[int, float],
        spot_ohlc: dict[int, tuple[float, float, float, float]] | None = None,
    ) -> tuple[float, float, float | None, float | None] | None:
        """
        Raw features for day d (no ranking yet).
        Needs prior session 09:00-17:29 and overnight gap into d 09:00.
        """
        ts_0900 = to_unix(ist_dt(d, 9, 0))
        spot_0900 = spot_close.get(ts_0900)
        if spot_0900 is None or spot_0900 <= 0:
            return None

        prev_rvol: float | None = None
        overnight: float | None = None
        probe = d - timedelta(days=1)
        for _ in range(10):
            ts_prev_1729 = to_unix(ist_dt(probe, 17, 29))
            px_prev = spot_close.get(ts_prev_1729)
            if px_prev is not None and px_prev > 0:
                rv = session_log_stdev(spot_close, probe, 9, 0, 17, 29)
                if rv is not None and rv > 0:
                    overnight = abs(float(spot_0900) - float(px_prev)) / float(px_prev)
                    prev_rvol = rv
                    break
            probe -= timedelta(days=1)
        if prev_rvol is None or overnight is None:
            return None

        morn_rvol = session_log_stdev(spot_close, d, 9, 0, 11, 0)
        morn_rng = None
        if spot_ohlc is not None:
            morn_rng = morning_range_pct(spot_ohlc, d, 9, 0, 11, 0)
        return prev_rvol, overnight, morn_rvol, morn_rng

    def update(
        self,
        *,
        d: date,
        spot_close: dict[int, float],
        spot_ohlc: dict[int, tuple[float, float, float, float]] | None = None,
    ) -> DaySignal | None:
        raw = self.compute_raw(d=d, spot_close=spot_close, spot_ohlc=spot_ohlc)
        if raw is None:
            return None
        prev_rvol, overnight, morn_rvol, morn_rng = raw
        self._hist_rvol.append(prev_rvol)
        self._hist_onn.append(overnight)
        r_rvol = expanding_rank_pct(self._hist_rvol, prev_rvol)
        r_onn = expanding_rank_pct(self._hist_onn, overnight)
        sig = 0.5 * (r_rvol + r_onn)
        out = DaySignal(
            d=d,
            prev_rvol=prev_rvol,
            overnight=overnight,
            morn_rvol=morn_rvol,
            morn_rng=morn_rng,
            sig=sig,
            rank_prev_rvol=r_rvol,
            rank_overnight=r_onn,
        )
        self._by_day[d] = out
        return out

    def get(self, d: date) -> DaySignal | None:
        return self._by_day.get(d)

    def history_rvol(self) -> list[float]:
        return list(self._hist_rvol)

    def history_overnight(self) -> list[float]:
        return list(self._hist_onn)


def build_signals_through(
    days: list[date],
    spot_close: dict[int, float],
    spot_ohlc: dict[int, tuple[float, float, float, float]] | None = None,
    *,
    through: date | None = None,
) -> dict[date, DaySignal]:
    """
    Build expanding signals for days <= through (inclusive).
    If through is None, use all days.
    """
    comp = ExpandingSignalComputer()
    out: dict[date, DaySignal] = {}
    for d in days:
        if through is not None and d > through:
            break
        s = comp.update(d=d, spot_close=spot_close, spot_ohlc=spot_ohlc)
        if s is not None:
            out[d] = s
    return out
