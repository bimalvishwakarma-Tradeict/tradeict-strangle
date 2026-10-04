#!/usr/bin/env python3
"""S018 4h trend long-option basket.

python backtest\\strategies\\s018_4h_trend\\run_s018.py --max-days 5
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE, estimate_option_fee  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, load_symbol_series, to_unix  # noqa: E402
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection  # noqa: E402
from backtest.slippage_model import load_slip_table, slip_pct  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    Bar1m,
    intrinsic,
    ist_date,
    load_spot_1m,
    option_symbol,
    supertrend,
    t_years,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s018")

SPOT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
OUT_DIR = Path("backtest/strategies/s018_4h_trend/runs")
DATA_FROM = date(2024, 9, 1)
STALE_SEC = 5 * 60
QTY = 1000
GST = 1.18
TGT = 250.0
SL = -250.0
ST_LEN, ST_MULT = 1, 1.0
EMA_FAST, EMA_SLOW = 3, 12
P_HI, P_LO = 1000.0, 300.0
TF_MIN = 240
RANDOM_SEEDS = tuple(range(20))
PERIODS = ("2024-09..12", "2025", "2026")


@dataclass
class Quote:
    ts: int
    px: float
    src: str  # real | stale


@dataclass
class Leg:
    symbol: str
    strike: float
    is_call: bool
    role: str  # hi | lo
    mark: float
    fill: float
    src: str
    delta: float
    ts: int


@dataclass
class Trade:
    arm: str
    side: str
    entry_ts: int
    exit_ts: int
    expiry: date
    reason: str
    gross: float
    fees: float
    net: float
    hold_hrs: float
    p_hi: float
    p_lo: float
    d_hi: float
    d_lo: float
    srcs: list[str]
    legs: list[Leg]
    period: str
    hod: int


def period_of(ts: int) -> str:
    d = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).date()
    if date(2024, 9, 1) <= d <= date(2024, 12, 31):
        return "2024-09..12"
    if d.year == 2025:
        return "2025"
    if d.year == 2026:
        return "2026"
    return "OUT"


def ist_str(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def hod_ist(ts: int) -> int:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).hour


def choose_expiry(t: int) -> date:
    d = ist_date(t)
    noon = to_unix(ist_dt(d, 12, 0))
    if int(t) < noon:
        return d
    return d + timedelta(days=1)


def expiry_unix(exp: date) -> int:
    return to_unix(ist_dt(exp, 17, 30))


def ema(x: np.ndarray, length: int) -> np.ndarray:
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < length:
        return out
    out[length - 1] = float(np.mean(x[:length]))
    a = 2.0 / (float(length) + 1.0)
    for i in range(length, n):
        out[i] = a * float(x[i]) + (1.0 - a) * float(out[i - 1])
    return out


def resample_4h(
    ts: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray
) -> tuple[np.ndarray, ...]:
    step = TF_MIN * 60
    buckets: dict[int, list[int]] = {}
    for i in range(len(ts)):
        key = (int(ts[i]) // step) * step
        buckets.setdefault(key, []).append(i)
    keys, oo, hh, ll, cc, close_t = [], [], [], [], [], []
    for key in sorted(buckets):
        idxs = buckets[key]
        last_need = key + step - 60
        have = {int(ts[i]) for i in idxs}
        if len(have) != TF_MIN or last_need not in have:
            continue
        keys.append(key)
        oo.append(float(o[idxs[0]]))
        hh.append(float(np.max(h[idxs])))
        ll.append(float(np.min(l[idxs])))
        cc.append(float(c[idxs[-1]]))
        close_t.append(last_need)
    return (
        np.array(keys, dtype=np.int64),
        np.array(oo, dtype=np.float64),
        np.array(hh, dtype=np.float64),
        np.array(ll, dtype=np.float64),
        np.array(cc, dtype=np.float64),
        np.array(close_t, dtype=np.int64),
    )


def bars_1m(spot: dict[int, Bar1m]) -> tuple[np.ndarray, ...]:
    ts = np.array(sorted(spot), dtype=np.int64)
    o = np.array([spot[int(t)].open for t in ts], dtype=np.float64)
    h = np.array([spot[int(t)].high for t in ts], dtype=np.float64)
    l = np.array([spot[int(t)].low for t in ts], dtype=np.float64)
    c = np.array([spot[int(t)].close for t in ts], dtype=np.float64)
    return ts, o, h, l, c


def mark_le(
    store: MarksStore, expiry: date, symbol: str, t: int
) -> Quote | None:
    """Exact (symbol, ts) then look-back <=5 min. Never look ahead."""
    conn = store.conn(expiry)
    if conn is None:
        return None
    minute = (int(t) // 60) * 60
    row = conn.execute(
        """
        SELECT ts, close FROM marks
        WHERE symbol=? AND ts=? AND close IS NOT NULL AND close>0
        """,
        (symbol, minute),
    ).fetchone()
    if row is not None:
        return Quote(ts=int(row[0]), px=float(row[1]), src="real")
    row = conn.execute(
        """
        SELECT ts, close FROM marks
        WHERE symbol=? AND ts<=? AND ts>=? AND close IS NOT NULL AND close>0
        ORDER BY ts DESC LIMIT 1
        """,
        (symbol, minute - 60, minute - STALE_SEC),
    ).fetchone()
    if row is None:
        return None
    return Quote(ts=int(row[0]), px=float(row[1]), src="stale")


def series_le(series: dict[int, float], t: int) -> Quote | None:
    minute = (int(t) // 60) * 60
    if minute in series:
        return Quote(ts=minute, px=float(series[minute]), src="real")
    for age in range(60, STALE_SEC + 1, 60):
        k = minute - age
        if k in series:
            return Quote(ts=k, px=float(series[k]), src="stale")
    return None


_CHAIN: dict[tuple[str, int], tuple[list[dict[str, Any]], str]] = {}


def load_chain(
    store: MarksStore, expiry: date, t: int
) -> tuple[list[dict[str, Any]], str] | None:
    minute = (int(t) // 60) * 60
    key = (expiry.isoformat(), minute)
    hit = _CHAIN.get(key)
    if hit is not None:
        return hit
    conn = store.conn(expiry)
    if conn is None:
        return None
    src = "real"
    rows: list[Any] = []
    for age in range(0, STALE_SEC + 1, 60):
        ts_q = minute - age
        rows = conn.execute(
            """
            SELECT symbol, strike, opt_type, close FROM marks
            WHERE expiry=? AND ts=? AND close IS NOT NULL AND close>0
            """,
            (expiry.isoformat(), ts_q),
        ).fetchall()
        if rows:
            src = "real" if age == 0 else "stale"
            break
    if not rows:
        return None
    out = [
        {
            "symbol": str(s),
            "strike": float(k),
            "is_call": str(ot).lower().startswith("c"),
            "mark": float(c),
            "src": src,
            "ts": minute if src == "real" else minute,
        }
        for s, k, ot, c in rows
    ]
    _CHAIN[key] = (out, src)
    return out, src


def nearest(rows: list[dict[str, Any]], is_call: bool, target: float) -> dict[str, Any] | None:
    cand = [r for r in rows if bool(r["is_call"]) == is_call]
    if not cand:
        return None
    return min(cand, key=lambda r: (abs(float(r["mark"]) - target), float(r["strike"])))


def fee_gst(prem: float, index: float) -> float:
    return float(estimate_option_fee(premium=prem, qty_lots=QTY, btc_index=index)) * GST


def buy_fill(mark: float, dte: int) -> tuple[float, float]:
    sf = float(slip_pct(mark, int(max(0, dte)))) / 100.0
    return float(mark) * (1.0 + sf), sf


def sell_fill(mark: float, dte: int) -> tuple[float, float]:
    sf = float(slip_pct(mark, int(max(0, dte)))) / 100.0
    return float(mark) * (1.0 - sf), sf


def signed_delta(
    mark: float, spot: float, strike: float, t_yr: float, is_call: bool
) -> float:
    iv = implied_vol_bisection(mark, spot, strike, t_yr, is_call)
    if iv is None:
        return float("nan")
    ad = black76_abs_delta(spot, strike, t_yr, iv, is_call)
    if ad is None:
        return float("nan")
    return float(ad) if is_call else -float(ad)


def pick_basket(
    store: MarksStore,
    side: str,
    t: int,
    spot: float,
    arm: str,
) -> tuple[list[Leg], date] | None:
    exp = choose_expiry(t)
    packed = load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _src = packed
    exp_ts = expiry_unix(exp)
    t_yr = t_years(t, exp_ts)
    dte = max(0, (exp - ist_date(t)).days)
    if side == "long":
        hi = nearest(rows, True, P_HI)
        lo = nearest(rows, False, P_LO)
    else:
        hi = nearest(rows, False, P_HI)
        lo = nearest(rows, True, P_LO)
    if hi is None:
        return None
    chosen = [hi] if arm == "C1" else [hi, lo]
    if any(x is None for x in chosen):
        return None
    legs: list[Leg] = []
    for i, r in enumerate(chosen):
        assert r is not None
        q = mark_le(store, exp, str(r["symbol"]), t)
        if q is None:
            return None
        fill, _ = buy_fill(q.px, dte)
        role = "hi" if i == 0 else "lo"
        dlt = signed_delta(q.px, spot, float(r["strike"]), t_yr, bool(r["is_call"]))
        legs.append(
            Leg(
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
    return legs, exp


def mark_pnl(legs: list[Leg], quotes: list[Quote]) -> float:
    pnl = 0.0
    for leg, q in zip(legs, quotes):
        pnl += (q.px - leg.fill) * QTY * OPTIONS_CONTRACT_VALUE
    return pnl


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


def _mean(xs: list[float]) -> float:
    return float(np.mean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(np.median(xs)) if xs else float("nan")


def walk_trade(
    store: MarksStore,
    spot_c: dict[int, float],
    legs: list[Leg],
    side: str,
    entry_ts: int,
    exp: date,
    close_set: set[int],
    ema3: np.ndarray,
    st: np.ndarray,
    close_to_i: dict[int, int],
    arm: str,
) -> tuple[int, str, float, float, list[str], list[tuple[Leg, float, int, str]]] | None:
    exp_ts = expiry_unix(exp)
    dte0 = max(0, (exp - ist_date(entry_ts)).days)
    series = [load_symbol_series(store, lg.symbol, entry_ts, exp_ts) for lg in legs]
    srcs = [lg.src for lg in legs]
    t = int(entry_ts) + 60
    while t <= exp_ts:
        dte = max(0, (exp - ist_date(t)).days)
        quotes: list[Quote] = []
        ok = True
        for ser in series:
            q = series_le(ser, t)
            if q is None:
                ok = False
                break
            quotes.append(q)
        if ok:
            gp = mark_pnl(legs, quotes)
            if gp >= TGT:
                return _exit_marks(
                    legs, quotes, t, "TARGET", dte, spot_c, srcs, False
                )
            if gp <= SL:
                return _exit_marks(legs, quotes, t, "SL", dte, spot_c, srcs, False)
            if t in close_set:
                i = close_to_i.get(t)
                if i is not None and not math.isnan(float(ema3[i])) and not math.isnan(float(st[i])):
                    trend_exit = (
                        float(ema3[i]) < float(st[i])
                        if side == "long"
                        else float(ema3[i]) > float(st[i])
                    )
                    if trend_exit:
                        return _exit_marks(
                            legs, quotes, t, "TREND", dte, spot_c, srcs, False
                        )
        if t == exp_ts:
            sp = spot_c.get(t)
            if sp is None:
                return None
            return _exit_settle(legs, t, float(sp), dte0, spot_c, srcs, entry_ts)
        t += 60
    return None


def _exit_marks(
    legs: list[Leg],
    quotes: list[Quote],
    t: int,
    reason: str,
    dte: int,
    spot_c: dict[int, float],
    srcs: list[str],
    settle: bool,
) -> tuple[int, str, float, float, list[str], list[tuple[Leg, float, int, str]]]:
    idx = float(spot_c.get(t, 0.0))
    gross = 0.0
    fees = 0.0
    fills: list[tuple[Leg, float, int, str]] = []
    for leg, q in zip(legs, quotes):
        xf, _ = sell_fill(q.px, dte)
        gross += (xf - leg.fill) * QTY * OPTIONS_CONTRACT_VALUE
        fees += fee_gst(leg.mark, float(spot_c.get(leg.ts, idx)))
        fees += fee_gst(q.px, idx if idx else 1.0)
        srcs.append(q.src)
        fills.append((leg, xf, q.ts, q.src))
    return t, reason, gross, fees, srcs, fills


def _exit_settle(
    legs: list[Leg],
    t: int,
    spot: float,
    dte: int,
    spot_c: dict[int, float],
    srcs: list[str],
    entry_ts: int,
) -> tuple[int, str, float, float, list[str], list[tuple[Leg, float, int, str]]]:
    idx0 = float(spot_c.get(entry_ts, spot))
    gross = 0.0
    fees = 0.0
    fills: list[tuple[Leg, float, int, str]] = []
    for leg in legs:
        px = intrinsic(leg.is_call, leg.strike, spot)
        gross += (px - leg.fill) * QTY * OPTIONS_CONTRACT_VALUE
        fees += fee_gst(leg.mark, idx0)
        if px > 0:
            fees += fee_gst(px, spot)
        srcs.append("settle")
        fills.append((leg, px, t, "settle"))
    return t, "EXPIRY", gross, fees, srcs, fills


def build_signals(
    ema3: np.ndarray, ema12: np.ndarray, st: np.ndarray
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    n = len(st)
    for i in range(1, n):
        if any(
            math.isnan(float(x[i])) or math.isnan(float(x[i - 1]))
            for x in (ema3, ema12, st)
        ):
            continue
        long_ok = (
            float(ema3[i]) > float(st[i])
            and float(st[i - 1]) <= float(ema12[i - 1])
            and float(st[i]) > float(ema12[i])
        )
        short_ok = (
            float(ema3[i]) < float(st[i])
            and float(st[i - 1]) >= float(ema12[i - 1])
            and float(st[i]) < float(ema12[i])
        )
        if long_ok:
            out.append((i, "long"))
        elif short_ok:
            out.append((i, "short"))
    return out


def simulate_arm(
    store: MarksStore,
    spot_c: dict[int, float],
    close_t: np.ndarray,
    ema3: np.ndarray,
    ema12: np.ndarray,
    st: np.ndarray,
    start_ts: int,
    cutoff: int | None,
    arm: str,
    forced: list[tuple[int, str]] | None,
) -> tuple[list[Trade], int]:
    close_set = {int(x) for x in close_t}
    close_to_i = {int(close_t[i]): i for i in range(len(close_t))}
    sigs = {i: s for i, s in build_signals(ema3, ema12, st)}
    n_sig = 0
    n_sig_per: dict[str, int] = {p: 0 for p in PERIODS}
    trades: list[Trade] = []
    busy_until = -1
    n = len(close_t)
    plan: list[tuple[int, str]]
    if forced is None:
        plan = []
        for i in range(n):
            t = int(close_t[i])
            if t < start_ts:
                continue
            if cutoff is not None and t >= cutoff:
                break
            if i in sigs:
                n_sig += 1
                per = period_of(t)
                if per in n_sig_per:
                    n_sig_per[per] += 1
                plan.append((i, sigs[i]))
    else:
        plan = list(forced)
        n_sig = len(plan)
        for i, _s in plan:
            per = period_of(int(close_t[i]))
            if per in n_sig_per:
                n_sig_per[per] += 1

    j = 0
    while j < len(plan):
        i, side = plan[j]
        t = int(close_t[i])
        if t <= busy_until:
            j += 1
            continue
        per = period_of(t)
        if per == "OUT":
            j += 1
            continue
        sp = spot_c.get(t)
        if sp is None:
            j += 1
            continue
        picked = pick_basket(store, side, t, float(sp), arm)
        if picked is None:
            j += 1
            continue
        legs, exp = picked
        walked = walk_trade(
            store, spot_c, legs, side, t, exp, close_set, ema3, st, close_to_i, arm
        )
        if walked is None:
            j += 1
            continue
        xt, reason, gross, fees, srcs, _fills = walked
        net = gross - fees
        p_hi = legs[0].mark
        p_lo = legs[1].mark if len(legs) > 1 else float("nan")
        trades.append(
            Trade(
                arm=arm, side=side, entry_ts=t, exit_ts=xt, expiry=exp, reason=reason,
                gross=gross, fees=fees, net=net,
                hold_hrs=(xt - t) / 3600.0,
                p_hi=p_hi, p_lo=p_lo, d_hi=legs[0].delta,
                d_lo=legs[1].delta if len(legs) > 1 else float("nan"),
                srcs=list(srcs), legs=legs, period=per, hod=hod_ist(t),
            )
        )
        busy_until = xt
        if reason == "TREND" and forced is None:
            ii = close_to_i.get(xt)
            if ii is not None and ii in sigs and sigs[ii] != side:
                plan.insert(j + 1, (ii, sigs[ii]))
        j += 1
    return trades, n_sig, n_sig_per


def summarize(
    tr: list[Trade], n_sig: int, ndays: int, label: str
) -> dict[str, Any]:
    n = len(tr)
    nets = [t.net for t in tr]
    reasons: dict[str, int] = defaultdict(int)
    for t in tr:
        reasons[t.reason] += 1
    srcs = [s for t in tr for s in t.srcs]
    nsrc = len(srcs) or 1
    return {
        "label": label,
        "n": n,
        "sig_day": n_sig / ndays if ndays else float("nan"),
        "win": 100.0 * sum(1 for x in nets if x > 0) / n if n else float("nan"),
        "mean": _mean(nets),
        "med": _med(nets),
        "gross": _mean([t.gross for t in tr]),
        "cost": _mean([t.fees for t in tr]),
        "reasons": dict(reasons),
        "worst": min(nets) if nets else float("nan"),
        "maxdd": max_dd(nets),
        "top5": top5_share(nets),
        "hold": _mean([t.hold_hrs for t in tr]),
        "p_hi": _mean([t.p_hi for t in tr]),
        "p_lo": _mean([t.p_lo for t in tr if t.p_lo == t.p_lo]),
        "d_hi": _mean([t.d_hi for t in tr if t.d_hi == t.d_hi]),
        "d_lo": _mean([t.d_lo for t in tr if t.d_lo == t.d_lo]),
        "real": 100.0 * srcs.count("real") / nsrc,
        "stale": 100.0 * srcs.count("stale") / nsrc,
        "settle": 100.0 * srcs.count("settle") / nsrc,
    }


def fmt(s: dict[str, Any]) -> str:
    return (
        f"n={s['n']} sig/day={s['sig_day']:.3f} win%={s['win']:.1f} "
        f"meanNet={s['mean']:.2f} medNet={s['med']:.2f} gross/t={s['gross']:.2f} "
        f"cost/t={s['cost']:.2f} worst={s['worst']:.2f} maxDD={s['maxdd']:.1f} "
        f"top5%={s['top5']:.1f} holdHrs={s['hold']:.2f} "
        f"premHi={s['p_hi']:.1f} premLo={s['p_lo']:.1f} "
        f"dHi={s['d_hi']:.3f} dLo={s['d_lo']:.3f} "
        f"src real={s['real']:.1f}% stale={s['stale']:.1f}% settle={s['settle']:.1f}% "
        f"exits={s['reasons']}"
    )


def cal_days(ts: np.ndarray, start_ts: int, cutoff: int | None, per: str) -> int:
    ds: set[date] = set()
    for t in ts:
        if int(t) < start_ts:
            continue
        if cutoff is not None and int(t) >= cutoff:
            break
        if period_of(int(t)) == per:
            ds.add(datetime.fromtimestamp(int(t), tz=UTC).astimezone(IST).date())
    return max(len(ds), 1)


def random_forced(
    real: list[Trade],
    close_t: np.ndarray,
    start_ts: int,
    cutoff: int | None,
    period: str,
    seed: int,
) -> list[tuple[int, str]]:
    need = [(t.hod, t.side) for t in real if t.period == period]
    if not need:
        return []
    by_h: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(close_t):
        tt = int(t)
        if tt < start_ts:
            continue
        if cutoff is not None and tt >= cutoff:
            continue
        if period_of(tt) != period:
            continue
        by_h[hod_ist(tt)].append(i)
    rng = np.random.default_rng(seed)
    out: list[tuple[int, str]] = []
    used: set[int] = set()
    for hod, side in need:
        pool = [i for i in by_h.get(hod, []) if i not in used]
        if not pool:
            continue
        i = int(rng.choice(pool))
        used.add(i)
        out.append((i, side))
    out.sort(key=lambda x: x[0])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=SPOT_CSV)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--max-days", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_slip_table()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("loading spot %s", args.csv)
    spot = load_spot_1m(args.csv)
    ts1, o1, h1, l1, c1 = bars_1m(spot)
    spot_c = {int(t): float(c) for t, c in zip(ts1, c1)}
    start_ts = int(datetime(2024, 9, 1, tzinfo=UTC).timestamp())
    cutoff: int | None = None
    if args.max_days:
        cutoff = start_ts + int(args.max_days) * 86400
        lo = start_ts - 10 * 86400
        hi = cutoff + 3 * 86400
        sel = (ts1 >= lo) & (ts1 < hi)
        ts1, o1, h1, l1, c1 = ts1[sel], o1[sel], h1[sel], l1[sel], c1[sel]
        spot_c = {int(t): float(c) for t, c in zip(ts1, c1)}
        print(f"SMOKE max-days={args.max_days} from={DATA_FROM} cutoff={cutoff}", flush=True)

    _o, _h, hh, ll, cc, close_t = resample_4h(ts1, o1, h1, l1, c1)
    print(f"4h complete bars={len(close_t)}", flush=True)
    trend, st = supertrend(hh, ll, cc, ST_LEN, ST_MULT)
    ema3 = ema(cc, EMA_FAST)
    ema12 = ema(cc, EMA_SLOW)
    _ = trend
    store = MarksStore()

    lines = [
        "S018 4h trend long-option basket",
        f"stamp={stamp} ST={ST_LEN}x{ST_MULT} EMA{EMA_FAST}/{EMA_SLOW} tgt/sl=±{TGT}",
        "expiry: <12:00 IST -> today 17:30; else next day. qty=1000 lots/leg",
        "fees=estimate_option_fee * 1.18 GST; slip=slip_pct; stale>5m tagged never fwd-fill",
    ]

    store_arms: dict[str, list[Trade]] = {}
    sig_n: dict[str, int] = {}
    sig_per: dict[str, dict[str, int]] = {}
    for arm in ("PRIMARY", "C1"):
        tr, ns, nsp = simulate_arm(
            store, spot_c, close_t, ema3, ema12, st, start_ts, cutoff, arm, None
        )
        store_arms[arm] = tr
        sig_n[arm] = ns
        sig_per[arm] = nsp
        print(f"{arm} trades={len(tr)} signals={ns}", flush=True)

    prim = store_arms["PRIMARY"]
    if prim:
        print("=== FIRST 5 PRIMARY strikes/premia/deltas ===")
        for t in prim[:5]:
            bits = [
                f"{lg.role} {lg.symbol} K={lg.strike:.0f} mark={lg.mark:.2f} "
                f"fill={lg.fill:.2f} d={lg.delta:.3f} src={lg.src}"
                for lg in t.legs
            ]
            print(
                f"  {t.side} {ist_str(t.entry_ts)} exp={t.expiry} " + " | ".join(bits)
            )

    if args.max_days:
        print("=== SMOKE 3 TRADES every leg price/time ===")
        for t in prim[:3]:
            print(
                f"TRADE {t.side} entry={ist_str(t.entry_ts)} exit={ist_str(t.exit_ts)} "
                f"reason={t.reason} net={t.net:.2f} gross={t.gross:.2f} fees={t.fees:.2f}"
            )
            for lg in t.legs:
                print(
                    f"  ENTRY {lg.role} {lg.symbol} ts={ist_str(lg.ts)} "
                    f"mark={lg.mark:.4f} fill={lg.fill:.4f} src={lg.src} delta={lg.delta:.4f}"
                )
            print(f"  EXIT reason={t.reason} ts={ist_str(t.exit_ts)}")
        if not prim:
            print("  (no primary trades in smoke window)")

    c2_mean: dict[str, float] = {}
    for per in PERIODS:
        real_p = [t for t in prim if t.period == per]
        if not real_p:
            c2_mean[per] = float("nan")
            continue
        means = []
        for seed in RANDOM_SEEDS:
            forced = random_forced(prim, close_t, start_ts, cutoff, per, seed)
            if not forced:
                continue
            tr, _, _ = simulate_arm(
                store, spot_c, close_t, ema3, ema12, st, start_ts, cutoff, "PRIMARY", forced
            )
            tr = [x for x in tr if x.period == per]
            means.append(_mean([x.net for x in tr]))
        c2_mean[per] = _mean([m for m in means if np.isfinite(m)]) if means else float("nan")

    days = {per: cal_days(close_t, start_ts, cutoff, per) for per in PERIODS}
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for arm, tr in store_arms.items():
        lines.append(f"ARM {arm}")
        for per in PERIODS:
            rows = [t for t in tr if t.period == per]
            sm = summarize(rows, sig_per[arm].get(per, 0), days[per], arm)
            if arm == "PRIMARY":
                sm["c2"] = c2_mean[per]
            stats[(arm, per)] = sm
            extra = ""
            if arm == "PRIMARY":
                extra = f" C2mean={c2_mean[per]:.2f}"
            lines.append(f"  {per} {fmt(sm)}{extra}")

    lines.append("")
    lines.append("--- PRE-REGISTERED PASS (PRIMARY 2025 & 2026) ---")
    ok = True
    bits = []
    for per in ("2025", "2026"):
        s = stats[("PRIMARY", per)]
        bits.append(
            f"{per} n={s['n']} mean={s['mean']} c2={c2_mean[per]} top5={s['top5']}"
        )
        if not (
            int(s["n"]) >= 30
            and np.isfinite(s["mean"])
            and s["mean"] > 0
            and np.isfinite(c2_mean[per])
            and s["mean"] > c2_mean[per]
            and np.isfinite(s["top5"])
            and s["top5"] < 100.0
        ):
            ok = False
    lines.append("PASS" if ok else "FAIL")
    lines.append("  " + " | ".join(bits))
    lines.append("C1 same windows:")
    for per in ("2025", "2026"):
        lines.append(f"  {per} {fmt(stats[('C1', per)])}")

    text = "\n".join(lines) + "\n"
    txt_path = out_dir / f"s018_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    csv_path = out_dir / f"s018_{stamp}_trades.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "arm", "period", "side", "entry_ts_ist", "exit_ts_ist", "expiry",
                "reason", "gross", "fees", "net", "hold_hrs",
                "leg0_sym", "leg0_k", "leg0_mark", "leg0_fill", "leg0_delta",
                "leg1_sym", "leg1_k", "leg1_mark", "leg1_fill", "leg1_delta",
            ],
        )
        w.writeheader()
        for t in store_arms["PRIMARY"] + store_arms["C1"]:
            l0 = t.legs[0]
            l1 = t.legs[1] if len(t.legs) > 1 else None
            w.writerow(
                {
                    "arm": t.arm,
                    "period": t.period,
                    "side": t.side,
                    "entry_ts_ist": ist_str(t.entry_ts),
                    "exit_ts_ist": ist_str(t.exit_ts),
                    "expiry": t.expiry.isoformat(),
                    "reason": t.reason,
                    "gross": t.gross,
                    "fees": t.fees,
                    "net": t.net,
                    "hold_hrs": t.hold_hrs,
                    "leg0_sym": l0.symbol,
                    "leg0_k": l0.strike,
                    "leg0_mark": l0.mark,
                    "leg0_fill": l0.fill,
                    "leg0_delta": l0.delta,
                    "leg1_sym": l1.symbol if l1 else "",
                    "leg1_k": l1.strike if l1 else "",
                    "leg1_mark": l1.mark if l1 else "",
                    "leg1_fill": l1.fill if l1 else "",
                    "leg1_delta": l1.delta if l1 else "",
                }
            )
    print(text)
    print(f"wrote {txt_path}")
    print(f"wrote {csv_path} (gitignored)")
    if args.max_days:
        print(f"SMOKE done days={args.max_days}")
    store.close()


if __name__ == "__main__":
    main()
