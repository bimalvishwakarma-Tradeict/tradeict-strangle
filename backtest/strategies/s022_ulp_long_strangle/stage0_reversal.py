#!/usr/bin/env python3
"""S022 Stage 0R: ULP reversal (direction) — wick vs full, distance-matched control.

Spot only. No option sim.

python backtest/strategies/s022_ulp_long_strangle/stage0_reversal.py
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    IST_OFF,
    MIN_SEP,
    N_VOL_BINS,
    RANGE_YM_HI,
    RANGE_YM_LO,
    SEED,
    assign_quantile_bins,
    ffill_1m_grid,
    forbidden_mask,
    grouped_cell_indices,
    ist_hod,
    ist_ym,
    rolling_pre_range_60,
    sample_control_times,
)

IST_OFF = IST_OFF  # re-export for tests
N_CTRL = 20
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
R_LIST = (200, 500, 1000)
H_KEYS = ("2h", "4h", "8h")
H_SEC = {"2h": 7200, "4h": 14400, "8h": 28800}
H4 = 14400
PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return "nan"
        return f"{x:.6g}"
    return str(x)


def _unique_folder(root: Path, stamp: str, tag: str = "stage0_reversal") -> Path:
    folder = root / f"{stamp}_{tag}"
    n = 0
    while folder.exists():
        n += 1
        folder = root / f"{stamp}_{tag}_{n}"
    return folder


def zone_direction(kind: str) -> int:
    """High zone expects down (-1); low zone expects up (+1)."""
    return -1 if str(kind) == "high" else 1


def zone_extreme(kind: str, zone_top: float, zone_bottom: float) -> float:
    if str(kind) == "high":
        return float(zone_top)
    return float(zone_bottom)


def distance_to_extreme(entry: float, direction: int, extreme: float) -> tuple[float, bool]:
    """Points from entry to extreme in the adverse direction. D=0 if already beyond."""
    e = float(entry)
    x = float(extreme)
    if int(direction) < 0:
        if e >= x:
            return 0.0, True
        return x - e, False
    if e <= x:
        return 0.0, True
    return e - x, False


def collapse_events_by_zone_kind(sigs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One event per (tf, mode, signal_ts, zone_kind)."""
    groups: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, int, str]] = []
    for s in sigs:
        if str(s.get("rule")) != "fresh":
            continue
        key = (str(s["tf"]), str(s["mode"]), int(s["signal_ts"]), str(s["zone_kind"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)
    out: list[dict[str, Any]] = []
    for key in order:
        rows = groups[key]
        r0 = rows[0]
        sts = int(r0["signal_ts"])
        kind = str(r0["zone_kind"])
        entry = float(r0["spot_close"])
        top = float(r0["zone_top"])
        bot = float(r0["zone_bottom"])
        dirc = zone_direction(kind)
        extreme = zone_extreme(kind, top, bot)
        dist, broken = distance_to_extreme(entry, dirc, extreme)
        out.append(
            {
                "tf": str(r0["tf"]),
                "mode": str(r0["mode"]),
                "signal_ts": sts,
                "signal_ist": str(r0.get("signal_ist") or ""),
                "entry": entry,
                "zone_kind": kind,
                "pivot_ts": int(r0["pivot_ts"]),
                "zone_top": top,
                "zone_bottom": bot,
                "direction": dirc,
                "extreme": extreme,
                "D": dist,
                "broken_on_signal_bar": broken,
                "n_raw": len(rows),
                "hod": ist_hod(sts),
                "ym": ist_ym(sts),
            }
        )
    return out


def forward_slice(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    signal_ts: int,
    horizon_sec: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """1m bars with ts strictly after signal_ts, up to signal_ts + horizon_sec."""
    t0 = int(signal_ts)
    t1 = t0 + int(horizon_sec)
    i0 = int(np.searchsorted(ts, t0, side="right"))
    i1 = int(np.searchsorted(ts, t1, side="right"))
    return ts[i0:i1], hi[i0:i1], lo[i0:i1]


def scan_reversal(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    entry: float,
    direction: int,
    D: float,
    r_list: tuple[int, ...] = R_LIST,
) -> dict[str, Any]:
    """First win time per R and first extreme-break time on the given path."""
    empty_w = {int(r): None for r in r_list}
    if ts.size == 0:
        return {
            "t_loss": None,
            "t_win": empty_w,
            "mfe_dir": float("nan"),
            "mae_dir": float("nan"),
        }
    e = float(entry)
    d = float(D)
    if int(direction) > 0:
        fav = hi.astype(np.float64) - e
        adv = e - lo.astype(np.float64)
        loss_mask = lo.astype(np.float64) <= (e - d) if d > 0 else np.zeros(hi.shape, dtype=bool)
    else:
        fav = e - lo.astype(np.float64)
        adv = hi.astype(np.float64) - e
        loss_mask = hi.astype(np.float64) >= (e + d) if d > 0 else np.zeros(hi.shape, dtype=bool)
    t_loss: int | None = int(ts[int(np.argmax(loss_mask))]) if bool(np.any(loss_mask)) else None
    t_win: dict[int, int | None] = {}
    for r in r_list:
        hit = fav >= float(r)
        t_win[int(r)] = int(ts[int(np.argmax(hit))]) if bool(np.any(hit)) else None
    return {
        "t_loss": t_loss,
        "t_win": t_win,
        "mfe_dir": float(np.max(fav)),
        "mae_dir": float(np.max(adv)),
    }


def classify_outcome(
    t_win: int | None,
    t_loss: int | None,
    t_end: int,
) -> str:
    """WIN if R is hit strictly before extreme; same-bar both-hit is LOSS."""
    win_ok = t_win is not None and int(t_win) <= int(t_end)
    loss_ok = t_loss is not None and int(t_loss) <= int(t_end)
    if win_ok and loss_ok:
        if int(t_win) < int(t_loss):
            return "WIN"
        return "LOSS"
    if loss_ok:
        return "LOSS"
    if win_ok:
        return "WIN"
    return "OPEN"


def wick_zone_from_pivot(kind: str, bar: dict[str, Any]) -> tuple[float, float]:
    o = float(bar["open"])
    h = float(bar["high"])
    l = float(bar["low"])
    c = float(bar["close"])
    if str(kind) == "high":
        return h, max(o, c)
    return min(o, c), l


def reached_wick_zone(
    hi: np.ndarray,
    lo: np.ndarray,
    kind: str,
    wick_top: float,
    wick_bottom: float,
) -> bool:
    if hi.size == 0:
        return False
    if str(kind) == "high":
        return bool(np.any(hi.astype(np.float64) >= float(wick_bottom)))
    return bool(np.any(lo.astype(np.float64) <= float(wick_top)))


def classify_missed_reversals(
    full_events: list[dict[str, Any]],
    wick_events: list[dict[str, Any]],
    pivot_bars: dict[int, dict[str, Any]],
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    R: int = 500,
    H: str = "4h",
) -> list[dict[str, Any]]:
    """Match (zone_kind, pivot_ts). Class A = full-only buffer catch that WINs."""
    h_sec = int(H_SEC[H])
    full_map: dict[tuple[str, int], dict[str, Any]] = {}
    for e in full_events:
        k = (str(e["zone_kind"]), int(e["pivot_ts"]))
        if k not in full_map or int(e["signal_ts"]) < int(full_map[k]["signal_ts"]):
            full_map[k] = e
    wick_map: dict[tuple[str, int], dict[str, Any]] = {}
    for e in wick_events:
        k = (str(e["zone_kind"]), int(e["pivot_ts"]))
        if k not in wick_map or int(e["signal_ts"]) < int(wick_map[k]["signal_ts"]):
            wick_map[k] = e
    keys = sorted(set(full_map) | set(wick_map), key=lambda x: (x[0], x[1]))
    rows: list[dict[str, Any]] = []
    for k in keys:
        kind, pivot_ts = k
        fe = full_map.get(k)
        we = wick_map.get(k)
        if fe is not None and we is None:
            pb = pivot_bars.get(int(pivot_ts))
            never_wick = False
            if pb is not None and not bool(fe.get("broken_on_signal_bar")):
                wt, wb = wick_zone_from_pivot(kind, pb)
                t0 = int(fe["signal_ts"])
                sl = int(fe.get("t_loss") or 0)
                cap = t0 + h_sec
                if sl > 0:
                    cap = min(cap, sl)
                fts, fhi, flo = forward_slice(ts, hi, lo, t0, cap - t0)
                never_wick = not reached_wick_zone(fhi, flo, kind, wt, wb)
            oc = str(fe.get(f"oc_{R}_{H}") or "")
            is_a = never_wick and oc == "WIN"
            rows.append(
                {
                    "class": "A" if is_a else "full_only",
                    "zone_kind": kind,
                    "pivot_ts": int(pivot_ts),
                    "never_reached_wick": never_wick,
                    "full_oc": oc,
                    "wick_oc": "",
                    "full_entry": float(fe["entry"]),
                    "wick_entry": float("nan"),
                    "entry_adv_pts": float("nan"),
                    "full_D": float(fe["D"]),
                    "wick_D": float("nan"),
                    "reversal_only_full_catches": is_a,
                }
            )
        elif fe is not None and we is not None:
            dirc = int(fe["direction"])
            adv = float(dirc) * (float(we["entry"]) - float(fe["entry"]))
            rows.append(
                {
                    "class": "B",
                    "zone_kind": kind,
                    "pivot_ts": int(pivot_ts),
                    "never_reached_wick": False,
                    "full_oc": str(fe.get(f"oc_{R}_{H}") or ""),
                    "wick_oc": str(we.get(f"oc_{R}_{H}") or ""),
                    "full_entry": float(fe["entry"]),
                    "wick_entry": float(we["entry"]),
                    "entry_adv_pts": adv,
                    "full_D": float(fe["D"]),
                    "wick_D": float(we["D"]),
                    "reversal_only_full_catches": False,
                }
            )
        else:
            rows.append(
                {
                    "class": "C",
                    "zone_kind": kind,
                    "pivot_ts": int(pivot_ts),
                    "never_reached_wick": False,
                    "full_oc": "",
                    "wick_oc": str(we.get(f"oc_{R}_{H}") or "") if we else "",
                    "full_entry": float("nan"),
                    "wick_entry": float(we["entry"]) if we else float("nan"),
                    "entry_adv_pts": float("nan"),
                    "full_D": float("nan"),
                    "wick_D": float(we["D"]) if we else float("nan"),
                    "reversal_only_full_catches": False,
                }
            )
    return rows


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def binom_z_p(k: int, n: int, p0: float) -> tuple[float, float]:
    if n <= 0 or not math.isfinite(p0) or p0 <= 0.0 or p0 >= 1.0:
        return float("nan"), float("nan")
    phat = float(k) / float(n)
    se = math.sqrt(p0 * (1.0 - p0) / float(n))
    if se <= 0.0:
        return float("nan"), float("nan")
    z = (phat - p0) / se
    return float(z), float(_norm_sf(z))


def _pct(num: int, den: int) -> float:
    if den <= 0:
        return float("nan")
    return 100.0 * float(num) / float(den)


def _median(xs: list[float]) -> float:
    a = [float(x) for x in xs if math.isfinite(float(x))]
    if not a:
        return float("nan")
    a.sort()
    m = len(a) // 2
    if len(a) % 2:
        return a[m]
    return 0.5 * (a[m - 1] + a[m])


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


def _annotate_event(
    e: dict[str, Any],
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
) -> None:
    t0 = int(e["signal_ts"])
    fts, fhi, flo = forward_slice(ts, hi, lo, t0, H_SEC["8h"])
    fts4, fhi4, flo4 = forward_slice(ts, hi, lo, t0, H4)
    if bool(e["broken_on_signal_bar"]):
        e["mfe_dir_4h"] = float("nan")
        e["mae_dir_4h"] = float("nan")
        e["t_loss"] = None
        e["t_win"] = {int(r): None for r in R_LIST}
        for r in R_LIST:
            for hk in H_KEYS:
                e[f"oc_{r}_{hk}"] = "BROKEN"
                e[f"t_win_{r}_{hk}"] = ""
                e[f"t_loss_{hk}"] = ""
        return
    sc8 = scan_reversal(fts, fhi, flo, float(e["entry"]), int(e["direction"]), float(e["D"]))
    sc4 = scan_reversal(fts4, fhi4, flo4, float(e["entry"]), int(e["direction"]), float(e["D"]))
    e["mfe_dir_4h"] = sc4["mfe_dir"]
    e["mae_dir_4h"] = sc4["mae_dir"]
    e["t_loss"] = sc8["t_loss"]
    e["t_win"] = sc8["t_win"]
    for r in R_LIST:
        tw = sc8["t_win"].get(int(r))
        for hk in H_KEYS:
            t_end = t0 + int(H_SEC[hk])
            oc = classify_outcome(tw, sc8["t_loss"], t_end)
            e[f"oc_{r}_{hk}"] = oc
            e[f"t_win_{r}_{hk}"] = "" if tw is None else int(tw)
            e[f"t_loss_{hk}"] = "" if sc8["t_loss"] is None else int(sc8["t_loss"])
            if oc == "WIN" and tw is not None:
                e[f"mins_to_win_{r}_{hk}"] = (int(tw) - t0) / 60.0
            else:
                e[f"mins_to_win_{r}_{hk}"] = float("nan")
            if oc == "LOSS" and sc8["t_loss"] is not None:
                e[f"mins_to_loss_{r}_{hk}"] = (int(sc8["t_loss"]) - t0) / 60.0
            else:
                e[f"mins_to_loss_{r}_{hk}"] = float("nan")


def _control_win_flags(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    ctrl_ts: np.ndarray,
    close_at: dict[int, float],
    direction: int,
    D: float,
) -> dict[tuple[int, str], list[int]]:
    out: dict[tuple[int, str], list[int]] = {(r, hk): [] for r in R_LIST for hk in H_KEYS}
    dirc = int(direction)
    dist = float(D)
    if dist <= 0:
        return out
    for ct in ctrl_ts:
        t0 = int(ct)
        entry = close_at.get(t0)
        if entry is None:
            continue
        fts, fhi, flo = forward_slice(ts, hi, lo, t0, H_SEC["8h"])
        sc = scan_reversal(fts, fhi, flo, float(entry), dirc, dist)
        for r in R_LIST:
            tw = sc["t_win"].get(int(r))
            for hk in H_KEYS:
                oc = classify_outcome(tw, sc["t_loss"], t0 + int(H_SEC[hk]))
                out[(int(r), hk)].append(1 if oc == "WIN" else 0)
    return out


def main() -> None:
    t_run = time.perf_counter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    from backtest.harness.run_archive import RunArchive
    from backtest.strategies.s018_4h_trend import run_s018 as s018
    from backtest.strategies.s020_vwap_touch import run_s020 as s020
    from backtest.strategies.s020_vwap_touch import run_s020_dev as d
    from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals
    from openpyxl import Workbook

    csv_path = str(args.csv or s018.SPOT_CSV)
    args.csv = csv_path
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = _unique_folder(RUNS_DIR, stamp, "stage0_reversal")
    archive = RunArchive(
        "s022",
        "stage0_reversal",
        args,
        month="2024-07_2026-09",
        prereg_note="Stage 0R ULP reversal wick vs full + distance-matched VOLMATCH control; spot only",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    spot = s018.load_spot_1m(csv_path)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    print(f"1m bars={len(ts)} csv={csv_path}", flush=True)
    close_at = {int(t): float(px) for t, px in zip(ts, c)}

    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    for t, px in zip(grid, gc):
        close_at.setdefault(int(t), float(px))
    pre = rolling_pre_range_60(gh, gl)
    bins, _edges = assign_quantile_bins(pre)
    hod_g = (((grid.astype(np.int64) + IST_OFF) // 3600) % 24).astype(np.int16)
    cell = np.full(int(grid.size), -1, dtype=np.int32)
    okb = bins >= 0
    cell[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)
    g0 = int(grid[0])
    n_grid = int(grid.size)
    grid_index = {int(t): i for i, t in enumerate(grid.tolist())}

    rng = np.random.default_rng(SEED)
    n_dup_collapsed = 0
    n_broken = 0
    events_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    pivot_bars: dict[tuple[str, int], dict[str, Any]] = {}
    all_events: list[dict[str, Any]] = []
    ctrl_win: dict[tuple[str, str, int, str], list[int]] = defaultdict(list)
    month_sig: dict[tuple[int, str, str], list[str]] = defaultdict(list)
    month_ctrl: dict[tuple[int, str, str], list[int]] = defaultdict(list)
    event_id = 0

    for tf in TFS:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        for b in bars:
            pivot_bars[(tf, int(b["time"]))] = b
        print(f"tf={tf} bars={len(bars)}", flush=True)
        for mode in MODES:
            raw = iter_ulp_signals(bars, mode, tf)
            fresh = [s for s in raw if s["rule"] == "fresh"]
            collapsed = collapse_events_by_zone_kind(fresh)
            n_dup_collapsed += max(0, len(fresh) - len(collapsed))
            events = [
                e for e in collapsed
                if RANGE_YM_LO <= int(e["ym"]) <= RANGE_YM_HI
            ]
            print(f"  mode={mode} fresh={len(fresh)} events={len(events)}", flush=True)
            sig_arr = np.array([int(e["signal_ts"]) for e in events], dtype=np.int64)
            near = forbidden_mask(grid, sig_arr, MIN_SEP)
            groups = grouped_cell_indices(
                cell,
                np.isfinite(pre) & (~near) & (cell >= 0),
            )
            annotated: list[dict[str, Any]] = []
            for e in events:
                _annotate_event(e, ts, h, l)
                if bool(e["broken_on_signal_bar"]):
                    n_broken += 1
                event_id += 1
                e["event_id"] = event_id
                annotated.append(e)
                all_events.append(e)
                if bool(e["broken_on_signal_bar"]):
                    continue
                t0 = int(e["signal_ts"])
                gi = grid_index.get(t0)
                if gi is None:
                    gi = int((t0 - g0) // 60)
                    if gi < 0 or gi >= n_grid:
                        continue
                cid = int(cell[gi]) if 0 <= gi < n_grid else -1
                pool = groups[cid] if 0 <= cid < len(groups) else np.zeros(0, dtype=np.int64)
                if pool.size == 0:
                    continue
                picked = sample_control_times(grid[pool], N_CTRL, rng)
                flags = _control_win_flags(
                    ts, h, l, picked, close_at, int(e["direction"]), float(e["D"]),
                )
                ym = int(e["ym"])
                for r in R_LIST:
                    for hk in H_KEYS:
                        ctrl_win[(tf, mode, int(r), hk)].extend(flags[(int(r), hk)])
                month_sig[(ym, tf, mode)].append(str(e.get("oc_500_4h") or ""))
                month_ctrl[(ym, tf, mode)].extend(flags[(500, "4h")])
            events_by[(tf, mode)] = annotated

    stats_rows: list[dict[str, Any]] = []
    for tf in TFS:
        for mode in MODES:
            evs = [e for e in events_by.get((tf, mode), []) if not bool(e["broken_on_signal_bar"])]
            n_br = sum(1 for e in events_by.get((tf, mode), []) if bool(e["broken_on_signal_bar"]))
            med_d = _median([float(e["D"]) for e in evs])
            for r in R_LIST:
                for hk in H_KEYS:
                    ocs = [str(e.get(f"oc_{r}_{hk}") or "") for e in evs]
                    n = len(ocs)
                    nw = sum(1 for x in ocs if x == "WIN")
                    nl = sum(1 for x in ocs if x == "LOSS")
                    no = sum(1 for x in ocs if x == "OPEN")
                    cw = ctrl_win.get((tf, mode, int(r), hk), [])
                    p0 = (float(sum(cw)) / float(len(cw))) if cw else float("nan")
                    z, pv = binom_z_p(nw, n, p0)
                    win_pct = _pct(nw, n)
                    ctrl_pct = 100.0 * p0 if math.isfinite(p0) else float("nan")
                    edge = win_pct - ctrl_pct if math.isfinite(win_pct) and math.isfinite(ctrl_pct) else float("nan")
                    stats_rows.append(
                        {
                            "tf": tf,
                            "mode": mode,
                            "R": int(r),
                            "H": hk,
                            "n": n,
                            "n_broken_signal_bar": n_br,
                            "n_win": nw,
                            "n_loss": nl,
                            "n_open": no,
                            "win_pct": win_pct,
                            "loss_pct": _pct(nl, n),
                            "open_pct": _pct(no, n),
                            "n_ctrl": len(cw),
                            "control_win_pct": ctrl_pct,
                            "edge_pct": edge,
                            "z": z,
                            "p_ge": pv,
                            "median_D": med_d,
                        }
                    )

    missed_rows: list[dict[str, Any]] = []
    missed_summary: list[dict[str, Any]] = []
    for tf in TFS:
        full_e = events_by.get((tf, "full"), [])
        wick_e = events_by.get((tf, "wick"), [])
        pb = {pt: b for (tfn, pt), b in pivot_bars.items() if tfn == tf}
        rows = classify_missed_reversals(full_e, wick_e, pb, ts, h, l, R=500, H="4h")
        for r in rows:
            r["tf"] = tf
            missed_rows.append(r)
        n = len(rows) if rows else 1
        nA = sum(1 for r in rows if r["class"] == "A")
        nB = sum(1 for r in rows if r["class"] == "B")
        nC = sum(1 for r in rows if r["class"] == "C")
        n_full_only = sum(1 for r in rows if r["class"] in ("A", "full_only"))
        missed_summary.append(
            {
                "tf": tf,
                "n_matched_zones": len(rows),
                "n_class_A": nA,
                "pct_A": _pct(nA, len(rows)),
                "n_full_only": n_full_only,
                "n_class_B": nB,
                "pct_B": _pct(nB, len(rows)),
                "n_class_C": nC,
                "pct_C": _pct(nC, len(rows)),
                "median_D_wick": _median([float(e["D"]) for e in wick_e if not e["broken_on_signal_bar"]]),
                "median_D_full": _median([float(e["D"]) for e in full_e if not e["broken_on_signal_bar"]]),
                "n_B_full_win_wick_loss": sum(
                    1 for r in rows if r["class"] == "B" and r["full_oc"] == "WIN" and r["wick_oc"] == "LOSS"
                ),
                "n_B_both_win": sum(
                    1 for r in rows if r["class"] == "B" and r["full_oc"] == "WIN" and r["wick_oc"] == "WIN"
                ),
                "median_entry_adv_pts_B": _median(
                    [float(r["entry_adv_pts"]) for r in rows if r["class"] == "B"]
                ),
            }
        )
        _ = n

    month_rows: list[dict[str, Any]] = []
    yms = sorted({k[0] for k in month_sig})
    for ym in yms:
        for tf in TFS:
            for mode in MODES:
                ocs = month_sig.get((ym, tf, mode), [])
                n = len(ocs)
                nw = sum(1 for x in ocs if x == "WIN")
                cw = month_ctrl.get((ym, tf, mode), [])
                p0 = (float(sum(cw)) / float(len(cw))) if cw else float("nan")
                win_pct = _pct(nw, n)
                ctrl_pct = 100.0 * p0 if math.isfinite(p0) else float("nan")
                month_rows.append(
                    {
                        "ym": ym,
                        "tf": tf,
                        "mode": mode,
                        "R": 500,
                        "H": "4h",
                        "n": n,
                        "n_win": nw,
                        "win_pct": win_pct,
                        "n_ctrl": len(cw),
                        "control_win_pct": ctrl_pct,
                        "edge_pct": (
                            win_pct - ctrl_pct
                            if math.isfinite(win_pct) and math.isfinite(ctrl_pct)
                            else float("nan")
                        ),
                    }
                )

    ev_cols = [
        "event_id", "tf", "mode", "signal_ts", "signal_ist", "hod", "ym",
        "zone_kind", "pivot_ts", "direction", "entry", "extreme", "D",
        "broken_on_signal_bar", "n_raw", "mfe_dir_4h", "mae_dir_4h",
    ]
    for r in R_LIST:
        for hk in H_KEYS:
            ev_cols.append(f"oc_{r}_{hk}")
            ev_cols.append(f"mins_to_win_{r}_{hk}")
            ev_cols.append(f"mins_to_loss_{r}_{hk}")

    ev_path = archive.folder / "events.csv"
    with ev_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=ev_cols, extrasaction="ignore")
        w.writeheader()
        for e in all_events:
            row = dict(e)
            row["broken_on_signal_bar"] = int(bool(e["broken_on_signal_bar"]))
            w.writerow(row)

    st_cols = [
        "tf", "mode", "R", "H", "n", "n_broken_signal_bar", "n_win", "n_loss", "n_open",
        "win_pct", "loss_pct", "open_pct", "n_ctrl", "control_win_pct", "edge_pct",
        "z", "p_ge", "median_D",
    ]
    st_path = archive.folder / "stats.csv"
    with st_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=st_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(stats_rows)

    miss_cols = [
        "tf", "class", "zone_kind", "pivot_ts", "never_reached_wick",
        "reversal_only_full_catches", "full_oc", "wick_oc",
        "full_entry", "wick_entry", "entry_adv_pts", "full_D", "wick_D",
    ]
    miss_path = archive.folder / "missed.csv"
    with miss_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=miss_cols, extrasaction="ignore")
        w.writeheader()
        for r in missed_rows:
            row = dict(r)
            row["never_reached_wick"] = int(bool(r["never_reached_wick"]))
            row["reversal_only_full_catches"] = int(bool(r["reversal_only_full_catches"]))
            w.writerow(row)

    mo_cols = [
        "ym", "tf", "mode", "R", "H", "n", "n_win", "win_pct",
        "n_ctrl", "control_win_pct", "edge_pct",
    ]
    mo_path = archive.folder / "per_month.csv"
    with mo_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=mo_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(month_rows)

    wb = Workbook()

    def _sheet(title: str, rows: list[dict[str, Any]], cols: list[str]) -> None:
        ws = wb.active if title == "stats" else wb.create_sheet(title)
        if title == "stats":
            ws.title = "stats"
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])

    _sheet("stats", stats_rows, st_cols)
    _sheet("missed_summary", missed_summary, list(missed_summary[0].keys()) if missed_summary else ["tf"])
    _sheet("missed", missed_rows, miss_cols)
    _sheet("per_month", month_rows, mo_cols)
    xlsx_path = archive.folder / "stage0_reversal.xlsx"
    wb.save(xlsx_path)

    elapsed = time.perf_counter() - t_run
    lines: list[str] = [
        "S022 Stage 0R — ULP reversal (direction) wick vs full + distance-matched control",
        f"folder={archive.folder}",
        f"range IST months {RANGE_YM_LO}..{RANGE_YM_HI}  csv={csv_path}",
        f"seed={SEED}  n_ctrl={N_CTRL}  min_sep_sec={MIN_SEP}",
        "WIN = R pts in expected dir BEFORE extreme broken; same-bar both-hit = LOSS.",
        "Control: 20 random minutes, same (pre_range_60 20-q × IST hour) cell, same dir, same D.",
        f"n_events={len(all_events)}  collapsed_raw_dups={n_dup_collapsed}  broken_on_signal_bar={n_broken}",
        f"runtime_sec={elapsed:.1f}",
        "",
    ]
    for tf in TFS:
        for mode in MODES:
            lines.append(f"=== {tf} {mode} ===")
            block = [r for r in stats_rows if r["tf"] == tf and r["mode"] == mode]
            if not block:
                continue
            lines.append(
                f"  n={block[0]['n']} broken_on_signal={block[0]['n_broken_signal_bar']} "
                f"median_D={_fmt(block[0]['median_D'])}"
            )
            for r in R_LIST:
                for hk in H_KEYS:
                    row = next(x for x in block if x["R"] == r and x["H"] == hk)
                    lines.append(
                        f"  [R={r} {hk}] n={row['n']} "
                        f"win%={_fmt(row['win_pct'])} loss%={_fmt(row['loss_pct'])} "
                        f"open%={_fmt(row['open_pct'])} ctrl_win%={_fmt(row['control_win_pct'])} "
                        f"edge={_fmt(row['edge_pct'])} z={_fmt(row['z'])} p_ge={_fmt(row['p_ge'])}"
                    )
            lines.append("")
    lines.append("--- missed reversals (match tf, zone_kind, pivot_ts); A = full buffer WIN R=500 H=4h, never reached wick ---")
    for r in missed_summary:
        lines.append(
            f"  {r['tf']}: zones={r['n_matched_zones']} "
            f"A(full-only WIN)={r['n_class_A']} ({_fmt(r['pct_A'])}%) "
            f"full_only={r['n_full_only']} B(both)={r['n_class_B']} ({_fmt(r['pct_B'])}%) "
            f"C(wick-only)={r['n_class_C']} ({_fmt(r['pct_C'])}%) "
            f"median_D wick={_fmt(r['median_D_wick'])} full={_fmt(r['median_D_full'])} "
            f"B fullWIN/wickLOSS={r['n_B_full_win_wick_loss']} B bothWIN={r['n_B_both_win']} "
            f"median entry_adv_pts (dir, full vs wick)={_fmt(r['median_entry_adv_pts_B'])}"
        )
    lines.append("")
    lines.append("--- per month win% R=500 H=4h ---")
    for r in month_rows:
        lines.append(
            f"  {r['ym']} {r['tf']} {r['mode']} n={r['n']} "
            f"win%={_fmt(r['win_pct'])} ctrl%={_fmt(r['control_win_pct'])} edge={_fmt(r['edge_pct'])}"
        )
    lines.append("")
    text = "\n".join(lines)
    sum_path = archive.folder / "summary.txt"
    sum_path.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    print(f"wrote {ev_path}", flush=True)
    print(f"wrote {st_path}", flush=True)
    print(f"wrote {miss_path}", flush=True)
    print(f"wrote {mo_path}", flush=True)
    print(f"wrote {xlsx_path}", flush=True)
    print(f"wrote {sum_path}", flush=True)
    print(f"runtime_sec={elapsed:.1f}", flush=True)

    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
