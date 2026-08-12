"""
earner_webhook.py — Fire-and-forget HTTP notifications to Tradeict Earner backend.

Called by bot_engine after a trade fully closes. Non-fatal: if Earner is
unreachable the bot continues normally and logs a warning.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

EARNER_WEBHOOK_URL = "http://127.0.0.1:5000/api/internal/bot-trade-closed"
EARNER_WEBHOOK_TIMEOUT = 5  # seconds — short, fire-and-forget


async def notify_earner_trade_closed(
    *,
    master_trade_id: int,
    exit_reason: str,
    final_pnl: float,
    slave_accounts: list[dict[str, Any]],
) -> None:
    """
    POST to Earner backend after a master trade closes.

    slave_accounts: list of dicts with keys:
        earner_user_id: str | None
        earner_subscription_id: str | None
        actual_quantity: int
        call_fill_price: float | None
        put_fill_price: float | None

    Only fires for slaves that have earner_user_id set.
    Non-fatal: logs warning on any error.
    """
    earner_slaves = [
        s for s in slave_accounts
        if s.get("earner_user_id")
    ]
    if not earner_slaves:
        return

    try:
        import httpx
        payload = {
            "master_trade_id": master_trade_id,
            "exit_reason": exit_reason,
            "final_pnl": round(float(final_pnl), 4),
            "slaves": earner_slaves,
        }
        async with httpx.AsyncClient(timeout=EARNER_WEBHOOK_TIMEOUT) as client:
            resp = await client.post(EARNER_WEBHOOK_URL, json=payload)
            if resp.status_code == 200:
                logger.info(
                    "[EARNER_WEBHOOK] Notified Earner: trade=%s reason=%s "
                    "pnl=%.4f slaves=%s",
                    master_trade_id,
                    exit_reason,
                    final_pnl,
                    len(earner_slaves),
                )
            else:
                logger.warning(
                    "[EARNER_WEBHOOK] Earner returned %s for trade=%s",
                    resp.status_code,
                    master_trade_id,
                )
    except Exception as exc:
        logger.warning(
            "[EARNER_WEBHOOK] Failed to notify Earner for trade=%s: %s",
            master_trade_id,
            exc,
        )
