#!/usr/bin/env python3
"""
ULP matched-control test: zone breakouts (A) vs ordinary breakouts (B).

Same entry (breakout close), same direction, same k*ATR(14) stop for both.
Difference is only whether an active ULP far-edge was crossed on that bar.

Does NOT invent or test an inverted rule.
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_BACKTEST = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s003_edge  # noqa: E402
import ulp_zones as uz  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
RESULTS_DIR = _BACKTEST / "results"
DATA_DIR = _BACKTEST / "data_1m"
LOOKBACK = 5
TIMEFRAMES = ("5m", "15m")
N_VALUES = (10, 20, 50)
K_VALUES = (1, 2, 3)
ATR_PERIOD = 14
TARGET_FOR_EXPECT = 100  # expectancy at common stop uses T=100, S=k*ATR

HOW_TO_READ = """\
HOW TO READ THIS REPORT
  A approximately equals B   ->  the ULP zone adds nothing beyond the fact that
                                 a breakout happened. The concept is dead.
  A clearly below B          ->  breakouts through untouched zones fail more
                                 often than ordinary breakouts. The zone
                                 carries information worth pursuing.

  Judge "clearly" by the z-score on A vs B, not by eye. With three N values,
  three k values, two timeframes and two shrink modes there are 36 cells, so a
  single standout cell proves nothing — look for a consistent sign across cells.

  Do NOT invert R2 from this result in this run. If A is clearly below B, the
  inverse rule must be pre-registered and tested on a separate unseen period.
