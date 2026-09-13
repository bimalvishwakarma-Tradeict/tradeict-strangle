#!/usr/bin/env python3
"""
Exit-system backtest — half/runner targets + stoploss-reversal chain.

MEASUREMENT (not a search): paired chain ON vs OFF within each config,
signals vs matched random baseline under the IDENTICAL exit system.

Costs from live fills: hedge OFF=23 pts/RT, hedge ON=53 pts/RT (per unit qty).
Stops are pure price — costs never widen/tighten a stop.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_BACKTEST = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s003_sim  # noqa: E402
import ulp_signals as ulp  # noqa: E402
import ulp_zones as uz  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
RESULTS_DIR = _BACKTEST / "results"
DATA_DIR = _BACKTEST / "data_1m"
SIM_CONFIG = _BACKTEST / "s003_sim_config.json"

TIMEFRAMES = ("1m", "3m", "5m", "15m")
STOPS = (50, 75, 100, 125, 150, 175, 200, 225, 250)
TARGET_MODES = ("A", "B")
SOURCES = ("LSR4", "ULP-R1")
COST_OFF = 23.0
COST_ON = 53.0
BASE_SEED = 20260913
Q = 1.0

# 2 sources * 4 tf * 9 S * 2 modes * 2 chain * 2 hedge
N_CONFIGS = (
    len(SOURCES)
    * len(TIMEFRAMES)
    * len(STOPS)
    * len(TARGET_MODES)
    * 2
    * 2
)

PREDICTIONS = """\
PREDICTIONS REGISTERED BEFORE THE RUN
  P1  Gross expectancy is approximately zero at every S and every target ratio.
      An 8:1 reward:risk does not raise it, because P(hit T2 before S) falls in
      proportion — on a driftless walk P = S/(S+T).
  P2  Net expectancy is approximately minus the expected cost:
      about -23 with chain OFF hedge OFF, about -54 with chain ON hedge OFF.
  P3  Chain ON is WORSE than chain OFF, by more than cost alone. The chain is a
      momentum bet — a stop-out means price moved against you and the reversal
      enters in that direction — while the ULP control test showed BTC
      mean-reverts at these horizons (R2 breakout entries ran z = 3 to 6.8
      BELOW baseline).
