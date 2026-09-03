# fees.py — Delta India options trading fee estimate + basket fee aggregation
#
# Paid fees MUST come from Delta fill/order commission (already includes GST).
# This module only ESTIMATES fees for future exits / missing backfill display.

from __future__ import annotations

import logging
from typing import Any

from backend.config import (
    GST_RATE,
    OPTION_FEE_RATE,
    OPTIONS_CONTRACT_VALUE,
    PREMIUM_CAP_RATE,
)

logger = logging.getLogger(__name__)


def estimate_option_trading_fee(
    *,
    option_price: float,
    quantity_lots: int,
    btc_index_price: float,
    contract_value: float | None = None,
    fee_rate: float | None = None,
    premium_cap_rate: float | None = None,
    gst_rate: float | None = None,
) -> float:
    """
    Estimate total trading fee (inc. GST) for one options market fill.

    qtyBTC = lots × contract_value
    notionalFee = BTCIndex × qtyBTC × OPTION_FEE_RATE
    premiumCap = optionPrice × qtyBTC × PREMIUM_CAP_RATE
    total = min(notionalFee, premiumCap) × (1 + GST)
    """
    lots = abs(int(quantity_lots or 0))
    px = float(option_price or 0.0)
    index = float(btc_index_price or 0.0)
    cv = float(OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value)
    rate = float(OPTION_FEE_RATE if fee_rate is None else fee_rate)
    cap_rate = float(PREMIUM_CAP_RATE if premium_cap_rate is None else premium_cap_rate)
    gst = float(GST_RATE if gst_rate is None else gst_rate)

    if lots <= 0 or px <= 0 or index <= 0 or cv <= 0:
        return 0.0

    qty_btc = lots * cv
    notional_fee = index * qty_btc * rate
    premium_cap = px * qty_btc * cap_rate
    base = min(notional_fee, premium_cap)
    return float(base * (1.0 + gst))


def leg_fees_paid(leg: Any) -> float:
    """Sum of actual entry + exit fees stored on a leg (never estimated)."""
    entry = float(getattr(leg, "entry_fee_usd", None) or 0.0)
    exit_ = float(getattr(leg, "exit_fee_usd", None) or 0.0)
    return max(0.0, entry) + max(0.0, exit_)


def basket_fees_paid_from_legs(legs: list[Any]) -> float:
    """All actual fees incurred across the basket lifetime."""
    return float(sum(leg_fees_paid(leg) for leg in legs))


def compute_slippage_amount(gross_mtm: float, slippage_pct: float | None) -> float:
    """slippage_amount = abs(gross_mtm) × slippage_pct / 100."""
    pct = float(slippage_pct if slippage_pct is not None else 2.0)
    if pct < 0:
        pct = 0.0
    return abs(float(gross_mtm or 0.0)) * pct / 100.0


def abs_execution_cost_usd(raw_usd: float | None) -> float:
    """
    Spread and slippage are ALWAYS a cost — never a credit.

    BUY and SELL both lose to the bid/ask: never multiply by direction in a way
    that turns a spread into a credit. Callers must subtract this (always ≥ 0).
    """
    return abs(float(raw_usd or 0.0))


def compute_entry_spread_usd(
    *,
    sent_price: float,
    fill_price: float,
    quantity: int,
    is_long: bool = False,
    contract_value: float | None = None,
) -> float:
    """
    Entry execution spread cost in USD (always ≥ 0 — never a credit).

    Short (SELL): adverse when fill < sent → (sent − fill) × qty × CV
    Long  (BUY):  adverse when fill > sent → (fill − sent) × qty × CV

    Magnitude is taken with abs_execution_cost_usd so a lucky fill cannot
    inflate profit via a negative "spread".
    """
    cv = float(OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value)
    qty = abs(int(quantity or 0))
    sent = float(sent_price or 0.0)
    fill = float(fill_price or 0.0)
    if is_long:
        raw = (fill - sent) * qty * cv
    else:
        raw = (sent - fill) * qty * cv
    return abs_execution_cost_usd(raw)


def get_entry_spread_for_sl(trade: Any) -> float:
    """
    Spread currently added back into gross_mtm_for_stoploss.

    This is the spread of the *latest* entry event only — not cumulative
    across adjustments. Never used in net_mtm.
    """
    val = getattr(trade, "entry_spread_for_sl_usd", None)
    if val is None:
        # Pre-migration rows may still only have the legacy column
        val = getattr(trade, "cumulative_entry_spread_usd", 0.0)
    return float(val or 0.0)


