"""S008 regime-gated 0DTE short strangle — hold to settlement."""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from backtest.harness.config import CONTRACT_VALUE
from backtest.harness.costs import fill_price, option_fee
from backtest.harness.data import MarksStore, ist_dt, to_unix
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection
from backtest.strategies.s008_regime_gate.signal import DaySignal

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s008.strategy")

IS_FROM = date(2025, 7, 4)
IS_TO = date(2026, 3, 31)
OOS_FROM = date(2026, 4, 1)
OOS_TO = date(2026, 9, 20)

WING_PTS = 2000.0
STRIKE_GRID = 200.0
DEFAULT_QTY = 100
MARK_TOL_SEC = 60
DEFAULT_MAX_STRIKE_GAP = 400.0
# Corrected 0DTE premium target: percent of spot (NOT the old 0.28571% 1DTE cap).
# 0.034% of spot ≈ ATM±2000 premium level.
DEFAULT_PREMIUM_TARGET_PCT = 0.034
DEFAULT_TARGET_DELTA = 0.12
# Reject any leg richer than this percent of spot (~3x a normal ATM±2000 leg).
DEFAULT_MAX_LEG_PREMIUM_PCT = 0.10
# 09:00 → 17:30 IST remaining fraction of year for Black-76
T_YEARS_0DTE_0900 = 8.5 / 24.0 / 365.0

GateMode = Literal["none", "switch", "flat"]
Side = Literal["sell", "buy", "flat"]
StrikeMode = Literal["points", "premium", "delta"]


def premium_target_usd(spot: float, premium_target_pct: float) -> float:
    """premium_target_pct is percent-of-spot (e.g. 0.034 → 0.034% of spot)."""
    return float(spot) * (float(premium_target_pct) / 100.0)


def format_symbol(opt: str, strike: float, exp: date) -> str:
    prefix = "C" if opt.lower().startswith("c") else "P"
    return f"{prefix}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def zero_dte_expiry(d: date) -> date:
    """0DTE expiry = calendar trading day (ISO date in marks)."""
    return d


def round_strike_grid(x: float, grid: float = STRIKE_GRID) -> float:
    return round(x / grid) * grid


def spot_based_targets(
    spot: float, wing_pts: float = WING_PTS, grid: float = STRIKE_GRID
) -> tuple[float, float]:
    """
    target_call_K = round((spot + wing)/grid)*grid
    target_put_K  = round((spot - wing)/grid)*grid
    Hard assert: call target > spot and put target < spot.
    """
    tc = round_strike_grid(spot + wing_pts, grid)
    tp = round_strike_grid(spot - wing_pts, grid)
    if not (tc > spot and tp < spot):
        raise AssertionError(
            f"target OTM assert fail: spot={spot} call_K={tc} put_K={tp}"
        )
    return tc, tp


