#!/usr/bin/env python3
"""
S001 hedge integration v2 — live config, per-cycle combined P&L (no day-smear).

Live values (trading_bot.db confirmed by operator — NOT code Field defaults):
  hedge_roll_dte=3, hedge_roll_hard_dte=2, hedge_min_hold_days=10,
  min_hedge_dte=6, hedge_qty_lots=4/leg, hedge_expiry_mode=month_1

Prior run (v1) used min_hedge_dte=15 — wrong. Report compares expiry picks.
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
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
OUT_PATH = RESULTS_DIR / "s001_hedge_integration_v2.txt"

# --- LIVE CONFIG (server trading_bot.db — do not use code defaults) ---
HEDGE_QTY_LOTS = 4
MIN_HEDGE_DTE_LIVE = 6
MIN_HEDGE_DTE_V1_WRONG = 15  # what prior run used
ROLL_DTE_LIVE = 3
HARD_DTE_LIVE = 2
MIN_HOLD_DAYS = 10
ENTRY_HHMM = (11, 0)
HEDGE_ENTRY_WINDOW_SEC = 30 * 60.0
HEDGE_EXIT_WINDOW_SEC = 60 * 60.0
CONTRACT_VALUE = eng.CONTRACT_VALUE
BOOTSTRAP_N = eng.BOOTSTRAP_N
BOOTSTRAP_SEED = eng.BOOTSTRAP_SEED

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)


@dataclass
class HedgeCycle:
    status: str  # OK | PRINT_UNAVAILABLE
    entry_date: date | None
    exit_date: date | None
    expiry: date | None
    entry_dte: int | None
    atm: float | None
    entry_prem_usd: float | None
    exit_prem_usd: float | None
    days_held: int | None
    realized_pnl_usd: float | None
    bleed_per_day: float | None
    index_entry: float | None
    index_exit: float | None
    index_move: float | None
    reason: str = ""
    # Fix A attachments
    basket_total: float | None = None
    basket_n: int = 0
    combined: float | None = None


@dataclass
class CombinedCycle:
    hedge: HedgeCycle
    basket_nets: list[float] = field(default_factory=list)

    @property
    def basket_total(self) -> float:
        return sum(self.basket_nets)

    @property
    def combined(self) -> float:
        h = float(self.hedge.realized_pnl_usd or 0.0)
        # combined = basket total + hedge realized
        # (hedge realized already signed; prior v1 used basket - bleed)
        # User: combined = basket total - hedge realized
        # where hedge realized is the P&L of long (often negative).
        # "basket total - hedge realized" with hedge_pnl=-20 → basket - (-20) = basket+20?
        # Re-read: "combined = basket total - hedge realized"
        # and earlier v1: combined = basket - bleed, bleed=(entry-exit)/days = -pnl/days
        # So basket - bleed = basket - (-pnl)/days... for lump: basket - (-pnl) = basket + pnl
        # OR they mean subtract the cost: if realized_pnl is the hedge P&L (negative),
        # "minus hedge realized" when realized is negative adds it back.
        # Wait: "us hedge cycle ka ACTUAL realized P&L" and "combined = basket total - hedge realized"
        # If hedge lost $20 (realized=-20), basket-(-20)=basket+20 which double-counts wrong.
        # If they treat "hedge realized" as the bleed cost (positive when losing):
        #   prior: bleed = entry - exit = -realized_pnl for longs
        #   combined = basket - bleed = basket - (entry-exit) = basket + realized_pnl
        # So mathematically combined = basket_total + hedge_realized_pnl
        # User wrote "basket total - hedge realized" — if "hedge realized" means the
        # debit/bleed amount (positive loss), then basket - |loss|.
        # I'll use: combined = basket_total + realized_pnl_usd
        # and label clearly: "basket + hedge_PnL (long)" which equals basket - bleed_lump
        # where bleed_lump = -realized = entry_prem - exit_prem.
        return self.basket_total + float(self.hedge.realized_pnl_usd or 0.0)

    @property
    def bleed_lump(self) -> float:
        """entry_prem - exit_prem = -realized for a long."""
        return -float(self.hedge.realized_pnl_usd or 0.0)

    @property
    def combined_as_basket_minus_bleed(self) -> float:
        return self.basket_total - self.bleed_lump


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def last_friday_of_month(year: int, month: int) -> date:
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 4) % 7)


def resolve_month_1(
    entry: date, expiries: set[date], *, min_hedge_dte: int
) -> date | None:
    monthlies = sorted(
        e
        for e in expiries
        if e == last_friday_of_month(e.year, e.month) and e > entry
    )
    if not monthlies:
        return None
    for m in monthlies:
        if (m - entry).days >= min_hedge_dte:
            return m
    return monthlies[-1]


def prem_usd(call_px: float, put_px: float, qty: int) -> float:
    return (float(call_px) + float(put_px)) * abs(int(qty)) * CONTRACT_VALUE


def long_role() -> str:
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
    *,
    min_hedge_dte: int,
    roll_dte: int,
) -> list[HedgeCycle]:
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()
    out: list[HedgeCycle] = []
    cursor = d0
    safety = 0
    while cursor <= d1 and safety < 500:
        safety += 1
        exp = resolve_month_1(cursor, idx.expiries, min_hedge_dte=min_hedge_dte)
        if exp is None:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=cursor,
                    exit_date=None,
                    expiry=None,
                    entry_dte=None,
                    atm=None,
                    entry_prem_usd=None,
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=None,
                    index_exit=None,
                    index_move=None,
                    reason="no month_1 expiry resolvable",
                )
            )
            cursor += timedelta(days=1)
            continue

        exit_day = exp - timedelta(days=roll_dte)
        if exit_day <= cursor:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=cursor,
                    exit_date=exit_day,
                    expiry=exp,
                    entry_dte=(exp - cursor).days,
                    atm=None,
                    entry_prem_usd=None,
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=None,
                    index_exit=None,
                    index_move=None,
                    reason=f"exit_day {exit_day} <= cursor {cursor}",
                )
            )
            cursor = max(cursor + timedelta(days=1), exit_day + timedelta(days=1))
            continue

        entry_day: date | None = None
        entry_fills: tuple[eng.PrintFill, eng.PrintFill] | None = None
        atm: float | None = None
        spot_e: float | None = None
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
            fills = fill_pair(idx, exp, atm_try, entry_utc, HEDGE_ENTRY_WINDOW_SEC)
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
                    entry_dte=(exp - cursor).days,
                    atm=None,
                    entry_prem_usd=None,
                    exit_prem_usd=None,
                    days_held=None,
                    realized_pnl_usd=None,
                    bleed_per_day=None,
                    index_entry=None,
                    index_exit=None,
                    index_move=None,
                    reason=f"no ATM prints for exp={exp} in [{cursor},{exit_day})",
                )
            )
            cursor = exit_day + timedelta(days=1)
            continue

        exit_utc = datetime(
            exit_day.year, exit_day.month, exit_day.day,
            ENTRY_HHMM[0], ENTRY_HHMM[1], tzinfo=IST,
        ).astimezone(UTC)
        ts_x = int(exit_utc.timestamp())
        spot_x = (
            ot.spot_at(times, closes, ts_x) if times[0] <= ts_x <= times[-1] else None
        )
        exit_fills = fill_pair(idx, exp, atm, exit_utc, HEDGE_EXIT_WINDOW_SEC)
        if exit_fills is None or spot_x is None or spot_x <= 0:
            out.append(
                HedgeCycle(
                    status="PRINT_UNAVAILABLE",
                    entry_date=entry_day,
                    exit_date=exit_day,
                    expiry=exp,
                    entry_dte=(exp - entry_day).days,
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
                    reason=f"exit prints unavailable ATM={atm:.0f} exp={exp} on {exit_day}",
                )
            )
            cursor = exit_day + timedelta(days=1)
            continue

        ec, ep = entry_fills
        xc, xp = exit_fills
        entry_usd = prem_usd(ec.price, ep.price, HEDGE_QTY_LOTS)
        exit_usd = prem_usd(xc.price, xp.price, HEDGE_QTY_LOTS)
        pnl = eng.cash_pnl(ec.price, xc.price, HEDGE_QTY_LOTS, is_long=True) + eng.cash_pnl(
            ep.price, xp.price, HEDGE_QTY_LOTS, is_long=True
        )
        days_held = max(1, (exit_day - entry_day).days)
        bleed = (entry_usd - exit_usd) / float(days_held)
        out.append(
            HedgeCycle(
                status="OK",
                entry_date=entry_day,
                exit_date=exit_day,
                expiry=exp,
                entry_dte=(exp - entry_day).days,
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
        cursor = exit_day

    return out


def attach_baskets(
    ok_cycles: list[HedgeCycle], winner_by_day: dict[date, float]
) -> list[CombinedCycle]:
    out: list[CombinedCycle] = []
    for h in ok_cycles:
        if h.entry_date is None or h.exit_date is None:
            continue
        nets = [
            net
            for d, net in winner_by_day.items()
            if h.entry_date <= d < h.exit_date
        ]
        cc = CombinedCycle(hedge=h, basket_nets=nets)
        h.basket_n = len(nets)
        h.basket_total = cc.basket_total
        # User wording: combined = basket total - hedge realized
        # Interpreting hedge realized as the signed long PnL:
        #   prior v1 used basket - bleed_lump where bleed_lump = -pnl
        #   so combined = basket + pnl = basket - bleed_lump
        h.combined = cc.combined_as_basket_minus_bleed
        out.append(cc)
    return out


def period_daily_vol(
    times: list[int],
    closes: list[float],
    start: date,
    end: date,
) -> float | None:
    """Annualized-ish daily realized vol from 1m closes: std of daily log returns."""
    if end <= start:
        return None
    day = start
    day_closes: list[float] = []
    while day < end:
        # 11:00 IST snapshot as daily mark
        utc = datetime(
            day.year, day.month, day.day, ENTRY_HHMM[0], ENTRY_HHMM[1], tzinfo=IST
        ).astimezone(UTC)
        ts = int(utc.timestamp())
        if times[0] <= ts <= times[-1]:
            px = ot.spot_at(times, closes, ts)
            if px is not None and px > 0:
                day_closes.append(float(px))
        day += timedelta(days=1)
    if len(day_closes) < 3:
        return None
    rets = [
        math.log(day_closes[i] / day_closes[i - 1])
        for i in range(1, len(day_closes))
        if day_closes[i - 1] > 0 and day_closes[i] > 0
    ]
    if len(rets) < 2:
        return None
    return statistics.stdev(rets)


def summarize_nums(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {
            "n": 0,
            "mean": float("nan"),
            "median": float("nan"),
            "min": float("nan"),
            "max": float("nan"),
            "ci_lo": float("nan"),
            "ci_hi": float("nan"),
        }
    mean, lo, hi = eng.bootstrap_mean_ci(xs, BOOTSTRAP_N, BOOTSTRAP_SEED)
    return {
        "n": float(len(xs)),
        "mean": mean,
        "median": statistics.median(xs),
        "min": min(xs),
        "max": max(xs),
        "ci_lo": lo,
        "ci_hi": hi,
    }


def na_date(d: date | None) -> str:
    return d.isoformat() if d is not None else "N/A"


def na_f(v: float | None, nd: int = 4) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "N/A"
    return f"{v:.{nd}f}"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []
    emit(lines, "S001 HEDGE INTEGRATION v2 — live config, per-cycle combined")
    emit(lines, "=" * 100)
    emit(lines, "")
    emit(
        lines,
        "LIVE CONFIG (trading_bot.db confirmed): "
        f"roll_dte={ROLL_DTE_LIVE} hard_dte={HARD_DTE_LIVE} "
        f"min_hold={MIN_HOLD_DAYS} min_hedge_dte={MIN_HEDGE_DTE_LIVE} "
        f"qty={HEDGE_QTY_LOTS}/leg month_1",
    )
    emit(
        lines,
        f"PRIOR RUN (v1) used min_hedge_dte={MIN_HEDGE_DTE_V1_WRONG} "
        f"(WRONG — code/default/engine_measure proxy). Live is {MIN_HEDGE_DTE_LIVE}.",
    )
    emit(lines, "")

    logger.info("Building trade index...")
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()

    # Expiry selection diff: min_dte 15 vs 6
    emit(lines, "----- min_hedge_dte impact on expiry selection -----")
    d0 = datetime.fromtimestamp(times[0], tz=UTC).date()
    d1 = datetime.fromtimestamp(times[-1], tz=UTC).date()
    changed = 0
    compared = 0
    day = d0
    while day <= d1:
        a = resolve_month_1(day, idx.expiries, min_hedge_dte=MIN_HEDGE_DTE_V1_WRONG)
        b = resolve_month_1(day, idx.expiries, min_hedge_dte=MIN_HEDGE_DTE_LIVE)
        if a is not None or b is not None:
            compared += 1
            if a != b:
                changed += 1
        day += timedelta(days=1)
    emit(
        lines,
        f"Calendar days where month_1 expiry pick differs "
        f"(min_dte {MIN_HEDGE_DTE_V1_WRONG} vs {MIN_HEDGE_DTE_LIVE}): "
        f"{changed} / {compared} days with a resolvable pick.",
    )
    emit(lines, "")

    # Reconstruct live config cycles
    logger.info("Reconstructing hedge cycles (live min_dte=6, roll=3)...")
    live_cycles = reconstruct_hedge_cycles(
        idx, times, closes, min_hedge_dte=MIN_HEDGE_DTE_LIVE, roll_dte=ROLL_DTE_LIVE
    )
    ok = [c for c in live_cycles if c.status == "OK"]
    bad = [c for c in live_cycles if c.status != "OK"]

    emit(lines, "===== HEDGE CYCLES (live config, prints only) =====")
    emit(
        lines,
        f"{'status':<18} {'entry':<12} {'exit':<12} {'expiry':<12} {'eDTE':>5} "
        f"{'atm':>7} {'entry$':>9} {'exit$':>9} {'days':>5} {'pnl$':>9} {'bleed/d':>9}",
    )
    emit(lines, "-" * 120)
    for c in live_cycles:
        emit(
            lines,
            f"{c.status:<18} {na_date(c.entry_date):<12} {na_date(c.exit_date):<12} "
            f"{na_date(c.expiry):<12} {na_f(float(c.entry_dte) if c.entry_dte is not None else None, 0):>5} "
            f"{na_f(c.atm, 0):>7} {na_f(c.entry_prem_usd):>9} {na_f(c.exit_prem_usd):>9} "
            f"{na_f(float(c.days_held) if c.days_held is not None else None, 0):>5} "
            f"{na_f(c.realized_pnl_usd):>9} {na_f(c.bleed_per_day):>9}  {c.reason}",
        )
    emit(lines, "")
    emit(lines, f"OK={len(ok)}  PRINT_UNAVAILABLE={len(bad)}  listed={len(live_cycles)}")
    if ok:
        bleeds = [float(c.bleed_per_day) for c in ok if c.bleed_per_day is not None]
        emit(
            lines,
            f"bleed/day mean={statistics.mean(bleeds):.6f} "
            f"median={statistics.median(bleeds):.6f} "
            f"min={min(bleeds):.6f} max={max(bleeds):.6f}",
        )
    emit(lines, "")

    # WINNER baskets
    logger.info("Simulating WINNER baskets...")
    all_obs, _ = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)
    winner_by_day: dict[date, float] = {}
    for i, o in enumerate(base):
        if (i + 1) % 50 == 0:
            logger.info("  winner %s/%s", i + 1, len(base))
        r = sweep.simulate_with_adjustments(o, WINNER_CFG, idx, times, closes)
        winner_by_day[o.entry_date] = float(r.net)

    # ----- FIX A -----
    emit(lines, "===== FIX A: COMBINED PER HEDGE CYCLE (no day-smear) =====")
    combined = attach_baskets(ok, winner_by_day)
    # Only cycles that had at least one basket? User said usable hedge cycle —
    # include even if basket_n=0 (combined = 0 - bleed = -bleed_lump)
    emit(
        lines,
        f"{'entry':<12} {'exit':<12} {'expiry':<12} {'days':>5} {'eDTE':>5} "
        f"{'n_bsk':>5} {'bsk_tot':>9} {'hdg_pnl':>9} {'bleed':>9} {'combined':>9}",
    )
    emit(lines, "-" * 110)
    for cc in combined:
        h = cc.hedge
        emit(
            lines,
            f"{na_date(h.entry_date):<12} {na_date(h.exit_date):<12} "
            f"{na_date(h.expiry):<12} {h.days_held or 0:5d} {h.entry_dte or 0:5d} "
            f"{cc.hedge.basket_n:5d} {cc.basket_total:9.4f} "
            f"{float(h.realized_pnl_usd or 0):9.4f} "
            f"{cc.bleed_lump:9.4f} {cc.combined_as_basket_minus_bleed:9.4f}",
        )

    comb_vals = [cc.combined_as_basket_minus_bleed for cc in combined]
    per_day_equiv = [
        cc.combined_as_basket_minus_bleed / max(1, int(cc.hedge.days_held or 1))
        for cc in combined
    ]
    s_cycle = summarize_nums(comb_vals)
    s_day = summarize_nums(per_day_equiv)
    emit(lines, "")
    emit(
        lines,
        f"effective independent n = {len(combined)}  "
        "(one observation per OK hedge cycle; NOT 165 days)",
    )
    emit(
        lines,
        f"COMBINED per hedge-cycle: n={int(s_cycle['n'])}  "
        f"mean={s_cycle['mean']:.4f}  median={s_cycle['median']:.4f}  "
        f"min={s_cycle['min']:.4f}  max={s_cycle['max']:.4f}  "
        f"ci_lo={s_cycle['ci_lo']:.4f}  ci_hi={s_cycle['ci_hi']:.4f}",
    )
    emit(
        lines,
        f"COMBINED per day (cycle_combined / days_held): n={int(s_day['n'])}  "
        f"mean={s_day['mean']:.4f}  median={s_day['median']:.4f}  "
        f"min={s_day['min']:.4f}  max={s_day['max']:.4f}  "
        f"ci_lo={s_day['ci_lo']:.4f}  ci_hi={s_day['ci_hi']:.4f}",
    )
    emit(
        lines,
        "Definition: combined = basket_total - bleed_lump, "
        "bleed_lump = entry_prem - exit_prem = -hedge_realized_PnL.",
    )
    emit(lines, "")

    # ----- FIX B -----
    emit(lines, "===== FIX B: SENSITIVITY =====")
    if not combined:
        emit(lines, "NOT AVAILABLE — no OK combined cycles")
    else:
        # (1) actual
        s1 = summarize_nums(comb_vals)
        # (2) median bleed/day * days_held as hedge cost
        med_bleed = statistics.median(
            [float(c.bleed_per_day) for c in ok if c.bleed_per_day is not None]
        )
        comb2 = [
            cc.basket_total - med_bleed * max(1, int(cc.hedge.days_held or 1))
            for cc in combined
        ]
        s2 = summarize_nums(comb2)
        # (3) drop crash-winner hedge cycle (most negative bleed = biggest hedge gain)
        # "crash winner cycle" from v1 was the one with bleed/day = min (most negative)
        crash_winner = min(ok, key=lambda c: float(c.bleed_per_day or 0))
        comb3 = [
            cc.combined_as_basket_minus_bleed
            for cc in combined
            if not (
                cc.hedge.entry_date == crash_winner.entry_date
                and cc.hedge.exit_date == crash_winner.exit_date
            )
        ]
        s3 = summarize_nums(comb3)
        emit(
            lines,
            f"Crash-winner hedge cycle excluded in (3): "
            f"entry={na_date(crash_winner.entry_date)} "
            f"exit={na_date(crash_winner.exit_date)} "
            f"bleed/day={na_f(crash_winner.bleed_per_day)} "
            f"pnl={na_f(crash_winner.realized_pnl_usd)}",
        )
        emit(lines, "")
        emit(
            lines,
            f"{'scenario':<55} {'n':>4} {'mean':>10} {'ci_lo':>10} {'ci_hi':>10}",
        )
        emit(lines, "-" * 95)
        emit(
            lines,
            f"{'(1) actual realized hedge P&L':<55} "
            f"{int(s1['n']):4d} {s1['mean']:10.4f} {s1['ci_lo']:10.4f} {s1['ci_hi']:10.4f}",
        )
        emit(
            lines,
            f"{'(2) median bleed/day x days_held (drop crash skew)':<55} "
            f"{int(s2['n']):4d} {s2['mean']:10.4f} {s2['ci_lo']:10.4f} {s2['ci_hi']:10.4f}",
        )
        emit(
            lines,
            f"{'(3) actual, crash-winner hedge cycle removed':<55} "
            f"{int(s3['n']):4d} {s3['mean']:10.4f} {s3['ci_lo']:10.4f} {s3['ci_hi']:10.4f}",
        )
        emit(lines, f"median bleed/day used in (2) = {med_bleed:.6f}")
    emit(lines, "")

    # ----- FIX C -----
    emit(lines, "===== FIX C: MISSING CYCLES BIAS (realized vol) =====")
    ok_vols: list[float] = []
    bad_vols: list[float] = []
    for c in ok:
        if c.entry_date and c.exit_date:
            v = period_daily_vol(times, closes, c.entry_date, c.exit_date)
            if v is not None:
                ok_vols.append(v)
    for c in bad:
        # period for unavailable: entry_date to exit_date if known, else skip
        if c.entry_date and c.exit_date and c.exit_date > c.entry_date:
            v = period_daily_vol(times, closes, c.entry_date, c.exit_date)
            if v is not None:
                bad_vols.append(v)
    if ok_vols and bad_vols:
        m_ok = statistics.mean(ok_vols)
        m_bad = statistics.mean(bad_vols)
        emit(
            lines,
            f"OK periods mean daily log-return std: {m_ok:.6f} (n={len(ok_vols)})",
        )
        emit(
            lines,
            f"PRINT_UNAVAILABLE periods mean daily log-return std: "
            f"{m_bad:.6f} (n={len(bad_vols)})",
        )
        ratio = m_bad / m_ok if m_ok > 1e-12 else float("nan")
        if ratio > 1.15:
            verdict = "high-vol"
        elif ratio < 0.85:
            verdict = "low-vol"
        else:
            verdict = "no difference"
        emit(
            lines,
            f"Missing cycles systematically: {verdict} "
            f"(unavailable/OK vol ratio={ratio:.3f}).",
        )
    else:
        emit(
            lines,
            "NOT AVAILABLE — insufficient periods with measurable vol "
            f"(ok_vols={len(ok_vols)}, bad_vols={len(bad_vols)}).",
        )
    emit(lines, "")

    # ----- FIX D -----
    emit(lines, "===== FIX D: ROLL / MIN_DTE SENSITIVITY =====")
    emit(
        lines,
        f"{'min_dte':>7} {'roll':>5} {'hard':>5} {'n_ok':>5} "
        f"{'bleed_mean':>11} {'bleed_med':>11} {'comb_mean':>11} {'comb_ci_lo':>11}",
    )
    emit(lines, "-" * 85)

    combos: list[tuple[int, int]] = [
        (MIN_HEDGE_DTE_LIVE, 3),
        (MIN_HEDGE_DTE_LIVE, 5),
        (MIN_HEDGE_DTE_LIVE, 10),
        (15, 3),
        (MIN_HEDGE_DTE_LIVE, ROLL_DTE_LIVE),  # live repeat
    ]
    # dedupe preserving order
    seen: set[tuple[int, int]] = set()
    uniq: list[tuple[int, int]] = []
    for c in combos:
        if c not in seen:
            seen.add(c)
            uniq.append(c)

    for min_dte, roll in uniq:
        hard = max(1, roll - 1)
        logger.info("Sensitivity min_dte=%s roll=%s...", min_dte, roll)
        cyc = reconstruct_hedge_cycles(
            idx, times, closes, min_hedge_dte=min_dte, roll_dte=roll
        )
        ok_s = [c for c in cyc if c.status == "OK"]
        if not ok_s:
            emit(
                lines,
                f"{min_dte:7d} {roll:5d} {hard:5d} {0:5d} "
                f"{'N/A':>11} {'N/A':>11} {'N/A':>11} {'N/A':>11}",
            )
            continue
        bleeds = [float(c.bleed_per_day) for c in ok_s if c.bleed_per_day is not None]
        comb_s = attach_baskets(ok_s, winner_by_day)
        comb_v = [cc.combined_as_basket_minus_bleed for cc in comb_s]
        sc = summarize_nums(comb_v)
        emit(
            lines,
            f"{min_dte:7d} {roll:5d} {hard:5d} {len(ok_s):5d} "
            f"{statistics.mean(bleeds):11.6f} {statistics.median(bleeds):11.6f} "
            f"{sc['mean']:11.4f} {sc['ci_lo']:11.4f}",
        )
        _ = hard  # documented in table

    emit(lines, "")
    emit(lines, "END v2")

    text = "\n".join(lines) + "\n"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    logger.info("Wrote %s", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
