# worker.py — S003 background signal loop (no orders)

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaClient
from backend.core.time_utils import get_utc_now
from backend.database import SessionLocal
from backend.strategies.s003_lsr4.candles import (
    POLL_INTERVAL_SECONDS,
    CandleFeed,
    fetch_product_tick_size,
)
from backend.strategies.s003_lsr4.config import (
    Strategy3Config,
    config_from_row,
    get_or_create_strategy3_config,
    get_or_create_strategy3_engine_state,
    insert_signal,
    load_arm_rows,
    persist_arm_state,
    persist_engine_state,
    timeframe_seconds,
)
from backend.strategies.s003_lsr4.lsr4 import LSR4Engine

logger = logging.getLogger(__name__)

_engine: LSR4Engine | None = None
_feed: CandleFeed | None = None
_client: DeltaClient | None = None
_owns_client = False
_last_reset_fp: tuple[Any, ...] | None = None
_bootstrapped = False


def _resolve_client() -> DeltaClient:
    global _client, _owns_client
    if _client is not None:
        return _client
    try:
        from backend.engine.bot_engine import bot_engine

        if bot_engine.delta_client is not None:
            _client = bot_engine.delta_client
            _owns_client = False
            return _client
    except Exception:
        pass
    # Public candle endpoint only — empty credentials OK for unsigned GET
    _client = DeltaClient("s003-public", "s003-public")
    _owns_client = True
    return _client


def _persist(engine: LSR4Engine) -> None:
    arms, estate = engine.dump_state()
    with SessionLocal() as db:
        persist_arm_state(db, arms)
        persist_engine_state(db, estate)


def _load_runtime_config() -> tuple[Strategy3Config, bool]:
    with SessionLocal() as db:
        row = get_or_create_strategy3_config(db)
        enabled = bool(row.enabled)
        cfg = config_from_row(row)
    return cfg, enabled


async def _ensure_engine(cfg: Strategy3Config) -> LSR4Engine:
    global _engine, _feed, _last_reset_fp, _bootstrapped
    client = _resolve_client()
    try:
        cfg.tick_size = await fetch_product_tick_size(client, cfg.symbol)
    except Exception as exc:
        logger.warning("S003 tick_size fetch failed (%s) — using %.4f", exc, cfg.tick_size)

    fp = cfg.fingerprint_reset()
    first_boot = _engine is None
    fp_changed = _last_reset_fp is not None and _last_reset_fp != fp

    if first_boot or fp_changed:
        _engine = LSR4Engine(cfg)
        _feed = CandleFeed(client, cfg)
        _bootstrapped = False
        with SessionLocal() as db:
            if fp_changed:
                # Indicator lengths / timeframe / VWAP tz changed — wipe state
                arms: list[dict] = []
                estate = {
                    "last_processed_candle_time": None,
                    "last_signal_candle_time": None,
                    "last_signal_price": None,
                    "candles_processed": 0,
                    "vwap_session_date": None,
                    "warm": False,
                    "last_error": None,
                }
                persist_arm_state(db, arms)
                persist_engine_state(db, estate)
            else:
                # Process restart — restore arm + engine cursor from DB
                arms = load_arm_rows(db)
                estate_row = get_or_create_strategy3_engine_state(db)
                estate = {
                    "last_processed_candle_time": estate_row.last_processed_candle_time,
                    "last_signal_candle_time": estate_row.last_signal_candle_time,
                    "last_signal_price": estate_row.last_signal_price,
                    "candles_processed": estate_row.candles_processed,
                    "vwap_session_date": estate_row.vwap_session_date,
                    "warm": estate_row.warm,
                    "last_error": estate_row.last_error,
                }
            _engine.load_state(arms, estate)
        _last_reset_fp = fp
    else:
        assert _engine is not None
        _engine.apply_config(cfg, full_reset=False)
        if _feed is not None:
            _feed.apply_config(cfg)

    assert _engine is not None and _feed is not None

    if not _bootstrapped:
        candles = await _feed.bootstrap(500)
        # Restart: expire arms if downtime > max_wait
        if _engine.last_processed is not None and candles:
            last = candles[-1].open_time
            missed = int(
                round(
                    (last - _engine.last_processed).total_seconds()
                    / float(timeframe_seconds(cfg.timeframe))
                )
            )
            if missed > 0:
                _engine.expire_stale_arms_after_restart(missed)
        # Replay closed candles (idempotent skip of already processed)
        for candle in candles:
            sig = _engine.process_closed_candle(candle)
            if sig is not None:
                with SessionLocal() as db:
                    insert_signal(db, sig)
            _persist(_engine)
        _bootstrapped = True
    return _engine


async def s003_signal_worker() -> None:
    """
    Lifespan background task. Idle (no Delta calls) while enabled=False.
    """
    logger.info("S003 signal worker started (disabled by default until config.enabled)")
    try:
        while True:
            try:
                cfg, enabled = _load_runtime_config()
                if not enabled:
                    await asyncio.sleep(30)
                    continue

                engine = await _ensure_engine(cfg)
                assert _feed is not None
                new_candles = await _feed.poll()
                for candle in new_candles:
                    sig = engine.process_closed_candle(candle)
                    if sig is not None:
                        with SessionLocal() as db:
                            insert_signal(db, sig)
                    _persist(engine)
                engine.set_last_error(None)
                _persist(engine)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_and_buffer(
                    "STRAT3_ENGINE_ERROR",
                    0,
                    {"error": str(exc), "summary": f"[STRAT3_ENGINE_ERROR] {exc}"},
                )
                try:
                    if _engine is not None:
                        _engine.set_last_error(str(exc))
                        _persist(_engine)
                except Exception:
                    pass
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("S003 signal worker cancelled — shutting down")
        raise
    finally:
        global _client, _owns_client
        if _owns_client and _client is not None:
            try:
                await _client.close()
            except Exception:
                pass
            _client = None
            _owns_client = False


def get_live_engine() -> LSR4Engine | None:
    return _engine


async def run_backfill(n: int = 500) -> list[dict[str, Any]]:
    """
    Fresh engine over last N candles. Does NOT write signals or disturb live state.
    """
    cfg, _ = _load_runtime_config()
    client = _resolve_client()
    try:
        cfg.tick_size = await fetch_product_tick_size(client, cfg.symbol)
    except Exception:
        pass
    feed = CandleFeed(client, cfg)
    candles = await feed.fetch_last_n(n)
    engine = LSR4Engine(cfg)
    signals = []
    for candle in candles:
        sig = engine.process_closed_candle(candle)
        if sig is not None:
            signals.append(
                {
                    "direction": sig.direction,
                    "mode": sig.mode,
                    "score": sig.score,
                    "signal_candle_time": sig.signal_candle_time.isoformat(),
                    "confirm_candle_time": sig.confirm_candle_time.isoformat(),
                    "confirm_price": sig.confirm_price,
                    "signal_extreme": sig.signal_extreme,
                    "atr_at_arm": sig.atr_at_arm,
                    "atr_at_confirm": sig.atr_at_confirm,
                    "adx_at_signal": sig.adx_at_signal,
                }
            )
    return signals
