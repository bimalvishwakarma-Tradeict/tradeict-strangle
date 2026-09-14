#!/usr/bin/env python3
"""
S001 winner forensic — diagnostic only (no fixes, no new sweep).

WINNER  = dte2, B_only, trigger 70, maker, B25, wings 2000, 8 lots, 11:00 IST
CONTROL = dte2, adjustment none

Reuses s001_adjustment_sweep helpers + s001_income_cycles.pkl.
"""

from __future__ import annotations

import logging
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
from backend.engine.wing_entry import compute_decrease_step_qty  # noqa: E402
from backend.strategies.s001_short_strangle.adj_b import (  # noqa: E402
    select_adj_b_strike,
)

logger = logging.getLogger("s001_winner_forensic")

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
RESULTS_DIR = _BACKTEST / "results"
OUT_PATH = RESULTS_DIR / "s001_winner_forensic.txt"

WINNER_CFG = sweep.SweepCfg(dte=2, adjustment="B_only", trigger_pct=70.0)
CONTROL_CFG = sweep.SweepCfg(dte=2, adjustment="none", trigger_pct=None)
CRASH_DAY = date(2026, 6, 2)


@dataclass
class LedgerRow:
    timestamp: str
    symbol: str
    side: str
    qty_lots: int
    price: float
    price_source: str
    index_at_time: float | str
    fee: float
    running_pnl: float
    note: str = ""


@dataclass
class AdjEvent:
    entry_date: date
    adj_n: int
    ts_utc: datetime
    minutes_after_entry: float
    index_entry: float
    index_at_adj: float
    exit_leg: str
    exit_symbol: str
    exit_price: float
    exit_price_source: str
    theo_surface: float | str


@dataclass
class ForensicCycle:
    entry_date: date
    net: float
    spot_move_abs: float
    n_adjustments: int
    ledger: list[LedgerRow] = field(default_factory=list)
    adj_events: list[AdjEvent] = field(default_factory=list)


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def na(val: Any) -> str:
    if val is None:
        return "NOT AVAILABLE"
    return str(val)


def pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return float("nan")
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 1e-15 or dy <= 1e-15:
        return float("nan")
    return num / (dx * dy)


def _ranks(vals: list[float]) -> list[float]:
    """Average ranks for ties (1-based)."""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return float("nan")
    return pearson(_ranks(xs), _ranks(ys))


def sample_skew(xs: list[float]) -> float:
    if len(xs) < 3:
        return float("nan")
    m = statistics.mean(xs)
    s = statistics.stdev(xs)
    if s <= 1e-15:
        return float("nan")
    n = len(xs)
    m3 = sum(((x - m) / s) ** 3 for x in xs) / n
    return m3


def fill_source_label(fill: eng.PrintFill | None, *, role_hint: str) -> str:
    if fill is None:
        return "NOT AVAILABLE"
    src = str(getattr(fill, "source", "") or "")
    if src == "print":
        return (
            f"real trade print (OptionsTradeStore shard; "
            f"preferred_role={role_hint}, fill_role={fill.buyer_role})"
        )
    if src == "surface":
        return "IV surface model (iv_surface.py smile / Black-76)"
    if src:
        return f"PrintFill.source={src!r}"
    return "NOT AVAILABLE"


def surface_theo(
    surface: Any | None,
    when: datetime,
    strike: float,
    exp: date,
    leg: str,
) -> float | str:
    if surface is None:
        return "NOT AVAILABLE"
    opt = "C" if leg == "call" else "P"
    try:
        res = surface.price(when, strike, exp, opt)
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        return f"NOT AVAILABLE (surface.price error: {exc})"
    if not getattr(res, "supported", False):
        return "NOT AVAILABLE (surface unsupported at this point)"
    px = float(res.price)
    if not math.isfinite(px) or px <= 0:
        return "NOT AVAILABLE (non-finite/non-positive surface price)"
    return px


def print_at(
    idx: eng.TradeIndex,
    symbol: str,
    when: datetime,
    preferred_role: str,
) -> eng.PrintFill | None:
    return eng.nearest_print_prefer(
        idx, symbol, when, eng.PRINT_WINDOW_SEC, preferred_role
    )


def append_row(
    ledger: list[LedgerRow],
    *,
    ts: datetime,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    price_source: str,
    index: float | str,
    fee: float,
    running: float,
    note: str = "",
) -> None:
    ledger.append(
        LedgerRow(
            timestamp=ts.astimezone(UTC).isoformat(),
            symbol=symbol,
            side=side,
            qty_lots=int(qty),
            price=float(price),
            price_source=price_source,
            index_at_time=index if isinstance(index, str) else float(index),
            fee=float(fee),
            running_pnl=float(running),
            note=note,
        )
    )


