#!/usr/bin/env python3
"""
P&L-relevant IV surface validation (price CHANGES, long-dated term path, basis,
structure-level errors). Does not refit the surface.

Usage:
  python backtest/iv_surface_pnl_validate.py
"""

from __future__ import annotations

import math
import statistics
import sys
import time
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

import iv_surface as iv  # noqa: E402
import options_trades as ot  # noqa: E402

UTC = timezone.utc
DAY_SEC = 86400.0
HORIZON_TOL = 2.5 * 3600.0  # ±2.5h around 1d / 2d
MAX_PRINTS_PER_SYM = 400
MAX_CHANGE_PAIRS = 80_000
MAX_LONG_DATED = 8_000
MAX_STRUCTURE = 25_000
SIGNAL_PCT = 3.0  # intended P&L signal size (% of premium)
# Held-out LEVEL baseline from prior validation report (median |err|%prem)
HELD_OUT_LEVEL_MED_PCT = 11.24
HELD_OUT_LEVEL_MED_USD = 13.75
MIN_PCP_NOTE = 20


def _pctile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    xs = sorted(vals)
    if len(xs) == 1:
        return xs[0]
    k = (p / 100.0) * (len(xs) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _summ(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "p90": _pctile(xs, 90),
    }


def _breakdown(
    rows: list[dict[str, Any]], err_key: str = "err", pct_key: str = "err_pct"
) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    abs_e = [abs(r[err_key]) for r in rows]
    abs_p = [abs(r[pct_key]) for r in rows]
    by_m: dict[str, list[float]] = defaultdict(list)
    by_d: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_m[r["m_bucket"]].append(abs(r[err_key]))
        by_d[r["d_bucket"]].append(abs(r[err_key]))
    return {
        "n": len(rows),
        "abs_usd": _summ(abs_e),
        "abs_pct_entry": _summ(abs_p),
        "by_moneyness_usd": {k: _summ(v) for k, v in sorted(by_m.items())},
        "by_dte_usd": {k: _summ(v) for k, v in sorted(by_d.items())},
    }


def _fmt_bd(bd: dict[str, Any], indent: str = "  ") -> list[str]:
    lines: list[str] = []
    if bd.get("n", 0) == 0:
        lines.append(f"{indent}n=0")
        return lines
    au, ap = bd["abs_usd"], bd["abs_pct_entry"]
    lines.append(
        f"{indent}n={bd['n']}  |err|USD mean={au['mean']:.2f} med={au['median']:.2f} "
        f"p90={au['p90']:.2f}"
    )
    lines.append(
        f"{indent}|err|%entry mean={ap['mean']:.2f} med={ap['median']:.2f} "
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


def collect_hourly_prints() -> dict[tuple[date, float, str], list[tuple[float, float]]]:
    """
    One print per UTC hour per (expiry, strike, opt). Caps per symbol.
    Returns lists sorted by ts.
    """
    # key -> hour -> (ts, px) keep last
    tmp: dict[tuple[date, float, str], dict[int, tuple[float, float]]] = defaultdict(
        dict
    )
    n = 0
    for tr in iv.iter_shard_trades():
        n += 1
        if n % 5_000_000 == 0:
            print(f"  scanned {n:,} trades...", flush=True)
        if tr.price < iv.MIN_PREMIUM:
            continue
        hour = int(tr.ts // 3600)
        key = (tr.expiry, tr.strike, tr.opt.upper()[0])
        cell = tmp[key]
        if len(cell) >= MAX_PRINTS_PER_SYM and hour not in cell:
            continue
        cell[hour] = (tr.ts, tr.price)
    out: dict[tuple[date, float, str], list[tuple[float, float]]] = {}
    for key, by_h in tmp.items():
        pts = sorted(by_h.values(), key=lambda x: x[0])
        if len(pts) >= 2:
            out[key] = pts
    print(f"Symbols with >=2 hourly prints: {len(out):,}", flush=True)
    return out


def _find_near(
    pts: list[tuple[float, float]], target: float, tol: float
) -> tuple[float, float] | None:
    """Nearest print to target within tol (pts sorted by ts)."""
    best = None
    best_d = None
    # linear from bisect-ish: walk — lists are short (<=400)
    for ts, px in pts:
        d = abs(ts - target)
        if d <= tol and (best_d is None or d < best_d):
            best_d = d
            best = (ts, px)
        if ts > target + tol:
            break
    return best


def build_change_pairs(
    surface: iv.IVSurface,
    prints: dict[tuple[date, float, str], list[tuple[float, float]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (change_rows_1d, change_rows_2d, hand_audit_candidate)."""
    rows_1: list[dict[str, Any]] = []
    rows_2: list[dict[str, Any]] = []
    audit: dict[str, Any] | None = None

    for (exp, strike, opt), pts in prints.items():
        if len(rows_1) >= MAX_CHANGE_PAIRS and len(rows_2) >= MAX_CHANGE_PAIRS:
            break
        for i, (t0, p0) in enumerate(pts):
            for horizon_days, bucket in ((1, rows_1), (2, rows_2)):
                if len(bucket) >= MAX_CHANGE_PAIRS:
                    continue
                hit = _find_near(pts[i + 1 :], t0 + horizon_days * DAY_SEC, HORIZON_TOL)
                if hit is None:
                    continue
                t1, p1 = hit
                m0 = surface.price(t0, strike, exp, opt)
                m1 = surface.price(t1, strike, exp, opt)
                if not (m0.supported and m1.supported):
                    continue
                if p0 <= 0:
                    continue
                actual_ch = p1 - p0
                model_ch = m1.price - m0.price
                err = model_ch - actual_ch
                level_err = m0.price - p0
                k = math.log(strike / m0.F) if m0.F > 0 and strike > 0 else 0.0
                T = float(m0.quality.get("T") or 0.0)
                row = {
                    "exp": exp,
                    "strike": strike,
                    "opt": opt,
                    "t0": t0,
                    "t1": t1,
                    "p0": p0,
                    "p1": p1,
                    "m0": m0.price,
                    "m1": m1.price,
                    "actual_ch": actual_ch,
                    "model_ch": model_ch,
                    "err": err,
                    "err_pct": (err / p0) * 100.0,
                    "level_err": level_err,
                    "level_err_pct": (level_err / p0) * 100.0,
                    "m_bucket": iv.moneyness_bucket(k),
                    "d_bucket": iv.dte_bucket(T),
                    "horizon_d": horizon_days,
                    "k": k,
                    "T": T,
                }
                bucket.append(row)
                if (
                    audit is None
                    and horizon_days == 1
                    and abs(k) <= 0.02
                    and iv.dte_bucket(T) in {"1DTE", "2-3DTE", "4-10DTE"}
                ):
                    audit = row
    return rows_1, rows_2, audit or {}


def validation_b_long_dated(
    surface: iv.IVSurface,
    prints: dict[tuple[date, float, str], list[tuple[float, float]]],
) -> dict[str, Any]:
    """
    For DTE>=15 near-ATM prints with a direct fit, compare direct vs force_term.
    """
    interp_vs_direct: list[float] = []
    extrap_vs_direct: list[float] = []
    interp_vs_trade: list[float] = []
    extrap_vs_trade: list[float] = []
    direct_vs_trade: list[float] = []
    n_tried = 0
    n_ok = 0
    skip_no_direct = 0
    skip_no_force = 0

    for (exp, strike, opt), pts in prints.items():
        if n_ok >= MAX_LONG_DATED:
            break
        for ts, px in pts:
            n_tried += 1
            q = surface.quality(ts, exp)
            if not q.get("supported"):
                continue
            T = float(q.get("T") or 0.0)
            if T * 365.25 < 15.0:
                continue
            if q.get("reason") != "direct_fit":
                skip_no_direct += 1
                continue
            F = float(q.get("F") or 0.0)
            if F <= 0:
                continue
            k = math.log(strike / F) if strike > 0 else 0.0
            if abs(k) > 0.03:
                continue
            direct = surface.price(ts, strike, exp, opt)
            forced = surface.price(ts, strike, exp, opt, force_term=True)
            if not direct.supported:
                continue
            if not forced.supported:
                skip_no_force += 1
                continue
            path = forced.quality.get("reason")
            if path not in {"term_interpolated", "term_extrapolated"}:
                # stored interpolated smile may still be returned if force failed path
                # label by whether upper neighbour existed
                path = forced.quality.get("reason") or "other"
            err_fd = forced.price - direct.price
            err_ft = forced.price - px
            err_dt = direct.price - px
            direct_vs_trade.append(abs(err_dt))
            n_ok += 1
            if path == "term_interpolated":
                interp_vs_direct.append(abs(err_fd))
                interp_vs_trade.append(abs(err_ft))
            elif path == "term_extrapolated":
                extrap_vs_direct.append(abs(err_fd))
                extrap_vs_trade.append(abs(err_ft))
            else:
                # Treat unknown forced path as extrap-like for reporting
                extrap_vs_direct.append(abs(err_fd))
                extrap_vs_trade.append(abs(err_ft))

    def pack(xs: list[float]) -> dict[str, Any]:
        return _summ(xs)

    return {
        "n_tried_prints": n_tried,
        "n_ok": n_ok,
        "skip_no_direct": skip_no_direct,
        "skip_no_force": skip_no_force,
        "direct_vs_trade": pack(direct_vs_trade),
        "interp_vs_direct": pack(interp_vs_direct),
        "interp_vs_trade": pack(interp_vs_trade),
        "extrap_vs_direct": pack(extrap_vs_direct),
        "extrap_vs_trade": pack(extrap_vs_trade),
    }


def validation_c_basis(stats: iv.BuildStats) -> list[str]:
    lines = [
        "VALIDATION C — BASIS AT HEDGE TENOR (F/spot - 1 from PCP)",
        "Full curve by calendar DTE (median basis). Long tenors = monthly hedge zone.",
    ]
    by = getattr(stats, "basis_by_dte", None) or {}
    if not by:
        lines.append(
            "No per-DTE PCP samples on cached BuildStats. "
            "Long tenors: when pairs < 2 the surface uses F=spot."
        )
        # still dump basis_stats if present on surface via stats attachment
        return lines

    keys = sorted(by.keys())
    lines.append(
        f"DTE range in sample: {keys[0]} .. {keys[-1]}  (n_dte_bins={len(keys)})"
    )
    lines.append(
        f"{'DTE':>5}  {'n_pairs':>8}  {'median_basis_%':>14}  {'mean_basis_%':>12}"
    )
    hedge_rows = []
    for d in keys:
        vs = by[d]
        if not vs:
            continue
        med = 100.0 * statistics.median(vs)
        mean = 100.0 * statistics.mean(vs)
        lines.append(f"{d:5d}  {len(vs):8d}  {med:14.4f}  {mean:12.4f}")
        if d >= 15:
            hedge_rows.append((d, len(vs), med))
    if hedge_rows:
        lines.append("")
        lines.append("Hedge-relevant (DTE>=15) summary:")
        for d, n, med in hedge_rows:
            note = ""
            if n < MIN_PCP_NOTE:
                note = (
                    "  [FEW PAIRS — basis noisy; surface falls back to "
                    "F=spot when <2 pairs in a bucket]"
                )
            lines.append(f"  DTE={d}: n={n} median_basis={med:.4f}%{note}")
        long_ok = [r for r in hedge_rows if r[1] >= 20]
        if long_ok:
            d, n, med = long_ok[-1]
            lines.append(
                f"At DTE={d} median basis {med:.4f}% on spot 77000 => "
                f"F-spot ≈ {77000 * med / 100.0:.1f} USD"
            )
    else:
        lines.append(
            "No DTE>=15 basis samples. Long tenors lack PCP pairs; "
            "surface uses F=spot when pairs are scarce."
        )
    return lines


def validation_d_structures(
    surface: iv.IVSurface,
    prints: dict[tuple[date, float, str], list[tuple[float, float]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    ATM straddles and ~25%-of-straddle-premium strangles: change errors over 1d.
    """
    # Index: (exp, opt, strike) already have prints
    # Group strikes by expiry
    by_exp: dict[date, dict[float, dict[str, list[tuple[float, float]]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for (exp, strike, opt), pts in prints.items():
        by_exp[exp][strike][opt] = pts

    straddles: list[dict[str, Any]] = []
    strangles: list[dict[str, Any]] = []

    for exp, strikes in by_exp.items():
        strike_list = sorted(strikes.keys())
        if len(strike_list) < 3:
            continue
        # sample entry times from ATM-ish calls
        for K in strike_list:
            if "C" not in strikes[K] or "P" not in strikes[K]:
                continue
            cpts = strikes[K]["C"]
            ppts = strikes[K]["P"]
            # walk call prints as t0 candidates
            for t0, c0 in cpts:
                if len(straddles) >= MAX_STRUCTURE and len(strangles) >= MAX_STRUCTURE:
                    break
                q = surface.quality(t0, exp)
                if not q.get("supported"):
                    continue
                F = float(q.get("F") or 0.0)
                T = float(q.get("T") or 0.0)
                if F <= 0 or abs(K / F - 1.0) > 0.015:
                    continue  # ATM only for straddle anchor
                p_hit = _find_near(ppts, t0, HORIZON_TOL)
                if p_hit is None:
                    continue
                _tp0, p0 = p_hit
                # t1 = t0 + 1d
                c1 = _find_near(cpts, t0 + DAY_SEC, HORIZON_TOL)
                p1 = _find_near(ppts, t0 + DAY_SEC, HORIZON_TOL)
                if c1 is None or p1 is None:
                    continue
                t1c, c1px = c1
                t1p, p1px = p1
                t1 = 0.5 * (t1c + t1p)

                mc0 = surface.price(t0, K, exp, "C")
                mp0 = surface.price(t0, K, exp, "P")
                mc1 = surface.price(t1, K, exp, "C")
                mp1 = surface.price(t1, K, exp, "P")
                if not all(x.supported for x in (mc0, mp0, mc1, mp1)):
                    continue
                entry = c0 + p0
                if entry <= 0:
                    continue
                act_ch = (c1px + p1px) - (c0 + p0)
                mod_ch = (mc1.price + mp1.price) - (mc0.price + mp0.price)
                err = mod_ch - act_ch
                # leg-level average abs for comparison
                leg_errs = [
                    abs((mc1.price - mc0.price) - (c1px - c0)),
                    abs((mp1.price - mp0.price) - (p1px - p0)),
                ]
                straddles.append(
                    {
                        "err": err,
                        "err_pct": (err / entry) * 100.0,
                        "m_bucket": "ATM",
                        "d_bucket": iv.dte_bucket(T),
                        "leg_mean_abs": statistics.mean(leg_errs),
                        "entry": entry,
                    }
                )

                # Strangle: OTM call / put with premium ~ 25% of ATM straddle entry each
                target = 0.25 * entry
                best_c = None
                best_p = None
                best_cd = None
                best_pd = None
                for Kc in strike_list:
                    if Kc <= K:
                        continue
                    if "C" not in strikes[Kc]:
                        continue
                    ch = _find_near(strikes[Kc]["C"], t0, HORIZON_TOL)
                    if ch is None:
                        continue
                    d = abs(ch[1] - target)
                    if best_cd is None or d < best_cd:
                        # within 40% relative band
                        if target > 0 and abs(ch[1] / target - 1.0) <= 0.40:
                            best_cd = d
                            best_c = (Kc, ch[0], ch[1])
                for Kp in strike_list:
                    if Kp >= K:
                        continue
                    if "P" not in strikes[Kp]:
                        continue
                    ph = _find_near(strikes[Kp]["P"], t0, HORIZON_TOL)
                    if ph is None:
                        continue
                    d = abs(ph[1] - target)
                    if best_pd is None or d < best_pd:
                        if target > 0 and abs(ph[1] / target - 1.0) <= 0.40:
                            best_pd = d
                            best_p = (Kp, ph[0], ph[1])
                if best_c is None or best_p is None:
                    continue
                Kc, _tc0, wc0 = best_c
                Kp, _tp0b, wp0 = best_p
                wc1 = _find_near(strikes[Kc]["C"], t0 + DAY_SEC, HORIZON_TOL)
                wp1 = _find_near(strikes[Kp]["P"], t0 + DAY_SEC, HORIZON_TOL)
                if wc1 is None or wp1 is None:
                    continue
                t1s = 0.5 * (wc1[0] + wp1[0])
                mwc0 = surface.price(t0, Kc, exp, "C")
                mwp0 = surface.price(t0, Kp, exp, "P")
                mwc1 = surface.price(t1s, Kc, exp, "C")
                mwp1 = surface.price(t1s, Kp, exp, "P")
                if not all(x.supported for x in (mwc0, mwp0, mwc1, mwp1)):
                    continue
                sentry = wc0 + wp0
                if sentry <= 0:
                    continue
                act = (wc1[1] + wp1[1]) - sentry
                mod = (mwc1.price + mwp1.price) - (mwc0.price + mwp0.price)
                err_s = mod - act
                leg_errs_s = [
                    abs((mwc1.price - mwc0.price) - (wc1[1] - wc0)),
                    abs((mwp1.price - mwp0.price) - (wp1[1] - wp0)),
                ]
                strangles.append(
                    {
                        "err": err_s,
                        "err_pct": (err_s / sentry) * 100.0,
                        "m_bucket": "OTM_strangle",
                        "d_bucket": iv.dte_bucket(T),
                        "leg_mean_abs": statistics.mean(leg_errs_s),
                        "entry": sentry,
                    }
                )

    def pack_struct(rows: list[dict[str, Any]]) -> dict[str, Any]:
        bd = _breakdown(rows)
        if rows:
            bd["leg_mean_abs_usd"] = _summ([r["leg_mean_abs"] for r in rows])
            bd["note"] = (
                "structure |err| vs mean of per-leg |change err| "
                f"(leg med={statistics.median([r['leg_mean_abs'] for r in rows]):.2f})"
            )
        return bd

    return pack_struct(straddles), pack_struct(strangles)


def write_pnl_report(
    *,
    runtime_s: float,
    rows_1: list[dict[str, Any]],
    rows_2: list[dict[str, Any]],
    long_b: dict[str, Any],
    basis_lines: list[str],
    straddles: dict[str, Any],
    strangles: dict[str, Any],
    audit: dict[str, Any],
) -> Path:
    iv.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=iv.IST).strftime("%Y%m%d_%H%M%S")
    path = iv.RESULTS_DIR / f"iv_surface_pnl_validation_{stamp}.txt"
    lines: list[str] = []
    lines.append("=== IV SURFACE P&L VALIDATION ===")
    lines.append(f"runtime_s: {runtime_s:.1f}")
    lines.append(
        "Question: is change-error small enough to measure a ~3% of-premium "
        "P&L signal over 1-2 day windows?"
    )
    lines.append("")

    # --- A ---
    lines.append("=" * 72)
    lines.append("VALIDATION A — PRICE CHANGE ACCURACY (decisive)")
    lines.append("=" * 72)
    lines.append(
        f"Pairs: same (expiry,strike,opt) with prints at t0 and t1≈t0+1d/2d "
        f"(±{HORIZON_TOL/3600:.1f}h)."
    )
    lines.append(
        f"Held-out LEVEL baseline (prior report): median |err|%prem="
        f"{HELD_OUT_LEVEL_MED_PCT:.2f}%  median |err|USD={HELD_OUT_LEVEL_MED_USD:.2f}"
    )
    lines.append("")
    for label, rows in (("1-day horizon", rows_1), ("2-day horizon", rows_2)):
        lines.append(f"--- {label} ---")
        ch = _breakdown(rows)
        # same-sample level error at t0
        level_rows = [
            {
                "err": r["level_err"],
                "err_pct": r["level_err_pct"],
                "m_bucket": r["m_bucket"],
                "d_bucket": r["d_bucket"],
            }
            for r in rows
        ]
        lv = _breakdown(level_rows)
        lines.append("CHANGE error (model_ch - actual_ch):")
        lines.extend(_fmt_bd(ch))
        lines.append("LEVEL error at t0 on the SAME triples (model - trade):")
        lines.extend(_fmt_bd(lv))
        if ch.get("n") and lv.get("n"):
            ratio_pct = ch["abs_pct_entry"]["median"] / max(
                lv["abs_pct_entry"]["median"], 1e-9
            )
            ratio_held = ch["abs_pct_entry"]["median"] / HELD_OUT_LEVEL_MED_PCT
            lines.append(
                f"  RATIO change_med% / same_sample_level_med% = {ratio_pct:.3f}"
            )
            lines.append(
                f"  RATIO change_med% / held_out_level_med% = {ratio_held:.3f}"
            )
            lines.append(
                f"  Headline: change median |err|%entry="
                f"{ch['abs_pct_entry']['median']:.2f}% vs level "
                f"{lv['abs_pct_entry']['median']:.2f}% "
                f"(held-out level {HELD_OUT_LEVEL_MED_PCT:.2f}%)"
            )
        lines.append("")

    # --- B ---
    lines.append("=" * 72)
    lines.append("VALIDATION B — LONG-DATED (DTE>=15): DIRECT vs FORCE TERM")
    lines.append("=" * 72)
    lines.append(
        "Near-ATM prints where a direct smile fit exists; force_term hides that "
        "expiry and rebuilds from neighbours."
    )
    lines.append(f"n_ok={long_b.get('n_ok')}  skip_no_force={long_b.get('skip_no_force')}")
    for key, label in (
        ("direct_vs_trade", "direct fit vs trade |err|USD"),
        ("interp_vs_direct", "INTERP path vs direct |err|USD"),
        ("interp_vs_trade", "INTERP path vs trade |err|USD"),
        ("extrap_vs_direct", "EXTRAP path vs direct |err|USD"),
        ("extrap_vs_trade", "EXTRAP path vs trade |err|USD"),
    ):
        s = long_b.get(key) or {"n": 0}
        if s.get("n", 0) == 0:
            lines.append(f"  {label}: n=0")
        else:
            lines.append(
                f"  {label}: n={s['n']} mean={s['mean']:.2f} med={s['median']:.2f} "
                f"p90={s['p90']:.2f}"
            )
    lines.append("")

    # --- C ---
    lines.append("=" * 72)
    lines.extend(basis_lines)
    lines.append("")

    # --- D ---
    lines.append("=" * 72)
    lines.append("VALIDATION D — STRUCTURE-LEVEL CHANGE (1-day)")
    lines.append("=" * 72)
    lines.append("ATM straddles (C+P same K near F):")
    lines.extend(_fmt_bd(straddles))
    if straddles.get("leg_mean_abs_usd"):
        lg = straddles["leg_mean_abs_usd"]
        lines.append(
            f"  per-leg |change err| mean-of-legs: med={lg['median']:.2f} "
            f"mean={lg['mean']:.2f}  ({straddles.get('note','')})"
        )
    lines.append("Strangles (~25% of ATM-straddle premium per wing):")
    lines.extend(_fmt_bd(strangles))
    if strangles.get("leg_mean_abs_usd"):
        lg = strangles["leg_mean_abs_usd"]
        lines.append(
            f"  per-leg |change err| mean-of-legs: med={lg['median']:.2f} "
            f"mean={lg['mean']:.2f}"
        )
    lines.append("")

    # Hand audit
    lines.append("=" * 72)
    lines.append("HAND AUDIT — one (symbol, t0, t1) triple")
    lines.append("=" * 72)
    if not audit:
        lines.append("No audit candidate found.")
    else:
        t0 = datetime.fromtimestamp(audit["t0"], tz=UTC)
        t1 = datetime.fromtimestamp(audit["t1"], tz=UTC)
        lines.append(
            f"symbol: exp={audit['exp']} K={audit['strike']:.0f} {audit['opt']}"
        )
        lines.append(f"t0={t0.isoformat()}  t1={t1.isoformat()}")
        lines.append(f"trade_px: t0={audit['p0']:.4f}  t1={audit['p1']:.4f}")
        lines.append(f"model_px: t0={audit['m0']:.4f}  t1={audit['m1']:.4f}")
        lines.append(
            f"actual_change={audit['actual_ch']:.4f}  "
            f"model_change={audit['model_ch']:.4f}  "
            f"err={audit['err']:.4f}  err%entry={audit['err_pct']:.2f}%"
        )
        lines.append(
            f"level_err_t0={audit['level_err']:.4f}  "
            f"({audit['level_err_pct']:.2f}% of entry)"
        )
    lines.append("")

    # Verdict
    lines.append("=" * 72)
    lines.append("VERDICT")
    lines.append("=" * 72)
    # Use 1-day change median % as primary
    ch1 = _breakdown(rows_1)
    med = ch1.get("abs_pct_entry", {}).get("median") if ch1.get("n") else None
    if med is None:
        verdict = (
            "NO — insufficient change pairs to judge. Surface cannot be cleared "
            "for a ~3% of-premium P&L measurement on this evidence."
        )
    else:
        # Cancellation real if change << level and change comparable or below signal
        ratio_held = med / HELD_OUT_LEVEL_MED_PCT
        if med <= SIGNAL_PCT and ratio_held < 0.6:
            verdict = (
                f"YES - 1-day median |change error| is {med:.2f}% of entry premium, "
                f"below the ~{SIGNAL_PCT:.0f}% signal and "
                f"{1.0/ratio_held:.1f}x smaller than the {HELD_OUT_LEVEL_MED_PCT:.2f}% "
                f"held-out level error (ratio={ratio_held:.3f}). Systematic smile error "
                f"cancels enough in differences for this measurement."
            )
        elif med <= SIGNAL_PCT * 1.5 and ratio_held < 0.75:
            verdict = (
                f"YES - 1-day median |change error| is {med:.2f}% of entry premium "
                f"(signal ~{SIGNAL_PCT:.0f}%; held-out level {HELD_OUT_LEVEL_MED_PCT:.2f}%; "
                f"ratio change/level={ratio_held:.3f}). Cancellation is real and the "
                f"surface is accurate enough to measure a ~3% P&L signal over 1-2 days."
            )
        else:
            verdict = (
                f"NO - 1-day median |change error| is {med:.2f}% of entry premium vs "
                f"~{SIGNAL_PCT:.0f}% signal and {HELD_OUT_LEVEL_MED_PCT:.2f}% held-out "
                f"level error (ratio={ratio_held:.3f}). Change error is not small enough "
                f"relative to the signal; this surface cannot support that P&L measurement."
            )
    lines.append(verdict)
    lines.append("")
    lines.append(f"runtime_s: {runtime_s:.1f}")

    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    (iv.RESULTS_DIR / "iv_surface_pnl_validation_latest.txt").write_text(
        text, encoding="utf-8"
    )
    return path


def main() -> int:
    t0 = time.time()
    print("Loading cached IV surface...", flush=True)
    loaded = iv.load_surface("full")
    if loaded is None:
        print("ERROR: no cached surface at", iv.cache_path("full"), flush=True)
        print("Run: python backtest/iv_surface.py --rebuild", flush=True)
        return 1
    surface, stats = loaded
    print(
        f"Loaded smiles={len(surface.smiles):,} buckets={len(surface.expiries_by_bucket):,}",
        flush=True,
    )

    print("Collecting hourly prints from shards...", flush=True)
    prints = collect_hourly_prints()

    print("Validation A: change pairs...", flush=True)
    rows_1, rows_2, audit = build_change_pairs(surface, prints)
    print(f"  1d pairs={len(rows_1):,}  2d pairs={len(rows_2):,}", flush=True)

    print("Validation B: long-dated direct vs force_term...", flush=True)
    long_b = validation_b_long_dated(surface, prints)
    print(f"  n_ok={long_b.get('n_ok')}", flush=True)

    print("Validation C: basis curve...", flush=True)
    basis_lines = validation_c_basis(stats)

    print("Validation D: structures...", flush=True)
    straddles, strangles = validation_d_structures(surface, prints)
    print(
        f"  straddles n={straddles.get('n',0)}  strangles n={strangles.get('n',0)}",
        flush=True,
    )

    runtime = time.time() - t0
    path = write_pnl_report(
        runtime_s=runtime,
        rows_1=rows_1,
        rows_2=rows_2,
        long_b=long_b,
        basis_lines=basis_lines,
        straddles=straddles,
        strangles=strangles,
        audit=audit,
    )
    print(f"Wrote {path}", flush=True)
    print(f"runtime_s={runtime:.1f}", flush=True)
    # print verdict line
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("YES") or line.startswith("NO"):
            print("VERDICT:", line, flush=True)
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
