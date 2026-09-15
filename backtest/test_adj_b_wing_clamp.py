#!/usr/bin/env python3
"""
Adj B wing clamp unit checks (selection filter + open/qty guard + forced exit).

No print(). Output: console via sys.stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    AdjBNoStrikeInsideWing,
    is_adj_b_no_strike_inside_wing,
    resolve_adj_b_wing_strike,
    select_adj_b_strike,
)
from backend.strategies.base_strategy import AdjustmentResult  # noqa: E402
from backend.engine.wing_exit import clamp_short_strike_inside_wing  # noqa: E402


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def put_chain() -> list[dict]:
    """Put marks: farther OTM (lower K) = cheaper; wing at 73200."""
    rows = []
    specs = [
        (71000, 40.0),
        (72000, 55.0),
        (73200, 80.0),
        (74000, 95.0),
        (74500, 110.0),
        (75000, 130.0),
        (76000, 200.0),
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
        (78000, 40.0),
        (77000, 55.0),
        (76000, 80.0),
        (75500, 95.0),
        (75000, 110.0),
        (74000, 200.0),
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


def only_beyond_wing_chain() -> list[dict]:
    return [
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
        {
            "option_type": "put",
            "strike": 75000.0,
            "mark_price": 200.0,
            "product_id": 75000,
            "symbol": "P-BTC-75000-010526",
        },
    ]


def result_to_forced_exit_flags(
    select_result: object, wing_k: float | None
) -> AdjustmentResult:
    """Mirror execute() branching for Adj B plan failure."""
    if is_adj_b_no_strike_inside_wing(select_result, wing_k):
        return AdjustmentResult(
            success=False,
            requires_basket_exit=True,
            close_basket=True,
            exit_reason="ADJ_B_NO_STRIKE_INSIDE_WING",
            error_message="ADJ_B_NO_STRIKE_INSIDE_WING",
        )
    return AdjustmentResult(
        success=False,
        requires_basket_exit=False,
        close_basket=False,
        error_message="ADJ_B_SKIPPED_NO_STRIKE",
    )


def main() -> int:
    emit("ADJ B WING CLAMP TESTS")
    emit("=" * 60)
    failed = 0

    # --- case 1 ---
    emit("")
    emit("CASE 1: put wing=73200 — candidates at/beyond wing rejected")
    r1 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=put_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
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

    # --- case 2 ---
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
    ok2 = r2.success and r2.strike is not None and float(r2.strike) > 73200.0
    emit(f"  chosen={r2.strike} prem={r2.premium}")
    emit(f"  RESULT: {'PASS' if ok2 else 'FAIL'}")
    if not ok2:
        failed += 1

    # --- case 3 ---
    emit("")
    emit("CASE 3: all surviving premiums are beyond wing → abort/no_valid")
    r3 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=only_beyond_wing_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=73200.0,
    )
    ok3 = (not r3.success) and r3.skip_reason == "no_valid_strike"
    emit(f"  success={r3.success} skip_reason={r3.skip_reason}")
    emit(f"  RESULT: {'PASS' if ok3 else 'FAIL'}")
    if not ok3:
        failed += 1

    # --- case 4 ---
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
    considered_732 = [
        c for c in r4.candidates_considered if abs(float(c["strike"]) - 73200) < 1e-9
    ]
    ok4 = (
        r4.success
        and len(no_wing_rej) == 0
        and bool(considered_732)
        and considered_732[0].get("rejected") is None
    )
    emit(f"  chosen={r4.strike} wing_rej_count={len(no_wing_rej)}")
    emit(f"  RESULT: {'PASS' if ok4 else 'FAIL'}")
    if not ok4:
        failed += 1

    # --- case 5 ---
    emit("")
    emit("CASE 5: Adj A clamp_short_strike_inside_wing unchanged")
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
    ok5b = status_d == "dead_end" and dead is None
    ok5 = ok5a and ok5b
    emit(f"  call clamp status={status} clamped={clamped}")
    emit(f"  put dead_end status={status_d} clamped={dead}")
    emit(f"  RESULT: {'PASS' if ok5 else 'FAIL'}")
    if not ok5:
        failed += 1

    # --- case 6: CLOSED wing → no filter ---
    emit("")
    emit("CASE 6: wing CLOSED → resolve returns None (filter off)")
    closed_wing = SimpleNamespace(status="closed", quantity=8, strike=73200.0)
    wk6 = resolve_adj_b_wing_strike(closed_wing)
    r6 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=put_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=wk6,
    )
    rej6 = [
        c for c in r6.candidates_considered if c.get("rejected") == "at_or_beyond_wing"
    ]
    ok6 = wk6 is None and len(rej6) == 0 and r6.success
    emit(f"  resolve={wk6} wing_rej={len(rej6)} chosen={r6.strike}")
    emit(f"  RESULT: {'PASS' if ok6 else 'FAIL'}")
    if not ok6:
        failed += 1

    # --- case 7: OPEN qty=0 → no filter ---
    emit("")
    emit("CASE 7: wing OPEN qty=0 → resolve returns None (filter off)")
    zero_wing = SimpleNamespace(status="open", quantity=0, strike=73200.0)
    wk7 = resolve_adj_b_wing_strike(zero_wing)
    r7 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=put_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=wk7,
    )
    rej7 = [
        c for c in r7.candidates_considered if c.get("rejected") == "at_or_beyond_wing"
    ]
    ok7 = wk7 is None and len(rej7) == 0 and r7.success
    emit(f"  resolve={wk7} wing_rej={len(rej7)} chosen={r7.strike}")
    emit(f"  RESULT: {'PASS' if ok7 else 'FAIL'}")
    if not ok7:
        failed += 1

    # --- case 8: all beyond wing → basket exit flags ---
    emit("")
    emit("CASE 8: all candidates beyond wing → forced basket EXIT (not skip)")
    open_wing = SimpleNamespace(status="open", quantity=8, strike=73200.0)
    wk8 = resolve_adj_b_wing_strike(open_wing)
    r8 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=only_beyond_wing_chain(),
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=wk8,
    )
    force = is_adj_b_no_strike_inside_wing(r8, wk8)
    flags = result_to_forced_exit_flags(r8, wk8)
    # Exception path used by live execute
    raised = False
    try:
        if force:
            raise AdjBNoStrikeInsideWing(
                {
                    "leg": "put",
                    "wing_strike": wk8,
                    "p_target": 150.0,
                    "n_candidates": len(r8.candidates_considered),
                    "reject_reasons": r8.candidates_considered,
                    "reason": "no_strike_inside_wing_selection",
                }
            )
    except AdjBNoStrikeInsideWing:
        raised = True
    ok8 = (
        wk8 == 73200.0
        and (not r8.success)
        and force
        and flags.requires_basket_exit is True
        and flags.close_basket is True
        and flags.error_message == "ADJ_B_NO_STRIKE_INSIDE_WING"
        and raised
    )
    emit(
        f"  force={force} exit={flags.requires_basket_exit} "
        f"close={flags.close_basket} msg={flags.error_message} raised={raised}"
    )
    emit(f"  RESULT: {'PASS' if ok8 else 'FAIL'}")
    if not ok8:
        failed += 1

    # --- case 9: empty chain / no wing → skip, NOT exit ---
    emit("")
    emit("CASE 9: empty chain → skip behaviour (no forced exit)")
    r9 = select_adj_b_strike(
        leg_type="put",
        p_target=150.0,
        chain=[],
        spot=75000.0,
        other_short_strike=78000.0,
        wing_strike=None,
    )
    force9 = is_adj_b_no_strike_inside_wing(r9, None)
    flags9 = result_to_forced_exit_flags(r9, None)
    ok9 = (
        (not r9.success)
        and (not force9)
        and flags9.requires_basket_exit is False
        and flags9.close_basket is False
        and flags9.error_message == "ADJ_B_SKIPPED_NO_STRIKE"
    )
    emit(
        f"  success={r9.success} force={force9} "
        f"exit={flags9.requires_basket_exit} msg={flags9.error_message}"
    )
    emit(f"  RESULT: {'PASS' if ok9 else 'FAIL'}")
    if not ok9:
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

    # Bonus: OPEN qty>0 resolves strike
    emit("")
    emit("BONUS: open qty>0 wing resolves strike")
    wk_ok = resolve_adj_b_wing_strike(
        SimpleNamespace(status="open", quantity=6, strike=73200.0)
    )
    okb = wk_ok == 73200.0
    emit(f"  resolve={wk_ok}")
    emit(f"  RESULT: {'PASS' if okb else 'FAIL'}")
    if not okb:
        failed += 1

    emit("")
    emit("=" * 60)
    emit(f"SUMMARY: failed={failed}")
    emit("ALL PASS" if failed == 0 else "SOME FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
