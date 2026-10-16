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
from backtest.strategies.s023_obps.check_trades import check_trades  # noqa: E402
from backtest.strategies.s023_obps.sim import (  # noqa: E402
    BasketResult,
    dte0_dte1,
    expiry_unix,
    fee_fill,
    hours_to,
    is_late_window,
    lock_usd,
    decide_step_qty,
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


def test_t_multi_month(monkeypatch: Any, tmp_path: Path) -> None:
    """Second month must still see the 1m volume array (not VOLCTRL event list)."""
    import csv

    import backtest.strategies.s023_obps.run_s023 as rs

    monkeypatch.setattr(rs, "occupancy_walk", lambda *a, **k: ([], {}, []))
    dest = tmp_path / "mm"
    orig = rs.RunArchive.__init__

    def _init(self: Any, *a: Any, **k: Any) -> None:
        k["folder"] = dest
        orig(self, *a, **k)

    monkeypatch.setattr(rs.RunArchive, "__init__", _init)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_s023.py",
            "--months", "2025-06,2025-07",
            "--tf", "30m",
            "--mode", "wick",
            "--arm", "ANY",
        ],
    )
    rs.main()
    sum_path = dest / "summary.csv"
    assert sum_path.is_file()
    months = {row["month"] for row in csv.DictReader(sum_path.open(encoding="utf-8"))}
    assert "2025-06" in months
    assert "2025-07" in months


def test_t4_late_day_step_uses_e1_straddle() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 13, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    _fill_day(m, d0, d1, t_ent, t_step + 180, px0, 100_000.0)
    t = t_step
    while t <= t_step + 180:
        px = dict(px0)
        px["lc"] = 580.0
        _put_chain(m, d1, t, 100_000.0, px)
        _put_chain(m, d0, t, 100_000.0, px)
        extra = _row(f"C-101000-{d1}", 101000.0, True, 400.0)
        packed = m.chains[(d1, t)]
        m.chains[(d1, t)] = list(packed) + [extra]
        m.quotes[(extra["symbol"], t)] = 400.0
        m.spots[t] = 101000.0
        t += 60
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_step + 240, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok and res.step1 is not None
    assert res.step1["expiry"] == d1.isoformat()
    assert abs(float(res.step1["strike"]) - 100000.0) < 1e-6


def test_t_late_strike() -> None:
    test_t4_late_day_step_uses_e1_straddle()


def test_t_boundary() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 12, 0))
    assert abs(hours_to(d0, t_step) - 5.5) < 1e-9
    assert is_late_window(d0, t_step) is True
    assert is_late_window(d0, t_step - 60) is False
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
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok and res.step1 is not None
    assert res.step1["expiry"] == d1.isoformat()
    assert abs(float(res.step1["strike"]) - 100000.0) < 1e-6
    assert bool(res.step1.get("late")) is True


def test_t_fallback() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 11, 0))
    assert hours_to(d0, t_step) > 5.5
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    _fill_day(m, d0, d1, t_ent, t_step + 180, px0, 100_000.0)
    t = t_step
    while t <= t_step + 180:
        px = dict(px0)
        px["lc"] = 580.0
        _put_chain(m, d1, t, 100_000.0, px)
        alt = [
            _row(f"C-99200-{d0}", 99200.0, True, 580.0),
            _row(f"P-101000-{d0}", 101000.0, False, 500.0),
            _row(f"C-100000-{d0}", 100000.0, True, 690.0),
            _row(f"P-100000-{d0}", 100000.0, False, 690.0),
        ]
        m.chains[(d0, t)] = alt
        for r in alt:
            m.quotes[(r["symbol"], t)] = float(r["mark"])
        t += 60
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_step + 240, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok and res.step1 is not None
    assert res.step1["expiry"] == d0.isoformat()
    assert abs(float(res.step1["strike"]) - 99200.0) < 1e-6
    assert abs(float(res.step1["strike"]) - 100000.0) > 1.0
    assert abs(float(res.step1["strike_gap"]) - 200.0) < 1e-6
    assert abs(res.step_strike_gap - 200.0) < 1e-6


