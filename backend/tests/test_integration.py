# test_integration.py — End-to-end integration test suite for the full system
"""
Run with (from trading-bot/):
  python backend/tests/test_integration.py
  python backend/tests/test_integration.py unit   # tests 4–7 only
  python backend/tests/test_integration.py api    # tests 1–3 only

Requires: .env with DELTA_API_KEY + DELTA_API_SECRET (for api / all modes)
           APP_SECRET_KEY (for encryption test)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")


def _print_summary(results: list[tuple[str, bool]]) -> None:
    print("\n" + "=" * 50)
    print("INTEGRATION TEST SUMMARY")
    print("=" * 50)
    for i, (name, passed) in enumerate(results):
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"Test {i + 1}: {name} — {status}")
    passed_count = sum(1 for _, p in results if p)
    print(f"\nTotal: {passed_count}/{len(results)} passed")
    if results and all(p for _, p in results):
        print("\n🎉 ALL TESTS PASSED — Bot is ready!")
    else:
        print("\n⚠️  Some tests failed — check above for details")


def _make_db_with_slabs(slabs: dict[str, float] | None = None) -> MagicMock:
    """Mock DB session so get_slabs() returns defaults (or provided values)."""
    db = MagicMock()
    if slabs is None:
        db.query.return_value.filter.return_value.all.return_value = []
        return db
    rows = []
    for key, value in slabs.items():
        row = MagicMock()
        row.key = key
        row.value = str(value)
        rows.append(row)
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


async def test_1_api_connection() -> None:
    print("\n🧪 Test 1: Delta Exchange Connection")
    from backend.core.delta_client import DeltaClient

    api_key = os.getenv("DELTA_API_KEY", "").strip()
    api_secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET missing in .env")

    client = DeltaClient(api_key, api_secret)
    try:
        profile = await client.test_connection()
        assert profile.get("account_name"), f"account_name missing: {profile}"
        wallet = await client.get_wallet_balance()
        balance = float(wallet.get("balance_usdt", 0))
        assert balance > 0, f"Expected positive balance, got {balance}"
        print(f"Account: {profile['account_name']} | Balance: ${balance:.2f}")
    finally:
        await client.close()


async def test_2_option_chain() -> None:
    print("\n🧪 Test 2: Option Chain Fetch")
    from backend.core.delta_client import DeltaClient

    api_key = os.getenv("DELTA_API_KEY", "").strip()
    api_secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET missing in .env")

    client = DeltaClient(api_key, api_secret)
    try:
        expiries = await client.get_available_expiries("BTC")
        assert expiries, "Expected non-empty expiries list"
        first = expiries[0]
        first_date = date.fromisoformat(str(first["date"]))
        assert first_date >= date.today(), f"First expiry {first_date} < today"

        chain = await client.get_option_chain("BTC", first_date.isoformat())
        assert len(chain) > 0, "Option chain empty"
        for row in chain:
            assert "strike" in row, f"Missing strike: {row}"
            assert "call_mark_price" in row, f"Missing call_mark_price: {row}"
            assert "put_mark_price" in row, f"Missing put_mark_price: {row}"
            assert float(row["call_mark_price"]) > 0 or float(row["put_mark_price"]) > 0
            assert float(row["put_delta"]) >= 0, "put_delta must be stored as positive"

        print(f"Expiry: {first_date} ({first.get('label')}) | rows: {len(chain)}")
        print("First 3 rows:")
        for row in chain[:3]:
            print(
                f"  strike={row['strike']} "
                f"call_mark={row['call_mark_price']} "
                f"put_mark={row['put_mark_price']}"
            )

        # Live-price integrity: chain marks must match /v2/tickers/{symbol}
        mid = chain[len(chain) // 2 : len(chain) // 2 + 3]
        print("Live ticker cross-check (middle rows):")
        for row in mid:
            call_live = await client.get_mark_price(row["call_symbol"])
            put_live = await client.get_mark_price(row["put_symbol"])
            print(
                f"  strike={row['strike']} "
                f"call chain={row['call_mark_price']:.2f} live={call_live:.2f} | "
                f"put chain={row['put_mark_price']:.2f} live={put_live:.2f}"
            )
            assert abs(float(row["call_mark_price"]) - call_live) < max(2.0, call_live * 0.05)
            assert abs(float(row["put_mark_price"]) - put_live) < max(2.0, put_live * 0.05)
            assert float(row["call_bid"]) > 0 or float(row["call_ask"]) > 0
            assert float(row["put_bid"]) > 0 or float(row["put_ask"]) > 0
    finally:
        await client.close()


async def test_3_find_strike_by_premium() -> None:
    print("\n🧪 Test 3: Find Strike by Premium")
    from backend.core.delta_client import DeltaClient

    api_key = os.getenv("DELTA_API_KEY", "").strip()
    api_secret = os.getenv("DELTA_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("DELTA_API_KEY / DELTA_API_SECRET missing in .env")

    client = DeltaClient(api_key, api_secret)
    try:
        expiries = await client.get_available_expiries("BTC")
        assert expiries, "No expiries available"
        expiry = str(expiries[0]["date"])

        call_row = await client.find_strike_by_premium("BTC", expiry, "call", 150.0)
        assert call_row.get("strike") is not None
        assert call_row.get("call_product_id") or call_row.get("product_id")
        assert call_row.get("call_symbol") or call_row.get("symbol")
        call_mark = float(call_row.get("call_mark_price") or call_row.get("mark_price") or 0)
        assert abs(call_mark - 150.0) < 100, f"Call mark {call_mark} too far from 150"
        print(
            f"Found call strike: ${call_row['strike']} @ ${call_mark:.2f} "
            f"({call_row.get('call_symbol')})"
        )

        put_row = await client.find_strike_by_premium("BTC", expiry, "put", 150.0)
        assert put_row.get("strike") is not None
        assert put_row.get("put_product_id") or put_row.get("product_id")
        assert put_row.get("put_symbol") or put_row.get("symbol")
        put_mark = float(put_row.get("put_mark_price") or put_row.get("mark_price") or 0)
        assert abs(put_mark - 150.0) < 100, f"Put mark {put_mark} too far from 150"
        print(
            f"Found put strike: ${put_row['strike']} @ ${put_mark:.2f} "
            f"({put_row.get('put_symbol')})"
        )
    finally:
        await client.close()


async def test_4_pnl_calculation() -> None:
    print("\n🧪 Test 4: P&L Calculation (Unit)")
    from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy

    strategy = ShortStrangleStrategy()
    trade = SimpleNamespace(id=1)

    # Case A: (150-100)*1 + (150-80)*1 = 120
    call_a = SimpleNamespace(initial_premium=150.0, quantity=1)
    put_a = SimpleNamespace(initial_premium=150.0, quantity=1)
    pnl_a = strategy.calculate_pnl(trade, call_a, put_a, 100.0, 80.0)
    assert pnl_a == 120.0, f"Case A expected 120, got {pnl_a}"
    print(f"PnL Test A: {pnl_a} ✅")

    # Case B: (150-200)*1 + (150-100)*1 = 0
    call_b = SimpleNamespace(initial_premium=150.0, quantity=1)
    put_b = SimpleNamespace(initial_premium=150.0, quantity=1)
    pnl_b = strategy.calculate_pnl(trade, call_b, put_b, 200.0, 100.0)
    assert pnl_b == 0.0, f"Case B expected 0, got {pnl_b}"
    print(f"PnL Test B: {pnl_b} ✅")

    # Case C: (150-300)*2 + (150-50)*2 = -100
    call_c = SimpleNamespace(initial_premium=150.0, quantity=2)
    put_c = SimpleNamespace(initial_premium=150.0, quantity=2)
    pnl_c = strategy.calculate_pnl(trade, call_c, put_c, 300.0, 50.0)
    assert pnl_c == -100.0, f"Case C expected -100, got {pnl_c}"
    print(f"PnL Test C: {pnl_c} ✅")


async def test_5_adjustment_trigger_logic() -> None:
    print("\n🧪 Test 5: Adjustment Trigger Logic (Unit)")
    from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy

    strategy = ShortStrangleStrategy()
    # Use +3 days so hours_left > 24 → slab_24h=200 (tomorrow may be <24h)
    expiry = date.today() + timedelta(days=3)
    trade = SimpleNamespace(
        id=1,
        profit_target_usd=200.0,
        stoploss_usd=300.0,
        expiry_date=expiry,
        underlying="BTC",
    )
    call_leg = SimpleNamespace(initial_premium=150.0, quantity=1)
    put_leg = SimpleNamespace(initial_premium=150.0, quantity=1)
    db = _make_db_with_slabs(
        {
            "slab_24h": 200.0,
            "slab_12h": 175.0,
            "slab_6h": 150.0,
            "slab_lt6h": 150.0,
        }
    )

    # A: call at 200% of 150 → adjust call (net MTM negative)
    action_a = await strategy.on_tick(trade, call_leg, put_leg, 300.0, 100.0, db)
    assert action_a.should_adjust is True
    assert action_a.adjust_leg == "call"
    assert action_a.triggered_leg == "call"
    print(f"Scenario A: adjust={action_a.adjust_leg} ✅")

    # B: put at 200% → adjust put
    action_b = await strategy.on_tick(trade, call_leg, put_leg, 100.0, 300.0, db)
    assert action_b.should_adjust is True
    assert action_b.adjust_leg == "put"
    print(f"Scenario B: adjust={action_b.adjust_leg} ✅")

    # C: both below trigger → no action
    action_c = await strategy.on_tick(trade, call_leg, put_leg, 100.0, 100.0, db)
    assert action_c.should_adjust is False
    assert action_c.should_exit is False
    print(f"Scenario C: no action (pnl={action_c.current_pnl}) ✅")

    # D: pnl >= profit target → PROFIT_TARGET exit (USD)
    # (150-50)+(150-50)=200 pts × 0.001 = $0.20; target $0.15
    trade.profit_target_usd = 0.15
    action_d = await strategy.on_tick(trade, call_leg, put_leg, 50.0, 50.0, db)
    assert action_d.should_exit is True
    assert action_d.exit_reason == "PROFIT_TARGET"
    print(f"Scenario D: exit={action_d.exit_reason} pnl={action_d.current_pnl} ✅")
    trade.profit_target_usd = 200.0

    # E: trigger hit but Net MTM > 0 → close basket (decision)
    # call entry 100 @ 200 (200%), put entry 250 @ 40 → gross positive
    call_e = SimpleNamespace(initial_premium=100.0, quantity=1, status="open")
    put_e = SimpleNamespace(initial_premium=250.0, quantity=1, status="open")
    trade.profit_target_usd = 10000.0
    trade.stoploss_usd = 10000.0
    trade.slippage_pct = 2.0
    action_e = await strategy.on_tick(trade, call_e, put_e, 200.0, 40.0, db)
    assert action_e.should_exit is True
    assert action_e.exit_reason == "DECISION_PROFIT_AT_TRIGGER"
    assert action_e.triggered_leg == "call"
    assert action_e.current_pnl > 0
    print(
        f"Scenario E: decision close leg={action_e.triggered_leg} "
        f"net_mtm={action_e.current_pnl} ✅"
    )


async def test_6_time_utils() -> None:
    print("\n🧪 Test 6: Time Utils Verification")
    from backend.core.time_utils import (
        get_dte_label,
        get_hours_to_expiry,
        get_ist_now,
        get_trigger_pct,
        is_pre_expiry_window,
    )

    now = get_ist_now()
    tz_name = getattr(now.tzinfo, "zone", None) or str(now.tzinfo)
    assert "Kolkata" in tz_name or "IST" in tz_name, f"Expected IST tz, got {tz_name}"

    tomorrow = date.today() + timedelta(days=1)
    yesterday = date.today() - timedelta(days=1)
    hours_tmr = get_hours_to_expiry(tomorrow)
    assert 0 < hours_tmr < 48, f"hours_to_expiry(tomorrow)={hours_tmr}"
    assert get_hours_to_expiry(yesterday) == 0.0
    assert is_pre_expiry_window(yesterday) is False

    default_slabs = {
        "slab_24h": 200.0,
        "slab_12h": 175.0,
        "slab_6h": 150.0,
        "slab_lt6h": 150.0,
    }
    assert get_trigger_pct(30, default_slabs) == 200.0
    assert get_trigger_pct(18, default_slabs) == 175.0
    assert get_trigger_pct(8, default_slabs) == 150.0
    assert get_trigger_pct(3, default_slabs) == 150.0
    assert get_dte_label(tomorrow) == "1DTE"
    print("All time utils assertions passed")


async def test_7_encryption_roundtrip() -> None:
    print("\n🧪 Test 7: Encryption Round-trip")
    from backend.core.encryption import DecryptionError, decrypt, encrypt

    if not os.getenv("APP_SECRET_KEY", "").strip():
        raise RuntimeError("APP_SECRET_KEY missing in .env")

    test_values = ["api-key-123", "secret-xyz-789", "a" * 50]
    passed = 0
    for value in test_values:
        token = encrypt(value)
        assert decrypt(token) == value
        passed += 1

    raised = False
    try:
        decrypt("invalid-cipher-text-not-a-token")
    except DecryptionError:
        raised = True
    assert raised, "Expected DecryptionError for invalid cipher"

    print(f"Encryption round-trip: {passed}/3 passed")


async def _run_named(
    name: str,
    coro_factory: Any,
    results: list[tuple[str, bool]],
) -> None:
    try:
        await coro_factory()
        results.append((name, True))
    except Exception as exc:
        print(f"❌ {name} FAILED: {exc}")
        results.append((name, False))


API_TESTS: list[tuple[str, Any]] = [
    ("API Connection", test_1_api_connection),
    ("Option Chain Fetch", test_2_option_chain),
    ("Find Strike by Premium", test_3_find_strike_by_premium),
]

UNIT_TESTS: list[tuple[str, Any]] = [
    ("P&L Calculation", test_4_pnl_calculation),
    ("Adjustment Trigger Logic", test_5_adjustment_trigger_logic),
    ("Time Utils Verification", test_6_time_utils),
    ("Encryption Round-trip", test_7_encryption_roundtrip),
]


async def run_tests(mode: str = "all") -> list[tuple[str, bool]]:
    results: list[tuple[str, bool]] = []
    if mode == "unit":
        suite = UNIT_TESTS
    elif mode == "api":
        suite = API_TESTS
    else:
        suite = API_TESTS + UNIT_TESTS

    for name, fn in suite:
        await _run_named(name, fn, results)

    _print_summary(results)
    return results


if __name__ == "__main__":
    mode_arg = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    if mode_arg not in {"all", "unit", "api"}:
        print(f"Unknown mode '{mode_arg}'. Use: all | unit | api")
        sys.exit(2)
    final = asyncio.run(run_tests(mode_arg))
    sys.exit(0 if all(p for _, p in final) else 1)
