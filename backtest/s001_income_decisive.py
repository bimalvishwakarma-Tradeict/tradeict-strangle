#!/usr/bin/env python3
"""
Extract decisive S001 income-engine numbers (Parts A–C).

Reads cycle-level data (cached pickle from a prior measure, or rebuilds once).
"""

from __future__ import annotations

import argparse
import math
import pickle
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s001_income_engine as eng  # noqa: E402
import options_trades as ot  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
CYCLES_CACHE = _BACKTEST / "cache" / "s001_income_cycles.pkl"
N_CONFIGS = eng.N_CONFIGS
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED
BOOTSTRAP_N = eng.BOOTSTRAP_N

# Live-equivalent: 2DTE, 25% of ATM straddle (mode B25), conservative maker
LIVE_DTE = 2
LIVE_STRIKE = "B25"
LIVE_FILL = "maker"

SCENARIO_PCTS = (
    -30, -25, -20, -15, -10, -7, -5, -3, -1, 0, 1, 3, 5, 7, 10, 15, 20, 25, 30
)


def cycles_per_year(n_cycles: int, day_span: int) -> float:
    if day_span <= 0:
        return float("nan")
    return n_cycles * (365.25 / day_span)


def annualize_sortino(per_cycle_sortino: float, cycles_yr: float) -> float:
    """Sharpe-style: Sortino_ann ≈ Sortino_per_cycle * sqrt(cycles_per_year)."""
    if not math.isfinite(per_cycle_sortino) or cycles_yr <= 0:
        return float("nan")
    return per_cycle_sortino * math.sqrt(cycles_yr)


