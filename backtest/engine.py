# engine.py — Orchestrate multi-day backtests from a local CSV directory

from __future__ import annotations

import glob
import gc
import os
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

try:
    from backtest.data_loader import (
        DataLoader,
        _frame_needs_slim,
        slim_trade_frame,
    )
    from backtest.strategy_sim import DayResult, StrategySimulator
except ImportError:
    from data_loader import DataLoader, _frame_needs_slim, slim_trade_frame
    from strategy_sim import DayResult, StrategySimulator

SIM_KEYS = (
    "trade_type",
    "expiry_type",
    "quantity",
    "trigger_pct",
    "profit_target_pct",
    "stoploss_pct",
    "min_replacement_premium",
    "conversion_equality_pct",
    "fee_per_leg_usd",
    "slippage_pct",
    "target_premium_per_side",
    "entry_hour_ist",
    "entry_minute_ist",
)

# Delta India bulk download name patterns (optional " (1)" from browser re-downloads)
_MONTHLY_RE = re.compile(
    r"^options-trades-monthly-BTC-(?P<y>\d{4})-(?P<m>\d{2})"
    r"\.csv(?: \(\d+\))?(?:\.zip)?$",
    re.IGNORECASE,
)
_DAILY_RE = re.compile(
    r"^options-trades-daily-BTC-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"\.csv(?: \(\d+\))?(?:\.zip)?$",
    re.IGNORECASE,
)
_LEGACY_MONTH_RE = re.compile(
    r"^BTC_(?P<y>\d{4})-(?P<m>\d{2})\.csv$",
    re.IGNORECASE,
)


def _parse_source_meta(path: Path) -> dict[str, Any] | None:
    """Classify a file as monthly / daily / legacy BTC month CSV."""
    name = path.name
    m = _MONTHLY_RE.match(name)
    if m:
        return {
            "kind": "monthly",
            "year": int(m.group("y")),
            "month": int(m.group("m")),
            "path": path,
        }
    m = _DAILY_RE.match(name)
    if m:
        return {
            "kind": "daily",
            "year": int(m.group("y")),
            "month": int(m.group("m")),
            "day": int(m.group("d")),
            "path": path,
        }
    m = _LEGACY_MONTH_RE.match(name)
    if m:
        return {
            "kind": "legacy",
            "year": int(m.group("y")),
            "month": int(m.group("m")),
            "path": path,
        }
    return None


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _prefer_cleaner_name(paths: list[Path]) -> Path:
    """Prefer exact .csv.zip / .csv over browser ' (1)' duplicates."""
    def score(p: Path) -> tuple[int, str]:
        n = p.name
        penalty = 1 if " (" in n else 0
        return (penalty, n)

    return sorted(paths, key=score)[0]


