# database.py — SQLAlchemy engine, session factory, Base, and DB init helpers

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

# Allow `python backend/database.py` from trading-bot/ root
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import DATABASE_URL


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


_connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and closes it afterward."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_schema() -> None:
    """Add missing columns for existing SQLite DBs (create_all does not alter)."""
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if "trades" in tables:
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "monitoring_starts_at" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE trades ADD COLUMN monitoring_starts_at DATETIME")
                )
        if "basket_number" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE trades ADD COLUMN basket_number INTEGER")
                )
                conn.execute(
                    text(
                        """
                        UPDATE trades
                        SET basket_number = (
                            SELECT COUNT(*)
                            FROM trades t2
                            WHERE t2.account_id = trades.account_id
                              AND t2.id <= trades.id
                        )
                        WHERE basket_number IS NULL
                        """
                    )
                )
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "initial_max_profit" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE trades ADD COLUMN initial_max_profit FLOAT")
                )
        if "tp_pct" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE trades ADD COLUMN tp_pct FLOAT DEFAULT 50.0")
                )
                conn.execute(
                    text("UPDATE trades SET tp_pct = 50.0 WHERE tp_pct IS NULL")
                )
        if "sl_pct" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE trades ADD COLUMN sl_pct FLOAT DEFAULT 100.0")
                )
                conn.execute(
                    text("UPDATE trades SET sl_pct = 100.0 WHERE sl_pct IS NULL")
                )
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "slippage_pct" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE trades ADD COLUMN slippage_pct FLOAT DEFAULT 2.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE trades SET slippage_pct = 2.0 WHERE slippage_pct IS NULL"
                    )
                )
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "cumulative_entry_spread_usd" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE trades ADD COLUMN "
                        "cumulative_entry_spread_usd REAL DEFAULT 0.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE trades SET cumulative_entry_spread_usd = 0.0 "
                        "WHERE cumulative_entry_spread_usd IS NULL"
                    )
                )
        # Preserve existing TP/SL $: backfill max from target / (tp_pct/100)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE trades
                    SET tp_pct = COALESCE(tp_pct, 50.0),
                        sl_pct = COALESCE(sl_pct, 100.0)
                    WHERE tp_pct IS NULL OR sl_pct IS NULL
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE trades
                    SET initial_max_profit = CASE
                        WHEN profit_target_usd IS NOT NULL
                             AND COALESCE(tp_pct, 50.0) > 0
                        THEN profit_target_usd / (COALESCE(tp_pct, 50.0) / 100.0)
                        ELSE NULL
                    END
                    WHERE initial_max_profit IS NULL
                      AND profit_target_usd IS NOT NULL
                      AND profit_target_usd > 0
                    """
                )
            )
    if "legs" in tables:
        leg_cols = {col["name"] for col in inspector.get_columns("legs")}
        if "trigger_premium" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN trigger_premium FLOAT")
                )
                conn.execute(
                    text(
                        "UPDATE legs SET trigger_premium = initial_premium "
                        "WHERE trigger_premium IS NULL"
                    )
                )
        # Refresh columns after possible alter
        leg_cols = {col["name"] for col in inspector.get_columns("legs")}
        if "trigger_baseline_premium" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE legs ADD COLUMN trigger_baseline_premium FLOAT"
                    )
                )
                # Prefer existing trigger_premium; else initial_premium
                conn.execute(
                    text(
                        """
                        UPDATE legs
                        SET trigger_baseline_premium = COALESCE(
                            trigger_premium, initial_premium
                        )
                        WHERE trigger_baseline_premium IS NULL
                        """
                    )
                )
        if "realized_pnl" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN realized_pnl FLOAT")
                )
        if "entry_fee_usd" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN entry_fee_usd FLOAT")
                )
        leg_cols = {col["name"] for col in inspector.get_columns("legs")}
        if "order_sent_price" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN order_sent_price REAL")
                )
        if "entry_spread_usd" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN entry_spread_usd REAL")
                )
        if "exit_fee_usd" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN exit_fee_usd FLOAT")
                )
        if "exit_order_id" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN exit_order_id VARCHAR(100)")
                )
        leg_cols = {col["name"] for col in inspector.get_columns("legs")}
        if "delta_sl_order_id" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE legs ADD COLUMN delta_sl_order_id VARCHAR(100)"
                    )
                )
        if "sl_trigger_price" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN sl_trigger_price FLOAT")
                )
        leg_cols = {col["name"] for col in inspector.get_columns("legs")}
        if "is_long" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE legs ADD COLUMN "
                        "is_long BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
    if "trades" in tables:
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "universal_sl_pct" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE trades ADD COLUMN universal_sl_pct "
                        "FLOAT DEFAULT 200.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE trades SET universal_sl_pct = 200.0 "
                        "WHERE universal_sl_pct IS NULL"
                    )
                )
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        for col_name in [
            "in_conversion_mode",
            "conversion_hedge_product_id",
            "conversion_hedge_order_id",
            "conversion_hedge_entry_price",
            "conversion_hedge_symbol",
            "conversion_triggered_leg",
        ]:
            if col_name not in trade_cols:
                if col_name == "in_conversion_mode":
                    col_type = "BOOLEAN NOT NULL DEFAULT 0"
                elif "price" in col_name:
                    col_type = "FLOAT"
                elif "product_id" in col_name:
                    col_type = "INTEGER"
                else:
                    col_type = "VARCHAR(100)"
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}"
                        )
                    )
                trade_cols.add(col_name)
    if "adjustments" in tables:
        adj_cols = {col["name"] for col in inspector.get_columns("adjustments")}
        if "decision_type" not in adj_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE adjustments ADD COLUMN decision_type VARCHAR(40)"
                    )
                )
    if "auto_trade_settings" in tables:
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "usd_inr_rate" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN usd_inr_rate FLOAT DEFAULT 85.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings SET usd_inr_rate = 85.0 "
                        "WHERE usd_inr_rate IS NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "trade_type" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN trade_type VARCHAR DEFAULT 'straddle'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings SET trade_type = 'straddle' "
                        "WHERE trade_type IS NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "target_premium_per_side" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN target_premium_per_side FLOAT DEFAULT 150.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings "
                        "SET target_premium_per_side = 150.0 "
                        "WHERE target_premium_per_side IS NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "adj_low_premium_exit_enabled" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN adj_low_premium_exit_enabled "
                        "BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings "
                        "SET adj_low_premium_exit_enabled = 0 "
                        "WHERE adj_low_premium_exit_enabled IS NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "adj_low_premium_min_usd" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN adj_low_premium_min_usd "
                        "FLOAT NOT NULL DEFAULT 150.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings "
                        "SET adj_low_premium_min_usd = 150.0 "
                        "WHERE adj_low_premium_min_usd IS NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "expiry_date_override" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN expiry_date_override VARCHAR(10) DEFAULT NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "conversion_equality_pct" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN conversion_equality_pct "
                        "FLOAT NOT NULL DEFAULT 10.0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings "
                        "SET conversion_equality_pct = 10.0 "
                        "WHERE conversion_equality_pct IS NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "conversion_mode_enabled" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN conversion_mode_enabled "
                        "BOOLEAN NOT NULL DEFAULT 1"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE auto_trade_settings "
                        "SET conversion_mode_enabled = 1 "
                        "WHERE conversion_mode_enabled IS NULL"
                    )
                )
        if "max_adjustments_per_basket" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN max_adjustments_per_basket "
                        "INTEGER DEFAULT NULL"
                    )
                )
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "premium_cover_loss_enabled" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN premium_cover_loss_enabled "
                        "BOOLEAN DEFAULT 0"
                    )
                )
    if "trades" in tables:
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "adjustment_count" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE trades ADD COLUMN "
                        "adjustment_count INTEGER DEFAULT 0"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE trades SET adjustment_count = 0 "
                        "WHERE adjustment_count IS NULL"
                    )
                )
    if "slave_accounts" in tables:
        slave_cols = {
            col["name"] for col in inspector.get_columns("slave_accounts")
        }
        if "capital_based_qty" not in slave_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE slave_accounts "
                        "ADD COLUMN capital_based_qty BOOLEAN DEFAULT 0"
                    )
                )
        if "user_allocated_capital" not in slave_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE slave_accounts "
                        "ADD COLUMN user_allocated_capital FLOAT DEFAULT NULL"
                    )
                )
        if "earner_user_id" not in slave_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE slave_accounts "
                        "ADD COLUMN earner_user_id VARCHAR(255) DEFAULT NULL"
                    )
                )
        if "earner_subscription_id" not in slave_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE slave_accounts "
                        "ADD COLUMN earner_subscription_id "
                        "VARCHAR(255) DEFAULT NULL"
                    )
                )
        if "is_virtual" not in slave_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE slave_accounts "
                        "ADD COLUMN is_virtual BOOLEAN DEFAULT 0"
                    )
                )
    # Demo / virtual master trades
    if "auto_trade_settings" in tables:
        at_cols = {
            col["name"] for col in inspector.get_columns("auto_trade_settings")
        }
        if "is_demo" not in at_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE auto_trade_settings "
                        "ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
    if "trades" in tables:
        trade_cols = {col["name"] for col in inspector.get_columns("trades")}
        if "is_demo" not in trade_cols:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE trades "
                        "ADD COLUMN is_demo BOOLEAN NOT NULL DEFAULT 0"
                    )
                )


def get_usd_inr_rate(db: Session) -> float:
    """Return configured USD→INR rate from global auto_trade_settings row."""
    settings = get_or_create_auto_settings(db)
    return float(settings.usd_inr_rate or 85.0)


def init_db() -> None:
    """Import models (register metadata) and create all tables."""
    import backend.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_schema()


def get_or_create_auto_settings(db: Session):
    """Return singleton AutoTradeSettings row (id=1), creating defaults if missing."""
    from backend.models import AutoTradeSettings

    settings = (
        db.query(AutoTradeSettings).filter(AutoTradeSettings.id == 1).first()
    )
    if settings is None:
        settings = AutoTradeSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def get_active_slave_accounts(db: Session) -> list:
    """Return all slave accounts currently set to mirror trades."""
    from backend.models import SlaveAccount

    return (
        db.query(SlaveAccount)
        .filter(SlaveAccount.is_active.is_(True))
        .order_by(SlaveAccount.id.asc())
        .all()
    )


if __name__ == "__main__":
    # Re-import via package so models bind to the same Base (not __main__.Base)
    import backend.database as db

    db.init_db()
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print(f"Tables created: {tables}")
    assert "accounts" in tables
    assert "trades" in tables
    assert "legs" in tables
    assert "adjustments" in tables
    assert "settings" in tables
    assert "auto_trade_settings" in tables
    assert "slave_accounts" in tables
    assert "slave_trades" in tables
    print("✅ DATABASE TEST PASSED")
