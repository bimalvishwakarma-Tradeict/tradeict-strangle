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
from backtest.s004_gate import implied_vol_bisection  # noqa: E402
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
RANDOM_SEEDS_20 = tuple(range(20))
TFS = ("1m", "3m", "5m", "15m")
VARIANTS = ("V0", "V1", "V2", "V3", "V4", "V5")
TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800}
LINE_TF = dict(TF_SEC)
TIME_STOP_SEC = 4 * 3600
FRESH_AGE_SEC = 12 * 3600
DEFAULT_TRAIL_CAP = 400.0

# Pre-registered --exit-grid arms; values are locked.
EXIT_ARMS: tuple[dict[str, Any], ...] = (
    {
        "id": "A0",
        "legs": 3,
        "trail_arm": 0.0,
        "trail_give": 0.0,
        "trail_cap": DEFAULT_TRAIL_CAP,
        "dte": 1,
        "tgt": PRIMARY_T,
        "sl": PRIMARY_SL,
    },
    {
        "id": "A1",
        "legs": 3,
        "trail_arm": 100.0,
        "trail_give": 75.0,
        "trail_cap": DEFAULT_TRAIL_CAP,
        "dte": 1,
        "tgt": PRIMARY_T,
        "sl": PRIMARY_SL,
    },
    {
        "id": "A2",
        "legs": 2,
        "trail_arm": 80.0,
        "trail_give": 60.0,
        "trail_cap": DEFAULT_TRAIL_CAP,
        "dte": 1,
        "tgt": PRIMARY_T,
        "sl": PRIMARY_SL,
    },
    {
        "id": "A3",
        "legs": 2,
        "trail_arm": 80.0,
        "trail_give": 60.0,
        "trail_cap": DEFAULT_TRAIL_CAP,
        "dte": 2,
        "tgt": PRIMARY_T,
        "sl": PRIMARY_SL,
    },
)
EXIT_GRID_SIGS: tuple[tuple[str, str, int, str], ...] = (
    ("1m", "V0", 0, "near"),
    ("15m", "V5", 300, "far"),
)
ARM_A4: dict[str, Any] = {
    "id": "A4",
    "legs": 2,
    "trail_arm": 80.0,
    "trail_give": 60.0,
    "trail_cap": DEFAULT_TRAIL_CAP,
    "dte": 1,
    "tgt": PRIMARY_T,
    "sl": PRIMARY_SL,
    "basket": "a4",
    "prem_pct": 0.0,
}
# Long strangle (buy call≈X + put≈X); TP/SL = 100% of debit USD.
STRANGLE_ARMS: tuple[dict[str, Any], ...] = (
    {
        "id": "B1",
        "legs": 2,
        "trail_arm": 0.0,
        "trail_give": 0.0,
        "trail_cap": DEFAULT_TRAIL_CAP,
        "dte": 1,
        "tgt": PRIMARY_T,
        "sl": PRIMARY_SL,
        "basket": "sg150",
        "prem_pct": 100.0,
        "sx": 150.0,
    },
    {
        "id": "B2",
        "legs": 2,
        "trail_arm": 0.0,
        "trail_give": 0.0,
        "trail_cap": DEFAULT_TRAIL_CAP,
        "dte": 1,
        "tgt": PRIMARY_T,
        "sl": PRIMARY_SL,
        "basket": "sg300",
        "prem_pct": 100.0,
        "sx": 300.0,
    },
)
TFBAND_TFS = ("5m", "15m", "30m")
TFBAND_VARS = ("V0", "V5")
TFBAND_BANDS = (0, 100, 200, 300, 400, 500, 600, 700, 800)
TFBAND_ARM_IDS = ("A0", "A1", "A2", "A4")


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


def expiry_2dte_bimal(t: int) -> date:
    """<17:30 IST -> parso 17:30; >=17:30 -> +3 days 17:30."""
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    d = dt.date()
    cut = dt.replace(hour=17, minute=30, second=0, microsecond=0)
    if dt < cut:
        return d + timedelta(days=2)
    return d + timedelta(days=3)


def expiry_for_dte(t: int, dte: int) -> date:
    if int(dte) >= 2:
        return expiry_2dte_bimal(t)
    return expiry_1dte_bimal(t)


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


def band_ok(dist: float, band: int, mode: str) -> bool:
    if int(band) <= 0:
        return True
    if not np.isfinite(dist):
        return False
    if str(mode) == "far":
        return float(dist) > float(band)
    return float(dist) <= float(band)


def keep_signal(s: dict[str, Any], band: int, mode: str) -> bool:
    return entry_allowed(int(s["ts"])) and band_ok(float(s.get("vwap_dist", float("nan"))), band, mode)


def attach_vwap_dist(sigs: list[dict[str, Any]], vwap_by_ts: dict[int, float]) -> None:
    for s in sigs:
        t = int(s["ts"])
        lvl = float(s.get("level", float("nan")))
        vw = float(vwap_by_ts.get(t, float("nan")))
        s["vwap"] = vw
        s["vwap_dist"] = abs(lvl - vw) if np.isfinite(lvl) and np.isfinite(vw) else float("nan")


def signal_breakdown(sigs: list[dict[str, Any]], band: int) -> dict[str, Any]:
    """Exclusive buckets in order: lunch, thusat, vwap_nan, near, far, other."""
    a = b = c = d = e = f = 0
    other_ex: list[str] = []
    bnd = float(band)
    for s in sigs:
        t = int(s["ts"])
        dist = float(s.get("vwap_dist", float("nan")))
        if skip_lunch_ist(t):
            a += 1
            continue
        if skip_thu_sat_window(t):
            b += 1
            continue
        if not np.isfinite(dist):
            c += 1
            continue
        if dist <= bnd:
            d += 1
            continue
        if dist > bnd:
            e += 1
            continue
        f += 1
        if len(other_ex) < 8:
            other_ex.append(
                f"ts={s018.ist_str(t)} dist={dist!r} level={s.get('level')!r} vwap={s.get('vwap')!r}"
            )
    n = len(sigs)
    return {
        "total": n,
        "lunch": a,
        "thusat": b,
        "vwap_nan": c,
        "near": d,
        "far": e,
        "other": f,
        "sum": a + b + c + d + e + f,
        "other_ex": other_ex,
    }


def cell_key(
    month: str,
    tf: str,
    variant: str,
    tgt: int,
    slv: int,
    band: int,
    mode: str,
    legs: int = 3,
    dte: int = 1,
    trail_arm: float = 0.0,
    trail_give: float = 0.0,
    trail_cap: float = DEFAULT_TRAIL_CAP,
    arm_id: str = "",
    basket: str = "std",
) -> str:
    key = f"{month}|{tf}|{variant}|T={tgt}|SL={slv}|band={int(band)}|{str(mode)}"
    if int(legs) != 3:
        key += f"|L={int(legs)}"
    if int(dte) != 1:
        key += f"|D={int(dte)}"
    if float(trail_arm) > 0:
        key += f"|tr={int(trail_arm)}/{int(trail_give)}/{int(trail_cap)}"
    bid = str(arm_id)
    bsk = str(basket)
    if bid == "A4" or bsk == "a4":
        key += "|A4"
    if bid.startswith("B") or bsk.startswith("sg"):
        key += f"|{bid or bsk}"
    return key


