"""S005 Tent Strategy — ATM straddle + BE strangle + earlier-DTE long protection."""

from __future__ import annotations

import logging
import math
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent.parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

from backtest.harness.config import (  # noqa: E402
    CONTRACT_VALUE,
    EXPIRY_HOUR_IST,
    EXPIRY_MINUTE_IST,
    SKIP_EXPIRIES,
)
from backtest.harness.costs import (  # noqa: E402
    ensure_slip_table,
    fill_price,
    option_fee,
    qty_btc,
)
from backtest.harness.data import (  # noqa: E402
    IST,
    UTC,
    MarksStore,
    find_spot_csv,
    ist_dt,
    load_chain,
    load_spot_map,
    mark_ohlc_at,
    resolve_forward,
    resolve_mark_ts,
    to_unix,
)
from backtest.harness.metrics import (  # noqa: E402
    day_clustered_ci_daily,
    lots_at_risk_cap,
    max_drawdown,
)
from backtest.harness.models import (  # noqa: E402
    Action,
    CycleResult,
    Leg,
    PositionState,
    SkipAccount,
    StrategyMeta,
)

logger = logging.getLogger("strategies.s005")

COOLDOWN_SEC = 2 * 3600
TIME_CUTOFF_HOUR = 17
TIME_CUTOFF_MINUTE = 25
ENTRY_START_HOUR = 9
ENTRY_START_MINUTE = 0
MONITOR_STEP = 60
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260919


def default_params() -> dict[str, Any]:
    return {
        "short_dte": 1,
        "long_dte": 0,
        "qty_straddle": 10,
        "qty_strangle": 20,
        "target_pct": 10.0,
        "sl_mult": 3.0,
        "slip_model": "bucketed",
        "slip_mult": 1.0,
        "arm": "s1l0_q10_20_tp10_sl3",
    }


@dataclass
class TentLeg:
    symbol: str
    strike: float
    opt_type: str
    side: str  # buy|sell
    qty: int
    entry_mark: float
    entry_fill: float
    entry_fee: float
    entry_slip: float
    series: dict[int, float] = field(default_factory=dict)


@dataclass
class BasketBuild:
    legs: list[TentLeg]
    net_credit: float
    target_usd: float
    stop_usd: float
    short_expiry: date
    long_expiry: date
    atm: float
    be_call: float
    be_put: float
    fees_entry: float
    slip_cost_entry: float


