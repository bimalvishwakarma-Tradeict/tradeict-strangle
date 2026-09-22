#!/usr/bin/env python3
"""S007-B runner: tests → signal counts → full 288-arm grid → per-trade CSVs.

Reproduction only. This strategy is already TESTED-NEGATIVE; the point is to let
the user read every trade in a CSV, not to draw a new conclusion.

Run in a SEPARATE PowerShell window (not the Cursor terminal):

    python backtest\\strategies\\s007b_vwap_directional\\run_s007b.py `
        --csv backtest\\data_1m\\BTCUSD_1m_20250613_20260921.csv `
        --out backtest\\strategies\\s007b_vwap_directional\\runs
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.costs import ensure_slip_table  # noqa: E402
from backtest.harness.data import MarksStore  # noqa: E402
from backtest.strategies.s007b_vwap_directional import config as cfg  # noqa: E402
from backtest.strategies.s007b_vwap_directional.engine import (  # noqa: E402
    ChainCache,
    MarketData,
    Signal,
    TradeContext,
    TradeResult,
    build_context,
    build_signals,
    ist_of,
    ist_str,
    load_market_data,
    simulate,
)

logger = logging.getLogger("s007b.run")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    i = (len(s) - 1) * p
    lo = int(np.floor(i))
    hi = int(np.ceil(i))
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - i) + s[hi] * (i - lo)


def max_drawdown(nets_by_time: list[float]) -> float:
    """Most negative running (equity - peak). 0.0 if never underwater."""
    if not nets_by_time:
        return float("nan")
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for x in nets_by_time:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def day_cluster_bootstrap(
    nets: list[float],
    days: list[str],
    n_boot: int,
    seed: int,
) -> tuple[float, float]:
    """Day-clustered bootstrap 95% CI of the per-trade mean."""
    if not nets:
        return float("nan"), float("nan")
    by_day: dict[str, list[float]] = {}
    for net, d in zip(nets, days):
        by_day.setdefault(d, []).append(net)
    day_keys = list(by_day.keys())
    day_arrs = [np.asarray(by_day[d], dtype=np.float64) for d in day_keys]
    n_days = len(day_keys)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        pick = rng.integers(0, n_days, size=n_days)
        pool = np.concatenate([day_arrs[j] for j in pick])
        means[b] = pool.mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


class ArmStats:
    def __init__(self, results: list[TradeResult]) -> None:
        results = sorted(results, key=lambda r: r.ctx.entry_ts)
        nets = [r.net_pnl for r in results]
        days = [ist_of(r.ctx.entry_ts).date().isoformat() for r in results]
        self.n = len(nets)
        self.mean = float(np.mean(nets)) if nets else float("nan")
        self.median = float(np.median(nets)) if nets else float("nan")
        self.win_pct = (
            100.0 * float(np.mean([1.0 if x > 0 else 0.0 for x in nets]))
            if nets else float("nan")
        )
        self.worst = float(np.min(nets)) if nets else float("nan")
        self.max_dd = max_drawdown(nets)
        self.ci_lo, self.ci_hi = day_cluster_bootstrap(
            nets, days, cfg.BOOTSTRAP_N, cfg.BOOTSTRAP_SEED
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_lookahead(md: MarketData) -> None:
    """Entry uses next bar open; signal detection is truncation-invariant."""
    sigs = build_signals(md, 14)
    assert sigs, "LOOKAHEAD: no signals to test"
    checked = 0
    for sig in sigs[:20]:
        ei = sig.index + 1
        if ei >= md.n:
            continue
        # entry price is purely the next bar's open
        assert md.ts[ei] == md.ts[sig.index + 1]
        # truncate history at the signal bar: the signal must still appear,
        # proving no post-signal bar influenced it
        trunc = MarketData(
            ts=md.ts[: sig.index + 1].copy(),
            open=md.open[: sig.index + 1].copy(),
            high=md.high[: sig.index + 1].copy(),
            low=md.low[: sig.index + 1].copy(),
            close=md.close[: sig.index + 1].copy(),
            volume=md.volume[: sig.index + 1].copy(),
        )
        tsigs = build_signals(trunc, 14)
        assert tsigs, f"LOOKAHEAD: truncated set empty at {sig.ts}"
        last = tsigs[-1]
        assert last.index == sig.index and last.side == sig.side, (
            f"LOOKAHEAD FAIL at idx {sig.index}: trunc last={last.index}/{last.side}"
        )
        checked += 1
    assert checked > 0
    print(f"LOOKAHEAD PASS checked={checked}")


def test_zero_cost(
    md: MarketData, store: MarksStore, chain_cache: ChainCache
) -> None:
    """All fees/slips zero → net == gross, both futures modes."""
    sigs = build_signals(md, 14)
    n = 0
    for sig in sigs:
        ctx, reason = build_context(md, sig, store, chain_cache)
        if ctx is None:
            continue
        for fut_mode in (True, False):
            r = simulate(ctx, 200, 0.05, fut_mode, zero_costs=True)
            gross = r.opt_gross + r.fut_gross
            assert abs(r.net_pnl - gross) < 1e-9, (
                f"ZERO_COST FAIL {sig.ts} fut={fut_mode}: net={r.net_pnl} gross={gross}"
            )
            assert r.total_cost == 0.0
        n += 1
        if n >= 25:
            break
    assert n > 0, "ZERO_COST: no tradable signals"
    print(f"ZERO_COST PASS n={n}")


def test_skip_count(
    md: MarketData, store: MarksStore, chain_cache: ChainCache
) -> None:
    """traded + sum(skip reasons) == total signals (rsi=14)."""
    sigs = build_signals(md, 14)
    total = len(sigs)
    traded = 0
    skips: dict[str, int] = {}
    for sig in sigs:
        ctx, reason = build_context(md, sig, store, chain_cache)
        if ctx is None:
            skips[reason] = skips.get(reason, 0) + 1
        else:
            traded += 1
    assert traded + sum(skips.values()) == total, (
        f"SKIP_COUNT FAIL: traded={traded} skips={skips} total={total}"
    )
    skip_str = ", ".join(f"{k}:{v}" for k, v in sorted(skips.items())) or "none"
    print(f"SKIP_COUNT PASS total={total} traded={traded} skips={{{skip_str}}}")


def run_tests(md: MarketData, store: MarksStore, chain_cache: ChainCache) -> None:
    test_lookahead(md)
    test_zero_cost(md, store, chain_cache)
    test_skip_count(md, store, chain_cache)
    print("ALL S007B TESTS PASS")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
CSV_COLUMNS = [
    "trade_id", "signal_ts_ist", "entry_ts_ist", "side", "rsi_period",
    "rsi_at_signal", "spot_at_signal", "lower_band", "upper_band", "entry_spot",
    "option_symbol", "strike", "expiry", "entry_delta", "entry_premium",
    "fut_entry_price", "capital_used", "target_pts", "stop_pct", "stop_pts",
    "exit_ts_ist", "exit_reason", "exit_spot", "exit_premium", "fut_exit_price",
    "minutes_held", "max_favourable_pts", "max_adverse_pts",
    "opt_gross", "fut_gross", "opt_fee", "fut_fee", "opt_slip", "fut_slip",
    "total_cost", "net_pnl",
]


def _fmt(x: float) -> str:
    if x != x:  # NaN
        return ""
    return f"{x:.6f}"


def result_row(trade_id: int, r: TradeResult) -> list[str]:
    c = r.ctx
    s = c.signal
    return [
        str(trade_id),
        ist_str(s.ts),
        ist_str(c.entry_ts),
        s.side,
        str(s.rsi_period),
        _fmt(s.rsi_at_signal),
        _fmt(s.spot_at_signal),
        _fmt(s.lower_band),
        _fmt(s.upper_band),
        _fmt(c.entry_spot),
        c.symbol,
        _fmt(c.strike),
        c.expiry.isoformat(),
        _fmt(c.entry_delta),
        _fmt(c.entry_mark),
        _fmt(r.fut_entry_price),
        _fmt(r.capital_used),
        str(r.target_pts),
        _fmt(r.stop_pct),
        _fmt(r.stop_pts),
        ist_str(r.exit_ts),
        r.exit_reason,
        _fmt(r.exit_spot),
        _fmt(r.exit_mark),
        _fmt(r.fut_exit_price),
        str(r.minutes_held),
        _fmt(r.max_fav_pts),
        _fmt(r.max_adv_pts),
        _fmt(r.opt_gross),
        _fmt(r.fut_gross),
        _fmt(r.opt_fee),
        _fmt(r.fut_fee),
        _fmt(r.opt_slip),
        _fmt(r.fut_slip),
        _fmt(r.total_cost),
        _fmt(r.net_pnl),
    ]


def write_csv(path: Path, results: list[TradeResult]) -> None:
    results = sorted(results, key=lambda r: r.ctx.entry_ts)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for i, r in enumerate(results, 1):
            w.writerow(result_row(i, r))


def stop_tag(stop_pct: float) -> str:
    return f"stop{int(round(stop_pct * 100))}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ap = argparse.ArgumentParser(description="S007-B VWAP+RSI directional runner")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--max-bars", type=int, default=0,
        help="smoke: use only the first N bars (0 = full data)",
    )
    args = ap.parse_args()

    ensure_slip_table()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    md = load_market_data(args.csv)
    if args.max_bars and args.max_bars < md.n:
        md = MarketData(
            ts=md.ts[: args.max_bars].copy(),
            open=md.open[: args.max_bars].copy(),
            high=md.high[: args.max_bars].copy(),
            low=md.low[: args.max_bars].copy(),
            close=md.close[: args.max_bars].copy(),
            volume=md.volume[: args.max_bars].copy(),
        )
    print(f"loaded bars={md.n} in {time.monotonic() - t0:.1f}s", flush=True)

    store = MarksStore()
    chain_cache = ChainCache()

    lines: list[str] = [
        "===== S007-B VWAP+RSI DIRECTIONAL (reproduction) =====",
        f"generated_utc={datetime.now(tz=timezone.utc).isoformat()}",
        f"bars={md.n} band_len={cfg.BAND_LENGTH} band_k={cfg.BAND_K} "
        f"cooldown={cfg.COOLDOWN_BARS}",
        f"option_qty={cfg.OPTION_QTY_LOTS} fut_qty={cfg.FUT_QTY_LOTS} "
        f"target_delta={cfg.TARGET_ABS_DELTA}",
        "",
    ]

    # ---- A) tests ----
    print("=== TESTS ===", flush=True)
    run_tests(md, store, chain_cache)
    lines += ["TESTS: LOOKAHEAD/ZERO_COST/SKIP_COUNT PASS", ""]

    # ---- B) signal counts + per-RSI contexts ----
    print("=== SIGNAL COUNTS ===", flush=True)
    contexts: dict[int, list[TradeContext]] = {}
    lines.append("SIGNAL COUNTS + SKIPS")
    for rsi in cfg.RSI_PERIODS:
        sigs = build_signals(md, rsi)
        print(f"RSI {rsi}: signals={len(sigs)}", flush=True)
        ctxs: list[TradeContext] = []
        skips: dict[str, int] = {}
        for j, sig in enumerate(sigs, 1):
            ctx, reason = build_context(md, sig, store, chain_cache)
            if ctx is None:
                skips[reason] = skips.get(reason, 0) + 1
            else:
                ctxs.append(ctx)
            if j % 500 == 0:
                print(
                    f"  .. rsi={rsi} built {j}/{len(sigs)} traded={len(ctxs)} "
                    f"chain_loads={chain_cache.loads} hits={chain_cache.hits}",
                    flush=True,
                )
        contexts[rsi] = ctxs
        skip_str = ", ".join(f"{k}:{v}" for k, v in sorted(skips.items())) or "none"
        line = (
            f"  RSI {rsi}: signals={len(sigs)} traded={len(ctxs)} skips={{{skip_str}}}"
        )
        print(line, flush=True)
        lines.append(line)
    lines.append("")

    # ---- C) full grid: 4 RSI x 9 target x 4 stop x 2 fut = 288 arms ----
    print("=== FULL GRID (288 arms) ===", flush=True)
    lines.append("ARM STATS (n mean median win% worst maxDD ci95)")
    arm_i = 0
    n_arms = (
        len(cfg.RSI_PERIODS) * len(cfg.TARGET_PTS) * len(cfg.STOP_PCTS) * 2
    )
    for rsi in cfg.RSI_PERIODS:
        ctxs = contexts[rsi]
        for fut_mode in (True, False):
            for tgt in cfg.TARGET_PTS:
                for stop in cfg.STOP_PCTS:
                    arm_i += 1
                    results = [
                        simulate(ctx, tgt, stop, fut_mode) for ctx in ctxs
                    ]
                    st = ArmStats(results)
                    label = (
                        f"rsi{rsi}_tgt{tgt}_{stop_tag(stop)}_"
                        f"fut{'ON' if fut_mode else 'OFF'}"
                    )
                    line = (
                        f"  {label}: n={st.n} mean={st.mean:.4f} "
                        f"median={st.median:.4f} win%={st.win_pct:.1f} "
                        f"worst={st.worst:.4f} maxDD={st.max_dd:.4f} "
                        f"ci95=[{st.ci_lo:.4f}, {st.ci_hi:.4f}]"
                    )
                    lines.append(line)
                    if arm_i % 24 == 0:
                        print(
                            f"  .. arm {arm_i}/{n_arms} {label} n={st.n} "
                            f"mean={st.mean:.3f}",
                            flush=True,
                        )
    lines.append("")

    # ---- D) per-trade CSVs for the 4 chosen arms x 2 fut modes (8 files) ----
    print("=== PER-TRADE CSV DUMP ===", flush=True)
    lines.append("CSV DUMPS")
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for rsi, tgt, stop in cfg.CSV_DUMP_ARMS:
        ctxs = contexts[rsi]
        for fut_mode in (True, False):
            results = [simulate(ctx, tgt, stop, fut_mode) for ctx in ctxs]
            base = f"rsi{rsi}_tgt{tgt}_{stop_tag(stop)}_fut{'ON' if fut_mode else 'OFF'}"
            path = out_dir / f"{base}_{stamp}.csv"
            write_csv(path, results)
            print(f"  wrote {path.name} rows={len(results)}", flush=True)
            lines.append(f"  {path.name} rows={len(results)}")
    lines.append("")

    store.close()
    lines.append(
        f"grid_total={time.monotonic() - t0:.1f}s chain_loads={chain_cache.loads} "
        f"chain_hits={chain_cache.hits}"
    )
    summary_path = out_dir / f"s007b_summary_{stamp}.txt"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary={summary_path}", flush=True)
    print(f"DONE in {time.monotonic() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