def path_tag(base: str, dte: int, basket: str = "std") -> str:
    tag = str(base)
    bsk = str(basket)
    if bsk == "a4":
        tag = f"{tag}_A4"
    elif bsk.startswith("sg"):
        tag = f"{tag}_{bsk}"
    if int(dte) != 1:
        tag = f"{tag}_D{int(dte)}"
    return tag


def arm_by_id(aid: str) -> dict[str, Any]:
    if aid == "A4":
        return dict(ARM_A4)
    for a in EXIT_ARMS:
        if str(a["id"]) == aid:
            return dict(a)
    for a in STRANGLE_ARMS:
        if str(a["id"]) == aid:
            return dict(a)
    raise KeyError(aid)


def work_fields_from_arm(arm: dict[str, Any]) -> dict[str, Any]:
    return {
        "legs": int(arm["legs"]),
        "dte": int(arm.get("dte", 1)),
        "trail_arm": float(arm.get("trail_arm", 0.0)),
        "trail_give": float(arm.get("trail_give", 0.0)),
        "trail_cap": float(arm.get("trail_cap", DEFAULT_TRAIL_CAP)),
        "arm_id": str(arm["id"]),
        "basket": str(arm.get("basket", "std")),
        "prem_pct": float(arm.get("prem_pct", 0.0)),
    }


def hour_idx_band(
    ts: np.ndarray,
    c: np.ndarray,
    vwap_1m: np.ndarray,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    band: int,
    mode: str,
) -> dict[int, np.ndarray]:
    buckets: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(ts):
        tu = int(t)
        if tu < start_ts or tu >= cutoff:
            continue
        if not s020.in_window(tu, win_from, win_to):
            continue
        if not entry_allowed(tu):
            continue
        vw = float(vwap_1m[i])
        dist = abs(float(c[i]) - vw) if np.isfinite(vw) else float("nan")
        if not band_ok(dist, band, mode):
            continue
        buckets[s018.hod_ist(tu)].append(i)
    return {h: np.asarray(v, dtype=np.int64) for h, v in buckets.items()}


def band_grid_jobs() -> list[tuple[str, str, int, str]]:
    jobs: list[tuple[str, str, int, str]] = []
    for tf in ("1m", "15m"):
        for var in ("V0", "V5"):
            jobs.append((tf, var, 0, "near"))
            for band in (100, 200, 300):
                for mode in ("near", "far"):
                    jobs.append((tf, var, band, mode))
    return jobs


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


def _strikes_sorted(rows: list[dict[str, Any]]) -> list[float]:
    return sorted({float(r["strike"]) for r in rows})


def atm_strike_of(rows: list[dict[str, Any]], spot: float) -> float | None:
    ks = _strikes_sorted(rows)
    if not ks:
        return None
    return min(ks, key=lambda k: (abs(k - float(spot)), k))


def row_strike(rows: list[dict[str, Any]], is_call: bool, strike: float) -> dict[str, Any] | None:
    cand = [
        r
        for r in rows
        if bool(r["is_call"]) == is_call and abs(float(r["strike"]) - float(strike)) < 1e-6
    ]
    return cand[0] if cand else None


def _legs_from_chosen(
    store: Any,
    exp: date,
    t: int,
    spot: float,
    t_yr: float,
    dte: int,
    chosen: list[tuple[dict[str, Any], str]],
) -> list[s018.Leg] | None:
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


def pick_basket_dev(
    store: Any,
    side: str,
    t: int,
    spot: float,
    flipped: bool,
    dte_mode: int = 1,
    basket: str = "std",
) -> list[s018.Leg] | None:
    want = side
    if flipped:
        want = "short" if side == "long" else "long"
    exp = expiry_for_dte(t, dte_mode)
    packed = s018.load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _ = packed
    exp_ts = s018.expiry_unix(exp)
    t_yr = t_years(t, exp_ts)
    dte = max(0, (exp - ist_date(t)).days)
    bsk = str(basket)
    chosen: list[tuple[dict[str, Any], str]] = []
    if bsk.startswith("sg"):
        try:
            px = float(bsk.replace("sg", ""))
        except ValueError:
            px = 150.0
        c_row = s018.nearest(rows, True, px)
        p_row = s018.nearest(rows, False, px)
        if c_row is None or p_row is None:
            return None
        chosen = [(c_row, f"c{int(px)}"), (p_row, f"p{int(px)}")]
    elif bsk == "a4":
        atm = atm_strike_of(rows, spot)
        if atm is None:
            return None
        ks = _strikes_sorted(rows)
        if want == "long":
            below = [k for k in ks if k < atm]
            itm_k = below[-1] if below else None
            p_row = s018.nearest(rows, False, 1000.0)
            c_row = row_strike(rows, True, float(itm_k)) if itm_k is not None else None
            if p_row is None or c_row is None:
                return None
            chosen = [(p_row, "p1000"), (c_row, "c_itm")]
        else:
            above = [k for k in ks if k > atm]
            itm_k = above[0] if above else None
            c_row = s018.nearest(rows, True, 1000.0)
            p_row = row_strike(rows, False, float(itm_k)) if itm_k is not None else None
            if c_row is None or p_row is None:
                return None
            chosen = [(c_row, "c1000"), (p_row, "p_itm")]
    else:
        if want == "long":
            specs = [(False, 1000.0, "p1000"), (True, 500.0, "c500"), (True, 300.0, "c300")]
        else:
            specs = [(True, 1000.0, "c1000"), (False, 500.0, "p500"), (False, 300.0, "p300")]
        for is_call, tgt, role in specs:
            r = s018.nearest(rows, is_call, tgt)
            if r is None:
                return None
            chosen.append((r, role))
    return _legs_from_chosen(store, exp, t, spot, t_yr, dte, chosen)


def get_or_build_path(
    store: Any,
    spot_c: dict[int, float],
    t: int,
    side: str,
    tag: str,
    flipped: bool,
    dte_mode: int = 1,
    basket: str = "std",
) -> tuple[dict[str, Any] | None, bool]:
    exp = expiry_for_dte(t, dte_mode)
    fp = cache_file(t, side, exp, path_tag(tag, dte_mode, basket=basket))
    path = s020.load_path(fp)
    if path is not None:
        return path, False
    sp = spot_c.get(t)
    if sp is None:
        return None, False
    legs = pick_basket_dev(
        store, side, t, float(sp), flipped, dte_mode=dte_mode, basket=basket
    )
    if legs is None:
        return None, False
    path = s020.build_path(store, spot_c, legs, t, exp)
    if path is None:
        return None, False
    if str(basket) == "a4":
        packed = s018.load_chain(store, exp, t)
        atm = atm_strike_of(packed[0], float(sp)) if packed else None
        for lg in path["legs"]:
            lg["atm_strike"] = atm
            lg["entry_spot"] = float(sp)
    s020.save_path(fp, path)
    return path, True


