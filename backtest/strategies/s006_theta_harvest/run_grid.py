#!/usr/bin/env python3
"""S006 Daily Theta Harvest — configurable combo grid runner."""

from __future__ import annotations

import argparse
import csv
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
from backtest.harness.mark_cache import get_mark_cache  # noqa: E402
from backtest.harness.registry import write_registry  # noqa: E402
from backtest.strategies.s006_theta_harvest.strategy import (  # noqa: E402
    BOOTSTRAP_N,
    BOOTSTRAP_SEED,
    DEFAULT_CUTOFF_HOUR,
    DEFAULT_CUTOFF_MINUTE,
    DEFAULT_EXPIRE_PROT_AT_CUTOFF,
    DEFAULT_MAX_DD_PCT,
    DEFAULT_PROTECTION_OFFSET,
    DEFAULT_PROTECTION_RATIO,
    DEFAULT_QTY_SHORT,
    DEFAULT_SHORT_DTE,
    DEFAULT_SHORT_OFFSET,
    DEFAULT_TARGET_PCT,
    S006ThetaHarvestStrategy,
    default_params,
    parse_cutoff_time,
    parse_max_dd_pct,
)

logger = logging.getLogger("s006_grid")
RUNS_DIR = Path(__file__).resolve().parent / "runs"

DEFAULT_PROTECTION_RATIOS = (2.0, 3.0, 4.0, 5.0)
DEFAULT_TARGET_PCTS = (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0)
DEFAULT_MAX_DD_PCTS: tuple[float | None, ...] = (10.0, 20.0, None)
DEFAULT_QTY_SHORTS = (DEFAULT_QTY_SHORT,)
DEFAULT_SHORT_OFFSETS = (DEFAULT_SHORT_OFFSET,)
DEFAULT_PROTECTION_OFFSETS = (DEFAULT_PROTECTION_OFFSET,)
DEFAULT_CUTOFF_TIMES = (f"{DEFAULT_CUTOFF_HOUR:02d}:{DEFAULT_CUTOFF_MINUTE:02d}",)
DEFAULT_EXPIRE_FLAGS = (DEFAULT_EXPIRE_PROT_AT_CUTOFF,)
DEFAULT_SHORT_DTES = (DEFAULT_SHORT_DTE,)
DEFAULT_ENTRY_MODE = "fixed"
DEFAULT_ENTRY_TIMES = ("0900",)
DEFAULT_ENTRY_WINDOW = "0900-1500"


def _parse_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def _parse_str_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_hhmm(s: str) -> tuple[int, int]:
    s = s.strip().replace(":", "")
    if len(s) == 3:
        s = "0" + s
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f"bad HHMM: {s!r}")
    return int(s[:2]), int(s[2:])


def _parse_entry_window(s: str) -> tuple[str, str]:
    s = s.strip().replace(" ", "")
    if "-" not in s:
        raise ValueError(f"bad --entry-window {s!r}, want HHMM-HHMM")
    a, b = s.split("-", 1)
    h0, m0 = _parse_hhmm(a)
    h1, m1 = _parse_hhmm(b)
    return f"{h0:02d}:{m0:02d}", f"{h1:02d}:{m1:02d}"


