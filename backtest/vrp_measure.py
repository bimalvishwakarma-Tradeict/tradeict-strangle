#!/usr/bin/env python3
"""
VRP measurement - short 0DTE/1DTE straddle & strangle, held to expiry.

Sell at entry (real trade prints), hold to 12:00 UTC settlement, no management.
Isolates whether premium collected exceeds settlement payout.

Uses OptionsTradeStore shards from options_trades.py + 1m spot.
stdlib only.
"""

from __future__ import annotations

import argparse
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

# 1 BTC notional = 1000 Delta lots of contract_value 0.001
QTY_BTC = 1.0
FEE_RATE = 0.0001
PREMIUM_CAP = 0.035
GST = 1.18

ENTRY_TIMES_IST = ((9, 0), (11, 0), (13, 0), (15, 0))
WINDOWS_MIN = (1, 5, 15)
STRANGLE_OFFSETS = (200, 400, 600)
DTES = (0, 1)
FILL_SIDES = ("maker", "taker")  # maker=conservative sell, taker=optimistic
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260914

# Settlement fee: NOT established from Delta docs in-repo (see S002 note).
# Report both. If charged to short, model as same trading-fee formula on
# each ITM leg's intrinsic at settlement.


@dataclass
class PrintFill:
    symbol: str
    price: float
    ts_utc: datetime
    buyer_role: str
    strike: float
    option_type: str


@dataclass
class Obs:
    expiry: date
    entry_date: date
    dte: int
    entry_hhmm: str
    structure: str
    window_min: int
    fill_side: str
    spot_entry: float
    spot_settle: float
    atm_strike: float
    call_strike: float
    put_strike: float
    call_fill: PrintFill
    put_fill: PrintFill
    premium: float
    payoff: float
    gross: float
    entry_fees: float
    settle_fees: float
    net_no_settle: float
    net_with_settle: float
    month_key: str


def option_fee(premium: float, index: float, qty_btc: float = QTY_BTC) -> float:
    """fee = min(index*qtyBTC*0.0001, premium*qtyBTC*0.035) * 1.18"""
    if premium <= 0 or index <= 0 or qty_btc <= 0:
        return 0.0
    base = min(index * qty_btc * FEE_RATE, premium * qty_btc * PREMIUM_CAP)
    return base * GST


def ist_to_utc(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST).astimezone(UTC)


def format_symbol(opt: str, strike: float, exp: date) -> str:
    return f"{opt}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def nearest_strike(strikes: set[float], target: float) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - target), k))


def call_payoff(spot: float, strike: float) -> float:
    return max(spot - strike, 0.0)


def put_payoff(spot: float, strike: float) -> float:
    return max(strike - spot, 0.0)


def structure_payoff(
    structure: str, spot: float, call_k: float, put_k: float
) -> float:
    return call_payoff(spot, call_k) + put_payoff(spot, put_k)


def load_spot() -> tuple[list[int], list[float], dict[int, float]]:
    times, closes = ot.load_spot_1m()
    by_ts = {t: c for t, c in zip(times, closes)}
    return times, closes, by_ts


def spot_at_exact_or_before(
    times: list[int], closes: list[float], ts_unix: int
) -> float | None:
    return ot.spot_at(times, closes, ts_unix)


def settle_spot_1200_utc(
    times: list[int], closes: list[float], exp: date
) -> float | None:
    """Settlement = 12:00 UTC candle close on expiry date (NOT 17:30)."""
    ts = int(datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp())
    # Prefer exact 12:00 bar if present
    i = bisect_left_times(times, ts)
    if i < len(times) and times[i] == ts:
        return closes[i]
    return spot_at_exact_or_before(times, closes, ts)


def bisect_left_times(times: list[int], ts: int) -> int:
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# In-memory trade index (filtered to entry hours) built from shards
# ---------------------------------------------------------------------------


