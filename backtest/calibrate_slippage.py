#!/usr/bin/env python3
"""
Bucketed mark-vs-print slippage calibration (read-only, no HTTP).

Does NOT modify calibrate_mark_to_fill.py (baseline 1.65% stays reproducible).
Uses intersection months of options_trades SQLite shards + option_marks shards.

Output: console + backtest/results/calibrate_slippage.txt + .csv
"""

from __future__ import annotations

import csv
import logging
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402
import s001_income_engine as eng  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

MARKS_DIR = _BACKTEST / "cache" / "option_marks"
TRADES_DIR = _BACKTEST / "cache" / "options_trades"
CACHE_DIR = _BACKTEST / "cache"
OUT_TXT = _BACKTEST / "results" / "calibrate_slippage.txt"
OUT_CSV = _BACKTEST / "results" / "calibrate_slippage.csv"

MARK_TOL_SEC = 60
LOW_N = 200
BASELINE_SLIP_PCT = 1.65
# role in trades table: 0 = buyer was maker, 1 = buyer was taker
# Same convention as calibrate_mark_to_fill: role==1 → buy print, else sell.
BUY_ROLE = 1

logger = logging.getLogger("calibrate_slippage")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def pctile(vals: list[float], p: float) -> float:
    return float(eng.pctile(vals, p))


@dataclass
class MatchRow:
    diff_pct: float
    diff_abs: float
    side: str  # buy | sell
    dte_bucket: str
    prem_bucket: str
    hour_bucket: str
    vol_bucket: str
    dte: int
    premium: float
    ym: str


@dataclass
class BucketStats:
    key: str
    n: int
    median_pct: float
    mean_pct: float
    p25_pct: float
    p75_pct: float
    median_abs: float
    median_buy_pct: float
    median_sell_pct: float
    n_buy: int
    n_sell: int
    recommended_slip_pct: float
    low_n: bool
    note: str = ""


def list_intersection_months() -> tuple[list[str], list[str], list[str]]:
    marks = {
        p.stem.replace("marks_", "")
        for p in MARKS_DIR.glob("marks_*.sqlite")
    }
    trades = {
        p.stem.replace("opt_trades_", "")
        for p in TRADES_DIR.glob("opt_trades_*.sqlite")
    }
    both = sorted(marks & trades)
    marks_only = sorted(marks - trades)
    trades_only = sorted(trades - marks)
    return both, marks_only, trades_only


def dte_bucket(dte: int) -> str:
    if dte <= 0:
        return "0"
    if dte == 1:
        return "1"
    if dte == 2:
        return "2"
    if dte <= 7:
        return "3-7"
    return "8+"


def prem_bucket(px: float) -> str:
    if px < 100:
        return "<100"
    if px < 300:
        return "100-300"
    if px < 600:
        return "300-600"
    if px < 900:
        return "600-900"
    return "900+"


def hour_bucket_ist(ts: int) -> str:
    h = datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).hour
    if h <= 5:
        return "0-5"
    if h <= 11:
        return "6-11"
    if h <= 17:
        return "12-17"
    return "18-23"


def build_1h_rv(
    times: list[int], closes: list[float]
) -> dict[int, float]:
    """
    Per-minute 1h realised vol: sqrt(sum r^2) over prior 60 log-returns.
    Keyed by bar open_time_unix (same as times[]).
    """
    n = len(times)
    if n < 62:
        return {}
    log_ret = [0.0] * n
    for i in range(1, n):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            log_ret[i] = math.log(b / a)
    out: dict[int, float] = {}
    # rolling sum of squares
    ss = 0.0
    for i in range(1, n):
        ss += log_ret[i] * log_ret[i]
        if i >= 61:
            old = log_ret[i - 60]
            ss -= old * old
            out[times[i]] = math.sqrt(max(0.0, ss))
        elif i == 60:
            out[times[i]] = math.sqrt(max(0.0, ss))
    return out


def assign_vol_tertiles(
    rv_at_trade: list[float],
) -> tuple[float, float]:
    """Return (t33, t66) cutpoints from RV values at matched trades."""
    if len(rv_at_trade) < 3:
        return float("nan"), float("nan")
    s = sorted(rv_at_trade)
    t33 = pctile(s, 33.333)
    t66 = pctile(s, 66.667)
    return t33, t66


