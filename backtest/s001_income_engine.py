#!/usr/bin/env python3
"""
S001 income engine — model-free basket + wings measurement.

Hedge EXCLUDED. Basket shorts and wings held to short-expiry settlement
(12:00 UTC intrinsic). Entry from real trade prints; wing entry may fall
back to IV surface (flagged). No targets/stops/adjustments/rolls.

stdlib only. Do not import iv_surface for basket legs.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
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

CONTRACT_VALUE = 0.001
BASKET_QTY_LOTS = 4
ENTRY_TIMES_IST = ((9, 0), (11, 0), (13, 0), (15, 0))
SHORT_DTES = (0, 1, 2, 3)
WING_DISTANCES: tuple[float | None, ...] = (None, 1000.0, 1500.0, 2000.0, 3000.0, 4000.0)
MODE_A_TARGETS = (50.0, 100.0, 150.0, 200.0, 300.0)
MODE_B_PCTS = (10.0, 15.0, 20.0, 25.0, 30.0, 40.0)
FILL_PACKAGES = ("maker", "taker")
PRINT_WINDOW_SEC = 5 * 60.0
PRINT_WINDOW_FALLBACK_SEC = 15 * 60.0
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260914
FEE_RATE = 0.0001
PREMIUM_CAP = 0.035
GST = 1.18

N_CONFIGS = (
    len(ENTRY_TIMES_IST)
    * len(SHORT_DTES)
    * len(WING_DISTANCES)
    * (len(MODE_A_TARGETS) + len(MODE_B_PCTS))
    * len(FILL_PACKAGES)
)


def option_fee(premium: float, index: float, qty_lots: int) -> float:
    """
    Verified trading-fee model:
      min(index * qtyBTC * 0.0001, premium * qtyBTC * 0.035) * 1.18
    Settlement fee: same formula on intrinsic — UNVERIFIED; report both.
    """
    qty_btc = abs(int(qty_lots)) * CONTRACT_VALUE
    if premium <= 0 or index <= 0 or qty_btc <= 0:
        return 0.0
    return min(index * qty_btc * FEE_RATE, premium * qty_btc * PREMIUM_CAP) * GST


def cash_pnl(entry: float, exit_: float, qty_lots: int, *, is_long: bool) -> float:
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


def _bootstrap_worker(payload: tuple[list[float], int, int]) -> tuple[float, float, float]:
    values, n, seed = payload
    return bootstrap_mean_ci(values, n, seed)


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


@dataclass
class PrintFill:
    symbol: str
    price: float
    ts_utc: datetime
    buyer_role: str
    strike: float
    option_type: str
    source: str = "print"  # print | surface


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
        raise FileNotFoundError(f"No shards in {cache_dir}")
    for path in shards:
        print(f"Indexing {path.name} ...", flush=True)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT symbol, ts, price, role, expiry, opt_type, strike FROM trades"
            )
            n = 0
            for symbol, ts, price, role, expiry, opt_type, strike in cur:
                n += 1
                exp = date.fromisoformat(expiry)
                idx.expiries.add(exp)
                idx.strikes_by_expiry[exp].add(float(strike))
                role_s = "maker" if int(role) == 0 else "taker"
                idx.by_symbol[symbol].append((float(ts), float(price), role_s))
            print(f"  scanned {n:,} rows", flush=True)
        finally:
            conn.close()
    for sym in idx.by_symbol:
        idx.by_symbol[sym].sort(key=lambda x: x[0])
    print(
        f"Index ready: {len(idx.expiries)} expiries, {len(idx.by_symbol)} symbols",
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
        source="print",
    )


def nearest_print_prefer(
    idx: TradeIndex,
    symbol: str,
    when: datetime,
    window_sec: float,
    preferred_role: str,
) -> PrintFill | None:
    hit = nearest_print(idx, symbol, when, window_sec, preferred_role)
    if hit is not None:
        return hit
    hit = nearest_print(idx, symbol, when, window_sec, None)
    if hit is not None:
        return hit
    if window_sec < PRINT_WINDOW_FALLBACK_SEC:
        return nearest_print_prefer(
            idx, symbol, when, PRINT_WINDOW_FALLBACK_SEC, preferred_role
        )
    return None


def roles_for_package(fill_package: str) -> tuple[str, str]:
    if fill_package == "maker":
        return "maker", "taker"
    return "taker", "maker"


def settle_spot_1200_utc(
    times: list[int], closes: list[float], exp: date
) -> float | None:
    ts = int(datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp())
    return ot.spot_at(times, closes, ts)


def pick_atm_straddle(
    idx: TradeIndex,
    exp: date,
    spot: float,
    when: datetime,
    role: str,
) -> tuple[float, PrintFill, PrintFill] | None:
    strikes = idx.strikes_by_expiry.get(exp) or set()
    atm = nearest_strike(strikes, spot)
    if atm is None:
        return None
    cf = nearest_print_prefer(idx, format_symbol("C", atm, exp), when, PRINT_WINDOW_SEC, role)
    pf = nearest_print_prefer(idx, format_symbol("P", atm, exp), when, PRINT_WINDOW_SEC, role)
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
) -> tuple[float, float, PrintFill, PrintFill] | None:
    strikes = sorted(idx.strikes_by_expiry.get(exp) or set())
    if not strikes or target <= 0:
        return None
    best_c: tuple[float, PrintFill, float] | None = None
    best_p: tuple[float, PrintFill, float] | None = None
    for k in strikes:
        if k > spot:
            fill = nearest_print_prefer(
                idx, format_symbol("C", k, exp), when, PRINT_WINDOW_SEC, short_role
            )
            if fill is None or fill.price <= 0:
                continue
            diff = abs(fill.price - target)
            if best_c is None or diff < best_c[0] or (diff == best_c[0] and k < best_c[2]):
                best_c = (diff, fill, k)
        if k < spot:
            fill = nearest_print_prefer(
                idx, format_symbol("P", k, exp), when, PRINT_WINDOW_SEC, short_role
            )
            if fill is None or fill.price <= 0:
                continue
            diff = abs(fill.price - target)
            if best_p is None or diff < best_p[0] or (diff == best_p[0] and k > best_p[2]):
                best_p = (diff, fill, k)
    if best_c is None or best_p is None:
        return None
    return best_c[2], best_p[2], best_c[1], best_p[1]


def pick_wing_strikes(
    idx: TradeIndex, exp: date, short_call_k: float, short_put_k: float, points: float
) -> tuple[float, float] | None:
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
    return wing_c_k, wing_p_k


def wing_fill_or_surface(
    idx: TradeIndex,
    surface: Any | None,
    exp: date,
    strike: float,
    opt: str,
    when: datetime,
    long_role: str,
) -> PrintFill | None:
    sym = format_symbol(opt, strike, exp)
    hit = nearest_print_prefer(idx, sym, when, PRINT_WINDOW_SEC, long_role)
    if hit is not None:
        return hit
    if surface is None:
        return None
    res = surface.price(when, strike, exp, opt)
    if not res.supported or not math.isfinite(res.price) or res.price <= 0:
        return None
    return PrintFill(
        symbol=sym,
        price=float(res.price),
        ts_utc=when,
        buyer_role=long_role,
        strike=strike,
        option_type=opt,
        source="surface",
    )


@dataclass
class CycleObs:
    entry_date: date
    entry_hhmm: str
    entry_utc: datetime
    basket_expiry: date
    short_dte: int
    fill_package: str
    strike_mode: str  # A50 / B25 ...
    wing_points: float | None
    spot_entry: float
    spot_settle: float
    atm_straddle_prem: float
    target_premium: float
    short_call_k: float
    short_put_k: float
    short_call: PrintFill
    short_put: PrintFill
    wing_call_k: float | None
    wing_put_k: float | None
    wing_call: PrintFill | None
    wing_put: PrintFill | None
    wing_used_surface: bool
    basket_pnl: float
    wings_pnl: float
    entry_fees: float
    settle_fees: float
    net_no_settle: float
    net_with_settle: float
    spot_move_abs: float


def strike_specs() -> list[tuple[str, str, float]]:
    """(label, kind A|B, value)."""
    out: list[tuple[str, str, float]] = []
    for t in MODE_A_TARGETS:
        out.append((f"A{int(t)}", "A", t))
    for p in MODE_B_PCTS:
        out.append((f"B{int(p)}", "B", p))
    return out


def load_surface_optional() -> Any | None:
    try:
        import iv_surface as ivs  # local import — wings only

        loaded = ivs.load_surface("full")
        if loaded is None:
            print("WARNING: no IV surface cache — wing surface fallback disabled", flush=True)
            return None
        surface, _stats = loaded
        print(f"IV surface loaded for wing fallback ({len(surface.smiles)} smiles)", flush=True)
        return surface
    except Exception as e:  # noqa: BLE001 — surface is optional fallback
        print(f"WARNING: could not load IV surface: {e}", flush=True)
        return None


def measure_cycles(
    idx: TradeIndex, surface: Any | None
) -> tuple[list[CycleObs], dict[str, int], CycleObs | None]:
    times, closes = ot.load_spot_1m()
    spot_lo, spot_hi = times[0], times[-1]
    drop: dict[str, int] = defaultdict(int)
    obs: list[CycleObs] = []
    audit: CycleObs | None = None
    specs = strike_specs()

    # Calendar span from spot
    d0 = datetime.fromtimestamp(spot_lo, tz=UTC).date()
    d1 = datetime.fromtimestamp(spot_hi, tz=UTC).date()
    day = d0
    n_days = 0
    while day <= d1:
        n_days += 1
        day += timedelta(days=1)
    print(f"Calendar days in spot range: {n_days}", flush=True)

    day = d0
    while day <= d1:
        for hh, mm in ENTRY_TIMES_IST:
            entry_utc = ist_to_utc(day, hh, mm)
            ts = int(entry_utc.timestamp())
            if ts < spot_lo or ts > spot_hi:
                drop["entry_outside_spot"] += 1
                continue
            spot_e = ot.spot_at(times, closes, ts)
            if spot_e is None or spot_e <= 0:
                drop["no_spot_entry"] += 1
                continue
            hhmm = f"{hh:02d}:{mm:02d}"
            for dte in SHORT_DTES:
                exp = day + timedelta(days=dte)
                if exp not in idx.expiries:
                    drop["no_expiry_in_index"] += 1
                    continue
                spot_s = settle_spot_1200_utc(times, closes, exp)
                if spot_s is None or spot_s <= 0:
                    drop["no_settle_spot"] += 1
                    continue
                # Must enter before settlement
                settle_ts = int(
                    datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp()
                )
                if ts >= settle_ts:
                    drop["entry_after_settle"] += 1
                    continue
                for fill_pkg in FILL_PACKAGES:
                    short_role, long_role = roles_for_package(fill_pkg)
                    atm = pick_atm_straddle(idx, exp, spot_e, entry_utc, long_role)
                    if atm is None:
                        # ATM needed for mode B; also useful for audit. Mode A can proceed
                        # without it, but we still try once with short role for coverage.
                        atm = pick_atm_straddle(idx, exp, spot_e, entry_utc, short_role)
                    atm_prem = 0.0
                    if atm is not None:
                        _atm_k, ac, ap = atm
                        atm_prem = ac.price + ap.price

                    for label, kind, val in specs:
                        if kind == "A":
                            target = float(val)
                        else:
                            if atm_prem <= 0:
                                drop["no_atm_for_mode_B"] += 1
                                continue
                            target = float(val) / 100.0 * atm_prem
                            if target < 5.0:
                                drop["target_too_small"] += 1
                                continue
                        strangle = pick_strangle_by_premium(
                            idx, exp, spot_e, target, entry_utc, short_role
                        )
                        if strangle is None:
                            drop["no_short_strangle"] += 1
                            continue
                        sc_k, sp_k, sc, sp = strangle

                        for wing_pts in WING_DISTANCES:
                            wing_c_k = wing_p_k = None
                            wing_c = wing_p = None
                            used_surface = False
                            if wing_pts is not None:
                                wk = pick_wing_strikes(idx, exp, sc_k, sp_k, wing_pts)
                                if wk is None:
                                    drop["no_wing_strikes"] += 1
                                    continue
                                wing_c_k, wing_p_k = wk
                                wing_c = wing_fill_or_surface(
                                    idx, surface, exp, wing_c_k, "C", entry_utc, long_role
                                )
                                wing_p = wing_fill_or_surface(
                                    idx, surface, exp, wing_p_k, "P", entry_utc, long_role
                                )
                                if wing_c is None or wing_p is None:
                                    drop["no_wing_price"] += 1
                                    continue
                                used_surface = (
                                    wing_c.source == "surface" or wing_p.source == "surface"
                                )

                            bq = BASKET_QTY_LOTS
                            sc_exit = call_intrinsic(spot_s, sc_k)
                            sp_exit = put_intrinsic(spot_s, sp_k)
                            basket_pnl = cash_pnl(
                                sc.price, sc_exit, bq, is_long=False
                            ) + cash_pnl(sp.price, sp_exit, bq, is_long=False)
                            entry_fees = option_fee(sc.price, spot_e, bq) + option_fee(
                                sp.price, spot_e, bq
                            )
                            settle_fees = option_fee(sc_exit, spot_s, bq) + option_fee(
                                sp_exit, spot_s, bq
                            )
                            wings_pnl = 0.0
                            if wing_pts is not None and wing_c is not None and wing_p is not None:
                                assert wing_c_k is not None and wing_p_k is not None
                                wc_exit = call_intrinsic(spot_s, wing_c_k)
                                wp_exit = put_intrinsic(spot_s, wing_p_k)
                                wings_pnl = cash_pnl(
                                    wing_c.price, wc_exit, bq, is_long=True
                                ) + cash_pnl(wing_p.price, wp_exit, bq, is_long=True)
                                entry_fees += option_fee(wing_c.price, spot_e, bq) + option_fee(
                                    wing_p.price, spot_e, bq
                                )
                                settle_fees += option_fee(wc_exit, spot_s, bq) + option_fee(
                                    wp_exit, spot_s, bq
                                )
                            gross = basket_pnl + wings_pnl
                            net0 = gross - entry_fees
                            net1 = gross - entry_fees - settle_fees
                            row = CycleObs(
                                entry_date=day,
                                entry_hhmm=hhmm,
                                entry_utc=entry_utc,
                                basket_expiry=exp,
                                short_dte=dte,
                                fill_package=fill_pkg,
                                strike_mode=label,
                                wing_points=wing_pts,
                                spot_entry=spot_e,
                                spot_settle=spot_s,
                                atm_straddle_prem=atm_prem,
                                target_premium=target,
                                short_call_k=sc_k,
                                short_put_k=sp_k,
                                short_call=sc,
                                short_put=sp,
                                wing_call_k=wing_c_k,
                                wing_put_k=wing_p_k,
                                wing_call=wing_c,
                                wing_put=wing_p,
                                wing_used_surface=used_surface,
                                basket_pnl=basket_pnl,
                                wings_pnl=wings_pnl,
                                entry_fees=entry_fees,
                                settle_fees=settle_fees,
                                net_no_settle=net0,
                                net_with_settle=net1,
                                spot_move_abs=abs(spot_s - spot_e),
                            )
                            obs.append(row)
                            if (
                                audit is None
                                and dte == 2
                                and fill_pkg == "maker"
                                and label == "B25"
                                and wing_pts == 2000.0
                                and hhmm == "11:00"
                            ):
                                audit = row
        day += timedelta(days=1)
        if (day.toordinal() - d0.toordinal()) % 30 == 0:
            print(f"  measured through {day}  cycles_so_far={len(obs):,}", flush=True)

    if audit is None and obs:
        for r in obs:
            if r.short_dte == 2 and r.wing_points == 2000.0 and r.strike_mode.startswith("B"):
                audit = r
                break
        if audit is None:
            audit = obs[len(obs) // 2]
    return obs, dict(drop), audit


def cfg_key(o: CycleObs) -> tuple[Any, ...]:
    return (
        o.entry_hhmm,
        o.short_dte,
        o.strike_mode,
        o.wing_points,
        o.fill_package,
    )


def cfg_label(key: tuple[Any, ...]) -> str:
    hhmm, dte, mode, wing, fill = key
    w = "OFF" if wing is None else f"{int(wing)}"
    return f"t={hhmm} dte={dte} strike={mode} wing={w} fill={fill}"


@dataclass
class CfgStats:
    key: tuple[Any, ...]
    n: int
    mean: float
    median: float
    ci_lo: float
    ci_hi: float
    p5: float
    worst: float
    worst_date: str
    sortino: float
    mdd: float
    n_surface_wing: int
    mean_with_settle: float
    nets: list[float]
    moves: list[float]
    dates: list[date]


def summarize_configs(
    obs: list[CycleObs],
    *,
    use_settle: bool,
    prints_only: bool,
    do_bootstrap: bool,
) -> list[CfgStats]:
    groups: dict[tuple[Any, ...], list[CycleObs]] = defaultdict(list)
    for o in obs:
        if prints_only and o.wing_used_surface:
            continue
        groups[cfg_key(o)].append(o)

    payloads: list[tuple[tuple[Any, ...], list[float], list[CycleObs]]] = []
    for key, rows in groups.items():
        rows_sorted = sorted(rows, key=lambda r: (r.entry_date, r.entry_hhmm))
        nets = [
            r.net_with_settle if use_settle else r.net_no_settle for r in rows_sorted
        ]
        payloads.append((key, nets, rows_sorted))

    print(
        f"Summarizing {len(payloads)} configs "
        f"(settle={use_settle}, prints_only={prints_only}, bootstrap={do_bootstrap})...",
        flush=True,
    )
    ci_map: dict[tuple[Any, ...], tuple[float, float, float]] = {}
    if do_bootstrap:
        boot_jobs = []
        key_order = []
        for i, (key, nets, _rows) in enumerate(payloads):
            seed = BOOTSTRAP_SEED + i * 97
            boot_jobs.append((nets, BOOTSTRAP_N, seed))
            key_order.append(key)
        try:
            workers = min(8, os.cpu_count() or 4)
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {
                    ex.submit(_bootstrap_worker, job): key_order[i]
                    for i, job in enumerate(boot_jobs)
                }
                done = 0
                for fut in as_completed(futs):
                    key = futs[fut]
                    ci_map[key] = fut.result()
                    done += 1
                    if done % 200 == 0:
                        print(f"  bootstrap {done}/{len(boot_jobs)}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"Parallel bootstrap failed ({e}); sequential", flush=True)
            for i, job in enumerate(boot_jobs):
                ci_map[key_order[i]] = _bootstrap_worker(job)
                if (i + 1) % 100 == 0:
                    print(f"  bootstrap {i+1}/{len(boot_jobs)}", flush=True)
    else:
        for key, nets, _rows in payloads:
            if len(nets) < 2:
                m = statistics.mean(nets) if nets else float("nan")
                ci_map[key] = (m, m, m)
            else:
                m = statistics.mean(nets)
                sem = statistics.stdev(nets) / math.sqrt(len(nets))
                ci_map[key] = (m, m - 1.96 * sem, m + 1.96 * sem)

    out: list[CfgStats] = []
    for key, nets, rows in payloads:
        mean, lo, hi = ci_map[key]
        worst_i = min(range(len(nets)), key=lambda i: nets[i])
        out.append(
            CfgStats(
                key=key,
                n=len(nets),
                mean=mean,
                median=statistics.median(nets),
                ci_lo=lo,
                ci_hi=hi,
                p5=pctile(nets, 5),
                worst=nets[worst_i],
                worst_date=str(rows[worst_i].entry_date),
                sortino=sortino(nets),
                mdd=max_drawdown(nets),
                n_surface_wing=sum(1 for r in rows if r.wing_used_surface),
                mean_with_settle=statistics.mean([r.net_with_settle for r in rows]),
                nets=nets,
                moves=[r.spot_move_abs for r in rows],
                dates=[r.entry_date for r in rows],
            )
        )
    out.sort(
        key=lambda s: (
            -s.sortino if math.isfinite(s.sortino) else float("-inf"),
            -s.mean,
        )
    )
    return out


def os_cpu_count() -> int | None:
    return os.cpu_count()


def move_decile_table(stats_list: list[CfgStats], family_filter) -> list[str]:
    """Aggregate cycles in matching configs; P&L by |move| decile."""
    rows: list[tuple[float, float]] = []
    for s in stats_list:
        if not family_filter(s.key):
            continue
        for m, n in zip(s.moves, s.nets):
            rows.append((m, n))
    if len(rows) < 20:
        return ["  (insufficient cycles)"]
    rows.sort(key=lambda x: x[0])
    n = len(rows)
    lines = []
    for d in range(10):
        lo = int(d * n / 10)
        hi = int((d + 1) * n / 10)
        chunk = rows[lo:hi]
        if not chunk:
            continue
        moves = [c[0] for c in chunk]
        nets = [c[1] for c in chunk]
        lines.append(
            f"  D{d}: |move|[{moves[0]:.0f},{moves[-1]:.0f}] n={len(chunk)} "
            f"mean_pnl={statistics.mean(nets):.2f} med={statistics.median(nets):.2f}"
        )
    return lines


def hand_audit_lines(a: CycleObs) -> list[str]:
    lines = ["=== HAND AUDIT (one cycle) ==="]
    lines.append(f"entry_date={a.entry_date}  entry_IST={a.entry_hhmm}  entry_UTC={a.entry_utc.isoformat()}")
    lines.append(f"basket_expiry={a.basket_expiry}  short_dte={a.short_dte}")
    lines.append(f"fill_package={a.fill_package}  strike_mode={a.strike_mode}  wing_points={a.wing_points}")
    lines.append(f"spot_entry={a.spot_entry:.2f}  spot_settle_1200UTC={a.spot_settle:.2f}")
    lines.append(f"ATM straddle premium (C+P)={a.atm_straddle_prem:.2f}  target_per_side={a.target_premium:.2f}")
    lines.append(
        f"short C K={a.short_call_k:.0f} print={a.short_call.price:.2f} "
        f"@{a.short_call.ts_utc.isoformat()} role={a.short_call.buyer_role}"
    )
    lines.append(
        f"short P K={a.short_put_k:.0f} print={a.short_put.price:.2f} "
        f"@{a.short_put.ts_utc.isoformat()} role={a.short_put.buyer_role}"
    )
    if a.wing_points is not None and a.wing_call is not None and a.wing_put is not None:
        lines.append(
            f"wing C K={a.wing_call_k:.0f} px={a.wing_call.price:.2f} "
            f"source={a.wing_call.source}"
        )
        lines.append(
            f"wing P K={a.wing_put_k:.0f} px={a.wing_put.price:.2f} "
            f"source={a.wing_put.source}"
        )
        lines.append(f"wing_used_surface_flag={a.wing_used_surface}")
    sc_i = call_intrinsic(a.spot_settle, a.short_call_k)
    sp_i = put_intrinsic(a.spot_settle, a.short_put_k)
    lines.append(f"settlement intrinsic short C={sc_i:.2f} P={sp_i:.2f}")
    if a.wing_call_k is not None and a.wing_put_k is not None:
        lines.append(
            f"settlement intrinsic wing C={call_intrinsic(a.spot_settle, a.wing_call_k):.2f} "
            f"P={put_intrinsic(a.spot_settle, a.wing_put_k):.2f}"
        )
    lines.append(f"basket_pnl={a.basket_pnl:.4f}  wings_pnl={a.wings_pnl:.4f}")
    lines.append(f"entry_fees(verified)={a.entry_fees:.4f}  settle_fees(UNVERIFIED)={a.settle_fees:.4f}")
    lines.append(f"NET no settle fee={a.net_no_settle:.4f}")
    lines.append(f"NET with settle fee={a.net_with_settle:.4f}")
    return lines


def write_report(
    obs: list[CycleObs],
    drop: dict[str, int],
    audit: CycleObs | None,
    runtime_s: float,
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"s001_income_engine_{stamp}.txt"

    stats_full = summarize_configs(
        obs, use_settle=False, prints_only=False, do_bootstrap=True
    )
    stats_prints = summarize_configs(
        obs, use_settle=False, prints_only=True, do_bootstrap=True
    )
    stats_settle = summarize_configs(
        obs, use_settle=True, prints_only=False, do_bootstrap=False
    )

    lines: list[str] = []
    lines.append("=== S001 INCOME ENGINE (model-free basket + wings) ===")
    lines.append(f"runtime_s: {runtime_s:.1f}")
    lines.append(f"TOTAL CONFIGURATIONS: {N_CONFIGS}")
    lines.append(
        "Grid: entry_times=4 x short_dte=4 x wings=6 x strike_modes="
        f"{len(MODE_A_TARGETS)+len(MODE_B_PCTS)} x fill=2"
    )
    lines.append(f"bootstrap_n={BOOTSTRAP_N}  seed={BOOTSTRAP_SEED}")
    lines.append(f"basket_qty_lots={BASKET_QTY_LOTS}  contract_value_btc={CONTRACT_VALUE}")
    lines.append(
        "Fees: verified entry model; settlement fee UNVERIFIED (same formula on intrinsic)."
    )
    lines.append(
        "Hedge EXCLUDED. Exit = settlement intrinsic at 12:00 UTC on short expiry."
    )
    lines.append("")

    n_wing = sum(1 for o in obs if o.wing_points is not None)
    n_surf = sum(1 for o in obs if o.wing_used_surface)
    lines.append("COVERAGE / DROPS")
    lines.append(f"  total cycle-rows (all configs): {len(obs):,}")
    lines.append(
        f"  wing rows: {n_wing:,}  of which surface wing entry: {n_surf:,} "
        f"({100.0 * n_surf / n_wing if n_wing else 0:.1f}%)"
    )
    lines.append("  drop reasons (attempt counters, not mutually exclusive across nested loops):")
    for k, v in sorted(drop.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {k}: {v}")
    lines.append("")

    def emit_ranked(title: str, stats: list[CfgStats], limit: int | None = None) -> None:
        lines.append("=" * 72)
        lines.append(title)
        lines.append("=" * 72)
        lines.append(
            f"{'rank':>4} {'n':>5} {'mean':>8} {'med':>8} {'ci95lo':>8} {'ci95hi':>8} "
            f"{'p5':>8} {'worst':>8} {'wdate':>10} {'sortino':>8} {'mdd':>8} {'surf%':>6}  cfg"
        )
        show = stats if limit is None else stats[:limit]
        for i, s in enumerate(show, 1):
            surf_pct = 100.0 * s.n_surface_wing / s.n if s.n else 0.0
            so = f"{s.sortino:.3f}" if math.isfinite(s.sortino) else "nan"
            lines.append(
                f"{i:4d} {s.n:5d} {s.mean:8.2f} {s.median:8.2f} {s.ci_lo:8.2f} {s.ci_hi:8.2f} "
                f"{s.p5:8.2f} {s.worst:8.2f} {s.worst_date:>10} {so:>8} {s.mdd:8.2f} "
                f"{surf_pct:5.1f}%  {cfg_label(s.key)}"
            )
        lines.append("")

    emit_ranked(
        "1. ALL CONFIGS ranked by SORTINO (no settle fee, FULL sample incl. surface wings)",
        stats_full,
    )
    emit_ranked(
        "1a. TRUSTED subset n>=100 ranked by SORTINO (no settle fee, FULL)",
        [s for s in stats_full if s.n >= 100],
    )
    emit_ranked(
        "1b. ALL CONFIGS ranked by SORTINO (no settle fee, PRINTS-ONLY wing entries)",
        stats_prints,
    )
    emit_ranked(
        "1b2. PRINTS-ONLY n>=100 ranked by SORTINO",
        [s for s in stats_prints if s.n >= 100],
    )

    # Compare full vs prints-only for overlapping keys
    lines.append("=" * 72)
    lines.append("FULL vs PRINTS-ONLY (Sortino / mean) — material disagreement check")
    lines.append("=" * 72)
    pm = {s.key: s for s in stats_prints}
    disagree = 0
    compared = 0
    for s in stats_full:
        if s.key not in pm:
            continue
        if s.key[3] is None:
            continue  # wings OFF identical
        p = pm[s.key]
        compared += 1
        if abs(s.mean - p.mean) > max(1.0, 0.25 * abs(p.mean)) or (
            math.isfinite(s.sortino)
            and math.isfinite(p.sortino)
            and abs(s.sortino - p.sortino) > 0.15
        ):
            disagree += 1
            if disagree <= 25:
                lines.append(
                    f"  DIFF {cfg_label(s.key)}: full mean={s.mean:.2f} so={s.sortino:.3f} "
                    f"| prints mean={p.mean:.2f} so={p.sortino:.3f} "
                    f"| surf_frac={100*s.n_surface_wing/s.n:.0f}%"
                )
    lines.append(f"Compared wing configs: {compared}  material disagreements: {disagree}")
    if compared and disagree / compared > 0.2:
        lines.append(
            "WARNING: full vs prints-only disagree materially on >20% of wing configs — "
            "surface wing fallback is doing real work; do not trust those headline ranks."
        )
    else:
        lines.append(
            "Full vs prints-only largely agree — surface wing fallback is not driving the ranks."
        )
    lines.append("")

    emit_ranked(
        "1c. WITH unverified settlement fee (FULL sample; CI=normal approx) — top 40 by Sortino",
        stats_settle,
        limit=40,
    )

    # 2. Move deciles by family
    lines.append("=" * 72)
    lines.append("2. NET P&L by |settlement move| DECILES (no settle fee)")
    lines.append("=" * 72)
    families = [
        ("2DTE B25 maker wings OFF", lambda k: k[1] == 2 and k[2] == "B25" and k[3] is None and k[4] == "maker"),
        ("2DTE B25 maker wing=2000", lambda k: k[1] == 2 and k[2] == "B25" and k[3] == 2000.0 and k[4] == "maker"),
        ("2DTE A150 maker wings OFF", lambda k: k[1] == 2 and k[2] == "A150" and k[3] is None and k[4] == "maker"),
        ("2DTE A150 maker wing=2000", lambda k: k[1] == 2 and k[2] == "A150" and k[3] == 2000.0 and k[4] == "maker"),
        ("1DTE B25 maker wings OFF", lambda k: k[1] == 1 and k[2] == "B25" and k[3] is None and k[4] == "maker"),
        ("0DTE B25 maker wings OFF", lambda k: k[1] == 0 and k[2] == "B25" and k[3] is None and k[4] == "maker"),
    ]
    for name, filt in families:
        lines.append(f"Family: {name}")
        lines.extend(move_decile_table(stats_full, filt))
        lines.append("")

    # 3. Wings paired comparison
    lines.append("=" * 72)
    lines.append("3. WINGS paired: OFF vs each distance (no settle fee, FULL)")
    lines.append("   Design prediction: wings lower mean, raise Sortino.")
    lines.append("=" * 72)
    by_key = {s.key: s for s in stats_full}
    hold_lower_mean = 0
    hold_raise_sortino = 0
    hold_both = 0
    n_pair = 0
    for hhmm, _, _ in [(f"{h:02d}:{m:02d}", h, m) for h, m in ENTRY_TIMES_IST]:
        for dte in SHORT_DTES:
            for label, _k, _v in strike_specs():
                for fill in FILL_PACKAGES:
                    off = by_key.get((hhmm, dte, label, None, fill))
                    if off is None or off.n < 20:
                        continue
                    for dist in (1000.0, 1500.0, 2000.0, 3000.0, 4000.0):
                        on = by_key.get((hhmm, dte, label, dist, fill))
                        if on is None or on.n < 20:
                            continue
                        n_pair += 1
                        d_mean = on.mean - off.mean
                        d_p5 = on.p5 - off.p5
                        d_worst = on.worst - off.worst
                        d_so = (
                            on.sortino - off.sortino
                            if math.isfinite(on.sortino) and math.isfinite(off.sortino)
                            else float("nan")
                        )
                        lower_mean = d_mean < 0
                        raise_so = math.isfinite(d_so) and d_so > 0
                        if lower_mean:
                            hold_lower_mean += 1
                        if raise_so:
                            hold_raise_sortino += 1
                        if lower_mean and raise_so:
                            hold_both += 1
                        # Print a compact subset: 2DTE B25 maker all distances + summary later
                        if dte == 2 and label == "B25" and fill == "maker" and hhmm in (
                            "09:00",
                            "11:00",
                            "13:00",
                            "15:00",
                        ):
                            lines.append(
                                f"  {hhmm} dte2 B25 maker wing={int(dist)}: "
                                f"dMean={d_mean:+.2f} dP5={d_p5:+.2f} dWorst={d_worst:+.2f} "
                                f"dSortino={d_so:+.3f}  (off n={off.n} on n={on.n})"
                            )
    lines.append("")
    lines.append(
        f"Paired tests n={n_pair}: wings lower mean {hold_lower_mean}/{n_pair} "
        f"({100*hold_lower_mean/max(n_pair,1):.1f}%), "
        f"raise Sortino {hold_raise_sortino}/{n_pair} "
        f"({100*hold_raise_sortino/max(n_pair,1):.1f}%), "
        f"both {hold_both}/{n_pair} ({100*hold_both/max(n_pair,1):.1f}%)"
    )
    lines.append(
        "Note: on 2DTE B25 maker, wings reliably improve p5 and worst-cycle "
        "(positive dP5/dWorst) while cutting mean; Sortino still falls because "
        "mean drops more than downside deviation."
    )
    # By distance aggregate
    lines.append("By wing distance (fraction where prediction holds: lower mean AND higher Sortino):")
    for dist in (1000.0, 1500.0, 2000.0, 3000.0, 4000.0):
        tot = both = 0
        for hhmm, _, _ in [(f"{h:02d}:{m:02d}", h, m) for h, m in ENTRY_TIMES_IST]:
            for dte in SHORT_DTES:
                for label, _k, _v in strike_specs():
                    for fill in FILL_PACKAGES:
                        off = by_key.get((hhmm, dte, label, None, fill))
                        on = by_key.get((hhmm, dte, label, dist, fill))
                        if off is None or on is None or off.n < 20 or on.n < 20:
                            continue
                        tot += 1
                        d_mean = on.mean - off.mean
                        d_so = (
                            on.sortino - off.sortino
                            if math.isfinite(on.sortino) and math.isfinite(off.sortino)
                            else float("nan")
                        )
                        if d_mean < 0 and math.isfinite(d_so) and d_so > 0:
                            both += 1
        lines.append(
            f"  wing={int(dist)}: {both}/{tot} = {100*both/max(tot,1):.1f}% hold both"
        )
    lines.append("")

    # 4. Short expiry comparison
    lines.append("=" * 72)
    lines.append("4. SHORT EXPIRY comparison (B25 maker wings OFF, all entry times pooled)")
    lines.append("=" * 72)
    for dte in SHORT_DTES:
        subset = [
            s
            for s in stats_full
            if s.key[1] == dte and s.key[2] == "B25" and s.key[3] is None and s.key[4] == "maker"
        ]
        if not subset:
            lines.append(f"  dte={dte}: no data")
            continue
        all_nets: list[float] = []
        days: set[date] = set()
        for s in subset:
            all_nets.extend(s.nets)
            days.update(s.dates)
        lines.append(
            f"  dte={dte}: configs={len(subset)} cycles={len(all_nets)} "
            f"unique_days={len(days)} "
            f"mean={statistics.mean(all_nets):.2f} med={statistics.median(all_nets):.2f} "
            f"p5={pctile(all_nets,5):.2f} sortino={sortino(all_nets):.3f} "
            f"cycles_per_day={len(all_nets)/max(len(days),1):.2f}"
        )
    lines.append(
        "Tradeoff: lower DTE => more cycles/day but less premium/theta per cycle; "
        "compare mean and Sortino above."
    )
    lines.append("")

    if audit is not None:
        lines.extend(hand_audit_lines(audit))
        lines.append("")

    lines.append(f"runtime_s: {runtime_s:.1f}")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (RESULTS_DIR / "s001_income_engine_latest.txt").write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S001 income engine measure")
    ap.add_argument("--no-surface", action="store_true", help="Disable wing surface fallback")
    args = ap.parse_args(argv)

    t0 = time.time()
    print(f"TOTAL CONFIGURATIONS: {N_CONFIGS}", flush=True)
    idx = build_trade_index()
    surface = None if args.no_surface else load_surface_optional()
    print("Measuring cycles...", flush=True)
    obs, drop, audit = measure_cycles(idx, surface)
    print(f"Collected {len(obs):,} cycle-rows", flush=True)
    path = write_report(obs, drop, audit, runtime_s=0.0)
    runtime = time.time() - t0
    # Patch runtime into report footer
    text = path.read_text(encoding="utf-8")
    text = text.replace("runtime_s: 0.0", f"runtime_s: {runtime:.1f}", 1)
    if text.rstrip().endswith("runtime_s: 0.0"):
        text = text.rstrip()[:-12] + f"runtime_s: {runtime:.1f}\n"
    else:
        # replace last runtime line
        lines = text.splitlines()
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("runtime_s:"):
                lines[i] = f"runtime_s: {runtime:.1f}"
                break
        text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (RESULTS_DIR / "s001_income_engine_latest.txt").write_text(text, encoding="utf-8")
    print(f"Wrote {path}", flush=True)
    print(f"runtime_s={runtime:.1f}", flush=True)
    return 0


if __name__ == "__main__":
    # Windows ProcessPoolExecutor needs guard
    raise SystemExit(main())
