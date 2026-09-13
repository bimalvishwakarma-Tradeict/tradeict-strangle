#!/usr/bin/env python3
"""
ULP — Untouched Liquidity Points zone tracker (zones + statistics only).

Pine ta.pivothigh / ta.pivotlow semantics (validated against TradingView via
PineForge corpus):
  HIGH: left bars must be <= pivot (non-strict); right bars must be < pivot (strict)
  LOW:  left bars must be >= pivot (non-strict); right bars must be > pivot (strict)

No look-ahead: zone becomes ACTIVE at confirm_index = pivot_index + lookback.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATA_DIR = Path(__file__).resolve().parent / "data_1m"
TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900}
COST_LEVELS = (23, 46, 53, 69)


# ---------------------------------------------------------------------------
# Candle load
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float


def load_bars(path: Path) -> list[Bar]:
    out: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(
                Bar(
                    time=int(row["open_time_unix"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    return out


def _fmt_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Pine pivot semantics
# ---------------------------------------------------------------------------


def is_pivot_high(highs: list[float], pivot: int, left: int, right: int) -> bool:
    """
    ta.pivothigh: LEFT non-strict (<=), RIGHT strict (<).
    Equal highs on the left are allowed; equal highs on the right invalidate
    until the flat-top ends (TV confirms one bar after the run completes).
    """
    ph = highs[pivot]
    for j in range(1, left + 1):
        if highs[pivot - j] > ph:
            return False
    for j in range(1, right + 1):
        if highs[pivot + j] >= ph:
            return False
    return True


def is_pivot_low(lows: list[float], pivot: int, left: int, right: int) -> bool:
    """
    ta.pivotlow: LEFT non-strict (>=), RIGHT strict (>).
    Mirror of pivothigh.
    """
    pl = lows[pivot]
    for j in range(1, left + 1):
        if lows[pivot - j] < pl:
            return False
    for j in range(1, right + 1):
        if lows[pivot + j] <= pl:
            return False
    return True


# ---------------------------------------------------------------------------
# Zone model + sorted active sets
# ---------------------------------------------------------------------------


@dataclass
class Zone:
    zone_id: int
    side: str  # HIGH | LOW
    pivot_index: int
    confirm_index: int
    zone_top: float
    zone_bot: float
    top_birth: float
    bot_birth: float
    zero_height: bool
    height_at_activation: float = 0.0
    first_partial_touch_index: int | None = None
    n_partial_touches: int = 0
    total_shrink_points: float = 0.0
    fully_swept_index: int | None = None
    still_alive: bool = True

    @property
    def height(self) -> float:
        return abs(self.zone_top - self.zone_bot)

    @property
    def height_birth(self) -> float:
        return abs(self.top_birth - self.bot_birth)


@dataclass
class ActiveHighSet:
    """Active HIGH zones sorted by zone_bot ascending."""

    zones: list[Zone] = field(default_factory=list)

    def insert(self, z: Zone) -> None:
        i = bisect.bisect_left(self.zones, z.zone_bot, key=lambda x: x.zone_bot)
        self.zones.insert(i, z)

    def remove(self, z: Zone) -> None:
        # binary search then linear scan for object identity in equal-key run
        i = bisect.bisect_left(self.zones, z.zone_bot, key=lambda x: x.zone_bot)
        while i < len(self.zones) and self.zones[i].zone_bot == z.zone_bot:
            if self.zones[i] is z:
                del self.zones[i]
                return
            i += 1
        # fallback identity scan (should be rare)
        for j, zz in enumerate(self.zones):
            if zz is z:
                del self.zones[j]
                return

    def candidates_touching(self, bar_high: float) -> list[Zone]:
        # zone_bot < bar_high can be partially/fully touched
        i = bisect.bisect_left(self.zones, bar_high, key=lambda x: x.zone_bot)
        return self.zones[:i]


@dataclass
class ActiveLowSet:
    """Active LOW zones sorted by zone_top ascending."""

    zones: list[Zone] = field(default_factory=list)

    def insert(self, z: Zone) -> None:
        i = bisect.bisect_left(self.zones, z.zone_top, key=lambda x: x.zone_top)
        self.zones.insert(i, z)

    def remove(self, z: Zone) -> None:
        i = bisect.bisect_left(self.zones, z.zone_top, key=lambda x: x.zone_top)
        while i < len(self.zones) and self.zones[i].zone_top == z.zone_top:
            if self.zones[i] is z:
                del self.zones[i]
                return
            i += 1
        for j, zz in enumerate(self.zones):
            if zz is z:
                del self.zones[j]
                return

    def candidates_touching(self, bar_low: float) -> list[Zone]:
        # zone_top > bar_low can be partially/fully touched
        i = bisect.bisect_right(self.zones, bar_low, key=lambda x: x.zone_top)
        return self.zones[i:]


def apply_bar_to_high_zone(z: Zone, bar: Bar, bar_index: int) -> str:
    """Return 'remove' | 'moved' | 'none'."""
    if bar.high >= z.zone_top:
        z.fully_swept_index = bar_index
        z.still_alive = False
        return "remove"
    if bar.high > z.zone_bot:
        shrink = bar.high - z.zone_bot
        z.zone_bot = bar.high
        z.total_shrink_points += shrink
        z.n_partial_touches += 1
        if z.first_partial_touch_index is None:
            z.first_partial_touch_index = bar_index
        return "moved"
    return "none"


def apply_bar_to_low_zone(z: Zone, bar: Bar, bar_index: int) -> str:
    if bar.low <= z.zone_bot:
        z.fully_swept_index = bar_index
        z.still_alive = False
        return "remove"
    if bar.low < z.zone_top:
        shrink = z.zone_top - bar.low
        z.zone_top = bar.low
        z.total_shrink_points += shrink
        z.n_partial_touches += 1
        if z.first_partial_touch_index is None:
            z.first_partial_touch_index = bar_index
        return "moved"
    return "none"


def replay_confirmation_shrink(
    z: Zone,
    bars: list[Bar],
    pivot: int,
    confirm: int,
) -> None:
    """
    Replay shrink on bars [pivot+1, confirm) so the zone enters already
    reflecting price action during the confirmation window. The confirmation
    bar itself is applied once by the main per-bar loop.
    """
    for j in range(pivot + 1, confirm):
        if z.side == "HIGH":
            if bars[j].high >= z.zone_top:
                z.fully_swept_index = j
                z.still_alive = False
                return
            if bars[j].high > z.zone_bot:
                shrink = bars[j].high - z.zone_bot
                z.zone_bot = bars[j].high
                z.total_shrink_points += shrink
                z.n_partial_touches += 1
                if z.first_partial_touch_index is None:
                    z.first_partial_touch_index = j
        else:
            if bars[j].low <= z.zone_bot:
                z.fully_swept_index = j
                z.still_alive = False
                return
            if bars[j].low < z.zone_top:
                shrink = z.zone_top - bars[j].low
                z.zone_top = bars[j].low
                z.total_shrink_points += shrink
                z.n_partial_touches += 1
                if z.first_partial_touch_index is None:
                    z.first_partial_touch_index = j


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def build_zones(
    bars: list[Bar],
    *,
    lookback: int,
    shrink_during_confirmation: bool,
) -> tuple[list[Zone], list[int]]:
    """
    Returns (all_zones, concurrent_active_count_per_bar).
    concurrent series is aligned to bar index (0 for bars before first possible confirm).
    """
    n = len(bars)
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    opens = [b.open for b in bars]
    closes = [b.close for b in bars]

    high_set = ActiveHighSet()
    low_set = ActiveLowSet()
    all_zones: list[Zone] = []
    concurrent: list[int] = [0] * n
    next_id = 0

    # Earliest confirmable pivot index is lookback; confirm at 2*lookback
    for i in range(n):
        # 1) Activate newly confirmed pivots at this bar
        pivot = i - lookback
        if pivot >= lookback:
            # HIGH pivot
            if is_pivot_high(highs, pivot, lookback, lookback):
                top = highs[pivot]
                bot = max(opens[pivot], closes[pivot])
                z = Zone(
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
                    replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                if z.still_alive:
                    high_set.insert(z)
                all_zones.append(z)

            # LOW pivot
            if is_pivot_low(lows, pivot, lookback, lookback):
                top = min(opens[pivot], closes[pivot])
                bot = lows[pivot]
                z = Zone(
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
                    replay_confirmation_shrink(z, bars, pivot, i)
                z.height_at_activation = z.height
                if z.still_alive:
                    low_set.insert(z)
                all_zones.append(z)

        # 2) Apply this bar's price action to active zones (including those
        #    just activated on this confirm bar — they can shrink/sweep same bar)
        bar = bars[i]
        # HIGH candidates
        for z in list(high_set.candidates_touching(bar.high)):
            action = apply_bar_to_high_zone(z, bar, i)
            if action == "remove":
                high_set.remove(z)
            elif action == "moved":
                high_set.remove(z)
                if z.still_alive:
                    high_set.insert(z)

        for z in list(low_set.candidates_touching(bar.low)):
            action = apply_bar_to_low_zone(z, bar, i)
            if action == "remove":
                low_set.remove(z)
            elif action == "moved":
                low_set.remove(z)
                if z.still_alive:
                    low_set.insert(z)

        concurrent[i] = len(high_set.zones) + len(low_set.zones)

    return all_zones, concurrent


# ---------------------------------------------------------------------------
# Stats / IO
# ---------------------------------------------------------------------------


def _pctile(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _dist_block(vals: list[float], label: str) -> list[str]:
    lines = [f"  {label} (n={len(vals)}):"]
    if not vals:
        lines.append("    (empty)")
        return lines
    lines.append(
        "    min={:.2f}  p10={:.2f}  p25={:.2f}  median={:.2f}  "
        "p75={:.2f}  p90={:.2f}  max={:.2f}".format(
            min(vals),
            _pctile(vals, 10) or 0,
            _pctile(vals, 25) or 0,
            _pctile(vals, 50) or 0,
            _pctile(vals, 75) or 0,
            _pctile(vals, 90) or 0,
            max(vals),
        )
    )
    return lines


def go_no_go_fractions(heights: list[float]) -> dict[int, float]:
    n = len(heights) or 1
    return {lvl: sum(1 for h in heights if h > lvl) / n for lvl in COST_LEVELS}


def write_zones_csv(path: Path, zones: list[Zone], bars: list[Bar]) -> None:
    cols = [
        "pivot_time_utc",
        "pivot_time_ist",
        "confirm_time_utc",
        "confirm_time_ist",
        "side",
        "zone_top_at_birth",
        "zone_bot_at_birth",
        "height_at_birth_points",
        "distance_from_close_at_confirm_points",
        "first_partial_touch_time",
        "n_partial_touches",
        "total_shrink_points",
        "fully_swept_time",
        "bars_alive",
        "still_alive",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for z in zones:
            conf_close = bars[z.confirm_index].close
            if z.side == "HIGH":
                dist = z.bot_birth - conf_close
            else:
                dist = conf_close - z.top_birth

            if z.fully_swept_index is not None:
                bars_alive: int | None = z.fully_swept_index - z.confirm_index
                swept_t = _fmt_utc(bars[z.fully_swept_index].time)
            else:
                bars_alive = None
                swept_t = ""

            if z.first_partial_touch_index is not None:
                touch_t = _fmt_utc(bars[z.first_partial_touch_index].time)
            else:
                touch_t = ""

            w.writerow(
                {
                    "pivot_time_utc": _fmt_utc(bars[z.pivot_index].time),
                    "pivot_time_ist": _fmt_ist(bars[z.pivot_index].time),
                    "confirm_time_utc": _fmt_utc(bars[z.confirm_index].time),
                    "confirm_time_ist": _fmt_ist(bars[z.confirm_index].time),
                    "side": z.side,
                    "zone_top_at_birth": f"{z.top_birth:.4f}",
                    "zone_bot_at_birth": f"{z.bot_birth:.4f}",
                    "height_at_birth_points": f"{z.height_birth:.4f}",
                    "distance_from_close_at_confirm_points": f"{dist:.4f}",
                    "first_partial_touch_time": touch_t,
                    "n_partial_touches": z.n_partial_touches,
                    "total_shrink_points": f"{z.total_shrink_points:.4f}",
                    "fully_swept_time": swept_t,
                    "bars_alive": "" if bars_alive is None else bars_alive,
                    "still_alive": int(z.still_alive),
                }
            )


def build_stats(
    *,
    zones: list[Zone],
    bars: list[Bar],
    concurrent: list[int],
    timeframe: str,
    lookback: int,
    shrink_during_confirmation: bool,
    runtime_s: float,
) -> str:
    highs = [z for z in zones if z.side == "HIGH"]
    lows = [z for z in zones if z.side == "LOW"]
    zero = [z for z in zones if z.zero_height]
    heights_all = [z.height_at_activation for z in zones]
    heights_h = [z.height_at_activation for z in highs]
    heights_l = [z.height_at_activation for z in lows]
    heights_birth = [z.height_birth for z in zones]

    span_days = max(
        (bars[-1].time - bars[0].time) / 86400.0 if bars else 1.0,
        1e-9,
    )

    full_sweeps = sum(1 for z in zones if z.fully_swept_index is not None)
    partial_events = sum(z.n_partial_touches for z in zones)

    lifetimes = [
        (z.fully_swept_index - z.confirm_index)
        for z in zones
        if z.fully_swept_index is not None
    ]
    never = sum(1 for z in zones if z.still_alive)
    alive_ages = [
        (len(bars) - 1 - z.confirm_index) for z in zones if z.still_alive
    ]

    dists = []
    for z in zones:
        c = bars[z.confirm_index].close
        if z.side == "HIGH":
            dists.append(z.bot_birth - c)
        else:
            dists.append(c - z.top_birth)

    touches = [z.n_partial_touches for z in zones]
    go = go_no_go_fractions(heights_all)
    go_h = go_no_go_fractions(heights_h)
    go_l = go_no_go_fractions(heights_l)

    lines: list[str] = []
    lines.append("=== ULP zone statistics ===")
    lines.append(f"timeframe: {timeframe}")
    lines.append(f"lookback: {lookback}")
    lines.append(f"shrink_during_confirmation: {shrink_during_confirmation}")
    lines.append(
        "pivot semantics: HIGH left<= / right< ; LOW left>= / right>"
    )
    lines.append(f"bars: {len(bars)}")
    lines.append(f"runtime_s: {runtime_s:.3f}")
    lines.append("")
    lines.append("--- 1. zones created ---")
    lines.append(f"total: {len(zones)}")
    lines.append(f"HIGH: {len(highs)}  LOW: {len(lows)}")
    lines.append(f"zero-height: {len(zero)} ({100.0 * len(zero) / max(1, len(zones)):.2f}%)")
    lines.append("")
    lines.append("--- 2. height distribution at ACTIVATION (points) ---")
    lines.append(
        "  (after optional confirmation-window shrink; this is the fade-stop distance)"
    )
    lines.extend(_dist_block(heights_h, "HIGH"))
    lines.extend(_dist_block(heights_l, "LOW"))
    lines.extend(_dist_block(heights_all, "ALL"))
    lines.extend(_dist_block(heights_birth, "ALL birth heights (pre-shrink, reference)"))
    lines.append("")
    lines.append("--- 3. GO/NO-GO: fraction with activation height > cost floor ---")
    lines.append("  ALL:")
    for lvl in COST_LEVELS:
        lines.append(f"    >{lvl}: {100.0 * go[lvl]:.1f}%")
    lines.append("  HIGH:")
    for lvl in COST_LEVELS:
        lines.append(f"    >{lvl}: {100.0 * go_h[lvl]:.1f}%")
    lines.append("  LOW:")
    for lvl in COST_LEVELS:
        lines.append(f"    >{lvl}: {100.0 * go_l[lvl]:.1f}%")
    lines.append("")
    lines.append("--- 4. concurrent active zones ---")
    if concurrent:
        lines.append(
            f"  min={min(concurrent)}  median={statistics.median(concurrent):.0f}  "
            f"max={max(concurrent)}"
        )
    lines.append("")
    lines.append("--- 5. lifetime (bars, confirm -> full sweep) ---")
    lines.extend(_dist_block([float(x) for x in lifetimes], "swept zones"))
    lines.append(
        f"  never swept by data end: {never}/{len(zones)} = "
        f"{100.0 * never / max(1, len(zones)):.1f}%"
    )
    lines.append("")
    lines.append("--- 6. touch rate ---")
    lines.append(f"  full sweeps / day: {full_sweeps / span_days:.2f}")
    lines.append(f"  partial touches / day: {partial_events / span_days:.2f}")
    lines.append("")
    lines.append("--- 7. distance from close at confirmation ---")
    lines.extend(_dist_block(dists, "signed (HIGH: bot-close, LOW: close-top)"))
    lines.append("")
    lines.append("--- 8. n_partial_touches per zone ---")
    lines.extend(_dist_block([float(x) for x in touches], "touches"))
    lines.append("")
    lines.append("--- 9. still alive at end ---")
    lines.append(f"  count: {never}")
    lines.extend(_dist_block([float(x) for x in alive_ages], "age in bars"))
    lines.append("")

    # machine-readable summary line for comparison table
    lines.append("--- SUMMARY_JSON ---")
    summary = {
        "timeframe": timeframe,
        "shrink_during_confirmation": shrink_during_confirmation,
        "zones_total": len(zones),
        "zones_high": len(highs),
        "zones_low": len(lows),
        "zero_height": len(zero),
        "median_height": _pctile(heights_all, 50),
        "median_height_birth": _pctile(heights_birth, 50),
        "go_gt_23": go[23],
        "go_gt_46": go[46],
        "go_gt_53": go[53],
        "go_gt_69": go[69],
        "concurrent_max": max(concurrent) if concurrent else 0,
        "concurrent_median": float(statistics.median(concurrent)) if concurrent else 0,
        "full_sweeps_per_day": full_sweeps / span_days,
        "partial_touches_per_day": partial_events / span_days,
        "pct_never_swept": never / max(1, len(zones)),
        "runtime_s": runtime_s,
    }
    lines.append(json.dumps(summary))
    return "\n".join(lines)


def active_zones_at(
    zones: list[Zone],
    bars: list[Bar],
    end_index: int,
) -> list[Zone]:
    """Zones that were confirmed by end_index and not yet fully swept by then."""
    out: list[Zone] = []
    for z in zones:
        if z.confirm_index > end_index:
            continue
        if z.fully_swept_index is not None and z.fully_swept_index <= end_index:
            continue
        # reconstruct state at end_index by replaying from birth — expensive;
        # for parity list we report birth levels for still-structurally-alive
        # zones; better: re-run shrink from confirm..end on a copy.
        out.append(z)
    return out


def parity_snapshot(
    bars: list[Bar],
    *,
    lookback: int,
    shrink_during_confirmation: bool,
    window: int = 200,
) -> str:
    """
    Rebuild zones on the last `window` bars only is WRONG (misses older zones).
    Instead run full history then list zones active at the final bar of a
    recent 200-bar window (end = n-1, window start = n-200).
    Report birth levels AND current levels at window end.
    """
    zones, _ = build_zones(
        bars,
        lookback=lookback,
        shrink_during_confirmation=shrink_during_confirmation,
    )
    n = len(bars)
    end = n - 1
    start = max(0, end - window + 1)

    # Replay each surviving candidate to get levels at `end`
    lines = [
        f"PARITY WINDOW: bars[{start}..{end}]  "
        f"({_fmt_ist(bars[start].time)} -> {_fmt_ist(bars[end].time)} IST)",
        f"lookback={lookback} shrink_during_confirmation={shrink_during_confirmation}",
        "Active zones at window END (current top/bot after shrinks):",
    ]
    active: list[tuple[Zone, float, float]] = []
    for z in zones:
        if z.confirm_index > end:
            continue
        # clone birth levels and replay confirm..end
        top, bot = z.top_birth, z.bot_birth
        dead = False
        # optional confirmation-window shrink already baked into birth path;
        # start from post-birth state at confirm
        top, bot = z.top_birth, z.bot_birth
        if shrink_during_confirmation:
            # rebuild from birth via replay to confirm first
            top, bot = z.top_birth, z.bot_birth
            tmp = Zone(
                zone_id=z.zone_id,
                side=z.side,
                pivot_index=z.pivot_index,
                confirm_index=z.confirm_index,
                zone_top=top,
                zone_bot=bot,
                top_birth=top,
                bot_birth=bot,
                zero_height=z.zero_height,
            )
            replay_confirmation_shrink(tmp, bars, z.pivot_index, z.confirm_index)
            if not tmp.still_alive or (
                tmp.fully_swept_index is not None and tmp.fully_swept_index <= end
            ):
                continue
            top, bot = tmp.zone_top, tmp.zone_bot
            start_j = z.confirm_index
        else:
            start_j = z.confirm_index

        for j in range(start_j, end + 1):
            b = bars[j]
            if z.side == "HIGH":
                if b.high >= top:
                    dead = True
                    break
                if b.high > bot:
                    bot = b.high
            else:
                if b.low <= bot:
                    dead = True
                    break
                if b.low < top:
                    top = b.low
        if dead:
            continue
        # only list if zone was already confirmed within/before window end
        # (always true) — include even if pivot before window
        active.append((z, top, bot))

    active.sort(key=lambda t: (t[0].side, -t[1]))
    recent = [(z, top, bot) for z, top, bot in active if z.confirm_index >= start]
    lines.append(f"all_active_at_end={len(active)}  (includes ancient unswept zones; TV maxLevels may hide these)")
    lines.append(f"confirmed_inside_window_still_active={len(recent)}  <-- use these for eye parity")
    lines.append("")
    lines.append("RECENT (confirm inside window):")
    for z, top, bot in recent:
        lines.append(
            f"  {z.side:4} pivot={_fmt_ist(bars[z.pivot_index].time)}  "
            f"confirm={_fmt_ist(bars[z.confirm_index].time)}  "
            f"top={top:.2f} bot={bot:.2f} height={top - bot:.2f}  "
            f"birth_top={z.top_birth:.2f} birth_bot={z.bot_birth:.2f}"
        )
    return "\n".join(lines)


def run_one(
    *,
    data_file: Path,
    timeframe: str,
    lookback: int,
    shrink_during_confirmation: bool,
) -> dict[str, Any]:
    bars = load_bars(data_file)
    t0 = time.perf_counter()
    zones, concurrent = build_zones(
        bars,
        lookback=lookback,
        shrink_during_confirmation=shrink_during_confirmation,
    )
    runtime_s = time.perf_counter() - t0
    if runtime_s > 180:
        print(
            f"WARNING: runtime {runtime_s:.1f}s exceeds 3 minutes "
            f"for {timeframe} shrink={shrink_during_confirmation}"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    shrink_tag = "shrinkY" if shrink_during_confirmation else "shrinkN"
    zpath = RESULTS_DIR / f"ulp_zones_{timeframe}_{shrink_tag}_{stamp}.csv"
    spath = RESULTS_DIR / f"ulp_stats_{timeframe}_{shrink_tag}_{stamp}.txt"

    write_zones_csv(zpath, zones, bars)
    stats = build_stats(
        zones=zones,
        bars=bars,
        concurrent=concurrent,
        timeframe=timeframe,
        lookback=lookback,
        shrink_during_confirmation=shrink_during_confirmation,
        runtime_s=runtime_s,
    )
    spath.write_text(stats, encoding="utf-8")
    print(stats)
    print(f"zones csv: {zpath}")
    print(f"stats:     {spath}")

    summary_line = stats.split("--- SUMMARY_JSON ---")[-1].strip().splitlines()[0]
    summary = json.loads(summary_line)
    summary["zones_path"] = str(zpath)
    summary["stats_path"] = str(spath)
    summary["bars"] = bars
    summary["zones"] = zones
    return summary


def comparison_table(rows: list[dict[str, Any]]) -> str:
    lines = [
        "=== ULP timeframe comparison (GO/NO-GO headline) ===",
        "activation height > cost floor = zone thick enough that a fade stop could clear costs",
        "shrink N = no shrink during confirmation; Y = replay confirmation window first",
        "",
        f"{'tf':>4} {'sh':>3} {'zones':>7} {'med_h':>7} "
        f"{'>23%':>6} {'>46%':>6} {'>53%':>6} {'>69%':>6} "
        f"{'swp/d':>7} {'ptch/d':>7} {'cmax':>5} {'rt':>6}",
        "-" * 88,
    ]
    for r in rows:
        lines.append(
            f"{r['timeframe']:>4} "
            f"{'Y' if r['shrink_during_confirmation'] else 'N':>3} "
            f"{r['zones_total']:>7} "
            f"{(r['median_height'] or 0):>7.1f} "
            f"{100 * r['go_gt_23']:>5.1f}% "
            f"{100 * r['go_gt_46']:>5.1f}% "
            f"{100 * r['go_gt_53']:>5.1f}% "
            f"{100 * r['go_gt_69']:>5.1f}% "
            f"{r['full_sweeps_per_day']:>7.1f} "
            f"{r['partial_touches_per_day']:>7.1f} "
            f"{r['concurrent_max']:>5} "
            f"{r['runtime_s']:>5.2f}s"
        )
    lines.append("")
    return "\n".join(lines)


def resolve_data(tf: str, explicit: Path | None) -> Path:
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return p
        alt = Path(__file__).resolve().parents[1] / explicit
        if alt.is_file():
            return alt
        raise FileNotFoundError(explicit)
    matches = sorted(DATA_DIR.glob(f"BTCUSD_{tf}_*.csv"))
    if not matches:
        raise FileNotFoundError(f"No BTCUSD_{tf}_*.csv in {DATA_DIR}")
    return matches[-1]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ULP untouched liquidity zone tracker")
    p.add_argument("--config", default="backtest/ulp_config.json")
    p.add_argument("--data-file", default=None)
    p.add_argument("--timeframe", default=None, choices=["1m", "3m", "5m", "15m"])
    p.add_argument("--lookback", type=int, default=None)
    p.add_argument(
        "--shrink-during-confirmation",
        default=None,
        choices=["true", "false"],
    )
    p.add_argument(
        "--all-timeframes",
        action="store_true",
        help="Run 1m/3m/5m/15m (and both shrink modes) + comparison table",
    )
    p.add_argument(
        "--parity",
        action="store_true",
        help="Print TradingView parity snapshot for recent 200-bar 1m window",
    )
    args = p.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        cfg_path = Path(__file__).resolve().parents[1] / args.config
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    lookback = int(args.lookback if args.lookback is not None else raw.get("lookback", 5))
    if args.shrink_during_confirmation is None:
        shrink_default = bool(raw.get("shrink_during_confirmation", False))
    else:
        shrink_default = args.shrink_during_confirmation == "true"

    try:
        if args.parity or args.all_timeframes:
            # parity on 1m
            data_1m = resolve_data("1m", Path(args.data_file) if args.data_file else None)
            bars = load_bars(data_1m)
            parity_txt = parity_snapshot(
                bars,
                lookback=lookback,
                shrink_during_confirmation=False,
                window=200,
            )
            print(parity_txt)
            print()
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp_p = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
            (RESULTS_DIR / f"ulp_parity_1m_{stamp_p}.txt").write_text(
                parity_txt, encoding="utf-8"
            )

        if args.all_timeframes or args.timeframe is None and args.data_file is None:
            rows: list[dict[str, Any]] = []
            for tf in ("1m", "3m", "5m", "15m"):
                data = resolve_data(tf, None)
                for shrink in (False, True):
                    print("=" * 72)
                    print(f"ULP {tf} shrink_during_confirmation={shrink}")
                    print("=" * 72)
                    summary = run_one(
                        data_file=data,
                        timeframe=tf,
                        lookback=lookback,
                        shrink_during_confirmation=shrink,
                    )
                    # drop heavy objects before table
                    rows.append(
                        {k: v for k, v in summary.items() if k not in {"bars", "zones"}}
                    )
            table = comparison_table(rows)
            stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            tpath = RESULTS_DIR / f"ulp_tf_comparison_{stamp}.txt"
            tpath.write_text(table, encoding="utf-8")
            print(table)
            print(f"comparison: {tpath}")
        else:
            tf = args.timeframe or str(raw.get("timeframe", "1m"))
            data = resolve_data(
                tf, Path(args.data_file) if args.data_file else Path(raw["data_file"])
            )
            run_one(
                data_file=data,
                timeframe=tf,
                lookback=lookback,
                shrink_during_confirmation=shrink_default,
            )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
