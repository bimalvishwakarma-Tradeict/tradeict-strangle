#!/usr/bin/env python3
"""S005 Tent — configurable combo grid runner (defaults = phase-1 72)."""

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
    DEFAULT_BE_MULT,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_CUTOFF_HOUR,
    DEFAULT_CUTOFF_MINUTE,
    DEFAULT_PROTECTION_EXPIRY,
    DEFAULT_PROTECTION_OFFSET,
    DEFAULT_PROTECTION_RATIO,
    S005TentStrategy,
    default_params,
    parse_cutoff_time,
)

logger = logging.getLogger("s005_grid")

RUNS_DIR = Path(__file__).resolve().parent / "runs"

# Phase-1 defaults (unchanged behaviour when CLI omitted)
DEFAULT_EXPIRY_PAIRS = ((1, 0), (2, 1))
DEFAULT_QTY_SPLITS = ((10, 20), (15, 15), (20, 10))
DEFAULT_TARGET_PCTS = (5.0, 10.0, 15.0)
DEFAULT_SL_MULTS = (2.0, 3.0, 4.0, 5.0)
DEFAULT_PROTECTION_EXPIRIES = (DEFAULT_PROTECTION_EXPIRY,)
DEFAULT_PROTECTION_OFFSETS = (DEFAULT_PROTECTION_OFFSET,)
DEFAULT_PROTECTION_RATIOS = (DEFAULT_PROTECTION_RATIO,)
DEFAULT_BE_MULTS = (DEFAULT_BE_MULT,)
DEFAULT_CUTOFF_TIMES = (
    f"{DEFAULT_CUTOFF_HOUR:02d}:{DEFAULT_CUTOFF_MINUTE:02d}",
)
DEFAULT_COOLDOWNS = (DEFAULT_COOLDOWN_HOURS,)


def _parse_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def _parse_ints(s: str) -> list[int]:
    return [int(float(x.strip())) for x in s.split(",") if x.strip()]


