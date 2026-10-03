#!/usr/bin/env python3
"""S017 ST-flip + engulfing scalper (BTCUSD perp points, 1m).

python backtest\\strategies\\s017_st_engulf_scalp\\run_s017.py --max-days 3
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    Bar1m,
    load_spot_1m,
    supertrend,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s017")

SPOT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
OUT_DIR = Path("backtest/strategies/s017_st_engulf_scalp/runs")
DATA_FROM = date(2024, 7, 1)

ST_LEN = 20
ST_MULT = 1.0
ENGULF_BODY = 0.40
SLIP = 0.5
SL_MODES: tuple[object, ...] = ("candle", 10, 20, 30, 40, 50)
M_LIST = (1, 2, 3, 5, 10)
FEE_MODES = ("F0", "F1", "F2", "F3")
TAKER = 0.0005
MAKER = 0.0002
GST = 1.18
RANDOM_SEEDS = (1, 2, 3, 4, 5)
SENS_LEN = (10, 20, 30)
SENS_MULT = (1.0, 2.0, 3.0)

PERIODS = ("2024H2", "2025", "2026")


@dataclass
class Sig:
    i: int
    ts: int
    side: str  # long | short
    o: float
    h: float
    l: float
    c: float
    po: float
    ph: float
    pl: float
    pc: float
    st_prev: int
    st_now: int


@dataclass
class Trade:
    cell: str
    st_len: int
    st_mult: float
    sl_mode: str
    m: int
    fee_mode: str
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    sl: float
    tp: float
    exit: float
    reason: str
    risk: float
    r_mult: float
    gross: float
    fee: float
    net: float
    amb: int
    period: str


def period_of(ts: int) -> str:
    d = datetime.fromtimestamp(int(ts), tz=UTC).date()
    if date(2024, 7, 1) <= d <= date(2024, 12, 31):
        return "2024H2"
    if date(2025, 1, 1) <= d <= date(2025, 12, 31):
        return "2025"
    if d.year == 2026:
        return "2026"
    return "OUT"


def ist_str(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def bars_arr(spot: dict[int, Bar1m]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ts = np.array(sorted(spot), dtype=np.int64)
    o = np.array([spot[int(t)].open for t in ts], dtype=np.float64)
    h = np.array([spot[int(t)].high for t in ts], dtype=np.float64)
    l = np.array([spot[int(t)].low for t in ts], dtype=np.float64)
    c = np.array([spot[int(t)].close for t in ts], dtype=np.float64)
    return ts, o, h, l, c


def engulf_bull(o0: float, c0: float, o1: float, c1: float) -> bool:
    if not (c1 > o1 and c0 < o0 and o1 <= c0 and c1 >= o0):
        return False
    prev = abs(c0 - o0)
    cur = abs(c1 - o1)
    return cur > 0 and prev < ENGULF_BODY * cur


def engulf_bear(o0: float, c0: float, o1: float, c1: float) -> bool:
    if not (c1 < o1 and c0 > o0 and o1 >= c0 and c1 <= o0):
        return False
    prev = abs(c0 - o0)
    cur = abs(c1 - o1)
    return cur > 0 and prev < ENGULF_BODY * cur


def collect_signals(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    trend: np.ndarray,
    start_ts: int,
    cutoff_ts: int | None,
) -> list[Sig]:
    out: list[Sig] = []
    n = len(ts)
    for i in range(1, n):
        t = int(ts[i])
        if t < start_ts:
            continue
        if cutoff_ts is not None and t >= cutoff_ts:
            break
        tr0, tr1 = int(trend[i - 1]), int(trend[i])
        if tr0 not in (-1, 1) or tr1 not in (-1, 1):
            continue
        side = ""
        if tr0 == -1 and tr1 == 1 and engulf_bull(float(o[i - 1]), float(c[i - 1]), float(o[i]), float(c[i])):
            side = "long"
        elif tr0 == 1 and tr1 == -1 and engulf_bear(float(o[i - 1]), float(c[i - 1]), float(o[i]), float(c[i])):
            side = "short"
        if not side:
            continue
        out.append(
            Sig(
                i=i, ts=t, side=side,
                o=float(o[i]), h=float(h[i]), l=float(l[i]), c=float(c[i]),
                po=float(o[i - 1]), ph=float(h[i - 1]), pl=float(l[i - 1]), pc=float(c[i - 1]),
                st_prev=tr0, st_now=tr1,
            )
        )
    return out


def sl_tp(side: str, entry: float, open_x: float, sl_mode: object, m: int) -> tuple[float, float, float] | None:
    if sl_mode == "candle":
        sl = float(open_x)
    else:
        pts = float(sl_mode)
        sl = entry - pts if side == "long" else entry + pts
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if side == "long":
        tp = entry + risk * float(m)
    else:
        tp = entry - risk * float(m)
    return sl, tp, risk


def sl_fill(side: str, sl: float, o: float) -> float:
    if side == "long":
        return float(o) if o < sl else sl - SLIP
    return float(o) if o > sl else sl + SLIP


def walk(
    i: int,
    side: str,
    sl: float,
    tp: float,
    h: np.ndarray,
    l: np.ndarray,
    o: np.ndarray,
    ts: np.ndarray,
) -> tuple[int, float, str, int] | None:
    n = len(ts)
    for j in range(i + 1, n):
        hit_sl = float(l[j]) <= sl if side == "long" else float(h[j]) >= sl
        hit_tp = float(h[j]) >= tp if side == "long" else float(l[j]) <= tp
        if hit_sl and hit_tp:
            return j, sl_fill(side, sl, float(o[j])), "SL", 1
        if hit_sl:
            return j, sl_fill(side, sl, float(o[j])), "SL", 0
        if hit_tp:
            return j, float(tp), "TP", 0
    return None


def fee_pts(price: float, rate: float) -> float:
    return abs(float(price)) * float(rate) * GST


def apply_fee(mode: str, side: str, entry: float, exit_px: float, reason: str, hold_min: float) -> float:
    if mode == "F0":
        return 0.0
    fe = fee_pts(entry, TAKER)
    if mode == "F1":
        return fe + fee_pts(exit_px, TAKER)
    # F2 / F3
    if reason == "TP":
        fx = fee_pts(exit_px, MAKER)
    else:
        fx = fee_pts(exit_px, TAKER)
    if mode == "F3" and hold_min <= 30.0:
        fx = 0.0
    return fe + fx


def simulate(
    sigs: list[Sig],
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    sl_mode: object,
    m: int,
    st_len: int,
    st_mult: float,
    fee_mode: str,
) -> list[Trade]:
    trades: list[Trade] = []
    busy = -1
    cell = f"SL={sl_mode}|M={m}|ST={st_len}x{st_mult}"
    for s in sigs:
        if s.i <= busy:
            continue
        per = period_of(s.ts)
        if per == "OUT":
            continue
        entry = s.c + SLIP if s.side == "long" else s.c - SLIP
        stp = sl_tp(s.side, entry, s.o, sl_mode, m)
        if stp is None:
            continue
        sl, tp, risk = stp
        walked = walk(s.i, s.side, sl, tp, h, l, o, ts)
        if walked is None:
            continue
        j, fill, reason, amb = walked
        gross = (fill - entry) if s.side == "long" else (entry - fill)
        hold = (int(ts[j]) - int(s.ts)) / 60.0
        fee = apply_fee(fee_mode, s.side, entry, fill, reason, hold)
        net = gross - fee
        r_mult = net / risk if risk else float("nan")
        trades.append(
            Trade(
                cell=cell, st_len=st_len, st_mult=st_mult,
                sl_mode=str(sl_mode), m=int(m), fee_mode=fee_mode,
                side=s.side, entry_ts=int(s.ts), exit_ts=int(ts[j]),
                entry=entry, sl=sl, tp=tp, exit=fill, reason=reason,
                risk=risk, r_mult=r_mult, gross=gross, fee=fee, net=net,
                amb=amb, period=per,
            )
        )
        busy = j
    return trades


def random_trades(
    n_need: int,
    period: str,
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    sl_mode: object,
    m: int,
    fee_mode: str,
    seed: int,
) -> list[Trade]:
    rng = np.random.default_rng(int(seed))
    idx = [i for i in range(1, len(ts) - 2) if period_of(int(ts[i])) == period]
    if not idx or n_need <= 0:
        return []
    order = rng.permutation(idx)
    fake: list[Sig] = []
    for i in order:
        if len(fake) >= n_need * 4:
            break
        side = "long" if rng.random() < 0.5 else "short"
        fake.append(
            Sig(
                i=int(i), ts=int(ts[i]), side=side,
                o=float(o[i]), h=float(h[i]), l=float(l[i]), c=float(c[i]),
                po=float(o[i - 1]), ph=float(h[i - 1]), pl=float(l[i - 1]),
                pc=float(c[i - 1]), st_prev=0, st_now=0,
            )
        )
    return simulate(fake, ts, o, h, l, sl_mode, m, ST_LEN, ST_MULT, fee_mode)[:n_need]


def max_dd(nets: list[float]) -> float:
    eq = peak = 0.0
    dd = 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def lose_streak(nets: list[float]) -> int:
    cur = worst = 0
    for n in nets:
        if n < 0:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def _mean(xs: list[float]) -> float:
    return float(np.mean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(np.median(xs)) if xs else float("nan")


def summarize(tr: list[Trade], ndays: int) -> dict[str, float]:
    n = len(tr)
    nets = [t.net for t in tr]
    return {
        "n": n,
        "per_day": n / ndays if ndays else float("nan"),
        "win": 100.0 * sum(1 for t in tr if t.net > 0) / n if n else float("nan"),
        "risk_mean": _mean([t.risk for t in tr]),
        "risk_med": _med([t.risk for t in tr]),
        "avg_r": _mean([t.r_mult for t in tr]),
        "net_pt": _mean(nets),
        "total": float(sum(nets)) if nets else 0.0,
        "maxdd": max_dd(nets),
        "streak": float(lose_streak(nets)),
        "amb": float(sum(t.amb for t in tr)),
    }


def sl_neighbors(sl: object) -> list[object]:
    seq = list(SL_MODES)
    i = seq.index(sl)
    out = []
    if i > 0:
        out.append(seq[i - 1])
    if i + 1 < len(seq):
        out.append(seq[i + 1])
    return out


def m_neighbors(m: int) -> list[int]:
    seq = list(M_LIST)
    i = seq.index(m)
    out = []
    if i > 0:
        out.append(seq[i - 1])
    if i + 1 < len(seq):
        out.append(seq[i + 1])
    return out


def check_30s() -> str:
    lines = [
        "=== 30s CHECK (no download) ===",
        "download_candles.py RESOLUTION_SECONDS has: 1m,3m,5m,15m — no 30s",
        "Delta GET /v2/history/candles resolution=30s -> HTTP 400",
        "Allowed values: 5s,1m,3m,5m,15m,30m,1h,2h,4h,6h,1d,1w",
        "30s supported: NO (5s yes, 30s no)",
        "",
        "=== Binance BTCUSDT 1s klines estimate (NOT downloaded) ===",
        "Source: data.binance.vision spot monthly 1s zips",
        "HEAD BTCUSDT-1s-2024-07.zip = 78.8 MB  2025-01 = 85.0 MB  2025-06 = 71.7 MB  2026-09 = 404",
        "Window 2024-07..2026-09 = 27 months; ~26 published * ~78 MB ~ 2.0 GB compressed",
        "Uncompressed CSV ~8-12x zip -> roughly 16-24 GB on disk",
        "Rows ~ 86400 s/day * ~820 days ~ 71e6 one-second bars",
        "Download time ~15-40 min on a typical home link; parse/load several hours",
        "",
    ]
    return "\n".join(lines)


def fmt_sum(s: dict[str, float], rnd: float | None = None) -> str:
    extra = "" if rnd is None else f" rnd_net={rnd:.4f}"
    return (
        f"n={int(s['n'])} /day={s['per_day']:.3f} win%={s['win']:.1f} "
        f"risk={s['risk_mean']:.2f} (med={s['risk_med']:.2f}) avgR={s['avg_r']:.3f} "
        f"net/t={s['net_pt']:.4f} total={s['total']:.1f} maxDD={s['maxdd']:.1f} "
        f"loseStreak={int(s['streak'])} amb={int(s['amb'])}{extra}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=SPOT_CSV)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--max-days", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    head = check_30s()
    print(head, flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    logger.info("loading %s", args.csv)
    spot = load_spot_1m(args.csv)
    ts, o, h, l, c = bars_arr(spot)
    start_ts = int(datetime(2024, 7, 1, tzinfo=UTC).timestamp())
    cutoff = None
    if args.max_days:
        cutoff = start_ts + int(args.max_days) * 86400
        lo = start_ts - 3 * 86400
        hi = cutoff + 2 * 86400
        sel = (ts >= lo) & (ts < hi)
        ts, o, h, l, c = ts[sel], o[sel], h[sel], l[sel], c[sel]
        print(f"SMOKE max-days={args.max_days} start={DATA_FROM} cutoff_ts={cutoff}", flush=True)

    logger.info("supertrend len=%s mult=%s", ST_LEN, ST_MULT)
    trend, _st = supertrend(h, l, c, ST_LEN, ST_MULT)
    sigs = collect_signals(ts, o, h, l, c, trend, start_ts, cutoff)
    print(f"signals={len(sigs)}", flush=True)

    print("=== HAND-CHECK 3 signals ===")
    for s in sigs[:3]:
        print(
            f"  {s.side} X={ist_str(s.ts)} ST {s.st_prev}->{s.st_now} "
            f"X-1 OHLC=({s.po:.1f},{s.ph:.1f},{s.pl:.1f},{s.pc:.1f}) "
            f"X OHLC=({s.o:.1f},{s.h:.1f},{s.l:.1f},{s.c:.1f})"
        )
    if not sigs:
        print("  (no signals in window)")

    cal_days = {}
    for per in PERIODS:
        days = {period_of(int(t)) for t in ts if period_of(int(t)) == per}
        # unique calendar days in data for period
        ds = set()
        for t in ts:
            if cutoff is not None and int(t) >= cutoff:
                break
            if int(t) < start_ts:
                continue
            p = period_of(int(t))
            if p == per:
                ds.add(datetime.fromtimestamp(int(t), tz=UTC).date())
        cal_days[per] = max(len(ds), 1)

    f2_by_cell: dict[tuple, dict[str, list[Trade]]] = {}
    lines = [
        "S017 ST-flip engulfing scalper 1m",
        f"stamp={stamp} ST=({ST_LEN},{ST_MULT}) signals={len(sigs)}",
        head,
    ]

    all_f2: list[Trade] = []
    for sl in SL_MODES:
        for m in M_LIST:
            path = simulate(sigs, ts, o, h, l, sl, m, ST_LEN, ST_MULT, "F2")
            # other fees from same path geometry: re-sim cheap
            by_fee = {"F2": path}
            for fm in ("F0", "F1", "F3"):
                by_fee[fm] = simulate(sigs, ts, o, h, l, sl, m, ST_LEN, ST_MULT, fm)
            cell = f"SL={sl}|M={m}"
            f2_by_cell[(str(sl), int(m))] = {}
            for per in PERIODS:
                f2_by_cell[(str(sl), int(m))][per] = [t for t in path if t.period == per]
            lines.append(f"CELL {cell}")
            for fm in FEE_MODES:
                for per in PERIODS:
                    rows = [t for t in by_fee[fm] if t.period == per]
                    sm = summarize(rows, cal_days[per])
                    rnd_net = float("nan")
                    if fm == "F2" and not args.max_days:
                        nneed = len(rows)
                        rnds = []
                        for seed in RANDOM_SEEDS:
                            rt = random_trades(
                                nneed, per, ts, o, h, l, c, sl, m, fm, seed
                            )
                            rnds.append(_mean([t.net for t in rt]))
                        rnd_net = _mean([x for x in rnds if np.isfinite(x)])
                    elif fm == "F2":
                        nneed = len(rows)
                        rnds = []
                        for seed in RANDOM_SEEDS:
                            rt = random_trades(
                                nneed, per, ts, o, h, l, c, sl, m, fm, seed
                            )
                            rnds.append(_mean([t.net for t in rt]))
                        rnd_net = _mean([x for x in rnds if np.isfinite(x)])
                    extra = rnd_net if fm == "F2" else None
                    lines.append(f"  {fm} {per} {fmt_sum(sm, extra)}")
                    if fm == "F2":
                        f2_by_cell[(str(sl), int(m))][per + "_rnd"] = rnd_net  # type: ignore[assignment]
            all_f2.extend(path)

    lines.append("")
    lines.append("=== SENSITIVITY ST ATR x mult (SL=candle M=2 F2, not PASS) ===")
    for ln in SENS_LEN:
        for mu in SENS_MULT:
            if ln == ST_LEN and abs(mu - ST_MULT) < 1e-9:
                tr = simulate(sigs, ts, o, h, l, "candle", 2, ST_LEN, ST_MULT, "F2")
            else:
                trd, _ = supertrend(h, l, c, ln, mu)
                sg = collect_signals(ts, o, h, l, c, trd, start_ts, cutoff)
                tr = simulate(sg, ts, o, h, l, "candle", 2, ln, mu, "F2")
            lines.append(f"ST={ln}x{mu}")
            for per in PERIODS:
                rows = [t for t in tr if t.period == per]
                lines.append(f"  F2 {per} {fmt_sum(summarize(rows, cal_days[per]))}")

    def cell_pos(sl: object, m: int) -> bool:
        ok = True
        for per in PERIODS:
            rows = f2_by_cell[(str(sl), int(m))].get(per, [])
            if not isinstance(rows, list) or not rows:
                return False
            mn = _mean([t.net for t in rows])
            if not (np.isfinite(mn) and mn > 0):
                return False
        return ok

    lines.append("")
    lines.append("--- PRE-REGISTERED PASS (F2) ---")
    pass_cells: list[str] = []
    for sl in SL_MODES:
        for m in M_LIST:
            recs = f2_by_cell[(str(sl), int(m))]
            ok = True
            bits = []
            for per in PERIODS:
                rows = recs[per]
                n = len(rows)
                mn = _mean([t.net for t in rows])
                rnd = recs.get(per + "_rnd", float("nan"))
                bits.append(f"{per} n={n} net={mn} rnd={rnd}")
                if not (
                    n >= 100
                    and np.isfinite(mn)
                    and mn > 0
                    and isinstance(rnd, float)
                    and np.isfinite(rnd)
                    and mn > rnd
                ):
                    ok = False
            adj = 0
            for nsl in sl_neighbors(sl):
                if cell_pos(nsl, m):
                    adj += 1
            for nm in m_neighbors(m):
                if cell_pos(sl, nm):
                    adj += 1
            bits.append(f"adj_pos={adj}")
            if ok and adj >= 2:
                pass_cells.append(f"SL={sl}|M={m} " + "; ".join(bits))
            else:
                lines.append(f"FAIL-cand SL={sl}|M={m} adj={adj} | " + " | ".join(bits))
    if pass_cells:
        lines.append("PASS")
        for p in pass_cells:
            lines.append("  " + p)
    else:
        lines.append("PASS cells: none")
        lines.append("FAIL")

    text = "\n".join(lines) + "\n"
    txt_path = out_dir / f"s017_1m_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    csv_path = out_dir / f"s017_1m_{stamp}_trades.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "cell", "entry_ts_ist", "dir", "entry", "sl", "tp", "exit",
                "exit_reason", "pts_gross", "fee_pts", "net", "period", "amb",
            ],
        )
        w.writeheader()
        for t in all_f2:
            w.writerow(
                {
                    "cell": t.cell,
                    "entry_ts_ist": ist_str(t.entry_ts),
                    "dir": t.side,
                    "entry": t.entry,
                    "sl": t.sl,
                    "tp": t.tp,
                    "exit": t.exit,
                    "exit_reason": t.reason,
                    "pts_gross": t.gross,
                    "fee_pts": t.fee,
                    "net": t.net,
                    "period": t.period,
                    "amb": t.amb,
                }
            )
    print(text)
    print(f"wrote {txt_path}")
    if args.max_days:
        print(f"SMOKE done days={args.max_days} signals={len(sigs)}")


if __name__ == "__main__":
    main()
