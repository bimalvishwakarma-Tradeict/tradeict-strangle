# bot_engine.py — Main async monitoring loop that manages all active trades

# ISOLATION: Only process legs from our DB, never all account positions
# MTM P&L DISPLAY RULE:
#   - delta_mtm_pnl / call_delta_mtm / put_delta_mtm → Delta official UPNL (exits + UI)
#   - calculated_pnl → fallback / reference only
#   - Match positions via Leg.product_id only (see DeltaClient.get_mtm_by_product_ids)

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import (
    ADJUSTMENT_COOLDOWN_MINUTES,
    MONITORING_INTERVAL_SECONDS,
    OPTIONS_CONTRACT_VALUE,
    ExitReason,
    TradeStatus,
)
from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaClient
from backend.core.delta_ws import DeltaWebSocket
from backend.core.encryption import decrypt
from backend.core.time_utils import (
    get_hours_to_expiry,
    get_ist_now,
    get_settling_info,
    settling_ends_at,
)
from backend.core.ws_manager import ws_manager
from backend.database import SessionLocal
from backend.engine.order_executor import OrderExecutor
from backend.engine.position_tracker import PositionTracker, TradeState
from backend.models import Account, Leg, Trade
from backend.strategies.s001_short_strangle.adjustment import AdjustmentExecutor
from backend.strategies.s001_short_strangle.logic import ShortStrangleStrategy

logger = logging.getLogger(__name__)

_CHAIN_CACHE_TTL_SECONDS = 60.0
_PRICE_TICK_MIN_INTERVAL_SECONDS = 2.0


