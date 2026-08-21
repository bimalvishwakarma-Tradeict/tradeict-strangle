#!/usr/bin/env python3
"""
One-off: recompute cum_closed_basket_pnl + structure_pnl for closed hedges.

Preference for the hedge component of structure_pnl:
  1. realized_pnl when non-null  → hedge_net_source='realized'
  2. else reconstruct from fill/exit when hedge_net_mtm is 0.0
     → hedge_net_source='reconstructed'
  3. else keep stored hedge_net_mtm

Does NOT modify realized_pnl or any leg row. Idempotent — a second --apply
run writes nothing if values already match.

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
_EPS = 1e-9


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
    Rebuild hedge component from stored fill/exit prices.

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


def _resolve_hedge_component(
    h: HedgePosition,
) -> tuple[float, str, dict[str, Any] | None]:
    """
    Return (hedge_component, source, optional reconstruct detail).

    Prefer realized_pnl; else reconstruct from fills when net MTM is zero;
    else keep stored hedge_net_mtm.
    """
    realized = getattr(h, "realized_pnl", None)
    if realized is not None:
        return float(realized), "realized", None

    stored_net = float(getattr(h, "hedge_net_mtm", 0.0) or 0.0)
    if abs(stored_net) <= 1e-12 and _four_prices_present(h):
        recon = reconstruct_hedge_net_mtm(h)
        if recon is not None:
            return float(recon["hedge_net_mtm"]), "reconstructed", recon

    source = str(getattr(h, "hedge_net_source", None) or "live")
    return stored_net, source, None


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
        help="Persist structure_pnl / source / reconstructed net",
    )
    args = parser.parse_args()
    apply = bool(args.apply)

    init_db()
    updated = 0
    realized_n = 0
    reconstructed_n = 0

    print(
        f"{'id':>4}  {'src':<14}  "
        f"{'hedge_comp':>12}  "
        f"{'cum_before':>12}  {'cum_after':>12}  "
        f"{'struct_before':>14}  {'struct_after':>14}"
    )
    print("-" * 110)

    with SessionLocal() as db:
        hedges = (
            db.query(HedgePosition)
            .filter(HedgePosition.status == "closed")
            .order_by(HedgePosition.id.asc())
            .all()
        )
        for h in hedges:
            hid = int(h.id)
            cum_before = float(getattr(h, "cum_closed_basket_pnl", 0.0) or 0.0)
            struct_before = float(getattr(h, "structure_pnl", 0.0) or 0.0)
            source_before = str(
                getattr(h, "hedge_net_source", None) or "live"
            )
            net_before = float(getattr(h, "hedge_net_mtm", 0.0) or 0.0)

            hedge_comp, src_label, recon = _resolve_hedge_component(h)
            cum_after = float(_cum_closed_basket_pnl(db, hid))
            struct_after = float(hedge_comp) + float(cum_after)

            print(
                f"{hid:4d}  {src_label:<14}  "
                f"{hedge_comp:12.6f}  "
                f"{cum_before:12.6f}  {cum_after:12.6f}  "
                f"{struct_before:14.6f}  {struct_after:14.6f}"
            )
            if recon is not None:
                print(
                    f"      reconstruct detail: "
                    f"gross={recon['gross']:.6f} "
                    f"entry_fees={recon['entry_fees']:.6f} "
                    f"est_exit_fees={recon['est_exit_fees']:.6f} "
                    f"net={recon['hedge_net_mtm']:.6f}"
                )

            needs_write = (
                abs(cum_before - cum_after) > _EPS
                or abs(struct_before - struct_after) > _EPS
                or source_before != src_label
                or (
                    src_label == "reconstructed"
                    and abs(net_before - hedge_comp) > _EPS
                )
            )
            if not needs_write:
                continue

            if not apply:
                continue

            h.cum_closed_basket_pnl = float(cum_after)
            h.structure_pnl = float(struct_after)
            h.hedge_net_source = src_label
            if src_label == "reconstructed" and recon is not None:
                # Only the reconstruction path writes hedge_net_mtm
                h.hedge_net_mtm = float(recon["hedge_net_mtm"])
                reconstructed_n += 1
            elif src_label == "realized":
                realized_n += 1
            updated += 1

        if apply:
            db.commit()
            print(
                f"\nApplied: updated {updated} closed hedge(s) "
                f"(realized={realized_n}, reconstructed={reconstructed_n})."
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
