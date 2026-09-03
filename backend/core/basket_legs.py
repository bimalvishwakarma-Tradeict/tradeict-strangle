# basket_legs.py — Single source of truth for short + wing legs on a trade
#
# Never assume only call_leg / put_leg. Wings (wing_call / wing_put) are first-class.

from __future__ import annotations

from typing import Any


SHORT_KEYS = ("short_call", "short_put")
WING_KEYS = ("wing_call", "wing_put")
ALL_KEYS = SHORT_KEYS + WING_KEYS


class BasketLegs(dict):
    """
    Mapping of role → leg (or None).

    Keys: short_call, short_put, wing_call, wing_put
    """

    def open_legs(self) -> list[Any]:
        out: list[Any] = []
        for key in ALL_KEYS:
            leg = self.get(key)
            if leg is None:
                continue
            if str(getattr(leg, "status", "open") or "open").lower() == "open":
                out.append(leg)
        return out

    def short_legs(self) -> list[Any]:
        out: list[Any] = []
        for key in SHORT_KEYS:
            leg = self.get(key)
            if leg is not None:
                out.append(leg)
        return out

    def wing_legs(self) -> list[Any]:
        out: list[Any] = []
        for key in WING_KEYS:
            leg = self.get(key)
            if leg is not None:
                out.append(leg)
        return out

    def open_short_legs(self) -> list[Any]:
        return [
            leg
            for leg in self.short_legs()
            if str(getattr(leg, "status", "open") or "open").lower() == "open"
        ]

    def open_wing_legs(self) -> list[Any]:
        return [
            leg
            for leg in self.wing_legs()
            if str(getattr(leg, "status", "open") or "open").lower() == "open"
        ]

    def wings_enabled(self) -> bool:
        return any(self.get(k) is not None for k in WING_KEYS)

    def wings_open(self) -> bool:
        return bool(self.open_wing_legs())


def _pick_latest(candidates: list[Any]) -> Any | None:
    if not candidates:
        return None
    open_ones = [
        leg
        for leg in candidates
        if str(getattr(leg, "status", "") or "").lower() == "open"
    ]
    pool = open_ones or candidates
    try:
        return max(pool, key=lambda x: int(getattr(x, "id", 0) or 0))
    except (TypeError, ValueError):
        return pool[-1]


def classify_legs(legs: list[Any] | None) -> BasketLegs:
    """Build BasketLegs from an arbitrary leg list (no DB)."""
    result = BasketLegs(
        short_call=None,
        short_put=None,
        wing_call=None,
        wing_put=None,
    )
    if not legs:
        return result

    buckets: dict[str, list[Any]] = {
        "short_call": [],
        "short_put": [],
        "wing_call": [],
        "wing_put": [],
    }
    for leg in legs:
        lt = str(getattr(leg, "leg_type", "") or "").lower()
        is_long = bool(getattr(leg, "is_long", False))
        if lt == "wing_call":
            buckets["wing_call"].append(leg)
        elif lt == "wing_put":
            buckets["wing_put"].append(leg)
        elif lt == "call" and not is_long:
            buckets["short_call"].append(leg)
        elif lt == "put" and not is_long:
            buckets["short_put"].append(leg)

    result["short_call"] = _pick_latest(buckets["short_call"])
    result["short_put"] = _pick_latest(buckets["short_put"])
    result["wing_call"] = _pick_latest(buckets["wing_call"])
    result["wing_put"] = _pick_latest(buckets["wing_put"])
    return result


def basket_legs(
    trade: Any,
    db: Any = None,
    legs: list[Any] | None = None,
) -> BasketLegs:
    """
    Resolve short_call / short_put / wing_call / wing_put for a trade.

    Prefer ``legs`` if provided; else query DB by trade_id; else empty.
    """
    if legs is not None:
        return classify_legs(list(legs))

    if db is not None and trade is not None:
        try:
            from backend.models import Leg

            tid = int(getattr(trade, "id", 0) or 0)
            if tid > 0:
                rows = (
                    db.query(Leg)
                    .filter(
                        Leg.trade_id == tid,
                        Leg.is_bot_managed.is_(True),
                    )
                    .all()
                )
                return classify_legs(list(rows))
        except Exception:
            pass

    # Last resort: trade.legs relationship
    try:
        rel = getattr(trade, "legs", None)
        if rel is not None:
            return classify_legs(list(rel))
    except Exception:
        pass

    return BasketLegs(
        short_call=None,
        short_put=None,
        wing_call=None,
        wing_put=None,
    )


def compute_net_credit_points(
    *,
    short_call_premium: float,
    short_put_premium: float,
    short_qty: int,
    wing_call_premium: float = 0.0,
    wing_put_premium: float = 0.0,
    wing_qty: int | None = None,
) -> float:
    """
    Net credit in premium points (not USD):
      Σ(short × qty) − Σ(wing × qty)
    """
    sq = abs(int(short_qty or 0))
    wq = abs(int(wing_qty if wing_qty is not None else short_qty) or 0)
    shorts = (float(short_call_premium or 0.0) + float(short_put_premium or 0.0)) * sq
    wings = (float(wing_call_premium or 0.0) + float(wing_put_premium or 0.0)) * wq
    return shorts - wings


def compute_net_credit_usd(
    *,
    short_call_premium: float,
    short_put_premium: float,
    short_qty: int,
    wing_call_premium: float = 0.0,
    wing_put_premium: float = 0.0,
    wing_qty: int | None = None,
    contract_value: float | None = None,
) -> float:
    """Net credit in USD = net points × contract_value."""
    from backend.config import OPTIONS_CONTRACT_VALUE

    cv = float(
        OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value
    )
    return compute_net_credit_points(
        short_call_premium=short_call_premium,
        short_put_premium=short_put_premium,
        short_qty=short_qty,
        wing_call_premium=wing_call_premium,
        wing_put_premium=wing_put_premium,
        wing_qty=wing_qty,
    ) * cv
