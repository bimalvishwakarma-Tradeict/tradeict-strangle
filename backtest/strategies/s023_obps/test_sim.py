"""S023 OBPS sim units: qty, fee, expiry resolver, SL ladder, occupancy, golden."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.strategies.s023_obps.sim import (  # noqa: E402
    BasketResult,
    dte0_dte1,
    expiry_unix,
    fee_fill,
    hours_to,
    lock_usd,
    protection_qty,
    sl0_usd,
    sl_now,
    simulate_basket,
    step_qty,
    target_usd,
)
from backtest.strategies.s023_obps.run_s023 import occupancy_walk  # noqa: E402


class FakeMkt:
    def __init__(self) -> None:
        self.spots: dict[int, float] = {}
        self.quotes: dict[tuple[str, int], float] = {}
        self.chains: dict[tuple[date, int], list[dict[str, Any]]] = {}

    def spot(self, t: int) -> float | None:
        return self.spots.get(int(t))

    def quote(self, symbol: str, t: int) -> tuple[float, str] | None:
        for age in range(0, 6):
            px = self.quotes.get((symbol, int(t) - 60 * age))
            if px is not None:
                return float(px), "real" if age == 0 else "stale"
        return None

    def chain(self, exp: date, t: int) -> tuple[list[dict[str, Any]], str] | None:
        rows = self.chains.get((exp, int(t)))
        if rows is None:
            for age in range(1, 6):
                rows = self.chains.get((exp, int(t) - 60 * age))
                if rows is not None:
                    return list(rows), "stale"
            return None
        return list(rows), "real"

    def ensure(self, symbol: str, exp: date) -> None:
        return None


def _row(sym: str, k: float, is_call: bool, px: float) -> dict[str, Any]:
    return {"symbol": sym, "strike": float(k), "is_call": is_call, "mark": float(px)}


def _put_chain(m: FakeMkt, exp: date, t: int, atm: float, px: dict[str, float]) -> None:
    rows = [
        _row(f"C-{int(atm-1000)}-{exp}", atm - 1000, True, px["lc"]),
        _row(f"P-{int(atm+1000)}-{exp}", atm + 1000, False, px["lp"]),
        _row(f"C-{int(atm)}-{exp}", atm, True, px["sc"]),
        _row(f"P-{int(atm)}-{exp}", atm, False, px["sp"]),
    ]
    m.chains[(exp, t)] = rows
    for r in rows:
        key = "lc" if r["is_call"] and r["strike"] < atm else (
            "sc" if r["is_call"] else ("lp" if r["strike"] > atm else "sp")
        )
        m.quotes[(r["symbol"], t)] = float(px[key])


def test_t1_qty() -> None:
    assert protection_qty(1000, 690) == 304
    assert step_qty(1000, 1750) == 120
    assert step_qty(1000, 1850) == 113
    assert target_usd(500) == 105
    assert sl0_usd(500) == 105
    assert lock_usd(500) == 35
    assert protection_qty(500, 690) == 152


def test_t2_fee() -> None:
    assert abs(fee_fill(2000, 100_000, 1000) - 10.00) < 1e-9
    assert abs(fee_fill(100, 100_000, 1000) - 4.13) < 1e-9
    assert fee_fill(2000, 100_000, 0) == 0.0


def test_t3_expiry_resolver() -> None:
    listed = [date(2025, 6, 2), date(2025, 6, 3), date(2025, 6, 4)]
    t10 = to_unix(ist_dt(date(2025, 6, 2), 10, 0))
    e0, e1 = dte0_dte1(t10, listed)
    assert e0 == date(2025, 6, 2)
    assert e1 == date(2025, 6, 3)
    assert expiry_unix(e0) == to_unix(ist_dt(date(2025, 6, 2), 17, 30))
    t20 = to_unix(ist_dt(date(2025, 6, 2), 20, 0))
    e0b, e1b = dte0_dte1(t20, listed)
    assert e0b == date(2025, 6, 3)
    assert e1b == date(2025, 6, 4)


def _fill_day(m: FakeMkt, d0: date, d1: date, t0: int, t1: int, px: dict[str, float], spot: float) -> None:
    t = t0
    while t <= t1:
        m.spots[t] = spot
        _put_chain(m, d1, t, 100_000.0, px)
        _put_chain(m, d0, t, 100_000.0, px)
        t += 60


def test_t4_late_day_step_uses_e1_atm() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 13, 0))
    assert abs(hours_to(d0, t_step) - 4.5) < 1e-6
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    _fill_day(m, d0, d1, t_ent, t_step + 180, px0, 100_000.0)
    t = t_step
    while t <= t_step + 180:
        px = dict(px0)
        px["lc"] = 580.0
        _put_chain(m, d1, t, 100_000.0, px)
        _put_chain(m, d0, t, 100_000.0, px)
        t += 60
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_step + 240, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=100000.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok
    assert res.step1 is not None
    assert res.step1["expiry"] == d1.isoformat()
    assert abs(float(res.step1["strike"]) - 100000.0) < 1e-6


def test_t5_sl_ladder() -> None:
    assert sl_now(0, 1000) == -210
    assert sl_now(1, 1000) == -140
    assert sl_now(2, 1000) == -70
    assert sl_now(0, 500) == -105


def test_t6_signal_price_only_after_step() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    t_end = t_ent + 10 * 60
    _fill_day(m, d0, d1, t_ent, t_end, px0, 100_000.0)
    ohlc = {tt: (100200.0, 99800.0, 100000.0) for tt in range(t_ent, t_end + 60, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=100000.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok
    assert res.steps == 0
    assert res.exit_reason != "SIGNAL_PRICE"


def test_t7_0dte_expiry_redeploy() -> None:
    d0, d1, d2 = date(2025, 6, 2), date(2025, 6, 3), date(2025, 6, 4)
    listed = [d0, d1, d2]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_1730 = expiry_unix(d0)
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    _fill_day(m, d0, d1, t_ent, t_1730 + 180, px0, 100_000.0)
    t_hi = to_unix(ist_dt(d0, 11, 0))
    t = t_hi
    while t <= t_hi + 180:
        px = dict(px0)
        px["lc"] = 580.0
        _put_chain(m, d0, t, 100_000.0, px)
        _put_chain(m, d1, t, 100_000.0, px)
        t += 60
    _put_chain(m, d1, t_1730 + 60, 100_000.0, px0)
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_1730 + 180, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=999999.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok
    assert res.step1 is not None
    assert res.step1["expiry"] == d0.isoformat()
    assert any(lg.settled and lg.is_step for lg in res.legs)
    assert res.redeploys >= 1


def test_t8_lookahead_fill_t1() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    t_end = t_ent + 8 * 60
    _fill_day(m, d0, d1, t_ent, t_end, px0, 100_000.0)
    t_hit = t_ent + 60
    for tt in range(t_hit, t_end + 60, 60):
        px = dict(px0)
        px["lc"] = 710.0
        _put_chain(m, d1, tt, 100_000.0, px)
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_end + 60, 60)}
    a = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert a.ok and a.exit_reason == "TARGET"
    for tt in range(a.exit_ts + 60, t_end + 60, 60):
        px = dict(px0)
        px["lc"] = 9000.0
        _put_chain(m, d1, tt, 100_000.0, px)
    b = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert abs(a.gross - b.gross) < 1e-6
    assert a.exit_ts == b.exit_ts


def test_t9_occupancy_skips_overlap() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    t_end = t_ent + 12 * 60
    _fill_day(m, d0, d1, t_ent - 60, t_end, px0, 100_000.0)
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent - 60, t_end + 60, 60)}

    class Wrap:
        def __init__(self, inner: FakeMkt) -> None:
            self.inner = inner

        def spot(self, t: int) -> float | None:
            return self.inner.spot(t)

        def quote(self, symbol: str, t: int) -> tuple[float, str] | None:
            return self.inner.quote(symbol, t)

        def chain(self, exp: date, t: int) -> tuple[list[dict[str, Any]], str] | None:
            return self.inner.chain(exp, t)

        def ensure(self, symbol: str, exp: date) -> None:
            return None

    evs = [
        {"signal_ts": t_ent - 60, "signal_price": 1.0, "tf": "30m", "mode": "full", "sig_arm": "ANY", "ctrl_arm": "F"},
        {"signal_ts": t_ent, "signal_price": 1.0, "tf": "30m", "mode": "full", "sig_arm": "ANY", "ctrl_arm": "F"},
    ]
    taken, skip, _rows = occupancy_walk(evs, Wrap(m), listed, ohlc, 1000)  # type: ignore[arg-type]
    assert skip.get("occupancy", 0) >= 1
    assert len(taken) == 1


def test_t10_golden_gross_fees_net() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    t_end = t_ent + 8 * 60
    _fill_day(m, d0, d1, t_ent, t_end, px0, 100_000.0)
    t_hit = t_ent + 60
    for tt in range(t_hit, t_end + 60, 60):
        px = dict(px0)
        px["lc"] = 710.0
        _put_chain(m, d1, tt, 100_000.0, px)
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_end + 60, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok
    assert res.exit_reason == "TARGET"
    assert abs(res.gross - 210.0) < 1e-6
    # 2 long @1000 fee cap 10; 2 short qty 304 cap 3.04; entry+exit
    fee_long = 10.0
    fee_short = 10.0 * 304 / 1000
    expect_fees = 2 * (2 * fee_long + 2 * fee_short)
    assert abs(res.fees - expect_fees) < 1e-6
    assert abs(res.net - (210.0 - expect_fees)) < 1e-6
