#!/usr/bin/env python3
"""S022-F Stage0F-2: ULP + RSI-div + Pine ADX vs VOLCTRL/C2, RSI len/level grid.

Default CLI (rsi-len=14, levels=70/30, all TFs) matches 89a69c8 event logic.
Expiry 40/60 fixed. ADX Pine v4, k=5. Spot only.

python backtest/strategies/s022_ulp_long_strangle/stage0_filter.py \\
  --from 2024-07 --to 2026-09 --exclude 2025-08,2025-09
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.strategies.s020_vwap_touch.rsi_div import (
    RSI_EXP_OBH,
    RSI_EXP_OBL,
    detect_from_rsi,
    rsi_wilder,
)
from backtest.strategies.s022_ulp_long_strangle.adx_pine import adx_pine_v4, adx_rising
from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (
    MIN_SEP,
    N_VOL_BINS,
    SEED,
    _bars_from_ohlc,
    _unique_folder,
    assign_quantile_bins,
    ffill_1m_grid,
    forbidden_mask,
    ist_hod,
    ist_hod_arr,
    ist_ym,
    ist_ym_arr,
    rolling_pre_range_60,
    sample_control_times,
    window_metrics,
)

IST = ZoneInfo("Asia/Kolkata")
PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
N_WINS = (3, 6, 12)
RSI_LENS_ALL = (2, 3, 5, 7, 10, 12, 14)
LEVELS_ALL = ((70.0, 30.0), (75.0, 25.0), (80.0, 20.0), (85.0, 15.0))
H_SEC = {"1h": 3600, "2h": 7200, "4h": 14400}
ADX_LEN = 14
ADX_WARMUP = 500
ADX_K = 5
ENTRY_LAG = 60
GATE_RATIO = 1.10
GATE_T = 2.0
GATE_PCT = 60.0
GATE_N_JUNJUL = 30
EXP_OBH = float(RSI_EXP_OBH)
EXP_OBL = float(RSI_EXP_OBL)
DEFAULT_RSI_LEN = 14
DEFAULT_LEVEL = (70.0, 30.0)


def ist_str(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def parse_ym(s: str) -> int:
    p = str(s).strip()
    if "-" in p:
        y, m = p.split("-", 1)
        return int(y) * 100 + int(m)
    return int(p)


def parse_tf_list(s: str) -> list[str]:
    out: list[str] = []
    for part in str(s).split(","):
        tf = part.strip()
        if not tf:
            continue
        if tf not in TFS:
            raise ValueError(f"unknown tf {tf!r}; want {TFS}")
        if tf not in out:
            out.append(tf)
    return out or list(TFS)


def parse_rsi_lens(s: str) -> list[int]:
    out: list[int] = []
    for part in str(s).split(","):
        p = part.strip()
        if not p:
            continue
        n = int(p)
        if n < 2:
            raise ValueError(f"rsi-len {n} < 2")
        if n not in out:
            out.append(n)
    return out or [DEFAULT_RSI_LEN]


def parse_levels(s: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for part in str(s).split(","):
        p = part.strip()
        if not p:
            continue
        a, b = p.split("/", 1)
        pair = (float(a), float(b))
        if pair not in out:
            out.append(pair)
    return out or [DEFAULT_LEVEL]


def month_list(ym_lo: int, ym_hi: int, exclude: set[int]) -> list[int]:
    out: list[int] = []
    y, m = ym_lo // 100, ym_lo % 100
    end_y, end_m = ym_hi // 100, ym_hi % 100
    while (y, m) <= (end_y, end_m):
        ym = y * 100 + m
        if ym not in exclude:
            out.append(ym)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def keep_in_range(
    events: list[dict[str, Any]],
    ym_lo: int,
    ym_hi: int,
    exclude: set[int],
    ts_key: str = "entry_time",
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for e in events:
        t = int(e.get(ts_key) or e.get("signal_time") or e.get("signal_ts") or 0)
        ym = int(e["ym"]) if e.get("ym") is not None else ist_ym(t)
        if ym < ym_lo or ym > ym_hi or ym in exclude:
            continue
        e = dict(e)
        e["ym"] = ym
        kept.append(e)
    return kept


def zone_key(s: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(s.get("pivot_ts") or 0),
        str(s.get("zone_kind") or ""),
        round(float(s.get("zone_top") or 0.0), 2),
        round(float(s.get("zone_bottom") or 0.0), 2),
    )


def rsi_masks(
    rsi_sigs: list[dict[str, Any]],
    tts: np.ndarray,
    tf_sec: int,
) -> tuple[np.ndarray, np.ndarray]:
    n = int(tts.size)
    short = np.zeros(n, dtype=bool)
    long = np.zeros(n, dtype=bool)
    close_to_i = {int(tts[j]) + int(tf_sec): j for j in range(n)}
    for s in rsi_sigs:
        j = close_to_i.get(int(s["ts"]))
        if j is None:
            continue
        side = str(s.get("side") or "")
        if side == "short":
            short[j] = True
        elif side == "long":
            long[j] = True
    return short, long


def first_qualifying_j(
    i: int,
    n_win: int,
    zone_kind: str,
    rsi_short: np.ndarray,
    rsi_long: np.ndarray,
    adx: np.ndarray,
    k: int = ADX_K,
) -> int | None:
    """First j in [i, i+N] with dir-matched RSI-div and strict ADX rise. Causal."""
    n = int(adx.size)
    ii = int(i)
    if ii < 0 or ii >= n:
        return None
    last = min(ii + int(n_win), n - 1)
    kind = str(zone_kind)
    try_high = kind in ("high", "both")
    try_low = kind in ("low", "both")
    for j in range(ii, last + 1):
        if not adx_rising(adx, j, k=k):
            continue
        if try_high and bool(rsi_short[j]):
            return j
        if try_low and bool(rsi_long[j]):
            return j
    return None


def collapse_same_entry(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for e in events:
        k = int(e["entry_time"])
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def match_arm_f(
    ulp_fresh: list[dict[str, Any]],
    rsi_short: np.ndarray,
    rsi_long: np.ndarray,
    adx: np.ndarray,
    tts: np.ndarray,
    tf_sec: int,
    n_win: int,
) -> list[dict[str, Any]]:
    """One event per ULP zone; first qualifying j; same entry_time collapsed."""
    used_zones: set[tuple[Any, ...]] = set()
    raw: list[dict[str, Any]] = []
    n = int(tts.size)
    close_to_i = {int(tts[j]) + int(tf_sec): j for j in range(n)}
    ordered = sorted(ulp_fresh, key=lambda s: (int(s["signal_ts"]), zone_key(s)))
    for s in ordered:
        if str(s.get("rule") or "fresh") != "fresh":
            continue
        zk = zone_key(s)
        if zk in used_zones:
            continue
        i = close_to_i.get(int(s["signal_ts"]))
        if i is None:
            continue
        kind = str(s.get("zone_kind") or "")
        j = first_qualifying_j(i, n_win, kind, rsi_short, rsi_long, adx)
        if j is None:
            continue
        used_zones.add(zk)
        try_high = kind in ("high", "both")
        match_side = "short" if (try_high and bool(rsi_short[j])) else "long"
        sig_t = int(tts[j]) + int(tf_sec)
        touch_t = int(s["signal_ts"])
        entry_t = sig_t + ENTRY_LAG
        raw.append(
            {
                "arm": "F",
                "tf": str(s.get("tf") or ""),
                "mode": str(s.get("mode") or ""),
                "N": int(n_win),
                "zone_kind": kind,
                "match_side": match_side,
                "touch_time": touch_t,
                "signal_time": sig_t,
                "entry_time": entry_t,
                "adx_j": float(adx[j]) if math.isfinite(float(adx[j])) else float("nan"),
                "bar_i": int(i),
                "bar_j": int(j),
                "ym": ist_ym(entry_t),
                "hod": ist_hod(entry_t),
            }
        )
    raw.sort(key=lambda e: int(e["entry_time"]))
    return collapse_same_entry(raw)


def arm_u_events(ulp_fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import collapse_events

    out: list[dict[str, Any]] = []
    for e in collapse_events(ulp_fresh):
        sig_t = int(e["signal_ts"])
        entry_t = sig_t + ENTRY_LAG
        out.append(
            {
                "arm": "U",
                "tf": str(e["tf"]),
                "mode": str(e["mode"]),
                "N": "",
                "zone_kind": str(e["zone_kind"]),
                "touch_time": sig_t,
                "signal_time": sig_t,
                "entry_time": entry_t,
                "adx_j": float("nan"),
                "ym": ist_ym(entry_t),
                "hod": ist_hod(entry_t),
            }
        )
    return collapse_same_entry(sorted(out, key=lambda r: int(r["entry_time"])))


def arm_r_events(
    rsi_short: np.ndarray,
    rsi_long: np.ndarray,
    adx: np.ndarray,
    tts: np.ndarray,
    tf_sec: int,
    tf: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = int(tts.size)
    for j in range(n):
        if not adx_rising(adx, j, k=ADX_K):
            continue
        kind = None
        if bool(rsi_short[j]):
            kind = "high"
        elif bool(rsi_long[j]):
            kind = "low"
        else:
            continue
        sig_t = int(tts[j]) + int(tf_sec)
        entry_t = sig_t + ENTRY_LAG
        out.append(
            {
                "arm": "R",
                "tf": tf,
                "mode": "",
                "N": "",
                "zone_kind": kind,
                "touch_time": sig_t,
                "signal_time": sig_t,
                "entry_time": entry_t,
                "adx_j": float(adx[j]) if math.isfinite(float(adx[j])) else float("nan"),
                "ym": ist_ym(entry_t),
                "hod": ist_hod(entry_t),
            }
        )
    return collapse_same_entry(out)


def attach_metrics(
    row: dict[str, Any],
    ts: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    entry_px: float,
) -> None:
    t0 = int(row["entry_time"])
    for hk, sec in H_SEC.items():
        m = window_metrics(ts, h, l, c, t0, t0 + int(sec), float(entry_px), None)
        row[f"mfe_{hk}"] = m["mfe_abs"]
        row[f"absmove_{hk}"] = m["end_abs"]


def _entry_px(entry_close: dict[int, float], t: int) -> float:
    px = entry_close.get(int(t))
    if px is not None and math.isfinite(float(px)):
        return float(px)
    return float("nan")


def sample_volctrl(
    f_events: list[dict[str, Any]],
    grid: np.ndarray,
    cell: np.ndarray,
    hod_g: np.ndarray,
    ym_g: np.ndarray,
    rng: np.random.Generator,
    near: np.ndarray,
    ym_lo: int,
    ym_hi: int,
    exclude: set[int],
    tf: str,
    mode: str,
    n_win: int,
) -> list[dict[str, Any]]:
    g0 = int(grid[0]) if grid.size else 0
    n = int(grid.size)
    in_ok = (
        (ym_g >= int(ym_lo))
        & (ym_g <= int(ym_hi))
        & (~np.isin(ym_g, np.array(list(exclude) or [0], dtype=np.int32)))
        & (~near)
        & (cell >= 0)
    )
    out: list[dict[str, Any]] = []
    for e in f_events:
        t0 = int(e["entry_time"])
        gi = int((t0 - g0) // 60)
        if gi < 0 or gi >= n:
            continue
        cid = int(cell[gi])
        hod = int(hod_g[gi]) if 0 <= gi < int(hod_g.size) else ist_hod(t0)
        bin0 = cid // 24 if cid >= 0 else -1
        pool = grid[in_ok & (cell == cid)] if cid >= 0 else np.zeros(0, dtype=np.int64)
        if pool.size == 0 and bin0 >= 0:
            neigh = [int((bin0 + d) * 24 + hod) for d in (-1, 1) if 0 <= bin0 + d < 20]
            if neigh:
                pool = grid[in_ok & np.isin(cell, np.array(neigh, dtype=np.int32))]
        if pool.size == 0:
            pool = grid[in_ok & (hod_g == hod)]
        if pool.size == 0:
            continue
        picked = sample_control_times(pool, 1, rng)
        if picked.size == 0:
            continue
        ct = int(picked[0])
        out.append(
            {
                "arm": "VOLCTRL",
                "tf": tf,
                "mode": mode,
                "N": int(n_win),
                "zone_kind": "",
                "touch_time": ct,
                "signal_time": ct,
                "entry_time": ct,
                "adx_j": float("nan"),
                "ym": ist_ym(ct),
                "hod": ist_hod(ct),
            }
        )
    return out


def sample_c2(
    f_events: list[dict[str, Any]],
    grid: np.ndarray,
    hod_g: np.ndarray,
    ym_g: np.ndarray,
    rng: np.random.Generator,
    near: np.ndarray,
    ym_lo: int,
    ym_hi: int,
    exclude: set[int],
    tf: str,
    mode: str,
    n_win: int,
) -> list[dict[str, Any]]:
    in_ok = (
        (ym_g >= int(ym_lo))
        & (ym_g <= int(ym_hi))
        & (~np.isin(ym_g, np.array(list(exclude) or [0], dtype=np.int32)))
        & (~near)
    )
    out: list[dict[str, Any]] = []
    by_hod: dict[int, np.ndarray] = {}
    for e in f_events:
        hod = int(e.get("hod") if e.get("hod") is not None else ist_hod(int(e["entry_time"])))
        if hod not in by_hod:
            by_hod[hod] = grid[in_ok & (hod_g == hod)]
        picked = sample_control_times(by_hod[hod], 1, rng)
        if picked.size == 0:
            continue
        ct = int(picked[0])
        out.append(
            {
                "arm": "C2",
                "tf": tf,
                "mode": mode,
                "N": int(n_win),
                "zone_kind": "",
                "touch_time": ct,
                "signal_time": ct,
                "entry_time": ct,
                "adx_j": float("nan"),
                "ym": ist_ym(ct),
                "hod": ist_hod(ct),
            }
        )
    return out


def _finite(xs: list[float]) -> np.ndarray:
    return np.asarray([float(x) for x in xs if x == x and math.isfinite(float(x))], dtype=np.float64)


def _mean(xs: list[float]) -> float:
    a = _finite(xs)
    return float(np.mean(a)) if a.size else float("nan")


def welch_t(a: list[float], b: list[float]) -> float:
    aa = _finite(a)
    bb = _finite(b)
    if aa.size < 2 or bb.size < 2:
        return float("nan")
    va = float(np.var(aa, ddof=1))
    vb = float(np.var(bb, ddof=1))
    se = math.sqrt(va / float(aa.size) + vb / float(bb.size))
    if se == 0.0 or not math.isfinite(se):
        return float("nan")
    return (float(np.mean(aa)) - float(np.mean(bb))) / se


def ratio_means(a: list[float], b: list[float]) -> float:
    ma = _mean(a)
    mb = _mean(b)
    if not math.isfinite(ma) or not math.isfinite(mb) or mb == 0.0:
        return float("nan")
    return ma / mb


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return "nan"
        return f"{x:.4f}"
    return str(x)


def cell_key(c: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(c["tf"]),
        str(c["mode"]),
        int(c["N"]),
        int(c["rsi_len"]),
        float(c["ob"]),
        float(c["os"]),
    )


def neighbour_keys(
    tf: str,
    mode: str,
    n_win: int,
    rsi_len: int,
    ob: float,
    os: float,
    lens: list[int],
    levels: list[tuple[float, float]],
) -> list[tuple[Any, ...]]:
    """Axis neighbours: rsi_len ±1 step, level ±1 step, same tf/mode/N."""
    out: list[tuple[Any, ...]] = []
    try:
        i_len = lens.index(int(rsi_len))
    except ValueError:
        return out
    try:
        i_lv = levels.index((float(ob), float(os)))
    except ValueError:
        return out
    if i_len > 0:
        out.append((tf, mode, int(n_win), int(lens[i_len - 1]), float(ob), float(os)))
    if i_len < len(lens) - 1:
        out.append((tf, mode, int(n_win), int(lens[i_len + 1]), float(ob), float(os)))
    if i_lv > 0:
        o2, s2 = levels[i_lv - 1]
        out.append((tf, mode, int(n_win), int(rsi_len), float(o2), float(s2)))
    if i_lv < len(levels) - 1:
        o2, s2 = levels[i_lv + 1]
        out.append((tf, mode, int(n_win), int(rsi_len), float(o2), float(s2)))
    return out


def mark_robust(
    cells: list[dict[str, Any]],
    lens: list[int],
    levels: list[tuple[float, float]],
) -> None:
    by: dict[tuple[Any, ...], dict[str, Any]] = {cell_key(c): c for c in cells}
    for c in cells:
        neigh = neighbour_keys(
            str(c["tf"]), str(c["mode"]), int(c["N"]),
            int(c["rsi_len"]), float(c["ob"]), float(c["os"]),
            lens, levels,
        )
        n_pass = 0
        for k in neigh:
            nb = by.get(k)
            if nb is not None and str(nb.get("gate") or "").startswith("GATE PASS"):
                n_pass += 1
        c["n_neighbours"] = len(neigh)
        c["n_neighbours_pass"] = n_pass
        gate_ok = str(c.get("gate") or "").startswith("GATE PASS")
        c["robust"] = "ROBUST PASS" if (gate_ok and n_pass >= 2) else "ROBUST FAIL"


def f_split_kind(e: dict[str, Any]) -> str:
    k = str(e.get("zone_kind") or "")
    if k == "high":
        return "high"
    if k == "low":
        return "low"
    if k == "both":
        return "high" if str(e.get("match_side") or "") == "short" else "low"
    return k


def gate_line(cell: dict[str, Any]) -> str:
    fails: list[str] = []
    ok_rt = False
    for hk in ("2h", "4h"):
        for met in ("mfe", "absmove"):
            r = float(cell[f"F_VOLCTRL_{met}_{hk}"])
            t = float(cell[f"t_{met}_{hk}"])
            if math.isfinite(r) and math.isfinite(t) and r >= GATE_RATIO and t >= GATE_T:
                ok_rt = True
    if not ok_rt:
        fails.append("F/VOLCTRL>=1.10 with t>=2 at 2h or 4h")
    pcts = [
        float(cell.get("pct_months_gt1_mfe_2h", float("nan"))),
        float(cell.get("pct_months_gt1_mfe_4h", float("nan"))),
        float(cell.get("pct_months_gt1_absmove_2h", float("nan"))),
        float(cell.get("pct_months_gt1_absmove_4h", float("nan"))),
    ]
    ok_pct = any(math.isfinite(p) and p >= GATE_PCT for p in pcts)
    if not ok_pct:
        fails.append(f"% months F/VOLCTRL>1 >= {GATE_PCT:.0f}%")
    njj = int(cell["n_F_2025-06"]) + int(cell["n_F_2025-07"])
    if njj < GATE_N_JUNJUL:
        fails.append(f"n_F_2025-06+2025-07>={GATE_N_JUNJUL} (got {njj})")
    if not fails:
        return "GATE PASS"
    return "GATE FAIL: " + "; ".join(fails)


def _arm_ratios(
    cell: dict[str, Any],
    f_rows: list[dict[str, Any]],
    v_rows: list[dict[str, Any]],
    u_rows: list[dict[str, Any]],
    r_rows: list[dict[str, Any]],
    months: list[int],
) -> None:
    n_by_m: dict[int, int] = {ym: 0 for ym in months}
    for e in f_rows:
        n_by_m[int(e["ym"])] = n_by_m.get(int(e["ym"]), 0) + 1
    counts = [n_by_m[ym] for ym in months] if months else [0]
    cell["n_F_total"] = len(f_rows)
    cell["n_F_2025-06"] = n_by_m.get(202506, 0)
    cell["n_F_2025-07"] = n_by_m.get(202507, 0)
    cell["median_n_F_per_month"] = float(np.median(np.asarray(counts, dtype=np.float64)))
    for met, col in (("mfe", "mfe"), ("absmove", "absmove")):
        for hk in ("1h", "2h", "4h"):
            key = f"{col}_{hk}"
            fv = [float(e.get(key, float("nan"))) for e in f_rows]
            vv = [float(e.get(key, float("nan"))) for e in v_rows]
            uv = [float(e.get(key, float("nan"))) for e in u_rows]
            rv = [float(e.get(key, float("nan"))) for e in r_rows]
            cell[f"F_VOLCTRL_{met}_{hk}"] = ratio_means(fv, vv)
            cell[f"t_{met}_{hk}"] = welch_t(fv, vv)
            cell[f"U_VOLCTRL_{met}_{hk}"] = ratio_means(uv, vv)
            cell[f"R_VOLCTRL_{met}_{hk}"] = ratio_means(rv, vv)
    for met, col in (("mfe", "mfe"), ("absmove", "absmove")):
        for hk in ("2h", "4h"):
            key = f"{col}_{hk}"
            n_ok = 0
            n_m = 0
            for ym in months:
                fm = [float(e[key]) for e in f_rows if int(e["ym"]) == ym]
                vm = [float(e[key]) for e in v_rows if int(e["ym"]) == ym]
                if not fm:
                    continue
                n_m += 1
                r = ratio_means(fm, vm)
                if math.isfinite(r) and r > 1.0:
                    n_ok += 1
            cell[f"pct_months_gt1_{met}_{hk}"] = (
                100.0 * n_ok / n_m if n_m else float("nan")
            )


def _side_split(
    cell: dict[str, Any],
    f_rows: list[dict[str, Any]],
    v_rows: list[dict[str, Any]],
) -> None:
    for side in ("high", "low"):
        idx = [i for i, e in enumerate(f_rows) if f_split_kind(e) == side]
        fv = [f_rows[i] for i in idx]
        vv = [v_rows[i] for i in idx if i < len(v_rows)]
        njj = sum(1 for e in fv if int(e["ym"]) in (202506, 202507))
        cell[f"n_{side}"] = len(fv)
        cell[f"n_{side}_2025-06+07"] = njj
        for hk in ("2h", "4h"):
            fa = [float(e.get(f"mfe_{hk}", float("nan"))) for e in fv]
            va = [float(e.get(f"mfe_{hk}", float("nan"))) for e in vv]
            cell[f"F_VOLCTRL_mfe_{hk}_{side}"] = ratio_means(fa, va)
            cell[f"t_mfe_{hk}_{side}"] = welch_t(fa, va)


def summarize_cell(
    tf: str,
    mode: str,
    n_win: int,
    months: list[int],
    by_arm: dict[str, list[dict[str, Any]]],
    rsi_len: int = DEFAULT_RSI_LEN,
    ob: float = 70.0,
    os: float = 30.0,
) -> dict[str, Any]:
    f_rows = by_arm.get("F", [])
    v_rows = by_arm.get("VOLCTRL", [])
    u_rows = by_arm.get("U", [])
    r_rows = by_arm.get("R", [])
    c2_rows = by_arm.get("C2", [])
    cell: dict[str, Any] = {
        "tf": tf,
        "mode": mode,
        "N": n_win,
        "rsi_len": int(rsi_len),
        "ob": float(ob),
        "os": float(os),
    }
    _arm_ratios(cell, f_rows, v_rows, u_rows, r_rows, months)
    _side_split(cell, f_rows, v_rows)
    cell["gate"] = gate_line(cell)
    null: dict[str, Any] = {
        "tf": tf, "mode": mode, "N": n_win,
        "rsi_len": int(rsi_len), "ob": float(ob), "os": float(os),
    }
    _arm_ratios(null, c2_rows, v_rows, [], [], months)
    ng = gate_line(null)
    cell["null_gate"] = "NULL_PASS" if ng == "GATE PASS" else ("NULL_FAIL: " + ng[len("GATE FAIL: "):] if ng.startswith("GATE FAIL: ") else "NULL_FAIL")
    return cell


def _stamp_rsi(rows: list[dict[str, Any]], rsi_len: int, ob: float, os: float) -> None:
    for e in rows:
        e["rsi_len"] = int(rsi_len)
        e["ob"] = float(ob)
        e["os"] = float(os)


def _row_csv(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "arm": e["arm"],
        "tf": e.get("tf") or "",
        "mode": e.get("mode") or "",
        "N": e.get("N") if e.get("N") != "" else "",
        "rsi_len": e.get("rsi_len", ""),
        "ob": e.get("ob", ""),
        "os": e.get("os", ""),
        "zone_kind": e.get("zone_kind") or "",
        "touch_time_ist": ist_str(int(e["touch_time"])) if e.get("touch_time") else "",
        "signal_time_ist": ist_str(int(e["signal_time"])) if e.get("signal_time") else "",
        "entry_time_ist": ist_str(int(e["entry_time"])) if e.get("entry_time") else "",
        "adx_j": e.get("adx_j", float("nan")),
        "mfe_1h": e.get("mfe_1h", float("nan")),
        "mfe_2h": e.get("mfe_2h", float("nan")),
        "mfe_4h": e.get("mfe_4h", float("nan")),
        "absmove_1h": e.get("absmove_1h", float("nan")),
        "absmove_2h": e.get("absmove_2h", float("nan")),
        "absmove_4h": e.get("absmove_4h", float("nan")),
    }


def _write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    ev_cols = [
        "arm", "tf", "mode", "N", "rsi_len", "ob", "os", "zone_kind",
        "touch_time_ist", "signal_time_ist", "entry_time_ist", "adx_j",
        "mfe_1h", "mfe_2h", "mfe_4h",
        "absmove_1h", "absmove_2h", "absmove_4h",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ev_cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(_row_csv(r))


def main() -> None:
    t_run = time.perf_counter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    ap.add_argument("--from", dest="ym_from", default="2024-07")
    ap.add_argument("--to", dest="ym_to", default="2026-09")
    ap.add_argument("--exclude", default="2025-08,2025-09")
    ap.add_argument("--rsi-len", dest="rsi_len", default=str(DEFAULT_RSI_LEN))
    ap.add_argument("--levels", default="70/30")
    ap.add_argument("--tf", dest="tf_list", default="5m,15m,30m")
    args = ap.parse_args()

    from backtest.harness.run_archive import RunArchive
    from backtest.strategies.s018_4h_trend import run_s018 as s018
    from backtest.strategies.s020_vwap_touch import run_s020 as s020
    from backtest.strategies.s020_vwap_touch import run_s020_dev as d

    ym_lo = parse_ym(str(args.ym_from))
    ym_hi = parse_ym(str(args.ym_to))
    exclude = {parse_ym(x) for x in str(args.exclude).split(",") if x.strip()}
    months = month_list(ym_lo, ym_hi, exclude)
    tfs = parse_tf_list(str(args.tf_list))
    rsi_lens = parse_rsi_lens(str(args.rsi_len))
    levels = parse_levels(str(args.levels))
    write_all_events = len(rsi_lens) * len(levels) == 1

    csv_path = str(args.csv or s018.SPOT_CSV)
    args.csv = csv_path
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = _unique_folder(RUNS_DIR, stamp, "stage0f2")
    archive = RunArchive(
        "s022",
        "stage0f2",
        args,
        month=f"{args.ym_from}_{args.ym_to}",
        prereg_note="Stage0F-2 ULP+RSI-div grid + ADX vs VOLCTRL/C2; OOS 2025-08/09 excluded",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)
    print(
        f"grid tf={tfs} rsi_len={rsi_lens} levels={levels} "
        f"n_cells={len(tfs)*len(MODES)*len(N_WINS)*len(rsi_lens)*len(levels)}",
        flush=True,
    )

    spot = s018.load_spot_1m(csv_path)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    print(f"1m bars={len(ts)} csv={csv_path}", flush=True)
    entry_close = {int(t): float(px) for t, px in zip(ts, c)}
    print("VOLCTRL grid", flush=True)
    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    pre = rolling_pre_range_60(gh, gl)
    bins, _edges = assign_quantile_bins(pre, N_VOL_BINS)
    hod_g = ist_hod_arr(grid)
    ym_g = ist_ym_arr(grid)
    vol_cell = np.full(grid.shape, -1, dtype=np.int32)
    okb = bins >= 0
    vol_cell[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)
    rng = np.random.default_rng(SEED)

    event_rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []

    for tf in tfs:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        print(f"{tf}: bars={len(bars)} ULP+ADX once", flush=True)
        _dip, _dim, adx = adx_pine_v4(th, tl, tc, ADX_LEN, warmup=ADX_WARMUP)
        ulp_by_mode: dict[str, list[dict[str, Any]]] = {}
        u_by_mode: dict[str, list[dict[str, Any]]] = {}
        for mode in MODES:
            raw = [s for s in iter_ulp_signals(bars, mode, tf) if s["rule"] == "fresh"]
            u_all = keep_in_range(arm_u_events(raw), ym_lo, ym_hi, exclude)
            for e in u_all:
                px = _entry_px(entry_close, int(e["entry_time"]))
                if not math.isfinite(px):
                    continue
                attach_metrics(e, ts, h, l, c, px)
            ulp_by_mode[mode] = raw
            u_by_mode[mode] = u_all
            print(f"  {mode} ULP fresh={len(raw)} U={len(u_all)}", flush=True)

        for rsi_len in rsi_lens:
            rsi_arr = rsi_wilder(tc, int(rsi_len))
            print(f"  rsi_len={rsi_len} computed", flush=True)
            for ob, os in levels:
                rsi_sigs, _z = detect_from_rsi(
                    tts, to_, th, tl, tc, rsi_arr, tf_sec,
                    float(ob), float(os), EXP_OBH, EXP_OBL,
                )
                rsi_short, rsi_long = rsi_masks(rsi_sigs, tts, tf_sec)
                r_all = keep_in_range(
                    arm_r_events(rsi_short, rsi_long, adx, tts, tf_sec, tf),
                    ym_lo, ym_hi, exclude,
                )
                for e in r_all:
                    px = _entry_px(entry_close, int(e["entry_time"]))
                    if not math.isfinite(px):
                        continue
                    attach_metrics(e, ts, h, l, c, px)
                print(
                    f"    {ob:g}/{os:g} rsi_sigs={len(rsi_sigs)} R={len(r_all)}",
                    flush=True,
                )
                for mode in MODES:
                    raw = ulp_by_mode[mode]
                    u_all = u_by_mode[mode]
                    for n_win in N_WINS:
                        f_raw = match_arm_f(
                            raw, rsi_short, rsi_long, adx, tts, tf_sec, n_win,
                        )
                        f_ev = keep_in_range(f_raw, ym_lo, ym_hi, exclude)
                        for e in f_ev:
                            e["tf"] = tf
                            e["mode"] = mode
                            e["N"] = int(n_win)
                            px = _entry_px(entry_close, int(e["entry_time"]))
                            if not math.isfinite(px):
                                continue
                            attach_metrics(e, ts, h, l, c, px)
                        f_times = np.array(
                            [int(e["entry_time"]) for e in f_ev], dtype=np.int64,
                        )
                        near = forbidden_mask(grid, f_times, MIN_SEP)
                        v_ev = sample_volctrl(
                            f_ev, grid, vol_cell, hod_g, ym_g, rng, near,
                            ym_lo, ym_hi, exclude, tf, mode, n_win,
                        )
                        c2_ev = sample_c2(
                            f_ev, grid, hod_g, ym_g, rng, near,
                            ym_lo, ym_hi, exclude, tf, mode, n_win,
                        )
                        for e in v_ev + c2_ev:
                            px = _entry_px(entry_close, int(e["entry_time"]))
                            if not math.isfinite(px):
                                continue
                            attach_metrics(e, ts, h, l, c, px)
                        _stamp_rsi(f_ev, rsi_len, ob, os)
                        _stamp_rsi(u_all, rsi_len, ob, os)
                        _stamp_rsi(r_all, rsi_len, ob, os)
                        _stamp_rsi(v_ev, rsi_len, ob, os)
                        _stamp_rsi(c2_ev, rsi_len, ob, os)
                        by_arm = {
                            "F": f_ev,
                            "U": u_all,
                            "R": r_all,
                            "VOLCTRL": v_ev,
                            "C2": c2_ev,
                        }
                        if write_all_events:
                            for arm_rows in by_arm.values():
                                event_rows.extend(arm_rows)
                        else:
                            event_rows.extend(f_ev)
                        cell_s = summarize_cell(
                            tf, mode, n_win, months, by_arm, rsi_len, ob, os,
                        )
                        cells.append(cell_s)
                        print(
                            f"    {tf} {mode} N={n_win} rsi={rsi_len} "
                            f"{ob:g}/{os:g} n_F={cell_s['n_F_total']} {cell_s['gate']}",
                            flush=True,
                        )

    mark_robust(cells, rsi_lens, levels)
    n_f_pass = sum(1 for r in cells if str(r.get("gate") or "").startswith("GATE PASS"))
    n_null = sum(1 for r in cells if str(r.get("null_gate") or "").startswith("NULL_PASS"))
    n_rob = sum(1 for r in cells if r.get("robust") == "ROBUST PASS")
    robust_keys = {cell_key(r) for r in cells if r.get("robust") == "ROBUST PASS"}
    robust_events = [
        e for e in event_rows
        if e.get("arm") == "F"
        and (
            str(e.get("tf")),
            str(e.get("mode")),
            int(e.get("N") or 0),
            int(e.get("rsi_len") or 0),
            float(e.get("ob") or 0),
            float(e.get("os") or 0),
        ) in robust_keys
    ]

    if write_all_events:
        _write_events(archive.folder / "events.csv", event_rows)
    _write_events(archive.folder / "events_robust.csv", robust_events)

    st_cols = list(cells[0].keys()) if cells else ["tf", "mode", "N", "gate"]
    with (archive.folder / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=st_cols, extrasaction="ignore")
        w.writeheader()
        for r in cells:
            w.writerow(r)

    lines: list[str] = [
        "S022-F stage0F-2  ULP + RSI-div grid + Pine ADX rising",
        f"range {args.ym_from}..{args.ym_to}  exclude={sorted(exclude)}",
        f"tf={tfs} rsi_len={rsi_lens} levels={levels}",
        f"csv={csv_path}",
        f"folder={archive.folder}",
        "",
        f"total_cells={len(cells)}  F_PASS={n_f_pass}  NULL_PASS={n_null}  ROBUST={n_rob}",
        "ROBUST cells:",
    ]
    for r in cells:
        if r.get("robust") != "ROBUST PASS":
            continue
        n_is = int(r["n_F_2025-06"]) + int(r["n_F_2025-07"])
        lines.append(
            f"  {r['tf']} {r['mode']} N={r['N']} rsi={r['rsi_len']} "
            f"{r['ob']:g}/{r['os']:g}  "
            f"F/V mfe2h={_fmt(r['F_VOLCTRL_mfe_2h'])} t2={_fmt(r['t_mfe_2h'])}  "
            f"mfe4h={_fmt(r['F_VOLCTRL_mfe_4h'])} t4={_fmt(r['t_mfe_4h'])}  "
            f"n_IS={n_is}  high n={r['n_high']} n_IS={r['n_high_2025-06+07']} "
            f"2h={_fmt(r['F_VOLCTRL_mfe_2h_high'])} 4h={_fmt(r['F_VOLCTRL_mfe_4h_high'])}  "
            f"low n={r['n_low']} n_IS={r['n_low_2025-06+07']} "
            f"2h={_fmt(r['F_VOLCTRL_mfe_2h_low'])} 4h={_fmt(r['F_VOLCTRL_mfe_4h_low'])}"
        )
    if n_rob == 0:
        lines.append("  (none)")
    lines.append("")
    for r in cells:
        lines.append(
            f"=== {r['tf']} {r['mode']} N={r['N']} rsi={r['rsi_len']} "
            f"{r['ob']:g}/{r['os']:g} ==="
        )
        lines.append(
            f"  n_F_total={r['n_F_total']}  n_F_2025-06={r['n_F_2025-06']}  "
            f"n_F_2025-07={r['n_F_2025-07']}  median_n_F/month={_fmt(r['median_n_F_per_month'])}"
        )
        for met in ("mfe", "absmove"):
            bits = []
            for hk in ("1h", "2h", "4h"):
                bits.append(
                    f"{hk} F/V={_fmt(r[f'F_VOLCTRL_{met}_{hk}'])} "
                    f"t={_fmt(r[f't_{met}_{hk}'])}"
                )
            lines.append(f"  F/VOLCTRL {met}: " + "  ".join(bits))
            lines.append(
                f"    U/VOLCTRL {met}: "
                + "  ".join(
                    f"{hk}={_fmt(r[f'U_VOLCTRL_{met}_{hk}'])}" for hk in ("1h", "2h", "4h")
                )
            )
            lines.append(
                f"    R/VOLCTRL {met}: "
                + "  ".join(
                    f"{hk}={_fmt(r[f'R_VOLCTRL_{met}_{hk}'])}" for hk in ("1h", "2h", "4h")
                )
            )
        lines.append(
            "  % months F/V>1: "
            f"mfe2h={_fmt(r['pct_months_gt1_mfe_2h'])}%  "
            f"mfe4h={_fmt(r['pct_months_gt1_mfe_4h'])}%  "
            f"abs2h={_fmt(r['pct_months_gt1_absmove_2h'])}%  "
            f"abs4h={_fmt(r['pct_months_gt1_absmove_4h'])}%"
        )
        lines.append(
            f"  side high: n={r['n_high']} n_06+07={r['n_high_2025-06+07']} "
            f"mfe2h={_fmt(r['F_VOLCTRL_mfe_2h_high'])} t={_fmt(r['t_mfe_2h_high'])} "
            f"mfe4h={_fmt(r['F_VOLCTRL_mfe_4h_high'])} t={_fmt(r['t_mfe_4h_high'])}"
        )
        lines.append(
            f"  side low:  n={r['n_low']} n_06+07={r['n_low_2025-06+07']} "
            f"mfe2h={_fmt(r['F_VOLCTRL_mfe_2h_low'])} t={_fmt(r['t_mfe_2h_low'])} "
            f"mfe4h={_fmt(r['F_VOLCTRL_mfe_4h_low'])} t={_fmt(r['t_mfe_4h_low'])}"
        )
        lines.append(f"  {r['null_gate']}")
        lines.append(f"  {r['gate']}")
        lines.append(f"  {r['robust']}")
        lines.append("")
    elapsed = time.perf_counter() - t_run
    lines.append(f"elapsed_sec={elapsed:.1f}")
    (archive.folder / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("".join(x + "\n" for x in lines), flush=True)
    print(f"done folder={archive.folder} elapsed={elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
