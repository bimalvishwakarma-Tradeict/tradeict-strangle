# basket_sim.py — Simulate one 1DTE basket spanning two calendar days
#
# Entry ~17:32 IST on entry_day → exit by 17:15 IST on expiry_day.
# Standalone: backtest/ + stdlib + pandas only.

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

try:
    from backtest.data_loader import DataLoader
    from backtest.strategy_sim import (
        DayResult,
        HedgeLeg,
        LegState,
    )
except ImportError:
    from data_loader import DataLoader
    from strategy_sim import DayResult, HedgeLeg, LegState


def _series_price_at(series: pd.Series, minute: datetime) -> Optional[float]:
    """Lookup minute price; return None if missing or NaN."""
    if series is None or series.empty:
        return None
    ts = pd.Timestamp(minute)
    val = None
    try:
        if ts in series.index:
            val = series.loc[ts]
        else:
            val = series.get(ts)
    except (KeyError, TypeError, ValueError):
        val = series.get(minute)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


class BasketSimulator:
    """Simulate one continuous 1DTE short strangle/straddle basket."""

    def __init__(
        self,
        trade_type: str = "strangle",
        target_premium_per_side: float = 250.0,
        entry_hour_ist: int = 17,
        entry_minute_ist: int = 32,
        trigger_pct: float = 160.0,
        profit_target_pct: float = 25.0,
        stoploss_pct: float = 50.0,
        min_replacement_premium: float = 150.0,
        conversion_equality_pct: float = 10.0,
        quantity: int = 100,
        fee_per_leg_usd: float = 0.75,
        slippage_pct: float = 2.0,
        contract_size: float = 0.001,
        strike_increment: float = 200.0,
    ) -> None:
        self.trade_type = trade_type
        self.target_premium_per_side = target_premium_per_side
        self.entry_hour_ist = entry_hour_ist
        self.entry_minute_ist = entry_minute_ist
        self.trigger_pct = trigger_pct
        self.profit_target_pct = profit_target_pct
        self.stoploss_pct = stoploss_pct
        self.min_replacement_premium = min_replacement_premium
        self.conversion_equality_pct = conversion_equality_pct
        self.quantity = quantity
        self.fee_per_leg_usd = fee_per_leg_usd
        self.slippage_pct = slippage_pct
        self.contract_size = contract_size
        self.strike_increment = strike_increment
        self.loader = DataLoader()

    def get_expiry_date(self, entry_day: date) -> date:
        """Next trading-day expiry (skip Sat/Sun — no weekend expiry)."""
        expiry = entry_day + timedelta(days=1)
        if expiry.weekday() == 5:  # Saturday → Monday
            expiry += timedelta(days=2)
        if expiry.weekday() == 6:  # Sunday → Monday
            expiry += timedelta(days=1)
        return expiry

    def get_full_price_series(
        self,
        df: pd.DataFrame,
        expiry_date: date,
        opt_type: str,
        strike: float,
        start_ist: datetime,
        end_ist: datetime,
    ) -> pd.Series:
        """
        Minute last-price series for a contract between start_ist and end_ist.

        Pre-filters by symbol once (via DataLoader.filter_symbol), then builds
        the minute series — spans entry evening + expiry day via expiry_date.
        """
        if isinstance(expiry_date, datetime):
            expiry_date = expiry_date.date()

        # Pre-filter once for this strike (avoids scanning full month per call)
        if len(df) > 100_000:
            sym = self.loader.filter_symbol(df, expiry_date, opt_type, strike)
        else:
            sym = df

        return self.loader.get_minute_prices(
            sym,
            start_ist.date(),
            expiry_date,
            opt_type,
            strike,
            start_ist,
            end_ist,
        )

    def _fail_result(
        self,
        entry_day: date,
        expiry_date: date,
        entry_ist: datetime,
        notes: str,
        call_strike: float = 0.0,
        put_strike: float = 0.0,
        entry_call_premium: float = 0.0,
        entry_put_premium: float = 0.0,
    ) -> DayResult:
        return DayResult(
            trade_date=entry_day,
            expiry_date=expiry_date,
            trade_type=self.trade_type,
            entry_ist=entry_ist,
            call_strike=call_strike,
            put_strike=put_strike,
            entry_call_premium=entry_call_premium,
            entry_put_premium=entry_put_premium,
            initial_premium=0.0,
            profit_target_usd=0.0,
            stoploss_usd=0.0,
            exit_ist=None,
            exit_reason="NO_DATA",
            gross_pnl=0.0,
            total_fees=0.0,
            net_pnl=0.0,
            max_drawdown=0.0,
            total_adjustments=0,
            total_conversions=0,
            total_reversals=0,
            minutes_in_conversion=0,
            adjustment_log=[],
            data_ok=False,
            notes=notes,
        )

    def simulate_basket(
        self,
        df: pd.DataFrame,
        entry_day: date,
        basket_number: int = 1,
        entry_ist_override: Optional[datetime] = None,
    ) -> DayResult:
        """
        Run one basket from entry_day evening through expiry_day 17:15 IST.

        entry_ist_override: optional exact entry time (continuous re-entry).
        """
        qty = self.quantity
        cs = self.contract_size
        fee = self.fee_per_leg_usd

        # STEP 1 — Setup
        expiry_date = self.get_expiry_date(entry_day)
        if entry_ist_override is not None:
            entry_ist = entry_ist_override.replace(second=0, microsecond=0)
            # Entry day follows the override clock (continuous mode)
            entry_day = entry_ist.date()
            expiry_date = self.get_expiry_date(entry_day)
        else:
            entry_ist = datetime(
                entry_day.year,
                entry_day.month,
                entry_day.day,
                self.entry_hour_ist,
                self.entry_minute_ist,
                0,
            )

        close_ist = datetime(
            expiry_date.year, expiry_date.month, expiry_date.day, 17, 15, 0
        )

        print(
            f"Basket {basket_number}: "
            f"{entry_ist.strftime('%Y-%m-%d %H:%M')} → "
            f"{close_ist.strftime('%Y-%m-%d %H:%M')}"
        )

        # STEP 2 — Find entry strikes
        try:
            atm = self.loader.get_atm_strike(
                df,
                entry_day,
                expiry_date,
                entry_ist.hour,
                entry_ist.minute,
                window_minutes=10,
            )
        except ValueError:
            return self._fail_result(
                entry_day,
                expiry_date,
                entry_ist,
                notes=f"No entry data for {entry_day} (ATM not found)",
            )

        if self.trade_type == "straddle":
            call_strike = put_strike = float(atm)
            entry_call_prem = self.loader.get_price_at_time(
                df,
                entry_day,
                expiry_date,
                "call",
                call_strike,
                entry_ist,
                lookback_minutes=10,
            )
            entry_put_prem = self.loader.get_price_at_time(
                df,
                entry_day,
                expiry_date,
                "put",
                put_strike,
                entry_ist,
                lookback_minutes=10,
            )
            if entry_call_prem is None or entry_put_prem is None:
                return self._fail_result(
                    entry_day,
                    expiry_date,
                    entry_ist,
                    notes=f"No entry data for {entry_day}",
                    call_strike=call_strike,
                    put_strike=put_strike,
                )
        else:
            try:
                call_strike, entry_call_prem = self.loader.find_strike_by_premium(
                    df,
                    entry_day,
                    expiry_date,
                    "call",
                    self.target_premium_per_side,
                    entry_ist,
                    exclude_strike=atm,
                    lookback_minutes=10,
                    prefer_above=True,
                )
                put_strike, entry_put_prem = self.loader.find_strike_by_premium(
                    df,
                    entry_day,
                    expiry_date,
                    "put",
                    self.target_premium_per_side,
                    entry_ist,
                    exclude_strike=atm,
                    lookback_minutes=10,
                    prefer_above=True,
                )
            except ValueError as e:
                return self._fail_result(
                    entry_day,
                    expiry_date,
                    entry_ist,
                    notes=f"No entry data for {entry_day}: {e}",
                )

        entry_call_prem = float(entry_call_prem)
        entry_put_prem = float(entry_put_prem)
        call_strike = float(call_strike)
        put_strike = float(put_strike)

        # STEP 3 — Targets
        initial_premium = entry_call_prem + entry_put_prem
        max_profit_usd = initial_premium * qty * cs
        profit_target_usd = max_profit_usd * self.profit_target_pct / 100.0
        stoploss_usd = max_profit_usd * self.stoploss_pct / 100.0

        print(
            f"  Entry: Call {call_strike:.0f} @${entry_call_prem:.0f} + "
            f"Put {put_strike:.0f} @${entry_put_prem:.0f} = ${initial_premium:.0f}"
        )
        print(
            f"  Target: +${profit_target_usd:.2f} | SL: -${stoploss_usd:.2f}"
        )

        # STEP 4 — Position
        call_leg = LegState(
            "call", call_strike, entry_call_prem, entry_call_prem, qty
        )
        put_leg = LegState(
            "put", put_strike, entry_put_prem, entry_put_prem, qty
        )
        hedge_leg: Optional[HedgeLeg] = None
        in_conversion_mode = False
        realized_pnl = 0.0
        total_fees = 2.0 * fee
        adjustment_log: list = []
        total_adjustments = 0
        total_conversions = 0
        total_reversals = 0
        minutes_in_conversion = 0
        max_drawdown = 0.0
        last_call_price = entry_call_prem
        last_put_price = entry_put_prem
        last_hedge_price: Optional[float] = None
        hedge_prices: pd.Series = pd.Series(dtype=float)
        exit_reason = "PRE_EXPIRY"
        exit_ist: Optional[datetime] = close_ist
        gross_mtm = 0.0

        # STEP 5 — Price series
        call_prices = self.get_full_price_series(
            df, expiry_date, "call", call_strike, entry_ist, close_ist
        )
        put_prices = self.get_full_price_series(
            df, expiry_date, "put", put_strike, entry_ist, close_ist
        )
        if call_prices.empty and put_prices.empty:
            return self._fail_result(
                entry_day,
                expiry_date,
                entry_ist,
                notes="No price data",
                call_strike=call_strike,
                put_strike=put_strike,
                entry_call_premium=entry_call_prem,
                entry_put_premium=entry_put_prem,
            )

        print(
            f"  Price series: {len(call_prices)} call ticks, "
            f"{len(put_prices)} put ticks"
        )

        # STEP 6 — Monitoring
        timeline = pd.date_range(start=entry_ist, end=close_ist, freq="1min")
        consecutive_no_data = 0

        for minute_ts in timeline:
            minute = minute_ts.to_pydatetime()

            call_raw = _series_price_at(call_prices, minute)
            put_raw = _series_price_at(put_prices, minute)

            if call_raw is None and put_raw is None:
                consecutive_no_data += 1
                call_curr = last_call_price
                put_curr = last_put_price
            else:
                consecutive_no_data = 0
                if call_raw is not None:
                    last_call_price = call_raw
                if put_raw is not None:
                    last_put_price = put_raw
                call_curr = last_call_price
                put_curr = last_put_price

            if call_curr is None or put_curr is None:
                continue

            if consecutive_no_data > 60:
                exit_reason = "NO_DATA"
                exit_ist = minute
                break

            call_upnl = (call_leg.entry_premium - call_curr) * qty * cs
            put_upnl = (put_leg.entry_premium - put_curr) * qty * cs
            hedge_upnl = 0.0
            if in_conversion_mode and hedge_leg is not None and hedge_leg.is_open:
                hedge_px = _series_price_at(hedge_prices, minute)
                if hedge_px is None:
                    hedge_px = last_hedge_price
                if hedge_px is None:
                    hedge_px = hedge_leg.entry_premium
                last_hedge_price = hedge_px
                hedge_upnl = (hedge_px - hedge_leg.entry_premium) * qty * cs

            gross_mtm = call_upnl + put_upnl + hedge_upnl + realized_pnl

            est_exit_fees = 2.0 * fee
            if hedge_leg is not None and hedge_leg.is_open:
                est_exit_fees += fee
            slippage = abs(gross_mtm) * self.slippage_pct / 100.0
            net_mtm = gross_mtm - total_fees - est_exit_fees - slippage

            if net_mtm < max_drawdown:
                max_drawdown = net_mtm

            if in_conversion_mode:
                minutes_in_conversion += 1

            # --- EXIT CHECKS (priority: PT → SL → PRE_EXPIRY) ---
            if net_mtm >= profit_target_usd:
                total_fees += 2.0 * fee
                if hedge_leg is not None and hedge_leg.is_open:
                    total_fees += fee
                exit_reason = "PROFIT_TARGET"
                exit_ist = minute
                break

            if net_mtm <= -stoploss_usd:
                total_fees += 2.0 * fee
                if hedge_leg is not None and hedge_leg.is_open:
                    total_fees += fee
                exit_reason = "STOPLOSS"
                exit_ist = minute
                break

            if minute >= close_ist:
                total_fees += 2.0 * fee
                if hedge_leg is not None and hedge_leg.is_open:
                    total_fees += fee
                exit_reason = "PRE_EXPIRY"
                exit_ist = minute
                break

            # --- CONVERSION REVERSAL ---
            if in_conversion_mode and hedge_leg is not None and hedge_leg.is_open:
                max_prem = max(call_curr, put_curr)
                if max_prem > 0:
                    diff_pct = abs(call_curr - put_curr) / max_prem * 100.0
                    if diff_pct <= self.conversion_equality_pct:
                        hedge_curr = _series_price_at(hedge_prices, minute)
                        if hedge_curr is None:
                            hedge_curr = last_hedge_price
                        if hedge_curr is None:
                            hedge_curr = hedge_leg.entry_premium

                        hedge_pnl = (
                            (hedge_curr - hedge_leg.entry_premium) * qty * cs
                        )
                        realized_pnl += hedge_pnl
                        total_fees += fee
                        hedge_leg.is_open = False
                        in_conversion_mode = False
                        total_reversals += 1
                        hedge_prices = pd.Series(dtype=float)
                        last_hedge_price = None

                        call_leg.baseline_premium = call_curr
                        put_leg.baseline_premium = put_curr

                        call_prices = self.get_full_price_series(
                            df,
                            expiry_date,
                            "call",
                            call_leg.strike,
                            minute,
                            close_ist,
                        )
                        put_prices = self.get_full_price_series(
                            df,
                            expiry_date,
                            "put",
                            put_leg.strike,
                            minute,
                            close_ist,
                        )

                        adjustment_log.append(
                            {
                                "minute": minute.strftime("%Y-%m-%d %H:%M"),
                                "time": minute.strftime("%H:%M"),
                                "adj_type": "CONVERSION_EXIT",
                                "triggered_leg": "hedge",
                                "old_strike": hedge_leg.strike,
                                "new_strike": hedge_leg.strike,
                                "trigger_pct_reached": round(diff_pct, 1),
                                "old_premium": round(hedge_leg.entry_premium, 2),
                                "new_premium": round(float(hedge_curr), 2),
                                "other_leg_premium": round(put_curr, 2),
                                "net_pnl_at_adj": round(net_mtm, 2),
                            }
                        )
                        continue

            # --- ADJUSTMENT TRIGGER ---
            if not in_conversion_mode:
                call_trigger = (
                    call_leg.baseline_premium * self.trigger_pct / 100.0
                )
                put_trigger = (
                    put_leg.baseline_premium * self.trigger_pct / 100.0
                )

                triggered: Optional[str] = None
                if call_curr >= call_trigger:
                    triggered = "call"
                elif put_curr >= put_trigger:
                    triggered = "put"

                if triggered is None:
                    continue

                triggered_leg = call_leg if triggered == "call" else put_leg
                other_leg = put_leg if triggered == "call" else call_leg
                triggered_curr = (
                    call_curr if triggered == "call" else put_curr
                )
                other_curr = put_curr if triggered == "call" else call_curr
                trigger_pct_reached = (
                    triggered_curr / triggered_leg.baseline_premium * 100.0
                    if triggered_leg.baseline_premium > 0
                    else 0.0
                )

                if other_curr >= self.min_replacement_premium:
                    # NORMAL ADJUSTMENT
                    leg_pnl = (
                        (triggered_leg.entry_premium - triggered_curr)
                        * qty
                        * cs
                    )
                    realized_pnl += leg_pnl
                    total_fees += fee

                    try:
                        new_strike, new_premium = (
                            self.loader.find_strike_by_premium(
                                df,
                                entry_day,
                                expiry_date,
                                triggered,
                                other_curr,
                                minute,
                                exclude_strike=triggered_leg.strike,
                            )
                        )
                    except ValueError:
                        realized_pnl -= leg_pnl
                        total_fees -= fee
                        continue

                    total_fees += fee
                    total_adjustments += 1

                    adjustment_log.append(
                        {
                            "minute": minute.strftime("%Y-%m-%d %H:%M"),
                            "time": minute.strftime("%H:%M"),
                            "adj_type": "NORMAL",
                            "triggered_leg": triggered,
                            "old_strike": triggered_leg.strike,
                            "new_strike": new_strike,
                            "trigger_pct_reached": round(trigger_pct_reached, 1),
                            "old_premium": round(triggered_curr, 2),
                            "new_premium": round(new_premium, 2),
                            "other_leg_premium": round(other_curr, 2),
                            "net_pnl_at_adj": round(net_mtm, 2),
                        }
                    )

                    triggered_leg.strike = new_strike
                    triggered_leg.entry_premium = new_premium
                    triggered_leg.baseline_premium = new_premium
                    other_leg.baseline_premium = other_curr

                    new_series = self.get_full_price_series(
                        df,
                        expiry_date,
                        triggered,
                        new_strike,
                        minute,
                        close_ist,
                    )
                    if triggered == "call":
                        call_prices = new_series
                        last_call_price = new_premium
                    else:
                        put_prices = new_series
                        last_put_price = new_premium

                else:
                    # CONVERSION MODE
                    if triggered == "call":
                        hedge_strike = (
                            triggered_leg.strike - self.strike_increment
                        )
                    else:
                        hedge_strike = (
                            triggered_leg.strike + self.strike_increment
                        )

                    hedge_series = self.get_full_price_series(
                        df,
                        expiry_date,
                        triggered,
                        hedge_strike,
                        entry_ist,
                        close_ist,
                    )
                    if hedge_series.empty:
                        continue

                    hedge_price = _series_price_at(hedge_series, minute)
                    if hedge_price is None:
                        continue

                    total_fees += fee
                    hedge_leg = HedgeLeg(
                        triggered, hedge_strike, float(hedge_price), qty
                    )
                    hedge_prices = hedge_series
                    last_hedge_price = float(hedge_price)

                    other_pnl = (
                        (other_leg.entry_premium - other_curr) * qty * cs
                    )
                    realized_pnl += other_pnl
                    total_fees += fee

                    new_other_target = triggered_curr / 2.0
                    try:
                        new_other_strike, new_other_premium = (
                            self.loader.find_strike_by_premium(
                                df,
                                entry_day,
                                expiry_date,
                                other_leg.opt_type,
                                new_other_target,
                                minute,
                                exclude_strike=other_leg.strike,
                                prefer_above=True,
                            )
                        )
                    except ValueError:
                        hedge_leg = None
                        hedge_prices = pd.Series(dtype=float)
                        last_hedge_price = None
                        realized_pnl -= other_pnl
                        total_fees -= 2.0 * fee
                        continue

                    total_fees += fee
                    total_conversions += 1
                    total_adjustments += 1

                    adjustment_log.append(
                        {
                            "minute": minute.strftime("%Y-%m-%d %H:%M"),
                            "time": minute.strftime("%H:%M"),
                            "adj_type": "CONVERSION_ENTER",
                            "triggered_leg": triggered,
                            "old_strike": other_leg.strike,
                            "new_strike": new_other_strike,
                            "trigger_pct_reached": round(trigger_pct_reached, 1),
                            "old_premium": round(other_curr, 2),
                            "new_premium": round(new_other_premium, 2),
                            "other_leg_premium": round(triggered_curr, 2),
                            "net_pnl_at_adj": round(net_mtm, 2),
                        }
                    )

                    other_leg.strike = new_other_strike
                    other_leg.entry_premium = new_other_premium
                    other_leg.baseline_premium = new_other_premium
                    in_conversion_mode = True

                    new_other_series = self.get_full_price_series(
                        df,
                        expiry_date,
                        other_leg.opt_type,
                        new_other_strike,
                        minute,
                        close_ist,
                    )
                    if triggered == "call":
                        put_prices = new_other_series
                        last_put_price = new_other_premium
                    else:
                        call_prices = new_other_series
                        last_call_price = new_other_premium

        final_gross = gross_mtm
        final_slippage = abs(final_gross) * self.slippage_pct / 100.0
        final_net = final_gross - total_fees - final_slippage

        exit_label = (
            exit_ist.strftime("%Y-%m-%d %H:%M") if exit_ist else "?"
        )
        print(
            f"  Exit: {exit_reason} at {exit_label} IST | "
            f"Net P&L: ${final_net:+.2f} | Adj: {total_adjustments} "
            f"Conv: {total_conversions}"
        )

        return DayResult(
            trade_date=entry_day,
            expiry_date=expiry_date,
            trade_type=self.trade_type,
            entry_ist=entry_ist,
            call_strike=call_leg.strike,
            put_strike=put_leg.strike,
            entry_call_premium=entry_call_prem,
            entry_put_premium=entry_put_prem,
            initial_premium=initial_premium,
            profit_target_usd=profit_target_usd,
            stoploss_usd=stoploss_usd,
            exit_ist=exit_ist,
            exit_reason=exit_reason,
            gross_pnl=final_gross,
            total_fees=total_fees,
            net_pnl=final_net,
            max_drawdown=max_drawdown,
            total_adjustments=total_adjustments,
            total_conversions=total_conversions,
            total_reversals=total_reversals,
            minutes_in_conversion=minutes_in_conversion,
            adjustment_log=adjustment_log,
            data_ok=True,
        )
