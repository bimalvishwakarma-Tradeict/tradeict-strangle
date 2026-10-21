# entry_slip.py — Note entry fill vs first mark (display only; already inside gross)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.time_utils import get_utc_now
from backend.core.trade_side import is_long_trade

ENTRY_SLIP_WINDOW_SECONDS = 60.0


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def contract_notional_usd(premium: float, quantity: int) -> float:
    return abs(float(premium or 0.0)) * abs(int(quantity or 0)) * float(
        OPTIONS_CONTRACT_VALUE
    )


def compute_entry_slip_usd(leg_pnl: float) -> float:
    return max(0.0, -float(leg_pnl))


def compute_entry_slip_ratio(
    entry_slip_usd: float,
    entry_premium: float,
    quantity: int,
) -> float | None:
    denom = contract_notional_usd(entry_premium, quantity)
    if denom <= 0:
        return None
    return float(entry_slip_usd) / denom


def compute_slip_now_usd(
    entry_slip_ratio: float | None,
    current_premium: float,
    quantity: int,
) -> float | None:
    if entry_slip_ratio is None:
        return None
    return (
        float(entry_slip_ratio)
        * abs(float(current_premium or 0.0))
        * abs(int(quantity or 0))
        * float(OPTIONS_CONTRACT_VALUE)
    )


def maybe_note_entry_slip(
    leg: Any,
    leg_pnl: float,
    *,
    db: Any | None = None,
    now: datetime | None = None,
) -> None:
    """Persist entry slip once from the first panel-style leg PnL within 60s of fill."""
    if getattr(leg, "entry_slip_usd", None) is not None:
        return
    entry_time = _aware_utc(getattr(leg, "entry_time", None))
    if entry_time is None:
        return
    stamp = _aware_utc(now) or get_utc_now()
    age = (stamp - entry_time).total_seconds()
    if age < 0 or age > ENTRY_SLIP_WINDOW_SECONDS:
        return
    slip_usd = compute_entry_slip_usd(leg_pnl)
    qty = abs(int(getattr(leg, "quantity", 0) or 0))
    entry_px = float(getattr(leg, "initial_premium", 0) or 0)
    ratio = compute_entry_slip_ratio(slip_usd, entry_px, qty)
    if db is not None and getattr(leg, "id", None) is not None:
        from backend.models import Leg

        row = db.query(Leg).filter(Leg.id == int(leg.id)).first()
        if row is None:
            return
        if getattr(row, "entry_slip_usd", None) is not None:
            leg.entry_slip_usd = row.entry_slip_usd
            leg.entry_slip_ratio = row.entry_slip_ratio
            return
        row.entry_slip_usd = slip_usd
        row.entry_slip_ratio = ratio
        db.commit()
    leg.entry_slip_usd = slip_usd
    leg.entry_slip_ratio = ratio


def long_net_mtm_kwargs() -> dict[str, float]:
    """Long net = gross − fees_paid − est_exit_fees (spread already in bid/offer gross)."""
    return {"slippage_pct": 0.0, "expected_exit_spread_usd": 0.0}


def build_live_progress_fields(
    trade: Any,
    *,
    gross: float,
    net: float,
    snaps: list[dict[str, Any]],
) -> dict[str, Any]:
    lots = 0
    for snap in snaps:
        if not snap:
            continue
        lots = max(lots, abs(int(snap.get("quantity") or 0)))
    slip_parts: list[float] = []
    entry_parts: list[float] = []
    for snap in snaps:
        if not snap:
            continue
        usd = snap.get("entry_slip_usd")
        if usd is not None:
            entry_parts.append(float(usd))
        now_part = compute_slip_now_usd(
            snap.get("entry_slip_ratio"),
            float(snap.get("current_premium") or 0.0),
            int(snap.get("quantity") or 0),
        )
        if now_part is not None:
            slip_parts.append(float(now_part))
    entry_total: float | None = round(sum(entry_parts), 6) if entry_parts else None
    slip_now: float | None = round(sum(slip_parts), 6) if slip_parts else None
    market_move: float | None = None
    if slip_now is not None:
        market_move = round(float(gross) - (-float(slip_now)), 4)
    if is_long_trade(trade):
        target_usd = float(
            getattr(trade, "target_usd", None)
            or getattr(trade, "profit_target_usd", None)
            or 0.0
        )
        sl_usd = float(
            getattr(trade, "sl_usd", None)
            or getattr(trade, "stoploss_usd", None)
            or 0.0
        )
    else:
        target_usd = float(getattr(trade, "profit_target_usd", None) or 0.0)
        sl_usd = float(getattr(trade, "stoploss_usd", None) or 0.0)
    trail_level = getattr(trade, "trail_sl_level", None)
    if trail_level is not None:
        sl_distance = round(float(gross) - float(trail_level), 4)
    else:
        sl_distance = round(float(gross) + float(sl_usd or 0.0), 4)
    return {
        "lots": lots,
        "gross": round(float(gross), 4),
        "net": round(float(net), 4),
        "entry_slip_usd": entry_total,
        "slip_now": slip_now,
        "market_move": market_move,
        "target_usd": target_usd,
        "sl_usd": sl_usd,
        "target_remaining": round(float(target_usd) - float(net), 4),
        "sl_distance": sl_distance,
    }
