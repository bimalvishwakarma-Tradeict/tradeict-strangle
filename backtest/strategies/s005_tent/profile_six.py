#!/usr/bin/env python3
"""STEP1: Profile S005 6-combo 3-month grid — write top20 to results/s005_profile.txt."""

from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from datetime import date
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent.parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.mark_cache import get_mark_cache, reset_mark_cache  # noqa: E402
from backtest.strategies.s005_tent.run_grid import build_combos, run_grid  # noqa: E402
from backtest.strategies.s005_tent.strategy import (  # noqa: E402
    DEFAULT_ABSORB_MIN,
    DEFAULT_BE_MULT,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_EXIT_MODE,
    DEFAULT_MAX_BASKET_LOSS,
    DEFAULT_NO_TARGET,
    DEFAULT_PROTECTION_EXPIRY,
    DEFAULT_PROTECTION_OFFSET,
    DEFAULT_PROTECTION_RATIO,
    DEFAULT_TRIGGER_PCT,
)

OUT = _BACKTEST / "results" / "s005_profile.txt"
D0 = date(2026, 6, 1)
D1 = date(2026, 8, 31)


def make_six_combos():
    # 2 expiry × 1 qty × 1 tp × 3 sl = 6
    return build_combos(
        expiry_pairs=[(1, 0), (2, 1)],
        qty_splits=[(10, 20)],
        target_pcts=[10.0],
        sl_mults=[2.0, 3.0, 4.0],
        protection_expiries=[DEFAULT_PROTECTION_EXPIRY],
        protection_offsets=[DEFAULT_PROTECTION_OFFSET],
        protection_ratios=[DEFAULT_PROTECTION_RATIO],
        be_mults=[DEFAULT_BE_MULT],
        cutoff_times=["17:25"],
        cooldowns=[DEFAULT_COOLDOWN_HOURS],
        exit_modes=[DEFAULT_EXIT_MODE],
        trigger_pcts=[DEFAULT_TRIGGER_PCT],
        absorb_mins=[DEFAULT_ABSORB_MIN],
        max_basket_losses=[DEFAULT_MAX_BASKET_LOSS],
        no_targets=[DEFAULT_NO_TARGET],
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        choices=("baseline", "after"),
        default="after",
        help="baseline writes fresh file; after appends comparison",
    )
    args = ap.parse_args()

    combos = make_six_combos()
    assert len(combos) == 6, len(combos)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    reset_mark_cache()  # clean slate so after-run measures cache fill+hit

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    out = run_grid(D0, D1, stage="KILL", tag=f"profile6_{args.phase}", combos=combos)
    pr.disable()
    wall = time.perf_counter() - t0

    buf = io.StringIO()
    stats = pstats.Stats(pr, stream=buf)
    stats.sort_stats("cumulative")
    stats.print_stats(20)

    mean_days = {r["arm"]: r.get("mean_day") for r in out["rows"]}
    cache_stats = get_mark_cache().stats()

    if args.phase == "baseline":
        lines = [
            "===== S005 PROFILE — BASELINE (before cache) =====",
            f"window={D0.isoformat()}..{D1.isoformat()}",
            f"n_combos={len(combos)}",
            f"arms={[c['arm'] for c in combos]}",
            f"wall_elapsed_sec={wall:.3f}",
            f"run_grid_elapsed_sec={out['elapsed_sec']:.3f}",
            f"total_baskets={sum(int(r.get('n_baskets') or 0) for r in out['rows'])}",
            f"mean_day_by_arm={mean_days!r}",
            "",
            "===== top 20 by cumulative time =====",
            buf.getvalue(),
            "",
        ]
        OUT.write_text("\n".join(lines), encoding="utf-8")
    else:
        # Append after-cache section; compare mean/day to baseline block if present
        prev = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        baseline_means = None
        for line in prev.splitlines():
            if line.startswith("mean_day_by_arm="):
                baseline_means = eval(line.split("=", 1)[1], {"__builtins__": {}})  # noqa: S307
                break
        match = True
        diffs: list[str] = []
        if isinstance(baseline_means, dict):
            for arm, md in mean_days.items():
                b = baseline_means.get(arm)
                if b is None or abs(float(md) - float(b)) > 1e-12:
                    match = False
                    diffs.append(f"{arm}: before={b!r} after={md!r}")
        else:
            match = False
            diffs.append("no baseline mean_day_by_arm found")

        append = [
            "",
            "===== STEP2 — BOTTLENECK REPORT (from baseline profile) =====",
            "Dominant cost: sqlite3.Cursor.fetchall (~626s / 659s ≈ 95%).",
            "Called mainly via harness.data.load_chain (~617s cum) and strategy._preload (~27s).",
            "Black-76 / _net_theta_usd ≈ 0.8s; bootstrap day_clustered_ci_daily ≈ 0.9s;",
            "monitor/_adj_mtm ≈ 5.6s — not the bottleneck.",
            "",
            "===== S005 PROFILE — AFTER shared mark cache =====",
            f"window={D0.isoformat()}..{D1.isoformat()}",
            f"n_combos={len(combos)}",
            f"wall_elapsed_sec={wall:.3f}",
            f"run_grid_elapsed_sec={out['elapsed_sec']:.3f}",
            f"total_baskets={sum(int(r.get('n_baskets') or 0) for r in out['rows'])}",
            f"mean_day_by_arm={mean_days!r}",
            f"cache_stats={cache_stats!r}",
            f"mean_day_identical_to_baseline={match}",
            f"mean_day_diffs={diffs!r}",
            "",
            "===== top 20 by cumulative time (after) =====",
            buf.getvalue(),
            "",
        ]
        with OUT.open("a", encoding="utf-8") as f:
            f.write("\n".join(append))

    print(f"wrote/appended {OUT} phase={args.phase}")
    print(f"wall={wall:.1f}s run_grid={out['elapsed_sec']:.1f}s cache={cache_stats}")
    for arm, md in mean_days.items():
        print(f"  {arm}: mean/day={md}")


if __name__ == "__main__":
    main()
