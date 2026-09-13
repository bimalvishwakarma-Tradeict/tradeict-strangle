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
    validate_strategy3_config_payload,
)
from backend.strategies.s003_lsr4.lsr4 import (
    BackfillDiagnostics,
    ChartTrace,
    LSR4Engine,
)

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


async def run_offline(
    n: int = 500,
    *,
    ignore_warmup: bool = False,
    timeframe: str | None = None,
    overrides: dict[str, Any] | None = None,
    with_chart: bool = False,
) -> dict[str, Any]:
    """
    Shared offline engine run for backfill + chart.

    Does NOT write strategy3_signals / config and does NOT touch live engine state.
    """
    import copy

    import pytz

    cfg, _ = _load_runtime_config()
    cfg = copy.deepcopy(cfg)
    overrides_applied: dict[str, Any] = {}

    if overrides:
        validated = validate_strategy3_config_payload(dict(overrides))
        for key, val in validated.items():
            if key == "enabled":
                # Echo only — never treat as a live toggle in offline preview
                overrides_applied[key] = val
                continue
            setattr(cfg, key, val)
            overrides_applied[key] = val

    if timeframe is not None:
        tf = str(timeframe).strip()
        if tf not in {"1m", "3m", "5m", "15m"}:
            raise ValueError(f"timeframe must be one of 1m/3m/5m/15m, got {tf}")
        cfg.timeframe = tf
        overrides_applied["timeframe"] = tf

    client = _resolve_client()
    try:
        cfg.tick_size = await fetch_product_tick_size(client, cfg.symbol)
    except Exception:
        pass

    feed = CandleFeed(client, cfg)
    candles = await feed.fetch_last_n(n)
    diag = BackfillDiagnostics()
    chart = ChartTrace() if with_chart else None
    engine = LSR4Engine(
        cfg,
        ignore_warmup=bool(ignore_warmup),
        diagnostics=diag,
        chart_trace=chart,
    )

    signals_iso: list[dict[str, Any]] = []
    for candle in candles:
        sig = engine.process_closed_candle(candle)
        if sig is not None:
            signals_iso.append(
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
                    "confirm_level": sig.confirm_level,
                }
            )
    engine.finalize_warmup_diagnostics()

    ist = pytz.timezone(str(cfg.vwap_anchor_tz or "Asia/Kolkata"))

    def _fmt(dt: Any) -> str | None:
        if dt is None:
            return None
        return dt.isoformat()

    first = candles[0] if candles else None
    last = candles[-1] if candles else None
    first_utc = first.open_time if first else None
    last_utc = last.open_time if last else None
    first_ist = first_utc.astimezone(ist) if first_utc is not None else None
    last_ist = last_utc.astimezone(ist) if last_utc is not None else None

    diag_payload = diag.to_dict()
    out: dict[str, Any] = {
        "timeframe": cfg.timeframe,
        "overrides_applied": overrides_applied,
        "candles_requested": int(n),
        "candles_fetched": len(candles),
        "first_candle_utc": _fmt(first_utc),
        "first_candle_ist": _fmt(first_ist),
        "last_candle_utc": _fmt(last_utc),
        "last_candle_ist": _fmt(last_ist),
        "warm_from_index": diag_payload["warm_from_index"],
        "warm_blocked_reason": diag_payload["warm_blocked_reason"],
        "vwap_session_resets": diag_payload["vwap_session_resets"],
        "diagnostics": diag_payload["counters"],
        "counters": diag_payload["counters"],
        "signals": signals_iso,
        "count": len(signals_iso),
        "_candles_raw": candles,
        "_chart": chart,
    }
    if len(candles) != int(n):
        out["fetch_note"] = (
            f"requested {int(n)} closed candles, exchange/finality returned "
            f"{len(candles)}"
        )
    if ignore_warmup:
        out["warning"] = (
            "warmup guard bypassed — VWAP and ADX may be unreliable, "
            "do not compare these signals against the chart"
        )
    return out


async def run_backfill(
    n: int = 500,
    *,
    ignore_warmup: bool = False,
) -> dict[str, Any]:
    """Backfill wrapper — same engine path as chart, no DB writes."""
    raw = await run_offline(n, ignore_warmup=ignore_warmup, with_chart=False)
    out: dict[str, Any] = {
        "candles_requested": raw["candles_requested"],
        "candles_fetched": raw["candles_fetched"],
        "first_candle_utc": raw["first_candle_utc"],
        "first_candle_ist": raw["first_candle_ist"],
        "last_candle_utc": raw["last_candle_utc"],
        "last_candle_ist": raw["last_candle_ist"],
        "warm_from_index": raw["warm_from_index"],
        "warm_blocked_reason": raw["warm_blocked_reason"],
        "vwap_session_resets": raw["vwap_session_resets"],
        "counters": raw["counters"],
        "signals": raw["signals"],
        "count": raw["count"],
    }
    if "fetch_note" in raw:
        out["fetch_note"] = raw["fetch_note"]
    if "warning" in raw:
        out["warning"] = raw["warning"]
    return out


async def run_chart(
    n: int = 1500,
    *,
    timeframe: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Chart payload: candles + engine indicator series + signals + arm events."""
    raw = await run_offline(
        n,
        ignore_warmup=False,
        timeframe=timeframe,
        overrides=overrides,
        with_chart=True,
    )
    candles_raw = raw.pop("_candles_raw")
    chart = raw.pop("_chart")
    assert isinstance(chart, ChartTrace)

    candle_rows = [
        {
            "time": int(c.open_time.timestamp()),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume),
        }
        for c in candles_raw
    ]

    out: dict[str, Any] = {
        "timeframe": raw["timeframe"],
        "overrides_applied": raw["overrides_applied"],
        "candles": candle_rows,
        "indicators": chart.indicators,
        "signals": chart.signals,
        "arm_events": chart.arm_events,
        "warm_from_index": raw["warm_from_index"],
        "diagnostics": raw["diagnostics"],
    }
    if "fetch_note" in raw:
        out["fetch_note"] = raw["fetch_note"]
    return out
