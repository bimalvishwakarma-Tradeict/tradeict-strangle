#!/usr/bin/env python3
"""S023 OBPS stage1: F / C2 / VOLCTRL occupancy walk. IS months only via CLI."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.harness.run_archive import RunArchive  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s021_hedged_long_straddle import run_s021 as s021  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.run_s022 import (  # noqa: E402
    S021Market,
    _c2_events,
    _volctrl_events,
)
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    SEED,
    assign_quantile_bins,
    ffill_1m_grid,
    forbidden_mask,
    ist_hod,
    ist_hod_arr,
    rolling_pre_range_60,
)
from backtest.strategies.s023_obps.signals import ARMS, MODES, TFS, iter_obps_signals  # noqa: E402
from backtest.strategies.s023_obps.sim import BasketResult, DEFAULT_QTY, simulate_basket  # noqa: E402

PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
CTRL_ARMS = ("F", "C2", "VOLCTRL")


def collect_listed(store: Any, d0: date, d1: date) -> list[date]:
    seen: set[date] = set()
    cur = date(d0.year, d0.month, 1)
    end = date(d1.year, d1.month, 1)
    while cur <= end:
        conn = store.conn(cur)
        if conn is not None:
            try:
                for (e,) in conn.execute("SELECT DISTINCT expiry FROM marks").fetchall():
                    seen.add(date.fromisoformat(str(e)[:10]))
            except Exception:
                pass
        y, m = cur.year, cur.month + 1
        cur = date(y + 1, 1, 1) if m == 13 else date(y, m, 1)
    return sorted(seen)


def _bars(tts: Any, to_: Any, th: Any, tl: Any, tc: Any) -> list[dict[str, Any]]:
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


def _window(month: str) -> tuple[int, int, date, date]:
    a, b = d.month_bounds(month)
    return to_unix(ist_dt(a, 0, 0)), to_unix(ist_dt(b + timedelta(days=1), 0, 0)), a, b


def _maxdd(nets: list[float]) -> float:
    eq = pk = dd = 0.0
    for x in nets:
        eq += float(x)
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dd


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(float(v) for v in xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = min(lo + 1, len(s) - 1)
    w = k - lo
    return s[lo] * (1.0 - w) + s[hi] * w


def _median(xs: list[float]) -> float:
    return _pct(xs, 50.0)


def occupancy_walk(
    events: list[dict[str, Any]],
    mkt: S021Market,
    listed: list[date],
    ohlc: dict[int, tuple[float, float, float]],
    qty: int,
) -> tuple[list[BasketResult], dict[str, int], list[dict[str, Any]]]:
    ordered = sorted(events, key=lambda e: int(e["signal_ts"]))
    taken: list[BasketResult] = []
    rows: list[dict[str, Any]] = []
    skip: dict[str, int] = defaultdict(int)
    busy_until = 0
    for e in ordered:
        entry = int(e["signal_ts"]) + 60
        if entry <= busy_until:
            skip["occupancy"] += 1
            continue
        sig_px = float(e.get("signal_price", e.get("close", e.get("entry", float("nan")))))
        res = simulate_basket(
            mkt, listed, entry_ts=entry, signal_price=sig_px, qty=qty, ohlc=ohlc,
        )
        if not res.ok:
            skip[res.skip_reason or "skip"] += 1
            continue
        taken.append(res)
        busy_until = int(res.exit_ts)
        rows.append(_trade_row(e, res))
    return taken, dict(skip), rows


def _trade_row(e: dict[str, Any], res: BasketResult) -> dict[str, Any]:
    legs_s = ";".join(
        f"{'C' if lg.is_call else 'P'}/{int(lg.strike)}/{lg.expiry.isoformat()}/{lg.qty}/{lg.fill_px:.4f}/{lg.role}"
        for lg in res.legs
    )
    s1 = res.step1 or {}
    s2 = res.step2 or {}
    return {
        "cell": f"{e.get('tf')}|{e.get('mode')}|{e.get('sig_arm')}",
        "arm": e.get("ctrl_arm"),
        "entry_ist": s018.ist_str(res.entry_ts),
        "signal_price": res.signal_price,
        "legs": legs_s,
        "step1": json.dumps(s1, default=str) if s1 else "",
        "step2": json.dumps(s2, default=str) if s2 else "",
        "redeploys": res.redeploys,
        "exit_ist": s018.ist_str(res.exit_ts),
        "exit_reason": res.exit_reason,
        "gross": res.gross,
        "fees": res.fees,
        "net": res.net,
        "slip": res.slip,
        "net_with_slip": res.net_with_slip,
        "max_gross": res.max_gross,
        "min_gross": res.min_gross,
        "hold_min": res.hold_min,
        "stale_minutes": res.stale_minutes,
    }


def _stats(taken: list[BasketResult], skip: dict[str, int]) -> dict[str, Any]:
    nets = [float(r.net) for r in taken]
    gross = [float(r.gross) for r in taken]
    n = len(nets)
    by_x: dict[str, int] = defaultdict(int)
    for r in taken:
        by_x[str(r.exit_reason)] += 1
    n_s1 = sum(1 for r in taken if r.step1)
    n_s2 = sum(1 for r in taken if r.step2)
    return {
        "n": n,
        "n_skipped": int(sum(skip.values())),
        "skip_reasons": dict(skip),
        "win_pct": (100.0 * sum(1 for x in nets if x > 0) / n) if n else float("nan"),
        "mean_gross": (sum(gross) / n) if n else float("nan"),
        "median_gross": _median(gross),
        "mean_net": (sum(nets) / n) if n else float("nan"),
        "median_net": _median(nets),
        "fees_mean": (sum(r.fees for r in taken) / n) if n else float("nan"),
        "slip_mean": (sum(r.slip for r in taken) / n) if n else float("nan"),
        "pct_TARGET": (100.0 * by_x.get("TARGET", 0) / n) if n else float("nan"),
        "pct_SL": (100.0 * by_x.get("SL", 0) / n) if n else float("nan"),
        "pct_SIGNAL_PRICE": (100.0 * by_x.get("SIGNAL_PRICE", 0) / n) if n else float("nan"),
        "pct_EXPIRY": (100.0 * by_x.get("EXPIRY", 0) / n) if n else float("nan"),
        "step1_hit_pct": (100.0 * n_s1 / n) if n else float("nan"),
        "step2_hit_pct": (100.0 * n_s2 / n) if n else float("nan"),
        "worst_net": min(nets) if nets else float("nan"),
        "best_net": max(nets) if nets else float("nan"),
        "total_net": sum(nets) if nets else 0.0,
        "maxDD": _maxdd(nets),
        "UNDERPOWERED": n < 20,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="2025-06")
    ap.add_argument("--tf", default="")
    ap.add_argument("--mode", default="")
    ap.add_argument("--arm", default="")
    ap.add_argument("--qty", type=int, default=DEFAULT_QTY)
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    args = ap.parse_args()
    months = [m.strip() for m in str(args.months).split(",") if m.strip()]
    tfs = [args.tf] if args.tf else list(TFS)
    modes = [args.mode] if args.mode else list(MODES)
    sig_arms = [args.arm.upper()] if args.arm else list(ARMS)
    qty = int(args.qty)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = RUNS_DIR / f"{stamp}_s023_stage1"
    n = 0
    while folder.exists():
        n += 1
        folder = RUNS_DIR / f"{stamp}_s023_stage1_{n}"
    archive = RunArchive(
        "s023",
        "stage1",
        args,
        month=",".join(months),
        prereg_note="S023 OBPS stage1; slip table M=3.2; fee cap $10/1000; IS months only",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    t_all0 = time.perf_counter()
    w0, _, d0, _ = _window(months[0])
    _, w1, _, d1 = _window(months[-1])
    spot = s018.load_spot_1m(args.csv)
    ts_all, o_all, h_all, l_all, c_all, vol_all = s020.bars_1m_vol(spot)
    lo = w0 - 7 * 86400
    hi = w1 + 4 * 86400
    sel = (ts_all >= lo) & (ts_all < hi)
    ts, o, h, l, c, vol = ts_all[sel], o_all[sel], h_all[sel], l_all[sel], c_all[sel], vol_all[sel]
    spot_c = {int(t): float(px) for t, px in zip(ts, c)}
    ohlc = {int(t): (float(hh), float(ll), float(cc)) for t, hh, ll, cc in zip(ts, h, l, c)}
    inner = MarksStore()
    store = d.MonthGuardStore(inner, d0 - timedelta(days=1), d1 + timedelta(days=14))
    s021._BOOK = s021.MarkBook(w0 - 86400, w1 + 4 * 86400)
    s021._CHAIN_S021 = {}
    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    pre = rolling_pre_range_60(gh, gl)
    bins, _ = assign_quantile_bins(pre)
    hod_g = (((grid.astype(np.int64) + 19800) // 3600) % 24).astype(np.int16)
    cell = np.full(int(grid.size), -1, dtype=np.int32)
    okb = bins >= 0
    cell[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)
    hod_1m = ist_hod_arr(ts)
    mkt = S021Market(store, s021._BOOK, spot_c, grid, gc)
    listed = collect_listed(store, d0 - timedelta(days=2), d1 + timedelta(days=16))
    print(f"listed_expiries={len(listed)} 1m={len(ts)}", flush=True)

    trade_cols = [
        "cell", "arm", "entry_ist", "signal_price", "legs", "step1", "step2",
        "redeploys", "exit_ist", "exit_reason", "gross", "fees", "net", "slip",
        "net_with_slip", "max_gross", "min_gross", "hold_min", "stale_minutes",
    ]
    trades_path = archive.folder / "trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=trade_cols).writeheader()

    summaries: list[dict[str, Any]] = []
    rng = np.random.default_rng(SEED)
    f_mean: dict[tuple[str, str, str], float] = {}

    for month in months:
        start_ts, cutoff, _a, _b = _window(month)
        pad_lo = start_ts - 7 * 86400
        pad_hi = cutoff + 86400
        msel = (ts >= pad_lo) & (ts < pad_hi)
        mts, mo, mh, ml, mc, mv = ts[msel], o[msel], h[msel], l[msel], c[msel], vol[msel]
        for tf in tfs:
            tf_sec = int(d.LINE_TF[tf])
            tts, to_, th, tl, tc, _tv = d.resample_tf(mts, mo, mh, ml, mc, mv, tf_sec)
            bars = _bars(tts, to_, th, tl, tc)
            for mode in modes:
                for sarm in sig_arms:
                    raw = iter_obps_signals(bars, tf, mode, sarm)
                    f_ev = []
                    for i, r in enumerate(raw):
                        st = int(r["signal_ts"])
                        if not (start_ts <= st < cutoff):
                            continue
                        f_ev.append(
                            {
                                **r,
                                "event_id": f"{tf}|{mode}|{sarm}|{i}|{st}",
                                "sig_arm": sarm,
                                "hod": ist_hod(st),
                                "ctrl_arm": "F",
                                "signal_price": float(r["signal_price"]),
                            }
                        )
                    sig_arr = np.array([int(e["signal_ts"]) for e in f_ev], dtype=np.int64)
                    near_g = forbidden_mask(grid, sig_arr, 3600)
                    near_1 = forbidden_mask(ts, sig_arr, 3600)
                    c2, _m = _c2_events(f_ev, ts, near_1, hod_1m, rng, start_ts, cutoff)
                    vol, _st = _volctrl_events(
                        f_ev, grid, cell, hod_g, rng, start_ts, cutoff, near_g,
                    )
                    for e in c2:
                        e["sig_arm"] = sarm
                        e["ctrl_arm"] = "C2"
                        e["tf"] = tf
                        e["mode"] = mode
                        e["signal_price"] = float(spot_c.get(int(e["signal_ts"]), float("nan")))
                    for e in vol:
                        e["sig_arm"] = sarm
                        e["ctrl_arm"] = "VOLCTRL"
                        e["tf"] = tf
                        e["mode"] = mode
                        e["signal_price"] = float(spot_c.get(int(e["signal_ts"]), float("nan")))
                    packs = {"F": f_ev, "C2": c2, "VOLCTRL": vol}
                    cell_stats: dict[str, dict[str, Any]] = {}
                    print(f"{month} {tf} {mode} {sarm} F={len(f_ev)} C2={len(c2)} VOL={len(vol)}", flush=True)
                    t_cell = time.perf_counter()
                    for name, evs in packs.items():
                        taken, skip, rows = occupancy_walk(evs, mkt, listed, ohlc, qty)
                        st = _stats(taken, skip)
                        st["month"] = month
                        st["tf"] = tf
                        st["mode"] = mode
                        st["sig_arm"] = sarm
                        st["ctrl_arm"] = name
                        cell_stats[name] = st
                        if name == "F":
                            f_mean[(month, tf, mode, sarm)] = float(st["mean_net"]) if st["n"] else float("nan")
                        with trades_path.open("a", newline="", encoding="utf-8") as f:
                            w = csv.DictWriter(f, fieldnames=trade_cols)
                            for row in rows:
                                w.writerow(row)
                    for name, st in cell_stats.items():
                        mn_f = cell_stats["F"]["mean_net"]
                        mn = st["mean_net"]
                        st["F_minus_C2"] = (
                            (mn_f - cell_stats["C2"]["mean_net"])
                            if name == "F" and cell_stats["C2"]["n"] and cell_stats["F"]["n"]
                            else float("nan")
                        )
                        st["F_minus_VOLCTRL"] = (
                            (mn_f - cell_stats["VOLCTRL"]["mean_net"])
                            if name == "F" and cell_stats["VOLCTRL"]["n"] and cell_stats["F"]["n"]
                            else float("nan")
                        )
                        summaries.append(st)
                    print(
                        f"  wall_s={time.perf_counter() - t_cell:.1f} "
                        f"F_n={cell_stats['F']['n']} mean_net={cell_stats['F']['mean_net']}",
                        flush=True,
                    )

    sum_cols = [
        "month", "tf", "mode", "sig_arm", "ctrl_arm", "n", "n_skipped", "skip_reasons",
        "win_pct", "mean_gross", "median_gross", "mean_net", "median_net", "fees_mean",
        "slip_mean", "pct_TARGET", "pct_SL", "pct_SIGNAL_PRICE", "pct_EXPIRY",
        "step1_hit_pct", "step2_hit_pct", "worst_net", "best_net", "total_net", "maxDD",
        "F_minus_C2", "F_minus_VOLCTRL", "UNDERPOWERED",
    ]
    sum_path = archive.folder / "summary.csv"
    with sum_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sum_cols, extrasaction="ignore")
        w.writeheader()
        for st in summaries:
            row = dict(st)
            row["skip_reasons"] = json.dumps(st.get("skip_reasons") or {})
            w.writerow(row)

    txt = archive.folder / "summary.txt"
    lines = [
        f"S023 stage1 months={months} qty={qty} wall_s={time.perf_counter() - t_all0:.1f}",
        f"folder={archive.folder}",
        "",
    ]
    for st in summaries:
        flag = " UNDERPOWERED" if st.get("UNDERPOWERED") else ""
        lines.append(
            f"{st['month']} {st['tf']} {st['mode']} {st['sig_arm']} {st['ctrl_arm']} "
            f"n={st['n']} skip={st['n_skipped']} win%={st['win_pct']} "
            f"mean_net={st['mean_net']} mean_gross={st['mean_gross']} "
            f"fees={st['fees_mean']} slip={st['slip_mean']} "
            f"exits T/SL/SP/E={st['pct_TARGET']}/{st['pct_SL']}/{st['pct_SIGNAL_PRICE']}/{st['pct_EXPIRY']} "
            f"s1={st['step1_hit_pct']} s2={st['step2_hit_pct']} "
            f"F-C2={st.get('F_minus_C2')} F-VOL={st.get('F_minus_VOLCTRL')}{flag}"
        )
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)

    archive.finalize()
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "summary"
    ws.append(sum_cols)
    for st in summaries:
        ws.append([json.dumps(st[c]) if c == "skip_reasons" else st.get(c, "") for c in sum_cols])
    wt = wb.create_sheet("trades")
    with trades_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            wt.append(row)
    xlsx = archive.folder / "summary.xlsx"
    wb.save(xlsx)
    print(f"done wall_s={time.perf_counter() - t_all0:.1f}", flush=True)


if __name__ == "__main__":
    main()
