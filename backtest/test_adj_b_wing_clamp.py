#!/usr/bin/env python3
"""
Adj B wing clamp unit checks (selection filter + Adj A clamp unchanged).

No print(). Output: console via sys.stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.engine.wing_exit import clamp_short_strike_inside_wing  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def put_chain() -> list[dict]:
    """Put marks: farther OTM (lower K) = cheaper; wing at 73200."""
    # spot ~75000 → ATM ~75000; puts <= ATM are OTM
    rows = []
    # strike, premium (below a high p_target so they survive premium filter)
    specs = [
        (71000, 40.0),  # beyond wing (further OTM than wing 73200) — reject
        (72000, 55.0),  # beyond wing
        (73200, 80.0),  # == wing — reject
        (74000, 95.0),  # inside wing (toward ATM)
        (74500, 110.0),  # inside, nearer ATM, highest prem among inside
        (75000, 130.0),  # ATM-ish
        (76000, 200.0),  # ITM for put (strike > ATM) — rejected by ITM
    ]
    for k, prem in specs:
        rows.append(
            {
                "option_type": "put",
                "strike": float(k),
                "mark_price": float(prem),
                "best_bid": float(prem),
                "product_id": int(k),
                "symbol": f"P-BTC-{int(k)}-010526",
            }
        )
    return rows


def call_chain() -> list[dict]:
    specs = [
        (78000, 40.0),  # beyond wing 76000
        (77000, 55.0),
        (76000, 80.0),  # == wing
        (75500, 95.0),  # inside
        (75000, 110.0),  # inside highest among inside-ish
        (74000, 200.0),  # ITM call (strike < ATM)
    ]
    rows = []
    for k, prem in specs:
        rows.append(
            {
                "option_type": "call",
                "strike": float(k),
                "mark_price": float(prem),
                "best_bid": float(prem),
                "product_id": int(k),
                "symbol": f"C-BTC-{int(k)}-010526",
            }
        )
    return rows


def main() -> int:
    emit("ADJ B WING CLAMP TESTS")
    emit("=" * 60)
    failed = 0

    # --- case 1: wing open, candidate at/beyond wing → rejected ---
    emit("")
    emit("CASE 1: put wing=73200 — candidates at/beyond wing rejected")
    r1 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=put_chain(),
        spot=75000.0,
        other_short_strike=78000.0,  # call short far above
        wing_strike=73200.0,
    )
    wing_rej = [
        c
        for c in r1.candidates_considered
        if c.get("rejected") == "at_or_beyond_wing"
    ]
    rej_ks = sorted(float(c["strike"]) for c in wing_rej)
    ok1 = (
        r1.success
        and r1.strike is not None
        and float(r1.strike) > 73200.0
        and 73200.0 in rej_ks
        and 72000.0 in rej_ks
        and 71000.0 in rej_ks
    )
    emit(f"  chosen={r1.strike} wing_rejects={rej_ks} success={r1.success}")
    emit(f"  RESULT: {'PASS' if ok1 else 'FAIL'}")
    if not ok1:
        failed += 1

    # --- case 2: wing open, candidate inside → accept ---
    emit("")
    emit("CASE 2: put wing=73200 — inside candidate accepted")
    r2 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=put_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=73200.0,
    )
    # Highest premium among survivors inside wing: 75000@130 then 74500@110...
    # 75000 is ATM (not ITM for put: strike > atm rejected; atm = nearest to spot)
    # atm from strikes ≈ 75000; put ITM if strike > atm → 76000 ITM
    # survivors inside: 74000, 74500, 75000 — highest prem 75000@130
    ok2 = r2.success and r2.strike is not None and float(r2.strike) > 73200.0
    emit(f"  chosen={r2.strike} prem={r2.premium} why={r2.chosen_why[:80] if r2.chosen_why else ''}")
    emit(f"  RESULT: {'PASS' if ok2 else 'FAIL'}")
    if not ok2:
        failed += 1

    # --- case 3: all candidates beyond wing → no_valid_strike ---
    emit("")
    emit("CASE 3: all surviving premiums are beyond wing → abort/no_valid")
    only_beyond = [
        {
            "option_type": "put",
            "strike": 71000.0,
            "mark_price": 90.0,
            "product_id": 71000,
            "symbol": "P-BTC-71000-010526",
        },
        {
            "option_type": "put",
            "strike": 72000.0,
            "mark_price": 100.0,
            "product_id": 72000,
            "symbol": "P-BTC-72000-010526",
        },
        {
            "option_type": "put",
            "strike": 73200.0,
            "mark_price": 110.0,
            "product_id": 73200,
            "symbol": "P-BTC-73200-010526",
        },
        # ITM filler so ATM exists near spot
        {
            "option_type": "put",
            "strike": 75000.0,
            "mark_price": 200.0,  # >= p_target → premium reject
            "product_id": 75000,
            "symbol": "P-BTC-75000-010526",
        },
    ]
    r3 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=only_beyond,
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=73200.0,
    )
    ok3 = (not r3.success) and r3.skip_reason == "no_valid_strike"
    emit(f"  success={r3.success} skip_reason={r3.skip_reason}")
    emit(f"  RESULT: {'PASS' if ok3 else 'FAIL'}")
    if not ok3:
        failed += 1

    # --- case 4: no wing → old behaviour (may pick wing-level / far OTM) ---
    emit("")
    emit("CASE 4: wing_strike=None — no wing filter (old behaviour)")
    r4 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=put_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=None,
    )
    no_wing_rej = [
        c for c in r4.candidates_considered if c.get("rejected") == "at_or_beyond_wing"
    ]
    # Without wing filter, highest prem below 150 among non-ITM is 75000@130
    # (76000 is ITM). Same as with wing for this chain — also check that
    # 73200 is NOT rejected as at_or_beyond_wing.
    ok4 = r4.success and len(no_wing_rej) == 0
    # And 73200 can be in pool (rejected=None) if premium qualifies
    considered_732 = [
        c for c in r4.candidates_considered if abs(float(c["strike"]) - 73200) < 1e-9
    ]
    ok4 = ok4 and bool(considered_732) and considered_732[0].get("rejected") is None
    emit(f"  chosen={r4.strike} wing_rej_count={len(no_wing_rej)} row732={considered_732}")
    emit(f"  RESULT: {'PASS' if ok4 else 'FAIL'}")
    if not ok4:
        failed += 1

    # --- case 5: Adj A clamp helper unchanged ---
    emit("")
    emit("CASE 5: Adj A clamp_short_strike_inside_wing unchanged")
    # From existing test_wing_exit semantics: call wanted past wing → clamp
    clamped, status = clamp_short_strike_inside_wing(
        leg="call",
        wanted_strike=85000.0,
        wing_strike=84000.0,
        available_strikes=[82000.0, 83000.0, 83500.0, 84000.0, 85000.0],
        current_short_strike=82000.0,
    )
    ok5a = status == "clamped" and clamped is not None and float(clamped) < 84000.0
    dead, status_d = clamp_short_strike_inside_wing(
        leg="put",
        wanted_strike=70000.0,
        wing_strike=71000.0,
        available_strikes=[70000.0, 71000.0, 72000.0],
        current_short_strike=72000.0,
    )
    # put: want 70000 <= wing 71000 → crosses; cands with k < 72000 and k > 71000 → none → dead_end
    ok5b = status_d == "dead_end" and dead is None
    ok5 = ok5a and ok5b
    emit(f"  call clamp status={status} clamped={clamped}")
    emit(f"  put dead_end status={status_d} clamped={dead}")
    emit(f"  RESULT: {'PASS' if ok5 else 'FAIL'}")
    if not ok5:
        failed += 1

    # Bonus: call wing filter
    emit("")
    emit("BONUS: call wing=76000 — beyond rejected, inside accepted")
    rc = select_adj_b_strike(
        leg_type="call",
        p_target=150.0,
        chain=call_chain(),
        spot=75000.0,
        other_short_strike=72000.0,
        wing_strike=76000.0,
    )
    call_rej = [
        float(c["strike"])
        for c in rc.candidates_considered
        if c.get("rejected") == "at_or_beyond_wing"
    ]
    okc = (
        rc.success
        and rc.strike is not None
        and float(rc.strike) < 76000.0
        and 76000.0 in call_rej
        and 77000.0 in call_rej
    )
    emit(f"  chosen={rc.strike} rejects={sorted(call_rej)}")
    emit(f"  RESULT: {'PASS' if okc else 'FAIL'}")
    if not okc:
        failed += 1

    emit("")
    emit("=" * 60)
    emit(f"SUMMARY: failed={failed}")
    emit("ALL PASS" if failed == 0 else "SOME FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
