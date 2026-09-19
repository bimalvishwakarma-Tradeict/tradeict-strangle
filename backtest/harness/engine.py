"""Harness engine — minute loop + optional strategy.run_cycle override."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from backtest.harness.config import (
    EXPIRY_HOUR_IST,
    EXPIRY_MINUTE_IST,
    HarnessConfig,
)
from backtest.harness.costs import ensure_slip_table
from backtest.harness.data import (
    IST,
    UTC,
    MarketContext,
    MarksStore,
    find_spot_csv,
    ist_dt,
    load_spot_map,
    resolve_forward,
    to_unix,
)
from backtest.harness.metrics import summarize_cycles
from backtest.harness.models import (
    Action,
    CycleResult,
    Leg,
    PositionState,
    SkipAccount,
    StrategyMeta,
)

logger = logging.getLogger("harness.engine")


class Strategy(Protocol):
    def meta(self) -> StrategyMeta: ...

    def entry_times(self, day: date) -> list[datetime]: ...

    def build(self, ctx: MarketContext, t: datetime) -> list[Leg] | None: ...

    def manage(
        self, ctx: MarketContext, state: PositionState, t: datetime
    ) -> Action | None: ...


class HarnessEngine:
    """
    Runs a strategy across a date window.

    If strategy implements run_cycle(ctx, entry_ts) -> CycleResult | None,
    that path is used (for complex ports like S001). Otherwise build+manage
    minute loop is used.
    """

    def __init__(self, strategy: Any, cfg: HarnessConfig) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.skips = SkipAccount()

    def run(self) -> tuple[list[CycleResult], SkipAccount, dict[str, Any]]:
        if self.cfg.slip_model == "bucketed":
            ensure_slip_table()

        spot_path = find_spot_csv()
        if spot_path is None:
            raise FileNotFoundError("No BTCUSD_1m_*.csv")
        spot_map = load_spot_map(spot_path)
        store = MarksStore()
        meta = self.strategy.meta()
        cycles: list[CycleResult] = []
        d0, d1 = self.cfg.from_date, self.cfg.to_date
        self.skips = SkipAccount(days_in_window=max(0, (d1 - d0).days + 1))

        day = d0
        while day <= d1:
            for entry_dt in self.strategy.entry_times(day):
                entry_ts = to_unix(entry_dt)
                dte = int(self.cfg.strategy_params.get("dte", 2))
                expiry = day + timedelta(days=dte)
                if expiry in self.cfg.skip_expiries:
                    self.skips.record("skipped_expiry_blocklist", day)
                    continue

                spot, src = resolve_forward(store, spot_map, expiry, entry_ts)
                if spot is None or spot <= 0:
                    self.skips.record("skipped_no_spot", day)
                    continue

                ctx = MarketContext(
                    store=store,
                    spot_map=spot_map,
                    day=day,
                    expiry=expiry,
                    spot=float(spot),
                    params=dict(self.cfg.strategy_params),
                )
                ctx.params["slip_mult"] = self.cfg.slip_mult
                ctx.params["slip_model"] = self.cfg.slip_model
                ctx.params["window"] = self.cfg.window_tag
                ctx.params["arm"] = self.cfg.strategy_params.get("arm", "")

                cyc: CycleResult | None = None
                if hasattr(self.strategy, "run_cycle"):
                    cyc = self.strategy.run_cycle(ctx, entry_ts)
                    if cyc is None:
                        reason = getattr(ctx, "skip_reason", None) or "skipped_other"
                        self.skips.record(str(reason), day)
                        continue
                else:
                    cyc = self._run_build_manage(ctx, entry_dt, entry_ts)
                    if cyc is None:
                        reason = getattr(ctx, "skip_reason", None) or "skipped_build"
                        self.skips.record(str(reason), day)
                        continue

                cyc.strategy_id = meta.id
                cyc.window = self.cfg.window_tag
                if not cyc.arm:
                    cyc.arm = str(self.cfg.strategy_params.get("arm", meta.id))
                cycles.append(cyc)
                self.skips.cycles_entered += 1
                logger.info(
                    "day=%s exit=%s net=%.4f",
                    day,
                    cyc.exit_reason,
                    cyc.net_pnl,
                )
            day += timedelta(days=1)

        store.close()
        stats = summarize_cycles(
            cycles,
            d0,
            d1,
            bootstrap_n=self.cfg.bootstrap_n,
            bootstrap_seed=self.cfg.bootstrap_seed,
        )
        stats["skips"] = {
            "days_in_window": self.skips.days_in_window,
            "cycles_entered": self.skips.cycles_entered,
            "counts": dict(self.skips.counts),
            "examples": dict(self.skips.examples),
        }
        stats["strategy_id"] = meta.id
        stats["stage"] = self.cfg.stage
        return cycles, self.skips, stats

    def _run_build_manage(
        self, ctx: MarketContext, entry_dt: datetime, entry_ts: int
    ) -> CycleResult | None:
        legs = self.strategy.build(ctx, entry_dt)
        if not legs:
            return None
        state = PositionState(
            entry_ts=entry_ts,
            entry_date=ctx.day,
            legs=list(legs),
        )
        exp_ts = to_unix(
            ist_dt(ctx.expiry, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST)
        )
        exit_reason = "EXPIRY"
        exit_ts = exp_ts
        ts = entry_ts + 60
        while ts <= exp_ts:
            t = datetime.fromtimestamp(ts, tz=UTC).astimezone(IST)
            action = self.strategy.manage(ctx, state, t) or Action(kind="hold")
            if action.kind == "exit":
                exit_reason = action.reason or "EXIT"
                exit_ts = ts
                break
            if action.kind == "adjust":
                state.n_adjustments += 1
                # strategy mutates state via action meta / legs
                for leg in action.legs_to_open:
                    state.legs.append(leg)
            ts += 60

        gross = float(state.realized)
        fees = float(state.fees)
        slip = float(state.slippage_cost)
        return CycleResult(
            strategy_id="",
            entry_date=ctx.day,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            exit_reason=exit_reason,
            hold_hours=max(0.0, (exit_ts - entry_ts) / 3600.0),
            gross_pnl=gross,
            fees=fees,
            slippage_cost=slip,
            net_pnl=gross - fees,
            worst_mtm=float(state.worst_mtm),
            n_adjustments=state.n_adjustments,
            meta=dict(state.meta),
        )
