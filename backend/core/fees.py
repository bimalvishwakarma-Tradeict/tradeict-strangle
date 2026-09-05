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


# Max allowed divergence between any two net-MTM producers before audit fires.
MTM_SOURCE_MISMATCH_USD = 0.005


def basket_net_mtm_snapshot(
    *,
    gross_mtm: float,
    fees_paid: float = 0.0,
    est_exit_fees: float = 0.0,
    slippage_pct: float | None = 2.0,
    expected_exit_spread_usd: float = 0.0,
    computed_at: Any | None = None,
    now: Any | None = None,
) -> dict[str, Any]:
    """
    Single source for basket net MTM + full deduction breakdown.

    Wraps compute_net_mtm (formula unchanged). Adds computed_at / stale_seconds
    so every API/UI producer can show freshness without local clock math.
    """
    from datetime import datetime, timezone

    from backend.core.time_utils import get_utc_now

    at = computed_at if computed_at is not None else get_utc_now()
    if isinstance(at, datetime) and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    ref = now if now is not None else get_utc_now()
    if isinstance(ref, datetime) and ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    slip_fields = compute_net_mtm(
        gross_mtm=gross_mtm,
        fees_paid=fees_paid,
        est_exit_fees=est_exit_fees,
        slippage_pct=slippage_pct,
        expected_exit_spread_usd=expected_exit_spread_usd,
    )
    fees = abs_execution_cost_usd(fees_paid)
    est_exit = abs_execution_cost_usd(est_exit_fees)
    stale = 0.0
    if isinstance(at, datetime) and isinstance(ref, datetime):
        stale = max(0.0, (ref - at).total_seconds())

    return {
        "gross_mtm": round(float(gross_mtm or 0.0), 4),
        "fees_paid": round(fees, 4),
        "est_exit_fees": round(est_exit, 4),
        "slippage_pct": float(slip_fields["slippage_pct"]),
        "slippage_amount": float(slip_fields["slippage_amount"]),
        "expected_exit_spread_usd": float(slip_fields["expected_exit_spread_usd"]),
        "total_deductions": float(slip_fields["total_deductions"]),
        "net_mtm": float(slip_fields["net_mtm"]),
        "computed_at": at,
        "computed_at_iso": at.isoformat() if isinstance(at, datetime) else str(at),
        "stale_seconds": round(float(stale), 1),
    }


def audit_mtm_source_mismatch(
    trade_id: int,
    source_a: str,
    value_a: float,
    source_b: str,
    value_b: float,
) -> None:
    """Log WARNING when two net-MTM producers disagree by more than $0.005."""
    diff = abs(float(value_a or 0.0) - float(value_b or 0.0))
    if diff <= MTM_SOURCE_MISMATCH_USD:
        return
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(
            "MTM_SOURCE_MISMATCH",
            int(trade_id),
            {
                "trade_id": int(trade_id),
                "source_a": str(source_a),
                "value_a": round(float(value_a or 0.0), 6),
                "source_b": str(source_b),
                "value_b": round(float(value_b or 0.0), 6),
                "diff": round(diff, 6),
            },
        )
    except Exception:
        logger.warning(
            "[MTM_SOURCE_MISMATCH] trade_id=%s source_a=%s value_a=%s "
            "source_b=%s value_b=%s diff=%s",
            trade_id,
            source_a,
            value_a,
            source_b,
            value_b,
            round(diff, 6),
        )


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
