#!/usr/bin/env python3
"""
S004 BTC-only signal sweep + TradingView parity check.

Uses ONLY BTC 1m OHLCV (backtest/data_1m/BTCUSD_1m_*.csv).
No option marks. Does not touch live bot code or s004_gate.py.

Modes:
  --parity  Compare indicator values vs chart snapshots; list window signals.
  --sweep   Grid search with TRAIN/TEST + random control.

No print() — logging + file writes only.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
DATA_1M_DIR = _BACKTEST / "data_1m"
RESULTS_DIR = _BACKTEST / "results"
PARITY_OUT = RESULTS_DIR / "s004_parity.txt"
SWEEP_CSV = RESULTS_DIR / "s004_signal_sweep.csv"
SWEEP_TXT = RESULTS_DIR / "s004_signal_sweep.txt"

STOP_PTS = 50.0
RSI_LONG_LO = 55.0
RSI_LONG_HI = 70.0
RSI_SHORT_LO = 30.0
RSI_SHORT_HI = 45.0
WARMUP_DAYS = 3
CUTOFF_HOUR = 17
CUTOFF_MINUTE = 25
CUT_HM = CUTOFF_HOUR * 60 + CUTOFF_MINUTE

FAST_GRID = (4, 5, 6, 7, 8, 9)
SLOW_GRID = tuple(range(14, 22))  # 14..21
RSI_GRID = tuple(range(5, 21))  # 5..20
TARGET_GRID = (150, 160, 170, 180, 190, 200)

TRAIN_START = date(2025, 6, 17)
TRAIN_END = date(2026, 1, 31)
TEST_START = date(2026, 2, 1)

CONTROL_REPEATS = 20
CONTROL_SEED = 20260916
MIN_TRAIN_TRADES = 100

# Default parity anchors (IST candle OPEN). Chart values used when timestamp matches.
DEFAULT_PARITY_TS: tuple[str, ...] = ("2026-09-16 04:37", "2026-09-16 01:42")
CHART_ANCHORS: dict[str, dict[str, Any]] = {
    "2026-09-16 04:37": {
        "o": 75757.5,
        "h": 75776.0,
        "l": 75748.0,
        "c": 75764.0,
        "vwap": 76567.2,
        "ma_fast": 75743.5,
        "ma_slow": 75698.7,
        "rsi": 63.62,
    },
    "2026-09-16 01:42": {
        "o": None,
        "h": None,
        "l": None,
        "c": None,
        "vwap": 76609.1,
        "ma_fast": 75982.5,
        "ma_slow": 76048.5,
        "rsi": 47.02,
    },
}
PARITY_SIGNAL_START = "2026-09-15 18:00"
PARITY_SIGNAL_END = "2026-09-16 06:00"

PARITY_CONFIGS: list[tuple[str, int, int, int, str]] = [
    ("EMA", 9, 21, 14, "UTC00"),
    ("EMA", 9, 21, 14, "IST00"),
    ("EMA", 5, 21, 14, "UTC00"),
    ("EMA", 5, 21, 14, "IST00"),
    ("SMA", 5, 21, 14, "UTC00"),
    ("SMA", 5, 21, 14, "IST00"),
]

BASELINE = {"ma_type": "EMA", "fast": 9, "slow": 21, "rsi_len": 14, "target": 150}

logger = logging.getLogger("s004_signal_sweep")

SideFilter = Literal["both", "long", "short"]
ExitLabel = Literal["RIGHT", "WRONG", "FLIP", "CUTOFF"]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def find_spot_csv() -> Path:
    files = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    if not files:
        raise FileNotFoundError(f"No BTCUSD_1m_*.csv in {DATA_1M_DIR}")
    return files[-1]


def load_ohlcv(path: Path | None = None) -> pd.DataFrame:
    csv_path = path or find_spot_csv()
    df = pd.read_csv(csv_path)
    need = {"open_time_unix", "open", "high", "low", "close", "volume"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns {missing}")
    df = df.sort_values("open_time_unix").reset_index(drop=True)
    ts = df["open_time_unix"].astype(np.int64).to_numpy()
    df["dt_utc"] = pd.to_datetime(ts, unit="s", utc=True)
    df["dt_ist"] = df["dt_utc"].dt.tz_convert(IST)
    df["ist_date"] = df["dt_ist"].dt.date
    df["ist_hour"] = df["dt_ist"].dt.hour.astype(np.int16)
    df["ist_minute"] = df["dt_ist"].dt.minute.astype(np.int16)
    df["utc_date"] = df["dt_utc"].dt.date
    logger.info(
        "loaded %s bars from %s (%s .. %s IST)",
        len(df),
        csv_path.name,
        df["dt_ist"].iloc[0],
        df["dt_ist"].iloc[-1],
    )
    return df


def ist_key(dt_ist: pd.Timestamp) -> str:
    return dt_ist.strftime("%Y-%m-%d %H:%M")


def build_cutoff_map(df: pd.DataFrame) -> dict[date, int]:
    """IST calendar date → bar index of 17:25 IST that day."""
    out: dict[date, int] = {}
    dates = df["ist_date"].to_numpy()
    hours = df["ist_hour"].to_numpy()
    mins = df["ist_minute"].to_numpy()
    for i in range(len(df)):
        if int(hours[i]) == CUTOFF_HOUR and int(mins[i]) == CUTOFF_MINUTE:
            out[dates[i]] = i
    return out


def cutoff_for_entry(
    entry_i: int,
    ist_dates: np.ndarray,
    hours: np.ndarray,
    mins: np.ndarray,
    cutoff_map: dict[date, int],
) -> int | None:
    hm = int(hours[entry_i]) * 60 + int(mins[entry_i])
    d0: date = ist_dates[entry_i]
    cut_day = d0 if hm < CUT_HM else d0 + timedelta(days=1)
    return cutoff_map.get(cut_day)


# ---------------------------------------------------------------------------
# Indicators (TradingView / Pine parity)
# ---------------------------------------------------------------------------
def sma_np(x: np.ndarray, length: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if length < 1 or n < length:
        return out
    csum = np.cumsum(x, dtype=np.float64)
    out[length - 1] = csum[length - 1] / length
    if n > length:
        out[length:] = (csum[length:] - csum[: n - length]) / length
    return out


def ema_np(x: np.ndarray, length: int) -> np.ndarray:
    """Pine ta.ema — seed with SMA of first `length` bars."""
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if length < 1 or n < length:
        return out
    alpha = 2.0 / (length + 1.0)
    out[length - 1] = float(np.mean(x[:length]))
    for i in range(length, n):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi_wilder_np(close: np.ndarray, length: int) -> np.ndarray:
    """Pine ta.rsi — Wilder RMA of gains/losses."""
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float64)
    if length < 1 or n < length + 1:
        return out
    delta = np.diff(close.astype(np.float64), prepend=np.nan)
    gain = np.where(np.isnan(delta), 0.0, np.where(delta > 0.0, delta, 0.0))
    loss = np.where(np.isnan(delta), 0.0, np.where(delta < 0.0, -delta, 0.0))
    avg_g = float(np.mean(gain[1 : length + 1]))
    avg_l = float(np.mean(loss[1 : length + 1]))
    out[length] = 100.0 if avg_l == 0.0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    for i in range(length + 1, n):
        avg_g = (avg_g * (length - 1) + float(gain[i])) / length
        avg_l = (avg_l * (length - 1) + float(loss[i])) / length
        out[i] = 100.0 if avg_l == 0.0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))
    return out


def session_vwap_np(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    session_ids: np.ndarray,
) -> np.ndarray:
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float64)
    hlc3 = (high + low + close) / 3.0
    cum_pv = 0.0
    cum_vol = 0.0
    prev_sid: Any = object()
    for i in range(n):
        sid = session_ids[i]
        if sid != prev_sid:
            cum_pv = 0.0
            cum_vol = 0.0
            prev_sid = sid
        vol = float(volume[i])
        cum_pv += float(hlc3[i]) * vol
        cum_vol += vol
        out[i] = float(hlc3[i]) if cum_vol <= 0.0 else cum_pv / cum_vol
    return out


def ma_series(close: np.ndarray, length: int, ma_type: str) -> np.ndarray:
    if ma_type == "EMA":
        return ema_np(close, length)
    if ma_type == "SMA":
        return sma_np(close, length)
    raise ValueError(f"unknown ma_type {ma_type}")


@dataclass
class IndicatorCache:
    ma_type: str
    vwap_anchor: str
    ma: dict[int, np.ndarray] = field(default_factory=dict)
    rsi: dict[int, np.ndarray] = field(default_factory=dict)
    vwap: np.ndarray | None = None


def build_cache(
    df: pd.DataFrame,
    ma_type: str,
    vwap_anchor: str,
    ma_lengths: list[int],
    rsi_lengths: list[int],
) -> IndicatorCache:
    close = df["close"].to_numpy(dtype=np.float64)
    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    volume = df["volume"].to_numpy(dtype=np.float64)
    if vwap_anchor == "UTC00":
        session_ids = df["utc_date"].to_numpy()
    elif vwap_anchor == "IST00":
        session_ids = df["ist_date"].to_numpy()
    else:
        raise ValueError(f"unknown vwap_anchor {vwap_anchor}")

    cache = IndicatorCache(ma_type=ma_type, vwap_anchor=vwap_anchor)
    for L in sorted(set(ma_lengths)):
        cache.ma[L] = ma_series(close, L, ma_type)
        logger.info("cached %s(%d)", ma_type, L)
    for L in sorted(set(rsi_lengths)):
        cache.rsi[L] = rsi_wilder_np(close, L)
        logger.info("cached RSI(%d)", L)
    cache.vwap = session_vwap_np(high, low, close, volume, session_ids)
    logger.info("cached VWAP anchor=%s", vwap_anchor)
    return cache


# ---------------------------------------------------------------------------
# Structure gate
# ---------------------------------------------------------------------------
@dataclass
class StructureState:
    above: bool | None = None
    seg_high: float = float("-inf")
    seg_low: float = float("inf")
    marked_swing_high: float | None = None
    marked_swing_low: float | None = None


def update_structure(
    st: StructureState, fast: float, vwap: float, high: float, low: float
) -> None:
    above = fast > vwap
    if st.above is None:
        st.above = above
        if above:
            st.seg_high = high
        else:
            st.seg_low = low
        return
    if above and st.above:
        st.seg_high = max(st.seg_high, high)
    elif (not above) and (not st.above):
        st.seg_low = min(st.seg_low, low)
    elif st.above and (not above):
        if st.seg_high != float("-inf"):
            st.marked_swing_high = st.seg_high
        st.seg_low = low
        st.above = False
    elif (not st.above) and above:
        if st.seg_low != float("inf"):
            st.marked_swing_low = st.seg_low
        st.seg_high = high
        st.above = True


def structure_allows(st: StructureState, close: float, side: str) -> tuple[bool, str]:
    msh = st.marked_swing_high
    msl = st.marked_swing_low
    if msh is not None and msl is not None and msl < close < msh:
        return False, f"BETWEEN(msl={msl:.1f},msh={msh:.1f},c={close:.1f})"
    if side == "LONG":
        if msh is None:
            return True, "LONG_OK(no_marked_high)"
        if close > msh:
            return True, f"LONG_OK(close>{msh:.1f})"
        return False, f"LONG_BLOCK(close<={msh:.1f})"
    if side == "SHORT":
        if msl is None:
            return True, "SHORT_OK(no_marked_low)"
        if close < msl:
            return True, f"SHORT_OK(close<{msl:.1f})"
        return False, f"SHORT_BLOCK(close>={msl:.1f})"
    return False, "UNKNOWN_SIDE"


# ---------------------------------------------------------------------------
# Signals + trades
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RawSignal:
    i: int  # signal candle index (close)
    side: str  # LONG | SHORT
    gate: str


@dataclass
class Trade:
    side: str
    signal_i: int
    entry_i: int
    entry_px: float
    entry_ts_ist: str
    exit_i: int
    exit_px: float
    exit_ts_ist: str
    label: ExitLabel
    pnl: float
    gate: str
    target: float
    ist_hour: int
    ist_day: date


def warmup_date_set(ist_dates: np.ndarray) -> set[date]:
    uniq: list[date] = []
    seen: set[date] = set()
    for d in ist_dates:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
            if len(uniq) >= WARMUP_DAYS:
                break
    return set(uniq[:WARMUP_DAYS])


def generate_signals(
    df: pd.DataFrame,
    cache: IndicatorCache,
    fast_len: int,
    slow_len: int,
    rsi_len: int,
    side_filter: SideFilter,
) -> list[RawSignal]:
    """Structure gate + MA/RSI/VWAP rules on every closed bar (full series)."""
    n = len(df)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    ist_dates = df["ist_date"].to_numpy()
    fast = cache.ma[fast_len]
    slow = cache.ma[slow_len]
    rsi = cache.rsi[rsi_len]
    vwap = cache.vwap
    assert vwap is not None
    warm = warmup_date_set(ist_dates)

    st = StructureState()
    out: list[RawSignal] = []
    for i in range(n):
        fi, vi = fast[i], vwap[i]
        if not (math.isnan(fi) or math.isnan(vi)):
            update_structure(st, float(fi), float(vi), float(h[i]), float(l[i]))

        if i < 2 or ist_dates[i] in warm:
            continue
        f0, f1 = fast[i], fast[i - 1]
        s0, s1 = slow[i], slow[i - 1]
        r0, r1, r2 = rsi[i], rsi[i - 1], rsi[i - 2]
        v0 = vwap[i]
        if any(math.isnan(x) for x in (f0, f1, s0, s1, r0, r1, r2, v0)):
            continue

        cand: str | None = None
        gate = ""
        # LONG: fast crosses above slow
        if f1 <= s1 and f0 > s0:
            if RSI_LONG_LO <= r0 <= RSI_LONG_HI and (
                r1 < RSI_LONG_LO or r2 < RSI_LONG_LO
            ):
                if f0 > v0 and s0 > v0:
                    ok, gate = structure_allows(st, float(c[i]), "LONG")
                    if ok:
                        cand = "LONG"
        # SHORT: slow crosses above fast
        if cand is None and s1 <= f1 and s0 > f0:
            if RSI_SHORT_LO <= r0 <= RSI_SHORT_HI and (
                r1 > RSI_SHORT_HI or r2 > RSI_SHORT_HI
            ):
                if f0 < v0 and s0 < v0:
                    ok, gate = structure_allows(st, float(c[i]), "SHORT")
                    if ok:
                        cand = "SHORT"

        if cand is None:
            continue
        if side_filter == "long" and cand != "LONG":
            continue
        if side_filter == "short" and cand != "SHORT":
            continue
        out.append(RawSignal(i=i, side=cand, gate=gate))
    return out


def check_bar_exit(
    side: str,
    entry_px: float,
    target: float,
    high: float,
    low: float,
    close: float,
) -> tuple[ExitLabel, float, float] | None:
    """Returns (label, pnl, exit_px) or None. Same-minute both → WRONG."""
    if side == "LONG":
        hit_t = high >= entry_px + target
        hit_s = close <= entry_px - STOP_PTS
        if hit_t and hit_s:
            return "WRONG", close - entry_px, close
        if hit_t:
            return "RIGHT", float(target), entry_px + target
        if hit_s:
            return "WRONG", close - entry_px, close
    else:
        hit_t = low <= entry_px - target
        hit_s = close >= entry_px + STOP_PTS
        if hit_t and hit_s:
            return "WRONG", entry_px - close, close
        if hit_t:
            return "RIGHT", float(target), entry_px - target
        if hit_s:
            return "WRONG", entry_px - close, close
    return None


def simulate_trades(
    df: pd.DataFrame,
    signals: list[RawSignal],
    target: float,
    cutoff_map: dict[date, int],
    start_date: date | None,
    end_date: date | None,
) -> list[Trade]:
    """
    Entry = next candle OPEN after signal.
    Same-side ignore; opposite → FLIP at next open + new entry.
    TARGET / STOP / CUTOFF as specified.
    """
    n = len(df)
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    dt_ist = df["dt_ist"]
    ist_dates = df["ist_date"].to_numpy()
    hours = df["ist_hour"].to_numpy()
    mins = df["ist_minute"].to_numpy()

    def in_range(day: date) -> bool:
        if start_date is not None and day < start_date:
            return False
        if end_date is not None and day > end_date:
            return False
        return True

    # Build timeline events: at each signal index, a side intent
    sig_at: dict[int, RawSignal] = {s.i: s for s in signals}

    trades: list[Trade] = []
    pos_side: str | None = None
    pos_entry_i: int | None = None
    pos_entry_px: float | None = None
    pos_gate = ""
    pos_signal_i = 0

    pending_side: str | None = None
    pending_gate = ""
    pending_signal_i = 0
    pending_is_flip = False

    for i in range(n):
        # --- bar OPEN: resolve pending flip/entry ---
        if pending_side is not None and pending_is_flip and pos_side is not None:
            assert pos_entry_i is not None and pos_entry_px is not None
            exit_px = float(o[i])
            pnl = (
                exit_px - pos_entry_px
                if pos_side == "LONG"
                else pos_entry_px - exit_px
            )
            if in_range(ist_dates[pos_entry_i]):
                trades.append(
                    Trade(
                        side=pos_side,
                        signal_i=pos_signal_i,
                        entry_i=pos_entry_i,
                        entry_px=pos_entry_px,
                        entry_ts_ist=ist_key(dt_ist.iloc[pos_entry_i]),
                        exit_i=i,
                        exit_px=exit_px,
                        exit_ts_ist=ist_key(dt_ist.iloc[i]),
                        label="FLIP",
                        pnl=pnl,
                        gate=pos_gate,
                        target=target,
                        ist_hour=int(hours[pos_entry_i]),
                        ist_day=ist_dates[pos_entry_i],
                    )
                )
            pos_side = None
            pos_entry_i = None
            pos_entry_px = None
            pending_is_flip = False

        if pending_side is not None and pos_side is None:
            pos_side = pending_side
            pos_entry_i = i
            pos_entry_px = float(o[i])
            pos_gate = pending_gate
            pos_signal_i = pending_signal_i
            pending_side = None

        # --- manage open position on this bar ---
        if pos_side is not None and pos_entry_i is not None and pos_entry_px is not None:
            if i >= pos_entry_i and in_range(ist_dates[pos_entry_i]):
                cut_i = cutoff_for_entry(pos_entry_i, ist_dates, hours, mins, cutoff_map)
                bar = check_bar_exit(
                    pos_side, pos_entry_px, target, float(h[i]), float(l[i]), float(c[i])
                )
                do_cut = cut_i is not None and i == cut_i
                if bar is not None:
                    lab, pnl, exit_px = bar
                    trades.append(
                        Trade(
                            side=pos_side,
                            signal_i=pos_signal_i,
                            entry_i=pos_entry_i,
                            entry_px=pos_entry_px,
                            entry_ts_ist=ist_key(dt_ist.iloc[pos_entry_i]),
                            exit_i=i,
                            exit_px=exit_px,
                            exit_ts_ist=ist_key(dt_ist.iloc[i]),
                            label=lab,
                            pnl=pnl,
                            gate=pos_gate,
                            target=target,
                            ist_hour=int(hours[pos_entry_i]),
                            ist_day=ist_dates[pos_entry_i],
                        )
                    )
                    pos_side = None
                    pos_entry_i = None
                    pos_entry_px = None
                elif do_cut:
                    exit_px = float(c[i])
                    pnl = (
                        exit_px - pos_entry_px
                        if pos_side == "LONG"
                        else pos_entry_px - exit_px
                    )
                    trades.append(
                        Trade(
                            side=pos_side,
                            signal_i=pos_signal_i,
                            entry_i=pos_entry_i,
                            entry_px=pos_entry_px,
                            entry_ts_ist=ist_key(dt_ist.iloc[pos_entry_i]),
                            exit_i=i,
                            exit_px=exit_px,
                            exit_ts_ist=ist_key(dt_ist.iloc[i]),
                            label="CUTOFF",
                            pnl=pnl,
                            gate=pos_gate,
                            target=target,
                            ist_hour=int(hours[pos_entry_i]),
                            ist_day=ist_dates[pos_entry_i],
                        )
                    )
                    pos_side = None
                    pos_entry_i = None
                    pos_entry_px = None

        # --- signal on close ---
        sig = sig_at.get(i)
        if sig is None:
            continue
        if pos_side is None:
            if i + 1 < n:
                pending_side = sig.side
                pending_gate = sig.gate
                pending_signal_i = sig.i
                pending_is_flip = False
            continue
        if sig.side == pos_side:
            continue  # same side ignore
        if i + 1 < n:
            pending_side = sig.side
            pending_gate = sig.gate
            pending_signal_i = sig.i
            pending_is_flip = True

    return trades


# ---------------------------------------------------------------------------
# Stats + random control
# ---------------------------------------------------------------------------
@dataclass
class PeriodStats:
    n_trades: int
    p_right: float
    p_wrong: float
    p_flip: float
    p_cutoff: float
    mean_pnl: float
    total_pnl: float
    max_consec_loss: int
    trades_per_day: float
    control_mean: float
    control_p95: float
    edge_pp: float
    beats_control: bool


def _props(trades: list[Trade]) -> dict[str, float]:
    n = len(trades)
    if n == 0:
        nan = float("nan")
        return {"p_right": nan, "p_wrong": nan, "p_flip": nan, "p_cutoff": nan}
    return {
        "p_right": sum(1 for t in trades if t.label == "RIGHT") / n,
        "p_wrong": sum(1 for t in trades if t.label == "WRONG") / n,
        "p_flip": sum(1 for t in trades if t.label == "FLIP") / n,
        "p_cutoff": sum(1 for t in trades if t.label == "CUTOFF") / n,
    }


def max_consecutive_losses(trades: list[Trade]) -> int:
    best = cur = 0
    for t in trades:
        if t.pnl < 0 or t.label == "WRONG":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def simulate_random_exit(
    side: str,
    entry_i: int,
    entry_px: float,
    target: float,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    ist_dates: np.ndarray,
    hours: np.ndarray,
    mins: np.ndarray,
    cutoff_map: dict[date, int],
    n: int,
) -> ExitLabel:
    cut_i = cutoff_for_entry(entry_i, ist_dates, hours, mins, cutoff_map)
    for i in range(entry_i, n):
        bar = check_bar_exit(side, entry_px, target, float(h[i]), float(l[i]), float(c[i]))
        if bar is not None:
            return bar[0]
        if cut_i is not None and i == cut_i:
            return "CUTOFF"
    return "CUTOFF"


def random_control_p_right(
    df: pd.DataFrame,
    trades: list[Trade],
    target: float,
    start_date: date,
    end_date: date,
    cutoff_map: dict[date, int],
    seed: int,
) -> tuple[float, float]:
    n_tr = len(trades)
    if n_tr == 0:
        return float("nan"), float("nan")

    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    l = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    ist_dates = df["ist_date"].to_numpy()
    hours = df["ist_hour"].to_numpy()
    mins = df["ist_minute"].to_numpy()
    n = len(df)

    mask = np.array([(start_date <= d <= end_date) for d in ist_dates], dtype=bool)
    pools: dict[int, np.ndarray] = {}
    for hh in range(24):
        pools[hh] = np.where(mask & (hours == hh))[0]
    any_pool = np.where(mask)[0]

    hour_list = [t.ist_hour for t in trades]
    rng = np.random.default_rng(seed)
    rates: list[float] = []
    for _ in range(CONTROL_REPEATS):
        rights = 0
        for hh in hour_list:
            pool = pools.get(hh, any_pool)
            if len(pool) == 0:
                pool = any_pool
            if len(pool) == 0:
                continue
            ei = int(rng.choice(pool))
            side = "LONG" if rng.random() < 0.5 else "SHORT"
            lab = simulate_random_exit(
                side, ei, float(o[ei]), target, h, l, c, ist_dates, hours, mins, cutoff_map, n
            )
            if lab == "RIGHT":
                rights += 1
        rates.append(rights / n_tr)
    arr = np.asarray(rates, dtype=np.float64)
    return float(np.mean(arr)), float(np.percentile(arr, 95))


def compute_stats(
    df: pd.DataFrame,
    trades: list[Trade],
    target: float,
    start_date: date,
    end_date: date,
    cutoff_map: dict[date, int],
    seed: int,
) -> PeriodStats:
    n = len(trades)
    props = _props(trades)
    days = max(1, (end_date - start_date).days + 1)
    if n == 0:
        nan = float("nan")
        return PeriodStats(
            0, nan, nan, nan, nan, nan, 0.0, 0, 0.0, nan, nan, nan, False
        )
    pnls = [t.pnl for t in trades]
    c_mean, c_p95 = random_control_p_right(
        df, trades, target, start_date, end_date, cutoff_map, seed
    )
    p_right = props["p_right"]
    edge = (p_right - c_mean) * 100.0 if not math.isnan(c_mean) else float("nan")
    beats = (not math.isnan(c_p95)) and (p_right > c_p95)
    return PeriodStats(
        n_trades=n,
        p_right=p_right,
        p_wrong=props["p_wrong"],
        p_flip=props["p_flip"],
        p_cutoff=props["p_cutoff"],
        mean_pnl=float(np.mean(pnls)),
        total_pnl=float(np.sum(pnls)),
        max_consec_loss=max_consecutive_losses(trades),
        trades_per_day=n / days,
        control_mean=c_mean,
        control_p95=c_p95,
        edge_pp=edge,
        beats_control=beats,
    )


def filter_side(trades: list[Trade], side: str) -> list[Trade]:
    if side == "BOTH":
        return trades
    return [t for t in trades if t.side == side]


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------
def _parse_parity_ts(raw: str) -> str:
    """Normalize 'YYYY-MM-DD HH:MM' (optional seconds stripped)."""
    s = raw.strip().replace("T", " ")
    if s.endswith(" IST"):
        s = s[: -len(" IST")].strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise SystemExit(
            f"--parity-ts must be 'YYYY-MM-DD HH:MM', got {raw!r}"
        ) from exc
    return dt.strftime("%Y-%m-%d %H:%M")


def build_parity_points(timestamps: list[str]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for ts in timestamps:
        pt: dict[str, Any] = {
            "ist": ts,
            "o": None,
            "h": None,
            "l": None,
            "c": None,
            "vwap": None,
            "ma_fast": None,
            "ma_slow": None,
            "rsi": None,
        }
        if ts in CHART_ANCHORS:
            pt.update(CHART_ANCHORS[ts])
        points.append(pt)
    return points


def run_parity(
    df: pd.DataFrame,
    parity_timestamps: list[str] | None = None,
) -> tuple[list[str], int]:
    """
    Returns (report_lines, exit_code).
    exit_code 2 when no requested timestamps exist in CSV for any config.
    """
    ts_list = list(parity_timestamps) if parity_timestamps else list(DEFAULT_PARITY_TS)
    points = build_parity_points(ts_list)

    lines: list[str] = []
    lines.append("===== S004 PARITY MODE =====")
    lines.append(f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    lines.append(f"spot_csv={find_spot_csv()}")
    lines.append(f"bars={len(df)}")
    lines.append(
        f"data_span_ist={df['dt_ist'].iloc[0]} .. {df['dt_ist'].iloc[-1]}"
    )
    lines.append(f"parity_timestamps={ts_list}")
    lines.append("")

    key_to_i = {ist_key(df["dt_ist"].iloc[i]): i for i in range(len(df))}
    # (score, n_points, cfg)
    scored: list[tuple[float, int, tuple[str, int, int, int, str]]] = []
    cutoff_map = build_cutoff_map(df)

    for ma_type, fast_l, slow_l, rsi_l, anchor in PARITY_CONFIGS:
        cache = build_cache(df, ma_type, anchor, [fast_l, slow_l], [rsi_l])
        lines.append(
            f"----- CONFIG {ma_type} {fast_l}/{slow_l} RSI{rsi_l} VWAP={anchor} -----"
        )
        total_abs = 0.0
        n_cmp = 0
        for pt in points:
            k = pt["ist"]
            i = key_to_i.get(k)
            lines.append(f"  timestamp IST (candle OPEN)={k}")
            if i is None:
                lines.append(
                    "    ERROR: candle not in CSV "
                    "(current 1m file may end before this timestamp)"
                )
                continue
            assert cache.vwap is not None
            o_ = float(df["open"].iloc[i])
            h_ = float(df["high"].iloc[i])
            l_ = float(df["low"].iloc[i])
            c_ = float(df["close"].iloc[i])
            vwap = float(cache.vwap[i])
            mf = float(cache.ma[fast_l][i])
            ms = float(cache.ma[slow_l][i])
            rr = float(cache.rsi[rsi_l][i])
            lines.append(
                f"    ours: O={o_:.1f} H={h_:.1f} L={l_:.1f} C={c_:.1f} "
                f"VWAP={vwap:.1f} MA={mf:.1f}/{ms:.1f} RSI={rr:.2f}"
            )
            n_cmp += 1
            if pt.get("vwap") is None and pt.get("ma_fast") is None:
                lines.append("    chart: (no chart anchor for this timestamp)")
                continue
            if pt.get("o") is not None:
                lines.append(
                    f"    chart: O={pt['o']} H={pt['h']} L={pt['l']} C={pt['c']} "
                    f"VWAP={pt['vwap']} MA={pt['ma_fast']}/{pt['ma_slow']} RSI={pt['rsi']}"
                )
                diffs = [
                    abs(o_ - float(pt["o"])),
                    abs(h_ - float(pt["h"])),
                    abs(l_ - float(pt["l"])),
                    abs(c_ - float(pt["c"])),
                    abs(vwap - float(pt["vwap"])),
                    abs(mf - float(pt["ma_fast"])),
                    abs(ms - float(pt["ma_slow"])),
                    abs(rr - float(pt["rsi"])),
                ]
                lines.append(
                    f"    diff: O={diffs[0]:.2f} H={diffs[1]:.2f} L={diffs[2]:.2f} "
                    f"C={diffs[3]:.2f} VWAP={diffs[4]:.2f} MAf={diffs[5]:.2f} "
                    f"MAs={diffs[6]:.2f} RSI={diffs[7]:.2f}"
                )
                total_abs += float(sum(diffs))
            else:
                lines.append(
                    f"    chart: VWAP={pt['vwap']} MA={pt['ma_fast']}/{pt['ma_slow']} "
                    f"RSI={pt['rsi']}"
                )
                diffs = [
                    abs(vwap - float(pt["vwap"])),
                    abs(mf - float(pt["ma_fast"])),
                    abs(ms - float(pt["ma_slow"])),
                    abs(rr - float(pt["rsi"])),
                ]
                lines.append(
                    f"    diff: VWAP={diffs[0]:.2f} MAf={diffs[1]:.2f} "
                    f"MAs={diffs[2]:.2f} RSI={diffs[3]:.2f}"
                )
                total_abs += float(sum(diffs))
        lines.append(f"  score_abs_sum={total_abs:.4f} (n_points={n_cmp})")
        lines.append("")
        if n_cmp > 0:
            scored.append(
                (total_abs, n_cmp, (ma_type, fast_l, slow_l, rsi_l, anchor))
            )

    if not scored:
        lines.insert(
            0,
            "PARITY FAILED - NO DATA AT REQUESTED TIMESTAMPS",
        )
        lines.append("BEST_PARITY skipped (n_points=0 for all configs).")
        lines.append("")
        return lines, 2

    scored.sort(key=lambda x: (x[0], -x[1]))
    best_score, best_n, best_cfg = scored[0]
    bm, bf, bs, br, ba = best_cfg
    lines.append(
        f"BEST_PARITY={bm} fast={bf} slow={bs} RSI={br} VWAP={ba} "
        f"score={best_score:.4f} n_points={best_n}"
    )
    lines.append("")

    cache = build_cache(df, bm, ba, [bf, bs], [br])
    sigs = generate_signals(df, cache, bf, bs, br, "both")
    trades = simulate_trades(
        df, sigs, 150.0, cutoff_map, date(2026, 9, 15), date(2026, 9, 16)
    )
    by_sig = {t.signal_i: t for t in trades}

    lines.append(
        f"===== SIGNALS {PARITY_SIGNAL_START} .. {PARITY_SIGNAL_END} IST "
        f"({bm} {bf}/{bs} RSI{br} {ba}) ====="
    )
    n_listed = 0
    for s in sigs:
        tm = ist_key(df["dt_ist"].iloc[s.i])
        if tm < PARITY_SIGNAL_START or tm > PARITY_SIGNAL_END:
            continue
        tr = by_sig.get(s.i)
        if tr is None:
            lines.append(
                f"  {tm} {s.side} entry=n/a gate={s.gate} exit=n/a pnl=n/a"
            )
        else:
            lines.append(
                f"  {tm} {s.side} entry={tr.entry_px:.1f}@{tr.entry_ts_ist} "
                f"gate={s.gate} exit={tr.label}@{tr.exit_ts_ist} pnl={tr.pnl:.2f}"
            )
        n_listed += 1
    if n_listed == 0:
        lines.append("  (no signals in window — check data coverage / warmup)")
    lines.append("")
    lines.append("BTC-only parity. Option marks not used.")
    return lines, 0


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------
def run_sweep(
    df: pd.DataFrame,
    ma_type: str,
    vwap_anchor: str,
    side_filter: SideFilter,
) -> tuple[list[str], pd.DataFrame]:
    lines: list[str] = []
    n_configs = len(FAST_GRID) * len(SLOW_GRID) * len(RSI_GRID) * len(TARGET_GRID)
    data_end: date = df["ist_date"].iloc[-1]
    cutoff_map = build_cutoff_map(df)

    lines.append("===== S004 SIGNAL SWEEP =====")
    lines.append(f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    lines.append(f"ma_type={ma_type} vwap_anchor={vwap_anchor} side_filter={side_filter}")
    lines.append(
        f"grid FAST={list(FAST_GRID)} SLOW={list(SLOW_GRID)} "
        f"RSI={list(RSI_GRID)} TARGET={list(TARGET_GRID)} STOP={STOP_PTS}"
    )
    lines.append(f"n_configs={n_configs}")
    lines.append(f"TRAIN={TRAIN_START}..{TRAIN_END}  TEST={TEST_START}..{data_end}")
    lines.append(f"spot_csv={find_spot_csv()}")
    lines.append("")

    cache = build_cache(
        df,
        ma_type,
        vwap_anchor,
        list(set(FAST_GRID) | set(SLOW_GRID)),
        list(RSI_GRID),
    )

    rows: list[dict[str, Any]] = []
    done = 0
    signal_combos = len(FAST_GRID) * len(SLOW_GRID) * len(RSI_GRID)
    for fast_l in FAST_GRID:
        for slow_l in SLOW_GRID:
            for rsi_l in RSI_GRID:
                sigs = generate_signals(df, cache, fast_l, slow_l, rsi_l, side_filter)
                done += 1
                if done % 25 == 0:
                    logger.info(
                        "signals %d/%d fast=%d slow=%d rsi=%d n_sig=%d",
                        done,
                        signal_combos,
                        fast_l,
                        slow_l,
                        rsi_l,
                        len(sigs),
                    )
                for tgt in TARGET_GRID:
                    all_tr = simulate_trades(
                        df, sigs, float(tgt), cutoff_map, TRAIN_START, data_end
                    )
                    train_tr = [
                        t for t in all_tr if TRAIN_START <= t.ist_day <= TRAIN_END
                    ]
                    test_tr = [
                        t for t in all_tr if TEST_START <= t.ist_day <= data_end
                    ]
                    for side_name, tr_tr, tr_te in (
                        ("BOTH", train_tr, test_tr),
                        (
                            "LONG",
                            filter_side(train_tr, "LONG"),
                            filter_side(test_tr, "LONG"),
                        ),
                        (
                            "SHORT",
                            filter_side(train_tr, "SHORT"),
                            filter_side(test_tr, "SHORT"),
                        ),
                    ):
                        seed = (
                            CONTROL_SEED
                            + fast_l * 100_000
                            + slow_l * 1_000
                            + rsi_l * 10
                            + int(tgt)
                            + (0 if side_name == "BOTH" else (1 if side_name == "LONG" else 2))
                        )
                        st_tr = compute_stats(
                            df,
                            tr_tr,
                            float(tgt),
                            TRAIN_START,
                            TRAIN_END,
                            cutoff_map,
                            seed,
                        )
                        st_te = compute_stats(
                            df,
                            tr_te,
                            float(tgt),
                            TEST_START,
                            data_end,
                            cutoff_map,
                            seed + 7,
                        )
                        rows.append(
                            {
                                "ma_type": ma_type,
                                "vwap_anchor": vwap_anchor,
                                "side_bucket": side_name,
                                "fast": fast_l,
                                "slow": slow_l,
                                "rsi_len": rsi_l,
                                "target": tgt,
                                "train_n": st_tr.n_trades,
                                "train_p_right": st_tr.p_right,
                                "train_p_wrong": st_tr.p_wrong,
                                "train_p_flip": st_tr.p_flip,
                                "train_p_cutoff": st_tr.p_cutoff,
                                "train_mean_pnl": st_tr.mean_pnl,
                                "train_total_pnl": st_tr.total_pnl,
                                "train_max_consec_loss": st_tr.max_consec_loss,
                                "train_trades_per_day": st_tr.trades_per_day,
                                "train_control_mean": st_tr.control_mean,
                                "train_control_p95": st_tr.control_p95,
                                "train_edge_pp": st_tr.edge_pp,
                                "train_beats_control": st_tr.beats_control,
                                "test_n": st_te.n_trades,
                                "test_p_right": st_te.p_right,
                                "test_p_wrong": st_te.p_wrong,
                                "test_p_flip": st_te.p_flip,
                                "test_p_cutoff": st_te.p_cutoff,
                                "test_mean_pnl": st_te.mean_pnl,
                                "test_total_pnl": st_te.total_pnl,
                                "test_max_consec_loss": st_te.max_consec_loss,
                                "test_trades_per_day": st_te.trades_per_day,
                                "test_control_mean": st_te.control_mean,
                                "test_control_p95": st_te.control_p95,
                                "test_edge_pp": st_te.edge_pp,
                                "test_beats_control": st_te.beats_control,
                            }
                        )

    rdf = pd.DataFrame(rows)
    lines.append(f"total_configs={n_configs}")
    lines.append(f"total_config_side_rows={len(rdf)}")
    lines.append("")

    def summarize_bucket(bucket: str) -> list[str]:
        out: list[str] = []
        sub = rdf[rdf["side_bucket"] == bucket].copy()
        out.append(f"===== SUMMARY side_bucket={bucket} =====")
        eligible = sub[sub["train_n"] >= MIN_TRAIN_TRADES]
        beats = eligible[eligible["train_beats_control"] == True]  # noqa: E712
        n_elig = len(eligible)
        n_beats = len(beats)
        expected = n_elig * 0.05
        out.append(f"total_rows={len(sub)}  train_n>={MIN_TRAIN_TRADES}: {n_elig}")
        out.append(
            f"TRAIN beats_control={n_beats}  expected_by_chance={expected:.1f} "
            f"(eligible x 0.05)"
        )
        top = eligible.sort_values("train_edge_pp", ascending=False).head(20)
        out.append("----- TOP 20 by TRAIN edge_pp (with TEST) -----")
        for rank, (_, r) in enumerate(top.iterrows(), start=1):
            out.append(
                f"  #{rank} {r['ma_type']} {int(r['fast'])}/{int(r['slow'])} "
                f"RSI{int(r['rsi_len'])} T{int(r['target'])} {r['vwap_anchor']} | "
                f"TRAIN n={int(r['train_n'])} P(R)={r['train_p_right']:.4f} "
                f"edge_pp={r['train_edge_pp']:.2f} beats={r['train_beats_control']} | "
                f"TEST n={int(r['test_n'])} P(R)={r['test_p_right']:.4f} "
                f"edge_pp={r['test_edge_pp']:.2f} beats={r['test_beats_control']}"
            )
        top20_test_beats = int(top["test_beats_control"].sum()) if len(top) else 0
        out.append(
            f"TRAIN top-20 also TEST beats_control: {top20_test_beats} / {len(top)}"
        )
        out.append("----- BASELINE EMA 9/21 RSI14 T150 -----")
        b = sub[
            (sub["fast"] == BASELINE["fast"])
            & (sub["slow"] == BASELINE["slow"])
            & (sub["rsi_len"] == BASELINE["rsi_len"])
            & (sub["target"] == BASELINE["target"])
            & (sub["ma_type"] == BASELINE["ma_type"])
        ]
        if len(b) == 0:
            out.append("  (baseline not in this run — check --ma-type)")
        else:
            r = b.iloc[0]
            out.append(
                f"  TRAIN n={int(r['train_n'])} P(R)={r['train_p_right']:.4f} "
                f"edge_pp={r['train_edge_pp']:.2f} beats={r['train_beats_control']} | "
                f"TEST n={int(r['test_n'])} P(R)={r['test_p_right']:.4f} "
                f"edge_pp={r['test_edge_pp']:.2f} beats={r['test_beats_control']}"
            )
        out.append("")
        return out

    for bucket in ("BOTH", "LONG", "SHORT"):
        lines.extend(summarize_bucket(bucket))

    lines.append(
        "BTC-only. Option cost alag: bucketed slip pe breakeven 54.1%, random 30.6%"
    )
    lines.append("")
    return lines, rdf


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def emit_file(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("wrote %s (%d lines)", path, len(lines))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S004 BTC-only signal sweep / parity")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--parity", action="store_true")
    mode.add_argument("--sweep", action="store_true")
    ap.add_argument("--ma-type", choices=("EMA", "SMA"), default="EMA")
    ap.add_argument("--vwap-anchor", choices=("UTC00", "IST00"), default="UTC00")
    ap.add_argument("--side", choices=("both", "long", "short"), default="both")
    ap.add_argument(
        "--parity-ts",
        action="append",
        default=None,
        metavar="YYYY-MM-DD HH:MM",
        help=(
            "IST candle OPEN timestamp for parity (repeatable). "
            f"Default: {list(DEFAULT_PARITY_TS)}"
        ),
    )
    args = ap.parse_args()

    df = load_ohlcv()
    if args.parity:
        ts_list = (
            [_parse_parity_ts(t) for t in args.parity_ts]
            if args.parity_ts
            else list(DEFAULT_PARITY_TS)
        )
        lines, code = run_parity(df, ts_list)
        emit_file(PARITY_OUT, lines)
        if code != 0:
            raise SystemExit(code)
        return

    lines, rdf = run_sweep(df, args.ma_type, args.vwap_anchor, args.side)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rdf.to_csv(SWEEP_CSV, index=False)
    logger.info("wrote %s (%d rows)", SWEEP_CSV, len(rdf))
    emit_file(SWEEP_TXT, lines)


if __name__ == "__main__":
    main()
