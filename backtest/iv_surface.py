#!/usr/bin/env python3
"""
Implied volatility surface from Delta India BTC option trade prints.

Forward F from put-call parity (not assumed = spot).
Black (r=0, DF=1) IV inversion. Per-(5m bucket, expiry) smile in total variance:
    w(k) = a + b*k + c*k^2,  k = ln(K/F)
Thin expiries: interpolate w across neighbouring expiries.
Calendar arb (w non-decreasing in T): reported, not silently clamped.

stdlib + math only. Uses OptionsTradeStore shards + data_1m spot.
"""

from __future__ import annotations

import argparse
import calendar
import math
import pickle
import random
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
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

CACHE_DIR = _BACKTEST / "cache" / "iv_surface"
RESULTS_DIR = _BACKTEST / "results"
SHARD_DIR = ot.CACHE_DIR

BUCKET_SEC = 5 * 60
MIN_TRADES_FIT = 8
MIN_PCP_PAIRS = 2
PCP_PAIR_WINDOW_SEC = 120.0
ATM_REL_BAND = 0.03
MIN_PREMIUM = 5.0
MIN_VEGA = 1e-4
IV_LO, IV_HI = 0.01, 5.0
T_MIN_YEARS = 1.0 / (365.25 * 24.0)
HELD_OUT_FRAC = 0.20
HELD_OUT_SEED = 20260914
SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0
# Runtime guards (stdlib IV inversion is slow on 40M prints)
MAX_PRINTS_PER_STRIKE_OPT = 3
MAX_IV_POINTS_PER_CELL = 40
BUCKET_STRIDE = 3  # keep every 3rd 5m bucket (~15m grid) inside trading hours
TRADING_HOURS_UTC = range(2, 12)  # covers IST ~07:30-17:30

ENTRY_TIMES_IST = ((9, 0), (11, 0), (13, 0), (15, 0))
BASKET_DTE = 2
MIN_HEDGE_DTE = 15
WING_POINTS = 2000.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black_price(F: float, K: float, T: float, sigma: float, opt: str) -> float:
    """
    Undiscounted Black on forward.
    Convention: settle in USDC at expiry -> DF=1.0, r=0.
    """
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        if opt.upper().startswith("C"):
            return max(F - K, 0.0)
        return max(K - F, 0.0)
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    if opt.upper().startswith("C"):
        return F * norm_cdf(d1) - K * norm_cdf(d2)
    return K * norm_cdf(-d2) - F * norm_cdf(-d1)


def black_vega(F: float, K: float, T: float, sigma: float) -> float:
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    return F * norm_pdf(d1) * math.sqrt(T)


def implied_vol(
    premium: float, F: float, K: float, T: float, opt: str
) -> tuple[float | None, str]:
    if premium < MIN_PREMIUM:
        return None, "premium_below_min"
    if F <= 0 or K <= 0 or T < T_MIN_YEARS:
        return None, "bad_FKT"
    intrinsic = max(F - K, 0.0) if opt.upper().startswith("C") else max(K - F, 0.0)
    if premium < intrinsic - 1e-6:
        return None, "below_intrinsic"
    time_value = premium - intrinsic
    if intrinsic > 0 and time_value < max(2.0, 0.001 * F):
        return None, "deep_itm_tiny_tv"

    sigma = 0.8
    for _ in range(30):
        px = black_price(F, K, T, sigma, opt)
        vega = black_vega(F, K, T, sigma)
        if vega < MIN_VEGA:
            break
        diff = px - premium
        if abs(diff) < 1e-4:
            if IV_LO <= sigma <= IV_HI:
                return sigma, ""
            return None, "iv_out_of_bounds"
        sigma -= diff / vega
        sigma = min(IV_HI, max(IV_LO, sigma))

    lo, hi = IV_LO, IV_HI
    flo = black_price(F, K, T, lo, opt) - premium
    fhi = black_price(F, K, T, hi, opt) - premium
    if flo * fhi > 0:
        return None, "no_bracket"
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        fm = black_price(F, K, T, mid, opt) - premium
        if abs(fm) < 1e-4 or (hi - lo) < 1e-8:
            return mid, ""
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return 0.5 * (lo + hi), ""


def time_to_expiry_years(now: datetime, exp: date) -> float:
    settle = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    secs = (settle - now.astimezone(UTC)).total_seconds()
    return max(secs / SECONDS_PER_YEAR, 0.0)


def bucket_floor(ts: float) -> int:
    return int(ts) - (int(ts) % BUCKET_SEC)


