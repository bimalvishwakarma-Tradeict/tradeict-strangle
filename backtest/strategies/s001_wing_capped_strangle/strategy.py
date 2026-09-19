"""S001 Wing-Capped Strangle — harness port via s001_mark_engine.simulate_cycle."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent.parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s001_mark_engine as me  # noqa: E402

from backtest.harness.data import MarketContext, ist_dt
from backtest.harness.models import Action, CycleResult, Leg, PositionState, StrategyMeta

logger = logging.getLogger("strategies.s001")

# Regression target (DESIGN / IS arm fixed_tp25_slip1.0)
REGRESSION_WINDOW = (date(2025, 7, 1), date(2026, 9, 13))
REGRESSION_ARM = "fixed_tp25_slip1"
REGRESSION_N = 144
REGRESSION_MEAN_DAY = -0.0718
REGRESSION_TOL = 0.0005


def default_s001_params() -> dict[str, Any]:
    return {
        "dec_pct": 40.0,
        "adj_b_trigger": 70.0,
        "adj_mode": "B_only",
        "hedge": "off",
        "profit_mode": "pct_of_credit",
        "tp_pct": 25.0,
        "profit_k": 1.0,
        "wing_points": 2000.0,
        "wing_roll": True,
        "qty_lots": 8,
        "entry_hour": 11,
        "entry_minute": 0,
        "dte": 2,
        "premium_mode": "fixed",
        "target_premium_per_side": 150.0,
        "premium_pct_of_hedge": 25.0,
        "max_adj": 2,
        "slip_model": "bucketed",
        "slip_mult": 1.0,
        "arm": "fixed_tp25_slip1",
    }


class S001WingCappedStrangle:
    """
    Harness strategy. Complex cycle path uses s001_mark_engine.simulate_cycle
    so results stay numerically aligned with the baseline script.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = default_s001_params()
        if params:
            self.params.update(params)

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            id="S001",
            name="S001 Wing-Capped Strangle",
            version="1.0.0-harness",
            description=(
                "Short strangle + wings + Adj B, wing-capped. "
                "Harness port of backtest/s001_mark_engine.py."
            ),
            status="CLOSED",
        )

    def entry_times(self, day: date) -> list[datetime]:
        h = int(self.params.get("entry_hour", 11))
        m = int(self.params.get("entry_minute", 0))
        return [ist_dt(day, h, m)]

    def build(self, ctx: MarketContext, t: datetime) -> list[Leg] | None:
        """Entry selection only — full fills handled in run_cycle."""
        # Marker so generic path knows to prefer run_cycle
        ctx.skip_reason = "use_run_cycle"
        return None

    def manage(
        self, ctx: MarketContext, state: PositionState, t: datetime
    ) -> Action | None:
        return Action(kind="hold")

    def run_cycle(self, ctx: MarketContext, entry_ts: int) -> CycleResult | None:
        cfg = dict(self.params)
        cfg["slip_mult"] = float(ctx.params.get("slip_mult", cfg.get("slip_mult", 1.0)))
        cfg["slip_model"] = str(ctx.params.get("slip_model", cfg.get("slip_model")))
        cfg["from_date"] = ctx.day
        cfg["to_date"] = ctx.day
        cfg["window"] = str(ctx.params.get("window") or "")

        cyc, skip = me.simulate_cycle(
            ctx.store,
            ctx.spot_map,
            day=ctx.day,
            entry_ts=entry_ts,
            expiry=ctx.expiry,
            cfg=cfg,
            slip_model=str(cfg["slip_model"]),
        )
        if cyc is None:
            ctx.skip_reason = skip or "skipped_other"
            return None

        return CycleResult(
            strategy_id="S001",
            entry_date=cyc.entry_date,
            entry_ts=cyc.entry_ts,
            exit_ts=cyc.exit_ts,
            exit_reason=cyc.exit_reason,
            hold_hours=cyc.hold_hours,
            gross_pnl=cyc.gross_pnl,
            fees=cyc.fees,
            slippage_cost=cyc.slippage_cost,
            net_pnl=cyc.net_pnl,
            worst_mtm=cyc.worst_mtm,
            n_adjustments=cyc.n_adjustments,
            arm=str(cfg.get("arm") or "fixed_tp25_slip1"),
            window=str(cfg.get("window") or ""),
            meta={
                "premium_mode": cyc.premium_mode,
                "target_premium": cyc.target_premium,
                "profit_mode": cyc.profit_mode,
                "profit_target_usd": cyc.profit_target_usd,
                "call_strike": cyc.call_strike,
                "put_strike": cyc.put_strike,
            },
        )


def check_regression(stats: dict[str, Any]) -> tuple[bool, str]:
    """
    Compare harness DESIGN metrics to known s001_mark_engine baseline.
    Returns (ok, message). On mismatch message starts with REGRESSION FAIL.
    """
    n = int(stats.get("n_cycles") or 0)
    mean_day = float(stats.get("mean_day") or float("nan"))
    if n != REGRESSION_N:
        return (
            False,
            f"REGRESSION FAIL n_cycles={n} expected={REGRESSION_N} "
            f"(arm={REGRESSION_ARM} window={REGRESSION_WINDOW[0]}..{REGRESSION_WINDOW[1]})",
        )
    if abs(mean_day - REGRESSION_MEAN_DAY) > REGRESSION_TOL:
        return (
            False,
            f"REGRESSION FAIL mean_day={mean_day:.6f} expected={REGRESSION_MEAN_DAY} "
            f"tol={REGRESSION_TOL}",
        )
    return True, f"REGRESSION OK n={n} mean_day={mean_day:.6f}"
