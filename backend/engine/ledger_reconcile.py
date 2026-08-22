# ledger_reconcile.py — Detect trades / hedges billing cannot attribute
#
# Read-only. Never writes. Findings are derived each run (not persisted).
# Identifiers only — no P&L or money fields.

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from backend.core.bot_logger import log_and_buffer
from backend.core.time_utils import as_utc, get_utc_now
from backend.models import (
    SlaveHedgePosition,
    SlaveTrade,
    Structure,
    StructureLeg,
    Trade,
)

logger = logging.getLogger(__name__)

# Structure ledger went live at first structure 2026-08-22 10:11:09.
# Override via env LEDGER_RECONCILE_SINCE (ISO8601 UTC) if the cutoff moves.
LEDGER_LIVE_FROM = "2026-08-22T10:11:00Z"

KIND_ORPHAN_TRADE = "ORPHAN_TRADE"
KIND_OPEN_LEG_CLOSED_STRUCTURE = "OPEN_LEG_CLOSED_STRUCTURE"
KIND_OPEN_LEG_CLOSED_TRADE = "OPEN_LEG_CLOSED_TRADE"
KIND_ATTRIBUTION_WARNING = "ATTRIBUTION_WARNING"
KIND_NO_STRUCTURE_FOR_HEDGE = "NO_STRUCTURE_FOR_HEDGE"

_BASKET_ROLES = ("BASKET_CALL", "BASKET_PUT")

# Statuses that never produced a billable mirror position
_ORPHAN_SKIP_STATUSES = frozenset(
    {
        "skipped_no_hedge",
        "skipped_low_capital",
        "error",
        "partial_entry_open",
    }
)


def ledger_reconcile_since() -> datetime:
    """Cutoff: ignore rows created/opened before structure ledger went live."""
    raw = str(os.getenv("LEDGER_RECONCILE_SINCE") or LEDGER_LIVE_FROM).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.error(
            "[LEDGER_RECONCILE] invalid LEDGER_RECONCILE_SINCE=%r — "
            "falling back to %s",
            raw,
            LEDGER_LIVE_FROM,
        )
        dt = datetime.fromisoformat(LEDGER_LIVE_FROM.replace("Z", "+00:00"))
    aware = as_utc(dt)
    if aware is None:
        return datetime(2026, 8, 22, 10, 11, 0, tzinfo=timezone.utc)
    return aware


def _is_before_cutoff(ts: Any, cutoff: datetime) -> bool:
    aware = as_utc(ts) if ts is not None else None
    if aware is None:
        # Unknown time — do not silence; treat as post-cutoff
        return False
    return aware < cutoff


def _has_real_position(st: SlaveTrade) -> bool:
    """True only when the slave actually opened a mirror position."""
    qty = int(getattr(st, "actual_quantity", 0) or 0)
    if qty <= 0:
        return False
    call_pid = getattr(st, "call_product_id", None)
    put_pid = getattr(st, "put_product_id", None)
    return call_pid is not None or put_pid is not None


def _finding(
    *,
    kind: str,
    detected_at: datetime,
    slave_account_id: int | None = None,
    master_trade_id: int | None = None,
    structure_id: int | None = None,
    leg_id: int | None = None,
    product_id: int | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "slave_account_id": slave_account_id,
        "master_trade_id": master_trade_id,
        "structure_id": structure_id,
        "leg_id": leg_id,
        "product_id": product_id,
        "symbol": symbol,
        "detected_at": detected_at.isoformat(),
    }


def _basket_legs_for_slave_trade(
    db: Session,
    *,
    slave_account_id: int,
    master_trade: Trade | None,
    slave_trade: SlaveTrade,
) -> list[StructureLeg]:
    """Structure legs that belong to this slave's mirror of the master basket."""
    if master_trade is None:
        return []
    hid = getattr(master_trade, "hedge_position_id", None)
    if hid is None:
        return []

    structs = (
        db.query(Structure)
        .filter(
            Structure.account_kind == "SLAVE",
            Structure.slave_account_id == int(slave_account_id),
            Structure.hedge_position_id == int(hid),
        )
        .all()
    )
    if not structs:
        return []

    struct_ids = [int(s.id) for s in structs]
    basket_seq = getattr(master_trade, "basket_seq_in_structure", None)
    q = db.query(StructureLeg).filter(
        StructureLeg.structure_id.in_(struct_ids),
        StructureLeg.leg_role.in_(_BASKET_ROLES),
    )
    if basket_seq is not None:
        q = q.filter(StructureLeg.basket_seq == int(basket_seq))
    else:
        call_pid = int(getattr(slave_trade, "call_product_id", 0) or 0)
        put_pid = int(getattr(slave_trade, "put_product_id", 0) or 0)
        pids = [p for p in (call_pid, put_pid) if p > 0]
        if pids:
            q = q.filter(StructureLeg.product_id.in_(pids))
        else:
            return []
    return list(q.all())


