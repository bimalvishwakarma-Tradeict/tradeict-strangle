#!/usr/bin/env python3
"""
S008 Step 0 — IV beta measurement (signal vs 0DTE implied vol).

Not a strategy. No P&L. Builds expanding-window stress signal and measures
whether high-signal days show elevated 0DTE Black-76 IV at ATM±2000.

Window: 2025-07-04 .. 2026-09-20 (IST trading days with data).
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
from dataclasses import dataclass
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

from s004_gate import implied_vol_bisection  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
MARKS_DIR = _BACKTEST / "cache" / "option_marks"
DATA_1M_DIR = _BACKTEST / "data_1m"
RESULTS_DIR = _BACKTEST / "results"
OUT_CSV = RESULTS_DIR / "s008_iv_beta.csv"
OUT_TXT = RESULTS_DIR / "s008_iv_beta.txt"

D0 = date(2025, 7, 4)
D1 = date(2026, 9, 20)
T_YEARS = 8.5 / 24.0 / 365.0  # 09:00 → 17:30 IST
WING_PTS = 2000.0
MARK_TOL_SEC = 60
MIN_HIST_FOR_DECILE = 10

logger = logging.getLogger("s008_iv_beta")


@dataclass
class DayRow:
    d: date
    spot_0900: float
    call_strike: float
    put_strike: float
    call_mark: float
    put_mark: float
    call_iv: float
    put_iv: float
    iv_mean: float
    prev_rvol: float
    overnight: float
    sig: float
    sig_rank_pct: float
    sig_decile: int | None
    realized_move_pct: float


def ist_dt(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def find_spot_csv() -> Path:
    files = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    if not files:
        raise FileNotFoundError(f"no BTCUSD_1m_*.csv in {DATA_1M_DIR}")
    return files[-1]


def load_spot_close(path: Path) -> dict[int, float]:
    out: dict[int, float] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[int(row["open_time_unix"])] = float(row["close"])
    return out


class MarksStore:
    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self.available = sorted(
            p.stem.replace("marks_", "") for p in MARKS_DIR.glob("marks_*.sqlite")
        )

    def conn(self, d: date) -> sqlite3.Connection | None:
        ym = f"{d.year:04d}-{d.month:02d}"
        if ym not in self.available:
            return None
        if ym not in self._conns:
            path = MARKS_DIR / f"marks_{ym}.sqlite"
            self._conns[ym] = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro", uri=True
            )
        return self._conns[ym]

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()


def format_symbol(opt: str, strike: float, exp: date) -> str:
    # marks DB: C-BTC-{strike}-{DDMMYY} / P-BTC-...
    prefix = "C" if opt.lower().startswith("c") else "P"
    return f"{prefix}-BTC-{int(strike)}-{exp.strftime('%d%m%y')}"


def mark_at(
    conn: sqlite3.Connection, symbol: str, ts: int
) -> tuple[int, float] | None:
    """PK lookup (symbol, ts); fallback ±MARK_TOL_SEC."""
    minute = (ts // 60) * 60
    row = conn.execute(
        "SELECT ts, close FROM marks WHERE symbol=? AND ts=?",
        (symbol, minute),
    ).fetchone()
    if row is not None and row[1] is not None and float(row[1]) > 0:
        return int(row[0]), float(row[1])
    best: tuple[int, float] | None = None
    best_abs = None
    for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
        if d == 0:
            continue
        row = conn.execute(
            "SELECT ts, close FROM marks WHERE symbol=? AND ts=?",
            (symbol, minute + d),
        ).fetchone()
        if row is None or row[1] is None or float(row[1]) <= 0:
            continue
        ad = abs(int(row[0]) - minute)
        if best_abs is None or ad < best_abs:
            best_abs = ad
            best = (int(row[0]), float(row[1]))
    return best


def load_chain_pk(
    conn: sqlite3.Connection,
    expiry: date,
    ts: int,
    spot: float,
    *,
    half_width: float = 6000.0,
    step: float = 100.0,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """
    Fast chain via PRIMARY KEY (symbol, ts). Probe strikes around spot.
    """
    atm0 = round(spot / step) * step
    calls: list[tuple[float, float]] = []
    puts: list[tuple[float, float]] = []
    k = atm0 - half_width
    while k <= atm0 + half_width:
        cs = format_symbol("C", k, expiry)
        ps = format_symbol("P", k, expiry)
        cm = mark_at(conn, cs, ts)
        pm = mark_at(conn, ps, ts)
        if cm is not None:
            calls.append((k, cm[1]))
        if pm is not None:
            puts.append((k, pm[1]))
        k += step
    return calls, puts


def resolve_mark_ts(conn: sqlite3.Connection, expiry: str, ts: int) -> int | None:
    # retained for compatibility — unused in fast path
    minute = (ts // 60) * 60
    row = conn.execute(
        "SELECT ts FROM marks WHERE expiry=? AND ts=? LIMIT 1",
        (expiry, minute),
    ).fetchone()
    if row is not None:
        return int(row[0])
    return None


def load_chain(
    conn: sqlite3.Connection, expiry: str, ts: int, opt_type: str
) -> list[tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT strike, close FROM marks
        WHERE expiry=? AND ts=? AND opt_type=?
          AND close IS NOT NULL AND close > 0
        ORDER BY strike
        """,
        (expiry, ts, opt_type),
    ).fetchall()
    return [(float(k), float(px)) for k, px in rows]


