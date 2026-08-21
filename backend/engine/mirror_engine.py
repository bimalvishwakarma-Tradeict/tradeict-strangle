# mirror_engine.py — Replicate master trade actions onto active slave accounts

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
    """

    def __init__(self, db_factory: Callable[[], Any] | None = None) -> None:
        self.db_factory = db_factory or SessionLocal
        # Per-slave serialisation for hedge open (never half-open two legs concurrently)
        self._slave_hedge_locks: dict[int, asyncio.Lock] = {}

    def _get_slave_hedge_lock(self, slave_id: int) -> asyncio.Lock:
        sid = int(slave_id)
        lock = self._slave_hedge_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._slave_hedge_locks[sid] = lock
        return lock

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

        allowed = reason_norm in VALID_HEDGE_EXIT_REASONS or reason_norm.startswith(
            "HEDGE_"
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

    def _calc_qty(
        self,
        master_qty: int,
        multiplier: float,
        slave: SlaveAccount | None = None,
        master_margin_used_usd: float | None = None,
        master_total_capital_usd: float | None = None,
        slave_available_usd: float | None = None,
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

        Hard ceiling: MAX_SLAVE_QTY (config) on every path.
        """
        max_qty = max(1, int(MAX_SLAVE_QTY))
        mq = max(0, int(master_qty or 0))

        if (
            slave is not None
            and bool(getattr(slave, "capital_based_qty", False))
            and master_margin_used_usd is not None
            and master_margin_used_usd > 0
            and master_total_capital_usd is not None
            and master_total_capital_usd > 0
            and mq > 0
        ):
            user_allocated = float(
                getattr(slave, "user_allocated_capital", None) or 0
            )
            is_virtual = bool(getattr(slave, "is_virtual", False))
            live = (
                float(slave_available_usd)
                if slave_available_usd is not None
                else 0.0
            )

            # Real slaves: refuse allocated-only sizing when live balance missing
            if not is_virtual and live <= 0:
                logger.warning(
                    "[SLAVE_SIZING] account_id=%s allocated=%.2f "
                    "live_balance=%.2f — refuse sizing without live balance",
                    getattr(slave, "id", None),
                    user_allocated,
                    live,
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
                logger.info(
                    "[SLAVE_SIZING] account_id=%s allocated=%.2f "
                    "live_balance=%.2f effective=0 → qty=0",
                    getattr(slave, "id", None),
                    user_allocated,
                    live,
                )
                return 0

            master_ratio = master_margin_used_usd / master_total_capital_usd
            per_lot_cost_usd = master_margin_used_usd / mq
            slave_margin_to_use = effective_capital * master_ratio

            if per_lot_cost_usd <= 0:
                calculated_qty = 0
            else:
                calculated_qty = int(
                    round(slave_margin_to_use / per_lot_cost_usd)
                )

            # Do NOT force max(1, ...) — insufficient capital must skip
            final_qty = max(0, min(calculated_qty, max_qty))
            logger.info(
                "[SLAVE_SIZING] account_id=%s allocated=%.2f "
                "live_balance=%.2f effective=%.2f "
                "master_used=%.2f per_lot=%.4f → final_qty=%s "
                "(raw=%s cap=%s)",
                getattr(slave, "id", None),
                user_allocated,
                live,
                effective_capital,
                master_margin_used_usd,
                per_lot_cost_usd,
                final_qty,
                calculated_qty,
                max_qty,
            )
            return int(final_qty)

        # Fallback: fixed multiplier (unchanged semantics), plus hard ceiling
        if mq <= 0:
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
    ) -> int:
        """
        Reduce qty until estimated margin ≤ 90% of live balance.
        Returns 0 if even 1 lot does not fit.
        """
        from backend.config import OPTIONS_CONTRACT_VALUE

        qty = max(0, int(slave_qty or 0))
        if qty <= 0:
            return 0
        balance = float(live_balance or 0.0)
        if balance <= 0:
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

        # Close each conflicting position (reduce_only preferred)
        for pos in conflicting:
            try:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or size == 0:
                continue
            close_size = max(1, abs(int(size)))
            side = "buy" if size < 0 else "sell"
            try:
                await client.place_order(
                    product_id=pid,
                    size=close_size,
                    side=side,
                    reduce_only=True,
                )
            except Exception as close_exc:
                try:
                    await client.place_order(
                        product_id=pid,
                        size=close_size,
                        side=side,
                    )
                except Exception as retry_exc:
                    logger.error(
                        "[SLAVE_CONFLICT_RESOLVE] slave='%s' close FAILED "
                        "pid=%s: %s / %s",
                        slave.name,
                        pid,
                        close_exc,
                        retry_exc,
                    )
                    return "failed"
            logger.info(
                "[SLAVE_CONFLICT_RESOLVE] slave='%s' closed pid=%s "
                "size=%s side=%s",
                slave.name,
                pid,
                close_size,
                side,
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
    ) -> None:
        """
        Mirror a new trade entry on all active slave accounts.
        Called right after master trade is placed successfully.
        Non-fatal: if one slave fails, others continue.

        master_bracket_sl_* are ABSOLUTE stop prices from the master's fill —
        slaves must use them verbatim (never recompute from slave fill/mark).
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
    ) -> None:
        # --- No naked baskets: require alive slave hedge under master hedge ---
        master_hedge_id = self._resolve_master_hedge_id_for_trade(
            db, int(master_trade_id)
        )
        if master_hedge_id is not None:
            alive_hedge = self._get_alive_slave_hedge(
                db,
                slave_id=int(slave.id),
                master_hedge_id=int(master_hedge_id),
            )
            if alive_hedge is None:
                logger.info(
                    "[SLAVE_ENTRY_BLOCK] slave=%s | guard=no_slave_hedge | "
                    "master_hedge=%s | master_trade=%s",
                    slave.id,
                    master_hedge_id,
                    master_trade_id,
                )
                log_and_buffer(
                    "SLAVE_ENTRY_BLOCK",
                    int(master_trade_id),
                    {
                        "slave": int(slave.id),
                        "guard": "no_slave_hedge",
                        "master_hedge": int(master_hedge_id),
                        "master_trade": int(master_trade_id),
                    },
                )
                skip_trade = SlaveTrade(
                    slave_account_id=int(slave.id),
                    master_trade_id=int(master_trade_id),
                    actual_quantity=0,
                    status="skipped_no_hedge",
                    last_error=(
                        f"no_slave_hedge master_hedge={master_hedge_id}"
                    )[:500],
                    error_count=0,
                )
                db.add(skip_trade)
                db.commit()
                return

        # Fetch master + fresh slave capital for capital-based qty calculation
        master_margin_used: float | None = None
        master_total_capital: float | None = None
        slave_fresh_available: float | None = None

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
                            wallet = await master_client.get_wallet_balance()
                            master_total_capital = float(
                                wallet.get("balance_usdt", 0) or 0
                            )
                            master_available = float(
                                wallet.get("available_balance", 0) or 0
                            )
                            master_margin_used = max(
                                0.0,
                                master_total_capital - master_available,
                            )
                            logger.info(
                                "Master capital: total=$%.2f available=$%.2f "
                                "used=$%.2f",
                                master_total_capital,
                                master_available,
                                master_margin_used,
                            )
                        finally:
                            await master_client.close()
            except Exception as cap_err:
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
            )

        if slave_qty < 1:
            msg = (
                f"skipped_low_capital: live=${live_for_margin:.2f} "
                f"allocated=${float(getattr(slave, 'user_allocated_capital', 0) or 0):.2f}"
            )
            logger.warning(
                "[SLAVE_SIZING] slave='%s' master_trade=%s — %s "
                "(no order placed)",
                slave.name,
                master_trade_id,
                msg,
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
                call_entry_fee_usd=0.0,
                put_entry_fee_usd=0.0,
                entry_spread_usd=float(virt_entry_spread or 0.0),
                status="active",
            )
            db.add(virt_trade)
            db.commit()
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

            # Place call order on slave (bracket SL attached)
            call_order = await client.place_order(
                product_id=int(call_product_id),
                size=slave_qty,
                side="sell",
                bracket_stop_loss_price=call_sl,
                bracket_stop_loss_limit_price=call_sl_limit,
            )
            call_fill = float(
                await client.resolve_fill_price(
                    call_order, symbol_for_fallback=master_call_symbol
                )
                or 0.0
            )
            if call_fill <= 0:
                call_fill = float(master_call_fill or 0.0)
            call_order_id = self._order_id(call_order)

            logger.info(
                "Slave '%s' CALL placed: qty=%s fill=%s id=%s bracket_sl=%s",
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

            # Place put order on slave (bracket SL attached)
            put_order = await client.place_order(
                product_id=int(put_product_id),
                size=slave_qty,
                side="sell",
                bracket_stop_loss_price=put_sl,
                bracket_stop_loss_limit_price=put_sl_limit,
            )
            put_fill = float(
                await client.resolve_fill_price(
                    put_order, symbol_for_fallback=master_put_symbol
                )
                or 0.0
            )
            if put_fill <= 0:
                put_fill = float(master_put_fill or 0.0)
            put_order_id = self._order_id(put_order)

            logger.info(
                "Slave '%s' PUT placed: qty=%s fill=%s id=%s bracket_sl=%s",
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

            if call_verified and put_verified:
                status = "active"
                last_error = None
                err_count = 0
                logger.info(
                    "Slave '%s' both positions verified", slave.name
                )
            elif call_verified or put_verified:
                status = "error"
                last_error = (
                    f"Partial fill: call={call_verified} put={put_verified}"
                )[:500]
                err_count = 1
                logger.error(
                    "Slave '%s' PARTIAL position: call=%s put=%s",
                    slave.name,
                    call_verified,
                    put_verified,
                )
            else:
                status = "error"
                last_error = "No positions found after placement"
                err_count = 1
                logger.error(
                    "Slave '%s' NO positions found after entry!",
                    slave.name,
                )

            call_entry_fee = await self._resolve_order_fee(client, call_order)
            put_entry_fee = await self._resolve_order_fee(client, put_order)
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
                call_fill_price=call_fill,
                put_fill_price=put_fill,
                call_product_id=int(call_product_id),
                put_product_id=int(put_product_id),
                call_symbol=str(master_call_symbol or ""),
                put_symbol=str(master_put_symbol or ""),
                call_strike=float(master_call_strike or 0),
                put_strike=float(master_put_strike or 0),
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
            try:
                db.rollback()
            except Exception:
                pass
            slave.connection_status = "error"
            slave.last_error = str(exc)[:500]
            slave.updated_at = get_utc_now()
            failed_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                actual_quantity=slave_qty,
                status="error",
                last_error=str(exc)[:500],
                error_count=1,
            )
            db.add(failed_trade)
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
        """Return live signed size for product_id, or None if absent."""
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
            if live_size is None or live_size == 0:
                logger.info(
                    "[MIRROR_ADJ_SKIP] slave='%s' already_flat "
                    "old_product=%s — skip close, open new leg",
                    slave.name,
                    old_pid,
                )
            else:
                close_size = max(1, abs(int(live_size)))
                is_long = float(live_size) > 0
                close_order = await client.close_position(
                    product_id=old_pid,
                    size=close_size,
                    is_long=is_long,
                )
                logger.info(
                    "[MIRROR_ADJ_VERIFY] slave='%s' stage=close_sent "
                    "product_id=%s size=%s is_long=%s order_id=%s",
                    slave.name,
                    old_pid,
                    close_size,
                    is_long,
                    self._order_id(close_order),
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
                if still is not None and abs(float(still)) > 0:
                    msg = (
                        f"adjust_close_failed: old product {old_pid} "
                        f"still open size={still} after close"
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
                    db.commit()
                    return

            # Close size for new entry: prefer live abs size, else stored
            entry_qty = (
                max(1, abs(int(live_size)))
                if live_size is not None and live_size != 0
                else stored_qty
            )

            # --- d. Open new leg; bracket SL = master's absolute stop ---
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

            new_order = await client.place_order(
                product_id=new_pid,
                size=entry_qty,
                side="sell",
                bracket_stop_loss_price=new_sl,
                bracket_stop_loss_limit_price=new_sl_limit,
            )
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
                db.commit()
                return

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
            # Prefer partial if we likely closed old but failed before verify
            try:
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

                client = self._get_slave_client(slave)
                qty = max(1, int(slave_trade.actual_quantity or 1))
                leg = str(other_leg_type).lower()
                try:
                    # 1) Buy hedge — long, no bracket SL
                    hedge_order = await client.place_order(
                        product_id=int(hedge_product_id),
                        size=qty,
                        side="buy",
                    )
                    logger.info(
                        "Slave '%s' hedge bought: product=%s order=%s",
                        slave.name,
                        hedge_product_id,
                        self._order_id(hedge_order),
                    )

                    # 2) Close old other short
                    await client.place_order(
                        product_id=int(old_other_product_id),
                        size=qty,
                        side="buy",
                    )

                    # 3) Open new other short — absolute master bracket SL
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
                    new_order = await client.place_order(
                        product_id=int(new_other_product_id),
                        size=qty,
                        side="sell",
                        bracket_stop_loss_price=new_sl,
                        bracket_stop_loss_limit_price=new_sl_limit,
                    )
                    new_fill = float(
                        await client.resolve_fill_price(
                            new_order, symbol_for_fallback=new_other_symbol
                        )
                        or 0.0
                    )
                    new_order_id = self._order_id(new_order)
                    if leg == "call":
                        slave_trade.call_order_id = new_order_id or None
                        slave_trade.call_sl_order_id = None
                        if new_fill > 0:
                            slave_trade.call_fill_price = new_fill
                    else:
                        slave_trade.put_order_id = new_order_id or None
                        slave_trade.put_sl_order_id = None
                        if new_fill > 0:
                            slave_trade.put_fill_price = new_fill

                    slave.last_error = None
                    slave.connection_status = "connected"
                    slave.last_connected_at = get_utc_now()
                    db.commit()
                    logger.info(
                        "✅ Slave '%s' conversion mirrored (hedge + %s replace) "
                        "bracket_sl=%s",
                        slave.name,
                        leg,
                        new_sl,
                    )
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
                    finally:
                        await m_client.close()
            except Exception as cap_err:
                logger.warning(
                    "[SLAVE_HEDGE_OPEN] master capital fetch failed: %s",
                    cap_err,
                )

            for slave in slaves:
                async with self._get_slave_hedge_lock(int(slave.id)):
                    result = await self._mirror_hedge_open_to_slave(
                        slave=slave,
                        db=work_db,
                        master_hedge_id=master_hedge_id,
                        master_account_id=master_account_id,
                        master_qty=master_qty,
                        master_hedge_cost=master_hedge_cost,
                        master_margin_used=master_margin_used,
                        master_total_capital=master_total_capital,
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
            put_result = await executor.buy_option(
                product_id=int(put_pid),
                quantity=int(slave_qty),
                delta_client=client,
                symbol_for_fallback=put_symbol or None,
            )
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

                async with self._get_slave_hedge_lock(sid):
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
        """Baskets → verify → hedge for one slave. Never naked-strangle."""
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
                        reason=f"HEDGE_CASCADE:{reason}",
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
                    # Hedge legs are longs — any remaining SHORT means a
                    # basket is still live (product_ids may differ after adj).
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
                                if size < -1e-9 and pid > 0:
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
            sh.exit_time = get_utc_now()
            sh.last_error = None
            db.commit()
            return True

        from backend.engine.order_executor import OrderExecutor

        client = self._get_slave_client(slave)
        executor = OrderExecutor()
        call_order: dict[str, Any] | None = None
        put_order: dict[str, Any] | None = None
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
                    continue
                try:
                    order = await client.close_position(
                        product_id=int(pid),
                        size=int(qty),
                        is_long=True,
                    )
                    if leg == "call":
                        call_order = order if isinstance(order, dict) else None
                    else:
                        put_order = order if isinstance(order, dict) else None
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
                        if leg == "call":
                            call_order = payload
                        else:
                            put_order = payload
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

            sh.call_exit_price = call_exit if call_exit > 0 else None
            sh.put_exit_price = put_exit if put_exit > 0 else None
            sh.realized_pnl = realized
            sh.status = "closed"
            sh.exit_reason = reason
            sh.exit_time = get_utc_now()
            sh.last_error = None
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
        """AUDIT-7: SELL-close legacy conversion hedge on active slaves."""
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
                client = self._get_slave_client(slave)
                qty = max(1, int(slave_trade.actual_quantity or 1))
                try:
                    exists = await client.verify_position_exists(
                        int(hedge_product_id)
                    )
                    if exists:
                        await client.place_order(
                            product_id=int(hedge_product_id),
                            size=qty,
                            side="sell",
                        )
                        logger.info(
                            "Slave '%s' conversion hedge closed (sell) "
                            "product=%s",
                            slave.name,
                            hedge_product_id,
                        )
                    else:
                        logger.warning(
                            "Slave '%s' conversion hedge not on Delta — skip",
                            slave.name,
                        )
                except Exception as exc:
                    logger.error(
                        "Slave '%s' conversion hedge close FAILED: %s",
                        slave.name,
                        exc,
                    )
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
        """
        leg = str(leg_type).lower()
        target_pid = int(product_id)

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
            logger.info(
                "[MIRROR_LEG_CLOSE] VIRTUAL slave='%s' leg=%s — DB only",
                slave.name,
                leg,
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
                logger.error(
                    "[MIRROR_LEG_CLOSE] slave='%s' get_option_positions "
                    "FAILED: %s — not treating as flat",
                    slave.name,
                    pos_exc,
                )

            if not fetch_ok:
                msg = (
                    f"leg_close_failed: positions fetch failed for {leg} "
                    f"product={target_pid}"
                )
                logger.critical(
                    "[MIRROR_LEG_CLOSE] slave='%s' %s",
                    slave.name,
                    msg,
                )
                _mark_failure(msg)
                return False

            live_size = self._position_size_for_product(
                live_positions, target_pid
            )
            logger.info(
                "[MIRROR_LEG_CLOSE] slave='%s' stage=pre_close "
                "product_id=%s live_size=%s",
                slave.name,
                target_pid,
                live_size,
            )

            if live_size is None or live_size == 0:
                logger.info(
                    "[MIRROR_LEG_CLOSE] slave='%s' already_flat product=%s",
                    slave.name,
                    target_pid,
                )
            else:
                close_size = max(1, abs(int(live_size)))
                is_long = float(live_size) > 0
                try:
                    await client.close_position(
                        product_id=target_pid,
                        size=close_size,
                        is_long=is_long,
                    )
                except Exception as close_exc:
                    # Retry with reduce_only place_order
                    side = "sell" if is_long else "buy"
                    try:
                        await client.place_order(
                            product_id=target_pid,
                            size=close_size,
                            side=side,
                            reduce_only=True,
                        )
                    except Exception as retry_exc:
                        msg = (
                            f"leg_close_failed: {leg} product={target_pid} "
                            f"{retry_exc}"
                        )
                        logger.error(
                            "[MIRROR_LEG_CLOSE] slave='%s' close FAILED "
                            "product=%s: %s / %s",
                            slave.name,
                            target_pid,
                            close_exc,
                            retry_exc,
                        )
                        logger.critical(
                            "[MIRROR_LEG_CLOSE] slave='%s' %s",
                            slave.name,
                            msg,
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
                logger.critical(
                    "[MIRROR_LEG_CLOSE] slave='%s' VERIFY fetch FAILED: %s",
                    slave.name,
                    verify_exc,
                )

            still = (
                self._position_size_for_product(verify_positions, target_pid)
                if verify_ok
                else None
            )
            logger.info(
                "[MIRROR_LEG_CLOSE] slave='%s' stage=post_close "
                "product_id=%s expected=0 actual_size=%s verify_ok=%s",
                slave.name,
                target_pid,
                still,
                verify_ok,
            )

            if (not verify_ok) or (
                still is not None and abs(float(still)) > 0
            ):
                msg = (
                    f"leg_close_failed: {leg} product={target_pid} "
                    f"still_size={still} verify_ok={verify_ok}"
                )
                logger.critical(
                    "[MIRROR_LEG_CLOSE] slave='%s' %s",
                    slave.name,
                    msg,
                )
                _mark_failure(msg)
                return False

            if leg == "call":
                slave_trade.call_sl_order_id = None
            else:
                slave_trade.put_sl_order_id = None
            _mark_success()
            logger.info(
                "[MIRROR_LEG_CLOSE] slave='%s' %s product=%s verified flat%s",
                slave.name,
                leg,
                target_pid,
                f" status={success_status}" if success_status else "",
            )
            return True
        except Exception as exc:
            logger.error(
                "[MIRROR_LEG_CLOSE] slave='%s' exception: %s",
                slave.name,
                exc,
                exc_info=True,
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
                    st.exit_time = get_utc_now()
                    st.exit_reason = str(reason or "")[:50]
                    st.last_updated = get_utc_now()
                    virt_db.commit()
                    logger.info(
                        "VIRTUAL EXIT done: slave='%s' slave_trade_id=%s",
                        slave.name,
                        st.id,
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
            return

        client = self._get_slave_client(slave)
        stored_qty = max(1, int(slave_trade.actual_quantity or 1))
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
                )
                if pid and int(pid) > 0
            }
            logger.info(
                "[MIRROR_EXIT] Slave '%s' positions_fetch_ok=%s "
                "live_positions=%s hint_ids=%s stored_qty=%s",
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
                stored_qty,
            )

            targets: list[dict[str, Any]] = []
            if live_positions:
                # Prefer positions matching master hints when present
                matched = [
                    p
                    for p in live_positions
                    if int(p.get("product_id") or 0) in hint_ids
                ]
                matched_pids = {
                    int(p.get("product_id") or 0) for p in matched
                }
                # Always include unmatched SHORTS — adjusted legs may not be
                # in hint_ids when master/slave drifted
                extras = [
                    p
                    for p in live_positions
                    if float(p.get("size") or 0) < 0
                    and int(p.get("product_id") or 0) not in matched_pids
                ]
                # Longs: include if matches hedge hint, or if any hedge was
                # expected and this long wasn't already matched
                if hedge_product_id:
                    for p in live_positions:
                        pid = int(p.get("product_id") or 0)
                        size = float(p.get("size") or 0)
                        if size > 0 and pid not in matched_pids:
                            extras.append(p)

                if matched or extras:
                    targets = matched + extras
                else:
                    # No hints matched — close SHORTS only; never structure hedge
                    targets = [
                        p
                        for p in live_positions
                        if float(p.get("size") or 0) < 0
                    ]
                    logger.warning(
                        "[MIRROR_EXIT] Slave '%s' — closing %s short "
                        "option positions (hints stale; hedge protected)",
                        slave.name,
                        len(targets),
                    )
            else:
                # Empty book OR fetch failed — last resort: hint product_ids
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
                ):
                    if pid <= 0:
                        continue
                    targets.append(
                        {
                            "product_id": pid,
                            "size": -float(stored_qty),
                            "product_symbol": f"hint-{pid}",
                            "_fallback_side": side,
                        }
                    )
                if hedge_product_id:
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
            exit_by_pid: dict[int, dict[str, float]] = {}
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
                close_order: dict[str, Any] | None = None
                try:
                    close_order = await client.place_order(
                        product_id=pid,
                        size=close_size,
                        side=side,
                        reduce_only=True,
                    )
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
                except Exception as close_exc:
                    # Retry without reduce_only (some accounts reject it)
                    try:
                        close_order = await client.place_order(
                            product_id=pid,
                            size=close_size,
                            side=side,
                        )
                        closed_count += 1
                        logger.info(
                            "[MIRROR_EXIT] Slave '%s' closed %s product=%s "
                            "size=%s side=%s (retry no reduce_only)",
                            slave.name,
                            sym,
                            pid,
                            close_size,
                            side,
                        )
                    except Exception as retry_exc:
                        logger.error(
                            "[MIRROR_EXIT] Slave '%s' FAILED close "
                            "product=%s size=%s side=%s: %s / %s",
                            slave.name,
                            pid,
                            close_size,
                            side,
                            close_exc,
                            retry_exc,
                        )
                        continue
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
                    }

            # Products that must be flat: basket hints + closed targets ONLY
            # (never require structure hedge longs to be flat)
            check_pids = set(hint_ids) | target_pids
            check_pids.discard(0)
            check_pids -= protected_hedge_pids

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
                    # If we have specific pids to check, only those matter;
                    # if none (already-flat path with no hints), any leftover
                    # option size blocks closed.
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
            slave_trade.last_error = None
            slave_trade.last_updated = get_utc_now()
            db.commit()

            logger.info(
                "[MIRROR_EXIT] Slave '%s' exit complete (verified flat) "
                "trade=%s reason=%s closed_legs=%s",
                slave.name,
                slave_trade.master_trade_id,
                reason,
                closed_count,
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


    async def update_all_slave_mtm(self, master_trade_id: int) -> None:
        """
        Refresh last_mtm for every active SlaveTrade under this master trade.

        Fetches live UPNL from each slave's Delta /v2/positions/margined so the
        Multi-Account Overview never shows a stale one-time copy value.
        """
        with self.db_factory() as db:
            slave_trades = (
                db.query(SlaveTrade)
                .filter(
                    SlaveTrade.master_trade_id == int(master_trade_id),
                    SlaveTrade.status == "active",
                )
                .all()
            )
            if not slave_trades:
                logger.debug(
                    "No active slave trades for master %s",
                    master_trade_id,
                )
                return

            for slave_trade in slave_trades:
                slave = (
                    db.query(SlaveAccount)
                    .filter(SlaveAccount.id == slave_trade.slave_account_id)
                    .first()
                )
                if slave is None or not slave.is_active:
                    continue

                # Virtual/paper: no real Delta positions — keep last_mtm from overview
                if is_virtual_slave_trade(slave, slave_trade):
                    logger.debug(
                        "Slave '%s' MTM skip (virtual SlaveTrade %s)",
                        slave.name,
                        slave_trade.id,
                    )
                    continue

                client = self._get_slave_client(slave)
                try:
                    # UPL@Offer via get_positions_upnl (best_ask, never API
                    # unrealized_pnl / mark-only).
                    upnl_map = await client.get_positions_upnl()
                    total_upnl = 0.0
                    for row in upnl_map.values():
                        try:
                            size = float(row.get("size") or 0)
                        except (TypeError, ValueError):
                            size = 0.0
                        if size == 0:
                            continue
                        total_upnl += float(row.get("upnl") or 0.0)

                    old_mtm = float(slave_trade.last_mtm or 0.0)
                    slave_trade.last_mtm = round(total_upnl, 4)
                    slave_trade.last_updated = get_utc_now()
                    db.commit()

                    logger.info(
                        "Slave '%s' MTM updated (UPL@Offer): %s → %.4f",
                        slave.name,
                        old_mtm,
                        total_upnl,
                    )
                except Exception as exc:
                    logger.warning(
                        "Slave '%s' MTM fetch failed: %s",
                        slave.name,
                        exc,
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                finally:
                    await client.close()

    # After this many consecutive close failures, only retry every Nth sweep
    _SWEEP_BACKOFF_AFTER = 3
    _SWEEP_BACKOFF_EVERY = 6  # generations between retries once backed off

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

            open_statuses = (
                "active",
                "partial",
                "partial_adjustment",
                "adjust_close_failed",
                "exit_failed",
                "error",
            )
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
                    et = exit_time
                    if getattr(et, "tzinfo", None) is None:
                        et = et.replace(tzinfo=timezone.utc)
                    orphan_sec = max(
                        0,
                        int(
                            (now - et.astimezone(timezone.utc)).total_seconds()
                        ),
                    )

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

            # Prefer master leg product_ids; else close entire live option book
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

            live_nonzero: list[dict[str, Any]] = []
            for pos in live_positions or []:
                try:
                    pid = int(pos.get("product_id") or 0)
                    size = float(pos.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or abs(size) <= 0:
                    continue
                # Close master's products if known; otherwise whole book.
                # Always include shorts (adjusted legs may drift from master).
                if master_pids:
                    if pid in master_pids or size < 0:
                        live_nonzero.append(pos)
                else:
                    live_nonzero.append(pos)

            if not live_nonzero:
                if self._close_slave_trade(
                    slave,
                    slave_trade,
                    reason="sweep_verified_flat",
                    allow_virtual=False,
                ):
                    slave_trade.last_error = None
                    slave_trade.last_updated = get_utc_now()
                    db.commit()
                    logger.info(
                        "[SLAVE_SWEEP] slave='%s' slave_trade=%s verified "
                        "flat → closed (master #%s)",
                        slave.name,
                        slave_trade.id,
                        master_trade_id,
                    )
                    return "closed_ok"
                return "close_failed"

            # Attempt real closes
            for pos in live_nonzero:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
                close_size = max(1, abs(int(size)))
                is_long = size > 0
                try:
                    await client.close_position(
                        product_id=pid,
                        size=close_size,
                        is_long=is_long,
                    )
                    logger.info(
                        "[SLAVE_SWEEP] slave='%s' closed product=%s "
                        "size=%s is_long=%s",
                        slave.name,
                        pid,
                        close_size,
                        is_long,
                    )
                except Exception as close_exc:
                    side = "sell" if is_long else "buy"
                    try:
                        await client.place_order(
                            product_id=pid,
                            size=close_size,
                            side=side,
                            reduce_only=True,
                        )
                    except Exception as retry_exc:
                        logger.error(
                            "[SLAVE_SWEEP] slave='%s' close FAILED "
                            "product=%s: %s / %s",
                            slave.name,
                            pid,
                            close_exc,
                            retry_exc,
                        )

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
                logger.critical(
                    "[SLAVE_SWEEP] slave='%s' verify UNREACHABLE: %s",
                    slave.name,
                    verify_exc,
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
                # Any leftover option size blocks closed for a dedicated slave
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
                logger.critical(
                    "[SLAVE_SWEEP] slave='%s' slave_trade=%s still has "
                    "positions after close: %s",
                    slave.name,
                    slave_trade.id,
                    remaining,
                )
                return "close_failed"

            if self._close_slave_trade(
                slave,
                slave_trade,
                reason="sweep_closed_ok",
                allow_virtual=False,
            ):
                slave_trade.last_error = None
                slave_trade.last_updated = get_utc_now()
                db.commit()
                logger.info(
                    "[SLAVE_SWEEP] slave='%s' slave_trade=%s closed OK "
                    "(master #%s)",
                    slave.name,
                    slave_trade.id,
                    master_trade_id,
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
                        (
                            "error",
                            "partial",
                            "partial_adjustment",
                            "adjust_close_failed",
                            "exit_failed",
                            "blocked_foreign_position",
                            "skipped_low_capital",
                        )
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

                # Master still active — surface adjust/exit failures every cycle
                if st.status in (
                    "partial_adjustment",
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
                        continue

                    # Count distinct short option products (size < 0)
                    short_pids: set[int] = set()
                    for pos in slave_positions:
                        try:
                            size = float(pos.get("size") or 0)
                            pid = int(pos.get("product_id") or 0)
                        except (TypeError, ValueError):
                            continue
                        if size < 0 and pid > 0:
                            short_pids.add(pid)

                    if (
                        master_open_legs >= 2
                        and len(short_pids) == 1
                    ):
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
                        slave_trade.status = "partial_adjustment"
                        slave_trade.last_error = msg[:500]
                        slave_trade.error_count = (
                            int(slave_trade.error_count or 0) + 1
                        )
                        slave_trade.last_updated = get_utc_now()
                        slave.connection_status = "error"
                        slave.last_error = msg[:500]
                        db.commit()
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
