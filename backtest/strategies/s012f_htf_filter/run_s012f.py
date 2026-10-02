#!/usr/bin/env python3
"""S012F: S012 signals + 1h/4h Supertrend agreement filter.

python backtest\\strategies\\s012f_htf_filter\\run_s012f.py --self-test
python backtest\\strategies\\s012f_htf_filter\\run_s012f.py --max-days 10 --st 4 --fresh
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from collections import defaultdict
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
    Candle,
    Signal,
    Trade,
    build_tf,
    collect_signals,
    ist_date,
    load_spot_1m,
    random_pool,
    run_cell,
    supertrend,
)
from backtest.strategies.s012_trend_follow.run_s012 import (  # noqa: E402
    day_cluster_bootstrap,
    max_dd,
    write_trades_csv,
)
from backtest.strategies.s012f_htf_filter import config as cfg  # noqa: E402

logger = logging.getLogger("s012f")


def period_of(d: date) -> str:
    if cfg.P2025_FROM <= d <= cfg.P2025_TO:
        return "P2025"
    if cfg.P2026_FROM <= d <= cfg.P2026_TO:
        return "P2026"
    return "OUT"


def last_completed_idx(candles: list[Candle], entry_ts: int, tf_sec: int) -> int:
    """Last HTF bar whose UTC close is strictly before entry_ts."""
    ans = -1
    for i, c in enumerate(candles):
        done = int(c.ts_open) + int(tf_sec)
        if done < int(entry_ts):
            ans = i
        else:
            break
    return ans


def htf_dir(
    entry_ts: int, candles: list[Candle], trend: np.ndarray, tf_sec: int
) -> int:
    idx = last_completed_idx(candles, entry_ts, tf_sec)
    if idx < 0:
        return 0
    td = int(trend[idx])
    return td if td in (-1, 1) else 0


def htf_allows(
    sig: Signal,
    c1h: list[Candle],
    t1h: np.ndarray,
    c4h: list[Candle],
    t4h: np.ndarray,
) -> bool:
    d1 = htf_dir(sig.entry_ts, c1h, t1h, 3600)
    d4 = htf_dir(sig.entry_ts, c4h, t4h, 14400)
    if sig.side == "long":
        return d1 == 1 and d4 == 1
    return d1 == -1 and d4 == -1


def _mean(xs: list[float]) -> float:
    return float(fmean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(median(xs)) if xs else float("nan")


def top5_share(nets: list[float]) -> float:
    if not nets:
        return float("nan")
    tot = float(sum(nets))
    if tot == 0.0:
        return float("nan")
    top = float(sum(sorted(nets, reverse=True)[:5]))
    return top / tot


def pos_months(rows: list[Trade]) -> str:
    by_m: dict[tuple[int, int], float] = defaultdict(float)
    for t in rows:
        by_m[(t.day.year, t.day.month)] += float(t.net)
    if not by_m:
        return "0/0"
    pos = sum(1 for v in by_m.values() if v > 0)
    return f"{pos}/{len(by_m)}"


def summarize(trades: list[Trade], period: str) -> dict[str, Any]:
    rows = [
        t
        for t in trades
        if not t.skip and period_of(t.day) == period
    ]
    n = len(rows)
    nets = [t.net for t in rows]
    _, ci_lo, ci_hi = day_cluster_bootstrap(rows, s012cfg.BOOTSTRAP_N, s012cfg.BOOTSTRAP_SEED)
    return {
        "period": period,
        "n": n,
        "win_pct": (100.0 * sum(t.win for t in rows) / n) if n else float("nan"),
        "mean_net": _mean(nets),
        "median_net": _med(nets),
        "worst_net": min(nets) if nets else float("nan"),
        "gross": sum(t.gross for t in rows),
        "fees": sum(t.fees for t in rows),
        "slippage": sum(t.slippage for t in rows),
        "max_dd": max_dd(nets),
        "pct_target": (100.0 * sum(t.target_hit for t in rows) / n) if n else float("nan"),
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "top5_share": top5_share(nets),
        "pos_months": pos_months(rows),
    }


def fmt_row(label: str, s: dict[str, Any], rnd_mean: float | None = None) -> str:
    def f(x: Any, nd: int = 4) -> str:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return "nan"
        return f"{float(x):.{nd}f}"

    extra = "" if rnd_mean is None else f" rnd_mean={f(rnd_mean)}"
    return (
        f"  {label} n={s['n']} win%={f(s['win_pct'], 1)} "
        f"mean={f(s['mean_net'])} med={f(s['median_net'])} worst={f(s['worst_net'])} "
        f"gross={f(s['gross'], 2)} fees={f(s['fees'], 2)} slip={f(s['slippage'], 2)} "
        f"maxDD={f(s['max_dd'])} tgt%={f(s['pct_target'], 1)} "
        f"CI95=[{f(s['ci_lo'])}, {f(s['ci_hi'])}] top5={f(s['top5_share'], 3)} "
        f"pos_mo={s['pos_months']}{extra}"
    )


def _cell(cells: list[dict[str, Any]], st: float, tgt: float) -> dict[str, Any] | None:
    for c in cells:
        if abs(float(c["st_mult"]) - st) < 1e-9 and abs(float(c["tgt"]) - tgt) < 1e-9:
            return c
    return None


def pass_fail(cells: list[dict[str, Any]]) -> str:
    lines = ["--- PRE-REGISTERED PASS/FAIL ---"]
    core = _cell(cells, cfg.PASS_ST, cfg.PASS_TGT)
    ok = True
    if core is None:
        lines.append(f"FAIL: missing cell ST={cfg.PASS_ST} TGT={cfg.PASS_TGT}")
        return "\n".join(lines) + "\n"

    def mean_ok(s: dict[str, Any], rnd: dict[str, Any], tag: str) -> bool:
        m, rm = s["mean_net"], rnd["mean_net"]
        lines.append(
            f"  ST={cfg.PASS_ST} TGT={cfg.PASS_TGT} {tag} "
            f"mean={m} rnd={rm} n={s['n']}"
        )
        if not isinstance(m, float) or math.isnan(m) or m <= 0:
            return False
        if not isinstance(rm, float) or math.isnan(rm) or m <= rm:
            return False
        return True

    ok = mean_ok(core["sig_p2025"], core["rnd_p2025"], "P2025") and ok
    ok = mean_ok(core["sig_p2026"], core["rnd_p2026"], "P2026") and ok
    for st in cfg.PASS_ST_NONNEG:
        c = _cell(cells, st, cfg.PASS_TGT)
        if c is None:
            lines.append(f"FAIL: missing cell ST={st} TGT={cfg.PASS_TGT}")
            ok = False
            continue
        for tag, key in (("P2025", "sig_p2025"), ("P2026", "sig_p2026")):
            m = c[key]["mean_net"]
            lines.append(f"  ST={st} TGT={cfg.PASS_TGT} {tag} mean={m} n={c[key]['n']}")
            if isinstance(m, float) and not math.isnan(m) and m < 0:
                ok = False
    lines.append("PASS" if ok else "FAIL")
    return "\n".join(lines) + "\n"


def render_report(
    stamp: str, cells: list[dict[str, Any]], elapsed: float, n_filt: int, n_raw: int
) -> str:
    lines = [
        "S012F — S012 + 1h/4h Supertrend agreement (F1)",
        f"stamp={stamp}",
        f"TF={cfg.TF_MIN} HTF_ST={cfg.HTF_ST_MULT} ATR={cfg.HTF_ST_LEN}",
        f"P2025={cfg.P2025_FROM}..{cfg.P2025_TO}  P2026={cfg.P2026_FROM}..{cfg.P2026_TO}",
        f"raw_signals={n_raw} after_F1={n_filt}  elapsed_sec={elapsed:.1f}",
        "",
    ]
    for cell in cells:
        lines.append(f"ST={cell['st_mult']} TGT={cell['tgt']}")
        lines.append(fmt_row("signal P2025", cell["sig_p2025"], cell["rnd_p2025"]["mean_net"]))
        lines.append(fmt_row("signal P2026", cell["sig_p2026"], cell["rnd_p2026"]["mean_net"]))
        lines.append(fmt_row("random P2025", cell["rnd_p2025"]))
        lines.append(fmt_row("random P2026", cell["rnd_p2026"]))
        lines.append("")
    lines.append(pass_fail(cells))
    return "\n".join(lines)


def load_ckpt(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"done": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _synthetic_htf(n: int, tf_sec: int, start: int, up: bool) -> tuple[list[Candle], np.ndarray]:
    candles: list[Candle] = []
    px = 100.0
    for i in range(n):
        t0 = start + i * tf_sec
        o = px
        px = px + (1.0 if up else -1.0)
        h, l, cl = max(o, px) + 0.2, min(o, px) - 0.2, px
        candles.append(Candle(t0, t0 + tf_sec - 60, o, h, l, cl))
    h = np.array([c.high for c in candles])
    l = np.array([c.low for c in candles])
    c = np.array([c.close for c in candles])
    trend, _ = supertrend(h, l, c, cfg.HTF_ST_LEN, cfg.HTF_ST_MULT)
    return candles, trend


def filter_decision_trunc(
    entry_ts: int,
    side: str,
    c1h: list[Candle],
    c4h: list[Candle],
) -> bool:
    """Recompute HTF Supertrend using only bars completed before entry."""
    t1 = [c for c in c1h if int(c.ts_open) + 3600 < entry_ts]
    t4 = [c for c in c4h if int(c.ts_open) + 14400 < entry_ts]
    if len(t1) < cfg.HTF_ST_LEN or len(t4) < cfg.HTF_ST_LEN:
        return False
    tr1, _ = supertrend(
        np.array([c.high for c in t1]),
        np.array([c.low for c in t1]),
        np.array([c.close for c in t1]),
        cfg.HTF_ST_LEN,
        cfg.HTF_ST_MULT,
    )
    tr4, _ = supertrend(
        np.array([c.high for c in t4]),
        np.array([c.low for c in t4]),
        np.array([c.close for c in t4]),
        cfg.HTF_ST_LEN,
        cfg.HTF_ST_MULT,
    )
    sig = Signal("long" if side == "long" else "short", 0, entry_ts - 60, entry_ts, 0, 0, 0, date(2025, 6, 1), 0)
    return htf_allows(sig, t1, tr1, t4, tr4)


def test_htf_lookahead() -> None:
    start = 1_735_689_600  # 2025-01-01 00:00 UTC
    c1h, t1h = _synthetic_htf(90, 3600, start, up=True)
    c4h, t4h = _synthetic_htf(30, 14400, start, up=True)
    # After 16 completed 4h bars (ATR 10 is live); entry mid-bar so last HTF is strictly prior.
    entry = start + 16 * 14400 + 1800
    sig_l = Signal("long", 0, entry - 60, entry, 100.0, 0, 1, date(2025, 1, 1), 2)
    sig_s = Signal("short", 0, entry - 60, entry, 100.0, 0, 1, date(2025, 1, 1), 2)
    full_l = htf_allows(sig_l, c1h, t1h, c4h, t4h)
    full_s = htf_allows(sig_s, c1h, t1h, c4h, t4h)
    trunc_l = filter_decision_trunc(entry, "long", c1h, c4h)
    trunc_s = filter_decision_trunc(entry, "short", c1h, c4h)
    assert full_l is True, "uptrend 1h+4h should allow long"
    assert full_s is False, "uptrend should reject short"
    assert trunc_l == full_l, "long filter look-ahead leak"
    assert trunc_s == full_s, "short filter look-ahead leak"
    # Downtrend mirror
    c1d, t1d = _synthetic_htf(90, 3600, start, up=False)
    c4d, t4d = _synthetic_htf(30, 14400, start, up=False)
    full_ld = htf_allows(sig_l, c1d, t1d, c4d, t4d)
    full_sd = htf_allows(sig_s, c1d, t1d, c4d, t4d)
    assert full_ld is False and full_sd is True
    assert filter_decision_trunc(entry, "long", c1d, c4d) == full_ld
    assert filter_decision_trunc(entry, "short", c1d, c4d) == full_sd
    print("s012f htf look-ahead: PASS")


def main() -> None:
    p = argparse.ArgumentParser(description="S012F HTF filter")
    p.add_argument("--csv", default=s012cfg.DEFAULT_CSV)
    p.add_argument("--out", default=cfg.DEFAULT_OUT)
    p.add_argument("--max-days", type=int, default=0)
    p.add_argument("--st", type=float, default=0.0)
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        test_htf_lookahead()
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
        ckpt = load_ckpt(ckpt_path)
        stamp = str(ckpt.get("stamp", stamp))
    else:
        ckpt = {"stamp": stamp, "done": [], "cells": [], "n_raw": 0, "n_filt": 0}

    txt_path = out_dir / f"s012f_{stamp}.txt"
    csv_path = out_dir / f"s012f_{stamp}_trades.csv"

    logger.info("loading spot %s", args.csv)
    spot = load_spot_1m(args.csv)
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
        logger.info("max-days=%s cutoff_ts=%s", args.max_days, cutoff_ts)

    logger.info("building 5m/1h/4h candles")
    c5 = build_tf(spot, cfg.TF_MIN)
    c1h = build_tf(spot, 60)
    c4h = build_tf(spot, 240)
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
    h5 = np.array([x.high for x in c5])
    l5 = np.array([x.low for x in c5])
    cl5 = np.array([x.close for x in c5])

    st_mults = (float(args.st),) if args.st else cfg.ST_MULTS
    store = MarksStore()
    cells: list[dict[str, Any]] = list(ckpt.get("cells", []))
    done = {float(x) for x in ckpt.get("done", [])}
    n_raw = int(ckpt.get("n_raw", 0))
    n_filt = int(ckpt.get("n_filt", 0))

    try:
        for st_m in st_mults:
            if float(st_m) in done:
                logger.info("skip checkpoint ST=%s", st_m)
                continue
            logger.info("5m supertrend ST=%s", st_m)
            trend, st_arr = supertrend(h5, l5, cl5, s012cfg.ST_LEN, float(st_m))
            sigs, lp_active, _la = collect_signals(
                c5, trend, st_arr, spot, start_ts, cutoff_ts
            )
            pool = random_pool(
                c5, trend, st_arr, spot, start_ts, cutoff_ts, lp_active, sigs
            )
            sigs_f = [s for s in sigs if htf_allows(s, c1h, t1h, c4h, t4h)]
            pool_f = [s for s in pool if htf_allows(s, c1h, t1h, c4h, t4h)]
            n_raw = len(sigs)
            n_filt = len(sigs_f)
            logger.info("raw_signals=%s after_F1=%s pool_f=%s", n_raw, n_filt, len(pool_f))
            for tgt in cfg.TGT_MULTS:
                sig_tr, rnd_tr, _ = run_cell(
                    candles=c5,
                    trend=trend,
                    st=st_arr,
                    spot=spot,
                    store=store,
                    start_ts=start_ts,
                    cutoff_ts=cutoff_ts,
                    tf_min=cfg.TF_MIN,
                    st_mult=float(st_m),
                    tgt=float(tgt),
                    rng_seed=s012cfg.RANDOM_SEED,
                    sigs=sigs_f,
                    pool=pool_f,
                    la_fail=0,
                )
                write_trades_csv(csv_path, sig_tr + rnd_tr)
                cell = {
                    "st_mult": float(st_m),
                    "tgt": float(tgt),
                    "sig_p2025": summarize(sig_tr, "P2025"),
                    "sig_p2026": summarize(sig_tr, "P2026"),
                    "rnd_p2025": summarize(rnd_tr, "P2025"),
                    "rnd_p2026": summarize(rnd_tr, "P2026"),
                }
                cells.append(cell)
                logger.info(
                    "ST=%s TGT=%s sig_n=%s rnd_n=%s",
                    st_m, tgt, len(sig_tr), len(rnd_tr),
                )
            done.add(float(st_m))
            ckpt = {
                "stamp": stamp,
                "done": sorted(done),
                "cells": cells,
                "n_raw": n_raw,
                "n_filt": n_filt,
            }
            ckpt_path.write_text(json.dumps(ckpt, default=str), encoding="utf-8")
            elapsed = time.perf_counter() - t0
            txt_path.write_text(
                render_report(stamp, cells, elapsed, n_filt, n_raw), encoding="utf-8"
            )
    finally:
        store.close()

    elapsed = time.perf_counter() - t0
    report = render_report(stamp, cells, elapsed, n_filt, n_raw)
    txt_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"SMOKE unique_signals_F1={n_filt} raw={n_raw} elapsed_sec={elapsed:.2f} txt={txt_path}")
    n_st = max(len(st_mults), 1)
    n_days = args.max_days if args.max_days else 10
    scale = (len(cfg.ST_MULTS) / n_st) * (630.0 / max(n_days, 1))
    print(f"EST_FULL_SEC={elapsed * scale:.1f} scale={scale:.3f}")


if __name__ == "__main__":
    main()
