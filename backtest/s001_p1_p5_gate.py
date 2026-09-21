#!/usr/bin/env python3
"""
S001 reverify P1–P5 gate — reads tagged cycle CSVs (no live backend).

Pre-registered criteria (plan):
  P1  OOS mean/day > 0 AND day-clustered bootstrap ci_lo > 0
  P2  every 3rd-day non-overlapping subset mean > 0 (3 subsets)
  P3  worst cycle >= -22 USD (if fail under wing_roll ON, recompute cap — do not retune config)
  P4  (locked − BASELINE_OLD_LIVE) paired ci_lo > 0 on OOS
  P5  OOS mean/day >= 50% of IS mean/day

Example (after external OOS/IS runs):
  python -m backtest.s001_p1_p5_gate \\
    --oos-csv backtest/results/s001_oos_locked_cycles.csv \\
    --is-csv  backtest/results/s001_is_locked_cycles.csv \\
    --oos-baseline-csv backtest/results/s001_oos_locked_baseline_cycles.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.s001_mark_engine import (  # noqa: E402
    BOOTSTRAP_N,
    BOOTSTRAP_SEED,
    WORST_CYCLE_CAP_USD,
    day_clustered_ci_daily,
    max_drawdown,
    paired_cycle_diff_ci,
)

logger = logging.getLogger("s001_p1_p5_gate")
RESULTS = _BACKTEST / "results"


def _f(x: Any, default: float = float("nan")) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def load_cycles_csv(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def daily_pnl(
    rows: list[dict[str, Any]], d0: date, d1: date
) -> tuple[list[float], dict[date, float]]:
    by_day: dict[date, float] = defaultdict(float)
    for r in rows:
        d = date.fromisoformat(str(r["date"]))
        by_day[d] += _f(r["net_pnl"], 0.0)
    daily = [by_day.get(d0 + timedelta(days=i), 0.0) for i in range((d1 - d0).days + 1)]
    return daily, by_day


def window_bounds(rows: list[dict[str, Any]]) -> tuple[date, date]:
    dates = [date.fromisoformat(str(r["date"])) for r in rows]
    return min(dates), max(dates)


def every_third_subset_means(
    by_day: dict[date, float], d0: date, d1: date
) -> list[tuple[str, float, int]]:
    """
    Three non-overlapping subsets by calendar day index mod 3.
    Subset k = days where (day - d0).days % 3 == k.
    """
    out: list[tuple[str, float, int]] = []
    for k in range(3):
        vals: list[float] = []
        d = d0
        while d <= d1:
            if (d - d0).days % 3 == k:
                vals.append(by_day.get(d, 0.0))
            d += timedelta(days=1)
        mean = sum(vals) / len(vals) if vals else float("nan")
        out.append((f"mod3={k}", mean, len(vals)))
    return out


def paired_from_csv(
    arm_rows: list[dict[str, Any]],
    base_rows: list[dict[str, Any]],
) -> tuple[float, float, float, int]:
    """Rebuild minimal CycleResult-like pairing by entry_date + order."""

    class _C:
        def __init__(self, d: date, net: float) -> None:
            self.entry_date = d
            self.net_pnl = net

    arm = [
        _C(date.fromisoformat(str(r["date"])), _f(r["net_pnl"]))
        for r in arm_rows
    ]
    base = [
        _C(date.fromisoformat(str(r["date"])), _f(r["net_pnl"]))
        for r in base_rows
    ]
    return paired_cycle_diff_ci(arm, base, BOOTSTRAP_N, BOOTSTRAP_SEED)  # type: ignore[arg-type]


def evaluate(
    *,
    oos_rows: list[dict[str, Any]],
    is_rows: list[dict[str, Any]],
    oos_baseline_rows: list[dict[str, Any]] | None,
    worst_cap: float,
) -> list[str]:
    lines: list[str] = []
    lines.append("===== S001 P1–P5 GATE (locked mark engine) =====")
    lines.append(f"generated_utc={datetime.now(tz=timezone.utc).isoformat()}")
    lines.append(f"bootstrap_n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED}")
    lines.append(f"worst_cap_usd={worst_cap}")
    lines.append("")

    oos_d0, oos_d1 = window_bounds(oos_rows)
    is_d0, is_d1 = window_bounds(is_rows)
    oos_daily, oos_by = daily_pnl(oos_rows, oos_d0, oos_d1)
    is_daily, _is_by = daily_pnl(is_rows, is_d0, is_d1)

    oos_mean = sum(oos_daily) / len(oos_daily) if oos_daily else float("nan")
    is_mean = sum(is_daily) / len(is_daily) if is_daily else float("nan")
    _bm, oos_lo, oos_hi = day_clustered_ci_daily(
        oos_daily, BOOTSTRAP_N, BOOTSTRAP_SEED
    )
    worst = min((_f(r["net_pnl"]) for r in oos_rows), default=float("nan"))
    worst_date = ""
    for r in oos_rows:
        if _f(r["net_pnl"]) == worst:
            worst_date = str(r["date"])
            break

    # P1
    p1_ok = (oos_mean > 0) and (oos_lo > 0)
    lines.append(
        f"P1 {'PASS' if p1_ok else 'FAIL'} | "
        f"OOS mean/day={oos_mean:.6f} ci_lo={oos_lo:.6f} ci_hi={oos_hi:.6f} "
        f"(need mean>0 and ci_lo>0) n_cycles={len(oos_rows)} "
        f"window={oos_d0}..{oos_d1}"
    )

    # P2
    subsets = every_third_subset_means(oos_by, oos_d0, oos_d1)
    p2_ok = all(m > 0 for _name, m, _n in subsets)
    detail = ", ".join(f"{name} mean={m:.6f} (n_days={n})" for name, m, n in subsets)
    lines.append(
        f"P2 {'PASS' if p2_ok else 'FAIL'} | every-3rd-day subsets: {detail} "
        f"(need all mean>0)"
    )

    # P3
    p3_ok = worst >= worst_cap
    lines.append(
        f"P3 {'PASS' if p3_ok else 'FAIL'} | worst_cycle={worst:.6f} "
        f"date={worst_date} cap={worst_cap:.2f} "
        f"(need worst >= cap). "
        f"NOTE: -22 was under wing_roll OFF; if FAIL with roll ON, "
        f"recompute cap — do not retune config."
    )
    if not p3_ok and not math.isnan(worst):
        lines.append(
            f"  P3_HINT suggested_cap_roll_on={worst:.2f} "
            f"(floor to whole USD if promoting)"
        )

    # P4
    if oos_baseline_rows:
        p_mean, p_lo, p_hi, p_n = paired_from_csv(oos_rows, oos_baseline_rows)
        p4_ok = p_lo > 0
        lines.append(
            f"P4 {'PASS' if p4_ok else 'FAIL'} | "
            f"paired(locked-baseline) mean={p_mean:.6f} "
            f"ci_lo={p_lo:.6f} ci_hi={p_hi:.6f} n={p_n} "
            f"(need ci_lo>0)"
        )
    else:
        p4_ok = False
        lines.append(
            "P4 FAIL | missing --oos-baseline-csv "
            "(re-run OOS with --with-baseline)"
        )

    # P5
    if is_mean > 0:
        p5_ok = oos_mean >= 0.5 * is_mean
        lines.append(
            f"P5 {'PASS' if p5_ok else 'FAIL'} | "
            f"OOS mean={oos_mean:.6f} IS mean={is_mean:.6f} "
            f"ratio={oos_mean / is_mean:.4f} (need OOS >= 50% of IS)"
        )
    elif is_mean == 0:
        p5_ok = oos_mean >= 0
        lines.append(
            f"P5 {'PASS' if p5_ok else 'FAIL'} | IS mean=0; "
            f"OOS mean={oos_mean:.6f}"
        )
    else:
        # IS negative — require OOS not worse than 50% of IS magnitude toward zero
        p5_ok = oos_mean >= 0.5 * is_mean
        lines.append(
            f"P5 {'PASS' if p5_ok else 'FAIL'} | IS mean negative "
            f"({is_mean:.6f}); OOS={oos_mean:.6f} "
            f"(need OOS >= 0.5*IS)"
        )

    lines.append("")
    flags = [
        ("P1", p1_ok),
        ("P2", p2_ok),
        ("P3", p3_ok),
        ("P4", p4_ok),
        ("P5", p5_ok),
    ]
    lines.append(
        " | ".join(f"{n} {'PASS' if ok else 'FAIL'}" for n, ok in flags)
    )
    n_pass = sum(1 for _n, ok in flags if ok)
    lines.append(f"TOTAL {'PASS' if n_pass == 5 else 'FAIL'} ({n_pass}/5)")
    lines.append(
        f"extras OOS max_dd={max_drawdown(oos_daily):.6f} "
        f"IS_window={is_d0}..{is_d1} IS_n={len(is_rows)}"
    )
    return lines


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S001 P1–P5 gate on cycle CSVs")
    ap.add_argument("--oos-csv", type=str, required=True)
    ap.add_argument("--is-csv", type=str, required=True)
    ap.add_argument("--oos-baseline-csv", type=str, default="")
    ap.add_argument(
        "--worst-cap",
        type=float,
        default=WORST_CYCLE_CAP_USD,
        help="P3 floor (default -22; raise only after roll-ON recompute)",
    )
    ap.add_argument(
        "--out",
        type=str,
        default=str(RESULTS / "s001_p1_p5_gate.txt"),
    )
    args = ap.parse_args()

    oos = load_cycles_csv(Path(args.oos_csv))
    is_rows = load_cycles_csv(Path(args.is_csv))
    if not oos or not is_rows:
        logger.error("empty CSV oos=%d is=%d", len(oos), len(is_rows))
        return 2
    base = (
        load_cycles_csv(Path(args.oos_baseline_csv))
        if args.oos_baseline_csv
        else None
    )
    lines = evaluate(
        oos_rows=oos,
        is_rows=is_rows,
        oos_baseline_rows=base,
        worst_cap=float(args.worst_cap),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for ln in lines:
        print(ln)
    logger.info("wrote %s", out)
    total = next((ln for ln in lines if ln.startswith("TOTAL ")), "TOTAL FAIL")
    return 0 if total.startswith("TOTAL PASS") else 1


if __name__ == "__main__":
    raise SystemExit(main())
