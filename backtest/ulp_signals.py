#!/usr/bin/env python3
"""
ULP — pre-registered entry rules R1/R2 + modifiers, measured vs random baselines.

Reuses ulp_zones for zone construction (import only). Emits signal CSVs in the
schema s003_edge.load_signals already consumes, then calls s003_edge.run
unchanged for each config with a fresh matched baseline seed.

Rules were fixed before seeing results. Do not tweak them based on numbers.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import statistics
import sys
import time
from dataclasses import dataclass
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
import ulp_zones as uz  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
RESULTS_DIR = _BACKTEST / "results"
DATA_DIR = _BACKTEST / "data_1m"
LOOKBACK = 5
TIMEFRAMES = ("5m", "15m")
MIN_HEIGHTS = (0, 23, 46, 69, 100)
AGE_FLOOR_SPLITS = (20, 100, 500)
BASE_SEED = 20260913
COST_23 = 23.0
COST_53 = 53.0

HOW_TO_READ = """\
HOW TO READ THIS REPORT
  With ~1,500 signals per config and 40 configs, the best config will show
  roughly +3.4pp of hit rate above baseline from luck alone:
      SD = sqrt(0.25/1500) = 1.29pp ;  best of 40 ~ sqrt(2 ln 40) x SD
  So a small hit-rate lead in one cell means nothing. What counts is
  edge_over_baseline clearing 23 points, and doing so across NEIGHBOURING
  cells rather than in one isolated square.
