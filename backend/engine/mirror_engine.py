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

from backend.core.delta_client import DeltaClient
from backend.core.encryption import decrypt
from backend.core.time_utils import get_ist_now
from backend.database import SessionLocal, get_active_slave_accounts
from backend.models import SlaveAccount, SlaveTrade

logger = logging.getLogger(__name__)


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

    def _calc_qty(self, master_qty: int, multiplier: float) -> int:
        """Calculate slave qty from master qty and multiplier (min 1)."""
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
        # Use call qty as primary lot size (strangle/straddle qty is symmetric)
        slave_qty = self._calc_qty(master_call_qty, float(slave.qty_multiplier or 1.0))

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
        Cancel SL orders + close both shorts (+ long hedge if present) at market.
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
                return

            logger.info(
                "Mirroring exit to %s slaves: reason=%s hedge=%s",
                len(slave_trades),
                reason,
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
        client = self._get_slave_client(slave)
        qty = max(1, int(slave_trade.actual_quantity or 1))

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

            # Verify slave positions before close
            try:
                call_on_slave = await client.verify_position_exists(
                    int(call_product_id)
                )
                put_on_slave = await client.verify_position_exists(
                    int(put_product_id)
                )
            except Exception as exc:
                logger.warning(
                    "Slave '%s' position verify failed: %s — assume exists",
                    slave.name,
                    exc,
                )
                call_on_slave = True
                put_on_slave = True

            if call_on_slave:
                await client.place_order(
                    product_id=int(call_product_id),
                    size=qty,
                    side="buy",
                )
                logger.info("Slave '%s' call closed", slave.name)
            else:
                logger.warning(
                    "Slave '%s' call not on Delta, skipping close",
                    slave.name,
                )

            if put_on_slave:
                await client.place_order(
                    product_id=int(put_product_id),
                    size=qty,
                    side="buy",
                )
                logger.info("Slave '%s' put closed", slave.name)
            else:
                logger.warning(
                    "Slave '%s' put not on Delta, skipping close",
                    slave.name,
                )

            # Close long hedge with SELL (not buy)
            if hedge_product_id:
                try:
                    hedge_on_slave = await client.verify_position_exists(
                        int(hedge_product_id)
                    )
                except Exception:
                    hedge_on_slave = True
                if hedge_on_slave:
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

            # Always mark closed in DB (positions may already be gone)
            slave_trade.status = "closed"
            slave_trade.call_sl_order_id = None
            slave_trade.put_sl_order_id = None
            slave_trade.last_updated = get_ist_now()
            db.commit()

            logger.info(
                "Slave '%s' exit complete for trade %s: reason=%s qty=%s",
                slave.name,
                slave_trade.master_trade_id,
                reason,
                qty,
            )

        except Exception as exc:
            logger.error("Slave '%s' exit FAILED: %s", slave.name, exc)
            try:
                db.rollback()
            except Exception:
                pass
            # Still try to mark closed so we don't leave stuck active slaves
            try:
                slave_trade.status = "closed"
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
                        slave_trade.status = "closed"
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