def reconcile_ledger(db: Session) -> dict[str, Any]:
    """
    Scan DB for ledger gaps billing cannot see.

    Read-only — does not flush, commit, or mutate any row.

    Returns:
        {
          "findings": [...],
          "skipped_pre_ledger": int,
          "skipped_no_position": int,
          "since": ISO8601 cutoff used,
        }
    """
    detected_at = get_utc_now()
    cutoff = ledger_reconcile_since()
    findings: list[dict[str, Any]] = []
    skipped_pre_ledger = 0
    skipped_no_position = 0

    # --- a) ORPHAN_TRADE + c) OPEN_LEG_CLOSED_TRADE ---
    slave_trades = db.query(SlaveTrade).all()
    master_ids = {
        int(st.master_trade_id)
        for st in slave_trades
        if st.master_trade_id is not None
    }
    masters_by_id: dict[int, Trade] = {}
    if master_ids:
        for row in db.query(Trade).filter(Trade.id.in_(master_ids)).all():
            masters_by_id[int(row.id)] = row

    for st in slave_trades:
        sid = int(st.slave_account_id)
        mid = int(st.master_trade_id)
        status = str(st.status or "").lower().strip()

        if _is_before_cutoff(getattr(st, "created_at", None), cutoff):
            skipped_pre_ledger += 1
            continue

        if status in _ORPHAN_SKIP_STATUSES or not _has_real_position(st):
            skipped_no_position += 1
            continue

        master = masters_by_id.get(mid)
        legs = _basket_legs_for_slave_trade(
            db,
            slave_account_id=sid,
            master_trade=master,
            slave_trade=st,
        )
        if not legs:
            findings.append(
                _finding(
                    kind=KIND_ORPHAN_TRADE,
                    detected_at=detected_at,
                    slave_account_id=sid,
                    master_trade_id=mid,
                    product_id=(
                        int(st.call_product_id)
                        if st.call_product_id
                        else (
                            int(st.put_product_id) if st.put_product_id else None
                        )
                    ),
                    symbol=st.call_symbol or st.put_symbol,
                )
            )
            continue

        if status == "closed":
            for leg in legs:
                if leg.closed_at is None:
                    findings.append(
                        _finding(
                            kind=KIND_OPEN_LEG_CLOSED_TRADE,
                            detected_at=detected_at,
                            slave_account_id=sid,
                            master_trade_id=mid,
                            structure_id=int(leg.structure_id),
                            leg_id=int(leg.id),
                            product_id=int(leg.product_id),
                            symbol=leg.symbol,
                        )
                    )

    # --- b) OPEN_LEG_CLOSED_STRUCTURE + d) ATTRIBUTION_WARNING ---
    closed_or_warned = (
        db.query(Structure)
        .options(joinedload(Structure.legs))
        .filter(
            or_(
                Structure.status == "closed",
                Structure.attribution_warning.isnot(None),
            )
        )
        .all()
    )
    seen_open_leg_closed_struct: set[int] = set()
    for struct in closed_or_warned:
        if _is_before_cutoff(getattr(struct, "opened_at", None), cutoff):
            skipped_pre_ledger += 1
            continue
        sid = (
            int(struct.slave_account_id)
            if struct.slave_account_id is not None
            else None
        )
        if struct.attribution_warning:
            findings.append(
                _finding(
                    kind=KIND_ATTRIBUTION_WARNING,
                    detected_at=detected_at,
                    slave_account_id=sid,
                    structure_id=int(struct.id),
                )
            )
        if str(struct.status or "").lower() != "closed":
            continue
        for leg in list(struct.legs or []):
            if leg.closed_at is not None:
                continue
            lid = int(leg.id)
            if lid in seen_open_leg_closed_struct:
                continue
            seen_open_leg_closed_struct.add(lid)
            findings.append(
                _finding(
                    kind=KIND_OPEN_LEG_CLOSED_STRUCTURE,
                    detected_at=detected_at,
                    slave_account_id=sid,
                    structure_id=int(struct.id),
                    leg_id=lid,
                    product_id=int(leg.product_id),
                    symbol=leg.symbol,
                )
            )

    # --- e) NO_STRUCTURE_FOR_HEDGE ---
    hedges = db.query(SlaveHedgePosition).all()
    for sh in hedges:
        if _is_before_cutoff(getattr(sh, "entry_time", None), cutoff):
            skipped_pre_ledger += 1
            continue
        sid = int(sh.slave_account_id)
        mid_h = int(sh.master_hedge_id)
        exists = (
            db.query(Structure.id)
            .filter(
                Structure.account_kind == "SLAVE",
                Structure.slave_account_id == sid,
                Structure.hedge_position_id == mid_h,
            )
            .first()
        )
        if exists is None:
            findings.append(
                _finding(
                    kind=KIND_NO_STRUCTURE_FOR_HEDGE,
                    detected_at=detected_at,
                    slave_account_id=sid,
                    product_id=(
                        int(sh.call_product_id)
                        if sh.call_product_id
                        else (
                            int(sh.put_product_id) if sh.put_product_id else None
                        )
                    ),
                    symbol=sh.call_symbol or sh.put_symbol,
                )
            )

    return {
        "findings": findings,
        "skipped_pre_ledger": int(skipped_pre_ledger),
        "skipped_no_position": int(skipped_no_position),
        "since": cutoff.isoformat(),
    }


