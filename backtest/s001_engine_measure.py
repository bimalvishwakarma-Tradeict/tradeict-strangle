#!/usr/bin/env python3
"""
S001 daily-engine measure — hedge bleed vs basket collection.

No management: enter, hold to 2DTE basket settlement, mark hedge at that same
instant. Wings on/off x qty-ratio sweep. Real trade prints + 1m spot.

Strike path (per claude/S001_CONFIG_SEMANTICS.md section 1):
  trade_type=strangle, strike_selection_mode=fixed_premium (not theta_based),
  strangle_premium_mode=pct_of_hedge, strangle_premium_pct_of_hedge=25.
  target = ceil(avg(hedge_call_mark, hedge_put_mark) * 25 / 100).
  target_premium_per_side is FALLBACK only — not used when marks are present.

stdlib only. Uses OptionsTradeStore (hand-audit) + same shard cache for bulk index.
"""

from __future__ import annotations

import argparse
import calendar
import math
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
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

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
CACHE_DIR = ot.CACHE_DIR

CONTRACT_VALUE = 0.001  # BTC per lot (Delta India)
HEDGE_QTY_LOTS = 4
STRANGLE_PCT_OF_HEDGE = 25.0  # live pct_of_hedge path
WING_POINTS = 2000.0
MIN_HEDGE_DTE = 15
BASKET_DTE = 2
ENTRY_TIMES_IST = ((9, 0), (11, 0), (13, 0), (15, 0))
PRINT_WINDOW_SEC = 5 * 60.0
PRINT_WINDOW_FALLBACK_SEC = 15 * 60.0
# Monthly hedge is thin vs daily basket — wider windows + role fallback.
HEDGE_ENTRY_WINDOW_SEC = 30 * 60.0
SETTLE_MARK_WINDOW_SEC = 60 * 60.0
QTY_RATIOS_PCT = (100, 150, 200, 250, 300)
FILL_PACKAGES = ("maker", "taker")  # short-side naming; longs reversed
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260914
FEE_RATE = 0.0001
PREMIUM_CAP = 0.035
GST = 1.18


def option_fee(premium: float, index: float, qty_lots: int) -> float:
    """
    Verified trading-fee model (VRP / S003 thread):
      min(index * qtyBTC * 0.0001, premium * qtyBTC * 0.035) * 1.18
    qtyBTC = lots * CONTRACT_VALUE.
    Settlement fee: same formula on intrinsic — UNVERIFIED; report both.
    """
    qty_btc = abs(int(qty_lots)) * CONTRACT_VALUE
    if premium <= 0 or index <= 0 or qty_btc <= 0:
        return 0.0
    return min(index * qty_btc * FEE_RATE, premium * qty_btc * PREMIUM_CAP) * GST


def cash_pnl(entry: float, exit_: float, qty_lots: int, *, is_long: bool) -> float:
    """USD P&L for one option leg from entry premium to exit mark/intrinsic."""
    qty_btc = abs(int(qty_lots)) * CONTRACT_VALUE
    if is_long:
        return (exit_ - entry) * qty_btc
    return (entry - exit_) * qty_btc


def call_intrinsic(spot: float, strike: float) -> float:
    return max(spot - strike, 0.0)


def put_intrinsic(spot: float, strike: float) -> float:
    return max(strike - spot, 0.0)


def ist_to_utc(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST).astimezone(UTC)


def format_symbol(opt: str, strike: float, exp: date) -> str:
    return f"{opt}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def nearest_strike(strikes: set[float], target: float) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - target), k))