def gp_at(path: dict[str, Any], i: int, nlegs: int) -> float:
    if int(nlegs) != 2:
        return float(path["pnl"][i])
    legs = list(path["legs"])
    pxs = [float(path["px0"][i]), float(path["px1"][i]), float(path["px2"][i])]
    total = 0.0
    for k, lg in enumerate(legs[:2]):
        total += (pxs[k] - float(lg["fill"])) * s018.QTY * OPTIONS_CONTRACT_VALUE
    return total


def _exit_idx_nlegs(
    path: dict[str, Any],
    i: int,
    reason: str,
    spot_c: dict[int, float],
    nlegs: int,
) -> dict[str, Any]:
    if int(nlegs) != 2:
        return s020._exit_idx(path, i, reason, spot_c)
    t = int(path["ts"][i])
    legs = list(path["legs"])[:2]
    dte = int(path["dte"])
    idx = float(path["spot"][i]) or float(spot_c.get(t, 0.0))
    pxs = [float(path["px0"][i]), float(path["px1"][i])]
    srcs_m = [int(path["src0"][i]), int(path["src1"][i])]
    gross = 0.0
    fees = sum(float(lg["fee"]) for lg in legs)
    slip = sum(float(lg["slip"]) for lg in legs)
    srcs = [str(lg["src"]) for lg in legs]
    for lg, mark, src_i in zip(legs, pxs, srcs_m):
        xf, _ = s018.sell_fill(mark, dte)
        gross += (xf - float(lg["fill"])) * s018.QTY * OPTIONS_CONTRACT_VALUE
        fees += s018.fee_gst(mark, idx if idx else 1.0)
        slip += (mark - xf) * s018.QTY * OPTIONS_CONTRACT_VALUE
        srcs.append(s020.SRC_NAME.get(src_i, "real"))
    return {
        "exit_ts": t,
        "reason": reason,
        "gross": gross,
        "fees": fees,
        "slip": slip,
        "net": gross - fees,
        "srcs": srcs,
    }


def _expiry_exit_nlegs(
    path: dict[str, Any],
    i: int,
    spot_c: dict[int, float],
    nlegs: int,
) -> dict[str, Any] | None:
    if int(nlegs) != 2:
        return s020.scan_path(path, 1e18, 1e18, spot_c)
    t = int(path["ts"][i])
    sp = float(path["spot"][i]) if float(path["spot"][i]) > 0 else float(spot_c.get(t, 0.0))
    if sp <= 0:
        return None
    legs = list(path["legs"])[:2]
    gross = 0.0
    fees = sum(float(lg["fee"]) for lg in legs)
    slip = sum(float(lg["slip"]) for lg in legs)
    srcs = [str(lg["src"]) for lg in legs]
    for lg in legs:
        px = intrinsic(bool(lg["is_call"]), float(lg["strike"]), sp)
        gross += (px - float(lg["fill"])) * s018.QTY * OPTIONS_CONTRACT_VALUE
        if px > 0:
            fees += s018.fee_gst(px, sp)
        srcs.append("settle")
    return {
        "exit_ts": t,
        "reason": "EXPIRY",
        "gross": gross,
        "fees": fees,
        "slip": slip,
        "net": gross - fees,
        "srcs": srcs,
    }


def scan_path_dev(
    path: dict[str, Any],
    tgt: float,
    sl: float,
    spot_c: dict[int, float],
    time_stop_sec: int | None,
    nlegs: int = 3,
    trail_arm: float = 0.0,
    trail_give: float = 0.0,
    prem_pct: float = 0.0,
) -> dict[str, Any] | None:
    trail_on = float(trail_arm) > 0.0
    if float(prem_pct) > 0.0:
        nuse = 2 if int(nlegs) == 2 else len(list(path["legs"]))
        debit = 0.0
        for lg in list(path["legs"])[:nuse]:
            debit += float(lg["fill"]) * s018.QTY * OPTIONS_CONTRACT_VALUE
        tgt = debit * (float(prem_pct) / 100.0)
        sl = debit * (float(prem_pct) / 100.0)
    custom = trail_on or int(nlegs) == 2 or float(prem_pct) > 0.0
    if not custom and time_stop_sec is None:
        return s020.scan_path(path, tgt, sl, spot_c)
    if not custom:
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
    ts = path["ts"]
    ok = path["ok"]
    exp_ts = int(path["exp_ts"])
    entry = int(path["entry_ts"])
    n = int(ts.size)
    stop_at = int(entry) + int(time_stop_sec) if time_stop_sec is not None else None
    armed = False
    peak = float("-inf")
    for i in range(n):
        t = int(ts[i])
        if bool(ok[i]):
            gp = gp_at(path, i, nlegs)
            if np.isfinite(gp):
                if gp <= -sl:
                    return _exit_idx_nlegs(path, i, "SL", spot_c, nlegs)
                if gp >= tgt:
                    return _exit_idx_nlegs(path, i, "TARGET", spot_c, nlegs)
                if trail_on:
                    if gp >= float(trail_arm):
                        armed = True
                    if armed:
                        peak = max(peak, gp)
                        if gp <= peak - float(trail_give):
                            return _exit_idx_nlegs(path, i, "TRAIL", spot_c, nlegs)
                if stop_at is not None and t >= stop_at:
                    return _exit_idx_nlegs(path, i, "TIME", spot_c, nlegs)
        if t == exp_ts:
            if int(nlegs) == 2:
                return _expiry_exit_nlegs(path, i, spot_c, nlegs)
            return s020.scan_path(path, tgt, sl, spot_c)
    return None


def entry_iv_avg(path: dict[str, Any], spot: float, t: int) -> float:
    exp_ts = int(path["exp_ts"])
    t_yr = t_years(t, exp_ts)
    ivs: list[float] = []
    for lg in list(path["legs"])[:3]:
        iv = implied_vol_bisection(
            float(lg["mark"]), float(spot), float(lg["strike"]), t_yr, bool(lg["is_call"])
        )
        if iv is not None and np.isfinite(iv):
            ivs.append(float(iv))
    return float(np.mean(ivs)) if ivs else float("nan")


