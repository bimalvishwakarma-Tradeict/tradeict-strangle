# s002_sim.py — S002 0DTE long strangle simulator
#
# Standalone backtest helper. Do NOT import from backend/.
# Does NOT modify strategy_sim.py / basket_sim.py (those are S001).

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

try:
    from backtest.fees_sim import OPTIONS_CONTRACT_VALUE, estimate_option_fee
except ImportError:
    from fees_sim import OPTIONS_CONTRACT_VALUE, estimate_option_fee


def _as_date(value: date | datetime | pd.Timestamp | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = str(value or "").strip()
    if not raw:
        return default
    parts = raw.split(":")
    if len(parts) != 2:
        return default
    return int(parts[0]), int(parts[1])


@dataclass
class S002Trade:
    trade_date: date
    entry_ist: datetime
    spot: float
    atm_strike: float
    call_strike: float
    put_strike: float
    call_mid: float
    put_mid: float
    diff: float
    call_ask: float
    put_ask: float
    lots: int
    capital_used: float
    entry_fees: float
    exit_ist: Optional[datetime]
    exit_reason: str
    call_bid_exit: float
    put_bid_exit: float
    gross_pnl: float
    net_pnl: float
    exit_fees: float
    spread_cost_entry: float
    spread_cost_exit: float
    minutes_held: int
    max_favourable: float
    max_adverse: float
    # Zero-cost twin (mid fills, zero fees)
    gross_pnl_zc: float = 0.0
    net_pnl_zc: float = 0.0
    capital_used_zc: float = 0.0
    target_usd: float = 0.0
    stoploss_usd: float = 0.0


@dataclass
class S002DayResult:
    trade_date: date
    data_ok: bool
    trades: list[S002Trade] = field(default_factory=list)
    no_entry_reason: Optional[str] = None
    min_diff_seen: Optional[float] = None
    min_diff_call_prem: Optional[float] = None
    min_diff_put_prem: Optional[float] = None
    min_max_premium_seen: Optional[float] = None
    notes: str = ""


class S002Simulator:
    """Scan and trade 0DTE long strangles with ask entry / bid exit."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.starting_capital = float(cfg.get("starting_capital", 10_000.0))
        self.capital_pct = float(cfg.get("capital_pct", 10.0))
        sh, sm = _parse_hhmm(str(cfg.get("scan_start_time_ist", "12:00")), (12, 0))
        ch, cm = _parse_hhmm(str(cfg.get("entry_cutoff_time_ist", "15:30")), (15, 30))
        self.scan_start = (sh, sm)
        self.entry_cutoff = (ch, cm)
        self.strike_offset_mode = str(
            cfg.get("strike_offset_mode", "strikes")
        ).lower().strip()
        self.strike_offset_value = float(cfg.get("strike_offset_value", 1))
        self.entry_diff_mode = str(
            cfg.get("entry_diff_mode", "absolute")
        ).lower().strip()
        self.entry_max_diff_usd = float(cfg.get("entry_max_diff_usd", 10.0))
        self.entry_max_premium = float(cfg.get("entry_max_premium", 70.0))
        self.quote_max_age_seconds = float(cfg.get("quote_max_age_seconds", 60.0))
        self.target_pct = float(cfg.get("target_pct_of_capital_used", 40.0))
        self.sl_multiplier = float(cfg.get("sl_multiplier", 0.5))
        self.sl_max_pct = float(cfg.get("sl_max_pct_of_capital_used", 50.0))
        self.max_trades_per_day = int(cfg.get("max_trades_per_day", 3))
        self.min_lots = int(cfg.get("min_lots", 1))
        self.trail_enabled = bool(cfg.get("trail_enabled", False))
        self.trail_activate_pct = float(
            cfg.get("trail_activate_at_pct_of_target", 100.0)
        )
        self.retrace_pct = float(cfg.get("retrace_pct", 0.30))
        self.retrace_pct_late = float(cfg.get("retrace_pct_late", 0.20))
        th, tm = _parse_hhmm(
            str(cfg.get("trail_tighten_after_time_ist", "16:00")), (16, 0)
        )
        self.trail_tighten_after = (th, tm)
        self.pre_expiry_close_enabled = bool(
            cfg.get("pre_expiry_close_enabled", False)
        )
        self.settlement_fee_enabled = bool(
            cfg.get("settlement_fee_enabled", False)
        )
        # Primary path is always real fills; zero-cost twin always computed.
        self.zero_cost_primary = bool(cfg.get("zero_cost", False))
        # Expiry 17:30 IST = 12:00 UTC
        self.expiry_h, self.expiry_m = 17, 30
        # Pre-expiry window: 15 minutes before expiry (S001 convention)
        self.pre_expiry_h, self.pre_expiry_m = 17, 15

    def _allocated(self) -> float:
        return self.starting_capital * self.capital_pct / 100.0

    def _offset_points(self, strike_step: float) -> float:
        if self.strike_offset_mode in ("points", "point", "usd"):
            return float(self.strike_offset_value)
        # default: N strikes
        return float(self.strike_offset_value) * float(strike_step)

    def _nearest_strike(self, strikes: np.ndarray, spot: float) -> float:
        idx = int(np.argmin(np.abs(strikes - spot)))
        return float(strikes[idx])

    def _pick_wing(
        self,
        strikes: np.ndarray,
        atm: float,
        *,
        side: str,
        offset_pts: float,
    ) -> float | None:
        """Nearest listed strike at/above ATM+offset (call) or at/below ATM-offset (put)."""
        if side == "call":
            target = atm + offset_pts
            cands = strikes[strikes >= target - 1e-9]
            if len(cands) == 0:
                return None
            return float(cands[np.argmin(np.abs(cands - target))])
        target = atm - offset_pts
        cands = strikes[strikes <= target + 1e-9]
        if len(cands) == 0:
            return None
        return float(cands[np.argmin(np.abs(cands - target))])

    def _build_day_panels(
        self,
        day: pd.DataFrame,
        minutes: pd.DatetimeIndex,
        strikes: np.ndarray,
    ) -> dict[str, Any]:
        """
        Per-minute bid/ask/mid/age panels for call and put across all strikes.

        Shape of each price panel: DataFrame index=minute, columns=strike.
        """
        work = day.copy()
        role = work["buyer_role"].astype(str).str.lower().str.strip()
        work["_role"] = role
        work["_minute"] = work["ist_time"].dt.floor("min")
        strike_cols = [float(s) for s in strikes]

        def panel(opt: str, role_name: str) -> tuple[pd.DataFrame, pd.DataFrame]:
            sub = work.loc[
                (work["opt_type"].astype(str).str.lower() == opt)
                & (work["_role"] == role_name)
            ]
            if sub.empty:
                empty_px = pd.DataFrame(index=minutes, columns=strike_cols, dtype=float)
                empty_ts = pd.DataFrame(index=minutes, columns=strike_cols)
                return empty_px, empty_ts
            g = (
                sub.groupby(["_minute", "strike"], sort=True)
                .agg(price=("price", "last"), ts=("ist_time", "last"))
            )
            px = g["price"].unstack("strike")
            ts = g["ts"].unstack("strike")
            px = px.reindex(index=minutes, columns=strike_cols).ffill()
            ts = ts.reindex(index=minutes, columns=strike_cols).ffill()
            return px, ts

        call_bid, call_bid_ts = panel("call", "maker")
        call_ask, call_ask_ts = panel("call", "taker")
        put_bid, put_bid_ts = panel("put", "maker")
        put_ask, put_ask_ts = panel("put", "taker")

        call_mid = (call_bid + call_ask) / 2.0
        put_mid = (put_bid + put_ask) / 2.0

        # Age vs floored minute (clip same-minute negatives to 0)
        minute_series = pd.Series(minutes, index=minutes)

        def ages(ts_panel: pd.DataFrame) -> pd.DataFrame:
            out = pd.DataFrame(index=minutes, columns=strike_cols, dtype=float)
            for col in strike_cols:
                if col not in ts_panel.columns:
                    continue
                delta = (minute_series - ts_panel[col]).dt.total_seconds()
                out[col] = delta.clip(lower=0)
            return out

        return {
            "call_bid": call_bid,
            "call_ask": call_ask,
            "call_mid": call_mid,
            "put_bid": put_bid,
            "put_ask": put_ask,
            "put_mid": put_mid,
            "call_bid_age": ages(call_bid_ts),
            "call_ask_age": ages(call_ask_ts),
            "put_bid_age": ages(put_bid_ts),
            "put_ask_age": ages(put_ask_ts),
        }

    def _cell(
        self,
        panel: pd.DataFrame,
        minute: pd.Timestamp,
        strike: float,
    ) -> float | None:
        try:
            v = panel.at[minute, float(strike)]
        except (KeyError, TypeError, ValueError):
            return None
        if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
            return None
        return float(v)

    def _spot_and_atm(
        self,
        panels: dict[str, Any],
        minute: pd.Timestamp,
        strikes: np.ndarray,
    ) -> tuple[float | None, float | None]:
        """Median put-call parity spot and nearest ATM strike for one minute."""
        if minute not in panels["call_mid"].index:
            return None, None
        spots: list[float] = []
        for k in strikes:
            c = self._cell(panels["call_mid"], minute, float(k))
            p = self._cell(panels["put_mid"], minute, float(k))
            if c is None or p is None:
                continue
            spots.append(c - p + float(k))
        if not spots:
            return None, None
        spot = float(np.median(spots))
        atm = self._nearest_strike(strikes, spot)
        return spot, atm

    def _quote_ok(
        self,
        panels: dict[str, Any],
        minute: pd.Timestamp,
        call_k: float,
        put_k: float,
    ) -> bool:
        for name, k in (
            ("call_bid", call_k),
            ("call_ask", call_k),
            ("put_bid", put_k),
            ("put_ask", put_k),
        ):
            if self._cell(panels[name], minute, k) is None:
                return False
        for name, k in (
            ("call_bid_age", call_k),
            ("call_ask_age", call_k),
            ("put_bid_age", put_k),
            ("put_ask_age", put_k),
        ):
            a = self._cell(panels[name], minute, k)
            if a is None or float(a) > self.quote_max_age_seconds:
                return False
        return True

    def _leg_px(
        self,
        panels: dict[str, Any],
        minute: pd.Timestamp,
        name: str,
        strike: float,
    ) -> float | None:
        return self._cell(panels[name], minute, strike)

    def simulate_day(self, df: pd.DataFrame, trade_date: date) -> S002DayResult:
        trade_date = _as_date(trade_date)
        expiry = trade_date  # 0DTE

        # Filter 0DTE prints for this calendar day
        ist = df["ist_date"].astype(object).map(_as_date)
        exp = df["expiry_date"].astype(object).map(_as_date)
        day = df.loc[(ist == trade_date) & (exp == expiry)].copy()
        if day.empty or "buyer_role" not in day.columns:
            return S002DayResult(
                trade_date=trade_date,
                data_ok=False,
                no_entry_reason="NO_QUOTES",
                notes="no 0DTE rows",
            )

        if not day["ist_time"].is_monotonic_increasing:
            day = day.sort_values("ist_time")

        strikes = np.array(sorted({float(s) for s in day["strike"].unique()}))
        if len(strikes) < 3:
            return S002DayResult(
                trade_date=trade_date,
                data_ok=False,
                no_entry_reason="NO_QUOTES",
                notes="too few strikes",
            )
        diffs = np.diff(strikes)
        strike_step = float(np.median(diffs)) if len(diffs) else 200.0
        if strike_step <= 0:
            strike_step = 200.0
        offset_pts = self._offset_points(strike_step)

        scan_start = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.scan_start[0],
            self.scan_start[1],
            0,
        )
        entry_cutoff = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.entry_cutoff[0],
            self.entry_cutoff[1],
            0,
        )
        expiry_ist = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.expiry_h,
            self.expiry_m,
            0,
        )
        pre_expiry_ist = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.pre_expiry_h,
            self.pre_expiry_m,
            0,
        )
        trail_tighten_ist = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.trail_tighten_after[0],
            self.trail_tighten_after[1],
            0,
        )

        minutes = pd.date_range(
            start=scan_start,
            end=expiry_ist,
            freq="1min",
        )
        # Diagnostic / entry-condition scan is ONLY this window (not post-cutoff).
        # Full `minutes` still runs to expiry for hold + settlement.
        panels = self._build_day_panels(day, minutes, strikes)

        # Precompute spot/ATM series (full day — needed for hold/settlement)
        spot_by_min: dict[pd.Timestamp, float] = {}
        atm_by_min: dict[pd.Timestamp, float] = {}
        for m in minutes:
            spot, atm = self._spot_and_atm(panels, m, strikes)
            if spot is not None and atm is not None:
                spot_by_min[m] = spot
                atm_by_min[m] = atm

        # Diagnostics accumulators — SCAN WINDOW ONLY (12:00–cutoff)
        min_diff_seen: float | None = None
        min_diff_call: float | None = None
        min_diff_put: float | None = None
        min_max_prem: float | None = None
        saw_any_quotes = False
        saw_fresh_quotes = False
        saw_diff_ok = False
        saw_prem_ok = False
        saw_all_before_cutoff = False
        saw_lots_zero = False
        # Post-cutoff: only used to decide CUTOFF_PASSED (numbers stay window mins)
        conditions_only_after_cutoff = False

        trades: list[S002Trade] = []
        i = 0
        n = len(minutes)

        while i < n and len(trades) < self.max_trades_per_day:
            m = minutes[i]
            # Settlement minute — cannot open new here
            if m >= expiry_ist:
                break

            in_entry_window = scan_start <= m <= entry_cutoff
            spot = spot_by_min.get(m)
            atm = atm_by_min.get(m)
            if spot is None or atm is None:
                i += 1
                continue

            call_k = self._pick_wing(
                strikes, atm, side="call", offset_pts=offset_pts
            )
            put_k = self._pick_wing(
                strikes, atm, side="put", offset_pts=offset_pts
            )
            if call_k is None or put_k is None:
                i += 1
                continue

            c_mid = self._leg_px(panels, m, "call_mid", call_k)
            p_mid = self._leg_px(panels, m, "put_mid", put_k)
            c_ask = self._leg_px(panels, m, "call_ask", call_k)
            p_ask = self._leg_px(panels, m, "put_ask", put_k)
            if None in (c_mid, p_mid, c_ask, p_ask):
                i += 1
                continue

            diff = abs(float(c_mid) - float(p_mid))
            max_side = max(float(c_mid), float(p_mid))
            fresh = self._quote_ok(panels, m, call_k, put_k)
            diff_ok = diff < self.entry_max_diff_usd
            prem_ok = (
                float(c_mid) < self.entry_max_premium
                and float(p_mid) < self.entry_max_premium
            )

            # --- Diagnostics + no-entry flags: SCAN WINDOW ONLY ---
            if in_entry_window:
                saw_any_quotes = True
                if min_diff_seen is None or diff < min_diff_seen:
                    min_diff_seen = diff
                    min_diff_call = float(c_mid)
                    min_diff_put = float(p_mid)
                if min_max_prem is None or max_side < min_max_prem:
                    min_max_prem = max_side
                if fresh:
                    saw_fresh_quotes = True
                if diff_ok:
                    saw_diff_ok = True
                if prem_ok:
                    saw_prem_ok = True

            # Post-cutoff: detect that entry would have been possible later
            if (
                (not in_entry_window)
                and fresh
                and diff_ok
                and prem_ok
                and m > entry_cutoff
            ):
                conditions_only_after_cutoff = True

            can_enter = (
                in_entry_window
                and fresh
                and diff_ok
                and prem_ok
            )
            if not can_enter:
                i += 1
                continue

            if in_entry_window and fresh and diff_ok and prem_ok:
                saw_all_before_cutoff = True

            # --- ENTRY ---
            allocated = self._allocated()
            cost_per_lot = (float(c_ask) + float(p_ask)) * OPTIONS_CONTRACT_VALUE
            if cost_per_lot <= 0:
                i += 1
                continue
            lots = int(math.floor(allocated / cost_per_lot))
            if lots < self.min_lots:
                saw_lots_zero = True
                i += 1
                continue

            print(
                f"[S002_ENTRY] {trade_date} {m} | "
                f"C={call_k:.0f} P={put_k:.0f} | "
                f"ask={float(c_ask):.2f}/{float(p_ask):.2f} | "
                f"cost_per_lot={cost_per_lot:.6f} | lots={lots} | "
                f"capital_used="
                f"{(float(c_ask) + float(p_ask)) * OPTIONS_CONTRACT_VALUE * lots:.2f}"
            )
            if lots > 100_000:
                print(
                    f"WARNING: lots={lots} exceeds 100000 on {trade_date} "
                    f"at {m} (cost_per_lot={cost_per_lot:.8f}, "
                    f"allocated={allocated:.2f}) — no cap applied, log only"
                )

            capital_used = (float(c_ask) + float(p_ask)) * OPTIONS_CONTRACT_VALUE * lots
            capital_used_zc = (
                (float(c_mid) + float(p_mid)) * OPTIONS_CONTRACT_VALUE * lots
            )
            entry_fees = estimate_option_fee(
                premium=float(c_ask), qty_lots=lots, btc_index=float(spot)
            ) + estimate_option_fee(
                premium=float(p_ask), qty_lots=lots, btc_index=float(spot)
            )
            spread_entry = (
                ((float(c_ask) - float(c_mid)) + (float(p_ask) - float(p_mid)))
                * OPTIONS_CONTRACT_VALUE
                * lots
            )

            target = capital_used * self.target_pct / 100.0
            stoploss = min(
                target * self.sl_multiplier,
                capital_used * self.sl_max_pct / 100.0,
            )

            # HOLD from next minute
            exit_ist: datetime | None = None
            exit_reason = "SETTLEMENT"
            call_bid_exit = float("nan")
            put_bid_exit = float("nan")
            call_mid_exit = float(c_mid)
            put_mid_exit = float(p_mid)
            exit_fees = 0.0
            spread_exit = 0.0
            gross = 0.0
            net = 0.0
            gross_zc = 0.0
            net_zc = 0.0
            max_fav = 0.0
            max_adv = 0.0
            peak_net = 0.0
            trail_armed = False
            minutes_held = 0

            j = i + 1
            while j < n:
                mj = minutes[j]
                minutes_held = int((mj - m).total_seconds() // 60)
                spot_j = spot_by_min.get(mj, spot)

                # Settlement at expiry
                if mj >= expiry_ist:
                    # last 5 minutes median spot before expiry
                    settle_mins = [
                        mm
                        for mm in minutes
                        if (expiry_ist - timedelta(minutes=5)) <= mm < expiry_ist
                        and mm in spot_by_min
                    ]
                    if settle_mins:
                        s_settle = float(
                            np.median([spot_by_min[mm] for mm in settle_mins])
                        )
                    else:
                        s_settle = float(spot_j if spot_j is not None else spot)

                    call_val = max(0.0, s_settle - float(call_k))
                    put_val = max(0.0, float(put_k) - s_settle)
                    payout = (call_val + put_val) * OPTIONS_CONTRACT_VALUE * lots
                    gross = payout - capital_used
                    gross_zc = (
                        (call_val + put_val) * OPTIONS_CONTRACT_VALUE * lots
                        - capital_used_zc
                    )
                    if self.settlement_fee_enabled:
                        exit_fees = estimate_option_fee(
                            premium=call_val, qty_lots=lots, btc_index=s_settle
                        ) + estimate_option_fee(
                            premium=put_val, qty_lots=lots, btc_index=s_settle
                        )
                    else:
                        exit_fees = 0.0
                    net = gross - entry_fees - exit_fees
                    net_zc = gross_zc  # zero fees
                    call_bid_exit = call_val
                    put_bid_exit = put_val
                    call_mid_exit = call_val
                    put_mid_exit = put_val
                    spread_exit = 0.0
                    exit_ist = expiry_ist
                    exit_reason = "SETTLEMENT"
                    max_fav = max(max_fav, net)
                    max_adv = min(max_adv, net)
                    break

                c_bid = self._leg_px(panels, mj, "call_bid", call_k)
                p_bid = self._leg_px(panels, mj, "put_bid", put_k)
                c_md = self._leg_px(panels, mj, "call_mid", call_k)
                p_md = self._leg_px(panels, mj, "put_mid", put_k)
                if None in (c_bid, p_bid):
                    j += 1
                    continue
                if c_md is None:
                    c_md = c_bid
                if p_md is None:
                    p_md = p_bid

                exit_value_gross = (
                    (float(c_bid) + float(p_bid)) * OPTIONS_CONTRACT_VALUE * lots
                )
                gross_mtm = exit_value_gross - capital_used
                est_exit_fees = estimate_option_fee(
                    premium=float(c_bid),
                    qty_lots=lots,
                    btc_index=float(spot_j if spot_j is not None else spot),
                ) + estimate_option_fee(
                    premium=float(p_bid),
                    qty_lots=lots,
                    btc_index=float(spot_j if spot_j is not None else spot),
                )
                net_mtm = gross_mtm - entry_fees - est_exit_fees

                exit_value_zc = (
                    (float(c_md) + float(p_md)) * OPTIONS_CONTRACT_VALUE * lots
                )
                gross_mtm_zc = exit_value_zc - capital_used_zc
                net_mtm_zc = gross_mtm_zc

                max_fav = max(max_fav, net_mtm)
                max_adv = min(max_adv, net_mtm)
                peak_net = max(peak_net, net_mtm)

                # Trailing (coded, default OFF — not used when trail_enabled=False)
                if self.trail_enabled:
                    activate_level = target * self.trail_activate_pct / 100.0
                    if net_mtm >= activate_level:
                        trail_armed = True
                    retrace = (
                        self.retrace_pct_late
                        if mj >= trail_tighten_ist
                        else self.retrace_pct
                    )
                    trail_floor = max(target, peak_net * (1.0 - retrace))
                    if trail_armed and net_mtm <= trail_floor:
                        exit_reason = "TRAIL"
                        exit_ist = mj.to_pydatetime()
                        call_bid_exit = float(c_bid)
                        put_bid_exit = float(p_bid)
                        call_mid_exit = float(c_md)
                        put_mid_exit = float(p_md)
                        exit_fees = est_exit_fees
                        spread_exit = (
                            ((float(c_md) - float(c_bid)) + (float(p_md) - float(p_bid)))
                            * OPTIONS_CONTRACT_VALUE
                            * lots
                        )
                        gross = gross_mtm
                        net = net_mtm
                        gross_zc = gross_mtm_zc
                        net_zc = net_mtm_zc
                        break

                # a) Target on NET
                if net_mtm >= target:
                    exit_reason = "TARGET"
                    exit_ist = mj.to_pydatetime()
                    call_bid_exit = float(c_bid)
                    put_bid_exit = float(p_bid)
                    call_mid_exit = float(c_md)
                    put_mid_exit = float(p_md)
                    exit_fees = est_exit_fees
                    spread_exit = (
                        ((float(c_md) - float(c_bid)) + (float(p_md) - float(p_bid)))
                        * OPTIONS_CONTRACT_VALUE
                        * lots
                    )
                    gross = gross_mtm
                    net = net_mtm
                    gross_zc = gross_mtm_zc
                    net_zc = net_mtm_zc
                    break

                # b) Stoploss on GROSS
                if gross_mtm <= -stoploss:
                    exit_reason = "STOPLOSS"
                    exit_ist = mj.to_pydatetime()
                    call_bid_exit = float(c_bid)
                    put_bid_exit = float(p_bid)
                    call_mid_exit = float(c_md)
                    put_mid_exit = float(p_md)
                    exit_fees = est_exit_fees
                    spread_exit = (
                        ((float(c_md) - float(c_bid)) + (float(p_md) - float(p_bid)))
                        * OPTIONS_CONTRACT_VALUE
                        * lots
                    )
                    gross = gross_mtm
                    net = net_mtm
                    gross_zc = gross_mtm_zc
                    net_zc = net_mtm_zc
                    break

                # c) Pre-expiry close
                if self.pre_expiry_close_enabled and mj >= pre_expiry_ist:
                    exit_reason = "PRE_EXPIRY"
                    exit_ist = mj.to_pydatetime()
                    call_bid_exit = float(c_bid)
                    put_bid_exit = float(p_bid)
                    call_mid_exit = float(c_md)
                    put_mid_exit = float(p_md)
                    exit_fees = est_exit_fees
                    spread_exit = (
                        ((float(c_md) - float(c_bid)) + (float(p_md) - float(p_bid)))
                        * OPTIONS_CONTRACT_VALUE
                        * lots
                    )
                    gross = gross_mtm
                    net = net_mtm
                    gross_zc = gross_mtm_zc
                    net_zc = net_mtm_zc
                    break

                j += 1
            else:
                # Exhausted minutes without hitting expiry bar — settle with last
                exit_reason = "SETTLEMENT"
                exit_ist = expiry_ist

            trade = S002Trade(
                trade_date=trade_date,
                entry_ist=m.to_pydatetime() if hasattr(m, "to_pydatetime") else m,
                spot=float(spot),
                atm_strike=float(atm),
                call_strike=float(call_k),
                put_strike=float(put_k),
                call_mid=float(c_mid),
                put_mid=float(p_mid),
                diff=float(diff),
                call_ask=float(c_ask),
                put_ask=float(p_ask),
                lots=int(lots),
                capital_used=float(capital_used),
                entry_fees=float(entry_fees),
                exit_ist=exit_ist,
                exit_reason=exit_reason,
                call_bid_exit=float(call_bid_exit)
                if not pd.isna(call_bid_exit)
                else 0.0,
                put_bid_exit=float(put_bid_exit)
                if not pd.isna(put_bid_exit)
                else 0.0,
                gross_pnl=float(gross),
                net_pnl=float(net),
                exit_fees=float(exit_fees),
                spread_cost_entry=float(spread_entry),
                spread_cost_exit=float(spread_exit),
                minutes_held=int(minutes_held),
                max_favourable=float(max_fav),
                max_adverse=float(max_adv),
                gross_pnl_zc=float(gross_zc),
                net_pnl_zc=float(net_zc),
                capital_used_zc=float(capital_used_zc),
                target_usd=float(target),
                stoploss_usd=float(stoploss),
            )
            trades.append(trade)

            # Resume scan after exit
            if exit_ist is None:
                break
            # Next minute after exit
            exit_floor = pd.Timestamp(exit_ist).floor("min")
            next_i = int(minutes.searchsorted(exit_floor, side="right"))
            i = max(next_i, j + 1 if j < n else n)
            continue

        no_entry_reason: str | None = None
        if not trades:
            # Reasons ranked from window-only observations.
            # CUTOFF_PASSED only if window never fully cleared but post-cutoff did.
            if not saw_any_quotes:
                no_entry_reason = "NO_QUOTES"
            elif not saw_diff_ok:
                no_entry_reason = "DIFF_NEVER_MET"
            elif not saw_prem_ok:
                no_entry_reason = "PREMIUM_NEVER_MET"
            elif not saw_fresh_quotes:
                no_entry_reason = "STALE_QUOTES"
            elif saw_lots_zero and not saw_all_before_cutoff:
                # Had quotes/diff/prem/fresh in window but lots never >= min
                no_entry_reason = "LOTS_ZERO"
            elif not saw_all_before_cutoff and conditions_only_after_cutoff:
                no_entry_reason = "CUTOFF_PASSED"
            elif saw_lots_zero:
                no_entry_reason = "LOTS_ZERO"
            else:
                if conditions_only_after_cutoff:
                    no_entry_reason = "CUTOFF_PASSED"
                elif not saw_diff_ok:
                    no_entry_reason = "DIFF_NEVER_MET"
                elif not saw_prem_ok:
                    no_entry_reason = "PREMIUM_NEVER_MET"
                else:
                    no_entry_reason = "STALE_QUOTES"

        return S002DayResult(
            trade_date=trade_date,
            data_ok=True,
            trades=trades,
            no_entry_reason=no_entry_reason,
            min_diff_seen=min_diff_seen,
            min_diff_call_prem=min_diff_call,
            min_diff_put_prem=min_diff_put,
            min_max_premium_seen=min_max_prem,
        )

    def write_debug_day_csv(
        self,
        df: pd.DataFrame,
        trade_date: date,
        out_csv: str | Path,
    ) -> Path:
        """
        Per-minute scan-window dump for one day (diagnosis only).

        Columns match the --debug-day contract. Does not change strategy rules.
        """
        trade_date = _as_date(trade_date)
        expiry = trade_date
        ist = df["ist_date"].astype(object).map(_as_date)
        exp = df["expiry_date"].astype(object).map(_as_date)
        day = df.loc[(ist == trade_date) & (exp == expiry)].copy()
        out_path = Path(out_csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if day.empty or "buyer_role" not in day.columns:
            pd.DataFrame(
                columns=[
                    "ist_minute",
                    "parity_spot",
                    "atm_strike",
                    "call_strike",
                    "call_bid",
                    "call_ask",
                    "call_mid",
                    "call_ask_age_s",
                    "put_strike",
                    "put_bid",
                    "put_ask",
                    "put_mid",
                    "put_ask_age_s",
                    "diff",
                    "cond_diff_ok",
                    "cond_premium_ok",
                    "cond_age_ok",
                    "cond_all_ok",
                ]
            ).to_csv(out_path, index=False)
            print(f"DEBUG day {trade_date}: no 0DTE rows — empty CSV {out_path}")
            return out_path

        if not day["ist_time"].is_monotonic_increasing:
            day = day.sort_values("ist_time")

        strikes = np.array(sorted({float(s) for s in day["strike"].unique()}))
        diffs = np.diff(strikes)
        strike_step = float(np.median(diffs)) if len(diffs) else 200.0
        if strike_step <= 0:
            strike_step = 200.0
        offset_pts = self._offset_points(strike_step)

        scan_start = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.scan_start[0],
            self.scan_start[1],
            0,
        )
        entry_cutoff = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.entry_cutoff[0],
            self.entry_cutoff[1],
            0,
        )
        expiry_ist = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            self.expiry_h,
            self.expiry_m,
            0,
        )
        # Panels need full day for ffill continuity into the window
        minutes_full = pd.date_range(start=scan_start, end=expiry_ist, freq="1min")
        scan_minutes = pd.date_range(start=scan_start, end=entry_cutoff, freq="1min")
        panels = self._build_day_panels(day, minutes_full, strikes)

        rows: list[dict[str, Any]] = []
        for m in scan_minutes:
            spot, atm = self._spot_and_atm(panels, m, strikes)
            call_k = (
                self._pick_wing(strikes, atm, side="call", offset_pts=offset_pts)
                if atm is not None
                else None
            )
            put_k = (
                self._pick_wing(strikes, atm, side="put", offset_pts=offset_pts)
                if atm is not None
                else None
            )
            c_bid = c_ask = c_mid = c_age = None
            p_bid = p_ask = p_mid = p_age = None
            diff = None
            cond_diff = cond_prem = cond_age = cond_all = False
            if call_k is not None and put_k is not None:
                c_bid = self._leg_px(panels, m, "call_bid", call_k)
                c_ask = self._leg_px(panels, m, "call_ask", call_k)
                c_mid = self._leg_px(panels, m, "call_mid", call_k)
                c_age = self._leg_px(panels, m, "call_ask_age", call_k)
                p_bid = self._leg_px(panels, m, "put_bid", put_k)
                p_ask = self._leg_px(panels, m, "put_ask", put_k)
                p_mid = self._leg_px(panels, m, "put_mid", put_k)
                p_age = self._leg_px(panels, m, "put_ask_age", put_k)
                if c_mid is not None and p_mid is not None:
                    diff = abs(float(c_mid) - float(p_mid))
                    cond_diff = diff < self.entry_max_diff_usd
                    cond_prem = (
                        float(c_mid) < self.entry_max_premium
                        and float(p_mid) < self.entry_max_premium
                    )
                cond_age = self._quote_ok(panels, m, call_k, put_k)
                cond_all = bool(cond_diff and cond_prem and cond_age)

            rows.append(
                {
                    "ist_minute": m.isoformat(sep=" ", timespec="minutes"),
                    "parity_spot": spot,
                    "atm_strike": atm,
                    "call_strike": call_k,
                    "call_bid": c_bid,
                    "call_ask": c_ask,
                    "call_mid": c_mid,
                    "call_ask_age_s": c_age,
                    "put_strike": put_k,
                    "put_bid": p_bid,
                    "put_ask": p_ask,
                    "put_mid": p_mid,
                    "put_ask_age_s": p_age,
                    "diff": diff,
                    "cond_diff_ok": cond_diff,
                    "cond_premium_ok": cond_prem,
                    "cond_age_ok": cond_age,
                    "cond_all_ok": cond_all,
                }
            )

        pd.DataFrame(rows).to_csv(out_path, index=False)
        print(
            f"DEBUG day {trade_date}: wrote {len(rows)} scan-window minutes "
            f"-> {out_path}"
        )
        return out_path


def s002_trade_to_dict(t: S002Trade) -> dict[str, Any]:
    d = asdict(t)
    d["trade_date"] = t.trade_date.isoformat()
    d["entry_ist"] = t.entry_ist.isoformat(sep=" ", timespec="minutes")
    d["exit_ist"] = (
        t.exit_ist.isoformat(sep=" ", timespec="minutes") if t.exit_ist else None
    )
    d["entry_hour"] = int(t.entry_ist.hour)
    for k in (
        "spot",
        "atm_strike",
        "call_strike",
        "put_strike",
        "call_mid",
        "put_mid",
        "diff",
        "call_ask",
        "put_ask",
        "capital_used",
        "entry_fees",
        "call_bid_exit",
        "put_bid_exit",
        "gross_pnl",
        "net_pnl",
        "exit_fees",
        "spread_cost_entry",
        "spread_cost_exit",
        "max_favourable",
        "max_adverse",
        "gross_pnl_zc",
        "net_pnl_zc",
        "capital_used_zc",
        "target_usd",
        "stoploss_usd",
    ):
        d[k] = round(float(d[k]), 4)
    return d


def s002_day_to_dict(day: S002DayResult) -> dict[str, Any]:
    return {
        "trade_date": day.trade_date.isoformat(),
        "data_ok": day.data_ok,
        "no_entry_reason": day.no_entry_reason,
        "min_diff_seen": (
            round(day.min_diff_seen, 4) if day.min_diff_seen is not None else None
        ),
        "min_diff_call_prem": (
            round(day.min_diff_call_prem, 4)
            if day.min_diff_call_prem is not None
            else None
        ),
        "min_diff_put_prem": (
            round(day.min_diff_put_prem, 4)
            if day.min_diff_put_prem is not None
            else None
        ),
        "min_max_premium_seen": (
            round(day.min_max_premium_seen, 4)
            if day.min_max_premium_seen is not None
            else None
        ),
        "n_trades": len(day.trades),
        "trades": [s002_trade_to_dict(t) for t in day.trades],
        "notes": day.notes,
    }


def compute_s002_summary(days: list[S002DayResult]) -> dict[str, Any]:
    trades: list[S002Trade] = []
    for d in days:
        trades.extend(d.trades)

    no_entry: dict[str, int] = {}
    for d in days:
        if d.data_ok and not d.trades and d.no_entry_reason:
            no_entry[d.no_entry_reason] = no_entry.get(d.no_entry_reason, 0) + 1

    if not trades:
        return {
            "strategy": "S002",
            "total_days": len(days),
            "data_ok_days": sum(1 for d in days if d.data_ok),
            "total_trades": 0,
            "win_rate": 0.0,
            "win_rate_zc": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "total_net_pnl": 0.0,
            "total_net_pnl_zc": 0.0,
            "total_fees": 0.0,
            "total_spread_cost": 0.0,
            "fees_pct_of_target": 0.0,
            "spread_pct_of_target": 0.0,
            "exit_counts": {},
            "entry_hour_counts": {},
            "no_entry_reasons": no_entry,
            "breakeven_win_rate": 0.0,
            "daily_dates": [],
            "daily_pnl": [],
            "daily_pnl_zc": [],
            "cumulative_pnl": [],
            "cumulative_pnl_zc": [],
            "settlement_fee_note": (
                "settlement_fee_enabled=false by default — Delta ITM "
                "settlement fee not verified"
            ),
        }

    nets = [t.net_pnl for t in trades]
    nets_zc = [t.net_pnl_zc for t in trades]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p <= 0]
    wins_zc = [p for p in nets_zc if p > 0]

    exit_counts: dict[str, int] = {}
    hour_counts: dict[str, int] = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
        h = f"{t.entry_ist.hour:02d}"
        hour_counts[h] = hour_counts.get(h, 0) + 1

    total_fees = sum(t.entry_fees + t.exit_fees for t in trades)
    total_spread = sum(t.spread_cost_entry + t.spread_cost_exit for t in trades)
    total_target = sum(t.target_usd for t in trades) or 1.0

    # Break-even win rate for payoff R:R from target vs stoploss
    # BE = SL / (TP + SL) using averages
    avg_tp = float(np.mean([t.target_usd for t in trades]))
    avg_sl = float(np.mean([t.stoploss_usd for t in trades]))
    be_wr = (
        100.0 * avg_sl / (avg_tp + avg_sl) if (avg_tp + avg_sl) > 0 else 0.0
    )

    # Daily aggregates
    by_day: dict[str, list[S002Trade]] = {}
    for t in trades:
        key = t.trade_date.isoformat()
        by_day.setdefault(key, []).append(t)
    daily_dates = sorted(by_day.keys())
    daily_pnl = [sum(x.net_pnl for x in by_day[d]) for d in daily_dates]
    daily_pnl_zc = [sum(x.net_pnl_zc for x in by_day[d]) for d in daily_dates]
    cum: list[float] = []
    cum_zc: list[float] = []
    r = 0.0
    rz = 0.0
    for p, pz in zip(daily_pnl, daily_pnl_zc):
        r += p
        rz += pz
        cum.append(r)
        cum_zc.append(rz)

    win_rate = 100.0 * len(wins) / len(trades)
    win_rate_zc = 100.0 * len(wins_zc) / len(trades)
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = float(np.mean(nets))

    return {
        "strategy": "S002",
        "total_days": len(days),
        "data_ok_days": sum(1 for d in days if d.data_ok),
        "days_with_trades": sum(1 for d in days if d.trades),
        "total_trades": len(trades),
        "win_rate": win_rate,
        "win_rate_zc": win_rate_zc,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "total_net_pnl": sum(nets),
        "total_net_pnl_zc": sum(nets_zc),
        "total_gross_pnl": sum(t.gross_pnl for t in trades),
        "total_gross_pnl_zc": sum(t.gross_pnl_zc for t in trades),
        "total_fees": total_fees,
        "total_spread_cost": total_spread,
        "fees_pct_of_target": 100.0 * total_fees / total_target,
        "spread_pct_of_target": 100.0 * total_spread / total_target,
        "exit_counts": exit_counts,
        "entry_hour_counts": hour_counts,
        "no_entry_reasons": no_entry,
        "breakeven_win_rate": be_wr,
        "daily_dates": daily_dates,
        "daily_pnl": daily_pnl,
        "daily_pnl_zc": daily_pnl_zc,
        "cumulative_pnl": cum,
        "cumulative_pnl_zc": cum_zc,
        "settlement_fee_note": (
            "settlement_fee_enabled=false by default — Delta ITM "
            "settlement fee not verified; report assumes no settlement fee "
            "unless flag is on"
        ),
    }