def make_arm(
    *,
    qty: int,
    pr: float,
    tp: float,
    dd: float | None,
    off: float,
    po: float,
    cut_h: int,
    cut_m: int,
    expire: bool,
    short_dte: int,
    entry_mode: str = "fixed",
    entry_hhmm: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> str:
    dd_s = "none" if dd is None else f"{dd:g}"
    arm = (
        f"q{qty}_pr{pr:g}_tp{tp:g}_dd{dd_s}_off{off:g}_po{po:g}_"
        f"cut{cut_h:02d}{cut_m:02d}_sdte{int(short_dte)}"
    )
    if not expire:
        arm += "_nox"
    em = entry_mode.lower()
    arm += f"_em{em}"
    if em == "fixed":
        hhmm = entry_hhmm or "0900"
        arm += f"_e{hhmm}"
    else:
        ws = (window_start or "09:00").replace(":", "")
        we = (window_end or "15:00").replace(":", "")
        arm += f"_w{ws}-{we}"
    return arm


def build_combos(
    *,
    qty_shorts: list[int],
    protection_ratios: list[float],
    target_pcts: list[float],
    max_dd_pcts: list[float | None],
    short_offsets: list[float],
    protection_offsets: list[float],
    cutoff_times: list[str],
    expire_flags: list[bool],
    short_dtes: list[int],
    entry_mode: str = "fixed",
    entry_times: list[str] | None = None,
    entry_window: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    em = entry_mode.lower()
    entry_times = entry_times or list(DEFAULT_ENTRY_TIMES)
    if entry_window is None:
        entry_window = _parse_entry_window(DEFAULT_ENTRY_WINDOW)
    ws, we = entry_window

    entry_axis: list[dict[str, Any]]
    if em == "fixed":
        entry_axis = []
        for et in entry_times:
            hh, mm = _parse_hhmm(et)
            entry_axis.append(
                {
                    "entry_mode": "fixed",
                    "entry_hour": hh,
                    "entry_minute": mm,
                    "entry_hhmm": f"{hh:02d}{mm:02d}",
                    "entry_window_start": ws,
                    "entry_window_end": we,
                }
            )
    else:
        entry_axis = [
            {
                "entry_mode": "vwap",
                "entry_hour": int(ws[:2]),
                "entry_minute": int(ws[3:5]),
                "entry_hhmm": None,
                "entry_window_start": ws,
                "entry_window_end": we,
            }
        ]

    for qty in qty_shorts:
        for pr in protection_ratios:
            for tp in target_pcts:
                for dd in max_dd_pcts:
                    for off in short_offsets:
                        for po in protection_offsets:
                            for ct in cutoff_times:
                                hh, mm = parse_cutoff_time(ct)
                                for ex in expire_flags:
                                    for sdte in short_dtes:
                                        for ea in entry_axis:
                                            arm = make_arm(
                                                qty=qty,
                                                pr=pr,
                                                tp=tp,
                                                dd=dd,
                                                off=off,
                                                po=po,
                                                cut_h=hh,
                                                cut_m=mm,
                                                expire=ex,
                                                short_dte=sdte,
                                                entry_mode=ea["entry_mode"],
                                                entry_hhmm=ea.get("entry_hhmm"),
                                                window_start=ea.get(
                                                    "entry_window_start"
                                                ),
                                                window_end=ea.get("entry_window_end"),
                                            )
                                            out.append(
                                                {
                                                    "qty_short": qty,
                                                    "protection_ratio": pr,
                                                    "target_pct": tp,
                                                    "max_dd_pct": dd,
                                                    "short_offset": off,
                                                    "protection_offset": po,
                                                    "cutoff_hour": hh,
                                                    "cutoff_minute": mm,
                                                    "expire_protection_at_cutoff": ex,
                                                    "short_dte": int(sdte),
                                                    "entry_mode": ea["entry_mode"],
                                                    "entry_hour": ea["entry_hour"],
                                                    "entry_minute": ea["entry_minute"],
                                                    "entry_window_start": ea[
                                                        "entry_window_start"
                                                    ],
                                                    "entry_window_end": ea[
                                                        "entry_window_end"
                                                    ],
                                                    "arm": arm,
                                                }
                                            )
    return out


def all_combos() -> list[dict[str, Any]]:
    return build_combos(
        qty_shorts=list(DEFAULT_QTY_SHORTS),
        protection_ratios=list(DEFAULT_PROTECTION_RATIOS),
        target_pcts=list(DEFAULT_TARGET_PCTS),
        max_dd_pcts=list(DEFAULT_MAX_DD_PCTS),
        short_offsets=list(DEFAULT_SHORT_OFFSETS),
        protection_offsets=list(DEFAULT_PROTECTION_OFFSETS),
        cutoff_times=list(DEFAULT_CUTOFF_TIMES),
        expire_flags=list(DEFAULT_EXPIRE_FLAGS),
        short_dtes=list(DEFAULT_SHORT_DTES),
        entry_mode=DEFAULT_ENTRY_MODE,
        entry_times=list(DEFAULT_ENTRY_TIMES),
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
        "===== S006 DAILY THETA HARVEST — COMBO GRID =====",
        f"window={window}",
        f"bootstrap n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED}",
        f"n_combos={len(rows)} elapsed_sec={elapsed:.1f}",
        "",
        (
            f"{'arm':<64} {'n':>4} {'b/d':>5} {'win%':>5} "
            f"{'mean/b':>8} {'med/b':>8} {'mean/d':>8} {'ci_lo':>8} {'ci_hi':>8} "
            f"{'mn_nc$':>7} {'mn_tgt$':>7} {'mae':>8} {'shPnl':>7} {'prPnl':>7} "
            f"{'noise':>10}  exits"
        ),
    ]
    for r in ranked:
        mix = ",".join(
            f"{k[:3]}={v:.0f}%" for k, v in (r.get("exit_mix") or {}).items()
        )
        lines.append(
            f"{str(r.get('arm','')):<64} "
            f"{int(r.get('n_baskets') or 0):4d} "
            f"{_fmt(r.get('baskets_per_day'), 2):>5} "
            f"{_fmt(r.get('win_pct'), 1):>5} "
            f"{_fmt(r.get('mean_net')):>8} "
            f"{_fmt(r.get('median_net')):>8} "
            f"{_fmt(r.get('mean_day')):>8} "
            f"{_fmt(r.get('ci_lo')):>8} "
            f"{_fmt(r.get('ci_hi')):>8} "
            f"{_fmt(r.get('mean_net_credit'), 3):>7} "
            f"{_fmt(r.get('mean_target_usd'), 3):>7} "
            f"{_fmt(r.get('mae_usd')):>8} "
            f"{_fmt(r.get('mean_shorts_pnl'), 3):>7} "
            f"{_fmt(r.get('mean_protection_pnl'), 3):>7} "
            f"{str(r.get('noise_vs_top') or ''):>10}  "
            f"{mix}"
        )
    lines.append("")
    lines.append("===== EXIT REASON DETAIL (top 5 arms) =====")
    for r in ranked[:5]:
        lines.append(
            f"{r.get('arm')}: mix={r.get('exit_mix')} "
            f"mean_net_by_reason={r.get('mean_net_by_exit_reason')} "
            f"target_pct={r.get('target_pct')} mean_target_usd={_fmt(r.get('mean_target_usd'))} "
            f"mean_net_credit={_fmt(r.get('mean_net_credit'))}"
        )
        w5 = r.get("worst5") or []
        bits = [
            f"{w.get('date')}:{w.get('exit_reason')}:{_fmt(w.get('net'), 3)}"
            for w in w5
        ]
        lines.append(f"  worst5: " + (" | ".join(bits) if bits else "(none)"))
        lines.append(
            f"  mae mean/med/worst={_fmt(r.get('mean_mae_usd'))}/"
            f"{_fmt(r.get('median_mae_usd'))}/{_fmt(r.get('mae_usd'))}"
        )
    lines.append("")
    lines.append("===== SKIP REASONS (per arm) =====")
    for r in ranked:
        skips = (r.get("skips") or {}).get("counts") or {}
        if not skips:
            lines.append(f"{r.get('arm')}: (none)")
            continue
        bits = [f"{k}={v}" for k, v in sorted(skips.items())]
        lines.append(f"{r.get('arm')}: " + ", ".join(bits))
    lines.append("")
    lines.append("===== 0DTE EXACT SHORT-STRIKE AVAILABILITY =====")
    for r in ranked:
        sa = r.get("strike_availability") or {}
        lines.append(
            f"{r.get('arm')}: exact strike mila = {_fmt(sa.get('exact_hit_pct'), 1)}%, "
            f"nahi mila = {_fmt(sa.get('exact_miss_pct'), 1)}%, "
            f"jab nahi mila to median fasla = {_fmt(sa.get('median_miss_gap_pts'), 0)} pts "
            f"(checks={sa.get('n_checks', 0)} hit={sa.get('exact_hit', 0)} "
            f"miss={sa.get('exact_miss', 0)})"
        )
    lines.append("")
    lines.append("===== VWAP FILTER CONTROL (signal days vs reject counterfactual) =====")
    for r in ranked:
        vc = r.get("vwap_control")
        if not vc:
            continue
        lines.append(
            f"{r.get('arm')}: signal_days n={vc.get('n_signal_days')} "
            f"mean={_fmt(vc.get('mean_net_signal_days'))} | "
            f"reject_cf n={vc.get('n_reject_days_with_cf')} "
            f"mean={_fmt(vc.get('mean_net_reject_cf_days'))}"
        )
    lines.append("")
    return lines


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = list(rows[0].keys())
    for r in rows[1:]:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


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
    print(f"S006 grid combo_count={len(combos)} window={d0}..{d1}", flush=True)
    logger.info("S006 grid n_combos=%d window=%s..%s", len(combos), d0, d1)

    rows: list[dict[str, Any]] = []
    basket_csv_rows: list[dict[str, Any]] = []
    intraday_csv_rows: list[dict[str, Any]] = []
    t0 = time.time()

    for i, combo in enumerate(combos, start=1):
        params = default_params()
        params.update(combo)
        strat = S006ThetaHarvestStrategy(params)
        ct0 = time.time()
        _cycles, _skips, stats = strat.run_window(
            d0, d1, store=store, spot_map=spot_map
        )
        basket_csv_rows.extend(list(stats.pop("basket_csv_rows", []) or []))
        intraday_csv_rows.extend(list(stats.pop("intraday_csv_rows", []) or []))
        row = {"arm": combo["arm"], **combo, **stats, "elapsed_sec": time.time() - ct0}
        rows.append(row)
        logger.info(
            "[%d/%d] %s n=%d mean/day=%s mean/b=%s elapsed=%.1fs",
            i,
            len(combos),
            combo["arm"],
            stats.get("n_baskets"),
            _fmt(stats.get("mean_day")),
            _fmt(stats.get("mean_net")),
            row["elapsed_sec"],
        )
        sa = stats.get("strike_availability") or {}
        hit_pct = sa.get("exact_hit_pct")
        miss_pct = sa.get("exact_miss_pct")
        med_gap = sa.get("median_miss_gap_pts")
        logger.info(
            "%s skips=%s | exact strike mila=%s%% nahi mila=%s%% "
            "median_miss_gap=%s pts",
            combo["arm"],
            (stats.get("skips") or {}).get("counts") or {},
            _fmt(hit_pct, 1),
            _fmt(miss_pct, 1),
            _fmt(med_gap, 0),
        )

    store.close()
    elapsed = time.time() - t0
    mark_ci_overlap_with_top(rows)
    window = f"{d0.isoformat()}..{d1.isoformat()}"
    lines = format_report(rows, window=window, elapsed=elapsed)

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{tag}_S006_{stage}_{stamp}" if tag else f"S006_{stage}_{stamp}"
    json_path = RUNS_DIR / f"{base}.json"
    md_path = RUNS_DIR / f"{base}.md"
    baskets_csv = RUNS_DIR / f"{base}_baskets.csv"
    intraday_csv = RUNS_DIR / f"{base}_intraday.csv"

    payload = {
        "strategy_id": "S006",
        "stage": stage,
        "tag": tag,
        "window": {"from": d0.isoformat(), "to": d1.isoformat()},
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(),
        "elapsed_sec": elapsed,
        "bootstrap": {"n": BOOTSTRAP_N, "seed": BOOTSTRAP_SEED},
        "n_combos": len(rows),
        "combos": rows,
        "baskets_csv": baskets_csv.name,
        "intraday_csv": intraday_csv.name,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_csv(baskets_csv, basket_csv_rows)
    _write_csv(intraday_csv, intraday_csv_rows)
    logger.info("wrote %s %s %s %s", json_path, md_path, baskets_csv, intraday_csv)
    logger.info("mark_cache %s", get_mark_cache().stats())

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
        "baskets_csv": str(baskets_csv),
        "intraday_csv": str(intraday_csv),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S006 Daily Theta Harvest grid")
    ap.add_argument("--stage", type=str, default="KILL")
    ap.add_argument("--from", dest="from_date", type=str, default=None)
    ap.add_argument("--to", dest="to_date", type=str, default=None)
    ap.add_argument("--start", dest="start_date", type=str, default=None, help="Alias for --from")
    ap.add_argument("--end", dest="end_date", type=str, default=None, help="Alias for --to")
    ap.add_argument("--tag", type=str, default=None)
    ap.add_argument("--qty-short", type=str, default=None, help="Comma list, default 100")
    ap.add_argument(
        "--protection-ratios",
        type=str,
        default=None,
        help="Comma list, default 2,3,4,5",
    )
    ap.add_argument(
        "--protection-ratio",
        type=str,
        default=None,
        help="Alias for --protection-ratios (supports 0=naked)",
    )
    ap.add_argument(
        "--target-pcts",
        type=str,
        default=None,
        help="Comma list %% of net credit, default 10..70",
    )
    ap.add_argument(
        "--max-dd-pcts",
        type=str,
        default=None,
        help="Comma list %% of $100 capital or none, default 10,20,none",
    )
    ap.add_argument(
        "--short-offset",
        type=str,
        default=None,
        help="Comma list points from ATM, default 2000",
    )
    ap.add_argument(
        "--protection-offsets",
        type=str,
        default=None,
        help="Comma list vs short strikes, default 0",
    )
    ap.add_argument(
        "--cutoff-times",
        type=str,
        default=None,
        help="Comma HH:MM IST, default 17:29",
    )
    ap.add_argument(
        "--expire-protection-at-cutoff",
        type=str,
        default=None,
        help="Comma true/false (default true). false = close longs at cutoff too",
    )
    ap.add_argument(
        "--short-dtes",
        type=str,
        default=None,
        help="Comma list short-leg DTE (default 1). Protection stays 0DTE.",
    )
    ap.add_argument(
        "--entry-mode",
        type=str,
        default="fixed",
        choices=["fixed", "vwap"],
        help="fixed | vwap (default fixed)",
    )
    ap.add_argument(
        "--entry-times",
        type=str,
        default=None,
        help="Comma HHMM list for fixed mode (default 0900)",
    )
    ap.add_argument(
        "--entry-window",
        type=str,
        default=None,
        help="HHMM-HHMM for vwap mode (default 0900-1500)",
    )
    args = ap.parse_args()

    from_s = args.from_date or args.start_date
    to_s = args.to_date or args.end_date
    if not from_s or not to_s:
        raise SystemExit("require --from/--to or --start/--end")

    qty_shorts = (
        [int(float(x)) for x in _parse_floats(args.qty_short)]
        if args.qty_short
        else list(DEFAULT_QTY_SHORTS)
    )
    pr_raw = args.protection_ratio or args.protection_ratios
    protection_ratios = (
        _parse_floats(pr_raw) if pr_raw else list(DEFAULT_PROTECTION_RATIOS)
    )
    target_pcts = (
        _parse_floats(args.target_pcts)
        if args.target_pcts
        else list(DEFAULT_TARGET_PCTS)
    )
    if args.max_dd_pcts:
        max_dd_pcts = [parse_max_dd_pct(x) for x in _parse_str_list(args.max_dd_pcts)]
    else:
        max_dd_pcts = list(DEFAULT_MAX_DD_PCTS)
    short_offsets = (
        _parse_floats(args.short_offset)
        if args.short_offset
        else list(DEFAULT_SHORT_OFFSETS)
    )
    protection_offsets = (
        _parse_floats(args.protection_offsets)
        if args.protection_offsets
        else list(DEFAULT_PROTECTION_OFFSETS)
    )
    cutoff_times = (
        _parse_str_list(args.cutoff_times)
        if args.cutoff_times
        else list(DEFAULT_CUTOFF_TIMES)
    )
    for ct in cutoff_times:
        parse_cutoff_time(ct)
    if args.expire_protection_at_cutoff:
        expire_flags = []
        for x in _parse_str_list(args.expire_protection_at_cutoff):
            xl = x.lower()
            if xl in ("1", "true", "yes", "on"):
                expire_flags.append(True)
            elif xl in ("0", "false", "no", "off"):
                expire_flags.append(False)
            else:
                raise SystemExit(f"bad --expire-protection-at-cutoff: {x!r}")
    else:
        expire_flags = list(DEFAULT_EXPIRE_FLAGS)
    short_dtes = (
        [max(1, int(float(x))) for x in _parse_floats(args.short_dtes)]
        if args.short_dtes
        else list(DEFAULT_SHORT_DTES)
    )
    entry_mode = str(args.entry_mode or "fixed").lower()
    entry_times = (
        _parse_str_list(args.entry_times)
        if args.entry_times
        else list(DEFAULT_ENTRY_TIMES)
    )
    entry_window = (
        _parse_entry_window(args.entry_window)
        if args.entry_window
        else _parse_entry_window(DEFAULT_ENTRY_WINDOW)
    )

    combos = build_combos(
        qty_shorts=qty_shorts,
        protection_ratios=protection_ratios,
        target_pcts=target_pcts,
        max_dd_pcts=max_dd_pcts,
        short_offsets=short_offsets,
        protection_offsets=protection_offsets,
        cutoff_times=cutoff_times,
        expire_flags=expire_flags,
        short_dtes=short_dtes,
        entry_mode=entry_mode,
        entry_times=entry_times,
        entry_window=entry_window,
    )
    d0 = date.fromisoformat(from_s)
    d1 = date.fromisoformat(to_s)
    out = run_grid(d0, d1, stage=args.stage.upper(), tag=args.tag, combos=combos)
    logger.info(
        "DONE combos=%d elapsed=%.1fs total_baskets=%d",
        len(out["rows"]),
        out["elapsed_sec"],
        sum(int(r.get("n_baskets") or 0) for r in out["rows"]),
    )


if __name__ == "__main__":
    main()
