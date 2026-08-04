import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import websockets


async def test() -> None:
    url = "ws://127.0.0.1:8000/ws/option-chain?underlying=BTC&expiry=2026-08-04"
    print(f"Connecting to: {url}")
    try:
        async with websockets.connect(url) as ws:
            print("Connected!")
            # Wait for first message
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(msg)
            print(f"Message type: {data.get('type')}")
            if data.get("type") == "CHAIN_SNAPSHOT":
                print(f"Chain rows: {len(data.get('chain', []))}")
                print(f"Current price: {data.get('current_price')}")
                if data.get("chain"):
                    print(f"First row: {data['chain'][0]}")
                print("✅ SNAPSHOT RECEIVED OK")
            elif data.get("type") == "ERROR":
                print(f"❌ SERVER ERROR: {data.get('message')}")
            else:
                print(f"Unexpected type: {data}")

            # Wait for second message (tick or error)
            msg2 = await asyncio.wait_for(ws.recv(), timeout=10)
            data2 = json.loads(msg2)
            print(f"Second message type: {data2.get('type')}")
            if data2.get("type") == "ERROR":
                print(f"Second ERROR: {data2.get('message')}")
            elif data2.get("type") == "PRICE_UPDATE":
                print(f"PRICE_UPDATE price={data2.get('price')}")
            elif data2.get("type") == "TICK_UPDATE":
                print(
                    f"TICK_UPDATE {data2.get('symbol')} mark={data2.get('mark_price')}"
                )

    except asyncio.TimeoutError:
        print("❌ TIMEOUT: No message received in 15 seconds")
    except Exception as e:
        print(f"❌ CONNECTION ERROR: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test())
