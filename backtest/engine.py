# engine.py — Orchestrate multi-day backtests from a local CSV directory

from __future__ import annotations

import glob
import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

try:
    from backtest.data_loader import DataLoader
    from backtest.strategy_sim import DayResult, StrategySimulator
except ImportError:
    from data_loader import DataLoader
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
        sim_kwargs = {k: self.config[k] for k in SIM_KEYS if k in self.config}
        self.sim = StrategySimulator(**sim_kwargs)

    def load_data_dir(self, data_dir: str) -> pd.DataFrame:
        """Load all BTC_*.csv files from directory into one DataFrame."""
        pattern = os.path.join(data_dir, "BTC_*.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(f"No BTC_*.csv files found in {data_dir}")

        print(
            f"Found {len(files)} CSV files: "
            f"{[os.path.basename(f) for f in files]}"
        )

        dfs: list[pd.DataFrame] = []
        for f in files:
            print(f"Loading {os.path.basename(f)}...")
            dfs.append(self.loader.load_csv(f))

        combined = pd.concat(dfs, ignore_index=True)

        # SPEED OPTIMIZATION: sort for faster equality filters + time windows
        print("Sorting and indexing data for fast lookup...")
        # Re-categorical after concat (concat can widen categories → object)
        combined["expiry_date"] = pd.Categorical(combined["expiry_date"])
        combined["ist_date"] = pd.Categorical(combined["ist_date"])
        combined["opt_type"] = pd.Categorical(combined["opt_type"])
        combined = combined.sort_values(
            ["expiry_date", "opt_type", "strike", "ist_time"]
        ).reset_index(drop=True)

        print(f"Total rows: {len(combined):,}")
        return combined

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
