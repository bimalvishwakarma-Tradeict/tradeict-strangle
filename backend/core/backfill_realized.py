"""Pure helpers for backfill_trade_realized_pnl (no DB, testable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.delta_client import short_leg_realized_pnl

_EPS = 1e-6


def _positive_float(val: Any) -> float | None:
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def snapshot_unresolved_leg(leg: Any) -> dict[str, Any]:
    """Snapshot a closed bot-managed leg with NULL realized_pnl."""
    return {
        "leg_id": int(getattr(leg, "id", 0) or 0),
        "trade_id": int(getattr(leg, "trade_id", 0) or 0),
        "leg_type": str(getattr(leg, "leg_type", "") or ""),
        "strike": getattr(leg, "strike", None),
        "initial_premium": getattr(leg, "initial_premium", None),
        "exit_premium": getattr(leg, "exit_premium", None),
        "exit_order_id": getattr(leg, "exit_order_id", None),
        "quantity": getattr(leg, "quantity", None),
        "symbol": getattr(leg, "symbol", None),
        "is_long": bool(getattr(leg, "is_long", False)),
    }


def sum_closed_leg_realized(legs: list[Any]) -> tuple[float, list[dict[str, Any]]]:
    """
    Sum realized_pnl for closed bot-managed legs.

    Returns (total_from_resolved_legs, unresolved_leg_snapshots).
    Unresolved legs are excluded from total — this exposes Class B gaps.
    """
    total = 0.0
    unresolved: list[dict[str, Any]] = []
    for leg in legs:
        if not bool(getattr(leg, "is_bot_managed", True)):
            continue
        if str(getattr(leg, "status", "") or "").lower() != "closed":
            continue
        rp = getattr(leg, "realized_pnl", None)
        if rp is None:
            snap = snapshot_unresolved_leg(leg)
            snap["trade_id"] = int(getattr(leg, "trade_id", snap["trade_id"]) or 0)
            unresolved.append(snap)
            continue
        total += float(rp)
    return round(total, 6), unresolved


def classify_unresolved_bucket(snap: dict[str, Any]) -> str:
    """
    B1: entry + exit premiums present — arithmetic repair possible.
    B2: exit missing — needs Delta fills or manual recovery.
    """
    entry = _positive_float(snap.get("initial_premium"))
    exit_px = _positive_float(snap.get("exit_premium"))
    if entry is not None and exit_px is not None:
        return "B1"
    return "B2"


def is_class_a_mismatch(stored: float | None, resolved_total: float) -> bool:
    """Trade total stale vs sum of legs that already have realized_pnl."""
    if stored is None:
        return resolved_total != 0.0 or False
    return abs(float(stored) - float(resolved_total)) >= _EPS


def compute_leg_realized_from_premiums(leg: Any) -> float | None:
    """Recompute gross realized from entry/exit fills (same as book_leg_close)."""
    entry = _positive_float(getattr(leg, "initial_premium", None))
    exit_px = _positive_float(getattr(leg, "exit_premium", None))
    if entry is None or exit_px is None:
        return None
    qty = abs(int(getattr(leg, "quantity", 0) or 0))
    if qty <= 0:
        return None
    from backend.config import OPTIONS_CONTRACT_VALUE

    cv = float(OPTIONS_CONTRACT_VALUE)
    if bool(getattr(leg, "is_long", False)):
        return (exit_px - entry) * qty * cv
    return short_leg_realized_pnl(
        entry_fill=entry,
        exit_fill=exit_px,
        quantity=qty,
    )


def append_pnl_unresolved_note(trade: Any, leg_type: str) -> None:
    note_tag = f"PNL_UNRESOLVED_{leg_type or 'leg'}"
    prior = str(getattr(trade, "notes", None) or "")
    if note_tag not in prior:
        trade.notes = f"{prior};{note_tag}".strip(";") if prior else note_tag


def repair_b1_leg(
    leg: Any,
    *,
    dry_run: bool = True,
) -> float | None:
    """
    Set leg.realized_pnl from entry/exit premiums (Class B1).

    Returns computed realized; does not mutate leg when dry_run=True.
    """
    if getattr(leg, "realized_pnl", None) is not None:
        return float(leg.realized_pnl)
    bucket = classify_unresolved_bucket(snapshot_unresolved_leg(leg))
    if bucket != "B1":
        return None
    computed = compute_leg_realized_from_premiums(leg)
    if computed is None:
        return None
    if not dry_run:
        leg.realized_pnl = float(computed)
    return float(computed)


@dataclass
class TradeBackfillAnalysis:
    trade_id: int
    stored: float | None
    resolved_total: float
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    b1_legs: list[dict[str, Any]] = field(default_factory=list)
    b2_legs: list[dict[str, Any]] = field(default_factory=list)
    class_a: bool = False
    hedge_position_id: int | None = None
    status: str = ""

    @property
    def has_unresolved(self) -> bool:
        return bool(self.unresolved)

    @property
    def false_healthy(self) -> bool:
        """stored matches resolved sum but unresolved legs exist."""
        if not self.unresolved:
            return False
        if self.class_a:
            return False
        if self.stored is None:
            return self.resolved_total == 0.0
        return abs(float(self.stored) - float(self.resolved_total)) < _EPS


def analyze_trade(trade: Any, legs: list[Any]) -> TradeBackfillAnalysis:
    tid = int(getattr(trade, "id", 0) or 0)
    stored = (
        round(float(trade.realized_pnl), 6)
        if getattr(trade, "realized_pnl", None) is not None
        else None
    )
    resolved_total, unresolved = sum_closed_leg_realized(legs)
    b1 = [u for u in unresolved if classify_unresolved_bucket(u) == "B1"]
    b2 = [u for u in unresolved if classify_unresolved_bucket(u) == "B2"]
    hid = getattr(trade, "hedge_position_id", None)
    return TradeBackfillAnalysis(
        trade_id=tid,
        stored=stored,
        resolved_total=resolved_total,
        unresolved=unresolved,
        b1_legs=b1,
        b2_legs=b2,
        class_a=is_class_a_mismatch(stored, resolved_total),
        hedge_position_id=int(hid) if hid is not None else None,
        status=str(getattr(trade, "status", "") or ""),
    )


def analyze_all_trades(trades: list[Any], legs_by_trade: dict[int, list[Any]]) -> list[TradeBackfillAnalysis]:
    out: list[TradeBackfillAnalysis] = []
    for trade in trades:
        tid = int(trade.id)
        out.append(analyze_trade(trade, legs_by_trade.get(tid, [])))
    return out


def bucket_trade_ids(analyses: list[TradeBackfillAnalysis]) -> dict[str, list[int]]:
    class_a: list[int] = []
    b1_trades: list[int] = []
    b2_trades: list[int] = []
    false_healthy: list[int] = []
    for row in analyses:
        if row.class_a:
            class_a.append(row.trade_id)
        if row.b1_legs:
            b1_trades.append(row.trade_id)
        if row.b2_legs:
            b2_trades.append(row.trade_id)
        if row.false_healthy:
            false_healthy.append(row.trade_id)
    return {
        "CLASS_A": sorted(set(class_a)),
        "CLASS_B1": sorted(set(b1_trades)),
        "CLASS_B2": sorted(set(b2_trades)),
        "FALSE_HEALTHY": sorted(set(false_healthy)),
    }