def mfe_mae(path: dict[str, Any], nlegs: int, exit_ts: int) -> tuple[float, float]:
    mfe = float("-inf")
    mae = float("inf")
    ts = path["ts"]
    ok = path["ok"]
    n = 0
    for i in range(int(ts.size)):
        t = int(ts[i])
        if t > int(exit_ts):
            break
        if not bool(ok[i]):
            continue
        gp = gp_at(path, i, nlegs)
        if not np.isfinite(gp):
            continue
        mfe = max(mfe, gp)
        mae = min(mae, gp)
        n += 1
    if n == 0:
        return float("nan"), float("nan")
    return float(mfe), float(mae)


def iv_tercile_lines(rows: list[dict[str, Any]]) -> list[str]:
    pts = [
        (float(r["entry_iv"]), float(r["net"]))
        for r in rows
        if np.isfinite(r.get("entry_iv", float("nan"))) and np.isfinite(r.get("net", float("nan")))
    ]
    if len(pts) < 3:
        return [f"  entry_iv tercile (info): n_iv={len(pts)} (need >=3)"]
    pts.sort(key=lambda x: x[0])
    n = len(pts)
    i1, i2 = n // 3, (2 * n) // 3
    groups = [("low", pts[:i1]), ("mid", pts[i1:i2]), ("high", pts[i2:])]
    lines = ["  entry_iv tercile (info):"]
    for name, g in groups:
        if not g:
            lines.append(f"    {name} n=0")
            continue
        lines.append(
            f"    {name} n={len(g)} iv={float(np.mean([x[0] for x in g])):.3f} "
            f"mean_net={float(np.mean([x[1] for x in g])):.2f}"
        )
    return lines


def dte2_coverage(
    store: Any, spot_c: dict[int, float], plan: list[tuple[int, str]], flipped: bool = False
) -> tuple[int, int]:
    ok_n = 0
    for t, side in plan:
        sp = spot_c.get(int(t))
        if sp is None:
            continue
        if pick_basket_dev(store, str(side), int(t), float(sp), flipped, dte_mode=2) is not None:
            ok_n += 1
    return ok_n, len(plan)


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
    band: int = 0,
    band_mode: str = "near",
    nlegs: int = 3,
    dte_mode: int = 1,
    trail_arm: float = 0.0,
    trail_give: float = 0.0,
    trail_cap: float = DEFAULT_TRAIL_CAP,
    basket: str = "std",
    prem_pct: float = 0.0,
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
        extra: dict[str, Any] = {}
        if extra_by is not None:
            extra = dict(extra_by.get((int(t), str(side)), {}))
        if extra:
            if not keep_signal(extra, band, band_mode):
                continue
        elif not entry_allowed(t):
            continue
        path, newp = get_or_build_path(
            store, spot_c, t, side, tag, flipped, dte_mode=dte_mode, basket=basket
        )
        if newp:
            built += 1
        if path is None:
            n_stale += 1
            continue
        scan_tgt = float(trail_cap) if float(trail_arm) > 0.0 else float(tgt)
        walked = scan_path_dev(
            path, scan_tgt, sl, spot_c, time_stop_sec,
            nlegs=nlegs, trail_arm=trail_arm, trail_give=trail_give,
            prem_pct=prem_pct,
        )
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        exp = date.fromisoformat(str(path["exp"]))
        hrs_exp = (s018.expiry_unix(exp) - int(t)) / 3600.0
        mf, ma = mfe_mae(path, nlegs, int(walked["exit_ts"]))
        eiv = entry_iv_avg(path, float(spot_c.get(t, 0.0)), t)
        sel = list(path["legs"])[: 2 if int(nlegs) == 2 else len(list(path["legs"]))]
        nd = 0.0
        nfin = 0
        for lg in sel:
            dv = float(lg.get("delta", float("nan")))
            if np.isfinite(dv):
                nd += dv
                nfin += 1
        net_d = nd if nfin else float("nan")
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
                "entry_iv": eiv,
                "mfe": mf,
                "mae": ma,
                "exit_reason": walked.get("reason"),
                "spot": float(spot_c.get(t, 0.0)),
                "atm_strike": sel[0].get("atm_strike") if sel else None,
                "leg_strikes": [float(lg["strike"]) for lg in sel],
                "leg_deltas": [float(lg.get("delta", float("nan"))) for lg in sel],
                "leg_roles": [str(lg.get("role", "")) for lg in sel],
                "net_delta": net_d,
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
    nds = [float(r.get("net_delta", float("nan"))) for r in rows]
    nds = [x for x in nds if np.isfinite(x)]
    base["avg_net_delta"] = float(np.mean(nds)) if nds else float("nan")
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
    exp2 = expiry_2dte_bimal(t)
    print(
        f"expiry check: Monday {monday.isoformat()} 18:00 IST -> {exp.isoformat()} 17:30 IST "
        f"(expect 2025-06-04 Wednesday); 2DTE -> {exp2.isoformat()} (expect 2025-06-05)",
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
    t_all: float,
    arm_i: int,
    arm_n: int,
    max_days: int,
    band: int,
    band_mode: str,
    n_sig_raw: int,
    vwap_1m: np.ndarray,
    c: np.ndarray,
    nlegs: int = 3,
    dte_mode: int = 1,
    trail_arm: float = 0.0,
    trail_give: float = 0.0,
    trail_cap: float = DEFAULT_TRAIL_CAP,
    arm_id: str = "",
    basket: str = "std",
    prem_pct: float = 0.0,
    do_c2: bool = True,
    do_c3: bool = True,
    c2_seeds: tuple[int, ...] | None = None,
) -> None:
    time_stop = TIME_STOP_SEC if variant == "V5" else None
    extra_by = {(int(s["ts"]), str(s["side"])): s for s in sigs}
    plan_all = [(int(s["ts"]), str(s["side"])) for s in sigs if keep_signal(s, band, band_mode)]
    hour_idx = hour_idx_band(
        ts, c, vwap_1m, start_ts, cutoff, win_from, win_to, band, band_mode
    )
    tag_p = f"DEV_{variant}_{tf}"
    sim_kw: dict[str, Any] = {
        "band": band,
        "band_mode": band_mode,
        "nlegs": nlegs,
        "dte_mode": dte_mode,
        "trail_arm": trail_arm,
        "trail_give": trail_give,
        "trail_cap": trail_cap,
        "basket": basket,
        "prem_pct": prem_pct,
    }
    if int(dte_mode) >= 2:
        cov_x, cov_y = dte2_coverage(store, spot_c, plan_all, False)
        print(f"2DTE coverage {cov_x}/{cov_y}", flush=True)
    for tgt, slv in cells:
        key = cell_key(
            month, tf, variant, tgt, slv, band, band_mode,
            legs=nlegs, dte=dte_mode, trail_arm=trail_arm,
            trail_give=trail_give, trail_cap=trail_cap,
            arm_id=arm_id, basket=basket,
        )
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
            **sim_kw,
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
                "n_sig_raw": int(n_sig_raw),
                "kept_pct": (100.0 * len(plan_all) / n_sig_raw) if n_sig_raw else 0.0,
                "n_days": n_days,
                "band": int(band),
                "band_mode": str(band_mode),
                "legs": int(nlegs),
                "dte": int(dte_mode),
                "trail_arm": float(trail_arm),
                "trail_give": float(trail_give),
                "trail_cap": float(trail_cap),
                "arm_id": str(arm_id),
                "basket": str(basket),
            }
        )
        c2m = float("nan")
        c3m = float("nan")
        seeds = RANDOM_SEEDS if c2_seeds is None else c2_seeds
        if tgt == PRIMARY_T and slv == PRIMARY_SL:
            if do_c2:
                c2s: list[float] = []
                c2_pool: list[dict[str, Any]] = []
                for si, seed in enumerate(seeds, start=1):
                    print(f"C2 {key} seed {si}/{len(seeds)}", flush=True)
                    forced = s020.random_c2(rows, ts, hour_idx, seed)
                    rr, _ = simulate_plan(
                        store, spot_c, forced, float(tgt), float(slv), f"C2{tag_p}", False,
                        start_ts, cutoff, win_from, win_to, time_stop,
                        label=f"C2 {key} seed={si}",
                        **sim_kw,
                    )
                    c2_pool.extend(rr)
                    if rr:
                        c2s.append(float(np.mean([x["net"] for x in rr])))
                c2m = float(np.mean(c2s)) if c2s else float("nan")
                stt["c2"] = c2m
                stt["c2_wd"] = s020.stats_ww(s020.split_wd_we(c2_pool)[0])
                stt["c2_we"] = s020.stats_ww(s020.split_wd_we(c2_pool)[1])
            if do_c3:
                print(f"C3 {key}", flush=True)
                plan_c3 = [(int(r["entry_ts"]), str(r["side"])) for r in rows]
                c3, _ = simulate_plan(
                    store, spot_c, plan_c3, float(tgt), float(slv), f"C3{tag_p}", True,
                    start_ts, cutoff, win_from, win_to, time_stop,
                    label=f"C3 {key}",
                    **sim_kw,
                )
                c3s = stats_dev(c3)
                c3m = float(c3s["mean"])
                stt["c3"] = c3m
                stt["c3_wd"] = c3s["wd"]
                stt["c3_we"] = c3s["we"]
                stt["c3_full"] = {k: v for k, v in c3s.items() if k not in ("wd", "we")}
            if do_c2:
                stt["c2"] = c2m
            if do_c3:
                stt["c3"] = c3m
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


