# fees_sim.py — Delta India options fee model for backtests (S002+)
#
# Values copied from backend/config.py + formula from backend/core/fees.py.
# Do NOT import from backend/ (backtest must stay standalone).
#
# NOTE: Live estimate_option_trading_fee also multiplies by (1 + GST_RATE).
# This module intentionally uses the base min(notional, premium_cap) only,
# matching the S002 backtest fee contract requested for phase 2B.

from __future__ import annotations

# Copied from backend/config.py (do not import)
OPTIONS_CONTRACT_VALUE = 0.001
OPTION_FEE_RATE = 0.00010  # 0.010% of notional
PREMIUM_CAP_RATE = 0.035  # 3.5% of premium


def estimate_option_fee(
    *,
    premium: float,
    qty_lots: int,
    btc_index: float,
    contract_value: float = OPTIONS_CONTRACT_VALUE,
    fee_rate: float = OPTION_FEE_RATE,
    premium_cap_rate: float = PREMIUM_CAP_RATE,
) -> float:
    """
    Fee for one options fill (base, no GST).

    notional_fee = btc_index * qty_lots * contract_value * fee_rate
    cap          = premium * qty_lots * contract_value * premium_cap_rate
    fee          = min(notional_fee, cap)
    """
    lots = abs(int(qty_lots or 0))
    px = float(premium or 0.0)
    index = float(btc_index or 0.0)
    cv = float(contract_value)
    rate = float(fee_rate)
    cap_rate = float(premium_cap_rate)

    if lots <= 0 or px <= 0 or index <= 0 or cv <= 0:
        return 0.0

    notional_fee = index * lots * cv * rate
    cap = px * lots * cv * cap_rate
    return float(min(notional_fee, cap))