def nearest_strike(strikes: list[float], target: float) -> float | None:
    if not strikes:
        return None
    return min(strikes, key=lambda k: (abs(k - target), k))


def pick_wing_pair(
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
    spot: float,
) -> tuple[float, float, float, float] | None:
    """ATM±2000: nearest listed call to ATM+2000, put to ATM-2000."""
    c_strikes = [k for k, _ in calls]
    p_strikes = [k for k, _ in puts]
    common = sorted(set(c_strikes) & set(p_strikes))
    atm = nearest_strike(common if common else c_strikes, spot)
    if atm is None:
        return None
    ck = nearest_strike(c_strikes, atm + WING_PTS)
    pk = nearest_strike(p_strikes, atm - WING_PTS)
    if ck is None or pk is None:
        return None
    c_by = {k: px for k, px in calls}
    p_by = {k: px for k, px in puts}
    return ck, pk, c_by[ck], p_by[pk]


def session_log_stdev(
    spot: dict[int, float], d: date, h0: int, m0: int, h1: int, m1: int
) -> float | None:
    """Stdev of consecutive 1m log returns from h0:m0 through h1:m1 inclusive."""
    t0 = to_unix(ist_dt(d, h0, m0))
    t1 = to_unix(ist_dt(d, h1, m1))
    closes: list[float] = []
    t = t0
    while t <= t1:
        px = spot.get(t)
        if px is not None and px > 0:
            closes.append(px)
        t += 60
    if len(closes) < 30:
        return None
    rets: list[float] = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 20:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var)


def expanding_rank_pct(history: list[float], value: float) -> float:
    """
    Expanding percentile of `value` among history INCLUDING value.
    Fraction of observations <= value. In (0, 1].
    """
    n = len(history)
    if n <= 0:
        return 1.0
    le = sum(1 for v in history if v <= value)
    return le / n


def expanding_decile(rank_pct: float, n_hist: int) -> int | None:
    if n_hist < MIN_HIST_FOR_DECILE:
        return None
    # rank_pct in (0,1] → deciles 0..9 (9 = top)
    d = int(math.floor(max(0.0, min(0.999999, rank_pct)) * 10.0))
    return min(9, max(0, d))


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return float("nan")
    return num / (dx * dy)


def spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = 0.5 * (i + j) + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    return pearson(ranks(xs), ranks(ys))


