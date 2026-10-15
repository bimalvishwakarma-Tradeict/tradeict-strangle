#!/usr/bin/env python3
"""S022 ULP rolling ITM long strangle backtest (smoke / grid).

python backtest/strategies/s022_ulp_long_strangle/run_s022.py --month 2025-06 --days 3 --smoke
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.harness.run_archive import RunArchive, greeks_from_mark  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import t_years as _t_years  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s021_hedged_long_straddle import run_s021 as s021  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    MIN_SEP,
    SEED,
    assign_quantile_bins,
    collapse_events,
    ffill_1m_grid,
    forbidden_mask,
    ist_hod,
    ist_hod_arr,
    ist_ym,
    rolling_pre_range_60,
    sample_control_times,
)
from backtest.strategies.s022_ulp_long_strangle.sim import (  # noqa: E402
    A_GRID,
    A_GRID_P5,
    A_GRID_REDUCED,
    DTE_GRID,
    DTE_GRID_P5,
    DTE_GRID_REDUCED,
    H_GRID,
    H_GRID_P5,
    H_GRID_REDUCED,
    SL_GRID,
    T_GRID,
    X_ITM,
    X_P5,
    X_REDUCED,
    expiry_date,
    filter_traded_events,
    map_src,
    simulate_path,
    sl_level,
    take_in_order,
)

PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
ARMS = ("ulp", "c2", "volctrl")


class S021Market:
    def __init__(
        self,
        store: Any,
        book: s021.MarkBook,
        spot_c: dict[int, float],
        grid: np.ndarray,
        gc: np.ndarray,
    ) -> None:
        self.store = store
        self.book = book
        self.spot_c = spot_c
        self.grid = grid
        self.gc = gc

    def spot(self, t: int) -> float | None:
        minute = (int(t) // 60) * 60
        if minute in self.spot_c:
            return float(self.spot_c[minute])
        for age in range(60, 3600, 60):
            v = self.spot_c.get(minute - age)
            if v is not None:
                return float(v)
        return None

    def quote(self, symbol: str, t: int) -> tuple[float, str] | None:
        q = self.book.quote(symbol, t)
        if q is None:
            return None
        return float(q.px), map_src(str(q.src))

    def chain(self, exp: Any, t: int) -> tuple[list[dict[str, Any]], str] | None:
        packed = s021.load_chain_cached(self.store, exp, t)
        if packed is None:
            return None
        rows, src = packed
        return list(rows), map_src(str(src))

    def ensure(self, symbol: str, exp: Any) -> None:
        self.book.ensure(self.store, symbol, exp)

    def mark_array(self, symbol: str) -> tuple[int, np.ndarray] | None:
        a = self.book.arr.get(symbol)
        if a is None:
            return None
        return int(self.book.t0), a

    def spot_array(self) -> tuple[int, np.ndarray] | None:
        if self.grid.size == 0:
            return None
        return int(self.grid[0]), self.gc


def _bars_from_ohlc(tts: Any, to_: Any, th: Any, tl: Any, tc: Any) -> list[dict[str, Any]]:
    return [
        {
            "time": int(t),
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
        }
        for t, o, h, lo, c in zip(tts, to_, th, tl, tc)
    ]


def _ulp_events(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    tfs: list[str],
    modes: list[str],
    start_ts: int,
    cutoff: int,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for tf in tfs:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        for mode in modes:
            raw = [s for s in iter_ulp_signals(bars, mode, tf) if s["rule"] == "fresh"]
            ev = collapse_events(raw)
            kept = [e for e in ev if start_ts <= int(e["signal_ts"]) < cutoff]
            for i, e in enumerate(kept):
                e["event_id"] = f"{tf}|{mode}|{i}|{int(e['signal_ts'])}"
                e["arm"] = "ulp"
            out[(tf, mode)] = kept
    return out


def _c2_events(
    ulp: list[dict[str, Any]],
    ts_1m: np.ndarray,
    near: np.ndarray,
    hod_1m: np.ndarray,
    rng: np.random.Generator,
    win_lo: int,
    win_hi: int,
) -> tuple[list[dict[str, Any]], int]:
    """One control per ULP event: same IST hour, inside traded window, ±60 exclusion."""
    in_win = (ts_1m.astype(np.int64) >= int(win_lo)) & (ts_1m.astype(np.int64) < int(win_hi)) & (~near)
    out: list[dict[str, Any]] = []
    n_miss = 0
    by_hod: dict[int, np.ndarray] = {}
    for i, e in enumerate(ulp):
        hod = int(e.get("hod") if e.get("hod") is not None else ist_hod(int(e["signal_ts"])))
        if hod not in by_hod:
            by_hod[hod] = ts_1m[in_win & (hod_1m == hod)]
        picked = sample_control_times(by_hod[hod], 1, rng)
        if picked.size == 0:
            n_miss += 1
            continue
        ct = int(picked[0])
        out.append(
            {
                "event_id": f"c2|{e.get('tf')}|{e.get('mode')}|{i}|{ct}",
                "arm": "c2",
                "tf": e["tf"],
                "mode": e["mode"],
                "signal_ts": ct,
                "signal_ist": s018.ist_str(ct),
                "hod": ist_hod(ct),
                "ym": ist_ym(ct),
                "src_event": e["event_id"],
            }
        )
    return out, n_miss


def _volctrl_events(
    ulp: list[dict[str, Any]],
    grid: np.ndarray,
    cell: np.ndarray,
    hod_g: np.ndarray,
    rng: np.random.Generator,
    win_lo: int,
    win_hi: int,
    near_g: np.ndarray,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """VOLCTRL from (vol bin × hour) inside the traded window; widen neighbour then hour."""
    g0 = int(grid[0]) if grid.size else 0
    n = int(grid.size)
    in_win = (
        (grid.astype(np.int64) >= int(win_lo))
        & (grid.astype(np.int64) < int(win_hi))
        & (~near_g)
        & (cell >= 0)
    )
    stats = {"neighbor_bin": 0, "same_hour": 0, "miss": 0}
    out: list[dict[str, Any]] = []
    for i, e in enumerate(ulp):
        t0 = int(e["signal_ts"])
        gi = int((t0 - g0) // 60)
        if gi < 0 or gi >= n:
            stats["miss"] += 1
            continue
        cid = int(cell[gi])
        hod = int(hod_g[gi]) if 0 <= gi < int(hod_g.size) else ist_hod(t0)
        bin0 = cid // 24 if cid >= 0 else -1
        pool = grid[in_win & (cell == cid)] if cid >= 0 else np.zeros(0, dtype=np.int64)
        widen = "cell"
        if pool.size == 0 and bin0 >= 0:
            neigh = [int((bin0 + d) * 24 + hod) for d in (-1, 1) if 0 <= bin0 + d < 20]
            if neigh:
                pool = grid[in_win & np.isin(cell, np.array(neigh, dtype=np.int32))]
            if pool.size:
                widen = "neighbor_bin"
                stats["neighbor_bin"] += 1
        if pool.size == 0:
            pool = grid[in_win & (hod_g == hod)]
            if pool.size:
                widen = "same_hour"
                stats["same_hour"] += 1
        if pool.size == 0:
            stats["miss"] += 1
            continue
        picked = sample_control_times(pool, 1, rng)
        if picked.size == 0:
            stats["miss"] += 1
            continue
        ct = int(picked[0])
        out.append(
            {
                "event_id": f"vol|{e.get('tf')}|{e.get('mode')}|{i}|{ct}",
                "arm": "volctrl",
                "tf": e["tf"],
                "mode": e["mode"],
                "signal_ts": ct,
                "signal_ist": s018.ist_str(ct),
                "hod": ist_hod(ct),
                "ym": ist_ym(ct),
                "src_event": e["event_id"],
                "vol_widen": widen,
            }
        )
    return out, stats


def _strike_cfgs() -> list[tuple[str, int]]:
    return [("itm", int(x)) for x in X_ITM] + [("otm", 1000)]


def _profit_factor(nets: list[float]) -> float:
    pos = sum(float(x) for x in nets if float(x) > 0)
    neg = sum(float(x) for x in nets if float(x) < 0)
    if neg == 0.0:
        return float("inf") if pos > 0 else float("nan")
    return pos / abs(neg)


def _a_pct(a: Any) -> float | None:
    if a is None or a == "" or a == "none":
        return None
    return float(a)


def _a_dist(a: Any, target: int = 125) -> int:
    if a is None or a == "" or a == "none":
        return 10**9
    return abs(int(a) - int(target))


def _h_dist(h: Any, target: int = 0) -> int:
    if h is None or h == "":
        return abs(0 - int(target)) if target == 0 else 10**9
    return abs(int(h) - int(target))


def _cell_key(r: dict[str, Any]) -> tuple[Any, ...]:
    return (
        r.get("tf"), r.get("mode"), r.get("style"), r.get("X"), r.get("dte"),
        r.get("a"), r.get("H"), r.get("T"), r.get("SL"),
    )


def _paired_ulp(
    cell_rows: list[dict[str, Any]],
    other: str,
) -> tuple[int, float, float]:
    idx = {
        _cell_key(r): r
        for r in cell_rows
        if r.get("arm") == other and int(r.get("n") or 0) > 0
    }
    diffs: list[float] = []
    n_better = 0
    for r in cell_rows:
        if r.get("arm") != "ulp" or int(r.get("n") or 0) <= 0:
            continue
        o = idx.get(_cell_key(r))
        if o is None:
            continue
        d = float(r["mean"]) - float(o["mean"])
        diffs.append(d)
        if d > 0:
            n_better += 1
    n = len(diffs)
    pct = (100.0 * n_better / n) if n else float("nan")
    return n, _median(diffs), pct


def _median(xs: list[float]) -> float:
    a = sorted(float(x) for x in xs)
    if not a:
        return float("nan")
    m = len(a) // 2
    if len(a) % 2:
        return a[m]
    return 0.5 * (a[m - 1] + a[m])


def _pctile(xs: list[float], p: float) -> float:
    a = sorted(float(x) for x in xs)
    if not a:
        return float("nan")
    if len(a) == 1:
        return a[0]
    k = (len(a) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(a) - 1)
    w = k - lo
    return a[lo] * (1.0 - w) + a[hi] * w


def _seq_maxdd(nets: list[float]) -> float:
    eq = 0.0
    pk = 0.0
    dd = 0.0
    for x in nets:
        eq += float(x)
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dd


def _db_expiries(store: Any, d0: Any, d1: Any) -> list[str]:
    inner = getattr(store, "inner", store)
    seen: set[str] = set()
    d = d0
    while d <= d1:
        conn = None
        try:
            conn = inner.conn(d)
        except Exception:
            conn = None
        if conn is not None:
            try:
                for (e,) in conn.execute("SELECT DISTINCT expiry FROM marks").fetchall():
                    seen.add(str(e)[:10])
            except Exception:
                pass
        d = d + timedelta(days=1)
    return sorted(seen)


def _cell_stats(taken: list[dict[str, Any]], n_skipped: int, reasons: dict[str, int]) -> dict[str, Any]:
    nets = [float(r["net"]) for r in taken]
    n = len(nets)
    by_r: dict[str, int] = {}
    for r in taken:
        by_r[str(r["reason"])] = by_r.get(str(r["reason"]), 0) + 1
    return {
        "n": n,
        "n_skipped_events": int(n_skipped),
        "skip_reasons": dict(reasons),
        "mean": (sum(nets) / n) if n else float("nan"),
        "median": _median(nets),
        "worst": min(nets) if nets else float("nan"),
        "p5": _pctile(nets, 5.0),
        "win_pct": (100.0 * sum(1 for x in nets if x > 0) / n) if n else float("nan"),
        "sum": sum(nets) if nets else 0.0,
        "maxDD": _seq_maxdd(nets),
        "avg_adj": (sum(float(r["n_adj_at_exit"]) for r in taken) / n) if n else float("nan"),
        "avg_fee": (sum(float(r["fees_at_exit"]) for r in taken) / n) if n else float("nan"),
        "avg_slip": (sum(float(r["slip_at_exit"]) for r in taken) / n) if n else float("nan"),
        "mean_gross": (
            sum(float(r.get("gross_at_exit", 0.0) or 0.0) for r in taken) / n
        ) if n else float("nan"),
        "mean_cost": (
            sum(
                float(r["fees_at_exit"]) + float(r["slip_at_exit"])
                for r in taken
            ) / n
        ) if n else float("nan"),
        "profit_factor": _profit_factor(nets),
        "n_TARGET": by_r.get("TARGET", 0),
        "n_SL": by_r.get("SL", 0),
        "n_TIME": by_r.get("TIME", 0),
        "n_EXPIRY": by_r.get("EXPIRY", 0),
    }


def _mean_ok(r: dict[str, Any]) -> float:
    v = float(r.get("mean", float("nan")))
    return v if v == v else -1e18


def _patch_legs_csv(folder: Path) -> None:
    """Add slip_pts_entry/exit columns (archive schema ignores extras on write)."""
    src = folder / "legs.csv"
    if not src.exists():
        return
    with src.open("r", encoding="utf-8", newline="") as fp:
        rows = list(csv.DictReader(fp))
    if not rows:
        return
    cols = list(rows[0].keys())
    for extra in ("slip_pts_entry", "slip_pts_exit"):
        if extra not in cols:
            cols.append(extra)
    for r in rows:
        try:
            em = float(r.get("entry_mark") or "nan")
            ef = float(r.get("entry_fill") or "nan")
            xm = float(r.get("exit_mark") or "nan")
            xf = float(r.get("exit_fill") or "nan")
            se = float(r.get("slip_exit") or 0.0)
            fe = float(r.get("fee_exit") or 0.0)
        except (TypeError, ValueError):
            em = ef = xm = xf = se = fe = float("nan")
        r["slip_pts_entry"] = abs(ef - em) if em == em and ef == ef else ""
        if se == 0.0 and fe == 0.0:
            r["slip_pts_exit"] = 0.0
        else:
            r["slip_pts_exit"] = abs(xm - xf) if xm == xm and xf == xf else ""
    with src.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _add_taken_trade(
    archive: RunArchive,
    rec: dict[str, Any],
    path: Any,
    cell: dict[str, Any],
    month: str,
    mkt: S021Market,
) -> None:
    ev = rec["event"]
    t0 = int(ev["signal_ts"])
    x_ts = int(rec.get("exit_ts") or path.tsl_ts or t0)
    spot0 = mkt.spot(t0) or float("nan")
    spot1 = mkt.spot(x_ts) or float("nan")
    tid = (
        f"{cell['arm']}|{ev.get('event_id')}|{cell['style']}|{cell['X']}|"
        f"{cell['dte']}|{cell['a']}|{cell.get('H')}|{cell['T']}|{cell['SL']}|{t0}"
    )
    trade = {
        "trade_id": tid,
        "month": month,
        "tf": cell["tf"],
        "variant": cell["style"],
        "band": cell["X"],
        "band_mode": f"a{cell['a']}",
        "arm": cell["arm"],
        "n_legs": len(path.legs),
        "dte": cell["dte"],
        "side": "long",
        "entry_ist": s018.ist_str(t0),
        "exit_ist": s018.ist_str(x_ts),
        "exit_reason": rec.get("reason", path.tsl_reason),
        "hold_hrs": (x_ts - t0) / 3600.0,
        "hrs_to_exp": max(0.0, (int(path.exp_ts) - t0) / 3600.0),
        "spot_entry": spot0,
        "spot_exit": spot1,
        "gross": rec.get("gross_at_exit", float("nan")),
        "brokerage": rec.get("fees_at_exit", path.fees),
        "slippage": rec.get("slip_at_exit", path.slips),
        "net": rec.get("net", path.tsl_net),
        "mfe": rec.get("mfe", float("nan")),
        "mae": rec.get("mae", float("nan")),
    }
    legs: list[dict[str, Any]] = []
    snaps: list[dict[str, Any]] = []
    for lg in path.legs:
        t_yr0 = _t_years(int(lg.opened_ts), int(s018.expiry_unix(lg.expiry)))
        sp_e = mkt.spot(int(lg.opened_ts)) or spot0
        g0 = greeks_from_mark(float(lg.mark0), float(sp_e), float(lg.strike), t_yr0, bool(lg.is_call))
        xt = int(lg.closed_ts) if lg.closed_ts else x_ts
        t_yr1 = _t_years(xt, int(s018.expiry_unix(lg.expiry)))
        sp_x = mkt.spot(xt) or spot1
        em = float(lg.exit_mark) if math.isfinite(float(lg.exit_mark)) else float(lg.mark0)
        g1 = greeks_from_mark(em, float(sp_x), float(lg.strike), t_yr1, bool(lg.is_call))
        legs.append(
            {
                "symbol": lg.symbol,
                "strike": lg.strike,
                "type": "C" if lg.is_call else "P",
                "expiry": lg.expiry.isoformat(),
                "qty": lg.qty,
                "long_short": "long",
                "entry_mark": lg.mark0,
                "entry_fill": lg.fill,
                "exit_mark": lg.exit_mark,
                "exit_fill": lg.exit_fill,
                "fee_entry": lg.fee,
                "fee_exit": lg.exit_fee,
                "slip_entry": lg.slip,
                "slip_exit": lg.exit_slip,
                "slip_pts_entry": getattr(lg, "slip_pts_entry", float("nan")),
                "slip_pts_exit": getattr(lg, "slip_pts_exit", float("nan")),
                "iv_entry": g0.get("iv"),
                "delta_entry": g0.get("delta"),
                "gamma_entry": g0.get("gamma"),
                "theta_entry": g0.get("theta"),
                "vega_entry": g0.get("vega"),
                "iv_exit": g1.get("iv"),
                "delta_exit": g1.get("delta"),
                "gamma_exit": g1.get("gamma"),
                "theta_exit": g1.get("theta"),
                "vega_exit": g1.get("vega"),
            }
        )
    for fr in path.fills:
        kind = "entry" if str(fr.get("reason")) == "ENTRY" else (
            "adj" if str(fr.get("reason")) == "ADJ" else str(fr.get("reason") or "mark").lower()
        )
        snaps.append(
            {
                "ts_ist": fr.get("ist"),
                "kind": kind,
                "spot": fr.get("spot"),
                "leg1_mark": fr.get("mark"),
                "leg1_iv": fr.get("iv"),
                "basket_mark_pnl": fr.get("gross", fr.get("net")),
            }
        )
    archive.add_trade(trade, legs, snaps)


def _chain_gap_windows(
    store: Any,
    expiries: list[Any],
    t_lo: int,
    t_hi: int,
) -> list[dict[str, Any]]:
    """Contiguous 1-minute windows with no marks for an expiry. Reporting only."""
    t_lo = (int(t_lo) // 60) * 60
    t_hi = (int(t_hi) // 60) * 60
    n = (t_hi - t_lo) // 60 + 1
    out: list[dict[str, Any]] = []
    if n <= 0:
        return out
    for exp in sorted(set(expiries), key=lambda x: str(x)):
        have = np.zeros(n, dtype=bool)
        conn = None
        try:
            conn = store.conn(exp)
        except Exception:
            conn = None
        rows: list[Any] = []
        if conn is not None:
            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT ts FROM marks
                    WHERE expiry=? AND ts BETWEEN ? AND ?
                      AND close IS NOT NULL AND close>0
                    """,
                    (str(exp)[:10], t_lo, t_hi),
                ).fetchall()
            except Exception:
                rows = []
        for (ts,) in rows:
            i = (int(ts) // 60 * 60 - t_lo) // 60
            if 0 <= i < n:
                have[i] = True
        i = 0
        while i < n:
            if bool(have[i]):
                i += 1
                continue
            j = i
            while j < n and not bool(have[j]):
                j += 1
            ts0 = t_lo + i * 60
            ts1 = t_lo + (j - 1) * 60
            out.append(
                {
                    "expiry": str(exp)[:10],
                    "ts_start": int(ts0),
                    "ts_end": int(ts1),
                    "ist_start": s018.ist_str(int(ts0)),
                    "ist_end": s018.ist_str(int(ts1)),
                    "minutes": int(j - i),
                }
            )
            i = j
    return out


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--months", nargs="+", default=None)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--arms", default="all")
    ap.add_argument("--tfs", default="5m,15m,30m")
    ap.add_argument("--modes", default="wick,full")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--cache-gb", type=float, default=1.0)
    ap.add_argument("--slip", type=float, default=20.0)
    ap.add_argument("--slip-model", dest="slip_model", choices=("fixed", "table"), default="fixed")
    ap.add_argument("--slip-mult", dest="slip_mult", type=float, default=1.0)
    ap.add_argument("--reduced", action="store_true")
    ap.add_argument("--p5", action="store_true")
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    months = [str(x) for x in (args.months if args.months else [args.month])]
    if str(args.slip_model) == "table":
        from backtest.slippage_model import load_slip_table

        load_slip_table()
    for month in months:
        run_month(args, month)


