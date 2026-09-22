"""S007-B engine: signal → 1DTE directional option + futures hedge → exit.

Look-ahead safety: a signal is confirmed on a bar CLOSE; entry is the NEXT bar's
OPEN. No signal-bar data is used to price the entry.

Real option premiums come from the marks sqlite (never a delta approximation).
Per-signal DB work (strike pick + mark series) happens ONCE; every target/stop/
futures-mode combo is then simulated in-memory over precomputed numpy arrays.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from backtest.harness.data import MarksStore, ist_dt, load_symbol_series, to_unix
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection
from backtest.strategies.s007b_vwap_directional import config as cfg
from backtest.strategies.s007b_vwap_directional.indicators import (
    rsi_wilder,
    smith_vwap,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
logger = logging.getLogger("s007b.engine")

_BIG = 1 << 60


# ---------------------------------------------------------------------------
# Market data container
# ---------------------------------------------------------------------------
@dataclass
class MarketData:
    ts: np.ndarray        # int64 unix seconds, ascending
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __post_init__(self) -> None:
        self._ts_index = {int(t): i for i, t in enumerate(self.ts)}

    @property
    def n(self) -> int:
        return int(self.ts.size)


def load_market_data(csv_path: str) -> MarketData:
    """Load 1m CSV. The IST timestamp column carries a ' IST' suffix."""
    import csv as _csv

    ts: list[int] = []
    o: list[float] = []
    h: list[float] = []
    lo: list[float] = []
    c: list[float] = []
    v: list[float] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            # open_time_ist has a trailing ' IST' — strip before any parse use.
            _ = row["open_time_ist"].replace(" IST", "")
            ts.append(int(row["open_time_unix"]))
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            lo.append(float(row["low"]))
            c.append(float(row["close"]))
            v.append(float(row["volume"]))
    md = MarketData(
        ts=np.asarray(ts, dtype=np.int64),
        open=np.asarray(o, dtype=np.float64),
        high=np.asarray(h, dtype=np.float64),
        low=np.asarray(lo, dtype=np.float64),
        close=np.asarray(c, dtype=np.float64),
        volume=np.asarray(v, dtype=np.float64),
    )
    return md


def ist_of(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(IST)


def ist_str(ts: int) -> str:
    return ist_of(ts).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Signal:
    index: int            # bar index where signal confirmed (on close)
    ts: int               # signal bar ts (unix)
    side: str             # "BULLISH" | "BEARISH"
    rsi_period: int
    rsi_at_signal: float
    spot_at_signal: float
    lower_band: float
    upper_band: float


def build_signals(md: MarketData, rsi_period: int) -> list[Signal]:
    """Band cross + RSI extreme, with a global 60-bar cooldown.

    BULLISH: close crosses BELOW lower band (prev close above, this close below)
             AND RSI < 30.
    BEARISH: close crosses ABOVE upper band AND RSI > 70.
    """
    lower, upper = smith_vwap(
        md.close, md.volume, length=cfg.BAND_LENGTH, k=cfg.BAND_K
    )
    rsi = rsi_wilder(md.close, rsi_period)
    close = md.close
    n = md.n

    out: list[Signal] = []
    last_accept = -(cfg.COOLDOWN_BARS + 1)
    for i in range(1, n):
        lb = lower[i]
        ub = upper[i]
        plb = lower[i - 1]
        pub = upper[i - 1]
        r = rsi[i]
        if np.isnan(lb) or np.isnan(plb) or np.isnan(r):
            continue

        cross_down = close[i - 1] >= plb and close[i] < lb
        cross_up = close[i - 1] <= pub and close[i] > ub

        side: str | None = None
        if cross_down and r < cfg.RSI_OVERSOLD:
            side = "BULLISH"
        elif cross_up and r > cfg.RSI_OVERBOUGHT:
            side = "BEARISH"
        if side is None:
            continue
        if i - last_accept <= cfg.COOLDOWN_BARS:
            continue  # global cooldown (side-agnostic)
        last_accept = i
        out.append(
            Signal(
                index=i,
                ts=int(md.ts[i]),
                side=side,
                rsi_period=rsi_period,
                rsi_at_signal=float(r),
                spot_at_signal=float(close[i]),
                lower_band=float(lb),
                upper_band=float(ub),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Option chain access (per-(expiry, minute, opt_type) cache across all signals)
# ---------------------------------------------------------------------------
class ChainCache:
    def __init__(self) -> None:
        self._c: dict[tuple[str, int, str], list[tuple[float, float]]] = {}
        self.loads = 0
        self.hits = 0

    def get(
        self,
        store: MarksStore,
        entry_date: date,
        expiry: date,
        ts_minute: int,
        opt_type: str,
    ) -> list[tuple[float, float]]:
        key = (expiry.isoformat(), int(ts_minute), opt_type)
        hit = self._c.get(key)
        if hit is not None:
            self.hits += 1
            return hit
        conn = store.conn(entry_date)
        rows: list[tuple[float, float]] = []
        if conn is not None:
            rows = _query_chain(conn, expiry, ts_minute, opt_type)
        self._c[key] = rows
        self.loads += 1
        return rows


def _query_chain(
    conn: sqlite3.Connection, expiry: date, ts_minute: int, opt_type: str
) -> list[tuple[float, float]]:
    cur = conn.execute(
        """
        SELECT strike, close FROM marks
        WHERE expiry=? AND ts=? AND opt_type=?
          AND close IS NOT NULL AND close > 0
        ORDER BY strike
        """,
        (expiry.isoformat(), int(ts_minute), opt_type),
    )
    return [(float(k), float(c)) for k, c in cur.fetchall()]


def option_symbol(opt_type: str, strike: float, expiry: date) -> str:
    prefix = "C" if opt_type == "call" else "P"
    return f"{prefix}-BTC-{int(strike)}-{expiry.strftime('%d%m%y')}"


# ---------------------------------------------------------------------------
# Trade context (all per-signal DB work done here, once)
# ---------------------------------------------------------------------------
@dataclass
class TradeContext:
    signal: Signal
    entry_ts: int
    entry_spot: float
    expiry: date
    opt_type: str
    is_bull: bool
    fut_sign: int          # futures position sign: -1 short (bull), +1 long (bear)
    symbol: str
    strike: float
    entry_delta: float     # signed (call +, put -)
    entry_mark: float
    # window arrays (entry bar .. 17:15 deadline bar)
    w_ts: np.ndarray
    w_close: np.ndarray
    w_optmark: np.ndarray
    opt_open: np.ndarray   # (optmark-entry_mark)*OPTION_QTY*LOT
    fut_open: np.ndarray   # fut_sign*(close-entry_spot)*FUT_QTY*LOT
    tgt_series: np.ndarray  # high (bull) or low (bear) for target touch
    fav_cum: np.ndarray
    adv_cum: np.ndarray


SKIP_REASONS = (
    "NO_ENTRY_BAR",
    "NO_WINDOW",
    "NO_CHAIN",
    "NO_STRIKE",
    "NO_ENTRY_MARK",
)


def build_context(
    md: MarketData,
    sig: Signal,
    store: MarksStore,
    chain_cache: ChainCache,
) -> tuple[TradeContext | None, str]:
    """Resolve entry, strike, and holding-window arrays. Returns (ctx, reason)."""
    ei = sig.index + 1
    if ei >= md.n:
        return None, "NO_ENTRY_BAR"
    entry_ts = int(md.ts[ei])
    entry_spot = float(md.open[ei])

    entry_dt = ist_of(entry_ts)
    entry_date = entry_dt.date()
    expiry = entry_date + timedelta(days=cfg.DTE_DAYS)
    deadline_ts = to_unix(
        ist_dt(expiry, cfg.TIME_EXIT_HOUR_IST, cfg.TIME_EXIT_MINUTE_IST)
    )
    if deadline_ts <= entry_ts:
        return None, "NO_WINDOW"

    lo = int(np.searchsorted(md.ts, entry_ts, side="left"))
    hi = int(np.searchsorted(md.ts, deadline_ts, side="right"))
    if hi - lo < 1:
        return None, "NO_WINDOW"

    is_bull = sig.side == "BULLISH"
    opt_type = "call" if is_bull else "put"
    fut_sign = -1 if is_bull else 1

    entry_minute = (entry_ts // 60) * 60
    chain = chain_cache.get(store, entry_date, expiry, entry_minute, opt_type)
    if not chain:
        return None, "NO_CHAIN"

    settle_ts = to_unix(ist_dt(expiry, cfg.EXPIRY_HOUR_IST, cfg.EXPIRY_MINUTE_IST))
    t_years = max(1e-9, (settle_ts - entry_ts) / cfg.SECONDS_PER_YEAR)
    is_call = is_bull
    best: tuple[float, float, float, float] | None = None  # err,strike,mark,delta
    for strike, mark in chain:
        iv = implied_vol_bisection(mark, entry_spot, strike, t_years, is_call)
        if iv is None or iv <= 0:
            continue
        adelta = black76_abs_delta(entry_spot, strike, t_years, iv, is_call)
        err = abs(adelta - cfg.TARGET_ABS_DELTA)
        if best is None or err < best[0]:
            best = (err, float(strike), float(mark), float(adelta))
    if best is None:
        return None, "NO_STRIKE"
    _, strike, entry_mark, adelta = best
    if entry_mark <= 0:
        return None, "NO_ENTRY_MARK"
    entry_delta = adelta if is_call else -adelta
    symbol = option_symbol(opt_type, strike, expiry)

    # window arrays
    w_ts = md.ts[lo:hi].astype(np.int64)
    w_high = md.high[lo:hi].astype(np.float64)
    w_low = md.low[lo:hi].astype(np.float64)
    w_close = md.close[lo:hi].astype(np.float64)

    # option mark series over the holding window (one DB pass, ffilled)
    series = load_symbol_series(store, symbol, entry_ts, deadline_ts)
    w_optmark = _ffill_marks(w_ts, series, entry_mark)

    lot = cfg.LOT_BTC
    opt_open = (w_optmark - entry_mark) * cfg.OPTION_QTY_LOTS * lot
    fut_open = fut_sign * (w_close - entry_spot) * cfg.FUT_QTY_LOTS * lot
    tgt_series = w_high if is_bull else w_low

    if is_bull:
        fav_cum = np.maximum.accumulate(np.maximum(w_high - entry_spot, 0.0))
        adv_cum = np.maximum.accumulate(np.maximum(entry_spot - w_low, 0.0))
    else:
        fav_cum = np.maximum.accumulate(np.maximum(entry_spot - w_low, 0.0))
        adv_cum = np.maximum.accumulate(np.maximum(w_high - entry_spot, 0.0))

    ctx = TradeContext(
        signal=sig,
        entry_ts=entry_ts,
        entry_spot=entry_spot,
        expiry=expiry,
        opt_type=opt_type,
        is_bull=is_bull,
        fut_sign=fut_sign,
        symbol=symbol,
        strike=strike,
        entry_delta=entry_delta,
        entry_mark=entry_mark,
        w_ts=w_ts,
        w_close=w_close,
        w_optmark=w_optmark,
        opt_open=opt_open,
        fut_open=fut_open,
        tgt_series=tgt_series,
        fav_cum=fav_cum,
        adv_cum=adv_cum,
    )
    return ctx, ""


def _ffill_marks(
    w_ts: np.ndarray, series: dict[int, float], entry_mark: float
) -> np.ndarray:
    """Nearest-minute mark, forward-filled; seeded with entry_mark."""
    out = np.empty(w_ts.size, dtype=np.float64)
    last = float(entry_mark)
    tol = cfg.MARK_TOL_SEC
    for i, t in enumerate(w_ts):
        ti = int(t)
        m = series.get(ti)
        if m is None:
            for d in range(60, tol + 1, 60):
                m = series.get(ti - d)
                if m is not None:
                    break
                m = series.get(ti + d)
                if m is not None:
                    break
        if m is not None and m > 0:
            last = float(m)
        out[i] = last
    return out


# ---------------------------------------------------------------------------
# Fees / slippage (per spec — verified, not re-derived)
# ---------------------------------------------------------------------------
def option_fee(premium: float, index_spot: float, qty_lots: int) -> float:
    notional = qty_lots * cfg.LOT_BTC
    by_index = cfg.OPTION_FEE_INDEX_PCT * notional * index_spot
    by_prem = cfg.OPTION_FEE_PREMIUM_PCT * notional * premium
    return min(by_index, by_prem) * cfg.OPTION_FEE_GST_MULT


def option_slip_frac(premium: float) -> float:
    for lo, hi, frac in cfg.OPTION_SLIP_BUCKETS:
        if lo <= premium < hi:
            return frac
    return cfg.OPTION_SLIP_BUCKETS[-1][2]


# ---------------------------------------------------------------------------
# Exit simulation (vectorized; one context, many combos)
# ---------------------------------------------------------------------------
@dataclass
class TradeResult:
    ctx: TradeContext
    target_pts: int
    stop_pct: float
    fut_mode: bool
    capital_used: float
    stop_pts: float
    exit_ts: int
    exit_reason: str       # TARGET | STOP | TIME
    exit_spot: float
    exit_mark: float
    minutes_held: int
    max_fav_pts: float
    max_adv_pts: float
    opt_gross: float
    fut_gross: float
    opt_fee: float
    fut_fee: float
    opt_slip: float
    fut_slip: float
    total_cost: float
    net_pnl: float
    fut_entry_price: float
    fut_exit_price: float


def _first_true(mask: np.ndarray) -> int:
    if mask.any():
        return int(np.argmax(mask))
    return _BIG


def simulate(
    ctx: TradeContext,
    target_pts: int,
    stop_pct: float,
    fut_mode: bool,
    *,
    zero_costs: bool = False,
) -> TradeResult:
    lot = cfg.LOT_BTC
    last = ctx.w_ts.size - 1
    dir_sign = 1.0 if ctx.is_bull else -1.0
    target_spot = ctx.entry_spot + dir_sign * target_pts

    capital_used = ctx.entry_mark * cfg.OPTION_QTY_LOTS * lot
    if fut_mode:
        capital_used += (
            cfg.FUT_INITIAL_MARGIN_PCT * cfg.FUT_QTY_LOTS * lot * ctx.entry_spot
        )
    stop_loss_usd = stop_pct * capital_used

    if ctx.is_bull:
        tgt_mask = ctx.tgt_series >= target_spot
    else:
        tgt_mask = ctx.tgt_series <= target_spot
    net_open = ctx.opt_open + (ctx.fut_open if fut_mode else 0.0)
    stop_mask = net_open <= -stop_loss_usd

    ti = _first_true(tgt_mask)
    si = _first_true(stop_mask)
    if si == _BIG and ti == _BIG:
        exit_idx = last
        reason = "TIME"
    elif si <= ti:
        exit_idx = si
        reason = "STOP"
    else:
        exit_idx = ti
        reason = "TARGET"

    exit_ts = int(ctx.w_ts[exit_idx])
    exit_mark = float(ctx.w_optmark[exit_idx])
    if reason == "TARGET":
        exit_spot = float(target_spot)
    else:
        exit_spot = float(ctx.w_close[exit_idx])

    # equivalent linear stop distance in spot points (delta-based reference)
    per_pt = abs(
        ctx.entry_delta * cfg.OPTION_QTY_LOTS
        + (ctx.fut_sign * cfg.FUT_QTY_LOTS if fut_mode else 0)
    ) * lot
    stop_pts = stop_loss_usd / per_pt if per_pt > 1e-12 else float("nan")

    opt_gross = (exit_mark - ctx.entry_mark) * cfg.OPTION_QTY_LOTS * lot
    fut_gross = 0.0
    fut_entry_price = float("nan")
    fut_exit_price = float("nan")
    if fut_mode:
        fut_entry_price = ctx.entry_spot
        fut_exit_price = exit_spot
        fut_gross = ctx.fut_sign * (exit_spot - ctx.entry_spot) * cfg.FUT_QTY_LOTS * lot

    if zero_costs:
        opt_fee = fut_fee = opt_slip = fut_slip = 0.0
    else:
        opt_fee = option_fee(
            ctx.entry_mark, ctx.entry_spot, cfg.OPTION_QTY_LOTS
        ) + option_fee(exit_mark, exit_spot, cfg.OPTION_QTY_LOTS)
        opt_slip = (
            ctx.entry_mark * option_slip_frac(ctx.entry_mark)
            + exit_mark * option_slip_frac(exit_mark)
        ) * cfg.OPTION_QTY_LOTS * lot
        if fut_mode:
            fut_fee = cfg.FUT_FEE_PCT * cfg.FUT_QTY_LOTS * lot * (
                ctx.entry_spot + exit_spot
            )
            fut_slip = 2.0 * cfg.FUT_SLIP_PTS * cfg.FUT_QTY_LOTS * lot
        else:
            fut_fee = 0.0
            fut_slip = 0.0

    total_cost = opt_fee + fut_fee + opt_slip + fut_slip
    net_pnl = opt_gross + fut_gross - total_cost

    return TradeResult(
        ctx=ctx,
        target_pts=target_pts,
        stop_pct=stop_pct,
        fut_mode=fut_mode,
        capital_used=capital_used,
        stop_pts=stop_pts,
        exit_ts=exit_ts,
        exit_reason=reason,
        exit_spot=exit_spot,
        exit_mark=exit_mark,
        minutes_held=int((exit_ts - ctx.entry_ts) // 60),
        max_fav_pts=float(ctx.fav_cum[exit_idx]),
        max_adv_pts=float(ctx.adv_cum[exit_idx]),
        opt_gross=opt_gross,
        fut_gross=fut_gross,
        opt_fee=opt_fee,
        fut_fee=fut_fee,
        opt_slip=opt_slip,
        fut_slip=fut_slip,
        total_cost=total_cost,
        net_pnl=net_pnl,
        fut_entry_price=fut_entry_price,
        fut_exit_price=fut_exit_price,
    )