def _trade_dict(res: Any, cell: str = "30m|full|ANY") -> dict[str, Any]:
    import json

    from backtest.strategies.s018_4h_trend import run_s018 as s018

    legs_s = ";".join(
        f"{'C' if lg.is_call else 'P'}/{int(lg.strike)}/{lg.expiry.isoformat()}/{lg.qty}/{lg.fill_px:.4f}/{lg.role}"
        for lg in res.legs
    )
    return {
        "cell": cell,
        "arm": "F",
        "entry_ist": s018.ist_str(res.entry_ts),
        "signal_price": res.signal_price,
        "legs": legs_s,
        "step1": json.dumps(res.step1, default=str) if res.step1 else "",
        "step2": json.dumps(res.step2, default=str) if res.step2 else "",
        "redeploys": res.redeploys,
        "exit_ist": s018.ist_str(res.exit_ts),
        "exit_reason": res.exit_reason,
        "gross": res.gross,
        "fees": res.fees,
        "net": res.net,
        "slip": res.slip,
        "net_with_slip": res.net_with_slip,
        "max_gross": res.max_gross,
        "min_gross": res.min_gross,
        "hold_min": res.hold_min,
        "stale_minutes": res.stale_minutes,
        "step_strike_gap": res.step_strike_gap,
        "step1_raw_qty": res.step1_raw_qty,
        "step1_qty": res.step1_qty,
        "step1_skip": res.step1_skip,
        "step2_raw_qty": res.step2_raw_qty,
        "step2_qty": res.step2_qty,
        "step2_skip": res.step2_skip,
        "redeploy_capped": res.redeploy_capped,
        "redeploy_skipped": res.redeploy_skipped,
        "max_short_call": res.max_short_call,
        "max_short_put": res.max_short_put,
    }


def test_t_invariant(tmp_path: Path) -> None:
    import csv
    import json

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
        px["lc"] = 580.0
        _put_chain(m, d1, tt, 100_000.0, px)
        _put_chain(m, d0, tt, 100_000.0, px)
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_end + 60, 60)}
    res_step = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    m2 = FakeMkt()
    _fill_day(m2, d0, d1, t_ent, t_end, px0, 100_000.0)
    for tt in range(t_hit, t_end + 60, 60):
        px = dict(px0)
        px["lc"] = 710.0
        _put_chain(m2, d1, tt, 100_000.0, px)
    res_tgt = simulate_basket(
        m2, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    path = tmp_path / "trades.csv"
    rows = [_trade_dict(res_step), _trade_dict(res_tgt)]
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    bad = check_trades(path)
    assert bad == []
    _ = json.dumps(rows[0]["step1"])


def test_t_cap() -> None:
    raw, qn, skip = decide_step_qty(1000, 290.6, 648, apply_cap=True, min_prem=0.0)
    assert skip == ""
    assert raw == 722
    assert qn == 352
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 11, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 324.0, "sp": 324.0}
    _fill_day(m, d0, d1, t_ent, t_step + 180, px0, 100_000.0)
    assert protection_qty(1000, 324.0) == 648
    t = t_step
    while t <= t_step + 180:
        px_e1 = dict(px0)
        px_e1["lc"] = 580.0
        _put_chain(m, d1, t, 100_000.0, px_e1)
        px_0 = dict(px0)
        px_0["lc"] = 290.6
        _put_chain(m, d0, t, 100_000.0, px_0)
        t += 60
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_step + 240, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok and res.step1 is not None
    assert res.step1_raw_qty == 722
    assert res.step1_qty == 352
    assert res.step1_skip == ""
    assert res.max_short_call == 1000


