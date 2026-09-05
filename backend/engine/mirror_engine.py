# mirror_engine.py — Replicate master trade actions onto active slave accounts

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaClient
from backend.core.encryption import decrypt
from backend.core.fees import compute_entry_spread_usd
from backend.core.time_utils import get_utc_now
from backend.database import SessionLocal, get_active_slave_accounts
from backend.models import SlaveAccount, SlaveHedgePosition, SlaveTrade, Trade
from backend.config import MAX_SLAVE_QTY, OPTIONS_CONTRACT_VALUE, ExitReason, TradeStatus

logger = logging.getLogger(__name__)

# Same debit buffer as master hedge open (hedge_lifecycle.HEDGE_AFFORD_BUFFER)
_HEDGE_AFFORD_BUFFER = 1.15
_CONTRACT_SIZE = float(OPTIONS_CONTRACT_VALUE)
_HEDGE_VERIFY_PAUSE_SECONDS = 2.0

# Earner/billing force-close — closes one slave structure on cancellation
FORCE_CLOSE_REASONS = frozenset(
    {
        "SUBSCRIPTION_CANCELLED",
        "API_DISCONNECTED",
        "ADMIN_FORCE",
    }
)

# Per-slave mutating-op lock acquire timeout (skip slave, do not block engine)
_SLAVE_LOCK_TIMEOUT_S = 120.0


def is_virtual_slave_trade(
    slave: SlaveAccount | None,
    slave_trade: SlaveTrade | None,
) -> bool:
    """
    True when this SlaveTrade must not be closed by integrity / auto-cleanup.

    Matches either the slave account flag or VIRTUAL paper order IDs.
    """
    if slave is not None and bool(getattr(slave, "is_virtual", False)):
        return True
    if slave_trade is None:
        return False
    call_oid = str(getattr(slave_trade, "call_order_id", None) or "").upper()
    put_oid = str(getattr(slave_trade, "put_order_id", None) or "").upper()
    return call_oid == "VIRTUAL" or put_oid == "VIRTUAL"


