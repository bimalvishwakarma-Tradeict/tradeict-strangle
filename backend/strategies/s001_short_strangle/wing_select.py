# wing_select.py — Pure strike selection for basket long wings (no DB / network)

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_WING_MODES = frozenset({"points", "delta", "pct_of_premium"})


def normalize_wing_mode(mode: str | None) -> str:
    """Whitelist wing_strike_mode; unknown → 'points'."""
    normalized = str(mode or "points").lower().strip()
    if normalized not in VALID_WING_MODES:
        return "points"
    return normalized


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _call_legs_beyond_short(
    chain: list[dict[str, Any]],
    short_call_strike: float,
) -> list[dict[str, Any]]:
    """Normalize chain rows into call-leg candidates strictly above short strike."""
    short_k = float(short_call_strike)
    out: list[dict[str, Any]] = []
    for row in chain:
        strike = _safe_float(row.get("strike"))
        if strike <= short_k:
            continue
        premium = _safe_float(
            row.get("call_mark_price", row.get("premium", row.get("mark_price")))
        )
        delta = _safe_float(row.get("call_delta", row.get("delta")))
        symbol = str(row.get("call_symbol") or row.get("symbol") or "")
        product_id = int(
            row.get("call_product_id")
            or row.get("product_id")
            or 0
        )
        out.append(
            {
                "strike": strike,
                "symbol": symbol,
                "product_id": product_id,
                "premium": premium,
                "delta": delta,
            }
        )
    out.sort(key=lambda r: r["strike"])
    return out


def _put_legs_beyond_short(
    chain: list[dict[str, Any]],
    short_put_strike: float,
) -> list[dict[str, Any]]:
    """Normalize chain rows into put-leg candidates strictly below short strike."""
    short_k = float(short_put_strike)
    out: list[dict[str, Any]] = []
    for row in chain:
        strike = _safe_float(row.get("strike"))
        if strike >= short_k:
            continue
        premium = _safe_float(
            row.get("put_mark_price", row.get("premium", row.get("mark_price")))
        )
        # Chain stores put_delta as abs; keep abs for band comparisons
        delta = abs(_safe_float(row.get("put_delta", row.get("delta"))))
        symbol = str(row.get("put_symbol") or row.get("symbol") or "")
        product_id = int(
            row.get("put_product_id")
            or row.get("product_id")
            or 0
        )
        out.append(
            {
                "strike": strike,
                "symbol": symbol,
                "product_id": product_id,
                "premium": premium,
                "delta": delta,
            }
        )
    out.sort(key=lambda r: r["strike"])
    return out


def _finish(
    leg: str,
    cand: dict[str, Any],
    *,
    short_strike: float,
    short_premium: float,
    mode: str,
    picked_by: str,
) -> dict[str, Any]:
    gap = abs(float(cand["strike"]) - float(short_strike))
    short_prem = float(short_premium or 0.0)
    wing_prem = float(cand["premium"] or 0.0)
    pct = (wing_prem / short_prem * 100.0) if short_prem > 0 else 0.0
    result = {
        "strike": float(cand["strike"]),
        "symbol": str(cand.get("symbol") or ""),
        "product_id": int(cand.get("product_id") or 0),
        "premium": wing_prem,
        "delta": float(cand.get("delta") or 0.0),
        "picked_by": picked_by,
    }
    logger.info(
        "[WING_SELECT] mode=%s leg=%s short_strike=%.2f wing_strike=%.2f "
        "wing_prem=%.4f delta=%.4f gap_points=%.2f pct_of_short=%.2f "
        "picked_by=%s",
        mode,
        leg,
        float(short_strike),
        result["strike"],
        result["premium"],
        result["delta"],
        gap,
        pct,
        picked_by,
    )
    return result


def _pick_points_call(
    candidates: list[dict[str, Any]],
    short_strike: float,
    points_away: float,
) -> tuple[dict[str, Any] | None, str]:
    if not candidates:
        return None, ""
    target = float(short_strike) + max(0.0, float(points_away))
    at_or_beyond = [c for c in candidates if c["strike"] >= target]
    if at_or_beyond:
        # Nearest at-or-beyond target (= smallest strike >= target)
        pick = min(at_or_beyond, key=lambda c: c["strike"])
        return pick, "points"
    # Chain exhausted before target — farthest OTM available
    pick = max(candidates, key=lambda c: c["strike"])
    logger.warning(
        "[WING_SELECT_CHAIN_END] leg=call requested=%.2f picked=%.2f",
        target,
        pick["strike"],
    )
    return pick, "chain_end"