def run_month(args: Any, month: str) -> None:
    days = int(args.days or 0)
    if bool(args.smoke) and days <= 0:
        days = 3
    tfs = [x.strip() for x in str(args.tfs).split(",") if x.strip()] or list(TFS)
    modes = [x.strip() for x in str(args.modes).split(",") if x.strip()] or list(MODES)
    arms_s = str(args.arms).strip().lower()
    if arms_s in ("", "all"):
        arms = list(ARMS)
    else:
        arms = [x.strip() for x in arms_s.split(",") if x.strip()]

    csv_path = str(args.csv or s018.SPOT_CSV)
    args.csv = csv_path
    win_from, win_to = d.month_bounds(month)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    month_cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    cutoff = start_ts + days * 86400 if days else month_cutoff

    from backtest.strategies.s012_trend_follow.engine import ist_date as _ist_date

    win_to_use = _ist_date(cutoff - 1) if days else win_to

    reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slip_pts = float(args.slip)
    slip_model = str(args.slip_model)
    slip_mult = float(args.slip_mult)
    reduced = bool(args.reduced)
    p5 = bool(args.p5)
    if p5:
        a_grid = A_GRID_P5
        dte_grid = DTE_GRID_P5
        h_grid = H_GRID_P5
        cfgs = list(X_P5)
        tag = "p5"
    elif reduced:
        a_grid = A_GRID_REDUCED
        dte_grid = DTE_GRID_REDUCED
        h_grid = H_GRID_REDUCED
        cfgs = list(X_REDUCED)
        tag = "reduced"
    else:
        a_grid = A_GRID
        dte_grid = DTE_GRID
        h_grid = H_GRID
        cfgs = _strike_cfgs()
        tag = "grid"
    if days or args.smoke:
        tag = "smoke"
    folder = RUNS_DIR / f"{stamp}_s022_{tag}_{month}"
    n = 0
    while folder.exists():
        n += 1
        folder = RUNS_DIR / f"{stamp}_s022_{tag}_{month}_{n}"
    archive = RunArchive(
        "s022",
        tag,
        args,
        month=month,
        prereg_note=(
            f"S022 ULP long strangle; slip_model={slip_model} slip={slip_pts} "
            f"slip_mult={slip_mult}; p5={p5} reduced={reduced}; settle intrinsic 0 fee"
        ),
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)
    meta_p = archive.folder / "run_meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta["slip_pts"] = slip_pts
    meta["slip_model"] = slip_model
    meta["slip_mult"] = slip_mult
    meta["reduced"] = reduced
    meta["p5"] = p5
    meta["grid"] = {
        "X": [list(c) for c in cfgs],
        "dte": list(dte_grid),
        "a": ["none" if a is None else a for a in a_grid],
        "H": ["none" if h is None else h for h in h_grid],
        "T": list(T_GRID),
        "SL": list(SL_GRID),
    }
    meta_p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    t_load = time.perf_counter()
    spot = s018.load_spot_1m(csv_path)
    ts_all, o_all, h_all, l_all, c_all, vol_all = s020.bars_1m_vol(spot)
    lo = start_ts - 7 * 86400
    hi = cutoff + 4 * 86400
    sel = (ts_all >= lo) & (ts_all < hi)
    ts, o, h, l, c, vol = ts_all[sel], o_all[sel], h_all[sel], l_all[sel], c_all[sel], vol_all[sel]
    spot_c = {int(t): float(px) for t, px in zip(ts, c)}
    print(f"1m bars={len(ts)} data_load_s={time.perf_counter() - t_load:.2f}", flush=True)

    inner = s020.MarksStore if False else None
    from backtest.harness.data import MarksStore

    inner = MarksStore()
    store = d.MonthGuardStore(inner, win_from - timedelta(days=1), win_to_use + timedelta(days=14))
    s021._BOOK = s021.MarkBook(start_ts - 86400, cutoff + 4 * 86400)
    s021._CHAIN_S021 = {}

    hod_1m = ist_hod_arr(ts)
    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    pre = rolling_pre_range_60(gh, gl)
    bins, _ = assign_quantile_bins(pre)
    hod_g = (((grid.astype(np.int64) + 19800) // 3600) % 24).astype(np.int16)
    cell = np.full(int(grid.size), -1, dtype=np.int32)
    okb = bins >= 0
    cell[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)

    mkt = S021Market(store, s021._BOOK, spot_c, grid, gc)
    ulp_by = _ulp_events(ts, o, h, l, c, vol, tfs, modes, start_ts, cutoff)
    print("counting full-month ULP events (dry pass)...", flush=True)
    ulp_month = _ulp_events(ts_all, o_all, h_all, l_all, c_all, vol_all, tfs, modes, start_ts, month_cutoff)
    n_ulp_month = sum(len(v) for v in ulp_month.values())

    rng = np.random.default_rng(SEED)
    events_by_arm: dict[str, list[dict[str, Any]]] = {a: [] for a in ARMS}
    n_ev_table: list[dict[str, Any]] = []
    vol_widen: dict[str, int] = {"neighbor_bin": 0, "same_hour": 0, "miss": 0}
    c2_miss = 0
    count_notes: list[str] = []
    for tf in tfs:
        for mode in modes:
            ulp = ulp_by.get((tf, mode), [])
            n_ev_table.append({"arm": "ulp", "tf": tf, "mode": mode, "n": len(ulp)})
            events_by_arm["ulp"].extend(ulp)
            sig_arr = np.array([int(e["signal_ts"]) for e in ulp], dtype=np.int64)
            near_g = forbidden_mask(grid, sig_arr, MIN_SEP)
            near_1 = forbidden_mask(ts, sig_arr, MIN_SEP)
            if "c2" in arms:
                c2, n_c2m = _c2_events(ulp, ts, near_1, hod_1m, rng, start_ts, cutoff)
                c2_miss += int(n_c2m)
                n_ev_table.append({"arm": "c2", "tf": tf, "mode": mode, "n": len(c2)})
                events_by_arm["c2"].extend(c2)
                if len(c2) != len(ulp):
                    count_notes.append(f"c2 {tf} {mode}: ulp={len(ulp)} c2={len(c2)} miss={n_c2m}")
            if "volctrl" in arms:
                vc, vw = _volctrl_events(ulp, grid, cell, hod_g, rng, start_ts, cutoff, near_g)
                for k, v in vw.items():
                    vol_widen[k] = vol_widen.get(k, 0) + int(v)
                n_ev_table.append({"arm": "volctrl", "tf": tf, "mode": mode, "n": len(vc)})
                events_by_arm["volctrl"].extend(vc)
                if len(vc) != len(ulp):
                    count_notes.append(f"volctrl {tf} {mode}: ulp={len(ulp)} vol={len(vc)} {vw}")

    n_before_clip = {a: len(events_by_arm[a]) for a in arms}
    for a in arms:
        events_by_arm[a] = filter_traded_events(events_by_arm[a], start_ts, cutoff)
    n_ev_table = []
    for tf in tfs:
        for mode in modes:
            for arm in arms:
                n_keep = sum(
                    1
                    for e in events_by_arm[arm]
                    if str(e.get("tf")) == tf and str(e.get("mode")) == mode
                )
                n_ev_table.append({"arm": arm, "tf": tf, "mode": mode, "n": n_keep})

    n_combo = len(cfgs) * len(dte_grid) * len(a_grid)
    work_events = [e for a in arms for e in events_by_arm[a]]
    n_clipped = {a: int(n_before_clip[a]) - len(events_by_arm[a]) for a in arms}
    print(
        f"events smoke={len(work_events)} ulp_month={n_ulp_month} "
        f"combos_per_event={n_combo} (X={len(cfgs)} dte={len(dte_grid)} a={len(a_grid)} H={len(h_grid)})",
        flush=True,
    )

    from datetime import date as date_cls
    import csv
    from openpyxl import Workbook

    listed_cache: dict[str, list] = {}
    skip: dict[str, int] = {
        "missing_expiry": 0,
        "entry_mark_none": 0,
        "no_chain": 0,
        "no_strikes": 0,
        "path_mark_none": 0,
        "adj_entry_none": 0,
        "other": 0,
    }
    src_tot = {"real": 0, "stale": 0, "parity": 0, "none": 0}
    src_by_x: dict[tuple[str, int], dict[str, int]] = {}
    src_by_dte: dict[int, dict[str, int]] = {int(d): dict(src_tot) for d in dte_grid}
    if 2 not in src_by_dte:
        src_by_dte[2] = dict(src_tot)
    n_paths = 0
    n_ok = 0
    t_sim = time.perf_counter()
    n_work = max(1, len(work_events))
    exits_by_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    skip_by_key: dict[tuple[Any, ...], str] = {}
    dump_spec = ("ulp", "15m", "wick", "itm", 1000, 1, 125)
    miss_exp_ev: dict[int, int] = {1: 0, 2: 0}
    miss_exp_dates: dict[int, set[str]] = {1: set(), 2: set()}
    x_prem: dict[tuple[str, int], list[float]] = {}
    x_spts: dict[tuple[str, int], list[float]] = {}
    mark_none_by_date: dict[str, int] = {}
    traded_exps: set[Any] = set()

    DUMP_T, DUMP_SL = 200, 200

    for ei, e in enumerate(work_events):
        t0 = int(e["signal_ts"])
        around = _ist_date(t0)
        ym = f"{around.year:04d}-{around.month:02d}"
        if ym not in listed_cache:
            listed_cache[ym] = s021.listed_expiries(store, around)
        exp_set = set(listed_cache[ym])
        for dte in dte_grid:
            ed = expiry_date(t0, int(dte))
            if ed not in exp_set:
                miss_exp_ev[int(dte)] += 1
                miss_exp_dates[int(dte)].add(ed.isoformat())
        for style, x in cfgs:
            for dte in dte_grid:
                ed = expiry_date(t0, int(dte))
                if ed not in exp_set:
                    skip["missing_expiry"] += len(a_grid)
                    for a in a_grid:
                        key = (e["event_id"], style, int(x), int(dte), a)
                        skip_by_key[key] = "missing_expiry"
                    continue
                exp_ts = int(s018.expiry_unix(ed))
                minutes = np.arange(t0, exp_ts + 1, 60, dtype=np.int64)
                for a in a_grid:
                    p = simulate_path(
                        mkt, t0, ed, int(x), style, _a_pct(a), minutes,
                        compact=True, record_fills=False,
                        slip_pts=slip_pts, h_grid=h_grid,
                        slip_model=slip_model, slip_mult=slip_mult, dte=int(dte),
                    )
                    n_paths += 1
                    key = (e["event_id"], style, int(x), int(dte), a)
                    bx = (style, int(x))
                    src_by_x.setdefault(bx, {"real": 0, "stale": 0, "parity": 0, "none": 0})
                    for k, v in p.src_counts.items():
                        src_tot[k] = src_tot.get(k, 0) + int(v)
                        src_by_x[bx][k] = src_by_x[bx].get(k, 0) + int(v)
                        src_by_dte[int(dte)][k] = src_by_dte[int(dte)].get(k, 0) + int(v)
                    if ed not in traded_exps and ed in exp_set:
                        traded_exps.add(ed)
                    if not p.ok:
                        reason = p.skip_reason or "other"
                        skip[reason] = skip.get(reason, 0) + 1
                        skip_by_key[key] = reason
                        if reason == "path_mark_none":
                            day = around.isoformat()
                            mark_none_by_date[day] = mark_none_by_date.get(day, 0) + 1
                    else:
                        n_ok += 1
                        exits_by_key[key] = list(p.exits)
                        xp = x_prem.setdefault(bx, [])
                        xs = x_spts.setdefault(bx, [])
                        for lg in p.legs:
                            if int(lg.opened_ts) == int(t0):
                                xp.append(float(lg.mark0))
                                xs.append(float(lg.slip_pts_entry))
                            if not lg.settled and lg.slip_pts_exit == lg.slip_pts_exit:
                                xs.append(float(lg.slip_pts_exit))
        if (ei + 1) % 10 == 0 or ei + 1 == n_work:
            elapsed = time.perf_counter() - t_sim
            print(
                f"  events {ei + 1}/{n_work} paths={n_paths} ok={n_ok} "
                f"elapsed_s={elapsed:.1f}",
                flush=True,
            )

    sim_s = time.perf_counter() - t_sim
    gap_hi = int(s018.expiry_unix(max(traded_exps))) if traded_exps else cutoff
    gap_rows = _chain_gap_windows(store, list(traded_exps), start_ts, gap_hi)
    per_path = (sim_s / n_paths) if n_paths else float("nan")
    n_month_events = n_ulp_month * len(arms)
    n_month_paths = n_month_events * n_combo
    proj_s = n_month_paths * per_path if n_paths else float("nan")

    t_occ = time.perf_counter()
    cell_rows: list[dict[str, Any]] = []
    n_taken = 0
    streams: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for e in work_events:
        for style, x in cfgs:
            for dte in dte_grid:
                for a in a_grid:
                    rec = dict(e)
                    rec["path_key"] = (e["event_id"], style, int(x), int(dte), a)
                    sk = (e["arm"], e["tf"], e["mode"], style, int(x), int(dte), a)
                    streams.setdefault(sk, []).append(rec)
    dump_taken: list[dict[str, Any]] = []
    dump_cell_used = dump_spec
    DUMP_H: int | None = None
    for sk, evs in streams.items():
        arm, tf, mode, style, x, dte, a = sk
        for T in T_GRID:
            for SL in SL_GRID:
                for H in h_grid:
                    taken, n_sk, reasons = take_in_order(
                        evs, exits_by_key, skip_by_key, float(T), float(SL), h=H,
                    )
                    n_taken += len(taken)
                    st = _cell_stats(taken, n_sk, reasons)
                    st.update(
                        {
                            "arm": arm, "tf": tf, "mode": mode, "style": style,
                            "X": int(x), "dte": int(dte), "a": a, "H": H,
                            "T": int(T), "SL": int(SL),
                        }
                    )
                    cell_rows.append(st)
                    if (
                        arm, tf, mode, style, int(x), int(dte), a, int(T), int(SL)
                    ) == (*dump_spec, DUMP_T, DUMP_SL) and H is None:
                        dump_taken = taken
                        dump_cell_used = dump_spec
    occ_s = time.perf_counter() - t_occ

    dump_requested = True
    if len(dump_taken) < 5:
        dump_requested = False
        near = [
            r for r in cell_rows
            if r["arm"] == "ulp" and r["tf"] == "15m" and r["mode"] == "wick"
            and r["style"] == "itm" and int(r["n"]) > 0
        ]
        near.sort(key=lambda r: (
            abs(int(r["X"]) - 1000), abs(int(r["dte"]) - 1),
            _a_dist(r["a"], 125), _h_dist(r.get("H"), 0),
            abs(int(r["T"]) - 200), abs(int(r["SL"]) - 200),
        ))
        if near:
            pick = near[0]
            dump_cell_used = (
                "ulp", "15m", "wick", "itm", int(pick["X"]), int(pick["dte"]), pick["a"],
            )
            sk = ("ulp", "15m", "wick", "itm", int(pick["X"]), int(pick["dte"]), pick["a"])
            evs = streams.get(sk, [])
            DUMP_H = pick.get("H")
            dump_taken, _, _ = take_in_order(
                evs, exits_by_key, skip_by_key, float(pick["T"]), float(pick["SL"]),
                h=DUMP_H,
            )
            DUMP_T, DUMP_SL = int(pick["T"]), int(pick["SL"])

    cell_path = archive.folder / "cells.csv"
    cell_cols = [
        "arm", "tf", "mode", "style", "X", "dte", "a", "H", "T", "SL",
        "n", "n_skipped_events", "mean", "median", "worst", "p5", "win_pct",
        "sum", "maxDD", "profit_factor", "avg_adj", "avg_fee", "avg_slip",
        "mean_gross", "mean_cost",
        "n_TARGET", "n_SL", "n_TIME", "n_EXPIRY",
    ]
    with cell_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=cell_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(cell_rows)
    gap_path = archive.folder / "gaps.csv"
    gap_cols = ["expiry", "ist_start", "ist_end", "minutes", "ts_start", "ts_end"]
    with gap_path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=gap_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(gap_rows)

    db_exps = _db_expiries(store, date_cls(2025, 5, 30), date_cls(2025, 6, 6))
    n_ulp_smoke = sum(1 for e in work_events if e.get("arm") == "ulp")
    miss_txt = []
    for dte in dte_grid:
        miss_txt.append(
            f"  dte={dte}: events_missing_expiry={miss_exp_ev[int(dte)]}/{len(work_events)} "
            f"dates={sorted(miss_exp_dates[int(dte)])}"
        )
    wanted_2dte = sorted(miss_exp_dates[2])
    genuinely_absent_2dte = [x for x in wanted_2dte if x not in db_exps]

    src_n = sum(src_tot.values()) or 1

    def _src_line(title: str, dsrc: dict[str, int]) -> str:
        n = sum(dsrc.values()) or 1
        return (
            f"  {title}: real={dsrc.get('real', 0)} ({100.0 * dsrc.get('real', 0) / n:.2f}%) "
            f"stale={dsrc.get('stale', 0)} ({100.0 * dsrc.get('stale', 0) / n:.2f}%) "
            f"parity={dsrc.get('parity', 0)} ({100.0 * dsrc.get('parity', 0) / n:.2f}%) "
            f"none={dsrc.get('none', 0)} ({100.0 * dsrc.get('none', 0) / n:.2f}%)"
        )

    ulp15 = [
        r for r in cell_rows
        if r["arm"] == "ulp" and r["tf"] == "15m" and r["n"] > 0
    ]
    ulp15_best = sorted(ulp15, key=lambda r: float(r["mean"]) if r["mean"] == r["mean"] else -1e18, reverse=True)[:5]
    ulp15_worst = sorted(ulp15, key=lambda r: float(r["mean"]) if r["mean"] == r["mean"] else 1e18)[:5]

    dump_paths: dict[tuple[Any, ...], Any] = {}
    dump_baskets = dump_taken[:5]
    for rec in dump_baskets:
        ev = rec["event"]
        key = ev["path_key"]
        t0 = int(ev["signal_ts"])
        _eid, style, x, dte, a = key
        ed = expiry_date(t0, int(dte))
        exp_ts = int(s018.expiry_unix(ed))
        minutes = np.arange(t0, exp_ts + 1, 60, dtype=np.int64)
        dump_paths[key] = simulate_path(
            mkt, t0, ed, int(x), str(style), _a_pct(a), minutes,
            compact=False, record_fills=True,
            slip_pts=slip_pts, h_grid=h_grid,
            slip_model=slip_model, slip_mult=slip_mult, dte=int(dte),
        )

    basket_rows: list[dict[str, Any]] = []
    basket_text: list[str] = []
    for bi, rec in enumerate(dump_baskets, start=1):
        ev = rec["event"]
        key = ev["path_key"]
        p = dump_paths.get(key)
        basket_text.append(f"=== basket #{bi} event={ev.get('event_id')} signal_ts={ev.get('signal_ts')} ===")
        if p is None:
            basket_text.append("  (path series not retained)")
            continue
        slv = float(DUMP_SL)
        x_ts = int(rec.get("exit_ts") or 0)
        for fr in p.fills:
            t = int(fr["ts"])
            idx = 0
            if p.ts:
                diffs = [abs(int(x) - t) for x in p.ts]
                idx = int(np.argmin(np.array(diffs)))
            net = float(p.net[idx]) if p.net else float("nan")
            pk = float(p.peak[idx]) if p.peak else float("nan")
            gv = float(p.gross[idx]) if p.gross else float(fr.get("gross", "nan") or "nan")
            pg = float(p.peak_gross[idx]) if p.peak_gross else float(fr.get("peak_gross", "nan") or "nan")
            fr2 = dict(fr)
            fr2["basket"] = bi
            fr2["net"] = net
            fr2["peak"] = pk
            fr2["gross"] = gv
            fr2["peak_gross"] = pg
            fr2["sl_level"] = sl_level(pg, slv) if pg == pg else float("nan")
            after = bool(x_ts) and t > x_ts
            fr2["exit_reason"] = "PATH_ONLY (after exit)" if after else rec.get("reason", "")
            basket_rows.append(fr2)
            basket_text.append(
                f"  {fr2['ist']} spot={fr2['spot']:.1f} K={fr2['strike']:.0f} "
                f"{'C' if fr2['is_call'] else 'P'} {fr2['side']} mark={fr2['mark']:.4f} "
                f"src={fr2['mark_src']} fill={fr2['fill']:.4f} fee={fr2['fee']:.4f} "
                f"slip={fr2['slip']:.4f} ref={fr2['ref']:.4f} trig={fr2['trigger']} "
                f"net={net:.4f} gross={gv:.4f} peak_g={pg:.4f} SLlvl={fr2['sl_level']:.4f} "
                f"{fr2['exit_reason']}"
            )
        basket_text.append(
            f"  EXIT {rec.get('reason')} ts={rec.get('exit_ts')} net={rec.get('net')} "
            f"gross={rec.get('gross_at_exit')} peak_gross={rec.get('peak_gross_at_exit')} "
            f"n_adj={rec.get('n_adj_at_exit')}"
        )

    wb = Workbook()
    ws = wb.active
    ws.title = "baskets"
    bcols = [
        "basket", "ist", "spot", "strike", "is_call", "side", "mark", "mark_src",
        "fill", "fee", "slip", "ref", "trigger", "net", "peak", "gross", "peak_gross",
        "sl_level", "exit_reason", "symbol", "ts",
    ]
    ws.append(bcols)
    for r in basket_rows:
        ws.append([r.get(c, "") for c in bcols])
    xlsx_path = archive.folder / "smoke_baskets.xlsx"
    wb.save(xlsx_path)
    (archive.folder / "basket1.txt").write_text(
        ("\n".join(basket_text[: max(2, next((i for i, l in enumerate(basket_text) if l.startswith('=== basket #2')), len(basket_text)))]) + "\n"),
        encoding="utf-8",
    )

    dump_note = (
        f"dump cell={dump_cell_used} T={DUMP_T} SL={DUMP_SL} H={DUMP_H} n_taken={len(dump_taken)} "
        f"dumped={len(dump_baskets)}"
        + (
            ""
            if dump_requested and dump_cell_used == dump_spec
            else " (nearest cell; requested ULP 15m wick X=1000 ITM dte=1 a=125 T=200 SL=200 H=None)"
        )
    )

    run_kind = "smoke" if bool(args.smoke) or days else ("reduced" if reduced else "grid")
    grid_a = ["none" if a is None else a for a in a_grid]
    grid_h = ["none" if h is None else h for h in h_grid]
    lines = [
        f"S022 {run_kind} — ULP rolling ITM long strangle",
        f"folder={archive.folder}",
        f"month={month} days={days} arms={arms} tfs={tfs} modes={modes}",
        f"grid X={list(cfgs)} a={grid_a} T={list(T_GRID)} SL={list(SL_GRID)} "
        f"dte={list(dte_grid)} H={grid_h}",
        f"slip_model={slip_model} slip_pts={slip_pts} slip_mult={slip_mult} "
        f"qty={1000} seed={SEED} reduced={reduced} p5={p5}",
        "SL_basis=GROSS TARGET_basis=NET (exit P&L is NET); TIME at H minutes after entry",
        "",
        "n events per arm/tf/mode:",
    ]
    for row in n_ev_table:
        lines.append(f"  {row['arm']:7s} {row['tf']:4s} {row['mode']:4s} n={row['n']}")
    lines += [
        "",
        f"n_paths_simulated={n_paths} n_ok={n_ok}",
        f"wall_s={sim_s:.2f} s_per_path={per_path:.6f} occupancy_s={occ_s:.2f} n_taken_all_cells={n_taken}",
        f"n_ulp_month={n_ulp_month} n_month_events_all_arms={n_month_events} n_month_paths_proj={n_month_paths}",
        (
            f"projected_full_month_runtime_s={proj_s:.1f} ({proj_s / 3600.0:.2f} h)"
            if proj_s == proj_s else "projected_full_month_runtime_s=nan"
        ),
        f"skip={skip}",
        _src_line("mark_src ALL", src_tot),
    ]
    for dte in dte_grid:
        lines.append(_src_line(f"mark_src dte={dte}", src_by_dte[int(dte)]))
    for (style, x), dsrc in sorted(src_by_x.items()):
        lines.append(_src_line(f"mark_src {style} X={x}", dsrc))
    lines += [
        f"book_symbols={len(s021._BOOK.arr) if s021._BOOK else 0} "
        f"book_hits={s021._BOOK.hits if s021._BOOK else 0} book_miss={s021._BOOK.misses if s021._BOOK else 0}",
        "",
        "--- missing_expiry diagnosis ---",
        f"listed_expiries union from s021 cache months: {sorted({str(x) for xs in listed_cache.values() for x in xs})}",
        f"marks DB DISTINCT expiry 2025-05-30..2025-06-06: {db_exps}",
        *miss_txt,
        (
            f"2DTE expiries are genuinely absent from the marks DB: {genuinely_absent_2dte}"
            if genuinely_absent_2dte
            else (
                "2DTE wanted dates are present in the marks DB (skip is listed_expiries/cache, not missing files)."
                if wanted_2dte
                else "no 2DTE missing_expiry events in this smoke window."
            )
        ),
        f"warmup_signals_dropped (not traded): {n_clipped}",
        f"c2_miss={c2_miss} volctrl_widen={vol_widen}",
        *(count_notes or ["control counts matched ULP for every tf/mode"]),
        "",
        "--- option-chain mark gaps (reporting only; handling unchanged) ---",
        f"gaps.csv n_windows={len(gap_rows)} expiries={sorted(str(x)[:10] for x in traded_exps)}",
    ]
    shown = gap_rows[:40]
    for g in shown:
        lines.append(
            f"  exp={g['expiry']} {g['ist_start']} -> {g['ist_end']} minutes={g['minutes']}"
        )
    if len(gap_rows) > 40:
        lines.append(f"  ... {len(gap_rows) - 40} more windows in gaps.csv")
    lines.append("path_mark_none paths by signal IST date:")
    if mark_none_by_date:
        for day in sorted(mark_none_by_date):
            lines.append(f"  {day} n={mark_none_by_date[day]}")
    else:
        lines.append("  (none)")
    lines += [
        "",
        dump_note,
        "",
        "--- 5 best cells arm=ulp tf=15m by mean (3-day PREVIEW only) ---",
    ]
    for r in ulp15_best:
        lines.append(
            f"  {r['mode']} {r['style']} X={r['X']} dte={r['dte']} a={r['a']} H={r.get('H')} "
            f"T={r['T']} SL={r['SL']} "
            f"n={r['n']} mean={r['mean']:.3f} win%={r['win_pct']:.1f} maxDD={r['maxDD']:.1f}"
        )
    lines.append("--- 5 worst cells arm=ulp tf=15m by mean (3-day PREVIEW only) ---")
    for r in ulp15_worst:
        lines.append(
            f"  {r['mode']} {r['style']} X={r['X']} dte={r['dte']} a={r['a']} H={r.get('H')} "
            f"T={r['T']} SL={r['SL']} "
            f"n={r['n']} mean={r['mean']:.3f} win%={r['win_pct']:.1f} maxDD={r['maxDD']:.1f}"
        )

    def _same_cell(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return all(
            a.get(k) == b.get(k)
            for k in ("tf", "mode", "style", "X", "dte", "a", "H", "T", "SL")
        )

    ulp_cells = [r for r in cell_rows if r["arm"] == "ulp" and int(r["n"]) > 0]
    best10 = sorted(ulp_cells, key=_mean_ok, reverse=True)[:10]
    lines.append("")
    lines.append("--- n cells with mean > 0 (net) ---")
    for arm in arms:
        npos = sum(1 for r in cell_rows if r["arm"] == arm and int(r["n"]) > 0 and float(r["mean"]) > 0)
        ncell = sum(1 for r in cell_rows if r["arm"] == arm and int(r["n"]) > 0)
        lines.append(f"  {arm}: {npos}/{ncell}")
    n_p, med_p, pct_p = _paired_ulp(cell_rows, "c2")
    n_v, med_v, pct_v = _paired_ulp(cell_rows, "volctrl")
    lines.append("--- paired ULP minus C2 / VOLCTRL (cells with n>0 both arms) ---")
    lines.append(
        f"  ULP-C2 n={n_p} median={med_p:.4f} pct_ULP_better={pct_p:.1f}%"
    )
    lines.append(
        f"  ULP-VOLCTRL n={n_v} median={med_v:.4f} pct_ULP_better={pct_v:.1f}%"
    )
    for tf in tfs:
        for mode in modes:
            sub = [r for r in cell_rows if r["tf"] == tf and r["mode"] == mode]
            n_p, med_p, pct_p = _paired_ulp(sub, "c2")
            n_v, med_v, pct_v = _paired_ulp(sub, "volctrl")
            lines.append(
                f"  {tf} {mode} ULP-C2 n={n_p} med={med_p:.4f} pct={pct_p:.1f}% | "
                f"ULP-VOL n={n_v} med={med_v:.4f} pct={pct_v:.1f}%"
            )
    lines.append("--- ULP median gross / cost / net per (X, H) (cell means) ---")
    xh_keys: list[tuple[Any, Any, Any]] = []
    seen_xh: set[tuple[Any, Any, Any]] = set()
    for r in cell_rows:
        if r["arm"] != "ulp":
            continue
        k = (r.get("style"), r.get("X"), r.get("H"))
        if k not in seen_xh:
            seen_xh.add(k)
            xh_keys.append(k)
    for st_k, x_k, h_k in xh_keys:
        rs = [
            r for r in cell_rows
            if r["arm"] == "ulp" and r.get("style") == st_k and r.get("X") == x_k
            and r.get("H") == h_k and int(r["n"]) > 0
        ]
        g = [float(r["mean_gross"]) for r in rs if r.get("mean_gross") == r.get("mean_gross")]
        c = [float(r["mean_cost"]) for r in rs if r.get("mean_cost") == r.get("mean_cost")]
        nts = [float(r["mean"]) for r in rs if r.get("mean") == r.get("mean")]
        lines.append(
            f"  {st_k} X={x_k} H={h_k} n_cells={len(rs)} median_gross={_median(g):.3f} "
            f"median_cost={_median(c):.3f} median_net={_median(nts):.3f}"
        )
    lines.append("--- avg entry premium per leg and avg slip_pts per fill by X ---")
    for (st_k, x_k) in sorted(set(list(x_prem.keys()) + list(x_spts.keys()))):
        prems = x_prem.get((st_k, x_k), [])
        spts = x_spts.get((st_k, x_k), [])
        lines.append(
            f"  {st_k} X={x_k} n_entry_legs={len(prems)} avg_entry_prem="
            f"{(sum(prems) / len(prems) if prems else float('nan')):.4f} "
            f"n_fills={len(spts)} avg_slip_pts="
            f"{(sum(spts) / len(spts) if spts else float('nan')):.4f}"
        )
    lines.append("--- best 10 ULP cells (C2 / VOLCTRL same cell side-by-side) ---")
    for r in best10:
        c2 = next((x for x in cell_rows if x["arm"] == "c2" and _same_cell(x, r)), None)
        vc = next((x for x in cell_rows if x["arm"] == "volctrl" and _same_cell(x, r)), None)
        pf = r.get("profit_factor", float("nan"))
        lines.append(
            f"  {r['tf']} {r['mode']} {r['style']} X={r['X']} dte={r['dte']} a={r['a']} "
            f"H={r.get('H')} T={r['T']} SL={r['SL']} "
            f"n={r['n']} mean={r['mean']:.3f} med={r['median']:.3f} worst={r['worst']:.3f} p5={r['p5']:.3f} "
            f"win%={r['win_pct']:.1f} maxDD={r['maxDD']:.1f} pf={pf} adj={r['avg_adj']:.2f} "
            f"fee={r['avg_fee']:.2f} slip={r['avg_slip']:.2f} "
            f"| C2 mean={c2['mean'] if c2 else float('nan')} n={c2['n'] if c2 else 0} "
            f"| VOL mean={vc['mean'] if vc else float('nan')} n={vc['n'] if vc else 0}"
        )
    lines.append("--- ULP net>0 cells: +-1 step neighbours in T, SL, H, X ---")
    t_vals = list(T_GRID)
    sl_vals = list(SL_GRID)
    h_vals = list(h_grid)
    x_vals = list(cfgs)
    ulp_idx = {
        (
            r.get("tf"), r.get("mode"), r.get("style"), int(r["X"]), int(r["dte"]),
            r.get("a"), r.get("H"), int(r["T"]), int(r["SL"]),
        ): r
        for r in cell_rows if r["arm"] == "ulp"
    }
    pos_ulp = [
        r for r in cell_rows
        if r["arm"] == "ulp" and int(r["n"]) > 0 and float(r["mean"]) > 0
    ]

    def _nb_ok(r0: dict[str, Any], axis: str, delta: int) -> str:
        style, xv = str(r0["style"]), int(r0["X"])
        hv, tv, slv = r0.get("H"), int(r0["T"]), int(r0["SL"])
        if axis == "T":
            i = t_vals.index(tv) if tv in t_vals else -1
            j = i + delta
            if i < 0 or j < 0 or j >= len(t_vals):
                return f"T{delta:+d}=edge"
            tv = int(t_vals[j])
        elif axis == "SL":
            i = sl_vals.index(slv) if slv in sl_vals else -1
            j = i + delta
            if i < 0 or j < 0 or j >= len(sl_vals):
                return f"SL{delta:+d}=edge"
            slv = int(sl_vals[j])
        elif axis == "H":
            i = h_vals.index(hv) if hv in h_vals else -1
            j = i + delta
            if i < 0 or j < 0 or j >= len(h_vals):
                return f"H{delta:+d}=edge"
            hv = h_vals[j]
        else:
            i = x_vals.index((style, xv)) if (style, xv) in x_vals else -1
            j = i + delta
            if i < 0 or j < 0 or j >= len(x_vals):
                return f"X{delta:+d}=edge"
            style, xv = x_vals[j]
        key = (
            r0.get("tf"), r0.get("mode"), style, int(xv), int(r0["dte"]),
            r0.get("a"), hv, int(tv), int(slv),
        )
        nb = ulp_idx.get(key)
        if nb is None or int(nb.get("n") or 0) <= 0:
            return f"{axis}{delta:+d}=missing"
        ok = float(nb["mean"]) > 0
        return f"{axis}{delta:+d}={'net>0' if ok else 'net<=0'}"

    if not pos_ulp:
        lines.append("  (no ULP cells with mean > 0)")
    for r in pos_ulp:
        bits = []
        all_ok = True
        for axis in ("T", "SL", "H", "X"):
            for delta in (-1, 1):
                lab = _nb_ok(r, axis, delta)
                bits.append(lab)
                if lab.endswith("net<=0") or lab.endswith("missing"):
                    all_ok = False
        lines.append(
            f"  {r['tf']} {r['mode']} {r['style']} X={r['X']} H={r.get('H')} "
            f"T={r['T']} SL={r['SL']} mean={r['mean']:.3f} all_nb={all_ok} | "
            + " ".join(bits)
        )
    lines.append("--- best cell per (tf, mode) by arm ---")
    for tf in tfs:
        for mode in modes:
            bits = []
            for arm in arms:
                cand = [r for r in cell_rows if r["arm"] == arm and r["tf"] == tf and r["mode"] == mode and int(r["n"]) > 0]
                cand = sorted(cand, key=_mean_ok, reverse=True)
                if cand:
                    b = cand[0]
                    bits.append(
                        f"{arm}: {b['style']} X={b['X']} dte={b['dte']} a={b['a']} H={b.get('H')} "
                        f"T={b['T']} SL={b['SL']} mean={b['mean']:.3f} n={b['n']}"
                    )
                else:
                    bits.append(f"{arm}: none")
            lines.append(f"  {tf} {mode} | " + " | ".join(bits))
    if best10:
        br = best10[0]
        sk = ("ulp", br["tf"], br["mode"], br["style"], int(br["X"]), int(br["dte"]), br["a"])
        evs = streams.get(sk, [])
        taken_best, _, _ = take_in_order(
            evs, exits_by_key, skip_by_key, float(br["T"]), float(br["SL"]), h=br.get("H"),
        )
        losers = [t for t in taken_best if float(t["net"]) <= 0]
        n_los = len(losers)
        n_prof_first = sum(1 for t in losers if float(t.get("peak_at_exit", 0.0) or 0.0) > 0)
        peaks = [float(t.get("peak_at_exit", float("nan"))) for t in losers]
        lines.append("--- loss anatomy best ULP cell (peak_net) ---")
        lines.append(
            f"  {br['tf']} {br['mode']} {br['style']} X={br['X']} dte={br['dte']} a={br['a']} "
            f"H={br.get('H')} T={br['T']} SL={br['SL']} "
            f"losers={n_los} share_profit_first={((100.0 * n_prof_first / n_los) if n_los else float('nan')):.1f}% "
            f"median_peak={_median(peaks) if peaks else float('nan'):.3f}"
        )
    lines.append("")
    text = "\n".join(lines)
    (archive.folder / "summary.txt").write_text(text + "\n", encoding="utf-8")
    (archive.folder / "smoke_meta.json").write_text(
        json.dumps(
            {
                "n_paths": n_paths,
                "n_ok": n_ok,
                "sim_s": sim_s,
                "s_per_path": per_path,
                "occ_s": occ_s,
                "n_taken": n_taken,
                "n_ulp_month": n_ulp_month,
                "n_month_paths_proj": n_month_paths,
                "projected_full_month_runtime_s": proj_s,
                "skip": skip,
                "src": src_tot,
                "n_events": n_ev_table,
                "db_expiries": db_exps,
                "dump_cell": list(dump_cell_used),
                "slip_pts": slip_pts,
                "slip_model": slip_model,
                "slip_mult": slip_mult,
                "reduced": reduced,
                "p5": p5,
                "grid": {
                    "X": [list(c) for c in cfgs],
                    "dte": list(dte_grid),
                    "a": grid_a,
                    "H": grid_h,
                    "T": list(T_GRID),
                    "SL": list(SL_GRID),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(text, flush=True)
    print("\n".join(basket_text[:80]), flush=True)
    print(f"wrote {cell_path}", flush=True)
    print(f"wrote {gap_path}", flush=True)
    print(f"wrote {xlsx_path}", flush=True)

    if not bool(args.smoke):
        for r in cell_rows:
            archive.add_config_result(
                {
                    "key": f"{r['arm']}|{r['tf']}|{r['mode']}|{r['style']}|{r['X']}|{r['dte']}|{r['a']}|{r.get('H')}|{r['T']}|{r['SL']}",
                    "month": month,
                    "tf": r["tf"],
                    "variant": r["style"],
                    "band": r["X"],
                    "band_mode": r["mode"],
                    "arm": r["arm"],
                    "dte": r["dte"],
                    "tgt": r["T"],
                    "sl": r["SL"],
                    "n": r["n"],
                    "mean": r["mean"],
                    "brokerage": r["avg_fee"],
                    "slippage": r["avg_slip"],
                    "win": r["win_pct"],
                }
            )
        for pick in best10:
            for arm in arms:
                cell = dict(pick)
                cell["arm"] = arm
                sk = (arm, pick["tf"], pick["mode"], pick["style"], int(pick["X"]), int(pick["dte"]), pick["a"])
                evs = streams.get(sk, [])
                taken, _, _ = take_in_order(
                    evs, exits_by_key, skip_by_key, float(pick["T"]), float(pick["SL"]),
                    h=pick.get("H"),
                )
                for rec in taken:
                    ev = rec["event"]
                    key = ev["path_key"]
                    t0 = int(ev["signal_ts"])
                    _eid, style, x, dte, a = key
                    ed = expiry_date(t0, int(dte))
                    exp_ts = int(s018.expiry_unix(ed))
                    minutes = np.arange(t0, exp_ts + 1, 60, dtype=np.int64)
                    pth = simulate_path(
                        mkt, t0, ed, int(x), str(style), _a_pct(a), minutes,
                        compact=False, record_fills=True,
                        slip_pts=slip_pts, h_grid=h_grid,
                        slip_model=slip_model, slip_mult=slip_mult, dte=int(dte),
                    )
                    if pth.ok:
                        _add_taken_trade(archive, rec, pth, cell, month, mkt)
        archive.finalize()
        _patch_legs_csv(archive.folder)
        mo = int(str(month).split("-")[1])
        mnames = (
            "", "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        )
        june_name = archive.folder / f"s022_{mnames[mo]}.xlsx"
        rpt = archive.folder / "report.xlsx"
        if rpt.exists():
            june_name.write_bytes(rpt.read_bytes())
            print(f"wrote {june_name}", flush=True)
        return

    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
