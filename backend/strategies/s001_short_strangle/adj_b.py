# adj_b.py — Adj B strike selection (roll untested side IN toward spot)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AdjBNoStrikeInsideWing(Exception):
    """Adj B abort: open wing active but no valid short strike inside it."""

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        super().__init__("ADJ_B_NO_STRIKE_INSIDE_WING")


def resolve_adj_b_wing_strike(wing_leg: Any | None) -> float | None:
    """
    Wing strike for Adj B filter only when leg is OPEN and quantity > 0.

    Closed / zero-qty / missing → None (no wing filter; prior behaviour).
    """
    if wing_leg is None:
        return None
    status = str(getattr(wing_leg, "status", "") or "").lower()
    if status != "open":
        return None
    try:
        qty = int(getattr(wing_leg, "quantity", 0) or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return None
    try:
        wk = float(getattr(wing_leg, "strike", 0) or 0)
    except (TypeError, ValueError):
        wk = 0.0
    if wk <= 0:
        return None
    return wk


def is_adj_b_no_strike_inside_wing(
    result: Any,
    wing_strike: float | None,
) -> bool:
    """
    True when failure is specifically: every pre-wing-eligible candidate
    was rejected as at_or_beyond_wing (nothing left inside the wing).
    """
    if wing_strike is None or float(wing_strike) <= 0:
        return False
    if getattr(result, "success", False):
        return False
    considered = list(getattr(result, "candidates_considered", None) or [])
    pre_wing: list[dict[str, Any]] = []
    for c in considered:
        if not isinstance(c, dict):
            continue
        rej = c.get("rejected")
        if rej in ("itm", "premium_not_below_target"):
            continue
        pre_wing.append(c)
    if not pre_wing:
        return False
    return all(c.get("rejected") == "at_or_beyond_wing" for c in pre_wing)


@dataclass
class AdjBCandidate:
    strike: float
    premium: float
    product_id: int | None = None
    symbol: str = ""


@dataclass
class AdjBStrikeResult:
    """Outcome of Adj B strike selection (pure — no I/O)."""

    success: bool
    strike: float | None = None
    premium: float | None = None
    product_id: int | None = None
    symbol: str = ""
    skip_reason: str | None = None
    candidates_considered: list[dict[str, Any]] = field(default_factory=list)
    required_gap: float = 0.0
    other_short_strike: float = 0.0
    atm_strike: float = 0.0
    p_target: float = 0.0
    chosen_why: str = ""


def _infer_strike_step(strikes: list[float]) -> float:
    """Minimum positive spacing between sorted unique strikes."""
    uniq = sorted({float(s) for s in strikes})
    gaps = [uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1) if uniq[i + 1] > uniq[i]]
    if not gaps:
        return 1000.0  # BTC default fallback
    return float(min(gaps))


def _atm_from_spot(spot: float, strikes: list[float]) -> float:
    """Nearest listed strike to spot (ATM reference)."""
    if not strikes:
        return float(spot)
    return float(min(strikes, key=lambda k: abs(float(k) - float(spot))))


