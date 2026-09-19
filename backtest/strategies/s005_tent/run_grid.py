#!/usr/bin/env python3
"""S005 Tent — 72-combo exploration grid runner."""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent.parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.data import MarksStore, find_spot_csv, load_spot_map  # noqa: E402
from backtest.harness.registry import write_registry  # noqa: E402
from backtest.strategies.s005_tent.strategy import (  # noqa: E402
    BOOTSTRAP_N,
    BOOTSTRAP_SEED,
    S005TentStrategy,
    default_params,
)

logger = logging.getLogger("s005_grid")

RUNS_DIR = Path(__file__).resolve().parent / "runs"

EXPIRY_PAIRS = ((1, 0), (2, 1))
QTY_SPLITS = ((10, 20), (15, 15), (20, 10))
TARGET_PCTS = (5.0, 10.0, 15.0)
SL_MULTS = (2.0, 3.0, 4.0, 5.0)


def all_combos() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sd, ld in EXPIRY_PAIRS:
        for qs, qg in QTY_SPLITS:
            for tp in TARGET_PCTS:
                for sl in SL_MULTS:
                    arm = f"s{sd}l{ld}_q{qs}_{qg}_tp{tp:.0f}_sl{sl:.0f}"
                    out.append(
                        {
                            "short_dte": sd,
                            "long_dte": ld,
                            "qty_straddle": qs,
                            "qty_strangle": qg,
                            "target_pct": tp,
                            "sl_mult": sl,
                            "arm": arm,
                        }
                    )
    return out


def _fmt(x: Any, prec: int = 4) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "nan"
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return "nan"
    return f"{v:.{prec}f}"


def mark_ci_overlap_with_top(rows: list[dict[str, Any]]) -> None:
    """Flag combos whose mean/day CI overlaps the top combo's CI (noise)."""
    if not rows:
        return
    ranked = sorted(
        rows,
        key=lambda r: float(r.get("mean_day") or float("-inf")),
        reverse=True,
    )
    top = ranked[0]
    t_lo = float(top.get("ci_lo") or float("nan"))
    t_hi = float(top.get("ci_hi") or float("nan"))
    for r in rows:
        lo = float(r.get("ci_lo") or float("nan"))
        hi = float(r.get("ci_hi") or float("nan"))
        if r is top or r.get("arm") == top.get("arm"):
            r["ci_overlap_top"] = False
            r["noise_vs_top"] = "TOP"
            continue
        if any(math.isnan(x) for x in (t_lo, t_hi, lo, hi)):
            r["ci_overlap_top"] = True
            r["noise_vs_top"] = "unknown"
            continue
        overlap = not (hi < t_lo or lo > t_hi)
        r["ci_overlap_top"] = overlap
        r["noise_vs_top"] = "YES_NOISE" if overlap else "SEPARATED"


def format_report(rows: list[dict[str, Any]], *, window: str, elapsed: float) -> list[str]:
    mark_ci_overlap_with_top(rows)
    ranked = sorted(
        rows, key=lambda r: float(r.get("mean_day") or float("-inf")), reverse=True
    )
    lines = [
        "===== S005 TENT — COMBO GRID =====",
        "3-month exploration — CI overlap wale combos ko alag mat maano"
        if "2026-06-01" in window
        else "SMOKE exploration — CI overlap wale combos ko alag mat maano",
        f"window={window}",
        f"bootstrap n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED}",
        f"n_combos={len(rows)} elapsed_sec={elapsed:.1f}",
        "",
        (
            f"{'arm':<28} {'n':>4} {'b/d':>5} {'win%':>5} "
            f"{'mean/d':>8} {'ci_lo':>8} {'ci_hi':>8} "
            f"{'worst':>8} {'maxDD':>8} {'hold':>5} "
            f"{'maxL':>7} {'units':>5} {'d%':>6} {'noise':>10}  exits"
        ),
    ]
    for r in ranked:
        mix = ",".join(
            f"{k[:3]}={v:.0f}%" for k, v in (r.get("exit_mix") or {}).items()
        )
        lines.append(
            f"{str(r.get('arm','')):<28} "
            f"{int(r.get('n_baskets') or 0):4d} "
            f"{_fmt(r.get('baskets_per_day'), 2):>5} "
            f"{_fmt(r.get('win_pct'), 1):>5} "
            f"{_fmt(r.get('mean_day')):>8} "
            f"{_fmt(r.get('ci_lo')):>8} "
            f"{_fmt(r.get('ci_hi')):>8} "
            f"{_fmt(r.get('worst_net')):>8} "
            f"{_fmt(r.get('max_dd')):>8} "
            f"{_fmt(r.get('hold_med'), 2):>5} "
            f"{_fmt(r.get('max_loss_per_basket'), 3):>7} "
            f"{int(r.get('basket_units_at_3pct') or 0):5d} "
            f"{_fmt(r.get('daily_net_pct_of_capital_at_size'), 2):>6} "
            f"{str(r.get('noise_vs_top') or ''):>10}  "
            f"{mix}"
        )
    lines.append("")
    lines.append("===== SKIP / COOLDOWN (per combo, summarized in json) =====")
    for r in ranked[:5]:
        sk = r.get("skips") or {}
        lines.append(
            f"{r.get('arm')}: entered={sk.get('cycles_entered')} "
            f"counts={sk.get('counts')} cooldown_skips={r.get('cooldown_entry_skips')} "
            f"examples={sk.get('examples')}"
        )
    lines.append("")
    return lines


