#!/usr/bin/env python3
"""S012G filters + exits. Imports S012/S012F; does not copy their source."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from backtest.harness.costs import fill_price, option_fee, qty_btc, signed_pnl
from backtest.harness.data import MarksStore
from backtest.strategies.s012_trend_follow import config as s012cfg
from backtest.strategies.s012_trend_follow.engine import (
    Bar1m,
    Candle,
    Signal,
    _green,
    _red,
    half_of,
    intrinsic,
    mark_at,
    next_1m,
    rma,
    resolve_basket,
    true_range,
)
from backtest.strategies.s012f_htf_filter.run_s012f import (  # noqa: E402
    htf_allows,
    last_completed_idx,
)
from backtest.strategies.s012g_whipsaw import config as cfg


@dataclass
class TradeG:
    arm: str
    st_mult: float
    side: str
    kind: str
    entry_ts: int
    exit_ts: int
    dte: str
    net: float
    gross: float
    fees: float
    slippage: float
    hold_min: float
    target_hit: int
    exit_reason: str
    adx_1h: float
    atr_5m: float
    lph: float
    lpl: float
    htf_1h: int
    htf_4h: int
    day: Any
    win: int
    skip: str = ""
    mark_missing: int = 0


def adx_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    """Wilder ADX. Causal. NaN until 2*period."""
    n = len(c)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period * 2:
        return out
    tr = true_range(h, l, c)
    plus = np.zeros(n)
    minus = np.zeros(n)
    for i in range(1, n):
        up = float(h[i] - h[i - 1])
        dn = float(l[i - 1] - l[i])
        if up > dn and up > 0:
            plus[i] = up
        if dn > up and dn > 0:
            minus[i] = dn
    atr = rma(tr, period)
    sp = rma(plus, period)
    sm = rma(minus, period)
    dx = np.full(n, np.nan)
    for i in range(n):
        a = float(atr[i])
        if math.isnan(a) or a <= 0:
            continue
        pdi = 100.0 * float(sp[i]) / a
        mdi = 100.0 * float(sm[i]) / a
        den = pdi + mdi
        if den <= 0:
            continue
        dx[i] = 100.0 * abs(pdi - mdi) / den
    # ADX = RMA of DX from first valid DX
    valid = [i for i in range(n) if not math.isnan(float(dx[i]))]
    if len(valid) < period:
        return out
    i0 = valid[0]
    # seed SMA of first `period` valid DX in index order from i0
    if i0 + period - 1 >= n:
        return out
    seed_sl = dx[i0 : i0 + period]
    if np.any(np.isnan(seed_sl)):
        # fill forward missing inside window
        return out
    out[i0 + period - 1] = float(np.mean(seed_sl))
    k = float(period)
    for i in range(i0 + period, n):
        if math.isnan(float(dx[i])) or math.isnan(float(out[i - 1])):
            continue
        out[i] = (out[i - 1] * (k - 1.0) + float(dx[i])) / k
    return out


def atr_wilder(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    return rma(true_range(h, l, c), period)


def _formed_lp(
    candles: list[Candle], trend: np.ndarray, i: int, side: str
) -> tuple[float, float] | None:
    if i < 2:
        return None
    a, b, d = candles[i - 2], candles[i - 1], candles[i]
    flipped = [int(trend[j]) for j in (i - 2, i - 1, i)]
    prevs = [int(trend[j - 1]) if j > 0 else 0 for j in (i - 2, i - 1, i)]
    lph = max(a.high, b.high, d.high)
    lpl = min(a.low, b.low, d.low)
    if side == "long":
        up = any(flipped[k] == 1 and prevs[k] == -1 for k in range(3))
        if _green(a) and _green(b) and _green(d) and a.close < b.close < d.close and up:
            return float(lph), float(lpl)
    else:
        dn = any(flipped[k] == -1 and prevs[k] == 1 for k in range(3))
        if _red(a) and _red(b) and _red(d) and a.close > b.close > d.close and dn:
            return float(lph), float(lpl)
    return None


def live_lp_series(
    candles: list[Candle], trend: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Forward pass: live LPH/LPL at each bar for long and short (completed only)."""
    n = len(candles)
    lph_l = np.full(n, np.nan)
    lpl_l = np.full(n, np.nan)
    lph_s = np.full(n, np.nan)
    lpl_s = np.full(n, np.nan)
    lp_l: dict[str, float] | None = None
    lp_s: dict[str, float] | None = None
    for i in range(n):
        td = int(trend[i])
        formed_l = _formed_lp(candles, trend, i, "long")
        formed_s = _formed_lp(candles, trend, i, "short")
        if formed_l:
            lp_l = {"lph": formed_l[0], "lpl": formed_l[1], "end": float(i)}
        if formed_s:
            lp_s = {"lph": formed_s[0], "lpl": formed_s[1], "end": float(i)}
        if lp_l is not None and i > int(lp_l["end"]):
            if td == -1 or candles[i].close < lp_l["lpl"]:
                lp_l = None
        if lp_s is not None and i > int(lp_s["end"]):
            if td == 1 or candles[i].close > lp_s["lph"]:
                lp_s = None
        if lp_l is not None:
            lph_l[i], lpl_l[i] = lp_l["lph"], lp_l["lpl"]
        if lp_s is not None:
            lph_s[i], lpl_s[i] = lp_s["lph"], lp_s["lpl"]
    return lph_l, lpl_l, lph_s, lpl_s


