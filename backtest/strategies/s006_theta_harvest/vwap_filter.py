"""S006-C Smith VWAP mid + swing filter (5m bars). Standalone, no strategy imports."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger("strategies.s006.vwap_filter")

IST = "Asia/Kolkata"
VWAP_WINDOW = 60  # 5m bars = 300 minutes
BAR_MINUTES = 5


def _ensure_ist_df(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Normalize 1m OHLCV to IST-indexed frame with required columns."""
    df = df_1m.copy()
    if "open_time_ist" in df.columns:
        ts = pd.to_datetime(df["open_time_ist"].astype(str).str.replace(" IST", "", regex=False))
        df["_ts"] = ts.dt.tz_localize(IST, ambiguous="infer", nonexistent="shift_forward")
    elif "open_time_unix" in df.columns:
        df["_ts"] = pd.to_datetime(df["open_time_unix"], unit="s", utc=True).dt.tz_convert(IST)
    else:
        raise ValueError("df_1m needs open_time_ist or open_time_unix")
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            raise ValueError(f"df_1m missing column {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("_ts")
    return df


def aggregate_5m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m bars to 5m IST OHLCV. Drops incomplete trailing bars (<5 1m rows)."""
    df = _ensure_ist_df(df_1m)
    df = df.set_index("_ts")
    # Floor to 5-minute open
    g = df.groupby(pd.Grouper(freq=f"{BAR_MINUTES}min", label="left", closed="left"))
    out = g.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        n_1m=("close", "count"),
    )
    out = out.dropna(subset=["open", "high", "low", "close"])
    # Complete bars only (exactly 5 one-minute prints)
    out = out[out["n_1m"] >= BAR_MINUTES].drop(columns=["n_1m"])
    out["bar_open_ist"] = out.index
    out["bar_close_ist"] = out.index + pd.Timedelta(minutes=BAR_MINUTES)
    return out.reset_index(drop=True)


def compute_smith_vwap_mid(bars_5m: pd.DataFrame, window: int = VWAP_WINDOW) -> pd.Series:
    """Volume-weighted geometric mean of closes over rolling `window` bars.

    mid[i] = exp( sum(log(close[j])*vol[j]) / sum(vol[j]) ) for j in [i-window+1, i]
    First (window-1) bars are NaN.
    """
    import numpy as np

    closes = bars_5m["close"].astype(float).to_numpy()
    vols = bars_5m["volume"].astype(float).fillna(0.0).to_numpy()
    n = len(closes)
    mid = np.full(n, np.nan, dtype=float)
    if n == 0:
        return pd.Series(mid, index=bars_5m.index, name="mid")
    log_c = np.where(closes > 0, np.log(closes), np.nan)
    wlog = log_c * vols
    # cumulative sums; nan breaks windows that contain invalid closes
    valid = np.isfinite(wlog) & np.isfinite(vols)
    wlog_clean = np.where(valid, wlog, 0.0)
    vol_clean = np.where(valid, vols, 0.0)
    invalid_count = (~valid).astype(int)
    c_wlog = np.concatenate([[0.0], np.cumsum(wlog_clean)])
    c_vol = np.concatenate([[0.0], np.cumsum(vol_clean)])
    c_inv = np.concatenate([[0], np.cumsum(invalid_count)])
    for i in range(window - 1, n):
        lo = i - window + 1
        if c_inv[i + 1] - c_inv[lo] > 0:
            continue
        den = c_vol[i + 1] - c_vol[lo]
        if den <= 0:
            continue
        num = c_wlog[i + 1] - c_wlog[lo]
        mid[i] = math.exp(num / den)
    return pd.Series(mid, index=bars_5m.index, name="mid")


@dataclass
class _OpenSegment:
    kind: str  # "up" | "down"
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    mids: list[float] = field(default_factory=list)
    start_i: int = 0


@dataclass
class SwingState:
    position: str | None = None  # 'above' | 'below'
    last_confirmed_swing_high: float | None = None
    last_confirmed_swing_low: float | None = None
    swing_high_vwap: float | None = None
    swing_low_vwap: float | None = None
    segment: _OpenSegment | None = None
    day: date | None = None


def _reset_swing_state(st: SwingState, day: date) -> None:
    st.position = None
    st.last_confirmed_swing_high = None
    st.last_confirmed_swing_low = None
    st.swing_high_vwap = None
    st.swing_low_vwap = None
    st.segment = None
    st.day = day


def _confirm_up_segment(st: SwingState, seg: _OpenSegment) -> None:
    """Confirm swing high from an above-mid segment (called on cross back below)."""
    if not seg.highs:
        return
    best_i = max(range(len(seg.highs)), key=lambda i: (seg.highs[i], i))
    st.last_confirmed_swing_high = float(seg.highs[best_i])
    st.swing_high_vwap = float(seg.mids[best_i])


def _confirm_down_segment(st: SwingState, seg: _OpenSegment) -> None:
    if not seg.lows:
        return
    best_i = min(range(len(seg.lows)), key=lambda i: (seg.lows[i], i))
    st.last_confirmed_swing_low = float(seg.lows[best_i])
    st.swing_low_vwap = float(seg.mids[best_i])


def update_swing_on_bar(
    st: SwingState,
    *,
    bar_day: date,
    close: float,
    high: float,
    low: float,
    mid: float,
    bar_i: int,
) -> None:
    """Update swing state with one CLOSED 5m bar. No look-ahead."""
    if st.day != bar_day:
        _reset_swing_state(st, bar_day)

    if mid != mid or math.isnan(mid):  # NaN mid — no signal/swing updates
        return

    new_pos = "above" if close > mid else "below"

    if st.position is None:
        st.position = new_pos
        if new_pos == "above":
            st.segment = _OpenSegment(kind="up", highs=[high], lows=[low], mids=[mid], start_i=bar_i)
        else:
            st.segment = _OpenSegment(kind="down", highs=[high], lows=[low], mids=[mid], start_i=bar_i)
        return

    if new_pos == st.position:
        # Extend open segment
        if st.segment is not None:
            st.segment.highs.append(high)
            st.segment.lows.append(low)
            st.segment.mids.append(mid)
        return

    # Cross: confirm previous segment, start new
    if st.position == "above" and new_pos == "below":
        # Was above; close went below -> confirm swing high
        if st.segment is not None and st.segment.kind == "up":
            _confirm_up_segment(st, st.segment)
        st.segment = _OpenSegment(kind="down", highs=[high], lows=[low], mids=[mid], start_i=bar_i)
    elif st.position == "below" and new_pos == "above":
        if st.segment is not None and st.segment.kind == "down":
            _confirm_down_segment(st, st.segment)
        st.segment = _OpenSegment(kind="up", highs=[high], lows=[low], mids=[mid], start_i=bar_i)

    st.position = new_pos


def _parse_hm(s: str) -> time:
    s = s.strip().replace(":", "")
    if len(s) == 3:
        s = "0" + s
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f"bad HHMM time: {s!r}")
    return time(int(s[:2]), int(s[2:]))


def get_vwap_signals(
    df_1m: pd.DataFrame,
    day: date,
    window_start: str = "09:00",
    window_end: str = "15:00",
) -> list[dict[str, Any]]:
    """Return VWAP-touch entry signals for `day` inside [window_start, window_end] IST.

    Each signal dict:
      ts_ist, mid, swing_high, swing_low, swing_high_vwap, swing_low_vwap,
      bars_since_reset
    Only uses closed 5m bars. Swing values are last *confirmed* (not live segment).
    """
    t0 = _parse_hm(window_start)
    t1 = _parse_hm(window_end)

    # Use only this IST calendar day's 1m bars (swing reset at 00:00).
    df = _ensure_ist_df(df_1m)
    day_mask = df["_ts"].dt.date == day
    day_1m = df.loc[day_mask].copy()
    if day_1m.empty:
        return []

    bars = aggregate_5m(day_1m)
    if bars.empty:
        return []
    bars["mid"] = compute_smith_vwap_mid(bars)

    st = SwingState()
    _reset_swing_state(st, day)
    signals: list[dict[str, Any]] = []
    bars_since_reset = 0

    for i, row in bars.iterrows():
        bars_since_reset += 1
        mid = float(row["mid"]) if row["mid"] == row["mid"] else float("nan")
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        bar_close: pd.Timestamp = row["bar_close_ist"]
        bar_day = bar_close.tz_convert(IST).date() if bar_close.tzinfo else bar_close.date()

        update_swing_on_bar(
            st,
            bar_day=bar_day,
            close=close,
            high=high,
            low=low,
            mid=mid,
            bar_i=int(i) if isinstance(i, (int,)) else bars_since_reset - 1,
        )

        # Window check on bar close time
        hm = bar_close.tz_convert(IST).time() if bar_close.tzinfo else bar_close.time()
        if hm < t0 or hm > t1:
            continue
        if mid != mid or math.isnan(mid):
            continue
        if st.last_confirmed_swing_high is None or st.last_confirmed_swing_low is None:
            continue
        sh = float(st.last_confirmed_swing_high)
        sl = float(st.last_confirmed_swing_low)
        shv = float(st.swing_high_vwap) if st.swing_high_vwap is not None else float("nan")
        slv = float(st.swing_low_vwap) if st.swing_low_vwap is not None else float("nan")
        # Construction guards — must hold; silent skip is forbidden
        if not (sh > shv):
            raise AssertionError(
                f"swing_high guard failed day={day} ts={bar_close}: "
                f"swing_high={sh} swing_high_vwap={shv}"
            )
        if not (sl < slv):
            raise AssertionError(
                f"swing_low guard failed day={day} ts={bar_close}: "
                f"swing_low={sl} swing_low_vwap={slv}"
            )
        # VWAP touch: mid inside bar range
        if not (low <= mid <= high):
            continue

        ts_ist = bar_close.tz_convert(IST).to_pydatetime()
        signals.append(
            {
                "ts_ist": ts_ist,
                "mid": mid,
                "swing_high": sh,
                "swing_low": sl,
                "swing_high_vwap": shv,
                "swing_low_vwap": slv,
                "bars_since_reset": bars_since_reset,
            }
        )

    return signals


def diagnose_no_signal(
    df_1m: pd.DataFrame,
    day: date,
    window_start: str = "09:00",
    window_end: str = "15:00",
) -> dict[str, Any]:
    """Explain why a day produced zero signals (for smoke reports)."""
    t0 = _parse_hm(window_start)
    t1 = _parse_hm(window_end)
    df = _ensure_ist_df(df_1m)
    day_1m = df.loc[df["_ts"].dt.date == day].copy()
    if day_1m.empty:
        return {"reason": "no_1m_data", "detail": f"no 1m rows for {day}"}
    bars = aggregate_5m(day_1m)
    if bars.empty:
        return {"reason": "no_5m_bars", "detail": "aggregate_5m empty"}
    bars["mid"] = compute_smith_vwap_mid(bars)
    st = SwingState()
    _reset_swing_state(st, day)
    n_window = 0
    n_mid_ok = 0
    n_both_swings = 0
    n_touch = 0
    first_missing = "both_swings"
    for i, row in bars.iterrows():
        mid = float(row["mid"]) if row["mid"] == row["mid"] else float("nan")
        update_swing_on_bar(
            st,
            bar_day=day,
            close=float(row["close"]),
            high=float(row["high"]),
            low=float(row["low"]),
            mid=mid,
            bar_i=0,
        )
        hm = row["bar_close_ist"].tz_convert(IST).time()
        if hm < t0 or hm > t1:
            continue
        n_window += 1
        if mid != mid:
            continue
        n_mid_ok += 1
        if st.last_confirmed_swing_high is None or st.last_confirmed_swing_low is None:
            continue
        n_both_swings += 1
        if float(row["low"]) <= mid <= float(row["high"]):
            n_touch += 1
    if n_window == 0:
        first_missing = "no_bars_in_window"
    elif n_mid_ok == 0:
        first_missing = "mid_nan"
    elif n_both_swings == 0:
        first_missing = "missing_confirmed_swing"
    elif n_touch == 0:
        first_missing = "no_vwap_touch"
    else:
        first_missing = "unknown_had_touches"
    return {
        "reason": first_missing,
        "n_window_bars": n_window,
        "n_mid_ok": n_mid_ok,
        "n_both_swings": n_both_swings,
        "n_touch": n_touch,
        "had_swing_high": st.last_confirmed_swing_high is not None,
        "had_swing_low": st.last_confirmed_swing_low is not None,
    }


_DF_1M_CACHE: pd.DataFrame | None = None


def load_1m_csv(path: Path | None = None) -> pd.DataFrame:
    global _DF_1M_CACHE
    if path is None and _DF_1M_CACHE is not None:
        return _DF_1M_CACHE
    if path is None:
        from backtest.harness.config import DATA_1M_DIR

        files = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
        if not files:
            raise FileNotFoundError("No BTCUSD_1m_*.csv in data_1m")
        path = files[-1]
        df = pd.read_csv(path)
        _DF_1M_CACHE = df
        return df
    return pd.read_csv(path)


def self_test_print_swings(days: list[date] | None = None) -> None:
    """Print confirmed swings for a few days for manual chart check."""
    if days is None:
        days = [date(2025, 11, 3), date(2025, 11, 4), date(2025, 11, 5)]
    df = load_1m_csv()
    for d in days:
        sigs = get_vwap_signals(df, d, "09:00", "15:00")
        diag = diagnose_no_signal(df, d, "09:00", "15:00")
        print(f"=== {d} signals={len(sigs)} diag={diag.get('reason')} ===")
        if sigs:
            s0 = sigs[0]
            print(
                f"  first: ts={s0['ts_ist']} mid={s0['mid']:.2f} "
                f"SH={s0['swing_high']:.2f}@{s0['swing_high_vwap']:.2f} "
                f"SL={s0['swing_low']:.2f}@{s0['swing_low_vwap']:.2f} "
                f"bars={s0['bars_since_reset']}"
            )
            if len(sigs) > 1:
                print(f"  (+{len(sigs)-1} more signals logged, first used)")


def test_no_lookahead(
    df_1m: pd.DataFrame | None = None,
    days: list[date] | None = None,
    window_start: str = "09:00",
    window_end: str = "15:00",
) -> None:
    """STEP 6: each signal at T must reproduce when data is truncated at T.

    Raises AssertionError on any mismatch.
    """
    if df_1m is None:
        df_1m = load_1m_csv()
    if days is None:
        days = [date(2025, 11, 3), date(2025, 11, 4), date(2025, 11, 5), date(2025, 11, 6)]

    df = _ensure_ist_df(df_1m)
    mismatches = 0
    checked = 0
    for d in days:
        full_sigs = get_vwap_signals(df, d, window_start, window_end)
        for sig in full_sigs:
            T: datetime = sig["ts_ist"]
            if T.tzinfo is None:
                T = pd.Timestamp(T).tz_localize(IST).to_pydatetime()
            # Truncate: only 1m bars whose open is strictly before T (bar closed by T)
            t_ts = pd.Timestamp(T)
            if t_ts.tzinfo is None:
                t_ts = t_ts.tz_localize(IST)
            truncated = df.loc[df["_ts"] < t_ts].copy()
            # Drop helper col before re-entry? get_vwap_signals re-normalizes
            trunc_raw = truncated.drop(columns=["_ts"], errors="ignore")
            # Rebuild minimal columns from original
            cols = [c for c in df_1m.columns if c in truncated.columns or c == "open_time_ist"]
            # Use truncated with original-like columns
            rebuild = truncated.copy()
            rebuild["open_time_ist"] = rebuild["_ts"].dt.strftime("%Y-%m-%d %H:%M:%S") + " IST"
            for c in ("open", "high", "low", "close", "volume"):
                rebuild[c] = truncated[c]
            redo = get_vwap_signals(rebuild, d, window_start, window_end)
            checked += 1
            if not redo:
                mismatches += 1
                print(f"FAIL day={d} T={T}: truncated produced ZERO signals, expected one at T")
                print(f"  expected={sig}")
                continue
            # First signal on truncated series that ends at T should be this signal
            # (may have earlier signals too — find matching T)
            match = None
            for r in redo:
                rt = r["ts_ist"]
                if pd.Timestamp(rt).tz_convert(IST) == pd.Timestamp(T).tz_convert(IST):
                    match = r
                    break
            if match is None:
                mismatches += 1
                print(
                    f"FAIL day={d} T={T}: truncated signals missing T. "
                    f"got={[str(x['ts_ist']) for x in redo[:5]]}"
                )
                continue
            for key in ("mid", "swing_high", "swing_low", "swing_high_vwap", "swing_low_vwap"):
                a, b = float(sig[key]), float(match[key])
                if abs(a - b) > 1e-6:
                    mismatches += 1
                    print(
                        f"FAIL day={d} T={T} key={key}: full={a} truncated={b}"
                    )
                    break
        # Also: no signal should appear that wasn't in full (on truncated-to-last-full-bar)
        # Covered by per-signal check above.

    if mismatches:
        raise AssertionError(
            f"look-ahead test FAILED: {mismatches} mismatches out of {checked} signals checked"
        )
    print(f"LOOK-AHEAD TEST PASS: checked={checked} signals across {len(days)} days")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    self_test_print_swings()
    test_no_lookahead()