def _month_start_end(year: int, month: int) -> tuple[date, date]:
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _month_overlaps_range(
    year: int,
    month: int,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    start, end = _month_start_end(year, month)
    if date_from is not None and end < date_from:
        return False
    if date_to is not None and start > date_to:
        return False
    return True


def _parse_config_date(raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    return pd.Timestamp(raw).date()


def _next_month_key(month_key: str) -> str:
    y, m = month_key.split("-")
    year, month = int(y), int(m)
    if month == 12:
        return _month_key(year + 1, 1)
    return _month_key(year, month + 1)


def day_result_to_dict(result: DayResult) -> dict[str, Any]:
    """Serialize a DayResult for JSON / HTML reports."""
    exit_time = None
    if result.exit_ist is not None:
        exit_time = result.exit_ist.strftime("%H:%M")

    adj_log: list[dict[str, Any]] = []
    for adj in result.adjustment_log or []:
        if not isinstance(adj, dict):
            continue
        row = dict(adj)
        minute = row.get("minute")
        if isinstance(minute, datetime):
            row["minute"] = minute.isoformat(sep=" ", timespec="minutes")
            row["time"] = minute.strftime("%H:%M")
        elif minute is not None:
            s = str(minute)
            row["minute"] = s
            row["time"] = s[11:16] if len(s) >= 16 else s
        adj_log.append(row)

    return {
        "trade_date": result.trade_date.isoformat()
        if hasattr(result.trade_date, "isoformat")
        else str(result.trade_date),
        "expiry_date": result.expiry_date.isoformat()
        if hasattr(result.expiry_date, "isoformat")
        else str(result.expiry_date),
        "trade_type": result.trade_type,
        "call_strike": result.call_strike,
        "put_strike": result.put_strike,
        "entry_call_premium": result.entry_call_premium,
        "entry_put_premium": result.entry_put_premium,
        "initial_premium": result.initial_premium,
        "profit_target_usd": result.profit_target_usd,
        "stoploss_usd": result.stoploss_usd,
        "exit_reason": result.exit_reason,
        "exit_time": exit_time,
        "exit_ist": (
            result.exit_ist.isoformat(sep=" ", timespec="minutes")
            if result.exit_ist
            else None
        ),
        "net_pnl": round(float(result.net_pnl), 4),
        "gross_pnl": round(float(result.gross_pnl), 4),
        "total_fees": round(float(result.total_fees), 4),
        "adjustments": int(result.total_adjustments),
        "conversions": int(result.total_conversions),
        "reversals": int(result.total_reversals),
        "minutes_in_conversion": int(result.minutes_in_conversion),
        "max_drawdown": round(float(result.max_drawdown), 4),
        "data_ok": bool(result.data_ok),
        "notes": result.notes or "",
        "adj_log": adj_log,
    }


def summary_for_api(summary: dict[str, Any]) -> dict[str, Any]:
    """Map engine summary keys to the frontend/API shape."""
    exit_counts = summary.get("exit_counts") or {}
    return {
        "total_days": summary.get("total_days", 0),
        "data_ok_days": summary.get("data_ok_days", 0),
        "win_days": summary.get("win_days", 0),
        "loss_days": summary.get("loss_days", 0),
        "win_rate": round(float(summary.get("win_rate") or 0), 2),
        "avg_win": round(float(summary.get("avg_win") or 0), 2),
        "avg_loss": round(float(summary.get("avg_loss") or 0), 2),
        "total_net_pnl": round(float(summary.get("total_net_pnl") or 0), 2),
        "max_single_win": round(float(summary.get("max_single_win") or 0), 2),
        "max_single_loss": round(float(summary.get("max_single_loss") or 0), 2),
        "max_drawdown": round(float(summary.get("max_drawdown") or 0), 2),
        "total_fees": round(float(summary.get("total_fees") or 0), 2),
        "total_adjustments": int(summary.get("total_adjustments") or 0),
        "total_conversions": int(summary.get("total_conversions") or 0),
        "total_reversals": int(summary.get("total_reversals") or 0),
        "profit_target_count": int(exit_counts.get("PROFIT_TARGET") or 0),
        "stoploss_count": int(exit_counts.get("STOPLOSS") or 0),
        "pre_expiry_count": int(exit_counts.get("PRE_EXPIRY") or 0),
        "exit_counts": exit_counts,
        "cumulative_pnl": summary.get("cumulative_pnl") or [],
        "daily_pnl": summary.get("daily_pnl") or [],
        "daily_dates": summary.get("daily_dates") or [],
    }


class BacktestEngine:
    """Load local CSV directory and run StrategySimulator across all days."""

    def __init__(self, config: dict) -> None:
        self.config = dict(config or {})
        self.loader = DataLoader()
        self.cache_refresh = bool(self.config.get("cache_refresh", False))
        sim_kwargs = {k: self.config[k] for k in SIM_KEYS if k in self.config}
        self.sim = StrategySimulator(**sim_kwargs)

    def _cache_path(self, cache_dir: Path, source: Path) -> Path:
        # Keep original filename visible; parquet replaces final extension(s)
        safe = source.name
        if safe.lower().endswith(".csv.zip"):
            safe = safe[: -len(".csv.zip")] + ".parquet"
        elif safe.lower().endswith(".zip"):
            safe = safe[: -len(".zip")] + ".parquet"
        elif safe.lower().endswith(".csv"):
            safe = safe[: -len(".csv")] + ".parquet"
        else:
            safe = safe + ".parquet"
        # Browser duplicate names: "file.csv (1).zip" → already handled above
        return cache_dir / safe

    def _load_source_cached(self, path: Path, cache_dir: Path) -> pd.DataFrame:
        """Load+enrich a source file, using parquet cache when fresh."""
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_path(cache_dir, path)
        src_mtime = path.stat().st_mtime

        from_cache = False
        if (
            not self.cache_refresh
            and cache_path.exists()
            and cache_path.stat().st_mtime >= src_mtime
        ):
            print(f"Cache hit: {cache_path.name}")
            df = pd.read_parquet(cache_path)
            print(f"Loaded {len(df)} rows from cache ({path.name})")
            from_cache = True
        else:
            print(f"Loading {path.name}...")
            df = self.loader.load_csv(path)
            # load_csv already slimmed + logged

        if _frame_needs_slim(df):
            # Old fat parquet (or unslimmed parse) — slim and rewrite cache
            print(f"Slimming frame for {path.name}...")
            df = slim_trade_frame(df, log=True)
            try:
                df.to_parquet(cache_path, index=False)
                print(f"Cache wrote (slim): {cache_path.name} ({len(df)} rows)")
            except Exception as exc:
                print(f"Cache write failed for {cache_path.name}: {exc}")
        else:
            # Already without fat columns — still downcast dtypes in memory
            # (parquet may widen float32→float64) without rewriting every time
            df = slim_trade_frame(df, log=from_cache)
            if not from_cache:
                try:
                    df.to_parquet(cache_path, index=False)
                    print(f"Cache wrote: {cache_path.name} ({len(df)} rows)")
                except Exception as exc:
                    print(f"Cache write failed for {cache_path.name}: {exc}")
        return df

    def _config_date_range(self) -> tuple[date | None, date | None]:
        return (
            _parse_config_date(self.config.get("date_from")),
            _parse_config_date(self.config.get("date_to")),
        )

    def _discover_sources(
        self, data_dir: str
    ) -> tuple[
        dict[str, list[Path]],
        list[tuple[date, Path]],
        list[tuple[str, Path]],
        Path,
        Path,
    ]:
        data_path = Path(data_dir)
        raw_path = data_path.parent / "data_raw"
        cache_dir = data_path.parent / "cache"

        monthly_by_month: dict[str, list[Path]] = {}
        daily_files: list[tuple[date, Path]] = []
        legacy_files: list[tuple[str, Path]] = []

        if raw_path.is_dir():
            for p in sorted(raw_path.iterdir()):
                if not p.is_file():
                    continue
                meta = _parse_source_meta(p)
                if meta is None:
                    continue
                if meta["kind"] == "monthly":
                    key = _month_key(meta["year"], meta["month"])
                    monthly_by_month.setdefault(key, []).append(p)
                elif meta["kind"] == "daily":
                    d = date(meta["year"], meta["month"], meta["day"])
                    daily_files.append((d, p))

        for f in sorted(glob.glob(os.path.join(str(data_path), "BTC_*.csv"))):
            p = Path(f)
            meta = _parse_source_meta(p)
            if meta and meta["kind"] == "legacy":
                legacy_files.append((_month_key(meta["year"], meta["month"]), p))

        return monthly_by_month, daily_files, legacy_files, raw_path, cache_dir

    def load_data_dir(self, data_dir: str) -> pd.DataFrame:
        """
        Load trade data from backtest/data (BTC_*.csv) and backtest/data_raw
        (Delta monthly/daily zip or csv), with calendar-date dedupe.

        If config has date_from/date_to, months/days outside that range are
        skipped BEFORE file load (FIX: avoid 41M-row OOM on S002 in-sample).

        Priority for any calendar date:
          1. monthly data_raw
          2. daily data_raw (only dates not in any monthly)
          3. legacy backtest/data/BTC_YYYY-MM.csv (months not covered by data_raw)
        """
        date_from, date_to = self._config_date_range()
        if date_from or date_to:
            print(
                f"Date filter (pre-load): "
                f"{date_from or '...'} .. {date_to or '...'}"
            )

        (
            monthly_by_month,
            daily_files,
            legacy_files,
            raw_path,
            cache_dir,
        ) = self._discover_sources(data_dir)

        if not monthly_by_month and not daily_files and not legacy_files:
            raise FileNotFoundError(
                f"No BTC_*.csv in {data_dir} and no Delta zip/csv in {raw_path}"
            )

        covered_dates: set[date] = set()
        months_covered_by_raw: set[str] = set()
        dfs: list[pd.DataFrame] = []
        files_loaded = 0
        files_skipped = 0

        # --- 1) Monthly (highest priority) ---
        for month_key in sorted(monthly_by_month.keys()):
            y, m = month_key.split("-")
            year, month = int(y), int(m)
            if not _month_overlaps_range(year, month, date_from, date_to):
                print(
                    f"SKIP monthly outside date range: "
                    f"{monthly_by_month[month_key][0].name} "
                    f"month={month_key}"
                )
                files_skipped += 1
                continue

            candidates = monthly_by_month[month_key]
            chosen = _prefer_cleaner_name(candidates)
            for dup in candidates:
                if dup != chosen:
                    print(
                        f"SKIP duplicate monthly: {dup.name} "
                        f"(using {chosen.name})"
                    )
                    files_skipped += 1
            df = self._load_source_cached(chosen, cache_dir)
            files_loaded += 1
            months_covered_by_raw.add(month_key)
            for d in df["ist_date"].dropna().unique():
                covered_dates.add(
                    d if isinstance(d, date) else pd.Timestamp(d).date()
                )
            dfs.append(df)

        # --- 2) Daily ---
        daily_by_day: dict[date, list[Path]] = {}
        for d, p in daily_files:
            daily_by_day.setdefault(d, []).append(p)

        for day_key in sorted(daily_by_day.keys()):
            if date_from is not None and day_key < date_from:
                print(
                    f"SKIP daily outside date range: "
                    f"{daily_by_day[day_key][0].name} date={day_key}"
                )
                files_skipped += 1
                continue
            if date_to is not None and day_key > date_to:
                print(
                    f"SKIP daily outside date range: "
                    f"{daily_by_day[day_key][0].name} date={day_key}"
                )
                files_skipped += 1
                continue

            candidates = daily_by_day[day_key]
            chosen = _prefer_cleaner_name(candidates)
            for dup in candidates:
                if dup != chosen:
                    print(
                        f"SKIP duplicate daily: {dup.name} "
                        f"(using {chosen.name})"
                    )
                    files_skipped += 1

            if day_key in covered_dates:
                print(
                    f"SKIP daily (date covered by monthly): {chosen.name} "
                    f"date={day_key}"
                )
                files_skipped += 1
                continue

            df = self._load_source_cached(chosen, cache_dir)
            before = len(df)
            mask_dates = df["ist_date"].map(
                lambda x: (
                    x if isinstance(x, date) else pd.Timestamp(x).date()
                )
            )
            keep = ~mask_dates.isin(covered_dates)
            df = df.loc[keep].copy()
            dropped = before - len(df)
            if dropped:
                print(
                    f"SKIP {dropped} daily rows already covered "
                    f"({chosen.name})"
                )
            if df.empty:
                print(f"SKIP daily (all rows covered): {chosen.name}")
                files_skipped += 1
                continue

            files_loaded += 1
            months_covered_by_raw.add(_month_key(day_key.year, day_key.month))
            for d in df["ist_date"].dropna().unique():
                covered_dates.add(
                    d if isinstance(d, date) else pd.Timestamp(d).date()
                )
            dfs.append(df)

        # --- 3) Legacy BTC_*.csv ---
        for month_key, path in legacy_files:
            y, m = month_key.split("-")
            year, month = int(y), int(m)
            if not _month_overlaps_range(year, month, date_from, date_to):
                print(
                    f"SKIP legacy outside date range: {path.name} "
                    f"month={month_key}"
                )
                files_skipped += 1
                continue
            if month_key in months_covered_by_raw:
                print(
                    f"SKIP legacy CSV (month covered by data_raw): "
                    f"{path.name} month={month_key}"
                )
                files_skipped += 1
                continue

            df = self._load_source_cached(path, cache_dir)
            before = len(df)
            mask_dates = df["ist_date"].map(
                lambda x: (
                    x if isinstance(x, date) else pd.Timestamp(x).date()
                )
            )
            keep = ~mask_dates.isin(covered_dates)
            df = df.loc[keep].copy()
            dropped = before - len(df)
            if dropped:
                print(
                    f"SKIP {dropped} legacy rows already covered "
                    f"({path.name})"
                )
            if df.empty:
                print(f"SKIP legacy (all rows covered): {path.name}")
                files_skipped += 1
                continue

            files_loaded += 1
            for d in df["ist_date"].dropna().unique():
                covered_dates.add(
                    d if isinstance(d, date) else pd.Timestamp(d).date()
                )
            dfs.append(df)

        if not dfs:
            raise FileNotFoundError(
                f"All sources skipped or empty under {data_dir} / {raw_path}"
            )

        combined = pd.concat(dfs, ignore_index=True)

        print("Sorting and indexing data for fast lookup...")
        combined["expiry_date"] = pd.Categorical(combined["expiry_date"])
        combined["ist_date"] = pd.Categorical(combined["ist_date"])
        combined["opt_type"] = pd.Categorical(combined["opt_type"])
        combined = combined.sort_values(
            ["expiry_date", "opt_type", "strike", "ist_time"]
        ).reset_index(drop=True)

        ist_dates = [
            d if isinstance(d, date) else pd.Timestamp(d).date()
            for d in combined["ist_date"].dropna().unique()
        ]
        oldest = min(ist_dates) if ist_dates else None
        newest = max(ist_dates) if ist_dates else None

        print(
            f"Load summary: files_loaded={files_loaded} "
            f"files_skipped={files_skipped} total_rows={len(combined):,} "
            f"ist_date_range={oldest} -> {newest}"
        )
        return combined

    def load_month_keys(
        self,
        data_dir: str,
        month_keys: list[str],
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> pd.DataFrame:
        """
        Load only the given calendar months (for S002 month-streaming).

        Applies the same monthly > daily > legacy priority within the set.
        """
        want = set(month_keys)
        (
            monthly_by_month,
            daily_files,
            legacy_files,
            raw_path,
            cache_dir,
        ) = self._discover_sources(data_dir)

        covered_dates: set[date] = set()
        months_covered_by_raw: set[str] = set()
        dfs: list[pd.DataFrame] = []

        for month_key in sorted(want):
            if month_key in monthly_by_month:
                chosen = _prefer_cleaner_name(monthly_by_month[month_key])
                df = self._load_source_cached(chosen, cache_dir)
                months_covered_by_raw.add(month_key)
                for d in df["ist_date"].dropna().unique():
                    covered_dates.add(
                        d if isinstance(d, date) else pd.Timestamp(d).date()
                    )
                dfs.append(df)

        # Daily files belonging to wanted months
        daily_by_day: dict[date, list[Path]] = {}
        for d, p in daily_files:
            mk = _month_key(d.year, d.month)
            if mk not in want:
                continue
            if date_from is not None and d < date_from:
                continue
            if date_to is not None and d > date_to:
                continue
            daily_by_day.setdefault(d, []).append(p)

        for day_key in sorted(daily_by_day.keys()):
            if day_key in covered_dates:
                continue
            chosen = _prefer_cleaner_name(daily_by_day[day_key])
            df = self._load_source_cached(chosen, cache_dir)
            mask_dates = df["ist_date"].map(
                lambda x: x if isinstance(x, date) else pd.Timestamp(x).date()
            )
            df = df.loc[~mask_dates.isin(covered_dates)].copy()
            if df.empty:
                continue
            months_covered_by_raw.add(_month_key(day_key.year, day_key.month))
            for d in df["ist_date"].dropna().unique():
                covered_dates.add(
                    d if isinstance(d, date) else pd.Timestamp(d).date()
                )
            dfs.append(df)

        for month_key, path in legacy_files:
            if month_key not in want:
                continue
            if month_key in months_covered_by_raw:
                continue
            df = self._load_source_cached(path, cache_dir)
            mask_dates = df["ist_date"].map(
                lambda x: x if isinstance(x, date) else pd.Timestamp(x).date()
            )
            df = df.loc[~mask_dates.isin(covered_dates)].copy()
            if df.empty:
                continue
            for d in df["ist_date"].dropna().unique():
                covered_dates.add(
                    d if isinstance(d, date) else pd.Timestamp(d).date()
                )
            dfs.append(df)

        if not dfs:
            return pd.DataFrame()

        combined = pd.concat(dfs, ignore_index=True)
        combined["expiry_date"] = pd.Categorical(combined["expiry_date"])
        combined["ist_date"] = pd.Categorical(combined["ist_date"])
        combined["opt_type"] = pd.Categorical(combined["opt_type"])
        combined = combined.sort_values(
            ["expiry_date", "opt_type", "strike", "ist_time"]
        ).reset_index(drop=True)
        return combined

    def _list_available_month_keys(self, data_dir: str) -> list[str]:
        monthly, daily, legacy, _, _ = self._discover_sources(data_dir)
        keys: set[str] = set(monthly.keys())
        for d, _p in daily:
            keys.add(_month_key(d.year, d.month))
        for mk, _p in legacy:
            keys.add(mk)
        return sorted(keys)

    def _s002_is_0dte(self) -> bool:
        """
        S002 simulator is hard-coded 0DTE (expiry == trade_date).

        If expiry_selection / expiry_type ever becomes 1DTE/N-DTE, a trade can
        cross a month boundary — then we must load current + next month.
        """
        raw = (
            self.config.get("expiry_selection")
            or self.config.get("expiry_type")
            or "0DTE"
        )
        return str(raw).strip().upper() in {"0DTE", "0", "ZERO"}

    def run(
        self,
        data_dir: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[list[DayResult], dict[str, Any]]:
        """
        Run backtest on all data.

        progress_callback: optional function(current, total, date_str)
        Returns: (day_results_list, summary_dict)
        """
        df = self.load_data_dir(data_dir)
        trade_dates = sorted(df["ist_date"].unique())
        results: list[DayResult] = []

        for i, trade_date in enumerate(trade_dates):
            date_str = str(trade_date)
            if progress_callback:
                progress_callback(i + 1, len(trade_dates), date_str)

            try:
                if not isinstance(trade_date, date):
                    # pandas may yield datetime.date already; coerce otherwise
                    trade_date = pd.Timestamp(trade_date).date()

                result = self.sim.simulate_day(df, trade_date)
                results.append(result)
                status = "✅" if result.data_ok else "⚠️"
                pnl_str = (
                    f"${result.net_pnl:+.2f}" if result.data_ok else "NO DATA"
                )
                print(
                    f"{status} {trade_date} | {result.exit_reason:15s} | "
                    f"P&L: {pnl_str:10s} | "
                    f"Adj: {result.total_adjustments} "
                    f"Conv: {result.total_conversions}"
                )
            except Exception as e:
                print(f"❌ {trade_date} ERROR: {e}")

        summary = self.compute_summary(results)
        return results, summary

    def run_s002(
        self,
        data_dir: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> tuple[list[Any], dict[str, Any]]:
        """
        S002 0DTE long-strangle mode — month-by-month streaming.

        Does NOT concat all months into one frame (that OOM'd at 41M rows).
        Does not alter run() / run_continuous().

        0DTE: each trade lives inside one calendar day → one month of data
        is enough. If expiry were 1DTE/N-DTE, we would also load the next
        month so month-boundary trades still see the following session.
        """
        try:
            from backtest.s002_sim import (
                S002Simulator,
                compute_s002_summary,
            )
        except ImportError:
            from s002_sim import S002Simulator, compute_s002_summary

        sim = S002Simulator(self.config)
        date_from, date_to = self._config_date_range()
        is_0dte = self._s002_is_0dte()
        print(
            f"S002 stream mode: expiry={'0DTE' if is_0dte else 'N-DTE'} | "
            f"date_from={date_from} date_to={date_to}"
        )
        if is_0dte:
            print(
                "0DTE: loading one calendar month at a time "
                "(no cross-month look-ahead)."
            )
        else:
            print(
                "Non-0DTE: loading current month + next month so trades "
                "near month end can see the following session."
            )

        available = self._list_available_month_keys(data_dir)
        month_keys = [
            mk
            for mk in available
            if _month_overlaps_range(
                int(mk[:4]), int(mk[5:7]), date_from, date_to
            )
        ]
        if not month_keys:
            raise FileNotFoundError(
                f"No months in range {date_from}..{date_to} under {data_dir}"
            )

        # Pre-count days for progress (approximate: calendar days in range)
        results: list[Any] = []
        day_index = 0
        total_guess = 0
        for mk in month_keys:
            y, m = int(mk[:4]), int(mk[5:7])
            start, end = _month_start_end(y, m)
            if date_from and start < date_from:
                start = date_from
            if date_to and end > date_to:
                end = date_to
            total_guess += max(0, (end - start).days + 1)

        for mi, mk in enumerate(month_keys):
            load_keys = [mk]
            # N-DTE only: also pull the next calendar month if it exists on disk
            if not is_0dte:
                nxt = _next_month_key(mk)
                if nxt in available:
                    load_keys.append(nxt)
                    print(
                        f"Month {mk}: loading {mk} + {nxt} (N-DTE look-ahead)"
                    )
                else:
                    print(
                        f"Month {mk}: loading {mk} only "
                        f"(next month {nxt} not on disk)"
                    )
            else:
                print(f"Month {mk}: loading {mk} only (0DTE)")

            df = self.load_month_keys(
                data_dir,
                load_keys,
                date_from=date_from,
                date_to=date_to,
            )
            if df.empty:
                print(f"Month {mk}: empty frame — skip")
                continue

            y, m = int(mk[:4]), int(mk[5:7])
            raw_dates = sorted(df["ist_date"].unique())
            month_days: list[date] = []
            for d in raw_dates:
                dd = d if isinstance(d, date) and not isinstance(d, datetime) else pd.Timestamp(d).date()
                # Only simulate days that belong to THIS month (avoid double
                # processing when N-DTE also loaded next month).
                if dd.year != y or dd.month != m:
                    continue
                if date_from is not None and dd < date_from:
                    continue
                if date_to is not None and dd > date_to:
                    continue
                month_days.append(dd)

            mem_mb = df.memory_usage(deep=True).sum() / (1024 ** 2)
            print(
                f"Month {mk}: {len(df):,} rows | {len(month_days)} days | "
                f"{mem_mb:.1f} MiB"
            )

            for trade_date in month_days:
                day_index += 1
                if progress_callback:
                    progress_callback(day_index, total_guess, str(trade_date))
                try:
                    day = sim.simulate_day(df, trade_date)
                    results.append(day)
                    if day.trades:
                        n = len(day.trades)
                        pnl = sum(t.net_pnl for t in day.trades)
                        pnl_zc = sum(t.net_pnl_zc for t in day.trades)
                        print(
                            f"OK {trade_date} | trades={n} | "
                            f"real=${pnl:+.2f} | zc=${pnl_zc:+.2f} | "
                            f"min_diff={day.min_diff_seen}"
                        )
                    else:
                        print(
                            f"-- {trade_date} | no entry | "
                            f"reason={day.no_entry_reason} | "
                            f"min_diff={day.min_diff_seen} "
                            f"(C={day.min_diff_call_prem}/P={day.min_diff_put_prem}) | "
                            f"min_max_prem={day.min_max_premium_seen}"
                        )
                except Exception as exc:
                    print(f"ERR {trade_date} ERROR: {exc}")

            del df
            gc.collect()
            print(f"Month {mk}: freed ({mi + 1}/{len(month_keys)})")

        summary = compute_s002_summary(results)
        return results, summary

    def run_s002_debug_day(
        self,
        data_dir: str,
        debug_day: str,
    ) -> tuple[Any, Path]:
        """
        Load only the debug day's month, dump scan-window CSV, simulate that day.
        """
        try:
            from backtest.s002_sim import S002Simulator, s002_trade_to_dict
        except ImportError:
            from s002_sim import S002Simulator, s002_trade_to_dict

        trade_date = pd.Timestamp(debug_day).date()
        mk = _month_key(trade_date.year, trade_date.month)
        load_keys = [mk]
        if not self._s002_is_0dte():
            nxt = _next_month_key(mk)
            if nxt in self._list_available_month_keys(data_dir):
                load_keys.append(nxt)

        df = self.load_month_keys(
            data_dir,
            load_keys,
            date_from=trade_date,
            date_to=trade_date,
        )
        sim = S002Simulator(self.config)
        out_csv = Path("backtest/results") / f"debug_{trade_date.isoformat()}.csv"
        sim.write_debug_day_csv(df, trade_date, out_csv)
        day = sim.simulate_day(df, trade_date)

        print(f"\n=== DEBUG DAY {trade_date} ===")
        print(
            f"no_entry_reason={day.no_entry_reason} | "
            f"min_diff={day.min_diff_seen} "
            f"(C={day.min_diff_call_prem}/P={day.min_diff_put_prem}) | "
            f"min_max_prem={day.min_max_premium_seen} "
            f"(scan window only)"
        )
        if not day.trades:
            print("No entries on this day.")
        for t in day.trades:
            d = s002_trade_to_dict(t)
            print(
                f"ENTRY {d['entry_ist']} | strikes C={d['call_strike']} "
                f"P={d['put_strike']} | ask {d['call_ask']}/{d['put_ask']} | "
                f"lots={d['lots']} | capital_used={d['capital_used']} | "
                f"entry_fees={d['entry_fees']}"
            )
            print(
                f"  EXIT {d['exit_ist']} | {d['exit_reason']} | "
                f"gross={d['gross_pnl']} | net={d['net_pnl']} | "
                f"zc_net={d['net_pnl_zc']}"
            )
        print(f"CSV: {out_csv.resolve()}")
        del df
        gc.collect()
        return day, out_csv

    def run_continuous(
        self,
        data_dir: str,
        progress_callback: Optional[Callable[[int, int | None, str], None]] = None,
    ) -> tuple[list[DayResult], dict[str, Any]]:
        """
        Continuous basket simulation:
        First entry: first available day at configured evening time (default 17:32).
        Each subsequent entry: 2 minutes after previous basket exits.
        """
        try:
            from backtest.basket_sim import BasketSimulator
        except ImportError:
            from basket_sim import BasketSimulator

        df = self.load_data_dir(data_dir)

        basket_sim = BasketSimulator(
            trade_type=self.config.get("trade_type", "strangle"),
            target_premium_per_side=self.config.get(
                "target_premium_per_side", 250.0
            ),
            entry_hour_ist=int(self.config.get("entry_hour_ist", 17)),
            entry_minute_ist=int(self.config.get("entry_minute_ist", 32)),
            trigger_pct=float(self.config.get("trigger_pct", 160.0)),
            profit_target_pct=float(self.config.get("profit_target_pct", 25.0)),
            stoploss_pct=float(self.config.get("stoploss_pct", 50.0)),
            min_replacement_premium=float(
                self.config.get("min_replacement_premium", 150.0)
            ),
            conversion_equality_pct=float(
                self.config.get("conversion_equality_pct", 10.0)
            ),
            quantity=int(self.config.get("quantity", 100)),
            fee_per_leg_usd=float(self.config.get("fee_per_leg_usd", 0.75)),
            slippage_pct=float(self.config.get("slippage_pct", 2.0)),
        )

        raw_dates = sorted(df["ist_date"].unique())
        all_dates: list[date] = []
        for d in raw_dates:
            if isinstance(d, date) and not isinstance(d, datetime):
                all_dates.append(d)
            else:
                all_dates.append(pd.Timestamp(d).date())

        if not all_dates:
            return [], self.compute_summary([])

        results: list[DayResult] = []
        basket_num = 1
        current_entry_day = all_dates[0]
        current_entry_time: Optional[datetime] = None  # None → default 17:32

        while current_entry_day <= all_dates[-1]:
            if progress_callback:
                progress_callback(basket_num, None, str(current_entry_day))

            result = basket_sim.simulate_basket(
                df,
                current_entry_day,
                basket_num,
                entry_ist_override=current_entry_time,
            )
            results.append(result)

            if not result.data_ok or result.exit_ist is None:
                # No data — try next calendar day present in the dataset
                try:
                    idx = all_dates.index(current_entry_day)
                except ValueError:
                    later = [d for d in all_dates if d > current_entry_day]
                    if not later:
                        break
                    current_entry_day = later[0]
                    current_entry_time = None
                    basket_num += 1
                    continue

                if idx + 1 < len(all_dates):
                    current_entry_day = all_dates[idx + 1]
                    current_entry_time = None
                    basket_num += 1
                    continue
                break

            # Next entry = exit_time + 2 minutes
            next_entry_ist = result.exit_ist + timedelta(minutes=2)
            next_entry_day = next_entry_ist.date()

            available_after = [d for d in all_dates if d >= next_entry_day]
            if not available_after:
                break

            current_entry_day = available_after[0]
            if current_entry_day == next_entry_day:
                current_entry_time = next_entry_ist
            else:
                # Skipped ahead (weekend / missing days) → default evening entry
                current_entry_time = None
            basket_num += 1

        summary = self.compute_summary(results)
        return results, summary

    def compute_summary(self, results: list[DayResult]) -> dict[str, Any]:
        ok = [r for r in results if r.data_ok]
        if not ok:
            return {
                "error": "No valid results",
                "total_days": len(results),
                "data_ok_days": 0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "total_net_pnl": 0.0,
                "max_drawdown": 0.0,
                "total_adjustments": 0,
                "total_conversions": 0,
                "total_reversals": 0,
                "total_fees": 0.0,
                "exit_counts": {},
                "cumulative_pnl": [],
                "daily_pnl": [],
                "daily_dates": [],
                "win_days": 0,
                "loss_days": 0,
                "max_single_win": 0.0,
                "max_single_loss": 0.0,
            }

        pnls = [r.net_pnl for r in ok]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        cumulative: list[float] = []
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            running += p
            cumulative.append(running)
            if running > peak:
                peak = running
            dd = running - peak
            if dd < max_dd:
                max_dd = dd

        exit_counts: dict[str, int] = {}
        for r in ok:
            exit_counts[r.exit_reason] = exit_counts.get(r.exit_reason, 0) + 1

        return {
            "total_days": len(results),
            "data_ok_days": len(ok),
            "win_days": len(wins),
            "loss_days": len(losses),
            "win_rate": (len(wins) / len(ok) * 100.0) if ok else 0.0,
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "total_net_pnl": sum(pnls),
            "max_single_win": max(pnls) if pnls else 0.0,
            "max_single_loss": min(pnls) if pnls else 0.0,
            "max_drawdown": max_dd,
            "total_fees": sum(r.total_fees for r in ok),
            "total_adjustments": sum(r.total_adjustments for r in ok),
            "total_conversions": sum(r.total_conversions for r in ok),
            "total_reversals": sum(r.total_reversals for r in ok),
            "exit_counts": exit_counts,
            "cumulative_pnl": cumulative,
            "daily_pnl": pnls,
            "daily_dates": [str(r.trade_date) for r in ok],
        }
