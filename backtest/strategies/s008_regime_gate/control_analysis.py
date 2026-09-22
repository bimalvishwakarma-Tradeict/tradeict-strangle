"""S008 T1/T2/T3 — gate vs forced-skip control analysis."""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.costs import ensure_slip_table  # noqa: E402
from backtest.harness.data import MarksStore, find_spot_csv, load_spot_map  # noqa: E402
from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.strategies.s008_regime_gate.signal import (  # noqa: E402
    DaySignal,
    build_signals_through,
)
from backtest.strategies.s008_regime_gate.strategy import (  # noqa: E402
    DEFAULT_MAX_STRIKE_GAP,
    DEFAULT_PREMIUM_TARGET_PCT,
    DEFAULT_TARGET_DELTA,
    IS_FROM,
    OOS_TO,
    S008RegimeGateStrategy,
    decide_side,
    iter_weekdays,
    load_chain_pk,
    load_chain_sql,
    pick_wings,
    sig_decile,
    zero_dte_expiry,
)

logger = logging.getLogger("s008.control")


@dataclass
class DayRecord:
    d: date
    strikes_available: bool
    overnight_move_pct: float
    sig: float
    sig_decile: int
    gate_decision_none: str
    gate_decision_flat: str
    gate_decision_switch: str
    skip_reason: str
    net_none: float | None
    net_flat: float | None
    net_switch: float | None


