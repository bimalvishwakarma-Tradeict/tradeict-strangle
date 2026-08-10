# strategy_sim.py — Simulate one short-straddle/strangle trading day
#
# Standalone backtest helper. Do NOT import from backend/.

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

try:
    from backtest.data_loader import DataLoader
except ImportError:
    from data_loader import DataLoader


@dataclass
class LegState:
    opt_type: str  # 'call' or 'put'
    strike: float
    entry_premium: float  # price when we shorted this leg
    baseline_premium: float  # for trigger calculation (resets on adjustment)
    quantity: int
    is_open: bool = True
    realized_pnl: float = 0.0
    fees_paid: float = 0.0


@dataclass
class HedgeLeg:
    opt_type: str  # same type as triggered leg
    strike: float
    entry_premium: float  # price when we BOUGHT this leg (long position)
    quantity: int
    is_open: bool = True


@dataclass
class AdjustmentEvent:
    minute: datetime
    adj_type: str  # 'NORMAL' | 'CONVERSION_ENTER' | 'CONVERSION_EXIT'
    triggered_leg: str  # 'call' or 'put'
    old_strike: float
    new_strike: float
    trigger_pct_reached: float
    old_premium: float
    new_premium: float
    other_leg_premium: float
    net_pnl_at_adj: float  # net MTM when adjustment fired


@dataclass
class DayResult:
    trade_date: date
    expiry_date: date
    trade_type: str  # 'straddle' or 'strangle'

    # Entry
    entry_ist: datetime
    call_strike: float
    put_strike: float
    entry_call_premium: float
    entry_put_premium: float
    initial_premium: float  # call + put combined
    profit_target_usd: float
    stoploss_usd: float

    # Exit
    exit_ist: Optional[datetime]
    exit_reason: str  # 'PROFIT_TARGET'|'STOPLOSS'|'PRE_EXPIRY'|'NO_DATA'

    # P&L (in USD, 1 lot = 0.001 BTC)
    gross_pnl: float  # UPNL + realized, before fees
    total_fees: float  # all entry + exit fees
    net_pnl: float  # gross - fees - slippage
    max_drawdown: float  # worst net_pnl seen during trade (negative = loss)

    # Activity
    total_adjustments: int
    total_conversions: int
    total_reversals: int
    minutes_in_conversion: int
    adjustment_log: list  # list of AdjustmentEvent dicts

    # Data quality
    data_ok: bool  # False if insufficient data
    notes: str = ""


def _skip_weekend(d: date) -> date:
    """Move Saturday → Monday, Sunday → Monday."""
    if d.weekday() == 5:  # Saturday
        return d + timedelta(days=2)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def _series_price_at(series: pd.Series, minute: datetime) -> Optional[float]:
    """Lookup minute price; return None if missing or NaN."""
    if series is None or series.empty:
        return None
    val = series.get(minute)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        # Try Timestamp key (date_range index)
        try:
            val = series.get(pd.Timestamp(minute))
        except (TypeError, ValueError, KeyError):
            val = None
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


