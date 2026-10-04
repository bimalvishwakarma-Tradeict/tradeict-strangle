#!/usr/bin/env python3
"""S018 grid train/holdout. Does not modify run_s018.py.

python backtest\\strategies\\s018_4h_trend\\run_s018_grid.py --stage A --max-days 10 --max-signal-sets 2
python backtest\\strategies\\s018_4h_trend\\run_s018_grid.py --stage B
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import pickle
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

TFS = (15, 60, 240)
EMA_F = (2, 3, 4, 5, 6)
EMA_S = (8, 10, 12, 14, 16, 18, 20, 22)
ST_M = (1.0, 2.0, 3.0)
TGTS = (100, 150, 200, 250, 300, 350, 400)
SLS = (100, 150, 200, 250)
EMODES = ("0DTE", "1DTE", "2DTE")
N_SIGNAL_SETS = len(TFS) * len(EMA_F) * len(EMA_S) * len(ST_M) * len(EMODES)
N_CELLS = N_SIGNAL_SETS * len(TGTS) * len(SLS)

TF_LAB = {15: "15m", 60: "1h", 240: "4h"}


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


def cache_file(entry_ts: int, side: str, exp: date, arm: str) -> Path:
    return CACHE_DIR / f"{entry_ts}_{side}_{exp.isoformat()}_{arm}.pkl.gz"


def load_path(p: Path) -> dict[str, Any] | None:
    if not p.exists():
        return None
    with gzip.open(p, "rb") as f:
        return pickle.load(f)  # noqa: S301


def save_path(p: Path, obj: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)


def build_path(
    store: Any,
    spot_c: dict[int, float],
    legs: list[s018.Leg],
    entry_ts: int,
    exp: date,
    close_set: set[int],
) -> dict[str, Any] | None:
    exp_ts = s018.expiry_unix(exp)
    series = [load_symbol_series(store, lg.symbol, entry_ts, exp_ts) for lg in legs]
    minutes: list[dict[str, Any]] = []
    t = int(entry_ts) + 60
    while t <= exp_ts:
        qs = [s018.series_le(ser, t) for ser in series]
        rec: dict[str, Any] = {
            "ts": t,
            "tf_close": 1 if t in close_set else 0,
            "spot": float(spot_c.get(t, 0.0)),
            "ok": 0,
            "pnl": None,
            "px": [],
            "src": [],
            "qts": [],
        }
        if all(q is not None for q in qs):
            rec["ok"] = 1
            rec["pnl"] = s018.mark_pnl(legs, qs)  # type: ignore[arg-type]
            rec["px"] = [q.px for q in qs]  # type: ignore[union-attr]
            rec["src"] = [q.src for q in qs]  # type: ignore[union-attr]
            rec["qts"] = [q.ts for q in qs]  # type: ignore[union-attr]
        minutes.append(rec)
        t += 60
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
        "minutes": minutes,
    }


def trend_exits(
    close_t: np.ndarray, ema_f: np.ndarray, st: np.ndarray, side: str
) -> set[int]:
    out: set[int] = set()
    for i in range(len(close_t)):
        if math.isnan(float(ema_f[i])) or math.isnan(float(st[i])):
            continue
        hit = (
            float(ema_f[i]) < float(st[i])
            if side == "long"
            else float(ema_f[i]) > float(st[i])
        )
        if hit:
            out.add(int(close_t[i]))
    return out


def scan_path(
    path: dict[str, Any],
    tgt: float,
    sl: float,
    side: str,
    trend_set: set[int],
    spot_c: dict[int, float],
    arm: str,
) -> dict[str, Any] | None:
    legs_raw = path["legs"]
    if arm == "C1":
        legs_raw = [lg for lg in legs_raw if lg["role"] == "hi"]
    fills = [float(lg["fill"]) for lg in legs_raw]
    entry_fee = sum(float(lg["fee"]) for lg in legs_raw)
    entry_slip = sum(float(lg["slip"]) for lg in legs_raw)
    exp_ts = int(path["exp_ts"])
    exp = date.fromisoformat(str(path["exp"]))
    dte0 = int(path["dte"])
    for rec in path["minutes"]:
        t = int(rec["ts"])
        if rec.get("ok") and rec.get("pnl") is not None:
            pnl = float(rec["pnl"])
            if arm == "C1" and rec.get("px"):
                pnl = (float(rec["px"][0]) - fills[0]) * s018.QTY * OPTIONS_CONTRACT_VALUE
            if pnl >= tgt:
                return _exit_from_rec(
                    rec, fills, legs_raw, t, "TARGET", dte0, spot_c, entry_fee, entry_slip
                )
            if pnl <= -sl:
                return _exit_from_rec(
                    rec, fills, legs_raw, t, "SL", dte0, spot_c, entry_fee, entry_slip
                )
            if rec.get("tf_close") and t in trend_set:
                return _exit_from_rec(
                    rec, fills, legs_raw, t, "TREND", dte0, spot_c, entry_fee, entry_slip
                )
        if t == exp_ts:
            sp = float(spot_c.get(t, 0.0))
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


def _exit_from_rec(
    rec: dict[str, Any],
    fills: list[float],
    legs_raw: list[dict[str, Any]],
    t: int,
    reason: str,
    dte: int,
    spot_c: dict[int, float],
    entry_fee: float,
    entry_slip: float,
) -> dict[str, Any]:
    idx = float(rec.get("spot") or spot_c.get(t, 0.0))
    px = [float(x) for x in rec["px"][: len(fills)]]
    gross = 0.0
    fees = entry_fee
    slip = entry_slip
    srcs = [str(lg["src"]) for lg in legs_raw]
    for fill, mark, lg, src in zip(fills, px, legs_raw, rec["src"]):
        xf, _ = s018.sell_fill(mark, dte)
        gross += (xf - fill) * s018.QTY * OPTIONS_CONTRACT_VALUE
        fees += s018.fee_gst(mark, idx if idx else 1.0)
        slip += (mark - xf) * s018.QTY * OPTIONS_CONTRACT_VALUE
        srcs.append(str(src))
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


def signal_sets(max_n: int) -> list[tuple[int, int, int, float, str]]:
    out: list[tuple[int, int, int, float, str]] = []
    for tf in TFS:
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
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh and CKPT_A.exists():
        CKPT_A.unlink()
        print("fresh: dropped Stage A checkpoint")
    if args.fresh:
        for p in CACHE_DIR.glob("*.pkl.gz"):
            p.unlink()
        print("fresh: dropped path cache")

    done = load_ckpt(CKPT_A)
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

    sets = signal_sets(int(args.max_signal_sets) if args.max_signal_sets else 0)
    print(
        f"FULL GRID cells={N_CELLS} signal_sets={N_SIGNAL_SETS} "
        f"tgt/sl={len(TGTS)*len(SLS)} this_run_signal_sets={len(sets)}",
        flush=True,
    )

    tf_pack: dict[int, tuple] = {}
    unique: set[tuple[int, str, str]] = set()
    packed: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    n_paths = 0
    for tf, ef, es, stm, emode in sets:
        if tf not in tf_pack:
            _k, hh, ll, cc, close_t = resample_tf(ts1, o1, h1, l1, c1, tf)
            tf_pack[tf] = (hh, ll, cc, close_t)
            print(f"resample {TF_LAB[tf]} bars={len(close_t)}", flush=True)
        hh, ll, cc, close_t = tf_pack[tf]
        _tr, st = supertrend(hh, ll, cc, 1, float(stm))
        ema_f = s018.ema(cc, int(ef))
        ema_s = s018.ema(cc, int(es))
        close_set = {int(x) for x in close_t}
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
            uk = (t, side, exp.isoformat())
            unique.add(uk)
            fp = cache_file(t, side, exp, "PRIMARY")
            path = None if args.fresh else load_path(fp)
            if path is None:
                legs = pick_basket_at(store, side, t, float(sp), exp, "PRIMARY")
                if legs is None:
                    continue
                path = build_path(store, spot_c, legs, t, exp, close_set)
                if path is None:
                    continue
                save_path(fp, path)
                n_paths += 1
            trset = trend_exits(close_t, ema_f, st, side)
            entries.append(
                {
                    "i": i,
                    "ts": t,
                    "side": side,
                    "exp": exp.isoformat(),
                    "path": path,
                    "trend": trset,
                    "hod": s018.hod_ist(t),
                }
            )
        packed.append(
            {
                "tf": tf, "ef": ef, "es": es, "stm": stm, "emode": emode,
                "entries": entries, "close_t": close_t, "ema_f": ema_f, "st": st,
            }
        )
        print(
            f"signal-set TF={TF_LAB[tf]} Ef={ef} Es={es} STm={stm:g} {emode} "
            f"entries={len(entries)}",
            flush=True,
        )

    elapsed = time.perf_counter() - t0
    n_unique = len(unique)
    per = elapsed / max(n_paths, 1)
    est_full_paths = (n_unique / max(len(sets), 1)) * N_SIGNAL_SETS
    est_min = (est_full_paths * per + N_CELLS * 0.002) / 60.0
    print(
        f"UNIQUE entries (entry_ts,dir,expiry)={n_unique} new_paths={n_paths} "
        f"path_build_s={elapsed:.1f} ~{per:.3f}s/new_path",
        flush=True,
    )
    print(
        f"EST full grid (rough): ~{est_full_paths:.0f} unique-scale paths, "
        f"~{est_min:.0f} min if path-rate holds + cell scans. "
        f"NOT starting remaining {N_SIGNAL_SETS - len(sets)} signal-sets "
        f"(smoke/cap).",
        flush=True,
    )

    cell_stats: dict[str, dict[str, Any]] = dict(done)
    cell_rows: dict[str, list[dict[str, Any]]] = {}
    n_scan = 0
    for pack in packed:
        for tgt in TGTS:
            for slv in SLS:
                cell = Cell(
                    tf=int(pack["tf"]),
                    ef=int(pack["ef"]),
                    es=int(pack["es"]),
                    stm=float(pack["stm"]),
                    tgt=int(tgt),
                    sl=int(slv),
                    emode=str(pack["emode"]),
                )
                ck = cell.key()
                busy = -1
                rows: list[dict[str, Any]] = []
                if ck in done and not args.fresh:
                    cell_stats[ck] = done[ck]
                    n_scan += 1
                    continue
                for e in pack["entries"]:
                    if int(e["ts"]) <= busy:
                        continue
                    walked = scan_path(
                        e["path"], float(tgt), float(slv), e["side"],
                        e["trend"], spot_c, "PRIMARY",
                    )
                    if walked is None:
                        continue
                    rec = {
                        "entry_ts": e["ts"],
                        "side": e["side"],
                        "hod": e["hod"],
                        **walked,
                    }
                    rows.append(rec)
                    busy = int(walked["exit_ts"])
                stt = stats_of(rows)
                stt["key"] = ck
                stt["cell"] = asdict(cell)
                cell_stats[ck] = stt
                cell_rows[ck] = rows
                append_ckpt(CKPT_A, stt)
                n_scan += 1
        print(f"scanned TF={TF_LAB[pack['tf']]} {pack['emode']} cells so far={n_scan}", flush=True)

    means = [float(s["mean"]) for s in cell_stats.values() if "mean" in s]
    n40p = sum(
        1
        for s in cell_stats.values()
        if int(s.get("n", 0)) >= 40 and np.isfinite(s.get("mean", float("nan"))) and s["mean"] > 0
    )
    lines = [
        f"S018 GRID STAGE A TRAIN {TRAIN_FROM}..{TRAIN_TO}",
        f"stamp={stamp} signal_sets={len(sets)} cells_scanned={n_scan} unique_entries={n_unique}",
        f"share n>=40 & mean>0: {n40p}/{len(cell_stats)} = "
        f"{(100.0 * n40p / max(len(cell_stats), 1)):.2f}%",
        f"histogram mean net: {hist_mean(means)}",
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
        rows = cell_rows.get(str(s["key"]), [])
        lines.append("  DOW train:")
        lines.extend(dow_table(rows))

    FINALISTS_PATH.write_text(json.dumps(finalists, indent=2, default=str), encoding="utf-8")
    txt = OUT_DIR / f"s018_gridA_{stamp}.txt"
    txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {txt}")
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
    close_set = {int(x) for x in close_t}
    emode = pack["emode"]
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
            path = build_path(store, spot_c, legs, t, exp, close_set)
            if path is None:
                continue
            save_path(fp, path)
        trset = trend_exits(close_t, ema_f, st, side)
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
        close_set = {int(x) for x in close_t}
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
            "close_set": close_set,
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
                path = build_path(store, spot_c, legs, t, exp, pack["close_set"])
                if path is None:
                    continue
                save_path(fp, path)
            ent2.append(
                {
                    **e,
                    "path": path,
                    "trend": trend_exits(pack["close_t"], pack["ema_f"], pack["st"], e["side"]),
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
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.stage == "A":
        stage_a(args)
    else:
        stage_b(args)


if __name__ == "__main__":
    main()
