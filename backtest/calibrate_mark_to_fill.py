#!/usr/bin/env python3
"""
Mark-vs-print calibration + May 2026 print-only cycle sanity (mark pipeline).

Requires: backtest/cache/option_marks/marks_2026-05.sqlite
Output: console + backtest/results/mark_pilot_2026-05.txt
No print().
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
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
TRADES_DB = _BACKTEST / "cache" / "options_trades" / f"opt_trades_{PILOT_YM}.sqlite"
OUT_PATH = _BACKTEST / "results" / "mark_pilot_2026-05.txt"

logger = logging.getLogger("calibrate_mark_to_fill")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def pctile(vals: list[float], p: float) -> float:
    return float(eng.pctile(vals, p))


def summarize_pct(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {
            "n": 0.0,
            "mean": float("nan"),
            "median": float("nan"),
            "std": float("nan"),
            "p5": float("nan"),
            "p25": float("nan"),
            "p50": float("nan"),
            "p75": float("nan"),
            "p95": float("nan"),
        }
    return {
        "n": float(len(vals)),
        "mean": float(statistics.fmean(vals)),
        "median": float(statistics.median(vals)),
        "std": float(statistics.pstdev(vals)) if len(vals) > 1 else 0.0,
        "p5": pctile(vals, 5),
        "p25": pctile(vals, 25),
        "p50": pctile(vals, 50),
        "p75": pctile(vals, 75),
        "p95": pctile(vals, 95),
    }


def fmt_sum(s: dict[str, float]) -> str:
    if s["n"] <= 0:
        return "n=0"
    return (
        f"n={int(s['n'])} mean={s['mean']:.4f} med={s['median']:.4f} "
        f"std={s['std']:.4f} "
        f"p5={s['p5']:.4f} p25={s['p25']:.4f} p50={s['p50']:.4f} "
        f"p75={s['p75']:.4f} p95={s['p95']:.4f}"
    )


def load_mark_close_at(
    conn: sqlite3.Connection, symbol: str, ts: int, tol_sec: int = 60
) -> float | None:
    """Exact minute first; else nearest within tol."""
    minute = (ts // 60) * 60
    row = conn.execute(
        "SELECT close FROM marks WHERE symbol=? AND ts=?",
        (symbol, minute),
    ).fetchone()
    if row is not None and row[0] is not None and float(row[0]) > 0:
        return float(row[0])
    row = conn.execute(
        """
        SELECT close, ts FROM marks
        WHERE symbol=? AND ts BETWEEN ? AND ?
        ORDER BY ABS(ts - ?) LIMIT 1
        """,
        (symbol, minute - tol_sec, minute + tol_sec, minute),
    ).fetchone()
    if row is None or row[0] is None or float(row[0]) <= 0:
        return None
    return float(row[0])


def part_b_calibration(lines: list[str]) -> float:
    """Return recommended fill adjustment X in percent (symmetric)."""
    emit(lines, "===== PART B: MARK vs PRINT CALIBRATION (2026-05) =====")
    if not MARKS_DB.is_file():
        emit(lines, f"ERROR: marks DB missing: {MARKS_DB}")
        return float("nan")
    if not TRADES_DB.is_file():
        emit(lines, f"ERROR: trades DB missing: {TRADES_DB}")
        return float("nan")

    marks = sqlite3.connect(f"file:{MARKS_DB}?mode=ro", uri=True)
    # Ensure symbol index for May trades shard (one-time, speeds join)
    trades_rw = sqlite3.connect(str(TRADES_DB))
    try:
        trades_rw.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)"
        )
        trades_rw.commit()
    finally:
        trades_rw.close()
    trades = sqlite3.connect(f"file:{TRADES_DB}?mode=ro", uri=True)
    times, closes = ot.load_spot_1m()

    syms = [
        str(r[0])
        for r in marks.execute("SELECT DISTINCT symbol FROM marks").fetchall()
    ]
    emit(lines, f"mark symbols available: {len(syms)}")

    all_pct: list[float] = []
    buy_pct: list[float] = []
    sell_pct: list[float] = []
    by_prem: dict[str, list[float]] = {
        "<100": [],
        "100-300": [],
        "300-600": [],
        "600+": [],
    }
    atm_pct: list[float] = []
    dotm_pct: list[float] = []
    n_no_mark = 0
    n_seen = 0

    for si, symbol in enumerate(syms, 1):
        mark_map = {
            int(ts): float(close)
            for ts, close in marks.execute(
                "SELECT ts, close FROM marks WHERE symbol=?", (symbol,)
            )
            if close is not None and float(close) > 0
        }
        if not mark_map:
            continue
        trows = trades.execute(
            "SELECT ts, price, role, strike FROM trades WHERE symbol=?",
            (symbol,),
        ).fetchall()
        for ts_f, price, role, strike in trows:
            n_seen += 1
            minute = (int(float(ts_f)) // 60) * 60
            mark = mark_map.get(minute)
            if mark is None:
                # nearest ±60s
                mark = mark_map.get(minute - 60) or mark_map.get(minute + 60)
            if mark is None or mark <= 1e-12:
                n_no_mark += 1
                continue
            px = float(price)
            if px <= 0:
                continue
            diff_pct = 100.0 * (px - mark) / mark
            all_pct.append(diff_pct)
            if int(role) == 1:
                buy_pct.append(diff_pct)
            else:
                sell_pct.append(diff_pct)
            if px < 100:
                by_prem["<100"].append(diff_pct)
            elif px < 300:
                by_prem["100-300"].append(diff_pct)
            elif px < 600:
                by_prem["300-600"].append(diff_pct)
            else:
                by_prem["600+"].append(diff_pct)

            spot = ot.spot_at(times, closes, int(float(ts_f)))
            if spot is not None and spot > 0 and strike is not None:
                mny = abs(float(strike) - float(spot)) / float(spot)
                if mny <= 0.01:
                    atm_pct.append(diff_pct)
                elif mny >= 0.05:
                    dotm_pct.append(diff_pct)
        if si % 50 == 0:
            logger.info("calib symbols %s/%s matched=%s", si, len(syms), len(all_pct))

    emit(lines, f"trades on mark symbols scanned: {n_seen}")
    emit(lines, f"matched mark+print: {len(all_pct)}  minute_miss: {n_no_mark}")
    emit(lines, "")
    emit(lines, f"ALL diff_pct (print-mark)/mark*100: {fmt_sum(summarize_pct(all_pct))}")
    emit(lines, f"BUY prints  (role=taker/buyer):     {fmt_sum(summarize_pct(buy_pct))}")
    emit(lines, f"SELL prints (role=maker/buyer):     {fmt_sum(summarize_pct(sell_pct))}")
    emit(lines, "")
    emit(lines, "premium buckets (diff_pct):")
    for k in ("<100", "100-300", "300-600", "600+"):
        emit(lines, f"  {k:<8} {fmt_sum(summarize_pct(by_prem[k]))}")
    emit(lines, "")
    emit(lines, f"ATM (|k-spot|/spot<=1%):  {fmt_sum(summarize_pct(atm_pct))}")
    emit(lines, f"deep OTM (>=5%):          {fmt_sum(summarize_pct(dotm_pct))}")
    emit(lines, "")

    if sell_pct:
        med_sell = float(statistics.median(sell_pct))
        x_sell = max(0.0, -med_sell)
    else:
        med_sell = float("nan")
        x_sell = float("nan")
    if buy_pct:
        med_buy = float(statistics.median(buy_pct))
        x_buy = max(0.0, med_buy)
    else:
        med_buy = float("nan")
        x_buy = float("nan")

    if math.isfinite(x_sell) and math.isfinite(x_buy):
        x_rec = 0.5 * (x_sell + x_buy)
    elif math.isfinite(x_sell):
        x_rec = x_sell
    elif math.isfinite(x_buy):
        x_rec = x_buy
    else:
        x_rec = float("nan")

    emit(
        lines,
        f"median sell diff_pct={med_sell:.4f} → X_sell={x_sell:.4f}%  |  "
        f"median buy diff_pct={med_buy:.4f} → X_buy={x_buy:.4f}%",
    )
    emit(
        lines,
        f'mark se fill karne pe realistic adjustment {x_rec:.2f}% hai '
        f"(sell pe mark − {x_rec:.2f}%, buy pe mark + {x_rec:.2f}%)",
    )
    emit(lines, "")
    marks.close()
    trades.close()
    return float(x_rec)


def build_mark_trade_index(symbols: set[str]) -> eng.TradeIndex:
    """TradeIndex from mark closes (both maker+taker roles) for listed symbols."""
    idx = eng.TradeIndex()
    if not MARKS_DB.is_file() or not symbols:
        return idx
    conn = sqlite3.connect(f"file:{MARKS_DB}?mode=ro", uri=True)
    for sym in sorted(symbols):
        rows = conn.execute(
            "SELECT ts, close, expiry, opt_type, strike FROM marks WHERE symbol=? ORDER BY ts",
            (sym,),
        ).fetchall()
        if not rows:
            continue
        series: list[tuple[float, float, str]] = []
        for ts, close, expiry, opt_type, strike in rows:
            if close is None or float(close) <= 0:
                continue
            px = float(close)
            # store both roles at same mark mid
            series.append((float(ts), px, "maker"))
            series.append((float(ts), px, "taker"))
            try:
                exp = date.fromisoformat(str(expiry))
            except ValueError:
                continue
            idx.expiries.add(exp)
            idx.strikes_by_expiry[exp].add(float(strike))
        if series:
            # nearest_print expects sorted by ts
            series.sort(key=lambda x: x[0])
            idx.by_symbol[sym] = series
    conn.close()
    return idx


def rebuild_cycle_with_mark_prices(
    o: eng.CycleObs,
    mark_conn: sqlite3.Connection,
) -> eng.CycleObs | None:
    """Copy cycle; replace fill prices with mark close at entry (raw mid)."""
    t0 = int(o.entry_utc.timestamp())

    def mk_fill(old: eng.PrintFill | None) -> eng.PrintFill | None:
        if old is None:
            return None
        px = load_mark_close_at(mark_conn, old.symbol, t0, tol_sec=120)
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


def part_c_sanity(lines: list[str], x_pct: float) -> None:
    emit(lines, "===== PART C: SANITY — May 2026 print-only cycles, mark vs print =====")
    if not math.isfinite(x_pct):
        emit(lines, "SKIP: no calibration X — cannot run mark path.")
        return

    all_obs, day_span = sweep.load_cycles()
    print_idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    base, skipped = fv.filter_and_rebuild_print_cycles(all_obs, print_idx)
    may_base = [o for o in base if o.entry_date.year == 2026 and o.entry_date.month == 5]
    emit(
        lines,
        f"print-only cycles total={len(base)} skipped={skipped}; "
        f"May 2026 entry cycles={len(may_base)}",
    )
    if not may_base:
        emit(lines, "ERROR: no May print-only cycles — cannot sanity check.")
        return

    # Print-based
    print_outs = fv.run_with_reentry(
        may_base,
        print_idx,
        times,
        closes,
        trigger_pct=float(fv.CFG["adj_b_trigger_pct"]),
        decrease_pct=float(fv.CFG["adjustment_qty_decrease_pct"]),
        profit_k=float(fv.CFG["profit_target_k"]),
        adj_mode=str(fv.CFG["adjustment_mode"]),
        wing_roll=bool(fv.CFG["wing_roll_with_short_enabled"]),
        slip=0.0,
        allow_reentry=bool(fv.CFG["same_day_reentry"]),
    )
    # one row per seed cycle day — take first entry of day for side-by-side
    print_by_day: dict[date, float] = {}
    for s in print_outs:
        if s.entry_date not in print_by_day and math.isfinite(s.net):
            print_by_day[s.entry_date] = float(s.net)

    # Mark-based index + cycles
    need_syms: set[str] = set()
    for o in may_base:
        for leg in (o.short_call, o.short_put, o.wing_call, o.wing_put):
            if leg is not None:
                need_syms.add(leg.symbol)
    mark_idx = build_mark_trade_index(need_syms)
    mark_conn = sqlite3.connect(f"file:{MARKS_DB}?mode=ro", uri=True)
    may_mark: list[eng.CycleObs] = []
    n_mark_fail = 0
    for o in may_base:
        mo = rebuild_cycle_with_mark_prices(o, mark_conn)
        if mo is None:
            n_mark_fail += 1
            continue
        may_mark.append(mo)
    mark_conn.close()
    emit(
        lines,
        f"mark cycles rebuilt={len(may_mark)} failed_missing_mark={n_mark_fail} "
        f"symbols_loaded={len(mark_idx.by_symbol)}/{len(need_syms)}",
    )

    slip = max(0.0, float(x_pct) / 100.0)
    emit(lines, f"mark path slip (fill adj) = {x_pct:.4f}% → slip={slip:.6f}")
    mark_outs = fv.run_with_reentry(
        may_mark,
        mark_idx,
        times,
        closes,
        trigger_pct=float(fv.CFG["adj_b_trigger_pct"]),
        decrease_pct=float(fv.CFG["adjustment_qty_decrease_pct"]),
        profit_k=float(fv.CFG["profit_target_k"]),
        adj_mode=str(fv.CFG["adjustment_mode"]),
        wing_roll=bool(fv.CFG["wing_roll_with_short_enabled"]),
        slip=slip,
        allow_reentry=bool(fv.CFG["same_day_reentry"]),
    )
    mark_by_day: dict[date, float] = {}
    for s in mark_outs:
        if s.entry_date not in mark_by_day and math.isfinite(s.net):
            mark_by_day[s.entry_date] = float(s.net)

    days = sorted(set(print_by_day) | set(mark_by_day))
    emit(lines, "")
    emit(
        lines,
        f"{'cycle date':<12} {'print PnL':>12} {'mark PnL':>12} {'farak':>12}",
    )
    emit(lines, "-" * 52)
    faraks: list[float] = []
    for d in days:
        p = print_by_day.get(d, float("nan"))
        m = mark_by_day.get(d, float("nan"))
        if math.isfinite(p) and math.isfinite(m):
            f = m - p
            faraks.append(f)
            emit(lines, f"{d.isoformat():<12} {p:12.4f} {m:12.4f} {f:12.4f}")
        else:
            emit(
                lines,
                f"{d.isoformat():<12} {p if math.isfinite(p) else float('nan'):12.4f} "
                f"{m if math.isfinite(m) else float('nan'):12.4f} {'n/a':>12}",
            )

    emit(lines, "")
    if faraks:
        abs_f = [abs(x) for x in faraks]
        mean_abs = float(statistics.fmean(abs_f))
        med_abs = float(statistics.median(abs_f))
        mean_p = float(statistics.fmean([print_by_day[d] for d in days if d in print_by_day]))
        rel = 100.0 * mean_abs / abs(mean_p) if abs(mean_p) > 1e-9 else float("nan")
        emit(lines, f"n paired={len(faraks)}  mean|farak|={mean_abs:.4f}  med|farak|={med_abs:.4f}")
        emit(lines, f"mean|farak| / |mean print PnL| = {rel:.2f}%")
        # stop rule
        if rel > 50.0 or mean_abs > max(5.0, 0.5 * abs(mean_p)):
            emit(
                lines,
                "VERDICT: BAHUT ALAG — RUKO. Poora download mat shuru karo. "
                "Mark pipeline print-only se match nahi karti.",
            )
        else:
            emit(
                lines,
                "VERDICT: lagbhag match — mark pipeline sanity OK for pilot "
                "(print vs mark side-by-side acceptable).",
            )
    else:
        emit(lines, "VERDICT: no paired cycles — RUKO, investigate missing marks.")
    emit(lines, "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "MARK PILOT 2026-05 — CALIBRATION + SANITY")
    emit(lines, "=" * 80)
    emit(lines, "")

    x_pct = part_b_calibration(lines)
    part_c_sanity(lines, x_pct)

    emit(lines, "DONE.")
    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