def summary_table(
    done: dict[str, dict[str, Any]], month: str, band: int, mode: str
) -> list[str]:
    lines = [
        "SUMMARY tf x variant @ T250/SL250:",
        f"{'tf':<5} {'var':<4} {'n':>5} {'mean':>9} {'gross':>9} {'broker':>8} {'slip':>8} {'C2':>9} {'C3':>9}",
    ]
    for tf in TFS:
        for var in VARIANTS:
            key = cell_key(month, tf, var, PRIMARY_T, PRIMARY_SL, band, mode)
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


def band_grid_table(done: dict[str, dict[str, Any]], month: str) -> list[str]:
    lines = [
        "BAND-GRID T250/SL250:",
        f"{'tf':<5} {'var':<4} {'band':>5} {'mode':<5} {'n':>5} {'kept%':>7} "
        f"{'mean':>9} {'gross':>9} {'broker':>8} {'slip':>8} {'C2':>9} {'C3':>9}",
    ]
    for tf, var, band, mode in band_grid_jobs():
        key = cell_key(month, tf, var, PRIMARY_T, PRIMARY_SL, band, mode)
        s = done.get(key)
        if s is None:
            continue
        lines.append(
            f"{tf:<5} {var:<4} {int(band):5d} {str(mode):<5} {int(s.get('n', 0)):5d} "
            f"{s020._fnum(s.get('kept_pct', float('nan')), 1):>7} "
            f"{s020._fnum(s.get('mean', float('nan')), 2):>9} "
            f"{s020._fnum(s.get('gross', float('nan')), 2):>9} "
            f"{s020._fnum(s.get('fee', float('nan')), 2):>8} "
            f"{s020._fnum(s.get('slip', float('nan')), 2):>8} "
            f"{s020._fnum(s.get('c2', float('nan')), 2):>9} "
            f"{s020._fnum(s.get('c3', float('nan')), 2):>9}"
        )
    return lines


def exit_grid_table(done: dict[str, dict[str, Any]], month: str) -> list[str]:
    lines = [
        "EXIT-GRID:",
        f"{'sig':<16} {'arm':<4} {'n':>5} {'mean':>9} {'gross':>9} "
        f"{'broker':>8} {'slip':>8} {'win%':>7} {'C2':>9} {'C3':>9}",
    ]
    for tf, var, band, mode in EXIT_GRID_SIGS:
        sig = f"{tf}/{var}/b{band}/{mode}"
        for arm in EXIT_ARMS:
            key = cell_key(
                month, tf, var, PRIMARY_T, PRIMARY_SL, band, mode,
                legs=int(arm["legs"]), dte=int(arm["dte"]),
                trail_arm=float(arm["trail_arm"]),
                trail_give=float(arm["trail_give"]),
                trail_cap=float(arm["trail_cap"]),
            )
            s = done.get(key)
            if s is None:
                continue
            lines.append(
                f"{sig:<16} {str(arm['id']):<4} {int(s.get('n', 0)):5d} "
                f"{s020._fnum(s.get('mean', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('gross', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('fee', float('nan')), 2):>8} "
                f"{s020._fnum(s.get('slip', float('nan')), 2):>8} "
                f"{s020._fnum(s.get('win', float('nan')), 1):>7} "
                f"{s020._fnum(s.get('c2', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('c3', float('nan')), 2):>9}"
            )
    return lines


def strangle_grid_table(done: dict[str, dict[str, Any]], month: str) -> list[str]:
    lines = [
        "STRANGLE-GRID (TP/SL=100% of debit):",
        f"{'sig':<16} {'arm':<4} {'n':>5} {'mean':>9} {'gross':>9} "
        f"{'broker':>8} {'slip':>8} {'win%':>7} {'C2':>9} {'C3':>9}",
    ]
    for tf, var, band, mode in EXIT_GRID_SIGS:
        sig = f"{tf}/{var}/b{band}/{mode}"
        for arm in STRANGLE_ARMS:
            key = cell_key(
                month, tf, var, PRIMARY_T, PRIMARY_SL, band, mode,
                legs=int(arm["legs"]), dte=int(arm["dte"]),
                trail_arm=0.0, trail_give=0.0, trail_cap=DEFAULT_TRAIL_CAP,
                arm_id=str(arm["id"]), basket=str(arm["basket"]),
            )
            s = done.get(key)
            if s is None:
                continue
            lines.append(
                f"{sig:<16} {str(arm['id']):<4} {int(s.get('n', 0)):5d} "
                f"{s020._fnum(s.get('mean', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('gross', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('fee', float('nan')), 2):>8} "
                f"{s020._fnum(s.get('slip', float('nan')), 2):>8} "
                f"{s020._fnum(s.get('win', float('nan')), 1):>7} "
                f"{s020._fnum(s.get('c2', float('nan')), 2):>9} "
                f"{s020._fnum(s.get('c3', float('nan')), 2):>9}"
            )
    return lines


