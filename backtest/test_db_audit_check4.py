#!/usr/bin/env python3
"""
db_audit CHECK 4 scoping unit checks (shared with SLAVE_SWEEP recover).

No print(). Output: console via sys.stdout.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.slave_orphan_scope import (  # noqa: E402
    scope_live_positions_for_closed_master_recovery,
)


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def pos(pid: int, size: float, symbol: str = "") -> dict:
    return {
        "product_id": int(pid),
        "size": float(size),
        "product_symbol": symbol or f"OPT-{pid}",
    }


def legacy_recover_filter(
    live: list[dict],
    *,
    master_pids: set[int],
    bot_owned: set[int],
    protected_hedge_pids: set[int],
) -> tuple[bool, list[dict]]:
    """
    Pre-refactor recover loop (bot_owned + hedge) with empty master_pids gate.
    Used only to prove typical single-trade cases still match after helper.
    """
    if not master_pids:
        return True, []
    out: list[dict] = []
    for p in live:
        pid = int(p["product_id"])
        size = float(p["size"])
        if pid <= 0 or abs(size) <= 0:
            continue
        if pid not in bot_owned:
            continue
        if pid in protected_hedge_pids and size > 0:
            continue
        out.append(p)
    return False, out


def check4_would_mark_closed(
    live: list[dict],
    closable: list[dict],
    *,
    empty_master_pids: bool,
) -> bool:
    """
    Mirror CHECK 4 status rule after scoping:
      empty book → closed
      live but no closable → leave status (NOT closed)
      closable present → close path (not asserted here)
    """
    if not live:
        return True
    if empty_master_pids or not closable:
        return False
    return False  # would attempt close; not auto-closed before verify


def main() -> int:
    emit("DB AUDIT CHECK 4 SCOPE TESTS")
    emit("=" * 60)
    failed = 0

    orphan_call = 101
    orphan_put = 102
    other_call = 201
    other_put = 202
    hedge_call = 301
    foreign_pid = 999

    master_pids = {orphan_call, orphan_put}
    bot_owned = {orphan_call, orphan_put, other_call, other_put, hedge_call}
    hedges = {hedge_call}

    # --- case 1 ---
    emit("")
    emit("CASE 1: orphan trade positions → closable")
    live1 = [
        pos(orphan_call, -8, "C-BTC-1"),
        pos(orphan_put, -8, "P-BTC-1"),
    ]
    s1 = scope_live_positions_for_closed_master_recovery(
        live1,
        master_pids=master_pids,
        bot_owned=bot_owned,
        protected_hedge_pids=hedges,
    )
    cids1 = sorted(int(p["product_id"]) for p in s1.closable)
    ok1 = (
        not s1.empty_master_pids
        and cids1 == [orphan_call, orphan_put]
        and len(s1.skipped) == 0
    )
    emit(f"  closable={cids1} skipped={len(s1.skipped)}")
    emit(f"  RESULT: {'PASS' if ok1 else 'FAIL'}")
    if not ok1:
        failed += 1

    # --- case 2 ---
    emit("")
    emit("CASE 2: other trade positions → skip different_trade")
    live2 = [
        pos(other_call, -4, "C-BTC-OTHER"),
        pos(other_put, -4, "P-BTC-OTHER"),
    ]
    s2 = scope_live_positions_for_closed_master_recovery(
        live2,
        master_pids=master_pids,
        bot_owned=bot_owned,
        protected_hedge_pids=hedges,
    )
    reasons2 = {int(sk["product_id"]): sk["reason"] for sk in s2.skipped}
    ok2 = (
        len(s2.closable) == 0
        and reasons2.get(other_call) == "different_trade"
        and reasons2.get(other_put) == "different_trade"
    )
    emit(f"  closable={len(s2.closable)} reasons={reasons2}")
    emit(f"  RESULT: {'PASS' if ok2 else 'FAIL'}")
    if not ok2:
        failed += 1

    # --- case 3 ---
    emit("")
    emit("CASE 3: hedge long → skip hedge")
    live3 = [pos(hedge_call, 6, "C-BTC-HEDGE")]
    s3 = scope_live_positions_for_closed_master_recovery(
        live3,
        master_pids=master_pids | {hedge_call},
        bot_owned=bot_owned,
        protected_hedge_pids=hedges,
    )
    ok3 = (
        len(s3.closable) == 0
        and len(s3.skipped) == 1
        and s3.skipped[0]["reason"] == "hedge"
    )
    emit(f"  closable={len(s3.closable)} reason={s3.skipped[0].get('reason') if s3.skipped else None}")
    emit(f"  RESULT: {'PASS' if ok3 else 'FAIL'}")
    if not ok3:
        failed += 1

    # --- case 4 ---
    emit("")
    emit("CASE 4: foreign (not bot-owned) → skip foreign")
    live4 = [pos(foreign_pid, -2, "C-BTC-FOREIGN")]
    s4 = scope_live_positions_for_closed_master_recovery(
        live4,
        master_pids=master_pids | {foreign_pid},
        bot_owned=bot_owned,
        protected_hedge_pids=hedges,
    )
    ok4 = (
        len(s4.closable) == 0
        and len(s4.skipped) == 1
        and s4.skipped[0]["reason"] == "foreign"
    )
    emit(f"  closable={len(s4.closable)} reason={s4.skipped[0].get('reason') if s4.skipped else None}")
    emit(f"  RESULT: {'PASS' if ok4 else 'FAIL'}")
    if not ok4:
        failed += 1

    # --- case 5 ---
    emit("")
    emit("CASE 5: all filtered out, live remain → status NOT closed")
    live5 = live2 + live3 + live4
    s5 = scope_live_positions_for_closed_master_recovery(
        live5,
        master_pids=master_pids,
        bot_owned=bot_owned,
        protected_hedge_pids=hedges,
    )
    mark_closed = check4_would_mark_closed(
        live5,
        list(s5.closable),
        empty_master_pids=s5.empty_master_pids,
    )
    ok5 = (
        len(live5) > 0
        and len(s5.closable) == 0
        and mark_closed is False
    )
    emit(
        f"  live={len(live5)} closable={len(s5.closable)} "
        f"would_mark_closed={mark_closed}"
    )
    emit(f"  RESULT: {'PASS' if ok5 else 'FAIL'}")
    if not ok5:
        failed += 1

    # --- case 6 ---
    emit("")
    emit("CASE 6: recover scoping — helper matches legacy for this-trade book")
    live6 = [
        pos(orphan_call, -8),
        pos(orphan_put, -8),
        pos(foreign_pid, -1),
        pos(hedge_call, 3),
    ]
    # Single orphan book: bot_owned only this trade + hedge + ignore foreign
    owned6 = {orphan_call, orphan_put, hedge_call}
    empty_legacy, legacy_c = legacy_recover_filter(
        live6,
        master_pids=master_pids,
        bot_owned=owned6,
        protected_hedge_pids=hedges,
    )
    s6 = scope_live_positions_for_closed_master_recovery(
        live6,
        master_pids=master_pids,
        bot_owned=owned6,
        protected_hedge_pids=hedges,
    )
    legacy_ids = sorted(int(p["product_id"]) for p in legacy_c)
    helper_ids = sorted(int(p["product_id"]) for p in s6.closable)
    empty6, _ = legacy_recover_filter(
        live6,
        master_pids=set(),
        bot_owned=owned6,
        protected_hedge_pids=hedges,
    )
    s6_empty = scope_live_positions_for_closed_master_recovery(
        live6,
        master_pids=set(),
        bot_owned=owned6,
        protected_hedge_pids=hedges,
    )
    ok6 = (
        empty_legacy is False
        and legacy_ids == helper_ids == [orphan_call, orphan_put]
        and empty6 is True
        and s6_empty.empty_master_pids is True
        and len(s6_empty.closable) == 0
        and any(sk["reason"] == "foreign" for sk in s6.skipped)
        and any(sk["reason"] == "hedge" for sk in s6.skipped)
    )
    emit(f"  legacy={legacy_ids} helper={helper_ids}")
    emit(
        f"  empty_gate legacy={empty6} helper={s6_empty.empty_master_pids}"
    )
    emit(f"  RESULT: {'PASS' if ok6 else 'FAIL'}")
    if not ok6:
        failed += 1

    # Mixed book bonus: orphan closable + others skipped
    emit("")
    emit("BONUS: mixed book — only orphan pids closable")
    live_m = live1 + live2 + live3 + live4
    sm = scope_live_positions_for_closed_master_recovery(
        live_m,
        master_pids=master_pids,
        bot_owned=bot_owned,
        protected_hedge_pids=hedges,
    )
    okm = sorted(int(p["product_id"]) for p in sm.closable) == [
        orphan_call,
        orphan_put,
    ] and len(sm.skipped) == 4
    emit(f"  closable={[int(p['product_id']) for p in sm.closable]} skipped={len(sm.skipped)}")
    emit(f"  RESULT: {'PASS' if okm else 'FAIL'}")
    if not okm:
        failed += 1

    emit("")
    emit("=" * 60)
    emit(f"SUMMARY: failed={failed}")
    emit("ALL PASS" if failed == 0 else "SOME FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
