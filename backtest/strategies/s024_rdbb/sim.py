#!/usr/bin/env python3
"""S024 RDBB stage1: short strangle on RDBB signals vs C2/VOLCTRL/FILTCTRL."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE, estimate_option_fee  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.harness.run_archive import RunArchive, git_commit_hash  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import intrinsic  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s021_hedged_long_straddle import run_s021 as s021  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.run_s022 import (  # noqa: E402
    S021Market,
    _c2_events,
    _volctrl_events,
)
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    SEED,
    assign_quantile_bins,
    ffill_1m_grid,
    forbidden_mask,
    ist_hod,
    ist_hod_arr,
    rolling_pre_range_60,
)
from backtest.strategies.s023_obps.run_s023 import collect_listed  # noqa: E402
from backtest.strategies.s023_obps.sim import (  # noqa: E402
    DEFAULT_QTY,
    GST,
    dte0_dte1,
    expiry_unix,
    hours_to,
    opt_symbol,
)
from backtest.strategies.s024_rdbb.signals import (  # noqa: E402
    FILTERS,
    MOVE_N,
    MOVE_X,
    NOFILTER,
    RSI_SETS,
    TFS,
    apply_move_filter,
    iter_rdbb_signals,
    parse_filter,
    weekend_bucket,
)
from backtest.strategies.s024_rdbb.stage0 import (  # noqa: E402
    filter_pass_close_ts,
    filtctrl_events,
)

PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
CV = float(OPTIONS_CONTRACT_VALUE)
QTY = int(DEFAULT_QTY)
TARGET_USD = 150.0
SL0_USD = -100.0
SL_STEP = 5.0
SL_PEAK_BIN = 10.0
HOURS_1DTE_CUT = 3.0
SLIP_PER_1000 = 5.0
ARMS = ("F", "C2", "VOLCTRL", "FILTCTRL")
GATE_N = 30


def fee_fill(premium: float, spot: float, qty: int) -> float:
    """S023 estimate_option_fee × GST, no per-fill cap."""
    if int(qty) <= 0:
        return 0.0
    return float(estimate_option_fee(
        premium=float(premium), qty_lots=int(qty), btc_index=float(spot),
    )) * float(GST)


def fee_round_trip(spot: float, qty: int = QTY, premium: float = 500.0) -> float:
    """Two fills (open+close one leg, or two-leg open) at notional/cap × GST."""
    return 2.0 * fee_fill(premium, spot, qty)


def slip_usd(qty: int = QTY) -> float:
    return SLIP_PER_1000 * float(qty) / float(QTY)


def sl_level(peak: float) -> float:
    return SL0_USD + SL_STEP * math.floor(max(0.0, float(peak)) / SL_PEAK_BIN)


def choose_expiry(t: int, listed: list[date]) -> tuple[date | None, date | None, bool]:
    """Return (trade_exp, e0, used_1dte). 0DTE if hours_to(e0)>3h else 1DTE."""
    e0, e1 = dte0_dte1(int(t), listed)
    if e0 is None:
        return None, None, False
    late = hours_to(e0, int(t)) <= HOURS_1DTE_CUT
    if late:
        if e1 is None:
            return None, e0, True
        return e1, e0, True
    return e0, e0, False


def pick_premium_match(
    rows: list[dict[str, Any]],
    atm: float,
) -> tuple[float, float] | None:
    """Higher ATM premium stays ATM; other type matches that premium."""
    rc = d.row_strike(rows, True, float(atm))
    rp = d.row_strike(rows, False, float(atm))
    if rc is None or rp is None:
        return None
    pc = float(rc["mark"])
    pp = float(rp["mark"])
    if not (math.isfinite(pc) and math.isfinite(pp) and pc > 0 and pp > 0):
        return None
    if pc >= pp:
        puts = [r for r in rows if not bool(r["is_call"])]
        if not puts:
            return None
        best_p = min(puts, key=lambda r: (abs(float(r["mark"]) - pc), abs(float(r["strike"]) - float(atm))))
        return float(atm), float(best_p["strike"])
    calls = [r for r in rows if bool(r["is_call"])]
    if not calls:
        return None
    best_c = min(calls, key=lambda r: (abs(float(r["mark"]) - pp), abs(float(r["strike"]) - float(atm))))
    return float(best_c["strike"]), float(atm)


@dataclass
class BasketResult:
    ok: bool
    skip_reason: str = ""
    entry_ts: int = 0
    exit_ts: int = 0
    exit_fill_ts: int = 0
    exit_reason: str = ""
    call_k: float = float("nan")
    put_k: float = float("nan")
    call_px: float = float("nan")
    put_px: float = float("nan")
    call_exit: float = float("nan")
    put_exit: float = float("nan")
    exp: date | None = None
    used_1dte: bool = False
    peak: float = 0.0
    sl_level: float = SL0_USD
    gross: float = 0.0
    fees: float = 0.0
    slip: float = 0.0
    net: float = 0.0
    net_after_slip: float = 0.0
    net_fee3x: float = 0.0


def _mark(mkt: Any, symbol: str, t: int, exp: date) -> float | None:
    mkt.ensure(symbol, exp)
    q = mkt.quote(symbol, int(t))
    if q is None:
        return None
    return float(q[0])


def _mark_arr(mkt: Any, symbol: str, exp: date) -> tuple[int, Any] | None:
    mkt.ensure(symbol, exp)
    fn = getattr(mkt, "mark_array", None)
    if not callable(fn):
        return None
    packed = fn(symbol)
    if packed is None:
        return None
    t0, arr = packed
    return int(t0), arr


def simulate_basket(
    mkt: Any,
    listed: list[date],
    *,
    entry_ts: int,
    qty: int = QTY,
    max_minutes: int = 80_000,
) -> BasketResult:
    t0 = int(entry_ts)
    exp, e0, used_1dte = choose_expiry(t0, listed)
    if exp is None or e0 is None:
        return BasketResult(False, "missing_expiry")
    spot0 = mkt.spot(t0)
    if spot0 is None or float(spot0) <= 0:
        return BasketResult(False, "missing_spot")
    packed = mkt.chain(exp, t0)
    if packed is None:
        return BasketResult(False, "missing_chain")
    rows, _src = packed
    atm = d.atm_strike_of(list(rows), float(spot0))
    if atm is None:
        return BasketResult(False, "missing_atm")
    picked = pick_premium_match(list(rows), float(atm))
    if picked is None:
        return BasketResult(False, "missing_strike")
    k_c, k_p = picked
    rc = d.row_strike(list(rows), True, float(k_c))
    rp = d.row_strike(list(rows), False, float(k_p))
    if rc is None or rp is None:
        return BasketResult(False, "missing_strike")
    sc = opt_symbol(True, float(k_c), exp)
    sp = opt_symbol(False, float(k_p), exp)
    px_c = _mark(mkt, sc, t0, exp)
    px_p = _mark(mkt, sp, t0, exp)
    if px_c is None:
        px_c = float(rc["mark"])
    if px_p is None:
        px_p = float(rp["mark"])
    if px_c <= 0 or px_p <= 0:
        return BasketResult(False, "missing_mark")
    q = int(qty)
    fees = fee_fill(px_c, float(spot0), q) + fee_fill(px_p, float(spot0), q)
    slip = slip_usd(q) + slip_usd(q)
    t_exp = expiry_unix(exp)
    peak = 0.0
    sl_now = sl_level(peak)
    last_sl = sl_now
    reason = "EXPIRY"
    trig = t_exp
    fill_t = t_exp
    ex_c = px_c
    ex_p = px_p

    def gross_at(mc: float, mp: float) -> float:
        return (float(px_c) - float(mc) + float(px_p) - float(mp)) * float(q) * CV

    def settle() -> None:
        nonlocal reason, trig, fill_t, ex_c, ex_p, fees, slip
        reason = "EXPIRY"
        trig = t_exp
        fill_t = t_exp
        spot_e = mkt.spot(t_exp) or spot0
        ex_c = intrinsic(True, float(k_c), float(spot_e))
        ex_p = intrinsic(False, float(k_p), float(spot_e))
        fees = fee_fill(px_c, float(spot0), q) + fee_fill(px_p, float(spot0), q)
        slip = slip_usd(q) + slip_usd(q)

    acp = _mark_arr(mkt, sc, exp)
    app = _mark_arr(mkt, sp, exp)

    def px_at(packed: tuple[int, Any] | None, symbol: str, t: int) -> float | None:
        if packed is None:
            return _mark(mkt, symbol, t, exp)
        t_arr, arr = packed
        i = (int(t) // 60 * 60 - int(t_arr)) // 60
        if 0 <= i < len(arr) and np.isfinite(arr[i]):
            return float(arr[i])
        return _mark(mkt, symbol, t, exp)

    n = 0
    while n < int(max_minutes):
        t = t0 + n * 60
        if t >= t_exp:
            settle()
            break
        mc = px_at(acp, sc, t)
        mp = px_at(app, sp, t)
        if mc is None or mp is None:
            n += 1
            continue
        g = gross_at(mc, mp)
        if g > peak:
            peak = g
        sl_now = sl_level(peak)
        if sl_now < last_sl:
            sl_now = last_sl
        last_sl = sl_now
        hit = None
        if g >= TARGET_USD:
            hit = "TARGET"
        elif g <= sl_now:
            hit = "SL"
        if hit is not None:
            ft = t + 60
            if ft <= t_exp:
                fc = px_at(acp, sc, ft)
                fp = px_at(app, sp, ft)
                if fc is not None and fp is not None:
                    reason = hit
                    trig = t
                    fill_t = ft
                    ex_c, ex_p = float(fc), float(fp)
                    spot_x = mkt.spot(ft) or spot0
                    fees += fee_fill(ex_c, float(spot_x), q) + fee_fill(ex_p, float(spot_x), q)
                    slip += slip_usd(q) + slip_usd(q)
                    break
        n += 1
    else:
        settle()

    if reason == "EXPIRY":
        settle()
    gross = gross_at(ex_c, ex_p)
    net = gross - fees
    return BasketResult(
        True, "", t0, int(trig), int(fill_t), reason,
        float(k_c), float(k_p), float(px_c), float(px_p), float(ex_c), float(ex_p),
        exp, bool(used_1dte), float(peak), float(last_sl),
        float(gross), float(fees), float(slip), float(net),
        float(net - slip), float(gross - 3.0 * fees),
    )


def occupancy_walk(
    events: list[dict[str, Any]],
    mkt: Any,
    listed: list[date],
    qty: int = QTY,
) -> tuple[list[BasketResult], list[dict[str, Any]]]:
    taken: list[BasketResult] = []
    rows: list[dict[str, Any]] = []
    busy_until = 0
    for e in sorted(events, key=lambda x: int(x["signal_ts"])):
        t0 = int(e["signal_ts"])
        if t0 < busy_until:
            continue
        res = simulate_basket(mkt, listed, entry_ts=t0, qty=qty)
        if not res.ok:
            continue
        busy_until = int(res.exit_fill_ts)
        taken.append(res)
        rows.append(_trade_row(e, res))
    return taken, rows


def _trade_row(e: dict[str, Any], res: BasketResult) -> dict[str, Any]:
    return {
        "cell": e.get("cell", ""),
        "arm": e.get("arm", ""),
        "month": e.get("month", ""),
        "bucket": weekend_bucket(int(res.entry_ts)),
        "tf": e.get("tf", ""),
        "rsi_set": e.get("rsi_set", ""),
        "filter": e.get("filter", ""),
        "entry_ts": int(res.entry_ts),
        "exit_ts": int(res.exit_ts),
        "exit_fill_ts": int(res.exit_fill_ts),
        "entry_ist": s018.ist_str(int(res.entry_ts)),
        "exit_ist": s018.ist_str(int(res.exit_ts)),
        "exit_fill_ist": s018.ist_str(int(res.exit_fill_ts)),
        "call_strike": res.call_k,
        "put_strike": res.put_k,
        "call_premium": res.call_px,
        "put_premium": res.put_px,
        "expiry": res.exp.isoformat() if res.exp else "",
        "used_1dte": int(res.used_1dte),
        "peak": res.peak,
        "sl_level": res.sl_level,
        "exit_reason": res.exit_reason,
        "gross": res.gross,
        "fees": res.fees,
        "net": res.net,
        "slip": res.slip,
        "net_after_slip": res.net_after_slip,
        "net_fee3x": res.net_fee3x,
    }


def _window(month: str) -> tuple[int, int, date, date]:
    a, b = d.month_bounds(month)
    return to_unix(ist_dt(a, 0, 0)), to_unix(ist_dt(b + timedelta(days=1), 0, 0)), a, b


def _maxdd(nets: list[float]) -> float:
    eq = pk = dd = 0.0
    for x in nets:
        eq += float(x)
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    return dd


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    nets = [float(r["net"]) for r in rows]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        reasons[str(r["exit_reason"])] += 1
    return {
        "n": n,
        "win_pct": (100.0 * len(wins) / n) if n else float("nan"),
        "gross": sum(float(r["gross"]) for r in rows),
        "fees": sum(float(r["fees"]) for r in rows),
        "net": sum(nets),
        "net_after_slip": sum(float(r["net_after_slip"]) for r in rows),
        "net_fee3x": sum(float(r["net_fee3x"]) for r in rows),
        "avg_win": (sum(wins) / len(wins)) if wins else float("nan"),
        "avg_loss": (sum(losses) / len(losses)) if losses else float("nan"),
        "maxDD": _maxdd(nets),
        "n_TARGET": int(reasons.get("TARGET", 0)),
        "n_SL": int(reasons.get("SL", 0)),
        "n_EXPIRY": int(reasons.get("EXPIRY", 0)),
    }


def _neighbour_filters(filt: str) -> list[str]:
    parsed = parse_filter(filt)
    if parsed is None:
        return []
    n, x = parsed
    ni = list(MOVE_N)
    xi = list(MOVE_X)
    out: list[str] = []
    try:
        i = ni.index(int(n))
    except ValueError:
        return []
    try:
        j = xi.index(float(x))
    except ValueError:
        return []
    if i > 0:
        out.append(f"N{ni[i - 1]}_X{x:g}")
    if i < len(ni) - 1:
        out.append(f"N{ni[i + 1]}_X{x:g}")
    if j > 0:
        out.append(f"N{n}_X{xi[j - 1]:g}")
    if j < len(xi) - 1:
        out.append(f"N{n}_X{xi[j + 1]:g}")
    return out


def _gate_pass_arm(
    net_by_month: dict[str, float],
    n_total: int,
    months: list[str],
    diffs: dict[str, float],
) -> bool:
    if n_total < GATE_N:
        return False
    pos_m = sum(1 for m in months if float(net_by_month.get(m, 0.0)) > 0)
    total = sum(float(net_by_month.get(m, 0.0)) for m in months)
    if pos_m < 2 or total <= 0:
        return False
    for v in diffs.values():
        if not (math.isfinite(v) and v > 0):
            return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="2025-05,2025-06,2025-07")
    ap.add_argument("--tf", default="")
    ap.add_argument("--rsi-set", default="")
    ap.add_argument("--filter", default="")
    ap.add_argument("--count-only", action="store_true")
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    args = ap.parse_args()
    months = [m.strip() for m in str(args.months).split(",") if m.strip()]
    tfs = [args.tf] if args.tf else list(TFS)
    rsi_sets = [args.rsi_set] if args.rsi_set else list(RSI_SETS)
    filts = [args.filter] if args.filter else list(FILTERS)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = RUNS_DIR / f"{stamp}_s024_stage1"
    n = 0
    while folder.exists():
        n += 1
        folder = RUNS_DIR / f"{stamp}_s024_stage1_{n}"
    archive = RunArchive(
        "s024", "stage1", args, month=",".join(months),
        prereg_note="S024 RDBB stage1 option sim; uncap fee; $5 slip/leg; FILTCTRL",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    w0, _, d0, _ = _window(months[0])
    _, w1, _, d1 = _window(months[-1])
    spot = s018.load_spot_1m(args.csv)
    ts_all, o_all, h_all, l_all, c_all, vol_all = s020.bars_1m_vol(spot)
    lo = w0 - 7 * 86400
    hi = w1 + 4 * 86400
    sel = (ts_all >= lo) & (ts_all < hi)
    ts, o, h, l, c, vol = ts_all[sel], o_all[sel], h_all[sel], l_all[sel], c_all[sel], vol_all[sel]
    spot_c = {int(t): float(px) for t, px in zip(ts, c)}
    print(f"1m bars={len(ts)} months={months}", flush=True)

    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    pre = rolling_pre_range_60(gh, gl)
    bins, _ = assign_quantile_bins(pre)
    hod_g = ist_hod_arr(grid)
    hod_1m = ist_hod_arr(ts)
    cell_id = np.full(grid.shape, -1, dtype=np.int32)
    okb = bins >= 0
    cell_id[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)

    month_bounds = {ym: _window(ym) for ym in months}
    rng = np.random.default_rng(SEED)
    f_counts: dict[tuple[str, str, str, str], int] = {}
    packs: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)

    t_count0 = time.perf_counter()
    for tf in tfs:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = [
            {"time": int(t), "open": float(oo), "high": float(hh), "low": float(ll), "close": float(cc)}
            for t, oo, hh, ll, cc in zip(tts, to_, th, tl, tc)
        ]
        for rsi_set in rsi_sets:
            raw = iter_rdbb_signals(bars, tf, rsi_set, NOFILTER)
            raw_ts = np.array([int(e["signal_ts"]) for e in raw], dtype=np.int64)
            print(f"{tf} {rsi_set} nofilter_raw={len(raw)}", flush=True)
            pool_cache: dict[str, np.ndarray] = {}
            for filt in filts:
                sigs = apply_move_filter(bars, raw, filt)
                if filt != NOFILTER:
                    pool_cache[filt] = filter_pass_close_ts(bars, filt, tf_sec)
                for ym in months:
                    start, cutoff, _, _ = month_bounds[ym]
                    f_ev = []
                    for i, e in enumerate(s for s in sigs if start <= int(s["signal_ts"]) < cutoff):
                        t0 = int(e["signal_ts"])
                        row = {
                            "arm": "F",
                            "cell": f"{tf}|{rsi_set}|{filt}",
                            "tf": tf, "rsi_set": rsi_set, "filter": filt,
                            "month": ym, "signal_ts": t0,
                            "event_id": f"{tf}|{rsi_set}|{filt}|{ym}|{i}|{t0}",
                            "hod": ist_hod(t0), "mode": rsi_set,
                            "signal_price": float(spot_c.get(t0, e.get("signal_price") or 0)),
                        }
                        f_ev.append(row)
                    f_counts[(tf, rsi_set, filt, ym)] = len(f_ev)
                    sig_arr = np.array([int(x["signal_ts"]) for x in f_ev], dtype=np.int64)
                    near_g = forbidden_mask(grid, sig_arr, 3600)
                    near_1 = forbidden_mask(ts, sig_arr, 3600)
                    c2_raw, _ = _c2_events(f_ev, ts, near_1, hod_1m, rng, start, cutoff)
                    vol_raw, _ = _volctrl_events(f_ev, grid, cell_id, hod_g, rng, start, cutoff, near_g)
                    if filt == NOFILTER:
                        fc_raw = c2_raw
                    else:
                        fc_raw, _ = filtctrl_events(
                            f_ev, pool_cache[filt], raw_ts, rng, start, cutoff, tf, rsi_set, filt,
                        )

                    def _ctrl(raw_rows: list[dict[str, Any]], arm: str) -> list[dict[str, Any]]:
                        out: list[dict[str, Any]] = []
                        for r in raw_rows:
                            ct = int(r["signal_ts"])
                            out.append({
                                "arm": arm,
                                "cell": f"{tf}|{rsi_set}|{filt}",
                                "tf": tf, "rsi_set": rsi_set, "filter": filt,
                                "month": ym, "signal_ts": ct,
                                "event_id": r.get("event_id", ""),
                                "hod": int(r.get("hod") or ist_hod(ct)),
                                "mode": rsi_set,
                                "signal_price": float(spot_c.get(ct, float("nan"))),
                            })
                        return out

                    packs[(tf, rsi_set, filt, ym, "F")] = f_ev
                    packs[(tf, rsi_set, filt, ym, "C2")] = _ctrl(c2_raw, "C2")
                    packs[(tf, rsi_set, filt, ym, "VOLCTRL")] = _ctrl(vol_raw, "VOLCTRL")
                    packs[(tf, rsi_set, filt, ym, "FILTCTRL")] = _ctrl(fc_raw, "FILTCTRL")

    print("=== F signal counts (3 months) ===", flush=True)
    print(f"{'tf':>5} {'rsi':>4} {'filter':>10} " + " ".join(f"{m:>8}" for m in months) + " tot", flush=True)
    for tf in tfs:
        for rsi_set in rsi_sets:
            for filt in filts:
                vals = [int(f_counts.get((tf, rsi_set, filt, ym), 0)) for ym in months]
                print(
                    f"{tf:>5} {rsi_set:>4} {filt:>10} "
                    + " ".join(f"{v:>8}" for v in vals)
                    + f" {sum(vals):>5}",
                    flush=True,
                )
    probe = ("15m", "R3", "N14_X1", "2025-06")
    print(
        f"probe cell {probe[0]} {probe[1]} {probe[2]} {probe[3]} "
        f"n_F={f_counts.get(probe, 0)}",
        flush=True,
    )
    print(f"count_wall_s={time.perf_counter() - t_count0:.1f}", flush=True)
    if args.count_only:
        meta = {
            "git_hash": git_commit_hash(), "seed": int(SEED), "months": months,
            "folder": str(archive.folder).replace("\\", "/"),
            "count_only": True,
        }
        (archive.folder / "run_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        archive.finalize()
        return

    inner = MarksStore()
    store = d.MonthGuardStore(inner, d0 - timedelta(days=1), d1 + timedelta(days=14))
    s021._BOOK = s021.MarkBook(w0 - 86400, w1 + 4 * 86400)
    s021._CHAIN_S021 = {}
    mkt = S021Market(store, s021._BOOK, spot_c, grid, gc)
    listed = collect_listed(store, d0 - timedelta(days=2), d1 + timedelta(days=16))
    print(f"listed_expiries={len(listed)}", flush=True)

    # Time one cell-month then project.
    t_one = time.perf_counter()
    probe_ev = packs.get(("15m", "R3", "N14_X1", "2025-06", "F"), [])
    occupancy_walk(probe_ev, mkt, listed, QTY)
    one_s = time.perf_counter() - t_one
    n_jobs = len(tfs) * len(rsi_sets) * len(filts) * len(months) * len(ARMS)
    n_f_total = sum(f_counts.values())
    proj = one_s * max(1.0, n_f_total / max(1, len(probe_ev))) * len(ARMS)
    print(
        f"timing 15m R3 N14_X1 2025-06 F n={len(probe_ev)} wall_s={one_s:.2f} "
        f"projected_s={proj:.0f} (~{proj / 60:.1f} min) jobs={n_jobs}",
        flush=True,
    )

    trade_cols = [
        "cell", "arm", "month", "bucket", "tf", "rsi_set", "filter",
        "entry_ts", "exit_ts", "exit_fill_ts", "entry_ist", "exit_ist", "exit_fill_ist",
        "call_strike", "put_strike", "call_premium", "put_premium", "expiry", "used_1dte",
        "peak", "sl_level", "exit_reason", "gross", "fees", "net", "slip",
        "net_after_slip", "net_fee3x",
    ]
    trades_path = archive.folder / "trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as fp:
        csv.DictWriter(fp, fieldnames=trade_cols).writeheader()

    all_rows: list[dict[str, Any]] = []
    t_run = time.perf_counter()
    for tf in tfs:
        for rsi_set in rsi_sets:
            for filt in filts:
                t_cell = time.perf_counter()
                ns: dict[str, int] = {}
                for arm in ARMS:
                    evs: list[dict[str, Any]] = []
                    for ym in months:
                        evs.extend(packs[(tf, rsi_set, filt, ym, arm)])
                    _taken, rows = occupancy_walk(evs, mkt, listed, QTY)
                    ns[arm] = len(rows)
                    all_rows.extend(rows)
                    with trades_path.open("a", newline="", encoding="utf-8") as fp:
                        w = csv.DictWriter(fp, fieldnames=trade_cols)
                        for row in rows:
                            w.writerow(row)
                print(
                    f"{tf} {rsi_set} {filt} "
                    f"F={ns['F']} C2={ns['C2']} V={ns['VOLCTRL']} FC={ns['FILTCTRL']} "
                    f"wall_s={time.perf_counter() - t_cell:.1f}",
                    flush=True,
                )
    print(f"sim_wall_s={time.perf_counter() - t_run:.1f} trades={len(all_rows)}", flush=True)

    sum_rows: list[dict[str, Any]] = []
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in all_rows:
        if str(r["bucket"]) == "WEEKEND":
            grouped[(r["tf"], r["rsi_set"], r["filter"], r["arm"], r["month"], "WEEKEND")].append(r)
        grouped[(r["tf"], r["rsi_set"], r["filter"], r["arm"], r["month"], "ANY")].append(r)
    for key, rs in grouped.items():
        tf, rsi_set, filt, arm, ym, bucket = key
        st = _stats(rs)
        st.update({
            "tf": tf, "rsi_set": rsi_set, "filter": filt, "arm": arm,
            "month": ym, "bucket": bucket, "cell": f"{tf}|{rsi_set}|{filt}",
        })
        sum_rows.append(st)
    sum_cols = [
        "cell", "tf", "rsi_set", "filter", "arm", "month", "bucket",
        "n", "win_pct", "gross", "fees", "net", "net_after_slip", "net_fee3x",
        "avg_win", "avg_loss", "maxDD", "n_TARGET", "n_SL", "n_EXPIRY",
    ]
    with (archive.folder / "summary.csv").open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=sum_cols, extrasaction="ignore")
        w.writeheader()
        for row in sum_rows:
            w.writerow(row)

    def _agg(tf: str, rsi: str, filt: str, arm: str) -> tuple[dict[str, float], int, float]:
        by_m = {m: 0.0 for m in months}
        n = 0
        tot = 0.0
        for r in sum_rows:
            if r["tf"] == tf and r["rsi_set"] == rsi and r["filter"] == filt and r["arm"] == arm and r["bucket"] == "ANY":
                by_m[str(r["month"])] = float(r["net"])
                n += int(r["n"])
                tot += float(r["net"])
        return by_m, n, tot

    gate_rows: list[dict[str, Any]] = []
    f_pass = 0
    c2_null = 0
    for tf in tfs:
        for rsi_set in rsi_sets:
            for filt in filts:
                nets: dict[str, tuple[dict[str, float], int, float]] = {}
                for arm in ARMS:
                    nets[arm] = _agg(tf, rsi_set, filt, arm)
                f_m, n_f, tot_f = nets["F"]
                diffs_f = {
                    "F_C2": tot_f - nets["C2"][2],
                    "F_VOLCTRL": tot_f - nets["VOLCTRL"][2],
                    "F_FILTCTRL": tot_f - nets["FILTCTRL"][2],
                }
                ok_f = _gate_pass_arm(f_m, n_f, months, diffs_f)
                c_m, n_c, tot_c = nets["C2"]
                diffs_c = {
                    "C2_F": tot_c - tot_f,
                    "C2_VOLCTRL": tot_c - nets["VOLCTRL"][2],
                    "C2_FILTCTRL": tot_c - nets["FILTCTRL"][2],
                }
                ok_c = _gate_pass_arm(c_m, n_c, months, diffs_c)
                neigh = _neighbour_filters(filt)
                n_ok = 0
                for nf in neigh:
                    nm, nn, nt = _agg(tf, rsi_set, nf, "F")
                    nd = {
                        "F_C2": nt - _agg(tf, rsi_set, nf, "C2")[2],
                        "F_VOLCTRL": nt - _agg(tf, rsi_set, nf, "VOLCTRL")[2],
                        "F_FILTCTRL": nt - _agg(tf, rsi_set, nf, "FILTCTRL")[2],
                    }
                    if _gate_pass_arm(nm, nn, months, nd):
                        n_ok += 1
                robust = ok_f and n_ok >= 2
                if ok_f:
                    f_pass += 1
                if ok_c:
                    c2_null += 1
                row = {
                    "cell": f"{tf}|{rsi_set}|{filt}",
                    "tf": tf, "rsi_set": rsi_set, "filter": filt,
                    "n_F": n_f, "net_F": tot_f,
                    "F_C2": diffs_f["F_C2"], "F_VOLCTRL": diffs_f["F_VOLCTRL"],
                    "F_FILTCTRL": diffs_f["F_FILTCTRL"],
                    "gate_F": "PASS" if ok_f else "FAIL",
                    "robust": "ROBUST PASS" if robust else "ROBUST FAIL",
                    "n_neighbours": len(neigh), "n_neighbours_pass": n_ok,
                    "gate_C2_null": "PASS" if ok_c else "FAIL",
                    "n_C2": n_c, "net_C2": tot_c,
                }
                for m in months:
                    row[f"net_F_{m}"] = f_m.get(m, 0.0)
                gate_rows.append(row)
    gate_cols = list(gate_rows[0].keys()) if gate_rows else ["cell"]
    with (archive.folder / "gate.csv").open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=gate_cols, extrasaction="ignore")
        w.writeheader()
        for row in sorted(gate_rows, key=lambda r: (-int(r["gate_F"] == "PASS"), -float(r["net_F"]))):
            w.writerow(row)
    print(f"gate F_PASS={f_pass} C2_null_PASS={c2_null} cells={len(gate_rows)}", flush=True)
    meta = {
        "git_hash": git_commit_hash(),
        "seed": int(SEED),
        "months": months,
        "folder": str(archive.folder).replace("\\", "/"),
        "n_trades": len(all_rows),
        "f_pass": f_pass,
        "c2_null_pass": c2_null,
    }
    archive.finalize()
    (archive.folder / "run_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
