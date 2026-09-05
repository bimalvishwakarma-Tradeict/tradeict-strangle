#!/usr/bin/env python3
"""
Back-fill missing entry fees on open legs / hedges from Delta order commission.

RULE 8: only Delta wallet/order commission is truth — never estimate.

Does NOT auto-run. Invoke explicitly after deploy:

  cd /path/to/trading-bot
  python deploy/backfill_entry_fees.py --dry-run
  python deploy/backfill_entry_fees.py --apply

Optional:
  --leg-id N
  --hedge-id N
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.delta_client import DeltaClient
from backend.core.encryption import decrypt
from backend.database import SessionLocal, init_db
from backend.models import Account, HedgePosition, Leg, SlaveHedgePosition

_OPEN_LEG = ("active", "open")
_OPEN_HEDGE = ("active", "pending_close")


def _fee_missing(val: Any) -> bool:
    if val is None:
        return True
    try:
        return float(val) <= 0
    except (TypeError, ValueError):
        return True


async def _commission(client: DeltaClient, order_id: str | int) -> float | None:
    try:
        val = abs(float(await client.get_order_commission(order_id)))
    except Exception as exc:
        print(f"  ! get_order_commission({order_id}) failed: {exc}")
        return None
    return val if val > 0 else None


async def _master_client(db: Any) -> DeltaClient:
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        raise RuntimeError("No active master Account with API keys")
    return DeltaClient(
        decrypt(account.api_key_encrypted),
        decrypt(account.api_secret_encrypted),
    )


async def run(*, apply: bool, leg_id: int | None, hedge_id: int | None) -> int:
    init_db()
    db = SessionLocal()
    client: DeltaClient | None = None
    updates = 0
    try:
        client = await _master_client(db)

        # --- Basket / wing legs ---
        leg_q = db.query(Leg).filter(Leg.status.in_(_OPEN_LEG))
        if leg_id is not None:
            leg_q = leg_q.filter(Leg.id == int(leg_id))
        for leg in leg_q.all():
            if not _fee_missing(getattr(leg, "entry_fee_usd", None)):
                continue
            oid = getattr(leg, "delta_order_id", None)
            if not oid:
                print(
                    f"leg id={leg.id} trade={leg.trade_id} "
                    f"type={leg.leg_type}: SKIP (no delta_order_id)"
                )
                continue
            fee = await _commission(client, oid)
            print(
                f"leg id={leg.id} trade={leg.trade_id} type={leg.leg_type} "
                f"order={oid} -> entry_fee_usd={fee}"
            )
            if fee is None:
                continue
            if apply:
                leg.entry_fee_usd = float(fee)
                updates += 1

        # --- Master hedges ---
        hq = db.query(HedgePosition).filter(
            HedgePosition.status.in_(_OPEN_HEDGE)
        )
        if hedge_id is not None:
            hq = hq.filter(HedgePosition.id == int(hedge_id))
        for h in hq.all():
            for side, oid_attr, fee_attr in (
                ("call", "call_order_id", "call_entry_fee_usd"),
                ("put", "put_order_id", "put_entry_fee_usd"),
            ):
                if not _fee_missing(getattr(h, fee_attr, None)):
                    continue
                oid = getattr(h, oid_attr, None)
                if not oid:
                    print(
                        f"hedge id={h.id} {side}: SKIP (no {oid_attr})"
                    )
                    continue
                fee = await _commission(client, oid)
                print(
                    f"hedge id={h.id} {side} order={oid} "
                    f"-> {fee_attr}={fee}"
                )
                if fee is None:
                    continue
                if apply:
                    setattr(h, fee_attr, float(fee))
                    updates += 1

        # --- Slave hedges (only rows still missing fees) ---
        shq = db.query(SlaveHedgePosition).filter(
            SlaveHedgePosition.status.in_(_OPEN_HEDGE)
        )
        if hedge_id is not None:
            shq = shq.filter(
                SlaveHedgePosition.master_hedge_id == int(hedge_id)
            )
        for sh in shq.all():
            for side, oid_attr, fee_attr in (
                ("call", "call_order_id", "call_entry_fee_usd"),
                ("put", "put_order_id", "put_entry_fee_usd"),
            ):
                if not _fee_missing(getattr(sh, fee_attr, None)):
                    continue
                oid = getattr(sh, oid_attr, None)
                if not oid:
                    print(
                        f"slave_hedge id={sh.id} {side}: SKIP (no {oid_attr})"
                    )
                    continue
                # Slave orders live on the slave account — master client
                # may not see them. Still try; print clearly on miss.
                fee = await _commission(client, oid)
                print(
                    f"slave_hedge id={sh.id} slave={sh.slave_account_id} "
                    f"{side} order={oid} -> {fee_attr}={fee} "
                    f"(master client; may need slave keys if None)"
                )
                if fee is None:
                    continue
                if apply:
                    setattr(sh, fee_attr, float(fee))
                    updates += 1

        if apply:
            db.commit()
            print(f"Applied {updates} fee update(s).")
        else:
            print("Dry-run only — pass --apply to write.")
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Show proposed fees (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist commissions to DB",
    )
    parser.add_argument("--leg-id", type=int, default=None)
    parser.add_argument("--hedge-id", type=int, default=None)
    args = parser.parse_args()
    apply = bool(args.apply)
    return asyncio.run(
        run(apply=apply, leg_id=args.leg_id, hedge_id=args.hedge_id)
    )


if __name__ == "__main__":
    raise SystemExit(main())
