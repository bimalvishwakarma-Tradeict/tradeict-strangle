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
import json
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

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE, estimate_option_fee  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, load_symbol_series, to_unix  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.harness.run_archive import (  # noqa: E402
    SNAPSHOT_SEC,
    RunArchive,
    greeks_from_mark,
)
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
ARCHIVE_PTR = OUT_DIR / "s020_dev_archive_ptr.json"

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
HEDGE_LONG = ("L2", "L3")
HEDGE_E = (0, 1)
HEDGE_OFF = (0, 300, 600)
HEDGE_W = (500, 1000)
HEDGE_Q = (0.25, 0.5)
HEDGE_TRAIL_ARM = 80.0
HEDGE_TRAIL_GIVE = 60.0
HEDGE_TRAIL_CAP = 400.0
HEDGE_LONG_SL = 250.0
HEDGE_N_COMBOS = 100
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
    "strangle_exit": False,
}
# Long strangle (buy call≈X + put≈X). Exits vs P = fill-sum USD:
# SL -0.50P, trail arm +0.30P give 0.20P, cap +1.00P.
SG_SL_FRAC = 0.50
SG_TRAIL_ARM_FRAC = 0.30
SG_TRAIL_GIVE_FRAC = 0.20
SG_CAP_FRAC = 1.00
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
        "prem_pct": 0.0,
        "strangle_exit": True,
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
        "prem_pct": 0.0,
        "strangle_exit": True,
        "sx": 300.0,
    },
)
TFBAND_TFS = ("5m", "15m", "30m")
TFBAND_VARS = ("V0", "V5")
TFBAND_BANDS = (0, 100, 200, 300, 400, 500, 600, 700, 800)
TFBAND_ARM_IDS = ("A0", "A1", "A2", "A4")
DUMP_N_COMBOS = 224
DUMP_TRADE_COLS: tuple[str, ...] = (
    "month", "tf", "variant", "band", "band_mode", "arm", "n_legs", "dte",
    "side", "entry_ist", "exit_ist", "exit_reason", "hold_hrs", "hrs_to_exp", "expiry",
    "spot_entry", "spot_exit", "line_level", "vwap_at_entry", "vwap_dist", "entry_iv",
    "leg1_symbol", "leg1_strike", "leg1_type", "leg1_entry_fill", "leg1_exit_px", "leg1_delta",
    "leg2_symbol", "leg2_strike", "leg2_type", "leg2_entry_fill", "leg2_exit_px", "leg2_delta",
    "leg3_symbol", "leg3_strike", "leg3_type", "leg3_entry_fill", "leg3_exit_px", "leg3_delta",
    "basket_net_delta", "gross", "brokerage", "slippage", "net", "mfe", "mae",
)


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
        key += f"|{bid or bsk}|sgx"
    if bid.startswith("L2") or bid.startswith("L3"):
        key += f"|{bid}"
    return key


def path_tag(base: str, dte: int, basket: str = "std") -> str:
    tag = str(base)
    bsk = str(basket)
    if bsk == "a4":
        tag = f"{tag}_A4"
    elif bsk.startswith("sg"):
        tag = f"{tag}_{bsk}"
    elif bsk.startswith("hdg"):
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
        "strangle_exit": bool(arm.get("strangle_exit", False)),
        "hedge": bool(arm.get("hedge", False)),
        "hedge_e": int(arm.get("hedge_e", -1)),
        "hedge_off": float(arm.get("hedge_off", 0.0)),
        "hedge_w": float(arm.get("hedge_w", 0.0)),
        "hedge_q": float(arm.get("hedge_q", 0.0)),
        "long_kind": str(arm.get("long_kind", "")),
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


def expiry_0dte_bimal(t: int) -> date:
    """<17:30 IST -> aaj 17:30; >=17:30 -> kal 17:30."""
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    d = dt.date()
    cut = dt.replace(hour=17, minute=30, second=0, microsecond=0)
    if dt < cut:
        return d
    return d + timedelta(days=1)


def fee_gst_qty(prem: float, index: float, qty: int) -> float:
    if int(qty) <= 0:
        return 0.0
    return float(estimate_option_fee(premium=prem, qty_lots=int(qty), btc_index=index)) * s018.GST


def nearest_listed(ks: list[float], target: float) -> float | None:
    if not ks:
        return None
    return min(ks, key=lambda k: (abs(float(k) - float(target)), float(k)))


def pick_short_hedge_rows(
    store: Any, t: int, spot: float, e: int, off: float, wing: float
) -> tuple[list[tuple[dict[str, Any], str]], date] | None:
    exp = expiry_0dte_bimal(t) if int(e) == 0 else expiry_1dte_bimal(t)
    packed = s018.load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _ = packed
    ks = _strikes_sorted(rows)
    atm = atm_strike_of(rows, spot)
    if atm is None:
        return None
    sc = nearest_listed(ks, float(atm) + float(off))
    spu = nearest_listed(ks, float(atm) - float(off))
    if sc is None or spu is None:
        return None
    wc = nearest_listed(ks, float(sc) + float(wing))
    wp = nearest_listed(ks, float(spu) - float(wing))
    if wc is None or wp is None:
        return None
    specs = [
        (True, float(sc), "scall"),
        (False, float(spu), "sput"),
        (True, float(wc), "wcall"),
        (False, float(wp), "wput"),
    ]
    chosen: list[tuple[dict[str, Any], str]] = []
    for is_call, k, role in specs:
        r = row_strike(rows, is_call, k)
        if r is None:
            return None
        chosen.append((r, role))
    return chosen, exp


def _leg_dict(
    r: dict[str, Any],
    q: Any,
    fill: float,
    dlt: float,
    role: str,
    long_short: str,
    qty: int,
    exp: date,
    fee: float,
    slip: float,
) -> dict[str, Any]:
    return {
        "symbol": str(r["symbol"]),
        "strike": float(r["strike"]),
        "is_call": bool(r["is_call"]),
        "role": role,
        "mark": float(q.px),
        "fill": float(fill),
        "src": q.src,
        "delta": dlt,
        "ts": q.ts,
        "fee": float(fee),
        "slip": float(slip),
        "qty": int(qty),
        "long_short": long_short,
        "expiry": exp.isoformat(),
        "exp_ts": s018.expiry_unix(exp),
    }