def _pctiles(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {k: float("nan") for k in ("n", "mean", "p25", "p50", "p75", "p90", "max")}
    s = sorted(xs)

    def q(p: float) -> float:
        if len(s) == 1:
            return s[0]
        i = (len(s) - 1) * p
        lo = int(math.floor(i))
        hi = int(math.ceil(i))
        if lo == hi:
            return s[lo]
        return s[lo] * (hi - i) + s[hi] * (i - lo)

    return {
        "n": float(len(xs)),
        "mean": float(statistics.fmean(xs)),
        "p25": q(0.25),
        "p50": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
        "max": float(s[-1]),
    }


def build_day_records(
    *,
    d0: date,
    d1: date,
    threshold: float,
    max_strike_gap: float,
    strike_mode: str,
    spot: dict[int, float],
    store: MarksStore,
    sigs: dict[date, DaySignal],
    premium_target_pct: float = DEFAULT_PREMIUM_TARGET_PCT,
    target_delta: float = DEFAULT_TARGET_DELTA,
) -> list[DayRecord]:
    """Per-day availability + gate decisions + PnL under each gate (if tradable)."""
    strats = {
        g: S008RegimeGateStrategy(
            gate=g,  # type: ignore[arg-type]
            threshold=threshold,
            max_strike_gap=max_strike_gap,
            strike_mode=strike_mode,  # type: ignore[arg-type]
            premium_target_pct=premium_target_pct,
            target_delta=target_delta,
        )
        for g in ("none", "flat", "switch")
    }
    out: list[DayRecord] = []
    for d in iter_weekdays(d0, d1):
        s = sigs.get(d)
        if s is None:
            continue
        entry_ts = to_unix(ist_dt(d, 9, 0))
        sp = spot.get(entry_ts)
        avail = False
        skip_reason = "no_spot_entry"
        if sp is not None and sp > 0:
            conn = store.conn(d)
            if conn is None:
                skip_reason = "no_marks"
            else:
                if strike_mode in ("premium", "delta"):
                    calls, puts = load_chain_sql(
                        conn, zero_dte_expiry(d), entry_ts
                    )
                    if not calls and not puts:
                        calls, puts = load_chain_pk(
                            conn, zero_dte_expiry(d), entry_ts, float(sp)
                        )
                else:
                    calls, puts = load_chain_pk(
                        conn, zero_dte_expiry(d), entry_ts, float(sp)
                    )
                picked = pick_wings(
                    calls,
                    puts,
                    float(sp),
                    strike_mode=strike_mode,  # type: ignore[arg-type]
                    max_strike_gap=max_strike_gap,
                    premium_target_pct=premium_target_pct,
                    target_delta=target_delta,
                )
                avail = picked.strikes_available
                skip_reason = picked.skip_reason or ""

        nets: dict[str, float | None] = {"none": None, "flat": None, "switch": None}
        for g, strat in strats.items():
            b = strat.simulate_day(d=d, sig=s, store=store, spot_close=spot)
            if not b.skipped:
                nets[g] = b.net_pnl

        out.append(
            DayRecord(
                d=d,
                strikes_available=avail,
                overnight_move_pct=s.overnight * 100.0,
                sig=s.sig,
                sig_decile=sig_decile(s.sig),
                gate_decision_none=decide_side("none", s.sig, threshold),
                gate_decision_flat=decide_side("flat", s.sig, threshold),
                gate_decision_switch=decide_side("switch", s.sig, threshold),
                skip_reason=skip_reason,
                net_none=nets["none"],
                net_flat=nets["flat"],
                net_switch=nets["switch"],
            )
        )
    return out


def run_t1_t2_t3(
    records: list[DayRecord], *, threshold: float
) -> list[str]:
    lines: list[str] = []
    avail = [r for r in records if r.strikes_available]
    unavail = [r for r in records if not r.strikes_available]

    # ----- T1 -----
    lines.append("===== T1 — |overnight| vs strikes_available =====")
    a_on = [abs(r.overnight_move_pct) for r in avail]
    u_on = [abs(r.overnight_move_pct) for r in unavail]
    pa, pu = _pctiles(a_on), _pctiles(u_on)
    lines.append(
        f"available n={int(pa['n'])} |overnight|% "
        f"mean={pa['mean']:.4f} p25={pa['p25']:.4f} p50={pa['p50']:.4f} "
        f"p75={pa['p75']:.4f} p90={pa['p90']:.4f} max={pa['max']:.4f}"
    )
    lines.append(
        f"unavailable n={int(pu['n'])} |overnight|% "
        f"mean={pu['mean']:.4f} p25={pu['p25']:.4f} p50={pu['p50']:.4f} "
        f"p75={pu['p75']:.4f} p90={pu['p90']:.4f} max={pu['max']:.4f}"
    )
    if a_on and u_on:
        ratio = pu["mean"] / pa["mean"] if pa["mean"] > 0 else float("nan")
        lines.append(
            f"unavailable_mean / available_mean = {ratio:.3f} "
            f"(>1 ⇒ unavailable days are higher-overnight)"
        )
    lines.append("")

    # ----- T2 -----
    lines.append(
        "===== T2 — DECISIVE: gate PnL ONLY on strikes_available=True ====="
    )
    lines.append("(forced skip removed from all arms — same tradeable day set)")

    def arm_stats(nets: list[float]) -> str:
        if not nets:
            return "n=0"
        mean = statistics.fmean(nets)
        total = sum(nets)
        win = sum(1 for x in nets if x > 0)
        return (
            f"n={len(nets)} total={total:.2f} mean/day={mean:.4f} "
            f"win%={100.0 * win / len(nets):.1f}"
        )

    n_none = [r.net_none for r in avail if r.net_none is not None]
    n_flat = [r.net_flat for r in avail if r.net_flat is not None]
    n_sw = [r.net_switch for r in avail if r.net_switch is not None]
    # On available days, gate=none should almost always trade (sell).
    # flat may skip high-sig days; switch may buy.
    flat_skipped = sum(1 for r in avail if r.gate_decision_flat == "flat")
    switch_buys = sum(1 for r in avail if r.gate_decision_switch == "buy")
    lines.append(f"gate=none  (always sell): {arm_stats([x for x in n_none if x is not None])}")
    lines.append(
        f"gate=flat  (skip high sig): {arm_stats([x for x in n_flat if x is not None])} "
        f"gate_flat_skips_on_avail={flat_skipped}"
    )
    lines.append(
        f"gate=switch (buy high sig): {arm_stats([x for x in n_sw if x is not None])} "
        f"buys_on_avail={switch_buys}"
    )

    # Matched-day comparison: days where ALL three have a trade PnL
    # (flat may have None when gate_flat — that's intentional)
    # Compare none vs switch on days both traded; none vs flat on days flat traded.
    both_ns = [
        (r.net_none, r.net_switch)
        for r in avail
        if r.net_none is not None and r.net_switch is not None
    ]
    if both_ns:
        d_sw = [b - a for a, b in both_ns]
        lines.append(
            f"matched none↔switch n={len(both_ns)} "
            f"mean(switch-none)={statistics.fmean(d_sw):.4f} "
            f"total(switch-none)={sum(d_sw):.2f}"
        )
    both_nf = [
        (r.net_none, r.net_flat)
        for r in avail
        if r.net_none is not None and r.net_flat is not None
    ]
    # Days flat traded (= low sig): compare; also report what none earned on days flat skipped
    none_on_flat_skip = [
        r.net_none
        for r in avail
        if r.gate_decision_flat == "flat" and r.net_none is not None
    ]
    if both_nf:
        lines.append(
            f"matched none↔flat (both traded) n={len(both_nf)} "
            f"mean(flat-none)={statistics.fmean([b - a for a, b in both_nf]):.4f}"
        )
    if none_on_flat_skip:
        lines.append(
            f"on days flat GATED OUT (still available): n={len(none_on_flat_skip)} "
            f"none_total={sum(none_on_flat_skip):.2f} "
            f"none_mean={statistics.fmean(none_on_flat_skip):.4f} "
            f"(this is the 'gate benefit' if positive → avoided loss; "
            f"if negative → gate skipped winners)"
        )
        # Fair T2: assign 0 PnL to flat on gated days, compare totals on same avail set
        flat_fair = []
        none_fair = []
        switch_fair = []
        for r in avail:
            if r.net_none is None:
                continue  # data hole
            none_fair.append(r.net_none)
            flat_fair.append(0.0 if r.gate_decision_flat == "flat" else (r.net_flat or 0.0))
            switch_fair.append(r.net_switch if r.net_switch is not None else 0.0)
        if none_fair:
            lines.append(
                f"FAIR same-day-set (avail only, flat/switch miss→0): "
                f"none_total={sum(none_fair):.2f} "
                f"flat_total={sum(flat_fair):.2f} "
                f"switch_total={sum(switch_fair):.2f}"
            )
            lines.append(
                f"  flat_edge={sum(flat_fair) - sum(none_fair):.2f}  "
                f"switch_edge={sum(switch_fair) - sum(none_fair):.2f}"
            )
            if abs(sum(flat_fair) - sum(none_fair)) < 1e-6 and abs(
                sum(switch_fair) - sum(none_fair)
            ) < 1e-6:
                lines.append(
                    "VERDICT: gate edge ≈ 0 on available days → "
                    "prior S008 edge was mechanical (forced skip)."
                )
            elif sum(flat_fair) > sum(none_fair) or sum(switch_fair) > sum(none_fair):
                lines.append(
                    "VERDICT: gate still helps on available days "
                    "(not purely mechanical)."
                )
            else:
                lines.append(
                    "VERDICT: gate HURTS or neutral on available days "
                    "vs always-sell — prior edge likely mechanical."
                )
    lines.append("")

    # ----- T3 -----
    lines.append("===== T3 — Overlap: gate flags ∩ strikes_unavailable =====")
    # Gate "flags" = days where flat would sit out OR switch would buy
    # (high-sig regime days)
    flagged = [r for r in records if r.sig >= threshold]
    flag_and_unavail = [r for r in flagged if not r.strikes_available]
    pct = (
        100.0 * len(flag_and_unavail) / len(flagged) if flagged else float("nan")
    )
    lines.append(
        f"gate-flagged days (sig>={threshold}): n={len(flagged)}"
    )
    lines.append(
        f"of those already strikes_unavailable: n={len(flag_and_unavail)} "
        f"({pct:.1f}%)"
    )
    lines.append(
        f"all unavailable: n={len(unavail)} / {len(records)} "
        f"({100.0 * len(unavail) / len(records) if records else 0:.1f}%)"
    )
    return lines


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ensure_slip_table()
    spot_path = find_spot_csv()
    if spot_path is None:
        raise SystemExit("no spot csv")
    spot = load_spot_map(spot_path)
    store = MarksStore()
    d0, d1 = IS_FROM, OOS_TO
    thr = 0.90
    max_gap = DEFAULT_MAX_STRIKE_GAP
    warm0 = date(2025, 6, 1)
    days = iter_weekdays(warm0, d1)
    sigs = build_signals_through(days, spot, through=d1)

    all_lines: list[str] = [
        "===== S008 CONTROL T1/T2/T3 =====",
        f"range={d0}..{d1} entry=09:00 threshold={thr} "
        f"max_strike_gap={max_gap}",
        "",
    ]
    for mode in ("points", "premium"):
        all_lines.append(f"########## strike_mode={mode} ##########")
        recs = build_day_records(
            d0=d0,
            d1=d1,
            threshold=thr,
            max_strike_gap=max_gap,
            strike_mode=mode,
            spot=spot,
            store=store,
            sigs=sigs,
        )
        all_lines.extend(run_t1_t2_t3(recs, threshold=thr))
        traded_pts = sum(1 for r in recs if r.strikes_available)
        all_lines.append(
            f"strikes_available days ({mode}): {traded_pts} / {len(recs)}"
        )
        all_lines.append("")

    store.close()
    text = "\n".join(all_lines) + "\n"
    out = Path(__file__).resolve().parent / "runs" / "control_t1_t2_t3.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
