#!/usr/bin/env python3
"""S008 regime-gate grid / smoke runner."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.costs import ensure_slip_table  # noqa: E402
from backtest.harness.data import MarksStore, find_spot_csv, load_spot_map  # noqa: E402
from backtest.strategies.s008_regime_gate.signal import (  # noqa: E402
    build_signals_through,
)
from backtest.strategies.s008_regime_gate.strategy import (  # noqa: E402
    DEFAULT_MAX_STRIKE_GAP,
    IS_FROM,
    IS_TO,
    OOS_FROM,
    OOS_TO,
    BasketResult,
    GateMode,
    S008RegimeGateStrategy,
    enforce_oos_threshold,
    iter_weekdays,
)

logger = logging.getLogger("s008_grid")
RUNS_DIR = Path(__file__).resolve().parent / "runs"


def _parse_hhmm(s: str) -> tuple[int, int]:
    s = s.strip().replace(":", "")
    if len(s) == 3:
        s = "0" + s
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f"bad HHMM: {s!r}")
    return int(s[:2]), int(s[2:])


def _parse_gates(s: str) -> list[GateMode]:
    out: list[GateMode] = []
    for part in s.split(","):
        g = part.strip().lower()
        if g not in ("none", "switch", "flat"):
            raise ValueError(f"bad gate {g!r}")
        out.append(g)  # type: ignore[arg-type]
    return out


def write_baskets_csv(path: Path, rows: list[BasketResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "date",
        "gate",
        "side",
        "skipped",
        "skip_reason",
        "entry_ts",
        "spot_entry",
        "call_strike",
        "put_strike",
        "target_call_K",
        "chosen_call_K",
        "call_gap",
        "target_put_K",
        "chosen_put_K",
        "put_gap",
        "strike_ok",
        "call_mark",
        "put_mark",
        "call_fill",
        "put_fill",
        "qty",
        "entry_fee",
        "entry_slip_cost",
        "exit_fee",
        "exit_slip_cost",
        "settle_ts",
        "settle_spot",
        "settle_hhmm",
        "call_payoff",
        "put_payoff",
        "settlement_pay",
        "premium_pnl",
        "gross_pnl",
        "net_pnl",
        "sig",
        "threshold",
        "prev_rvol",
        "overnight",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for b in rows:
            w.writerow(
                {
                    "date": b.d.isoformat(),
                    "gate": b.gate,
                    "side": b.side,
                    "skipped": int(b.skipped),
                    "skip_reason": b.skip_reason,
                    "entry_ts": b.entry_ts,
                    "spot_entry": f"{b.spot_entry:.4f}",
                    "call_strike": f"{b.call_strike:.0f}",
                    "put_strike": f"{b.put_strike:.0f}",
                    "target_call_K": f"{b.target_call_k:.0f}",
                    "chosen_call_K": f"{b.chosen_call_k:.0f}",
                    "call_gap": f"{b.call_gap:.0f}",
                    "target_put_K": f"{b.target_put_k:.0f}",
                    "chosen_put_K": f"{b.chosen_put_k:.0f}",
                    "put_gap": f"{b.put_gap:.0f}",
                    "strike_ok": int(b.strike_ok),
                    "call_mark": f"{b.call_mark:.6f}",
                    "put_mark": f"{b.put_mark:.6f}",
                    "call_fill": f"{b.call_fill:.6f}",
                    "put_fill": f"{b.put_fill:.6f}",
                    "qty": b.qty,
                    "entry_fee": f"{b.entry_fee:.6f}",
                    "entry_slip_cost": f"{b.entry_slip_cost:.6f}",
                    "exit_fee": f"{b.exit_fee:.6f}",
                    "exit_slip_cost": f"{b.exit_slip_cost:.6f}",
                    "settle_ts": b.settle_ts,
                    "settle_spot": f"{b.settle_spot:.4f}",
                    "settle_hhmm": f"{b.settle_hour:02d}:{b.settle_minute:02d}",
                    "call_payoff": f"{b.call_payoff:.6f}",
                    "put_payoff": f"{b.put_payoff:.6f}",
                    "settlement_pay": f"{b.settlement_pay:.6f}",
                    "premium_pnl": f"{b.premium_pnl:.6f}",
                    "gross_pnl": f"{b.gross_pnl:.6f}",
                    "net_pnl": f"{b.net_pnl:.6f}",
                    "sig": f"{b.sig:.6f}",
                    "threshold": f"{b.threshold:.4f}",
                    "prev_rvol": f"{b.prev_rvol:.8f}",
                    "overnight": f"{b.overnight:.8f}",
                }
            )


def format_trace(b: BasketResult) -> list[str]:
    return [
        f"--- {b.d.isoformat()} gate={b.gate} side={b.side} "
        f"skip={b.skipped}/{b.skip_reason} ---",
        f"  entry {b.entry_hour:02d}:{b.entry_minute:02d} spot={b.spot_entry:.2f} "
        f"sig={b.sig:.4f} thr={b.threshold:.2f}",
        f"  C target={b.target_call_k:.0f} chosen={b.chosen_call_k:.0f} "
        f"gap={b.call_gap:.0f} mark={b.call_mark:.4f} fill={b.call_fill:.4f}",
        f"  P target={b.target_put_k:.0f} chosen={b.chosen_put_k:.0f} "
        f"gap={b.put_gap:.0f} mark={b.put_mark:.4f} fill={b.put_fill:.4f}",
        f"  strike_ok={b.strike_ok}",
        f"  settle {b.settle_hour:02d}:{b.settle_minute:02d} "
        f"S={b.settle_spot:.2f} ts={b.settle_ts}",
        f"  payoff C={b.call_payoff:.4f} P={b.put_payoff:.4f} "
        f"settlement_pay={b.settlement_pay:.4f}",
        f"  premium_pnl={b.premium_pnl:.4f} gross={b.gross_pnl:.4f} "
        f"entry_fee={b.entry_fee:.4f} exit_fee={b.exit_fee:.4f} "
        f"net={b.net_pnl:.4f}",
    ]


def run_arm(
    *,
    gate: GateMode,
    threshold: float,
    d0: date,
    d1: date,
    entry_h: int,
    entry_m: int,
    window: str,
    max_strike_gap: float,
    spot: dict[int, float],
    store: MarksStore,
    sigs: dict[date, Any],
) -> tuple[list[BasketResult], dict[str, int]]:
    strat = S008RegimeGateStrategy(
        gate=gate,
        threshold=threshold,
        entry_hour=entry_h,
        entry_minute=entry_m,
        window=window,
        max_strike_gap=max_strike_gap,
    )
    rows: list[BasketResult] = []
    counts = {
        "traded": 0,
        "flat_gate": 0,
        "skip_data": 0,
        "strike_unavailable": 0,
        "days": 0,
    }
    for d in iter_weekdays(d0, d1):
        counts["days"] += 1
        s = sigs.get(d)
        if s is None:
            counts["skip_data"] += 1
            continue
        b = strat.simulate_day(d=d, sig=s, store=store, spot_close=spot)
        rows.append(b)
        if b.skipped:
            if b.skip_reason == "gate_flat":
                counts["flat_gate"] += 1
            elif b.skip_reason == "STRIKE_UNAVAILABLE":
                counts["strike_unavailable"] += 1
            else:
                counts["skip_data"] += 1
        else:
            counts["traded"] += 1
    return rows, counts


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S008 regime-gate runner")
    ap.add_argument("--start", type=str, required=True)
    ap.add_argument("--end", type=str, required=True)
    ap.add_argument("--entry-time", type=str, default="0900")
    ap.add_argument("--gate", type=str, default="none,flat,switch")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument(
        "--window",
        type=str,
        default="is",
        choices=("is", "oos", "custom"),
        help="oos HARD-LOCKS: threshold must be passed, never searched",
    )
    ap.add_argument("--tag", type=str, default="s008")
    ap.add_argument("--qty", type=int, default=100)
    ap.add_argument("--slip-model", type=str, default="bucketed")
    ap.add_argument(
        "--max-strike-gap",
        type=float,
        default=DEFAULT_MAX_STRIKE_GAP,
        help="Skip basket if |chosen-target| > this (default 400). Never silent snap.",
    )
    ap.add_argument("--run-tests", action="store_true", default=True)
    ap.add_argument("--no-run-tests", action="store_false", dest="run_tests")
    args = ap.parse_args()

    d0 = date.fromisoformat(args.start)
    d1 = date.fromisoformat(args.end)
    thr = enforce_oos_threshold(args.window, args.threshold)
    eh, em = _parse_hhmm(args.entry_time)
    gates = _parse_gates(args.gate)
    max_gap = float(args.max_strike_gap)

    # Window sanity (warn only for custom)
    if args.window == "is" and (d0 < IS_FROM or d1 > IS_TO):
        logger.warning(
            "start/end outside locked IS %s..%s (ok for smoke)",
            IS_FROM,
            IS_TO,
        )
    if args.window == "oos" and (d0 < OOS_FROM or d1 > OOS_TO):
        logger.warning(
            "start/end outside locked OOS %s..%s",
            OOS_FROM,
            OOS_TO,
        )

    test_lines: list[str] = []
    if args.run_tests:
        from backtest.strategies.s008_regime_gate import test_s008 as tmod

        try:
            tmod.test_lookahead_sig_truncate()
            test_lines.append("LOOKAHEAD PASS")
            tmod.test_exit_cost_zero()
            test_lines.append("EXIT_COST_ZERO PASS")
            tmod.test_settlement_spot_timestamp()
            test_lines.append("SETTLEMENT_SPOT PASS")
            tmod.test_strike_gap_guard()
            test_lines.append("STRIKE_GAP PASS")
            test_lines.append("ALL S008 TESTS PASS")
        except AssertionError as exc:
            test_lines.append(f"TEST FAIL: {exc}")
            raise

    ensure_slip_table()
    spot_path = find_spot_csv()
    if spot_path is None:
        raise SystemExit("no spot csv")
    spot = load_spot_map(spot_path)
    store = MarksStore()

    # Warm signals from ~60 calendar days before start (expanding ranks)
    warm0 = date.fromordinal(max(d0.toordinal() - 60, date(2025, 7, 1).toordinal()))
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)
    logger.info("signals n=%d warm=%s..%s", len(sigs), warm0, d1)

    lines: list[str] = [
        "===== S008 REGIME GATE =====",
        f"generated_utc={datetime.now(tz=timezone.utc).isoformat()}",
        f"tag={args.tag} window={args.window} threshold={thr} "
        f"max_strike_gap={max_gap:.0f}",
        f"range={d0}..{d1} entry={eh:02d}:{em:02d} gates={gates}",
        f"spot_csv={spot_path.name}",
        "",
    ]
    lines.extend(test_lines)
    lines.append("")

    all_rows: list[BasketResult] = []
    for g in gates:
        rows, counts = run_arm(
            gate=g,
            threshold=thr,
            d0=d0,
            d1=d1,
            entry_h=eh,
            entry_m=em,
            window=args.window,
            max_strike_gap=max_gap,
            spot=spot,
            store=store,
            sigs=sigs,
        )
        all_rows.extend(rows)
        lines.append(
            f"gate={g}: days={counts['days']} traded={counts['traded']} "
            f"flat_gate={counts['flat_gate']} "
            f"STRIKE_UNAVAILABLE={counts['strike_unavailable']} "
            f"skip_data={counts['skip_data']}"
        )
        # Show STRIKE_UNAVAILABLE traces (important for smoke)
        strike_skips = [b for b in rows if b.skip_reason == "STRIKE_UNAVAILABLE"]
        if strike_skips:
            lines.append(f"  --- STRIKE_UNAVAILABLE ({len(strike_skips)}) ---")
            for b in strike_skips[:5]:
                lines.append(
                    f"  {b.d} spot={b.spot_entry:.0f} "
                    f"C t={b.target_call_k:.0f} c={b.chosen_call_k:.0f} "
                    f"g={b.call_gap:.0f} | "
                    f"P t={b.target_put_k:.0f} c={b.chosen_put_k:.0f} "
                    f"g={b.put_gap:.0f}"
                )
        # 3-day leg traces (first 3 traded)
        traded = [b for b in rows if not b.skipped]
        lines.append(f"  --- traces gate={g} (up to 3) ---")
        for b in traded[:3]:
            lines.extend(format_trace(b))
        # Always show 2025-11-05 if in range (smoke regression day)
        for b in rows:
            if b.d == date(2025, 11, 5):
                lines.append("  --- 2025-11-05 (regression) ---")
                lines.extend(format_trace(b))
        lines.append("")

    store.close()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = RUNS_DIR / f"{args.tag}_{stamp}_baskets.csv"
    txt_path = RUNS_DIR / f"{args.tag}_{stamp}.txt"
    write_baskets_csv(csv_path, all_rows)
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for ln in lines:
        print(ln)
        logger.info("%s", ln)
    print(f"csv={csv_path}")
    print(f"txt={txt_path}")


if __name__ == "__main__":
    main()
