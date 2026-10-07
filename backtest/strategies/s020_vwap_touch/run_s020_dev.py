#!/usr/bin/env python3
"""S020 DEV: 1-month refine (1DTE Bimal, time filters, TF lines, V0-V5).

Does not write s020_ckpt.jsonl / s020_pathcache (TRAIN stays untouched).

python backtest\\strategies\\s020_vwap_touch\\run_s020_dev.py --month 2025-06 --tf 5m --variant V3 --max-days 3
python backtest\\strategies\\s020_vwap_touch\\run_s020_dev.py --month 2025-06 --all
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gc
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.slippage_model import load_slip_table  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    intrinsic,
    ist_date,
    t_years,
)
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402

logger = logging.getLogger("s020_dev")

OUT_DIR = Path("backtest/strategies/s020_vwap_touch/runs")
CACHE_DIR = OUT_DIR / "s020_dev_pathcache"
CKPT = OUT_DIR / "s020_dev_ckpt.jsonl"
PATH_VER = "vdev1"
TRAIN_CKPT = OUT_DIR / "s020_ckpt.jsonl"

TGTS = (100, 150, 200, 250, 300)
SLS = (100, 150, 200, 250, 300)
PRIMARY_T, PRIMARY_SL = 250, 250
RANDOM_SEEDS = tuple(range(10))
TFS = ("1m", "3m", "5m", "15m")
VARIANTS = ("V0", "V1", "V2", "V3", "V4", "V5")
TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}
TIME_STOP_SEC = 4 * 3600
FRESH_AGE_SEC = 12 * 3600


class MonthGuardStore:
    def __init__(self, inner: MarksStore, allow_from: date, allow_to: date) -> None:
        self.inner = inner
        self.allow_from = allow_from
        self.allow_to = allow_to

    def conn(self, d: date) -> Any:
        if d < self.allow_from or d > self.allow_to:
            return None
        return self.inner.conn(d)

    def close(self) -> None:
        self.inner.close()


@dataclass
class DevLine:
    kind: str
    level: float
    extreme: float
    create_ts: int
    active_from_ts: int
    session_utc: int
    expire_ts: int | None = None
    touches: int = 0
    swept: bool = False
    sweep_i: int | None = None
    cross_ts: int = 0


def month_bounds(ym: str) -> tuple[date, date]:
    y, m = (int(x) for x in ym.split("-", 1))
    last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


def expiry_1dte_bimal(t: int) -> date:
    """Entry IST < 17:30 -> next day 17:30; >= 17:30 -> day after next 17:30."""
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    d = dt.date()
    cut = dt.replace(hour=17, minute=30, second=0, microsecond=0)
    if dt < cut:
        return d + timedelta(days=1)
    return d + timedelta(days=2)


def skip_lunch_ist(t: int) -> bool:
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    mins = dt.hour * 60 + dt.minute
    return (5 * 60 + 30) <= mins <= (8 * 60 + 30)


def skip_thu_sat_window(t: int) -> bool:
    """Skip Thu 17:30 IST inclusive through Sat 17:30 IST exclusive."""
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    wd = int(dt.weekday())
    mins = dt.hour * 60 + dt.minute
    cut = 17 * 60 + 30
    if wd == 3 and mins >= cut:
        return True
    if wd == 4:
        return True
    if wd == 5 and mins < cut:
        return True
    return False


def entry_allowed(t: int) -> bool:
    return (not skip_lunch_ist(t)) and (not skip_thu_sat_window(t))


def line_fresh(ln: DevLine, t: int) -> bool:
    if int(t) // 86400 == int(ln.session_utc):
        return True
    return (int(t) - int(ln.create_ts)) <= FRESH_AGE_SEC


def resample_tf(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    tf_sec: int,
) -> tuple[np.ndarray, ...]:
    if tf_sec <= 60:
        return ts, o, h, l, c, vol
    n = len(ts)
    need = tf_sec // 60
    buckets = (ts // tf_sec) * tf_sec
    ots: list[int] = []
    oo: list[float] = []
    hh: list[float] = []
    ll: list[float] = []
    cc: list[float] = []
    vv: list[float] = []
    i = 0
    while i < n:
        b = int(buckets[i])
        j = i + 1
        while j < n and int(buckets[j]) == b:
            j += 1
        if (j - i) < need:
            i = j
            continue
        ots.append(b)
        oo.append(float(o[i]))
        hh.append(float(np.max(h[i:j])))
        ll.append(float(np.min(l[i:j])))
        cc.append(float(c[j - 1]))
        vv.append(float(np.sum(vol[i:j])))
        i = j
    return (
        np.asarray(ots, dtype=np.int64),
        np.asarray(oo, dtype=np.float64),
        np.asarray(hh, dtype=np.float64),
        np.asarray(ll, dtype=np.float64),
        np.asarray(cc, dtype=np.float64),
        np.asarray(vv, dtype=np.float64),
    )


def tf_lines_to_dev(tf_ts: np.ndarray, lines: list[s020.SwingLine], tf_sec: int) -> list[DevLine]:
    out: list[DevLine] = []
    n = len(tf_ts)
    for ln in lines:
        ci = int(ln.cross_i)
        if ci < 0 or ci >= n:
            continue
        create_ts = int(tf_ts[ci]) + int(tf_sec)
        out.append(
            DevLine(
                kind=str(ln.kind),
                level=float(ln.level),
                extreme=float(ln.extreme),
                create_ts=create_ts,
                active_from_ts=create_ts,
                session_utc=create_ts // 86400,
                cross_ts=int(tf_ts[ci]),
            )
        )
    return out


def collect_signals(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    lines: list[DevLine],
    variant: str,
) -> tuple[list[dict[str, Any]], int]:
    both_skip = 0
    sigs: list[dict[str, Any]] = []
    live = sorted(lines, key=lambda x: x.active_from_ts)
    n = len(ts)
    for i in range(n):
        t = int(ts[i])
        cl = float(c[i])
        hi = float(h[i])
        lo = float(l[i])
        long_hit = False
        short_hit = False
        hit_meta: list[dict[str, Any]] = []
        for ln in live:
            if ln.expire_ts is not None or t < ln.active_from_ts:
                continue
            trig_long = False
            trig_short = False
            if variant in ("V0", "V1", "V2", "V5"):
                ok_line = True
                if variant == "V1" and not line_fresh(ln, t):
                    ok_line = False
                if variant == "V2" and ln.touches >= 1:
                    ok_line = False
                if ok_line:
                    if ln.kind == "low" and lo <= ln.level:
                        trig_long = True
                    if ln.kind == "high" and hi >= ln.level:
                        trig_short = True
            elif variant == "V3":
                if ln.kind == "low":
                    if lo < ln.level:
                        if not ln.swept:
                            ln.sweep_i = i
                        ln.swept = True
                    if ln.swept and cl > ln.level:
                        trig_long = True
                else:
                    if hi > ln.level:
                        if not ln.swept:
                            ln.sweep_i = i
                        ln.swept = True
                    if ln.swept and cl < ln.level:
                        trig_short = True
            elif variant == "V4":
                if ln.kind == "low" and cl < ln.level:
                    trig_short = True
                if ln.kind == "high" and cl > ln.level:
                    trig_long = True
            if trig_long or trig_short:
                ln.touches += 1
                meta = {
                    "kind": ln.kind,
                    "level": ln.level,
                    "create_ts": ln.create_ts,
                    "sweep_i": ln.sweep_i if ln.sweep_i is not None else i,
                    "reclaim_i": i,
                }
                hit_meta.append(meta)
            if trig_long:
                long_hit = True
            if trig_short:
                short_hit = True
        if long_hit and short_hit:
            both_skip += 1
        elif long_hit or short_hit:
            side = "long" if long_hit else "short"
            hm = hit_meta[0] if hit_meta else {}
            si = int(hm.get("sweep_i", i))
            ri = int(hm.get("reclaim_i", i))
            sigs.append(
                {
                    "i": i,
                    "ts": t,
                    "side": side,
                    "o": float(o[i]),
                    "h": hi,
                    "l": lo,
                    "c": cl,
                    "level": float(hm.get("level", float("nan"))),
                    "kind": str(hm.get("kind", "")),
                    "sweep_i": si,
                    "reclaim_i": ri,
                    "sweep_o": float(o[si]),
                    "sweep_h": float(h[si]),
                    "sweep_l": float(l[si]),
                    "sweep_c": float(c[si]),
                    "reclaim_o": float(o[ri]),
                    "reclaim_h": float(h[ri]),
                    "reclaim_l": float(l[ri]),
                    "reclaim_c": float(c[ri]),
                    "exp": expiry_1dte_bimal(t).isoformat(),
                }
            )
        for ln in live:
            if ln.expire_ts is not None or t < ln.active_from_ts:
                continue
            if ln.kind == "low" and cl < ln.level:
                ln.expire_ts = t
            elif ln.kind == "high" and cl > ln.level:
                ln.expire_ts = t
    return sigs, both_skip


def cache_file(entry_ts: int, side: str, exp: date, tag: str) -> Path:
    return CACHE_DIR / f"{entry_ts}_{side}_{exp.isoformat()}_{tag}_{PATH_VER}.npz"


def pick_basket_dev(
    store: Any, side: str, t: int, spot: float, flipped: bool
) -> list[s018.Leg] | None:
    want = side
    if flipped:
        want = "short" if side == "long" else "long"
    exp = expiry_1dte_bimal(t)
    packed = s018.load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _ = packed
    exp_ts = s018.expiry_unix(exp)
    t_yr = t_years(t, exp_ts)
    dte = max(0, (exp - ist_date(t)).days)
    if want == "long":
        specs = [(False, 1000.0, "p1000"), (True, 500.0, "c500"), (True, 300.0, "c300")]
    else:
        specs = [(True, 1000.0, "c1000"), (False, 500.0, "p500"), (False, 300.0, "p300")]
    chosen: list[tuple[dict[str, Any], str]] = []
    for is_call, tgt, role in specs:
        r = s018.nearest(rows, is_call, tgt)
        if r is None:
            return None
        chosen.append((r, role))
    legs: list[s018.Leg] = []
    for r, role in chosen:
        q = s018.mark_le(store, exp, str(r["symbol"]), t)
        if q is None:
            return None
        fill, _ = s018.buy_fill(q.px, dte)
        dlt = s018.signed_delta(q.px, spot, float(r["strike"]), t_yr, bool(r["is_call"]))
        legs.append(
            s018.Leg(
                symbol=str(r["symbol"]),
                strike=float(r["strike"]),
                is_call=bool(r["is_call"]),
                role=role,
                mark=q.px,
                fill=fill,
                src=q.src,
                delta=dlt,
                ts=q.ts,
            )
        )
    return legs


def get_or_build_path(
    store: Any,
    spot_c: dict[int, float],
    t: int,
    side: str,
    tag: str,
    flipped: bool,
) -> tuple[dict[str, Any] | None, bool]:
    exp = expiry_1dte_bimal(t)
    fp = cache_file(t, side, exp, tag)
    path = s020.load_path(fp)
    if path is not None:
        return path, False
    sp = spot_c.get(t)
    if sp is None:
        return None, False
    legs = pick_basket_dev(store, side, t, float(sp), flipped)
    if legs is None:
        return None, False
    path = s020.build_path(store, spot_c, legs, t, exp)
    if path is None:
        return None, False
    s020.save_path(fp, path)
    return path, True


def scan_path_dev(
    path: dict[str, Any],
    tgt: float,
    sl: float,
    spot_c: dict[int, float],
    time_stop_sec: int | None,
) -> dict[str, Any] | None:
    if time_stop_sec is None:
        return s020.scan_path(path, tgt, sl, spot_c)
    ts = path["ts"]
    ok = path["ok"]
    pnl_a = path["pnl"]
    exp_ts = int(path["exp_ts"])
    entry = int(path["entry_ts"])
    n = int(ts.size)
    stop_at = int(entry) + int(time_stop_sec)
    for i in range(n):
        t = int(ts[i])
        if bool(ok[i]) and np.isfinite(pnl_a[i]):
            gp = float(pnl_a[i])
            if gp >= tgt:
                return s020._exit_idx(path, i, "TARGET", spot_c)
            if gp <= -sl:
                return s020._exit_idx(path, i, "SL", spot_c)
            if t >= stop_at:
                return s020._exit_idx(path, i, "TIME", spot_c)
        if t == exp_ts:
            return s020.scan_path(path, tgt, sl, spot_c)
    return None


def simulate_plan(
    store: Any,
    spot_c: dict[int, float],
    plan: list[tuple[int, str]],
    tgt: float,
    sl: float,
    tag: str,
    flipped: bool,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    time_stop_sec: int | None,
    label: str = "",
    extra_by: dict[tuple[int, str], dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    busy = -1
    rows: list[dict[str, Any]] = []
    n_stale = 0
    built = 0
    t0 = time.perf_counter()
    st_prog: dict[str, int] = {"mark": -1}
    nplan = len(plan)
    for j, (t, side) in enumerate(plan, start=1):
        if label:
            s020.progress_every_2pct(label, j, nplan, built, t0, st_prog)
        if t < start_ts or t >= cutoff or t <= busy:
            continue
        if not s020.in_window(t, win_from, win_to):
            continue
        if not entry_allowed(t):
            continue
        path, newp = get_or_build_path(store, spot_c, t, side, tag, flipped)
        if newp:
            built += 1
        if path is None:
            n_stale += 1
            continue
        walked = scan_path_dev(path, tgt, sl, spot_c, time_stop_sec)
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        exp = expiry_1dte_bimal(t)
        hrs_exp = (s018.expiry_unix(exp) - int(t)) / 3600.0
        extra: dict[str, Any] = {}
        if extra_by is not None:
            extra = dict(extra_by.get((int(t), str(side)), {}))
        rows.append(
            {
                "entry_ts": t,
                "side": side,
                "hod": s018.hod_ist(t),
                "hold_hrs": hold,
                "hrs_to_exp": hrs_exp,
                "entry_dlt": s020.entry_net_delta(list(path["legs"]), side),
                "entry_tv": s020.entry_tv(list(path["legs"]), float(spot_c.get(t, 0.0))),
                "legs": path["legs"],
                "exp": exp.isoformat(),
                **extra,
                **walked,
            }
        )
        busy = int(walked["exit_ts"])
    return rows, n_stale


def stats_dev(rows: list[dict[str, Any]]) -> dict[str, Any]:
    base = s020.stats_of(rows, len(rows), max(1.0, 1.0))
    hrs = [float(r.get("hrs_to_exp", float("nan"))) for r in rows]
    hrs = [x for x in hrs if np.isfinite(x)]
    base["hrs_exp"] = float(np.mean(hrs)) if hrs else float("nan")
    wd, we = s020.ww_block(rows)
    base["wd"] = wd
    base["we"] = we
    return base


def fmt_dev(s: dict[str, Any]) -> str:
    return (
        f"n={s.get('n', 0)} win%={s020._fnum(s.get('win', float('nan')), 1)} "
        f"mean={s020._fnum(s.get('mean', float('nan')), 2)} "
        f"med={s020._fnum(s.get('med', float('nan')), 2)} "
        f"gross={s020._fnum(s.get('gross', float('nan')), 2)} "
        f"BROKERAGE/t={s020._fnum(s.get('fee', float('nan')), 2)} "
        f"SLIPPAGE/t={s020._fnum(s.get('slip', float('nan')), 2)} "
        f"worst={s020._fnum(s.get('worst', float('nan')), 2)} "
        f"maxDD={s020._fnum(s.get('maxdd', float('nan')), 1)} "
        f"holdHrs={s020._fnum(s.get('hold', float('nan')), 2)} "
        f"hrsToExp={s020._fnum(s.get('hrs_exp', float('nan')), 2)} "
        f"exits={s.get('exits', {})}"
    )


def drop_dev_fresh() -> None:
    if TRAIN_CKPT.exists():
        print(f"fresh DEV: leaving TRAIN {TRAIN_CKPT.name} untouched", flush=True)
    if CKPT.exists():
        CKPT.unlink()
        print(f"fresh: dropped {CKPT.name}", flush=True)
    n = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob(f"*_{PATH_VER}.npz"):
            p.unlink(missing_ok=True)
            n += 1
    print(f"fresh: dropped {n} S020 DEV path-cache files", flush=True)


def ckpt_window_ok(done: dict[str, dict[str, Any]], month: str, max_days: int) -> bool:
    if not done:
        return True
    for rec in done.values():
        if str(rec.get("month", "")) != month:
            return False
        try:
            if int(rec.get("max_days", -1)) != int(max_days):
                return False
        except (TypeError, ValueError):
            return False
    return True


def cells_for(variant: str, best_v0: tuple[int, int] | None) -> list[tuple[int, int]]:
    if variant == "V0":
        return [(int(t), int(s)) for t in TGTS for s in SLS]
    out = [(PRIMARY_T, PRIMARY_SL)]
    if best_v0 is not None and best_v0 != (PRIMARY_T, PRIMARY_SL):
        out.append(best_v0)
    return out


def print_v3_examples(sigs: list[dict[str, Any]], ts: np.ndarray) -> None:
    print("=== 2 V3 ENTRIES ===", flush=True)
    n = 0
    for s in sigs:
        if str(s.get("side")) not in ("long", "short"):
            continue
        si = int(s["sweep_i"])
        ri = int(s["reclaim_i"])
        print(
            f"  {s['side']} level={float(s['level']):.1f} kind={s.get('kind')} "
            f"sweep={s018.ist_str(int(ts[si]))} "
            f"OHLC={s['sweep_o']:.1f}/{s['sweep_h']:.1f}/{s['sweep_l']:.1f}/{s['sweep_c']:.1f}",
            flush=True,
        )
        print(
            f"    reclaim={s018.ist_str(int(ts[ri]))} "
            f"OHLC={s['reclaim_o']:.1f}/{s['reclaim_h']:.1f}/{s['reclaim_l']:.1f}/{s['reclaim_c']:.1f} "
            f"entry={s018.ist_str(int(s['ts']))} expiry={s.get('exp')} 17:30 IST",
            flush=True,
        )
        n += 1
        if n >= 2:
            break
    if n == 0:
        print("  (no V3 signals in window)", flush=True)


def print_expiry_check() -> None:
    monday = date(2025, 6, 2)
    t = to_unix(ist_dt(monday, 18, 0))
    exp = expiry_1dte_bimal(t)
    print(
        f"expiry check: Monday {monday.isoformat()} 18:00 IST -> {exp.isoformat()} 17:30 IST "
        f"(expect 2025-06-04 Wednesday)",
        flush=True,
    )


def run_combo(
    store: Any,
    spot_c: dict[int, float],
    ts: np.ndarray,
    sigs: list[dict[str, Any]],
    variant: str,
    tf: str,
    month: str,
    cells: list[tuple[int, int]],
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    n_days: float,
    done: dict[str, dict[str, Any]],
    cell_rows: dict[str, list[dict[str, Any]]],
    hour_idx: dict[int, np.ndarray],
    t_all: float,
    arm_i: int,
    arm_n: int,
    max_days: int,
) -> None:
    time_stop = TIME_STOP_SEC if variant == "V5" else None
    extra_by = {(int(s["ts"]), str(s["side"])): s for s in sigs}
    plan_all = [(int(s["ts"]), str(s["side"])) for s in sigs if entry_allowed(int(s["ts"]))]
    tag_p = f"DEV_{variant}_{tf}"
    for tgt, slv in cells:
        key = f"{month}|{tf}|{variant}|T={tgt}|SL={slv}"
        if key in done:
            print(f"done SKIP {key}", flush=True)
            continue
        print(
            f"arm {arm_i}/{arm_n} {key} RSS={s020.rss_mb() or 0:.0f}MB "
            f"elapsed={time.perf_counter()-t_all:.0f}s",
            flush=True,
        )
        rows, n_stale = simulate_plan(
            store, spot_c, plan_all, float(tgt), float(slv), tag_p, False,
            start_ts, cutoff, win_from, win_to, time_stop,
            label=f"{tf} {variant} T={tgt}|SL={slv}",
            extra_by=extra_by,
        )
        stt = stats_dev(rows)
        stt.update(
            {
                "key": key,
                "month": month,
                "tf": tf,
                "variant": variant,
                "tgt": tgt,
                "sl": slv,
                "max_days": int(max_days),
                "n_stale": n_stale,
                "n_sig": len(plan_all),
                "n_days": n_days,
            }
        )
        c2m = float("nan")
        c3m = float("nan")
        if tgt == PRIMARY_T and slv == PRIMARY_SL:
            c2s: list[float] = []
            c2_pool: list[dict[str, Any]] = []
            for si, seed in enumerate(RANDOM_SEEDS, start=1):
                print(f"C2 {key} seed {si}/{len(RANDOM_SEEDS)}", flush=True)
                forced = s020.random_c2(rows, ts, hour_idx, seed)
                rr, _ = simulate_plan(
                    store, spot_c, forced, float(tgt), float(slv), f"C2{tag_p}", False,
                    start_ts, cutoff, win_from, win_to, time_stop,
                    label=f"C2 {key} seed={si}",
                )
                c2_pool.extend(rr)
                if rr:
                    c2s.append(float(np.mean([x["net"] for x in rr])))
            print(f"C3 {key}", flush=True)
            plan_c3 = [(int(r["entry_ts"]), str(r["side"])) for r in rows]
            c3, _ = simulate_plan(
                store, spot_c, plan_c3, float(tgt), float(slv), f"C3{tag_p}", True,
                start_ts, cutoff, win_from, win_to, time_stop,
                label=f"C3 {key}",
            )
            c2m = float(np.mean(c2s)) if c2s else float("nan")
            c3s = stats_dev(c3)
            c3m = float(c3s["mean"])
            stt["c2"] = c2m
            stt["c3"] = c3m
            stt["c2_wd"] = s020.stats_ww(s020.split_wd_we(c2_pool)[0])
            stt["c2_we"] = s020.stats_ww(s020.split_wd_we(c2_pool)[1])
            stt["c3_wd"] = c3s["wd"]
            stt["c3_we"] = c3s["we"]
            stt["c3_full"] = {k: v for k, v in c3s.items() if k not in ("wd", "we")}
        rec = {k: v for k, v in stt.items() if k != "exits"}
        rec["exits"] = stt.get("exits", {})
        s020.append_ckpt(CKPT, rec)
        done[key] = stt
        cell_rows[key] = rows
        s018._CHAIN.clear()
        gc.collect()


def best_v0_cell(done: dict[str, dict[str, Any]], month: str, tf: str) -> tuple[int, int] | None:
    ranked: list[dict[str, Any]] = []
    for rec in done.values():
        if str(rec.get("month")) != month or str(rec.get("tf")) != tf:
            continue
        if str(rec.get("variant")) != "V0":
            continue
        if not np.isfinite(rec.get("mean", float("nan"))):
            continue
        ranked.append(rec)
    if not ranked:
        return None
    ranked.sort(key=lambda s: float(s["mean"]), reverse=True)
    return int(ranked[0]["tgt"]), int(ranked[0]["sl"])


def summary_table(done: dict[str, dict[str, Any]], month: str) -> list[str]:
    lines = [
        "SUMMARY tf x variant @ T250/SL250:",
        f"{'tf':<5} {'var':<4} {'n':>5} {'mean':>9} {'gross':>9} {'broker':>8} {'slip':>8} {'C2':>9} {'C3':>9}",
    ]
    for tf in TFS:
        for var in VARIANTS:
            key = f"{month}|{tf}|{var}|T={PRIMARY_T}|SL={PRIMARY_SL}"
            s = done.get(key)
            if s is None:
                continue
            lines.append(
                f"{tf:<5} {var:<4} {int(s.get('n', 0)):5d} "
                f"{s020._fnum(s.get('mean', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('gross', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('fee', float('nan')), 2):>8} "
                f"{s020._fnum(s.get('slip', float('nan')), 2):>8} "
                f"{s020._fnum(s.get('c2', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('c3', float('nan')), 2):>9}"
            )
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--tf", default="1m", choices=list(TFS))
    ap.add_argument("--variant", default="V0", choices=list(VARIANTS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--cache-gb", type=float, default=1.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("Disable PC sleep")
    load_slip_table()
    reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        drop_dev_fresh()

    month = str(args.month)
    win_from, win_to = month_bounds(month)
    max_days = int(args.max_days or 0)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    if max_days:
        cutoff = start_ts + max_days * 86400
        win_to = min(win_to, ist_date(cutoff - 1))
    if not args.fresh:
        preexisting = s020.load_ckpt(CKPT)
        if preexisting and not ckpt_window_ok(preexisting, month, max_days):
            print("checkpoint window mismatch -> use --fresh", flush=True)
            sys.exit(1)

    print_expiry_check()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    lo = start_ts - 3 * 86400
    hi = cutoff + 3 * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    if max_days:
        print(f"SMOKE max-days={max_days} month={month} cutoff_ts={cutoff}", flush=True)
    spot_c = {int(t): float(x) for t, x in zip(ts, c)}
    n_days = float(max_days) if max_days else float((win_to - win_from).days + 1)

    inner = MarksStore()
    store = MonthGuardStore(inner, win_from - timedelta(days=1), win_to + timedelta(days=3))
    done: dict[str, dict[str, Any]] = {} if args.fresh else s020.load_ckpt(CKPT)
    cell_rows: dict[str, list[dict[str, Any]]] = {}

    tfs = list(TFS) if args.all else [str(args.tf)]
    variants = list(VARIANTS) if args.all else [str(args.variant)]
    arm_n = max(1, len(tfs) * 30)
    arm_i = 0
    t_all = time.perf_counter()

    eligible = [
        i
        for i, t in enumerate(ts)
        if start_ts <= int(t) < cutoff
        and s020.in_window(int(t), win_from, win_to)
        and entry_allowed(int(t))
    ]
    hour_idx: dict[int, np.ndarray] = defaultdict(list)  # type: ignore[assignment]
    buckets: dict[int, list[int]] = defaultdict(list)
    for i in eligible:
        buckets[s018.hod_ist(int(ts[i]))].append(i)
    hour_idx = {h: np.asarray(v, dtype=np.int64) for h, v in buckets.items()}

    smoke_sigs: list[dict[str, Any]] = []
    for tf in tfs:
        tf_sec = TF_SEC[tf]
        tts, to_, th, tl, tc, tv = resample_tf(ts, o, h, l, c, vol, tf_sec)
        vwap = s020.session_vwap(tts, th, tl, tc, tv)
        raw_lines, _, _ = s020.detect_swings(tts, to_, th, tl, tc, vwap)
        dlines = tf_lines_to_dev(tts, raw_lines, tf_sec)
        v0_best = best_v0_cell(done, month, tf)
        seq = list(variants)
        if args.all and "V0" in seq:
            seq = ["V0"] + [v for v in seq if v != "V0"]
        for variant in seq:
            if variant != "V0":
                v0_best = best_v0_cell(done, month, tf)
            lines_copy = [
                DevLine(
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
            sigs, both_skip = collect_signals(ts, o, h, l, c, lines_copy, variant)
            sigs = [
                s
                for s in sigs
                if start_ts <= int(s["ts"]) < cutoff
                and s020.in_window(int(s["ts"]), win_from, win_to)
                and entry_allowed(int(s["ts"]))
            ]
            print(
                f"S020-DEV {month} tf={tf} {variant} lines={len(dlines)} "
                f"signals={len(sigs)} both_skip={both_skip}",
                flush=True,
            )
            if max_days and variant == "V3":
                smoke_sigs = sigs
                print_v3_examples(sigs, ts)
            cells = cells_for(variant, v0_best if variant != "V0" else None)
            arm_i += 1
            run_combo(
                store, spot_c, ts, sigs, variant, tf, month, cells,
                start_ts, cutoff, win_from, win_to, n_days,
                done, cell_rows, hour_idx, t_all, arm_i, arm_n, max_days,
            )
            if max_days and variant == "V3":
                pk = f"{month}|{tf}|{variant}|T={PRIMARY_T}|SL={PRIMARY_SL}"
                print("=== 2 V3 FILLED ENTRIES ===", flush=True)
                shown = 0
                for tr in cell_rows.get(pk, []):
                    print(
                        f"  {tr['side']} level={float(tr.get('level', float('nan'))):.1f} "
                        f"entry={s018.ist_str(int(tr['entry_ts']))} expiry={tr.get('exp')} 17:30 IST "
                        f"net={float(tr['net']):.2f} reason={tr['reason']}",
                        flush=True,
                    )
                    print(
                        f"    sweep OHLC={tr.get('sweep_o')}/{tr.get('sweep_h')}/"
                        f"{tr.get('sweep_l')}/{tr.get('sweep_c')} "
                        f"reclaim OHLC={tr.get('reclaim_o')}/{tr.get('reclaim_h')}/"
                        f"{tr.get('reclaim_l')}/{tr.get('reclaim_c')}",
                        flush=True,
                    )
                    shown += 1
                    if shown >= 2:
                        break
                if shown == 0:
                    print("  (no filled V3 trades)", flush=True)

    report = [
        f"S020 DEV month={month} {win_from}..{win_to} stamp={stamp}",
        "1DTE=Bimal (<17:30 IST next day; >=17:30 day-after-next); skip 05:30-08:30 IST; "
        "skip Thu 17:30-Sat 17:30 IST; TRAIN ckpt/cache untouched",
        "fees=estimate_option_fee*1.18; slip=slip_pct; stale>5m skip",
        "NOTE: weekday/weekend splits are informational; a split is only actionable "
        "if it holds in TRAIN and HOLDOUT.",
    ]
    for tf in tfs:
        for variant in variants:
            for tgt, slv in cells_for(variant, best_v0_cell(done, month, tf) if variant != "V0" else None):
                key = f"{month}|{tf}|{variant}|T={tgt}|SL={slv}"
                st = done.get(key)
                if st is None:
                    continue
                report.append(f"{key} ALL {fmt_dev(st)}")
                if isinstance(st.get("wd"), dict):
                    report.append(f"  WEEKDAY {s020.fmt_ww(st['wd'])}")
                    report.append(f"  WEEKEND {s020.fmt_ww(st['we'])}")
                if tgt == PRIMARY_T and slv == PRIMARY_SL:
                    report.append(
                        f"  C2mean={s020._fnum(st.get('c2', float('nan')), 2)} "
                        f"C3mean={s020._fnum(st.get('c3', float('nan')), 2)}"
                    )
                    if isinstance(st.get("c2_wd"), dict):
                        report.append(f"  C2 WEEKDAY {s020.fmt_ww(st['c2_wd'])}")
                        report.append(f"  C2 WEEKEND {s020.fmt_ww(st['c2_we'])}")
                    if isinstance(st.get("c3_wd"), dict):
                        report.append(f"  C3 WEEKDAY {s020.fmt_ww(st['c3_wd'])}")
                        report.append(f"  C3 WEEKEND {s020.fmt_ww(st['c3_we'])}")
    report.extend(summary_table(done, month))
    txtp = OUT_DIR / f"s020_dev_{month}_{stamp}.txt"
    txtp.write_text("\n".join(report) + "\n", encoding="utf-8")
    prim_key = f"{month}|{tfs[0]}|{variants[0]}|T={PRIMARY_T}|SL={PRIMARY_SL}"
    prow = cell_rows.get(prim_key, [])
    csvp = OUT_DIR / f"s020_dev_{month}_{stamp}_trades.csv"
    with csvp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "side", "entry_ts_ist", "exit_ts_ist", "reason", "gross", "fees", "net",
                "hold_hrs", "hrs_to_exp", "exp",
            ],
        )
        w.writeheader()
        for r in prow:
            w.writerow(
                {
                    "side": r["side"],
                    "entry_ts_ist": s018.ist_str(int(r["entry_ts"])),
                    "exit_ts_ist": s018.ist_str(int(r["exit_ts"])),
                    "reason": r["reason"],
                    "gross": r["gross"],
                    "fees": r["fees"],
                    "net": r["net"],
                    "hold_hrs": r["hold_hrs"],
                    "hrs_to_exp": r.get("hrs_to_exp"),
                    "exp": r.get("exp"),
                }
            )
    print("\n".join(report))
    print(f"wrote {txtp}")
    print(f"wrote {csvp}")
    store.close()


if __name__ == "__main__":
    main()
