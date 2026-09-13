#!/usr/bin/env python3
"""
S003 — run sim + edge across 1m/3m/5m/15m and emit one comparison table.

Reuses backtest.s003_sim / s003_edge (same live LSR4 engine). No backend edits.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_BACKTEST = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s003_edge  # noqa: E402
import s003_sim  # noqa: E402

run_edge = s003_edge.run
run_sim = s003_sim.run
DEFAULT_SEED = s003_edge.DEFAULT_SEED
DEFAULT_STOPS = s003_edge.DEFAULT_STOPS
DEFAULT_TARGETS = s003_edge.DEFAULT_TARGETS

IST = ZoneInfo("Asia/Kolkata")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATA_DIR = Path(__file__).resolve().parent / "data_1m"
CONFIG = Path(__file__).resolve().parent / "s003_sim_config.json"
TIMEFRAMES = ("1m", "3m", "5m", "15m")

# Distinct reproducible seeds per timeframe (never reuse a 1m baseline for 15m).
SEED_BY_TF = {
    "1m": DEFAULT_SEED + 1,
    "3m": DEFAULT_SEED + 3,
    "5m": DEFAULT_SEED + 5,
    "15m": DEFAULT_SEED + 15,
}


def _data_file(tf: str) -> Path:
    matches = sorted(DATA_DIR.glob(f"BTCUSD_{tf}_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_{tf}_*.csv in {DATA_DIR}")
    # Prefer the longest / latest-named 12m file
    return matches[-1]


def _fmt(v: Any, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def _pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{100.0 * v:.1f}%"


def _ts_label(ts: tuple[int, int] | None) -> str:
    if ts is None:
        return "n/a"
    return f"T={ts[0]}/S={ts[1]}"


def run_comparison() -> Path:
    rows: list[dict[str, Any]] = []
    for tf in TIMEFRAMES:
        seed = SEED_BY_TF[tf]
        data = _data_file(tf)
        print("=" * 72)
        print(f"TIMEFRAME {tf}  data={data.name}  seed={seed}")
        print("=" * 72)

        t0 = time.perf_counter()
        _sig, _arm, _sum, sim_meta = run_sim(
            config_path=CONFIG,
            start=None,
            end=None,
            data_file=data,
            timeframe=tf,
        )
        sim_s = time.perf_counter() - t0

        t1 = time.perf_counter()
        _txt, _csv, edge = run_edge(
            signals_path=sim_meta["signals_path"],
            data_path=data,
            seed=seed,
            timeframe=tf,
            targets=DEFAULT_TARGETS,
            stops=DEFAULT_STOPS,
        )
        edge_s = time.perf_counter() - t1

        rows.append(
            {
                "timeframe": tf,
                "seed": seed,
                "data_file": data.name,
                "signals_total": edge["signals_total"],
                "signals_per_day": edge["signals_per_day"],
                "median_distance_points": edge["median_distance_points"],
                "cost_as_pct_of_median_distance": edge[
                    "cost_as_pct_of_median_distance"
                ],
                "hit_100_before_50_signal": edge["hit_100_before_50_signal"],
                "hit_100_before_50_baseline": edge["hit_100_before_50_baseline"],
                "best_expectancy_signal": edge["best_expectancy_signal"],
                "best_expectancy_signal_ts": edge["best_expectancy_signal_ts"],
                "best_expectancy_baseline": edge["best_expectancy_baseline"],
                "best_expectancy_baseline_ts": edge["best_expectancy_baseline_ts"],
                "edge_over_baseline": edge["edge_over_baseline"],
                "clears_23": edge["clears_23"],
                "clears_53": edge["clears_53"],
                "sim_runtime_s": sim_s,
                "edge_runtime_s": edge_s,
                "runtime_s": sim_s + edge_s,
            }
        )

    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"s003_tf_comparison_{stamp}.txt"

    lines: list[str] = []
    lines.append("=== S003 timeframe comparison (sim + edge) ===")
    lines.append(f"targets: {list(DEFAULT_TARGETS)}")
    lines.append(f"stops:   {list(DEFAULT_STOPS)}")
    lines.append("cost floors: hedge OFF=23  hedge ON=53")
    lines.append(
        "baseline: regenerated per timeframe with distinct seeds "
        "(never reuse another TF's random entries)"
    )
    lines.append("")
    lines.append(
        f"{'tf':>4}  {'sigs':>6}  {'spd':>6}  {'med_dist':>8}  "
        f"{'cost%dist':>9}  {'h100s':>6}  {'h100b':>6}  "
        f"{'E_sig':>7}  {'E_base':>7}  {'edge':>7}  "
        f"{'T/S':>12}  {'>23':>3}  {'>53':>3}  {'seed':>10}  {'runtime':>8}"
    )
    lines.append("-" * 120)
    for r in rows:
        lines.append(
            f"{r['timeframe']:>4}  "
            f"{r['signals_total']:>6}  "
            f"{_fmt(r['signals_per_day'], 1):>6}  "
            f"{_fmt(r['median_distance_points'], 1):>8}  "
            f"{_fmt(r['cost_as_pct_of_median_distance'], 1):>9}  "
            f"{_pct(r['hit_100_before_50_signal']):>6}  "
            f"{_pct(r['hit_100_before_50_baseline']):>6}  "
            f"{_fmt(r['best_expectancy_signal']):>7}  "
            f"{_fmt(r['best_expectancy_baseline']):>7}  "
            f"{_fmt(r['edge_over_baseline']):>7}  "
            f"{_ts_label(r['best_expectancy_signal_ts']):>12}  "
            f"{'yes' if r['clears_23'] else 'no':>3}  "
            f"{'yes' if r['clears_53'] else 'no':>3}  "
            f"{r['seed']:>10}  "
            f"{_fmt(r['runtime_s'], 1):>8}"
        )
    lines.append("")
    lines.append("Column notes:")
    lines.append("  spd      = signals_per_day")
    lines.append("  cost%dist = 23 / median_distance * 100")
    lines.append("  h100s/b  = hit_100_before_50 signal vs baseline")
    lines.append("  E_sig/E_base = best expectancy over the T/S grid")
    lines.append("  edge     = E_sig - E_base   <--- decisive column")
    lines.append("")
    lines.append("Per-timeframe detail:")
    for r in rows:
        lines.append(
            f"  {r['timeframe']}: signals={r['signals_total']}  "
            f"median_distance={_fmt(r['median_distance_points'], 2)}  "
            f"seed={r['seed']}  data={r['data_file']}  "
            f"sim={_fmt(r['sim_runtime_s'], 2)}s  "
            f"edge={_fmt(r['edge_runtime_s'], 2)}s"
        )
        lines.append(
            f"    best signal expectancy {_fmt(r['best_expectancy_signal'])} at "
            f"{_ts_label(r['best_expectancy_signal_ts'])}; "
            f"baseline {_fmt(r['best_expectancy_baseline'])} at "
            f"{_ts_label(r['best_expectancy_baseline_ts'])}; "
            f"edge_over_baseline={_fmt(r['edge_over_baseline'])}"
        )
    lines.append("")

    text = "\n".join(lines)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"saved: {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(
        description="S003 4-timeframe sim+edge comparison"
    ).parse_args(argv)
    try:
        run_comparison()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