@dataclass
class TradeIndex:
    strikes_by_expiry: dict[date, set[float]] = field(default_factory=lambda: defaultdict(set))
    # symbol -> sorted list of (ts, price, role)
    by_symbol: dict[str, list[tuple[float, float, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    expiries: set[date] = field(default_factory=set)


def build_trade_index(cache_dir: Path = CACHE_DIR) -> TradeIndex:
    """
    Load shards. Keep ALL strikes per expiry.
    Keep prints whose UTC hour is 2..11 (covers 09:00-15:00 IST +/-15m).
    """
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
                hour = datetime.fromtimestamp(float(ts), tz=UTC).hour
                if 2 <= hour <= 11:
                    role_s = "maker" if int(role) == 0 else "taker"
                    idx.by_symbol[symbol].append(
                        (float(ts), float(price), role_s)
                    )
            print(f"  scanned {n:,} rows", flush=True)
        finally:
            conn.close()
    # sort each symbol
    for sym in idx.by_symbol:
        idx.by_symbol[sym].sort(key=lambda x: x[0])
    print(
        f"Index ready: {len(idx.expiries)} expiries, "
        f"{len(idx.by_symbol)} symbols with entry-hour prints",
        flush=True,
    )
    return idx


def nearest_print(
    idx: TradeIndex,
    symbol: str,
    when: datetime,
    window_sec: float,
    fill_side: str,
) -> PrintFill | None:
    series = idx.by_symbol.get(symbol)
    if not series:
        return None
    target = when.timestamp()
    # binary search
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid
    candidates: list[tuple[float, float, str]] = []
    for j in (lo - 1, lo, lo + 1):
        if 0 <= j < len(series):
            candidates.append(series[j])
    # also scan neighbors within window
    best: tuple[float, float, str] | None = None
    best_abs = None
    # expand around lo
    for j in range(max(0, lo - 50), min(len(series), lo + 50)):
        ts, px, role = series[j]
        if abs(ts - target) > window_sec:
            continue
        if role != fill_side:
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


def choose_legs(
    strikes: set[float],
    spot: float,
    structure: str,
) -> tuple[float, float, float] | None:
    """Return (atm, call_k, put_k) from traded strikes only."""
    atm = nearest_strike(strikes, spot)
    if atm is None:
        return None
    if structure == "ATM_straddle":
        return atm, atm, atm
    if structure.startswith("strangle_"):
        off = int(structure.split("_")[1])
        call_k = nearest_strike(strikes, atm + off)
        put_k = nearest_strike(strikes, atm - off)
        if call_k is None or put_k is None:
            return None
        # require call above atm-ish and put below; allow equal only if no choice
        if call_k < atm or put_k > atm:
            # still accept nearest - but reject if collapsed to same strike both sides
            if call_k == put_k:
                return None
        return atm, call_k, put_k
    raise ValueError(structure)


def bootstrap_mean_ci(
    values: list[float], n: int, seed: int
) -> tuple[float, float, float]:
    """Return (mean, lo95, hi95)."""
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


def measure_all(idx: TradeIndex) -> tuple[list[Obs], dict[str, Any], Obs | None]:
    times, closes, _ = load_spot()
    spot_lo, spot_hi = times[0], times[-1]
    drop_reasons: dict[str, int] = defaultdict(int)
    observations: list[Obs] = []
    audit_obs: Obs | None = None

    structures = ["ATM_straddle"] + [f"strangle_{o}" for o in STRANGLE_OFFSETS]
    expiries = sorted(idx.expiries)

    for exp in expiries:
        settle = settle_spot_1200_utc(times, closes, exp)
        if settle is None:
            drop_reasons["no_settle_spot"] += 1
            continue
        settle_ts = int(
            datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC).timestamp()
        )
        if settle_ts < spot_lo or settle_ts > spot_hi:
            drop_reasons["settle_outside_spot_range"] += 1
            continue

        strikes = idx.strikes_by_expiry.get(exp) or set()
        if not strikes:
            drop_reasons["no_strikes"] += 1
            continue

        for dte in DTES:
            entry_date = exp if dte == 0 else exp - timedelta(days=1)
            for hh, mm in ENTRY_TIMES_IST:
                entry_utc = ist_to_utc(entry_date, hh, mm)
                entry_ts = int(entry_utc.timestamp())
                if entry_ts < spot_lo or entry_ts > spot_hi:
                    drop_reasons["entry_outside_spot_range"] += 1
                    continue
                spot_e = spot_at_exact_or_before(times, closes, entry_ts)
                if spot_e is None:
                    drop_reasons["no_entry_spot"] += 1
                    continue

                for structure in structures:
                    legs = choose_legs(strikes, spot_e, structure)
                    if legs is None:
                        drop_reasons["strike_select_fail"] += 1
                        continue
                    atm, call_k, put_k = legs
                    call_sym = format_symbol("C", call_k, exp)
                    put_sym = format_symbol("P", put_k, exp)

                    for win in WINDOWS_MIN:
                        wsec = win * 60.0
                        for fill_side in FILL_SIDES:
                            cf = nearest_print(
                                idx, call_sym, entry_utc, wsec, fill_side
                            )
                            pf = nearest_print(
                                idx, put_sym, entry_utc, wsec, fill_side
                            )
                            if cf is None or pf is None:
                                drop_reasons[
                                    f"missing_print_{fill_side}_w{win}"
                                ] += 1
                                continue

                            premium = cf.price + pf.price
                            payoff = structure_payoff(
                                structure, settle, call_k, put_k
                            )
                            gross = premium - payoff
                            fee_c = option_fee(cf.price, spot_e)
                            fee_p = option_fee(pf.price, spot_e)
                            entry_fees = fee_c + fee_p
                            # settlement fees on ITM intrinsics (hypothetical)
                            settle_fee_c = option_fee(
                                call_payoff(settle, call_k), settle
                            )
                            settle_fee_p = option_fee(
                                put_payoff(settle, put_k), settle
                            )
                            settle_fees = settle_fee_c + settle_fee_p
                            net0 = gross - entry_fees
                            net1 = gross - entry_fees - settle_fees

                            obs = Obs(
                                expiry=exp,
                                entry_date=entry_date,
                                dte=dte,
                                entry_hhmm=f"{hh:02d}:{mm:02d}",
                                structure=structure,
                                window_min=win,
                                fill_side=fill_side,
                                spot_entry=spot_e,
                                spot_settle=settle,
                                atm_strike=atm,
                                call_strike=call_k,
                                put_strike=put_k,
                                call_fill=cf,
                                put_fill=pf,
                                premium=premium,
                                payoff=payoff,
                                gross=gross,
                                entry_fees=entry_fees,
                                settle_fees=settle_fees,
                                net_no_settle=net0,
                                net_with_settle=net1,
                                month_key=f"{exp.year:04d}-{exp.month:02d}",
                            )
                            observations.append(obs)

                            if (
                                audit_obs is None
                                and dte == 0
                                and structure == "ATM_straddle"
                                and win == 5
                                and fill_side == "maker"
                                and hh == 11
                            ):
                                audit_obs = obs

    meta = {
        "drop_reasons": dict(drop_reasons),
        "n_expiries": len(expiries),
        "n_obs": len(observations),
    }
    return observations, meta, audit_obs


def summarize_group(obs_list: list[Obs], *, use_settle_fee: bool) -> dict[str, Any]:
    if not obs_list:
        return {"n": 0}
    nets = [
        o.net_with_settle if use_settle_fee else o.net_no_settle for o in obs_list
    ]
    premiums = [o.premium for o in obs_list]
    payoffs = [o.payoff for o in obs_list]
    grosses = [o.gross for o in obs_list]
    fees = [
        (o.entry_fees + o.settle_fees) if use_settle_fee else o.entry_fees
        for o in obs_list
    ]
    mean_net, lo, hi = bootstrap_mean_ci(nets, BOOTSTRAP_N, BOOTSTRAP_SEED)
    worst = min(obs_list, key=lambda o: (
        o.net_with_settle if use_settle_fee else o.net_no_settle
    ))
    return {
        "n": len(obs_list),
        "prem_med": statistics.median(premiums),
        "prem_mean": statistics.mean(premiums),
        "pay_med": statistics.median(payoffs),
        "pay_mean": statistics.mean(payoffs),
        "gross_med": statistics.median(grosses),
        "gross_mean": statistics.mean(grosses),
        "fees_mean": statistics.mean(fees),
        "net_med": statistics.median(nets),
        "net_mean": mean_net,
        "win_rate": sum(1 for x in nets if x > 0) / len(nets),
        "p5": pctile(nets, 5),
        "p25": pctile(nets, 25),
        "p75": pctile(nets, 75),
        "p95": pctile(nets, 95),
        "ci_lo": lo,
        "ci_hi": hi,
        "ci_clears_zero": lo > 0,
        "worst_net": (
            worst.net_with_settle if use_settle_fee else worst.net_no_settle
        ),
        "worst_date": worst.expiry.isoformat(),
        "worst_structure": worst.structure,
    }


def fmt(v: Any, d: int = 2) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{d}f}"
    return str(v)


def pct(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{100.0 * v:.1f}%"


def write_report(
    observations: list[Obs],
    meta: dict[str, Any],
    audit: Obs | None,
    runtime_s: float,
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"vrp_measure_{stamp}.txt"
    lines: list[str] = []

    lines.append("=== VRP: short straddle/strangle held to expiry ===")
    lines.append(f"runtime_s: {runtime_s:.1f}")
    lines.append(f"bootstrap_n: {BOOTSTRAP_N}  seed: {BOOTSTRAP_SEED}")
    lines.append(f"qtyBTC: {QTY_BTC} (P&L and fees in USD per 1 BTC notional)")
    lines.append(
        "fee = min(index*qtyBTC*0.0001, premium*qtyBTC*0.035)*1.18  "
        "on each entry leg"
    )
    lines.append(
        "SETTLEMENT FEE: NOT ESTABLISHED. Delta India ITM settlement fee is "
        "unverified in-repo (S002 note). Results reported BOTH without and "
        "with a hypothetical settlement fee = same formula on each ITM "
        "leg's intrinsic. Headline table uses NO settlement fee; with-fee "
        "summary follows."
    )
    lines.append(
        "fill_side=maker: buyer was maker -> seller crossed (CONSERVATIVE market sell). "
        "fill_side=taker: buyer was taker -> seller was passive (OPTIMISTIC)."
    )
    lines.append(
        "Settlement spot = 12:00 UTC 1m close on expiry date (17:30 IST)."
    )
    lines.append("")

    # Coverage
    lines.append("=== COVERAGE / DROPS ===")
    lines.append(f"expiries_in_index: {meta['n_expiries']}")
    lines.append(f"observations_kept: {meta['n_obs']}")
    lines.append("drop_reasons (counts are per attempted slot, not unique days):")
    for k, v in sorted(meta["drop_reasons"].items(), key=lambda x: -x[1]):
        lines.append(f"  {k}: {v}")
    lines.append("")

    # Window sensitivity headline
    lines.append("=== WINDOW SENSITIVITY (ATM straddle, 0DTE, 11:00 IST, maker) ===")
    for win in WINDOWS_MIN:
        subset = [
            o
            for o in observations
            if o.structure == "ATM_straddle"
            and o.dte == 0
            and o.entry_hhmm == "11:00"
            and o.fill_side == "maker"
            and o.window_min == win
        ]
        s = summarize_group(subset, use_settle_fee=False)
        lines.append(
            f"  +/-{win}m: n={s.get('n',0)} mean_net={fmt(s.get('net_mean'))} "
            f"CI=[{fmt(s.get('ci_lo'))},{fmt(s.get('ci_hi'))}] "
            f"clears0={s.get('ci_clears_zero')}"
        )
    # Compare windows more broadly
    lines.append("Mean net by window (all structures, maker, no settle fee):")
    for win in WINDOWS_MIN:
        subset = [
            o
            for o in observations
            if o.window_min == win and o.fill_side == "maker"
        ]
        if subset:
            lines.append(
                f"  +/-{win}m: n={len(subset)} mean_net="
                f"{fmt(statistics.mean(o.net_no_settle for o in subset))}"
            )
    lines.append("")

    # Main table - one row per (structure, dte, entry, fill, window)
    # Primary headline: window=5 as default matching gate2, but report all windows
    rows: list[dict[str, Any]] = []
    for structure in ["ATM_straddle"] + [f"strangle_{o}" for o in STRANGLE_OFFSETS]:
        for dte in DTES:
            for hh, mm in ENTRY_TIMES_IST:
                hhmm = f"{hh:02d}:{mm:02d}"
                for fill_side in FILL_SIDES:
                    for win in WINDOWS_MIN:
                        subset = [
                            o
                            for o in observations
                            if o.structure == structure
                            and o.dte == dte
                            and o.entry_hhmm == hhmm
                            and o.fill_side == fill_side
                            and o.window_min == win
                        ]
                        # coverage % vs expiries that had spot
                        s = summarize_group(subset, use_settle_fee=False)
                        s2 = summarize_group(subset, use_settle_fee=True)
                        rows.append(
                            {
                                "structure": structure,
                                "dte": dte,
                                "entry": hhmm,
                                "fill": fill_side,
                                "window": win,
                                "cov_n": s.get("n", 0),
                                **{f"a_{k}": v for k, v in s.items()},
                                **{f"b_{k}": v for k, v in s2.items()},
                            }
                        )

    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r.get("a_net_mean") is None
            or (
                isinstance(r.get("a_net_mean"), float)
                and math.isnan(r["a_net_mean"])
            ),
            -(r["a_net_mean"] if isinstance(r.get("a_net_mean"), float) else -1e18),
        ),
    )

    lines.append(
        "=== HEADLINE TABLE (NO settlement fee) - sorted by mean net desc ==="
    )
    lines.append(
        "Mark [*] = bootstrap 95% CI lower bound on mean net > 0"
    )
    lines.append(
        "struct            DTE entry fill   win  n   premMed premMean "
        "payMed  grossMean feeMean netMed  netMean  win%  "
        "p5     p95    CIlo   CIhi  worstDay"
    )
    lines.append("-" * 140)
    for r in rows_sorted:
        if r["cov_n"] == 0:
            continue
        mark = "*" if r.get("a_ci_clears_zero") else " "
        lines.append(
            f"{mark}{r['structure']:<16} {r['dte']}  {r['entry']} "
            f"{r['fill']:<6} +/-{r['window']:<2} {r['cov_n']:>4}  "
            f"{fmt(r['a_prem_med']):>7} {fmt(r['a_prem_mean']):>8} "
            f"{fmt(r['a_pay_med']):>7} {fmt(r['a_gross_mean']):>9} "
            f"{fmt(r['a_fees_mean']):>7} {fmt(r['a_net_med']):>7} "
            f"{fmt(r['a_net_mean']):>8} {pct(r['a_win_rate']):>5} "
            f"{fmt(r['a_p5']):>7} {fmt(r['a_p95']):>7} "
            f"{fmt(r['a_ci_lo']):>7} {fmt(r['a_ci_hi']):>7} "
            f"{r['a_worst_date']}({fmt(r['a_worst_net'])})"
        )

    n_clear = sum(1 for r in rows_sorted if r.get("a_ci_clears_zero"))
    lines.append(f"Rows with CI lower > 0 (no settle fee): {n_clear}")
    lines.append("")

    lines.append("=== SAME TABLE WITH hypothetical settlement fee ===")
    rows2 = sorted(
        rows,
        key=lambda r: (
            r.get("b_net_mean") is None
            or (
                isinstance(r.get("b_net_mean"), float)
                and math.isnan(r["b_net_mean"])
            ),
            -(r["b_net_mean"] if isinstance(r.get("b_net_mean"), float) else -1e18),
        ),
    )
    lines.append(
        "struct            DTE entry fill   win  n   netMean  CIlo   CIhi  clears0"
    )
    for r in rows2:
        if r["cov_n"] == 0:
            continue
        mark = "*" if r.get("b_ci_clears_zero") else " "
        lines.append(
            f"{mark}{r['structure']:<16} {r['dte']}  {r['entry']} "
            f"{r['fill']:<6} +/-{r['window']:<2} {r['cov_n']:>4}  "
            f"{fmt(r['b_net_mean']):>8} {fmt(r['b_ci_lo']):>7} "
            f"{fmt(r['b_ci_hi']):>7}  {r.get('b_ci_clears_zero')}"
        )
    lines.append("")

    # Monthly stability for top structures
    lines.append("=== NET P&L BY MONTH (best candidates, window=5, maker, no settle) ===")
    # pick top 4 unique (structure,dte,entry) from window=5 maker by mean net
    cand = [
        r
        for r in rows_sorted
        if r["window"] == 5 and r["fill"] == "maker" and r["cov_n"] >= 30
    ][:6]
    for r in cand:
        subset = [
            o
            for o in observations
            if o.structure == r["structure"]
            and o.dte == r["dte"]
            and o.entry_hhmm == r["entry"]
            and o.fill_side == "maker"
            and o.window_min == 5
        ]
        by_m: dict[str, list[float]] = defaultdict(list)
        for o in subset:
            by_m[o.month_key].append(o.net_no_settle)
        lines.append(
            f"  {r['structure']} {r['dte']}DTE {r['entry']}:"
        )
        for mk in sorted(by_m.keys()):
            lines.append(
                f"    {mk}: n={len(by_m[mk])} mean={fmt(statistics.mean(by_m[mk]))} "
                f"med={fmt(statistics.median(by_m[mk]))}"
            )
    lines.append("")

    # 10 worst days overall (window=5 maker)
    lines.append("=== 10 WORST DAYS (window=5, maker, no settle fee) ===")
    pool = [
        o
        for o in observations
        if o.window_min == 5 and o.fill_side == "maker"
    ]
    worst10 = sorted(pool, key=lambda o: o.net_no_settle)[:10]
    for o in worst10:
        lines.append(
            f"  {o.expiry} {o.structure} {o.dte}DTE {o.entry_hhmm} "
            f"prem={fmt(o.premium)} pay={fmt(o.payoff)} gross={fmt(o.gross)} "
            f"net={fmt(o.net_no_settle)} spot_e={fmt(o.spot_entry)} "
            f"spot_s={fmt(o.spot_settle)} move={fmt(o.spot_settle - o.spot_entry)}"
        )
    lines.append("")

    # ATM straddle: premium vs |settlement move| deciles
    lines.append(
        "=== ATM STRADDLE: premium vs |settle-entry move| (0DTE, +/-5m, maker) ==="
    )
    atm_obs = [
        o
        for o in observations
        if o.structure == "ATM_straddle"
        and o.dte == 0
        and o.window_min == 5
        and o.fill_side == "maker"
    ]
    if atm_obs:
        moves = sorted(abs(o.spot_settle - o.spot_entry) for o in atm_obs)
        # decile edges
        for d in range(10):
            lo_i = int(d * len(moves) / 10)
            hi_i = int((d + 1) * len(moves) / 10) - 1
            lo_v = moves[lo_i]
            hi_v = moves[min(hi_i, len(moves) - 1)]
            bucket = [
                o
                for o in atm_obs
                if lo_v <= abs(o.spot_settle - o.spot_entry) <= hi_v
            ]
            if not bucket:
                continue
            lines.append(
                f"  decile {d+1} |move|~[{fmt(lo_v)},{fmt(hi_v)}]: n={len(bucket)} "
                f"avg_prem={fmt(statistics.mean(o.premium for o in bucket))} "
                f"avg_pay={fmt(statistics.mean(o.payoff for o in bucket))} "
                f"avg_net={fmt(statistics.mean(o.net_no_settle for o in bucket))}"
            )
    lines.append("")

    # Hand audit
    lines.append("=== HAND AUDIT - one 0DTE ATM straddle ===")
    if audit is None:
        lines.append("No audit observation found.")
    else:
        o = audit
        entry_utc = ist_to_utc(
            o.entry_date,
            int(o.entry_hhmm[:2]),
            int(o.entry_hhmm[3:]),
        )
        lines.append(f"expiry_date: {o.expiry}")
        lines.append(
            f"entry: {o.entry_date} {o.entry_hhmm} IST = {entry_utc.isoformat()} UTC"
        )
        lines.append(f"spot_entry: {o.spot_entry:.2f}")
        lines.append(f"chosen ATM strike: {o.atm_strike:.0f}")
        lines.append(
            f"CALL print: {o.call_fill.symbol} px={o.call_fill.price} "
            f"ts={o.call_fill.ts_utc.isoformat()} role={o.call_fill.buyer_role}"
        )
        lines.append(
            f"PUT  print: {o.put_fill.symbol} px={o.put_fill.price} "
            f"ts={o.put_fill.ts_utc.isoformat()} role={o.put_fill.buyer_role}"
        )
        lines.append(f"premium = {o.call_fill.price} + {o.put_fill.price} = {o.premium}")
        lines.append(
            f"settlement_spot: {o.spot_settle:.2f}  "
            f"(12:00 UTC candle on {o.expiry} - NOT 17:30)"
        )
        lines.append(
            f"payoff = max(S-K,0)+max(K-S,0) = {o.payoff:.2f}  "
            f"(straddle intrinsic = |S-K| = {abs(o.spot_settle - o.atm_strike):.2f})"
        )
        lines.append(f"gross P&L (short) = premium - payoff = {o.gross:.4f}")
        fc = option_fee(o.call_fill.price, o.spot_entry)
        fp = option_fee(o.put_fill.price, o.spot_entry)
        lines.append(
            f"entry fee CALL = min({o.spot_entry}*{QTY_BTC}*{FEE_RATE}, "
            f"{o.call_fill.price}*{QTY_BTC}*{PREMIUM_CAP})*{GST} = {fc:.6f}"
        )
        lines.append(
            f"entry fee PUT  = min({o.spot_entry}*{QTY_BTC}*{FEE_RATE}, "
            f"{o.put_fill.price}*{QTY_BTC}*{PREMIUM_CAP})*{GST} = {fp:.6f}"
        )
        lines.append(f"entry_fees_total = {o.entry_fees:.6f}")
        lines.append(
            f"settle_fees_hypothetical = {o.settle_fees:.6f} "
            f"(NOT established - shown for sensitivity)"
        )
        lines.append(f"net_no_settle_fee = {o.net_no_settle:.4f}")
        lines.append(f"net_with_settle_fee = {o.net_with_settle:.4f}")

    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="VRP hold-to-expiry measure").parse_args(
        argv
    )
    t0 = time.perf_counter()
    idx = build_trade_index()
    observations, meta, audit = measure_all(idx)
    runtime = time.perf_counter() - t0
    path = write_report(observations, meta, audit, runtime)
    # print condensed headline (ASCII-safe on Windows consoles)
    text = path.read_text(encoding="utf-8")
    cut = text.find("=== NET P&L BY MONTH")
    out = text[:cut] if cut > 0 else text
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(out.encode("ascii", errors="replace").decode("ascii"))
    print(f"report: {path}")
    print(f"runtime_s: {runtime:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
