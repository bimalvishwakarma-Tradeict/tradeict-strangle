# delta_ws.py — Delta Exchange India WebSocket client for live option tickers

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger(__name__)

TickCallback = Callable[[str, dict[str, Any]], Awaitable[None]]

SUBSCRIBE_CHUNK_SIZE = 50
CONNECT_TIMEOUT_SECONDS = 5.0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_ticker_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """
    Normalize a Delta v2/ticker WS message into {mark_price, bid, ask, delta}.

    Bid/ask live under data['quotes'] on Delta India (top-level best_bid is often null).
    """
    symbol = str(data.get("symbol") or "")
    if not symbol:
        return None

    quotes = data.get("quotes") if isinstance(data.get("quotes"), dict) else {}
    bid = _safe_float(
        data.get("best_bid")
        or data.get("bid")
        or quotes.get("best_bid")
        or quotes.get("bid")
    )
    ask = _safe_float(
        data.get("best_ask")
        or data.get("ask")
        or quotes.get("best_ask")
        or quotes.get("ask")
    )
    mark = _safe_float(data.get("mark_price") or data.get("mark") or data.get("close"))
    if mark <= 0 and bid > 0 and ask > 0:
        mark = (bid + ask) / 2.0

    greeks = data.get("greeks") if isinstance(data.get("greeks"), dict) else {}
    delta = _safe_float(greeks.get("delta") if greeks else data.get("delta"))

    return {
        "symbol": symbol,
        "mark_price": mark,
        "bid": bid,
        "ask": ask,
        "delta": delta,
    }


class DeltaWebSocket:
    """Live ticker feed from Delta Exchange India public WebSocket."""

    WS_URL = "wss://socket.india.delta.exchange"

    def __init__(self) -> None:
        self._ws: Any | None = None
        self._closed = False

    async def connect(self) -> None:
        """Open WebSocket connection to Delta India (with timeout)."""
        self._closed = False
        logger.info("Connecting to Delta WS: %s", self.WS_URL)
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                    open_timeout=CONNECT_TIMEOUT_SECONDS,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS + 1.0,
            )
        except Exception as exc:
            logger.error("Delta WS connect failed: %s", exc, exc_info=True)
            self._ws = None
            raise
        logger.info("Connected to Delta WebSocket %s", self.WS_URL)

    async def subscribe_option_chain(self, symbols: list[str]) -> None:
        """Subscribe to v2/ticker for the given symbols (chunks of 50)."""
        if self._ws is None:
            raise RuntimeError("DeltaWebSocket not connected")
        unique = [s for s in dict.fromkeys(symbols) if s]
        logger.info("Subscribing to %s symbols", len(unique))
        logger.info("First 3 symbols: %s", unique[:3])
        for i in range(0, len(unique), SUBSCRIBE_CHUNK_SIZE):
            chunk = unique[i : i + SUBSCRIBE_CHUNK_SIZE]
            payload = {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {
                            "name": "v2/ticker",
                            "symbols": chunk,
                        }
                    ]
                },
            }
            logger.info("Subscribe payload: %s", json.dumps(payload)[:500])
            await self._ws.send(json.dumps(payload))
            logger.info(
                "Subscribed Delta ticker chunk %s–%s (%s symbols)",
                i,
                i + len(chunk),
                len(chunk),
            )
            await asyncio.sleep(0.1)

    async def listen(self, callback: TickCallback) -> None:
        """
        Listen forever and invoke callback(symbol, normalized_data) per ticker.

        Stops when connection closes or close() is called.
        """
        if self._ws is None:
            raise RuntimeError("DeltaWebSocket not connected")
        logger.info("Listening for Delta ticker messages...")
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                raw_text = raw if isinstance(raw, str) else str(raw)
                logger.debug("Raw WS message: %s", raw_text[:200])
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON Delta WS message: %s", raw_text[:120])
                    continue
                if not isinstance(data, dict):
                    continue
                msg_type = str(data.get("type") or "")
                logger.debug("Message type: %s", msg_type)
                if msg_type in {"subscriptions", "heartbeat", "ping", "pong"}:
                    continue
                if msg_type != "v2/ticker" and "mark_price" not in data:
                    continue
                parsed = parse_ticker_payload(data)
                if parsed is None:
                    continue
                await callback(parsed["symbol"], parsed)
        except ConnectionClosed as exc:
            logger.info("Delta WebSocket connection closed: %s", exc)
        finally:
            await self.close()

    async def close(self) -> None:
        """Close the WebSocket if open."""
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("Delta WebSocket close ignored error", exc_info=True)
