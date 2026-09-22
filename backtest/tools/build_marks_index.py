#!/usr/bin/env python3
"""One-time: add idx_marks_expiry_ts ON marks(expiry, ts) to every marks file.

WHY
    The files already carry idx_marks_expiry ON marks(expiry). For a chain read
    (`WHERE expiry=? AND ts=?`) SQLite seeks that index on expiry, then does a
    rowid lookup for EVERY row of that expiry just to test `ts`. A daily expiry
    holds hundreds of strikes x its whole quoted life, so a single chain read
    touches ~1M random rows in a ~2.3 GB table: low CPU, saturated disk.
    A composite (expiry, ts) index makes the same read a direct range seek.

SAFETY
    This is the ONLY script that writes to the marks files; the backtest opens
    them read-only. Adding an index does not change any stored row, but it does
    rewrite file structure, so BACK UP FIRST (or be ready to re-download):

        robocopy "backtest\\cache\\option_marks" "E:\\marks_backup" /MIR

    Run with no backtest process holding the files open. Each file grows by
    roughly 10-15% (index pages); budget ~8 GB free across all 25 files.
    Re-running is safe: CREATE INDEX IF NOT EXISTS + skip when already present.

USAGE
    python -m backtest.tools.build_marks_index --dry-run
    python -m backtest.tools.build_marks_index --yes
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parents[1]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MARKS_DIR  # noqa: E402

INDEX_NAME = "idx_marks_expiry_ts"
INDEX_SQL = f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON marks(expiry, ts)"


def has_index(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row is not None


def main() -> int:
    ap = argparse.ArgumentParser(description="Add (expiry, ts) index to marks files")
    ap.add_argument("--marks-dir", type=str, default=str(MARKS_DIR))
    ap.add_argument("--dry-run", action="store_true", help="report only, no writes")
    ap.add_argument(
        "--yes",
        action="store_true",
        help="required to actually write (confirms backup exists)",
    )
    args = ap.parse_args()

    marks_dir = Path(args.marks_dir)
    files = sorted(marks_dir.glob("marks_*.sqlite"))
    if not files:
        print(f"no marks files in {marks_dir}")
        return 1
    if not args.dry_run and not args.yes:
        print("refusing to write without --yes (back up the marks folder first)")
        return 2

    print(f"marks_dir={marks_dir} files={len(files)} dry_run={args.dry_run}")
    t_all = time.monotonic()
    for i, p in enumerate(files, 1):
        size_before = p.stat().st_size / 1e6
        con = sqlite3.connect(p)
        try:
            if has_index(con, INDEX_NAME):
                print(f"[{i}/{len(files)}] {p.name} already has {INDEX_NAME} — skip")
                continue
            if args.dry_run:
                print(f"[{i}/{len(files)}] {p.name} WOULD create {INDEX_NAME}")
                continue
            t0 = time.monotonic()
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            con.execute("PRAGMA cache_size=-524288")  # ~512 MB page cache
            con.execute(INDEX_SQL)
            con.commit()
            con.execute("ANALYZE")
            con.commit()
            secs = time.monotonic() - t0
            size_after = p.stat().st_size / 1e6
            print(
                f"[{i}/{len(files)}] {p.name} built in {secs:7.1f}s "
                f"{size_before:.0f}MB -> {size_after:.0f}MB",
                flush=True,
            )
        finally:
            con.close()
    print(f"total={time.monotonic() - t_all:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
