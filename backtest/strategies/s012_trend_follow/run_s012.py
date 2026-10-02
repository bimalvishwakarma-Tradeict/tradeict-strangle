#!/usr/bin/env python3
"""S012 Trend Follow engine-lite.

python backtest\\strategies\\s012_trend_follow\\run_s012.py `
    --csv backtest\\data_1m\\BTCUSD_1m_20240630_20260921.csv `
    --out backtest\\strategies\\s012_trend_follow\\runs `
    --max-days 10 --tf 5 --st 3
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import fmean, median
from typing import Any

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.costs import ensure_slip_table  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.strategies.s012_trend_follow import config as cfg  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    Trade,
    build_tf,
    collect_signals,
    ist_date,
    load_spot_1m,
    random_pool,
    run_cell,
    supertrend,
)

logger = logging.getLogger("s012")


def _mean(xs: list[float]) -> float:
    return float(fmean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(median(xs)) if xs else float("nan")


def max_dd(nets: list[float]) -> float:
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for n in nets:
        eq += float(n)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def day_cluster_bootstrap(
    trades: list[Trade], n: int, seed: int
) -> tuple[float, float, float]:
    by_day: dict[date, list[float]] = defaultdict(list)
    for t in trades:
        if t.skip:
            continue
        by_day[t.day].append(float(t.net))
    days = list(by_day.keys())
    if not days:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(int(seed))
    means: list[float] = []
    nd = len(days)
    for _ in range(int(n)):
        samp = rng.choice(days, size=nd, replace=True)
        vals: list[float] = []
        for d in samp:
            vals.extend(by_day[d])
        means.append(float(np.mean(vals)))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(np.mean(means)), float(lo), float(hi)


def summarize(trades: list[Trade], half: str) -> dict[str, Any]:
    rows = [t for t in trades if t.half == half and not t.skip]
    miss = sum(1 for t in trades if t.half == half and t.mark_missing)
    n = len(rows)
    nets = [t.net for t in rows]
    _, ci_lo, ci_hi = day_cluster_bootstrap(rows, cfg.BOOTSTRAP_N, cfg.BOOTSTRAP_SEED)
    return {
        "half": half,
        "n": n,
        "win_pct": (100.0 * sum(t.win for t in rows) / n) if n else float("nan"),
        "mean_net": _mean(nets),
        "median_net": _med(nets),
        "worst_net": min(nets) if nets else float("nan"),
        "gross": sum(t.gross for t in rows),
        "fees": sum(t.fees for t in rows),
        "slippage": sum(t.slippage for t in rows),
        "max_dd": max_dd(nets),
        "mean_hold_min": _mean([t.hold_min for t in rows]),
        "pct_target": (100.0 * sum(t.target_hit for t in rows) / n) if n else float("nan"),
        "pct_0dte": (
            100.0 * sum(1 for t in rows if t.dte == "0DTE") / n if n else float("nan")
        ),
        "pct_1dte": (
            100.0 * sum(1 for t in rows if t.dte == "1DTE") / n if n else float("nan")
        ),
        "pct_weekend": (
            100.0 * sum(t.weekend for t in rows) / n if n else float("nan")
        ),
        "mark_missing": miss,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
    }


def fmt_row(label: str, s: dict[str, Any]) -> str:
    def f(x: Any, nd: int = 4) -> str:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return "nan"
        return f"{float(x):.{nd}f}"

    return (
        f"  {label} n={s['n']} win%={f(s['win_pct'], 1)} "
        f"mean={f(s['mean_net'])} med={f(s['median_net'])} worst={f(s['worst_net'])} "
        f"gross={f(s['gross'], 2)} fees={f(s['fees'], 2)} slip={f(s['slippage'], 2)} "
        f"maxDD={f(s['max_dd'])} hold_min={f(s['mean_hold_min'], 1)} "
        f"tgt%={f(s['pct_target'], 1)} 0DTE%={f(s['pct_0dte'], 1)} "
        f"1DTE%={f(s['pct_1dte'], 1)} wknd%={f(s['pct_weekend'], 1)} "
        f"mark_missing={s['mark_missing']} "
        f"CI95=[{f(s['ci_lo'])}, {f(s['ci_hi'])}]"
    )


def write_trades_csv(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(trades[0]).keys()) if trades else [
        "side", "tf", "st_mult", "tgt", "kind", "entry_ts", "exit_ts",
        "day", "half", "dte", "net", "gross", "fees", "slippage",
        "hold_min", "win", "target_hit", "weekend", "mark_missing", "skip",
    ]
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        for t in trades:
            row = asdict(t)
            row["day"] = t.day.isoformat()
            w.writerow(row)


def render_report(
    stamp: str,
    cells: list[dict[str, Any]],
    la_fail: int,
    elapsed: float,
) -> str:
    lines = [
        "S012 Trend Follow — engine-lite",
        f"stamp={stamp}",
        f"DATA_START={cfg.DATA_START.isoformat()}",
        f"H1={cfg.H1_FROM}..{cfg.H1_TO}  H2={cfg.H2_FROM}..{cfg.H2_TO}",
        f"lookahead_fail={la_fail}  elapsed_sec={elapsed:.1f}",
        "",
    ]
    best_h1: dict[str, Any] | None = None
    best_mean = float("-inf")
    for cell in cells:
        key = f"TF={cell['tf']} ST={cell['st_mult']} TGT={cell['tgt']}"
        lines.append(key)
        lines.append(fmt_row("signal H1", cell["sig_h1"]))
        lines.append(fmt_row("signal H2", cell["sig_h2"]))
        lines.append(fmt_row("random H1", cell["rnd_h1"]))
        lines.append(fmt_row("random H2", cell["rnd_h2"]))
        lines.append("")
        m = cell["sig_h1"]["mean_net"]
        n = cell["sig_h1"]["n"]
        if n > 0 and isinstance(m, float) and not math.isnan(m) and m > best_mean:
            best_mean = m
            best_h1 = cell

    lines.append("--- PRE-REGISTERED PASS/FAIL ---")
    if best_h1 is None:
        lines.append("FAIL: no H1 cell with trades")
        lines.append("best_cell=NONE")
    else:
        h2 = best_h1["sig_h2"]
        rnd = best_h1["rnd_h2"]
        mn = h2["mean_net"]
        lo = h2["ci_lo"]
        rm = rnd["mean_net"]
        ok = (
            isinstance(mn, float)
            and not math.isnan(mn)
            and mn > 0
            and isinstance(lo, float)
            and not math.isnan(lo)
            and lo > 0
            and isinstance(rm, float)
            and not math.isnan(rm)
            and mn > rm
        )
        cell_s = (
            f"TF={best_h1['tf']} ST={best_h1['st_mult']} TGT={best_h1['tgt']}"
        )
        lines.append(f"best_H1_cell={cell_s} H1_mean={best_h1['sig_h1']['mean_net']:.6f}")
        lines.append(
            f"H2_mean={mn} H2_CI_lo={lo} H2_random_mean={rm}"
        )
        lines.append("PASS" if ok else "FAIL")
    lines.append("")
    return "\n".join(lines)


def load_ckpt(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"done": []}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser(description="S012 Trend Follow")
    p.add_argument("--csv", default=cfg.DEFAULT_CSV)
    p.add_argument("--out", default="backtest/strategies/s012_trend_follow/runs")
    p.add_argument("--max-days", type=int, default=0)
    p.add_argument("--tf", type=int, default=0, help="single TF minutes, 0=all")
    p.add_argument("--st", type=float, default=0.0, help="single ST multiplier, 0=all")
    p.add_argument("--fresh", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    ensure_slip_table()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / cfg.CKPT_NAME
    if args.fresh and ckpt_path.exists():
        ckpt_path.unlink()
        for old in out_dir.glob("s012_trades_partial.csv"):
            old.unlink()

    t0 = time.perf_counter()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if ckpt_path.exists() and not args.fresh:
        ckpt = load_ckpt(ckpt_path)
        stamp = str(ckpt.get("stamp", stamp))
    else:
        ckpt = {"stamp": stamp, "done": [], "cells": [], "la_fail": 0}

    txt_path = out_dir / f"s012_{stamp}.txt"
    csv_path = out_dir / f"s012_{stamp}_trades.csv"

    logger.info("loading spot %s", args.csv)
    spot = load_spot_1m(args.csv)
    start_ts = to_unix(ist_dt(cfg.DATA_START, 0, 0))
    cutoff_ts: int | None = None
    if args.max_days and args.max_days > 0:
        days: list[date] = []
        seen: set[date] = set()
        for ts in sorted(spot):
            if ts < start_ts:
                continue
            d = ist_date(ts)
            if d < cfg.DATA_START:
                continue
            if d not in seen:
                seen.add(d)
                days.append(d)
            if len(days) >= args.max_days:
                cutoff_ts = to_unix(ist_dt(days[-1], 23, 59)) + 60
                break
        logger.info("max-days=%s cutoff_ts=%s", args.max_days, cutoff_ts)

    tfs = (args.tf,) if args.tf else cfg.TFS_MIN
    st_mults = (float(args.st),) if args.st else cfg.ST_MULTS
    store = MarksStore()
    cells: list[dict[str, Any]] = list(ckpt.get("cells", []))
    done = {tuple(x) for x in ckpt.get("done", [])}
    la_fail_tot = int(ckpt.get("la_fail", 0))
    n_signals_last = 0

    try:
        for tf in tfs:
            logger.info("building %sm candles", tf)
            candles = build_tf(spot, int(tf))
            h = np.array([c.high for c in candles], dtype=np.float64)
            l = np.array([c.low for c in candles], dtype=np.float64)
            c = np.array([c.close for c in candles], dtype=np.float64)
            for st_m in st_mults:
                key = (int(tf), float(st_m))
                if key in done:
                    logger.info("skip checkpoint TF=%s ST=%s", tf, st_m)
                    continue
                logger.info("supertrend TF=%s ST=%s", tf, st_m)
                trend, st_arr = supertrend(h, l, c, cfg.ST_LEN, float(st_m))
                sigs, lp_active, la_once = collect_signals(
                    candles, trend, st_arr, spot, start_ts, cutoff_ts
                )
                pool = random_pool(
                    candles,
                    trend,
                    st_arr,
                    spot,
                    start_ts,
                    cutoff_ts,
                    lp_active,
                    sigs,
                )
                logger.info("signals=%s random_pool=%s", len(sigs), len(pool))
                n_signals_last = len(sigs)
                for tgt in cfg.TGT_MULTS:
                    sig_tr, rnd_tr, la_fail = run_cell(
                        candles=candles,
                        trend=trend,
                        st=st_arr,
                        spot=spot,
                        store=store,
                        start_ts=start_ts,
                        cutoff_ts=cutoff_ts,
                        tf_min=int(tf),
                        st_mult=float(st_m),
                        tgt=float(tgt),
                        rng_seed=cfg.RANDOM_SEED,
                        sigs=sigs,
                        pool=pool,
                        la_fail=la_once if tgt == cfg.TGT_MULTS[0] else 0,
                    )
                    la_fail_tot += la_fail
                    all_tr = sig_tr + rnd_tr
                    write_trades_csv(csv_path, all_tr)
                    cell = {
                        "tf": int(tf),
                        "st_mult": float(st_m),
                        "tgt": float(tgt),
                        "sig_h1": summarize(sig_tr, "H1"),
                        "sig_h2": summarize(sig_tr, "H2"),
                        "rnd_h1": summarize(rnd_tr, "H1"),
                        "rnd_h2": summarize(rnd_tr, "H2"),
                    }
                    cells.append(cell)
                    logger.info(
                        "TF=%s ST=%s TGT=%s sig_n=%s rnd_n=%s",
                        tf,
                        st_m,
                        tgt,
                        len(sig_tr),
                        len(rnd_tr),
                    )
                done.add(key)
                ckpt = {
                    "stamp": stamp,
                    "done": [list(x) for x in done],
                    "cells": cells,
                    "la_fail": la_fail_tot,
                }
                ckpt_path.write_text(
                    json.dumps(ckpt, default=str), encoding="utf-8"
                )
                elapsed = time.perf_counter() - t0
                txt_path.write_text(
                    render_report(stamp, cells, la_fail_tot, elapsed),
                    encoding="utf-8",
                )
    finally:
        store.close()

    elapsed = time.perf_counter() - t0
    report = render_report(stamp, cells, la_fail_tot, elapsed)
    txt_path.write_text(report, encoding="utf-8")
    print(report)
    print(
        f"SMOKE unique_signals={n_signals_last} "
        f"elapsed_sec={elapsed:.2f} txt={txt_path}"
    )


if __name__ == "__main__":
    main()