def save_path_hedge(p: Path, obj: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps(
        {
            "exp": obj["exp"],
            "legs": obj["legs"],
            "hedge": True,
            "nlong": int(obj["nlong"]),
            "short_exp": obj.get("short_exp", ""),
            "short_settle_fee": float(obj.get("short_settle_fee", 0.0)),
            "short_settle_slip": float(obj.get("short_settle_slip", 0.0)),
            "short_settle_gross": float(obj.get("short_settle_gross", 0.0)),
        }
    )
    np.savez_compressed(
        p,
        ts=obj["ts"],
        ok=obj["ok"],
        pnl=obj["pnl"],
        pnl_long=obj["pnl_long"],
        pnl_short=obj["pnl_short"],
        spot=obj["spot"],
        px=np.stack(obj["pxs"]),
        src=np.stack(obj["srcs"]),
        entry_ts=np.array([obj["entry_ts"]], dtype=np.int64),
        exp_ts=np.array([obj["exp_ts"]], dtype=np.int64),
        short_exp_ts=np.array([int(obj.get("short_exp_ts") or 0)], dtype=np.int64),
        dte=np.array([obj["dte"]], dtype=np.int32),
        meta_json=np.array([meta.encode("utf-8")]),
    )


def load_path_hedge(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    z = np.load(p, allow_pickle=False)
    raw = z["meta_json"][0]
    meta = json.loads(bytes(raw).decode("utf-8") if isinstance(raw, (bytes, np.bytes_)) else str(raw))
    px = z["px"]
    src = z["src"]
    pxs = [np.array(px[k]) for k in range(int(px.shape[0]))]
    srcs = [np.array(src[k]) for k in range(int(src.shape[0]))]
    return {
        "entry_ts": int(z["entry_ts"][0]),
        "exp": str(meta["exp"]),
        "exp_ts": int(z["exp_ts"][0]),
        "dte": int(z["dte"][0]),
        "legs": meta["legs"],
        "hedge": True,
        "nlong": int(meta.get("nlong", 0)),
        "short_exp": str(meta.get("short_exp", "")),
        "short_settle_fee": float(meta.get("short_settle_fee", 0.0)),
        "short_settle_slip": float(meta.get("short_settle_slip", 0.0)),
        "short_settle_gross": float(meta.get("short_settle_gross", 0.0)),
        "ts": z["ts"],
        "ok": z["ok"],
        "pnl": z["pnl"],
        "pnl_long": z["pnl_long"],
        "pnl_short": z["pnl_short"],
        "spot": z["spot"],
        "pxs": pxs,
        "srcs": srcs,
        "short_exp_ts": int(z["short_exp_ts"][0]),
    }


def build_path_hedge(
    store: Any,
    spot_c: dict[int, float],
    legs: list[dict[str, Any]],
    entry_ts: int,
    long_exp: date,
    nlong: int,
) -> dict[str, Any] | None:
    long_exp_ts = s018.expiry_unix(long_exp)
    shorts = [lg for lg in legs if str(lg["long_short"]) == "short"]
    short_exp_ts = int(shorts[0]["exp_ts"]) if shorts else 0
    short_exp = date.fromisoformat(str(shorts[0]["expiry"])) if shorts else long_exp
    series = [load_symbol_series(store, str(lg["symbol"]), entry_ts, int(lg["exp_ts"])) for lg in legs]
    ts = np.arange(int(entry_ts) + 60, int(long_exp_ts) + 1, 60, dtype=np.int64)
    n = int(ts.size)
    if n <= 0:
        return None
    nleg = len(legs)
    ok = np.zeros(n, dtype=np.bool_)
    pnl = np.full(n, np.nan, dtype=np.float32)
    pnl_l = np.full(n, np.nan, dtype=np.float32)
    pnl_s = np.full(n, np.nan, dtype=np.float32)
    spot = np.zeros(n, dtype=np.float32)
    pxs = [np.full(n, np.nan, dtype=np.float32) for _ in range(nleg)]
    srcs = [np.zeros(n, dtype=np.int8) for _ in range(nleg)]
    settled = False
    settle_gp = 0.0
    settle_fee = 0.0
    settle_slip = 0.0
    settle_gross = 0.0
    for i in range(n):
        t = int(ts[i])
        sp = float(spot_c.get(t, 0.0))
        spot[i] = sp
        qs: list[Any] = []
        missing = False
        for k, lg in enumerate(legs):
            is_short = str(lg["long_short"]) == "short"
            if is_short and short_exp_ts and t >= short_exp_ts:
                qs.append(None)
                continue
            q = s018.series_le(series[k], t)
            if q is None:
                missing = True
                break
            qs.append(q)
        if missing:
            continue
        long_gp = 0.0
        short_gp = 0.0
        for k, lg in enumerate(legs):
            qty = int(lg["qty"])
            is_short = str(lg["long_short"]) == "short"
            if is_short and short_exp_ts and t >= short_exp_ts:
                if not settled:
                    inn = intrinsic(bool(lg["is_call"]), float(lg["strike"]), sp)
                    pxs[k][i] = np.float32(inn)
                    srcs[k][i] = np.int8(3)
                    gp = (float(lg["fill"]) - inn) * qty * OPTIONS_CONTRACT_VALUE
                    settle_gp += gp
                    settle_gross += gp
                    if inn > 0:
                        settle_fee += fee_gst_qty(inn, sp if sp else 1.0, qty)
                else:
                    pxs[k][i] = pxs[k][i - 1] if i else np.float32(np.nan)
                continue
            q = qs[k]
            pxs[k][i] = np.float32(q.px)
            srcs[k][i] = np.int8(s020.SRC_CODE.get(q.src, 0))
            gp = (float(q.px) - float(lg["fill"])) * qty * OPTIONS_CONTRACT_VALUE
            if is_short:
                short_gp += -gp
            else:
                long_gp += gp
        if shorts and short_exp_ts and t >= short_exp_ts:
            if not settled:
                settled = True
            short_gp = settle_gp
        ok[i] = True
        pnl_l[i] = np.float32(long_gp)
        pnl_s[i] = np.float32(short_gp)
        pnl[i] = np.float32(long_gp + short_gp)
    dte = max(0, (long_exp - ist_date(entry_ts)).days)
    return {
        "entry_ts": int(entry_ts),
        "exp": long_exp.isoformat(),
        "exp_ts": long_exp_ts,
        "dte": dte,
        "legs": legs,
        "hedge": True,
        "nlong": int(nlong),
        "ts": ts,
        "ok": ok,
        "pnl": pnl,
        "pnl_long": pnl_l,
        "pnl_short": pnl_s,
        "spot": spot,
        "pxs": pxs,
        "srcs": srcs,
        "short_exp_ts": int(short_exp_ts),
        "short_exp": short_exp.isoformat() if shorts else "",
        "short_settle_fee": float(settle_fee),
        "short_settle_slip": float(settle_slip),
        "short_settle_gross": float(settle_gross),
    }


def get_or_build_hedge_path(
    store: Any,
    spot_c: dict[int, float],
    t: int,
    side: str,
    tag: str,
    long_kind: str,
    hedge_e: int,
    hedge_off: float,
    hedge_w: float,
    hedge_q: float,
) -> tuple[dict[str, Any] | None, bool]:
    nlong = 2 if str(long_kind) == "L2" else 3
    bsk = f"hdg_{long_kind}_REF" if float(hedge_q) <= 0 else (
        f"hdg_{long_kind}_E{int(hedge_e)}_o{int(hedge_off)}_W{int(hedge_w)}_q{int(round(float(hedge_q)*100))}"
    )
    long_exp = expiry_2dte_bimal(t)
    fp = cache_file(t, side, long_exp, path_tag(tag, 2, basket=bsk))
    path = load_path_hedge(fp)
    if path is not None:
        return path, False
    sp = spot_c.get(t)
    if sp is None:
        return None, False
    long_obj = pick_basket_dev(store, side, t, float(sp), False, dte_mode=2, basket="std")
    if long_obj is None:
        return None, False
    t_yr_l = t_years(t, s018.expiry_unix(long_exp))
    dte_l = max(0, (long_exp - ist_date(t)).days)
    qty_l = int(s018.QTY)
    legs: list[dict[str, Any]] = []
    for lg in long_obj[:nlong]:
        fee = fee_gst_qty(float(lg.mark), float(sp), qty_l)
        slip = (float(lg.fill) - float(lg.mark)) * qty_l * OPTIONS_CONTRACT_VALUE
        legs.append(
            {
                "symbol": lg.symbol,
                "strike": lg.strike,
                "is_call": lg.is_call,
                "role": lg.role,
                "mark": lg.mark,
                "fill": lg.fill,
                "src": lg.src,
                "delta": lg.delta,
                "ts": lg.ts,
                "fee": fee,
                "slip": slip,
                "qty": qty_l,
                "long_short": "long",
                "expiry": long_exp.isoformat(),
                "exp_ts": s018.expiry_unix(long_exp),
            }
        )
    if float(hedge_q) > 0:
        packed = pick_short_hedge_rows(store, t, float(sp), int(hedge_e), float(hedge_off), float(hedge_w))
        if packed is None:
            return None, False
        chosen, sexp = packed
        qty_s = int(round(float(hedge_q) * float(s018.QTY)))
        dte_s = max(0, (sexp - ist_date(t)).days)
        t_yr_s = t_years(t, s018.expiry_unix(sexp))
        for r, role in chosen:
            q = s018.mark_le(store, sexp, str(r["symbol"]), t)
            if q is None:
                return None, False
            fill, _ = s018.sell_fill(q.px, dte_s)
            dlt = s018.signed_delta(q.px, float(sp), float(r["strike"]), t_yr_s, bool(r["is_call"]))
            fee = fee_gst_qty(float(q.px), float(sp), qty_s)
            slip = (float(fill) - float(q.px)) * qty_s * OPTIONS_CONTRACT_VALUE
            legs.append(_leg_dict(r, q, fill, dlt, role, "short", qty_s, sexp, fee, slip))
    path = build_path_hedge(store, spot_c, legs, t, long_exp, nlong)
    if path is None:
        return None, False
    save_path_hedge(fp, path)
    return path, True


def scan_hedge_path(path: dict[str, Any], spot_c: dict[int, float]) -> dict[str, Any] | None:
    ts = path["ts"]
    ok = path["ok"]
    exp_ts = int(path["exp_ts"])
    armed = False
    peak = float("-inf")
    n = int(ts.size)
    for i in range(n):
        t = int(ts[i])
        if bool(ok[i]) and np.isfinite(path["pnl"][i]):
            long_gp = float(path["pnl_long"][i])
            comb = float(path["pnl"][i])
            if long_gp <= -float(HEDGE_LONG_SL):
                return _hedge_exit(path, i, "LOSS", spot_c)
            if comb >= float(HEDGE_TRAIL_CAP):
                return _hedge_exit(path, i, "TARGET", spot_c)
            if comb >= float(HEDGE_TRAIL_ARM):
                armed = True
            if armed:
                peak = max(peak, comb)
                if comb <= peak - float(HEDGE_TRAIL_GIVE):
                    return _hedge_exit(path, i, "TRAIL", spot_c)
        if t == exp_ts:
            return _hedge_exit(path, i, "EXPIRY", spot_c)
    return None


def _hedge_exit(
    path: dict[str, Any], i: int, reason: str, spot_c: dict[int, float]
) -> dict[str, Any]:
    t = int(path["ts"][i])
    sp = float(path["spot"][i]) or float(spot_c.get(t, 0.0))
    short_exp_ts = int(path.get("short_exp_ts") or 0)
    fees = sum(float(lg["fee"]) for lg in path["legs"])
    slip = sum(float(lg["slip"]) for lg in path["legs"])
    fees += float(path.get("short_settle_fee", 0.0))
    slip += float(path.get("short_settle_slip", 0.0))
    gross_l = 0.0
    gross_s = float(path.get("short_settle_gross", 0.0)) if short_exp_ts and t >= short_exp_ts else 0.0
    for k, lg in enumerate(path["legs"]):
        qty = int(lg["qty"])
        is_short = str(lg["long_short"]) == "short"
        if is_short and short_exp_ts and t >= short_exp_ts:
            continue
        mark = float(path["pxs"][k][i])
        dte = max(0, (date.fromisoformat(str(lg["expiry"])) - ist_date(t)).days)
        if is_short:
            xf, _ = s018.buy_fill(mark, dte)
            gp = (float(lg["fill"]) - xf) * qty * OPTIONS_CONTRACT_VALUE
            fees += fee_gst_qty(mark, sp if sp else 1.0, qty)
            slip += (xf - mark) * qty * OPTIONS_CONTRACT_VALUE
            gross_s += gp
        elif reason == "EXPIRY":
            xf = intrinsic(bool(lg["is_call"]), float(lg["strike"]), sp)
            gp = (xf - float(lg["fill"])) * qty * OPTIONS_CONTRACT_VALUE
            if xf > 0:
                fees += fee_gst_qty(xf, sp if sp else 1.0, qty)
            gross_l += gp
        else:
            xf, _ = s018.sell_fill(mark, dte)
            gp = (xf - float(lg["fill"])) * qty * OPTIONS_CONTRACT_VALUE
            fees += fee_gst_qty(mark, sp if sp else 1.0, qty)
            slip += (mark - xf) * qty * OPTIONS_CONTRACT_VALUE
            gross_l += gp
    gross = gross_l + gross_s
    return {
        "exit_ts": t,
        "reason": reason,
        "gross": gross,
        "fees": fees,
        "slip": slip,
        "net": gross - fees,
        "long_pnl": gross_l,
        "short_pnl": gross_s,
        "spot_exit": sp,
        "srcs": [],
    }


def short_hedge_coverage(
    store: Any, spot_c: dict[int, float], plan: list[tuple[int, str]], e: int
) -> tuple[int, int]:
    ok_n = 0
    for t, _side in plan:
        sp = spot_c.get(int(t))
        if sp is None:
            continue
        if pick_short_hedge_rows(store, int(t), float(sp), int(e), 0.0, 500.0) is not None:
            ok_n += 1
    return ok_n, len(plan)


def gp_at(path: dict[str, Any], i: int, nlegs: int) -> float:
    if path.get("hedge"):
        return float(path["pnl"][i])
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


def basket_entry_prem(path: dict[str, Any], nlegs: int) -> float:
    nuse = 2 if int(nlegs) == 2 else len(list(path["legs"]))
    tot = 0.0
    for lg in list(path["legs"])[:nuse]:
        tot += float(lg["fill"]) * s018.QTY * OPTIONS_CONTRACT_VALUE
    return tot


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
    strangle_exit: bool = False,
) -> dict[str, Any] | None:
    trail_on = float(trail_arm) > 0.0
    if strangle_exit:
        p = basket_entry_prem(path, nlegs)
        sl = float(SG_SL_FRAC) * p
        tgt = float(SG_CAP_FRAC) * p
        trail_arm = float(SG_TRAIL_ARM_FRAC) * p
        trail_give = float(SG_TRAIL_GIVE_FRAC) * p
        trail_on = True
    custom = trail_on or int(nlegs) == 2 or strangle_exit
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
    mf, ma, _, _ = mfe_mae_with_ts(path, nlegs, exit_ts)
    return mf, ma


def mfe_mae_with_ts(
    path: dict[str, Any], nlegs: int, exit_ts: int
) -> tuple[float, float, int, int]:
    mfe = float("-inf")
    mae = float("inf")
    mfe_ts = 0
    mae_ts = 0
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
        if gp > mfe:
            mfe = gp
            mfe_ts = t
        if gp < mae:
            mae = gp
            mae_ts = t
        n += 1
    if n == 0:
        return float("nan"), float("nan"), 0, 0
    return float(mfe), float(mae), int(mfe_ts), int(mae_ts)


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


def path_exit_leg_pxs(
    path: dict[str, Any],
    exit_ts: int,
    reason: str,
    nlegs: int,
    spot_c: dict[int, float],
) -> tuple[float, list[float]]:
    ts_a = path["ts"]
    xi: int | None = None
    for i in range(int(ts_a.size)):
        if int(ts_a[i]) == int(exit_ts):
            xi = i
            break
    spot_x = float(spot_c.get(int(exit_ts), 0.0))
    nuse = int(nlegs)
    pxs = [float("nan")] * nuse
    if xi is None:
        return spot_x, pxs
    sp = float(path["spot"][xi])
    if sp > 0:
        spot_x = sp
    legs = list(path["legs"])[:nuse]
    for k, lg in enumerate(legs):
        if str(reason) == "EXPIRY":
            pxs[k] = intrinsic(bool(lg["is_call"]), float(lg["strike"]), spot_x)
            continue
        mark = float(path[f"px{k}"][xi])
        xf, _ = s018.sell_fill(mark, int(path["dte"]))
        pxs[k] = float(xf)
    return spot_x, pxs


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
    strangle_exit: bool = False,
    on_trade: Any | None = None,
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
            prem_pct=prem_pct, strangle_exit=strangle_exit,
        )
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        exp = date.fromisoformat(str(path["exp"]))
        hrs_exp = (s018.expiry_unix(exp) - int(t)) / 3600.0
        mf, ma, mfe_ts, mae_ts = mfe_mae_with_ts(path, nlegs, int(walked["exit_ts"]))
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
        spot_exit, exit_pxs = path_exit_leg_pxs(
            path, int(walked["exit_ts"]), str(walked.get("reason", "")), nlegs, spot_c
        )
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
                "mfe_ts": mfe_ts,
                "mae": ma,
                "mae_ts": mae_ts,
                "exit_reason": walked.get("reason"),
                "spot": float(spot_c.get(t, 0.0)),
                "atm_strike": sel[0].get("atm_strike") if sel else None,
                "leg_strikes": [float(lg["strike"]) for lg in sel],
                "leg_deltas": [float(lg.get("delta", float("nan"))) for lg in sel],
                "leg_roles": [str(lg.get("role", "")) for lg in sel],
                "net_delta": net_d,
                "entry_prem": basket_entry_prem(path, nlegs),
                "spot_exit": spot_exit,
                "exit_pxs": exit_pxs,
                **extra,
                **walked,
            }
        )
        if on_trade is not None:
            on_trade(rows[-1], path)
        busy = int(walked["exit_ts"])
    return rows, n_stale