def _pick_points_put(
    candidates: list[dict[str, Any]],
    short_strike: float,
    points_away: float,
) -> tuple[dict[str, Any] | None, str]:
    if not candidates:
        return None, ""
    target = float(short_strike) - max(0.0, float(points_away))
    at_or_beyond = [c for c in candidates if c["strike"] <= target]
    if at_or_beyond:
        # Nearest at-or-beyond (= largest strike <= target)
        pick = max(at_or_beyond, key=lambda c: c["strike"])
        return pick, "points"
    pick = min(candidates, key=lambda c: c["strike"])
    logger.warning(
        "[WING_SELECT_CHAIN_END] leg=put requested=%.2f picked=%.2f",
        target,
        pick["strike"],
    )
    return pick, "chain_end"


def _pick_delta(
    candidates: list[dict[str, Any]],
    *,
    leg: str,
    delta_min: float,
    delta_max: float,
) -> tuple[dict[str, Any] | None, str]:
    if not candidates:
        return None, ""
    d_min = min(float(delta_min), float(delta_max))
    d_max = max(float(delta_min), float(delta_max))
    mid = (d_min + d_max) / 2.0
    in_band = [
        c
        for c in candidates
        if d_min <= abs(float(c.get("delta") or 0.0)) <= d_max
    ]
    if in_band:
        pick = min(
            in_band,
            key=lambda c: abs(abs(float(c.get("delta") or 0.0)) - mid),
        )
        return pick, "delta_band"
    pick = min(
        candidates,
        key=lambda c: abs(abs(float(c.get("delta") or 0.0)) - mid),
    )
    logger.warning(
        "[WING_SELECT] delta band miss leg=%s band=[%.4f,%.4f] "
        "picked_strike=%.2f picked_delta=%.4f — using nearest |delta|",
        leg,
        d_min,
        d_max,
        pick["strike"],
        abs(float(pick.get("delta") or 0.0)),
    )
    return pick, "delta_nearest"


def _pick_pct(
    candidates: list[dict[str, Any]],
    short_premium: float,
    pct_of_premium: float,
) -> tuple[dict[str, Any] | None, str]:
    if not candidates:
        return None, ""
    target = float(short_premium) * max(0.0, float(pct_of_premium)) / 100.0
    pick = min(
        candidates,
        key=lambda c: abs(float(c.get("premium") or 0.0) - target),
    )
    return pick, "pct_of_premium"


def resolve_wing_strikes(
    *,
    chain: list[dict[str, Any]],
    short_call_strike: float,
    short_put_strike: float,
    short_call_premium: float,
    short_put_premium: float,
    mode: str,
    points_away: float,
    delta_min: float,
    delta_max: float,
    pct_of_premium: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    Pick long far-OTM wing call + put for a short basket.

    Returns (wing_call, wing_put). Either may be None when no OTM strike exists
    beyond the short. Wings are ALWAYS strictly more OTM than the shorts.
    """
    mode_n = normalize_wing_mode(mode)
    call_cands = _call_legs_beyond_short(chain or [], short_call_strike)
    put_cands = _put_legs_beyond_short(chain or [], short_put_strike)

    wing_call: dict[str, Any] | None = None
    wing_put: dict[str, Any] | None = None

    if mode_n == "delta":
        call_pick, call_by = _pick_delta(
            call_cands,
            leg="call",
            delta_min=delta_min,
            delta_max=delta_max,
        )
        put_pick, put_by = _pick_delta(
            put_cands,
            leg="put",
            delta_min=delta_min,
            delta_max=delta_max,
        )
    elif mode_n == "pct_of_premium":
        call_pick, call_by = _pick_pct(
            call_cands, short_call_premium, pct_of_premium
        )
        put_pick, put_by = _pick_pct(
            put_cands, short_put_premium, pct_of_premium
        )
    else:
        call_pick, call_by = _pick_points_call(
            call_cands, short_call_strike, points_away
        )
        put_pick, put_by = _pick_points_put(
            put_cands, short_put_strike, points_away
        )

    if call_pick is not None:
        # Hard invariant: never at-or-inside short
        if float(call_pick["strike"]) > float(short_call_strike):
            wing_call = _finish(
                "call",
                call_pick,
                short_strike=short_call_strike,
                short_premium=short_call_premium,
                mode=mode_n,
                picked_by=call_by,
            )
    if put_pick is not None:
        if float(put_pick["strike"]) < float(short_put_strike):
            wing_put = _finish(
                "put",
                put_pick,
                short_strike=short_put_strike,
                short_premium=short_put_premium,
                mode=mode_n,
                picked_by=put_by,
            )

    return wing_call, wing_put
