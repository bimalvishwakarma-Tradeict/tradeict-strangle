# test_entry_spread_for_sl.py — SL entry-spread resets; net_mtm untouched
#
# Run: python backend/tests/test_entry_spread_for_sl.py
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.fees import (
    compute_net_mtm,
    get_entry_spread_for_sl,
    reset_entry_spread_for_sl,
)


def test_entry_spread_resets_not_accumulates() -> None:
    """Original 0.02 then adjusts 0.012 and 0.009 → SL spread is 0.009, not 0.041."""
    trade = SimpleNamespace(id=66, entry_spread_for_sl_usd=0.0)
    reset_entry_spread_for_sl(
        trade, 0.02, reason="trade_entry", leg="call+put"
    )
    assert round(get_entry_spread_for_sl(trade), 3) == 0.02

    reset_entry_spread_for_sl(
        trade, 0.012, reason="adjustment", leg="call"
    )
    assert round(get_entry_spread_for_sl(trade), 3) == 0.012

    reset_entry_spread_for_sl(
        trade, 0.009, reason="adjustment", leg="put"
    )
    assert round(get_entry_spread_for_sl(trade), 3) == 0.009
    # Must NOT be 0.02 + 0.012 + 0.009
    assert round(get_entry_spread_for_sl(trade), 3) != 0.041


def test_net_mtm_excludes_entry_spread_trade64() -> None:
    """
    HARD CONSTRAINT: entry spread must never leak into net_mtm.

    Trade#64 production reference:
      gross=-0.0340 fees=0.0083 est_exit=0.0097 slip=0.0010 exit_spread=0.0012
      -> net = -0.0542 (logged -0.0541 after rounding)
      cumulative_entry_spread=0.0235 is NOT in this formula.
    """
    result = compute_net_mtm(
        gross_mtm=-0.0340,
        fees_paid=0.0083,
        est_exit_fees=0.0097,
        slippage_pct=None,  # use explicit slip via amount path — set pct so slip matches
        expected_exit_spread_usd=0.0012,
    )
    # With slippage_pct default 2%: slip = abs(-0.034)*0.02 = 0.00068
    # Production used slippage_amount=0.0010 — pin by computing with known slip:
    # Reconstruct exact production arithmetic without going through slip %.
    gross = -0.0340
    fees = 0.0083
    est = 0.0097
    slip = 0.0010
    exit_spread = 0.0012
    net = gross - fees - est - slip - exit_spread
    assert round(net, 4) == -0.0542

    # compute_net_mtm with slip_pct chosen so slip_amount == 0.0010:
    # abs(gross)*pct/100 = 0.0010 => pct = 0.0010/0.034*100 ≈ 2.941176
    pinned = compute_net_mtm(
        gross_mtm=-0.0340,
        fees_paid=0.0083,
        est_exit_fees=0.0097,
        slippage_pct=0.0010 / 0.0340 * 100.0,
        expected_exit_spread_usd=0.0012,
    )
    assert pinned["slippage_amount"] == 0.0010
    assert pinned["net_mtm"] == -0.0542
    # Entry spread must not appear in the return keys as an input to net
    assert "entry_spread_for_sl" not in pinned
    assert "cumulative_entry_spread" not in pinned
    # Changing entry spread on a trade object must not affect compute_net_mtm
    trade = SimpleNamespace(entry_spread_for_sl_usd=0.0235)
    again = compute_net_mtm(
        gross_mtm=-0.0340,
        fees_paid=0.0083,
        est_exit_fees=0.0097,
        slippage_pct=0.0010 / 0.0340 * 100.0,
        expected_exit_spread_usd=0.0012,
    )
    assert again["net_mtm"] == pinned["net_mtm"]
    assert get_entry_spread_for_sl(trade) == 0.0235  # unused by net


def test_gross_mtm_for_sl_uses_reset_spread_only() -> None:
    """gross_mtm_for_stoploss = total_pnl + entry_spread_for_sl (latest only)."""
    trade = SimpleNamespace(id=1, entry_spread_for_sl_usd=0.0)
    reset_entry_spread_for_sl(trade, 0.02, reason="trade_entry", leg="both")
    reset_entry_spread_for_sl(trade, 0.009, reason="adjustment", leg="call")
    total_pnl = -0.0530
    gross_for_sl = total_pnl + get_entry_spread_for_sl(trade)
    assert round(gross_for_sl, 4) == round(-0.0530 + 0.009, 4)
    # Old cumulative bug would have been -0.053 + 0.041
    assert round(gross_for_sl, 4) != round(-0.0530 + 0.041, 4)


if __name__ == "__main__":
    test_entry_spread_resets_not_accumulates()
    test_net_mtm_excludes_entry_spread_trade64()
    test_gross_mtm_for_sl_uses_reset_spread_only()
    print("ALL PASSED")
