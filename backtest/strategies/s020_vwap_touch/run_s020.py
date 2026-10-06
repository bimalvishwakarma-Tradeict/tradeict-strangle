#!/usr/bin/env python3
"""S020 VWAP swing-level touch basket.

python backtest\\strategies\\s020_vwap_touch\\run_s020.py --max-days 5
python backtest\\strategies\\s020_vwap_touch\\run_s020.py --holdout
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
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
from backtest.harness.data import MarksStore, load_symbol_series  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.slippage_model import load_slip_table  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    intrinsic,
    ist_date,
    t_years,
)
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402

logger = logging.getLogger("s020")

OUT_DIR = Path("backtest/strategies/s020_vwap_touch/runs")
CACHE_DIR = OUT_DIR / "s020_pathcache"
CKPT = OUT_DIR / "s020_ckpt.jsonl"
PATH_VER = "v1"

TRAIN_FROM = date(2024, 9, 1)
TRAIN_TO = date(2025, 12, 31)
HOLD_FROM = date(2026, 1, 1)
HOLD_TO = date(2026, 9, 21)

TGTS = (100, 150, 200, 250, 300)
SLS = (100, 150, 200, 250, 300)
PRIMARY_T, PRIMARY_SL = 250, 250
RANDOM_SEEDS = tuple(range(20))
SRC_CODE = {"real": 1, "stale": 2, "settle": 3}
SRC_NAME = {1: "real", 2: "stale", 3: "settle"}


class GuardStore:
    def __init__(self, inner: MarksStore, forbid_year: int | None) -> None:
        self.inner = inner
        self.forbid_year = forbid_year

    def conn(self, d: date) -> Any:
        if self.forbid_year is not None and d.year >= self.forbid_year:
            return None
        return self.inner.conn(d)

    def close(self) -> None:
        self.inner.close()


@dataclass
class SwingLine:
    kind: str  # low | high
    level: float
    extreme: float
    cross_i: int
    active_from: int
    expire_i: int | None = None
    touches: list[int] = field(default_factory=list)


def rss_mb() -> float | None:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if ok:
            return float(counters.WorkingSetSize) / (1024.0 * 1024.0)
    except Exception:
        return None
    return None


def bars_1m_vol(spot: dict[int, Any]) -> tuple[np.ndarray, ...]:
    ts = np.array(sorted(spot), dtype=np.int64)
    o = np.array([spot[int(t)].open for t in ts], dtype=np.float64)
    h = np.array([spot[int(t)].high for t in ts], dtype=np.float64)
    l = np.array([spot[int(t)].low for t in ts], dtype=np.float64)
    c = np.array([spot[int(t)].close for t in ts], dtype=np.float64)
    v = np.array([spot[int(t)].volume for t in ts], dtype=np.float64)
    return ts, o, h, l, c, v


def session_vwap(ts: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, vol: np.ndarray) -> np.ndarray:
    n = len(ts)
    out = np.full(n, np.nan, dtype=np.float64)
    day = -1
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        d = int(ts[i]) // 86400
        if d != day:
            day = d
            cum_pv = 0.0
            cum_v = 0.0
        tp = (float(h[i]) + float(l[i]) + float(c[i])) / 3.0
        vv = float(vol[i])
        if vv > 0 and np.isfinite(tp):
            cum_pv += tp * vv
            cum_v += vv
        if cum_v > 0:
            out[i] = cum_pv / cum_v
    return out


def chop_of(
    hit_lines: list[SwingLine],
    side: str,
    i: int,
    cl: float,
    vw: float,
    n_act: int,
) -> dict[str, Any]:
    want = "low" if side == "long" else "high"
    cands = [ln for ln in hit_lines if ln.kind == want] or list(hit_lines)
    ln = max(cands, key=lambda x: len(x.touches))
    return {
        "touch_count_of_line": int(len(ln.touches)),
        "line_age_min": int(max(0, i - ln.active_from)),
        "abs_spot_vwap": abs(float(cl) - float(vw)),
        "n_active_lines": int(n_act),
    }


def detect_swings(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vwap: np.ndarray,
) -> tuple[list[SwingLine], list[dict[str, Any]], int]:
    n = len(ts)
    lines: list[SwingLine] = []
    low_run: list[int] | None = None
    high_run: list[int] | None = None
    both_skip = 0
    signals: list[dict[str, Any]] = []
    day = -1

    def expire_at(i: int) -> None:
        cl = float(c[i])
        for ln in lines:
            if ln.expire_i is not None or i < ln.active_from:
                continue
            if ln.kind == "low" and cl < ln.level:
                ln.expire_i = i
            elif ln.kind == "high" and cl > ln.level:
                ln.expire_i = i

    for i in range(n):
        d = int(ts[i]) // 86400
        if d != day:
            day = d
            low_run = None
            high_run = None
        vw = float(vwap[i])
        if not np.isfinite(vw):
            expire_at(i)
            continue
        cl = float(c[i])
        hi = float(h[i])
        lo = float(l[i])
        long_hit = False
        short_hit = False
        hit_lines: list[SwingLine] = []
        n_act = 0
        for ln in lines:
            if ln.expire_i is not None or i < ln.active_from:
                continue
            n_act += 1
            if ln.kind == "low" and lo <= ln.level:
                long_hit = True
                ln.touches.append(i)
                hit_lines.append(ln)
            if ln.kind == "high" and hi >= ln.level:
                short_hit = True
                ln.touches.append(i)
                hit_lines.append(ln)
        if long_hit and short_hit:
            both_skip += 1
        elif long_hit:
            signals.append(
                {
                    "i": i,
                    "ts": int(ts[i]),
                    "side": "long",
                    "o": float(o[i]),
                    "h": hi,
                    "l": lo,
                    "c": cl,
                    "lines": hit_lines,
                    **chop_of(hit_lines, "long", i, cl, vw, n_act),
                }
            )
        elif short_hit:
            signals.append(
                {
                    "i": i,
                    "ts": int(ts[i]),
                    "side": "short",
                    "o": float(o[i]),
                    "h": hi,
                    "l": lo,
                    "c": cl,
                    "lines": hit_lines,
                    **chop_of(hit_lines, "short", i, cl, vw, n_act),
                }
            )
        expire_at(i)
        if cl < vw:
            if high_run:
                extreme = max(float(h[j]) for j in high_run)
                lines.append(
                    SwingLine(
                        kind="high",
                        level=extreme,
                        extreme=extreme,
                        cross_i=i,
                        active_from=i + 1,
                    )
                )
                high_run = None
            if low_run is None:
                low_run = [i]
            else:
                low_run.append(i)
        elif cl > vw:
            if low_run:
                extreme = min(float(l[j]) for j in low_run)
                lines.append(
                    SwingLine(
                        kind="low",
                        level=extreme,
                        extreme=extreme,
                        cross_i=i,
                        active_from=i + 1,
                    )
                )
                low_run = None
            if high_run is None:
                high_run = [i]
            else:
                high_run.append(i)
        else:
            low_run = None
            high_run = None
    return lines, signals, both_skip


def expiry_1dte(t: int) -> date:
    return ist_date(int(t)) + timedelta(days=1)


def pick_touch_basket(
    store: Any, side: str, t: int, spot: float, flipped: bool
) -> list[s018.Leg] | None:
    want = side
    if flipped:
        want = "short" if side == "long" else "long"
    exp = expiry_1dte(t)
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


def cache_file(entry_ts: int, side: str, exp: date, tag: str) -> Path:
    return CACHE_DIR / f"{entry_ts}_{side}_{exp.isoformat()}_{tag}_{PATH_VER}.npz"


def save_path(p: Path, obj: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"exp": obj["exp"], "legs": obj["legs"]})
    np.savez_compressed(
        p,
        ts=obj["ts"],
        ok=obj["ok"],
        pnl=obj["pnl"],
        spot=obj["spot"],
        px0=obj["px0"],
        px1=obj["px1"],
        px2=obj["px2"],
        src0=obj["src0"],
        src1=obj["src1"],
        src2=obj["src2"],
        entry_ts=np.array([obj["entry_ts"]], dtype=np.int64),
        exp_ts=np.array([obj["exp_ts"]], dtype=np.int64),
        dte=np.array([obj["dte"]], dtype=np.int32),
        meta_json=np.array([meta.encode("utf-8")]),
    )


def load_path(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    z = np.load(p, allow_pickle=False)
    raw = z["meta_json"][0]
    if isinstance(raw, (bytes, np.bytes_)):
        meta = json.loads(bytes(raw).decode("utf-8"))
    else:
        meta = json.loads(str(raw))
    return {
        "entry_ts": int(z["entry_ts"][0]),
        "exp": str(meta["exp"]),
        "exp_ts": int(z["exp_ts"][0]),
        "dte": int(z["dte"][0]),
        "legs": meta["legs"],
        "ts": z["ts"],
        "ok": z["ok"],
        "pnl": z["pnl"],
        "spot": z["spot"],
        "px0": z["px0"],
        "px1": z["px1"],
        "px2": z["px2"],
        "src0": z["src0"],
        "src1": z["src1"],
        "src2": z["src2"],
    }


def build_path(
    store: Any,
    spot_c: dict[int, float],
    legs: list[s018.Leg],
    entry_ts: int,
    exp: date,
) -> dict[str, Any] | None:
    exp_ts = s018.expiry_unix(exp)
    series = [load_symbol_series(store, lg.symbol, entry_ts, exp_ts) for lg in legs]
    ts = np.arange(int(entry_ts) + 60, int(exp_ts) + 1, 60, dtype=np.int64)
    n = int(ts.size)
    if n <= 0:
        return None
    ok = np.zeros(n, dtype=np.bool_)
    pnl = np.full(n, np.nan, dtype=np.float32)
    spot = np.zeros(n, dtype=np.float32)
    px = [np.zeros(n, dtype=np.float32) for _ in range(3)]
    src = [np.zeros(n, dtype=np.int8) for _ in range(3)]
    nleg = len(legs)
    for i in range(n):
        t = int(ts[i])
        spot[i] = float(spot_c.get(t, 0.0))
        qs = [s018.series_le(ser, t) for ser in series]
        if any(q is None for q in qs):
            continue
        ok[i] = True
        pnl[i] = np.float32(s018.mark_pnl(legs, qs))  # type: ignore[arg-type]
        for k in range(nleg):
            px[k][i] = np.float32(qs[k].px)  # type: ignore[union-attr]
            src[k][i] = np.int8(SRC_CODE.get(qs[k].src, 0))  # type: ignore[union-attr]
    dte = max(0, (exp - ist_date(entry_ts)).days)
    el = []
    for lg in legs:
        slip = (lg.fill - lg.mark) * s018.QTY * OPTIONS_CONTRACT_VALUE
        fee = s018.fee_gst(lg.mark, float(spot_c.get(lg.ts, 0.0)))
        el.append(
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
            }
        )
    return {
        "entry_ts": int(entry_ts),
        "exp": exp.isoformat(),
        "exp_ts": exp_ts,
        "dte": dte,
        "legs": el,
        "ts": ts,
        "ok": ok,
        "pnl": pnl,
        "spot": spot,
        "px0": px[0],
        "px1": px[1],
        "px2": px[2],
        "src0": src[0],
        "src1": src[1],
        "src2": src[2],
    }


def _exit_idx(
    path: dict[str, Any],
    i: int,
    reason: str,
    spot_c: dict[int, float],
) -> dict[str, Any]:
    t = int(path["ts"][i])
    legs = list(path["legs"])
    dte = int(path["dte"])
    idx = float(path["spot"][i]) or float(spot_c.get(t, 0.0))
    pxs = [float(path["px0"][i]), float(path["px1"][i]), float(path["px2"][i])]
    srcs_m = [int(path["src0"][i]), int(path["src1"][i]), int(path["src2"][i])]
    gross = 0.0
    fees = sum(float(lg["fee"]) for lg in legs)
    slip = sum(float(lg["slip"]) for lg in legs)
    srcs = [str(lg["src"]) for lg in legs]
    for lg, mark, src_i in zip(legs, pxs, srcs_m):
        xf, _ = s018.sell_fill(mark, dte)
        gross += (xf - float(lg["fill"])) * s018.QTY * OPTIONS_CONTRACT_VALUE
        fees += s018.fee_gst(mark, idx if idx else 1.0)
        slip += (mark - xf) * s018.QTY * OPTIONS_CONTRACT_VALUE
        srcs.append(SRC_NAME.get(src_i, "real"))
    return {
        "exit_ts": t,
        "reason": reason,
        "gross": gross,
        "fees": fees,
        "slip": slip,
        "net": gross - fees,
        "srcs": srcs,
    }


def scan_path(
    path: dict[str, Any], tgt: float, sl: float, spot_c: dict[int, float]
) -> dict[str, Any] | None:
    ts = path["ts"]
    ok = path["ok"]
    pnl_a = path["pnl"]
    exp_ts = int(path["exp_ts"])
    n = int(ts.size)
    for i in range(n):
        t = int(ts[i])
        if bool(ok[i]) and np.isfinite(pnl_a[i]):
            gp = float(pnl_a[i])
            if gp >= tgt:
                return _exit_idx(path, i, "TARGET", spot_c)
            if gp <= -sl:
                return _exit_idx(path, i, "SL", spot_c)
        if t == exp_ts:
            sp = float(path["spot"][i]) if float(path["spot"][i]) > 0 else float(spot_c.get(t, 0.0))
            if sp <= 0:
                return None
            legs = list(path["legs"])
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
    return None


def in_window(ts: int, a: date, b: date) -> bool:
    d = datetime.fromtimestamp(int(ts), tz=s018.UTC).astimezone(s018.IST).date()
    return a <= d <= b


def max_dd(nets: list[float]) -> float:
    eq = peak = 0.0
    dd = 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def top5_share(nets: list[float]) -> float:
    wins = sorted([n for n in nets if n > 0], reverse=True)
    tot = float(sum(wins))
    if tot <= 0:
        return float("nan")
    return 100.0 * float(sum(wins[:5])) / tot


def stats_of(rows: list[dict[str, Any]], n_sig: int, n_days: float) -> dict[str, Any]:
    n = len(rows)
    nets = [float(r["net"]) for r in rows]
    reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        reasons[str(r["reason"])] += 1
    holds = [float(r.get("hold_hrs", float("nan"))) for r in rows]
    holds = [x for x in holds if np.isfinite(x)]
    dlts = [float(r["entry_dlt"]) for r in rows if np.isfinite(r.get("entry_dlt", float("nan")))]
    tvs = [float(r["entry_tv"]) for r in rows if np.isfinite(r.get("entry_tv", float("nan")))]
    return {
        "n": n,
        "sig_day": float(n_sig) / n_days if n_days > 0 else float("nan"),
        "win": 100.0 * sum(1 for x in nets if x > 0) / n if n else float("nan"),
        "mean": float(np.mean(nets)) if nets else float("nan"),
        "med": float(np.median(nets)) if nets else float("nan"),
        "gross": float(np.mean([r["gross"] for r in rows])) if rows else float("nan"),
        "fee": float(np.mean([r["fees"] for r in rows])) if rows else float("nan"),
        "slip": float(np.mean([r["slip"] for r in rows])) if rows else float("nan"),
        "worst": min(nets) if nets else float("nan"),
        "maxdd": max_dd(nets),
        "top5": top5_share(nets),
        "exits": dict(reasons),
        "hold": float(np.mean(holds)) if holds else float("nan"),
        "avg_dlt": float(np.mean(dlts)) if dlts else float("nan"),
        "avg_tv": float(np.mean(tvs)) if tvs else float("nan"),
    }


def stats_ww(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    nets = [float(r["net"]) for r in rows]
    return {
        "n": n,
        "win": 100.0 * sum(1 for x in nets if x > 0) / n if n else float("nan"),
        "mean": float(np.mean(nets)) if nets else float("nan"),
        "med": float(np.median(nets)) if nets else float("nan"),
        "gross": float(np.mean([float(r["gross"]) for r in rows])) if rows else float("nan"),
        "worst": min(nets) if nets else float("nan"),
        "maxdd": max_dd(nets),
    }


def is_weekend_ist(entry_ts: int) -> bool:
    dt = datetime.fromtimestamp(int(entry_ts), tz=s018.UTC).astimezone(s018.IST)
    return int(dt.weekday()) >= 5


def split_wd_we(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    wd = [r for r in rows if not is_weekend_ist(int(r["entry_ts"]))]
    we = [r for r in rows if is_weekend_ist(int(r["entry_ts"]))]
    return wd, we


def _fnum(v: Any, nd: int) -> str:
    if isinstance(v, float) and not np.isfinite(v):
        return "nan"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def fmt_stats(s: dict[str, Any]) -> str:
    return (
        f"n={s['n']} sig/day={_fnum(s.get('sig_day', float('nan')), 3)} "
        f"win%={_fnum(s.get('win', float('nan')), 1)} mean={_fnum(s.get('mean', float('nan')), 2)} "
        f"med={_fnum(s.get('med', float('nan')), 2)} gross/t={_fnum(s.get('gross', float('nan')), 2)} "
        f"fee/t={_fnum(s.get('fee', float('nan')), 2)} slip/t={_fnum(s.get('slip', float('nan')), 2)} "
        f"worst={_fnum(s.get('worst', float('nan')), 2)} maxDD={_fnum(s.get('maxdd', float('nan')), 1)} "
        f"top5%={_fnum(s.get('top5', float('nan')), 1)} holdHrs={_fnum(s.get('hold', float('nan')), 2)} "
        f"avg_dlt={_fnum(s.get('avg_dlt', float('nan')), 4)} avg_tv={_fnum(s.get('avg_tv', float('nan')), 2)} "
        f"exits={s.get('exits', {})}"
    )


def fmt_ww(s: dict[str, Any]) -> str:
    return (
        f"n={s.get('n', 0)} win%={_fnum(s.get('win', float('nan')), 1)} "
        f"mean={_fnum(s.get('mean', float('nan')), 2)} med={_fnum(s.get('med', float('nan')), 2)} "
        f"gross={_fnum(s.get('gross', float('nan')), 2)} worst={_fnum(s.get('worst', float('nan')), 2)} "
        f"maxDD={_fnum(s.get('maxdd', float('nan')), 1)}"
    )


def ww_block(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    wd, we = split_wd_we(rows)
    return stats_ww(wd), stats_ww(we)


def touch_bucket(n: int) -> str:
    if n <= 1:
        return "1"
    if n == 2:
        return "2"
    if n <= 5:
        return "3-5"
    return "6+"


def chop_tables(rows: list[dict[str, Any]]) -> list[str]:
    by_b: dict[str, list[float]] = {"1": [], "2": [], "3-5": [], "6+": []}
    for r in rows:
        b = touch_bucket(int(r.get("touch_count_of_line", 1)))
        by_b[b].append(float(r["net"]))
    out = [
        "CHOP (primary, informational):",
        "  NOTE: weekday/weekend and chop splits are informational; "
        "a split is only actionable if it holds in TRAIN and HOLDOUT.",
        "  mean net by touch_count_of_line:",
    ]
    for b in ("1", "2", "3-5", "6+"):
        xs = by_b[b]
        if not xs:
            out.append(f"    {b} n=0")
            continue
        out.append(f"    {b} n={len(xs)} mean={float(np.mean(xs)):.2f}")
    wd, we = split_wd_we(rows)
    out.append("  mean net by weekday/weekend (entry IST):")
    for name, xs_rows in (("WEEKDAY", wd), ("WEEKEND", we)):
        xs = [float(r["net"]) for r in xs_rows]
        if not xs:
            out.append(f"    {name} n=0")
            continue
        out.append(f"    {name} n={len(xs)} mean={float(np.mean(xs)):.2f}")
    if rows:
        out.append(
            "  field means: "
            f"touch_count={float(np.mean([float(r.get('touch_count_of_line', float('nan'))) for r in rows])):.2f} "
            f"line_age_min={float(np.mean([float(r.get('line_age_min', float('nan'))) for r in rows])):.1f} "
            f"|spot-VWAP|={float(np.mean([float(r.get('abs_spot_vwap', float('nan'))) for r in rows])):.1f} "
            f"n_active={float(np.mean([float(r.get('n_active_lines', float('nan'))) for r in rows])):.2f}"
        )
    return out


def progress_every_2pct(
    label: str,
    done: int,
    total: int,
    built: int,
    t0: float,
    state: dict[str, int],
) -> None:
    if total <= 0:
        return
    pct = 100.0 * float(done) / float(total)
    mark = int(pct // 2) * 2
    if done >= total:
        mark = 100
    last = int(state.get("mark", -1))
    if mark <= last and done < total:
        return
    if mark < 2 and done < total:
        return
    state["mark"] = mark
    elapsed = time.perf_counter() - t0
    eta = (elapsed / float(done)) * float(total - done) if done else 0.0
    mem = rss_mb()
    extra = f" RSS={mem:.0f}MB" if mem is not None else ""
    print(
        f"  {label} {done}/{total} ({mark}%) built={built} "
        f"elapsed={elapsed:.0f}s ETA={eta:.0f}s{extra}",
        flush=True,
    )


def ckpt_window_ok(
    done: dict[str, dict[str, Any]],
    start_ts: int,
    cutoff: int,
    max_days: int,
) -> bool:
    if not done:
        return True
    want = (int(start_ts), int(cutoff), int(max_days))
    for rec in done.values():
        got = (
            rec.get("win_start"),
            rec.get("win_cutoff"),
            rec.get("max_days"),
        )
        try:
            got_t = (int(got[0]), int(got[1]), int(got[2]))
        except (TypeError, ValueError):
            return False
        if got_t != want:
            return False
    return True


def entry_net_delta(legs: list[dict[str, Any]], side: str) -> float:
    xs = [float(lg["delta"]) for lg in legs if np.isfinite(float(lg.get("delta", float("nan"))))]
    if not xs:
        return float("nan")
    net = float(sum(xs))
    return net if side == "long" else -net


def entry_tv(legs: list[dict[str, Any]], spot: float) -> float:
    if spot <= 0:
        return float("nan")
    tot = 0.0
    for lg in legs:
        inn = intrinsic(bool(lg["is_call"]), float(lg["strike"]), float(spot))
        tot += (float(lg["mark"]) - inn) * s018.QTY * OPTIONS_CONTRACT_VALUE
    return tot


def load_ckpt(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[str(rec["key"])] = rec
    return out


def append_ckpt(path: Path, rec: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def cal_days(a: date, b: date) -> float:
    return float((b - a).days + 1)


def dow_hod_tables(rows: list[dict[str, Any]]) -> list[str]:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_d: dict[int, list[float]] = defaultdict(list)
    by_h: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        dt = datetime.fromtimestamp(int(r["entry_ts"]), tz=s018.UTC).astimezone(s018.IST)
        by_d[dt.weekday()].append(float(r["net"]))
        by_h[dt.hour].append(float(r["net"]))
    lines = ["  day-of-week:"]
    for i, name in enumerate(names):
        xs = by_d.get(i, [])
        if not xs:
            lines.append(f"    {name} n=0")
            continue
        win = 100.0 * sum(1 for x in xs if x > 0) / len(xs)
        lines.append(f"    {name} n={len(xs)} win%={win:.1f} mean={float(np.mean(xs)):.2f}")
    lines.append("  hour-of-day IST:")
    for h in range(24):
        xs = by_h.get(h, [])
        if not xs:
            continue
        win = 100.0 * sum(1 for x in xs if x > 0) / len(xs)
        lines.append(f"    {h:02d} n={len(xs)} win%={win:.1f} mean={float(np.mean(xs)):.2f}")
    return lines


def build_hour_index(
    ts: np.ndarray,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
) -> dict[int, np.ndarray]:
    buckets: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(ts):
        tt = int(t)
        if tt < start_ts or tt >= cutoff:
            continue
        if not in_window(tt, win_from, win_to):
            continue
        buckets[s018.hod_ist(tt)].append(i)
    return {h: np.asarray(v, dtype=np.int64) for h, v in buckets.items()}


def random_c2(
    real: list[dict[str, Any]],
    ts: np.ndarray,
    hour_idx: dict[int, np.ndarray],
    seed: int,
) -> list[tuple[int, str]]:
    need = [(s018.hod_ist(int(r["entry_ts"])), str(r["side"])) for r in real]
    rng = np.random.default_rng(seed)
    shuffled: dict[int, np.ndarray] = {}
    ptr: dict[int, int] = {}
    for h, arr in hour_idx.items():
        shuffled[h] = rng.permutation(arr)
        ptr[h] = 0
    out: list[tuple[int, str]] = []
    for hod, side in need:
        arr = shuffled.get(int(hod))
        if arr is None:
            continue
        p = int(ptr.get(int(hod), 0))
        if p >= int(arr.size):
            continue
        i = int(arr[p])
        ptr[int(hod)] = p + 1
        out.append((int(ts[i]), side))
    out.sort(key=lambda x: x[0])
    return out


def get_or_build_path(
    store: Any,
    spot_c: dict[int, float],
    t: int,
    side: str,
    tag: str,
    flipped: bool,
) -> tuple[dict[str, Any] | None, bool]:
    exp = expiry_1dte(t)
    fp = cache_file(t, side, exp, tag)
    path = load_path(fp)
    if path is not None:
        return path, False
    sp = spot_c.get(t)
    if sp is None:
        return None, False
    legs = pick_touch_basket(store, side, t, float(sp), flipped)
    if legs is None:
        return None, False
    path = build_path(store, spot_c, legs, t, exp)
    if path is None:
        return None, False
    save_path(fp, path)
    return path, True


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
    label: str = "",
    chop_by: dict[tuple[int, str], dict[str, Any]] | None = None,
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
            progress_every_2pct(label, j, nplan, built, t0, st_prog)
        if t < start_ts or t >= cutoff or t <= busy:
            continue
        if not in_window(t, win_from, win_to):
            continue
        path, newp = get_or_build_path(store, spot_c, t, side, tag, flipped)
        if newp:
            built += 1
        if path is None:
            n_stale += 1
            continue
        walked = scan_path(path, tgt, sl, spot_c)
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        extra: dict[str, Any] = {}
        if chop_by is not None:
            extra = dict(chop_by.get((int(t), str(side)), {}))
        rows.append(
            {
                "entry_ts": t,
                "side": side,
                "hod": s018.hod_ist(t),
                "hold_hrs": hold,
                "entry_dlt": entry_net_delta(list(path["legs"]), side),
                "entry_tv": entry_tv(list(path["legs"]), float(spot_c.get(t, 0.0))),
                "legs": path["legs"],
                **extra,
                **walked,
            }
        )
        busy = int(walked["exit_ts"])
    if label and nplan == 0:
        print(f"  {label} 0/0 (100%) built=0 elapsed=0s ETA=0s", flush=True)
    return rows, n_stale


def drop_s020_fresh() -> None:
    if CKPT.exists():
        CKPT.unlink()
        print(f"fresh: dropped {CKPT.name}")
    n = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.glob(f"*_{PATH_VER}.npz"):
            p.unlink(missing_ok=True)
            n += 1
    print(f"fresh: dropped {n} S020 path-cache files")


def print_examples(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    lines: list[SwingLine],
) -> None:
    print("=== 3 EXAMPLE SWING LINES ===", flush=True)
    shown = 0
    for ln in lines:
        if not ln.touches or ln.expire_i is None:
            continue
        ti = ln.touches[0]
        ei = ln.expire_i
        ci = ln.cross_i
        af = ln.active_from
        print(
            f"  {ln.kind.upper()} cross={s018.ist_str(int(ts[ci]))} "
            f"run_extreme={ln.extreme:.1f} level={ln.level:.1f} "
            f"active_from={s018.ist_str(int(ts[af])) if af < len(ts) else 'end'}",
            flush=True,
        )
        print(
            f"    touch {s018.ist_str(int(ts[ti]))} "
            f"OHLC={o[ti]:.1f}/{h[ti]:.1f}/{l[ti]:.1f}/{c[ti]:.1f}",
            flush=True,
        )
        print(
            f"    expire {s018.ist_str(int(ts[ei]))} close={c[ei]:.1f}",
            flush=True,
        )
        shown += 1
        if shown >= 3:
            break
    if shown == 0:
        print("  (no complete touch+expire examples in window)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--holdout", action="store_true")
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
        drop_s020_fresh()

    holdout = bool(args.holdout)
    if holdout:
        win_from, win_to = HOLD_FROM, HOLD_TO
        forbid = None
        tag = "holdout"
        start_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        cutoff = int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp())
    else:
        win_from, win_to = TRAIN_FROM, TRAIN_TO
        forbid = 2026
        tag = "train"
        start_ts = int(datetime(2024, 9, 1, tzinfo=timezone.utc).timestamp())
        cutoff = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    max_days = int(args.max_days or 0)
    if max_days:
        cutoff = start_ts + max_days * 86400
    if not args.fresh:
        preexisting = load_ckpt(CKPT)
        if preexisting and not ckpt_window_ok(preexisting, start_ts, cutoff, max_days):
            print("checkpoint window mismatch -> use --fresh", flush=True)
            sys.exit(1)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = bars_1m_vol(spot)
    if max_days:
        lo = start_ts - 2 * 86400
        hi = cutoff + 3 * 86400
        sel = (ts >= lo) & (ts < hi)
        ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
        print(f"SMOKE max-days={max_days} cutoff_ts={cutoff} window={tag}", flush=True)
    spot_c = {int(t): float(x) for t, x in zip(ts, c)}
    vwap = session_vwap(ts, h, l, c, vol)
    lines, raw_sigs, both_skip = detect_swings(ts, o, h, l, c, vwap)
    sigs = [
        s
        for s in raw_sigs
        if start_ts <= int(s["ts"]) < cutoff and in_window(int(s["ts"]), win_from, win_to)
    ]
    n_days = cal_days(win_from, win_to) if not max_days else max(1.0, float(max_days))
    print(
        f"S020 {tag} lines={len(lines)} signals={len(sigs)} both_skip={both_skip} "
        f"cells={len(TGTS)*len(SLS)}",
        flush=True,
    )
    if max_days:
        print_examples(ts, o, h, l, c, lines)

    chop_by: dict[tuple[int, str], dict[str, Any]] = {}
    for s in sigs:
        chop_by[(int(s["ts"]), str(s["side"]))] = {
            "touch_count_of_line": int(s.get("touch_count_of_line", 0)),
            "line_age_min": int(s.get("line_age_min", 0)),
            "abs_spot_vwap": float(s.get("abs_spot_vwap", float("nan"))),
            "n_active_lines": int(s.get("n_active_lines", 0)),
        }

    inner = MarksStore()
    store = GuardStore(inner, forbid_year=forbid)
    done = {} if args.fresh else load_ckpt(CKPT)
    cells = [(int(t), int(s)) for t in TGTS for s in SLS]
    t_run = time.perf_counter()
    cell_rows: dict[str, list[dict[str, Any]]] = {}
    n_stale_prim = 0
    win_meta = {"win_start": int(start_ts), "win_cutoff": int(cutoff), "max_days": int(max_days)}

    for k, (tgt, slv) in enumerate(cells, start=1):
        key = f"T={tgt}|SL={slv}"
        if key in done and not args.fresh:
            mem = rss_mb()
            extra = f" RSS={mem:.0f}MB" if mem is not None else ""
            print(f"done {k}/{len(cells)} SKIP {key}{extra}", flush=True)
            continue
        t0 = time.perf_counter()
        plan = [(int(s["ts"]), str(s["side"])) for s in sigs]
        rows, n_stale = simulate_plan(
            store, spot_c, plan, float(tgt), float(slv), "PRIMARY", False,
            start_ts, cutoff, win_from, win_to,
            label=f"grid {key}",
            chop_by=chop_by,
        )
        if tgt == PRIMARY_T and slv == PRIMARY_SL:
            n_stale_prim = n_stale
        stt = stats_of(rows, len(sigs), n_days)
        wd_s, we_s = ww_block(rows)
        stt["key"] = key
        stt["tgt"] = tgt
        stt["sl"] = slv
        stt["wd"] = wd_s
        stt["we"] = we_s
        stt.update(win_meta)
        append_ckpt(CKPT, {kk: vv for kk, vv in stt.items() if kk != "exits"} | {"exits": stt["exits"]})
        done[key] = stt
        cell_rows[key] = rows
        elapsed = time.perf_counter() - t_run
        eta = (elapsed / k) * (len(cells) - k)
        mem = rss_mb()
        extra = f" RSS={mem:.0f}MB" if mem is not None else ""
        print(
            f"done {k}/{len(cells)} {key} n={stt['n']} mean={stt['mean']:.2f} "
            f"set_s={time.perf_counter()-t0:.1f} elapsed={elapsed:.0f}s ETA={eta:.0f}s{extra}",
            flush=True,
        )
        s018._CHAIN.clear()
        gc.collect()

    ranked = sorted(
        [s for s in done.values() if "mean" in s],
        key=lambda s: float(s["mean"]) if np.isfinite(s.get("mean", float("nan"))) else -1e18,
        reverse=True,
    )
    prim_key = f"T={PRIMARY_T}|SL={PRIMARY_SL}"
    best = ranked[0] if ranked else None
    special = {prim_key}
    if best is not None:
        special.add(str(best["key"]))

    def ensure_rows(key: str, tgt: int, slv: int) -> list[dict[str, Any]]:
        if key in cell_rows:
            return cell_rows[key]
        print(f"ensure_rows {key}", flush=True)
        plan = [(int(s["ts"]), str(s["side"])) for s in sigs]
        rows, _ = simulate_plan(
            store, spot_c, plan, float(tgt), float(slv), "PRIMARY", False,
            start_ts, cutoff, win_from, win_to,
            label=f"ensure_rows {key}",
            chop_by=chop_by,
        )
        cell_rows[key] = rows
        return rows

    print("CONTROLS phase (C2 seeds + C3)", flush=True)
    hour_idx = build_hour_index(ts, start_ts, cutoff, win_from, win_to)
    ctrl_lines: list[str] = []
    for key in special:
        parts = str(key).replace("T=", "").replace("SL=", "").split("|")
        tgt, slv = int(parts[0]), int(parts[1])
        print(f"ensure_rows {key} (controls)", flush=True)
        rows = ensure_rows(key, tgt, slv)
        c2s: list[float] = []
        c2_pool: list[dict[str, Any]] = []
        n_seeds = len(RANDOM_SEEDS)
        for si, seed in enumerate(RANDOM_SEEDS, start=1):
            print(f"C2 {key} seed {si}/{n_seeds}", flush=True)
            forced = random_c2(rows, ts, hour_idx, seed)
            rr, _ = simulate_plan(
                store, spot_c, forced, float(tgt), float(slv), "C2", False,
                start_ts, cutoff, win_from, win_to,
                label=f"C2 {key} seed={si}/{n_seeds}",
            )
            c2_pool.extend(rr)
            if rr:
                c2s.append(float(np.mean([x["net"] for x in rr])))
        print(f"C3 {key}", flush=True)
        plan = [(int(r["entry_ts"]), str(r["side"])) for r in rows]
        c3, _ = simulate_plan(
            store, spot_c, plan, float(tgt), float(slv), "C3", True,
            start_ts, cutoff, win_from, win_to,
            label=f"C3 {key}",
        )
        c2m = float(np.mean(c2s)) if c2s else float("nan")
        c3s = stats_of(c3, len(plan), n_days)
        c2_wd, c2_we = ww_block(c2_pool)
        c3_wd, c3_we = ww_block(c3)
        c2_all_s = stats_ww(c2_pool)
        done[key]["c2"] = c2m
        done[key]["c3"] = c3s["mean"]
        ctrl_lines.append(f"  {key} C2mean={c2m:.2f} (seeds={len(c2s)}) C3 {fmt_stats(c3s)}")
        ctrl_lines.append(f"    C2 ALL {fmt_ww(c2_all_s)}")
        ctrl_lines.append(f"    C2 WEEKDAY {fmt_ww(c2_wd)}")
        ctrl_lines.append(f"    C2 WEEKEND {fmt_ww(c2_we)}")
        ctrl_lines.append(f"    C3 ALL {fmt_ww(stats_ww(c3))}")
        ctrl_lines.append(f"    C3 WEEKDAY {fmt_ww(c3_wd)}")
        ctrl_lines.append(f"    C3 WEEKEND {fmt_ww(c3_we)}")

    if max_days:
        prow = ensure_rows(prim_key, PRIMARY_T, PRIMARY_SL)
        print("=== FIRST 3 TRADES (all legs) ===", flush=True)
        for tr in prow[:3]:
            print(
                f"  {tr['side']} entry={s018.ist_str(int(tr['entry_ts']))} "
                f"exit={s018.ist_str(int(tr['exit_ts']))} reason={tr['reason']} "
                f"net={tr['net']:.2f}",
                flush=True,
            )
            for lg in tr.get("legs", []):
                kind = "CALL" if lg["is_call"] else "PUT"
                print(
                    f"    {lg['role']} {kind} K={float(lg['strike']):.0f} "
                    f"mark={float(lg['mark']):.2f} delta={float(lg.get('delta', float('nan'))):.4f} "
                    f"src={lg.get('src')}",
                    flush=True,
                )
        if not prow:
            print("  (no trades)", flush=True)

    report = [
        f"S020 VWAP swing-touch basket {tag.upper()} {win_from}..{win_to}",
        f"stamp={stamp} signals={len(sigs)} both_skip={both_skip} stale_skip~={n_stale_prim}",
        "expiry=next IST day 17:30 (1DTE); LONG put~1000+call~500+call~300; "
        "SHORT call~1000+put~500+put~300; qty=1000",
        "fees=estimate_option_fee*1.18; slip=slip_pct; stale>5m skip",
        "NOTE: weekday/weekend and chop splits are informational; "
        "a split is only actionable if it holds in TRAIN and HOLDOUT.",
        f"PRIMARY T={PRIMARY_T} SL={PRIMARY_SL}",
        f"PRIMARY ALL {fmt_stats(done.get(prim_key, {'n': 0, 'exits': {}}))}",
    ]
    prim_st = done.get(prim_key, {})
    if "wd" in prim_st:
        report.append(f"PRIMARY WEEKDAY {fmt_ww(prim_st['wd'])}")
        report.append(f"PRIMARY WEEKEND {fmt_ww(prim_st['we'])}")
    report.append("GRID (all T x SL):")
    for s in ranked:
        report.append(f"  {s.get('key')} ALL {fmt_stats(s)}")
        wd = s.get("wd")
        we = s.get("we")
        if isinstance(wd, dict) and isinstance(we, dict):
            report.append(f"    WEEKDAY {fmt_ww(wd)}")
            report.append(f"    WEEKEND {fmt_ww(we)}")
        elif str(s.get("key")) in cell_rows:
            wdx, wex = ww_block(cell_rows[str(s.get("key"))])
            report.append(f"    WEEKDAY {fmt_ww(wdx)}")
            report.append(f"    WEEKEND {fmt_ww(wex)}")
    report.append("CONTROLS (primary + best-train cell):")
    report.extend(ctrl_lines)
    prow = ensure_rows(prim_key, PRIMARY_T, PRIMARY_SL)
    report.append("PRIMARY day/hour tables:")
    report.extend(dow_hod_tables(prow))
    report.extend(chop_tables(prow))
    report.append("")
    report.append("--- PRE-REGISTERED PASS (PRIMARY 2025 & 2026; n>=30 mean>0 >C2 top5<100) ---")
    by_year: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in prow:
        y = datetime.fromtimestamp(int(r["entry_ts"]), tz=s018.UTC).astimezone(s018.IST).year
        by_year[y].append(r)
    ok = True
    bits = []
    c2_all = float(done.get(prim_key, {}).get("c2", float("nan")))
    for yr in (2025, 2026):
        rs = by_year.get(yr, [])
        st = stats_of(rs, len(rs), 365.0)
        bits.append(f"{yr} n={st['n']} mean={st['mean']} c2={c2_all} top5={st['top5']}")
        if not (
            int(st["n"]) >= 30
            and np.isfinite(st["mean"])
            and st["mean"] > 0
            and np.isfinite(c2_all)
            and st["mean"] > c2_all
            and np.isfinite(st["top5"])
            and st["top5"] < 100.0
        ):
            ok = False
    report.append("PASS" if ok else "FAIL")
    report.append("  " + " | ".join(bits))

    txtp = OUT_DIR / f"s020_{tag}_{stamp}.txt"
    txtp.write_text("\n".join(report) + "\n", encoding="utf-8")
    csvp = OUT_DIR / f"s020_{tag}_{stamp}_trades.csv"
    with csvp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "side", "entry_ts_ist", "exit_ts_ist", "reason", "gross", "fees", "net",
                "hold_hrs", "touch_count_of_line", "line_age_min", "abs_spot_vwap",
                "n_active_lines",
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
                    "touch_count_of_line": r.get("touch_count_of_line"),
                    "line_age_min": r.get("line_age_min"),
                    "abs_spot_vwap": r.get("abs_spot_vwap"),
                    "n_active_lines": r.get("n_active_lines"),
                }
            )
    print("\n".join(report))
    print(f"wrote {txtp}")
    print(f"wrote {csvp}")
    store.close()


if __name__ == "__main__":
    main()
