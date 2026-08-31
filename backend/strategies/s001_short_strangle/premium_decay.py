"""Premium-decay basket exit — pure evaluation (B26, no DB)."""

from __future__ import annotations

from typing import Any


def _leg_entry_basis(leg: Any) -> float:
    try:
        return float(getattr(leg, "initial_premium", 0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _leg_qty(leg: Any) -> int:
    try:
        return max(0, int(getattr(leg, "quantity", 0) or 0))
    except (TypeError, ValueError):
        return 0


def evaluate_premium_decay_exit(
    *,
    call_leg: Any,
    put_leg: Any,
    call_premium: float,
    put_premium: float,
    enabled: bool,
    decay_pct: float,
    mode: str,
    trade_id: int | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    Premium-decay basket exit (B26).

    ``decay_pct`` is the remaining-premium threshold (e.g. 50 = exit when
    premium is at or below 50% of entry). Returns (should_exit, detail_dict).
    """
    detail: dict[str, Any] = {
        "trade_id": trade_id,
        "enabled": bool(enabled),
        "mode": str(mode or "both_legs").lower().strip(),
        "threshold_pct": float(decay_pct),
        "should_exit": False,
        "block_reason": None,
        "legs": {},
    }
    if not enabled:
        detail["block_reason"] = "disabled"
        return False, detail

    try:
        threshold = float(decay_pct)
    except (TypeError, ValueError):
        detail["block_reason"] = "invalid_threshold"
        return False, detail
    if threshold <= 0 or threshold >= 100:
        detail["block_reason"] = "invalid_threshold"
        return False, detail

    call_open = str(getattr(call_leg, "status", "open")).lower() == "open"
    put_open = str(getattr(put_leg, "status", "open")).lower() == "open"
    if not (call_open and put_open):
        detail["block_reason"] = "not_both_legs_open"
        return False, detail

    legs_info: list[dict[str, Any]] = []
    for leg_type, leg, current in (
        ("call", call_leg, float(call_premium)),
        ("put", put_leg, float(put_premium)),
    ):
        entry = _leg_entry_basis(leg)
        qty = _leg_qty(leg)
        leg_detail: dict[str, Any] = {
            "leg_type": leg_type,
            "entry": round(entry, 4),
            "current": round(current, 4),
            "qty": qty,
            "remaining_pct": None,
        }
        if current <= 0:
            detail["block_reason"] = "no_live_premium"
            detail["legs"] = {leg_type: leg_detail}
            return False, detail
        if entry <= 0:
            detail["block_reason"] = "no_entry_basis"
            detail["legs"] = {leg_type: leg_detail}
            return False, detail
        remaining_pct = (current / entry) * 100.0
        leg_detail["remaining_pct"] = round(remaining_pct, 4)
        legs_info.append(leg_detail)

    detail["legs"] = {row["leg_type"]: row for row in legs_info}
    normalized_mode = detail["mode"]
    if normalized_mode not in {"both_legs", "combined"}:
        normalized_mode = "both_legs"
        detail["mode"] = normalized_mode

    if normalized_mode == "both_legs":
        all_at_threshold = all(
            float(row["remaining_pct"]) <= threshold for row in legs_info
        )
        detail["combined_remaining_pct"] = None
        detail["should_exit"] = all_at_threshold
        if not all_at_threshold:
            detail["block_reason"] = "above_threshold"
        return all_at_threshold, detail

    entry_sum = sum(float(row["entry"]) * int(row["qty"]) for row in legs_info)
    current_sum = sum(float(row["current"]) * int(row["qty"]) for row in legs_info)
    combined_remaining = (current_sum / entry_sum) * 100.0 if entry_sum > 0 else 0.0
    detail["combined_remaining_pct"] = round(combined_remaining, 4)
    should_exit = combined_remaining <= threshold
    detail["should_exit"] = should_exit
    if not should_exit:
        detail["block_reason"] = "above_threshold"
    return should_exit, detail
