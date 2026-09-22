"""One-time probe: report table DDL + existing indexes on every marks file."""

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


def main() -> int:
    files = sorted(MARKS_DIR.glob("marks_*.sqlite"))
    print(f"marks_dir={MARKS_DIR}")
    print(f"files={len(files)}")
    total_mb = 0.0
    for i, p in enumerate(files):
        mb = p.stat().st_size / 1e6
        total_mb += mb
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            tables = [
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
            idx = list(
                con.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='index'"
                )
            )
            rows = con.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
            if i == 0:
                ddl = con.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='marks'"
                ).fetchone()
                print("marks DDL:")
                print(ddl[0] if ddl else "  <none>")
                plan = list(
                    con.execute(
                        "EXPLAIN QUERY PLAN SELECT symbol, strike, close "
                        "FROM marks WHERE expiry=? AND ts=?",
                        ("2025-11-05", 0),
                    )
                )
                print(f"query plan (expiry,ts): {plan}")
            print(
                f"{p.name} {mb:8.1f}MB rows={rows:>9} tables={tables} "
                f"indexes={[(n, s) for n, s in idx]}"
            )
        finally:
            con.close()
    print(f"total={total_mb:.1f}MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
