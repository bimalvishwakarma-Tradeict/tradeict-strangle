#!/usr/bin/env python3
"""
PART B: May 2026 1m mark vs 5m-resampled mark resolution adequacy.
PART C: Strike-vs-spot band coverage across all print-only cycles.

NO new candle download. Uses existing marks_2026-05.sqlite.
No print(). Output: console + backtest/results/resolution_and_band.txt
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_final_validation as fv  # noqa: E402
import s001_income_engine as eng  # noqa: E402

UTC = timezone.utc
PILOT_YM = "2026-05"
MARKS_DB = _BACKTEST / "cache" / "option_marks" / f"marks_{PILOT_YM}.sqlite"
OUT_PATH = _BACKTEST / "results" / "resolution_and_band.txt"
BAR_5M = 300

logger = logging.getLogger("test_resolution_and_band")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def pctile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    return float(eng.pctile(vals, p))


def summarize_dists(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {
            "n": 0.0,
            "max": float("nan"),
            "p99": float("nan"),
            "p95": float("nan"),
            "p90": float("nan"),
            "median": float("nan"),
        }
    return {
        "n": float(len(vals)),
        "max": float(max(vals)),
        "p99": pctile(vals, 99),
        "p95": pctile(vals, 95),
        "p90": pctile(vals, 90),
        "median": float(statistics.median(vals)),
    }


def strike_from_symbol(symbol: str) -> float | None:
    parsed = ot.parse_symbol(symbol)
    return float(parsed.strike) if parsed is not None else None


def build_index_from_closes(
    by_sym: dict[str, list[tuple[int, float, str, str, float]]],
) -> eng.TradeIndex:
    """by_sym[symbol] = [(ts, close, expiry, opt_type, strike), ...] sorted."""
    idx = eng.TradeIndex()
    for sym, rows in by_sym.items():
        series: list[tuple[float, float, str]] = []
        for ts, close, expiry, opt_type, strike in rows:
            if close is None or float(close) <= 0:
                continue
            px = float(close)
            series.append((float(ts), px, "maker"))
            series.append((float(ts), px, "taker"))
            try:
                exp = date.fromisoformat(str(expiry))
            except ValueError:
                continue
            idx.expiries.add(exp)
            idx.strikes_by_expiry[exp].add(float(strike))
        if series:
            series.sort(key=lambda x: x[0])
            idx.by_symbol[sym] = series
    return idx


def load_1m_mark_rows(
    conn: sqlite3.Connection, symbols: set[str]
) -> dict[str, list[tuple[int, float, float, float, float, str, str, float]]]:
    """symbol -> [(ts, open, high, low, close, expiry, opt_type, strike), ...]"""
    out: dict[str, list[tuple[int, float, float, float, float, str, str, float]]] = {}
    for sym in sorted(symbols):
        rows = conn.execute(
            """
            SELECT ts, open, high, low, close, expiry, opt_type, strike
            FROM marks WHERE symbol=? ORDER BY ts
            """,
            (sym,),
        ).fetchall()
        series: list[tuple[int, float, float, float, float, str, str, float]] = []
        for ts, o, h, l, c, expiry, opt_type, strike in rows:
            if c is None or float(c) <= 0:
                continue
            series.append(
                (
                    int(ts),
                    float(o if o is not None else c),
                    float(h if h is not None else c),
                    float(l if l is not None else c),
                    float(c),
                    str(expiry),
                    str(opt_type),
                    float(strike),
                )
            )
        if series:
            out[sym] = series
    return out


def resample_1m_to_5m(
    rows_1m: list[tuple[int, float, float, float, float, str, str, float]],
) -> list[tuple[int, float, float, float, float, str, str, float]]:
    """Join every 5 consecutive 1m candles into one 5m OHLC bar (bucket by floor ts/300)."""
    buckets: dict[int, list[Any]] = {}
    meta: dict[int, tuple[str, str, float]] = {}
    for ts, o, h, l, c, expiry, opt_type, strike in rows_1m:
        b = (int(ts) // BAR_5M) * BAR_5M
        if b not in buckets:
            buckets[b] = [o, h, l, c]
            meta[b] = (expiry, opt_type, strike)
        else:
            buckets[b][1] = max(buckets[b][1], h)
            buckets[b][2] = min(buckets[b][2], l)
            buckets[b][3] = c  # last close in bucket
    out: list[tuple[int, float, float, float, float, str, str, float]] = []
    for b in sorted(buckets):
        o, h, l, c = buckets[b]
        expiry, opt_type, strike = meta[b]
        out.append((b, float(o), float(h), float(l), float(c), expiry, opt_type, strike))
    return out


def closes_only(
    ohlc: dict[str, list[tuple[int, float, float, float, float, str, str, float]]],
) -> dict[str, list[tuple[int, float, str, str, float]]]:
    return {
        sym: [(ts, c, exp, ot_, k) for ts, _o, _h, _l, c, exp, ot_, k in rows]
        for sym, rows in ohlc.items()
    }


def mark_close_from_series(
    series: list[tuple[int, float, str, str, float]],
    ts: int,
    tol_sec: int,
) -> float | None:
    if not series:
        return None
    minute = (int(ts) // 60) * 60
    # exact
    for t, c, *_ in series:
        if int(t) == minute and c > 0:
            return float(c)
    best: tuple[int, float] | None = None
    for t, c, *_ in series:
        if c <= 0:
            continue
        d = abs(int(t) - minute)
        if d > tol_sec:
            continue
        if best is None or d < best[0]:
            best = (d, float(c))
    return best[1] if best is not None else None


def rebuild_cycle_with_series(
    o: eng.CycleObs,
    by_close: dict[str, list[tuple[int, float, str, str, float]]],
    *,
    tol_sec: int,
) -> eng.CycleObs | None:
    t0 = int(o.entry_utc.timestamp())

    def mk_fill(old: eng.PrintFill | None) -> eng.PrintFill | None:
        if old is None:
            return None
        px = mark_close_from_series(by_close.get(old.symbol, []), t0, tol_sec)
        if px is None:
            return None
        return eng.PrintFill(
            symbol=old.symbol,
            price=float(px),
            ts_utc=old.ts_utc,
            buyer_role=old.buyer_role,
            strike=old.strike,
            option_type=old.option_type,
            source="mark",
        )

    sc = mk_fill(o.short_call)
    sp = mk_fill(o.short_put)
    wc = mk_fill(o.wing_call)
    wp = mk_fill(o.wing_put)
    if sc is None or sp is None or wc is None or wp is None:
        return None
    return eng.CycleObs(
        entry_date=o.entry_date,
        entry_hhmm=o.entry_hhmm,
        entry_utc=o.entry_utc,
        basket_expiry=o.basket_expiry,
        short_dte=o.short_dte,
        fill_package=o.fill_package,
        strike_mode=o.strike_mode,
        wing_points=o.wing_points,
        spot_entry=o.spot_entry,
        spot_settle=o.spot_settle,
        atm_straddle_prem=o.atm_straddle_prem,
        target_premium=o.target_premium,
        short_call_k=o.short_call_k,
        short_put_k=o.short_put_k,
        short_call=sc,
        short_put=sp,
        wing_call_k=o.wing_call_k,
        wing_put_k=o.wing_put_k,
        wing_call=wc,
        wing_put=wp,
        wing_used_surface=False,
        basket_pnl=0.0,
        wings_pnl=0.0,
        entry_fees=0.0,
        settle_fees=0.0,
        net_no_settle=0.0,
        net_with_settle=0.0,
        spot_move_abs=o.spot_move_abs,
    )


def run_cfg(
    cycles: list[eng.CycleObs],
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    collect_for: set[date] | None,
) -> list[fv.SimOut]:
    return fv.run_with_reentry(
        cycles,
        idx,
        times,
        closes,
        trigger_pct=float(fv.CFG["adj_b_trigger_pct"]),
        decrease_pct=float(fv.CFG["adjustment_qty_decrease_pct"]),
        profit_k=float(fv.CFG["profit_target_k"]),
        adj_mode=str(fv.CFG["adjustment_mode"]),
        wing_roll=bool(fv.CFG["wing_roll_with_short_enabled"]),
        slip=0.0,
        allow_reentry=bool(fv.CFG["same_day_reentry"]),
        collect_ledgers_for=collect_for,
    )


def first_by_day(outs: list[fv.SimOut]) -> dict[date, fv.SimOut]:
    out: dict[date, fv.SimOut] = {}
    for s in outs:
        if s.entry_date not in out and math.isfinite(s.net):
            out[s.entry_date] = s
    return out


def adj_enters(sim: fv.SimOut) -> list[tuple[int, float | None, str]]:
    events: list[tuple[int, float | None, str]] = []
    for row in sim.ledger:
        note = (row.note or "").lower()
        if "adj" in note and "enter" in note:
            events.append((int(row.ts), strike_from_symbol(row.symbol), row.note))
    return events


def part_b(
    lines: list[str],
    may: list[eng.CycleObs],
    times: list[int],
    closes: list[float],
) -> None:
    emit(lines, "===== PART B: 1m vs 5m RESAMPLED MARK (May 2026) =====")
    emit(
        lines,
        "Config: dte=2 B_only trigger=70 maker B25 wings=2000 qty=8 "
        "11:00 IST hedge=OFF dec%=40 profit_k=1.0 slip=0 (pure mark)",
    )
    emit(lines, f"marks_db={MARKS_DB}")
    emit(lines, f"monitor_step_sec={sweep.MONITOR_STEP_SEC} (sim already steps 5m)")
    emit(lines, "")

    if not MARKS_DB.is_file():
        emit(lines, f"ERROR: marks DB missing: {MARKS_DB}")
        return

    need: set[str] = set()
    for o in may:
        for leg in (o.short_call, o.short_put, o.wing_call, o.wing_put):
            if leg is not None:
                need.add(leg.symbol)

    conn = sqlite3.connect(f"file:{MARKS_DB}?mode=ro", uri=True)
    try:
        ohlc_1m = load_1m_mark_rows(conn, need)
    finally:
        conn.close()

    closes_1m = closes_only(ohlc_1m)
    ohlc_5m = {sym: resample_1m_to_5m(rows) for sym, rows in ohlc_1m.items()}
    closes_5m = closes_only(ohlc_5m)

    n_1m_bars = sum(len(v) for v in ohlc_1m.values())
    n_5m_bars = sum(len(v) for v in ohlc_5m.values())
    emit(
        lines,
        f"symbols_with_marks={len(ohlc_1m)}/{len(need)} "
        f"1m_bars={n_1m_bars} 5m_bars={n_5m_bars}",
    )

    idx_1m = build_index_from_closes(closes_1m)
    idx_5m = build_index_from_closes(closes_5m)

    may_1m: list[eng.CycleObs] = []
    may_5m: list[eng.CycleObs] = []
    fail_1m = 0
    fail_5m = 0
    for o in may:
        a = rebuild_cycle_with_series(o, closes_1m, tol_sec=120)
        b = rebuild_cycle_with_series(o, closes_5m, tol_sec=360)
        if a is None:
            fail_1m += 1
        else:
            may_1m.append(a)
        if b is None:
            fail_5m += 1
        else:
            may_5m.append(b)

    emit(
        lines,
        f"cycles rebuilt 1m={len(may_1m)} fail={fail_1m} | "
        f"5m={len(may_5m)} fail={fail_5m}",
    )

    days_collect = {o.entry_date for o in may_1m} | {o.entry_date for o in may_5m}
    outs_1m = run_cfg(may_1m, idx_1m, times, closes, collect_for=days_collect)
    outs_5m = run_cfg(may_5m, idx_5m, times, closes, collect_for=days_collect)
    by1 = first_by_day(outs_1m)
    by5 = first_by_day(outs_5m)
    days = sorted(set(by1) & set(by5))
    emit(lines, f"paired cycle days={len(days)}")
    emit(lines, "")
    emit(
        lines,
        f"{'cycle date':<12} {'1m PnL':>12} {'5m PnL':>12} {'farak':>12}",
    )
    emit(lines, "-" * 52)

    faraks: list[float] = []
    pnls_1m: list[float] = []
    time_diff_cycles = 0
    strike_diff_cycles = 0
    minute_diffs: list[float] = []

    for d in days:
        p1 = float(by1[d].net)
        p5 = float(by5[d].net)
        farak = p5 - p1
        faraks.append(farak)
        pnls_1m.append(p1)
        emit(
            lines,
            f"{d.isoformat():<12} {p1:>12.2f} {p5:>12.2f} {farak:>12.2f}",
        )

        e1 = adj_enters(by1[d])
        e5 = adj_enters(by5[d])
        n_pair = min(len(e1), len(e5))
        time_diff = len(e1) != len(e5)
        strike_diff = len(e1) != len(e5)
        cycle_min_diffs: list[float] = []
        for i in range(n_pair):
            t1, k1, _ = e1[i]
            t5, k5, _ = e5[i]
            if t1 != t5:
                time_diff = True
                cycle_min_diffs.append(abs(t5 - t1) / 60.0)
            if k1 is None or k5 is None or abs(float(k1) - float(k5)) > 1e-6:
                strike_diff = True
        if time_diff:
            time_diff_cycles += 1
            if cycle_min_diffs:
                minute_diffs.append(float(statistics.mean(cycle_min_diffs)))
            elif len(e1) != len(e5):
                # count mismatch — no paired minute delta
                pass
        if strike_diff:
            strike_diff_cycles += 1

    emit(lines, "")
    mean_abs_farak = (
        float(statistics.mean(abs(x) for x in faraks)) if faraks else float("nan")
    )
    mean_1m = float(statistics.mean(pnls_1m)) if pnls_1m else float("nan")
    rel_pct = (
        100.0 * mean_abs_farak / abs(mean_1m)
        if faraks and math.isfinite(mean_1m) and abs(mean_1m) > 1e-12
        else float("nan")
    )
    mean_min = float(statistics.mean(minute_diffs)) if minute_diffs else float("nan")
    emit(lines, f"n_paired={len(days)}")
    emit(lines, f"mean_1m_PnL={mean_1m:.4f}")
    emit(lines, f"mean_|farak|={mean_abs_farak:.4f}")
    emit(lines, f"mean_|farak| / |mean_1m_PnL| % = {rel_pct:.4f}")
    emit(lines, f"cycles_adj_TIME_different={time_diff_cycles}")
    emit(
        lines,
        f"mean_|adj_time_diff|_minutes (among cycles with paired ts diffs)="
        f"{mean_min:.4f}  (n={len(minute_diffs)})",
    )
    emit(lines, f"cycles_adj_STRIKE_different={strike_diff_cycles}")
    emit(lines, "")


def part_c(lines: list[str], base: list[eng.CycleObs], times: list[int], closes: list[float]) -> None:
    emit(lines, "===== PART C: STRIKE BAND vs ENTRY SPOT (all usable cycles) =====")
    emit(lines, f"n_cycles={len(base)}")

    short_d: list[float] = []
    wing_d: list[float] = []
    adj_d: list[float] = []

    for o in base:
        spot = float(o.spot_entry) if o.spot_entry is not None else float("nan")
        if not math.isfinite(spot) or spot <= 0:
            continue
        for k in (o.short_call_k, o.short_put_k):
            if k is not None and math.isfinite(float(k)):
                short_d.append(abs(float(k) - spot))
        for k in (o.wing_call_k, o.wing_put_k):
            if k is not None and math.isfinite(float(k)):
                wing_d.append(abs(float(k) - spot))

    # Adjustment legs: run print-path sim with ledgers
    print_idx = eng.build_trade_index()
    collect_days = {o.entry_date for o in base}
    outs = run_cfg(base, print_idx, times, closes, collect_for=collect_days)
    by_day = first_by_day(outs)
    for o in base:
        spot = float(o.spot_entry) if o.spot_entry is not None else float("nan")
        if not math.isfinite(spot) or spot <= 0:
            continue
        sim = by_day.get(o.entry_date)
        if sim is None:
            continue
        # Prefer sim matching this entry_ts if multiple same day
        matched = [
            s
            for s in outs
            if s.entry_date == o.entry_date and int(s.entry_ts) == int(o.entry_utc.timestamp())
        ]
        use = matched[0] if matched else sim
        for _ts, k, _note in adj_enters(use):
            if k is not None and math.isfinite(float(k)):
                adj_d.append(abs(float(k) - spot))

    def emit_block(name: str, vals: list[float]) -> dict[str, float]:
        s = summarize_dists(vals)
        emit(lines, "")
        emit(lines, f"--- {name} (n={int(s['n'])}) ---")
        emit(lines, f"max={s['max']:.2f}")
        emit(lines, f"p99={s['p99']:.2f}")
        emit(lines, f"p95={s['p95']:.2f}")
        emit(lines, f"p90={s['p90']:.2f}")
        emit(lines, f"median={s['median']:.2f}")
        return s

    s_short = emit_block("short legs", short_d)
    s_wing = emit_block("wing legs", wing_d)
    s_adj = emit_block("adjustment legs", adj_d)

    all_d = short_d + wing_d + adj_d
    s_all = summarize_dists(all_d)
    x99 = s_all["p99"]
    # Round up to a clean points band (nearest 100)
    if math.isfinite(x99):
        x_band = int(math.ceil(x99 / 100.0) * 100)
    else:
        x_band = -1
    emit(lines, "")
    emit(lines, f"ALL legs n={int(s_all['n'])} p99={s_all['p99']:.2f} max={s_all['max']:.2f}")
    emit(
        lines,
        f"band ±{x_band} points rakhne se 99% legs cover ho jaati hain "
        f"(raw p99={s_all['p99']:.2f}; short_p99={s_short['p99']:.2f} "
        f"wing_p99={s_wing['p99']:.2f} adj_p99={s_adj['p99']:.2f})",
    )
    emit(lines, "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "RESOLUTION + STRIKE BAND TEST")
    emit(lines, "=" * 80)
    emit(lines, f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    emit(lines, "NO new candle download")
    emit(lines, "")

    all_obs, _day_span = sweep.load_cycles()
    print_idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    base, skipped = fv.filter_and_rebuild_print_cycles(all_obs, print_idx)
    may = [o for o in base if o.entry_date.year == 2026 and o.entry_date.month == 5]
    emit(
        lines,
        f"print-only cycles total={len(base)} skipped={skipped}; "
        f"May 2026={len(may)}",
    )
    emit(lines, "")

    part_b(lines, may, times, closes)
    part_c(lines, base, times, closes)

    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()
    logger.info("wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
