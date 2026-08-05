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
from backend.core.delta_client import DeltaClient, compute_signed_upnl
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

                with SessionLocal() as db:
                    recon = await reconcile_open_legs_with_delta(
                        db=db,
                        client=self.delta_client,
                        position_tracker=self.position_tracker,
                    )
                closed_ids = list(recon.get("fully_closed") or [])
                for tid in closed_ids:
                    await ws_manager.broadcast(
                        {
                            "type": "TRADE_CLOSED",
                            "trade_id": tid,
                            "reason": ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value,
                            "message": "Basket closed — Delta size flat",
                        }
                    )
                for alert in recon.get("naked_risk") or []:
                    tid = int(alert["trade_id"])
                    remaining = str(alert["remaining"])
                    state = self.position_tracker.get(tid)
                    if state is None:
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
                    await self._emergency_close_remaining_leg(state, remaining)
            except Exception as exc:
                logger.warning("Monitor reconcile failed: %s", exc)

        count = len(self.position_tracker.get_all_active())
        if self._last_trade_count != count:
            logger.info("Active trades in tracker: %s", count)
            self._last_trade_count = count

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

    def _check_partial_position(
        self, trade_state: TradeState, positions: list[dict[str, Any]]
    ) -> str:
        """Return 'both_open' | 'call_only' | 'put_only' | 'none'."""
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
        Only runs when bot still thinks BOTH legs are open.
        """
        call_open = (
            str(getattr(trade_state.call_leg, "status", "open")).lower() == "open"
        )
        put_open = (
            str(getattr(trade_state.put_leg, "status", "open")).lower() == "open"
        )
        # Already one-legged in memory (e.g. after partial adjustment) —
        # do NOT emergency-close; keep monitoring remaining leg.
        if not (call_open and put_open):
            return True

        if trade_state.trade_id in self._integrity_in_progress:
            return True
        if self.delta_client is None:
            return True

        # DB may already show one-legged (partial adjust) while memory stale
        with self.db_factory() as db:
            open_n = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == trade_state.trade_id,
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                )
                .count()
            )
        if open_n < 2:
            logger.critical(
                "Trade %s integrity: DB has %s open leg(s) — syncing memory, "
                "skipping emergency close (likely partial adjustment)",
                trade_state.trade_id,
                open_n,
            )
            self._reload_legs_after_partial(trade_state)
            await self._push_error(
                trade_state.trade_id,
                (
                    "One-legged position after partial adjustment. "
                    "Close remaining leg manually — auto emergency close disabled."
                ),
                requires_manual_action=True,
            )
            return True

        positions = await self._fetch_positions_safe()
        if positions is None:
            return True

        status = self._check_partial_position(trade_state, positions)
        log_and_buffer(
            "POSITION_CHECK",
            trade_state.trade_id,
            {"status": status, "source": source},
        )
        if status == "both_open":
            return True

        self._integrity_in_progress.add(trade_state.trade_id)
        try:
            if status == "none":
                await self._handle_manual_close(trade_state)
                return False
            if status == "call_only":
                log_and_buffer(
                    "NAKED_POSITION",
                    trade_state.trade_id,
                    {"missing": "put", "remaining": "call", "source": source},
                )
                logger.critical(
                    "Trade %s: PUT position MISSING! Closing CALL.",
                    trade_state.trade_id,
                )
                await self._emergency_close_remaining_leg(trade_state, "call")
                return False
            if status == "put_only":
                log_and_buffer(
                    "NAKED_POSITION",
                    trade_state.trade_id,
                    {"missing": "call", "remaining": "put", "source": source},
                )
                logger.critical(
                    "Trade %s: CALL position MISSING! Closing PUT.",
                    trade_state.trade_id,
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
            "Trade %s manually closed on Delta — cancelling SL orders "
            "and marking trade closed",
            trade_id,
        )
        log_and_buffer(
            "MANUAL_EXCHANGE_CLOSE",
            trade_id,
            {"action": "cancel_sl_and_close"},
        )

        from backend.core.delta_sl import cancel_leg_sl_order
        from backend.engine.trade_reconcile import book_leg_close

        assert self.delta_client is not None
        now_utc = datetime.now(timezone.utc)

        with self.db_factory() as db:
            call_leg = (
                db.query(Leg).filter(Leg.id == trade_state.call_leg.id).first()
            ) or trade_state.call_leg
            put_leg = (
                db.query(Leg).filter(Leg.id == trade_state.put_leg.id).first()
            ) or trade_state.put_leg
            trade = db.query(Trade).filter(Trade.id == trade_id).first()

            for leg in (call_leg, put_leg):
                await cancel_leg_sl_order(
                    self.delta_client, leg, clear_fields=True
                )
                if (
                    trade is not None
                    and str(getattr(leg, "status", "")).lower() == "open"
                ):
                    exit_px = float(
                        getattr(leg, "exit_premium", None)
                        or getattr(leg, "initial_premium", 0)
                        or 0
                    )
                    book_leg_close(
                        leg=leg,
                        trade=trade,
                        exit_premium=exit_px,
                        exit_time=now_utc,
                    )

            if trade is not None:
                trade.status = TradeStatus.CLOSED.value
                trade.exit_reason = ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value
                trade.exit_time = get_ist_now()
                if trade.realized_pnl is None:
                    trade.realized_pnl = float(
                        getattr(trade_state, "last_delta_mtm", 0) or 0
                    )
            try:
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Could not persist manual exchange close trade=%s: %s",
                    trade_id,
                    exc,
                )
                db.rollback()

        self.position_tracker.mark_closed(trade_id)
        await ws_manager.broadcast(
            {
                "type": "TRADE_CLOSED",
                "trade_id": trade_id,
                "reason": ExitReason.MANUAL_CLOSE_ON_EXCHANGE.value,
                "message": (
                    "Trade was manually closed on Delta Exchange. "
                    "SL orders cancelled."
                ),
            }
        )
        self._maybe_schedule_auto_reentry(
            str(getattr(trade_state.trade, "underlying", "") or "")
        )

    async def _emergency_close_remaining_leg(
        self, trade_state: TradeState, leg_to_close: str
    ) -> None:
        """Close remaining open leg after the other vanished on Delta."""
        trade_id = trade_state.trade_id
        remaining = str(leg_to_close).lower().strip()
        missing = "put" if remaining == "call" else "call"
        logger.critical(
            "EMERGENCY: Closing %s leg due to naked position risk (trade %s)",
            remaining,
            trade_id,
        )
        log_and_buffer(
            "EMERGENCY_CLOSE",
            trade_id,
            {"closing": remaining, "missing": missing},
        )

        if self.delta_client is None:
            self._refresh_delta_client()
        assert self.delta_client is not None

        from backend.core.delta_sl import cancel_leg_sl_order
        from backend.engine.trade_reconcile import book_leg_close

        now_utc = datetime.now(timezone.utc)
        close_ok = False
        filled = 0.0

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

            if missing_leg is not None and trade is not None:
                await cancel_leg_sl_order(
                    self.delta_client, missing_leg, clear_fields=True
                )
                exit_px = float(missing_leg.initial_premium or 0)
                try:
                    mark = float(
                        await self.delta_client.get_mark_price(
                            str(missing_leg.symbol)
                        )
                    )
                    if mark > 0:
                        exit_px = mark
                except Exception:
                    pass
                book_leg_close(
                    leg=missing_leg,
                    trade=trade,
                    exit_premium=exit_px,
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
                            exit_premium=filled,
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
                    logger.critical("Emergency close FAILED: %s", result.error)
                    log_and_buffer(
                        "EXIT_FAIL",
                        trade_id,
                        {
                            "reason": "EMERGENCY_CLOSE",
                            "error": result.error,
                        },
                    )

            if trade is not None:
                trade.status = TradeStatus.CLOSED.value
                trade.exit_reason = (
                    ExitReason.SL_TRIGGERED_EMERGENCY_CLOSE.value
                )
                trade.exit_time = get_ist_now()
            try:
                db.commit()
            except Exception as exc:
                logger.warning(
                    "Could not persist emergency close trade=%s: %s",
                    trade_id,
                    exc,
                )
                db.rollback()

        self.position_tracker.mark_closed(trade_id)
        await ws_manager.broadcast(
            {
                "type": "ERROR",
                "trade_id": trade_id,
                "message": (
                    f"⚠️ Delta SL triggered on one leg! "
                    f"Emergency closed {remaining} to prevent naked position. "
                    f"Trade is now fully closed."
                    + ("" if close_ok else " (close may need manual check)")
                ),
                "requires_manual_action": not close_ok,
            }
        )
        await ws_manager.broadcast(
            {
                "type": "TRADE_CLOSED",
                "trade_id": trade_id,
                "reason": ExitReason.SL_TRIGGERED_EMERGENCY_CLOSE.value,
                "final_pnl": float(
                    getattr(trade_state, "last_delta_mtm", 0) or 0
                ),
            }
        )
        self._maybe_schedule_auto_reentry(
            str(getattr(trade_state.trade, "underlying", "") or "")
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

        # Position integrity: cancel orphan SLs / prevent naked after Delta SL
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
            },
        )

        # Step 1–2: Delta UPL @offer (never API unrealized_pnl — that field is wrong)
        call_mtm = 0.0
        put_mtm = 0.0
        delta_upnl = 0.0
        mtm_available = False
        realized = float(getattr(trade, "realized_pnl", None) or 0.0)
        call_offer = call_premium
        put_offer = put_premium
        try:
            pids: list[int] = []
            if call_open and int(call_leg.product_id) > 0:
                pids.append(int(call_leg.product_id))
            if put_open and int(put_leg.product_id) > 0:
                pids.append(int(put_leg.product_id))
            if pids:
                upnl_data = await self.delta_client.get_positions_upnl(pids)
                call_pid = int(call_leg.product_id)
                put_pid = int(put_leg.product_id)
                call_row = upnl_data.get(call_pid) or {}
                put_row = upnl_data.get(put_pid) or {}
                if call_open and call_row:
                    call_mtm = float(call_row.get("upnl") or 0.0)
                    if float(call_row.get("best_offer") or 0) > 0:
                        call_offer = float(call_row["best_offer"])
                        call_premium = call_offer
                if put_open and put_row:
                    put_mtm = float(put_row.get("upnl") or 0.0)
                    if float(put_row.get("best_offer") or 0) > 0:
                        put_offer = float(put_row["best_offer"])
                        put_premium = put_offer
                if (call_open and call_pid in upnl_data) or (
                    put_open and put_pid in upnl_data
                ):
                    delta_upnl = call_mtm + put_mtm
                    self.position_tracker.update_delta_mtm(trade_id, delta_upnl)
                    # Keep live cache on Best Offer (not mark)
                    if call_open:
                        self._live_prices[str(call_leg.symbol)] = call_premium
                    if put_open:
                        self._live_prices[str(put_leg.symbol)] = put_premium
                    mtm_available = True
                    logger.info(
                        "Trade %s P&L: call_upnl=%.4f put_upnl=%.4f "
                        "delta_upnl=%.4f realized=%.4f total=%.4f "
                        "call_offer=%.2f put_offer=%.2f target=%s sl=%s",
                        trade_id,
                        call_mtm,
                        put_mtm,
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

        # Fallback unrealized = UPL @ our Best Offer premiums
        if not mtm_available:
            if call_open:
                call_mtm = compute_signed_upnl(
                    float(call_leg.initial_premium),
                    call_premium,
                    size=-abs(int(call_leg.quantity)),
                    contract_value=OPTIONS_CONTRACT_VALUE,
                )
            if put_open:
                put_mtm = compute_signed_upnl(
                    float(put_leg.initial_premium),
                    put_premium,
                    size=-abs(int(put_leg.quantity)),
                    contract_value=OPTIONS_CONTRACT_VALUE,
                )
            delta_upnl = call_mtm + put_mtm

        total_pnl = realized + delta_upnl
        target = float(getattr(trade, "profit_target_usd", 0) or 0)
        stoploss = float(getattr(trade, "stoploss_usd", 0) or 0)
        pnl_pct = (total_pnl / target * 100.0) if target else 0.0
        slip_pct = float(getattr(trade, "slippage_pct", None) or 2.0)

        # Net MTM for exit decisions (same as frontend display)
        from backend.core.fees import (
            basket_fees_paid_from_legs,
            compute_net_mtm,
            estimate_option_trading_fee,
        )
        from backend.models import Leg as LegModel

        fees_paid = 0.0
        est_exit = 0.0
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
                offer = (
                    float(call_premium)
                    if str(leg.leg_type).lower() == "call"
                    else float(put_premium)
                )
                if offer > 0 and btc > 0:
                    est_exit += estimate_option_trading_fee(
                        option_price=offer,
                        quantity_lots=int(leg.quantity or 0),
                        btc_index_price=btc,
                    )

        slip_fields = compute_net_mtm(
            gross_mtm=total_pnl,
            fees_paid=fees_paid,
            est_exit_fees=est_exit,
            slippage_pct=slip_pct,
        )
        net_mtm_val = float(slip_fields["net_mtm"])
        slippage_amount = float(slip_fields["slippage_amount"])

        log_and_buffer(
            "PNL_CHECK",
            trade_id,
            {
                "realized_pnl": round(realized, 4),
                "delta_upnl": round(delta_upnl, 4),
                "call_upnl": round(call_mtm, 4),
                "put_upnl": round(put_mtm, 4),
                "gross_mtm": round(total_pnl, 4),
                "fees_paid": round(fees_paid, 4),
                "est_exit_fees": round(est_exit, 4),
                "slippage_pct": slip_pct,
                "slippage_amount": round(slippage_amount, 4),
                "net_mtm": round(net_mtm_val, 4),
                "total_pnl": round(total_pnl, 4),
                "profit_target": target,
                "stoploss": stoploss,
                "pnl_pct": round(pnl_pct, 1),
                "will_exit_profit": net_mtm_val >= target if target else False,
                "will_exit_stoploss": (
                    net_mtm_val <= -stoploss if stoploss else False
                ),
                "mtm_source": "delta_position" if mtm_available else "computed_fallback",
                "contract_value": OPTIONS_CONTRACT_VALUE,
            },
        )

        with self.db_factory() as db:
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

        call_repl, put_repl = await self._estimate_replacements(
            trade_state, call_premium, put_premium
        )

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
        trade_id = trade_state.trade_id
        trade = trade_state.trade
        pnl_now = float(
            total_pnl
            if total_pnl is not None
            else (trade_state.last_delta_mtm or trade_state.last_pnl)
        )
        gross = float(gross_mtm if gross_mtm is not None else pnl_now)
        fees = float(fees_paid or 0.0) + float(est_exit_fees or 0.0)
        slip = float(slippage_amount or 0.0)
        net = float(net_mtm if net_mtm is not None else pnl_now)

        log_and_buffer(
            "EXIT_TRIGGERED",
            trade_id,
            {
                "reason": reason,
                "gross_mtm": round(gross, 4),
                "fees_paid": round(float(fees_paid or 0), 4),
                "est_exit_fees": round(float(est_exit_fees or 0), 4),
                "slippage_amount": round(slip, 4),
                "net_mtm": round(net, 4),
                "total_pnl": round(pnl_now, 2),
                "profit_target": float(getattr(trade, "profit_target_usd", 0) or 0),
                "stoploss": float(getattr(trade, "stoploss_usd", 0) or 0),
            },
        )
        logger.info(
            "EXIT TRADE %s: reason=%s | gross_mtm=%.2f | fees=%.2f | "
            "slippage=%.2f | net_mtm=%.2f | target=%s | sl=%s",
            trade_id,
            reason,
            gross,
            fees,
            slip,
            net,
            getattr(trade, "profit_target_usd", 0),
            getattr(trade, "stoploss_usd", 0),
        )
        if reason == ExitReason.DECISION_PROFIT_AT_TRIGGER.value:
            logger.info(
                "Closing basket: trigger fired but profitable — "
                "booking net_mtm=%.2f (trade %s)",
                net,
                trade_id,
            )
        assert self.delta_client is not None

        # Mirror exit to slave accounts (before master legs close — need product_ids)
        try:
            import backend.engine.mirror_engine as mirror_module

            if mirror_module.mirror_engine is not None:
                asyncio.create_task(
                    mirror_module.mirror_engine.mirror_exit(
                        master_trade_id=trade_id,
                        call_product_id=int(trade_state.call_leg.product_id),
                        put_product_id=int(trade_state.put_leg.product_id),
                        reason=reason,
                    )
                )
                logger.info("Mirror exit queued for trade %s", trade_id)
        except Exception as exc:
            logger.warning("Mirror exit queue failed: %s", exc)

        call_close = await self.order_executor.close_leg(
            trade_state.call_leg, self.delta_client
        )
        put_close = await self.order_executor.close_leg(
            trade_state.put_leg, self.delta_client
        )

        if not call_close.success or not put_close.success:
            msg = (
                f"Exit order failure trade={trade_id} "
                f"call_ok={call_close.success} put_ok={put_close.success} "
                f"call_err={call_close.error} put_err={put_close.error}"
            )
            logger.critical(msg)
            log_and_buffer(
                "EXIT_FAIL",
                trade_id,
                {
                    "reason": reason,
                    "call_ok": call_close.success,
                    "put_ok": put_close.success,
                    "error": msg,
                },
            )
            await self._push_error(trade_id, msg, requires_manual_action=True)
            return

        status = self._status_for_reason(reason)
        now_utc = datetime.now(timezone.utc)
        call_fill = float(call_close.filled_price or 0.0)
        put_fill = float(put_close.filled_price or 0.0)
        final_pnl = pnl_now

        with self.db_factory() as db:
            trade_row = db.query(Trade).filter(Trade.id == trade_id).first()
            call_leg = db.query(Leg).filter(Leg.id == trade_state.call_leg.id).first()
            put_leg = db.query(Leg).filter(Leg.id == trade_state.put_leg.id).first()
            if trade_row is None or call_leg is None or put_leg is None:
                logger.error("Exit DB rows missing for trade %s", trade_id)
                log_and_buffer(
                    "EXIT_FAIL",
                    trade_id,
                    {"reason": reason, "error": "DB rows missing"},
                )
                return

            from backend.engine.trade_reconcile import book_leg_close

            # Only book realized for legs that were still open at exit
            if str(call_leg.status).lower() == "open":
                book_leg_close(
                    leg=call_leg,
                    trade=trade_row,
                    exit_premium=call_fill,
                    exit_time=now_utc,
                    exit_fee_usd=(
                        float(call_close.commission)
                        if call_close.commission is not None
                        else None
                    ),
                    exit_order_id=(
                        str(call_close.order_id)
                        if call_close.order_id is not None
                        else None
                    ),
                )
            if str(put_leg.status).lower() == "open":
                book_leg_close(
                    leg=put_leg,
                    trade=trade_row,
                    exit_premium=put_fill,
                    exit_time=now_utc,
                    exit_fee_usd=(
                        float(put_close.commission)
                        if put_close.commission is not None
                        else None
                    ),
                    exit_order_id=(
                        str(put_close.order_id)
                        if put_close.order_id is not None
                        else None
                    ),
                )

            trade_row.status = status
            trade_row.exit_time = get_ist_now()
            # Prefer booked realized sum; fall back to monitor total
            trade_row.realized_pnl = float(trade_row.realized_pnl or final_pnl)
            trade_row.exit_reason = reason

            db.commit()

        self.position_tracker.mark_closed(trade_id)
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
                "timestamp": get_ist_now().isoformat(),
            }
        )
        self._maybe_schedule_auto_reentry(
            str(getattr(trade, "underlying", "") or "")
        )

    async def _adjust_trade(
        self, trade_state: TradeState, triggered_leg_type: str
    ) -> None:
        trade_id = trade_state.trade_id
        triggered = triggered_leg_type.lower()
        old_leg = (
            trade_state.call_leg if triggered == "call" else trade_state.put_leg
        )
        other_leg = (
            trade_state.put_leg if triggered == "call" else trade_state.call_leg
        )
        old_strike = float(old_leg.strike)
        old_premium = float(old_leg.initial_premium)
        other_prem = float(
            getattr(trade_state, "last_put_premium", 0)
            if triggered == "call"
            else getattr(trade_state, "last_call_premium", 0)
        ) or float(other_leg.initial_premium)

        logger.info("Adjusting trade %s, leg: %s", trade_id, triggered_leg_type)
        log_and_buffer(
            "ADJUSTMENT_START",
            trade_id,
            {
                "triggered_leg": triggered,
                "old_strike": old_strike,
                "old_premium": round(old_premium, 2),
                "target_new_premium": round(other_prem, 2),
            },
        )
        self.position_tracker.set_adjusting(trade_id, True)
        try:
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
                await self._push_adjustment(trade_state, triggered_leg_type, result)
            else:
                err = result.error_message or "Adjustment failed"
                is_hold = "ADJUSTMENT_HOLD" in err and "no other" in err.lower()
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
                    # CRITICAL: do NOT mark trade closed / remove from tracker /
                    # schedule auto re-entry. Sync memory so integrity check
                    # sees one closed leg and will NOT emergency-close remaining.
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
                        "Trade %s PARTIAL adjustment — remaining leg stays "
                        "ACTIVE in tracker. Manual close required. %s",
                        trade_id,
                        err,
                    )
                    await self._push_error(
                        trade_id,
                        (
                            f"PARTIAL ADJUSTMENT: {err} "
                            "Trade kept open — close remaining leg manually."
                        ),
                        requires_manual_action=True,
                    )
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
            # ALWAYS release lock — prevents permanent skip of this trade
            self.position_tracker.set_adjusting(trade_id, False)

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
        by the next on_tick() trigger check.
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
            trade_state.call_leg = call_leg
            trade_state.put_leg = put_leg
            if trade_row is not None:
                # Keep in-memory trade.realized_pnl in sync for next on_tick
                trade_state.trade.realized_pnl = float(trade_row.realized_pnl or 0.0)
            logger.info(
                "Legs reloaded after adjustment: "
                "call entry=%s baseline=%s put entry=%s baseline=%s "
                "trade=%s realized_pnl=%s",
                call_leg.initial_premium,
                getattr(call_leg, "trigger_baseline_premium", None)
                or getattr(call_leg, "trigger_premium", None),
                put_leg.initial_premium,
                getattr(put_leg, "trigger_baseline_premium", None)
                or getattr(put_leg, "trigger_premium", None),
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
        # Fallback estimate if order not stored yet
        if call_sl_px is None or float(call_sl_px or 0) <= 0:
            call_sl_px = round(call_base * (uni_sl / 100.0), 4) if call_base > 0 else None
        if put_sl_px is None or float(put_sl_px or 0) <= 0:
            put_sl_px = round(put_base * (uni_sl / 100.0), 4) if put_base > 0 else None

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
        )

        # Fees from DB legs + slippage for Net MTM on TRADE_UPDATE
        from backend.core.fees import (
            basket_fees_paid_from_legs,
            compute_net_mtm,
            estimate_option_trading_fee,
        )
        from backend.models import Leg as LegModel

        fees_paid = 0.0
        est_exit = 0.0
        with self.db_factory() as db:
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
                offer = (
                    float(call_prem)
                    if str(leg.leg_type).lower() == "call"
                    else float(put_prem)
                )
                if offer > 0 and btc > 0:
                    est_exit += estimate_option_trading_fee(
                        option_price=offer,
                        quantity_lots=int(leg.quantity or 0),
                        btc_index_price=btc,
                    )

        slip_fields = compute_net_mtm(
            gross_mtm=display_total,
            fees_paid=fees_paid,
            est_exit_fees=est_exit,
            slippage_pct=getattr(trade_state.trade, "slippage_pct", None),
        )
        slip_pct = float(slip_fields["slippage_pct"])
        slip_amt = float(slip_fields["slippage_amount"])
        net_mtm_out = float(slip_fields["net_mtm"])
        total_deductions = float(slip_fields["total_deductions"])

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
        }
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
    ) -> None:
        await ws_manager.broadcast(
            {
                "type": "ERROR",
                "trade_id": trade_id,
                "message": message,
                "requires_manual_action": requires_manual_action,
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