def simulate_hedge_plan(
    store: Any,
    spot_c: dict[int, float],
    plan: list[tuple[int, str]],
    tag: str,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    extra_by: dict[tuple[int, str], dict[str, Any]] | None,
    band: int,
    band_mode: str,
    long_kind: str,
    hedge_e: int,
    hedge_off: float,
    hedge_w: float,
    hedge_q: float,
    label: str = "",
    on_trade: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    busy = -1
    rows: list[dict[str, Any]] = []
    n_stale = 0
    built = 0
    t0 = time.perf_counter()
    st_prog: dict[str, int] = {"mark": -1}
    nplan = len(plan)
    nlong = 2 if str(long_kind) == "L2" else 3
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
        path, newp = get_or_build_hedge_path(
            store, spot_c, t, side, tag, long_kind, hedge_e, hedge_off, hedge_w, hedge_q
        )
        if newp:
            built += 1
        if path is None:
            n_stale += 1
            continue
        walked = scan_hedge_path(path, spot_c)
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        exp = date.fromisoformat(str(path["exp"]))
        hrs_exp = (s018.expiry_unix(exp) - int(t)) / 3600.0
        mf, ma, mfe_ts, mae_ts = mfe_mae_with_ts(path, nlong, int(walked["exit_ts"]))
        sel = list(path["legs"])
        nd = 0.0
        nfin = 0
        for lg in sel:
            dv = float(lg.get("delta", float("nan")))
            pos = 1.0 if str(lg.get("long_short")) == "long" else -1.0
            if np.isfinite(dv):
                nd += pos * dv
                nfin += 1
        rows.append(
            {
                "entry_ts": t,
                "side": side,
                "hod": s018.hod_ist(t),
                "hold_hrs": hold,
                "hrs_to_exp": hrs_exp,
                "legs": sel,
                "exp": exp.isoformat(),
                "mfe": mf,
                "mfe_ts": mfe_ts,
                "mae": ma,
                "mae_ts": mae_ts,
                "exit_reason": walked.get("reason"),
                "spot": float(spot_c.get(t, 0.0)),
                "net_delta": nd if nfin else float("nan"),
                "long_pnl": walked.get("long_pnl"),
                "short_pnl": walked.get("short_pnl"),
                **extra,
                **walked,
            }
        )
        if on_trade is not None:
            on_trade(rows[-1], path)
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
    ps = [float(r.get("entry_prem", float("nan"))) for r in rows]
    ps = [x for x in ps if np.isfinite(x)]
    base["avg_p"] = float(np.mean(ps)) if ps else float("nan")
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


def path_cache_n() -> int:
    if not CACHE_DIR.exists():
        return 0
    return len(list(CACHE_DIR.glob(f"*_{PATH_VER}.npz")))


def drop_dev_fresh() -> None:
    if TRAIN_CKPT.exists():
        print(f"fresh DEV: leaving TRAIN {TRAIN_CKPT.name} untouched", flush=True)
    if CKPT.exists():
        CKPT.unlink()
        print(f"fresh: dropped {CKPT.name} (path cache kept)", flush=True)
    else:
        print("fresh: no DEV checkpoint; path cache kept", flush=True)
    if ARCHIVE_PTR.exists():
        ARCHIVE_PTR.unlink()
        print(f"fresh: dropped {ARCHIVE_PTR.name}", flush=True)


def drop_dev_cache() -> None:
    n = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob(f"*_{PATH_VER}.npz"):
            p.unlink(missing_ok=True)
            n += 1
    print(f"purge-cache: dropped {n} S020 DEV path-cache files", flush=True)


def archive_mode_name(args: argparse.Namespace) -> str:
    if bool(getattr(args, "dump_trades", False)):
        return "dump-trades"
    if bool(getattr(args, "tf_band_grid", False)):
        return "tf-band-grid"
    if bool(getattr(args, "strangle_grid", False)):
        return "strangle-grid"
    if bool(getattr(args, "exit_grid", False)):
        return "exit-grid"
    if bool(getattr(args, "hedge_grid", False)):
        return "hedge-grid"
    if bool(getattr(args, "band_grid", False)):
        return "band-grid"
    if bool(getattr(args, "all", False)):
        return "all"
    return f"{args.tf}-{args.variant}"


def _path_i_at(path: dict[str, Any], t: int) -> int | None:
    ts_a = path["ts"]
    last: int | None = None
    tgt = int(t)
    for i in range(int(ts_a.size)):
        ti = int(ts_a[i])
        if ti == tgt:
            return i
        if ti <= tgt:
            last = i
        else:
            break
    return last


def _snap_times(entry_ts: int, exit_ts: int, mfe_ts: int, mae_ts: int) -> list[tuple[int, str]]:
    ranked: dict[int, str] = {}
    pri = {"entry": 5, "exit": 4, "mfe": 3, "mae": 2, "15m": 1}
    def put(t: int, kind: str) -> None:
        if t <= 0:
            return
        old = ranked.get(int(t))
        if old is None or pri[kind] > pri[old]:
            ranked[int(t)] = kind
    put(int(entry_ts), "entry")
    t = int(entry_ts) + int(SNAPSHOT_SEC)
    while t < int(exit_ts):
        put(t, "15m")
        t += int(SNAPSHOT_SEC)
    put(int(mfe_ts), "mfe")
    put(int(mae_ts), "mae")
    put(int(exit_ts), "exit")
    return sorted(ranked.items(), key=lambda x: x[0])


def emit_archive_trade(
    archive: RunArchive,
    cfg: dict[str, Any],
    r: dict[str, Any],
    path: dict[str, Any],
    nlegs: int,
    long_short: str,
) -> None:
    tid = (
        f"{cfg.get('key','')}|{int(r['entry_ts'])}|{r['side']}"
    )
    hedge = bool(path.get("hedge"))
    nuse = len(list(path["legs"])) if hedge else (2 if int(nlegs) == 2 else len(list(path["legs"])))
    sel = list(path["legs"])[:nuse]
    exp = str(path.get("exp", r.get("exp", "")))
    exp_ts = int(path["exp_ts"])
    dte = int(path.get("dte", cfg.get("dte", 1)))
    qty = int(s018.QTY)
    pos = -1.0 if long_short == "short" else 1.0
    exit_ts = int(r["exit_ts"])
    entry_ts = int(r["entry_ts"])
    reason = str(r.get("reason", ""))
    xi = _path_i_at(path, exit_ts)
    spot_e = float(r.get("spot", 0.0))
    spot_x = float(r.get("spot_exit", 0.0))
    t_e = t_years(entry_ts, exp_ts)
    t_x = t_years(exit_ts, exp_ts)
    trade = {
        "trade_id": tid,
        "month": cfg.get("month"),
        "tf": cfg.get("tf"),
        "variant": cfg.get("variant"),
        "band": cfg.get("band"),
        "band_mode": cfg.get("band_mode"),
        "arm": cfg.get("arm_id"),
        "n_legs": nuse,
        "dte": dte,
        "side": r["side"],
        "entry_ist": s018.ist_str(entry_ts),
        "exit_ist": s018.ist_str(exit_ts),
        "exit_reason": r.get("exit_reason", reason),
        "hold_hrs": r.get("hold_hrs"),
        "hrs_to_exp": r.get("hrs_to_exp"),
        "spot_entry": spot_e,
        "spot_exit": spot_x,
        "line_level": r.get("level"),
        "vwap_dist": r.get("vwap_dist"),
        "gross": r.get("gross"),
        "brokerage": r.get("fees"),
        "slippage": r.get("slip"),
        "net": r.get("net"),
        "long_pnl": r.get("long_pnl", ""),
        "short_pnl": r.get("short_pnl", ""),
        "mfe": r.get("mfe"),
        "mfe_time": s018.ist_str(int(r["mfe_ts"])) if int(r.get("mfe_ts") or 0) else "",
        "mae": r.get("mae"),
        "mae_time": s018.ist_str(int(r["mae_ts"])) if int(r.get("mae_ts") or 0) else "",
    }
    legs_out: list[dict[str, Any]] = []
    for k, lg in enumerate(sel):
        em = float(lg["mark"])
        ef = float(lg["fill"])
        fee_e = float(lg.get("fee", 0.0))
        slip_e = float(lg.get("slip", 0.0))
        qlg = int(lg.get("qty", qty))
        ls_lg = str(lg.get("long_short", long_short))
        exp_lg = str(lg.get("expiry", exp))
        exp_ts_lg = int(lg.get("exp_ts", exp_ts))
        t_e_lg = t_years(entry_ts, exp_ts_lg)
        t_x_lg = t_years(exit_ts, exp_ts_lg)
        if xi is None:
            xm = float("nan")
            xf = float("nan")
            fee_x = 0.0
            slip_x = 0.0
        elif hedge and "pxs" in path:
            xm = float(path["pxs"][k][xi])
            if ls_lg == "short" and int(path.get("short_exp_ts") or 0) and exit_ts >= int(path["short_exp_ts"]):
                xf = xm
                fee_x = fee_gst_qty(xm, spot_x, qlg) if xm > 0 else 0.0
                slip_x = 0.0
            elif ls_lg == "short":
                xf, _ = s018.buy_fill(xm, dte)
                fee_x = fee_gst_qty(xm, spot_x if spot_x else 1.0, qlg)
                slip_x = (xf - xm) * qlg * OPTIONS_CONTRACT_VALUE
            elif reason == "EXPIRY":
                xf = intrinsic(bool(lg["is_call"]), float(lg["strike"]), spot_x)
                xm = xf
                fee_x = fee_gst_qty(xf, spot_x, qlg) if xf > 0 else 0.0
                slip_x = 0.0
            else:
                xf, _ = s018.sell_fill(xm, dte)
                fee_x = fee_gst_qty(xm, spot_x if spot_x else 1.0, qlg)
                slip_x = (xm - xf) * qlg * OPTIONS_CONTRACT_VALUE
        elif reason == "EXPIRY":
            xm = intrinsic(bool(lg["is_call"]), float(lg["strike"]), spot_x)
            xf = xm
            fee_x = s018.fee_gst(xm, spot_x) if xm > 0 else 0.0
            slip_x = 0.0
        else:
            xm = float(path[f"px{k}"][xi])
            xf, _ = s018.sell_fill(xm, dte)
            fee_x = s018.fee_gst(xm, spot_x if spot_x else 1.0)
            slip_x = (xm - xf) * qty * OPTIONS_CONTRACT_VALUE
        ge = greeks_from_mark(em, spot_e, float(lg["strike"]), t_e_lg, bool(lg["is_call"]))
        gx = greeks_from_mark(xm, spot_x, float(lg["strike"]), t_x_lg, bool(lg["is_call"]))
        d_ent = float(lg.get("delta", ge["delta"]))
        legs_out.append(
            {
                "symbol": str(lg.get("symbol", "")),
                "strike": float(lg["strike"]),
                "type": "call" if bool(lg["is_call"]) else "put",
                "expiry": exp_lg,
                "qty": qlg,
                "long_short": ls_lg,
                "entry_mark": em,
                "entry_fill": ef,
                "exit_mark": xm,
                "exit_fill": xf,
                "fee_entry": fee_e,
                "fee_exit": fee_x,
                "slip_entry": slip_e,
                "slip_exit": slip_x,
                "iv_entry": ge["iv"],
                "delta_entry": d_ent,
                "gamma_entry": ge["gamma"],
                "theta_entry": ge["theta"],
                "vega_entry": ge["vega"],
                "iv_exit": gx["iv"],
                "delta_exit": gx["delta"],
                "gamma_exit": gx["gamma"],
                "theta_exit": gx["theta"],
                "vega_exit": gx["vega"],
            }
        )
    snaps: list[dict[str, Any]] = []
    for ts_i, kind in _snap_times(entry_ts, exit_ts, int(r.get("mfe_ts") or 0), int(r.get("mae_ts") or 0)):
        if ts_i == entry_ts:
            sp = spot_e
            marks = [float(lg["mark"]) for lg in sel]
            pnl = float("nan")
            long_pnl_m = 0.0
            if hedge:
                pnl = 0.0
                for lg in sel:
                    qlg = int(lg.get("qty", qty))
                    gp = (float(lg["mark"]) - float(lg["fill"])) * qlg * OPTIONS_CONTRACT_VALUE
                    if str(lg.get("long_short")) == "short":
                        pnl -= gp
                    else:
                        pnl += gp
                        long_pnl_m += gp
            else:
                pnl = 0.0
                for lg in sel:
                    pnl += (float(lg["mark"]) - float(lg["fill"])) * qty * OPTIONS_CONTRACT_VALUE
                long_pnl_m = pnl
        else:
            pi = _path_i_at(path, ts_i)
            if pi is None or not bool(path["ok"][pi]):
                continue
            sp = float(path["spot"][pi]) or float(r.get("spot_exit", 0.0))
            if hedge and "pxs" in path:
                marks = [float(path["pxs"][k][pi]) for k in range(len(sel))]
                pnl = float(path["pnl"][pi])
                long_pnl_m = float(path["pnl_long"][pi])
            else:
                marks = [float(path[f"px{k}"][pi]) for k in range(len(sel))]
                pnl = gp_at(path, pi, nlegs)
                long_pnl_m = pnl
        t_yr = t_years(ts_i, exp_ts)
        bd = bg = bt = bv = 0.0
        ld = lgk = lt = lv = 0.0
        row: dict[str, Any] = {
            "ts_ist": s018.ist_str(ts_i),
            "kind": kind,
            "spot": sp,
            "leg1_mark": "", "leg1_iv": "",
            "leg2_mark": "", "leg2_iv": "",
            "leg3_mark": "", "leg3_iv": "",
        }
        nfin = 0
        nlong_g = 0
        for k, lg in enumerate(sel):
            mk = float(marks[k]) if k < len(marks) else float("nan")
            exp_ts_lg = int(lg.get("exp_ts", exp_ts))
            g = greeks_from_mark(
                mk, sp, float(lg["strike"]), t_years(ts_i, exp_ts_lg), bool(lg["is_call"])
            )
            if k < 3:
                row[f"leg{k+1}_mark"] = mk
                row[f"leg{k+1}_iv"] = g["iv"]
            ls_lg = str(lg.get("long_short", long_short))
            psign = 1.0 if ls_lg == "long" else -1.0
            if (not hedge):
                psign = pos
            if np.isfinite(g["delta"]):
                bd += psign * float(g["delta"])
                bg += psign * float(g["gamma"])
                bt += psign * float(g["theta"])
                bv += psign * float(g["vega"])
                nfin += 1
                if ls_lg == "long" or not hedge:
                    ld += psign * float(g["delta"])
                    lgk += psign * float(g["gamma"])
                    lt += psign * float(g["theta"])
                    lv += psign * float(g["vega"])
                    nlong_g += 1
        row["basket_delta"] = bd if nfin else float("nan")
        row["basket_gamma"] = bg if nfin else float("nan")
        row["basket_theta"] = bt if nfin else float("nan")
        row["basket_vega"] = bv if nfin else float("nan")
        row["basket_mark_pnl"] = pnl
        row["long_delta"] = ld if nlong_g else float("nan")
        row["long_gamma"] = lgk if nlong_g else float("nan")
        row["long_theta"] = lt if nlong_g else float("nan")
        row["long_vega"] = lv if nlong_g else float("nan")
        row["long_mark_pnl"] = long_pnl_m
        snaps.append(row)
    archive.add_trade(trade, legs_out, snaps)


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
    strangle_exit: bool = False,
    do_c2: bool = True,
    do_c3: bool = True,
    c2_seeds: tuple[int, ...] | None = None,
    skip_done: bool = True,
    write_ckpt: bool = True,
    archive: RunArchive | None = None,
    hedge: bool = False,
    hedge_e: int = -1,
    hedge_off: float = 0.0,
    hedge_w: float = 0.0,
    hedge_q: float = 0.0,
    long_kind: str = "",
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
        "strangle_exit": strangle_exit,
    }
    if int(dte_mode) >= 2 and not hedge:
        cov_x, cov_y = dte2_coverage(store, spot_c, plan_all, False)
        print(f"2DTE coverage {cov_x}/{cov_y}", flush=True)
    for tgt, slv in cells:
        key = cell_key(
            month, tf, variant, tgt, slv, band, band_mode,
            legs=nlegs, dte=dte_mode, trail_arm=trail_arm,
            trail_give=trail_give, trail_cap=trail_cap,
            arm_id=arm_id, basket=basket,
        )
        if skip_done and key in done:
            print(f"done SKIP {key}", flush=True)
            continue
        print(
            f"arm {arm_i}/{arm_n} {key} RSS={s020.rss_mb() or 0:.0f}MB "
            f"elapsed={time.perf_counter()-t_all:.0f}s",
            flush=True,
        )
        cfg_now = {
            "month": month, "tf": tf, "variant": variant, "key": key,
            "band": band, "band_mode": band_mode, "arm_id": arm_id, "dte": dte_mode,
        }
        ls = "long" if (strangle_exit or hedge) else "short"
        on_tr = None
        if archive is not None:
            on_tr = lambda r, p, _c=cfg_now, _n=nlegs, _ls=ls: emit_archive_trade(
                archive, _c, r, p, _n, _ls
            )
        if hedge:
            rows, n_stale = simulate_hedge_plan(
                store, spot_c, plan_all, tag_p,
                start_ts, cutoff, win_from, win_to, extra_by, band, band_mode,
                long_kind, hedge_e, hedge_off, hedge_w, hedge_q,
                label=f"{tf} {variant} {arm_id}",
                on_trade=on_tr,
            )
        else:
            rows, n_stale = simulate_plan(
                store, spot_c, plan_all, float(tgt), float(slv), tag_p, False,
                start_ts, cutoff, win_from, win_to, time_stop,
                label=f"{tf} {variant} T={tgt}|SL={slv}",
                extra_by=extra_by,
                on_trade=on_tr,
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
                    if hedge:
                        rr, _ = simulate_hedge_plan(
                            store, spot_c, forced, f"C2{tag_p}",
                            start_ts, cutoff, win_from, win_to, None, band, band_mode,
                            long_kind, hedge_e, hedge_off, hedge_w, hedge_q,
                            label=f"C2 {key} seed={si}",
                        )
                    else:
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
        if archive is not None:
            c2v = stt.get("c2", float("nan"))
            c3v = stt.get("c3", float("nan"))
            archive.add_config_result(
                {
                    "month": month, "tf": tf, "variant": variant,
                    "band": band, "band_mode": band_mode, "arm": arm_id,
                    "n_legs": nlegs, "dte": dte_mode, "tgt": tgt, "sl": slv,
                    "n" : stt.get("n", 0), "mean": stt.get("mean"),
                    "gross": stt.get("gross"), "brokerage": stt.get("fee"),
                    "slippage": stt.get("slip"), "win": stt.get("win"),
                    "C2": "-" if not np.isfinite(c2v) else c2v,
                    "C3": "-" if not np.isfinite(c3v) else c3v,
                    "n_sig": stt.get("n_sig"), "n_stale": n_stale,
                    "avg_net_delta": stt.get("avg_net_delta"), "key": key,
                }
            )
        if write_ckpt:
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
        "STRANGLE-GRID (prereg: SL 50%P, trail 30/20, cap 100%P):",
        f"{'sig':<16} {'arm':<4} {'n':>5} {'mean':>9} {'gross':>9} "
        f"{'broker':>8} {'slip':>8} {'win%':>7} {'C2':>9} {'C3':>9}",
    ]
    extra: list[str] = ["P / brokerage/trade / slippage/trade:"]
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
            extra.append(
                f"  {sig} {str(arm['id'])}  "
                f"P={s020._fnum(s.get('avg_p', float('nan')), 2)}  "
                f"brokerage/trade={s020._fnum(s.get('fee', float('nan')), 2)}  "
                f"slippage/trade={s020._fnum(s.get('slip', float('nan')), 2)}"
            )
    lines.extend(extra)
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


