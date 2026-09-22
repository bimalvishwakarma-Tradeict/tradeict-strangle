"""S008 look-ahead / exit-cost / settlement / strike-gap / OTM tests."""

from __future__ import annotations

import sys
from datetime import date
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
    DEFAULT_MAX_LEG_PREMIUM_PCT,
    DEFAULT_MAX_STRIKE_GAP,
    ChainCache,
    S008RegimeGateStrategy,
    enforce_oos_threshold,
    iter_weekdays,
    premium_target_usd,
    settlement_spot,
    spot_based_targets,
)


# Shared: every test re-reads the same (expiry, ts) chains.
_CHAIN_CACHE = ChainCache()


def _load_spot() -> dict[int, float]:
    p = find_spot_csv()
    assert p is not None
    return load_spot_map(p)


def test_lookahead_sig_truncate() -> None:
    """(a) Truncate history at T → same sig as full expanding path."""
    spot = _load_spot()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
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
    """(b) Every traded basket: exit_fee=0 and exit_slip=0."""
    ensure_slip_table()
    spot = _load_spot()
    store = MarksStore()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    strat = S008RegimeGateStrategy(
        gate="none",
        threshold=0.90,
        entry_hour=9,
        entry_minute=0,
        qty=100,
        max_strike_gap=DEFAULT_MAX_STRIKE_GAP,
        strike_mode="points",
    )
    n = 0
    for d in iter_weekdays(d0, d1):
        s = sigs.get(d)
        if s is None:
            continue
        b = strat.simulate_day(
            d=d, sig=s, store=store, spot_close=spot, chain_cache=_CHAIN_CACHE
        )
        if b.skipped:
            continue
        n += 1
        assert b.exit_fee == 0.0, f"exit_fee>0 on {d}: {b.exit_fee}"
        assert b.exit_slip_cost == 0.0, f"exit_slip>0 on {d}: {b.exit_slip_cost}"
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
        gate="none",
        threshold=0.90,
        entry_hour=9,
        entry_minute=0,
        qty=100,
        max_strike_gap=DEFAULT_MAX_STRIKE_GAP,
        strike_mode="points",
    )
    n = 0
    for d in iter_weekdays(d0, d1):
        s = sigs.get(d)
        if s is None:
            continue
        b = strat.simulate_day(
            d=d, sig=s, store=store, spot_close=spot, chain_cache=_CHAIN_CACHE
        )
        if b.skipped:
            continue
        n += 1
        ts_1730 = to_unix(ist_dt(d, 17, 30))
        ts_1729 = to_unix(ist_dt(d, 17, 29))
        assert b.settle_ts in (ts_1730, ts_1729), (
            f"SETTLE FAIL {d}: settle_ts={b.settle_ts}"
        )
        assert (b.settle_hour, b.settle_minute) in ((17, 30), (17, 29))
        helper = settlement_spot(spot, d)
        assert helper is not None and helper[0] == b.settle_ts
    store.close()
    assert n > 0
    print(f"SETTLEMENT_SPOT PASS n_traded={n}")


def test_strike_gap_guard() -> None:
    """(d) Traded baskets: gaps <= max; targets spot-based; 2025-11-05 skip."""
    ensure_slip_table()
    spot = _load_spot()
    store = MarksStore()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    max_gap = DEFAULT_MAX_STRIKE_GAP
    strat = S008RegimeGateStrategy(
        gate="none",
        threshold=0.90,
        entry_hour=9,
        entry_minute=0,
        qty=100,
        max_strike_gap=max_gap,
        strike_mode="points",
    )
    n_traded = 0
    n_strike_skip = 0
    for d in iter_weekdays(d0, d1):
        s = sigs.get(d)
        if s is None:
            continue
        b = strat.simulate_day(
            d=d, sig=s, store=store, spot_close=spot, chain_cache=_CHAIN_CACHE
        )
        if b.skip_reason in ("STRIKE_UNAVAILABLE", "CHAIN_ONE_SIDED", "ITM_STRIKE"):
            n_strike_skip += 1
            assert b.skipped
            assert b.strike_ok is False
            continue
        if b.skipped:
            continue
        n_traded += 1
        assert b.strike_ok is True
        assert b.call_gap <= max_gap
        assert b.put_gap <= max_gap
        tc, tp = spot_based_targets(b.spot_entry)
        assert b.target_call_k == tc and b.target_put_k == tp
        assert b.target_call_k > b.spot_entry
        assert b.target_put_k < b.spot_entry
    d_bad = date(2025, 11, 5)
    if d_bad in sigs:
        b_bad = strat.simulate_day(
            d=d_bad,
            sig=sigs[d_bad],
            store=store,
            spot_close=spot,
            chain_cache=_CHAIN_CACHE,
        )
        assert b_bad.skipped and b_bad.skip_reason in (
            "STRIKE_UNAVAILABLE",
            "CHAIN_ONE_SIDED",
        ), (
            f"2025-11-05 must skip strike, got "
            f"{b_bad.skipped}/{b_bad.skip_reason} put={b_bad.chosen_put_k}"
        )
    store.close()
    assert n_traded > 0
    assert n_strike_skip >= 1
    print(
        f"STRIKE_GAP PASS n_traded={n_traded} "
        f"strike_skips={n_strike_skip} max_gap={max_gap:.0f}"
    )


