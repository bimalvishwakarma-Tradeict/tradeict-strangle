# auto_trade_engine.py — Auto re-entry of ATM straddles after exits

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import OPTIONS_CONTRACT_VALUE, TradeStatus
from backend.core.time_utils import (
    get_expiry_date_for_dte,
    get_hours_to_expiry,
    get_ist_now,
    settling_ends_at_after_place,
)
from backend.core.ws_manager import ws_manager
from backend.engine.trade_reconcile import next_basket_number

logger = logging.getLogger(__name__)

_AUTO_LOOP_SECONDS = 30
_RETRY_DELAY_SECONDS = 60


def _as_ist(dt: datetime | None) -> datetime | None:
    """Normalize DB datetime to IST-aware for comparisons."""
    if dt is None:
        return None
    from backend.config import IST

    if dt.tzinfo is None:
        return IST.localize(dt)
    return dt.astimezone(IST)


class AutoTradeEngine:
    """
    Monitors for empty baskets and automatically places new ATM straddles.

    Runs as a background loop alongside BotEngine. Re-entry is scheduled via
    schedule_reentry() after a monitored trade exits.
    """

    def __init__(
        self,
        db_factory: Callable[[], Any],
        delta_client: Any | None,
        position_tracker: Any,
        order_executor: Any,
    ) -> None:
        self.db_factory = db_factory
        self.delta_client = delta_client
        self.position_tracker = position_tracker
        self.order_executor = order_executor
        self.is_running = False
        self._bot_engine_ref: Any | None = None
        self._loop_task: asyncio.Task[None] | None = None

    def set_bot_engine(self, bot_engine: Any) -> None:
        self._bot_engine_ref = bot_engine

    def _resolve_delta_client(self) -> Any | None:
        if self.delta_client is not None:
            return self.delta_client
        if self._bot_engine_ref is not None:
            client = getattr(self._bot_engine_ref, "delta_client", None)
            if client is None and hasattr(self._bot_engine_ref, "_refresh_delta_client"):
                self._bot_engine_ref._refresh_delta_client()
                client = getattr(self._bot_engine_ref, "delta_client", None)
            self.delta_client = client
            return client
        return None

    async def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        logger.info("🔄 Auto trade engine started")
        self._loop_task = asyncio.create_task(self._loop(), name="auto-trade-loop")

    async def stop(self) -> None:
        self.is_running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        logger.info("Auto trade engine stopped")

    async def _loop(self) -> None:
        """Check every 30 seconds if auto re-entry is needed."""
        while self.is_running:
            try:
                await self._tick()
            except Exception as exc:
                logger.error("Auto trade loop error: %s", exc, exc_info=True)
            await asyncio.sleep(_AUTO_LOOP_SECONDS)

    async def _tick(self) -> None:
        from backend.core.bot_logger import log_and_buffer
        from backend.database import get_or_create_auto_settings
        from backend.models import Trade

        with self.db_factory() as db:
            settings = get_or_create_auto_settings(db)

            if not settings.is_enabled:
                return

            underlying = str(settings.underlying).upper()
            now = get_ist_now()

            # Guard 5: adjustment in progress (any trade)
            for state in self.position_tracker.get_all_active():
                if getattr(state, "is_adjusting", False):
                    logger.info(
                        "Auto trade BLOCKED: Trade %s has adjustment in progress",
                        state.trade_id,
                    )
                    log_and_buffer(
                        "ENTRY_GUARD_BLOCK",
                        int(state.trade_id),
                        {
                            "source": "auto",
                            "guard": "adjusting",
                            "underlying": underlying,
                        },
                    )
                    return

            # Guard 4: settling period on active trade for this underlying
            active_for_underlying = (
                db.query(Trade)
                .filter(
                    Trade.underlying == underlying,
                    Trade.status == TradeStatus.ACTIVE.value,
                )
                .all()
            )
            for candidate in active_for_underlying:
                starts = _as_ist(getattr(candidate, "monitoring_starts_at", None))
                if starts is not None and now < starts:
                    logger.info(
                        "Auto trade BLOCKED: Trade %s still in settling period "
                        "until %s",
                        candidate.id,
                        starts,
                    )
                    log_and_buffer(
                        "ENTRY_GUARD_BLOCK",
                        int(candidate.id),
                        {
                            "source": "auto",
                            "guard": "settling",
                            "underlying": underlying,
                            "monitoring_starts_at": starts.isoformat(),
                        },
                    )
                    return

            # Guard 1 (tick-level): any active master trade for underlying
            if active_for_underlying:
                return

            next_entry = _as_ist(settings.next_entry_time)
            if next_entry is not None and now < next_entry:
                remaining = int((next_entry - now).total_seconds())
                logger.info(
                    "Auto trade waiting %ss (%s %sDTE)",
                    remaining,
                    settings.underlying,
                    settings.expiry_dte,
                )
                await ws_manager.broadcast(
                    {
                        "type": "AUTO_TRADE_WAITING",
                        "underlying": settings.underlying,
                        "seconds_remaining": max(0, remaining),
                        "next_entry_time": next_entry.isoformat(),
                    }
                )
                return

            logger.info(
                "AUTO TRADE triggered: %s %sDTE qty=%s",
                settings.underlying,
                settings.expiry_dte,
                settings.quantity,
            )
            await self._place_trade(settings, db)

    async def _place_trade(self, settings: Any, db: Any) -> None:
        from backend.models import Account, Leg, Setting, Trade

        client = self._resolve_delta_client()
        if client is None:
            await self._record_failure(
                settings,
                db,
                "No Delta client available — connect API keys in Settings",
            )
            return

        account = (
            db.query(Account)
            .filter(Account.is_active.is_(True))
            .order_by(Account.id.asc())
            .first()
        )
        if account is None:
            await self._record_failure(settings, db, "No active account in DB")
            return

        from backend.core.bot_logger import log_and_buffer

        underlying = str(settings.underlying).upper()

        # --- STRICT triple guard (auto blocks on Delta positions) ---
        # Guard 1: DB
        active_db = (
            db.query(Trade)
            .filter(
                Trade.underlying == underlying,
                Trade.status == TradeStatus.ACTIVE.value,
            )
            .count()
        )
        if active_db > 0:
            logger.warning(
                "Auto trade BLOCKED: %s active trade(s) in DB for %s",
                active_db,
                underlying,
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "db",
                    "underlying": underlying,
                    "count": active_db,
                },
            )
            return

        # Guard 2: Tracker (any active basket)
        active_tracker = len(self.position_tracker.get_all_active())
        if active_tracker > 0:
            logger.warning(
                "Auto trade BLOCKED: %s active trade(s) in tracker",
                active_tracker,
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "tracker",
                    "count": active_tracker,
                },
            )
            return

        # Guard 3: Delta positions (STRICT)
        delta_option_positions = await client.get_option_positions()
        underlying_positions = [
            p
            for p in delta_option_positions
            if underlying
            in str(p.get("product_symbol") or p.get("symbol") or "").upper()
        ]
        if underlying_positions:
            symbols = [
                p.get("product_symbol") or p.get("symbol")
                for p in underlying_positions
            ]
            logger.warning(
                "Auto trade BLOCKED: Delta has %s open %s option positions: %s. "
                "Will retry after positions are cleared.",
                len(underlying_positions),
                underlying,
                symbols,
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "delta",
                    "underlying": underlying,
                    "symbols": symbols,
                },
            )
            settings.next_entry_time = get_ist_now() + timedelta(minutes=2)
            settings.last_error = f"Delta has open positions: {symbols}"[:500]
            db.commit()
            return

        # Guard 4 (place-level): settling active trade
        now_check = get_ist_now()
        for candidate in (
            db.query(Trade)
            .filter(
                Trade.underlying == underlying,
                Trade.status == TradeStatus.ACTIVE.value,
            )
            .all()
        ):
            starts = _as_ist(getattr(candidate, "monitoring_starts_at", None))
            if starts is not None and now_check < starts:
                logger.info(
                    "Auto trade BLOCKED: Trade %s still in settling until %s",
                    candidate.id,
                    starts,
                )
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    int(candidate.id),
                    {"source": "auto", "guard": "settling", "underlying": underlying},
                )
                return

        # Guard 5 (place-level): adjustment lock
        for state in self.position_tracker.get_all_active():
            if getattr(state, "is_adjusting", False):
                logger.info(
                    "Auto trade BLOCKED: Trade %s has adjustment in progress",
                    state.trade_id,
                )
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    int(state.trade_id),
                    {"source": "auto", "guard": "adjusting"},
                )
                return

        # Guard: expiry not too close (before any order)
        expiry_date = get_expiry_date_for_dte(int(settings.expiry_dte))
        hours_to_expiry = float(get_hours_to_expiry(expiry_date))
        if hours_to_expiry < 1.0:
            logger.warning(
                "Auto trade BLOCKED: Expiry %s is only %.1fh away. Too close.",
                expiry_date,
                hours_to_expiry,
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "expiry_too_close",
                    "expiry": str(expiry_date),
                    "hours_to_expiry": round(hours_to_expiry, 1),
                },
            )
            settings.next_entry_time = get_ist_now() + timedelta(hours=1)
            settings.last_error = f"Expiry too close: {hours_to_expiry:.1f}h"[:500]
            db.commit()
            return

        logger.info(
            "Expiry validated: %s (%.1fh to expiry)",
            expiry_date,
            hours_to_expiry,
        )
        logger.info("All entry guards passed — proceeding with auto trade entry")
        log_and_buffer(
            "ENTRY_GUARD_PASS",
            0,
            {
                "underlying": settings.underlying,
                "expiry": str(expiry_date),
                "hours_to_expiry": round(hours_to_expiry, 1),
                "quantity": int(settings.quantity or 0),
                "trigger_mode": str(settings.trigger_mode or "slab"),
            },
        )

        try:
            expiry_str = expiry_date.isoformat()
            logger.info("Auto trade: expiry=%s", expiry_str)

            trade_type = str(
                getattr(settings, "trade_type", None) or "straddle"
            ).lower().strip()
            if trade_type == "strangle":
                target_prem = float(
                    getattr(settings, "target_premium_per_side", None) or 150.0
                )
                logger.info(
                    "Auto trade: STRANGLE mode target_premium=$%.2f",
                    target_prem,
                )
                straddle = await client.find_strangle_by_premium(
                    underlying=str(settings.underlying),
                    expiry_date=expiry_str,
                    target_premium=target_prem,
                )
            else:
                logger.info("Auto trade: STRADDLE mode (ATM)")
                straddle = await client.find_atm_straddle(
                    str(settings.underlying), expiry_str
                )
            logger.info(
                "%s: call_strike=%s put_strike=%s call=%.2f put=%.2f",
                trade_type.upper(),
                straddle.get("call_strike", straddle.get("strike")),
                straddle.get("put_strike", straddle.get("strike")),
                straddle["call_premium"],
                straddle["put_premium"],
            )

            qty = max(1, int(settings.quantity))
            call_mark = float(straddle["call_premium"])
            put_mark = float(straddle["put_premium"])
            tp_pct = float(settings.tp_pct or 50.0)
            sl_pct = float(settings.sl_pct or 100.0)
            universal_sl_pct = float(settings.universal_sl_pct or 200.0)
            # Bracket SL confirmed working on Delta Exchange India
            # Format: bracket_stop_loss_price + bracket_stop_loss_limit_price
            # Bracket auto-cancels when position is closed (any reason)
            # Use expected mark before fill (bracket must be set at order time).
            call_sl_trigger_price = round(
                call_mark * (universal_sl_pct / 100.0), 2
            )
            put_sl_trigger_price = round(
                put_mark * (universal_sl_pct / 100.0), 2
            )
            call_sl_limit = round(call_sl_trigger_price * 1.05, 2)
            put_sl_limit = round(put_sl_trigger_price * 1.05, 2)

            # --- Place CALL ---
            logger.info(
                "Placing CALL: %s qty=%s", straddle["call_symbol"], qty
            )
            call_result = await self.order_executor.sell_option(
                product_id=int(straddle["call_product_id"]),
                quantity=qty,
                delta_client=client,
                symbol_for_fallback=str(straddle["call_symbol"]),
                bracket_sl_price=(
                    call_sl_trigger_price if call_sl_trigger_price > 0 else None
                ),
                bracket_sl_limit=call_sl_limit if call_sl_trigger_price > 0 else None,
            )
            if not call_result.success:
                raise RuntimeError(
                    f"Call order failed: {call_result.error or 'unknown'}"
                )
            call_fill = float(call_result.filled_price or 0.0)
            if call_fill <= 0:
                call_fill = call_mark
                logger.warning(
                    "Call fill unavailable, using mark: %.4f", call_fill
                )
            call_order_id = (
                str(call_result.order_id)
                if call_result.order_id is not None
                else None
            )
            call_fee = (
                abs(float(call_result.commission))
                if call_result.commission is not None
                else None
            )
            logger.info(
                "Call filled @ %s order_id=%s", call_fill, call_order_id
            )

            # --- Place PUT ---
            logger.info("Placing PUT: %s qty=%s", straddle["put_symbol"], qty)
            put_result = await self.order_executor.sell_option(
                product_id=int(straddle["put_product_id"]),
                quantity=qty,
                delta_client=client,
                symbol_for_fallback=str(straddle["put_symbol"]),
                bracket_sl_price=(
                    put_sl_trigger_price if put_sl_trigger_price > 0 else None
                ),
                bracket_sl_limit=put_sl_limit if put_sl_trigger_price > 0 else None,
            )
            if not put_result.success:
                raise RuntimeError(
                    f"PARTIAL: Call placed @ {call_fill} "
                    f"(order {call_order_id}) but Put failed: "
                    f"{put_result.error or 'unknown'}"
                )
            put_fill = float(put_result.filled_price or 0.0)
            if put_fill <= 0:
                put_fill = put_mark
                logger.warning(
                    "Put fill unavailable, using mark: %.4f", put_fill
                )
            put_order_id = (
                str(put_result.order_id)
                if put_result.order_id is not None
                else None
            )
            put_fee = (
                abs(float(put_result.commission))
                if put_result.commission is not None
                else None
            )
            logger.info("Put filled @ %s order_id=%s", put_fill, put_order_id)

            # TP/SL locked to initial deployment premium (actual fills)
            # initial_max_profit never changes after trade entry
            # adjustments do NOT affect TP/SL
            initial_max_profit = round(
                (call_fill + put_fill) * qty * float(OPTIONS_CONTRACT_VALUE),
                6,
            )
            profit_target_usd = round(initial_max_profit * tp_pct / 100.0, 2)
            stoploss_usd = round(initial_max_profit * sl_pct / 100.0, 2)

            now_utc = datetime.now(timezone.utc)
            now_ist = get_ist_now()
            monitoring_starts = settling_ends_at_after_place(now_ist)
            basket_no = next_basket_number(db, int(account.id))

            trade = Trade(
                account_id=int(account.id),
                underlying=str(settings.underlying).upper(),
                expiry_date=expiry_date,
                status=TradeStatus.ACTIVE.value,
                entry_time=now_utc,
                total_premium_collected=(call_fill + put_fill) * qty,
                profit_target_usd=profit_target_usd,
                stoploss_usd=stoploss_usd,
                trigger_mode=str(settings.trigger_mode or "slab"),
                realized_pnl=0.0,
                monitoring_starts_at=monitoring_starts,
                initial_max_profit=initial_max_profit,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                universal_sl_pct=float(settings.universal_sl_pct or 200.0),
                slippage_pct=float(settings.slippage_pct or 2.0),
                basket_number=basket_no,
                notes="auto_trade",
            )
            db.add(trade)
            db.flush()

            from backend.core.bot_logger import log_tp_sl_locked

            log_tp_sl_locked(
                trade_id=int(trade.id),
                initial_max_profit=initial_max_profit,
                profit_target_usd=profit_target_usd,
                stoploss_usd=stoploss_usd,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
            )

            call_leg = Leg(
                trade_id=trade.id,
                leg_type="call",
                strike=float(straddle.get("call_strike", straddle["strike"])),
                symbol=str(straddle["call_symbol"]),
                product_id=int(straddle["call_product_id"]),
                initial_premium=call_fill,
                trigger_baseline_premium=call_fill,
                trigger_premium=call_fill,
                quantity=qty,
                entry_time=now_utc,
                status="open",
                is_bot_managed=True,
                delta_order_id=call_order_id,
                delta_at_entry=float(straddle.get("call_delta") or 0),
                entry_fee_usd=call_fee,
                sl_trigger_price=float(call_sl_trigger_price)
                if call_sl_trigger_price and call_sl_trigger_price > 0
                else None,
                delta_sl_order_id=None,  # bracket has no separate stop-order ID
            )
            put_leg = Leg(
                trade_id=trade.id,
                leg_type="put",
                strike=float(straddle.get("put_strike", straddle["strike"])),
                symbol=str(straddle["put_symbol"]),
                product_id=int(straddle["put_product_id"]),
                initial_premium=put_fill,
                trigger_baseline_premium=put_fill,
                trigger_premium=put_fill,
                quantity=qty,
                entry_time=now_utc,
                status="open",
                is_bot_managed=True,
                delta_order_id=put_order_id,
                delta_at_entry=float(straddle.get("put_delta") or 0),
                entry_fee_usd=put_fee,
                sl_trigger_price=float(put_sl_trigger_price)
                if put_sl_trigger_price and put_sl_trigger_price > 0
                else None,
                delta_sl_order_id=None,  # bracket has no separate stop-order ID
            )
            db.add(call_leg)
            db.add(put_leg)

            mode = str(settings.trigger_mode or "slab")
            slab_map: dict[str, Any] = {
                "trigger_mode": mode,
                "slab_24h": settings.slab_24h,
                "slab_12h": settings.slab_12h,
                "slab_6h": settings.slab_6h,
                "slab_lt6h": settings.slab_lt6h,
            }
            if settings.flat_trigger_pct is not None:
                slab_map["flat_trigger_pct"] = settings.flat_trigger_pct
            if mode == "premium":
                slab_map.update(
                    {
                        "premium_slab_300": settings.premium_slab_300,
                        "premium_slab_200": settings.premium_slab_200,
                        "premium_slab_100": settings.premium_slab_100,
                        "premium_slab_lt100": settings.premium_slab_lt100,
                    }
                )
            for key, value in slab_map.items():
                db.add(Setting(trade_id=trade.id, key=key, value=str(value)))

            db.commit()
            db.refresh(trade)
            db.refresh(call_leg)
            db.refresh(put_leg)

            # --- Post-placement verification ---
            saved_trade = (
                db.query(Trade)
                .filter(
                    Trade.id == trade.id,
                    Trade.status == TradeStatus.ACTIVE.value,
                )
                .first()
            )
            if saved_trade is None:
                logger.critical(
                    "Auto trade %s NOT found in DB after save! Critical bug.",
                    trade.id,
                )
            else:
                logger.info("Auto trade %s verified in DB", trade.id)

            saved_legs = (
                db.query(Leg)
                .filter(Leg.trade_id == trade.id, Leg.status == "open")
                .all()
            )
            if len(saved_legs) != 2:
                logger.critical(
                    "Auto trade %s has %s open legs (expected 2)! "
                    "DB save may be incomplete.",
                    trade.id,
                    len(saved_legs),
                )
            else:
                logger.info("Auto trade %s has 2 open legs in DB", trade.id)

            # Detach for tracker after session commits
            db.expunge(trade)
            db.expunge(call_leg)
            db.expunge(put_leg)
            self.position_tracker.add(trade, call_leg, put_leg)
            state = self.position_tracker.get(trade.id)
            if state is None:
                logger.critical(
                    "Auto trade %s NOT in position tracker! "
                    "Trade will not be monitored!",
                    trade.id,
                )
                self.position_tracker.add(trade, call_leg, put_leg)
                logger.info("Re-added trade %s to tracker", trade.id)
                log_and_buffer(
                    "ERROR",
                    int(trade.id),
                    {"stage": "auto_tracker_add", "error": "missing_after_add_retried"},
                )
            else:
                logger.info("Auto trade %s in tracker", trade.id)
                log_and_buffer(
                    "ENTRY_GUARD_PASS",
                    int(trade.id),
                    {"stage": "tracker_confirmed", "source": "auto"},
                )

            # Mirror to slave accounts (non-fatal)
            try:
                import backend.engine.mirror_engine as mirror_module

                if mirror_module.mirror_engine is not None:
                    asyncio.create_task(
                        mirror_module.mirror_engine.mirror_trade_entry(
                            master_trade_id=int(trade.id),
                            call_product_id=int(straddle["call_product_id"]),
                            put_product_id=int(straddle["put_product_id"]),
                            master_call_qty=qty,
                            master_put_qty=qty,
                            master_call_strike=float(
                                straddle.get("call_strike", straddle["strike"])
                            ),
                            master_put_strike=float(
                                straddle.get("put_strike", straddle["strike"])
                            ),
                            master_call_symbol=str(straddle["call_symbol"]),
                            master_put_symbol=str(straddle["put_symbol"]),
                            master_call_fill=float(call_fill),
                            master_put_fill=float(put_fill),
                            expiry_date=expiry_date,
                            underlying=str(settings.underlying),
                        )
                    )
                    logger.info("Mirror task queued for auto trade %s", trade.id)
            except Exception as exc:
                logger.warning("Mirror queue failed (non-fatal): %s", exc)

            # Refresh settings row after possible rollback
            from backend.database import get_or_create_auto_settings

            settings = get_or_create_auto_settings(db)
            settings.last_trade_id = int(trade.id)
            settings.retry_count = 0
            settings.last_error = None
            settings.next_entry_time = None
            settings.updated_at = now_ist
            db.commit()
            logger.info(
                "Cleared next_entry_time after successful placement"
            )

            logger.info(
                "AUTO TRADE PLACED: id=%s call_strike=%s put_strike=%s "
                "call_fill=%s put_fill=%s target=%s sl=%s",
                trade.id,
                straddle.get("call_strike", straddle["strike"]),
                straddle.get("put_strike", straddle["strike"]),
                call_fill,
                put_fill,
                profit_target_usd,
                stoploss_usd,
            )

            await ws_manager.broadcast(
                {
                    "type": "AUTO_TRADE_PLACED",
                    "trade_id": trade.id,
                    "underlying": settings.underlying,
                    "strike": straddle.get("call_strike", straddle["strike"]),
                    "call_strike": straddle.get("call_strike", straddle["strike"]),
                    "put_strike": straddle.get("put_strike", straddle["strike"]),
                    "call_premium": call_fill,
                    "put_premium": put_fill,
                    "expiry_date": expiry_str,
                    "message": (
                        f"Auto trade placed: {settings.underlying} "
                        f"call {straddle.get('call_strike', straddle['strike'])} / "
                        f"put {straddle.get('put_strike', straddle['strike'])} "
                        f"({settings.expiry_dte}DTE)"
                    ),
                }
            )
            await ws_manager.broadcast(
                {
                    "type": "TRADE_UPDATE",
                    "trade_id": trade.id,
                    "underlying": settings.underlying,
                    "status": TradeStatus.ACTIVE.value,
                    "basket_number": basket_no,
                }
            )

        except Exception as exc:
            logger.error("Auto trade placement failed: %s", exc, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
            await self._record_failure(settings, db, str(exc))

    async def _record_failure(
        self, settings: Any, db: Any, error: str
    ) -> None:
        from backend.database import get_or_create_auto_settings

        try:
            settings = get_or_create_auto_settings(db)
            now = get_ist_now()
            settings.retry_count = int(settings.retry_count or 0) + 1
            settings.last_error = error[:500]
            settings.next_entry_time = now + timedelta(seconds=_RETRY_DELAY_SECONDS)
            settings.updated_at = now
            db.commit()
        except Exception as exc:
            logger.warning("Could not persist auto-trade failure state: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass

        await ws_manager.broadcast(
            {
                "type": "AUTO_TRADE_FAILED",
                "underlying": getattr(settings, "underlying", "?"),
                "error": error,
                "retry_in_seconds": _RETRY_DELAY_SECONDS,
                "message": (
                    f"Auto trade failed: {error[:100]}. "
                    f"Retrying in {_RETRY_DELAY_SECONDS}s."
                ),
            }
        )

    def schedule_reentry(self, underlying: str, delay_minutes: int) -> None:
        """
        Called by bot_engine when a trade exits.
        Schedules next auto re-entry after delay (minimum 1 minute).
        """
        from backend.database import get_or_create_auto_settings

        with self.db_factory() as db:
            settings = get_or_create_auto_settings(db)

            if not settings.is_enabled:
                return
            if str(settings.underlying).upper() != str(underlying).upper():
                return

            now = get_ist_now()
            user_delay = int(delay_minutes)
            if user_delay <= 0:
                user_delay = int(settings.re_entry_delay_minutes or 1)
            # Minimum delay: max(user_delay, 1) — prevents instant re-entry
            effective_delay = max(user_delay, 1)
            reentry_time = now + timedelta(minutes=effective_delay)

            settings.last_exit_time = now
            settings.next_entry_time = reentry_time
            settings.retry_count = 0
            settings.last_error = None
            settings.updated_at = now
            db.commit()

            logger.info(
                "Auto re-entry scheduled: %s in %smin at %s IST",
                underlying,
                effective_delay,
                reentry_time.strftime("%H:%M:%S"),
            )


# Global singleton (set in main.py lifespan)
auto_trade_engine: AutoTradeEngine | None = None


# Module-level instance is constructed by callers (main / bot wiring).
# Import AutoTradeEngine and construct with real deps — no broken singleton.
