#!/usr/bin/env python3
"""S022 Stage 0: does ULP fresh-touch predict a bigger BTC move than random times?

Spot only. No option sim.

python backtest/strategies/s022_ulp_long_strangle/stage0_move_edge.py
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

IST = ZoneInfo("Asia/Kolkata")
IST_OFF = 19800
SEED = 22
N_CTRL = 20
N_PERM = 10_000
MIN_SEP = 3600
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
H_KEYS = ("1h", "2h", "4h", "8h", "1730")
H_SEC = {"1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800}
METRICS = ("mfe_abs", "end_abs", "rv", "hit_1000", "hit_1500", "hit_2000", "zone_crossed")
VOL_METRICS = ("mfe_abs", "end_abs", "rv", "hit_1000", "hit_1500", "hit_2000")
RV_ANN = math.sqrt(365.25 * 24.0 * 60.0)
N_VOL_BINS = 20
H_MINUTES = {"1h": 60, "2h": 120, "4h": 240, "8h": 480}
PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
RANGE_YM_LO = 202407
RANGE_YM_HI = 202609


def next_1730_ist(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST)
    cand = dt.replace(hour=17, minute=30, second=0, microsecond=0)
    unix = int(cand.astimezone(timezone.utc).timestamp())
    if unix > int(ts):
        return unix
    return int((cand + timedelta(days=1)).astimezone(timezone.utc).timestamp())


def window_end(signal_ts: int, h_key: str) -> int:
    if h_key == "1730":
        return next_1730_ist(int(signal_ts))
    return int(signal_ts) + int(H_SEC[h_key])


def ist_hod(ts: int) -> int:
    return int(((int(ts) + IST_OFF) // 3600) % 24)


def ist_ym(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts) + IST_OFF, tz=timezone.utc)
    return int(dt.year) * 100 + int(dt.month)


def ist_ym_arr(ts: np.ndarray) -> np.ndarray:
    shifted = (ts.astype(np.int64) + IST_OFF).astype("datetime64[s]")
    months = shifted.astype("datetime64[M]").astype(np.int64)
    year = months // 12 + 1970
    month = months % 12 + 1
    return (year * 100 + month).astype(np.int32)


def ist_hod_arr(ts: np.ndarray) -> np.ndarray:
    return (((ts.astype(np.int64) + IST_OFF) // 3600) % 24).astype(np.int16)


def collapse_events(sigs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One event per (tf, mode, signal_ts); keep every touched zone for crossing."""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, int]] = []
    for s in sigs:
        if str(s.get("rule")) != "fresh":
            continue
        key = (str(s["tf"]), str(s["mode"]), int(s["signal_ts"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)
    out: list[dict[str, Any]] = []
    for key in order:
        rows = groups[key]
        zones = [
            (str(r["zone_kind"]), float(r["zone_top"]), float(r["zone_bottom"]))
            for r in rows
        ]
        kinds = {z[0] for z in zones}
        r0 = rows[0]
        sts = int(r0["signal_ts"])
        out.append(
            {
                "tf": str(r0["tf"]),
                "mode": str(r0["mode"]),
                "signal_ts": sts,
                "signal_ist": str(r0.get("signal_ist") or ""),
                "entry": float(r0["spot_close"]),
                "zone_kind": "both" if len(kinds) > 1 else str(r0["zone_kind"]),
                "zone_top": float(r0["zone_top"]),
                "zone_bottom": float(r0["zone_bottom"]),
                "zones": zones,
                "n_raw": len(rows),
                "hod": ist_hod(sts),
                "ym": ist_ym(sts),
            }
        )
    return out


def forbidden_mask(ts_1m: np.ndarray, sig_times: np.ndarray, sep: int = MIN_SEP) -> np.ndarray:
    """True where a 1m timestamp is within ±sep of any signal time."""
    n = int(ts_1m.size)
    if n == 0:
        return np.zeros(0, dtype=bool)
    sig = np.unique(np.asarray(sig_times, dtype=np.int64))
    if sig.size == 0:
        return np.zeros(n, dtype=bool)
    t = ts_1m.astype(np.int64)
    idx = np.searchsorted(sig, t)
    prev_i = np.clip(idx - 1, 0, sig.size - 1)
    next_i = np.clip(idx, 0, sig.size - 1)
    dist = np.minimum(np.abs(t - sig[prev_i]), np.abs(sig[next_i] - t))
    return dist <= int(sep)


def sample_control_times(
    pool_ts: np.ndarray,
    n: int,
    rng: np.random.Generator,
) -> np.ndarray:
    if pool_ts.size == 0 or n <= 0:
        return np.zeros(0, dtype=np.int64)
    if pool_ts.size <= n:
        return pool_ts.astype(np.int64)
    pick = rng.choice(pool_ts.size, size=int(n), replace=False)
    return pool_ts[pick].astype(np.int64)


def _zone_crossed(hi: np.ndarray, lo: np.ndarray, zones: list[tuple[str, float, float]]) -> float:
    if hi.size == 0 or not zones:
        return 0.0
    for kind, top, bot in zones:
        if kind == "high" and float(np.max(hi)) >= float(top):
            return 1.0
        if kind == "low" and float(np.min(lo)) <= float(bot):
            return 1.0
    return 0.0


def window_metrics(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    cl: np.ndarray,
    t0: int,
    t1: int,
    entry: float,
    zones: list[tuple[str, float, float]] | None,
) -> dict[str, float]:
    """Metrics on 1m bars with t0 < ts <= t1. Spike AT t0 must not count."""
    nan = float("nan")
    empty = {
        "mfe_abs": nan,
        "end_abs": nan,
        "rv": nan,
        "hit_1000": nan,
        "hit_1500": nan,
        "hit_2000": nan,
        "zone_crossed": nan,
        "n_bars": 0.0,
    }
    i0 = int(np.searchsorted(ts, int(t0), side="right"))
    i1 = int(np.searchsorted(ts, int(t1), side="right"))
    if i1 <= i0:
        return empty
    h = hi[i0:i1]
    l = lo[i0:i1]
    c = cl[i0:i1]
    mx = float(np.max(h))
    mn = float(np.min(l))
    mfe = max(mx - float(entry), float(entry) - mn)
    end_abs = abs(float(c[-1]) - float(entry))
    if c.size >= 2:
        rets = np.diff(np.log(np.clip(c.astype(np.float64), 1e-12, None)))
        sd = float(np.std(rets, ddof=1)) if rets.size >= 2 else (
            float(np.std(rets, ddof=0)) if rets.size == 1 else nan
        )
        rv = sd * RV_ANN if math.isfinite(sd) else nan
    else:
        rv = nan
    zc = _zone_crossed(h, l, zones or [])
    return {
        "mfe_abs": mfe,
        "end_abs": end_abs,
        "rv": rv,
        "hit_1000": 1.0 if mfe >= 1000.0 else 0.0,
        "hit_1500": 1.0 if mfe >= 1500.0 else 0.0,
        "hit_2000": 1.0 if mfe >= 2000.0 else 0.0,
        "zone_crossed": zc,
        "n_bars": float(c.size),
    }


def metrics_all_h(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    cl: np.ndarray,
    t0: int,
    entry: float,
    zones: list[tuple[str, float, float]] | None,
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for hk in H_KEYS:
        out[hk] = window_metrics(ts, hi, lo, cl, t0, window_end(t0, hk), entry, zones)
    return out


def _mean(xs: list[float]) -> float:
    v = [float(x) for x in xs if x == x]
    return float(np.mean(v)) if v else float("nan")


def _median(xs: list[float]) -> float:
    v = [float(x) for x in xs if x == x]
    return float(np.median(v)) if v else float("nan")


def ffill_1m_grid(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Regular 1m grid; OHLC forward-filled across missing minutes."""
    t = np.asarray(ts, dtype=np.int64)
    t0 = int(t[0])
    t1 = int(t[-1])
    grid = np.arange(t0, t1 + 1, 60, dtype=np.int64)
    n = int(grid.size)
    oo = np.full(n, np.nan)
    hh = np.full(n, np.nan)
    ll = np.full(n, np.nan)
    cc = np.full(n, np.nan)
    pos = ((t - t0) // 60).astype(np.int64)
    pos = np.clip(pos, 0, n - 1)
    oo[pos] = o
    hh[pos] = h
    ll[pos] = l
    cc[pos] = c

    def _ffill(a: np.ndarray) -> np.ndarray:
        good = np.isfinite(a)
        if not np.any(good):
            return a
        idx = np.where(good, np.arange(n), 0)
        np.maximum.accumulate(idx, out=idx)
        out = a.copy()
        first = int(np.argmax(good))
        out[first:] = a[idx[first:]]
        return out

    return grid, _ffill(oo), _ffill(hh), _ffill(ll), _ffill(cc)


def rolling_pre_range_60(high: np.ndarray, low: np.ndarray) -> np.ndarray:
    """max(high[i-59..i]) - min(low[i-59..i]); nan until 60 bars exist."""
    n = int(high.size)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 60:
        return out
    from collections import deque

    def _roll(a: np.ndarray, op_ge: bool) -> np.ndarray:
        dq: deque[int] = deque()
        r = np.empty(n, dtype=np.float64)
        for i in range(n):
            if op_ge:
                while dq and a[dq[-1]] <= a[i]:
                    dq.pop()
            else:
                while dq and a[dq[-1]] >= a[i]:
                    dq.pop()
            dq.append(i)
            if dq[0] <= i - 60:
                dq.popleft()
            r[i] = a[dq[0]] if i >= 59 else np.nan
        return r

    hx = _roll(high.astype(np.float64), True)
    ln = _roll(low.astype(np.float64), False)
    out[59:] = hx[59:] - ln[59:]
    return out


def assign_quantile_bins(pre_range: np.ndarray, n_bins: int = N_VOL_BINS) -> tuple[np.ndarray, np.ndarray]:
    valid = pre_range[np.isfinite(pre_range)]
    if valid.size < n_bins:
        edges = np.linspace(0.0, 1.0, n_bins + 1)
        bins = np.full(pre_range.shape, -1, dtype=np.int16)
        return bins, edges
    edges = np.quantile(valid, np.linspace(0.0, 1.0, n_bins + 1))
    edges[0] = -np.inf
    edges[-1] = np.inf
    bins = np.full(pre_range.shape, -1, dtype=np.int16)
    ok = np.isfinite(pre_range)
    bins[ok] = np.digitize(pre_range[ok], edges[1:-1], right=True).astype(np.int16)
    bins[ok] = np.clip(bins[ok], 0, n_bins - 1)
    return bins, edges


def rolling_max(a: np.ndarray, w: int) -> np.ndarray:
    n = int(a.size)
    out = np.full(n, np.nan, dtype=np.float64)
    if w <= 0 or n == 0:
        return out
    from collections import deque

    dq: deque[int] = deque()
    x = a.astype(np.float64)
    for i in range(n):
        while dq and x[dq[-1]] <= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out[i] = x[dq[0]]
    return out


def rolling_min(a: np.ndarray, w: int) -> np.ndarray:
    n = int(a.size)
    out = np.full(n, np.nan, dtype=np.float64)
    if w <= 0 or n == 0:
        return out
    from collections import deque

    dq: deque[int] = deque()
    x = a.astype(np.float64)
    for i in range(n):
        while dq and x[dq[-1]] >= x[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out[i] = x[dq[0]]
    return out


def fwd_mfe_end(high: np.ndarray, low: np.ndarray, close: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Forward max(high)-style MFE and |end-entry| using bars i+1..i+w (entry = close[i])."""
    n = int(high.size)
    mfe = np.full(n, np.nan)
    end_abs = np.full(n, np.nan)
    if n < 2 or w <= 0:
        return mfe, end_abs
    rmax = rolling_max(high, w)
    rmin = rolling_min(low, w)
    j = np.arange(n) + w
    ok = j < n
    jj = np.clip(j, 0, n - 1)
    fh = rmax[jj]
    fl = rmin[jj]
    entry = close.astype(np.float64)
    mfe[ok] = np.maximum(fh[ok] - entry[ok], entry[ok] - fl[ok])
    end_abs[ok] = np.abs(close[jj][ok] - entry[ok])
    # last w-1 indices: use remaining bars only
    for i in range(max(0, n - w), n - 1):
        sl = slice(i + 1, n)
        mx = float(np.max(high[sl]))
        mn = float(np.min(low[sl]))
        en = float(close[i])
        mfe[i] = max(mx - en, en - mn)
        end_abs[i] = abs(float(close[-1]) - en)
    return mfe, end_abs


def _sparse_max(a: np.ndarray) -> np.ndarray:
    n = int(a.size)
    if n <= 0:
        return np.zeros((1, 0), dtype=np.float64)
    k = int(np.floor(np.log2(n))) + 1
    st = np.empty((k, n), dtype=np.float64)
    st[0] = a.astype(np.float64)
    for i in range(1, k):
        half = 1 << (i - 1)
        span = 1 << i
        lim = n - span + 1
        st[i, :lim] = np.maximum(st[i - 1, :lim], st[i - 1, half:half + lim])
        if lim < n:
            st[i, lim:] = st[i - 1, lim:]
    return st


def _sparse_min(a: np.ndarray) -> np.ndarray:
    n = int(a.size)
    if n <= 0:
        return np.zeros((1, 0), dtype=np.float64)
    k = int(np.floor(np.log2(n))) + 1
    st = np.empty((k, n), dtype=np.float64)
    st[0] = a.astype(np.float64)
    for i in range(1, k):
        half = 1 << (i - 1)
        span = 1 << i
        lim = n - span + 1
        st[i, :lim] = np.minimum(st[i - 1, :lim], st[i - 1, half:half + lim])
        if lim < n:
            st[i, lim:] = st[i - 1, lim:]
    return st


def _rmq_max(st: np.ndarray, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    length = np.maximum(right - left + 1, 1)
    i = np.floor(np.log2(length)).astype(np.int32)
    p2 = np.left_shift(1, i)
    a = st[i, left]
    b = st[i, right - p2 + 1]
    return np.maximum(a, b)


def _rmq_min(st: np.ndarray, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    length = np.maximum(right - left + 1, 1)
    i = np.floor(np.log2(length)).astype(np.int32)
    p2 = np.left_shift(1, i)
    a = st[i, left]
    b = st[i, right - p2 + 1]
    return np.minimum(a, b)


def fwd_mfe_until(high: np.ndarray, low: np.ndarray, close: np.ndarray, ts: np.ndarray, end_ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = int(high.size)
    mfe = np.full(n, np.nan)
    end_abs = np.full(n, np.nan)
    if n < 2:
        return mfe, end_abs
    end_i = np.searchsorted(ts, end_ts, side="right") - 1
    left = np.arange(n, dtype=np.int64) + 1
    right = end_i.astype(np.int64)
    ok = (left < n) & (right >= left) & (right < n)
    sth = _sparse_max(high)
    stl = _sparse_min(low)
    mx = _rmq_max(sth, left[ok], right[ok])
    mn = _rmq_min(stl, left[ok], right[ok])
    en = close.astype(np.float64)[ok]
    mfe[ok] = np.maximum(mx - en, en - mn)
    end_abs[ok] = np.abs(close[right[ok]] - en)
    return mfe, end_abs


def next_1730_arr(ts: np.ndarray) -> np.ndarray:
    t = ts.astype(np.int64)
    ist_sec = (t + IST_OFF) % 86400
    target = 17 * 3600 + 30 * 60
    delta = (target - ist_sec) % 86400
    delta = np.where(delta == 0, 86400, delta)
    return t + delta


def percentile_ranks(pre_range: np.ndarray, xs: np.ndarray) -> np.ndarray:
    valid = np.sort(pre_range[np.isfinite(pre_range)])
    if valid.size == 0:
        return np.full(xs.shape, np.nan)
    return 100.0 * np.searchsorted(valid, xs, side="left") / float(valid.size)


def hour_matched_mfe_ratio(
    ts: np.ndarray,
    hod: np.ndarray,
    mfe: np.ndarray,
    signal_idx: np.ndarray,
    rng: np.random.Generator,
    n_ctrl: int = N_CTRL,
    min_sep: int = MIN_SEP,
) -> float:
    """Hour-only control mean vs signal mean (for tests; one metric array)."""
    sig_ts = ts[np.asarray(signal_idx, dtype=np.int64)]
    near = forbidden_mask(ts, sig_ts, min_sep)
    svals = mfe[np.asarray(signal_idx, dtype=np.int64)]
    svals = svals[np.isfinite(svals)]
    if svals.size == 0:
        return float("nan")
    ctrl: list[float] = []
    for i in np.asarray(signal_idx, dtype=np.int64):
        pool = np.where((hod == hod[i]) & (~near) & np.isfinite(mfe))[0]
        if pool.size == 0:
            continue
        pick = sample_control_times(pool.astype(np.int64), n_ctrl, rng)
        ctrl.extend(float(mfe[int(k)]) for k in pick)
    sm = float(np.mean(svals))
    cm = float(np.mean(ctrl)) if ctrl else float("nan")
    if not math.isfinite(cm) or cm == 0:
        return float("nan")
    return sm / cm


def grouped_cell_indices(
    cell: np.ndarray,
    valid: np.ndarray,
    ncell: int = N_VOL_BINS * 24,
) -> list[np.ndarray]:
    ix = np.flatnonzero(valid & (cell >= 0) & (cell < ncell))
    empty = [np.zeros(0, dtype=np.int64) for _ in range(ncell)]
    if ix.size == 0:
        return empty
    cid = cell[ix].astype(np.int32)
    order = np.argsort(cid, kind="mergesort")
    ix = ix[order]
    cid = cid[order]
    starts = np.searchsorted(cid, np.arange(ncell), side="left")
    ends = np.searchsorted(cid, np.arange(ncell), side="right")
    return [ix[int(starts[c]): int(ends[c])] for c in range(ncell)]


def volmatch_expected_perm(
    event_idx: np.ndarray,
    cell: np.ndarray,
    metric: np.ndarray,
    groups: list[np.ndarray],
    rng: np.random.Generator,
    n_perm: int = N_PERM,
) -> dict[str, float]:
    """Expected = mean of ALL valid minutes in each event's (bin, hour) cell.

    Perm: 10k draws; each draw takes one random minute from that event's cell.
    """
    nan = {
        "n_events": 0, "signal_mean": float("nan"), "expected_mean": float("nan"),
        "ratio": float("nan"), "p_ge": float("nan"), "p_le": float("nan"),
    }
    ev = np.asarray(event_idx, dtype=np.int64)
    if ev.size == 0:
        return nan
    ok = np.isfinite(metric[ev]) & (cell[ev] >= 0)
    ev = ev[ok]
    if ev.size == 0:
        return nan
    smn = float(np.mean(metric[ev]))
    pools: list[np.ndarray] = []
    exp_parts: list[float] = []
    for i in ev:
        g = groups[int(cell[i])]
        pool = metric[g]
        pool = pool[np.isfinite(pool)]
        if pool.size == 0:
            continue
        pools.append(pool)
        exp_parts.append(float(np.mean(pool)))
    if not pools:
        nan["n_events"] = int(ev.size)
        nan["signal_mean"] = smn
        return nan
    expected = float(np.mean(exp_parts))
    ratio = (smn / expected) if expected != 0 else float("nan")
    n_e = len(pools)
    sizes = np.array([p.size for p in pools], dtype=np.int64)
    maxlen = int(np.max(sizes))
    pad = np.zeros((n_e, maxlen), dtype=np.float64)
    for k, p in enumerate(pools):
        pad[k, : p.size] = p
    u = rng.random((n_e, int(n_perm)))
    idx = np.minimum((u * sizes[:, None].astype(np.float64)).astype(np.int64), sizes[:, None] - 1)
    draws = np.take_along_axis(pad, idx, axis=1)
    dmean = draws.mean(axis=0)
    return {
        "n_events": n_e,
        "signal_mean": smn,
        "expected_mean": expected,
        "ratio": ratio,
        "p_ge": float(np.mean(dmean >= smn)),
        "p_le": float(np.mean(dmean <= smn)),
    }


def perm_pvalue(
    signal_mean: float,
    control_vals: np.ndarray,
    n_events: int,
    rng: np.random.Generator,
    n_perm: int = N_PERM,
) -> float:
    if not math.isfinite(signal_mean) or n_events <= 0 or control_vals.size == 0:
        return float("nan")
    pool = control_vals[np.isfinite(control_vals)]
    if pool.size == 0:
        return float("nan")
    draws = rng.choice(pool, size=(int(n_perm), int(n_events)), replace=True)
    means = draws.mean(axis=1)
    return float(np.mean(means >= float(signal_mean)))


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return "nan"
        return f"{x:.6g}"
    return str(x)


def _unique_folder(root: Path, stamp: str, tag: str = "stage0") -> Path:
    folder = root / f"{stamp}_{tag}"
    n = 0
    while folder.exists():
        n += 1
        folder = root / f"{stamp}_{tag}_{n}"
    return folder


def _hits_from_mfe(mfe: np.ndarray, thr: float) -> np.ndarray:
    out = np.full(mfe.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(mfe)
    out[ok] = (mfe[ok] >= float(thr)).astype(np.float64)
    return out


def compute_volmatch(
    grid: np.ndarray,
    gh: np.ndarray,
    gl: np.ndarray,
    gc: np.ndarray,
    event_rows: list[dict[str, Any]],
    rng: np.random.Generator,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    print("VOLMATCH: rolling pre_range_60 + quantile bins", flush=True)
    pre = rolling_pre_range_60(gh, gl)
    bins, _edges = assign_quantile_bins(pre, N_VOL_BINS)
    hod = ist_hod_arr(grid)
    cell = np.full(grid.shape, -1, dtype=np.int32)
    okb = bins >= 0
    cell[okb] = bins[okb].astype(np.int32) * 24 + hod[okb].astype(np.int32)
    t0 = int(grid[0])
    ngrid = int(grid.size)

    def to_i(t: int) -> int:
        i = (int(t) - t0) // 60
        if 0 <= i < ngrid:
            return i
        return -1

    print("VOLMATCH: forward MFE on 1m grid", flush=True)
    fwd: dict[str, dict[str, np.ndarray]] = {}
    for hk, w in H_MINUTES.items():
        mfe, end = fwd_mfe_end(gh, gl, gc, w)
        fwd[hk] = {
            "mfe_abs": mfe,
            "end_abs": end,
            "hit_1000": _hits_from_mfe(mfe, 1000.0),
            "hit_1500": _hits_from_mfe(mfe, 1500.0),
            "hit_2000": _hits_from_mfe(mfe, 2000.0),
        }
        print(f"  fwd {hk} done", flush=True)
    m1730, e1730 = fwd_mfe_until(gh, gl, gc, grid, next_1730_arr(grid))
    fwd["1730"] = {
        "mfe_abs": m1730,
        "end_abs": e1730,
        "hit_1000": _hits_from_mfe(m1730, 1000.0),
        "hit_1500": _hits_from_mfe(m1730, 1500.0),
        "hit_2000": _hits_from_mfe(m1730, 2000.0),
    }
    print("  fwd 1730 done", flush=True)

    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in event_rows:
        by[(str(r["tf"]), str(r["mode"]))].append(r)

    stats_rows: list[dict[str, Any]] = []
    month_rows: list[dict[str, Any]] = []
    pre_rows: list[dict[str, Any]] = []
    for tf in TFS:
        for mode in MODES:
            recs = by.get((tf, mode), [])
            eidx: list[int] = []
            eym: list[int] = []
            for r in recs:
                i = to_i(int(r["signal_ts"]))
                if i < 0 or not np.isfinite(pre[i]) or int(cell[i]) < 0:
                    continue
                eidx.append(i)
                eym.append(int(r["ym"]))
            eidx_a = np.asarray(eidx, dtype=np.int64)
            eym_a = np.asarray(eym, dtype=np.int32)
            pr_e = pre[eidx_a] if eidx_a.size else np.zeros(0)
            pct = percentile_ranks(pre, pr_e) if pr_e.size else np.zeros(0)
            pre_rows.append({
                "tf": tf,
                "mode": mode,
                "n": int(eidx_a.size),
                "pre_range_60_mean": float(np.mean(pr_e)) if pr_e.size else float("nan"),
                "pre_range_60_median": float(np.median(pr_e)) if pr_e.size else float("nan"),
                "pre_range_60_mean_percentile": float(np.mean(pct)) if pct.size else float("nan"),
            })
            print(
                f"VOLMATCH {tf} {mode}: n={eidx_a.size} "
                f"pre_range_median={pre_rows[-1]['pre_range_60_median']:.1f} "
                f"mean_pct={pre_rows[-1]['pre_range_60_mean_percentile']:.1f}",
                flush=True,
            )
            sig_ts = np.array([int(r["signal_ts"]) for r in recs], dtype=np.int64)
            near = forbidden_mask(grid, sig_ts, MIN_SEP)
            valid = np.isfinite(pre) & (~near) & (cell >= 0)
            groups = grouped_cell_indices(cell, valid)
            for hk in H_KEYS:
                for mk in ("mfe_abs", "end_abs", "hit_1000", "hit_1500", "hit_2000"):
                    st = volmatch_expected_perm(
                        eidx_a, cell, fwd[hk][mk], groups, rng, N_PERM,
                    )
                    stats_rows.append({
                        "tf": tf, "mode": mode, "H": hk, "metric": mk,
                        "n_events": st["n_events"],
                        "signal_mean": st["signal_mean"],
                        "expected_mean": st["expected_mean"],
                        "ratio": st["ratio"],
                        "p_ge_signal_mean": st["p_ge"],
                        "p_le_signal_mean": st["p_le"],
                    })
                if hk in ("2h", "4h") and mode == "wick" and tf in ("15m", "30m"):
                    mfe = fwd[hk]["mfe_abs"]
                    for ym in sorted(set(int(x) for x in eym_a.tolist())):
                        evm = eidx_a[eym_a == ym]
                        st = volmatch_expected_perm(evm, cell, mfe, groups, rng, N_PERM)
                        month_rows.append({
                            "ym": ym, "tf": tf, "mode": mode, "H": hk,
                            "n_signal": st["n_events"],
                            "signal_mean_mfe": st["signal_mean"],
                            "expected_mean_mfe": st["expected_mean"],
                            "ratio": st["ratio"],
                            "p_ge": st["p_ge"],
                            "p_le": st["p_le"],
                        })
    return stats_rows, month_rows, pre_rows


def _bars_from_ohlc(
    tts: Any, to_: Any, th: Any, tl: Any, tc: Any,
) -> list[dict[str, Any]]:
    return [
        {
            "time": int(t),
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
        }
        for t, o, h, lo, c in zip(tts, to_, th, tl, tc)
    ]


def main() -> None:
    t_run = time.perf_counter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    ap.add_argument("--volmatch", action="store_true", default=True)
    ap.add_argument("--no-volmatch", dest="volmatch", action="store_false")
    args = ap.parse_args()

    from backtest.harness.run_archive import RunArchive
    from backtest.strategies.s018_4h_trend import run_s018 as s018
    from backtest.strategies.s020_vwap_touch import run_s020 as s020
    from backtest.strategies.s020_vwap_touch import run_s020_dev as d
    from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals

    csv_path = str(args.csv or s018.SPOT_CSV)
    args.csv = csv_path
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "stage0_volmatch" if bool(args.volmatch) else "stage0"
    folder = _unique_folder(RUNS_DIR, stamp, tag)
    archive = RunArchive(
        "s022",
        tag,
        args,
        month="2024-07_2026-09",
        prereg_note="Stage 0 move-edge vs hour-matched random + VOLMATCH control; spot only",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    spot = s018.load_spot_1m(csv_path)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    print(f"1m bars={len(ts)} csv={csv_path}", flush=True)
    hod_1m = ist_hod_arr(ts)
    ym_1m = ist_ym_arr(ts)
    entry_close = {int(t): float(px) for t, px in zip(ts, c)}

    rng = np.random.default_rng(SEED)
    perm_rng = np.random.default_rng(SEED)

    event_rows: list[dict[str, Any]] = []
    ctrl_fp = (archive.folder / "controls.csv").open("w", newline="", encoding="utf-8")
    ctrl_cols = ["event_id", "tf", "mode", "ctrl_i", "ctrl_ts", "hod", "ym"]
    for hk in H_KEYS:
        for m in METRICS:
            ctrl_cols.append(f"{m}_{hk}")
    ctrl_w = csv.DictWriter(ctrl_fp, fieldnames=ctrl_cols, extrasaction="ignore")
    ctrl_w.writeheader()

    sig_vals: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    ctrl_vals: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    month_sig: dict[tuple[int, str, str, str], list[float]] = defaultdict(list)
    month_ctrl: dict[tuple[int, str, str, str], list[float]] = defaultdict(list)
    hour_hist: dict[tuple[str, str, int], int] = defaultdict(int)
    n_dup_collapsed = 0
    n_events_tot = 0
    event_id = 0

    for tf in TFS:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        print(f"tf={tf} bars={len(bars)}", flush=True)
        for mode in MODES:
            raw = iter_ulp_signals(bars, mode, tf)
            fresh = [s for s in raw if s["rule"] == "fresh"]
            events = collapse_events(fresh)
            n_dup_collapsed += max(0, len(fresh) - len(events))
            events = [
                e for e in events
                if RANGE_YM_LO <= int(e["ym"]) <= RANGE_YM_HI
            ]
            print(f"  mode={mode} fresh={len(fresh)} events={len(events)}", flush=True)
            sig_arr = np.array([int(e["signal_ts"]) for e in events], dtype=np.int64)
            near = forbidden_mask(ts, sig_arr, MIN_SEP)
            for e in events:
                hour_hist[(tf, mode, int(e["hod"]))] += 1
            by_pool: dict[tuple[int, int], np.ndarray] = {}

            def pool_for(hod: int, ym: int) -> np.ndarray:
                k = (int(hod), int(ym))
                if k not in by_pool:
                    m = (hod_1m == k[0]) & (ym_1m == k[1]) & (~near)
                    by_pool[k] = ts[m]
                return by_pool[k]

            for e in events:
                t0 = int(e["signal_ts"])
                entry = float(e["entry"])
                zones = list(e["zones"])
                sm = metrics_all_h(ts, h, l, c, t0, entry, zones)
                if all(sm[hk]["n_bars"] <= 0 for hk in H_KEYS):
                    continue
                event_id += 1
                n_events_tot += 1
                rec: dict[str, Any] = {
                    "event_id": event_id,
                    "tf": tf,
                    "mode": mode,
                    "signal_ts": t0,
                    "signal_ist": e["signal_ist"],
                    "hod": e["hod"],
                    "ym": e["ym"],
                    "entry": entry,
                    "zone_kind": e["zone_kind"],
                    "zone_top": e["zone_top"],
                    "zone_bottom": e["zone_bottom"],
                    "n_raw": e["n_raw"],
                }
                for hk in H_KEYS:
                    for mk in METRICS:
                        rec[f"{mk}_{hk}"] = sm[hk][mk]
                    rec[f"n_bars_{hk}"] = sm[hk]["n_bars"]
                    for mk in METRICS:
                        sig_vals[(tf, mode, hk, mk)].append(float(sm[hk][mk]))
                    if hk in ("2h", "4h"):
                        month_sig[(int(e["ym"]), tf, mode, hk)].append(float(sm[hk]["mfe_abs"]))
                event_rows.append(rec)

                pool = pool_for(int(e["hod"]), int(e["ym"]))
                ctrls = sample_control_times(pool, N_CTRL, rng)
                for ci, ct in enumerate(ctrls):
                    ct = int(ct)
                    c_entry = float(entry_close.get(ct, float("nan")))
                    if not math.isfinite(c_entry):
                        continue
                    cm = metrics_all_h(ts, h, l, c, ct, c_entry, None)
                    crow: dict[str, Any] = {
                        "event_id": event_id,
                        "tf": tf,
                        "mode": mode,
                        "ctrl_i": ci,
                        "ctrl_ts": ct,
                        "hod": ist_hod(ct),
                        "ym": ist_ym(ct),
                    }
                    for hk in H_KEYS:
                        for mk in METRICS:
                            val = float(cm[hk][mk])
                            crow[f"{mk}_{hk}"] = val
                            ctrl_vals[(tf, mode, hk, mk)].append(val)
                        if hk in ("2h", "4h"):
                            month_ctrl[(int(e["ym"]), tf, mode, hk)].append(
                                float(cm[hk]["mfe_abs"])
                            )
                    ctrl_w.writerow(crow)
                if n_events_tot % 2000 == 0:
                    print(f"    events done={n_events_tot}", flush=True)

    ctrl_fp.close()

    ev_cols = [
        "event_id", "tf", "mode", "signal_ts", "signal_ist", "hod", "ym",
        "entry", "zone_kind", "zone_top", "zone_bottom", "n_raw",
    ]
    for hk in H_KEYS:
        for mk in METRICS:
            ev_cols.append(f"{mk}_{hk}")
        ev_cols.append(f"n_bars_{hk}")
    ev_path = archive.folder / "events.csv"
    with ev_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ev_cols, extrasaction="ignore")
        w.writeheader()
        for r in event_rows:
            w.writerow(r)

    stats_rows: list[dict[str, Any]] = []
    for tf in TFS:
        for mode in MODES:
            for hk in H_KEYS:
                n_ev = 0
                for mk in METRICS:
                    sv = sig_vals[(tf, mode, hk, mk)]
                    cv = np.asarray(ctrl_vals[(tf, mode, hk, mk)], dtype=np.float64)
                    n_ev = sum(1 for x in sv if x == x)
                    smn, smd = _mean(sv), _median(sv)
                    cmn, cmd = (
                        float(np.nanmean(cv)) if cv.size else float("nan"),
                        float(np.nanmedian(cv)) if cv.size else float("nan"),
                    )
                    ratio = (smn / cmn) if (math.isfinite(smn) and math.isfinite(cmn) and cmn != 0) else float("nan")
                    p = perm_pvalue(smn, cv, n_ev, perm_rng, N_PERM)
                    stats_rows.append({
                        "tf": tf, "mode": mode, "H": hk, "metric": mk,
                        "n_events": n_ev,
                        "signal_mean": smn, "signal_median": smd,
                        "control_mean": cmn, "control_median": cmd,
                        "ratio": ratio, "p_ge_signal_mean": p,
                    })

    st_cols = [
        "tf", "mode", "H", "metric", "n_events",
        "signal_mean", "signal_median", "control_mean", "control_median",
        "ratio", "p_ge_signal_mean",
    ]
    st_path = archive.folder / "stats.csv"
    with st_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=st_cols)
        w.writeheader()
        for r in stats_rows:
            w.writerow(r)

    month_rows: list[dict[str, Any]] = []
    for key in sorted(month_sig):
        ym, tf, mode, hk = key
        smn = _mean(month_sig[key])
        cmn = _mean(month_ctrl.get(key, []))
        ratio = (smn / cmn) if (math.isfinite(smn) and math.isfinite(cmn) and cmn != 0) else float("nan")
        month_rows.append({
            "ym": ym, "tf": tf, "mode": mode, "H": hk,
            "n_signal": sum(1 for x in month_sig[key] if x == x),
            "signal_mean_mfe": smn, "control_mean_mfe": cmn, "ratio": ratio,
        })
    mo_path = archive.folder / "per_month.csv"
    with mo_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["ym", "tf", "mode", "H", "n_signal", "signal_mean_mfe", "control_mean_mfe", "ratio"],
        )
        w.writeheader()
        for r in month_rows:
            w.writerow(r)

    hr_rows: list[dict[str, Any]] = []
    for tf in TFS:
        for mode in MODES:
            for hod in range(24):
                hr_rows.append({
                    "tf": tf, "mode": mode, "hod_ist": hod,
                    "n": hour_hist.get((tf, mode, hod), 0),
                })
    hr_path = archive.folder / "hours.csv"
    with hr_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tf", "mode", "hod_ist", "n"])
        w.writeheader()
        for r in hr_rows:
            w.writerow(r)

    vm_stats: list[dict[str, Any]] = []
    vm_month: list[dict[str, Any]] = []
    vm_pre: list[dict[str, Any]] = []
    if bool(args.volmatch):
        print("VOLMATCH: ffill 1m grid", flush=True)
        grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
        print(f"VOLMATCH: grid bars={len(grid)}", flush=True)
        vol_rng = np.random.default_rng(SEED)
        vm_stats, vm_month, vm_pre = compute_volmatch(grid, gh, gl, gc, event_rows, vol_rng)
        vm_cols = [
            "tf", "mode", "H", "metric", "n_events",
            "signal_mean", "expected_mean", "ratio",
            "p_ge_signal_mean", "p_le_signal_mean",
        ]
        vm_path = archive.folder / "stats_volmatch.csv"
        with vm_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=vm_cols)
            w.writeheader()
            for r in vm_stats:
                w.writerow(r)
        vm_mo_cols = [
            "ym", "tf", "mode", "H", "n_signal",
            "signal_mean_mfe", "expected_mean_mfe", "ratio", "p_ge", "p_le",
        ]
        vm_mo_path = archive.folder / "per_month_volmatch.csv"
        with vm_mo_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=vm_mo_cols)
            w.writeheader()
            for r in vm_month:
                w.writerow(r)
        pre_path = archive.folder / "pre_range_volmatch.csv"
        with pre_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "tf", "mode", "n", "pre_range_60_mean",
                    "pre_range_60_median", "pre_range_60_mean_percentile",
                ],
            )
            w.writeheader()
            for r in vm_pre:
                w.writerow(r)
        print(f"wrote {vm_path}", flush=True)

    from openpyxl import Workbook

    wb = Workbook()

    def _sheet(title: str, rows: list[dict[str, Any]], cols: list[str]) -> None:
        ws = wb.active if title == "stats" else wb.create_sheet(title)
        if title == "stats":
            ws.title = "stats"
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])

    _sheet("stats", stats_rows, st_cols)
    _sheet("per_month", month_rows, ["ym", "tf", "mode", "H", "n_signal", "signal_mean_mfe", "control_mean_mfe", "ratio"])
    _sheet("hours", hr_rows, ["tf", "mode", "hod_ist", "n"])
    _sheet("events", event_rows, ev_cols)
    if vm_stats:
        _sheet(
            "stats_volmatch",
            vm_stats,
            [
                "tf", "mode", "H", "metric", "n_events",
                "signal_mean", "expected_mean", "ratio",
                "p_ge_signal_mean", "p_le_signal_mean",
            ],
        )
        _sheet(
            "per_month_volmatch",
            vm_month,
            [
                "ym", "tf", "mode", "H", "n_signal",
                "signal_mean_mfe", "expected_mean_mfe", "ratio", "p_ge", "p_le",
            ],
        )
        _sheet(
            "pre_range",
            vm_pre,
            [
                "tf", "mode", "n", "pre_range_60_mean",
                "pre_range_60_median", "pre_range_60_mean_percentile",
            ],
        )
    xlsx_name = "stage0_volmatch.xlsx" if vm_stats else "stage0.xlsx"
    xlsx_path = archive.folder / xlsx_name
    wb.save(xlsx_path)

    elapsed = time.perf_counter() - t_run
    lines: list[str] = [
        "S022 Stage 0 — ULP fresh-touch vs hour-matched random control (spot only)",
        f"folder={archive.folder}",
        f"range IST months {RANGE_YM_LO}..{RANGE_YM_HI}  csv={csv_path}",
        f"seed={SEED}  n_ctrl={N_CTRL}  n_perm={N_PERM}  min_sep_sec={MIN_SEP}",
        f"n_events={n_events_tot}  collapsed_raw_dups={n_dup_collapsed}",
        f"runtime_sec={elapsed:.1f}",
        "",
        "ratio = signal_mean / control_mean. p = share of 10k control resamples with mean >= signal mean.",
        "H=1730 is path until next 17:30 IST after signal_ts.",
        "",
    ]
    for tf in TFS:
        for mode in MODES:
            lines.append(f"=== {tf} {mode} ===")
            n_show = next((r["n_events"] for r in stats_rows if r["tf"] == tf and r["mode"] == mode and r["H"] == "1h" and r["metric"] == "mfe_abs"), 0)
            lines.append(f"  n_events={n_show}")
            for hk in H_KEYS:
                block = [r for r in stats_rows if r["tf"] == tf and r["mode"] == mode and r["H"] == hk]
                by_m = {r["metric"]: r for r in block}
                mfe = by_m.get("mfe_abs", {})
                end = by_m.get("end_abs", {})
                rv = by_m.get("rv", {})
                h1 = by_m.get("hit_1000", {})
                zc = by_m.get("zone_crossed", {})
                lines.append(
                    f"  [{hk}] mfe_abs sig={_fmt(mfe.get('signal_mean', float('nan')))} "
                    f"ctrl={_fmt(mfe.get('control_mean', float('nan')))} "
                    f"ratio={_fmt(mfe.get('ratio', float('nan')))} "
                    f"p={_fmt(mfe.get('p_ge_signal_mean', float('nan')))}"
                )
                lines.append(
                    f"        end_abs ratio={_fmt(end.get('ratio', float('nan')))} "
                    f"rv ratio={_fmt(rv.get('ratio', float('nan')))} "
                    f"hit_1000 sig={_fmt(h1.get('signal_mean', float('nan')))} "
                    f"ctrl={_fmt(h1.get('control_mean', float('nan')))} "
                    f"zone_crossed sig={_fmt(zc.get('signal_mean', float('nan')))}"
                )
            lines.append("")
    lines.append("--- mfe_abs ratio by month (2h / 4h), wick 15m ---")
    for r in month_rows:
        if r["tf"] == "15m" and r["mode"] == "wick":
            lines.append(
                f"  {r['ym']} H={r['H']} n={r['n_signal']} ratio={_fmt(r['ratio'])}"
            )
    lines.append("")
    lines.append("--- IST hour-of-day histogram (15m wick) ---")
    for hod in range(24):
        n = hour_hist.get(("15m", "wick", hod), 0)
        lines.append(f"  hod={hod:02d} n={n}")
    lines.append("")
    if vm_stats:
        lines.append("======== VOLMATCH (pre_range_60 quantile bin × IST hour) ========")
        lines.append("ratio = signal_mean / cell expected mean. p_ge = P(draw mean >= sig); p_le = P(draw mean <= sig).")
        lines.append("Cell minutes exclude ±60 min of that tf/mode's signals. 20 quantile bins on full-file pre_range_60.")
        lines.append("")
        for r in vm_pre:
            lines.append(
                f"pre_range_60 {r['tf']} {r['mode']}: n={r['n']} "
                f"median={_fmt(r['pre_range_60_median'])} mean={_fmt(r['pre_range_60_mean'])} "
                f"mean_percentile={_fmt(r['pre_range_60_mean_percentile'])}"
            )
        lines.append("")
        for tf in TFS:
            for mode in MODES:
                lines.append(f"=== VOLMATCH {tf} {mode} ===")
                for hk in H_KEYS:
                    block = [
                        r for r in vm_stats
                        if r["tf"] == tf and r["mode"] == mode and r["H"] == hk
                    ]
                    by_m = {r["metric"]: r for r in block}
                    mfe = by_m.get("mfe_abs", {})
                    end = by_m.get("end_abs", {})
                    h1 = by_m.get("hit_1000", {})
                    lines.append(
                        f"  [{hk}] mfe_abs sig={_fmt(mfe.get('signal_mean', float('nan')))} "
                        f"exp={_fmt(mfe.get('expected_mean', float('nan')))} "
                        f"ratio={_fmt(mfe.get('ratio', float('nan')))} "
                        f"p_ge={_fmt(mfe.get('p_ge_signal_mean', float('nan')))} "
                        f"p_le={_fmt(mfe.get('p_le_signal_mean', float('nan')))}"
                    )
                    lines.append(
                        f"        end_abs ratio={_fmt(end.get('ratio', float('nan')))} "
                        f"hit_1000 sig={_fmt(h1.get('signal_mean', float('nan')))} "
                        f"exp={_fmt(h1.get('expected_mean', float('nan')))}"
                    )
                lines.append("")
        lines.append("--- VOLMATCH mfe_abs ratio by month (2h / 4h), 15m wick + 30m wick ---")
        for r in vm_month:
            lines.append(
                f"  {r['ym']} {r['tf']} {r['mode']} H={r['H']} n={r['n_signal']} "
                f"ratio={_fmt(r['ratio'])} p_ge={_fmt(r['p_ge'])} p_le={_fmt(r['p_le'])}"
            )
        lines.append("")
    text = "\n".join(lines)
    sum_path = archive.folder / "summary.txt"
    sum_path.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    print(f"wrote {ev_path}", flush=True)
    print(f"wrote {archive.folder / 'controls.csv'}", flush=True)
    print(f"wrote {st_path}", flush=True)
    print(f"wrote {xlsx_path}", flush=True)
    print(f"wrote {sum_path}", flush=True)
    print(f"runtime_sec={elapsed:.1f}", flush=True)

    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
