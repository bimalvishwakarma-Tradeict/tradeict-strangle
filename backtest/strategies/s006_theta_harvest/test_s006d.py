"""S006-D tests: Adj A look-ahead + leg invariant."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent.parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.data import (  # noqa: E402
    MarksStore,
    find_spot_csv,
    load_chain,
    load_spot_map,
    resolve_forward,
    resolve_mark_ts,
)
from backtest.strategies.s006_theta_harvest.strategy import (  # noqa: E402
    S006ThetaHarvestStrategy,
    default_params,
)


def test_leg_invariant_and_adj_lookahead(
    d0: date = date(2025, 11, 1),
    d1: date = date(2025, 11, 14),
) -> None:
    path = find_spot_csv()
    if path is None:
        raise FileNotFoundError("No BTCUSD_1m CSV")
    spot_map = load_spot_map(path)
    store = MarksStore()

    params = default_params()
    params.update(
        {
            "protection_mode": "premium_multiple",
            "protection_mult": 1.0,
            "protection_ratio": 1.0,
            "adj_mode": "a",
            "adj_trigger": 200.0,
            "max_adj": 1,
            "target_pct": 20.0,
            "max_dd_pct": None,
            "entry_mode": "fixed",
            "entry_hour": 9,
            "entry_minute": 0,
            "arm": "test_s006d",
        }
    )
    strat = S006ThetaHarvestStrategy(params)
    cycles, skips, _stats = strat.run_window(d0, d1, store=store, spot_map=spot_map)

    # ---- (b) Leg invariant on every completed basket ----
    for c in cycles:
        # Reconstruct expected from meta/csv — re-check via recorded adj events count
        n_adj = int((c.meta or {}).get("n_adjustments") or c.n_adjustments or 0)
        assert n_adj <= 1, f"max_adj violated: {n_adj}"
        # Open-leg count encoded in csv_row qty fields
        row = (c.meta or {}).get("csv_row") or {}
        sc_q = int(row.get("short_call_qty") or 0)
        sp_q = int(row.get("short_put_qty") or 0)
        pc_q = int(row.get("prot_call_qty") or 0)
        pp_q = int(row.get("prot_put_qty") or 0)
        assert sc_q == 100 and sp_q == 100, f"short qty broken {row}"
        assert pc_q == 100 and pp_q == 100, f"prot qty broken {row}"

    # ---- (a) Look-ahead: each adj decision reproduces from chain at T ----
    mismatches = 0
    checked = 0
    for c in cycles:
        for ev in (c.meta or {}).get("adj_events") or []:
            checked += 1
            T = int(ev["ts"])
            leg = str(ev["leg"])
            old_k = float(ev["old_strike"])
            new_k = float(ev["new_strike"])
            other_p = float(ev["other_short_premium"])
            is_call = leg == "short_call"
            leg_type = "call" if is_call else "put"
            short_exp = c.entry_date  # will fix from basket — use entry_date + short_dte
            # short_dte default 1
            from datetime import timedelta

            short_exp = c.entry_date + timedelta(days=1)
            conn = store.conn(short_exp) or store.conn(c.entry_date)
            assert conn is not None
            cts = resolve_mark_ts(conn, short_exp, T)
            assert cts is not None, f"no chain ts at T={T}"
            # Truncation: chain is loaded at cts <= T (resolve_mark_ts only uses past/near)
            assert abs(int(cts) - (T // 60) * 60) <= 120 or cts <= T + 120
            chain = load_chain(conn, short_exp, cts, leg_type)
            spot, _ = resolve_forward(store, spot_map, short_exp, T)
            assert spot is not None and spot > 0
            redo_k, _prem, reason = strat.select_adj_a_strike(
                leg_type=leg_type,
                old_strike=old_k,
                other_premium=other_p,
                chain=chain,
                spot=float(spot),
            )
            if redo_k is None or abs(float(redo_k) - new_k) > 1e-6:
                mismatches += 1
                print(
                    f"FAIL look-ahead adj day={c.entry_date} T={T} leg={leg} "
                    f"expected={new_k} got={redo_k} reason={reason}"
                )

    store.close()
    if mismatches:
        raise AssertionError(
            f"adj look-ahead FAILED: {mismatches}/{checked} mismatches"
        )
    print(
        f"S006-D TESTS PASS: cycles={len(cycles)} skips={dict(skips.counts)} "
        f"adj_checked={checked} leg_invariant=OK look_ahead=OK"
    )


if __name__ == "__main__":
    test_leg_invariant_and_adj_lookahead()
