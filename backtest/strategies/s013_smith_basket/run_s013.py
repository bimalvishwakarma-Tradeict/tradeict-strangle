#!/usr/bin/env python3
"""S013 Smith + S012 basket.

python backtest\\strategies\\s013_smith_basket\\run_s013.py `
    --csv backtest\\data_1m\\BTCUSD_1m_20240630_20260921.csv `
    --out backtest\\strategies\\s013_smith_basket\\runs `
    --max-days 10 --rsi 14
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
from backtest.strategies.s007b_vwap_directional.indicators import (  # noqa: E402
    rsi_wilder,
    smith_vwap,
)
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    build_tf,
    ist_date,
    load_spot_1m,
    supertrend,
)
from backtest.strategies.s012_trend_follow.run_s012 import (  # noqa: E402
    day_cluster_bootstrap,
    max_dd,
)
from backtest.strategies.s013_smith_basket import config as cfg  # noqa: E402
from backtest.strategies.s013_smith_basket.engine import (  # noqa: E402
    Trade,
    candle_volumes,
    detect_smith,
    lookahead_ok,
    pick_random,
    random_pool,
    take_one_at_a_time,
)

logger = logging.getLogger("s013")


def _mean(xs: list[float]) -> float:
    return float(fmean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(median(xs)) if xs else float("nan")


def summarize(trades: list[Trade], half: str) -> dict[str, Any]:
    rows = [t for t in trades if t.half == half and not t.skip]
    miss = sum(1 for t in trades if t.half == half and t.mark_missing)
    n = len(rows)
    nets = [t.net for t in rows]
    _, ci_lo, ci_hi = day_cluster_bootstrap(rows, cfg.BOOTSTRAP_N, cfg.BOOTSTRAP_SEED)  # type: ignore[arg-type]
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
        "pct_stop": (100.0 * sum(t.stop_hit for t in rows) / n) if n else float("nan"),
        "pct_expiry": (100.0 * sum(t.expiry_exit for t in rows) / n) if n else float("nan"),
        "pct_0dte": (
            100.0 * sum(1 for t in rows if t.dte == "0DTE") / n if n else float("nan")
        ),
        "mean_w": _mean([t.w for t in rows]),
        "same_bar": sum(t.same_bar for t in rows),
        "pct_runner_wait": (
            100.0 * sum(t.runner_wait for t in rows) / n if n else float("nan")
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
        f"tgt%={f(s['pct_target'], 1)} stop%={f(s['pct_stop'], 1)} "
        f"exp%={f(s['pct_expiry'], 1)} 0DTE%={f(s['pct_0dte'], 1)} "
        f"meanW={f(s['mean_w'], 2)} same_bar={s['same_bar']} "
        f"runner_wait%={f(s['pct_runner_wait'], 1)} mark_missing={s['mark_missing']} "
        f"CI95=[{f(s['ci_lo'])}, {f(s['ci_hi'])}]"
    )


def write_trades_csv(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(asdict(trades[0]).keys()) if trades else []
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        for t in trades:
            row = asdict(t)
            row["day"] = t.day.isoformat() if hasattr(t.day, "isoformat") else str(t.day)
            w.writerow(row)


def render_report(
    stamp: str, cells: list[dict[str, Any]], la_fail: int, elapsed: float
) -> str:
    lines = [
        "S013 Smith signal + S012 basket",
        f"stamp={stamp}",
        f"DATA_START={cfg.DATA_START.isoformat()}",
        f"H1={cfg.H1_FROM}..{cfg.H1_TO}  H2={cfg.H2_FROM}..{cfg.H2_TO}",
        f"lookahead_fail={la_fail}  elapsed_sec={elapsed:.1f}",
        "",
    ]
    best_h1: dict[str, Any] | None = None
    best_mean = float("-inf")
    for cell in cells:
        key = f"RSI={cell['rsi']} N={cell['n_ratio']}"
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
        mn, lo, rm = h2["mean_net"], h2["ci_lo"], rnd["mean_net"]
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
        cell_s = f"RSI={best_h1['rsi']} N={best_h1['n_ratio']}"
        lines.append(f"best_H1_cell={cell_s} H1_mean={best_h1['sig_h1']['mean_net']:.6f}")
        lines.append(f"H2_mean={mn} H2_CI_lo={lo} H2_random_mean={rm}")
        lines.append("PASS" if ok else "FAIL")
    lines.append("")
    return "\n".join(lines)


def load_ckpt(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"done": []}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    p = argparse.ArgumentParser(description="S013 Smith + S012 basket")
    p.add_argument("--csv", default=cfg.DEFAULT_CSV)
    p.add_argument("--out", default="backtest/strategies/s013_smith_basket/runs")
    p.add_argument("--max-days", type=int, default=0)
    p.add_argument("--rsi", type=int, default=0, help="single RSI period, 0=all")
    p.add_argument("--fresh", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_slip_table()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / cfg.CKPT_NAME
    if args.fresh and ckpt_path.exists():
        ckpt_path.unlink()

    t0 = time.perf_counter()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if ckpt_path.exists() and not args.fresh:
        ckpt = load_ckpt(ckpt_path)
        stamp = str(ckpt.get("stamp", stamp))
    else:
        ckpt = {"stamp": stamp, "done": [], "cells": [], "la_fail": 0}

    txt_path = out_dir / f"s013_{stamp}.txt"
    csv_path = out_dir / f"s013_{stamp}_trades.csv"

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

    logger.info("building %sm candles", cfg.TF_MIN)
    candles = build_tf(spot, cfg.TF_MIN)
    vol = candle_volumes(spot, candles, cfg.TF_MIN)
    close = np.array([c.close for c in candles], dtype=np.float64)
    high = np.array([c.high for c in candles], dtype=np.float64)
    low = np.array([c.low for c in candles], dtype=np.float64)
    lower, upper = smith_vwap(close, vol, cfg.SMITH_LEN, cfg.SMITH_K)
    trend, _st = supertrend(high, low, close, cfg.ST_LEN, cfg.ST_MULT)
    logger.info("supertrend ST=%s (imported S012)", cfg.ST_MULT)

    rsis = (int(args.rsi),) if args.rsi else cfg.RSI_PERIODS
    store = MarksStore()
    cells: list[dict[str, Any]] = list(ckpt.get("cells", []))
    done = set(int(x) for x in ckpt.get("done", []))
    la_fail_tot = int(ckpt.get("la_fail", 0))
    n_signals_last = 0
    mean_w_last = float("nan")

    try:
        for rp in rsis:
            if int(rp) in done:
                logger.info("skip checkpoint RSI=%s", rp)
                continue
            rsi = rsi_wilder(close, int(rp))
            raw = detect_smith(
                candles, lower, upper, rsi, spot, start_ts, cutoff_ts
            )
            sigs = []
            la_once = 0
            for s in raw:
                if lookahead_ok(candles, lower, upper, rsi, spot, start_ts, s):
                    sigs.append(s)
                else:
                    la_once += 1
            n_signals_last = len(sigs)
            mean_w_last = _mean([s.w for s in sigs])
            pool = random_pool(
                candles, lower, upper, sigs, spot, start_ts, cutoff_ts
            )
            rng = np.random.default_rng(cfg.RANDOM_SEED)
            rnd_sigs = pick_random(
                candles, lower, upper, sigs, pool, spot, rng
            )
            logger.info("RSI=%s signals=%s meanW=%.2f", rp, len(sigs), mean_w_last)
            for n_ratio in cfg.N_RATIOS:
                sig_tr = take_one_at_a_time(
                    sigs,
                    n_ratio=float(n_ratio),
                    rsi_period=int(rp),
                    candles=candles,
                    trend=trend,
                    spot=spot,
                    store=store,
                    kind="signal",
                )
                rnd_tr = [
                    take_one_at_a_time(
                        [rs],
                        n_ratio=float(n_ratio),
                        rsi_period=int(rp),
                        candles=candles,
                        trend=trend,
                        spot=spot,
                        store=store,
                        kind="random",
                    )[0]
                    for rs in rnd_sigs
                ]
                write_trades_csv(csv_path, sig_tr + rnd_tr)
                cell = {
                    "rsi": int(rp),
                    "n_ratio": float(n_ratio),
                    "sig_h1": summarize(sig_tr, "H1"),
                    "sig_h2": summarize(sig_tr, "H2"),
                    "rnd_h1": summarize(rnd_tr, "H1"),
                    "rnd_h2": summarize(rnd_tr, "H2"),
                }
                cells.append(cell)
                logger.info(
                    "RSI=%s N=%s sig_n=%s rnd_n=%s",
                    rp, n_ratio, len(sig_tr), len(rnd_tr),
                )
            done.add(int(rp))
            la_fail_tot += la_once
            ckpt = {
                "stamp": stamp,
                "done": sorted(done),
                "cells": cells,
                "la_fail": la_fail_tot,
            }
            ckpt_path.write_text(json.dumps(ckpt, default=str), encoding="utf-8")
            elapsed = time.perf_counter() - t0
            txt_path.write_text(
                render_report(stamp, cells, la_fail_tot, elapsed), encoding="utf-8"
            )
    finally:
        store.close()

    elapsed = time.perf_counter() - t0
    report = render_report(stamp, cells, la_fail_tot, elapsed)
    txt_path.write_text(report, encoding="utf-8")
    print(report)
    print(
        f"SMOKE unique_signals={n_signals_last} mean_w={mean_w_last:.4f} "
        f"elapsed_sec={elapsed:.2f} txt={txt_path}"
    )
    n_days_smoke = args.max_days if args.max_days else 10
    n_rsi = max(len(rsis), 1)
    scale = (len(cfg.RSI_PERIODS) / n_rsi) * (630.0 / max(n_days_smoke, 1))
    print(f"EST_FULL_SEC={elapsed * scale:.1f} scale={scale:.3f}")


if __name__ == "__main__":
    main()
