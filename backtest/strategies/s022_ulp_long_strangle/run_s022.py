#!/usr/bin/env python3
"""S022 ULP rolling ITM long strangle backtest (smoke / grid).

python backtest/strategies/s022_ulp_long_strangle/run_s022.py --month 2025-06 --days 3 --smoke
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.harness.run_archive import RunArchive  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s021_hedged_long_straddle import run_s021 as s021  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    MIN_SEP,
    SEED,
    assign_quantile_bins,
    collapse_events,
    ffill_1m_grid,
    forbidden_mask,
    grouped_cell_indices,
    ist_hod,
    ist_hod_arr,
    ist_ym,
    rolling_pre_range_60,
    sample_control_times,
)
from backtest.strategies.s022_ulp_long_strangle.sim import (  # noqa: E402
    A_GRID,
    DTE_GRID,
    SL_GRID,
    T_GRID,
    X_ITM,
    expiry_date,
    map_src,
    simulate_path,
    sl_level,
    take_in_order,
)

PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
ARMS = ("ulp", "c2", "volctrl")


class S021Market:
    def __init__(
        self,
        store: Any,
        book: s021.MarkBook,
        spot_c: dict[int, float],
        grid: np.ndarray,
        gc: np.ndarray,
    ) -> None:
        self.store = store
        self.book = book
        self.spot_c = spot_c
        self.grid = grid
        self.gc = gc

    def spot(self, t: int) -> float | None:
        minute = (int(t) // 60) * 60
        if minute in self.spot_c:
            return float(self.spot_c[minute])
        for age in range(60, 3600, 60):
            v = self.spot_c.get(minute - age)
            if v is not None:
                return float(v)
        return None

    def quote(self, symbol: str, t: int) -> tuple[float, str] | None:
        q = self.book.quote(symbol, t)
        if q is None:
            return None
        return float(q.px), map_src(str(q.src))

    def chain(self, exp: Any, t: int) -> tuple[list[dict[str, Any]], str] | None:
        packed = s021.load_chain_cached(self.store, exp, t)
        if packed is None:
            return None
        rows, src = packed
        return list(rows), map_src(str(src))

    def ensure(self, symbol: str, exp: Any) -> None:
        self.book.ensure(self.store, symbol, exp)

    def mark_array(self, symbol: str) -> tuple[int, np.ndarray] | None:
        a = self.book.arr.get(symbol)
        if a is None:
            return None
        return int(self.book.t0), a

    def spot_array(self) -> tuple[int, np.ndarray] | None:
        if self.grid.size == 0:
            return None
        return int(self.grid[0]), self.gc


def _bars_from_ohlc(tts: Any, to_: Any, th: Any, tl: Any, tc: Any) -> list[dict[str, Any]]:
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


def _ulp_events(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    tfs: list[str],
    modes: list[str],
    start_ts: int,
    cutoff: int,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for tf in tfs:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        for mode in modes:
            raw = [s for s in iter_ulp_signals(bars, mode, tf) if s["rule"] == "fresh"]
            ev = collapse_events(raw)
            kept = [e for e in ev if start_ts <= int(e["signal_ts"]) < cutoff]
            for i, e in enumerate(kept):
                e["event_id"] = f"{tf}|{mode}|{i}|{int(e['signal_ts'])}"
                e["arm"] = "ulp"
            out[(tf, mode)] = kept
    return out


def _c2_events(
    ulp: list[dict[str, Any]],
    ts_1m: np.ndarray,
    near: np.ndarray,
    hod_1m: np.ndarray,
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    by_pool: dict[tuple[int, int], np.ndarray] = {}
    ym_1m = None
    from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import ist_ym_arr

    ym_1m = ist_ym_arr(ts_1m)
    out: list[dict[str, Any]] = []
    for i, e in enumerate(ulp):
        hod = int(e.get("hod") if e.get("hod") is not None else ist_hod(int(e["signal_ts"])))
        ym = int(e.get("ym") if e.get("ym") is not None else ist_ym(int(e["signal_ts"])))
        k = (hod, ym)
        if k not in by_pool:
            m = (hod_1m == hod) & (ym_1m == ym) & (~near)
            by_pool[k] = ts_1m[m]
        picked = sample_control_times(by_pool[k], 1, rng)
        if picked.size == 0:
            continue
        ct = int(picked[0])
        out.append(
            {
                "event_id": f"c2|{e.get('tf')}|{e.get('mode')}|{i}|{ct}",
                "arm": "c2",
                "tf": e["tf"],
                "mode": e["mode"],
                "signal_ts": ct,
                "signal_ist": s018.ist_str(ct),
                "hod": ist_hod(ct),
                "ym": ist_ym(ct),
                "src_event": e["event_id"],
            }
        )
    return out


def _volctrl_events(
    ulp: list[dict[str, Any]],
    grid: np.ndarray,
    cell: np.ndarray,
    groups: list[np.ndarray],
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    g0 = int(grid[0]) if grid.size else 0
    n = int(grid.size)
    out: list[dict[str, Any]] = []
    for i, e in enumerate(ulp):
        t0 = int(e["signal_ts"])
        gi = int((t0 - g0) // 60)
        if gi < 0 or gi >= n:
            continue
        cid = int(cell[gi])
        pool = groups[cid] if 0 <= cid < len(groups) else np.zeros(0, dtype=np.int64)
        if pool.size == 0:
            continue
        picked = sample_control_times(grid[pool], 1, rng)
        if picked.size == 0:
            continue
        ct = int(picked[0])
        out.append(
            {
                "event_id": f"vol|{e.get('tf')}|{e.get('mode')}|{i}|{ct}",
                "arm": "volctrl",
                "tf": e["tf"],
                "mode": e["mode"],
                "signal_ts": ct,
                "signal_ist": s018.ist_str(ct),
                "hod": ist_hod(ct),
                "ym": ist_ym(ct),
                "src_event": e["event_id"],
            }
        )
    return out


def _strike_cfgs() -> list[tuple[str, int]]:
    return [("itm", int(x)) for x in X_ITM] + [("otm", 1000)]


def _median(xs: list[float]) -> float:
    a = sorted(float(x) for x in xs)
    if not a:
        return float("nan")
    m = len(a) // 2
    if len(a) % 2:
        return a[m]
    return 0.5 * (a[m - 1] + a[m])


def _pctile(xs: list[float], p: float) -> float:
    a = sorted(float(x) for x in xs)
    if not a:
        return float("nan")
    if len(a) == 1:
        return a[0]
    k = (len(a) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(a) - 1)
    w = k - lo
    return a[lo] * (1.0 - w) + a[hi] * w


def _seq_maxdd(nets: list[float]) -> float:
    eq = 0.0
    pk = 0.0
    dd = 0.0
    for x in nets:
        eq += float(x)
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dd


def _db_expiries(store: Any, d0: Any, d1: Any) -> list[str]:
    inner = getattr(store, "inner", store)
    seen: set[str] = set()
    d = d0
    while d <= d1:
        conn = None
        try:
            conn = inner.conn(d)
        except Exception:
            conn = None
        if conn is not None:
            try:
                for (e,) in conn.execute("SELECT DISTINCT expiry FROM marks").fetchall():
                    seen.add(str(e)[:10])
            except Exception:
                pass
        d = d + timedelta(days=1)
    return sorted(seen)


def _cell_stats(taken: list[dict[str, Any]], n_skipped: int, reasons: dict[str, int]) -> dict[str, Any]:
    nets = [float(r["net"]) for r in taken]
    n = len(nets)
    by_r: dict[str, int] = {}
    for r in taken:
        by_r[str(r["reason"])] = by_r.get(str(r["reason"]), 0) + 1
    return {
        "n": n,
        "n_skipped_events": int(n_skipped),
        "skip_reasons": dict(reasons),
        "mean": (sum(nets) / n) if n else float("nan"),
        "median": _median(nets),
        "worst": min(nets) if nets else float("nan"),
        "p5": _pctile(nets, 5.0),
        "win_pct": (100.0 * sum(1 for x in nets if x > 0) / n) if n else float("nan"),
        "sum": sum(nets) if nets else 0.0,
        "maxDD": _seq_maxdd(nets),
        "avg_adj": (sum(float(r["n_adj_at_exit"]) for r in taken) / n) if n else float("nan"),
        "avg_fee": (sum(float(r["fees_at_exit"]) for r in taken) / n) if n else float("nan"),
        "avg_slip": (sum(float(r["slip_at_exit"]) for r in taken) / n) if n else float("nan"),
        "n_TARGET": by_r.get("TARGET", 0),
        "n_SL": by_r.get("SL", 0),
        "n_EXPIRY": by_r.get("EXPIRY", 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--arms", default="all")
    ap.add_argument("--tfs", default="5m,15m,30m")
    ap.add_argument("--modes", default="wick,full")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--cache-gb", type=float, default=1.0)
    args = ap.parse_args()

    month = str(args.month)
    days = int(args.days or 0)
    if bool(args.smoke) and days <= 0:
        days = 3
    tfs = [x.strip() for x in str(args.tfs).split(",") if x.strip()] or list(TFS)
    modes = [x.strip() for x in str(args.modes).split(",") if x.strip()] or list(MODES)
    arms_s = str(args.arms).strip().lower()
    if arms_s in ("", "all"):
        arms = list(ARMS)
    else:
        arms = [x.strip() for x in arms_s.split(",") if x.strip()]

    csv_path = str(args.csv or s018.SPOT_CSV)
    args.csv = csv_path
    win_from, win_to = d.month_bounds(month)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    month_cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    cutoff = start_ts + days * 86400 if days else month_cutoff

    from backtest.strategies.s012_trend_follow.engine import ist_date as _ist_date

    win_to_use = _ist_date(cutoff - 1) if days else win_to

    reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = "smoke" if (days or args.smoke) else "grid"
    folder = RUNS_DIR / f"{stamp}_s022_{tag}_{month}"
    n = 0
    while folder.exists():
        n += 1
        folder = RUNS_DIR / f"{stamp}_s022_{tag}_{month}_{n}"
    archive = RunArchive(
        "s022",
        tag,
        args,
        month=month,
        prereg_note="S022 ULP rolling ITM long strangle; fixed 20pt slip; settle intrinsic 0 fee",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    t_load = time.perf_counter()
    spot = s018.load_spot_1m(csv_path)
    ts_all, o_all, h_all, l_all, c_all, vol_all = s020.bars_1m_vol(spot)
    lo = start_ts - 7 * 86400
    hi = cutoff + 4 * 86400
    sel = (ts_all >= lo) & (ts_all < hi)
    ts, o, h, l, c, vol = ts_all[sel], o_all[sel], h_all[sel], l_all[sel], c_all[sel], vol_all[sel]
    spot_c = {int(t): float(px) for t, px in zip(ts, c)}
    print(f"1m bars={len(ts)} data_load_s={time.perf_counter() - t_load:.2f}", flush=True)

    inner = s020.MarksStore if False else None
    from backtest.harness.data import MarksStore

    inner = MarksStore()
    store = d.MonthGuardStore(inner, win_from - timedelta(days=1), win_to_use + timedelta(days=14))
    s021._BOOK = s021.MarkBook(start_ts - 86400, cutoff + 4 * 86400)
    s021._CHAIN_S021 = {}

    hod_1m = ist_hod_arr(ts)
    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    pre = rolling_pre_range_60(gh, gl)
    bins, _ = assign_quantile_bins(pre)
    hod_g = (((grid.astype(np.int64) + 19800) // 3600) % 24).astype(np.int16)
    cell = np.full(int(grid.size), -1, dtype=np.int32)
    okb = bins >= 0
    cell[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)

    mkt = S021Market(store, s021._BOOK, spot_c, grid, gc)
    ulp_by = _ulp_events(ts, o, h, l, c, vol, tfs, modes, start_ts, cutoff)
    print("counting full-month ULP events (dry pass)...", flush=True)
    ulp_month = _ulp_events(ts_all, o_all, h_all, l_all, c_all, vol_all, tfs, modes, start_ts, month_cutoff)
    n_ulp_month = sum(len(v) for v in ulp_month.values())

    rng = np.random.default_rng(SEED)
    events_by_arm: dict[str, list[dict[str, Any]]] = {a: [] for a in ARMS}
    n_ev_table: list[dict[str, Any]] = []
    for tf in tfs:
        for mode in modes:
            ulp = ulp_by.get((tf, mode), [])
            n_ev_table.append({"arm": "ulp", "tf": tf, "mode": mode, "n": len(ulp)})
            events_by_arm["ulp"].extend(ulp)
            sig_arr = np.array([int(e["signal_ts"]) for e in ulp], dtype=np.int64)
            near_g = forbidden_mask(grid, sig_arr, MIN_SEP)
            near_1 = forbidden_mask(ts, sig_arr, MIN_SEP)
            groups = grouped_cell_indices(cell, np.isfinite(pre) & (~near_g) & (cell >= 0))
            if "c2" in arms:
                c2 = _c2_events(ulp, ts, near_1, hod_1m, rng)
                n_ev_table.append({"arm": "c2", "tf": tf, "mode": mode, "n": len(c2)})
                events_by_arm["c2"].extend(c2)
            if "volctrl" in arms:
                vc = _volctrl_events(ulp, grid, cell, groups, rng)
                n_ev_table.append({"arm": "volctrl", "tf": tf, "mode": mode, "n": len(vc)})
                events_by_arm["volctrl"].extend(vc)

    cfgs = _strike_cfgs()
    n_combo = len(cfgs) * len(DTE_GRID) * len(A_GRID)
    work_events = [e for a in arms for e in events_by_arm[a]]
    print(
        f"events smoke={len(work_events)} ulp_month={n_ulp_month} "
        f"combos_per_event={n_combo} (X={len(cfgs)} dte={len(DTE_GRID)} a={len(A_GRID)})",
        flush=True,
    )

    from datetime import date as date_cls
    import csv
    from openpyxl import Workbook

    listed_cache: dict[str, list] = {}
    skip: dict[str, int] = {
        "missing_expiry": 0,
        "entry_mark_none": 0,
        "no_chain": 0,
        "no_strikes": 0,
        "path_mark_none": 0,
        "adj_entry_none": 0,
        "other": 0,
    }
    src_tot = {"real": 0, "stale": 0, "parity": 0, "none": 0}
    src_by_x: dict[tuple[str, int], dict[str, int]] = {}
    src_by_dte: dict[int, dict[str, int]] = {1: dict(src_tot), 2: dict(src_tot)}
    n_paths = 0
    n_ok = 0
    t_sim = time.perf_counter()
    n_work = max(1, len(work_events))
    exits_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    skip_by_key: dict[tuple[Any, ...], str] = {}
    dump_spec = ("ulp", "15m", "wick", "itm", 1000, 1, 125)
    miss_exp_ev: dict[int, int] = {1: 0, 2: 0}
    miss_exp_dates: dict[int, set[str]] = {1: set(), 2: set()}

    DUMP_T, DUMP_SL = 200, 200

    for ei, e in enumerate(work_events):
        t0 = int(e["signal_ts"])
        around = _ist_date(t0)
        ym = f"{around.year:04d}-{around.month:02d}"
        if ym not in listed_cache:
            listed_cache[ym] = s021.listed_expiries(store, around)
        exp_set = set(listed_cache[ym])
        for dte in DTE_GRID:
            ed = expiry_date(t0, int(dte))
            if ed not in exp_set:
                miss_exp_ev[int(dte)] += 1
                miss_exp_dates[int(dte)].add(ed.isoformat())
        for style, x in cfgs:
            for dte in DTE_GRID:
                ed = expiry_date(t0, int(dte))
                if ed not in exp_set:
                    skip["missing_expiry"] += len(A_GRID)
                    for a in A_GRID:
                        key = (e["event_id"], style, int(x), int(dte), int(a))
                        skip_by_key[key] = "missing_expiry"
                    continue
                exp_ts = int(s018.expiry_unix(ed))
                minutes = np.arange(t0, exp_ts + 1, 60, dtype=np.int64)
                for a in A_GRID:
                    p = simulate_path(
                        mkt, t0, ed, int(x), style, float(a), minutes,
                        compact=True, record_fills=False,
                    )
                    n_paths += 1
                    key = (e["event_id"], style, int(x), int(dte), int(a))
                    bx = (style, int(x))
                    src_by_x.setdefault(bx, {"real": 0, "stale": 0, "parity": 0, "none": 0})
                    for k, v in p.src_counts.items():
                        src_tot[k] = src_tot.get(k, 0) + int(v)
                        src_by_x[bx][k] = src_by_x[bx].get(k, 0) + int(v)
                        src_by_dte[int(dte)][k] = src_by_dte[int(dte)].get(k, 0) + int(v)
                    if not p.ok:
                        reason = p.skip_reason or "other"
                        skip[reason] = skip.get(reason, 0) + 1
                        skip_by_key[key] = reason
                    else:
                        n_ok += 1
                        exits_by_key[key] = list(p.exits)
        if (ei + 1) % 10 == 0 or ei + 1 == n_work:
            elapsed = time.perf_counter() - t_sim
            print(
                f"  events {ei + 1}/{n_work} paths={n_paths} ok={n_ok} "
                f"elapsed_s={elapsed:.1f}",
                flush=True,
            )

    sim_s = time.perf_counter() - t_sim
    per_path = (sim_s / n_paths) if n_paths else float("nan")
    n_month_events = n_ulp_month * len(arms)
    n_month_paths = n_month_events * n_combo
    proj_s = n_month_paths * per_path if n_paths else float("nan")

    t_occ = time.perf_counter()
    cell_rows: list[dict[str, Any]] = []
    n_taken = 0
    streams: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for e in work_events:
        for style, x in cfgs:
            for dte in DTE_GRID:
                for a in A_GRID:
                    rec = dict(e)
                    rec["path_key"] = (e["event_id"], style, int(x), int(dte), int(a))
                    sk = (e["arm"], e["tf"], e["mode"], style, int(x), int(dte), int(a))
                    streams.setdefault(sk, []).append(rec)
    dump_taken: list[dict[str, Any]] = []
    dump_cell_used = dump_spec
    for sk, evs in streams.items():
        arm, tf, mode, style, x, dte, a = sk
        for T in T_GRID:
            for SL in SL_GRID:
                taken, n_sk, reasons = take_in_order(evs, exits_by_key, skip_by_key, float(T), float(SL))
                n_taken += len(taken)
                st = _cell_stats(taken, n_sk, reasons)
                st.update(
                    {
                        "arm": arm, "tf": tf, "mode": mode, "style": style,
                        "X": int(x), "dte": int(dte), "a": int(a), "T": int(T), "SL": int(SL),
                    }
                )
                cell_rows.append(st)
                if (
                    arm, tf, mode, style, int(x), int(dte), int(a), int(T), int(SL)
                ) == (*dump_spec, DUMP_T, DUMP_SL):
                    dump_taken = taken
                    dump_cell_used = dump_spec
    occ_s = time.perf_counter() - t_occ

    dump_requested = True
    if len(dump_taken) < 5:
        dump_requested = False
        near = [
            r for r in cell_rows
            if r["arm"] == "ulp" and r["tf"] == "15m" and r["mode"] == "wick"
            and r["style"] == "itm" and int(r["n"]) > 0
        ]
        near.sort(key=lambda r: (
            abs(int(r["X"]) - 1000), abs(int(r["dte"]) - 1),
            abs(int(r["a"]) - 125), abs(int(r["T"]) - 200), abs(int(r["SL"]) - 200),
        ))
        if near:
            pick = near[0]
            dump_cell_used = (
                "ulp", "15m", "wick", "itm", int(pick["X"]), int(pick["dte"]), int(pick["a"]),
            )
            sk = ("ulp", "15m", "wick", "itm", int(pick["X"]), int(pick["dte"]), int(pick["a"]))
            evs = streams.get(sk, [])
            dump_taken, _, _ = take_in_order(
                evs, exits_by_key, skip_by_key, float(pick["T"]), float(pick["SL"]),
            )
            DUMP_T, DUMP_SL = int(pick["T"]), int(pick["SL"])

    cell_path = archive.folder / "cells.csv"
    cell_cols = [
        "arm", "tf", "mode", "style", "X", "dte", "a", "T", "SL",
        "n", "n_skipped_events", "mean", "median", "worst", "p5", "win_pct",
        "sum", "maxDD", "avg_adj", "avg_fee", "avg_slip",
        "n_TARGET", "n_SL", "n_EXPIRY",
    ]
    with cell_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cell_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(cell_rows)

    db_exps = _db_expiries(store, date_cls(2025, 5, 30), date_cls(2025, 6, 6))
    n_ulp_smoke = sum(1 for e in work_events if e.get("arm") == "ulp")
    miss_txt = []
    for dte in DTE_GRID:
        miss_txt.append(
            f"  dte={dte}: events_missing_expiry={miss_exp_ev[int(dte)]}/{len(work_events)} "
            f"dates={sorted(miss_exp_dates[int(dte)])}"
        )
    wanted_2dte = sorted(miss_exp_dates[2])
    genuinely_absent_2dte = [x for x in wanted_2dte if x not in db_exps]

    src_n = sum(src_tot.values()) or 1

    def _src_line(title: str, dsrc: dict[str, int]) -> str:
        n = sum(dsrc.values()) or 1
        return (
            f"  {title}: real={dsrc.get('real', 0)} ({100.0 * dsrc.get('real', 0) / n:.2f}%) "
            f"stale={dsrc.get('stale', 0)} ({100.0 * dsrc.get('stale', 0) / n:.2f}%) "
            f"parity={dsrc.get('parity', 0)} ({100.0 * dsrc.get('parity', 0) / n:.2f}%) "
            f"none={dsrc.get('none', 0)} ({100.0 * dsrc.get('none', 0) / n:.2f}%)"
        )

    ulp15 = [
        r for r in cell_rows
        if r["arm"] == "ulp" and r["tf"] == "15m" and r["n"] > 0
    ]
    ulp15_best = sorted(ulp15, key=lambda r: float(r["mean"]) if r["mean"] == r["mean"] else -1e18, reverse=True)[:5]
    ulp15_worst = sorted(ulp15, key=lambda r: float(r["mean"]) if r["mean"] == r["mean"] else 1e18)[:5]

    dump_paths: dict[tuple[Any, ...], Any] = {}
    dump_baskets = dump_taken[:5]
    for rec in dump_baskets:
        ev = rec["event"]
        key = ev["path_key"]
        t0 = int(ev["signal_ts"])
        _eid, style, x, dte, a = key
        ed = expiry_date(t0, int(dte))
        exp_ts = int(s018.expiry_unix(ed))
        minutes = np.arange(t0, exp_ts + 1, 60, dtype=np.int64)
        dump_paths[key] = simulate_path(
            mkt, t0, ed, int(x), str(style), float(a), minutes,
            compact=False, record_fills=True,
        )

    basket_rows: list[dict[str, Any]] = []
    basket_text: list[str] = []
    for bi, rec in enumerate(dump_baskets, start=1):
        ev = rec["event"]
        key = ev["path_key"]
        p = dump_paths.get(key)
        basket_text.append(f"=== basket #{bi} event={ev.get('event_id')} signal_ts={ev.get('signal_ts')} ===")
        if p is None:
            basket_text.append("  (path series not retained)")
            continue
        slv = float(DUMP_SL)
        for fr in p.fills:
            t = int(fr["ts"])
            idx = 0
            if p.ts:
                diffs = [abs(int(x) - t) for x in p.ts]
                idx = int(np.argmin(np.array(diffs)))
            net = float(p.net[idx]) if p.net else float("nan")
            pk = float(p.peak[idx]) if p.peak else float("nan")
            fr2 = dict(fr)
            fr2["basket"] = bi
            fr2["net"] = net
            fr2["peak"] = pk
            fr2["sl_level"] = sl_level(pk, slv) if pk == pk else float("nan")
            fr2["exit_reason"] = rec.get("reason", "")
            basket_rows.append(fr2)
            basket_text.append(
                f"  {fr2['ist']} spot={fr2['spot']:.1f} K={fr2['strike']:.0f} "
                f"{'C' if fr2['is_call'] else 'P'} {fr2['side']} mark={fr2['mark']:.4f} "
                f"src={fr2['mark_src']} fill={fr2['fill']:.4f} fee={fr2['fee']:.4f} "
                f"slip={fr2['slip']:.4f} ref={fr2['ref']:.4f} trig={fr2['trigger']} "
                f"net={net:.4f} peak={pk:.4f} SLlvl={fr2['sl_level']:.4f} {fr2['exit_reason']}"
            )
        basket_text.append(
            f"  EXIT {rec.get('reason')} ts={rec.get('exit_ts')} net={rec.get('net')} "
            f"n_adj={rec.get('n_adj_at_exit')}"
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "baskets"
    bcols = [
        "basket", "ist", "spot", "strike", "is_call", "side", "mark", "mark_src",
        "fill", "fee", "slip", "ref", "trigger", "net", "peak", "sl_level", "exit_reason",
        "symbol", "ts",
    ]
    ws.append(bcols)
    for r in basket_rows:
        ws.append([r.get(c, "") for c in bcols])
    xlsx_path = archive.folder / "smoke_baskets.xlsx"
    wb.save(xlsx_path)
    (archive.folder / "basket1.txt").write_text(
        ("\n".join(basket_text[: max(2, next((i for i, l in enumerate(basket_text) if l.startswith('=== basket #2')), len(basket_text)))]) + "\n"),
        encoding="utf-8",
    )

    dump_note = (
        f"dump cell={dump_cell_used} T={DUMP_T} SL={DUMP_SL} n_taken={len(dump_taken)} "
        f"dumped={len(dump_baskets)}"
        + (
            ""
            if dump_requested and dump_cell_used == dump_spec
            else " (nearest cell; requested ULP 15m wick X=1000 ITM dte=1 a=125 T=200 SL=200)"
        )
    )

    lines = [
        "S022 smoke — ULP rolling ITM long strangle (no full-month run)",
        f"folder={archive.folder}",
        f"month={month} days={days} arms={arms} tfs={tfs} modes={modes}",
        f"grid X_ITM={list(X_ITM)} OTM1000 a={list(A_GRID)} T={list(T_GRID)} SL={list(SL_GRID)} dte={list(DTE_GRID)}",
        f"slip_pts={20} qty={1000} seed={SEED}",
        "",
        "n events per arm/tf/mode:",
    ]
    for row in n_ev_table:
        lines.append(f"  {row['arm']:7s} {row['tf']:4s} {row['mode']:4s} n={row['n']}")
    lines += [
        "",
        f"n_paths_simulated={n_paths} n_ok={n_ok}",
        f"wall_s={sim_s:.2f} s_per_path={per_path:.6f} occupancy_s={occ_s:.2f} n_taken_all_cells={n_taken}",
        f"n_ulp_month={n_ulp_month} n_month_events_all_arms={n_month_events} n_month_paths_proj={n_month_paths}",
        (
            f"projected_full_month_runtime_s={proj_s:.1f} ({proj_s / 3600.0:.2f} h)"
            if proj_s == proj_s else "projected_full_month_runtime_s=nan"
        ),
        f"skip={skip}",
        _src_line("mark_src ALL", src_tot),
    ]
    for dte in DTE_GRID:
        lines.append(_src_line(f"mark_src dte={dte}", src_by_dte[int(dte)]))
    for (style, x), dsrc in sorted(src_by_x.items()):
        lines.append(_src_line(f"mark_src {style} X={x}", dsrc))
    lines += [
        f"book_symbols={len(s021._BOOK.arr) if s021._BOOK else 0} "
        f"book_hits={s021._BOOK.hits if s021._BOOK else 0} book_miss={s021._BOOK.misses if s021._BOOK else 0}",
        "",
        "--- missing_expiry diagnosis ---",
        f"listed_expiries union from s021 cache months: {sorted({str(x) for xs in listed_cache.values() for x in xs})}",
        f"marks DB DISTINCT expiry 2025-05-30..2025-06-06: {db_exps}",
        *miss_txt,
        (
            f"2DTE expiries are genuinely absent from the marks DB: {genuinely_absent_2dte}"
            if genuinely_absent_2dte
            else (
                "2DTE wanted dates are present in the marks DB (skip is listed_expiries/cache, not missing files)."
                if wanted_2dte
                else "no 2DTE missing_expiry events in this smoke window."
            )
        ),
        "",
        dump_note,
        "",
        "--- 5 best cells arm=ulp tf=15m by mean (3-day PREVIEW only) ---",
    ]
    for r in ulp15_best:
        lines.append(
            f"  {r['mode']} {r['style']} X={r['X']} dte={r['dte']} a={r['a']} T={r['T']} SL={r['SL']} "
            f"n={r['n']} mean={r['mean']:.3f} win%={r['win_pct']:.1f} maxDD={r['maxDD']:.1f}"
        )
    lines.append("--- 5 worst cells arm=ulp tf=15m by mean (3-day PREVIEW only) ---")
    for r in ulp15_worst:
        lines.append(
            f"  {r['mode']} {r['style']} X={r['X']} dte={r['dte']} a={r['a']} T={r['T']} SL={r['SL']} "
            f"n={r['n']} mean={r['mean']:.3f} win%={r['win_pct']:.1f} maxDD={r['maxDD']:.1f}"
        )
    lines.append("")
    text = "\n".join(lines)
    (archive.folder / "summary.txt").write_text(text + "\n", encoding="utf-8")
    (archive.folder / "smoke_meta.json").write_text(
        json.dumps(
            {
                "n_paths": n_paths,
                "n_ok": n_ok,
                "sim_s": sim_s,
                "s_per_path": per_path,
                "occ_s": occ_s,
                "n_taken": n_taken,
                "n_ulp_month": n_ulp_month,
                "n_month_paths_proj": n_month_paths,
                "projected_full_month_runtime_s": proj_s,
                "skip": skip,
                "src": src_tot,
                "n_events": n_ev_table,
                "db_expiries": db_exps,
                "dump_cell": list(dump_cell_used),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(text, flush=True)
    print("\n".join(basket_text[:80]), flush=True)
    print(f"wrote {cell_path}", flush=True)
    print(f"wrote {xlsx_path}", flush=True)
    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
