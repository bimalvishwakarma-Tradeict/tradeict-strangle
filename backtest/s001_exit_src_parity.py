#!/usr/bin/env python3
"""Compare --new vs --old S001 mark-engine CSVs on P&L identity fields.

Does not re-run the engine. Extra tagging columns on --new are ignored.

    python backtest\\s001_exit_src_parity.py --new new.csv --old old.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


TOL = 1e-9
STR_KEYS = ("date", "exit_ts", "exit_reason")
NUM_KEYS = ("gross_pnl", "net_pnl")


def _load(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(x: str | None) -> float:
    if x is None or x == "":
        return float("nan")
    return float(x)


def main() -> int:
    ap = argparse.ArgumentParser(description="S001 exit-src P&L parity")
    ap.add_argument("--new", required=True)
    ap.add_argument("--old", required=True)
    args = ap.parse_args()
    new_rows = _load(Path(args.new))
    old_rows = _load(Path(args.old))
    mismatches: list[str] = []
    if len(new_rows) != len(old_rows):
        mismatches.append(f"row_count new={len(new_rows)} old={len(old_rows)}")
    n = min(len(new_rows), len(old_rows))
    for i in range(n):
        a = new_rows[i]
        b = old_rows[i]
        row = i + 2  # header is line 1
        for k in STR_KEYS:
            if str(a.get(k, "")) != str(b.get(k, "")):
                mismatches.append(
                    f"row {row} {k}: new={a.get(k)!r} old={b.get(k)!r}"
                )
        for k in NUM_KEYS:
            na = _num(a.get(k))
            nb = _num(b.get(k))
            if abs(na - nb) > TOL:
                mismatches.append(
                    f"row {row} {k}: new={na} old={nb} delta={na - nb}"
                )
    if mismatches:
        print("FAIL")
        for line in mismatches:
            print(line)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
