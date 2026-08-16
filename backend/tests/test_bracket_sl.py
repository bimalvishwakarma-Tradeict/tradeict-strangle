# test_bracket_sl.py — Canonical bracket SL from master fill
#
# Run: python backend/tests/test_bracket_sl.py
# (from trading-bot/ root)

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.delta_sl import compute_bracket_sl


def test_trade64_call_fill_based() -> None:
    """Trade#64: fill 9.00 × 220% → 19.80 (not quote 10.35 → 22.76)."""
    stop, limit = compute_bracket_sl(
        9.0,
        220.0,
        master_mark=10.35,
        leg="call",
        trade_id=64,
    )
    assert round(stop, 2) == 19.80
    assert round(limit, 2) == round(19.80 * 1.05, 2)


def test_trade64_put_fill_based() -> None:
    """Trade#64: fill 11.00 × 220% → 24.20 (not quote 12.00 → 26.40)."""
    stop, limit = compute_bracket_sl(
        11.0,
        220.0,
        master_mark=12.0,
        leg="put",
        trade_id=64,
    )
    assert round(stop, 2) == 24.20
    assert round(limit, 2) == round(24.20 * 1.05, 2)


def test_anomaly_falls_back_to_mark() -> None:
    """|fill − mark| / mark > 35% → use mark × uni_sl."""
    stop, _limit = compute_bracket_sl(
        1.0,
        220.0,
        master_mark=10.0,
        leg="call",
        trade_id=1,
    )
    assert round(stop, 2) == 22.0  # 10.0 × 2.20


if __name__ == "__main__":
    test_trade64_call_fill_based()
    test_trade64_put_fill_based()
    test_anomaly_falls_back_to_mark()
    print("ALL PASSED")
