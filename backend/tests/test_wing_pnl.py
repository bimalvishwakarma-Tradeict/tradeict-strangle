# test_wing_pnl.py — Wings P&L, deductions, net-credit target/SL, decay net basis
#
# Run: python -m pytest backend/tests/test_wing_pnl.py -q

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.basket_legs import (
    classify_legs,
    compute_net_credit_usd,
)
from backend.core.delta_client import compute_signed_upnl
from backend.core.fees import (
    abs_execution_cost_usd,
    basket_fees_paid_from_legs,
    compute_entry_spread_usd,
    compute_net_mtm,
    estimate_expected_exit_spread_usd,
)
from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy
from backend.strategies.s001_short_strangle.premium_decay import (
    evaluate_premium_decay_exit,
)

CV = float(OPTIONS_CONTRACT_VALUE)


def _leg(
    *,
    leg_type: str,
    entry: float,
    qty: int = 1,
    status: str = "open",
    is_long: bool = False,
    entry_fee: float = 0.0,
    exit_fee: float = 0.0,
):
    return SimpleNamespace(
        id=hash(leg_type) % 10000,
        leg_type=leg_type,
        initial_premium=entry,
        quantity=qty,
        status=status,
        is_long=is_long,
        entry_fee_usd=entry_fee,
        exit_fee_usd=exit_fee,
        entry_spread_usd=0.0,
        symbol=f"{leg_type}-SYM",
        product_id=1000 + (hash(leg_type) % 100),
    )


def test_four_leg_pnl_short_vs_wing_signs() -> None:
    """1. Shorts (entry−mark); wings (mark−entry) via compute_signed_upnl."""
    sc = _leg(leg_type="call", entry=357.0, is_long=False)
    sp = _leg(leg_type="put", entry=285.0, is_long=False)
    wc = _leg(leg_type="wing_call", entry=157.0, is_long=True)
    wp = _leg(leg_type="wing_put", entry=96.0, is_long=True)
    # marks: shorts decay, wings grow a bit
    call_mark, put_mark = 300.0, 250.0
    wing_call_mark, wing_put_mark = 170.0, 100.0

    short_call_upnl = compute_signed_upnl(357.0, call_mark, size=-1, contract_value=CV)
    short_put_upnl = compute_signed_upnl(285.0, put_mark, size=-1, contract_value=CV)
    wing_call_upnl = compute_signed_upnl(157.0, wing_call_mark, size=+1, contract_value=CV)
    wing_put_upnl = compute_signed_upnl(96.0, wing_put_mark, size=+1, contract_value=CV)

    assert short_call_upnl == pytest_approx((357.0 - 300.0) * CV)
    assert short_put_upnl == pytest_approx((285.0 - 250.0) * CV)
    assert wing_call_upnl == pytest_approx((170.0 - 157.0) * CV)
    assert wing_put_upnl == pytest_approx((100.0 - 96.0) * CV)

    trade = SimpleNamespace(id=1)
    strat = ShortStrangleStrategy()
    total = strat.calculate_pnl(
        trade,
        sc,
        sp,
        call_mark,
        put_mark,
        realized_pnl=0.0,
        wing_call_leg=wc,
        wing_put_leg=wp,
        wing_call_premium=wing_call_mark,
        wing_put_premium=wing_put_mark,
    )
    expected = short_call_upnl + short_put_upnl + wing_call_upnl + wing_put_upnl
    assert total == pytest_approx(expected)


def test_demo_path_wing_sign() -> None:
    """2. DEMO path: wing = (px − entry) × qty × CV (most critical)."""
    entry, px, qty = 157.0, 180.0, 2
    demo_wing = (px - entry) * qty * CV
    demo_short = (entry - px) * qty * CV  # would be wrong for wing
    assert demo_wing > 0
    assert demo_wing == pytest_approx(-demo_short)
    # Strategy helper matches DEMO long formula
    upnl = compute_signed_upnl(entry, px, size=+qty, contract_value=CV)
    assert upnl == pytest_approx(demo_wing)


