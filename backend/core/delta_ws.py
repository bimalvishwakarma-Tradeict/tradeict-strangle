# delta_ws.py — Delta Exchange India WebSocket (public tickers + private margins)

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sys
import time
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
MARGINS_CACHE_TTL_SECONDS = 60.0
USD_ASSET_ID = 14
WS_AUTH_PATH = "/live"

# Margins cache — keyed by client_cache_key(api_key), then asset symbol (e.g. USD).
# Tests may also write _ws_margins_cache["USD"] directly (legacy single-account).
_ws_margins_cache: dict[str, Any] = {}


def client_cache_key(api_key: str) -> str:
    """Stable in-memory key for per-account WS margins (never logged)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def get_ws_margins(
    asset: str = "USD",
    *,
    cache_key: str | None = None,
) -> dict[str, float] | None:
    """
    Return cached margins row for asset if fresh (<60s), else None.

    Source: Delta private WebSocket ``margins`` channel (asset_id=14 / USD).
    """
    asset_u = asset.upper()
    bucket: dict[str, float] | None = None
    if cache_key:
        nested = _ws_margins_cache.get(cache_key)
        if isinstance(nested, dict):
            entry = nested.get(asset_u)
            if isinstance(entry, dict) and "ts" in entry:
                bucket = entry
    else:
        entry = _ws_margins_cache.get(asset_u)
        if isinstance(entry, dict) and "ts" in entry:
            bucket = entry
    if bucket is None:
        return None
    age = time.time() - float(bucket.get("ts", 0))
    if age > MARGINS_CACHE_TTL_SECONDS:
        return None
    return bucket


def _store_ws_margins(cache_key: str, asset: str, row: dict[str, float]) -> None:
    asset_u = asset.upper()
    account_bucket = _ws_margins_cache.setdefault(cache_key, {})
    if not isinstance(account_bucket, dict):
        account_bucket = {}
        _ws_margins_cache[cache_key] = account_bucket
    account_bucket[asset_u] = row


def _parse_margins_message(msg: dict[str, Any]) -> dict[str, float] | None:
    """Normalize a margins-channel update for USD wallet."""
    if str(msg.get("type") or "") != "margins":
        return None
    sym = str(msg.get("asset_symbol") or "").upper()
    asset_id = msg.get("asset_id")
    if sym != "USD" and asset_id != USD_ASSET_ID:
        return None
    return {
        "available_balance": _safe_float(msg.get("available_balance")),
        "balance": _safe_float(msg.get("balance")),
        "blocked_margin": _safe_float(msg.get("blocked_margin")),
        "ts": time.time(),
    }


def _ws_key_auth_signature(api_secret: str, timestamp: str) -> str:
    """HMAC for private WS: message = GET + timestamp + /live."""
    message = f"GET{timestamp}{WS_AUTH_PATH}"
    return hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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


class DeltaMarginsWebSocket:
    """
    Private margins feed — Delta UI Available Margin via ``margins`` channel.

    Auth: key-auth (GET + timestamp + /live). Margins channel was NOT subscribed
    before B19 — only public v2/ticker existed on DeltaWebSocket.
    """

    WS_URL = "wss://socket.india.delta.exchange"

    def __init__(self, api_key: str, api_secret: str, cache_key: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.cache_key = cache_key
        self._ws: Any | None = None
        self._closed = False
        self._authenticated = False

    async def _authenticate(self) -> None:
        if self._ws is None:
            raise RuntimeError("DeltaMarginsWebSocket not connected")
        timestamp = str(int(time.time()))
        signature = _ws_key_auth_signature(self.api_secret, timestamp)
        payload = {
            "type": "key-auth",
            "payload": {
                "api-key": self.api_key,
                "timestamp": timestamp,
                "signature": signature,
            },
        }
        await self._ws.send(json.dumps(payload))
        logger.info("Sent Delta margins WS key-auth (cache_key=%s…)", self.cache_key[:8])

    async def _subscribe_margins(self) -> None:
        if self._ws is None:
            raise RuntimeError("DeltaMarginsWebSocket not connected")
        payload = {
            "type": "subscribe",
            "payload": {
                "channels": [{"name": "margins"}],
            },
        }
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed Delta margins channel (cache_key=%s…)", self.cache_key[:8])

    async def run(self) -> None:
        """Connect, authenticate, subscribe margins, listen until disconnect."""
        self._closed = False
        self._authenticated = False
        logger.info("Connecting Delta margins WS: %s", self.WS_URL)
        async with websockets.connect(
            self.WS_URL,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
            open_timeout=CONNECT_TIMEOUT_SECONDS,
        ) as ws:
            self._ws = ws
            await self._authenticate()
            auth_deadline = time.time() + 10.0
            async for raw in ws:
                if self._closed:
                    break
                raw_text = raw if isinstance(raw, str) else str(raw)
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON margins WS message: %s", raw_text[:120])
                    continue
                if not isinstance(data, dict):
                    continue
                msg_type = str(data.get("type") or "")
                if msg_type in {"heartbeat", "ping", "pong", "subscriptions"}:
                    continue
                if msg_type == "key-auth":
                    if data.get("success"):
                        self._authenticated = True
                        logger.info(
                            "Delta margins WS authenticated (cache_key=%s…)",
                            self.cache_key[:8],
                        )
                        await self._subscribe_margins()
                    else:
                        logger.error(
                            "Delta margins WS auth failed: %s",
                            data.get("message") or data.get("status"),
                        )
                        return
                    continue
                if not self._authenticated and time.time() > auth_deadline:
                    logger.error("Delta margins WS auth timeout (cache_key=%s…)", self.cache_key[:8])
                    return
                if msg_type == "margins":
                    parsed = _parse_margins_message(data)
                    if parsed is not None:
                        _store_ws_margins(self.cache_key, "USD", parsed)
                        logger.debug(
                            "Margins WS USD update avail=%s balance=%s blocked=%s",
                            parsed["available_balance"],
                            parsed["balance"],
                            parsed["blocked_margin"],
                        )
        self._ws = None

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("Delta margins WS close ignored error", exc_info=True)


class MarginsFeedManager:
    """One background margins feed per API key."""

    _tasks: dict[str, asyncio.Task[None]] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def ensure_feed(cls, api_key: str, api_secret: str) -> None:
        ck = client_cache_key(api_key)
        async with cls._lock:
            task = cls._tasks.get(ck)
            if task is not None and not task.done():
                return
            cls._tasks[ck] = asyncio.create_task(
                cls._run_feed(api_key, api_secret, ck),
                name=f"delta-margins-{ck[:8]}",
            )

    @classmethod
    async def _run_feed(cls, api_key: str, api_secret: str, cache_key: str) -> None:
        while True:
            try:
                feed = DeltaMarginsWebSocket(api_key, api_secret, cache_key)
                await feed.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Margins feed reconnecting (cache_key=%s…): %s",
                    cache_key[:8],
                    exc,
                )
            await asyncio.sleep(5.0)

    @classmethod
    async def stop_all(cls) -> None:
        async with cls._lock:
            tasks = list(cls._tasks.values())
            cls._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


async def ensure_margins_feed(api_key: str, api_secret: str) -> None:
    """Start private margins WS for this account if not already running."""
    await MarginsFeedManager.ensure_feed(api_key, api_secret)
