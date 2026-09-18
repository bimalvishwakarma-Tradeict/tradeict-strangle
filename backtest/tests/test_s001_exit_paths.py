"""Synthetic exit-path proofs for s001_mark_engine.simulate_synthetic_cycle."""

from __future__ import annotations

from datetime import date

from backtest.s001_mark_engine import (
    LegState,
    ist_dt,
    lock_profit_target_usd,
    simulate_synthetic_cycle,
    to_unix,
)


def _cfg(**over: object) -> dict:
    base: dict = {
        "adj_mode": "B_only",
        "adj_b_trigger": 70.0,
        "dec_pct": 40.0,
        "max_adj": 2,
        "slip_model": "flat165",
        "slip_mult": 0.0,
        "profit_mode": "pct_of_credit",
        "tp_pct": 50.0,
        "wing_roll": True,
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


def test_profit_target_fires_when_premiums_collapse() -> None:
    day = date(2026, 5, 10)
    expiry = day  # same-day for short monitor window
    entry_ts = to_unix(ist_dt(day, 11, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 100.0, 100.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)

    tp = lock_profit_target_usd(
        call_fill=100.0,
        put_fill=100.0,
        wing_c_fill=5.0,
        wing_p_fill=5.0,
        entry_fees=0.0,
        qty=qty,
        cfg={"profit_mode": "pct_of_credit", "tp_pct": 50.0},
    )
    assert tp is not None and tp > 0

    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol]
    # collapse shorts after first minute → large short MTM
    series = _fill_series(symbols, entry_ts, entry_ts + 600, 100.0)
    for t in range(entry_ts + 60, entry_ts + 600 + 1, 60):
        series[call.symbol][t] = 10.0
        series[put.symbol][t] = 10.0
        series[wing_c.symbol][t] = 5.0
        series[wing_p.symbol][t] = 5.0

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
        all_strikes=[],
        cfg=_cfg(),
        profit_target=tp,
    )
    assert res.exit_reason == "PROFIT_TARGET"


def test_pre_expiry_fires_at_1715_ist() -> None:
    from datetime import timezone

    from backtest.s001_mark_engine import IST

    day = date(2026, 5, 10)
    expiry = day
    entry_ts = to_unix(ist_dt(day, 16, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 100.0, 100.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)

    symbols = [call.symbol, put.symbol, wing_c.symbol, wing_p.symbol]
    end_ts = to_unix(ist_dt(day, 17, 30))
    series = _fill_series(symbols, entry_ts, end_ts, 100.0)
    for t in range(entry_ts, end_ts + 1, 60):
        series[wing_c.symbol][t] = 5.0
        series[wing_p.symbol][t] = 5.0

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
        all_strikes=[],
        cfg=_cfg(profit_mode="none"),
        profit_target=None,
    )
    assert res.exit_reason == "PRE_EXPIRY"
    exit_ist = __import__("datetime").datetime.fromtimestamp(
        res.exit_ts, tz=timezone.utc
    ).astimezone(IST)
    assert exit_ist.hour == 17 and exit_ist.minute == 15


def _put_chain_row(strike: float, prem: float, symbol: str) -> dict:
    return {
        "symbol": symbol,
        "strike": strike,
        "mark_price": prem,
        "mark": prem,
        "premium": prem,
        "option_type": "put",
    }


def test_max_adjustments_reached_on_third_trigger() -> None:
    day = date(2026, 5, 10)
    expiry = day + __import__("datetime").timedelta(days=2)
    entry_ts = to_unix(ist_dt(day, 11, 0))
    qty = 8
    call = _leg("C_102k", 102_000.0, "call", qty, 100.0, 100.0)
    put = _leg("P_98k", 98_000.0, "put", qty, 80.0, 80.0)
    wing_c = _leg("WC_104k", 104_000.0, "call", qty, 5.0, 5.0)
    wing_p = _leg("WP_96k", 96_000.0, "put", qty, 5.0, 5.0)

    # Candidate puts inside wing (96000 < k <= spot), prem < tested call
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

    # Three trigger windows: call pressured (>=baseline), put decayed (<70%)
    # baseliness start call=100 put=80 → put decayed if <56, call pressured if >=100
    trigger_ts = [entry_ts + 60, entry_ts + 180, entry_ts + 300]
    for tt in trigger_ts:
        series[call.symbol][tt] = 110.0
        series[put.symbol][tt] = 40.0
        for s, _, p in cands:
            series[s][tt] = min(p, 50.0)

    # After adj, new put symbol becomes active — keep those marks decayed too
    # and call pressured relative to new baselines (set high call / low puts)
    for tt in (entry_ts + 180, entry_ts + 300):
        for s, _, _ in cands:
            series[s][tt] = 30.0
        series[call.symbol][tt] = 200.0

    chain = [_put_chain_row(k, p, s) for s, k, p in cands]
    chain_by_ts = {tt: {"put": chain, "call": []} for tt in trigger_ts}
    # also provide chain on following minutes (monitor may shift)
    for tt in range(entry_ts + 60, entry_ts + 400, 60):
        chain_by_ts[tt] = {"put": chain, "call": []}

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
    assert res.n_adjustments == 2
    assert res.exit_reason == "MAX_ADJUSTMENTS_REACHED"


def test_adj_b_no_strike_inside_wing_exit() -> None:
    day = date(2026, 5, 10)
    expiry = day + __import__("datetime").timedelta(days=2)
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
        series[wing_c.symbol][t] = 5.0
        series[wing_p.symbol][t] = 5.0
        series[call.symbol][t] = 100.0
        series[put.symbol][t] = 80.0

    tt = entry_ts + 60
    series[call.symbol][tt] = 110.0  # pressured
    series[put.symbol][tt] = 40.0  # decayed

    # Only puts at/beyond wing (strike <= 96000) → all rejected at_or_beyond_wing
    bad_chain = [
        _put_chain_row(95_000.0, 50.0, "P_95k"),
        _put_chain_row(96_000.0, 45.0, "P_96k"),
        _put_chain_row(94_000.0, 55.0, "P_94k"),
    ]
    for row in bad_chain:
        symbols.append(row["symbol"])
        for t in range(entry_ts, end_ts + 1, 60):
            series.setdefault(row["symbol"], {})[t] = float(row["mark_price"])

    res = simulate_synthetic_cycle(
        entry_ts=entry_ts,
        expiry=expiry,
        spot=100_000.0,
        call_leg=call,
        put_leg=put,
        wing_c=wing_c,
        wing_p=wing_p,
        series=series,
        chain_by_ts={tt: {"put": bad_chain, "call": []}},
        all_strikes=[94_000, 95_000, 96_000, 98_000, 102_000],
        cfg=_cfg(profit_mode="none"),
        profit_target=None,
    )
    assert res.exit_reason == "ADJ_B_NO_STRIKE_INSIDE_WING"
