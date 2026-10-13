#!/usr/bin/env python3
"""S021 Hedged Long Straddle backtest.

python backtest/strategies/s021_hedged_long_straddle/run_s021.py --month 2025-06 --max-days 3 --m 2 --tgt 400 --sl 400 --a1 50 --a2 50 --baselines
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, load_symbol_series, to_unix  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.harness.run_archive import (  # noqa: E402
    CONFIG_COLS,
    GREEK_COLS,
    LEG_COLS,
    TRADE_COLS,
    RunArchive,
    greeks_from_mark,
)
from backtest.slippage_model import load_slip_table  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    intrinsic,
    ist_date,
    t_years,
)
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402

logger = logging.getLogger("s021")

OUT_DIR = Path("backtest/strategies/s021_hedged_long_straddle/runs")
CKPT = OUT_DIR / "s021_ckpt.jsonl"
ARCHIVE_PTR = OUT_DIR / "s021_archive_ptr.json"
QTY = 1000
M_GRID = (1, 2, 3)
TSL = (200, 250, 300, 350, 400, 450, 500)
A_GRID = (30, 40, 50, 60, 70)
SIX_H = 6 * 3600
TRADE_EXTRA = (
    "m", "a1", "a2", "n_adj", "weekday", "n_atm_fallback", "state_end"
)
ADJ_COLS = (
    "trade_id", "ts_ist", "adj_no", "kind", "state_from", "state_to",
    "closed_role", "closed_strike", "closed_prem", "opened_role",
    "opened_strike", "opened_prem", "ref_call", "ref_put",
    "realized_pnl", "fee", "slip",
)
S021_TRADE_COLS = tuple(list(TRADE_COLS) + list(TRADE_EXTRA))


class S021Archive(RunArchive):
    def _ensure_headers(self) -> None:
        for name, cols in (
            ("configs.csv", CONFIG_COLS),
            ("trades.csv", S021_TRADE_COLS),
            ("legs.csv", LEG_COLS),
            ("greeks.csv", GREEK_COLS),
            ("adjustments.csv", ADJ_COLS),
        ):
            p = self.folder / name
            if p.exists() and p.stat().st_size > 0:
                continue
            with p.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=list(cols)).writeheader()

    def add_trade(
        self,
        trade_dict: dict[str, Any],
        legs: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> None:
        tid = str(trade_dict.get("trade_id", ""))
        if not tid or tid in self._seen_trades:
            return
        self._seen_trades.add(tid)
        self._append("trades.csv", S021_TRADE_COLS, trade_dict)
        for lg in legs:
            lg2 = dict(lg)
            lg2["trade_id"] = tid
            self._append("legs.csv", LEG_COLS, lg2)
        for sn in snapshots:
            sn2 = dict(sn)
            sn2["trade_id"] = tid
            self._append("greeks.csv", GREEK_COLS, sn2)
            self._n_greeks += 1

    def add_adjustments(self, trade_id: str, rows: list[dict[str, Any]]) -> None:
        for rec in rows:
            r = dict(rec)
            r["trade_id"] = trade_id
            self._append("adjustments.csv", ADJ_COLS, r)


@dataclass
class LegPos:
    role: str
    symbol: str
    strike: float
    is_call: bool
    expiry: date
    long_short: str
    mark0: float
    fill: float
    fee: float
    slip: float
    qty: int
    ref: float
    series: dict[int, float] = field(default_factory=dict)
    open: bool = True
    iv0: float = float("nan")


def cell_key(month: str, m: int, tgt: int, slv: int, a1: str, a2: str) -> str:
    return f"s021|{int(m)}|{int(tgt)}|{int(slv)}|{a1}|{a2}|{month}"


def work_cells(
    ms: list[int],
    tgts: list[int],
    sls: list[int],
    a1s: list[int],
    a2s: list[int],
    baselines: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in ms:
        for tgt in tgts:
            for slv in sls:
                for a1 in a1s:
                    for a2 in a2s:
                        out.append(
                            {
                                "m": int(m), "tgt": int(tgt), "sl": int(slv),
                                "a1": int(a1), "a2": int(a2), "mode": "adj",
                            }
                        )
    if baselines:
        for tgt in tgts:
            for slv in sls:
                out.append(
                    {
                        "m": 0, "tgt": int(tgt), "sl": int(slv),
                        "a1": 0, "a2": 0, "mode": "m0",
                    }
                )
        for m in [x for x in ms if int(x) > 0]:
            for tgt in tgts:
                for slv in sls:
                    out.append(
                        {
                            "m": int(m), "tgt": int(tgt), "sl": int(slv),
                            "a1": -1, "a2": -1, "mode": "noadj",
                        }
                    )
    return out


def a_label(w: dict[str, Any]) -> tuple[str, str]:
    if str(w["mode"]) == "m0":
        return "none", "none"
    if str(w["mode"]) == "noadj":
        return "noadj", "noadj"
    return str(int(w["a1"])), str(int(w["a2"]))


_EXP_CACHE: dict[str, list[date]] = {}


def listed_expiries(store: Any, around: date) -> list[date]:
    ym = f"{around.year:04d}-{around.month:02d}"
    hit = _EXP_CACHE.get(ym)
    if hit is not None:
        return hit
    seen: set[date] = set()
    for delta in (-1, 0, 1):
        y, mo = around.year, around.month + delta
        if mo < 1:
            y -= 1
            mo = 12
        if mo > 12:
            y += 1
            mo = 1
        probe = date(y, mo, 1)
        conn = store.conn(probe)
        if conn is None:
            continue
        try:
            rows = conn.execute("SELECT DISTINCT expiry FROM marks").fetchall()
        except Exception:
            continue
        for (e,) in rows:
            try:
                seen.add(date.fromisoformat(str(e)[:10]))
            except ValueError:
                continue
    out = sorted(seen)
    _EXP_CACHE[ym] = out
    return out


def next_listed(exps: list[date], after: date) -> date | None:
    for e in exps:
        if e > after:
            return e
    return None


def resolve_e0_e1(store: Any, t: int) -> tuple[date, date | None]:
    e0 = d.expiry_0dte_bimal(t)
    exps = listed_expiries(store, e0)
    if int(s018.expiry_unix(e0)) - int(t) < SIX_H:
        nxt = next_listed(exps, e0)
        if nxt is not None:
            e0 = nxt
    e1 = next_listed(exps, e0)
    return e0, e1


def quote_at(leg: LegPos, t: int) -> s018.Quote | None:
    return s018.series_le(leg.series, t)


def dte_of(exp: date, t: int) -> int:
    return max(0, (exp - ist_date(t)).days)


def open_long(
    store: Any, row: dict[str, Any], exp: date, t: int, spot: float, role: str
) -> LegPos | None:
    q = s018.mark_le(store, exp, str(row["symbol"]), t)
    if q is None:
        return None
    fill, _ = s018.buy_fill(q.px, dte_of(exp, t))
    fee = d.fee_gst_qty(q.px, spot, QTY)
    slip = (fill - q.px) * QTY * OPTIONS_CONTRACT_VALUE
    t_yr = t_years(t, s018.expiry_unix(exp))
    g = greeks_from_mark(q.px, spot, float(row["strike"]), t_yr, bool(row["is_call"]))
    return LegPos(
        role=role, symbol=str(row["symbol"]), strike=float(row["strike"]),
        is_call=bool(row["is_call"]), expiry=exp, long_short="long",
        mark0=q.px, fill=fill, fee=fee, slip=slip, qty=QTY, ref=q.px,
        iv0=float(g["iv"]),
    )


def open_short(
    store: Any, row: dict[str, Any], exp: date, t: int, spot: float, role: str
) -> LegPos | None:
    q = s018.mark_le(store, exp, str(row["symbol"]), t)
    if q is None:
        return None
    fill, _ = s018.sell_fill(q.px, dte_of(exp, t))
    fee = d.fee_gst_qty(q.px, spot, QTY)
    slip = (q.px - fill) * QTY * OPTIONS_CONTRACT_VALUE
    t_yr = t_years(t, s018.expiry_unix(exp))
    g = greeks_from_mark(q.px, spot, float(row["strike"]), t_yr, bool(row["is_call"]))
    return LegPos(
        role=role, symbol=str(row["symbol"]), strike=float(row["strike"]),
        is_call=bool(row["is_call"]), expiry=exp, long_short="short",
        mark0=q.px, fill=fill, fee=fee, slip=slip, qty=QTY, ref=q.px,
        iv0=float(g["iv"]),
    )


def close_leg(
    lg: LegPos, t: int, mark: float, spot: float, settle: bool
) -> tuple[float, float, float, float]:
    """Returns pnl, fee, slip, exit_fill."""
    if settle and lg.long_short == "long":
        xf = intrinsic(lg.is_call, lg.strike, spot)
        pnl = (xf - lg.fill) * lg.qty * OPTIONS_CONTRACT_VALUE
        return pnl, 0.0, 0.0, xf
    dte = dte_of(lg.expiry, t)
    if lg.long_short == "long":
        xf, _ = s018.sell_fill(mark, dte)
        fee = d.fee_gst_qty(mark, spot if spot else 1.0, lg.qty)
        slip = (mark - xf) * lg.qty * OPTIONS_CONTRACT_VALUE
        pnl = (xf - lg.fill) * lg.qty * OPTIONS_CONTRACT_VALUE
        return pnl, fee, slip, xf
    xf, _ = s018.buy_fill(mark, dte)
    fee = d.fee_gst_qty(mark, spot if spot else 1.0, lg.qty)
    slip = (xf - mark) * lg.qty * OPTIONS_CONTRACT_VALUE
    pnl = (lg.fill - xf) * lg.qty * OPTIONS_CONTRACT_VALUE
    return pnl, fee, slip, xf


def mark_pnl(lg: LegPos, mark: float) -> float:
    if lg.long_short == "long":
        return (mark - lg.fill) * lg.qty * OPTIONS_CONTRACT_VALUE
    return (lg.fill - mark) * lg.qty * OPTIONS_CONTRACT_VALUE


def pick_otm(
    rows: list[dict[str, Any]],
    is_call: bool,
    atm: float,
    target: float,
    atm_prem: float,
) -> tuple[dict[str, Any] | None, bool]:
    atm_row = d.row_strike(rows, is_call, float(atm))
    if atm_row is None:
        cand = [r for r in rows if bool(r["is_call"]) == is_call]
        if cand:
            atm_row = min(cand, key=lambda r: abs(float(r["strike"]) - float(atm)))
    if target <= 0.0 or (np.isfinite(atm_prem) and target > float(atm_prem) + 1e-9):
        return atm_row, True
    if is_call:
        otm = [r for r in rows if bool(r["is_call"]) and float(r["strike"]) > float(atm)]
    else:
        otm = [r for r in rows if (not bool(r["is_call"])) and float(r["strike"]) < float(atm)]
    if not otm:
        return atm_row, True
    best = min(
        otm,
        key=lambda r: (abs(float(r["mark"]) - float(target)), abs(float(r["strike"]) - float(atm))),
    )
    return best, False


def load_series(store: Any, lg: LegPos, t0: int, t1: int) -> None:
    lg.series = load_symbol_series(store, lg.symbol, t0, t1)


def reset_short_refs(legs: list[LegPos], t: int) -> None:
    for lg in legs:
        if not lg.open or lg.long_short != "short":
            continue
        q = quote_at(lg, t)
        if q is not None:
            lg.ref = float(q.px)


def shorts_open(legs: list[LegPos]) -> list[LegPos]:
    return [lg for lg in legs if lg.open and lg.long_short == "short"]


def pick_basket(
    store: Any,
    t: int,
    spot: float,
    m: int,
    cov: dict[str, int],
) -> tuple[list[LegPos], date, date | None, int] | None:
    e0, e1 = resolve_e0_e1(store, t)
    packed0 = s018.load_chain(store, e0, t)
    if packed0 is None:
        cov["e0_miss"] += 1
        return None
    cov["e0_ok"] += 1
    rows0, _ = packed0
    atm = d.atm_strike_of(rows0, spot)
    if atm is None:
        return None
    ks = d._strikes_sorted(rows0)
    k_call = d.nearest_listed(ks, float(atm) - 1000.0)
    k_put = d.nearest_listed(ks, float(atm) + 1000.0)
    cov["itm_need"] += 2
    if k_call is not None and abs(float(k_call) - (float(atm) - 1000.0)) < 1e-6:
        cov["itm_ok"] += 1
    if k_put is not None and abs(float(k_put) - (float(atm) + 1000.0)) < 1e-6:
        cov["itm_ok"] += 1
    c_row = d.row_strike(rows0, True, float(k_call)) if k_call is not None else None
    p_row = d.row_strike(rows0, False, float(k_put)) if k_put is not None else None
    if c_row is None or p_row is None:
        return None
    l1 = open_long(store, c_row, e0, t, spot, "L1")
    l2 = open_long(store, p_row, e0, t, spot, "L2")
    if l1 is None or l2 is None:
        return None
    legs: list[LegPos] = [l1, l2]
    n_fb = 0
    if int(m) <= 0:
        return legs, e0, e1, n_fb
    cov["e1_need"] += 1
    if e1 is None:
        cov["e1_miss"] += 1
        return None
    packed1 = s018.load_chain(store, e1, t)
    if packed1 is None:
        cov["e1_miss"] += 1
        return None
    cov["e1_ok"] += 1
    rows1, _ = packed1
    atm1 = d.atm_strike_of(rows1, spot)
    if atm1 is None:
        atm1 = float(atm)
    atm_c = d.row_strike(rows1, True, float(atm1))
    atm_p = d.row_strike(rows1, False, float(atm1))
    prem_atm_c = float(atm_c["mark"]) if atm_c is not None else float("nan")
    prem_atm_p = float(atm_p["mark"]) if atm_p is not None else float("nan")
    tgt_c = (float(l1.mark0) - 1000.0) * float(m)
    tgt_p = (float(l2.mark0) - 1000.0) * float(m)
    sc, fb_c = pick_otm(rows1, True, float(atm1), tgt_c, prem_atm_c)
    spu, fb_p = pick_otm(rows1, False, float(atm1), tgt_p, prem_atm_p)
    if fb_c:
        n_fb += 1
    if fb_p:
        n_fb += 1
    if sc is None or spu is None:
        return None
    l3 = open_short(store, sc, e1, t, spot, "L3")
    l4 = open_short(store, spu, e1, t, spot, "L4")
    if l3 is None or l4 is None:
        return None
    legs.extend([l3, l4])
    return legs, e0, e1, n_fb


def try_adjust(
    store: Any,
    legs: list[LegPos],
    t: int,
    spot: float,
    e1: date,
    state: str,
    a1: float,
    a2: float,
    cov: dict[str, int],
) -> tuple[str, dict[str, Any] | None]:
    ss = shorts_open(legs)
    if len(ss) < 2:
        return state, None
    packed = s018.load_chain(store, e1, t)
    if packed is None:
        return state, None
    rows1, _ = packed
    atm = d.atm_strike_of(rows1, spot)
    if atm is None:
        return state, None
    ratios: list[tuple[float, LegPos, float]] = []
    for lg in ss:
        q = quote_at(lg, t)
        if q is None or lg.ref <= 1e-12:
            continue
        ratios.append((float(q.px) / float(lg.ref), lg, float(q.px)))
    if not ratios:
        return state, None
    trig = float(a1) / 100.0 if state == "strangle" else float(a2) / 100.0
    hits = [x for x in ratios if x[0] <= trig]
    if not hits:
        return state, None
    hits.sort(key=lambda x: x[0])
    win_ratio, win, win_px = hits[0]
    lose = [x for x in ss if x.symbol != win.symbol]
    if not lose:
        return state, None
    lose_lg = lose[0]
    lose_q = quote_at(lose_lg, t)
    lose_px = float(lose_q.px) if lose_q is not None else float("nan")
    if state == "strangle":
        new_row = d.row_strike(rows1, win.is_call, float(lose_lg.strike))
        if new_row is None:
            cov["adj_miss"] += 1
            return "strangle", None
        role = "S_call" if win.is_call else "S_put"
        newbie = open_short(store, new_row, e1, t, spot, role)
        if newbie is None:
            cov["adj_miss"] += 1
            return "strangle", None
        pnl, fee, slip, _xf = close_leg(win, t, win_px, spot, False)
        win.open = False
        load_series(store, newbie, t, s018.expiry_unix(e1))
        legs.append(newbie)
        reset_short_refs(legs, t)
        ev = {
            "ts": t, "kind": "Adj1", "state_from": "strangle", "state_to": "straddle",
            "closed_role": win.role, "closed_strike": win.strike, "closed_prem": win_px,
            "opened_role": newbie.role, "opened_strike": newbie.strike,
            "opened_prem": newbie.mark0, "realized_pnl": pnl, "fee": fee, "slip": slip,
            "ratio": win_ratio,
        }
        return "straddle", ev
    new_row, _fb = pick_otm(rows1, lose_lg.is_call, float(atm), float(win_px), win_px)
    if new_row is None:
        cov["adj_miss"] += 1
        return "straddle", None
    role = "S_call" if lose_lg.is_call else "S_put"
    newbie = open_short(store, new_row, e1, t, spot, role)
    if newbie is None:
        cov["adj_miss"] += 1
        return "straddle", None
    pnl, fee, slip, _xf = close_leg(
        lose_lg, t, lose_px if np.isfinite(lose_px) else win_px, spot, False
    )
    lose_lg.open = False
    load_series(store, newbie, t, s018.expiry_unix(e1))
    legs.append(newbie)
    reset_short_refs(legs, t)
    ev = {
        "ts": t, "kind": "Adj2", "state_from": "straddle", "state_to": "strangle",
        "closed_role": lose_lg.role, "closed_strike": lose_lg.strike, "closed_prem": lose_px,
        "opened_role": newbie.role, "opened_strike": newbie.strike,
        "opened_prem": newbie.mark0, "realized_pnl": pnl, "fee": fee, "slip": slip,
        "ratio": win_ratio,
    }
    return "strangle", ev


def snapshot_greeks(
    legs: list[LegPos], t: int, spot: float, kind: str, gp: float
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ts_ist": s018.ist_str(t), "kind": kind, "spot": spot,
        "leg1_mark": "", "leg1_iv": "", "leg2_mark": "", "leg2_iv": "",
        "leg3_mark": "", "leg3_iv": "",
        "basket_mark_pnl": gp, "long_mark_pnl": gp,
    }
    bd = bg = bt = bv = 0.0
    nfin = 0
    opened = [lg for lg in legs if lg.open][:3]
    for k, lg in enumerate(opened):
        q = quote_at(lg, t)
        mk = float(q.px) if q is not None else float(lg.mark0)
        t_yr = t_years(t, s018.expiry_unix(lg.expiry))
        g = greeks_from_mark(mk, spot, lg.strike, t_yr, lg.is_call)
        row[f"leg{k+1}_mark"] = mk
        row[f"leg{k+1}_iv"] = g["iv"]
        sign = 1.0 if lg.long_short == "long" else -1.0
        if np.isfinite(g["delta"]):
            bd += sign * float(g["delta"])
            bg += sign * float(g["gamma"])
            bt += sign * float(g["theta"])
            bv += sign * float(g["vega"])
            nfin += 1
    row["basket_delta"] = bd if nfin else float("nan")
    row["basket_gamma"] = bg if nfin else float("nan")
    row["basket_theta"] = bt if nfin else float("nan")
    row["basket_vega"] = bv if nfin else float("nan")
    row["long_delta"] = row["basket_delta"]
    row["long_gamma"] = row["basket_gamma"]
    row["long_theta"] = row["basket_theta"]
    row["long_vega"] = row["basket_vega"]
    return row


def run_cycle(
    store: Any,
    spot_c: dict[int, float],
    t0: int,
    w: dict[str, Any],
    cov: dict[str, int],
    smoke: bool,
) -> dict[str, Any] | None:
    spot = float(spot_c.get(int(t0), 0.0))
    if spot <= 0:
        return None
    picked = pick_basket(store, t0, spot, int(w["m"]), cov)
    if picked is None:
        return None
    legs, e0, e1, n_fb = picked
    exp_ts = s018.expiry_unix(e0)
    e1_ts = s018.expiry_unix(e1) if e1 is not None else exp_ts
    for lg in legs:
        load_series(store, lg, t0, max(exp_ts, e1_ts))
    fees = sum(lg.fee for lg in legs)
    slip = sum(lg.slip for lg in legs)
    realized = 0.0
    adjs: list[dict[str, Any]] = []
    events: list[str] = []
    if smoke:
        parts = [
            f"{lg.role} {lg.long_short} {lg.expiry} k={lg.strike:.0f} "
            f"mark={lg.mark0:.2f} fill={lg.fill:.2f} iv={lg.iv0:.3f}"
            for lg in legs
        ]
        events.append("ENTRY " + " | ".join(parts))
    state = "strangle" if int(w["m"]) > 0 and str(w["mode"]) != "m0" else "naked"
    do_adj = str(w["mode"]) == "adj" and int(w["m"]) > 0
    mfe = float("-inf")
    mae = float("inf")
    mfe_ts = t0
    mae_ts = t0
    snaps: list[dict[str, Any]] = [snapshot_greeks(legs, t0, spot, "entry", 0.0)]
    last_snap = t0
    exit_reason = ""
    exit_ts = exp_ts
    gp = 0.0
    spot_x = spot

    def basket_gp(tt: int) -> tuple[float | None, float]:
        sp = float(spot_c.get(int(tt), 0.0))
        mp = 0.0
        for lg in legs:
            if not lg.open:
                continue
            q = quote_at(lg, tt)
            if q is None:
                return None, sp
            mp += mark_pnl(lg, float(q.px))
        return realized + mp, sp

    a1 = float(w["a1"]) if int(w["a1"]) > 0 else 50.0
    a2 = float(w["a2"]) if int(w["a2"]) > 0 else 50.0
    tgt = float(w["tgt"])
    slv = float(w["sl"])

    t = int(t0) + 60
    while t <= exp_ts:
        gp_now, sp = basket_gp(t)
        if gp_now is None:
            t += 60
            continue
        gp = gp_now
        spot_x = sp if sp > 0 else spot_x
        if gp > mfe:
            mfe = gp
            mfe_ts = t
        if gp < mae:
            mae = gp
            mae_ts = t
        if t - last_snap >= 15 * 60:
            snaps.append(snapshot_greeks(legs, t, spot_x, "15m", gp))
            last_snap = t
        if gp >= tgt:
            exit_reason = "TARGET"
            exit_ts = t
            break
        if gp <= -slv:
            exit_reason = "SL"
            exit_ts = t
            break
        if t == exp_ts:
            exit_reason = "EXPIRY_1730"
            exit_ts = t
            break
        if do_adj and e1 is not None:
            new_state, ev = try_adjust(store, legs, t, spot_x, e1, state, a1, a2, cov)
            if ev is not None:
                realized += float(ev["realized_pnl"])
                fees += float(ev["fee"])
                slip += float(ev["slip"])
                state = new_state
                n = len(adjs) + 1
                sc = next((x.ref for x in shorts_open(legs) if x.is_call), float("nan"))
                spu = next((x.ref for x in shorts_open(legs) if not x.is_call), float("nan"))
                rec = {
                    "adj_no": n,
                    "ts_ist": s018.ist_str(t),
                    "kind": ev["kind"],
                    "state_from": ev["state_from"],
                    "state_to": ev["state_to"],
                    "closed_role": ev["closed_role"],
                    "closed_strike": ev["closed_strike"],
                    "closed_prem": ev["closed_prem"],
                    "opened_role": ev["opened_role"],
                    "opened_strike": ev["opened_strike"],
                    "opened_prem": ev["opened_prem"],
                    "ref_call": sc,
                    "ref_put": spu,
                    "realized_pnl": ev["realized_pnl"],
                    "fee": ev["fee"],
                    "slip": ev["slip"],
                }
                adjs.append(rec)
                if smoke:
                    events.append(
                        f"{ev['kind']} {s018.ist_str(t)} close {ev['closed_role']} "
                        f"k={ev['closed_strike']:.0f} px={ev['closed_prem']:.2f} "
                        f"ratio={ev['ratio']:.3f} open {ev['opened_role']} "
                        f"k={ev['opened_strike']:.0f} px={ev['opened_prem']:.2f} "
                        f"realized={ev['realized_pnl']:.2f} {ev['state_from']}->{ev['state_to']}"
                    )
        t += 60

    snaps.append(snapshot_greeks(legs, exit_ts, spot_x, "exit", gp))
    settle = exit_reason == "EXPIRY_1730"
    exit_pxs: dict[str, float] = {}
    for lg in legs:
        if not lg.open:
            continue
        q = quote_at(lg, exit_ts)
        mk = float(q.px) if q is not None else float(lg.mark0)
        if settle and lg.long_short == "long":
            mk = intrinsic(lg.is_call, lg.strike, spot_x)
        pnl, fee, slp, xf = close_leg(lg, exit_ts, mk, spot_x, settle and lg.long_short == "long")
        realized += pnl
        fees += fee
        slip += slp
        exit_pxs[lg.role + str(id(lg))] = xf
        lg.open = False
    gross = realized
    net = gross - fees
    snaps.append(snapshot_greeks(legs, mfe_ts, float(spot_c.get(mfe_ts, spot_x)), "mfe", mfe))
    snaps.append(snapshot_greeks(legs, mae_ts, float(spot_c.get(mae_ts, spot_x)), "mae", mae))
    if smoke:
        events.append(
            f"EXIT {exit_reason} {s018.ist_str(exit_ts)} gross={gross:.2f} "
            f"fees={fees:.2f} slip={slip:.2f} net={net:.2f}"
        )
    wd = datetime.fromtimestamp(int(t0), tz=s018.UTC).astimezone(s018.IST)
    return {
        "entry_ts": t0,
        "exit_ts": exit_ts,
        "reason": exit_reason,
        "exit_reason": exit_reason,
        "gross": gross,
        "fees": fees,
        "slip": slip,
        "net": net,
        "mfe": mfe if mfe != float("-inf") else float("nan"),
        "mae": mae if mae != float("inf") else float("nan"),
        "mfe_ts": mfe_ts,
        "mae_ts": mae_ts,
        "hold_hrs": (int(exit_ts) - int(t0)) / 3600.0,
        "hrs_to_exp": (exp_ts - int(t0)) / 3600.0,
        "spot": spot,
        "spot_exit": spot_x,
        "legs": legs,
        "e0": e0.isoformat(),
        "e1": e1.isoformat() if e1 else "",
        "n_adj": len(adjs),
        "adjs": adjs,
        "events": events,
        "snaps": snaps,
        "n_fb": n_fb,
        "state": state,
        "weekday": "weekend" if int(wd.weekday()) >= 5 else "weekday",
        "exit_pxs": exit_pxs,
    }


def emit_trade(
    archive: S021Archive,
    cfg: dict[str, Any],
    r: dict[str, Any],
    a1s: str,
    a2s: str,
) -> None:
    tid = f"{cfg['key']}|{int(r['entry_ts'])}"
    legs_out: list[dict[str, Any]] = []
    for lg in list(r["legs"]):
        t_e = t_years(int(r["entry_ts"]), s018.expiry_unix(lg.expiry))
        t_x = t_years(int(r["exit_ts"]), s018.expiry_unix(lg.expiry))
        ge = greeks_from_mark(lg.mark0, float(r["spot"]), lg.strike, t_e, lg.is_call)
        xm = float("nan")
        xf = float("nan")
        q = quote_at(lg, int(r["exit_ts"]))
        if q is not None:
            xm = float(q.px)
        gx = greeks_from_mark(xm, float(r["spot_exit"]), lg.strike, t_x, lg.is_call)
        legs_out.append(
            {
                "symbol": lg.symbol,
                "strike": lg.strike,
                "type": "call" if lg.is_call else "put",
                "expiry": lg.expiry.isoformat(),
                "qty": lg.qty,
                "long_short": lg.long_short,
                "entry_mark": lg.mark0,
                "entry_fill": lg.fill,
                "exit_mark": xm,
                "exit_fill": xf,
                "fee_entry": lg.fee,
                "fee_exit": 0.0,
                "slip_entry": lg.slip,
                "slip_exit": 0.0,
                "iv_entry": lg.iv0 if np.isfinite(lg.iv0) else ge["iv"],
                "delta_entry": ge["delta"],
                "gamma_entry": ge["gamma"],
                "theta_entry": ge["theta"],
                "vega_entry": ge["vega"],
                "iv_exit": gx["iv"],
                "delta_exit": gx["delta"],
                "gamma_exit": gx["gamma"],
                "theta_exit": gx["theta"],
                "vega_exit": gx["vega"],
            }
        )
    trade = {
        "trade_id": tid,
        "month": cfg.get("month"),
        "tf": "1m",
        "variant": "S021",
        "band": cfg.get("tgt"),
        "band_mode": cfg.get("sl"),
        "arm": cfg.get("arm"),
        "n_legs": len(legs_out),
        "dte": 0,
        "side": "hedged",
        "entry_ist": s018.ist_str(int(r["entry_ts"])),
        "exit_ist": s018.ist_str(int(r["exit_ts"])),
        "exit_reason": r["exit_reason"],
        "hold_hrs": r["hold_hrs"],
        "hrs_to_exp": r["hrs_to_exp"],
        "spot_entry": r["spot"],
        "spot_exit": r["spot_exit"],
        "line_level": "",
        "vwap_dist": "",
        "gross": r["gross"],
        "brokerage": r["fees"],
        "slippage": r["slip"],
        "net": r["net"],
        "long_pnl": "",
        "short_pnl": "",
        "mfe": r["mfe"],
        "mfe_time": s018.ist_str(int(r["mfe_ts"])) if int(r.get("mfe_ts") or 0) else "",
        "mae": r["mae"],
        "mae_time": s018.ist_str(int(r["mae_ts"])) if int(r.get("mae_ts") or 0) else "",
        "m": cfg.get("m"),
        "a1": a1s,
        "a2": a2s,
        "n_adj": r["n_adj"],
        "weekday": r["weekday"],
        "n_atm_fallback": r["n_fb"],
        "state_end": r.get("state", ""),
    }
    archive.add_trade(trade, legs_out, list(r.get("snaps") or []))
    archive.add_adjustments(tid, list(r.get("adjs") or []))


def run_cell(
    store: Any,
    spot_c: dict[int, float],
    w: dict[str, Any],
    month: str,
    start_entry: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    archive: S021Archive | None,
    cov: dict[str, int],
    smoke: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    a1s, a2s = a_label(w)
    key = cell_key(month, int(w["m"]), int(w["tgt"]), int(w["sl"]), a1s, a2s)
    t0c = time.perf_counter()
    rows: list[dict[str, Any]] = []
    t = int(start_entry)
    while t < cutoff:
        if ist_date(t) > win_to:
            break
        if t not in spot_c:
            t += 60
            continue
        rec = run_cycle(store, spot_c, t, w, cov, smoke and len(rows) < 2)
        if rec is None:
            t += 60
            continue
        rec["key"] = key
        rows.append(rec)
        if archive is not None:
            emit_trade(
                archive,
                {
                    "key": key, "month": month, "tgt": w["tgt"], "sl": w["sl"],
                    "m": w["m"], "arm": f"m{w['m']}_{w['mode']}_{a1s}_{a2s}",
                },
                rec,
                a1s,
                a2s,
            )
        t = int(rec["exit_ts"]) + 60
    elapsed = time.perf_counter() - t0c
    nets = [float(r["net"]) for r in rows]
    fees = [float(r["fees"]) for r in rows]
    slips = [float(r["slip"]) for r in rows]
    nadj = [int(r["n_adj"]) for r in rows]
    stt = {
        "key": key,
        "month": month,
        "m": int(w["m"]),
        "tgt": int(w["tgt"]),
        "sl": int(w["sl"]),
        "a1": a1s,
        "a2": a2s,
        "mode": w["mode"],
        "n": len(rows),
        "mean": float(np.mean(nets)) if nets else float("nan"),
        "month_total": float(np.sum(nets)) if nets else 0.0,
        "win": 100.0 * sum(1 for x in nets if x > 0) / len(nets) if nets else float("nan"),
        "maxdd": s020.max_dd(nets) if nets else 0.0,
        "worst": float(min(nets)) if nets else float("nan"),
        "avg_adj": float(np.mean(nadj)) if nadj else 0.0,
        "fee": float(np.mean(fees)) if fees else float("nan"),
        "slip": float(np.mean(slips)) if slips else float("nan"),
        "gross": float(np.mean([r["gross"] for r in rows])) if rows else float("nan"),
        "exits": {},
        "cell_sec": elapsed,
    }
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[str(r["reason"])] = reasons.get(str(r["reason"]), 0) + 1
    stt["exits"] = reasons
    return stt, rows, elapsed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--m", type=int, default=-1)
    ap.add_argument("--tgt", type=int, default=-1)
    ap.add_argument("--sl", type=int, default=-1)
    ap.add_argument("--a1", type=int, default=-1)
    ap.add_argument("--a2", type=int, default=-1)
    ap.add_argument("--baselines", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--cache-gb", type=float, default=1.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("S021 Hedged Long Straddle", flush=True)
    print("fees_sim: no expiry/settlement fee -> settlement_fee_assumed_0", flush=True)
    load_slip_table()
    reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh and CKPT.exists():
        CKPT.unlink()
        print(f"fresh: dropped {CKPT.name}", flush=True)

    month = str(args.month)
    win_from, win_to = d.month_bounds(month)
    max_days = int(args.max_days or 0)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    if max_days:
        cutoff = start_ts + max_days * 86400
        win_to = min(win_to, ist_date(cutoff - 1))
    start_entry = to_unix(ist_dt(win_from, 18, 0))
    if start_entry >= cutoff:
        print("start_entry after cutoff", flush=True)
        sys.exit(2)

    ms = list(M_GRID) if int(args.m) < 0 else [int(args.m)]
    tgts = list(TSL) if int(args.tgt) < 0 else [int(args.tgt)]
    sls = list(TSL) if int(args.sl) < 0 else [int(args.sl)]
    a1s = list(A_GRID) if int(args.a1) < 0 else [int(args.a1)]
    a2s = list(A_GRID) if int(args.a2) < 0 else [int(args.a2)]
    baselines = bool(args.baselines) or (int(args.m) < 0)
    work = work_cells(ms, tgts, sls, a1s, a2s, baselines)
    smoke = bool(max_days)
    print(f"cells={len(work)} month={month} entry0={s018.ist_str(start_entry)}", flush=True)

    spot = s018.load_spot_1m(args.csv)
    ts, _o, _h, _l, c, _v = s020.bars_1m_vol(spot)
    lo = start_ts - 86400
    hi = cutoff + 14 * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, c = ts[sel], c[sel]
    spot_c = {int(t): float(x) for t, x in zip(ts, c)}
    inner = MarksStore()
    store = d.MonthGuardStore(inner, win_from - timedelta(days=1), win_to + timedelta(days=14))
    done = {} if args.fresh else s020.load_ckpt(CKPT)
    archive = S021Archive(
        "s021",
        "smoke" if smoke else "grid",
        args,
        month=month,
        prereg_note="S021 hedged long straddle; settlement_fee_assumed_0",
    )
    meta_p = archive.folder / "run_meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["settlement_fee_assumed_0"] = True
    meta["settlement_fee"] = "fees_sim has no expiry/settlement fee; assumed 0"
    meta_p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    ARCHIVE_PTR.write_text(str(archive.folder), encoding="utf-8")
    print(f"archive folder={archive.folder}", flush=True)

    cov = {
        "e0_ok": 0, "e0_miss": 0, "e1_need": 0, "e1_ok": 0, "e1_miss": 0,
        "itm_need": 0, "itm_ok": 0, "adj_miss": 0,
    }
    t_all = time.perf_counter()
    cell_secs: list[float] = []
    sample_rows: list[dict[str, Any]] = []
    n_work = len(work)
    for i, w in enumerate(work, start=1):
        a1s_, a2s_ = a_label(w)
        key = cell_key(month, int(w["m"]), int(w["tgt"]), int(w["sl"]), a1s_, a2s_)
        if key in done:
            print(f"done SKIP {key}", flush=True)
            continue
        print(f"[{i}/{n_work}] {key}", flush=True)
        stt, rows, elapsed = run_cell(
            store, spot_c, w, month, start_entry, cutoff, win_from, win_to,
            archive, cov, smoke,
        )
        cell_secs.append(elapsed)
        if smoke and not sample_rows:
            sample_rows = rows
        s020.append_ckpt(CKPT, stt)
        done[key] = stt
        if archive is not None:
            archive.add_config_result(
                {
                    "month": month, "tf": "1m", "variant": "S021",
                    "band": w["tgt"], "band_mode": w["sl"],
                    "arm": f"m{w['m']}_{w['mode']}", "n_legs": 2 if int(w["m"]) == 0 else 4,
                    "dte": 0, "tgt": w["tgt"], "sl": w["sl"],
                    "n": stt["n"], "mean": stt["mean"], "gross": stt["gross"],
                    "brokerage": stt["fee"], "slippage": stt["slip"],
                    "win": stt["win"], "C2": "-", "C3": "-",
                    "n_sig": stt["n"], "n_stale": 0, "avg_net_delta": "",
                    "key": key,
                }
            )
        print(
            f"  n={stt['n']} mean={s020._fnum(stt['mean'], 2)} "
            f"total={s020._fnum(stt['month_total'], 2)} adj={stt['avg_adj']:.2f} "
            f"sec={elapsed:.1f}",
            flush=True,
        )
        s018._CHAIN.clear()

    e1_need = max(1, int(cov["e1_need"]))
    itm_need = max(1, int(cov["itm_need"]))
    e1_miss_pct = 100.0 * float(cov["e1_miss"]) / float(e1_need)
    itm_ok_pct = 100.0 * float(cov["itm_ok"]) / float(itm_need)
    mean_cell = float(np.mean(cell_secs)) if cell_secs else float("nan")
    proj_h = (mean_cell * (3675 + 196) / 3600.0) if np.isfinite(mean_cell) else float("nan")

    if smoke and sample_rows:
        print("=== first 2 cycles ===", flush=True)
        for i, rec in enumerate(sample_rows[:2], start=1):
            print(f"-- cycle {i} --", flush=True)
            for line in rec.get("events") or []:
                print("  " + line, flush=True)
            if rec.get("adjs"):
                a0 = rec["adjs"][0]
                print(
                    f"  MANUAL Adj1/2 check: {a0.get('kind')} close px vs open; "
                    f"see ratio in event line",
                    flush=True,
                )

    report = [
        f"S021 month={month} {win_from}..{win_to}",
        f"cells_run={len(done)} work={len(work)}",
        f"settlement_fee_assumed_0=True (fees_sim has no expiry fee)",
        (
            f"coverage E1 need={cov['e1_need']} ok={cov['e1_ok']} "
            f"miss={cov['e1_miss']} ({e1_miss_pct:.1f}%)"
        ),
        (
            f"coverage ITM exact ATM±1000 ok={cov['itm_ok']}/{cov['itm_need']} "
            f"({itm_ok_pct:.1f}%)"
        ),
        f"cell_runtime_s={mean_cell:.2f} projected_3871_hours={proj_h:.1f}",
    ]
    for k, st in done.items():
        report.append(
            f"{k} n={st.get('n')} mean={s020._fnum(st.get('mean'), 2)} "
            f"total={s020._fnum(st.get('month_total'), 2)} "
            f"win={s020._fnum(st.get('win'), 1)} maxDD={s020._fnum(st.get('maxdd'), 1)} "
            f"worst={s020._fnum(st.get('worst'), 2)} avg_adj={st.get('avg_adj')} "
            f"brokerage/t={s020._fnum(st.get('fee'), 2)} slip/t={s020._fnum(st.get('slip'), 2)} "
            f"exits={st.get('exits')}"
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    txtp = OUT_DIR / f"s021_{month}_{stamp}.txt"
    txtp.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report), flush=True)
    print(f"wrote {txtp} elapsed={time.perf_counter() - t_all:.0f}s", flush=True)
    archive.finalize()
    store.close()


if __name__ == "__main__":
    main()