"""


@dataclass
class RawSignal:
    bar_index: int
    rule: str  # R1 | R2
    direction: str  # LONG | SHORT
    zone_side: str  # HIGH | LOW
    zone_id: int
    zone_top_at_activation: float
    zone_bot_at_activation: float
    zone_height_at_activation: float
    zone_age_bars_at_entry: int
    n_partial_touches_before_entry: int
    entry_price: float
    stop_price: float
    stop_distance_points: float
    distance_from_close_at_confirm: float
    # levels at signal bar (pre-shrink) + bar OHLC (audit)
    zone_top_at_entry: float
    zone_bot_at_entry: float
    bar_high: float
    bar_low: float
    bar_close: float


def resolve_data(tf: str) -> Path:
    matches = sorted(DATA_DIR.glob(f"BTCUSD_{tf}_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_{tf}_*.csv in {DATA_DIR}")
    return matches[-1]


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{100.0 * v:.1f}%"


def _fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"


def _ts_label(ts: tuple[int, int] | None) -> str:
    if ts is None:
        return "n/a"
    return f"T={ts[0]}/S={ts[1]}"


def collect_signals(
    bars: list[uz.Bar],
    *,
    lookback: int,
    shrink_during_confirmation: bool,
) -> tuple[list[RawSignal], list[RawSignal]]:
    """
    One zone-engine pass. Emit R1 and R2 candidates BEFORE each bar's shrink.
    Zone geometry / pivots / shrink via ulp_zones primitives only.
    """
    n = len(bars)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    opens = [b.open for b in bars]
    closes = [b.close for b in bars]

    high_set = uz.ActiveHighSet()
    low_set = uz.ActiveLowSet()
    next_id = 0
    r1: list[RawSignal] = []
    r2: list[RawSignal] = []

    # Parallel activation bookkeeping (not on Zone dataclass to avoid editing it)
    top_act: dict[int, float] = {}
    bot_act: dict[int, float] = {}
    height_act: dict[int, float] = {}
    dist_confirm: dict[int, float] = {}

    for i in range(n):
        pivot = i - lookback
        if pivot >= lookback:
            if uz.is_pivot_high(highs, pivot, lookback, lookback):
                top = highs[pivot]
                bot = max(opens[pivot], closes[pivot])
                z = uz.Zone(
                    zone_id=next_id,
                    side="HIGH",
                    pivot_index=pivot,
                    confirm_index=i,
                    zone_top=top,
                    zone_bot=bot,
                    top_birth=top,
                    bot_birth=bot,
                    zero_height=(top == bot),
                )
                next_id += 1
                if shrink_during_confirmation:
                    uz.replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                top_act[z.zone_id] = z.zone_top
                bot_act[z.zone_id] = z.zone_bot
                height_act[z.zone_id] = z.height
                dist_confirm[z.zone_id] = z.zone_bot - closes[i]
                if z.still_alive:
                    high_set.insert(z)

            if uz.is_pivot_low(lows, pivot, lookback, lookback):
                top = min(opens[pivot], closes[pivot])
                bot = lows[pivot]
                z = uz.Zone(
                    zone_id=next_id,
                    side="LOW",
                    pivot_index=pivot,
                    confirm_index=i,
                    zone_top=top,
                    zone_bot=bot,
                    top_birth=top,
                    bot_birth=bot,
                    zero_height=(top == bot),
                )
                next_id += 1
                if shrink_during_confirmation:
                    uz.replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                top_act[z.zone_id] = z.zone_top
                bot_act[z.zone_id] = z.zone_bot
                height_act[z.zone_id] = z.height
                dist_confirm[z.zone_id] = closes[i] - z.zone_top
                if z.still_alive:
                    low_set.insert(z)

        bar = bars[i]

        # --- Signal checks BEFORE shrink (use pre-bar levels) ---
        for z in list(high_set.zones):
            ta = top_act[z.zone_id]
            ba = bot_act[z.zone_id]
            ha = height_act[z.zone_id]
            dc = dist_confirm[z.zone_id]
            age = i - z.confirm_index
            touches = z.n_partial_touches

            # R1 fade SHORT
            if bar.high > z.zone_bot and bar.close < z.zone_bot:
                stop = ta
                entry = bar.close
                r1.append(
                    RawSignal(
                        bar_index=i,
                        rule="R1",
                        direction="SHORT",
                        zone_side="HIGH",
                        zone_id=z.zone_id,
                        zone_top_at_activation=ta,
                        zone_bot_at_activation=ba,
                        zone_height_at_activation=ha,
                        zone_age_bars_at_entry=age,
                        n_partial_touches_before_entry=touches,
                        entry_price=entry,
                        stop_price=stop,
                        stop_distance_points=abs(stop - entry),
                        distance_from_close_at_confirm=dc,
                        zone_top_at_entry=z.zone_top,
                        zone_bot_at_entry=z.zone_bot,
                        bar_high=bar.high,
                        bar_low=bar.low,
                        bar_close=bar.close,
                    )
                )

            # R2 break LONG — stop = current zone_bot (pre-bar)
            if bar.close > z.zone_top:
                stop = z.zone_bot
                entry = bar.close
                r2.append(
                    RawSignal(
                        bar_index=i,
                        rule="R2",
                        direction="LONG",
                        zone_side="HIGH",
                        zone_id=z.zone_id,
                        zone_top_at_activation=ta,
                        zone_bot_at_activation=ba,
                        zone_height_at_activation=ha,
                        zone_age_bars_at_entry=age,
                        n_partial_touches_before_entry=touches,
                        entry_price=entry,
                        stop_price=stop,
                        stop_distance_points=abs(entry - stop),
                        distance_from_close_at_confirm=dc,
                        zone_top_at_entry=z.zone_top,
                        zone_bot_at_entry=z.zone_bot,
                        bar_high=bar.high,
                        bar_low=bar.low,
                        bar_close=bar.close,
                    )
                )

        for z in list(low_set.zones):
            ta = top_act[z.zone_id]
            ba = bot_act[z.zone_id]
            ha = height_act[z.zone_id]
            dc = dist_confirm[z.zone_id]
            age = i - z.confirm_index
            touches = z.n_partial_touches

            # R1 fade LONG
            if bar.low < z.zone_top and bar.close > z.zone_top:
                stop = ba
                entry = bar.close
                r1.append(
                    RawSignal(
                        bar_index=i,
                        rule="R1",
                        direction="LONG",
                        zone_side="LOW",
                        zone_id=z.zone_id,
                        zone_top_at_activation=ta,
                        zone_bot_at_activation=ba,
                        zone_height_at_activation=ha,
                        zone_age_bars_at_entry=age,
                        n_partial_touches_before_entry=touches,
                        entry_price=entry,
                        stop_price=stop,
                        stop_distance_points=abs(entry - stop),
                        distance_from_close_at_confirm=dc,
                        zone_top_at_entry=z.zone_top,
                        zone_bot_at_entry=z.zone_bot,
                        bar_high=bar.high,
                        bar_low=bar.low,
                        bar_close=bar.close,
                    )
                )

            # R2 break SHORT — stop = current zone_top (pre-bar)
            if bar.close < z.zone_bot:
                stop = z.zone_top
                entry = bar.close
                r2.append(
                    RawSignal(
                        bar_index=i,
                        rule="R2",
                        direction="SHORT",
                        zone_side="LOW",
                        zone_id=z.zone_id,
                        zone_top_at_activation=ta,
                        zone_bot_at_activation=ba,
                        zone_height_at_activation=ha,
                        zone_age_bars_at_entry=age,
                        n_partial_touches_before_entry=touches,
                        entry_price=entry,
                        stop_price=stop,
                        stop_distance_points=abs(stop - entry),
                        distance_from_close_at_confirm=dc,
                        zone_top_at_entry=z.zone_top,
                        zone_bot_at_entry=z.zone_bot,
                        bar_high=bar.high,
                        bar_low=bar.low,
                        bar_close=bar.close,
                    )
                )

        # --- Shrink / sweep (ulp_zones) ---
        for z in list(high_set.candidates_touching(bar.high)):
            action = uz.apply_bar_to_high_zone(z, bar, i)
            if action == "remove":
                high_set.remove(z)
            elif action == "moved":
                high_set.remove(z)
                if z.still_alive:
                    high_set.insert(z)

        for z in list(low_set.candidates_touching(bar.low)):
            action = uz.apply_bar_to_low_zone(z, bar, i)
            if action == "remove":
                low_set.remove(z)
            elif action == "moved":
                low_set.remove(z)
                if z.still_alive:
                    low_set.insert(z)

    return r1, r2


def write_signals_csv(
    path: Path,
    signals: list[RawSignal],
    bars: list[uz.Bar],
    *,
    timeframe: str,
    shrink_mode: str,
) -> None:
    cols = [
        "signal_candle_utc",
        "signal_candle_ist",
        "confirm_candle_utc",
        "confirm_candle_ist",
        "direction",
        "mode",
        "score",
        "c_sweep",
        "c_wick",
        "c_volume",
        "c_vwap_ext",
        "c_rsi",
        "confirm_price",
        "signal_extreme",
        "distance_points",
        "atr_at_arm",
        "atr_at_confirm",
        "adx_at_signal",
        "rsi_at_arm",
        "vwap_at_arm",
        "volume_at_arm",
        "vol_sma_at_arm",
        "bars_since_previous_signal",
        # ULP extras (ignored by s003_edge.load_signals)
        "rule",
        "side",
        "zone_side",
        "timeframe",
        "shrink_mode",
        "zone_top_at_activation",
        "zone_bot_at_activation",
        "zone_height_at_activation",
        "zone_age_bars_at_entry",
        "n_partial_touches_before_entry",
        "entry_price",
        "stop_price",
        "stop_distance_points",
        "distance_from_close_at_confirm",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        prev_i: int | None = None
        for sig in signals:
            b = bars[sig.bar_index]
            t_utc = uz._fmt_utc(b.time)
            t_ist = uz._fmt_ist(b.time)
            since = "" if prev_i is None else sig.bar_index - prev_i
            prev_i = sig.bar_index
            w.writerow(
                {
                    "signal_candle_utc": t_utc,
                    "signal_candle_ist": t_ist,
                    "confirm_candle_utc": t_utc,
                    "confirm_candle_ist": t_ist,
                    "direction": sig.direction,
                    "mode": sig.rule,
                    "score": 0,
                    "c_sweep": 0,
                    "c_wick": 0,
                    "c_volume": 0,
                    "c_vwap_ext": 0,
                    "c_rsi": 0,
                    "confirm_price": f"{sig.entry_price:.4f}",
                    "signal_extreme": f"{sig.stop_price:.4f}",
                    "distance_points": f"{sig.stop_distance_points:.4f}",
                    "atr_at_arm": "0",
                    "atr_at_confirm": "0",
                    "adx_at_signal": "0",
                    "rsi_at_arm": "0",
                    "vwap_at_arm": "0",
                    "volume_at_arm": "0",
                    "vol_sma_at_arm": "0",
                    "bars_since_previous_signal": since,
                    "rule": sig.rule,
                    "side": sig.direction,
                    "zone_side": sig.zone_side,
                    "timeframe": timeframe,
                    "shrink_mode": shrink_mode,
                    "zone_top_at_activation": f"{sig.zone_top_at_activation:.4f}",
                    "zone_bot_at_activation": f"{sig.zone_bot_at_activation:.4f}",
                    "zone_height_at_activation": f"{sig.zone_height_at_activation:.4f}",
                    "zone_age_bars_at_entry": sig.zone_age_bars_at_entry,
                    "n_partial_touches_before_entry": sig.n_partial_touches_before_entry,
                    "entry_price": f"{sig.entry_price:.4f}",
                    "stop_price": f"{sig.stop_price:.4f}",
                    "stop_distance_points": f"{sig.stop_distance_points:.4f}",
                    "distance_from_close_at_confirm": f"{sig.distance_from_close_at_confirm:.4f}",
                }
            )


def filter_min_height(sigs: list[RawSignal], min_h: float) -> list[RawSignal]:
    return [s for s in sigs if s.zone_height_at_activation >= min_h]


def filter_first_touch(sigs: list[RawSignal]) -> list[RawSignal]:
    return [s for s in sigs if s.n_partial_touches_before_entry == 0]


def filter_age(sigs: list[RawSignal], min_age: int) -> list[RawSignal]:
    return [s for s in sigs if s.zone_age_bars_at_entry >= min_age]


def quartile_split(
    sigs: list[RawSignal],
) -> list[tuple[str, list[RawSignal]]]:
    if not sigs:
        return [("Q1", []), ("Q2", []), ("Q3", []), ("Q4", [])]
    dists = sorted(s.distance_from_close_at_confirm for s in sigs)
    q1, q2, q3 = s003_edge.quartile_edges(dists)
    buckets: dict[str, list[RawSignal]] = {"Q1": [], "Q2": [], "Q3": [], "Q4": []}
    for s in sigs:
        buckets[s003_edge.bucket_quartile(s.distance_from_close_at_confirm, q1, q2, q3)].append(
            s
        )
    return [(k, buckets[k]) for k in ("Q1", "Q2", "Q3", "Q4")]


def median_stop(sigs: list[RawSignal]) -> float | None:
    if not sigs:
        return None
    return float(statistics.median(s.stop_distance_points for s in sigs))


def span_days(bars: list[uz.Bar]) -> float:
    if len(bars) < 2:
        return 1.0
    return max((bars[-1].time - bars[0].time) / 86400.0, 1e-9)


def precompute_path_metrics(
    sigs: list[RawSignal],
    *,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[dict[str, Any]]:
    targets = s003_edge.DEFAULT_TARGETS
    stops = s003_edge.DEFAULT_STOPS
    out: list[dict[str, Any]] = []
    for sig in sigs:
        idx = sig.bar_index
        entry = closes[idx]
        out.append(
            s003_edge.compute_path_metrics(
                idx=idx,
                direction=sig.direction,
                entry=entry,
                extreme=sig.stop_price,
                highs=highs,
                lows=lows,
                closes=closes,
                targets=targets,
                stops=stops,
            )
        )
    return out


def measure_subset(
    sigs: list[RawSignal],
    metrics: list[dict[str, Any]],
    *,
    times: list[int],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    seed: int,
    label: str,
) -> dict[str, Any]:
    """Matched baseline + expectancy using s003_edge helpers (modifier splits)."""
    targets = s003_edge.DEFAULT_TARGETS
    stops = s003_edge.DEFAULT_STOPS
    if not sigs:
        return {
            "label": label,
            "seed": seed,
            "n_signals": 0,
            "signals_per_day": 0.0,
            "median_stop_distance_points": None,
            "hit_100_before_50_signal": None,
            "hit_100_before_50_baseline": None,
            "best_expectancy_signal": None,
            "best_expectancy_baseline": None,
            "best_expectancy_signal_ts": None,
            "best_expectancy_baseline_ts": None,
            "edge_over_baseline": None,
            "clears_23": False,
            "clears_53": False,
            "skipped": True,
        }

    signal_meta: list[dict[str, Any]] = []
    for sig in sigs:
        ts = times[sig.bar_index]
        signal_meta.append(
            {
                "direction": sig.direction,
                "hour_ist": datetime.fromtimestamp(ts, tz=IST).hour,
                "confirm_price": closes[sig.bar_index],
                "signal_extreme": sig.stop_price,
                "distance_points": sig.stop_distance_points,
                "atr_at_arm": 0.0,
                "adx_at_signal": 0.0,
            }
        )

    import random as _random

    rng = _random.Random(seed)
    base_meta, base_metrics = s003_edge.build_baseline(
        signal_meta,
        times,
        highs,
        lows,
        closes,
        rng,
        targets=targets,
        stops=stops,
    )

    sig_rows: list[dict[str, Any]] = []
    for meta, met in zip(signal_meta, metrics):
        row = {**meta, **met}
        sig_rows.append(row)

    base_rows: list[dict[str, Any]] = []
    for meta, met in zip(base_meta, base_metrics):
        base_rows.append({**meta, **met})

    sig_best, sig_ts = s003_edge._best_expectancy(sig_rows, targets, stops)
    base_best, base_ts = s003_edge._best_expectancy(base_rows, targets, stops)
    edge = None
    if sig_best is not None and base_best is not None:
        edge = sig_best - base_best

    span = max((times[-1] - times[0]) / 86400.0, 1e-9) if times else 1.0
    return {
        "label": label,
        "seed": seed,
        "n_signals": len(sigs),
        "signals_per_day": len(sigs) / span,
        "median_stop_distance_points": median_stop(sigs),
        "hit_100_before_50_signal": s003_edge._hit_rate(
            sig_rows, "hit_rate_100_before_50"
        ),
        "hit_100_before_50_baseline": s003_edge._hit_rate(
            base_rows, "hit_rate_100_before_50"
        ),
        "best_expectancy_signal": sig_best,
        "best_expectancy_baseline": base_best,
        "best_expectancy_signal_ts": sig_ts,
        "best_expectancy_baseline_ts": base_ts,
        "edge_over_baseline": edge,
        "clears_23": bool(edge is not None and edge > COST_23),
        "clears_53": bool(edge is not None and edge > COST_53),
        "skipped": False,
    }


def run_edge_for(
    sigs: list[RawSignal],
    bars: list[uz.Bar],
    data_path: Path,
    *,
    timeframe: str,
    shrink_mode: str,
    seed: int,
    label: str,
) -> dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S_%f")
    safe = label.replace(" ", "_").replace("|", "_").replace("=", "")
    sig_path = RESULTS_DIR / f"ulp_signals_{safe}_{stamp}.csv"
    write_signals_csv(
        sig_path, sigs, bars, timeframe=timeframe, shrink_mode=shrink_mode
    )
    if not sigs:
        return {
            "label": label,
            "seed": seed,
            "n_signals": 0,
            "signals_per_day": 0.0,
            "median_stop_distance_points": None,
            "hit_100_before_50_signal": None,
            "hit_100_before_50_baseline": None,
            "best_expectancy_signal": None,
            "best_expectancy_baseline": None,
            "best_expectancy_signal_ts": None,
            "best_expectancy_baseline_ts": None,
            "edge_over_baseline": None,
            "clears_23": False,
            "clears_53": False,
            "signals_path": str(sig_path),
            "skipped": True,
        }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _txt, _csv, summary = s003_edge.run(
            signals_path=sig_path,
            data_path=data_path,
            seed=seed,
            timeframe=timeframe,
            targets=s003_edge.DEFAULT_TARGETS,
            stops=s003_edge.DEFAULT_STOPS,
        )
    return {
        "label": label,
        "seed": seed,
        "n_signals": summary["signals_total"],
        "signals_per_day": summary["signals_per_day"],
        "median_stop_distance_points": median_stop(sigs),
        "hit_100_before_50_signal": summary["hit_100_before_50_signal"],
        "hit_100_before_50_baseline": summary["hit_100_before_50_baseline"],
        "best_expectancy_signal": summary["best_expectancy_signal"],
        "best_expectancy_baseline": summary["best_expectancy_baseline"],
        "best_expectancy_signal_ts": summary["best_expectancy_signal_ts"],
        "best_expectancy_baseline_ts": summary["best_expectancy_baseline_ts"],
        "edge_over_baseline": summary["edge_over_baseline"],
        "clears_23": summary["clears_23"],
        "clears_53": summary["clears_53"],
        "signals_path": str(sig_path),
        "edge_txt": str(_txt),
        "skipped": False,
    }


def hand_audit_r1_short(
    sigs: list[RawSignal], bars: list[uz.Bar]
) -> list[str]:
    lines = ["=== HAND AUDIT — one R1 SHORT end-to-end ==="]
    cand = next((s for s in sigs if s.rule == "R1" and s.direction == "SHORT"), None)
    if cand is None:
        lines.append("No R1 SHORT signal found.")
        return lines

    i = cand.bar_index
    entry = cand.entry_price
    stop = cand.stop_price
    if i + 30 < len(bars):
        px30 = bars[i + 30].close
        raw = px30 - entry
        fwd = -raw  # SHORT: favourable if price falls
    else:
        px30 = None
        raw = None
        fwd = None

    lines.append(f"bar_index={i}  time_ist={uz._fmt_ist(bars[i].time)}")
    lines.append(f"zone_side={cand.zone_side} zone_id={cand.zone_id}")
    lines.append(
        f"zone_top_at_activation={cand.zone_top_at_activation:.4f}  "
        f"zone_bot_at_activation={cand.zone_bot_at_activation:.4f}  "
        f"height_at_activation={cand.zone_height_at_activation:.4f}"
    )
    lines.append(
        f"bar high={cand.bar_high:.4f}  low={cand.bar_low:.4f}  close={cand.bar_close:.4f}"
    )
    r1_ok = cand.bar_high > cand.zone_bot_at_entry and cand.bar_close < cand.zone_bot_at_entry
    lines.append(
        f"R1 check: high={cand.bar_high:.4f} > zone_bot_at_entry={cand.zone_bot_at_entry:.4f} "
        f"AND close={cand.bar_close:.4f} < zone_bot_at_entry "
        f"-> ok={r1_ok} ; SHORT at close"
    )
    lines.append(
        f"entry_price={entry:.4f}  stop_price={stop:.4f}  "
        f"stop_distance={cand.stop_distance_points:.4f}"
    )
    lines.append(
        f"stop is zone_top_at_activation={cand.zone_top_at_activation:.4f} "
        f"(match={abs(stop - cand.zone_top_at_activation) < 1e-9})"
    )
    if px30 is None:
        lines.append("price 30 bars later: n/a (tail)")
    else:
        lines.append(
            f"price_30={px30:.4f}  raw_close_move={raw:.4f}  "
            f"signed_fwd_return_SHORT={fwd:.4f}  "
            f"(positive => price fell, fade worked)"
        )
        # Sign check: SHORT favours down moves
        if fwd is not None and raw is not None:
            ok = abs(fwd - (-raw)) < 1e-9
            lines.append(f"sign_convention_ok={ok}")
    return lines


def format_row(r: dict[str, Any]) -> str:
    return (
        f"{r['rule']:>2}  {r['tf']:>3}  {r['shrink']:>1}  {r['min_height']:>3}  "
        f"{r['n_signals']:>7}  {_fmt_num(r['signals_per_day'], 2):>7}  "
        f"{_fmt_num(r['median_stop_distance_points'], 1):>7}  "
        f"{_fmt_pct(r['hit_100_before_50_signal']):>6}  "
        f"{_fmt_pct(r['hit_100_before_50_baseline']):>6}  "
        f"{_fmt_num(r['best_expectancy_signal']):>7}  "
        f"{_fmt_num(r['best_expectancy_baseline']):>7}  "
        f"{_ts_label(r['best_expectancy_signal_ts']):>12}  "
        f"{_fmt_num(r['edge_over_baseline']):>7}  "
        f"{'Y' if r['clears_23'] else 'N':>3}  "
        f"{'Y' if r['clears_53'] else 'N':>3}  "
        f"seed={r['seed']}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ULP signals + edge measurement")
    p.add_argument("--lookback", type=int, default=LOOKBACK)
    args = p.parse_args(argv)

    t_all = time.perf_counter()
    lookback = int(args.lookback)

    # Cache raw R1/R2 per (tf, shrink)
    cache: dict[tuple[str, bool], tuple[list[RawSignal], list[RawSignal], list[uz.Bar], Path]] = {}

    base_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    seeds_used: list[tuple[str, int]] = []
    eval_count = 0
    audit_lines: list[str] = []
    audit_done = False

    config_idx = 0
    split_idx = 0

    for tf in TIMEFRAMES:
        data_path = resolve_data(tf)
        for shrink in (False, True):
            key = (tf, shrink)
            if key not in cache:
                print(f"Building zones+signals  tf={tf} shrink={shrink} ...")
                t0 = time.perf_counter()
                bars = uz.load_bars(data_path)
                r1, r2 = collect_signals(
                    bars,
                    lookback=lookback,
                    shrink_during_confirmation=shrink,
                )
                cache[key] = (r1, r2, bars, data_path)
                print(
                    f"  done in {time.perf_counter() - t0:.2f}s  "
                    f"R1={len(r1)} R2={len(r2)} bars={len(bars)}"
                )

            r1, r2, bars, data_path = cache[key]
            shrink_mode = "Y" if shrink else "N"
            days = span_days(bars)
            times = [b.time for b in bars]
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            closes = [b.close for b in bars]

            if not audit_done and r1:
                if tf == "5m" and not shrink:
                    audit_lines = hand_audit_r1_short(r1, bars)
                    audit_done = True

            for rule, pool in (("R1", r1), ("R2", r2)):
                print(
                    f"Precomputing path metrics  {rule} {tf} sh={shrink_mode} "
                    f"n={len(pool)} ...",
                    flush=True,
                )
                t_m = time.perf_counter()
                pool_metrics = precompute_path_metrics(
                    pool, highs=highs, lows=lows, closes=closes
                )
                print(
                    f"  metrics done in {time.perf_counter() - t_m:.2f}s",
                    flush=True,
                )
                paired = list(zip(pool, pool_metrics))

                for min_h in MIN_HEIGHTS:
                    filtered_pairs = [
                        (s, m)
                        for s, m in paired
                        if s.zone_height_at_activation >= float(min_h)
                    ]
                    filtered = [s for s, _ in filtered_pairs]
                    filtered_metrics = [m for _, m in filtered_pairs]
                    seed = BASE_SEED + 10000 + config_idx
                    config_idx += 1
                    label = f"{rule}_{tf}_sh{shrink_mode}_h{min_h}"
                    print(
                        f"EDGE base {label} n={len(filtered)} seed={seed}",
                        flush=True,
                    )
                    result = run_edge_for(
                        filtered,
                        bars,
                        data_path,
                        timeframe=tf,
                        shrink_mode=shrink_mode,
                        seed=seed,
                        label=label,
                    )
                    eval_count += 1
                    seeds_used.append((label, seed))
                    row = {
                        "rule": rule,
                        "tf": tf,
                        "shrink": shrink_mode,
                        "min_height": min_h,
                        "kind": "base",
                        **result,
                        "signals_per_day": len(filtered) / days,
                        "median_stop_distance_points": median_stop(filtered),
                        "n_signals": len(filtered),
                    }
                    if not result.get("skipped"):
                        row["n_signals"] = result["n_signals"]
                        row["signals_per_day"] = result["signals_per_day"]
                    base_rows.append(row)

                    def _emit_split(
                        mod_name: str,
                        subset_pairs: list[tuple[RawSignal, dict[str, Any]]],
                    ) -> None:
                        nonlocal split_idx, eval_count
                        split_idx += 1
                        subset = [s for s, _ in subset_pairs]
                        subset_m = [m for _, m in subset_pairs]
                        seed_s = BASE_SEED + 20000 + split_idx
                        lab_s = f"{label}_{mod_name}"
                        print(
                            f"  split {mod_name} {lab_s} n={len(subset)} seed={seed_s}",
                            flush=True,
                        )
                        rs = measure_subset(
                            subset,
                            subset_m,
                            times=times,
                            highs=highs,
                            lows=lows,
                            closes=closes,
                            seed=seed_s,
                            label=lab_s,
                        )
                        eval_count += 1
                        seeds_used.append((lab_s, seed_s))
                        split_rows.append(
                            {
                                "parent": label,
                                "modifier": mod_name,
                                "rule": rule,
                                "tf": tf,
                                "shrink": shrink_mode,
                                "min_height": min_h,
                                **rs,
                                "median_stop_distance_points": median_stop(subset),
                            }
                        )

                    # R3 first touch
                    _emit_split(
                        "R3_first_touch",
                        [
                            (s, m)
                            for s, m in filtered_pairs
                            if s.n_partial_touches_before_entry == 0
                        ],
                    )
                    # R4 age floors
                    for age_n in AGE_FLOOR_SPLITS:
                        _emit_split(
                            f"R4_age>={age_n}",
                            [
                                (s, m)
                                for s, m in filtered_pairs
                                if s.zone_age_bars_at_entry >= age_n
                            ],
                        )
                    # R6 distance quartiles
                    if filtered:
                        dists = [
                            s.distance_from_close_at_confirm for s, _ in filtered_pairs
                        ]
                        q1, q2, q3 = s003_edge.quartile_edges(dists)
                        for qname in ("Q1", "Q2", "Q3", "Q4"):
                            _emit_split(
                                f"R6_{qname}",
                                [
                                    (s, m)
                                    for s, m in filtered_pairs
                                    if s003_edge.bucket_quartile(
                                        s.distance_from_close_at_confirm, q1, q2, q3
                                    )
                                    == qname
                                ],
                            )
                    else:
                        for qname in ("Q1", "Q2", "Q3", "Q4"):
                            _emit_split(f"R6_{qname}", [])

    if not audit_done:
        # fallback any cached R1
        for (_tf, _sh), (r1, _r2, bars, _) in cache.items():
            if r1:
                audit_lines = hand_audit_r1_short(r1, bars)
                break

    runtime = time.perf_counter() - t_all
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"ulp_edge_comparison_{stamp}.txt"

    base_sorted = sorted(
        base_rows,
        key=lambda r: (
            r["edge_over_baseline"] is None,
            -(r["edge_over_baseline"] or -1e18),
        ),
    )

    lines: list[str] = []
    lines.append("=== ULP entry rules — edge vs random baseline ===")
    lines.append(f"TOTAL_CONFIGURATIONS_EVALUATED: {eval_count}")
    lines.append(
        f"  breakdown: {len(base_rows)} base (R1/R2 x tf x shrink x min_height) "
        f"via s003_edge.run unchanged; "
        f"{len(split_rows)} modifier splits (R3/R4/R6) via same harness "
        f"helpers (compute_path_metrics + build_baseline + expectancy grid), "
        f"each with its own fresh seed/baseline"
    )
    lines.append(f"lookback: {lookback}")
    lines.append(f"timeframes: {list(TIMEFRAMES)}")
    lines.append(f"min_heights: {list(MIN_HEIGHTS)}")
    lines.append(f"runtime_s: {runtime:.2f}")
    lines.append("")
    lines.append(HOW_TO_READ)
    lines.append("")
    lines.append("--- SEEDS (every config) ---")
    for lab, seed in seeds_used:
        lines.append(f"  {lab}: {seed}")
    lines.append("")
    lines.append("--- BASE COMPARISON TABLE (sorted by edge_over_baseline desc) ---")
    lines.append(
        "rule tf sh  minH   n_sig   sig/d  medStop  h100s  h100b   E_sig   E_base   best(T,S)    edge  c23 c53  seed"
    )
    lines.append("-" * 120)
    for r in base_sorted:
        lines.append(format_row(r))
    lines.append("")
    lines.append("--- MODIFIER SPLITS (R3 / R4 / R6) — each has its own baseline ---")
    lines.append(
        "parent  modifier  n_sig  sig/d  medStop  h100s  h100b  E_sig  E_base  edge  c23 c53  seed"
    )
    lines.append("-" * 120)
    split_sorted = sorted(
        split_rows,
        key=lambda r: (
            r["edge_over_baseline"] is None,
            -(r["edge_over_baseline"] or -1e18),
        ),
    )
    for r in split_sorted:
        lines.append(
            f"{r['parent']}  {r['modifier']:<16}  "
            f"{r['n_signals']:>6}  {_fmt_num(r['signals_per_day'], 2):>6}  "
            f"{_fmt_num(r['median_stop_distance_points'], 1):>7}  "
            f"{_fmt_pct(r['hit_100_before_50_signal']):>6}  "
            f"{_fmt_pct(r['hit_100_before_50_baseline']):>6}  "
            f"{_fmt_num(r['best_expectancy_signal']):>7}  "
            f"{_fmt_num(r['best_expectancy_baseline']):>7}  "
            f"{_fmt_num(r['edge_over_baseline']):>7}  "
            f"{'Y' if r['clears_23'] else 'N'}  {'Y' if r['clears_53'] else 'N'}  "
            f"seed={r['seed']}"
        )
    lines.append("")
    lines.extend(audit_lines)
    lines.append("")
    lines.append("=== SUGGESTIONS (NOT tested this run) ===")
    lines.append(
        "- If R1 fails and R2 fails: do not invent hybrids here; next task only."
    )
    lines.append(
        "- One zone / one signal lifetime cap might cut re-entry spam on thin zones."
    )
    lines.append(
        "- Pairing stop_distance to the harness (T,S) grid (use natural stop as S) "
        "was not done; default S003 stops were used unchanged."
    )

    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    print()
    print(text)
    print(f"report: {out_path}")
    print(f"TOTAL_CONFIGURATIONS_EVALUATED: {eval_count}")
    print(f"runtime_s: {runtime:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
