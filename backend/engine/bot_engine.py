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

    async def start(self) -> None:
        self.is_running = True
        logger.info("Bot engine starting...")
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
                    if ask <= 0:
                        return
                    prev = self._live_prices.get(symbol)
                    self._live_prices[symbol] = ask
                    if prev is not None and abs(prev - ask) < 1e-9:
                        return
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
                    closed_ids = await reconcile_open_legs_with_delta(
                        db=db,
                        client=self.delta_client,
                        position_tracker=self.position_tracker,
                    )
                for tid in closed_ids:
                    await ws_manager.broadcast(
                        {
                            "type": "TRADE_CLOSED",
                            "trade_id": tid,
                            "reason": ExitReason.MANUAL_LEG_CLOSE.value,
                            "message": "Basket closed — Delta size flat",
                        }
                    )
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

        for trade_state in self.position_tracker.get_all_active():
            if trade_state.is_adjusting:
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

        log_and_buffer(
            "PNL_CHECK",
            trade_id,
            {
                "realized_pnl": round(realized, 4),
                "delta_upnl": round(delta_upnl, 4),
                "call_upnl": round(call_mtm, 4),
                "put_upnl": round(put_mtm, 4),
                "total_pnl": round(total_pnl, 4),
                "profit_target": target,
                "stoploss": stoploss,
                "pnl_pct": round(pnl_pct, 1),
                "will_exit_profit": total_pnl >= target if target else False,
                "will_exit_stoploss": total_pnl <= -stoploss if stoploss else False,
                "mtm_source": "delta_position" if mtm_available else "computed_fallback",
                "contract_value": OPTIONS_CONTRACT_VALUE,
            },
        )

        with self.db_factory() as db:
            trigger_pct = float(self.strategy.get_current_trigger_pct(trade, db))
            action = await self.strategy.on_tick(
                trade,
                call_leg,
                put_leg,
                call_premium,
                put_premium,
                db,
                realized_pnl=realized,
                delta_mtm=delta_upnl,  # unrealized only (Delta or calculated)
            )
            trigger_for_plan = float(getattr(action, "trigger_pct_used", 0) or 0)
            if trigger_for_plan <= 0:
                trigger_for_plan = trigger_pct

        def _trig_base(leg: Any) -> float:
            for attr in ("trigger_baseline_premium", "trigger_premium"):
                val = getattr(leg, attr, None)
                if val is not None and float(val) > 0:
                    return float(val)
            return float(getattr(leg, "initial_premium", 0) or 0)

        call_trigger = _trig_base(call_leg) * (trigger_for_plan / 100.0)
        put_trigger = _trig_base(put_leg) * (trigger_for_plan / 100.0)
        call_pct = (call_premium / call_trigger * 100.0) if call_trigger > 0 else 0.0
        put_pct = (put_premium / put_trigger * 100.0) if put_trigger > 0 else 0.0
        if action.should_exit:
            action_label = action.exit_reason or "EXIT"
        elif action.should_adjust and action.adjust_leg:
            action_label = f"ADJUST_{action.adjust_leg}"
        else:
            action_label = "HOLD"

        log_and_buffer(
            "TRIGGER_CHECK",
            trade_id,
            {
                "trigger_pct": trigger_for_plan,
                "call_trigger_at": round(call_trigger, 2),
                "put_trigger_at": round(put_trigger, 2),
                "call_current": round(call_premium, 2),
                "call_pct_to_trigger": round(call_pct, 1),
                "put_current": round(put_premium, 2),
                "put_pct_to_trigger": round(put_pct, 1),
                "action": action_label,
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
            await self._exit_trade(
                trade_state,
                action.exit_reason or "UNKNOWN",
                total_pnl=total_pnl,
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
            )

    async def _exit_trade(
        self,
        trade_state: TradeState,
        reason: str,
        total_pnl: float | None = None,
    ) -> None:
        trade_id = trade_state.trade_id
        trade = trade_state.trade
        pnl_now = float(
            total_pnl
            if total_pnl is not None
            else (trade_state.last_delta_mtm or trade_state.last_pnl)
        )
        log_and_buffer(
            "EXIT_TRIGGERED",
            trade_id,
            {
                "reason": reason,
                "total_pnl": round(pnl_now, 2),
                "profit_target": float(getattr(trade, "profit_target_usd", 0) or 0),
                "stoploss": float(getattr(trade, "stoploss_usd", 0) or 0),
            },
        )
        logger.info("Exiting trade %s, reason: %s", trade_id, reason)
        assert self.delta_client is not None

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
                else:
                    log_and_buffer(
                        "ADJUSTMENT_FAIL",
                        trade_id,
                        {
                            "leg": triggered,
                            "error": err,
                            "is_partial": bool(result.is_partial),
                        },
                    )
                    await self._push_error(
                        trade_id,
                        err,
                        requires_manual_action=bool(result.is_partial),
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

        CRITICAL: Must re-query BOTH legs so updated initial_premium baselines
        (triggered = new fill, untouched = offer at adjustment) are used
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
                    "After adjustment, open legs missing for trade %s",
                    trade_state.trade_id,
                )
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
    ) -> dict[str, Any]:
        """Shared monitoring-plan fields for WS and /api/trade/active.

        Entry display = initial_premium (never changes for a leg row).
        Trigger calc = trigger_baseline_premium (resets each adjustment).
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
        pct = float(trigger_pct) if trigger_pct > 0 else 150.0
        call_trigger = call_base * (pct / 100.0)
        put_trigger = put_base * (pct / 100.0)
        call_pct_to = (call_prem / call_trigger * 100.0) if call_trigger > 0 else 0.0
        put_pct_to = (put_prem / put_trigger * 100.0) if put_trigger > 0 else 0.0

        cached = self._replacement_estimates.get(trade_state.trade_id) or {}
        if call_replacement is None:
            call_replacement = cached.get("estimated_call_replacement")
        if put_replacement is None:
            put_replacement = cached.get("estimated_put_replacement")

        return {
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
            "current_trigger_pct": pct,
            "call_trigger_price": round(call_trigger, 4),
            "put_trigger_price": round(put_trigger, 4),
            "call_pct_to_trigger": round(call_pct_to, 2),
            "put_pct_to_trigger": round(put_pct_to, 2),
            "call_distance_to_trigger": round(call_trigger - call_prem, 4),
            "put_distance_to_trigger": round(put_trigger - put_prem, 4),
            "estimated_call_replacement": call_replacement,
            "estimated_put_replacement": put_replacement,
            "trigger_mode": str(getattr(trade_state.trade, "trigger_mode", "slab")),
        }

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
        )

        await ws_manager.broadcast(
            {
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
                "net_mtm": display_total,  # fees applied on /active; tick path must not recompute
                "underlying_price": float(self._btc_spot or 0) or None,
                "last_mtm_update": get_ist_now().strftime("%H:%M:%S IST"),
                "pnl_pct_of_target": pnl_pct_of_target,
                "profit_target_usd": target,
                "stoploss_usd": stoploss,
                "hours_to_expiry": get_hours_to_expiry(trade_state.trade.expiry_date),
                "status": "active",
                "is_settling": settling["is_settling"],
                "settling_ends_at": settling["settling_ends_at"],
                "settling_minutes_left": settling["settling_minutes_left"],
                **plan,
            }
        )

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
                trigger_pct = float(
                    self.strategy.get_current_trigger_pct(trade_state.trade, db)
                )
            except Exception:
                trigger_pct = 150.0

        plan = self.build_bot_plan_fields(
            trade_state, call_prem, put_prem, trigger_pct
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
