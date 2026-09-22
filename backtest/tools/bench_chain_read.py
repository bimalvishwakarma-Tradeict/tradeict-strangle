"""Time cold chain reads (expiry+ts) to size up the index decision."""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parents[1]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.strategies.s008_regime_gate.strategy import (  # noqa: E402
    load_chain_pk,
    load_chain_sql,
    zero_dte_expiry,
)

DAYS = [
    date(2026, 5, 12),
    date(2026, 5, 13),
    date(2026, 6, 10),
    date(2026, 7, 8),
    date(2026, 8, 12),
]


def main() -> int:
    store = MarksStore()
    for d in DAYS:
        conn = store.conn(d)
        if conn is None:
            print(f"{d}: no marks file")
            continue
        ts = to_unix(ist_dt(d, 9, 0))
        exp = zero_dte_expiry(d)
        t0 = time.monotonic()
        calls, puts = load_chain_sql(conn, exp, ts)
        t_sql = time.monotonic() - t0
        t0 = time.monotonic()
        c2, p2 = load_chain_pk(conn, exp, ts, 100_000.0)
        t_pk = time.monotonic() - t0
        print(
            f"{d} expiry={exp} sql={t_sql:7.2f}s (c={len(calls)} p={len(puts)}) "
            f"pk={t_pk:6.2f}s (c={len(c2)} p={len(p2)})",
            flush=True,
        )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
