#!/usr/bin/env python3
"""
Backfill trade.realized_pnl from sum of closed bot-managed leg realized_pnl.

Does NOT auto-run. Invoke explicitly after deploy:

  cd /path/to/trading-bot
  python deploy/backfill_trade_realized_pnl.py --dry-run
  python deploy/backfill_trade_realized_pnl.py --apply

Optional: --trade-id 121 to limit to one trade.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import TradeStatus
from backend.database import SessionLocal, init_db
from backend.engine.trade_reconcile import recompute_trade_realized_pnl
from backend.models import Leg, Trade

_CLOSED_STATUSES = {
    TradeStatus.CLOSED.value,
    TradeStatus.EMERGENCY_CLOSED.value,
}


def _sum_closed_leg_realized(db, trade_id: int) -> float:
    legs = (
        db.query(Leg)
        .filter(Leg.trade_id == int(trade_id), Leg.is_bot_managed.is_(True))
        .all()
    )
    total = 0.0
    for leg in legs:
        if str(getattr(leg, "status", "") or "").lower() != "closed":
            continue
        rp = getattr(leg, "realized_pnl", None)
        if rp is None:
            continue
        total += float(rp)
    return round(total, 6)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print mismatches only (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write corrected trade.realized_pnl values",
    )
    parser.add_argument("--trade-id", type=int, default=None)
    args = parser.parse_args()
    do_apply = bool(args.apply)
    if do_apply:
        args.dry_run = False

    init_db()

    with SessionLocal() as db:
        q = db.query(Trade).filter(Trade.status.in_(sorted(_CLOSED_STATUSES)))
        if args.trade_id is not None:
            q = q.filter(Trade.id == int(args.trade_id))
        trades = q.order_by(Trade.id.asc()).all()

        mismatches: list[dict[str, object]] = []
        fixed_hedge_ids: set[int] = set()

        for trade in trades:
            tid = int(trade.id)
            stored = (
                round(float(trade.realized_pnl), 6)
                if trade.realized_pnl is not None
                else None
            )
            recomputed = _sum_closed_leg_realized(db, tid)
            if stored is None and recomputed == 0.0:
                continue
            if stored is not None and abs(stored - recomputed) < 1e-6:
                continue
            diff = (
                round(recomputed - (stored or 0.0), 6)
                if stored is not None
                else recomputed
            )
            hid = getattr(trade, "hedge_position_id", None)
            row = {
                "trade_id": tid,
                "stored": stored,
                "recomputed": recomputed,
                "diff": diff,
                "hedge_position_id": int(hid) if hid is not None else None,
                "status": trade.status,
            }
            mismatches.append(row)
            print(
                f"  MISMATCH trade={tid} stored={stored} "
                f"recomputed={recomputed} diff={diff}"
                + (
                    f" hedge={hid}"
                    if hid is not None
                    else ""
                )
            )
            if do_apply:
                recompute_trade_realized_pnl(db, trade)
                if hid is not None:
                    fixed_hedge_ids.add(int(hid))

        if do_apply and mismatches:
            db.commit()
            print(f"\nApplied fixes for {len(mismatches)} trade(s).")
            if fixed_hedge_ids:
                print(
                    "Hedge structures affected (cum_closed_basket_pnl derives "
                    "from these trades):"
                )
                for hid in sorted(fixed_hedge_ids):
                    print(f"  hedge_position_id={hid}")
        elif not mismatches:
            print("No mismatches found.")
        else:
            print(
                f"\nDry-run: {len(mismatches)} mismatch(es). "
                "Re-run with --apply to write."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
