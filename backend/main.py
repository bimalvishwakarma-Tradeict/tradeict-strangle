# main.py — FastAPI app entry point, CORS, and router registration

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes_account import router as account_router
from backend.api.routes_auto_trade import router as auto_trade_router
from backend.api.routes_logs import router as logs_router
from backend.api.routes_slave import router as slave_router
from backend.api.routes_strategy import router as strategy_router
from backend.api.routes_trade import router as trade_router
from backend.api.routes_ws import router as ws_router
from backend.core.bot_logger import setup_bot_logger
from backend.database import init_db
from backend.engine.bot_engine import bot_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: init DB + start bot + auto-trade + mirror engines. Shutdown: stop both."""
    from backend.database import SessionLocal
    from backend.engine import auto_trade_engine as ate_module
    from backend.engine import mirror_engine as mirror_module
    from backend.engine.auto_trade_engine import AutoTradeEngine
    from backend.engine.mirror_engine import MirrorEngine

    init_db()
    setup_bot_logger()
    logger.info("Database initialized / tables ready")

    auto_trade_engine = AutoTradeEngine(
        db_factory=SessionLocal,
        delta_client=bot_engine.delta_client,
        position_tracker=bot_engine.position_tracker,
        order_executor=bot_engine.order_executor,
    )
    auto_trade_engine.set_bot_engine(bot_engine)
    bot_engine.auto_trade_engine = auto_trade_engine
    ate_module.auto_trade_engine = auto_trade_engine

    mirror_engine = MirrorEngine(db_factory=SessionLocal)
    mirror_module.mirror_engine = mirror_engine
    bot_engine.mirror_engine = mirror_engine
    logger.info("🪞 Mirror engine ready")

    bot_task = asyncio.create_task(bot_engine.start(), name="bot-engine")
    auto_task = asyncio.create_task(
        auto_trade_engine.start(), name="auto-trade-engine"
    )
    try:
        yield
    finally:
        await bot_engine.stop()
        await auto_trade_engine.stop()
        for task in (bot_task, auto_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Delta Trading Bot",
    description="Short Strangle (S001) trade management bot — Delta Exchange India",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(account_router)
app.include_router(strategy_router)
app.include_router(trade_router)
app.include_router(auto_trade_router)
app.include_router(slave_router)
app.include_router(logs_router)
app.include_router(ws_router)


@app.get("/")
async def root() -> dict[str, Any]:
    """Health/status ping for the API service."""
    return {"status": "ok", "service": "Delta Trading Bot"}


@app.get("/health")
async def health() -> dict[str, Any]:
    """Explicit health check used by the frontend shell."""
    return {"status": "ok", "service": "Delta Trading Bot"}


@app.get("/debug/tracker")
async def debug_tracker() -> dict[str, Any]:
    """Temporary debug: in-memory position tracker contents."""
    positions = bot_engine.position_tracker.get_all_active()
    return {
        "active_count": len(positions),
        "trade_ids": [p.trade_id for p in positions],
        "bot_running": bot_engine.is_running,
        "live_price_symbols": list(bot_engine._live_prices.keys()),
    }


@app.get("/debug/positions")
async def debug_positions() -> dict[str, Any]:
    """Debug: raw positions + UPL@offer calculation (matches Delta UI)."""
    from backend.core.delta_client import DeltaClient
    from backend.core.encryption import decrypt
    from backend.database import SessionLocal
    from backend.models import Account

    db = SessionLocal()
    try:
        account = (
            db.query(Account)
            .filter(Account.is_active.is_(True))
            .order_by(Account.id.asc())
            .first()
        )
        if account is None:
            account = db.query(Account).order_by(Account.id.asc()).first()
        if account is None:
            return {"error": "No account configured", "positions": []}
        api_key = decrypt(account.api_key_encrypted)
        api_secret = decrypt(account.api_secret_encrypted)
    finally:
        db.close()

    client = DeltaClient(api_key, api_secret)
    try:
        positions = await client._request("GET", "/v2/positions/margined")
        raw_list = positions if isinstance(positions, list) else []
        pids: list[int] = []
        for pos in raw_list:
            if not isinstance(pos, dict):
                continue
            if float(pos.get("size") or 0) == 0:
                continue
            pid = pos.get("product_id") or (pos.get("product") or {}).get("id")
            if pid is not None:
                pids.append(int(pid))
        upnl_data = await client.get_positions_upnl(pids) if pids else {}
        total = sum(float(v.get("upnl") or 0) for v in upnl_data.values())
        return {
            "raw_positions": positions,
            "upnl_calculation": {
                str(k): {kk: vv for kk, vv in v.items() if kk != "raw"}
                for k, v in upnl_data.items()
            },
            "total_upnl": total,
            "endpoint_version": "upl_offer_v2",
            "note": (
                "API unrealized_pnl is NOT UPL (it is ~mark*|size|*cv). "
                "upnl_calculation uses L2 Best Offer = Delta UI UPL@offer."
            ),
        }
    finally:
        await client.close()