def load_or_measure(*, rebuild: bool) -> tuple[list[eng.CycleObs], int]:
    CYCLES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if not rebuild and CYCLES_CACHE.is_file():
        print(f"Loading cycles from {CYCLES_CACHE}", flush=True)
        with CYCLES_CACHE.open("rb") as f:
            obj = pickle.load(f)
        return obj["obs"], int(obj["day_span"])
    idx = eng.build_trade_index()
    surface = eng.load_surface_optional()
    obs, _drop, _audit = eng.measure_cycles(idx, surface)
    times, _ = ot.load_spot_1m()
    day_span = max(1, (times[-1] - times[0]) // 86400)
    with CYCLES_CACHE.open("wb") as f:
        pickle.dump({"obs": obs, "day_span": day_span}, f, protocol=4)
    print(f"Cached {len(obs):,} rows -> {CYCLES_CACHE}", flush=True)
    return obs, day_span


def group_stats(
    obs: list[eng.CycleObs], *, fill: str | None, min_n: int = 0
) -> list[eng.CfgStats]:
    filtered = obs
    if fill is not None:
        filtered = [o for o in obs if o.fill_package == fill]
    stats = eng.summarize_configs(
        filtered, use_settle=False, prints_only=False, do_bootstrap=True
    )
    if min_n > 0:
        stats = [s for s in stats if s.n >= min_n]
    return stats


def count_ci_above_zero(stats: list[eng.CfgStats]) -> int:
    return sum(1 for s in stats if s.ci_lo > 0)


def fmt_cfg(s: eng.CfgStats) -> str:
    return eng.cfg_label(s.key)


def decile_table_split(
    rows: list[eng.CycleObs],
) -> list[str]:
    """Net and leg P&L by |settlement move| decile; basket vs wings."""
    if len(rows) < 20:
        return ["  (insufficient cycles)"]
    sorted_rows = sorted(rows, key=lambda r: r.spot_move_abs)
    n = len(sorted_rows)
    lines: list[str] = []
    lines.append(
        f"  {'dec':>3} {'|move|':>14} {'n':>5} {'net_mean':>9} {'basket':>9} {'wings':>9}"
    )
    for d in range(10):
        lo = int(d * n / 10)
        hi = int((d + 1) * n / 10)
        chunk = sorted_rows[lo:hi]
        if not chunk:
            continue
        moves = [c.spot_move_abs for c in chunk]
        nets = [c.net_no_settle for c in chunk]
        bsk = [c.basket_pnl for c in chunk]
        wng = [c.wings_pnl for c in chunk]
        lines.append(
            f"  D{d} [{moves[0]:.0f},{moves[-1]:.0f}] {len(chunk):5d} "
            f"{statistics.mean(nets):9.2f} {statistics.mean(bsk):9.2f} {statistics.mean(wng):9.2f}"
        )
    return lines


def filter_family(
    obs: list[eng.CycleObs],
    *,
    dte: int,
    strike: str,
    fill: str,
    wing: float | None,
    entry_hhmm: str | None = None,
) -> list[eng.CycleObs]:
    out = []
    for o in obs:
        if o.short_dte != dte or o.strike_mode != strike or o.fill_package != fill:
            continue
        if o.wing_points != wing:
            continue
        if entry_hhmm is not None and o.entry_hhmm != entry_hhmm:
            continue
        out.append(o)
    return out


def median_cycle(rows: list[eng.CycleObs]) -> eng.CycleObs:
    rows = sorted(rows, key=lambda r: r.spot_entry)
    return rows[len(rows) // 2]


def settle_pnl_at_spot(
    *,
    spot_entry: float,
    spot_settle: float,
    sc_k: float,
    sp_k: float,
    sc_px: float,
    sp_px: float,
    wing_pts: float | None,
    wing_c_k: float | None,
    wing_p_k: float | None,
    wc_px: float,
    wp_px: float,
    qty_lots: int = eng.BASKET_QTY_LOTS,
    include_settle_fee: bool = False,
) -> tuple[float, float, float]:
    """Returns (net, basket_gross-fees, wings_gross-fees) approx net no settle unless flag."""
    sc_i = eng.call_intrinsic(spot_settle, sc_k)
    sp_i = eng.put_intrinsic(spot_settle, sp_k)
    bq = qty_lots
    basket_pnl = eng.cash_pnl(sc_px, sc_i, bq, is_long=False) + eng.cash_pnl(
        sp_px, sp_i, bq, is_long=False
    )
    entry_fees = eng.option_fee(sc_px, spot_entry, bq) + eng.option_fee(
        sp_px, spot_entry, bq
    )
    settle_fees = eng.option_fee(sc_i, spot_settle, bq) + eng.option_fee(
        sp_i, spot_settle, bq
    )
    wings_pnl = 0.0
    if wing_pts is not None and wing_c_k is not None and wing_p_k is not None:
        wc_i = eng.call_intrinsic(spot_settle, wing_c_k)
        wp_i = eng.put_intrinsic(spot_settle, wing_p_k)
        wings_pnl = eng.cash_pnl(wc_px, wc_i, bq, is_long=True) + eng.cash_pnl(
            wp_px, wp_i, bq, is_long=True
        )
        entry_fees += eng.option_fee(wc_px, spot_entry, bq) + eng.option_fee(
            wp_px, spot_entry, bq
        )
        settle_fees += eng.option_fee(wc_i, spot_settle, bq) + eng.option_fee(
            wp_i, spot_settle, bq
        )
    gross = basket_pnl + wings_pnl
    net = gross - entry_fees
    if include_settle_fee:
        net -= settle_fees
    return net, basket_pnl - entry_fees * 0.5, wings_pnl  # rough split for display


def deterministic_wing_valuation(
    rep: eng.CycleObs,
    wing_dist: float,
    idx: eng.TradeIndex,
    wing_median_prices: tuple[float, float] | None,
) -> dict[str, Any]:
    """Grid P&L wings OFF vs ON; rep is wings-OFF row for short strikes/premiums."""
    assert rep.wing_points is None
    S0 = rep.spot_entry
    sc_k, sp_k = rep.short_call_k, rep.short_put_k
    sc_px, sp_px = rep.short_call.price, rep.short_put.price
    wk = eng.pick_wing_strikes(idx, rep.basket_expiry, sc_k, sp_k, wing_dist)
    if wk is None:
        return {"error": "no wing strikes"}
    wing_c_k, wing_p_k = wk
    if wing_median_prices is not None:
        wc_px, wp_px = wing_median_prices
    else:
        _long = eng.roles_for_package(rep.fill_package)[1]
        wc_f = eng.wing_fill_or_surface(
            idx, None, rep.basket_expiry, wing_c_k, "C", rep.entry_utc, _long
        )
        wp_f = eng.wing_fill_or_surface(
            idx, None, rep.basket_expiry, wing_p_k, "P", rep.entry_utc, _long
        )
        wc_px = wc_f.price if wc_f else 50.0
        wp_px = wp_f.price if wp_f else 50.0

    premium_short = (sc_px + sp_px) * eng.BASKET_QTY_LOTS * eng.CONTRACT_VALUE
    premium_wings = (wc_px + wp_px) * eng.BASKET_QTY_LOTS * eng.CONTRACT_VALUE

    grid: list[dict[str, Any]] = []
    nets_off: list[float] = []
    nets_on: list[float] = []
    insurance: list[float] = []

    for pct in SCENARIO_PCTS:
        S = S0 * (1.0 + pct / 100.0)
        n_off, _, _ = settle_pnl_at_spot(
            spot_entry=S0,
            spot_settle=S,
            sc_k=sc_k,
            sp_k=sp_k,
            sc_px=sc_px,
            sp_px=sp_px,
            wing_pts=None,
            wing_c_k=None,
            wing_p_k=None,
            wc_px=0,
            wp_px=0,
        )
        n_on, _, _ = settle_pnl_at_spot(
            spot_entry=S0,
            spot_settle=S,
            sc_k=sc_k,
            sp_k=sp_k,
            sc_px=sc_px,
            sp_px=sp_px,
            wing_pts=wing_dist,
            wing_c_k=wing_c_k,
            wing_p_k=wing_p_k,
            wc_px=wc_px,
            wp_px=wp_px,
        )
        ins = n_on - n_off
        grid.append({"pct": pct, "net_off": n_off, "net_on": n_on, "insurance": ins})
        nets_off.append(n_off)
        nets_on.append(n_on)
        insurance.append(ins)

    max_loss_off = min(nets_off)
    max_loss_on = min(nets_on)
    breakeven = None
    for row in sorted(grid, key=lambda x: abs(x["pct"])):
        if row["insurance"] > 0 and abs(row["pct"]) >= 3:
            breakeven = row["pct"]
            break

    wing_cost_pct = (
        100.0 * premium_wings / premium_short if premium_short > 0 else float("nan")
    )
    bounded_max = wing_dist * eng.BASKET_QTY_LOTS * eng.CONTRACT_VALUE - (
        premium_short + premium_wings
    )

    return {
        "rep_date": str(rep.entry_date),
        "S0": S0,
        "sc_k": sc_k,
        "sp_k": sp_k,
        "wing_c_k": wing_c_k,
        "wing_p_k": wing_p_k,
        "sc_px": sc_px,
        "sp_px": sp_px,
        "wc_px": wc_px,
        "wp_px": wp_px,
        "premium_short_usd": premium_short,
        "premium_wings_usd": premium_wings,
        "grid": grid,
        "max_loss_off": max_loss_off,
        "max_loss_on": max_loss_on,
        "max_loss_off_x_prem": max_loss_off / premium_short if premium_short else float("nan"),
        "max_loss_on_x_prem": max_loss_on / premium_short if premium_short else float("nan"),
        "bounded_max_loss_approx": bounded_max,
        "wing_cost_pct": wing_cost_pct,
        "breakeven_pct": breakeven,
    }


def hand_check_lines(val: dict[str, Any]) -> list[str]:
    lines = ["--- Part C hand audit (one family, line by line) ---"]
    lines.append(
        f"Rep entry {val['rep_date']} S0={val['S0']:.2f} shorts K={val['sc_k']:.0f}/"
        f"{val['sp_k']:.0f} px={val['sc_px']:.2f}/{val['sp_px']:.2f}"
    )
    lines.append(
        f"Wings dist -> K={val['wing_c_k']:.0f}/{val['wing_p_k']:.0f} "
        f"px={val['wc_px']:.2f}/{val['wp_px']:.2f}"
    )
    lines.append(f"Premium collected short=${val['premium_short_usd']:.2f} wings=${val['premium_wings_usd']:.2f}")
    for row in val["grid"]:
        if row["pct"] in (-10, -5, 0, 5, 10, 20):
            S = val["S0"] * (1 + row["pct"] / 100)
            lines.append(
                f"  move {row['pct']:+d}% S={S:.0f} net_off={row['net_off']:.4f} "
                f"net_on={row['net_on']:.4f} insurance={row['insurance']:.4f}"
            )
    lines.append(
        f"max_loss OFF={val['max_loss_off']:.2f} ({val['max_loss_off_x_prem']:.2f}x prem) "
        f"ON={val['max_loss_on']:.2f} ({val['max_loss_on_x_prem']:.2f}x prem)"
    )
    return lines


def empirical_insurance(
    obs: list[eng.CycleObs],
    *,
    dte: int,
    strike: str,
    fill: str,
    wing_dist: float,
    exclude_crash_dates: set[date] | None = None,
) -> dict[str, Any]:
    """Match OFF/ON pairs by (entry_date, entry_hhmm, strike, fill, dte)."""
    off_map: dict[tuple[Any, ...], eng.CycleObs] = {}
    on_map: dict[tuple[Any, ...], eng.CycleObs] = {}
    for o in obs:
        if o.short_dte != dte or o.strike_mode != strike or o.fill_package != fill:
            continue
        key = (o.entry_date, o.entry_hhmm)
        if exclude_crash_dates and o.entry_date in exclude_crash_dates:
            continue
        if o.wing_points is None:
            off_map[key] = o
        elif o.wing_points == wing_dist:
            on_map[key] = o
    keys = sorted(set(off_map) & set(on_map))
    moves: list[float] = []
    payoffs: list[float] = []
    for k in keys:
        a, b = off_map[k], on_map[k]
        move_pct = (a.spot_settle / a.spot_entry - 1.0) * 100.0
        moves.append(move_pct)
        payoffs.append(b.net_no_settle - a.net_no_settle)
    if not payoffs:
        return {"n": 0}
    # Scenario freq: |move| >= threshold
    freqs: dict[int, float] = {}
    n = len(moves)
    for thr in (3, 5, 7, 10, 15, 20, 25, 30):
        freqs[thr] = sum(1 for m in moves if abs(m) >= thr) / n
    return {
        "n": n,
        "mean_insurance_usd": statistics.mean(payoffs),
        "freq_abs_move": freqs,
        "moves": moves,
        "payoffs": payoffs,
    }


def crash_dates_from_moves(obs: list[eng.CycleObs], dte: int, top_n: int = 2) -> set[date]:
    """Two days with largest |settlement move| for live family wings OFF."""
    rows = filter_family(obs, dte=dte, strike=LIVE_STRIKE, fill=LIVE_FILL, wing=None)
    by_date: dict[date, float] = {}
    for r in rows:
        pct = abs(r.spot_settle / r.spot_entry - 1.0)
        by_date[r.entry_date] = max(by_date.get(r.entry_date, 0.0), pct)
    worst = sorted(by_date.items(), key=lambda x: -x[1])[:top_n]
    return {d for d, _ in worst}


def write_sweep_configs_csv(stats: list[eng.CfgStats]) -> Path:
    """
    Per-config rows for distribution audits.
    Columns match cfg_key: time_of_day, dte, strike_mode, wing, fill_package.
    This income sweep has no adjustments — column adjustment_setting is always 'none'.
    """
    import csv

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "s001_sweep_configs.csv"
    fields = [
        "time_of_day",
        "dte",
        "strike_mode",
        "adjustment_setting",
        "wing",
        "fill_package",
        "n",
        "mean",
        "median",
        "ci_lo",
        "ci_hi",
        "p5",
        "worst",
        "worst_date",
        "sortino_per_cycle",
        "mdd",
        "surf_wing_pct",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in stats:
            hhmm, dte, mode, wing, fill = s.key
            w.writerow(
                {
                    "time_of_day": hhmm,
                    "dte": int(dte),
                    "strike_mode": mode,
                    "adjustment_setting": "none",
                    "wing": "OFF" if wing is None else str(int(wing)),
                    "fill_package": fill,
                    "n": s.n,
                    "mean": f"{s.mean:.6f}",
                    "median": f"{s.median:.6f}",
                    "ci_lo": f"{s.ci_lo:.6f}",
                    "ci_hi": f"{s.ci_hi:.6f}",
                    "p5": f"{s.p5:.6f}",
                    "worst": f"{s.worst:.6f}",
                    "worst_date": s.worst_date,
                    "sortino_per_cycle": (
                        f"{s.sortino:.6f}" if math.isfinite(s.sortino) else "nan"
                    ),
                    "mdd": f"{s.mdd:.6f}",
                    "surf_wing_pct": (
                        f"{100.0 * s.n_surface_wing / s.n:.2f}" if s.n else "0"
                    ),
                }
            )
    return path


def write_report(
    obs: list[eng.CycleObs],
    day_span: int,
    stats_all: list[eng.CfgStats],
    stats_maker: list[eng.CfgStats],
    stats_maker_n300: list[eng.CfgStats],
    part_c: dict[str, Any],
    runtime_s: float,
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"s001_income_decisive_{stamp}.txt"
    lines: list[str] = []

    lines.append("=== S001 INCOME ENGINE — DECISIVE NUMBERS ===")
    lines.append(f"runtime_s: {runtime_s:.1f}")
    lines.append("")

    # PART A
    lines.append("=" * 72)
    lines.append("PART A — THREE QUESTIONS")
    lines.append("=" * 72)
    lines.append("A1. SORTINO CONVENTION")
    lines.append(
        "  Per-cycle Sortino = mean(net) / sqrt(mean(min(net,0)^2)) on USD P&L per "
        "cycle (one entry held to settlement). NOT annualized in the engine report."
    )
    lines.append(
        "  Annualized Sortino (for comparison) = Sortino_per_cycle * sqrt(cycles_per_year)."
    )
    lines.append(
        f"  Sample calendar span ≈ {day_span} days; typical 0DTE config n≈386 => "
        f"cycles/year ≈ {cycles_per_year(386, day_span):.0f} => sqrt ≈ "
        f"{math.sqrt(cycles_per_year(386, day_span)):.2f}."
    )
    lines.append("  Example: Sortino_per_cycle=0.15 with ~365 cycles/yr => Sortino_ann ≈ 2.9.")
    lines.append("")
    lines.append("  Top 10 by Sortino (per-cycle) vs annualized — ALL fills:")
    top_so = sorted(
        stats_all,
        key=lambda s: (-s.sortino if math.isfinite(s.sortino) else float("-inf")),
    )[:10]
    for i, s in enumerate(top_so, 1):
        cpy = cycles_per_year(s.n, day_span)
        so_a = annualize_sortino(s.sortino, cpy)
        lines.append(
            f"    {i:2d}. n={s.n:4d} Sortino/cycle={s.sortino:.4f} Sortino_ann={so_a:.2f} "
            f"mean={s.mean:.2f} ci=[{s.ci_lo:.2f},{s.ci_hi:.2f}]  {fmt_cfg(s)}"
        )
    lines.append("")

    lines.append("A2. MAKER-ONLY LEADERBOARD (conservative fills)")
    by_mean = sorted(stats_maker, key=lambda s: -s.mean)[:25]
    by_sort = sorted(
        stats_maker,
        key=lambda s: (-s.sortino if math.isfinite(s.sortino) else float("-inf")),
    )[:25]
    lines.append("  By MEAN (top 25):")
    for s in by_mean:
        lines.append(
            f"    n={s.n:4d} mean={s.mean:.2f} ci=[{s.ci_lo:.2f},{s.ci_hi:.2f}] "
            f"Sortino/cycle={s.sortino:.3f}  {fmt_cfg(s)}"
        )
    lines.append("  By SORTINO per-cycle (top 25):")
    for s in by_sort:
        cpy = cycles_per_year(s.n, day_span)
        lines.append(
            f"    n={s.n:4d} Sortino/cycle={s.sortino:.3f} Sortino_ann="
            f"{annualize_sortino(s.sortino, cpy):.2f} mean={s.mean:.2f} "
            f"ci=[{s.ci_lo:.2f},{s.ci_hi:.2f}]  {fmt_cfg(s)}"
        )
    lines.append("")

    n_all = len(stats_all)
    n_clear_all = count_ci_above_zero(stats_all)
    chance_all = n_all * 0.05
    n_maker300 = len(stats_maker_n300)
    n_clear_m = count_ci_above_zero(stats_maker_n300)
    chance_m = n_maker300 * 0.05

    lines.append("A3. HEADLINE — bootstrap 95% CI lower bound > 0 (mean net USD/cycle)")
    lines.append(f"  bootstrap_n={BOOTSTRAP_N}  seed={BOOTSTRAP_SEED}")
    lines.append(
        f"  ALL {n_all} configs: {n_clear_all} with ci_lo>0  vs  chance@5% = "
        f"{n_all} x 0.05 = {chance_all:.1f}"
    )
    lines.append(
        f"  MAKER only, n>=300: {n_clear_m} with ci_lo>0  vs  chance = "
        f"{n_maker300} x 0.05 = {chance_m:.1f}"
    )
    if n_clear_all <= chance_all * 1.5:
        lines.append(
            "  VERDICT A3: Count is in line with multiple-testing chance — no strong "
            "evidence of positive mean edge across the sweep."
        )
    else:
        lines.append(
            "  VERDICT A3: Count exceeds chance expectation — some configs may have "
            "real positive mean (check maker n>=300 subset)."
        )
    lines.append("")

    # PART B
    lines.append("=" * 72)
    lines.append("PART B — |SETTLEMENT MOVE| DECILES (net vs basket vs wings leg P&L)")
    lines.append("=" * 72)
    live_off = filter_family(
        obs, dte=LIVE_DTE, strike=LIVE_STRIKE, fill=LIVE_FILL, wing=None
    )
    live_on = filter_family(
        obs, dte=LIVE_DTE, strike=LIVE_STRIKE, fill=LIVE_FILL, wing=2000.0
    )
    lines.append(f"Live-equivalent: dte={LIVE_DTE} {LIVE_STRIKE} {LIVE_FILL} (all entry times pooled)")
    lines.append("  WINGS OFF:")
    lines.extend(decile_table_split(live_off))
    lines.append("  WINGS 2000:")
    lines.extend(decile_table_split(live_on))
    lines.append("")
    # Maker leaderboard configs: top 3 by mean among maker n>=300
    leaders = sorted(
        [s for s in stats_maker if s.n >= 300], key=lambda s: -s.mean
    )[:3]
    for s in leaders:
        hhmm, dte, mode, wing, fill = s.key
        rows = filter_family(
            obs,
            dte=int(dte),
            strike=str(mode),
            fill=str(fill),
            wing=wing if wing is not None else None,
        )
        lines.append(f"Maker leaderboard config: {fmt_cfg(s)}")
        lines.extend(decile_table_split(rows))
        lines.append("")

    # PART C
    lines.append("=" * 72)
    lines.append("PART C — DETERMINISTIC WING VALUATION (arithmetic, no CI)")
    lines.append("=" * 72)
    val = part_c.get("grid_val")
    if val and "error" not in val:
        lines.append(
            f"Family: {LIVE_STRIKE} dte={LIVE_DTE} {LIVE_FILL} wing={part_c.get('wing_dist')} "
            f"qty={eng.BASKET_QTY_LOTS} lots"
        )
        lines.append(
            f"Max loss on grid OFF={val['max_loss_off']:.2f} USD "
            f"({val['max_loss_off_x_prem']:.2f}x short premium collected; "
            f"theoretically unbounded beyond grid); "
            f"ON={val['max_loss_on']:.2f} USD ({val['max_loss_on_x_prem']:.2f}x). "
            f"Wing distance caps tail: ~{part_c.get('wing_dist', 0):.0f} pts * qty ≈ "
            f"{float(part_c.get('wing_dist', 0)) * eng.BASKET_QTY_LOTS * eng.CONTRACT_VALUE:.2f} USD scale."
        )
        lines.append(f"Wing cost = {val['wing_cost_pct']:.1f}% of short premium collected.")
        lines.append(f"Break-even |move| (insurance>0, first >=5% grid): {val['breakeven_pct']}%")
        lines.append("Insurance payoff (net_on - net_off) by scenario:")
        for row in val["grid"]:
            lines.append(
                f"  move {row['pct']:+3d}%: insurance={row['insurance']:+.2f} USD "
                f"(off={row['net_off']:+.2f} on={row['net_on']:+.2f})"
            )
        lines.extend(hand_check_lines(val))
    lines.append("")
    for dte in (0, 1, 2):
        emp = part_c.get(f"emp_dte{dte}", {})
        if emp.get("n", 0) == 0:
            continue
        lines.append(f"Empirical insurance (paired OFF/ON), dte={dte} n={emp['n']}:")
        lines.append(f"  mean insurance USD/cycle = {emp['mean_insurance_usd']:.4f}")
        for thr, fr in sorted(emp.get("freq_abs_move", {}).items()):
            lines.append(f"  P(|move|>={thr}%) = {100*fr:.2f}%")
    crash = part_c.get("crash_dates", set())
    emp_ex = part_c.get("emp_ex_crash", {})
    if emp_ex.get("n"):
        lines.append(
            f"Excluding crash days {[str(d) for d in sorted(crash)]}: mean insurance = "
            f"{emp_ex['mean_insurance_usd']:.4f} (n={emp_ex['n']}) vs full sample "
            f"{part_c.get('emp_full_mean', 0):.4f} — sensitive to tail days."
        )
    lines.append("")
    lines.append(f"runtime_s: {runtime_s:.1f}")

    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (RESULTS_DIR / "s001_income_decisive_latest.txt").write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-cycles", action="store_true")
    ap.add_argument(
        "--csv-only",
        action="store_true",
        help="From cycle cache: bootstrap configs and write s001_sweep_configs.csv only",
    )
    args = ap.parse_args(argv)
    t0 = time.time()

    obs, day_span = load_or_measure(rebuild=args.rebuild_cycles)
    print(f"Cycles: {len(obs):,}  day_span={day_span}", flush=True)

    print("Bootstrap all configs...", flush=True)
    stats_all = eng.summarize_configs(
        obs, use_settle=False, prints_only=False, do_bootstrap=True
    )
    csv_path = write_sweep_configs_csv(stats_all)
    print(f"Wrote {csv_path} ({len(stats_all)} rows)", flush=True)
    n_clear = count_ci_above_zero(stats_all)
    print(f"ci_lo>0 count={n_clear}/{len(stats_all)}", flush=True)

    if args.csv_only:
        print(f"runtime_s={time.time() - t0:.1f}", flush=True)
        return 0

    stats_maker = [s for s in stats_all if s.key[4] == "maker"]
    stats_maker_n300 = [s for s in stats_maker if s.n >= 300]

    idx = eng.build_trade_index()
    rep_rows = filter_family(
        obs, dte=LIVE_DTE, strike=LIVE_STRIKE, fill=LIVE_FILL, wing=None
    )
    rep = median_cycle(rep_rows) if rep_rows else obs[0]
    wing_rows = filter_family(
        obs, dte=LIVE_DTE, strike=LIVE_STRIKE, fill=LIVE_FILL, wing=2000.0
    )
    wc_med = statistics.median(
        [r.wing_call.price for r in wing_rows if r.wing_call is not None]
    )
    wp_med = statistics.median(
        [r.wing_put.price for r in wing_rows if r.wing_put is not None]
    )
    print("Part C grid...", flush=True)
    grid_val = deterministic_wing_valuation(
        rep, 2000.0, idx, (wc_med, wp_med)
    )

    crash = crash_dates_from_moves(obs, LIVE_DTE, top_n=2)
    emp2 = empirical_insurance(
        obs, dte=LIVE_DTE, strike=LIVE_STRIKE, fill=LIVE_FILL, wing_dist=2000.0
    )
    emp2_ex = empirical_insurance(
        obs,
        dte=LIVE_DTE,
        strike=LIVE_STRIKE,
        fill=LIVE_FILL,
        wing_dist=2000.0,
        exclude_crash_dates=crash,
    )
    part_c = {
        "wing_dist": 2000.0,
        "grid_val": grid_val,
        "crash_dates": crash,
        "emp_dte0": empirical_insurance(
            obs, dte=0, strike=LIVE_STRIKE, fill=LIVE_FILL, wing_dist=2000.0
        ),
        "emp_dte1": empirical_insurance(
            obs, dte=1, strike=LIVE_STRIKE, fill=LIVE_FILL, wing_dist=2000.0
        ),
        "emp_dte2": emp2,
        "emp_ex_crash": emp2_ex,
        "emp_full_mean": emp2.get("mean_insurance_usd", 0),
    }

    runtime = time.time() - t0
    path = write_report(
        obs,
        day_span,
        stats_all,
        stats_maker,
        stats_maker_n300,
        part_c,
        runtime,
    )
    print(f"Wrote {path}", flush=True)
    print(
        f"A3: {count_ci_above_zero(stats_all)}/{len(stats_all)} ci_lo>0 "
        f"(chance {len(stats_all)*0.05:.1f}); "
        f"maker n>=300: {count_ci_above_zero(stats_maker_n300)}/{len(stats_maker_n300)} "
        f"(chance {len(stats_maker_n300)*0.05:.1f})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