def vol_label(rv: float | None, t33: float, t66: float) -> str:
    if rv is None or not math.isfinite(rv) or not math.isfinite(t33):
        return "unknown"
    if rv <= t33:
        return "low"
    if rv <= t66:
        return "mid"
    return "high"


def lookup_mark(mark_map: dict[int, float], ts: int) -> float | None:
    minute = (ts // 60) * 60
    m = mark_map.get(minute)
    if m is not None and m > 0:
        return m
    m = mark_map.get(minute - 60)
    if m is not None and m > 0:
        return m
    m = mark_map.get(minute + 60)
    if m is not None and m > 0:
        return m
    return None


def summarize_bucket(
    key: str,
    rows: list[MatchRow],
    *,
    note: str = "",
) -> BucketStats:
    pcts = [r.diff_pct for r in rows]
    abss = [r.diff_abs for r in rows]
    buys = [r.diff_pct for r in rows if r.side == "buy"]
    sells = [r.diff_pct for r in rows if r.side == "sell"]
    n = len(rows)
    med_buy = float(statistics.median(buys)) if buys else float("nan")
    med_sell = float(statistics.median(sells)) if sells else float("nan")
    if math.isfinite(med_buy) and math.isfinite(med_sell):
        rec = 0.5 * (abs(med_buy) + abs(med_sell))
    elif math.isfinite(med_buy):
        rec = abs(med_buy)
    elif math.isfinite(med_sell):
        rec = abs(med_sell)
    else:
        rec = float("nan")
    low = n < LOW_N
    extra = note
    if low:
        tag = "LOW_N — use parent bucket"
        extra = f"{tag}" if not extra else f"{extra}; {tag}"
    return BucketStats(
        key=key,
        n=n,
        median_pct=float(statistics.median(pcts)) if pcts else float("nan"),
        mean_pct=float(statistics.fmean(pcts)) if pcts else float("nan"),
        p25_pct=pctile(pcts, 25) if pcts else float("nan"),
        p75_pct=pctile(pcts, 75) if pcts else float("nan"),
        median_abs=float(statistics.median(abss)) if abss else float("nan"),
        median_buy_pct=med_buy,
        median_sell_pct=med_sell,
        n_buy=len(buys),
        n_sell=len(sells),
        recommended_slip_pct=rec,
        low_n=low,
        note=extra,
    )


def fmt_stat(s: BucketStats) -> str:
    def f(x: float) -> str:
        return f"{x:.4f}" if math.isfinite(x) else "n/a"

    low = "  ** LOW_N — use parent bucket **" if s.low_n else ""
    note = f"  ({s.note})" if s.note and not s.low_n else ""
    return (
        f"{s.key:<28} n={s.n:8d}  med%={f(s.median_pct):>8}  "
        f"mean%={f(s.mean_pct):>8}  p25={f(s.p25_pct):>8}  p75={f(s.p75_pct):>8}  "
        f"med_abs$={f(s.median_abs):>8}  "
        f"rec_slip%={f(s.recommended_slip_pct):>7}  "
        f"buy_med%={f(s.median_buy_pct):>8} (n={s.n_buy})  "
        f"sell_med%={f(s.median_sell_pct):>8} (n={s.n_sell})"
        f"{low}{note}"
    )


def collect_matches(
    months: list[str],
    times: list[int],
    closes: list[float],
    rv_map: dict[int, float],
) -> tuple[list[MatchRow], dict[str, Any]]:
    rows: list[MatchRow] = []
    counts: dict[str, Any] = {
        "months": 0,
        "symbols": 0,
        "trades_seen": 0,
        "matched": 0,
        "no_mark": 0,
        "bad_price": 0,
        "bad_expiry": 0,
        "vol_t33": float("nan"),
        "vol_t66": float("nan"),
    }
    # first pass: gather RVs at candidate matches for tertile cutpoints later
    # We assign vol after tertiles known — store provisional rv, label later
    provisional: list[tuple[MatchRow, float | None]] = []

    for ym in months:
        counts["months"] += 1
        marks_path = MARKS_DIR / f"marks_{ym}.sqlite"
        trades_path = TRADES_DIR / f"opt_trades_{ym}.sqlite"
        logger.info("month=%s loading…", ym)
        marks = sqlite3.connect(f"file:{marks_path}?mode=ro", uri=True)
        trades = sqlite3.connect(f"file:{trades_path}?mode=ro", uri=True)
        try:
            syms = [
                str(r[0])
                for r in marks.execute("SELECT DISTINCT symbol FROM marks").fetchall()
            ]
            for si, symbol in enumerate(syms, 1):
                counts["symbols"] += 1
                mark_map = {
                    int(ts): float(close)
                    for ts, close in marks.execute(
                        "SELECT ts, close FROM marks WHERE symbol=?", (symbol,)
                    )
                    if close is not None and float(close) > 0
                }
                if not mark_map:
                    continue
                trows = trades.execute(
                    "SELECT ts, price, role, expiry FROM trades WHERE symbol=?",
                    (symbol,),
                ).fetchall()
                for ts_f, price, role, expiry_s in trows:
                    counts["trades_seen"] += 1
                    ts = int(float(ts_f))
                    px = float(price)
                    if px <= 0:
                        counts["bad_price"] += 1
                        continue
                    mark = lookup_mark(mark_map, ts)
                    if mark is None:
                        counts["no_mark"] += 1
                        continue
                    try:
                        exp = date.fromisoformat(str(expiry_s))
                    except ValueError:
                        counts["bad_expiry"] += 1
                        continue
                    trade_day = datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date()
                    dte = (exp - trade_day).days
                    diff_pct = 100.0 * (px - mark) / mark
                    diff_abs = px - mark
                    side = "buy" if int(role) == BUY_ROLE else "sell"
                    minute = (ts // 60) * 60
                    rv = rv_map.get(minute)
                    if rv is None:
                        # nearest prior bar with RV
                        i = _bisect_left(times, minute) - 1
                        if i >= 0:
                            rv = rv_map.get(times[i])
                    row = MatchRow(
                        diff_pct=diff_pct,
                        diff_abs=diff_abs,
                        side=side,
                        dte_bucket=dte_bucket(dte),
                        prem_bucket=prem_bucket(px),
                        hour_bucket=hour_bucket_ist(ts),
                        vol_bucket="pending",
                        dte=dte,
                        premium=px,
                        ym=ym,
                    )
                    provisional.append((row, rv))
                    counts["matched"] += 1
                if si % 200 == 0:
                    logger.info(
                        "  %s symbols %d/%d matched_so_far=%d",
                        ym,
                        si,
                        len(syms),
                        counts["matched"],
                    )
        finally:
            marks.close()
            trades.close()
        logger.info("month=%s done matched=%d", ym, counts["matched"])

    rvs = [rv for _, rv in provisional if rv is not None and math.isfinite(rv)]
    t33, t66 = assign_vol_tertiles(rvs)
    counts["vol_t33"] = t33
    counts["vol_t66"] = t66
    for row, rv in provisional:
        row.vol_bucket = vol_label(rv, t33, t66)
        rows.append(row)
    return rows, counts


def _bisect_left(a: list[int], x: int) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def filter_rows(
    rows: list[MatchRow],
    *,
    dte: str | None = None,
    prem: str | None = None,
    side: str | None = None,
    hour: str | None = None,
    vol: str | None = None,
    dte_in: set[str] | None = None,
) -> list[MatchRow]:
    out = rows
    if dte is not None:
        out = [r for r in out if r.dte_bucket == dte]
    if dte_in is not None:
        out = [r for r in out if r.dte_bucket in dte_in]
    if prem is not None:
        out = [r for r in out if r.prem_bucket == prem]
    if side is not None:
        out = [r for r in out if r.side == side]
    if hour is not None:
        out = [r for r in out if r.hour_bucket == hour]
    if vol is not None:
        out = [r for r in out if r.vol_bucket == vol]
    return out


def cross_median_table(
    rows: list[MatchRow],
    dte_keys: list[str],
    prem_keys: list[str],
) -> list[tuple[str, str, int, float]]:
    out: list[tuple[str, str, int, float]] = []
    for d in dte_keys:
        for p in prem_keys:
            sub = filter_rows(rows, dte=d, prem=p)
            med = (
                float(statistics.median([r.diff_pct for r in sub]))
                if sub
                else float("nan")
            )
            out.append((d, p, len(sub), med))
    return out


def run() -> list[str]:
    lines: list[str] = []
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    emit(lines, "===== CALIBRATE SLIPPAGE (bucketed mark vs print) =====")
    emit(lines, f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    emit(lines, f"baseline_symmetric_slip_pct={BASELINE_SLIP_PCT:.2f}%  (from prior all-expiry pool)")
    emit(lines, f"mark_tol_sec=±{MARK_TOL_SEC}")
    emit(lines, "role convention: role=1 buy (taker buyer), role=0 sell — same as calibrate_mark_to_fill")
    emit(lines)

    both, marks_only, trades_only = list_intersection_months()
    emit(lines, f"intersection_months ({len(both)}): {both}")
    emit(lines, f"marks_only ({len(marks_only)}): {marks_only}")
    emit(lines, f"trades_only ({len(trades_only)}): {trades_only}")
    parquets = sorted(CACHE_DIR.glob("options-trades-*.parquet"))
    emit(lines, f"parquet_files_in_cache={len(parquets)} (SQLite shards used for matching)")
    emit(lines)

    if not both:
        emit(lines, "ERROR: no overlapping months between marks and trades")
        return lines

    times, closes = ot.load_spot_1m()
    rv_map = build_1h_rv(times, closes)
    emit(lines, f"spot_bars={len(times)}  rv_minutes={len(rv_map)}")
    emit(lines)

    rows, counts = collect_matches(both, times, closes, rv_map)
    emit(lines, "----- MATCH COUNTS -----")
    for k in ("months", "symbols", "trades_seen", "matched", "no_mark", "bad_price", "bad_expiry"):
        emit(lines, f"  {k}={counts[k]}")
    emit(lines, f"  vol_tertile_cuts t33={counts.get('vol_t33')} t66={counts.get('vol_t66')}")
    emit(lines)

    # Overall (for summary vs 1.65%)
    overall = summarize_bucket("ALL", rows)
    buy_all = summarize_bucket("ALL_buy", filter_rows(rows, side="buy"))
    sell_all = summarize_bucket("ALL_sell", filter_rows(rows, side="sell"))

    emit(lines, "===== SUMMARY: baseline 1.65% vs new =====")
    emit(
        lines,
        f"  OLD baseline (pooled): {BASELINE_SLIP_PCT:.2f}% symmetric "
        f"(sell mark-X%, buy mark+X%)",
    )
    emit(
        lines,
        f"  NEW overall recommended_slip% = {overall.recommended_slip_pct:.4f}%  "
        f"(n={overall.n}, med_diff%={overall.median_pct:.4f})",
    )
    emit(
        lines,
        f"    buy med_diff%={buy_all.median_pct:.4f} (n={buy_all.n})  "
        f"sell med_diff%={sell_all.median_pct:.4f} (n={sell_all.n})",
    )
    emit(lines)

    csv_rows: list[dict[str, Any]] = []

    def add_stat(section: str, s: BucketStats) -> None:
        emit(lines, "  " + fmt_stat(s))
        csv_rows.append(
            {
                "section": section,
                "key": s.key,
                "n": s.n,
                "median_diff_pct": s.median_pct,
                "mean_diff_pct": s.mean_pct,
                "p25_diff_pct": s.p25_pct,
                "p75_diff_pct": s.p75_pct,
                "median_diff_abs": s.median_abs,
                "median_buy_pct": s.median_buy_pct,
                "median_sell_pct": s.median_sell_pct,
                "n_buy": s.n_buy,
                "n_sell": s.n_sell,
                "recommended_slip_pct": s.recommended_slip_pct,
                "low_n": int(s.low_n),
                "note": s.note,
            }
        )

    # A) DTE
    emit(lines, "===== A) DTE BUCKETS =====")
    for d in ("0", "1", "2", "3-7", "8+"):
        add_stat("A_dte", summarize_bucket(f"dte={d}", filter_rows(rows, dte=d)))
    emit(lines)

    # B) premium
    emit(lines, "===== B) PREMIUM BUCKETS =====")
    for p in ("<100", "100-300", "300-600", "600-900", "900+"):
        add_stat("B_premium", summarize_bucket(f"prem={p}", filter_rows(rows, prem=p)))
    emit(lines)

    # C) side
    emit(lines, "===== C) SIDE =====")
    add_stat("C_side", summarize_bucket("side=buy", filter_rows(rows, side="buy")))
    add_stat("C_side", summarize_bucket("side=sell", filter_rows(rows, side="sell")))
    emit(lines)

    # D) DTE x premium cross (median % and n only)
    emit(lines, "===== D) DTE x PREMIUM CROSS (median diff_pct, n) =====")
    dte_keys = ["0", "1", "2", "3-7", "8+"]
    prem_keys = ["<100", "100-300", "300-600", "600-900", "900+"]
    emit(
        lines,
        f"  {'dte':<6} "
        + " ".join(f"{p:>14}" for p in prem_keys),
    )
    cross = cross_median_table(rows, dte_keys, prem_keys)
    by_d: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    for d, p, n, med in cross:
        by_d[d].append((p, n, med))
        csv_rows.append(
            {
                "section": "D_cross",
                "key": f"dte={d}|prem={p}",
                "n": n,
                "median_diff_pct": med,
                "mean_diff_pct": float("nan"),
                "p25_diff_pct": float("nan"),
                "p75_diff_pct": float("nan"),
                "median_diff_abs": float("nan"),
                "median_buy_pct": float("nan"),
                "median_sell_pct": float("nan"),
                "n_buy": "",
                "n_sell": "",
                "recommended_slip_pct": float("nan"),
                "low_n": int(n < LOW_N),
                "note": "LOW_N — use parent bucket" if n < LOW_N else "",
            }
        )
    for d in dte_keys:
        cells = []
        for p, n, med in by_d[d]:
            med_s = f"{med:.3f}" if math.isfinite(med) else "n/a"
            tag = "*" if n < LOW_N else " "
            cells.append(f"{med_s}/{n}{tag:>1}".rjust(14))
        emit(lines, f"  {d:<6} " + " ".join(cells))
    emit(lines, "  (* = n<200 LOW_N)")
    emit(lines)

    # E) IST hour
    emit(lines, "===== E) IST HOUR BUCKETS =====")
    for h in ("0-5", "6-11", "12-17", "18-23"):
        add_stat("E_hour", summarize_bucket(f"hour={h}", filter_rows(rows, hour=h)))
    emit(lines)

    # F) vol tertiles
    emit(lines, "===== F) 1H REALISED VOL TERTILES =====")
    for v in ("low", "mid", "high", "unknown"):
        sub = filter_rows(rows, vol=v)
        if v == "unknown" and not sub:
            continue
        add_stat("F_vol", summarize_bucket(f"vol={v}", sub))
    emit(lines)

    # Highlight special strategy rows
    emit(lines, "===== HIGHLIGHT: STRATEGY-LIKE BUCKETS =====")
    specials = [
        (
            "S001_entry",
            "DTE 2, premium 100-300",
            filter_rows(rows, dte="2", prem="100-300"),
            "parent: dte=2 or prem=100-300",
        ),
        (
            "S001_adjustment",
            "DTE 0-1, premium 300-600",
            [
                r
                for r in rows
                if r.dte_bucket in {"0", "1"} and r.prem_bucket == "300-600"
            ],
            "parent: dte=0/1 or prem=300-600",
        ),
        (
            "S004_like",
            "DTE 0-1, premium 600-900",
            [
                r
                for r in rows
                if r.dte_bucket in {"0", "1"} and r.prem_bucket == "600-900"
            ],
            "parent: dte=0/1 or prem=600-900",
        ),
    ]
    for key, desc, sub, parent in specials:
        s = summarize_bucket(key, sub, note=desc)
        if s.low_n:
            s.note = f"{desc}; LOW_N — use parent bucket ({parent})"
        emit(lines, f"  >>> {desc}")
        add_stat("HIGHLIGHT", s)
        emit(lines)
    emit(lines)

    emit(
        lines,
        "NOTE: recommended_slip% = (|median_buy_diff%| + |median_sell_diff%|) / 2",
    )
    emit(
        lines,
        "  Apply as: buy fill ≈ mark*(1+slip/100), sell fill ≈ mark*(1-slip/100)",
    )
    emit(lines, f"  LOW_N threshold = {LOW_N}")

    # write CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "section",
        "key",
        "n",
        "median_diff_pct",
        "mean_diff_pct",
        "p25_diff_pct",
        "p75_diff_pct",
        "median_diff_abs",
        "median_buy_pct",
        "median_sell_pct",
        "n_buy",
        "n_sell",
        "recommended_slip_pct",
        "low_n",
        "note",
    ]
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in csv_rows:
            w.writerow(row)

    return lines


def main() -> int:
    lines = run()
    text = "\n".join(lines) + "\n"
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()
    logger.info("wrote %s", OUT_TXT)
    logger.info("wrote %s", OUT_CSV)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
