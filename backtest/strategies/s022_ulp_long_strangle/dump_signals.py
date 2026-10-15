#!/usr/bin/env python3
"""Dump S022 ULP touch signals for a calendar month (no option sim).

python backtest/strategies/s022_ulp_long_strangle/dump_signals.py --month 2025-06
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.harness.run_archive import RunArchive  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.signals import (  # noqa: E402
    TF_SEC,
    IST,
    iter_ulp_signals,
)

PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
SIG_COLS = (
    "signal_ts",
    "signal_ist",
    "tf",
    "mode",
    "rule",
    "zone_kind",
    "pivot_ts",
    "confirm_ts",
    "age_bars",
    "zone_top",
    "zone_bottom",
    "zone_height",
    "open",
    "high",
    "low",
    "close",
    "spot_close",
    "n_active_zones",
)


def _bars_from_ohlc(
    tts: Any,
    to_: Any,
    th: Any,
    tl: Any,
    tc: Any,
) -> list[dict[str, Any]]:
    return [
        {
            "time": int(t),
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
        }
        for t, o, h, lo, c in zip(tts, to_, th, tl, tc)
    ]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(float(x) for x in xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return s[lo]
    w = k - lo
    return s[lo] * (1.0 - w) + s[hi] * w


def _median(xs: list[float]) -> float:
    return _pct(xs, 50.0)


def _ist_date(ts: int) -> Any:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST).date()


def _summarize(
    rows: list[dict[str, Any]],
    n_days: int,
) -> dict[str, Any]:
    n = len(rows)
    by_k: dict[str, int] = defaultdict(int)
    for r in rows:
        by_k[str(r["zone_kind"])] += 1
    heights = [float(r["zone_height"]) for r in rows]
    ages = [float(r["age_bars"]) for r in rows]
    n_age20 = sum(1 for r in rows if int(r["age_bars"]) == 20)
    return {
        "n": n,
        "signals_per_day": (n / n_days) if n_days else float("nan"),
        "n_high": by_k.get("high", 0),
        "n_low": by_k.get("low", 0),
        "median_zone_height": _median(heights),
        "age_p10": _pct(ages, 10),
        "age_p50": _pct(ages, 50),
        "age_p90": _pct(ages, 90),
        "pct_any_age20": (100.0 * n_age20 / n) if n else float("nan"),
        "n_age20": n_age20,
    }


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return "nan"
        return f"{x:.4f}"
    return str(x)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    args = ap.parse_args()
    month = str(args.month)
    win_from, win_to = d.month_bounds(month)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    n_days = int((win_to - win_from).days + 1)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = RUNS_DIR / f"{stamp}_signals_{month}"
    n = 0
    while folder.exists():
        n += 1
        folder = RUNS_DIR / f"{stamp}_signals_{month}_{n}"

    archive = RunArchive(
        "s022",
        "signals",
        args,
        month=month,
        prereg_note="ULP touch signal dump (chart compute_ulp parity); no option sim",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    lo = start_ts - 7 * 86400
    hi = cutoff + 1 * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    print(f"1m bars={len(ts)} pad lo={lo} hi={hi}", flush=True)

    all_rows: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for tf in TFS:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        print(f"tf={tf} bars={len(bars)} tf_sec={tf_sec}", flush=True)
        for mode in MODES:
            raw = iter_ulp_signals(bars, mode, tf)
            kept = [
                r
                for r in raw
                if start_ts <= int(r["signal_ts"]) < cutoff
            ]
            groups[(tf, mode)] = kept
            all_rows.extend(kept)
            print(f"  mode={mode} signals={len(kept)} (raw={len(raw)})", flush=True)

    csv_path = archive.folder / "signals.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(SIG_COLS), extrasaction="ignore")
        w.writeheader()
        for r in sorted(all_rows, key=lambda x: (int(x["signal_ts"]), x["tf"], x["mode"], x["rule"])):
            w.writerow({c: r.get(c, "") for c in SIG_COLS})

    from openpyxl import Workbook

    wb = Workbook()
    first = True
    for tf in TFS:
        for mode in MODES:
            title = f"{tf}_{mode}"[:31]
            ws = wb.active if first else wb.create_sheet(title)
            if first:
                ws.title = title
                first = False
            ws.append(list(SIG_COLS))
            for r in groups[(tf, mode)]:
                ws.append([r.get(c, "") for c in SIG_COLS])
    xlsx_path = archive.folder / "signals.xlsx"
    wb.save(xlsx_path)

    lines: list[str] = [
        f"S022 ULP touch signals  month={month}  folder={archive.folder}",
        f"lookback={10} maxLevels={100}  tfs={list(TFS)}  modes={list(MODES)}",
        f"calendar_days={n_days}  window={win_from}..{win_to} IST",
        f"candle_source={args.csv}  resample=run_s020_dev.resample_tf (unix bucket)",
        "",
    ]
    for tf in TFS:
        for mode in MODES:
            rows = groups[(tf, mode)]
            for rule in ("fresh", "any"):
                sub = [r for r in rows if r["rule"] == rule]
                st = _summarize(sub, n_days)
                lines.append(f"[{tf} {mode} {rule}]")
                lines.append(
                    f"  n={st['n']}  signals/day={_fmt(st['signals_per_day'])}  "
                    f"high={st['n_high']} low={st['n_low']}"
                )
                lines.append(
                    f"  median_zone_height={_fmt(st['median_zone_height'])}  "
                    f"age_bars p10/p50/p90="
                    f"{_fmt(st['age_p10'])}/{_fmt(st['age_p50'])}/{_fmt(st['age_p90'])}"
                )
                if rule == "any":
                    lines.append(
                        f"  pct_fired_at_age_20={_fmt(st['pct_any_age20'])}%  "
                        f"(n_age20={st['n_age20']}; pre-touched / sitting-in-zone)"
                    )
                lines.append("")
    summary_path = archive.folder / "summary.txt"
    text = "\n".join(lines).rstrip() + "\n"
    summary_path.write_text(text, encoding="utf-8")
    print(text, end="", flush=True)
    print(f"wrote {csv_path}", flush=True)
    print(f"wrote {xlsx_path}", flush=True)
    print(f"wrote {summary_path}", flush=True)

    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
