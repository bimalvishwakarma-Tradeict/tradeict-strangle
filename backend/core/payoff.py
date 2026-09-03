# payoff.py — Short strangle / iron-condor expiry payoff (premium points + USD)
#
# Expiry PnL per lot (premium points), wings optional:
#   pnl(S) = net_credit
#            − max(0, S − short_call_K)
#            + max(0, S − wing_call_K)
#            − max(0, short_put_K − S)
#            + max(0, wing_put_K − S)
#   USD = pnl(S) × qty × contract_value
#
# Wings OFF → wing terms omitted → classic short strangle.

from __future__ import annotations

from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE


def net_credit_points(
    *,
    short_call_premium: float,
    short_put_premium: float,
    wing_call_premium: float | None = None,
    wing_put_premium: float | None = None,
) -> float:
    credit = float(short_call_premium or 0.0) + float(short_put_premium or 0.0)
    if wing_call_premium is not None:
        credit -= float(wing_call_premium or 0.0)
    if wing_put_premium is not None:
        credit -= float(wing_put_premium or 0.0)
    return credit


def expiry_pnl_points(
    spot: float,
    *,
    short_call_strike: float,
    short_put_strike: float,
    net_credit: float,
    wing_call_strike: float | None = None,
    wing_put_strike: float | None = None,
) -> float:
    """Expiry P&L in premium points for one lot at underlying ``spot``."""
    s = float(spot)
    pnl = float(net_credit)
    pnl -= max(0.0, s - float(short_call_strike))
    pnl -= max(0.0, float(short_put_strike) - s)
    if wing_call_strike is not None:
        pnl += max(0.0, s - float(wing_call_strike))
    if wing_put_strike is not None:
        pnl += max(0.0, float(wing_put_strike) - s)
    return pnl


def expiry_pnl_usd(
    spot: float,
    *,
    short_call_strike: float,
    short_put_strike: float,
    net_credit: float,
    quantity: int,
    wing_call_strike: float | None = None,
    wing_put_strike: float | None = None,
    contract_value: float | None = None,
) -> float:
    cv = float(
        OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value
    )
    qty = abs(int(quantity or 0))
    pts = expiry_pnl_points(
        spot,
        short_call_strike=short_call_strike,
        short_put_strike=short_put_strike,
        net_credit=net_credit,
        wing_call_strike=wing_call_strike,
        wing_put_strike=wing_put_strike,
    )
    return pts * qty * cv


def breakevens(
    *,
    short_call_strike: float,
    short_put_strike: float,
    net_credit_per_lot: float,
) -> tuple[float, float]:
    """upper = short_call_K + net_credit; lower = short_put_K − net_credit."""
    nc = float(net_credit_per_lot)
    return (
        float(short_call_strike) + nc,
        float(short_put_strike) - nc,
    )


def build_payoff_curve(
    *,
    current_price: float,
    short_call_strike: float,
    short_put_strike: float,
    short_call_premium: float,
    short_put_premium: float,
    quantity: int,
    wing_call_strike: float | None = None,
    wing_put_strike: float | None = None,
    wing_call_premium: float | None = None,
    wing_put_premium: float | None = None,
    contract_value: float | None = None,
    range_pct: float = 0.20,
    points: int = 101,
) -> dict[str, Any]:
    """
    Spot ± range_pct curve in USD, plus summary metrics.

    When wing strikes are omitted, behaves as short strangle (unlimited loss).
    """
    from backend.core.basket_legs import compute_max_loss_usd

    cv = float(
        OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value
    )
    qty = max(1, abs(int(quantity or 1)))
    spot = float(current_price)
    if spot <= 0:
        raise ValueError("current_price must be > 0")

    wings_on = wing_call_strike is not None and wing_put_strike is not None
    nc = net_credit_points(
        short_call_premium=short_call_premium,
        short_put_premium=short_put_premium,
        wing_call_premium=wing_call_premium if wings_on else None,
        wing_put_premium=wing_put_premium if wings_on else None,
    )
    be_upper, be_lower = breakevens(
        short_call_strike=short_call_strike,
        short_put_strike=short_put_strike,
        net_credit_per_lot=nc,
    )
    max_profit_usd = nc * qty * cv
    max_loss_usd: float | None = None
    if wings_on:
        max_loss_usd = compute_max_loss_usd(
            short_call_strike=float(short_call_strike),
            short_put_strike=float(short_put_strike),
            wing_call_strike=float(wing_call_strike),
            wing_put_strike=float(wing_put_strike),
            net_credit_usd=max_profit_usd,
            quantity=qty,
            contract_value=cv,
        )

    price_min = spot * (1.0 - float(range_pct))
    price_max = spot * (1.0 + float(range_pct))
    n = max(2, int(points))
    step = (price_max - price_min) / (n - 1)

    price_points: list[float] = []
    expiry_pnl: list[float] = []
    for i in range(n):
        p = price_min + step * i
        usd = expiry_pnl_usd(
            p,
            short_call_strike=short_call_strike,
            short_put_strike=short_put_strike,
            net_credit=nc,
            quantity=qty,
            wing_call_strike=wing_call_strike if wings_on else None,
            wing_put_strike=wing_put_strike if wings_on else None,
            contract_value=cv,
        )
        price_points.append(round(p, 4))
        expiry_pnl.append(round(usd, 6))

    risk_reward: float | None = None
    if (
        max_loss_usd is not None
        and max_loss_usd > 0
        and max_profit_usd > 0
    ):
        risk_reward = round(max_loss_usd / max_profit_usd, 4)

    return {
        "price_points": price_points,
        "expiry_pnl": expiry_pnl,
        "breakeven_upper": round(be_upper, 4),
        "breakeven_lower": round(be_lower, 4),
        "net_credit_points": round(nc, 4),
        "max_profit_usd": round(max_profit_usd, 6),
        "max_loss_usd": (
            round(max_loss_usd, 6) if max_loss_usd is not None else None
        ),
        "risk_reward": risk_reward,
        "wings_on": wings_on,
        "quantity": qty,
        "contract_value": cv,
    }