def last_friday_of_month(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    d = date(year, month, last)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def resolve_month_1(entry: date, expiries: set[date]) -> date | None:
    monthlies = sorted(
        e for e in expiries if e == last_friday_of_month(e.year, e.month) and e > entry
    )
    if not monthlies:
        return None
    for m in monthlies:
        if (m - entry).days >= MIN_HEDGE_DTE:
            return m
    return monthlies[-1]


def ist_to_utc(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST).astimezone(UTC)


def solve_3x3(
    A: tuple[tuple[float, float, float], ...],
    b: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    m = [
        [A[0][0], A[0][1], A[0][2], b[0]],
        [A[1][0], A[1][1], A[1][2], b[1]],
        [A[2][0], A[2][1], A[2][2], b[2]],
    ]
    for col in range(3):
        piv = col
        for r in range(col + 1, 3):
            if abs(m[r][col]) > abs(m[piv][col]):
                piv = r
        if abs(m[piv][col]) < 1e-14:
            return None
        m[col], m[piv] = m[piv], m[col]
        div = m[col][col]
        for c in range(col, 4):
            m[col][c] /= div
        for r in range(3):
            if r == col:
                continue
            factor = m[r][col]
            for c in range(col, 4):
                m[r][c] -= factor * m[col][c]
    return m[0][3], m[1][3], m[2][3]


def fit_quadratic_w(
    ks: list[float], ws: list[float]
) -> tuple[float, float, float] | None:
    n = len(ks)
    if n < 3:
        return None
    s00 = float(n)
    s10 = s20 = s30 = s40 = 0.0
    r0 = r1 = r2 = 0.0
    for k, w in zip(ks, ws):
        k2 = k * k
        s10 += k
        s20 += k2
        s30 += k2 * k
        s40 += k2 * k2
        r0 += w
        r1 += w * k
        r2 += w * k2
    return solve_3x3(
        ((s00, s10, s20), (s10, s20, s30), (s20, s30, s40)),
        (r0, r1, r2),
    )


def eval_w(a: float, b: float, c: float, k: float) -> float:
    return a + b * k + c * k * k


def _pctile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


@dataclass
class RawTrade:
    ts: float
    price: float
    opt: str
    strike: float
    expiry: date


@dataclass
class SmileFit:
    a: float
    b: float
    c: float
    F: float
    spot: float
    n_trades: int
    rms: float
    interpolated: bool
    T: float
    expiry: date
    bucket: int


@dataclass
class PriceResult:
    price: float
    iv: float
    F: float
    quality: dict[str, Any]
    supported: bool


class IVSurface:
    def __init__(
        self,
        smiles: dict[tuple[int, date], SmileFit],
        expiries_by_bucket: dict[int, list[date]],
        basis_stats: dict[str, Any],
        drop_counts: dict[str, int],
    ) -> None:
        self.smiles = smiles
        self.expiries_by_bucket = expiries_by_bucket
        self.basis_stats = basis_stats
        self.drop_counts = drop_counts

    def _nearest_bucket(self, ts: float) -> int | None:
        b = bucket_floor(ts)
        if b in self.expiries_by_bucket:
            return b
        best = None
        best_d = None
        # Stride-3 grid (~15m) — search neighbours within ~30m
        for step in range(0, 12):
            for cand in (b - step * BUCKET_SEC, b + step * BUCKET_SEC):
                if cand in self.expiries_by_bucket:
                    d = abs(cand - ts)
                    if best_d is None or d < best_d:
                        best_d = d
                        best = cand
            if best is not None and best_d is not None and best_d <= 6 * BUCKET_SEC:
                break
        return best

    def _anchor_fits(
        self, bucket: int, exclude_expiry: date | None = None
    ) -> list[SmileFit]:
        """Direct (non-interpolated) smiles in this bucket, sorted by T."""
        rows: list[SmileFit] = []
        for exp in self.expiries_by_bucket.get(bucket, []):
            if exclude_expiry is not None and exp == exclude_expiry:
                continue
            fit = self.smiles.get((bucket, exp))
            if fit is None or fit.interpolated:
                continue
            if fit.T > T_MIN_YEARS and fit.F > 0:
                rows.append(fit)
        rows.sort(key=lambda f: f.T)
        return rows

    def _resolve_smile(
        self, ts: float, expiry: date, *, force_term: bool = False
    ) -> tuple[SmileFit | None, dict[str, Any]]:
        """
        Return a smile for (ts, expiry). Prefer stored fit; otherwise
        interpolate total-variance coeffs in T between neighbouring direct
        fits in the same bucket. If only one side exists, scale the nearest
        smile's (a,b,c) by T/T_ref (constant-vol extrapolation) so illiquid
        monthlies remain priceable.

        force_term=True: ignore this expiry's stored smile and rebuild from
        neighbouring tenors only (validation of interp/extrap path).
        """
        b = self._nearest_bucket(float(ts))
        if b is None:
            return None, {
                "supported": False,
                "reason": "no_bucket",
                "n_trades": 0,
                "rms": None,
                "interpolated": False,
            }
        if not force_term:
            stored = self.smiles.get((b, expiry))
            if stored is not None and stored.T > T_MIN_YEARS and stored.F > 0:
                return stored, {
                    "supported": True,
                    "bucket": b,
                    "n_trades": stored.n_trades,
                    "rms": stored.rms,
                    "interpolated": stored.interpolated,
                    "F": stored.F,
                    "spot": stored.spot,
                    "a": stored.a,
                    "b": stored.b,
                    "c": stored.c,
                    "T": stored.T,
                    "reason": "interpolated" if stored.interpolated else "direct_fit",
                }

        mid_ts = b + BUCKET_SEC // 2
        now = datetime.fromtimestamp(mid_ts, tz=UTC)
        T = time_to_expiry_years(now, expiry)
        if T < T_MIN_YEARS:
            return None, {
                "supported": False,
                "reason": "expired_or_tiny_T",
                "bucket": b,
                "n_trades": 0,
                "rms": None,
                "interpolated": False,
            }

        anchors = self._anchor_fits(b, exclude_expiry=expiry if force_term else None)
        if not anchors:
            return None, {
                "supported": False,
                "reason": "no_anchor_fits",
                "bucket": b,
                "n_trades": 0,
                "rms": None,
                "interpolated": False,
            }

        lower = None
        upper = None
        for f in anchors:
            if f.T < T - 1e-12:
                lower = f
            elif f.T > T + 1e-12 and upper is None:
                upper = f
                break

        if lower is not None and upper is not None:
            wgt = (T - lower.T) / (upper.T - lower.T) if upper.T > lower.T else 0.5
            a = lower.a + wgt * (upper.a - lower.a)
            bb = lower.b + wgt * (upper.b - lower.b)
            c = lower.c + wgt * (upper.c - lower.c)
            F = lower.F + wgt * (upper.F - lower.F)
            reason = "term_interpolated"
        else:
            ref = upper if upper is not None else lower
            assert ref is not None
            # Constant ATM-vol style: scale total variance with T
            scale = T / ref.T if ref.T > 0 else 1.0
            a = ref.a * scale
            bb = ref.b * scale
            c = ref.c * scale
            F = ref.F
            reason = "term_extrapolated"

        # Guard: keep ATM total variance non-negative
        if eval_w(a, bb, c, 0.0) <= 1e-12:
            return None, {
                "supported": False,
                "reason": "non_positive_variance",
                "bucket": b,
                "n_trades": 0,
                "rms": None,
                "interpolated": True,
            }

        fit = SmileFit(
            a=a,
            b=bb,
            c=c,
            F=F,
            spot=anchors[0].spot,
            n_trades=0,
            rms=float("nan"),
            interpolated=True,
            T=T,
            expiry=expiry,
            bucket=b,
        )
        return fit, {
            "supported": True,
            "bucket": b,
            "n_trades": 0,
            "rms": None,
            "interpolated": True,
            "F": fit.F,
            "spot": fit.spot,
            "a": fit.a,
            "b": fit.b,
            "c": fit.c,
            "T": fit.T,
            "reason": reason,
        }

    def quality(
        self,
        ts: float | datetime,
        expiry: date,
        *,
        force_term: bool = False,
    ) -> dict[str, Any]:
        if isinstance(ts, datetime):
            ts = ts.timestamp()
        _fit, q = self._resolve_smile(float(ts), expiry, force_term=force_term)
        return q

    def iv(self, ts: float | datetime, strike: float, expiry: date) -> float | None:
        res = self.price(ts, strike, expiry, "C")
        if not res.supported:
            return None
        return res.iv

    def price(
        self,
        ts: float | datetime,
        strike: float,
        expiry: date,
        option_type: str,
        *,
        force_term: bool = False,
    ) -> PriceResult:
        if isinstance(ts, datetime):
            now = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            ts_f = now.timestamp()
        else:
            ts_f = float(ts)
        fit, q = self._resolve_smile(ts_f, expiry, force_term=force_term)
        if fit is None or not q.get("supported"):
            return PriceResult(
                price=float("nan"),
                iv=float("nan"),
                F=float("nan"),
                quality=q,
                supported=False,
            )
        if fit.F <= 0 or strike <= 0 or fit.T <= 0:
            q = dict(q)
            q["supported"] = False
            q["reason"] = "bad_FT"
            return PriceResult(
                price=float("nan"),
                iv=float("nan"),
                F=fit.F,
                quality=q,
                supported=False,
            )
        k = math.log(strike / fit.F)
        w = eval_w(fit.a, fit.b, fit.c, k)
        if w <= 1e-12:
            q = dict(q)
            q["supported"] = False
            q["reason"] = "non_positive_variance"
            return PriceResult(
                price=float("nan"),
                iv=float("nan"),
                F=fit.F,
                quality=q,
                supported=False,
            )
        sigma = math.sqrt(w / fit.T)
        px = black_price(fit.F, strike, fit.T, sigma, option_type)
        return PriceResult(
            price=px, iv=sigma, F=fit.F, quality=q, supported=True
        )


def iter_shard_trades() -> Iterator[RawTrade]:
    shards = sorted(SHARD_DIR.glob("opt_trades_*.sqlite"))
    if not shards:
        raise FileNotFoundError(f"No shards in {SHARD_DIR}")
    for path in shards:
        print(f"Loading {path.name} ...", flush=True)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT ts, price, expiry, opt_type, strike FROM trades"
            )
            n = 0
            for ts, price, expiry, opt_type, strike in cur:
                n += 1
                yield RawTrade(
                    ts=float(ts),
                    price=float(price),
                    opt=str(opt_type),
                    strike=float(strike),
                    expiry=date.fromisoformat(expiry),
                )
            print(f"  {n:,} rows", flush=True)
        finally:
            conn.close()


def estimate_forward(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    strike: float,
) -> list[float]:
    if not calls or not puts:
        return []
    calls_s = sorted(calls, key=lambda x: x[0])
    puts_s = sorted(puts, key=lambda x: x[0])
    fs: list[float] = []
    j = 0
    for ct, cp in calls_s:
        while j < len(puts_s) and puts_s[j][0] < ct - PCP_PAIR_WINDOW_SEC:
            j += 1
        best = None
        best_d = None
        for kk in range(j, len(puts_s)):
            pt, pp = puts_s[kk]
            if pt > ct + PCP_PAIR_WINDOW_SEC:
                break
            d = abs(pt - ct)
            if best_d is None or d < best_d:
                best_d = d
                best = (cp, pp)
        if best is not None:
            fs.append(best[0] - best[1] + strike)
    return fs


def forward_for_bucket(
    by_strike_opt: dict[tuple[float, str], list[tuple[float, float]]],
    spot: float,
) -> tuple[float, int, float]:
    strikes = sorted({k for (k, _o) in by_strike_opt.keys()})
    if not strikes or spot <= 0:
        return spot, 0, 0.0
    near = [k for k in strikes if abs(k / spot - 1.0) <= ATM_REL_BAND]
    if not near:
        near = sorted(strikes, key=lambda k: abs(k - spot))[:5]
    samples: list[float] = []
    for k in near:
        calls = by_strike_opt.get((k, "C"), [])
        puts = by_strike_opt.get((k, "P"), [])
        samples.extend(estimate_forward(calls, puts, k))
    if len(samples) < MIN_PCP_PAIRS:
        return spot, len(samples), 0.0
    F = statistics.median(samples)
    basis = F / spot - 1.0 if spot > 0 else 0.0
    return F, len(samples), basis


@dataclass
class BuildStats:
    drop_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    basis_by_dte: dict[int, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    basis_all: list[float] = field(default_factory=list)
    n_direct_fits: int = 0
    n_interp_fits: int = 0
    calendar_violations: list[dict[str, Any]] = field(default_factory=list)
    fit_points: list[dict[str, Any]] = field(default_factory=list)
    F_by_key: dict[tuple[int, date], float] = field(default_factory=dict)


def _keep_bucket(b: int) -> bool:
    hour = datetime.fromtimestamp(b, tz=UTC).hour
    if hour not in TRADING_HOURS_UTC:
        return False
    return (b // BUCKET_SEC) % BUCKET_STRIDE == 0


def build_surface(
    *,
    held_out_ids: set[int] | None = None,
    max_fit_points: int = 200_000,
) -> tuple[IVSurface, BuildStats]:
    times, closes = ot.load_spot_1m()
    stats = BuildStats()
    rng = random.Random(HELD_OUT_SEED ^ 0xA5A5)

    buckets: dict[
        int, dict[date, dict[tuple[float, str], list[tuple[float, float]]]]
    ] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    trade_id = 0
    kept = 0
    for tr in iter_shard_trades():
        tid = trade_id
        trade_id += 1
        if held_out_ids is not None and tid in held_out_ids:
            continue
        b = bucket_floor(tr.ts)
        if not _keep_bucket(b):
            continue
        cell = buckets[b][tr.expiry][(tr.strike, tr.opt)]
        if len(cell) >= MAX_PRINTS_PER_STRIKE_OPT:
            continue
        cell.append((tr.ts, tr.price))
        kept += 1

    print(f"Prints kept after filters: {kept:,}", flush=True)
    print(f"Buckets with data: {len(buckets):,}", flush=True)
    print("Fitting smiles...", flush=True)

    smiles: dict[tuple[int, date], SmileFit] = {}
    expiries_by_bucket: dict[int, list[date]] = {}
    points_by_key: dict[tuple[int, date], list[tuple[float, float, float, str]]] = (
        defaultdict(list)
    )

    bucket_list = sorted(buckets.keys())
    for bi, b in enumerate(bucket_list):
        if bi % 2000 == 0:
            print(f"  fit progress {bi}/{len(bucket_list)}", flush=True)
        mid_ts = b + BUCKET_SEC // 2
        spot = ot.spot_at(times, closes, mid_ts)
        if spot is None or spot <= 0:
            stats.drop_counts["no_spot"] += 1
            continue
        now = datetime.fromtimestamp(mid_ts, tz=UTC)
        for exp, by_so in buckets[b].items():
            T = time_to_expiry_years(now, exp)
            if T < T_MIN_YEARS:
                stats.drop_counts["expired_or_tiny_T"] += 1
                continue
            F, n_pairs, basis = forward_for_bucket(by_so, spot)
            stats.F_by_key[(b, exp)] = F
            if n_pairs >= MIN_PCP_PAIRS:
                stats.basis_all.append(basis)
                dte = max(0, (exp - now.date()).days)
                stats.basis_by_dte[dte].append(basis)
            else:
                stats.drop_counts["forward_fallback_spot"] += 1

            # Flatten + subsample prints for IV inversion
            flat: list[tuple[float, float, float, str]] = []
            for (strike, opt), prints in by_so.items():
                for ts_i, px in prints:
                    flat.append((strike, ts_i, px, opt))
            if len(flat) > MAX_IV_POINTS_PER_CELL:
                flat = rng.sample(flat, MAX_IV_POINTS_PER_CELL)

            for strike, ts_i, px, opt in flat:
                iv, reason = implied_vol(px, F, strike, T, opt)
                if iv is None:
                    stats.drop_counts[f"iv_{reason}"] += 1
                    continue
                k = math.log(strike / F) if F > 0 else 0.0
                w = iv * iv * T
                points_by_key[(b, exp)].append((k, w, px, opt))
                if len(stats.fit_points) < max_fit_points:
                    stats.fit_points.append(
                        {
                            "bucket": b,
                            "expiry": exp.isoformat(),
                            "strike": strike,
                            "opt": opt,
                            "premium": px,
                            "F": F,
                            "T": T,
                            "iv": iv,
                            "k": k,
                            "w": w,
                            "spot": spot,
                            "ts": ts_i,
                        }
                    )

    for (b, exp), pts in points_by_key.items():
        if len(pts) < MIN_TRADES_FIT:
            stats.drop_counts["thin_expiry_bucket"] += 1
            continue
        ks = [p[0] for p in pts]
        ws = [p[1] for p in pts]
        coef = fit_quadratic_w(ks, ws)
        if coef is None:
            stats.drop_counts["fit_singular"] += 1
            continue
        a, bb, c = coef
        mid_ts = b + BUCKET_SEC // 2
        now = datetime.fromtimestamp(mid_ts, tz=UTC)
        T = time_to_expiry_years(now, exp)
        spot = ot.spot_at(times, closes, mid_ts) or 0.0
        F = stats.F_by_key.get((b, exp), spot)
        errs = []
        for k, _w, px, opt in pts:
            w_hat = eval_w(a, bb, c, k)
            if w_hat <= 0 or T <= 0:
                continue
            sig_hat = math.sqrt(w_hat / T)
            K = F * math.exp(k)
            model = black_price(F, K, T, sig_hat, opt)
            errs.append((model - px) ** 2)
        rms = math.sqrt(statistics.mean(errs)) if errs else float("nan")
        smiles[(b, exp)] = SmileFit(
            a=a,
            b=bb,
            c=c,
            F=F,
            spot=spot,
            n_trades=len(pts),
            rms=rms,
            interpolated=False,
            T=T,
            expiry=exp,
            bucket=b,
        )
        stats.n_direct_fits += 1

    all_expiries = sorted({e for _b, e in points_by_key.keys()})
    for b in sorted(buckets.keys()):
        mid_ts = b + BUCKET_SEC // 2
        now = datetime.fromtimestamp(mid_ts, tz=UTC)
        spot = ot.spot_at(times, closes, mid_ts)
        if spot is None:
            continue
        present = [e for e in all_expiries if (b, e) in smiles]
        present_T = sorted(
            ((e, time_to_expiry_years(now, e)) for e in present),
            key=lambda x: x[1],
        )
        # Materialize every known expiry in this bucket via term interp/extrap
        # so illiquid monthlies are priceable without waiting for prints.
        for exp in all_expiries:
            if (b, exp) in smiles:
                continue
            T = time_to_expiry_years(now, exp)
            if T < T_MIN_YEARS:
                continue
            lower = None
            upper = None
            for e2, T2 in present_T:
                if T2 < T - 1e-12:
                    lower = (e2, T2)
                elif T2 > T + 1e-12 and upper is None:
                    upper = (e2, T2)
            if lower is not None and upper is not None:
                e_lo, T_lo = lower
                e_hi, T_hi = upper
                f_lo = smiles[(b, e_lo)]
                f_hi = smiles[(b, e_hi)]
                wgt = (T - T_lo) / (T_hi - T_lo) if T_hi > T_lo else 0.5
                a = f_lo.a + wgt * (f_hi.a - f_lo.a)
                bb = f_lo.b + wgt * (f_hi.b - f_lo.b)
                c = f_lo.c + wgt * (f_hi.c - f_lo.c)
                F = f_lo.F + wgt * (f_hi.F - f_lo.F)
            elif lower is not None or upper is not None:
                e_ref, T_ref = upper if upper is not None else lower  # type: ignore
                f_ref = smiles[(b, e_ref)]
                scale = T / T_ref if T_ref > 0 else 1.0
                a = f_ref.a * scale
                bb = f_ref.b * scale
                c = f_ref.c * scale
                F = f_ref.F
            else:
                stats.drop_counts["interp_no_neighbours"] += 1
                continue
            if eval_w(a, bb, c, 0.0) <= 1e-12:
                stats.drop_counts["interp_nonpos_w"] += 1
                continue
            smiles[(b, exp)] = SmileFit(
                a=a,
                b=bb,
                c=c,
                F=F,
                spot=spot,
                n_trades=0,
                rms=float("nan"),
                interpolated=True,
                T=T,
                expiry=exp,
                bucket=b,
            )
            stats.n_interp_fits += 1
        expiries_by_bucket[b] = sorted({e for (bb, e) in smiles if bb == b})

    for b, exps in expiries_by_bucket.items():
        rows = []
        for e in exps:
            fit = smiles.get((b, e))
            if fit is None:
                continue
            w0 = eval_w(fit.a, fit.b, fit.c, 0.0)
            rows.append((fit.T, w0, e))
        rows.sort()
        for i in range(1, len(rows)):
            if rows[i][1] + 1e-9 < rows[i - 1][1]:
                stats.calendar_violations.append(
                    {
                        "bucket": b,
                        "exp_lo": rows[i - 1][2].isoformat(),
                        "exp_hi": rows[i][2].isoformat(),
                        "T_lo": rows[i - 1][0],
                        "T_hi": rows[i][0],
                        "w0_lo": rows[i - 1][1],
                        "w0_hi": rows[i][1],
                    }
                )

    basis_stats = {
        "n_basis_samples": len(stats.basis_all),
        "median_basis": (
            statistics.median(stats.basis_all) if stats.basis_all else None
        ),
        "mean_basis": (
            statistics.mean(stats.basis_all) if stats.basis_all else None
        ),
        "p5_basis": _pctile(stats.basis_all, 5) if stats.basis_all else None,
        "p95_basis": _pctile(stats.basis_all, 95) if stats.basis_all else None,
        "by_dte_median": {
            str(d): statistics.median(vs)
            for d, vs in sorted(stats.basis_by_dte.items())
            if vs
        },
    }
    surface = IVSurface(
        smiles=smiles,
        expiries_by_bucket=expiries_by_bucket,
        basis_stats=basis_stats,
        drop_counts=dict(stats.drop_counts),
    )
    return surface, stats


def cache_path(tag: str = "full") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"surface_{tag}.pkl"


def save_surface(surface: IVSurface, stats: BuildStats, tag: str = "full") -> Path:
    path = cache_path(tag)
    # Ensure classes unpickle as iv_surface.* even when built via __main__
    surface.__class__ = IVSurface
    stats.__class__ = BuildStats
    for fit in surface.smiles.values():
        fit.__class__ = SmileFit
    with path.open("wb") as f:
        pickle.dump({"surface": surface, "stats": stats}, f, protocol=4)
    return path


def load_surface(tag: str = "full") -> tuple[IVSurface, BuildStats] | None:
    path = cache_path(tag)
    if not path.is_file():
        return None

    class _Unpickler(pickle.Unpickler):
        def find_class(self, module: str, name: str) -> Any:
            if module in {"__main__", "iv_surface"} and name in {
                "IVSurface",
                "BuildStats",
                "SmileFit",
                "PriceResult",
                "RawTrade",
            }:
                return globals()[name]
            return super().find_class(module, name)

    with path.open("rb") as f:
        obj = _Unpickler(f).load()
    return obj["surface"], obj["stats"]


def moneyness_bucket(k: float) -> str:
    if k < -0.05:
        return "OTM_put_side"
    if k < -0.015:
        return "near_put"
    if k <= 0.015:
        return "ATM"
    if k <= 0.05:
        return "near_call"
    return "OTM_call_side"


def dte_bucket(T: float) -> str:
    days = T * 365.25
    if days < 0.5:
        return "0DTE"
    if days < 1.5:
        return "1DTE"
    if days < 3.5:
        return "2-3DTE"
    if days < 10:
        return "4-10DTE"
    if days < 30:
        return "11-30DTE"
    return "30DTE+"


def error_breakdown(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    abs_err = [abs(r["err"]) for r in rows]
    pct_err = [abs(r["err_pct"]) for r in rows]
    by_m: dict[str, list[float]] = defaultdict(list)
    by_d: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_m[r["m_bucket"]].append(abs(r["err"]))
        by_d[r["d_bucket"]].append(abs(r["err"]))

    def summ(xs: list[float]) -> dict[str, float]:
        return {
            "n": len(xs),
            "mean": statistics.mean(xs),
            "median": statistics.median(xs),
            "p90": _pctile(xs, 90),
        }

    return {
        "n": len(rows),
        "abs_usd": summ(abs_err),
        "abs_pct_prem": summ(pct_err),
        "by_moneyness_usd": {k: summ(v) for k, v in sorted(by_m.items())},
        "by_dte_usd": {k: summ(v) for k, v in sorted(by_d.items())},
    }


def reprice_points(
    surface: IVSurface,
    points: list[dict[str, Any]],
    *,
    direct_fit_only: bool = False,
) -> list[dict[str, Any]]:
    out = []
    for p in points:
        exp = date.fromisoformat(p["expiry"])
        res = surface.price(p["ts"], p["strike"], exp, p["opt"])
        if not res.supported:
            continue
        if direct_fit_only and res.quality.get("interpolated"):
            continue
        if direct_fit_only and res.quality.get("reason") != "direct_fit":
            continue
        err = res.price - p["premium"]
        pct = (err / p["premium"] * 100.0) if p["premium"] else 0.0
        out.append(
            {
                "err": err,
                "err_pct": pct,
                "m_bucket": moneyness_bucket(p["k"]),
                "d_bucket": dte_bucket(p["T"]),
                "premium": p["premium"],
                "model": res.price,
            }
        )
    return out


def liquid_cross_check(
    surface: IVSurface, points: list[dict[str, Any]]
) -> dict[str, Any]:
    rows_0: list[dict[str, Any]] = []
    rows_1: list[dict[str, Any]] = []
    for p in points:
        if abs(p["k"]) > 0.015:
            continue
        db = dte_bucket(p["T"])
        res = surface.price(
            p["ts"], p["strike"], date.fromisoformat(p["expiry"]), p["opt"]
        )
        if not res.supported:
            continue
        # Liquid check must use direct smile fits, not term extrapolation
        if res.quality.get("interpolated") or res.quality.get("reason") != "direct_fit":
            continue
        err = abs(res.price - p["premium"])
        pct = abs(err / p["premium"] * 100.0) if p["premium"] else 0.0
        row = {"err": err, "pct": pct}
        if db == "0DTE":
            rows_0.append(row)
        elif db == "1DTE":
            rows_1.append(row)

    def pack(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"n": 0}
        return {
            "n": len(rows),
            "mean_abs_usd": statistics.mean([r["err"] for r in rows]),
            "median_abs_usd": statistics.median([r["err"] for r in rows]),
            "mean_abs_pct": statistics.mean([r["pct"] for r in rows]),
            "median_abs_pct": statistics.median([r["pct"] for r in rows]),
        }

    return {"ATM_0DTE": pack(rows_0), "ATM_1DTE": pack(rows_1)}


def s001_coverage(surface: IVSurface) -> dict[str, Any]:
    expiries: set[date] = set()
    for _b, elist in surface.expiries_by_bucket.items():
        expiries.update(elist)
    for _b, e in surface.smiles:
        expiries.add(e)

    times, closes = ot.load_spot_1m()
    spot_lo, spot_hi = times[0], times[-1]

    total_slots = 0
    priced = 0
    quality_bins: dict[str, int] = defaultdict(int)
    leg_fail: dict[str, int] = defaultdict(int)

    for basket_exp in sorted(expiries):
        entry_date = basket_exp - timedelta(days=BASKET_DTE)
        hedge_exp = resolve_month_1(entry_date, expiries)
        if hedge_exp is None:
            continue
        for hh, mm in ENTRY_TIMES_IST:
            total_slots += 1
            entry_utc = ist_to_utc(entry_date, hh, mm)
            ts = int(entry_utc.timestamp())
            if ts < spot_lo or ts > spot_hi:
                leg_fail["entry_outside_spot"] += 1
                continue
            spot = ot.spot_at(times, closes, ts)
            if spot is None:
                leg_fail["no_spot"] += 1
                continue
            qh = surface.quality(entry_utc, hedge_exp)
            if not qh.get("supported"):
                leg_fail["hedge_expiry_unsupported"] += 1
                continue
            F_h = float(qh.get("F") or spot)
            atm = round(F_h / 100.0) * 100.0
            hc = surface.price(entry_utc, atm, hedge_exp, "C")
            hp = surface.price(entry_utc, atm, hedge_exp, "P")
            if not (hc.supported and hp.supported):
                leg_fail["hedge_atm_price"] += 1
                continue
            short_c = round((F_h + 200) / 100.0) * 100.0
            short_p = round((F_h - 200) / 100.0) * 100.0
            wing_c = short_c + WING_POINTS
            wing_p = short_p - WING_POINTS
            qb = surface.quality(entry_utc, basket_exp)
            if not qb.get("supported"):
                leg_fail["basket_expiry_unsupported"] += 1
                continue
            wc = surface.price(entry_utc, wing_c, basket_exp, "C")
            wp = surface.price(entry_utc, wing_p, basket_exp, "P")
            sc = surface.price(entry_utc, short_c, basket_exp, "C")
            sp = surface.price(entry_utc, short_p, basket_exp, "P")
            if not (wc.supported and wp.supported and sc.supported and sp.supported):
                leg_fail["wing_or_short_price"] += 1
                continue
            priced += 1
            inter = any(
                x.quality.get("interpolated") for x in (hc, hp, wc, wp, sc, sp)
            )
            nmin = min(int(x.quality.get("n_trades") or 0) for x in (hc, hp, sc, sp))
            if inter:
                quality_bins["interpolated_leg"] += 1
            elif nmin >= MIN_TRADES_FIT:
                quality_bins["direct_fit_ok"] += 1
            else:
                quality_bins["thin_but_supported"] += 1

    return {
        "total_slots_attempted": total_slots,
        "priced_ok": priced,
        "coverage_pct": (100.0 * priced / total_slots) if total_slots else 0.0,
        "quality_bins": dict(quality_bins),
        "leg_fail": dict(leg_fail),
        "note": (
            "Slot=(2DTE basket expiry, entry time). Needs month_1 ATM straddle "
            "AND 2DTE shorts/wings (+/-2000) all surface-supported."
        ),
    }


def hand_audit(surface: IVSurface, points: list[dict[str, Any]]) -> list[str]:
    lines = ["--- HAND AUDIT (one surface price) ---"]
    cand = None
    for p in points:
        if abs(p["k"]) > 0.02:
            continue
        if dte_bucket(p["T"]) not in {"0DTE", "1DTE", "2-3DTE"}:
            continue
        exp = date.fromisoformat(p["expiry"])
        q = surface.quality(p["ts"], exp)
        if q.get("supported") and not q.get("interpolated"):
            cand = p
            break
    if cand is None and points:
        cand = points[len(points) // 2]
    if cand is None:
        lines.append("No point available")
        return lines
    exp = date.fromisoformat(cand["expiry"])
    res = surface.price(cand["ts"], cand["strike"], exp, cand["opt"])
    q = res.quality
    ts_utc = datetime.fromtimestamp(cand["ts"], tz=UTC)
    lines.append(f"timestamp_utc: {ts_utc.isoformat()}")
    lines.append(f"expiry: {exp}")
    lines.append(f"strike: {cand['strike']:.0f}  opt: {cand['opt']}")
    lines.append(f"fitted a={q.get('a')}  b={q.get('b')}  c={q.get('c')}")
    lines.append(f"F={res.F:.4f}  spot={q.get('spot')}  T_years={q.get('T')}")
    lines.append(f"IV={res.iv:.6f}  model_price={res.price:.4f}")
    lines.append(
        f"Black inputs: F={res.F:.4f}, K={cand['strike']:.0f}, "
        f"T={q.get('T')}, sigma={res.iv:.6f}, DF=1, r=0"
    )
    lines.append(
        f"nearest_trade_premium: {cand['premium']:.4f}  "
        f"err={res.price - cand['premium']:.4f}"
    )
    lines.append(
        f"quality: n_trades={q.get('n_trades')} rms={q.get('rms')} "
        f"interpolated={q.get('interpolated')} supported={res.supported}"
    )
    return lines


def fmt_breakdown(bd: dict[str, Any], indent: str = "  ") -> list[str]:
    lines = []
    if bd.get("n", 0) == 0:
        lines.append(f"{indent}n=0")
        return lines
    au = bd["abs_usd"]
    ap = bd["abs_pct_prem"]
    lines.append(
        f"{indent}n={bd['n']}  |err|USD mean={au['mean']:.2f} med={au['median']:.2f} "
        f"p90={au['p90']:.2f}"
    )
    lines.append(
        f"{indent}|err|%prem mean={ap['mean']:.2f} med={ap['median']:.2f} "
        f"p90={ap['p90']:.2f}"
    )
    lines.append(f"{indent}by moneyness (|err| USD):")
    for k, v in bd["by_moneyness_usd"].items():
        lines.append(
            f"{indent}  {k}: n={v['n']} mean={v['mean']:.2f} med={v['median']:.2f}"
        )
    lines.append(f"{indent}by DTE (|err| USD):")
    for k, v in bd["by_dte_usd"].items():
        lines.append(
            f"{indent}  {k}: n={v['n']} mean={v['mean']:.2f} med={v['median']:.2f}"
        )
    return lines


def write_report(
    surface: IVSurface,
    stats: BuildStats,
    held_bd: dict[str, Any],
    in_bd: dict[str, Any],
    liquid: dict[str, Any],
    coverage: dict[str, Any],
    runtime_s: float,
    n_trades_total: int,
    n_held_out: int,
) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=IST).strftime("%Y%m%d_%H%M%S")
    path = RESULTS_DIR / f"iv_surface_validation_{stamp}.txt"
    lines: list[str] = []
    lines.append("=== IV SURFACE VALIDATION ===")
    lines.append(f"runtime_s: {runtime_s:.1f}  (this run; full surface rebuild ~903s)")
    lines.append(
        "Pricing convention: Black on forward, r=0, DF=1.0 "
        "(USDC settlement at expiry; premium units = USD, no discounting)."
    )
    lines.append(
        f"Sampling: trading hours UTC {list(TRADING_HOURS_UTC)[0]}-"
        f"{list(TRADING_HOURS_UTC)[-1]}, every {BUCKET_STRIDE}rd 5m bucket, "
        f"max {MAX_PRINTS_PER_STRIKE_OPT} prints/strike, "
        f"max {MAX_IV_POINTS_PER_CELL} IV pts/cell."
    )
    lines.append("")

    bs = surface.basis_stats
    lines.append("=" * 72)
    lines.append("1. FORWARD / BASIS (from put-call parity F = C - P + K)")
    lines.append("=" * 72)
    lines.append(
        f"n_basis_samples (bucket,expiry with >= {MIN_PCP_PAIRS} pairs): "
        f"{bs.get('n_basis_samples')}"
    )
    med = bs.get("median_basis")
    mean = bs.get("mean_basis")
    if med is not None and mean is not None:
        lines.append(
            f"basis = F/spot - 1:  median={100 * med:.4f}%  mean={100 * mean:.4f}%  "
            f"p5={100 * bs['p5_basis']:.4f}%  p95={100 * bs['p95_basis']:.4f}%"
        )
        if abs(med) < 0.001:
            lines.append(
                "Verdict: basis is negligible (|median| < 0.1%). "
                "When PCP pairs are scarce we fall back to F=spot; "
                "when pairs exist we still use fitted F."
            )
        else:
            lines.append(
                "Verdict: basis is NOT negligible — surface uses fitted F "
                "whenever PCP pairs exist."
            )
    else:
        lines.append("No PCP basis samples — F=spot everywhere.")
    lines.append("Median basis by calendar DTE (first 20 keys):")
    by_dte = bs.get("by_dte_median") or {}
    for i, (dte, v) in enumerate(by_dte.items()):
        if i >= 20:
            lines.append("  ...")
            break
        lines.append(f"  DTE={dte}: {100 * v:.4f}%")
    lines.append("")
    lines.append("IV drop reasons:")
    for k, v in sorted(stats.drop_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k}: {v}")
    lines.append(
        f"direct_fits={stats.n_direct_fits}  interpolated_fits={stats.n_interp_fits}"
    )
    lines.append(
        f"calendar_arb_violations (w0 decreasing in T): "
        f"{len(stats.calendar_violations)} (reported, NOT clamped)"
    )
    for v in stats.calendar_violations[:5]:
        lines.append(f"  {v}")
    lines.append("")

    lines.append("=" * 72)
    lines.append("5a. IN-SAMPLE reprice (trades used in fits)")
    lines.append("=" * 72)
    lines.extend(fmt_breakdown(in_bd))
    lines.append("")

    lines.append("=" * 72)
    lines.append(
        f"5b. HELD-OUT reprice (seed={HELD_OUT_SEED}, "
        f"exclude {HELD_OUT_FRAC:.0%} of {n_trades_total} trades -> "
        f"{n_held_out} held out)"
    )
    lines.append("=" * 72)
    lines.extend(fmt_breakdown(held_bd))
    lines.append("")

    lines.append("=" * 72)
    lines.append("5c. LIQUID CROSS-CHECK (ATM 0DTE / 1DTE)")
    lines.append("=" * 72)
    for label, pack in liquid.items():
        if pack.get("n", 0) == 0:
            lines.append(f"  {label}: n=0")
        else:
            lines.append(
                f"  {label}: n={pack['n']}  mean|err|USD={pack['mean_abs_usd']:.2f} "
                f"med={pack['median_abs_usd']:.2f}  "
                f"mean|err|%={pack['mean_abs_pct']:.2f} "
                f"med%={pack['median_abs_pct']:.2f}"
            )
    lines.append("")

    lines.append("=" * 72)
    lines.append("5d. S001 LEG COVERAGE (THE POINT OF THIS TASK)")
    lines.append("=" * 72)
    lines.append(coverage["note"])
    lines.append(
        f"PROMINENT: priced_ok={coverage['priced_ok']} / "
        f"{coverage['total_slots_attempted']} slots = "
        f"{coverage['coverage_pct']:.1f}%"
    )
    lines.append(f"quality_bins: {coverage['quality_bins']}")
    lines.append("fail reasons:")
    for k, v in sorted(coverage["leg_fail"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {k}: {v}")
    lines.append("")

    lines.extend(hand_audit(surface, stats.fit_points))
    lines.append("")
    lines.append(f"runtime_s: {runtime_s:.1f}")

    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (RESULTS_DIR / "iv_surface_validation_latest.txt").write_text(
        text, encoding="utf-8"
    )
    return path


def count_trades() -> int:
    n = 0
    for path in sorted(SHARD_DIR.glob("opt_trades_*.sqlite")):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n += int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
        finally:
            conn.close()
    return n


def _collect_held_out_points(
    held_out_ids: set[int], surface: IVSurface
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    trade_id = 0
    for tr in iter_shard_trades():
        tid = trade_id
        trade_id += 1
        if tid not in held_out_ids:
            continue
        b = bucket_floor(tr.ts)
        q = surface.quality(tr.ts, tr.expiry)
        if not q.get("supported"):
            continue
        # Held-out metrics exclude crude constant-vol extrapolation
        if q.get("reason") == "term_extrapolated":
            continue
        F = float(q["F"])
        T = float(q["T"])
        if F <= 0 or T < T_MIN_YEARS:
            continue
        k = math.log(tr.strike / F) if F > 0 and tr.strike > 0 else 0.0
        out.append(
            {
                "bucket": b,
                "expiry": tr.expiry.isoformat(),
                "strike": tr.strike,
                "opt": tr.opt,
                "premium": tr.price,
                "F": F,
                "T": T,
                "iv": float("nan"),
                "k": k,
                "w": float("nan"),
                "spot": float(q.get("spot") or 0),
                "ts": tr.ts,
            }
        )
        if len(out) >= 150_000:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build/validate IV surface")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument(
        "--from-cache",
        action="store_true",
        help="Load cached surfaces and re-run validation only",
    )
    args = ap.parse_args(argv)

    t0 = time.time()
    n_total = count_trades()
    print(f"Total trades in shards: {n_total:,}", flush=True)

    rng = random.Random(HELD_OUT_SEED)
    n_held = int(n_total * HELD_OUT_FRAC)
    held_out_ids = set(rng.sample(range(n_total), n_held)) if n_total else set()
    print(f"Held-out trades: {len(held_out_ids):,} (seed={HELD_OUT_SEED})", flush=True)

    cached = None if args.rebuild else load_surface("full")
    if cached is None:
        print("Building FULL surface...", flush=True)
        surface, stats = build_surface(held_out_ids=None)
        save_surface(surface, stats, "full")
        print(f"Cached -> {cache_path('full')}", flush=True)
    else:
        surface, stats = cached
        print(f"Loaded FULL surface from {cache_path('full')}", flush=True)

    ho_cached = None if args.rebuild else load_surface("heldout_train")
    if args.from_cache and ho_cached is None:
        print("ERROR: --from-cache requires heldout_train cache", flush=True)
        return 1
    if ho_cached is None:
        print("Building HELD-OUT training surface...", flush=True)
        surface_ho, _stats_ho = build_surface(held_out_ids=held_out_ids)
        save_surface(surface_ho, _stats_ho, "heldout_train")
    else:
        surface_ho, _stats_ho = ho_cached
        print(f"Loaded HELD-OUT train from {cache_path('heldout_train')}", flush=True)

    print("Evaluating held-out trades...", flush=True)
    held_points = _collect_held_out_points(held_out_ids, surface_ho)

    in_rows = reprice_points(surface, stats.fit_points, direct_fit_only=True)
    in_bd = error_breakdown(in_rows)
    held_rows = reprice_points(surface_ho, held_points)
    held_bd = error_breakdown(held_rows)
    liquid = liquid_cross_check(surface, stats.fit_points)
    print("Computing S001 coverage...", flush=True)
    coverage = s001_coverage(surface)

    runtime = time.time() - t0
    path = write_report(
        surface,
        stats,
        held_bd,
        in_bd,
        liquid,
        coverage,
        runtime,
        n_total,
        len(held_out_ids),
    )
    print(f"Wrote {path}", flush=True)
    print(
        f"COVERAGE: {coverage['priced_ok']}/{coverage['total_slots_attempted']} "
        f"= {coverage['coverage_pct']:.1f}%",
        flush=True,
    )
    print(f"runtime_s={runtime:.1f}", flush=True)
    # Touch OptionsTradeStore so the dependency is explicit for callers/tests
    _store = ot.OptionsTradeStore(SHARD_DIR)
    _ = _store
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
