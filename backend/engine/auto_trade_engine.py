# auto_trade_engine.py — Auto re-entry of ATM straddles after exits

from __future__ import annotations

import asyncio
import logging
import math
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
from backend.core.entry_basis import blend_entry_premium
from backend.engine.trade_reconcile import next_basket_number, next_basket_seq_in_structure

logger = logging.getLogger(__name__)

_AUTO_LOOP_SECONDS = 30
_RETRY_DELAY_SECONDS = 60
# After N consecutive hedge-gate failures, pause retries to avoid burning spread
_HEDGE_GATE_FAIL_THRESHOLD = 3
_HEDGE_GATE_BACKOFF_SECONDS = 15 * 60


def resolve_basket_qty_from_hedge(hedge_qty: int, pct: float) -> int:
    """ceil(hedge_qty × pct / 100). Returns 0 when inputs are non-positive."""
    try:
        hq = int(hedge_qty)
        p = float(pct)
    except (TypeError, ValueError):
        return 0
    if hq <= 0 or p <= 0:
        return 0
    return int(math.ceil(hq * p / 100.0))


def compute_dynamic_basket_qty_pct(
    *,
    hedge_call_theta: float,
    theta_mult: float,
    call_ask: float,
) -> float | None:
    """
    basket_qty_pct = (hedge_call_theta × theta_mult × 100) / call_ask
    Returns None when call_ask or theta is non-positive.
    """
    try:
        theta = abs(float(hedge_call_theta))
        mult = float(theta_mult)
        ask = float(call_ask)
    except (TypeError, ValueError):
        return None
    if ask <= 0 or theta <= 0 or mult <= 0:
        return None
    return (theta * mult * 100.0) / ask


def resolve_entry_basket_pct(
    settings: Any,
    *,
    straddle: dict[str, Any],
    hedge_call_theta: float | None,
    sizing_mode: str,
) -> tuple[float, float | None, bool]:
    """
    Return (pct_for_sizing, computed_pct_audit, dynamic_requested).

    When dynamic is off or sizing is fixed, uses manual basket_qty_pct_of_hedge.
    When dynamic is on but formula inputs are invalid, falls back to manual pct
    and returns computed_pct_audit=None.
    """
    manual_pct = float(getattr(settings, "basket_qty_pct_of_hedge", None) or 20.0)
    if sizing_mode != "pct_of_hedge":
        return manual_pct, None, False

    dynamic = bool(getattr(settings, "basket_qty_dynamic", False))
    if not dynamic:
        return manual_pct, None, False

    if not bool(getattr(settings, "hedge_enabled", False)):
        return manual_pct, None, False

    mult = float(getattr(settings, "basket_qty_theta_mult", None) or 2.0)
    call_ask = float(straddle.get("call_premium") or 0)
    theta_val = float(hedge_call_theta or 0)
    computed = compute_dynamic_basket_qty_pct(
        hedge_call_theta=theta_val,
        theta_mult=mult,
        call_ask=call_ask,
    )
    if computed is not None and computed > 0:
        return float(computed), float(computed), True
    return manual_pct, None, True


def resolve_adjustment_basket_qty(
    *,
    settings: Any,
    triggered_leg_qty: int,
    hedge_qty: int,
    hedge_call_theta: float,
    new_strike_ask: float,
    trade_id: int | None = None,
    original_qty: int | None = None,
    adjustment_number: int | None = None,
) -> tuple[int, bool]:
    """
    Lot count for replacement short leg at adjustment.

    Returns (new_qty, close_basket).
    Wings are never resized here — caller must leave wing legs untouched.

    Modes (adjustment_qty_mode, with migration from use_dynamic_qty_on_adjustment):
      unchanged         — return triggered_leg_qty
      increase_dynamic  — B25 theta formula + 50% hedge cap
      decrease_step     — floor(original × (1 − pct/100 × adj_n)), min 1
    """
    from backend.engine.wing_entry import (
        compute_decrease_step_qty,
        resolve_adjustment_qty_mode,
    )

    base_qty = max(1, int(triggered_leg_qty or 1))
    mode = resolve_adjustment_qty_mode(settings)

    if mode == "decrease_step":
        orig = int(original_qty) if original_qty is not None else base_qty
        orig = max(1, orig)
        adj_n = int(adjustment_number) if adjustment_number is not None else 1
        adj_n = max(1, adj_n)
        pct = float(
            getattr(settings, "adjustment_qty_decrease_pct", None) or 25.0
        )
        if not (0 < pct < 100):
            pct = 25.0
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=orig,
            adjustment_number=adj_n,
            decrease_pct=pct,
        )
        if close_basket or new_qty is None:
            try:
                from backend.core.bot_logger import log_and_buffer

                log_and_buffer(
                    "ADJ_QTY_DECREASE",
                    int(trade_id) if trade_id is not None else 0,
                    {
                        "original": orig,
                        "adj_n": adj_n,
                        "pct": pct,
                        "new_qty": "close",
                        "note": "remaining<=0",
                    },
                )
            except Exception:
                logger.info(
                    "[ADJ_QTY_DECREASE] trade=%s original=%s adj_n=%s pct=%s "
                    "new_qty=close remaining<=0",
                    trade_id if trade_id is not None else "?",
                    orig,
                    adj_n,
                    pct,
                )
            return base_qty, True
        try:
            from backend.core.bot_logger import log_and_buffer

            log_and_buffer(
                "ADJ_QTY_DECREASE",
                int(trade_id) if trade_id is not None else 0,
                {
                    "original": orig,
                    "adj_n": adj_n,
                    "pct": pct,
                    "new_qty": int(new_qty),
                },
            )
        except Exception:
            logger.info(
                "[ADJ_QTY_DECREASE] trade=%s original=%s adj_n=%s pct=%s new_qty=%s",
                trade_id if trade_id is not None else "?",
                orig,
                adj_n,
                pct,
                new_qty,
            )
        return int(new_qty), False

    if mode != "increase_dynamic":
        return base_qty, False

    # increase_dynamic (B25): requires basket_qty_dynamic
    basket_dyn = bool(getattr(settings, "basket_qty_dynamic", False))
    if not basket_dyn:
        return base_qty, False

    mult = float(getattr(settings, "basket_qty_theta_mult", None) or 2.0)
    raw_pct = compute_dynamic_basket_qty_pct(
        hedge_call_theta=hedge_call_theta,
        theta_mult=mult,
        call_ask=new_strike_ask,
    )
    hq = int(hedge_qty or 0)
    if raw_pct is None or raw_pct <= 0 or hq <= 0:
        return base_qty, False

    raw_qty = int(math.ceil(hq * float(raw_pct) / 100.0))
    max_qty = max(1, int(math.floor(hq * 0.5)))
    capped = max(1, min(raw_qty, max_qty))
    # Cap must never shrink below current leg qty — increase_dynamic only grows.
    # Entry can already be above 50% of hedge; cutting on adjustment is wrong.
    if capped < base_qty:
        logger.info(
            "[ADJ_QTY_CAP_SKIP] trade=%s current=%s raw=%s cap=%s "
            "reason=cap_would_shrink",
            trade_id if trade_id is not None else "?",
            base_qty,
            raw_qty,
            max_qty,
        )
        new_qty = base_qty
    else:
        new_qty = capped
    logger.info(
        "[ADJ_QTY_DYNAMIC] trade=%s | hedge_theta=%.4f | mult=%.1f | "
        "call_ask=%.2f | raw_pct=%.2f | raw_qty=%d | cap=%d | new_qty=%d",
        trade_id if trade_id is not None else "?",
        float(hedge_call_theta),
        mult,
        float(new_strike_ask),
        float(raw_pct),
        raw_qty,
        max_qty,
        new_qty,
    )
    return new_qty, False


