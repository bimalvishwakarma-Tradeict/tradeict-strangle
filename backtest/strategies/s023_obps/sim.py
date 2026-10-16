"""S023 OBPS basket: 1DTE ITM1000 long strangle + ATM short, 3-step lock."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE, estimate_option_fee
from backtest.harness.data import ist_dt, to_unix
from backtest.strategies.s012_trend_follow.engine import intrinsic, ist_date
from backtest.strategies.s020_vwap_touch import run_s020_dev as d
from backtest.strategies.s022_ulp_long_strangle.sim import resolve_slip_pts

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
CV = float(OPTIONS_CONTRACT_VALUE)
GST = 1.18
FEE_CAP_PER_1000 = 10.0
LOCK_USD_PER_1000 = 70.0
TARGET_USD_PER_1000 = 210.0
SL0_USD_PER_1000 = 210.0
ITM_PTS = 1000.0
STRIKE_MAX_DEV = 500.0
HOURS_0DTE_CUT = 5.5
SLIP_MODEL = "table"
SLIP_MULT = 3.2
STALE_MIN = 5
DEFAULT_QTY = 1000


class Market(Protocol):
    def spot(self, t: int) -> float | None: ...
    def quote(self, symbol: str, t: int) -> tuple[float, str] | None: ...
    def chain(self, exp: date, t: int) -> tuple[list[dict[str, Any]], str] | None: ...
    def ensure(self, symbol: str, exp: date) -> None: ...


def scale_of(qty: int) -> float:
    return float(qty) / float(DEFAULT_QTY)


def target_usd(qty: int) -> float:
    return TARGET_USD_PER_1000 * scale_of(qty)


def sl0_usd(qty: int) -> float:
    return SL0_USD_PER_1000 * scale_of(qty)


def lock_usd(qty: int) -> float:
    return LOCK_USD_PER_1000 * scale_of(qty)


def sl_now(steps: int, qty: int) -> float:
    """SL ladder: 0:-210, 1:-140, 2:-70 (scaled)."""
    n = max(0, min(2, int(steps)))
    return -float((210 - 70 * n) * scale_of(qty))


def protection_qty(qty: int, avg_prem: float) -> int:
    if avg_prem <= 0:
        return 0
    return int(math.floor(70.0 * float(qty) / float(avg_prem) * 3.0))


def step_qty(qty: int, prem: float) -> int:
    return protection_qty(qty, prem)


def fee_fill(premium: float, spot: float, qty: int) -> float:
    if int(qty) <= 0:
        return 0.0
    raw = float(estimate_option_fee(premium=float(premium), qty_lots=int(qty), btc_index=float(spot))) * GST
    cap = FEE_CAP_PER_1000 * float(qty) / float(DEFAULT_QTY)
    return float(min(raw, cap))


def slip_usd(premium: float, dte: int, qty: int, slip_mult: float = SLIP_MULT) -> float:
    pts = resolve_slip_pts(float(premium), int(dte), SLIP_MODEL, slip_mult=float(slip_mult))
    return float(pts) * float(qty) * CV


def expiry_unix(exp: date) -> int:
    return to_unix(ist_dt(exp, 17, 30))


def expiries_after(t: int, listed: list[date]) -> list[date]:
    tt = int(t)
    return sorted(e for e in listed if expiry_unix(e) > tt)


def dte0_dte1(t: int, listed: list[date]) -> tuple[date | None, date | None]:
    xs = expiries_after(t, listed)
    e0 = xs[0] if xs else None
    e1 = xs[1] if len(xs) > 1 else None
    return e0, e1


def hours_to(exp: date, t: int) -> float:
    return max(0.0, (expiry_unix(exp) - int(t)) / 3600.0)


def dte_days(exp: date, t: int) -> int:
    return max(0, (exp - ist_date(int(t))).days)


def _ddmmyy(exp: date) -> str:
    return exp.strftime("%d%m%y")


def opt_symbol(is_call: bool, strike: float, exp: date) -> str:
    pref = "C" if is_call else "P"
    return f"{pref}-BTC-{int(round(float(strike)))}-{_ddmmyy(exp)}"


@dataclass
class Leg:
    role: str
    is_call: bool
    strike: float
    expiry: date
    qty: int
    sign: int
    symbol: str
    fill_px: float
    fill_ts: int
    open: bool = True
    settled: bool = False
    exit_ts: int = 0
    exit_px: float = float("nan")
    realized: float = 0.0
    is_step: bool = False


@dataclass
class BasketResult:
    ok: bool
    skip_reason: str = ""
    entry_ts: int = 0
    exit_ts: int = 0
    exit_reason: str = ""
    signal_price: float = float("nan")
    e1: date | None = None
    legs: list[Leg] = field(default_factory=list)
    step1: dict[str, Any] | None = None
    step2: dict[str, Any] | None = None
    redeploys: int = 0
    gross: float = 0.0
    fees: float = 0.0
    slip: float = 0.0
    net: float = 0.0
    net_with_slip: float = 0.0
    max_gross: float = 0.0
    min_gross: float = 0.0
    hold_min: int = 0
    stale_minutes: int = 0
    steps: int = 0
    step_strike_gap: float = 0.0


def _mtm(lg: Leg, mark: float) -> float:
    return float(lg.sign) * float(lg.qty) * CV * (float(mark) - float(lg.fill_px))


class _MarkCache:
    def __init__(self, mkt: Market) -> None:
        self.mkt = mkt
        self.last: dict[str, tuple[int, float]] = {}
        self.stale_minutes = 0

    def px(self, symbol: str, t: int, exp: date) -> float | None:
        self.mkt.ensure(symbol, exp)
        q = self.mkt.quote(symbol, t)
        if q is not None:
            px, src = q
            age_ok = str(src) != "stale"
            self.last[symbol] = (int(t), float(px))
            if not age_ok:
                self.stale_minutes += 1
            return float(px)
        hit = self.last.get(symbol)
        if hit is None:
            self.stale_minutes += 1
            return None
        self.stale_minutes += 1
        return float(hit[1])

    def next_px(self, symbol: str, t0: int, exp: date, horizon: int = 3600) -> tuple[int, float] | None:
        t = int(t0)
        end = t + int(horizon)
        while t <= end:
            px = self.px(symbol, t, exp)
            if px is not None:
                return t, px
            t += 60
        return None


def _chain_rows(mkt: Market, exp: date, t: int) -> list[dict[str, Any]]:
    packed = mkt.chain(exp, t)
    if packed is None:
        return []
    rows, _src = packed
    return list(rows)


def _pick_k(ks: list[float], target: float) -> float | None:
    k = d.nearest_listed(ks, float(target))
    if k is None:
        return None
    if abs(float(k) - float(target)) >= STRIKE_MAX_DEV:
        return None
    return float(k)


def _leg_row(rows: list[dict[str, Any]], is_call: bool, strike: float) -> dict[str, Any] | None:
    return d.row_strike(rows, bool(is_call), float(strike))


def is_late_window(e0: date | None, t: int) -> bool:
    """Prereg: hours_to(0DTE, t) <= 5.5 is late (boundary inclusive)."""
    if e0 is None:
        return True
    return hours_to(e0, t) <= HOURS_0DTE_CUT


def resolve_listed_strike(
    rows: list[dict[str, Any]],
    is_call: bool,
    wanted: float,
) -> tuple[float, float]:
    """Exact listed strike, else nearest listed of that type. Never ATM."""
    if _leg_row(rows, is_call, wanted) is not None:
        return float(wanted), 0.0
    ks = sorted({float(r["strike"]) for r in rows if bool(r["is_call"]) == bool(is_call)})
    k = d.nearest_listed(ks, float(wanted))
    if k is None:
        k = d.nearest_listed(sorted({float(r["strike"]) for r in rows}), float(wanted))
    if k is None:
        return float(wanted), 0.0
    return float(k), abs(float(k) - float(wanted))


def simulate_basket(
    mkt: Market,
    listed: list[date],
    *,
    entry_ts: int,
    signal_price: float,
    qty: int = DEFAULT_QTY,
    ohlc: dict[int, tuple[float, float, float]] | None = None,
    slip_mult: float = SLIP_MULT,
    max_minutes: int = 80_000,
    event_log: list[dict[str, Any]] | None = None,
) -> BasketResult:
    t0 = int(entry_ts)
    Q = int(qty)
    tgt = target_usd(Q)
    lock = lock_usd(Q)
    e0, e1 = dte0_dte1(t0, listed)
    if e1 is None:
        return BasketResult(False, "missing_expiry")
    spot0 = mkt.spot(t0)
    if spot0 is None or spot0 <= 0:
        return BasketResult(False, "missing_spot")
    rows = _chain_rows(mkt, e1, t0)
    if not rows:
        return BasketResult(False, "missing_chain")
    atm = d.atm_strike_of(rows, float(spot0))
    if atm is None:
        return BasketResult(False, "missing_atm")
    ks = sorted({float(r["strike"]) for r in rows})
    k_c = _pick_k(ks, float(atm) - ITM_PTS)
    k_p = _pick_k(ks, float(atm) + ITM_PTS)
    if k_c is None or k_p is None:
        return BasketResult(False, "missing_strike")
    rc = _leg_row(rows, True, float(k_c))
    rp = _leg_row(rows, False, float(k_p))
    r_atm_c = _leg_row(rows, True, float(atm))
    r_atm_p = _leg_row(rows, False, float(atm))
    if rc is None or rp is None or r_atm_c is None or r_atm_p is None:
        return BasketResult(False, "missing_strike")
    cache = _MarkCache(mkt)

    def peek_mark(lg: Leg, tt: int) -> float | None:
        q = mkt.quote(lg.symbol, tt)
        if q is not None:
            return float(q[0])
        hit = cache.last.get(lg.symbol)
        return float(hit[1]) if hit is not None else None

    def peek_gross(tt: int) -> float:
        g = float(realized)
        for lg in legs:
            if not lg.open:
                continue
            px = peek_mark(lg, tt)
            if px is None:
                continue
            g += _mtm(lg, px)
        return g

    def emit(
        event: str,
        *,
        trigger_ts: int,
        fill_ts: int,
        lg: Leg | None,
        price: float,
        fee: float,
        slip: float,
    ) -> None:
        if event_log is None:
            return
        closing = event.startswith("EXIT") or event == "EXPIRY_SETTLE"
        side = ""
        if lg is not None:
            if closing:
                side = "sell" if lg.sign > 0 else "buy"
            else:
                side = "buy" if lg.sign > 0 else "sell"
        event_log.append(
            {
                "event": event,
                "trigger_ts": int(trigger_ts),
                "fill_ts": int(fill_ts),
                "leg": "" if lg is None else lg.role,
                "symbol": "" if lg is None else lg.symbol,
                "is_call": None if lg is None else lg.is_call,
                "strike": None if lg is None else lg.strike,
                "expiry": None if lg is None else lg.expiry,
                "side": side,
                "qty": 0 if lg is None else int(lg.qty),
                "price": float(price),
                "fee": float(fee),
                "slip": float(slip),
                "gross_after": peek_gross(int(fill_ts)),
                "sl_after": sl_now(steps, Q),
            }
        )

    def open_leg(
        role: str,
        is_call: bool,
        strike: float,
        exp: date,
        qn: int,
        sign: int,
        t: int,
        is_step: bool = False,
    ) -> Leg | None:
        if qn <= 0:
            return None
        row = None
        ch = _chain_rows(mkt, exp, t)
        if ch:
            row = _leg_row(ch, is_call, strike)
        sym = str(row["symbol"]) if row is not None else opt_symbol(is_call, strike, exp)
        got = cache.next_px(sym, t, exp)
        if got is None:
            return None
        ft, px = got
        sp = mkt.spot(ft) or spot0
        fees_acc[0] += fee_fill(px, float(sp), qn)
        slips_acc[0] += slip_usd(px, dte_days(exp, ft), qn, slip_mult)
        return Leg(
            role=role,
            is_call=is_call,
            strike=float(strike),
            expiry=exp,
            qty=int(qn),
            sign=int(sign),
            symbol=sym,
            fill_px=float(px),
            fill_ts=int(ft),
            is_step=bool(is_step),
        )

    fees_acc = [0.0]
    slips_acc = [0.0]
    gaps_acc = [0.0]
    realized = 0.0
    steps = 0
    legs: list[Leg] = []
    px_c = cache.px(str(r_atm_c["symbol"]), t0, e1)
    px_p = cache.px(str(r_atm_p["symbol"]), t0, e1)
    if px_c is None or px_p is None:
        return BasketResult(False, "missing_marks")
    avg = 0.5 * (float(px_c) + float(px_p))
    sq = protection_qty(Q, avg)
    if sq <= 0:
        return BasketResult(False, "missing_qty")
    for spec in (
        ("main_call", True, k_c, e1, Q, 1, False),
        ("main_put", False, k_p, e1, Q, 1, False),
        ("short_call", True, float(atm), e1, sq, -1, False),
        ("short_put", False, float(atm), e1, sq, -1, False),
    ):
        lg = open_leg(*spec[:6], t0, spec[6])
        if lg is None:
            return BasketResult(False, "missing_marks")
        legs.append(lg)
        sp_f = mkt.spot(lg.fill_ts) or spot0
        emit(
            "ENTRY",
            trigger_ts=t0,
            fill_ts=lg.fill_ts,
            lg=lg,
            price=lg.fill_px,
            fee=fee_fill(lg.fill_px, float(sp_f), lg.qty),
            slip=slip_usd(lg.fill_px, dte_days(lg.expiry, lg.fill_ts), lg.qty, slip_mult),
        )

    pending: dict[str, Any] | None = None
    step1: dict[str, Any] | None = None
    step2: dict[str, Any] | None = None
    redeploys = 0
    max_g = 0.0
    min_g = 0.0
    t_end = expiry_unix(e1) + 120
    ohlc = ohlc or {}
    t = t0
    nloop = 0
    exit_reason = ""
    exit_ts = t0
    last_gross = 0.0

    def mark_of(lg: Leg, tt: int) -> float | None:
        return cache.px(lg.symbol, tt, lg.expiry)

    def gross_at(tt: int) -> float:
        g = float(realized)
        for lg in legs:
            if not lg.open:
                continue
            px = mark_of(lg, tt)
            if px is None:
                continue
            g += _mtm(lg, px)
        return g

    def close_open(tt: int, *, event: str = "", trigger_ts: int = 0) -> bool:
        nonlocal realized
        ok = True
        for lg in legs:
            if not lg.open:
                continue
            got = cache.next_px(lg.symbol, tt, lg.expiry)
            if got is None:
                ok = False
                continue
            ft, px = got
            sp = mkt.spot(ft) or 0.0
            fee = fee_fill(px, float(sp), lg.qty)
            slp = slip_usd(px, dte_days(lg.expiry, ft), lg.qty, slip_mult)
            realized += _mtm(lg, px)
            fees_acc[0] += fee
            slips_acc[0] += slp
            lg.open = False
            lg.exit_ts = int(ft)
            lg.exit_px = float(px)
            if event:
                emit(event, trigger_ts=trigger_ts or tt, fill_ts=ft, lg=lg, price=px, fee=fee, slip=slp)
        return ok

    def settle_exp(exp: date, tt: int) -> None:
        nonlocal realized
        sp = mkt.spot(tt)
        if sp is None:
            return
        for lg in legs:
            if not lg.open or lg.expiry != exp:
                continue
            xf = float(intrinsic(lg.is_call, lg.strike, float(sp)))
            realized += float(lg.sign) * float(lg.qty) * CV * (xf - float(lg.fill_px))
            lg.open = False
            lg.settled = True
            lg.exit_ts = int(tt)
            lg.exit_px = xf
            emit(
                "EXPIRY_SETTLE",
                trigger_ts=tt,
                fill_ts=tt,
                lg=lg,
                price=xf,
                fee=0.0,
                slip=0.0,
            )

    def main_open() -> bool:
        return any(lg.open and lg.role in ("main_call", "main_put") for lg in legs)

    def profit_is_call(tt: int) -> bool:
        mc = next(lg for lg in legs if lg.role == "main_call")
        mp = next(lg for lg in legs if lg.role == "main_put")

        def mtm_open_or_exit(lg: Leg) -> float:
            if lg.open:
                px = mark_of(lg, tt)
                return _mtm(lg, float(px if px is not None else lg.fill_px))
            xf = lg.exit_px if lg.exit_px == lg.exit_px else lg.fill_px
            return float(lg.sign) * lg.qty * CV * (float(xf) - lg.fill_px)

        return mtm_open_or_exit(mc) >= mtm_open_or_exit(mp)

    def plan_step(tt: int) -> dict[str, Any]:
        is_call = profit_is_call(tt)
        e0n, _ = dte0_dte1(tt, listed)
        late = is_late_window(e0n, tt)
        if late:
            role = "short_call" if is_call else "short_put"
            wanted = float(next(lg.strike for lg in legs if lg.role == role))
            exp_s = e1
        else:
            role = "main_call" if is_call else "main_put"
            wanted = float(next(lg.strike for lg in legs if lg.role == role))
            exp_s = e0n if e0n is not None else e1
        ch = _chain_rows(mkt, exp_s, tt)
        k_s, gap = resolve_listed_strike(ch, is_call, wanted)
        return {
            "is_call": is_call,
            "strike": k_s,
            "wanted": wanted,
            "exp": exp_s,
            "gap": gap,
            "late": late,
        }

    while t <= t_end and nloop < max_minutes:
        nloop += 1
        if pending is not None and int(pending["fill_ts"]) <= t:
            kind = pending["kind"]
            if kind == "close":
                reason = str(pending["reason"])
                trig = int(pending.get("trigger_ts", t))
                close_open(t, event=f"EXIT_{reason}", trigger_ts=trig)
                exit_reason = reason
                exit_ts = t
                last_gross = gross_at(t)
                pending = None
                break
            if kind == "step":
                is_call = bool(pending["is_call"])
                exp = pending["exp"]
                wanted = float(pending.get("wanted", pending["strike"]))
                ch = _chain_rows(mkt, exp, t)
                strike, gap = resolve_listed_strike(ch, is_call, wanted)
                gaps_acc[0] = max(gaps_acc[0], float(pending.get("gap", 0.0)), gap)
                row = _leg_row(ch, is_call, strike) if ch else None
                prem_row = row
                sym = str(prem_row["symbol"]) if prem_row is not None else opt_symbol(is_call, strike, exp)
                got = cache.next_px(sym, t, exp)
                if got is not None:
                    ft, prem = got
                    qn = step_qty(Q, prem)
                    lg = open_leg(
                        f"step{steps+1}_{'C' if is_call else 'P'}",
                        is_call, strike, exp, qn, -1, ft, True,
                    )
                    if lg is not None:
                        legs.append(lg)
                        rec = {
                            "ts": int(lg.fill_ts),
                            "is_call": is_call,
                            "strike": float(lg.strike),
                            "expiry": exp.isoformat(),
                            "qty": int(lg.qty),
                            "prem": float(lg.fill_px),
                            "wanted_strike": wanted,
                            "strike_gap": abs(float(lg.strike) - wanted),
                            "late": bool(pending.get("late")),
                        }
                        ev_name = "STEP1" if steps == 0 else "STEP2"
                        if steps == 0:
                            step1 = rec
                            steps = 1
                        elif steps == 1:
                            step2 = rec
                            steps = 2
                        sp_f = mkt.spot(lg.fill_ts) or spot0
                        emit(
                            ev_name,
                            trigger_ts=int(pending.get("trigger_ts", t)),
                            fill_ts=lg.fill_ts,
                            lg=lg,
                            price=lg.fill_px,
                            fee=fee_fill(lg.fill_px, float(sp_f), lg.qty),
                            slip=slip_usd(lg.fill_px, dte_days(lg.expiry, lg.fill_ts), lg.qty, slip_mult),
                        )
                pending = None
            elif kind == "redeploy":
                for item in pending["items"]:
                    lg0: Leg = item
                    e_new = pending["new_exp"]
                    ch = _chain_rows(mkt, e_new, t)
                    wanted = float(lg0.strike)
                    k_s, gap = resolve_listed_strike(ch, lg0.is_call, wanted)
                    gaps_acc[0] = max(gaps_acc[0], gap)
                    row = _leg_row(ch, lg0.is_call, k_s) if ch else None
                    sym = str(row["symbol"]) if row is not None else opt_symbol(lg0.is_call, k_s, e_new)
                    got = cache.next_px(sym, t, e_new)
                    if got is None:
                        continue
                    ft, prem = got
                    qn = step_qty(Q, prem)
                    lg = open_leg(
                        lg0.role + "_rd",
                        lg0.is_call, k_s, e_new, qn, -1, ft, True,
                    )
                    if lg is not None:
                        legs.append(lg)
                        redeploys += 1
                        sp_f = mkt.spot(lg.fill_ts) or spot0
                        emit(
                            "REDEPLOY",
                            trigger_ts=int(pending.get("trigger_ts", t)),
                            fill_ts=lg.fill_ts,
                            lg=lg,
                            price=lg.fill_px,
                            fee=fee_fill(lg.fill_px, float(sp_f), lg.qty),
                            slip=slip_usd(lg.fill_px, dte_days(lg.expiry, lg.fill_ts), lg.qty, slip_mult),
                        )
                pending = None

        exp_now = [e for e in listed if expiry_unix(e) == t]
        for ex in exp_now:
            settled_steps = [lg for lg in legs if lg.open and lg.expiry == ex and lg.is_step]
            settle_exp(ex, t)
            if settled_steps and main_open():
                e_new, _ = dte0_dte1(t, listed)
                if e_new is not None:
                    pending = {
                        "kind": "redeploy",
                        "fill_ts": t + 60,
                        "trigger_ts": t,
                        "items": settled_steps,
                        "new_exp": e_new,
                    }
            if not main_open():
                exit_reason = "EXPIRY"
                exit_ts = t
                last_gross = gross_at(t)
                break
        if exit_reason:
            break

        g = gross_at(t)
        last_gross = g
        max_g = max(max_g, g)
        min_g = min(min_g, g)

        if pending is None:
            if g >= tgt:
                pending = {"kind": "close", "reason": "TARGET", "fill_ts": t + 60, "trigger_ts": t}
            elif g <= sl_now(steps, Q):
                pending = {"kind": "close", "reason": "SL", "fill_ts": t + 60, "trigger_ts": t}
            elif steps >= 1:
                bar = ohlc.get(t)
                if bar is not None:
                    hi, lo, _c = bar
                    if float(lo) <= float(signal_price) <= float(hi):
                        pending = {
                            "kind": "close",
                            "reason": "SIGNAL_PRICE",
                            "fill_ts": t + 60,
                            "trigger_ts": t,
                        }
            if pending is None and steps == 0 and g >= lock:
                spec = plan_step(t)
                gaps_acc[0] = max(gaps_acc[0], float(spec["gap"]))
                pending = {
                    "kind": "step",
                    "fill_ts": t + 60,
                    "trigger_ts": t,
                    **spec,
                }
            elif pending is None and steps == 1 and g >= 2.0 * lock:
                spec = plan_step(t)
                gaps_acc[0] = max(gaps_acc[0], float(spec["gap"]))
                pending = {
                    "kind": "step",
                    "fill_ts": t + 60,
                    "trigger_ts": t,
                    **spec,
                }
        t += 60

    if not exit_reason:
        xt = min(t, t_end)
        close_open(xt, event="EXIT_EXPIRY", trigger_ts=xt)
        exit_reason = "EXPIRY"
        exit_ts = xt
        last_gross = gross_at(exit_ts)

    fees = float(fees_acc[0])
    slip = float(slips_acc[0])
    net = float(last_gross) - fees
    return BasketResult(
        True,
        "",
        entry_ts=t0,
        exit_ts=int(exit_ts),
        exit_reason=exit_reason,
        signal_price=float(signal_price),
        e1=e1,
        legs=legs,
        step1=step1,
        step2=step2,
        redeploys=int(redeploys),
        gross=float(last_gross),
        fees=fees,
        slip=slip,
        net=net,
        net_with_slip=net - slip,
        max_gross=float(max_g),
        min_gross=float(min_g),
        hold_min=int(max(0, (exit_ts - t0) // 60)),
        stale_minutes=int(cache.stale_minutes),
        steps=int(steps),
        step_strike_gap=float(gaps_acc[0]),
    )