def test_spread_always_subtracted_for_wings() -> None:
    """3. Spread on wings is cost (abs), never credit — entry and exit."""
    # BUY: fill above sent → cost
    adverse = compute_entry_spread_usd(
        sent_price=100.0, fill_price=110.0, quantity=1, is_long=True
    )
    assert adverse == pytest_approx(10.0 * CV)
    assert adverse == abs_execution_cost_usd(adverse)

    # BUY: fill below sent (lucky) — still stored as ≥0 cost, never negative credit
    lucky = compute_entry_spread_usd(
        sent_price=100.0, fill_price=90.0, quantity=1, is_long=True
    )
    assert lucky >= 0.0
    assert lucky == pytest_approx(10.0 * CV)

    exit_spread = estimate_expected_exit_spread_usd(offer_price=150.0, quantity=1)
    assert exit_spread > 0
    fields = compute_net_mtm(
        gross_mtm=1.0,
        fees_paid=0.0,
        est_exit_fees=0.0,
        slippage_pct=0.0,
        expected_exit_spread_usd=exit_spread,
    )
    assert fields["net_mtm"] == pytest_approx(round(1.0 - exit_spread, 4))


def test_slippage_on_all_four_legs_via_gross() -> None:
    """4. Slippage applies to full gross MTM (all four legs already in gross)."""
    sc = compute_signed_upnl(357.0, 300.0, size=-1, contract_value=CV)
    sp = compute_signed_upnl(285.0, 250.0, size=-1, contract_value=CV)
    wc = compute_signed_upnl(157.0, 170.0, size=+1, contract_value=CV)
    wp = compute_signed_upnl(96.0, 100.0, size=+1, contract_value=CV)
    gross = sc + sp + wc + wp
    fields = compute_net_mtm(
        gross_mtm=gross,
        fees_paid=0.0,
        est_exit_fees=0.0,
        slippage_pct=2.0,
        expected_exit_spread_usd=0.0,
    )
    assert fields["slippage_amount"] == pytest_approx(round(abs(gross) * 0.02, 4))


def test_fees_sum_all_four_legs() -> None:
    """5. Fees from all four legs."""
    legs = [
        _leg(leg_type="call", entry=357.0, entry_fee=0.01, exit_fee=0.02),
        _leg(leg_type="put", entry=285.0, entry_fee=0.01, exit_fee=0.02),
        _leg(
            leg_type="wing_call",
            entry=157.0,
            is_long=True,
            entry_fee=0.015,
            exit_fee=0.015,
        ),
        _leg(
            leg_type="wing_put",
            entry=96.0,
            is_long=True,
            entry_fee=0.015,
            exit_fee=0.015,
        ),
    ]
    total = basket_fees_paid_from_legs(legs)
    assert total == pytest_approx(0.12)


def test_initial_max_profit_net_credit() -> None:
    """6. initial_max_profit = 642 − 253 = 389 pts → $0.389/lot."""
    net = compute_net_credit_usd(
        short_call_premium=357.0,
        short_put_premium=285.0,
        short_qty=1,
        wing_call_premium=157.0,
        wing_put_premium=96.0,
        wing_qty=1,
    )
    assert net == pytest_approx(389.0 * CV)
    assert net == pytest_approx(0.389)


def test_tp_pct_on_net_not_gross() -> None:
    """7. tp_pct 10% → 0.0389, NOT gross 0.0642."""
    net = compute_net_credit_usd(
        short_call_premium=357.0,
        short_put_premium=285.0,
        short_qty=1,
        wing_call_premium=157.0,
        wing_put_premium=96.0,
    )
    gross = (357.0 + 285.0) * CV
    target_net = net * 0.10
    target_gross_wrong = gross * 0.10
    assert target_net == pytest_approx(0.0389)
    assert target_gross_wrong == pytest_approx(0.0642)
    assert abs(target_net - target_gross_wrong) > 0.01


