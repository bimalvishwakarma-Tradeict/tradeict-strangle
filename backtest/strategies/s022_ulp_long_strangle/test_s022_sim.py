"""S022 path sim: cash conservation, post-hoc T/SL, ratchet, adj fills, no look-ahead."""

from __future__ import annotations

from datetime import date, timedelta

from backtest.strategies.s012_trend_follow.engine import ist_date
from backtest.strategies.s018_4h_trend import run_s018 as s018
from backtest.strategies.s022_ulp_long_strangle.sim import (
    QTY,
    SLIP_PTS,
    buy_fill,
    cash_conservation_net,
    first_exit,
    sell_fill,
    simulate_path,
    sl_level,
    slip_usd,
)


class MemMkt:
    def __init__(self) -> None:
        self.spots: dict[int, float] = {}
        self.marks: dict[str, dict[int, float]] = {}
        self.rows: list[dict] = []
        self.quoted: list[tuple[str, int]] = []

    def spot(self, t: int) -> float | None:
        return self.spots.get((int(t) // 60) * 60)

    def quote(self, symbol: str, t: int) -> tuple[float, str] | None:
        minute = (int(t) // 60) * 60
        self.quoted.append((str(symbol), minute))
        series = self.marks.get(str(symbol), {})
        if minute in series:
            return float(series[minute]), "real"
        for age in (60, 120, 180, 240, 300):
            k = minute - age
            if k in series:
                return float(series[k]), "ffill"
        return None

    def chain(self, exp: date, t: int) -> tuple[list[dict], str] | None:
        return list(self.rows), "real"

    def ensure(self, symbol: str, exp: date) -> None:
        return None


def _row(sym: str, k: float, is_call: bool) -> dict:
    return {"symbol": sym, "strike": float(k), "is_call": bool(is_call)}


def _chain_atm100k() -> list[dict]:
    ks = (99_600.0, 100_000.0, 100_400.0, 99_000.0, 101_000.0)
    rows = []
    for k in ks:
        rows.append(_row(f"C-{int(k)}", k, True))
        rows.append(_row(f"P-{int(k)}", k, False))
    return rows


def _fill_flat(
    mkt: MemMkt,
    t0: int,
    n_min: int,
    spot: float,
    c_px: float,
    p_px: float,
    c_sym: str = "C-99600",
    p_sym: str = "P-100400",
) -> None:
    for i in range(n_min + 1):
        t = t0 + i * 60
        mkt.spots[t] = float(spot)
        mkt.marks.setdefault(c_sym, {})[t] = float(c_px)
        mkt.marks.setdefault(p_sym, {})[t] = float(p_px)


def test_sl_ratchet_levels() -> None:
    sl = 100.0
    assert sl_level(0.0, sl) == -100.0
    assert sl_level(10.0, sl) == -95.0
    assert sl_level(25.0, sl) == -90.0


def test_entry_quotes_not_after_signal_ts() -> None:
    t0 = 1_700_000_040
    exp = ist_date(t0) + timedelta(days=1)
    exp_ts = int(s018.expiry_unix(exp))
    mkt = MemMkt()
    mkt.rows = _chain_atm100k()
    n = max(3, (exp_ts - t0) // 60)
    _fill_flat(mkt, t0, min(n, 20), 100_000.0, 80.0, 80.0)
    mkt.quoted.clear()
    minutes = list(range(t0, t0 + 10 * 60, 60))
    simulate_path(mkt, t0, exp, 400, "itm", 150.0, minutes)
    assert mkt.quoted
    assert mkt.quoted[0][1] == t0
    assert mkt.quoted[1][1] == t0
    assert all(t <= t0 for _s, t in mkt.quoted[:2])


def test_adjustment_fills_and_ref_reset() -> None:
    t0 = 1_700_000_040
    exp = ist_date(t0) + timedelta(days=1)
    mkt = MemMkt()
    mkt.rows = _chain_atm100k()
    minutes = [t0 + i * 60 for i in range(6)]
    for t in minutes:
        mkt.spots[t] = 100_000.0
        mkt.marks.setdefault("C-99600", {})[t] = 100.0
        mkt.marks.setdefault("P-100400", {})[t] = 50.0
    t1 = t0 + 60
    mkt.marks["C-99600"][t1] = 125.0
    p = simulate_path(mkt, t0, exp, 400, "itm", 120.0, minutes)
    assert p.ok
    assert p.n_adj_total >= 1
    adj = p.adjs[0]
    assert abs(float(adj["closed_fill"]) - sell_fill(125.0)) < 1e-9
    assert abs(float(adj["opened_fill"]) - buy_fill(float(adj["opened_mark"]))) < 1e-9
    old = [lg for lg in p.legs if abs(lg.strike - 99_600.0) < 1e-6 and lg.opened_ts == t0][0]
    neu = [lg for lg in p.legs if lg.is_call and lg.opened_ts == t1][0]
    put = [lg for lg in p.legs if (not lg.is_call) and lg.open][0]
    assert abs(old.ref - 100.0) < 1e-9
    assert abs(neu.ref - neu.mark0) < 1e-9
    assert abs(put.ref - 50.0) < 1e-9
    assert abs(neu.fill - (neu.mark0 + SLIP_PTS)) < 1e-9


def _assert_cash(p, reason: str) -> None:
    closed = list(p.legs)
    got = cash_conservation_net(closed, p.fees, p.slips)
    assert abs(got - float(p.tsl_net)) < 1e-6, (reason, got, p.tsl_net, p.skip_reason)


def test_cash_conservation_all_exit_paths() -> None:
    t0 = 1_700_000_040
    exp = ist_date(t0) + timedelta(days=1)
    exp_ts = int(s018.expiry_unix(exp))

    mkt = MemMkt()
    mkt.rows = _chain_atm100k()
    minutes = list(range(t0, t0 + 30 * 60, 60))
    _fill_flat(mkt, t0, 40, 100_000.0, 80.0, 80.0)
    mkt.marks["C-99600"][t0 + 60] = 280.0
    mkt.marks["P-100400"][t0 + 60] = 280.0
    p_tgt = simulate_path(mkt, t0, exp, 400, "itm", 150.0, minutes, tsl=(150.0, 200.0))
    assert p_tgt.tsl_reason == "TARGET"
    _assert_cash(p_tgt, "TARGET")

    mkt2 = MemMkt()
    mkt2.rows = _chain_atm100k()
    _fill_flat(mkt2, t0, 40, 100_000.0, 5.0, 5.0)
    p_sl = simulate_path(mkt2, t0, exp, 400, "itm", 150.0, minutes, tsl=(400.0, 50.0))
    assert p_sl.tsl_reason == "SL"
    _assert_cash(p_sl, "SL")

    mkt3 = MemMkt()
    mkt3.rows = _chain_atm100k()
    mins_x = list(range(t0, exp_ts + 1, 60))
    _fill_flat(mkt3, t0, (exp_ts - t0) // 60 + 2, 100_000.0, 40.0, 40.0)
    p_x = simulate_path(mkt3, t0, exp, 400, "itm", 150.0, mins_x)
    assert p_x.ok
    xts, reason, net = first_exit(p_x, 10_000.0, 10_000.0)
    assert reason == "EXPIRY"
    p_x.tsl_reason, p_x.tsl_net, p_x.tsl_ts = reason, net, xts
    _assert_cash(p_x, "EXPIRY")

    mkt4 = MemMkt()
    mkt4.rows = _chain_atm100k()
    minutes4 = [t0 + i * 60 for i in range(8)]
    for t in minutes4:
        mkt4.spots[t] = 100_000.0
        mkt4.marks.setdefault("C-99600", {})[t] = 100.0
        mkt4.marks.setdefault("P-100400", {})[t] = 40.0
    mkt4.marks["C-99600"][t0 + 60] = 130.0
    mkt4.marks["C-99600"][t0 + 120] = 100.0
    mkt4.marks["C-99600"][t0 + 180] = 130.0
    mkt4.marks["C-99600"][t0 + 240] = 250.0
    mkt4.marks["P-100400"][t0 + 240] = 200.0
    p_a = simulate_path(mkt4, t0, exp, 400, "itm", 120.0, minutes4, tsl=(150.0, 400.0))
    assert p_a.n_adj_total >= 1
    assert p_a.tsl_reason in ("TARGET", "SL")
    _assert_cash(p_a, "after-k-adjustments")


def test_posthoc_tsl_matches_brute_on_three_paths() -> None:
    t0 = 1_700_000_040
    exp = ist_date(t0) + timedelta(days=1)
    exp_ts = int(s018.expiry_unix(exp))
    minutes = list(range(t0, min(t0 + 40 * 60, exp_ts) + 1, 60))

    mkt = MemMkt()
    mkt.rows = _chain_atm100k()
    _fill_flat(mkt, t0, 50, 100_000.0, 80.0, 80.0)
    mkt.marks["C-99600"][t0 + 120] = 280.0
    mkt.marks["P-100400"][t0 + 120] = 280.0
    full = simulate_path(mkt, t0, exp, 400, "itm", 150.0, minutes)
    brute = simulate_path(mkt, t0, exp, 400, "itm", 150.0, minutes, tsl=(150.0, 200.0))
    ts, reason, net = first_exit(full, 150.0, 200.0)
    assert reason == brute.tsl_reason == "TARGET"
    assert ts == brute.tsl_ts
    assert abs(net - brute.tsl_net) < 1e-6

    mkt2 = MemMkt()
    mkt2.rows = _chain_atm100k()
    _fill_flat(mkt2, t0, 50, 100_000.0, 8.0, 8.0)
    full2 = simulate_path(mkt2, t0, exp, 400, "itm", 150.0, minutes)
    brute2 = simulate_path(mkt2, t0, exp, 400, "itm", 150.0, minutes, tsl=(400.0, 50.0))
    ts2, r2, n2 = first_exit(full2, 400.0, 50.0)
    assert r2 == brute2.tsl_reason == "SL"
    assert ts2 == brute2.tsl_ts
    assert abs(n2 - brute2.tsl_net) < 1e-6

    mins_x = list(range(t0, exp_ts + 1, 60))
    mkt3 = MemMkt()
    mkt3.rows = _chain_atm100k()
    _fill_flat(mkt3, t0, (exp_ts - t0) // 60 + 2, 100_000.0, 30.0, 30.0)
    full3 = simulate_path(mkt3, t0, exp, 400, "itm", 150.0, mins_x)
    brute3 = simulate_path(mkt3, t0, exp, 400, "itm", 150.0, mins_x, tsl=(50_000.0, 50_000.0))
    ts3, r3, n3 = first_exit(full3, 50_000.0, 50_000.0)
    assert r3 == "EXPIRY"
    assert brute3.tsl_reason == "EXPIRY"
    assert ts3 == brute3.tsl_ts
    assert abs(n3 - brute3.tsl_net) < 1e-6
    _ = QTY
    _ = slip_usd()