class S005TentStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = default_params()
        if params:
            self.params.update(params)
        self.skips = SkipAccount()
        self.cooldown_skips = 0

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            id="S005",
            name="S005 Tent Strategy",
            version="1.0.0",
            description=(
                "ATM short straddle + BE short strangle + earlier-DTE long "
                "protection; continuous; TP/SL/time cutoff; no adjustments."
            ),
            status="TESTING",
        )

    def entry_times(self, day: date) -> list[datetime]:
        # Continuous runner owns schedule; harness day-loop unused.
        return [ist_dt(day, ENTRY_START_HOUR, ENTRY_START_MINUTE)]

    def build(self, ctx: Any, t: datetime) -> list[Leg] | None:
        return None

    def manage(self, ctx: Any, state: PositionState, t: datetime) -> Action | None:
        return Action(kind="hold")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _nearest_strike(self, strikes: list[float], target: float) -> float | None:
        if not strikes:
            return None
        return min(strikes, key=lambda k: (abs(k - target), k))

    def _strike_step(self, strikes: list[float]) -> float:
        if len(strikes) < 2:
            return 500.0
        diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
        diffs = [d for d in diffs if d > 0]
        return float(statistics.median(diffs)) if diffs else 500.0

    def _by_strike(self, chain: list[dict[str, Any]]) -> dict[float, dict[str, Any]]:
        return {float(r["strike"]): r for r in chain}

    def _preload(
        self, store: MarksStore, symbol: str, t0: int, t1: int
    ) -> dict[int, float]:
        out: dict[int, float] = {}
        d0 = datetime.fromtimestamp(t0, tz=UTC).astimezone(IST).date()
        d1 = datetime.fromtimestamp(t1, tz=UTC).astimezone(IST).date()
        day = d0
        while day <= d1:
            conn = store.conn(day)
            if conn is not None:
                rows = conn.execute(
                    "SELECT ts, close FROM marks WHERE symbol=? AND ts BETWEEN ? AND ?",
                    (symbol, t0 - 120, t1 + 120),
                ).fetchall()
                for ts, c in rows:
                    if c is not None and float(c) > 0:
                        out[int(ts)] = float(c)
            day += timedelta(days=1)
        return out

    def _mark_at(self, series: dict[int, float], ts: int) -> float | None:
        minute = (ts // 60) * 60
        if minute in series:
            return series[minute]
        for d in range(-60, 61, 60):
            if minute + d in series:
                return series[minute + d]
        return None

    def _can_enter(self, now_ts: int, long_expiry: date) -> bool:
        cutoff = to_unix(ist_dt(long_expiry, TIME_CUTOFF_HOUR, TIME_CUTOFF_MINUTE))
        return now_ts < cutoff

    def _pick_atm_straddle(
        self,
        calls: list[dict[str, Any]],
        puts: list[dict[str, Any]],
        spot: float,
    ) -> tuple[float, dict[str, Any], dict[str, Any]] | None:
        c_by = self._by_strike(calls)
        p_by = self._by_strike(puts)
        common = sorted(set(c_by) & set(p_by))
        if not common:
            return None
        # Prefer nearest to spot; among near candidates minimize |c-p|
        near = sorted(common, key=lambda k: abs(k - spot))[:7]
        best = None
        for k in near:
            c, p = c_by[k], p_by[k]
            cm, pm = float(c["mark_price"]), float(p["mark_price"])
            if cm <= 0 or pm <= 0:
                continue
            score = (abs(k - spot), abs(cm - pm))
            if best is None or score < best[0]:
                best = (score, k, c, p)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _pick_long_pair(
        self,
        calls: list[dict[str, Any]],
        puts: list[dict[str, Any]],
        call_k: float,
        put_k: float,
        step: float,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        c_by = self._by_strike(calls)
        p_by = self._by_strike(puts)
        # try exact, then one step OTM
        call_cands = [call_k, call_k + step]
        put_cands = [put_k, put_k - step]
        best = None
        for ck in call_cands:
            for pk in put_cands:
                # snap to listed
                ck2 = self._nearest_strike(sorted(c_by), ck)
                pk2 = self._nearest_strike(sorted(p_by), pk)
                if ck2 is None or pk2 is None:
                    continue
                if ck2 <= pk2:
                    continue
                cr, pr = c_by.get(ck2), p_by.get(pk2)
                if cr is None or pr is None:
                    continue
                cm, pm = float(cr["mark_price"]), float(pr["mark_price"])
                if cm <= 0 or pm <= 0:
                    continue
                score = (abs(ck2 - call_k) + abs(pk2 - put_k), abs(cm - pm))
                if best is None or score < best[0]:
                    best = (score, cr, pr)
        if best is None:
            return None
        return best[1], best[2]

    def build_basket(
        self,
        store: MarksStore,
        spot_map: dict[int, float],
        *,
        day: date,
        entry_ts: int,
    ) -> tuple[BasketBuild | None, str | None]:
        p = self.params
        short_dte = int(p["short_dte"])
        long_dte = int(p["long_dte"])
        short_exp = day + timedelta(days=short_dte)
        long_exp = day + timedelta(days=long_dte)
        if short_exp in SKIP_EXPIRIES or long_exp in SKIP_EXPIRIES:
            return None, "skipped_expiry_blocklist"
        if not self._can_enter(entry_ts, long_exp):
            return None, "skipped_no_time_before_cutoff"

        conn = store.conn(day)
        if conn is None:
            return None, "skipped_no_mark"

        cts_s = resolve_mark_ts(conn, short_exp, entry_ts)
        if cts_s is None:
            # try expiry month conn
            conn_s = store.conn(short_exp) or conn
            cts_s = resolve_mark_ts(conn_s, short_exp, entry_ts)
            conn_use_s = conn_s
        else:
            conn_use_s = conn
        if cts_s is None:
            return None, "skipped_no_mark"

        spot, _ = resolve_forward(store, spot_map, short_exp, entry_ts)
        if spot is None or spot <= 0:
            return None, "skipped_no_spot"

        calls_s = load_chain(conn_use_s, short_exp, cts_s, "call")
        puts_s = load_chain(conn_use_s, short_exp, cts_s, "put")
        if not calls_s or not puts_s:
            return None, "skipped_no_chain"

        atm_pick = self._pick_atm_straddle(calls_s, puts_s, float(spot))
        if atm_pick is None:
            return None, "skipped_no_strike"
        atm_k, atm_c, atm_p = atm_pick
        straddle_prem = float(atm_c["mark_price"]) + float(atm_p["mark_price"])
        be_up = atm_k + straddle_prem
        be_dn = atm_k - straddle_prem

        all_k = sorted(
            {float(r["strike"]) for r in calls_s}
            | {float(r["strike"]) for r in puts_s}
        )
        step = self._strike_step(all_k)
        c_by = self._by_strike(calls_s)
        p_by = self._by_strike(puts_s)
        be_call_k = self._nearest_strike([k for k in all_k if k in c_by], be_up)
        be_put_k = self._nearest_strike([k for k in all_k if k in p_by], be_dn)
        if be_call_k is None or be_put_k is None:
            return None, "skipped_no_strike"
        if be_call_k <= atm_k or be_put_k >= atm_k:
            # force at least one step OTM from ATM
            be_call_k = self._nearest_strike(
                [k for k in all_k if k > atm_k and k in c_by], atm_k + step
            )
            be_put_k = self._nearest_strike(
                [k for k in all_k if k < atm_k and k in p_by], atm_k - step
            )
        if be_call_k is None or be_put_k is None:
            return None, "skipped_no_strike"
        be_c_row, be_p_row = c_by[be_call_k], p_by[be_put_k]

        # Long protection chain
        conn_l = store.conn(long_exp) or store.conn(day)
        if conn_l is None:
            return None, "skipped_no_protection"
        cts_l = resolve_mark_ts(conn_l, long_exp, entry_ts)
        if cts_l is None:
            return None, "skipped_no_protection"
        calls_l = load_chain(conn_l, long_exp, cts_l, "call")
        puts_l = load_chain(conn_l, long_exp, cts_l, "put")
        if not calls_l or not puts_l:
            return None, "skipped_no_protection"
        long_pair = self._pick_long_pair(
            calls_l, puts_l, be_call_k, be_put_k, step
        )
        if long_pair is None:
            return None, "skipped_no_protection"
        long_c, long_p = long_pair

        q_sd = int(p["qty_straddle"])
        q_sg = int(p["qty_strangle"])
        q_long = q_sd + q_sg
        dte_s = max(0, short_dte)
        dte_l = max(0, long_dte)
        slip_model = str(p.get("slip_model") or "bucketed")
        slip_mult = float(p.get("slip_mult") or 1.0)

        def make_short(row: dict[str, Any], qty: int, dte: int) -> TentLeg:
            m = float(row["mark_price"])
            fill, sf = fill_price(
                m, "sell", dte=dte, slip_model=slip_model, slip_mult=slip_mult
            )
            fee = option_fee(fill, float(spot), qty)
            return TentLeg(
                symbol=str(row["symbol"]),
                strike=float(row["strike"]),
                opt_type=str(row["option_type"]),
                side="sell",
                qty=qty,
                entry_mark=m,
                entry_fill=fill,
                entry_fee=fee,
                entry_slip=sf * 100.0,
            )

        def make_long(row: dict[str, Any], qty: int, dte: int) -> TentLeg:
            m = float(row["mark_price"])
            fill, sf = fill_price(
                m, "buy", dte=dte, slip_model=slip_model, slip_mult=slip_mult
            )
            fee = option_fee(fill, float(spot), qty)
            return TentLeg(
                symbol=str(row["symbol"]),
                strike=float(row["strike"]),
                opt_type=str(row["option_type"]),
                side="buy",
                qty=qty,
                entry_mark=m,
                entry_fill=fill,
                entry_fee=fee,
                entry_slip=sf * 100.0,
            )

        legs = [
            make_short(atm_c, q_sd, dte_s),
            make_short(atm_p, q_sd, dte_s),
            make_short(be_c_row, q_sg, dte_s),
            make_short(be_p_row, q_sg, dte_s),
            make_long(long_c, q_long, dte_l),
            make_long(long_p, q_long, dte_l),
        ]

        short_credit = sum(
            leg.entry_fill * qty_btc(leg.qty) for leg in legs if leg.side == "sell"
        )
        long_debit = sum(
            leg.entry_fill * qty_btc(leg.qty) for leg in legs if leg.side == "buy"
        )
        fees = sum(leg.entry_fee for leg in legs)
        slip_cost = sum(
            abs(leg.entry_fill - leg.entry_mark) * qty_btc(leg.qty) for leg in legs
        )
        net_credit = short_credit - long_debit
        # lock targets on NET_CREDIT before fees (spec); MTM subtracts fees
        tp_pct = float(p["target_pct"]) / 100.0
        sl_mult = float(p["sl_mult"])
        target_usd = max(0.0, net_credit * tp_pct)
        stop_usd = -(sl_mult * tp_pct * net_credit)

        cutoff_ts = to_unix(ist_dt(long_exp, TIME_CUTOFF_HOUR, TIME_CUTOFF_MINUTE))
        for leg in legs:
            leg.series = self._preload(store, leg.symbol, entry_ts, cutoff_ts + 120)

        return (
            BasketBuild(
                legs=legs,
                net_credit=net_credit,
                target_usd=target_usd,
                stop_usd=stop_usd,
                short_expiry=short_exp,
                long_expiry=long_exp,
                atm=atm_k,
                be_call=be_call_k,
                be_put=be_put_k,
                fees_entry=fees,
                slip_cost_entry=slip_cost,
            ),
            None,
        )

    def _net_mtm(self, basket: BasketBuild, ts: int, spot: float) -> float | None:
        mtm = 0.0
        for leg in basket.legs:
            m = self._mark_at(leg.series, ts)
            if m is None:
                return None
            if leg.side == "sell":
                mtm += (leg.entry_fill - m) * qty_btc(leg.qty)
            else:
                mtm += (m - leg.entry_fill) * qty_btc(leg.qty)
        return mtm - basket.fees_entry

    def _flatten(
        self,
        basket: BasketBuild,
        exit_ts: int,
        spot: float,
        reason: str,
        entry_ts: int,
        entry_day: date,
        worst_mtm: float,
    ) -> CycleResult:
        p = self.params
        slip_model = str(p.get("slip_model") or "bucketed")
        slip_mult = float(p.get("slip_mult") or 1.0)
        realized = 0.0
        fees = basket.fees_entry
        slip_cost = basket.slip_cost_entry
        for leg in basket.legs:
            m = self._mark_at(leg.series, exit_ts) or leg.entry_mark
            dte = max(
                0,
                int(
                    (
                        to_unix(
                            ist_dt(
                                basket.short_expiry
                                if leg.side == "sell"
                                else basket.long_expiry,
                                EXPIRY_HOUR_IST,
                                EXPIRY_MINUTE_IST,
                            )
                        )
                        - exit_ts
                    )
                    / 86400
                ),
            )
            if leg.side == "sell":
                # buy to close
                fill, sf = fill_price(
                    m, "buy", dte=dte, slip_model=slip_model, slip_mult=slip_mult
                )
                realized += (leg.entry_fill - fill) * qty_btc(leg.qty)
            else:
                fill, sf = fill_price(
                    m, "sell", dte=dte, slip_model=slip_model, slip_mult=slip_mult
                )
                realized += (fill - leg.entry_fill) * qty_btc(leg.qty)
            fee = option_fee(fill, spot, leg.qty)
            fees += fee
            slip_cost += abs(fill - m) * qty_btc(leg.qty)

        return CycleResult(
            strategy_id="S005",
            entry_date=entry_day,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            exit_reason=reason,
            hold_hours=max(0.0, (exit_ts - entry_ts) / 3600.0),
            gross_pnl=realized,
            fees=fees,
            slippage_cost=slip_cost,
            net_pnl=realized - fees,
            worst_mtm=worst_mtm,
            n_adjustments=0,
            arm=str(p.get("arm") or ""),
            meta={
                "net_credit": basket.net_credit,
                "target_usd": basket.target_usd,
                "stop_usd": basket.stop_usd,
                "atm": basket.atm,
                "be_call": basket.be_call,
                "be_put": basket.be_put,
            },
        )

    def monitor_basket(
        self,
        store: MarksStore,
        spot_map: dict[int, float],
        basket: BasketBuild,
        entry_ts: int,
        entry_day: date,
    ) -> CycleResult:
        cutoff = to_unix(
            ist_dt(basket.long_expiry, TIME_CUTOFF_HOUR, TIME_CUTOFF_MINUTE)
        )
        worst = 0.0
        exit_reason = "TIME_CUTOFF"
        exit_ts = cutoff
        ts = entry_ts + MONITOR_STEP
        spot = float(
            resolve_forward(store, spot_map, basket.short_expiry, entry_ts)[0] or 0
        )

        while ts <= cutoff:
            fwd, _ = resolve_forward(store, spot_map, basket.short_expiry, ts)
            if fwd is not None and fwd > 0:
                spot = float(fwd)
            mtm = self._net_mtm(basket, ts, spot)
            if mtm is None:
                ts += MONITOR_STEP
                continue
            worst = min(worst, mtm)
            if mtm >= basket.target_usd:
                exit_reason = "TARGET"
                exit_ts = ts
                break
            if mtm <= basket.stop_usd:
                exit_reason = "STOPLOSS"
                exit_ts = ts
                break
            ts += MONITOR_STEP
        else:
            exit_reason = "TIME_CUTOFF"
            exit_ts = cutoff
            mtm = self._net_mtm(basket, exit_ts, spot)
            if mtm is not None:
                worst = min(worst, mtm)

        return self._flatten(
            basket, exit_ts, spot, exit_reason, entry_ts, entry_day, worst
        )

    def run_window(
        self,
        d0: date,
        d1: date,
        *,
        store: MarksStore | None = None,
        spot_map: dict[int, float] | None = None,
    ) -> tuple[list[CycleResult], SkipAccount, dict[str, Any]]:
        ensure_slip_table()
        own_store = store is None
        if store is None:
            store = MarksStore()
        if spot_map is None:
            path = find_spot_csv()
            if path is None:
                raise FileNotFoundError("No BTCUSD_1m CSV")
            spot_map = load_spot_map(path)

        self.skips = SkipAccount(days_in_window=max(0, (d1 - d0).days + 1))
        self.cooldown_skips = 0
        cycles: list[CycleResult] = []

        now_ts = to_unix(ist_dt(d0, ENTRY_START_HOUR, ENTRY_START_MINUTE))
        end_ts = to_unix(ist_dt(d1, 23, 59))
        cooldown_until = 0

        while now_ts <= end_ts:
            day = datetime.fromtimestamp(now_ts, tz=UTC).astimezone(IST).date()
            if day > d1:
                break

            if now_ts < cooldown_until:
                # count skipped entry opportunities hourly while in cooldown
                self.cooldown_skips += 1
                now_ts = min(cooldown_until, now_ts + 3600)
                continue

            long_dte = int(self.params["long_dte"])
            long_exp = day + timedelta(days=long_dte)
            if not self._can_enter(now_ts, long_exp):
                # jump to next day start
                nxt = day + timedelta(days=1)
                now_ts = to_unix(ist_dt(nxt, ENTRY_START_HOUR, ENTRY_START_MINUTE))
                continue

            basket, skip = self.build_basket(
                store, spot_map, day=day, entry_ts=now_ts
            )
            if basket is None:
                self.skips.record(skip or "skipped_other", day)
                # Avoid minute-spinning on empty books
                now_ts += 5 * MONITOR_STEP
                continue

            cyc = self.monitor_basket(store, spot_map, basket, now_ts, day)
            cyc.arm = str(self.params.get("arm") or "")
            cycles.append(cyc)
            self.skips.cycles_entered += 1
            logger.info(
                "S005 %s exit=%s net=%.4f hold=%.2fh",
                day,
                cyc.exit_reason,
                cyc.net_pnl,
                cyc.hold_hours,
            )

            if cyc.exit_reason == "STOPLOSS":
                cooldown_until = cyc.exit_ts + COOLDOWN_SEC
                now_ts = cyc.exit_ts + MONITOR_STEP
            elif cyc.exit_reason == "TARGET":
                now_ts = cyc.exit_ts + MONITOR_STEP
            else:
                # TIME_CUTOFF — next day
                nxt = day + timedelta(days=1)
                now_ts = to_unix(ist_dt(nxt, ENTRY_START_HOUR, ENTRY_START_MINUTE))

        if own_store:
            store.close()

        stats = summarize_s005(
            cycles, d0, d1, self.skips, self.cooldown_skips, self.params
        )
        return cycles, self.skips, stats


def summarize_s005(
    cycles: list[CycleResult],
    d0: date,
    d1: date,
    skips: SkipAccount,
    cooldown_skips: int,
    params: dict[str, Any],
) -> dict[str, Any]:
    n = len(cycles)
    day_span = max(1, (d1 - d0).days + 1)
    by_day: dict[date, float] = {}
    for c in cycles:
        by_day[c.entry_date] = by_day.get(c.entry_date, 0.0) + c.net_pnl
    daily = [by_day.get(d0 + timedelta(days=i), 0.0) for i in range(day_span)]
    mean_day = float(statistics.fmean(daily)) if daily else float("nan")
    mean_p, lo, hi = day_clustered_ci_daily(daily, BOOTSTRAP_N, BOOTSTRAP_SEED)

    wins = sum(1 for c in cycles if c.net_pnl > 0)
    reasons: dict[str, int] = {}
    for c in cycles:
        reasons[c.exit_reason] = reasons.get(c.exit_reason, 0) + 1
    mix = {k: (100.0 * v / n if n else float("nan")) for k, v in sorted(reasons.items())}
    holds = [c.hold_hours for c in cycles]
    worst = min(cycles, key=lambda c: c.net_pnl) if cycles else None

    # designed max loss from SL (median absolute stop)
    stops = [abs(float((c.meta or {}).get("stop_usd") or 0)) for c in cycles]
    max_loss = float(statistics.median(stops)) if stops else float("nan")
    units = lots_at_risk_cap(max_loss, 100.0, 3.0) if max_loss == max_loss else 0
    daily_net_at_size = (
        mean_day * units if mean_day == mean_day else float("nan")
    )
    daily_pct = (
        100.0 * daily_net_at_size / 100.0 if daily_net_at_size == daily_net_at_size else float("nan")
    )

    return {
        "n_baskets": n,
        "baskets_per_day": n / day_span,
        "win_pct": (100.0 * wins / n) if n else float("nan"),
        "mean_gross": float(statistics.fmean([c.gross_pnl for c in cycles]))
        if cycles
        else float("nan"),
        "mean_fees": float(statistics.fmean([c.fees for c in cycles]))
        if cycles
        else float("nan"),
        "mean_slippage": float(statistics.fmean([c.slippage_cost for c in cycles]))
        if cycles
        else float("nan"),
        "mean_net": float(statistics.fmean([c.net_pnl for c in cycles]))
        if cycles
        else float("nan"),
        "mean_day": mean_day,
        "ci_lo": lo,
        "ci_hi": hi,
        "bootstrap_mean": mean_p,
        "worst_net": float(worst.net_pnl) if worst else float("nan"),
        "worst_date": worst.entry_date.isoformat() if worst else "",
        "max_dd": max_drawdown(daily) if daily else float("nan"),
        "hold_med": float(statistics.median(holds)) if holds else float("nan"),
        "exit_mix": mix,
        "cooldown_entry_skips": cooldown_skips,
        "skips": {
            "days_in_window": skips.days_in_window,
            "cycles_entered": skips.cycles_entered,
            "counts": dict(skips.counts),
            "examples": dict(skips.examples),
        },
        "max_loss_per_basket": max_loss,
        "basket_units_at_3pct": units,
        "daily_net_pct_of_capital_at_size": daily_pct,
        "params": dict(params),
        "n_cycles": n,  # alias for registry
    }
