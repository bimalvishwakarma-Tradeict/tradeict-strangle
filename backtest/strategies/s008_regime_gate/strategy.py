"""S008 regime-gated 0DTE short strangle — hold to settlement."""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from backtest.harness.config import CONTRACT_VALUE
from backtest.harness.costs import fill_price, option_fee
from backtest.harness.data import MarksStore, ist_dt, to_unix
from backtest.strategies.s008_regime_gate.signal import (
    DaySignal,
    ExpandingSignalComputer,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s008.strategy")

IS_FROM = date(2025, 7, 4)
IS_TO = date(2026, 3, 31)
OOS_FROM = date(2026, 4, 1)
OOS_TO = date(2026, 9, 20)

WING_PTS = 2000.0
DEFAULT_QTY = 100
MARK_TOL_SEC = 60

GateMode = Literal["none", "switch", "flat"]
Side = Literal["sell", "buy", "flat"]


def format_symbol(opt: str, strike: float, exp: date) -> str:
    prefix = "C" if opt.lower().startswith("c") else "P"
    return f"{prefix}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def zero_dte_expiry(d: date) -> date:
    """0DTE expiry = calendar trading day (ISO date in marks)."""
    return d


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
    half_width: float = 6000.0,
    step: float = 100.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
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


def nearest_strike(strikes: list[float], target: float) -> float | None:
    """Nearest available strike to target. Caller must enforce gap guard —
    silent wide snaps are forbidden (S006 lesson)."""
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - target), k))


@dataclass(frozen=True)
class WingPick:
    atm: float
    target_call_k: float
    target_put_k: float
    chosen_call_k: float
    chosen_put_k: float
    call_gap: float
    put_gap: float
    call_mark: float
    put_mark: float

    @property
    def max_gap(self) -> float:
        return max(self.call_gap, self.put_gap)


def pick_atm_wings(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    spot: float,
    wing_pts: float = WING_PTS,
) -> WingPick | None:
    """ATM±wing selection. Returns targets + chosen + gaps; does NOT skip.
    Gap enforcement lives in simulate_day via max_strike_gap."""
    c_strikes = [k for k, _ in calls]
    p_strikes = [k for k, _ in puts]
    common = sorted(set(c_strikes) & set(p_strikes))
    atm = nearest_strike(common if common else c_strikes, spot)
    if atm is None:
        return None
    target_c = atm + wing_pts
    target_p = atm - wing_pts
    ck = nearest_strike(c_strikes, target_c)
    pk = nearest_strike(p_strikes, target_p)
    if ck is None or pk is None:
        return None
    c_by = {k: px for k, px in calls}
    p_by = {k: px for k, px in puts}
    return WingPick(
        atm=atm,
        target_call_k=target_c,
        target_put_k=target_p,
        chosen_call_k=ck,
        chosen_put_k=pk,
        call_gap=abs(ck - target_c),
        put_gap=abs(pk - target_p),
        call_mark=c_by[ck],
        put_mark=p_by[pk],
    )


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
    # flat
    if sig >= threshold:
        return "flat"
    return "sell"


DEFAULT_MAX_STRIKE_GAP = 400.0


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
    threshold: float
    prev_rvol: float
    overnight: float


@dataclass
class RunStats:
    n_days: int = 0
    n_traded: int = 0
    n_flat_gate: int = 0
    n_skip_data: int = 0
    n_strike_unavailable: int = 0
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
            threshold=self.threshold,
            prev_rvol=sig.prev_rvol,
            overnight=sig.overnight,
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
        calls, puts = load_chain_pk(conn, expiry, entry_ts, float(spot_entry))
        picked = pick_atm_wings(calls, puts, float(spot_entry), self.wing_pts)
        if picked is None:
            return self._empty_basket(
                d=d,
                side=side,
                sig=sig,
                skip_reason="no_strikes",
                entry_ts=entry_ts,
                spot_entry=float(spot_entry),
            )

        # Strike gap guard — never silently snap beyond max_strike_gap
        strike_ok = (
            picked.call_gap <= self.max_strike_gap
            and picked.put_gap <= self.max_strike_gap
        )
        if not strike_ok:
            return self._empty_basket(
                d=d,
                side=side,
                sig=sig,
                skip_reason="STRIKE_UNAVAILABLE",
                entry_ts=entry_ts,
                spot_entry=float(spot_entry),
                target_call_k=picked.target_call_k,
                target_put_k=picked.target_put_k,
                chosen_call_k=picked.chosen_call_k,
                chosen_put_k=picked.chosen_put_k,
                call_gap=picked.call_gap,
                put_gap=picked.put_gap,
                strike_ok=False,
            )

        ck = picked.chosen_call_k
        pk = picked.chosen_put_k
        c_mark = picked.call_mark
        p_mark = picked.put_mark

        # dte=0 for slip buckets
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
        # slip cost ≈ |fill-mark| * qty * CV
        slip_cost = (
            abs(c_fill - c_mark) + abs(p_fill - p_mark)
        ) * self.qty * CONTRACT_VALUE

        # Settlement intrinsic (USD)
        c_intr = call_intrinsic(settle_px, ck) * self.qty * CONTRACT_VALUE
        p_intr = put_intrinsic(settle_px, pk) * self.qty * CONTRACT_VALUE
        settlement_pay = c_intr + p_intr

        prem_notional = (c_fill + p_fill) * self.qty * CONTRACT_VALUE
        if side == "sell":
            # credit received, pay settlement
            premium_pnl = prem_notional
            gross = premium_pnl - settlement_pay
        else:
            # debit paid, receive settlement
            premium_pnl = -prem_notional
            gross = settlement_pay + premium_pnl

        # EXIT COST ZERO by design
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
            threshold=self.threshold,
            prev_rvol=sig.prev_rvol,
            overnight=sig.overnight,
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
