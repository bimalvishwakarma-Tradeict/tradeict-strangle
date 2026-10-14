"""Public Delta India WS proxy for chart_app. No API keys. Broadcasts ticks to browsers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger("chart_app.live")

DELTA_WS = "wss://socket.india.delta.exchange"
SYMBOL = "BTCUSD"
TICKER_CH = "v2/ticker"
TRADES_CH = "all_trades"


def _ws_mock() -> bool:
    return os.environ.get("CHART_APP_WS_MOCK", "").strip().lower() in ("1", "true", "yes")


def _unix(v: Any) -> int | None:
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t > 1e15:
        t = t / 1e9
    elif t > 1e12:
        t = t / 1000.0
    t = int(t)
    return t if t > 1e9 else None


def _price(msg: dict[str, Any]) -> float | None:
    for key in ("price", "mark_price", "close", "spot_price", "last"):
        raw = msg.get(key)
        if raw is None and isinstance(msg.get("symbol_data"), dict):
            raw = msg["symbol_data"].get(key)
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


class LiveHub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.connected = False
        self.last_latency_ms: float | None = None
        self.channel = TICKER_CH
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None and not _ws_mock():
            self._task = asyncio.create_task(self._run_delta())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.connected = False

    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        await self._send(ws, self._status())

    def unregister(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    def _status(self) -> dict[str, Any]:
        return {
            "type": "status",
            "connected": self.connected or _ws_mock(),
            "channel": self.channel,
            "latency_ms": self.last_latency_ms,
            "mock": _ws_mock(),
        }

    async def _send(self, ws: WebSocket, msg: dict[str, Any]) -> bool:
        if ws.client_state != WebSocketState.CONNECTED:
            return False
        try:
            await ws.send_json(msg)
            return True
        except Exception:
            return False

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self.clients)
        for ws in clients:
            ok = await self._send(ws, msg)
            if not ok:
                dead.append(ws)
        for ws in dead:
            self.unregister(ws)

    async def inject_tick(self, price: float, ts: int | None = None) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        ts_sec = int(ts) if ts else int(now_ms / 1000)
        out = {
            "type": "tick",
            "channel": "mock",
            "price": float(price),
            "ts": ts_sec,
            "recv_ms": now_ms,
            "latency_ms": 0.0,
        }
        self.last_latency_ms = 0.0
        await self.broadcast(out)
        return out

    def _norm(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        typ = str(msg.get("type") or "")
        payload = msg
        if isinstance(msg.get("data"), dict):
            payload = dict(msg["data"])
            payload.setdefault("type", typ)
        px = _price(payload) or _price(msg)
        if px is None:
            return None
        ts = _unix(payload.get("timestamp") or payload.get("time") or msg.get("timestamp"))
        now_ms = int(time.time() * 1000)
        ts_sec = ts if ts is not None else int(now_ms / 1000)
        lat = max(0.0, now_ms - ts_sec * 1000) if ts is not None else 0.0
        if lat > 120000:
            lat = 0.0
        self.last_latency_ms = lat
        ch = typ if typ else TICKER_CH
        if typ in (TICKER_CH, TRADES_CH) or "trade" in typ or typ == TICKER_CH:
            self.channel = typ or TICKER_CH
        return {
            "type": "tick",
            "channel": ch,
            "price": px,
            "ts": ts_sec,
            "recv_ms": now_ms,
            "latency_ms": round(lat, 1),
        }

    async def _run_delta(self) -> None:
        import websockets

        backoff = 1.0
        sub = {
            "type": "subscribe",
            "payload": {
                "channels": [
                    {"name": TICKER_CH, "symbols": [SYMBOL]},
                    {"name": TRADES_CH, "symbols": [SYMBOL]},
                ]
            },
        }
        while True:
            try:
                async with websockets.connect(DELTA_WS, ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    backoff = 1.0
                    await self.broadcast(self._status())
                    await ws.send(json.dumps(sub))
                    try:
                        await ws.send(json.dumps({"type": "enable_heartbeat"}))
                    except Exception:
                        pass
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        out = self._norm(msg)
                        if out is not None:
                            await self.broadcast(out)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                logger.warning("delta ws disconnect: %s", e)
                self.connected = False
                await self.broadcast(self._status())
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)


HUB = LiveHub()


async def client_loop(ws: WebSocket) -> None:
    await HUB.register(ws)
    try:
        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "tick" and msg.get("mock"):
                await HUB.inject_tick(float(msg.get("price") or 0), msg.get("ts"))
            elif msg.get("cmd") == "status":
                await HUB._send(ws, HUB._status())
    finally:
        HUB.unregister(ws)