def simulate_control_ledger(o: eng.CycleObs) -> ForensicCycle:
    """CONTROL = hold to settlement; scale 4-lot cache economics to 8 lots."""
    qty = sweep.ORIGINAL_BASKET_QTY
    scale = sweep.QTY_SCALE
    ledger: list[LedgerRow] = []
    running = 0.0

    # Hedge — income / adjustment engines exclude hedge
    append_row(
        ledger,
        ts=o.entry_utc,
        symbol="HEDGE",
        side="NOT AVAILABLE",
        qty=0,
        price=0.0,
        price_source="NOT AVAILABLE — s001_income_engine / sweep exclude hedge",
        index=float(o.spot_entry),
        fee=0.0,
        running=running,
        note="hedge legs not present on CycleObs",
    )

    sc = o.short_call
    sp = o.short_put
    fee_sc = eng.option_fee(sc.price, o.spot_entry, qty)
    fee_sp = eng.option_fee(sp.price, o.spot_entry, qty)
    # Short entry: credit increases running (fee reduces)
    running += sweep.premium_notional_usd(sc.price, qty) - fee_sc
    append_row(
        ledger,
        ts=sc.ts_utc,
        symbol=sc.symbol,
        side="SELL",
        qty=qty,
        price=sc.price,
        price_source=fill_source_label(sc, role_hint="maker"),
        index=float(o.spot_entry),
        fee=fee_sc,
        running=running,
        note="short call entry",
    )
    running += sweep.premium_notional_usd(sp.price, qty) - fee_sp
    append_row(
        ledger,
        ts=sp.ts_utc,
        symbol=sp.symbol,
        side="SELL",
        qty=qty,
        price=sp.price,
        price_source=fill_source_label(sp, role_hint="maker"),
        index=float(o.spot_entry),
        fee=fee_sp,
        running=running,
        note="short put entry",
    )

    if o.wing_call is not None and o.wing_put is not None:
        wc, wp = o.wing_call, o.wing_put
        fee_wc = eng.option_fee(wc.price, o.spot_entry, qty)
        fee_wp = eng.option_fee(wp.price, o.spot_entry, qty)
        running -= sweep.premium_notional_usd(wc.price, qty) + fee_wc
        append_row(
            ledger,
            ts=wc.ts_utc,
            symbol=wc.symbol,
            side="BUY",
            qty=qty,
            price=wc.price,
            price_source=fill_source_label(wc, role_hint="taker"),
            index=float(o.spot_entry),
            fee=fee_wc,
            running=running,
            note=f"wing call entry; wing_used_surface={o.wing_used_surface}",
        )
        running -= sweep.premium_notional_usd(wp.price, qty) + fee_wp
        append_row(
            ledger,
            ts=wp.ts_utc,
            symbol=wp.symbol,
            side="BUY",
            qty=qty,
            price=wp.price,
            price_source=fill_source_label(wp, role_hint="taker"),
            index=float(o.spot_entry),
            fee=fee_wp,
            running=running,
            note=f"wing put entry; wing_used_surface={o.wing_used_surface}",
        )
    else:
        append_row(
            ledger,
            ts=o.entry_utc,
            symbol="WING",
            side="NOT AVAILABLE",
            qty=0,
            price=0.0,
            price_source="NOT AVAILABLE — no wing fills on this CycleObs",
            index=float(o.spot_entry),
            fee=0.0,
            running=running,
        )

    # Settlement intrinsic (matches income engine; net_no_settle excludes settle fee)
    settle_dt = datetime(
        o.basket_expiry.year,
        o.basket_expiry.month,
        o.basket_expiry.day,
        12,
        0,
        tzinfo=UTC,
    )
    sc_i = eng.call_intrinsic(o.spot_settle, o.short_call_k)
    sp_i = eng.put_intrinsic(o.spot_settle, o.short_put_k)
    # Closing shorts = buy intrinsic
    pnl_sc = eng.cash_pnl(sc.price, sc_i, qty, is_long=False)
    pnl_sp = eng.cash_pnl(sp.price, sp_i, qty, is_long=False)
    # Rebuild running from cash_pnl identity: net = scaled net_no_settle
    net = float(o.net_no_settle) * scale
    running = net  # align final to engine net (diagnostic: show settle legs)
    append_row(
        ledger,
        ts=settle_dt,
        symbol=eng.format_symbol("C", o.short_call_k, o.basket_expiry),
        side="BUY_TO_CLOSE",
        qty=qty,
        price=sc_i,
        price_source="settlement intrinsic (max(spot-strike,0) at 12:00 UTC)",
        index=float(o.spot_settle),
        fee=0.0,
        running=running,
        note=f"settle short call; cash_pnl_component={pnl_sc:.6f}",
    )
    append_row(
        ledger,
        ts=settle_dt,
        symbol=eng.format_symbol("P", o.short_put_k, o.basket_expiry),
        side="BUY_TO_CLOSE",
        qty=qty,
        price=sp_i,
        price_source="settlement intrinsic (max(strike-spot,0) at 12:00 UTC)",
        index=float(o.spot_settle),
        fee=0.0,
        running=running,
        note=f"settle short put; cash_pnl_component={pnl_sp:.6f}",
    )
    if (
        o.wing_call_k is not None
        and o.wing_put_k is not None
        and o.wing_call is not None
        and o.wing_put is not None
    ):
        wc_i = eng.call_intrinsic(o.spot_settle, o.wing_call_k)
        wp_i = eng.put_intrinsic(o.spot_settle, o.wing_put_k)
        append_row(
            ledger,
            ts=settle_dt,
            symbol=eng.format_symbol("C", o.wing_call_k, o.basket_expiry),
            side="SELL_TO_CLOSE",
            qty=qty,
            price=wc_i,
            price_source="settlement intrinsic (max(spot-strike,0) at 12:00 UTC)",
            index=float(o.spot_settle),
            fee=0.0,
            running=running,
            note="settle wing call",
        )
        append_row(
            ledger,
            ts=settle_dt,
            symbol=eng.format_symbol("P", o.wing_put_k, o.basket_expiry),
            side="SELL_TO_CLOSE",
            qty=qty,
            price=wp_i,
            price_source="settlement intrinsic (max(strike-spot,0) at 12:00 UTC)",
            index=float(o.spot_settle),
            fee=0.0,
            running=running,
            note="settle wing put",
        )

    append_row(
        ledger,
        ts=settle_dt,
        symbol="NET",
        side="MARK",
        qty=0,
        price=net,
        price_source=(
            "engine net_no_settle x QTY_SCALE "
            f"(cache_qty={eng.BASKET_QTY_LOTS} -> live_qty={qty})"
        ),
        index=float(o.spot_settle),
        fee=float(o.entry_fees) * scale,
        running=net,
        note="CONTROL final net (fees already inside net_no_settle)",
    )

    return ForensicCycle(
        entry_date=o.entry_date,
        net=net,
        spot_move_abs=float(o.spot_move_abs),
        n_adjustments=0,
        ledger=ledger,
    )


