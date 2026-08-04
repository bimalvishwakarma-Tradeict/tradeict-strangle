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
                # Backfill sequential basket numbers per account (by id order)
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
    if "legs" in tables:
        leg_cols = {col["name"] for col in inspector.get_columns("legs")}
        if "trigger_premium" not in leg_cols:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE legs ADD COLUMN trigger_premium FLOAT")
                )
                # Backfill: trigger baseline = fill premium for existing rows
                conn.execute(
                    text(
                        "UPDATE legs SET trigger_premium = initial_premium "
                        "WHERE trigger_premium IS NULL"
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


def init_db() -> None:
    """Import models (register metadata) and create all tables."""
    import backend.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_schema()


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
    print("✅ DATABASE TEST PASSED")
