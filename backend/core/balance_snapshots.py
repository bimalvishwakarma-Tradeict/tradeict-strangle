"""Daily noon IST balance snapshots + dashboard balance field helpers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytz
from sqlalchemy.orm import Session

from backend.config import IST
from backend.core.balance_utils import (
    compute_daily_growth_pct,
    wallet_to_balance_fields,
)
from backend.core.delta_client import DeltaClient
from backend.core.encryption import decrypt
from backend.core.time_utils import get_utc_now
from backend.models import Account, BalanceSnapshot, SlaveAccount

logger = logging.getLogger(__name__)

SNAPSHOT_WINDOW_MINUTES = 3

__all__ = [
    "SNAPSHOT_WINDOW_MINUTES",
    "build_balance_detail",
    "compute_daily_growth_pct",
    "get_yesterday_noon_snapshot_balance",
    "take_daily_balance_snapshot",
    "wallet_to_balance_fields",
]


def get_yesterday_noon_snapshot_balance(
    db: Session,
    *,
    account_id: int,
    account_type: str,
) -> float | None:
    """Return wallet_balance from yesterday 12:00 PM IST snapshot, if any."""
    now_ist = datetime.now(IST)
    yesterday_noon = (now_ist - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    window_start = yesterday_noon - timedelta(minutes=SNAPSHOT_WINDOW_MINUTES)
    window_end = yesterday_noon + timedelta(minutes=SNAPSHOT_WINDOW_MINUTES)
    row = (
        db.query(BalanceSnapshot)
        .filter(
            BalanceSnapshot.account_id == int(account_id),
            BalanceSnapshot.account_type == str(account_type),
            BalanceSnapshot.snapshot_time >= window_start.astimezone(pytz.utc),
            BalanceSnapshot.snapshot_time <= window_end.astimezone(pytz.utc),
        )
        .order_by(BalanceSnapshot.snapshot_time.desc())
        .first()
    )
    if row is None:
        return None
    return float(row.wallet_balance)


def build_balance_detail(
    db: Session,
    wallet: dict[str, float] | None,
    *,
    account_id: int,
    account_type: str,
    usd_inr_rate: float,
) -> dict[str, object]:
    fields = wallet_to_balance_fields(wallet, usd_inr_rate=usd_inr_rate)
    yesterday = get_yesterday_noon_snapshot_balance(
        db,
        account_id=account_id,
        account_type=account_type,
    )
    actual = fields.get("actual_balance")
    growth = compute_daily_growth_pct(actual, yesterday)
    return {
        **fields,
        "yesterday_balance": round(yesterday, 4) if yesterday is not None else None,
        "daily_growth_pct": growth,
    }


def _snapshot_exists_for_noon(
    db: Session,
    *,
    account_id: int,
    account_type: str,
    noon_ist: datetime,
) -> bool:
    window_start = noon_ist - timedelta(minutes=SNAPSHOT_WINDOW_MINUTES)
    window_end = noon_ist + timedelta(minutes=SNAPSHOT_WINDOW_MINUTES)
    return (
        db.query(BalanceSnapshot.id)
        .filter(
            BalanceSnapshot.account_id == int(account_id),
            BalanceSnapshot.account_type == str(account_type),
            BalanceSnapshot.snapshot_time >= window_start.astimezone(pytz.utc),
            BalanceSnapshot.snapshot_time <= window_end.astimezone(pytz.utc),
        )
        .first()
        is not None
    )


async def take_daily_balance_snapshot(db: Session, client: DeltaClient | None) -> None:
    """
    Runs each monitor cycle. During 12:00–12:02 PM IST, persist wallet_balance
    once per account per day.
    """
    now_ist = datetime.now(IST)
    if now_ist.hour != 12 or now_ist.minute >= SNAPSHOT_WINDOW_MINUTES:
        return

    today_noon = now_ist.replace(hour=12, minute=0, second=0, microsecond=0)

    master = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if master is not None and client is not None:
        if not _snapshot_exists_for_noon(
            db,
            account_id=int(master.id),
            account_type="master",
            noon_ist=today_noon,
        ):
            try:
                bal = await client.get_wallet_balance()
                wallet_bal = float(
                    bal.get("wallet_balance") or bal.get("balance_usdt") or 0.0
                )
                snap = BalanceSnapshot(
                    account_id=int(master.id),
                    account_type="master",
                    snapshot_time=get_utc_now(),
                    wallet_balance=wallet_bal,
                )
                db.add(snap)
                db.commit()
                logger.info(
                    "[BALANCE_SNAPSHOT] master id=%s | wallet=%s",
                    master.id,
                    round(wallet_bal, 4),
                )
            except Exception as exc:
                db.rollback()
                logger.warning("Master balance snapshot failed: %s", exc)

    slaves = (
        db.query(SlaveAccount)
        .filter(
            SlaveAccount.is_active.is_(True),
            SlaveAccount.is_virtual.is_(False),
        )
        .order_by(SlaveAccount.id.asc())
        .all()
    )
    for slave in slaves:
        if _snapshot_exists_for_noon(
            db,
            account_id=int(slave.id),
            account_type="slave",
            noon_ist=today_noon,
        ):
            continue
        slave_client: DeltaClient | None = None
        try:
            slave_client = DeltaClient(
                decrypt(slave.api_key_encrypted),
                decrypt(slave.api_secret_encrypted),
            )
            bal = await slave_client.get_wallet_balance()
            wallet_bal = float(
                bal.get("wallet_balance") or bal.get("balance_usdt") or 0.0
            )
            snap = BalanceSnapshot(
                account_id=int(slave.id),
                account_type="slave",
                snapshot_time=get_utc_now(),
                wallet_balance=wallet_bal,
            )
            db.add(snap)
            db.commit()
            logger.info(
                "[BALANCE_SNAPSHOT] slave id=%s name=%s | wallet=%s",
                slave.id,
                slave.name,
                round(wallet_bal, 4),
            )
        except Exception as exc:
            db.rollback()
            logger.warning(
                "Slave %s balance snapshot failed: %s", slave.name, exc
            )
        finally:
            if slave_client is not None:
                try:
                    await slave_client.close()
                except Exception:
                    pass
