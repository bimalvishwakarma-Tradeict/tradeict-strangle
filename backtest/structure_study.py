#!/usr/bin/env python3
"""
Condor structure study — read-only measurement on option MARK bars.

No strategy execution, no adjustments, no early exits.
Answers: which (DTE, short method, wing width) baskets are deployable
and what width-ratio / breakeven vs observed look like over ~25 months.

Does NOT modify live bot code. Marks opened mode=ro.
No print() — logging + file writes only.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
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

import s001_income_engine as eng  # noqa: E402
import s001_mark_engine as me  # noqa: E402
from s004_gate import (  # noqa: E402
    SECONDS_PER_YEAR,
    black76_abs_delta,
    implied_vol_bisection,
)
from slippage_model import load_slip_table, slip_pct  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_TXT = RESULTS_DIR / "structure_study.txt"
OUT_CSV = RESULTS_DIR / "structure_study.csv"

DTES = (0, 1, 2, 3, 7)
DELTA_TARGETS = (0.15, 0.20, 0.25, 0.30)
PREMIUM_TARGETS = (100.0, 150.0, 200.0, 300.0)
WING_WIDTHS = (250.0, 500.0, 750.0, 1000.0, 1500.0, 2000.0, 2500.0, 3000.0)
ENTRY_HOUR, ENTRY_MINUTE = 11, 0
TRADEABLE_MARK_MIN = 10.0
DELTA_TOL = 0.05
PREMIUM_REL_TOL = 0.25
PREMIUM_ABS_TOL = 30.0
CAPITAL_USD = 100.0
RISK_BUDGET_USD = 3.0  # 3% of $100
CONTRACT_VALUE = eng.CONTRACT_VALUE

logger = logging.getLogger("structure_study")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    if len(ys) == 1:
        return float(ys[0])
    k = (len(ys) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(ys[int(k)])
    return float(ys[f] * (c - k) + ys[c] * (k - f))


def t_years_to_expiry(entry_ts: int, expiry: date) -> float:
    exp_ts = me.to_unix(me.ist_dt(expiry, me.EXPIRY_HOUR_IST, me.EXPIRY_MINUTE_IST))
    return max((exp_ts - entry_ts) / SECONDS_PER_YEAR, 1e-12)


def strike_step(strikes: list[float]) -> float:
    if len(strikes) < 2:
        return float("nan")
    diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
    diffs = [d for d in diffs if d > 0]
    return float(statistics.median(diffs)) if diffs else float("nan")


def slip_frac(premium: float, dte: int) -> float:
    return max(0.0, float(slip_pct(premium, int(max(0, dte)))) / 100.0)


def sell_fill(mark: float, sf: float) -> float:
    return float(mark) * (1.0 - sf)


def buy_fill(mark: float, sf: float) -> float:
    return float(mark) * (1.0 + sf)


def premium_ok(prem: float, target: float) -> bool:
    if target <= 0 or prem <= 0:
        return False
    return abs(prem - target) <= max(PREMIUM_ABS_TOL, PREMIUM_REL_TOL * target)


def iter_calendar_days(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# ---------------------------------------------------------------------------
# Short selection
# ---------------------------------------------------------------------------
@dataclass
class ShortPick:
    method: str
    call_k: float
    put_k: float
    call_mark: float
    put_mark: float
    call_metric: float  # |delta| or premium
    put_metric: float
    found: bool
    closest_note: str = ""


def _score_delta_side(
    rows: list[dict[str, Any]],
    *,
    spot: float,
    t_years: float,
    target_abs_delta: float,
    is_call: bool,
) -> tuple[dict[str, Any] | None, float, float]:
    """Return (best_row, |delta|, mark) closest to target among OTM."""
    best: tuple[float, dict[str, Any], float, float] | None = None
    for r in rows:
        k = float(r["strike"])
        m = float(r["mark_price"])
        if m <= 0:
            continue
        if is_call and k <= spot:
            continue
        if (not is_call) and k >= spot:
            continue
        iv = implied_vol_bisection(m, spot, k, t_years, is_call)
        if iv is None or iv <= 0:
            continue
        dlt = black76_abs_delta(spot, k, t_years, iv, is_call)
        err = abs(dlt - target_abs_delta)
        if best is None or err < best[0] or (err == best[0] and abs(k - spot) < abs(best[1]["strike"] - spot)):
            best = (err, r, dlt, m)
    if best is None:
        return None, float("nan"), float("nan")
    return best[1], best[2], best[3]


def pick_short_delta(
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    spot: float,
    t_years: float,
    target: float,
) -> ShortPick:
    method = f"delta_{target:.2f}"
    cr, cd, cm = _score_delta_side(
        calls, spot=spot, t_years=t_years, target_abs_delta=target, is_call=True
    )
    pr, pd, pm = _score_delta_side(
        puts, spot=spot, t_years=t_years, target_abs_delta=target, is_call=False
    )
    if cr is None or pr is None:
        note_parts = []
        if cr is None:
            note_parts.append("no_otm_call_iv")
        else:
            note_parts.append(f"call_|d|={cd:.3f}")
        if pr is None:
            note_parts.append("no_otm_put_iv")
        else:
            note_parts.append(f"put_|d|={pd:.3f}")
        return ShortPick(
            method=method,
            call_k=float(cr["strike"]) if cr else float("nan"),
            put_k=float(pr["strike"]) if pr else float("nan"),
            call_mark=cm if cr else float("nan"),
            put_mark=pm if pr else float("nan"),
            call_metric=cd,
            put_metric=pd,
            found=False,
            closest_note=";".join(note_parts),
        )
    found = abs(cd - target) <= DELTA_TOL and abs(pd - target) <= DELTA_TOL
    note = ""
    if not found:
        note = f"closest call_|d|={cd:.3f} put_|d|={pd:.3f}"
    return ShortPick(
        method=method,
        call_k=float(cr["strike"]),
        put_k=float(pr["strike"]),
        call_mark=cm,
        put_mark=pm,
        call_metric=cd,
        put_metric=pd,
        found=found,
        closest_note=note,
    )


def pick_short_premium(
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    spot: float,
    target: float,
) -> ShortPick:
    method = f"prem_{target:.0f}"
    picked = me.pick_strangle_by_premium_marks(calls, puts, spot, target)
    if picked is None:
        # report closest OTM marks if any
        otm_c = [float(r["mark_price"]) for r in calls if float(r["strike"]) > spot and float(r["mark_price"]) > 0]
        otm_p = [float(r["mark_price"]) for r in puts if float(r["strike"]) < spot and float(r["mark_price"]) > 0]
        note = (
            f"no_pair closest_call_prem="
            f"{min(otm_c, key=lambda x: abs(x - target)) if otm_c else 'none'} "
            f"closest_put_prem="
            f"{min(otm_p, key=lambda x: abs(x - target)) if otm_p else 'none'}"
        )
        return ShortPick(
            method=method,
            call_k=float("nan"),
            put_k=float("nan"),
            call_mark=float("nan"),
            put_mark=float("nan"),
            call_metric=float("nan"),
            put_metric=float("nan"),
            found=False,
            closest_note=note,
        )
    cr, pr = picked
    cm = float(cr["mark_price"])
    pm = float(pr["mark_price"])
    found = premium_ok(cm, target) and premium_ok(pm, target)
    note = ""
    if not found:
        note = f"closest call_prem={cm:.2f} put_prem={pm:.2f}"
    return ShortPick(
        method=method,
        call_k=float(cr["strike"]),
        put_k=float(pr["strike"]),
        call_mark=cm,
        put_mark=pm,
        call_metric=cm,
        put_metric=pm,
        found=found,
        closest_note=note,
    )


# ---------------------------------------------------------------------------
# Accumulators
# ---------------------------------------------------------------------------
@dataclass
class DepthDay:
    n_strikes: int
    step: float
    far_call_pts: float
    far_put_pts: float
    tradeable_call_pts: float
    tradeable_put_pts: float


@dataclass
class ShortDay:
    found: bool
    closest_note: str = ""


@dataclass
class WingObs:
    wing_present: bool
    wing_c_mark: float = float("nan")
    wing_p_mark: float = float("nan")
    net_credit: float = float("nan")
    max_loss: float = float("nan")
    width_ratio: float = float("nan")
    be_win_rate: float = float("nan")
    lots_at_3pct: int = 0
    daily_ceiling: float = float("nan")
    daily_ceiling_pct: float = float("nan")
    # settlement (section D)
    settled: bool = False
    between_shorts: bool = False
    one_side_itm: bool = False
    max_loss_hit: bool = False
    realized_pnl: float = float("nan")


@dataclass
class StudyState:
    depth: dict[int, list[DepthDay]] = field(default_factory=lambda: defaultdict(list))
    short_avail: dict[tuple[int, str], list[ShortDay]] = field(
        default_factory=lambda: defaultdict(list)
    )
    wing_obs: dict[tuple[int, str, float], list[WingObs]] = field(
        default_factory=lambda: defaultdict(list)
    )
    days_seen: int = 0
    days_with_spot: int = 0


def measure_depth(
    calls: list[dict[str, Any]], puts: list[dict[str, Any]], spot: float
) -> DepthDay:
    all_k = sorted({float(r["strike"]) for r in calls} | {float(r["strike"]) for r in puts})
    step = strike_step(all_k)
    otm_c = [
        (float(r["strike"]), float(r["mark_price"]))
        for r in calls
        if float(r["strike"]) > spot and float(r["mark_price"]) > 0
    ]
    otm_p = [
        (float(r["strike"]), float(r["mark_price"]))
        for r in puts
        if float(r["strike"]) < spot and float(r["mark_price"]) > 0
    ]
    far_c = max((k for k, _ in otm_c), default=float("nan"))
    far_p = min((k for k, _ in otm_p), default=float("nan"))
    far_c_pts = (far_c - spot) if far_c == far_c else float("nan")
    far_p_pts = (spot - far_p) if far_p == far_p else float("nan")
    trad_c = [k for k, m in otm_c if m >= TRADEABLE_MARK_MIN]
    trad_p = [k for k, m in otm_p if m >= TRADEABLE_MARK_MIN]
    tc = max(trad_c) if trad_c else float("nan")
    tp = min(trad_p) if trad_p else float("nan")
    return DepthDay(
        n_strikes=len(all_k),
        step=step,
        far_call_pts=far_c_pts,
        far_put_pts=far_p_pts,
        tradeable_call_pts=(tc - spot) if tc == tc else float("nan"),
        tradeable_put_pts=(spot - tp) if tp == tp else float("nan"),
    )


def build_wing_obs(
    *,
    pick: ShortPick,
    calls: list[dict[str, Any]],
    puts: list[dict[str, Any]],
    spot: float,
    dte: int,
    width: float,
    settle_spot: float | None,
) -> WingObs:
    all_k = sorted({float(r["strike"]) for r in calls} | {float(r["strike"]) for r in puts})
    wings = me.pick_wing_strikes(all_k, pick.call_k, pick.put_k, width)
    if wings is None:
        return WingObs(wing_present=False)
    wc_k, wp_k = wings
    wc_row = me.find_symbol(calls, wc_k)
    wp_row = me.find_symbol(puts, wp_k)
    if wc_row is None or wp_row is None:
        return WingObs(wing_present=False)
    wc_m = float(wc_row["mark_price"])
    wp_m = float(wp_row["mark_price"])
    if wc_m <= 0 or wp_m <= 0:
        return WingObs(wing_present=False)

    sc_sf = slip_frac(pick.call_mark, dte)
    sp_sf = slip_frac(pick.put_mark, dte)
    wc_sf = slip_frac(wc_m, dte)
    wp_sf = slip_frac(wp_m, dte)
    sc_fill = sell_fill(pick.call_mark, sc_sf)
    sp_fill = sell_fill(pick.put_mark, sp_sf)
    wc_fill = buy_fill(wc_m, wc_sf)
    wp_fill = buy_fill(wp_m, wp_sf)

    net_credit = (sc_fill + sp_fill - wc_fill - wp_fill) * CONTRACT_VALUE
    actual_w_c = wc_k - pick.call_k
    actual_w_p = pick.put_k - wp_k
    wider = max(actual_w_c, actual_w_p)
    max_loss = wider * CONTRACT_VALUE - net_credit
    width_ratio = (
        net_credit / (wider * CONTRACT_VALUE) if wider > 0 else float("nan")
    )
    be_wr = (
        max_loss / (max_loss + net_credit)
        if (max_loss + net_credit) > 0
        else float("nan")
    )
    lots = (
        int(math.floor(RISK_BUDGET_USD / max_loss))
        if max_loss > 1e-12
        else 0
    )
    lots = max(0, lots)
    ceiling = lots * net_credit
    ceiling_pct = 100.0 * ceiling / CAPITAL_USD if CAPITAL_USD > 0 else float("nan")

    obs = WingObs(
        wing_present=True,
        wing_c_mark=wc_m,
        wing_p_mark=wp_m,
        net_credit=net_credit,
        max_loss=max_loss,
        width_ratio=width_ratio,
        be_win_rate=be_wr,
        lots_at_3pct=lots,
        daily_ceiling=ceiling,
        daily_ceiling_pct=ceiling_pct,
    )

    if settle_spot is None or settle_spot <= 0:
        return obs

    s = float(settle_spot)
    # settlement intrinsic (no exit slip)
    sc_exit = eng.call_intrinsic(s, pick.call_k)
    sp_exit = eng.put_intrinsic(s, pick.put_k)
    wc_exit = eng.call_intrinsic(s, wc_k)
    wp_exit = eng.put_intrinsic(s, wp_k)
    gross = (
        (sc_fill - sc_exit)
        + (sp_fill - sp_exit)
        + (wc_exit - wc_fill)
        + (wp_exit - wp_fill)
    ) * CONTRACT_VALUE
    fees = (
        eng.option_fee(sc_fill, spot, 1)
        + eng.option_fee(sp_fill, spot, 1)
        + eng.option_fee(wc_fill, spot, 1)
        + eng.option_fee(wp_fill, spot, 1)
    )
    obs.settled = True
    obs.realized_pnl = gross - fees
    obs.between_shorts = pick.put_k < s < pick.call_k
    call_itm = s > pick.call_k
    put_itm = s < pick.put_k
    obs.one_side_itm = (call_itm or put_itm) and not (call_itm and put_itm)
    # max-loss zone: beyond a wing
    obs.max_loss_hit = s >= wc_k or s <= wp_k
    return obs


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------
def resolve_date_window(args: argparse.Namespace) -> tuple[date, date]:
    store = me.MarksStore()
    months = store.available_months
    store.close()
    if not months:
        raise SystemExit("No marks_YYYY-MM.sqlite files found")
    first = date.fromisoformat(months[0] + "-01")
    last_ym = months[-1]
    y, m = int(last_ym[:4]), int(last_ym[5:7])
    if m == 12:
        last = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(y, m + 1, 1) - timedelta(days=1)
    d0 = date.fromisoformat(args.from_date) if args.from_date else first
    d1 = date.fromisoformat(args.to_date) if args.to_date else last
    return d0, d1


def scan(d0: date, d1: date) -> StudyState:
    load_slip_table()
    spot_path = me.find_spot_csv()
    if spot_path is None:
        raise SystemExit("No BTCUSD_1m_*.csv found")
    spot_map = me.load_spot_map(spot_path)
    logger.info("spot_csv=%s bars=%d window=%s..%s", spot_path.name, len(spot_map), d0, d1)

    store = me.MarksStore()
    state = StudyState()
    current_month = ""
    month_t0 = time.time()

    short_methods: list[tuple[str, Any]] = []
    for dlt in DELTA_TARGETS:
        short_methods.append(("delta", dlt))
    for prem in PREMIUM_TARGETS:
        short_methods.append(("prem", prem))

    for day in iter_calendar_days(d0, d1):
        mk = month_key(day)
        if mk != current_month:
            if current_month:
                logger.info(
                    "progress month=%s done days_seen=%d elapsed_month=%.1fs",
                    current_month,
                    state.days_seen,
                    time.time() - month_t0,
                )
            current_month = mk
            month_t0 = time.time()
            logger.info("scanning month=%s ...", current_month)

        state.days_seen += 1
        entry_ts = me.to_unix(me.ist_dt(day, ENTRY_HOUR, ENTRY_MINUTE))
        conn = store.conn(day)
        if conn is None:
            continue

        day_had_spot = False
        for dte in DTES:
            expiry = day + timedelta(days=dte)
            if expiry == me.SKIP_EXPIRY:
                continue
            cts = me.resolve_mark_ts(conn, expiry, entry_ts)
            if cts is None:
                continue
            spot, _src = me.resolve_forward(store, spot_map, expiry, entry_ts)
            if spot is None or spot <= 0:
                continue
            day_had_spot = True

            calls = me.load_chain(conn, expiry, cts, "call")
            puts = me.load_chain(conn, expiry, cts, "put")
            if not calls or not puts:
                continue

            state.depth[dte].append(measure_depth(calls, puts, spot))
            t_y = t_years_to_expiry(entry_ts, expiry)

            settle_ts = me.to_unix(
                me.ist_dt(expiry, me.EXPIRY_HOUR_IST, me.EXPIRY_MINUTE_IST)
            )
            settle_spot, _ = me.resolve_forward(store, spot_map, expiry, settle_ts)

            for kind, target in short_methods:
                if kind == "delta":
                    pick = pick_short_delta(calls, puts, spot, t_y, float(target))
                else:
                    pick = pick_short_premium(calls, puts, spot, float(target))
                state.short_avail[(dte, pick.method)].append(
                    ShortDay(found=pick.found, closest_note=pick.closest_note)
                )
                if not pick.found:
                    continue
                for width in WING_WIDTHS:
                    obs = build_wing_obs(
                        pick=pick,
                        calls=calls,
                        puts=puts,
                        spot=spot,
                        dte=dte,
                        width=float(width),
                        settle_spot=settle_spot,
                    )
                    state.wing_obs[(dte, pick.method, float(width))].append(obs)

        if day_had_spot:
            state.days_with_spot += 1

    if current_month:
        logger.info(
            "progress month=%s done days_seen=%d elapsed_month=%.1fs",
            current_month,
            state.days_seen,
            time.time() - month_t0,
        )
    store.close()
    return state


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _finite(xs: list[float]) -> list[float]:
    return [x for x in xs if x == x and not math.isinf(x)]


def format_report(state: StudyState, d0: date, d1: date) -> tuple[list[str], list[dict[str, Any]]]:
    lines: list[str] = [
        "===== CONDOR STRUCTURE STUDY =====",
        f"generated_utc={datetime.now(tz=UTC).isoformat()}",
        f"window={d0} .. {d1}",
        f"entry=11:00 IST  dtes={list(DTES)}",
        f"delta_targets={list(DELTA_TARGETS)}  premium_targets={list(PREMIUM_TARGETS)}",
        f"wing_widths={list(WING_WIDTHS)}",
        f"days_seen={state.days_seen} days_with_spot_touch={state.days_with_spot}",
        "slippage=bucketed  fees=s001_income_engine.option_fee  CV=0.001",
        "settlement=intrinsic at 17:30 IST (no adjustments / no early exit)",
        "",
    ]

    # --- A depth ---
    lines.append("===== A) CHAIN DEPTH =====")
    lines.append(
        f"{'DTE':>3} {'n_med':>6} {'step':>6} "
        f"{'farC_med':>9} {'farC_p10':>9} {'farP_med':>9} {'farP_p10':>9} "
        f"{'trC_med':>9} {'trC_p10':>9} {'trP_med':>9} {'trP_p10':>9} {'ndays':>5}"
    )
    for dte in DTES:
        rows = state.depth.get(dte) or []
        if not rows:
            lines.append(f"{dte:>3}  (no data)")
            continue
        n_med = statistics.median([r.n_strikes for r in rows])
        step = statistics.median(_finite([r.step for r in rows]) or [float("nan")])
        fc = _finite([r.far_call_pts for r in rows])
        fp = _finite([r.far_put_pts for r in rows])
        tc = _finite([r.tradeable_call_pts for r in rows])
        tp = _finite([r.tradeable_put_pts for r in rows])
        lines.append(
            f"{dte:>3} {n_med:6.0f} {step:6.0f} "
            f"{statistics.median(fc) if fc else float('nan'):9.0f} "
            f"{percentile(fc, 10) if fc else float('nan'):9.0f} "
            f"{statistics.median(fp) if fp else float('nan'):9.0f} "
            f"{percentile(fp, 10) if fp else float('nan'):9.0f} "
            f"{statistics.median(tc) if tc else float('nan'):9.0f} "
            f"{percentile(tc, 10) if tc else float('nan'):9.0f} "
            f"{statistics.median(tp) if tp else float('nan'):9.0f} "
            f"{percentile(tp, 10) if tp else float('nan'):9.0f} "
            f"{len(rows):5d}"
        )
    lines.append(
        "far*=farthest OTM points from spot; tr*=farthest OTM with mark>=$10 "
        "(tradeable depth). med=median across days, p10=10th percentile (thin days)."
    )
    lines.append("")

    # --- B short availability ---
    lines.append("===== B) SHORT SELECTION AVAILABILITY =====")
    lines.append(
        f"{'DTE':>3} {'method':<12} {'found%':>7} {'n':>5}  miss_examples_closest"
    )
    for dte in DTES:
        for kind, target in (
            [("delta", d) for d in DELTA_TARGETS]
            + [("prem", p) for p in PREMIUM_TARGETS]
        ):
            method = f"delta_{target:.2f}" if kind == "delta" else f"prem_{target:.0f}"
            rows = state.short_avail.get((dte, method)) or []
            if not rows:
                continue
            n = len(rows)
            found_n = sum(1 for r in rows if r.found)
            miss_notes = [r.closest_note for r in rows if (not r.found) and r.closest_note]
            # unique up to 3
            uniq: list[str] = []
            for note in miss_notes:
                if note not in uniq:
                    uniq.append(note)
                if len(uniq) >= 3:
                    break
            lines.append(
                f"{dte:>3} {method:<12} {100.0 * found_n / n:6.1f}% {n:5d}  "
                f"{' | '.join(uniq) if uniq else '-'}"
            )
    lines.append(
        f"found = |delta| within ±{DELTA_TOL} of target, or premium within "
        f"max(${PREMIUM_ABS_TOL:.0f}, {100*PREMIUM_REL_TOL:.0f}% of target)."
    )
    lines.append("")

    # --- C + D combo table → CSV rows ---
    lines.append("===== C/D) WING + SETTLEMENT (no adjustments) =====")
    csv_rows: list[dict[str, Any]] = []
    hdr = (
        f"{'DTE':>3} {'method':<12} {'wing':>5} {'wing%':>6} "
        f"{'wMark':>7} {'credit':>7} {'maxL':>7} {'wRatio':>7} "
        f"{'beWR':>6} {'obsWR':>6} {'btwn%':>6} {'1ITM%':>6} {'maxL%':>6} "
        f"{'meanPnL':>8} {'lots3':>5} {'ceil':>7} {'ceil%':>6} {'n':>5}"
    )
    lines.append(hdr)

    combo_stats: list[dict[str, Any]] = []
    for dte in DTES:
        for kind, target in (
            [("delta", d) for d in DELTA_TARGETS]
            + [("prem", p) for p in PREMIUM_TARGETS]
        ):
            method = f"delta_{target:.2f}" if kind == "delta" else f"prem_{target:.0f}"
            for width in WING_WIDTHS:
                obs_list = state.wing_obs.get((dte, method, float(width))) or []
                if not obs_list:
                    continue
                n = len(obs_list)
                present = [o for o in obs_list if o.wing_present]
                wing_pct = 100.0 * len(present) / n if n else float("nan")
                if not present:
                    continue
                w_marks = _finite(
                    [(o.wing_c_mark + o.wing_p_mark) / 2.0 for o in present]
                )
                credits = _finite([o.net_credit for o in present])
                maxls = _finite([o.max_loss for o in present])
                ratios = _finite([o.width_ratio for o in present])
                be_wrs = _finite([o.be_win_rate for o in present])
                settled = [o for o in present if o.settled]
                ns = len(settled)
                btwn = (
                    100.0 * sum(1 for o in settled if o.between_shorts) / ns
                    if ns
                    else float("nan")
                )
                one_itm = (
                    100.0 * sum(1 for o in settled if o.one_side_itm) / ns
                    if ns
                    else float("nan")
                )
                max_hit = (
                    100.0 * sum(1 for o in settled if o.max_loss_hit) / ns
                    if ns
                    else float("nan")
                )
                # observed win = positive realized PnL
                wins = [o for o in settled if o.realized_pnl == o.realized_pnl and o.realized_pnl > 0]
                obs_wr = 100.0 * len(wins) / ns if ns else float("nan")
                pnls = _finite([o.realized_pnl for o in settled])
                mean_pnl = statistics.fmean(pnls) if pnls else float("nan")
                med_credit = statistics.median(credits) if credits else float("nan")
                med_maxl = statistics.median(maxls) if maxls else float("nan")
                med_ratio = statistics.median(ratios) if ratios else float("nan")
                med_be = statistics.median(be_wrs) if be_wrs else float("nan")
                med_wmark = statistics.median(w_marks) if w_marks else float("nan")
                lots = int(
                    statistics.median([o.lots_at_3pct for o in present])
                ) if present else 0
                # recompute ceiling from median credit × lots
                ceil = lots * med_credit if med_credit == med_credit else float("nan")
                ceil_pct = (
                    100.0 * ceil / CAPITAL_USD if ceil == ceil else float("nan")
                )

                row = {
                    "dte": dte,
                    "method": method,
                    "wing_width": width,
                    "n_short_days": n,
                    "wing_present_pct": wing_pct,
                    "median_wing_mark": med_wmark,
                    "median_net_credit": med_credit,
                    "median_max_loss": med_maxl,
                    "median_width_ratio": med_ratio,
                    "median_breakeven_win_rate": med_be,
                    "observed_win_rate_pct": obs_wr,
                    "between_shorts_pct": btwn,
                    "one_side_itm_pct": one_itm,
                    "max_loss_hit_pct": max_hit,
                    "mean_realized_pnl_per_lot": mean_pnl,
                    "lots_at_3pct": lots,
                    "max_daily_ceiling": ceil,
                    "max_daily_ceiling_pct_of_100": ceil_pct,
                    "n_settled": ns,
                }
                csv_rows.append(row)
                combo_stats.append(row)
                lines.append(
                    f"{dte:>3} {method:<12} {width:5.0f} {wing_pct:5.1f}% "
                    f"{med_wmark:7.2f} {med_credit:7.4f} {med_maxl:7.4f} "
                    f"{med_ratio:7.3f} {100.0*med_be if med_be==med_be else float('nan'):5.1f}% "
                    f"{obs_wr:5.1f}% {btwn:5.1f}% {one_itm:5.1f}% {max_hit:5.1f}% "
                    f"{mean_pnl:8.4f} {lots:5d} {ceil:7.4f} {ceil_pct:5.1f}% {ns:5d}"
                )

    lines.append("")
    lines.append("===== E) SUMMARY — best 3 combos per DTE by mean realized PnL/lot =====")
    lines.append(
        f"{'DTE':>3} {'rank':>4} {'method':<12} {'wing':>5} {'meanPnL':>8} "
        f"{'wRatio':>7} {'beWR':>6} {'obsWR':>6} {'lots3':>5} {'ceil%':>6}"
    )
    for dte in DTES:
        subset = [
            r
            for r in combo_stats
            if r["dte"] == dte
            and r["mean_realized_pnl_per_lot"] == r["mean_realized_pnl_per_lot"]
            and r["n_settled"] >= 20
        ]
        subset.sort(key=lambda r: r["mean_realized_pnl_per_lot"], reverse=True)
        if not subset:
            lines.append(f"{dte:>3}  (insufficient settled samples)")
            continue
        for i, r in enumerate(subset[:3], start=1):
            be = r["median_breakeven_win_rate"]
            lines.append(
                f"{dte:>3} {i:4d} {r['method']:<12} {r['wing_width']:5.0f} "
                f"{r['mean_realized_pnl_per_lot']:8.4f} "
                f"{r['median_width_ratio']:7.3f} "
                f"{100.0*be if be==be else float('nan'):5.1f}% "
                f"{r['observed_win_rate_pct']:5.1f}% "
                f"{r['lots_at_3pct']:5d} "
                f"{r['max_daily_ceiling_pct_of_100']:5.1f}%"
            )

    lines.append("")
    lines.append(
        "Ye structure study hai — adjustments aur early exit shaamil nahi. "
        "S001 ki asli kamai adjustments se aati hai, isliye ye lower bound hai."
    )
    lines.append("")
    return lines, csv_rows


def write_outputs(lines: list[str], csv_rows: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("wrote %s", OUT_TXT)
    if not csv_rows:
        logger.warning("no CSV rows")
        return
    cols = list(csv_rows[0].keys())
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in csv_rows:
            w.writerow(row)
    logger.info("wrote %s (%d rows)", OUT_CSV, len(csv_rows))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="Condor structure study on mark data (read-only)"
    )
    ap.add_argument("--from", dest="from_date", type=str, default=None)
    ap.add_argument("--to", dest="to_date", type=str, default=None)
    args = ap.parse_args()

    d0, d1 = resolve_date_window(args)
    t0 = time.time()
    state = scan(d0, d1)
    lines, csv_rows = format_report(state, d0, d1)
    lines.append(f"elapsed_sec={time.time() - t0:.1f}")
    write_outputs(lines, csv_rows)
    for ln in lines:
        logger.info("%s", ln)


if __name__ == "__main__":
    main()
