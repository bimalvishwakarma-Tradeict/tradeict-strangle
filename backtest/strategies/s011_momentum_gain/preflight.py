#!/usr/bin/env python3
"""S011 PREFLIGHT — momentum 3-candle signal vs random breakeven-hit.

Measurement only. No P&L engine.

    python backtest\\strategies\\s011_momentum_gain\\preflight.py `
        --csv backtest\\data_1m\\BTCUSD_1m_20240630_20260921.csv `
        --out backtest\\strategies\\s011_momentum_gain\\runs `
        --max-days 10
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import sys
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection  # noqa: E402
from backtest.strategies.s011_momentum_gain import config as cfg  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s011.preflight")


@dataclass
class Bar1m:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Bar1h:
    ts_open: int
    ts_close_bar: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    gap: int
    direction: str
    c3_index: int
    signal_ts: int
    entry_ts: int
    spot_entry: float
    utc_hour: int
    day: date
    half: str
    lookahead_ok: bool


def _ist_date(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).date()


def _half_of(d: date) -> str:
    if cfg.H1_FROM <= d <= cfg.H1_TO:
        return "H1"
    if cfg.H2_FROM <= d <= cfg.H2_TO:
        return "H2"
    return "OUT"


def _pctile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    i = (len(s) - 1) * p
    lo = int(math.floor(i))
    hi = int(math.ceil(i))
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - i) + s[hi] * (i - lo)


def check_volume_column(csv_path: Path) -> tuple[bool, list[str]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
    has = any(x.lower() == "volume" for x in fields)
    return has, fields


def load_spot_1m(csv_path: Path) -> dict[int, Bar1m]:
    out: dict[int, Bar1m] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            vol_key = next(k for k in row if k.lower() == "volume")
            out[ts] = Bar1m(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row[vol_key]),
            )
    return out


def build_1h(spot: dict[int, Bar1m]) -> list[Bar1h]:
    buckets: dict[int, list[Bar1m]] = defaultdict(list)
    for ts, bar in spot.items():
        buckets[(int(ts) // 3600) * 3600].append(bar)
    hours: list[Bar1h] = []
    for ts_open in sorted(buckets):
        bars = sorted(buckets[ts_open], key=lambda b: b.ts)
        hours.append(
            Bar1h(
                ts_open=ts_open,
                ts_close_bar=bars[-1].ts,
                open=bars[0].open,
                high=max(b.high for b in bars),
                low=min(b.low for b in bars),
                close=bars[-1].close,
                volume=sum(b.volume for b in bars),
            )
        )
    return hours


def rsi_wilder(closes: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0.0)
        losses[i] = max(-d, 0.0)
    avg_g = sum(gains[1 : n + 1]) / float(n)
    avg_l = sum(losses[1 : n + 1]) / float(n)
    if avg_l <= 0:
        out[n] = 100.0 if avg_g > 0 else 50.0
    else:
        out[n] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(n + 1, len(closes)):
        avg_g = (avg_g * (n - 1) + gains[i]) / float(n)
        avg_l = (avg_l * (n - 1) + losses[i]) / float(n)
        if avg_l <= 0:
            out[i] = 100.0 if avg_g > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def _green(c: Bar1h) -> bool:
    return c.close > c.open


def _red(c: Bar1h) -> bool:
    return c.close < c.open


def classify_triple(
    c1: Bar1h, c2: Bar1h, c3: Bar1h, rsi: float | None
) -> dict[int, str]:
    """gap -> direction for every GAP that fires. Empty if none."""
    out: dict[int, str] = {}
    if rsi is None:
        return out
    vol_ok = c3.volume > c1.volume and c3.volume > c2.volume
    if not vol_ok:
        return out
    if _green(c1) and _green(c2) and _green(c3) and rsi > cfg.RSI_UP:
        move = c3.close - c1.open
        for gap in cfg.GAP_POINTS:
            if move >= gap:
                out[int(gap)] = "UP"
    elif _red(c1) and _red(c2) and _red(c3) and rsi < cfg.RSI_DN:
        move = c1.open - c3.close
        for gap in cfg.GAP_POINTS:
            if move >= gap:
                out[int(gap)] = "DOWN"
    return out


def detect_signals(
    hours: list[Bar1h],
    spot: dict[int, Bar1m],
    start_ts: int,
    cutoff_ts: int | None,
) -> tuple[list[Signal], Counter]:
    skips: Counter = Counter()
    closes = [h.close for h in hours]
    rsi = rsi_wilder(closes, cfg.RSI_LEN)
    signals: list[Signal] = []
    for i in range(2, len(hours)):
        c3 = hours[i]
        if c3.ts_close_bar < start_ts:
            continue
        if cutoff_ts is not None and c3.ts_open >= cutoff_ts:
            continue
        r = rsi[i]
        fired = classify_triple(hours[i - 2], hours[i - 1], c3, r)
        if not fired:
            continue
        trunc = hours[: i + 1]
        rsi_t = rsi_wilder([h.close for h in trunc], cfg.RSI_LEN)
        fired_t = classify_triple(
            trunc[-3], trunc[-2], trunc[-1], rsi_t[-1]
        )
        lookahead_ok = fired_t == fired
        if not lookahead_ok:
            skips["lookahead_mismatch"] += 1
            continue
        signal_ts = c3.ts_close_bar
        entry_ts = int(signal_ts) + 60
        bar = spot.get(entry_ts)
        if bar is None:
            skips["no_entry_bar"] += 1
            continue
        day = _ist_date(entry_ts)
        half = _half_of(day)
        if half == "OUT":
            skips["outside_h1_h2"] += 1
            continue
        utc_hour = datetime.fromtimestamp(entry_ts, tz=UTC).hour
        for gap, direction in fired.items():
            signals.append(
                Signal(
                    gap=gap,
                    direction=direction,
                    c3_index=i,
                    signal_ts=signal_ts,
                    entry_ts=entry_ts,
                    spot_entry=bar.close,
                    utc_hour=utc_hour,
                    day=day,
                    half=half,
                    lookahead_ok=True,
                )
            )
    return signals, skips


def expiry_of(entry_ts: int, dte_label: str) -> tuple[date, int]:
    off = cfg.DTE_OFFSETS[dte_label]
    d = _ist_date(entry_ts) + timedelta(days=off)
    ts = to_unix(ist_dt(d, cfg.EXPIRY_HOUR_IST, cfg.EXPIRY_MINUTE_IST))
    return d, ts


def hours_left(entry_ts: int, expiry_ts: int) -> float:
    return (int(expiry_ts) - int(entry_ts)) / 3600.0


def load_chain(
    store: MarksStore, expiry: date, ts: int
) -> tuple[dict[float, float], dict[float, float]]:
    """Expiry-month shard only (never timestamp-month)."""
    conn = store.conn(expiry)
    if conn is None:
        return {}, {}
    minute = (int(ts) // 60) * 60
    tol = cfg.MARK_TOL_SEC
    rows = conn.execute(
        """
        SELECT ts, strike, opt_type, close FROM marks
        WHERE expiry=? AND ts BETWEEN ? AND ?
          AND close IS NOT NULL AND close > 0
        """,
        (expiry.isoformat(), minute - tol, minute + tol),
    ).fetchall()
    best: dict[tuple[str, float], tuple[int, float]] = {}
    for ts_m, strike, opt, close in rows:
        ad = abs(int(ts_m) - minute)
        key = (str(opt).lower(), float(strike))
        prev = best.get(key)
        if prev is None or ad < prev[0]:
            best[key] = (ad, float(close))
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    for (opt, k), (_, px) in best.items():
        if opt.startswith("c"):
            calls[k] = px
        else:
            puts[k] = px
    return calls, puts


def atm_straddle(
    calls: dict[float, float], puts: dict[float, float], spot: float
) -> tuple[float, float] | None:
    both = sorted(set(calls) & set(puts))
    if not both:
        return None
    k = min(both, key=lambda x: abs(x - spot))
    b = calls[k] + puts[k]
    if b <= 0:
        return None
    return k, b


def t_years(entry_ts: int, expiry_ts: int) -> float:
    return max((int(expiry_ts) - int(entry_ts)) / cfg.SECONDS_PER_YEAR, 1e-12)


def pick_itm_delta(
    legs: dict[float, float],
    spot: float,
    t_yr: float,
    target: float,
    is_call: bool,
) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_err = float("inf")
    for k, mark in legs.items():
        if is_call and k >= spot:
            continue
        if (not is_call) and k <= spot:
            continue
        iv = implied_vol_bisection(mark, spot, k, t_yr, is_call)
        if iv is None:
            continue
        dlt = black76_abs_delta(spot, k, t_yr, iv, is_call)
        err = abs(dlt - target)
        if err < best_err:
            best_err = err
            intrinsic = max(spot - k, 0.0) if is_call else max(k - spot, 0.0)
            best = {
                "status": "ok",
                "strike": k,
                "mark": mark,
                "intrinsic": intrinsic,
                "extrinsic": mark - intrinsic,
                "delta": dlt,
            }
    if best is None:
        return {"status": "missing"}
    return best


def max_excursion(
    ts_arr: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    entry_ts: int,
    spot_entry: float,
    end_ts: int,
) -> float:
    lo = int(np.searchsorted(ts_arr, entry_ts, side="left"))
    hi = int(np.searchsorted(ts_arr, end_ts, side="right"))
    if lo >= hi:
        return float("nan")
    up = float(np.max(high[lo:hi]) - spot_entry)
    dn = float(spot_entry - np.min(low[lo:hi]))
    return max(up, dn, 0.0)


def bootstrap_edge(
    sig: list[float],
    sig_days: list[date],
    rnd: list[float],
    rnd_days: list[date],
    n: int,
    seed: int,
) -> tuple[float, float, float]:
    """Resample IST days with replacement (not individual rows)."""
    if not sig or not rnd or not sig_days or not rnd_days:
        return float("nan"), float("nan"), float("nan")
    sig_by: dict[date, list[float]] = defaultdict(list)
    rnd_by: dict[date, list[float]] = defaultdict(list)
    for h, d in zip(sig, sig_days):
        sig_by[d].append(h)
    for h, d in zip(rnd, rnd_days):
        rnd_by[d].append(h)
    sd = list(sig_by.keys())
    rd = list(rnd_by.keys())
    if not sd or not rd:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    diffs: list[float] = []
    nsd, nrd = len(sd), len(rd)
    for _ in range(n):
        s_vals: list[float] = []
        for _i in range(nsd):
            s_vals.extend(sig_by[sd[rng.randrange(nsd)]])
        r_vals: list[float] = []
        for _i in range(nrd):
            r_vals.extend(rnd_by[rd[rng.randrange(nrd)]])
        diffs.append((fmean(s_vals) - fmean(r_vals)) * 100.0)
    diffs.sort()
    edge = (fmean(sig) - fmean(rnd)) * 100.0
    return edge, _pctile(diffs, 0.025), _pctile(diffs, 0.975)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _from_json(obj: Any) -> Any:
    if obj is None:
        return float("nan")
    if isinstance(obj, dict):
        return {k: _from_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_json(v) for v in obj]
    return obj


def load_entry_cache(path: Path) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not path.is_file():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ts = int(rec["entry_ts"])
            out[ts] = {
                "spot_entry": float(rec["spot_entry"]),
                "rows": _from_json(rec["rows"]),
            }
    return out


def append_entry_cache(
    fh: Any, entry_ts: int, spot_entry: float, rows: list[dict[str, Any]]
) -> None:
    rec = {
        "entry_ts": int(entry_ts),
        "spot_entry": float(spot_entry),
        "rows": _json_safe(rows),
    }
    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    fh.flush()


def near_any(ts: int, sorted_ts: list[int], window_sec: int) -> bool:
    if not sorted_ts:
        return False
    i = bisect_left(sorted_ts, ts)
    for j in (i - 1, i):
        if 0 <= j < len(sorted_ts) and abs(sorted_ts[j] - ts) <= window_sec:
            return True
    return False


def measure_entry(
    store: MarksStore,
    ts_arr: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    entry_ts: int,
    spot_entry: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dte in cfg.DTE_LABELS:
        exp_d, exp_ts = expiry_of(entry_ts, dte)
        if dte == "0DTE" and hours_left(entry_ts, exp_ts) < cfg.MIN_0DTE_HOURS:
            rows.append({"dte": dte, "skip": "skipped_0dte_late"})
            continue
        if exp_ts <= entry_ts:
            rows.append({"dte": dte, "skip": "expiry_already_passed"})
            continue
        calls, puts = load_chain(store, exp_d, entry_ts)
        if not calls and not puts:
            rows.append({"dte": dte, "skip": "no_chain"})
            continue
        atm = atm_straddle(calls, puts, spot_entry)
        if atm is None:
            rows.append({"dte": dte, "skip": "no_atm"})
            continue
        _k, b = atm
        t_yr = t_years(entry_ts, exp_ts)
        delta_block: dict[str, Any] = {}
        for tgt in cfg.DELTA_TARGETS:
            tag = f"{tgt:.2f}"
            cleg = pick_itm_delta(calls, spot_entry, t_yr, tgt, True)
            pleg = pick_itm_delta(puts, spot_entry, t_yr, tgt, False)
            delta_block[tag] = {"call": cleg, "put": pleg}
        hits: dict[str, int] = {}
        ratios: dict[str, float] = {}
        exc: dict[str, float] = {}
        for hname in cfg.HORIZONS:
            sec = cfg.HORIZON_SEC[hname]
            end = exp_ts if sec is None else min(entry_ts + sec, exp_ts)
            mx = max_excursion(ts_arr, high, low, entry_ts, spot_entry, end)
            exc[hname] = mx
            if mx != mx:  # nan
                hits[hname] = 0
                ratios[hname] = float("nan")
            else:
                hits[hname] = int(mx >= b)
                ratios[hname] = mx / b if b > 0 else float("nan")
        rows.append(
            {
                "dte": dte,
                "skip": "",
                "expiry": exp_d.isoformat(),
                "B": b,
                "hits": hits,
                "ratios": ratios,
                "exc": exc,
                "delta": delta_block,
            }
        )
    return rows


def skips_from_rows(rows: list[dict[str, Any]]) -> Counter:
    c: Counter = Counter()
    for r in rows:
        sk = r.get("skip") or ""
        if sk:
            c[sk] += 1
            continue
        exc = r.get("exc") or {}
        for hname in cfg.HORIZONS:
            mx = exc.get(hname)
            if isinstance(mx, float) and mx != mx:
                c["no_spot_path"] += 1
    return c


def _mean_or_nan(xs: list[float]) -> float:
    ys = [x for x in xs if x == x]
    return fmean(ys) if ys else float("nan")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S011 momentum-gain preflight")
    ap.add_argument("--csv", default=cfg.DEFAULT_CSV)
    ap.add_argument(
        "--out",
        default="backtest/strategies/s011_momentum_gain/runs",
    )
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="ignore and rewrite runs/s011_preflight_cache.jsonl",
    )
    args = ap.parse_args()
    csv_path = Path(args.csv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = []

    has_vol, fields = check_volume_column(csv_path)
    lines.append("===== S011 PREFLIGHT (no P&L engine) =====")
    lines.append(f"csv={csv_path} columns={fields}")
    if not has_vol:
        lines.append("STOP: no volume column. Volume was not invented.")
        tpath = out_dir / f"s011_preflight_{stamp}.txt"
        tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines), flush=True)
        return 1

    t0 = time.monotonic()
    print("loading spot...", flush=True)
    spot = load_spot_1m(csv_path)
    hours = build_1h(spot)
    start_ts = to_unix(datetime(cfg.DATA_START.year, cfg.DATA_START.month, cfg.DATA_START.day, tzinfo=UTC))
    cutoff_ts = None
    if args.max_days and args.max_days > 0:
        cutoff_d = cfg.DATA_START + timedelta(days=int(args.max_days))
        cutoff_ts = to_unix(datetime(cutoff_d.year, cutoff_d.month, cutoff_d.day, tzinfo=UTC))
        lines.append(f"max_days={args.max_days} cutoff_utc={cutoff_d.isoformat()}")
    print(f"1m_bars={len(spot)} 1h_bars={len(hours)}", flush=True)

    signals, skip_det = detect_signals(hours, spot, start_ts, cutoff_ts)
    skips: Counter = Counter(skip_det)
    print(f"signals={len(signals)}", flush=True)

    ts_sorted = np.array(sorted(spot), dtype=np.int64)
    high_arr = np.array([spot[int(t)].high for t in ts_sorted], dtype=np.float64)
    low_arr = np.array([spot[int(t)].low for t in ts_sorted], dtype=np.float64)

    signal_ts_by_gap: dict[int, list[int]] = {}
    for gap in cfg.GAP_POINTS:
        signal_ts_by_gap[int(gap)] = sorted(
            {s.entry_ts for s in signals if s.gap == gap}
        )
    exclude_sec = int(cfg.RANDOM_EXCLUDE_HOURS * 3600)

    hour_slots: dict[int, list[tuple[date, int, float]]] = defaultdict(list)
    seen_slot: set[tuple[int, date]] = set()
    for ts, bar in spot.items():
        if ts < start_ts:
            continue
        if cutoff_ts is not None and ts >= cutoff_ts:
            continue
        dt = datetime.fromtimestamp(int(ts), tz=UTC)
        if dt.minute != 0:
            continue
        day = _ist_date(ts)
        if _half_of(day) == "OUT":
            continue
        key = (dt.hour, day)
        if key in seen_slot:
            continue
        seen_slot.add(key)
        hour_slots[dt.hour].append((day, int(ts), bar.close))

    pool_size: dict[int, int] = {}
    for gap in cfg.GAP_POINTS:
        sts = signal_ts_by_gap[int(gap)]
        n_ok = 0
        for _h, slots in hour_slots.items():
            for _day, rts, _sp in slots:
                if not near_any(rts, sts, exclude_sec):
                    n_ok += 1
        pool_size[int(gap)] = n_ok

    cache_path = out_dir / cfg.CACHE_NAME
    if args.fresh and cache_path.is_file():
        cache_path.unlink()
        print(f"fresh: removed {cache_path}", flush=True)
    mem_cache = load_entry_cache(cache_path)
    loaded_from_disk = set(mem_cache.keys())
    print(f"cache_loaded={len(mem_cache)} path={cache_path}", flush=True)
    cache_fh = cache_path.open("a", encoding="utf-8")

    store = MarksStore()
    n_unique_done = 0
    n_cache_skip = 0
    n_measured = 0
    seen_progress: set[int] = set()

    def get_meas(entry_ts: int, spot_entry: float) -> list[dict[str, Any]]:
        nonlocal n_unique_done, n_cache_skip, n_measured
        ts = int(entry_ts)
        hit = mem_cache.get(ts)
        if hit is not None:
            if ts not in seen_progress:
                seen_progress.add(ts)
                n_unique_done += 1
                if ts in loaded_from_disk:
                    n_cache_skip += 1
                if n_unique_done % cfg.UNIQUE_ENTRY_PROGRESS == 0:
                    print(
                        f"  unique_entries={n_unique_done} "
                        f"cache_skip={n_cache_skip} measured={n_measured} "
                        f"elapsed_s={time.monotonic() - t0:.1f}",
                        flush=True,
                    )
            return hit["rows"]
        rows = measure_entry(
            store, ts_sorted, high_arr, low_arr, entry_ts, spot_entry
        )
        mem_cache[ts] = {
            "spot_entry": float(spot_entry),
            "rows": rows,
        }
        append_entry_cache(cache_fh, entry_ts, spot_entry, rows)
        seen_progress.add(ts)
        n_measured += 1
        n_unique_done += 1
        if n_unique_done % cfg.UNIQUE_ENTRY_PROGRESS == 0:
            print(
                f"  unique_entries={n_unique_done} "
                f"cache_skip={n_cache_skip} measured={n_measured} "
                f"elapsed_s={time.monotonic() - t0:.1f}",
                flush=True,
            )
        return rows

    csv_rows: list[dict[str, Any]] = []
    sig_hit: dict[tuple, list[float]] = defaultdict(list)
    sig_hit_days: dict[tuple, list[date]] = defaultdict(list)
    sig_ratio: dict[tuple, list[float]] = defaultdict(list)
    sig_b: dict[tuple, list[float]] = defaultdict(list)
    sig_b_ge: dict[tuple, list[float]] = defaultdict(list)
    rnd_hit: dict[tuple, dict[int, list[float]]] = defaultdict(
        lambda: {s: [] for s in cfg.RANDOM_SEEDS}
    )
    rnd_hit_days: dict[tuple, dict[int, list[date]]] = defaultdict(
        lambda: {s: [] for s in cfg.RANDOM_SEEDS}
    )
    rnd_ratio: dict[tuple, dict[int, list[float]]] = defaultdict(
        lambda: {s: [] for s in cfg.RANDOM_SEEDS}
    )
    rnd_b: dict[tuple, dict[int, list[float]]] = defaultdict(
        lambda: {s: [] for s in cfg.RANDOM_SEEDS}
    )
    rnd_b_ge: dict[tuple, dict[int, list[float]]] = defaultdict(
        lambda: {s: [] for s in cfg.RANDOM_SEEDS}
    )
    delta_cap: dict[str, list[float]] = defaultdict(list)
    delta_ext: dict[str, list[float]] = defaultdict(list)
    delta_miss = Counter()
    delta_n = Counter()
    delta_done: set[tuple[int, str]] = set()
    skip_counted: set[int] = set()

    for sig in signals:
        meas = get_meas(sig.entry_ts, sig.spot_entry)
        if sig.entry_ts not in skip_counted:
            skips.update(skips_from_rows(meas))
            skip_counted.add(sig.entry_ts)
        for row in meas:
            dte = row["dte"]
            rec = {
                "kind": "signal",
                "gap": sig.gap,
                "direction": sig.direction,
                "day": sig.day.isoformat(),
                "half": sig.half,
                "entry_ts": sig.entry_ts,
                "spot_entry": sig.spot_entry,
                "dte": dte,
                "skip": row.get("skip") or "",
                "B": row.get("B", ""),
                "expiry": row.get("expiry", ""),
            }
            if not row.get("skip"):
                for hname in cfg.HORIZONS:
                    rec[f"hit_{hname}"] = row["hits"][hname]
                    rec[f"exc_{hname}"] = row["exc"][hname]
                    rec[f"ratio_{hname}"] = row["ratios"][hname]
                    key = (sig.gap, dte, hname, sig.half)
                    sig_hit[key].append(float(row["hits"][hname]))
                    sig_hit_days[key].append(sig.day)
                    if row["ratios"][hname] == row["ratios"][hname]:
                        sig_ratio[key].append(float(row["ratios"][hname]))
                    sig_b[key].append(float(row["B"]))
                sig_b_ge[(sig.gap, dte)].append(float(row["B"]))
                dkey = (sig.entry_ts, str(dte))
                if dkey not in delta_done:
                    delta_done.add(dkey)
                    for tgt, blob in row["delta"].items():
                        cleg, pleg = blob["call"], blob["put"]
                        delta_n[tgt] += 2
                        for side, leg in (("c", cleg), ("p", pleg)):
                            rec[f"d{tgt}_{side}_status"] = leg["status"]
                            if leg["status"] != "ok":
                                delta_miss[tgt] += 1
                                rec[f"d{tgt}_{side}_mark"] = ""
                                continue
                            rec[f"d{tgt}_{side}_strike"] = leg["strike"]
                            rec[f"d{tgt}_{side}_mark"] = leg["mark"]
                            rec[f"d{tgt}_{side}_intr"] = leg["intrinsic"]
                            rec[f"d{tgt}_{side}_ext"] = leg["extrinsic"]
                        if cleg["status"] == "ok" and pleg["status"] == "ok":
                            delta_cap[tgt].append(cleg["mark"] + pleg["mark"])
                            delta_ext[tgt].append(
                                cleg["extrinsic"] + pleg["extrinsic"]
                            )
            csv_rows.append(rec)

        sts = signal_ts_by_gap[sig.gap]
        pool = [
            t
            for t in hour_slots.get(sig.utc_hour, [])
            if not near_any(t[1], sts, exclude_sec)
        ]
        if len(pool) < cfg.RANDOM_N:
            skips["random_pool_short"] += 1
        for seed in cfg.RANDOM_SEEDS:
            rng = random.Random(seed + sig.entry_ts + sig.gap)
            take = list(pool)
            rng.shuffle(take)
            chosen = take[: cfg.RANDOM_N]
            if len(chosen) < cfg.RANDOM_N:
                skips["random_underfilled"] += 1
            for _day, rts, rspot in chosen:
                rmeas = get_meas(rts, rspot)
                if rts not in skip_counted:
                    skips.update(skips_from_rows(rmeas))
                    skip_counted.add(rts)
                for row in rmeas:
                    if row.get("skip"):
                        continue
                    dte = row["dte"]
                    csv_rows.append(
                        {
                            "kind": f"random_{seed}",
                            "gap": sig.gap,
                            "direction": sig.direction,
                            "day": _day.isoformat(),
                            "half": _half_of(_day),
                            "entry_ts": rts,
                            "spot_entry": rspot,
                            "dte": dte,
                            "skip": "",
                            "B": row["B"],
                            "expiry": row.get("expiry", ""),
                            **{
                                f"hit_{h}": row["hits"][h] for h in cfg.HORIZONS
                            },
                            **{
                                f"ratio_{h}": row["ratios"][h]
                                for h in cfg.HORIZONS
                            },
                        }
                    )
                    rhalf = _half_of(_day)
                    for hname in cfg.HORIZONS:
                        key = (sig.gap, dte, hname, rhalf)
                        rnd_hit[key][seed].append(float(row["hits"][hname]))
                        rnd_hit_days[key][seed].append(_day)
                        if row["ratios"][hname] == row["ratios"][hname]:
                            rnd_ratio[key][seed].append(
                                float(row["ratios"][hname])
                            )
                        rnd_b[key][seed].append(float(row["B"]))
                    rnd_b_ge[(sig.gap, dte)][seed].append(float(row["B"]))

    cache_fh.close()
    store.close()
    print(
        f"unique_entries={n_unique_done} cache_skip={n_cache_skip} "
        f"measured={n_measured} elapsed_s={time.monotonic() - t0:.1f}",
        flush=True,
    )

    # counts
    lines.append(f"DATA_START={cfg.DATA_START.isoformat()} volume_column=yes")
    lines.append(
        f"GAP_POINTS={list(cfg.GAP_POINTS)} RSI_LEN={cfg.RSI_LEN} "
        f"RSI_UP={cfg.RSI_UP} RSI_DN={cfg.RSI_DN}"
    )
    lines.append(f"n_signals={len(signals)}")
    lines.append("--- unique signal entry times per GAP ---")
    for gap in cfg.GAP_POINTS:
        n_u = len({s.entry_ts for s in signals if s.gap == gap})
        lines.append(f"  gap={gap}: unique_entries={n_u}")
    lines.append(
        f"--- random pool (exclude +/-{cfg.RANDOM_EXCLUDE_HOURS}h of any "
        "signal entry_ts for that GAP; same UTC hour) ---"
    )
    for gap in cfg.GAP_POINTS:
        lines.append(f"  gap={gap}: pool_size={pool_size[int(gap)]}")
    lines.append("--- signals sharing an IST day with another signal (same GAP) ---")
    for gap in cfg.GAP_POINTS:
        gs = [s for s in signals if s.gap == gap]
        day_n = Counter(s.day for s in gs)
        n_share = sum(1 for s in gs if day_n[s.day] > 1)
        n_days = len(day_n)
        n_multi = sum(1 for v in day_n.values() if v > 1)
        lines.append(
            f"  gap={gap}: share_day_signals={n_share}/{len(gs)} "
            f"multi_days={n_multi}/{n_days}"
        )
    by_gd: dict[tuple[int, str], int] = Counter()
    by_month: dict[tuple[int, str], int] = Counter()
    by_half: dict[tuple[int, str, str], int] = Counter()
    for s in signals:
        by_gd[(s.gap, s.direction)] += 1
        by_month[(s.gap, s.day.strftime("%Y-%m"))] += 1
        by_half[(s.gap, s.direction, s.half)] += 1
    lines.append("--- signal counts GAP x direction ---")
    for gap in cfg.GAP_POINTS:
        for d in ("UP", "DOWN"):
            lines.append(f"  gap={gap} {d}: {by_gd[(gap, d)]}")
    lines.append("--- per month (any direction) ---")
    for gap in cfg.GAP_POINTS:
        months = sorted({m for g, m in by_month if g == gap})
        parts = [f"{m}={by_month[(gap, m)]}" for m in months]
        lines.append(f"  gap={gap}: " + (", ".join(parts) if parts else "0"))
    lines.append("--- per half ---")
    for gap in cfg.GAP_POINTS:
        for d in ("UP", "DOWN"):
            lines.append(
                f"  gap={gap} {d} H1={by_half[(gap, d, 'H1')]} "
                f"H2={by_half[(gap, d, 'H2')]}"
            )

    lines.append("--- hit% / edge by (GAP, expiry, horizon) x half ---")
    pass_cells_h1: set[tuple] = set()
    pass_cells_h2: set[tuple] = set()
    for gap in cfg.GAP_POINTS:
        for dte in cfg.DTE_LABELS:
            for hname in cfg.HORIZONS:
                for half in ("H1", "H2"):
                    key = (gap, dte, hname, half)
                    sh = sig_hit.get(key, [])
                    # pooled random across both seeds
                    rh: list[float] = []
                    rdays: list[date] = []
                    for seed in cfg.RANDOM_SEEDS:
                        rh.extend(rnd_hit[key][seed])
                        rdays.extend(rnd_hit_days[key][seed])
                    sig_pct = 100.0 * fmean(sh) if sh else float("nan")
                    per_seed: list[str] = []
                    for seed in cfg.RANDOM_SEEDS:
                        rs = rnd_hit[key][seed]
                        rp = 100.0 * fmean(rs) if rs else float("nan")
                        per_seed.append(
                            f"rand{seed}={rp:.2f}% n={len(rs)}"
                        )
                    rnd_pct = 100.0 * fmean(rh) if rh else float("nan")
                    edge, lo, hi = bootstrap_edge(
                        sh,
                        sig_hit_days.get(key, []),
                        rh,
                        rdays,
                        cfg.BOOTSTRAP_N,
                        cfg.BOOTSTRAP_SEED,
                    )
                    mb = _mean_or_nan(sig_b.get(key, []))
                    mbr = _mean_or_nan(
                        [x for seed in cfg.RANDOM_SEEDS for x in rnd_b[key][seed]]
                    )
                    mr = _mean_or_nan(sig_ratio.get(key, []))
                    mrr = _mean_or_nan(
                        [
                            x
                            for seed in cfg.RANDOM_SEEDS
                            for x in rnd_ratio[key][seed]
                        ]
                    )
                    lines.append(
                        f"  gap={gap} {dte} {hname} {half}: "
                        f"sig_hit={sig_pct:.2f}% n={len(sh)} "
                        f"rand_pooled={rnd_pct:.2f}% n={len(rh)} "
                        + " ".join(per_seed)
                        + f" edge={edge:.2f}pp CI=[{lo:.2f},{hi:.2f}] "
                        f"meanB_sig={mb:.2f} meanB_rand={mbr:.2f} "
                        f"exc/B_sig={mr:.3f} exc/B_rand={mrr:.3f}"
                    )
                    ok = (
                        sh
                        and rh
                        and edge == edge
                        and lo == lo
                        and edge >= cfg.EDGE_PASS_PP
                        and lo > 0
                    )
                    if ok:
                        cell = (gap, dte, hname)
                        if half == "H1":
                            pass_cells_h1.add(cell)
                        else:
                            pass_cells_h2.add(cell)

    lines.append("--- mean B signal vs random per (GAP, expiry) ---")
    for gap in cfg.GAP_POINTS:
        for dte in cfg.DTE_LABELS:
            sb = sig_b_ge.get((gap, dte), [])
            rb = [
                x
                for seed in cfg.RANDOM_SEEDS
                for x in rnd_b_ge[(gap, dte)][seed]
            ]
            lines.append(
                f"  gap={gap} {dte}: meanB_sig={_mean_or_nan(sb):.2f} n={len(sb)} "
                f"meanB_rand={_mean_or_nan(rb):.2f} n={len(rb)}"
            )

    lines.append("--- delta 0.75 / 0.90 (signal entries, both legs) ---")
    for tgt in cfg.DELTA_TARGETS:
        tag = f"{tgt:.2f}"
        n = delta_n[tag]
        miss = delta_miss[tag]
        share = 100.0 * miss / n if n else float("nan")
        lines.append(
            f"  delta={tag} mean_capital={_mean_or_nan(delta_cap[tag]):.2f} "
            f"mean_extrinsic={_mean_or_nan(delta_ext[tag]):.2f} "
            f"missing={miss}/{n} ({share:.1f}%)"
        )

    lines.append("--- skip reasons ---")
    if skips:
        for k, v in sorted(skips.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")

    both = pass_cells_h1 & pass_cells_h2
    if both:
        lines.append("PASS")
        lines.append(
            "pass_cells="
            + ", ".join(f"gap={g} {d} {h}" for g, d, h in sorted(both))
        )
    else:
        lines.append("FAIL")
        lines.append(
            "no (GAP, expiry, horizon) had edge>=+5.0pp and CI_lo>0 in BOTH H1 and H2"
        )

    elapsed = time.monotonic() - t0

    tpath = out_dir / f"s011_preflight_{stamp}.txt"
    cpath = out_dir / f"s011_preflight_{stamp}.csv"
    tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if csv_rows:
        keys: list[str] = []
        seen_k: set[str] = set()
        for r in csv_rows:
            for k in r:
                if k not in seen_k:
                    seen_k.add(k)
                    keys.append(k)
        with cpath.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(csv_rows)
    else:
        cpath.write_text("kind\n", encoding="utf-8")
    for ln in lines:
        print(ln, flush=True)
    print(f"txt={tpath}", flush=True)
    print(f"csv={cpath}", flush=True)
    print(f"DONE in {elapsed:.1f}s signals={len(signals)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
