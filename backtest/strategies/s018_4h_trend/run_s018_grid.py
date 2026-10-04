#!/usr/bin/env python3
"""S018 grid train/holdout. Does not modify run_s018.py.

python backtest\\strategies\\s018_4h_trend\\run_s018_grid.py --stage A --max-days 10 --max-signal-sets 2
python backtest\\strategies\\s018_4h_trend\\run_s018_grid.py --stage B
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
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
    supertrend,
    t_years,
)
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402

logger = logging.getLogger("s018_grid")

OUT_DIR = Path("backtest/strategies/s018_4h_trend/runs")
CACHE_DIR = OUT_DIR / "s018_pathcache"
FINALISTS_PATH = OUT_DIR / "s018_finalists.json"
CKPT_A = OUT_DIR / "s018_gridA_ckpt.jsonl"

TRAIN_FROM = date(2024, 9, 1)
TRAIN_TO = date(2025, 12, 31)
HOLD_FROM = date(2026, 1, 1)
HOLD_TO = date(2026, 9, 21)

TFS = (240, 60, 15)  # 4h, 1h, 15m — same grid, 4h first
EMA_F = (2, 3, 4, 5, 6)
EMA_S = (8, 10, 12, 14, 16, 18, 20, 22)
ST_M = (1.0, 2.0, 3.0)
TGTS = (100, 150, 200, 250, 300, 350, 400)
SLS = (100, 150, 200, 250)
EMODES = ("0DTE", "1DTE", "2DTE")
N_SIGNAL_SETS = len(TFS) * len(EMA_F) * len(EMA_S) * len(ST_M) * len(EMODES)
N_CELLS = N_SIGNAL_SETS * len(TGTS) * len(SLS)

TF_LAB = {15: "15m", 60: "1h", 240: "4h"}
SRC_CODE = {"real": 1, "stale": 2, "settle": 3}
SRC_NAME = {1: "real", 2: "stale", 3: "settle"}
PATH_VER = "v2"


@dataclass(frozen=True)
class Cell:
    tf: int
    ef: int
    es: int
    stm: float
    tgt: int
    sl: int
    emode: str

    def key(self) -> str:
        return (
            f"TF={TF_LAB[self.tf]}|Ef={self.ef}|Es={self.es}|STm={self.stm:g}"
            f"|T={self.tgt}|SL={self.sl}|E={self.emode}"
        )


class GuardStore:
    """Refuse sqlite months in forbid_year+ (Stage A: no 2026 marks)."""

    def __init__(self, inner: MarksStore, forbid_year: int | None) -> None:
        self.inner = inner
        self.forbid_year = forbid_year

    def conn(self, d: date) -> Any:
        if self.forbid_year is not None and d.year >= self.forbid_year:
            return None
        return self.inner.conn(d)

    def close(self) -> None:
        self.inner.close()


def print_entry_timing() -> None:
    print("=== ENTRY TIMING CHECK ===")
    print(
        "1m spot CSV column is open_time_unix: row 09:29 IST is the candle that "
        "OPENS 09:29 and CLOSES 09:30. The close field is the 09:30 price."
    )
    print(
        "Option marks.ts is Delta history candle time (same open label); "
        "marks.close at ts=09:29 is the mark at 09:30."
    )
    print(
        "S018 close_t = TF_open + tf_len - 60s (last 1m of the bar). "
        "Using that row's close is the price available AT bar close. OK — "
        "run_s018.py not changed."
    )
    print(
        "Example: 4h 00:00-04:00 UTC last 1m open=03:59 UTC=09:29 IST; "
        "close on that row = 04:00 UTC / 09:30 IST price. "
        "Smoke entry 2024-09-02 09:29 IST is the 09:30 4h close. OK."
    )
    print()


def expiry_mode(t: int, mode: str) -> date:
    d = ist_date(t)
    if mode == "0DTE":
        return s018.choose_expiry(t)
    if mode == "1DTE":
        return d + timedelta(days=1)
    return d + timedelta(days=2)


def resample_tf(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    tf_min: int,
) -> tuple[np.ndarray, ...]:
    step = int(tf_min) * 60
    buckets: dict[int, list[int]] = {}
    for i in range(len(ts)):
        key = (int(ts[i]) // step) * step
        buckets.setdefault(key, []).append(i)
    keys, hh, ll, cc, close_t = [], [], [], [], []
    for key in sorted(buckets):
        idxs = buckets[key]
        last_need = key + step - 60
        have = {int(ts[i]) for i in idxs}
        if len(have) != int(tf_min) or last_need not in have:
            continue
        keys.append(key)
        hh.append(float(np.max(h[idxs])))
        ll.append(float(np.min(l[idxs])))
        cc.append(float(c[idxs[-1]]))
        close_t.append(last_need)
    return (
        np.array(keys, dtype=np.int64),
        np.array(hh, dtype=np.float64),
        np.array(ll, dtype=np.float64),
        np.array(cc, dtype=np.float64),
        np.array(close_t, dtype=np.int64),
    )


def pick_basket_at(
    store: Any, side: str, t: int, spot: float, exp: date, arm: str
) -> list[s018.Leg] | None:
    packed = s018.load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _ = packed
    exp_ts = s018.expiry_unix(exp)
    t_yr = t_years(t, exp_ts)
    dte = max(0, (exp - ist_date(t)).days)
    if side == "long":
        hi = s018.nearest(rows, True, s018.P_HI)
        lo = s018.nearest(rows, False, s018.P_LO)
    else:
        hi = s018.nearest(rows, False, s018.P_HI)
        lo = s018.nearest(rows, True, s018.P_LO)
    if hi is None:
        return None
    chosen = [hi] if arm == "C1" else [hi, lo]
    if any(x is None for x in chosen):
        return None
    legs: list[s018.Leg] = []
    for i, r in enumerate(chosen):
        assert r is not None
        q = s018.mark_le(store, exp, str(r["symbol"]), t)
        if q is None:
            return None
        fill, _sf = s018.buy_fill(q.px, dte)
        dlt = s018.signed_delta(q.px, spot, float(r["strike"]), t_yr, bool(r["is_call"]))
        legs.append(
            s018.Leg(
                symbol=str(r["symbol"]),
                strike=float(r["strike"]),
                is_call=bool(r["is_call"]),
                role="hi" if i == 0 else "lo",
                mark=q.px,
                fill=fill,
                src=q.src,
                delta=dlt,
                ts=q.ts,
            )
        )
    return legs


def rss_mb() -> float | None:
    try:
        import psutil  # noqa: PLC0415

        return float(psutil.Process().memory_info().rss) / (1024.0 * 1024.0)
    except Exception:
        pass
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


def ts_in_sorted(t: int, arr: np.ndarray) -> bool:
    if arr.size == 0:
        return False
    i = int(np.searchsorted(arr, t))
    return i < arr.size and int(arr[i]) == int(t)


def cache_file(entry_ts: int, side: str, exp: date, arm: str) -> Path:
    return CACHE_DIR / f"{entry_ts}_{side}_{exp.isoformat()}_{arm}_{PATH_VER}.npz"


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
        "tf_close": z["tf_close"],
        "px0": z["px0"],
        "px1": z["px1"],
        "src0": z["src0"],
        "src1": z["src1"],
    }


def save_path(p: Path, obj: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps({"exp": obj["exp"], "legs": obj["legs"]})
    np.savez_compressed(
        p,
        ts=obj["ts"],
        ok=obj["ok"],
        pnl=obj["pnl"],
        spot=obj["spot"],
        tf_close=obj["tf_close"],
        px0=obj["px0"],
        px1=obj["px1"],
        src0=obj["src0"],
        src1=obj["src1"],
        entry_ts=np.array([obj["entry_ts"]], dtype=np.int64),
        exp_ts=np.array([obj["exp_ts"]], dtype=np.int64),
        dte=np.array([obj["dte"]], dtype=np.int32),
        meta_json=np.array([meta.encode("utf-8")]),
    )


def build_path(
    store: Any,
    spot_c: dict[int, float],
    legs: list[s018.Leg],
    entry_ts: int,
    exp: date,
    close_t: np.ndarray,
) -> dict[str, Any] | None:
    exp_ts = s018.expiry_unix(exp)
    series = [load_symbol_series(store, lg.symbol, entry_ts, exp_ts) for lg in legs]
    ts = np.arange(int(entry_ts) + 60, int(exp_ts) + 1, 60, dtype=np.int64)
    n = int(ts.size)
    ok = np.zeros(n, dtype=np.bool_)
    pnl = np.full(n, np.nan, dtype=np.float32)
    spot = np.zeros(n, dtype=np.float32)
    px0 = np.zeros(n, dtype=np.float32)
    px1 = np.zeros(n, dtype=np.float32)
    src0 = np.zeros(n, dtype=np.int8)
    src1 = np.zeros(n, dtype=np.int8)
    j = np.searchsorted(close_t, ts)
    jclip = np.minimum(j, max(len(close_t) - 1, 0))
    tf_close = (j < len(close_t)) & (close_t[jclip] == ts) if len(close_t) else np.zeros(n, dtype=np.bool_)
    nleg = len(legs)
    for i in range(n):
        t = int(ts[i])
        spot[i] = float(spot_c.get(t, 0.0))
        qs = [s018.series_le(ser, t) for ser in series]
        if any(q is None for q in qs):
            continue
        ok[i] = True
        pnl[i] = np.float32(s018.mark_pnl(legs, qs))  # type: ignore[arg-type]
        px0[i] = np.float32(qs[0].px)  # type: ignore[union-attr]
        src0[i] = np.int8(SRC_CODE.get(qs[0].src, 0))  # type: ignore[union-attr]
        if nleg > 1:
            px1[i] = np.float32(qs[1].px)  # type: ignore[union-attr]
            src1[i] = np.int8(SRC_CODE.get(qs[1].src, 0))  # type: ignore[union-attr]
    dte = max(0, (exp - ist_date(entry_ts)).days)
    el: list[dict[str, Any]] = []
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
        "tf_close": tf_close,
        "px0": px0,
        "px1": px1,
        "src0": src0,
        "src1": src1,
    }


def trend_exit_ts(
    close_t: np.ndarray, ema_f: np.ndarray, st: np.ndarray, side: str
) -> np.ndarray:
    finite = np.isfinite(ema_f) & np.isfinite(st)
    if side == "long":
        hit = finite & (ema_f < st)
    else:
        hit = finite & (ema_f > st)
    return np.ascontiguousarray(close_t[hit], dtype=np.int64)


def scan_path(
    path: dict[str, Any],
    tgt: float,
    sl: float,
    side: str,
    trend_ts: np.ndarray,
    spot_c: dict[int, float],
    arm: str,
) -> dict[str, Any] | None:
    legs_raw = list(path["legs"])
    if arm == "C1":
        legs_raw = [lg for lg in legs_raw if lg["role"] == "hi"]
    fills = [float(lg["fill"]) for lg in legs_raw]
    entry_fee = sum(float(lg["fee"]) for lg in legs_raw)
    entry_slip = sum(float(lg["slip"]) for lg in legs_raw)
    exp_ts = int(path["exp_ts"])
    dte0 = int(path["dte"])
    ts = path["ts"]
    ok = path["ok"]
    pnl_a = path["pnl"]
    tf_c = path["tf_close"]
    px0 = path["px0"]
    px1 = path["px1"]
    src0 = path["src0"]
    src1 = path["src1"]
    spot_a = path["spot"]
    n = int(ts.size)
    for i in range(n):
        t = int(ts[i])
        if bool(ok[i]) and np.isfinite(pnl_a[i]):
            pnl = float(pnl_a[i])
            if arm == "C1":
                pnl = (float(px0[i]) - fills[0]) * s018.QTY * OPTIONS_CONTRACT_VALUE
            if pnl >= tgt:
                return _exit_from_idx(
                    i, fills, legs_raw, t, "TARGET", dte0, spot_c, entry_fee, entry_slip,
                    px0, px1, src0, src1, spot_a,
                )
            if pnl <= -sl:
                return _exit_from_idx(
                    i, fills, legs_raw, t, "SL", dte0, spot_c, entry_fee, entry_slip,
                    px0, px1, src0, src1, spot_a,
                )
            if bool(tf_c[i]) and ts_in_sorted(t, trend_ts):
                return _exit_from_idx(
                    i, fills, legs_raw, t, "TREND", dte0, spot_c, entry_fee, entry_slip,
                    px0, px1, src0, src1, spot_a,
                )
        if t == exp_ts:
            sp = float(spot_a[i]) if float(spot_a[i]) > 0 else float(spot_c.get(t, 0.0))
            if sp <= 0:
                return None
            gross = 0.0
            fees = entry_fee
            slip = entry_slip
            srcs = [str(lg["src"]) for lg in legs_raw]
            for lg in legs_raw:
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


def _exit_from_idx(
    i: int,
    fills: list[float],
    legs_raw: list[dict[str, Any]],
    t: int,
    reason: str,
    dte: int,
    spot_c: dict[int, float],
    entry_fee: float,
    entry_slip: float,
    px0: np.ndarray,
    px1: np.ndarray,
    src0: np.ndarray,
    src1: np.ndarray,
    spot_a: np.ndarray,
) -> dict[str, Any]:
    idx = float(spot_a[i]) or float(spot_c.get(t, 0.0))
    pxs = [float(px0[i])]
    srcs_m = [int(src0[i])]
    if len(fills) > 1:
        pxs.append(float(px1[i]))
        srcs_m.append(int(src1[i]))
    gross = 0.0
    fees = entry_fee
    slip = entry_slip
    srcs = [str(lg["src"]) for lg in legs_raw]
    for fill, mark, src_i in zip(fills, pxs, srcs_m):
        xf, _ = s018.sell_fill(mark, dte)
        gross += (xf - fill) * s018.QTY * OPTIONS_CONTRACT_VALUE
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


def detect_signals(
    ema_f: np.ndarray, ema_s: np.ndarray, st: np.ndarray
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    n = len(st)
    for i in range(1, n):
        if any(
            math.isnan(float(x[i])) or math.isnan(float(x[i - 1]))
            for x in (ema_f, ema_s, st)
        ):
            continue
        if (
            float(ema_f[i]) > float(st[i])
            and float(st[i - 1]) <= float(ema_s[i - 1])
            and float(st[i]) > float(ema_s[i])
        ):
            out.append((i, "long"))
        elif (
            float(ema_f[i]) < float(st[i])
            and float(st[i - 1]) >= float(ema_s[i - 1])
            and float(st[i]) < float(ema_s[i])
        ):
            out.append((i, "short"))
    return out


def signal_sets(max_n: int, tfs: tuple[int, ...] | None = None) -> list[tuple[int, int, int, float, str]]:
    out: list[tuple[int, int, int, float, str]] = []
    for tf in (tfs if tfs is not None else TFS):
        for ef in EMA_F:
            for es in EMA_S:
                if es <= ef:
                    continue
                for stm in ST_M:
                    for em in EMODES:
                        out.append((tf, ef, es, stm, em))
                        if max_n and len(out) >= max_n:
                            return out
    return out


def set_cell_keys(tf: int, ef: int, es: int, stm: float, emode: str) -> list[str]:
    return [
        Cell(tf=tf, ef=ef, es=es, stm=stm, tgt=int(tgt), sl=int(slv), emode=emode).key()
        for tgt in TGTS
        for slv in SLS
    ]


def set_complete(done: dict[str, Any] | set[str], tf: int, ef: int, es: int, stm: float, emode: str) -> bool:
    keys = set_cell_keys(tf, ef, es, stm, emode)
    if isinstance(done, dict):
        return all(k in done for k in keys)
    return all(k in done for k in keys)


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


def stats_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    nets = [float(r["net"]) for r in rows]
    reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        reasons[str(r["reason"])] += 1
    return {
        "n": n,
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
    }


def fmt_stats(s: dict[str, Any]) -> str:
    return (
        f"n={s['n']} win%={s['win']:.1f} mean={s['mean']:.2f} med={s['med']:.2f} "
        f"gross/t={s['gross']:.2f} fee/t={s['fee']:.2f} slip/t={s['slip']:.2f} "
        f"worst={s['worst']:.2f} maxDD={s['maxdd']:.1f} top5%={s['top5']:.1f} "
        f"exits={s['exits']}"
    )


def neighbors(c: Cell) -> list[Cell]:
    seqs: dict[str, tuple] = {
        "tf": TFS,
        "ef": EMA_F,
        "es": EMA_S,
        "stm": ST_M,
        "tgt": TGTS,
        "sl": SLS,
        "emode": EMODES,
    }
    out: list[Cell] = []
    d = asdict(c)
    for k, seq in seqs.items():
        try:
            i = list(seq).index(d[k])
        except ValueError:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(seq):
                nd = dict(d)
                nd[k] = seq[j]
                if nd["es"] <= nd["ef"]:
                    continue
                out.append(Cell(**nd))
    return out


def hist_mean(means: list[float]) -> str:
    edges = [-1e9, -400, -200, -100, -50, 0, 50, 100, 200, 400, 1e9]
    labs = [
        "<-400", "-400..-200", "-200..-100", "-100..-50", "-50..0",
        "0..50", "50..100", "100..200", "200..400", ">400",
    ]
    cnt = [0] * (len(edges) - 1)
    for m in means:
        if not np.isfinite(m):
            continue
        for i in range(len(cnt)):
            if edges[i] <= m < edges[i + 1]:
                cnt[i] += 1
                break
    return " ".join(f"{labs[i]}={cnt[i]}" for i in range(len(cnt)))


def dow_table(rows: list[dict[str, Any]]) -> list[str]:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(int(r["entry_ts"]), tz=s018.UTC).astimezone(s018.IST)
        by[d.weekday()].append(float(r["net"]))
    lines = []
    for i, name in enumerate(names):
        xs = by.get(i, [])
        if not xs:
            lines.append(f"  {name} n=0")
            continue
        win = 100.0 * sum(1 for x in xs if x > 0) / len(xs)
        lines.append(f"  {name} n={len(xs)} win%={win:.1f} mean={float(np.mean(xs)):.2f}")
    return lines


def in_window(ts: int, a: date, b: date) -> bool:
    d = datetime.fromtimestamp(int(ts), tz=s018.UTC).astimezone(s018.IST).date()
    return a <= d <= b


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
        f.write(json.dumps(rec) + "\n")


def random_c2(
    real: list[dict[str, Any]],
    close_t: np.ndarray,
    start_ts: int,
    cutoff: int | None,
    seed: int,
    a: date,
    b: date,
) -> list[tuple[int, str]]:
    need = []
    for r in real:
        hod = s018.hod_ist(int(r["entry_ts"]))
        need.append((hod, str(r["side"])))
    by_h: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(close_t):
        tt = int(t)
        if tt < start_ts:
            continue
        if cutoff is not None and tt >= cutoff:
            continue
        if not in_window(tt, a, b):
            continue
        by_h[s018.hod_ist(tt)].append(i)
    rng = np.random.default_rng(seed)
    used: set[int] = set()
    out: list[tuple[int, str]] = []
    for hod, side in need:
        pool = [i for i in by_h.get(hod, []) if i not in used]
        if not pool:
            continue
        i = int(rng.choice(pool))
        used.add(i)
        out.append((i, side))
    out.sort(key=lambda x: x[0])
    return out


def stage_a(args: argparse.Namespace) -> None:
    print_entry_timing()
    load_slip_table()
    cache_gb = float(args.cache_gb)
    reset_mark_cache(max_bytes=int(cache_gb * 1024**3))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh and CKPT_A.exists():
        CKPT_A.unlink()
        print("fresh: dropped Stage A checkpoint")
    if args.fresh:
        for pth in CACHE_DIR.glob("*.pkl.gz"):
            pth.unlink(missing_ok=True)
        for pth in CACHE_DIR.glob(f"*_{PATH_VER}.npz"):
            pth.unlink(missing_ok=True)
        print("fresh: dropped path cache")

    done: dict[str, dict[str, Any]] = {} if args.fresh else load_ckpt(CKPT_A)
    spot = s018.load_spot_1m(args.csv)
    ts1, o1, h1, l1, c1 = s018.bars_1m(spot)
    start_ts = int(datetime(2024, 9, 1, tzinfo=timezone.utc).timestamp())
    cutoff = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    if args.max_days:
        cutoff = start_ts + int(args.max_days) * 86400
        lo = start_ts - 5 * 86400
        hi = cutoff + 3 * 86400
        sel = (ts1 >= lo) & (ts1 < hi)
        ts1, o1, h1, l1, c1 = ts1[sel], o1[sel], h1[sel], l1[sel], c1[sel]
        print(f"SMOKE max-days={args.max_days} cutoff_ts={cutoff}", flush=True)
    spot_c = {int(t): float(c) for t, c in zip(ts1, c1)}
    inner = MarksStore()
    store = GuardStore(inner, forbid_year=2026)

    tfs = TFS
    if getattr(args, "tf", None):
        tfs = tuple(int(x) for x in str(args.tf).split(",") if x.strip())
    sets = signal_sets(int(args.max_signal_sets) if args.max_signal_sets else 0, tfs)
    print(
        f"FULL GRID cells={N_CELLS} signal_sets={N_SIGNAL_SETS} "
        f"tgt/sl={len(TGTS)*len(SLS)} this_run_signal_sets={len(sets)} "
        f"TF_order={[TF_LAB[t] for t in tfs]} mark_cache_gb={cache_gb}",
        flush=True,
    )
    print("EST: stream per signal set x 28 cells. ETA after first completed set.", flush=True)

    t_run = time.perf_counter()
    n_unique = 0
    n_paths = 0
    rss_hist: list[float] = []
    cur_tf: int | None = None
    hh = ll = cc = close_t = None  # type: ignore[assignment]

    for k_set, (tf, ef, es, stm, emode) in enumerate(sets, start=1):
        t_set = time.perf_counter()
        if (not args.fresh) and set_complete(done, tf, ef, es, stm, emode):
            elapsed = time.perf_counter() - t_run
            eta = (elapsed / k_set) * (len(sets) - k_set)
            mem = rss_mb()
            extra = f" RSS={mem:.0f}MB" if mem is not None else ""
            if mem is not None:
                rss_hist.append(mem)
            print(
                f"done {k_set}/{len(sets)} SKIP completed "
                f"TF={TF_LAB[tf]} Ef={ef} Es={es} STm={stm:g} {emode} "
                f"elapsed={elapsed:.0f}s ETA={eta:.0f}s{extra}",
                flush=True,
            )
            continue
        if cur_tf != tf:
            hh = ll = cc = close_t = None
            gc.collect()
            _k, hh, ll, cc, close_t = resample_tf(ts1, o1, h1, l1, c1, tf)
            cur_tf = tf
            print(f"resample {TF_LAB[tf]} bars={len(close_t)}", flush=True)
        _tr, st = supertrend(hh, ll, cc, 1, float(stm))
        ema_f = s018.ema(cc, int(ef))
        ema_s = s018.ema(cc, int(es))
        trend_long = trend_exit_ts(close_t, ema_f, st, "long")
        trend_short = trend_exit_ts(close_t, ema_f, st, "short")
        sigs = detect_signals(ema_f, ema_s, st)
        entries: list[dict[str, Any]] = []
        for i, side in sigs:
            t = int(close_t[i])
            if t < start_ts or t >= cutoff:
                continue
            if not in_window(t, TRAIN_FROM, TRAIN_TO):
                continue
            exp = expiry_mode(t, emode)
            if exp.year >= 2026:
                continue
            sp = spot_c.get(t)
            if sp is None:
                continue
            n_unique += 1
            fp = cache_file(t, side, exp, "PRIMARY")
            path = None if args.fresh else load_path(fp)
            if path is None:
                legs = pick_basket_at(store, side, t, float(sp), exp, "PRIMARY")
                if legs is None:
                    continue
                path = build_path(store, spot_c, legs, t, exp, close_t)
                if path is None:
                    continue
                save_path(fp, path)
                n_paths += 1
            entries.append(
                {
                    "ts": t,
                    "side": side,
                    "path": path,
                    "trend": trend_long if side == "long" else trend_short,
                    "hod": s018.hod_ist(t),
                }
            )
        for tgt in TGTS:
            for slv in SLS:
                cell = Cell(
                    tf=int(tf),
                    ef=int(ef),
                    es=int(es),
                    stm=float(stm),
                    tgt=int(tgt),
                    sl=int(slv),
                    emode=str(emode),
                )
                ck = cell.key()
                if ck in done and not args.fresh:
                    continue
                busy = -1
                rows: list[dict[str, Any]] = []
                for e in entries:
                    if int(e["ts"]) <= busy:
                        continue
                    walked = scan_path(
                        e["path"],
                        float(tgt),
                        float(slv),
                        e["side"],
                        e["trend"],
                        spot_c,
                        "PRIMARY",
                    )
                    if walked is None:
                        continue
                    rows.append(
                        {
                            "entry_ts": e["ts"],
                            "side": e["side"],
                            "hod": e["hod"],
                            **walked,
                        }
                    )
                    busy = int(walked["exit_ts"])
                stt = stats_of(rows)
                stt["key"] = ck
                stt["cell"] = asdict(cell)
                append_ckpt(CKPT_A, stt)
                done[ck] = stt
        del entries, ema_f, ema_s, st, trend_long, trend_short, _tr
        gc.collect()
        elapsed = time.perf_counter() - t_run
        dt = time.perf_counter() - t_set
        eta = (elapsed / k_set) * (len(sets) - k_set)
        mem = rss_mb()
        extra = f" RSS={mem:.0f}MB" if mem is not None else ""
        if mem is not None:
            rss_hist.append(mem)
        print(
            f"done {k_set}/{len(sets)} TF={TF_LAB[tf]} Ef={ef} Es={es} "
            f"STm={stm:g} {emode} n_entry={n_unique} new_paths={n_paths} "
            f"set_s={dt:.1f} elapsed={elapsed:.0f}s ETA={eta:.0f}s{extra}",
            flush=True,
        )

    cell_stats = load_ckpt(CKPT_A)
    means = [float(s["mean"]) for s in cell_stats.values() if "mean" in s]
    n40p = sum(
        1
        for s in cell_stats.values()
        if int(s.get("n", 0)) >= 40
        and np.isfinite(s.get("mean", float("nan")))
        and s["mean"] > 0
    )
    lines = [
        f"S018 GRID STAGE A TRAIN {TRAIN_FROM}..{TRAIN_TO}",
        f"stamp={stamp} signal_sets={len(sets)} unique_entries~={n_unique} new_paths={n_paths}",
        f"share n>=40 & mean>0: {n40p}/{len(cell_stats)} = "
        f"{(100.0 * n40p / max(len(cell_stats), 1)):.2f}%",
        f"histogram mean net: {hist_mean(means)}",
        f"RSS hist MB={rss_hist}" if rss_hist else "RSS hist=n/a",
        "TOP 20 by mean net (any n):",
    ]
    ranked = sorted(
        cell_stats.values(),
        key=lambda s: float(s["mean"]) if np.isfinite(s.get("mean", float("nan"))) else -1e18,
        reverse=True,
    )
    for s in ranked[:20]:
        lines.append(f"  {s.get('key')} {fmt_stats(s)}")
    elig = [
        s
        for s in ranked
        if int(s.get("n", 0)) >= 40 and np.isfinite(s.get("mean", float("nan")))
    ]
    finalists: list[dict[str, Any]] = []
    by_key = {str(s["key"]): s for s in cell_stats.values()}
    for s in elig:
        c = Cell(**s["cell"])
        nbs = neighbors(c)
        pos = 0
        for nb in nbs:
            ns = by_key.get(nb.key())
            if ns and np.isfinite(ns.get("mean", float("nan"))) and ns["mean"] > 0:
                pos += 1
        frac = pos / len(nbs) if nbs else 0.0
        s["nb_frac"] = frac
        s["nb_n"] = len(nbs)
        if frac >= 0.5:
            finalists.append(s)
        if len(finalists) >= 5:
            break
    lines.append("FINALISTS (top mean n>=40, >=50% neighbors mean>0):")
    if not finalists:
        lines.append("  NONE")
    for s in finalists:
        lines.append(f"  {s['key']} {fmt_stats(s)} nb_frac={s['nb_frac']:.2f}")
    FINALISTS_PATH.write_text(json.dumps(finalists, indent=2, default=str), encoding="utf-8")
    txtp = OUT_DIR / f"s018_gridA_{stamp}.txt"
    txtp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {txtp}")
    print(f"wrote {FINALISTS_PATH}")
    print("STAGE A STOP")
    store.close()

def _run_entries_holdout(
    store: Any,
    spot_c: dict[int, float],
    pack: dict[str, Any],
    tgt: int,
    slv: int,
    arm: str,
    forced: list[tuple[int, str]] | None,
    start_ts: int,
    cutoff: int,
) -> list[dict[str, Any]]:
    close_t = pack["close_t"]
    ema_f = pack["ema_f"]
    st = pack["st"]
    emode = pack["emode"]
    trend_long = trend_exit_ts(close_t, ema_f, st, "long")
    trend_short = trend_exit_ts(close_t, ema_f, st, "short")
    if forced is None:
        plan = [(e["i"], e["side"], e["ts"]) for e in pack["entries"]]
    else:
        plan = [(i, side, int(close_t[i])) for i, side in forced]
    busy = -1
    rows: list[dict[str, Any]] = []
    for i, side, t in plan:
        if t < start_ts or t >= cutoff or t <= busy:
            continue
        if not in_window(t, HOLD_FROM, HOLD_TO):
            continue
        exp = expiry_mode(t, emode)
        sp = spot_c.get(t)
        if sp is None:
            continue
        fp = cache_file(t, side, exp, arm)
        path = load_path(fp)
        if path is None:
            legs = pick_basket_at(store, side, t, float(sp), exp, arm)
            if legs is None:
                continue
            path = build_path(store, spot_c, legs, t, exp, close_t)
            if path is None:
                continue
            save_path(fp, path)
        trset = trend_long if side == "long" else trend_short
        walked = scan_path(path, float(tgt), float(slv), side, trset, spot_c, arm)
        if walked is None:
            continue
        rows.append({"entry_ts": t, "side": side, "hod": s018.hod_ist(t), **walked})
        busy = int(walked["exit_ts"])
    return rows


def stage_b(args: argparse.Namespace) -> None:
    print_entry_timing()
    load_slip_table()
    if not FINALISTS_PATH.exists():
        print("NO finalists json — run Stage A first")
        return
    finalists = json.loads(FINALISTS_PATH.read_text(encoding="utf-8"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spot = s018.load_spot_1m(args.csv)
    ts1, o1, h1, l1, c1 = s018.bars_1m(spot)
    start_ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    cutoff = int(datetime(2026, 9, 22, tzinfo=timezone.utc).timestamp())
    lo = start_ts - 10 * 86400
    sel = (ts1 >= lo) & (ts1 < cutoff + 86400)
    ts1, o1, h1, l1, c1 = ts1[sel], o1[sel], h1[sel], l1[sel], c1[sel]
    spot_c = {int(t): float(c) for t, c in zip(ts1, c1)}
    store = MarksStore()

    need_cells: list[Cell] = []
    seen: set[str] = set()
    for s in finalists:
        c = Cell(**s["cell"])
        for x in [c, *neighbors(c)]:
            if x.key() not in seen:
                seen.add(x.key())
                need_cells.append(x)

    tf_pack: dict[int, tuple] = {}
    sig_pack: dict[tuple, dict[str, Any]] = {}
    lines = [f"S018 GRID STAGE B HOLDOUT {HOLD_FROM}..{HOLD_TO}", f"stamp={stamp}"]

    def get_pack(tf: int, ef: int, es: int, stm: float, emode: str) -> dict[str, Any]:
        k = (tf, ef, es, stm, emode)
        if k in sig_pack:
            return sig_pack[k]
        if tf not in tf_pack:
            _kk, hh, ll, cc, close_t = resample_tf(ts1, o1, h1, l1, c1, tf)
            tf_pack[tf] = (hh, ll, cc, close_t)
        hh, ll, cc, close_t = tf_pack[tf]
        _tr, st = supertrend(hh, ll, cc, 1, float(stm))
        ema_f = s018.ema(cc, int(ef))
        ema_s = s018.ema(cc, int(es))
        sigs = detect_signals(ema_f, ema_s, st)
        entries = []
        for i, side in sigs:
            t = int(close_t[i])
            if t < start_ts or t >= cutoff:
                continue
            entries.append({"i": i, "ts": t, "side": side})
        pack = {
            "tf": tf, "ef": ef, "es": es, "stm": stm, "emode": emode,
            "entries": entries, "close_t": close_t, "ema_f": ema_f, "st": st,
        }
        sig_pack[k] = pack
        return pack

    results: dict[str, dict[str, Any]] = {}
    for c in need_cells:
        pack = get_pack(c.tf, c.ef, c.es, c.stm, c.emode)
        # rebuild entries with paths
        ent2 = []
        for e in pack["entries"]:
            t = int(e["ts"])
            exp = expiry_mode(t, c.emode)
            sp = spot_c.get(t)
            if sp is None:
                continue
            fp = cache_file(t, e["side"], exp, "PRIMARY")
            path = load_path(fp)
            if path is None:
                legs = pick_basket_at(store, e["side"], t, float(sp), exp, "PRIMARY")
                if legs is None:
                    continue
                path = build_path(store, spot_c, legs, t, exp, pack["close_t"])
                if path is None:
                    continue
                save_path(fp, path)
            ent2.append(
                {
                    **e,
                    "path": path,
                    "trend": trend_exit_ts(
                        pack["close_t"], pack["ema_f"], pack["st"], e["side"]
                    ),
                    "hod": s018.hod_ist(t),
                }
            )
        pack_h = {**pack, "entries": ent2}
        rows = []
        busy = -1
        for e in ent2:
            if int(e["ts"]) <= busy:
                continue
            w = scan_path(
                e["path"], float(c.tgt), float(c.sl), e["side"], e["trend"], spot_c, "PRIMARY"
            )
            if w is None:
                continue
            rows.append({"entry_ts": e["ts"], "side": e["side"], **w})
            busy = int(w["exit_ts"])
        stt = stats_of(rows)
        stt["rows"] = rows
        results[c.key()] = stt

    fin_keys = [Cell(**s["cell"]).key() for s in finalists]
    for fk in fin_keys:
        c = Cell(**next(s["cell"] for s in finalists if Cell(**s["cell"]).key() == fk))
        pack = get_pack(c.tf, c.ef, c.es, c.stm, c.emode)
        prim = results[fk]
        rows = prim.get("rows", [])
        c1 = _run_entries_holdout(
            store, spot_c, pack, c.tgt, c.sl, "C1", None, start_ts, cutoff
        )
        c2s = []
        for seed in s018.RANDOM_SEEDS:
            forced = random_c2(rows, pack["close_t"], start_ts, cutoff, seed, HOLD_FROM, HOLD_TO)
            rr = _run_entries_holdout(
                store, spot_c, pack, c.tgt, c.sl, "PRIMARY", forced, start_ts, cutoff
            )
            if rr:
                c2s.append(float(np.mean([x["net"] for x in rr])))
        c2m = float(np.mean(c2s)) if c2s else float("nan")
        nbs = neighbors(c)
        pos = sum(
            1
            for nb in nbs
            if nb.key() in results
            and np.isfinite(results[nb.key()].get("mean", float("nan")))
            and results[nb.key()]["mean"] > 0
        )
        frac = pos / len(nbs) if nbs else 0.0
        s = prim
        ok = (
            int(s["n"]) >= 30
            and np.isfinite(s["mean"])
            and s["mean"] > 0
            and np.isfinite(c2m)
            and s["mean"] > c2m
            and np.isfinite(s["top5"])
            and s["top5"] < 100.0
            and frac >= 0.5
        )
        lines.append(f"FINALIST {fk}")
        lines.append(f"  HOLDOUT PRIMARY {fmt_stats(s)} C2mean={c2m:.2f} nb_pos_frac={frac:.2f}")
        lines.append(f"  C1 {fmt_stats(stats_of(c1))}")
        lines.append("  PASS" if ok else "  FAIL")
        lines.append("  DOW holdout:")
        lines.extend(dow_table(rows))
        lines.append("  DOW train: (from Stage A json if present)")

    txt = OUT_DIR / f"s018_gridB_{stamp}.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {txt}")
    store.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("A", "B"), default="A")
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--max-signal-sets", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--cache-gb", type=float, default=1.0)
    ap.add_argument("--tf", default="", help="comma TF minutes, e.g. 15 or 240,60,15")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.stage == "A":
        stage_a(args)
    else:
        reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
        stage_b(args)


if __name__ == "__main__":
    main()
