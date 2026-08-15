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
           slave_qty = master_qty × qty_multiplier

        2. Capital-based mode (capital_based_qty=True):
           effective_capital = min(user_allocated_capital, slave_available)
           master_ratio = master_margin_used / master_total_capital
           per_lot_cost_usd = master_margin_used / master_qty
           slave_margin_to_use = effective_capital × master_ratio
           slave_qty = max(1, round(slave_margin_to_use / per_lot_cost_usd))

           All values in USD. Falls back to fixed multiplier if capital
           data is unavailable.
        """
        if (
            slave is not None
            and bool(getattr(slave, "capital_based_qty", False))
            and master_margin_used_usd is not None
            and master_margin_used_usd > 0
            and master_total_capital_usd is not None
            and master_total_capital_usd > 0
            and master_qty > 0
        ):
            user_allocated = float(
                getattr(slave, "user_allocated_capital", None) or 0
            )

            # Get slave available balance (fresh from API or cached)
            slave_available = (
                float(slave_available_usd)
                if slave_available_usd is not None
                else float(getattr(slave, "balance_usd", 0) or 0)
            )

            # Effective capital = min(what user allocated, what is actually available)
            if user_allocated > 0 and slave_available > 0:
                effective_capital = min(user_allocated, slave_available)
            elif user_allocated > 0:
                effective_capital = user_allocated
            else:
                effective_capital = slave_available

            if effective_capital > 0:
                master_ratio = (
                    master_margin_used_usd / master_total_capital_usd
                )
                per_lot_cost_usd = master_margin_used_usd / master_qty
                slave_margin_to_use = effective_capital * master_ratio

                if per_lot_cost_usd > 0:
                    calculated_qty = max(
                        1,
                        round(slave_margin_to_use / per_lot_cost_usd),
                    )
                else:
                    calculated_qty = master_qty

                logger.info(
                    "Capital-based qty: master_total=$%.2f master_used=$%.2f "
                    "master_qty=%s master_ratio=%.3f per_lot=$%.4f "
                    "slave_allocated=$%.2f slave_available=$%.2f "
                    "effective=$%.2f slave_margin=$%.2f → slave_qty=%s",
                    master_total_capital_usd,
                    master_margin_used_usd,
                    master_qty,
                    master_ratio,
                    per_lot_cost_usd,
                    user_allocated,
                    slave_available,
                    effective_capital,
                    slave_margin_to_use,
                    calculated_qty,
                )
                return int(calculated_qty)

        # Fallback: fixed multiplier
        return max(1, int(round(float(master_qty) * float(multiplier))))

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

            # Fetch fresh slave balance from Delta API
            # (virtual/paper slaves have no real wallet — use allocated/cached)
            if bool(getattr(slave, "is_virtual", False)):
                allocated = float(
                    getattr(slave, "user_allocated_capital", None) or 0
                )
                cached = float(getattr(slave, "balance_usd", 0) or 0)
                slave_fresh_available = (
                    allocated if allocated > 0 else cached
                )
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
                        # Update cached balance in DB
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
                    # Fallback to cached balance
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
            # Guard: skip if slave already holds these products
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
                        "Slave '%s' already has conflicting positions: %s. "
                        "Skipping mirror entry.",
                        slave.name,
                        symbols,
                    )
                    slave_trade = SlaveTrade(
                        slave_account_id=int(slave.id),
                        master_trade_id=int(master_trade_id),
                        actual_quantity=slave_qty,
                        status="error",
                        last_error="Conflicting positions already exist on slave",
                        error_count=1,
                    )
                    db.add(slave_trade)
                    slave.connection_status = "error"
                    slave.last_error = "Conflicting positions already exist on slave"
                    slave.updated_at = get_ist_now()
                    db.commit()
                    return
            except Exception as exc:
                logger.warning(
                    "Slave '%s' position check failed: %s — continuing",
                    slave.name,
                    exc,
                )

            # Bracket SL confirmed working on Delta Exchange India
            # Format: bracket_stop_loss_price + bracket_stop_loss_limit_price
            # Default 200% of master fill (slave fill unknown until after place)
            call_baseline = float(master_call_fill or 0.0)
            put_baseline = float(master_put_fill or 0.0)
            call_sl = round(call_baseline * 2.0, 2) if call_baseline > 0 else None
            put_sl = round(put_baseline * 2.0, 2) if put_baseline > 0 else None

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
    ) -> None:
        """
        Mirror an adjustment on all slaves.
        Close old leg, open new leg with same product_id.
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

            log_and_buffer(
                "MIRROR_ADJ_ENGINE",
                master_trade_id,
                {"slaves_found": len(slave_trades)},
            )
            logger.info(
                "[MIRROR_ADJ_ENGINE] Trade#%s slaves found=%s",
                master_trade_id,
                len(slave_trades),
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
                )

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
                    # No live slave fill in virtual mode — keep existing fills
                    # and mark the adjusted leg order as VIRTUAL
                    leg = str(triggered_leg_type).lower()
                    if leg == "call":
                        st.call_order_id = "VIRTUAL"
                    else:
                        st.put_order_id = "VIRTUAL"
                    virt_db.commit()
            return

        client = self._get_slave_client(slave)
        qty = max(1, int(slave_trade.actual_quantity or 1))
        leg = str(triggered_leg_type).lower()

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

            # Close old leg (buy back) — bracket SL on that position auto-cancels
            close_order = await client.place_order(
                product_id=int(old_product_id),
                size=qty,
                side="buy",
            )
            logger.info(
                "Slave '%s' closed %s: order_id=%s",
                slave.name,
                leg,
                self._order_id(close_order),
            )

            # Open new leg with bracket SL (200% of expected fill / mark)
            expected_fill = 0.0
            try:
                expected_fill = float(
                    await client.get_mark_price(new_symbol)
                )
            except Exception:
                expected_fill = float(
                    (slave_trade.call_fill_price or 0)
                    if leg == "call"
                    else (slave_trade.put_fill_price or 0)
                )
            new_sl = round(expected_fill * 2.0, 2) if expected_fill > 0 else None

            new_order = await client.place_order(
                product_id=int(new_product_id),
                size=qty,
                side="sell",
                bracket_stop_loss_price=new_sl,
                bracket_stop_loss_limit_price=(
                    round(new_sl * 1.05, 2) if new_sl else None
                ),
            )
            new_fill = float(
                await client.resolve_fill_price(
                    new_order, symbol_for_fallback=new_symbol
                )
                or 0.0
            )
            new_order_id = self._order_id(new_order)

            logger.info(
                "Slave '%s' opened new %s: strike=%s fill=%s id=%s bracket_sl=%s",
                slave.name,
                leg,
                new_strike,
                new_fill,
                new_order_id,
                new_sl,
            )

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
            logger.info("✅ Slave '%s' adjustment mirrored", slave.name)

        except Exception as exc:
            logger.error(
                "❌ Slave '%s' adjustment FAILED: %s", slave.name, exc
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

                    # 3) Open new other short (with bracket SL like normal adj)
                    expected_fill = 0.0
                    try:
                        expected_fill = float(
                            await client.get_mark_price(new_other_symbol)
                        )
                    except Exception:
                        expected_fill = 0.0
                    new_sl = (
                        round(expected_fill * 2.0, 2) if expected_fill > 0 else None
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
                    "[MIRROR_EXIT] Trade#%s — no active slave_trades "
                    "(call=%s put=%s reason=%s)",
                    master_trade_id,
                    call_product_id,
                    put_product_id,
                    reason,
                )
                return

            logger.info(
                "[MIRROR_EXIT] Trade#%s mirroring to %s slaves: "
                "reason=%s hint_call=%s hint_put=%s hint_hedge=%s",
                master_trade_id,
                len(slave_trades),
                reason,
                call_product_id,
                put_product_id,
                hedge_product_id,
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
                        SlaveTrade.status == "active",
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
            try:
                live_positions = await client.get_option_positions()
            except Exception as pos_exc:
                logger.error(
                    "[MIRROR_EXIT] Slave '%s' get_option_positions FAILED: %s "
                    "— falling back to hint product_ids",
                    slave.name,
                    pos_exc,
                )
                live_positions = []

            hint_ids = {
                int(pid)
                for pid in (call_product_id, put_product_id, hedge_product_id or 0)
                if pid and int(pid) > 0
            }
            logger.info(
                "[MIRROR_EXIT] Slave '%s' live_positions=%s hint_ids=%s "
                "stored_qty=%s",
                slave.name,
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
                # Positions API empty — last resort: try hint product_ids
                logger.warning(
                    "[MIRROR_EXIT] Slave '%s' no live positions returned — "
                    "trying hint product_ids call=%s put=%s hedge=%s",
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
                            "product_symbol": f"hint-hedge-{hedge_product_id}",
                            "_fallback_side": "sell",
                        }
                    )

            closed_count = 0
            for pos in targets:
                pid = int(pos.get("product_id") or 0)
                size = float(pos.get("size") or 0)
                sym = str(pos.get("product_symbol") or "")
                if pid <= 0 or size == 0:
                    continue
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

            # Always mark closed in DB (positions may already be gone)
            if not self._close_slave_trade(
                slave,
                slave_trade,
                reason=f"mirror_exit:{reason}",
                allow_virtual=False,
            ):
                return
            slave_trade.call_sl_order_id = None
            slave_trade.put_sl_order_id = None
            slave_trade.last_updated = get_ist_now()
            db.commit()

            logger.info(
                "[MIRROR_EXIT] Slave '%s' exit complete trade=%s "
                "reason=%s closed_legs=%s",
                slave.name,
                slave_trade.master_trade_id,
                reason,
                closed_count,
            )

        except Exception as exc:
            logger.error("Slave '%s' exit FAILED: %s", slave.name, exc)
            try:
                db.rollback()
            except Exception:
                pass
            # Still try to mark closed so we don't leave stuck active slaves
            # (never auto-close virtual/paper SlaveTrades)
            try:
                if not self._close_slave_trade(
                    slave,
                    slave_trade,
                    reason=f"mirror_exit_failed:{exc}",
                    allow_virtual=False,
                ):
                    slave_trade.last_error = str(exc)[:500]
                    slave_trade.error_count = (
                        int(slave_trade.error_count or 0) + 1
                    )
                    slave_trade.last_updated = get_ist_now()
                    db.commit()
                    return
                slave_trade.call_sl_order_id = None
                slave_trade.put_sl_order_id = None
                slave_trade.last_error = str(exc)[:500]
                slave_trade.error_count = int(slave_trade.error_count or 0) + 1
                slave_trade.last_updated = get_ist_now()
                db.commit()
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
        Periodic check: active SlaveTrades should still have open options
        on the slave account. Mark closed if the slave book is empty.

        Virtual/paper SlaveTrades are never closed here.
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
                    else:
                        logger.debug(
                            "Slave '%s' integrity OK: %s option positions",
                            slave.name,
                            len(slave_positions),
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
