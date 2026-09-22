"""S008 arm statistics — full distribution + gate paired bootstrap."""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from backtest.harness.config import BOOTSTRAP_N, BOOTSTRAP_SEED
from backtest.harness.metrics import day_clustered_ci_daily, lots_at_risk_cap, max_drawdown
from backtest.strategies.s008_regime_gate.strategy import BasketResult, decide_side

CAPITAL_USD = 100.0
RISK_CAP_PCT = 3.0


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    i = (len(s) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (hi - i) + s[hi] * (i - lo)


def calendar_daily(
    rows: list[BasketResult], d0: date, d1: date
) -> list[float]:
    """One PnL per calendar day in range (0 if no trade)."""
    by_day: dict[date, float] = defaultdict(float)
    for b in rows:
        if not b.skipped:
            by_day[b.d] += b.net_pnl
    day_span = max(1, (d1 - d0).days + 1)
    return [by_day.get(d0 + timedelta(days=i), 0.0) for i in range(day_span)]


def traded_basket_pnls(rows: list[BasketResult]) -> list[float]:
    return [b.net_pnl for b in rows if not b.skipped]


def arm_stats(
    rows: list[BasketResult],
    d0: date,
    d1: date,
    *,
    bootstrap_n: int = BOOTSTRAP_N,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Full stats for one arm (gate × strike params)."""
    traded = [b for b in rows if not b.skipped]
    pnls = [b.net_pnl for b in traded]
    daily = calendar_daily(rows, d0, d1)
    mean_day = float(statistics.fmean(daily)) if daily else float("nan")
    _, ci_lo, ci_hi = day_clustered_ci_daily(daily, bootstrap_n, bootstrap_seed)

    worst_basket = min(pnls) if pnls else float("nan")
    max_loss = abs(worst_basket) if worst_basket == worst_basket else float("nan")
    # 3% worst-basket rule: capital sized so one worst basket = 3% of capital.
    # At qty=100 the worst basket dwarfs a $100 book, so capital is derived from
    # the worst basket rather than counting lots against a fixed $100.
    capital_req = (
        max_loss / (RISK_CAP_PCT / 100.0)
        if max_loss == max_loss and max_loss > 0
        else float("nan")
    )
    units = (
        lots_at_risk_cap(max_loss, CAPITAL_USD, RISK_CAP_PCT)
        if max_loss == max_loss and max_loss > 0
        else 0
    )
    ret_pct_day = (
        100.0 * mean_day / capital_req
        if capital_req == capital_req and capital_req > 0 and mean_day == mean_day
        else float("nan")
    )

    # Achieved premium as % of spot per leg — shows when the chain could not
    # offer anything near the requested target (e.g. only near-ATM strikes left).
    prem_pcts = [
        100.0 * m / b.spot_entry
        for b in traded
        for m in (b.call_mark, b.put_mark)
        if b.spot_entry > 0
    ]

    return {
        "n_traded": len(traded),
        "n_weekdays": len([b for b in rows if b.d.weekday() < 5]),
        "mean_leg_prem_pct": (
            float(statistics.fmean(prem_pcts)) if prem_pcts else float("nan")
        ),
        "max_leg_prem_pct": max(prem_pcts) if prem_pcts else float("nan"),
        "mean_basket": float(statistics.fmean(pnls)) if pnls else float("nan"),
        "median_basket": float(statistics.median(pnls)) if pnls else float("nan"),
        "win_pct": (100.0 * sum(1 for x in pnls if x > 0) / len(pnls)) if pnls else float("nan"),
        "worst_basket": worst_basket,
        "p5_basket": _percentile(pnls, 0.05),
        "max_dd": max_drawdown(daily) if daily else float("nan"),
        "capital_usd": capital_req,
        "risk_cap_pct": RISK_CAP_PCT,
        "units_3pct_worst": units,
        "mean_day": mean_day,
        "return_pct_day": ret_pct_day,
        "bootstrap_mean_day": mean_day,
        "ci_lo_day": ci_lo,
        "ci_hi_day": ci_hi,
        "ci_crosses_zero": bool(ci_lo <= 0 <= ci_hi) if ci_lo == ci_lo else True,
    }


def format_arm_stats(label: str, st: dict[str, Any]) -> list[str]:
    cross = "YES (CI includes 0)" if st.get("ci_crosses_zero") else "NO"
    return [
        f"--- {label} ---",
        f"  n={st['n_traded']} mean={st['mean_basket']:.4f} "
        f"median={st['median_basket']:.4f} win%={st['win_pct']:.1f}",
        f"  worst={st['worst_basket']:.4f} p5={st['p5_basket']:.4f} "
        f"maxDD={st['max_dd']:.4f}",
        f"  capital@3%worst={st['capital_usd']:.2f} "
        f"return%/day={st['return_pct_day']:.4f}",
        f"  mean/day={st['mean_day']:.4f} bootstrap95%=[{st['ci_lo_day']:.4f}, "
        f"{st['ci_hi_day']:.4f}] crosses_zero={cross}",
        f"  leg_premium%ofspot mean={st['mean_leg_prem_pct']:.4f} "
        f"max={st['max_leg_prem_pct']:.4f}",
    ]


@dataclass
class GateActDay:
    d: date
    net_none: float
    net_gated: float
    diff: float


def gate_actually_acted(b: BasketResult, gate: str, threshold: float) -> bool:
    """Gate changed behavior vs always-sell on an available day."""
    if gate == "none":
        return False
    side = decide_side(gate, b.sig, threshold)  # type: ignore[arg-type]
    if gate == "flat":
        return side == "flat"
    if gate == "switch":
        return side == "buy"
    return False


def paired_gate_bootstrap(
    act_days: list[GateActDay],
    *,
    bootstrap_n: int = BOOTSTRAP_N,
    bootstrap_seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float, float, int]:
    """Day-clustered bootstrap of mean(gated − none) on acted days."""
    if not act_days:
        return float("nan"), float("nan"), float("nan"), 0
    diffs = [d.diff for d in act_days]
    mean = float(statistics.fmean(diffs))
    by_day = {d.d: [d.diff] for d in act_days}
    days = sorted(by_day)
    rng = random.Random(bootstrap_seed)
    means: list[float] = []
    for _ in range(bootstrap_n):
        sample: list[float] = []
        for _ in days:
            day = days[rng.randrange(len(days))]
            sample.extend(by_day[day])
        if sample:
            means.append(float(statistics.fmean(sample)))
    if not means:
        return mean, float("nan"), float("nan"), len(diffs)
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))]
    hi = means[int(0.975 * (len(means) - 1))]
    return mean, float(lo), float(hi), len(diffs)


MIN_ACT_DAYS_FOR_CI = 10


def gate_paired_report(
    *,
    rows_none: list[BasketResult],
    rows_gated: list[BasketResult],
    gate: str,
    threshold: float,
    d0: date,
    d1: date,
    label: str = "",
) -> list[str]:
    """Paired stats on days where strikes were available AND gate acted."""
    by_none = {b.d: b for b in rows_none}
    by_gated = {b.d: b for b in rows_gated}
    act: list[GateActDay] = []
    for d in iter_weekdays_in_range(d0, d1):
        bn = by_none.get(d)
        bg = by_gated.get(d)
        if bn is None or bg is None:
            continue
        if not bn.strikes_available:
            continue
        if not gate_actually_acted(bn, gate, threshold):
            continue
        if bn.skipped:
            continue
        net_n = bn.net_pnl
        net_g = bg.net_pnl if not bg.skipped else 0.0
        act.append(GateActDay(d=d, net_none=net_n, net_gated=net_g, diff=net_g - net_n))

    mean_d, lo, hi, n = paired_gate_bootstrap(act)
    cross = "YES (CI includes 0)" if lo <= 0 <= hi else "NO"
    suffix = f" [{label}]" if label else ""
    lines = [
        f"===== GATE PAIRED ({gate} vs none, acted days only){suffix} =====",
        f"  n_act={n} mean(gated-none)={mean_d:.4f} "
        f"bootstrap95%=[{lo:.4f}, {hi:.4f}] crosses_zero={cross}",
    ]
    if 0 < n < MIN_ACT_DAYS_FOR_CI:
        lines.append(
            f"  WARNING: n_act={n} < {MIN_ACT_DAYS_FOR_CI} — CI is not "
            f"trustworthy at this sample size; treat as no evidence"
        )
    if act:
        tot_n = sum(a.net_none for a in act)
        tot_g = sum(a.net_gated for a in act)
        lines.append(
            f"  totals on acted days: none={tot_n:.2f} gated={tot_g:.2f} "
            f"edge={tot_g - tot_n:.2f}"
        )
    return lines


def iter_weekdays_in_range(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out
