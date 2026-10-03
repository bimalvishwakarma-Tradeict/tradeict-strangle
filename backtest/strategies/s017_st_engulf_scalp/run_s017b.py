#!/usr/bin/env python3
"""S017B gate: 3m/5m S017 signal + candle SL + M2/M3/M5 or ST-flip exit.

Does not modify S017 1m runner. Import helpers only.

python backtest\\strategies\\s017_st_engulf_scalp\\run_s017b.py --max-days 3
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.strategies.s012_trend_follow.engine import supertrend  # noqa: E402
from backtest.strategies.s017_st_engulf_scalp.run_s017 import (  # noqa: E402
    DATA_FROM,
    PERIODS,
    RANDOM_SEEDS,
    SLIP,
    SPOT_CSV,
    Sig,
    apply_fee,
    bars_arr,
    collect_signals,
    ist_str,
    load_spot_1m,
    period_of,
    sl_fill,
)

IST_UTC = timezone.utc
logger = logging.getLogger("s017b")

OUT_DIR = Path("backtest/strategies/s017_st_engulf_scalp/runs")
TFS = (3, 5)
ATRS = (10, 20)
MULTS = (1.0, 2.0, 3.0)
EXITS = ("M2", "M3", "M5", "STFLIP")
M_OF = {"M2": 2, "M3": 3, "M5": 5}


@dataclass
class TradeB:
    cell: str
    tf: int
    st_len: int
    st_mult: float
    exit_kind: str
    side: str
    entry_ts: int
    exit_ts: int
    entry: float
    sl: float
    tp: float
    exit: float
    reason: str
    risk: float
    hold_min: float
    gross: float
    fee: float
    net: float
    amb: int
    period: str


def resample_complete(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    tf_min: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """UTC-floor buckets labeled by open. Keep bar only if all tf_min 1m slots exist."""
    step = int(tf_min) * 60
    n = len(ts)
    buckets: dict[int, list[int]] = {}
    for i in range(n):
        key = (int(ts[i]) // step) * step
        buckets.setdefault(key, []).append(i)
    ots: list[int] = []
    oo: list[float] = []
    hh: list[float] = []
    ll: list[float] = []
    cc: list[float] = []
    for key in sorted(buckets):
        idxs = buckets[key]
        last_need = key + step - 60
        have = {int(ts[i]) for i in idxs}
        if len(have) != int(tf_min) or last_need not in have:
            continue
        first = idxs[0]
        last = idxs[-1]
        ots.append(key)
        oo.append(float(o[first]))
        hh.append(float(np.max(h[idxs])))
        ll.append(float(np.min(l[idxs])))
        cc.append(float(c[last]))
    return (
        np.array(ots, dtype=np.int64),
        np.array(oo, dtype=np.float64),
        np.array(hh, dtype=np.float64),
        np.array(ll, dtype=np.float64),
        np.array(cc, dtype=np.float64),
    )


def walk_m(
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


def walk_stflip(
    i: int,
    side: str,
    sl: float,
    h: np.ndarray,
    l: np.ndarray,
    o: np.ndarray,
    c: np.ndarray,
    ts: np.ndarray,
    trend: np.ndarray,
) -> tuple[int, float, str, int] | None:
    aligned = 1 if side == "long" else -1
    n = len(ts)
    for j in range(i + 1, n):
        hit_sl = float(l[j]) <= sl if side == "long" else float(h[j]) >= sl
        tr0, tr1 = int(trend[j - 1]), int(trend[j])
        against = tr0 == aligned and tr1 == -aligned
        if hit_sl and against:
            return j, sl_fill(side, sl, float(o[j])), "SL", 1
        if hit_sl:
            return j, sl_fill(side, sl, float(o[j])), "SL", 0
        if against:
            fill = float(c[j]) - SLIP if side == "long" else float(c[j]) + SLIP
            return j, fill, "STFLIP", 0
    return None


def sl_from_candle(side: str, entry: float, open_x: float) -> tuple[float, float] | None:
    sl = float(open_x)
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return sl, risk


def simulate(
    sigs: list[Sig],
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    trend: np.ndarray,
    tf: int,
    st_len: int,
    st_mult: float,
    exit_kind: str,
    fee_mode: str,
) -> list[TradeB]:
    trades: list[TradeB] = []
    busy = -1
    cell = f"TF={tf}m|ST={st_len}x{st_mult}|{exit_kind}"
    m = M_OF.get(exit_kind, 0)
    for s in sigs:
        if s.i <= busy:
            continue
        per = period_of(s.ts)
        if per == "OUT":
            continue
        entry = s.c + SLIP if s.side == "long" else s.c - SLIP
        stp = sl_from_candle(s.side, entry, s.o)
        if stp is None:
            continue
        sl, risk = stp
        if exit_kind == "STFLIP":
            tp = float("nan")
            walked = walk_stflip(s.i, s.side, sl, h, l, o, c, ts, trend)
        else:
            tp = entry + risk * float(m) if s.side == "long" else entry - risk * float(m)
            walked = walk_m(s.i, s.side, sl, tp, h, l, o, ts)
        if walked is None:
            continue
        j, fill, reason, amb = walked
        gross = (fill - entry) if s.side == "long" else (entry - fill)
        hold = (int(ts[j]) - int(s.ts)) / 60.0
        fee = apply_fee(fee_mode, s.side, entry, fill, reason, hold)
        net = gross - fee
        trades.append(
            TradeB(
                cell=cell, tf=tf, st_len=st_len, st_mult=st_mult, exit_kind=exit_kind,
                side=s.side, entry_ts=int(s.ts), exit_ts=int(ts[j]),
                entry=entry, sl=sl, tp=float(tp) if tp == tp else 0.0,
                exit=fill, reason=reason, risk=risk, hold_min=hold,
                gross=gross, fee=fee, net=net, amb=amb, period=per,
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
    trend: np.ndarray,
    tf: int,
    st_len: int,
    st_mult: float,
    exit_kind: str,
    seed: int,
) -> list[TradeB]:
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
    return simulate(
        fake, ts, o, h, l, c, trend, tf, st_len, st_mult, exit_kind, "F2"
    )[:n_need]


def max_dd(nets: list[float]) -> float:
    eq = peak = 0.0
    dd = 0.0
    for n in nets:
        eq += n
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def _mean(xs: list[float]) -> float:
    return float(np.mean(xs)) if xs else float("nan")


def _med(xs: list[float]) -> float:
    return float(np.median(xs)) if xs else float("nan")


def cal_days_map(
    ts: np.ndarray, start_ts: int, cutoff: int | None
) -> dict[str, int]:
    out: dict[str, int] = {}
    for per in PERIODS:
        ds: set[date] = set()
        for t in ts:
            if int(t) < start_ts:
                continue
            if cutoff is not None and int(t) >= cutoff:
                break
            if period_of(int(t)) == per:
                ds.add(datetime.fromtimestamp(int(t), tz=IST_UTC).date())
        out[per] = max(len(ds), 1)
    return out


def neighbors(
    tf: int, atr: int, mu: float, ex: str
) -> list[tuple[int, int, float, str]]:
    out: list[tuple[int, int, float, str]] = []
    ai = ATRS.index(atr)
    if ai > 0:
        out.append((tf, ATRS[ai - 1], mu, ex))
    if ai + 1 < len(ATRS):
        out.append((tf, ATRS[ai + 1], mu, ex))
    mi = MULTS.index(mu)
    if mi > 0:
        out.append((tf, atr, MULTS[mi - 1], ex))
    if mi + 1 < len(MULTS):
        out.append((tf, atr, MULTS[mi + 1], ex))
    ei = EXITS.index(ex)
    if ei > 0:
        out.append((tf, atr, mu, EXITS[ei - 1]))
    if ei + 1 < len(EXITS):
        out.append((tf, atr, mu, EXITS[ei + 1]))
    return out


def option_ok(stats: dict[str, Any]) -> bool:
    for per in PERIODS:
        s = stats[per]
        n = int(s["n"])
        g = float(s["f0_gross"])
        if not (n >= 100 and np.isfinite(g) and g >= 60.0):
            return False
    return True


def fut_pos(stats: dict[str, Any]) -> bool:
    for per in PERIODS:
        s = stats[per]
        n = float(s["f2_net"])
        if not (np.isfinite(n) and n > 0):
            return False
    return True


def fut_pass(stats: dict[str, Any]) -> bool:
    for per in PERIODS:
        s = stats[per]
        n = float(s["f2_net"])
        r = float(s["rnd"])
        if not (np.isfinite(n) and n > 0 and np.isfinite(r) and n > r):
            return False
    return True


def fmt_row(per: str, s: dict[str, float]) -> str:
    return (
        f"{per} n={int(s['n'])} /day={s['per_day']:.3f} win%={s['win']:.1f} "
        f"medRisk={s['med_risk']:.2f} holdMin={s['hold']:.1f} "
        f"F0gross/t={s['f0_gross']:.4f} F2net/t={s['f2_net']:.4f} "
        f"maxDD={s['maxdd']:.1f} rndF2={s['rnd']:.4f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=SPOT_CSV)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--max-days", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(IST_UTC).strftime("%Y%m%dT%H%M%SZ")

    logger.info("loading %s", args.csv)
    spot = load_spot_1m(args.csv)
    ts1, o1, h1, l1, c1 = bars_arr(spot)
    start_ts = int(datetime(2024, 7, 1, tzinfo=IST_UTC).timestamp())
    cutoff: int | None = None
    if args.max_days:
        cutoff = start_ts + int(args.max_days) * 86400
        lo = start_ts - 5 * 86400
        hi = cutoff + 3 * 86400
        sel = (ts1 >= lo) & (ts1 < hi)
        ts1, o1, h1, l1, c1 = ts1[sel], o1[sel], h1[sel], l1[sel], c1[sel]
        print(f"SMOKE max-days={args.max_days} start={DATA_FROM} cutoff_ts={cutoff}", flush=True)

    tf_bars: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for tf in TFS:
        tf_bars[tf] = resample_complete(ts1, o1, h1, l1, c1, tf)
        print(f"resample {tf}m complete_bars={len(tf_bars[tf][0])}", flush=True)

    st_cache: dict[tuple[int, int, float], tuple[np.ndarray, list[Sig]]] = {}
    for tf in TFS:
        ts, o, h, l, c = tf_bars[tf]
        for atr in ATRS:
            for mu in MULTS:
                trend, _st = supertrend(h, l, c, atr, mu)
                sigs = collect_signals(ts, o, h, l, c, trend, start_ts, cutoff)
                st_cache[(tf, atr, mu)] = (trend, sigs)
                print(f"signals TF={tf}m ST={atr}x{mu} n={len(sigs)}", flush=True)

    print("=== HAND-CHECK 3 signals (3m ST 10x1) ===")
    _tr, s0 = st_cache[(3, 10, 1.0)]
    for s in s0[:3]:
        print(
            f"  {s.side} X={ist_str(s.ts)} ST {s.st_prev}->{s.st_now} "
            f"X-1 OHLC=({s.po:.1f},{s.ph:.1f},{s.pl:.1f},{s.pc:.1f}) "
            f"X OHLC=({s.o:.1f},{s.h:.1f},{s.l:.1f},{s.c:.1f})"
        )
    if not s0:
        print("  (none)")

    lines = [
        "S017B 3m/5m ST-flip engulfing gate",
        f"stamp={stamp} grid TF={TFS} ATR={ATRS} mult={MULTS} exits={EXITS} SL=candle",
        "F0 none; F2 taker 0.05% entry, maker 0.02% TP, taker 0.05% SL/STFLIP, GST 1.18",
    ]

    cell_stats: dict[tuple[int, int, float, str], dict[str, Any]] = {}
    all_f2: list[TradeB] = []

    for tf in TFS:
        ts, o, h, l, c = tf_bars[tf]
        days = cal_days_map(ts, start_ts, cutoff)
        for atr in ATRS:
            for mu in MULTS:
                trend, sigs = st_cache[(tf, atr, mu)]
                for ex in EXITS:
                    f0 = simulate(sigs, ts, o, h, l, c, trend, tf, atr, mu, ex, "F0")
                    f2 = simulate(sigs, ts, o, h, l, c, trend, tf, atr, mu, ex, "F2")
                    all_f2.extend(f2)
                    rec: dict[str, Any] = {}
                    lines.append(f"CELL TF={tf}m ST={atr}x{mu} {ex}")
                    for per in PERIODS:
                        r0 = [t for t in f0 if t.period == per]
                        r2 = [t for t in f2 if t.period == per]
                        n = len(r2)
                        rnds: list[float] = []
                        for seed in RANDOM_SEEDS:
                            rt = random_trades(
                                n, per, ts, o, h, l, c, trend, tf, atr, mu, ex, seed
                            )
                            rnds.append(_mean([t.net for t in rt]))
                        finite = [x for x in rnds if np.isfinite(x)]
                        rnd = _mean(finite) if finite else float("nan")
                        srow = {
                            "n": float(n),
                            "per_day": n / days[per] if days[per] else float("nan"),
                            "win": 100.0 * sum(1 for t in r2 if t.net > 0) / n if n else float("nan"),
                            "med_risk": _med([t.risk for t in r2]),
                            "hold": _mean([t.hold_min for t in r2]),
                            "f0_gross": _mean([t.gross for t in r0]),
                            "f2_net": _mean([t.net for t in r2]),
                            "maxdd": max_dd([t.net for t in r2]),
                            "rnd": rnd,
                        }
                        rec[per] = srow
                        lines.append("  " + fmt_row(per, srow))
                    cell_stats[(tf, atr, mu, ex)] = rec

    lines.append("")
    lines.append("--- OPTION_GATE (F0 gross>=60/t AND n>=100 all periods; >=2 neighbors same) ---")
    opt: list[str] = []
    for tf in TFS:
        for atr in ATRS:
            for mu in MULTS:
                for ex in EXITS:
                    st = cell_stats[(tf, atr, mu, ex)]
                    if not option_ok(st):
                        continue
                    adj = 0
                    for nb in neighbors(tf, atr, mu, ex):
                        if option_ok(cell_stats[nb]):
                            adj += 1
                    tag = f"TF={tf}m ST={atr}x{mu} {ex} adj={adj}"
                    if adj >= 2:
                        opt.append(tag)
    if opt:
        for x in opt:
            lines.append("  " + x)
    else:
        lines.append("NONE")

    lines.append("")
    lines.append("--- FUTURES_PASS (F2 net>0 all 3 AND > random; >=2 neighbors F2 net>0 all 3) ---")
    fut: list[str] = []
    for tf in TFS:
        for atr in ATRS:
            for mu in MULTS:
                for ex in EXITS:
                    st = cell_stats[(tf, atr, mu, ex)]
                    if not fut_pass(st):
                        continue
                    adj = 0
                    for nb in neighbors(tf, atr, mu, ex):
                        if fut_pos(cell_stats[nb]):
                            adj += 1
                    tag = f"TF={tf}m ST={atr}x{mu} {ex} adj={adj}"
                    if adj >= 2:
                        fut.append(tag)
    if fut:
        for x in fut:
            lines.append("  " + x)
    else:
        lines.append("NONE")

    text = "\n".join(lines) + "\n"
    txt_path = out_dir / f"s017b_{stamp}.txt"
    txt_path.write_text(text, encoding="utf-8")
    csv_path = out_dir / f"s017b_{stamp}_trades.csv"
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
    print(f"wrote {csv_path}")
    if args.max_days:
        print(f"SMOKE done days={args.max_days}")


if __name__ == "__main__":
    main()
