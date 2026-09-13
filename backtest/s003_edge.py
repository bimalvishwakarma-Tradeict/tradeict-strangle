#!/usr/bin/env python3
"""
S003 Phase 2.3 — Signal edge measurement vs random-entry baseline.

No trading, no costs — forward price behaviour after each LSR4 signal only.
"""

from __future__ import annotations

import argparse
import csv
import glob
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
HORIZON = 240
FWD_BARS = (5, 15, 30, 60, 120, 240)
MFE_MAE_WINDOWS = (30, 60, 120, 240)
HIT_LEVELS = (50, 100, 150, 200, 300, 400)
STOP_LEVELS = (30, 50, 80, 120)
TARGETS = (100, 150, 200, 300)
STOPS = (50, 59, 80, 120)
COST_HEDGE_OFF = 23.0
COST_HEDGE_ON = 53.0
DEFAULT_SEED = 20260913

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_DATA = Path(__file__).resolve().parent / "data_1m" / "BTCUSD_1m_20250913_20260913.csv"


def _latest_signals_csv() -> Path:
    matches = sorted(
        glob.glob(str(RESULTS_DIR / "s003_signals_*.csv")),
        key=lambda p: Path(p).stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError(f"No s003_signals_*.csv in {RESULTS_DIR}")
    return Path(matches[0])


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.mean(vals))


