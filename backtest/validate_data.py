# validate_data.py — Sanity-check loaded Delta options trade data
#
# Standalone. Do NOT import from backend/.

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.engine import BacktestEngine


def _as_date(v: object) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    return pd.Timestamp(v).date()


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def print_monthly_stats(df: pd.DataFrame) -> None:
    print("\n=== Monthly row counts / date ranges ===")
    work = df.copy()
    work["_d"] = work["ist_date"].map(_as_date)
    work["_m"] = work["_d"].map(_month_key)
    for month, g in work.groupby("_m", sort=True):
        dates = sorted(g["_d"].unique())
        print(
            f"  {month}: rows={len(g):,}  "
            f"dates={dates[0]} -> {dates[-1]}  "
            f"n_days={len(dates)}"
        )


def print_expiry_stats(df: pd.DataFrame) -> None:
    expiries = sorted({_as_date(x) for x in df["expiry_date"].dropna().unique()})
    print(f"\n=== Unique expiry dates: {len(expiries)} ===")
    if expiries:
        print(f"  first={expiries[0]}  last={expiries[-1]}")


def print_0dte_last_trade(df: pd.DataFrame) -> None:
    """
    For each month, pick one 0DTE expiry (ist_date == expiry_date) and print
    the last trade timestamp. Expect ~12:00 UTC (= 17:30 IST) on full days.
    """
    print("\n=== Last trade on one 0DTE expiry per month ===")
    print("  (expect last print near 12:00 UTC = 17:30 IST)")
    ist = df["ist_date"].astype(object).map(_as_date)
    exp = df["expiry_date"].astype(object).map(_as_date)
    zero_mask = ist == exp
    zero_dte = df.loc[zero_mask].copy()
    zero_dte["_ist"] = ist.loc[zero_mask].to_numpy()
    zero_dte["_exp"] = exp.loc[zero_mask].to_numpy()
    zero_dte["_m"] = [_month_key(d) for d in zero_dte["_ist"]]

    if zero_dte.empty:
        print("  No 0DTE rows in dataset")
        return

    for month, g in zero_dte.groupby("_m", sort=True):
        by_exp = g.groupby("_exp", sort=True).size().sort_values(ascending=False)
        exp_day = by_exp.index[0]
        day = g.loc[g["_exp"] == exp_day]
        last_ist = pd.to_datetime(day["ist_time"]).max()
        last_utc = last_ist - pd.Timedelta(hours=5, minutes=30)
        print(
            f"  {month}: expiry={exp_day}  last_ist={last_ist}  "
            f"last_utc~={last_utc}  rows={len(day):,}"
        )


def put_call_parity_spot(df: pd.DataFrame) -> None:
    """
    Spot check: S ≈ C − P + K at three near-ATM strikes on one liquid day.
    Three estimates should land close to each other.
    """
    print("\n=== Put-call parity spot check (S = C - P + K) ===")
    ist = df["ist_date"].astype(object).map(_as_date)
    exp = df["expiry_date"].astype(object).map(_as_date)
    zero_mask = ist == exp
    zero = df.loc[zero_mask].copy()
    if zero.empty:
        print("  No 0DTE data — skip")
        return
    zero["_ist"] = ist.loc[zero_mask].to_numpy()
    day_counts = zero.groupby("_ist").size().sort_values(ascending=False)
    trade_day = day_counts.index[0]
    day = zero.loc[zero["_ist"] == trade_day]
    expiry = trade_day

    # Window around 10:00 IST
    t0 = datetime.combine(trade_day, time(10, 0, 0))
    win = day.loc[
        (day["ist_time"] >= t0 - timedelta(minutes=5))
        & (day["ist_time"] <= t0 + timedelta(minutes=5))
    ]
    if win.empty:
        win = day

    # ATM ≈ strike where |median(call) − median(put)| is smallest
    best_k: float | None = None
    best_diff = float("inf")
    for k, g in win.groupby("strike"):
        c = g.loc[g["opt_type"] == "call", "price"]
        p = g.loc[g["opt_type"] == "put", "price"]
        if len(c) < 2 or len(p) < 2:
            continue
        diff = abs(float(c.median()) - float(p.median()))
        if diff < best_diff:
            best_diff = diff
            best_k = float(k)

    if best_k is None:
        print(f"  {trade_day}: could not find ATM in window")
        return

    strikes = sorted({float(s) for s in win["strike"].unique()})
    # Three strikes: ATM- step, ATM, ATM+ step (use nearest available)
    step = 200.0
    candidates = [best_k - step, best_k, best_k + step]
    chosen: list[float] = []
    for target in candidates:
        nearest = min(strikes, key=lambda s: abs(s - target))
        if nearest not in chosen:
            chosen.append(nearest)
    while len(chosen) < 3 and strikes:
        for s in strikes:
            if s not in chosen:
                chosen.append(s)
                break
        else:
            break

    print(f"  day={trade_day} expiry={expiry} atm~={best_k:.0f}")
    spots: list[float] = []
    for k in chosen[:3]:
        g = win.loc[win["strike"] == k]
        c = g.loc[g["opt_type"] == "call", "price"]
        p = g.loc[g["opt_type"] == "put", "price"]
        if c.empty or p.empty:
            print(f"  K={k:.0f}: missing call or put")
            continue
        c_med = float(c.median())
        p_med = float(p.median())
        s_est = c_med - p_med + k
        spots.append(s_est)
        print(
            f"  K={k:.0f}  C={c_med:.1f}  P={p_med:.1f}  "
            f"S~={s_est:.1f}"
        )
    if len(spots) >= 2:
        spread = max(spots) - min(spots)
        print(f"  spot spread across strikes: {spread:.1f} USD")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate backtest data load (raw zips + legacy CSV)"
    )
    parser.add_argument(
        "--data-dir",
        default="backtest/data",
        help="Legacy CSV dir (sibling data_raw/ is also read)",
    )
    parser.add_argument(
        "--cache-refresh",
        action="store_true",
        help="Ignore parquet cache and re-parse sources",
    )
    args = parser.parse_args(argv)

    engine = BacktestEngine({"cache_refresh": bool(args.cache_refresh)})
    print(f"Loading from data_dir={args.data_dir} ...")
    df = engine.load_data_dir(args.data_dir)

    print_monthly_stats(df)
    print_expiry_stats(df)
    print_0dte_last_trade(df)
    put_call_parity_spot(df)
    print("\nValidation done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
