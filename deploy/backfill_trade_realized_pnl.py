#!/usr/bin/env python3
"""
Backfill / repair trade.realized_pnl from closed bot-managed legs.

Classifies issues into:
  CLASS A  — trade total stale (stored != sum of legs with realized)
  CLASS B1 — leg realized NULL but entry+exit premium present (arithmetic repair)
  CLASS B2 — exit premium missing (needs Delta fills)

Selection filters (--max-abs-diff, --min-abs-diff, --trade-ids) apply to
modifications only; the full scan report always lists every closed trade.

Does NOT auto-run. Examples:

  python deploy/backfill_trade_realized_pnl.py
  python deploy/backfill_trade_realized_pnl.py --max-abs-diff 0.5
  python deploy/backfill_trade_realized_pnl.py --apply --max-abs-diff 0.5 --yes
  python deploy/backfill_trade_realized_pnl.py --apply --repair-legs --yes
  python deploy/backfill_trade_realized_pnl.py --trade-ids 121,122 --apply --repair-legs --yes
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

_DEPLOY_DIR = Path(__file__).resolve().parent

from backend.config import TradeStatus
from backend.core.backfill_realized import (
    BackfillAuditTrail,
    LegChangeRecord,
    SelectionCriteria,
    TradeAuditRecord,
    TradeBackfillAnalysis,
    analyze_all_trades,
    append_pnl_unresolved_note,
    audit_json_path,
    bucket_trade_ids,
    classify_unresolved_bucket,
    collect_unrecoverable_legs,
    compute_leg_realized_from_premiums,
    format_filtered_out_message,
    new_audit_trail,
    parse_trade_ids_csv,
    repair_b1_leg,
    select_analyses,
    trade_abs_diff,
    trade_diff,
    write_audit_json,
)
from backend.core.delta_client import DeltaClient, short_leg_realized_pnl
from backend.core.encryption import decrypt
from backend.database import SessionLocal, init_db
from backend.engine.trade_reconcile import (
    recompute_trade_realized_pnl,
    resolve_external_exit_fill,
)
from backend.models import Account, HedgePosition, Leg, Trade

_CLOSED_STATUSES = {
    TradeStatus.CLOSED.value,
    TradeStatus.EMERGENCY_CLOSED.value,
}

_DELTA_SLEEP_SEC = 0.35


def _build_delta_client(account: Account) -> DeltaClient:
    return DeltaClient(
        api_key=decrypt(account.api_key_encrypted),
        api_secret=decrypt(account.api_secret_encrypted),
    )


def _load_legs_by_trade(db: Any, trade_ids: list[int]) -> dict[int, list[Any]]:
    if not trade_ids:
        return {}
    rows = (
        db.query(Leg)
        .filter(Leg.trade_id.in_(trade_ids), Leg.is_bot_managed.is_(True))
        .all()
    )
    out: dict[int, list[Any]] = {tid: [] for tid in trade_ids}
    for leg in rows:
        out[int(leg.trade_id)].append(leg)
    return out


def _build_selection_criteria(args: argparse.Namespace) -> SelectionCriteria:
    trade_ids = parse_trade_ids_csv(getattr(args, "trade_ids", None))
    if args.trade_id is not None:
        explicit = trade_ids or []
        if int(args.trade_id) not in explicit:
            explicit.append(int(args.trade_id))
        trade_ids = sorted(set(explicit))
    return SelectionCriteria(
        max_abs_diff=args.max_abs_diff,
        min_abs_diff=args.min_abs_diff,
        trade_ids=trade_ids,
    )


def _print_bucket(title: str, trade_ids: list[int], detail_fn: Any | None = None) -> None:
    print(f"\n{title} — count={len(trade_ids)}")
    if not trade_ids:
        print("  (none)")
        return
    print(f"  trade_ids: {trade_ids}")
    if detail_fn is not None:
        for tid in trade_ids:
            detail_fn(tid)


def _collect_hedge_ids(analyses: list[TradeBackfillAnalysis]) -> set[int]:
    ids: set[int] = set()
    for row in analyses:
        if row.hedge_position_id is not None and (
            row.class_a or row.b1_legs or row.b2_legs
        ):
            ids.add(int(row.hedge_position_id))
    return ids


def _print_hedge_safety(db: Any, hedge_ids: set[int]) -> list[int]:
    active: list[int] = []
    if not hedge_ids:
        print("\nHedge structures affected: (none)")
        return active
    print(f"\nHedge structures affected: {sorted(hedge_ids)}")
    for hid in sorted(hedge_ids):
        hedge = db.query(HedgePosition).filter(HedgePosition.id == hid).first()
        status = str(getattr(hedge, "status", "") or "").lower() if hedge else "?"
        marker = "ACTIVE" if status == "active" else status
        print(f"  hedge_position_id={hid} status={marker}")
        if status == "active":
            active.append(hid)
    if active:
        for hid in active:
            print(
                f"⚠ ACTIVE hedge #{hid} affected — live SL budget will shift. "
                "Verify [SL_BASIS] logs after apply."
            )
    return active


def _count_pending_writes(
    analyses: list[TradeBackfillAnalysis],
    *,
    do_apply: bool,
    repair_legs: bool,
    from_delta: bool,
) -> tuple[int, int, int]:
    class_a_n = sum(1 for a in analyses if a.class_a) if do_apply else 0
    b1_n = sum(len(a.b1_legs) for a in analyses) if repair_legs and do_apply else 0
    b2_n = 0
    if from_delta and do_apply:
        for a in analyses:
            for snap in a.b2_legs:
                if snap.get("exit_order_id"):
                    b2_n += 1
    return class_a_n, b1_n, b2_n


def _confirm_apply(
    *,
    class_a_n: int,
    b1_n: int,
    b2_n: int,
    skip_confirm: bool,
) -> bool:
    total = class_a_n + b1_n + b2_n
    print(
        f"\nApply will modify up to {total} row(s): "
        f"CLASS_A trades={class_a_n}, B1 legs={b1_n}, B2 delta legs={b2_n}"
    )
    if total == 0:
        print("Nothing to write.")
        return False
    if skip_confirm:
        return True
    resp = input("Type YES to continue: ").strip()
    return resp == "YES"


class _AuditTracker:
    def __init__(self, trail: BackfillAuditTrail) -> None:
        self._trail = trail
        self._by_trade: dict[int, TradeAuditRecord] = {}

    def _ensure(
        self,
        analysis: TradeBackfillAnalysis,
        stored_before: float | None,
    ) -> TradeAuditRecord:
        tid = int(analysis.trade_id)
        if tid not in self._by_trade:
            rec = TradeAuditRecord(
                trade_id=tid,
                stored_before=stored_before,
                applied_value=None,
                diff=trade_diff(analysis),
                hedge_position_id=analysis.hedge_position_id,
            )
            self._by_trade[tid] = rec
            self._trail.add_trade(rec)
        return self._by_trade[tid]

    def record_leg_change(
        self,
        analysis: TradeBackfillAnalysis,
        *,
        stored_before: float | None,
        leg_id: int,
        field: str,
        before: Any,
        after: Any,
    ) -> None:
        rec = self._ensure(analysis, stored_before)
        rec.leg_changes.append(
            LegChangeRecord(
                leg_id=int(leg_id),
                field=str(field),
                before=before,
                after=after,
            )
        )

    def finalize_trade(
        self,
        analysis: TradeBackfillAnalysis,
        *,
        stored_before: float | None,
        applied_value: float | None,
    ) -> None:
        rec = self._ensure(analysis, stored_before)
        rec.applied_value = applied_value
        rec.diff = trade_diff(analysis)
        if applied_value is not None and stored_before is not None:
            rec.diff = round(float(applied_value) - float(stored_before), 6)
        elif applied_value is not None:
            rec.diff = round(float(applied_value), 6)


async def _repair_b2_from_delta(
    db: Any,
    client: DeltaClient,
    analyses: list[TradeBackfillAnalysis],
    audit: _AuditTracker | None,
    legs_by_trade: dict[int, list[Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    repaired: list[dict[str, Any]] = []
    unrecoverable: list[dict[str, Any]] = []

    for analysis in analyses:
        tid = int(analysis.trade_id)
        trade = db.query(Trade).filter(Trade.id == tid).first()
        if trade is None:
            continue
        stored_before = getattr(trade, "realized_pnl", None)
        legs = legs_by_trade.get(tid, [])
        leg_by_id = {int(lg.id): lg for lg in legs}

        for snap in analysis.b2_legs:
            if classify_unresolved_bucket(snap) != "B2":
                continue
            leg = leg_by_id.get(int(snap["leg_id"]))
            if leg is None:
                continue
            if getattr(leg, "realized_pnl", None) is not None:
                continue
            oid = getattr(leg, "exit_order_id", None)
            if not oid:
                unrecoverable.append(
                    {
                        "trade_id": tid,
                        "leg_id": int(leg.id),
                        "leg_type": getattr(leg, "leg_type", None),
                        "symbol": getattr(leg, "symbol", None),
                        "exit_order_id": None,
                        "reason": "no_exit_order_id",
                    }
                )
                continue

            await asyncio.sleep(_DELTA_SLEEP_SEC)
            exit_px = await resolve_external_exit_fill(client, leg)
            if exit_px is None or float(exit_px) <= 0:
                append_pnl_unresolved_note(trade, str(leg.leg_type or "leg"))
                unrecoverable.append(
                    {
                        "trade_id": tid,
                        "leg_id": int(leg.id),
                        "leg_type": getattr(leg, "leg_type", None),
                        "symbol": getattr(leg, "symbol", None),
                        "exit_order_id": str(oid),
                        "reason": "delta_fill_not_found",
                    }
                )
                continue

            before_realized = getattr(leg, "realized_pnl", None)
            before_exit = getattr(leg, "exit_premium", None)
            computed = compute_leg_realized_from_premiums(
                _with_exit_premium(leg, float(exit_px))
            )
            if computed is None:
                entry = float(getattr(leg, "initial_premium", 0) or 0)
                qty = abs(int(getattr(leg, "quantity", 0) or 0))
                computed = short_leg_realized_pnl(entry, float(exit_px), qty)

            leg.exit_premium = float(exit_px)
            leg.realized_pnl = float(computed)
            if audit is not None:
                audit.record_leg_change(
                    analysis,
                    stored_before=stored_before,
                    leg_id=int(leg.id),
                    field="exit_premium",
                    before=before_exit,
                    after=float(exit_px),
                )
                audit.record_leg_change(
                    analysis,
                    stored_before=stored_before,
                    leg_id=int(leg.id),
                    field="realized_pnl",
                    before=before_realized,
                    after=float(computed),
                )

            row = {
                "trade_id": tid,
                "leg_id": int(leg.id),
                "entry": float(getattr(leg, "initial_premium", 0) or 0),
                "exit": float(exit_px),
                "qty": int(getattr(leg, "quantity", 0) or 0),
                "realized": round(float(computed), 6),
            }
            print(
                f"  [B2 DELTA] trade={tid} leg={leg.id} "
                f"entry={row['entry']} exit={exit_px} qty={row['qty']} "
                f"realized={row['realized']}"
            )
            repaired.append(row)

    affected_trades = sorted({int(r["trade_id"]) for r in repaired})
    for tid in affected_trades:
        trade = db.query(Trade).filter(Trade.id == tid).first()
        if trade is not None:
            recompute_trade_realized_pnl(db, trade)

    return repaired, unrecoverable


def _with_exit_premium(leg: Any, exit_px: float) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        initial_premium=getattr(leg, "initial_premium", None),
        exit_premium=exit_px,
        quantity=getattr(leg, "quantity", None),
        is_long=bool(getattr(leg, "is_long", False)),
        realized_pnl=None,
    )


def _print_report(analyses: list[TradeBackfillAnalysis]) -> dict[str, list[int]]:
    buckets = bucket_trade_ids(analyses)
    analysis_by_id = {a.trade_id: a for a in analyses}

    def _a_detail(tid: int) -> None:
        a = analysis_by_id[tid]
        print(
            f"    trade={tid} stored={a.stored} "
            f"resolved_sum={a.resolved_total} diff={trade_diff(a)} "
            f"|diff|={trade_abs_diff(a)}"
        )

    def _b1_detail(tid: int) -> None:
        a = analysis_by_id[tid]
        for snap in a.b1_legs:
            print(
                f"    trade={tid} leg={snap['leg_id']} type={snap['leg_type']} "
                f"entry={snap['initial_premium']} exit={snap['exit_premium']} "
                f"qty={snap['quantity']}"
            )

    def _b2_detail(tid: int) -> None:
        a = analysis_by_id[tid]
        for snap in a.b2_legs:
            print(
                f"    trade={tid} leg={snap['leg_id']} type={snap['leg_type']} "
                f"exit_order_id={snap.get('exit_order_id')} "
                f"exit_premium={snap.get('exit_premium')}"
            )

    _print_bucket("CLASS A — trade total stale (recompute fixable)", buckets["CLASS_A"], _a_detail)
    _print_bucket(
        "CLASS B1 — realized NULL, entry+exit present (arithmetic repair)",
        buckets["CLASS_B1"],
        _b1_detail,
    )
    _print_bucket(
        "CLASS B2 — exit premium missing (needs Delta / manual)",
        buckets["CLASS_B2"],
        _b2_detail,
    )
    if buckets["FALSE_HEALTHY"]:
        print(
            f"\nFALSE HEALTHY — stored matches resolved sum but unresolved legs "
            f"exist: count={len(buckets['FALSE_HEALTHY'])} "
            f"trade_ids={buckets['FALSE_HEALTHY']}"
        )

    print(
        f"\nSUMMARY: CLASS_A={len(buckets['CLASS_A'])} "
        f"CLASS_B1={len(buckets['CLASS_B1'])} "
        f"CLASS_B2={len(buckets['CLASS_B2'])} "
        f"FALSE_HEALTHY={len(buckets['FALSE_HEALTHY'])} "
        f"trades_scanned={len(analyses)}"
    )
    return buckets


def _print_unrecoverable(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    print(f"\nUNRECOVERABLE — count={len(rows)}")
    for row in rows:
        print(
            f"  trade={row.get('trade_id')} leg={row.get('leg_id')} "
            f"type={row.get('leg_type')} symbol={row.get('symbol')} "
            f"exit_order_id={row.get('exit_order_id')} reason={row.get('reason')}"
        )


async def _async_main(args: argparse.Namespace) -> int:
    do_apply = bool(args.apply)
    repair_legs = bool(args.repair_legs)
    from_delta = bool(args.from_delta)
    criteria = _build_selection_criteria(args)

    if from_delta and not do_apply:
        print("ERROR: --from-delta requires --apply")
        return 1
    if repair_legs and not do_apply:
        print("NOTE: --repair-legs without --apply is preview-only (dry-run).")

    init_db()

    with SessionLocal() as db:
        q = db.query(Trade).filter(Trade.status.in_(sorted(_CLOSED_STATUSES)))
        trades = q.order_by(Trade.id.asc()).all()
        trade_ids = [int(t.id) for t in trades]
        legs_by_trade = _load_legs_by_trade(db, trade_ids)
        all_analyses = analyze_all_trades(trades, legs_by_trade)
        selection = select_analyses(all_analyses, criteria)
        selected = selection.selected

        print(f"Scanned {len(all_analyses)} closed trade(s).")
        buckets = _print_report(all_analyses)

        msg = format_filtered_out_message(selection)
        if msg:
            print(f"\n{msg}")
            print(
                f"  filtered trade_ids: "
                f"{sorted(a.trade_id for a in selection.filtered_out)}"
            )
        print(f"\nSelected for modification: {len(selected)} trade(s)")

        has_work = any(buckets[k] for k in ("CLASS_A", "CLASS_B1", "CLASS_B2"))
        if not has_work:
            print("\nNo issues found in any bucket.")
            return 0

        selected_ids = {a.trade_id for a in selected}
        selected_analyses = [a for a in all_analyses if a.trade_id in selected_ids]
        hedge_ids = _collect_hedge_ids(selected_analyses)
        _print_hedge_safety(db, hedge_ids)

        class_a_n, b1_n, b2_n = _count_pending_writes(
            selected_analyses,
            do_apply=do_apply,
            repair_legs=repair_legs,
            from_delta=from_delta,
        )

        audit_trail: BackfillAuditTrail | None = None
        audit_tracker: _AuditTracker | None = None
        if do_apply:
            audit_trail = new_audit_trail(
                {
                    "max_abs_diff": criteria.max_abs_diff,
                    "min_abs_diff": criteria.min_abs_diff,
                    "trade_ids": criteria.trade_ids,
                    "repair_legs": repair_legs,
                    "from_delta": from_delta,
                }
            )
            audit_tracker = _AuditTracker(audit_trail)
            if not _confirm_apply(
                class_a_n=class_a_n,
                b1_n=b1_n,
                b2_n=b2_n,
                skip_confirm=bool(args.yes),
            ):
                print("Aborted.")
                return 1

        unrecoverable: list[dict[str, Any]] = []
        trades_touched: set[int] = set()

        if repair_legs:
            for analysis in selected_analyses:
                if not analysis.b1_legs:
                    continue
                trade = db.query(Trade).filter(Trade.id == analysis.trade_id).first()
                if trade is None:
                    continue
                stored_before = getattr(trade, "realized_pnl", None)
                legs = legs_by_trade.get(analysis.trade_id, [])
                leg_by_id = {int(lg.id): lg for lg in legs}
                for snap in analysis.b1_legs:
                    leg = leg_by_id.get(int(snap["leg_id"]))
                    if leg is None:
                        continue
                    before_realized = getattr(leg, "realized_pnl", None)
                    computed = repair_b1_leg(leg, dry_run=not do_apply)
                    if computed is None:
                        continue
                    print(
                        f"  [{'DRY-RUN ' if not do_apply else ''}B1 REPAIR] "
                        f"trade={analysis.trade_id} leg={leg.id} "
                        f"entry={snap.get('initial_premium')} "
                        f"exit={snap.get('exit_premium')} qty={snap.get('quantity')} "
                        f"realized={round(float(computed), 6)}"
                    )
                    if do_apply and audit_tracker is not None:
                        audit_tracker.record_leg_change(
                            analysis,
                            stored_before=stored_before,
                            leg_id=int(leg.id),
                            field="realized_pnl",
                            before=before_realized,
                            after=float(computed),
                        )
                        trades_touched.add(int(analysis.trade_id))

        if from_delta and do_apply:
            account = (
                db.query(Account)
                .filter(Account.is_active.is_(True))
                .order_by(Account.id.asc())
                .first()
            )
            if account is None:
                print("ERROR: no active account for Delta client")
                return 1
            client = _build_delta_client(account)
            try:
                _repaired, delta_unrec = await _repair_b2_from_delta(
                    db,
                    client,
                    selected_analyses,
                    audit_tracker,
                    legs_by_trade,
                )
                unrecoverable.extend(delta_unrec)
                trades_touched.update(int(r["trade_id"]) for r in _repaired)
            finally:
                await client.close()
        elif from_delta and not do_apply:
            for analysis in selected_analyses:
                for snap in analysis.b2_legs:
                    if snap.get("exit_order_id"):
                        print(
                            f"  [DRY-RUN B2] would fetch Delta fill "
                            f"trade={snap['trade_id']} leg={snap['leg_id']} "
                            f"order={snap.get('exit_order_id')}"
                        )

        if do_apply:
            for analysis in selected_analyses:
                tid = int(analysis.trade_id)
                if not (analysis.class_a or tid in trades_touched):
                    continue
                trade = db.query(Trade).filter(Trade.id == tid).first()
                if trade is None:
                    continue
                stored_before = (
                    round(float(trade.realized_pnl), 6)
                    if trade.realized_pnl is not None
                    else None
                )
                total = recompute_trade_realized_pnl(db, trade)
                print(
                    f"  [CLASS A RECOMPUTE] trade={tid} "
                    f"stored={stored_before} -> {total}"
                )
                if audit_tracker is not None:
                    audit_tracker.finalize_trade(
                        analysis,
                        stored_before=stored_before,
                        applied_value=round(float(total), 6),
                    )
                trades_touched.add(tid)

            db.commit()
            print(f"\nApplied changes to {len(trades_touched)} trade(s).")
            if hedge_ids:
                print("Hedge structures to verify:")
                for hid in sorted(hedge_ids):
                    print(f"  hedge_position_id={hid}")

            if audit_trail is not None:
                out_path = audit_json_path(_DEPLOY_DIR, audit_trail.timestamp_utc)
                write_audit_json(out_path, audit_trail)
                print(f"\nAudit trail written: {out_path}")
        else:
            print("\nDry-run complete — no DB writes.")

        preview_unrec = collect_unrecoverable_legs(all_analyses)
        if not do_apply or not from_delta:
            _print_unrecoverable(preview_unrec)
        elif unrecoverable:
            _print_unrecoverable(unrecoverable)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview only (default)",
    )
    parser.add_argument("--apply", action="store_true", help="Write DB changes")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt when using --apply",
    )
    parser.add_argument(
        "--repair-legs",
        action="store_true",
        help="Repair CLASS B1 legs via entry/exit arithmetic",
    )
    parser.add_argument(
        "--from-delta",
        action="store_true",
        help="Fetch B2 exit fills from Delta (requires --apply)",
    )
    parser.add_argument("--trade-id", type=int, default=None)
    parser.add_argument(
        "--trade-ids",
        type=str,
        default=None,
        help="Comma-separated trade ids (e.g. 121,122)",
    )
    parser.add_argument(
        "--max-abs-diff",
        type=float,
        default=None,
        help="Select trades with |diff| <= value for modification",
    )
    parser.add_argument(
        "--min-abs-diff",
        type=float,
        default=None,
        help="Select trades with |diff| >= value for modification",
    )
    args = parser.parse_args()
    if args.apply:
        args.dry_run = False
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
