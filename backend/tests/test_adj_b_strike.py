# test_adj_b_strike.py — Adj B worked examples from the v2 spec
#
# Run: python -m pytest backend/tests/test_adj_b_strike.py -q

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.strategies.s001_short_strangle.adj_b import select_adj_b_strike


def _call_row(strike: float, premium: float) -> dict:
    return {
        "option_type": "call",
        "strike": strike,
        "mark_price": premium,
        "product_id": int(strike),
        "symbol": f"C-BTC-{int(strike)}",
    }


def test_adj_b_move1_sell_80000c() -> None:
    """
    Move 1 — spot ~80k, put short 75000, P_target 580 (tested put).
    81000C 420 | 80000C 540 | 79000C 610
    → 540 is highest below 580 and 80000 > 75000 → sell 80000C
    """
    chain = [
        _call_row(81000, 420),
        _call_row(80000, 540),
        _call_row(79000, 610),
        _call_row(78000, 700),  # ITM-ish vs ATM 80k — still listed
    ]
    result = select_adj_b_strike(
        leg_type="call",
        p_target=580.0,
        chain=chain,
        spot=80_000.0,
        other_short_strike=75_000.0,
        min_short_gap_points=0.0,
    )
    assert result.success is True
    assert result.strike == 80_000.0
    assert result.premium == 540.0


def test_adj_b_move2_step_out_to_76000c() -> None:
    """
    Move 2 — spot has moved down (~75.5k); put short still 75000; P_target 850.
    76000C 750 | 75000C 790 | 74000C 840
    840 @ 74000 below put → rejected
    790 @ 75000 equals put (straddle) → rejected
    step out → 76000C @ 750 → sell 76000C
    """
    chain = [
        _call_row(76000, 750),
        _call_row(75000, 790),
        _call_row(74000, 840),
        _call_row(77000, 700),
    ]
    result = select_adj_b_strike(
        leg_type="call",
        p_target=850.0,
        chain=chain,
        spot=75_500.0,
        other_short_strike=75_000.0,
        min_short_gap_points=0.0,
    )
    assert result.success is True
    assert result.strike == 76_000.0
    assert result.premium == 750.0
    rejected = {
        round(float(c["strike"])): c.get("rejected")
        for c in result.candidates_considered
    }
    # 74000 / 75000 must not win — ITM and/or gap/straddle vs the 75000 put
    assert rejected.get(74000) in ("itm", "at_or_across_other_short")
    assert rejected.get(75000) in ("itm", "at_or_across_other_short")
    assert rejected.get(76000) is None
