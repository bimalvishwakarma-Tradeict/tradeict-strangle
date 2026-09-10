# data_loader.py — Load and query Delta Exchange India options trade CSVs
#
# Standalone backtest helper. Do NOT import from backend/.

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# C-BTC-73600-010626  or  P-BTC-73400-010626
_SYMBOL_RE = re.compile(
    r"^(?P<typ>[CP])-BTC-(?P<strike>\d+)-(?P<ddmmyy>\d{6})$"
)

# Skip expensive symbol filter when df is already a small pre-filtered slice
_LARGE_DF_ROWS = 100_000

# Columns kept after slim (everything else dropped post-parse)
_SLIM_KEEP = (
    "price",
    "size",
    "buyer_role",
    "opt_type",
    "strike",
    "expiry_date",
    "ist_time",
    "ist_date",
)


def _parse_expiry_ddmmyy(token: str) -> date:
    """Parse DDMMYY expiry token into a date (e.g. '010626' → 2026-06-01)."""
    day = int(token[0:2])
    month = int(token[2:4])
    year = 2000 + int(token[4:6])
    return date(year, month, day)


def _as_date(value: date | datetime | pd.Timestamp | str) -> date:
    """Normalize trade/expiry args to datetime.date for fast equality checks."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def slim_trade_frame(df: pd.DataFrame, *, log: bool = True) -> pd.DataFrame:
    """
    Drop unused raw columns and downcast dtypes for memory.

    Verified: DataFrame column `trade_date` is never read after load_csv
    (filters use ist_date). Safe to drop with product_symbol + timestamp.
    """
    if df is None or df.empty:
        return df

    before = int(df.memory_usage(deep=True).sum())

    drop_cols = [
        c
        for c in ("product_symbol", "timestamp", "trade_date")
        if c in df.columns
    ]
    out = df.drop(columns=drop_cols) if drop_cols else df.copy()

    # Keep only known trade columns when present (ignore extras)
    keep = [c for c in _SLIM_KEEP if c in out.columns]
    if keep and list(out.columns) != keep:
        out = out.loc[:, keep]

    if "price" in out.columns and out["price"].dtype != np.float32:
        out["price"] = pd.to_numeric(out["price"], errors="coerce").astype(
            np.float32
        )
    if "size" in out.columns and out["size"].dtype != np.int32:
        out["size"] = (
            pd.to_numeric(out["size"], errors="coerce")
            .fillna(0)
            .round()
            .astype(np.int32)
        )
    if "strike" in out.columns and out["strike"].dtype != np.float32:
        out["strike"] = pd.to_numeric(out["strike"], errors="coerce").astype(
            np.float32
        )
    if "buyer_role" in out.columns:
        if out["buyer_role"].dtype.name != "category":
            out["buyer_role"] = (
                out["buyer_role"]
                .astype(str)
                .str.lower()
                .str.strip()
                .astype("category")
            )
    if "opt_type" in out.columns and out["opt_type"].dtype.name != "category":
        out["opt_type"] = out["opt_type"].astype("category")
    if "expiry_date" in out.columns:
        if out["expiry_date"].dtype.name != "category":
            out["expiry_date"] = pd.Categorical(
                pd.to_datetime(out["expiry_date"], errors="coerce").dt.date
            )
    if "ist_date" in out.columns:
        if out["ist_date"].dtype.name != "category":
            out["ist_date"] = pd.Categorical(
                pd.to_datetime(out["ist_date"], errors="coerce").dt.date
            )

    after = int(out.memory_usage(deep=True).sum())
    if log:
        print(
            f"Slim frame: {before / (1024 ** 2):.1f} MiB -> "
            f"{after / (1024 ** 2):.1f} MiB "
            f"(saved {(before - after) / (1024 ** 2):.1f} MiB)"
        )
    return out


def _frame_needs_slim(df: pd.DataFrame) -> bool:
    """True if cache/frame still has fat raw columns that should be dropped."""
    return any(
        c in df.columns for c in ("product_symbol", "timestamp", "trade_date")
    )


class DataLoader:
    """Load Delta options trade CSVs and query ATM / premiums by IST time."""

    def load_csv(self, filepath: str | Path) -> pd.DataFrame:
        """
        Load a Delta trade CSV and enrich with opt_type, strike, expiry, IST times.

        Returns slim DataFrame:
          price, size, buyer_role, opt_type, strike, expiry_date, ist_time, ist_date
        """
        path = Path(filepath)
        raw = pd.read_csv(path)

        required = {"product_symbol", "price", "size", "timestamp", "buyer_role"}
        missing = required - set(raw.columns)
        if missing:
            raise ValueError(f"CSV missing columns: {sorted(missing)}")

        n_raw = len(raw)
        parsed = raw["product_symbol"].astype(str).str.extract(_SYMBOL_RE)
        valid_mask = parsed["typ"].notna()
        dropped = int((~valid_mask).sum())

        df = raw.loc[valid_mask].copy()
        parsed = parsed.loc[valid_mask]

        df["opt_type"] = parsed["typ"].map({"C": "call", "P": "put"})
        df["strike"] = parsed["strike"].astype(float)
        df["expiry_date"] = parsed["ddmmyy"].map(_parse_expiry_ddmmyy)

        # Timestamp is UTC
        ts_utc = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        bad_ts = int(ts_utc.isna().sum())
        if bad_ts:
            df = df.loc[ts_utc.notna()].copy()
            ts_utc = ts_utc.loc[ts_utc.notna()]
            dropped += bad_ts

        df["trade_date"] = ts_utc.dt.date

        # IST = UTC + 5:30 — store naive datetime / date
        ist = ts_utc + pd.Timedelta(hours=5, minutes=30)
        df["ist_time"] = ist.dt.tz_localize(None)
        df["ist_date"] = df["ist_time"].dt.date

        # Normalize date columns to uniform Python date objects (not mixed types)
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors="coerce").dt.date
        df["ist_date"] = pd.to_datetime(df["ist_date"], errors="coerce").dt.date
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.date

        # Drop rows with bad dates
        bad_dates = df["expiry_date"].isna() | df["ist_date"].isna()
        if bool(bad_dates.any()):
            n_bad = int(bad_dates.sum())
            df = df.loc[~bad_dates].copy()
            dropped += n_bad

        print(
            f"Loaded {len(df)} rows from {path.name} "
            f"({dropped} dropped of {n_raw} raw)"
        )
        # Slim at end of parse (drops product_symbol/timestamp/trade_date)
        return slim_trade_frame(df.reset_index(drop=True), log=True)

    def filter_symbol(
        self,
        df: pd.DataFrame,
        expiry_date: date | datetime,
        opt_type: str,
        strike: float,
    ) -> pd.DataFrame:
        """
        Pre-filter DataFrame for one specific option symbol.

        Call ONCE per strike, then reuse for time lookups — much faster than
        filtering the full frame every minute.
        """
        expiry_date = _as_date(expiry_date)
        opt = str(opt_type).lower().strip()
        strike_f = float(strike)
        mask = (
            (df["expiry_date"] == expiry_date)
            & (df["opt_type"] == opt)
            & (df["strike"] == strike_f)
        )
        return df.loc[mask].copy()

    def get_atm_strike(
        self,
        df: pd.DataFrame,
        trade_date: date,
        expiry_date: date,
        entry_ist_hour: int = 10,
        entry_ist_minute: int = 0,
        window_minutes: int = 15,
    ) -> float:
        """
        Find ATM strike: min |call_median − put_median| in entry window,
        requiring ≥3 trades per side.
        """
        # Ensure date types match for fast comparison
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if isinstance(expiry_date, datetime):
            expiry_date = expiry_date.date()
        trade_date = _as_date(trade_date)
        expiry_date = _as_date(expiry_date)

        day = df[
            (df["ist_date"] == trade_date) & (df["expiry_date"] == expiry_date)
        ]
        if day.empty:
            raise ValueError(
                f"No trades for trade_date={trade_date} expiry={expiry_date}"
            )

        entry = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            entry_ist_hour,
            entry_ist_minute,
            0,
        )
        win = timedelta(minutes=int(window_minutes))
        window = day[
            (day["ist_time"] >= entry - win) & (day["ist_time"] <= entry + win)
        ]
        if window.empty:
            raise ValueError(
                f"No trades in ±{window_minutes}m window around {entry}"
            )

        best_strike: float | None = None
        best_diff = float("inf")

        for strike, g in window.groupby("strike"):
            calls = g.loc[g["opt_type"] == "call", "price"]
            puts = g.loc[g["opt_type"] == "put", "price"]
            if len(calls) < 3 or len(puts) < 3:
                continue
            call_med = float(calls.median())
            put_med = float(puts.median())
            diff = abs(call_med - put_med)
            if diff < best_diff:
                best_diff = diff
                best_strike = float(strike)

        if best_strike is None:
            raise ValueError(
                f"No valid ATM found for {trade_date} / {expiry_date} "
                f"(need ≥3 call and put trades per strike in window)"
            )
        return best_strike

    def get_price_at_time(
        self,
        df: pd.DataFrame,
        trade_date: date,
        expiry_date: date,
        opt_type: str,
        strike: float,
        ist_datetime: datetime,
        lookback_minutes: int = 30,
    ) -> float | None:
        """Last trade price in [ist_datetime − lookback, ist_datetime]."""
        # Ensure date types match for fast comparison
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if isinstance(expiry_date, datetime):
            expiry_date = expiry_date.date()
        expiry_date = _as_date(expiry_date)

        opt = str(opt_type).lower().strip()
        strike_f = float(strike)
        lookback = timedelta(minutes=int(lookback_minutes))
        start = ist_datetime - lookback

        # Filter only when frame is large or still multi-symbol
        needs_filter = len(df) > _LARGE_DF_ROWS or (
            "strike" in df.columns
            and (
                df["expiry_date"].nunique(dropna=True) > 1
                or df["opt_type"].nunique(dropna=True) > 1
                or df["strike"].nunique(dropna=True) > 1
            )
        )
        work = (
            self.filter_symbol(df, expiry_date, opt, strike_f)
            if needs_filter
            else df
        )

        subset = work[
            (work["ist_time"] >= start) & (work["ist_time"] <= ist_datetime)
        ]
        if subset.empty:
            return None

        if not subset["ist_time"].is_monotonic_increasing:
            subset = subset.sort_values("ist_time")
        return float(subset.iloc[-1]["price"])

    def get_minute_prices(
        self,
        df: pd.DataFrame,
        trade_date: date,
        expiry_date: date,
        opt_type: str,
        strike: float,
        start_ist: datetime,
        end_ist: datetime,
    ) -> pd.Series:
        """
        Last price per minute between start_ist and end_ist, forward-filled.

        Filters by expiry/strike/type and ist_time range only (not entry
        trade_date) so 1DTE overnight windows include next-day prints.
        """
        # Ensure date types match for fast comparison
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if isinstance(expiry_date, datetime):
            expiry_date = expiry_date.date()
        expiry_date = _as_date(expiry_date)

        opt = str(opt_type).lower().strip()
        strike_f = float(strike)

        needs_filter = len(df) > _LARGE_DF_ROWS or (
            "strike" in df.columns
            and (
                df["expiry_date"].nunique(dropna=True) > 1
                or df["opt_type"].nunique(dropna=True) > 1
                or df["strike"].nunique(dropna=True) > 1
            )
        )
        work = (
            self.filter_symbol(df, expiry_date, opt, strike_f)
            if needs_filter
            else df
        )

        subset = work[
            (work["ist_time"] >= start_ist) & (work["ist_time"] <= end_ist)
        ].copy()

        if subset.empty:
            return pd.Series(dtype=float)

        if not subset["ist_time"].is_monotonic_increasing:
            subset = subset.sort_values("ist_time")
        subset["minute"] = subset["ist_time"].dt.floor("min")
        by_min = subset.groupby("minute", sort=True)["price"].last()

        full_idx = pd.date_range(
            start=start_ist.replace(second=0, microsecond=0),
            end=end_ist.replace(second=0, microsecond=0),
            freq="1min",
        )
        series = by_min.reindex(full_idx).ffill()
        series.index.name = "ist_minute"
        series.name = "price"
        return series

    def get_minute_bid_ask(
        self,
        df: pd.DataFrame,
        trade_date: date,
        expiry_date: date,
        opt_type: str,
        strike: float,
        start_ist: datetime,
        end_ist: datetime,
    ) -> pd.DataFrame:
        """
        Reconstruct per-minute bid/ask from buyer_role trade prints.

        buyer_role == "taker" → buyer lifted the offer → price ≈ ASK
        buyer_role == "maker" → seller hit the bid     → price ≈ BID

        Returns DataFrame indexed by ist_minute with columns:
          bid, ask, mid, bid_age_s, ask_age_s
        Prices are last print in the minute, then forward-filled.
        Ages are seconds since the last print that set that side.
        """
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if isinstance(expiry_date, datetime):
            expiry_date = expiry_date.date()
        expiry_date = _as_date(expiry_date)

        opt = str(opt_type).lower().strip()
        strike_f = float(strike)

        needs_filter = len(df) > _LARGE_DF_ROWS or (
            "strike" in df.columns
            and (
                df["expiry_date"].nunique(dropna=True) > 1
                or df["opt_type"].nunique(dropna=True) > 1
                or df["strike"].nunique(dropna=True) > 1
            )
        )
        work = (
            self.filter_symbol(df, expiry_date, opt, strike_f)
            if needs_filter
            else df
        )

        subset = work[
            (work["ist_time"] >= start_ist) & (work["ist_time"] <= end_ist)
        ].copy()

        full_idx = pd.date_range(
            start=start_ist.replace(second=0, microsecond=0),
            end=end_ist.replace(second=0, microsecond=0),
            freq="1min",
        )
        empty = pd.DataFrame(
            index=full_idx,
            columns=["bid", "ask", "mid", "bid_age_s", "ask_age_s"],
            dtype=float,
        )
        empty.index.name = "ist_minute"
        if subset.empty or "buyer_role" not in subset.columns:
            return empty

        if not subset["ist_time"].is_monotonic_increasing:
            subset = subset.sort_values("ist_time")
        subset["minute"] = subset["ist_time"].dt.floor("min")
        role = subset["buyer_role"].astype(str).str.lower().str.strip()

        def _side_frame(mask: pd.Series) -> tuple[pd.Series, pd.Series]:
            side = subset.loc[mask]
            if side.empty:
                return (
                    pd.Series(dtype=float),
                    pd.Series(dtype="datetime64[ns]"),
                )
            g = side.groupby("minute", sort=True)
            px = g["price"].last()
            ts = g["ist_time"].last()
            return px, ts

        bid_px, bid_ts = _side_frame(role == "maker")
        ask_px, ask_ts = _side_frame(role == "taker")

        bid = bid_px.reindex(full_idx).ffill()
        ask = ask_px.reindex(full_idx).ffill()
        bid_last = bid_ts.reindex(full_idx).ffill()
        ask_last = ask_ts.reindex(full_idx).ffill()

        # Age = seconds from last print that set this side → current minute.
        # Same-minute prints can be after the floored minute stamp → clip at 0.
        minute_as_ts = pd.Series(full_idx, index=full_idx)
        bid_age = (minute_as_ts - bid_last).dt.total_seconds().clip(lower=0)
        ask_age = (minute_as_ts - ask_last).dt.total_seconds().clip(lower=0)
        bid_age = bid_age.where(bid.notna())
        ask_age = ask_age.where(ask.notna())

        out = pd.DataFrame(
            {
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2.0,
                "bid_age_s": bid_age,
                "ask_age_s": ask_age,
            },
            index=full_idx,
        )
        out.index.name = "ist_minute"
        return out

    def find_strike_by_premium(
        self,
        df: pd.DataFrame,
        trade_date: date,
        expiry_date: date,
        opt_type: str,
        target_premium: float,
        at_ist_datetime: datetime,
        exclude_strike: float | None = None,
        lookback_minutes: int = 15,
        prefer_above: bool = True,
    ) -> tuple[float, float]:
        """
        Strike whose price at at_ist_datetime is nearest to target_premium.

        Uses expiry_date + opt_type as primary filter (not trade_date) so
        1DTE contracts are found across both calendar days they trade.

        Returns (strike, actual_price).
        """
        # Ensure date types match for fast comparison
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        if isinstance(expiry_date, datetime):
            expiry_date = expiry_date.date()
        trade_date = _as_date(trade_date)
        expiry_date = _as_date(expiry_date)

        opt = str(opt_type).lower().strip()
        target = float(target_premium)

        # Primary filter: expiry_date only (not trade_date / ist_date)
        pool = df[(df["expiry_date"] == expiry_date) & (df["opt_type"] == opt)]
        if pool.empty:
            raise ValueError(
                f"No {opt} strikes for expiry={expiry_date}"
            )

        strikes = sorted(float(s) for s in pool["strike"].unique())
        excl = float(exclude_strike) if exclude_strike is not None else None

        candidates: list[tuple[float, float, float]] = []
        for strike in strikes:
            if excl is not None and abs(strike - excl) < 0.01:
                continue
            # Pass pre-filtered pool (usually << 100k) so time lookup is cheap
            price = self.get_price_at_time(
                pool,
                trade_date,
                expiry_date,
                opt,
                strike,
                at_ist_datetime,
                lookback_minutes=lookback_minutes,
            )
            if price is None:
                continue
            abs_diff = abs(price - target)
            above_key = 0 if (prefer_above and price >= target) else 1
            if not prefer_above:
                above_key = 0 if price <= target else 1
            candidates.append((abs_diff, above_key, strike, price))

        if not candidates:
            raise ValueError(
                f"No liquid {opt} strike found near premium ${target:.2f} "
                f"on {trade_date} expiry {expiry_date}"
            )

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        _, _, best_strike, best_price = candidates[0]
        return float(best_strike), float(best_price)


if __name__ == "__main__":
    import sys

    # Test with June CSV
    csv_path = (
        sys.argv[1] if len(sys.argv) > 1 else "backtest/data/BTC_2026-06.csv"
    )

    print(f"Loading {csv_path}...")
    loader = DataLoader()
    df = loader.load_csv(csv_path)

    print(f"\nDataFrame shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
    print("\nSample rows:")
    print(df.head(3).to_string())

    print(f'\nUnique trade dates: {sorted(df["ist_date"].unique())[:5]}')
    print(f'Unique expiry dates: {sorted(df["expiry_date"].unique())[:10]}')

    # Test ATM detection on June 1 with 1DTE expiry (expires June 2)
    from datetime import date, datetime

    try:
        atm = loader.get_atm_strike(
            df,
            trade_date=date(2026, 6, 1),
            expiry_date=date(2026, 6, 2),
            entry_ist_hour=10,
            entry_minute_ist=0,
        )
        print(f"\nATM strike on June 1 (1DTE, entry 10AM IST): {atm}")

        # Test get_price_at_time
        entry_ist = datetime(2026, 6, 1, 10, 0, 0)
        call_price = loader.get_price_at_time(
            df, date(2026, 6, 1), date(2026, 6, 2), "call", atm, entry_ist
        )
        put_price = loader.get_price_at_time(
            df, date(2026, 6, 1), date(2026, 6, 2), "put", atm, entry_ist
        )
        print(f"Call @ {atm}: ${call_price}")
        print(f"Put  @ {atm}: ${put_price}")
        print(f"Straddle premium: ${(call_price or 0) + (put_price or 0)}")

        # Test minute prices
        start = datetime(2026, 6, 1, 10, 0)
        end = datetime(2026, 6, 1, 12, 0)
        call_series = loader.get_minute_prices(
            df, date(2026, 6, 1), date(2026, 6, 2), "call", atm, start, end
        )
        print("\nCall minute prices (10AM-12PM, first 10):")
        print(call_series.head(10))

    except ValueError as e:
        print(f"Error: {e}")
