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
    src_tot = {"real": 0, "ffill": 0, "none": 0}
    n_paths = 0
    n_ok = 0
    t_sim = time.perf_counter()
    n_work = max(1, len(work_events))

    for ei, e in enumerate(work_events):
        t0 = int(e["signal_ts"])
        around = _ist_date(t0)
        ym = f"{around.year:04d}-{around.month:02d}"
        if ym not in listed_cache:
            listed_cache[ym] = s021.listed_expiries(store, around)
        exp_set = set(listed_cache[ym])
        for style, x in cfgs:
            for dte in DTE_GRID:
                ed = expiry_date(t0, int(dte))
                if ed not in exp_set:
                    skip["missing_expiry"] += len(A_GRID)
                    continue
                exp_ts = int(s018.expiry_unix(ed))
                minutes = np.arange(t0, exp_ts + 1, 60, dtype=np.int64)
                for a in A_GRID:
                    p = simulate_path(mkt, t0, ed, int(x), style, float(a), minutes)
                    n_paths += 1
                    for k, v in p.src_counts.items():
                        src_tot[k] = src_tot.get(k, 0) + int(v)
                    if not p.ok:
                        reason = p.skip_reason or "other"
                        skip[reason] = skip.get(reason, 0) + 1
                    else:
                        n_ok += 1
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
    n_taken = 0
    occ_s = 0.0

    src_n = sum(src_tot.values()) or 1
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
        f"projected_full_month_runtime_s={proj_s:.1f} ({proj_s / 3600.0:.2f} h)" if proj_s == proj_s else "projected_full_month_runtime_s=nan",
        f"skip={skip}",
        f"mark_src real={src_tot.get('real', 0)} ({100.0 * src_tot.get('real', 0) / src_n:.2f}%) "
        f"ffill={src_tot.get('ffill', 0)} ({100.0 * src_tot.get('ffill', 0) / src_n:.2f}%) "
        f"none={src_tot.get('none', 0)} ({100.0 * src_tot.get('none', 0) / src_n:.2f}%)",
        f"book_symbols={len(s021._BOOK.arr) if s021._BOOK else 0} "
        f"book_hits={s021._BOOK.hits if s021._BOOK else 0} book_miss={s021._BOOK.misses if s021._BOOK else 0}",
        "",
    ]
    text = "\n".join(lines)
    (archive.folder / "summary.txt").write_text(text + "\n", encoding="utf-8")
    (archive.folder / "smoke_meta.json").write_text(
        json.dumps(
            {
                "n_paths": n_paths,
                "n_ok": n_ok,
                "sim_s": sim_s,
                "s_per_path": per_path,
                "n_ulp_month": n_ulp_month,
                "n_month_paths_proj": n_month_paths,
                "projected_full_month_runtime_s": proj_s,
                "skip": skip,
                "src": src_tot,
                "n_events": n_ev_table,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(text, flush=True)
    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