def test_otm_only() -> None:
    """(e) No traded leg is ITM vs entry spot (points + premium + delta)."""
    ensure_slip_table()
    spot = _load_spot()
    store = MarksStore()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    n = 0
    for mode in ("points", "premium", "delta"):
        strat = S008RegimeGateStrategy(
            gate="none",
            threshold=0.90,
            entry_hour=9,
            entry_minute=0,
            qty=100,
            max_strike_gap=DEFAULT_MAX_STRIKE_GAP,
            strike_mode=mode,  # type: ignore[arg-type]
        )
        for d in iter_weekdays(d0, d1):
            s = sigs.get(d)
            if s is None:
                continue
            b = strat.simulate_day(
            d=d, sig=s, store=store, spot_close=spot, chain_cache=_CHAIN_CACHE
        )
            if b.skipped:
                continue
            n += 1
            assert b.chosen_call_k > b.spot_entry, (
                f"OTM_ONLY FAIL {d} {mode}: call {b.chosen_call_k} "
                f"<= spot {b.spot_entry}"
            )
            assert b.chosen_put_k < b.spot_entry, (
                f"OTM_ONLY FAIL {d} {mode}: put {b.chosen_put_k} "
                f">= spot {b.spot_entry}"
            )
    store.close()
    assert n > 0
    print(f"OTM_ONLY PASS n_traded_legs_checked={n}")


def test_premium_band() -> None:
    """(g) No traded leg richer than max_leg_premium_pct of entry spot."""
    ensure_slip_table()
    spot = _load_spot()
    store = MarksStore()
    d0 = date(2025, 11, 1)
    d1 = date(2025, 11, 30)
    warm0 = date(2025, 10, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    band_pct = DEFAULT_MAX_LEG_PREMIUM_PCT
    n_traded = 0
    n_band_skip = 0
    for mode in ("points", "premium", "delta"):
        strat = S008RegimeGateStrategy(
            gate="none",
            threshold=0.90,
            entry_hour=9,
            entry_minute=0,
            qty=100,
            max_strike_gap=DEFAULT_MAX_STRIKE_GAP,
            strike_mode=mode,  # type: ignore[arg-type]
            max_leg_premium_pct=band_pct,
        )
        for d in iter_weekdays(d0, d1):
            s = sigs.get(d)
            if s is None:
                continue
            b = strat.simulate_day(
            d=d, sig=s, store=store, spot_close=spot, chain_cache=_CHAIN_CACHE
        )
            if b.skip_reason == "LEG_PREMIUM_OUT_OF_BAND":
                n_band_skip += 1
                assert b.skipped and b.strike_ok is False
                continue
            if b.skipped:
                continue
            n_traded += 1
            band = premium_target_usd(b.spot_entry, band_pct)
            assert b.call_mark <= band, (
                f"PREMIUM_BAND FAIL {d} {mode}: call {b.call_mark} > {band}"
            )
            assert b.put_mark <= band, (
                f"PREMIUM_BAND FAIL {d} {mode}: put {b.put_mark} > {band}"
            )
    # 2025-11-05 premium mode leg was 0.6636% of spot — must be caught.
    d_bad = date(2025, 11, 5)
    if d_bad in sigs:
        strat_bad = S008RegimeGateStrategy(
            gate="none",
            threshold=0.90,
            entry_hour=9,
            entry_minute=0,
            qty=100,
            max_strike_gap=DEFAULT_MAX_STRIKE_GAP,
            strike_mode="premium",
            max_leg_premium_pct=band_pct,
        )
        b_bad = strat_bad.simulate_day(
            d=d_bad,
            sig=sigs[d_bad],
            store=store,
            spot_close=spot,
            chain_cache=_CHAIN_CACHE,
        )
        assert b_bad.skipped, (
            f"2025-11-05 premium mode must skip, got "
            f"call={b_bad.call_mark} put={b_bad.put_mark}"
        )
    store.close()
    assert n_traded > 0
    print(
        f"PREMIUM_BAND PASS n_traded={n_traded} "
        f"band_skips={n_band_skip} band_pct={band_pct:.2f}"
    )


def test_oos_lock_and_premium_target() -> None:
    """(f) OOS needs an argument threshold; premium target is % of spot."""
    try:
        enforce_oos_threshold("oos", None)
    except ValueError:
        pass
    else:
        raise AssertionError("OOS_LOCK FAIL: missing threshold was accepted")
    assert enforce_oos_threshold("oos", 0.90) == 0.90
    # 0.034% of 100000 = 34.0 (old 0.28571% would have been 285.71)
    assert abs(premium_target_usd(100_000.0, 0.034) - 34.0) < 1e-9
    print("OOS_LOCK PASS + premium_target_pct is percent-of-spot")


def main() -> int:
    test_lookahead_sig_truncate()
    test_exit_cost_zero()
    test_settlement_spot_timestamp()
    test_strike_gap_guard()
    test_otm_only()
    test_premium_band()
    test_oos_lock_and_premium_target()
    print("ALL S008 TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