def test_gross_mtm_for_stoploss_includes_wings() -> None:
    """8. SL gross includes wing P&L (+ entry spread add-back)."""
    sc = compute_signed_upnl(357.0, 400.0, size=-1, contract_value=CV)  # loss
    sp = compute_signed_upnl(285.0, 250.0, size=-1, contract_value=CV)
    wc = compute_signed_upnl(157.0, 200.0, size=+1, contract_value=CV)  # wing win
    wp = compute_signed_upnl(96.0, 80.0, size=+1, contract_value=CV)
    total_pnl = sc + sp + wc + wp
    entry_spread_for_sl = 0.01
    gross_mtm_for_sl = total_pnl + entry_spread_for_sl
    # Without wings would miss wc+wp
    shorts_only = sc + sp
    assert gross_mtm_for_sl != pytest_approx(shorts_only + entry_spread_for_sl)
    assert gross_mtm_for_sl == pytest_approx(total_pnl + entry_spread_for_sl)


def test_decay_exit_combined_net_ratio() -> None:
    """9. Decay combined fires on net remaining_pct."""
    call = _leg(leg_type="call", entry=357.0)
    put = _leg(leg_type="put", entry=285.0)
    wc = _leg(leg_type="wing_call", entry=157.0, is_long=True)
    wp = _leg(leg_type="wing_put", entry=96.0, is_long=True)
    # entry_net = 642 - 253 = 389
    # current: shorts half, wings same → current_net = 321 - 253 = 68
    # remaining = 68/389 ≈ 17.5% → exit at 50%
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=178.5,
        put_premium=142.5,
        enabled=True,
        decay_pct=50.0,
        mode="combined",
        wing_call_leg=wc,
        wing_put_leg=wp,
        wing_call_premium=157.0,
        wing_put_premium=96.0,
    )
    assert detail["entry_net"] == pytest_approx(389.0)
    assert detail["current_net"] == pytest_approx(68.0)
    assert should_exit is True


def test_decay_exit_negative_current_net_fires() -> None:
    """10. current_net negative → exit (large profit) — no guard."""
    call = _leg(leg_type="call", entry=357.0)
    put = _leg(leg_type="put", entry=285.0)
    wc = _leg(leg_type="wing_call", entry=157.0, is_long=True)
    wp = _leg(leg_type="wing_put", entry=96.0, is_long=True)
    # shorts nearly worthless, wings expensive → current_net negative
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=10.0,
        put_premium=10.0,
        enabled=True,
        decay_pct=50.0,
        mode="combined",
        wing_call_leg=wc,
        wing_put_leg=wp,
        wing_call_premium=300.0,
        wing_put_premium=200.0,
    )
    assert detail["current_net"] < 0
    assert should_exit is True


def test_wings_disabled_regression_numbers() -> None:
    """11. Wings off → classic short-only numbers unchanged."""
    net_no_wings = compute_net_credit_usd(
        short_call_premium=357.0,
        short_put_premium=285.0,
        short_qty=1,
    )
    assert net_no_wings == pytest_approx(642.0 * CV)

    call = _leg(leg_type="call", entry=200.0)
    put = _leg(leg_type="put", entry=200.0)
    should_exit, detail = evaluate_premium_decay_exit(
        call_leg=call,
        put_leg=put,
        call_premium=90.0,
        put_premium=90.0,
        enabled=True,
        decay_pct=50.0,
        mode="combined",
    )
    assert should_exit is True
    assert detail["wings_active"] is False
    assert detail["combined_remaining_pct"] == pytest_approx(45.0)

    trade = SimpleNamespace(id=1)
    strat = ShortStrangleStrategy()
    sc = _leg(leg_type="call", entry=150.0)
    sp = _leg(leg_type="put", entry=150.0)
    pnl = strat.calculate_pnl(trade, sc, sp, 100.0, 80.0)
    expected = compute_signed_upnl(150.0, 100.0, size=-1, contract_value=CV)
    expected += compute_signed_upnl(150.0, 80.0, size=-1, contract_value=CV)
    assert pnl == pytest_approx(expected)

    bl = classify_legs([sc, sp])
    assert bl.wings_enabled() is False
    assert len(bl.short_legs()) == 2


def pytest_approx(val: float, rel: float = 1e-6, abs_: float = 1e-9):
    import pytest

    return pytest.approx(val, rel=rel, abs=abs_)
