#!/usr/bin/env python3
"""S021 Hedged Long Straddle backtest (mark cache + cycle memo).

python backtest/strategies/s021_hedged_long_straddle/run_s021.py --month 2025-06 --max-days 3 --m 2 --tgt 400 --sl 400 --a1 50 --a2 50 --baselines
python backtest/strategies/s021_hedged_long_straddle/run_s021.py --month 2025-06 --stage1
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import defaultdict
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
from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
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

_BOOK: "MarkBook | None" = None
_CHAIN_S021: dict[tuple[str, int], Any] = {}
_MEMO: dict[tuple[Any, ...], Any] = {}
_MEMO_HIT = 0
_MEMO_MISS = 0


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
    opened_ts: int = 0
    closed_ts: int = 0


class MarkBook:
    """Minute-indexed mark arrays for the run window (no look-ahead)."""

    def __init__(self, t_lo: int, t_hi: int) -> None:
        self.t0 = (int(t_lo) // 60) * 60
        self.t1 = (int(t_hi) // 60) * 60
        self.n = max(1, (self.t1 - self.t0) // 60 + 1)
        self.arr: dict[str, np.ndarray] = {}
        self.hits = 0
        self.misses = 0

    def ensure(self, store: Any, symbol: str, expiry: date) -> np.ndarray:
        hit = self.arr.get(symbol)
        if hit is not None:
            return hit
        a = np.full(self.n, np.nan, dtype=np.float64)
        conn = store.conn(expiry)
        if conn is not None:
            rows = conn.execute(
                """
                SELECT ts, close FROM marks
                WHERE symbol=? AND ts BETWEEN ? AND ?
                  AND close IS NOT NULL AND close>0
                """,
                (symbol, self.t0 - s018.STALE_SEC, self.t1 + 60),
            ).fetchall()
            for ts, c in rows:
                i = (int(ts) // 60 * 60 - self.t0) // 60
                if 0 <= i < self.n:
                    a[i] = float(c)
        self.arr[symbol] = a
        return a

    def quote(self, symbol: str, t: int) -> s018.Quote | None:
        a = self.arr.get(symbol)
        if a is None:
            self.misses += 1
            return None
        minute = (int(t) // 60) * 60
        i = (minute - self.t0) // 60
        if 0 <= i < self.n and np.isfinite(a[i]):
            self.hits += 1
            return s018.Quote(ts=minute, px=float(a[i]), src="real")
        for age in range(60, s018.STALE_SEC + 1, 60):
            j = i - age // 60
            if 0 <= j < self.n and np.isfinite(a[j]):
                self.hits += 1
                return s018.Quote(ts=minute - age, px=float(a[j]), src="stale")
        self.misses += 1
        return None


@dataclass
class PathSim:
    t0: int
    exp_ts: int
    e0: date
    e1: date | None
    legs: list[LegPos]
    path_ts: list[int]
    path_gp: list[float]
    path_real: list[float]
    path_fees: list[float]
    path_slip: list[float]
    path_spot: list[float]
    path_nadj: list[int]
    path_state: list[str]
    adjs: list[dict[str, Any]]
    n_fb: int
    entry_event: str
    spot0: float
    snaps_entry: dict[str, Any]


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


def stage1_cells() -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []

    def add(w: dict[str, Any]) -> None:
        k = (w["m"], w["tgt"], w["sl"], w["a1"], w["a2"], w["mode"])
        if k in seen:
            return
        seen.add(k)
        out.append(w)

    for m in M_GRID:
        for a1 in A_GRID:
            for a2 in A_GRID:
                add(
                    {
                        "m": int(m), "tgt": 400, "sl": 400,
                        "a1": int(a1), "a2": int(a2), "mode": "adj",
                    }
                )
    for m in M_GRID:
        for tgt in TSL:
            for slv in TSL:
                add(
                    {
                        "m": int(m), "tgt": int(tgt), "sl": int(slv),
                        "a1": 50, "a2": 50, "mode": "adj",
                    }
                )
    for tgt in TSL:
        for slv in TSL:
            add(
                {
                    "m": 0, "tgt": int(tgt), "sl": int(slv),
                    "a1": 0, "a2": 0, "mode": "m0",
                }
            )
    for m in M_GRID:
        for tgt in TSL:
            for slv in TSL:
                add(
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


def load_chain_cached(store: Any, exp: date, t: int) -> Any:
    minute = (int(t) // 60) * 60
    key = (exp.isoformat(), minute)
    if key in _CHAIN_S021:
        return _CHAIN_S021[key]
    packed = s018.load_chain(store, exp, t)
    _CHAIN_S021[key] = packed
    return packed


def quote_at(leg: LegPos, t: int) -> s018.Quote | None:
    if _BOOK is not None:
        return _BOOK.quote(leg.symbol, t)
    return s018.series_le(leg.series, t)


def dte_of(exp: date, t: int) -> int:
    return max(0, (exp - ist_date(t)).days)


def open_long(
    store: Any, row: dict[str, Any], exp: date, t: int, spot: float, role: str
) -> LegPos | None:
    if _BOOK is not None:
        _BOOK.ensure(store, str(row["symbol"]), exp)
        q = _BOOK.quote(str(row["symbol"]), t)
    else:
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
        iv0=float(g["iv"]), opened_ts=int(t),
    )


def open_short(
    store: Any, row: dict[str, Any], exp: date, t: int, spot: float, role: str
) -> LegPos | None:
    if _BOOK is not None:
        _BOOK.ensure(store, str(row["symbol"]), exp)
        q = _BOOK.quote(str(row["symbol"]), t)
    else:
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
        iv0=float(g["iv"]), opened_ts=int(t),
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


def reset_short_refs(legs: list[LegPos], t: int) -> None:
    for lg in legs:
        if lg.closed_ts > 0 or lg.long_short != "short":
            continue
        if lg.opened_ts > t:
            continue
        q = quote_at(lg, t)
        if q is not None:
            lg.ref = float(q.px)


def shorts_live(legs: list[LegPos], t: int) -> list[LegPos]:
    out: list[LegPos] = []
    for lg in legs:
        if lg.long_short != "short":
            continue
        if lg.opened_ts > t:
            continue
        if lg.closed_ts > 0 and lg.closed_ts <= t:
            continue
        out.append(lg)
    return out


def live_at(lg: LegPos, t: int) -> bool:
    if lg.opened_ts > t:
        return False
    if lg.closed_ts > 0 and lg.closed_ts <= t:
        return False
    return True


def pick_basket(
    store: Any,
    t: int,
    spot: float,
    m: int,
    cov: dict[str, int],
) -> tuple[list[LegPos], date, date | None, int] | None:
    e0, e1 = resolve_e0_e1(store, t)
    packed0 = load_chain_cached(store, e0, t)
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
    packed1 = load_chain_cached(store, e1, t)
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
    ss = [lg for lg in legs if lg.long_short == "short" and lg.closed_ts == 0]
    if len(ss) < 2:
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
    packed = load_chain_cached(store, e1, t)
    if packed is None:
        return state, None
    rows1, _ = packed
    atm = d.atm_strike_of(rows1, spot)
    if atm is None:
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
        win.closed_ts = int(t)
        win.open = False
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
    lose_lg.closed_ts = int(t)
    lose_lg.open = False
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
    opened = [lg for lg in legs if live_at(lg, t) or (kind == "entry" and lg.opened_ts == t)][:3]
    if kind == "entry":
        opened = [lg for lg in legs if lg.opened_ts == t][:3]
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


def _basket_gp(legs: list[LegPos], realized: float, tt: int) -> float | None:
    mp = 0.0
    for lg in legs:
        if lg.closed_ts > 0:
            continue
        q = quote_at(lg, tt)
        if q is None:
            return None
        mp += mark_pnl(lg, float(q.px))
    return realized + mp


def simulate_hold(
    store: Any,
    spot_c: dict[int, float],
    t0: int,
    w: dict[str, Any],
    cov: dict[str, int],
) -> PathSim | None:
    spot = float(spot_c.get(int(t0), 0.0))
    if spot <= 0:
        return None
    picked = pick_basket(store, t0, spot, int(w["m"]), cov)
    if picked is None:
        return None
    legs, e0, e1, n_fb = picked
    exp_ts = s018.expiry_unix(e0)
    parts = [
        f"{lg.role} {lg.long_short} {lg.expiry} k={lg.strike:.0f} "
        f"mark={lg.mark0:.2f} fill={lg.fill:.2f} iv={lg.iv0:.3f}"
        for lg in legs
    ]
    entry_event = "ENTRY " + " | ".join(parts)
    state = "strangle" if int(w["m"]) > 0 and str(w["mode"]) != "m0" else "naked"
    do_adj = str(w["mode"]) == "adj" and int(w["m"]) > 0
    fees = sum(lg.fee for lg in legs)
    slip = sum(lg.slip for lg in legs)
    realized = 0.0
    adjs: list[dict[str, Any]] = []
    path_ts: list[int] = []
    path_gp: list[float] = []
    path_real: list[float] = []
    path_fees: list[float] = []
    path_slip: list[float] = []
    path_spot: list[float] = []
    path_nadj: list[int] = []
    path_state: list[str] = []
    a1 = float(w["a1"]) if int(w["a1"]) > 0 else 50.0
    a2 = float(w["a2"]) if int(w["a2"]) > 0 else 50.0
    snaps_entry = snapshot_greeks(legs, t0, spot, "entry", 0.0)
    t = int(t0) + 60
    spot_x = spot
    while t <= exp_ts:
        gp_now = _basket_gp(legs, realized, t)
        if gp_now is None:
            t += 60
            continue
        sp = float(spot_c.get(int(t), 0.0))
        if sp > 0:
            spot_x = sp
        path_ts.append(t)
        path_gp.append(float(gp_now))
        path_real.append(float(realized))
        path_fees.append(float(fees))
        path_slip.append(float(slip))
        path_spot.append(float(spot_x))
        path_nadj.append(len(adjs))
        path_state.append(state)
        if t == exp_ts:
            break
        if do_adj and e1 is not None:
            new_state, ev = try_adjust(store, legs, t, spot_x, e1, state, a1, a2, cov)
            if ev is not None:
                realized += float(ev["realized_pnl"])
                fees += float(ev["fee"])
                slip += float(ev["slip"])
                state = new_state
                n = len(adjs) + 1
                sc = next(
                    (x.ref for x in legs
                     if x.long_short == "short" and x.closed_ts == 0 and x.is_call),
                    float("nan"),
                )
                spu = next(
                    (x.ref for x in legs
                     if x.long_short == "short" and x.closed_ts == 0 and not x.is_call),
                    float("nan"),
                )
                rec = {
                    "adj_no": n,
                    "ts": int(t),
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
                    "ratio": ev["ratio"],
                    "event_line": (
                        f"{ev['kind']} {s018.ist_str(t)} close {ev['closed_role']} "
                        f"k={ev['closed_strike']:.0f} px={ev['closed_prem']:.2f} "
                        f"ratio={ev['ratio']:.3f} open {ev['opened_role']} "
                        f"k={ev['opened_strike']:.0f} px={ev['opened_prem']:.2f} "
                        f"realized={ev['realized_pnl']:.2f} {ev['state_from']}->{ev['state_to']}"
                    ),
                }
                adjs.append(rec)
        t += 60
    return PathSim(
        t0=int(t0), exp_ts=int(exp_ts), e0=e0, e1=e1, legs=legs,
        path_ts=path_ts, path_gp=path_gp, path_real=path_real,
        path_fees=path_fees, path_slip=path_slip, path_spot=path_spot,
        path_nadj=path_nadj, path_state=path_state, adjs=adjs, n_fb=n_fb,
        entry_event=entry_event, spot0=spot, snaps_entry=snaps_entry,
    )


def _open_at_exit(lg: LegPos, t0: int, exit_ts: int) -> bool:
    if lg.opened_ts > t0 and lg.opened_ts >= exit_ts:
        return False
    if lg.closed_ts > 0 and lg.closed_ts < exit_ts:
        return False
    return True


def apply_tsl(sim: PathSim, w: dict[str, Any], smoke: bool) -> dict[str, Any]:
    tgt = float(w["tgt"])
    slv = float(w["sl"])
    mfe = float("-inf")
    mae = float("inf")
    mfe_ts = sim.t0
    mae_ts = sim.t0
    exit_reason = ""
    exit_i = -1
    for i, (ts, gp) in enumerate(zip(sim.path_ts, sim.path_gp)):
        if gp > mfe:
            mfe = gp
            mfe_ts = ts
        if gp < mae:
            mae = gp
            mae_ts = ts
        if gp >= tgt:
            exit_reason = "TARGET"
            exit_i = i
            break
        if gp <= -slv:
            exit_reason = "SL"
            exit_i = i
            break
        if ts == sim.exp_ts:
            exit_reason = "EXPIRY_1730"
            exit_i = i
            break
    if exit_i < 0:
        if sim.path_ts:
            exit_i = len(sim.path_ts) - 1
            if sim.path_ts[exit_i] == sim.exp_ts:
                exit_reason = "EXPIRY_1730"
        else:
            exit_reason = ""
    if sim.path_ts and exit_i >= 0:
        exit_ts = int(sim.path_ts[exit_i])
        gp = float(sim.path_gp[exit_i])
        realized = float(sim.path_real[exit_i])
        fees = float(sim.path_fees[exit_i])
        slip = float(sim.path_slip[exit_i])
        spot_x = float(sim.path_spot[exit_i])
        state = str(sim.path_state[exit_i])
        n_adj = int(sim.path_nadj[exit_i])
    else:
        exit_ts = int(sim.exp_ts)
        gp = 0.0
        realized = 0.0
        fees = sum(lg.fee for lg in sim.legs if lg.opened_ts == sim.t0)
        slip = sum(lg.slip for lg in sim.legs if lg.opened_ts == sim.t0)
        spot_x = float(sim.spot0)
        state = "strangle" if int(w["m"]) > 0 and str(w["mode"]) != "m0" else "naked"
        n_adj = 0
    settle = exit_reason == "EXPIRY_1730"
    exit_pxs: dict[str, float] = {}
    for lg in sim.legs:
        if not _open_at_exit(lg, sim.t0, exit_ts):
            continue
        q = quote_at(lg, exit_ts)
        mk = float(q.px) if q is not None else float(lg.mark0)
        if settle and lg.long_short == "long":
            mk = intrinsic(lg.is_call, lg.strike, spot_x)
        pnl, fee, slp, xf = close_leg(
            lg, exit_ts, mk, spot_x, settle and lg.long_short == "long"
        )
        realized += pnl
        fees += fee
        slip += slp
        exit_pxs[lg.role + str(id(lg))] = xf
    gross = realized
    net = gross - fees
    adjs = [a for a in sim.adjs if int(a["ts"]) < exit_ts]
    n_adj = len(adjs)
    events: list[str] = []
    if smoke:
        events.append(sim.entry_event)
        for a in adjs:
            events.append(str(a.get("event_line", "")))
        events.append(
            f"EXIT {exit_reason} {s018.ist_str(exit_ts)} gross={gross:.2f} "
            f"fees={fees:.2f} slip={slip:.2f} net={net:.2f}"
        )
    legs_view = [lg for lg in sim.legs if lg.opened_ts < exit_ts or lg.opened_ts == sim.t0]
    snaps = [sim.snaps_entry]
    snaps.append(snapshot_greeks(legs_view, exit_ts, spot_x, "exit", gp))
    snaps.append(
        snapshot_greeks(legs_view, mfe_ts, float(sim.spot0 if mfe_ts == sim.t0 else spot_x), "mfe", mfe)
    )
    snaps.append(snapshot_greeks(legs_view, mae_ts, spot_x, "mae", mae))
    wd = datetime.fromtimestamp(int(sim.t0), tz=s018.UTC).astimezone(s018.IST)
    return {
        "entry_ts": sim.t0,
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
        "hold_hrs": (int(exit_ts) - int(sim.t0)) / 3600.0,
        "hrs_to_exp": (sim.exp_ts - int(sim.t0)) / 3600.0,
        "spot": sim.spot0,
        "spot_exit": spot_x,
        "legs": legs_view,
        "e0": sim.e0.isoformat(),
        "e1": sim.e1.isoformat() if sim.e1 else "",
        "n_adj": n_adj,
        "adjs": adjs,
        "events": events,
        "snaps": snaps,
        "n_fb": sim.n_fb,
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


def _summarize(
    key: str, month: str, w: dict[str, Any], a1s: str, a2s: str,
    rows: list[dict[str, Any]], elapsed: float,
) -> dict[str, Any]:
    nets = [float(r["net"]) for r in rows]
    fees = [float(r["fees"]) for r in rows]
    slips = [float(r["slip"]) for r in rows]
    nadj = [int(r["n_adj"]) for r in rows]
    reasons: dict[str, int] = {}
    for r in rows:
        reasons[str(r["reason"])] = reasons.get(str(r["reason"]), 0) + 1
    return {
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
        "exits": reasons,
        "cell_sec": elapsed,
        "nets": [float(r["net"]) for r in rows],
        "entry_ts": [int(r["entry_ts"]) for r in rows],
    }


def run_group(
    store: Any,
    spot_c: dict[int, float],
    ws: list[dict[str, Any]],
    month: str,
    start_entry: int,
    cutoff: int,
    win_to: date,
    archive: S021Archive | None,
    cov: dict[str, int],
    smoke: bool,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]], float]:
    global _MEMO_HIT, _MEMO_MISS
    t0c = time.perf_counter()
    n = len(ws)
    rows_per: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    pending: dict[int, list[int]] = {int(start_entry): list(range(n))}
    while pending:
        t = min(pending)
        idxs = pending.pop(t)
        if t >= cutoff or ist_date(t) > win_to:
            continue
        if t not in spot_c:
            pending.setdefault(t + 60, []).extend(idxs)
            continue
        w0 = ws[idxs[0]]
        mk = (int(t), int(w0["m"]), str(w0["mode"]), int(w0["a1"]), int(w0["a2"]))
        if mk in _MEMO:
            _MEMO_HIT += len(idxs)
            sim = _MEMO[mk]
        else:
            _MEMO_MISS += 1
            _MEMO_HIT += max(0, len(idxs) - 1)
            sim = simulate_hold(store, spot_c, t, w0, cov)
            _MEMO[mk] = sim
        if sim is None:
            pending.setdefault(t + 60, []).extend(idxs)
            continue
        for i in idxs:
            w = ws[i]
            a1s, a2s = a_label(w)
            rec = apply_tsl(sim, w, smoke and len(rows_per[i]) < 2)
            key = cell_key(month, int(w["m"]), int(w["tgt"]), int(w["sl"]), a1s, a2s)
            rec["key"] = key
            rows_per[i].append(rec)
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
            nxt = int(rec["exit_ts"]) + 60
            if nxt < cutoff:
                pending.setdefault(nxt, []).append(i)
    elapsed = time.perf_counter() - t0c
    stts: list[dict[str, Any]] = []
    share = elapsed / max(1, n)
    for i, w in enumerate(ws):
        a1s, a2s = a_label(w)
        key = cell_key(month, int(w["m"]), int(w["tgt"]), int(w["sl"]), a1s, a2s)
        stts.append(_summarize(key, month, w, a1s, a2s, rows_per[i], share))
    return stts, rows_per, elapsed


def main() -> None:
    global _BOOK, _CHAIN_S021, _MEMO, _MEMO_HIT, _MEMO_MISS
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
    ap.add_argument("--stage1", action="store_true")
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

    if args.stage1:
        work = stage1_cells()
        baselines = True
    else:
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
    _BOOK = MarkBook(start_ts - 86400, cutoff + 14 * 86400)
    _CHAIN_S021 = {}
    _MEMO = {}
    _MEMO_HIT = 0
    _MEMO_MISS = 0
    done = {} if args.fresh else s020.load_ckpt(CKPT)
    archive = S021Archive(
        "s021",
        "smoke" if smoke else ("stage1" if args.stage1 else "grid"),
        args,
        month=month,
        prereg_note="S021 hedged long straddle; settlement_fee_assumed_0; mark_cache+cycle_memo",
    )
    meta_p = archive.folder / "run_meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["settlement_fee_assumed_0"] = True
    meta["settlement_fee"] = "fees_sim has no expiry/settlement fee; assumed 0"
    meta["mark_cache"] = True
    meta["cycle_memo"] = True
    meta_p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    ARCHIVE_PTR.write_text(str(archive.folder), encoding="utf-8")
    print(f"archive folder={archive.folder}", flush=True)

    cov = {
        "e0_ok": 0, "e0_miss": 0, "e1_need": 0, "e1_ok": 0, "e1_miss": 0,
        "itm_need": 0, "itm_ok": 0, "adj_miss": 0,
    }
    t_all = time.perf_counter()
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for w in work:
        a1s_, a2s_ = a_label(w)
        key = cell_key(month, int(w["m"]), int(w["tgt"]), int(w["sl"]), a1s_, a2s_)
        if key in done:
            print(f"done SKIP {key}", flush=True)
            continue
        groups[(int(w["m"]), str(w["mode"]), int(w["a1"]), int(w["a2"]))].append(w)

    cell_secs: list[float] = []
    sample_rows: list[dict[str, Any]] = []
    n_work = len(work)
    gi = 0
    for _gk, ws in groups.items():
        gi += 1
        print(
            f"[group {gi}/{len(groups)} n_tsl={len(ws)} m={ws[0]['m']} "
            f"mode={ws[0]['mode']} a1={ws[0]['a1']} a2={ws[0]['a2']}]",
            flush=True,
        )
        stts, rows_per, elapsed = run_group(
            store, spot_c, ws, month, start_entry, cutoff, win_to,
            archive, cov, smoke,
        )
        cell_secs.append(elapsed)
        for stt, rows, w in zip(stts, rows_per, ws):
            if smoke and not sample_rows and str(w["mode"]) == "adj":
                sample_rows = rows
            elif smoke and not sample_rows:
                sample_rows = rows
            s020.append_ckpt(CKPT, {k: v for k, v in stt.items() if k not in ("nets", "entry_ts")})
            done[stt["key"]] = stt
            if archive is not None:
                archive.add_config_result(
                    {
                        "month": month, "tf": "1m", "variant": "S021",
                        "band": w["tgt"], "band_mode": w["sl"],
                        "arm": f"m{w['m']}_{w['mode']}",
                        "n_legs": 2 if int(w["m"]) == 0 else 4,
                        "dte": 0, "tgt": w["tgt"], "sl": w["sl"],
                        "n": stt["n"], "mean": stt["mean"], "gross": stt["gross"],
                        "brokerage": stt["fee"], "slippage": stt["slip"],
                        "win": stt["win"], "C2": "-", "C3": "-",
                        "n_sig": stt["n"], "n_stale": 0, "avg_net_delta": "",
                        "key": stt["key"],
                    }
                )
            print(
                f"  {stt['key']} n={stt['n']} mean={s020._fnum(stt['mean'], 2)} "
                f"total={s020._fnum(stt['month_total'], 2)} adj={stt['avg_adj']:.2f}",
                flush=True,
            )
        print(f"  group_sec={elapsed:.1f} memo hit={_MEMO_HIT} miss={_MEMO_MISS}", flush=True)

    e1_need = max(1, int(cov["e1_need"]))
    itm_need = max(1, int(cov["itm_need"]))
    e1_miss_pct = 100.0 * float(cov["e1_miss"]) / float(e1_need)
    itm_ok_pct = 100.0 * float(cov["itm_ok"]) / float(itm_need)
    tot_hit = _MEMO_HIT + _MEMO_MISS
    print(
        f"memo hit={_MEMO_HIT} miss={_MEMO_MISS} "
        f"rate={(_MEMO_HIT / tot_hit * 100) if tot_hit else 0:.1f}% "
        f"book_symbols={len(_BOOK.arr) if _BOOK else 0} "
        f"book_hits={_BOOK.hits if _BOOK else 0} book_miss={_BOOK.misses if _BOOK else 0}",
        flush=True,
    )

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
            print(f"  NET={float(rec['net']):.6f} entry={rec['entry_ts']}", flush=True)

    report = [
        f"S021 month={month} {win_from}..{win_to}",
        f"cells_run={len(done)} work={n_work} stage1={bool(args.stage1)}",
        "settlement_fee_assumed_0=True (fees_sim has no expiry fee)",
        (
            f"coverage E1 need={cov['e1_need']} ok={cov['e1_ok']} "
            f"miss={cov['e1_miss']} ({e1_miss_pct:.1f}%)"
        ),
        (
            f"coverage ITM exact ATM±1000 ok={cov['itm_ok']}/{cov['itm_need']} "
            f"({itm_ok_pct:.1f}%)"
        ),
        f"memo_hit={_MEMO_HIT} memo_miss={_MEMO_MISS}",
        f"group_runtime_s={float(np.mean(cell_secs)) if cell_secs else float('nan'):.2f}",
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
        if st.get("nets"):
            report.append("  nets=" + ",".join(f"{x:.4f}" for x in st["nets"]))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    txtp = OUT_DIR / f"s021_{month}_{stamp}.txt"
    txtp.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report), flush=True)
    print(f"wrote {txtp} elapsed={time.perf_counter() - t_all:.0f}s", flush=True)
    archive.finalize()
    store.close()


if __name__ == "__main__":
    main()
