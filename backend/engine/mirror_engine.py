# mirror_engine.py — Replicate master trade actions onto active slave accounts

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.bot_logger import log_and_buffer
from backend.core.delta_client import DeltaClient
from backend.core.encryption import decrypt
from backend.core.time_utils import get_ist_now
from backend.database import SessionLocal, get_active_slave_accounts
from backend.models import SlaveAccount, SlaveTrade, Trade
from backend.config import MAX_SLAVE_QTY, TradeStatus

logger = logging.getLogger(__name__)


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
            st.last_updated = get_ist_now()

        db.commit()
        return "cleared"

    @staticmethod
    def _order_id(order_result: dict[str, Any] | None) -> str:
        if not order_result:
            return ""
        oid = order_result.get("order_id") or order_result.get("id")
        return str(oid) if oid is not None else ""

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
    ) -> None:
        """
        Mirror a new trade entry on all active slave accounts.
        Called right after master trade is placed successfully.
        Non-fatal: if one slave fails, others continue.
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
    ) -> None:
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
            virt_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                actual_quantity=slave_qty,
                call_fill_price=float(master_call_fill or 0),
                put_fill_price=float(master_put_fill or 0),
                call_order_id="VIRTUAL",
                put_order_id="VIRTUAL",
                status="active",
            )
            db.add(virt_trade)
            db.commit()
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
                        slave.updated_at = get_ist_now()
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
                        slave.updated_at = get_ist_now()
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

            # Bracket SL from master's universal_sl_pct (default 200%)
            uni_sl = self._master_universal_sl_pct(db, int(master_trade_id))
            call_baseline = float(master_call_fill or 0.0)
            put_baseline = float(master_put_fill or 0.0)
            call_sl = (
                round(call_baseline * (uni_sl / 100.0), 2)
                if call_baseline > 0
                else None
            )
            put_sl = (
                round(put_baseline * (uni_sl / 100.0), 2)
                if put_baseline > 0
                else None
            )

            # Place call order on slave (bracket SL attached)
            call_order = await client.place_order(
                product_id=int(call_product_id),
                size=slave_qty,
                side="sell",
                bracket_stop_loss_price=call_sl,
                bracket_stop_loss_limit_price=(
                    round(call_sl * 1.05, 2) if call_sl else None
                ),
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

            # Place put order on slave (bracket SL attached)
            put_order = await client.place_order(
                product_id=int(put_product_id),
                size=slave_qty,
                side="sell",
                bracket_stop_loss_price=put_sl,
                bracket_stop_loss_limit_price=(
                    round(put_sl * 1.05, 2) if put_sl else None
                ),
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

            slave_trade = SlaveTrade(
                slave_account_id=int(slave.id),
                master_trade_id=int(master_trade_id),
                call_order_id=call_order_id or None,
                put_order_id=put_order_id or None,
                call_sl_order_id=None,  # bracket has no separate stop-order ID
                put_sl_order_id=None,
                actual_quantity=slave_qty,
                call_fill_price=call_fill,
                put_fill_price=put_fill,
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
            slave.last_connected_at = get_ist_now()
            slave.updated_at = get_ist_now()
            db.commit()

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
            slave.updated_at = get_ist_now()
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
    ) -> None:
        """
        Mirror an adjustment on all slaves.
        Close old leg, open new leg — atomic verify-close-verify.
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
                {"slaves_found": len(slave_trades)},
            )
            logger.info(
                "[MIRROR_ADJ_ENGINE] Trade#%s slaves found=%s uni_sl=%.1f%%",
                master_trade_id,
                len(slave_trades),
                uni_sl,
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
                    else:
                        st.put_order_id = "VIRTUAL"
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
                try:
                    await client.cancel_order(int(slave_trade.call_sl_order_id))
                    slave_trade.call_sl_order_id = None
                except Exception as exc:
                    logger.warning("Slave SL cancel failed: %s", exc)
            elif leg == "put" and slave_trade.put_sl_order_id:
                try:
                    await client.cancel_order(int(slave_trade.put_sl_order_id))
                    slave_trade.put_sl_order_id = None
                except Exception as exc:
                    logger.warning("Slave SL cancel failed: %s", exc)

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
                    slave_trade.last_updated = get_ist_now()
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

            # --- d. Open new leg; bracket SL from mark or post-fill ---
            expected_fill = 0.0
            try:
                expected_fill = float(
                    await client.get_mark_price(new_symbol)
                )
            except Exception as mark_exc:
                logger.warning(
                    "[MIRROR_ADJ] slave='%s' get_mark_price(%s) failed: %s "
                    "— will set bracket SL after fill or skip",
                    slave.name,
                    new_symbol,
                    mark_exc,
                )
                expected_fill = 0.0

            new_sl = None
            new_sl_limit = None
            if expected_fill > 0:
                new_sl = round(expected_fill * (uni_sl / 100.0), 2)
                new_sl_limit = (
                    round(new_sl * 1.05, 2) if new_sl > 0 else None
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

            # If mark failed but we have a fill, log that SL was skipped
            if expected_fill <= 0:
                if new_fill > 0:
                    logger.warning(
                        "[MIRROR_ADJ] slave='%s' bracket SL skipped "
                        "(no mark); new fill=$%.2f — no old-leg fallback",
                        slave.name,
                        new_fill,
                    )
                else:
                    logger.warning(
                        "[MIRROR_ADJ] slave='%s' bracket SL skipped "
                        "(no mark and no fill yet)",
                        slave.name,
                    )

            logger.info(
                "Slave '%s' opened new %s: strike=%s fill=%s id=%s "
                "bracket_sl=%s (uni_sl=%.1f%%) qty=%s",
                slave.name,
                leg,
                new_strike,
                new_fill,
                new_order_id,
                new_sl,
                uni_sl,
                entry_qty,
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
                slave_trade.last_updated = get_ist_now()
                if leg == "call":
                    slave_trade.call_order_id = new_order_id or None
                else:
                    slave_trade.put_order_id = new_order_id or None
                slave.connection_status = "error"
                slave.last_error = msg[:500]
                db.commit()
                return

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

            # Keep actual_quantity synced to live entry size
            slave_trade.actual_quantity = entry_qty
            slave_trade.status = "active"
            slave_trade.last_error = None
            slave_trade.last_updated = get_ist_now()
            slave.last_error = None
            slave.connection_status = "connected"
            slave.last_connected_at = get_ist_now()
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
                slave_trade.last_updated = get_ist_now()
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
    ) -> None:
        """
        AUDIT-7: Mirror conversion-mode entry to slaves.

        1) BUY long hedge (no bracket SL)
        2) Close old other short + open new other short
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
                "Mirroring conversion to %s slaves: hedge=%s other=%s→%s",
                len(slave_trades),
                hedge_symbol,
                old_other_product_id,
                new_other_product_id,
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

                    # 3) Open new other short (bracket SL = master universal_sl_pct)
                    uni_sl = self._master_universal_sl_pct(
                        db, int(master_trade_id)
                    )
                    expected_fill = 0.0
                    try:
                        expected_fill = float(
                            await client.get_mark_price(new_other_symbol)
                        )
                    except Exception as mark_exc:
                        logger.warning(
                            "[MIRROR_CONV] slave='%s' mark failed for %s: %s "
                            "— skip bracket SL (no old-leg fallback)",
                            slave.name,
                            new_other_symbol,
                            mark_exc,
                        )
                        expected_fill = 0.0
                    new_sl = (
                        round(expected_fill * (uni_sl / 100.0), 2)
                        if expected_fill > 0
                        else None
                    )
                    new_order = await client.place_order(
                        product_id=int(new_other_product_id),
                        size=qty,
                        side="sell",
                        bracket_stop_loss_price=new_sl,
                        bracket_stop_loss_limit_price=(
                            round(new_sl * 1.05, 2) if new_sl else None
                        ),
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
                    slave.last_connected_at = get_ist_now()
                    db.commit()
                    logger.info(
                        "✅ Slave '%s' conversion mirrored (hedge + %s replace)",
                        slave.name,
                        leg,
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

    async def mirror_hedge_close(
        self,
        master_trade_id: int,
        hedge_product_id: int,
    ) -> None:
        """AUDIT-7: SELL-close long hedge on all active slaves (reversal)."""
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
                            "Slave '%s' hedge closed (sell) product=%s",
                            slave.name,
                            hedge_product_id,
                        )
                    else:
                        logger.warning(
                            "Slave '%s' hedge not on Delta — skip close",
                            slave.name,
                        )
                except Exception as exc:
                    logger.error(
                        "Slave '%s' hedge close FAILED: %s", slave.name, exc
                    )
                finally:
                    await client.close()

    async def mirror_leg_close(
        self,
        master_trade_id: int,
        leg_type: str,
        product_id: int,
    ) -> dict[str, int]:
        """
        Close a single leg (call or put) on every non-closed slave.

        Live-position targeting for product_id, reduce_only close, then verify
        that product is flat. Does NOT close the whole basket (use mirror_exit).
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
                    continue

                ok = await self._mirror_leg_close_to_slave(
                    slave=slave,
                    slave_trade=slave_trade,
                    leg_type=leg,
                    product_id=target_pid,
                    db=db,
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
    ) -> bool:
        """
        Close one product on one slave. Returns True if verified flat (or
        already flat / virtual).
        """
        leg = str(leg_type).lower()
        target_pid = int(product_id)

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
            slave_trade.last_updated = get_ist_now()
            db.commit()
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
                slave_trade.last_error = (
                    f"leg_close_failed: positions fetch failed for {leg} "
                    f"product={target_pid}"
                )[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                slave_trade.last_updated = get_ist_now()
                db.commit()
                logger.critical(
                    "[MIRROR_LEG_CLOSE] slave='%s' exit_failed fetch "
                    "leg=%s product_id=%s",
                    slave.name,
                    leg,
                    target_pid,
                )
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
                    # Retry without reduce_only path via place_order
                    side = "sell" if is_long else "buy"
                    try:
                        await client.place_order(
                            product_id=target_pid,
                            size=close_size,
                            side=side,
                            reduce_only=True,
                        )
                    except Exception as retry_exc:
                        logger.error(
                            "[MIRROR_LEG_CLOSE] slave='%s' close FAILED "
                            "product=%s: %s / %s",
                            slave.name,
                            target_pid,
                            close_exc,
                            retry_exc,
                        )
                        slave_trade.last_error = (
                            f"leg_close_failed: {leg} product={target_pid} "
                            f"{retry_exc}"
                        )[:500]
                        slave_trade.error_count = (
                            int(slave_trade.error_count or 0) + 1
                        )
                        slave_trade.last_updated = get_ist_now()
                        db.commit()
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
                slave_trade.last_error = msg[:500]
                slave_trade.error_count = (
                    int(slave_trade.error_count or 0) + 1
                )
                slave_trade.last_updated = get_ist_now()
                db.commit()
                return False

            if leg == "call":
                slave_trade.call_sl_order_id = None
            else:
                slave_trade.put_sl_order_id = None
            slave_trade.last_error = None
            slave_trade.last_updated = get_ist_now()
            db.commit()
            logger.info(
                "[MIRROR_LEG_CLOSE] slave='%s' %s product=%s verified flat",
                slave.name,
                leg,
                target_pid,
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
                slave_trade.last_error = (
                    f"leg_close_failed: exception {exc}"
                )[:500]
                slave_trade.error_count = (
                    int(slave_trade.error_count or 0) + 1
                )
                slave_trade.last_updated = get_ist_now()
                db.commit()
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
                logger.info(
                    "[MIRROR_EXIT] Trade#%s — no non-closed slave_trades "
                    "(call=%s put=%s reason=%s)",
                    master_trade_id,
                    call_product_id,
                    put_product_id,
                    reason,
                )
                return

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
                    st.last_updated = get_ist_now()
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
            return

        client = self._get_slave_client(slave)
        stored_qty = max(1, int(slave_trade.actual_quantity or 1))

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
                    # No hints matched and no shorts found via filter —
                    # close entire option book for this dedicated slave
                    targets = list(live_positions)
                    logger.warning(
                        "[MIRROR_EXIT] Slave '%s' — closing ALL %s live "
                        "option positions (hints stale or empty)",
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

            closed_count = 0
            target_pids: set[int] = set()
            for pos in targets:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
                sym = str(pos.get("product_symbol") or "")
                if pid <= 0 or size == 0:
                    continue
                target_pids.add(pid)
                close_size = max(1, abs(int(size)))
                if size < 0:
                    side = "buy"  # close short
                else:
                    side = "sell"  # close long hedge
                # Explicit fallback override when inventing hint rows
                if pos.get("_fallback_side"):
                    side = str(pos["_fallback_side"])
                try:
                    await client.place_order(
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
                        await client.place_order(
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

            # Products that must be flat: hints + anything we tried to close
            check_pids = set(hint_ids) | target_pids
            check_pids.discard(0)

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
                slave_trade.last_updated = get_ist_now()
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
            slave_trade.last_error = None
            slave_trade.last_updated = get_ist_now()
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
                slave_trade.last_updated = get_ist_now()
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
                    slave.last_connected_at = get_ist_now()
                    slave.last_error = None
                    slave.updated_at = get_ist_now()
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
                    slave.updated_at = get_ist_now()
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
                    slave_trade.last_updated = get_ist_now()
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

    async def check_slave_integrity(self, master_trade_id: int) -> None:
        """
        Periodic check (every 5 monitor cycles):

        1. Active SlaveTrades should still have open options — else mark closed.
        2. Error / partial_adjustment / adjust_close_failed / exit_failed:
           alert + retry (entry errors only); if master CLOSED → force close.
        3. Naked one-legged short while master has two opens → CRITICAL.
        4. Virtual/paper SlaveTrades are never closed here.
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

            from backend.models import Leg

            master_open_legs = 0
            if master_active:
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
                if not master_active:
                    prior_status = str(st.status)
                    # Broken adjust/exit states may still have live Delta legs —
                    # attempt live exit before clearing the DB row.
                    if (
                        prior_status
                        in (
                            "partial_adjustment",
                            "adjust_close_failed",
                            "exit_failed",
                        )
                        and slave is not None
                        and not is_virtual_slave_trade(slave, st)
                    ):
                        try:
                            await self._mirror_exit_to_slave(
                                slave=slave,
                                slave_trade=st,
                                call_product_id=0,
                                put_product_id=0,
                                reason="reconcile_master_closed",
                                db=db,
                            )
                        except Exception as exit_exc:
                            logger.critical(
                                "[SLAVE_RECONCILE] live exit failed "
                                "slave_trade=%s status=%s: %s",
                                st.id,
                                prior_status,
                                exit_exc,
                            )
                    if self._close_slave_trade(
                        slave,
                        st,
                        reason="reconcile_master_closed",
                        allow_virtual=bool(
                            slave and getattr(slave, "is_virtual", False)
                        ),
                    ):
                        st.last_error = (
                            f"reconcile: master #{master_trade_id} closed — "
                            f"cleared prior status={prior_status}"
                        )[:500]
                        st.last_updated = get_ist_now()
                        db.commit()
                        logger.info(
                            "[SLAVE_RECONCILE] slave_trade=%s force-closed "
                            "(master #%s not active)",
                            st.id,
                            master_trade_id,
                        )
                    continue

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
                    # Do NOT auto-retry entry: would double exposure.
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
                st.last_updated = get_ist_now()
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
                            slave_trade.last_updated = get_ist_now()
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
                        slave_trade.last_updated = get_ist_now()
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
