# spread_utils.py — Configurable exit-spread % (AUTO from L2 / MANUAL / FALLBACK)

from __future__ import annotations

import logging
from typing import Any

from backend.config import OPTIONS_CONTRACT_VALUE
from backend.core.bot_logger import log_and_buffer
from backend.core.fees import estimate_expected_exit_spread_usd

logger = logging.getLogger(__name__)

DEFAULT_BASKET_EXIT_SPREAD_PCT = 0.5  # matches legacy spread_factor=0.005
DEFAULT_HEDGE_EXIT_SPREAD_PCT = 2.7
DEFAULT_SPREAD_CAP_PCT = 8.0


def _configured_pct(settings: Any, kind: str) -> float:
    kind_l = str(kind or "basket").lower().strip()
    if kind_l == "hedge":
        raw = getattr(settings, "hedge_exit_spread_pct", None)
        default = DEFAULT_HEDGE_EXIT_SPREAD_PCT
    else:
        raw = getattr(settings, "basket_exit_spread_pct", None)
        default = DEFAULT_BASKET_EXIT_SPREAD_PCT
    try:
        pct = float(raw if raw is not None else default)
    except (TypeError, ValueError):
        pct = default
    return max(0.0, pct)


def _spread_cap_pct(settings: Any) -> float:
    try:
        cap = float(
            getattr(settings, "spread_cap_pct", None)
            if getattr(settings, "spread_cap_pct", None) is not None
            else DEFAULT_SPREAD_CAP_PCT
        )
    except (TypeError, ValueError):
        cap = DEFAULT_SPREAD_CAP_PCT
    return max(0.0, cap)


def _spread_mode(settings: Any) -> str:
    mode = str(getattr(settings, "spread_mode", None) or "AUTO").upper().strip()
    if mode not in {"AUTO", "MANUAL"}:
        return "AUTO"
    return mode


async def _l2_bid_ask(client: Any, symbol: str) -> tuple[float, float] | None:
    """Top-of-book bid/ask from L2, or None on failure."""
    if client is None or not symbol:
        return None
    try:
        if hasattr(client, "get_l2_top_of_book"):
            bid, ask = await client.get_l2_top_of_book(str(symbol))
            bid_f = float(bid or 0)
            ask_f = float(ask or 0)
            if bid_f > 0 and ask_f > 0 and ask_f >= bid_f:
                return bid_f, ask_f
            return None
    except Exception as exc:
        logger.debug("L2 fetch failed for %s: %s", symbol, exc)
        return None
    return None


async def get_exit_spread_pct(
    symbol: str,
    settings: Any,
    kind: str,
    *,
    client: Any | None = None,
) -> tuple[float, str]:
    """
    Resolve exit-spread percentage for one symbol.

    Returns (capped_pct, source) where source is AUTO | MANUAL | FALLBACK.
    Cap applies in every mode.
    """
    capped, _raw, source = await get_exit_spread_pct_detail(
        symbol, settings, kind, client=client
    )
    return capped, source


async def get_exit_spread_pct_detail(
    symbol: str,
    settings: Any,
    kind: str,
    *,
    client: Any | None = None,
) -> tuple[float, float, str]:
    """Returns (capped_pct, raw_pct, source)."""
    configured = _configured_pct(settings, kind)
    cap = _spread_cap_pct(settings)
    mode = _spread_mode(settings)

    if mode == "MANUAL":
        raw = configured
        source = "MANUAL"
    else:
        book = await _l2_bid_ask(client, str(symbol or ""))
        if book is None:
            raw = configured
            source = "FALLBACK"
        else:
            bid, ask = book
            mid = (bid + ask) / 2.0
            if mid <= 0:
                raw = configured
                source = "FALLBACK"
            else:
                raw = (ask - bid) / mid * 100.0
                source = "AUTO"

    raw = max(0.0, float(raw))
    capped = min(raw, cap) if cap > 0 else raw
    return float(capped), float(raw), source


async def estimate_and_log_exit_spread_usd(
    *,
    symbol: str,
    offer_price: float,
    quantity: int,
    settings: Any,
    kind: str,
    client: Any | None = None,
    log_id: int = 0,
    contract_value: float | None = None,
) -> float:
    """
    Convert resolved exit-spread % → USD for one leg, and log [SPREAD_EST].

    USD formula unchanged from fees.estimate_expected_exit_spread_usd
    (offer × qty × CV × pct/100).
    """
    capped, raw, source = await get_exit_spread_pct_detail(
        symbol, settings, kind, client=client
    )
    usd = estimate_expected_exit_spread_usd(
        offer_price=float(offer_price or 0),
        quantity=int(quantity or 0),
        contract_value=contract_value,
        spread_factor=float(capped) / 100.0,
    )
    details = {
        "kind": str(kind),
        "symbol": str(symbol or ""),
        "mode": source,
        "raw_pct": round(float(raw), 6),
        "capped_pct": round(float(capped), 6),
        "usd": round(float(usd), 6),
        "summary": (
            f"[SPREAD_EST] kind={kind} symbol={symbol} mode={source} "
            f"raw_pct={round(float(raw), 6)} capped_pct={round(float(capped), 6)} "
            f"usd={round(float(usd), 6)}"
        ),
    }
    try:
        log_and_buffer("SPREAD_EST", int(log_id or 0), details)
    except Exception as exc:
        logger.warning("SPREAD_EST log failed: %s", exc)
    return float(usd)


def exit_spread_usd_from_pct(
    *,
    offer_price: float,
    quantity: int,
    pct: float,
    contract_value: float | None = None,
) -> float:
    """Synchronous USD conversion (same formula as estimate_expected_exit_spread_usd)."""
    cv = float(OPTIONS_CONTRACT_VALUE if contract_value is None else contract_value)
    return estimate_expected_exit_spread_usd(
        offer_price=offer_price,
        quantity=quantity,
        contract_value=cv,
        spread_factor=max(0.0, float(pct or 0.0)) / 100.0,
    )
