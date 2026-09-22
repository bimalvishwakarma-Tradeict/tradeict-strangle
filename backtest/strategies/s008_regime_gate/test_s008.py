"""S008 look-ahead / exit-cost / settlement tests."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.costs import ensure_slip_table  # noqa: E402
from backtest.harness.data import (  # noqa: E402
    MarksStore,
    find_spot_csv,
    load_spot_map,
    ist_dt,
    to_unix,
)
from backtest.strategies.s008_regime_gate.signal import (  # noqa: E402
    build_signals_through,
)
from backtest.strategies.s008_regime_gate.strategy import (  # noqa: E402
    S008RegimeGateStrategy,
    iter_weekdays,
    settlement_spot,
)


def _load_spot() -> dict[int, float]:
    p = find_spot_csv()
    assert p is not None
    return load_spot_map(p)


def test_lookahead_sig_truncate() -> None:
    """(a) Truncate history at T → same sig as full expanding path."""
    spot = _load_spot()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    # warm-up from earlier so ranks are non-trivial
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    full = build_signals_through(days, spot, through=d1)
    mismatches = 0
    checked = 0
    for d in iter_weekdays(d0, d1):
        if d not in full:
            continue
        trunc = build_signals_through(days, spot, through=d)
        assert d in trunc
        a = full[d].sig
        b = trunc[d].sig
        checked += 1
        if abs(a - b) > 1e-12:
            mismatches += 1
            print(f"LOOKAHEAD FAIL {d}: full={a} trunc={b}")
    assert checked > 0
    assert mismatches == 0, f"{mismatches} look-ahead mismatches"
    print(f"LOOKAHEAD PASS checked={checked}")


def test_exit_cost_zero() -> None:
    """(b) Every traded basket: exit_fee=0 and exit_slip=0; fees==entry only."""
    ensure_slip_table()
    spot = _load_spot()
    store = MarksStore()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    strat = S008RegimeGateStrategy(
        gate="none", threshold=0.90, entry_hour=9, entry_minute=0, qty=100
    )
    n = 0
    for d in iter_weekdays(d0, d1):
        s = sigs.get(d)
        if s is None:
            continue
        b = strat.simulate_day(d=d, sig=s, store=store, spot_close=spot)
        if b.skipped:
            continue
        n += 1
        assert b.exit_fee == 0.0, f"exit_fee>0 on {d}: {b.exit_fee}"
        assert b.exit_slip_cost == 0.0, f"exit_slip>0 on {d}: {b.exit_slip_cost}"
        # total fee == entry fee (no exit)
        assert abs((b.entry_fee + b.exit_fee) - b.entry_fee) < 1e-15
    store.close()
    assert n > 0
    print(f"EXIT_COST_ZERO PASS n_traded={n}")


def test_settlement_spot_timestamp() -> None:
    """(c) Payoff spot timestamp is that day's 17:29 or 17:30 IST."""
    ensure_slip_table()
    spot = _load_spot()
    store = MarksStore()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    strat = S008RegimeGateStrategy(
        gate="none", threshold=0.90, entry_hour=9, entry_minute=0, qty=100
    )
    n = 0
    for d in iter_weekdays(d0, d1):
        s = sigs.get(d)
        if s is None:
            continue
        b = strat.simulate_day(d=d, sig=s, store=store, spot_close=spot)
        if b.skipped:
            continue
        n += 1
        ts_1730 = to_unix(ist_dt(d, 17, 30))
        ts_1729 = to_unix(ist_dt(d, 17, 29))
        assert b.settle_ts in (ts_1730, ts_1729), (
            f"SETTLE FAIL {d}: settle_ts={b.settle_ts} "
            f"not in {{17:30={ts_1730}, 17:29={ts_1729}}}"
        )
        assert (b.settle_hour, b.settle_minute) in ((17, 30), (17, 29))
        # settlement helper agrees
        helper = settlement_spot(spot, d)
        assert helper is not None and helper[0] == b.settle_ts
    store.close()
    assert n > 0
    print(f"SETTLEMENT_SPOT PASS n_traded={n}")


def main() -> int:
    test_lookahead_sig_truncate()
    test_exit_cost_zero()
    test_settlement_spot_timestamp()
    print("ALL S008 TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
