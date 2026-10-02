#!/usr/bin/env python3
"""S012 engine-lite: Supertrend LP signal + real option-mark basket.

No P&L invention: missing marks are tagged, never forward-filled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

import numpy as np

from backtest.harness.costs import fill_price, option_fee, qty_btc, signed_pnl
from backtest.harness.data import MarksStore, ist_dt, to_unix
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection
from backtest.strategies.s012_trend_follow import config as cfg

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
Side = Literal["long", "short"]


@dataclass
class Bar1m:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Candle:
    ts_open: int
    ts_close_bar: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class Signal:
    side: Side
    index: int
    signal_ts: int
    entry_ts: int
    spot_entry: float
    st_value: float
    r: float
    day: date
    weekday: int


@dataclass
class Trade:
    side: str
    tf: int
    st_mult: float
    tgt: float
    kind: str  # signal | random
    entry_ts: int
    exit_ts: int
    day: date
    half: str
    dte: str
    net: float
    gross: float
    fees: float
    slippage: float
    hold_min: float
    win: int
    target_hit: int
    weekend: int
    mark_missing: int
    skip: str = ""


def ist_date(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).date()


def half_of(d: date) -> str:
    if cfg.H1_FROM <= d <= cfg.H1_TO:
        return "H1"
    if cfg.H2_FROM <= d <= cfg.H2_TO:
        return "H2"
    return "OUT"


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    n = len(c)
    tr = np.empty(n, dtype=np.float64)
    tr[0] = float(h[0] - l[0])
    for i in range(1, n):
        tr[i] = max(
            float(h[i] - l[i]),
            abs(float(h[i] - c[i - 1])),
            abs(float(l[i] - c[i - 1])),
        )
    return tr


def rma(x: np.ndarray, length: int) -> np.ndarray:
    """TradingView RMA: SMA seed, then (prev*(n-1)+x)/n."""
    n = len(x)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < length or length < 1:
        return out
    out[length - 1] = float(np.mean(x[:length]))
    k = float(length)
    for i in range(length, n):
        out[i] = (out[i - 1] * (k - 1.0) + float(x[i])) / k
    return out


def supertrend(
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    length: int,
    mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    """TradingView Supertrend. trend +1 = UP, -1 = DOWN. Flip on close."""
    atr = rma(true_range(h, l, c), length)
    src = (h + l) * 0.5
    n = len(c)
    up = src - mult * atr
    dn = src + mult * atr
    fu = np.copy(up)
    fd = np.copy(dn)
    trend = np.zeros(n, dtype=np.int8)
    st = np.full(n, np.nan, dtype=np.float64)
    started = False
    for i in range(n):
        if math.isnan(float(atr[i])):
            continue
        if not started:
            fu[i] = float(up[i])
            fd[i] = float(dn[i])
            trend[i] = 1
            st[i] = fu[i]
            started = True
            continue
        up1 = float(fu[i - 1])
        dn1 = float(fd[i - 1])
        ui = float(up[i])
        di = float(dn[i])
        fu[i] = max(ui, up1) if float(c[i - 1]) > up1 else ui
        fd[i] = min(di, dn1) if float(c[i - 1]) < dn1 else di
        prev = int(trend[i - 1])
        if prev == 0:
            prev = 1
        cl = float(c[i])
        if prev == -1 and cl > dn1:
            trend[i] = 1
        elif prev == 1 and cl < up1:
            trend[i] = -1
        else:
            trend[i] = prev
        st[i] = float(fu[i] if trend[i] == 1 else fd[i])
    return trend, st


def load_spot_1m(csv_path: Any) -> dict[int, Bar1m]:
    import csv

    out: dict[int, Bar1m] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            vk = next(k for k in row if k.lower() == "volume")
            out[ts] = Bar1m(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row[vk]),
            )
    return out


def build_tf(spot: dict[int, Bar1m], tf_min: int) -> list[Candle]:
    step = int(tf_min) * 60
    buckets: dict[int, list[Bar1m]] = {}
    for ts, bar in spot.items():
        key = (int(ts) // step) * step
        buckets.setdefault(key, []).append(bar)
    candles: list[Candle] = []
    for ts_open in sorted(buckets):
        bars = sorted(buckets[ts_open], key=lambda b: b.ts)
        candles.append(
            Candle(
                ts_open=ts_open,
                ts_close_bar=bars[-1].ts,
                open=bars[0].open,
                high=max(b.high for b in bars),
                low=min(b.low for b in bars),
                close=bars[-1].close,
            )
        )
    return candles


def _green(c: Candle) -> bool:
    return c.close > c.open


def _red(c: Candle) -> bool:
    return c.close < c.open


def detect_signals(
    candles: list[Candle],
    trend: np.ndarray,
    st: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
    cutoff_ts: int | None,
) -> tuple[list[Signal], np.ndarray]:
    """Most-recent LP only. lp_active[i]=1 while a live LP is being tracked."""
    n = len(candles)
    lp_active = np.zeros(n, dtype=np.int8)
    signals: list[Signal] = []

    # LP tracker: None or dict
    lp: dict[str, Any] | None = None

    def cancel() -> None:
        nonlocal lp
        lp = None

    for i in range(n):
        c = candles[i]
        if cutoff_ts is not None and c.ts_open >= cutoff_ts:
            break
        td = int(trend[i])
        if td == 0 or math.isnan(float(st[i])):
            continue

        # Form new LP from candles i-2,i-1,i (most recent wins)
        if i >= 2:
            a, b, d = candles[i - 2], candles[i - 1], candles[i]
            flipped = [int(trend[j]) for j in (i - 2, i - 1, i)]
            prevs = []
            for j in (i - 2, i - 1, i):
                prevs.append(int(trend[j - 1]) if j > 0 else 0)
            up_flip = any(
                flipped[k] == 1 and prevs[k] == -1 for k in range(3)
            )
            dn_flip = any(
                flipped[k] == -1 and prevs[k] == 1 for k in range(3)
            )
            if (
                _green(a)
                and _green(b)
                and _green(d)
                and a.close < b.close < d.close
                and up_flip
            ):
                lp = {
                    "side": "long",
                    "end": i,
                    "lph": max(a.high, b.high, d.high),
                    "lpl": min(a.low, b.low, d.low),
                    "phase": 0,
                }
            elif (
                _red(a)
                and _red(b)
                and _red(d)
                and a.close > b.close > d.close
                and dn_flip
            ):
                lp = {
                    "side": "short",
                    "end": i,
                    "lph": max(a.high, b.high, d.high),
                    "lpl": min(a.low, b.low, d.low),
                    "phase": 0,
                }

        if lp is None:
            continue
        lp_active[i] = 1
        if i <= int(lp["end"]):
            continue  # "later" candles only
        side = lp["side"]
        lph = float(lp["lph"])
        lpl = float(lp["lpl"])
        if side == "long":
            if td == -1 or c.close < lpl:
                cancel()
                continue
            ph = int(lp["phase"])
            if ph == 0 and c.high > lph:
                lp["phase"] = 1
            elif ph == 1 and lpl < c.close < lph:
                lp["phase"] = 2
            elif ph == 2 and c.close > lph:
                sig = _make_signal("long", i, c, st, spot, start_ts)
                if sig is not None:
                    signals.append(sig)
                cancel()
        else:
            if td == 1 or c.close > lph:
                cancel()
                continue
            ph = int(lp["phase"])
            if ph == 0 and c.low < lpl:
                lp["phase"] = 1
            elif ph == 1 and lpl < c.close < lph:
                lp["phase"] = 2
            elif ph == 2 and c.close < lpl:
                sig = _make_signal("short", i, c, st, spot, start_ts)
                if sig is not None:
                    signals.append(sig)
                cancel()
    return signals, lp_active


def _make_signal(
    side: Side,
    i: int,
    c: Candle,
    st: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
) -> Signal | None:
    if c.ts_close_bar < start_ts:
        return None
    entry_ts = int(c.ts_close_bar) + 60
    bar = spot.get(entry_ts)
    if bar is None:
        return None
    day = ist_date(c.ts_close_bar)
    wd = datetime.fromtimestamp(int(c.ts_close_bar), tz=UTC).astimezone(IST).weekday()
    if wd not in cfg.WEEKDAYS_OK:
        return None
    if half_of(day) == "OUT":
        return None
    stv = float(st[i])
    spot_e = float(bar.close)
    r = abs(spot_e - stv)
    if r <= 0:
        return None
    return Signal(
        side=side,
        index=i,
        signal_ts=int(c.ts_close_bar),
        entry_ts=entry_ts,
        spot_entry=spot_e,
        st_value=stv,
        r=r,
        day=day,
        weekday=wd,
    )


def lookahead_ok(
    candles: list[Candle],
    trend: np.ndarray,
    st: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
    sig: Signal,
) -> bool:
    sub_c = candles[: sig.index + 1]
    sub_t = trend[: sig.index + 1]
    sub_s = st[: sig.index + 1]
    found, _ = detect_signals(sub_c, sub_t, sub_s, spot, start_ts, None)
    if not found:
        return False
    last = found[-1]
    return last.index == sig.index and last.side == sig.side


def t_years(entry_ts: int, expiry_ts: int) -> float:
    return max((int(expiry_ts) - int(entry_ts)) / cfg.SECONDS_PER_YEAR, 1e-12)


def option_symbol(is_call: bool, strike: float, expiry: date) -> str:
    prefix = "C" if is_call else "P"
    return f"{prefix}-BTC-{int(strike)}-{expiry.strftime('%d%m%y')}"


def pick_delta(
    legs: dict[float, float],
    spot: float,
    t_yr: float,
    target: float,
    is_call: bool,
    expiry: date,
    minute: int,
) -> tuple[float, float] | None:
    best: tuple[float, float] | None = None
    best_err = 1e9
    for k, mark in legs.items():
        iv, dlt = iv_delta(option_symbol(is_call, k, expiry), minute, mark, spot, k, t_yr, is_call)
        if iv is None or dlt is None:
            continue
        err = abs(dlt - target)
        if err < best_err:
            best_err = err
            best = (k, mark)
    return best


def iv_delta(
    symbol: str,
    minute: int,
    mark: float,
    spot: float,
    strike: float,
    t_yr: float,
    is_call: bool,
) -> tuple[float | None, float | None]:
    """Cache IV + abs delta by (symbol, minute); recompute if spot/t differ."""
    ck = (symbol, int(minute))
    hit = _IV.get(ck)
    if (
        hit is not None
        and hit[0] == float(spot)
        and hit[1] == float(t_yr)
        and hit[2] == is_call
        and hit[3] == float(mark)
    ):
        return hit[4], hit[5]
    iv = implied_vol_bisection(mark, spot, strike, t_yr, is_call)
    dlt = (
        black76_abs_delta(spot, strike, t_yr, iv, is_call) if iv is not None else None
    )
    _IV[ck] = (float(spot), float(t_yr), is_call, float(mark), iv, dlt)
    return iv, dlt


@dataclass
class _Basket:
    skip: str
    miss_entry: int
    exp_d: date | None
    exp_ts: int
    dte_lab: str
    a_k: float
    a_m: float
    w_k: float
    w_m: float
    a_call: bool
    w_call: bool
    a_fill: float
    w_fill: float
    fees: float
    slip: float


_BASKET: dict[tuple[int, str], _Basket] = {}
_CHAIN: dict[tuple[str, int, int], tuple[dict[float, float], dict[float, float]]] = {}
_MARK: dict[tuple[str, int], float | None] = {}  # (symbol, minute)
_IV: dict[tuple[str, int], tuple[float, float, bool, float, float | None, float | None]] = {}
_STRIKES: dict[str, list[tuple[float, bool]]] = {}


def expiry_strikes(store: MarksStore, expiry: date) -> list[tuple[float, bool]]:
    """DISTINCT strike, opt_type for this contract-expiry month. Cached."""
    key = expiry.isoformat()
    hit = _STRIKES.get(key)
    if hit is not None:
        return hit
    conn = store.conn(expiry)
    if conn is None:
        _STRIKES[key] = []
        return []
    rows = conn.execute(
        "SELECT DISTINCT strike, opt_type FROM marks WHERE expiry=?",
        (key,),
    ).fetchall()
    out: list[tuple[float, bool]] = []
    for strike, opt in rows:
        out.append((float(strike), str(opt).lower().startswith("c")))
    _STRIKES[key] = out
    return out


def mark_by_symbol(
    store: MarksStore, expiry: date, symbol: str, minute: int
) -> float | None:
    ck = (symbol, int(minute))
    if ck in _MARK:
        return _MARK[ck]
    conn = store.conn(expiry)
    if conn is None:
        _MARK[ck] = None
        return None
    tol = cfg.MARK_TOL_SEC
    rows = conn.execute(
        """
        SELECT ts, close FROM marks
        WHERE symbol=? AND ts BETWEEN ? AND ?
          AND close IS NOT NULL AND close > 0
        """,
        (symbol, int(minute) - tol, int(minute) + tol),
    ).fetchall()
    best: float | None = None
    best_ad = tol + 1
    best_ts = 10**18
    for ts_m, close in rows:
        ad = abs(int(ts_m) - int(minute))
        ts_i = int(ts_m)
        if ad < best_ad or (ad == best_ad and ts_i < best_ts):
            best_ad = ad
            best_ts = ts_i
            best = float(close)
    _MARK[ck] = best
    return best


def load_chain(
    store: MarksStore, expiry: date, ts: int, spot: float
) -> tuple[dict[float, float], dict[float, float]]:
    minute = (int(ts) // 60) * 60
    key = (expiry.isoformat(), minute, int(round(spot)))
    hit = _CHAIN.get(key)
    if hit is not None:
        return hit
    conn = store.conn(expiry)
    if conn is None:
        _CHAIN[key] = ({}, {})
        return {}, {}
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    band = float(cfg.STRIKE_BAND)
    for strike, is_call in expiry_strikes(store, expiry):
        if abs(strike - float(spot)) > band:
            continue
        sym = option_symbol(is_call, strike, expiry)
        px = mark_by_symbol(store, expiry, sym, minute)
        if px is None:
            continue
        if is_call:
            calls[strike] = px
        else:
            puts[strike] = px
    _CHAIN[key] = (calls, puts)
    return calls, puts


def expiry_choice(
    store: MarksStore, entry_ts: int, spot: float, side: Side
) -> tuple[date, int, str] | None:
    """0DTE if still before 17:30 IST and Leg-1 ATM mark > 200; else 1DTE."""
    d0 = ist_date(entry_ts)
    e0 = to_unix(ist_dt(d0, cfg.EXPIRY_HOUR_IST, cfg.EXPIRY_MINUTE_IST))
    minute = (int(entry_ts) // 60) * 60
    if e0 > entry_ts:
        calls, puts = load_chain(store, d0, entry_ts, spot)
        t0 = t_years(entry_ts, e0)
        if side == "long":
            atm = pick_delta(calls, spot, t0, cfg.ATM_DELTA, True, d0, minute)
        else:
            atm = pick_delta(puts, spot, t0, cfg.ATM_DELTA, False, d0, minute)
        if atm is not None and atm[1] > cfg.ATM_MARK_0DTE_MIN:
            return d0, e0, "0DTE"
    d1 = d0 + timedelta(days=1)
    e1 = to_unix(ist_dt(d1, cfg.EXPIRY_HOUR_IST, cfg.EXPIRY_MINUTE_IST))
    if e1 <= entry_ts:
        return None
    return d1, e1, "1DTE"


def mark_at(
    store: MarksStore, expiry: date, is_call: bool, strike: float, ts: int
) -> float | None:
    minute = (int(ts) // 60) * 60
    return mark_by_symbol(
        store, expiry, option_symbol(is_call, strike, expiry), minute
    )


def intrinsic(is_call: bool, strike: float, spot: float) -> float:
    if is_call:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def next_1m(spot: dict[int, Bar1m], ts: int) -> int | None:
    nxt = int(ts) + 60
    return nxt if nxt in spot else None


def weekend_held(entry_ts: int, exit_ts: int) -> int:
    d0 = ist_date(entry_ts)
    d1 = ist_date(exit_ts)
    d = d0
    while d <= d1:
        if d.weekday() >= 5:
            return 1
        d += timedelta(days=1)
    return 0


def resolve_basket(store: MarksStore, sig: Signal) -> _Basket:
    key = (int(sig.entry_ts), str(sig.side))
    cached = _BASKET.get(key)
    if cached is not None:
        return cached
    exp = expiry_choice(store, sig.entry_ts, sig.spot_entry, sig.side)
    if exp is None:
        b = _Basket(
            skip="no_expiry", miss_entry=1, exp_d=None, exp_ts=0, dte_lab="",
            a_k=0, a_m=0, w_k=0, w_m=0, a_call=True, w_call=False,
            a_fill=0, w_fill=0, fees=0, slip=0,
        )
        _BASKET[key] = b
        return b
    exp_d, exp_ts, dte_lab = exp
    minute = (int(sig.entry_ts) // 60) * 60
    calls, puts = load_chain(store, exp_d, sig.entry_ts, sig.spot_entry)
    t_yr = t_years(sig.entry_ts, exp_ts)
    if sig.side == "long":
        a = pick_delta(calls, sig.spot_entry, t_yr, cfg.ATM_DELTA, True, exp_d, minute)
        w = pick_delta(puts, sig.spot_entry, t_yr, cfg.WING_DELTA, False, exp_d, minute)
        a_call, w_call = True, False
    else:
        a = pick_delta(puts, sig.spot_entry, t_yr, cfg.ATM_DELTA, False, exp_d, minute)
        w = pick_delta(calls, sig.spot_entry, t_yr, cfg.WING_DELTA, True, exp_d, minute)
        a_call, w_call = False, True
    if a is None or w is None:
        b = _Basket(
            skip="mark_missing", miss_entry=1, exp_d=exp_d, exp_ts=exp_ts,
            dte_lab=dte_lab, a_k=0, a_m=0, w_k=0, w_m=0,
            a_call=a_call, w_call=w_call, a_fill=0, w_fill=0, fees=0, slip=0,
        )
        _BASKET[key] = b
        return b
    a_k, a_m = a
    w_k, w_m = w
    dte_slip = 0 if dte_lab == "0DTE" else 1
    a_fill, _ = fill_price(a_m, "buy", dte=dte_slip)
    w_fill, _ = fill_price(w_m, "buy", dte=dte_slip)
    q = cfg.QTY
    fees = option_fee(a_fill, sig.spot_entry, q) + option_fee(
        w_fill, sig.spot_entry, q
    )
    slip = abs(a_fill - a_m) * qty_btc(q) + abs(w_fill - w_m) * qty_btc(q)
    b = _Basket(
        skip="", miss_entry=0, exp_d=exp_d, exp_ts=exp_ts, dte_lab=dte_lab,
        a_k=a_k, a_m=a_m, w_k=w_k, w_m=w_m, a_call=a_call, w_call=w_call,
        a_fill=a_fill, w_fill=w_fill, fees=fees, slip=slip,
    )
    _BASKET[key] = b
    return b


def simulate_trade(
    *,
    sig: Signal,
    candles: list[Candle],
    trend: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
    tf_min: int,
    st_mult: float,
    tgt: float,
    kind: str,
) -> Trade:
    day = sig.day
    half = half_of(day)
    miss = 0
    bsk = resolve_basket(store, sig)
    if bsk.skip:
        return Trade(
            sig.side, tf_min, st_mult, tgt, kind, sig.entry_ts, sig.entry_ts,
            day, half, bsk.dte_lab, 0, 0, 0, 0, 0, 0, 0, 1, bsk.skip,
        )
    assert bsk.exp_d is not None
    exp_d, exp_ts, dte_lab = bsk.exp_d, bsk.exp_ts, bsk.dte_lab
    a_k, a_m, w_k, w_m = bsk.a_k, bsk.a_m, bsk.w_k, bsk.w_m
    a_call, w_call = bsk.a_call, bsk.w_call
    a_fill, w_fill = bsk.a_fill, bsk.w_fill
    dte_slip = 0 if dte_lab == "0DTE" else 1
    q = cfg.QTY
    fees = float(bsk.fees)
    slip = float(bsk.slip)
    gross = 0.0

    # remaining qty
    a_qty = q
    w_qty = q
    target_px = (
        sig.spot_entry + sig.r * tgt
        if sig.side == "long"
        else sig.spot_entry - sig.r * tgt
    )
    target_hit = 0
    target_done = False
    flip_i: int | None = None
    for j in range(sig.index + 1, len(candles)):
        if int(trend[j]) == 0:
            continue
        if sig.side == "long" and int(trend[j]) == -1:
            flip_i = j
            break
        if sig.side == "short" and int(trend[j]) == 1:
            flip_i = j
            break
        if candles[j].ts_close_bar >= exp_ts:
            break

    flip_exec = None
    if flip_i is not None:
        flip_exec = next_1m(spot, candles[flip_i].ts_close_bar)

    # 1m scan for target
    ts = sig.entry_ts
    last_ts = sig.entry_ts
    while ts in spot and ts < exp_ts:
        if flip_exec is not None and ts >= flip_exec:
            break
        bar = spot[ts]
        hit = (
            bar.high >= target_px
            if sig.side == "long"
            else bar.low <= target_px
        )
        if hit and not target_done:
            ex_ts = next_1m(spot, ts)
            if ex_ts is not None and ex_ts < exp_ts and (
                flip_exec is None or ex_ts < flip_exec
            ):
                px_a = mark_at(store, exp_d, a_call, a_k, ex_ts)
                px_w = mark_at(store, exp_d, w_call, w_k, ex_ts)
                if px_a is None or px_w is None:
                    miss = 1
                else:
                    q60 = int(round(q * cfg.PARTIAL_FRAC))
                    fa, _ = fill_price(px_a, "sell", dte=dte_slip)
                    fw, _ = fill_price(px_w, "sell", dte=dte_slip)
                    sp = spot[ex_ts].close
                    fees += option_fee(fa, sp, q60) + option_fee(fw, sp, q60)
                    slip += abs(fa - px_a) * qty_btc(q60) + abs(fw - px_w) * qty_btc(q60)
                    gross += signed_pnl(a_fill, fa, q60, is_long=True)
                    gross += signed_pnl(w_fill, fw, q60, is_long=True)
                    a_qty -= q60
                    w_qty -= q60
                    target_hit = 1
                    target_done = True
                    last_ts = ex_ts
                    break
        last_ts = ts
        ts += 60

    # remainder: flip or expiry
    def close_rem(at: int, settle: bool) -> int:
        nonlocal gross, fees, slip, miss, a_qty, w_qty
        if a_qty <= 0 and w_qty <= 0:
            return at
        sp_bar = spot.get(at)
        spx = sp_bar.close if sp_bar is not None else sig.spot_entry
        if settle:
            px_a = intrinsic(a_call, a_k, spx)
            px_w = intrinsic(w_call, w_k, spx)
            fa, fw = px_a, px_w
        else:
            px_a = mark_at(store, exp_d, a_call, a_k, at)
            px_w = mark_at(store, exp_d, w_call, w_k, at)
            if px_a is None or px_w is None:
                miss = 1
                return at
            fa, _ = fill_price(px_a, "sell", dte=dte_slip)
            fw, _ = fill_price(px_w, "sell", dte=dte_slip)
            fees += option_fee(fa, spx, a_qty) + option_fee(fw, spx, w_qty)
            slip += abs(fa - px_a) * qty_btc(a_qty) + abs(fw - px_w) * qty_btc(w_qty)
        gross += signed_pnl(a_fill, fa, a_qty, is_long=True)
        gross += signed_pnl(w_fill, fw, w_qty, is_long=True)
        a_qty = 0
        w_qty = 0
        return at

    exit_ts = last_ts
    if a_qty > 0 or w_qty > 0:
        if flip_exec is not None and flip_exec < exp_ts:
            exit_ts = close_rem(flip_exec, settle=False)
        else:
            at = exp_ts if exp_ts in spot else last_ts
            exit_ts = close_rem(at, settle=True)

    net = gross - fees
    hold = max(0.0, (exit_ts - sig.entry_ts) / 60.0)
    return Trade(
        side=sig.side,
        tf=tf_min,
        st_mult=st_mult,
        tgt=tgt,
        kind=kind,
        entry_ts=sig.entry_ts,
        exit_ts=exit_ts,
        day=day,
        half=half,
        dte=dte_lab,
        net=net,
        gross=gross,
        fees=fees,
        slippage=slip,
        hold_min=hold,
        win=int(net > 0),
        target_hit=target_hit,
        weekend=weekend_held(sig.entry_ts, exit_ts),
        mark_missing=miss,
        skip="",
    )


def collect_signals(
    candles: list[Candle],
    trend: np.ndarray,
    st: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
    cutoff_ts: int | None,
) -> tuple[list[Signal], np.ndarray, int]:
    raw, lp_active = detect_signals(
        candles, trend, st, spot, start_ts, cutoff_ts
    )
    sigs: list[Signal] = []
    la_fail = 0
    for s in raw:
        if lookahead_ok(candles, trend, st, spot, start_ts, s):
            sigs.append(s)
        else:
            la_fail += 1
    return sigs, lp_active, la_fail


def random_pool(
    candles: list[Candle],
    trend: np.ndarray,
    st: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
    cutoff_ts: int | None,
    lp_active: np.ndarray,
    sigs: list[Signal],
) -> list[Signal]:
    cands: list[Signal] = []
    for i, c in enumerate(candles):
        if cutoff_ts is not None and c.ts_open >= cutoff_ts:
            break
        if c.ts_close_bar < start_ts:
            continue
        if int(lp_active[i]) == 1:
            continue
        td = int(trend[i])
        if td == 0 or math.isnan(float(st[i])):
            continue
        side: Side = "long" if td == 1 else "short"
        s = _make_signal(side, i, c, st, spot, start_ts)
        if s is None:
            continue
        cands.append(s)
    sig_set = {(x.entry_ts, x.side) for x in sigs}
    return [x for x in cands if (x.entry_ts, x.side) not in sig_set]


def take_one_at_a_time(
    sigs: list[Signal],
    *,
    candles: list[Candle],
    trend: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
    tf_min: int,
    st_mult: float,
    tgt: float,
    kind: str,
) -> list[Trade]:
    trades: list[Trade] = []
    busy_until = -1
    for s in sigs:
        if s.entry_ts < busy_until:
            continue
        tr = simulate_trade(
            sig=s,
            candles=candles,
            trend=trend,
            spot=spot,
            store=store,
            tf_min=tf_min,
            st_mult=st_mult,
            tgt=tgt,
            kind=kind,
        )
        trades.append(tr)
        if not tr.skip:
            busy_until = tr.exit_ts
    return trades


def run_cell(
    *,
    candles: list[Candle],
    trend: np.ndarray,
    st: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
    start_ts: int,
    cutoff_ts: int | None,
    tf_min: int,
    st_mult: float,
    tgt: float,
    rng_seed: int,
    sigs: list[Signal] | None = None,
    pool: list[Signal] | None = None,
    la_fail: int = 0,
) -> tuple[list[Trade], list[Trade], int]:
    if sigs is None:
        sigs, lp_active, la_fail = collect_signals(
            candles, trend, st, spot, start_ts, cutoff_ts
        )
        pool = random_pool(
            candles, trend, st, spot, start_ts, cutoff_ts, lp_active, sigs
        )
    assert pool is not None
    trades = take_one_at_a_time(
        sigs,
        candles=candles,
        trend=trend,
        spot=spot,
        store=store,
        tf_min=tf_min,
        st_mult=st_mult,
        tgt=tgt,
        kind="signal",
    )
    need = cfg.RANDOM_MULT * max(len([t for t in trades if not t.skip]), 0)
    rng = np.random.default_rng(rng_seed)
    if pool and need > 0:
        idx = rng.choice(len(pool), size=min(need, len(pool)), replace=False)
        picks = [pool[int(i)] for i in np.atleast_1d(idx)]
    else:
        picks = []
    rand_trades = [
        simulate_trade(
            sig=s,
            candles=candles,
            trend=trend,
            spot=spot,
            store=store,
            tf_min=tf_min,
            st_mult=st_mult,
            tgt=tgt,
            kind="random",
        )
        for s in picks
    ]
    return trades, rand_trades, la_fail
