#!/usr/bin/env python3
"""S012G whipsaw arms on S012F.

python backtest\\strategies\\s012g_whipsaw\\run_s012g.py --self-test
python backtest\\strategies\\s012g_whipsaw\\run_s012g.py --max-days 10 --st 4 --fresh
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
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
from backtest.strategies.s012_trend_follow import config as s012cfg  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    build_tf,
    collect_signals,
    ist_date,
    load_spot_1m,
    random_pool,
    supertrend,
)
from backtest.strategies.s012_trend_follow.run_s012 import (  # noqa: E402
    day_cluster_bootstrap,
    max_dd,
)
from backtest.strategies.s012f_htf_filter import run_s012f as _s012f  # noqa: E402
from backtest.strategies.s012f_htf_filter.run_s012f import (  # noqa: E402
    period_of,
    pos_months,
    top5_share,
)
from backtest.strategies.s012g_whipsaw import config as cfg  # noqa: E402
from backtest.strategies.s012g_whipsaw.engine import (  # noqa: E402
    TradeG,
    adx_wilder,
    atr_wilder,
    filter_entry,
    last_completed_idx_fast,
    live_lp_series,
    simulate_g,
    take_sequential,
    test_new_filters_lookahead,
)

_s012f.last_completed_idx = last_completed_idx_fast

logger = logging.getLogger("s012g")


def _mean(xs: list[float]) -> float:
    return float(fmean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(median(xs)) if xs else float("nan")


def summarize(trades: list[TradeG], period: str) -> dict[str, Any]:
    rows = [t for t in trades if not t.skip and period_of(t.day) == period]
    n = len(rows)
    nets = [t.net for t in rows]
    nets05 = [t.net + 0.5 * t.slippage for t in rows]
    _, ci_lo, ci_hi = day_cluster_bootstrap(rows, cfg.BOOTSTRAP_N, cfg.BOOTSTRAP_SEED)  # type: ignore[arg-type]
    return {
        "n": n,
        "win_pct": (100.0 * sum(t.win for t in rows) / n) if n else float("nan"),
        "mean_net": _mean(nets),
        "median_net": _med(nets),
        "worst_net": min(nets) if nets else float("nan"),
        "max_dd": max_dd(nets),
        "pct_target": (100.0 * sum(t.target_hit for t in rows) / n) if n else float("nan"),
        "mean_hold": _mean([t.hold_min for t in rows]),
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "top5_share": top5_share(nets),
        "pos_months": pos_months(rows),  # type: ignore[arg-type]
        "mean_net_slip05": _mean(nets05),
    }


def fmt_row(label: str, s: dict[str, Any], rnd: float | None = None) -> str:
    def f(x: Any, nd: int = 4) -> str:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return "nan"
        return f"{float(x):.{nd}f}"

    extra = "" if rnd is None else f" rnd_mean={f(rnd)}"
    return (
        f"  {label} n={s['n']} win%={f(s['win_pct'], 1)} "
        f"mean={f(s['mean_net'])} med={f(s['median_net'])} worst={f(s['worst_net'])} "
        f"maxDD={f(s['max_dd'])} tgt%={f(s['pct_target'], 1)} hold={f(s['mean_hold'], 1)} "
        f"CI95=[{f(s['ci_lo'])}, {f(s['ci_hi'])}] top5={f(s['top5_share'], 3)} "
        f"pos_mo={s['pos_months']} slip05={f(s['mean_net_slip05'])}{extra}"
    )


def write_csv(path: Path, trades: list[TradeG]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not trades:
        return
    fields = list(asdict(trades[0]).keys())
    header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if header:
            w.writeheader()
        for t in trades:
            row = asdict(t)
            row["day"] = t.day.isoformat() if hasattr(t.day, "isoformat") else str(t.day)
            w.writerow(row)


def _find(
    cells: list[dict[str, Any]], arm: str, st: float
) -> dict[str, Any] | None:
    for c in cells:
        if c["arm"] == arm and abs(float(c["st"]) - st) < 1e-9:
            return c
    return None


def pass_arm(cells: list[dict[str, Any]], arm: str) -> str:
    lines = [f"--- PRE-REGISTERED {arm} ---"]
    core = _find(cells, arm, cfg.PASS_ST)
    a4 = _find(cells, "A", cfg.PASS_ST)
    ok = True
    if core is None or a4 is None:
        lines.append(f"FAIL: missing {arm} or A at ST={cfg.PASS_ST}")
        return "\n".join(lines) + "\nFAIL\n"
    for tag, skey, rkey in (
        ("P2025", "sig_p2025", "rnd_p2025"),
        ("P2026", "sig_p2026", "rnd_p2026"),
    ):
        m = core[skey]["mean_net"]
        rm = core[rkey]["mean_net"]
        am = a4[skey]["mean_net"]
        lines.append(
            f"  {arm} ST={cfg.PASS_ST} {tag} mean={m} rnd={rm} A={am} n={core[skey]['n']}"
        )
        if not isinstance(m, float) or math.isnan(m) or m <= 0:
            ok = False
        if not isinstance(rm, float) or math.isnan(rm) or m <= rm:
            ok = False
        if not isinstance(am, float) or math.isnan(am) or m <= am:
            ok = False
    for st in cfg.PASS_NEIGHBOR:
        c = _find(cells, arm, st)
        if c is None:
            lines.append(f"FAIL: missing {arm} ST={st}")
            ok = False
            continue
        for tag, skey in (("P2025", "sig_p2025"), ("P2026", "sig_p2026")):
            m = c[skey]["mean_net"]
            lines.append(f"  {arm} ST={st} {tag} mean={m} n={c[skey]['n']}")
            if not isinstance(m, float) or math.isnan(m) or m <= 0:
                ok = False
    lines.append("PASS" if ok else "FAIL")
    return "\n".join(lines) + "\n"


def render(
    stamp: str,
    cells: list[dict[str, Any]],
    elapsed: float,
    counts: dict[str, int],
) -> str:
    lines = [
        "S012G — whipsaw arms on S012F",
        f"stamp={stamp} TGT={cfg.TGT} TF={cfg.TF_MIN}",
        f"elapsed_sec={elapsed:.1f} signal_counts={counts}",
        "",
    ]
    for cell in cells:
        lines.append(f"ARM={cell['arm']} ST={cell['st']}")
        lines.append(fmt_row("signal P2025", cell["sig_p2025"], cell["rnd_p2025"]["mean_net"]))
        lines.append(fmt_row("signal P2026", cell["sig_p2026"], cell["rnd_p2026"]["mean_net"]))
        lines.append(fmt_row("random P2025", cell["rnd_p2025"]))
        lines.append(fmt_row("random P2026", cell["rnd_p2026"]))
        lines.append("")
    lines.append(pass_arm(cells, "F"))
    lines.append(pass_arm(cells, "G"))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="S012G whipsaw arms")
    p.add_argument("--csv", default=cfg.DEFAULT_CSV)
    p.add_argument("--out", default=cfg.DEFAULT_OUT)
    p.add_argument("--max-days", type=int, default=0)
    p.add_argument("--st", type=float, default=0.0)
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        test_new_filters_lookahead()
        return

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
        ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
        stamp = str(ckpt.get("stamp", stamp))
    else:
        ckpt = {"stamp": stamp, "done": [], "cells": [], "counts": {}}

    txt_path = out_dir / f"s012g_{stamp}.txt"
    csv_path = out_dir / f"s012g_{stamp}_trades.csv"

    logger.info("loading spot %s", args.csv)
    t_load0 = time.perf_counter()
    spot = load_spot_1m(args.csv)
    load_sec = time.perf_counter() - t_load0
    logger.info("csv_load_sec=%.2f", load_sec)
    start_ts = to_unix(ist_dt(s012cfg.DATA_START, 0, 0))
    cutoff_ts: int | None = None
    if args.max_days and args.max_days > 0:
        days: list[date] = []
        seen: set[date] = set()
        for ts in sorted(spot):
            if ts < start_ts:
                continue
            d = ist_date(ts)
            if d < s012cfg.DATA_START:
                continue
            if d not in seen:
                seen.add(d)
                days.append(d)
            if len(days) >= args.max_days:
                cutoff_ts = to_unix(ist_dt(days[-1], 23, 59)) + 60
                break

    logger.info("building 5m/1h/4h")
    c5 = build_tf(spot, cfg.TF_MIN)
    c1h = build_tf(spot, 60)
    c4h = build_tf(spot, 240)
    logger.info("HTF supertrend + ADX/ATR")
    t1h, _ = supertrend(
        np.array([x.high for x in c1h]),
        np.array([x.low for x in c1h]),
        np.array([x.close for x in c1h]),
        cfg.HTF_ST_LEN,
        cfg.HTF_ST_MULT,
    )
    t4h, _ = supertrend(
        np.array([x.high for x in c4h]),
        np.array([x.low for x in c4h]),
        np.array([x.close for x in c4h]),
        cfg.HTF_ST_LEN,
        cfg.HTF_ST_MULT,
    )
    h1 = np.array([x.high for x in c1h])
    l1 = np.array([x.low for x in c1h])
    cl1 = np.array([x.close for x in c1h])
    adx = adx_wilder(h1, l1, cl1, cfg.ADX_LEN)
    h5 = np.array([x.high for x in c5])
    l5 = np.array([x.low for x in c5])
    cl5 = np.array([x.close for x in c5])
    atr5 = atr_wilder(h5, l5, cl5, cfg.ATR_LEN)

    st_mults = (float(args.st),) if args.st else cfg.ST_MULTS
    store = MarksStore()
    cells: list[dict[str, Any]] = list(ckpt.get("cells", []))
    done = {tuple(x) for x in ckpt.get("done", [])}
    counts: dict[str, int] = dict(ckpt.get("counts", {}))

    try:
        for st_m in st_mults:
            trend, st_arr = supertrend(h5, l5, cl5, s012cfg.ST_LEN, float(st_m))
            sigs, lp_active, _ = collect_signals(
                c5, trend, st_arr, spot, start_ts, cutoff_ts
            )
            pool = random_pool(
                c5, trend, st_arr, spot, start_ts, cutoff_ts, lp_active, sigs
            )
            lp_cache = live_lp_series(c5, trend)
            for arm in cfg.ARMS:
                key = (arm, float(st_m))
                if key in done:
                    logger.info("skip %s ST=%s", arm, st_m)
                    continue
                tagged = []
                for s in sigs:
                    ok, info = filter_entry(
                        s, arm, c1h=c1h, t1h=t1h, c4h=c4h, t4h=t4h,
                        adx=adx, candles=c5, trend=trend, atr5=atr5,
                        lp_cache=lp_cache,
                    )
                    if ok:
                        tagged.append((s, info))
                pool_f = []
                for s in pool:
                    ok, info = filter_entry(
                        s, arm, c1h=c1h, t1h=t1h, c4h=c4h, t4h=t4h,
                        adx=adx, candles=c5, trend=trend, atr5=atr5,
                        lp_cache=lp_cache,
                    )
                    if ok:
                        pool_f.append((s, info))
                counts[arm] = len(tagged)
                logger.info("ARM=%s ST=%s signals=%s pool=%s", arm, st_m, len(tagged), len(pool_f))
                sig_tr = take_sequential(
                    tagged, arm=arm, st_mult=float(st_m),
                    candles=c5, trend=trend, spot=spot, store=store,
                )
                need = cfg.RANDOM_MULT * max(len([t for t in sig_tr if not t.skip]), 0)
                rng = np.random.default_rng(cfg.RANDOM_SEED)
                if pool_f and need > 0:
                    idx = rng.choice(len(pool_f), size=min(need, len(pool_f)), replace=False)
                    picks = [pool_f[int(i)] for i in np.atleast_1d(idx)]
                else:
                    picks = []
                rnd_tr = [
                    simulate_g(
                        sig=s, arm=arm, st_mult=float(st_m), candles=c5,
                        trend=trend, spot=spot, store=store, kind="random", filt=info,
                    )
                    for s, info in picks
                ]
                write_csv(csv_path, sig_tr + rnd_tr)
                cell = {
                    "arm": arm,
                    "st": float(st_m),
                    "sig_p2025": summarize(sig_tr, "P2025"),
                    "sig_p2026": summarize(sig_tr, "P2026"),
                    "rnd_p2025": summarize(rnd_tr, "P2025"),
                    "rnd_p2026": summarize(rnd_tr, "P2026"),
                }
                cells.append(cell)
                done.add(key)
                ckpt = {
                    "stamp": stamp,
                    "done": [list(x) for x in done],
                    "cells": cells,
                    "counts": counts,
                    "load_sec": load_sec,
                }
                ckpt_path.write_text(json.dumps(ckpt, default=str), encoding="utf-8")
                elapsed = time.perf_counter() - t0
                txt_path.write_text(
                    render(stamp, cells, elapsed, counts), encoding="utf-8"
                )
    finally:
        store.close()

    elapsed = time.perf_counter() - t0
    report = render(stamp, cells, elapsed, counts)
    txt_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"SMOKE signal_counts={counts} elapsed_sec={elapsed:.2f} load_sec={load_sec:.2f}")
    n_st = max(len(st_mults), 1)
    n_days = args.max_days if args.max_days else 10
    work = max(elapsed - load_sec, 0.0)
    scale = (len(cfg.ST_MULTS) / n_st) * (630.0 / max(n_days, 1))
    est = load_sec + work * scale
    print(f"EST_FULL_SEC={est:.1f} scale={scale:.3f} work_sec={work:.2f}")


if __name__ == "__main__":
    main()