def dump_trades_work() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in tf_band_work():
        rec["do_c2"] = False
        rec["do_c3"] = False
        rec["c2_seeds"] = ()
        out.append(rec)
    for tf, var, band, mode in EXIT_GRID_SIGS:
        for arm in EXIT_ARMS:
            rec = {"tf": tf, "variant": var, "band": band, "band_mode": mode}
            rec.update(work_fields_from_arm(arm))
            rec["do_c2"] = False
            rec["do_c3"] = False
            rec["c2_seeds"] = ()
            out.append(rec)
    return out


def hedge_grid_work() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tf, var, band, mode in EXIT_GRID_SIGS:
        for lk in HEDGE_LONG:
            nlong = 2 if lk == "L2" else 3
            variants: list[tuple[int, float, float, float, bool]] = [(-1, 0.0, 0.0, 0.0, True)]
            for e in HEDGE_E:
                for off in HEDGE_OFF:
                    for wing in HEDGE_W:
                        for q in HEDGE_Q:
                            do_c2 = int(e) == 0 and int(off) == 600 and int(wing) == 1000 and abs(float(q) - 0.5) < 1e-9
                            variants.append((int(e), float(off), float(wing), float(q), do_c2))
            for e, off, wing, q, do_c2 in variants:
                if e < 0:
                    aid = f"{lk}_REF"
                    bsk = f"hdg_{lk}_REF"
                else:
                    aid = f"{lk}_E{e}_o{int(off)}_W{int(wing)}_q{int(round(q*100))}"
                    bsk = f"hdg_{lk}_E{e}_o{int(off)}_W{int(wing)}_q{int(round(q*100))}"
                rec = {
                    "tf": tf, "variant": var, "band": band, "band_mode": mode,
                    "legs": nlong, "dte": 2,
                    "trail_arm": HEDGE_TRAIL_ARM, "trail_give": HEDGE_TRAIL_GIVE,
                    "trail_cap": HEDGE_TRAIL_CAP, "arm_id": aid, "basket": bsk,
                    "prem_pct": 0.0, "strangle_exit": False,
                    "hedge": True, "hedge_e": int(e), "hedge_off": float(off),
                    "hedge_w": float(wing), "hedge_q": float(q), "long_kind": lk,
                    "do_c2": bool(do_c2), "do_c3": False,
                    "c2_seeds": RANDOM_SEEDS_20 if do_c2 else (),
                }
                out.append(rec)
    return out