class StrategySimulator:
    """Simulate one day of short straddle/strangle with adjustments."""

    def __init__(
        self,
        trade_type: str = "straddle",
        expiry_type: str = "1DTE",
        entry_hour_ist: int = 10,
        entry_minute_ist: int = 0,
        trigger_pct: float = 150.0,
        profit_target_pct: float = 50.0,
        stoploss_pct: float = 100.0,
        min_replacement_premium: float = 150.0,
        conversion_equality_pct: float = 10.0,
        target_premium_per_side: float = 350.0,
        quantity: int = 100,
        fee_per_leg_usd: float = 0.75,
        slippage_pct: float = 2.0,
        contract_size: float = 0.001,
        strike_increment: float = 200.0,
    ) -> None:
        self.trade_type = trade_type
        self.expiry_type = expiry_type
        self.entry_hour_ist = entry_hour_ist
        self.entry_minute_ist = entry_minute_ist
        self.trigger_pct = trigger_pct
        self.profit_target_pct = profit_target_pct
        self.stoploss_pct = stoploss_pct
        self.min_replacement_premium = min_replacement_premium
        self.conversion_equality_pct = conversion_equality_pct
        self.target_premium_per_side = target_premium_per_side
        self.quantity = quantity
        self.fee_per_leg_usd = fee_per_leg_usd
        self.slippage_pct = slippage_pct
        self.contract_size = contract_size
        self.strike_increment = strike_increment
        self.loader = DataLoader()

    def _fail_result(
        self,
        trade_date: date,
        expiry_date: date,
        entry_ist: datetime,
        notes: str,
        call_strike: float = 0.0,
        put_strike: float = 0.0,
        entry_call_premium: float = 0.0,
        entry_put_premium: float = 0.0,
    ) -> DayResult:
        return DayResult(
            trade_date=trade_date,
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

    def _close_all(
        self,
        call_leg: LegState,
        put_leg: LegState,
        hedge_leg: Optional[HedgeLeg],
        call_curr: float,
        put_curr: float,
        df: pd.DataFrame,
        minute: datetime,
        trade_date: date,
        expiry_date: date,
        realized_pnl: float,
    ) -> tuple[float, float]:
        """
        Close open short legs + hedge. Returns (exit_fees, updated_realized_pnl).

        Short PnL: (entry - exit) * qty * contract_size
        Long hedge PnL: (exit - entry) * qty * contract_size
        """
        fees = 0.0
        realized = realized_pnl
        qty = self.quantity
        cs = self.contract_size
        fee = self.fee_per_leg_usd

        if call_leg.is_open:
            realized += (call_leg.entry_premium - call_curr) * qty * cs
            fees += fee
            call_leg.is_open = False

        if put_leg.is_open:
            realized += (put_leg.entry_premium - put_curr) * qty * cs
            fees += fee
            put_leg.is_open = False

        if hedge_leg is not None and hedge_leg.is_open:
            hedge_curr = self.loader.get_price_at_time(
                df,
                trade_date,
                expiry_date,
                hedge_leg.opt_type,
                hedge_leg.strike,
                minute,
            )
            if hedge_curr is None:
                hedge_curr = hedge_leg.entry_premium
            realized += (hedge_curr - hedge_leg.entry_premium) * qty * cs
            fees += fee
            hedge_leg.is_open = False

        return fees, realized

    def simulate_day(self, df: pd.DataFrame, trade_date: date) -> DayResult:
        """Run full day simulation for trade_date. Returns DayResult."""
        loader = self.loader
        qty = self.quantity
        cs = self.contract_size
        fee = self.fee_per_leg_usd

        # STEP 0 — Expiry date
        if self.expiry_type == "0DTE":
            expiry_date = trade_date
        else:
            expiry_date = trade_date + timedelta(days=1)
        expiry_date = _skip_weekend(expiry_date)

        # STEP 1 — Entry / close times
        entry_ist = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.entry_hour_ist,
            self.entry_minute_ist,
            0,
        )
        if expiry_date == trade_date:
            # 0DTE: pre-expiry close 15 min before 17:30 IST
            close_ist = datetime(
                expiry_date.year, expiry_date.month, expiry_date.day, 17, 15, 0
            )
        else:
            close_ist = datetime(
                expiry_date.year, expiry_date.month, expiry_date.day, 17, 30, 0
            )

        # STEP 2 — Strikes
        try:
            atm = loader.get_atm_strike(
                df,
                trade_date,
                expiry_date,
                self.entry_hour_ist,
                self.entry_minute_ist,
            )
        except ValueError:
            return self._fail_result(
                trade_date, expiry_date, entry_ist, notes="ATM not found"
            )

        if self.trade_type == "strangle":
            try:
                call_strike, _ = loader.find_strike_by_premium(
                    df,
                    trade_date,
                    expiry_date,
                    "call",
                    self.target_premium_per_side,
                    entry_ist,
                    exclude_strike=atm,
                )
                put_strike, _ = loader.find_strike_by_premium(
                    df,
                    trade_date,
                    expiry_date,
                    "put",
                    self.target_premium_per_side,
                    entry_ist,
                    exclude_strike=atm,
                )
            except ValueError as e:
                return self._fail_result(
                    trade_date,
                    expiry_date,
                    entry_ist,
                    notes=f"Strangle strikes not found: {e}",
                )
        else:
            call_strike = put_strike = float(atm)

        # STEP 3 — Entry premiums
        entry_call_premium = loader.get_price_at_time(
            df, trade_date, expiry_date, "call", call_strike, entry_ist
        )
        entry_put_premium = loader.get_price_at_time(
            df, trade_date, expiry_date, "put", put_strike, entry_ist
        )
        if entry_call_premium is None or entry_put_premium is None:
            return self._fail_result(
                trade_date,
                expiry_date,
                entry_ist,
                notes="No price at entry",
                call_strike=call_strike,
                put_strike=put_strike,
                entry_call_premium=entry_call_premium or 0.0,
                entry_put_premium=entry_put_premium or 0.0,
            )

        initial_premium = float(entry_call_premium) + float(entry_put_premium)
        profit_target_usd = (
            initial_premium * self.profit_target_pct / 100.0 * qty * cs
        )
        stoploss_usd = (
            initial_premium * self.stoploss_pct / 100.0 * qty * cs
        )

        # STEP 4 — Position state
        call_leg = LegState(
            "call",
            float(call_strike),
            float(entry_call_premium),
            float(entry_call_premium),
            qty,
        )
        put_leg = LegState(
            "put",
            float(put_strike),
            float(entry_put_premium),
            float(entry_put_premium),
            qty,
        )
        hedge_leg: Optional[HedgeLeg] = None
        in_conversion_mode = False
        realized_pnl = 0.0
        total_fees = 2.0 * fee  # entry fees
        adjustment_log: list = []
        total_adjustments = 0
        total_conversions = 0
        total_reversals = 0
        minutes_in_conversion = 0
        max_drawdown = 0.0

        # STEP 5 — Initial price series
        call_prices = loader.get_minute_prices(
            df,
            trade_date,
            expiry_date,
            "call",
            call_leg.strike,
            entry_ist,
            close_ist,
        )
        put_prices = loader.get_minute_prices(
            df,
            trade_date,
            expiry_date,
            "put",
            put_leg.strike,
            entry_ist,
            close_ist,
        )
        if call_prices.empty and put_prices.empty:
            return self._fail_result(
                trade_date,
                expiry_date,
                entry_ist,
                notes="No minute price series",
                call_strike=call_strike,
                put_strike=put_strike,
                entry_call_premium=float(entry_call_premium),
                entry_put_premium=float(entry_put_premium),
            )

        # STEP 6 — Monitoring loop
        timeline = pd.date_range(start=entry_ist, end=close_ist, freq="1min")
        last_call_price: Optional[float] = float(entry_call_premium)
        last_put_price: Optional[float] = float(entry_put_premium)
        last_hedge_price: Optional[float] = None
        hedge_prices: pd.Series = pd.Series(dtype=float)
        gross_mtm = 0.0
        exit_reason = "PRE_EXPIRY"
        exit_ist: Optional[datetime] = None

        for minute_ts in timeline:
            minute = minute_ts.to_pydatetime()

            call_curr = _series_price_at(call_prices, minute)
            put_curr = _series_price_at(put_prices, minute)
            if call_curr is None:
                call_curr = last_call_price
            if put_curr is None:
                put_curr = last_put_price
            if call_curr is None or put_curr is None:
                continue

            last_call_price = call_curr
            last_put_price = put_curr

            # --- Net MTM (shorts) ---
            call_upnl = (call_leg.entry_premium - call_curr) * qty * cs
            put_upnl = (put_leg.entry_premium - put_curr) * qty * cs
            combined_upnl = call_upnl + put_upnl

            # Hedge UPNL if open (long) — use minute series, not full DF scan
            hedge_upnl = 0.0
            if in_conversion_mode and hedge_leg is not None and hedge_leg.is_open:
                hedge_px = _series_price_at(hedge_prices, minute)
                if hedge_px is None:
                    hedge_px = last_hedge_price
                if hedge_px is None:
                    hedge_px = hedge_leg.entry_premium
                last_hedge_price = hedge_px
                hedge_upnl = (hedge_px - hedge_leg.entry_premium) * qty * cs

            gross_mtm = combined_upnl + hedge_upnl + realized_pnl

            est_exit_fees = 2.0 * fee
            if hedge_leg is not None and hedge_leg.is_open:
                est_exit_fees += fee
            slippage = abs(gross_mtm) * self.slippage_pct / 100.0
            net_mtm = gross_mtm - total_fees - est_exit_fees - slippage

            if net_mtm < max_drawdown:
                max_drawdown = net_mtm

            if in_conversion_mode:
                minutes_in_conversion += 1

            # --- CHECK 1: PROFIT TARGET ---
            if net_mtm >= profit_target_usd:
                exit_fees, realized_pnl = self._close_all(
                    call_leg,
                    put_leg,
                    hedge_leg,
                    call_curr,
                    put_curr,
                    df,
                    minute,
                    trade_date,
                    expiry_date,
                    realized_pnl,
                )
                total_fees += exit_fees
                exit_reason = "PROFIT_TARGET"
                exit_ist = minute
                break

            # --- CHECK 2: STOP LOSS ---
            if net_mtm <= -stoploss_usd:
                exit_fees, realized_pnl = self._close_all(
                    call_leg,
                    put_leg,
                    hedge_leg,
                    call_curr,
                    put_curr,
                    df,
                    minute,
                    trade_date,
                    expiry_date,
                    realized_pnl,
                )
                total_fees += exit_fees
                exit_reason = "STOPLOSS"
                exit_ist = minute
                break

            # --- CHECK 3: PRE-EXPIRY ---
            if minute >= close_ist:
                exit_fees, realized_pnl = self._close_all(
                    call_leg,
                    put_leg,
                    hedge_leg,
                    call_curr,
                    put_curr,
                    df,
                    minute,
                    trade_date,
                    expiry_date,
                    realized_pnl,
                )
                total_fees += exit_fees
                exit_reason = "PRE_EXPIRY"
                exit_ist = minute
                break

            # --- CHECK 4: CONVERSION MODE REVERSAL ---
            if in_conversion_mode and hedge_leg is not None and hedge_leg.is_open:
                max_prem = max(call_curr, put_curr)
                if max_prem > 0:
                    diff_pct = abs(call_curr - put_curr) / max_prem * 100.0
                    if diff_pct <= self.conversion_equality_pct:
                        hedge_curr = loader.get_price_at_time(
                            df,
                            trade_date,
                            expiry_date,
                            hedge_leg.opt_type,
                            hedge_leg.strike,
                            minute,
                        )
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

                        adjustment_log.append(
                            AdjustmentEvent(
                                minute=minute,
                                adj_type="CONVERSION_EXIT",
                                triggered_leg=hedge_leg.opt_type,
                                old_strike=hedge_leg.strike,
                                new_strike=hedge_leg.strike,
                                trigger_pct_reached=diff_pct,
                                old_premium=hedge_leg.entry_premium,
                                new_premium=float(hedge_curr),
                                other_leg_premium=put_curr
                                if hedge_leg.opt_type == "call"
                                else call_curr,
                                net_pnl_at_adj=net_mtm,
                            ).__dict__
                        )

                        call_prices = loader.get_minute_prices(
                            df,
                            trade_date,
                            expiry_date,
                            "call",
                            call_leg.strike,
                            minute,
                            close_ist,
                        )
                        put_prices = loader.get_minute_prices(
                            df,
                            trade_date,
                            expiry_date,
                            "put",
                            put_leg.strike,
                            minute,
                            close_ist,
                        )
                        continue

            # --- CHECK 5: ADJUSTMENT TRIGGER ---
            if not in_conversion_mode:
                call_trigger = call_leg.baseline_premium * self.trigger_pct / 100.0
                put_trigger = put_leg.baseline_premium * self.trigger_pct / 100.0

                triggered: Optional[str] = None
                if call_curr >= call_trigger:
                    triggered = "call"
                elif put_curr >= put_trigger:
                    triggered = "put"

                if triggered is not None:
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
                        # === NORMAL ADJUSTMENT ===
                        leg_pnl = (
                            (triggered_leg.entry_premium - triggered_curr)
                            * qty
                            * cs
                        )
                        realized_pnl += leg_pnl
                        total_fees += fee

                        try:
                            new_strike, new_premium = loader.find_strike_by_premium(
                                df,
                                trade_date,
                                expiry_date,
                                triggered,
                                other_curr,
                                minute,
                                exclude_strike=triggered_leg.strike,
                            )
                        except ValueError:
                            # Undo close accounting — skip this minute
                            realized_pnl -= leg_pnl
                            total_fees -= fee
                            continue

                        total_fees += fee

                        adjustment_log.append(
                            AdjustmentEvent(
                                minute=minute,
                                adj_type="NORMAL",
                                triggered_leg=triggered,
                                old_strike=triggered_leg.strike,
                                new_strike=new_strike,
                                trigger_pct_reached=trigger_pct_reached,
                                old_premium=triggered_curr,
                                new_premium=new_premium,
                                other_leg_premium=other_curr,
                                net_pnl_at_adj=net_mtm,
                            ).__dict__
                        )

                        triggered_leg.strike = new_strike
                        triggered_leg.entry_premium = new_premium
                        triggered_leg.baseline_premium = new_premium
                        other_leg.baseline_premium = other_curr
                        total_adjustments += 1

                        if triggered == "call":
                            call_prices = loader.get_minute_prices(
                                df,
                                trade_date,
                                expiry_date,
                                "call",
                                new_strike,
                                minute,
                                close_ist,
                            )
                            last_call_price = new_premium
                        else:
                            put_prices = loader.get_minute_prices(
                                df,
                                trade_date,
                                expiry_date,
                                "put",
                                new_strike,
                                minute,
                                close_ist,
                            )
                            last_put_price = new_premium

                    else:
                        # === CONVERSION MODE ===
                        if triggered == "call":
                            hedge_strike = (
                                triggered_leg.strike - self.strike_increment
                            )
                        else:
                            hedge_strike = (
                                triggered_leg.strike + self.strike_increment
                            )

                        hedge_price = loader.get_price_at_time(
                            df,
                            trade_date,
                            expiry_date,
                            triggered,
                            hedge_strike,
                            minute,
                        )
                        if hedge_price is None:
                            continue

                        total_fees += fee
                        hedge_leg = HedgeLeg(
                            triggered, hedge_strike, float(hedge_price), qty
                        )
                        last_hedge_price = float(hedge_price)
                        hedge_prices = loader.get_minute_prices(
                            df,
                            trade_date,
                            expiry_date,
                            triggered,
                            hedge_strike,
                            minute,
                            close_ist,
                        )

                        other_pnl = (
                            (other_leg.entry_premium - other_curr) * qty * cs
                        )
                        realized_pnl += other_pnl
                        total_fees += fee

                        new_other_target = triggered_curr / 2.0
                        try:
                            new_other_strike, new_other_premium = (
                                loader.find_strike_by_premium(
                                    df,
                                    trade_date,
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
                            realized_pnl -= other_pnl
                            total_fees -= 2.0 * fee
                            continue

                        total_fees += fee

                        adjustment_log.append(
                            AdjustmentEvent(
                                minute=minute,
                                adj_type="CONVERSION_ENTER",
                                triggered_leg=triggered,
                                old_strike=other_leg.strike,
                                new_strike=new_other_strike,
                                trigger_pct_reached=trigger_pct_reached,
                                old_premium=other_curr,
                                new_premium=new_other_premium,
                                other_leg_premium=triggered_curr,
                                net_pnl_at_adj=net_mtm,
                            ).__dict__
                        )

                        other_leg.strike = new_other_strike
                        other_leg.entry_premium = new_other_premium
                        other_leg.baseline_premium = new_other_premium

                        in_conversion_mode = True
                        total_conversions += 1
                        total_adjustments += 1

                        if triggered == "call":
                            put_prices = loader.get_minute_prices(
                                df,
                                trade_date,
                                expiry_date,
                                "put",
                                new_other_strike,
                                minute,
                                close_ist,
                            )
                            last_put_price = new_other_premium
                        else:
                            call_prices = loader.get_minute_prices(
                                df,
                                trade_date,
                                expiry_date,
                                "call",
                                new_other_strike,
                                minute,
                                close_ist,
                            )
                            last_call_price = new_other_premium
        else:
            # Loop exhausted without break
            exit_reason = "PRE_EXPIRY"
            exit_ist = close_ist
            # Realize at last known prices if still open
            if call_leg.is_open or put_leg.is_open or (
                hedge_leg is not None and hedge_leg.is_open
            ):
                cpx = last_call_price if last_call_price is not None else 0.0
                ppx = last_put_price if last_put_price is not None else 0.0
                exit_fees, realized_pnl = self._close_all(
                    call_leg,
                    put_leg,
                    hedge_leg,
                    cpx,
                    ppx,
                    df,
                    close_ist,
                    trade_date,
                    expiry_date,
                    realized_pnl,
                )
                total_fees += exit_fees
                # Recompute gross from realized (positions closed)
                gross_mtm = realized_pnl

        final_gross = gross_mtm
        final_slippage = abs(final_gross) * self.slippage_pct / 100.0
        final_net = final_gross - total_fees - final_slippage

        return DayResult(
            trade_date=trade_date,
            expiry_date=expiry_date,
            trade_type=self.trade_type,
            entry_ist=entry_ist,
            call_strike=float(call_strike),
            put_strike=float(put_strike),
            entry_call_premium=float(entry_call_premium),
            entry_put_premium=float(entry_put_premium),
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


if __name__ == "__main__":
    import os
    import sys
    from datetime import date

    sys.path.insert(0, os.getcwd())
    from backtest.data_loader import DataLoader

    csv_path = (
        sys.argv[1] if len(sys.argv) > 1 else "backtest/data/BTC_2026-06.csv"
    )

    print("Loading data...")
    loader = DataLoader()
    df = loader.load_csv(csv_path)

    sim = StrategySimulator(
        trade_type="straddle",
        expiry_type="1DTE",
        trigger_pct=150.0,
        profit_target_pct=50.0,
        stoploss_pct=100.0,
        min_replacement_premium=150.0,
        conversion_equality_pct=10.0,
        quantity=100,
        fee_per_leg_usd=0.75,
        slippage_pct=2.0,
    )

    print("Simulating June 1, 2026...")
    result = sim.simulate_day(df, date(2026, 6, 1))

    print("\n=== JUNE 1 RESULT ===")
    print(f"Data OK: {result.data_ok}")
    if result.data_ok:
        print(f"Trade: {result.trade_type} | Expiry: {result.expiry_date}")
        print(
            f"Entry: Call ${result.call_strike} @ ${result.entry_call_premium:.0f} | "
            f"Put ${result.put_strike} @ ${result.entry_put_premium:.0f}"
        )
        print(f"Initial premium: ${result.initial_premium:.0f}")
        print(
            f"Target: ${result.profit_target_usd:.2f} | SL: ${result.stoploss_usd:.2f}"
        )
        print(f"Exit: {result.exit_reason} at {result.exit_ist}")
        print(
            f"Adjustments: {result.total_adjustments} "
            f"(conversions: {result.total_conversions}, "
            f"reversals: {result.total_reversals})"
        )
        print(f"Gross P&L: ${result.gross_pnl:.2f}")
        print(f"Fees:      ${result.total_fees:.2f}")
        print(f"Net P&L:   ${result.net_pnl:.2f}")
        print(f"Max DD:    ${result.max_drawdown:.2f}")
        print("\nAdjustment log:")
        for adj in result.adjustment_log:
            print(
                f"  {adj['minute'].strftime('%H:%M')} IST | "
                f"{adj['adj_type']:20s} | "
                f"{adj['triggered_leg'].upper()} | "
                f"Strike: {adj['old_strike']:.0f}→{adj['new_strike']:.0f} | "
                f"Premium: {adj['old_premium']:.0f}→{adj['new_premium']:.0f} | "
                f"Trigger: {adj['trigger_pct_reached']:.0f}% | "
                f"Net MTM: ${adj['net_pnl_at_adj']:.2f}"
            )
    else:
        print(f"Notes: {result.notes}")
