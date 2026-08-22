"""
earner_webhook.py — Fire-and-forget HTTP notifications to Tradeict Earner backend.

Called by bot_engine after a trade fully closes. Non-fatal: if Earner is
unreachable the bot continues normally and logs a warning.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from backend.config import BOT_WEBHOOK_SECRET

logger = logging.getLogger(__name__)

EARNER_WEBHOOK_URL = "http://127.0.0.1:5000/api/internal/bot-trade-closed"
EARNER_WEBHOOK_TIMEOUT = 5  # seconds — short, fire-and-forget

# Allowed slave keys — ids / qty / names only (never money or prices).
_SLAVE_PAYLOAD_KEYS = (
    "earner_user_id",
    "earner_subscription_id",
    "actual_quantity",
    "slave_account_id",
    "slave_name",
)


def _slave_id_payload(slave: dict[str, Any]) -> dict[str, Any]:
    """Copy identity fields only; drop any price/P&L keys."""
    out: dict[str, Any] = {}
    for key in _SLAVE_PAYLOAD_KEYS:
        if key in slave and slave[key] is not None:
            out[key] = slave[key]
    return out


async def notify_earner_trade_closed(
    *,
    master_trade_id: int,
    exit_reason: str,
    slave_accounts: list[dict[str, Any]],
) -> None:
    """
    POST to Earner backend after a master trade closes.

    slave_accounts: list of dicts with identity keys only:
        earner_user_id, earner_subscription_id, actual_quantity,
        slave_account_id, slave_name

    Money fields (final_pnl, fill prices, etc.) are never sent — the earner
    computes P&L from the customer's Delta wallet ledger.

    Only fires for slaves that have earner_user_id set.
    Non-fatal: logs warning on any error.
    """
    earner_slaves = [
        _slave_id_payload(s)
        for s in slave_accounts
        if s.get("earner_user_id")
    ]
    if not earner_slaves:
        return

    secret = str(BOT_WEBHOOK_SECRET or "").strip()
    if not secret:
        logger.warning(
            "[EARNER_WEBHOOK] BOT_WEBHOOK_SECRET empty — skipping post "
            "for trade=%s",
            master_trade_id,
        )
        return

    try:
        import httpx

        # IDs and timestamps only. P&L is NEVER sent from here -- the earner
        # computes it from the customer's own Delta wallet ledger.
        # See routes_structures.py for the same rule.
        payload = {
            "master_trade_id": master_trade_id,
            "exit_reason": exit_reason,
            "slaves": earner_slaves,
        }
        # Serialize EXACTLY once — same bytes for HMAC and POST body.
        # Never use json= on the client (re-serialize would break the signature).
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(
            secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "x-internal-signature": sig,
        }
        async with httpx.AsyncClient(timeout=EARNER_WEBHOOK_TIMEOUT) as client:
            resp = await client.post(
                EARNER_WEBHOOK_URL,
                content=body_bytes,
                headers=headers,
            )
            if resp.status_code == 200:
                logger.info(
                    "[EARNER_WEBHOOK] Notified Earner: trade=%s reason=%s "
                    "slaves=%s",
                    master_trade_id,
                    exit_reason,
                    len(earner_slaves),
                )
            elif resp.status_code == 401:
                logger.error(
                    "earner webhook rejected: signature mismatch -- "
                    "check BOT_WEBHOOK_SECRET"
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
