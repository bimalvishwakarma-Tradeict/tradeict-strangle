#!/usr/bin/env python3
"""S016 Phase 0 preflight: range formation + breakout continuation.

python backtest\\strategies\\s016_dynamic_range\\preflight.py --max-sessions 3 --fresh
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    Bar1m,
    Candle,
    build_tf,
    load_spot_1m,
)
from backtest.strategies.s012_trend_follow.run_s012 import (  # noqa: E402
    day_cluster_bootstrap,
)
from backtest.strategies.s012g_whipsaw.engine import atr_wilder  # noqa: E402
from backtest.strategies.s015_regime_map.regime_map import (  # noqa: E402
    atm_straddle,
    last_closed_1m,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s016")

SPOT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
OUT_DIR = Path("backtest/strategies/s016_dynamic_range/runs")
SESS_FROM = date(2024, 7, 1)
SESS_TO = date(2026, 9, 20)
PIVOT_K = 2
ATR_N = 14
W_ATR_LO = 0.5
W_ATR_HI = 6.0
BUF_FLOOR = 25.0
BUF_ATR = 0.20
BODY_MIN = 0.5
VOL_MIN = 1.3
DEADLINE_H, DEADLINE_M = 15, 30
EXP_H, EXP_M = 17, 30
HORIZONS_SEC = (("30m", 30 * 60), ("1h", 3600), ("2h", 7200))
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261003
RANDOM_N = 20
RANDOM_SEED = 20261003
PASS_N_SESS = 30
PASS_FORM_MED = (13, 30)

# Frozen — do not tune


@dataclass
class Swing:
    kind: str  # low | high
    pivot_i: int
    confirm_i: int
    pivot_ts: int
    confirm_ts: int
    price: float


@dataclass
class Range:
    L: float
    H: float
    W: float
    atr: float
    B: float
    form_ts: int
    form_i: int
    first: Swing
    second: Swing
    prem: float
    w_over_prem: float


@dataclass
class Event:
    session_end: str
    period: str
    event: str
    direction: str
    ts: int
    ist: str
    L: float
    H: float
    W: float
    atr: float
    B: float
    form_ts: int
    form_ist: str
    pivot1_kind: str
    pivot1_ts: int
    confirm1_ts: int
    pivot2_kind: str
    pivot2_ts: int
    confirm2_ts: int
    test_ts: int
    accept_ts: int
    spot: float
    fwd_30m_atr: float
    fwd_1h_atr: float
    fwd_2h_atr: float
    fwd_exp_atr: float
    base_30m: float
    base_1h: float
    base_2h: float
    base_exp: float
    toward_mid_30m: float
    toward_mid_1h: float
    toward_mid_2h: float
    stayed_inside: int
    prem: float
    w_over_prem: float


def period_of(d: date) -> str:
    if date(2024, 7, 1) <= d <= date(2024, 12, 31):
        return "2024H2"
    if date(2025, 1, 1) <= d <= date(2025, 12, 31):
        return "2025"
    if date(2026, 1, 1) <= d <= date(2026, 9, 20):
        return "2026"
    return "OUT"


def ist_str(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M"
    )


def ist_minutes(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST)
    return dt.hour * 60 + dt.minute


def bar_close_ts(c: Candle) -> int:
    return int(c.ts_open) + 300


def vwap_1m(spot: dict[int, Bar1m]) -> dict[int, float]:
    out: dict[int, float] = {}
    num = den = 0.0
    last_key: int | None = None
    for ts in sorted(spot):
        key = to_unix(ist_dt(datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date(), 17, 30))
        # reset at/after this day's 17:30; use previous 17:30 as session id
        d_ist = datetime.fromtimestamp(ts, tz=UTC).astimezone(IST)
        sess = to_unix(ist_dt(d_ist.date(), 17, 30))
        if ts < sess:
            sess = to_unix(ist_dt(d_ist.date() - timedelta(days=1), 17, 30))
        if last_key is None or sess != last_key:
            num = den = 0.0
            last_key = sess
        b = spot[ts]
        tp = (b.high + b.low + b.close) / 3.0
        num += tp * float(b.volume)
        den += float(b.volume)
        out[ts] = (num / den) if den > 0 else float("nan")
    return out


def vol_5m(spot: dict[int, Bar1m], candles: list[Candle]) -> np.ndarray:
    out = np.zeros(len(candles))
    for i, c in enumerate(candles):
        s = 0.0
        ts = int(c.ts_open)
        end = ts + 300
        while ts < end:
            b = spot.get(ts)
            if b is not None:
                s += float(b.volume)
            ts += 60
        out[i] = s
    return out


def vwap_at_5m(vwap: dict[int, float], c: Candle) -> float:
    last_min = int(c.ts_close_bar)
    return float(vwap.get(last_min, float("nan")))


def is_pivot(arr: np.ndarray, i: int, high: bool) -> bool:
    lo = i - PIVOT_K
    hi = i + PIVOT_K
    if lo < 0 or hi >= len(arr):
        return False
    m = float(np.max(arr[lo : hi + 1]) if high else np.min(arr[lo : hi + 1]))
    if abs(float(arr[i]) - m) > 1e-12:
        return False
    for j in range(lo, hi + 1):
        if abs(float(arr[j]) - m) <= 1e-12:
            return j == i
    return False


def _mean(xs: list[float]) -> float:
    v = [float(x) for x in xs if x is not None and np.isfinite(x)]
    return float(np.mean(v)) if v else float("nan")


def _med(xs: list[float]) -> float:
    v = [float(x) for x in xs if x is not None and np.isfinite(x)]
    return float(np.median(v)) if v else float("nan")


def _pct(xs: list[float], q: float) -> float:
    v = [float(x) for x in xs if x is not None and np.isfinite(x)]
    return float(np.percentile(v, q)) if v else float("nan")


def pct_pos(xs: list[float]) -> float:
    v = [float(x) for x in xs if np.isfinite(x)]
    if not v:
        return float("nan")
    return 100.0 * sum(1 for x in v if x > 0) / len(v)


def body_frac(c: Candle) -> float:
    rng = float(c.high) - float(c.low)
    if rng <= 1e-12:
        return 0.0
    return abs(float(c.close) - float(c.open)) / rng


def spot_at(spot: dict[int, Bar1m], t: int) -> float | None:
    b = last_closed_1m(spot, int(t))
    if b is None:
        b = spot.get((int(t) // 60) * 60)
    return None if b is None else float(b.close)


def fwd_move(
    spot: dict[int, Bar1m],
    t0: int,
    t1: int,
    direction: str,
    atr: float,
) -> float:
    if atr is None or not np.isfinite(atr) or atr <= 0:
        return float("nan")
    p0 = spot_at(spot, t0)
    p1 = spot_at(spot, t1)
    if p0 is None or p1 is None:
        return float("nan")
    raw = (p1 - p0) if direction == "up" else (p0 - p1)
    return float(raw / atr)


def toward_mid(
    spot: dict[int, Bar1m], t0: int, t1: int, mid: float, atr: float
) -> float:
    if atr is None or not np.isfinite(atr) or atr <= 0:
        return float("nan")
    p0 = spot_at(spot, t0)
    p1 = spot_at(spot, t1)
    if p0 is None or p1 is None:
        return float("nan")
    return float((abs(p0 - mid) - abs(p1 - mid)) / atr)


def find_swings(
    candles: list[Candle],
    lows: np.ndarray,
    highs: np.ndarray,
    vwap5: np.ndarray,
    i0: int,
    i1: int,
    deadline_ts: int,
) -> list[Swing]:
    """Causal: pivot at i only after i+k complete; confirm first later close vs VWAP."""
    swings: list[Swing] = []
    pending: list[tuple[int, str, float]] = []
    for i in range(i0, i1 + 1):
        if i - PIVOT_K < 0 or i + PIVOT_K > i:
            # window not complete until we reach i as the right edge
            pass
        right = i
        piv = right - PIVOT_K
        if piv - PIVOT_K >= 0 and piv >= i0 and right <= i1:
            if is_pivot(lows, piv, high=False):
                cv = float(vwap5[piv])
                if np.isfinite(cv) and float(candles[piv].close) < cv:
                    pending.append((piv, "low", float(candles[piv].low)))
            if is_pivot(highs, piv, high=True):
                cv = float(vwap5[piv])
                if np.isfinite(cv) and float(candles[piv].close) > cv:
                    pending.append((piv, "high", float(candles[piv].high)))
        still: list[tuple[int, str, float]] = []
        for piv, kind, px in pending:
            confirmed = None
            for j in range(piv + 1, i + 1):
                vw = float(vwap5[j])
                if not np.isfinite(vw):
                    continue
                cl = float(candles[j].close)
                if kind == "low" and cl > vw:
                    confirmed = j
                    break
                if kind == "high" and cl < vw:
                    confirmed = j
                    break
            if confirmed is None:
                still.append((piv, kind, px))
                continue
            cts = bar_close_ts(candles[confirmed])
            if cts > deadline_ts:
                still.append((piv, kind, px))
                continue
            swings.append(
                Swing(
                    kind=kind,
                    pivot_i=piv,
                    confirm_i=confirmed,
                    pivot_ts=bar_close_ts(candles[piv]),
                    confirm_ts=cts,
                    price=px,
                )
            )
        pending = still
    return swings


def form_range(
    swings: list[Swing],
    atr: np.ndarray,
    deadline_ts: int,
) -> Range | None:
    swings = sorted(swings, key=lambda s: (s.confirm_ts, s.confirm_i))
    i = 0
    while i < len(swings):
        first = swings[i]
        opp = None
        for s in swings:
            if s.confirm_ts <= first.confirm_ts:
                continue
            if s.kind == first.kind:
                continue
            if s.confirm_i <= first.confirm_i:
                continue
            opp = s
            break
        if opp is None:
            return None
        if first.kind == "low":
            L, H = first.price, opp.price
        else:
            H, L = first.price, opp.price
        if H <= L:
            i += 1
            continue
        W = H - L
        a = float(atr[opp.confirm_i])
        if not np.isfinite(a) or a <= 0 or not (W_ATR_LO * a <= W <= W_ATR_HI * a):
            i += 1
            continue
        if opp.confirm_ts > deadline_ts:
            return None
        B = max(BUF_FLOOR, BUF_ATR * a)
        return Range(
            L=L, H=H, W=W, atr=a, B=B, form_ts=opp.confirm_ts,
            form_i=opp.confirm_i, first=first, second=opp,
            prem=float("nan"), w_over_prem=float("nan"),
        )
    return None


def accept_1c(
    c: Candle, rng: Range, vol: np.ndarray, i: int, side: str
) -> bool:
    if i < 20:
        return False
    mv = float(np.mean(vol[i - 20 : i]))
    vr = float(vol[i]) / mv if mv > 0 else 0.0
    if body_frac(c) < BODY_MIN or vr < VOL_MIN:
        return False
    if side == "up":
        return float(c.close) > rng.H + rng.B
    return float(c.close) < rng.L - rng.B


def close_beyond(c: Candle, rng: Range, side: str) -> bool:
    if side == "up":
        return float(c.close) > rng.H + rng.B
    return float(c.close) < rng.L - rng.B


def blank_event(**kw: object) -> Event:
    base = dict(
        session_end="", period="", event="", direction="", ts=0, ist="",
        L=0.0, H=0.0, W=0.0, atr=0.0, B=0.0, form_ts=0, form_ist="",
        pivot1_kind="", pivot1_ts=0, confirm1_ts=0,
        pivot2_kind="", pivot2_ts=0, confirm2_ts=0,
        test_ts=0, accept_ts=0, spot=0.0,
        fwd_30m_atr=float("nan"), fwd_1h_atr=float("nan"),
        fwd_2h_atr=float("nan"), fwd_exp_atr=float("nan"),
        base_30m=float("nan"), base_1h=float("nan"),
        base_2h=float("nan"), base_exp=float("nan"),
        toward_mid_30m=float("nan"), toward_mid_1h=float("nan"),
        toward_mid_2h=float("nan"), stayed_inside=0,
        prem=float("nan"), w_over_prem=float("nan"),
    )
    base.update(kw)
    return Event(**base)  # type: ignore[arg-type]


def analyze_session(
    *,
    session_end: date,
    candles: list[Candle],
    lows: np.ndarray,
    highs: np.ndarray,
    atr: np.ndarray,
    vol: np.ndarray,
    vwap5: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore | None,
) -> dict:
    start = to_unix(ist_dt(session_end - timedelta(days=1), EXP_H, EXP_M))
    end = to_unix(ist_dt(session_end, EXP_H, EXP_M))
    deadline = to_unix(ist_dt(session_end, DEADLINE_H, DEADLINE_M))
    i0 = next((i for i, c in enumerate(candles) if bar_close_ts(c) > start), 0)
    i1 = 0
    for i, c in enumerate(candles):
        if bar_close_ts(c) <= end:
            i1 = i
    swings = find_swings(candles, lows, highs, vwap5, i0, i1, deadline)
    rng = form_range(swings, atr, deadline)
    period = period_of(session_end)
    out: dict = {
        "session_end": session_end.isoformat(),
        "period": period,
        "has_range": 0,
        "form_ts": 0,
        "form_min": None,
        "w_atr": float("nan"),
        "prem": float("nan"),
        "w_over_prem": float("nan"),
        "n_test": 0,
        "n_fail": 0,
        "n_1c": 0,
        "n_2c": 0,
        "no_accept": 0,
        "events": [],
        "hand": None,
    }
    if rng is None:
        return out
    out["has_range"] = 1
    out["form_ts"] = rng.form_ts
    out["form_min"] = ist_minutes(rng.form_ts)
    out["w_atr"] = rng.W / rng.atr if rng.atr else float("nan")
    px = spot_at(spot, rng.form_ts)
    if store is not None and px is not None:
        atm = atm_straddle(store, session_end, rng.form_ts, px)
        if atm is not None and atm[3] <= 5 * 60:
            rng.prem = float(atm[1] + atm[2])
            rng.w_over_prem = rng.W / rng.prem if rng.prem > 0 else float("nan")
    out["prem"] = rng.prem
    out["w_over_prem"] = rng.w_over_prem

    tests: list[int] = []
    fails: list[int] = []
    acc1: list[tuple[int, str]] = []
    acc2: list[tuple[int, str]] = []
    pending_test: int | None = None
    first_test = 0
    first_acc = 0
    got_accept = False

    for i in range(rng.form_i + 1, i1 + 1):
        c = candles[i]
        tested = float(c.high) > rng.H or float(c.low) < rng.L
        if tested:
            tests.append(i)
            if first_test == 0:
                first_test = bar_close_ts(c)
            if pending_test is None:
                pending_test = i
        if (
            pending_test is not None
            and not got_accept
            and rng.L <= float(c.close) <= rng.H
        ):
            fails.append(pending_test)
            pending_test = None
        side_1c = ""
        if accept_1c(c, rng, vol, i, "up"):
            side_1c = "up"
        elif accept_1c(c, rng, vol, i, "down"):
            side_1c = "down"
        if side_1c and not acc1:
            acc1.append((i, side_1c))
            got_accept = True
            if first_acc == 0:
                first_acc = bar_close_ts(c)
        if i > rng.form_i + 1:
            c0 = candles[i - 1]
            for side in ("up", "down"):
                if (
                    close_beyond(c0, rng, side)
                    and close_beyond(c, rng, side)
                    and accept_1c(c0, rng, vol, i - 1, side)
                    and not acc2
                ):
                    acc2.append((i, side))
                    got_accept = True
                    if first_acc == 0:
                        first_acc = bar_close_ts(c)
                    break

    out["n_test"] = len(tests)
    out["n_fail"] = len(fails)
    out["n_1c"] = len(acc1)
    out["n_2c"] = len(acc2)
    out["no_accept"] = 1 if (not acc1 and not acc2) else 0
    out["hand"] = {
        "session_end": session_end.isoformat(),
        "pivot1_kind": rng.first.kind,
        "pivot1_ts": rng.first.pivot_ts,
        "confirm1_ts": rng.first.confirm_ts,
        "pivot2_kind": rng.second.kind,
        "pivot2_ts": rng.second.pivot_ts,
        "confirm2_ts": rng.second.confirm_ts,
        "L": rng.L,
        "H": rng.H,
        "first_test_ts": first_test,
        "accept_ts": first_acc,
        "form_ist": ist_str(rng.form_ts),
    }
    common = dict(
        session_end=session_end.isoformat(),
        period=period,
        L=rng.L, H=rng.H, W=rng.W, atr=rng.atr, B=rng.B,
        form_ts=rng.form_ts, form_ist=ist_str(rng.form_ts),
        pivot1_kind=rng.first.kind, pivot1_ts=rng.first.pivot_ts,
        confirm1_ts=rng.first.confirm_ts,
        pivot2_kind=rng.second.kind, pivot2_ts=rng.second.pivot_ts,
        confirm2_ts=rng.second.confirm_ts,
        prem=rng.prem, w_over_prem=rng.w_over_prem,
    )
    events: list[Event] = []
    for i, side in acc1:
        t = bar_close_ts(candles[i])
        events.append(make_break_event("ACCEPT_1C", i, t, side, rng, candles, spot, end, **common))
    for i, side in acc2:
        t = bar_close_ts(candles[i])
        events.append(make_break_event("ACCEPT_2C", i, t, side, rng, candles, spot, end, **common))
    for i in fails:
        t = bar_close_ts(candles[i])
        events.append(make_fail_event(i, t, rng, candles, spot, end, **common))
    out["events"] = [asdict(e) for e in events]
    return out


def make_break_event(
    name: str, i: int, t: int, side: str, rng: Range,
    candles: list[Candle], spot: dict[int, Bar1m], exp_ts: int, **common: object
) -> Event:
    px = spot_at(spot, t) or 0.0
    f30 = fwd_move(spot, t, t + 1800, side, rng.atr)
    f1 = fwd_move(spot, t, t + 3600, side, rng.atr)
    f2 = fwd_move(spot, t, t + 7200, side, rng.atr)
    fe = fwd_move(spot, t, exp_ts, side, rng.atr)
    return blank_event(
        event=name, direction=side, ts=t, ist=ist_str(t),
        test_ts=t, accept_ts=t, spot=px,
        fwd_30m_atr=f30, fwd_1h_atr=f1, fwd_2h_atr=f2, fwd_exp_atr=fe,
        **common,
    )


def make_fail_event(
    i: int, t: int, rng: Range, candles: list[Candle],
    spot: dict[int, Bar1m], exp_ts: int, **common: object
) -> Event:
    mid = 0.5 * (rng.L + rng.H)
    px = spot_at(spot, t) or 0.0
    stayed = 1
    ts = t
    while ts <= exp_ts:
        p = spot_at(spot, ts)
        if p is not None and (p < rng.L - rng.B or p > rng.H + rng.B):
            stayed = 0
            break
        ts += 300
    return blank_event(
        event="FAIL", direction="", ts=t, ist=ist_str(t),
        test_ts=t, accept_ts=0, spot=px,
        toward_mid_30m=toward_mid(spot, t, t + 1800, mid, rng.atr),
        toward_mid_1h=toward_mid(spot, t, t + 3600, mid, rng.atr),
        toward_mid_2h=toward_mid(spot, t, t + 7200, mid, rng.atr),
        stayed_inside=stayed,
        **common,
    )


def attach_baselines(
    events: list[dict],
    pool: dict[str, list[tuple[int, str]]],
    spot: dict[int, Bar1m],
    rng_atr: dict[str, float],
    exp_of: dict[str, int],
) -> None:
    rng = np.random.default_rng(RANDOM_SEED)
    for ev in events:
        if ev["event"] not in {"ACCEPT_1C", "ACCEPT_2C"}:
            continue
        sess = str(ev["session_end"])
        m0 = ist_minutes(int(ev["ts"]))
        cands: list[tuple[int, str]] = []
        for other, rows in pool.items():
            if other == sess:
                continue
            for ts, sid in rows:
                if abs(ist_minutes(ts) - m0) <= 30:
                    cands.append((ts, other))
        atr = float(ev["atr"])
        side = str(ev["direction"])
        t0 = int(ev["ts"])
        if not cands:
            continue
        take = cands if len(cands) <= RANDOM_N else [
            cands[j] for j in rng.choice(len(cands), size=RANDOM_N, replace=False)
        ]
        def mean_fwd(dt: int | None, exp_other: bool) -> float:
            xs = []
            for ts, osess in take:
                t1 = int(exp_of[osess]) if exp_other else int(ts) + int(dt or 0)
                xs.append(fwd_move(spot, int(ts), t1, side, atr))
            return _mean(xs)
        ev["base_30m"] = mean_fwd(1800, False)
        ev["base_1h"] = mean_fwd(3600, False)
        ev["base_2h"] = mean_fwd(7200, False)
        ev["base_exp"] = mean_fwd(None, True)


def report(sessions: list[dict], events: list[dict], stamp: str, out_dir: Path) -> str:
    lines = [f"S016 Phase 0 PREFLIGHT stamp={stamp}", ""]
    for per in ("2024H2", "2025", "2026"):
        ss = [s for s in sessions if s["period"] == per]
        n = len(ss)
        nr = sum(s["has_range"] for s in ss)
        fmins = [s["form_min"] for s in ss if s["has_range"] and s["form_min"] is not None]
        watr = [s["w_atr"] for s in ss if s["has_range"]]
        prem = [s["prem"] for s in ss if s["has_range"]]
        wop = [s["w_over_prem"] for s in ss if s["has_range"]]
        lines.append(f"=== {per} sessions={n} ===")
        lines.append(
            f"A valid_range%={100.0 * nr / n if n else float('nan'):.2f} n_range={nr}"
        )
        def hm(m: float) -> str:
            if m is None or not np.isfinite(m):
                return "nan"
            m = int(round(m))
            return f"{m // 60:02d}:{m % 60:02d}"
        lines.append(
            f"  form_IST median={hm(_med(fmins))} p25={hm(_pct(fmins, 25))} "
            f"p75={hm(_pct(fmins, 75))}"
        )
        lines.append(
            f"  W/ATR mean={_mean(watr):.4f} med={_med(watr):.4f} "
            f"straddle_prem mean={_mean(prem):.2f} W/prem mean={_mean(wop):.4f}"
        )
        lines.append(
            f"B tests={sum(s['n_test'] for s in ss)} failed={sum(s['n_fail'] for s in ss)} "
            f"acc_1C={sum(s['n_1c'] for s in ss)} acc_2C={sum(s['n_2c'] for s in ss)}"
        )
        evp = [e for e in events if e["period"] == per]
        for kind, tag in (("ACCEPT_1C", "1C"), ("ACCEPT_2C", "2C")):
            ee = [e for e in evp if e["event"] == kind]
            nsess = len({e["session_end"] for e in ee})
            lines.append(f"C accepted {tag} events={len(ee)} sessions={nsess}")
            for fld, lab in (
                ("fwd_30m_atr", "+30m"),
                ("fwd_1h_atr", "+1h"),
                ("fwd_2h_atr", "+2h"),
                ("fwd_exp_atr", "17:30"),
            ):
                xs = [float(e[fld]) for e in ee]
                bf = "base_" + fld.split("fwd_")[1] if fld.startswith("fwd_") else ""
                if fld == "fwd_30m_atr":
                    bf = "base_30m"
                elif fld == "fwd_1h_atr":
                    bf = "base_1h"
                elif fld == "fwd_2h_atr":
                    bf = "base_2h"
                else:
                    bf = "base_exp"
                diff = [
                    SimpleNamespace(
                        day=date.fromisoformat(e["session_end"]),
                        skip="",
                        net=float(e[fld]) - float(e[bf])
                        if np.isfinite(e[fld]) and np.isfinite(e[bf])
                        else float("nan"),
                    )
                    for e in ee
                    if np.isfinite(float(e[fld])) and np.isfinite(float(e.get(bf, float("nan"))))
                ]
                diff = [d for d in diff if np.isfinite(d.net)]
                _, lo, hi = (
                    day_cluster_bootstrap(diff, BOOTSTRAP_N, BOOTSTRAP_SEED)  # type: ignore[arg-type]
                    if diff
                    else (float("nan"), float("nan"), float("nan"))
                )
                lines.append(
                    f"  {lab} mean={_mean(xs):.4f} med={_med(xs):.4f} "
                    f"%pos={pct_pos(xs):.1f} base={_mean([float(e[bf]) for e in ee]):.4f} "
                    f"diff_CI=[{lo:.4f},{hi:.4f}]"
                )
        ff = [e for e in evp if e["event"] == "FAIL"]
        fsess = [s for s in ss if s["n_fail"] > 0]
        stay = [e["stayed_inside"] for e in ff]
        lines.append(
            f"D failed events={len(ff)} toward_mid +30m={_mean([float(e['toward_mid_30m']) for e in ff]):.4f} "
            f"+1h={_mean([float(e['toward_mid_1h']) for e in ff]):.4f} "
            f"+2h={_mean([float(e['toward_mid_2h']) for e in ff]):.4f} "
            f"%stay_inside={100.0 * _mean([float(x) for x in stay]) if stay else float('nan'):.1f} "
            f"(n_fail_events={len(ff)})"
        )
        nrr = sum(s["has_range"] for s in ss)
        n_no = sum(s["no_accept"] for s in ss if s["has_range"])
        lines.append(
            f"E after range, no accepted BO until 17:30: "
            f"{100.0 * n_no / nrr if nrr else float('nan'):.2f}% ({n_no}/{nrr})"
        )
        lines.append("")

    form_all = [s["form_min"] for s in sessions if s["has_range"] and s.get("form_min") is not None]
    med_all = _med(form_all)
    lines.append(f"overall median formation IST minutes={med_all} -> {int(med_all)//60:02d}:{int(med_all)%60:02d}" if np.isfinite(med_all) else "overall median formation=nan")

    def pass_kind(kind: str) -> str:
        bits = [f"--- PRE-REGISTERED {kind} ---"]
        ok = True
        form_pass = [s["form_min"] for s in sessions if s["period"] in {"2025", "2026"} and s["has_range"] and s.get("form_min") is not None]
        med = _med(form_pass)
        bits.append(f"median_formation_2025+2026={med} cutoff=13:30")
        if not (np.isfinite(med) and med <= PASS_FORM_MED[0] * 60 + PASS_FORM_MED[1]):
            ok = False
        for per in ("2025", "2026"):
            ee = [e for e in events if e["period"] == per and e["event"] == kind]
            nsess = len({e["session_end"] for e in ee})
            diffs = [
                SimpleNamespace(
                    day=date.fromisoformat(e["session_end"]),
                    skip="",
                    net=float(e["fwd_1h_atr"]) - float(e["base_1h"]),
                )
                for e in ee
                if np.isfinite(float(e["fwd_1h_atr"])) and np.isfinite(float(e.get("base_1h", float("nan"))))
            ]
            mu = _mean([d.net for d in diffs])
            _, lo, hi = (
                day_cluster_bootstrap(diffs, BOOTSTRAP_N, BOOTSTRAP_SEED)  # type: ignore[arg-type]
                if diffs
                else (float("nan"), float("nan"), float("nan"))
            )
            bits.append(
                f"{per} n_sessions={nsess} n_events={len(ee)} "
                f"diff_1h_mean={mu} CI=[{lo},{hi}]"
            )
            if not (nsess >= PASS_N_SESS and np.isfinite(mu) and mu > 0 and np.isfinite(lo) and lo > 0):
                ok = False
        ee24 = [e for e in events if e["period"] == "2024H2" and e["event"] == kind]
        bits.append(f"2024H2 extra n_sessions={len({e['session_end'] for e in ee24})} n_events={len(ee24)} (not in PASS)")
        bits.append("PASS" if ok else "FAIL")
        return "\n".join(bits)

    lines.append("")
    lines.append(pass_kind("ACCEPT_1C"))
    lines.append("")
    lines.append(pass_kind("ACCEPT_2C"))
    lines.append("")
    lines.append("=== HAND-CHECK 3 sessions ===")
    shown = 0
    for s in sessions:
        if s.get("hand") and s["has_range"]:
            h = s["hand"]
            lines.append(
                f"  {h['session_end']} p1={h['pivot1_kind']} pivot={ist_str(h['pivot1_ts'])} "
                f"conf={ist_str(h['confirm1_ts'])} p2={h['pivot2_kind']} "
                f"pivot={ist_str(h['pivot2_ts'])} conf={ist_str(h['confirm2_ts'])} "
                f"L={h['L']:.2f} H={h['H']:.2f} first_test={ist_str(h['first_test_ts']) if h['first_test_ts'] else 'none'} "
                f"accept={ist_str(h['accept_ts']) if h['accept_ts'] else 'none'}"
            )
            shown += 1
            if shown >= 3:
                break
    if shown == 0:
        lines.append("  (no ranged sessions in this run)")
    text = "\n".join(lines) + "\n"
    (out_dir / f"s016_preflight_{stamp}.txt").write_text(text, encoding="utf-8")
    csv_path = out_dir / f"s016_preflight_{stamp}_events.csv"
    if events:
        fields = list(events[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for e in events:
                w.writerow(e)
    return text


def session_dates(max_n: int) -> list[date]:
    out: list[date] = []
    d = SESS_FROM
    while d <= SESS_TO:
        out.append(d)
        if max_n and len(out) >= max_n:
            break
        d += timedelta(days=1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=SPOT_CSV)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--max-sessions", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"max{args.max_sessions}" if args.max_sessions else "full"
    ckpt = out_dir / f"s016_preflight_cache_{tag}.jsonl"
    if args.fresh and ckpt.exists():
        ckpt.unlink()

    t0 = time.perf_counter()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logger.info("loading spot")
    spot = load_spot_1m(args.csv)
    logger.info("VWAP 1m session 17:30 IST")
    vw = vwap_1m(spot)
    logger.info("5m UTC bars + ATR")
    c5 = build_tf(spot, 5)
    h = np.array([x.high for x in c5], dtype=np.float64)
    l = np.array([x.low for x in c5], dtype=np.float64)
    cl = np.array([x.close for x in c5], dtype=np.float64)
    atr = atr_wilder(h, l, cl, ATR_N)
    vol = vol_5m(spot, c5)
    vwap5 = np.array([vwap_at_5m(vw, c) for c in c5], dtype=np.float64)

    dates = session_dates(args.max_sessions)
    print(f"S016 planned_sessions={len(dates)} first={dates[0]} last={dates[-1]}", flush=True)
    done: set[str] = set()
    sessions: list[dict] = []
    if ckpt.exists():
        with ckpt.open(encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                done.add(obj["session_end"])
                sessions.append(obj)
        logger.info("resume %s", len(done))

    store = MarksStore()
    ntot = len(dates)
    try:
        with ckpt.open("a", encoding="utf-8") as cf:
            for n, d in enumerate(dates, start=1):
                if d.isoformat() in done:
                    continue
                rec = analyze_session(
                    session_end=d, candles=c5, lows=l, highs=h, atr=atr,
                    vol=vol, vwap5=vwap5, spot=spot, store=store,
                )
                sessions.append(rec)
                cf.write(json.dumps(rec) + "\n")
                cf.flush()
                if n == 1 or n % max(1, ntot // 50) == 0 or n == ntot:
                    elapsed = time.perf_counter() - t0
                    eta = elapsed / n * (ntot - n)
                    logger.info(
                        "progress %.1f%% n=%s/%s range=%s eta_sec=%.0f",
                        100.0 * n / ntot, n, ntot, rec["has_range"], eta,
                    )
    finally:
        store.close()

    events: list[dict] = []
    for s in sessions:
        events.extend(s.get("events") or [])
    pool: dict[str, list[tuple[int, str]]] = defaultdict(list)
    exp_of: dict[str, int] = {}
    for d in dates:
        sid = d.isoformat()
        start = to_unix(ist_dt(d - timedelta(days=1), EXP_H, EXP_M))
        end = to_unix(ist_dt(d, EXP_H, EXP_M))
        exp_of[sid] = end
        for c in c5:
            t = bar_close_ts(c)
            if start < t <= end:
                pool[sid].append((t, sid))
    atr_map = {s["session_end"]: float(s.get("w_atr") or 0) for s in sessions}
    attach_baselines(events, pool, spot, atr_map, exp_of)
    text = report(sessions, events, stamp, out_dir)
    print(text)
    if args.max_sessions:
        print(f"SMOKE max-sessions={args.max_sessions} n={len(sessions)}")


if __name__ == "__main__":
    main()