def resolve_strangle_target_premium(
    *,
    settings: Any,
    hedge_call_mark: float | None,
    hedge_put_mark: float | None,
) -> tuple[float, bool]:
    """
    Resolve strangle target premium per side.

    fixed mode → settings.target_premium_per_side
    pct_of_hedge → ceil(avg(call_mark, put_mark) × pct / 100), marks required

    Returns (target_premium_per_side, used_dynamic).
    """
    fixed = float(getattr(settings, "target_premium_per_side", None) or 150.0)
    mode = str(
        getattr(settings, "strangle_premium_mode", None) or "fixed"
    ).lower().strip()
    if mode != "pct_of_hedge":
        return fixed, False

    from backend.core.bot_logger import log_and_buffer

    def _fallback(reason: str, **extra: Any) -> tuple[float, bool]:
        payload: dict[str, Any] = {
            "level": "WARNING",
            "reason": reason,
            "using_fixed": round(fixed, 4),
            "summary": (
                f"[STRANGLE_PREMIUM_FALLBACK] reason={reason} "
                f"using_fixed={round(fixed, 4)}"
            ),
        }
        payload.update(extra)
        log_and_buffer("STRANGLE_PREMIUM_FALLBACK", 0, payload)
        logger.warning(payload["summary"])
        return fixed, False

    if not bool(getattr(settings, "hedge_enabled", False)):
        return _fallback("hedge_disabled")

    try:
        call_m = float(hedge_call_mark) if hedge_call_mark is not None else 0.0
        put_m = float(hedge_put_mark) if hedge_put_mark is not None else 0.0
    except (TypeError, ValueError):
        call_m = 0.0
        put_m = 0.0

    if call_m <= 0 or put_m <= 0:
        return _fallback(
            "marks_unavailable",
            call_mark=call_m if call_m > 0 else None,
            put_mark=put_m if put_m > 0 else None,
        )

    pct = float(getattr(settings, "strangle_premium_pct_of_hedge", None) or 3.0)
    avg = (call_m + put_m) / 2.0
    computed = int(math.ceil(avg * pct / 100.0))
    if computed <= 0:
        return _fallback(
            "computed_non_positive",
            call_mark=round(call_m, 4),
            put_mark=round(put_m, 4),
            avg=round(avg, 4),
            pct=round(pct, 4),
            computed=computed,
        )

    logger.info(
        "[STRANGLE_PREMIUM_DYNAMIC] call_mark=%.2f put_mark=%.2f "
        "avg=%.2f pct=%.2f target=%d",
        call_m,
        put_m,
        avg,
        pct,
        computed,
    )
    return float(computed), True


# Re-export for adjustment path and tests
__all__ = [
    "blend_entry_premium",
    "compute_dynamic_basket_qty_pct",
    "resolve_adjustment_basket_qty",
    "resolve_basket_qty_from_hedge",
    "resolve_entry_basket_pct",
    "resolve_sizing_mode",
    "resolve_strangle_target_premium",
]