def _fmt(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"


def _pct(num: int, den: int) -> str:
    if den <= 0:
        return "n/a"
    return f"{100.0 * num / den:.1f}%"


def load_candles(path: Path) -> tuple[list[int], list[float], list[float], list[float], list[float]]:
    times: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            times.append(int(row["open_time_unix"]))
            opens.append(float(row["open"]))
            highs.append(float(row["high"]))
            lows.append(float(row["low"]))
            closes.append(float(row["close"]))
    return times, opens, highs, lows, closes


def load_signals(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            conf_dt = _parse_utc(row["confirm_candle_utc"])
            out.append(
                {
                    "confirm_unix": int(conf_dt.timestamp()),
                    "direction": str(row["direction"]).upper(),
                    "mode": str(row["mode"]).upper(),
                    "score": int(row["score"]),
                    "c_wick": row["c_wick"] in ("1", "true", "True"),
                    "c_volume": row["c_volume"] in ("1", "true", "True"),
                    "c_vwap_ext": row["c_vwap_ext"] in ("1", "true", "True"),
                    "c_rsi": row["c_rsi"] in ("1", "true", "True"),
                    "confirm_price": float(row["confirm_price"]),
                    "signal_extreme": float(row["signal_extreme"]),
                    "distance_points": float(row["distance_points"]),
                    "atr_at_arm": float(row["atr_at_arm"]),
                    "adx_at_signal": float(row["adx_at_signal"]),
                    "hour_ist": conf_dt.astimezone(IST).hour,
                }
            )
    return out


def compute_path_metrics(
    *,
    idx: int,
    direction: str,
    entry: float,
    extreme: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> dict[str, Any]:
    """
    Walk forward up to HORIZON bars from confirm index `idx`.
    Favourable moves are positive for both LONG and SHORT.
    """
    n = len(closes)
    long = direction == "LONG"
    max_fwd = n - 1 - idx
    row: dict[str, Any] = {
        "entry_idx": idx,
        "max_forward_bars": max_fwd,
        "tail_truncated": max_fwd < HORIZON,
    }

    def fav_adv(j: int) -> tuple[float, float]:
        if long:
            return highs[j] - entry, entry - lows[j]
        return entry - lows[j], highs[j] - entry

    # close-to-close forwards
    for b in FWD_BARS:
        key = f"fwd_{b}"
        if max_fwd < b:
            row[key] = None
        else:
            move = closes[idx + b] - entry
            row[key] = move if long else -move

    # MFE / MAE windows
    for w in MFE_MAE_WINDOWS:
        mfe_k, mae_k = f"mfe_{w}", f"mae_{w}"
        if max_fwd < 1:
            row[mfe_k] = None
            row[mae_k] = None
            continue
        upto = min(w, max_fwd)
        mfe = 0.0
        mae = 0.0
        for j in range(idx + 1, idx + 1 + upto):
            fav, adv = fav_adv(j)
            if fav > mfe:
                mfe = fav
            if adv > mae:
                mae = adv
        row[mfe_k] = mfe if max_fwd >= w or upto > 0 else None
        row[mae_k] = mae if max_fwd >= w or upto > 0 else None
        # If window not complete, still report partial MFE/MAE but mark — user said null for windows that do not fit
        if max_fwd < w:
            row[mfe_k] = None
            row[mae_k] = None

    # First-touch helpers over full available horizon (capped at 240)
    walk = min(HORIZON, max_fwd)
    hit_bars: dict[int, int | None] = {lv: None for lv in HIT_LEVELS}
    stop_bars: dict[int, int | None] = {lv: None for lv in STOP_LEVELS}
    breach_bars: int | None = None
    running_fav = 0.0
    running_adv = 0.0

    # Path outcomes for expectancy / before rates
    # path_hits[T][S] = points outcome or None if incomplete horizon and neither hit
    path_result: dict[tuple[int, int], float | None] = {
        (t, s): None for t in TARGETS for s in STOPS
    }
    hit100_before50: bool | None = None
    hit150_before59: bool | None = None
    path_done: set[tuple[int, int]] = set()
    flag_100_50_done = False
    flag_150_59_done = False

    for step in range(1, walk + 1):
        j = idx + step
        fav, adv = fav_adv(j)
        if fav > running_fav:
            running_fav = fav
        if adv > running_adv:
            running_adv = adv

        for lv in HIT_LEVELS:
            if hit_bars[lv] is None and running_fav >= lv:
                hit_bars[lv] = step
        for lv in STOP_LEVELS:
            if stop_bars[lv] is None and running_adv >= lv:
                stop_bars[lv] = step

        if breach_bars is None:
            if long and lows[j] < extreme:
                breach_bars = step
            elif (not long) and highs[j] > extreme:
                breach_bars = step

        # Adverse-first within the bar for target/stop races
        if not flag_100_50_done:
            if running_adv >= 50:
                hit100_before50 = False
                flag_100_50_done = True
            elif running_fav >= 100:
                hit100_before50 = True
                flag_100_50_done = True
        if not flag_150_59_done:
            if running_adv >= 59:
                hit150_before59 = False
                flag_150_59_done = True
            elif running_fav >= 150:
                hit150_before59 = True
                flag_150_59_done = True

        for t in TARGETS:
            for s in STOPS:
                key = (t, s)
                if key in path_done:
                    continue
                if running_adv >= s:
                    path_result[key] = -float(s)
                    path_done.add(key)
                elif running_fav >= t:
                    path_result[key] = float(t)
                    path_done.add(key)

    # Close unresolved paths at horizon if full 240 available
    if max_fwd >= HORIZON:
        close_move = closes[idx + HORIZON] - entry
        close_pts = close_move if long else -close_move
        for key, val in list(path_result.items()):
            if val is None:
                path_result[key] = close_pts
        if not flag_100_50_done:
            hit100_before50 = False
        if not flag_150_59_done:
            hit150_before59 = False
    else:
        # incomplete — leave unresolved as None; before-rates None if undecided
        if not flag_100_50_done:
            hit100_before50 = None
        if not flag_150_59_done:
            hit150_before59 = None

    for lv in HIT_LEVELS:
        row[f"bars_to_hit_{lv}"] = hit_bars[lv]
    for lv in STOP_LEVELS:
        row[f"bars_to_stop_{lv}"] = stop_bars[lv]
    row["bars_to_breach_extreme"] = breach_bars
    row["hit_rate_100_before_50"] = hit100_before50
    row["hit_rate_150_before_59"] = hit150_before59
    for t in TARGETS:
        for s in STOPS:
            row[f"expect_t{t}_s{s}"] = path_result[(t, s)]
    return row


def quartile_edges(vals: list[float]) -> tuple[float, float, float]:
    qs = statistics.quantiles(vals, n=4, method="inclusive")
    return float(qs[0]), float(qs[1]), float(qs[2])


def bucket_quartile(v: float, q1: float, q2: float, q3: float) -> str:
    if v <= q1:
        return "Q1"
    if v <= q2:
        return "Q2"
    if v <= q3:
        return "Q3"
    return "Q4"


def adx_bucket(adx: float) -> str:
    if adx < 15:
        return "<15"
    if adx < 20:
        return "15-20"
    if adx < 25:
        return "20-25"
    if adx < 28:
        return "25-28"
    return ">=28"


def summarize_group(rows: list[dict[str, Any]], label: str) -> list[str]:
    lines: list[str] = []
    n = len(rows)
    lines.append(f"### {label}  (n={n})")
    if n == 0:
        lines.append("  (empty)")
        lines.append("")
        return lines

    def collect(key: str) -> list[float]:
        return [float(r[key]) for r in rows if r.get(key) is not None]

    # Headline hit rates / expectancy
    h100 = [r["hit_rate_100_before_50"] for r in rows if r.get("hit_rate_100_before_50") is not None]
    h150 = [r["hit_rate_150_before_59"] for r in rows if r.get("hit_rate_150_before_59") is not None]
    n100 = sum(1 for x in h100 if x)
    n150 = sum(1 for x in h150 if x)
    lines.append(
        f"  hit_rate_100_before_50: {n100}/{len(h100)} = {_pct(n100, len(h100))} "
        f"(eligible {len(h100)}/{n})"
    )
    lines.append(
        f"  hit_rate_150_before_59: {n150}/{len(h150)} = {_pct(n150, len(h150))} "
        f"(eligible {len(h150)}/{n})"
    )
    lines.append("  expectancy grid (points; cost floors OFF=23 ON=53):")
    for t in TARGETS:
        for s in STOPS:
            key = f"expect_t{t}_s{s}"
            vals = collect(key)
            m = _mean(vals)
            clears_off = "YES" if m is not None and m > COST_HEDGE_OFF else "no"
            clears_on = "YES" if m is not None and m > COST_HEDGE_ON else "no"
            lines.append(
                f"    T={t} S={s}: mean={_fmt(m)}  n={len(vals)}  "
                f">23? {clears_off}  >53? {clears_on}"
            )

    lines.append("  forward close-to-close (mean / median):")
    for b in FWD_BARS:
        vals = collect(f"fwd_{b}")
        lines.append(
            f"    fwd_{b}: mean={_fmt(_mean(vals))}  median={_fmt(_median(vals))}  n={len(vals)}"
        )
    lines.append("  MFE / MAE (mean / median):")
    for w in MFE_MAE_WINDOWS:
        mf = collect(f"mfe_{w}")
        ma = collect(f"mae_{w}")
        lines.append(
            f"    mfe_{w}: mean={_fmt(_mean(mf))} median={_fmt(_median(mf))} | "
            f"mae_{w}: mean={_fmt(_mean(ma))} median={_fmt(_median(ma))}"
        )
    lines.append("  hit rates (reached level within available <=240 bars):")
    for lv in HIT_LEVELS:
        key = f"bars_to_hit_{lv}"
        # among rows with full horizon only for fair rate
        full = [r for r in rows if int(r.get("max_forward_bars") or 0) >= HORIZON]
        hits = sum(1 for r in full if r.get(key) is not None)
        lines.append(f"    hit_{lv}: {hits}/{len(full)} = {_pct(hits, len(full))}")
    lines.append("  stop rates:")
    for lv in STOP_LEVELS:
        key = f"bars_to_stop_{lv}"
        full = [r for r in rows if int(r.get("max_forward_bars") or 0) >= HORIZON]
        hits = sum(1 for r in full if r.get(key) is not None)
        lines.append(f"    stop_{lv}: {hits}/{len(full)} = {_pct(hits, len(full))}")
    full = [r for r in rows if int(r.get("max_forward_bars") or 0) >= HORIZON]
    br = sum(1 for r in full if r.get("bars_to_breach_extreme") is not None)
    lines.append(
        f"  breach_extreme rate: {br}/{len(full)} = {_pct(br, len(full))}"
    )
    lines.append("")
    return lines


def build_baseline(
    signals: list[dict[str, Any]],
    times: list[int],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    rng: random.Random,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match LONG/SHORT mix and IST hour distribution; fixed seed via rng."""
    # candle indices by IST hour that have enough room when possible
    by_hour: dict[int, list[int]] = defaultdict(list)
    by_hour_full: dict[int, list[int]] = defaultdict(list)
    for i, ts in enumerate(times):
        hour = datetime.fromtimestamp(ts, tz=IST).hour
        by_hour[hour].append(i)
        if i + HORIZON < len(times):
            by_hour_full[hour].append(i)

    directions = [s["direction"] for s in signals]
    rng.shuffle(directions)

    meta: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for i, sig in enumerate(signals):
        hour = int(sig["hour_ist"])
        pool = by_hour_full.get(hour) or by_hour.get(hour) or list(range(len(times)))
        idx = pool[rng.randrange(len(pool))]
        direction = directions[i]
        entry = closes[idx]
        dist = float(sig["distance_points"])
        extreme = entry - dist if direction == "LONG" else entry + dist
        m = compute_path_metrics(
            idx=idx,
            direction=direction,
            entry=entry,
            extreme=extreme,
            highs=highs,
            lows=lows,
            closes=closes,
        )
        meta.append(
            {
                "kind": "baseline",
                "direction": direction,
                "mode": "RANDOM",
                "score": -1,
                "hour_ist": hour,
                "confirm_price": entry,
                "signal_extreme": extreme,
                "distance_points": dist,
                "atr_at_arm": float(sig["atr_at_arm"]),
                "adx_at_signal": float(sig["adx_at_signal"]),
                "c_wick": False,
                "c_volume": False,
                "c_vwap_ext": False,
                "c_rsi": False,
            }
        )
        metrics.append(m)
    return meta, metrics


def run(
    *,
    signals_path: Path,
    data_path: Path,
    seed: int,
) -> tuple[Path, Path]:
    t0 = time.perf_counter()
    times, _o, highs, lows, closes = load_candles(data_path)
    index_by_time = {t: i for i, t in enumerate(times)}
    signals = load_signals(signals_path)

    atr_vals = [s["atr_at_arm"] for s in signals]
    dist_vals = [s["distance_points"] for s in signals]
    atr_q = quartile_edges(atr_vals)
    dist_q = quartile_edges(dist_vals)

    signal_meta: list[dict[str, Any]] = []
    signal_metrics: list[dict[str, Any]] = []
    missing_confirm = 0
    tail_count = 0

    for sig in signals:
        idx = index_by_time.get(sig["confirm_unix"])
        if idx is None:
            missing_confirm += 1
            continue
        # Prefer CSV confirm close; fall back to stored confirm_price
        entry = closes[idx]
        m = compute_path_metrics(
            idx=idx,
            direction=sig["direction"],
            entry=entry,
            extreme=float(sig["signal_extreme"]),
            highs=highs,
            lows=lows,
            closes=closes,
        )
        if m["tail_truncated"]:
            tail_count += 1
        meta = dict(sig)
        meta["kind"] = "signal"
        meta["confirm_price"] = entry
        meta["atr_bucket"] = bucket_quartile(sig["atr_at_arm"], *atr_q)
        meta["dist_bucket"] = bucket_quartile(sig["distance_points"], *dist_q)
        meta["adx_bucket"] = adx_bucket(sig["adx_at_signal"])
        signal_meta.append(meta)
        signal_metrics.append(m)

    rng = random.Random(seed)
    base_meta, base_metrics = build_baseline(
        signal_meta, times, highs, lows, closes, rng
    )

    # Worked SHORT example for audit
    worked = None
    for meta, met in zip(signal_meta, signal_metrics):
        if meta["direction"] == "SHORT" and met.get("fwd_30") is not None:
            idx = met["entry_idx"]
            worked = {
                "confirm_unix": times[idx],
                "confirm_price": closes[idx],
                "price_30": closes[idx + 30],
                "raw_move": closes[idx + 30] - closes[idx],
                "fwd_30": met["fwd_30"],
            }
            break

    runtime = time.perf_counter() - t0
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = RESULTS_DIR / f"s003_edge_{stamp}.txt"
    csv_path = RESULTS_DIR / f"s003_edge_{stamp}.csv"

    # Write per-signal CSV (signals only)
    fieldnames = [
        "kind",
        "confirm_unix",
        "confirm_ist",
        "direction",
        "mode",
        "score",
        "c_wick",
        "c_volume",
        "c_vwap_ext",
        "c_rsi",
        "confirm_price",
        "signal_extreme",
        "distance_points",
        "atr_at_arm",
        "adx_at_signal",
        "hour_ist",
        "adx_bucket",
        "atr_bucket",
        "dist_bucket",
        "tail_truncated",
        "max_forward_bars",
    ]
    for b in FWD_BARS:
        fieldnames.append(f"fwd_{b}")
    for w in MFE_MAE_WINDOWS:
        fieldnames.extend([f"mfe_{w}", f"mae_{w}"])
    for lv in HIT_LEVELS:
        fieldnames.append(f"bars_to_hit_{lv}")
    for lv in STOP_LEVELS:
        fieldnames.append(f"bars_to_stop_{lv}")
    fieldnames.append("bars_to_breach_extreme")
    fieldnames.extend(["hit_100_before_50", "hit_150_before_59"])
    for t in TARGETS:
        for s in STOPS:
            fieldnames.append(f"expect_t{t}_s{s}")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for meta, met in zip(signal_meta, signal_metrics):
            conf_u = int(meta["confirm_unix"])
            row = {
                "kind": "signal",
                "confirm_unix": conf_u,
                "confirm_ist": datetime.fromtimestamp(conf_u, tz=IST).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "direction": meta["direction"],
                "mode": meta["mode"],
                "score": meta["score"],
                "c_wick": int(meta["c_wick"]),
                "c_volume": int(meta["c_volume"]),
                "c_vwap_ext": int(meta["c_vwap_ext"]),
                "c_rsi": int(meta["c_rsi"]),
                "confirm_price": meta["confirm_price"],
                "signal_extreme": meta["signal_extreme"],
                "distance_points": meta["distance_points"],
                "atr_at_arm": meta["atr_at_arm"],
                "adx_at_signal": meta["adx_at_signal"],
                "hour_ist": meta["hour_ist"],
                "adx_bucket": meta["adx_bucket"],
                "atr_bucket": meta["atr_bucket"],
                "dist_bucket": meta["dist_bucket"],
                "tail_truncated": int(met["tail_truncated"]),
                "max_forward_bars": met["max_forward_bars"],
                "hit_100_before_50": (
                    ""
                    if met["hit_rate_100_before_50"] is None
                    else int(met["hit_rate_100_before_50"])
                ),
                "hit_150_before_59": (
                    ""
                    if met["hit_rate_150_before_59"] is None
                    else int(met["hit_rate_150_before_59"])
                ),
            }
            for b in FWD_BARS:
                row[f"fwd_{b}"] = met.get(f"fwd_{b}")
            for ww in MFE_MAE_WINDOWS:
                row[f"mfe_{ww}"] = met.get(f"mfe_{ww}")
                row[f"mae_{ww}"] = met.get(f"mae_{ww}")
            for lv in HIT_LEVELS:
                row[f"bars_to_hit_{lv}"] = met.get(f"bars_to_hit_{lv}")
            for lv in STOP_LEVELS:
                row[f"bars_to_stop_{lv}"] = met.get(f"bars_to_stop_{lv}")
            row["bars_to_breach_extreme"] = met.get("bars_to_breach_extreme")
            for t in TARGETS:
                for s in STOPS:
                    row[f"expect_t{t}_s{s}"] = met.get(f"expect_t{t}_s{s}")
            w.writerow(row)

    def pack(metas: list[dict[str, Any]], mets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**a, **b} for a, b in zip(metas, mets)]

    sig_rows = pack(signal_meta, signal_metrics)
    base_rows = pack(base_meta, base_metrics)

    lines: list[str] = []
    lines.append("=== S003 Phase 2.3 — signal edge vs random baseline ===")
    lines.append(f"signals_file: {signals_path}")
    lines.append(f"data_file:    {data_path}")
    lines.append(f"random_seed:  {seed}")
    lines.append(f"signals loaded: {len(signals)}")
    lines.append(f"signals matched to candles: {len(signal_meta)}")
    lines.append(f"confirm timestamps missing from CSV: {missing_confirm}")
    lines.append(
        f"tail-truncated signals ( <{HORIZON} bars of forward data ): {tail_count}"
    )
    lines.append(f"baseline entries: {len(base_rows)}")
    lines.append(f"runtime_s: {runtime:.3f}")
    lines.append(f"ATR quartiles: Q1={atr_q[0]:.4f} Q2={atr_q[1]:.4f} Q3={atr_q[2]:.4f}")
    lines.append(
        f"distance_points quartiles: Q1={dist_q[0]:.2f} Q2={dist_q[1]:.2f} Q3={dist_q[2]:.2f}"
    )
    lines.append("")
    lines.append("COST FLOORS (round-trip, points): hedge OFF=23  hedge ON=53")
    lines.append("")

    # HEADLINE first
    lines.append("========== HEADLINE ==========")
    lines.extend(summarize_group(sig_rows, "ALL SIGNALS"))
    lines.extend(summarize_group(base_rows, "RANDOM BASELINE"))

    lines.append("========== BY SCORE ==========")
    for sc in (3, 4, 5):
        lines.extend(
            summarize_group([r for r in sig_rows if int(r["score"]) == sc], f"score={sc}")
        )
    lines.extend(summarize_group(base_rows, "BASELINE (vs score cuts)"))

    lines.append("========== BY COMPONENT ==========")
    for comp in ("c_wick", "c_volume", "c_vwap_ext", "c_rsi"):
        lines.extend(
            summarize_group([r for r in sig_rows if r[comp]], f"{comp}=1 (fired)")
        )
        lines.extend(
            summarize_group([r for r in sig_rows if not r[comp]], f"{comp}=0 (not fired)")
        )

    lines.append("========== BY DIRECTION ==========")
    for d in ("LONG", "SHORT"):
        lines.extend(
            summarize_group([r for r in sig_rows if r["direction"] == d], f"direction={d}")
        )
        lines.extend(
            summarize_group(
                [r for r in base_rows if r["direction"] == d], f"baseline direction={d}"
            )
        )

    lines.append("========== BY MODE ==========")
    for mode in ("RANGE", "EXHAUSTION"):
        lines.extend(
            summarize_group([r for r in sig_rows if r["mode"] == mode], f"mode={mode}")
        )

    lines.append("========== BY ADX BUCKET ==========")
    for b in ("<15", "15-20", "20-25", "25-28", ">=28"):
        lines.extend(
            summarize_group(
                [r for r in sig_rows if r["adx_bucket"] == b], f"adx={b}"
            )
        )

    lines.append("========== BY ATR QUARTILE ==========")
    for b in ("Q1", "Q2", "Q3", "Q4"):
        lines.extend(
            summarize_group(
                [r for r in sig_rows if r["atr_bucket"] == b], f"atr={b}"
            )
        )

    lines.append("========== BY DISTANCE QUARTILE ==========")
    for b in ("Q1", "Q2", "Q3", "Q4"):
        lines.extend(
            summarize_group(
                [r for r in sig_rows if r["dist_bucket"] == b], f"distance={b}"
            )
        )

    lines.append("========== BY HOUR IST ==========")
    for hour in range(24):
        lines.extend(
            summarize_group(
                [r for r in sig_rows if int(r["hour_ist"]) == hour],
                f"hour_ist={hour:02d}",
            )
        )
        lines.extend(
            summarize_group(
                [r for r in base_rows if int(r["hour_ist"]) == hour],
                f"baseline hour_ist={hour:02d}",
            )
        )

    lines.append("========== SHORT SIGN AUDIT ==========")
    if worked is None:
        lines.append("No SHORT signal with fwd_30 available.")
    else:
        lines.append(
            f"confirm_unix={worked['confirm_unix']}  "
            f"confirm_ist={datetime.fromtimestamp(worked['confirm_unix'], tz=IST)}"
        )
        lines.append(f"confirm_price={worked['confirm_price']}")
        lines.append(f"price_30_bars_later={worked['price_30']}")
        lines.append(f"raw_close_move (later-confirm)={worked['raw_move']}")
        lines.append(
            f"fwd_30 (SHORT => negated)={worked['fwd_30']}  "
            f"[expected={-worked['raw_move']}]"
        )
        ok = abs(worked["fwd_30"] - (-worked["raw_move"])) < 1e-9
        lines.append(f"sign_flip_ok={ok}")

    text = "\n".join(lines) + "\n"
    txt_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"report: {txt_path}")
    print(f"csv:    {csv_path}")
    return txt_path, csv_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="S003 signal edge vs random baseline")
    p.add_argument("--signals", default=None, help="Path to s003_signals_*.csv")
    p.add_argument("--data", default=str(DEFAULT_DATA))
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = p.parse_args(argv)
    try:
        signals_path = Path(args.signals) if args.signals else _latest_signals_csv()
        data_path = Path(args.data)
        if not data_path.is_file():
            alt = Path(__file__).resolve().parents[1] / args.data
            if alt.is_file():
                data_path = alt
        run(signals_path=signals_path, data_path=data_path, seed=int(args.seed))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
