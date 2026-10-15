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
from backtest.strategies.s012_trend_follow.engine import intrinsic, ist_date
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


def map_src(raw: str) -> str:
    s = str(raw).strip().lower()
    if s == "real":
        return "real"
    if s in ("stale", "ffill"):
        return "ffill"
    if s in ("none", ""):
        return "none"
    return s


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
    n_adj: list[int] = field(default_factory=list)
    src_counts: dict[str, int] = field(default_factory=lambda: {"real": 0, "ffill": 0, "none": 0})
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


def _open_leg(
    mkt: Market,
    row: dict[str, Any],
    exp: date,
    t: int,
    spot: float,
    role: str,
) -> SimLeg | None:
    sym = str(row["symbol"])
    mkt.ensure(sym, exp)
    q = mkt.quote(sym, t)
    if q is None:
        return None
    px, src = q
    src = map_src(src)
    if src == "none" or not math.isfinite(px):
        return None
    fee = fee_at(px, spot, QTY)
    sl = slip_usd(QTY)
    return SimLeg(
        role=role,
        symbol=sym,
        strike=float(row["strike"]),
        is_call=bool(row["is_call"]),
        expiry=exp,
        mark0=float(px),
        fill=buy_fill(px),
        fee=fee,
        slip=sl,
        qty=QTY,
        ref=float(px),
        src=src,
        opened_ts=int(t),
    )


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
    mkt: Market, lg: SimLeg, t: int, src_counts: dict[str, int]
) -> tuple[float, str] | None:
    q = mkt.quote(lg.symbol, t)
    if q is None:
        src_counts["none"] += 1
        return None
    px, src = q
    src = map_src(src)
    src_counts[src] = src_counts.get(src, 0) + 1
    if src == "none" or not math.isfinite(px):
        return None
    return float(px), src


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
    mkt.ensure(str(row["symbol"]), exp)
    q = mkt.quote(str(row["symbol"]), t)
    if q is None:
        src_counts["none"] += 1
        return None
    px, src = q
    src = map_src(src)
    src_counts[src] = src_counts.get(src, 0) + 1
    if src == "none":
        return None
    role = "C" if lg.is_call else "P"
    return _open_leg(mkt, row, exp, t, spot, role)


def simulate_path(
    mkt: Market,
    t0: int,
    exp: date,
    x: int,
    style: str,
    a_pct: float,
    minutes: list[int],
    tsl: tuple[float, float] | None = None,
) -> PathResult:
    """Run adjustments to expiry; record per-minute pre-adj net (T/SL view)."""
    src_counts = {"real": 0, "ffill": 0, "none": 0}
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
    call = _open_leg(mkt, c_row, exp, int(t0), float(spot0), "C")
    put = _open_leg(mkt, p_row, exp, int(t0), float(spot0), "P")
    if call is None or put is None:
        out.skip_reason = "entry_mark_none"
        src_counts["none"] += 1
        return out
    src_counts[call.src] = src_counts.get(call.src, 0) + 1
    src_counts[put.src] = src_counts.get(put.src, 0) + 1
    out.k_call0 = call.strike
    out.k_put0 = put.strike
    live = [call, put]
    hist = [call, put]
    fees = call.fee + put.fee
    slips = call.slip + put.slip
    realized = 0.0
    n_adj = 0
    peak = 0.0
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
            got = _quote_open(mkt, lg, t, src_counts)
            if got is None:
                out.skip_reason = "exit_mark_none"
                return out
            _close_live(lg, t, got[0], float(spx))
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
        reals: list[np.ndarray] = []
        for lg in live:
            px, is_r = _series(mkt, lg.symbol, seg)
            if not np.isfinite(px[0]):
                out.skip_reason = "path_mark_none"
                src_counts["none"] += 1
                return out
            pxs.append(px)
            reals.append(is_r)
        open_pnl = np.zeros(seg.shape, dtype=np.float64)
        est = np.zeros(seg.shape, dtype=np.float64)
        for lg, px in zip(live, pxs):
            open_pnl += (px - float(lg.mark0)) * MULT
            est += _vec_fee(px, spots) + slip_usd(lg.qty)
        at_exp = seg >= int(exp_ts)
        nets = realized + open_pnl - float(fees) - float(slips) - est
        nets = np.where(at_exp, realized + open_pnl - float(fees) - float(slips), nets)
        miss = ~np.isfinite(spots)
        for px in pxs:
            miss = miss | (~np.isfinite(px))
        if np.any(miss):
            out.skip_reason = "path_mark_none"
            src_counts["none"] += int(np.sum(miss))
            return out
        src_counts["real"] += int(sum(int(np.sum(r)) for r in reals))
        src_counts["ffill"] += int(sum(int(r.size - np.sum(r)) for r in reals))

        hit_adj: int | None = None
        for lg, px in zip(live, pxs):
            if lg.ref <= 0:
                continue
            h = np.flatnonzero(px >= float(lg.ref) * ratio)
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

        stop_j: int | None = None
        stop_why = ""
        if tsl is not None:
            tgt, slv = float(tsl[0]), float(tsl[1])
            run_peak = float(peak)
            for j in range(end):
                if bool(at_exp[j]):
                    break
                nv = float(nets[j])
                run_peak = max(run_peak, nv)
                if nv >= tgt:
                    stop_j, stop_why = j, "TARGET"
                    break
                if nv <= sl_level(run_peak, slv):
                    stop_j, stop_why = j, "SL"
                    break
            if stop_j is not None:
                end = stop_j + 1

        for j in range(end):
            nv = float(nets[j])
            peak = max(peak, nv)
            out.ts.append(int(seg[j]))
            out.net.append(nv)
            out.peak.append(float(peak))
            out.n_adj.append(int(n_adj))
        last_t = int(seg[end - 1])

        if stop_j is not None:
            return _close_tsl(last_t, float(nets[stop_j]), stop_why)

        if bool(at_exp[end - 1]):
            sp = float(spots[end - 1])
            for lg in list(live):
                _settle(lg, last_t, sp)
                realized += _cash_mark_closed(lg)
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
                    got = _quote_open(mkt, lg, t, src_counts)
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
    return out


def first_exit(
    path: PathResult,
    target: float,
    sl: float,
) -> tuple[int, str, float]:
    """First minute where T/SL hits on the stored pre-adj net; else EXPIRY."""
    if not path.ok or not path.ts:
        return int(path.t0), "SKIP", float("nan")
    tgt = float(target)
    slv = float(sl)
    last_i = len(path.ts) - 1
    for i, t in enumerate(path.ts):
        n = float(path.net[i])
        pk = float(path.peak[i])
        if t >= path.exp_ts:
            return int(t), "EXPIRY", float(path.expiry_net)
        if n >= tgt:
            return int(t), "TARGET", n
        if n <= sl_level(pk, slv):
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
    paths: dict[Any, PathResult],
    target: float,
    sl: float,
) -> list[dict[str, Any]]:
    """Walk events in time; skip while a basket is open for this cell."""
    taken: list[dict[str, Any]] = []
    busy_until = -1
    for e in sorted(events, key=lambda r: int(r["signal_ts"])):
        t0 = int(e["signal_ts"])
        if t0 <= busy_until:
            continue
        key = e.get("path_key")
        p = paths.get(key)
        if p is None or not p.ok:
            continue
        xts, reason, net = first_exit(p, target, sl)
        taken.append(
            {
                "event": e,
                "exit_ts": xts,
                "reason": reason,
                "net": net,
                "n_adj": p.n_adj_total,
            }
        )
        busy_until = int(xts)
    return taken