class MirrorEngine:
    """
    Mirror master account actions (entry / adjustment / exit) onto all
    active slave Delta accounts, scaled by each slave's qty_multiplier.

    Failures on one slave are non-fatal — remaining slaves still run.

    Concurrency: one asyncio.Lock per slave serialises ALL mutating mirror
    ops (entry, adjustment, exit, leg close, hedge open/close, sweeps,
    conversion). That lock is process-local only — it does NOT protect
    across uvicorn workers, processes, or restarts. Safe while the app
    runs with a single worker (uvicorn --workers 1). Raising worker count
    without an external lock would silently break this invariant.
    """

    def __init__(self, db_factory: Callable[[], Any] | None = None) -> None:
        self.db_factory = db_factory or SessionLocal
        # Per-slave lock for every mutating mirror operation (not hedge-only).
        # Process-local asyncio.Lock — see class docstring.
        self._slave_locks: dict[int, asyncio.Lock] = {}
        # SlaveTrade ids that already logged [SLAVE_MTM_FALLBACK] this process
        self._slave_mtm_fallback_logged: set[int] = set()
        # Last SLAVE_SIZING_ZERO reason (carried into SlaveTrade.last_error)
        self._last_sizing_zero_reason: str = ""

    def _get_slave_lock(self, slave_id: int) -> asyncio.Lock:
        sid = int(slave_id)
        lock = self._slave_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._slave_locks[sid] = lock
        return lock

    @asynccontextmanager
    async def _slave_op_lock(
        self, slave_id: int, op: str
    ) -> AsyncIterator[bool]:
        """
        Acquire the per-slave mutating-op lock (timeout → yield False).

        Top-level public entry points only. Nested helpers must NOT call this —
        asyncio.Lock is not re-entrant. Yields True if held, False on timeout
        (caller skips this slave for the cycle).

        Process-local only (uvicorn --workers 1). Not multi-worker safe.
        """
        sid = int(slave_id)
        lock = self._get_slave_lock(sid)
        try:
            await asyncio.wait_for(
                lock.acquire(), timeout=_SLAVE_LOCK_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            log_and_buffer(
                "SLAVE_LOCK_TIMEOUT",
                0,
                {
                    "slave": sid,
                    "op": str(op),
                    "waited": float(_SLAVE_LOCK_TIMEOUT_S),
                },
            )
            yield False
            return
        try:
            yield True
        finally:
            lock.release()

    @staticmethod
    def _slave_hedge_status_is_alive(status: str | None) -> bool:
        """True when slave hedge still protects baskets (matches master sweep)."""
        s = str(status or "").lower().strip()
        return s in {"active", "pending_close"}

    def _resolve_master_hedge_id_for_trade(
        self, db: Any, master_trade_id: int
    ) -> int | None:
        """
        Current master hedge for a basket: trade.hedge_position_id, else any
        alive master HedgePosition (hedge mode with unstamped trade).
        """
        from backend.models import HedgePosition

        trade = (
            db.query(Trade)
            .filter(Trade.id == int(master_trade_id))
            .first()
        )
        if trade is not None and getattr(trade, "hedge_position_id", None):
            return int(trade.hedge_position_id)
        alive = (
            db.query(HedgePosition)
            .filter(HedgePosition.status.in_(("active", "pending_close")))
            .order_by(HedgePosition.id.desc())
            .first()
        )
        if alive is not None:
            return int(alive.id)
        return None

    def _get_alive_slave_hedge(
        self,
        db: Any,
        *,
        slave_id: int,
        master_hedge_id: int,
    ) -> SlaveHedgePosition | None:
        return (
            db.query(SlaveHedgePosition)
            .filter(
                SlaveHedgePosition.slave_account_id == int(slave_id),
                SlaveHedgePosition.master_hedge_id == int(master_hedge_id),
                SlaveHedgePosition.status.in_(("active", "pending_close")),
            )
            .first()
        )

    def _assert_hedge_before_basket(
        self,
        db: Any,
        slave_id: int,
        master_hedge_id: int | None,
    ) -> tuple[bool, str]:
        """
        Hard invariant: never open a short basket without a live slave hedge.

        Returns (ok, reason) where reason is no_master_hedge | no_slave_hedge | ok.
        """
        if master_hedge_id is None:
            return False, "no_master_hedge"
        alive = self._get_alive_slave_hedge(
            db,
            slave_id=int(slave_id),
            master_hedge_id=int(master_hedge_id),
        )
        if alive is None:
            return False, "no_slave_hedge"
        return True, "ok"

    def _skip_basket_no_hedge(
        self,
        db: Any,
        *,
        slave: SlaveAccount,
        master_trade_id: int,
        master_hedge_id: int | None,
        reason: str,
    ) -> None:
        """Persist skipped_no_hedge row + SLAVE_NO_HEDGE audit log."""
        log_and_buffer(
            "SLAVE_NO_HEDGE",
            int(master_trade_id),
            {
                "slave": int(slave.id),
                "master_trade": int(master_trade_id),
                "master_hedge": master_hedge_id,
                "reason": str(reason),
                "note": "basket_entry_skipped",
            },
        )
        logger.warning(
            "[SLAVE_NO_HEDGE] slave=%s master_trade=%s reason=%s "
            "basket_entry_skipped",
            slave.id,
            master_trade_id,
            reason,
        )
        skip_trade = SlaveTrade(
            slave_account_id=int(slave.id),
            master_trade_id=int(master_trade_id),
            actual_quantity=0,
            status="skipped_no_hedge",
            last_error=(
                f"{reason} master_hedge={master_hedge_id}"
            )[:500],
            error_count=0,
        )
        db.add(skip_trade)
        db.commit()

    def _structure_hedge_pids_for_slave(
        self, db: Any, slave_id: int
    ) -> set[int]:
        """Product ids of structure hedges that must survive basket exits."""
        rows = (
            db.query(SlaveHedgePosition)
            .filter(
                SlaveHedgePosition.slave_account_id == int(slave_id),
                SlaveHedgePosition.status.in_(
                    ("active", "pending_close", "partial", "exit_failed", "error")
                ),
            )
            .all()
        )
        pids: set[int] = set()
        for row in rows:
            for attr in ("call_product_id", "put_product_id"):
                try:
                    pid = int(getattr(row, attr, 0) or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid > 0:
                    pids.add(pid)
        return pids

    def _bot_owned_product_ids(self, db: Any, slave_id: int) -> set[int]:
        """
        Product ids this bot opened for a slave — single source of truth.

        Includes:
        - call/put product_id on non-closed SlaveTrade rows
        - open StructureLeg rows on that slave's active structures
        """
        from backend.models import Structure, StructureLeg

        pids: set[int] = set()
        sid = int(slave_id)

        trades = (
            db.query(SlaveTrade)
            .filter(
                SlaveTrade.slave_account_id == sid,
                SlaveTrade.status != "closed",
            )
            .all()
        )
        for st in trades:
            for attr in (
                "call_product_id",
                "put_product_id",
                "wing_call_product_id",
                "wing_put_product_id",
            ):
                try:
                    pid = int(getattr(st, attr, 0) or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid > 0:
                    pids.add(pid)

        active_structs = (
            db.query(Structure)
            .filter(
                Structure.slave_account_id == sid,
                Structure.account_kind == "SLAVE",
                Structure.status == "active",
            )
            .all()
        )
        struct_ids = [int(s.id) for s in active_structs]
        if struct_ids:
            legs = (
                db.query(StructureLeg)
                .filter(
                    StructureLeg.structure_id.in_(struct_ids),
                    StructureLeg.closed_at.is_(None),
                )
                .all()
            )
            for lg in legs:
                try:
                    pid = int(getattr(lg, "product_id", 0) or 0)
                except (TypeError, ValueError):
                    pid = 0
                if pid > 0:
                    pids.add(pid)

        return pids

    def _assert_slave_hedge_close_allowed(
        self, *, slave_id: int, reason: str
    ) -> bool:
        """
        Only the master hedge-close cascade may close a slave structure hedge.
        Returns True if allowed; logs CRITICAL and returns False otherwise.
        """
        reason_norm = str(reason or "").upper().strip()
        # Cascade passes HEDGE_STOPLOSS / HEDGE_TARGET / … from close_hedge
        from backend.engine.hedge_lifecycle import VALID_HEDGE_EXIT_REASONS

        allowed = (
            reason_norm in VALID_HEDGE_EXIT_REASONS
            or reason_norm.startswith("HEDGE_")
            or reason_norm in FORCE_CLOSE_REASONS
        )
        if allowed:
            return True
        logger.critical(
            "[SLAVE_HEDGE_PROTECTED] slave=%s | attempted_by=%s",
            slave_id,
            reason_norm or "unknown",
        )
        log_and_buffer(
            "SLAVE_HEDGE_PROTECTED",
            0,
            {
                "slave": int(slave_id),
                "attempted_by": reason_norm or "unknown",
            },
        )
        return False

    def _cascade_basket_exit_reason(self, reason: str) -> str:
        """Basket exit reason: force-close uses plain reason; hedge cascade prefixes."""
        reason_norm = str(reason or "").upper().strip()
        if reason_norm in FORCE_CLOSE_REASONS:
            return reason_norm
        return f"HEDGE_CASCADE:{reason_norm}"

    def _get_slave_client(self, slave: SlaveAccount) -> DeltaClient:
        """Create DeltaClient for a slave account."""
        api_key = decrypt(slave.api_key_encrypted)
        api_secret = decrypt(slave.api_secret_encrypted)
        return DeltaClient(api_key, api_secret)

    def _close_slave_trade(
        self,
        slave: SlaveAccount | None,
        slave_trade: SlaveTrade,
        *,
        reason: str,
        allow_virtual: bool = False,
    ) -> bool:
        """
        Set SlaveTrade status to closed.

        Virtual/paper trades are refused unless allow_virtual=True
        (intentional master-exit mirror only).
        """
        if not allow_virtual and is_virtual_slave_trade(slave, slave_trade):
            logger.warning(
                "Refusing to auto-close virtual SlaveTrade id=%s "
                "(slave=%s call_order_id=%s put_order_id=%s) reason=%s",
                getattr(slave_trade, "id", None),
                getattr(slave, "name", None) if slave else None,
                getattr(slave_trade, "call_order_id", None),
                getattr(slave_trade, "put_order_id", None),
                reason,
            )
            return False
        slave_trade.status = "closed"
        return True

    def _log_slave_sizing_zero(
        self,
        *,
        reason: str,
        slave: SlaveAccount | None = None,
        master_trade_id: int = 0,
        master_qty: int = 0,
        computed_slave_qty: int = 0,
        live_balance: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Loud WARNING when slave sizing resolves to 0 (RULE 10 — never silent)."""
        self._last_sizing_zero_reason = str(reason)
        details: dict[str, Any] = {
            "slave_account_id": int(getattr(slave, "id", 0) or 0)
            if slave is not None
            else 0,
            "slave_name": str(getattr(slave, "name", "") or "")
            if slave is not None
            else "",
            "master_trade_id": int(master_trade_id or 0),
            "master_qty": int(master_qty or 0),
            "computed_slave_qty": int(computed_slave_qty or 0),
            "live_balance": live_balance,
            "reason": str(reason),
        }
        if extra:
            details.update(extra)
        log_and_buffer(
            "SLAVE_SIZING_ZERO",
            int(master_trade_id or 0),
            details,
        )

    async def _count_open_option_positions(self, client: DeltaClient) -> int:
        """How many option positions have non-zero size (margin race detector)."""
        try:
            positions = await client.get_option_positions()
        except Exception:
            return 0
        count = 0
        for pos in positions or []:
            try:
                size = abs(float(pos.get("size") or 0))
            except (TypeError, ValueError):
                continue
            if size > 1e-9:
                count += 1
        return count

    async def _fetch_master_capital_for_basket_sizing(
        self,
        *,
        master_client: DeltaClient,
        master_trade_id: int = 0,
    ) -> dict[str, Any]:
        """
        Fetch master total/used capital for capital-based basket sizing.

        Portfolio-margin accounts often report used/position_margin = 0 (or
        even available > balance) while many options are open — that is a
        valid Delta response, not a race. Sizing uses total only
        (qty = floor(effective * mq / total)); used is audit-only.

        Failure: only when total <= 0 (or the request raises).
        """
        wallet = await master_client.get_wallet_balance()
        total = float(wallet.get("balance_usdt", 0) or 0)
        available = float(wallet.get("available_balance", 0) or 0)
        pos_margin = float(wallet.get("position_margin", 0) or 0)
        order_margin = float(wallet.get("order_margin", 0) or 0)
        # Prefer exchange-reported blocked margin; else total − available.
        used = max(0.0, pos_margin + order_margin)
        if used <= 0:
            used = max(0.0, total - available)

        open_count = await self._count_open_option_positions(master_client)
        result: dict[str, Any] = {
            "total": total,
            "used": used,
            "available": available,
            "position_margin": pos_margin,
            "open_position_count": open_count,
            "failed": False,
            "fail_reason": "",
            "retries": 0,
        }

        if total <= 0:
            result["failed"] = True
            result["fail_reason"] = "master_capital_unusable"
            logger.warning(
                "Master capital fetch returned unusable total for "
                "capital-based sizing (total=$%.2f used=$%.2f "
                "open_positions=%s)",
                total,
                used,
                open_count,
            )
            return result

        if open_count > 0 and used <= 0:
            # Portfolio margin: defined-risk structures often show used=0.
            # Expected — do not fail or retry.
            log_and_buffer(
                "MASTER_CAPITAL_ZERO_MARGIN",
                int(master_trade_id or 0),
                {
                    "total": round(total, 4),
                    "available": round(available, 4),
                    "portfolio_margin": round(pos_margin, 4),
                    "order_margin": round(order_margin, 4),
                    "used": round(used, 4),
                    "open_position_count": open_count,
                    "note": (
                        "portfolio margin — used=0 is expected, "
                        "sizing does not depend on it"
                    ),
                },
            )

        logger.info(
            "Master capital: total=$%.2f available=$%.2f used=$%.2f "
            "open_positions=%s",
            total,
            available,
            used,
            open_count,
        )
        return result

    def _calc_qty(
        self,
        master_qty: int,
        multiplier: float,
        slave: SlaveAccount | None = None,
        master_margin_used_usd: float | None = None,
        master_total_capital_usd: float | None = None,
        slave_available_usd: float | None = None,
        master_capital_fetch_failed: bool = False,
        master_capital_fail_reason: str = "",
        master_trade_id: int = 0,
    ) -> int:
        """
        Calculate slave qty.

        Two modes:
        1. Fixed multiplier mode (capital_based_qty=False, default):
           slave_qty = master_qty × qty_multiplier  (then capped at MAX_SLAVE_QTY)

        2. Capital-based mode (capital_based_qty=True):
           LIVE balance is mandatory for real slaves.
           effective_capital = min(user_allocated_capital, live_balance)
           Never size from declared capital alone.
           Returns 0 when capital is insufficient (caller must skip, not place).
           NEVER falls through to the multiplier branch — if master capital
           is unreadable, return 0 (skip), never master×1.0 full size.

        Hard ceiling: MAX_SLAVE_QTY (config) on every path.
        """
        max_qty = max(1, int(MAX_SLAVE_QTY))
        mq = max(0, int(master_qty or 0))
        capital_based = slave is not None and bool(
            getattr(slave, "capital_based_qty", False)
        )
        live = (
            float(slave_available_usd)
            if slave_available_usd is not None
            else 0.0
        )

        if capital_based:
            capital_readable = (
                not master_capital_fetch_failed
                and master_total_capital_usd is not None
                and master_total_capital_usd > 0
                and mq > 0
            )
            if not capital_readable:
                if master_capital_fetch_failed:
                    reason = (
                        str(master_capital_fail_reason).strip()
                        or "master_capital_fetch_failed"
                    )
                else:
                    reason = (
                        "master_capital_unreadable "
                        f"(total={master_total_capital_usd!r} mq={mq})"
                    )
                self._log_slave_sizing_zero(
                    reason=reason,
                    slave=slave,
                    master_trade_id=master_trade_id,
                    master_qty=mq,
                    computed_slave_qty=0,
                    live_balance=live,
                    extra={"mode": "capital_based"},
                )
                return 0

            user_allocated = float(
                getattr(slave, "user_allocated_capital", None) or 0
            )
            is_virtual = bool(getattr(slave, "is_virtual", False))

            # Real slaves: refuse allocated-only sizing when live balance missing
            if not is_virtual and live <= 0:
                self._log_slave_sizing_zero(
                    reason="balance_zero",
                    slave=slave,
                    master_trade_id=master_trade_id,
                    master_qty=mq,
                    computed_slave_qty=0,
                    live_balance=live,
                    extra={
                        "mode": "capital_based",
                        "user_allocated": user_allocated,
                    },
                )
                return 0

            if is_virtual:
                # Paper: allocated (or cached live) is the simulated bankroll
                effective_capital = (
                    user_allocated if user_allocated > 0 else live
                )
            elif user_allocated > 0:
                effective_capital = min(user_allocated, live)
            else:
                effective_capital = live

            if effective_capital <= 0:
                self._log_slave_sizing_zero(
                    reason="effective_capital_zero",
                    slave=slave,
                    master_trade_id=master_trade_id,
                    master_qty=mq,
                    computed_slave_qty=0,
                    live_balance=live,
                    extra={
                        "mode": "capital_based",
                        "user_allocated": user_allocated,
                        "effective_capital": effective_capital,
                    },
                )
                return 0

            # qty = floor(effective * mq / master_total)
            # master_used cancels out of the old ratio formula — do not divide
            # by it (portfolio margin often reports used=0).
            calculated_qty = int(
                effective_capital * mq / float(master_total_capital_usd)
            )

            # Do NOT force max(1, ...) — insufficient capital must skip
            final_qty = max(0, min(calculated_qty, max_qty))
            logger.info(
                "[SLAVE_SIZING] account_id=%s effective=%.2f "
                "master_qty=%s master_total=%.2f calculated_qty=%s "
                "final_qty=%s master_used=%.2f "
                "(audit only — not used in sizing) "
                "allocated=%.2f live_balance=%.2f cap=%s",
                getattr(slave, "id", None),
                effective_capital,
                mq,
                float(master_total_capital_usd),
                calculated_qty,
                final_qty,
                float(master_margin_used_usd or 0),
                user_allocated,
                live,
                max_qty,
            )
            if final_qty <= 0:
                self._log_slave_sizing_zero(
                    reason="capital_based_qty_zero",
                    slave=slave,
                    master_trade_id=master_trade_id,
                    master_qty=mq,
                    computed_slave_qty=0,
                    live_balance=live,
                    extra={
                        "mode": "capital_based",
                        "user_allocated": user_allocated,
                        "effective_capital": effective_capital,
                        "master_total_capital": float(
                            master_total_capital_usd
                        ),
                        "raw_qty": calculated_qty,
                    },
                )
            return int(final_qty)

        # Fixed multiplier only — capital-based never reaches here
        if mq <= 0:
            self._log_slave_sizing_zero(
                reason="input_qty_zero",
                slave=slave,
                master_trade_id=master_trade_id,
                master_qty=mq,
                computed_slave_qty=0,
                live_balance=live if slave_available_usd is not None else None,
                extra={"mode": "multiplier"},
            )
            return 0
        calculated_qty = max(1, int(round(float(mq) * float(multiplier or 1.0))))
        final_qty = min(calculated_qty, max_qty)
        if slave is not None:
            logger.info(
                "[SLAVE_SIZING] account_id=%s mode=multiplier "
                "master_qty=%s mult=%.3f → final_qty=%s (cap=%s)",
                getattr(slave, "id", None),
                mq,
                float(multiplier or 1.0),
                final_qty,
                max_qty,
            )
        return int(final_qty)

    def _fit_qty_to_margin(
        self,
        slave_qty: int,
        *,
        live_balance: float,
        master_margin_used_usd: float | None,
        master_qty: int,
        call_fill: float = 0.0,
        put_fill: float = 0.0,
        slave: SlaveAccount | None = None,
        master_trade_id: int = 0,
    ) -> int:
        """
        Reduce qty until estimated margin ≤ 90% of live balance.
        Returns 0 if even 1 lot does not fit.
        """
        from backend.config import OPTIONS_CONTRACT_VALUE

        qty = max(0, int(slave_qty or 0))
        balance = float(live_balance or 0.0)
        if qty <= 0:
            self._log_slave_sizing_zero(
                reason="input_qty_zero",
                slave=slave,
                master_trade_id=master_trade_id,
                master_qty=int(master_qty or 0),
                computed_slave_qty=0,
                live_balance=balance,
                extra={"phase": "fit_qty_to_margin"},
            )
            return 0
        if balance <= 0:
            self._log_slave_sizing_zero(
                reason="balance_zero",
                slave=slave,
                master_trade_id=master_trade_id,
                master_qty=int(master_qty or 0),
                computed_slave_qty=0,
                live_balance=balance,
                extra={
                    "phase": "fit_qty_to_margin",
                    "input_slave_qty": int(slave_qty or 0),
                },
            )
            return 0

        mq = max(1, int(master_qty or 1))
        if master_margin_used_usd is not None and master_margin_used_usd > 0:
            per_lot = float(master_margin_used_usd) / mq
        else:
            # Conservative fallback: premium notional × 5 as rough margin proxy
            prem = float(call_fill or 0) + float(put_fill or 0)
            per_lot = max(
                prem * float(OPTIONS_CONTRACT_VALUE) * 5.0,
                1.0,
            )

        headroom = balance * 0.9
        while qty >= 1:
            required = per_lot * qty
            if required <= headroom:
                break
            qty -= 1

        if qty < int(slave_qty or 0):
            logger.info(
                "[SLAVE_MARGIN_PRECHECK] reduced qty %s → %s "
                "(per_lot=$%.4f headroom=$%.2f balance=$%.2f)",
                slave_qty,
                qty,
                per_lot,
                headroom,
                balance,
            )
        if qty <= 0:
            self._log_slave_sizing_zero(
                reason="margin_fit_zero",
                slave=slave,
                master_trade_id=master_trade_id,
                master_qty=int(master_qty or 0),
                computed_slave_qty=0,
                live_balance=balance,
                extra={
                    "phase": "fit_qty_to_margin",
                    "input_slave_qty": int(slave_qty or 0),
                    "per_lot": per_lot,
                    "headroom": headroom,
                },
            )
        return max(0, qty)

    async def _resolve_entry_conflicts(
        self,
        slave: SlaveAccount,
        client: DeltaClient,
        conflicting: list[dict[str, Any]],
        call_product_id: int,
        put_product_id: int,
        master_trade_id: int,
        db: Any,
    ) -> str:
        """
        Resolve option positions that block a new mirror entry.

        Returns:
          'cleared' — conflicts closed; proceed with entry
          'foreign' — unknown/user positions; do not touch
          'failed'  — close attempt failed
        """
        conflicting_pids = set()
        for pos in conflicting:
            try:
                conflicting_pids.add(int(pos.get("product_id") or 0))
            except (TypeError, ValueError):
                continue
        conflicting_pids.discard(0)
        if not conflicting_pids:
            return "cleared"

        from backend.models import Leg

        # Prior bot-managed slave trades on this account (any non-closed)
        prior = (
            db.query(SlaveTrade)
            .filter(
                SlaveTrade.slave_account_id == int(slave.id),
                SlaveTrade.status.in_(
                    (
                        "active",
                        "error",
                        "partial",
                        "partial_adjustment",
                        "adjust_close_failed",
                        "blocked_foreign_position",
                        "exit_failed",
                    )
                ),
            )
            .all()
        )

        bot_owned_pids: set[int] = set()
        owning_trades: list[SlaveTrade] = []
        for st in prior:
            leg_rows = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == int(st.master_trade_id),
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            )
            st_pids = {
                int(getattr(lg, "product_id", 0) or 0) for lg in leg_rows
            }
            st_pids.discard(0)
            overlap = conflicting_pids & st_pids
            if overlap:
                bot_owned_pids |= overlap
                owning_trades.append(st)

        # Also treat same-master leftover from a failed prior attempt as bot-owned
        if conflicting_pids & {int(call_product_id), int(put_product_id)}:
            for st in prior:
                if int(st.master_trade_id) == int(master_trade_id):
                    bot_owned_pids |= (
                        conflicting_pids
                        & {int(call_product_id), int(put_product_id)}
                    )
                    if st not in owning_trades:
                        owning_trades.append(st)

        foreign_pids = conflicting_pids - bot_owned_pids
        if foreign_pids:
            logger.warning(
                "[SLAVE_CONFLICT_RESOLVE] slave='%s' branch=foreign "
                "foreign_pids=%s bot_owned=%s — skip entry",
                slave.name,
                sorted(foreign_pids),
                sorted(bot_owned_pids),
            )
            return "foreign"

        logger.info(
            "[SLAVE_CONFLICT_RESOLVE] slave='%s' branch=bot_stale "
            "closing_pids=%s owning_slave_trades=%s",
            slave.name,
            sorted(conflicting_pids),
            [int(st.id) for st in owning_trades],
        )

        # Close each conflicting position — reduce_only only (never drop flag)
        for pos in conflicting:
            try:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or size == 0:
                continue
            ok, _order, err = await self._close_with_reduce_only(
                client=client,
                slave=slave,
                product_id=pid,
                signed_size=size,
                master_trade_id=int(master_trade_id),
                path="_resolve_entry_conflicts",
            )
            if not ok:
                logger.error(
                    "[SLAVE_CONFLICT_RESOLVE] slave='%s' close FAILED "
                    "pid=%s: %s",
                    slave.name,
                    pid,
                    err,
                )
                return "failed"
            logger.info(
                "[SLAVE_CONFLICT_RESOLVE] slave='%s' closed pid=%s "
                "size=%s",
                slave.name,
                pid,
                size,
            )

        for st in owning_trades:
            if str(st.status).lower() == "closed":
                continue
            note = (
                f"auto_closed_stale_conflict→new_master={master_trade_id}"
            )
            self._close_slave_trade(
                slave,
                st,
                reason=note,
                allow_virtual=False,
            )
            st.last_error = note[:500]
            st.last_updated = get_utc_now()

        db.commit()
        return "cleared"

    @staticmethod
    def _order_id(order_result: dict[str, Any] | None) -> str:
        if not order_result:
            return ""
        oid = order_result.get("order_id") or order_result.get("id")
        return str(oid) if oid is not None else ""

    @staticmethod
    def _fee_from_order(order_result: dict[str, Any] | None) -> float:
        if not order_result or not isinstance(order_result, dict):
            return 0.0
        for src in (order_result, order_result.get("raw") or {}):
            if not isinstance(src, dict):
                continue
            for field in ("paid_commission", "commission"):
                try:
                    val = abs(float(src.get(field) or 0))
                except (TypeError, ValueError):
                    val = 0.0
                if val > 0:
                    return val
        return 0.0

    async def _resolve_order_fee(
        self,
        client: DeltaClient,
        order_result: dict[str, Any] | None,
    ) -> float:
        fee = self._fee_from_order(order_result)
        if fee > 0:
            return fee
        oid = self._order_id(order_result)
        if not oid or str(oid).upper() == "VIRTUAL":
            return 0.0
        try:
            return abs(float(await client.get_order_commission(oid) or 0))
        except Exception:
            return 0.0

    def _log_slave_trade_detail(
        self,
        *,
        slave_id: int,
        master_trade_id: int,
        qty: int,
        call_symbol: str,
        call_fill: float,
        put_symbol: str,
        put_fill: float,
        entry_spread: float,
        entry_fees: float,
    ) -> None:
        log_and_buffer(
            "SLAVE_TRADE_DETAIL",
            int(master_trade_id),
            {
                "slave": int(slave_id),
                "master_trade": int(master_trade_id),
                "qty": int(qty),
                "call": f"{call_symbol}@{call_fill}",
                "put": f"{put_symbol}@{put_fill}",
                "entry_spread": round(float(entry_spread), 6),
                "entry_fees": round(float(entry_fees), 6),
            },
        )
        logger.info(
            "[SLAVE_TRADE_DETAIL] slave=%s | master_trade=%s | qty=%s | "
            "call=%s@%s | put=%s@%s | entry_spread=%s | entry_fees=%s",
            slave_id,
            master_trade_id,
            qty,
            call_symbol,
            call_fill,
            put_symbol,
            put_fill,
            round(float(entry_spread), 6),
            round(float(entry_fees), 6),
        )

    async def mirror_trade_entry(
        self,
        master_trade_id: int,
        call_product_id: int,
        put_product_id: int,
        master_call_qty: int,
        master_put_qty: int,
        master_call_strike: float,
        master_put_strike: float,
        master_call_symbol: str,
        master_put_symbol: str,
        master_call_fill: float,
        master_put_fill: float,
        expiry_date: Any,
        underlying: str,
        master_bracket_sl_call: float | None = None,
        master_bracket_sl_put: float | None = None,
        wing_call_product_id: int | None = None,
        wing_put_product_id: int | None = None,
        wing_call_strike: float | None = None,
        wing_put_strike: float | None = None,
        wing_call_symbol: str | None = None,
        wing_put_symbol: str | None = None,
        wing_call_fill: float | None = None,
        wing_put_fill: float | None = None,
    ) -> None:
        """
        Mirror a new trade entry on all active slave accounts.
        Called right after master trade is placed successfully.
        Non-fatal: if one slave fails, others continue.

        master_bracket_sl_* are ABSOLUTE stop prices from the master's fill —
        slaves must use them verbatim (never recompute from slave fill/mark).

        When wing_* product ids are set, slave places wings BUY first then
        shorts (same strikes as master, qty = slave sizing). Wing fail →
        abort without shorts (NIYAM 0).
        """
        with self.db_factory() as db:
            slaves = get_active_slave_accounts(db)
            if not slaves:
                return

            logger.info(
                "Mirroring trade entry to %s slave account(s) master_trade=%s",
                len(slaves),
                master_trade_id,
            )

            for slave in slaves:
                async with self._slave_op_lock(
                    int(slave.id), "mirror_trade_entry"
                ) as acquired:
                    if not acquired:
                        continue
                    await self._mirror_entry_to_slave(
                        slave=slave,
                        master_trade_id=master_trade_id,
                        call_product_id=call_product_id,
                        put_product_id=put_product_id,
                        master_call_qty=master_call_qty,
                        master_put_qty=master_put_qty,
                        master_call_strike=master_call_strike,
                        master_put_strike=master_put_strike,
                        master_call_symbol=master_call_symbol,
                        master_put_symbol=master_put_symbol,
                        master_call_fill=master_call_fill,
                        master_put_fill=master_put_fill,
                        expiry_date=expiry_date,
                        underlying=underlying,
                        db=db,
                        master_bracket_sl_call=master_bracket_sl_call,
                        master_bracket_sl_put=master_bracket_sl_put,
                        wing_call_product_id=wing_call_product_id,
                        wing_put_product_id=wing_put_product_id,
                        wing_call_strike=wing_call_strike,
                        wing_put_strike=wing_put_strike,
                        wing_call_symbol=wing_call_symbol,
                        wing_put_symbol=wing_put_symbol,
                        wing_call_fill=wing_call_fill,
                        wing_put_fill=wing_put_fill,
                    )

    async def _mirror_entry_to_slave(
        self,
        slave: SlaveAccount,
        master_trade_id: int,
        call_product_id: int,
        put_product_id: int,
        master_call_qty: int,
        master_put_qty: int,
        master_call_strike: float,
        master_put_strike: float,
        master_call_symbol: str,
        master_put_symbol: str,
        master_call_fill: float,
        master_put_fill: float,
        expiry_date: Any,
        underlying: str,
        db: Any,
        master_bracket_sl_call: float | None = None,
        master_bracket_sl_put: float | None = None,
        wing_call_product_id: int | None = None,
        wing_put_product_id: int | None = None,
        wing_call_strike: float | None = None,
        wing_put_strike: float | None = None,
        wing_call_symbol: str | None = None,
        wing_put_symbol: str | None = None,
        wing_call_fill: float | None = None,
        wing_put_fill: float | None = None,
    ) -> None:
        # Caller MUST hold the per-slave lock.
        master_hedge_id = self._resolve_master_hedge_id_for_trade(
            db, int(master_trade_id)
        )
        ok_hedge, hedge_reason = self._assert_hedge_before_basket(
            db, int(slave.id), master_hedge_id
        )
        if not ok_hedge:
            self._skip_basket_no_hedge(
                db,
                slave=slave,
                master_trade_id=int(master_trade_id),
                master_hedge_id=master_hedge_id,
                reason=hedge_reason,
            )
            return

        # Fetch master + fresh slave capital for capital-based qty calculation
        master_margin_used: float | None = None
        master_total_capital: float | None = None
        slave_fresh_available: float | None = None
        master_capital_fetch_failed = False
        master_capital_fail_reason = ""
        master_open_position_count = 0
        self._last_sizing_zero_reason = ""

        if bool(getattr(slave, "capital_based_qty", False)):
            try:
                # Fetch master capital
                with self.db_factory() as cap_db:
                    from backend.models import Account

                    master_acc = (
                        cap_db.query(Account)
                        .filter(Account.is_active.is_(True))
                        .order_by(Account.id.asc())
                        .first()
                    )
                    if master_acc:
                        master_client = DeltaClient(
                            decrypt(master_acc.api_key_encrypted),
                            decrypt(master_acc.api_secret_encrypted),
                        )
                        try:
                            cap = await self._fetch_master_capital_for_basket_sizing(
                                master_client=master_client,
                                master_trade_id=int(master_trade_id),
                            )
                            master_total_capital = float(cap["total"])
                            master_margin_used = float(cap["used"])
                            master_open_position_count = int(
                                cap.get("open_position_count") or 0
                            )
                            if bool(cap.get("failed")):
                                master_capital_fetch_failed = True
                                master_capital_fail_reason = str(
                                    cap.get("fail_reason")
                                    or "master_capital_fetch_failed"
                                )
                        finally:
                            await master_client.close()
                    else:
                        master_capital_fetch_failed = True
                        master_capital_fail_reason = "no_active_master_account"
                        logger.warning(
                            "Master capital fetch failed: no active master account"
                        )
            except Exception as cap_err:
                master_capital_fetch_failed = True
                master_capital_fail_reason = f"master_capital_fetch_exception:{cap_err}"
                logger.warning("Master capital fetch failed: %s", cap_err)

        # Always fetch live slave balance for sizing / margin headroom
        # (virtual/paper slaves have no real wallet — use allocated/cached)
        if bool(getattr(slave, "is_virtual", False)):
            allocated = float(
                getattr(slave, "user_allocated_capital", None) or 0
            )
            cached = float(getattr(slave, "balance_usd", 0) or 0)
            slave_fresh_available = allocated if allocated > 0 else cached
            logger.info(
                "Slave '%s' virtual capital: available=$%.2f "
                "(no Delta wallet fetch)",
                slave.name,
                slave_fresh_available,
            )
        else:
            try:
                slave_client = self._get_slave_client(slave)
                try:
                    slave_wallet = await slave_client.get_wallet_balance()
                    slave_fresh_available = float(
                        slave_wallet.get("available_balance", 0) or 0
                    )
                    logger.info(
                        "Slave '%s' fresh balance: available=$%.2f",
                        slave.name,
                        slave_fresh_available,
                    )
                    with self.db_factory() as upd_db:
                        from backend.models import SlaveAccount as SA

                        s = (
                            upd_db.query(SA)
                            .filter(SA.id == slave.id)
                            .first()
                        )
                        if s:
                            s.balance_usd = float(
                                slave_wallet.get("balance_usdt", 0) or 0
                            )
                            upd_db.commit()
                finally:
                    await slave_client.close()
            except Exception as bal_err:
                logger.warning(
                    "Slave '%s' balance fetch failed, using cached: %s",
                    slave.name,
                    bal_err,
                )
                slave_fresh_available = float(
                    getattr(slave, "balance_usd", 0) or 0
                )

        # Use master_call_qty as the lot size for capital-based scaling
        slave_qty = self._calc_qty(
            int(master_call_qty),
            float(slave.qty_multiplier or 1.0),
            slave=slave,
            master_margin_used_usd=master_margin_used,
            master_total_capital_usd=master_total_capital,
            slave_available_usd=slave_fresh_available,
            master_capital_fetch_failed=master_capital_fetch_failed,
            master_capital_fail_reason=master_capital_fail_reason,
            master_trade_id=int(master_trade_id),
        )

        # Margin headroom: never let Delta be the first insufficient_margin gate
        live_for_margin = float(
            slave_fresh_available
            if slave_fresh_available is not None
            else (getattr(slave, "balance_usd", 0) or 0)
        )
        if not bool(getattr(slave, "is_virtual", False)) and slave_qty > 0:
            slave_qty = self._fit_qty_to_margin(
                slave_qty,
                live_balance=live_for_margin,
                master_margin_used_usd=master_margin_used,
                master_qty=int(master_call_qty),
                call_fill=float(master_call_fill or 0),
                put_fill=float(master_put_fill or 0),
                slave=slave,
                master_trade_id=int(master_trade_id),
            )

        if slave_qty < 1:
            # Prefer reason already logged by _calc_qty / _fit_qty_to_margin
            zero_reason = str(
                getattr(self, "_last_sizing_zero_reason", "") or ""
            ).strip()
            if not zero_reason and master_capital_fetch_failed:
                zero_reason = (
                    master_capital_fail_reason or "master_capital_fetch_failed"
                )
            if not zero_reason:
                zero_reason = "entry_skip_qty_zero"

            allocated = float(
                getattr(slave, "user_allocated_capital", 0) or 0
            )
            # "under-funded" only when slave capital is genuinely the limit
            underfunded_reasons = {
                "balance_zero",
                "effective_capital_zero",
                "margin_fit_zero",
                "input_qty_zero",
            }
            if zero_reason in (
                "master_capital_fetch_failed",
                "master_capital_unusable",
                "no_active_master_account",
            ) or zero_reason.startswith("master_capital"):
                msg = (
                    f"skipped_{zero_reason}: master_total="
                    f"${float(master_total_capital or 0):.2f} "
                    f"master_used=${float(master_margin_used or 0):.2f} "
                    f"master_open_positions={master_open_position_count} "
                    f"slave_live=${live_for_margin:.2f} "
                    f"allocated=${allocated:.2f} master_qty={int(master_call_qty)} "
                    f"(NOT under-funded — master capital unreadable)"
                )
            elif zero_reason in underfunded_reasons:
                msg = (
                    f"skipped_low_capital: reason={zero_reason} "
                    f"live=${live_for_margin:.2f} allocated=${allocated:.2f} "
                    f"master_qty={int(master_call_qty)} "
                    f"(entry not mirrored — under-funded)"
                )
            else:
                msg = (
                    f"skipped_{zero_reason}: qty=0 live=${live_for_margin:.2f} "
                    f"allocated=${allocated:.2f} "
                    f"master_qty={int(master_call_qty)} "
                    f"(entry not mirrored)"
                )

            # status stays skipped_low_capital for legacy UI filters;
            # last_error carries the real reason (not always under-funded).
            log_and_buffer(
                "SLAVE_SIZING_ZERO",
                int(master_trade_id),
                {
                    "slave_account_id": int(slave.id),
                    "slave_name": str(slave.name or ""),
                    "master_trade_id": int(master_trade_id),
                    "master_qty": int(master_call_qty),
                    "computed_slave_qty": 0,
                    "live_balance": live_for_margin,
                    "reason": zero_reason,
                    "last_error": msg[:500],
                    "master_total": master_total_capital,
                    "master_used": master_margin_used,
                    "master_open_positions": master_open_position_count,
                },
            )
            skip_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                actual_quantity=0,
                status="skipped_low_capital",
                last_error=msg[:500],
                error_count=0,
            )
            db.add(skip_trade)
            db.commit()
            return

        logger.info(
            "Mirroring to slave '%s': master_qty=%s × %s = slave_qty=%s "
            "(call=%s put=%s %s)",
            slave.name,
            master_call_qty,
            slave.qty_multiplier,
            slave_qty,
            master_call_strike,
            master_put_strike,
            underlying,
        )

        # Virtual mode: track position in DB but don't place real orders
        if bool(getattr(slave, "is_virtual", False)):
            logger.info(
                "VIRTUAL TRADE: slave='%s' call=%s put=%s qty=%s "
                "(no real order placed)",
                slave.name,
                master_call_strike,
                master_put_strike,
                slave_qty,
            )
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            virt_basket_open_ts = get_utc_now()
            virt_call_fill = float(master_call_fill or 0)
            virt_put_fill = float(master_put_fill or 0)
            virt_entry_spread = (
                compute_entry_spread_usd(
                    sent_price=virt_call_fill,
                    fill_price=virt_call_fill,
                    quantity=slave_qty,
                    is_long=False,
                )
                + compute_entry_spread_usd(
                    sent_price=virt_put_fill,
                    fill_price=virt_put_fill,
                    quantity=slave_qty,
                    is_long=False,
                )
            )
            virt_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                actual_quantity=slave_qty,
                original_quantity=slave_qty,
                call_fill_price=virt_call_fill,
                put_fill_price=virt_put_fill,
                call_order_id="VIRTUAL",
                put_order_id="VIRTUAL",
                call_product_id=int(call_product_id),
                put_product_id=int(put_product_id),
                call_symbol=str(master_call_symbol or ""),
                put_symbol=str(master_put_symbol or ""),
                call_strike=float(master_call_strike or 0),
                put_strike=float(master_put_strike or 0),
                wing_call_product_id=(
                    int(wing_call_product_id)
                    if wing_call_product_id
                    else None
                ),
                wing_put_product_id=(
                    int(wing_put_product_id)
                    if wing_put_product_id
                    else None
                ),
                wing_call_symbol=(
                    str(wing_call_symbol or "")
                    if wing_call_product_id
                    else None
                ),
                wing_put_symbol=(
                    str(wing_put_symbol or "")
                    if wing_put_product_id
                    else None
                ),
                wing_call_strike=(
                    float(wing_call_strike or 0)
                    if wing_call_product_id
                    else None
                ),
                wing_put_strike=(
                    float(wing_put_strike or 0)
                    if wing_put_product_id
                    else None
                ),
                wing_call_order_id=(
                    "VIRTUAL" if wing_call_product_id else None
                ),
                wing_put_order_id=(
                    "VIRTUAL" if wing_put_product_id else None
                ),
                wing_call_fill_price=(
                    float(wing_call_fill or 0)
                    if wing_call_product_id
                    else None
                ),
                wing_put_fill_price=(
                    float(wing_put_fill or 0)
                    if wing_put_product_id
                    else None
                ),
                call_entry_fee_usd=0.0,
                put_entry_fee_usd=0.0,
                entry_spread_usd=float(virt_entry_spread or 0.0),
                status="active",
            )
            db.add(virt_trade)
            db.commit()
            try:
                from backend.engine.structure_ledger import (
                    record_slave_basket_entry,
                )
                from backend.models import Trade as MasterTrade

                master_row = (
                    db.query(MasterTrade)
                    .filter(MasterTrade.id == int(master_trade_id))
                    .first()
                )
                if master_row is not None:
                    record_slave_basket_entry(
                        db,
                        slave_trade=virt_trade,
                        slave_account_id=int(slave.id),
                        master_trade=master_row,
                        call_opened_at=virt_basket_open_ts,
                        put_opened_at=virt_basket_open_ts,
                        call_fill_at=virt_basket_open_ts,
                        put_fill_at=virt_basket_open_ts,
                    )
                    db.commit()
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave basket entry failed: %s",
                    ledger_exc,
                    exc_info=True,
                )
            self._log_slave_trade_detail(
                slave_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                qty=slave_qty,
                call_symbol=str(master_call_symbol or ""),
                call_fill=virt_call_fill,
                put_symbol=str(master_put_symbol or ""),
                put_fill=virt_put_fill,
                entry_spread=float(virt_entry_spread or 0.0),
                entry_fees=0.0,
            )
            logger.info(
                "VIRTUAL TRADE created: slave='%s' slave_trade_id=%s",
                slave.name,
                virt_trade.id,
            )
            return  # Skip real order placement

        client = self._get_slave_client(slave)
        try:
            # Guard: resolve leftover bot positions; never block forever
            try:
                slave_positions = await client.get_option_positions()
                wanted = {int(call_product_id), int(put_product_id)}
                conflicting = []
                for pos in slave_positions:
                    try:
                        pid = int(pos.get("product_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if pid in wanted:
                        conflicting.append(pos)
                if conflicting:
                    symbols = [
                        p.get("product_symbol") or p.get("symbol")
                        for p in conflicting
                    ]
                    logger.warning(
                        "Slave '%s' conflicting positions: %s — resolving",
                        slave.name,
                        symbols,
                    )
                    resolve = await self._resolve_entry_conflicts(
                        slave=slave,
                        client=client,
                        conflicting=conflicting,
                        call_product_id=int(call_product_id),
                        put_product_id=int(put_product_id),
                        master_trade_id=int(master_trade_id),
                        db=db,
                    )
                    if resolve == "foreign":
                        slave_trade = SlaveTrade(
                            slave_account_id=int(slave.id),
                            master_trade_id=int(master_trade_id),
                            actual_quantity=slave_qty,
                            status="blocked_foreign_position",
                            last_error=(
                                "blocked_foreign_position: unknown option "
                                f"positions on slave {symbols}"
                            )[:500],
                            error_count=1,
                        )
                        db.add(slave_trade)
                        slave.connection_status = "error"
                        slave.last_error = (
                            "blocked_foreign_position — not auto-closing "
                            "user-owned positions"
                        )[:500]
                        slave.updated_at = get_utc_now()
                        db.commit()
                        return
                    if resolve == "failed":
                        slave_trade = SlaveTrade(
                            slave_account_id=int(slave.id),
                            master_trade_id=int(master_trade_id),
                            actual_quantity=slave_qty,
                            status="error",
                            last_error=(
                                "conflict_close_failed: could not clear "
                                f"stale positions {symbols}"
                            )[:500],
                            error_count=1,
                        )
                        db.add(slave_trade)
                        slave.connection_status = "error"
                        slave.last_error = "conflict_close_failed"[:500]
                        slave.updated_at = get_utc_now()
                        db.commit()
                        return
                    # cleared — continue with entry
                    logger.info(
                        "[SLAVE_CONFLICT_RESOLVE] slave='%s' cleared — "
                        "proceeding with mirror entry",
                        slave.name,
                    )
            except Exception as exc:
                logger.warning(
                    "Slave '%s' position check failed: %s — continuing",
                    slave.name,
                    exc,
                )

            # Canonical ABSOLUTE bracket SL from master fill — never slave fill/mark.
            from backend.core.delta_sl import compute_bracket_sl
            from backend.engine.slave_wings import (
                build_slave_entry_plan,
                filled_leg_to_unwind_dict,
                log_slave_wing_entry_abort,
                place_slave_plan_legs,
                sort_unwind_dicts,
                wings_enabled_from_master_picks,
            )
            from backend.engine.wing_entry import EntryPartialUnwind
            from backend.strategies.base_strategy import OrderResult

            uni_sl = self._master_universal_sl_pct(db, int(master_trade_id))
            if master_bracket_sl_call is not None and float(master_bracket_sl_call) > 0:
                call_sl = round(float(master_bracket_sl_call), 2)
                call_sl_limit = round(call_sl * 1.05, 2)
            else:
                call_sl, call_sl_limit = compute_bracket_sl(
                    float(master_call_fill or 0.0),
                    uni_sl,
                    leg="call",
                    trade_id=int(master_trade_id),
                )
                if call_sl <= 0:
                    call_sl = None  # type: ignore[assignment]
                    call_sl_limit = None  # type: ignore[assignment]
            if master_bracket_sl_put is not None and float(master_bracket_sl_put) > 0:
                put_sl = round(float(master_bracket_sl_put), 2)
                put_sl_limit = round(put_sl * 1.05, 2)
            else:
                put_sl, put_sl_limit = compute_bracket_sl(
                    float(master_put_fill or 0.0),
                    uni_sl,
                    leg="put",
                    trade_id=int(master_trade_id),
                )
                if put_sl <= 0:
                    put_sl = None  # type: ignore[assignment]
                    put_sl_limit = None  # type: ignore[assignment]

            wing_call_pick = None
            wing_put_pick = None
            try:
                wc_pid = int(wing_call_product_id or 0)
                wp_pid = int(wing_put_product_id or 0)
            except (TypeError, ValueError):
                wc_pid, wp_pid = 0, 0
            if wc_pid > 0 and wp_pid > 0:
                wing_call_pick = {
                    "product_id": wc_pid,
                    "symbol": str(wing_call_symbol or ""),
                    "strike": float(wing_call_strike or 0),
                    "premium": float(wing_call_fill or 0),
                }
                wing_put_pick = {
                    "product_id": wp_pid,
                    "symbol": str(wing_put_symbol or ""),
                    "strike": float(wing_put_strike or 0),
                    "premium": float(wing_put_fill or 0),
                }
            wings_on = wings_enabled_from_master_picks(
                wing_call_pick, wing_put_pick
            )

            plan = build_slave_entry_plan(
                slave_qty=int(slave_qty),
                call_product_id=int(call_product_id),
                put_product_id=int(put_product_id),
                call_symbol=str(master_call_symbol or ""),
                put_symbol=str(master_put_symbol or ""),
                call_strike=float(master_call_strike or 0),
                put_strike=float(master_put_strike or 0),
                call_premium=float(master_call_fill or 0),
                put_premium=float(master_put_fill or 0),
                wing_call=wing_call_pick,
                wing_put=wing_put_pick,
                call_bracket_sl=call_sl,
                call_bracket_limit=call_sl_limit,
                put_bracket_sl=put_sl,
                put_bracket_limit=put_sl_limit,
            )

            call_open_ts = get_utc_now()
            put_open_ts = call_open_ts
            call_fill_ts = call_open_ts
            put_fill_ts = call_open_ts
            call_order: dict[str, Any] | None = None
            put_order: dict[str, Any] | None = None
            call_fill = float(master_call_fill or 0.0)
            put_fill = float(master_put_fill or 0.0)
            call_order_id: str | None = None
            put_order_id: str | None = None
            wing_call_open_ts = None
            wing_put_open_ts = None
            wing_call_fill_ts = None
            wing_put_fill_ts = None
            wing_call_fill_px = float(wing_call_fill or 0.0)
            wing_put_fill_px = float(wing_put_fill or 0.0)
            wing_call_oid: str | None = None
            wing_put_oid: str | None = None
            filled_entry_legs: list[Any] = []

            async def _place_raw(spec: Any) -> OrderResult:
                side = "buy" if spec.is_long else "sell"
                try:
                    raw = await client.place_order(
                        product_id=int(spec.product_id),
                        size=int(spec.quantity),
                        side=side,
                        bracket_stop_loss_price=(
                            None if spec.is_long else spec.bracket_sl_price
                        ),
                        bracket_stop_loss_limit_price=(
                            None if spec.is_long else spec.bracket_sl_limit
                        ),
                    )
                    fill_px = float(
                        await client.resolve_fill_price(
                            raw, symbol_for_fallback=str(spec.symbol)
                        )
                        or 0.0
                    )
                    oid = raw.get("order_id") or raw.get("id")
                    filled_size = int(spec.quantity)
                    try:
                        if raw.get("size") is not None:
                            filled_size = int(raw.get("size"))
                    except (TypeError, ValueError):
                        filled_size = int(spec.quantity)
                    return OrderResult(
                        success=True,
                        order_id=int(oid) if oid is not None else None,
                        filled_price=fill_px if fill_px > 0 else float(
                            spec.mark_premium
                        ),
                        filled_size=filled_size,
                        commission=None,
                    )
                except Exception as place_exc:
                    return OrderResult(
                        success=False,
                        error=str(place_exc),
                        filled_size=0,
                    )

            def _place_fn_for(spec: Any):
                async def _inner() -> OrderResult:
                    return await _place_raw(spec)

                return _inner

            try:
                filled_entry_legs = await place_slave_plan_legs(
                    plan=plan,
                    place_fn_for_spec=_place_fn_for,
                    slave_name=str(slave.name),
                )
            except EntryPartialUnwind as partial:
                # NIYAM 0: if wings failed before shorts, or any leg incomplete —
                # unwind filled legs (shorts first, then wings). Never leave
                # naked shorts without wings when master was a condor.
                filled_entry_legs = list(partial.filled_legs)
                to_uw = sort_unwind_dicts(
                    [filled_leg_to_unwind_dict(x) for x in filled_entry_legs]
                )
                for item in to_uw:
                    item["opened_at"] = get_utc_now()
                    item["fill_at"] = get_utc_now()
                unwound = await self._unwind_slave_entry_legs(
                    client=client,
                    slave=slave,
                    master_trade_id=int(master_trade_id),
                    legs_to_unwind=to_uw,
                )
                wings_closed = sum(
                    1
                    for x in unwound
                    if str(x.get("leg") or "").startswith("wing")
                    and x.get("unwound")
                )
                wings_failed = sum(
                    1
                    for x in unwound
                    if str(x.get("leg") or "").startswith("wing")
                    and not x.get("unwound")
                )
                # If failure was on a wing role and no shorts filled → abort
                failed_is_wing = str(partial.failed_role or "").startswith(
                    "wing"
                )
                shorts_filled = any(
                    not x.is_long for x in filled_entry_legs
                )
                if failed_is_wing and not shorts_filled:
                    log_slave_wing_entry_abort(
                        slave_name=str(slave.name),
                        reason=str(partial),
                        wings_closed=wings_closed,
                        wings_failed=wings_failed,
                    )
                else:
                    log_slave_wing_entry_abort(
                        slave_name=str(slave.name),
                        reason=f"partial_entry:{partial.failed_role}:{partial}",
                        wings_closed=wings_closed,
                        wings_failed=wings_failed,
                    )
                abort_msg = (
                    f"SLAVE_WING_ENTRY_ABORT: {partial.failed_role}: {partial}"
                )[:500]
                slave_trade = SlaveTrade(
                    slave_account_id=int(slave.id),
                    master_trade_id=int(master_trade_id),
                    actual_quantity=slave_qty,
                    original_quantity=slave_qty,
                    call_product_id=int(call_product_id),
                    put_product_id=int(put_product_id),
                    call_symbol=str(master_call_symbol or ""),
                    put_symbol=str(master_put_symbol or ""),
                    call_strike=float(master_call_strike or 0),
                    put_strike=float(master_put_strike or 0),
                    status="error",
                    last_error=abort_msg,
                    error_count=1,
                )
                db.add(slave_trade)
                slave.connection_status = "error"
                slave.last_error = abort_msg
                slave.updated_at = get_utc_now()
                db.commit()
                return

            # Map filled legs → call/put/wing locals (compat with verify/ledger).
            # Use per-leg pre-placement opened_at from place_slave_plan_legs —
            # never get_utc_now() here (post-fill overwrite breaks attribution).
            def _slave_leg_open_ts(fl: Any, role: str) -> Any:
                ts = getattr(fl, "opened_at", None)
                if ts is not None:
                    return ts
                log_and_buffer(
                    "SLAVE_LEG_TS_FALLBACK",
                    int(master_trade_id),
                    {
                        "leg": role,
                        "slave_id": int(slave.id),
                        "slave": str(slave.name or ""),
                        "note": "FilledEntryLeg.opened_at missing — "
                        "using get_utc_now() fallback",
                    },
                )
                return get_utc_now()

            for fl in filled_entry_legs:
                if fl.role == "wing_call":
                    wing_call_open_ts = _slave_leg_open_ts(fl, "wing_call")
                    # No exchange fill clock on OrderResult — placement time.
                    wing_call_fill_ts = wing_call_open_ts
                    wing_call_fill_px = float(fl.fill_price)
                    wing_call_oid = fl.order_id
                elif fl.role == "wing_put":
                    wing_put_open_ts = _slave_leg_open_ts(fl, "wing_put")
                    wing_put_fill_ts = wing_put_open_ts
                    wing_put_fill_px = float(fl.fill_price)
                    wing_put_oid = fl.order_id
                elif fl.role == "call":
                    call_open_ts = _slave_leg_open_ts(fl, "call")
                    call_fill_ts = call_open_ts
                    call_fill = float(fl.fill_price) or call_fill
                    call_order_id = fl.order_id
                    call_order = {"order_id": fl.order_id, "id": fl.order_id}
                    logger.info(
                        "Slave '%s' CALL placed: qty=%s fill=%s id=%s "
                        "bracket_sl=%s",
                        slave.name,
                        slave_qty,
                        call_fill,
                        call_order_id,
                        call_sl,
                    )
                    log_and_buffer(
                        "BRACKET_SL",
                        int(master_trade_id),
                        {
                            "leg": "call",
                            "slave": slave.name,
                            "stop_price": call_sl,
                            "source": "master_absolute",
                        },
                    )
                elif fl.role == "put":
                    put_open_ts = _slave_leg_open_ts(fl, "put")
                    put_fill_ts = put_open_ts
                    put_fill = float(fl.fill_price) or put_fill
                    put_order_id = fl.order_id
                    put_order = {"order_id": fl.order_id, "id": fl.order_id}
                    logger.info(
                        "Slave '%s' PUT placed: qty=%s fill=%s id=%s "
                        "bracket_sl=%s",
                        slave.name,
                        slave_qty,
                        put_fill,
                        put_order_id,
                        put_sl,
                    )
                    log_and_buffer(
                        "BRACKET_SL",
                        int(master_trade_id),
                        {
                            "leg": "put",
                            "slave": slave.name,
                            "stop_price": put_sl,
                            "source": "master_absolute",
                        },
                    )

            if call_fill <= 0:
                call_fill = float(master_call_fill or 0.0)
            if put_fill <= 0:
                put_fill = float(master_put_fill or 0.0)

            # Verify positions landed on slave Delta
            await asyncio.sleep(2)
            call_verified = False
            put_verified = False
            try:
                call_verified = await client.verify_position_exists(
                    int(call_product_id)
                )
                put_verified = await client.verify_position_exists(
                    int(put_product_id)
                )
            except Exception as exc:
                logger.warning(
                    "Slave '%s' post-entry verify failed: %s",
                    slave.name,
                    exc,
                )

            # Live sizes — verify can miss; also drives partial unwind size
            call_live_size: float | None = None
            put_live_size: float | None = None
            wing_call_live: float | None = None
            wing_put_live: float | None = None
            try:
                live_positions = await client.get_option_positions()
                call_live_size = self._position_size_for_product(
                    live_positions, int(call_product_id)
                )
                put_live_size = self._position_size_for_product(
                    live_positions, int(put_product_id)
                )
                if wings_on and wc_pid > 0:
                    wing_call_live = self._position_size_for_product(
                        live_positions, wc_pid
                    )
                if wings_on and wp_pid > 0:
                    wing_put_live = self._position_size_for_product(
                        live_positions, wp_pid
                    )
            except Exception as pos_exc:
                logger.warning(
                    "Slave '%s' post-entry positions fetch failed: %s",
                    slave.name,
                    pos_exc,
                )

            call_on_exchange = call_verified or (
                call_live_size is not None and abs(float(call_live_size)) > 1e-9
            )
            put_on_exchange = put_verified or (
                put_live_size is not None and abs(float(put_live_size)) > 1e-9
            )
            wing_call_on = (not wings_on) or (
                wing_call_live is not None
                and abs(float(wing_call_live)) > 1e-9
            )
            wing_put_on = (not wings_on) or (
                wing_put_live is not None and abs(float(wing_put_live)) > 1e-9
            )

            ledger_legs: list[dict[str, Any]] = []
            status = "error"
            last_error: str | None = None
            err_count = 1

            all_ok = (
                call_on_exchange
                and put_on_exchange
                and wing_call_on
                and wing_put_on
            )
            any_on = (
                call_on_exchange
                or put_on_exchange
                or (wings_on and (wing_call_on or wing_put_on))
            )

            if all_ok:
                status = "active"
                last_error = None
                err_count = 0
                logger.info(
                    "Slave '%s' both positions verified%s",
                    slave.name,
                    " (+wings)" if wings_on else "",
                )
            elif any_on:
                # Partial — unwind whatever landed (never leave naked short)
                to_unwind: list[dict[str, Any]] = []
                if call_on_exchange:
                    signed = (
                        float(call_live_size)
                        if call_live_size is not None
                        and abs(float(call_live_size)) > 1e-9
                        else -float(slave_qty)
                    )
                    to_unwind.append(
                        {
                            "leg": "call",
                            "product_id": int(call_product_id),
                            "signed_size": signed,
                            "opened_at": call_open_ts,
                            "fill_at": call_fill_ts,
                            "order_id": call_order_id,
                            "symbol": str(master_call_symbol or ""),
                            "strike": float(master_call_strike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
                if put_on_exchange:
                    signed = (
                        float(put_live_size)
                        if put_live_size is not None
                        and abs(float(put_live_size)) > 1e-9
                        else -float(slave_qty)
                    )
                    to_unwind.append(
                        {
                            "leg": "put",
                            "product_id": int(put_product_id),
                            "signed_size": signed,
                            "opened_at": put_open_ts,
                            "fill_at": put_fill_ts,
                            "order_id": put_order_id,
                            "symbol": str(master_put_symbol or ""),
                            "strike": float(master_put_strike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
                if wings_on and wing_call_on and wc_pid > 0:
                    signed = (
                        float(wing_call_live)
                        if wing_call_live is not None
                        and abs(float(wing_call_live)) > 1e-9
                        else float(slave_qty)
                    )
                    to_unwind.append(
                        {
                            "leg": "wing_call",
                            "product_id": wc_pid,
                            "signed_size": signed,
                            "opened_at": wing_call_open_ts or call_open_ts,
                            "fill_at": wing_call_fill_ts,
                            "order_id": wing_call_oid,
                            "symbol": str(wing_call_symbol or ""),
                            "strike": float(wing_call_strike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
                if wings_on and wing_put_on and wp_pid > 0:
                    signed = (
                        float(wing_put_live)
                        if wing_put_live is not None
                        and abs(float(wing_put_live)) > 1e-9
                        else float(slave_qty)
                    )
                    to_unwind.append(
                        {
                            "leg": "wing_put",
                            "product_id": wp_pid,
                            "signed_size": signed,
                            "opened_at": wing_put_open_ts or put_open_ts,
                            "fill_at": wing_put_fill_ts,
                            "order_id": wing_put_oid,
                            "symbol": str(wing_put_symbol or ""),
                            "strike": float(wing_put_strike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
                from backend.engine.slave_wings import sort_unwind_dicts

                to_unwind = sort_unwind_dicts(to_unwind)
                unwound_legs = await self._unwind_slave_entry_legs(
                    client=client,
                    slave=slave,
                    master_trade_id=int(master_trade_id),
                    legs_to_unwind=to_unwind,
                )
                ledger_legs = unwound_legs
                all_unwound = all(bool(x.get("unwound")) for x in unwound_legs)
                if all_unwound:
                    status = "error"
                    parts = [
                        (
                            f"{x['leg']} product={x['product_id']} "
                            f"unwound"
                        )
                        for x in unwound_legs
                    ]
                    last_error = (
                        "Partial fill closed: " + "; ".join(parts)
                    )[:500]
                else:
                    status = "partial_entry_open"
                    naked = [
                        x for x in unwound_legs if not x.get("unwound")
                    ]
                    parts = [
                        (
                            f"NAKED {x['leg']} product_id={x['product_id']} "
                            f"size={x.get('signed_size')} still OPEN"
                        )
                        for x in naked
                    ]
                    last_error = (
                        "Partial entry — customer has naked leg(s): "
                        + "; ".join(parts)
                    )[:500]
                logger.error(
                    "Slave '%s' PARTIAL position: call=%s put=%s "
                    "wings_ok=%s status=%s",
                    slave.name,
                    call_on_exchange,
                    put_on_exchange,
                    wing_call_on and wing_put_on,
                    status,
                )
            else:
                status = "error"
                last_error = "No positions found after placement"
                err_count = 1
                logger.error(
                    "Slave '%s' NO positions found after entry!",
                    slave.name,
                )

            call_entry_fee = (
                await self._resolve_order_fee(client, call_order)
                if call_order
                else 0.0
            )
            put_entry_fee = (
                await self._resolve_order_fee(client, put_order)
                if put_order
                else 0.0
            )
            entry_spread = (
                compute_entry_spread_usd(
                    sent_price=float(master_call_fill or 0),
                    fill_price=float(call_fill or 0),
                    quantity=slave_qty,
                    is_long=False,
                )
                + compute_entry_spread_usd(
                    sent_price=float(master_put_fill or 0),
                    fill_price=float(put_fill or 0),
                    quantity=slave_qty,
                    is_long=False,
                )
            )

            slave_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                call_order_id=call_order_id or None,
                put_order_id=put_order_id or None,
                call_sl_order_id=None,  # bracket — no separate stop order id
                put_sl_order_id=None,
                actual_quantity=slave_qty,
                original_quantity=slave_qty,
                call_fill_price=call_fill,
                put_fill_price=put_fill,
                call_product_id=int(call_product_id),
                put_product_id=int(put_product_id),
                call_symbol=str(master_call_symbol or ""),
                put_symbol=str(master_put_symbol or ""),
                call_strike=float(master_call_strike or 0),
                put_strike=float(master_put_strike or 0),
                wing_call_product_id=wc_pid if wings_on else None,
                wing_put_product_id=wp_pid if wings_on else None,
                wing_call_symbol=(
                    str(wing_call_symbol or "") if wings_on else None
                ),
                wing_put_symbol=(
                    str(wing_put_symbol or "") if wings_on else None
                ),
                wing_call_strike=(
                    float(wing_call_strike or 0) if wings_on else None
                ),
                wing_put_strike=(
                    float(wing_put_strike or 0) if wings_on else None
                ),
                wing_call_order_id=wing_call_oid if wings_on else None,
                wing_put_order_id=wing_put_oid if wings_on else None,
                wing_call_fill_price=(
                    wing_call_fill_px if wings_on else None
                ),
                wing_put_fill_price=(
                    wing_put_fill_px if wings_on else None
                ),
                call_entry_fee_usd=float(call_entry_fee or 0.0),
                put_entry_fee_usd=float(put_entry_fee or 0.0),
                entry_spread_usd=float(entry_spread or 0.0),
                status=status,
                last_error=last_error,
                error_count=err_count,
            )
            db.add(slave_trade)
            db.flush()

            if status == "active":
                slave.connection_status = "connected"
                slave.last_error = None
            else:
                slave.connection_status = "error"
                slave.last_error = last_error
            slave.last_connected_at = get_utc_now()
            slave.updated_at = get_utc_now()
            db.commit()

            # Ledger: always record legs that actually filled (not gated on active)
            try:
                if status == "active":
                    from backend.engine.structure_ledger import (
                        record_slave_basket_entry,
                    )
                    from backend.models import Trade as MasterTrade

                    master_row = (
                        db.query(MasterTrade)
                        .filter(MasterTrade.id == int(master_trade_id))
                        .first()
                    )
                    if master_row is not None:
                        record_slave_basket_entry(
                            db,
                            slave_trade=slave_trade,
                            slave_account_id=int(slave.id),
                            master_trade=master_row,
                            call_opened_at=call_open_ts,
                            put_opened_at=put_open_ts,
                            call_fill_at=call_fill_ts,
                            put_fill_at=put_fill_ts,
                        )
                        db.commit()
                elif ledger_legs:
                    await self._ledger_slave_basket_legs(
                        db,
                        slave_account_id=int(slave.id),
                        master_trade_id=int(master_trade_id),
                        slave_trade=slave_trade,
                        legs=ledger_legs,
                    )
                    db.commit()
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave basket entry failed: %s",
                    ledger_exc,
                    exc_info=True,
                )

            self._log_slave_trade_detail(
                slave_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                qty=slave_qty,
                call_symbol=str(master_call_symbol or ""),
                call_fill=float(call_fill or 0),
                put_symbol=str(master_put_symbol or ""),
                put_fill=float(put_fill or 0),
                entry_spread=float(entry_spread or 0.0),
                entry_fees=float(call_entry_fee or 0) + float(put_entry_fee or 0),
            )

            logger.info(
                "Slave '%s' trade mirrored status=%s (expiry=%s)",
                slave.name,
                status,
                expiry_date,
            )

        except Exception as exc:
            logger.error(
                "Slave '%s' mirror FAILED: %s",
                slave.name,
                exc,
                exc_info=True,
            )
            # Call may already be filled when put place_order raises — unwind live
            exception_ledger_legs: list[dict[str, Any]] = []
            unwind_status = "error"
            unwind_error = str(exc)[:500]
            try:
                positions = await client.get_option_positions()
            except Exception:
                positions = []
            to_unwind_exc: list[dict[str, Any]] = []
            call_pid_i = int(call_product_id)
            put_pid_i = int(put_product_id)
            if "call_open_ts" in locals() or "call_order" in locals():
                clive = self._position_size_for_product(positions, call_pid_i)
                if clive is not None and abs(float(clive)) > 1e-9:
                    to_unwind_exc.append(
                        {
                            "leg": "call",
                            "product_id": call_pid_i,
                            "signed_size": float(clive),
                            "opened_at": locals().get("call_open_ts")
                            or get_utc_now(),
                            "fill_at": locals().get("call_fill_ts"),
                            "order_id": locals().get("call_order_id"),
                            "symbol": str(master_call_symbol or ""),
                            "strike": float(master_call_strike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
            if "put_open_ts" in locals() or "put_order" in locals():
                plive = self._position_size_for_product(positions, put_pid_i)
                if plive is not None and abs(float(plive)) > 1e-9:
                    to_unwind_exc.append(
                        {
                            "leg": "put",
                            "product_id": put_pid_i,
                            "signed_size": float(plive),
                            "opened_at": locals().get("put_open_ts")
                            or get_utc_now(),
                            "fill_at": locals().get("put_fill_ts"),
                            "order_id": locals().get("put_order_id"),
                            "symbol": str(master_put_symbol or ""),
                            "strike": float(master_put_strike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
            # Also unwind any filled wings (shorts-first sort inside unwind)
            for wrole, wpid_key, wsym, wstrike in (
                (
                    "wing_call",
                    "wc_pid",
                    wing_call_symbol,
                    wing_call_strike,
                ),
                (
                    "wing_put",
                    "wp_pid",
                    wing_put_symbol,
                    wing_put_strike,
                ),
            ):
                wpid = int(locals().get(wpid_key) or 0)
                if wpid <= 0:
                    continue
                wlive = self._position_size_for_product(positions, wpid)
                if wlive is not None and abs(float(wlive)) > 1e-9:
                    to_unwind_exc.append(
                        {
                            "leg": wrole,
                            "product_id": wpid,
                            "signed_size": float(wlive),
                            "opened_at": get_utc_now(),
                            "fill_at": get_utc_now(),
                            "order_id": None,
                            "symbol": str(wsym or ""),
                            "strike": float(wstrike or 0),
                            "quantity": int(slave_qty),
                        }
                    )
            if to_unwind_exc:
                try:
                    exception_ledger_legs = await self._unwind_slave_entry_legs(
                        client=client,
                        slave=slave,
                        master_trade_id=int(master_trade_id),
                        legs_to_unwind=to_unwind_exc,
                    )
                    if all(
                        bool(x.get("unwound")) for x in exception_ledger_legs
                    ):
                        unwind_status = "error"
                        unwind_error = (
                            f"Entry exception after fill — unwound: {exc}"
                        )[:500]
                    else:
                        unwind_status = "partial_entry_open"
                        naked = [
                            x
                            for x in exception_ledger_legs
                            if not x.get("unwound")
                        ]
                        parts = [
                            (
                                f"NAKED {x['leg']} product_id={x['product_id']} "
                                f"size={x.get('signed_size')} still OPEN"
                            )
                            for x in naked
                        ]
                        unwind_error = (
                            f"Entry exception — naked leg(s) remain: "
                            f"{'; '.join(parts)} | {exc}"
                        )[:500]
                except Exception as uw_exc:
                    unwind_status = "partial_entry_open"
                    unwind_error = (
                        f"Entry exception + unwind failed: {exc} / {uw_exc}"
                    )[:500]

            try:
                db.rollback()
            except Exception:
                pass
            slave.connection_status = "error"
            slave.last_error = unwind_error
            slave.updated_at = get_utc_now()
            failed_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                actual_quantity=slave_qty,
                call_product_id=int(call_product_id),
                put_product_id=int(put_product_id),
                call_symbol=str(master_call_symbol or ""),
                put_symbol=str(master_put_symbol or ""),
                call_strike=float(master_call_strike or 0),
                put_strike=float(master_put_strike or 0),
                call_order_id=locals().get("call_order_id") or None,
                put_order_id=locals().get("put_order_id") or None,
                call_fill_price=locals().get("call_fill"),
                put_fill_price=locals().get("put_fill"),
                status=unwind_status,
                last_error=unwind_error,
                error_count=1,
            )
            db.add(failed_trade)
            db.flush()
            if exception_ledger_legs:
                await self._ledger_slave_basket_legs(
                    db,
                    slave_account_id=int(slave.id),
                    master_trade_id=int(master_trade_id),
                    slave_trade=failed_trade,
                    legs=exception_ledger_legs,
                )
            db.commit()

        finally:
            await client.close()

    async def mirror_adjustment(
        self,
        master_trade_id: int,
        triggered_leg_type: str,
        old_product_id: int,
        new_product_id: int,
        new_symbol: str,
        new_strike: float,
        master_qty: int,
        universal_sl_pct: float | None = None,
        master_bracket_sl: float | None = None,
    ) -> None:
        """
        Mirror an adjustment on all slaves.
        Close old leg, open new leg — atomic verify-close-verify.

        master_bracket_sl: absolute stop from master's new-leg fill (verbatim).
        """
        with self.db_factory() as db:
            uni_sl = float(universal_sl_pct) if universal_sl_pct else 0.0
            if uni_sl <= 0:
                uni_sl = self._master_universal_sl_pct(db, master_trade_id)

            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == master_trade_id,
                    SlaveTrade.status == "active",
                )
                .all()
            )

            log_and_buffer(
                "MIRROR_ADJ_ENGINE",
                master_trade_id,
                {
                    "slaves_found": len(slave_trades),
                    "master_bracket_sl": master_bracket_sl,
                },
            )
            logger.info(
                "[MIRROR_ADJ_ENGINE] Trade#%s slaves found=%s uni_sl=%.1f%% "
                "master_bracket_sl=%s",
                master_trade_id,
                len(slave_trades),
                uni_sl,
                master_bracket_sl,
            )

            if not slave_trades:
                logger.info(
                    "No active slave trades for master %s", master_trade_id
                )
                return

            logger.info(
                "Mirroring adjustment to %s slaves: leg=%s new_product=%s "
                "(master_qty=%s symbol=%s)",
                len(slave_trades),
                triggered_leg_type,
                new_product_id,
                master_qty,
                new_symbol,
            )

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if not slave or not slave.is_active:
                    continue

                async with self._slave_op_lock(
                    int(slave.id), "mirror_adjustment"
                ) as acquired:
                    if not acquired:
                        continue
                    await self._mirror_adjustment_to_slave(
                        slave=slave,
                        slave_trade=slave_trade,
                        triggered_leg_type=triggered_leg_type,
                        old_product_id=old_product_id,
                        new_product_id=new_product_id,
                        new_symbol=new_symbol,
                        new_strike=new_strike,
                        db=db,
                        universal_sl_pct=uni_sl,
                        master_bracket_sl=master_bracket_sl,
                    )

    def _master_universal_sl_pct(
        self, db: Any, master_trade_id: int
    ) -> float:
        try:
            row = (
                db.query(Trade)
                .filter(Trade.id == int(master_trade_id))
                .first()
            )
            return float(getattr(row, "universal_sl_pct", None) or 200.0)
        except Exception:
            return 200.0

    @staticmethod
    def _position_size_for_product(
        positions: list[dict[str, Any]], product_id: int
    ) -> float | None:
        """
        Return live signed size for product_id, or None if absent.

        Not a sizing gate — lookup only. Zero/None here means book state,
        not a slave entry skip (those use SLAVE_SIZING_ZERO).
        """
        wanted = int(product_id)
        for pos in positions or []:
            try:
                pid = int(pos.get("product_id") or 0)
            except (TypeError, ValueError):
                continue
            if pid == wanted:
                try:
                    return float(pos.get("size") or 0)
                except (TypeError, ValueError):
                    return 0.0
        return None

    @staticmethod
    def _resolve_basket_exit_closed_at(
        exit_by_pid: dict[int, dict[str, Any]],
        product_id: int,
        *,
        exit_batch_ts: Any,
    ) -> Any:
        """
        closed_at for a basket leg on exit.

        Prefer this-pass close timestamp; else last known fill_at from the
        same exit map; else the exit-batch timestamp captured BEFORE any
        close orders (already-flat / bracket-SL legs).
        """
        pid = int(product_id or 0)
        if pid <= 0:
            return exit_batch_ts
        meta = exit_by_pid.get(pid) or {}
        closed = meta.get("closed_at")
        if closed is not None:
            return closed
        fill_at = meta.get("fill_at")
        if fill_at is not None:
            return fill_at
        return exit_batch_ts

    @staticmethod
    def _is_reduce_only_unsupported(exc: BaseException) -> bool:
        """True when Delta rejects reduce_only as unsupported for the account."""
        msg = str(exc).lower()
        needles = (
            "reduce_only",
            "reduce only",
            "reduce-only",
            "invalid reduce",
            "unsupported reduce",
        )
        return any(n in msg for n in needles)

    async def _close_with_reduce_only(
        self,
        *,
        client: DeltaClient,
        slave: SlaveAccount,
        product_id: int,
        signed_size: float,
        master_trade_id: int = 0,
        path: str = "",
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
    ) -> tuple[bool, dict[str, Any] | None, str]:
        """
        Close a position with reduce_only=True only. Never drops the flag.

        After every accepted order, re-read live size. Success only when the
        product is flat (IOC accept ≠ fill on a thin book). Still open →
        retry with live size + reduce_only (max ``max_retries``).
        On exception: same re-read — flat → treat as success; open → retry.
        If Delta rejects reduce_only specifically: leave position, do not open
        an opposite naked order.

        Returns (ok, last_order_or_None, error_or_empty).
        """
        pid = int(product_id)
        slave_id = int(getattr(slave, "id", 0) or 0)
        live_size = float(signed_size)
        last_order: dict[str, Any] | None = None
        last_err = ""

        if abs(live_size) <= 1e-9:
            return True, None, ""

        for attempt in range(0, int(max_retries) + 1):
            close_size = max(1, abs(int(round(live_size))))
            side = "buy" if live_size < 0 else "sell"
            order_accepted = False
            try:
                last_order = await client.place_order(
                    product_id=pid,
                    size=close_size,
                    side=side,
                    reduce_only=True,
                )
                order_accepted = True
            except Exception as close_exc:
                last_err = str(close_exc)
                if self._is_reduce_only_unsupported(close_exc):
                    logger.error(
                        "[SLAVE_CLOSE] slave=%s product_id=%s reduce_only "
                        "NOT SUPPORTED on this account — leaving position "
                        "untouched (will NOT place without reduce_only) "
                        "err=%s path=%s",
                        slave_id,
                        pid,
                        close_exc,
                        path,
                    )
                    log_and_buffer(
                        "SLAVE_CLOSE_REDUCE_ONLY_UNSUPPORTED",
                        int(master_trade_id or 0),
                        {
                            "slave": slave_id,
                            "product_id": pid,
                            "error": last_err[:300],
                            "path": path,
                        },
                    )
                    return False, None, "reduce_only_unsupported"

            # Always re-read: accepted IOC can cancel unfilled
            await asyncio.sleep(float(backoff_seconds))
            live_before = live_size
            try:
                positions = await client.get_option_positions()
            except Exception as pos_exc:
                last_err = (
                    f"close_ok_but_positions_fetch_failed: {pos_exc}"
                    if order_accepted
                    else (
                        f"close_failed then positions fetch failed: "
                        f"{last_err} / {pos_exc}"
                    )
                )
                log_and_buffer(
                    "SLAVE_CLOSE_RETRY",
                    int(master_trade_id or 0),
                    {
                        "slave": slave_id,
                        "product_id": pid,
                        "attempt": attempt + 1,
                        "live_size_before": live_before,
                        "reason": "positions_fetch_failed",
                        "path": path,
                    },
                )
                if attempt >= int(max_retries):
                    return False, last_order, last_err
                continue

            rechecked = self._position_size_for_product(positions, pid)
            if rechecked is None or abs(float(rechecked)) <= 1e-9:
                log_and_buffer(
                    "SLAVE_CLOSE_RETRY",
                    int(master_trade_id or 0),
                    {
                        "slave": slave_id,
                        "product_id": pid,
                        "attempt": attempt + 1,
                        "live_size_before": live_before,
                        "reason": (
                            "flat_verified"
                            if order_accepted
                            else "flat_after_exception_treat_success"
                        ),
                        "path": path,
                    },
                )
                return True, last_order, ""

            live_size = float(rechecked)
            if order_accepted:
                last_err = (
                    f"order_accepted_but_not_flat live_size={live_size}"
                )
            reason = f"still_open:{last_err[:120]}"
            log_and_buffer(
                "SLAVE_CLOSE_RETRY",
                int(master_trade_id or 0),
                {
                    "slave": slave_id,
                    "product_id": pid,
                    "attempt": attempt + 1,
                    "live_size_before": live_size,
                    "reason": reason,
                    "path": path,
                },
            )
            if attempt >= int(max_retries):
                return False, last_order, (
                    f"close_not_flat after {max_retries} retries: {last_err}"
                )

        return False, last_order, last_err or "close_failed"

    async def _ledger_slave_basket_legs(
        self,
        db: Any,
        *,
        slave_account_id: int,
        master_trade_id: int,
        slave_trade: SlaveTrade,
        legs: list[dict[str, Any]],
    ) -> None:
        """
        Record StructureLeg rows for legs that actually filled.

        Each item in ``legs``:
          leg: "call"|"put"
          opened_at, fill_at, product_id, order_id, symbol, strike, quantity
          closed_at: optional — set when unwound
          close_reason: optional
        """
        if not legs:
            return
        try:
            from backend.engine.structure_ledger import (
                KIND_SLAVE,
                ROLE_BASKET_CALL,
                ROLE_BASKET_PUT,
                close_leg,
                get_active_structure,
                open_leg,
            )
            from backend.models import Trade as MasterTrade

            master_row = (
                db.query(MasterTrade)
                .filter(MasterTrade.id == int(master_trade_id))
                .first()
            )
            if master_row is None:
                return
            hid = getattr(master_row, "hedge_position_id", None)
            if hid is None:
                logger.error(
                    "[LEDGER_MISS] slave=%s reason=no_hedge_position_id -- "
                    "basket entry NOT recorded",
                    slave_account_id,
                )
                return
            struct = get_active_structure(
                db,
                hedge_position_id=int(hid),
                account_kind=KIND_SLAVE,
                slave_account_id=int(slave_account_id),
            )
            if struct is None:
                logger.error(
                    "[LEDGER_MISS] slave=%s reason=no_active_structure -- "
                    "basket entry NOT recorded",
                    slave_account_id,
                )
                return
            basket_seq = getattr(master_row, "basket_seq_in_structure", None)
            bs = int(basket_seq) if basket_seq is not None else None
            for item in legs:
                from backend.engine.slave_wings import ledger_role_for_slave_leg

                role, side = ledger_role_for_slave_leg(
                    str(item.get("leg") or "")
                )
                if not role:
                    continue
                pid = int(item.get("product_id") or 0)
                opened_at = item.get("opened_at")
                if pid <= 0 or opened_at is None:
                    continue
                row = open_leg(
                    db,
                    structure=struct,
                    leg_role=role,
                    product_id=pid,
                    side=side,
                    quantity=abs(int(item.get("quantity") or 1)),
                    symbol=item.get("symbol"),
                    strike=item.get("strike"),
                    basket_seq=bs,
                    adj_seq=0,
                    entry_order_id=item.get("order_id"),
                    opened_at=opened_at,
                    fill_at=item.get("fill_at"),
                )
                closed_at = item.get("closed_at")
                if closed_at is not None:
                    close_leg(
                        db,
                        row,
                        reason=str(item.get("close_reason") or "PARTIAL_UNWIND"),
                        closed_at=closed_at,
                        structure=struct,
                        fill_at=item.get("unwind_fill_at"),
                    )
            # Keep slave_trade product ids in sync for ownership helper
            for item in legs:
                lt = str(item.get("leg") or "").lower()
                pid = int(item.get("product_id") or 0)
                if lt == "call" and pid > 0:
                    slave_trade.call_product_id = pid
                    if item.get("order_id"):
                        slave_trade.call_order_id = str(item["order_id"])
                    if item.get("symbol"):
                        slave_trade.call_symbol = str(item["symbol"])
                    if item.get("strike") is not None:
                        slave_trade.call_strike = float(item["strike"])
                elif lt == "put" and pid > 0:
                    slave_trade.put_product_id = pid
                    if item.get("order_id"):
                        slave_trade.put_order_id = str(item["order_id"])
                    if item.get("symbol"):
                        slave_trade.put_symbol = str(item["symbol"])
                    if item.get("strike") is not None:
                        slave_trade.put_strike = float(item["strike"])
                elif lt == "wing_call" and pid > 0:
                    slave_trade.wing_call_product_id = pid
                    if item.get("order_id"):
                        slave_trade.wing_call_order_id = str(item["order_id"])
                    if item.get("symbol"):
                        slave_trade.wing_call_symbol = str(item["symbol"])
                    if item.get("strike") is not None:
                        slave_trade.wing_call_strike = float(item["strike"])
                elif lt == "wing_put" and pid > 0:
                    slave_trade.wing_put_product_id = pid
                    if item.get("order_id"):
                        slave_trade.wing_put_order_id = str(item["order_id"])
                    if item.get("symbol"):
                        slave_trade.wing_put_symbol = str(item["symbol"])
                    if item.get("strike") is not None:
                        slave_trade.wing_put_strike = float(item["strike"])
            db.flush()
        except Exception as ledger_exc:
            logger.error(
                "structure ledger partial/full slave basket legs failed: %s",
                ledger_exc,
                exc_info=True,
            )

    async def _unwind_slave_entry_legs(
        self,
        *,
        client: DeltaClient,
        slave: SlaveAccount,
        master_trade_id: int,
        legs_to_unwind: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Unwind filled short entry legs with _close_with_reduce_only.

        ``legs_to_unwind`` items need: leg, product_id, signed_size,
        opened_at, fill_at, order_id, symbol, strike, quantity.

        Returns updated leg dicts with unwound=True/False and closed_at set
        when unwind succeeded.
        """
        from backend.engine.slave_wings import sort_unwind_dicts

        results: list[dict[str, Any]] = []
        for item in sort_unwind_dicts(legs_to_unwind):
            pid = int(item.get("product_id") or 0)
            signed = float(item.get("signed_size") or 0)
            leg_name = str(item.get("leg") or "")
            if pid <= 0:
                continue
            if abs(signed) <= 1e-9:
                signed = -abs(float(item.get("quantity") or 1))
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            unwind_closed_at = get_utc_now()
            ok, _order, err = await self._close_with_reduce_only(
                client=client,
                slave=slave,
                product_id=pid,
                signed_size=signed,
                master_trade_id=int(master_trade_id),
                path="partial_basket_entry_unwind",
            )
            out = dict(item)
            out["unwound"] = bool(ok)
            out["unwind_error"] = err if not ok else ""
            if ok:
                out["closed_at"] = unwind_closed_at
                out["close_reason"] = "PARTIAL_UNWIND"
            log_and_buffer(
                "SLAVE_PARTIAL_ENTRY",
                int(master_trade_id),
                {
                    "slave": int(slave.id),
                    "filled_leg": leg_name,
                    "product_id": pid,
                    "size": signed,
                    "unwound": "yes" if ok else "no",
                    "reason": (
                        "unwind_ok"
                        if ok
                        else f"unwind_failed:{err[:120]}"
                    ),
                },
            )
            results.append(out)
        return results

    async def _mirror_adjustment_to_slave(
        self,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        triggered_leg_type: str,
        old_product_id: int,
        new_product_id: int,
        new_symbol: str,
        new_strike: float,
        db: Any,
        universal_sl_pct: float = 200.0,
        master_bracket_sl: float | None = None,
    ) -> None:
        # Caller MUST hold the per-slave lock.
        # Virtual mode: track adjustment in DB but don't place real orders
        if bool(getattr(slave, "is_virtual", False)):
            logger.info(
                "VIRTUAL ADJUSTMENT: slave='%s' skipping real orders",
                slave.name,
            )
            with self.db_factory() as virt_db:
                st = (
                    virt_db.query(SlaveTrade)
                    .filter(
                        SlaveTrade.slave_account_id == slave.id,
                        SlaveTrade.master_trade_id
                        == slave_trade.master_trade_id,
                        SlaveTrade.status == "active",
                    )
                    .first()
                )
                if st:
                    leg = str(triggered_leg_type).lower()
                    # Captured BEFORE any order — this is an attribution window bound.
                    # See e3e6b7d: a post-fill timestamp silently drops the fill.
                    virt_adj_close_ts = get_utc_now()
                    try:
                        from backend.engine.structure_ledger import (
                            record_slave_adjustment,
                        )
                        from backend.models import Trade as MasterTrade

                        master_row = (
                            virt_db.query(MasterTrade)
                            .filter(
                                MasterTrade.id
                                == int(st.master_trade_id or 0)
                            )
                            .first()
                        )
                        record_slave_adjustment(
                            virt_db,
                            slave_trade=st,
                            slave_account_id=int(slave.id),
                            master_trade=master_row,
                            triggered_leg=leg,
                            new_product_id=int(new_product_id),
                            new_symbol=str(new_symbol or ""),
                            new_strike=float(new_strike or 0),
                            new_order_id="VIRTUAL",
                            reason="ADJUSTMENT",
                            old_leg_closed_at=virt_adj_close_ts,
                            new_leg_opened_at=virt_adj_close_ts,
                            old_leg_fill_at=virt_adj_close_ts,
                            new_leg_fill_at=virt_adj_close_ts,
                        )
                    except Exception as ledger_exc:
                        logger.error(
                            "structure ledger slave adjustment failed: %s",
                            ledger_exc,
                            exc_info=True,
                        )
                    if leg == "call":
                        st.call_order_id = "VIRTUAL"
                        st.call_product_id = int(new_product_id)
                        st.call_symbol = str(new_symbol or "")
                        st.call_strike = float(new_strike or 0)
                    else:
                        st.put_order_id = "VIRTUAL"
                        st.put_product_id = int(new_product_id)
                        st.put_symbol = str(new_symbol or "")
                        st.put_strike = float(new_strike or 0)
                    virt_db.commit()
            return

        client = self._get_slave_client(slave)
        stored_qty = max(1, int(slave_trade.actual_quantity or 1))
        leg = str(triggered_leg_type).lower()
        uni_sl = float(universal_sl_pct or 200.0)
        if uni_sl <= 0:
            uni_sl = 200.0
        old_pid = int(old_product_id)
        new_pid = int(new_product_id)

        try:
            # Cancel legacy separate SL only if present (bracket auto-cancels)
            if leg == "call" and slave_trade.call_sl_order_id:
                oid = str(slave_trade.call_sl_order_id)
                if not oid.startswith("ABS:"):
                    try:
                        await client.cancel_order(int(oid))
                    except Exception as exc:
                        logger.warning("Slave SL cancel failed: %s", exc)
                slave_trade.call_sl_order_id = None
            elif leg == "put" and slave_trade.put_sl_order_id:
                oid = str(slave_trade.put_sl_order_id)
                if not oid.startswith("ABS:"):
                    try:
                        await client.cancel_order(int(oid))
                    except Exception as exc:
                        logger.warning("Slave SL cancel failed: %s", exc)
                slave_trade.put_sl_order_id = None

            # --- a. Live positions ---
            live_positions = await client.get_option_positions()
            live_size = self._position_size_for_product(
                live_positions, old_pid
            )
            logger.info(
                "[MIRROR_ADJ_VERIFY] slave='%s' stage=pre_close "
                "product_id=%s live_size=%s stored_qty=%s",
                slave.name,
                old_pid,
                live_size,
                stored_qty,
            )
            log_and_buffer(
                "MIRROR_ADJ_VERIFY",
                int(slave_trade.master_trade_id),
                {
                    "slave": slave.name,
                    "stage": "pre_close",
                    "product_id": old_pid,
                    "live_size": live_size,
                    "stored_qty": stored_qty,
                },
            )

            # --- b/c. Close old leg with reduce_only using LIVE size ---
            old_leg_closed_ts = None
            old_leg_close_fill_ts = None
            new_leg_open_ts = None
            new_leg_fill_ts = None
            new_order_id: str | None = None
            if live_size is None or live_size == 0:
                logger.info(
                    "[MIRROR_ADJ_SKIP] slave='%s' already_flat "
                    "old_product=%s — skip close, open new leg",
                    slave.name,
                    old_pid,
                )
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                old_leg_closed_ts = get_utc_now()
            else:
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                old_leg_closed_ts = get_utc_now()
                ok_close, close_order, close_err = await self._close_with_reduce_only(
                    client=client,
                    slave=slave,
                    product_id=old_pid,
                    signed_size=float(live_size),
                    master_trade_id=int(slave_trade.master_trade_id or 0),
                    path="mirror_adjustment_close_old",
                )
                old_leg_close_fill_ts = get_utc_now()
                logger.info(
                    "[MIRROR_ADJ_VERIFY] slave='%s' stage=close_sent "
                    "product_id=%s size=%s ok=%s order_id=%s err=%s",
                    slave.name,
                    old_pid,
                    abs(int(live_size)),
                    ok_close,
                    self._order_id(close_order) if close_order else None,
                    close_err,
                )

                await asyncio.sleep(2)
                post_close = await client.get_option_positions()
                still = self._position_size_for_product(post_close, old_pid)
                logger.info(
                    "[MIRROR_ADJ_VERIFY] slave='%s' stage=post_close "
                    "product_id=%s expected=0 actual_size=%s",
                    slave.name,
                    old_pid,
                    still,
                )
                log_and_buffer(
                    "MIRROR_ADJ_VERIFY",
                    int(slave_trade.master_trade_id),
                    {
                        "slave": slave.name,
                        "stage": "post_close",
                        "product_id": old_pid,
                        "actual_size": still,
                    },
                )
                if (not ok_close) or (
                    still is not None and abs(float(still)) > 0
                ):
                    msg = (
                        f"adjust_close_failed: old product {old_pid} "
                        f"still open size={still} after close "
                        f"err={close_err}"
                    )
                    logger.critical(
                        "[MIRROR_ADJ_VERIFY] slave='%s' %s — ABORT new leg",
                        slave.name,
                        msg,
                    )
                    slave_trade.status = "adjust_close_failed"
                    slave_trade.last_error = msg[:500]
                    slave_trade.error_count = (
                        int(slave_trade.error_count or 0) + 1
                    )
                    slave_trade.last_updated = get_utc_now()
                    slave.connection_status = "error"
                    slave.last_error = msg[:500]
                    log_and_buffer(
                        "SLAVE_ADJ_LEDGER",
                        int(slave_trade.master_trade_id or 0),
                        {
                            "slave": int(slave.id),
                            "old_pid": old_pid,
                            "old_closed": "no",
                            "new_pid": new_pid,
                            "new_opened": "no",
                            "outcome": "error",
                        },
                    )
                    db.commit()
                    return

            # Old leg flat on exchange — close ledger window NOW (before new open)
            from backend.engine.structure_ledger import (
                record_slave_adjustment_close,
                record_slave_adjustment_open,
                set_structure_attribution_warning,
            )
            from backend.models import Trade as MasterTrade

            master_row = (
                db.query(MasterTrade)
                .filter(
                    MasterTrade.id == int(slave_trade.master_trade_id or 0)
                )
                .first()
            )
            old_closed_ok = record_slave_adjustment_close(
                db,
                slave_account_id=int(slave.id),
                master_trade=master_row,
                triggered_leg=leg,
                reason="ADJUSTMENT",
                old_leg_closed_at=old_leg_closed_ts,
                old_leg_fill_at=old_leg_close_fill_ts,
                old_product_id=old_pid,
            )
            db.commit()

            # Close size for new entry: prefer live abs size, else stored.
            # decrease_step uses THIS slave's original_quantity (not master).
            entry_qty = (
                max(1, abs(int(live_size)))
                if live_size is not None and live_size != 0
                else stored_qty
            )
            try:
                from backend.database import get_or_create_auto_settings
                from backend.engine.wing_entry import (
                    compute_decrease_step_qty,
                    resolve_adjustment_qty_mode,
                )
                from backend.models import Adjustment

                settings = get_or_create_auto_settings(db)
                qty_mode = resolve_adjustment_qty_mode(settings)
                if qty_mode == "decrease_step":
                    orig = int(
                        getattr(slave_trade, "original_quantity", None)
                        or slave_trade.actual_quantity
                        or stored_qty
                        or 1
                    )
                    adj_n = (
                        db.query(Adjustment)
                        .filter(
                            Adjustment.trade_id
                            == int(slave_trade.master_trade_id or 0)
                        )
                        .count()
                    )
                    adj_n = max(1, int(adj_n))
                    dec_pct = float(
                        getattr(settings, "adjustment_qty_decrease_pct", None)
                        or 10.0
                    )
                    new_q, close_basket = compute_decrease_step_qty(
                        original_qty=orig,
                        adjustment_number=adj_n,
                        decrease_pct=dec_pct,
                    )
                    if close_basket:
                        logger.warning(
                            "[MIRROR_ADJ] slave='%s' decrease_step remaining<=0 "
                            "— keeping entry_qty=%s (basket close is master-driven)",
                            slave.name,
                            entry_qty,
                        )
                    elif new_q is not None:
                        entry_qty = max(1, int(new_q))
                        logger.info(
                            "[MIRROR_ADJ] slave='%s' decrease_step "
                            "orig=%s adj_n=%s → entry_qty=%s (wings untouched)",
                            slave.name,
                            orig,
                            adj_n,
                            entry_qty,
                        )
            except Exception as qty_exc:
                logger.warning(
                    "[MIRROR_ADJ] decrease_step sizing skipped: %s",
                    qty_exc,
                )

            # --- d. Open new leg; bracket SL = master's absolute stop ---
            # Wings are NEVER closed or resized on adjustment.
            new_sl = None
            new_sl_limit = None
            if master_bracket_sl is not None and float(master_bracket_sl) > 0:
                new_sl = round(float(master_bracket_sl), 2)
                new_sl_limit = round(new_sl * 1.05, 2)
            else:
                # Fallback only if caller omitted absolute — still from mark
                # is wrong; leave None and log CRITICAL.
                logger.critical(
                    "[BRACKET_SL] slave='%s' mirror_adjustment missing "
                    "master_bracket_sl — placing without bracket",
                    slave.name,
                )

            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            new_leg_open_ts = get_utc_now()
            new_order = await client.place_order(
                product_id=new_pid,
                size=entry_qty,
                side="sell",
                bracket_stop_loss_price=new_sl,
                bracket_stop_loss_limit_price=new_sl_limit,
            )
            new_leg_fill_ts = get_utc_now()
            new_fill = float(
                await client.resolve_fill_price(
                    new_order, symbol_for_fallback=new_symbol
                )
                or 0.0
            )
            new_order_id = self._order_id(new_order)

            logger.info(
                "Slave '%s' opened new %s: strike=%s fill=%s id=%s "
                "bracket_sl=%s (master_absolute) qty=%s",
                slave.name,
                leg,
                new_strike,
                new_fill,
                new_order_id,
                new_sl,
                entry_qty,
            )
            log_and_buffer(
                "BRACKET_SL",
                int(slave_trade.master_trade_id),
                {
                    "leg": leg,
                    "slave": slave.name,
                    "stop_price": new_sl,
                    "source": "master_absolute",
                    "stage": "adjustment",
                },
            )

            await asyncio.sleep(2)
            post_entry = await client.get_option_positions()
            new_live = self._position_size_for_product(post_entry, new_pid)
            logger.info(
                "[MIRROR_ADJ_VERIFY] slave='%s' stage=post_entry "
                "product_id=%s expected_short_qty=%s actual_size=%s",
                slave.name,
                new_pid,
                entry_qty,
                new_live,
            )
            log_and_buffer(
                "MIRROR_ADJ_VERIFY",
                int(slave_trade.master_trade_id),
                {
                    "slave": slave.name,
                    "stage": "post_entry",
                    "product_id": new_pid,
                    "expected_qty": entry_qty,
                    "actual_size": new_live,
                },
            )

            if new_live is None or abs(float(new_live)) <= 0:
                msg = (
                    f"partial_adjustment: old leg {old_pid} closed but "
                    f"new product {new_pid} missing on Delta"
                )
                logger.critical(
                    "[MIRROR_ADJ_VERIFY] slave='%s' %s",
                    slave.name,
                    msg,
                )
                logger.error(
                    "[LEDGER_MISS] slave=%s structure=active "
                    "old_pid=%s new_pid=%s reason=partial_adjustment -- "
                    "old closed, new missing",
                    slave.id,
                    old_pid,
                    new_pid,
                )
                set_structure_attribution_warning(
                    db,
                    slave_account_id=int(slave.id),
                    master_trade=master_row,
                    warning=(
                        "partial_adjustment: old leg closed, new leg missing"
                    ),
                )
                slave_trade.status = "partial_adjustment"
                slave_trade.last_error = msg[:500]
                slave_trade.error_count = (
                    int(slave_trade.error_count or 0) + 1
                )
                slave_trade.last_updated = get_utc_now()
                if leg == "call":
                    slave_trade.call_order_id = new_order_id or None
                    slave_trade.call_product_id = new_pid
                    slave_trade.call_symbol = str(new_symbol or "")
                    slave_trade.call_strike = float(new_strike or 0)
                    if new_fill > 0:
                        slave_trade.call_fill_price = new_fill
                else:
                    slave_trade.put_order_id = new_order_id or None
                    slave_trade.put_product_id = new_pid
                    slave_trade.put_symbol = str(new_symbol or "")
                    slave_trade.put_strike = float(new_strike or 0)
                    if new_fill > 0:
                        slave_trade.put_fill_price = new_fill
                slave.connection_status = "error"
                slave.last_error = msg[:500]
                log_and_buffer(
                    "SLAVE_ADJ_LEDGER",
                    int(slave_trade.master_trade_id or 0),
                    {
                        "slave": int(slave.id),
                        "old_pid": old_pid,
                        "old_closed": "yes" if old_closed_ok else "no",
                        "new_pid": new_pid,
                        "new_opened": "no",
                        "outcome": "partial",
                    },
                )
                db.commit()
                return

            # New leg confirmed live — open ledger window
            new_opened_ok = record_slave_adjustment_open(
                db,
                slave_trade=slave_trade,
                slave_account_id=int(slave.id),
                master_trade=master_row,
                triggered_leg=leg,
                new_product_id=new_pid,
                new_symbol=str(new_symbol or ""),
                new_strike=float(new_strike or 0),
                new_order_id=new_order_id or None,
                reason="ADJUSTMENT",
                new_leg_opened_at=new_leg_open_ts,
                new_leg_fill_at=new_leg_fill_ts,
                quantity=entry_qty,
            )

            if leg == "call":
                slave_trade.call_order_id = new_order_id or None
                slave_trade.call_sl_order_id = None
                slave_trade.call_product_id = new_pid
                slave_trade.call_symbol = str(new_symbol or "")
                slave_trade.call_strike = float(new_strike or 0)
                if new_fill > 0:
                    slave_trade.call_fill_price = new_fill
            else:
                slave_trade.put_order_id = new_order_id or None
                slave_trade.put_sl_order_id = None
                slave_trade.put_product_id = new_pid
                slave_trade.put_symbol = str(new_symbol or "")
                slave_trade.put_strike = float(new_strike or 0)
                if new_fill > 0:
                    slave_trade.put_fill_price = new_fill

            # Keep actual_quantity synced to live entry size
            slave_trade.actual_quantity = entry_qty
            slave_trade.status = "active"
            slave_trade.last_error = None
            slave_trade.last_updated = get_utc_now()
            slave.last_error = None
            slave.connection_status = "connected"
            slave.last_connected_at = get_utc_now()
            log_and_buffer(
                "SLAVE_ADJ_LEDGER",
                int(slave_trade.master_trade_id or 0),
                {
                    "slave": int(slave.id),
                    "old_pid": old_pid,
                    "old_closed": "yes" if old_closed_ok else "no",
                    "new_pid": new_pid,
                    "new_opened": "yes" if new_opened_ok else "no",
                    "outcome": "ok",
                },
            )
            db.commit()
            logger.info(
                "✅ Slave '%s' adjustment mirrored (atomic verify OK)",
                slave.name,
            )

        except Exception as exc:
            logger.error(
                "❌ Slave '%s' adjustment FAILED: %s",
                slave.name,
                exc,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            # Record what actually happened on the exchange (RULE 9)
            try:
                from backend.engine.structure_ledger import (
                    record_slave_adjustment_close,
                    record_slave_adjustment_open,
                    set_structure_attribution_warning,
                )
                from backend.models import Trade as MasterTrade

                master_row = (
                    db.query(MasterTrade)
                    .filter(
                        MasterTrade.id
                        == int(slave_trade.master_trade_id or 0)
                    )
                    .first()
                )
                try:
                    exc_positions = await client.get_option_positions()
                except Exception:
                    exc_positions = []
                old_live_exc = self._position_size_for_product(
                    exc_positions, old_pid
                )
                new_live_exc = self._position_size_for_product(
                    exc_positions, new_pid
                )
                old_flat = (
                    old_live_exc is None
                    or abs(float(old_live_exc)) <= 1e-9
                )
                new_live_short = (
                    new_live_exc is not None
                    and float(new_live_exc) < -1e-9
                )
                # Old flat → close window (idempotent if already closed)
                if old_flat:
                    close_ts = (
                        old_leg_closed_ts
                        if old_leg_closed_ts is not None
                        else get_utc_now()
                    )
                    old_closed_exc = record_slave_adjustment_close(
                        db,
                        slave_account_id=int(slave.id),
                        master_trade=master_row,
                        triggered_leg=leg,
                        reason="ADJUSTMENT",
                        old_leg_closed_at=close_ts,
                        old_leg_fill_at=old_leg_close_fill_ts,
                        old_product_id=old_pid,
                    )
                # New short live → open window
                if new_live_short:
                    open_ts = (
                        new_leg_open_ts
                        if new_leg_open_ts is not None
                        else get_utc_now()
                    )
                    new_opened_exc = record_slave_adjustment_open(
                        db,
                        slave_trade=slave_trade,
                        slave_account_id=int(slave.id),
                        master_trade=master_row,
                        triggered_leg=leg,
                        new_product_id=new_pid,
                        new_symbol=str(new_symbol or ""),
                        new_strike=float(new_strike or 0),
                        new_order_id=new_order_id,
                        reason="ADJUSTMENT",
                        new_leg_opened_at=open_ts,
                        new_leg_fill_at=new_leg_fill_ts,
                        quantity=stored_qty,
                    )
                if old_flat and not new_live_short:
                    set_structure_attribution_warning(
                        db,
                        slave_account_id=int(slave.id),
                        master_trade=master_row,
                        warning=(
                            "partial_adjustment: old leg closed, "
                            "new leg missing"
                        ),
                    )
                slave_trade.status = "partial_adjustment"
                slave_trade.last_error = (
                    f"partial_adjustment: exception during adjust: {exc}"
                )[:500]
                slave_trade.error_count = (
                    int(slave_trade.error_count or 0) + 1
                )
                slave_trade.last_updated = get_utc_now()
                slave.connection_status = "error"
                slave.last_error = str(exc)[:500]
                log_and_buffer(
                    "SLAVE_ADJ_LEDGER",
                    int(slave_trade.master_trade_id or 0),
                    {
                        "slave": int(slave.id),
                        "old_pid": old_pid,
                        "old_closed": "yes" if old_flat else "no",
                        "new_pid": new_pid,
                        "new_opened": "yes" if new_live_short else "no",
                        "outcome": "error",
                    },
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        finally:
            await client.close()

    async def mirror_conversion(
        self,
        master_trade_id: int,
        hedge_product_id: int,
        hedge_symbol: str,
        old_other_product_id: int,
        new_other_product_id: int,
        new_other_symbol: str,
        new_other_strike: float,
        other_leg_type: str,
        master_qty: int,
        master_bracket_sl: float | None = None,
    ) -> None:
        """
        # Conversion Mode is OFF by default. Do not enable until this path
        # has been tested end-to-end on a slave with a live hedge.

        AUDIT-7: Mirror conversion-mode entry to slaves.

        1) BUY long hedge (no bracket SL)
        2) Close old other short + open new other short

        master_bracket_sl: absolute stop from master's new-other fill.
        """
        with self.db_factory() as db:
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == master_trade_id,
                    SlaveTrade.status == "active",
                )
                .all()
            )
            if not slave_trades:
                logger.info(
                    "No active slave trades for conversion mirror master=%s",
                    master_trade_id,
                )
                return

            logger.info(
                "Mirroring conversion to %s slaves: hedge=%s other=%s→%s "
                "master_bracket_sl=%s",
                len(slave_trades),
                hedge_symbol,
                old_other_product_id,
                new_other_product_id,
                master_bracket_sl,
            )

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if not slave or not slave.is_active:
                    continue

                async with self._slave_op_lock(
                    int(slave.id), "mirror_conversion"
                ) as acquired:
                    if not acquired:
                        continue
                    await self._mirror_conversion_to_slave(
                        slave=slave,
                        slave_trade=slave_trade,
                        hedge_product_id=hedge_product_id,
                        hedge_symbol=hedge_symbol,
                        old_other_product_id=old_other_product_id,
                        new_other_product_id=new_other_product_id,
                        new_other_symbol=new_other_symbol,
                        new_other_strike=new_other_strike,
                        other_leg_type=other_leg_type,
                        master_bracket_sl=master_bracket_sl,
                        db=db,
                    )

    async def _mirror_conversion_to_slave(
        self,
        *,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        hedge_product_id: int,
        hedge_symbol: str,
        old_other_product_id: int,
        new_other_product_id: int,
        new_other_symbol: str,
        new_other_strike: float,
        other_leg_type: str,
        master_bracket_sl: float | None,
        db: Any,
    ) -> None:
        """
        # Conversion Mode is OFF by default. Do not enable until this path
        # has been tested end-to-end on a slave with a live hedge.

        Caller MUST hold the per-slave lock.

        1) BUY conversion hedge (open — live qty from old other short)
        2) Close old other short via _close_with_reduce_only
        3) Open new other short (same live qty)
        Ledger rows written for each step; LEDGER_MISS if no structure.
        """
        client = self._get_slave_client(slave)
        leg = str(other_leg_type).lower()
        master_trade_id = int(slave_trade.master_trade_id or 0)
        slave_id = int(slave.id)
        hedge_pid = int(hedge_product_id)
        old_pid = int(old_other_product_id)
        new_pid = int(new_other_product_id)
        # Hedge matches triggered side = opposite of the other short
        hedge_is_call = leg == "put"
        try:
            from backend.engine.structure_ledger import (
                KIND_SLAVE,
                ROLE_BASKET_CALL,
                ROLE_BASKET_PUT,
                ROLE_HEDGE_CALL,
                ROLE_HEDGE_PUT,
                _next_adj_seq,
                close_leg,
                find_open_leg,
                get_active_structure,
                open_leg,
            )
            from backend.models import Trade as MasterTrade

            master_row = (
                db.query(MasterTrade)
                .filter(MasterTrade.id == master_trade_id)
                .first()
            )
            hid = (
                getattr(master_row, "hedge_position_id", None)
                if master_row is not None
                else None
            )
            struct = None
            if hid is not None:
                struct = get_active_structure(
                    db,
                    hedge_position_id=int(hid),
                    account_kind=KIND_SLAVE,
                    slave_account_id=slave_id,
                )
            if struct is None:
                log_and_buffer(
                    "LEDGER_MISS",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "reason": "no_active_structure",
                        "path": "mirror_conversion",
                    },
                )
                logger.error(
                    "[LEDGER_MISS] slave=%s reason=no_active_structure -- "
                    "conversion ledger NOT recorded",
                    slave_id,
                )

            # --- Live book: size from old other short (never stored qty) ---
            live_positions = await client.get_option_positions()
            old_live = self._position_size_for_product(live_positions, old_pid)
            if old_live is None or abs(float(old_live)) <= 1e-9:
                msg = (
                    f"conversion_abort: old other product {old_pid} "
                    f"not live on slave (size={old_live})"
                )
                log_and_buffer(
                    "SLAVE_CONVERSION",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "op": "abort",
                        "product_id": old_pid,
                        "live_size_before": old_live,
                        "size_used": 0,
                        "reason": "old_other_not_live",
                    },
                )
                slave_trade.last_error = msg[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                db.commit()
                return

            entry_qty = max(1, abs(int(round(float(old_live)))))
            basket_seq = (
                getattr(master_row, "basket_seq_in_structure", None)
                if master_row is not None
                else None
            )
            bs = int(basket_seq) if basket_seq is not None else None
            other_role = (
                ROLE_BASKET_CALL if leg == "call" else ROLE_BASKET_PUT
            )
            hedge_role = (
                ROLE_HEDGE_CALL if hedge_is_call else ROLE_HEDGE_PUT
            )

            # --- 1) BUY conversion hedge (open) ---
            hedge_live_before = self._position_size_for_product(
                live_positions, hedge_pid
            )
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            hedge_open_ts = get_utc_now()
            hedge_order = await client.place_order(
                product_id=hedge_pid,
                size=entry_qty,
                side="buy",
            )
            hedge_fill_ts = get_utc_now()
            log_and_buffer(
                "SLAVE_CONVERSION",
                master_trade_id,
                {
                    "slave": slave_id,
                    "op": "open",
                    "product_id": hedge_pid,
                    "live_size_before": hedge_live_before,
                    "size_used": entry_qty,
                    "reason": "conversion_hedge_buy",
                },
            )
            post_hedge = await client.get_option_positions()
            hedge_live_after = self._position_size_for_product(
                post_hedge, hedge_pid
            )
            if (
                hedge_live_after is None
                or float(hedge_live_after) <= 1e-9
            ):
                raise RuntimeError(
                    f"conversion hedge buy not on book product={hedge_pid} "
                    f"live_after={hedge_live_after}"
                )
            if struct is not None:
                open_leg(
                    db,
                    structure=struct,
                    leg_role=hedge_role,
                    product_id=hedge_pid,
                    side="BUY",
                    quantity=entry_qty,
                    symbol=str(hedge_symbol or ""),
                    strike=None,
                    basket_seq=None,
                    adj_seq=_next_adj_seq(
                        db,
                        structure_id=int(struct.id),
                        leg_role=hedge_role,
                        basket_seq=None,
                    ),
                    entry_order_id=self._order_id(hedge_order),
                    opened_at=hedge_open_ts,
                    fill_at=hedge_fill_ts,
                )

            # --- 2) Close old other short (reduce_only + live size) ---
            old_live_now = self._position_size_for_product(post_hedge, old_pid)
            if old_live_now is None or abs(float(old_live_now)) <= 1e-9:
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                old_close_ts = get_utc_now()
                old_close_fill_ts = old_close_ts
                log_and_buffer(
                    "SLAVE_CONVERSION",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "op": "close",
                        "product_id": old_pid,
                        "live_size_before": old_live_now,
                        "size_used": 0,
                        "reason": "old_other_already_flat",
                    },
                )
                if struct is not None:
                    open_row = find_open_leg(
                        db,
                        structure_id=int(struct.id),
                        leg_role=other_role,
                        basket_seq=bs,
                        product_id=old_pid,
                    )
                    if open_row is None:
                        open_row = find_open_leg(
                            db,
                            structure_id=int(struct.id),
                            leg_role=other_role,
                            basket_seq=bs,
                        )
                    if open_row is not None:
                        close_leg(
                            db,
                            open_row,
                            reason="CONVERSION",
                            closed_at=old_close_ts,
                            structure=struct,
                            fill_at=old_close_fill_ts,
                        )
            else:
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                old_close_ts = get_utc_now()
                ok_close, _ord, close_err = await self._close_with_reduce_only(
                    client=client,
                    slave=slave,
                    product_id=old_pid,
                    signed_size=float(old_live_now),
                    master_trade_id=master_trade_id,
                    path="mirror_conversion_old_other",
                )
                old_close_fill_ts = get_utc_now()
                size_used = max(1, abs(int(round(float(old_live_now)))))
                log_and_buffer(
                    "SLAVE_CONVERSION",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "op": "close",
                        "product_id": old_pid,
                        "live_size_before": old_live_now,
                        "size_used": size_used,
                        "reason": (
                            "old_other_close_ok"
                            if ok_close
                            else f"old_other_close_fail:{close_err[:80]}"
                        ),
                    },
                )
                if not ok_close:
                    raise RuntimeError(
                        f"conversion old other close failed: {close_err}"
                    )
                if struct is not None:
                    open_row = find_open_leg(
                        db,
                        structure_id=int(struct.id),
                        leg_role=other_role,
                        basket_seq=bs,
                        product_id=old_pid,
                    )
                    if open_row is None:
                        open_row = find_open_leg(
                            db,
                            structure_id=int(struct.id),
                            leg_role=other_role,
                            basket_seq=bs,
                        )
                    if open_row is not None:
                        close_leg(
                            db,
                            open_row,
                            reason="CONVERSION",
                            closed_at=old_close_ts,
                            structure=struct,
                            fill_at=old_close_fill_ts,
                        )

            # --- 3) Open new other short ---
            new_sl = None
            new_sl_limit = None
            if (
                master_bracket_sl is not None
                and float(master_bracket_sl) > 0
            ):
                new_sl = round(float(master_bracket_sl), 2)
                new_sl_limit = round(new_sl * 1.05, 2)
            else:
                logger.critical(
                    "[BRACKET_SL] slave='%s' conversion missing "
                    "master_bracket_sl — placing without bracket",
                    slave.name,
                )
            pre_new = await client.get_option_positions()
            new_live_before = self._position_size_for_product(pre_new, new_pid)
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            new_open_ts = get_utc_now()
            new_order = await client.place_order(
                product_id=new_pid,
                size=entry_qty,
                side="sell",
                bracket_stop_loss_price=new_sl,
                bracket_stop_loss_limit_price=new_sl_limit,
            )
            new_fill_ts = get_utc_now()
            log_and_buffer(
                "SLAVE_CONVERSION",
                master_trade_id,
                {
                    "slave": slave_id,
                    "op": "open",
                    "product_id": new_pid,
                    "live_size_before": new_live_before,
                    "size_used": entry_qty,
                    "reason": "conversion_new_other_short",
                },
            )
            post_new = await client.get_option_positions()
            new_live_after = self._position_size_for_product(post_new, new_pid)
            if new_live_after is None or float(new_live_after) >= -1e-9:
                raise RuntimeError(
                    f"conversion new other short missing product={new_pid} "
                    f"live_after={new_live_after}"
                )
            new_fill = float(
                await client.resolve_fill_price(
                    new_order, symbol_for_fallback=new_other_symbol
                )
                or 0.0
            )
            new_order_id = self._order_id(new_order)
            if struct is not None:
                open_leg(
                    db,
                    structure=struct,
                    leg_role=other_role,
                    product_id=new_pid,
                    side="SELL",
                    quantity=entry_qty,
                    symbol=str(new_other_symbol or ""),
                    strike=float(new_other_strike or 0) or None,
                    basket_seq=bs,
                    adj_seq=_next_adj_seq(
                        db,
                        structure_id=int(struct.id),
                        leg_role=other_role,
                        basket_seq=bs,
                    ),
                    entry_order_id=new_order_id,
                    opened_at=new_open_ts,
                    fill_at=new_fill_ts,
                )

            if leg == "call":
                slave_trade.call_order_id = new_order_id or None
                slave_trade.call_sl_order_id = None
                slave_trade.call_product_id = new_pid
                slave_trade.call_symbol = str(new_other_symbol or "")
                slave_trade.call_strike = float(new_other_strike or 0)
                if new_fill > 0:
                    slave_trade.call_fill_price = new_fill
            else:
                slave_trade.put_order_id = new_order_id or None
                slave_trade.put_sl_order_id = None
                slave_trade.put_product_id = new_pid
                slave_trade.put_symbol = str(new_other_symbol or "")
                slave_trade.put_strike = float(new_other_strike or 0)
                if new_fill > 0:
                    slave_trade.put_fill_price = new_fill

            slave.last_error = None
            slave.connection_status = "connected"
            slave.last_connected_at = get_utc_now()
            db.commit()
            log_and_buffer(
                "BRACKET_SL",
                int(master_trade_id),
                {
                    "leg": leg,
                    "slave": slave.name,
                    "stop_price": new_sl,
                    "source": "master_absolute",
                    "stage": "conversion",
                },
            )
        except Exception as exc:
            logger.error(
                "❌ Slave '%s' conversion FAILED: %s", slave.name, exc
            )
            try:
                db.rollback()
            except Exception:
                pass
            slave_trade.last_error = str(exc)[:500]
            slave_trade.error_count = int(slave_trade.error_count or 0) + 1
            db.commit()
        finally:
            await client.close()

    def _fit_hedge_qty_to_debit(
        self,
        slave_qty: int,
        *,
        call_fill: float,
        put_fill: float,
        available_usd: float,
    ) -> tuple[int, float]:
        """
        Reduce qty until (call+put)*qty*CV * afford_buffer ≤ available.

        Returns (fitted_qty, required_usd_at_fitted_qty).
        """
        qty = max(0, int(slave_qty or 0))
        per_lot = (
            float(call_fill or 0) + float(put_fill or 0)
        ) * _CONTRACT_SIZE
        if qty <= 0 or per_lot <= 0:
            return 0, 0.0
        avail = float(available_usd or 0.0)
        while qty >= 1:
            required = per_lot * qty * _HEDGE_AFFORD_BUFFER
            if required <= avail:
                return qty, float(required)
            qty -= 1
        return 0, float(per_lot * _HEDGE_AFFORD_BUFFER)

    async def mirror_hedge_open(
        self,
        master_hedge: Any,
        db: Any = None,
    ) -> dict[str, int]:
        """
        Mirror a confirmed master long ATM straddle onto every ACTIVE slave.

        Sizing uses the same path as baskets (_calc_qty / _fit_qty_to_margin)
        so hedge:basket stays 1:1. Hedge debit must be affordable at that qty;
        if not, reduce qty — if still 0, skip the slave (no naked baskets).

        Master hedge is never rolled back on slave failure.
        """
        from backend.engine.order_executor import OrderExecutor

        master_hedge_id = int(getattr(master_hedge, "id", 0) or 0)
        master_qty = max(1, int(getattr(master_hedge, "quantity", 1) or 1))
        master_account_id = int(getattr(master_hedge, "account_id", 0) or 0)
        call_pid = int(getattr(master_hedge, "call_product_id", 0) or 0)
        put_pid = int(getattr(master_hedge, "put_product_id", 0) or 0)
        call_symbol = str(getattr(master_hedge, "call_symbol", "") or "")
        put_symbol = str(getattr(master_hedge, "put_symbol", "") or "")
        master_call_fill = float(
            getattr(master_hedge, "call_fill_price", 0) or 0
        )
        master_put_fill = float(
            getattr(master_hedge, "put_fill_price", 0) or 0
        )
        master_entry_spread = float(
            getattr(master_hedge, "entry_spread_usd", 0) or 0
        )
        underlying = str(getattr(master_hedge, "underlying", "") or "")
        expiry_date = getattr(master_hedge, "expiry_date", None)
        strike = float(getattr(master_hedge, "strike", 0) or 0)
        entry_total_theta = getattr(master_hedge, "entry_total_theta", None)
        entry_call_iv = getattr(master_hedge, "entry_call_iv", None)
        entry_put_iv = getattr(master_hedge, "entry_put_iv", None)
        target_usd = getattr(master_hedge, "target_usd", None)
        stoploss_usd = getattr(master_hedge, "stoploss_usd", None)

        opened = 0
        skipped = 0
        failed = 0
        slaves_total = 0

        if master_hedge_id <= 0 or call_pid <= 0 or put_pid <= 0:
            logger.error(
                "[SLAVE_HEDGE_OPEN] invalid master_hedge id=%s "
                "call_pid=%s put_pid=%s — abort",
                master_hedge_id,
                call_pid,
                put_pid,
            )
            return {
                "slaves_total": 0,
                "opened": 0,
                "skipped": 0,
                "failed": 0,
            }

        # Master hedge debit ≈ capital "used" for capital-based sizing
        master_hedge_cost = (
            (master_call_fill + master_put_fill) * master_qty * _CONTRACT_SIZE
        )

        with self.db_factory() as work_db:
            slaves = get_active_slave_accounts(work_db)
            slaves_total = len(slaves)
            if not slaves:
                log_and_buffer(
                    "SLAVE_HEDGE_OPEN_SUMMARY",
                    int(master_hedge_id),
                    {
                        "master_hedge": master_hedge_id,
                        "slaves_total": 0,
                        "opened": 0,
                        "skipped": 0,
                        "failed": 0,
                    },
                )
                return {
                    "slaves_total": 0,
                    "opened": 0,
                    "skipped": 0,
                    "failed": 0,
                }

            master_total_capital: float | None = None
            master_margin_used: float | None = (
                master_hedge_cost if master_hedge_cost > 0 else None
            )
            master_capital_fetch_failed = False
            try:
                from backend.models import Account

                master_acc = (
                    work_db.query(Account)
                    .filter(Account.id == master_account_id)
                    .first()
                )
                if master_acc is None:
                    master_acc = (
                        work_db.query(Account)
                        .filter(Account.is_active.is_(True))
                        .order_by(Account.id.asc())
                        .first()
                    )
                if master_acc is not None:
                    m_client = DeltaClient(
                        decrypt(master_acc.api_key_encrypted),
                        decrypt(master_acc.api_secret_encrypted),
                    )
                    try:
                        wallet = await m_client.get_wallet_balance()
                        master_total_capital = float(
                            wallet.get("balance_usdt", 0) or 0
                        )
                        if master_total_capital <= 0:
                            master_total_capital = float(
                                wallet.get("available_balance", 0) or 0
                            )
                        if master_total_capital <= 0:
                            master_capital_fetch_failed = True
                            logger.warning(
                                "[SLAVE_HEDGE_OPEN] master capital fetch "
                                "returned unusable total=$%.2f",
                                master_total_capital,
                            )
                    finally:
                        await m_client.close()
                else:
                    master_capital_fetch_failed = True
                    logger.warning(
                        "[SLAVE_HEDGE_OPEN] master capital fetch failed: "
                        "no master account"
                    )
            except Exception as cap_err:
                master_capital_fetch_failed = True
                logger.warning(
                    "[SLAVE_HEDGE_OPEN] master capital fetch failed: %s",
                    cap_err,
                )

            for slave in slaves:
                async with self._slave_op_lock(
                    int(slave.id), "mirror_hedge_open"
                ) as acquired:
                    if not acquired:
                        skipped += 1
                        continue
                    result = await self._mirror_hedge_open_to_slave(
                        slave=slave,
                        db=work_db,
                        master_hedge_id=master_hedge_id,
                        master_account_id=master_account_id,
                        master_qty=master_qty,
                        master_hedge_cost=master_hedge_cost,
                        master_margin_used=master_margin_used,
                        master_total_capital=master_total_capital,
                        master_capital_fetch_failed=master_capital_fetch_failed,
                        call_pid=call_pid,
                        put_pid=put_pid,
                        call_symbol=call_symbol,
                        put_symbol=put_symbol,
                        master_call_fill=master_call_fill,
                        master_put_fill=master_put_fill,
                        master_entry_spread=master_entry_spread,
                        underlying=underlying,
                        expiry_date=expiry_date,
                        strike=strike,
                        entry_total_theta=entry_total_theta,
                        entry_call_iv=entry_call_iv,
                        entry_put_iv=entry_put_iv,
                        target_usd=target_usd,
                        stoploss_usd=stoploss_usd,
                        executor=OrderExecutor(),
                    )
                    if result == "opened":
                        opened += 1
                    elif result == "skipped":
                        skipped += 1
                    else:
                        failed += 1

        log_and_buffer(
            "SLAVE_HEDGE_OPEN_SUMMARY",
            int(master_hedge_id),
            {
                "master_hedge": master_hedge_id,
                "slaves_total": slaves_total,
                "opened": opened,
                "skipped": skipped,
                "failed": failed,
            },
        )
        logger.info(
            "[SLAVE_HEDGE_OPEN_SUMMARY] master_hedge=%s | slaves_total=%s | "
            "opened=%s | skipped=%s | failed=%s",
            master_hedge_id,
            slaves_total,
            opened,
            skipped,
            failed,
        )
        return {
            "slaves_total": slaves_total,
            "opened": opened,
            "skipped": skipped,
            "failed": failed,
        }

    async def _mirror_hedge_open_to_slave(
        self,
        *,
        slave: SlaveAccount,
        db: Any,
        master_hedge_id: int,
        master_account_id: int,
        master_qty: int,
        master_hedge_cost: float,
        master_margin_used: float | None,
        master_total_capital: float | None,
        master_capital_fetch_failed: bool = False,
        call_pid: int,
        put_pid: int,
        call_symbol: str,
        put_symbol: str,
        master_call_fill: float,
        master_put_fill: float,
        master_entry_spread: float,
        underlying: str,
        expiry_date: Any,
        strike: float,
        entry_total_theta: Any,
        entry_call_iv: Any,
        entry_put_iv: Any,
        target_usd: Any,
        stoploss_usd: Any,
        executor: Any,
    ) -> str:
        """
        Open long straddle on one slave. Returns 'opened' | 'skipped' | 'failed'.

        Caller MUST hold the per-slave lock.
        """
        slave_id = int(slave.id)
        # Any live slave hedge blocks a second open (roll race / blocked close)
        existing_any = (
            db.query(SlaveHedgePosition)
            .filter(
                SlaveHedgePosition.slave_account_id == slave_id,
                SlaveHedgePosition.status.in_(("active", "partial")),
            )
            .first()
        )
        if existing_any is not None:
            existing_id = int(existing_any.id)
            existing_master = int(existing_any.master_hedge_id or 0)
            if existing_master == int(master_hedge_id):
                logger.info(
                    "[SLAVE_HEDGE_OPEN] slave=%s already has active row id=%s "
                    "for master_hedge=%s — skip",
                    slave_id,
                    existing_id,
                    master_hedge_id,
                )
                return "skipped"
            logger.error(
                "[SLAVE_HEDGE_DUPLICATE_SKIP] slave=%s | existing_hedge=%s | "
                "new_master_hedge=%s",
                slave_id,
                existing_id,
                master_hedge_id,
            )
            log_and_buffer(
                "SLAVE_HEDGE_DUPLICATE_SKIP",
                int(master_hedge_id),
                {
                    "slave": slave_id,
                    "existing_hedge": existing_id,
                    "existing_master_hedge": existing_master,
                    "new_master_hedge": int(master_hedge_id),
                },
            )
            return "skipped"

        # Fresh available balance
        available_before = 0.0
        if bool(getattr(slave, "is_virtual", False)):
            allocated = float(
                getattr(slave, "user_allocated_capital", None) or 0
            )
            cached = float(getattr(slave, "balance_usd", 0) or 0)
            available_before = allocated if allocated > 0 else cached
        else:
            client_bal = self._get_slave_client(slave)
            try:
                try:
                    wallet = await client_bal.get_wallet_balance()
                    available_before = float(
                        wallet.get("available_balance", 0) or 0
                    )
                    slave.balance_usd = float(
                        wallet.get("balance_usdt", 0) or 0
                    )
                    db.commit()
                except Exception as bal_err:
                    logger.warning(
                        "[SLAVE_HEDGE_OPEN] slave=%s balance fetch failed: %s",
                        slave_id,
                        bal_err,
                    )
                    available_before = float(
                        getattr(slave, "balance_usd", 0) or 0
                    )
            finally:
                await client_bal.close()

        slave_qty = self._calc_qty(
            int(master_qty),
            float(slave.qty_multiplier or 1.0),
            slave=slave,
            master_margin_used_usd=master_margin_used,
            master_total_capital_usd=master_total_capital,
            slave_available_usd=available_before,
            master_capital_fetch_failed=master_capital_fetch_failed,
            master_trade_id=int(master_hedge_id or 0),
        )

        # Same margin headroom path as baskets (ratio consistency)
        if not bool(getattr(slave, "is_virtual", False)) and slave_qty > 0:
            slave_qty = self._fit_qty_to_margin(
                slave_qty,
                live_balance=available_before,
                master_margin_used_usd=master_margin_used,
                master_qty=int(master_qty),
                call_fill=float(master_call_fill or 0),
                put_fill=float(master_put_fill or 0),
                slave=slave,
                master_trade_id=int(master_hedge_id or 0),
            )

        # Hedge is a debit — must fit after buffer
        required_at_qty = 0.0
        if slave_qty > 0:
            slave_qty, required_at_qty = self._fit_hedge_qty_to_debit(
                slave_qty,
                call_fill=master_call_fill,
                put_fill=master_put_fill,
                available_usd=available_before,
            )

        if slave_qty < 1:
            req = required_at_qty or (
                (master_call_fill + master_put_fill)
                * _CONTRACT_SIZE
                * _HEDGE_AFFORD_BUFFER
            )
            logger.info(
                "[SLAVE_HEDGE_SKIP] slave=%s | reason=insufficient_capital | "
                "required=%.4f | available=%.4f",
                slave_id,
                req,
                available_before,
            )
            log_and_buffer(
                "SLAVE_HEDGE_SKIP",
                int(master_hedge_id),
                {
                    "slave": slave_id,
                    "reason": "insufficient_capital",
                    "required": round(float(req), 4),
                    "available": round(float(available_before), 4),
                },
            )
            return "skipped"

        # Scale master's entry spread by qty ratio
        entry_spread_usd = 0.0
        if master_qty > 0 and master_entry_spread > 0:
            entry_spread_usd = float(master_entry_spread) * (
                float(slave_qty) / float(master_qty)
            )

        # Virtual: DB only
        if bool(getattr(slave, "is_virtual", False)):
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            virt_hedge_open_ts = get_utc_now()
            cost_usd = (
                (master_call_fill + master_put_fill)
                * slave_qty
                * _CONTRACT_SIZE
            )
            row = SlaveHedgePosition(
                account_id=int(master_account_id),
                slave_account_id=slave_id,
                master_hedge_id=int(master_hedge_id),
                underlying=underlying,
                expiry_date=expiry_date,
                strike=float(strike),
                quantity=int(slave_qty),
                status="active",
                call_product_id=int(call_pid),
                call_symbol=call_symbol or None,
                call_order_id="VIRTUAL",
                call_fill_price=float(master_call_fill or 0) or None,
                put_product_id=int(put_pid),
                put_symbol=put_symbol or None,
                put_order_id="VIRTUAL",
                put_fill_price=float(master_put_fill or 0) or None,
                entry_time=get_utc_now(),
                target_usd=float(target_usd) if target_usd is not None else None,
                stoploss_usd=(
                    float(stoploss_usd) if stoploss_usd is not None else None
                ),
                entry_total_theta=(
                    float(entry_total_theta)
                    if entry_total_theta is not None
                    else None
                ),
                entry_call_iv=(
                    float(entry_call_iv) if entry_call_iv is not None else None
                ),
                entry_put_iv=(
                    float(entry_put_iv) if entry_put_iv is not None else None
                ),
                entry_spread_usd=float(entry_spread_usd),
                entry_cost_usd=float(cost_usd),
                allocated_capital=float(
                    getattr(slave, "user_allocated_capital", None) or 0
                )
                or None,
                is_bot_managed=True,
                error_count=0,
            )
            db.add(row)
            db.commit()
            try:
                from backend.engine.structure_ledger import record_slave_hedge_open

                record_slave_hedge_open(
                    db,
                    slave_hedge=row,
                    slave_account=slave,
                    structure_opened_at=virt_hedge_open_ts,
                    call_opened_at=virt_hedge_open_ts,
                    put_opened_at=virt_hedge_open_ts,
                    call_fill_at=virt_hedge_open_ts,
                    put_fill_at=virt_hedge_open_ts,
                )
                db.commit()
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave hedge open failed: %s",
                    ledger_exc,
                    exc_info=True,
                )
            log_and_buffer(
                "SLAVE_HEDGE_OPEN",
                int(master_hedge_id),
                {
                    "master_hedge": master_hedge_id,
                    "slave": slave_id,
                    "qty": slave_qty,
                    "master_qty": master_qty,
                    "call_fill": master_call_fill,
                    "put_fill": master_put_fill,
                    "cost": round(cost_usd, 4),
                    "entry_spread": round(entry_spread_usd, 6),
                    "available_before": round(available_before, 4),
                    "virtual": True,
                },
            )
            logger.info(
                "[SLAVE_HEDGE_OPEN] master_hedge=%s | slave=%s | qty=%s | "
                "master_qty=%s | call_fill=%s | put_fill=%s | cost=%s | "
                "entry_spread=%s | available_before=%s (virtual)",
                master_hedge_id,
                slave_id,
                slave_qty,
                master_qty,
                master_call_fill,
                master_put_fill,
                round(cost_usd, 4),
                round(entry_spread_usd, 6),
                round(available_before, 4),
            )
            return "opened"

        client = self._get_slave_client(slave)
        call_fill = 0.0
        put_fill = 0.0
        call_fee = 0.0
        put_fee = 0.0
        call_order_id: str | None = None
        put_order_id: str | None = None
        try:
            # --- BUY CALL ---
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            call_open_ts = get_utc_now()
            call_result = await executor.buy_option(
                product_id=int(call_pid),
                quantity=int(slave_qty),
                delta_client=client,
                symbol_for_fallback=call_symbol or None,
            )
            if not call_result.success:
                logger.error(
                    "[SLAVE_HEDGE_OPEN] slave=%s CALL buy failed: %s",
                    slave_id,
                    call_result.error,
                )
                return "failed"

            call_fill_ts = get_utc_now()
            await asyncio.sleep(_HEDGE_VERIFY_PAUSE_SECONDS)
            call_ok = await client.verify_position_exists(int(call_pid))
            if not call_ok:
                logger.error(
                    "[SLAVE_HEDGE_OPEN] slave=%s CALL verify failed "
                    "product=%s — attempting unwind",
                    slave_id,
                    call_pid,
                )
                unwound = False
                try:
                    await client.close_position(
                        product_id=int(call_pid),
                        size=int(slave_qty),
                        is_long=True,
                    )
                    await asyncio.sleep(_HEDGE_VERIFY_PAUSE_SECONDS)
                    still = await client.verify_position_exists(int(call_pid))
                    unwound = not still
                except Exception as uw_err:
                    logger.critical(
                        "[SLAVE_HEDGE_UNWIND] slave=%s | leg=call | "
                        "unwound=False | err=%s",
                        slave_id,
                        uw_err,
                    )
                log_and_buffer(
                    "SLAVE_HEDGE_UNWIND",
                    int(master_hedge_id),
                    {
                        "slave": slave_id,
                        "leg": "call",
                        "unwound": unwound,
                        "reason": "call_verify_failed",
                    },
                )
                logger.critical(
                    "[SLAVE_HEDGE_UNWIND] slave=%s | leg=call | unwound=%s",
                    slave_id,
                    unwound,
                )
                return "failed"

            call_fill = float(call_result.filled_price or 0) or float(
                master_call_fill or 0
            )
            call_fee = float(call_result.commission or 0)
            call_order_id = (
                str(call_result.order_id)
                if call_result.order_id is not None
                else None
            )

            # --- BUY PUT ---
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            put_open_ts = get_utc_now()
            put_result = await executor.buy_option(
                product_id=int(put_pid),
                quantity=int(slave_qty),
                delta_client=client,
                symbol_for_fallback=put_symbol or None,
            )
            put_fill_ts = get_utc_now()
            put_ok = False
            put_fail_reason = ""
            if not put_result.success:
                put_fail_reason = put_result.error or "Put buy failed"
            else:
                await asyncio.sleep(_HEDGE_VERIFY_PAUSE_SECONDS)
                put_ok = await client.verify_position_exists(int(put_pid))
                if not put_ok:
                    put_fail_reason = (
                        f"Put not on Delta product_id={put_pid}"
                    )
                else:
                    put_fill = float(put_result.filled_price or 0) or float(
                        master_put_fill or 0
                    )
                    put_fee = float(put_result.commission or 0)
                    put_order_id = (
                        str(put_result.order_id)
                        if put_result.order_id is not None
                        else None
                    )

            if not put_ok:
                unwound = False
                try:
                    await client.close_position(
                        product_id=int(call_pid),
                        size=int(slave_qty),
                        is_long=True,
                    )
                    await asyncio.sleep(_HEDGE_VERIFY_PAUSE_SECONDS)
                    still = await client.verify_position_exists(int(call_pid))
                    unwound = not still
                except Exception as uw_err:
                    logger.critical(
                        "[SLAVE_HEDGE_UNWIND] slave=%s | leg=call | "
                        "unwound=False | err=%s | put_fail=%s",
                        slave_id,
                        uw_err,
                        put_fail_reason,
                    )
                log_and_buffer(
                    "SLAVE_HEDGE_UNWIND",
                    int(master_hedge_id),
                    {
                        "slave": slave_id,
                        "leg": "call",
                        "unwound": unwound,
                        "reason": put_fail_reason[:200],
                    },
                )
                logger.critical(
                    "[SLAVE_HEDGE_UNWIND] slave=%s | leg=call | unwound=%s",
                    slave_id,
                    unwound,
                )
                err_row = SlaveHedgePosition(
                    account_id=int(master_account_id),
                    slave_account_id=slave_id,
                    master_hedge_id=int(master_hedge_id),
                    underlying=underlying,
                    expiry_date=expiry_date,
                    strike=float(strike),
                    quantity=int(slave_qty),
                    status="error",
                    call_product_id=int(call_pid),
                    call_symbol=call_symbol or None,
                    call_order_id=call_order_id,
                    call_fill_price=call_fill if call_fill > 0 else None,
                    call_entry_fee_usd=call_fee if call_fee > 0 else None,
                    put_product_id=int(put_pid),
                    put_symbol=put_symbol or None,
                    put_order_id=put_order_id,
                    entry_time=get_utc_now(),
                    entry_spread_usd=float(entry_spread_usd),
                    last_error=(
                        f"put_failed:{put_fail_reason}; "
                        f"call_unwound={unwound}"
                    )[:500],
                    error_count=1,
                    is_bot_managed=True,
                )
                db.add(err_row)
                db.commit()
                return "failed"

            cost_usd = (call_fill + put_fill) * slave_qty * _CONTRACT_SIZE
            row = SlaveHedgePosition(
                account_id=int(master_account_id),
                slave_account_id=slave_id,
                master_hedge_id=int(master_hedge_id),
                underlying=underlying,
                expiry_date=expiry_date,
                strike=float(strike),
                quantity=int(slave_qty),
                status="active",
                call_product_id=int(call_pid),
                call_symbol=call_symbol or None,
                call_order_id=call_order_id,
                call_fill_price=call_fill if call_fill > 0 else None,
                call_entry_fee_usd=call_fee if call_fee > 0 else None,
                put_product_id=int(put_pid),
                put_symbol=put_symbol or None,
                put_order_id=put_order_id,
                put_fill_price=put_fill if put_fill > 0 else None,
                put_entry_fee_usd=put_fee if put_fee > 0 else None,
                entry_time=get_utc_now(),
                target_usd=float(target_usd) if target_usd is not None else None,
                stoploss_usd=(
                    float(stoploss_usd) if stoploss_usd is not None else None
                ),
                entry_total_theta=(
                    float(entry_total_theta)
                    if entry_total_theta is not None
                    else None
                ),
                entry_call_iv=(
                    float(entry_call_iv) if entry_call_iv is not None else None
                ),
                entry_put_iv=(
                    float(entry_put_iv) if entry_put_iv is not None else None
                ),
                entry_spread_usd=float(entry_spread_usd),
                entry_cost_usd=float(cost_usd),
                allocated_capital=float(
                    getattr(slave, "user_allocated_capital", None) or 0
                )
                or None,
                capital_per_lot=(
                    float(cost_usd) / float(slave_qty) if slave_qty > 0 else None
                ),
                is_bot_managed=True,
                error_count=0,
            )
            db.add(row)
            db.commit()
            try:
                from backend.engine.structure_ledger import record_slave_hedge_open

                record_slave_hedge_open(
                    db,
                    slave_hedge=row,
                    slave_account=slave,
                    structure_opened_at=call_open_ts,
                    call_opened_at=call_open_ts,
                    put_opened_at=put_open_ts,
                    call_fill_at=call_fill_ts,
                    put_fill_at=put_fill_ts,
                )
                db.commit()
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave hedge open failed: %s",
                    ledger_exc,
                    exc_info=True,
                )

            log_and_buffer(
                "SLAVE_HEDGE_OPEN",
                int(master_hedge_id),
                {
                    "master_hedge": master_hedge_id,
                    "slave": slave_id,
                    "qty": slave_qty,
                    "master_qty": master_qty,
                    "call_fill": call_fill,
                    "put_fill": put_fill,
                    "cost": round(cost_usd, 4),
                    "entry_spread": round(entry_spread_usd, 6),
                    "available_before": round(available_before, 4),
                },
            )
            logger.info(
                "[SLAVE_HEDGE_OPEN] master_hedge=%s | slave=%s | qty=%s | "
                "master_qty=%s | call_fill=%s | put_fill=%s | cost=%s | "
                "entry_spread=%s | available_before=%s",
                master_hedge_id,
                slave_id,
                slave_qty,
                master_qty,
                call_fill,
                put_fill,
                round(cost_usd, 4),
                round(entry_spread_usd, 6),
                round(available_before, 4),
            )
            return "opened"

        except Exception as exc:
            logger.error(
                "[SLAVE_HEDGE_OPEN] slave=%s FAILED: %s",
                slave_id,
                exc,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return "failed"
        finally:
            await client.close()

    def _master_trade_call_put_pids(
        self, db: Any, master_trade_id: int
    ) -> tuple[int, int]:
        """Latest call/put product_ids for a master trade (any leg status)."""
        from backend.models import Leg

        call_pid = 0
        put_pid = 0
        legs = (
            db.query(Leg)
            .filter(Leg.trade_id == int(master_trade_id))
            .order_by(Leg.id.asc())
            .all()
        )
        for leg in legs:
            lt = str(getattr(leg, "leg_type", "") or "").lower()
            pid = int(getattr(leg, "product_id", 0) or 0)
            if pid <= 0:
                continue
            if lt == "call":
                call_pid = pid
            elif lt == "put":
                put_pid = pid
        return call_pid, put_pid

    async def _verify_slave_products_flat(
        self,
        client: DeltaClient,
        product_ids: list[int],
    ) -> tuple[bool, list[dict[str, Any]]]:
        """
        Exchange-truth check: listed products must be flat.
        Returns (all_flat, remaining_rows).
        """
        wanted = {int(p) for p in product_ids if int(p or 0) > 0}
        if not wanted:
            return True, []
        try:
            positions = await client.get_option_positions()
        except Exception as exc:
            logger.error(
                "[SLAVE_HEDGE_CASCADE] get_option_positions failed: %s",
                exc,
            )
            return False, [{"error": "verify_fetch_failed", "detail": str(exc)}]
        remaining: list[dict[str, Any]] = []
        for pos in positions or []:
            try:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if pid in wanted and abs(size) > 1e-9:
                remaining.append(
                    {
                        "product_id": pid,
                        "size": size,
                        "symbol": str(pos.get("product_symbol") or ""),
                    }
                )
        return (len(remaining) == 0), remaining

    async def mirror_hedge_close(
        self,
        master_hedge: Any,
        reason: str = "",
        db: Any = None,
        *,
        closed_master_trade_ids: list[int] | None = None,
    ) -> dict[str, int]:
        """
        Cascade structure-hedge close to slaves: baskets first, then hedge.

        Per slave — never close the long hedge while any basket for that
        slave under this master hedge is still live on the exchange.
        Slave failures do not raise; master close is independent.
        """
        master_hedge_id = int(getattr(master_hedge, "id", 0) or 0)
        reason_norm = str(reason or "HEDGE_MANUAL").upper().strip()
        hedges_closed = 0
        blocked = 0
        slaves_n = 0

        if master_hedge_id <= 0:
            logger.error("[SLAVE_HEDGE_CASCADE] invalid master_hedge — abort")
            return {
                "slaves": 0,
                "hedges_closed": 0,
                "blocked": 0,
            }

        with self.db_factory() as work_db:
            # Master baskets under this hedge (all statuses — cascade may
            # already have closed them via close_master_trade → mirror_exit)
            master_rows = (
                work_db.query(Trade)
                .filter(Trade.hedge_position_id == master_hedge_id)
                .order_by(Trade.id.asc())
                .all()
            )
            master_trade_ids = [int(t.id) for t in master_rows]
            if closed_master_trade_ids:
                for tid in closed_master_trade_ids:
                    tid_i = int(tid)
                    if tid_i not in master_trade_ids:
                        master_trade_ids.append(tid_i)

            slave_hedges = (
                work_db.query(SlaveHedgePosition)
                .filter(
                    SlaveHedgePosition.master_hedge_id == master_hedge_id,
                    SlaveHedgePosition.status.in_(
                        ("active", "partial", "exit_failed", "error")
                    ),
                )
                .all()
            )
            # Also include active slaves that might lack a row? No — only
            # mirrored hedges. One row per slave for this master hedge.
            seen_slave_ids: set[int] = set()
            for sh in slave_hedges:
                sid = int(sh.slave_account_id)
                if sid in seen_slave_ids:
                    continue
                seen_slave_ids.add(sid)
                slaves_n += 1
                slave = (
                    work_db.query(SlaveAccount)
                    .filter(SlaveAccount.id == sid)
                    .first()
                )
                if slave is None or not bool(getattr(slave, "is_active", True)):
                    blocked += 1
                    continue

                async with self._slave_op_lock(
                    sid, "mirror_hedge_close"
                ) as acquired:
                    if not acquired:
                        blocked += 1
                        continue
                    outcome = await self._cascade_close_slave_hedge(
                        slave=slave,
                        slave_hedge=sh,
                        db=work_db,
                        master_hedge_id=master_hedge_id,
                        master_trade_ids=master_trade_ids,
                        reason=reason_norm,
                    )
                    if outcome.get("hedge_closed"):
                        hedges_closed += 1
                    else:
                        blocked += 1

        log_and_buffer(
            "SLAVE_HEDGE_CASCADE_SUMMARY",
            int(master_hedge_id),
            {
                "master_hedge": master_hedge_id,
                "slaves": slaves_n,
                "hedges_closed": hedges_closed,
                "blocked": blocked,
                "reason": reason_norm,
            },
        )
        logger.info(
            "[SLAVE_HEDGE_CASCADE_SUMMARY] master_hedge=%s | slaves=%s | "
            "hedges_closed=%s | blocked=%s",
            master_hedge_id,
            slaves_n,
            hedges_closed,
            blocked,
        )
        return {
            "slaves": slaves_n,
            "hedges_closed": hedges_closed,
            "blocked": blocked,
        }

    async def force_close_slave_structure(
        self,
        *,
        slave_id: int,
        reason: str,
    ) -> dict[str, Any]:
        """
        Force-close ONE slave's structure (baskets → verify → hedge).

        Used on subscription cancellation / API disconnect. Never touches
        master or other slaves. Idempotent when already flat.
        """
        reason_norm = str(reason or "").upper().strip()
        if reason_norm not in FORCE_CLOSE_REASONS:
            raise ValueError(
                f"Invalid force-close reason: {reason_norm or 'empty'}"
            )

        _ignore_trade_statuses = {
            "skipped_low_capital",
            "skipped_no_hedge",
            "closed",
        }
        _alive_hedge_statuses = (
            "active",
            "partial",
            "exit_failed",
            "error",
            "pending_close",
        )

        result: dict[str, Any] = {
            "slave_id": int(slave_id),
            "reason": reason_norm,
            "baskets_found": 0,
            "baskets_closed": 0,
            "baskets_failed": 0,
            "failed_baskets": [],
            "hedge_closed": False,
            "structures_closed": 0,
            "already_closed": False,
            "success": False,
        }

        with self.db_factory() as db:
            slave = (
                db.query(SlaveAccount)
                .filter(SlaveAccount.id == int(slave_id))
                .first()
            )
            if slave is None:
                raise LookupError(f"Slave {slave_id} not found")

            from backend.models import Structure

            alive_hedges = (
                db.query(SlaveHedgePosition)
                .filter(
                    SlaveHedgePosition.slave_account_id == int(slave_id),
                    SlaveHedgePosition.status.in_(_alive_hedge_statuses),
                )
                .order_by(SlaveHedgePosition.id.asc())
                .all()
            )
            open_trades = [
                st
                for st in db.query(SlaveTrade)
                .filter(SlaveTrade.slave_account_id == int(slave_id))
                .all()
                if str(st.status or "").lower() not in _ignore_trade_statuses
            ]
            active_structures = (
                db.query(Structure)
                .filter(
                    Structure.slave_account_id == int(slave_id),
                    Structure.account_kind == "SLAVE",
                    Structure.status == "active",
                )
                .all()
            )

            if not alive_hedges and not open_trades and not active_structures:
                result["already_closed"] = True
                result["success"] = True
                if bool(slave.is_active):
                    slave.is_active = False
                    slave.updated_at = get_utc_now()
                    db.commit()
                earner_uid = getattr(slave, "earner_user_id", None)
                logger.info(
                    "[SLAVE_FORCE_CLOSE] slave=%s earner_user=%s reason=%s | "
                    "baskets_closed=0 | hedge_closed=False | structures_closed=0 "
                    "(already flat)",
                    slave_id,
                    earner_uid,
                    reason_norm,
                )
                return result

            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            force_close_batch_ts = get_utc_now()
            async with self._slave_op_lock(
                int(slave_id), "force_close_slave_structure"
            ) as acquired:
                if not acquired:
                    result["success"] = False
                    result["lock_timeout"] = True
                    return result
                for sh in alive_hedges:
                    master_hid = int(sh.master_hedge_id)
                    master_trade_ids = [
                        int(t.id)
                        for t in db.query(Trade)
                        .filter(Trade.hedge_position_id == master_hid)
                        .order_by(Trade.id.asc())
                        .all()
                    ]
                    cascade = await self._cascade_close_slave_hedge(
                        slave=slave,
                        slave_hedge=sh,
                        db=db,
                        master_hedge_id=master_hid,
                        master_trade_ids=master_trade_ids,
                        reason=reason_norm,
                    )
                    result["baskets_found"] += int(
                        cascade.get("baskets_found") or 0
                    )
                    result["baskets_closed"] += int(
                        cascade.get("baskets_closed") or 0
                    )
                    result["baskets_failed"] += int(
                        cascade.get("baskets_failed") or 0
                    )
                    result["failed_baskets"].extend(
                        list(cascade.get("failed_baskets") or [])
                    )
                    if cascade.get("hedge_closed"):
                        result["hedge_closed"] = True

                # Orphan baskets (no alive hedge row) — still must flat before done
                if not alive_hedges and open_trades:
                    result["baskets_found"] = len(open_trades)
                    for st in open_trades:
                        if str(st.status or "").lower() == "closed":
                            result["baskets_closed"] += 1
                            continue
                        mid = int(st.master_trade_id)
                        call_pid, put_pid = self._master_trade_call_put_pids(
                            db, mid
                        )
                        try:
                            await self._mirror_exit_to_slave(
                                slave=slave,
                                slave_trade=st,
                                call_product_id=call_pid,
                                put_product_id=put_pid,
                                reason=reason_norm,
                                db=db,
                                hedge_product_id=None,
                            )
                        except Exception as exit_exc:
                            logger.error(
                                "[SLAVE_FORCE_CLOSE] slave=%s orphan basket "
                                "master=%s exit raised: %s",
                                slave_id,
                                mid,
                                exit_exc,
                            )

                    db.expire_all()
                    orphan_ok = await self._verify_slave_baskets_flat(
                        db=db,
                        slave=slave,
                        trades=open_trades,
                    )
                    if orphan_ok:
                        result["baskets_closed"] = result["baskets_found"]
                    else:
                        result["baskets_failed"] = result["baskets_found"]
                        result["failed_baskets"] = [
                            int(st.master_trade_id) for st in open_trades
                        ]

            if int(result["baskets_failed"] or 0) > 0:
                failed_ids = sorted(set(int(x) for x in result["failed_baskets"]))
                result["failed_baskets"] = failed_ids
                logger.critical(
                    "[SLAVE_FORCE_CLOSE_BLOCKED] slave=%s failed_baskets=%s",
                    slave_id,
                    failed_ids,
                )
                log_and_buffer(
                    "SLAVE_FORCE_CLOSE_BLOCKED",
                    0,
                    {
                        "slave": int(slave_id),
                        "failed_baskets": failed_ids,
                        "reason": reason_norm,
                    },
                )
                result["success"] = False
                return result

            # Close any stale active structure rows once positions are flat
            db.expire_all()
            still_active = (
                db.query(Structure)
                .filter(
                    Structure.slave_account_id == int(slave_id),
                    Structure.account_kind == "SLAVE",
                    Structure.status == "active",
                )
                .all()
            )
            if still_active:
                from backend.engine.structure_ledger import close_structure

                for struct in still_active:
                    close_structure(
                        db,
                        struct,
                        reason=reason_norm,
                        closed_at=force_close_batch_ts,
                    )
                    result["structures_closed"] += 1
                db.flush()

            slave.is_active = False
            slave.updated_at = get_utc_now()
            db.commit()

            result["success"] = True
            earner_uid = getattr(slave, "earner_user_id", None)
            logger.info(
                "[SLAVE_FORCE_CLOSE] slave=%s earner_user=%s reason=%s | "
                "baskets_closed=%s | hedge_closed=%s | structures_closed=%s",
                slave_id,
                earner_uid,
                reason_norm,
                result["baskets_closed"],
                result["hedge_closed"],
                result["structures_closed"],
            )
            log_and_buffer(
                "SLAVE_FORCE_CLOSE",
                0,
                {
                    "slave": int(slave_id),
                    "earner_user": earner_uid,
                    "reason": reason_norm,
                    "baskets_closed": result["baskets_closed"],
                    "hedge_closed": bool(result["hedge_closed"]),
                    "structures_closed": result["structures_closed"],
                },
            )
            return result

    async def _verify_slave_baskets_flat(
        self,
        *,
        db: Any,
        slave: SlaveAccount,
        trades: list[SlaveTrade],
    ) -> bool:
        """Exchange-truth: no live shorts for the given slave basket rows."""
        if not trades:
            return True
        if bool(getattr(slave, "is_virtual", False)):
            return all(
                str(st.status or "").lower() == "closed" for st in trades
            )

        client = self._get_slave_client(slave)
        try:
            try:
                positions = await client.get_option_positions()
            except Exception as pos_exc:
                logger.error(
                    "[SLAVE_FORCE_CLOSE] slave=%s positions fetch failed: %s",
                    slave.id,
                    pos_exc,
                )
                return False

            bot_owned = self._bot_owned_product_ids(db, int(slave.id))
            live_shorts = [
                int(p.get("product_id") or 0)
                for p in positions
                if float(p.get("size") or 0) < -1e-9
                and int(p.get("product_id") or 0) in bot_owned
            ]
            if live_shorts:
                return False

            for st in trades:
                mid = int(st.master_trade_id)
                call_pid, put_pid = self._master_trade_call_put_pids(db, mid)
                if call_pid or put_pid:
                    flat, _rem = await self._verify_slave_products_flat(
                        client, [call_pid, put_pid]
                    )
                    if not flat:
                        return False
                if str(st.status or "").lower() != "closed":
                    return False
            return True
        finally:
            await client.close()

    async def _cascade_close_slave_hedge(
        self,
        *,
        slave: SlaveAccount,
        slave_hedge: SlaveHedgePosition,
        db: Any,
        master_hedge_id: int,
        master_trade_ids: list[int],
        reason: str,
    ) -> dict[str, Any]:
        """
        Baskets → verify → hedge for one slave. Never naked-strangle.

        Caller MUST hold the per-slave lock.
        """
        slave_id = int(slave.id)
        baskets_found = 0
        baskets_closed = 0
        baskets_failed = 0
        failed_basket_ids: list[int] = []
        hedge_closed = False
        # Never-placed rows are not live baskets to count/close
        _ignore_statuses = {"skipped_low_capital", "skipped_no_hedge"}

        try:
            # Every slave basket under this master hedge (already-closed
            # included) — found must count all we consider, not only open.
            considered: list[SlaveTrade] = []
            if master_trade_ids:
                considered = (
                    db.query(SlaveTrade)
                    .filter(
                        SlaveTrade.slave_account_id == slave_id,
                        SlaveTrade.master_trade_id.in_(master_trade_ids),
                    )
                    .order_by(SlaveTrade.id.asc())
                    .all()
                )
            considered = [
                st
                for st in considered
                if str(st.status or "").lower() not in _ignore_statuses
            ]
            baskets_found = len(considered)

            # --- a. Close any still-open slave baskets ---
            for st in considered:
                if str(st.status or "").lower() == "closed":
                    continue
                mid = int(st.master_trade_id)
                call_pid, put_pid = self._master_trade_call_put_pids(db, mid)
                try:
                    await self._mirror_exit_to_slave(
                        slave=slave,
                        slave_trade=st,
                        call_product_id=call_pid,
                        put_product_id=put_pid,
                        reason=self._cascade_basket_exit_reason(reason),
                        db=db,
                        hedge_product_id=None,  # never touch structure hedge here
                    )
                except Exception as exit_exc:
                    logger.error(
                        "[SLAVE_HEDGE_CASCADE] slave=%s basket master=%s "
                        "exit raised: %s",
                        slave_id,
                        mid,
                        exit_exc,
                    )

            # --- b. Exchange verify — do not trust DB status alone ---
            db.expire_all()
            check_ids = {int(st.id) for st in considered}
            check_trades: list[SlaveTrade] = []
            if check_ids:
                check_trades = (
                    db.query(SlaveTrade)
                    .filter(SlaveTrade.id.in_(list(check_ids)))
                    .order_by(SlaveTrade.id.asc())
                    .all()
                )

            if not check_trades:
                baskets_closed = 0
            else:
                is_virtual = bool(getattr(slave, "is_virtual", False))
                client: DeltaClient | None = None
                try:
                    if not is_virtual:
                        client = self._get_slave_client(slave)
                    bot_owned = self._bot_owned_product_ids(db, slave_id)
                    # Hedge legs are longs — remaining *bot-owned* SHORT means
                    # a basket is still live (product_ids may differ after adj).
                    live_shorts: list[dict[str, Any]] = []
                    if client is not None:
                        try:
                            positions = await client.get_option_positions()
                        except Exception as pos_exc:
                            logger.error(
                                "[SLAVE_HEDGE_CASCADE] slave=%s positions "
                                "fetch failed: %s",
                                slave_id,
                                pos_exc,
                            )
                            positions = None
                        if positions is None:
                            for st in check_trades:
                                baskets_failed += 1
                                failed_basket_ids.append(
                                    int(st.master_trade_id)
                                )
                            positions = []
                        else:
                            for pos in positions:
                                try:
                                    size = float(pos.get("size") or 0)
                                    pid = int(pos.get("product_id") or 0)
                                except (TypeError, ValueError):
                                    continue
                                if size < -1e-9 and pid > 0 and pid in bot_owned:
                                    live_shorts.append(
                                        {
                                            "product_id": pid,
                                            "size": size,
                                            "symbol": str(
                                                pos.get("product_symbol") or ""
                                            ),
                                        }
                                    )

                    for st in check_trades:
                        mid = int(st.master_trade_id)
                        st_status = str(st.status or "").lower()
                        if is_virtual:
                            if st_status == "closed":
                                baskets_closed += 1
                            else:
                                baskets_failed += 1
                                failed_basket_ids.append(mid)
                            continue

                        call_pid, put_pid = self._master_trade_call_put_pids(
                            db, mid
                        )
                        # Per-trade hint flat + no live shorts on account
                        hint_flat = True
                        if client is not None and (call_pid or put_pid):
                            hint_flat, _rem = await self._verify_slave_products_flat(
                                client, [call_pid, put_pid]
                            )
                        if (
                            st_status == "closed"
                            and hint_flat
                            and not live_shorts
                        ):
                            baskets_closed += 1
                        else:
                            baskets_failed += 1
                            failed_basket_ids.append(mid)
                            if live_shorts:
                                logger.warning(
                                    "[SLAVE_HEDGE_CASCADE] slave=%s still has "
                                    "shorts=%s (blocks hedge close)",
                                    slave_id,
                                    live_shorts,
                                )
                finally:
                    if client is not None:
                        await client.close()

            # Invariant: found always covers closed
            if baskets_closed > baskets_found:
                baskets_found = baskets_closed

            if baskets_failed > 0:
                logger.critical(
                    "[SLAVE_HEDGE_CLOSE_BLOCKED] slave=%s | master_hedge=%s | "
                    "failed_baskets=%s",
                    slave_id,
                    master_hedge_id,
                    failed_basket_ids,
                )
                log_and_buffer(
                    "SLAVE_HEDGE_CLOSE_BLOCKED",
                    int(master_hedge_id),
                    {
                        "slave": slave_id,
                        "master_hedge": master_hedge_id,
                        "failed_baskets": failed_basket_ids,
                        "reason": reason,
                    },
                )
                hedge_closed = False
            else:
                # --- c. Close slave hedge legs, verify, persist ---
                hedge_closed = await self._close_slave_hedge_legs(
                    slave=slave,
                    slave_hedge=slave_hedge,
                    db=db,
                    reason=reason,
                )

        except Exception as exc:
            logger.error(
                "[SLAVE_HEDGE_CASCADE] slave=%s unexpected: %s",
                slave_id,
                exc,
                exc_info=True,
            )
            hedge_closed = False

        log_and_buffer(
            "SLAVE_HEDGE_CASCADE",
            int(master_hedge_id),
            {
                "master_hedge": master_hedge_id,
                "slave": slave_id,
                "reason": reason,
                "baskets_found": baskets_found,
                "baskets_closed": baskets_closed,
                "baskets_failed": baskets_failed,
                "hedge_closed": bool(hedge_closed),
                "failed_baskets": failed_basket_ids,
            },
        )
        logger.info(
            "[SLAVE_HEDGE_CASCADE] master_hedge=%s | slave=%s | reason=%s | "
            "baskets_found=%s | baskets_closed=%s | baskets_failed=%s | "
            "hedge_closed=%s",
            master_hedge_id,
            slave_id,
            reason,
            baskets_found,
            baskets_closed,
            baskets_failed,
            bool(hedge_closed),
        )
        return {
            "hedge_closed": bool(hedge_closed),
            "baskets_found": baskets_found,
            "baskets_closed": baskets_closed,
            "baskets_failed": baskets_failed,
            "failed_baskets": failed_basket_ids,
        }

    async def _close_slave_hedge_legs(
        self,
        *,
        slave: SlaveAccount,
        slave_hedge: SlaveHedgePosition,
        db: Any,
        reason: str,
    ) -> bool:
        """Sell both long legs, verify flat, mark slave_hedge_positions closed."""
        sh = slave_hedge
        slave_id = int(slave.id)
        if not self._assert_slave_hedge_close_allowed(
            slave_id=slave_id, reason=reason
        ):
            return False
        call_pid = int(getattr(sh, "call_product_id", 0) or 0)
        put_pid = int(getattr(sh, "put_product_id", 0) or 0)
        qty = max(1, int(getattr(sh, "quantity", 1) or 1))
        call_symbol = str(getattr(sh, "call_symbol", "") or "")
        put_symbol = str(getattr(sh, "put_symbol", "") or "")

        if call_pid <= 0 or put_pid <= 0:
            logger.error(
                "[SLAVE_HEDGE_CASCADE] slave=%s hedge=%s missing product ids",
                slave_id,
                getattr(sh, "id", None),
            )
            return False

        # Virtual: DB only
        if bool(getattr(slave, "is_virtual", False)) or str(
            getattr(sh, "call_order_id", "") or ""
        ).upper() == "VIRTUAL":
            entry_call = float(getattr(sh, "call_fill_price", 0) or 0)
            entry_put = float(getattr(sh, "put_fill_price", 0) or 0)
            sh.call_exit_price = entry_call
            sh.put_exit_price = entry_put
            sh.realized_pnl = 0.0
            sh.status = "closed"
            sh.exit_reason = reason
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            virt_hedge_close_ts = get_utc_now()
            sh.exit_time = virt_hedge_close_ts
            sh.last_error = None
            try:
                from backend.engine.structure_ledger import record_slave_hedge_close

                record_slave_hedge_close(
                    db,
                    slave_hedge=sh,
                    slave_account_id=slave_id,
                    reason=str(reason or ""),
                    call_closed_at=virt_hedge_close_ts,
                    put_closed_at=virt_hedge_close_ts,
                    structure_closed_at=virt_hedge_close_ts,
                )
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave hedge close failed: %s",
                    ledger_exc,
                    exc_info=True,
                )
            db.commit()
            return True

        from backend.engine.order_executor import OrderExecutor

        client = self._get_slave_client(slave)
        executor = OrderExecutor()
        call_order: dict[str, Any] | None = None
        put_order: dict[str, Any] | None = None
        call_close_ts: Any = None
        put_close_ts: Any = None
        call_close_fill_ts: Any = None
        put_close_fill_ts: Any = None
        # Captured BEFORE any order — this is an attribution window bound.
        # See e3e6b7d: a post-fill timestamp silently drops the fill.
        # Used for already-flat legs and structure_closed_at (shared batch).
        hedge_close_batch_ts = get_utc_now()
        try:
            for leg, pid, sym in (
                ("call", call_pid, call_symbol),
                ("put", put_pid, put_symbol),
            ):
                try:
                    exists = await client.verify_position_exists(int(pid))
                except Exception:
                    exists = True
                if not exists:
                    logger.info(
                        "[SLAVE_HEDGE_CASCADE] slave=%s %s already flat pid=%s",
                        slave_id,
                        leg,
                        pid,
                    )
                    # No fill of ours — shared batch bound ends the window
                    if leg == "call":
                        call_close_ts = hedge_close_batch_ts
                    else:
                        put_close_ts = hedge_close_batch_ts
                    continue
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                close_ts = get_utc_now()
                try:
                    order = await client.close_position(
                        product_id=int(pid),
                        size=int(qty),
                        is_long=True,
                    )
                    if leg == "call":
                        call_order = order if isinstance(order, dict) else None
                        call_close_ts = close_ts
                        call_close_fill_ts = get_utc_now()
                    else:
                        put_order = order if isinstance(order, dict) else None
                        put_close_ts = close_ts
                        put_close_fill_ts = get_utc_now()
                except Exception as close_exc:
                    logger.warning(
                        "[SLAVE_HEDGE_CASCADE] slave=%s close_position %s "
                        "failed: %s — OrderExecutor",
                        slave_id,
                        leg,
                        close_exc,
                    )
                    res = await executor.close_long_position(
                        product_id=int(pid),
                        quantity=int(qty),
                        delta_client=client,
                        symbol_for_fallback=sym or None,
                    )
                    if res.success:
                        payload = {
                            "order_id": res.order_id,
                            "avg_fill_price": res.filled_price,
                        }
                        fill_ts = get_utc_now()
                        if leg == "call":
                            call_order = payload
                            if call_close_ts is None:
                                call_close_ts = close_ts
                            call_close_fill_ts = fill_ts
                        else:
                            put_order = payload
                            if put_close_ts is None:
                                put_close_ts = close_ts
                            put_close_fill_ts = fill_ts
                    else:
                        logger.error(
                            "[SLAVE_HEDGE_CASCADE] slave=%s %s close failed: %s",
                            slave_id,
                            leg,
                            res.error,
                        )

            await asyncio.sleep(_HEDGE_VERIFY_PAUSE_SECONDS)
            flat, remaining = await self._verify_slave_products_flat(
                client, [call_pid, put_pid]
            )
            if not flat:
                err = f"hedge not flat after close remaining={remaining}"
                sh.status = "exit_failed"
                sh.last_error = err[:500]
                sh.error_count = int(sh.error_count or 0) + 1
                db.commit()
                logger.critical(
                    "[SLAVE_HEDGE_CASCADE] slave=%s hedge=%s %s",
                    slave_id,
                    sh.id,
                    err,
                )
                return False

            def _fill_from_order(order: dict[str, Any] | None) -> float:
                if not order:
                    return 0.0
                for key in (
                    "avg_fill_price",
                    "average_fill_price",
                    "filled_price",
                ):
                    try:
                        v = float(order.get(key) or 0)
                    except (TypeError, ValueError):
                        v = 0.0
                    if v > 0:
                        return v
                return 0.0

            call_exit = _fill_from_order(call_order)
            put_exit = _fill_from_order(put_order)
            if call_exit <= 0:
                try:
                    call_exit = float(
                        await client.resolve_fill_price(
                            call_order or {},
                            symbol_for_fallback=call_symbol or None,
                        )
                        or 0
                    )
                except Exception:
                    call_exit = 0.0
            if put_exit <= 0:
                try:
                    put_exit = float(
                        await client.resolve_fill_price(
                            put_order or {},
                            symbol_for_fallback=put_symbol or None,
                        )
                        or 0
                    )
                except Exception:
                    put_exit = 0.0

            entry_call = float(getattr(sh, "call_fill_price", 0) or 0)
            entry_put = float(getattr(sh, "put_fill_price", 0) or 0)
            realized: float | None = None
            if call_exit > 0 and put_exit > 0 and entry_call > 0 and entry_put > 0:
                realized = round(
                    ((call_exit - entry_call) + (put_exit - entry_put))
                    * qty
                    * _CONTRACT_SIZE,
                    6,
                )

            if call_close_ts is None:
                call_close_ts = hedge_close_batch_ts
            if put_close_ts is None:
                put_close_ts = hedge_close_batch_ts

            sh.call_exit_price = call_exit if call_exit > 0 else None
            sh.put_exit_price = put_exit if put_exit > 0 else None
            sh.realized_pnl = realized
            sh.status = "closed"
            sh.exit_reason = reason
            sh.exit_time = hedge_close_batch_ts
            sh.last_error = None
            try:
                from backend.engine.structure_ledger import record_slave_hedge_close

                record_slave_hedge_close(
                    db,
                    slave_hedge=sh,
                    slave_account_id=slave_id,
                    reason=str(reason or ""),
                    call_closed_at=call_close_ts,
                    put_closed_at=put_close_ts,
                    structure_closed_at=hedge_close_batch_ts,
                    call_fill_at=call_close_fill_ts,
                    put_fill_at=put_close_fill_ts,
                )
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave hedge close failed: %s",
                    ledger_exc,
                    exc_info=True,
                )
            db.commit()
            return True
        except Exception as exc:
            logger.error(
                "[SLAVE_HEDGE_CASCADE] slave=%s hedge close error: %s",
                slave_id,
                exc,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return False
        finally:
            await client.close()

    async def mirror_conversion_hedge_close(
        self,
        master_trade_id: int,
        hedge_product_id: int,
    ) -> None:
        """
        # Conversion Mode is OFF by default. Do not enable until this path
        # has been tested end-to-end on a slave with a live hedge.

        AUDIT-7: SELL-close legacy conversion hedge on active slaves.
        """
        if not hedge_product_id:
            return
        with self.db_factory() as db:
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == master_trade_id,
                    SlaveTrade.status == "active",
                )
                .all()
            )
            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if not slave or not slave.is_active:
                    continue
                async with self._slave_op_lock(
                    int(slave.id), "mirror_conversion_hedge_close"
                ) as acquired:
                    if not acquired:
                        continue
                    await self._mirror_conversion_hedge_close_to_slave(
                        slave=slave,
                        slave_trade=slave_trade,
                        hedge_product_id=int(hedge_product_id),
                        db=db,
                    )

    async def _mirror_conversion_hedge_close_to_slave(
        self,
        *,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        hedge_product_id: int,
        db: Any,
    ) -> None:
        """
        # Conversion Mode is OFF by default. Do not enable until this path
        # has been tested end-to-end on a slave with a live hedge.

        Caller MUST hold the per-slave lock.

        Closes conversion long via _close_with_reduce_only using LIVE size.
        Writes structure_leg closed_at; [LEDGER_MISS] if no structure.
        """
        client = self._get_slave_client(slave)
        hedge_pid = int(hedge_product_id)
        slave_id = int(slave.id)
        master_trade_id = int(slave_trade.master_trade_id or 0)
        try:
            from backend.engine.structure_ledger import (
                KIND_SLAVE,
                ROLE_HEDGE_CALL,
                ROLE_HEDGE_PUT,
                close_leg,
                find_open_leg,
                get_active_structure,
            )
            from backend.models import Trade as MasterTrade

            positions = await client.get_option_positions()
            live_size = self._position_size_for_product(positions, hedge_pid)
            if live_size is None or abs(float(live_size)) <= 1e-9:
                log_and_buffer(
                    "SLAVE_CONVERSION",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "op": "close",
                        "product_id": hedge_pid,
                        "live_size_before": live_size,
                        "size_used": 0,
                        "reason": "hedge_already_flat",
                    },
                )
                return

            # Long hedge → signed size > 0; reduce_only sells live size
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            closed_at = get_utc_now()
            ok, _ord, err = await self._close_with_reduce_only(
                client=client,
                slave=slave,
                product_id=hedge_pid,
                signed_size=float(live_size),
                master_trade_id=master_trade_id,
                path="mirror_conversion_hedge_close",
            )
            fill_at = get_utc_now()
            size_used = max(1, abs(int(round(float(live_size)))))
            log_and_buffer(
                "SLAVE_CONVERSION",
                master_trade_id,
                {
                    "slave": slave_id,
                    "op": "close",
                    "product_id": hedge_pid,
                    "live_size_before": live_size,
                    "size_used": size_used,
                    "reason": (
                        "conversion_hedge_close_ok"
                        if ok
                        else f"conversion_hedge_close_fail:{err[:80]}"
                    ),
                },
            )
            if not ok:
                slave_trade.last_error = (
                    f"conversion_hedge_close_failed: {err}"
                )[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                db.commit()
                return

            master_row = (
                db.query(MasterTrade)
                .filter(MasterTrade.id == master_trade_id)
                .first()
            )
            hid = (
                getattr(master_row, "hedge_position_id", None)
                if master_row is not None
                else None
            )
            struct = None
            if hid is not None:
                struct = get_active_structure(
                    db,
                    hedge_position_id=int(hid),
                    account_kind=KIND_SLAVE,
                    slave_account_id=slave_id,
                )
            if struct is None:
                log_and_buffer(
                    "LEDGER_MISS",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "reason": "no_active_structure",
                        "path": "mirror_conversion_hedge_close",
                    },
                )
                logger.error(
                    "[LEDGER_MISS] slave=%s reason=no_active_structure -- "
                    "conversion hedge close NOT recorded",
                    slave_id,
                )
                return

            open_row = find_open_leg(
                db,
                structure_id=int(struct.id),
                leg_role=ROLE_HEDGE_CALL,
                basket_seq=None,
                product_id=hedge_pid,
            )
            if open_row is None:
                open_row = find_open_leg(
                    db,
                    structure_id=int(struct.id),
                    leg_role=ROLE_HEDGE_PUT,
                    basket_seq=None,
                    product_id=hedge_pid,
                )
            if open_row is not None:
                close_leg(
                    db,
                    open_row,
                    reason="CONVERSION_HEDGE_CLOSE",
                    closed_at=closed_at,
                    structure=struct,
                    fill_at=fill_at,
                )
                db.commit()
            else:
                logger.error(
                    "[LEDGER_MISS] slave=%s reason=no_open_conversion_hedge_leg "
                    "product_id=%s -- close NOT recorded",
                    slave_id,
                    hedge_pid,
                )
                log_and_buffer(
                    "LEDGER_MISS",
                    master_trade_id,
                    {
                        "slave": slave_id,
                        "reason": "no_open_conversion_hedge_leg",
                        "product_id": hedge_pid,
                        "path": "mirror_conversion_hedge_close",
                    },
                )
        except Exception as exc:
            logger.error(
                "Slave '%s' conversion hedge close FAILED: %s",
                slave.name,
                exc,
            )
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            await client.close()

    async def mirror_leg_close(
        self,
        master_trade_id: int,
        leg_type: str,
        product_id: int,
        *,
        success_status: str | None = None,
        failure_status: str | None = None,
    ) -> dict[str, int]:
        """
        Close a single leg (call or put) on every non-closed slave.

        Used for partial-adjustment cleanup only. UI "Exit Basket (Call/Put)"
        goes through close_master_trade → mirror_exit (full basket), not here.

        Live-position targeting for product_id, reduce_only close, then verify
        that product is flat. Does NOT close the whole basket (use mirror_exit).

        Optional success_status / failure_status update SlaveTrade.status after
        each per-slave attempt (e.g. partial adj → 'partial' / 'exit_failed').
        """
        leg = str(leg_type).lower().strip()
        target_pid = int(product_id or 0)
        slaves_total = 0
        slaves_closed = 0
        slaves_failed = 0

        if target_pid <= 0 or leg not in ("call", "put"):
            logger.warning(
                "[MIRROR_LEG_CLOSE] Trade#%s invalid leg=%s product_id=%s",
                master_trade_id,
                leg,
                target_pid,
            )
            result = {
                "slaves_total": 0,
                "slaves_closed": 0,
                "slaves_failed": 0,
            }
            logger.info(
                "[MIRROR_LEG_CLOSE] trade_id=%s leg=%s product_id=%s "
                "slaves_total=%s slaves_closed=%s slaves_failed=%s",
                master_trade_id,
                leg,
                target_pid,
                0,
                0,
                0,
            )
            return result

        with self.db_factory() as db:
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == int(master_trade_id),
                    SlaveTrade.status != "closed",
                )
                .all()
            )
            slaves_total = len(slave_trades)
            logger.info(
                "[MIRROR_LEG_CLOSE] Trade#%s closing %s product=%s on "
                "%s non-closed slaves",
                master_trade_id,
                leg,
                target_pid,
                slaves_total,
            )

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if not slave or not slave.is_active:
                    slaves_failed += 1
                    if failure_status:
                        slave_trade.status = failure_status
                        slave_trade.last_updated = get_utc_now()
                        db.commit()
                    continue

                async with self._slave_op_lock(
                    int(slave.id), "mirror_leg_close"
                ) as acquired:
                    if not acquired:
                        slaves_failed += 1
                        continue
                    ok = await self._mirror_leg_close_to_slave(
                        slave=slave,
                        slave_trade=slave_trade,
                        leg_type=leg,
                        product_id=target_pid,
                        db=db,
                        success_status=success_status,
                        failure_status=failure_status,
                    )
                    if ok:
                        slaves_closed += 1
                    else:
                        slaves_failed += 1

        result = {
            "slaves_total": slaves_total,
            "slaves_closed": slaves_closed,
            "slaves_failed": slaves_failed,
        }
        logger.info(
            "[MIRROR_LEG_CLOSE] trade_id=%s leg=%s product_id=%s "
            "slaves_total=%s slaves_closed=%s slaves_failed=%s",
            master_trade_id,
            leg,
            target_pid,
            result["slaves_total"],
            result["slaves_closed"],
            result["slaves_failed"],
        )
        return result

    def _write_slave_product_leg_close_ledger(
        self,
        db: Any,
        *,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        leg_type: str,
        product_id: int,
        closed_at: Any,
        fill_at: Any = None,
        reason: str = "LEG_CLOSE",
    ) -> str:
        """
        Close one basket StructureLeg window for product_id.

        Returns ``written`` or ``missing`` (no leg row / no structure).
        """
        from backend.engine.structure_ledger import (
            ROLE_BASKET_CALL,
            ROLE_BASKET_PUT,
            close_leg,
            find_open_leg,
            get_active_structure,
            KIND_SLAVE,
            record_slave_adjustment_close,
        )
        from backend.models import StructureLeg, Trade as MasterTrade

        slave_id = int(slave.id)
        pid = int(product_id or 0)
        leg = str(leg_type or "").lower()
        master_row = (
            db.query(MasterTrade)
            .filter(MasterTrade.id == int(slave_trade.master_trade_id or 0))
            .first()
        )
        closed_ok = record_slave_adjustment_close(
            db,
            slave_account_id=slave_id,
            master_trade=master_row,
            triggered_leg=leg,
            reason=reason,
            old_leg_closed_at=closed_at,
            old_leg_fill_at=fill_at,
            old_product_id=pid if pid > 0 else None,
        )
        if closed_ok:
            return "written"

        # Already closed, or never recorded — distinguish for LEDGER_MISS
        hid = (
            getattr(master_row, "hedge_position_id", None)
            if master_row is not None
            else None
        )
        if hid is None:
            logger.error(
                "[LEDGER_MISS] slave=%s product_id=%s reason=leg_not_found -- "
                "closed on exchange but no leg row",
                slave_id,
                pid,
            )
            log_and_buffer(
                "LEDGER_MISS",
                int(slave_trade.master_trade_id or 0),
                {
                    "slave": slave_id,
                    "product_id": pid,
                    "reason": "leg_not_found",
                    "note": "closed_on_exchange_but_no_leg_row",
                },
            )
            return "missing"

        struct = get_active_structure(
            db,
            hedge_position_id=int(hid),
            account_kind=KIND_SLAVE,
            slave_account_id=slave_id,
        )
        if struct is None:
            logger.error(
                "[LEDGER_MISS] slave=%s product_id=%s reason=leg_not_found -- "
                "closed on exchange but no leg row",
                slave_id,
                pid,
            )
            log_and_buffer(
                "LEDGER_MISS",
                int(slave_trade.master_trade_id or 0),
                {
                    "slave": slave_id,
                    "product_id": pid,
                    "reason": "leg_not_found",
                    "note": "closed_on_exchange_but_no_leg_row",
                },
            )
            return "missing"

        existing = (
            db.query(StructureLeg)
            .filter(
                StructureLeg.structure_id == int(struct.id),
                StructureLeg.product_id == pid,
            )
            .order_by(StructureLeg.id.desc())
            .first()
        )
        if existing is not None and existing.closed_at is not None:
            return "written"

        role = ROLE_BASKET_CALL if leg == "call" else ROLE_BASKET_PUT
        basket_seq = (
            getattr(master_row, "basket_seq_in_structure", None)
            if master_row is not None
            else None
        )
        bs = int(basket_seq) if basket_seq is not None else None
        open_row = find_open_leg(
            db,
            structure_id=int(struct.id),
            leg_role=role,
            basket_seq=bs,
            product_id=pid if pid > 0 else None,
        )
        if open_row is None and pid > 0:
            open_row = find_open_leg(
                db,
                structure_id=int(struct.id),
                leg_role=role,
                basket_seq=bs,
            )
        if open_row is not None and closed_at is not None:
            close_leg(
                db,
                open_row,
                reason=reason,
                closed_at=closed_at,
                structure=struct,
                fill_at=fill_at,
            )
            db.flush()
            return "written"

        logger.error(
            "[LEDGER_MISS] slave=%s product_id=%s reason=leg_not_found -- "
            "closed on exchange but no leg row",
            slave_id,
            pid,
        )
        log_and_buffer(
            "LEDGER_MISS",
            int(slave_trade.master_trade_id or 0),
            {
                "slave": slave_id,
                "product_id": pid,
                "reason": "leg_not_found",
                "note": "closed_on_exchange_but_no_leg_row",
            },
        )
        return "missing"

    async def _mirror_leg_close_to_slave(
        self,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        leg_type: str,
        product_id: int,
        db: Any,
        *,
        success_status: str | None = None,
        failure_status: str | None = None,
    ) -> bool:
        """
        Close one product on one slave. Returns True if verified flat (or
        already flat / virtual).

        Caller MUST hold the per-slave lock.
        """
        leg = str(leg_type).lower()
        target_pid = int(product_id)
        master_trade_id = int(slave_trade.master_trade_id or 0)

        def _mark_failure(msg: str) -> None:
            slave_trade.last_error = msg[:500]
            slave_trade.error_count = int(slave_trade.error_count or 0) + 1
            slave_trade.last_updated = get_utc_now()
            if failure_status:
                slave_trade.status = failure_status
                slave.connection_status = "error"
                slave.last_error = msg[:500]
            db.commit()

        def _mark_success() -> None:
            if success_status:
                slave_trade.status = success_status
                slave_trade.last_error = (
                    f"partial: closed {leg} product={target_pid} "
                    f"(one-legged, matching master)"
                )[:500]
            else:
                slave_trade.last_error = None
            slave_trade.last_updated = get_utc_now()
            db.commit()

        if is_virtual_slave_trade(slave, slave_trade):
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            closed_at = get_utc_now()
            ledger = self._write_slave_product_leg_close_ledger(
                db,
                slave=slave,
                slave_trade=slave_trade,
                leg_type=leg,
                product_id=target_pid,
                closed_at=closed_at,
                fill_at=closed_at,
                reason="LEG_CLOSE",
            )
            log_and_buffer(
                "SLAVE_LEG_CLOSE",
                master_trade_id,
                {
                    "slave": int(slave.id),
                    "product_id": target_pid,
                    "live_size": 0,
                    "ledger": ledger,
                    "outcome": "ok",
                    "virtual": True,
                },
            )
            if leg == "call":
                slave_trade.call_order_id = "VIRTUAL_CLOSED"
                slave_trade.call_sl_order_id = None
            else:
                slave_trade.put_order_id = "VIRTUAL_CLOSED"
                slave_trade.put_sl_order_id = None
            _mark_success()
            return True

        client = self._get_slave_client(slave)
        try:
            # Cancel leg-specific legacy SL if present
            sl_id = (
                slave_trade.call_sl_order_id
                if leg == "call"
                else slave_trade.put_sl_order_id
            )
            if sl_id:
                try:
                    await client.cancel_order(int(sl_id))
                except Exception as exc:
                    logger.warning(
                        "[MIRROR_LEG_CLOSE] SL cancel failed slave='%s': %s",
                        slave.name,
                        exc,
                    )
                if leg == "call":
                    slave_trade.call_sl_order_id = None
                else:
                    slave_trade.put_sl_order_id = None

            try:
                live_positions = await client.get_option_positions()
                fetch_ok = True
            except Exception as pos_exc:
                fetch_ok = False
                live_positions = []
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    master_trade_id,
                    {
                        "slave": int(slave.id),
                        "product_id": target_pid,
                        "live_size": None,
                        "ledger": "missing",
                        "outcome": "failed",
                        "reason": f"positions_fetch:{pos_exc}",
                    },
                )

            if not fetch_ok:
                msg = (
                    f"leg_close_failed: positions fetch failed for {leg} "
                    f"product={target_pid}"
                )
                _mark_failure(msg)
                return False

            live_size = self._position_size_for_product(
                live_positions, target_pid
            )
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            closed_at = get_utc_now()

            if live_size is None or abs(float(live_size)) <= 1e-9:
                ledger = self._write_slave_product_leg_close_ledger(
                    db,
                    slave=slave,
                    slave_trade=slave_trade,
                    leg_type=leg,
                    product_id=target_pid,
                    closed_at=closed_at,
                    fill_at=closed_at,
                    reason="LEG_CLOSE",
                )
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    master_trade_id,
                    {
                        "slave": int(slave.id),
                        "product_id": target_pid,
                        "live_size": live_size if live_size is not None else 0,
                        "ledger": ledger,
                        "outcome": "ok",
                        "reason": "already_flat",
                    },
                )
                if leg == "call":
                    slave_trade.call_sl_order_id = None
                else:
                    slave_trade.put_sl_order_id = None
                _mark_success()
                return True

            ok, _order, err = await self._close_with_reduce_only(
                client=client,
                slave=slave,
                product_id=target_pid,
                signed_size=float(live_size),
                master_trade_id=master_trade_id,
                path="_mirror_leg_close_to_slave",
            )
            fill_at = get_utc_now()
            if not ok:
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    master_trade_id,
                    {
                        "slave": int(slave.id),
                        "product_id": target_pid,
                        "live_size": live_size,
                        "ledger": "missing",
                        "outcome": "failed",
                        "reason": err[:200],
                    },
                )
                msg = (
                    f"leg_close_failed: {leg} product={target_pid} {err}"
                )
                _mark_failure(msg)
                return False

            await asyncio.sleep(2)

            try:
                verify_positions = await client.get_option_positions()
                verify_ok = True
            except Exception as verify_exc:
                verify_ok = False
                verify_positions = []
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    master_trade_id,
                    {
                        "slave": int(slave.id),
                        "product_id": target_pid,
                        "live_size": live_size,
                        "ledger": "missing",
                        "outcome": "failed",
                        "reason": f"verify_fetch:{verify_exc}",
                    },
                )

            still = (
                self._position_size_for_product(verify_positions, target_pid)
                if verify_ok
                else None
            )

            if (not verify_ok) or (
                still is not None and abs(float(still)) > 0
            ):
                msg = (
                    f"leg_close_failed: {leg} product={target_pid} "
                    f"still_size={still} verify_ok={verify_ok}"
                )
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    master_trade_id,
                    {
                        "slave": int(slave.id),
                        "product_id": target_pid,
                        "live_size": still,
                        "ledger": "missing",
                        "outcome": "failed",
                        "reason": msg[:200],
                    },
                )
                _mark_failure(msg)
                return False

            ledger = self._write_slave_product_leg_close_ledger(
                db,
                slave=slave,
                slave_trade=slave_trade,
                leg_type=leg,
                product_id=target_pid,
                closed_at=closed_at,
                fill_at=fill_at,
                reason="LEG_CLOSE",
            )
            log_and_buffer(
                "SLAVE_LEG_CLOSE",
                master_trade_id,
                {
                    "slave": int(slave.id),
                    "product_id": target_pid,
                    "live_size": live_size,
                    "ledger": ledger,
                    "outcome": "ok",
                },
            )

            if leg == "call":
                slave_trade.call_sl_order_id = None
            else:
                slave_trade.put_sl_order_id = None
            _mark_success()
            return True
        except Exception as exc:
            log_and_buffer(
                "SLAVE_LEG_CLOSE",
                master_trade_id,
                {
                    "slave": int(slave.id),
                    "product_id": target_pid,
                    "live_size": None,
                    "ledger": "missing",
                    "outcome": "failed",
                    "reason": f"exception:{exc}",
                },
            )
            try:
                _mark_failure(f"leg_close_failed: exception {exc}")
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            return False
        finally:
            await client.close()

    async def mirror_exit(
        self,
        master_trade_id: int,
        call_product_id: int,
        put_product_id: int,
        reason: str,
        hedge_product_id: int | None = None,
    ) -> None:
        """
        Mirror trade exit on all slaves.

        Closes CURRENT live Delta option positions (post-adjustment product_ids
        may differ from entry). Hint product_ids are logged for diagnosis only.
        """
        with self.db_factory() as db:
            # Every non-closed SlaveTrade for this master — entry failures
            # ('error'/'partial') and exit_failed rows often still hold live legs.
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == master_trade_id,
                    SlaveTrade.status != "closed",
                )
                .all()
            )

            if not slave_trades:
                log_and_buffer(
                    "MIRROR_EXIT",
                    int(master_trade_id),
                    {
                        "stage": "no_slaves",
                        "reason": reason,
                        "slaves_total": 0,
                        "hint_call": int(call_product_id or 0),
                        "hint_put": int(put_product_id or 0),
                        "hint_hedge": int(hedge_product_id or 0)
                        if hedge_product_id
                        else None,
                    },
                )
                logger.info(
                    "[MIRROR_EXIT] Trade#%s — no non-closed slave_trades "
                    "(call=%s put=%s reason=%s)",
                    master_trade_id,
                    call_product_id,
                    put_product_id,
                    reason,
                )
                return

            log_and_buffer(
                "MIRROR_EXIT",
                int(master_trade_id),
                {
                    "stage": "start",
                    "reason": reason,
                    "slaves_total": len(slave_trades),
                    "hint_call": int(call_product_id or 0),
                    "hint_put": int(put_product_id or 0),
                    "hint_hedge": int(hedge_product_id or 0)
                    if hedge_product_id
                    else None,
                    "statuses": [str(st.status) for st in slave_trades],
                },
            )
            logger.info(
                "[MIRROR_EXIT] Trade#%s mirroring to %s slaves: "
                "reason=%s hint_call=%s hint_put=%s hint_hedge=%s "
                "statuses=%s",
                master_trade_id,
                len(slave_trades),
                reason,
                call_product_id,
                put_product_id,
                hedge_product_id,
                [str(st.status) for st in slave_trades],
            )

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if not slave:
                    continue

                async with self._slave_op_lock(
                    int(slave.id), "mirror_exit"
                ) as acquired:
                    if not acquired:
                        continue
                    await self._mirror_exit_to_slave(
                        slave=slave,
                        slave_trade=slave_trade,
                        call_product_id=call_product_id,
                        put_product_id=put_product_id,
                        reason=reason,
                        db=db,
                        hedge_product_id=hedge_product_id,
                    )

    async def _mirror_exit_to_slave(
        self,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        call_product_id: int,
        put_product_id: int,
        reason: str,
        db: Any,
        hedge_product_id: int | None = None,
    ) -> None:
        # Caller MUST hold the per-slave lock.
        # Virtual mode: close in DB only — no real Delta orders
        if is_virtual_slave_trade(slave, slave_trade):
            logger.info(
                "VIRTUAL EXIT: slave='%s' master_trade_id=%s "
                "(no real order placed)",
                slave.name,
                slave_trade.master_trade_id,
            )
            with self.db_factory() as virt_db:
                st = (
                    virt_db.query(SlaveTrade)
                    .filter(
                        SlaveTrade.slave_account_id == slave.id,
                        SlaveTrade.master_trade_id
                        == slave_trade.master_trade_id,
                        SlaveTrade.status != "closed",
                    )
                    .first()
                )
                if st:
                    # Captured BEFORE any order — this is an attribution window bound.
                    # See e3e6b7d: a post-fill timestamp silently drops the fill.
                    virt_basket_close_ts = get_utc_now()
                    self._close_slave_trade(
                        slave,
                        st,
                        reason=f"virtual_master_exit:{reason}",
                        allow_virtual=True,
                    )
                    st.call_sl_order_id = None
                    st.put_sl_order_id = None
                    st.call_exit_price = float(st.call_fill_price or 0) or None
                    st.put_exit_price = float(st.put_fill_price or 0) or None
                    st.call_exit_fee_usd = 0.0
                    st.put_exit_fee_usd = 0.0
                    st.exit_time = virt_basket_close_ts
                    st.exit_reason = str(reason or "")[:50]
                    self._apply_slave_realized_pnl(st)
                    st.last_updated = get_utc_now()
                    try:
                        from backend.engine.structure_ledger import (
                            record_slave_basket_exit,
                        )
                        from backend.models import Trade as MasterTrade

                        master_row = (
                            virt_db.query(MasterTrade)
                            .filter(
                                MasterTrade.id
                                == int(st.master_trade_id or 0)
                            )
                            .first()
                        )
                        record_slave_basket_exit(
                            virt_db,
                            slave_trade=st,
                            slave_account_id=int(slave.id),
                            master_trade=master_row,
                            reason=str(reason or ""),
                            call_closed_at=virt_basket_close_ts,
                            put_closed_at=virt_basket_close_ts,
                            call_fill_at=virt_basket_close_ts,
                            put_fill_at=virt_basket_close_ts,
                            wing_call_closed_at=(
                                virt_basket_close_ts
                                if int(
                                    getattr(st, "wing_call_product_id", 0)
                                    or 0
                                )
                                > 0
                                else None
                            ),
                            wing_put_closed_at=(
                                virt_basket_close_ts
                                if int(
                                    getattr(st, "wing_put_product_id", 0) or 0
                                )
                                > 0
                                else None
                            ),
                            wing_call_fill_at=(
                                virt_basket_close_ts
                                if int(
                                    getattr(st, "wing_call_product_id", 0)
                                    or 0
                                )
                                > 0
                                else None
                            ),
                            wing_put_fill_at=(
                                virt_basket_close_ts
                                if int(
                                    getattr(st, "wing_put_product_id", 0) or 0
                                )
                                > 0
                                else None
                            ),
                        )
                    except Exception as ledger_exc:
                        logger.error(
                            "structure ledger slave basket exit failed: %s",
                            ledger_exc,
                            exc_info=True,
                        )
                    virt_db.commit()
                    logger.info(
                        "VIRTUAL EXIT done: slave='%s' slave_trade_id=%s "
                        "realized_pnl=%s",
                        slave.name,
                        st.id,
                        getattr(st, "realized_pnl", None),
                    )
            # Also mark the in-session object closed so caller state is consistent
            self._close_slave_trade(
                slave,
                slave_trade,
                reason=f"virtual_master_exit:{reason}",
                allow_virtual=True,
            )
            slave_trade.call_exit_price = float(
                slave_trade.call_fill_price or 0
            ) or None
            slave_trade.put_exit_price = float(
                slave_trade.put_fill_price or 0
            ) or None
            slave_trade.call_exit_fee_usd = 0.0
            slave_trade.put_exit_fee_usd = 0.0
            slave_trade.exit_time = get_utc_now()
            slave_trade.exit_reason = str(reason or "")[:50]
            self._apply_slave_realized_pnl(slave_trade)
            return

        # qty=0 rows (skipped_low_capital / never filled) — never send close orders
        stored_qty_raw = abs(int(slave_trade.actual_quantity or 0))
        if stored_qty_raw <= 0:
            skip_msg = (
                str(slave_trade.last_error or "").strip()
                or (
                    "entry_skipped_qty_0: no position opened "
                    f"(status={slave_trade.status})"
                )
            )
            log_and_buffer(
                "SLAVE_CLOSE_SKIP_ZERO_QTY",
                int(slave_trade.master_trade_id or 0),
                {
                    "slave_account_id": int(slave.id),
                    "slave_name": str(slave.name or ""),
                    "slave_trade_id": int(getattr(slave_trade, "id", 0) or 0),
                    "master_trade_id": int(slave_trade.master_trade_id or 0),
                    "actual_quantity": stored_qty_raw,
                    "status": str(slave_trade.status or ""),
                    "reason": str(reason or ""),
                    "last_error": skip_msg[:500],
                },
            )
            if not str(slave_trade.last_error or "").strip():
                slave_trade.last_error = skip_msg[:500]
            self._close_slave_trade(
                slave,
                slave_trade,
                reason=f"zero_qty_skip:{reason}",
                allow_virtual=False,
            )
            slave_trade.exit_time = get_utc_now()
            slave_trade.exit_reason = str(reason or "")[:50]
            slave_trade.last_updated = get_utc_now()
            try:
                db.commit()
            except Exception as commit_exc:
                logger.warning(
                    "[SLAVE_CLOSE_SKIP_ZERO_QTY] commit failed slave=%s: %s",
                    slave.id,
                    commit_exc,
                )
            return

        client = self._get_slave_client(slave)
        # Guaranteed >= 1 here — zero-qty rows returned above (RULE 10)
        stored_qty = abs(int(slave_trade.actual_quantity or 0))
        # Structure long straddle must survive normal basket exits
        protected_hedge_pids = self._structure_hedge_pids_for_slave(
            db, int(slave.id)
        )

        try:
            # Cancel SL orders first
            for sl_id in (
                slave_trade.call_sl_order_id,
                slave_trade.put_sl_order_id,
            ):
                if sl_id:
                    try:
                        await client.cancel_order(int(sl_id))
                    except Exception as exc:
                        logger.warning("SL cancel failed: %s", exc)

            # Close CURRENT live positions — do NOT rely on hint product_ids
            # (they go stale after adjustment if slave legs differ from master).
            positions_fetch_ok = True
            try:
                live_positions = await client.get_option_positions()
            except Exception as pos_exc:
                positions_fetch_ok = False
                live_positions = []
                logger.error(
                    "[MIRROR_EXIT] Slave '%s' get_option_positions FAILED: %s "
                    "— will try hint product_ids then VERIFY before close mark "
                    "(fetch failure is NOT treated as flat)",
                    slave.name,
                    pos_exc,
                )

            hint_ids = {
                int(pid)
                for pid in (
                    call_product_id,
                    put_product_id,
                    hedge_product_id or 0,
                    getattr(slave_trade, "wing_call_product_id", 0) or 0,
                    getattr(slave_trade, "wing_put_product_id", 0) or 0,
                )
                if pid and int(pid) > 0
            }
            bot_owned = self._bot_owned_product_ids(db, int(slave.id))
            # Current exit hints for this basket are always bot-owned for this trade
            bot_owned |= hint_ids
            logger.info(
                "[MIRROR_EXIT] Slave '%s' positions_fetch_ok=%s "
                "live_positions=%s hint_ids=%s bot_owned=%s stored_qty=%s",
                slave.name,
                positions_fetch_ok,
                [
                    {
                        "product_id": int(p.get("product_id") or 0),
                        "symbol": str(p.get("product_symbol") or ""),
                        "size": float(p.get("size") or 0),
                    }
                    for p in live_positions
                ],
                sorted(hint_ids),
                sorted(bot_owned),
                stored_qty,
            )

            targets: list[dict[str, Any]] = []
            foreign_notes: list[str] = []
            if live_positions:
                for p in live_positions:
                    try:
                        pid = int(p.get("product_id") or 0)
                        size = float(p.get("size") or 0)
                    except (TypeError, ValueError):
                        continue
                    if pid <= 0 or abs(size) <= 0:
                        continue
                    if pid in bot_owned:
                        targets.append(p)
                        continue
                    # Customer / non-bot position — never buy back or sell
                    log_and_buffer(
                        "SLAVE_FOREIGN",
                        int(slave_trade.master_trade_id or 0),
                        {
                            "slave": int(slave.id),
                            "product_id": pid,
                            "size": size,
                            "symbol": str(p.get("product_symbol") or ""),
                            "path": "_mirror_exit_to_slave",
                            "note": "left untouched (not bot-owned)",
                        },
                    )
                    foreign_notes.append(f"pid={pid} size={size}")
                if foreign_notes:
                    note = (
                        "foreign_left_untouched: " + "; ".join(foreign_notes)
                    )[:500]
                    prior = str(slave_trade.last_error or "")
                    slave_trade.last_error = (
                        f"{prior};{note}".strip(";") if prior else note
                    )[:500]
            else:
                # Empty book OR fetch failed — last resort: bot-owned hint pids only
                if positions_fetch_ok:
                    logger.info(
                        "[MIRROR_EXIT] Slave '%s' verified empty option book "
                        "— still trying hint product_ids as safety net "
                        "call=%s put=%s hedge=%s",
                        slave.name,
                        call_product_id,
                        put_product_id,
                        hedge_product_id,
                    )
                else:
                    logger.warning(
                        "[MIRROR_EXIT] Slave '%s' positions fetch FAILED — "
                        "trying hint product_ids call=%s put=%s hedge=%s "
                        "(will NOT mark closed without verify)",
                        slave.name,
                        call_product_id,
                        put_product_id,
                        hedge_product_id,
                    )
                for pid, side in (
                    (int(call_product_id or 0), "buy"),
                    (int(put_product_id or 0), "buy"),
                    (
                        int(
                            getattr(slave_trade, "wing_call_product_id", 0)
                            or 0
                        ),
                        "sell",
                    ),
                    (
                        int(
                            getattr(slave_trade, "wing_put_product_id", 0)
                            or 0
                        ),
                        "sell",
                    ),
                ):
                    if pid <= 0 or pid not in bot_owned:
                        continue
                    targets.append(
                        {
                            "product_id": pid,
                            "size": (
                                -float(stored_qty)
                                if side == "buy"
                                else float(stored_qty)
                            ),
                            "product_symbol": f"hint-{pid}",
                            "_fallback_side": side,
                        }
                    )
                if hedge_product_id and int(hedge_product_id) in bot_owned:
                    targets.append(
                        {
                            "product_id": int(hedge_product_id),
                            "size": float(stored_qty),
                            "product_symbol": (
                                f"hint-hedge-{hedge_product_id}"
                            ),
                            "_fallback_side": "sell",
                        }
                    )

            # Hard rule: never sell structure-hedge longs on a basket exit path
            if protected_hedge_pids and targets:
                filtered_targets: list[dict[str, Any]] = []
                for p in targets:
                    try:
                        pid = int(p.get("product_id") or 0)
                        size = float(p.get("size") or 0)
                    except (TypeError, ValueError):
                        filtered_targets.append(p)
                        continue
                    # Longs on structure hedge products are protected
                    if pid in protected_hedge_pids and (
                        size > 0
                        or str(p.get("_fallback_side") or "").lower() == "sell"
                    ):
                        logger.critical(
                            "[SLAVE_HEDGE_PROTECTED] slave=%s | "
                            "attempted_by=%s",
                            slave.id,
                            reason,
                        )
                        log_and_buffer(
                            "SLAVE_HEDGE_PROTECTED",
                            int(slave_trade.master_trade_id or 0),
                            {
                                "slave": int(slave.id),
                                "attempted_by": str(reason),
                                "product_id": pid,
                                "path": "_mirror_exit_to_slave",
                            },
                        )
                        continue
                    filtered_targets.append(p)
                targets = filtered_targets

            closed_count = 0
            target_pids: set[int] = set()
            exit_by_pid: dict[int, dict[str, Any]] = {}
            call_pid_hint = int(
                getattr(slave_trade, "call_product_id", None)
                or call_product_id
                or 0
            )
            put_pid_hint = int(
                getattr(slave_trade, "put_product_id", None)
                or put_product_id
                or 0
            )
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            # Used for already-flat legs so ledger windows always end.
            exit_batch_ts = get_utc_now()

            # Exit order: SHORTS first, then WINGS (never reverse — NIYAM 0)
            from backend.engine.slave_wings import (
                sort_exit_targets,
                wing_dicts_from_slave_trade,
            )

            short_pids, wing_pids = wing_dicts_from_slave_trade(slave_trade)
            short_pids |= {
                pid
                for pid in (call_pid_hint, put_pid_hint)
                if pid > 0
            }
            targets = sort_exit_targets(
                targets, short_pids=short_pids, wing_pids=wing_pids
            )

            wing_close_failed = False
            for pos in targets:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
                sym = str(pos.get("product_symbol") or "")
                if pid <= 0 or size == 0:
                    continue
                # Belt-and-suspenders: skip protected longs even if filter missed
                if pid in protected_hedge_pids and size > 0:
                    logger.critical(
                        "[SLAVE_HEDGE_PROTECTED] slave=%s | attempted_by=%s",
                        slave.id,
                        reason,
                    )
                    continue
                target_pids.add(pid)
                close_size = max(1, abs(int(size)))
                if size < 0:
                    side = "buy"  # close short
                else:
                    side = "sell"  # close long (conversion hedge only)
                # Explicit fallback override when inventing hint rows
                if pos.get("_fallback_side"):
                    side = str(pos["_fallback_side"])
                if (
                    pid in protected_hedge_pids
                    and str(side).lower() == "sell"
                ):
                    logger.critical(
                        "[SLAVE_HEDGE_PROTECTED] slave=%s | attempted_by=%s",
                        slave.id,
                        reason,
                    )
                    continue
                # Hint rows invent size sign from _fallback_side
                signed_for_close = float(size)
                if pos.get("_fallback_side"):
                    fb = str(pos["_fallback_side"]).lower()
                    signed_for_close = (
                        -abs(float(stored_qty))
                        if fb == "buy"
                        else abs(float(stored_qty))
                    )
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                close_ts = get_utc_now()
                ok, close_order, err = await self._close_with_reduce_only(
                    client=client,
                    slave=slave,
                    product_id=pid,
                    signed_size=signed_for_close,
                    master_trade_id=int(slave_trade.master_trade_id or 0),
                    path="_mirror_exit_to_slave",
                )
                if not ok:
                    logger.error(
                        "[MIRROR_EXIT] Slave '%s' FAILED close "
                        "product=%s size=%s side=%s: %s",
                        slave.name,
                        pid,
                        close_size,
                        side,
                        err,
                    )
                    if pid in wing_pids:
                        wing_close_failed = True
                        logger.critical(
                            "[WING_CLOSE_FAILED] slave=%s product_id=%s "
                            "err=%s",
                            slave.id,
                            pid,
                            err,
                        )
                    continue
                closed_count += 1
                logger.info(
                    "[MIRROR_EXIT] Slave '%s' closed %s product=%s "
                    "size=%s side=%s",
                    slave.name,
                    sym,
                    pid,
                    close_size,
                    side,
                )
                if close_order is not None:
                    try:
                        exit_fill = float(
                            await client.resolve_fill_price(
                                close_order, symbol_for_fallback=sym or None
                            )
                            or 0.0
                        )
                    except Exception:
                        exit_fill = 0.0
                    exit_fee = await self._resolve_order_fee(
                        client, close_order
                    )
                    exit_by_pid[pid] = {
                        "fill": exit_fill,
                        "fee": float(exit_fee or 0.0),
                        "closed_at": close_ts,
                        "fill_at": get_utc_now(),
                    }
                else:
                    # Flat after exception — no order payload; still record close time
                    exit_by_pid[pid] = {
                        "fill": 0.0,
                        "fee": 0.0,
                        "closed_at": close_ts,
                        "fill_at": get_utc_now(),
                    }

            # Products that must be flat: basket hints + closed targets ONLY
            # (never require structure hedge longs or foreign positions to be flat)
            check_pids = set(hint_ids) | target_pids
            check_pids.discard(0)
            check_pids -= protected_hedge_pids
            check_pids &= bot_owned

            # VERIFY flat before marking closed — never trust order success alone
            await asyncio.sleep(2)
            try:
                verify_positions = await client.get_option_positions()
                verify_fetch_ok = True
            except Exception as verify_exc:
                verify_fetch_ok = False
                verify_positions = []
                logger.critical(
                    "[MIRROR_EXIT] Slave '%s' VERIFY get_option_positions "
                    "FAILED: %s — marking exit_failed (not closed)",
                    slave.name,
                    verify_exc,
                )

            remaining: list[dict[str, Any]] = []
            if verify_fetch_ok:
                for p in verify_positions:
                    try:
                        pid = int(p.get("product_id") or 0)
                        size = float(p.get("size") or 0)
                    except (TypeError, ValueError):
                        continue
                    if pid <= 0 or abs(size) <= 0:
                        continue
                    if pid not in bot_owned:
                        continue
                    if pid in protected_hedge_pids and size > 0:
                        continue
                    # If we have specific pids to check, only those matter;
                    # if none (already-flat path with no hints), any leftover
                    # *bot-owned* option size blocks closed.
                    if check_pids and pid not in check_pids:
                        continue
                    remaining.append(
                        {
                            "product_id": pid,
                            "symbol": str(p.get("product_symbol") or ""),
                            "size": size,
                        }
                    )

            if (not verify_fetch_ok) or remaining:
                rem_summary = (
                    remaining
                    if verify_fetch_ok
                    else [{"error": "verify_fetch_failed"}]
                )
                msg = (
                    f"exit_failed: positions not verified flat "
                    f"closed_orders={closed_count} remaining={rem_summary}"
                )
                logger.critical(
                    "[MIRROR_EXIT] Slave '%s' %s product_ids=%s "
                    "remaining_sizes=%s reason=%s",
                    slave.name,
                    msg,
                    sorted(check_pids),
                    rem_summary,
                    reason,
                )
                slave_trade.status = "exit_failed"
                slave_trade.last_error = msg[:500]
                slave_trade.error_count = (
                    int(slave_trade.error_count or 0) + 1
                )
                if wing_close_failed or any(
                    int(r.get("product_id") or 0) in wing_pids
                    for r in remaining
                    if isinstance(r, dict)
                ):
                    slave_trade.wing_close_failed = True
                slave_trade.last_updated = get_utc_now()
                slave.connection_status = "error"
                slave.last_error = msg[:500]
                db.commit()
                return

            # Verified flat — safe to mark closed
            if not self._close_slave_trade(
                slave,
                slave_trade,
                reason=f"mirror_exit:{reason}",
                allow_virtual=False,
            ):
                return
            slave_trade.wing_close_failed = False
            slave_trade.call_sl_order_id = None
            slave_trade.put_sl_order_id = None
            # Populate exit fills/fees from close orders (no P&L math yet)
            call_exit = exit_by_pid.get(call_pid_hint)
            put_exit = exit_by_pid.get(put_pid_hint)
            if call_exit:
                if float(call_exit.get("fill") or 0) > 0:
                    slave_trade.call_exit_price = float(call_exit["fill"])
                slave_trade.call_exit_fee_usd = float(
                    call_exit.get("fee") or 0.0
                )
            if put_exit:
                if float(put_exit.get("fill") or 0) > 0:
                    slave_trade.put_exit_price = float(put_exit["fill"])
                slave_trade.put_exit_fee_usd = float(
                    put_exit.get("fee") or 0.0
                )
            slave_trade.exit_time = get_utc_now()
            slave_trade.exit_reason = str(reason or "")[:50]
            self._apply_slave_realized_pnl(slave_trade)
            slave_trade.last_error = None
            slave_trade.last_updated = get_utc_now()
            try:
                from backend.engine.structure_ledger import (
                    record_slave_basket_exit,
                )
                from backend.models import Trade as MasterTrade

                master_row = (
                    db.query(MasterTrade)
                    .filter(
                        MasterTrade.id
                        == int(slave_trade.master_trade_id or 0)
                    )
                    .first()
                )
                call_closed_at = self._resolve_basket_exit_closed_at(
                    exit_by_pid,
                    call_pid_hint,
                    exit_batch_ts=exit_batch_ts,
                )
                put_closed_at = self._resolve_basket_exit_closed_at(
                    exit_by_pid,
                    put_pid_hint,
                    exit_batch_ts=exit_batch_ts,
                )
                call_fill_at = (
                    exit_by_pid.get(call_pid_hint, {}).get("fill_at")
                    or call_closed_at
                )
                put_fill_at = (
                    exit_by_pid.get(put_pid_hint, {}).get("fill_at")
                    or put_closed_at
                )
                wing_call_pid = int(
                    getattr(slave_trade, "wing_call_product_id", 0) or 0
                )
                wing_put_pid = int(
                    getattr(slave_trade, "wing_put_product_id", 0) or 0
                )
                # Verified-flat path only reaches here — stamp wing windows
                # when this slave trade carried wings (same resolve as shorts).
                wing_call_closed_at = None
                wing_put_closed_at = None
                wing_call_fill_at = None
                wing_put_fill_at = None
                if wing_call_pid > 0:
                    wing_call_closed_at = self._resolve_basket_exit_closed_at(
                        exit_by_pid,
                        wing_call_pid,
                        exit_batch_ts=exit_batch_ts,
                    )
                    wing_call_fill_at = (
                        exit_by_pid.get(wing_call_pid, {}).get("fill_at")
                        or wing_call_closed_at
                    )
                if wing_put_pid > 0:
                    wing_put_closed_at = self._resolve_basket_exit_closed_at(
                        exit_by_pid,
                        wing_put_pid,
                        exit_batch_ts=exit_batch_ts,
                    )
                    wing_put_fill_at = (
                        exit_by_pid.get(wing_put_pid, {}).get("fill_at")
                        or wing_put_closed_at
                    )
                record_slave_basket_exit(
                    db,
                    slave_trade=slave_trade,
                    slave_account_id=int(slave.id),
                    master_trade=master_row,
                    reason=str(reason or ""),
                    call_closed_at=call_closed_at,
                    put_closed_at=put_closed_at,
                    call_fill_at=call_fill_at,
                    put_fill_at=put_fill_at,
                    wing_call_closed_at=wing_call_closed_at,
                    wing_put_closed_at=wing_put_closed_at,
                    wing_call_fill_at=wing_call_fill_at,
                    wing_put_fill_at=wing_put_fill_at,
                )
            except Exception as ledger_exc:
                logger.error(
                    "structure ledger slave basket exit failed: %s",
                    ledger_exc,
                    exc_info=True,
                )
            db.commit()

            logger.info(
                "[MIRROR_EXIT] Slave '%s' exit complete (verified flat) "
                "trade=%s reason=%s closed_legs=%s realized_pnl=%s",
                slave.name,
                slave_trade.master_trade_id,
                reason,
                closed_count,
                getattr(slave_trade, "realized_pnl", None),
            )

        except Exception as exc:
            logger.error(
                "Slave '%s' exit FAILED: %s",
                slave.name,
                exc,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                pass
            # Do NOT mark closed — position may still be live
            try:
                slave_trade.status = "exit_failed"
                slave_trade.last_error = (
                    f"exit_failed: exception during mirror_exit: {exc}"
                )[:500]
                slave_trade.error_count = (
                    int(slave_trade.error_count or 0) + 1
                )
                slave_trade.last_updated = get_utc_now()
                slave.connection_status = "error"
                slave.last_error = str(exc)[:500]
                db.commit()
                logger.critical(
                    "[MIRROR_EXIT] Slave '%s' marked exit_failed after "
                    "exception (NOT closed): %s",
                    slave.name,
                    exc,
                )
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
        finally:
            await client.close()

    async def fetch_slave_balances(self) -> list[dict[str, Any]]:
        """
        Fetch current balance for all slave accounts.
        Used for dashboard display. Updates slave.balance_usd in DB.
        """
        results: list[dict[str, Any]] = []
        with self.db_factory() as db:
            from backend.database import get_usd_inr_rate

            rate = get_usd_inr_rate(db)
            slaves = db.query(SlaveAccount).order_by(SlaveAccount.id.asc()).all()

            for slave in slaves:
                client = self._get_slave_client(slave)
                try:
                    balance = await client.get_wallet_balance()
                    bal_usd = float(balance.get("balance_usdt", 0.0) or 0.0)

                    slave.balance_usd = bal_usd
                    slave.balance_inr = round(bal_usd * rate, 2)
                    slave.connection_status = "connected"
                    slave.last_connected_at = get_utc_now()
                    slave.last_error = None
                    slave.updated_at = get_utc_now()
                    db.commit()

                    results.append(
                        {
                            "id": slave.id,
                            "name": slave.name,
                            "balance_usd": bal_usd,
                            "balance_inr": slave.balance_inr,
                            "status": "connected",
                        }
                    )
                except Exception as exc:
                    slave.connection_status = "error"
                    slave.last_error = str(exc)[:500]
                    slave.updated_at = get_utc_now()
                    db.commit()
                    results.append(
                        {
                            "id": slave.id,
                            "name": slave.name,
                            "balance_usd": 0.0,
                            "status": "error",
                            "error": str(exc),
                        }
                    )
                finally:
                    await client.close()

        return results


    def _apply_slave_realized_pnl(self, slave_trade: SlaveTrade) -> float | None:
        """
        Persist realized_pnl from actual exit fills + stored fees (billing number).

        short PnL per leg: (entry − exit) × qty × CV, then subtract entry+exit fees.
        Returns the value written, or None if fills are incomplete.
        """
        from backend.core.delta_client import short_leg_realized_pnl

        qty = abs(int(slave_trade.actual_quantity or 0))
        call_entry = float(slave_trade.call_fill_price or 0)
        put_entry = float(slave_trade.put_fill_price or 0)
        call_exit = float(slave_trade.call_exit_price or 0)
        put_exit = float(slave_trade.put_exit_price or 0)
        if qty <= 0 or call_entry <= 0 or put_entry <= 0:
            return None
        if call_exit <= 0 or put_exit <= 0:
            return None
        gross = short_leg_realized_pnl(
            call_entry, call_exit, qty
        ) + short_leg_realized_pnl(put_entry, put_exit, qty)
        fees = (
            float(slave_trade.call_entry_fee_usd or 0)
            + float(slave_trade.put_entry_fee_usd or 0)
            + float(slave_trade.call_exit_fee_usd or 0)
            + float(slave_trade.put_exit_fee_usd or 0)
        )
        realized = float(gross) - max(0.0, fees)
        slave_trade.realized_pnl = round(realized, 6)
        return float(slave_trade.realized_pnl)

    async def _fetch_slave_leg_offer(
        self,
        client: DeltaClient,
        *,
        product_id: int | None,
        symbol: str | None,
        upnl_map: dict[int, dict[str, Any]] | None,
        is_long: bool = False,
    ) -> float:
        """
        Close-ref price for a slave leg.

        Shorts: best ask (UPL@Offer). Longs/wings: best bid via get_long_exit_price
        when not already in the positions upnl map.
        """
        pid = int(product_id or 0)
        if upnl_map and pid > 0:
            row = upnl_map.get(pid) or {}
            try:
                offer = float(row.get("best_offer") or 0)
            except (TypeError, ValueError):
                offer = 0.0
            if offer > 0:
                return offer
        sym = str(symbol or "").strip()
        if sym:
            try:
                if is_long:
                    return float(await client.get_long_exit_price(sym) or 0)
                return float(await client.get_short_exit_price(sym) or 0)
            except Exception:
                try:
                    return float(await client.get_mark_price(sym) or 0)
                except Exception:
                    return 0.0
        return 0.0

    async def _compute_open_slave_mtm(
        self,
        *,
        client: DeltaClient,
        slave_trade: SlaveTrade,
        slip_pct: float,
        settings: Any,
        master_trade_id: int,
    ) -> dict[str, Any] | None:
        """
        Slave basket MTM from OWN fills + live offers (master conventions).

        Shorts: (fill − offer) × qty × CV
        Wings (long): (offer − fill) × qty × CV
        net = basket_net_mtm_snapshot → compute_net_mtm (same helper as master)
        """
        from backend.config import OPTIONS_CONTRACT_VALUE
        from backend.core.fees import (
            basket_net_mtm_snapshot,
            estimate_option_trading_fee,
        )
        from backend.core.spread_utils import estimate_and_log_exit_spread_usd

        call_pid = getattr(slave_trade, "call_product_id", None)
        put_pid = getattr(slave_trade, "put_product_id", None)
        if not call_pid or not put_pid:
            return None

        qty = abs(int(slave_trade.actual_quantity or 0))
        call_fill = float(slave_trade.call_fill_price or 0)
        put_fill = float(slave_trade.put_fill_price or 0)
        if qty <= 0 or call_fill <= 0 or put_fill <= 0:
            return None

        wc_pid_raw = getattr(slave_trade, "wing_call_product_id", None)
        wp_pid_raw = getattr(slave_trade, "wing_put_product_id", None)
        wc_fill = float(getattr(slave_trade, "wing_call_fill_price", None) or 0)
        wp_fill = float(getattr(slave_trade, "wing_put_fill_price", None) or 0)
        # Include each wing only when product id + fill are both present
        has_wing_call = bool(wc_pid_raw) and wc_fill > 0
        has_wing_put = bool(wp_pid_raw) and wp_fill > 0

        pids = [int(call_pid), int(put_pid)]
        if has_wing_call:
            pids.append(int(wc_pid_raw))
        if has_wing_put:
            pids.append(int(wp_pid_raw))
        try:
            upnl_map = await client.get_positions_upnl(pids)
        except Exception:
            upnl_map = {}

        call_offer = await self._fetch_slave_leg_offer(
            client,
            product_id=int(call_pid),
            symbol=getattr(slave_trade, "call_symbol", None),
            upnl_map=upnl_map,
        )
        put_offer = await self._fetch_slave_leg_offer(
            client,
            product_id=int(put_pid),
            symbol=getattr(slave_trade, "put_symbol", None),
            upnl_map=upnl_map,
        )
        if call_offer <= 0 or put_offer <= 0:
            return None

        wing_call_offer = 0.0
        wing_put_offer = 0.0
        if has_wing_call:
            wing_call_offer = await self._fetch_slave_leg_offer(
                client,
                product_id=int(wc_pid_raw),
                symbol=getattr(slave_trade, "wing_call_symbol", None),
                upnl_map=upnl_map,
                is_long=True,
            )
            if wing_call_offer <= 0:
                return None
        if has_wing_put:
            wing_put_offer = await self._fetch_slave_leg_offer(
                client,
                product_id=int(wp_pid_raw),
                symbol=getattr(slave_trade, "wing_put_symbol", None),
                upnl_map=upnl_map,
                is_long=True,
            )
            if wing_put_offer <= 0:
                return None

        cv = float(OPTIONS_CONTRACT_VALUE)
        # Shorts: credit when offer drops; wings (long): credit when mark rises
        short_call_gross = (call_fill - call_offer) * qty * cv
        short_put_gross = (put_fill - put_offer) * qty * cv
        wing_call_gross = 0.0
        wing_put_gross = 0.0
        if has_wing_call:
            wing_call_gross = (wing_call_offer - wc_fill) * qty * cv
        if has_wing_put:
            wing_put_gross = (wing_put_offer - wp_fill) * qty * cv
        gross_mtm = float(
            short_call_gross
            + short_put_gross
            + wing_call_gross
            + wing_put_gross
        )
        leg_count = 2 + (1 if has_wing_call else 0) + (1 if has_wing_put else 0)

        fees_paid = max(
            0.0,
            float(slave_trade.call_entry_fee_usd or 0)
            + float(slave_trade.put_entry_fee_usd or 0),
        )

        btc = 0.0
        try:
            btc = float(await client.get_btc_index_price() or 0)
        except Exception:
            btc = 0.0

        # No wing fee columns on SlaveTrade — estimate entry fees from fills
        if btc > 0:
            if has_wing_call:
                fees_paid += estimate_option_trading_fee(
                    option_price=wc_fill,
                    quantity_lots=qty,
                    btc_index_price=btc,
                )
            if has_wing_put:
                fees_paid += estimate_option_trading_fee(
                    option_price=wp_fill,
                    quantity_lots=qty,
                    btc_index_price=btc,
                )

        est_exit = 0.0
        if btc > 0:
            est_exit += estimate_option_trading_fee(
                option_price=call_offer,
                quantity_lots=qty,
                btc_index_price=btc,
            )
            est_exit += estimate_option_trading_fee(
                option_price=put_offer,
                quantity_lots=qty,
                btc_index_price=btc,
            )
            if has_wing_call:
                est_exit += estimate_option_trading_fee(
                    option_price=wing_call_offer,
                    quantity_lots=qty,
                    btc_index_price=btc,
                )
            if has_wing_put:
                est_exit += estimate_option_trading_fee(
                    option_price=wing_put_offer,
                    quantity_lots=qty,
                    btc_index_price=btc,
                )

        expected_exit_spread = 0.0
        spread_legs: list[tuple[str, float]] = [
            (str(getattr(slave_trade, "call_symbol", "") or ""), call_offer),
            (str(getattr(slave_trade, "put_symbol", "") or ""), put_offer),
        ]
        if has_wing_call:
            spread_legs.append(
                (
                    str(getattr(slave_trade, "wing_call_symbol", "") or ""),
                    wing_call_offer,
                )
            )
        if has_wing_put:
            spread_legs.append(
                (
                    str(getattr(slave_trade, "wing_put_symbol", "") or ""),
                    wing_put_offer,
                )
            )
        for sym, offer in spread_legs:
            if offer > 0 and sym:
                expected_exit_spread += await estimate_and_log_exit_spread_usd(
                    symbol=sym,
                    offer_price=offer,
                    quantity=qty,
                    settings=settings,
                    kind="basket",
                    client=client,
                    log_id=int(master_trade_id),
                )

        mtm_snap = basket_net_mtm_snapshot(
            gross_mtm=gross_mtm,
            fees_paid=fees_paid,
            est_exit_fees=est_exit,
            slippage_pct=float(slip_pct),
            expected_exit_spread_usd=expected_exit_spread,
        )
        return {
            "gross_mtm": round(gross_mtm, 4),
            "fees_paid": round(fees_paid, 6),
            "est_exit_fees": round(float(est_exit), 6),
            "expected_exit_spread_usd": round(float(expected_exit_spread), 6),
            "net_mtm": float(mtm_snap["net_mtm"]),
            "call_offer": call_offer,
            "put_offer": put_offer,
            "short_call_gross": round(float(short_call_gross), 4),
            "short_put_gross": round(float(short_put_gross), 4),
            "wing_call_gross": round(float(wing_call_gross), 4),
            "wing_put_gross": round(float(wing_put_gross), 4),
            "leg_count": int(leg_count),
            "stale_seconds": float(mtm_snap.get("stale_seconds") or 0),
            "computed_at_iso": mtm_snap.get("computed_at_iso"),
        }

    async def update_all_slave_mtm(
        self,
        master_trade_id: int,
        master_net_mtm: float | None = None,
    ) -> None:
        """
        Compute each open SlaveTrade's net_mtm from that slave's own fills + offers.

        Never copies the master MTM except as an explicit fallback for pre-schema
        rows missing product ids (mtm_source='copied').
        """
        from backend.database import get_or_create_auto_settings

        mid = int(master_trade_id)
        master_net = (
            float(master_net_mtm)
            if master_net_mtm is not None
            else None
        )
        if master_net is None:
            try:
                from backend.engine.bot_engine import bot_engine as _be

                st = _be.position_tracker.get(mid)
                if st is not None:
                    master_net = float(getattr(st, "last_net_mtm", 0) or 0)
            except Exception:
                master_net = 0.0
        if master_net is None:
            master_net = 0.0

        with self.db_factory() as db:
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == mid,
                    SlaveTrade.status == "active",
                )
                .all()
            )
            if not slave_trades:
                return

            settings = get_or_create_auto_settings(db)
            master_row = db.query(Trade).filter(Trade.id == mid).first()
            slip_pct = float(
                getattr(master_row, "slippage_pct", None) or 2.0
            ) if master_row is not None else 2.0

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if slave is None or not slave.is_active:
                    continue

                call_pid = getattr(slave_trade, "call_product_id", None)
                put_pid = getattr(slave_trade, "put_product_id", None)
                has_pids = bool(call_pid) and bool(put_pid)

                if not has_pids:
                    # Pre-cf60d78 row — fall back to scaled master (legacy only)
                    mult = float(slave.qty_multiplier or 1.0)
                    copied = round(float(master_net) * mult, 4)
                    slave_trade.last_mtm = copied
                    slave_trade.net_mtm = copied
                    slave_trade.mtm_source = "copied"
                    slave_trade.last_updated = get_utc_now()
                    db.commit()
                    st_id = int(slave_trade.id)
                    if st_id not in self._slave_mtm_fallback_logged:
                        self._slave_mtm_fallback_logged.add(st_id)
                        logger.warning(
                            "[SLAVE_MTM_FALLBACK] slave_trade=%s | "
                            "reason=missing_product_ids",
                            st_id,
                        )
                        log_and_buffer(
                            "SLAVE_MTM_FALLBACK",
                            mid,
                            {
                                "slave_trade": st_id,
                                "slave": int(slave.id),
                                "reason": "missing_product_ids",
                            },
                        )
                    continue

                client = self._get_slave_client(slave)
                try:
                    computed = await self._compute_open_slave_mtm(
                        client=client,
                        slave_trade=slave_trade,
                        slip_pct=slip_pct,
                        settings=settings,
                        master_trade_id=mid,
                    )
                    if computed is None:
                        logger.warning(
                            "Slave '%s' MTM compute failed (no offers) "
                            "slave_trade=%s — leaving prior net_mtm",
                            slave.name,
                            slave_trade.id,
                        )
                        continue

                    gross = float(computed["gross_mtm"])
                    net = float(computed["net_mtm"])
                    fees = float(computed["fees_paid"])
                    spread = float(computed["expected_exit_spread_usd"])
                    slave_trade.last_mtm = gross
                    slave_trade.net_mtm = net
                    slave_trade.mtm_source = "computed"
                    slave_trade.last_updated = get_utc_now()
                    db.commit()

                    # Scale master by slave/master lot ratio (not bare multiplier)
                    slave_qty = max(1, int(slave_trade.actual_quantity or 1))
                    master_qty = 1
                    try:
                        from backend.models import Leg

                        m_leg = (
                            db.query(Leg)
                            .filter(
                                Leg.trade_id == mid,
                                Leg.leg_type == "call",
                                Leg.status == "active",
                            )
                            .first()
                        )
                        if m_leg is not None:
                            master_qty = max(1, int(m_leg.quantity or 1))
                        else:
                            mult = float(slave.qty_multiplier or 1.0)
                            if mult > 0:
                                master_qty = max(
                                    1, int(round(slave_qty / mult))
                                )
                    except Exception:
                        mult = float(slave.qty_multiplier or 1.0)
                        if mult > 0:
                            master_qty = max(1, int(round(slave_qty / mult)))

                    master_net_scaled = float(master_net) * (
                        float(slave_qty) / float(master_qty)
                    )
                    if abs(master_net_scaled) > 1e-9:
                        divergence_pct = (
                            (net - master_net_scaled) / abs(master_net_scaled)
                        ) * 100.0
                    else:
                        divergence_pct = 0.0 if abs(net) < 1e-9 else 100.0
                    abs_diff = abs(float(net) - float(master_net_scaled))

                    mtm_details = {
                        "slave": int(slave.id),
                        "slave_trade": int(slave_trade.id),
                        "short_call_gross": float(
                            computed.get("short_call_gross") or 0
                        ),
                        "short_put_gross": float(
                            computed.get("short_put_gross") or 0
                        ),
                        "wing_call_gross": float(
                            computed.get("wing_call_gross") or 0
                        ),
                        "wing_put_gross": float(
                            computed.get("wing_put_gross") or 0
                        ),
                        "leg_count": int(computed.get("leg_count") or 2),
                        "gross": round(gross, 4),
                        "fees": round(fees, 6),
                        "spread": round(spread, 6),
                        "net_mtm": round(net, 4),
                        "master_net_mtm": round(float(master_net), 4),
                        "master_net_scaled": round(float(master_net_scaled), 4),
                        "divergence_pct": round(divergence_pct, 4),
                        "abs_diff": round(abs_diff, 6),
                    }
                    log_and_buffer("SLAVE_MTM", mid, mtm_details)
                    mtm_line = (
                        "[SLAVE_MTM] slave=%s | slave_trade=%s | "
                        "short_call_gross=%s | short_put_gross=%s | "
                        "wing_call_gross=%s | wing_put_gross=%s | "
                        "leg_count=%s | gross=%s | fees=%s | spread=%s | "
                        "net_mtm=%s | master_net_mtm=%s | "
                        "master_net_scaled=%s | divergence_pct=%s"
                        % (
                            slave.id,
                            slave_trade.id,
                            mtm_details["short_call_gross"],
                            mtm_details["short_put_gross"],
                            mtm_details["wing_call_gross"],
                            mtm_details["wing_put_gross"],
                            mtm_details["leg_count"],
                            mtm_details["gross"],
                            mtm_details["fees"],
                            mtm_details["spread"],
                            mtm_details["net_mtm"],
                            mtm_details["master_net_mtm"],
                            mtm_details["master_net_scaled"],
                            mtm_details["divergence_pct"],
                        )
                    )
                    # Warn only on real $ gap AND large % — avoid near-zero master noise
                    if abs_diff > 0.05 and abs(divergence_pct) > 50.0:
                        logger.warning(mtm_line)
                    else:
                        logger.info(mtm_line)
                except Exception as exc:
                    logger.warning(
                        "Slave '%s' MTM compute failed: %s",
                        slave.name,
                        exc,
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        await client.close()
                    except Exception:
                        pass

    # After this many consecutive close failures, only retry every Nth sweep
    _SWEEP_BACKOFF_AFTER = 3
    _SWEEP_BACKOFF_EVERY = 6  # generations between retries once backed off
    # Bounded unwind retries for partial_entry_open / naked repair before escalate
    _PARTIAL_ENTRY_UNWIND_MAX_ATTEMPTS = 5

    # Orphan-basket sweep — rows that may still hold exchange risk
    _ORPHAN_BASKET_OPEN_STATUSES = (
        "active",
        "partial",
        "partial_entry_open",
        "partial_adjustment",
        "adjust_close_failed",
        "exit_failed",
        "error",
    )
    # Active-master integrity problem set (entry/adjust leftovers)
    _INTEGRITY_PROBLEM_STATUSES = (
        "error",
        "partial",
        "partial_entry_open",
        "partial_adjustment",
        "adjust_close_failed",
        "exit_failed",
        "blocked_foreign_position",
        "skipped_low_capital",
    )

    async def _close_bot_owned_shorts(
        self,
        *,
        client: DeltaClient,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        db: Any,
        live_positions: list[dict[str, Any]] | None = None,
        path: str = "",
    ) -> tuple[bool, str]:
        """
        Close bot-owned short option products only (never foreign manuals).

        Returns (all_flat, error_or_empty). Caller must hold per-slave lock.
        """
        bot_owned = self._bot_owned_product_ids(db, int(slave.id))
        if live_positions is None:
            try:
                live_positions = await client.get_option_positions()
            except Exception as pos_exc:
                return False, f"get_option_positions: {pos_exc}"

        shorts: list[tuple[int, float]] = []
        for pos in live_positions or []:
            try:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if pid > 0 and size < -1e-9 and pid in bot_owned:
                shorts.append((pid, size))

        if not shorts:
            return True, ""

        errors: list[str] = []
        for pid, size in shorts:
            ok, _ord, err = await self._close_with_reduce_only(
                client=client,
                slave=slave,
                product_id=pid,
                signed_size=float(size),
                master_trade_id=int(slave_trade.master_trade_id or 0),
                path=path or "close_bot_owned_shorts",
            )
            if not ok:
                errors.append(f"product={pid}:{err}")

        if errors:
            return False, "; ".join(errors)[:500]

        # Final verify
        try:
            post = await client.get_option_positions()
        except Exception as pos_exc:
            return False, f"post_close_positions: {pos_exc}"
        for pos in post or []:
            try:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if pid in bot_owned and size < -1e-9:
                return False, f"still_short product={pid} size={size}"
        return True, ""

    async def _repair_partial_or_naked_slave(
        self,
        *,
        db: Any,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        path: str,
    ) -> str:
        """
        Re-attempt unwind of remaining bot-owned shorts for
        partial_entry_open / partial_adjustment. Escalates after
        ``_PARTIAL_ENTRY_UNWIND_MAX_ATTEMPTS`` failures.

        Returns outcome: repaired | retry_pending | escalated | unreachable
        Caller MUST hold the per-slave lock.
        """
        client = self._get_slave_client(slave)
        try:
            try:
                live = await client.get_option_positions()
            except Exception as pos_exc:
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                slave_trade.last_error = (
                    f"repair_unreachable: {pos_exc}"
                )[:500]
                slave_trade.last_updated = get_utc_now()
                slave.connection_status = "error"
                slave.last_error = str(pos_exc)[:500]
                db.commit()
                logger.critical(
                    "[SLAVE_REPAIR] slave_trade=%s unreachable: %s",
                    slave_trade.id,
                    pos_exc,
                )
                return "unreachable"

            ok, err = await self._close_bot_owned_shorts(
                client=client,
                slave=slave,
                slave_trade=slave_trade,
                db=db,
                live_positions=live,
                path=path,
            )
            if ok:
                if self._close_slave_trade(
                    slave,
                    slave_trade,
                    reason=f"repaired:{path}",
                    allow_virtual=False,
                ):
                    slave_trade.last_error = (
                        f"Repaired via {path}: bot-owned shorts closed"
                    )[:500]
                    slave_trade.last_updated = get_utc_now()
                    db.commit()
                logger.critical(
                    "[SLAVE_REPAIR] slave_trade=%s slave='%s' REPAIRED "
                    "path=%s — naked/partial shorts closed",
                    slave_trade.id,
                    slave.name,
                    path,
                )
                return "repaired"

            slave_trade.error_count = int(slave_trade.error_count or 0) + 1
            attempts = int(slave_trade.error_count or 0)
            slave_trade.last_error = (
                f"repair_failed ({attempts}): {err}"
            )[:500]
            slave_trade.last_updated = get_utc_now()
            slave.connection_status = "error"
            slave.last_error = (err or "")[:500]

            if attempts >= self._PARTIAL_ENTRY_UNWIND_MAX_ATTEMPTS:
                slave_trade.status = "exit_failed"
                db.commit()
                logger.critical(
                    "[SLAVE_REPAIR] slave_trade=%s slave='%s' ESCALATED "
                    "status=exit_failed after %s unwind attempts path=%s "
                    "err=%s — needs human review",
                    slave_trade.id,
                    slave.name,
                    attempts,
                    path,
                    (err or "")[:200],
                )
                return "escalated"

            # Keep status so sweep retries (partial_entry_open / partial_adjustment)
            db.commit()
            logger.critical(
                "[SLAVE_REPAIR] slave_trade=%s slave='%s' retry_pending "
                "attempt=%s/%s path=%s err=%s",
                slave_trade.id,
                slave.name,
                attempts,
                self._PARTIAL_ENTRY_UNWIND_MAX_ATTEMPTS,
                path,
                (err or "")[:200],
            )
            return "retry_pending"
        finally:
            await client.close()

    async def sweep_orphan_slave_baskets(self) -> dict[str, int]:
        """
        Close slave baskets whose structure hedge is missing or not alive.

        pending_close counts as alive (same as master orphan sweep).
        Verifies exchange flat before marking closed. Runs every monitor cycle.
        """
        from backend.models import HedgePosition

        orphans_found = 0
        orphans_closed = 0
        slaves_active = 0
        with_hedge = 0
        without_hedge = 0
        baskets_open = 0

        with self.db_factory() as db:
            active_slaves = get_active_slave_accounts(db)
            slaves_active = len(active_slaves)

            for slave in active_slaves:
                alive = (
                    db.query(SlaveHedgePosition)
                    .filter(
                        SlaveHedgePosition.slave_account_id == int(slave.id),
                        SlaveHedgePosition.status.in_(
                            ("active", "pending_close")
                        ),
                    )
                    .count()
                )
                if alive > 0:
                    with_hedge += 1
                else:
                    without_hedge += 1

            open_statuses = self._ORPHAN_BASKET_OPEN_STATUSES
            open_baskets = (
                db.query(SlaveTrade)
                .filter(SlaveTrade.status.in_(open_statuses))
                .order_by(SlaveTrade.id.asc())
                .all()
            )
            baskets_open = len(open_baskets)
            now = get_utc_now()

            for st in open_baskets:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == int(st.slave_account_id))
                    .first()
                )
                if slave is None:
                    continue

                master_hedge_id = self._resolve_master_hedge_id_for_trade(
                    db, int(st.master_trade_id)
                )
                slave_hedge: SlaveHedgePosition | None = None
                hedge_status = "missing"
                if master_hedge_id is not None:
                    slave_hedge = (
                        db.query(SlaveHedgePosition)
                        .filter(
                            SlaveHedgePosition.slave_account_id
                            == int(slave.id),
                            SlaveHedgePosition.master_hedge_id
                            == int(master_hedge_id),
                        )
                        .order_by(SlaveHedgePosition.id.desc())
                        .first()
                    )
                    if slave_hedge is not None:
                        hedge_status = str(slave_hedge.status or "missing")
                    else:
                        mh = (
                            db.query(HedgePosition)
                            .filter(HedgePosition.id == int(master_hedge_id))
                            .first()
                        )
                        if mh is not None:
                            hedge_status = (
                                f"slave_missing(master={mh.status})"
                            )

                if slave_hedge is not None and self._slave_hedge_status_is_alive(
                    hedge_status
                ):
                    continue

                if master_hedge_id is None and slave_hedge is None:
                    master_trade = (
                        db.query(Trade)
                        .filter(Trade.id == int(st.master_trade_id))
                        .first()
                    )
                    if (
                        master_trade is None
                        or getattr(master_trade, "hedge_position_id", None)
                        is None
                    ):
                        continue

                orphans_found += 1
                exit_time = (
                    getattr(slave_hedge, "exit_time", None)
                    if slave_hedge is not None
                    else None
                )
                if exit_time is None and master_hedge_id is not None:
                    mh = (
                        db.query(HedgePosition)
                        .filter(HedgePosition.id == int(master_hedge_id))
                        .first()
                    )
                    if mh is not None:
                        exit_time = getattr(mh, "exit_time", None)
                orphan_sec = 0
                if exit_time is not None:
                    from backend.core.time_utils import duration_seconds_since

                    secs, _unreliable = duration_seconds_since(
                        exit_time,
                        table=(
                            "slave_hedge_positions"
                            if slave_hedge is not None
                            else "hedge_positions"
                        ),
                        row_id=(
                            getattr(slave_hedge, "id", None)
                            if slave_hedge is not None
                            else master_hedge_id
                        ),
                        end=now,
                        skip_if_legacy=False,  # trading — log only
                    )
                    orphan_sec = int(secs or 0)

                logger.critical(
                    "[SLAVE_ORPHAN_BASKET] slave=%s | slave_trade=%s | "
                    "master_trade=%s | hedge_status=%s | "
                    "orphan_duration_sec=%s",
                    slave.id,
                    st.id,
                    st.master_trade_id,
                    hedge_status,
                    orphan_sec,
                )
                log_and_buffer(
                    "SLAVE_ORPHAN_BASKET",
                    int(st.master_trade_id or 0),
                    {
                        "slave": int(slave.id),
                        "slave_trade": int(st.id),
                        "master_trade": int(st.master_trade_id),
                        "hedge_status": hedge_status,
                        "orphan_duration_sec": orphan_sec,
                        "master_hedge": master_hedge_id,
                    },
                )

                call_pid, put_pid = self._master_trade_call_put_pids(
                    db, int(st.master_trade_id)
                )
                try:
                    async with self._slave_op_lock(
                        int(slave.id), "sweep_orphan_slave_baskets"
                    ) as acquired:
                        if not acquired:
                            continue
                        await self._mirror_exit_to_slave(
                            slave=slave,
                            slave_trade=st,
                            call_product_id=call_pid,
                            put_product_id=put_pid,
                            reason=ExitReason.ORPHAN_NO_HEDGE.value,
                            db=db,
                            hedge_product_id=None,
                        )
                except Exception as close_exc:
                    logger.critical(
                        "[SLAVE_ORPHAN_BASKET] close raised slave_trade=%s: %s",
                        st.id,
                        close_exc,
                        exc_info=True,
                    )
                    continue

                db.expire_all()
                refreshed = (
                    db.query(SlaveTrade)
                    .filter(SlaveTrade.id == int(st.id))
                    .first()
                )
                if (
                    refreshed is not None
                    and str(refreshed.status or "").lower() == "closed"
                ):
                    orphans_closed += 1
                else:
                    logger.critical(
                        "[SLAVE_ORPHAN_BASKET] slave_trade=%s still not "
                        "closed after exit (status=%s) — will retry",
                        st.id,
                        getattr(refreshed, "status", None),
                    )

        log_and_buffer(
            "SLAVE_HEDGE_HEALTH",
            0,
            {
                "slaves_active": slaves_active,
                "with_hedge": with_hedge,
                "without_hedge": without_hedge,
                "baskets_open": baskets_open,
                "orphans_found": orphans_found,
                "orphans_closed": orphans_closed,
            },
        )
        logger.info(
            "[SLAVE_HEDGE_HEALTH] slaves_active=%s | with_hedge=%s | "
            "without_hedge=%s | baskets_open=%s | orphans_found=%s",
            slaves_active,
            with_hedge,
            without_hedge,
            baskets_open,
            orphans_found,
        )
        return {
            "slaves_active": slaves_active,
            "with_hedge": with_hedge,
            "without_hedge": without_hedge,
            "baskets_open": baskets_open,
            "orphans_found": orphans_found,
            "orphans_closed": orphans_closed,
        }

    async def sweep_open_slave_trades(self) -> dict[str, int]:
        """
        DB-driven integrity sweep — independent of position_tracker.

        Queries every SlaveTrade with status != 'closed', groups by master,
        and recovers orphans under CLOSED masters via live Delta closes.
        ACTIVE masters keep the existing per-trade integrity checks.
        """
        self._sweep_generation = int(
            getattr(self, "_sweep_generation", 0) or 0
        ) + 1
        gen = self._sweep_generation

        rows_scanned = 0
        closed_ok = 0
        close_failed = 0
        unreachable = 0
        skipped_backoff = 0

        with self.db_factory() as db:
            open_rows = (
                db.query(SlaveTrade)
                .filter(SlaveTrade.status != "closed")
                .all()
            )
            rows_scanned = len(open_rows)

            by_master: dict[int, list[SlaveTrade]] = {}
            for st in open_rows:
                mid = int(st.master_trade_id)
                by_master.setdefault(mid, []).append(st)

            for master_trade_id, slave_trades in by_master.items():
                master = (
                    db.query(Trade)
                    .filter(Trade.id == int(master_trade_id))
                    .first()
                )
                master_active = bool(
                    master is not None
                    and str(getattr(master, "status", "")).lower()
                    == TradeStatus.ACTIVE.value
                )

                # Skip demo masters for active-path checks
                if master_active and bool(
                    getattr(master, "is_demo", False)
                ):
                    continue

                if master_active:
                    # Existing ACTIVE behaviour (naked-leg + entry retry)
                    await self.check_slave_integrity(int(master_trade_id))
                    continue

                # Master CLOSED (or missing) — recover live positions
                for st in slave_trades:
                    slave = (
                        db.query(SlaveAccount)
                        .filter(SlaveAccount.id == st.slave_account_id)
                        .first()
                    )
                    if slave is None:
                        close_failed += 1
                        continue

                    if is_virtual_slave_trade(slave, st):
                        logger.warning(
                            "[SLAVE_SWEEP] skip virtual slave_trade=%s "
                            "master=#%s",
                            st.id,
                            master_trade_id,
                        )
                        continue

                    err_count = int(st.error_count or 0)
                    if err_count >= self._SWEEP_BACKOFF_AFTER:
                        # Back off: only attempt every Nth generation
                        if gen % self._SWEEP_BACKOFF_EVERY != 0:
                            skipped_backoff += 1
                            if (
                                err_count == self._SWEEP_BACKOFF_AFTER
                                or err_count % 10 == 0
                            ):
                                logger.critical(
                                    "[SLAVE_SWEEP] slave_trade=%s slave='%s' "
                                    "backed off error_count=%s — will retry "
                                    "every %s sweeps",
                                    st.id,
                                    slave.name,
                                    err_count,
                                    self._SWEEP_BACKOFF_EVERY,
                                )
                            continue

                    outcome = None
                    async with self._slave_op_lock(
                        int(slave.id), "sweep_open_slave_trades"
                    ) as acquired:
                        if not acquired:
                            continue
                        outcome = await self._recover_slave_under_closed_master(
                            slave=slave,
                            slave_trade=st,
                            master_trade_id=int(master_trade_id),
                            db=db,
                        )
                    if outcome == "closed_ok":
                        closed_ok += 1
                    elif outcome == "unreachable":
                        unreachable += 1
                    elif outcome == "close_failed":
                        close_failed += 1

        logger.info(
            "[SLAVE_SWEEP] rows_scanned=%s closed_ok=%s close_failed=%s "
            "unreachable=%s skipped_backoff=%s generation=%s",
            rows_scanned,
            closed_ok,
            close_failed,
            unreachable,
            skipped_backoff,
            gen,
        )
        log_and_buffer(
            "SLAVE_SWEEP",
            0,
            {
                "rows_scanned": rows_scanned,
                "closed_ok": closed_ok,
                "close_failed": close_failed,
                "unreachable": unreachable,
                "skipped_backoff": skipped_backoff,
                "generation": gen,
            },
        )
        return {
            "rows_scanned": rows_scanned,
            "closed_ok": closed_ok,
            "close_failed": close_failed,
            "unreachable": unreachable,
            "skipped_backoff": skipped_backoff,
        }

    async def _recover_slave_under_closed_master(
        self,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        master_trade_id: int,
        db: Any,
    ) -> str:
        """
        Fetch live positions; if flat mark closed; else reduce_only close,
        verify, mark closed only on success. Returns outcome label.

        Caller MUST hold the per-slave lock.
        """
        from backend.models import Leg

        client = self._get_slave_client(slave)
        try:
            try:
                live_positions = await client.get_option_positions()
            except Exception as pos_exc:
                slave_trade.status = "exit_failed"
                slave_trade.last_error = (
                    f"sweep_unreachable: get_option_positions: {pos_exc}"
                )[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                slave_trade.last_updated = get_utc_now()
                slave.connection_status = "error"
                slave.last_error = str(pos_exc)[:500]
                db.commit()
                logger.critical(
                    "[SLAVE_SWEEP] slave='%s' slave_trade=%s UNREACHABLE: %s",
                    slave.name,
                    slave_trade.id,
                    pos_exc,
                )
                return "unreachable"

            # Prefer master leg product_ids; never close whole book or foreign shorts
            master_pids: set[int] = set()
            for lg in (
                db.query(Leg)
                .filter(
                    Leg.trade_id == int(master_trade_id),
                    Leg.is_bot_managed.is_(True),
                )
                .all()
            ):
                try:
                    pid = int(getattr(lg, "product_id", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if pid > 0:
                    master_pids.add(pid)

            bot_owned = self._bot_owned_product_ids(db, int(slave.id))
            protected_hedge_pids = self._structure_hedge_pids_for_slave(
                db, int(slave.id)
            )

            if not master_pids:
                log_and_buffer(
                    "SLAVE_SWEEP",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "slave_trade": int(slave_trade.id),
                        "reason": "empty_master_pids_skip_close",
                        "note": (
                            "no bot-managed master legs — refusing to "
                            "close any live option positions"
                        ),
                    },
                )
                slave_trade.last_error = (
                    "sweep_skipped: empty_master_pids — no close attempted"
                )[:500]
                slave_trade.last_updated = get_utc_now()
                db.commit()
                logger.critical(
                    "[SLAVE_SWEEP] slave='%s' slave_trade=%s master=#%s "
                    "empty master_pids — skip close (will not touch book)",
                    slave.name,
                    slave_trade.id,
                    master_trade_id,
                )
                return "close_failed"

            live_nonzero: list[dict[str, Any]] = []
            for pos in live_positions or []:
                try:
                    pid = int(pos.get("product_id") or 0)
                    size = float(pos.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or abs(size) <= 0:
                    continue
                if pid not in bot_owned:
                    log_and_buffer(
                        "SLAVE_FOREIGN",
                        int(master_trade_id),
                        {
                            "slave": int(slave.id),
                            "product_id": pid,
                            "size": size,
                            "symbol": str(pos.get("product_symbol") or ""),
                            "path": "_recover_slave_under_closed_master",
                            "note": "left untouched (not bot-owned)",
                        },
                    )
                    continue
                # Structure hedge longs must survive basket/sweep closes
                if pid in protected_hedge_pids and size > 0:
                    log_and_buffer(
                        "SLAVE_HEDGE_PROTECTED",
                        int(master_trade_id),
                        {
                            "slave": int(slave.id),
                            "attempted_by": "SLAVE_SWEEP",
                            "product_id": pid,
                            "path": "_recover_slave_under_closed_master",
                        },
                    )
                    continue
                live_nonzero.append(pos)

            if not live_nonzero:
                # Already flat on exchange — still end attribution windows
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                exit_batch_ts = get_utc_now()
                call_pid_hint = int(
                    getattr(slave_trade, "call_product_id", 0) or 0
                )
                put_pid_hint = int(
                    getattr(slave_trade, "put_product_id", 0) or 0
                )
                if call_pid_hint <= 0 or put_pid_hint <= 0:
                    call_pid_hint, put_pid_hint = (
                        self._master_trade_call_put_pids(
                            db, int(master_trade_id)
                        )
                    )
                for leg_name, pid in (
                    ("call", call_pid_hint),
                    ("put", put_pid_hint),
                ):
                    if pid <= 0:
                        continue
                    ledger = self._write_slave_product_leg_close_ledger(
                        db,
                        slave=slave,
                        slave_trade=slave_trade,
                        leg_type=leg_name,
                        product_id=pid,
                        closed_at=exit_batch_ts,
                        fill_at=exit_batch_ts,
                        reason="SWEEP_CLOSE",
                    )
                    log_and_buffer(
                        "SLAVE_LEG_CLOSE",
                        int(master_trade_id),
                        {
                            "slave": int(slave.id),
                            "product_id": pid,
                            "live_size": 0,
                            "ledger": ledger,
                            "outcome": "ok",
                            "path": "sweep_already_flat",
                        },
                    )
                if self._close_slave_trade(
                    slave,
                    slave_trade,
                    reason="sweep_verified_flat",
                    allow_virtual=False,
                ):
                    slave_trade.last_error = None
                    slave_trade.last_updated = get_utc_now()
                    db.commit()
                    log_and_buffer(
                        "SLAVE_SWEEP",
                        int(master_trade_id),
                        {
                            "slave": int(slave.id),
                            "slave_trade": int(slave_trade.id),
                            "outcome": "closed_ok",
                            "reason": "sweep_verified_flat",
                        },
                    )
                    return "closed_ok"
                return "close_failed"

            # Attempt real closes via shared reduce_only helper
            # Captured BEFORE any order — this is an attribution window bound.
            # See e3e6b7d: a post-fill timestamp silently drops the fill.
            exit_batch_ts = get_utc_now()
            closed_pids: dict[int, Any] = {}
            for pos in live_nonzero:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
                if pid <= 0 or abs(size) <= 1e-9:
                    continue
                # Captured BEFORE any order — this is an attribution window bound.
                # See e3e6b7d: a post-fill timestamp silently drops the fill.
                closed_at = get_utc_now()
                ok, _order, err = await self._close_with_reduce_only(
                    client=client,
                    slave=slave,
                    product_id=pid,
                    signed_size=float(size),
                    master_trade_id=int(master_trade_id),
                    path="_recover_slave_under_closed_master",
                )
                fill_at = get_utc_now()
                if not ok:
                    log_and_buffer(
                        "SLAVE_LEG_CLOSE",
                        int(master_trade_id),
                        {
                            "slave": int(slave.id),
                            "product_id": pid,
                            "live_size": size,
                            "ledger": "missing",
                            "outcome": "failed",
                            "path": "sweep",
                            "reason": err[:200],
                        },
                    )
                    continue
                closed_pids[pid] = {
                    "closed_at": closed_at,
                    "fill_at": fill_at,
                    "live_size": size,
                }

            await asyncio.sleep(2)
            try:
                verify = await client.get_option_positions()
            except Exception as verify_exc:
                slave_trade.status = "exit_failed"
                slave_trade.last_error = (
                    f"sweep_verify_unreachable: {verify_exc}"
                )[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                slave_trade.last_updated = get_utc_now()
                db.commit()
                log_and_buffer(
                    "SLAVE_SWEEP",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "outcome": "unreachable",
                        "reason": str(verify_exc)[:200],
                    },
                )
                return "unreachable"

            remaining: list[dict[str, Any]] = []
            for pos in verify or []:
                try:
                    pid = int(pos.get("product_id") or 0)
                    size = float(pos.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or abs(size) <= 0:
                    continue
                # Foreign / customer positions do not block sweep success
                if pid not in bot_owned:
                    continue
                # Structure hedge longs are expected to remain
                if pid in protected_hedge_pids and size > 0:
                    continue
                remaining.append({"product_id": pid, "size": size})

            if remaining:
                slave_trade.status = "exit_failed"
                slave_trade.last_error = (
                    f"sweep_close_failed: remaining={remaining}"
                )[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                slave_trade.last_updated = get_utc_now()
                slave.connection_status = "error"
                slave.last_error = slave_trade.last_error
                db.commit()
                log_and_buffer(
                    "SLAVE_SWEEP",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "slave_trade": int(slave_trade.id),
                        "outcome": "close_failed",
                        "remaining": remaining,
                    },
                )
                return "close_failed"

            # Flat — write ledger for each closed product + any hint still open
            call_pid_hint = int(
                getattr(slave_trade, "call_product_id", 0) or 0
            )
            put_pid_hint = int(
                getattr(slave_trade, "put_product_id", 0) or 0
            )
            if call_pid_hint <= 0 or put_pid_hint <= 0:
                mc, mp = self._master_trade_call_put_pids(
                    db, int(master_trade_id)
                )
                if call_pid_hint <= 0:
                    call_pid_hint = mc
                if put_pid_hint <= 0:
                    put_pid_hint = mp

            for leg_name, pid in (
                ("call", call_pid_hint),
                ("put", put_pid_hint),
            ):
                if pid <= 0:
                    continue
                meta = closed_pids.get(pid) or {}
                closed_at = meta.get("closed_at") or exit_batch_ts
                fill_at = meta.get("fill_at") or closed_at
                live_sz = meta.get("live_size", 0)
                ledger = self._write_slave_product_leg_close_ledger(
                    db,
                    slave=slave,
                    slave_trade=slave_trade,
                    leg_type=leg_name,
                    product_id=pid,
                    closed_at=closed_at,
                    fill_at=fill_at,
                    reason="SWEEP_CLOSE",
                )
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "product_id": pid,
                        "live_size": live_sz,
                        "ledger": ledger,
                        "outcome": "ok",
                        "path": "sweep",
                    },
                )
            # Also close any other pids we closed that aren't call/put hints
            for pid, meta in closed_pids.items():
                if pid in (call_pid_hint, put_pid_hint):
                    continue
                # Infer leg from size sign was short → basket; use call role fallback
                # Prefer matching slave_trade product fields
                leg_guess = "call"
                if int(getattr(slave_trade, "put_product_id", 0) or 0) == pid:
                    leg_guess = "put"
                elif int(getattr(slave_trade, "call_product_id", 0) or 0) == pid:
                    leg_guess = "call"
                ledger = self._write_slave_product_leg_close_ledger(
                    db,
                    slave=slave,
                    slave_trade=slave_trade,
                    leg_type=leg_guess,
                    product_id=pid,
                    closed_at=meta.get("closed_at") or exit_batch_ts,
                    fill_at=meta.get("fill_at"),
                    reason="SWEEP_CLOSE",
                )
                log_and_buffer(
                    "SLAVE_LEG_CLOSE",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "product_id": pid,
                        "live_size": meta.get("live_size"),
                        "ledger": ledger,
                        "outcome": "ok",
                        "path": "sweep_extra_pid",
                    },
                )

            if self._close_slave_trade(
                slave,
                slave_trade,
                reason="sweep_closed_ok",
                allow_virtual=False,
            ):
                slave_trade.last_error = None
                slave_trade.last_updated = get_utc_now()
                db.commit()
                log_and_buffer(
                    "SLAVE_SWEEP",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "slave_trade": int(slave_trade.id),
                        "outcome": "closed_ok",
                        "reason": "sweep_closed_ok",
                    },
                )
                return "closed_ok"
            return "close_failed"
        finally:
            await client.close()

    async def check_slave_integrity(self, master_trade_id: int) -> None:
        """
        ACTIVE-master integrity checks (called from sweep_open_slave_trades):

        1. Active SlaveTrades should still have open options — else mark closed.
        2. Error / partial rows: alert + limited entry retry.
        3. Naked one-legged short while master has two opens → CRITICAL.
        4. Virtual/paper SlaveTrades are never closed here.

        Closed-master recovery lives in sweep_open_slave_trades /
        _recover_slave_under_closed_master (must verify Delta first).

        Acquires the per-slave lock for each mutating check/retry. Caller
        (sweep) must NOT hold the lock — nested acquisition would deadlock.
        """
        with self.db_factory() as db:
            master = (
                db.query(Trade)
                .filter(Trade.id == int(master_trade_id))
                .first()
            )
            master_active = bool(
                master is not None
                and str(getattr(master, "status", "")).lower()
                == TradeStatus.ACTIVE.value
            )
            if not master_active:
                # Closed masters are handled by the DB sweep recover path
                return

            from backend.models import Leg

            master_open_legs = (
                db.query(Leg)
                .filter(
                    Leg.trade_id == int(master_trade_id),
                    Leg.status == "open",
                    Leg.is_bot_managed.is_(True),
                    Leg.leg_type.in_(("call", "put")),
                )
                .count()
            )

            # --- Reconcile error / blocked / adjust/exit-failure rows ---
            problem_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == int(master_trade_id),
                    SlaveTrade.status.in_(
                        self._INTEGRITY_PROBLEM_STATUSES
                    ),
                )
                .all()
            )
            for st in problem_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == st.slave_account_id)
                    .first()
                )

                # Re-attempt unwind for naked / partial-entry leftovers
                if st.status in ("partial_entry_open", "partial_adjustment"):
                    if slave is None or not slave.is_active:
                        continue
                    if is_virtual_slave_trade(slave, st):
                        continue
                    async with self._slave_op_lock(
                        int(slave.id), "check_slave_integrity"
                    ) as acquired:
                        if not acquired:
                            continue
                        await self._repair_partial_or_naked_slave(
                            db=db,
                            slave=slave,
                            slave_trade=st,
                            path=(
                                "integrity_partial_entry_open"
                                if st.status == "partial_entry_open"
                                else "integrity_partial_adjustment"
                            ),
                        )
                    continue

                # Master still active — surface adjust/exit failures every cycle
                if st.status in (
                    "adjust_close_failed",
                    "exit_failed",
                ):
                    logger.critical(
                        "[SLAVE_INTEGRITY] slave_trade=%s slave='%s' "
                        "status=%s last_error=%s — not healthy active; "
                        "needs manual review or master exit",
                        st.id,
                        getattr(slave, "name", "?"),
                        st.status,
                        (st.last_error or "")[:200],
                    )
                    continue

                # Retry mirror once for retriable entry errors
                err = str(st.last_error or "")
                already_retried = "[RETRY_DONE]" in err
                err_count = int(st.error_count or 0)
                retriable = (
                    not already_retried
                    and err_count <= 1
                    and st.status in ("error", "partial")
                    and "blocked_foreign" not in err
                    and "skipped_low_capital" not in err
                )
                if not retriable or slave is None or not slave.is_active:
                    continue
                if is_virtual_slave_trade(slave, st):
                    continue

                call_leg = (
                    db.query(Leg)
                    .filter(
                        Leg.trade_id == int(master_trade_id),
                        Leg.leg_type == "call",
                        Leg.status == "open",
                        Leg.is_bot_managed.is_(True),
                    )
                    .first()
                )
                put_leg = (
                    db.query(Leg)
                    .filter(
                        Leg.trade_id == int(master_trade_id),
                        Leg.leg_type == "put",
                        Leg.status == "open",
                        Leg.is_bot_managed.is_(True),
                    )
                    .first()
                )
                if call_leg is None or put_leg is None:
                    continue

                async with self._slave_op_lock(
                    int(slave.id), "check_slave_integrity"
                ) as acquired:
                    if not acquired:
                        continue
                    logger.info(
                        "[SLAVE_RECONCILE] retrying mirror once for "
                        "slave_trade=%s slave='%s' master=#%s",
                        st.id,
                        slave.name,
                        master_trade_id,
                    )
                    st.last_error = (f"[RETRY_DONE] prior: {err}")[:500]
                    st.error_count = err_count + 1
                    st.status = "closed"  # clear slot so new entry can record
                    st.last_updated = get_utc_now()
                    db.commit()

                    try:
                        await self._mirror_entry_to_slave(
                            slave=slave,
                            master_trade_id=int(master_trade_id),
                            call_product_id=int(call_leg.product_id),
                            put_product_id=int(put_leg.product_id),
                            master_call_qty=int(call_leg.quantity or 1),
                            master_put_qty=int(put_leg.quantity or 1),
                            master_call_strike=float(call_leg.strike or 0),
                            master_put_strike=float(put_leg.strike or 0),
                            master_call_symbol=str(call_leg.symbol or ""),
                            master_put_symbol=str(put_leg.symbol or ""),
                            master_call_fill=float(
                                call_leg.initial_premium or 0
                            ),
                            master_put_fill=float(
                                put_leg.initial_premium or 0
                            ),
                            expiry_date=getattr(master, "expiry_date", None),
                            underlying=str(
                                getattr(master, "underlying", "") or ""
                            ),
                            db=db,
                        )
                    except Exception as retry_exc:
                        logger.warning(
                            "[SLAVE_RECONCILE] retry failed slave_trade=%s: %s",
                            st.id,
                            retry_exc,
                        )

            # --- Active book: empty / naked one-legged ---
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == int(master_trade_id),
                    SlaveTrade.status == "active",
                )
                .all()
            )
            if not slave_trades:
                return

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if not slave or not slave.is_active:
                    continue

                # Permanent guard: never integrity-close virtual/paper trades
                if is_virtual_slave_trade(slave, slave_trade):
                    logger.warning(
                        "Slave integrity SKIP (virtual): slave='%s' "
                        "slave_trade=%s is_virtual=%s call_order_id=%s "
                        "put_order_id=%s — leaving status=active",
                        slave.name,
                        slave_trade.id,
                        bool(getattr(slave, "is_virtual", False)),
                        slave_trade.call_order_id,
                        slave_trade.put_order_id,
                    )
                    continue

                async with self._slave_op_lock(
                    int(slave.id), "check_slave_integrity"
                ) as acquired:
                    if not acquired:
                        continue
                    await self._check_slave_active_book_integrity(
                        db=db,
                        slave=slave,
                        slave_trade=slave_trade,
                        master_open_legs=master_open_legs,
                    )

    async def _check_slave_active_book_integrity(
        self,
        *,
        db: Any,
        slave: SlaveAccount,
        slave_trade: SlaveTrade,
        master_open_legs: int,
    ) -> None:
        # Caller MUST hold the per-slave lock.
        client = self._get_slave_client(slave)
        try:
            slave_positions = await client.get_option_positions()
            if not slave_positions:
                logger.warning(
                    "Slave '%s' has NO open positions but "
                    "SlaveTrade %s is 'active'. Marking as closed.",
                    slave.name,
                    slave_trade.id,
                )
                if self._close_slave_trade(
                    slave,
                    slave_trade,
                    reason="integrity_empty_book",
                    allow_virtual=False,
                ):
                    slave_trade.last_updated = get_utc_now()
                    db.commit()
                return

            # Count distinct short option products (size < 0)
            short_pids: set[int] = set()
            short_sizes: dict[int, float] = {}
            for pos in slave_positions:
                try:
                    size = float(pos.get("size") or 0)
                    pid = int(pos.get("product_id") or 0)
                except (TypeError, ValueError):
                    continue
                if size < 0 and pid > 0:
                    short_pids.add(pid)
                    short_sizes[pid] = size

            if master_open_legs >= 2 and len(short_pids) == 1:
                naked_pid = next(iter(short_pids))
                bot_owned = self._bot_owned_product_ids(db, int(slave.id))
                msg = (
                    f"naked_one_leg: slave has {len(short_pids)} "
                    f"short product(s) {sorted(short_pids)} while "
                    f"master has {master_open_legs} open legs"
                )
                logger.critical(
                    "[SLAVE_INTEGRITY] slave='%s' slave_trade=%s %s",
                    slave.name,
                    slave_trade.id,
                    msg,
                )

                if naked_pid not in bot_owned:
                    # Foreign short — never touch; leave for human
                    slave_trade.status = "partial_adjustment"
                    slave_trade.last_error = (
                        f"{msg} | FOREIGN product={naked_pid} not bot-owned"
                    )[:500]
                    slave_trade.error_count = (
                        int(slave_trade.error_count or 0) + 1
                    )
                    slave_trade.last_updated = get_utc_now()
                    slave.connection_status = "error"
                    slave.last_error = slave_trade.last_error
                    db.commit()
                    return

                # Close bot-owned naked short immediately (verified fill)
                ok, _ord, err = await self._close_with_reduce_only(
                    client=client,
                    slave=slave,
                    product_id=int(naked_pid),
                    signed_size=float(short_sizes.get(naked_pid) or -1),
                    master_trade_id=int(slave_trade.master_trade_id or 0),
                    path="naked_one_leg",
                )
                if ok:
                    if self._close_slave_trade(
                        slave,
                        slave_trade,
                        reason="naked_one_leg_closed",
                        allow_virtual=False,
                    ):
                        slave_trade.last_error = (
                            f"{msg} | closed product={naked_pid}"
                        )[:500]
                        slave_trade.last_updated = get_utc_now()
                        db.commit()
                    logger.critical(
                        "[SLAVE_INTEGRITY] slave='%s' slave_trade=%s "
                        "naked short product=%s CLOSED",
                        slave.name,
                        slave_trade.id,
                        naked_pid,
                    )
                else:
                    slave_trade.status = "partial_adjustment"
                    slave_trade.last_error = (
                        f"{msg} | close_failed: {err}"
                    )[:500]
                    slave_trade.error_count = (
                        int(slave_trade.error_count or 0) + 1
                    )
                    slave_trade.last_updated = get_utc_now()
                    slave.connection_status = "error"
                    slave.last_error = slave_trade.last_error
                    db.commit()
                    logger.critical(
                        "[SLAVE_INTEGRITY] slave='%s' slave_trade=%s "
                        "naked close FAILED product=%s err=%s — "
                        "status=partial_adjustment for sweep retry",
                        slave.name,
                        slave_trade.id,
                        naked_pid,
                        (err or "")[:200],
                    )
            else:
                logger.debug(
                    "Slave '%s' integrity OK: %s option positions "
                    "(%s shorts)",
                    slave.name,
                    len(slave_positions),
                    len(short_pids),
                )
        except Exception as exc:
            logger.warning(
                "Slave '%s' integrity check failed: %s",
                slave.name,
                exc,
            )
        finally:
            await client.close()


# Global singleton — set during app lifespan
mirror_engine: MirrorEngine | None = None