def mark_at(
    conn: sqlite3.Connection, symbol: str, ts: int
) -> tuple[int, float] | None:
    minute = (ts // 60) * 60
    row = conn.execute(
        "SELECT ts, close FROM marks WHERE symbol=? AND ts=?",
        (symbol, minute),
    ).fetchone()
    if row is not None and row[1] is not None and float(row[1]) > 0:
        return int(row[0]), float(row[1])
    best: tuple[int, float] | None = None
    best_abs: int | None = None
    for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
        if d == 0:
            continue
        row = conn.execute(
            "SELECT ts, close FROM marks WHERE symbol=? AND ts=?",
            (symbol, minute + d),
        ).fetchone()
        if row is None or row[1] is None or float(row[1]) <= 0:
            continue
        ad = abs(int(row[0]) - minute)
        if best_abs is None or ad < best_abs:
            best_abs = ad
            best = (int(row[0]), float(row[1]))
    return best


def load_chain_pk(
    conn: sqlite3.Connection,
    expiry: date,
    ts: int,
    spot: float,
    *,
    half_width: float = 15000.0,
    step: float = 100.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Load marks near spot via PK symbol lookups (wide window for premium mode)."""
    atm0 = round(spot / step) * step
    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    k = atm0 - half_width
    while k <= atm0 + half_width:
        cm = mark_at(conn, format_symbol("C", k, expiry), ts)
        pm = mark_at(conn, format_symbol("P", k, expiry), ts)
        if cm is not None:
            calls.append((k, cm[1]))
        if pm is not None:
            puts.append((k, pm[1]))
        k += step
    return calls, puts


def load_chain_sql(
    conn: sqlite3.Connection, expiry: date, ts: int
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """All strikes at exact minute for expiry (fallback / full chain)."""
    minute = (ts // 60) * 60
    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    for opt, bucket in (("call", calls), ("put", puts)):
        rows = conn.execute(
            """
            SELECT strike, close FROM marks
            WHERE expiry=? AND ts=? AND opt_type=? AND close IS NOT NULL AND close > 0
            ORDER BY strike
            """,
            (expiry.isoformat(), minute, opt),
        ).fetchall()
        for strike, close in rows:
            bucket.append((float(strike), float(close)))
    return calls, puts


def nearest_strike(strikes: list[float], target: float) -> float | None:
    """Nearest available strike to target. Caller enforces gap / OTM guards."""
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - target), k))


def nearest_premium(
    legs: list[tuple[float, float]], target_prem: float
) -> tuple[float, float] | None:
    """Strike whose mark is closest to target premium. Tie → nearer ATM later."""
    if not legs:
        return None
    return min(legs, key=lambda kp: (abs(kp[1] - target_prem), kp[0]))


def pick_delta_strike(
    legs: list[tuple[float, float]],
    spot: float,
    *,
    target_delta: float,
    is_call: bool,
    t_years: float = T_YEARS_0DTE_0900,
) -> tuple[float, float] | None:
    """OTM strike with |delta| closest to target (Black-76 IV from mark)."""
    best: tuple[float, float, float] | None = None  # err, k, mark
    for k, mark in legs:
        if is_call and k <= spot:
            continue
        if not is_call and k >= spot:
            continue
        if mark <= 0:
            continue
        iv = implied_vol_bisection(mark, spot, k, t_years, is_call)
        if iv is None or iv <= 0:
            continue
        dlt = black76_abs_delta(spot, k, t_years, iv, is_call)
        err = abs(dlt - target_delta)
        if best is None or err < best[0] or (
            err == best[0] and abs(k - spot) < abs(best[1] - spot)
        ):
            best = (err, k, mark)
    if best is None:
        return None
    return best[1], best[2]


@dataclass(frozen=True)
class WingPick:
    target_call_k: float
    target_put_k: float
    chosen_call_k: float
    chosen_put_k: float
    call_gap: float
    put_gap: float
    call_mark: float
    put_mark: float
    strikes_available: bool
    skip_reason: str  # "" if ok to trade after guards; else reason code

    @property
    def max_gap(self) -> float:
        return max(self.call_gap, self.put_gap)


def _otm_calls(
    calls: list[tuple[float, float]], spot: float
) -> list[tuple[float, float]]:
    return [(k, px) for k, px in calls if k > spot]


def _otm_puts(
    puts: list[tuple[float, float]], spot: float
) -> list[tuple[float, float]]:
    return [(k, px) for k, px in puts if k < spot]


def pick_wings(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    spot: float,
    *,
    strike_mode: StrikeMode = "points",
    wing_pts: float = WING_PTS,
    max_strike_gap: float = DEFAULT_MAX_STRIKE_GAP,
    premium_target_pct: float = DEFAULT_PREMIUM_TARGET_PCT,
    target_delta: float = DEFAULT_TARGET_DELTA,
    max_leg_premium_pct: float = DEFAULT_MAX_LEG_PREMIUM_PCT,
    t_years: float = T_YEARS_0DTE_0900,
) -> WingPick:
    """
    Spot-based targets + OTM-only + (points: gap guard | premium: prem match).

    Order: OTM filter → select → OTM assert on chosen → gap (points only)
    → leg premium band (all modes).
    Never silently snaps past max_strike_gap or trades a leg richer than
    max_leg_premium_pct of spot.
    """
    target_c, target_p = spot_based_targets(spot, wing_pts)
    otm_c = _otm_calls(calls, spot)
    otm_p = _otm_puts(puts, spot)

    if not otm_c or not otm_p:
        return WingPick(
            target_call_k=target_c,
            target_put_k=target_p,
            chosen_call_k=0.0,
            chosen_put_k=0.0,
            call_gap=0.0,
            put_gap=0.0,
            call_mark=0.0,
            put_mark=0.0,
            strikes_available=False,
            skip_reason="CHAIN_ONE_SIDED",
        )

    c_by = {k: px for k, px in otm_c}
    p_by = {k: px for k, px in otm_p}

    if strike_mode == "premium":
        tgt_prem = premium_target_usd(spot, premium_target_pct)
        c_pick = nearest_premium(otm_c, tgt_prem)
        p_pick = nearest_premium(otm_p, tgt_prem)
        if c_pick is None or p_pick is None:
            return WingPick(
                target_call_k=target_c,
                target_put_k=target_p,
                chosen_call_k=0.0,
                chosen_put_k=0.0,
                call_gap=0.0,
                put_gap=0.0,
                call_mark=0.0,
                put_mark=0.0,
                strikes_available=False,
                skip_reason="CHAIN_ONE_SIDED",
            )
        ck, c_mark = c_pick
        pk, p_mark = p_pick
    elif strike_mode == "delta":
        c_pick = pick_delta_strike(
            otm_c, spot, target_delta=target_delta, is_call=True, t_years=t_years
        )
        p_pick = pick_delta_strike(
            otm_p, spot, target_delta=target_delta, is_call=False, t_years=t_years
        )
        if c_pick is None or p_pick is None:
            return WingPick(
                target_call_k=target_c,
                target_put_k=target_p,
                chosen_call_k=0.0,
                chosen_put_k=0.0,
                call_gap=0.0,
                put_gap=0.0,
                call_mark=0.0,
                put_mark=0.0,
                strikes_available=False,
                skip_reason="CHAIN_ONE_SIDED",
            )
        ck, c_mark = c_pick
        pk, p_mark = p_pick
    else:
        ck = nearest_strike(list(c_by.keys()), target_c)
        pk = nearest_strike(list(p_by.keys()), target_p)
        if ck is None or pk is None:
            return WingPick(
                target_call_k=target_c,
                target_put_k=target_p,
                chosen_call_k=0.0,
                chosen_put_k=0.0,
                call_gap=0.0,
                put_gap=0.0,
                call_mark=0.0,
                put_mark=0.0,
                strikes_available=False,
                skip_reason="CHAIN_ONE_SIDED",
            )
        c_mark = c_by[ck]
        p_mark = p_by[pk]

    # Hard OTM guard (before gap) — should already hold via filter
    if not (ck > spot and pk < spot):
        return WingPick(
            target_call_k=target_c,
            target_put_k=target_p,
            chosen_call_k=ck,
            chosen_put_k=pk,
            call_gap=abs(ck - target_c),
            put_gap=abs(pk - target_p),
            call_mark=c_mark,
            put_mark=p_mark,
            strikes_available=False,
            skip_reason="ITM_STRIKE",
        )

    call_gap = abs(ck - target_c)
    put_gap = abs(pk - target_p)

    if strike_mode == "points":
        if call_gap > max_strike_gap or put_gap > max_strike_gap:
            return WingPick(
                target_call_k=target_c,
                target_put_k=target_p,
                chosen_call_k=ck,
                chosen_put_k=pk,
                call_gap=call_gap,
                put_gap=put_gap,
                call_mark=c_mark,
                put_mark=p_mark,
                strikes_available=False,
                skip_reason="STRIKE_UNAVAILABLE",
            )

    # Leg premium band — all modes. A thin one-sided chain can leave only
    # near-ATM strikes, which are far richer than the intended wing.
    band_usd = premium_target_usd(spot, max_leg_premium_pct)
    if c_mark > band_usd or p_mark > band_usd:
        return WingPick(
            target_call_k=target_c,
            target_put_k=target_p,
            chosen_call_k=ck,
            chosen_put_k=pk,
            call_gap=call_gap,
            put_gap=put_gap,
            call_mark=c_mark,
            put_mark=p_mark,
            strikes_available=False,
            skip_reason="LEG_PREMIUM_OUT_OF_BAND",
        )

    return WingPick(
        target_call_k=target_c,
        target_put_k=target_p,
        chosen_call_k=ck,
        chosen_put_k=pk,
        call_gap=call_gap,
        put_gap=put_gap,
        call_mark=c_mark,
        put_mark=p_mark,
        strikes_available=True,
        skip_reason="",
    )


# Back-compat alias used by older probes
def pick_atm_wings(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    spot: float,
    wing_pts: float = WING_PTS,
) -> WingPick | None:
    pick = pick_wings(calls, puts, spot, strike_mode="points", wing_pts=wing_pts)
    return pick


def settlement_spot(
    spot_close: dict[int, float], d: date
) -> tuple[int, float] | None:
    """Prefer 17:30 IST close; fallback 17:29. Never any other minute."""
    for hm in ((17, 30), (17, 29)):
        ts = to_unix(ist_dt(d, hm[0], hm[1]))
        px = spot_close.get(ts)
        if px is not None and px > 0:
            return ts, float(px)
    return None


def call_intrinsic(s: float, k: float) -> float:
    return max(0.0, s - k)


def put_intrinsic(s: float, k: float) -> float:
    return max(0.0, k - s)


def decide_side(gate: GateMode, sig: float, threshold: float) -> Side:
    if gate == "none":
        return "sell"
    if gate == "switch":
        return "buy" if sig >= threshold else "sell"
    if sig >= threshold:
        return "flat"
    return "sell"


def sig_decile(sig: float) -> int:
    """1..10 from expanding-rank sig in [0,1]."""
    if sig < 0:
        return 1
    if sig >= 1.0:
        return 10
    return int(sig * 10) + 1


@dataclass
class BasketResult:
    d: date
    gate: str
    side: str
    skipped: bool
    skip_reason: str
    entry_ts: int
    entry_hour: int
    entry_minute: int
    spot_entry: float
    call_strike: float
    put_strike: float
    target_call_k: float
    target_put_k: float
    chosen_call_k: float
    chosen_put_k: float
    call_gap: float
    put_gap: float
    strike_ok: bool
    strikes_available: bool
    strike_mode: str
    call_mark: float
    put_mark: float
    call_fill: float
    put_fill: float
    qty: int
    entry_fee: float
    entry_slip_cost: float
    exit_fee: float
    exit_slip_cost: float
    settle_ts: int
    settle_spot: float
    settle_hour: int
    settle_minute: int
    call_payoff: float
    put_payoff: float
    settlement_pay: float
    premium_pnl: float
    gross_pnl: float
    net_pnl: float
    sig: float
    sig_decile: int
    threshold: float
    premium_target_pct: float
    target_delta: float
    prev_rvol: float
    overnight: float
    overnight_move_pct: float
    gate_decision: str


@dataclass
class RunStats:
    n_days: int = 0
    n_traded: int = 0
    n_flat_gate: int = 0
    n_skip_data: int = 0
    n_strike_unavailable: int = 0
    n_chain_one_sided: int = 0
    baskets: list[BasketResult] = field(default_factory=list)


class S008RegimeGateStrategy:
    def __init__(
        self,
        *,
        gate: GateMode = "none",
        threshold: float = 0.90,
        entry_hour: int = 9,
        entry_minute: int = 0,
        qty: int = DEFAULT_QTY,
        wing_pts: float = WING_PTS,
        max_strike_gap: float = DEFAULT_MAX_STRIKE_GAP,
        strike_mode: StrikeMode = "points",
        premium_target_pct: float = DEFAULT_PREMIUM_TARGET_PCT,
        target_delta: float = DEFAULT_TARGET_DELTA,
        max_leg_premium_pct: float = DEFAULT_MAX_LEG_PREMIUM_PCT,
        slip_model: str = "bucketed",
        slip_mult: float = 1.0,
        window: str = "is",
    ) -> None:
        self.gate: GateMode = gate
        self.threshold = float(threshold)
        self.entry_hour = int(entry_hour)
        self.entry_minute = int(entry_minute)
        self.qty = int(qty)
        self.wing_pts = float(wing_pts)
        self.max_strike_gap = float(max_strike_gap)
        self.strike_mode: StrikeMode = strike_mode
        self.premium_target_pct = float(premium_target_pct)
        self.target_delta = float(target_delta)
        self.max_leg_premium_pct = float(max_leg_premium_pct)
        self.slip_model = slip_model
        self.slip_mult = float(slip_mult)
        self.window = str(window).lower().strip()
        if self.window == "oos" and (
            threshold is None or (isinstance(threshold, float) and math.isnan(threshold))
        ):
            raise ValueError("OOS window requires explicit --threshold (no search)")

    def _empty_basket(
        self,
        *,
        d: date,
        side: str,
        sig: DaySignal,
        skip_reason: str,
        spot_entry: float = 0.0,
        entry_ts: int = 0,
        target_call_k: float = 0.0,
        target_put_k: float = 0.0,
        chosen_call_k: float = 0.0,
        chosen_put_k: float = 0.0,
        call_gap: float = 0.0,
        put_gap: float = 0.0,
        strike_ok: bool = False,
        strikes_available: bool = False,
    ) -> BasketResult:
        return BasketResult(
            d=d,
            gate=self.gate,
            side=side,
            skipped=True,
            skip_reason=skip_reason,
            entry_ts=entry_ts,
            entry_hour=self.entry_hour,
            entry_minute=self.entry_minute,
            spot_entry=spot_entry,
            call_strike=chosen_call_k,
            put_strike=chosen_put_k,
            target_call_k=target_call_k,
            target_put_k=target_put_k,
            chosen_call_k=chosen_call_k,
            chosen_put_k=chosen_put_k,
            call_gap=call_gap,
            put_gap=put_gap,
            strike_ok=strike_ok,
            strikes_available=strikes_available,
            strike_mode=self.strike_mode,
            call_mark=0.0,
            put_mark=0.0,
            call_fill=0.0,
            put_fill=0.0,
            qty=self.qty,
            entry_fee=0.0,
            entry_slip_cost=0.0,
            exit_fee=0.0,
            exit_slip_cost=0.0,
            settle_ts=0,
            settle_spot=0.0,
            settle_hour=0,
            settle_minute=0,
            call_payoff=0.0,
            put_payoff=0.0,
            settlement_pay=0.0,
            premium_pnl=0.0,
            gross_pnl=0.0,
            net_pnl=0.0,
            sig=sig.sig,
            sig_decile=sig_decile(sig.sig),
            threshold=self.threshold,
            premium_target_pct=self.premium_target_pct,
            target_delta=self.target_delta,
            prev_rvol=sig.prev_rvol,
            overnight=sig.overnight,
            overnight_move_pct=sig.overnight * 100.0,
            gate_decision=side,
        )

    def simulate_day(
        self,
        *,
        d: date,
        sig: DaySignal,
        store: MarksStore,
        spot_close: dict[int, float],
    ) -> BasketResult:
        side = decide_side(self.gate, sig.sig, self.threshold)
        if side == "flat":
            return self._empty_basket(d=d, side=side, sig=sig, skip_reason="gate_flat")

        entry_ts = to_unix(ist_dt(d, self.entry_hour, self.entry_minute))
        spot_entry = spot_close.get(entry_ts)
        if spot_entry is None or spot_entry <= 0:
            return self._empty_basket(
                d=d, side=side, sig=sig, skip_reason="no_spot_entry", entry_ts=entry_ts
            )

        settle = settlement_spot(spot_close, d)
        if settle is None:
            return self._empty_basket(
                d=d,
                side=side,
                sig=sig,
                skip_reason="no_settle_spot",
                entry_ts=entry_ts,
                spot_entry=float(spot_entry),
            )
        settle_ts, settle_px = settle
        settle_dt = datetime.fromtimestamp(settle_ts, tz=UTC).astimezone(IST)

        conn = store.conn(d)
        if conn is None:
            return self._empty_basket(
                d=d,
                side=side,
                sig=sig,
                skip_reason="no_marks",
                entry_ts=entry_ts,
                spot_entry=float(spot_entry),
            )
        expiry = zero_dte_expiry(d)
        if self.strike_mode in ("premium", "delta"):
            calls, puts = load_chain_sql(conn, expiry, entry_ts)
            if not calls and not puts:
                calls, puts = load_chain_pk(
                    conn, expiry, entry_ts, float(spot_entry)
                )
        else:
            calls, puts = load_chain_pk(conn, expiry, entry_ts, float(spot_entry))

        picked = pick_wings(
            calls,
            puts,
            float(spot_entry),
            strike_mode=self.strike_mode,
            wing_pts=self.wing_pts,
            max_strike_gap=self.max_strike_gap,
            premium_target_pct=self.premium_target_pct,
            target_delta=self.target_delta,
            max_leg_premium_pct=self.max_leg_premium_pct,
        )
        if picked.skip_reason:
            return self._empty_basket(
                d=d,
                side=side,
                sig=sig,
                skip_reason=picked.skip_reason,
                entry_ts=entry_ts,
                spot_entry=float(spot_entry),
                target_call_k=picked.target_call_k,
                target_put_k=picked.target_put_k,
                chosen_call_k=picked.chosen_call_k,
                chosen_put_k=picked.chosen_put_k,
                call_gap=picked.call_gap,
                put_gap=picked.put_gap,
                strike_ok=False,
                strikes_available=picked.strikes_available,
            )

        ck = picked.chosen_call_k
        pk = picked.chosen_put_k
        c_mark = picked.call_mark
        p_mark = picked.put_mark

        c_fill, _c_sf = fill_price(
            c_mark,
            "sell" if side == "sell" else "buy",
            dte=0,
            slip_model=self.slip_model,
            slip_mult=self.slip_mult,
        )
        p_fill, _p_sf = fill_price(
            p_mark,
            "sell" if side == "sell" else "buy",
            dte=0,
            slip_model=self.slip_model,
            slip_mult=self.slip_mult,
        )
        fee = option_fee(c_fill, float(spot_entry), self.qty) + option_fee(
            p_fill, float(spot_entry), self.qty
        )
        slip_cost = (
            abs(c_fill - c_mark) + abs(p_fill - p_mark)
        ) * self.qty * CONTRACT_VALUE

        c_intr = call_intrinsic(settle_px, ck) * self.qty * CONTRACT_VALUE
        p_intr = put_intrinsic(settle_px, pk) * self.qty * CONTRACT_VALUE
        settlement_pay = c_intr + p_intr

        prem_notional = (c_fill + p_fill) * self.qty * CONTRACT_VALUE
        if side == "sell":
            premium_pnl = prem_notional
            gross = premium_pnl - settlement_pay
        else:
            premium_pnl = -prem_notional
            gross = settlement_pay + premium_pnl

        exit_fee = 0.0
        exit_slip = 0.0
        net = gross - fee - exit_fee

        return BasketResult(
            d=d,
            gate=self.gate,
            side=side,
            skipped=False,
            skip_reason="",
            entry_ts=entry_ts,
            entry_hour=self.entry_hour,
            entry_minute=self.entry_minute,
            spot_entry=float(spot_entry),
            call_strike=ck,
            put_strike=pk,
            target_call_k=picked.target_call_k,
            target_put_k=picked.target_put_k,
            chosen_call_k=ck,
            chosen_put_k=pk,
            call_gap=picked.call_gap,
            put_gap=picked.put_gap,
            strike_ok=True,
            strikes_available=True,
            strike_mode=self.strike_mode,
            call_mark=c_mark,
            put_mark=p_mark,
            call_fill=c_fill,
            put_fill=p_fill,
            qty=self.qty,
            entry_fee=fee,
            entry_slip_cost=slip_cost,
            exit_fee=exit_fee,
            exit_slip_cost=exit_slip,
            settle_ts=settle_ts,
            settle_spot=settle_px,
            settle_hour=settle_dt.hour,
            settle_minute=settle_dt.minute,
            call_payoff=c_intr,
            put_payoff=p_intr,
            settlement_pay=settlement_pay,
            premium_pnl=premium_pnl,
            gross_pnl=gross,
            net_pnl=net,
            sig=sig.sig,
            sig_decile=sig_decile(sig.sig),
            threshold=self.threshold,
            premium_target_pct=self.premium_target_pct,
            target_delta=self.target_delta,
            prev_rvol=sig.prev_rvol,
            overnight=sig.overnight,
            overnight_move_pct=sig.overnight * 100.0,
            gate_decision=side,
        )


def iter_weekdays(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def enforce_oos_threshold(window: str, threshold: float | None) -> float:
    """Hard lock: OOS must receive threshold; never invent one."""
    w = str(window).lower().strip()
    if w == "oos":
        if threshold is None:
            raise ValueError(
                "HARD LOCK: --window oos requires --threshold "
                "(threshold must not be computed inside OOS)"
            )
        return float(threshold)
    if threshold is None:
        raise ValueError("--threshold required")
    return float(threshold)