def reset_entry_spread_for_sl(
    trade: Any,
    entry_spread_usd: float | None,
    *,
    reason: str,
    leg: str = "",
) -> float:
    """
    SET (do not add) trade.entry_spread_for_sl_usd for the stop-loss add-back.

    Call once per entry event:
      - trade entry: sum of both opening legs' spreads
      - adjustment / conversion: spread(s) of the newly opened leg(s) only
    """
    old_value = get_entry_spread_for_sl(trade)
    new_value = abs_execution_cost_usd(entry_spread_usd)
    trade.entry_spread_for_sl_usd = new_value
    trade_id = int(getattr(trade, "id", 0) or 0)
    logger.info(
        "[ENTRY_SPREAD_RESET] trade_id=%s leg=%s old_value=%.6f "
        "new_value=%.6f reason=%s",
        trade_id,
        leg or "?",
        old_value,
        new_value,
        reason,
    )
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "ENTRY_SPREAD_RESET",
            trade_id,
            {
                "leg": leg or "?",
                "old_value": round(old_value, 6),
                "new_value": round(new_value, 6),
                "reason": reason,
            },
        )
    except Exception:
        pass
    return new_value


def accumulate_entry_spread_on_trade(trade: Any, entry_spread_usd: float | None) -> None:
    """
    DEPRECATED — use reset_entry_spread_for_sl.

    Kept as a thin wrapper that SETs (does not accumulate) so any stray caller
    cannot reintroduce the looser-SL bug.
    """
    reset_entry_spread_for_sl(
        trade,
        entry_spread_usd,
        reason="legacy_accumulate_wrapper",
        leg="?",
    )


def estimate_expected_exit_spread_usd(
    *,
    offer_price: float,
    quantity: int,
    contract_value: float | None = None,
    spread_factor: float = 0.005,
) -> float:
    """Conservative exit-spread cost (always ≥ 0): offer × qty × CV × 0.5%."""
    cv = float(OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value)
    offer = float(offer_price or 0.0)
    qty = abs(int(quantity or 0))
    if offer <= 0 or qty <= 0:
        return 0.0
    return abs_execution_cost_usd(offer * qty * cv * float(spread_factor))


def compute_net_mtm(
    *,
    gross_mtm: float,
    fees_paid: float = 0.0,
    est_exit_fees: float = 0.0,
    slippage_pct: float | None = 2.0,
    expected_exit_spread_usd: float = 0.0,
) -> dict[str, float]:
    """
    Net MTM = Gross − Fees Paid − Est Exit Fees − Slippage − Expected Exit Spread.

    Returns slippage_pct, slippage_amount, expected_exit_spread_usd,
    net_mtm, total_deductions.
    """
    slip_pct = float(slippage_pct if slippage_pct is not None else 2.0)
    if slip_pct < 0:
        slip_pct = 0.0
    fees = abs_execution_cost_usd(fees_paid)
    est_exit = abs_execution_cost_usd(est_exit_fees)
    exit_spread = abs_execution_cost_usd(expected_exit_spread_usd)
    slip = abs_execution_cost_usd(compute_slippage_amount(gross_mtm, slip_pct))
    deductions = fees + est_exit + slip + exit_spread
    net = float(gross_mtm or 0.0) - deductions
    return {
        "slippage_pct": slip_pct,
        "slippage_amount": round(slip, 4),
        "expected_exit_spread_usd": round(exit_spread, 4),
        "total_deductions": round(deductions, 4),
        "net_mtm": round(net, 4),
    }


def build_fee_summary(
    *,
    legs: list[Any],
    open_leg_estimates: dict[int, float] | None = None,
    basket_closed: bool = False,
) -> dict[str, float]:
    """
    Basket fee rollup for API/UI.

    fees_paid: actual Delta commissions stored on legs
    est_exit_fees: sum of estimates for currently open legs (0 if basket closed)
    total_expected_fees: fees_paid + est_exit_fees
    """
    fees_paid = basket_fees_paid_from_legs(legs)
    est_map = open_leg_estimates or {}
    if basket_closed:
        est_exit = 0.0
    else:
        est_exit = 0.0
        for leg in legs:
            if str(getattr(leg, "status", "") or "").lower() != "open":
                continue
            lid = int(getattr(leg, "id", 0) or 0)
            est_exit += float(est_map.get(lid, 0.0))
    total = fees_paid + est_exit
    return {
        "fees_paid": fees_paid,
        "est_exit_fees": est_exit,
        "total_expected_fees": total,
    }
