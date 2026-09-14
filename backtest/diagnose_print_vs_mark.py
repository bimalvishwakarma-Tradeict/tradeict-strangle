#!/usr/bin/env python3
"""
Diagnose print-based vs mark-based P&L gap (May 2026 pilot).

NO new mark download. Uses existing marks_2026-05.sqlite.
No print(). Output: console + backtest/results/print_vs_mark_diagnosis.txt
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import calibrate_mark_to_fill as cal  # noqa: E402
import options_trades as ot  # noqa: E402
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_final_validation as fv  # noqa: E402
import s001_income_engine as eng  # noqa: E402

UTC = timezone.utc
PILOT_YM = "2026-05"
MARKS_DB = _BACKTEST / "cache" / "option_marks" / f"marks_{PILOT_YM}.sqlite"
OUT_PATH = _BACKTEST / "results" / "print_vs_mark_diagnosis.txt"

SYM_X = 1.65  # prior symmetric calibration
SELL_X = 1.59  # median sell |diff| from pilot
BUY_X = 1.71  # median buy diff from pilot
WORST_DAY = date(2026, 5, 24)

logger = logging.getLogger("diagnose_print_vs_mark")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def pctile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    return float(eng.pctile(vals, p))


def fmt_ts(ts: int | float | None) -> str:
    if ts is None or not math.isfinite(float(ts)):
        return "n/a"
    return datetime.fromtimestamp(int(ts), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def side_role(side: str) -> str:
    s = side.upper()
    if "SELL" in s:
        return sweep.short_role()
    return sweep.long_role()


def mark_exact(
    conn: sqlite3.Connection, symbol: str, ts: int
) -> float | None:
    minute = (int(ts) // 60) * 60
    row = conn.execute(
        "SELECT close FROM marks WHERE symbol=? AND ts=?",
        (symbol, minute),
    ).fetchone()
    if row is None or row[0] is None or float(row[0]) <= 0:
        return None
    return float(row[0])


def mark_any_count(conn: sqlite3.Connection, symbol: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM marks WHERE symbol=?", (symbol,)
    ).fetchone()
    return int(row[0]) if row else 0


def progress_row(
    conn: sqlite3.Connection, symbol: str
) -> tuple[str, int, str] | None:
    row = conn.execute(
        "SELECT status, n_rows, detail FROM download_progress WHERE symbol=?",
        (symbol,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), int(row[1]), str(row[2] or "")


@dataclass
class FillGap:
    cycle_date: date
    note: str
    symbol: str
    side: str
    required_ts: int
    print_ts: int | None
    gap_sec: float | None
    print_px: float | None
    mark_px: float | None
    diff: float | None
    diff_pct: float | None


def resolve_print_fill(
    idx: eng.TradeIndex, symbol: str, required_ts: int, side: str
) -> eng.PrintFill | None:
    when = datetime.fromtimestamp(int(required_ts), tz=UTC)
    return eng.nearest_print_prefer(
        idx,
        symbol,
        when,
        eng.PRINT_WINDOW_SEC,
        side_role(side),
    )


def load_may_cycles() -> tuple[
    list[eng.CycleObs],
    eng.TradeIndex,
    list[int],
    list[float],
    int,
]:
    all_obs, day_span = sweep.load_cycles()
    print_idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    base, skipped = fv.filter_and_rebuild_print_cycles(all_obs, print_idx)
    may = [o for o in base if o.entry_date.year == 2026 and o.entry_date.month == 5]
    logger.info("May print-only cycles=%s (skipped_rebuild=%s)", len(may), skipped)
    return may, print_idx, times, closes, day_span


def rebuild_mark_cycles(
    may: list[eng.CycleObs], mark_conn: sqlite3.Connection
) -> tuple[list[eng.CycleObs], list[eng.CycleObs], eng.TradeIndex]:
    need: set[str] = set()
    for o in may:
        for leg in (o.short_call, o.short_put, o.wing_call, o.wing_put):
            if leg is not None:
                need.add(leg.symbol)
    mark_idx = cal.build_mark_trade_index(need)
    ok: list[eng.CycleObs] = []
    fail: list[eng.CycleObs] = []
    for o in may:
        mo = cal.rebuild_cycle_with_mark_prices(o, mark_conn)
        if mo is None:
            fail.append(o)
        else:
            ok.append(mo)
    return ok, fail, mark_idx


def run_may(
    cycles: list[eng.CycleObs],
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    *,
    slip: float,
    collect_for: set[date] | None = None,
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
        slip=slip,
        allow_reentry=bool(fv.CFG["same_day_reentry"]),
        collect_ledgers_for=collect_for,
    )


def first_by_day(outs: list[fv.SimOut]) -> dict[date, fv.SimOut]:
    out: dict[date, fv.SimOut] = {}
    for s in outs:
        if s.entry_date not in out and math.isfinite(s.net):
            out[s.entry_date] = s
    return out


def gap_bucket(gap: float) -> str:
    if gap < 60:
        return "<1min"
    if gap < 600:
        return "1-10min"
    if gap < 3600:
        return "10-60min"
    return ">60min"


def part_a(
    lines: list[str],
    paired_days: list[date],
    print_by_day: dict[date, fv.SimOut],
    print_idx: eng.TradeIndex,
    mark_conn: sqlite3.Connection,
) -> None:
    emit(lines, "===== PART A: NEAREST-PRINT TIME GAP =====")
    emit(lines, f"cycles: {', '.join(d.isoformat() for d in paired_days)}")
    emit(lines, "")
    rows: list[FillGap] = []

    for d in paired_days:
        s = print_by_day[d]
        emit(lines, f"--- cycle {d.isoformat()} n_ledger={len(s.ledger)} net={s.net:.4f} ---")
        emit(
            lines,
            f"{'note':<22} {'symbol':<24} {'side':<14} "
            f"{'req_ts':<22} {'print_ts':<22} {'gap_s':>8} "
            f"{'print_px':>10} {'mark_px':>10} {'diff':>10} {'diff%':>8}",
        )
        emit(lines, "-" * 160)
        for led in s.ledger:
            pf = resolve_print_fill(print_idx, led.symbol, led.ts, led.side)
            print_ts = int(pf.ts_utc.timestamp()) if pf is not None else None
            gap = abs(print_ts - led.ts) if print_ts is not None else None
            print_px = float(pf.price) if pf is not None else None
            # ledger price is what sim used (slip=0 → should ≈ print)
            mark_px = mark_exact(mark_conn, led.symbol, led.ts)
            diff = None
            diff_pct = None
            if print_px is not None and mark_px is not None and mark_px > 1e-12:
                diff = print_px - mark_px
                diff_pct = 100.0 * diff / mark_px
            fg = FillGap(
                cycle_date=d,
                note=led.note,
                symbol=led.symbol,
                side=led.side,
                required_ts=int(led.ts),
                print_ts=print_ts,
                gap_sec=float(gap) if gap is not None else None,
                print_px=print_px,
                mark_px=mark_px,
                diff=diff,
                diff_pct=diff_pct,
            )
            rows.append(fg)
            emit(
                lines,
                f"{led.note[:22]:<22} {led.symbol:<24} {led.side:<14} "
                f"{fmt_ts(led.ts):<22} {fmt_ts(print_ts):<22} "
                f"{(gap if gap is not None else float('nan')):8.1f} "
                f"{(print_px if print_px is not None else float('nan')):10.4f} "
                f"{(mark_px if mark_px is not None else float('nan')):10.4f} "
                f"{(diff if diff is not None else float('nan')):10.4f} "
                f"{(diff_pct if diff_pct is not None else float('nan')):8.3f}",
            )
        emit(lines, "")

    gaps = [float(r.gap_sec) for r in rows if r.gap_sec is not None]
    emit(lines, "SUMMARY — gap distribution (seconds)")
    if gaps:
        emit(lines, f"  n fills with print match: {len(gaps)} / {len(rows)}")
        emit(lines, f"  median: {statistics.median(gaps):.2f}")
        emit(lines, f"  p75:    {pctile(gaps, 75):.2f}")
        emit(lines, f"  p90:    {pctile(gaps, 90):.2f}")
        emit(lines, f"  p95:    {pctile(gaps, 95):.2f}")
        emit(lines, f"  max:    {max(gaps):.2f}")
        pct_60 = 100.0 * sum(1 for g in gaps if g <= 60.0) / len(gaps)
        pct_1h = 100.0 * sum(1 for g in gaps if g > 3600.0) / len(gaps)
        emit(lines, f"  % gap <= 60s:   {pct_60:.2f}%")
        emit(lines, f"  % gap > 1 hour: {pct_1h:.2f}%")
    else:
        emit(lines, "  no gaps (no print matches)")
    emit(lines, "")

    emit(lines, "CROSSTAB — gap bucket × median |price diff %|")
    emit(
        lines,
        f"{'bucket':<12} {'n':>6} {'med|diff%|':>12} {'med gap_s':>12}",
    )
    emit(lines, "-" * 46)
    by_b: dict[str, list[FillGap]] = defaultdict(list)
    for r in rows:
        if r.gap_sec is None or r.diff_pct is None:
            continue
        by_b[gap_bucket(float(r.gap_sec))].append(r)
    for b in ("<1min", "1-10min", "10-60min", ">60min"):
        xs = by_b.get(b) or []
        if not xs:
            emit(lines, f"{b:<12} {0:6d} {'n/a':>12} {'n/a':>12}")
            continue
        med_abs = statistics.median([abs(float(x.diff_pct)) for x in xs])
        med_g = statistics.median([float(x.gap_sec) for x in xs if x.gap_sec is not None])
        emit(lines, f"{b:<12} {len(xs):6d} {med_abs:12.4f} {med_g:12.2f}")

    # correlation hint
    paired = [
        (float(r.gap_sec), abs(float(r.diff_pct)))
        for r in rows
        if r.gap_sec is not None and r.diff_pct is not None
    ]
    if len(paired) >= 3:
        # simple Spearman via rank
        gs = [p[0] for p in paired]
        ds = [p[1] for p in paired]

        def ranks(vals: list[float]) -> list[float]:
            order = sorted(range(len(vals)), key=lambda i: vals[i])
            r = [0.0] * len(vals)
            for rank, i in enumerate(order):
                r[i] = float(rank)
            return r

        rg, rd = ranks(gs), ranks(ds)
        mean_g, mean_d = statistics.fmean(rg), statistics.fmean(rd)
        num = sum((a - mean_g) * (b - mean_d) for a, b in zip(rg, rd))
        den_g = math.sqrt(sum((a - mean_g) ** 2 for a in rg))
        den_d = math.sqrt(sum((b - mean_d) ** 2 for b in rd))
        spear = num / (den_g * den_d) if den_g > 0 and den_d > 0 else float("nan")
        emit(
            lines,
            f"Spearman(gap, |diff%|) ≈ {spear:.4f}  "
            f"(>0 ⇒ gap badhne pe price error badhta)",
        )
    emit(lines, "")


def enrich_print_ledger_row(
    led: fv.LedRow, print_idx: eng.TradeIndex
) -> dict[str, Any]:
    pf = resolve_print_fill(print_idx, led.symbol, led.ts, led.side)
    print_ts = int(pf.ts_utc.timestamp()) if pf is not None else None
    gap = abs(print_ts - led.ts) if print_ts is not None else None
    return {
        "required_ts": led.ts,
        "used_print_ts": print_ts,
        "gap_sec": gap,
        "price": led.price,
        "source": led.price_source,
        "symbol": led.symbol,
        "side": led.side,
        "qty": led.qty,
        "fee": led.fee,
        "note": led.note,
        "running": led.running_pnl,
    }


def enrich_mark_ledger_row(
    led: fv.LedRow, mark_conn: sqlite3.Connection, slip: float
) -> dict[str, Any]:
    raw = mark_exact(mark_conn, led.symbol, led.ts)
    # map side for slip direction
    side_key = "sell" if "SELL" in led.side.upper() else "buy"
    adj = fv.apply_slip(float(raw), side=side_key, slip=slip) if raw is not None else None
    return {
        "required_ts": led.ts,
        "mark_close": raw,
        "adjusted_price": adj,
        "price_used": led.price,
        "source": led.price_source if led.price_source else "mark",
        "symbol": led.symbol,
        "side": led.side,
        "qty": led.qty,
        "fee": led.fee,
        "note": led.note,
        "running": led.running_pnl,
    }


def part_b(
    lines: list[str],
    print_out: fv.SimOut,
    mark_out: fv.SimOut,
    print_idx: eng.TradeIndex,
    mark_conn: sqlite3.Connection,
    slip: float,
) -> None:
    emit(lines, "===== PART B: DUAL LEDGER — 2026-05-24 (largest |farak|) =====")
    emit(
        lines,
        f"print net={print_out.net:.6f}  mark net={mark_out.net:.6f}  "
        f"farak={mark_out.net - print_out.net:.6f}",
    )
    emit(lines, "")

    pr = [enrich_print_ledger_row(x, print_idx) for x in print_out.ledger]
    mk = [enrich_mark_ledger_row(x, mark_conn, slip) for x in mark_out.ledger]

    emit(lines, "(a) PRINT-BASED ledger")
    emit(
        lines,
        f"{'#':>3} {'note':<22} {'symbol':<24} {'side':<14} "
        f"{'req_ts':<22} {'used_print_ts':<22} {'gap_s':>8} "
        f"{'price':>10} {'src':<8} {'fee':>8} {'run':>10}",
    )
    emit(lines, "-" * 170)
    for i, r in enumerate(pr, 1):
        emit(
            lines,
            f"{i:3d} {str(r['note'])[:22]:<22} {r['symbol']:<24} {r['side']:<14} "
            f"{fmt_ts(r['required_ts']):<22} {fmt_ts(r['used_print_ts']):<22} "
            f"{(r['gap_sec'] if r['gap_sec'] is not None else float('nan')):8.1f} "
            f"{float(r['price']):10.4f} {str(r['source']):<8} "
            f"{float(r['fee']):8.4f} {float(r['running']):10.4f}",
        )
    emit(lines, f"PRINT total (sim net)={print_out.net:.6f}  "
         f"manual_ledger={print_out.ledger_manual_total:.6f}")
    emit(lines, "")

    emit(lines, "(b) MARK-BASED ledger (symmetric slip "
         f"{100.0 * slip:.4f}%)")
    emit(
        lines,
        f"{'#':>3} {'note':<22} {'symbol':<24} {'side':<14} "
        f"{'req_ts':<22} {'mark_close':>10} {'adj_px':>10} "
        f"{'used':>10} {'src':<8} {'fee':>8} {'run':>10}",
    )
    emit(lines, "-" * 160)
    for i, r in enumerate(mk, 1):
        emit(
            lines,
            f"{i:3d} {str(r['note'])[:22]:<22} {r['symbol']:<24} {r['side']:<14} "
            f"{fmt_ts(r['required_ts']):<22} "
            f"{(r['mark_close'] if r['mark_close'] is not None else float('nan')):10.4f} "
            f"{(r['adjusted_price'] if r['adjusted_price'] is not None else float('nan')):10.4f} "
            f"{float(r['price_used']):10.4f} {str(r['source']):<8} "
            f"{float(r['fee']):8.4f} {float(r['running']):10.4f}",
        )
    emit(lines, f"MARK total (sim net)={mark_out.net:.6f}  "
         f"manual_ledger={mark_out.ledger_manual_total:.6f}")
    emit(lines, "")

    # Path divergence check
    print_syms = [(r["note"], r["symbol"], r["required_ts"]) for r in pr]
    mark_syms = [(r["note"], r["symbol"], r["required_ts"]) for r in mk]
    diverged = print_syms != mark_syms
    if diverged:
        emit(
            lines,
            "CRITICAL: print vs mark PATH DIVERGED — different adj strikes "
            "and/or event times. Index-aligned price Δ is MISLEADING after entry.",
        )
        emit(lines, "PRINT event sequence:")
        for i, (note, sym, ts) in enumerate(print_syms, 1):
            emit(lines, f"  P{i:02d} {fmt_ts(ts)}  {note:<22} {sym}")
        emit(lines, "MARK event sequence:")
        for i, (note, sym, ts) in enumerate(mark_syms, 1):
            emit(lines, f"  M{i:02d} {fmt_ts(ts)}  {note:<22} {sym}")
        emit(lines, "")

    emit(lines, "SIDE-BY-SIDE ENTRY ONLY (same 4 legs — fair price compare)")
    emit(
        lines,
        f"{'note':<22} {'symbol':<24} "
        f"{'print_px':>10} {'mark_used':>10} {'Δpx':>10} {'Δcash≈':>10}",
    )
    emit(lines, "-" * 100)
    for i in range(min(4, len(pr), len(mk))):
        a, b = pr[i], mk[i]
        if a["symbol"] != b["symbol"]:
            emit(lines, f"{a['note']:<22} SYMBOL MISMATCH {a['symbol']} vs {b['symbol']}")
            continue
        pp, mp = float(a["price"]), float(b["price_used"])
        dpx = pp - mp
        qty = int(a["qty"])
        side = str(a["side"])
        sign = -1.0 if "BUY" in side.upper() else 1.0
        dcash = sign * dpx * abs(qty) * eng.CONTRACT_VALUE
        emit(
            lines,
            f"{str(a['note'])[:22]:<22} {a['symbol']:<24} "
            f"{pp:10.4f} {mp:10.4f} {dpx:10.4f} {dcash:10.4f}",
        )
    entry_dcash = 0.0
    for i in range(min(4, len(pr), len(mk))):
        a, b = pr[i], mk[i]
        if a["symbol"] != b["symbol"]:
            continue
        dpx = float(a["price"]) - float(b["price_used"])
        sign = -1.0 if "BUY" in str(a["side"]).upper() else 1.0
        entry_dcash += sign * dpx * abs(int(a["qty"])) * eng.CONTRACT_VALUE
    emit(lines, f"entry-only approx Δcash (print−mark used): {entry_dcash:.4f}")
    emit(
        lines,
        f"TOTALS: print={print_out.net:.6f}  mark={mark_out.net:.6f}  "
        f"farak(mark-print)={mark_out.net - print_out.net:.6f}",
    )
    emit(
        lines,
        "Farak ka bada hissa PATH divergence (alag adj strikes/timing) se "
        "aa sakta hai — sirf entry fill noise se nahi.",
    )
    emit(lines, "")


def part_c(
    lines: list[str],
    may_mark: list[eng.CycleObs],
    mark_idx: eng.TradeIndex,
    print_by_day: dict[date, float],
    times: list[int],
    closes: list[float],
    paired_days: list[date],
) -> None:
    emit(lines, "===== PART C: 1.65% vs ASYM vs PURE MARK =====")
    emit(lines, f"SELL_X={SELL_X}%  BUY_X={BUY_X}%  SYM_X={SYM_X}%")
    emit(lines, "")

    variants: list[tuple[str, float | None]] = [
        ("(1) symmetric 1.65%", SYM_X / 100.0),
        ("(2) asymmetric sell-1.59 / buy+1.71", None),  # monkeypatch
        ("(3) pure mark (no adj)", 0.0),
    ]

    print_mean = statistics.fmean([print_by_day[d] for d in paired_days])
    emit(lines, f"print-based mean PnL over {len(paired_days)} paired days: {print_mean:.6f}")
    emit(
        lines,
        f"{'variant':<42} {'mean PnL':>10} {'vs print':>10} {'n':>4}",
    )
    emit(lines, "-" * 70)

    orig_slip = fv.apply_slip

    def asym_slip(price: float, *, side: str, slip: float) -> float:
        p = float(price)
        if side == "sell":
            return p * (1.0 - SELL_X / 100.0)
        return p * (1.0 + BUY_X / 100.0)

    for name, slip in variants:
        if slip is None:
            fv.apply_slip = asym_slip  # type: ignore[assignment]
            use_slip = 0.0165  # ignored by asym_slip but kept non-zero path
            # Actually asym_slip ignores slip; simulate still calls apply_slip(..., slip=use_slip)
            # Use use_slip=1.0 so code enters slip paths? No — apply_slip checks s<=0 on the
            # slip ARG before our monkeypatch replaces the whole function. Our asym ignores slip.
            use_slip = 0.01  # any >0; asym_slip ignores it
        else:
            fv.apply_slip = orig_slip
            use_slip = float(slip)

        outs = run_may(may_mark, mark_idx, times, closes, slip=use_slip)
        by = first_by_day(outs)
        nets = [float(by[d].net) for d in paired_days if d in by]
        mean_m = statistics.fmean(nets) if nets else float("nan")
        farak = mean_m - print_mean if nets else float("nan")
        emit(lines, f"{name:<42} {mean_m:10.4f} {farak:10.4f} {len(nets):4d}")
        # per-day
        for d in paired_days:
            p = print_by_day.get(d, float("nan"))
            m = float(by[d].net) if d in by else float("nan")
            emit(
                lines,
                f"    {d.isoformat()}  print={p:8.4f}  mark={m:8.4f}  "
                f"farak={m - p if math.isfinite(m) and math.isfinite(p) else float('nan'):8.4f}",
            )

    fv.apply_slip = orig_slip
    emit(lines, "")
    emit(
        lines,
        "NOTE: agar (1)≈(2) aur dono (3) se alag → farak mostly pricing source/"
        "path; agar (3) print ke paas → adjustment zyada zimmedar.",
    )
    emit(lines, "")


def product_lookup(symbol: str) -> dict[str, Any]:
    """Tiny non-bulk probe: GET /v2/products/{symbol}."""
    url = f"https://api.india.delta.exchange/v2/products/{symbol}"
    try:
        with httpx.Client() as client:
            resp = client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Tradeict-PrintMarkDiag/1.0",
                },
                timeout=30.0,
            )
            time.sleep(0.75)
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            return {
                "status": resp.status_code,
                "body": payload if payload is not None else resp.text[:500],
            }
    except httpx.HTTPError as exc:
        return {"status": 0, "body": f"HTTPError: {exc}"}


def part_d(
    lines: list[str],
    fail_cycles: list[eng.CycleObs],
    mark_conn: sqlite3.Connection,
) -> None:
    emit(lines, "===== PART D: MISSING WINGS (nan mark cycles) =====")
    if not fail_cycles:
        emit(lines, "No failed mark rebuilds.")
        emit(lines, "")
        return

    for o in fail_cycles:
        emit(lines, f"--- cycle entry={o.entry_date.isoformat()} "
             f"expiry={o.basket_expiry} entry_utc={o.entry_utc} ---")
        t0 = int(o.entry_utc.timestamp())
        minute = (t0 // 60) * 60
        legs = [
            ("short_call", o.short_call),
            ("short_put", o.short_put),
            ("wing_call", o.wing_call),
            ("wing_put", o.wing_put),
        ]
        for name, fill in legs:
            if fill is None:
                emit(lines, f"  {name}: FILL IS None")
                continue
            sym = fill.symbol
            mx = mark_exact(mark_conn, sym, t0)
            n_rows = mark_any_count(mark_conn, sym)
            prog = progress_row(mark_conn, sym)
            emit(
                lines,
                f"  {name}: symbol={sym} strike={fill.strike} "
                f"print_px={fill.price:.4f}",
            )
            emit(
                lines,
                f"    mark@exact_minute {fmt_ts(minute)}: "
                f"{'FOUND '+str(mx) if mx is not None else 'MISSING'}",
            )
            emit(lines, f"    rows in marks table for symbol: {n_rows}")
            emit(lines, f"    download_progress: {prog}")

            # Why not downloaded?
            if mx is not None and n_rows > 0:
                emit(
                    lines,
                    "    CONCLUSION: mark OK at entry minute — not the failing leg.",
                )
            elif n_rows == 0:
                emit(lines, "    WHY: no mark rows — checking /v2/products/{symbol} ...")
                info = product_lookup(sym)
                emit(lines, f"    products status={info['status']}")
                body = info["body"]
                if isinstance(body, dict):
                    res = body.get("result")
                    if isinstance(res, dict):
                        sett = str(res.get("settlement_time") or "")
                        sett_day = sett[:10]
                        emit(
                            lines,
                            f"    product state={res.get('state')} "
                            f"settlement_time={sett} "
                            f"strike={res.get('strike_price')} "
                            f"contract_type={res.get('contract_type')}",
                        )
                        spot = float(o.spot_entry)
                        try:
                            k = float(res.get("strike_price"))
                        except (TypeError, ValueError):
                            k = float(fill.strike)
                        band_ok = abs(k - spot) <= 6000.0
                        emit(
                            lines,
                            f"    entry spot={spot:.2f} strike={k:.2f} "
                            f"|Δ|={abs(k - spot):.2f} within ±6000? {band_ok}",
                        )
                        in_may = sett_day.startswith("2026-05")
                        emit(
                            lines,
                            f"    settlement in pilot month 2026-05? {in_may} "
                            f"(downloader keeps only May settlement_time)",
                        )
                        if not in_may:
                            emit(
                                lines,
                                "    CONCLUSION: symbol EXISTS on Delta but settlement "
                                "OUTSIDE May → May pilot products filter EXCLUDED it "
                                "(by design). Not a ±6000 miss.",
                            )
                        elif not band_ok:
                            emit(
                                lines,
                                "    CONCLUSION: May settlement + EXISTS but OUTSIDE "
                                "±6000 strike band → intentionally not downloaded.",
                            )
                        elif prog is None:
                            emit(
                                lines,
                                "    CONCLUSION: in May products scope + band OK but "
                                "NOT in download_progress — pagination/filter miss "
                                "or never queued.",
                            )
                        else:
                            emit(
                                lines,
                                f"    CONCLUSION: progress={prog[0]} detail={prog[2]} "
                                "— download attempted but no usable rows.",
                            )
                    else:
                        emit(lines, f"    body sample: {str(body)[:400]}")
                        emit(
                            lines,
                            "    CONCLUSION: symbol NOT found / empty result on "
                            "/v2/products/{symbol}.",
                        )
                else:
                    emit(lines, f"    body: {body}")
            else:
                ts_rng = mark_conn.execute(
                    "SELECT MIN(ts), MAX(ts) FROM marks WHERE symbol=?",
                    (sym,),
                ).fetchone()
                emit(
                    lines,
                    f"    mark ts range: {fmt_ts(ts_rng[0])} -> {fmt_ts(ts_rng[1])}",
                )
                near = mark_conn.execute(
                    """
                    SELECT ts, close FROM marks
                    WHERE symbol=? AND ts BETWEEN ? AND ?
                    ORDER BY ABS(ts-?) LIMIT 3
                    """,
                    (sym, minute - 3600, minute + 3600, minute),
                ).fetchall()
                emit(lines, f"    nearest marks within ±1h: {near}")
                emit(
                    lines,
                    "    CONCLUSION: symbol downloaded but EXACT entry minute "
                    "mark missing (gap in 1m series or outside contract_window).",
                )
        emit(lines, "")
    emit(lines, "")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "PRINT vs MARK DIAGNOSIS — 2026-05")
    emit(lines, "=" * 90)
    emit(lines, f"marks db: {MARKS_DB} exists={MARKS_DB.is_file()}")
    emit(lines, "NO new download — diagnose only.")
    emit(lines, "")

    if not MARKS_DB.is_file():
        emit(lines, "ERROR: marks DB missing — cannot diagnose.")
        text = "\n".join(lines) + "\n"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        return 1

    may, print_idx, times, closes, _day_span = load_may_cycles()
    mark_conn = sqlite3.connect(f"file:{MARKS_DB}?mode=ro", uri=True)
    may_mark, fail_cycles, mark_idx = rebuild_mark_cycles(may, mark_conn)
    emit(
        lines,
        f"May cycles: {len(may)}  mark_ok={len(may_mark)}  "
        f"mark_fail={len(fail_cycles)} "
        f"fail_dates={[c.entry_date.isoformat() for c in fail_cycles]}",
    )
    emit(lines, "")

    # Print path with ledgers for all May days we care about
    collect_days = {o.entry_date for o in may_mark} | {WORST_DAY}
    print_outs = run_may(
        may, print_idx, times, closes, slip=0.0, collect_for=collect_days
    )
    print_by = first_by_day(print_outs)
    print_pnl = {d: float(s.net) for d, s in print_by.items()}

    slip_sym = SYM_X / 100.0
    mark_outs = run_may(
        may_mark,
        mark_idx,
        times,
        closes,
        slip=slip_sym,
        collect_for={WORST_DAY},
    )
    mark_by = first_by_day(mark_outs)

    paired = sorted(set(print_by) & set(mark_by))
    emit(lines, "Paired May days (print+mark):")
    for d in paired:
        f = mark_by[d].net - print_by[d].net
        emit(
            lines,
            f"  {d.isoformat()}  print={print_by[d].net:.4f}  "
            f"mark={mark_by[d].net:.4f}  farak={f:.4f}",
        )
    emit(lines, "")

    part_a(lines, paired, print_by, print_idx, mark_conn)

    if WORST_DAY not in print_by or WORST_DAY not in mark_by:
        emit(lines, f"ERROR: {WORST_DAY} missing from paired — cannot Part B")
    else:
        part_b(
            lines,
            print_by[WORST_DAY],
            mark_by[WORST_DAY],
            print_idx,
            mark_conn,
            slip_sym,
        )

    part_c(
        lines,
        may_mark,
        mark_idx,
        print_pnl,
        times,
        closes,
        paired,
    )
    part_d(lines, fail_cycles, mark_conn)

    emit(lines, "DONE.")
    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    logger.info("Wrote %s", OUT_PATH)
    mark_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
