"""S023 trace: hook is number-neutral; auto-pick replay matches trades.csv."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.strategies.s023_obps.sim import simulate_basket  # noqa: E402
from backtest.strategies.s023_obps.test_sim import FakeMkt, _fill_day, _put_chain  # noqa: E402
from backtest.strategies.s023_obps.trace_basket import (  # noqa: E402
    RunContext,
    load_trades,
    pick_auto,
    replay_row,
    totals_vs_trade,
)

DRY_RUN = (
    Path(__file__).resolve().parent / "runs" / "20261016T122944Z_s023_stage1"
)


def test_hook_off_on_same_numbers() -> None:
    d0, d1 = date(2025, 6, 2), date(2025, 6, 3)
    listed = [d0, d1, date(2025, 6, 4)]
    t_ent = to_unix(ist_dt(d0, 10, 0))
    m = FakeMkt()
    px0 = {"lc": 500.0, "lp": 500.0, "sc": 690.0, "sp": 690.0}
    t_end = t_ent + 8 * 60
    _fill_day(m, d0, d1, t_ent, t_end, px0, 100_000.0)
    t_hit = t_ent + 60
    for tt in range(t_hit, t_end + 60, 60):
        px = dict(px0)
        px["lc"] = 710.0
        _put_chain(m, d1, tt, 100_000.0, px)
    ohlc = {tt: (100010.0, 99990.0, 100000.0) for tt in range(t_ent, t_end + 60, 60)}
    a = simulate_basket(m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0)
    log: list = []
    b = simulate_basket(
        m, listed, entry_ts=t_ent, signal_price=1.0, qty=1000, ohlc=ohlc, slip_mult=0.0, event_log=log,
    )
    assert a.ok and b.ok
    assert abs(a.gross - b.gross) < 1e-6
    assert abs(a.fees - b.fees) < 1e-6
    assert abs(a.net - b.net) < 1e-6
    assert abs(a.slip - b.slip) < 1e-6
    assert a.stale_minutes == b.stale_minutes
    assert a.exit_ts == b.exit_ts
    assert any(e["event"] == "ENTRY" for e in log)
    assert any(str(e["event"]).startswith("EXIT_") for e in log)


def test_pick_auto_order() -> None:
    rows = [
        {"cell": "30m|full|ANY", "arm": "F", "entry_ist": "a", "exit_reason": "SL", "step1": "", "step2": "", "redeploys": "0", "legs": ""},
        {"cell": "15m|full|ANY", "arm": "F", "entry_ist": "t", "exit_reason": "TARGET", "step1": "", "step2": "", "redeploys": "0", "legs": ""},
        {"cell": "15m|full|ANY", "arm": "F", "entry_ist": "sl", "exit_reason": "SL", "step1": "", "step2": "", "redeploys": "0", "legs": ""},
        {"cell": "15m|full|ANY", "arm": "F", "entry_ist": "s", "exit_reason": "SIGNAL_PRICE", "step1": "{}", "step2": "", "redeploys": "0", "legs": "C/1/2025-06-11/1/1/main_call"},
        {"cell": "15m|full|ANY", "arm": "F", "entry_ist": "rd", "exit_reason": "EXPIRY", "step1": "{}", "step2": "", "redeploys": "1", "legs": ""},
        {"cell": "15m|full|ANY", "arm": "C2", "entry_ist": "x", "exit_reason": "TARGET", "step1": "", "step2": "", "redeploys": "0", "legs": ""},
    ]
    got = pick_auto(rows)
    assert [r["entry_ist"] for r in got] == ["t", "sl", "s", "rd"]


def test_four_auto_baskets_match_trades_csv() -> None:
    assert DRY_RUN.is_dir(), f"missing dry-run folder {DRY_RUN}"
    rows = load_trades(DRY_RUN)
    picked = pick_auto(rows)
    assert len(picked) == 4
    ctx = RunContext(DRY_RUN)
    for row in picked:
        res, _log, _sig = replay_row(ctx, row, min_prem=0.0, apply_cap=False)
        assert res.ok, res.skip_reason
        tot = totals_vs_trade(res, row)
        for k in ("gross", "fees", "net"):
            assert abs(tot[k]["diff"]) < 1e-6, (row["entry_ist"], k, tot[k])