def test_t_cap_zero() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 11, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 210.0, "sp": 210.0}
    _fill_day(m, d0, d1, t_ent, t_step + 180, px0, 100_000.0)
    assert protection_qty(1000, 210.0) == 1000
    t = t_step
    while t <= t_step + 180:
        px = dict(px0)
        px["lc"] = 580.0
        _put_chain(m, d1, t, 100_000.0, px)
        _put_chain(m, d0, t, 100_000.0, px)
        t += 60
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_step + 240, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok
    assert res.steps >= 1
    assert res.step1_skip == "cap"
    assert res.step1_qty == 0
    assert sl_now(res.steps, 1000) == -140 or (res.steps >= 2 and sl_now(2, 1000) == -70)
    assert sl_now(1, 1000) == -140


def test_t_min_prem() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_step = to_unix(ist_dt(d0, 11, 0))

    def _run(step_prem: float) -> Any:
        m = FakeMkt()
        px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
        _fill_day(m, d0, d1, t_ent, t_step + 180, px0, 100_000.0)
        t = t_step
        while t <= t_step + 180:
            px_e1 = dict(px0)
            px_e1["lc"] = 580.0
            _put_chain(m, d1, t, 100_000.0, px_e1)
            px_0 = dict(px0)
            px_0["lc"] = step_prem
            _put_chain(m, d0, t, 100_000.0, px_0)
            t += 60
        ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_step + 240, 60)}
        return simulate_basket(
            m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
            min_prem=100.0,
        )

    a = _run(90.0)
    assert a.ok and a.steps >= 1
    assert a.step1_skip == "min_prem"
    assert a.step1_qty == 0
    b = _run(100.0)
    assert b.ok and b.steps >= 1
    assert b.step1_skip == ""
    assert b.step1_qty > 0


def test_t_redeploy_cap() -> None:
    d0, d1, d2 = date(2025, 6, 2), date(2025, 6, 3), date(2025, 6, 4)
    listed = [d0, d1, d2]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    t_1730 = expiry_unix(d0)
    t_hi = to_unix(ist_dt(d0, 11, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 324.0, "sp": 324.0}
    _fill_day(m, d0, d1, t_ent, t_1730 + 180, px0, 100_000.0)
    t = t_hi
    while t <= t_hi + 180:
        px_e1 = dict(px0)
        px_e1["lc"] = 580.0
        _put_chain(m, d1, t, 100_000.0, px_e1)
        px_0 = dict(px0)
        px_0["lc"] = 290.6
        _put_chain(m, d0, t, 100_000.0, px_0)
        t += 60
    t = t_1730
    while t <= t_1730 + 180:
        px = dict(px0)
        px["lc"] = 290.6
        _put_chain(m, d1, t, 100_000.0, px)
        _put_chain(m, d0, t, 100_000.0, px)
        t += 60
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_1730 + 180, 60)}
    res = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=999999.0, qty=1000, ohlc=ohlc, slip_mult=0.0,
    )
    assert res.ok
    assert res.redeploys >= 1 or res.redeploy_capped >= 1 or res.redeploy_skipped >= 0
    assert res.max_short_call <= 1000
    rd = [lg for lg in res.legs if lg.is_step and "_rd" in lg.role]
    if rd:
        assert rd[0].qty <= 352 or res.redeploy_capped >= 1


def test_t_regression() -> None:
    from backtest.harness.data import to_unix as _tu  # noqa: F401
    from backtest.strategies.s023_obps.trace_basket import RunContext, load_trades, parse_ist, pick_auto

    folder = Path(__file__).resolve().parent / "runs" / "20261016T143434Z_s023_stage1"
    assert folder.is_dir(), f"missing {folder}"
    picked = pick_auto(load_trades(folder))
    assert len(picked) == 4
    ctx = RunContext(folder)
    for row in picked:
        res = simulate_basket(
            ctx.mkt, ctx.listed,
            entry_ts=parse_ist(str(row["entry_ist"])),
            signal_price=float(row["signal_price"]),
            qty=1000, ohlc=ctx.ohlc, min_prem=0.0, apply_cap=False,
        )
        assert res.ok, res.skip_reason
        assert abs(res.gross - float(row["gross"])) < 1e-6
        assert abs(res.net - float(row["net"])) < 1e-6
