#!/usr/bin/env python3
"""
One-off: recompute cum_closed_basket_pnl + structure_pnl for closed hedges.

Uses each hedge's stored hedge_net_mtm and SUM(realized_pnl) of its closed
baskets. Does NOT modify realized_pnl or any leg row.

  cd /path/to/trading-bot
  python deploy/backfill_structure_pnl.py --dry-run
  python deploy/backfill_structure_pnl.py --apply
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
from backend.engine.hedge_lifecycle import _cum_closed_basket_pnl
from backend.models import HedgePosition


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print before/after only (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist recomputed cum_closed / structure_pnl",
    )
    args = parser.parse_args()
    apply = bool(args.apply)

    init_db()
    updated = 0

    print(
        f"{'id':>4}  {'status':<8}  "
        f"{'hedge_net':>12}  "
        f"{'cum_before':>12}  {'cum_after':>12}  "
        f"{'struct_before':>14}  {'struct_after':>14}"
    )
    print("-" * 96)

    with SessionLocal() as db:
        hedges = (
            db.query(HedgePosition)
            .filter(HedgePosition.status == "closed")
            .order_by(HedgePosition.id.asc())
            .all()
        )
        for h in hedges:
            hid = int(h.id)
            hedge_net = float(getattr(h, "hedge_net_mtm", 0.0) or 0.0)
            cum_before = float(getattr(h, "cum_closed_basket_pnl", 0.0) or 0.0)
            struct_before = float(getattr(h, "structure_pnl", 0.0) or 0.0)

            cum_after = float(_cum_closed_basket_pnl(db, hid))
            struct_after = float(hedge_net) + float(cum_after)

            print(
                f"{hid:4d}  {str(h.status):<8}  "
                f"{hedge_net:12.6f}  "
                f"{cum_before:12.6f}  {cum_after:12.6f}  "
                f"{struct_before:14.6f}  {struct_after:14.6f}"
            )

            if apply and (
                abs(cum_before - cum_after) > 1e-9
                or abs(struct_before - struct_after) > 1e-9
            ):
                h.cum_closed_basket_pnl = float(cum_after)
                h.structure_pnl = float(struct_after)
                updated += 1

        if apply:
            db.commit()
            print(f"\nApplied: updated {updated} closed hedge(s).")
        else:
            print(
                f"\nDry-run: {len(hedges)} closed hedge(s). "
                "Re-run with --apply to persist."
            )

    # Sanity: closed hedges must not still have active baskets affecting open
    with SessionLocal() as db:
        orphan_open = 0
        for h in (
            db.query(HedgePosition)
            .filter(HedgePosition.status == "closed")
            .all()
        ):
            from backend.models import Trade

            n = (
                db.query(Trade)
                .filter(
                    Trade.hedge_position_id == int(h.id),
                    Trade.status == TradeStatus.ACTIVE.value,
                )
                .count()
            )
            if n:
                orphan_open += 1
                print(
                    f"WARNING: closed hedge #{h.id} still has {n} active basket(s)"
                )
        if orphan_open == 0:
            print("OK: no closed hedges with active baskets.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
