#!/usr/bin/env python3
"""
One-off: recompute cum_closed_basket_pnl + structure_pnl for closed hedges.

Also reconstructs hedge_net_mtm from fill/exit prices when it is still 0.0
(pre-instrumentation closes). Does NOT modify realized_pnl or any leg row.

  cd /path/to/trading-bot
  python deploy/backfill_structure_pnl.py --dry-run
  python deploy/backfill_structure_pnl.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import OPTIONS_CONTRACT_VALUE, TradeStatus
from backend.core.fees import estimate_option_trading_fee
from backend.database import SessionLocal, init_db
from backend.engine.hedge_lifecycle import _cum_closed_basket_pnl
from backend.models import HedgePosition

CONTRACT_SIZE = float(OPTIONS_CONTRACT_VALUE)


def _four_prices_present(h: HedgePosition) -> bool:
    for attr in (
        "call_fill_price",
        "call_exit_price",
        "put_fill_price",
        "put_exit_price",
    ):
        v = getattr(h, attr, None)
        if v is None:
            return False
        try:
            if float(v) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def reconstruct_hedge_net_mtm(h: HedgePosition) -> dict[str, float] | None:
    """
    Rebuild hedge_net_mtm from stored fill/exit prices.

    Returns None if prices are incomplete. Does not mutate ``h``.
    """
    if not _four_prices_present(h):
        return None

    call_fill = float(h.call_fill_price)
    call_exit = float(h.call_exit_price)
    put_fill = float(h.put_fill_price)
    put_exit = float(h.put_exit_price)
    qty = max(1, int(h.quantity or 1))

    gross = (
        (call_exit - call_fill) + (put_exit - put_fill)
    ) * qty * CONTRACT_SIZE

    entry_fees = max(0.0, float(h.call_entry_fee_usd or 0.0)) + max(
        0.0, float(h.put_entry_fee_usd or 0.0)
    )

    # Same per-leg exit fee estimator as the live hedge monitor.
    # ATM strike is the best historical proxy for BTC index when spot is gone.
    btc_index = float(h.strike or 0.0)
    est_exit = 0.0
    if btc_index > 0:
        est_exit += estimate_option_trading_fee(
            option_price=call_exit,
            quantity_lots=qty,
            btc_index_price=btc_index,
        )
        est_exit += estimate_option_trading_fee(
            option_price=put_exit,
            quantity_lots=qty,
            btc_index_price=btc_index,
        )

    net = float(gross) - float(entry_fees) - float(est_exit)
    return {
        "gross": float(gross),
        "entry_fees": float(entry_fees),
        "est_exit_fees": float(est_exit),
        "hedge_net_mtm": float(net),
    }


def _should_reconstruct(h: HedgePosition) -> bool:
    """Only zero hedge_net_mtm with complete fill/exit prices."""
    net = float(getattr(h, "hedge_net_mtm", 0.0) or 0.0)
    if abs(net) > 1e-12:
        return False
    return _four_prices_present(h)


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
        help="Persist reconstructed hedge_net + structure_pnl",
    )
    args = parser.parse_args()
    apply = bool(args.apply)

    init_db()
    updated = 0
    reconstructed = 0

    print(
        f"{'id':>4}  {'src':<14}  "
        f"{'gross':>10}  {'fees':>10}  {'net':>10}  "
        f"{'hedge_before':>12}  {'hedge_after':>12}  "
        f"{'struct_before':>14}  {'struct_after':>14}"
    )
    print("-" * 128)

    with SessionLocal() as db:
        hedges = (
            db.query(HedgePosition)
            .filter(HedgePosition.status == "closed")
            .order_by(HedgePosition.id.asc())
            .all()
        )
        for h in hedges:
            hid = int(h.id)
            hedge_before = float(getattr(h, "hedge_net_mtm", 0.0) or 0.0)
            cum_before = float(getattr(h, "cum_closed_basket_pnl", 0.0) or 0.0)
            struct_before = float(getattr(h, "structure_pnl", 0.0) or 0.0)
            source_before = str(
                getattr(h, "hedge_net_source", None) or "live"
            )

            hedge_after = hedge_before
            gross = 0.0
            fees_total = 0.0
            src_label = source_before
            did_reconstruct = False

            recon: dict[str, Any] | None = None
            if _should_reconstruct(h):
                recon = reconstruct_hedge_net_mtm(h)
                if recon is not None:
                    hedge_after = float(recon["hedge_net_mtm"])
                    gross = float(recon["gross"])
                    fees_total = float(recon["entry_fees"]) + float(
                        recon["est_exit_fees"]
                    )
                    src_label = "reconstructed"
                    did_reconstruct = True

            cum_after = float(_cum_closed_basket_pnl(db, hid))
            struct_after = float(hedge_after) + float(cum_after)

            print(
                f"{hid:4d}  {src_label:<14}  "
                f"{gross:10.6f}  {fees_total:10.6f}  {hedge_after:10.6f}  "
                f"{hedge_before:12.6f}  {hedge_after:12.6f}  "
                f"{struct_before:14.6f}  {struct_after:14.6f}"
            )
            if did_reconstruct and recon is not None:
                print(
                    f"      reconstruct detail: "
                    f"gross={recon['gross']:.6f} "
                    f"entry_fees={recon['entry_fees']:.6f} "
                    f"est_exit_fees={recon['est_exit_fees']:.6f} "
                    f"net={recon['hedge_net_mtm']:.6f}"
                )

            dirty = False
            if apply and did_reconstruct and recon is not None:
                h.hedge_net_mtm = float(recon["hedge_net_mtm"])
                h.hedge_net_source = "reconstructed"
                reconstructed += 1
                dirty = True

            if apply and (
                abs(cum_before - cum_after) > 1e-9
                or abs(struct_before - struct_after) > 1e-9
                or dirty
            ):
                h.cum_closed_basket_pnl = float(cum_after)
                h.structure_pnl = float(struct_after)
                updated += 1

        if apply:
            db.commit()
            print(
                f"\nApplied: updated {updated} closed hedge(s) "
                f"({reconstructed} hedge_net reconstructed)."
            )
        else:
            print(
                f"\nDry-run: {len(hedges)} closed hedge(s). "
                "Re-run with --apply to persist."
            )

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