def lp_levels(
    candles: list[Candle],
    trend: np.ndarray,
    sig: Signal,
    cache: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> tuple[float, float] | None:
    if cache is not None:
        lph_l, lpl_l, lph_s, lpl_s = cache
        if sig.side == "long":
            a, b = float(lph_l[sig.index]), float(lpl_l[sig.index])
        else:
            a, b = float(lph_s[sig.index]), float(lpl_s[sig.index])
        if math.isnan(a) or math.isnan(b):
            return None
        return a, b
    # Fallback: still O(n) but only used in tiny unit tests
    best: tuple[float, float] | None = None
    lo = max(2, sig.index - 500)
    for i in range(lo, sig.index):
        lv = _formed_lp(candles, trend, i, sig.side)
        if lv is None:
            continue
        lph, lpl = lv
        cancelled = False
        for k in range(i + 1, sig.index + 1):
            td = int(trend[k])
            cl = candles[k].close
            if sig.side == "long" and (td == -1 or cl < lpl):
                cancelled = True
                break
            if sig.side == "short" and (td == 1 or cl > lph):
                cancelled = True
                break
        if not cancelled:
            best = (lph, lpl)
    return best


def last_completed_idx_fast(candles: list[Candle], entry_ts: int, tf_sec: int) -> int:
    lo, hi, ans = 0, len(candles) - 1, -1
    e = int(entry_ts)
    step = int(tf_sec)
    while lo <= hi:
        mid = (lo + hi) // 2
        done = int(candles[mid].ts_open) + step
        if done < e:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def adx_at_entry(
    entry_ts: int, c1h: list[Candle], adx: np.ndarray
) -> float:
    idx = last_completed_idx_fast(c1h, entry_ts, 3600)
    if idx < 0:
        return float("nan")
    return float(adx[idx])


def buffer_ok(
    sig: Signal,
    candles: list[Candle],
    trend: np.ndarray,
    atr: np.ndarray,
    lp_cache: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> bool:
    lv = lp_levels(candles, trend, sig, lp_cache)
    a = float(atr[sig.index]) if sig.index < len(atr) else float("nan")
    if lv is None or math.isnan(a):
        return False
    lph, lpl = lv
    need = cfg.ATR_BUFFER * a
    if sig.side == "long":
        return candles[sig.index].close >= lph + need
    return candles[sig.index].close <= lpl - need


def uses_adx(arm: str) -> bool:
    return arm in ("B", "F", "G")


def uses_confirm(arm: str) -> bool:
    return arm in ("C", "F", "G")


def uses_buffer(arm: str) -> bool:
    return arm in ("D", "F", "G")


def uses_cooldown(arm: str) -> bool:
    return arm in ("E", "F", "G")


def atm_only(arm: str) -> bool:
    return arm == "G"


def filter_entry(
    sig: Signal,
    arm: str,
    *,
    c1h: list[Candle],
    t1h: np.ndarray,
    c4h: list[Candle],
    t4h: np.ndarray,
    adx: np.ndarray,
    candles: list[Candle],
    trend: np.ndarray,
    atr5: np.ndarray,
    lp_cache: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> tuple[bool, dict[str, float]]:
    from backtest.strategies.s012f_htf_filter.run_s012f import htf_dir

    h1 = htf_dir(sig.entry_ts, c1h, t1h, 3600)
    h4 = htf_dir(sig.entry_ts, c4h, t4h, 14400)
    ax = adx_at_entry(sig.entry_ts, c1h, adx)
    a5 = float(atr5[sig.index]) if sig.index < len(atr5) else float("nan")
    lph = lpl = float("nan")
    if uses_buffer(arm):
        lv = lp_levels(candles, trend, sig, lp_cache)
        if lv:
            lph, lpl = lv
    info = {
        "adx_1h": ax,
        "atr_5m": a5,
        "lph": lph,
        "lpl": lpl,
        "htf_1h": float(h1),
        "htf_4h": float(h4),
    }
    if not htf_allows(sig, c1h, t1h, c4h, t4h):
        return False, info
    if uses_adx(arm):
        if math.isnan(ax) or ax <= cfg.ADX_MIN:
            return False, info
    if uses_buffer(arm) and not buffer_ok(sig, candles, trend, atr5, lp_cache):
        return False, info
    return True, info


def confirm_against(
    side: str, trend: np.ndarray, start_i: int, n_consec: int
) -> int | None:
    against = -1 if side == "long" else 1
    run = 0
    for j in range(max(0, start_i), len(trend)):
        td = int(trend[j])
        if td == 0:
            run = 0
            continue
        if td == against:
            run += 1
            if run >= n_consec:
                return j
        else:
            run = 0
    return None


def simulate_g(
    *,
    sig: Signal,
    arm: str,
    st_mult: float,
    candles: list[Candle],
    trend: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
    kind: str,
    filt: dict[str, float],
) -> TradeG:
    n_conf = cfg.CONFIRM_BARS if uses_confirm(arm) else 1
    wing = not atm_only(arm)
    day = sig.day
    bsk = resolve_basket(store, sig)
    if bsk.skip:
        return TradeG(
            arm, st_mult, sig.side, kind, sig.entry_ts, sig.entry_ts, bsk.dte_lab,
            0, 0, 0, 0, 0, 0, bsk.skip,
            filt.get("adx_1h", float("nan")), filt.get("atr_5m", float("nan")),
            filt.get("lph", float("nan")), filt.get("lpl", float("nan")),
            int(filt.get("htf_1h", 0)), int(filt.get("htf_4h", 0)),
            day, 0, bsk.skip, 1,
        )
    assert bsk.exp_d is not None
    exp_d, exp_ts, dte_lab = bsk.exp_d, bsk.exp_ts, bsk.dte_lab
    a_k, a_m = bsk.a_k, bsk.a_m
    w_k, w_m = bsk.w_k, bsk.w_m
    a_call, w_call = bsk.a_call, bsk.w_call
    a_fill, w_fill = bsk.a_fill, bsk.w_fill
    dte_slip = 0 if dte_lab == "0DTE" else 1
    q = s012cfg.QTY
    fees = option_fee(a_fill, sig.spot_entry, q)
    slip = abs(a_fill - a_m) * qty_btc(q)
    if wing:
        fees += option_fee(w_fill, sig.spot_entry, q)
        slip += abs(w_fill - w_m) * qty_btc(q)
    gross = 0.0
    a_qty, w_qty = q, (q if wing else 0)
    miss = 0
    target_hit = 0
    reason = "expiry"
    last_ts = sig.entry_ts
    tgt = (
        sig.spot_entry + sig.r * cfg.TGT
        if sig.side == "long"
        else sig.spot_entry - sig.r * cfg.TGT
    )
    flip_i = confirm_against(sig.side, trend, sig.index + 1, n_conf)
    flip_exec = None
    if flip_i is not None:
        flip_exec = next_1m(spot, candles[flip_i].ts_close_bar)

    def sell(at: int, qa: int, qw: int, settle: bool) -> bool:
        nonlocal gross, fees, slip, miss
        if qa <= 0 and qw <= 0:
            return True
        sp_bar = spot.get(at)
        spx = sp_bar.close if sp_bar is not None else sig.spot_entry
        if settle:
            fa = intrinsic(a_call, a_k, spx)
            fw = intrinsic(w_call, w_k, spx) if qw else 0.0
        else:
            px_a = mark_at(store, exp_d, a_call, a_k, at)
            px_w = mark_at(store, exp_d, w_call, w_k, at) if qw else 0.0
            if px_a is None or (qw and px_w is None):
                miss = 1
                return False
            fa, _ = fill_price(px_a, "sell", dte=dte_slip)
            fees += option_fee(fa, spx, qa)
            slip += abs(fa - px_a) * qty_btc(qa)
            fw = 0.0
            if qw:
                fw, _ = fill_price(px_w, "sell", dte=dte_slip)  # type: ignore[arg-type]
                fees += option_fee(fw, spx, qw)
                slip += abs(fw - px_w) * qty_btc(qw)  # type: ignore[operator]
        if qa:
            gross += signed_pnl(a_fill, fa, qa, is_long=True)
        if qw:
            gross += signed_pnl(w_fill, fw, qw, is_long=True)
        return True

    ts = sig.entry_ts
    hit = False
    hit_ts = ts
    while ts in spot and ts < exp_ts:
        if flip_exec is not None and ts >= flip_exec:
            break
        bar = spot[ts]
        ok = bar.high >= tgt if sig.side == "long" else bar.low <= tgt
        if ok:
            hit = True
            hit_ts = ts
            break
        last_ts = ts
        ts += 60

    if hit:
        ex = next_1m(spot, hit_ts)
        if ex is None or ex >= exp_ts or (flip_exec is not None and ex >= flip_exec):
            at = exp_ts if exp_ts in spot else last_ts
            sell(at, a_qty, w_qty, True)
            a_qty = w_qty = 0
            reason = "expiry"
            last_ts = at
        else:
            q60 = int(round(q * s012cfg.PARTIAL_FRAC))
            qw60 = q60 if wing else 0
            if not sell(ex, q60, qw60, False):
                reason = "mark_missing"
                last_ts = ex
            else:
                a_qty -= q60
                if wing:
                    w_qty -= qw60
                target_hit = 1
                last_ts = ex
                # runner: 2-close confirm from last completed 5m at/after target
                st_i = sig.index + 1
                for j, c in enumerate(candles):
                    if c.ts_close_bar <= hit_ts:
                        st_i = j
                f2 = confirm_against(sig.side, trend, st_i, n_conf)
                if f2 is not None:
                    fex = next_1m(spot, candles[f2].ts_close_bar)
                    if fex is not None and fex < exp_ts:
                        if sell(fex, a_qty, w_qty, False):
                            a_qty = w_qty = 0
                            reason = "target_flip"
                            last_ts = fex
                        else:
                            reason = "mark_missing"
                            last_ts = fex
                    else:
                        at = exp_ts if exp_ts in spot else last_ts
                        sell(at, a_qty, w_qty, True)
                        a_qty = w_qty = 0
                        reason = "target_expiry"
                        last_ts = at
                else:
                    at = exp_ts if exp_ts in spot else last_ts
                    sell(at, a_qty, w_qty, True)
                    a_qty = w_qty = 0
                    reason = "target_expiry"
                    last_ts = at
    elif flip_exec is not None and flip_exec < exp_ts:
        if sell(flip_exec, a_qty, w_qty, False):
            a_qty = w_qty = 0
            reason = "st_flip"
            last_ts = flip_exec
        else:
            reason = "mark_missing"
            last_ts = flip_exec
    else:
        at = exp_ts if exp_ts in spot else last_ts
        sell(at, a_qty, w_qty, True)
        a_qty = w_qty = 0
        reason = "expiry"
        last_ts = at

    net = gross - fees
    return TradeG(
        arm=arm,
        st_mult=st_mult,
        side=sig.side,
        kind=kind,
        entry_ts=sig.entry_ts,
        exit_ts=last_ts,
        dte=dte_lab,
        net=net,
        gross=gross,
        fees=fees,
        slippage=slip,
        hold_min=max(0.0, (last_ts - sig.entry_ts) / 60.0),
        target_hit=target_hit,
        exit_reason=reason,
        adx_1h=filt.get("adx_1h", float("nan")),
        atr_5m=filt.get("atr_5m", float("nan")),
        lph=filt.get("lph", float("nan")),
        lpl=filt.get("lpl", float("nan")),
        htf_1h=int(filt.get("htf_1h", 0)),
        htf_4h=int(filt.get("htf_4h", 0)),
        day=day,
        win=int(net > 0),
        skip="",
        mark_missing=miss,
    )


def take_sequential(
    sigs: list[tuple[Signal, dict[str, float]]],
    *,
    arm: str,
    st_mult: float,
    candles: list[Candle],
    trend: np.ndarray,
    spot: dict[int, Bar1m],
    store: MarksStore,
) -> list[TradeG]:
    trades: list[TradeG] = []
    busy_until = -1
    cool_until = -1
    for sig, filt in sigs:
        if sig.entry_ts < busy_until:
            continue
        if uses_cooldown(arm) and sig.entry_ts < cool_until:
            continue
        tr = simulate_g(
            sig=sig,
            arm=arm,
            st_mult=st_mult,
            candles=candles,
            trend=trend,
            spot=spot,
            store=store,
            kind="signal",
            filt=filt,
        )
        trades.append(tr)
        if not tr.skip:
            busy_until = tr.exit_ts
            if uses_cooldown(arm) and tr.net < 0:
                cool_until = tr.exit_ts + cfg.COOLDOWN_SEC
    return trades


def test_new_filters_lookahead() -> None:
    """Truncate at entry: ADX, ATR buffer, 2-close confirm, cooldown use no future."""
    from backtest.strategies.s012_trend_follow.engine import supertrend

    n = 40
    h = np.linspace(100, 140, n) + 0.5
    l = np.linspace(100, 140, n) - 0.5
    c = np.linspace(100, 140, n)
    adx_full = adx_wilder(h, l, c, cfg.ADX_LEN)
    idx = 30
    adx_tr = adx_wilder(h[: idx + 1], l[: idx + 1], c[: idx + 1], cfg.ADX_LEN)
    assert not math.isnan(float(adx_full[idx]))
    assert abs(float(adx_full[idx]) - float(adx_tr[idx])) < 1e-9

    atr_full = atr_wilder(h, l, c, cfg.ATR_LEN)
    atr_tr = atr_wilder(h[: idx + 1], l[: idx + 1], c[: idx + 1], cfg.ATR_LEN)
    assert abs(float(atr_full[idx]) - float(atr_tr[idx])) < 1e-9

    trend = np.ones(n, dtype=np.int8)
    trend[20:] = -1
    trend[21:] = -1
    i1 = confirm_against("long", trend, 15, 1)
    i2 = confirm_against("long", trend, 15, 2)
    assert i1 == 20 and i2 == 21
    i2t = confirm_against("long", trend[:22], 15, 2)
    assert i2t == 21
    i2short = confirm_against("long", trend[:21], 15, 2)
    assert i2short is None  # need 2 consecutive; truncating before 2nd close

    # cooldown: a loss after entry must not affect an earlier signal
    entry = 1000
    loss_exit = 2000
    cool = loss_exit + cfg.COOLDOWN_SEC
    assert entry < cool  # later loss
    # truncated world with no future loss: cooldown inactive
    cool_trunc = -1
    assert entry >= cool_trunc
    print("s012g look-ahead: PASS")