"""


@dataclass
class Signal:
    bar_index: int
    direction: str  # LONG | SHORT
    entry: float
    hour_ist: int


@dataclass
class LegResult:
    name: str
    direction: str
    qty: float
    entry: float
    exit: float
    exit_reason: str
    gross: float
    cost: float
    net: float
    stop_level: float
    target_level: float | None


@dataclass
class TradeResult:
    hit_t1: bool
    stopped_before_t1: bool
    went_rev1: bool
    went_rev2: bool
    round_trips_qty: float
    gross: float
    cost: float
    net: float
    winner_main: bool
    legs: list[LegResult] = field(default_factory=list)
    audit: list[str] = field(default_factory=list)


def resolve_data(tf: str) -> Path:
    matches = sorted(DATA_DIR.glob(f"BTCUSD_{tf}_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_{tf}_*.csv in {DATA_DIR}")
    return matches[-1]


def targets_for(mode: str, s: float) -> tuple[float, float, float]:
    """Return (T1, T2, Tr)."""
    if mode == "A":
        return (2.0 * s, 8.0 * s, 1.0 * s)
    if mode == "B":
        return (100.0, 400.0, 50.0)
    raise ValueError(mode)


def opposite(direction: str) -> str:
    return "SHORT" if direction == "LONG" else "LONG"


def signed_move(direction: str, entry: float, exit_px: float) -> float:
    if direction == "LONG":
        return exit_px - entry
    return entry - exit_px


def stop_price(direction: str, entry: float, s: float) -> float:
    if direction == "LONG":
        return entry - s
    return entry + s


def target_price(direction: str, entry: float, t: float) -> float:
    if direction == "LONG":
        return entry + t
    return entry - t


def runner_be_price(direction: str, entry: float, cost_rt: float) -> float:
    """
    Runner net P&L = qty * signed_move - qty * cost_rt.
    Breakeven when signed_move == cost_rt (costs included, price points).
    """
    if direction == "LONG":
        return entry + cost_rt
    return entry - cost_rt


def bar_hits_stop(
    direction: str, high: float, low: float, sp: float
) -> bool:
    if direction == "LONG":
        return low <= sp
    return high >= sp


def bar_hits_target(
    direction: str, high: float, low: float, tp: float
) -> bool:
    if direction == "LONG":
        return high >= tp
    return low <= tp


def simulate_simple_leg(
    *,
    direction: str,
    qty: float,
    entry: float,
    s: float,
    t: float,
    cost_rt: float,
    highs: list[float],
    lows: list[float],
    start_i: int,
    name: str,
    same_bar_high: float | None = None,
    same_bar_low: float | None = None,
) -> tuple[LegResult, int]:
    """
    Full qty exits at target T or stop S. Returns (leg, exit_bar_index).
    Adverse-first if both hit on same bar. Stops are pure price.
    """
    sp = stop_price(direction, entry, s)
    tp = target_price(direction, entry, t)
    cost = cost_rt * qty

    def finish(exit_px: float, reason: str, bar_i: int) -> tuple[LegResult, int]:
        g = qty * signed_move(direction, entry, exit_px)
        return (
            LegResult(
                name=name,
                direction=direction,
                qty=qty,
                entry=entry,
                exit=exit_px,
                exit_reason=reason,
                gross=g,
                cost=cost,
                net=g - cost,
                stop_level=sp,
                target_level=tp,
            ),
            bar_i,
        )

    # Optional same-bar continuation (after an immediate reverse fill)
    if same_bar_high is not None and same_bar_low is not None:
        hit_s = bar_hits_stop(direction, same_bar_high, same_bar_low, sp)
        hit_t = bar_hits_target(direction, same_bar_high, same_bar_low, tp)
        if hit_s and hit_t:
            return finish(sp, "STOP", start_i)
        if hit_s:
            return finish(sp, "STOP", start_i)
        if hit_t:
            return finish(tp, "TARGET", start_i)

    n = len(highs)
    for j in range(start_i + 1, n):
        hit_s = bar_hits_stop(direction, highs[j], lows[j], sp)
        hit_t = bar_hits_target(direction, highs[j], lows[j], tp)
        if hit_s and hit_t:
            return finish(sp, "STOP", j)
        if hit_s:
            return finish(sp, "STOP", j)
        if hit_t:
            return finish(tp, "TARGET", j)

    # Data end: close at last close approx via mid of last bar range — use close path
    # Caller passes closes separately; use last bar's close-equivalent = (h+l)/2 avoided.
    # Exit at last close provided by walking — use lows/highs midpoint no:
    # We need closes[j]. Pass via stop fill at last available — use entry unchanged 0.
    last = n - 1
    # Prefer explicit: exit at last bar close if provided through highs/lows only —
    # simulate_trade will pass and we use a sentinel: exit at stop side of last bar close
    # Actually use the last close from a parallel array — refactor to pass closes.
    return finish(entry, "DATA_END", last)  # overwritten — see simulate_trade


def simulate_trade(
    *,
    direction: str,
    entry: float,
    entry_i: int,
    s: float,
    t1: float,
    t2: float,
    tr: float,
    chain: bool,
    cost_rt: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    audit: bool = False,
) -> TradeResult:
    """
    Main Q with half@T1 + runner@T2/BE; optional REV1/REV2 chain on stop-before-T1.
    """
    lines: list[str] = []
    legs: list[LegResult] = []
    sp = stop_price(direction, entry, s)
    tp1 = target_price(direction, entry, t1)
    tp2 = target_price(direction, entry, t2)
    be = runner_be_price(direction, entry, cost_rt)
    half = Q * 0.5

    if audit:
        lines.append(
            f"MAIN {direction} entry={entry:.2f} @bar={entry_i}  "
            f"S={s} stop_px={sp:.2f}  T1={t1} tp1={tp1:.2f}  "
            f"T2={t2} tp2={tp2:.2f}  runner_BE_px={be:.2f}  "
            f"cost_rt={cost_rt}  chain={chain}"
        )
        lines.append("  (stops are pure price — cost NOT added to stop distance)")

    n = len(closes)
    hit_t1 = False
    stopped = False
    exit_i = entry_i
    main_stop_fill = sp

    # Phase 1: full position until T1 or stop
    for j in range(entry_i + 1, n):
        hit_s = bar_hits_stop(direction, highs[j], lows[j], sp)
        hit_t = bar_hits_target(direction, highs[j], lows[j], tp1)
        if hit_s and hit_t:
            hit_s, hit_t = True, False  # adverse-first
        if hit_s:
            stopped = True
            exit_i = j
            main_stop_fill = sp
            break
        if hit_t:
            hit_t1 = True
            exit_i = j
            break
    else:
        # data end before T1/stop — flatten at last close
        last = n - 1
        g = Q * signed_move(direction, entry, closes[last])
        c = cost_rt * Q
        legs.append(
            LegResult(
                "MAIN_FLAT",
                direction,
                Q,
                entry,
                closes[last],
                "DATA_END",
                g,
                c,
                g - c,
                sp,
                tp1,
            )
        )
        if audit:
            lines.append(f"  DATA_END flatten @ {closes[last]:.2f} gross={g:.2f} cost={c:.2f}")
        return TradeResult(
            hit_t1=False,
            stopped_before_t1=False,
            went_rev1=False,
            went_rev2=False,
            round_trips_qty=Q,
            gross=g,
            cost=c,
            net=g - c,
            winner_main=False,
            legs=legs,
            audit=lines,
        )

    gross = 0.0
    cost = 0.0
    rt_qty = 0.0
    went_rev1 = False
    went_rev2 = False

    if stopped:
        g = Q * signed_move(direction, entry, main_stop_fill)
        c = cost_rt * Q
        gross += g
        cost += c
        rt_qty += Q
        legs.append(
            LegResult(
                "MAIN_STOP",
                direction,
                Q,
                entry,
                main_stop_fill,
                "STOP",
                g,
                c,
                g - c,
                sp,
                tp1,
            )
        )
        if audit:
            lines.append(
                f"  MAIN STOP @ {main_stop_fill:.2f} bar={exit_i}  "
                f"gross={g:.2f} cost={c:.2f} net={g - c:.2f}"
            )

        if chain:
            # REV1 opposite, qty Q, target Tr, stop S
            d1 = opposite(direction)
            e1 = main_stop_fill
            went_rev1 = True
            leg1, i1 = _run_full_leg(
                direction=d1,
                qty=Q,
                entry=e1,
                s=s,
                t=tr,
                cost_rt=cost_rt,
                highs=highs,
                lows=lows,
                closes=closes,
                start_i=exit_i,
                same_bar=True,
                name="REV1",
            )
            legs.append(leg1)
            gross += leg1.gross
            cost += leg1.cost
            rt_qty += Q
            if audit:
                lines.append(
                    f"  REV1 {d1} entry={e1:.2f} stop={leg1.stop_level:.2f} "
                    f"target={leg1.target_level:.2f} -> {leg1.exit_reason} "
                    f"@ {leg1.exit:.2f}  gross={leg1.gross:.2f} cost={leg1.cost:.2f} "
                    f"net={leg1.net:.2f}"
                )

            if leg1.exit_reason == "STOP":
                # REV2 opposite of REV1, qty 2Q
                d2 = opposite(d1)
                e2 = leg1.exit
                went_rev2 = True
                leg2, _i2 = _run_full_leg(
                    direction=d2,
                    qty=2.0 * Q,
                    entry=e2,
                    s=s,
                    t=tr,
                    cost_rt=cost_rt,
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    start_i=i1,
                    same_bar=True,
                    name="REV2",
                )
                legs.append(leg2)
                gross += leg2.gross
                cost += leg2.cost
                rt_qty += 2.0 * Q
                if audit:
                    lines.append(
                        f"  REV2 {d2} qty=2 entry={e2:.2f} stop={leg2.stop_level:.2f} "
                        f"target={leg2.target_level:.2f} -> {leg2.exit_reason} "
                        f"@ {leg2.exit:.2f}  gross={leg2.gross:.2f} cost={leg2.cost:.2f} "
                        f"net={leg2.net:.2f}"
                    )
                    lines.append("  chain ENDS after REV2")

        if audit:
            lines.append(
                f"  TOTAL gross={gross:.2f} cost={cost:.2f} net={gross - cost:.2f}  "
                f"rt_qty={rt_qty:.1f}"
            )
        return TradeResult(
            hit_t1=False,
            stopped_before_t1=True,
            went_rev1=went_rev1,
            went_rev2=went_rev2,
            round_trips_qty=rt_qty,
            gross=gross,
            cost=cost,
            net=gross - cost,
            winner_main=False,
            legs=legs,
            audit=lines,
        )

    # T1 hit — book half; winner; no chain
    g1 = half * signed_move(direction, entry, tp1)
    c1 = cost_rt * half
    gross += g1
    cost += c1
    rt_qty += half
    legs.append(
        LegResult(
            "MAIN_T1",
            direction,
            half,
            entry,
            tp1,
            "T1",
            g1,
            c1,
            g1 - c1,
            sp,
            tp1,
        )
    )
    if audit:
        lines.append(
            f"  T1 HIT @ {tp1:.2f} bar={exit_i}  half booked  "
            f"gross={g1:.2f} cost={c1:.2f}  WINNER — chain suppressed"
        )

    # Runner: if T1 move does not cover runner's cost share, runner is already
    # net-negative at T1 — exit runner at T1 as well (no net-profit to protect).
    # Else manage T2 vs cost-inclusive BE (signed_move == cost_rt).
    runner_exit: float
    runner_reason: str
    runner_i = exit_i
    if t1 <= cost_rt:
        runner_exit = tp1
        runner_reason = "RUNNER_FLAT_AT_T1"
        if audit:
            lines.append(
                f"  RUNNER flat at T1 (T1={t1} <= cost_rt={cost_rt}; "
                f"never net-profitable after costs)"
            )
    else:
        runner_exit = closes[n - 1]
        runner_reason = "DATA_END"
        runner_i = n - 1
        for j in range(exit_i + 1, n):
            hit_t2 = bar_hits_target(direction, highs[j], lows[j], tp2)
            if direction == "LONG":
                hit_be = lows[j] <= be
            else:
                hit_be = highs[j] >= be
            # whichever comes first; if both, adverse-first (BE before T2)
            if hit_be and hit_t2:
                runner_exit, runner_reason, runner_i = be, "RUNNER_BE", j
                break
            if hit_be:
                runner_exit, runner_reason, runner_i = be, "RUNNER_BE", j
                break
            if hit_t2:
                runner_exit, runner_reason, runner_i = tp2, "T2", j
                break

    g2 = half * signed_move(direction, entry, runner_exit)
    c2 = cost_rt * half
    gross += g2
    cost += c2
    rt_qty += half
    legs.append(
        LegResult(
            "MAIN_RUNNER",
            direction,
            half,
            entry,
            runner_exit,
            runner_reason,
            g2,
            c2,
            g2 - c2,
            be,
            tp2,
        )
    )
    if audit:
        lines.append(
            f"  RUNNER exit @ {runner_exit:.2f} ({runner_reason}) bar={runner_i}  "
            f"gross={g2:.2f} cost={c2:.2f} net={g2 - c2:.2f}"
        )
        lines.append(
            f"  TOTAL gross={gross:.2f} cost={cost:.2f} net={gross - cost:.2f}  "
            f"rt_qty={rt_qty:.1f}"
        )

    return TradeResult(
        hit_t1=True,
        stopped_before_t1=False,
        went_rev1=False,
        went_rev2=False,
        round_trips_qty=rt_qty,
        gross=gross,
        cost=cost,
        net=gross - cost,
        winner_main=True,
        legs=legs,
        audit=lines,
    )


def _run_full_leg(
    *,
    direction: str,
    qty: float,
    entry: float,
    s: float,
    t: float,
    cost_rt: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    start_i: int,
    same_bar: bool,
    name: str,
) -> tuple[LegResult, int]:
    sp = stop_price(direction, entry, s)
    tp = target_price(direction, entry, t)
    cost = cost_rt * qty
    n = len(closes)

    def finish(exit_px: float, reason: str, bar_i: int) -> tuple[LegResult, int]:
        g = qty * signed_move(direction, entry, exit_px)
        return (
            LegResult(
                name,
                direction,
                qty,
                entry,
                exit_px,
                reason,
                g,
                cost,
                g - cost,
                sp,
                tp,
            ),
            bar_i,
        )

    first = start_i if same_bar else start_i + 1
    for j in range(first, n):
        hit_s = bar_hits_stop(direction, highs[j], lows[j], sp)
        hit_t = bar_hits_target(direction, highs[j], lows[j], tp)
        if hit_s and hit_t:
            return finish(sp, "STOP", j)
        if hit_s:
            return finish(sp, "STOP", j)
        if hit_t:
            return finish(tp, "TARGET", j)
    return finish(closes[n - 1], "DATA_END", n - 1)


def summarize_trades(trades: list[TradeResult]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "n": 0,
            "win_rate_main": None,
            "pct_t1": None,
            "pct_rev1": None,
            "pct_rev2": None,
            "avg_rt": None,
            "gross_exp": None,
            "net_exp": None,
            "avg_cost": None,
        }
    return {
        "n": n,
        "win_rate_main": sum(1 for t in trades if t.winner_main) / n,
        "pct_t1": sum(1 for t in trades if t.hit_t1) / n,
        "pct_rev1": sum(1 for t in trades if t.went_rev1) / n,
        "pct_rev2": sum(1 for t in trades if t.went_rev2) / n,
        "avg_rt": sum(t.round_trips_qty for t in trades) / n,
        "gross_exp": sum(t.gross for t in trades) / n,
        "net_exp": sum(t.net for t in trades) / n,
        "avg_cost": sum(t.cost for t in trades) / n,
    }


def run_book(
    signals: list[Signal],
    *,
    s: float,
    t1: float,
    t2: float,
    tr: float,
    chain: bool,
    cost_rt: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[TradeResult]:
    out: list[TradeResult] = []
    for sig in signals:
        out.append(
            simulate_trade(
                direction=sig.direction,
                entry=sig.entry,
                entry_i=sig.bar_index,
                s=s,
                t1=t1,
                t2=t2,
                tr=tr,
                chain=chain,
                cost_rt=cost_rt,
                highs=highs,
                lows=lows,
                closes=closes,
            )
        )
    return out


def make_baseline_signals(
    signals: list[Signal],
    times: list[int],
    closes: list[float],
    rng: random.Random,
) -> list[Signal]:
    by_hour: dict[int, list[int]] = defaultdict(list)
    for i, ts in enumerate(times):
        hour = datetime.fromtimestamp(ts, tz=IST).hour
        # need some forward room
        if i + 5 < len(times):
            by_hour[hour].append(i)

    directions = [s.direction for s in signals]
    rng.shuffle(directions)
    out: list[Signal] = []
    for i, sig in enumerate(signals):
        pool = by_hour.get(sig.hour_ist) or list(range(max(0, len(times) - 10)))
        idx = pool[rng.randrange(len(pool))]
        out.append(
            Signal(
                bar_index=idx,
                direction=directions[i],
                entry=closes[idx],
                hour_ist=sig.hour_ist,
            )
        )
    return out


def load_lsr4_signals(tf: str, data_path: Path) -> list[Signal]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _sig, _arm, _sum, meta = s003_sim.run(
            config_path=SIM_CONFIG,
            start=None,
            end=None,
            data_file=data_path,
            timeframe=tf,
        )
    path = Path(meta["signals_path"])
    times, _o, _h, _l, closes = _load_ohlc(data_path)
    index_by_time = {t: i for i, t in enumerate(times)}
    out: list[Signal] = []
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            conf_dt = datetime.strptime(
                row["confirm_candle_utc"].strip(), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            ts = int(conf_dt.timestamp())
            idx = index_by_time.get(ts)
            if idx is None:
                continue
            out.append(
                Signal(
                    bar_index=idx,
                    direction=str(row["direction"]).upper(),
                    entry=closes[idx],
                    hour_ist=conf_dt.astimezone(IST).hour,
                )
            )
    return out


def load_ulp_r1_signals(tf: str, data_path: Path) -> list[Signal]:
    bars = uz.load_bars(data_path)
    r1, _r2 = ulp.collect_signals(
        bars, lookback=5, shrink_during_confirmation=False
    )
    out: list[Signal] = []
    for s in r1:
        ts = bars[s.bar_index].time
        hour = datetime.fromtimestamp(ts, tz=IST).hour
        out.append(
            Signal(
                bar_index=s.bar_index,
                direction=s.direction,
                entry=s.entry_price,
                hour_ist=hour,
            )
        )
    return out


def _load_ohlc(
    path: Path,
) -> tuple[list[int], list[float], list[float], list[float], list[float]]:
    import csv

    times: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            times.append(int(row["open_time_unix"]))
            opens.append(float(row["open"]))
            highs.append(float(row["high"]))
            lows.append(float(row["low"]))
            closes.append(float(row["close"]))
    return times, opens, highs, lows, closes


def _fmt(v: float | None, d: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{d}f}"


def _pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{100.0 * v:.1f}%"


def find_chain_audit(
    signals: list[Signal],
    *,
    s: float,
    t1: float,
    t2: float,
    tr: float,
    cost_rt: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> TradeResult | None:
    for sig in signals:
        trd = simulate_trade(
            direction=sig.direction,
            entry=sig.entry,
            entry_i=sig.bar_index,
            s=s,
            t1=t1,
            t2=t2,
            tr=tr,
            chain=True,
            cost_rt=cost_rt,
            highs=highs,
            lows=lows,
            closes=closes,
            audit=True,
        )
        if trd.went_rev2:
            trd.audit.insert(
                0,
                f"signal {sig.direction} bar={sig.bar_index} entry={sig.entry:.2f}",
            )
            return trd
    return None


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Exit-system chain test").parse_args(argv)

    t_all = time.perf_counter()
    print(f"TOTAL_CONFIGURATIONS (matrix): {N_CONFIGS}", flush=True)
    print(PREDICTIONS, flush=True)

    # Load signals once per (source, tf)
    signal_cache: dict[tuple[str, str], list[Signal]] = {}
    ohlc_cache: dict[str, tuple[list[int], list[float], list[float], list[float]]] = {}

    for tf in TIMEFRAMES:
        data = resolve_data(tf)
        times, _o, highs, lows, closes = _load_ohlc(data)
        ohlc_cache[tf] = (times, highs, lows, closes)
        print(f"Loading LSR4 {tf} ...", flush=True)
        signal_cache[("LSR4", tf)] = load_lsr4_signals(tf, data)
        print(f"  LSR4 n={len(signal_cache[('LSR4', tf)])}", flush=True)
        print(f"Loading ULP-R1 {tf} ...", flush=True)
        signal_cache[("ULP-R1", tf)] = load_ulp_r1_signals(tf, data)
        print(f"  ULP-R1 n={len(signal_cache[('ULP-R1', tf)])}", flush=True)

    rows: list[dict[str, Any]] = []
    seeds_used: list[tuple[str, int]] = []
    config_idx = 0
    audit_trade: TradeResult | None = None

    for source in SOURCES:
        for tf in TIMEFRAMES:
            times, highs, lows, closes = ohlc_cache[tf]
            sigs = signal_cache[(source, tf)]
            for s in STOPS:
                for mode in TARGET_MODES:
                    t1, t2, tr = targets_for(mode, float(s))
                    for chain in (False, True):
                        for hedge in (False, True):
                            cost_rt = COST_ON if hedge else COST_OFF
                            seed = BASE_SEED + 30000 + config_idx
                            config_idx += 1
                            label = (
                                f"{source}_{tf}_S{s}_{mode}_"
                                f"ch{'Y' if chain else 'N'}_"
                                f"h{'Y' if hedge else 'N'}"
                            )
                            seeds_used.append((label, seed))

                            sig_trades = run_book(
                                sigs,
                                s=float(s),
                                t1=t1,
                                t2=t2,
                                tr=tr,
                                chain=chain,
                                cost_rt=cost_rt,
                                highs=highs,
                                lows=lows,
                                closes=closes,
                            )
                            sm = summarize_trades(sig_trades)

                            rng = random.Random(seed)
                            base_sigs = make_baseline_signals(
                                sigs, times, closes, rng
                            )
                            base_trades = run_book(
                                base_sigs,
                                s=float(s),
                                t1=t1,
                                t2=t2,
                                tr=tr,
                                chain=chain,
                                cost_rt=cost_rt,
                                highs=highs,
                                lows=lows,
                                closes=closes,
                            )
                            bm = summarize_trades(base_trades)

                            edge = None
                            if sm["net_exp"] is not None and bm["net_exp"] is not None:
                                edge = sm["net_exp"] - bm["net_exp"]

                            rows.append(
                                {
                                    "source": source,
                                    "tf": tf,
                                    "S": s,
                                    "mode": mode,
                                    "chain": "ON" if chain else "OFF",
                                    "hedge": "ON" if hedge else "OFF",
                                    "seed": seed,
                                    "label": label,
                                    "n_signals": sm["n"],
                                    "win_rate_main": sm["win_rate_main"],
                                    "pct_t1": sm["pct_t1"],
                                    "pct_rev1": sm["pct_rev1"],
                                    "pct_rev2": sm["pct_rev2"],
                                    "avg_rt": sm["avg_rt"],
                                    "gross_exp": sm["gross_exp"],
                                    "net_exp": sm["net_exp"],
                                    "avg_cost": sm["avg_cost"],
                                    "baseline_net_exp": bm["net_exp"],
                                    "baseline_gross_exp": bm["gross_exp"],
                                    "baseline_avg_cost": bm["avg_cost"],
                                    "edge_over_baseline": edge,
                                }
                            )

                            if (
                                audit_trade is None
                                and chain
                                and source == "LSR4"
                                and tf == "5m"
                                and s == 100
                                and mode == "B"
                                and not hedge
                            ):
                                audit_trade = find_chain_audit(
                                    sigs,
                                    s=float(s),
                                    t1=t1,
                                    t2=t2,
                                    tr=tr,
                                    cost_rt=cost_rt,
                                    highs=highs,
                                    lows=lows,
                                    closes=closes,
                                )

                            if config_idx % 48 == 0:
                                print(
                                    f"  progress {config_idx}/{N_CONFIGS}",
                                    flush=True,
                                )

    runtime = time.perf_counter() - t_all

    # --- Paired chain ON vs OFF ---
    pair_key = lambda r: (r["source"], r["tf"], r["S"], r["mode"], r["hedge"])
    by_pair: dict[Any, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        by_pair[pair_key(r)][r["chain"]] = r

    sig_diffs: list[float] = []
    base_diffs: list[float] = []
    n_sig_on_better = 0
    n_base_on_better = 0
    n_pairs = 0
    for _k, d in by_pair.items():
        if "ON" not in d or "OFF" not in d:
            continue
        n_pairs += 1
        sd = d["ON"]["net_exp"] - d["OFF"]["net_exp"]
        bd = d["ON"]["baseline_net_exp"] - d["OFF"]["baseline_net_exp"]
        sig_diffs.append(sd)
        base_diffs.append(bd)
        if sd > 0:
            n_sig_on_better += 1
        if bd > 0:
            n_base_on_better += 1

    # Gross vs S / mode
    gross_by_s: dict[int, list[float]] = defaultdict(list)
    gross_by_mode: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["gross_exp"] is not None:
            gross_by_s[int(r["S"])].append(r["gross_exp"])
            gross_by_mode[r["mode"]].append(r["gross_exp"])

    # Prediction checks
    all_gross = [r["gross_exp"] for r in rows if r["gross_exp"] is not None]
    med_gross = statistics.median(all_gross) if all_gross else None
    mean_gross = statistics.mean(all_gross) if all_gross else None

    off_off = [
        r["net_exp"]
        for r in rows
        if r["chain"] == "OFF" and r["hedge"] == "OFF" and r["net_exp"] is not None
    ]
    on_off = [
        r["net_exp"]
        for r in rows
        if r["chain"] == "ON" and r["hedge"] == "OFF" and r["net_exp"] is not None
    ]

    p1_hold = (
        mean_gross is not None
        and abs(mean_gross) < 15
        and med_gross is not None
        and abs(med_gross) < 15
    )
    # also check no systematic S trend: max|mean_s| still small
    s_means = {s: statistics.mean(vs) for s, vs in gross_by_s.items() if vs}
    p1_hold = p1_hold and all(abs(v) < 25 for v in s_means.values())

    mean_off_off = statistics.mean(off_off) if off_off else None
    mean_on_off = statistics.mean(on_off) if on_off else None
    p2_hold = (
        mean_off_off is not None
        and mean_on_off is not None
        and abs(mean_off_off - (-23)) < 15
        and abs(mean_on_off - (-54)) < 25
    )

    mean_sig_diff = statistics.mean(sig_diffs) if sig_diffs else None
    # cost-alone chain effect approx: compare avg cost ON vs OFF hedge OFF
    cost_off_chain_off = statistics.mean(
        [
            r["avg_cost"]
            for r in rows
            if r["chain"] == "OFF" and r["hedge"] == "OFF" and r["avg_cost"]
        ]
    )
    cost_off_chain_on = statistics.mean(
        [
            r["avg_cost"]
            for r in rows
            if r["chain"] == "ON" and r["hedge"] == "OFF" and r["avg_cost"]
        ]
    )
    cost_delta = cost_off_chain_on - cost_off_chain_off
    # P3: chain ON worse by MORE than cost alone → mean_sig_diff < -cost_delta
    # (diff is ON - OFF, negative means ON worse)
    p3_hold = (
        mean_sig_diff is not None
        and mean_sig_diff < 0
        and mean_sig_diff < -(cost_delta) + 1e-9  # worse than cost delta
    )
    # Actually "by more than cost alone" means the net deterioration exceeds the
    # extra cost: |net_diff| > cost_delta when net_diff < 0, i.e. net_diff < -cost_delta
    p3_hold = mean_sig_diff is not None and mean_sig_diff < -abs(cost_delta)

    # Sort full table
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r["net_exp"] is None,
            -(r["net_exp"] if r["net_exp"] is not None else -1e18),
        ),
    )

    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"exit_system_{stamp}.txt"

    lines: list[str] = []
    lines.append("=== Exit-system backtest: half/runner + reversal chain ===")
    lines.append(f"TOTAL_CONFIGURATIONS_EVALUATED: {N_CONFIGS}")
    lines.append(
        f"  (+ {N_CONFIGS} matched random baselines with identical exit rules)"
    )
    lines.append(f"paired ON/OFF comparisons: {n_pairs}")
    lines.append(f"runtime_s: {runtime:.2f}")
    lines.append("")
    lines.append(PREDICTIONS)
    lines.append("")
    lines.append("=== PREDICTION OUTCOMES ===")
    lines.append(
        f"P1 HOLD={p1_hold}  mean_gross={_fmt(mean_gross)}  "
        f"median_gross={_fmt(med_gross)}"
    )
    lines.append("  gross mean by S: " + ", ".join(
        f"S{s}={_fmt(statistics.mean(gross_by_s[s]))}" for s in STOPS
    ))
    lines.append(
        "  gross mean by mode: "
        + ", ".join(f"{m}={_fmt(statistics.mean(gross_by_mode[m]))}" for m in TARGET_MODES)
    )
    if not p1_hold:
        lines.append("  P1 FAILED — gross expectancy is NOT approximately zero everywhere.")
    else:
        lines.append("  P1 held — gross sits near zero across S and target modes.")

    lines.append(
        f"P2 HOLD={p2_hold}  mean net chainOFF hedgeOFF={_fmt(mean_off_off)} "
        f"(expect ~-23);  chainON hedgeOFF={_fmt(mean_on_off)} (expect ~-54)"
    )
    if not p2_hold:
        lines.append("  P2 FAILED — net expectancy does not match the stated cost anchors.")
    else:
        lines.append("  P2 held.")

    lines.append(
        f"P3 HOLD={p3_hold}  mean(net_ON - net_OFF) signals={_fmt(mean_sig_diff)}  "
        f"extra_avg_cost(ON-OFF, hedgeOFF)={_fmt(cost_delta)}"
    )
    lines.append(
        f"  baseline mean(net_ON - net_OFF)={_fmt(statistics.mean(base_diffs) if base_diffs else None)}"
    )
    if not p3_hold:
        lines.append(
            "  P3 FAILED — chain ON is not worse than OFF by more than cost alone "
            "(or is not worse at all)."
        )
    else:
        lines.append("  P3 held — chain ON is worse than cost alone would explain.")

    lines.append("")
    lines.append("=== 1. HEADLINE — paired chain ON vs OFF ===")
    lines.append(
        f"signals:   mean(ON-OFF)={_fmt(mean_sig_diff)}  "
        f"median={_fmt(statistics.median(sig_diffs) if sig_diffs else None)}  "
        f"ON_better={n_sig_on_better}/{n_pairs}"
    )
    lines.append(
        f"baseline:  mean(ON-OFF)={_fmt(statistics.mean(base_diffs) if base_diffs else None)}  "
        f"median={_fmt(statistics.median(base_diffs) if base_diffs else None)}  "
        f"ON_better={n_base_on_better}/{n_pairs}"
    )
    lines.append(
        "If signals and baseline move similarly, the chain is arithmetic, "
        "not a signal interaction."
    )

    lines.append("")
    lines.append("=== 2. GROSS vs NET ===")
    lines.append(
        f"Overall gross: mean={_fmt(mean_gross)} median={_fmt(med_gross)}"
    )
    if mean_gross is not None and abs(mean_gross) < 15:
        lines.append(
            "Gross expectancy sits near zero throughout — it does NOT rise "
            "systematically with S or with the 8:1 target ratio."
        )
    else:
        lines.append(
            "Gross expectancy does NOT sit near zero — see means by S/mode above."
        )
    lines.append(
        f"Avg cost hedgeOFF chainOFF={_fmt(cost_off_chain_off)}  "
        f"chainON={_fmt(cost_off_chain_on)}"
    )

    lines.append("")
    lines.append("=== 3. FULL TABLE (sorted by net_exp desc) ===")
    lines.append(
        "source   tf  S  mode ch hedge  n   win%  T1%  R1%  R2%  avgRT  "
        "gross   net   baseNet  edge  seed"
    )
    lines.append("-" * 120)
    for r in rows_sorted:
        lines.append(
            f"{r['source']:<7} {r['tf']:>3} {r['S']:>3}  {r['mode']}  "
            f"{r['chain']:>3}  {r['hedge']:>3}  {r['n_signals']:>5}  "
            f"{_pct(r['win_rate_main']):>5} {_pct(r['pct_t1']):>5} "
            f"{_pct(r['pct_rev1']):>5} {_pct(r['pct_rev2']):>5}  "
            f"{_fmt(r['avg_rt'], 2):>5}  {_fmt(r['gross_exp']):>7} "
            f"{_fmt(r['net_exp']):>7} {_fmt(r['baseline_net_exp']):>7} "
            f"{_fmt(r['edge_over_baseline']):>7}  {r['seed']}"
        )

    lines.append("")
    lines.append("=== SEEDS ===")
    for lab, seed in seeds_used:
        lines.append(f"  {lab}: {seed}")

    lines.append("")
    lines.append("=== HAND AUDIT — full chain end to end ===")
    if audit_trade is None:
        # fallback search
        for source, tf in (("LSR4", "5m"), ("ULP-R1", "5m"), ("LSR4", "1m")):
            times, highs, lows, closes = ohlc_cache[tf]
            sigs = signal_cache[(source, tf)]
            audit_trade = find_chain_audit(
                sigs,
                s=100.0,
                t1=100.0,
                t2=400.0,
                tr=50.0,
                cost_rt=COST_OFF,
                highs=highs,
                lows=lows,
                closes=closes,
            )
            if audit_trade:
                lines.append(f"(audit from {source} {tf} S=100 mode=B hedge=OFF)")
                break
    if audit_trade and audit_trade.audit:
        lines.extend(audit_trade.audit)
        lines.append("Stop levels used above are pure price (no fee/spread added).")
    else:
        lines.append("No REV2 chain found for audit search params.")

    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    # Print headline sections (not entire 576-row table to console)
    head = "\n".join(lines[: lines.index("=== 3. FULL TABLE (sorted by net_exp desc) ===") + 1])
    print()
    print(head)
    print(f"(full table + seeds in {out_path})")
    # print top/bottom 5
    print("--- top 5 by net_exp ---")
    for r in rows_sorted[:5]:
        print(
            f"  {r['source']} {r['tf']} S={r['S']} {r['mode']} ch={r['chain']} "
            f"h={r['hedge']} net={_fmt(r['net_exp'])} gross={_fmt(r['gross_exp'])} "
            f"edge={_fmt(r['edge_over_baseline'])}"
        )
    print("--- bottom 5 ---")
    for r in rows_sorted[-5:]:
        print(
            f"  {r['source']} {r['tf']} S={r['S']} {r['mode']} ch={r['chain']} "
            f"h={r['hedge']} net={_fmt(r['net_exp'])} gross={_fmt(r['gross_exp'])}"
        )
    if audit_trade:
        print("--- AUDIT ---")
        print("\n".join(audit_trade.audit))
    print(f"report: {out_path}")
    print(f"TOTAL_CONFIGURATIONS_EVALUATED: {N_CONFIGS}")
    print(f"runtime_s: {runtime:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