def print_a4_leg_details(rows: list[dict[str, Any]], n: int = 2) -> None:
    print("=== A4 LEG DETAIL ===", flush=True)
    shown = 0
    for r in rows:
        roles = list(r.get("leg_roles") or [])
        strikes = list(r.get("leg_strikes") or [])
        deltas = list(r.get("leg_deltas") or [])
        itm_call = float("nan")
        itm_put = float("nan")
        for role, k in zip(roles, strikes):
            if role == "c_itm":
                itm_call = float(k)
            if role == "p_itm":
                itm_put = float(k)
        print(
            f"  {r['side']} entry={s018.ist_str(int(r['entry_ts']))} "
            f"spot={float(r.get('spot', float('nan'))):.1f} "
            f"ATM={r.get('atm_strike')} ITM_call={itm_call} ITM_put={itm_put} "
            f"strikes={strikes} deltas={deltas} net_delta={r.get('net_delta')}",
            flush=True,
        )
        shown += 1
        if shown >= n:
            break
    if shown == 0:
        print("  (no A4 trades)", flush=True)


def tf_band_work() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tf in TFBAND_TFS:
        for var in TFBAND_VARS:
            for band in TFBAND_BANDS:
                mode = "near" if int(band) == 0 else "far"
                for aid in TFBAND_ARM_IDS:
                    arm = arm_by_id(aid)
                    rec = {
                        "tf": tf,
                        "variant": var,
                        "band": int(band),
                        "band_mode": mode,
                    }
                    rec.update(work_fields_from_arm(arm))
                    rec["do_c2"] = int(band) in (0, 300)
                    rec["do_c3"] = aid == "A0"
                    rec["c2_seeds"] = RANDOM_SEEDS_20 if rec["do_c2"] else ()
                    out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--tf", default="1m", choices=list(TF_SEC))
    ap.add_argument("--variant", default="V0", choices=list(VARIANTS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--band", type=int, default=0)
    ap.add_argument("--band-mode", default="near", choices=("near", "far"))
    ap.add_argument("--band-grid", action="store_true")
    ap.add_argument("--legs", type=int, default=3, choices=(2, 3))
    ap.add_argument("--trail-arm", type=float, default=0.0)
    ap.add_argument("--trail-give", type=float, default=0.0)
    ap.add_argument("--trail-cap", type=float, default=DEFAULT_TRAIL_CAP)
    ap.add_argument("--dte", type=int, default=1, choices=(1, 2))
    ap.add_argument("--exit-grid", action="store_true")
    ap.add_argument("--strangle-grid", action="store_true")
    ap.add_argument("--tf-band-grid", action="store_true")
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
    exit_grid = bool(args.exit_grid)
    strangle_grid = bool(args.strangle_grid)
    tf_band = bool(args.tf_band_grid)
    need_d2 = exit_grid or int(args.dte) >= 2
    pad_d = 5 if need_d2 else 3
    lo = start_ts - 3 * 86400
    hi = cutoff + pad_d * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    if max_days:
        print(f"SMOKE max-days={max_days} month={month} cutoff_ts={cutoff}", flush=True)
    spot_c = {int(t): float(x) for t, x in zip(ts, c)}
    vwap_1m = s020.session_vwap(ts, h, l, c, vol)
    vwap_by_ts = {
        int(t): float(vwap_1m[i])
        for i, t in enumerate(ts)
        if np.isfinite(vwap_1m[i])
    }
    n_days = float(max_days) if max_days else float((win_to - win_from).days + 1)

    inner = MarksStore()
    store = MonthGuardStore(inner, win_from - timedelta(days=1), win_to + timedelta(days=pad_d))
    done: dict[str, dict[str, Any]] = {} if args.fresh else s020.load_ckpt(CKPT)
    cell_rows: dict[str, list[dict[str, Any]]] = {}

    grid = bool(args.band_grid)
    jobs: list[tuple[str, str, int, str]]
    work: list[dict[str, Any]] = []
    if tf_band:
        band0, mode0 = 0, "near"
        work = tf_band_work()
        tfs = sorted({str(w["tf"]) for w in work})
        variants = sorted({str(w["variant"]) for w in work})
        jobs = [(str(w["tf"]), str(w["variant"]), int(w["band"]), str(w["band_mode"])) for w in work]
    elif strangle_grid:
        band0, mode0 = 0, "near"
        tfs = sorted({s[0] for s in EXIT_GRID_SIGS})
        variants = sorted({s[1] for s in EXIT_GRID_SIGS})
        jobs = list(EXIT_GRID_SIGS)
        for tf, var, band, mode in EXIT_GRID_SIGS:
            for arm in STRANGLE_ARMS:
                rec = {"tf": tf, "variant": var, "band": band, "band_mode": mode}
                rec.update(work_fields_from_arm(arm))
                work.append(rec)
    elif exit_grid:
        band0, mode0 = 0, "near"
        tfs = sorted({s[0] for s in EXIT_GRID_SIGS})
        variants = sorted({s[1] for s in EXIT_GRID_SIGS})
        jobs = list(EXIT_GRID_SIGS)
        for tf, var, band, mode in EXIT_GRID_SIGS:
            for arm in EXIT_ARMS:
                rec = {"tf": tf, "variant": var, "band": band, "band_mode": mode}
                rec.update(work_fields_from_arm(arm))
                work.append(rec)
    elif grid:
        jobs = band_grid_jobs()
        tfs = sorted({j[0] for j in jobs})
        variants = sorted({j[1] for j in jobs})
        band0 = 0
        mode0 = "near"
        for tf, var, band, mode in jobs:
            work.append(
                {
                    "tf": tf, "variant": var, "band": band, "band_mode": mode,
                    "legs": 3, "dte": 1, "trail_arm": 0.0, "trail_give": 0.0,
                    "trail_cap": DEFAULT_TRAIL_CAP, "arm_id": "",
                    "basket": "std", "prem_pct": 0.0,
                }
            )
    else:
        tfs = list(TFS) if args.all else [str(args.tf)]
        variants = list(VARIANTS) if args.all else [str(args.variant)]
        band0 = int(args.band)
        mode0 = str(args.band_mode)
        jobs = [(tf, var, band0, mode0) for tf in tfs for var in variants]
        for tf, var, band, mode in jobs:
            work.append(
                {
                    "tf": tf, "variant": var, "band": band, "band_mode": mode,
                    "legs": int(args.legs), "dte": int(args.dte),
                    "trail_arm": float(args.trail_arm),
                    "trail_give": float(args.trail_give),
                    "trail_cap": float(args.trail_cap),
                    "arm_id": "",
                    "basket": "std", "prem_pct": 0.0,
                }
            )
    arm_n = max(1, len(work))
    arm_i = 0
    t_all = time.perf_counter()

    smoke_sigs: list[dict[str, Any]] = []
    sig_cache: dict[tuple[str, str], tuple[list[DevLine], list[dict[str, Any]], int]] = {}
    for w in work:
        tf = str(w["tf"])
        variant = str(w["variant"])
        band = int(w["band"])
        band_mode = str(w["band_mode"])
        nlegs = int(w["legs"])
        dte_mode = int(w["dte"])
        trail_arm = float(w["trail_arm"])
        trail_give = float(w["trail_give"])
        trail_cap = float(w["trail_cap"])
        arm_id = str(w["arm_id"])
        basket = str(w.get("basket", "std"))
        prem_pct = float(w.get("prem_pct", 0.0))
        do_c2 = bool(w.get("do_c2", True))
        do_c3 = bool(w.get("do_c3", True))
        c2_seeds = w.get("c2_seeds")
        ck = (tf, variant)
        if ck not in sig_cache:
            tf_sec = LINE_TF[tf]
            tts, to_, th, tl, tc, tv = resample_tf(ts, o, h, l, c, vol, tf_sec)
            vwap_tf = s020.session_vwap(tts, th, tl, tc, tv)
            raw_lines, _, _ = s020.detect_swings(tts, to_, th, tl, tc, vwap_tf)
            dlines = tf_lines_to_dev(tts, raw_lines, tf_sec)
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
            raw_sigs, both_skip = collect_signals(ts, o, h, l, c, lines_copy, variant)
            win_sigs = [
                s
                for s in raw_sigs
                if start_ts <= int(s["ts"]) < cutoff
                and s020.in_window(int(s["ts"]), win_from, win_to)
            ]
            attach_vwap_dist(win_sigs, vwap_by_ts)
            sig_cache[ck] = (dlines, win_sigs, both_skip)
        dlines, win_sigs, both_skip = sig_cache[ck]
        n_raw = len(win_sigs)
        sigs = [s for s in win_sigs if keep_signal(s, band, band_mode)]
        kept_pct = (100.0 * len(sigs) / n_raw) if n_raw else 0.0
        if not tf_band:
            print(
                f"S020-DEV {month} tf={tf} {variant} band={band} mode={band_mode} "
                f"lines={len(dlines)} kept {len(sigs)} of {n_raw} signals ({kept_pct:.1f}%) "
                f"both_skip={both_skip}",
                flush=True,
            )
            bd = signal_breakdown(win_sigs, band)
            print(
                f"signals total={bd['total']} | time_skip_lunch={bd['lunch']} | "
                f"time_skip_thusat={bd['thusat']} | vwap_nan={bd['vwap_nan']} | "
                f"near(<={band})={bd['near']} | far(>{band})={bd['far']} | "
                f"other={bd['other']} | sum={bd['sum']} "
                f"(kept Y=window_raw not time-allowed)",
                flush=True,
            )
            if bd["other"]:
                print(f"other examples: {bd['other_ex']}", flush=True)
        if max_days and variant == "V3":
            smoke_sigs = sigs
            print_v3_examples(sigs, ts)
        if grid or exit_grid or strangle_grid or tf_band:
            cells = [(PRIMARY_T, PRIMARY_SL)]
        else:
            v0_best = best_v0_cell(done, month, tf)
            cells = cells_for(variant, v0_best if variant != "V0" else None)
        arm_i += 1
        run_combo(
            store, spot_c, ts, win_sigs, variant, tf, month, cells,
            start_ts, cutoff, win_from, win_to, n_days,
            done, cell_rows, t_all, arm_i, arm_n, max_days,
            band, band_mode, n_raw, vwap_1m, c,
            nlegs=nlegs, dte_mode=dte_mode, trail_arm=trail_arm,
            trail_give=trail_give, trail_cap=trail_cap, arm_id=arm_id,
            basket=basket, prem_pct=prem_pct,
            do_c2=do_c2, do_c3=do_c3,
            c2_seeds=tuple(c2_seeds) if c2_seeds is not None else None,
        )
        pk = cell_key(
            month, tf, variant, PRIMARY_T, PRIMARY_SL, band, band_mode,
            legs=nlegs, dte=dte_mode, trail_arm=trail_arm,
            trail_give=trail_give, trail_cap=trail_cap,
            arm_id=arm_id, basket=basket,
        )
        st = done.get(pk, {})
        if tf_band:
            print(
                f"[{arm_i}/{arm_n}] {tf} {variant} band={band} {arm_id or '-'} "
                f"n={int(st.get('n', 0))} mean={s020._fnum(st.get('mean', float('nan')), 2)} "
                f"elapsed={time.perf_counter()-t_all:.0f}s",
                flush=True,
            )
        elif grid or exit_grid or strangle_grid:
            print(
                f"[{arm_i}/{arm_n}] {tf} {variant} {arm_id or '-'} L{nlegs} D{dte_mode} "
                f"band={band} {band_mode} n={int(st.get('n', 0))} "
                f"mean={s020._fnum(st.get('mean', float('nan')), 2)} "
                f"gross={s020._fnum(st.get('gross', float('nan')), 2)} "
                f"elapsed={time.perf_counter()-t_all:.0f}s",
                flush=True,
            )
        if max_days and arm_id == "A4" and arm_i == 4:
            print_a4_leg_details(cell_rows.get(pk, []), 2)
        if max_days and variant == "V3":
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

    wh = work[0]
    hdr_st = done.get(
        cell_key(
            month, str(wh["tf"]), str(wh["variant"]), PRIMARY_T, PRIMARY_SL,
            int(wh["band"]), str(wh["band_mode"]),
            legs=int(wh["legs"]), dte=int(wh["dte"]),
            trail_arm=float(wh["trail_arm"]), trail_give=float(wh["trail_give"]),
            trail_cap=float(wh["trail_cap"]),
            arm_id=str(wh.get("arm_id", "")), basket=str(wh.get("basket", "std")),
        ),
        next(iter(done.values()), {}),
    )
    report = [
        f"S020 DEV month={month} {win_from}..{win_to} stamp={stamp}",
        f"band={band0} mode={mode0} kept {int(hdr_st.get('n_sig', 0))} of "
        f"{int(hdr_st.get('n_sig_raw', 0))} signals "
        f"({s020._fnum(hdr_st.get('kept_pct', float('nan')), 1)}%)",
        "1DTE=Bimal (<17:30 IST next day; >=17:30 day-after-next); skip 05:30-08:30 IST; "
        "skip Thu 17:30-Sat 17:30 IST; TRAIN ckpt/cache untouched",
        "fees=estimate_option_fee*1.18; slip=slip_pct; stale>5m skip",
        "NOTE: weekday/weekend splits are informational; a split is only actionable "
        "if it holds in TRAIN and HOLDOUT.",
    ]
    seen_keys: set[str] = set()
    for w in work:
        tf = str(w["tf"])
        variant = str(w["variant"])
        band = int(w["band"])
        band_mode = str(w["band_mode"])
        cell_list = (
            [(PRIMARY_T, PRIMARY_SL)]
            if grid or exit_grid or strangle_grid or tf_band
            else cells_for(variant, best_v0_cell(done, month, tf) if variant != "V0" else None)
        )
        for tgt, slv in cell_list:
            key = cell_key(
                month, tf, variant, tgt, slv, band, band_mode,
                legs=int(w["legs"]), dte=int(w["dte"]),
                trail_arm=float(w["trail_arm"]), trail_give=float(w["trail_give"]),
                trail_cap=float(w["trail_cap"]),
                arm_id=str(w.get("arm_id", "")), basket=str(w.get("basket", "std")),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            st = done.get(key)
            if st is None:
                continue
            if not tf_band:
                report.append(
                    f"{key} kept {int(st.get('n_sig', 0))} of {int(st.get('n_sig_raw', 0))} "
                    f"({s020._fnum(st.get('kept_pct', float('nan')), 1)}%) ALL {fmt_dev(st)}"
                )
                if isinstance(st.get("wd"), dict):
                    report.append(f"  WEEKDAY {s020.fmt_ww(st['wd'])}")
                    report.append(f"  WEEKEND {s020.fmt_ww(st['we'])}")
                report.extend(iv_tercile_lines(cell_rows.get(key, [])))
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
                if str(w.get("arm_id", "")) in ("A2", "A4"):
                    report.append(
                        f"  avg_net_delta={s020._fnum(st.get('avg_net_delta', float('nan')), 4)}"
                    )
    if exit_grid:
        report.extend(exit_grid_table(done, month))
    elif strangle_grid:
        report.extend(strangle_grid_table(done, month))
    elif tf_band:
        d2 = [float(s.get("avg_net_delta", float("nan"))) for s in done.values() if str(s.get("arm_id")) == "A2"]
        d4 = [float(s.get("avg_net_delta", float("nan"))) for s in done.values() if str(s.get("arm_id")) == "A4"]
        d2 = [x for x in d2 if np.isfinite(x)]
        d4 = [x for x in d4 if np.isfinite(x)]
        report.append(
            f"avg_net_delta A2={s020._fnum(float(np.mean(d2)) if d2 else float('nan'), 4)} "
            f"A4={s020._fnum(float(np.mean(d4)) if d4 else float('nan'), 4)}"
        )
    elif grid:
        report.extend(band_grid_table(done, month))
    else:
        report.extend(summary_table(done, month, band0, mode0))
    txtp = OUT_DIR / f"s020_dev_{month}_{stamp}.txt"
    txtp.write_text("\n".join(report) + "\n", encoding="utf-8")
    prim_key = cell_key(
        month, str(wh["tf"]), str(wh["variant"]), PRIMARY_T, PRIMARY_SL,
        int(wh["band"]), str(wh["band_mode"]),
        legs=int(wh["legs"]), dte=int(wh["dte"]),
        trail_arm=float(wh["trail_arm"]), trail_give=float(wh["trail_give"]),
        trail_cap=float(wh["trail_cap"]),
        arm_id=str(wh.get("arm_id", "")), basket=str(wh.get("basket", "std")),
    )
    prow = cell_rows.get(prim_key, [])
    csvp = OUT_DIR / f"s020_dev_{month}_{stamp}_trades.csv"
    with csvp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "side", "entry_ts_ist", "exit_ts_ist", "reason", "gross", "fees", "net",
                "hold_hrs", "hrs_to_exp", "exp", "vwap_dist",
                "entry_iv", "mfe", "mae", "exit_reason",
            ],
        )
        w.writeheader()
        for r in prow:
            vd = r.get("vwap_dist")
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
                    "vwap_dist": vd,
                    "entry_iv": r.get("entry_iv"),
                    "mfe": r.get("mfe"),
                    "mae": r.get("mae"),
                    "exit_reason": r.get("exit_reason", r.get("reason")),
                }
            )
    if tf_band:
        tbcsv = OUT_DIR / f"s020_tfband_{month}_{stamp}.csv"
        with tbcsv.open("w", newline="", encoding="utf-8") as f:
            tw = csv.DictWriter(
                f,
                fieldnames=[
                    "month", "tf", "variant", "band", "arm", "n", "mean", "gross",
                    "brokerage", "slippage", "win", "C2", "C3", "avg_net_delta",
                ],
            )
            tw.writeheader()
            for wj in work:
                k = cell_key(
                    month, str(wj["tf"]), str(wj["variant"]), PRIMARY_T, PRIMARY_SL,
                    int(wj["band"]), str(wj["band_mode"]),
                    legs=int(wj["legs"]), dte=int(wj["dte"]),
                    trail_arm=float(wj["trail_arm"]), trail_give=float(wj["trail_give"]),
                    trail_cap=float(wj["trail_cap"]),
                    arm_id=str(wj.get("arm_id", "")), basket=str(wj.get("basket", "std")),
                )
                s = done.get(k, {})
                c2v = s.get("c2", float("nan"))
                c3v = s.get("c3", float("nan"))
                tw.writerow(
                    {
                        "month": month,
                        "tf": wj["tf"],
                        "variant": wj["variant"],
                        "band": wj["band"],
                        "arm": wj.get("arm_id", ""),
                        "n": s.get("n", 0),
                        "mean": s.get("mean", float("nan")),
                        "gross": s.get("gross", float("nan")),
                        "brokerage": s.get("fee", float("nan")),
                        "slippage": s.get("slip", float("nan")),
                        "win": s.get("win", float("nan")),
                        "C2": "-" if not np.isfinite(c2v) else c2v,
                        "C3": "-" if not np.isfinite(c3v) else c3v,
                        "avg_net_delta": s.get("avg_net_delta", float("nan")),
                    }
                )
        print(f"wrote {tbcsv}", flush=True)
    print("\n".join(report))
    print(f"wrote {txtp}")
    print(f"wrote {csvp}")
    print(f"TOTAL elapsed={time.perf_counter()-t_all:.0f}s", flush=True)
    store.close()


if __name__ == "__main__":
    main()
