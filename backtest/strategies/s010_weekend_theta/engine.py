"""S010 weekend-theta P&L engine.

Arm A: short D+2 ATM straddle + short D+2 OTM strangle + long D+3 ATM straddle.
Arm B: same shorts + long D+2 ATM straddle (no calendar). Full independent run.

Entry is D 18:00 IST close. Basket always ends by D+2 17:30 IST.
Look-ahead: chain and spot at entry use only the 18:00 bar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import numpy as np

from backtest.harness.data import MarksStore, ist_dt, load_symbol_series, to_unix
from backtest.strategies.s010_weekend_theta import config as cfg
from backtest.strategies.s010_weekend_theta.preflight import (
    ChainCache,
    format_symbol,
    load_spot_ohlc,
    spot_close_at,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s010.engine")

Arm = Literal["A", "B"]
SpotMap = dict[int, tuple[float, float, float, float]]


def ist_of(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST)


def ist_str(ts: int) -> str:
    return ist_of(ts).strftime("%Y-%m-%d %H:%M:%S")


def settlement_spot(spot: SpotMap, d: date) -> tuple[int, float] | None:
    """D+2 17:30 IST close; fallback 17:29. No other minute."""
    ts_1730 = to_unix(ist_dt(d, cfg.EXPIRY_HOUR_IST, cfg.EXPIRY_MINUTE_IST))
    px = spot_close_at(spot, ts_1730)
    if px is not None:
        return ts_1730, px
    ts_1729 = to_unix(ist_dt(d, cfg.EXPIRY_HOUR_IST, cfg.EXPIRY_MINUTE_IST - 1))
    px = spot_close_at(spot, ts_1729)
    if px is not None:
        return ts_1729, px
    return None


def candidate_dates(spot: SpotMap) -> list[date]:
    """Entry dates >= DATA_START that have 18:00 and a D+2 17:30/29 settle."""
    seen: set[date] = set()
    for ts in spot:
        dt = datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST)
        if dt.hour == cfg.ENTRY_HOUR_IST and dt.minute == cfg.ENTRY_MINUTE_IST:
            seen.add(dt.date())
    out: list[date] = []
    for d in sorted(seen):
        if d < cfg.DATA_START:
            continue
        exp2 = d + timedelta(days=2)
        if settlement_spot(spot, exp2) is None:
            continue
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------
def option_fee(premium: float, index_spot: float, qty_lots: int) -> float:
    notional = abs(int(qty_lots)) * cfg.LOT_BTC
    by_index = cfg.OPTION_FEE_INDEX_PCT * notional * index_spot
    by_prem = cfg.OPTION_FEE_PREMIUM_PCT * notional * premium
    return min(by_index, by_prem) * cfg.OPTION_FEE_GST_MULT


def option_slip_frac(premium: float) -> float:
    for lo, hi, frac in cfg.OPTION_SLIP_BUCKETS:
        if lo <= premium < hi:
            return frac
    return cfg.OPTION_SLIP_BUCKETS[-1][2]


def slip_usd(premium: float, qty_lots: int) -> float:
    return float(premium) * option_slip_frac(premium) * abs(int(qty_lots)) * cfg.LOT_BTC


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
@dataclass
class Leg:
    side: Literal["short", "long"]
    opt: Literal["call", "put"]
    expiry: date
    strike: float
    entry_mark: float
    qty: int
    symbol: str
    expires_d2: bool

    @property
    def sign(self) -> int:
        return -1 if self.side == "short" else 1


def pick_atm(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    spot: float,
) -> tuple[float, float, float] | None:
    c_by = {k: px for k, px in calls}
    p_by = {k: px for k, px in puts}
    common = sorted(set(c_by) & set(p_by))
    if not common:
        return None
    strike = min(common, key=lambda k: (abs(k - spot), k))
    if abs(strike - spot) > cfg.ATM_MAX_GAP:
        return None
    return strike, c_by[strike], p_by[strike]


def pick_strangle(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    atm_k: float,
    upper_be: float,
) -> tuple[float, float, float, float] | None:
    """OTM call = highest K with ATM < K <= upper_be.

    Put = OTM put (K < ATM) whose mark is closest to that call's mark.
    """
    c_otm = [(k, px) for k, px in calls if atm_k < k <= upper_be]
    if not c_otm:
        return None
    ck, cpx = max(c_otm, key=lambda kp: kp[0])
    p_otm = [(k, px) for k, px in puts if k < atm_k]
    if not p_otm:
        return None
    pk, ppx = min(p_otm, key=lambda kp: (abs(kp[1] - cpx), -kp[0]))
    return ck, cpx, pk, ppx


def intrinsic(opt: str, strike: float, spot: float) -> float:
    if opt == "call":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def expiry_pnl_at(legs: list[Leg], spot: float) -> float:
    lot = cfg.LOT_BTC
    pnl = 0.0
    for lg in legs:
        exit_px = intrinsic(lg.opt, lg.strike, spot)
        pnl += lg.sign * (exit_px - lg.entry_mark) * lg.qty * lot
    return pnl


def capital_used_usd(legs: list[Leg]) -> float:
    """Max theoretical loss at D+2 expiry from strikes + entry premiums."""
    strikes = sorted({lg.strike for lg in legs})
    lo = min(strikes[0] * 0.5, max(strikes[0] - 50_000.0, 1.0))
    hi = strikes[-1] + 50_000.0
    grid = [lo, hi, *strikes]
    # Midpoints catch piecewise-linear kinks between strikes.
    for a, b in zip(strikes, strikes[1:]):
        grid.append(0.5 * (a + b))
    min_pnl = min(expiry_pnl_at(legs, s) for s in grid)
    return cfg.MARGIN_MULT * max(-min_pnl, cfg.CAPITAL_FLOOR_USD)


# ---------------------------------------------------------------------------
# Basket
# ---------------------------------------------------------------------------
@dataclass
class BasketResult:
    d: date
    dow: str
    arm: Arm
    ts_on: bool
    entry_ts: int
    spot_entry: float
    atm_strike: float
    straddle_premium: float
    upper_be: float
    lower_be: float
    short_call_k: float
    short_call_prem: float
    short_put_k: float
    short_put_prem: float
    prot_expiry: date
    prot_strike: float
    prot_call_prem: float
    prot_put_prem: float
    atm_call_prem: float
    atm_put_prem: float
    prot_call_prem_exit: float
    prot_put_prem_exit: float
    prot_call_symbol: str
    prot_put_symbol: str
    prot_mark_source_exit: str
    entry_debit: float
    cap: float
    cap_violation: bool
    cause_a_missing_mark: bool
    cause_b_same_option_fail: bool
    cause_c_below_intrinsic: bool
    capital_used: float
    target_usd: float
    stop_usd: float
    exit_ts: int
    exit_reason: str
    spot_exit: float
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    worst_intracycle_mtm: float
    d2_exit_fee: float
    d2_exit_slip: float
    entry_fees: float
    prot_expiry_is_d2: bool
    skipped: bool = False
    skip_reason: str = ""
    legs: list[Leg] = field(default_factory=list)


def _leg(
    side: Literal["short", "long"],
    opt: Literal["call", "put"],
    expiry: date,
    strike: float,
    mark: float,
    qty: int,
    expires_d2: bool,
) -> Leg:
    return Leg(
        side=side,
        opt=opt,
        expiry=expiry,
        strike=strike,
        entry_mark=mark,
        qty=qty,
        symbol=format_symbol("C" if opt == "call" else "P", strike, expiry),
        expires_d2=expires_d2,
    )


def build_legs(
    d: date,
    spot_entry: float,
    store: MarksStore,
    chain_cache: ChainCache,
    arm: Arm,
    entry_ts: int,
) -> tuple[list[Leg], dict[str, float], str]:
    """Return (legs, meta, skip_reason). skip_reason empty on success."""
    exp2 = d + timedelta(days=2)
    exp3 = d + timedelta(days=3)
    c2, p2 = chain_cache.get(store, d, exp2, entry_ts)
    atm = pick_atm(c2, p2, spot_entry)
    if atm is None:
        return [], {}, "atm_miss"
    atm_k, atm_c, atm_p = atm
    straddle_p = atm_c + atm_p
    upper_be = atm_k + straddle_p
    lower_be = atm_k - straddle_p
    sg = pick_strangle(c2, p2, atm_k, upper_be)
    if sg is None:
        return [], {}, "no_otm_strangle"
    sc_k, sc_px, sp_k, sp_px = sg

    if arm == "A":
        c3, p3 = chain_cache.get(store, d, exp3, entry_ts)
        prot = pick_atm(c3, p3, spot_entry)
        if prot is None:
            return [], {}, "no_d3_atm"
        prot_exp = exp3
        prot_k, prot_c, prot_p = prot
        prot_d2 = False
    else:
        prot_exp = exp2
        prot_k, prot_c, prot_p = atm_k, atm_c, atm_p
        prot_d2 = True

    q_st = cfg.STRADDLE_QTY
    q_sg = cfg.STRANGLE_QTY
    q_pr = cfg.PROTECTION_QTY
    legs = [
        _leg("short", "call", exp2, atm_k, atm_c, q_st, True),
        _leg("short", "put", exp2, atm_k, atm_p, q_st, True),
        _leg("short", "call", exp2, sc_k, sc_px, q_sg, True),
        _leg("short", "put", exp2, sp_k, sp_px, q_sg, True),
        _leg("long", "call", prot_exp, prot_k, prot_c, q_pr, prot_d2),
        _leg("long", "put", prot_exp, prot_k, prot_p, q_pr, prot_d2),
    ]
    if any(lg.entry_mark <= 0 for lg in legs):
        return [], {}, "no_entry_mark"
    meta = {
        "atm_k": atm_k,
        "straddle_p": straddle_p,
        "upper_be": upper_be,
        "lower_be": lower_be,
        "sc_k": sc_k,
        "sc_px": sc_px,
        "sp_k": sp_k,
        "sp_px": sp_px,
        "prot_k": prot_k,
        "prot_c": prot_c,
        "prot_p": prot_p,
        "atm_c": atm_c,
        "atm_p": atm_p,
    }
    return legs, meta, ""


def classify_mark_source(
    series: dict[int, float], ts: int, *, seed: float, used: float
) -> str:
    """How the exit mark was obtained. Does not change the value used.

    db        = exact-minute close in the series
    stale     = nearest bar within MARK_TOL_SEC, not the exact minute
    fallback  = ffill from an earlier bar in the holding window
    missing   = no usable series point; engine kept the entry seed
    """
    minute = (int(ts) // 60) * 60
    exact = series.get(minute)
    if exact is not None and exact > 0:
        return "db"
    tol = cfg.MARK_TOL_SEC
    for delta in range(60, tol + 1, 60):
        for cand in (minute - delta, minute + delta):
            m = series.get(cand)
            if m is not None and m > 0:
                return "stale"
    earlier = [
        t for t, m in series.items() if t < minute and m is not None and m > 0
    ]
    if earlier:
        return "fallback"
    return "missing"


def arm_a_entry_debit(
    atm_call: float,
    atm_put: float,
    short_call: float,
    short_put: float,
    prot_call: float,
    prot_put: float,
) -> float:
    """Net debit in USD at 1:1:2 sizing (1000/1000/2000 × 0.001 BTC).

    2*(D+3 ATM) − (D+2 ATM) − (D+2 strangle), in premium-point units
    which equal USD at this lot size.
    """
    return (
        2.0 * (prot_call + prot_put)
        - (atm_call + atm_put)
        - (short_call + short_put)
    )


def _ffill_series(
    ts_arr: np.ndarray, series: dict[int, float], seed: float
) -> np.ndarray:
    out = np.empty(ts_arr.size, dtype=np.float64)
    last = float(seed)
    tol = cfg.MARK_TOL_SEC
    for i, t in enumerate(ts_arr):
        ti = int(t)
        m = series.get(ti)
        if m is None:
            for delta in range(60, tol + 1, 60):
                m = series.get(ti - delta) or series.get(ti + delta)
                if m is not None:
                    break
        if m is not None and m > 0:
            last = float(m)
        out[i] = last
    return out


def _spot_path(spot: SpotMap, ts_arr: np.ndarray, seed: float) -> np.ndarray:
    out = np.empty(ts_arr.size, dtype=np.float64)
    last = float(seed)
    for i, t in enumerate(ts_arr):
        px = spot_close_at(spot, int(t))
        if px is None:
            bar = spot.get(int(t))
            if bar is not None and bar[3] > 0:
                px = bar[3]
        if px is not None:
            last = float(px)
        out[i] = last
    return out


def _gross_at(
    legs: list[Leg], marks: list[float]
) -> float:
    lot = cfg.LOT_BTC
    g = 0.0
    for lg, m in zip(legs, marks):
        g += lg.sign * (m - lg.entry_mark) * lg.qty * lot
    return g


def simulate_day(
    *,
    d: date,
    spot: SpotMap,
    store: MarksStore,
    chain_cache: ChainCache,
    arm: Arm,
    ts_on: bool,
    zero_costs: bool = False,
) -> tuple[BasketResult | None, str]:
    dow = cfg.DOW_NAMES[d.weekday()]
    entry_ts = to_unix(ist_dt(d, cfg.ENTRY_HOUR_IST, cfg.ENTRY_MINUTE_IST))
    spot_entry = spot_close_at(spot, entry_ts)
    if spot_entry is None:
        return None, "no_spot_1800"
    exp2 = d + timedelta(days=2)
    settle = settlement_spot(spot, exp2)
    if settle is None:
        return None, "no_settle_spot"
    settle_ts, settle_spot_px = settle

    legs, meta, reason = build_legs(
        d, spot_entry, store, chain_cache, arm, entry_ts
    )
    if reason:
        return None, reason

    capital = capital_used_usd(legs)
    target_usd = cfg.TARGET_PCT * capital
    stop_usd = cfg.STOP_PCT * capital

    # Minute grid entry .. settle inclusive.
    n_min = int((settle_ts - entry_ts) // 60) + 1
    if n_min < 1:
        return None, "no_window"
    ts_arr = entry_ts + np.arange(n_min, dtype=np.int64) * 60
    spot_path = _spot_path(spot, ts_arr, spot_entry)

    series = [
        load_symbol_series(store, lg.symbol, entry_ts, settle_ts) for lg in legs
    ]
    mark_paths = [
        _ffill_series(ts_arr, ser, lg.entry_mark) for ser, lg in zip(series, legs)
    ]

    def marks_at(idx: int, *, settling: bool) -> list[float]:
        out: list[float] = []
        sp = float(spot_path[idx])
        for j, lg in enumerate(legs):
            if settling and lg.expires_d2:
                out.append(intrinsic(lg.opt, lg.strike, sp))
            else:
                out.append(float(mark_paths[j][idx]))
        return out

    # Arm A hold-to-expiry needs a live D+3 mark at settle.
    last = n_min - 1
    if arm == "A":
        prot_marks = marks_at(last, settling=True)
        if any(
            (not lg.expires_d2) and prot_marks[j] <= 0
            for j, lg in enumerate(legs)
        ):
            return None, "no_exit_mark"

    exit_idx = last
    exit_reason = "EXPIRY"
    worst = 0.0
    if ts_on:
        for i in range(n_min):
            settling = i == last
            mtm = _gross_at(legs, marks_at(i, settling=settling))
            if mtm < worst:
                worst = mtm
            hit_stop = mtm <= -stop_usd
            hit_tgt = mtm >= target_usd
            if hit_stop and hit_tgt:
                exit_idx, exit_reason = i, "STOP"
                break
            if hit_stop:
                exit_idx, exit_reason = i, "STOP"
                break
            if hit_tgt:
                exit_idx, exit_reason = i, "TARGET"
                break
    else:
        for i in range(n_min):
            settling = i == last
            mtm = _gross_at(legs, marks_at(i, settling=settling))
            if mtm < worst:
                worst = mtm

    settling = exit_reason == "EXPIRY"
    exit_marks = marks_at(exit_idx, settling=settling)
    exit_spot = float(spot_path[exit_idx])
    exit_ts = int(ts_arr[exit_idx])
    gross = _gross_at(legs, exit_marks)

    if zero_costs:
        fees = slippage = entry_fees = d2_exit_fee = d2_exit_slip = 0.0
    else:
        entry_fees = sum(
            option_fee(lg.entry_mark, spot_entry, lg.qty) for lg in legs
        )
        entry_slip = sum(slip_usd(lg.entry_mark, lg.qty) for lg in legs)
        exit_fees = 0.0
        exit_slip = 0.0
        d2_exit_fee = 0.0
        d2_exit_slip = 0.0
        for lg, em in zip(legs, exit_marks):
            expires_now = settling and lg.expires_d2
            if expires_now:
                continue  # expiry is free
            f = option_fee(em, exit_spot, lg.qty)
            s = slip_usd(em, lg.qty)
            exit_fees += f
            exit_slip += s
            if lg.expires_d2:
                d2_exit_fee += f
                d2_exit_slip += s
        fees = entry_fees + exit_fees
        slippage = entry_slip + exit_slip

    net = gross - fees - slippage

    prot_call_lg = next(lg for lg in legs if lg.side == "long" and lg.opt == "call")
    prot_put_lg = next(lg for lg in legs if lg.side == "long" and lg.opt == "put")
    prot_call_exit = float(exit_marks[legs.index(prot_call_lg)])
    prot_put_exit = float(exit_marks[legs.index(prot_put_lg)])
    src_c = classify_mark_source(
        series[legs.index(prot_call_lg)],
        exit_ts,
        seed=prot_call_lg.entry_mark,
        used=prot_call_exit,
    )
    src_p = classify_mark_source(
        series[legs.index(prot_put_lg)],
        exit_ts,
        seed=prot_put_lg.entry_mark,
        used=prot_put_exit,
    )
    prot_src = src_c if src_c == src_p else f"call:{src_c}|put:{src_p}"

    want_c = format_symbol("C", prot_call_lg.strike, prot_call_lg.expiry)
    want_p = format_symbol("P", prot_put_lg.strike, prot_put_lg.expiry)
    same_option_ok = (
        prot_call_lg.symbol == want_c
        and prot_put_lg.symbol == want_p
        and prot_call_lg.strike == meta["prot_k"]
        and prot_put_lg.strike == meta["prot_k"]
        and prot_call_lg.expiry == (d + timedelta(days=2 if arm == "B" else 3))
        and prot_put_lg.expiry == prot_call_lg.expiry
    )

    entry_debit = arm_a_entry_debit(
        meta["atm_c"],
        meta["atm_p"],
        meta["sc_px"],
        meta["sp_px"],
        meta["prot_c"],
        meta["prot_p"],
    )
    total_cost = fees + slippage
    inv_cap = -(entry_debit + total_cost) - 1.0
    cap_violation = bool(arm == "A" and net < inv_cap)

    cause_a = bool(
        arm == "A"
        and (
            src_c == "missing"
            or src_p == "missing"
            or prot_call_exit <= 0.0
            or prot_put_exit <= 0.0
        )
    )
    cause_b = bool(arm == "A" and not same_option_ok)
    call_intr = intrinsic("call", prot_call_lg.strike, exit_spot)
    put_intr = intrinsic("put", prot_put_lg.strike, exit_spot)
    cause_c = bool(
        arm == "A"
        and (
            prot_call_exit < call_intr - 1e-6
            or prot_put_exit < put_intr - 1e-6
        )
    )

    res = BasketResult(
        d=d,
        dow=dow,
        arm=arm,
        ts_on=ts_on,
        entry_ts=entry_ts,
        spot_entry=spot_entry,
        atm_strike=meta["atm_k"],
        straddle_premium=meta["straddle_p"],
        upper_be=meta["upper_be"],
        lower_be=meta["lower_be"],
        short_call_k=meta["sc_k"],
        short_call_prem=meta["sc_px"],
        short_put_k=meta["sp_k"],
        short_put_prem=meta["sp_px"],
        prot_expiry=d + timedelta(days=2 if arm == "B" else 3),
        prot_strike=meta["prot_k"],
        prot_call_prem=meta["prot_c"],
        prot_put_prem=meta["prot_p"],
        atm_call_prem=meta["atm_c"],
        atm_put_prem=meta["atm_p"],
        prot_call_prem_exit=prot_call_exit,
        prot_put_prem_exit=prot_put_exit,
        prot_call_symbol=prot_call_lg.symbol,
        prot_put_symbol=prot_put_lg.symbol,
        prot_mark_source_exit=prot_src,
        entry_debit=entry_debit,
        cap=inv_cap,
        cap_violation=cap_violation,
        cause_a_missing_mark=cause_a,
        cause_b_same_option_fail=cause_b,
        cause_c_below_intrinsic=cause_c,
        capital_used=capital,
        target_usd=target_usd,
        stop_usd=stop_usd,
        exit_ts=exit_ts,
        exit_reason=exit_reason,
        spot_exit=exit_spot,
        gross_pnl=gross,
        fees=fees,
        slippage=slippage,
        net_pnl=net,
        worst_intracycle_mtm=worst,
        d2_exit_fee=d2_exit_fee,
        d2_exit_slip=d2_exit_slip,
        entry_fees=entry_fees,
        prot_expiry_is_d2=(arm == "B"),
        legs=legs,
    )
    return res, ""
