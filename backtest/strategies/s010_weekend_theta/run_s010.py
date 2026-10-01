#!/usr/bin/env python3
"""S010 weekend-theta runner: tests then 28-arm grid + per-basket CSVs.

Arm A = D+3 calendar protection. Arm B = same-expiry control (full re-run).
Filter DATA_START = 2025-01-01. Full grid belongs in a separate PowerShell.

    python backtest\\strategies\\s010_weekend_theta\\run_s010.py `
        --csv backtest\\data_1m\\BTCUSD_1m_20240630_20260921.csv `
        --out backtest\\strategies\\s010_weekend_theta\\runs
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import MarksStore  # noqa: E402
from backtest.strategies.s010_weekend_theta import config as cfg  # noqa: E402
from backtest.strategies.s010_weekend_theta.engine import (  # noqa: E402
    BasketResult,
    candidate_dates,
    ist_str,
    load_spot_ohlc,
    simulate_day,
)
from backtest.strategies.s010_weekend_theta.preflight import ChainCache  # noqa: E402

logger = logging.getLogger("s010.run")

CSV_COLUMNS = [
    "date",
    "dow",
    "entry_ts",
    "spot_entry",
    "atm_strike",
    "straddle_premium",
    "upper_be",
    "lower_be",
    "short_call_k",
    "short_call_prem",
    "short_put_k",
    "short_put_prem",
    "prot_expiry",
    "prot_strike",
    "prot_call_prem",
    "prot_put_prem",
    "capital_used",
    "target_usd",
    "stop_usd",
    "exit_ts",
    "exit_reason",
    "spot_exit",
    "gross_pnl",
    "fees",
    "slippage",
    "net_pnl",
    "worst_intracycle_mtm",
]


def _fmt(x: float) -> str:
    if isinstance(x, float) and math.isnan(x):
        return ""
    return f"{x:.8f}"


def write_basket_csv(path: Path, rows: list[BasketResult]) -> None:
    rows = sorted(rows, key=lambda r: r.entry_ts)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow(
                [
                    r.d.isoformat(),
                    r.dow,
                    r.entry_ts,
                    _fmt(r.spot_entry),
                    _fmt(r.atm_strike),
                    _fmt(r.straddle_premium),
                    _fmt(r.upper_be),
                    _fmt(r.lower_be),
                    _fmt(r.short_call_k),
                    _fmt(r.short_call_prem),
                    _fmt(r.short_put_k),
                    _fmt(r.short_put_prem),
                    r.prot_expiry.isoformat(),
                    _fmt(r.prot_strike),
                    _fmt(r.prot_call_prem),
                    _fmt(r.prot_put_prem),
                    _fmt(r.capital_used),
                    _fmt(r.target_usd),
                    _fmt(r.stop_usd),
                    r.exit_ts,
                    r.exit_reason,
                    _fmt(r.spot_exit),
                    _fmt(r.gross_pnl),
                    _fmt(r.fees),
                    _fmt(r.slippage),
                    _fmt(r.net_pnl),
                    _fmt(r.worst_intracycle_mtm),
                ]
            )


def max_drawdown(nets: list[float]) -> float:
    if not nets:
        return float("nan")
    eq = 0.0
    peak = 0.0
    mdd = 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def day_cluster_bootstrap(
    nets: list[float], days: list[str], n_boot: int, seed: int
) -> tuple[float, float]:
    if not nets:
        return float("nan"), float("nan")
    by_day: dict[str, list[float]] = {}
    for net, d in zip(nets, days):
        by_day.setdefault(d, []).append(net)
    keys = list(by_day.keys())
    arrs = [np.asarray(by_day[k], dtype=np.float64) for k in keys]
    n_days = len(keys)
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        pick = rng.integers(0, n_days, size=n_days)
        pool = np.concatenate([arrs[j] for j in pick])
        means[b] = pool.mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


class ArmStats:
    def __init__(self, results: list[BasketResult]) -> None:
        results = sorted(results, key=lambda r: r.entry_ts)
        nets = [r.net_pnl for r in results]
        gross = [r.gross_pnl for r in results]
        days = [r.d.isoformat() for r in results]
        self.n = len(nets)
        self.mean = float(np.mean(nets)) if nets else float("nan")
        self.median = float(np.median(nets)) if nets else float("nan")
        self.win_pct = (
            100.0 * float(np.mean([1.0 if x > 0 else 0.0 for x in nets]))
            if nets
            else float("nan")
        )
        self.worst = float(np.min(nets)) if nets else float("nan")
        self.max_dd = max_drawdown(nets)
        self.mean_gross = float(np.mean(gross)) if gross else float("nan")
        self.ci_lo, self.ci_hi = day_cluster_bootstrap(
            nets, days, cfg.BOOTSTRAP_N, cfg.BOOTSTRAP_SEED
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_no_lookahead(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    from backtest.harness.data import ist_dt, to_unix

    n = 0
    for d in dates:
        res, _reason = simulate_day(
            d=d, spot=spot, store=store, chain_cache=cache, arm="A", ts_on=False
        )
        if res is None:
            continue
        want = to_unix(ist_dt(d, cfg.ENTRY_HOUR_IST, cfg.ENTRY_MINUTE_IST))
        assert res.entry_ts == want, (
            f"NO_LOOKAHEAD FAIL {d}: entry_ts={res.entry_ts} want={want}"
        )
        n += 1
        if n >= 8:
            break
    assert n > 0
    print(f"NO_LOOKAHEAD PASS checked={n}")


def test_zero_cost(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    n = 0
    for d in dates:
        for arm in ("A", "B"):
            res, _ = simulate_day(
                d=d,
                spot=spot,
                store=store,
                chain_cache=cache,
                arm=arm,  # type: ignore[arg-type]
                ts_on=False,
                zero_costs=True,
            )
            if res is None:
                continue
            assert abs(res.net_pnl - res.gross_pnl) < 1e-9, (
                f"ZERO_COST FAIL {d} {arm}: net={res.net_pnl} gross={res.gross_pnl}"
            )
            assert res.fees == 0.0 and res.slippage == 0.0
            n += 1
        if n >= 10:
            break
    assert n > 0
    print(f"ZERO_COST PASS n={n}")


def test_expiry_free(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    n = 0
    for d in dates:
        for arm in ("A", "B"):
            res, _ = simulate_day(
                d=d,
                spot=spot,
                store=store,
                chain_cache=cache,
                arm=arm,  # type: ignore[arg-type]
                ts_on=False,
            )
            if res is None or res.exit_reason != "EXPIRY":
                continue
            assert res.d2_exit_fee == 0.0 and res.d2_exit_slip == 0.0, (
                f"EXPIRY_FREE FAIL {d} {arm}: "
                f"d2_fee={res.d2_exit_fee} d2_slip={res.d2_exit_slip}"
            )
            if arm == "B":
                # Every leg expires — no exit cost at all.
                assert res.fees == res.entry_fees
            n += 1
        if n >= 10:
            break
    assert n > 0
    print(f"EXPIRY_FREE PASS n={n}")


def test_arm_b_full(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    n = 0
    for d in dates:
        a, ra = simulate_day(
            d=d, spot=spot, store=store, chain_cache=cache, arm="A", ts_on=False
        )
        b, rb = simulate_day(
            d=d, spot=spot, store=store, chain_cache=cache, arm="B", ts_on=False
        )
        if a is None or b is None:
            continue
        assert b.prot_expiry == d + timedelta(days=2), (
            f"ARM_B_FULL FAIL {d}: prot_expiry={b.prot_expiry}"
        )
        assert a.prot_expiry == d + timedelta(days=3)
        assert b.prot_expiry_is_d2 is True
        # Own entry cost — not a subtraction from A.
        assert b.entry_fees > 0.0
        assert abs(b.entry_fees - a.entry_fees) > 1e-12 or abs(
            b.net_pnl - a.net_pnl
        ) > 1e-12
        n += 1
        if n >= 8:
            break
    assert n > 0
    print(f"ARM_B_FULL PASS n={n}")


def test_skip_count(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    traded = 0
    skips: dict[str, int] = {}
    for d in dates:
        res, reason = simulate_day(
            d=d, spot=spot, store=store, chain_cache=cache, arm="A", ts_on=False
        )
        if res is None:
            skips[reason] = skips.get(reason, 0) + 1
        else:
            traded += 1
    total = traded + sum(skips.values())
    assert total == len(dates), (
        f"SKIP_COUNT FAIL traded={traded} skips={skips} dates={len(dates)}"
    )
    skip_s = ", ".join(f"{k}:{v}" for k, v in sorted(skips.items())) or "none"
    print(f"SKIP_COUNT PASS total={total} traded={traded} skips={{{skip_s}}}")


def test_data_start(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    assert all(d >= cfg.DATA_START for d in dates)
    n = 0
    for d in dates[:20]:
        res, _ = simulate_day(
            d=d, spot=spot, store=store, chain_cache=cache, arm="A", ts_on=False
        )
        if res is None:
            continue
        assert res.d >= cfg.DATA_START, f"DATA_START FAIL basket {res.d}"
        n += 1
    print(f"DATA_START PASS dates={len(dates)} traded_checked={n}")


def run_tests(
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
) -> None:
    test_no_lookahead(dates, spot, store, cache)
    test_zero_cost(dates, spot, store, cache)
    test_expiry_free(dates, spot, store, cache)
    test_arm_b_full(dates, spot, store, cache)
    test_skip_count(dates, spot, store, cache)
    test_data_start(dates, spot, store, cache)
    print("ALL S010 ENGINE TESTS PASS")


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
def arm_label(arm: str, dow: str, ts_on: bool) -> str:
    ts = "tsON" if ts_on else "tsOFF"
    return f"arm{arm}_{dow}_{ts}"


def run_one_arm(
    *,
    dates: list[date],
    spot: dict,
    store: MarksStore,
    cache: ChainCache,
    arm: str,
    dow: str,
    ts_on: bool,
) -> tuple[list[BasketResult], dict[str, int]]:
    rows: list[BasketResult] = []
    counts = {"traded": 0, "skip": 0, "days": 0}
    for d in dates:
        if cfg.DOW_NAMES[d.weekday()] != dow:
            continue
        counts["days"] += 1
        res, reason = simulate_day(
            d=d,
            spot=spot,
            store=store,
            chain_cache=cache,
            arm=arm,  # type: ignore[arg-type]
            ts_on=ts_on,
        )
        if res is None:
            counts["skip"] += 1
            continue
        rows.append(res)
        counts["traded"] += 1
        if counts["traded"] % cfg.PROGRESS_EVERY == 0:
            print(
                f"    .. {arm_label(arm, dow, ts_on)} "
                f"n={counts['traded']} day={d} "
                f"net={res.net_pnl:.2f} reason={res.exit_reason} "
                f"chain_loads={cache.loads} hits={cache.hits}",
                flush=True,
            )
    return rows, counts


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S010 weekend-theta engine")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--no-tests", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    print("loading spot...", flush=True)
    spot = load_spot_ohlc(Path(args.csv))
    dates = candidate_dates(spot)
    if args.max_days and args.max_days < len(dates):
        dates = dates[: args.max_days]
    print(
        f"entry_dates={len(dates)} data_start={cfg.DATA_START} "
        f"spot_bars={len(spot)}",
        flush=True,
    )
    if dates:
        assert dates[0] >= cfg.DATA_START

    store = MarksStore()
    cache = ChainCache()

    if not args.no_tests:
        print("=== TESTS ===", flush=True)
        run_tests(dates, spot, store, cache)

    lines: list[str] = [
        "===== S010 WEEKEND THETA =====",
        f"generated_utc={datetime.now(tz=timezone.utc).isoformat()}",
        f"DATA_START={cfg.DATA_START.isoformat()} n_entry_dates={len(dates)}",
        f"qty straddle={cfg.STRADDLE_QTY} strangle={cfg.STRANGLE_QTY} "
        f"prot={cfg.PROTECTION_QTY} MARGIN_MULT={cfg.MARGIN_MULT}",
        "Arm A = D+3 calendar protection; Arm B = D+2 same-expiry control "
        "(independent run, Trap 1).",
        "",
    ]

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n_arms = 2 * 7 * 2
    arm_i = 0
    for arm in ("A", "B"):
        for dow in cfg.DOW_NAMES:
            for ts_on in (True, False):
                arm_i += 1
                label = arm_label(arm, dow, ts_on)
                print(f"[arm {arm_i}/{n_arms}] start {label}", flush=True)
                t_arm = time.monotonic()
                rows, counts = run_one_arm(
                    dates=dates,
                    spot=spot,
                    store=store,
                    cache=cache,
                    arm=arm,
                    dow=dow,
                    ts_on=ts_on,
                )
                st = ArmStats(rows)
                csv_path = out_dir / f"s010_{label}_{stamp}.csv"
                write_basket_csv(csv_path, rows)
                secs = time.monotonic() - t_arm
                line = (
                    f"  {label}: n={st.n} mean={st.mean:.4f} "
                    f"median={st.median:.4f} win%={st.win_pct:.1f} "
                    f"worst={st.worst:.4f} maxDD={st.max_dd:.4f} "
                    f"gross_mean={st.mean_gross:.4f} "
                    f"ci95=[{st.ci_lo:.4f}, {st.ci_hi:.4f}] "
                    f"skips={counts['skip']}/{counts['days']} secs={secs:.1f}"
                )
                lines.append(line)
                print(
                    f"[arm {arm_i}/{n_arms}] done  {label} "
                    f"n={st.n} secs={secs:.1f} csv={csv_path.name}",
                    flush=True,
                )

    elapsed = time.monotonic() - t0
    lines.append("")
    lines.append(
        f"grid_total={elapsed:.1f}s chain_loads={cache.loads} "
        f"cache_hits={cache.hits}"
    )
    txt_path = out_dir / f"s010_engine_{stamp}.txt"
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for ln in lines:
        print(ln, flush=True)
    print(f"txt={txt_path}", flush=True)
    print(f"DONE in {elapsed:.1f}s", flush=True)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