def simulate_winner_ledger(
    o: eng.CycleObs,
    idx: eng.TradeIndex,
    times: list[int],
    closes: list[float],
    surface: Any | None,
    *,
    collect_ledger: bool,
) -> ForensicCycle:
    """
    Mirror of sweep.simulate_with_adjustments with optional full ledger.
    Diagnostic only — does not change trading rules.
    """
    cfg = WINNER_CFG
    assert cfg.trigger_pct is not None
    mode = cfg.adjustment.upper()
    allow_adj_a = mode in {"A_ONLY", "BOTH"}
    allow_adj_b = mode in {"B_ONLY", "BOTH"}
    flat_trigger = float(cfg.trigger_pct)
    adj_b_trig = sweep.adj_b_pct_from_trigger(float(cfg.trigger_pct))

    exp = o.basket_expiry
    short_role_s = sweep.short_role()
    long_role_s = sweep.long_role()
    qty = sweep.ORIGINAL_BASKET_QTY
    original_qty = sweep.ORIGINAL_BASKET_QTY

    sc_k = float(o.short_call_k)
    sp_k = float(o.short_put_k)
    sc_entry = float(o.short_call.price)
    sp_entry = float(o.short_put.price)
    sc_base = sc_entry
    sp_base = sp_entry
    wc_k = float(o.wing_call_k) if o.wing_call_k is not None else None
    wp_k = float(o.wing_put_k) if o.wing_put_k is not None else None
    wc_entry = float(o.wing_call.price) if o.wing_call is not None else None
    wp_entry = float(o.wing_put.price) if o.wing_put is not None else None

    ledger: list[LedgerRow] = []
    adj_events: list[AdjEvent] = []
    spot_e = float(o.spot_entry)
    fees = eng.option_fee(sc_entry, spot_e, qty) + eng.option_fee(sp_entry, spot_e, qty)
    if wc_entry is not None and wp_entry is not None:
        fees += eng.option_fee(wc_entry, spot_e, qty) + eng.option_fee(
            wp_entry, spot_e, qty
        )

    realized = 0.0
    adj_count = 0
    closed_early = False
    running = 0.0

    if collect_ledger:
        append_row(
            ledger,
            ts=o.entry_utc,
            symbol="HEDGE",
            side="NOT AVAILABLE",
            qty=0,
            price=0.0,
            price_source="NOT AVAILABLE — s001_income_engine / sweep exclude hedge",
            index=spot_e,
            fee=0.0,
            running=0.0,
            note="hedge legs not present on CycleObs",
        )
        fee_sc = eng.option_fee(sc_entry, spot_e, qty)
        running += sweep.premium_notional_usd(sc_entry, qty) - fee_sc
        append_row(
            ledger,
            ts=o.short_call.ts_utc,
            symbol=o.short_call.symbol,
            side="SELL",
            qty=qty,
            price=sc_entry,
            price_source=fill_source_label(o.short_call, role_hint="maker"),
            index=spot_e,
            fee=fee_sc,
            running=running,
            note="short call entry",
        )
        fee_sp = eng.option_fee(sp_entry, spot_e, qty)
        running += sweep.premium_notional_usd(sp_entry, qty) - fee_sp
        append_row(
            ledger,
            ts=o.short_put.ts_utc,
            symbol=o.short_put.symbol,
            side="SELL",
            qty=qty,
            price=sp_entry,
            price_source=fill_source_label(o.short_put, role_hint="maker"),
            index=spot_e,
            fee=fee_sp,
            running=running,
            note="short put entry",
        )
        if o.wing_call is not None and o.wing_put is not None and wc_entry and wp_entry:
            fee_wc = eng.option_fee(wc_entry, spot_e, qty)
            running -= sweep.premium_notional_usd(wc_entry, qty) + fee_wc
            append_row(
                ledger,
                ts=o.wing_call.ts_utc,
                symbol=o.wing_call.symbol,
                side="BUY",
                qty=qty,
                price=wc_entry,
                price_source=fill_source_label(o.wing_call, role_hint="taker"),
                index=spot_e,
                fee=fee_wc,
                running=running,
                note=f"wing call; wing_used_surface={o.wing_used_surface}",
            )
            fee_wp = eng.option_fee(wp_entry, spot_e, qty)
            running -= sweep.premium_notional_usd(wp_entry, qty) + fee_wp
            append_row(
                ledger,
                ts=o.wing_put.ts_utc,
                symbol=o.wing_put.symbol,
                side="BUY",
                qty=qty,
                price=wp_entry,
                price_source=fill_source_label(o.wing_put, role_hint="taker"),
                index=spot_e,
                fee=fee_wp,
                running=running,
                note=f"wing put; wing_used_surface={o.wing_used_surface}",
            )

    t0 = int(o.entry_utc.timestamp())
    settle_dt = datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC)
    t_end = int(settle_dt.timestamp())
    if t_end <= t0:
        r = sweep.simulate_none(o)
        return ForensicCycle(
            entry_date=o.entry_date,
            net=r.net,
            spot_move_abs=float(o.spot_move_abs),
            n_adjustments=0,
            ledger=ledger,
        )

    t = t0 + sweep.MONITOR_STEP_SEC
    while t < t_end and not closed_early:
        when = datetime.fromtimestamp(t, tz=UTC)
        spot = ot.spot_at(times, closes, t)
        if spot is None or spot <= 0:
            t += sweep.MONITOR_STEP_SEC
            continue

        sc_fill = print_at(
            idx, eng.format_symbol("C", sc_k, exp), when, long_role_s
        )
        sp_fill = print_at(
            idx, eng.format_symbol("P", sp_k, exp), when, long_role_s
        )
        if sc_fill is None or sp_fill is None:
            t += sweep.MONITOR_STEP_SEC
            continue
        sc_now = float(sc_fill.price)
        sp_now = float(sp_fill.price)

        wc_now = sweep.wing_premium_at(idx, exp, "call", wc_k, when) if wc_k else None
        wp_now = sweep.wing_premium_at(idx, exp, "put", wp_k, when) if wp_k else None

        net_dec = sweep.mtm_net(
            sc_entry=sc_entry,
            sp_entry=sp_entry,
            sc_now=sc_now,
            sp_now=sp_now,
            sc_k=sc_k,
            sp_k=sp_k,
            qty=qty,
            wc_entry=wc_entry,
            wp_entry=wp_entry,
            wc_now=wc_now,
            wp_now=wp_now,
            realized=realized,
            fees=fees,
        )

        call_hit = sc_base > 0 and sc_now >= sc_base * (flat_trigger / 100.0)
        put_hit = sp_base > 0 and sp_now >= sp_base * (flat_trigger / 100.0)
        action: str | None = None
        if allow_adj_a:
            if call_hit:
                action = "A:call"
            elif put_hit:
                action = "A:put"
        if action is None and allow_adj_b:
            call_pressured = sc_base > 0 and sc_now >= sc_base * 1.0
            put_pressured = sp_base > 0 and sp_now >= sp_base * 1.0
            thresh = adj_b_trig / 100.0
            call_decayed = sc_base > 0 and sc_now < sc_base * thresh
            put_decayed = sp_base > 0 and sp_now < sp_base * thresh
            if call_pressured and put_decayed:
                action = "B:put"
            elif put_pressured and call_decayed:
                action = "B:call"

        if action is None:
            t += sweep.MONITOR_STEP_SEC
            continue

        kind, leg = action.split(":")
        if adj_count >= sweep.MAX_ADJUSTMENTS_PER_BASKET:
            # force exit at marks
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
                sp_entry, sp_now, qty, is_long=False
            )
            exit_fee = eng.option_fee(sc_now, spot, qty) + eng.option_fee(
                sp_now, spot, qty
            )
            if (
                wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            if collect_ledger:
                running = realized - fees
                append_row(
                    ledger,
                    ts=when,
                    symbol="BASKET_FORCE_EXIT",
                    side="CLOSE_ALL",
                    qty=qty,
                    price=0.0,
                    price_source=(
                        "real trade print marks via nearest_print_prefer "
                        "(long_role for short exits)"
                    ),
                    index=float(spot),
                    fee=exit_fee,
                    running=running,
                    note="max_adjustments_per_basket gate",
                )
            closed_early = True
            break

        if kind == "A" and net_dec > 0:
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
                sp_entry, sp_now, qty, is_long=False
            )
            exit_fee = eng.option_fee(sc_now, spot, qty) + eng.option_fee(
                sp_now, spot, qty
            )
            if (
                wc_entry is not None
                and wp_entry is not None
                and wc_now is not None
                and wp_now is not None
            ):
                exit_pnl += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                exit_pnl += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                exit_fee += eng.option_fee(wc_now, spot, qty) + eng.option_fee(
                    wp_now, spot, qty
                )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            break

        next_n = adj_count + 1
        new_qty, close_basket = compute_decrease_step_qty(
            original_qty=original_qty,
            adjustment_number=next_n,
            decrease_pct=sweep.ADJUSTMENT_QTY_DECREASE_PCT,
        )
        if close_basket or new_qty is None:
            exit_pnl = eng.cash_pnl(sc_entry, sc_now, qty, is_long=False) + eng.cash_pnl(
                sp_entry, sp_now, qty, is_long=False
            )
            exit_fee = eng.option_fee(sc_now, spot, qty) + eng.option_fee(
                sp_now, spot, qty
            )
            realized += exit_pnl
            fees += exit_fee
            closed_early = True
            break

        new_k: float | None = None
        new_fill_px: float | None = None
        new_fill_src = "NOT AVAILABLE"
        exit_px = sc_now if leg == "call" else sp_now
        exit_fill = sc_fill if leg == "call" else sp_fill
        exit_src = fill_source_label(exit_fill, role_hint=long_role_s)

        if kind == "A":
            if leg == "call":
                target, _, _, _ = sweep.compute_adjustment_target_premium(
                    sp_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            else:
                target, _, _, _ = sweep.compute_adjustment_target_premium(
                    sc_now, [sc_base, sp_base], [sc_now, sp_now]
                )
            old_k = sc_k if leg == "call" else sp_k
            hit = sweep.find_farther_otm_by_premium(
                idx, exp, leg, old_k, float(target), when
            )
            if hit is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_k, new_fill = hit
            new_fill_px = float(new_fill.price)
            new_fill_src = fill_source_label(new_fill, role_hint=short_role_s)
        else:
            tested = "put" if leg == "call" else "call"
            p_target = sp_now if tested == "put" else sc_now
            other_k = sp_k if leg == "call" else sc_k
            chain = sweep.build_adj_b_chain(idx, exp, leg, when)
            res = select_adj_b_strike(
                leg_type=leg,
                p_target=float(p_target),
                chain=chain,
                spot=float(spot),
                other_short_strike=float(other_k),
                min_short_gap_points=0.0,
            )
            if not res.success or res.strike is None or res.premium is None:
                t += sweep.MONITOR_STEP_SEC
                continue
            new_k = float(res.strike)
            fill = print_at(
                idx,
                eng.format_symbol("C" if leg == "call" else "P", new_k, exp),
                when,
                short_role_s,
            )
            if fill is not None:
                new_fill_px = float(fill.price)
                new_fill_src = fill_source_label(fill, role_hint=short_role_s)
            else:
                new_fill_px = float(res.premium)
                new_fill_src = (
                    "adj_b.select_adj_b_strike premium from print-built chain "
                    "(fallback when preferred-role fill missing)"
                )

        assert new_k is not None and new_fill_px is not None

        theo = surface_theo(surface, when, sc_k if leg == "call" else sp_k, exp, leg)
        minutes = (t - t0) / 60.0
        exit_sym = eng.format_symbol("C" if leg == "call" else "P", sc_k if leg == "call" else sp_k, exp)
        adj_events.append(
            AdjEvent(
                entry_date=o.entry_date,
                adj_n=next_n,
                ts_utc=when,
                minutes_after_entry=minutes,
                index_entry=spot_e,
                index_at_adj=float(spot),
                exit_leg=leg,
                exit_symbol=exit_sym,
                exit_price=float(exit_px),
                exit_price_source=exit_src,
                theo_surface=theo,
            )
        )

        # Execute exit + entry
        if leg == "call":
            realized += eng.cash_pnl(sc_entry, exit_px, qty, is_long=False)
            fee_exit = eng.option_fee(exit_px, spot, qty)
            fee_entry = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_exit + fee_entry
            if collect_ledger:
                append_row(
                    ledger,
                    ts=when,
                    symbol=exit_sym,
                    side="BUY_TO_CLOSE",
                    qty=qty,
                    price=exit_px,
                    price_source=exit_src,
                    index=float(spot),
                    fee=fee_exit,
                    running=realized - fees,
                    note=f"Adj {kind} #{next_n} exit call",
                )
                ent_sym = eng.format_symbol("C", new_k, exp)
                append_row(
                    ledger,
                    ts=when,
                    symbol=ent_sym,
                    side="SELL",
                    qty=int(new_qty),
                    price=new_fill_px,
                    price_source=new_fill_src,
                    index=float(spot),
                    fee=fee_entry,
                    running=realized - fees,
                    note=f"Adj {kind} #{next_n} enter call",
                )
            sc_k = new_k
            sc_entry = new_fill_px
            sc_base = new_fill_px
            sp_base = new_fill_px
        else:
            realized += eng.cash_pnl(sp_entry, exit_px, qty, is_long=False)
            fee_exit = eng.option_fee(exit_px, spot, qty)
            fee_entry = eng.option_fee(new_fill_px, spot, int(new_qty))
            fees += fee_exit + fee_entry
            if collect_ledger:
                append_row(
                    ledger,
                    ts=when,
                    symbol=exit_sym,
                    side="BUY_TO_CLOSE",
                    qty=qty,
                    price=exit_px,
                    price_source=exit_src,
                    index=float(spot),
                    fee=fee_exit,
                    running=realized - fees,
                    note=f"Adj {kind} #{next_n} exit put",
                )
                ent_sym = eng.format_symbol("P", new_k, exp)
                append_row(
                    ledger,
                    ts=when,
                    symbol=ent_sym,
                    side="SELL",
                    qty=int(new_qty),
                    price=new_fill_px,
                    price_source=new_fill_src,
                    index=float(spot),
                    fee=fee_entry,
                    running=realized - fees,
                    note=f"Adj {kind} #{next_n} enter put",
                )
            sp_k = new_k
            sp_entry = new_fill_px
            sp_base = new_fill_px
            sc_base = new_fill_px

        # Wing resize / roll (same as sweep)
        if (
            wc_k is not None
            and wp_k is not None
            and wc_entry is not None
            and wp_entry is not None
        ):
            roll = (leg == "call" and new_k >= wc_k - 1e-9) or (
                leg == "put" and new_k <= wp_k + 1e-9
            )
            if roll and wc_now is not None and wp_now is not None:
                realized += eng.cash_pnl(wc_entry, wc_now, qty, is_long=True)
                realized += eng.cash_pnl(wp_entry, wp_now, qty, is_long=True)
                wf = eng.option_fee(wc_now, spot, qty) + eng.option_fee(wp_now, spot, qty)
                fees += wf
                if collect_ledger:
                    append_row(
                        ledger,
                        ts=when,
                        symbol=eng.format_symbol("C", wc_k, exp),
                        side="SELL_TO_CLOSE",
                        qty=qty,
                        price=wc_now,
                        price_source=(
                            "real trade print via nearest_print_prefer (long_role)"
                        ),
                        index=float(spot),
                        fee=wf / 2.0,
                        running=realized - fees,
                        note=f"wing roll exit call adj#{next_n}",
                    )
                    append_row(
                        ledger,
                        ts=when,
                        symbol=eng.format_symbol("P", wp_k, exp),
                        side="SELL_TO_CLOSE",
                        qty=qty,
                        price=wp_now,
                        price_source=(
                            "real trade print via nearest_print_prefer (long_role)"
                        ),
                        index=float(spot),
                        fee=wf / 2.0,
                        running=realized - fees,
                        note=f"wing roll exit put adj#{next_n}",
                    )
                wk = eng.pick_wing_strikes(idx, exp, sc_k, sp_k, sweep.WING_POINTS)
                if wk is not None:
                    wc_k, wp_k = wk
                    wcf = print_at(
                        idx, eng.format_symbol("C", wc_k, exp), when, long_role_s
                    )
                    wpf = print_at(
                        idx, eng.format_symbol("P", wp_k, exp), when, long_role_s
                    )
                    if wcf is not None and wpf is not None:
                        wc_entry = float(wcf.price)
                        wp_entry = float(wpf.price)
                        wf_in = eng.option_fee(wc_entry, spot, int(new_qty)) + eng.option_fee(
                            wp_entry, spot, int(new_qty)
                        )
                        fees += wf_in
                        if collect_ledger:
                            append_row(
                                ledger,
                                ts=when,
                                symbol=wcf.symbol,
                                side="BUY",
                                qty=int(new_qty),
                                price=wc_entry,
                                price_source=fill_source_label(
                                    wcf, role_hint=long_role_s
                                ),
                                index=float(spot),
                                fee=eng.option_fee(wc_entry, spot, int(new_qty)),
                                running=realized - fees,
                                note=f"wing roll enter call adj#{next_n}",
                            )
                            append_row(
                                ledger,
                                ts=when,
                                symbol=wpf.symbol,
                                side="BUY",
                                qty=int(new_qty),
                                price=wp_entry,
                                price_source=fill_source_label(
                                    wpf, role_hint=long_role_s
                                ),
                                index=float(spot),
                                fee=eng.option_fee(wp_entry, spot, int(new_qty)),
                                running=realized - fees,
                                note=f"wing roll enter put adj#{next_n}",
                            )
                    else:
                        wc_k = wp_k = None
                        wc_entry = wp_entry = None
                else:
                    wc_k = wp_k = None
                    wc_entry = wp_entry = None
            elif int(new_qty) < qty:
                closed = qty - int(new_qty)
                if wc_now is not None and wp_now is not None and closed > 0:
                    realized += eng.cash_pnl(wc_entry, wc_now, closed, is_long=True)
                    realized += eng.cash_pnl(wp_entry, wp_now, closed, is_long=True)
                    wf = eng.option_fee(wc_now, spot, closed) + eng.option_fee(
                        wp_now, spot, closed
                    )
                    fees += wf
                    if collect_ledger:
                        append_row(
                            ledger,
                            ts=when,
                            symbol=eng.format_symbol("C", wc_k, exp),
                            side="SELL_PARTIAL",
                            qty=closed,
                            price=wc_now,
                            price_source=(
                                "real trade print via nearest_print_prefer (long_role)"
                            ),
                            index=float(spot),
                            fee=eng.option_fee(wc_now, spot, closed),
                            running=realized - fees,
                            note=f"wing qty reduce call adj#{next_n}",
                        )
                        append_row(
                            ledger,
                            ts=when,
                            symbol=eng.format_symbol("P", wp_k, exp),
                            side="SELL_PARTIAL",
                            qty=closed,
                            price=wp_now,
                            price_source=(
                                "real trade print via nearest_print_prefer (long_role)"
                            ),
                            index=float(spot),
                            fee=eng.option_fee(wp_now, spot, closed),
                            running=realized - fees,
                            note=f"wing qty reduce put adj#{next_n}",
                        )

        qty = int(new_qty)
        adj_count += 1
        t += sweep.MONITOR_STEP_SEC

    if not closed_early:
        spot_s = float(o.spot_settle)
        pnl, settle_fee = sweep.settle_legs(
            spot_s,
            sc_k,
            sp_k,
            sc_entry,
            sp_entry,
            qty,
            wc_k,
            wp_k,
            wc_entry,
            wp_entry,
        )
        _ = settle_fee
        realized += pnl
        net = realized - fees
        if collect_ledger:
            sc_i = eng.call_intrinsic(spot_s, sc_k)
            sp_i = eng.put_intrinsic(spot_s, sp_k)
            append_row(
                ledger,
                ts=settle_dt,
                symbol=eng.format_symbol("C", sc_k, exp),
                side="BUY_TO_CLOSE",
                qty=qty,
                price=sc_i,
                price_source="settlement intrinsic (max(spot-strike,0) at 12:00 UTC)",
                index=spot_s,
                fee=0.0,
                running=net,
                note="final settle short call (settle fee excluded from net_no_settle)",
            )
            append_row(
                ledger,
                ts=settle_dt,
                symbol=eng.format_symbol("P", sp_k, exp),
                side="BUY_TO_CLOSE",
                qty=qty,
                price=sp_i,
                price_source="settlement intrinsic (max(strike-spot,0) at 12:00 UTC)",
                index=spot_s,
                fee=0.0,
                running=net,
                note="final settle short put",
            )
            if wc_k is not None and wp_k is not None:
                append_row(
                    ledger,
                    ts=settle_dt,
                    symbol=eng.format_symbol("C", wc_k, exp),
                    side="SELL_TO_CLOSE",
                    qty=qty,
                    price=eng.call_intrinsic(spot_s, wc_k),
                    price_source="settlement intrinsic at 12:00 UTC",
                    index=spot_s,
                    fee=0.0,
                    running=net,
                    note="final settle wing call",
                )
                append_row(
                    ledger,
                    ts=settle_dt,
                    symbol=eng.format_symbol("P", wp_k, exp),
                    side="SELL_TO_CLOSE",
                    qty=qty,
                    price=eng.put_intrinsic(spot_s, wp_k),
                    price_source="settlement intrinsic at 12:00 UTC",
                    index=spot_s,
                    fee=0.0,
                    running=net,
                    note="final settle wing put",
                )
            append_row(
                ledger,
                ts=settle_dt,
                symbol="NET",
                side="MARK",
                qty=0,
                price=net,
                price_source="realized cash PnL minus all trading fees (no settle fee)",
                index=spot_s,
                fee=fees,
                running=net,
                note=f"WINNER final net; adj_count={adj_count}",
            )
    else:
        net = realized - fees
        if collect_ledger:
            append_row(
                ledger,
                ts=datetime.fromtimestamp(t, tz=UTC),
                symbol="NET",
                side="MARK",
                qty=0,
                price=net,
                price_source="early exit net (realized - fees)",
                index="NOT AVAILABLE",
                fee=fees,
                running=net,
                note=f"WINNER early exit; adj_count={adj_count}",
            )

    return ForensicCycle(
        entry_date=o.entry_date,
        net=net,
        spot_move_abs=float(o.spot_move_abs),
        n_adjustments=adj_count,
        ledger=ledger,
        adj_events=adj_events,
    )


def decile_table(moves: list[float], pnls: list[float]) -> list[str]:
    pairs = sorted(zip(moves, pnls), key=lambda x: x[0])
    n = len(pairs)
    lines: list[str] = []
    lines.append(
        f"  {'dec':>4}  {'|move|_lo':>10}  {'|move|_hi':>10}  {'n':>5}  {'mean_pnl':>10}"
    )
    if n < 10:
        lines.append("  NOT AVAILABLE — need >=10 cycles for deciles")
        return lines
    for d in range(10):
        lo = int(d * n / 10)
        hi = int((d + 1) * n / 10)
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        m_lo, m_hi = chunk[0][0], chunk[-1][0]
        mean_p = statistics.mean([p for _, p in chunk])
        lines.append(
            f"  D{d:<3}  {m_lo:10.1f}  {m_hi:10.1f}  {len(chunk):5d}  {mean_p:10.4f}"
        )
    return lines


def fmt_ledger(rows: list[LedgerRow]) -> list[str]:
    out: list[str] = []
    out.append(
        f"{'timestamp':<28}  {'symbol':<28}  {'side':<14}  {'qty':>4}  "
        f"{'price':>10}  {'index':>10}  {'fee':>8}  {'run_pnl':>10}  PRICE_SOURCE"
    )
    out.append("-" * 160)
    for r in rows:
        idx = (
            f"{r.index_at_time:.2f}"
            if isinstance(r.index_at_time, float)
            else str(r.index_at_time)
        )
        note = f"  [{r.note}]" if r.note else ""
        out.append(
            f"{r.timestamp:<28}  {r.symbol:<28}  {r.side:<14}  {r.qty_lots:4d}  "
            f"{r.price:10.4f}  {idx:>10}  {r.fee:8.4f}  {r.running_pnl:10.4f}  "
            f"{r.price_source}{note}"
        )
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    lines: list[str] = []

    logger.info("Loading cycles...")
    all_obs, day_span = sweep.load_cycles()
    base = sweep.filter_base(all_obs, 2)
    logger.info("dte=2 base cycles: %s", len(base))
    if not base:
        sys.stderr.write("ABORT: no base cycles\n")
        return 2
    if getattr(base[0], "entry_date", None) is None:
        sys.stderr.write("ABORT: entry_date NOT AVAILABLE — stopping\n")
        return 2
    if getattr(base[0], "spot_move_abs", None) is None:
        sys.stderr.write("ABORT: spot_move_abs NOT AVAILABLE — stopping\n")
        return 2

    logger.info("Loading IV surface (optional, for theo)...")
    surface = eng.load_surface_optional()

    logger.info("Building trade index...")
    idx = eng.build_trade_index()
    times, closes = ot.load_spot_1m()

    logger.info("Simulating all WINNER + CONTROL cycles...")
    winners: list[ForensicCycle] = []
    controls: list[ForensicCycle] = []
    all_adj: list[AdjEvent] = []
    for i, o in enumerate(base):
        if (i + 1) % 50 == 0:
            logger.info("  progress %s/%s", i + 1, len(base))
        w = simulate_winner_ledger(
            o, idx, times, closes, surface, collect_ledger=False
        )
        winners.append(w)
        all_adj.extend(w.adj_events)
        c_net = float(o.net_no_settle) * sweep.QTY_SCALE
        controls.append(
            ForensicCycle(
                entry_date=o.entry_date,
                net=c_net,
                spot_move_abs=float(o.spot_move_abs),
                n_adjustments=0,
            )
        )

    # Identify special cycles
    by_date = {o.entry_date: o for o in base}
    crash_o = by_date.get(CRASH_DAY)
    worst_w = min(winners, key=lambda x: x.net)
    best_w = max(winners, key=lambda x: x.net)
    worst_o = by_date.get(worst_w.entry_date)
    best_o = by_date.get(best_w.entry_date)

    special: list[tuple[str, eng.CycleObs | None, ForensicCycle | None]] = [
        ("(a) CRASH_DAY 2026-06-02", crash_o, None),
        (f"(b) WINNER worst net={worst_w.net:.4f} date={worst_w.entry_date}", worst_o, None),
        (f"(c) WINNER best net={best_w.net:.4f} date={best_w.entry_date}", best_o, None),
    ]

    emit(lines, "S001 WINNER FORENSIC — diagnostic only (no fixes)")
    emit(lines, "=" * 100)
    emit(
        lines,
        "WINNER=dte2 B_only trig70 maker B25 wing2000 qty8 entry11:00 | "
        "CONTROL=dte2 none",
    )
    emit(lines, f"n={len(base)}  day_span={day_span}  cache={sweep.CYCLES_CACHE}")
    emit(lines, "")

    # ===== PART 1 =====
    emit(lines, "===== PART 1: GAMMA FINGERPRINT =====")
    emit(
        lines,
        "|move| = CycleObs.spot_move_abs = |settlement_index - entry_index| (points)",
    )
    w_moves = [w.spot_move_abs for w in winners]
    w_pnls = [w.net for w in winners]
    c_moves = [c.spot_move_abs for c in controls]
    c_pnls = [c.net for c in controls]

    emit(lines, "")
    emit(lines, "Correlations (P&L vs |move|):")
    emit(
        lines,
        f"  WINNER  Pearson={pearson(w_moves, w_pnls):.6f}  "
        f"Spearman={spearman(w_moves, w_pnls):.6f}",
    )
    emit(
        lines,
        f"  CONTROL Pearson={pearson(c_moves, c_pnls):.6f}  "
        f"Spearman={spearman(c_moves, c_pnls):.6f}",
    )
    emit(lines, "")
    emit(lines, "WINNER |move| deciles (mean P&L):")
    for ln in decile_table(w_moves, w_pnls):
        emit(lines, ln)
    emit(lines, "")
    emit(lines, "CONTROL |move| deciles (mean P&L):")
    for ln in decile_table(c_moves, c_pnls):
        emit(lines, ln)
    emit(lines, "")
    emit(lines, "Per-cycle P&L moments:")
    for name, pnls in (("WINNER", w_pnls), ("CONTROL", c_pnls)):
        emit(
            lines,
            f"  {name}: std={statistics.stdev(pnls):.4f}  "
            f"skew={sample_skew(pnls):.4f}  "
            f"min={min(pnls):.4f}  max={max(pnls):.4f}",
        )
    emit(lines, "")

    # ===== PART 2 =====
    emit(lines, "===== PART 2: FULL LEDGER (3 CYCLES) =====")
    emit(
        lines,
        "Hedge rows: NOT AVAILABLE (engine excludes hedge). "
        "PRICE_SOURCE is the critical column.",
    )
    emit(lines, "")

    for label, obs, _ in special:
        emit(lines, "-" * 100)
        emit(lines, label)
        if obs is None:
            emit(lines, "  CycleObs for this date: NOT AVAILABLE")
            emit(lines, "")
            continue
        logger.info("Building full ledger for %s ...", label)
        w_full = simulate_winner_ledger(
            obs, idx, times, closes, surface, collect_ledger=True
        )
        c_full = simulate_control_ledger(obs)
        emit(
            lines,
            f"  entry_date={obs.entry_date}  entry_utc={obs.entry_utc.isoformat()}  "
            f"|move|={obs.spot_move_abs:.1f}  "
            f"spot_entry={obs.spot_entry:.2f}  spot_settle={obs.spot_settle:.2f}",
        )
        emit(
            lines,
            f"  WINNER net={w_full.net:.6f}  adj={w_full.n_adjustments}  |  "
            f"CONTROL net={c_full.net:.6f}",
        )
        emit(lines, "")
        emit(lines, "  --- WINNER ledger ---")
        for ln in fmt_ledger(w_full.ledger):
            emit(lines, "  " + ln)
        emit(lines, "")
        emit(lines, "  --- CONTROL ledger ---")
        for ln in fmt_ledger(c_full.ledger):
            emit(lines, "  " + ln)
        emit(lines, "")

    # ===== PART 3 =====
    emit(lines, "===== PART 3: ADJUSTMENT TIMING =====")
    emit(lines, f"Total Adj events across WINNER cycles: {len(all_adj)}")
    if not all_adj:
        emit(lines, "  NOT AVAILABLE — no adjustments recorded")
    else:
        emit(lines, "")
        emit(lines, "Per-adjustment sample (first 40 rows; full count above):")
        emit(
            lines,
            f"  {'date':<12}  {'n':>3}  {'min_aft':>8}  {'idx_e':>9}  {'idx_a':>9}  "
            f"{'d_idx':>9}  {'leg':<5}  {'exit_px':>9}  {'theo':>10}  exit_src",
        )
        for ev in all_adj[:40]:
            theo_s = (
                f"{ev.theo_surface:.4f}"
                if isinstance(ev.theo_surface, float)
                else str(ev.theo_surface)
            )
            emit(
                lines,
                f"  {ev.entry_date.isoformat():<12}  {ev.adj_n:3d}  "
                f"{ev.minutes_after_entry:8.1f}  {ev.index_entry:9.1f}  "
                f"{ev.index_at_adj:9.1f}  "
                f"{ev.index_at_adj - ev.index_entry:9.1f}  "
                f"{ev.exit_leg:<5}  {ev.exit_price:9.4f}  {theo_s:>10}  "
                f"{ev.exit_price_source}",
            )
        if len(all_adj) > 40:
            emit(lines, f"  ... ({len(all_adj) - 40} more events not printed)")

        emit(lines, "")
        emit(lines, "Summary stats (minutes after entry):")
        mins = [e.minutes_after_entry for e in all_adj]
        emit(
            lines,
            f"  n={len(mins)}  mean={statistics.mean(mins):.1f}  "
            f"median={statistics.median(mins):.1f}  "
            f"min={min(mins):.1f}  max={max(mins):.1f}",
        )
        emit(lines, "")
        emit(lines, "Time-of-day distribution (IST hour of adjustment timestamp):")
        hours = Counter(e.ts_utc.astimezone(IST).hour for e in all_adj)
        for h in range(24):
            if hours.get(h, 0) == 0:
                continue
            emit(lines, f"  hour_IST={h:02d}:00  count={hours[h]}")

        emit(lines, "")
        emit(lines, "Exit-leg distribution:")
        for leg, cnt in sorted(Counter(e.exit_leg for e in all_adj).items()):
            emit(lines, f"  {leg}: {cnt}")

        emit(lines, "")
        emit(lines, "Exit PRICE_SOURCE distribution:")
        for src, cnt in Counter(e.exit_price_source for e in all_adj).most_common():
            emit(lines, f"  n={cnt}: {src}")

        theo_ok = sum(1 for e in all_adj if isinstance(e.theo_surface, float))
        emit(lines, "")
        emit(
            lines,
            f"Surface theo available on {theo_ok}/{len(all_adj)} adjustments "
            f"(model=iv_surface.py when supported; else NOT AVAILABLE)",
        )

    emit(lines, "")
    emit(lines, "END FORENSIC (no code changes applied to live trading logic)")

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
