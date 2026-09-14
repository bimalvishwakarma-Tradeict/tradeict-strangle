#!/usr/bin/env python3
"""
S001 hedge integration — real-print hedge bleed + WINNER basket combined P&L.

Diagnostic / measurement only. No sweep, no optimization, no IV surface for hedge.
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import options_trades as ot  # noqa: E402
import s001_adjustment_sweep as sweep  # noqa: E402
import s001_income_engine as eng  # noqa: E402

logger = logging.getLogger("s001_hedge_integration")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_hedge_integration.txt"

# Live-config values used for reconstruction (task + S001_CONFIG_SEMANTICS §6
# example). Code defaults differ — reported in PART 1.
HEDGE_QTY_LOTS = 4
MIN_HEDGE_DTE = 15
ROLL_DTE = 3
HARD_DTE = 2
MIN_HOLD_DAYS = 10
ENTRY_HHMM = (11, 0)
HEDGE_ENTRY_WINDOW_SEC = 30 * 60.0
HEDGE_EXIT_WINDOW_SEC = 60 * 60.0
CONTRACT_VALUE = eng.CONTRACT_VALUE
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
WINNER_MEAN_CITED = 0.90  # from prior adjustment sweep / stress


@dataclass
class HedgeCycle:
    status: str  # OK | PRINT_UNAVAILABLE
    entry_date: date | None
    exit_date: date | None
    expiry: date | None
    atm: float | None
    entry_prem_usd: float | None  # total debit for 4-lot straddle
    exit_prem_usd: float | None
    days_held: int | None
    realized_pnl_usd: float | None
    bleed_per_day: float | None  # (entry - exit) / days_held
    index_entry: float | None
    index_exit: float | None
    index_move: float | None
    reason: str = ""


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def last_friday_of_month(year: int, month: int) -> date:
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 4) % 7)


def resolve_month_1(entry: date, expiries: set[date]) -> date | None:
    """
    Offline proxy for Delta label month_1 + min_hedge_dte advance.
    Mirrors backtest/s001_engine_measure.py:120-133 and live
    resolve_hedge_expiry_date + enforce_min_hedge_dte behaviour.
    """
    monthlies = sorted(
        e
        for e in expiries
        if e == last_friday_of_month(e.year, e.month) and e > entry
    )
    if not monthlies:
        return None
    for m in monthlies:
        if (m - entry).days >= MIN_HEDGE_DTE:
            return m
    return monthlies[-1]


def prem_usd(call_px: float, put_px: float, qty: int) -> float:
    return (float(call_px) + float(put_px)) * abs(int(qty)) * CONTRACT_VALUE


def long_role() -> str:
    # Buying hedge: under maker fill_package shorts are maker; longs take taker
    return eng.roles_for_package("maker")[1]


def pick_atm(strikes: set[float], spot: float) -> float | None:
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda k: (abs(k - spot), k))


def fill_pair(
    idx: eng.TradeIndex,
    exp: date,
    atm: float,
    when: datetime,
    window: float,
) -> tuple[eng.PrintFill, eng.PrintFill] | None:
    role = long_role()
    cf = eng.nearest_print_prefer(
        idx, eng.format_symbol("C", atm, exp), when, window, role
    )
    pf = eng.nearest_print_prefer(
        idx, eng.format_symbol("P", atm, exp), when, window, role
    )
    if cf is None or pf is None or cf.price <= 0 or pf.price <= 0:
        return None
    return cf, pf


def reconstruct_hedge_cycles(
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
) -> list[HedgeCycle]:
    """
    Walk the sample chronologically:
      open month_1 ATM long straddle on prints → hold until calendar_dte <= ROLL_DTE
      (with no open baskets, soft execute closes immediately — hedge_lifecycle
       :3251-3263) → reopen next month_1.
    No surface fills. Misses → PRINT_UNAVAILABLE.
    """
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()
    out: list[HedgeCycle] = []
    cursor = d0
    safety = 0
    while cursor <= d1 and safety < 500:
        safety += 1
        exp = resolve_month_1(cursor, idx.expiries)
        if exp is None:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=cursor,
                    exit_date=None,
                    expiry=None,
                    atm=None,
                    entry_prem_usd=None,
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=None,
                    index_exit=None,
                    index_move=None,
                    reason="no month_1 expiry resolvable from shard expiries",
                )
            )
            cursor += timedelta(days=1)
            continue

        # Soft-roll exit day: first calendar day with DTE <= ROLL_DTE
        exit_day = exp - timedelta(days=ROLL_DTE)
        if exit_day <= cursor:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=cursor,
                    exit_date=exit_day,
                    expiry=exp,
                    atm=None,
                    entry_prem_usd=None,
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=None,
                    index_exit=None,
                    index_move=None,
                    reason=(
                        f"exit_day {exit_day} <= entry cursor {cursor} "
                        f"(expiry={exp}, roll_dte={ROLL_DTE})"
                    ),
                )
            )
            cursor = max(cursor + timedelta(days=1), exit_day + timedelta(days=1))
            continue

        # Find first entry day in [cursor, exit_day) with ATM prints at 11:00 IST
        entry_day: date | None = None
        entry_fills: tuple[eng.PrintFill, eng.PrintFill] | None = None
        atm: float | None = None
        spot_e: float | None = None
        entry_utc: datetime | None = None
        scan = cursor
        while scan < exit_day:
            entry_utc = datetime(
                scan.year, scan.month, scan.day, ENTRY_HHMM[0], ENTRY_HHMM[1],
                tzinfo=IST,
            ).astimezone(UTC)
            ts = int(entry_utc.timestamp())
            if ts < times[0] or ts > times[-1]:
                scan += timedelta(days=1)
                continue
            spot = ot.spot_at(times, closes, ts)
            if spot is None or spot <= 0:
                scan += timedelta(days=1)
                continue
            strikes = idx.strikes_by_expiry.get(exp) or set()
            atm_try = pick_atm(strikes, float(spot))
            if atm_try is None:
                scan += timedelta(days=1)
                continue
            fills = fill_pair(
                idx, exp, atm_try, entry_utc, HEDGE_ENTRY_WINDOW_SEC
            )
            if fills is None:
                scan += timedelta(days=1)
                continue
            entry_day = scan
            entry_fills = fills
            atm = atm_try
            spot_e = float(spot)
            break

        if entry_day is None or entry_fills is None or atm is None or spot_e is None:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=cursor,
                    exit_date=exit_day,
                    expiry=exp,
                    atm=None,
                    entry_prem_usd=None,
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=None,
                    index_exit=None,
                    index_move=None,
                    reason=(
                        f"no ATM long-straddle prints for expiry={exp} "
                        f"in [{cursor}, {exit_day})"
                    ),
                )
            )
            cursor = exit_day + timedelta(days=1)
            continue

        exit_utc = datetime(
            exit_day.year, exit_day.month, exit_day.day,
            ENTRY_HHMM[0], ENTRY_HHMM[1], tzinfo=IST,
        ).astimezone(UTC)
        ts_x = int(exit_utc.timestamp())
        spot_x = ot.spot_at(times, closes, ts_x) if times[0] <= ts_x <= times[-1] else None
        exit_fills = fill_pair(
            idx, exp, atm, exit_utc, HEDGE_EXIT_WINDOW_SEC
        )
        if exit_fills is None or spot_x is None or spot_x <= 0:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=entry_day,
                    exit_date=exit_day,
                    expiry=exp,
                    atm=atm,
                    entry_prem_usd=prem_usd(
                        entry_fills[0].price, entry_fills[1].price, HEDGE_QTY_LOTS
                    ),
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=spot_e,
                    index_exit=float(spot_x) if spot_x else None,
                    index_move=None,
                    reason=(
                        f"exit prints unavailable for ATM={atm:.0f} "
                        f"exp={exp} on {exit_day}"
                    ),
                )
            )
            cursor = exit_day + timedelta(days=1)
            continue

        ec, ep = entry_fills
        xc, xp = exit_fills
        entry_usd = prem_usd(ec.price, ep.price, HEDGE_QTY_LOTS)
        exit_usd = prem_usd(xc.price, xp.price, HEDGE_QTY_LOTS)
        # Long straddle realized
        pnl = (
            eng.cash_pnl(ec.price, xc.price, HEDGE_QTY_LOTS, is_long=True)
            + eng.cash_pnl(ep.price, xp.price, HEDGE_QTY_LOTS, is_long=True)
        )
        days_held = max(1, (exit_day - entry_day).days)
        bleed = (entry_usd - exit_usd) / float(days_held)
        out.append(
            HedgeCycle(
                status="OK",
                entry_date=entry_day,
                exit_date=exit_day,
                expiry=exp,
                atm=atm,
                entry_prem_usd=entry_usd,
                exit_prem_usd=exit_usd,
                days_held=days_held,
                realized_pnl_usd=pnl,
                bleed_per_day=bleed,
                index_entry=spot_e,
                index_exit=float(spot_x),
                index_move=float(spot_x) - spot_e,
                reason="ok",
            )
        )
        # Auto-reopen after roll: next search starts on exit day
        cursor = exit_day

    return out


def active_bleed_on_day(cycles: list[HedgeCycle], d: date) -> float | None:
    for c in cycles:
        if c.status != "OK" or c.entry_date is None or c.exit_date is None:
            continue
        if c.entry_date <= d < c.exit_date and c.bleed_per_day is not None:
            return float(c.bleed_per_day)
    return None


def part1_lines() -> list[str]:
    lines: list[str] = []
    emit(lines, "===== PART 1: HEDGE STRUCTURE (from live code — not guessed) =====")
    emit(lines, "")
    emit(lines, "1) hedge_expiry_mode = month_1 — what expiry?")
    emit(
        lines,
        "   resolve_hedge_expiry_date (backend/core/hedge_theta.py:128-201):",
    )
    emit(
        lines,
        "   fetches Delta get_available_expiries, picks row where key == 'month_1'.",
    )
    emit(
        lines,
        "   Key assignment (backend/core/time_utils.py:480-514 get_expiry_label_key):",
    )
    emit(
        lines,
        "   month_N = N-th upcoming last-Friday monthly in the future expiry list.",
    )
    emit(
        lines,
        "   So month_1 = nearest upcoming monthly (last Friday of a month) still listed.",
    )
    emit(
        lines,
        "   Then open_hedge may call enforce_min_hedge_dte "
        "(hedge_theta.py:204+, wired hedge_lifecycle.py:686-697)",
    )
    emit(
        lines,
        f"   when min_hedge_dte_enabled: if calendar DTE < min_hedge_dte "
        f"(default {MIN_HEDGE_DTE}), advance to a further monthly.",
    )
    emit(lines, "")
    emit(lines, "2) Structure — single option or straddle/strangle?")
    emit(
        lines,
        "   LONG ATM STRADDLE (buy call + buy put, same strike).",
    )
    emit(
        lines,
        "   open_hedge docstring + ATM resolve: "
        "backend/engine/hedge_lifecycle.py:616-617, 717-732.",
    )
    emit(
        lines,
        "   Legs persisted as call_* and put_* on HedgePosition "
        "(same file ~1019+); not a single option, not a strangle.",
    )
    emit(lines, "")
    emit(lines, "3) Strike selection")
    emit(
        lines,
        "   annotate_atm(chain, spot) → ATM strike nearest spot "
        "(hedge_lifecycle.py:717-722).",
    )
    emit(
        lines,
        "   Both call_product_id and put_product_id taken from that ATM row.",
    )
    emit(lines, "")
    emit(lines, "4) hedge_qty_lots = 4 — per leg or total?")
    emit(
        lines,
        "   PER LEG (same qty on call and put).",
    )
    emit(
        lines,
        "   auto_trade_engine.py:2998-3059 (pct_of_hedge path):",
    )
    emit(
        lines,
        "   hedge_qty = max(1, int(settings.hedge_qty_lots)); "
        "open_hedge(..., quantity_override=hedge_qty).",
    )
    emit(
        lines,
        "   open_hedge uses that qty for BOTH legs "
        "(hedge_lifecycle.py:627-634, 993+, HedgePosition.quantity=qty).",
    )
    emit(
        lines,
        "   So hedge_qty_lots=4 → 4-lot long call + 4-lot long put (not 4 total).",
    )
    emit(lines, "")
    emit(lines, "5) Roll timing — hedge_roll_dte / hard / min_hold")
    emit(
        lines,
        "   CODE DEFAULTS (models.py / routes_auto_trade.py Field defaults):",
    )
    emit(
        lines,
        "   hedge_roll_dte default=10 (models.py ~393; routes_auto_trade.py:90)",
    )
    emit(
        lines,
        "   hedge_roll_hard_dte default=5 (models.py ~395; routes_auto_trade.py:91)",
    )
    emit(
        lines,
        "   hedge_min_hold_days default=10 (models.py ~422; routes_auto_trade.py:95)",
    )
    emit(
        lines,
        "   LIVE DB values: NOT AVAILABLE in this repo session "
        "(no trading.db here) — do not assume.",
    )
    emit(
        lines,
        "   TASK / S001_CONFIG_SEMANTICS.md §6 EXAMPLE values: "
        "roll_dte=3, hard_dte=2, min_hold=10.",
    )
    emit(lines, "   Exact roll behaviour (hedge_lifecycle.py:3183-3263):")
    emit(
        lines,
        "   - calendar_dte <= roll_dte and status=active → status=pending_close",
    )
    emit(
        lines,
        "   - pending_close + calendar_dte <= hard_dte + force enabled "
        "→ close HEDGE_ROLL",
    )
    emit(
        lines,
        "   - pending_close + zero open baskets → close HEDGE_ROLL "
        "(soft execute)",
    )
    emit(
        lines,
        "   - pending_close + open baskets + dte > hard → WAIT "
        "(do not close yet)",
    )
    emit(
        lines,
        "   min_hold blocks STRUCTURE TARGET only "
        "(hedge_lifecycle.py:2841-2890); does NOT block roll/SL/expiry.",
    )
    emit(
        lines,
        "   After HEDGE_ROLL close, maybe_auto_reopen_after_roll "
        "(:1937-2013) opens next hedge if flags allow.",
    )
    emit(lines, "")
    emit(
        lines,
        f"THIS MEASUREMENT uses roll_dte={ROLL_DTE}, hard_dte={HARD_DTE}, "
        f"min_hold={MIN_HOLD_DAYS}, min_hedge_dte={MIN_HEDGE_DTE}, "
        f"qty={HEDGE_QTY_LOTS}/leg — matching the task example "
        "(not the code Field defaults of 10/5).",
    )
    emit(
        lines,
        "Standalone hedge (no baskets): soft execute closes on first day "
        f"calendar_dte <= {ROLL_DTE}.",
    )
    emit(lines, "")
    return lines


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 HEDGE INTEGRATION — diagnostic + measurement")
    emit(lines, "=" * 100)
    emit(lines, "")

    # ----- PART 1 -----
    lines.extend(part1_lines())

    logger.info("Building trade index (no new download)...")
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()
    day_span = max(1, (times[-1] - times[0]) // 86400)

    # ----- PART 2 -----
    emit(lines, "===== PART 2: REAL HEDGE CYCLES (prints only) =====")
    emit(
        lines,
        f"qty={HEDGE_QTY_LOTS}/leg  entry={ENTRY_HHMM[0]:02d}:{ENTRY_HHMM[1]:02d} IST  "
        f"entry_window={HEDGE_ENTRY_WINDOW_SEC:.0f}s  "
        f"exit_window={HEDGE_EXIT_WINDOW_SEC:.0f}s  "
        f"roll_dte={ROLL_DTE}  min_hedge_dte={MIN_HEDGE_DTE}",
    )
    emit(lines, "IV surface: NOT USED for hedge (rule).")
    emit(lines, "")

    logger.info("Reconstructing hedge cycles...")
    hedge_cycles = reconstruct_hedge_cycles(idx, times, closes)
    ok = [c for c in hedge_cycles if c.status == "OK"]
    bad = [c for c in hedge_cycles if c.status != "OK"]

    emit(
        lines,
        f"{'status':<18} {'entry':<12} {'exit':<12} {'expiry':<12} {'atm':>7} "
        f"{'entry$':>9} {'exit$':>9} {'days':>5} {'pnl$':>9} {'bleed/d':>9} "
        f"{'d_idx':>9}  note",
    )
    emit(lines, "-" * 140)
    for c in hedge_cycles:
        emit(
            lines,
            f"{c.status:<18} "
            f"{na_date(c.entry_date):<12} "
            f"{na_date(c.exit_date):<12} "
            f"{na_date(c.expiry):<12} "
            f"{na_f(c.atm, 0):>7} "
            f"{na_f(c.entry_prem_usd):>9} "
            f"{na_f(c.exit_prem_usd):>9} "
            f"{na_i(c.days_held):>5} "
            f"{na_f(c.realized_pnl_usd):>9} "
            f"{na_f(c.bleed_per_day):>9} "
            f"{na_f(c.index_move, 1):>9}  "
            f"{c.reason}",
        )

    emit(lines, "")
    emit(
        lines,
        f"Cycles OK (real prints): {len(ok)}  |  PRINT_UNAVAILABLE: {len(bad)}  |  "
        f"attempts listed: {len(hedge_cycles)}",
    )
    if ok:
        bleeds = [float(c.bleed_per_day) for c in ok if c.bleed_per_day is not None]
        pnls = [float(c.realized_pnl_usd) for c in ok if c.realized_pnl_usd is not None]
        emit(lines, "OK-cycle summary:")
        emit(
            lines,
            f"  bleed/day  mean={statistics.mean(bleeds):.6f}  "
            f"median={statistics.median(bleeds):.6f}  "
            f"min={min(bleeds):.6f}  max={max(bleeds):.6f}",
        )
        emit(
            lines,
            f"  realized$  mean={statistics.mean(pnls):.4f}  "
            f"median={statistics.median(pnls):.4f}  "
            f"min={min(pnls):.4f}  max={max(pnls):.4f}",
        )
        emit(
            lines,
            f"  days_held  mean={statistics.mean([c.days_held for c in ok if c.days_held]):.1f}",
        )
    else:
        emit(lines, "OK-cycle summary: NOT AVAILABLE (zero print-complete cycles)")
    emit(lines, "")

    # ----- PART 3 -----
    emit(lines, "===== PART 3: COMBINED P&L (WINNER basket + hedge bleed) =====")
    emit(
        lines,
        "WINNER = dte2 B_only trig70 maker B25 wing2000 qty8 entry 11:00 IST",
    )
    emit(
        lines,
        "Per day: combined = basket_cycle_net - hedge_bleed_that_day "
        "(only days with BOTH).",
    )
    emit(lines, "")

    logger.info("Loading basket cycle cache + simulating WINNER...")
    all_obs, _ = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)
    winner_by_day: dict[date, float] = {}
    for i, o in enumerate(base):
        if (i + 1) % 50 == 0:
            logger.info("  winner sim %s/%s", i + 1, len(base))
        r = sweep.simulate_with_adjustments(o, WINNER_CFG, idx, times, closes)
        winner_by_day[o.entry_date] = float(r.net)

    combined_days: list[tuple[date, float, float, float]] = []
    # (date, basket, bleed, combined)
    skipped_basket_only = 0
    skipped_hedge_only = 0
    for d, bnet in sorted(winner_by_day.items()):
        bleed = active_bleed_on_day(ok, d)
        if bleed is None:
            skipped_basket_only += 1
            continue
        combined_days.append((d, bnet, bleed, bnet - bleed))

    # Count hedge-active days without basket
    if ok:
        hedge_days: set[date] = set()
        for c in ok:
            assert c.entry_date and c.exit_date
            dd = c.entry_date
            while dd < c.exit_date:
                hedge_days.add(dd)
                dd += timedelta(days=1)
        for d in hedge_days:
            if d not in winner_by_day:
                skipped_hedge_only += 1

    emit(
        lines,
        f"Winner basket entry-days: {len(winner_by_day)}  |  "
        f"combined days (both): {len(combined_days)}  |  "
        f"skipped basket-without-hedge: {skipped_basket_only}  |  "
        f"skipped hedge-without-basket: {skipped_hedge_only}",
    )
    emit(lines, "")

    if not combined_days:
        emit(lines, "COMBINED: NOT AVAILABLE — no overlapping days")
    else:
        b_only = [x[1] for x in combined_days]
        h_only = [-x[2] for x in combined_days]  # hedge daily P&L ≈ -bleed
        comb = [x[3] for x in combined_days]
        emit(
            lines,
            f"{'metric':<22} {'basket alone':>14} {'hedge alone':>14} {'combined':>14}",
        )
        emit(lines, "-" * 68)

        def fmt_stats(xs: list[float]) -> dict[str, float]:
            mean, lo, hi = eng.bootstrap_mean_ci(xs, BOOTSTRAP_N, BOOTSTRAP_SEED)
            return {
                "mean": mean,
                "median": statistics.median(xs),
                "std": statistics.stdev(xs) if len(xs) > 1 else float("nan"),
                "worst": min(xs),
                "ci_lo": lo,
                "ci_hi": hi,
                "mdd": eng.max_drawdown(xs),
            }

        sb = fmt_stats(b_only)
        sh = fmt_stats(h_only)
        sc = fmt_stats(comb)

        def emit_metric(key: str, title: str) -> None:
            emit(
                lines,
                f"{title:<22} {sb[key]:14.4f} {sh[key]:14.4f} {sc[key]:14.4f}",
            )

        emit_metric("mean", "mean per day")
        emit_metric("median", "median")
        emit_metric("std", "std")
        emit_metric("worst", "worst day")
        emit_metric("ci_lo", "bootstrap ci_lo")
        emit_metric("ci_hi", "bootstrap ci_hi")
        emit_metric("mdd", "max_drawdown (run sum)")
        emit(lines, "")
        emit(
            lines,
            "Note: hedge alone column = -bleed_per_day on that calendar day "
            "(long-theta expected negative).",
        )
        emit(
            lines,
            f"bootstrap_n={BOOTSTRAP_N} seed={BOOTSTRAP_SEED} day_span={day_span}",
        )

    emit(lines, "")

    # ----- PART 4 -----
    emit(lines, "===== PART 4: BREAK-EVEN =====")
    if not ok:
        emit(
            lines,
            "Break-even: NOT AVAILABLE — no OK hedge cycles to measure bleed.",
        )
    elif not combined_days:
        # Still can give bleed-based BE from hedge alone
        bleeds = [float(c.bleed_per_day) for c in ok if c.bleed_per_day is not None]
        be = statistics.mean(bleeds)
        emit(
            lines,
            f"Mean hedge bleed per calendar day = {be:.6f} USD/day "
            f"(from {len(bleeds)} OK cycles).",
        )
        emit(
            lines,
            "With one WINNER basket entry per overlapping day, break-even "
            f"per cycle = {be:.6f} USD (must earn this to cover one day of bleed).",
        )
        emit(
            lines,
            "Combined overlap days: NOT AVAILABLE — cannot confirm against "
            f"cited WINNER mean +{WINNER_MEAN_CITED:.2f} on the same day set.",
        )
        gap = WINNER_MEAN_CITED - be
        emit(
            lines,
            f"Cited WINNER mean +{WINNER_MEAN_CITED:.2f} vs break-even {be:.6f}: "
            f"{'ABOVE' if gap >= 0 else 'BELOW'} by {abs(gap):.6f} USD/cycle "
            "(using hedge-only bleed; overlap not available).",
        )
    else:
        bleeds_on_combined = [x[2] for x in combined_days]
        be = statistics.mean(bleeds_on_combined)
        basket_mean = statistics.mean([x[1] for x in combined_days])
        gap = basket_mean - be
        emit(
            lines,
            f"On the {len(combined_days)} overlapping days, mean hedge bleed/day "
            f"= {be:.6f} USD.",
        )
        emit(
            lines,
            "Break-even: each WINNER basket cycle must earn at least "
            f"{be:.6f} USD to exactly cover that day's hedge bleed.",
        )
        emit(
            lines,
            f"Mojooda WINNER mean on those days = {basket_mean:.6f} USD/cycle "
            f"(cited sweep mean was ~+{WINNER_MEAN_CITED:.2f}).",
        )
        emit(
            lines,
            f"Result: basket mean is {'ABOVE' if gap >= 0 else 'BELOW'} "
            f"break-even by {abs(gap):.6f} USD/cycle.",
        )
        emit(
            lines,
            f"Combined mean (basket - bleed) = "
            f"{statistics.mean([x[3] for x in combined_days]):.6f} USD/day.",
        )

    emit(lines, "")
    emit(lines, "END")

    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    logger.info("Wrote %s", OUT_PATH)
    return 0


def na_date(d: date | None) -> str:
    return d.isoformat() if d is not None else "N/A"


def na_f(v: float | None, nd: int = 4) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "N/A"
    return f"{v:.{nd}f}"


def na_i(v: int | None) -> str:
    return str(v) if v is not None else "N/A"


if __name__ == "__main__":
    raise SystemExit(main())
