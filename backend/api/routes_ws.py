# routes_ws.py — WebSocket endpoints for trades + live option chain

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from backend.core.chain_utils import annotate_atm
from backend.core.delta_client import DeltaAPIError, DeltaClient
from backend.core.delta_ws import DeltaWebSocket
from backend.core.encryption import decrypt
from backend.core.ws_manager import ws_manager
from backend.database import SessionLocal
from backend.engine.bot_engine import bot_engine
from backend.models import Account
from backend.strategies.s001_short_strangle.config import (
    SUPPORTED_UNDERLYINGS,
    UNDERLYING_SYMBOLS,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

WS_PING_INTERVAL_SECONDS = 20.0
REST_POLL_INTERVAL_SECONDS = 3.0
DELTA_WS_CONNECT_TIMEOUT = 5.0


def _resolve_underlying_symbol(underlying: str) -> str:
    key = underlying.upper().strip()
    if key not in SUPPORTED_UNDERLYINGS and key not in UNDERLYING_SYMBOLS:
        raise ValueError(f"Unsupported underlying '{underlying}'")
    return UNDERLYING_SYMBOLS.get(key, key)


def _resolve_product_underlying(underlying: str) -> str:
    mapped = _resolve_underlying_symbol(underlying)
    if mapped.endswith("USD") and len(mapped) > 3:
        return mapped[:-3]
    return mapped


def _build_delta_client(db: Session) -> DeltaClient:
    account = (
        db.query(Account)
        .filter(Account.is_active.is_(True))
        .order_by(Account.id.asc())
        .first()
    )
    if account is None:
        raise RuntimeError("No account connected. Please add API keys in Settings.")
    return DeltaClient(
        decrypt(account.api_key_encrypted),
        decrypt(account.api_secret_encrypted),
    )


async def _safe_send(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send JSON; return False if client already gone."""
    try:
        await websocket.send_json(payload)
        return True
    except Exception:
        return False


@router.websocket("/ws/trades")
async def websocket_trades(websocket: WebSocket) -> None:
    """
    Live trade feed.

    On connect: send INITIAL_STATE with all active trades.
    Every 20s without client message: send ping to keep connection alive.
    Frontend may ignore ping (no pong required).
    """
    await ws_manager.connect(websocket)
    try:
        await ws_manager.send_personal(websocket, bot_engine.get_initial_state_payload())
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=WS_PING_INTERVAL_SECONDS,
                )
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("type") in {"pong", "ping"}:
                    continue
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as exc:
        logger.error("WebSocket error: %s", exc, exc_info=True)
        ws_manager.disconnect(websocket)


async def _stream_delta_ticks(
    websocket: WebSocket,
    delta_ws: DeltaWebSocket,
    price_symbol: str,
) -> None:
    """Forward Delta ticker messages to the frontend client."""

    async def on_tick(symbol: str, data: dict[str, Any]) -> None:
        if symbol == price_symbol:
            price = float(data.get("mark_price") or 0.0)
            if price <= 0:
                return
            await _safe_send(
                websocket,
                {"type": "PRICE_UPDATE", "symbol": symbol, "price": price},
            )
            return
        await _safe_send(
            websocket,
            {
                "type": "TICK_UPDATE",
                "symbol": symbol,
                "mark_price": float(data.get("mark_price") or 0.0),
                "bid": float(data.get("bid") or 0.0),
                "ask": float(data.get("ask") or 0.0),
                "delta": float(data.get("delta") or 0.0),
            },
        )

    async def client_watch() -> None:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=WS_PING_INTERVAL_SECONDS,
                )
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("type") in {"ping", "pong"}:
                    continue
            except asyncio.TimeoutError:
                if not await _safe_send(websocket, {"type": "ping"}):
                    break

    listen_task = asyncio.create_task(delta_ws.listen(on_tick))
    watch_task = asyncio.create_task(client_watch())
    done, pending = await asyncio.wait(
        {listen_task, watch_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
            logger.error("option-chain live tick task error: %s", exc, exc_info=exc)


async def _poll_rest_fallback(
    websocket: WebSocket,
    client: DeltaClient,
    product_symbol: str,
    price_symbol: str,
    expiry: str,
) -> None:
    """
    Poll REST every few seconds and push TICK_UPDATE / PRICE_UPDATE.

    Used when Delta public WS cannot be established.
    """
    logger.info("REST poll fallback active (every %ss)", REST_POLL_INTERVAL_SECONDS)
    while True:
        # Detect client disconnect without blocking forever
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=0.01)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "ping":
                await _safe_send(websocket, {"type": "pong"})
        except asyncio.TimeoutError:
            pass
        except WebSocketDisconnect:
            raise

        try:
            fresh_chain = await client.get_option_chain(product_symbol, expiry)
            current_price = await client.get_underlying_price(price_symbol)
        except Exception as exc:
            logger.warning("REST poll fetch failed: %s", exc)
            await asyncio.sleep(REST_POLL_INTERVAL_SECONDS)
            continue

        annotate_atm(fresh_chain, float(current_price))
        for row in fresh_chain:
            ok = await _safe_send(
                websocket,
                {
                    "type": "TICK_UPDATE",
                    "symbol": row["call_symbol"],
                    "mark_price": row["call_mark_price"],
                    "bid": row["call_bid"],
                    "ask": row["call_ask"],
                    "delta": row["call_delta"],
                },
            )
            if not ok:
                return
            ok = await _safe_send(
                websocket,
                {
                    "type": "TICK_UPDATE",
                    "symbol": row["put_symbol"],
                    "mark_price": row["put_mark_price"],
                    "bid": row["put_bid"],
                    "ask": row["put_ask"],
                    "delta": row["put_delta"],
                },
            )
            if not ok:
                return

        if not await _safe_send(
            websocket,
            {
                "type": "PRICE_UPDATE",
                "symbol": price_symbol,
                "price": float(current_price),
            },
        ):
            return

        await asyncio.sleep(REST_POLL_INTERVAL_SECONDS)


@router.websocket("/ws/option-chain")
async def websocket_option_chain(
    websocket: WebSocket,
    underlying: str = Query(..., description="BTC / ETH / XAU"),
    expiry: str = Query(..., description="YYYY-MM-DD"),
) -> None:
    """
    Live option chain feed.

    1) REST snapshot with ATM flags (always)
    2) Delta Exchange WS ticks when available
    3) REST poll fallback every 3s if Delta WS fails
    """
    await websocket.accept()
    logger.info("WS option chain request: %s %s", underlying, expiry)

    delta_ws: DeltaWebSocket | None = None
    client: DeltaClient | None = None
    db: Session | None = None

    try:
        try:
            price_symbol = _resolve_underlying_symbol(underlying)
            product_symbol = _resolve_product_underlying(underlying)
        except ValueError as exc:
            logger.error("Invalid underlying for option-chain WS: %s", exc)
            await _safe_send(websocket, {"type": "ERROR", "message": str(exc)})
            await websocket.close(code=1008)
            return

        db = SessionLocal()
        try:
            client = _build_delta_client(db)
        except RuntimeError as exc:
            logger.error("Option-chain WS account error: %s", exc)
            await _safe_send(websocket, {"type": "ERROR", "message": str(exc)})
            await websocket.close(code=1008)
            return

        logger.info("Fetching chain snapshot...")
        try:
            current_price = await client.get_underlying_price(price_symbol)
            chain = await client.get_option_chain(product_symbol, expiry)
        except Exception as exc:
            logger.error("Chain fetch failed: %s", exc, exc_info=True)
            await _safe_send(
                websocket,
                {
                    "type": "ERROR",
                    "message": f"Failed to fetch chain: {exc}",
                },
            )
            await websocket.close(code=1011)
            return

        atm_strike = annotate_atm(chain, float(current_price))
        sent = await _safe_send(
            websocket,
            {
                "type": "CHAIN_SNAPSHOT",
                "current_price": current_price,
                "atm_strike": atm_strike,
                "chain": chain,
            },
        )
        if not sent:
            logger.warning("Client disconnected before snapshot delivery")
            return
        logger.info(
            "Chain snapshot sent, %s rows, price=%s atm=%s",
            len(chain),
            current_price,
            atm_strike,
        )

        symbols: list[str] = [price_symbol]
        for row in chain:
            if row.get("call_symbol"):
                symbols.append(str(row["call_symbol"]))
            if row.get("put_symbol"):
                symbols.append(str(row["put_symbol"]))

        logger.info("Connecting to Delta WS...")
        delta_ws_connected = False
        try:
            delta_ws = DeltaWebSocket()
            await asyncio.wait_for(delta_ws.connect(), timeout=DELTA_WS_CONNECT_TIMEOUT)
            delta_ws_connected = True
            logger.info("Delta WS connected for live ticks")
        except Exception as exc:
            logger.warning(
                "Delta WS failed: %s — falling back to REST poll",
                exc,
                exc_info=True,
            )
            if delta_ws is not None:
                await delta_ws.close()
                delta_ws = None

        if delta_ws_connected and delta_ws is not None:
            logger.info("Subscribing to %s symbols...", len(symbols))
            try:
                await delta_ws.subscribe_option_chain(symbols)
                logger.info("Listening for ticks...")
                await _stream_delta_ticks(websocket, delta_ws, price_symbol)
                logger.warning("Delta WS stream ended — switching to REST poll")
            except Exception as exc:
                logger.warning(
                    "Delta WS stream failed (%s) — switching to REST poll",
                    exc,
                    exc_info=True,
                )
            finally:
                await delta_ws.close()
                delta_ws = None
            await _poll_rest_fallback(
                websocket, client, product_symbol, price_symbol, expiry
            )
        else:
            await _poll_rest_fallback(
                websocket, client, product_symbol, price_symbol, expiry
            )

    except WebSocketDisconnect:
        logger.info("Option-chain WS client disconnected")
    except Exception as exc:
        logger.error("WS option chain error: %s", exc, exc_info=True)
        await _safe_send(websocket, {"type": "ERROR", "message": str(exc)})
    finally:
        if delta_ws is not None:
            await delta_ws.close()
        if client is not None:
            await client.close()
        if db is not None:
            db.close()
        logger.info("WS option chain session closed: %s %s", underlying, expiry)