def last_friday_of_month(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    d = date(year, month, last)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def resolve_month_1(entry: date, expiries: set[date]) -> date | None:
    """
    Proxy for Delta label month_1: last-Friday monthlies present in the shard,
    then enforce min_hedge_dte=15 by advancing to the next monthly if needed.
    """
    monthlies = sorted(
        e for e in expiries if e == last_friday_of_month(e.year, e.month) and e > entry
    )
    if not monthlies:
        return None
    for m in monthlies:
        if (m - entry).days >= MIN_HEDGE_DTE:
            return m
    return monthlies[-1]


def bootstrap_mean_ci(
    values: list[float], n: int, seed: int
) -> tuple[float, float, float]:
    if not values:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    m = len(values)
    means: list[float] = []
    for _ in range(n):
        s = 0.0
        for _j in range(m):
            s += values[rng.randrange(m)]
        means.append(s / m)
    means.sort()
    lo = means[int(0.025 * n)]
    hi = means[min(n - 1, int(0.975 * n))]
    return statistics.mean(values), lo, hi


def pctile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def sortino(nets: list[float]) -> float:
    if not nets:
        return float("nan")
    mu = statistics.mean(nets)
    downs = [min(x, 0.0) ** 2 for x in nets]
    dd = math.sqrt(statistics.mean(downs)) if downs else 0.0
    if dd <= 1e-12:
        return float("inf") if mu > 0 else float("nan")
    return mu / dd


def max_drawdown(nets_chronological: list[float]) -> float:
    if not nets_chronological:
        return float("nan")
    cum = 0.0
    peak = 0.0
    worst = 0.0
    for x in nets_chronological:
        cum += x
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def ceil_target_premium(avg_hedge: float, pct: float = STRANGLE_PCT_OF_HEDGE) -> float:
    return float(int(math.ceil(avg_hedge * pct / 100.0)))


@dataclass
class PrintFill:
    symbol: str
    price: float
    ts_utc: datetime
    buyer_role: str
    strike: float
    option_type: str


@dataclass
class TradeIndex:
    strikes_by_expiry: dict[date, set[float]] = field(
        default_factory=lambda: defaultdict(set)
    )
    by_symbol: dict[str, list[tuple[float, float, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    expiries: set[date] = field(default_factory=set)


def build_trade_index(cache_dir: Path = CACHE_DIR) -> TradeIndex:
    idx = TradeIndex()
    shards = sorted(cache_dir.glob("opt_trades_*.sqlite"))
    if not shards:
        raise FileNotFoundError(
            f"No shards in {cache_dir} - run options_trades.py first"
        )
    for path in shards:
        print(f"Indexing {path.name} ...", flush=True)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT symbol, ts, price, role, expiry, opt_type, strike "
                "FROM trades"
            )
            n = 0
            for symbol, ts, price, role, expiry, opt_type, strike in cur:
                n += 1
                exp = date.fromisoformat(expiry)
                idx.expiries.add(exp)
                idx.strikes_by_expiry[exp].add(float(strike))
                # Index all hours: monthly hedge prints are sparse; settlement
                # marks need coverage around 12:00 UTC and beyond entry slots.
                role_s = "maker" if int(role) == 0 else "taker"
                idx.by_symbol[symbol].append(
                    (float(ts), float(price), role_s)
                )
            print(f"  scanned {n:,} rows", flush=True)
        finally:
            conn.close()
    for sym in idx.by_symbol:
        idx.by_symbol[sym].sort(key=lambda x: x[0])
    print(
        f"Index ready: {len(idx.expiries)} expiries, "
        f"{len(idx.by_symbol)} symbols",
        flush=True,
    )
    return idx


def nearest_print(
    idx: TradeIndex,
    symbol: str,
    when: datetime,
    window_sec: float,
    buyer_role: str | None,
) -> PrintFill | None:
    """
    Nearest print within window_sec.
    If buyer_role is set, require that role; if None, accept any role.
    """
    series = idx.by_symbol.get(symbol)
    if not series:
        return None
    target = when.timestamp()
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid
    # Scan enough neighbors for wide monthly windows (up to 60m)
    span = max(80, int(window_sec / 5) + 20)
    best: tuple[float, float, str] | None = None
    best_abs: float | None = None
    for j in range(max(0, lo - span), min(len(series), lo + span)):
        ts, px, role = series[j]
        if abs(ts - target) > window_sec:
            continue
        if buyer_role is not None and role != buyer_role:
            continue
        d = abs(ts - target)
        if best_abs is None or d < best_abs:
            best_abs = d
            best = (ts, px, role)
    if best is None:
        return None
    parsed = ot.parse_symbol(symbol)
    if parsed is None:
        return None
    return PrintFill(
        symbol=symbol,
        price=best[1],
        ts_utc=datetime.fromtimestamp(best[0], tz=UTC),
        buyer_role=best[2],
        strike=parsed.strike,
        option_type=parsed.option_type,
    )


def nearest_print_prefer(
    idx: TradeIndex,
    symbol: str,
    when: datetime,
    window_sec: float,
    preferred_role: str,
    *,
    fallback_any_role: bool = True,
) -> PrintFill | None:
    """Try preferred buyer_role, then any role in the same window."""
    hit = nearest_print(idx, symbol, when, window_sec, preferred_role)
    if hit is not None:
        return hit
    if fallback_any_role:
        return nearest_print(idx, symbol, when, window_sec, None)
    return None


def roles_for_package(fill_package: str) -> tuple[str, str]:
    """
    Package named by SHORT side (VRP convention):
      maker = conservative shorts (buyer_role=maker); longs use taker.
      taker = optimistic shorts; longs use maker.
    Returns (short_buyer_role, long_buyer_role).
    """
    if fill_package == "maker":
        return "maker", "taker"
    return "taker", "maker"


def bisect_left_times(times: list[int], ts: int) -> int:
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def settle_spot_1200_utc(
    times: list[int], closes: list[float], exp: date
) -> float | None:
    ts = int(datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp())
    i = bisect_left_times(times, ts)
    if i < len(times) and times[i] == ts:
        return closes[i]
    return ot.spot_at(times, closes, ts)


def pick_atm_straddle_fills(
    idx: TradeIndex,
    exp: date,
    spot: float,
    when: datetime,
    long_role: str,
    window_sec: float,
) -> tuple[float, PrintFill, PrintFill] | None:
    strikes = idx.strikes_by_expiry.get(exp) or set()
    atm = nearest_strike(strikes, spot)
    if atm is None:
        return None
    cf = nearest_print_prefer(
        idx, format_symbol("C", atm, exp), when, window_sec, long_role
    )
    pf = nearest_print_prefer(
        idx, format_symbol("P", atm, exp), when, window_sec, long_role
    )
    if cf is None or pf is None:
        return None
    return atm, cf, pf


def pick_strangle_by_premium(
    idx: TradeIndex,
    exp: date,
    spot: float,
    target: float,
    when: datetime,
    short_role: str,
    window_sec: float,
) -> tuple[float, float, PrintFill, PrintFill] | None:
    strikes = sorted(idx.strikes_by_expiry.get(exp) or set())
    if not strikes or target <= 0:
        return None
    best_c: tuple[float, PrintFill, float] | None = None
    best_p: tuple[float, PrintFill, float] | None = None
    for k in strikes:
        if k > spot:
            fill = nearest_print_prefer(
                idx,
                format_symbol("C", k, exp),
                when,
                window_sec,
                short_role,
            )
            if fill is None or fill.price <= 0:
                continue
            diff = abs(fill.price - target)
            if best_c is None or diff < best_c[0] or (
                diff == best_c[0] and k < best_c[2]
            ):
                best_c = (diff, fill, k)
        if k < spot:
            fill = nearest_print_prefer(
                idx,
                format_symbol("P", k, exp),
                when,
                window_sec,
                short_role,
            )
            if fill is None or fill.price <= 0:
                continue
            diff = abs(fill.price - target)
            if best_p is None or diff < best_p[0] or (
                diff == best_p[0] and k > best_p[2]
            ):
                best_p = (diff, fill, k)
    if best_c is None or best_p is None:
        return None
    return best_c[2], best_p[2], best_c[1], best_p[1]


def pick_wing_fills(
    idx: TradeIndex,
    exp: date,
    short_call_k: float,
    short_put_k: float,
    when: datetime,
    long_role: str,
    window_sec: float,
    points: float = WING_POINTS,
) -> tuple[float, float, PrintFill, PrintFill] | None:
    strikes = idx.strikes_by_expiry.get(exp) or set()
    call_target = short_call_k + points
    put_target = short_put_k - points
    at_or_beyond_c = [k for k in strikes if k >= call_target]
    at_or_beyond_p = [k for k in strikes if k <= put_target]
    beyond_c = [k for k in strikes if k > short_call_k]
    beyond_p = [k for k in strikes if k < short_put_k]
    if not beyond_c or not beyond_p:
        return None
    wing_c_k = min(at_or_beyond_c) if at_or_beyond_c else max(beyond_c)
    wing_p_k = max(at_or_beyond_p) if at_or_beyond_p else min(beyond_p)
    if wing_c_k <= short_call_k or wing_p_k >= short_put_k:
        return None
    cf = nearest_print_prefer(
        idx, format_symbol("C", wing_c_k, exp), when, window_sec, long_role
    )
    pf = nearest_print_prefer(
        idx, format_symbol("P", wing_p_k, exp), when, window_sec, long_role
    )
    if cf is None or pf is None:
        return None
    return wing_c_k, wing_p_k, cf, pf


@dataclass
class WindowBase:
    entry_date: date
    entry_hhmm: str
    entry_utc: datetime
    basket_expiry: date
    hedge_expiry: date
    spot_entry: float
    spot_settle: float
    fill_package: str
    hedge_atm: float
    hedge_call: PrintFill
    hedge_put: PrintFill
    hedge_mark_call: PrintFill
    hedge_mark_put: PrintFill
    target_premium: float
    short_call_k: float
    short_put_k: float
    short_call: PrintFill
    short_put: PrintFill
    wing_call_k: float | None
    wing_put_k: float | None
    wing_call: PrintFill | None
    wing_put: PrintFill | None


@dataclass
class VariantObs:
    base: WindowBase
    wings_on: bool
    qty_ratio_pct: int
    basket_qty: int
    hedge_qty: int
    hedge_pnl: float
    basket_pnl: float
    wings_pnl: float
    entry_fees: float
    settle_fees: float
    net_no_settle: float
    net_with_settle: float
    spot_move_abs: float


def scale_basket_qty(ratio_pct: int, hedge_qty: int = HEDGE_QTY_LOTS) -> int:
    return int(math.ceil(hedge_qty * ratio_pct / 100.0))


def build_variant(base: WindowBase, wings_on: bool, ratio_pct: int) -> VariantObs:
    hq = HEDGE_QTY_LOTS
    bq = scale_basket_qty(ratio_pct, hq)
    spot_e = base.spot_entry
    spot_s = base.spot_settle

    hedge_pnl = cash_pnl(
        base.hedge_call.price, base.hedge_mark_call.price, hq, is_long=True
    ) + cash_pnl(
        base.hedge_put.price, base.hedge_mark_put.price, hq, is_long=True
    )
    hedge_entry_fees = option_fee(base.hedge_call.price, spot_e, hq) + option_fee(
        base.hedge_put.price, spot_e, hq
    )
    hedge_exit_fees = option_fee(
        base.hedge_mark_call.price, spot_s, hq
    ) + option_fee(base.hedge_mark_put.price, spot_s, hq)

    sc_exit = call_intrinsic(spot_s, base.short_call_k)
    sp_exit = put_intrinsic(spot_s, base.short_put_k)
    basket_pnl = cash_pnl(
        base.short_call.price, sc_exit, bq, is_long=False
    ) + cash_pnl(base.short_put.price, sp_exit, bq, is_long=False)
    basket_entry_fees = option_fee(base.short_call.price, spot_e, bq) + option_fee(
        base.short_put.price, spot_e, bq
    )
    basket_settle_fees = option_fee(sc_exit, spot_s, bq) + option_fee(
        sp_exit, spot_s, bq
    )

    wings_pnl = 0.0
    wings_entry_fees = 0.0
    wings_settle_fees = 0.0
    if (
        wings_on
        and base.wing_call is not None
        and base.wing_put is not None
        and base.wing_call_k is not None
        and base.wing_put_k is not None
    ):
        wc_exit = call_intrinsic(spot_s, base.wing_call_k)
        wp_exit = put_intrinsic(spot_s, base.wing_put_k)
        wings_pnl = cash_pnl(
            base.wing_call.price, wc_exit, bq, is_long=True
        ) + cash_pnl(base.wing_put.price, wp_exit, bq, is_long=True)
        wings_entry_fees = option_fee(base.wing_call.price, spot_e, bq) + option_fee(
            base.wing_put.price, spot_e, bq
        )
        wings_settle_fees = option_fee(wc_exit, spot_s, bq) + option_fee(
            wp_exit, spot_s, bq
        )

    entry_fees_all = (
        hedge_entry_fees
        + basket_entry_fees
        + wings_entry_fees
        + hedge_exit_fees
    )
    settle_only = basket_settle_fees + wings_settle_fees
    gross = hedge_pnl + basket_pnl + wings_pnl
    net_no_settle = gross - entry_fees_all
    net_with_settle = gross - entry_fees_all - settle_only

    return VariantObs(
        base=base,
        wings_on=wings_on,
        qty_ratio_pct=ratio_pct,
        basket_qty=bq,
        hedge_qty=hq,
        hedge_pnl=hedge_pnl,
        basket_pnl=basket_pnl,
        wings_pnl=wings_pnl,
        entry_fees=entry_fees_all,
        settle_fees=settle_only,
        net_no_settle=net_no_settle,
        net_with_settle=net_with_settle,
        spot_move_abs=abs(spot_s - spot_e),
    )


def measure_all(
    idx: TradeIndex,
) -> tuple[list[VariantObs], dict[str, Any], WindowBase | None]:
    times, closes = ot.load_spot_1m()
    spot_lo, spot_hi = times[0], times[-1]
    drop: dict[str, int] = defaultdict(int)
    variants: list[VariantObs] = []
    audit_base: WindowBase | None = None
    bases_built = 0

    basket_expiries = sorted(idx.expiries)
    for basket_exp in basket_expiries:
        entry_date = basket_exp - timedelta(days=BASKET_DTE)
        settle = settle_spot_1200_utc(times, closes, basket_exp)
        if settle is None:
            drop["no_settle_spot"] += 1
            continue
        settle_ts = int(
            datetime(
                basket_exp.year, basket_exp.month, basket_exp.day, 12, 0, tzinfo=UTC
            ).timestamp()
        )
        settle_utc = datetime.fromtimestamp(settle_ts, tz=UTC)
        if settle_ts < spot_lo or settle_ts > spot_hi:
            drop["settle_outside_spot_range"] += 1
            continue

        hedge_exp = resolve_month_1(entry_date, idx.expiries)
        if hedge_exp is None:
            drop["no_month_1"] += 1
            continue

        for hh, mm in ENTRY_TIMES_IST:
            entry_utc = ist_to_utc(entry_date, hh, mm)
            entry_ts = int(entry_utc.timestamp())
            if entry_ts < spot_lo or entry_ts > spot_hi:
                drop["entry_outside_spot_range"] += 1
                continue
            spot_e = ot.spot_at(times, closes, entry_ts)
            if spot_e is None:
                drop["no_entry_spot"] += 1
                continue

            for fill_pkg in FILL_PACKAGES:
                short_role, long_role = roles_for_package(fill_pkg)

                hedge = pick_atm_straddle_fills(
                    idx,
                    hedge_exp,
                    spot_e,
                    entry_utc,
                    long_role,
                    HEDGE_ENTRY_WINDOW_SEC,
                )
                if hedge is None:
                    drop[f"hedge_entry_miss_{fill_pkg}"] += 1
                    continue
                atm_h, hc, hp = hedge

                hm_c = nearest_print_prefer(
                    idx,
                    format_symbol("C", atm_h, hedge_exp),
                    settle_utc,
                    SETTLE_MARK_WINDOW_SEC,
                    long_role,
                )
                hm_p = nearest_print_prefer(
                    idx,
                    format_symbol("P", atm_h, hedge_exp),
                    settle_utc,
                    SETTLE_MARK_WINDOW_SEC,
                    long_role,
                )
                if hm_c is None or hm_p is None:
                    drop[f"hedge_mark_miss_{fill_pkg}"] += 1
                    continue

                avg_h = (hc.price + hp.price) / 2.0
                if avg_h <= 0:
                    drop["hedge_prem_nonpos"] += 1
                    continue
                target = ceil_target_premium(avg_h, STRANGLE_PCT_OF_HEDGE)
                if target <= 0:
                    drop["target_nonpos"] += 1
                    continue

                strangle = pick_strangle_by_premium(
                    idx,
                    basket_exp,
                    spot_e,
                    target,
                    entry_utc,
                    short_role,
                    PRINT_WINDOW_SEC,
                )
                if strangle is None:
                    # Daily chain is thicker — one wider fallback attempt
                    strangle = pick_strangle_by_premium(
                        idx,
                        basket_exp,
                        spot_e,
                        target,
                        entry_utc,
                        short_role,
                        PRINT_WINDOW_FALLBACK_SEC,
                    )
                if strangle is None:
                    drop[f"strangle_miss_{fill_pkg}"] += 1
                    continue
                sc_k, sp_k, sc, sp = strangle

                wings = pick_wing_fills(
                    idx,
                    basket_exp,
                    sc_k,
                    sp_k,
                    entry_utc,
                    long_role,
                    PRINT_WINDOW_FALLBACK_SEC,
                )
                if wings is None:
                    drop[f"wing_miss_{fill_pkg}"] += 1
                    wc_k = wp_k = None
                    wc = wp = None
                else:
                    wc_k, wp_k, wc, wp = wings

                base = WindowBase(
                    entry_date=entry_date,
                    entry_hhmm=f"{hh:02d}:{mm:02d}",
                    entry_utc=entry_utc,
                    basket_expiry=basket_exp,
                    hedge_expiry=hedge_exp,
                    spot_entry=spot_e,
                    spot_settle=settle,
                    fill_package=fill_pkg,
                    hedge_atm=atm_h,
                    hedge_call=hc,
                    hedge_put=hp,
                    hedge_mark_call=hm_c,
                    hedge_mark_put=hm_p,
                    target_premium=target,
                    short_call_k=sc_k,
                    short_put_k=sp_k,
                    short_call=sc,
                    short_put=sp,
                    wing_call_k=wc_k,
                    wing_put_k=wp_k,
                    wing_call=wc,
                    wing_put=wp,
                )
                bases_built += 1

                for ratio in QTY_RATIOS_PCT:
                    variants.append(build_variant(base, False, ratio))
                    if wc is not None and wp is not None:
                        variants.append(build_variant(base, True, ratio))

                if (
                    audit_base is None
                    and fill_pkg == "maker"
                    and hh == 11
                    and wc is not None
                ):
                    audit_base = base

    meta = {
        "drop_reasons": dict(drop),
        "n_basket_expiries": len(basket_expiries),
        "n_bases": bases_built,
        "n_variants": len(variants),
        "strike_path": (
            "strangle_premium_mode=pct_of_hedge @ "
            f"{STRANGLE_PCT_OF_HEDGE:.0f}% of avg hedge mark; "
            "target_premium_per_side NOT used (live path per S001_CONFIG_SEMANTICS.md)"
        ),
        "month_1_rule": (
            f"last Friday of month present in shards, DTE>={MIN_HEDGE_DTE} "
            "(advance to next monthly if nearer)"
        ),
        "hedge_window_note": (
            "Hedge marked from entry to basket 2DTE settlement only - NOT full "
            "multi-week hedge lifecycle / roll at DTE 3. Measures daily-engine "
            "contribution; full simulator needed for hedge lifecycle."
        ),
    }
    return variants, meta, audit_base


def fmt(v: Any, d: int = 2) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


def filter_obs(
    all_obs: list[VariantObs],
    *,
    entry_hhmm: str | None = None,
    fill_package: str | None = None,
    wings_on: bool | None = None,
    qty_ratio_pct: int | None = None,
) -> list[VariantObs]:
    out = all_obs
    if entry_hhmm is not None:
        out = [o for o in out if o.base.entry_hhmm == entry_hhmm]
    if fill_package is not None:
        out = [o for o in out if o.base.fill_package == fill_package]
    if wings_on is not None:
        out = [o for o in out if o.wings_on == wings_on]
    if qty_ratio_pct is not None:
        out = [o for o in out if o.qty_ratio_pct == qty_ratio_pct]
    return out


def risk_block(obs: list[VariantObs], *, use_settle: bool) -> dict[str, Any]:
    if not obs:
        return {"n": 0}
    key = (
        (lambda o: o.net_with_settle)
        if use_settle
        else (lambda o: o.net_no_settle)
    )
    nets = [key(o) for o in obs]
    ordered = sorted(obs, key=lambda o: (o.base.entry_utc, o.base.fill_package))
    chron = [key(o) for o in ordered]
    mean, lo, hi = bootstrap_mean_ci(nets, BOOTSTRAP_N, BOOTSTRAP_SEED)
    worst = min(obs, key=key)
    return {
        "n": len(obs),
        "mean": mean,
        "median": statistics.median(nets),
        "ci_lo": lo,
        "ci_hi": hi,
        "p5": pctile(nets, 5),
        "worst": key(worst),
        "worst_date": worst.base.entry_date.isoformat(),
        "worst_exp": worst.base.basket_expiry.isoformat(),
        "sortino": sortino(nets),
        "max_dd": max_drawdown(chron),
        "hedge_mean": statistics.mean([o.hedge_pnl for o in obs]),
        "basket_mean": statistics.mean([o.basket_pnl for o in obs]),
        "wings_mean": statistics.mean([o.wings_pnl for o in obs]),
        "fees_mean": statistics.mean(
            [o.entry_fees + (o.settle_fees if use_settle else 0.0) for o in obs]
        ),
    }


def decile_blocks(obs: list[VariantObs], *, use_settle: bool) -> list[dict[str, Any]]:
    if len(obs) < 10:
        return []
    ranked = sorted(obs, key=lambda o: o.spot_move_abs)
    n = len(ranked)
    rows = []
    for d in range(10):
        lo_i = int(d * n / 10)
        hi_i = int((d + 1) * n / 10)
        chunk = ranked[lo_i:hi_i]
        if not chunk:
            continue
        nets = [
            o.net_with_settle if use_settle else o.net_no_settle for o in chunk
        ]
        rows.append(
            {
                "decile": d + 1,
                "n": len(chunk),
                "move_lo": chunk[0].spot_move_abs,
                "move_hi": chunk[-1].spot_move_abs,
                "net_mean": statistics.mean(nets),
                "hedge_mean": statistics.mean([o.hedge_pnl for o in chunk]),
                "basket_mean": statistics.mean([o.basket_pnl for o in chunk]),
                "wings_mean": statistics.mean([o.wings_pnl for o in chunk]),
            }
        )
    return rows


def hand_audit_lines(base: WindowBase, store: ot.OptionsTradeStore) -> list[str]:
    lines: list[str] = []
    lines.append("--- HAND AUDIT (one complete window) ---")
    lines.append(f"entry_date: {base.entry_date}")
    lines.append(
        f"entry_time_IST: {base.entry_hhmm}  entry_time_UTC: "
        f"{base.entry_utc.isoformat()}"
    )
    lines.append(f"basket_expiry (2DTE settle): {base.basket_expiry}")
    lines.append(f"hedge_expiry (month_1): {base.hedge_expiry}")
    sr, lr = roles_for_package(base.fill_package)
    lines.append(
        f"fill_package: {base.fill_package} "
        f"(shorts buyer_role={sr}, longs buyer_role={lr})"
    )
    lines.append(f"spot_entry: {base.spot_entry:.2f}")
    lines.append(
        f"spot_settle (12:00 UTC on {base.basket_expiry}): {base.spot_settle:.2f}"
    )
    lines.append(f"|spot_move|: {abs(base.spot_settle - base.spot_entry):.2f}")
    lines.append("")
    lines.append("HEDGE (long ATM straddle, qty=4):")
    lines.append(
        f"  strike={base.hedge_atm:.0f}  "
        f"C entry={base.hedge_call.price:.2f} @ {base.hedge_call.ts_utc.isoformat()} "
        f"({base.hedge_call.buyer_role})  "
        f"P entry={base.hedge_put.price:.2f} @ {base.hedge_put.ts_utc.isoformat()} "
        f"({base.hedge_put.buyer_role})"
    )
    lines.append(
        f"  hedge_premium_avg="
        f"{(base.hedge_call.price + base.hedge_put.price) / 2:.2f}"
    )
    lines.append(
        f"  mark@settle C={base.hedge_mark_call.price:.2f} "
        f"P={base.hedge_mark_put.price:.2f}"
    )
    lines.append(
        f"  target_premium (25% of avg, ceil) = {base.target_premium:.0f}  "
        "[LIVE pct_of_hedge path - NOT target_premium_per_side]"
    )
    lines.append("")
    lines.append("BASKET (short strangle, qty=8 at 200%):")
    lines.append(
        f"  call_k={base.short_call_k:.0f} fill={base.short_call.price:.2f} "
        f"@ {base.short_call.ts_utc.isoformat()} ({base.short_call.buyer_role})"
    )
    lines.append(
        f"  put_k={base.short_put_k:.0f} fill={base.short_put.price:.2f} "
        f"@ {base.short_put.ts_utc.isoformat()} ({base.short_put.buyer_role})"
    )
    sc_i = call_intrinsic(base.spot_settle, base.short_call_k)
    sp_i = put_intrinsic(base.spot_settle, base.short_put_k)
    lines.append(f"  settlement intrinsic C={sc_i:.2f} P={sp_i:.2f}")
    lines.append("")
    if base.wing_call is not None and base.wing_put is not None:
        lines.append("WINGS (long, +2000 pts, qty=8):")
        lines.append(
            f"  call_k={base.wing_call_k:.0f} fill={base.wing_call.price:.2f} "
            f"@ {base.wing_call.ts_utc.isoformat()}"
        )
        lines.append(
            f"  put_k={base.wing_put_k:.0f} fill={base.wing_put.price:.2f} "
            f"@ {base.wing_put.ts_utc.isoformat()}"
        )
        wc_i = call_intrinsic(base.spot_settle, float(base.wing_call_k or 0))
        wp_i = put_intrinsic(base.spot_settle, float(base.wing_put_k or 0))
        lines.append(f"  settlement intrinsic C={wc_i:.2f} P={wp_i:.2f}")
    else:
        lines.append("WINGS: none (fills missing)")
    lines.append("")

    live = build_variant(base, True, 200)
    lines.append("LIVE CONFIG P&L (wings ON, 200%):")
    lines.append(f"  hedge_pnl:  {live.hedge_pnl:.4f}")
    lines.append(f"  basket_pnl: {live.basket_pnl:.4f}")
    lines.append(f"  wings_pnl:  {live.wings_pnl:.4f}")
    lines.append(f"  entry+hedge_exit_fees: {live.entry_fees:.4f}")
    lines.append(
        f"  settle_fees_unverified (basket+wings ITM): {live.settle_fees:.4f}"
    )
    lines.append(f"  NET (no settle fee):   {live.net_no_settle:.4f}")
    lines.append(f"  NET (with settle fee): {live.net_with_settle:.4f}")
    lines.append(
        f"  claim check: basket ({live.basket_pnl:.2f}) vs "
        f"-hedge ({-live.hedge_pnl:.2f}) -> "
        f"surplus={live.basket_pnl + live.hedge_pnl:.2f}"
    )
    lines.append("")
    lines.append("OptionsTradeStore re-check (short call, +/-5m, any role):")
    sym = format_symbol("C", base.short_call_k, base.basket_expiry)
    t = store.nearest_trade(sym, base.entry_utc, PRINT_WINDOW_SEC)
    if t is None:
        lines.append(f"  {sym}: no trade in store window")
    else:
        lines.append(
            f"  {sym}: price={t.price:.2f} role={t.buyer_role} "
            f"ts={t.ts_utc.isoformat()}"
        )
    return lines


def write_report(
    obs: list[VariantObs],
    meta: dict[str, Any],
    audit: WindowBase | None,
    runtime_s: float,
    store: ot.OptionsTradeStore,
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"s001_engine_measure_{stamp}.txt"
    lines: list[str] = []

    lines.append("=== S001 daily engine: hedge bleed vs basket collection ===")
    lines.append(f"runtime_s: {runtime_s:.1f}")
    lines.append(f"bootstrap_n: {BOOTSTRAP_N}  seed: {BOOTSTRAP_SEED}")
    lines.append(
        f"hedge_qty={HEDGE_QTY_LOTS}  basket=ceil(hedge*ratio/100)  "
        f"wing_points={WING_POINTS:.0f}  basket_dte={BASKET_DTE}"
    )
    lines.append(f"STRIKE PATH USED: {meta['strike_path']}")
    lines.append(f"month_1: {meta['month_1_rule']}")
    lines.append(f"LIMITATION: {meta['hedge_window_note']}")
    lines.append("")
    lines.append("FILL CONVENTION (package named by SHORT side, as VRP):")
    lines.append(
        "  maker package (conservative): shorts use buyer_role=maker; "
        "longs (hedge/wings) use buyer_role=taker (pay up)."
    )
    lines.append(
        "FILL WINDOWS: basket prefer +/-5m (fallback +/-15m); "
        "hedge entry +/-30m; hedge mark@settle +/-60m. "
        "Preferred buyer_role first, then any role in the same window "
        "(monthly ATM is thin)."
    )
    lines.append(
        "FEE: min(index*qtyBTC*0.0001, premium*qtyBTC*0.035)*1.18 per fill; "
        "qtyBTC=lots*0.001. Hedge exit fee on mark always included. "
        "ITM settlement fee on basket/wings UNVERIFIED - tables default to "
        "NO settlement fee; with-fee summary at end."
    )
    lines.append("")

    n_entry_slots = meta["n_basket_expiries"] * len(ENTRY_TIMES_IST)
    lines.append("--- COVERAGE ---")
    lines.append(f"basket_expiries_in_shards: {meta['n_basket_expiries']}")
    lines.append(f"entry_slots (expiries x 4 times): {n_entry_slots}")
    lines.append(
        f"usable_bases (entry x fill_package with full structure): "
        f"{meta['n_bases']}"
    )
    lines.append(f"variant_rows: {meta['n_variants']}")
    lines.append("drop_reasons:")
    for k, v in sorted(meta["drop_reasons"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.append("=" * 72)
    lines.append("1. HEADLINE DECOMPOSITION (no settlement fee)")
    lines.append(
        "   basket_vs_bleed = basket_mean + hedge_mean  "
        "(>0 means collection covers bleed)"
    )
    lines.append("=" * 72)

    for fill_pkg in FILL_PACKAGES:
        lines.append(f"\n## fill_package={fill_pkg}")
        for hh, mm in ENTRY_TIMES_IST:
            hhmm = f"{hh:02d}:{mm:02d}"
            lines.append(f"\n### entry {hhmm} IST")
            lines.append(
                f"{'wings':5} {'ratio':>5} {'n':>5} "
                f"{'hedge':>9} {'basket':>9} {'wingsPnL':>9} {'fees':>8} "
                f"{'NET':>9} {'bask+hedge':>10}"
            )
            for wings_on in (True, False):
                for ratio in QTY_RATIOS_PCT:
                    chunk = filter_obs(
                        obs,
                        entry_hhmm=hhmm,
                        fill_package=fill_pkg,
                        wings_on=wings_on,
                        qty_ratio_pct=ratio,
                    )
                    if not chunk:
                        continue
                    r = risk_block(chunk, use_settle=False)
                    surplus = r["basket_mean"] + r["hedge_mean"]
                    live_mark = ""
                    if wings_on and ratio == 200 and fill_pkg == "maker":
                        live_mark = "  <<<LIVE"
                    lines.append(
                        f"{'ON' if wings_on else 'OFF':5} {ratio:5d} {r['n']:5d} "
                        f"{r['hedge_mean']:9.2f} {r['basket_mean']:9.2f} "
                        f"{r['wings_mean']:9.2f} {r['fees_mean']:8.2f} "
                        f"{r['mean']:9.2f} {surplus:10.2f}{live_mark}"
                    )

    lines.append("")
    lines.append("=" * 72)
    lines.append(
        "2. NET BY |spot move| DECILES - LIVE "
        "(wings ON, 200%, maker, no settle fee)"
    )
    lines.append("=" * 72)
    live_chunk = filter_obs(
        obs, fill_package="maker", wings_on=True, qty_ratio_pct=200
    )
    for label, chunk in (
        ("ALL entry times", live_chunk),
        *(
            (
                f"entry {hh:02d}:{mm:02d}",
                filter_obs(
                    obs,
                    entry_hhmm=f"{hh:02d}:{mm:02d}",
                    fill_package="maker",
                    wings_on=True,
                    qty_ratio_pct=200,
                ),
            )
            for hh, mm in ENTRY_TIMES_IST
        ),
    ):
        lines.append(f"\n[{label}] n={len(chunk)}")
        rows = decile_blocks(chunk, use_settle=False)
        if not rows:
            lines.append("  (insufficient n for deciles)")
            continue
        lines.append(
            f"  {'dec':>3} {'n':>5} {'|move|lo':>10} {'|move|hi':>10} "
            f"{'NET':>9} {'hedge':>9} {'basket':>9} {'wings':>9}"
        )
        for row in rows:
            lines.append(
                f"  {row['decile']:3d} {row['n']:5d} "
                f"{row['move_lo']:10.1f} {row['move_hi']:10.1f} "
                f"{row['net_mean']:9.2f} {row['hedge_mean']:9.2f} "
                f"{row['basket_mean']:9.2f} {row['wings_mean']:9.2f}"
            )
        if len(rows) >= 2:
            gap = rows[-1]["net_mean"] - rows[0]["net_mean"]
            lines.append(
                f"  gamma_cost_proxy (decile10_net - decile1_net) = {gap:.2f}"
            )

    lines.append("")
    lines.append("=" * 72)
    lines.append(
        "3. RISK (no settle fee) - ranked by SORTINO, maker package"
    )
    lines.append("=" * 72)
    for hh, mm in ENTRY_TIMES_IST:
        hhmm = f"{hh:02d}:{mm:02d}"
        lines.append(f"\n### entry {hhmm} IST")
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for wings_on in (True, False):
            for ratio in QTY_RATIOS_PCT:
                chunk = filter_obs(
                    obs,
                    entry_hhmm=hhmm,
                    fill_package="maker",
                    wings_on=wings_on,
                    qty_ratio_pct=ratio,
                )
                r = risk_block(chunk, use_settle=False)
                if r.get("n", 0) == 0:
                    continue
                so = r["sortino"]
                so_key = (
                    so
                    if isinstance(so, float) and not math.isnan(so)
                    else -1e99
                )
                label = f"wings={'ON' if wings_on else 'OFF'} ratio={ratio}%"
                if wings_on and ratio == 200:
                    label += " [LIVE]"
                ranked.append((so_key, label, r))
        ranked.sort(key=lambda t: t[0], reverse=True)
        lines.append(
            f"{'rank':>4} {'variant':<28} {'n':>5} {'mean':>9} {'med':>9} "
            f"{'CI95lo':>9} {'CI95hi':>9} {'p5':>9} {'worst':>9} "
            f"{'date':>12} {'Sortino':>8} {'maxDD':>9}"
        )
        for i, (_sk, label, r) in enumerate(ranked, 1):
            lines.append(
                f"{i:4d} {label:<28} {r['n']:5d} {r['mean']:9.2f} "
                f"{r['median']:9.2f} {r['ci_lo']:9.2f} {r['ci_hi']:9.2f} "
                f"{r['p5']:9.2f} {r['worst']:9.2f} {r['worst_date']:>12} "
                f"{fmt(r['sortino'], 3):>8} {r['max_dd']:9.2f}"
            )

    lines.append("")
    lines.append("=" * 72)
    lines.append(
        "4. WINGS ON vs OFF (paired on same bases with wing fills) - maker, "
        "no settle fee. Design predicts: mean DOWN, Sortino UP."
    )
    lines.append("=" * 72)
    for hh, mm in ENTRY_TIMES_IST:
        hhmm = f"{hh:02d}:{mm:02d}"
        lines.append(f"\n### entry {hhmm} IST")
        lines.append(
            f"{'ratio':>5} {'d_mean':>9} {'d_p5':>9} {'d_worst':>9} "
            f"{'d_Sortino':>10} {'mean_down':>10} {'sortino_up':>11}"
        )
        for ratio in QTY_RATIOS_PCT:
            on = filter_obs(
                obs,
                entry_hhmm=hhmm,
                fill_package="maker",
                wings_on=True,
                qty_ratio_pct=ratio,
            )
            # Same bases only: wing fills present (OFF rows exist for wingless
            # days too; pairing those would mix regimes).
            off = [
                o
                for o in filter_obs(
                    obs,
                    entry_hhmm=hhmm,
                    fill_package="maker",
                    wings_on=False,
                    qty_ratio_pct=ratio,
                )
                if o.base.wing_call is not None and o.base.wing_put is not None
            ]
            if not on or not off:
                continue
            ron = risk_block(on, use_settle=False)
            roff = risk_block(off, use_settle=False)
            d_mean = ron["mean"] - roff["mean"]
            d_p5 = ron["p5"] - roff["p5"]
            d_worst = ron["worst"] - roff["worst"]
            so_on = ron["sortino"]
            so_off = roff["sortino"]
            d_so = float("nan")
            if (
                isinstance(so_on, float)
                and isinstance(so_off, float)
                and not math.isinf(so_on)
                and not math.isinf(so_off)
                and not math.isnan(so_on)
                and not math.isnan(so_off)
            ):
                d_so = so_on - so_off
            lines.append(
                f"{ratio:5d} {d_mean:9.2f} {d_p5:9.2f} {d_worst:9.2f} "
                f"{fmt(d_so, 3):>10} "
                f"{'YES' if d_mean < 0 else 'NO':>10} "
                f"{'YES' if (isinstance(d_so, float) and not math.isnan(d_so) and d_so > 0) else 'NO':>11}"
            )

    lines.append("")
    lines.append("=" * 72)
    lines.append(
        "SETTLEMENT FEE SENSITIVITY - LIVE (wings ON, 200%, maker) "
        "ITM settle fee UNVERIFIED"
    )
    lines.append("=" * 72)
    for hh, mm in ENTRY_TIMES_IST:
        hhmm = f"{hh:02d}:{mm:02d}"
        chunk = filter_obs(
            obs,
            entry_hhmm=hhmm,
            fill_package="maker",
            wings_on=True,
            qty_ratio_pct=200,
        )
        r0 = risk_block(chunk, use_settle=False)
        r1 = risk_block(chunk, use_settle=True)
        if r0.get("n", 0) == 0:
            continue
        lines.append(
            f"  {hhmm}: n={r0['n']}  NET_no_settle={r0['mean']:.2f} "
            f"NET_with_settle={r1['mean']:.2f}  "
            f"Sortino {fmt(r0['sortino'], 3)} -> {fmt(r1['sortino'], 3)}"
        )

    lines.append("")
    if audit is not None:
        lines.extend(hand_audit_lines(audit, store))
    else:
        lines.append(
            "--- HAND AUDIT: no complete wings-ON maker 11:00 window found ---"
        )

    lines.append("")
    lines.append(f"runtime_s: {runtime_s:.1f}")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (RESULTS_DIR / "s001_engine_measure_latest.txt").write_text(
        text, encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="S001 daily engine measure").parse_args(
        argv
    )
    t0 = time.time()
    print("Building trade index...", flush=True)
    idx = build_trade_index()
    print("Measuring windows...", flush=True)
    obs, meta, audit = measure_all(idx)
    store = ot.OptionsTradeStore(CACHE_DIR)
    runtime = time.time() - t0
    path = write_report(obs, meta, audit, runtime, store)
    print(f"Wrote {path}", flush=True)
    print(f"Also {RESULTS_DIR / 's001_engine_measure_latest.txt'}", flush=True)
    print(f"runtime_s={runtime:.1f} variants={len(obs)}", flush=True)
    live = filter_obs(obs, fill_package="maker", wings_on=True, qty_ratio_pct=200)
    if live:
        r = risk_block(live, use_settle=False)
        surplus = r["basket_mean"] + r["hedge_mean"]
        print(
            f"LIVE all-times: n={r['n']} hedge={r['hedge_mean']:.2f} "
            f"basket={r['basket_mean']:.2f} wings={r['wings_mean']:.2f} "
            f"NET={r['mean']:.2f} basket+hedge={surplus:.2f} "
            f"Sortino={fmt(r['sortino'], 3)}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
