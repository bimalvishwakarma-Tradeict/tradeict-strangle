#!/usr/bin/env python3
"""
S003 Phase 2.2 — Replay 1m CSV through the live LSR4 engine (signals only).

Uses the SAME modules as the bot:
  backend.strategies.s003_lsr4.lsr4 / indicators / config
No trading, exits, or costs.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Allow `python backtest/s003_sim.py` from repo root or trading-bot/
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.strategies.s003_lsr4.config import Strategy3Config, config_from_dict
from backend.strategies.s003_lsr4.lsr4 import (
    BackfillDiagnostics,
    Candle,
    ChartTrace,
    LSR4Engine,
)

IST = ZoneInfo("Asia/Kolkata")
RESULTS_DIR = Path(__file__).resolve().parent / "results"

SIGNAL_COLS = [
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
]

ARM_COLS = [
    "arm_candle_utc",
    "arm_candle_ist",
    "outcome_candle_utc",
    "outcome_candle_ist",
    "side",
    "direction",
    "mode",
    "score",
    "outcome",
    "c_sweep",
    "c_wick",
    "c_volume",
    "c_vwap_ext",
    "c_rsi",
    "signal_extreme",
    "confirm_level",
    "atr_at_arm",
    "adx_at_arm",
    "rsi_at_arm",
    "vwap_at_arm",
    "volume_at_arm",
    "vol_sma_at_arm",
]


def _fmt_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S")


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _bool01(v: Any) -> str:
    return "1" if bool(v) else "0"


def load_config(path: Path) -> tuple[dict[str, Any], Strategy3Config]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = config_from_dict(raw)
    cfg.timeframe = str(raw.get("timeframe") or cfg.timeframe)
    return raw, cfg


def load_candles(
    data_file: Path,
    *,
    start: str | None,
    end: str | None,
) -> list[Candle]:
    start_ts = int(_parse_day(start).timestamp()) if start else None
    # inclusive end day → end of that UTC day
    end_ts = None
    if end:
        end_ts = int(_parse_day(end).timestamp()) + 86400 - 1

    out: list[Candle] = []
    with data_file.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["open_time_unix"])
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts > end_ts:
                continue
            out.append(
                Candle(
                    open_time=datetime.fromtimestamp(ts, tz=timezone.utc),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    return out


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return float(statistics.median(vals))


def write_signals_csv(path: Path, signals: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_COLS)
        w.writeheader()
        prev_confirm: int | None = None
        # signals are in emission order; bars_since needs candle index — stored as unix delta / 60
        for s in signals:
            arm_t = int(s["arm_time"])
            conf_t = int(s["time"])
            if prev_confirm is None:
                bars_since = ""
            else:
                bars_since = str(int((conf_t - prev_confirm) // 60))
            prev_confirm = conf_t
            dist = abs(float(s["confirm_price"]) - float(s["signal_extreme"]))
            w.writerow(
                {
                    "signal_candle_utc": _fmt_utc(arm_t),
                    "signal_candle_ist": _fmt_ist(arm_t),
                    "confirm_candle_utc": _fmt_utc(conf_t),
                    "confirm_candle_ist": _fmt_ist(conf_t),
                    "direction": s["direction"],
                    "mode": s["mode"],
                    "score": s["score"],
                    "c_sweep": _bool01(s.get("c_sweep")),
                    "c_wick": _bool01(s.get("c_wick")),
                    "c_volume": _bool01(s.get("c_volume")),
                    "c_vwap_ext": _bool01(s.get("c_vwap_ext")),
                    "c_rsi": _bool01(s.get("c_rsi")),
                    "confirm_price": f"{float(s['confirm_price']):.4f}",
                    "signal_extreme": f"{float(s['signal_extreme']):.4f}",
                    "distance_points": f"{dist:.4f}",
                    "atr_at_arm": f"{float(s['atr_at_arm']):.6f}",
                    "atr_at_confirm": f"{float(s.get('atr_at_confirm') or s['atr_at_arm']):.6f}",
                    "adx_at_signal": f"{float(s['adx_at_signal']):.6f}",
                    "rsi_at_arm": (
                        ""
                        if s.get("rsi_at_arm") is None
                        else f"{float(s['rsi_at_arm']):.6f}"
                    ),
                    "vwap_at_arm": (
                        ""
                        if s.get("vwap_at_arm") is None
                        else f"{float(s['vwap_at_arm']):.6f}"
                    ),
                    "volume_at_arm": (
                        ""
                        if s.get("volume_at_arm") is None
                        else f"{float(s['volume_at_arm']):.4f}"
                    ),
                    "vol_sma_at_arm": (
                        ""
                        if s.get("vol_sma_at_arm") is None
                        else f"{float(s['vol_sma_at_arm']):.6f}"
                    ),
                    "bars_since_previous_signal": bars_since,
                }
            )


def write_arms_csv(path: Path, arms: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ARM_COLS)
        w.writeheader()
        for a in arms:
            arm_t = int(a["arm_time"])
            out_t = a.get("outcome_time")
            w.writerow(
                {
                    "arm_candle_utc": _fmt_utc(arm_t),
                    "arm_candle_ist": _fmt_ist(arm_t),
                    "outcome_candle_utc": "" if out_t is None else _fmt_utc(int(out_t)),
                    "outcome_candle_ist": "" if out_t is None else _fmt_ist(int(out_t)),
                    "side": a.get("side"),
                    "direction": a.get("direction"),
                    "mode": a.get("mode"),
                    "score": a.get("score"),
                    "outcome": a.get("outcome"),
                    "c_sweep": _bool01(a.get("c_sweep")),
                    "c_wick": _bool01(a.get("c_wick")),
                    "c_volume": _bool01(a.get("c_volume")),
                    "c_vwap_ext": _bool01(a.get("c_vwap_ext")),
                    "c_rsi": _bool01(a.get("c_rsi")),
                    "signal_extreme": f"{float(a['signal_extreme']):.4f}",
                    "confirm_level": f"{float(a['confirm_level']):.4f}",
                    "atr_at_arm": f"{float(a['atr_at_arm']):.6f}",
                    "adx_at_arm": f"{float(a['adx_at_arm']):.6f}",
                    "rsi_at_arm": (
                        ""
                        if a.get("rsi_at_arm") is None
                        else f"{float(a['rsi_at_arm']):.6f}"
                    ),
                    "vwap_at_arm": (
                        ""
                        if a.get("vwap_at_arm") is None
                        else f"{float(a['vwap_at_arm']):.6f}"
                    ),
                    "volume_at_arm": (
                        ""
                        if a.get("volume_at_arm") is None
                        else f"{float(a['volume_at_arm']):.4f}"
                    ),
                    "vol_sma_at_arm": (
                        ""
                        if a.get("vol_sma_at_arm") is None
                        else f"{float(a['vol_sma_at_arm']):.6f}"
                    ),
                }
            )


def build_summary(
    *,
    candles: list[Candle],
    diag: BackfillDiagnostics,
    signals: list[dict[str, Any]],
    arms: list[dict[str, Any]],
    runtime_s: float,
) -> str:
    counters = diag.to_dict()["counters"]
    first = candles[0].open_time if candles else None
    last = candles[-1].open_time if candles else None

    by_month: Counter[str] = Counter()
    by_weekday: Counter[str] = Counter()
    by_day: Counter[str] = Counter()
    dirs: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    scores: Counter[int] = Counter()
    distances: list[float] = []
    comp_hits = {
        "c_sweep": 0,
        "c_wick": 0,
        "c_volume": 0,
        "c_vwap_ext": 0,
        "c_rsi": 0,
    }

    for s in signals:
        conf_t = int(s["time"])
        dt = datetime.fromtimestamp(conf_t, tz=IST)
        by_month[dt.strftime("%Y-%m")] += 1
        by_weekday[dt.strftime("%A")] += 1
        by_day[dt.strftime("%Y-%m-%d")] += 1
        dirs[str(s["direction"])] += 1
        modes[str(s["mode"])] += 1
        scores[int(s["score"])] += 1
        distances.append(abs(float(s["confirm_price"]) - float(s["signal_extreme"])))
        for k in comp_hits:
            if s.get(k):
                comp_hits[k] += 1

    day_counts = list(by_day.values())
    arm_n = len(arms)
    lines: list[str] = []
    lines.append("=== S003 Phase 2.2 backtest summary ===")
    lines.append(f"candles processed: {len(candles)}")
    lines.append(f"warm_from_index: {diag.warm_from_index}")
    lines.append(
        f"date range UTC: {first.isoformat() if first else 'n/a'} -> "
        f"{last.isoformat() if last else 'n/a'}"
    )
    lines.append(f"wall-clock runtime_s: {runtime_s:.3f}")
    lines.append("")
    lines.append("--- diagnostics funnel ---")
    for k in (
        "adx_below_trend",
        "adx_at_or_above_trend",
        "sweep_up_count",
        "sweep_dn_count",
        "score_hist",
        "score_hist_top",
        "score_hist_bottom",
        "arms_top",
        "arms_bottom",
        "invalidates",
        "expires",
        "confirms",
        "cooldown_blocks",
        "conflicts",
        "gaps",
        "emitted",
    ):
        lines.append(f"{k}: {counters.get(k)}")
    lines.append("")
    lines.append("--- funnel reconciliation ---")
    arms_sum = int(counters["arms_top"]) + int(counters["arms_bottom"])
    term = (
        int(counters["confirms"])
        + int(counters["invalidates"])
        + int(counters["expires"])
    )
    emitted_check = int(counters["confirms"]) - int(counters["cooldown_blocks"])
    lines.append(f"ARM == arms_top+arms_bottom: {arms_sum} (arms CSV rows={arm_n})")
    lines.append(
        f"CONFIRM+INVALIDATE+EXPIRE == ARM: {term} == {arms_sum} "
        f"({'OK' if term == arms_sum else 'MISMATCH'})"
    )
    lines.append(
        f"confirms - cooldown_blocks == emitted: "
        f"{emitted_check} == {counters['emitted']} "
        f"({'OK' if emitted_check == int(counters['emitted']) else 'MISMATCH'})"
    )
    lines.append(f"signals CSV rows: {len(signals)}")
    lines.append("")
    lines.append("--- signals per month (IST) ---")
    for m in sorted(by_month):
        lines.append(f"  {m}: {by_month[m]}")
    lines.append("--- signals per weekday (IST) ---")
    for wd in (
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ):
        lines.append(f"  {wd}: {by_weekday.get(wd, 0)}")
    if day_counts:
        lines.append(
            "signals per day: "
            f"min={min(day_counts)} median={_median([float(x) for x in day_counts]):.1f} "
            f"max={max(day_counts)}"
        )
    else:
        lines.append("signals per day: n/a")
    lines.append("")
    lines.append(f"LONG vs SHORT: {dirs.get('LONG', 0)} / {dirs.get('SHORT', 0)}")
    lines.append(
        f"RANGE vs EXHAUSTION: {modes.get('RANGE', 0)} / {modes.get('EXHAUSTION', 0)}"
    )
    lines.append("score distribution (emitted):")
    for sc in range(0, 6):
        lines.append(f"  score {sc}: {scores.get(sc, 0)}")
    if distances:
        lines.append(
            "distance_points: "
            f"min={min(distances):.2f} median={_median(distances):.2f} "
            f"mean={statistics.mean(distances):.2f} max={max(distances):.2f}"
        )
    else:
        lines.append("distance_points: n/a")
    n_sig = max(1, len(signals))
    lines.append("component fire rate among emitted signals:")
    for k, v in comp_hits.items():
        lines.append(f"  {k}: {v} / {len(signals)} ({100.0 * v / n_sig:.1f}%)")
    lines.append("")
    return "\n".join(lines)


def run(
    *,
    config_path: Path,
    start: str | None,
    end: str | None,
    data_file: Path | None = None,
    timeframe: str | None = None,
) -> tuple[Path, Path, Path, dict[str, Any]]:
    raw, cfg = load_config(config_path)

    if data_file is not None:
        resolved = Path(data_file)
    else:
        resolved = Path(raw["data_file"])
    if not resolved.is_file():
        alt = _ROOT / resolved
        if alt.is_file():
            resolved = alt
        else:
            raise FileNotFoundError(f"data_file not found: {resolved}")

    if timeframe is not None:
        tf = str(timeframe).strip()
        if tf not in {"1m", "3m", "5m", "15m"}:
            raise ValueError(f"timeframe must be one of 1m/3m/5m/15m, got {tf}")
        cfg.timeframe = tf
    else:
        tf = str(cfg.timeframe)

    candles = load_candles(resolved, start=start, end=end)
    if not candles:
        raise RuntimeError("No candles loaded for the requested window")

    diag = BackfillDiagnostics()
    chart = ChartTrace(record_bars=False)
    engine = LSR4Engine(cfg, diagnostics=diag, chart_trace=chart)

    t0 = time.perf_counter()
    for candle in candles:
        engine.process_closed_candle(candle)
    engine.finalize_warmup_diagnostics()
    runtime_s = time.perf_counter() - t0

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    sig_path = RESULTS_DIR / f"s003_signals_{tf}_{stamp}.csv"
    arm_path = RESULTS_DIR / f"s003_arms_{tf}_{stamp}.csv"
    sum_path = RESULTS_DIR / f"s003_summary_{tf}_{stamp}.txt"

    write_signals_csv(sig_path, chart.signals)
    write_arms_csv(arm_path, chart.arms)
    summary = build_summary(
        candles=candles,
        diag=diag,
        signals=chart.signals,
        arms=chart.arms,
        runtime_s=runtime_s,
    )
    sum_path.write_text(summary, encoding="utf-8")
    print(summary)
    print(f"signals: {sig_path}")
    print(f"arms:    {arm_path}")
    print(f"summary: {sum_path}")

    first = candles[0].open_time
    last = candles[-1].open_time
    span_days = max(
        (last - first).total_seconds() / 86400.0,
        1e-9,
    )
    meta = {
        "timeframe": tf,
        "data_file": str(resolved),
        "signals_path": sig_path,
        "arms_path": arm_path,
        "summary_path": sum_path,
        "signals_total": len(chart.signals),
        "candles": len(candles),
        "span_days": span_days,
        "signals_per_day": len(chart.signals) / span_days,
        "runtime_s": runtime_s,
        "emitted": int(diag.emitted),
    }
    return sig_path, arm_path, sum_path, meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="S003 LSR4 backtest simulator (signals only)")
    p.add_argument(
        "--config",
        default="backtest/s003_sim_config.json",
        help="Path to s003_sim_config.json",
    )
    p.add_argument("--start", default=None, help="UTC start date YYYY-MM-DD inclusive")
    p.add_argument("--end", default=None, help="UTC end date YYYY-MM-DD inclusive")
    p.add_argument(
        "--data-file",
        default=None,
        help="OHLCV CSV path (overrides config data_file)",
    )
    p.add_argument(
        "--timeframe",
        default=None,
        choices=["1m", "3m", "5m", "15m"],
        help="Engine timeframe (overrides config)",
    )
    args = p.parse_args(argv)
    try:
        run(
            config_path=Path(args.config),
            start=args.start,
            end=args.end,
            data_file=Path(args.data_file) if args.data_file else None,
            timeframe=args.timeframe,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
