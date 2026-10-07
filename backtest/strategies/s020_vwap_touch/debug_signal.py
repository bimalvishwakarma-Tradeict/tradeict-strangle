#!/usr/bin/env python3
"""Dump S020 V0/V3 line + signal state at an IST timestamp.

Does not modify TRAIN/DEV runners.

python backtest\\strategies\\s020_vwap_touch\\debug_signal.py --at "2025-06-01 08:33" --tf 1m --variant V0 --window-min 60
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as s020_dev  # noqa: E402

TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}


def parse_at(s: str) -> int:
    raw = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=s018.IST)
            return int(dt.timestamp())
        except ValueError:
            continue
    raise SystemExit(f"bad --at {s!r}; use 'YYYY-MM-DD HH:MM' IST")


def skip_reason(t: int) -> str:
    bits: list[str] = []
    if s020_dev.skip_lunch_ist(t):
        bits.append("skip_lunch")
    if s020_dev.skip_thu_sat_window(t):
        bits.append("skip_thu_sat")
    return ",".join(bits)


def extreme_index(
    ln: s020.SwingLine,
    tts: np.ndarray,
    th: np.ndarray,
    tl: np.ndarray,
    tc: np.ndarray,
    vwap: np.ndarray,
) -> int | None:
    ci = int(ln.cross_i)
    if ci <= 0 or ci >= len(tts):
        return None
    day = int(tts[ci]) // 86400
    kind = str(ln.kind)
    level = float(ln.level)
    run: list[int] = []
    i = ci - 1
    while i >= 0 and int(tts[i]) // 86400 == day:
        vw = float(vwap[i])
        cl = float(tc[i])
        if not np.isfinite(vw):
            break
        if kind == "low" and not (cl < vw):
            break
        if kind == "high" and not (cl > vw):
            break
        run.append(i)
        i -= 1
    run.reverse()
    if not run:
        return None
    for j in run:
        px = float(tl[j]) if kind == "low" else float(th[j])
        if abs(px - level) <= 1e-6:
            return j
    if kind == "low":
        return min(run, key=lambda j: float(tl[j]))
    return max(run, key=lambda j: float(th[j]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", required=True, help="IST 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--tf", default="1m", choices=tuple(TF_SEC))
    ap.add_argument("--variant", default="V0")
    ap.add_argument("--window-min", type=int, default=60)
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    args = ap.parse_args()
    at_ts = parse_at(args.at)
    tf_sec = TF_SEC[str(args.tf)]
    win = int(args.window_min)
    lo_ts = at_ts - win * 60
    hi_ts = at_ts + win * 60

    print(f"csv={args.csv}", flush=True)
    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    print(f"1m bars={len(ts)} first={s018.ist_str(int(ts[0]))} last={s018.ist_str(int(ts[-1]))}", flush=True)
    pad_lo = at_ts - 20 * 86400
    pad_hi = at_ts + 10 * 86400
    sel = (ts >= pad_lo) & (ts <= pad_hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    print(
        f"work window {s018.ist_str(int(ts[0]))} .. {s018.ist_str(int(ts[-1]))} n={len(ts)}",
        flush=True,
    )

    vwap_1m = s020.session_vwap(ts, h, l, c, vol)
    tts, to_, th, tl, tc, tv = s020_dev.resample_tf(ts, o, h, l, c, vol, tf_sec)
    vwap_tf = s020.session_vwap(tts, th, tl, tc, tv)
    raw_lines, _, _ = s020.detect_swings(tts, to_, th, tl, tc, vwap_tf)
    dlines = s020_dev.tf_lines_to_dev(tts, raw_lines, tf_sec)
    work = [
        s020_dev.DevLine(
            kind=x.kind,
            level=x.level,
            extreme=x.extreme,
            create_ts=x.create_ts,
            active_from_ts=x.active_from_ts,
            session_utc=x.session_utc,
            cross_ts=x.cross_ts,
        )
        for x in dlines
    ]
    sigs, _ = s020_dev.collect_signals(ts, o, h, l, c, work, str(args.variant))
    by_cross = {int(ln.cross_i): ln for ln in raw_lines}

    idx_map = {int(t): i for i, t in enumerate(ts)}
    i_at = idx_map.get(at_ts)
    print()
    print("=== BAR AT --at ===")
    if i_at is None:
        print(f"no 1m bar at {args.at} unix={at_ts}")
    else:
        vw = float(vwap_1m[i_at])
        print(
            f"IST={s018.ist_str(at_ts)} O={float(o[i_at]):.2f} H={float(h[i_at]):.2f} "
            f"L={float(l[i_at]):.2f} C={float(c[i_at]):.2f} VWAP={vw:.2f}"
        )

    print()
    print("=== TRIGGER LINE(S) AT --at ===")
    hits = [s for s in sigs if int(s["ts"]) == at_ts]
    if not hits:
        print("no collect_signals hit at this minute")
        if i_at is not None:
            lo_px = float(l[i_at])
            hi_px = float(h[i_at])
            touching: list[s020_dev.DevLine] = []
            for ln in work:
                if ln.expire_ts is not None and int(ln.expire_ts) < at_ts:
                    continue
                if at_ts < int(ln.active_from_ts):
                    continue
                if ln.kind == "low" and lo_px <= ln.level:
                    touching.append(ln)
                if ln.kind == "high" and hi_px >= ln.level:
                    touching.append(ln)
            if touching:
                print(f"price still touches {len(touching)} active line(s) without a kept signal:")
                hits_meta = touching
            else:
                hits_meta = []
        else:
            hits_meta = []
    else:
        hits_meta = []
        for s in hits:
            print(
                f"signal side={s['side']} kind={s.get('kind')} level={float(s.get('level') or 0):.2f} "
                f"OHLC={float(s['o']):.2f}/{float(s['h']):.2f}/{float(s['l']):.2f}/{float(s['c']):.2f}"
            )
            for ln in work:
                if abs(float(ln.level) - float(s.get("level") or 0)) > 1e-6:
                    continue
                if str(ln.kind) != str(s.get("kind") or ln.kind):
                    continue
                hits_meta.append(ln)

    seen: set[int] = set()
    for ln in hits_meta:
        key = int(ln.create_ts)
        if key in seen:
            continue
        seen.add(key)
        raw = None
        for rl in raw_lines:
            if str(rl.kind) == str(ln.kind) and abs(float(rl.level) - float(ln.level)) <= 1e-6:
                if int(tts[int(rl.cross_i)]) == int(ln.cross_ts) if 0 <= int(rl.cross_i) < len(tts) else False:
                    raw = rl
                    break
        if raw is None:
            raw = by_cross.get(int(np.searchsorted(tts, int(ln.cross_ts))))
        xi = extreme_index(raw, tts, th, tl, tc, vwap_tf) if raw is not None else None
        ext_ist = s018.ist_str(int(tts[xi])) if xi is not None else "—"
        exp_s = s018.ist_str(int(ln.expire_ts)) if ln.expire_ts is not None else "still active"
        print(
            f"  line kind={ln.kind} level={ln.level:.2f} "
            f"cross={s018.ist_str(int(ln.cross_ts))} "
            f"create={s018.ist_str(int(ln.create_ts))} "
            f"active_from={s018.ist_str(int(ln.active_from_ts))} "
            f"extreme_bar={ext_ist} expire={exp_s}"
        )

    print()
    print(f"=== RAW SIGNALS IN ±{win} min ===")
    n = 0
    for s in sigs:
        t = int(s["ts"])
        if t < lo_ts or t > hi_ts:
            continue
        n += 1
        ok = s020_dev.entry_allowed(t)
        why = skip_reason(t)
        print(
            f"{s018.ist_str(t)} side={s['side']} kind={s.get('kind')} "
            f"level={float(s.get('level') or 0):.2f} C={float(s['c']):.2f} "
            f"entry_allowed={ok} reason={why or 'ok'}"
        )
    print(f"count={n}")

    print()
    print("=== ACTIVE LINES AT --at ===")
    n_act = 0
    for ln in work:
        if at_ts < int(ln.active_from_ts):
            continue
        if ln.expire_ts is not None and int(ln.expire_ts) < at_ts:
            continue
        n_act += 1
        age_h = (at_ts - int(ln.create_ts)) / 3600.0
        print(
            f"kind={ln.kind} level={ln.level:.2f} created={s018.ist_str(int(ln.create_ts))} "
            f"age_hours={age_h:.2f}"
        )
    print(f"count={n_act}")


if __name__ == "__main__":
    main()
