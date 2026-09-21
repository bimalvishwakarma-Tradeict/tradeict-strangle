"""
S001 G1–G10 parity gate — synthetic scenarios only.

Prints a single PASS/FAIL table. Does not run historical backtests or
print aggregate PnL / Sharpe / win-rate.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

_BACKTEST = Path(__file__).resolve().parents[1]
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    is_adj_b_no_strike_inside_wing,
    resolve_adj_b_wing_strike,
    select_adj_b_strike,
)
from backtest.s001_mark_engine import (  # noqa: E402
    LegState,
    is_pre_expiry,
    ist_dt,
    lock_profit_target_usd,
    pick_atm_straddle_marks,
    pick_strangle_by_premium_marks,
    pick_wing_strikes,
    simulate_synthetic_cycle,
    to_unix,
)


Result = tuple[bool, str]


def _cfg(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "adj_mode": "B_only",
        "adj_b_trigger": 70.0,
        "dec_pct": 40.0,
        "max_adj": 2,
        "slip_model": "flat165",
        "slip_mult": 0.0,
        "profit_mode": "none",
        "tp_pct": 50.0,
        "profit_k": 1.0,
        "wing_roll": True,
        "wing_points": 2000.0,
    }
    base.update(over)
    return base


def _leg(
    symbol: str,
    strike: float,
    opt: str,
    qty: int,
    fill: float,
    mark: float,
    *,
    fee: float = 0.0,
    status: str = "open",
) -> LegState:
    return LegState(
        symbol=symbol,
        strike=strike,
        opt_type=opt,
        qty=qty,
        entry_fill=fill,
        entry_mark=mark,
        baseline=mark,
        entry_fee=fee,
        status=status,
    )


def _fill_series(
    symbols: list[str],
    start_ts: int,
    end_ts: int,
    value: float,
) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {s: {} for s in symbols}
    t = start_ts
    while t <= end_ts:
        for s in symbols:
            out[s][t] = value
        t += 60
    return out


def _put_row(k: float, prem: float, sym: str) -> dict[str, Any]:
    return {
        "strike": k,
        "option_type": "put",
        "mark_price": prem,
        "premium": prem,
        "symbol": sym,
        "product_id": int(k),
    }


def _call_row(k: float, prem: float, sym: str) -> dict[str, Any]:
    return {
        "strike": k,
        "option_type": "call",
        "mark_price": prem,
        "premium": prem,
        "symbol": sym,
        "product_id": int(k),
    }


# ---------------------------------------------------------------------------
# G1 — entry 11:00 IST, 2DTE, B25 per-side
# ---------------------------------------------------------------------------
def g1() -> Result:
    # Clock / DTE knobs (engine defaults + CLI)
    entry = ist_dt(date(2025, 7, 15), 11, 0)
    if entry.hour != 11 or entry.minute != 0:
        return False, f"entry clock not 11:00 IST got {entry}"
    dte = 2
    expiry = date(2025, 7, 15) + timedelta(days=dte)
    if (expiry - date(2025, 7, 15)).days != 2:
        return False, "2DTE expiry offset wrong"

    spot = 100_000.0
    calls = [
        _call_row(100_000.0, 800.0, "C_atm"),
        _call_row(102_000.0, 200.0, "C_102"),
        _call_row(104_000.0, 50.0, "C_104"),
    ]
    puts = [
        _put_row(100_000.0, 800.0, "P_atm"),
        _put_row(98_000.0, 200.0, "P_98"),
        _put_row(96_000.0, 50.0, "P_96"),
    ]
    atm = pick_atm_straddle_marks(calls, puts, spot)
    if atm is None:
        return False, "ATM straddle missing"
    _k, ac, ap = atm
    target = 0.25 * (ac + ap)  # B25 per-side of hedge/ATM straddle
    if abs(target - 400.0) > 1e-9:
        return False, f"B25 target expected 400 got {target}"
    picked = pick_strangle_by_premium_marks(calls, puts, spot, target)
    if picked is None:
        return False, "strangle pick failed"
    # nearest to 400 among OTM: none at 400; 200 is closer than 50
    c, p = picked
    if float(c["strike"]) != 102_000.0 or float(p["strike"]) != 98_000.0:
        return False, (
            f"expected C102k/P98k got C{c['strike']}/P{p['strike']}"
        )
    return True, f"11:00 IST + 2DTE + B25 target={target:.0f} → C102k/P98k"


# ---------------------------------------------------------------------------
# G2 — Adj B dual trigger (pressured + decayed), NOT single-leg ≥ trigger%
# ---------------------------------------------------------------------------
def g2() -> Result:
    day = date(2026, 5, 10)
    expiry = day + timedelta(days=2)
    entry_ts = to_unix(ist_dt(day, 11, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 80.0, 80.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)
    cands = [
        ("P_97k", 97_000.0, 40.0),
        ("P_975k", 97_500.0, 45.0),
        ("P_985k", 98_500.0, 55.0),
    ]
    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol] + [
        s for s, _, _ in cands
    ]
    end_ts = entry_ts + 600
    series = _fill_series(symbols, entry_ts, end_ts, 80.0)
    for t in range(entry_ts, end_ts + 1, 60):
        series[call.symbol][t] = 100.0
        series[put.symbol][t] = 80.0
        series[wing_c.symbol][t] = 5.0
        series[wing_p.symbol][t] = 5.0
        for s, _, p in cands:
            series[s][t] = p

    # Minute 1: only put decayed (<70% of 80=56) — call NOT pressured → no adj
    t1 = entry_ts + 60
    series[call.symbol][t1] = 99.0  # < baseline → not pressured
    series[put.symbol][t1] = 40.0  # decayed

    # Minute 2: call pressured + put decayed → Adj B fires
    t2 = entry_ts + 120
    series[call.symbol][t2] = 110.0
    series[put.symbol][t2] = 40.0
    for s, _, p in cands:
        series[s][t2] = min(p, 50.0)

    chain = [_put_row(k, p, s) for s, k, p in cands]
    chain_by_ts = {
        t1: {"put": chain, "call": []},
        t2: {"put": chain, "call": []},
    }
    for tt in range(entry_ts + 60, end_ts + 1, 60):
        chain_by_ts.setdefault(tt, {"put": chain, "call": []})

    res = simulate_synthetic_cycle(
        entry_ts=entry_ts,
        expiry=expiry,
        spot=100_000.0,
        call_leg=call,
        put_leg=put,
        wing_c=wing_c,
        wing_p=wing_p,
        series=series,
        chain_by_ts=chain_by_ts,
        all_strikes=[96_000, 97_000, 97_500, 98_000, 98_500, 102_000, 104_000],
        cfg=_cfg(profit_mode="none", max_adj=2),
        profit_target=None,
    )
    if res.n_adjustments < 1:
        return False, "expected Adj B on dual gate; got 0 adjs"
    if res.adj_events[0].ts != t2:
        return False, (
            f"adj fired at {res.adj_events[0].ts}, expected t2={t2} "
            "(single-leg decay alone must not trigger)"
        )
    if res.adj_events[0].leg != "put":
        return False, f"expected untested=put, got {res.adj_events[0].leg}"
    return True, "dual gate: skip decay-only; fire pressured+decayed → roll put"


# ---------------------------------------------------------------------------
# G3 — premium < P_target, then MOST expensive survivor
# ---------------------------------------------------------------------------
def g3() -> Result:
    chain = [
        _put_row(97_000.0, 40.0, "P_97"),
        _put_row(97_500.0, 55.0, "P_975"),
        _put_row(98_000.0, 69.0, "P_98"),  # highest < 70 → winner
        _put_row(98_500.0, 70.0, "P_985"),  # == P_target → reject
        _put_row(99_000.0, 80.0, "P_99"),  # > P_target → reject
    ]
    res = select_adj_b_strike(
        leg_type="put",
        p_target=70.0,
        chain=chain,
        spot=100_000.0,
        other_short_strike=102_000.0,
        wing_strike=None,
    )
    if not res.success:
        return False, f"select failed: {res.skip_reason}"
    if float(res.strike) != 98_000.0:
        return False, (
            f"expected most-expensive below target = 98000, got {res.strike}"
        )
    if float(res.premium) != 69.0:
        return False, f"expected prem 69 got {res.premium}"
    return True, "strict < P_target then max premium → 98000 @ 69"


# ---------------------------------------------------------------------------
# G4 — wing filter: open+qty>0 only; reject at/beyond wing
# ---------------------------------------------------------------------------
def g4() -> Result:
    class W:
        pass

    open_wing = W()
    open_wing.status = "open"
    open_wing.quantity = 4
    open_wing.strike = 96_000.0
    if resolve_adj_b_wing_strike(open_wing) != 96_000.0:
        return False, "open qty>0 wing must count"

    closed = W()
    closed.status = "closed"
    closed.quantity = 4
    closed.strike = 96_000.0
    if resolve_adj_b_wing_strike(closed) is not None:
        return False, "closed wing must not count"

    zero = W()
    zero.status = "open"
    zero.quantity = 0
    zero.strike = 96_000.0
    if resolve_adj_b_wing_strike(zero) is not None:
        return False, "qty=0 open wing must not count"

    chain = [
        _put_row(95_000.0, 60.0, "P_95"),  # <= wing → reject
        _put_row(96_000.0, 55.0, "P_96"),  # == wing → reject
        _put_row(97_000.0, 50.0, "P_97"),  # inside → ok
        _put_row(97_500.0, 45.0, "P_975"),
    ]
    res = select_adj_b_strike(
        leg_type="put",
        p_target=70.0,
        chain=chain,
        spot=100_000.0,
        other_short_strike=102_000.0,
        wing_strike=96_000.0,
    )
    if not res.success:
        return False, f"inside-wing pick failed: {res.skip_reason}"
    if float(res.strike) != 97_000.0:
        return False, f"expected 97000 (highest inside wing) got {res.strike}"
    beyond = [
        c
        for c in (res.candidates_considered or [])
        if c.get("rejected") == "at_or_beyond_wing"
    ]
    if len(beyond) < 2:
        return False, f"expected ≥2 at_or_beyond_wing rejects, got {beyond}"
    return True, "wing open+qty>0; reject ≤wing; pick 97000 inside"


# ---------------------------------------------------------------------------
# G5 — no strike inside wing → basket EXIT (not skip)
# ---------------------------------------------------------------------------
def g5() -> Result:
    day = date(2026, 5, 10)
    expiry = day + timedelta(days=2)
    entry_ts = to_unix(ist_dt(day, 11, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 80.0, 80.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)

    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol]
    end_ts = entry_ts + 600
    series = _fill_series(symbols, entry_ts, end_ts, 80.0)
    for t in range(entry_ts, end_ts + 1, 60):
        series[call.symbol][t] = 100.0
        series[put.symbol][t] = 80.0
        series[wing_c.symbol][t] = 5.0
        series[wing_p.symbol][t] = 5.0

    tt = entry_ts + 60
    series[call.symbol][tt] = 110.0
    series[put.symbol][tt] = 40.0

    bad_chain = [
        _put_row(95_000.0, 50.0, "P_95"),
        _put_row(96_000.0, 45.0, "P_96"),
        _put_row(94_000.0, 55.0, "P_94"),
    ]
    for row in bad_chain:
        series[row["symbol"]] = {t: float(row["mark_price"]) for t in range(entry_ts, end_ts + 1, 60)}
    chain_by_ts = {t: {"put": bad_chain, "call": []} for t in range(entry_ts, end_ts + 1, 60)}

    # Helper must classify as no-strike-inside-wing
    sel = select_adj_b_strike(
        leg_type="put",
        p_target=110.0,
        chain=bad_chain,
        spot=100_000.0,
        other_short_strike=102_000.0,
        wing_strike=96_000.0,
    )
    if not is_adj_b_no_strike_inside_wing(sel, 96_000.0):
        return False, "helper did not flag no-strike-inside-wing"

    res = simulate_synthetic_cycle(
        entry_ts=entry_ts,
        expiry=expiry,
        spot=100_000.0,
        call_leg=call,
        put_leg=put,
        wing_c=wing_c,
        wing_p=wing_p,
        series=series,
        chain_by_ts=chain_by_ts,
        all_strikes=[94_000, 95_000, 96_000, 98_000, 102_000, 104_000],
        cfg=_cfg(profit_mode="none"),
        profit_target=None,
    )
    if res.exit_reason != "ADJ_B_NO_STRIKE_INSIDE_WING":
        return False, f"expected ADJ_B_NO_STRIKE_INSIDE_WING got {res.exit_reason}"
    if res.n_adjustments != 0:
        return False, "must EXIT without placing adj"
    return True, "no candidate inside wing → force-exit ADJ_B_NO_STRIKE_INSIDE_WING"


# ---------------------------------------------------------------------------
# G6 — max 2 adj; 3rd trigger → force-exit
# ---------------------------------------------------------------------------
def g6() -> Result:
    day = date(2026, 5, 10)
    expiry = day + timedelta(days=2)
    entry_ts = to_unix(ist_dt(day, 11, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 80.0, 80.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)
    cands = [
        ("P_97k", 97_000.0, 40.0),
        ("P_975k", 97_500.0, 45.0),
        ("P_985k", 98_500.0, 55.0),
        ("P_99k", 99_000.0, 60.0),
        ("P_995k", 99_500.0, 65.0),
    ]
    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol] + [
        s for s, _, _ in cands
    ]
    end_ts = entry_ts + 3600
    series = _fill_series(symbols, entry_ts, end_ts, 50.0)
    for t in range(entry_ts, end_ts + 1, 60):
        series[wing_c.symbol][t] = 5.0
        series[wing_p.symbol][t] = 5.0
        series[call.symbol][t] = 100.0
        series[put.symbol][t] = 80.0
        for s, _, p in cands:
            series[s][t] = p

    trigger_ts = [entry_ts + 60, entry_ts + 180, entry_ts + 300]
    for tt in trigger_ts:
        series[call.symbol][tt] = 110.0
        series[put.symbol][tt] = 40.0
        for s, _, p in cands:
            series[s][tt] = min(p, 50.0)
    for tt in (entry_ts + 180, entry_ts + 300):
        for s, _, _ in cands:
            series[s][tt] = 30.0
        series[call.symbol][tt] = 200.0

    chain = [_put_row(k, p, s) for s, k, p in cands]
    chain_by_ts = {tt: {"put": chain, "call": []} for tt in range(entry_ts + 60, entry_ts + 400, 60)}

    res = simulate_synthetic_cycle(
        entry_ts=entry_ts,
        expiry=expiry,
        spot=100_000.0,
        call_leg=call,
        put_leg=put,
        wing_c=wing_c,
        wing_p=wing_p,
        series=series,
        chain_by_ts=chain_by_ts,
        all_strikes=[96_000, 97_000, 97_500, 98_000, 98_500, 99_000, 99_500, 102_000],
        cfg=_cfg(profit_mode="none", max_adj=2),
        profit_target=None,
    )
    if res.n_adjustments != 2:
        return False, f"expected exactly 2 adjs got {res.n_adjustments}"
    if res.exit_reason != "MAX_ADJUSTMENTS_REACHED":
        return False, f"expected MAX_ADJUSTMENTS_REACHED got {res.exit_reason}"
    return True, "2 adjs placed; 3rd trigger → MAX_ADJUSTMENTS_REACHED"


# ---------------------------------------------------------------------------
# G7 — compute_decrease_step_qty floor() as-is (D2 mirror)
# ---------------------------------------------------------------------------
def g7() -> Result:
    # orig=8, pct=40 → adj1 floor(4.8)=4; adj2 floor(1.6)=1; adj3 close
    q1, close1 = compute_decrease_step_qty(
        original_qty=8, adjustment_number=1, decrease_pct=40.0
    )
    q2, close2 = compute_decrease_step_qty(
        original_qty=8, adjustment_number=2, decrease_pct=40.0
    )
    q3, close3 = compute_decrease_step_qty(
        original_qty=8, adjustment_number=3, decrease_pct=40.0
    )
    if close1 or q1 != 4:
        return False, f"adj1 expected qty=4 got {q1} close={close1}"
    if close2 or q2 != 1:
        return False, f"adj2 expected qty=1 got {q2} close={close2}"
    if not close3 or q3 is not None:
        return False, f"adj3 expected close_basket got qty={q3} close={close3}"
    # Prove floor truncates (not round): 8*0.6=4.8 → 4
    if int(4.8) == 4 and q1 == 4:
        return True, (
            "floor(orig×remaining) mirrored: 8→4→1→close "
            "(NOTE: floor truncates 4.8→4; D2 left open on purpose)"
        )
    return False, "floor assertion failed"


# ---------------------------------------------------------------------------
# G8 — wings 2000 pts; wing_roll default ON
# ---------------------------------------------------------------------------
def g8() -> Result:
    strikes = list(range(90_000, 115_001, 500))
    wings = pick_wing_strikes(strikes, 102_000.0, 98_000.0, 2000.0)
    if wings is None:
        return False, "wing pick failed"
    wc, wp = wings
    if wc != 104_000.0 or wp != 96_000.0:
        return False, f"expected WC104k/WP96k got {wc}/{wp}"
    cfg = _cfg(wing_roll=True, wing_points=2000.0)
    if not cfg["wing_roll"] or cfg["wing_points"] != 2000.0:
        return False, "wing_roll/points cfg wrong"
    return True, "wings ±2000 → 104k/96k; wing_roll=1"


# ---------------------------------------------------------------------------
# G9 — profit target = cost × k; same-day re-entry is scheduler behaviour
# ---------------------------------------------------------------------------
def g9() -> Result:
    qty = 8
    from backtest.s001_mark_engine import qty_btc

    # cost_k formula: entry_cost = wing_debit + fees (NOT short credit)
    tp_formula = lock_profit_target_usd(
        call_fill=100.0,
        put_fill=100.0,
        wing_c_fill=10.0,
        wing_p_fill=10.0,
        entry_fees=5.0,
        qty=qty,
        cfg={"profit_mode": "cost_k", "profit_k": 1.0},
    )
    expected = 5.0 + (10.0 + 10.0) * qty_btc(qty)
    if tp_formula is None or abs(float(tp_formula) - expected) > 1e-9:
        return False, f"cost_k target expected {expected} got {tp_formula}"

    # Cycle TP: tiny cost so premium collapse clearly clears it
    tp = lock_profit_target_usd(
        call_fill=100.0,
        put_fill=100.0,
        wing_c_fill=1.0,
        wing_p_fill=1.0,
        entry_fees=0.0,
        qty=qty,
        cfg={"profit_mode": "cost_k", "profit_k": 1.0},
    )
    day = date(2026, 5, 10)
    expiry = day
    entry_ts = to_unix(ist_dt(day, 11, 0))
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 100.0, 100.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 1.0, 1.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 1.0, 1.0)
    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol]
    series = _fill_series(symbols, entry_ts, entry_ts + 600, 100.0)
    for t in range(entry_ts + 60, entry_ts + 600 + 1, 60):
        series[call.symbol][t] = 10.0
        series[put.symbol][t] = 10.0
        series[wing_c.symbol][t] = 1.0
        series[wing_p.symbol][t] = 1.0
    res = simulate_synthetic_cycle(
        entry_ts=entry_ts,
        expiry=expiry,
        spot=100_000.0,
        call_leg=call,
        put_leg=put,
        wing_c=wing_c,
        wing_p=wing_p,
        series=series,
        chain_by_ts={},
        all_strikes=[96_000, 98_000, 102_000, 104_000],
        cfg=_cfg(profit_mode="cost_k", profit_k=1.0, adj_mode="none"),
        profit_target=tp,
    )
    if res.exit_reason != "PROFIT_TARGET":
        return False, f"expected PROFIT_TARGET got {res.exit_reason} tp={tp}"
    return True, (
        f"cost×k formula={expected:.4f}; cycle TP exit; "
        "same-day re-entry gated on PROFIT_TARGET in run()"
    )


# ---------------------------------------------------------------------------
# G10 — pre-expiry close 17:15 IST
# ---------------------------------------------------------------------------
def g10() -> Result:
    exp = date(2026, 5, 10)
    # 17:14 → outside window (hours_left > 0.25)
    ts_1414 = to_unix(ist_dt(exp, 17, 14))
    # 17:15 → inside
    ts_1515 = to_unix(ist_dt(exp, 17, 15))
    # 17:30 → expired (h<=0 also exits)
    ts_1730 = to_unix(ist_dt(exp, 17, 30))
    if is_pre_expiry(ts_1414, exp):
        return False, "17:14 must NOT be pre-expiry"
    if not is_pre_expiry(ts_1515, exp):
        return False, "17:15 must be pre-expiry"
    if not is_pre_expiry(ts_1730, exp):
        return False, "17:30 must exit (h<=0)"

    day = date(2026, 5, 10)
    expiry = day
    entry_ts = to_unix(ist_dt(day, 17, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 100.0, 100.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)
    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol]
    end_ts = to_unix(ist_dt(day, 17, 30))
    series = _fill_series(symbols, entry_ts, end_ts, 100.0)
    res = simulate_synthetic_cycle(
        entry_ts=entry_ts,
        expiry=expiry,
        spot=100_000.0,
        call_leg=call,
        put_leg=put,
        wing_c=wing_c,
        wing_p=wing_p,
        series=series,
        chain_by_ts={},
        all_strikes=[96_000, 98_000, 102_000, 104_000],
        cfg=_cfg(profit_mode="none", adj_mode="none"),
        profit_target=None,
    )
    if res.exit_reason != "PRE_EXPIRY":
        return False, f"expected PRE_EXPIRY got {res.exit_reason}"
    exit_ist = __import__("datetime").datetime.fromtimestamp(
        res.exit_ts, tz=__import__("datetime").timezone.utc
    ).astimezone(ist_dt(day, 0, 0).tzinfo)
    if exit_ist.hour != 17 or exit_ist.minute < 15:
        return False, f"exit before 17:15 IST: {exit_ist}"
    return True, f"pre-expiry fires at {exit_ist.strftime('%H:%M')} IST"


# ---------------------------------------------------------------------------
def main() -> int:
    gates: list[tuple[str, Callable[[], Result]]] = [
        ("G1", g1),
        ("G2", g2),
        ("G3", g3),
        ("G4", g4),
        ("G5", g5),
        ("G6", g6),
        ("G7", g7),
        ("G8", g8),
        ("G9", g9),
        ("G10", g10),
    ]
    rows: list[str] = []
    failed = 0
    for name, fn in gates:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 — parity table must always print
            ok, detail = False, f"EXCEPTION: {exc}"
        status = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        if ok:
            rows.append(f"{name} {status}")
        else:
            rows.append(f"{name} {status} ({detail})")
        print(f"{name} {status} | {detail}")

    print("")
    print(" | ".join(rows))
    print(f"TOTAL {'PASS' if failed == 0 else 'FAIL'} ({10 - failed}/10)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