"""


@dataclass
class BreakoutEvent:
    bar_index: int
    direction: str  # LONG | SHORT
    group: str  # A | B
    entry: float
    atr: float
    prior_extreme: float  # N-bar high (UP) or low (DOWN)
    zone_id: int | None
    zone_top: float | None
    zone_bot: float | None
    natural_stop_distance: float | None  # info only (group A)
    bar_high: float
    bar_low: float
    bar_close: float


def resolve_data(tf: str) -> Path:
    matches = sorted(DATA_DIR.glob(f"BTCUSD_{tf}_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_{tf}_*.csv in {DATA_DIR}")
    return matches[-1]


def wilder_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> list[float | None]:
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out
    trs: list[float] = [0.0] * n
    trs[0] = highs[0] - lows[0]
    for i in range(1, n):
        trs[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def collect_breakouts(
    bars: list[uz.Bar],
    *,
    lookback: int,
    shrink_during_confirmation: bool,
    n_breakout: int,
    atr_series: list[float | None],
) -> tuple[list[BreakoutEvent], list[BreakoutEvent]]:
    """
    Returns (group_A, group_B) for one N. Zone engine via ulp_zones primitives.
    One group-A contribution per zone_id lifetime.
    """
    n = len(bars)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    opens = [b.open for b in bars]
    closes = [b.close for b in bars]

    high_set = uz.ActiveHighSet()
    low_set = uz.ActiveLowSet()
    next_id = 0
    used_zones: set[int] = set()
    group_a: list[BreakoutEvent] = []
    group_b: list[BreakoutEvent] = []

    for i in range(n):
        pivot = i - lookback
        if pivot >= lookback:
            if uz.is_pivot_high(highs, pivot, lookback, lookback):
                top = highs[pivot]
                bot = max(opens[pivot], closes[pivot])
                z = uz.Zone(
                    zone_id=next_id,
                    side="HIGH",
                    pivot_index=pivot,
                    confirm_index=i,
                    zone_top=top,
                    zone_bot=bot,
                    top_birth=top,
                    bot_birth=bot,
                    zero_height=(top == bot),
                )
                next_id += 1
                if shrink_during_confirmation:
                    uz.replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                if z.still_alive:
                    high_set.insert(z)

            if uz.is_pivot_low(lows, pivot, lookback, lookback):
                top = min(opens[pivot], closes[pivot])
                bot = lows[pivot]
                z = uz.Zone(
                    zone_id=next_id,
                    side="LOW",
                    pivot_index=pivot,
                    confirm_index=i,
                    zone_top=top,
                    zone_bot=bot,
                    top_birth=top,
                    bot_birth=bot,
                    zero_height=(top == bot),
                )
                next_id += 1
                if shrink_during_confirmation:
                    uz.replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                if z.still_alive:
                    low_set.insert(z)

        bar = bars[i]
        atr = atr_series[i]

        # Breakout tests need N prior bars
        if i >= n_breakout and atr is not None and atr > 0:
            prior_high = max(highs[i - n_breakout : i])
            prior_low = min(lows[i - n_breakout : i])
            is_up = bar.close > prior_high
            is_down = bar.close < prior_low

            if is_up:
                # Active HIGH zone whose zone_top lies in this bar's range
                crossed: list[uz.Zone] = [
                    z
                    for z in high_set.zones
                    if bar.low <= z.zone_top <= bar.high and z.zone_id not in used_zones
                ]
                if crossed:
                    # Prefer the zone whose top is nearest to the close (deterministic)
                    z = min(crossed, key=lambda zz: abs(zz.zone_top - bar.close))
                    used_zones.add(z.zone_id)
                    nat = abs(bar.close - z.zone_bot)
                    group_a.append(
                        BreakoutEvent(
                            bar_index=i,
                            direction="LONG",
                            group="A",
                            entry=bar.close,
                            atr=atr,
                            prior_extreme=prior_high,
                            zone_id=z.zone_id,
                            zone_top=z.zone_top,
                            zone_bot=z.zone_bot,
                            natural_stop_distance=nat,
                            bar_high=bar.high,
                            bar_low=bar.low,
                            bar_close=bar.close,
                        )
                    )
                else:
                    # No unused HIGH zone top in range -> group B
                    any_high_edge = any(
                        bar.low <= z.zone_top <= bar.high for z in high_set.zones
                    )
                    if not any_high_edge:
                        group_b.append(
                            BreakoutEvent(
                                bar_index=i,
                                direction="LONG",
                                group="B",
                                entry=bar.close,
                                atr=atr,
                                prior_extreme=prior_high,
                                zone_id=None,
                                zone_top=None,
                                zone_bot=None,
                                natural_stop_distance=None,
                                bar_high=bar.high,
                                bar_low=bar.low,
                                bar_close=bar.close,
                            )
                        )

            elif is_down:
                crossed = [
                    z
                    for z in low_set.zones
                    if bar.low <= z.zone_bot <= bar.high and z.zone_id not in used_zones
                ]
                if crossed:
                    z = min(crossed, key=lambda zz: abs(zz.zone_bot - bar.close))
                    used_zones.add(z.zone_id)
                    nat = abs(z.zone_top - bar.close)
                    group_a.append(
                        BreakoutEvent(
                            bar_index=i,
                            direction="SHORT",
                            group="A",
                            entry=bar.close,
                            atr=atr,
                            prior_extreme=prior_low,
                            zone_id=z.zone_id,
                            zone_top=z.zone_top,
                            zone_bot=z.zone_bot,
                            natural_stop_distance=nat,
                            bar_high=bar.high,
                            bar_low=bar.low,
                            bar_close=bar.close,
                        )
                    )
                else:
                    any_low_edge = any(
                        bar.low <= z.zone_bot <= bar.high for z in low_set.zones
                    )
                    if not any_low_edge:
                        group_b.append(
                            BreakoutEvent(
                                bar_index=i,
                                direction="SHORT",
                                group="B",
                                entry=bar.close,
                                atr=atr,
                                prior_extreme=prior_low,
                                zone_id=None,
                                zone_top=None,
                                zone_bot=None,
                                natural_stop_distance=None,
                                bar_high=bar.high,
                                bar_low=bar.low,
                                bar_close=bar.close,
                            )
                        )

        # Shrink / sweep after signal check
        for z in list(high_set.candidates_touching(bar.high)):
            action = uz.apply_bar_to_high_zone(z, bar, i)
            if action == "remove":
                high_set.remove(z)
            elif action == "moved":
                high_set.remove(z)
                if z.still_alive:
                    high_set.insert(z)

        for z in list(low_set.candidates_touching(bar.low)):
            action = uz.apply_bar_to_low_zone(z, bar, i)
            if action == "remove":
                low_set.remove(z)
            elif action == "moved":
                low_set.remove(z)
                if z.still_alive:
                    low_set.insert(z)

    return group_a, group_b


def expectancy_at_stop(
    *,
    idx: int,
    direction: str,
    entry: float,
    stop_dist: float,
    target: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> float | None:
    """Path PnL with common stop distance and fixed target (harness-style)."""
    n = len(closes)
    max_fwd = n - 1 - idx
    if max_fwd < 1:
        return None
    long = direction == "LONG"
    walk = min(s003_edge.HORIZON, max_fwd)
    running_fav = 0.0
    running_adv = 0.0
    for step in range(1, walk + 1):
        j = idx + step
        if long:
            fav, adv = highs[j] - entry, entry - lows[j]
        else:
            fav, adv = entry - lows[j], highs[j] - entry
        if fav > running_fav:
            running_fav = fav
        if adv > running_adv:
            running_adv = adv
        if running_adv >= stop_dist:
            return -stop_dist
        if running_fav >= target:
            return target
    # horizon close
    move = closes[idx + walk] - entry
    return move if long else -move


def measure_group(
    events: list[BreakoutEvent],
    *,
    k: float,
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> dict[str, Any]:
    targets = s003_edge.DEFAULT_TARGETS
    stops = s003_edge.DEFAULT_STOPS
    hit_flags: list[bool] = []
    expects: list[float] = []
    fwd30: list[float] = []
    fwd60: list[float] = []
    fwd120: list[float] = []
    nat_stops: list[float] = []

    for ev in events:
        stop_dist = k * ev.atr
        extreme = (
            ev.entry - stop_dist if ev.direction == "LONG" else ev.entry + stop_dist
        )
        m = s003_edge.compute_path_metrics(
            idx=ev.bar_index,
            direction=ev.direction,
            entry=ev.entry,
            extreme=extreme,
            highs=highs,
            lows=lows,
            closes=closes,
            targets=targets,
            stops=stops,
        )
        h = m.get("hit_rate_100_before_50")
        if h is not None:
            hit_flags.append(bool(h))
        exp = expectancy_at_stop(
            idx=ev.bar_index,
            direction=ev.direction,
            entry=ev.entry,
            stop_dist=stop_dist,
            target=float(TARGET_FOR_EXPECT),
            highs=highs,
            lows=lows,
            closes=closes,
        )
        if exp is not None:
            expects.append(exp)
        if m.get("fwd_30") is not None:
            fwd30.append(float(m["fwd_30"]))
        if m.get("fwd_60") is not None:
            fwd60.append(float(m["fwd_60"]))
        if m.get("fwd_120") is not None:
            fwd120.append(float(m["fwd_120"]))
        if ev.natural_stop_distance is not None:
            nat_stops.append(ev.natural_stop_distance)

    def med(xs: list[float]) -> float | None:
        return float(statistics.median(xs)) if xs else None

    hit = (sum(hit_flags) / len(hit_flags)) if hit_flags else None
    return {
        "n": len(events),
        "n_hit_defined": len(hit_flags),
        "hit_rate": hit,
        "expectancy": float(statistics.mean(expects)) if expects else None,
        "fwd30": med(fwd30),
        "fwd60": med(fwd60),
        "fwd120": med(fwd120),
        "nat_stop_med": med(nat_stops),
    }


def two_prop_z(p_a: float, n_a: int, p_b: float, n_b: int) -> float | None:
    if n_a <= 0 or n_b <= 0:
        return None
    # Unpooled SE for difference of proportions
    se = math.sqrt(p_a * (1.0 - p_a) / n_a + p_b * (1.0 - p_b) / n_b)
    if se <= 0:
        return None
    return (p_a - p_b) / se


def _fmt(v: float | None, d: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{d}f}"


def _pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{100.0 * v:.1f}%"


def _pp(v: float | None) -> str:
    """Percentage-point difference."""
    if v is None:
        return "n/a"
    return f"{100.0 * v:+.1f}pp"


def hand_audit(
    a: BreakoutEvent | None,
    b: BreakoutEvent | None,
    bars: list[uz.Bar],
    *,
    n_breakout: int,
    k: float,
) -> list[str]:
    lines = ["=== HAND AUDIT — one group A and one group B (side by side) ==="]

    def one(ev: BreakoutEvent | None, label: str) -> None:
        if ev is None:
            lines.append(f"{label}: none found")
            return
        i = ev.bar_index
        stop_dist = k * ev.atr
        stop_px = (
            ev.entry - stop_dist if ev.direction == "LONG" else ev.entry + stop_dist
        )
        if i + 30 < len(bars):
            px30 = bars[i + 30].close
            raw = px30 - ev.entry
            fwd = raw if ev.direction == "LONG" else -raw
        else:
            px30 = None
            raw = None
            fwd = None
        lines.append(f"--- {label} ---")
        lines.append(
            f"  time_ist={uz._fmt_ist(bars[i].time)}  dir={ev.direction}  "
            f"close={ev.bar_close:.2f}  high={ev.bar_high:.2f}  low={ev.bar_low:.2f}"
        )
        kind = "N-bar high" if ev.direction == "LONG" else "N-bar low"
        lines.append(
            f"  broke {kind}={ev.prior_extreme:.2f} (N={n_breakout})  "
            f"close {'>' if ev.direction == 'LONG' else '<'} extreme"
        )
        if ev.group == "A":
            lines.append(
                f"  zone_id={ev.zone_id}  top={ev.zone_top:.2f}  bot={ev.zone_bot:.2f}  "
                f"edge in bar range=True  natural_stop_dist={ev.natural_stop_distance:.2f}"
            )
        else:
            lines.append("  zone edge crossed=False (ordinary breakout)")
        lines.append(
            f"  ATR(14)={ev.atr:.2f}  k={k}  stop_dist={stop_dist:.2f}  "
            f"stop_price={stop_px:.2f}"
        )
        if px30 is None:
            lines.append("  price_30=n/a")
        else:
            lines.append(
                f"  price_30={px30:.2f}  raw_move={raw:.2f}  "
                f"signed_fwd={fwd:.2f}"
            )

    one(a, "GROUP A (zone breakout)")
    one(b, "GROUP B (ordinary breakout)")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ULP matched-control breakout test")
    p.add_argument("--lookback", type=int, default=LOOKBACK)
    args = p.parse_args(argv)
    lookback = int(args.lookback)

    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    audit_lines: list[str] = []
    audit_done = False
    size_notes: list[str] = []

    # Cache bars + ATR + breakouts per (tf, shrink, N)
    for tf in TIMEFRAMES:
        data_path = resolve_data(tf)
        bars = uz.load_bars(data_path)
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        atr_series = wilder_atr(highs, lows, closes, ATR_PERIOD)
        print(f"Loaded {tf} bars={len(bars)} from {data_path.name}", flush=True)

        for shrink in (False, True):
            shrink_mode = "Y" if shrink else "N"
            for n_bo in N_VALUES:
                print(
                    f"  collect breakouts tf={tf} shrink={shrink_mode} N={n_bo} ...",
                    flush=True,
                )
                t1 = time.perf_counter()
                ga, gb = collect_breakouts(
                    bars,
                    lookback=lookback,
                    shrink_during_confirmation=shrink,
                    n_breakout=n_bo,
                    atr_series=atr_series,
                )
                print(
                    f"    A={len(ga)} B={len(gb)} in {time.perf_counter() - t1:.2f}s",
                    flush=True,
                )
                ratio = (len(gb) / len(ga)) if ga else float("inf")
                size_notes.append(
                    f"  {tf} shrink={shrink_mode} N={n_bo}: "
                    f"n_A={len(ga)} n_B={len(gb)}  B/A={ratio:.2f}"
                )

                if not audit_done and ga and gb and tf == "5m" and not shrink and n_bo == 20:
                    audit_lines = hand_audit(
                        ga[0], gb[0], bars, n_breakout=n_bo, k=2.0
                    )
                    audit_done = True

                for k in K_VALUES:
                    ma = measure_group(
                        ga, k=float(k), highs=highs, lows=lows, closes=closes
                    )
                    mb = measure_group(
                        gb, k=float(k), highs=highs, lows=lows, closes=closes
                    )
                    hit_diff = None
                    if ma["hit_rate"] is not None and mb["hit_rate"] is not None:
                        hit_diff = ma["hit_rate"] - mb["hit_rate"]
                    exp_diff = None
                    if ma["expectancy"] is not None and mb["expectancy"] is not None:
                        exp_diff = ma["expectancy"] - mb["expectancy"]
                    z = None
                    if (
                        ma["hit_rate"] is not None
                        and mb["hit_rate"] is not None
                        and ma["n_hit_defined"] > 0
                        and mb["n_hit_defined"] > 0
                    ):
                        z = two_prop_z(
                            ma["hit_rate"],
                            ma["n_hit_defined"],
                            mb["hit_rate"],
                            mb["n_hit_defined"],
                        )
                    rows.append(
                        {
                            "tf": tf,
                            "shrink": shrink_mode,
                            "N": n_bo,
                            "k": k,
                            "n_A": ma["n"],
                            "n_B": mb["n"],
                            "hit_A": ma["hit_rate"],
                            "hit_B": mb["hit_rate"],
                            "hit_diff": hit_diff,
                            "exp_A": ma["expectancy"],
                            "exp_B": mb["expectancy"],
                            "exp_diff": exp_diff,
                            "fwd30_A": ma["fwd30"],
                            "fwd30_B": mb["fwd30"],
                            "fwd60_A": ma["fwd60"],
                            "fwd60_B": mb["fwd60"],
                            "fwd120_A": ma["fwd120"],
                            "fwd120_B": mb["fwd120"],
                            "nat_stop_A": ma["nat_stop_med"],
                            "z": z,
                        }
                    )

    if not audit_done:
        audit_lines = ["=== HAND AUDIT ===", "No A/B pair available for audit."]

    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r["hit_diff"] is None,
            -(r["hit_diff"] if r["hit_diff"] is not None else 0.0),
        ),
    )

    runtime = time.perf_counter() - t0
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"ulp_control_{stamp}.txt"

    # Sign consistency
    diffs = [r["hit_diff"] for r in rows if r["hit_diff"] is not None]
    n_neg = sum(1 for d in diffs if d < 0)
    n_pos = sum(1 for d in diffs if d > 0)
    n_zero = sum(1 for d in diffs if d == 0)

    lines: list[str] = []
    lines.append("=== ULP matched-control: zone breakouts (A) vs ordinary (B) ===")
    lines.append(f"cells: {len(rows)}  (tf x shrink x N x k)")
    lines.append(f"lookback_zones: {lookback}  ATR: Wilder({ATR_PERIOD})")
    lines.append(
        f"expectancy: T={TARGET_FOR_EXPECT} vs common stop S=k*ATR "
        f"(identical rule for A and B)"
    )
    lines.append(f"runtime_s: {runtime:.2f}")
    lines.append("")
    lines.append(HOW_TO_READ)
    lines.append("")
    lines.append("--- SAMPLE SIZES ---")
    lines.extend(size_notes)
    lines.append("")
    # Per-timeframe totals (sum over shrink/N is not independent; report per cell range)
    for tf in TIMEFRAMES:
        sub = [r for r in rows if r["tf"] == tf]
        lines.append(
            f"  {tf} across cells: n_A range "
            f"{min(r['n_A'] for r in sub)}..{max(r['n_A'] for r in sub)}, "
            f"n_B range {min(r['n_B'] for r in sub)}..{max(r['n_B'] for r in sub)}"
        )
    lines.append("")
    lines.append(
        f"Sign of (hit_A - hit_B) across {len(diffs)} cells: "
        f"negative={n_neg} positive={n_pos} zero={n_zero}"
    )
    lines.append("")
    lines.append(
        "tf   sh  N   k   n_A    n_B   hitA   hitB   A-B     "
        "expA    expB   expA-B  "
        "f30A   f30B   f60A   f60B  f120A  f120B  natStopA     z"
    )
    lines.append("-" * 140)
    for r in rows_sorted:
        lines.append(
            f"{r['tf']:>3}  {r['shrink']:>1}  {r['N']:>3}  {r['k']}  "
            f"{r['n_A']:>5}  {r['n_B']:>5}  "
            f"{_pct(r['hit_A']):>6} {_pct(r['hit_B']):>6} {_pp(r['hit_diff']):>8}  "
            f"{_fmt(r['exp_A']):>7} {_fmt(r['exp_B']):>7} {_fmt(r['exp_diff']):>7}  "
            f"{_fmt(r['fwd30_A'], 1):>6} {_fmt(r['fwd30_B'], 1):>6} "
            f"{_fmt(r['fwd60_A'], 1):>6} {_fmt(r['fwd60_B'], 1):>6} "
            f"{_fmt(r['fwd120_A'], 1):>6} {_fmt(r['fwd120_B'], 1):>6}  "
            f"{_fmt(r['nat_stop_A'], 1):>8}  {_fmt(r['z'], 2):>6}"
        )
    lines.append("")
    lines.extend(audit_lines)

    text = "\n".join(lines) + "\n"
    out_path.write_text(text, encoding="utf-8")
    print()
    print(text)
    print(f"report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
