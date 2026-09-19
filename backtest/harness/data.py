"""Mark + spot data access for the harness (read-only sqlite)."""

from __future__ import annotations

import csv
import logging
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.config import MARKS_DIR, DATA_1M_DIR, MARK_TOL_SEC, STALE_BAR_SEC

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("harness.data")


def ist_dt(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


class MarksStore:
    """Read-only marks_YYYY-MM.sqlite store."""

    def __init__(self, marks_dir: Path | None = None) -> None:
        self.marks_dir = marks_dir or MARKS_DIR
        self._conns: dict[str, sqlite3.Connection] = {}
        self.available_months: list[str] = sorted(
            p.stem.replace("marks_", "")
            for p in self.marks_dir.glob("marks_*.sqlite")
        )

    def conn(self, d: date) -> sqlite3.Connection | None:
        ym = f"{d.year:04d}-{d.month:02d}"
        if ym not in self.available_months:
            return None
        if ym not in self._conns:
            path = self.marks_dir / f"marks_{ym}.sqlite"
            self._conns[ym] = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro", uri=True
            )
        return self._conns[ym]

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()


def find_spot_csv(data_dir: Path | None = None) -> Path | None:
    d = data_dir or DATA_1M_DIR
    files = sorted(d.glob("BTCUSD_1m_*.csv"))
    return files[-1] if files else None


def load_spot_map(path: Path) -> dict[int, float]:
    out: dict[int, float] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[int(row["open_time_unix"])] = float(row["close"])
    return out


def resolve_mark_ts(
    conn: sqlite3.Connection,
    expiry: date,
    ts: int,
    tol_sec: int = MARK_TOL_SEC,
) -> int | None:
    minute = (ts // 60) * 60
    exp = expiry.isoformat()
    row = conn.execute(
        "SELECT ts FROM marks WHERE expiry=? AND ts=? LIMIT 1",
        (exp, minute),
    ).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.execute(
        """
        SELECT ts FROM marks
        WHERE expiry=? AND ts BETWEEN ? AND ?
        ORDER BY ABS(ts - ?) LIMIT 1
        """,
        (exp, minute - tol_sec, minute + tol_sec, minute),
    ).fetchone()
    return int(row[0]) if row is not None else None


def load_chain(
    conn: sqlite3.Connection, expiry: date, chain_ts: int, opt_type: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT symbol, strike, close FROM marks
        WHERE expiry=? AND ts=? AND opt_type=?
          AND close IS NOT NULL AND close > 0
        """,
        (expiry.isoformat(), chain_ts, opt_type),
    ).fetchall()
    return [
        {
            "symbol": str(s),
            "strike": float(k),
            "mark_price": float(c),
            "mark": float(c),
            "premium": float(c),
            "option_type": opt_type,
        }
        for s, k, c in rows
    ]


def mark_ohlc_at(
    conn: sqlite3.Connection,
    symbol: str,
    ts: int,
    tol_sec: int = MARK_TOL_SEC,
) -> dict[str, float] | None:
    """Nearest bar OHLC within tol. Stale if |ts_found - ts| > STALE_BAR_SEC."""
    minute = (ts // 60) * 60
    row = conn.execute(
        "SELECT ts, open, high, low, close FROM marks WHERE symbol=? AND ts=?",
        (symbol, minute),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT ts, open, high, low, close FROM marks
            WHERE symbol=? AND ts BETWEEN ? AND ?
            ORDER BY ABS(ts - ?) LIMIT 1
            """,
            (symbol, minute - tol_sec, minute + tol_sec, minute),
        ).fetchone()
    if row is None:
        return None
    found_ts, o, h, l, c = row
    if abs(int(found_ts) - minute) > STALE_BAR_SEC:
        logger.debug("stale bar symbol=%s want=%s got=%s", symbol, minute, found_ts)
        return None
    if c is None or float(c) <= 0:
        return None
    return {
        "ts": float(found_ts),
        "open": float(o or c),
        "high": float(h or c),
        "low": float(l or c),
        "close": float(c),
    }


def put_call_parity_forward(
    conn: sqlite3.Connection, expiry: date, chain_ts: int
) -> float | None:
    calls = {
        float(k): float(c)
        for k, c in conn.execute(
            "SELECT strike, close FROM marks WHERE expiry=? AND ts=? AND opt_type='call' AND close>0",
            (expiry.isoformat(), chain_ts),
        )
    }
    puts = {
        float(k): float(c)
        for k, c in conn.execute(
            "SELECT strike, close FROM marks WHERE expiry=? AND ts=? AND opt_type='put' AND close>0",
            (expiry.isoformat(), chain_ts),
        )
    }
    vals: list[float] = []
    for k, c in calls.items():
        p = puts.get(k)
        if p is None:
            continue
        vals.append(k + c - p)
    if not vals:
        return None
    vals.sort()
    return float(vals[len(vals) // 2])


def resolve_forward(
    store: MarksStore,
    spot_map: dict[int, float],
    expiry: date,
    ts: int,
    tol_sec: int = MARK_TOL_SEC,
) -> tuple[float | None, str]:
    minute = (ts // 60) * 60
    if minute in spot_map:
        return spot_map[minute], "spot_1m"
    for d in range(-tol_sec, tol_sec + 1, 60):
        if minute + d in spot_map:
            return spot_map[minute + d], "spot_1m_near"
    day = datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date()
    conn = store.conn(day)
    if conn is None:
        return None, "none"
    cts = resolve_mark_ts(conn, expiry, ts, tol_sec)
    if cts is None:
        return None, "none"
    f = put_call_parity_forward(conn, expiry, cts)
    if f is None or f <= 0:
        return None, "none"
    return f, "put_call_parity"


class MarketContext:
    """Passed into strategy build/manage."""

    def __init__(
        self,
        *,
        store: MarksStore,
        spot_map: dict[int, float],
        day: date,
        expiry: date,
        spot: float,
        params: dict[str, Any],
    ) -> None:
        self.store = store
        self.spot_map = spot_map
        self.day = day
        self.expiry = expiry
        self.spot = spot
        self.params = params
        self.skip_reason: str | None = None

    def chain(self, ts: int, opt_type: str) -> list[dict[str, Any]]:
        conn = self.store.conn(self.day)
        if conn is None:
            return []
        cts = resolve_mark_ts(conn, self.expiry, ts)
        if cts is None:
            return []
        return load_chain(conn, self.expiry, cts, opt_type)