def resolve_sizing_mode(settings: Any) -> str:
    """
    Return 'pct_of_hedge' only when mode, hedge_enabled, and hedge_qty_lots
    are all valid; otherwise 'fixed' (with a BASKET_SIZING warning when
    pct_of_hedge was requested but prerequisites are missing).
    """
    from backend.core.bot_logger import log_and_buffer

    requested = str(
        getattr(settings, "basket_qty_mode", None) or "fixed"
    ).lower().strip()
    if requested != "pct_of_hedge":
        return "fixed"

    hedge_enabled = bool(getattr(settings, "hedge_enabled", False))
    raw_lots = getattr(settings, "hedge_qty_lots", None)
    try:
        hedge_qty_lots = int(raw_lots) if raw_lots is not None else None
    except (TypeError, ValueError):
        hedge_qty_lots = None

    if not hedge_enabled:
        log_and_buffer(
            "BASKET_SIZING",
            0,
            {
                "level": "WARNING",
                "reason": "hedge_disabled",
                "requested_mode": "pct_of_hedge",
                "resolved_mode": "fixed",
            },
        )
        return "fixed"

    if hedge_qty_lots is None or hedge_qty_lots <= 0:
        log_and_buffer(
            "BASKET_SIZING",
            0,
            {
                "level": "WARNING",
                "reason": "hedge_qty_lots_missing",
                "requested_mode": "pct_of_hedge",
                "resolved_mode": "fixed",
                "hedge_qty_lots": raw_lots,
            },
        )
        return "fixed"

    return "pct_of_hedge"


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
        from backend.engine.wing_entry import (
            EntryGuardBlock,
            EntryPartialUnwind,
            unwind_partial_entry,
        )

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

        # Phase 1: Hedge BEFORE wings/shorts so pct_of_hedge qty + strangle
        # premium can read an active hedge (avoids no_active_hedge deadlock).
        hedge_position_id: int | None = None
        hedge_enabled_for_entry = bool(getattr(settings, "hedge_enabled", False))
        if hedge_enabled_for_entry:
            from backend.engine.hedge_lifecycle import get_pending_close_hedge
            from backend.strategies.s001_short_strangle.logic import (
                log_sequence_step,
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

            log_sequence_step(
                trade_id=0,
                action="entry_phase_start",
                phase="hedge",
                position=1,
                underlying=str(underlying),
            )
            hedge_position_id = await self._hedge_entry_gate(
                settings=settings,
                db=db,
                account=account,
                client=client,
                underlying=underlying,
            )
            if hedge_position_id is None:
                log_sequence_step(
                    trade_id=0,
                    action="entry_phase_failed",
                    phase="hedge",
                    position=1,
                    underlying=str(underlying),
                    note="entry_aborted_before_wings_shorts",
                )
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    0,
                    {
                        "source": "auto",
                        "guard": "hedge_open_failed",
                        "underlying": underlying,
                        "note": (
                            "entry aborted — no wings or shorts placed"
                        ),
                    },
                )
                logger.critical(
                    "[ENTRY_SEQUENCE] hedge phase failed for %s — "
                    "aborting entry before wings/shorts",
                    underlying,
                )
                return
            log_sequence_step(
                trade_id=0,
                action="entry_phase_complete",
                phase="hedge",
                position=1,
                hedge_position_id=int(hedge_position_id),
            )

        entry_basket_qty: int | None = None
        entry_computed_pct: float | None = None
        strangle_premium_computed: float | None = None
        sizing_mode_for_entry = resolve_sizing_mode(settings)
        theta_info: dict[str, Any] | None = None
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
                        log_phase="entry",
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
                hedge_call_mark: float | None = None
                hedge_put_mark: float | None = None
                if hedge_on:
                    from backend.core.hedge_theta import get_hedge_theta
                    from backend.engine.hedge_lifecycle import get_active_hedge

                    hedge_for_prem = get_active_hedge(
                        db,
                        account_id=int(account.id),
                        underlying=str(settings.underlying),
                    )
                    if hedge_for_prem is not None:
                        try:
                            theta_prem = await get_hedge_theta(
                                client, hedge_for_prem
                            )
                            hedge_call_mark = float(
                                theta_prem.get("call_ask") or 0
                            )
                            hedge_put_mark = float(
                                theta_prem.get("put_ask") or 0
                            )
                        except Exception as exc:
                            logger.warning(
                                "strangle premium: get_hedge_theta failed: %s",
                                exc,
                            )

                target_prem, used_dynamic_prem = resolve_strangle_target_premium(
                    settings=settings,
                    hedge_call_mark=hedge_call_mark,
                    hedge_put_mark=hedge_put_mark,
                )
                if used_dynamic_prem:
                    strangle_premium_computed = float(target_prem)
                logger.info(
                    "Auto trade: STRANGLE mode target_premium=$%.2f dynamic=%s",
                    target_prem,
                    used_dynamic_prem,
                )
                straddle = await client.find_strangle_by_premium(
                    underlying=str(settings.underlying),
                    expiry_date=expiry_str,
                    target_premium=target_prem,
                )
            else:
                logger.info("Auto trade: STRADDLE mode (ATM)")
                tol = getattr(
                    settings, "entry_premium_match_tolerance_pct", None
                )
                straddle = await client.find_atm_straddle(
                    str(settings.underlying),
                    expiry_str,
                    tolerance_pct=float(tol) if tol is not None else None,
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

            pct = float(
                getattr(settings, "basket_qty_pct_of_hedge", None) or 20.0
            )
            if sizing_mode_for_entry == "pct_of_hedge":
                from backend.core.bot_logger import log_and_buffer
                from backend.engine.hedge_lifecycle import get_active_hedge

                hedge_row = get_active_hedge(
                    db,
                    account_id=int(account.id),
                    underlying=str(underlying),
                )
                if hedge_row is None:
                    log_and_buffer(
                        "ENTRY_GUARD_BLOCK",
                        0,
                        {
                            "source": "auto",
                            "guard": "no_active_hedge",
                            "underlying": underlying,
                            "sizing_mode": sizing_mode_for_entry,
                            "context": "basket_qty_resolution",
                        },
                    )
                    return
                hedge_qty_for_log = int(hedge_row.quantity)

                hedge_call_theta_val: float | None = None
                if theta_info is not None:
                    hedge_call_theta_val = float(theta_info.get("call_theta") or 0)
                elif bool(getattr(settings, "basket_qty_dynamic", False)):
                    from backend.core.hedge_theta import get_hedge_theta

                    try:
                        fetched_theta = await get_hedge_theta(client, hedge_row)
                        hedge_call_theta_val = float(
                            fetched_theta.get("call_theta") or 0
                        )
                    except Exception as exc:
                        logger.warning(
                            "Dynamic basket pct: get_hedge_theta failed: %s",
                            exc,
                        )
                        hedge_call_theta_val = 0.0

                pct, entry_computed_pct, dynamic_requested = resolve_entry_basket_pct(
                    settings,
                    straddle=straddle,
                    hedge_call_theta=hedge_call_theta_val,
                    sizing_mode=sizing_mode_for_entry,
                )
                qty = resolve_basket_qty_from_hedge(hedge_qty_for_log, pct)
                qty_source = "hedge_row"

                if dynamic_requested:
                    theta_mult = float(
                        getattr(settings, "basket_qty_theta_mult", None) or 2.0
                    )
                    call_ask = float(straddle.get("call_premium") or 0)
                    log_payload: dict[str, Any] = {
                        "sizing_mode": sizing_mode_for_entry,
                        "dynamic": True,
                        "hedge_call_theta": (
                            round(float(hedge_call_theta_val or 0), 6)
                            if hedge_call_theta_val is not None
                            else None
                        ),
                        "theta_mult": round(theta_mult, 4),
                        "call_ask": round(call_ask, 6),
                        "computed_pct": (
                            round(float(entry_computed_pct), 6)
                            if entry_computed_pct is not None
                            else None
                        ),
                        "manual_pct_fallback": (
                            round(pct, 6)
                            if entry_computed_pct is None
                            else None
                        ),
                        "hedge_qty": hedge_qty_for_log,
                        "basket_qty": qty,
                        "summary": (
                            f"[BASKET_SIZING] sizing_mode=pct_of_hedge | "
                            f"dynamic=True | "
                            f"hedge_call_theta={round(float(hedge_call_theta_val or 0), 6)} | "
                            f"theta_mult={round(theta_mult, 4)} | "
                            f"call_ask={round(call_ask, 6)} | "
                            f"computed_pct={round(float(entry_computed_pct), 6) if entry_computed_pct is not None else 'fallback'} | "
                            f"hedge_qty={hedge_qty_for_log} | basket_qty={qty}"
                        ),
                    }
                    if entry_computed_pct is None:
                        log_payload["level"] = "WARNING"
                        log_payload["reason"] = (
                            "dynamic_pct_unavailable"
                            if (call_ask <= 0 or float(hedge_call_theta_val or 0) == 0)
                            else "dynamic_pct_invalid"
                        )
                    log_and_buffer("BASKET_SIZING", 0, log_payload)
            else:
                qty = max(1, int(settings.quantity))
                hedge_qty_for_log = None
                qty_source = "settings"

            if qty <= 0:
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    0,
                    {
                        "source": "auto",
                        "guard": "basket_qty_zero",
                        "sizing_mode": sizing_mode_for_entry,
                        "hedge_qty": hedge_qty_for_log,
                        "pct": pct,
                        "underlying": underlying,
                    },
                )
                logger.warning(
                    "Auto trade BLOCKED: basket_qty=0 (sizing_mode=%s hedge_qty=%s pct=%s)",
                    sizing_mode_for_entry,
                    hedge_qty_for_log,
                    pct,
                )
                return

            entry_basket_qty = qty
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

            # --- Wings strike resolve + entry order sequencing ---
            from backend.core.bot_logger import log_and_buffer
            from backend.engine.wing_entry import (
                EntryGuardBlock,
                EntryPartialUnwind,
                FilledEntryLeg,
                build_entry_order_plan,
                is_full_fill,
                place_leg_with_retries,
                unwind_partial_entry,
            )

            wings_enabled = bool(
                getattr(settings, "basket_wings_enabled", False)
            )
            wing_call_pick: dict[str, Any] | None = None
            wing_put_pick: dict[str, Any] | None = None
            wing_call_fill = 0.0
            wing_put_fill = 0.0
            wing_call_order_id: str | None = None
            wing_put_order_id: str | None = None
            wing_call_fee: float | None = None
            wing_put_fee: float | None = None
            wing_call_open_ts = None
            wing_put_open_ts = None
            wing_call_fill_ts = None
            wing_put_fill_ts = None
            wing_call_mark = 0.0
            wing_put_mark = 0.0
            filled_entry_legs: list[FilledEntryLeg] = []

            if wings_enabled:
                from backend.strategies.s001_short_strangle.wing_select import (
                    resolve_wing_strikes,
                )

                chain_for_wings = locals().get("short_chain") or None
                if not chain_for_wings:
                    und_key = str(settings.underlying).upper().strip()
                    chain_for_wings = await client.get_option_chain(
                        und_key, expiry_str
                    )
                wing_call_pick, wing_put_pick = resolve_wing_strikes(
                    chain=chain_for_wings or [],
                    short_call_strike=float(
                        straddle.get("call_strike", straddle.get("strike"))
                    ),
                    short_put_strike=float(
                        straddle.get("put_strike", straddle.get("strike"))
                    ),
                    short_call_premium=call_mark,
                    short_put_premium=put_mark,
                    mode=str(
                        getattr(settings, "wing_strike_mode", None) or "points"
                    ),
                    points_away=float(
                        getattr(settings, "wing_points_away", None) or 2000.0
                    ),
                    delta_min=float(
                        getattr(settings, "wing_delta_min", None) or 0.05
                    ),
                    delta_max=float(
                        getattr(settings, "wing_delta_max", None) or 0.07
                    ),
                    pct_of_premium=float(
                        getattr(settings, "wing_pct_of_premium", None) or 20.0
                    ),
                )
                if wing_call_pick is None or wing_put_pick is None:
                    missing = "call" if wing_call_pick is None else "put"
                    log_and_buffer(
                        "ENTRY_GUARD_BLOCK",
                        0,
                        {
                            "source": "auto",
                            "guard": "no_wing_strike",
                            "leg": missing,
                            "underlying": underlying,
                        },
                    )
                    logger.error(
                        "[ENTRY_GUARD_BLOCK] guard=no_wing_strike leg=%s",
                        missing,
                    )
                    raise EntryGuardBlock("no_wing_strike", missing)

            try:
                plan = build_entry_order_plan(
                    qty=qty,
                    straddle=straddle,
                    wing_call=wing_call_pick,
                    wing_put=wing_put_pick,
                    wings_enabled=wings_enabled,
                    call_bracket_sl=call_prov_sl if call_prov_sl > 0 else None,
                    call_bracket_limit=(
                        call_prov_limit if call_prov_sl > 0 else None
                    ),
                    put_bracket_sl=put_prov_sl if put_prov_sl > 0 else None,
                    put_bracket_limit=(
                        put_prov_limit if put_prov_sl > 0 else None
                    ),
                )
            except EntryGuardBlock as guard_exc:
                log_and_buffer(
                    "ENTRY_GUARD_BLOCK",
                    0,
                    {
                        "source": "auto",
                        "guard": guard_exc.guard,
                        "leg": guard_exc.leg,
                        "underlying": underlying,
                    },
                )
                logger.error(
                    "[ENTRY_GUARD_BLOCK] guard=%s leg=%s",
                    guard_exc.guard,
                    guard_exc.leg,
                )
                raise

            call_result = None
            put_result = None
            call_fill = 0.0
            put_fill = 0.0
            call_order_id = None
            put_order_id = None
            call_fee = None
            put_fee = None
            call_open_ts = get_utc_now()
            put_open_ts = get_utc_now()
            call_fill_ts = call_open_ts
            put_fill_ts = put_open_ts
            call_sl_trigger_price = call_prov_sl
            call_sl_limit = call_prov_limit
            put_sl_trigger_price = put_prov_sl
            put_sl_limit = put_prov_limit

            from backend.engine.midprice_executor import (
                clamp_chase_max_seconds,
                clamp_hold_seconds,
                clamp_partner_window_seconds,
                execute_paired_legs,
                should_use_midprice,
            )
            import time as _time_mod

            mp_on = bool(getattr(settings, "midprice_enabled", False))
            chase_max = clamp_chase_max_seconds(
                getattr(settings, "midprice_chase_max_seconds", None)
            )
            hold_s = clamp_hold_seconds(
                getattr(settings, "midprice_hold_seconds", None)
            )
            partner_win_s = clamp_partner_window_seconds(
                getattr(settings, "midprice_partner_window_seconds", None)
            )
            entry_tol = float(
                getattr(settings, "entry_premium_match_tolerance_pct", None)
                or 15.0
            )
            entry_reason = (
                "CONDOR_ENTRY" if wings_enabled else "BASKET_ENTRY"
            )
            use_mp_entry = should_use_midprice(
                enabled=mp_on, reason=entry_reason
            )
            selection_ts = _time_mod.monotonic()

            from backend.strategies.s001_short_strangle.logic import (
                log_sequence_step,
            )

            # Role-based consecutive groups (never by bare index):
            # wings ON  -> [[wing_call, wing_put], [call, put]]
            # wings OFF -> [[call, put]]
            # Groups stay SEQUENTIAL (margin rule W2); pair only within a group.
            _wing_roles = frozenset({"wing_call", "wing_put"})
            entry_groups: list[tuple[str, list[Any]]] = []
            for _spec in plan:
                _gkey = (
                    "wing" if str(_spec.role) in _wing_roles else "short"
                )
                if not entry_groups or entry_groups[-1][0] != _gkey:
                    entry_groups.append((_gkey, [_spec]))
                else:
                    entry_groups[-1][1].append(_spec)

            for phase, group_specs in entry_groups:
                if phase == "wing":
                    log_sequence_step(
                        trade_id=0,
                        action="entry_phase_start",
                        phase="wing",
                        position=2 if hedge_enabled_for_entry else 1,
                        underlying=str(underlying),
                    )
                elif phase == "short":
                    log_sequence_step(
                        trade_id=0,
                        action="entry_phase_start",
                        phase="short",
                        position=3 if hedge_enabled_for_entry else 2,
                        underlying=str(underlying),
                    )

                for _spec in group_specs:
                    if not _spec.is_long:
                        logger.info(
                            "Placing %s: %s qty=%s bracket_sl=%s profile=%s",
                            _spec.role.upper(),
                            _spec.symbol,
                            _spec.quantity,
                            _spec.bracket_sl_price,
                            "paired_chase" if use_mp_entry else "market",
                        )
                    else:
                        logger.info(
                            "Placing WING %s BUY: %s qty=%s (no bracket SL) "
                            "profile=%s",
                            _spec.role,
                            _spec.symbol,
                            _spec.quantity,
                            "paired_chase" if use_mp_entry else "market",
                        )

                # (spec, result, open_ts, fill_ts) — bookkeeping unchanged below
                placements: list[
                    tuple[Any, Any, Any, Any]
                ] = []

                if use_mp_entry:
                    group_open_ts = get_utc_now()
                    pair_results = await execute_paired_legs(
                        legs=[
                            {
                                "product_id": int(s.product_id),
                                "side": "buy" if s.is_long else "sell",
                                "quantity": int(s.quantity),
                                "symbol": str(s.symbol),
                                "leg_label": str(s.role),
                                "selected_premium": float(
                                    s.mark_premium or 0
                                )
                                or None,
                                "bracket_sl_price": s.bracket_sl_price,
                                "bracket_sl_limit": s.bracket_sl_limit,
                            }
                            for s in group_specs
                        ],
                        delta_client=client,
                        reason=entry_reason,
                        midprice_enabled=True,
                        max_chase_seconds=chase_max,
                        hold_seconds=hold_s,
                        partner_window_seconds=partner_win_s,
                        entry_premium_match_tolerance_pct=entry_tol,
                        selection_ts=selection_ts,
                        phase=phase,
                    )
                    group_fill_ts = get_utc_now()
                    if len(pair_results) != len(group_specs):
                        raise EntryPartialUnwind(
                            f"Entry {phase} pair returned "
                            f"{len(pair_results)} results for "
                            f"{len(group_specs)} legs",
                            filled_legs=list(filled_entry_legs),
                            failed_role=str(group_specs[0].role),
                        )
                    for s, res in zip(group_specs, pair_results):
                        placements.append(
                            (s, res, group_open_ts, group_fill_ts)
                        )
                else:
                    # Market path — sequential per leg (unchanged behaviour)
                    for spec in group_specs:

                        async def _place_one(_spec=spec):
                            if _spec.is_long:
                                return await self.order_executor.buy_option(
                                    product_id=int(_spec.product_id),
                                    quantity=int(_spec.quantity),
                                    delta_client=client,
                                    symbol_for_fallback=str(_spec.symbol),
                                )
                            return await self.order_executor.sell_option(
                                product_id=int(_spec.product_id),
                                quantity=int(_spec.quantity),
                                delta_client=client,
                                symbol_for_fallback=str(_spec.symbol),
                                bracket_sl_price=_spec.bracket_sl_price,
                                bracket_sl_limit=_spec.bracket_sl_limit,
                            )

                        open_ts = get_utc_now()
                        result = await place_leg_with_retries(
                            role=spec.role,
                            requested=int(spec.quantity),
                            place_fn=_place_one,
                        )
                        fill_ts = get_utc_now()
                        placements.append((spec, result, open_ts, fill_ts))

                # Commit every leg in this group before the next group starts.
                for spec, result, open_ts, fill_ts in placements:
                    if not is_full_fill(result, int(spec.quantity)):
                        # Record any partial fill before raising unwind
                        partial_size = 0
                        if result.success:
                            try:
                                partial_size = int(
                                    getattr(result, "filled_size", 0) or 0
                                )
                            except (TypeError, ValueError):
                                partial_size = 0
                        if partial_size > 0:
                            filled_entry_legs.append(
                                FilledEntryLeg(
                                    role=spec.role,
                                    product_id=int(spec.product_id),
                                    symbol=str(spec.symbol),
                                    strike=float(spec.strike),
                                    requested_qty=int(spec.quantity),
                                    filled_size=partial_size,
                                    fill_price=float(
                                        result.filled_price
                                        or spec.mark_premium
                                    ),
                                    order_id=(
                                        str(result.order_id)
                                        if result.order_id is not None
                                        else None
                                    ),
                                    commission=(
                                        abs(float(result.commission))
                                        if result.commission is not None
                                        else None
                                    ),
                                    is_long=bool(spec.is_long),
                                    mark_premium=float(spec.mark_premium),
                                )
                            )
                        raise EntryPartialUnwind(
                            f"Entry leg {spec.role} incomplete after retries: "
                            f"{result.error or 'partial_fill'}",
                            filled_legs=list(filled_entry_legs),
                            failed_role=spec.role,
                        )

                    fill_px = float(result.filled_price or 0.0)
                    if fill_px <= 0:
                        fill_px = float(spec.mark_premium)
                    oid = (
                        str(result.order_id)
                        if result.order_id is not None
                        else None
                    )
                    fee = (
                        abs(float(result.commission))
                        if result.commission is not None
                        else None
                    )
                    filled_size = int(
                        getattr(result, "filled_size", None) or spec.quantity
                    )

                    filled_entry_legs.append(
                        FilledEntryLeg(
                            role=spec.role,
                            product_id=int(spec.product_id),
                            symbol=str(spec.symbol),
                            strike=float(spec.strike),
                            requested_qty=int(spec.quantity),
                            filled_size=filled_size,
                            fill_price=fill_px,
                            order_id=oid,
                            commission=fee,
                            is_long=bool(spec.is_long),
                            mark_premium=float(spec.mark_premium),
                        )
                    )

                    if spec.role == "wing_call":
                        wing_call_open_ts = open_ts
                        wing_call_fill_ts = fill_ts
                        wing_call_fill = fill_px
                        wing_call_order_id = oid
                        wing_call_fee = fee
                        wing_call_mark = float(spec.mark_premium)
                        logger.info(
                            "[WING_ENTRY] leg=call strike=%s qty=%s fill=%s "
                            "order_id=%s",
                            spec.strike,
                            spec.quantity,
                            fill_px,
                            oid,
                        )
                    elif spec.role == "wing_put":
                        wing_put_open_ts = open_ts
                        wing_put_fill_ts = fill_ts
                        wing_put_fill = fill_px
                        wing_put_order_id = oid
                        wing_put_fee = fee
                        wing_put_mark = float(spec.mark_premium)
                        logger.info(
                            "[WING_ENTRY] leg=put strike=%s qty=%s fill=%s "
                            "order_id=%s",
                            spec.strike,
                            spec.quantity,
                            fill_px,
                            oid,
                        )
                    elif spec.role == "call":
                        call_result = result
                        call_open_ts = open_ts
                        call_fill_ts = fill_ts
                        call_fill = fill_px
                        call_order_id = oid
                        call_fee = fee
                        logger.info(
                            "Call filled @ %s order_id=%s",
                            call_fill,
                            call_order_id,
                        )
                        call_sl_trigger_price, call_sl_limit = (
                            await finalize_bracket_sl_after_fill(
                                client,
                                entry_order_id=call_order_id,
                                product_id=int(spec.product_id),
                                mark_price=call_mark,
                                fill_price=call_fill,
                                universal_sl_pct=universal_sl_pct,
                                provisional_stop=call_prov_sl,
                                provisional_limit=call_prov_limit,
                                leg="call",
                                trade_id=None,
                            )
                        )
                        filled_entry_legs[-1].sl_trigger_price = (
                            float(call_sl_trigger_price)
                            if call_sl_trigger_price
                            else None
                        )
                    elif spec.role == "put":
                        put_result = result
                        put_open_ts = open_ts
                        put_fill_ts = fill_ts
                        put_fill = fill_px
                        put_order_id = oid
                        put_fee = fee
                        logger.info(
                            "Put filled @ %s order_id=%s",
                            put_fill,
                            put_order_id,
                        )
                        put_sl_trigger_price, put_sl_limit = (
                            await finalize_bracket_sl_after_fill(
                                client,
                                entry_order_id=put_order_id,
                                product_id=int(spec.product_id),
                                mark_price=put_mark,
                                fill_price=put_fill,
                                universal_sl_pct=universal_sl_pct,
                                provisional_stop=put_prov_sl,
                                provisional_limit=put_prov_limit,
                                leg="put",
                                trade_id=None,
                            )
                        )
                        filled_entry_legs[-1].sl_trigger_price = (
                            float(put_sl_trigger_price)
                            if put_sl_trigger_price
                            else None
                        )

            # Hedge already opened/reused in phase 1 (before wings/shorts).

            # TP/SL locked to initial deployment premium (actual fills)
            # initial_max_profit never changes after trade entry
            # adjustments do NOT affect TP/SL
            # Net credit when wings on: shorts credit − wings debit
            from backend.core.basket_legs import compute_net_credit_usd

            wing_premium_paid_usd = 0.0
            if (
                wings_enabled
                and wing_call_pick is not None
                and wing_put_pick is not None
            ):
                wing_premium_paid_usd = round(
                    (float(wing_call_fill) + float(wing_put_fill))
                    * qty
                    * float(OPTIONS_CONTRACT_VALUE),
                    6,
                )
            initial_max_profit = round(
                compute_net_credit_usd(
                    short_call_premium=float(call_fill),
                    short_put_premium=float(put_fill),
                    short_qty=qty,
                    wing_call_premium=float(wing_call_fill)
                    if wing_premium_paid_usd > 0
                    else 0.0,
                    wing_put_premium=float(wing_put_fill)
                    if wing_premium_paid_usd > 0
                    else 0.0,
                    wing_qty=qty,
                ),
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
                wing_call_fill=float(wing_call_fill)
                if wing_premium_paid_usd > 0
                else None,
                wing_put_fill=float(wing_put_fill)
                if wing_premium_paid_usd > 0
                else None,
                wing_call_fee=wing_call_fee if wing_premium_paid_usd > 0 else None,
                wing_put_fee=wing_put_fee if wing_premium_paid_usd > 0 else None,
                wing_call_symbol=(
                    str(wing_call_pick.get("symbol") or "")
                    if wing_call_pick is not None and wing_premium_paid_usd > 0
                    else None
                ),
                wing_put_symbol=(
                    str(wing_put_pick.get("symbol") or "")
                    if wing_put_pick is not None and wing_premium_paid_usd > 0
                    else None
                ),
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
                basket_qty_computed_pct=(
                    float(entry_computed_pct)
                    if entry_computed_pct is not None
                    else None
                ),
                strangle_premium_computed_usd=(
                    float(strangle_premium_computed)
                    if strangle_premium_computed is not None
                    else None
                ),
                original_basket_qty=int(qty),
                wing_premium_paid_usd=(
                    float(wing_premium_paid_usd)
                    if wing_premium_paid_usd > 0
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

            log_and_buffer(
                "BASKET_SIZING",
                int(trade.id),
                {
                    "sizing_mode": sizing_mode_for_entry,
                    "hedge_qty": hedge_qty_for_log,
                    "pct": pct,
                    "basket_qty": qty,
                    "source": qty_source,
                },
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
                is_long=False,
                side="SELL",
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
                is_long=False,
                side="SELL",
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

            wing_call_leg = None
            wing_put_leg = None
            if wings_enabled and wing_call_pick is not None and wing_put_pick is not None:
                wing_call_entry_spread = compute_entry_spread_usd(
                    sent_price=wing_call_mark,
                    fill_price=wing_call_fill,
                    quantity=qty,
                    is_long=True,
                )
                wing_put_entry_spread = compute_entry_spread_usd(
                    sent_price=wing_put_mark,
                    fill_price=wing_put_fill,
                    quantity=qty,
                    is_long=True,
                )
                wing_call_leg = Leg(
                    trade_id=trade.id,
                    leg_type="wing_call",
                    strike=float(wing_call_pick["strike"]),
                    symbol=str(wing_call_pick["symbol"]),
                    product_id=int(wing_call_pick["product_id"]),
                    initial_premium=wing_call_fill,
                    trigger_baseline_premium=wing_call_fill,
                    trigger_premium=wing_call_fill,
                    quantity=qty,
                    entry_time=now_utc,
                    status="open",
                    is_bot_managed=True,
                    is_long=True,
                    side="BUY",
                    delta_order_id=wing_call_order_id,
                    delta_at_entry=float(wing_call_pick.get("delta") or 0),
                    entry_fee_usd=wing_call_fee,
                    order_sent_price=wing_call_mark,
                    entry_spread_usd=wing_call_entry_spread,
                )
                wing_put_leg = Leg(
                    trade_id=trade.id,
                    leg_type="wing_put",
                    strike=float(wing_put_pick["strike"]),
                    symbol=str(wing_put_pick["symbol"]),
                    product_id=int(wing_put_pick["product_id"]),
                    initial_premium=wing_put_fill,
                    trigger_baseline_premium=wing_put_fill,
                    trigger_premium=wing_put_fill,
                    quantity=qty,
                    entry_time=now_utc,
                    status="open",
                    is_bot_managed=True,
                    is_long=True,
                    side="BUY",
                    delta_order_id=wing_put_order_id,
                    delta_at_entry=float(wing_put_pick.get("delta") or 0),
                    entry_fee_usd=wing_put_fee,
                    order_sent_price=wing_put_mark,
                    entry_spread_usd=wing_put_entry_spread,
                )
                db.add(wing_call_leg)
                db.add(wing_put_leg)
                # All four legs' entry spreads feed SL add-back
                reset_entry_spread_for_sl(
                    trade,
                    abs(float(call_entry_spread or 0.0))
                    + abs(float(put_entry_spread or 0.0))
                    + abs(float(wing_call_entry_spread or 0.0))
                    + abs(float(wing_put_entry_spread or 0.0)),
                    reason="trade_entry_with_wings",
                    leg="call+put+wings",
                )

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
            if wing_call_leg is not None:
                db.refresh(wing_call_leg)
            if wing_put_leg is not None:
                db.refresh(wing_put_leg)

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
                    wing_call_leg=wing_call_leg,
                    wing_put_leg=wing_put_leg,
                    wing_call_opened_at=wing_call_open_ts,
                    wing_put_opened_at=wing_put_open_ts,
                    wing_call_fill_at=wing_call_fill_ts,
                    wing_put_fill_at=wing_put_fill_ts,
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
            expected_open = 4 if (
                wings_enabled
                and wing_call_leg is not None
                and wing_put_leg is not None
            ) else 2
            if len(saved_legs) != expected_open:
                logger.critical(
                    "Auto trade %s has %s open legs (expected %s)! "
                    "DB save may be incomplete.",
                    trade.id,
                    len(saved_legs),
                    expected_open,
                )
            else:
                logger.info(
                    "Auto trade %s has %s open legs in DB",
                    trade.id,
                    expected_open,
                )

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
                            wing_call_product_id=(
                                int(wing_call_pick["product_id"])
                                if wings_enabled
                                and wing_call_pick is not None
                                else None
                            ),
                            wing_put_product_id=(
                                int(wing_put_pick["product_id"])
                                if wings_enabled
                                and wing_put_pick is not None
                                else None
                            ),
                            wing_call_strike=(
                                float(wing_call_pick["strike"])
                                if wings_enabled
                                and wing_call_pick is not None
                                else None
                            ),
                            wing_put_strike=(
                                float(wing_put_pick["strike"])
                                if wings_enabled
                                and wing_put_pick is not None
                                else None
                            ),
                            wing_call_symbol=(
                                str(wing_call_pick.get("symbol") or "")
                                if wings_enabled
                                and wing_call_pick is not None
                                else None
                            ),
                            wing_put_symbol=(
                                str(wing_put_pick.get("symbol") or "")
                                if wings_enabled
                                and wing_put_pick is not None
                                else None
                            ),
                            wing_call_fill=(
                                float(wing_call_fill)
                                if wings_enabled and wing_call_fill
                                else None
                            ),
                            wing_put_fill=(
                                float(wing_put_fill)
                                if wings_enabled and wing_put_fill
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

        except EntryGuardBlock as guard_exc:
            logger.error(
                "Auto trade entry blocked by guard: %s",
                guard_exc,
            )
            try:
                db.rollback()
            except Exception:
                pass
            await self._record_failure(settings, db, str(guard_exc))

        except EntryPartialUnwind as unwind_exc:
            logger.critical(
                "ENTRY PARTIAL — unwinding filled legs (failed=%s): %s",
                unwind_exc.failed_role,
                unwind_exc,
            )
            unwind_res = None
            closed_trade_id = None
            note = None
            try:
                if client is not None and unwind_exc.filled_legs:
                    unwind_res = await unwind_partial_entry(
                        order_executor=self.order_executor,
                        delta_client=client,
                        filled_legs=list(unwind_exc.filled_legs),
                        trade_id=None,
                    )
                # Persist a CLOSED trade for audit + cooldown
                from backend.config import ExitReason as _ER
                from backend.database import get_or_create_auto_settings

                now_u = get_utc_now()
                note = None
                if unwind_res is not None and unwind_res.legs_failed > 0:
                    note = (
                        "PARTIAL_UNWIND_FAILED: "
                        + "; ".join(unwind_res.failures[:5])
                    )
                    logger.critical(
                        "PARTIAL_UNWIND_FAILED trade pending: %s",
                        note,
                    )
                closed = Trade(
                    account_id=int(account.id) if account is not None else 0,
                    underlying=str(
                        getattr(settings, "underlying", "BTC") or "BTC"
                    ).upper(),
                    expiry_date=expiry_date if "expiry_date" in locals() else (
                        get_ist_now().date()
                    ),
                    status=TradeStatus.CLOSED.value,
                    entry_time=now_u,
                    exit_time=now_u,
                    total_premium_collected=0.0,
                    profit_target_usd=0.0,
                    stoploss_usd=0.0,
                    trigger_mode=str(
                        getattr(settings, "trigger_mode", None) or "slab"
                    ),
                    realized_pnl=0.0,
                    exit_reason=_ER.ENTRY_PARTIAL_UNWIND.value,
                    notes=note or "ENTRY_PARTIAL_UNWIND",
                    hedge_position_id=(
                        int(hedge_position_id)
                        if "hedge_position_id" in locals()
                        and hedge_position_id is not None
                        else None
                    ),
                )
                db.add(closed)
                db.commit()
                db.refresh(closed)
                closed_trade_id = int(closed.id)
                logger.warning(
                    "[ENTRY_PARTIAL_UNWIND] trade=%s legs_closed=%s "
                    "legs_failed=%s",
                    closed_trade_id,
                    unwind_res.legs_closed if unwind_res else 0,
                    unwind_res.legs_failed if unwind_res else 0,
                )
                # Cooldown — do not instant re-enter
                cooldown = int(
                    getattr(settings, "cooldown_after_loss_minutes", None)
                    if getattr(settings, "cooldown_after_loss_minutes", None)
                    is not None
                    else 120
                )
                self.schedule_reentry(
                    str(getattr(settings, "underlying", "") or ""),
                    cooldown,
                    source="cooldown_after_loss",
                )
                await ws_manager.broadcast(
                    {
                        "type": "ENTRY_PARTIAL_UNWIND",
                        "trade_id": closed_trade_id,
                        "exit_reason": _ER.ENTRY_PARTIAL_UNWIND.value,
                        "legs_closed": (
                            unwind_res.legs_closed if unwind_res else 0
                        ),
                        "legs_failed": (
                            unwind_res.legs_failed if unwind_res else 0
                        ),
                        "partial_unwind_failed": bool(
                            note and "PARTIAL_UNWIND_FAILED" in note
                        ),
                        "message": (
                            f"Entry partial unwind — trade {closed_trade_id} "
                            f"closed ({_ER.ENTRY_PARTIAL_UNWIND.value})"
                            + (
                                " — PARTIAL_UNWIND_FAILED, check positions!"
                                if note and "PARTIAL_UNWIND_FAILED" in note
                                else ""
                            )
                        ),
                    }
                )
            except Exception as persist_exc:
                logger.critical(
                    "ENTRY_PARTIAL_UNWIND persist/schedule failed: %s",
                    persist_exc,
                    exc_info=True,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                await self._record_failure(settings, db, str(unwind_exc))
            else:
                # Already scheduled cooldown — skip short retry
                try:
                    settings_row = get_or_create_auto_settings(db)
                    settings_row.last_error = str(unwind_exc)[:500]
                    settings_row.updated_at = get_utc_now()
                    db.commit()
                except Exception:
                    try:
                        db.rollback()
                    except Exception:
                        pass

        except Exception as exc:
            # Import may not be in scope if failure before wing imports
            from backend.engine.wing_entry import (
                EntryGuardBlock as _EGB,
                EntryPartialUnwind as _EPU,
            )

            if isinstance(exc, (_EGB, _EPU)):
                raise  # should have been caught above
            logger.error("Auto trade placement failed: %s", exc, exc_info=True)

            # Prefer filled_entry_legs unwind when available (wings path)
            if (
                "filled_entry_legs" in locals()
                and filled_entry_legs
                and client is not None
            ):
                try:
                    await unwind_partial_entry(
                        order_executor=self.order_executor,
                        delta_client=client,
                        filled_legs=list(filled_entry_legs),
                        trade_id=None,
                    )
                except Exception as cleanup_exc:
                    logger.critical(
                        "PARTIAL ENTRY CLEANUP (filled_entry_legs) FAILED: %s",
                        cleanup_exc,
                        exc_info=True,
                    )
            # Legacy: If call was placed but put failed, close the call
            elif (
                "call_result" in locals()
                and call_result is not None
                and getattr(call_result, "success", False)
                and "straddle" in locals()
                and client is not None
            ):
                if "put_result" not in locals() or not getattr(
                    put_result, "success", False
                ):
                    logger.critical(
                        "PARTIAL ENTRY: Call placed but Put failed. "
                        "Attempting to close call order to avoid naked exposure."
                    )
                    try:
                        call_pid = int(straddle["call_product_id"])
                        call_qty = int(entry_basket_qty or 0)
                        if call_qty <= 0:
                            raise RuntimeError(
                                "partial entry cleanup: entry_basket_qty missing"
                            )
                        close_res = await client.close_position(
                            product_id=call_pid,
                            size=call_qty,
                            is_long=False,
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
            str(getattr(settings, "basket_qty_mode", "fixed") or "fixed"),
            float(getattr(settings, "basket_qty_pct_of_hedge", 20.0) or 20.0),
            (
                int(getattr(settings, "hedge_qty_lots"))
                if getattr(settings, "hedge_qty_lots", None) is not None
                else None
            ),
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
        Ensure an active long hedge exists. Called FIRST in entry sequence
        (Hedge → Wings → Shorts) so pct_of_hedge sizing and strangle premium
        can read live hedge marks before any basket orders.

        Returns hedge_positions.id on success, or None if hedge open failed
        (caller must abort entry — no wings/shorts without an active hedge
        when hedge_enabled).
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

        sizing_mode = resolve_sizing_mode(settings)
        pct = float(getattr(settings, "basket_qty_pct_of_hedge", None) or 20.0)

        if sizing_mode == "pct_of_hedge":
            hedge_qty = max(1, int(settings.hedge_qty_lots))
            basket_qty = resolve_basket_qty_from_hedge(hedge_qty, pct)
            ratio = None
        else:
            basket_qty = max(1, int(settings.quantity or 1))
            try:
                ratio = float(getattr(settings, "hedge_qty_ratio", None) or 1.0)
            except (TypeError, ValueError):
                ratio = 1.0
            if ratio <= 0:
                ratio = 1.0
            hedge_qty = max(1, int(round(basket_qty * ratio)))

        gate_payload: dict[str, Any] = {
            "hedge_enabled": True,
            "existing_hedge_id": None,
            "action": "open",
            "underlying": underlying,
            "basket_qty": basket_qty,
            "hedge_qty": hedge_qty,
            "sizing_mode": sizing_mode,
        }
        if ratio is not None:
            gate_payload["hedge_qty_ratio"] = ratio
        if sizing_mode == "pct_of_hedge":
            gate_payload["basket_qty_pct_of_hedge"] = pct
            gate_payload["hedge_qty_lots"] = int(settings.hedge_qty_lots)

        log_and_buffer(
            "HEDGE_GATE",
            0,
            gate_payload,
        )
        if sizing_mode == "pct_of_hedge":
            logger.info(
                "HEDGE_GATE: no active hedge for %s — opening hedge qty=%s "
                "(sizing_mode=pct_of_hedge hedge_qty_lots=%s basket_qty=%s "
                "pct=%.2f)",
                underlying,
                hedge_qty,
                int(settings.hedge_qty_lots),
                basket_qty,
                pct,
            )
        else:
            logger.info(
                "HEDGE_GATE: no active hedge for %s — opening hedge qty=%s "
                "(basket_qty=%s × ratio=%.2f sizing_mode=fixed)",
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
                "[HEDGE_GATE_BLOCK] hedge open failed after basket placed. %s",
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
                "[HEDGE_GATE_BLOCK] unexpected hedge open error after basket "
                "placed: %s",
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
