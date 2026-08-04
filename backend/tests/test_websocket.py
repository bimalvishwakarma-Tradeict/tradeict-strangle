# test_websocket.py — Live WebSocket feed verification for /ws/trades

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import websockets


async def test() -> None:
    uri = "ws://127.0.0.1:8000/ws/trades"
    async with websockets.connect(uri) as ws:
        print("Connected!")

        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(msg)
        print(f"Type: {data['type']}")
        print(f"Trades: {len(data.get('trades', []))}")
        assert data.get("type") == "INITIAL_STATE", f"Expected INITIAL_STATE, got {data}"
        assert isinstance(data.get("trades"), list), "trades must be a list"

        # Next message: ping (~20s) or TRADE_UPDATE if an active trade is monitored
        msg2 = await asyncio.wait_for(ws.recv(), timeout=25)
        data2 = json.loads(msg2)
        print(f"Second message type: {data2['type']}")
        assert data2.get("type") in {"ping", "TRADE_UPDATE", "ERROR", "ADJUSTMENT"}, (
            f"Unexpected second message: {data2}"
        )
        print("✅ WEBSOCKET TEST PASSED")


if __name__ == "__main__":
    asyncio.run(test())