def run_grid(d0: date, d1: date, *, stage: str = "KILL") -> dict[str, Any]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    spot_path = find_spot_csv()
    if spot_path is None:
        raise FileNotFoundError("No BTCUSD_1m CSV")
    spot_map = load_spot_map(spot_path)
    store = MarksStore()
    combos = all_combos()
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    logger.info("S005 grid n_combos=%d window=%s..%s", len(combos), d0, d1)

    for i, combo in enumerate(combos, start=1):
        params = default_params()
        params.update(combo)
        strat = S005TentStrategy(params)
        ct0 = time.time()
        _cycles, _skips, stats = strat.run_window(
            d0, d1, store=store, spot_map=spot_map
        )
        row = {
            "arm": combo["arm"],
            **combo,
            **stats,
            "elapsed_sec": time.time() - ct0,
        }
        rows.append(row)
        logger.info(
            "[%d/%d] %s n=%d mean/day=%s elapsed=%.1fs",
            i,
            len(combos),
            combo["arm"],
            stats.get("n_baskets"),
            _fmt(stats.get("mean_day")),
            row["elapsed_sec"],
        )

    store.close()
    elapsed = time.time() - t0
    mark_ci_overlap_with_top(rows)
    window = f"{d0.isoformat()}..{d1.isoformat()}"
    lines = format_report(rows, window=window, elapsed=elapsed)

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"S005_{stage}_{stamp}"
    json_path = RUNS_DIR / f"{base}.json"
    md_path = RUNS_DIR / f"{base}.md"
    payload = {
        "strategy_id": "S005",
        "stage": stage,
        "window": {"from": d0.isoformat(), "to": d1.isoformat()},
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
        "elapsed_sec": elapsed,
        "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED},
        "combos": rows,
        "note": "3-month exploration — CI overlap wale combos ko alag mat maano",
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("wrote %s and %s", json_path, md_path)

    # update registry meta tests_done with smoke/kill summary
    meta_path = Path(__file__).resolve().parent / "registry_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        top = max(rows, key=lambda r: float(r.get("mean_day") or float("-inf")))
        meta.setdefault("tests_done", []).append(
            {
                "stage": stage,
                "window": window,
                "date": stamp[:8],
                "n": top.get("n_baskets"),
                "mean_day": top.get("mean_day"),
                "ci_lo": top.get("ci_lo"),
                "ci_hi": top.get("ci_hi"),
                "verdict": f"GRID_{len(rows)}_COMBOS",
                "top_arm": top.get("arm"),
            }
        )
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        write_registry()
    except Exception as e:
        logger.warning("registry update failed: %s", e)

    for ln in lines:
        logger.info("%s", ln)
    return {"rows": rows, "elapsed_sec": elapsed, "json": str(json_path), "md": str(md_path)}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S005 Tent 72-combo grid")
    ap.add_argument("--stage", type=str, default="KILL")
    ap.add_argument("--from", dest="from_date", type=str, required=True)
    ap.add_argument("--to", dest="to_date", type=str, required=True)
    args = ap.parse_args()
    d0 = date.fromisoformat(args.from_date)
    d1 = date.fromisoformat(args.to_date)
    out = run_grid(d0, d1, stage=args.stage.upper())
    logger.info(
        "DONE combos=%d elapsed=%.1fs total_baskets=%d",
        len(out["rows"]),
        out["elapsed_sec"],
        sum(int(r.get("n_baskets") or 0) for r in out["rows"]),
    )


if __name__ == "__main__":
    main()
