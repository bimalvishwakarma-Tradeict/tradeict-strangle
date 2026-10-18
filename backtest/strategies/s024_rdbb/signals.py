"""S024 RDBB: RSI divergence + causal Bollinger breakout, optional pre-move filter."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from backtest.strategies.s020_vwap_touch.rsi_div import detect_rsi_div
from backtest.strategies.s022_ulp_long_strangle.signals import tf_seconds

IST = ZoneInfo("Asia/Kolkata")
BB_LEN = 20
BB_K = 2.0
TFS = ("5m", "15m", "30m")
RSI_SETS: dict[str, dict[str, float | int]] = {
    "R3": {"n": 3, "ob": 80.0, "os": 20.0, "exp_obh": 50.0, "exp_obl": 50.0},
    "R14": {"n": 14, "ob": 70.0, "os": 30.0, "exp_obh": 40.0, "exp_obl": 60.0},
}
MOVE_N = (4, 6, 8, 10, 12, 14, 16, 18, 20)
MOVE_X = (1.0, 1.5, 2.0)
NOFILTER = "nofilter"
FILTERS: tuple[str, ...] = (NOFILTER,) + tuple(
    f"N{n}_X{x:g}" for n in MOVE_N for x in MOVE_X
)


def parse_filter(tag: str) -> tuple[int, float] | None:
    s = str(tag).strip()
    if s == NOFILTER:
        return None
    if not s.startswith("N") or "_X" not in s:
        raise ValueError(f"bad filter {tag!r}")
    a, b = s[1:].split("_X", 1)
    return int(a), float(b)


def bollinger(
    close: np.ndarray,
    n: int = BB_LEN,
    k: float = BB_K,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Causal SMA ± k * population stdev (ddof=0, TradingView ta.stdev)."""
    c = np.asarray(close, dtype=np.float64)
    nn = int(n)
    sma = np.full(c.shape[0], np.nan, dtype=np.float64)
    upper = np.full(c.shape[0], np.nan, dtype=np.float64)
    lower = np.full(c.shape[0], np.nan, dtype=np.float64)
    if nn < 1 or c.size < nn:
        return sma, upper, lower
    win = np.lib.stride_tricks.sliding_window_view(c, nn)
    m = win.mean(axis=1)
    s = win.std(axis=1, ddof=0)
    sma[nn - 1 :] = m
    upper[nn - 1 :] = m + float(k) * s
    lower[nn - 1 :] = m - float(k) * s
    return sma, upper, lower


def bb_side_ok(
    side: str,
    high: float,
    low: float,
    upper: float,
    lower: float,
) -> bool:
    if not (math_finite(upper) and math_finite(lower)):
        return False
    s = str(side).lower()
    if s == "short":
        return float(high) > float(upper)
    if s == "long":
        return float(low) < float(lower)
    return False


def math_finite(x: float) -> bool:
    return x == x and abs(x) != float("inf")


def move_range_frac(high: np.ndarray, low: np.ndarray, close: np.ndarray, j: int, n: int) -> float:
    i0 = int(j) - int(n) + 1
    if i0 < 0 or int(j) >= int(close.size):
        return float("nan")
    cl = float(close[j])
    if cl <= 0.0:
        return float("nan")
    return (float(np.max(high[i0 : int(j) + 1])) - float(np.min(low[i0 : int(j) + 1]))) / cl


def passes_move_filter(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    j: int,
    filt: str,
) -> bool:
    parsed = parse_filter(filt)
    if parsed is None:
        return True
    n, x_pct = parsed
    rng = move_range_frac(high, low, close, j, n)
    if not math_finite(rng):
        return False
    return float(rng) >= float(x_pct) / 100.0


def weekend_bucket(ts: int) -> str:
    """WEEKEND: IST Fri 12:00 inclusive through Sun 04:00 exclusive."""
    dt = datetime.fromtimestamp(int(ts), tz=IST)
    wd = int(dt.weekday())
    minutes = int(dt.hour) * 60 + int(dt.minute)
    if wd == 4 and minutes >= 12 * 60:
        return "WEEKEND"
    if wd == 5:
        return "WEEKEND"
    if wd == 6 and minutes < 4 * 60:
        return "WEEKEND"
    return "WEEKDAY"


def apply_move_filter(
    bars: list[dict[str, Any]],
    sigs: list[dict[str, Any]],
    filt: str,
) -> list[dict[str, Any]]:
    """Keep BB+RSI signals whose pre-signal range meets filt (causal)."""
    if str(filt) == NOFILTER:
        out = []
        for e in sigs:
            row = dict(e)
            row["filter"] = NOFILTER
            out.append(row)
        return out
    parse_filter(filt)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    out: list[dict[str, Any]] = []
    for e in sigs:
        j = int(e["bar_j"])
        if not passes_move_filter(h, l, c, j, filt):
            continue
        row = dict(e)
        row["filter"] = filt
        out.append(row)
    return out


def iter_rdbb_signals(
    bars: list[dict[str, Any]],
    tf: str,
    rsi_set: str,
    filt: str = NOFILTER,
) -> list[dict[str, Any]]:
    """Closed-bar RSI-div filtered by BB breakout and optional pre-move range."""
    if rsi_set not in RSI_SETS:
        raise ValueError(f"unsupported rsi_set {rsi_set!r}")
    parse_filter(filt)
    tf_sec = tf_seconds(tf)
    if not bars:
        return []
    ts = np.array([int(b["time"]) for b in bars], dtype=np.int64)
    o = np.array([float(b["open"]) for b in bars], dtype=np.float64)
    h = np.array([float(b["high"]) for b in bars], dtype=np.float64)
    l = np.array([float(b["low"]) for b in bars], dtype=np.float64)
    c = np.array([float(b["close"]) for b in bars], dtype=np.float64)
    cfg = RSI_SETS[rsi_set]
    rsi_sigs, _z = detect_rsi_div(
        ts, o, h, l, c, tf_sec,
        n=int(cfg["n"]), ob=float(cfg["ob"]), os=float(cfg["os"]),
        exp_obh=float(cfg["exp_obh"]), exp_obl=float(cfg["exp_obl"]),
    )
    _sma, upper, lower = bollinger(c, BB_LEN, BB_K)
    t_index = {int(b["time"]): i for i, b in enumerate(bars)}
    out: list[dict[str, Any]] = []
    for sig in rsi_sigs:
        close_ts = int(sig["ts"])
        j = t_index.get(int(close_ts) - int(tf_sec))
        if j is None:
            continue
        if not bb_side_ok(
            str(sig["side"]), float(h[j]), float(l[j]), float(upper[j]), float(lower[j]),
        ):
            continue
        if not passes_move_filter(h, l, c, j, filt):
            continue
        out.append(
            {
                "signal_ts": close_ts,
                "entry_ts": close_ts,
                "signal_price": float(c[j]),
                "tf": tf,
                "rsi_set": rsi_set,
                "filter": filt,
                "side": str(sig["side"]),
                "bar_j": int(j),
                "high": float(h[j]),
                "low": float(l[j]),
                "close": float(c[j]),
                "bb_upper": float(upper[j]),
                "bb_lower": float(lower[j]),
                "bucket": weekend_bucket(close_ts),
                "ob_level": float(sig.get("ob_level", 0.0) or 0.0),
                "sig_rsi": float(sig.get("sig_rsi", float("nan"))),
            }
        )
    return out