def iter_weekdays(d0: date, d1: date) -> list[date]:
    out: list[date] = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_rows(
    spot: dict[int, float],
    store: MarksStore,
    d0: date,
    d1: date,
) -> tuple[list[DayRow], dict[str, int]]:
    skips = {
        "no_spot_0900": 0,
        "no_prev_session": 0,
        "no_marks": 0,
        "no_chain": 0,
        "no_iv": 0,
        "no_realized": 0,
    }
    rows: list[DayRow] = []
    hist_rvol: list[float] = []
    hist_onn: list[float] = []
    hist_sig: list[float] = []

    days = iter_weekdays(d0, d1)
    for d in days:
        ts_0900 = to_unix(ist_dt(d, 9, 0))
        spot_0900 = spot.get(ts_0900)
        if spot_0900 is None or spot_0900 <= 0:
            skips["no_spot_0900"] += 1
            continue

        prev_rvol: float | None = None
        overnight: float | None = None
        found_prev = False
        probe = d - timedelta(days=1)
        for _ in range(10):
            ts_prev_1729 = to_unix(ist_dt(probe, 17, 29))
            px_prev = spot.get(ts_prev_1729)
            if px_prev is not None and px_prev > 0:
                rv = session_log_stdev(spot, probe, 9, 0, 17, 29)
                if rv is not None and rv > 0:
                    overnight = abs(spot_0900 - px_prev) / px_prev
                    prev_rvol = rv
                    found_prev = True
                    break
            probe -= timedelta(days=1)
        if not found_prev or prev_rvol is None or overnight is None:
            skips["no_prev_session"] += 1
            continue

        conn = store.conn(d)
        if conn is None:
            skips["no_marks"] += 1
            continue
        calls, puts = load_chain_pk(conn, d, ts_0900, spot_0900)
        if not calls or not puts:
            skips["no_chain"] += 1
            continue
        picked = pick_wing_pair(calls, puts, spot_0900)
        if picked is None:
            skips["no_chain"] += 1
            continue
        ck, pk, c_mark, p_mark = picked

        # Black-76 IV; F = spot (futures proxy used elsewhere in mark path)
        civ = implied_vol_bisection(c_mark, spot_0900, ck, T_YEARS, True)
        piv = implied_vol_bisection(p_mark, spot_0900, pk, T_YEARS, False)
        if civ is None or piv is None or civ <= 0 or piv <= 0:
            skips["no_iv"] += 1
            continue

        ts_1729 = to_unix(ist_dt(d, 17, 29))
        px_eod = spot.get(ts_1729)
        if px_eod is None or px_eod <= 0:
            skips["no_realized"] += 1
            continue
        realized = abs(px_eod - spot_0900) / spot_0900 * 100.0

        hist_rvol.append(prev_rvol)
        hist_onn.append(overnight)
        r_rvol = expanding_rank_pct(hist_rvol, prev_rvol)
        r_onn = expanding_rank_pct(hist_onn, overnight)
        sig = 0.5 * (r_rvol + r_onn)
        hist_sig.append(sig)
        sig_rank = expanding_rank_pct(hist_sig, sig)
        decile = expanding_decile(sig_rank, len(hist_sig))

        rows.append(
            DayRow(
                d=d,
                spot_0900=spot_0900,
                call_strike=ck,
                put_strike=pk,
                call_mark=c_mark,
                put_mark=p_mark,
                call_iv=civ,
                put_iv=piv,
                iv_mean=0.5 * (civ + piv),
                prev_rvol=prev_rvol,
                overnight=overnight,
                sig=sig,
                sig_rank_pct=sig_rank,
                sig_decile=decile,
                realized_move_pct=realized,
            )
        )

    return rows, skips


