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
    get_utc_now,
    settling_ends_at_after_place,
    to_utc_for_db,
)
from backend.core.ws_manager import ws_manager
from backend.engine.trade_reconcile import next_basket_number, next_basket_seq_in_structure

logger = logging.getLogger(__name__)

_AUTO_LOOP_SECONDS = 30
_RETRY_DELAY_SECONDS = 60
# After N consecutive hedge-gate failures, pause retries to avoid burning spread
_HEDGE_GATE_FAIL_THRESHOLD = 3
_HEDGE_GATE_BACKOFF_SECONDS = 15 * 60


def _as_ist(dt: datetime | None) -> datetime | None:
    """Normalize DB datetime to IST for comparisons (naive = UTC wall-clock)."""
    from backend.core.time_utils import _as_ist as _shared_as_ist

    return _shared_as_ist(dt)


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
        # Consecutive hedge-open failures (reset on success or settings change)
        self._hedge_gate_fail_count: int = 0
        self._hedge_gate_settings_sig: tuple[Any, ...] | None = None

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
                        "next_entry_source": getattr(
                            settings, "next_entry_source", None
                        ),
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

        # Guard 3: Delta positions — block only bot-tracked; auto-close orphans
        # Active long-hedge legs (hedge_positions) are never orphans and never block.
        from backend.engine.hedge_lifecycle import get_active_hedge

        hedge_symbols: set[str] = set()
        hedge_pids: set[int] = set()
        _active_hedge = get_active_hedge(
            db,
            account_id=int(account.id),
            underlying=underlying,
        )
        if _active_hedge is not None:
            for _sym in (_active_hedge.call_symbol, _active_hedge.put_symbol):
                if _sym:
                    hedge_symbols.add(str(_sym).upper())
            for _pid in (
                _active_hedge.call_product_id,
                _active_hedge.put_product_id,
            ):
                try:
                    if _pid and int(_pid) > 0:
                        hedge_pids.add(int(_pid))
                except (TypeError, ValueError):
                    pass

        delta_option_positions = await client.get_option_positions()
        underlying_positions = [
            p
            for p in delta_option_positions
            if underlying
            in str(p.get("product_symbol") or p.get("symbol") or "").upper()
        ]
        tracked_legs = (
            db.query(Leg)
            .filter(
                Leg.status == "open",
                Leg.is_bot_managed.is_(True),
            )
            .all()
        )
        tracked_symbols = {
            str(leg.symbol or "").upper()
            for leg in tracked_legs
            if leg.symbol
        }

        blocking_symbols: list[str] = []
        for pos in underlying_positions:
            symbol = str(
                pos.get("product_symbol") or pos.get("symbol") or ""
            )
            try:
                size = int(float(pos.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            if size == 0:
                continue
            try:
                product_id = int(
                    pos.get("product_id")
                    or (pos.get("product") or {}).get("id")
                    or 0
                )
            except (TypeError, ValueError, AttributeError):
                product_id = 0

            if symbol.upper() in hedge_symbols or product_id in hedge_pids:
                # Active long hedge — keep it; short basket may share the book
                continue

            if symbol.upper() in tracked_symbols:
                # Bot's own open position — block entry
                blocking_symbols.append(symbol)
                continue

            # Orphan / manual position — auto-close, do not block
            log_and_buffer(
                "ORPHAN_DETECTED",
                0,
                {
                    "symbol": symbol,
                    "size": size,
                    "action": "auto_close",
                    "underlying": underlying,
                },
            )
            logger.warning(
                "ORPHAN_DETECTED: %s size=%s — auto-closing",
                symbol,
                size,
            )
            close_result: dict[str, Any] | str
            try:
                if product_id <= 0:
                    raise ValueError(f"missing product_id for {symbol}")
                close_result = await client.close_position(
                    product_id=product_id,
                    size=abs(size),
                    is_long=(size > 0),
                )
            except Exception as exc:
                close_result = f"failed: {exc}"
                logger.error(
                    "ORPHAN_AUTO_CLOSED failed for %s: %s", symbol, exc
                )
            log_and_buffer(
                "ORPHAN_AUTO_CLOSED",
                0,
                {"symbol": symbol, "close_result": close_result},
            )
            logger.info(
                "ORPHAN_AUTO_CLOSED: %s result=%s", symbol, close_result
            )

        if blocking_symbols:
            logger.warning(
                "Auto trade BLOCKED: Delta has %s bot-tracked %s option "
                "positions: %s. Will retry after positions are cleared.",
                len(blocking_symbols),
                underlying,
                blocking_symbols,
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "delta",
                    "underlying": underlying,
                    "symbols": blocking_symbols,
                },
            )
            settings.next_entry_time = get_utc_now() + timedelta(minutes=2)
            settings.next_entry_source = "retry"
            settings.last_error = (
                f"Delta has bot-tracked positions: {blocking_symbols}"[:500]
            )
            db.commit()
            return

        log_and_buffer(
            "ENTRY_GUARD_PASS",
            0,
            {
                "source": "auto",
                "guard": "delta",
                "underlying": underlying,
                "message": "all orphans cleared, proceeding",
            },
        )
        logger.info(
            "ENTRY_GUARD_PASS: delta guard — all orphans cleared, proceeding"
        )

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
        from datetime import date as _dt_date

        _override = getattr(settings, "expiry_date_override", None)
        _dte = int(settings.expiry_dte or 1)

        if _override and _dte > 2:
            # Weekly/Monthly: user explicitly chose a specific week/month.
            # Use exact date but validate it's still in the future.
            try:
                _parsed = _dt_date.fromisoformat(str(_override))
                ist_now = get_ist_now()
                cutoff = ist_now.replace(
                    hour=17, minute=15, second=0, microsecond=0
                )

                if ist_now > cutoff:
                    if _parsed > ist_now.date():
                        expiry_date = _parsed
                    else:
                        # Override date passed — compute from DTE
                        expiry_date = get_expiry_date_for_dte(_dte)
                else:
                    expiry_date = (
                        _parsed
                        if _parsed >= ist_now.date()
                        else get_expiry_date_for_dte(_dte)
                    )
            except (ValueError, TypeError):
                expiry_date = get_expiry_date_for_dte(_dte)
        else:
            # 0DTE/1DTE/2DTE: always compute fresh from integer.
            # get_expiry_date_for_dte already handles 5:15 PM cutoff.
            expiry_date = get_expiry_date_for_dte(_dte)
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
            settings.next_entry_time = get_utc_now() + timedelta(hours=1)
            settings.next_entry_source = "expiry_too_close"
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

        # Hedge gate — only when hedge_enabled. When False, skip entirely
        # (byte-identical to pre-hedge auto entry).
        hedge_position_id: int | None = None
        if bool(getattr(settings, "hedge_enabled", False)):
            from backend.engine.hedge_lifecycle import (
                get_active_hedge,
                get_pending_close_hedge,
            )

            pending = get_pending_close_hedge(
                db,
                account_id=int(account.id),
                underlying=str(underlying),
            )
            if pending is not None:
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    0,
                    {
                        "source": "auto",
                        "guard": "hedge_pending_close",
                        "hedge": int(pending.id),
                        "underlying": underlying,
                        "summary": (
                            f"[ENTRY_GUARD_BLOCK] guard=hedge_pending_close | "
                            f"hedge={int(pending.id)} | underlying={underlying}"
                        ),
                    },
                )
                logger.error(
                    "[ENTRY_GUARD_BLOCK] guard=hedge_pending_close | "
                    "hedge=%s | underlying=%s",
                    int(pending.id),
                    underlying,
                )
                return

            hedge_position_id = await self._hedge_entry_gate(
                settings=settings,
                db=db,
                account=account,
                client=client,
                underlying=underlying,
            )
            if hedge_position_id is None:
                return

            # Preventive: never place a basket without a live active hedge
            active_hedge = get_active_hedge(
                db,
                account_id=int(account.id),
                underlying=str(underlying),
            )
            if (
                active_hedge is None
                or int(active_hedge.id) != int(hedge_position_id)
                or str(active_hedge.status or "").lower().strip() != "active"
            ):
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    0,
                    {
                        "source": "auto",
                        "guard": "no_active_hedge",
                        "underlying": underlying,
                        "hedge_position_id": hedge_position_id,
                        "hedge_status": (
                            None
                            if active_hedge is None
                            else str(active_hedge.status or "")
                        ),
                    },
                )
                logger.error(
                    "[ENTRY_GUARD_BLOCK] guard=no_active_hedge | underlying=%s",
                    underlying,
                )
                return

        try:
            expiry_str = expiry_date.isoformat()
            logger.info("Auto trade: expiry=%s", expiry_str)

            trade_type = str(
                getattr(settings, "trade_type", None) or "straddle"
            ).lower().strip()
            strike_mode = str(
                getattr(settings, "strike_selection_mode", None)
                or "fixed_premium"
            ).lower().strip()
            hedge_on = bool(getattr(settings, "hedge_enabled", False))

            if hedge_on and strike_mode == "theta_based":
                from backend.core.hedge_theta import (
                    HedgeThetaError,
                    get_hedge_theta,
                    select_theta_based_strikes,
                )
                from backend.engine.hedge_lifecycle import get_active_hedge

                if hedge_position_id is None:
                    raise RuntimeError(
                        "theta_based entry requires an active hedge"
                    )
                hedge_row = get_active_hedge(
                    db,
                    account_id=int(account.id),
                    underlying=str(underlying),
                )
                if hedge_row is None:
                    raise RuntimeError(
                        f"Active hedge #{hedge_position_id} not found for "
                        "theta_based strike selection"
                    )
                theta_info = await get_hedge_theta(client, hedge_row)
                hedge_call_th = abs(float(theta_info["call_theta"]))
                mult = float(
                    getattr(settings, "theta_multiplier", None) or 3.0
                )
                required_th = hedge_call_th * mult
                if required_th <= 0:
                    raise RuntimeError(
                        "theta_based entry: hedge call_theta is zero"
                    )
                und_key = str(settings.underlying).upper().strip()
                price_map = {
                    "BTC": "BTCUSD",
                    "ETH": "ETHUSD",
                    "XAU": "XAUUSD",
                }
                price_symbol = price_map.get(
                    und_key,
                    und_key if und_key.endswith("USD") else f"{und_key}USD",
                )
                spot = float(await client.get_underlying_price(price_symbol))
                short_chain = await client.get_option_chain(
                    und_key, expiry_str
                )
                if not short_chain:
                    raise RuntimeError(
                        f"Empty option chain for {settings.underlying} "
                        f"{expiry_str}"
                    )
                tol = getattr(
                    settings, "entry_premium_match_tolerance_pct", None
                )
                try:
                    picks = select_theta_based_strikes(
                        short_chain,
                        spot,
                        required_th,
                        hedge_call_theta=hedge_call_th,
                        theta_multiplier=mult,
                        log_hedge_id=int(hedge_position_id),
                        log_trade_id=0,
                        entry_premium_match_tolerance_pct=(
                            float(tol) if tol is not None else None
                        ),
                    )
                except HedgeThetaError as exc:
                    raise RuntimeError(
                        f"theta_based strike selection failed: {exc}"
                    ) from exc
                call_pick = picks["call"]
                put_pick = picks["put"]
                call_pid = int(call_pick.get("product_id") or 0)
                put_pid = int(put_pick.get("product_id") or 0)
                call_sym = str(call_pick.get("symbol") or "")
                put_sym = str(put_pick.get("symbol") or "")
                if call_pid <= 0 or put_pid <= 0:
                    # Resolve from chain if product_id missing
                    call_row = next(
                        (
                            r
                            for r in short_chain
                            if abs(
                                float(r["strike"]) - float(call_pick["strike"])
                            )
                            < 0.01
                        ),
                        None,
                    )
                    put_row = next(
                        (
                            r
                            for r in short_chain
                            if abs(
                                float(r["strike"]) - float(put_pick["strike"])
                            )
                            < 0.01
                        ),
                        None,
                    )
                    if call_row is None or put_row is None:
                        raise RuntimeError(
                            "theta_based entry: selected strikes not on chain"
                        )
                    call_pid = int(call_row.get("call_product_id") or 0)
                    put_pid = int(put_row.get("put_product_id") or 0)
                    call_sym = str(call_row.get("call_symbol") or "")
                    put_sym = str(put_row.get("put_symbol") or "")
                straddle = {
                    "call_strike": float(call_pick["strike"]),
                    "call_symbol": call_sym,
                    "call_product_id": call_pid,
                    "call_premium": float(call_pick["premium"]),
                    "put_strike": float(put_pick["strike"]),
                    "put_symbol": put_sym,
                    "put_product_id": put_pid,
                    "put_premium": float(put_pick["premium"]),
                    "spot_price": float(spot),
                    "expiry_date": expiry_str,
                    "underlying": str(settings.underlying).upper().strip(),
                    "trade_type": "strangle",
                    "strike": float(call_pick["strike"]),
                }
                logger.info(
                    "THETA_BASED: call_strike=%s put_strike=%s "
                    "call=%.2f put=%.2f deviation_pct=%.2f",
                    straddle["call_strike"],
                    straddle["put_strike"],
                    straddle["call_premium"],
                    straddle["put_premium"],
                    float(picks.get("premium_deviation_pct") or 0),
                )
            elif trade_type == "strangle":
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
            if not (hedge_on and strike_mode == "theta_based"):
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
            # Chicken-and-egg: bracket must ship WITH the entry order before
            # fill exists. Attach mark × uni_sl now; after fill try amend to
            # fill-derived. If amend fails, mark-derived stays canonical for
            # master AND slaves so they still match exactly.
            from backend.core.delta_sl import (
                compute_bracket_sl,
                finalize_bracket_sl_after_fill,
            )

            call_prov_sl, call_prov_limit = compute_bracket_sl(
                call_mark,
                universal_sl_pct,
                master_mark=call_mark,
                leg="call",
                trade_id=None,
            )
            put_prov_sl, put_prov_limit = compute_bracket_sl(
                put_mark,
                universal_sl_pct,
                master_mark=put_mark,
                leg="put",
                trade_id=None,
            )

            # --- Place CALL (bracket SL attached inline) ---
            logger.info(
                "Placing CALL: %s qty=%s bracket_sl=%s",
                straddle["call_symbol"],
                qty,
                call_prov_sl,
            )
            call_open_ts = get_utc_now()
            call_result = await self.order_executor.sell_option(
                product_id=int(straddle["call_product_id"]),
                quantity=qty,
                delta_client=client,
                symbol_for_fallback=str(straddle["call_symbol"]),
                bracket_sl_price=call_prov_sl if call_prov_sl > 0 else None,
                bracket_sl_limit=call_prov_limit if call_prov_sl > 0 else None,
            )
            if not call_result.success:
                raise RuntimeError(
                    f"Call order failed: {call_result.error or 'unknown'}"
                )
            call_fill_ts = get_utc_now()
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
            call_sl_trigger_price, call_sl_limit = (
                await finalize_bracket_sl_after_fill(
                    client,
                    entry_order_id=call_order_id,
                    product_id=int(straddle["call_product_id"]),
                    mark_price=call_mark,
                    fill_price=call_fill,
                    universal_sl_pct=universal_sl_pct,
                    provisional_stop=call_prov_sl,
                    provisional_limit=call_prov_limit,
                    leg="call",
                    trade_id=None,
                )
            )

            # --- Place PUT (bracket SL attached inline) ---
            logger.info(
                "Placing PUT: %s qty=%s bracket_sl=%s",
                straddle["put_symbol"],
                qty,
                put_prov_sl,
            )
            put_open_ts = get_utc_now()
            put_result = await self.order_executor.sell_option(
                product_id=int(straddle["put_product_id"]),
                quantity=qty,
                delta_client=client,
                symbol_for_fallback=str(straddle["put_symbol"]),
                bracket_sl_price=put_prov_sl if put_prov_sl > 0 else None,
                bracket_sl_limit=put_prov_limit if put_prov_sl > 0 else None,
            )
            if not put_result.success:
                raise RuntimeError(
                    f"PARTIAL: Call placed @ {call_fill} "
                    f"(order {call_order_id}) but Put failed: "
                    f"{put_result.error or 'unknown'}"
                )
            put_fill_ts = get_utc_now()
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
            put_sl_trigger_price, put_sl_limit = (
                await finalize_bracket_sl_after_fill(
                    client,
                    entry_order_id=put_order_id,
                    product_id=int(straddle["put_product_id"]),
                    mark_price=put_mark,
                    fill_price=put_fill,
                    universal_sl_pct=universal_sl_pct,
                    provisional_stop=put_prov_sl,
                    provisional_limit=put_prov_limit,
                    leg="put",
                    trade_id=None,
                )
            )

            # TP/SL locked to initial deployment premium (actual fills)
            # initial_max_profit never changes after trade entry
            # adjustments do NOT affect TP/SL
            initial_max_profit = round(
                (call_fill + put_fill) * qty * float(OPTIONS_CONTRACT_VALUE),
                6,
            )
            stoploss_usd = round(initial_max_profit * sl_pct / 100.0, 2)

            from backend.core.hedge_theta import (
                compute_basket_profit_target_at_entry,
            )
            from backend.engine.hedge_lifecycle import get_active_hedge

            hedge_for_target = None
            if hedge_position_id is not None:
                hedge_for_target = get_active_hedge(
                    db,
                    account_id=int(account.id),
                    underlying=str(underlying),
                )
            btc_for_target = 0.0
            try:
                und_key = str(settings.underlying).upper().strip()
                price_map = {
                    "BTC": "BTCUSD",
                    "ETH": "ETHUSD",
                    "XAU": "XAUUSD",
                }
                price_symbol = price_map.get(
                    und_key,
                    und_key if und_key.endswith("USD") else f"{und_key}USD",
                )
                btc_for_target = float(
                    await client.get_underlying_price(price_symbol)
                )
            except Exception:
                btc_for_target = 0.0

            target_info = await compute_basket_profit_target_at_entry(
                settings=settings,
                client=client,
                hedge=hedge_for_target,
                quantity=qty,
                credit_usd=float(initial_max_profit),
                call_fill=float(call_fill),
                put_fill=float(put_fill),
                call_fee=call_fee,
                put_fee=put_fee,
                call_symbol=str(straddle.get("call_symbol") or ""),
                put_symbol=str(straddle.get("put_symbol") or ""),
                tp_pct=float(tp_pct),
                btc_index=btc_for_target,
            )
            profit_target_usd = float(target_info["profit_target_usd"])
            target_source = str(target_info["target_source"])
            hedge_theta_at_entry = target_info.get("hedge_theta_at_entry")

            if target_info.get("capped"):
                logger.warning(
                    "[TARGET_UNREACHABLE] target=%s max_achievable=%s "
                    "credit=%s friction=%s capture_required_pct=%s "
                    "(capping at 90%% of max_achievable)",
                    target_info["raw_target_usd"],
                    target_info["max_achievable"],
                    target_info["credit_usd"],
                    target_info["friction"],
                    target_info["capture_required_pct"],
                )

            now_utc = get_utc_now()
            now_ist = get_ist_now()
            from backend.config import ENTRY_SETTLING_SECONDS

            entry_secs = int(ENTRY_SETTLING_SECONDS)
            raw_entry = getattr(settings, "entry_settling_seconds", None)
            if raw_entry is not None:
                entry_secs = int(raw_entry)
            entry_secs = max(0, min(300, entry_secs))
            monitoring_starts = settling_ends_at_after_place(
                now_ist, seconds=entry_secs
            )
            basket_no = next_basket_number(db, int(account.id))
            seq_in_structure = next_basket_seq_in_structure(
                db,
                int(hedge_position_id) if hedge_position_id is not None else None,
            )

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
                combined_trigger_mode=bool(
                    getattr(settings, "combined_trigger_mode", False)
                ),
                realized_pnl=0.0,
                monitoring_starts_at=to_utc_for_db(monitoring_starts, context="trades.monitoring_starts_at"),
                initial_max_profit=initial_max_profit,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                universal_sl_pct=float(settings.universal_sl_pct or 200.0),
                slippage_pct=float(settings.slippage_pct or 2.0),
                basket_number=basket_no,
                basket_seq_in_structure=seq_in_structure,
                notes="auto_trade",
                entry_spread_for_sl_usd=0.0,
                hedge_position_id=(
                    int(hedge_position_id)
                    if hedge_position_id is not None
                    else None
                ),
                target_source=target_source,
                hedge_theta_at_entry=(
                    float(hedge_theta_at_entry)
                    if hedge_theta_at_entry is not None
                    else None
                ),
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

            # Re-emit TARGET_UNREACHABLE with real trade id if capped
            if target_info.get("capped"):
                log_and_buffer(
                    "TARGET_UNREACHABLE",
                    int(trade.id),
                    {
                        "trade": int(trade.id),
                        "target": round(
                            float(target_info["raw_target_usd"]), 6
                        ),
                        "max_achievable": round(
                            float(target_info["max_achievable"]), 6
                        ),
                        "credit": round(float(target_info["credit_usd"]), 6),
                        "friction": round(float(target_info["friction"]), 6),
                        "capture_required_pct": round(
                            float(
                                target_info.get(
                                    "capture_required_pct_raw",
                                    target_info["capture_required_pct"],
                                )
                            ),
                            2,
                        ),
                        "summary": (
                            f"[TARGET_UNREACHABLE] trade={int(trade.id)} | "
                            f"target="
                            f"{round(float(target_info['raw_target_usd']), 6)} | "
                            f"max_achievable="
                            f"{round(float(target_info['max_achievable']), 6)} | "
                            f"credit="
                            f"{round(float(target_info['credit_usd']), 6)} | "
                            f"friction="
                            f"{round(float(target_info['friction']), 6)} | "
                            f"capture_required_pct="
                            f"{round(float(target_info.get('capture_required_pct_raw', target_info['capture_required_pct'])), 2)}"
                        ),
                    },
                )

            log_and_buffer(
                "BASKET_TARGET_SET",
                int(trade.id),
                {
                    "trade": int(trade.id),
                    "mode": target_source,
                    "hedge_total_theta": (
                        round(float(hedge_theta_at_entry), 4)
                        if hedge_theta_at_entry is not None
                        else None
                    ),
                    "multiple": round(
                        float(target_info["basket_target_multiple"]), 4
                    ),
                    "target_usd": round(profit_target_usd, 6),
                    "credit": round(float(target_info["credit_usd"]), 6),
                    "friction": round(float(target_info["friction"]), 6),
                    "capture_required_pct": round(
                        float(target_info["capture_required_pct"]), 2
                    ),
                    "capped": bool(target_info.get("capped")),
                    "summary": (
                        f"[BASKET_TARGET_SET] trade={int(trade.id)} | "
                        f"mode={target_source} | "
                        f"hedge_total_theta="
                        f"{round(float(hedge_theta_at_entry), 4) if hedge_theta_at_entry is not None else 'n/a'} | "
                        f"multiple="
                        f"{round(float(target_info['basket_target_multiple']), 4)} | "
                        f"target_usd={round(profit_target_usd, 6)} | "
                        f"credit={round(float(target_info['credit_usd']), 6)} | "
                        f"friction={round(float(target_info['friction']), 6)} | "
                        f"capture_required_pct="
                        f"{round(float(target_info['capture_required_pct']), 2)} | "
                        f"capped={bool(target_info.get('capped'))}"
                    ),
                },
            )
            logger.info(
                "[BASKET_TARGET_SET] trade=%s | mode=%s | target_usd=%s | "
                "hedge_total_theta=%s | multiple=%s",
                int(trade.id),
                target_source,
                round(profit_target_usd, 6),
                (
                    round(float(hedge_theta_at_entry), 4)
                    if hedge_theta_at_entry is not None
                    else None
                ),
                round(float(target_info["basket_target_multiple"]), 4),
            )

            from backend.core.fees import (
                compute_entry_spread_usd,
                reset_entry_spread_for_sl,
            )

            call_entry_spread = compute_entry_spread_usd(
                sent_price=call_mark,
                fill_price=call_fill,
                quantity=qty,
                is_long=False,
            )
            put_entry_spread = compute_entry_spread_usd(
                sent_price=put_mark,
                fill_price=put_fill,
                quantity=qty,
                is_long=False,
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
                order_sent_price=call_mark,
                entry_spread_usd=call_entry_spread,
                sl_trigger_price=float(call_sl_trigger_price)
                if call_sl_trigger_price and call_sl_trigger_price > 0
                else None,
                delta_sl_order_id=None,  # bracket — no separate stop order id
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
                order_sent_price=put_mark,
                entry_spread_usd=put_entry_spread,
                sl_trigger_price=float(put_sl_trigger_price)
                if put_sl_trigger_price and put_sl_trigger_price > 0
                else None,
                delta_sl_order_id=None,  # bracket — no separate stop order id
            )
            reset_entry_spread_for_sl(
                trade,
                abs(float(call_entry_spread or 0.0))
                + abs(float(put_entry_spread or 0.0)),
                reason="trade_entry",
                leg="call+put",
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

            try:
                from backend.engine.structure_ledger import (
                    record_master_basket_entry,
                )

                record_master_basket_entry(
                    db,
                    trade,
                    call_leg,
                    put_leg,
                    call_opened_at=call_open_ts,
                    put_opened_at=put_open_ts,
                    call_fill_at=call_fill_ts,
                    put_fill_at=put_fill_ts,
                )
                db.commit()
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger master basket entry failed: %s",
                    ledger_exc,
                    exc_info=True,
                )

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
                            master_bracket_sl_call=(
                                float(call_sl_trigger_price)
                                if call_sl_trigger_price > 0
                                else None
                            ),
                            master_bracket_sl_put=(
                                float(put_sl_trigger_price)
                                if put_sl_trigger_price > 0
                                else None
                            ),
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
            settings.next_entry_source = None
            settings.updated_at = get_utc_now()
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

            # CRITICAL: If call was placed but put failed, close the call
            # to avoid naked exposure on Delta
            if (
                "call_result" in locals()
                and call_result.success
                and "straddle" in locals()
                and client is not None
            ):
                if "put_result" not in locals() or not put_result.success:
                    logger.critical(
                        "PARTIAL ENTRY: Call placed but Put failed. "
                        "Attempting to close call order to avoid naked exposure."
                    )
                    try:
                        call_pid = int(straddle["call_product_id"])
                        call_qty = max(1, int(settings.quantity))
                        close_res = await client.close_position(
                            product_id=call_pid,
                            size=call_qty,
                            is_long=False,  # we sold it (short), close = buy
                        )
                        logger.info("Partial entry cleanup: %s", close_res)
                        log_and_buffer(
                            "PARTIAL_ENTRY_CLEANUP",
                            0,
                            {
                                "symbol": straddle.get("call_symbol"),
                                "result": str(close_res),
                            },
                        )
                    except Exception as cleanup_exc:
                        logger.critical(
                            "PARTIAL ENTRY CLEANUP FAILED: %s. "
                            "Manual intervention required!",
                            cleanup_exc,
                        )
                        log_and_buffer(
                            "PARTIAL_ENTRY_CLEANUP_FAILED",
                            0,
                            {
                                "symbol": straddle.get("call_symbol", "?"),
                                "error": str(cleanup_exc),
                            },
                        )

            try:
                db.rollback()
            except Exception:
                pass
            await self._record_failure(settings, db, str(exc))

    def _hedge_gate_settings_signature(self, settings: Any) -> tuple[Any, ...]:
        """Fingerprint of settings that should reset the hedge-gate fail counter."""
        return (
            str(getattr(settings, "underlying", "") or "").upper(),
            bool(getattr(settings, "hedge_enabled", False)),
            int(getattr(settings, "quantity", 0) or 0),
            float(getattr(settings, "hedge_qty_ratio", 1.0) or 1.0),
            float(getattr(settings, "hedge_target_usd", 0) or 0),
            float(getattr(settings, "hedge_stoploss_usd", 0) or 0),
            str(getattr(settings, "hedge_expiry_mode", "") or ""),
        )

    def _reset_hedge_gate_failures_if_settings_changed(self, settings: Any) -> None:
        sig = self._hedge_gate_settings_signature(settings)
        if sig != self._hedge_gate_settings_sig:
            if self._hedge_gate_fail_count > 0:
                logger.info(
                    "Hedge gate fail counter reset (settings changed) "
                    "was=%s",
                    self._hedge_gate_fail_count,
                )
            self._hedge_gate_fail_count = 0
            self._hedge_gate_settings_sig = sig

    async def _record_hedge_gate_failure(
        self, settings: Any, db: Any, error: str
    ) -> None:
        """
        Record a hedge-gate failure with escalating backoff.

        First failures: normal short retry. After threshold consecutive fails:
        long backoff so margin errors do not burn bid-ask every minute.
        """
        from backend.core.bot_logger import log_and_buffer
        from backend.database import get_or_create_auto_settings

        self._reset_hedge_gate_failures_if_settings_changed(settings)
        self._hedge_gate_fail_count = int(self._hedge_gate_fail_count or 0) + 1
        attempts = self._hedge_gate_fail_count

        if attempts >= _HEDGE_GATE_FAIL_THRESHOLD:
            delay = _HEDGE_GATE_BACKOFF_SECONDS
            source = "hedge_gate"
        else:
            delay = _RETRY_DELAY_SECONDS
            source = "retry"

        now = get_utc_now()
        next_at = now + timedelta(seconds=delay)

        try:
            settings = get_or_create_auto_settings(db)
            settings.retry_count = int(settings.retry_count or 0) + 1
            settings.last_error = error[:500]
            settings.next_entry_time = next_at
            settings.next_entry_source = source
            settings.updated_at = now
            db.commit()
        except Exception as exc:
            logger.warning(
                "Could not persist hedge-gate failure state: %s", exc
            )
            try:
                db.rollback()
            except Exception:
                pass

        if attempts >= _HEDGE_GATE_FAIL_THRESHOLD:
            log_and_buffer(
                "HEDGE_GATE_BACKOFF",
                0,
                {
                    "attempts": attempts,
                    "threshold": _HEDGE_GATE_FAIL_THRESHOLD,
                    "backoff_seconds": delay,
                    "next_attempt_at": next_at.isoformat(),
                    "error": error[:200],
                },
            )
            logger.critical(
                "[HEDGE_GATE_BACKOFF] attempts=%s — pausing hedge open "
                "retries until %s (%ss). %s",
                attempts,
                next_at.isoformat(),
                delay,
                error[:160],
            )

        await ws_manager.broadcast(
            {
                "type": "AUTO_TRADE_FAILED",
                "underlying": getattr(settings, "underlying", "?"),
                "error": error,
                "retry_in_seconds": delay,
                "message": (
                    f"Auto trade failed: {error[:160]}. "
                    f"Retrying in {delay}s."
                ),
            }
        )

    async def _hedge_entry_gate(
        self,
        *,
        settings: Any,
        db: Any,
        account: Any,
        client: Any,
        underlying: str,
    ) -> int | None:
        """
        Ensure an active long hedge exists before placing the short basket.

        Returns hedge_positions.id on success, or None if the basket must NOT
        be placed (caller should return immediately).
        """
        from backend.core.bot_logger import log_and_buffer
        from backend.engine.hedge_lifecycle import (
            HedgeOpenError,
            get_active_hedge,
            open_hedge,
        )

        self._reset_hedge_gate_failures_if_settings_changed(settings)

        existing = get_active_hedge(
            db,
            account_id=int(account.id),
            underlying=str(underlying),
        )
        if existing is not None:
            hid = int(existing.id)
            self._hedge_gate_fail_count = 0
            log_and_buffer(
                "HEDGE_GATE",
                hid,
                {
                    "hedge_enabled": True,
                    "existing_hedge_id": hid,
                    "action": "reuse",
                    "underlying": underlying,
                },
            )
            log_and_buffer(
                "ENTRY_GUARD_PASS",
                hid,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "action": "reuse",
                    "hedge_id": hid,
                    "underlying": underlying,
                },
            )
            logger.info(
                "HEDGE_GATE: reuse active hedge #%s for %s — placing basket only",
                hid,
                underlying,
            )
            return hid

        basket_qty = max(1, int(settings.quantity or 1))
        try:
            ratio = float(getattr(settings, "hedge_qty_ratio", None) or 1.0)
        except (TypeError, ValueError):
            ratio = 1.0
        if ratio <= 0:
            ratio = 1.0
        hedge_qty = max(1, int(round(basket_qty * ratio)))

        log_and_buffer(
            "HEDGE_GATE",
            0,
            {
                "hedge_enabled": True,
                "existing_hedge_id": None,
                "action": "open",
                "underlying": underlying,
                "basket_qty": basket_qty,
                "hedge_qty": hedge_qty,
                "hedge_qty_ratio": ratio,
            },
        )
        logger.info(
            "HEDGE_GATE: no active hedge for %s — opening hedge qty=%s "
            "(basket_qty=%s × ratio=%.2f)",
            underlying,
            hedge_qty,
            basket_qty,
            ratio,
        )

        try:
            hedge = await open_hedge(
                account,
                settings,
                db,
                client=client,
                quantity_override=hedge_qty,
            )
        except HedgeOpenError as exc:
            reason = f"{exc.stage}: {exc.reason}"
            log_and_buffer(
                "HEDGE_GATE",
                0,
                {
                    "hedge_enabled": True,
                    "existing_hedge_id": None,
                    "action": "blocked",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            log_and_buffer(
                "HEDGE_GATE_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "underlying": underlying,
                    "stage": exc.stage,
                    "reason": str(exc.reason),
                },
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "underlying": underlying,
                    "stage": exc.stage,
                    "reason": str(exc.reason),
                },
            )
            logger.critical(
                "[HEDGE_GATE_BLOCK] hedge open failed — NOT placing basket. %s",
                reason,
            )
            await self._record_hedge_gate_failure(
                settings,
                db,
                f"HEDGE_GATE_BLOCK: {reason}",
            )
            return None
        except Exception as exc:
            reason = str(exc)
            log_and_buffer(
                "HEDGE_GATE",
                0,
                {
                    "hedge_enabled": True,
                    "existing_hedge_id": None,
                    "action": "blocked",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            log_and_buffer(
                "HEDGE_GATE_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                0,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            logger.critical(
                "[HEDGE_GATE_BLOCK] unexpected hedge open error — "
                "NOT placing basket: %s",
                exc,
                exc_info=True,
            )
            await self._record_hedge_gate_failure(
                settings,
                db,
                f"HEDGE_GATE_BLOCK: {reason}",
            )
            return None

        hid = int(hedge.id)
        # open_hedge returns existing if raced — treat as success either way
        if str(hedge.status or "").lower() != "active":
            reason = f"hedge #{hid} status={hedge.status} after open"
            log_and_buffer(
                "HEDGE_GATE",
                hid,
                {
                    "hedge_enabled": True,
                    "existing_hedge_id": hid,
                    "action": "blocked",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            log_and_buffer(
                "HEDGE_GATE_BLOCK",
                hid,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            log_and_buffer(
                "ENTRY_GUARD_BLOCK",
                hid,
                {
                    "source": "auto",
                    "guard": "hedge",
                    "underlying": underlying,
                    "reason": reason,
                },
            )
            logger.critical(
                "[HEDGE_GATE_BLOCK] %s — NOT placing basket", reason
            )
            await self._record_hedge_gate_failure(
                settings, db, f"HEDGE_GATE_BLOCK: {reason}"
            )
            return None

        self._hedge_gate_fail_count = 0
        log_and_buffer(
            "HEDGE_GATE",
            hid,
            {
                "hedge_enabled": True,
                "existing_hedge_id": hid,
                "action": "open",
                "underlying": underlying,
                "hedge_qty": hedge_qty,
            },
        )
        log_and_buffer(
            "ENTRY_GUARD_PASS",
            hid,
            {
                "source": "auto",
                "guard": "hedge",
                "action": "open",
                "hedge_id": hid,
                "underlying": underlying,
            },
        )
        logger.info(
            "HEDGE_GATE: opened hedge #%s — proceeding to place short basket",
            hid,
        )
        return hid

    async def _record_failure(
        self, settings: Any, db: Any, error: str
    ) -> None:
        from backend.database import get_or_create_auto_settings

        try:
            settings = get_or_create_auto_settings(db)
            now = get_utc_now()
            settings.retry_count = int(settings.retry_count or 0) + 1
            settings.last_error = error[:500]
            settings.next_entry_time = now + timedelta(seconds=_RETRY_DELAY_SECONDS)
            settings.next_entry_source = "retry"
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

    def schedule_reentry(
        self,
        underlying: str,
        delay_minutes: int,
        *,
        source: str = "reentry_delay",
    ) -> None:
        """
        Called by bot_engine when a trade exits.
        Schedules next auto re-entry after delay (minimum 1 minute).

        source: 'reentry_delay' | 'cooldown_after_loss' — only reentry_delay
        is recomputed when the user edits re_entry_delay_minutes.
        """
        from backend.database import get_or_create_auto_settings

        with self.db_factory() as db:
            settings = get_or_create_auto_settings(db)

            if not settings.is_enabled:
                return
            if str(settings.underlying).upper() != str(underlying).upper():
                return

            now = get_utc_now()
            user_delay = int(delay_minutes)
            if user_delay <= 0:
                user_delay = int(settings.re_entry_delay_minutes or 1)
            # Minimum delay: max(user_delay, 1) — prevents instant re-entry
            effective_delay = max(user_delay, 1)
            reentry_time = now + timedelta(minutes=effective_delay)

            src = str(source or "reentry_delay").strip().lower()
            if src not in {"reentry_delay", "cooldown_after_loss"}:
                src = "reentry_delay"

            settings.last_exit_time = now
            settings.next_entry_time = reentry_time
            settings.next_entry_source = src
            settings.retry_count = 0
            settings.last_error = None
            settings.updated_at = now
            db.commit()

            logger.info(
                "Auto re-entry scheduled: %s in %smin at %s IST (source=%s)",
                underlying,
                effective_delay,
                reentry_time.strftime("%H:%M:%S"),
                src,
            )


# Global singleton (set in main.py lifespan)
auto_trade_engine: AutoTradeEngine | None = None


# Module-level instance is constructed by callers (main / bot wiring).
# Import AutoTradeEngine and construct with real deps — no broken singleton.