def print_hedge_leg_details(rows: list[dict[str, Any]], e_label: str) -> None:
    print(f"=== HEDGE LEG DETAIL E={e_label} ===", flush=True)
    if not rows:
        print("  (no trades)", flush=True)
        return
    r = rows[0]
    print(
        f"  {r.get('side')} entry={s018.ist_str(int(r['entry_ts']))} "
        f"spot={r.get('spot')} reason={r.get('reason')} net={float(r.get('net', float('nan'))):.2f}",
        flush=True,
    )
    for lg in list(r.get("legs") or []):
        print(
            f"    {lg.get('long_short')} {lg.get('role')} {lg.get('symbol')} "
            f"k={lg.get('strike')} exp={lg.get('expiry')} qty={lg.get('qty')} "
            f"mark={float(lg.get('mark', float('nan'))):.4f} fill={float(lg.get('fill', float('nan'))):.4f} "
            f"fee={float(lg.get('fee', float('nan'))):.4f}",
            flush=True,
        )


def _dump_leg_fields(lg: dict[str, Any] | None, exit_px: float) -> dict[str, Any]:
    if lg is None:
        return {
            "symbol": "",
            "strike": "",
            "type": "",
            "entry_fill": "",
            "exit_px": "",
            "delta": "",
        }
    return {
        "symbol": str(lg.get("symbol", "")),
        "strike": float(lg["strike"]),
        "type": "call" if bool(lg["is_call"]) else "put",
        "entry_fill": float(lg["fill"]),
        "exit_px": exit_px if np.isfinite(exit_px) else "",
        "delta": float(lg.get("delta", float("nan"))),
    }


