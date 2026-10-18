"""S024 stage1 sim unit tests."""

from __future__ import annotations

from datetime import date
from typing import Any

from backtest.strategies.s012_trend_follow.engine import intrinsic
from backtest.strategies.s023_obps.sim import expiry_unix, hours_to, opt_symbol
from backtest.strategies.s024_rdbb.sim import (
    QTY,
    choose_expiry,
    fee_round_trip,
    pick_premium_match,
    simulate_basket,
    sl_level,
)


class FakeMkt:
    def __init__(
        self,
        spot: dict[int, float],
        chains: dict[date, list[dict[str, Any]]],
        marks: dict[tuple[str, int], float],
    ) -> None:
        self._spot = spot
        self._chains = chains
        self._marks = marks

    def spot(self, t: int) -> float | None:
        m = (int(t) // 60) * 60
        if m in self._spot:
            return float(self._spot[m])
        return float(next(iter(self._spot.values()))) if self._spot else None

    def chain(self, exp: date, t: int) -> tuple[list[dict[str, Any]], str] | None:
        rows = self._chains.get(exp)
        if rows is None:
            return None
        return list(rows), "m"

    def ensure(self, symbol: str, exp: date) -> None:
        return None

    def quote(self, symbol: str, t: int) -> tuple[float, str] | None:
        m = (int(t) // 60) * 60
        px = self._marks.get((symbol, m))
        if px is None:
            return None
        return float(px), "m"


def test_fee_round_trip_81697() -> None:
    got = fee_round_trip(81697.0, QTY, 500.0)
    assert round(got, 2) == 19.28


def test_premium_match_call_stays_atm() -> None:
    rows = [
        {"is_call": True, "strike": 85000.0, "mark": 350.0},
        {"is_call": False, "strike": 85000.0, "mark": 280.0},
        {"is_call": False, "strike": 85200.0, "mark": 320.0},
        {"is_call": True, "strike": 85200.0, "mark": 200.0},
    ]
    picked = pick_premium_match(rows, 85000.0)
    assert picked == (85000.0, 85200.0)


def test_trail_sl_from_peak() -> None:
    assert sl_level(100.0) == -50.0
    assert sl_level(140.0) == -30.0
    assert sl_level(-20.0) == -100.0


def test_entry_2_9h_uses_1dte() -> None:
    e0 = date(2025, 6, 10)
    e1 = date(2025, 6, 11)
    t = expiry_unix(e0) - int(2.9 * 3600)
    assert hours_to(e0, t) <= 3.0
    exp, _e0, used = choose_expiry(t, [e0, e1])
    assert used is True
    assert exp == e1


def test_expiry_settle_fee_zero_intrinsic() -> None:
    e0 = date(2025, 6, 10)
    e1 = date(2025, 6, 11)
    t0 = expiry_unix(e0) - int(6 * 3600)
    t_exp = expiry_unix(e0)
    spot_px = 85000.0
    rows = [
        {"is_call": True, "strike": 85000.0, "mark": 100.0},
        {"is_call": False, "strike": 85000.0, "mark": 100.0},
    ]
    sc = opt_symbol(True, 85000.0, e0)
    sp = opt_symbol(False, 85000.0, e0)
    marks: dict[tuple[str, int], float] = {}
    spot: dict[int, float] = {}
    t = t0
    while t <= t_exp:
        marks[(sc, t)] = 100.0
        marks[(sp, t)] = 100.0
        spot[t] = spot_px
        t += 60
    mkt = FakeMkt(spot, {e0: rows, e1: rows}, marks)
    res = simulate_basket(mkt, [e0, e1], entry_ts=t0)
    assert res.ok
    assert res.exit_reason == "EXPIRY"
    assert res.call_exit == intrinsic(True, 85000.0, spot_px)
    assert res.put_exit == intrinsic(False, 85000.0, spot_px)
    # settle fee/slip 0: only two entry fills
    assert abs(res.slip - 10.0) < 1e-9
    entry_fees = res.fees
    assert entry_fees > 0
    # no extra exit fees: round-trip would be ~2x if closed on marks
    closed = 2.0 * entry_fees
    assert res.fees < closed * 0.6
    assert abs(res.gross - 200.0) < 1e-6
