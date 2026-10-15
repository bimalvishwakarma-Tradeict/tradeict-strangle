"""S022 Stage 0 helpers: no look-ahead, control exclusion, duplicate collapse."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    MIN_SEP,
    assign_quantile_bins,
    collapse_events,
    forbidden_mask,
    fwd_mfe_end,
    grouped_cell_indices,
    hour_matched_mfe_ratio,
    ist_hod_arr,
    rolling_pre_range_60,
    sample_control_times,
    volmatch_expected_perm,
    window_metrics,
)


def test_forward_window_excludes_bar_at_signal_ts() -> None:
    t0 = 1_700_000_000
    ts = np.array([t0 - 60, t0, t0 + 60, t0 + 120], dtype=np.int64)
    hi = np.array([100.0, 50_000.0, 101.0, 102.0], dtype=np.float64)
    lo = np.array([99.0, 99.0, 100.0, 100.0], dtype=np.float64)
    cl = np.array([99.5, 100.0, 101.0, 101.5], dtype=np.float64)
    m = window_metrics(ts, hi, lo, cl, t0, t0 + 180, entry=100.0, zones=None)
    assert m["n_bars"] == 2
    assert m["mfe_abs"] < 1000.0
    assert abs(m["end_abs"] - 1.5) < 1e-9


def test_controls_never_within_60min_of_signal() -> None:
    rng = np.random.default_rng(0)
    ts = np.arange(1_700_000_000, 1_700_000_000 + 10 * 3600, 60, dtype=np.int64)
    sig = np.array([int(ts[120]), int(ts[300])], dtype=np.int64)
    near = forbidden_mask(ts, sig, MIN_SEP)
    pool = ts[~near]
    assert pool.size > 20
    for t in pool:
        assert min(abs(int(t) - int(s)) for s in sig) > MIN_SEP
    picked = sample_control_times(pool, 20, rng)
    assert picked.size == 20
    for t in picked:
        assert min(abs(int(t) - int(s)) for s in sig) > MIN_SEP
        assert int(t) in set(int(x) for x in pool)


def test_same_bar_duplicates_count_once() -> None:
    base = {
        "tf": "15m",
        "mode": "wick",
        "rule": "fresh",
        "signal_ts": 1_700_000_900,
        "signal_ist": "x",
        "spot_close": 100.0,
        "zone_top": 110.0,
        "zone_bottom": 105.0,
    }
    a = dict(base, zone_kind="high")
    b = dict(base, zone_kind="low", zone_top=95.0, zone_bottom=90.0)
    c = dict(base, signal_ts=1_700_001_800, zone_kind="high")
    ev = collapse_events([a, b, c])
    assert len(ev) == 2
    same = [e for e in ev if e["signal_ts"] == 1_700_000_900]
    assert len(same) == 1
    assert same[0]["n_raw"] == 2
    assert same[0]["zone_kind"] == "both"
    assert len(same[0]["zones"]) == 2


def test_volmatch_neutralizes_trailing_range_selection() -> None:
    """Forward MFE depends only on trailing range; high-range 'signals' look edgey vs hour-only."""
    rng = np.random.default_rng(7)
    n_days = 16
    n = n_days * 1440
    ts0 = 1_700_000_000
    ts = ts0 + np.arange(n, dtype=np.int64) * 60
    state = np.zeros(n, dtype=np.int32)
    for i in range(1, n):
        if rng.random() < 0.004:
            state[i] = 1 - state[i - 1]
        else:
            state[i] = state[i - 1]
    rng_bar = 15.0 + 900.0 * state.astype(np.float64)
    close = 100_000.0 + np.cumsum(rng.normal(0.0, 1.5, n))
    high = close + 0.5 * rng_bar
    low = close - 0.5 * rng_bar
    pre = rolling_pre_range_60(high, low)
    mfe, _end = fwd_mfe_end(high, low, close, 60)
    hod = ist_hod_arr(ts)
    bins, _ = assign_quantile_bins(pre)
    cell = np.full(n, -1, dtype=np.int32)
    okb = bins >= 0
    cell[okb] = bins[okb].astype(np.int32) * 24 + hod[okb].astype(np.int32)
    usable = np.flatnonzero(np.isfinite(pre) & np.isfinite(mfe) & (cell >= 0))
    cut = float(np.quantile(pre[usable], 0.85))
    hi = usable[pre[usable] >= cut]
    sig = rng.choice(hi, size=min(400, int(hi.size)), replace=False)
    hour_ratio = hour_matched_mfe_ratio(ts, hod, mfe, sig, np.random.default_rng(11), n_ctrl=20)
    near = forbidden_mask(ts, ts[sig], MIN_SEP)
    groups = grouped_cell_indices(cell, np.isfinite(pre) & (~near) & (cell >= 0))
    st = volmatch_expected_perm(sig, cell, mfe, groups, np.random.default_rng(22), n_perm=2000)
    assert hour_ratio == hour_ratio and hour_ratio > 1.1, hour_ratio
    assert abs(float(st["ratio"]) - 1.0) < 0.05, st