def dump_trade_row(month: str, w: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    nlegs = int(w["legs"])
    sel = list(r.get("legs") or [])[:nlegs]
    pxs = list(r.get("exit_pxs") or [])
    row: dict[str, Any] = {
        "month": month,
        "tf": w["tf"],
        "variant": w["variant"],
        "band": int(w["band"]),
        "band_mode": str(w["band_mode"]),
        "arm": str(w.get("arm_id", "")),
        "n_legs": nlegs,
        "dte": int(w["dte"]),
        "side": r["side"],
        "entry_ist": s018.ist_str(int(r["entry_ts"])),
        "exit_ist": s018.ist_str(int(r["exit_ts"])),
        "exit_reason": r.get("exit_reason", r.get("reason")),
        "hold_hrs": r.get("hold_hrs"),
        "hrs_to_exp": r.get("hrs_to_exp"),
        "expiry": r.get("exp"),
        "spot_entry": r.get("spot"),
        "spot_exit": r.get("spot_exit"),
        "line_level": r.get("level"),
        "vwap_at_entry": r.get("vwap"),
        "vwap_dist": r.get("vwap_dist"),
        "entry_iv": r.get("entry_iv"),
        "basket_net_delta": r.get("net_delta"),
        "gross": r.get("gross"),
        "brokerage": r.get("fees"),
        "slippage": r.get("slip"),
        "net": r.get("net"),
        "mfe": r.get("mfe"),
        "mae": r.get("mae"),
    }
    for i in range(3):
        lg = sel[i] if i < len(sel) else None
        px = float(pxs[i]) if i < len(pxs) else float("nan")
        fld = _dump_leg_fields(lg, px)
        pfx = f"leg{i + 1}_"
        row[f"{pfx}symbol"] = fld["symbol"]
        row[f"{pfx}strike"] = fld["strike"]
        row[f"{pfx}type"] = fld["type"]
        row[f"{pfx}entry_fill"] = fld["entry_fill"]
        row[f"{pfx}exit_px"] = fld["exit_px"]
        row[f"{pfx}delta"] = fld["delta"]
    return row


def write_dump_trades_csv(
    path: Path, month: str, items: list[tuple[dict[str, Any], dict[str, Any]]]
) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        wcsv = csv.DictWriter(f, fieldnames=list(DUMP_TRADE_COLS))
        wcsv.writeheader()
        for w, r in items:
            wcsv.writerow(dump_trade_row(month, w, r))


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
    ap.add_argument("--dump-trades", action="store_true")
    ap.add_argument("--hedge-grid", action="store_true")
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--purge-cache", action="store_true")
    ap.add_argument("--prereg-note", default="")
    ap.add_argument("--cache-gb", type=float, default=1.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("Disable PC sleep")
    load_slip_table()
    reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dump_trades = bool(args.dump_trades)
    n_cache0 = path_cache_n()
    print(f"path-cache files={n_cache0}", flush=True)
    if args.purge_cache:
        print("This deletes S020 DEV path-cache npz. Type YES to confirm:", flush=True)
        ans = sys.stdin.readline().strip()
        if ans != "YES":
            print("purge-cache aborted", flush=True)
            sys.exit(1)
        drop_dev_cache()
    if args.fresh:
        drop_dev_fresh()
    print(f"path-cache files after flags={path_cache_n()}", flush=True)

    month = str(args.month)
    win_from, win_to = month_bounds(month)
    max_days = int(args.max_days or 0)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    if max_days:
        cutoff = start_ts + max_days * 86400
        win_to = min(win_to, ist_date(cutoff - 1))
    if (not args.fresh) and (not dump_trades):
        preexisting = s020.load_ckpt(CKPT)
        if preexisting and not ckpt_window_ok(preexisting, month, max_days):
            print("checkpoint window mismatch -> use --fresh", flush=True)
            sys.exit(1)

    mode_n = archive_mode_name(args)
    arch_folder: Path | None = None
    if (not args.fresh) and (not dump_trades) and ARCHIVE_PTR.exists():
        try:
            ptr = json.loads(ARCHIVE_PTR.read_text(encoding="utf-8"))
            if (
                str(ptr.get("month", "")) == month
                and int(ptr.get("max_days", -1)) == int(max_days)
                and str(ptr.get("mode", "")) == mode_n
            ):
                cand = Path(str(ptr.get("folder", "")))
                if cand.is_dir():
                    arch_folder = cand
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            arch_folder = None
    archive = RunArchive(
        "s020",
        mode_n,
        args,
        month=month,
        prereg_note=str(args.prereg_note or ""),
        folder=arch_folder,
    )
    ARCHIVE_PTR.write_text(
        json.dumps(
            {
                "month": month,
                "max_days": int(max_days),
                "mode": mode_n,
                "folder": str(archive.folder),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"archive folder={archive.folder}", flush=True)
    print_expiry_check()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    exit_grid = bool(args.exit_grid)
    strangle_grid = bool(args.strangle_grid)
    tf_band = bool(args.tf_band_grid)
    hedge_grid = bool(args.hedge_grid)
    need_d2 = dump_trades or exit_grid or hedge_grid or int(args.dte) >= 2
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
    done: dict[str, dict[str, Any]] = {} if (args.fresh or dump_trades) else s020.load_ckpt(CKPT)
    cell_rows: dict[str, list[dict[str, Any]]] = {}
    dump_items: list[tuple[dict[str, Any], dict[str, Any]]] = []

    grid = bool(args.band_grid)
    jobs: list[tuple[str, str, int, str]]
    work: list[dict[str, Any]] = []
    if dump_trades:
        band0, mode0 = 0, "near"
        work = dump_trades_work()
        tfs = sorted({str(w["tf"]) for w in work})
        variants = sorted({str(w["variant"]) for w in work})
        jobs = [(str(w["tf"]), str(w["variant"]), int(w["band"]), str(w["band_mode"])) for w in work]
        print(f"DUMP-TRADES combos={len(work)} (expect {DUMP_N_COMBOS}); C2/C3 off; ckpt/cache untouched", flush=True)
    elif hedge_grid:
        band0, mode0 = 0, "near"
        work = hedge_grid_work()
        tfs = sorted({str(w["tf"]) for w in work})
        variants = sorted({str(w["variant"]) for w in work})
        jobs = list(EXIT_GRID_SIGS)
        print(f"HEDGE-GRID combos={len(work)} (expect {HEDGE_N_COMBOS})", flush=True)
    elif tf_band:
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
                    "basket": "std", "prem_pct": 0.0, "strangle_exit": False,
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
                    "basket": "std", "prem_pct": 0.0, "strangle_exit": False,
                }
            )
    arm_n = max(1, len(work))
    arm_i = 0
    t_all = time.perf_counter()

    smoke_sigs: list[dict[str, Any]] = []
    sig_cache: dict[tuple[str, str], tuple[list[DevLine], list[dict[str, Any]], int]] = {}
    if hedge_grid:
        for tf0, var0, band0s, mode0s in EXIT_GRID_SIGS:
            tf_sec = LINE_TF[tf0]
            tts, to_, th, tl, tc, tv = resample_tf(ts, o, h, l, c, vol, tf_sec)
            vwap_tf = s020.session_vwap(tts, th, tl, tc, tv)
            raw_lines, _, _ = s020.detect_swings(tts, to_, th, tl, tc, vwap_tf)
            dlines = tf_lines_to_dev(tts, raw_lines, tf_sec)
            raw_sigs, both_skip = collect_signals(ts, o, h, l, c, dlines, var0)
            win_sigs = [
                s
                for s in raw_sigs
                if start_ts <= int(s["ts"]) < cutoff
                and s020.in_window(int(s["ts"]), win_from, win_to)
            ]
            attach_vwap_dist(win_sigs, vwap_by_ts)
            sig_cache[(tf0, var0)] = (dlines, win_sigs, both_skip)
        plans_h: list[tuple[int, str]] = []
        seen_p: set[tuple[int, str]] = set()
        for tf0, var0, band0s, mode0s in EXIT_GRID_SIGS:
            _, win_sigs, _ = sig_cache[(tf0, var0)]
            for s in win_sigs:
                if keep_signal(s, int(band0s), str(mode0s)):
                    kp = (int(s["ts"]), str(s["side"]))
                    if kp not in seen_p:
                        seen_p.add(kp)
                        plans_h.append(kp)
        for e in (0, 1):
            cx, cy = short_hedge_coverage(store, spot_c, plans_h, e)
            print(f"short chain coverage {'0DTE' if e == 0 else '1DTE'} {cx}/{cy}", flush=True)
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
        strangle_exit = bool(w.get("strangle_exit", False))
        hedge = bool(w.get("hedge", False))
        hedge_e = int(w.get("hedge_e", -1))
        hedge_off = float(w.get("hedge_off", 0.0))
        hedge_w = float(w.get("hedge_w", 0.0))
        hedge_q = float(w.get("hedge_q", 0.0))
        long_kind = str(w.get("long_kind", ""))
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
        if (not tf_band) and (not hedge_grid):
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
        if grid or exit_grid or strangle_grid or tf_band or dump_trades or hedge_grid:
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
            basket=basket, prem_pct=prem_pct, strangle_exit=strangle_exit,
            do_c2=do_c2, do_c3=do_c3,
            c2_seeds=tuple(c2_seeds) if c2_seeds is not None else None,
            skip_done=not dump_trades,
            write_ckpt=not dump_trades,
            archive=archive,
            hedge=hedge, hedge_e=hedge_e, hedge_off=hedge_off,
            hedge_w=hedge_w, hedge_q=hedge_q, long_kind=long_kind,
        )
        pk = cell_key(
            month, tf, variant, PRIMARY_T, PRIMARY_SL, band, band_mode,
            legs=nlegs, dte=dte_mode, trail_arm=trail_arm,
            trail_give=trail_give, trail_cap=trail_cap,
            arm_id=arm_id, basket=basket,
        )
        st = done.get(pk, {})
        if hedge_grid:
            e_s = "ref" if hedge_e < 0 else str(hedge_e)
            off_s = "-" if hedge_e < 0 else str(int(hedge_off))
            w_s = "-" if hedge_e < 0 else str(int(hedge_w))
            q_s = "-" if hedge_e < 0 else str(hedge_q)
            print(
                f"[{arm_i}/{arm_n}] {tf}/{variant} {long_kind} E={e_s} off={off_s} "
                f"W={w_s} q={q_s} n={int(st.get('n', 0))} "
                f"mean={s020._fnum(st.get('mean', float('nan')), 2)} "
                f"elapsed={time.perf_counter()-t_all:.0f}s",
                flush=True,
            )
            if max_days and hedge_e in (0, 1) and int(st.get("n", 0)) > 0:
                shown_e = getattr(print_hedge_leg_details, "_shown", set())
                if hedge_e not in shown_e:
                    print_hedge_leg_details(cell_rows.get(pk, []), f"{hedge_e}DTE")
                    shown_e.add(hedge_e)
                    print_hedge_leg_details._shown = shown_e  # type: ignore[attr-defined]
        elif dump_trades:
            for tr in cell_rows.get(pk, []):
                dump_items.append((w, tr))
            if arm_i % 10 == 0 or arm_i == arm_n:
                print(
                    f"[{arm_i}/{arm_n}] elapsed={time.perf_counter()-t_all:.0f}s",
                    flush=True,
                )
        elif tf_band:
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
            if grid or exit_grid or strangle_grid or tf_band or dump_trades or hedge_grid
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
    elif hedge_grid:
        report.append(f"HEDGE-GRID combos={len(work)} (C2 on REF and E0/off600/W1000/q0.5 only)")
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
    dump_csv: Path | None = None
    if dump_trades:
        dump_csv = OUT_DIR / f"s020_trades_all_{month}_{stamp}.csv"
        write_dump_trades_csv(dump_csv, month, dump_items)
        print(f"DUMP-TRADES rows={len(dump_items)} wrote {dump_csv}", flush=True)
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
    if dump_csv is not None:
        print(f"wrote {dump_csv}")
    print(f"TOTAL elapsed={time.perf_counter()-t_all:.0f}s", flush=True)
    print(f"path-cache files end={path_cache_n()} (start={n_cache0})", flush=True)
    archive.finalize()
    store.close()


if __name__ == "__main__":
    main()
