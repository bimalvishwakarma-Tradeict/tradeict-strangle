#!/usr/bin/env python3
"""S015 Regime Map — measurement only.

python backtest\\strategies\\s015_regime_map\\regime_map.py --max-days 1
python backtest\\strategies\\s015_regime_map\\regime_map.py --fresh
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.fees_sim import estimate_option_fee  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection  # noqa: E402
from backtest.slippage_model import load_slip_table, slip_pct  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    Bar1m,
    Candle,
    build_tf,
    intrinsic,
    ist_date,
    load_spot_1m,
    option_symbol,
    t_years,
)
from backtest.strategies.s012_trend_follow.run_s012 import (  # noqa: E402
    day_cluster_bootstrap,
)
from backtest.strategies.s012g_whipsaw.engine import adx_wilder, atr_wilder  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s015")

SPOT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
OUT_DIR = Path("backtest/strategies/s015_regime_map/runs")
DATA_START = date(2025, 1, 1)
QTY = 1000
STALE_SEC = 5 * 60
SAMPLE_START = (0, 0)
SAMPLE_END = (15, 30)
STEP_MIN = 15
EXP_H, EXP_M = 17, 30
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261003
MARK_TOL = 60
CHOP_N = 14
BB_LEN, BB_K = 20, 2.0

# PRE-REGISTERED thresholds — do not change
COMP_ADX = 20.0
COMP_CHOP = 61.8
AWAKE_ADX = 20.0
AWAKE_CHOP = 61.8
AWAKE_VOL = 1.5
SHORT_MIN_SCORE = 6
LONG_MIN_SCORE = 4
PASS_MIN_DAYS = 30


@dataclass
class Sample:
    day: date
    ts: int
    year: int
    spot: float
    k: float
    c_mark: float
    p_mark: float
    y_exp: float
    y_1h: float
    y_2h: float
    y_4h: float
    cost_exp: float
    cost_1h: float
    cost_2h: float
    cost_4h: float
    net_short_exp: float
    net_short_1h: float
    net_short_2h: float
    net_short_4h: float
    net_long_exp: float
    net_long_1h: float
    net_long_2h: float
    net_long_4h: float
    compression: int
    awakening: int
    skip: str = ""
    feats: dict[str, float] = field(default_factory=dict)


def last_closed_1m(spot: dict[int, Bar1m], t: int) -> Bar1m | None:
    ts = (int(t) // 60) * 60 - 60
    return spot.get(ts) or spot.get(int(t) - 60)


def last_tf_idx(candles: list[Candle], t: int, tf_sec: int) -> int:
    ans = -1
    for i, c in enumerate(candles):
        if int(c.ts_open) + int(tf_sec) <= int(t):
            ans = i
        else:
            break
    return ans


def _mean(xs: list[float]) -> float:
    v = [float(x) for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(np.mean(v)) if v else float("nan")


def spearman(xs: list[float], ys: list[float]) -> float:
    a = np.array(xs, dtype=np.float64)
    b = np.array(ys, dtype=np.float64)
    m = np.isfinite(a) & np.isfinite(b)
    a, b = a[m], b[m]
    if len(a) < 3:
        return float("nan")
    ra = np.argsort(np.argsort(a)).astype(np.float64)
    rb = np.argsort(np.argsort(b)).astype(np.float64)
    if np.std(ra) == 0 or np.std(rb) == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def chop_index(h: np.ndarray, l: np.ndarray, atr: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(h), np.nan)
    logn = math.log10(n) if n > 1 else 1.0
    for i in range(n - 1, len(h)):
        sl_atr = atr[i - n + 1 : i + 1]
        if np.any(np.isnan(sl_atr)):
            continue
        hh = float(np.max(h[i - n + 1 : i + 1]))
        ll = float(np.min(l[i - n + 1 : i + 1]))
        den = hh - ll
        s = float(np.sum(sl_atr))
        if den <= 1e-12 or s <= 0:
            continue
        out[i] = 100.0 * math.log10(s / den) / logn
    return out


def bbw_series(c: np.ndarray, length: int, k: float) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan)
    for i in range(length - 1, n):
        w = c[i - length + 1 : i + 1]
        m = float(np.mean(w))
        s = float(np.std(w, ddof=1))
        if m <= 0:
            continue
        out[i] = (2.0 * k * s) / m
    return out


def session_vwap(candles: list[Candle], vols: np.ndarray) -> np.ndarray:
    n = len(candles)
    out = np.full(n, np.nan)
    num = den = 0.0
    last_day: date | None = None
    for i, c in enumerate(candles):
        d = datetime.fromtimestamp(int(c.ts_open), tz=UTC).date()
        if last_day is None or d != last_day:
            num = den = 0.0
            last_day = d
        tp = (float(c.high) + float(c.low) + float(c.close)) / 3.0
        v = float(vols[i]) if i < len(vols) else 0.0
        num += tp * v
        den += v
        if den > 0:
            out[i] = num / den
    return out


def vwap_crosses(close: np.ndarray, vwap: np.ndarray, i: int, look: int) -> float:
    i0 = max(1, i - look + 1)
    n = 0
    for j in range(i0, i + 1):
        if math.isnan(vwap[j]) or math.isnan(vwap[j - 1]):
            continue
        a = close[j - 1] - vwap[j - 1]
        b = close[j] - vwap[j]
        if a == 0 or b == 0:
            continue
        if (a > 0 and b < 0) or (a < 0 and b > 0):
            n += 1
    return float(n)


def rv_ann(close: np.ndarray, i: int, n_bars: int, bar_sec: int) -> float:
    if i < n_bars:
        return float("nan")
    rets = []
    for j in range(i - n_bars + 1, i + 1):
        p0 = float(close[j - 1])
        p1 = float(close[j])
        if p0 > 0 and p1 > 0:
            rets.append(math.log(p1 / p0))
    if len(rets) < 3:
        return float("nan")
    per_year = (365.25 * 24.0 * 3600.0) / float(bar_sec)
    return float(np.std(rets, ddof=1) * math.sqrt(per_year))


def mark_le(
    store: MarksStore, expiry: date, symbol: str, t: int
) -> tuple[int, float] | None:
    conn = store.conn(expiry)
    if conn is None:
        return None
    minute = (int(t) // 60) * 60
    row = conn.execute(
        """
        SELECT ts, close FROM marks
        WHERE symbol=? AND ts<=? AND ts>=? AND close IS NOT NULL AND close>0
        ORDER BY ts DESC LIMIT 1
        """,
        (symbol, minute, minute - STALE_SEC - MARK_TOL),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), float(row[1])


def atm_straddle(
    store: MarksStore, expiry: date, t: int, spot_px: float
) -> tuple[float, float, float, int] | None:
    """Return (K, call, put, worst_age_sec) or None."""
    k0 = int(round(float(spot_px) / 50.0) * 50)
    best: tuple[float, float, float, int] | None = None
    best_ad = 1e18
    for k in range(k0 - 2000, k0 + 2000 + 1, 50):
        cs = option_symbol(True, float(k), expiry)
        ps = option_symbol(False, float(k), expiry)
        cm = mark_le(store, expiry, cs, t)
        pm = mark_le(store, expiry, ps, t)
        if cm is None or pm is None:
            continue
        ad = abs(float(k) - float(spot_px))
        age = max(int(t) - cm[0], int(t) - pm[0])
        if ad < best_ad:
            best_ad = ad
            best = (float(k), cm[1], pm[1], age)
    return best


def fees_slip(prem: float, index: float, dte: int) -> tuple[float, float]:
    fee = estimate_option_fee(premium=prem, qty_lots=QTY, btc_index=index)
    sf = float(slip_pct(prem, dte)) / 100.0
    slip = abs(prem) * sf * (QTY * 0.001)
    return fee, slip


def round_cost(
    c0: float, p0: float, c1: float, p1: float, idx0: float, idx1: float, dte: int,
    settle: bool,
) -> float:
    f1, s1 = fees_slip(c0, idx0, dte)
    f2, s2 = fees_slip(p0, idx0, dte)
    if settle:
        f3, _ = fees_slip(max(c1, 1e-9), idx1, dte)
        f4, _ = fees_slip(max(p1, 1e-9), idx1, dte)
        s3 = s4 = 0.0
    else:
        f3, s3 = fees_slip(c1, idx1, dte)
        f4, s4 = fees_slip(p1, idx1, dte)
    return f1 + f2 + f3 + f4 + s1 + s2 + s3 + s4


def compression_score(f: dict[str, float]) -> int:
    s = 0
    if f.get("range_ratio", 99) < 1:
        s += 1
    if f.get("atr_chg", 0) < 0:
        s += 1
    if f.get("bbw", 99) < f.get("bbw_mean50", -99) and f.get("bbw_slope", 1) < 0:
        s += 1
    if f.get("adx_5m", 99) < COMP_ADX:
        s += 1
    if f.get("chop14", -1) > COMP_CHOP:
        s += 1
    if f.get("vol_ratio", 99) < 1:
        s += 1
    if f.get("iv_chg_15m", 1) <= 0:
        s += 1
    if f.get("atm_iv", -1) > f.get("rv_1h", 99):
        s += 1
    return s


def awakening_score(f: dict[str, float]) -> int:
    s = 0
    if f.get("awake_bbw", 0) == 1:
        s += 1
    if f.get("awake_atr", 0) == 1:
        s += 1
    if f.get("awake_adx", 0) == 1:
        s += 1
    if f.get("awake_chop", 0) == 1:
        s += 1
    if f.get("vol_ratio", 0) > AWAKE_VOL:
        s += 1
    if f.get("iv_chg_15m", 0) > 0 and f.get("iv_chg_60m", 0) > 0:
        s += 1
    return s


class TfPack:
    def __init__(self, candles: list[Candle], vols: np.ndarray, tf_sec: int) -> None:
        self.c = candles
        self.tf = tf_sec
        self.h = np.array([x.high for x in candles], dtype=np.float64)
        self.l = np.array([x.low for x in candles], dtype=np.float64)
        self.cl = np.array([x.close for x in candles], dtype=np.float64)
        self.rng = self.h - self.l
        self.vol = vols
        self.atr = atr_wilder(self.h, self.l, self.cl, 14)
        self.adx = adx_wilder(self.h, self.l, self.cl, 14)
        self.bbw = bbw_series(self.cl, BB_LEN, BB_K)
        self.chop = chop_index(self.h, self.l, self.atr, CHOP_N)
        self.vwap = session_vwap(candles, vols) if tf_sec == 300 else None


def _tf_vols(spot: dict[int, Bar1m], candles: list[Candle], tf_min: int) -> np.ndarray:
    step = tf_min * 60
    out = np.zeros(len(candles))
    for i, c in enumerate(candles):
        s = 0.0
        ts = int(c.ts_open)
        end = ts + step
        while ts < end:
            b = spot.get(ts)
            if b is not None:
                s += float(b.volume)
            ts += 60
        out[i] = s
    return out


def features_at(t: int, p5: TfPack, p15: TfPack, p1h: TfPack) -> dict[str, float]:
    f: dict[str, float] = {}
    i = last_tf_idx(p5.c, t, 300)
    i15 = last_tf_idx(p15.c, t, 900)
    i1h = last_tf_idx(p1h.c, t, 3600)
    nan = float("nan")
    for k in (
        "range_ratio", "atr14", "atr_chg", "bbw", "bbw_mean50", "bbw_slope",
        "adx_5m", "adx_15m", "adx_1h", "chop14", "vwap_crosses", "vol_ratio",
        "breakout", "dist_1h_atr", "awake_bbw", "awake_atr", "awake_adx", "awake_chop",
    ):
        f[k] = nan
    if i < 0:
        return f
    if i >= 23:
        a4 = float(np.mean(p5.rng[i - 3 : i + 1]))
        a20 = float(np.mean(p5.rng[i - 23 : i - 3]))
        f["range_ratio"] = a4 / a20 if a20 > 0 else nan
    f["atr14"] = float(p5.atr[i])
    if i >= 4:
        f["atr_chg"] = float(p5.atr[i] - p5.atr[i - 4])
    f["bbw"] = float(p5.bbw[i])
    if i >= 49:
        f["bbw_mean50"] = float(np.nanmean(p5.bbw[i - 49 : i + 1]))
    if i >= 1:
        f["bbw_slope"] = float(p5.bbw[i] - p5.bbw[i - 1])
    f["adx_5m"] = float(p5.adx[i])
    if i15 >= 0:
        f["adx_15m"] = float(p15.adx[i15])
    if i1h >= 0:
        f["adx_1h"] = float(p1h.adx[i1h])
    f["chop14"] = float(p5.chop[i])
    if p5.vwap is not None:
        f["vwap_crosses"] = vwap_crosses(p5.cl, p5.vwap, i, 24)
    if i >= 19:
        mv = float(np.mean(p5.vol[i - 19 : i + 1]))
        f["vol_ratio"] = float(p5.vol[i]) / mv if mv > 0 else nan
    if i1h >= 1:
        prev = p1h.c[i1h]
        cl = float(p5.cl[i])
        f["breakout"] = 1.0 if (cl > float(prev.high) or cl < float(prev.low)) else 0.0
        atr = float(p5.atr[i])
        if atr > 0:
            if cl >= (float(prev.high) + float(prev.low)) / 2.0:
                f["dist_1h_atr"] = (float(prev.high) - cl) / atr
            else:
                f["dist_1h_atr"] = (cl - float(prev.low)) / atr
    # awakening extras
    if i >= 99:
        win100 = p5.bbw[i - 99 : i + 1]
        q20 = float(np.nanpercentile(win100, 20))
        last12 = p5.bbw[i - 11 : i + 1]
        hit = bool(np.nanmin(last12) <= q20) if np.isfinite(q20) else False
        rising = i >= 1 and p5.bbw[i] > p5.bbw[i - 1]
        f["awake_bbw"] = 1.0 if hit and rising else 0.0
    if i >= 12:
        lo = float(np.nanmin(p5.atr[i - 11 : i + 1]))
        rising = i >= 1 and float(p5.atr[i]) > float(p5.atr[i - 1])
        f["awake_atr"] = 1.0 if rising and float(p5.atr[i]) > lo else 0.0
    if i >= 6:
        was = any(float(p5.adx[j]) < AWAKE_ADX for j in range(i - 5, i + 1) if np.isfinite(p5.adx[j]))
        rising = i >= 1 and p5.adx[i] > p5.adx[i - 1]
        f["awake_adx"] = 1.0 if rising and was else 0.0
        crossed = False
        for j in range(i - 5, i + 1):
            if j < 1:
                continue
            if p5.chop[j - 1] >= AWAKE_CHOP and p5.chop[j] < AWAKE_CHOP:
                crossed = True
        f["awake_chop"] = 1.0 if crossed else 0.0
    f["rv_1h"] = rv_ann(p5.cl, i, 12, 300)
    f["rv_4h"] = rv_ann(p5.cl, i, 48, 300)
    return f


def option_vol(
    store: Any, symbols: list[str], t: int
) -> float:
    if store is None:
        return float("nan")
    start = datetime.fromtimestamp(int(t) - 15 * 60, tz=UTC)
    end = datetime.fromtimestamp(int(t), tz=UTC)
    tot = 0.0
    any_ = False
    for sym in symbols:
        rows = store.trades_for_symbol(sym, start, end)
        if rows:
            any_ = True
            tot += sum(float(r.size) for r in rows)
    return tot if any_ else float("nan")


def sample_grid(days: list[date]) -> list[tuple[date, int]]:
    out: list[tuple[date, int]] = []
    for d in days:
        t0 = to_unix(ist_dt(d, SAMPLE_START[0], SAMPLE_START[1]))
        t1 = to_unix(ist_dt(d, SAMPLE_END[0], SAMPLE_END[1]))
        t = t0
        while t <= t1:
            out.append((d, t))
            t += STEP_MIN * 60
    return out


def fmt_bucket(title: str, rows: list[str]) -> str:
    return "\n".join([title] + rows)


def bootstrap_nets(pairs: list[tuple[date, float]]) -> tuple[float, float, float]:
    objs = [SimpleNamespace(day=d, skip="", net=v) for d, v in pairs]
    return day_cluster_bootstrap(objs, BOOTSTRAP_N, BOOTSTRAP_SEED)  # type: ignore[arg-type]


def main() -> None:
    ap = argparse.ArgumentParser(description="S015 regime map")
    ap.add_argument("--csv", default=SPOT_CSV)
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_slip_table()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg_tag = f"max{args.max_days}" if args.max_days else "full"
    ckpt_path = out_dir / f"s015_cache_{cfg_tag}.jsonl"
    if args.fresh and ckpt_path.exists():
        ckpt_path.unlink()

    t0 = time.perf_counter()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    logger.info("loading spot")
    spot = load_spot_1m(args.csv)
    start_ts = to_unix(ist_dt(DATA_START, 0, 0))
    days: list[date] = []
    seen: set[date] = set()
    for ts in sorted(spot):
        if ts < start_ts:
            continue
        d = ist_date(ts)
        if d < DATA_START:
            continue
        if d in seen:
            continue
        seen.add(d)
        days.append(d)
        if args.max_days and len(days) >= int(args.max_days):
            break
    grid = sample_grid(days)
    print(
        f"S015 planned_samples={len(grid)} days={len(days)} first={days[0] if days else None}",
        flush=True,
    )
    logger.info("building 5m/15m/1h")
    c5 = build_tf(spot, 5)
    c15 = build_tf(spot, 15)
    c1h = build_tf(spot, 60)
    p5 = TfPack(c5, _tf_vols(spot, c5, 5), 300)
    p15 = TfPack(c15, _tf_vols(spot, c15, 15), 900)
    p1h = TfPack(c1h, _tf_vols(spot, c1h, 60), 3600)

    store = MarksStore()
    try:
        from backtest.options_trades import OptionsTradeStore

        tstore: Any = OptionsTradeStore()
    except Exception:
        tstore = None
    try:
        from backtest.iv_surface import load_surface

        surf_pack = load_surface("full")
        surface = surf_pack[0] if surf_pack else None
    except Exception:
        surface = None

    done: set[int] = set()
    if ckpt_path.exists():
        with ckpt_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                done.add(int(obj["ts"]))
        logger.info("resume done=%s", len(done))

    samples: list[Sample] = []
    stale_n = 0
    ntot = len(grid)
    next_pct = 2
    iv_hist: dict[int, float] = {}
    opt_vol_hist: list[float] = []
    ckpt_f = ckpt_path.open("a", encoding="utf-8")
    try:
        for n, (d, t) in enumerate(grid, start=1):
            pct = 100.0 * n / max(ntot, 1)
            if pct >= next_pct:
                elapsed = time.perf_counter() - t0
                eta = elapsed / n * (ntot - n) if n else 0
                logger.info("progress %.0f%% n=%s/%s eta_sec=%.0f", pct, n, ntot, eta)
                while next_pct <= pct:
                    next_pct += 2
            if t in done:
                continue
            bar = last_closed_1m(spot, t)
            if bar is None:
                stale_n += 1
                continue
            exp_d = d
            exp_ts = to_unix(ist_dt(exp_d, EXP_H, EXP_M))
            if t >= exp_ts:
                continue
            atm = atm_straddle(store, exp_d, t, float(bar.close))
            if atm is None:
                continue
            k, cm, pm, age = atm
            if age > STALE_SEC:
                stale_n += 1
                continue
            s_exp_bar = last_closed_1m(spot, exp_ts) or spot.get(exp_ts)
            if s_exp_bar is None:
                continue
            y_exp = (cm + pm) - abs(float(s_exp_bar.close) - k)
            def y_h(hours: int) -> float:
                th = t + hours * 3600
                if th > exp_ts:
                    return float("nan")
                fut = atm_straddle(store, exp_d, th, float(last_closed_1m(spot, th).close) if last_closed_1m(spot, th) else k)
                if fut is None:
                    return float("nan")
                return (cm + pm) - (fut[1] + fut[2])

            y1, y2, y4 = y_h(1), y_h(2), y_h(4)
            ic_exp = intrinsic(True, k, float(s_exp_bar.close))
            ip_exp = intrinsic(False, k, float(s_exp_bar.close))
            cost_exp = round_cost(cm, pm, ic_exp, ip_exp, bar.close, s_exp_bar.close, 0, True)

            def cost_h(hours: int, y: float) -> float:
                if math.isnan(y):
                    return float("nan")
                th = t + hours * 3600
                b1 = last_closed_1m(spot, th)
                fut = atm_straddle(store, exp_d, th, float(b1.close) if b1 else k)
                if fut is None or b1 is None:
                    return float("nan")
                return round_cost(cm, pm, fut[1], fut[2], bar.close, b1.close, 0, False)

            c1, c2, c4 = cost_h(1, y1), cost_h(2, y2), cost_h(4, y4)
            feats = features_at(t, p5, p15, p1h)
            t_yr = t_years(t, exp_ts)
            iv_c = implied_vol_bisection(cm, bar.close, k, t_yr, True)
            iv_p = implied_vol_bisection(pm, bar.close, k, t_yr, False)
            atm_iv = float("nan")
            if iv_c is not None and iv_p is not None:
                atm_iv = 0.5 * (iv_c + iv_p)
            elif iv_c is not None:
                atm_iv = float(iv_c)
            elif iv_p is not None:
                atm_iv = float(iv_p)
            feats["atm_iv"] = atm_iv
            feats["call_iv_minus_put_iv"] = (
                float(iv_c - iv_p) if iv_c is not None and iv_p is not None else float("nan")
            )
            iv_hist[t] = atm_iv
            t15 = t - 15 * 60
            t60 = t - 60 * 60
            iv15 = iv_hist.get(t15, float("nan"))
            iv60 = iv_hist.get(t60, float("nan"))
            feats["iv_chg_15m"] = atm_iv - iv15 if np.isfinite(atm_iv) and np.isfinite(iv15) else float("nan")
            feats["iv_chg_60m"] = atm_iv - iv60 if np.isfinite(atm_iv) and np.isfinite(iv60) else float("nan")
            t30 = t - 30 * 60
            iv30 = iv_hist.get(t30, float("nan"))
            chg_prev = (iv15 - iv30) if np.isfinite(iv15) and np.isfinite(iv30) else float("nan")
            feats["iv_accel"] = (
                feats["iv_chg_15m"] - chg_prev
                if np.isfinite(feats["iv_chg_15m"]) and np.isfinite(chg_prev)
                else float("nan")
            )
            feats["iv_minus_rv1h"] = (
                atm_iv - feats["rv_1h"]
                if np.isfinite(atm_iv) and np.isfinite(feats["rv_1h"])
                else float("nan")
            )
            skew = float("nan")
            if surface is not None and iv_c is not None:
                # 25d call/put IVs from surface at t (bucket <= t via nearest)
                try:
                    k_hi = k
                    k_lo = k
                    for dk in range(50, 4000, 50):
                        if black76_abs_delta(bar.close, k + dk, t_yr, iv_c, True) <= 0.25:
                            k_hi = k + dk
                            break
                    for dk in range(50, 4000, 50):
                        if black76_abs_delta(bar.close, k - dk, t_yr, iv_c, False) <= 0.25:
                            k_lo = k - dk
                            break
                    iv25c = surface.iv(float(t), float(k_hi), exp_d)
                    iv25p = surface.iv(float(t), float(k_lo), exp_d)
                    if iv25c is not None and iv25p is not None:
                        skew = float(iv25c) - float(iv25p)
                except Exception:
                    skew = float("nan")
            feats["skew_25d"] = skew
            cs = option_symbol(True, k, exp_d)
            ps = option_symbol(False, k, exp_d)
            ov = option_vol(tstore, [cs, ps], t)
            opt_vol_hist.append(ov)
            if len(opt_vol_hist) >= 20 and np.isfinite(ov):
                avg = float(np.nanmean(opt_vol_hist[-20:]))
                feats["opt_vol_ratio"] = ov / avg if avg > 0 else float("nan")
            else:
                feats["opt_vol_ratio"] = float("nan")

            comp = compression_score(feats)
            awake = awakening_score(feats)
            ns_exp = y_exp - cost_exp
            nl_exp = -y_exp - cost_exp

            def net_pair(y: float, c: float, long: bool) -> float:
                if math.isnan(y) or math.isnan(c):
                    return float("nan")
                return (-y if long else y) - c

            smp = Sample(
                day=d, ts=t, year=d.year, spot=float(bar.close), k=k,
                c_mark=cm, p_mark=pm, y_exp=y_exp, y_1h=y1, y_2h=y2, y_4h=y4,
                cost_exp=cost_exp, cost_1h=c1, cost_2h=c2, cost_4h=c4,
                net_short_exp=ns_exp,
                net_short_1h=net_pair(y1, c1, False),
                net_short_2h=net_pair(y2, c2, False),
                net_short_4h=net_pair(y4, c4, False),
                net_long_exp=nl_exp,
                net_long_1h=net_pair(y1, c1, True),
                net_long_2h=net_pair(y2, c2, True),
                net_long_4h=net_pair(y4, c4, True),
                compression=comp, awakening=awake, feats=feats,
            )
            samples.append(smp)
            rec = asdict(smp)
            rec["day"] = d.isoformat()
            ckpt_f.write(json.dumps(rec) + "\n")
            ckpt_f.flush()
    finally:
        ckpt_f.close()
        store.close()

    # reload checkpoint samples if resumed empty this session
    if not samples and ckpt_path.exists():
        with ckpt_path.open(encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                obj["day"] = date.fromisoformat(obj["day"])
                samples.append(Sample(**{k: obj[k] for k in Sample.__dataclass_fields__}))

    elapsed = time.perf_counter() - t0
    print(f"collected_samples={len(samples)} stale_skips={stale_n} elapsed_sec={elapsed:.1f}")
    for s in samples[:3]:
        print(
            f"row day={s.day} ts={s.ts} K={s.k:.0f} C={s.c_mark:.2f} P={s.p_mark:.2f} "
            f"Yexp={s.y_exp:.2f} nS={s.net_short_exp:.2f} nL={s.net_long_exp:.2f} "
            f"C={s.compression} A={s.awakening}"
        )

    csv_path = out_dir / f"s015_regime_{stamp}_samples.csv"
    if samples:
        fields = [k for k in asdict(samples[0]) if k != "feats"] + sorted(samples[0].feats.keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for s in samples:
                row = asdict(s)
                feats = row.pop("feats")
                row["day"] = s.day.isoformat()
                row.update(feats)
                w.writerow(row)

    def by_year(year: int) -> list[Sample]:
        return [s for s in samples if s.year == year]

    lines = [
        "S015 Regime Map",
        f"stamp={stamp} samples={len(samples)} days={len(days)} stale={stale_n} elapsed={elapsed:.1f}",
        "",
        "=== BASELINE all samples ===",
    ]
    for year in (2025, 2026):
        ys = by_year(year)
        lines.append(
            f"year={year} n={len(ys)} mean_net_short_exp={_mean([s.net_short_exp for s in ys])} "
            f"mean_net_long_exp={_mean([s.net_long_exp for s in ys])}"
        )
    lines.append("")
    lines.append("=== COMPRESSION score tables (net_short) ===")
    for year in (2025, 2026):
        ys = by_year(year)
        base = _mean([s.net_short_exp for s in ys])
        means: list[float] = []
        scores: list[float] = []
        for sc in range(0, 9):
            bucket = [s for s in ys if s.compression == sc]
            days_n = len({s.day for s in bucket})
            pairs = [(s.day, s.net_short_exp) for s in bucket]
            _, lo, hi = bootstrap_nets(pairs) if pairs else (float("nan"),) * 3
            lines.append(
                f"year={year} C={sc} n={len(bucket)} days={days_n} "
                f"mean_exp={_mean([s.net_short_exp for s in bucket])} "
                f"mean_1h={_mean([s.net_short_1h for s in bucket])} "
                f"mean_2h={_mean([s.net_short_2h for s in bucket])} "
                f"mean_4h={_mean([s.net_short_4h for s in bucket])} "
                f"CI95=[{lo},{hi}]"
            )
            m = _mean([s.net_short_exp for s in bucket])
            if not math.isnan(m):
                scores.append(float(sc))
                means.append(m)
        sp = spearman(scores, means)
        lines.append(f"year={year} Spearman(score, bucket_mean_net_short_exp)={sp} baseline={base}")
    lines.append("")
    lines.append("=== AWAKENING score tables (net_long) ===")
    for year in (2025, 2026):
        ys = by_year(year)
        base = _mean([s.net_long_exp for s in ys])
        means = []
        scores = []
        for sc in range(0, 7):
            bucket = [s for s in ys if s.awakening == sc]
            days_n = len({s.day for s in bucket})
            pairs = [(s.day, s.net_long_exp) for s in bucket]
            _, lo, hi = bootstrap_nets(pairs) if pairs else (float("nan"),) * 3
            lines.append(
                f"year={year} A={sc} n={len(bucket)} days={days_n} "
                f"mean_exp={_mean([s.net_long_exp for s in bucket])} "
                f"mean_1h={_mean([s.net_long_1h for s in bucket])} "
                f"mean_2h={_mean([s.net_long_2h for s in bucket])} "
                f"mean_4h={_mean([s.net_long_4h for s in bucket])} "
                f"CI95=[{lo},{hi}]"
            )
            m = _mean([s.net_long_exp for s in bucket])
            if not math.isnan(m):
                scores.append(float(sc))
                means.append(m)
        sp = spearman(scores, means)
        lines.append(f"year={year} Spearman(score, bucket_mean_net_long_exp)={sp} baseline={base}")

    lines.append("")
    lines.append("EXPLORATORY, NOT PASS BASIS")
    lines.append("=== single-feature quintiles (net_short_exp) ===")
    feat_keys = [
        "range_ratio", "atr14", "bbw", "adx_5m", "chop14", "vol_ratio",
        "atm_iv", "iv_chg_15m", "rv_1h", "iv_minus_rv1h",
    ]
    for year in (2025, 2026):
        ys = by_year(year)
        for fk in feat_keys:
            vals = [(s, s.feats.get(fk, float("nan"))) for s in ys]
            vals = [v for v in vals if np.isfinite(v[1])]
            if len(vals) < 10:
                lines.append(f"year={year} feat={fk} n={len(vals)} (skip)")
                continue
            qs = np.nanpercentile([v[1] for v in vals], [20, 40, 60, 80])
            lines.append(f"year={year} feat={fk} quintile_edges={list(qs)}")
            bounds = [-1e18] + list(qs) + [1e18]
            for qi in range(5):
                chunk = [s for s, x in vals if bounds[qi] <= x <= bounds[qi + 1]]
                lines.append(
                    f"  Q{qi+1} n={len(chunk)} mean_net_short_exp={_mean([s.net_short_exp for s in chunk])}"
                )

    def pass_short() -> str:
        ok = True
        bits = ["--- PRE-REGISTERED SHORT (compression>=6) ---"]
        for year in (2025, 2026):
            ys = by_year(year)
            bucket = [s for s in ys if s.compression >= SHORT_MIN_SCORE]
            days_n = len({s.day for s in bucket})
            m = _mean([s.net_short_exp for s in bucket])
            base = _mean([s.net_short_exp for s in ys])
            means, scores = [], []
            for sc in range(0, 9):
                b = [s for s in ys if s.compression == sc]
                mm = _mean([s.net_short_exp for s in b])
                if not math.isnan(mm):
                    scores.append(float(sc))
                    means.append(mm)
            sp = spearman(scores, means)
            bits.append(
                f"year={year} days={days_n} mean={m} baseline={base} spearman={sp}"
            )
            if not (
                days_n >= PASS_MIN_DAYS
                and isinstance(m, float) and m > 0
                and isinstance(base, float) and not math.isnan(base) and m > base
                and isinstance(sp, float) and not math.isnan(sp) and sp > 0
            ):
                ok = False
        bits.append("PASS" if ok else "FAIL")
        return "\n".join(bits)

    def pass_long() -> str:
        ok = True
        bits = ["--- PRE-REGISTERED LONG (awakening>=4) ---"]
        for year in (2025, 2026):
            ys = by_year(year)
            bucket = [s for s in ys if s.awakening >= LONG_MIN_SCORE]
            days_n = len({s.day for s in bucket})
            m = _mean([s.net_long_exp for s in bucket])
            base = _mean([s.net_long_exp for s in ys])
            means, scores = [], []
            for sc in range(0, 7):
                b = [s for s in ys if s.awakening == sc]
                mm = _mean([s.net_long_exp for s in b])
                if not math.isnan(mm):
                    scores.append(float(sc))
                    means.append(mm)
            sp = spearman(scores, means)
            bits.append(
                f"year={year} days={days_n} mean={m} baseline={base} spearman={sp}"
            )
            if not (
                days_n >= PASS_MIN_DAYS
                and isinstance(m, float) and m > 0
                and isinstance(base, float) and not math.isnan(base) and m > base
                and isinstance(sp, float) and not math.isnan(sp) and sp > 0
            ):
                ok = False
        bits.append("PASS" if ok else "FAIL")
        return "\n".join(bits)

    lines.append("")
    lines.append(pass_short())
    lines.append("")
    lines.append(pass_long())
    txt = "\n".join(lines) + "\n"
    txt_path = out_dir / f"s015_regime_{stamp}.txt"
    txt_path.write_text(txt, encoding="utf-8")
    print(txt)
    print(f"wrote {txt_path}")
    if args.max_days:
        print(f"SMOKE max-days={args.max_days} samples={len(samples)}")


if __name__ == "__main__":
    main()
