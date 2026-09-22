"""How many rows one chain read has to touch under the (expiry)-only index."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parents[1]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MARKS_DIR  # noqa: E402

PROBES = [
    ("marks_2026-05.sqlite", "2026-05-12"),
    ("marks_2025-11.sqlite", "2025-11-05"),
]


def main() -> int:
    for fname, expiry in PROBES:
        p = MARKS_DIR / fname
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            n_exp = con.execute(
                "SELECT COUNT(*) FROM marks WHERE expiry=?", (expiry,)
            ).fetchone()[0]
            n_min = con.execute(
                "SELECT COUNT(*) FROM marks WHERE expiry=? AND ts=?",
                (expiry, 1778992200),
            ).fetchone()[0]
            plan = list(
                con.execute(
                    "EXPLAIN QUERY PLAN SELECT symbol, strike, close FROM marks "
                    "WHERE expiry=? AND ts=? AND opt_type=?",
                    (expiry, 0, "call"),
                )
            )
            print(
                f"{fname} expiry={expiry}: rows_for_expiry={n_exp:,} "
                f"rows_at_one_minute={n_min}"
            )
            print(f"  plan={plan}")
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