def summarize(rows: list[DayRow]) -> list[str]:
    lines: list[str] = []
    lines.append("===== S008 IV BETA MEASUREMENT =====")
    lines.append(f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    lines.append(f"window={D0.isoformat()} .. {D1.isoformat()}")
    lines.append(f"T_years={T_YEARS:.10f} (=8.5h/365d) wing_pts={WING_PTS:.0f}")
    lines.append(f"n_days={len(rows)}")
    lines.append("")

    usable = [r for r in rows if r.sig_decile is not None]
    lines.append(f"n_days_with_decile={len(usable)} (min_hist={MIN_HIST_FOR_DECILE})")

    # --- (a) decile table ---
    lines.append("")
    lines.append("===== (a) SIG DECILE → MEAN 0DTE IV =====")
    lines.append(
        f"{'decile':>6} {'n':>5} {'iv_call':>10} {'iv_put':>10} "
        f"{'iv_mean':>10} {'realized%':>10} {'sig_mean':>10}"
    )
    by_dec: dict[int, list[DayRow]] = {i: [] for i in range(10)}
    for r in usable:
        assert r.sig_decile is not None
        by_dec[r.sig_decile].append(r)

    def _mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else float("nan")

    for d in range(10):
        grp = by_dec[d]
        if not grp:
            lines.append(f"{d:6d} {0:5d}")
            continue
        lines.append(
            f"{d:6d} {len(grp):5d} "
            f"{_mean([r.call_iv for r in grp]):10.4f} "
            f"{_mean([r.put_iv for r in grp]):10.4f} "
            f"{_mean([r.iv_mean for r in grp]):10.4f} "
            f"{_mean([r.realized_move_pct for r in grp]):10.4f} "
            f"{_mean([r.sig for r in grp]):10.4f}"
        )

    def top_vs_rest(
        rows_u: list[DayRow], top_frac: float, label: str
    ) -> list[str]:
        out: list[str] = []
        ranked = sorted(rows_u, key=lambda r: r.sig)
        n = len(ranked)
        k = max(1, int(math.ceil(n * top_frac)))
        top = ranked[-k:]
        rest = ranked[:-k] if k < n else []
        iv_top = _mean([r.iv_mean for r in top])
        iv_rest = _mean([r.iv_mean for r in rest]) if rest else float("nan")
        rz_top = _mean([r.realized_move_pct for r in top])
        rz_rest = _mean([r.realized_move_pct for r in rest]) if rest else float("nan")
        ratio = iv_top / iv_rest if iv_rest and iv_rest > 0 else float("nan")
        pct = (ratio - 1.0) * 100.0 if ratio == ratio else float("nan")
        rz_ratio = (
            rz_top / rz_rest if rz_rest and rz_rest > 0 else float("nan")
        )
        rz_pct = (rz_ratio - 1.0) * 100.0 if rz_ratio == rz_ratio else float("nan")
        out.append(f"----- {label} (top {top_frac:.0%} = n={len(top)}) -----")
        out.append(
            f"  IV  top={iv_top:.4f}  rest={iv_rest:.4f}  "
            f"ratio={ratio:.4f}  lift={pct:.2f}%"
        )
        out.append(
            f"  REALIZED% top={rz_top:.4f}  rest={rz_rest:.4f}  "
            f"ratio={rz_ratio:.4f}  lift={rz_pct:.2f}%"
        )
        if pct == pct and rz_pct == rz_pct:
            out.append(
                f"  COMPARE: IV_lift={pct:.2f}% vs realized_lift={rz_pct:.2f}%  "
                f"gap(IV-real)={pct - rz_pct:.2f} pp"
            )
            if pct > rz_pct:
                out.append(
                    "  → IV rises MORE than realized on flagged days "
                    "(market over-pricing stress vs subsequent move)."
                )
            elif pct < rz_pct:
                out.append(
                    "  → Realized rises MORE than IV "
                    "(market under-pricing relative to subsequent move)."
                )
            else:
                out.append("  → IV lift ≈ realized lift.")
        return out

    # Beta on expanding top-decile membership (decile==9)
    lines.append("")
    lines.append("===== (b) IV BETA — expanding top decile vs rest =====")
    top_d = by_dec[9]
    rest_d = [r for r in usable if r.sig_decile is not None and r.sig_decile < 9]
    iv_top = _mean([r.iv_mean for r in top_d])
    iv_rest = _mean([r.iv_mean for r in rest_d])
    ratio = iv_top / iv_rest if iv_rest > 0 else float("nan")
    pct = (ratio - 1.0) * 100.0 if ratio == ratio else float("nan")
    lines.append(f"  n_top_decile={len(top_d)} n_rest={len(rest_d)}")
    lines.append(
        f"  IV_BETA ratio={ratio:.4f}  lift={pct:.2f}%  "
        f"(top_mean_iv={iv_top:.4f} rest_mean_iv={iv_rest:.4f})"
    )
    lines.append(
        "  ★ DECISION NUMBER: "
        f"top-decile IV is {pct:.2f}% above the rest"
        if pct == pct
        else "  ★ DECISION NUMBER: n/a"
    )

    # (c) top 5% / 20% by final expanding sig level among usable
    lines.append("")
    lines.append("===== (c) TOP 5% / 20% (by expanding sig level) =====")
    lines.extend(top_vs_rest(usable, 0.05, "TOP 5%"))
    lines.extend(top_vs_rest(usable, 0.20, "TOP 20%"))
    # also top 10% for alignment with decile
    lines.extend(top_vs_rest(usable, 0.10, "TOP 10%"))

    # (d) correlation
    lines.append("")
    lines.append("===== (d) SCATTER / CORRELATION sig vs iv_mean =====")
    xs = [r.sig for r in rows]
    ys = [r.iv_mean for r in rows]
    pr = pearson(xs, ys)
    sp = spearman(xs, ys)
    lines.append(f"  Pearson r={pr:.4f}")
    lines.append(f"  Spearman ρ={sp:.4f}")
    lines.append(f"  n={len(rows)}")

    # (e) IV lift vs realized lift for top decile
    lines.append("")
    lines.append("===== (e) IV LIFT vs REALIZED LIFT (top decile) =====")
    rz_top = _mean([r.realized_move_pct for r in top_d])
    rz_rest = _mean([r.realized_move_pct for r in rest_d])
    rz_ratio = rz_top / rz_rest if rz_rest > 0 else float("nan")
    rz_pct = (rz_ratio - 1.0) * 100.0 if rz_ratio == rz_ratio else float("nan")
    lines.append(
        f"  realized% top={rz_top:.4f} rest={rz_rest:.4f} "
        f"ratio={rz_ratio:.4f} lift={rz_pct:.2f}%"
    )
    lines.append(
        f"  IV lift={pct:.2f}%  realized lift={rz_pct:.2f}%  "
        f"gap={pct - rz_pct:.2f} pp"
        if pct == pct and rz_pct == rz_pct
        else "  gap n/a"
    )
    if pct == pct and rz_pct == rz_pct:
        if pct > rz_pct + 1.0:
            lines.append(
                "  INTERPRETATION: IV overshoots realized on high-sig days — "
                "buying 0DTE options into the signal pays elevated premium."
            )
        elif rz_pct > pct + 1.0:
            lines.append(
                "  INTERPRETATION: realized moves more than IV elevates — "
                "options may be cheap relative to subsequent move."
            )
        else:
            lines.append(
                "  INTERPRETATION: IV and realized lifts are similar."
            )

    lines.append("")
    return lines


def write_csv(rows: list[DayRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "date",
        "spot_0900",
        "call_strike",
        "put_strike",
        "call_mark",
        "put_mark",
        "call_iv",
        "put_iv",
        "iv_mean",
        "prev_rvol",
        "overnight",
        "sig",
        "sig_rank_pct",
        "sig_decile",
        "realized_move_pct",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "date": r.d.isoformat(),
                    "spot_0900": f"{r.spot_0900:.4f}",
                    "call_strike": f"{r.call_strike:.0f}",
                    "put_strike": f"{r.put_strike:.0f}",
                    "call_mark": f"{r.call_mark:.6f}",
                    "put_mark": f"{r.put_mark:.6f}",
                    "call_iv": f"{r.call_iv:.6f}",
                    "put_iv": f"{r.put_iv:.6f}",
                    "iv_mean": f"{r.iv_mean:.6f}",
                    "prev_rvol": f"{r.prev_rvol:.8f}",
                    "overnight": f"{r.overnight:.8f}",
                    "sig": f"{r.sig:.6f}",
                    "sig_rank_pct": f"{r.sig_rank_pct:.6f}",
                    "sig_decile": "" if r.sig_decile is None else r.sig_decile,
                    "realized_move_pct": f"{r.realized_move_pct:.6f}",
                }
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S008 IV beta measurement")
    ap.add_argument("--from", dest="d0", type=str, default=D0.isoformat())
    ap.add_argument("--to", dest="d1", type=str, default=D1.isoformat())
    args = ap.parse_args()
    d0 = date.fromisoformat(args.d0)
    d1 = date.fromisoformat(args.d1)

    spot_path = find_spot_csv()
    logger.info("spot_csv=%s", spot_path.name)
    spot = load_spot_close(spot_path)
    logger.info("spot_bars=%d", len(spot))

    store = MarksStore()
    logger.info("marks_months=%s", store.available)
    rows, skips = build_rows(spot, store, d0, d1)
    store.close()
    logger.info("built n_days=%d skips=%s", len(rows), skips)

    lines = summarize(rows)
    lines.append("===== SKIPS =====")
    for k, v in skips.items():
        lines.append(f"  {k}={v}")
    lines.append(f"csv={OUT_CSV}")

    write_csv(rows, OUT_CSV)
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for ln in lines:
        logger.info("%s", ln)
        print(ln)


if __name__ == "__main__":
    main()