def flatten_unified_option_chain(chain: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert Delta unified call/put rows into per-leg rows for select_adj_b_strike."""
    flat: list[dict[str, Any]] = []
    for raw in chain or []:
        if not isinstance(raw, dict):
            continue
        try:
            strike = float(raw.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        # Already-flat rows (option_type set)
        opt = str(
            raw.get("option_type") or raw.get("type") or raw.get("contract_type") or ""
        ).lower()
        if "call" in opt or "put" in opt:
            flat.append(raw)
            continue
        call_mark = 0.0
        for key in ("call_mark_price", "call_bid", "call_ask"):
            try:
                v = float(raw.get(key) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                call_mark = v
                break
        put_mark = 0.0
        for key in ("put_mark_price", "put_bid", "put_ask"):
            try:
                v = float(raw.get(key) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                put_mark = v
                break
        if call_mark > 0:
            flat.append(
                {
                    "option_type": "call",
                    "strike": strike,
                    "mark_price": call_mark,
                    "best_bid": float(raw.get("call_bid") or 0) or call_mark,
                    "product_id": raw.get("call_product_id"),
                    "symbol": str(raw.get("call_symbol") or ""),
                }
            )
        if put_mark > 0:
            flat.append(
                {
                    "option_type": "put",
                    "strike": strike,
                    "mark_price": put_mark,
                    "best_bid": float(raw.get("put_bid") or 0) or put_mark,
                    "product_id": raw.get("put_product_id"),
                    "symbol": str(raw.get("put_symbol") or ""),
                }
            )
    return flat


def select_adj_b_strike(
    *,
    leg_type: str,
    p_target: float,
    chain: list[dict[str, Any]],
    spot: float,
    other_short_strike: float,
    min_short_gap_points: float = 0.0,
    strike_step: float | None = None,
    wing_strike: float | None = None,
) -> AdjBStrikeResult:
    """
    Pick a nearer-OTM strike for the untested side (Adj B).

    Spec (exact):
      1. P_target = tested side's current premium
      2. Same option type; not ITM (call >= ATM, put <= ATM)
      3. Premium STRICTLY less than P_target
      4. Highest premium among survivors (nearest spot)
      5. Gap guard vs other short (+ optional min_short_gap_points);
         if violated, try next further-OUT candidates
      6. If none survive → skip (never force ITM / equal / cross)
      7. Optional open-wing guard: call < wing_strike, put > wing_strike
         (strict). If wing_strike is None, this filter is skipped.
    """
    leg = str(leg_type or "").lower().strip()
    if leg not in ("call", "put"):
        return AdjBStrikeResult(
            success=False,
            skip_reason="invalid_leg_type",
            p_target=float(p_target or 0),
            other_short_strike=float(other_short_strike or 0),
        )

    p_tgt = float(p_target or 0)
    other_k = float(other_short_strike or 0)
    min_gap = max(0.0, float(min_short_gap_points or 0))
    spot_f = float(spot or 0)
    wing_k: float | None = None
    if wing_strike is not None:
        try:
            wk = float(wing_strike)
        except (TypeError, ValueError):
            wk = 0.0
        if wk > 0:
            wing_k = wk

    if p_tgt <= 0 or spot_f <= 0:
        return AdjBStrikeResult(
            success=False,
            skip_reason="invalid_p_target_or_spot",
            p_target=p_tgt,
            other_short_strike=other_k,
        )

    # Normalize chain rows
    rows: list[AdjBCandidate] = []
    for raw in chain or []:
        if not isinstance(raw, dict):
            continue
        opt = str(
            raw.get("option_type")
            or raw.get("type")
            or raw.get("contract_type")
            or ""
        ).lower()
        if "call" in opt:
            row_leg = "call"
        elif "put" in opt:
            row_leg = "put"
        else:
            # Allow caller to pre-filter by leg; missing type → skip
            continue
        if row_leg != leg:
            continue
        try:
            strike = float(raw.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0:
            continue
        prem = None
        for key in ("mark_price", "mark", "best_bid", "bid", "premium", "ask"):
            try:
                v = float(raw.get(key) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                prem = v
                break
        if prem is None or prem <= 0:
            continue
        pid_raw = raw.get("product_id") or raw.get("id")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid = None
        rows.append(
            AdjBCandidate(
                strike=strike,
                premium=float(prem),
                product_id=pid,
                symbol=str(raw.get("symbol") or ""),
            )
        )

    all_strikes = [r.strike for r in rows]
    atm = _atm_from_spot(spot_f, all_strikes) if all_strikes else spot_f
    step = float(strike_step) if strike_step and strike_step > 0 else _infer_strike_step(
        all_strikes
    )
    # One strike step minimum gap between shorts (equal = straddle forbidden)
    required_gap = max(step, min_gap) if min_gap > 0 else step

    considered: list[dict[str, Any]] = []
    pool: list[AdjBCandidate] = []
    for r in rows:
        # Not ITM
        if leg == "call" and r.strike < atm:
            considered.append(
                {
                    "strike": r.strike,
                    "premium": r.premium,
                    "rejected": "itm",
                }
            )
            continue
        if leg == "put" and r.strike > atm:
            considered.append(
                {
                    "strike": r.strike,
                    "premium": r.premium,
                    "rejected": "itm",
                }
            )
            continue
        # Strictly below P_target
        if r.premium >= p_tgt:
            considered.append(
                {
                    "strike": r.strike,
                    "premium": r.premium,
                    "rejected": "premium_not_below_target",
                }
            )
            continue
        # Open-wing guard: stay STRICTLY inside wing (toward ATM)
        if wing_k is not None:
            beyond_wing = (
                (leg == "call" and r.strike >= wing_k - 1e-9)
                or (leg == "put" and r.strike <= wing_k + 1e-9)
            )
            if beyond_wing:
                considered.append(
                    {
                        "strike": r.strike,
                        "premium": r.premium,
                        "rejected": "at_or_beyond_wing",
                        "wing_strike": wing_k,
                    }
                )
                continue
        pool.append(r)
        considered.append(
            {
                "strike": r.strike,
                "premium": r.premium,
                "rejected": None,
            }
        )

    # Highest premium first (= nearest spot among OTM-below-target)
    pool.sort(key=lambda c: (-c.premium, c.strike if leg == "call" else -c.strike))

    def _gap_ok(new_strike: float) -> tuple[bool, str]:
        if leg == "call":
            # Call must sit ABOVE put short (strict); gap >= required
            if new_strike <= other_k:
                return False, "at_or_across_other_short"
            if (new_strike - other_k) < required_gap - 1e-9:
                return False, "gap_too_small"
            return True, "ok"
        # put must sit BELOW call short
        if new_strike >= other_k:
            return False, "at_or_across_other_short"
        if (other_k - new_strike) < required_gap - 1e-9:
            return False, "gap_too_small"
        return True, "ok"

    for cand in pool:
        ok, why = _gap_ok(cand.strike)
        if not ok:
            for row in considered:
                if (
                    row.get("rejected") is None
                    and abs(float(row["strike"]) - cand.strike) < 1e-9
                ):
                    row["rejected"] = why
            continue
        why_txt = (
            f"highest premium {cand.premium:.4f} < P_target {p_tgt:.4f}; "
            f"gap to other short {other_k:g} ok (required {required_gap:g})"
        )
        return AdjBStrikeResult(
            success=True,
            strike=cand.strike,
            premium=cand.premium,
            product_id=cand.product_id,
            symbol=cand.symbol,
            candidates_considered=considered,
            required_gap=required_gap,
            other_short_strike=other_k,
            atm_strike=atm,
            p_target=p_tgt,
            chosen_why=why_txt,
        )

    return AdjBStrikeResult(
        success=False,
        skip_reason="no_valid_strike",
        candidates_considered=considered,
        required_gap=required_gap,
        other_short_strike=other_k,
        atm_strike=atm,
        p_target=p_tgt,
    )