def counts_by_kind(findings: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(f.get("kind") or "") for f in findings)
    return dict(sorted(counter.items()))


def log_reconcile_findings(result: dict[str, Any]) -> None:
    """
    findings == 0 → one INFO summary (LEDGER_RECONCILE)
    findings > 0  → ERROR summary + one ERROR line per finding
                    (LEDGER_RECONCILE_ALERT + logger.error for error.log)
    """
    findings = list(result.get("findings") or [])
    skipped_pre = int(result.get("skipped_pre_ledger") or 0)
    skipped_pos = int(result.get("skipped_no_position") or 0)
    n = len(findings)

    if n == 0:
        try:
            log_and_buffer(
                "LEDGER_RECONCILE",
                0,
                {
                    "findings": 0,
                    "skipped_pre_ledger": skipped_pre,
                    "skipped_no_position": skipped_pos,
                },
            )
        except Exception as exc:
            logger.warning("LEDGER_RECONCILE buffer failed: %s", exc)
            logger.info(
                "[LEDGER_RECONCILE] findings=0 skipped_pre_ledger=%s "
                "skipped_no_position=%s",
                skipped_pre,
                skipped_pos,
            )
        return

    # Summary at ERROR — money-critical gaps exist
    summary_details = {
        "findings": n,
        "skipped_pre_ledger": skipped_pre,
        "skipped_no_position": skipped_pos,
    }
    logger.error(
        "[LEDGER_RECONCILE] findings=%s skipped_pre_ledger=%s "
        "skipped_no_position=%s",
        n,
        skipped_pre,
        skipped_pos,
    )
    try:
        log_and_buffer("LEDGER_RECONCILE_ALERT", 0, summary_details)
    except Exception as exc:
        logger.warning("LEDGER_RECONCILE_ALERT buffer failed: %s", exc)

    for f in findings:
        kind = str(f.get("kind") or "")
        slave = f.get("slave_account_id")
        master = f.get("master_trade_id")
        structure = f.get("structure_id")
        leg = f.get("leg_id")
        pid = f.get("product_id")
        logger.error(
            "[LEDGER_RECONCILE] kind=%s slave=%s master_trade=%s "
            "structure=%s leg=%s product_id=%s",
            kind,
            slave,
            master,
            structure,
            leg,
            pid,
        )
        try:
            log_and_buffer(
                "LEDGER_RECONCILE_ALERT",
                int(master or 0),
                {
                    "kind": kind,
                    "slave": slave,
                    "master_trade": master,
                    "structure": structure,
                    "leg": leg,
                    "product_id": pid,
                    "symbol": f.get("symbol"),
                },
            )
        except Exception as exc:
            logger.warning("LEDGER_RECONCILE_ALERT buffer failed: %s", exc)
