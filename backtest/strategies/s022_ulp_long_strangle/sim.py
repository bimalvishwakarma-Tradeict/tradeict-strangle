"""S022 path simulator: ITM/OTM long strangle, per-leg a% roll, post-hoc T/SL.

Adjustments do not depend on T/SL. Simulate one path to expiry, then slice T/SL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol

import numpy as np

from backtest.fees_sim import OPTION_FEE_RATE, OPTIONS_CONTRACT_VALUE, PREMIUM_CAP_RATE
from backtest.harness.run_archive import greeks_from_mark
from backtest.strategies.s012_trend_follow.engine import intrinsic, ist_date, t_years
from backtest.strategies.s018_4h_trend import run_s018 as s018
from backtest.strategies.s020_vwap_touch import run_s020_dev as d

QTY = 1000
SLIP_PTS = 20.0
X_ITM = (400, 600, 800, 1000, 1200)
X_OTM = (1000,)
A_GRID = (120, 125, 130, 135, 140, 145, 150)
T_GRID = (150, 200, 250, 300, 350, 400)
SL_GRID = (50, 100, 150, 200)
DTE_GRID = (1, 2)
MULT = float(QTY) * float(OPTIONS_CONTRACT_VALUE)
GST = 1.18
STALE_MIN = 5


class Market(Protocol):
    def spot(self, t: int) -> float | None: ...
    def quote(self, symbol: str, t: int) -> tuple[float, str] | None: ...
    def chain(self, exp: date, t: int) -> tuple[list[dict[str, Any]], str] | None: ...
    def ensure(self, symbol: str, exp: date) -> None: ...


def slip_usd(qty: int = QTY) -> float:
    return float(SLIP_PTS) * float(qty) * float(OPTIONS_CONTRACT_VALUE)


def buy_fill(mark: float) -> float:
    return float(mark) + SLIP_PTS


def sell_fill(mark: float) -> float:
    return float(mark) - SLIP_PTS


def sl_level(peak_net: float, sl: float) -> float:
    """Ratchet: -SL + 5 * floor(max(0, peak)/10). peak 0->-SL; 10->-SL+5; 25->-SL+10."""
    return -float(sl) + 5.0 * math.floor(max(0.0, float(peak_net)) / 10.0)


def fee_at(mark: float, spot: float, qty: int = QTY) -> float:
    return float(d.fee_gst_qty(float(mark), float(spot), int(qty)))


def est_exit_cost(mark: float, spot: float, qty: int = QTY) -> float:
    return fee_at(mark, spot, qty) + slip_usd(qty)


def expiry_date(entry_ts: int, dte: int) -> date:
    return ist_date(int(entry_ts)) + timedelta(days=int(dte))


def in_trade_window(signal_ts: int, month_start: int, month_end: int) -> bool:
    """True iff month_start <= signal_ts < month_end. Warmup signals are not traded."""
    t = int(signal_ts)
    return int(month_start) <= t < int(month_end)


def filter_traded_events(
    events: list[dict[str, Any]],
    month_start: int,
    month_end: int,
) -> list[dict[str, Any]]:
    return [e for e in events if in_trade_window(int(e["signal_ts"]), month_start, month_end)]


def map_src(raw: str) -> str:
    s = str(raw).strip().lower()
    if s == "real":
        return "real"
    if s in ("stale", "ffill"):
        return "stale"
    if s == "parity":
        return "parity"
    if s in ("none", ""):
        return "none"
    return s


def parity_mark(is_call: bool, strike: float, spot: float, opp_px: float) -> float:
    """call = put + S - K; put = call - S + K; floor at intrinsic."""
    k = float(strike)
    s = float(spot)
    o = float(opp_px)
    if bool(is_call):
        px = o + s - k
        floor = max(0.0, s - k)
    else:
        px = o - s + k
        floor = max(0.0, k - s)
    return max(float(px), float(floor))


@dataclass
class SimLeg:
    role: str
    symbol: str
    strike: float
    is_call: bool
    expiry: date
    mark0: float
    fill: float
    fee: float
    slip: float
    qty: int
    ref: float
    src: str
    opened_ts: int
    opp_symbol: str = ""
    open: bool = True
    closed_ts: int = 0
    exit_mark: float = float("nan")
    exit_fill: float = float("nan")
    exit_fee: float = 0.0
    exit_slip: float = 0.0
    settled: bool = False


@dataclass
class PathResult:
    ok: bool
    skip_reason: str
    t0: int
    exp_ts: int
    ts: list[int] = field(default_factory=list)
    net: list[float] = field(default_factory=list)
    peak: list[float] = field(default_factory=list)
    gross: list[float] = field(default_factory=list)
    peak_gross: list[float] = field(default_factory=list)
    n_adj: list[int] = field(default_factory=list)
    src_counts: dict[str, int] = field(default_factory=lambda: {"real": 0, "stale": 0, "parity": 0, "none": 0})
    fees_m: list[float] = field(default_factory=list)
    slip_m: list[float] = field(default_factory=list)
    est_fee_m: list[float] = field(default_factory=list)
    est_slip_m: list[float] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)
    n_adj_total: int = 0
    fees: float = 0.0
    slips: float = 0.0
    strike_dev: list[float] = field(default_factory=list)
    k_call0: float = float("nan")
    k_put0: float = float("nan")
    legs: list[SimLeg] = field(default_factory=list)
    adjs: list[dict[str, Any]] = field(default_factory=list)
    expiry_net: float = float("nan")
    cash_mark: float = 0.0
    tsl_reason: str = ""
    tsl_ts: int = 0
    tsl_net: float = float("nan")


def _mark_pnl(lg: SimLeg, mark: float) -> float:
    return (float(mark) - float(lg.mark0)) * float(lg.qty) * float(OPTIONS_CONTRACT_VALUE)


def _cash_mark_closed(lg: SimLeg) -> float:
    em = float(lg.exit_mark) if math.isfinite(float(lg.exit_mark)) else float(lg.mark0)
    return (em - float(lg.mark0)) * float(lg.qty) * float(OPTIONS_CONTRACT_VALUE)


def reported_net(
    realized_mark: float,
    open_legs: list[tuple[SimLeg, float]],
    fees: float,
    slips: float,
    spot: float,
    include_est: bool,
) -> float:
    open_pnl = 0.0
    est = 0.0
    for lg, mk in open_legs:
        open_pnl += _mark_pnl(lg, mk)
        if include_est:
            est += est_exit_cost(mk, spot, lg.qty)
    return float(realized_mark) + open_pnl - float(fees) - float(slips) - est


def cash_conservation_net(legs: list[SimLeg], fees: float, slips: float) -> float:
    """Sum of mark cash flows minus fees minus slippage (all legs closed/settled)."""
    s = 0.0
    for lg in legs:
        s += _cash_mark_closed(lg)
    return s - float(fees) - float(slips)


def pick_strikes(
    rows: list[dict[str, Any]],
    spot: float,
    x: int,
    style: str,
) -> tuple[dict[str, Any], dict[str, Any], float, float, float] | None:
    atm = d.atm_strike_of(rows, float(spot))
    if atm is None:
        return None
    ks = sorted({float(r["strike"]) for r in rows})
    if style == "itm":
        t_call, t_put = float(atm) - float(x), float(atm) + float(x)
    else:
        t_call, t_put = float(atm) + float(x), float(atm) - float(x)
    k_call = d.nearest_listed(ks, t_call)
    k_put = d.nearest_listed(ks, t_put)
    if k_call is None or k_put is None:
        return None
    c_row = d.row_strike(rows, True, float(k_call))
    p_row = d.row_strike(rows, False, float(k_put))
    if c_row is None or p_row is None:
        return None
    return c_row, p_row, float(k_call) - t_call, float(k_put) - t_put, float(atm)


def _opp_symbol(rows: list[dict[str, Any]], is_call: bool, strike: float) -> str:
    r = d.row_strike(rows, (not bool(is_call)), float(strike))
    return str(r["symbol"]) if r is not None else ""


def _quote_with_parity(
    mkt: Market,
    symbol: str,
    t: int,
    *,
    is_call: bool,
    strike: float,
    exp: date,
    opp_symbol: str,
    spot: float | None,
    src_counts: dict[str, int] | None = None,
) -> tuple[float, str] | None:
    mkt.ensure(symbol, exp)
    q = mkt.quote(symbol, t)
    if q is not None:
        px, src = q
        src = map_src(src)
        if src != "none" and math.isfinite(float(px)):
            if src_counts is not None:
                src_counts[src] = src_counts.get(src, 0) + 1
            return float(px), src
    if not opp_symbol:
        if src_counts is not None:
            src_counts["none"] = src_counts.get("none", 0) + 1
        return None
    mkt.ensure(opp_symbol, exp)
    oq = mkt.quote(opp_symbol, t)
    sp = float(spot) if spot is not None else mkt.spot(t)
    if oq is None or sp is None or not math.isfinite(float(sp)):
        if src_counts is not None:
            src_counts["none"] = src_counts.get("none", 0) + 1
        return None
    px = parity_mark(bool(is_call), float(strike), float(sp), float(oq[0]))
    if src_counts is not None:
        src_counts["parity"] = src_counts.get("parity", 0) + 1
    return float(px), "parity"


def _open_leg(
    mkt: Market,
    row: dict[str, Any],
    exp: date,
    t: int,
    spot: float,
    role: str,
    rows: list[dict[str, Any]] | None = None,
    src_counts: dict[str, int] | None = None,
) -> SimLeg | None:
    sym = str(row["symbol"])
    is_call = bool(row["is_call"])
    strike = float(row["strike"])
    opp = _opp_symbol(rows or [], is_call, strike) if rows else ""
    q = _quote_with_parity(
        mkt, sym, t, is_call=is_call, strike=strike, exp=exp,
        opp_symbol=opp, spot=float(spot), src_counts=src_counts,
    )
    if q is None:
        return None
    px, src = q
    fee = fee_at(px, spot, QTY)
    sl = slip_usd(QTY)
    return SimLeg(
        role=role,
        symbol=sym,
        strike=strike,
        is_call=is_call,
        expiry=exp,
        mark0=float(px),
        fill=buy_fill(px),
        fee=fee,
        slip=sl,
        qty=QTY,
        ref=float(px),
        src=src,
        opened_ts=int(t),
        opp_symbol=opp,
    )


def _fill_rec(
    t: int,
    spot: float,
    lg: SimLeg,
    side: str,
    mark: float,
    src: str,
    is_buy: bool,
    net: float,
    peak: float,
    reason: str,
    a_pct: float = 0.0,
    gross: float = float("nan"),
    peak_gross: float = float("nan"),
) -> dict[str, Any]:
    fill = buy_fill(mark) if is_buy else (lg.exit_fill if side == "sell" and math.isfinite(lg.exit_fill) else sell_fill(mark))
    fee = lg.fee if is_buy else float(lg.exit_fee)
    slip = lg.slip if is_buy else float(lg.exit_slip)
    if side == "settle":
        fill = float(lg.exit_fill)
        fee = 0.0
        slip = 0.0
    t_yr = t_years(int(t), int(s018.expiry_unix(lg.expiry)))
    gk = greeks_from_mark(float(mark), float(spot), float(lg.strike), float(t_yr), bool(lg.is_call))
    return {
        "ts": int(t),
        "ist": s018.ist_str(int(t)),
        "spot": float(spot),
        "strike": float(lg.strike),
        "is_call": bool(lg.is_call),
        "side": side,
        "mark": float(mark),
        "mark_src": str(src),
        "fill": float(fill),
        "fee": float(fee),
        "slip": float(slip),
        "ref": float(lg.ref),
        "trigger": float(lg.ref) * (float(a_pct) / 100.0) if a_pct else float("nan"),
        "net": float(net),
        "peak": float(peak),
        "gross": float(gross),
        "peak_gross": float(peak_gross),
        "sl_level": float("nan"),
        "reason": reason,
        "symbol": lg.symbol,
        "iv": gk.get("iv", float("nan")),
        "delta": gk.get("delta", float("nan")),
    }


def _close_live(lg: SimLeg, t: int, mark: float, spot: float) -> None:
    lg.open = False
    lg.closed_ts = int(t)
    lg.exit_mark = float(mark)
    lg.exit_fill = sell_fill(mark)
    lg.exit_fee = fee_at(mark, spot, lg.qty)
    lg.exit_slip = slip_usd(lg.qty)
    lg.settled = False


def _settle(lg: SimLeg, t: int, spot: float) -> None:
    xf = float(intrinsic(lg.is_call, lg.strike, float(spot)))
    lg.open = False
    lg.closed_ts = int(t)
    lg.exit_mark = xf
    lg.exit_fill = xf
    lg.exit_fee = 0.0
    lg.exit_slip = 0.0
    lg.settled = True


def _stale_px(arr: np.ndarray, t_book: int, ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = int(arr.size)
    idx = ((ts.astype(np.int64) - int(t_book)) // 60).astype(np.int64)
    px = np.full(ts.shape, np.nan, dtype=np.float64)
    is_real = np.zeros(ts.shape, dtype=bool)
    filled = np.zeros(ts.shape, dtype=bool)
    for k in range(0, STALE_MIN + 1):
        j = idx - k
        good = (j >= 0) & (j < n) & (~filled)
        if not np.any(good):
            continue
        vals = arr[np.clip(j, 0, n - 1)]
        hit = good & np.isfinite(vals)
        px[hit] = vals[hit]
        if k == 0:
            is_real[hit] = True
        filled |= hit
    return px, is_real


def _vec_fee(prem: np.ndarray, spot: np.ndarray) -> np.ndarray:
    notional = spot * float(QTY) * OPTIONS_CONTRACT_VALUE * OPTION_FEE_RATE * GST
    cap = prem * float(QTY) * OPTIONS_CONTRACT_VALUE * PREMIUM_CAP_RATE * GST
    return np.minimum(notional, cap)


def _series(mkt: Market, symbol: str, ts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    fn = getattr(mkt, "mark_array", None)
    if callable(fn):
        got = fn(symbol)
        if got is not None:
            t_book, arr = got
            return _stale_px(np.asarray(arr, dtype=np.float64), int(t_book), ts)
    px = np.full(ts.shape, np.nan, dtype=np.float64)
    real = np.zeros(ts.shape, dtype=bool)
    for i, t in enumerate(ts.tolist()):
        q = mkt.quote(symbol, int(t))
        if q is None:
            continue
        p, src = q
        px[i] = float(p)
        real[i] = map_src(src) == "real"
    return px, real


def _apply_parity_vec(
    px: np.ndarray,
    is_real: np.ndarray,
    opp_px: np.ndarray,
    spots: np.ndarray,
    is_call: bool,
    strike: float,
) -> tuple[np.ndarray, np.ndarray]:
    """src: 1=real 2=stale 3=parity 0=none."""
    src = np.zeros(px.shape, dtype=np.int8)
    finite = np.isfinite(px)
    src[finite & is_real] = 1
    src[finite & (~is_real)] = 2
    miss = ~finite
    k = float(strike)
    if bool(is_call):
        par = opp_px + spots - k
        floor = np.maximum(spots - k, 0.0)
    else:
        par = opp_px - spots + k
        floor = np.maximum(k - spots, 0.0)
    par = np.maximum(par, floor)
    use = miss & np.isfinite(par) & np.isfinite(spots)
    out = px.copy()
    out[use] = par[use]
    src[use] = 3
    return out, src


def _spot_series(mkt: Market, ts: np.ndarray) -> np.ndarray:
    fn = getattr(mkt, "spot_array", None)
    if callable(fn):
        got = fn()
        if got is not None:
            t_book, arr = got
            px, _ = _stale_px(np.asarray(arr, dtype=np.float64), int(t_book), ts)
            return px
    out = np.full(ts.shape, np.nan, dtype=np.float64)
    for i, t in enumerate(ts.tolist()):
        v = mkt.spot(int(t))
        if v is not None:
            out[i] = float(v)
    return out


def _quote_open(
    mkt: Market, lg: SimLeg, t: int, src_counts: dict[str, int], spot: float | None = None,
) -> tuple[float, str] | None:
    return _quote_with_parity(
        mkt, lg.symbol, t,
        is_call=lg.is_call, strike=lg.strike, exp=lg.expiry,
        opp_symbol=lg.opp_symbol, spot=spot, src_counts=src_counts,
    )


def _adjust_one(
    mkt: Market,
    lg: SimLeg,
    t: int,
    spot: float,
    exp: date,
    x: int,
    style: str,
    src_counts: dict[str, int],
) -> SimLeg | None:
    packed = mkt.chain(exp, t)
    if packed is None:
        return None
    rows, _csrc = packed
    picked = pick_strikes(rows, spot, x, style)
    if picked is None:
        return None
    c_row, p_row, _dc, _dp, _atm = picked
    row = c_row if lg.is_call else p_row
    role = "C" if lg.is_call else "P"
    return _open_leg(mkt, row, exp, t, spot, role, rows=rows, src_counts=src_counts)


def simulate_path(
    mkt: Market,
    t0: int,
    exp: date,
    x: int,
    style: str,
    a_pct: float,
    minutes: list[int],
    tsl: tuple[float, float] | None = None,
    compact: bool = False,
    record_fills: bool = False,
) -> PathResult:
    """Run adjustments to expiry; record per-minute pre-adj net (T/SL view)."""
    src_counts = {"real": 0, "stale": 0, "parity": 0, "none": 0}
    out = PathResult(ok=False, skip_reason="", t0=int(t0), exp_ts=int(s018.expiry_unix(exp)))
    out.src_counts = src_counts
    spot0 = mkt.spot(int(t0))
    if spot0 is None or not math.isfinite(float(spot0)):
        out.skip_reason = "no_spot"
        return out
    packed = mkt.chain(exp, int(t0))
    if packed is None:
        out.skip_reason = "no_chain"
        return out
    rows, _ = packed
    picked = pick_strikes(rows, float(spot0), int(x), style)
    if picked is None:
        out.skip_reason = "no_strikes"
        return out
    c_row, p_row, d_c, d_p, _atm = picked
    out.strike_dev = [abs(d_c), abs(d_p)]
    call = _open_leg(mkt, c_row, exp, int(t0), float(spot0), "C", rows=rows, src_counts=src_counts)
    put = _open_leg(mkt, p_row, exp, int(t0), float(spot0), "P", rows=rows, src_counts=src_counts)
    if call is None or put is None:
        out.skip_reason = "entry_mark_none"
        return out
    out.k_call0 = call.strike
    out.k_put0 = put.strike
    live = [call, put]
    hist = [call, put]
    fees = call.fee + put.fee
    slips = call.slip + put.slip
    if record_fills:
        out.fills.append(
            _fill_rec(
                int(t0), float(spot0), call, "buy", call.mark0, call.src, True, 0.0, 0.0, "ENTRY",
                gross=0.0, peak_gross=0.0,
            )
        )
        out.fills.append(
            _fill_rec(
                int(t0), float(spot0), put, "buy", put.mark0, put.src, True, 0.0, 0.0, "ENTRY",
                gross=0.0, peak_gross=0.0,
            )
        )
    realized = 0.0
    n_adj = 0
    peak = 0.0
    peak_g = 0.0
    ratio = float(a_pct) / 100.0
    exp_ts = out.exp_ts

    ts_all = np.array([int(t) for t in minutes if int(t) >= int(t0)], dtype=np.int64)
    if ts_all.size == 0 or int(ts_all[0]) != int(t0):
        if ts_all.size == 0 or int(ts_all[0]) > int(t0):
            ts_all = np.concatenate([np.array([int(t0)], dtype=np.int64), ts_all])
    cursor = 0

    def _close_tsl(t: int, net_v: float, reason: str) -> PathResult:
        nonlocal fees, slips, realized
        spx = mkt.spot(t)
        if spx is None:
            out.skip_reason = "exit_spot_none"
            return out
        for lg in list(live):
            got = _quote_open(mkt, lg, t, src_counts, spot=float(spx))
            if got is None:
                out.skip_reason = "exit_mark_none"
                return out
            _close_live(lg, t, got[0], float(spx))
            if record_fills:
                gv = float(out.gross[-1]) if out.gross else 0.0
                pg = float(out.peak_gross[-1]) if out.peak_gross else 0.0
                nv = float(out.net[-1]) if out.net else 0.0
                pk = float(out.peak[-1]) if out.peak else 0.0
                out.fills.append(
                    _fill_rec(
                        t, float(spx), lg, "sell", got[0], got[1], False, nv, pk, "",
                        gross=gv, peak_gross=pg,
                    )
                )
            realized += _cash_mark_closed(lg)
            fees += lg.exit_fee
            slips += lg.exit_slip
        out.ok = True
        out.fees = fees
        out.slips = slips
        out.legs = hist
        out.n_adj_total = n_adj
        out.tsl_reason = reason
        out.tsl_ts = int(t)
        out.tsl_net = reported_net(realized, [], fees, slips, float(spx), False)
        out.cash_mark = realized
        out.expiry_net = out.tsl_net
        return out

    while cursor < int(ts_all.size):
        seg = ts_all[cursor:]
        spots = _spot_series(mkt, seg)
        if not np.isfinite(spots[0]):
            out.skip_reason = "no_spot"
            return out
        pxs: list[np.ndarray] = []
        srcs: list[np.ndarray] = []
        for lg in live:
            if lg.opp_symbol:
                mkt.ensure(lg.opp_symbol, lg.expiry)
            raw, is_r = _series(mkt, lg.symbol, seg)
            if lg.opp_symbol:
                opp_raw, _ = _series(mkt, lg.opp_symbol, seg)
            else:
                opp_raw = np.full(seg.shape, np.nan, dtype=np.float64)
            px, src = _apply_parity_vec(raw, is_r, opp_raw, spots, lg.is_call, lg.strike)
            pxs.append(px)
            srcs.append(src)
        at_exp = seg >= int(exp_ts)
        hit_adj: int | None = None
        for lg, px in zip(live, pxs):
            if lg.ref <= 0:
                continue
            h = np.flatnonzero(np.isfinite(px) & (px >= float(lg.ref) * ratio))
            if h.size:
                j = int(h[0])
                if hit_adj is None or j < hit_adj:
                    hit_adj = j
        end = int(seg.size)
        if hit_adj is not None:
            end = min(end, hit_adj + 1)
        exp_j = np.flatnonzero(at_exp)
        if exp_j.size:
            end = min(end, int(exp_j[0]) + 1)
        miss = ~np.isfinite(spots[:end])
        for px in pxs:
            miss = miss | (~np.isfinite(px[:end]))
        if np.any(miss):
            out.skip_reason = "path_mark_none"
            src_counts["none"] += int(np.sum(miss))
            return out
        if not np.isfinite(spots[0]) or any(not np.isfinite(px[0]) for px in pxs):
            out.skip_reason = "path_mark_none"
            src_counts["none"] += 1
            return out
        for src in srcs:
            slc = src[:end]
            src_counts["real"] += int(np.sum(slc == 1))
            src_counts["stale"] += int(np.sum(slc == 2))
            src_counts["parity"] += int(np.sum(slc == 3))
            src_counts["none"] += int(np.sum(slc == 0))
        open_pnl = np.zeros(end, dtype=np.float64)
        est = np.zeros(end, dtype=np.float64)
        for lg, px in zip(live, pxs):
            sl = px[:end]
            open_pnl += (sl - float(lg.mark0)) * MULT
            est += _vec_fee(sl, spots[:end]) + slip_usd(lg.qty)
        grosses = realized + open_pnl
        nets = grosses - float(fees) - float(slips) - est
        nets = np.where(at_exp[:end], grosses - float(fees) - float(slips), nets)

        stop_j: int | None = None
        stop_why = ""
        if tsl is not None:
            tgt, slv = float(tsl[0]), float(tsl[1])
            run_peak_n = float(peak)
            run_peak_g = float(peak_g)
            for j in range(end):
                if bool(at_exp[j]):
                    break
                nv = float(nets[j])
                gv = float(grosses[j])
                run_peak_n = max(run_peak_n, nv)
                run_peak_g = max(run_peak_g, gv)
                if nv >= tgt:
                    stop_j, stop_why = j, "TARGET"
                    break
                if gv <= sl_level(run_peak_g, slv):
                    stop_j, stop_why = j, "SL"
                    break
            if stop_j is not None:
                end = stop_j + 1

        n_live = len(live)
        est_slip_one = slip_usd(QTY) * float(n_live)
        for j in range(end):
            nv = float(nets[j])
            gv = float(grosses[j])
            peak = max(peak, nv)
            peak_g = max(peak_g, gv)
            out.ts.append(int(seg[j]))
            out.net.append(nv)
            out.peak.append(float(peak))
            out.gross.append(gv)
            out.peak_gross.append(float(peak_g))
            out.n_adj.append(int(n_adj))
            out.fees_m.append(float(fees))
            out.slip_m.append(float(slips))
            out.est_fee_m.append(float(est[j]) - est_slip_one)
            out.est_slip_m.append(est_slip_one)
        last_t = int(seg[end - 1])

        if stop_j is not None:
            ret = _close_tsl(last_t, float(nets[stop_j]), stop_why)
            if ret.ok:
                _finalize_path(ret, compact=compact)
            return ret

        if bool(at_exp[end - 1]):
            sp = float(spots[end - 1])
            for lg in list(live):
                _settle(lg, last_t, sp)
                realized += _cash_mark_closed(lg)
                if record_fills:
                    out.fills.append(
                        _fill_rec(
                            last_t, sp, lg, "settle", lg.exit_mark, "settle", False,
                            float(out.net[-1]) if out.net else 0.0,
                            float(out.peak[-1]) if out.peak else 0.0,
                            "EXPIRY",
                            gross=float(out.gross[-1]) if out.gross else 0.0,
                            peak_gross=float(out.peak_gross[-1]) if out.peak_gross else 0.0,
                        )
                    )
            live = []
            out.expiry_net = reported_net(realized, [], fees, slips, sp, False)
            out.cash_mark = realized
            out.tsl_reason = "EXPIRY"
            out.tsl_ts = last_t
            out.tsl_net = out.expiry_net
            break

        if hit_adj is not None and end - 1 == hit_adj:
            t = last_t
            sp = float(spots[hit_adj])
            progressed = True
            while progressed:
                progressed = False
                ranked: list[tuple[float, SimLeg, float]] = []
                for lg in live:
                    got = _quote_open(mkt, lg, t, src_counts, spot=sp)
                    if got is None:
                        out.skip_reason = "adj_mark_none"
                        return out
                    mk = got[0]
                    if lg.ref > 0 and mk >= float(lg.ref) * ratio:
                        ranked.append((mk / float(lg.ref), lg, mk))
                if not ranked:
                    break
                ranked.sort(key=lambda z: z[0], reverse=True)
                _ratio, lg, mk = ranked[0]
                _close_live(lg, t, mk, sp)
                realized += _cash_mark_closed(lg)
                fees += lg.exit_fee
                slips += lg.exit_slip
                live = [x for x in live if x.open]
                neu = _adjust_one(mkt, lg, t, sp, exp, int(x), style, src_counts)
                if neu is None:
                    out.skip_reason = "adj_entry_none"
                    return out
                live.append(neu)
                hist.append(neu)
                fees += neu.fee
                slips += neu.slip
                n_adj += 1
                if record_fills:
                    nv = float(out.net[-1]) if out.net else 0.0
                    pk = float(out.peak[-1]) if out.peak else 0.0
                    gv = float(out.gross[-1]) if out.gross else 0.0
                    pg = float(out.peak_gross[-1]) if out.peak_gross else 0.0
                    out.fills.append(
                        _fill_rec(t, sp, lg, "sell", mk, "real", False, nv, pk, "ADJ", a_pct, gross=gv, peak_gross=pg)
                    )
                    out.fills.append(
                        _fill_rec(
                            t, sp, neu, "buy", neu.mark0, neu.src, True, nv, pk, "ADJ", a_pct,
                            gross=gv, peak_gross=pg,
                        )
                    )
                out.adjs.append(
                    {
                        "ts": t,
                        "closed_strike": lg.strike,
                        "closed_fill": lg.exit_fill,
                        "closed_mark": mk,
                        "opened_strike": neu.strike,
                        "opened_fill": neu.fill,
                        "opened_mark": neu.mark0,
                        "is_call": lg.is_call,
                    }
                )
                progressed = True
            cursor += end
            continue
        cursor += end

    out.ok = True
    out.skip_reason = ""
    out.n_adj_total = n_adj
    out.fees = fees
    out.slips = slips
    out.legs = hist
    if not math.isfinite(out.expiry_net) and out.net:
        out.expiry_net = float(out.net[-1])
        out.cash_mark = realized
    _finalize_path(out, compact=compact)
    return out


def drop_series(path: PathResult) -> None:
    path.ts = []
    path.net = []
    path.peak = []
    path.gross = []
    path.peak_gross = []
    path.n_adj = []
    path.fees_m = []
    path.slip_m = []
    path.est_fee_m = []
    path.est_slip_m = []


def exit_table(path: PathResult) -> list[dict[str, Any]]:
    """24 (T,SL) first-exit rows. Costs are as of the exit minute, not expiry."""
    if not path.ok or not path.ts:
        return []
    ts = np.asarray(path.ts, dtype=np.int64)
    net = np.asarray(path.net, dtype=np.float64)
    peak = np.asarray(path.peak, dtype=np.float64)
    gross = np.asarray(path.gross, dtype=np.float64) if path.gross else np.zeros(ts.shape)
    peak_g = np.asarray(path.peak_gross, dtype=np.float64) if path.peak_gross else np.zeros(ts.shape)
    if gross.size != ts.size:
        gross = np.zeros(ts.shape)
    if peak_g.size != ts.size:
        peak_g = np.zeros(ts.shape)
    nadj = np.asarray(path.n_adj, dtype=np.int32)
    fees = np.asarray(path.fees_m, dtype=np.float64) if path.fees_m else np.zeros(ts.shape)
    slips = np.asarray(path.slip_m, dtype=np.float64) if path.slip_m else np.zeros(ts.shape)
    est_f = np.asarray(path.est_fee_m, dtype=np.float64) if path.est_fee_m else np.zeros(ts.shape)
    est_s = np.asarray(path.est_slip_m, dtype=np.float64) if path.est_slip_m else np.zeros(ts.shape)
    if est_f.size != ts.size:
        est_f = np.zeros(ts.shape)
    if est_s.size != ts.size:
        est_s = np.zeros(ts.shape)
    live = ts < int(path.exp_ts)
    rows: list[dict[str, Any]] = []
    for tgt in T_GRID:
        for slv in SL_GRID:
            lvl = -float(slv) + 5.0 * np.floor(np.maximum(0.0, peak_g) / 10.0)
            tgt_hit = live & (net >= float(tgt))
            sl_hit = live & (gross <= lvl)
            i_t = int(np.flatnonzero(tgt_hit)[0]) if np.any(tgt_hit) else None
            i_s = int(np.flatnonzero(sl_hit)[0]) if np.any(sl_hit) else None
            reason = "EXPIRY"
            i = int(ts.size - 1)
            if i_t is not None and (i_s is None or i_t <= i_s):
                reason, i = "TARGET", i_t
            elif i_s is not None:
                reason, i = "SL", i_s
            else:
                exp_i = np.flatnonzero(~live)
                if exp_i.size:
                    i = int(exp_i[0])
                    reason = "EXPIRY"
            if reason == "EXPIRY":
                net_x = float(path.expiry_net) if math.isfinite(path.expiry_net) else float(net[i])
                fee_x = float(fees[i])
                slip_x = float(slips[i])
            else:
                net_x = float(net[i])
                fee_x = float(fees[i]) + float(est_f[i])
                slip_x = float(slips[i]) + float(est_s[i])
            slc = net[: i + 1]
            rows.append(
                {
                    "T": int(tgt),
                    "SL": int(slv),
                    "exit_ts": int(ts[i]),
                    "reason": reason,
                    "net": net_x,
                    "gross_at_exit": float(gross[i]),
                    "n_adj_at_exit": int(nadj[i]),
                    "peak_at_exit": float(peak[i]),
                    "peak_gross_at_exit": float(peak_g[i]),
                    "mfe": float(np.max(slc)),
                    "mae": float(np.min(slc)),
                    "fees_at_exit": fee_x,
                    "slip_at_exit": slip_x,
                }
            )
    return rows


def _finalize_path(path: PathResult, compact: bool) -> None:
    if path.ok:
        path.exits = exit_table(path)
    if compact:
        drop_series(path)


def first_exit(
    path: PathResult,
    target: float,
    sl: float,
) -> tuple[int, str, float]:
    """First minute: TARGET on net, SL on gross (ratchet on peak_gross); else EXPIRY."""
    if not path.ok or not path.ts:
        return int(path.t0), "SKIP", float("nan")
    tgt = float(target)
    slv = float(sl)
    last_i = len(path.ts) - 1
    for i, t in enumerate(path.ts):
        n = float(path.net[i])
        g = float(path.gross[i]) if path.gross else n
        pg = float(path.peak_gross[i]) if path.peak_gross else float(path.peak[i])
        if t >= path.exp_ts:
            return int(t), "EXPIRY", float(path.expiry_net)
        if n >= tgt:
            return int(t), "TARGET", n
        if g <= sl_level(pg, slv):
            return int(t), "SL", n
    t = int(path.ts[last_i])
    return t, "EXPIRY", float(path.expiry_net)


def brute_exit(
    path: PathResult,
    target: float,
    sl: float,
) -> tuple[int, str, float]:
    """Same rule as first_exit; used to prove post-hoc == minute walk."""
    return first_exit(path, target, sl)


def take_in_order(
    events: list[dict[str, Any]],
    exits_by_key: dict[Any, list[dict[str, Any]]],
    skip_by_key: dict[Any, str],
    target: float,
    sl: float,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Walk events in time; skip while a basket is open. Returns taken, n_skipped, skip_reasons."""
    taken: list[dict[str, Any]] = []
    n_skipped = 0
    reasons: dict[str, int] = {}
    busy_until = -1
    prev_ts: int | None = None
    tgt, slv = int(target), int(sl)
    for e in sorted(events, key=lambda r: int(r["signal_ts"])):
        t0 = int(e["signal_ts"])
        key = e.get("path_key")
        if t0 <= busy_until:
            n_skipped += 1
            reasons["occupancy"] = reasons.get("occupancy", 0) + 1
            continue
        if prev_ts is not None and t0 == prev_ts:
            n_skipped += 1
            reasons["same_bar"] = reasons.get("same_bar", 0) + 1
            continue
        prev_ts = t0
        why = skip_by_key.get(key, "")
        rows = exits_by_key.get(key) or []
        row = next((r for r in rows if int(r["T"]) == tgt and int(r["SL"]) == slv), None)
        if row is None:
            n_skipped += 1
            label = why or "no_path"
            reasons[label] = reasons.get(label, 0) + 1
            continue
        taken.append({"event": e, **row})
        busy_until = int(row["exit_ts"])
    return taken, n_skipped, reasons