class BotEngine:
    """Async monitoring loop for bot-managed short strangle trades."""

    def __init__(
        self,
        db_factory: Callable[[], Any],
        strategy: Any,
        position_tracker: PositionTracker,
        order_executor: OrderExecutor,
        adjustment_executor: AdjustmentExecutor,
        delta_client: DeltaClient | None = None,
    ) -> None:
        self.db_factory = db_factory
        self.delta_client = delta_client
        self.strategy = strategy
        self.position_tracker = position_tracker
        self.order_executor = order_executor
        self.adjustment_executor = adjustment_executor
        self.is_running = False
        self._last_trade_count: int | None = None
        # { "BTC_2026-08-05": (monotonic_ts, chain_rows) }
        self._chain_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # Last estimated replacements per trade_id for /active reuse
        self._replacement_estimates: dict[int, dict[str, Any]] = {}
        # Live mark prices from Delta WS: { symbol: mark_price }
        self._live_prices: dict[str, float] = {}
        self._ws_feed_task: asyncio.Task[None] | None = None
        self._last_price_tick_at: dict[int, float] = {}
        self._btc_spot: float = 0.0
        self._btc_spot_at: float = 0.0
        # Debounce emergency / manual-close integrity handlers
        self._integrity_in_progress: set[int] = set()
        self._premium_collapse_pending: set[int] = set()
        # Set from main.py lifespan after AutoTradeEngine is created
        self.auto_trade_engine: Any | None = None
        # Set from main.py lifespan after MirrorEngine is created
        self.mirror_engine: Any | None = None
        # Monitor-cycle counter for periodic slave integrity checks
        self._cycle_count: int = 0
        # Throttle wrongly-closed leg recovery (trade_id → monotonic ts)
        self._leg_recovery_last_checked: dict[int, float] = {}
        # Per-master-trade exit locks — prevent dual funnel / overwrite races
        self._exit_locks: dict[int, asyncio.Lock] = {}

    def _get_exit_lock(self, trade_id: int) -> asyncio.Lock:
        tid = int(trade_id)
        lock = self._exit_locks.get(tid)
        if lock is None:
            lock = asyncio.Lock()
            self._exit_locks[tid] = lock
        return lock

    async def start(self) -> None:
        self.is_running = True
        logger.info("🤖 Bot engine started")
        self._refresh_delta_client()
        with self.db_factory() as db:
            count = self.position_tracker.load_from_db(db)
            logger.info("Loaded %s active trades into position tracker", count)
            for state in self.position_tracker.get_all_active():
                logger.info(
                    "  Trade %s: %s | call=%s | put=%s",
                    state.trade_id,
                    state.trade.underlying,
                    state.call_leg.symbol,
                    state.put_leg.symbol,
                )
        self._last_trade_count = len(self.position_tracker.get_all_active())
        self._ws_feed_task = asyncio.create_task(
            self._start_price_feed(), name="delta-price-feed"
        )
        logger.info("Starting monitoring loop...")
        await self.monitoring_loop()

    async def stop(self) -> None:
        self.is_running = False
        if self._ws_feed_task is not None:
            self._ws_feed_task.cancel()
            try:
                await self._ws_feed_task
            except asyncio.CancelledError:
                pass
            self._ws_feed_task = None
        if self.delta_client is not None:
            try:
                await self.delta_client.close()
            except Exception:
                logger.exception("Error closing DeltaClient on stop")
            self.delta_client = None
        logger.info("Bot engine stopped")

    async def _start_price_feed(self) -> None:
        """Subscribe to Delta WS tickers for all monitored legs; update live prices."""
        while self.is_running:
            try:
                active = self.position_tracker.get_all_active()
                if not active:
                    await asyncio.sleep(5)
                    continue

                symbols: list[str] = []
                for state in active:
                    if str(getattr(state.call_leg, "status", "open")).lower() == "open":
                        symbols.append(str(state.call_leg.symbol))
                    if str(getattr(state.put_leg, "status", "open")).lower() == "open":
                        symbols.append(str(state.put_leg.symbol))
                symbols = list(dict.fromkeys(symbols))
                if not symbols:
                    await asyncio.sleep(5)
                    continue
                logger.info("Starting WS price feed for %s symbols", len(symbols))

                delta_ws = DeltaWebSocket()
                await delta_ws.connect()
                await delta_ws.subscribe_option_chain(symbols)
                subscribed = set(symbols)

                async def on_ticker(symbol: str, data: dict[str, Any]) -> None:
                    # ONLY best ask for shorts — never mark (mark poisons UI vs Delta UPL @offer)
                    ask = float(data.get("ask") or 0)
                    if ask < 0:
                        return
                    # Zero/near-zero ask can mean position closed by Delta SL
                    if ask == 0:
                        await self._maybe_flag_premium_collapse(symbol, ask)
                        return
                    prev = self._live_prices.get(symbol)
                    self._live_prices[symbol] = ask
                    if prev is not None and abs(prev - ask) < 1e-9:
                        return
                    # Premium collapse → likely Delta SL / external close
                    await self._maybe_flag_premium_collapse(symbol, ask)
                    # Refresh BTC spot opportunistically for payoff graph
                    try:
                        await self._refresh_btc_spot()
                    except Exception:
                        pass
                    await self._broadcast_price_tick(symbol, ask)

                # Re-check subscription set periodically via race with listen
                listen_task = asyncio.create_task(delta_ws.listen(on_ticker))
                try:
                    while self.is_running and not listen_task.done():
                        await asyncio.sleep(10)
                        current_syms: list[str] = []
                        for state in self.position_tracker.get_all_active():
                            if str(getattr(state.call_leg, "status", "open")).lower() == "open":
                                current_syms.append(str(state.call_leg.symbol))
                            if str(getattr(state.put_leg, "status", "open")).lower() == "open":
                                current_syms.append(str(state.put_leg.symbol))
                        current_set = set(dict.fromkeys(current_syms))
                        if current_set != subscribed:
                            logger.info(
                                "Monitored symbols changed — restarting WS feed"
                            )
                            break
                        if not current_set:
                            break
                finally:
                    await delta_ws.close()
                    listen_task.cancel()
                    try:
                        await listen_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WS price feed error: %s, restarting in 5s", exc)
                await asyncio.sleep(5)

    async def _maybe_flag_premium_collapse(
        self, symbol: str, ask: float
    ) -> None:
        """
        If ask < 5% of entry for an open leg, run immediate position integrity
        (Delta SL / external close likely).
        """
        for state in self.position_tracker.get_all_active():
            # Demo trades have no Delta positions — never trigger integrity
            if bool(getattr(state.trade, "is_demo", False)):
                continue
            call_sym = str(state.call_leg.symbol)
            put_sym = str(state.put_leg.symbol)
            if symbol not in {call_sym, put_sym}:
                continue
            is_call = symbol == call_sym
            leg = state.call_leg if is_call else state.put_leg
            if str(getattr(leg, "status", "open")).lower() != "open":
                continue
            entry = float(getattr(leg, "initial_premium", 0) or 0)
            if entry <= 0:
                continue
            if ask >= entry * 0.05:
                continue
            tid = state.trade_id
            if tid in self._integrity_in_progress:
                continue
            if tid in self._premium_collapse_pending:
                continue
            # Never schedule integrity while mid-adjustment (leg ask collapses
            # when we close the triggered leg — that is intentional).
            if getattr(state, "is_adjusting", False):
                continue
            logger.warning(
                "Premium collapse trade=%s %s ask=%.4f entry=%.4f "
                "— immediate position check",
                tid,
                "call" if is_call else "put",
                ask,
                entry,
            )
            self._premium_collapse_pending.add(tid)
            asyncio.create_task(
                self._run_immediate_integrity(tid),
                name=f"integrity-{tid}",
            )

    async def _run_immediate_integrity(self, trade_id: int) -> None:
        """WS-triggered position integrity (debounced via _integrity_in_progress)."""
        await asyncio.sleep(0.2)
        self._premium_collapse_pending.discard(trade_id)
        state = self.position_tracker.get(trade_id)
        if state is None or state.is_adjusting:
            return
        try:
            await self._enforce_position_integrity(
                state, source="premium_collapse_ws"
            )
        except Exception as exc:
            logger.error(
                "Immediate integrity check failed trade=%s: %s",
                trade_id,
                exc,
                exc_info=True,
            )

    async def _broadcast_price_tick(self, symbol: str, price: float) -> None:
        """Push lightweight PRICE_TICK to frontend (throttled per trade)."""
        now = time.monotonic()
        for state in self.position_tracker.get_all_active():
            call_sym = str(state.call_leg.symbol)
            put_sym = str(state.put_leg.symbol)
            if symbol not in {call_sym, put_sym}:
                continue
            last = self._last_price_tick_at.get(state.trade_id, 0.0)
            if now - last < _PRICE_TICK_MIN_INTERVAL_SECONDS:
                continue
            self._last_price_tick_at[state.trade_id] = now
            call_p = self._live_prices.get(call_sym, float(state.last_call_premium or 0))
            put_p = self._live_prices.get(put_sym, float(state.last_put_premium or 0))
            # Keep tracker premiums fresh for /active between full cycles
            self.position_tracker.update_premiums(
                state.trade_id,
                float(call_p or 0),
                float(put_p or 0),
                float(state.last_pnl),
            )
            await ws_manager.broadcast(
                {
                    "type": "PRICE_TICK",
                    "trade_id": state.trade_id,
                    "call_premium": call_p,
                    "put_premium": put_p,
                    "symbol": symbol,
                    "price": price,
                    "price_type": "ask",  # frontend must ignore non-ask ticks for PnL
                    "call_quantity": int(getattr(state.call_leg, "quantity", 0) or 0),
                    "put_quantity": int(getattr(state.put_leg, "quantity", 0) or 0),
                    "underlying_price": float(self._btc_spot or 0) or None,
                }
            )

    async def _refresh_btc_spot(self) -> float:
        """Cache BTC spot/index for payoff graph + fee estimates (max 1× / 2s)."""
        now = time.monotonic()
        if self._btc_spot > 0 and (now - self._btc_spot_at) < 2.0:
            return float(self._btc_spot)
        if self.delta_client is None:
            return float(self._btc_spot or 0)
        try:
            px = float(await self.delta_client.get_btc_index_price())
            if px > 0:
                self._btc_spot = px
                self._btc_spot_at = now
        except Exception as exc:
            logger.debug("BTC spot refresh failed: %s", exc)
        return float(self._btc_spot or 0)

    async def _get_premium(self, symbol: str) -> float:
        """
        Short-exit premium = Best Offer (L2/ticker ask).

        Do NOT use cached WS mark for PnL — that diverges from Delta UPL @offer.
        WS ask cache is OK when present; otherwise REST L2/ticker ask.
        """
        ws_price = self._live_prices.get(symbol)
        # Only trust WS cache if it was stored as ask (we only write ask now).
        # Still refresh via REST periodically would be ideal; for now prefer REST
        # when cache is missing, and use WS ask for speed between REST polls.
        assert self.delta_client is not None
        try:
            return float(await self.delta_client.get_short_exit_price(symbol))
        except Exception:
            if ws_price is not None and ws_price > 0:
                return float(ws_price)
            raise

    async def monitoring_loop(self) -> None:
        while self.is_running:
            try:
                await self._process_all_trades()
            except Exception as exc:
                logger.critical("Monitoring loop error: %s", exc, exc_info=True)
            await asyncio.sleep(MONITORING_INTERVAL_SECONDS)

    async def _process_all_trades(self) -> None:
        # Heal DB vs Delta before monitoring (external closes, zombies)
        if self.delta_client is None:
            self._refresh_delta_client()
        if self.delta_client is not None:
            try:
                from backend.database import SessionLocal
                from backend.engine.trade_reconcile import reconcile_open_legs_with_delta

                # Skip entire reconcile while any trade is mid-adjustment —
                # Delta will show a temporary one-leg-missing state.
                adjusting_ids = [
                    int(s.trade_id)
                    for s in self.position_tracker.get_all_active()
                    if getattr(s, "is_adjusting", False)
                ]
                if adjusting_ids:
                    logger.info(
                        "[RECONCILE_SKIP] trades adjusting=%s — "
                        "skipping full reconcile this cycle",
                        adjusting_ids,
                    )
                else:
                    with SessionLocal() as db:
                        recon = await reconcile_open_legs_with_delta(
                            db=db,
                            client=self.delta_client,
                            position_tracker=self.position_tracker,
                        )
                        closed_ids = list(recon.get("fully_closed") or [])
                        close_reasons = dict(recon.get("close_reasons") or {})
                        for tid in closed_ids:
                            tid_i = int(tid)
                            reason = str(
                                close_reasons.get(tid_i)
                                or ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value
                            )
                            funnel = await self.close_master_trade(
                                trade_id=tid_i,
                                reason=reason,
                                db=db,
                                skip_master_legs=True,
                                trade_state=self.position_tracker.get(tid_i),
                            )
                            slaves_closed = int(
                                funnel.get("slaves_closed") or 0
                            )
                            slaves_failed = int(
                                funnel.get("slaves_failed") or 0
                            )
                            if slaves_failed > 0:
                                logger.critical(
                                    "[EXIT_FUNNEL] reconcile close trade=%s "
                                    "reason=%s slaves_failed=%s "
                                    "slaves_closed=%s — exit_failed rows "
                                    "left for retry sweep",
                                    tid_i,
                                    reason,
                                    slaves_failed,
                                    slaves_closed,
                                )
                            await ws_manager.broadcast(
                                {
                                    "type": "TRADE_CLOSED",
                                    "trade_id": tid_i,
                                    "reason": reason,
                                    "message": (
                                        "Basket closed — Delta size flat"
                                    ),
                                    "slaves_closed": slaves_closed,
                                    "slaves_failed": slaves_failed,
                                }
                            )
                    for alert in recon.get("naked_risk") or []:
                        tid = int(alert["trade_id"])
                        remaining = str(alert["remaining"])
                        state = self.position_tracker.get(tid)
                        if state is None:
                            continue

                        # CRITICAL: Skip naked risk close if trade is adjusting
                        if getattr(state, "is_adjusting", False):
                            logger.info(
                                "[NAKED_SKIP] Trade#%s — is_adjusting=True, "
                                "skipping emergency close (reconcile bypass guard)",
                                tid,
                            )
                            continue

                        log_and_buffer(
                            "NAKED_POSITION",
                            tid,
                            {
                                "missing": alert.get("missing"),
                                "remaining": remaining,
                                "source": "reconcile",
                            },
                        )
                        await self._emergency_close_remaining_leg(
                            state, remaining
                        )
            except Exception as exc:
                logger.warning("Monitor reconcile failed: %s", exc)

        count = len(self.position_tracker.get_all_active())
        if self._last_trade_count != count:
            logger.info("Active trades in tracker: %s", count)
            self._last_trade_count = count

        # DB-driven slave sweep — runs even when the tracker is empty so
        # exit_failed / orphan SlaveTrades under closed masters still recover.
        self._cycle_count = int(getattr(self, "_cycle_count", 0) or 0) + 1
        if self._cycle_count % 5 == 0:
            try:
                import backend.engine.mirror_engine as mirror_mod

                me = mirror_mod.mirror_engine or self.mirror_engine
                if me is not None:
                    await me.sweep_open_slave_trades()
                else:
                    log_and_buffer(
                        "SLAVE_SWEEP",
                        0,
                        {
                            "note": "mirror_engine_none",
                            "rows_scanned": 0,
                            "closed_ok": 0,
                            "close_failed": 0,
                            "unreachable": 0,
                            "skipped_backoff": 0,
                        },
                    )
            except Exception as exc:
                log_and_buffer(
                    "SLAVE_SWEEP",
                    0,
                    {"note": "sweep_exception", "error": str(exc)},
                )
                logger.warning("Slave integrity cycle failed: %s", exc)

        if count == 0:
            return

        if self.delta_client is None:
            self._refresh_delta_client()
        if self.delta_client is None:
            logger.warning("No Delta client available — skipping trade processing")
            return

        try:
            await self._refresh_btc_spot()
        except Exception:
            pass

        for trade_state in list(self.position_tracker.get_all_active()):
            if trade_state.is_adjusting:
                continue
            # Fast path from WS premium-collapse flag
            if trade_state.trade_id in self._premium_collapse_pending:
                self._premium_collapse_pending.discard(trade_state.trade_id)
                ok = await self._enforce_position_integrity(
                    trade_state, source="premium_collapse"
                )
                if not ok:
                    continue
            try:
                await self._process_trade(trade_state)
            except Exception as exc:
                logger.error(
                    "Error processing trade %s: %s",
                    trade_state.trade_id,
                    exc,
                    exc_info=True,
                )

    def _active_product_ids_from_positions(
        self, positions: list[dict[str, Any]]
    ) -> set[int]:
        active: set[int] = set()
        for pos in positions or []:
            try:
                pid = int(pos.get("product_id"))
            except (TypeError, ValueError):
                continue
            try:
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0
            if pid > 0 and size != 0:
                active.add(pid)
        return active

    async def _check_position_integrity(self, trade_state: TradeState) -> str:
        """
        Compare Delta positions to what the bot expects.

        Returns:
            'ok'           — both legs open on Delta
            'manual_close' — neither leg on Delta (user/external close)
            'naked_call'   — only call open, put missing
            'naked_put'    — only put open, call missing
        """
        trade_id = trade_state.trade_id

        # CRITICAL: Skip integrity if adjustment is in progress.
        # During adjustment one leg is intentionally closed temporarily.
        # Re-read from tracker so we never use a stale is_adjusting=False.
        live = self.position_tracker.get(trade_id)
        # DIAGNOSTIC — remove after is_adjusting race root-caused
        logger.warning(
            "[DIAG_IS_ADJUSTING] (b) _check_position_integrity START "
            "trade_id=%s tracker.is_adjusting=%s "
            "trade_state.is_adjusting=%s tracker_has_trade=%s",
            trade_id,
            getattr(live, "is_adjusting", None) if live else None,
            getattr(trade_state, "is_adjusting", None),
            live is not None,
        )
        if live is not None and getattr(live, "is_adjusting", False):
            logger.debug(
                "[INTEGRITY_SKIP] Trade#%s — adjustment in progress, "
                "skipping integrity check",
                trade_id,
            )
            return "ok"
        if getattr(trade_state, "is_adjusting", False):
            logger.debug(
                "[INTEGRITY_SKIP] Trade#%s — adjustment in progress "
                "(trade_state flag), skipping integrity check",
                trade_id,
            )
            return "ok"

        # Demo/virtual trades have no real Delta positions — skip integrity
        if bool(getattr(trade_state.trade, "is_demo", False)):
            logger.debug(
                "Trade %s: skipping integrity check (demo/virtual trade)",
                trade_id,
            )
            return "ok"

        # Skip integrity during conversion mode — position structure changes
        if bool(getattr(trade_state.trade, "in_conversion_mode", False)):
            logger.debug(
                "Trade %s: skipping integrity check (in conversion mode)",
                trade_id,
            )
            return "ok"

        # AUDIT-6: only short call/put count toward integrity (ignore hedge_*)
        with self.db_factory() as db:
            open_leg_count = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                    Leg.leg_type.in_(("call", "put")),
                )
                .count()
            )

        if open_leg_count < 2:
            # Also skip when adjusting (one leg intentionally closed in DB)
            live2 = self.position_tracker.get(trade_id)
            if live2 is not None and getattr(live2, "is_adjusting", False):
                logger.debug(
                    "[INTEGRITY_SKIP] Trade#%s — adjusting with %s open "
                    "short leg(s)",
                    trade_id,
                    open_leg_count,
                )
                return "ok"
            logger.warning(
                "Trade %s has only %s open short leg(s) in DB. "
                "Partial adjustment state — skipping integrity check.",
                trade_id,
                open_leg_count,
            )
            return "ok"

        if self.delta_client is None:
            return "ok"

        call_pid = int(getattr(trade_state.call_leg, "product_id", 0) or 0)
        put_pid = int(getattr(trade_state.put_leg, "product_id", 0) or 0)
        try:
            call_exists = (
                await self.delta_client.verify_position_exists(call_pid)
                if call_pid > 0
                else False
            )
            put_exists = (
                await self.delta_client.verify_position_exists(put_pid)
                if put_pid > 0
                else False
            )
        except Exception as exc:
            logger.warning(
                "Trade %s: integrity check failed (API error): %s. "
                "Skipping this cycle.",
                trade_id,
                exc,
            )
            return "ok"

        # Re-check adjusting AFTER awaits — race: adjust may have started
        # while we were verifying Delta positions.
        live3 = self.position_tracker.get(trade_id)
        if live3 is not None and getattr(live3, "is_adjusting", False):
            logger.debug(
                "[INTEGRITY_SKIP] Trade#%s — adjustment started during "
                "Delta verify, ignoring result",
                trade_id,
            )
            return "ok"

        logger.debug(
            "[INTEGRITY] Trade %s: call_exists=%s put_exists=%s",
            trade_id,
            call_exists,
            put_exists,
        )

        if call_exists and put_exists:
            return "ok"
        if not call_exists and not put_exists:
            return "manual_close"
        if call_exists and not put_exists:
            return "naked_call"
        return "naked_put"

    def _check_partial_position(
        self, trade_state: TradeState, positions: list[dict[str, Any]]
    ) -> str:
        """Return 'both_open' | 'call_only' | 'put_only' | 'none' (legacy)."""
        active_pids = self._active_product_ids_from_positions(positions)
        call_pid = int(getattr(trade_state.call_leg, "product_id", 0) or 0)
        put_pid = int(getattr(trade_state.put_leg, "product_id", 0) or 0)
        call_open_delta = call_pid in active_pids
        put_open_delta = put_pid in active_pids
        if call_open_delta and put_open_delta:
            return "both_open"
        if call_open_delta and not put_open_delta:
            return "call_only"
        if put_open_delta and not call_open_delta:
            return "put_only"
        return "none"

    async def _fetch_positions_safe(self) -> list[dict[str, Any]] | None:
        assert self.delta_client is not None
        try:
            return await self.delta_client.get_positions()
        except Exception as exc:
            logger.error("Could not verify positions: %s", exc)
            return None

    async def _enforce_position_integrity(
        self,
        trade_state: TradeState,
        *,
        source: str = "monitor",
    ) -> bool:
        """
        Verify both open legs still exist on Delta.

        Returns False if trade was closed / emergency-handled (caller must stop).
        """
        trade_id = trade_state.trade_id

        # Absolute gate: never act while adjustment is in progress
        live = self.position_tracker.get(trade_id)
        if (live is not None and getattr(live, "is_adjusting", False)) or getattr(
            trade_state, "is_adjusting", False
        ):
            logger.debug(
                "[INTEGRITY_SKIP] Trade#%s enforce skipped (adjusting) source=%s",
                trade_id,
                source,
            )
            return True

        if trade_id in self._integrity_in_progress:
            return True

        integrity = await self._check_position_integrity(trade_state)
        log_and_buffer(
            "POSITION_CHECK",
            trade_id,
            {"status": integrity, "source": source},
        )

        if integrity == "ok":
            return True

        # Race guard: adjustment may have started during the check
        live2 = self.position_tracker.get(trade_id)
        if live2 is not None and getattr(live2, "is_adjusting", False):
            logger.warning(
                "[INTEGRITY_SKIP] Trade#%s — was %s but adjustment started; "
                "not acting",
                trade_id,
                integrity,
            )
            return True

        self._integrity_in_progress.add(trade_id)
        try:
            if integrity == "manual_close":
                logger.warning(
                    "[INTEGRITY] Trade %s: Both positions gone from Delta. "
                    "User likely closed manually.",
                    trade_state.trade_id,
                )
                log_and_buffer(
                    "INTEGRITY_MANUAL_CLOSE",
                    trade_state.trade_id,
                    {"source": source},
                )
                await self._handle_manual_close(trade_state)
                return False

            if integrity == "naked_call":
                logger.critical(
                    "[INTEGRITY] Trade %s: PUT position MISSING on Delta! "
                    "Closing call immediately.",
                    trade_state.trade_id,
                )
                log_and_buffer(
                    "INTEGRITY_NAKED",
                    trade_state.trade_id,
                    {"missing": "put", "remaining": "call", "source": source},
                )
                log_and_buffer(
                    "NAKED_POSITION",
                    trade_state.trade_id,
                    {"missing": "put", "remaining": "call", "source": source},
                )
                await self._emergency_close_remaining_leg(trade_state, "call")
                return False

            if integrity == "naked_put":
                logger.critical(
                    "[INTEGRITY] Trade %s: CALL position MISSING on Delta! "
                    "Closing put immediately.",
                    trade_state.trade_id,
                )
                log_and_buffer(
                    "INTEGRITY_NAKED",
                    trade_state.trade_id,
                    {"missing": "call", "remaining": "put", "source": source},
                )
                log_and_buffer(
                    "NAKED_POSITION",
                    trade_state.trade_id,
                    {"missing": "call", "remaining": "put", "source": source},
                )
                await self._emergency_close_remaining_leg(trade_state, "put")
                return False

            return True
        finally:
            self._integrity_in_progress.discard(trade_state.trade_id)

    async def _handle_manual_close(self, trade_state: TradeState) -> None:
        """Both legs gone on Delta — cancel orphan SLs and mark trade closed."""
        trade_id = trade_state.trade_id
        logger.warning(
            "[MANUAL_CLOSE] Handling manual close for trade %s",
            trade_id,
        )
        log_and_buffer(
            "MANUAL_EXCHANGE_CLOSE",
            trade_id,
            {"action": "cancel_sl_and_close"},
        )
        log_and_buffer(
            "INTEGRITY_MANUAL_CLOSE",
            trade_id,
            {"action": "handle"},
        )

        from backend.core.delta_sl import cancel_leg_sl_order
        from backend.engine.trade_reconcile import (
            book_leg_close,
            recompute_trade_realized_pnl,
            resolve_external_exit_fill,
        )

        now_utc = datetime.now(timezone.utc)
        closed_n = 0

        with self.db_factory() as db:
            open_legs = (
                db.query(Leg)
                .filter(Leg.trade_id == trade_id, Leg.status == "open")
                .all()
            )
            trade = db.query(Trade).filter(Trade.id == trade_id).first()

            for leg in open_legs:
                if self.delta_client is not None:
                    try:
                        await cancel_leg_sl_order(
                            self.delta_client, leg, clear_fields=True
                        )
                    except Exception as exc:
                        logger.warning("SL cancel failed: %s", exc)
                exit_px = await resolve_external_exit_fill(
                    self.delta_client, leg
                )
                if trade is not None:
                    book_leg_close(
                        leg=leg,
                        trade=trade,
                        exit_premium=exit_px,
                        exit_time=now_utc,
                    )
                else:
                    leg.status = "closed"
                    leg.exit_time = get_ist_now()
                    leg.exit_premium = exit_px
                closed_n += 1

            if trade is not None:
                recompute_trade_realized_pnl(db, trade)

            try:
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Could not persist manual exchange close trade=%s: %s",
                    trade_id,
                    exc,
                )
                db.rollback()

        logger.info(
            "[MANUAL_CLOSE] Trade %s master legs booked in DB (%s). "
            "Routing slaves + Trade.status through close_master_trade.",
            trade_id,
            closed_n,
        )

        # Mirror slaves + set Trade.status / mark_closed via single funnel
        with self.db_factory() as db:
            await self.close_master_trade(
                trade_id=trade_id,
                reason=ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value,
                db=db,
                skip_master_legs=True,
                trade_state=trade_state,
            )

    async def _emergency_close_remaining_leg(
        self, trade_state: TradeState, leg_to_close: str
    ) -> None:
        """
        One leg is missing (SL likely triggered).
        Close the remaining leg immediately to avoid naked exposure.
        """
        trade_id = trade_state.trade_id
        remaining = str(leg_to_close).lower().strip()
        missing = "put" if remaining == "call" else "call"

        # Double-check: never close remaining leg if adjustment in progress
        # (covers reconcile naked_risk and any other caller)
        live = self.position_tracker.get(trade_id)
        if getattr(trade_state, "is_adjusting", False) or (
            live is not None and getattr(live, "is_adjusting", False)
        ):
            logger.warning(
                "[EMERGENCY_CLOSE_BLOCKED] Trade#%s — is_adjusting=True, "
                "refusing emergency close of %s leg",
                trade_id,
                remaining,
            )
            return

        # DIAGNOSTIC — remove after is_adjusting race root-caused
        _live_c = live if live is not None else self.position_tracker.get(trade_id)
        logger.warning(
            "[DIAG_IS_ADJUSTING] (c) BEFORE EMERGENCY_CLOSE trade_id=%s "
            "closing=%s missing=%s | tracker.is_adjusting=%s "
            "trade_state.is_adjusting=%s",
            trade_id,
            remaining,
            missing,
            getattr(_live_c, "is_adjusting", None) if _live_c else None,
            getattr(trade_state, "is_adjusting", None),
        )

        remaining_leg_mem = (
            trade_state.call_leg if remaining == "call" else trade_state.put_leg
        )
        missing_leg_mem = (
            trade_state.put_leg if remaining == "call" else trade_state.call_leg
        )

        logger.critical(
            "[EMERGENCY_CLOSE] Trade %s: %s (%s) missing on Delta. "
            "Closing %s (%s) now.",
            trade_id,
            missing,
            getattr(missing_leg_mem, "symbol", "?"),
            remaining,
            getattr(remaining_leg_mem, "symbol", "?"),
        )
        log_and_buffer(
            "EMERGENCY_CLOSE",
            trade_id,
            {"closing": remaining, "missing": missing},
        )
        log_and_buffer(
            "INTEGRITY_NAKED",
            trade_id,
            {"closing": remaining, "missing": missing},
        )

        if self.delta_client is None:
            self._refresh_delta_client()
        assert self.delta_client is not None

        # Verify remaining still exists; if not → both gone
        rem_pid = int(getattr(remaining_leg_mem, "product_id", 0) or 0)
        remaining_exists = False
        if rem_pid > 0:
            try:
                remaining_exists = await self.delta_client.verify_position_exists(
                    rem_pid
                )
            except Exception as exc:
                logger.warning(
                    "[EMERGENCY_CLOSE] verify remaining failed: %s", exc
                )
                remaining_exists = True  # attempt close anyway

        if not remaining_exists:
            logger.warning(
                "[EMERGENCY_CLOSE] Remaining leg also gone from Delta! "
                "Treating as manual close."
            )
            await self._handle_manual_close(trade_state)
            return

        from backend.core.delta_sl import cancel_leg_sl_order
        from backend.engine.trade_reconcile import (
            book_leg_close,
            recompute_trade_realized_pnl,
            resolve_external_exit_fill,
        )

        now_utc = datetime.now(timezone.utc)
        close_ok = False
        filled = 0.0
        result: Any = None

        with self.db_factory() as db:
            legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            remaining_leg = next(
                (
                    leg
                    for leg in legs
                    if str(leg.leg_type).lower() == remaining
                    and str(leg.status).lower() == "open"
                ),
                None,
            )
            missing_leg = next(
                (
                    leg
                    for leg in legs
                    if str(leg.leg_type).lower() == missing
                    and str(leg.status).lower() == "open"
                ),
                None,
            )
            trade = db.query(Trade).filter(Trade.id == trade_id).first()

            # Book missing leg as externally closed — real fill, never 0.0
            if missing_leg is not None and trade is not None:
                await cancel_leg_sl_order(
                    self.delta_client, missing_leg, clear_fields=True
                )
                missing_px = await resolve_external_exit_fill(
                    self.delta_client, missing_leg
                )
                book_leg_close(
                    leg=missing_leg,
                    trade=trade,
                    exit_premium=missing_px,
                    exit_time=now_utc,
                )

            if remaining_leg is not None:
                await cancel_leg_sl_order(
                    self.delta_client, remaining_leg, clear_fields=True
                )
                result = await self.order_executor.close_leg(
                    remaining_leg, self.delta_client
                )
                if result.success:
                    close_ok = True
                    filled = float(result.filled_price or 0)
                    logger.info(
                        "Emergency close SUCCESS: %s @ %s", remaining, filled
                    )
                    if trade is not None:
                        book_leg_close(
                            leg=remaining_leg,
                            trade=trade,
                            exit_premium=filled if filled > 0 else None,
                            exit_time=now_utc,
                            exit_fee_usd=(
                                float(result.commission)
                                if result.commission is not None
                                else None
                            ),
                            exit_order_id=(
                                str(result.order_id)
                                if result.order_id is not None
                                else None
                            ),
                        )
                else:
                    logger.critical(
                        "Emergency close FAILED: %s", result.error
                    )
                    log_and_buffer(
                        "EXIT_FAIL",
                        trade_id,
                        {
                            "reason": "EMERGENCY_CLOSE",
                            "error": result.error,
                        },
                    )
                    # Still mark closed in DB to stop monitoring loops;
                    # surface manual action if Delta still holds size.
                    if trade is not None:
                        remaining_leg.status = "closed"
                        remaining_leg.exit_time = now_utc
                        remaining_leg.exit_premium = None
                        remaining_leg.realized_pnl = None
                        note_tag = f"PNL_UNRESOLVED_{remaining}"
                        prior_notes = str(getattr(trade, "notes", None) or "")
                        if note_tag not in prior_notes:
                            trade.notes = (
                                f"{prior_notes};{note_tag}".strip(";")
                                if prior_notes
                                else note_tag
                            )
            # Close any other open legs (incl. tracked hedge — not treated as orphan)
            other_open = (
                db.query(Leg)
                .filter(Leg.trade_id == trade_id, Leg.status == "open")
                .all()
            )
            for leftover in other_open:
                is_long = bool(getattr(leftover, "is_long", False)) or str(
                    getattr(leftover, "leg_type", "")
                ).startswith("hedge")
                exit_px: float | None = None
                if self.delta_client is not None:
                    try:
                        if is_long:
                            close_res = await self.order_executor.close_long_position(
                                product_id=int(leftover.product_id),
                                quantity=int(leftover.quantity),
                                delta_client=self.delta_client,
                                symbol_for_fallback=str(leftover.symbol),
                            )
                        else:
                            close_res = await self.order_executor.close_leg(
                                leftover, self.delta_client
                            )
                        if close_res.success:
                            px = float(close_res.filled_price or 0)
                            exit_px = px if px > 0 else None
                    except Exception as exc:
                        logger.warning(
                            "Emergency leftover close failed %s: %s",
                            leftover.symbol,
                            exc,
                        )
                if trade is not None:
                    book_leg_close(
                        leg=leftover,
                        trade=trade,
                        exit_premium=exit_px,
                        exit_time=now_utc,
                    )
                else:
                    leftover.status = "closed"
                    leftover.exit_time = get_ist_now()
                    leftover.exit_premium = exit_px

            if trade is not None:
                recompute_trade_realized_pnl(db, trade)

            try:
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Could not persist emergency close trade=%s: %s",
                    trade_id,
                    exc,
                )
                db.rollback()

            remaining_open = (
                db.query(Leg)
                .filter(Leg.trade_id == trade_id, Leg.status == "open")
                .count()
            )
            if remaining_open > 0:
                logger.critical(
                    "[EMERGENCY_CLOSE] %s legs still open in DB after "
                    "emergency close! DB inconsistency!",
                    remaining_open,
                )
            else:
                logger.info("[EMERGENCY_CLOSE] All legs closed in DB")

        # Mirror slaves + Trade.status / mark_closed via funnel (master legs done)
        with self.db_factory() as db:
            await self.close_master_trade(
                trade_id=trade_id,
                reason=ExitReason.SL_TRIGGERED_EMERGENCY_CLOSE.value,
                db=db,
                skip_master_legs=True,
                trade_state=trade_state,
            )

        await ws_manager.broadcast(
            {
                "type": "ERROR",
                "trade_id": trade_id,
                "message": (
                    f"⚠️ Delta SL triggered on {missing} leg! "
                    f"Emergency closed {remaining} to prevent naked position. "
                    f"Trade fully closed."
                    + ("" if close_ok else " (close may need manual check)")
                ),
                "requires_manual_action": not close_ok,
                "severity": "WARNING" if close_ok else "CRITICAL",
            }
        )
        logger.info(
            "[EMERGENCY_CLOSE] Complete for trade %s. "
            "remaining_leg_close: success=%s fill=%s",
            trade_id,
            close_ok,
            filled,
        )

    def _maybe_schedule_auto_reentry(self, underlying: str) -> None:
        """Schedule auto re-entry if AutoTradeEngine is enabled for this underlying."""
        if not getattr(self, "auto_trade_engine", None):
            return
        if not underlying:
            return
        try:
            from backend.database import get_or_create_auto_settings

            with self.db_factory() as db:
                settings = get_or_create_auto_settings(db)
                if not settings.is_enabled:
                    return
                if str(settings.underlying).upper() != str(underlying).upper():
                    return
                delay = int(settings.re_entry_delay_minutes or 1)
                self.auto_trade_engine.schedule_reentry(underlying, delay)
                logger.info(
                    "Auto re-entry scheduled for %s in %s min",
                    underlying,
                    delay,
                )
        except Exception as exc:
            logger.warning("Could not schedule auto re-entry: %s", exc)

    async def _process_trade(self, trade_state: TradeState) -> None:
        assert self.delta_client is not None
        trade_id = trade_state.trade_id

        # Never run monitor/integrity while an adjustment holds the lock
        live = self.position_tracker.get(trade_id)
        if (live is not None and getattr(live, "is_adjusting", False)) or getattr(
            trade_state, "is_adjusting", False
        ):
            logger.debug(
                "[MONITOR_SKIP] Trade#%s — adjustment in progress",
                trade_id,
            )
            return

        # Recover legs wrongly closed by pre-fix emergency race (≤1/min)
        await self._maybe_recover_legs(trade_state)

        call_leg = trade_state.call_leg
        put_leg = trade_state.put_leg
        trade = trade_state.trade
        call_open = str(getattr(call_leg, "status", "open")).lower() == "open"
        put_open = str(getattr(put_leg, "status", "open")).lower() == "open"

        if call_open:
            call_premium = await self._get_premium(str(call_leg.symbol))
        else:
            call_premium = float(
                getattr(call_leg, "exit_premium", None)
                or call_leg.initial_premium
                or 0.0
            )
        if put_open:
            put_premium = await self._get_premium(str(put_leg.symbol))
        else:
            put_premium = float(
                getattr(put_leg, "exit_premium", None)
                or put_leg.initial_premium
                or 0.0
            )

        # STEP 0: Position integrity vs Delta (every cycle)
        if not await self._enforce_position_integrity(
            trade_state, source="monitor_tick"
        ):
            return

        settling = get_settling_info(getattr(trade, "monitoring_starts_at", None))
        is_settling = bool(settling.get("is_settling"))
        if is_settling:
            log_and_buffer(
                "SETTLING",
                trade_id,
                {
                    "minutes_left": settling.get("settling_minutes_left"),
                    "ends_at": settling.get("settling_ends_at"),
                },
            )

        log_and_buffer(
            "MONITOR_TICK",
            trade_id,
            {
                "call_symbol": call_leg.symbol,
                "put_symbol": put_leg.symbol,
                "call_premium": round(call_premium, 2),
                "put_premium": round(put_premium, 2),
                "call_entry": round(float(call_leg.initial_premium), 2),
                "put_entry": round(float(put_leg.initial_premium), 2),
                "call_open": call_open,
                "put_open": put_open,
                "is_settling": is_settling,
                "is_demo": bool(getattr(trade, "is_demo", False)),
            },
        )

        # Step 1–2: Delta UPL @offer for ALL open legs (shorts + long hedge)
        call_mtm = 0.0
        put_mtm = 0.0
        hedge_upnl = 0.0
        delta_upnl = 0.0
        mtm_available = False
        realized = float(getattr(trade, "realized_pnl", None) or 0.0)
        call_offer = call_premium
        put_offer = put_premium
        trade_is_demo = bool(getattr(trade, "is_demo", False))

        with self.db_factory() as _db_legs:
            all_open_legs = (
                _db_legs.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            # Detach so objects remain usable after session closes
            for _leg in all_open_legs:
                _db_legs.expunge(_leg)

        # Demo trades: no Delta positions — P&L from live mark prices only
        # demo_upnl = (entry - mark) × qty × 0.001 for shorts
        if trade_is_demo:
            delta_upnl = 0.0
            hedge_upnl = 0.0
            call_mtm = 0.0
            put_mtm = 0.0
            for leg in all_open_legs:
                lt = str(leg.leg_type or "").lower()
                is_long = bool(getattr(leg, "is_long", False)) or lt.startswith(
                    "hedge"
                )
                try:
                    px = float(
                        await self.delta_client.get_mark_price(str(leg.symbol))
                    )
                except Exception:
                    px = 0.0
                if px <= 0:
                    px = float(
                        self._live_prices.get(str(leg.symbol))
                        or leg.initial_premium
                        or 0
                    )
                entry = float(leg.initial_premium or 0)
                qty = abs(int(leg.quantity or 0))
                if is_long:
                    leg_upnl = (px - entry) * qty * float(OPTIONS_CONTRACT_VALUE)
                else:
                    leg_upnl = (entry - px) * qty * float(OPTIONS_CONTRACT_VALUE)
                delta_upnl += leg_upnl
                if lt == "call":
                    call_mtm = leg_upnl
                    call_premium = px
                    call_offer = px
                elif lt == "put":
                    put_mtm = leg_upnl
                    put_premium = px
                    put_offer = px
                elif is_long:
                    hedge_upnl = leg_upnl
                self._live_prices[str(leg.symbol)] = px
            self.position_tracker.update_delta_mtm(trade_id, delta_upnl)
            mtm_available = True
            log_and_buffer(
                "DEMO_PNL",
                trade_id,
                {
                    "call_mark": round(call_premium, 2),
                    "put_mark": round(put_premium, 2),
                    "call_upnl": round(call_mtm, 4),
                    "put_upnl": round(put_mtm, 4),
                    "hedge_upnl": round(hedge_upnl, 4),
                    "delta_upnl": round(delta_upnl, 4),
                    "realized": round(realized, 4),
                },
            )
            logger.info(
                "[DEMO] Trade %s P&L from marks: call=%.4f put=%.4f "
                "hedge=%.4f total_upnl=%.4f",
                trade_id,
                call_mtm,
                put_mtm,
                hedge_upnl,
                delta_upnl,
            )

        try:
            if not trade_is_demo:
                pids: list[int] = [
                    int(leg.product_id)
                    for leg in all_open_legs
                    if int(getattr(leg, "product_id", 0) or 0) > 0
                ]
                if pids:
                    upnl_data = await self.delta_client.get_positions_upnl(pids)
                    any_hit = False
                    for leg in all_open_legs:
                        pid = int(leg.product_id)
                        row = upnl_data.get(pid) or {}
                        if pid not in upnl_data:
                            continue
                        any_hit = True
                        leg_upnl = float(row.get("upnl") or 0.0)
                        delta_upnl += leg_upnl
                        lt = str(leg.leg_type or "").lower()
                        best = float(row.get("best_offer") or 0)
                        if lt == "call":
                            call_mtm = leg_upnl
                            if best > 0:
                                call_offer = best
                                call_premium = call_offer
                                self._live_prices[str(leg.symbol)] = call_premium
                        elif lt == "put":
                            put_mtm = leg_upnl
                            if best > 0:
                                put_offer = best
                                put_premium = put_offer
                                self._live_prices[str(leg.symbol)] = put_premium
                        elif bool(getattr(leg, "is_long", False)) or lt.startswith(
                            "hedge"
                        ):
                            hedge_upnl = leg_upnl
                            if best > 0:
                                self._live_prices[str(leg.symbol)] = best

                    if any_hit:
                        self.position_tracker.update_delta_mtm(trade_id, delta_upnl)
                        mtm_available = True
                        logger.info(
                            "Trade %s P&L: call_upnl=%.4f put_upnl=%.4f "
                            "hedge_upnl=%.4f delta_upnl=%.4f realized=%.4f "
                            "total=%.4f call_offer=%.2f put_offer=%.2f "
                            "target=%s sl=%s",
                            trade_id,
                            call_mtm,
                            put_mtm,
                            hedge_upnl,
                            delta_upnl,
                            realized,
                            realized + delta_upnl,
                            call_offer,
                            put_offer,
                            getattr(trade, "profit_target_usd", 0),
                            getattr(trade, "stoploss_usd", 0),
                        )
                    else:
                        logger.warning(
                            "Trade %s: no Delta positions for open product_ids %s",
                            trade_id,
                            pids,
                        )
        except Exception as exc:
            logger.warning(
                "Delta UPL@offer fetch failed for trade %s — fallback: %s",
                trade_id,
                exc,
            )
            log_and_buffer(
                "ERROR",
                trade_id,
                {"stage": "mtm_fetch", "error": str(exc)},
            )

        # Fallback: compute UPNL for every open leg (short vs long)
        if not mtm_available:
            delta_upnl = 0.0
            hedge_upnl = 0.0
            call_mtm = 0.0
            put_mtm = 0.0
            for leg in all_open_legs:
                lt = str(leg.leg_type or "").lower()
                is_long = bool(getattr(leg, "is_long", False)) or lt.startswith(
                    "hedge"
                )
                try:
                    if is_long:
                        px = float(
                            await self.delta_client.get_long_exit_price(
                                str(leg.symbol)
                            )
                            or 0
                        )
                    else:
                        px = float(
                            await self.delta_client.get_short_exit_price(
                                str(leg.symbol)
                            )
                            or 0
                        )
                except Exception:
                    px = 0.0
                if px <= 0:
                    px = float(leg.initial_premium or 0)
                entry = float(leg.initial_premium or 0)
                qty = abs(int(leg.quantity or 0))
                if is_long:
                    leg_upnl = (px - entry) * qty * float(OPTIONS_CONTRACT_VALUE)
                else:
                    leg_upnl = (entry - px) * qty * float(OPTIONS_CONTRACT_VALUE)
                delta_upnl += leg_upnl
                if lt == "call":
                    call_mtm = leg_upnl
                    call_premium = px
                elif lt == "put":
                    put_mtm = leg_upnl
                    put_premium = px
                elif is_long:
                    hedge_upnl = leg_upnl

        total_pnl = realized + delta_upnl
        target = float(getattr(trade, "profit_target_usd", 0) or 0)
        stoploss = float(getattr(trade, "stoploss_usd", 0) or 0)
        pnl_pct = (total_pnl / target * 100.0) if target else 0.0
        slip_pct = float(getattr(trade, "slippage_pct", None) or 2.0)

        # Net MTM for exit decisions (same as frontend display)
        from backend.core.fees import (
            basket_fees_paid_from_legs,
            compute_net_mtm,
            estimate_expected_exit_spread_usd,
            estimate_option_trading_fee,
            get_entry_spread_for_sl,
        )
        from backend.models import Leg as LegModel

        fees_paid = 0.0
        est_exit = 0.0
        expected_exit_spread = 0.0
        with self.db_factory() as db:
            legs = (
                db.query(LegModel)
                .filter(
                    LegModel.trade_id == trade_id,
                    LegModel.is_bot_managed.is_(True),
                )
                .all()
            )
            fees_paid = basket_fees_paid_from_legs(legs)
            btc = float(self._btc_spot or 0)
            for leg in legs:
                if str(getattr(leg, "status", "") or "").lower() != "open":
                    continue
                lt = str(leg.leg_type or "").lower()
                if lt == "call":
                    offer = float(call_premium)
                elif lt == "put":
                    offer = float(put_premium)
                else:
                    offer = float(
                        self._live_prices.get(str(leg.symbol), 0)
                        or leg.initial_premium
                        or 0
                    )
                if offer > 0 and btc > 0:
                    est_exit += estimate_option_trading_fee(
                        option_price=offer,
                        quantity_lots=int(leg.quantity or 0),
                        btc_index_price=btc,
                    )
                if offer > 0:
                    expected_exit_spread += estimate_expected_exit_spread_usd(
                        offer_price=offer,
                        quantity=int(leg.quantity or 0),
                    )

        # Gross MTM for stoploss: add back latest entry-event spread only
        # (reset on each adjustment — never cumulative across adjusts)
        entry_spread_for_sl = get_entry_spread_for_sl(trade)
        gross_mtm_for_stoploss = total_pnl + entry_spread_for_sl

        slip_fields = compute_net_mtm(
            gross_mtm=total_pnl,
            fees_paid=fees_paid,
            est_exit_fees=est_exit,
            slippage_pct=slip_pct,
            expected_exit_spread_usd=expected_exit_spread,
        )
        net_mtm_val = float(slip_fields["net_mtm"])
        slippage_amount = float(slip_fields["slippage_amount"])

        pnl_log: dict[str, Any] = {
            "realized_pnl": round(realized, 4),
            "delta_upnl": round(delta_upnl, 4),
            "call_upnl": round(call_mtm, 4),
            "put_upnl": round(put_mtm, 4),
            "gross_mtm": round(total_pnl, 4),
            "gross_mtm_for_stoploss": round(gross_mtm_for_stoploss, 4),
            "entry_spread_for_sl": round(entry_spread_for_sl, 4),
            "fees_paid": round(fees_paid, 4),
            "est_exit_fees": round(est_exit, 4),
            "expected_exit_spread_usd": round(expected_exit_spread, 4),
            "slippage_pct": slip_pct,
            "slippage_amount": round(slippage_amount, 4),
            "net_mtm": round(net_mtm_val, 4),
            "total_pnl": round(total_pnl, 4),
            "profit_target": target,
            "stoploss": stoploss,
            "pnl_pct": round(pnl_pct, 1),
            "will_exit_profit": net_mtm_val >= target if target else False,
            "will_exit_stoploss": (
                gross_mtm_for_stoploss <= -stoploss if stoploss else False
            ),
            "mtm_source": "delta_position" if mtm_available else "computed_fallback",
            "contract_value": OPTIONS_CONTRACT_VALUE,
        }
        if bool(getattr(trade, "in_conversion_mode", False)):
            pnl_log["hedge_upnl"] = round(hedge_upnl, 4)
        log_and_buffer("PNL_CHECK", trade_id, pnl_log)

        with self.db_factory() as db:
            # Refresh combined_trigger_mode from SQLite onto in-memory trade
            # BEFORE on_tick — tracker copy goes stale after AutoTrade toggle.
            try:
                row = (
                    db.query(Trade)
                    .filter(Trade.id == trade_id)
                    .first()
                )
                if row is not None:
                    trade.combined_trigger_mode = bool(
                        getattr(row, "combined_trigger_mode", False)
                    )
            except Exception as refresh_exc:
                logger.warning(
                    "[COMBINED_TRIGGER_MODE] pre-tick refresh failed "
                    "trade=%s: %s",
                    trade_id,
                    refresh_exc,
                )
            call_trig_pct = float(
                self.strategy.get_trigger_for_leg(call_premium, trade, db)
            )
            put_trig_pct = float(
                self.strategy.get_trigger_for_leg(put_premium, trade, db)
            )
            trigger_pct = call_trig_pct
            premium_slabs = None
            if str(getattr(trade, "trigger_mode", "") or "").lower() == "premium":
                premium_slabs = self.strategy.get_slabs(trade.id, db)
            action = await self.strategy.on_tick(
                trade,
                call_leg,
                put_leg,
                call_premium,
                put_premium,
                db,
                realized_pnl=realized,
                delta_mtm=delta_upnl,
                net_mtm=net_mtm_val,
                slippage_pct=slip_pct,
                gross_mtm_for_sl=gross_mtm_for_stoploss,
            )
            if float(getattr(action, "call_trigger_pct", 0) or 0) > 0:
                call_trig_pct = float(action.call_trigger_pct)
            if float(getattr(action, "put_trigger_pct", 0) or 0) > 0:
                put_trig_pct = float(action.put_trigger_pct)
            trigger_for_plan = float(getattr(action, "trigger_pct_used", 0) or 0)
            if trigger_for_plan <= 0:
                trigger_for_plan = trigger_pct

        def _trig_base(leg: Any) -> float:
            for attr in ("trigger_baseline_premium", "trigger_premium"):
                val = getattr(leg, attr, None)
                if val is not None and float(val) > 0:
                    return float(val)
            return float(getattr(leg, "initial_premium", 0) or 0)

        call_trigger = _trig_base(call_leg) * (call_trig_pct / 100.0)
        put_trigger = _trig_base(put_leg) * (put_trig_pct / 100.0)
        call_pct = (call_premium / call_trigger * 100.0) if call_trigger > 0 else 0.0
        put_pct = (put_premium / put_trigger * 100.0) if put_trigger > 0 else 0.0
        if action.should_exit:
            action_label = action.exit_reason or "EXIT"
        elif action.should_adjust and action.adjust_leg:
            action_label = f"ADJUST_{action.adjust_leg}"
        else:
            action_label = "HOLD"

        mode = str(getattr(trade, "trigger_mode", "slab") or "slab").lower()
        trigger_details: dict[str, Any] = {
            "trigger_mode": mode,
            "combined_trigger_mode": bool(
                getattr(trade, "combined_trigger_mode", False)
            ),
            "trigger_pct": trigger_for_plan,
            "call_trigger_pct": round(call_trig_pct, 1),
            "put_trigger_pct": round(put_trig_pct, 1),
            "call_trigger_at": round(call_trigger, 2),
            "put_trigger_at": round(put_trigger, 2),
            "call_current": round(call_premium, 2),
            "call_pct_to_trigger": round(call_pct, 1),
            "put_current": round(put_premium, 2),
            "put_pct_to_trigger": round(put_pct, 1),
            "action": action_label,
        }
        if mode == "premium":
            from backend.core.time_utils import premium_slab_band_label

            trigger_details["call_premium_band"] = premium_slab_band_label(
                call_premium
            )
            trigger_details["put_premium_band"] = premium_slab_band_label(put_premium)
            trigger_details["trigger_pct_note"] = (
                f"call {call_trig_pct:.0f}% ({premium_slab_band_label(call_premium)}); "
                f"put {put_trig_pct:.0f}% ({premium_slab_band_label(put_premium)})"
            )

        log_and_buffer("TRIGGER_CHECK", trade_id, trigger_details)

        # Decision at trigger: profitable close vs adjust
        triggered_leg = getattr(action, "triggered_leg", None)
        if triggered_leg:
            net_for_decision = float(getattr(action, "current_pnl", 0) or 0)
            is_close = bool(
                action.should_exit
                and action.exit_reason
                == ExitReason.DECISION_PROFIT_AT_TRIGGER.value
            )
            decision = "CLOSE_PROFITABLE" if is_close else "ADJUST"
            reason_txt = (
                "Net MTM positive — booking profit"
                if is_close
                else "Net MTM negative — adjusting"
            )
            log_and_buffer(
                "DECISION_TRIGGER",
                trade_id,
                {
                    "leg": triggered_leg,
                    "trigger_pct": float(
                        getattr(action, "trigger_pct_hit", 0)
                        or trigger_for_plan
                        or 0
                    ),
                    "net_mtm": round(net_for_decision, 4),
                    "decision": decision,
                    "reason": reason_txt,
                },
            )

        self.position_tracker.update_premiums(
            trade_id,
            call_premium,
            put_premium,
            total_pnl,
        )
        self.position_tracker.update_delta_mtm(trade_id, delta_upnl)
        self.position_tracker.update_net_mtm(trade_id, net_mtm_val)

        call_repl, put_repl = await self._estimate_replacements(
            trade_state, call_premium, put_premium
        )

        # --- CONVERSION MODE MONITORING ---
        # If in conversion mode, check for reversal before normal adjustment logic
        conversion_active = bool(
            getattr(trade_state.trade, "in_conversion_mode", False)
        )
        if conversion_active:
            reversed_ok = await self._check_conversion_mode_exit(
                trade_state, call_premium, put_premium
            )
            if reversed_ok:
                conversion_active = False
                logger.info(
                    "[CONVERSION_REVERSAL] Trade %s normal mode resumed "
                    "— adjustment monitoring active from next tick",
                    trade_state.trade_id,
                )
                # Fall through to normal push_update / exit checks.
                # Next tick will run full trigger check.
            # else: stay in conversion mode (hedge still open)

        if action.should_exit:
            exit_reason = str(action.exit_reason or "UNKNOWN")
            # Safety valve: never honor STOPLOSS if gross MTM for SL is still
            # inside the limit (guards against any net_mtm misuse in strategy).
            if exit_reason == "STOPLOSS" and stoploss > 0:
                if gross_mtm_for_stoploss > -stoploss:
                    logger.critical(
                        "[SL_FALSE_TRIGGER_BLOCKED] Trade#%s — strategy "
                        "returned STOPLOSS but gross_mtm_for_stoploss=%.4f "
                        "> -stoploss=%.4f (net_mtm=%.4f). Ignoring SL exit.",
                        trade_id,
                        gross_mtm_for_stoploss,
                        -stoploss,
                        net_mtm_val,
                    )
                    log_and_buffer(
                        "SL_FALSE_TRIGGER_BLOCKED",
                        trade_id,
                        {
                            "gross_mtm_for_stoploss": round(
                                gross_mtm_for_stoploss, 4
                            ),
                            "net_mtm": round(net_mtm_val, 4),
                            "stoploss": stoploss,
                        },
                    )
                    action.should_exit = False
                    action.exit_reason = None

        if action.should_exit:
            exit_pnl = float(getattr(action, "current_pnl", net_mtm_val) or net_mtm_val)
            await self._exit_trade(
                trade_state,
                action.exit_reason or "UNKNOWN",
                total_pnl=exit_pnl,
                gross_mtm=total_pnl,
                fees_paid=fees_paid,
                est_exit_fees=est_exit,
                slippage_amount=slippage_amount,
                net_mtm=net_mtm_val,
            )
        elif conversion_active:
            # Stay in conversion — push live premiums, do not adjust
            if action.should_adjust and action.adjust_leg:
                logger.info(
                    "[CONVERSION_HOLD] Trade %s: %s trigger suppressed "
                    "(conversion mode active — waiting for reversal)",
                    trade_state.trade_id,
                    action.adjust_leg,
                )
                log_and_buffer(
                    "CONVERSION_HOLD",
                    trade_state.trade_id,
                    {"suppressed_leg": action.adjust_leg},
                )
            await self._push_update(
                trade_state,
                call_premium,
                put_premium,
                total_pnl,
                delta_upnl,
                call_mtm,
                put_mtm,
                trigger_pct=trigger_for_plan,
                call_replacement=call_repl,
                put_replacement=put_repl,
                call_trigger_pct=call_trig_pct,
                put_trigger_pct=put_trig_pct,
                premium_slabs=premium_slabs,
            )
            try:
                import backend.engine.mirror_engine as mirror_mod

                if (
                    mirror_mod.mirror_engine is not None
                    and hasattr(mirror_mod.mirror_engine, "update_all_slave_mtm")
                ):
                    asyncio.create_task(
                        mirror_mod.mirror_engine.update_all_slave_mtm(
                            trade_state.trade_id
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Slave MTM update queue failed trade=%s: %s",
                    trade_state.trade_id,
                    exc,
                )
        elif action.should_adjust and action.adjust_leg:
            await self._adjust_trade(trade_state, action.adjust_leg)
        else:
            await self._push_update(
                trade_state,
                call_premium,
                put_premium,
                total_pnl,
                delta_upnl,
                call_mtm,
                put_mtm,
                trigger_pct=trigger_for_plan,
                call_replacement=call_repl,
                put_replacement=put_repl,
                call_trigger_pct=call_trig_pct,
                put_trigger_pct=put_trig_pct,
                premium_slabs=premium_slabs,
            )

            # Update slave MTM from each slave's Delta positions every cycle
            try:
                import backend.engine.mirror_engine as mirror_mod

                if (
                    mirror_mod.mirror_engine is not None
                    and hasattr(mirror_mod.mirror_engine, "update_all_slave_mtm")
                ):
                    asyncio.create_task(
                        mirror_mod.mirror_engine.update_all_slave_mtm(
                            trade_state.trade_id
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Slave MTM update queue failed trade=%s: %s",
                    trade_state.trade_id,
                    exc,
                )

    @staticmethod
    def _emit_exit_funnel(
        trade_id: int,
        reason: str,
        result: dict[str, int],
        *,
        note: str | None = None,
    ) -> None:
        """Emit [EXIT_FUNNEL] via bot_activity (same path as EXIT_START/VERIFY)."""
        details: dict[str, Any] = {
            "reason": reason,
            "slaves_total": int(result.get("slaves_total") or 0),
            "slaves_closed": int(result.get("slaves_closed") or 0),
            "slaves_failed": int(result.get("slaves_failed") or 0),
            "master_legs_closed": int(result.get("master_legs_closed") or 0),
        }
        if note:
            details["note"] = note
        log_and_buffer("EXIT_FUNNEL", int(trade_id), details)

    async def _close_master_trade_db_only(
        self,
        trade_id: int,
        reason: str,
        db: Any,
    ) -> dict[str, int]:
        """
        Funnel path when TradeState is gone (reconcile / external close).

        a. mirror_exit
        b. skip master legs (already flat)
        c. set Trade.status + exit_reason + exit_time
        d. mark_closed
        e. return slave counts
        """
        from backend.models import SlaveTrade as _SlaveTradeExit

        trade_id = int(trade_id)
        slaves_total = 0
        slaves_closed = 0
        slaves_failed = 0
        pre_slave_ids: set[int] = set()

        log_and_buffer(
            "EXIT_START",
            trade_id,
            {"reason": reason, "path": "db_only"},
        )
        logger.info(
            "[EXIT_START] Trade %s: reason=%s (db-only funnel, no TradeState)",
            trade_id,
            reason,
        )

        # Resolve hint product ids from remaining/closed legs
        call_pid = 0
        put_pid = 0
        hedge_pid = None
        with self.db_factory() as load_db:
            legs = (
                load_db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            for leg in legs:
                lt = str(getattr(leg, "leg_type", "") or "").lower()
                pid = int(getattr(leg, "product_id", 0) or 0)
                if lt == "call" and pid > 0:
                    call_pid = pid
                elif lt == "put" and pid > 0:
                    put_pid = pid
                elif (
                    bool(getattr(leg, "is_long", False))
                    or lt.startswith("hedge")
                ) and pid > 0:
                    hedge_pid = pid

        try:
            pre_slave_rows = (
                db.query(_SlaveTradeExit)
                .filter(
                    _SlaveTradeExit.master_trade_id == trade_id,
                    _SlaveTradeExit.status != "closed",
                )
                .all()
            )
            slaves_total = len(pre_slave_rows)
            pre_slave_ids = {int(st.id) for st in pre_slave_rows}
        except Exception as slave_count_exc:
            logger.warning(
                "[EXIT_FUNNEL] pre-mirror slave count failed: %s",
                slave_count_exc,
            )

        try:
            import backend.engine.mirror_engine as mirror_module

            me = mirror_module.mirror_engine or self.mirror_engine
            if me is not None:
                await me.mirror_exit(
                    master_trade_id=trade_id,
                    call_product_id=call_pid,
                    put_product_id=put_pid,
                    reason=reason,
                    hedge_product_id=hedge_pid,
                )
                log_and_buffer(
                    "MIRROR_EXIT",
                    trade_id,
                    {
                        "stage": "awaited_complete",
                        "path": "db_only",
                        "reason": reason,
                    },
                )
            else:
                log_and_buffer(
                    "MIRROR_EXIT",
                    trade_id,
                    {
                        "stage": "engine_none",
                        "path": "db_only",
                        "reason": reason,
                    },
                )
                logger.warning(
                    "[MIRROR_EXIT] mirror_engine is None — slaves not closed "
                    "for trade %s",
                    trade_id,
                )
        except Exception as exc:
            log_and_buffer(
                "MIRROR_EXIT",
                trade_id,
                {
                    "stage": "failed",
                    "path": "db_only",
                    "reason": reason,
                    "error": str(exc),
                },
            )
            logger.warning(
                "[MIRROR_EXIT] Failed for trade %s (non-fatal): %s",
                trade_id,
                exc,
                exc_info=True,
            )

        try:
            db.expire_all()
            if pre_slave_ids:
                post_rows = (
                    db.query(_SlaveTradeExit)
                    .filter(_SlaveTradeExit.id.in_(pre_slave_ids))
                    .all()
                )
                slaves_closed = sum(
                    1 for st in post_rows if str(st.status) == "closed"
                )
                slaves_failed = max(0, slaves_total - slaves_closed)
        except Exception as slave_post_exc:
            logger.warning(
                "[EXIT_FUNNEL] post-mirror slave count failed: %s",
                slave_post_exc,
            )
            slaves_failed = slaves_total

        status = self._status_for_reason(reason)
        trade_row = db.query(Trade).filter(Trade.id == trade_id).first()
        if trade_row is not None:
            from backend.engine.trade_reconcile import (
                pnl_sanity_check,
                recompute_trade_realized_pnl,
            )

            trade_row.status = status
            trade_row.exit_time = get_ist_now()
            trade_row.exit_reason = reason
            final_pnl = recompute_trade_realized_pnl(db, trade_row)
            legs_for_sanity = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            last_gross = None
            state = self.position_tracker.get(trade_id)
            if state is not None:
                last_gross = float(
                    getattr(state, "last_delta_mtm", None)
                    or getattr(state, "last_pnl", None)
                    or 0
                )
            pnl_sanity_check(
                trade_id=trade_id,
                realized_pnl=final_pnl,
                last_gross_mtm=last_gross,
                legs=legs_for_sanity,
            )
            trade_row.in_conversion_mode = False
            db.commit()
        else:
            log_and_buffer(
                "EXIT_FUNNEL",
                trade_id,
                {
                    "reason": reason,
                    "note": "missing_trade_row_db_only",
                    "slaves_total": slaves_total,
                    "slaves_closed": slaves_closed,
                    "slaves_failed": slaves_failed,
                    "master_legs_closed": 0,
                },
            )
            logger.error(
                "[EXIT_FUNNEL] trade_id=%s missing Trade row in db-only path",
                trade_id,
            )

        self.position_tracker.mark_closed(trade_id)
        self._maybe_schedule_auto_reentry(
            str(getattr(trade_row, "underlying", "") or "")
            if trade_row is not None
            else ""
        )

        try:
            from backend.api.routes_account import cleanup_orphan_sl_orders

            with self.db_factory() as orphan_db:
                await cleanup_orphan_sl_orders(orphan_db, trade_id=trade_id)
        except Exception as orphan_exc:
            logger.warning(
                "[ORPHAN_SL] db-only post-exit sweep failed: %s", orphan_exc
            )

        result = {
            "slaves_total": slaves_total,
            "slaves_closed": slaves_closed,
            "slaves_failed": slaves_failed,
            "master_legs_closed": 0,
        }
        self._emit_exit_funnel(trade_id, reason, result, note="db_only")
        return result

    async def close_master_trade(
        self,
        trade_id: int,
        reason: str,
        db: Any,
        skip_master_legs: bool = False,
        *,
        trade_state: TradeState | None = None,
        total_pnl: float | None = None,
        gross_mtm: float | None = None,
        fees_paid: float | None = None,
        est_exit_fees: float | None = None,
        slippage_amount: float | None = None,
        net_mtm: float | None = None,
    ) -> dict[str, int]:
        """
        Single funnel for closing a master trade.

        Order (do not change):
          a. await mirror_exit
          b. close master legs (unless skip_master_legs)
          c. set Trade.status closed + exit_reason + exit_time
          d. position_tracker.mark_closed
          e. return slave/leg close counts

        Protected by a per-trade asyncio.Lock so a second exit path cannot
        overwrite fills or double-run the funnel.
        """
        tid = int(trade_id)
        lock = self._get_exit_lock(tid)
        async with lock:
            # After waiting: if another funnel already closed this trade, skip.
            with self.db_factory() as check_db:
                row = (
                    check_db.query(Trade).filter(Trade.id == tid).first()
                )
                if row is not None:
                    st = str(row.status or "").lower()
                    if st != TradeStatus.ACTIVE.value:
                        existing_reason = str(
                            getattr(row, "exit_reason", None) or ""
                        )
                        logger.warning(
                            "[EXIT_SKIP] trade_id=%s reason=%s "
                            "existing_reason=%s status=%s — no orders, no DB writes",
                            tid,
                            reason,
                            existing_reason,
                            st,
                        )
                        log_and_buffer(
                            "EXIT_SKIP",
                            tid,
                            {
                                "reason": reason,
                                "existing_reason": existing_reason,
                                "status": st,
                            },
                        )
                        return {
                            "slaves_total": 0,
                            "slaves_closed": 0,
                            "slaves_failed": 0,
                            "master_legs_closed": 0,
                            "skipped": 1,
                        }

            return await self._close_master_trade_locked(
                trade_id=tid,
                reason=reason,
                db=db,
                skip_master_legs=skip_master_legs,
                trade_state=trade_state,
                total_pnl=total_pnl,
                gross_mtm=gross_mtm,
                fees_paid=fees_paid,
                est_exit_fees=est_exit_fees,
                slippage_amount=slippage_amount,
                net_mtm=net_mtm,
            )

    async def _close_master_trade_locked(
        self,
        trade_id: int,
        reason: str,
        db: Any,
        skip_master_legs: bool = False,
        *,
        trade_state: TradeState | None = None,
        total_pnl: float | None = None,
        gross_mtm: float | None = None,
        fees_paid: float | None = None,
        est_exit_fees: float | None = None,
        slippage_amount: float | None = None,
        net_mtm: float | None = None,
    ) -> dict[str, int]:
        """Inner funnel body — caller must already hold the per-trade exit lock."""
        if trade_state is None:
            trade_state = self.position_tracker.get(int(trade_id))
        if trade_state is None:
            # Reconcile / external close may leave no in-memory state.
            # With skip_master_legs we still mirror slaves + set Trade.status.
            if skip_master_legs:
                return await self._close_master_trade_db_only(
                    trade_id=int(trade_id),
                    reason=reason,
                    db=db,
                )
            logger.error(
                "[EXIT_FUNNEL] trade_id=%s reason=%s — no TradeState in tracker",
                trade_id,
                reason,
            )
            result = {
                "slaves_total": 0,
                "slaves_closed": 0,
                "slaves_failed": 0,
                "master_legs_closed": 0,
            }
            self._emit_exit_funnel(
                int(trade_id),
                reason,
                result,
                note="no_trade_state",
            )
            return result

        trade_id = int(trade_id)
        trade = trade_state.trade
        call_leg_mem = trade_state.call_leg
        put_leg_mem = trade_state.put_leg
        pnl_now = float(
            total_pnl
            if total_pnl is not None
            else (trade_state.last_delta_mtm or trade_state.last_pnl)
        )
        gross = float(gross_mtm if gross_mtm is not None else pnl_now)
        fees = float(fees_paid or 0.0) + float(est_exit_fees or 0.0)
        slip = float(slippage_amount or 0.0)
        net = float(net_mtm if net_mtm is not None else pnl_now)
        slaves_total = 0
        slaves_closed = 0
        slaves_failed = 0
        master_legs_closed = 0
        pre_slave_ids: set[int] = set()

        log_and_buffer(
            "EXIT_START",
            trade_id,
            {
                "reason": reason,
                "call": getattr(call_leg_mem, "symbol", "?"),
                "put": getattr(put_leg_mem, "symbol", "?"),
                "gross_mtm": round(gross, 4),
                "net_mtm": round(net, 4),
                "fees": round(fees, 4),
                "slippage": round(slip, 4),
            },
        )
        logger.info(
            "[EXIT_START] Trade %s: reason=%s call=%s put=%s",
            trade_id,
            reason,
            getattr(call_leg_mem, "symbol", "?"),
            getattr(put_leg_mem, "symbol", "?"),
        )
        if reason == ExitReason.DECISION_PROFIT_AT_TRIGGER.value:
            logger.info(
                "Closing basket: trigger fired but profitable — "
                "booking net_mtm=%.2f (trade %s)",
                net,
                trade_id,
            )
        assert self.delta_client is not None

        from backend.core.delta_sl import cancel_leg_sl_order
        from backend.engine.trade_reconcile import (
            book_leg_close,
            pnl_sanity_check,
            recompute_trade_realized_pnl,
            resolve_external_exit_fill,
        )
        with self.db_factory() as load_db:
            all_open_legs = (
                load_db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            for leg in all_open_legs:
                load_db.expunge(leg)

        hedge_pid = None
        for leg in all_open_legs:
            if bool(getattr(leg, "is_long", False)) or str(
                getattr(leg, "leg_type", "")
            ).startswith("hedge"):
                hedge_pid = int(leg.product_id or 0) or None
                break
        if hedge_pid is None:
            hedge_pid = getattr(trade, "conversion_hedge_product_id", None)

        # Step 1: Verify each open leg on Delta
        exists_map: dict[int, bool] = {}
        for leg in all_open_legs:
            pid = int(getattr(leg, "product_id", 0) or 0)
            if pid <= 0:
                exists_map[int(leg.id)] = False
                continue
            try:
                exists_map[int(leg.id)] = await self.delta_client.verify_position_exists(
                    pid
                )
            except Exception as exc:
                logger.warning(
                    "[EXIT_VERIFY] verify failed for %s: %s — assume exists",
                    leg.symbol,
                    exc,
                )
                exists_map[int(leg.id)] = True

        logger.info(
            "[EXIT_VERIFY] Pre-exit: %s",
            {
                str(leg.leg_type): exists_map.get(int(leg.id), False)
                for leg in all_open_legs
            },
        )
        log_and_buffer(
            "EXIT_VERIFY",
            trade_id,
            {
                "stage": "pre_exit",
                "legs": [
                    {
                        "leg": str(leg.leg_type),
                        "symbol": str(leg.symbol),
                        "is_long": bool(getattr(leg, "is_long", False)),
                        "exists": exists_map.get(int(leg.id), False),
                    }
                    for leg in all_open_legs
                ],
            },
        )

        # Step 2: Cancel legacy separate SL orders on short legs
        with self.db_factory() as sl_db:
            for leg_mem in all_open_legs:
                if bool(getattr(leg_mem, "is_long", False)):
                    continue
                leg_db = sl_db.query(Leg).filter(Leg.id == leg_mem.id).first()
                if leg_db is None:
                    continue
                if getattr(leg_db, "delta_sl_order_id", None):
                    try:
                        await cancel_leg_sl_order(
                            self.delta_client, leg_db, clear_fields=True
                        )
                        logger.info(
                            "Cancelled legacy SL order for %s",
                            leg_db.leg_type,
                        )
                    except Exception as exc:
                        logger.warning("SL cancel failed (non-fatal): %s", exc)
            sl_db.commit()

        # Count eligible slaves before mirror (mirror_exit uses its own session)
        from backend.models import SlaveTrade as _SlaveTradeExit

        _exit_statuses = (
            "active",
            "partial_adjustment",
            "adjust_close_failed",
        )
        try:
            pre_slave_rows = (
                db.query(_SlaveTradeExit)
                .filter(
                    _SlaveTradeExit.master_trade_id == trade_id,
                    _SlaveTradeExit.status.in_(_exit_statuses),
                )
                .all()
            )
            slaves_total = len(pre_slave_rows)
            pre_slave_ids = {int(st.id) for st in pre_slave_rows}
        except Exception as slave_count_exc:
            logger.warning(
                "[EXIT_FUNNEL] pre-mirror slave count failed: %s",
                slave_count_exc,
            )
            pre_slave_ids = set()
            slaves_total = 0

        # Step 3: Mirror exit to slaves BEFORE closing master (incl. hedge).
        # AWAIT — fire-and-forget left slaves open after adjustments when
        # hint product_ids were stale / task errors were swallowed.
        try:
            import backend.engine.mirror_engine as mirror_module

            me = mirror_module.mirror_engine or self.mirror_engine
            if me is not None:
                await me.mirror_exit(
                    master_trade_id=trade_id,
                    call_product_id=int(
                        getattr(call_leg_mem, "product_id", 0) or 0
                    ),
                    put_product_id=int(
                        getattr(put_leg_mem, "product_id", 0) or 0
                    ),
                    reason=reason,
                    hedge_product_id=(
                        int(hedge_pid) if hedge_pid else None
                    ),
                )
                log_and_buffer(
                    "MIRROR_EXIT",
                    trade_id,
                    {
                        "stage": "awaited_complete",
                        "reason": reason,
                        "slaves_total": slaves_total,
                    },
                )
            else:
                log_and_buffer(
                    "MIRROR_EXIT",
                    trade_id,
                    {
                        "stage": "engine_none",
                        "reason": reason,
                    },
                )
                logger.warning(
                    "[MIRROR_EXIT] mirror_engine is None — slaves not closed "
                    "for trade %s",
                    trade_id,
                )
        except Exception as exc:
            log_and_buffer(
                "MIRROR_EXIT",
                trade_id,
                {
                    "stage": "failed",
                    "reason": reason,
                    "error": str(exc),
                },
            )
            logger.warning(
                "[MIRROR_EXIT] Failed for trade %s (non-fatal): %s",
                trade_id,
                exc,
                exc_info=True,
            )

        try:
            db.expire_all()
            if pre_slave_ids:
                post_rows = (
                    db.query(_SlaveTradeExit)
                    .filter(_SlaveTradeExit.id.in_(pre_slave_ids))
                    .all()
                )
                slaves_closed = sum(
                    1 for st in post_rows if str(st.status) == "closed"
                )
                slaves_failed = max(0, slaves_total - slaves_closed)
            else:
                slaves_closed = 0
                slaves_failed = 0
        except Exception as slave_post_exc:
            logger.warning(
                "[EXIT_FUNNEL] post-mirror slave count failed: %s",
                slave_post_exc,
            )
            slaves_closed = 0
            slaves_failed = slaves_total

        # Step 4: Close ALL open legs on Delta (short=BUY, long hedge=SELL)
        close_results: dict[int, Any] = {}
        hard_fail = False
        trade_is_demo = bool(getattr(trade, "is_demo", False))

        if skip_master_legs:
            log_and_buffer(
                "EXIT_FUNNEL",
                trade_id,
                {
                    "reason": reason,
                    "note": "skip_master_legs",
                    "slaves_total": slaves_total,
                },
            )
            logger.info(
                "[EXIT_FUNNEL] trade_id=%s skip_master_legs=True — "
                "skipping Delta master leg closes",
                trade_id,
            )
        elif trade_is_demo:
            from backend.strategies.base_strategy import OrderResult

            logger.info(
                "[DEMO] Virtual exit — no real Delta orders, "
                "marking closed with mark prices"
            )
            for leg in all_open_legs:
                leg_id = int(leg.id)
                is_long = bool(getattr(leg, "is_long", False)) or str(
                    getattr(leg, "leg_type", "")
                ).startswith("hedge")
                fill_label = "hedge" if is_long else str(leg.leg_type)
                try:
                    px = float(
                        await self.delta_client.get_mark_price(str(leg.symbol))
                    )
                except Exception:
                    px = float(getattr(leg, "initial_premium", 0) or 0)
                close_results[leg_id] = OrderResult(
                    success=True,
                    order_id=None,
                    filled_price=px,
                    commission=0.0,
                )
                log_and_buffer(
                    "EXIT_CLOSE",
                    trade_id,
                    {
                        "leg": fill_label,
                        "ok": True,
                        "fill": px,
                        "is_long": is_long,
                        "demo": True,
                    },
                )
        else:
            for leg in all_open_legs:
                leg_id = int(leg.id)
                is_long = bool(getattr(leg, "is_long", False)) or str(
                    getattr(leg, "leg_type", "")
                ).startswith("hedge")
                fill_label = "hedge" if is_long else str(leg.leg_type)
                on_delta = exists_map.get(leg_id, False)

                if not on_delta:
                    logger.warning(
                        "[EXIT_CLOSE] %s not on Delta — skipping close", fill_label
                    )
                    log_and_buffer(
                        "EXIT_CLOSE",
                        trade_id,
                        {
                            "leg": fill_label,
                            "ok": True,
                            "skipped": "not_on_delta",
                            "is_long": is_long,
                        },
                    )
                    continue

                if is_long:
                    close_result = await self.order_executor.close_long_position(
                        product_id=int(leg.product_id),
                        quantity=int(leg.quantity),
                        delta_client=self.delta_client,
                        symbol_for_fallback=str(leg.symbol),
                    )
                else:
                    close_result = await self.order_executor.close_leg(
                        leg, self.delta_client
                    )
                close_results[leg_id] = close_result

                if close_result.success:
                    logger.info(
                        "EXIT_CLOSE %s @ %.2f",
                        fill_label,
                        float(close_result.filled_price or 0),
                    )
                    log_and_buffer(
                        "EXIT_CLOSE",
                        trade_id,
                        {
                            "leg": fill_label,
                            "ok": True,
                            "fill": float(close_result.filled_price or 0),
                            "is_long": is_long,
                        },
                    )
                else:
                    logger.error(
                        "EXIT_CLOSE FAILED for %s: %s",
                        leg.symbol,
                        close_result.error,
                    )
                    log_and_buffer(
                        "EXIT_CLOSE",
                        trade_id,
                        {
                            "leg": fill_label,
                            "ok": False,
                            "error": close_result.error,
                            "is_long": is_long,
                        },
                    )

        # Step 5: Wait and verify closes
        if not skip_master_legs:
            await asyncio.sleep(2 if not trade_is_demo else 0)
            still_map: dict[str, bool] = {}
            for leg in all_open_legs:
                leg_id = int(leg.id)
                if not exists_map.get(leg_id, False):
                    continue
                res = close_results.get(leg_id)
                if res is not None and not res.success:
                    still = await self.delta_client.verify_position_exists(
                        int(leg.product_id)
                    )
                    label = (
                        "hedge"
                        if bool(getattr(leg, "is_long", False))
                        else str(leg.leg_type)
                    )
                    still_map[label] = still
                    if still:
                        hard_fail = True
                        logger.warning(
                            "[EXIT_VERIFY] %s still visible on Delta after close!",
                            label,
                        )
                else:
                    label = (
                        "hedge"
                        if bool(getattr(leg, "is_long", False))
                        else str(leg.leg_type)
                    )
                    still_map[label] = False

            log_and_buffer(
                "EXIT_VERIFY",
                trade_id,
                {"stage": "post_exit", "still_open": still_map},
            )

            if hard_fail:
                msg = (
                    f"Exit order failure trade={trade_id} "
                    f"still_open={still_map}"
                )
                logger.critical(msg)
                log_and_buffer(
                    "EXIT_FAIL",
                    trade_id,
                    {"reason": reason, "error": msg},
                )
                await self._push_error(
                    trade_id, msg, requires_manual_action=True
                )
                result = {
                    "slaves_total": slaves_total,
                    "slaves_closed": slaves_closed,
                    "slaves_failed": slaves_failed,
                    "master_legs_closed": master_legs_closed,
                }
                self._emit_exit_funnel(
                    trade_id, reason, result, note="exit_order_hard_fail"
                )
                return result

        # Step 6: Update DB — book closes for all tracked legs
        status = self._status_for_reason(reason)
        now_utc = datetime.now(timezone.utc)
        call_fill = 0.0
        put_fill = 0.0
        call_close = None
        put_close = None
        final_pnl = pnl_now

        with self.db_factory() as exit_db:
            trade_row = exit_db.query(Trade).filter(Trade.id == trade_id).first()
            if trade_row is None:
                logger.error("Exit DB trade row missing for trade %s", trade_id)
                log_and_buffer(
                    "EXIT_FAIL",
                    trade_id,
                    {"reason": reason, "error": "DB trade missing"},
                )
                result = {
                    "slaves_total": slaves_total,
                    "slaves_closed": slaves_closed,
                    "slaves_failed": slaves_failed,
                    "master_legs_closed": master_legs_closed,
                }
                self._emit_exit_funnel(
                    trade_id, reason, result, note="db_trade_missing"
                )
                return result

            booked_ids: set[int] = set()
            for leg_mem in all_open_legs:
                leg_db = exit_db.query(Leg).filter(Leg.id == leg_mem.id).first()
                if leg_db is None or str(leg_db.status).lower() != "open":
                    continue
                res = close_results.get(int(leg_mem.id))
                exit_px: float | None = None
                if res is not None and res.success:
                    px = float(res.filled_price or 0.0)
                    exit_px = px if px > 0 else None
                if exit_px is None and skip_master_legs:
                    # Exchange-close / already flat — fetch real fill, never 0.0
                    exit_px = await resolve_external_exit_fill(
                        self.delta_client, leg_db
                    )
                book_leg_close(
                    leg=leg_db,
                    trade=trade_row,
                    exit_premium=exit_px,
                    exit_time=now_utc,
                    exit_fee_usd=(
                        float(res.commission)
                        if res is not None and res.commission is not None
                        else None
                    ),
                    exit_order_id=(
                        str(res.order_id)
                        if res is not None and res.order_id is not None
                        else None
                    ),
                )
                booked_ids.add(int(leg_db.id))
                lt = str(leg_db.leg_type or "").lower()
                if lt == "call":
                    call_fill = float(exit_px or 0.0)
                    call_close = res
                elif lt == "put":
                    put_fill = float(exit_px or 0.0)
                    put_close = res

            # True orphans only — hedge_call/hedge_put are tracked, not orphans
            tracked_types = {"call", "put", "hedge_call", "hedge_put"}
            remaining_open = (
                exit_db.query(Leg)
                .filter(Leg.trade_id == trade_id, Leg.status == "open")
                .all()
            )
            true_orphans = [
                leg
                for leg in remaining_open
                if int(leg.id) not in booked_ids
                and str(leg.leg_type or "").lower() not in tracked_types
            ]
            # Any remaining tracked legs that failed to book (edge) — still close in DB
            leftover_tracked = [
                leg
                for leg in remaining_open
                if int(leg.id) not in booked_ids
                and str(leg.leg_type or "").lower() in tracked_types
            ]
            if true_orphans:
                logger.warning(
                    "[EXIT_CLEANUP] Found %s untracked orphan legs for "
                    "trade %s: %s",
                    len(true_orphans),
                    trade_id,
                    [leg.symbol for leg in true_orphans],
                )
                log_and_buffer(
                    "EXIT_CLEANUP",
                    trade_id,
                    {
                        "orphan_count": len(true_orphans),
                        "symbols": [leg.symbol for leg in true_orphans],
                    },
                )
                for orphan in true_orphans:
                    existing_px = getattr(orphan, "exit_premium", None)
                    exit_px = (
                        float(existing_px)
                        if existing_px is not None and float(existing_px) > 0
                        else await resolve_external_exit_fill(
                            self.delta_client, orphan
                        )
                    )
                    book_leg_close(
                        leg=orphan,
                        trade=trade_row,
                        exit_premium=exit_px,
                        exit_time=now_utc,
                    )
            for leftover in leftover_tracked:
                logger.info(
                    "[EXIT_CLEANUP] Booking leftover tracked leg %s (%s)",
                    leftover.symbol,
                    leftover.leg_type,
                )
                existing_px = getattr(leftover, "exit_premium", None)
                exit_px = (
                    float(existing_px)
                    if existing_px is not None and float(existing_px) > 0
                    else await resolve_external_exit_fill(
                        self.delta_client, leftover
                    )
                )
                book_leg_close(
                    leg=leftover,
                    trade=trade_row,
                    exit_premium=exit_px,
                    exit_time=now_utc,
                )

            # Clear conversion mode
            trade_row.in_conversion_mode = False
            trade_row.conversion_hedge_symbol = None
            trade_row.conversion_hedge_product_id = None
            trade_row.conversion_hedge_entry_price = None
            trade_row.conversion_hedge_order_id = None
            trade_row.conversion_triggered_leg = None

            trade_row.status = status
            trade_row.exit_time = get_ist_now()
            trade_row.exit_reason = reason
            # Single source of truth: sum of closed legs (not incremental paths)
            final_pnl = recompute_trade_realized_pnl(exit_db, trade_row)
            all_legs_for_sanity = (
                exit_db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            pnl_sanity_check(
                trade_id=trade_id,
                realized_pnl=final_pnl,
                last_gross_mtm=gross,
                legs=all_legs_for_sanity,
            )
            exit_db.commit()

            still_open = (
                exit_db.query(Leg)
                .filter(Leg.trade_id == trade_id, Leg.status == "open")
                .count()
            )
            if still_open > 0:
                logger.critical(
                    "[EXIT_VERIFY] %s legs still 'open' in DB for closed "
                    "trade %s! DB inconsistency!",
                    still_open,
                    trade_id,
                )
            else:
                logger.info(
                    "[EXIT_VERIFY] All legs closed in DB for trade %s",
                    trade_id,
                )

        master_legs_closed = sum(
            1
            for res in close_results.values()
            if res is not None and getattr(res, "success", False)
        )
        if skip_master_legs:
            # Legs already flat on Delta — report open-leg count as closed in DB path
            master_legs_closed = len(all_open_legs)

        trade_state.hedge_leg = None

        # Step 7: Remove from tracker
        self.position_tracker.mark_closed(trade_id)
        if self.position_tracker.get(trade_id) is not None:
            logger.error(
                "[EXIT_VERIFY] Trade %s still in tracker after mark_closed!",
                trade_id,
            )
        else:
            logger.info(
                "[EXIT_VERIFY] Trade %s removed from tracker", trade_id
            )

        # Step 8: Schedule auto re-entry
        self._maybe_schedule_auto_reentry(
            str(getattr(trade, "underlying", "") or "")
        )

        # Step 9: Broadcast
        log_and_buffer(
            "EXIT_COMPLETE",
            trade_id,
            {
                "reason": reason,
                "call_closed_at": round(call_fill, 2),
                "put_closed_at": round(put_fill, 2),
                "final_pnl": round(final_pnl, 4),
            },
        )
        log_and_buffer(
            "EXIT_DONE",
            trade_id,
            {
                "reason": reason,
                "call_closed_at": round(call_fill, 2),
                "put_closed_at": round(put_fill, 2),
                "final_pnl": round(final_pnl, 2),
            },
        )
        await ws_manager.broadcast(
            {
                "type": "TRADE_CLOSED",
                "trade_id": trade_id,
                "reason": reason,
                "final_pnl": final_pnl,
                "call_closed_at": call_fill if call_close and call_close.success else None,
                "put_closed_at": put_fill if put_close and put_close.success else None,
                "timestamp": get_ist_now().isoformat(),
            }
        )
        logger.info(
            "[EXIT_COMPLETE] Trade %s fully closed: reason=%s final_pnl=%.4f",
            trade_id,
            reason,
            final_pnl,
        )

        # Notify Earner backend (fire-and-forget, non-fatal)
        try:
            from backend.core.earner_webhook import notify_earner_trade_closed
            from backend.models import SlaveAccount, SlaveTrade

            with self.db_factory() as webhook_db:
                # Find all slave trades linked to this master trade
                slave_trades = (
                    webhook_db.query(SlaveTrade)
                    .filter(
                        SlaveTrade.master_trade_id == trade_id,
                        SlaveTrade.status == "active",
                    )
                    .all()
                )
                slave_payloads = []
                for st in slave_trades:
                    slave_acc = (
                        webhook_db.query(SlaveAccount)
                        .filter(SlaveAccount.id == st.slave_account_id)
                        .first()
                    )
                    if slave_acc and slave_acc.earner_user_id:
                        slave_payloads.append({
                            "earner_user_id": slave_acc.earner_user_id,
                            "earner_subscription_id": (
                                slave_acc.earner_subscription_id
                            ),
                            "actual_quantity": int(st.actual_quantity or 1),
                            "call_fill_price": float(st.call_fill_price or 0),
                            "put_fill_price": float(st.put_fill_price or 0),
                            "slave_account_id": int(slave_acc.id),
                            "slave_name": slave_acc.name,
                        })

                if slave_payloads:
                    asyncio.ensure_future(
                        notify_earner_trade_closed(
                            master_trade_id=trade_id,
                            exit_reason=str(reason),
                            final_pnl=float(final_pnl),
                            slave_accounts=slave_payloads,
                        )
                    )
        except Exception as webhook_exc:
            logger.warning(
                "[EARNER_WEBHOOK] Setup failed: %s", webhook_exc
            )

        # Per-exit orphan standalone SL sweep (master + slaves).
        # Live-position stops (e.g. Trade#65 mid-trade) are kept until flat.
        try:
            from backend.api.routes_account import cleanup_orphan_sl_orders

            with self.db_factory() as orphan_db:
                orphan_summary = await cleanup_orphan_sl_orders(
                    orphan_db, trade_id=trade_id
                )
            logger.info(
                "[ORPHAN_SL] post-exit sweep trade=%s cancelled=%s kept=%s",
                trade_id,
                orphan_summary.get("cancelled"),
                orphan_summary.get("kept"),
            )
        except Exception as orphan_exc:
            logger.warning(
                "[ORPHAN_SL] post-exit sweep failed (non-fatal): %s",
                orphan_exc,
            )

        result = {
            "slaves_total": slaves_total,
            "slaves_closed": slaves_closed,
            "slaves_failed": slaves_failed,
            "master_legs_closed": master_legs_closed,
        }
        self._emit_exit_funnel(trade_id, reason, result)
        return result

    async def _exit_trade(
        self,
        trade_state: TradeState,
        reason: str,
        total_pnl: float | None = None,
        gross_mtm: float | None = None,
        fees_paid: float | None = None,
        est_exit_fees: float | None = None,
        slippage_amount: float | None = None,
        net_mtm: float | None = None,
    ) -> None:
        """Exit a trade via the single close_master_trade funnel (identical behaviour)."""
        with self.db_factory() as db:
            await self.close_master_trade(
                trade_id=int(trade_state.trade_id),
                reason=reason,
                db=db,
                skip_master_legs=False,
                trade_state=trade_state,
                total_pnl=total_pnl,
                gross_mtm=gross_mtm,
                fees_paid=fees_paid,
                est_exit_fees=est_exit_fees,
                slippage_amount=slippage_amount,
                net_mtm=net_mtm,
            )

    async def _check_conversion_mode_exit(
        self,
        trade_state: TradeState,
        call_premium: float,
        put_premium: float,
    ) -> bool:
        """
        Check if conversion mode reversal condition is met.
        Returns True if hedge was closed and normal mode resumed.

        Reversal condition: both short leg premiums within equality_pct% of each other.
        """
        trade = trade_state.trade
        trade_id = int(trade.id)

        if not bool(getattr(trade, "in_conversion_mode", False)):
            return False

        # Always read hedge from Leg table (authoritative) — in-memory
        # conversion_hedge_* fields can be stale after restart.
        with self.db_factory() as _db_hedge:
            hedge_leg_db = (
                _db_hedge.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.is_long.is_(True),
                    Leg.status == "open",
                )
                .first()
            )
            if hedge_leg_db is None:
                hedge_leg_db = (
                    _db_hedge.query(Leg)
                    .filter(
                        Leg.trade_id == trade_id,
                        Leg.status == "open",
                        Leg.leg_type.in_(("hedge_call", "hedge_put")),
                    )
                    .first()
                )
            if hedge_leg_db is None:
                logger.warning(
                    "[CONVERSION_REVERSAL] Trade %s: in_conversion_mode but "
                    "no open hedge leg in DB — clearing conversion flag",
                    trade_id,
                )
                trade_state.trade.in_conversion_mode = False
                try:
                    t_row = (
                        _db_hedge.query(Trade)
                        .filter(Trade.id == trade_id)
                        .first()
                    )
                    if t_row is not None:
                        t_row.in_conversion_mode = False
                        t_row.conversion_hedge_product_id = None
                        t_row.conversion_hedge_order_id = None
                        t_row.conversion_hedge_entry_price = None
                        t_row.conversion_hedge_symbol = None
                        t_row.conversion_triggered_leg = None
                        _db_hedge.commit()
                except Exception as clear_exc:
                    logger.warning(
                        "Could not clear stale conversion flag: %s", clear_exc
                    )
                return False

            hedge_entry_price = float(hedge_leg_db.initial_premium or 0)
            hedge_product_id = int(hedge_leg_db.product_id or 0)
            hedge_symbol = str(hedge_leg_db.symbol or "")
            _db_hedge.expunge(hedge_leg_db)

        # Get equality threshold from settings
        try:
            with self.db_factory() as _db:
                from backend.database import get_or_create_auto_settings

                _cfg = get_or_create_auto_settings(_db)
                equality_pct = float(
                    getattr(_cfg, "conversion_equality_pct", 10.0) or 10.0
                )
        except Exception:
            equality_pct = 10.0

        # Check equality condition
        max_prem = max(call_premium, put_premium)
        if max_prem <= 0:
            return False
        diff_pct = abs(call_premium - put_premium) / max_prem * 100.0

        logger.debug(
            "[CONVERSION_MONITOR] Trade %s: call=%.2f put=%.2f "
            "diff=%.1f%% threshold=%.1f%% hedge=%s",
            trade_id,
            call_premium,
            put_premium,
            diff_pct,
            equality_pct,
            hedge_symbol,
        )

        if diff_pct > equality_pct:
            return False  # Not equal yet, stay in conversion mode

        # REVERSAL DETECTED — close hedge leg and resume normal mode
        logger.info(
            "[CONVERSION_REVERSAL] Trade %s: premiums equal (call=%.2f put=%.2f "
            "diff=%.1f%%) — closing hedge and resuming normal mode",
            trade_id,
            call_premium,
            put_premium,
            diff_pct,
        )

        hedge_close_success = False
        hedge_close_price = 0.0

        with self.db_factory() as db:
            hedge_leg = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.status == "open",
                    Leg.is_long.is_(True),
                )
                .first()
            )
            if hedge_leg is None:
                hedge_leg = (
                    db.query(Leg)
                    .filter(
                        Leg.trade_id == trade_id,
                        Leg.status == "open",
                        Leg.leg_type.in_(("hedge_call", "hedge_put")),
                    )
                    .first()
                )

            if hedge_leg is not None:
                hedge_symbol = str(hedge_leg.symbol or hedge_symbol)
                hedge_entry_price = float(
                    hedge_leg.initial_premium or hedge_entry_price
                )
                hedge_product_id = int(hedge_leg.product_id)
                try:
                    if bool(getattr(trade, "is_demo", False)):
                        logger.info(
                            "[DEMO] Virtual hedge close — no real Delta order"
                        )
                        from backend.strategies.base_strategy import OrderResult

                        try:
                            px = float(
                                await self.delta_client.get_mark_price(
                                    str(hedge_leg.symbol)
                                )
                            )
                        except Exception:
                            px = float(hedge_leg.initial_premium or 0)
                        close_res = OrderResult(
                            success=True,
                            order_id=None,
                            filled_price=px,
                            commission=0.0,
                        )
                    else:
                        close_res = await self.order_executor.close_long_position(
                            product_id=int(hedge_leg.product_id),
                            quantity=int(hedge_leg.quantity),
                            delta_client=self.delta_client,
                            symbol_for_fallback=str(hedge_leg.symbol),
                        )
                    if close_res.success:
                        hedge_close_success = True
                        hedge_close_price = float(close_res.filled_price or 0)
                        hedge_leg.status = "closed"
                        hedge_leg.exit_time = datetime.now(timezone.utc)
                        hedge_leg.exit_premium = hedge_close_price
                        if close_res.order_id is not None:
                            hedge_leg.exit_order_id = str(close_res.order_id)
                        if close_res.commission is not None:
                            hedge_leg.exit_fee_usd = abs(
                                float(close_res.commission)
                            )
                        # Long P&L: sell_exit - buy_entry
                        hedge_pnl = (
                            (
                                hedge_close_price
                                - float(hedge_leg.initial_premium or 0)
                            )
                            * int(hedge_leg.quantity)
                            * float(OPTIONS_CONTRACT_VALUE)
                        )
                        hedge_leg.realized_pnl = hedge_pnl
                        t_row = (
                            db.query(Trade).filter(Trade.id == trade_id).first()
                        )
                        if t_row is not None:
                            t_row.realized_pnl = (
                                float(t_row.realized_pnl or 0) + hedge_pnl
                            )
                        logger.info(
                            "[CONVERSION_REVERSAL] Hedge closed: "
                            "symbol=%s fill=%.2f pnl=%.4f",
                            hedge_symbol,
                            hedge_close_price,
                            hedge_pnl,
                        )
                    else:
                        logger.error(
                            "[CONVERSION_REVERSAL] Hedge close FAILED "
                            "for trade %s: %s",
                            trade_id,
                            close_res.error,
                        )
                except Exception as exc:
                    logger.error(
                        "[CONVERSION_REVERSAL] Hedge close error trade %s: %s",
                        trade_id,
                        exc,
                    )
            elif hedge_product_id:
                # Legacy fallback: conversion fields only, no Leg row
                try:
                    qty = int(
                        getattr(trade_state.call_leg, "quantity", None)
                        or getattr(trade_state.put_leg, "quantity", None)
                        or 1
                    )
                    if bool(getattr(trade, "is_demo", False)):
                        from backend.strategies.base_strategy import OrderResult

                        logger.info(
                            "[DEMO] Virtual legacy hedge close — no real order"
                        )
                        try:
                            px = float(
                                await self.delta_client.get_mark_price(
                                    str(hedge_symbol or "")
                                )
                            )
                        except Exception:
                            px = float(hedge_entry_price or 0)
                        close_res = OrderResult(
                            success=True,
                            filled_price=px,
                            commission=0.0,
                        )
                    else:
                        close_res = await self.order_executor.close_long_position(
                            product_id=int(hedge_product_id),
                            quantity=qty,
                            delta_client=self.delta_client,
                            symbol_for_fallback=hedge_symbol or None,
                        )
                    if close_res.success:
                        hedge_close_success = True
                        hedge_close_price = float(close_res.filled_price or 0)
                        logger.info(
                            "[CONVERSION_REVERSAL] Legacy hedge closed: "
                            "symbol=%s fill=%.2f",
                            hedge_symbol,
                            hedge_close_price,
                        )
                except Exception as exc:
                    logger.error(
                        "[CONVERSION_REVERSAL] Legacy hedge close error: %s",
                        exc,
                    )

            # AUDIT-7: mirror hedge close to slaves (only if closed)
            if hedge_close_success and hedge_product_id:
                try:
                    import backend.engine.mirror_engine as mirror_module

                    if mirror_module.mirror_engine is not None:
                        asyncio.create_task(
                            mirror_module.mirror_engine.mirror_hedge_close(
                                master_trade_id=trade_id,
                                hedge_product_id=int(hedge_product_id),
                            )
                        )
                except Exception as exc:
                    logger.warning(
                        "Mirror hedge close queue failed (non-fatal): %s", exc
                    )

            # Only clear conversion mode if hedge close succeeded.
            # If close failed, keep conversion mode and retry next tick.
            if not hedge_close_success:
                logger.warning(
                    "[CONVERSION_REVERSAL] Hedge close failed — "
                    "staying in conversion mode, will retry next tick"
                )
                db.commit()
                return False

            t = db.query(Trade).filter(Trade.id == trade_id).first()
            if t:
                t.in_conversion_mode = False
                t.conversion_hedge_product_id = None
                t.conversion_hedge_order_id = None
                t.conversion_hedge_entry_price = None
                t.conversion_hedge_symbol = None
                t.conversion_triggered_leg = None

            # Reset short-leg baselines only (not hedge)
            open_legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_id,
                    Leg.status == "open",
                    Leg.leg_type.in_(("call", "put")),
                )
                .all()
            )
            for leg in open_legs:
                try:
                    if self.delta_client is None:
                        continue
                    new_baseline = await self.delta_client.get_short_exit_price(
                        str(leg.symbol)
                    )
                    if float(new_baseline or 0) > 0:
                        leg.trigger_baseline_premium = float(new_baseline)
                        leg.trigger_premium = float(new_baseline)
                        logger.info(
                            "[CONVERSION_REVERSAL] Reset baseline %s → %.2f",
                            leg.leg_type,
                            new_baseline,
                        )
                except Exception as exc:
                    logger.warning(
                        "[CONVERSION_REVERSAL] Could not reset baseline for %s: %s",
                        leg.leg_type,
                        exc,
                    )
            db.commit()

        if not hedge_close_success:
            return False

        # Sync in-memory trade flags only if hedge actually closed
        trade.in_conversion_mode = False
        trade.conversion_hedge_product_id = None
        trade.conversion_hedge_order_id = None
        trade.conversion_hedge_entry_price = None
        trade.conversion_hedge_symbol = None
        trade.conversion_triggered_leg = None
        trade_state.hedge_leg = None

        log_and_buffer(
            "CONVERSION_REVERSAL",
            trade_id,
            {
                "call_premium": round(call_premium, 2),
                "put_premium": round(put_premium, 2),
                "diff_pct": round(diff_pct, 2),
                "hedge_symbol": hedge_symbol,
                "hedge_entry_price": round(hedge_entry_price, 2),
                "hedge_close_price": round(hedge_close_price, 2),
                "hedge_closed": hedge_close_success,
            },
        )

        # Reload legs in tracker so normal mode works with fresh state
        self._reload_legs(trade_state)
        return True

    async def _adjust_trade(
        self, trade_state: TradeState, triggered_leg_type: str
    ) -> None:
        trade_id = trade_state.trade_id
        # Lock FIRST — before any async work — so integrity monitor cannot
        # emergency-close a mid-adjustment naked leg (INTEGRITY_NAKED race).
        self.position_tracker.set_adjusting(trade_id, True)
        # DIAGNOSTIC — remove after is_adjusting race root-caused
        _live_a = self.position_tracker.get(trade_id)
        logger.warning(
            "[DIAG_IS_ADJUSTING] (a) _adjust_trade START trade_id=%s "
            "set_adjusting(True) done | tracker.is_adjusting=%s "
            "trade_state.is_adjusting=%s tracker_has_trade=%s",
            trade_id,
            getattr(_live_a, "is_adjusting", None) if _live_a else None,
            getattr(trade_state, "is_adjusting", None),
            _live_a is not None,
        )
        try:
            triggered = triggered_leg_type.lower()
            old_leg = (
                trade_state.call_leg if triggered == "call" else trade_state.put_leg
            )
            other_leg = (
                trade_state.put_leg if triggered == "call" else trade_state.call_leg
            )
            old_strike = float(old_leg.strike)
            old_premium = float(old_leg.initial_premium)
            old_product_id = int(getattr(old_leg, "product_id", 0) or 0)
            master_qty = int(getattr(old_leg, "quantity", 1) or 1)
            other_prem = float(
                getattr(trade_state, "last_put_premium", 0)
                if triggered == "call"
                else getattr(trade_state, "last_call_premium", 0)
            ) or float(other_leg.initial_premium)

            logger.info("Adjusting trade %s, leg: %s", trade_id, triggered_leg_type)
            # ADJUSTMENT_START with final target_new_premium is emitted inside
            # AdjustmentExecutor after the basket-loss formula is computed —
            # do not log a pre-loss placeholder here (caused Trade#66 mismatch).
            with self.db_factory() as db:
                result = await self.adjustment_executor.execute(
                    trade_state.trade,
                    triggered_leg_type,
                    self.strategy,
                    self.delta_client,
                    self.order_executor,
                    db,
                )
            if result.success:
                self._reload_legs(trade_state)
                # Pause exits/adjusts so new-leg ask settles (stops cascade)
                self._apply_adjustment_cooldown(trade_state)
                # Delta UPNL stale right after adjust — avoid false exits
                trade_state.last_delta_mtm = 0.0
                self.position_tracker.update_delta_mtm(trade_id, 0.0)
                log_and_buffer(
                    "ADJUSTMENT_DONE",
                    trade_id,
                    {
                        "leg": triggered,
                        "old_strike": float(result.old_strike or old_strike),
                        "new_strike": float(result.new_strike or 0),
                        "old_premium": round(old_premium, 2),
                        "new_premium": round(float(result.premium_collected or 0), 2),
                        "cooldown_minutes": ADJUSTMENT_COOLDOWN_MINUTES,
                    },
                )

                # Mirror normal adjustment to slaves (await — do not fire-and-forget)
                # Conversion mode uses mirror_conversion from adjustment.execute instead.
                # Use log_and_buffer so MIRROR_ADJ_* appears in bot_activity.log
                # (module logger alone only hits /var/log/trading-bot/error.log).
                log_and_buffer(
                    "MIRROR_ADJ_DEBUG",
                    trade_id,
                    {
                        "result.success": result.success,
                        "result.new_product_id": getattr(
                            result, "new_product_id", "MISSING"
                        ),
                        "result.new_symbol": getattr(
                            result, "new_symbol", "MISSING"
                        ),
                        "result.old_product_id": getattr(
                            result, "old_product_id", "MISSING"
                        ),
                        "conversion_mode": getattr(
                            result, "conversion_mode", False
                        ),
                    },
                )
                if not getattr(result, "conversion_mode", False):
                    try:
                        import backend.engine.mirror_engine as mirror_module

                        me = mirror_module.mirror_engine or self.mirror_engine
                        # After _reload_legs, get fresh open leg for the adjusted side
                        if triggered == "call":
                            new_leg = trade_state.call_leg
                        else:
                            new_leg = trade_state.put_leg

                        new_pid = int(
                            getattr(result, "new_product_id", None)
                            or getattr(new_leg, "product_id", 0)
                            or 0
                        )
                        old_pid = int(
                            getattr(result, "old_product_id", None)
                            or old_product_id
                            or 0
                        )
                        new_sym = str(
                            getattr(result, "new_symbol", None)
                            or getattr(new_leg, "symbol", "")
                            or ""
                        )
                        new_stk = float(
                            getattr(result, "new_strike", None)
                            or getattr(new_leg, "strike", 0)
                            or 0
                        )
                        qty = int(
                            getattr(result, "quantity", None) or master_qty or 1
                        )
                        log_and_buffer(
                            "MIRROR_ADJ_PRE",
                            trade_id,
                            {
                                "new_pid": new_pid,
                                "old_pid": old_pid,
                                "new_sym": new_sym,
                                "me": me is not None,
                            },
                        )
                        if me is None:
                            log_and_buffer(
                                "MIRROR_ADJ_SKIP",
                                trade_id,
                                {"reason": "mirror_engine is None"},
                            )
                        elif old_pid <= 0 or new_pid <= 0:
                            log_and_buffer(
                                "MIRROR_ADJ_SKIP",
                                trade_id,
                                {
                                    "reason": "missing product_ids",
                                    "old_pid": old_pid,
                                    "new_pid": new_pid,
                                },
                            )
                        else:
                            await me.mirror_adjustment(
                                master_trade_id=trade_id,
                                triggered_leg_type=triggered,
                                old_product_id=old_pid,
                                new_product_id=new_pid,
                                new_symbol=new_sym,
                                new_strike=new_stk,
                                master_qty=qty,
                                universal_sl_pct=float(
                                    getattr(
                                        trade_state.trade,
                                        "universal_sl_pct",
                                        None,
                                    )
                                    or 200.0
                                ),
                                master_bracket_sl=(
                                    float(result.master_bracket_sl)
                                    if getattr(
                                        result, "master_bracket_sl", None
                                    )
                                    else None
                                ),
                            )
                            log_and_buffer(
                                "MIRROR_ADJ_CALLED",
                                trade_id,
                                {
                                    "triggered_leg": triggered,
                                    "old_product": old_pid,
                                    "new_product": new_pid,
                                    "qty": qty,
                                },
                            )
                    except Exception as mirror_adj_err:
                        logger.warning(
                            "[MIRROR_ADJ_FAIL] Trade#%s: %s",
                            trade_id,
                            mirror_adj_err,
                            exc_info=True,
                        )
                        log_and_buffer(
                            "MIRROR_ADJ_FAIL",
                            trade_id,
                            {"error": str(mirror_adj_err)[:300]},
                        )

                if getattr(result, "conversion_mode", False):
                    logger.info(
                        "[CONVERSION_MODE] Trade %s entered conversion mode. "
                        "Hedge: %s @ %.2f. Normal adjustment suspended until reversal.",
                        trade_id,
                        result.hedge_symbol,
                        result.hedge_entry_price or 0,
                    )
                    log_and_buffer(
                        "CONVERSION_MODE_ACTIVE",
                        trade_id,
                        {
                            "hedge_symbol": result.hedge_symbol,
                            "hedge_fill": round(
                                float(result.hedge_entry_price or 0), 2
                            ),
                            "new_strike": float(result.new_strike or 0),
                            "premium_collected": round(
                                float(result.premium_collected or 0), 2
                            ),
                        },
                    )
                    # Sync conversion flags onto in-memory trade
                    trade_state.trade.in_conversion_mode = True
                    trade_state.trade.conversion_hedge_symbol = result.hedge_symbol
                    trade_state.trade.conversion_hedge_product_id = (
                        result.hedge_product_id
                    )
                    trade_state.trade.conversion_hedge_entry_price = (
                        result.hedge_entry_price
                    )
                    trade_state.trade.conversion_hedge_order_id = (
                        result.hedge_order_id
                    )
                    # Reload legs again to pick up new other leg + hedge from DB
                    self._reload_legs(trade_state)
                await self._push_adjustment(trade_state, triggered_leg_type, result)
            else:
                if getattr(result, "requires_basket_exit", False) or result.close_basket:
                    exit_reason = (
                        getattr(result, "exit_reason", None)
                        or "ADJ_LOW_PREMIUM_EXIT"
                    )
                    err_msg = str(result.error_message or "")
                    if not getattr(result, "exit_reason", None):
                        if "CONVERSION_DISABLED" in err_msg.upper():
                            exit_reason = "CONVERSION_DISABLED_EXIT"
                        elif "NO_STRIKE_AVAILABLE" in err_msg.upper():
                            exit_reason = "NO_STRIKE_AVAILABLE"
                        elif "NO_HEDGE_STRIKE" in err_msg.upper():
                            exit_reason = "NO_HEDGE_STRIKE_AVAILABLE"
                        elif "NO_OTHER_STRIKE" in err_msg.upper():
                            exit_reason = "NO_OTHER_STRIKE_IN_CONVERSION"
                    logger.critical(
                        "[%s] Triggering basket exit for trade %s reason=%s — %s",
                        exit_reason,
                        trade_id,
                        exit_reason,
                        result.error_message,
                    )
                    log_and_buffer(
                        exit_reason,
                        trade_id,
                        {
                            "leg": triggered,
                            "old_strike": old_strike,
                            "other_leg_offer": round(other_prem, 2),
                            "reason": result.error_message,
                            "action": "EXIT_BASKET",
                        },
                    )
                    await self._exit_trade(
                        trade_state,
                        reason=exit_reason,
                    )
                    return
                err = result.error_message or "Adjustment failed"
                is_hold = (
                    "ADJUSTMENT_HOLD" in err
                    and "no other" in err.lower()
                    and "NO_STRIKE_AVAILABLE" not in err.upper()
                )
                if is_hold:
                    log_and_buffer(
                        "ADJUSTMENT_HOLD",
                        trade_id,
                        {
                            "leg": triggered,
                            "old_strike": old_strike,
                            "reason": err,
                            "action": "HOLD_UNTIL_DIFFERENT_STRIKE",
                        },
                    )
                    logger.info(
                        "Trade %s adjustment held (same/no alternate strike) — %s",
                        trade_id,
                        err,
                    )
                elif result.is_partial:
                    # CRITICAL: Do NOT remove from tracker / mark closed /
                    # schedule auto re-entry. Keep monitoring remaining leg.
                    self._reload_legs_after_partial(trade_state)
                    log_and_buffer(
                        "PARTIAL_ADJUSTMENT",
                        trade_id,
                        {
                            "leg": triggered,
                            "error": err,
                            "action": "KEEP_MONITORING_REMAINING_LEG",
                        },
                    )
                    logger.critical(
                        "PARTIAL ADJUSTMENT on trade %s. "
                        "Trade remains ACTIVE with one leg. "
                        "Auto trade will NOT fire. %s",
                        trade_id,
                        err,
                    )

                    # Mirror: close the same runaway leg on every slave.
                    # Do NOT open a replacement — master has none either.
                    old_pid = int(
                        getattr(result, "old_product_id", None)
                        or old_product_id
                        or 0
                    )
                    try:
                        import backend.engine.mirror_engine as mirror_module

                        me = (
                            mirror_module.mirror_engine or self.mirror_engine
                        )
                        if me is None:
                            logger.critical(
                                "[MIRROR_PARTIAL_ADJ] trade=%s mirror_engine "
                                "is None — slaves still hold %s leg",
                                trade_id,
                                triggered,
                            )
                        elif old_pid <= 0:
                            logger.critical(
                                "[MIRROR_PARTIAL_ADJ] trade=%s missing "
                                "old_product_id — cannot mirror leg close",
                                trade_id,
                            )
                        else:
                            mirror_counts = await me.mirror_leg_close(
                                master_trade_id=trade_id,
                                leg_type=triggered,
                                product_id=old_pid,
                                success_status="partial",
                                failure_status="exit_failed",
                            )
                            logger.info(
                                "[MIRROR_PARTIAL_ADJ] master_trade_id=%s "
                                "leg=%s product_id=%s slaves_total=%s "
                                "slaves_closed=%s slaves_failed=%s",
                                trade_id,
                                triggered,
                                old_pid,
                                mirror_counts.get("slaves_total"),
                                mirror_counts.get("slaves_closed"),
                                mirror_counts.get("slaves_failed"),
                            )
                            log_and_buffer(
                                "MIRROR_PARTIAL_ADJ",
                                trade_id,
                                {
                                    "leg": triggered,
                                    "product_id": old_pid,
                                    "slaves_total": mirror_counts.get(
                                        "slaves_total"
                                    ),
                                    "slaves_closed": mirror_counts.get(
                                        "slaves_closed"
                                    ),
                                    "slaves_failed": mirror_counts.get(
                                        "slaves_failed"
                                    ),
                                },
                            )
                            if int(
                                mirror_counts.get("slaves_failed") or 0
                            ) > 0:
                                logger.critical(
                                    "[MIRROR_PARTIAL_ADJ] trade=%s leg=%s "
                                    "slaves_failed=%s still hold runaway leg",
                                    trade_id,
                                    triggered,
                                    mirror_counts.get("slaves_failed"),
                                )
                    except Exception as mirror_exc:
                        logger.critical(
                            "[MIRROR_PARTIAL_ADJ] trade=%s FAILED: %s",
                            trade_id,
                            mirror_exc,
                            exc_info=True,
                        )

                    await self._push_error(
                        trade_id,
                        err,
                        requires_manual_action=True,
                        severity="CRITICAL",
                    )
                    return
                else:
                    log_and_buffer(
                        "ADJUSTMENT_FAIL",
                        trade_id,
                        {
                            "leg": triggered,
                            "error": err,
                            "is_partial": False,
                        },
                    )
                    await self._push_error(
                        trade_id,
                        err,
                        requires_manual_action=False,
                    )
        except Exception as exc:
            log_and_buffer(
                "ADJUSTMENT_FAIL",
                trade_id,
                {
                    "leg": triggered,
                    "error": str(exc),
                    "is_partial": False,
                },
            )
            logger.exception("Adjustment crashed for trade %s", trade_id)
            await self._push_error(
                trade_id, f"Adjustment crashed: {exc}", requires_manual_action=True
            )
        finally:
            # ALWAYS release lock — even if adjustment crashes or returns early
            try:
                self.position_tracker.set_adjusting(trade_id, False)
            except Exception as unlock_exc:
                logger.error(
                    "[ADJUST_LOCK_RELEASE] Trade#%s set_adjusting(False) "
                    "failed: %s",
                    trade_id,
                    unlock_exc,
                )
            # DIAGNOSTIC — remove after is_adjusting race root-caused
            _live_d = self.position_tracker.get(trade_id)
            logger.warning(
                "[DIAG_IS_ADJUSTING] (d) _adjust_trade END trade_id=%s "
                "set_adjusting(False) done | tracker.is_adjusting=%s "
                "trade_state.is_adjusting=%s",
                trade_id,
                getattr(_live_d, "is_adjusting", None) if _live_d else None,
                getattr(trade_state, "is_adjusting", None),
            )
            logger.debug(
                "[ADJUST_LOCK_RELEASED] Trade#%s is_adjusting=False",
                trade_id,
            )

    async def _maybe_recover_legs(self, trade_state: TradeState) -> None:
        """
        If Delta still has size for a short call/put product_id but DB marks
        that leg closed, re-open the DB row.

        Recovers baskets damaged by the pre-c93d58b emergency-close-during-
        adjustment race. Throttled to once per trade per 60s.
        """
        trade_id = int(trade_state.trade_id)
        now = time.monotonic()
        last = float(self._leg_recovery_last_checked.get(trade_id, 0.0) or 0.0)
        if now - last < 60.0:
            return
        self._leg_recovery_last_checked[trade_id] = now

        if bool(getattr(trade_state.trade, "is_demo", False)):
            return

        if self.delta_client is None:
            return

        try:
            positions = await self.delta_client.get_positions()
        except Exception as exc:
            logger.warning(
                "[LEG_RECOVERY] Delta positions fetch failed trade=%s: %s",
                trade_id,
                exc,
            )
            return

        size_by_pid: dict[int, int] = {}
        for pos in positions or []:
            try:
                pid = int(pos.get("product_id") or 0)
                size = abs(int(pos.get("size") or 0))
            except (TypeError, ValueError):
                continue
            if pid > 0 and size > 0:
                size_by_pid[pid] = size

        if not size_by_pid:
            return

        try:
            with self.db_factory() as recovery_db:
                recovered = False
                for leg_type in ("call", "put"):
                    leg = (
                        recovery_db.query(Leg)
                        .filter(
                            Leg.trade_id == trade_id,
                            Leg.leg_type == leg_type,
                            Leg.is_bot_managed.is_(True),
                        )
                        .order_by(Leg.id.desc())
                        .first()
                    )
                    if leg is None:
                        continue
                    if str(leg.status or "").lower() != "closed":
                        continue
                    pid = int(leg.product_id or 0)
                    if pid <= 0 or size_by_pid.get(pid, 0) <= 0:
                        continue
                    leg.status = "open"
                    leg.exit_premium = None
                    leg.exit_time = None
                    leg.exit_order_id = None
                    recovered = True
                    logger.warning(
                        "[LEG_RECOVERY] Trade#%s %s leg id=%s product=%s "
                        "re-opened (Delta still has size; was wrongly closed)",
                        trade_id,
                        leg_type,
                        leg.id,
                        pid,
                    )

                if recovered:
                    # Ensure trade stays ACTIVE
                    trade_row = (
                        recovery_db.query(Trade)
                        .filter(Trade.id == trade_id)
                        .first()
                    )
                    if trade_row is not None and str(
                        trade_row.status or ""
                    ).lower() != TradeStatus.ACTIVE.value:
                        trade_row.status = TradeStatus.ACTIVE.value
                        trade_row.exit_reason = None
                        trade_row.exit_time = None
                    recovery_db.commit()
                    # Reload tracker legs so monitor sees both open
                    self._reload_legs(trade_state)
        except Exception as rec_err:
            logger.warning(
                "[LEG_RECOVERY] Failed for Trade#%s: %s",
                trade_id,
                rec_err,
            )

    def _apply_adjustment_cooldown(self, trade_state: TradeState) -> None:
        """
        After a successful adjust, block further adjust/exit for N minutes.

        Without this, the next monitor tick can re-trigger immediately because:
        - new fill baseline is low while ask is still elevated, or
        - nearer-OTM replacements (now forbidden) leave premium hot.
        """
        starts = settling_ends_at(minutes=ADJUSTMENT_COOLDOWN_MINUTES)
        trade_state.trade.monitoring_starts_at = starts
        with self.db_factory() as db:
            row = db.query(Trade).filter(Trade.id == trade_state.trade_id).first()
            if row is not None:
                row.monitoring_starts_at = starts
                db.commit()
        logger.info(
            "Trade %s adjustment cooldown until %s (%sm)",
            trade_state.trade_id,
            starts.isoformat(),
            ADJUSTMENT_COOLDOWN_MINUTES,
        )

    def _reload_legs(self, trade_state: TradeState) -> None:
        """
        Reload open bot-managed legs from DB after adjustment.

        CRITICAL: Must re-query BOTH legs so updated trigger baselines
        (triggered = new fill, untouched = best offer at adjustment) are used
        by the next on_tick() trigger check. Also loads hedge_leg if present.
        """
        with self.db_factory() as db:
            legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_state.trade_id,
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            call_leg = next((leg for leg in legs if leg.leg_type == "call"), None)
            put_leg = next((leg for leg in legs if leg.leg_type == "put"), None)
            hedge_leg = next(
                (
                    leg
                    for leg in legs
                    if bool(getattr(leg, "is_long", False))
                    or str(getattr(leg, "leg_type", "")).startswith("hedge")
                ),
                None,
            )
            if call_leg is None or put_leg is None:
                logger.error(
                    "After adjustment, open legs missing for trade %s "
                    "(call=%s put=%s) — falling back to partial reload",
                    trade_state.trade_id,
                    call_leg is not None,
                    put_leg is not None,
                )
                self._reload_legs_after_partial(trade_state)
                return
            trade_row = (
                db.query(Trade).filter(Trade.id == trade_state.trade_id).first()
            )
            # Detach for in-memory use after session closes
            db.expunge(call_leg)
            db.expunge(put_leg)
            if hedge_leg is not None:
                db.expunge(hedge_leg)
            trade_state.call_leg = call_leg
            trade_state.put_leg = put_leg
            trade_state.hedge_leg = hedge_leg
            if trade_row is not None:
                # Keep in-memory trade.realized_pnl in sync for next on_tick
                trade_state.trade.realized_pnl = float(trade_row.realized_pnl or 0.0)
                trade_state.trade.in_conversion_mode = bool(
                    getattr(trade_row, "in_conversion_mode", False)
                )
            logger.info(
                "Legs reloaded after adjustment: "
                "call entry=%s baseline=%s put entry=%s baseline=%s "
                "hedge=%s trade=%s realized_pnl=%s",
                call_leg.initial_premium,
                getattr(call_leg, "trigger_baseline_premium", None)
                or getattr(call_leg, "trigger_premium", None),
                put_leg.initial_premium,
                getattr(put_leg, "trigger_baseline_premium", None)
                or getattr(put_leg, "trigger_premium", None),
                getattr(hedge_leg, "symbol", None),
                trade_state.trade_id,
                getattr(trade_state.trade, "realized_pnl", 0.0),
            )

    def _reload_legs_after_partial(self, trade_state: TradeState) -> None:
        """
        After partial adjustment: sync in-memory legs from DB.

        One leg is closed; keep the remaining open leg in the tracker.
        Does NOT remove the trade or mark it closed — integrity check must
        see the closed status so it will not emergency-exit the remaining leg.
        """
        from backend.engine.trade_reconcile import pick_call_put_legs

        with self.db_factory() as db:
            all_legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_state.trade_id,
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            call_leg, put_leg = pick_call_put_legs(all_legs)
            trade_row = (
                db.query(Trade).filter(Trade.id == trade_state.trade_id).first()
            )
            if call_leg is None or put_leg is None:
                logger.critical(
                    "Partial reload failed trade=%s — incomplete leg history "
                    "(call=%s put=%s). Keeping prior in-memory legs.",
                    trade_state.trade_id,
                    call_leg is not None,
                    put_leg is not None,
                )
                return

            # Ensure trade stays ACTIVE in DB
            if trade_row is not None:
                if str(trade_row.status).lower() != TradeStatus.ACTIVE.value:
                    logger.critical(
                        "Partial reload: trade %s status was %s — restoring ACTIVE",
                        trade_row.id,
                        trade_row.status,
                    )
                    trade_row.status = TradeStatus.ACTIVE.value
                    trade_row.exit_reason = None
                    trade_row.exit_time = None
                    db.commit()
                trade_state.trade.realized_pnl = float(trade_row.realized_pnl or 0.0)
                trade_state.trade.status = TradeStatus.ACTIVE.value

            db.expunge(call_leg)
            db.expunge(put_leg)
            trade_state.call_leg = call_leg
            trade_state.put_leg = put_leg
            self.position_tracker.update_legs(
                trade_state.trade_id, call_leg, put_leg, trade_state.trade
            )
            open_n = sum(
                1
                for leg in (call_leg, put_leg)
                if str(getattr(leg, "status", "")).lower() == "open"
            )
            logger.critical(
                "Partial reload trade=%s: call=%s(%s) put=%s(%s) open_count=%s "
                "— trade remains in tracker",
                trade_state.trade_id,
                call_leg.symbol,
                call_leg.status,
                put_leg.symbol,
                put_leg.status,
                open_n,
            )

    async def _get_cached_chain(
        self, underlying: str, expiry_date: str
    ) -> list[dict[str, Any]]:
        """Option chain with 60s in-memory cache."""
        assert self.delta_client is not None
        key = f"{underlying.upper()}_{expiry_date}"
        now = time.monotonic()
        cached = self._chain_cache.get(key)
        if cached is not None and (now - cached[0]) < _CHAIN_CACHE_TTL_SECONDS:
            return cached[1]
        chain = await self.delta_client.get_option_chain(underlying, expiry_date)
        self._chain_cache[key] = (now, chain)
        return chain

    @staticmethod
    def _find_nearest_premium(
        chain: list[dict[str, Any]],
        leg_type: str,
        target_premium: float,
        exclude_strike: float | None = None,
    ) -> dict[str, Any] | None:
        """UI estimate: different strike with mark nearest to other-leg premium."""
        if not chain or target_premium <= 0:
            return None
        mark_key = f"{leg_type}_mark_price"
        symbol_key = f"{leg_type}_symbol"
        excl = float(exclude_strike) if exclude_strike is not None else None
        target = float(target_premium)

        best: dict[str, Any] | None = None
        best_key: tuple[float, float] | None = None
        for row in chain:
            try:
                strike = float(row.get("strike") or 0)
                mark = float(row.get(mark_key) or 0)
            except (TypeError, ValueError):
                continue
            if mark <= 0:
                continue
            if excl is not None and abs(strike - excl) < 0.01:
                continue
            prem_diff = abs(mark - target)
            # Tie-break only: prefer farther OTM when premiums equal
            otm_rank = -strike if leg_type == "call" else strike
            key = (prem_diff, otm_rank)
            if best_key is None or key < best_key:
                best_key = key
                best = row
        if best is None:
            return None
        return {
            "symbol": str(best.get(symbol_key) or ""),
            "strike": float(best.get("strike") or 0),
            "premium": float(best.get(mark_key) or 0),
        }

    async def _estimate_replacements(
        self,
        trade_state: TradeState,
        call_premium: float,
        put_premium: float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """
        If call triggers → new call matches current put premium.
        If put triggers → new put matches current call premium.
        """
        call_repl = None
        put_repl = None
        try:
            if self.delta_client is None:
                return None, None
            expiry = str(trade_state.trade.expiry_date)
            underlying = str(trade_state.trade.underlying)
            chain = await self._get_cached_chain(underlying, expiry)
            # Call replacement should be farther OTM than current call when possible
            call_repl = self._find_nearest_premium(
                chain,
                "call",
                put_premium,
                exclude_strike=float(trade_state.call_leg.strike),
            )
            put_repl = self._find_nearest_premium(
                chain,
                "put",
                call_premium,
                exclude_strike=float(trade_state.put_leg.strike),
            )
            self._replacement_estimates[trade_state.trade_id] = {
                "estimated_call_replacement": call_repl,
                "estimated_put_replacement": put_repl,
            }
        except Exception as exc:
            logger.warning(
                "Could not fetch replacement estimates for trade %s: %s",
                trade_state.trade_id,
                exc,
            )
            cached = self._replacement_estimates.get(trade_state.trade_id) or {}
            call_repl = cached.get("estimated_call_replacement")
            put_repl = cached.get("estimated_put_replacement")
        return call_repl, put_repl

    def build_bot_plan_fields(
        self,
        trade_state: TradeState,
        call_prem: float,
        put_prem: float,
        trigger_pct: float,
        call_replacement: dict[str, Any] | None = None,
        put_replacement: dict[str, Any] | None = None,
        call_trigger_pct: float | None = None,
        put_trigger_pct: float | None = None,
        premium_slabs: dict[str, float] | None = None,
        net_mtm: float | None = None,
        gross_mtm_for_stoploss: float | None = None,
        conversion_min_premium: float | None = None,
        conversion_equality_pct: float | None = None,
        conversion_enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Shared monitoring-plan fields for WS and /api/trade/active.

        Entry display = initial_premium (never changes for a leg row).
        Trigger calc = trigger_baseline_premium (resets each adjustment).
        Premium mode: call_trigger_pct / put_trigger_pct may differ.
        """

        def _baseline(leg: Any) -> float:
            for attr in ("trigger_baseline_premium", "trigger_premium"):
                val = getattr(leg, attr, None)
                if val is not None and float(val) > 0:
                    return float(val)
            return float(getattr(leg, "initial_premium", 0) or 0)

        def _parse_strike_from_symbol(symbol: str | None) -> float | None:
            parts = str(symbol or "").split("-")
            if len(parts) >= 3:
                try:
                    return float(parts[2])
                except (TypeError, ValueError):
                    return None
            return None

        call_entry = float(trade_state.call_leg.initial_premium or 0)
        put_entry = float(trade_state.put_leg.initial_premium or 0)
        call_base = _baseline(trade_state.call_leg)
        put_base = _baseline(trade_state.put_leg)
        fallback_pct = float(trigger_pct) if trigger_pct > 0 else 150.0
        call_pct = (
            float(call_trigger_pct)
            if call_trigger_pct is not None and call_trigger_pct > 0
            else fallback_pct
        )
        put_pct = (
            float(put_trigger_pct)
            if put_trigger_pct is not None and put_trigger_pct > 0
            else fallback_pct
        )
        call_trigger = call_base * (call_pct / 100.0)
        put_trigger = put_base * (put_pct / 100.0)
        call_pct_to = (call_prem / call_trigger * 100.0) if call_trigger > 0 else 0.0
        put_pct_to = (put_prem / put_trigger * 100.0) if put_trigger > 0 else 0.0

        cached = self._replacement_estimates.get(trade_state.trade_id) or {}
        if call_replacement is None:
            call_replacement = cached.get("estimated_call_replacement")
        if put_replacement is None:
            put_replacement = cached.get("estimated_put_replacement")

        mode = str(getattr(trade_state.trade, "trigger_mode", "slab") or "slab")
        uni_sl = float(getattr(trade_state.trade, "universal_sl_pct", None) or 200.0)
        call_sl_px = getattr(trade_state.call_leg, "sl_trigger_price", None)
        put_sl_px = getattr(trade_state.put_leg, "sl_trigger_price", None)
        call_sl_id = getattr(trade_state.call_leg, "delta_sl_order_id", None)
        put_sl_id = getattr(trade_state.put_leg, "delta_sl_order_id", None)
        # Fallback estimate if order not stored yet — still from master fill baseline
        if call_sl_px is None or float(call_sl_px or 0) <= 0:
            from backend.core.delta_sl import compute_bracket_sl

            call_sl_px, _ = compute_bracket_sl(
                float(call_base or 0),
                uni_sl,
                leg="call",
                trade_id=int(trade_state.trade_id),
            )
            call_sl_px = call_sl_px if call_sl_px > 0 else None
        if put_sl_px is None or float(put_sl_px or 0) <= 0:
            from backend.core.delta_sl import compute_bracket_sl

            put_sl_px, _ = compute_bracket_sl(
                float(put_base or 0),
                uni_sl,
                leg="put",
                trade_id=int(trade_state.trade_id),
            )
            put_sl_px = put_sl_px if put_sl_px > 0 else None

        trade = trade_state.trade
        target_usd = float(getattr(trade, "profit_target_usd", 0) or 0)
        stoploss_usd = float(getattr(trade, "stoploss_usd", 0) or 0)
        net_now = float(net_mtm if net_mtm is not None else 0.0)
        gross_sl_now = float(
            gross_mtm_for_stoploss
            if gross_mtm_for_stoploss is not None
            else 0.0
        )
        conv_min = float(
            conversion_min_premium
            if conversion_min_premium is not None
            else 150.0
        )
        eq_pct = float(
            conversion_equality_pct
            if conversion_equality_pct is not None
            else float(getattr(trade, "conversion_equality_pct", None) or 10.0)
        )
        conv_on = bool(
            conversion_enabled
            if conversion_enabled is not None
            else False
        )
        in_conversion = bool(getattr(trade, "in_conversion_mode", False))

        closer_leg = "call" if call_pct_to >= put_pct_to else "put"
        pct_to_tp = (net_now / target_usd * 100.0) if target_usd > 0 else 0.0
        pct_to_sl = (
            (abs(gross_sl_now) / stoploss_usd * 100.0) if stoploss_usd > 0 else 0.0
        )

        # Combined premium trigger (call+put vs combined entry × %)
        combined_mode = bool(getattr(trade, "combined_trigger_mode", False))
        mode_l = str(mode or "slab").lower()
        if mode_l == "premium":
            combined_trig_pct = (call_pct + put_pct) / 2.0
        else:
            combined_trig_pct = float(call_pct)
        combined_entry = call_entry + put_entry
        combined_current = float(call_prem) + float(put_prem)
        combined_threshold = (
            combined_entry * (combined_trig_pct / 100.0)
            if combined_entry > 0
            else 0.0
        )
        combined_pct_to = (
            (combined_current / combined_threshold * 100.0)
            if combined_threshold > 0
            else 0.0
        )
        # Which leg moved more vs entry (for combined adjust target)
        call_move = (call_prem / call_entry) if call_entry > 0 else 0.0
        put_move = (put_prem / put_entry) if put_entry > 0 else 0.0
        combined_triggered_leg = "call" if call_move >= put_move else "put"

        # Priority: conversion → near SL → near TP → adjust/conversion_likely → HOLD
        if in_conversion:
            hedge_sym = getattr(trade, "conversion_hedge_symbol", None)
            next_action = "REVERSAL_WATCH" if hedge_sym else "CONVERSION_ACTIVE"
        elif stoploss_usd > 0 and pct_to_sl >= 80.0 and gross_sl_now < 0:
            next_action = "STOPLOSS_NEAR"
        elif target_usd > 0 and pct_to_tp >= 80.0:
            next_action = "PROFIT_TARGET_NEAR"
        elif combined_mode and combined_pct_to >= 80.0:
            triggered = combined_triggered_leg
            other_offer = float(put_prem if triggered == "call" else call_prem)
            if conv_on and other_offer < conv_min:
                next_action = "CONVERSION_LIKELY"
            else:
                next_action = (
                    "ADJUST_CALL" if triggered == "call" else "ADJUST_PUT"
                )
            closer_leg = triggered
        elif (not combined_mode) and (call_pct_to >= 80.0 or put_pct_to >= 80.0):
            triggered = closer_leg
            other_offer = float(put_prem if triggered == "call" else call_prem)
            if conv_on and other_offer < conv_min:
                next_action = "CONVERSION_LIKELY"
            else:
                next_action = (
                    "ADJUST_CALL" if triggered == "call" else "ADJUST_PUT"
                )
        else:
            next_action = "HOLD"

        next_plan: dict[str, Any] = {
            "next_action": next_action,
            "closer_leg": closer_leg,
            "call_pct_to_trigger": round(call_pct_to, 2),
            "put_pct_to_trigger": round(put_pct_to, 2),
            "exit_conditions_watch": {
                "profit_target_usd": target_usd,
                "stoploss_usd": stoploss_usd,
                "current_net_mtm": round(net_now, 4),
                "current_gross_for_sl": round(gross_sl_now, 4),
                "pct_to_profit_target": round(pct_to_tp, 2),
                "pct_to_stoploss": round(pct_to_sl, 2),
            },
        }

        if next_action in ("ADJUST_CALL", "ADJUST_PUT", "CONVERSION_LIKELY"):
            triggered = "call" if next_action == "ADJUST_CALL" else (
                "put" if next_action == "ADJUST_PUT" else closer_leg
            )
            other_offer = float(put_prem if triggered == "call" else call_prem)
            repl = call_replacement if triggered == "call" else put_replacement
            adj_type = (
                "conversion_likely"
                if (conv_on and other_offer < conv_min)
                else "normal"
            )
            next_plan.update(
                {
                    "triggered_leg": triggered,
                    "other_leg_current_offer": round(other_offer, 4),
                    "estimated_new_strike": (
                        repl.get("strike")
                        if isinstance(repl, dict) and repl.get("strike") is not None
                        else "calculating..."
                    ),
                    "estimated_new_premium": (
                        float(repl["premium"])
                        if isinstance(repl, dict) and repl.get("premium") is not None
                        else None
                    ),
                    "adjustment_type": adj_type,
                    "conversion_min_premium": conv_min,
                }
            )

            if adj_type == "conversion_likely":
                strike_increment = 200.0
                triggered_leg_strike = float(
                    trade_state.call_leg.strike
                    if triggered == "call"
                    else trade_state.put_leg.strike
                )
                other_leg_strike = float(
                    trade_state.put_leg.strike
                    if triggered == "call"
                    else trade_state.call_leg.strike
                )
                if triggered == "call":
                    hedge_strike = triggered_leg_strike - strike_increment
                    hedge_type = "C"
                    new_other_leg_side = "put"
                else:
                    hedge_strike = triggered_leg_strike + strike_increment
                    hedge_type = "P"
                    new_other_leg_side = "call"
                triggered_leg_current_offer = float(
                    call_prem if triggered == "call" else put_prem
                )
                # Parse expiry suffix from existing symbol (e.g. C-BTC-64400-110826)
                sym_parts = str(
                    getattr(trade_state.call_leg, "symbol", "") or ""
                ).split("-")
                expiry_code = sym_parts[-1] if len(sym_parts) >= 4 else ""
                underlying_code = (
                    str(getattr(trade, "underlying", "BTC") or "BTC").upper()
                )
                if expiry_code:
                    hedge_symbol_expected = (
                        f"{hedge_type}-{underlying_code}-"
                        f"{int(hedge_strike)}-{expiry_code}"
                    )
                else:
                    hedge_symbol_expected = (
                        f"{hedge_type}-{underlying_code}-{int(hedge_strike)}"
                    )
                new_other_target = triggered_leg_current_offer / 2.0
                next_plan["conversion_plan"] = {
                    "triggered_leg": triggered,
                    "triggered_strike": triggered_leg_strike,
                    "triggered_current_offer": round(triggered_leg_current_offer, 2),
                    "hedge_action": "BUY " + (
                        "CALL" if triggered == "call" else "PUT"
                    ),
                    "hedge_strike": hedge_strike,
                    "hedge_symbol_expected": hedge_symbol_expected,
                    "keep_triggered_short": True,
                    "close_other_leg": new_other_leg_side,
                    "other_leg_strike": other_leg_strike,
                    "new_other_leg_side": new_other_leg_side,
                    "new_other_target_premium": round(new_other_target, 2),
                    "new_other_target_note": (
                        "estimated as triggered_offer/2; actual will be hedge_fill/2"
                    ),
                    "conversion_min_premium": conv_min,
                    "other_leg_current_offer": round(other_offer, 2),
                    "why_conversion": (
                        f"Other leg offer ${round(other_offer, 2)} "
                        f"< minimum ${round(conv_min, 2)}"
                    ),
                }

        if next_action in ("CONVERSION_ACTIVE", "REVERSAL_WATCH"):
            hedge_sym = str(getattr(trade, "conversion_hedge_symbol", "") or "")
            hedge_entry = float(
                getattr(trade, "conversion_hedge_entry_price", 0) or 0
            )
            avg_prem = (float(call_prem) + float(put_prem)) / 2.0
            prem_eq = (
                abs(float(call_prem) - float(put_prem)) / avg_prem * 100.0
                if avg_prem > 0
                else 0.0
            )
            next_plan.update(
                {
                    "hedge_symbol": hedge_sym or None,
                    "hedge_entry_price": hedge_entry,
                    "hedge_strike": _parse_strike_from_symbol(hedge_sym),
                    "short_call_premium": round(float(call_prem), 4),
                    "short_put_premium": round(float(put_prem), 4),
                    "premium_equality_pct": round(prem_eq, 2),
                    "equality_threshold_pct": eq_pct,
                    "reversal_condition": (
                        f"Waiting for |call-put|/avg <= {eq_pct:.1f}% "
                        f"(now {prem_eq:.1f}%)"
                    ),
                }
            )

        out: dict[str, Any] = {
            "call_entry_premium": call_entry,
            "put_entry_premium": put_entry,
            "call_trigger_baseline": call_base,
            "put_trigger_baseline": put_base,
            "call_strike": float(trade_state.call_leg.strike),
            "put_strike": float(trade_state.put_leg.strike),
            "call_symbol": str(trade_state.call_leg.symbol),
            "put_symbol": str(trade_state.put_leg.symbol),
            "call_quantity": int(trade_state.call_leg.quantity),
            "put_quantity": int(trade_state.put_leg.quantity),
            "current_trigger_pct": call_pct,  # primary / call (compat)
            "call_trigger_pct": round(call_pct, 2),
            "put_trigger_pct": round(put_pct, 2),
            "call_trigger_price": round(call_trigger, 4),
            "put_trigger_price": round(put_trigger, 4),
            "call_pct_to_trigger": round(call_pct_to, 2),
            "put_pct_to_trigger": round(put_pct_to, 2),
            "call_distance_to_trigger": round(call_trigger - call_prem, 4),
            "put_distance_to_trigger": round(put_trigger - put_prem, 4),
            "estimated_call_replacement": call_replacement,
            "estimated_put_replacement": put_replacement,
            "trigger_mode": mode,
            "combined_trigger_mode": combined_mode,
            "combined_entry_premium": round(combined_entry, 4),
            "combined_current_premium": round(combined_current, 4),
            "combined_trigger_pct": round(combined_trig_pct, 2),
            "combined_trigger_threshold": round(combined_threshold, 4),
            "combined_pct_to_trigger": round(combined_pct_to, 2),
            "combined_triggered_leg": combined_triggered_leg,
            "universal_sl_pct": uni_sl,
            "call_sl_trigger_price": (
                float(call_sl_px) if call_sl_px is not None else None
            ),
            "put_sl_trigger_price": (
                float(put_sl_px) if put_sl_px is not None else None
            ),
            "call_sl_order_id": call_sl_id,
            "put_sl_order_id": put_sl_id,
            # For bracket SLs, delta_sl_order_id may be None, but sl_trigger_price is present.
            "delta_sl_active": bool(call_sl_px and put_sl_px),
            "next_action_plan": next_plan,
            "bot_next_action": next_action,
            "bot_closer_leg": closer_leg,
            "bot_call_pct_to_trigger": round(call_pct_to, 2),
            "bot_put_pct_to_trigger": round(put_pct_to, 2),
        }
        if premium_slabs:
            out.update(
                {
                    "premium_slab_300": float(
                        premium_slabs.get("premium_slab_300", 150)
                    ),
                    "premium_slab_200": float(
                        premium_slabs.get("premium_slab_200", 160)
                    ),
                    "premium_slab_100": float(
                        premium_slabs.get("premium_slab_100", 180)
                    ),
                    "premium_slab_lt100": float(
                        premium_slabs.get("premium_slab_lt100", 200)
                    ),
                }
            )
        return out

    async def _push_update(
        self,
        trade_state: TradeState,
        call_prem: float,
        put_prem: float,
        calculated_pnl: float,
        delta_mtm: float,
        call_delta_mtm: float,
        put_delta_mtm: float,
        trigger_pct: float = 0.0,
        call_replacement: dict[str, Any] | None = None,
        put_replacement: dict[str, Any] | None = None,
        call_trigger_pct: float | None = None,
        put_trigger_pct: float | None = None,
        premium_slabs: dict[str, float] | None = None,
    ) -> None:
        call_change = 0.0
        put_change = 0.0
        if float(trade_state.call_leg.initial_premium) > 0:
            call_change = (
                (call_prem / float(trade_state.call_leg.initial_premium)) - 1.0
            ) * 100.0
        if float(trade_state.put_leg.initial_premium) > 0:
            put_change = (
                (put_prem / float(trade_state.put_leg.initial_premium)) - 1.0
            ) * 100.0

        target = float(getattr(trade_state.trade, "profit_target_usd", 0) or 0)
        stoploss = float(getattr(trade_state.trade, "stoploss_usd", 0) or 0)
        realized_pnl = float(getattr(trade_state.trade, "realized_pnl", None) or 0.0)
        # Display total uses Delta official MTM + realized
        display_total = realized_pnl + float(delta_mtm)
        calculated_total = float(calculated_pnl)
        pnl_pct_of_target = (display_total / target * 100.0) if target else 0.0
        settling = get_settling_info(
            getattr(trade_state.trade, "monitoring_starts_at", None)
        )

        # Fees from DB legs + slippage for Net MTM on TRADE_UPDATE
        from backend.core.fees import (
            basket_fees_paid_from_legs,
            compute_net_mtm,
            estimate_expected_exit_spread_usd,
            estimate_option_trading_fee,
            get_entry_spread_for_sl,
        )
        from backend.models import Leg as LegModel

        fees_paid = 0.0
        est_exit = 0.0
        expected_exit_spread = 0.0
        conversion_equality_pct = 10.0
        conversion_min_premium = 150.0
        conversion_enabled = False
        max_adjustments_per_basket = None
        conversion_mode_enabled_flag = True
        with self.db_factory() as db:
            from backend.database import get_or_create_auto_settings

            try:
                _cfg = get_or_create_auto_settings(db)
                conversion_equality_pct = float(
                    getattr(_cfg, "conversion_equality_pct", 10.0) or 10.0
                )
                conversion_min_premium = float(
                    getattr(_cfg, "adj_low_premium_min_usd", 150.0) or 150.0
                )
                conversion_enabled = bool(
                    getattr(_cfg, "adj_low_premium_exit_enabled", False)
                ) and bool(getattr(_cfg, "conversion_mode_enabled", True))
                max_adjustments_per_basket = getattr(
                    _cfg, "max_adjustments_per_basket", None
                )
                if max_adjustments_per_basket is not None:
                    max_adjustments_per_basket = int(max_adjustments_per_basket)
                conversion_mode_enabled_flag = bool(
                    getattr(_cfg, "conversion_mode_enabled", True)
                )
            except Exception:
                conversion_equality_pct = 10.0
                conversion_min_premium = 150.0
                conversion_enabled = False
                max_adjustments_per_basket = None
                conversion_mode_enabled_flag = True

            legs = (
                db.query(LegModel)
                .filter(
                    LegModel.trade_id == trade_state.trade_id,
                    LegModel.is_bot_managed.is_(True),
                )
                .all()
            )
            fees_paid = basket_fees_paid_from_legs(legs)
            btc = float(self._btc_spot or 0)
            for leg in legs:
                if str(getattr(leg, "status", "") or "").lower() != "open":
                    continue
                lt = str(leg.leg_type or "").lower()
                if lt == "call":
                    offer = float(call_prem)
                elif lt == "put":
                    offer = float(put_prem)
                else:
                    offer = float(
                        self._live_prices.get(str(leg.symbol), 0)
                        or leg.initial_premium
                        or 0
                    )
                if offer > 0 and btc > 0:
                    est_exit += estimate_option_trading_fee(
                        option_price=offer,
                        quantity_lots=int(leg.quantity or 0),
                        btc_index_price=btc,
                    )
                if offer > 0:
                    expected_exit_spread += estimate_expected_exit_spread_usd(
                        offer_price=offer,
                        quantity=int(leg.quantity or 0),
                    )

        entry_spread_for_sl = get_entry_spread_for_sl(trade_state.trade)
        gross_mtm_for_stoploss = float(display_total) + entry_spread_for_sl

        slip_fields = compute_net_mtm(
            gross_mtm=display_total,
            fees_paid=fees_paid,
            est_exit_fees=est_exit,
            slippage_pct=getattr(trade_state.trade, "slippage_pct", None),
            expected_exit_spread_usd=expected_exit_spread,
        )
        slip_pct = float(slip_fields["slippage_pct"])
        slip_amt = float(slip_fields["slippage_amount"])
        net_mtm_out = float(slip_fields["net_mtm"])
        total_deductions = float(slip_fields["total_deductions"])

        plan = self.build_bot_plan_fields(
            trade_state,
            call_prem,
            put_prem,
            trigger_pct,
            call_replacement,
            put_replacement,
            call_trigger_pct=call_trigger_pct,
            put_trigger_pct=put_trigger_pct,
            premium_slabs=premium_slabs,
            net_mtm=net_mtm_out,
            gross_mtm_for_stoploss=gross_mtm_for_stoploss,
            conversion_min_premium=conversion_min_premium,
            conversion_equality_pct=conversion_equality_pct,
            conversion_enabled=conversion_enabled,
        )

        payload: dict[str, Any] = {
            "type": "TRADE_UPDATE",
            "trade_id": trade_state.trade_id,
            "underlying": getattr(trade_state.trade, "underlying", ""),
            "call_premium": call_prem,
            "put_premium": put_prem,
            "call_change_pct": call_change,
            "put_change_pct": put_change,
            "calculated_pnl": calculated_total,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": float(delta_mtm),
            "total_pnl": display_total,
            "delta_mtm_pnl": delta_mtm,
            "delta_upnl": float(delta_mtm),
            "call_delta_mtm": call_delta_mtm,
            "put_delta_mtm": put_delta_mtm,
            "call_upnl": float(call_delta_mtm),
            "put_upnl": float(put_delta_mtm),
            "call_offer": float(call_prem),
            "put_offer": float(put_prem),
            "pnl": delta_mtm,
            "gross_mtm": display_total,
            "gross_mtm_for_stoploss": round(gross_mtm_for_stoploss, 4),
            "entry_spread_for_sl": round(entry_spread_for_sl, 4),
            "expected_exit_spread_usd": round(expected_exit_spread, 4),
            "fees_paid": round(fees_paid, 6),
            "est_exit_fees": round(est_exit, 6),
            "total_expected_fees": round(fees_paid + est_exit, 6),
            "underlying_price": float(self._btc_spot or 0) or None,
            "last_mtm_update": get_ist_now().strftime("%H:%M:%S IST"),
            "pnl_pct_of_target": pnl_pct_of_target,
            "profit_target_usd": target,
            "stoploss_usd": stoploss,
            "initial_max_profit": float(
                getattr(trade_state.trade, "initial_max_profit", None) or 0
            )
            or None,
            "tp_pct": float(getattr(trade_state.trade, "tp_pct", None) or 50.0),
            "sl_pct": float(getattr(trade_state.trade, "sl_pct", None) or 100.0),
            "hours_to_expiry": get_hours_to_expiry(trade_state.trade.expiry_date),
            "status": "active",
            "is_settling": settling["is_settling"],
            "settling_ends_at": settling["settling_ends_at"],
            "settling_minutes_left": settling["settling_minutes_left"],
            **plan,
            # Slippage AFTER plan so keys are never overwritten
            "slippage_pct": slip_pct,
            "slippage_amount": slip_amt,
            "total_deductions": total_deductions,
            "net_mtm": net_mtm_out,
            "in_conversion_mode": bool(
                getattr(trade_state.trade, "in_conversion_mode", False)
            ),
            "conversion_hedge_symbol": getattr(
                trade_state.trade, "conversion_hedge_symbol", None
            ),
            "conversion_hedge_entry_price": float(
                getattr(trade_state.trade, "conversion_hedge_entry_price", 0) or 0
            ),
            "conversion_triggered_leg": getattr(
                trade_state.trade, "conversion_triggered_leg", None
            ),
            "conversion_equality_pct": conversion_equality_pct,
            "conversion_mode_enabled": conversion_mode_enabled_flag,
            "adjustment_count": int(
                getattr(trade_state.trade, "adjustment_count", 0) or 0
            ),
            "max_adjustments_per_basket": (
                max_adjustments_per_basket
                if not conversion_mode_enabled_flag
                else None
            ),
            "adjustments_remaining": (
                max(
                    0,
                    int(max_adjustments_per_basket)
                    - int(
                        getattr(trade_state.trade, "adjustment_count", 0) or 0
                    ),
                )
                if (
                    not conversion_mode_enabled_flag
                    and max_adjustments_per_basket is not None
                )
                else None
            ),
        }
        if bool(getattr(trade_state.trade, "in_conversion_mode", False)):
            # Hedge UPNL if available on trade_state
            hedge_leg = getattr(trade_state, "hedge_leg", None)
            if hedge_leg is not None:
                hedge_px = float(
                    self._live_prices.get(str(hedge_leg.symbol), 0)
                    or getattr(hedge_leg, "initial_premium", 0)
                    or 0
                )
                hedge_entry = float(getattr(hedge_leg, "initial_premium", 0) or 0)
                hedge_qty = abs(int(getattr(hedge_leg, "quantity", 0) or 0))
                if hedge_px > 0 and hedge_entry > 0:
                    payload["hedge_upnl"] = round(
                        (hedge_px - hedge_entry)
                        * hedge_qty
                        * float(OPTIONS_CONTRACT_VALUE),
                        4,
                    )
        logger.info(
            "TRADE_UPDATE slippage: trade=%s pct=%s amount=%s net=%s "
            "gross=%s fees=%s exit_fees=%s keys=%s",
            trade_state.trade_id,
            slip_pct,
            slip_amt,
            net_mtm_out,
            display_total,
            fees_paid,
            est_exit,
            sorted(
                k
                for k in (
                    "slippage_pct",
                    "slippage_amount",
                    "total_deductions",
                    "net_mtm",
                )
                if k in payload
            ),
        )
        await ws_manager.broadcast(payload)

    async def _push_adjustment(
        self,
        trade_state: TradeState,
        triggered_leg_type: str,
        result: Any,
    ) -> None:
        call_leg = trade_state.call_leg
        put_leg = trade_state.put_leg
        call_prem = float(
            getattr(trade_state, "last_call_premium", 0)
            or getattr(call_leg, "initial_premium", 0)
            or 0
        )
        put_prem = float(
            getattr(trade_state, "last_put_premium", 0)
            or getattr(put_leg, "initial_premium", 0)
            or 0
        )
        if str(triggered_leg_type).lower() == "call":
            call_prem = float(result.premium_collected or call_prem)
        else:
            put_prem = float(result.premium_collected or put_prem)

        settling = get_settling_info(
            getattr(trade_state.trade, "monitoring_starts_at", None)
        )

        def _snap(leg: Any, prem: float) -> dict[str, Any]:
            initial = float(leg.initial_premium or 0)
            change = ((prem / initial) - 1.0) * 100.0 if initial else 0.0
            return {
                "id": int(getattr(leg, "id", 0) or 0),
                "strike": float(leg.strike),
                "symbol": str(leg.symbol),
                "quantity": int(leg.quantity),
                "initial_premium": initial,
                "current_premium": float(prem),
                "exit_premium": (
                    float(leg.exit_premium)
                    if getattr(leg, "exit_premium", None) is not None
                    else None
                ),
                "change_pct": round(change, 2),
                "status": str(getattr(leg, "status", "open") or "open"),
            }

        leg_history: list[dict[str, Any]] = []
        adj_count = 0
        trigger_pct = 150.0
        with self.db_factory() as db:
            from backend.models import Adjustment

            legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_state.trade_id,
                    Leg.is_bot_managed.is_(True),
                )
                .order_by(Leg.id.asc())
                .all()
            )
            for leg in legs:
                leg_history.append(
                    {
                        "id": int(leg.id),
                        "leg_type": leg.leg_type,
                        "strike": float(leg.strike),
                        "symbol": leg.symbol,
                        "quantity": int(leg.quantity),
                        "initial_premium": float(leg.initial_premium or 0),
                        "exit_premium": (
                            float(leg.exit_premium)
                            if leg.exit_premium is not None
                            else None
                        ),
                        "status": str(leg.status or "open"),
                        "realized_pnl": (
                            float(leg.realized_pnl)
                            if getattr(leg, "realized_pnl", None) is not None
                            else None
                        ),
                    }
                )
            adj_count = (
                db.query(Adjustment)
                .filter(Adjustment.trade_id == trade_state.trade_id)
                .count()
            )
            try:
                call_trig_pct = float(
                    self.strategy.get_trigger_for_leg(
                        call_prem, trade_state.trade, db
                    )
                )
                put_trig_pct = float(
                    self.strategy.get_trigger_for_leg(
                        put_prem, trade_state.trade, db
                    )
                )
                trigger_pct = call_trig_pct
                premium_slabs = None
                if (
                    str(getattr(trade_state.trade, "trigger_mode", "") or "").lower()
                    == "premium"
                ):
                    premium_slabs = self.strategy.get_slabs(
                        trade_state.trade.id, db
                    )
            except Exception:
                trigger_pct = 150.0
                call_trig_pct = 150.0
                put_trig_pct = 150.0
                premium_slabs = None

        plan = self.build_bot_plan_fields(
            trade_state,
            call_prem,
            put_prem,
            trigger_pct,
            call_trigger_pct=call_trig_pct,
            put_trigger_pct=put_trig_pct,
            premium_slabs=premium_slabs,
            net_mtm=None,
            gross_mtm_for_stoploss=None,
        )

        await ws_manager.broadcast(
            {
                "type": "ADJUSTMENT",
                "trade_id": trade_state.trade_id,
                "leg_type": triggered_leg_type,
                "old_strike": result.old_strike,
                "new_strike": result.new_strike,
                "premium_collected": result.premium_collected,
                "timestamp": get_ist_now().isoformat(),
                "call_leg": _snap(call_leg, call_prem),
                "put_leg": _snap(put_leg, put_prem),
                "leg_history": leg_history,
                "call_premium": call_prem,
                "put_premium": put_prem,
                "adjustment_count": adj_count,
                "is_settling": settling["is_settling"],
                "settling_ends_at": settling["settling_ends_at"],
                "settling_minutes_left": settling["settling_minutes_left"],
                "open_leg_count": sum(
                    1
                    for x in (
                        str(getattr(call_leg, "status", "open")).lower() == "open",
                        str(getattr(put_leg, "status", "open")).lower() == "open",
                    )
                    if x
                ),
                **plan,
            }
        )

    async def _push_error(
        self,
        trade_id: int,
        message: str,
        requires_manual_action: bool = False,
        severity: str = "ERROR",
    ) -> None:
        await ws_manager.broadcast(
            {
                "type": "ERROR",
                "trade_id": trade_id,
                "message": message,
                "requires_manual_action": requires_manual_action,
                "severity": severity,
            }
        )

    def get_initial_state_payload(self) -> dict[str, Any]:
        """Snapshot of all active trades for new WebSocket clients."""
        trades: list[dict[str, Any]] = []
        for state in self.position_tracker.get_all_active():
            settling = get_settling_info(
                getattr(state.trade, "monitoring_starts_at", None)
            )
            trades.append(
                {
                    "trade_id": state.trade_id,
                    "underlying": getattr(state.trade, "underlying", ""),
                    "call_premium": state.last_call_premium,
                    "put_premium": state.last_put_premium,
                    "calculated_pnl": state.last_pnl,
                    "delta_mtm_pnl": state.last_delta_mtm,
                    "hours_to_expiry": get_hours_to_expiry(state.trade.expiry_date),
                    "status": "active",
                    "is_settling": settling["is_settling"],
                    "settling_ends_at": settling["settling_ends_at"],
                    "settling_minutes_left": settling["settling_minutes_left"],
                }
            )
        return {"type": "INITIAL_STATE", "trades": trades}

    def _refresh_delta_client(self) -> None:
        """Build DeltaClient from first active encrypted account in DB."""
        try:
            with self.db_factory() as db:
                account = (
                    db.query(Account)
                    .filter(Account.is_active.is_(True))
                    .order_by(Account.id.asc())
                    .first()
                )
                if account is None:
                    logger.info("No active account — bot will idle until connect")
                    self.delta_client = None
                    return
                api_key = decrypt(account.api_key_encrypted)
                api_secret = decrypt(account.api_secret_encrypted)
            self.delta_client = DeltaClient(api_key, api_secret)
            logger.info("Delta client attached for bot engine")
        except Exception as exc:
            logger.error("Failed to build Delta client: %s", exc, exc_info=True)
            self.delta_client = None

    @staticmethod
    def _status_for_reason(reason: str) -> str:
        if reason == ExitReason.MANUAL_EMERGENCY.value:
            return TradeStatus.EMERGENCY_CLOSED.value
        if reason == ExitReason.PRE_EXPIRY.value:
            return TradeStatus.EXPIRED.value
        return TradeStatus.CLOSED.value


def _build_bot_engine() -> BotEngine:
    return BotEngine(
        db_factory=SessionLocal,
        strategy=ShortStrangleStrategy(),
        position_tracker=PositionTracker(),
        order_executor=OrderExecutor(),
        adjustment_executor=AdjustmentExecutor(),
        delta_client=None,
    )


# Global singleton — imported by routes (e.g. routes_trade / routes_ws)
bot_engine = _build_bot_engine()
