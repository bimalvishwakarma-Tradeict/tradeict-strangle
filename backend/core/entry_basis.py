"""Pure entry-premium helpers (no I/O, no ORM)."""

from __future__ import annotations


def blend_entry_premium(
    *,
    old_entry: float,
    old_qty: int,
    extra_fill: float,
    extra_qty: int,
) -> float:
    """
    Qty-weighted average entry after topping up an open leg (B25 fix).

    Returns old_entry unchanged when inputs are invalid so a bad fill never
    corrupts the stored entry basis.
    """
    try:
        entry = float(old_entry)
        fill = float(extra_fill)
        oq = int(old_qty)
        eq = int(extra_qty)
    except (TypeError, ValueError):
        return float(old_entry or 0.0)
    if entry <= 0 or fill <= 0 or oq <= 0 or eq <= 0:
        return entry if entry > 0 else 0.0
    total_qty = oq + eq
    if total_qty <= 0:
        return entry
    return (entry * oq + fill * eq) / float(total_qty)
