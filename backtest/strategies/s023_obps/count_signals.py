#!/usr/bin/env python3
"""Raw S023 OBPS signal counts (no basket / one-at-a-time)."""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s023_obps.signals import (  # noqa: E402
    ARMS,
    MODES,
    TFS,
    iter_obps_signals,
)

FOCUS = ("2025-06", "2025-07", "2025-08", "2025-09")
TOTAL_FROM = "2024-07"
TOTAL_TO = "2026-09"


def _month_list(a: str, b: str) -> list[str]:
    y, m = int(a[:4]), int(a[5:7])
    y2, m2 = int(b[:4]), int(b[5:7])
    out: list[str] = []
    while (y, m) <= (y2, m2):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def _bars_from_ohlc(tts: Any, to_: Any, th: Any, tl: Any, tc: Any) -> list[dict[str, Any]]:
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


def _window(month: str) -> tuple[int, int]:
    win_from, win_to = d.month_bounds(month)
    start = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    return start, cutoff


def main() -> None:
    months = _month_list(TOTAL_FROM, TOTAL_TO)
    t0, _ = _window(months[0])
    _, t1 = _window(months[-1])
    spot = s018.load_spot_1m(s018.SPOT_CSV)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    lo = t0 - 7 * 86400
    hi = t1 + 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    print(f"1m bars={len(ts)} months={months[0]}..{months[-1]}", flush=True)

    month_bounds = {ym: _window(ym) for ym in months}
    counts: dict[tuple[str, str, str, str], int] = defaultdict(int)

    for tf in TFS:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        print(f"tf={tf} bars={len(bars)}", flush=True)
        for mode in MODES:
            for arm in ARMS:
                raw = iter_obps_signals(bars, tf, mode, arm)
                for r in raw:
                    st = int(r["signal_ts"])
                    for ym, (a, b) in month_bounds.items():
                        if a <= st < b:
                            counts[(tf, mode, arm, ym)] += 1
                            break
                n_tot = sum(counts[(tf, mode, arm, ym)] for ym in months)
                print(f"  mode={mode} arm={arm} total={n_tot} raw={len(raw)}", flush=True)

    hdr = "tf | mode | arm | Jun | Jul | Aug | Sep | total_2024-07..2026-09"
    print("\n" + hdr, flush=True)
    print("-" * len(hdr), flush=True)
    for tf in TFS:
        for mode in MODES:
            for arm in ARMS:
                focus_n = [counts[(tf, mode, arm, ym)] for ym in FOCUS]
                tot = sum(counts[(tf, mode, arm, ym)] for ym in months)
                print(
                    f"{tf} | {mode} | {arm} | {focus_n[0]} | {focus_n[1]} | {focus_n[2]} | {focus_n[3]} | {tot}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
