#!/usr/bin/env python3
"""S013: Smith-band signal, S012 basket/marks/fills. No S012 source copied."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np

from backtest.harness.costs import fill_price, option_fee, qty_btc, signed_pnl
from backtest.harness.data import MarksStore
from backtest.strategies.s007b_vwap_directional.indicators import rsi_wilder, smith_vwap
from backtest.strategies.s012_trend_follow import config as s012cfg
from backtest.strategies.s012_trend_follow.engine import (
    Bar1m,
    Candle,
    Signal as S012Signal,
    build_tf,
    half_of,
    intrinsic,
    ist_date,
    load_spot_1m,
    mark_at,
    mark_by_symbol,
    next_1m,
    resolve_basket,
    expiry_choice,
    supertrend,
    weekend_held,
)
from backtest.strategies.s013_smith_basket import config as cfg

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
Side = Literal["long", "short"]


@dataclass
class SmithSig:
    side: Side
    index: int
    signal_ts: int
    entry_ts: int
    spot_entry: float
    w: float
    lower: float
    upper: float
    rsi: float
    day: object
    weekday: int
    hour_ist: int


@dataclass
class Trade:
    rsi_period: int
    n_ratio: float
    side: str
    kind: str
    w: float
    target_px: float
    stop_px: float
    spot_entry: float
    spot_exit: float
    a_strike: float
    w_strike: float
    a_entry_mark: float
    w_entry_mark: float
    a_exit_mark: float
    w_exit_mark: float
    exit_reason: str
    st_at_target: str
    runner_wait: int
    same_bar: int
    entry_ts: int
    exit_ts: int
    day: object
    half: str
    dte: str
    net: float
    gross: float
    fees: float
    slippage: float
    hold_min: float
    win: int
    target_hit: int
    stop_hit: int
    expiry_exit: int
    weekend: int
    mark_missing: int
    skip: str = ""


def candle_volumes(spot: dict[int, Bar1m], candles: list[Candle], tf_min: int) -> np.ndarray:
    step = int(tf_min) * 60
    out = np.zeros(len(candles), dtype=np.float64)
    for i, c in enumerate(candles):
        tot = 0.0
        t = int(c.ts_open)
        end = t + step
        while t < end:
            b = spot.get(t)
            if b is not None:
                tot += float(b.volume)
            t += 60
        out[i] = tot
    return out


def _weekday_ok(ts: int) -> tuple[int, object, int] | None:
    dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST)
    wd = dt.weekday()
    if wd not in s012cfg.WEEKDAYS_OK:
        return None
    day = ist_date(ts)
    if half_of(day) == "OUT":
        return None
    return wd, day, dt.hour


def detect_smith(
    candles: list[Candle],
    lower: np.ndarray,
    upper: np.ndarray,
    rsi: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
    cutoff_ts: int | None,
) -> list[SmithSig]:
    """Smith close-through-band + RSI. Cooldown after each accepted signal."""
    out: list[SmithSig] = []
    last_sig_ts = -10**18
    n = len(candles)
    for i in range(1, n):
        c = candles[i]
        if cutoff_ts is not None and c.ts_open >= cutoff_ts:
            break
        if c.ts_close_bar < start_ts:
            continue
        lo, up = float(lower[i]), float(upper[i])
        plo, pup = float(lower[i - 1]), float(upper[i - 1])
        if any(math.isnan(x) for x in (lo, up, plo, pup, float(rsi[i]), float(rsi[i - 1]))):
            continue
        prev_c = candles[i - 1].close
        side: Side | None = None
        if prev_c >= plo and c.close < lo and float(rsi[i]) < cfg.RSI_LONG:
            side = "long"
        elif prev_c <= pup and c.close > up and float(rsi[i]) > cfg.RSI_SHORT:
            side = "short"
        if side is None:
            continue
        if int(c.ts_close_bar) - last_sig_ts < cfg.COOLDOWN_SEC:
            continue
        wd = _weekday_ok(c.ts_close_bar)
        if wd is None:
            continue
        entry_ts = int(c.ts_close_bar) + 60
        bar = spot.get(entry_ts)
        if bar is None:
            continue
        w = up - lo
        if w <= 0:
            continue
        weekday, day, hour = wd
        out.append(
            SmithSig(
                side=side,
                index=i,
                signal_ts=int(c.ts_close_bar),
                entry_ts=entry_ts,
                spot_entry=float(bar.close),
                w=float(w),
                lower=lo,
                upper=up,
                rsi=float(rsi[i]),
                day=day,
                weekday=weekday,
                hour_ist=hour,
            )
        )
        last_sig_ts = int(c.ts_close_bar)
    return out


def lookahead_ok(
    candles: list[Candle],
    lower: np.ndarray,
    upper: np.ndarray,
    rsi: np.ndarray,
    spot: dict[int, Bar1m],
    start_ts: int,
    sig: SmithSig,
) -> bool:
    found = detect_smith(
        candles[: sig.index + 1],
        lower[: sig.index + 1],
        upper[: sig.index + 1],
        rsi[: sig.index + 1],
        spot,
        start_ts,
        None,
    )
    if not found:
        return False
    last = found[-1]
    return last.index == sig.index and last.side == sig.side


def to_s012(sig: SmithSig) -> S012Signal:
    return S012Signal(
        side=sig.side,
        index=sig.index,
        signal_ts=sig.signal_ts,
        entry_ts=sig.entry_ts,
        spot_entry=sig.spot_entry,
        st_value=0.0,
        r=sig.w,
        day=sig.day,  # type: ignore[arg-type]
        weekday=sig.weekday,
    )


def _st_closed_idx(candles: list[Candle], ts: int) -> int | None:
    last: int | None = None
    for i, c in enumerate(candles):
        if c.ts_close_bar <= ts:
            last = i
        else:
            break
    return last


def _flip_against(
    side: Side, candles: list[Candle], trend: np.ndarray, start_i: int
) -> int | None:
    """First TF close at/after start_i that is against `side` after having been with it.

    If already against at start_i, wait until with, then against.
    """
    n = len(candles)
    i0 = max(0, start_i)

    def with_trade(i: int) -> bool | None:
        td = int(trend[i])
        if td == 0:
            return None
        if side == "long":
            return td == 1
        return td == -1

    aligned = with_trade(i0)
    # skip unknown
    j = i0
    while j < n and with_trade(j) is None:
        j += 1
    if j >= n:
        return None
    if with_trade(j) is False:
        while j < n and with_trade(j) is not True:
            j += 1
        if j >= n:
            return None
        j += 1
        while j < n:
            w = with_trade(j)
            if w is False:
                return j
            j += 1
        return None
    # already with
    j += 1
    while j < n:
        w = with_trade(j)
        if w is False:
            return j
        j += 1
    return None


def simulate(
    *,
    sig: SmithSig,
    n_ratio: float,
    rsi_period: int,
    candles: list[Candle],
    trend: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
    kind: str,
) -> Trade:
    day = sig.day
    half = half_of(day)  # type: ignore[arg-type]
    tgt = (
        sig.spot_entry + sig.w
        if sig.side == "long"
        else sig.spot_entry - sig.w
    )
    stop = (
        sig.spot_entry - sig.w / n_ratio
        if sig.side == "long"
        else sig.spot_entry + sig.w / n_ratio
    )
    blank = Trade(
        rsi_period, n_ratio, sig.side, kind, sig.w, tgt, stop,
        sig.spot_entry, sig.spot_entry, 0, 0, 0, 0, 0, 0, "skip", "na",
        0, 0, sig.entry_ts, sig.entry_ts, day, half, "", 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 1, "skip",
    )
    bsk = resolve_basket(store, to_s012(sig))
    if bsk.skip:
        blank.skip = bsk.skip
        blank.dte = bsk.dte_lab
        blank.mark_missing = 1
        return blank
    assert bsk.exp_d is not None
    exp_d, exp_ts, dte_lab = bsk.exp_d, bsk.exp_ts, bsk.dte_lab
    a_k, a_m, w_k, w_m = bsk.a_k, bsk.a_m, bsk.w_k, bsk.w_m
    a_call, w_call = bsk.a_call, bsk.w_call
    a_fill, w_fill = bsk.a_fill, bsk.w_fill
    dte_slip = 0 if dte_lab == "0DTE" else 1
    q = s012cfg.QTY
    fees = float(bsk.fees)
    slip = float(bsk.slip)
    gross = 0.0
    a_qty, w_qty = q, q
    miss = 0
    same_bar = 0
    target_hit = 0
    stop_hit = 0
    expiry_exit = 0
    a_exit = 0.0
    w_exit = 0.0
    reason = "expiry"
    st_at = "na"
    runner_wait = 0
    last_ts = sig.entry_ts
    spot_exit = sig.spot_entry

    def sell_legs(at: int, qty: int, settle: bool) -> bool:
        nonlocal gross, fees, slip, miss, a_exit, w_exit, spot_exit
        if qty <= 0:
            return True
        sp_bar = spot.get(at)
        spx = sp_bar.close if sp_bar is not None else sig.spot_entry
        spot_exit = spx
        if settle:
            px_a = intrinsic(a_call, a_k, spx)
            px_w = intrinsic(w_call, w_k, spx)
            fa, fw = px_a, px_w
        else:
            px_a = mark_at(store, exp_d, a_call, a_k, at)
            px_w = mark_at(store, exp_d, w_call, w_k, at)
            if px_a is None or px_w is None:
                miss = 1
                return False
            fa, _ = fill_price(px_a, "sell", dte=dte_slip)
            fw, _ = fill_price(px_w, "sell", dte=dte_slip)
            fees += option_fee(fa, spx, qty) + option_fee(fw, spx, qty)
            slip += abs(fa - px_a) * qty_btc(qty) + abs(fw - px_w) * qty_btc(qty)
        gross += signed_pnl(a_fill, fa, qty, is_long=True)
        gross += signed_pnl(w_fill, fw, qty, is_long=True)
        a_exit, w_exit = fa, fw
        return True

    ts = sig.entry_ts
    hit_kind: str | None = None
    hit_ts = ts
    while ts in spot and ts < exp_ts:
        bar = spot[ts]
        ht = bar.high >= tgt if sig.side == "long" else bar.low <= tgt
        hs = bar.low <= stop if sig.side == "long" else bar.high >= stop
        if ht and hs:
            same_bar = 1
            hit_kind = "stop"
            hit_ts = ts
            break
        if hs:
            hit_kind = "stop"
            hit_ts = ts
            break
        if ht:
            hit_kind = "target"
            hit_ts = ts
            break
        last_ts = ts
        ts += 60

    if hit_kind == "stop":
        ex = next_1m(spot, hit_ts)
        if ex is None or ex >= exp_ts:
            sell_legs(exp_ts if exp_ts in spot else last_ts, q, settle=True)
            expiry_exit = 1
            reason = "expiry"
            last_ts = exp_ts if exp_ts in spot else last_ts
        else:
            if sell_legs(ex, q, settle=False):
                a_qty = w_qty = 0
                stop_hit = 1
                reason = "stop"
                last_ts = ex
            else:
                reason = "mark_missing"
                last_ts = ex
    elif hit_kind == "target":
        ex = next_1m(spot, hit_ts)
        if ex is None or ex >= exp_ts:
            sell_legs(exp_ts if exp_ts in spot else last_ts, q, settle=True)
            expiry_exit = 1
            reason = "expiry"
            last_ts = exp_ts if exp_ts in spot else last_ts
        else:
            q60 = int(round(q * s012cfg.PARTIAL_FRAC))
            if not sell_legs(ex, q60, settle=False):
                reason = "mark_missing"
                last_ts = ex
            else:
                a_qty -= q60
                w_qty -= q60
                target_hit = 1
                last_ts = ex
                st_i = _st_closed_idx(candles, hit_ts)
                if st_i is None:
                    st_at = "na"
                else:
                    td = int(trend[st_i])
                    with_dir = (sig.side == "long" and td == 1) or (
                        sig.side == "short" and td == -1
                    )
                    against = (sig.side == "long" and td == -1) or (
                        sig.side == "short" and td == 1
                    )
                    st_at = "with" if with_dir else ("against" if against else "na")
                    runner_wait = int(st_at == "against")
                    flip_i = _flip_against(sig.side, candles, trend, st_i)
                    if flip_i is not None:
                        fex = next_1m(spot, candles[flip_i].ts_close_bar)
                        if fex is not None and fex < exp_ts:
                            if sell_legs(fex, a_qty, settle=False):
                                a_qty = w_qty = 0
                                reason = "target_flip"
                                last_ts = fex
                            else:
                                reason = "mark_missing"
                                last_ts = fex
                        else:
                            at = exp_ts if exp_ts in spot else last_ts
                            sell_legs(at, a_qty, settle=True)
                            a_qty = w_qty = 0
                            expiry_exit = 1
                            reason = "target_expiry"
                            last_ts = at
                    else:
                        at = exp_ts if exp_ts in spot else last_ts
                        sell_legs(at, a_qty, settle=True)
                        a_qty = w_qty = 0
                        expiry_exit = 1
                        reason = "target_expiry"
                        last_ts = at
    else:
        at = exp_ts if exp_ts in spot else last_ts
        sell_legs(at, q, settle=True)
        a_qty = w_qty = 0
        expiry_exit = 1
        reason = "expiry"
        last_ts = at

    net = gross - fees
    hold = max(0.0, (last_ts - sig.entry_ts) / 60.0)
    return Trade(
        rsi_period=rsi_period,
        n_ratio=n_ratio,
        side=sig.side,
        kind=kind,
        w=sig.w,
        target_px=tgt,
        stop_px=stop,
        spot_entry=sig.spot_entry,
        spot_exit=spot_exit,
        a_strike=a_k,
        w_strike=w_k,
        a_entry_mark=a_m,
        w_entry_mark=w_m,
        a_exit_mark=a_exit,
        w_exit_mark=w_exit,
        exit_reason=reason,
        st_at_target=st_at,
        runner_wait=runner_wait,
        same_bar=same_bar,
        entry_ts=sig.entry_ts,
        exit_ts=last_ts,
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
        stop_hit=stop_hit,
        expiry_exit=expiry_exit,
        weekend=weekend_held(sig.entry_ts, last_ts),
        mark_missing=miss,
        skip="",
    )


def take_one_at_a_time(
    sigs: list[SmithSig],
    *,
    n_ratio: float,
    rsi_period: int,
    candles: list[Candle],
    trend: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
    kind: str,
) -> list[Trade]:
    trades: list[Trade] = []
    busy_until = -1
    for s in sigs:
        if s.entry_ts < busy_until:
            continue
        tr = simulate(
            sig=s,
            n_ratio=n_ratio,
            rsi_period=rsi_period,
            candles=candles,
            trend=trend,
            spot=spot,
            store=store,
            kind=kind,
        )
        trades.append(tr)
        if not tr.skip:
            busy_until = tr.exit_ts
    return trades


def random_pool(
    candles: list[Candle],
    lower: np.ndarray,
    upper: np.ndarray,
    sigs: list[SmithSig],
    spot: dict[int, Bar1m],
    start_ts: int,
    cutoff_ts: int | None,
) -> dict[int, list[int]]:
    """Candidate 5m indices by IST hour: weekday, not in cooldown after a signal."""
    blocked: set[int] = set()
    for s in sigs:
        blocked.add(s.index)
    cool = {s.signal_ts for s in sigs}
    by_hour: dict[int, list[int]] = {h: [] for h in range(24)}
    for i, c in enumerate(candles):
        if cutoff_ts is not None and c.ts_open >= cutoff_ts:
            break
        if c.ts_close_bar < start_ts:
            continue
        if i in blocked:
            continue
        if math.isnan(float(lower[i])) or math.isnan(float(upper[i])):
            continue
        if float(upper[i]) - float(lower[i]) <= 0:
            continue
        in_cool = any(
            0 <= int(c.ts_close_bar) - st < cfg.COOLDOWN_SEC for st in cool
        )
        if in_cool:
            continue
        wd = _weekday_ok(c.ts_close_bar)
        if wd is None:
            continue
        if c.ts_close_bar + 60 not in spot:
            continue
        by_hour[wd[2]].append(i)
    return by_hour


def pick_random(
    candles: list[Candle],
    lower: np.ndarray,
    upper: np.ndarray,
    sigs: list[SmithSig],
    pool: dict[int, list[int]],
    spot: dict[int, Bar1m],
    rng: np.random.Generator,
) -> list[SmithSig]:
    need_h: dict[int, int] = {h: 0 for h in range(24)}
    for s in sigs:
        need_h[s.hour_ist] += cfg.RANDOM_MULT
    out: list[SmithSig] = []
    used: set[int] = set()
    for hour, need in need_h.items():
        cands = [i for i in pool.get(hour, []) if i not in used]
        if not cands or need <= 0:
            continue
        take = min(need, len(cands))
        idx = rng.choice(len(cands), size=take, replace=False)
        for k in np.atleast_1d(idx):
            i = cands[int(k)]
            used.add(i)
            c = candles[i]
            wd = _weekday_ok(c.ts_close_bar)
            if wd is None:
                continue
            weekday, day, hour_ist = wd
            bar = spot[c.ts_close_bar + 60]
            side: Side = "long" if float(rng.random()) < 0.5 else "short"
            w = float(upper[i]) - float(lower[i])
            out.append(
                SmithSig(
                    side=side,
                    index=i,
                    signal_ts=int(c.ts_close_bar),
                    entry_ts=int(c.ts_close_bar) + 60,
                    spot_entry=float(bar.close),
                    w=w,
                    lower=float(lower[i]),
                    upper=float(upper[i]),
                    rsi=float("nan"),
                    day=day,
                    weekday=weekday,
                    hour_ist=hour_ist,
                )
            )
    return out