def _parse_expiry_pairs(s: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("/")
        out.append((int(a), int(b)))
    return out


def _parse_qty_splits(s: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("/")
        out.append((int(a), int(b)))
    return out


def _parse_str_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def make_arm(
    *,
    sd: int,
    ld: int,
    qs: int,
    qg: int,
    tp: float,
    sl: float,
    pe: str,
    off: int,
    pr: float,
    be: float,
    cut_h: int,
    cut_m: int,
    cd: float,
) -> str:
    """Classic phase-1 arm when new axes are defaults; else append tokens."""
    base = f"s{sd}l{ld}_q{qs}_{qg}_tp{tp:g}_sl{sl:g}"
    extras: list[str] = []
    if pe != DEFAULT_PROTECTION_EXPIRY:
        extras.append(f"pe{pe}")
    if off != DEFAULT_PROTECTION_OFFSET:
        extras.append(f"off{off}")
    if abs(pr - DEFAULT_PROTECTION_RATIO) > 1e-9:
        extras.append(f"pr{pr:g}")
    if abs(be - DEFAULT_BE_MULT) > 1e-9:
        extras.append(f"be{be:g}")
    if (cut_h, cut_m) != (DEFAULT_CUTOFF_HOUR, DEFAULT_CUTOFF_MINUTE):
        extras.append(f"cut{cut_h:02d}{cut_m:02d}")
    if abs(cd - DEFAULT_COOLDOWN_HOURS) > 1e-9:
        extras.append(f"cd{cd:g}")
    if not extras:
        return base
    return base + "_" + "_".join(extras)


def build_combos(
    *,
    expiry_pairs: list[tuple[int, int]],
    qty_splits: list[tuple[int, int]],
    target_pcts: list[float],
    sl_mults: list[float],
    protection_expiries: list[str],
    protection_offsets: list[int],
    protection_ratios: list[float],
    be_mults: list[float],
    cutoff_times: list[str],
    cooldowns: list[float],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sd, ld in expiry_pairs:
        for qs, qg in qty_splits:
            for tp in target_pcts:
                for sl in sl_mults:
                    for pe in protection_expiries:
                        for off in protection_offsets:
                            for pr in protection_ratios:
                                for be in be_mults:
                                    for ct in cutoff_times:
                                        hh, mm = parse_cutoff_time(ct)
                                        for cd in cooldowns:
                                            arm = make_arm(
                                                sd=sd,
                                                ld=ld,
                                                qs=qs,
                                                qg=qg,
                                                tp=tp,
                                                sl=sl,
                                                pe=pe,
                                                off=off,
                                                pr=pr,
                                                be=be,
                                                cut_h=hh,
                                                cut_m=mm,
                                                cd=cd,
                                            )
                                            out.append(
                                                {
                                                    "short_dte": sd,
                                                    "long_dte": ld,
                                                    "qty_straddle": qs,
                                                    "qty_strangle": qg,
                                                    "target_pct": tp,
                                                    "sl_mult": sl,
                                                    "protection_expiry": pe,
                                                    "protection_offset": off,
                                                    "protection_ratio": pr,
                                                    "be_mult": be,
                                                    "cutoff_hour": hh,
                                                    "cutoff_minute": mm,
                                                    "cooldown_hours": cd,
                                                    "arm": arm,
                                                }
                                            )
    return out


def all_combos() -> list[dict[str, Any]]:
    """Phase-1 72-combo grid (defaults only)."""
    return build_combos(
        expiry_pairs=list(DEFAULT_EXPIRY_PAIRS),
        qty_splits=list(DEFAULT_QTY_SPLITS),
        target_pcts=list(DEFAULT_TARGET_PCTS),
        sl_mults=list(DEFAULT_SL_MULTS),
        protection_expiries=list(DEFAULT_PROTECTION_EXPIRIES),
        protection_offsets=list(DEFAULT_PROTECTION_OFFSETS),
        protection_ratios=list(DEFAULT_PROTECTION_RATIOS),
        be_mults=list(DEFAULT_BE_MULTS),
        cutoff_times=list(DEFAULT_CUTOFF_TIMES),
        cooldowns=list(DEFAULT_COOLDOWNS),
    )


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
            f"{'arm':<36} {'n':>4} {'b/d':>5} {'win%':>5} "
            f"{'mean/d':>8} {'ci_lo':>8} {'ci_hi':>8} "
            f"{'worst':>8} {'maxDD':>8} {'hold':>5} "
            f"{'tc%':>5} {'tcNet':>7} {'shPnl':>7} {'prPnl':>7} "
            f"{'maxL':>7} {'units':>5} {'d%':>6} {'noise':>10}  exits"
        ),
    ]
    for r in ranked:
        mix = ",".join(
            f"{k[:3]}={v:.0f}%" for k, v in (r.get("exit_mix") or {}).items()
        )
        lines.append(
            f"{str(r.get('arm','')):<36} "
            f"{int(r.get('n_baskets') or 0):4d} "
            f"{_fmt(r.get('baskets_per_day'), 2):>5} "
            f"{_fmt(r.get('win_pct'), 1):>5} "
            f"{_fmt(r.get('mean_day')):>8} "
            f"{_fmt(r.get('ci_lo')):>8} "
            f"{_fmt(r.get('ci_hi')):>8} "
            f"{_fmt(r.get('worst_net')):>8} "
            f"{_fmt(r.get('max_dd')):>8} "
            f"{_fmt(r.get('hold_med'), 2):>5} "
            f"{_fmt(r.get('time_cutoff_pct'), 1):>5} "
            f"{_fmt(r.get('time_cutoff_mean_net'), 3):>7} "
            f"{_fmt(r.get('mean_shorts_pnl'), 3):>7} "
            f"{_fmt(r.get('mean_protection_pnl'), 3):>7} "
            f"{_fmt(r.get('max_loss_per_basket'), 3):>7} "
            f"{int(r.get('basket_units_at_3pct') or 0):5d} "
            f"{_fmt(r.get('daily_net_pct_of_capital_at_size'), 2):>6} "
            f"{str(r.get('noise_vs_top') or ''):>10}  "
            f"{mix}"
        )
    lines.append("")
    lines.append("===== TIME_CUTOFF / PROTECTION (top 5 by mean/day) =====")
    for r in ranked[:5]:
        lines.append(
            f"{r.get('arm')}: tc%={_fmt(r.get('time_cutoff_pct'), 1)} "
            f"tc_mean_net={_fmt(r.get('time_cutoff_mean_net'), 4)} "
            f"shorts_pnl={_fmt(r.get('mean_shorts_pnl'), 4)} "
            f"protection_pnl={_fmt(r.get('mean_protection_pnl'), 4)}"
        )
    lines.append("")
    lines.append("===== WORST 5 BASKETS (per combo, top 5 arms) =====")
    for r in ranked[:5]:
        w5 = r.get("worst5") or []
        bits = [
            f"{w.get('date')}:{w.get('exit_reason')}:{_fmt(w.get('net'), 3)}"
            for w in w5
        ]
        lines.append(f"{r.get('arm')}: " + (" | ".join(bits) if bits else "(none)"))
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


def run_grid(
    d0: date,
    d1: date,
    *,
    stage: str = "KILL",
    tag: str | None = None,
    combos: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    spot_path = find_spot_csv()
    if spot_path is None:
        raise FileNotFoundError("No BTCUSD_1m CSV")
    spot_map = load_spot_map(spot_path)
    store = MarksStore()
    if combos is None:
        combos = all_combos()
    print(f"S005 grid combo_count={len(combos)} window={d0}..{d1}", flush=True)
    logger.info("S005 grid n_combos=%d window=%s..%s", len(combos), d0, d1)

    rows: list[dict[str, Any]] = []
    t0 = time.time()

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
    if tag:
        base = f"{tag}_S005_{stage}_{stamp}"
    else:
        base = f"S005_{stage}_{stamp}"
    json_path = RUNS_DIR / f"{base}.json"
    md_path = RUNS_DIR / f"{base}.md"
    payload = {
        "strategy_id": "S005",
        "stage": stage,
        "tag": tag,
        "window": {"from": d0.isoformat(), "to": d1.isoformat()},
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
        "elapsed_sec": elapsed,
        "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED},
        "n_combos": len(rows),
        "combos": rows,
        "note": "3-month exploration — CI overlap wale combos ko alag mat maano",
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("wrote %s and %s", json_path, md_path)

    meta_path = Path(__file__).resolve().parent / "registry_meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        top = max(rows, key=lambda r: float(r.get("mean_day") or float("-inf")))
        meta.setdefault("tests_done", []).append(
            {
                "stage": stage,
                "tag": tag,
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
    return {
        "rows": rows,
        "elapsed_sec": elapsed,
        "json": str(json_path),
        "md": str(md_path),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="S005 Tent combo grid (defaults = phase-1 72)"
    )
    ap.add_argument("--stage", type=str, default="KILL")
    ap.add_argument("--from", dest="from_date", type=str, required=True)
    ap.add_argument("--to", dest="to_date", type=str, required=True)
    ap.add_argument(
        "--tag",
        type=str,
        default=None,
        help="Prefix output files: runs/<tag>_S005_<stage>_...",
    )
    ap.add_argument(
        "--sl-mults",
        type=str,
        default=None,
        help="Comma list, default 2,3,4,5",
    )
    ap.add_argument(
        "--target-pcts",
        type=str,
        default=None,
        help="Comma list, default 5,10,15",
    )
    ap.add_argument(
        "--qty-splits",
        type=str,
        default=None,
        help="Comma list of straddle/strangle, default 10/20,15/15,20/10",
    )
    ap.add_argument(
        "--expiry-pairs",
        type=str,
        default=None,
        help="Comma list of short/long DTE, default 1/0,2/1",
    )
    ap.add_argument(
        "--protection-expiry",
        type=str,
        default=None,
        help="Comma list: calendar|same (default calendar)",
    )
    ap.add_argument(
        "--protection-offset",
        type=str,
        default=None,
        help="Comma list of OTM steps for long vs short strikes (default 0)",
    )
    ap.add_argument(
        "--protection-ratio",
        type=str,
        default=None,
        help="Comma list; long qty = ratio × total short (default 1.0)",
    )
    ap.add_argument(
        "--be-mult",
        type=str,
        default=None,
        help="Comma list; BE = ATM ± be_mult×straddle premium (default 1.0)",
    )
    ap.add_argument(
        "--cutoff-times",
        type=str,
        default=None,
        help="Comma list HH:MM IST on long expiry day (default 17:25)",
    )
    ap.add_argument(
        "--cooldowns",
        type=str,
        default=None,
        help="Comma list of hours after STOPLOSS (default 2)",
    )
    args = ap.parse_args()

    expiry_pairs = (
        _parse_expiry_pairs(args.expiry_pairs)
        if args.expiry_pairs
        else list(DEFAULT_EXPIRY_PAIRS)
    )
    qty_splits = (
        _parse_qty_splits(args.qty_splits)
        if args.qty_splits
        else list(DEFAULT_QTY_SPLITS)
    )
    target_pcts = (
        _parse_floats(args.target_pcts)
        if args.target_pcts
        else list(DEFAULT_TARGET_PCTS)
    )
    sl_mults = (
        _parse_floats(args.sl_mults) if args.sl_mults else list(DEFAULT_SL_MULTS)
    )
    protection_expiries = (
        [x.lower() for x in _parse_str_list(args.protection_expiry)]
        if args.protection_expiry
        else list(DEFAULT_PROTECTION_EXPIRIES)
    )
    for pe in protection_expiries:
        if pe not in ("calendar", "same"):
            raise SystemExit(f"invalid --protection-expiry value: {pe!r}")
    protection_offsets = (
        _parse_ints(args.protection_offset)
        if args.protection_offset
        else list(DEFAULT_PROTECTION_OFFSETS)
    )
    protection_ratios = (
        _parse_floats(args.protection_ratio)
        if args.protection_ratio
        else list(DEFAULT_PROTECTION_RATIOS)
    )
    be_mults = (
        _parse_floats(args.be_mult) if args.be_mult else list(DEFAULT_BE_MULTS)
    )
    cutoff_times = (
        _parse_str_list(args.cutoff_times)
        if args.cutoff_times
        else list(DEFAULT_CUTOFF_TIMES)
    )
    for ct in cutoff_times:
        parse_cutoff_time(ct)  # validate early
    cooldowns = (
        _parse_floats(args.cooldowns)
        if args.cooldowns
        else list(DEFAULT_COOLDOWNS)
    )

    combos = build_combos(
        expiry_pairs=expiry_pairs,
        qty_splits=qty_splits,
        target_pcts=target_pcts,
        sl_mults=sl_mults,
        protection_expiries=protection_expiries,
        protection_offsets=protection_offsets,
        protection_ratios=protection_ratios,
        be_mults=be_mults,
        cutoff_times=cutoff_times,
        cooldowns=cooldowns,
    )

    d0 = date.fromisoformat(args.from_date)
    d1 = date.fromisoformat(args.to_date)
    out = run_grid(
        d0, d1, stage=args.stage.upper(), tag=args.tag, combos=combos
    )
    logger.info(
        "DONE combos=%d elapsed=%.1fs total_baskets=%d",
        len(out["rows"]),
        out["elapsed_sec"],
        sum(int(r.get("n_baskets") or 0) for r in out["rows"]),
    )


if __name__ == "__main__":
    main()
