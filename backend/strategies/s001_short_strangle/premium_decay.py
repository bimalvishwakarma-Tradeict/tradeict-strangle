"""Premium-decay basket exit — pure evaluation (B26, no DB).

With wings (iron condor): combined mode uses net credit remaining:
  entry_net   = Σ(short_entry × qty) − Σ(wing_entry × qty)
  current_net = Σ(short_now × qty)   − Σ(wing_now × qty)
  remaining_pct = current_net / entry_net × 100

both_legs mode still checks SHORT legs only (wing decay is a loss, not exit signal).
Wings disabled → short-only behaviour unchanged.
"""

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


def _leg_detail(
    *,
    leg_type: str,
    leg: Any,
    current: float,
) -> dict[str, Any]:
    entry = _leg_entry_basis(leg)
    qty = _leg_qty(leg)
    remaining_pct = None
    if entry > 0 and current > 0:
        remaining_pct = round((current / entry) * 100.0, 4)
    return {
        "leg_type": leg_type,
        "entry": round(entry, 4),
        "current": round(current, 4),
        "qty": qty,
        "remaining_pct": remaining_pct,
    }


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
    wing_call_leg: Any | None = None,
    wing_put_leg: Any | None = None,
    wing_call_premium: float | None = None,
    wing_put_premium: float | None = None,
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
        "threshold_pct": float(decay_pct) if decay_pct is not None else 0.0,
        "should_exit": False,
        "block_reason": None,
        "legs": {},
        "wings_active": False,
        "entry_net": None,
        "current_net": None,
        "combined_remaining_pct": None,
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

    wings_active = (
        wing_call_leg is not None
        and wing_put_leg is not None
        and str(getattr(wing_call_leg, "status", "open")).lower() == "open"
        and str(getattr(wing_put_leg, "status", "open")).lower() == "open"
    )
    detail["wings_active"] = wings_active

    # Short-leg diagnostics (always)
    legs_info: list[dict[str, Any]] = []
    for leg_type, leg, current in (
        ("call", call_leg, float(call_premium)),
        ("put", put_leg, float(put_premium)),
    ):
        leg_detail = _leg_detail(leg_type=leg_type, leg=leg, current=current)
        if current <= 0:
            detail["block_reason"] = "no_live_premium"
            detail["legs"] = {leg_type: leg_detail}
            return False, detail
        if float(leg_detail["entry"]) <= 0:
            detail["block_reason"] = "no_entry_basis"
            detail["legs"] = {leg_type: leg_detail}
            return False, detail
        legs_info.append(leg_detail)

    if wings_active:
        wc_px = float(
            wing_call_premium
            if wing_call_premium is not None
            else _leg_entry_basis(wing_call_leg)
        )
        wp_px = float(
            wing_put_premium
            if wing_put_premium is not None
            else _leg_entry_basis(wing_put_leg)
        )
        # Wings may be marked at 0 after a big move — still valid for net formula
        legs_info.append(
            _leg_detail(leg_type="wing_call", leg=wing_call_leg, current=wc_px)
        )
        legs_info.append(
            _leg_detail(leg_type="wing_put", leg=wing_put_leg, current=wp_px)
        )
        for row in legs_info:
            if row["leg_type"].startswith("wing") and float(row["entry"]) <= 0:
                detail["block_reason"] = "no_entry_basis"
                detail["legs"] = {r["leg_type"]: r for r in legs_info}
                return False, detail

    detail["legs"] = {row["leg_type"]: row for row in legs_info}
    normalized_mode = detail["mode"]
    if normalized_mode not in {"both_legs", "combined"}:
        normalized_mode = "both_legs"
        detail["mode"] = normalized_mode

    short_rows = [row for row in legs_info if row["leg_type"] in ("call", "put")]

    if normalized_mode == "both_legs":
        # Wings never participate in both_legs threshold (wing decay = loss)
        if any(int(row["qty"]) <= 0 for row in short_rows):
            detail["block_reason"] = "invalid_qty"
            detail["combined_remaining_pct"] = None
            detail["should_exit"] = False
            return False, detail
        all_at_threshold = all(
            float(row["remaining_pct"] or 0) <= threshold for row in short_rows
        )
        detail["combined_remaining_pct"] = None
        detail["should_exit"] = all_at_threshold
        if not all_at_threshold:
            detail["block_reason"] = "above_threshold"
        return all_at_threshold, detail

    # combined mode
    if wings_active:
        wing_rows = [
            row for row in legs_info if row["leg_type"] in ("wing_call", "wing_put")
        ]
        if any(int(row["qty"]) <= 0 for row in short_rows + wing_rows):
            detail["block_reason"] = "invalid_qty"
            detail["combined_remaining_pct"] = None
            detail["should_exit"] = False
            return False, detail
        entry_net = sum(
            float(row["entry"]) * int(row["qty"]) for row in short_rows
        ) - sum(float(row["entry"]) * int(row["qty"]) for row in wing_rows)
        current_net = sum(
            float(row["current"]) * int(row["qty"]) for row in short_rows
        ) - sum(float(row["current"]) * int(row["qty"]) for row in wing_rows)
        detail["entry_net"] = round(entry_net, 4)
        detail["current_net"] = round(current_net, 4)
        if entry_net <= 0:
            detail["block_reason"] = "no_entry_basis"
            detail["combined_remaining_pct"] = None
            detail["should_exit"] = False
            return False, detail
        # current_net may be negative (big move / wings expensive) — that is
        # large PROFIT for a short-credit basket; exit MUST fire. No guard.
        combined_remaining = (current_net / entry_net) * 100.0
        detail["combined_remaining_pct"] = round(combined_remaining, 4)
        should_exit = combined_remaining <= threshold
        detail["should_exit"] = should_exit
        if not should_exit:
            detail["block_reason"] = "above_threshold"
        return should_exit, detail

    # Short-only combined (wings disabled / not open)
    # Order matches B26: entry_sum guard before invalid_qty (zero qty → no_entry_basis)
    entry_sum = sum(float(row["entry"]) * int(row["qty"]) for row in short_rows)
    if entry_sum <= 0:
        detail["block_reason"] = "no_entry_basis"
        detail["combined_remaining_pct"] = None
        detail["should_exit"] = False
        return False, detail
    if any(int(row["qty"]) <= 0 for row in short_rows):
        detail["block_reason"] = "invalid_qty"
        detail["combined_remaining_pct"] = None
        detail["should_exit"] = False
        return False, detail

    current_sum = sum(float(row["current"]) * int(row["qty"]) for row in short_rows)
    combined_remaining = (current_sum / entry_sum) * 100.0
    detail["combined_remaining_pct"] = round(combined_remaining, 4)
    detail["entry_net"] = round(entry_sum, 4)
    detail["current_net"] = round(current_sum, 4)
    should_exit = combined_remaining <= threshold
    detail["should_exit"] = should_exit
    if not should_exit:
        detail["block_reason"] = "above_threshold"
    return should_exit, detail
