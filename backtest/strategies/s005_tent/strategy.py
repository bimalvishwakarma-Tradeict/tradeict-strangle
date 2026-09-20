"""S005 Tent Strategy v2 â€” Absorption Stop + entry-drag / MAE / theta."""

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
    load_symbol_series,
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

DEFAULT_COOLDOWN_HOURS = 2.0
DEFAULT_CUTOFF_HOUR = 17
DEFAULT_CUTOFF_MINUTE = 25
DEFAULT_PROTECTION_EXPIRY = "calendar"
DEFAULT_PROTECTION_OFFSET = 0
DEFAULT_PROTECTION_RATIO = 1.0
DEFAULT_BE_MULT = 1.0
DEFAULT_EXIT_MODE = "credit_pct"
DEFAULT_TRIGGER_PCT = 150.0
DEFAULT_ABSORB_MIN = 0.6
DEFAULT_MAX_BASKET_LOSS: float | None = None
DEFAULT_NO_TARGET = False
ENTRY_START_HOUR = 9
ENTRY_START_MINUTE = 0
MONITOR_STEP = 60
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260919
IV_LO = 0.01
IV_HI = 5.0

COOLDOWN_SEC = int(DEFAULT_COOLDOWN_HOURS * 3600)
TIME_CUTOFF_HOUR = DEFAULT_CUTOFF_HOUR
TIME_CUTOFF_MINUTE = DEFAULT_CUTOFF_MINUTE

LEG_ROLES = (
    "atm_call",
    "atm_put",
    "be_call",
    "be_put",
    "prot_call",
    "prot_put",
)


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
        "protection_expiry": DEFAULT_PROTECTION_EXPIRY,
        "protection_offset": DEFAULT_PROTECTION_OFFSET,
        "protection_ratio": DEFAULT_PROTECTION_RATIO,
        "be_mult": DEFAULT_BE_MULT,
        "cutoff_hour": DEFAULT_CUTOFF_HOUR,
        "cutoff_minute": DEFAULT_CUTOFF_MINUTE,
        "cooldown_hours": DEFAULT_COOLDOWN_HOURS,
        "exit_mode": DEFAULT_EXIT_MODE,
        "trigger_pct": DEFAULT_TRIGGER_PCT,
        "absorb_min": DEFAULT_ABSORB_MIN,
        "max_basket_loss_usd": DEFAULT_MAX_BASKET_LOSS,
        "no_target": DEFAULT_NO_TARGET,
    }


def parse_cutoff_time(s: str) -> tuple[int, int]:
    parts = str(s).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad cutoff time: {s!r}")
    return int(parts[0]), int(parts[1])


def parse_max_basket_loss(s: str) -> float | None:
    t = str(s).strip().lower()
    if t in ("", "none", "nan", "null"):
        return None
    return float(t)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76_price(f: float, k: float, t: float, sigma: float, is_call: bool) -> float:
    if f <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return max(f - k, 0.0) if is_call else max(k - f, 0.0)
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    if is_call:
        return f * _norm_cdf(d1) - k * _norm_cdf(d2)
    return k * _norm_cdf(-d2) - f * _norm_cdf(-d1)


def implied_vol_bisection(
    premium: float, f: float, k: float, t: float, is_call: bool
) -> float | None:
    if premium <= 0 or f <= 0 or k <= 0 or t <= 1e-12:
        return None
    intrinsic = max(f - k, 0.0) if is_call else max(k - f, 0.0)
    if premium < intrinsic - 1e-6:
        return None
    lo, hi = IV_LO, IV_HI
    flo = black76_price(f, k, t, lo, is_call) - premium
    fhi = black76_price(f, k, t, hi, is_call) - premium
    if flo * fhi > 0:
        for _ in range(8):
            hi *= 1.5
            if hi > 20.0:
                break
            fhi = black76_price(f, k, t, hi, is_call) - premium
            if flo * fhi <= 0:
                break
        else:
            return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        fm = black76_price(f, k, t, mid, is_call) - premium
        if abs(fm) < 1e-4 or (hi - lo) < 1e-10:
            return mid
        if flo * fm <= 0:
            hi = mid
            fhi = fm
        else:
            lo = mid
            flo = fm
    return 0.5 * (lo + hi)


def black76_theta_per_day(
    f: float, k: float, t: float, sigma: float, is_call: bool
) -> float:
    if f <= 0 or k <= 0 or t <= 1e-12 or sigma <= 1e-12:
        return 0.0
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol_sqrt_t
    d_v_d_t = f * _norm_pdf(d1) * sigma / (2.0 * math.sqrt(t))
    _ = is_call
    return -d_v_d_t / 365.0


def _ts_ist_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


@dataclass
class TentLeg:
    role: str
    symbol: str
    strike: float
    opt_type: str
    side: str
    qty: int
    entry_mark: float
    entry_fill: float
    entry_fee: float
    entry_slip: float
    series: dict[int, float] = field(default_factory=dict)
    exit_mark: float = 0.0
    exit_fill: float = 0.0
    exit_fee: float = 0.0
    leg_pnl: float = 0.0


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
    prot_call: float
    prot_put: float
    fees_entry: float
    slip_cost_entry: float
    entry_drag: float
    entry_spot: float
    net_theta_entry: float
    qty_straddle: int
    qty_strangle: int
    qty_protection: int
    basket_id: int = 0
class S005TentStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = default_params()
        if params:
            self.params.update(params)
        self.skips = SkipAccount()
        self.cooldown_skips = 0
        self._basket_seq = 0

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            id="S005",
            name="S005 Tent Strategy",
            version="2.0.0",
            description=(
                "ATM short straddle + BE short strangle + long protection; "
                "credit_pct or absorption stop; entry-drag adjusted MTM; "
                "HARD_FLOOR + TIME_CUTOFF; no adjustments."
            ),
            status="TESTING",
        )

    def entry_times(self, day: date) -> list[datetime]:
        return [ist_dt(day, ENTRY_START_HOUR, ENTRY_START_MINUTE)]

    def build(self, ctx: Any, t: datetime) -> list[Leg] | None:
        return None

    def manage(self, ctx: Any, state: PositionState, t: datetime) -> Action | None:
        return Action(kind="hold")

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
        return load_symbol_series(store, symbol, t0, t1)

    def _mark_at(self, series: dict[int, float], ts: int) -> float | None:
        minute = (ts // 60) * 60
        if minute in series:
            return series[minute]
        for d in range(-60, 61, 60):
            if minute + d in series:
                return series[minute + d]
        return None

    def _cutoff_hm(self) -> tuple[int, int]:
        p = self.params
        return (
            int(p.get("cutoff_hour", DEFAULT_CUTOFF_HOUR)),
            int(p.get("cutoff_minute", DEFAULT_CUTOFF_MINUTE)),
        )

    def _can_enter(self, now_ts: int, long_expiry: date) -> bool:
        hh, mm = self._cutoff_hm()
        cutoff = to_unix(ist_dt(long_expiry, hh, mm))
        return now_ts < cutoff

    def _leg_by_role(self, basket: BasketBuild, role: str) -> TentLeg:
        for leg in basket.legs:
            if leg.role == role:
                return leg
        raise KeyError(role)

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
        offset: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        c_by = self._by_strike(calls)
        p_by = self._by_strike(puts)
        off = max(0, int(offset))
        call_cands = [call_k + off * step, call_k + (off + 1) * step]
        put_cands = [put_k - off * step, put_k - (off + 1) * step]
        best = None
        for ck in call_cands:
            for pk in put_cands:
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

    def _net_theta_usd(
        self,
        legs: list[TentLeg],
        *,
        spot: float,
        entry_ts: int,
        short_exp: date,
        long_exp: date,
    ) -> float:
        short_th = 0.0
        long_th = 0.0
        for leg in legs:
            exp = short_exp if leg.side == "sell" else long_exp
            exp_ts = to_unix(ist_dt(exp, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))
            t_years = max(0.0, (exp_ts - entry_ts) / (365.25 * 86400.0))
            is_call = leg.opt_type.lower().startswith("c")
            iv = implied_vol_bisection(leg.entry_mark, spot, leg.strike, t_years, is_call)
            if iv is None:
                continue
            th = black76_theta_per_day(spot, leg.strike, t_years, iv, is_call)
            th_usd = th * qty_btc(leg.qty)
            if leg.side == "sell":
                # short of decaying option → positive $ theta
                short_th += -th_usd
            else:
                # long option → usually negative $ theta
                long_th += -th_usd  # magnitude of long theta cost (positive)
        # shorts theta minus longs theta (both as positive USD/day magnitudes)
        return short_th - long_th
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
        pe = str(p.get("protection_expiry") or DEFAULT_PROTECTION_EXPIRY).lower()
        short_exp = day + timedelta(days=short_dte)
        if pe == "same":
            long_exp = short_exp
            long_dte_eff = short_dte
        else:
            long_exp = day + timedelta(days=long_dte)
            long_dte_eff = long_dte
        if short_exp in SKIP_EXPIRIES or long_exp in SKIP_EXPIRIES:
            return None, "skipped_expiry_blocklist"
        if not self._can_enter(entry_ts, long_exp):
            return None, "skipped_no_time_before_cutoff"

        conn = store.conn(day)
        if conn is None:
            return None, "skipped_no_mark"

        cts_s = resolve_mark_ts(conn, short_exp, entry_ts)
        if cts_s is None:
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
        be_mult = float(p.get("be_mult", DEFAULT_BE_MULT))
        be_up = atm_k + be_mult * straddle_prem
        be_dn = atm_k - be_mult * straddle_prem

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
            be_call_k = self._nearest_strike(
                [k for k in all_k if k > atm_k and k in c_by], atm_k + step
            )
            be_put_k = self._nearest_strike(
                [k for k in all_k if k < atm_k and k in p_by], atm_k - step
            )
        if be_call_k is None or be_put_k is None:
            return None, "skipped_no_strike"
        be_c_row, be_p_row = c_by[be_call_k], p_by[be_put_k]

        if pe == "same":
            calls_l, puts_l = calls_s, puts_s
        else:
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
        prot_off = int(p.get("protection_offset", DEFAULT_PROTECTION_OFFSET))
        long_pair = self._pick_long_pair(
            calls_l, puts_l, be_call_k, be_put_k, step, offset=prot_off
        )
        if long_pair is None:
            return None, "skipped_no_protection"
        long_c, long_p = long_pair

        q_sd = int(p["qty_straddle"])
        q_sg = int(p["qty_strangle"])
        prot_ratio = float(p.get("protection_ratio", DEFAULT_PROTECTION_RATIO))
        q_long = max(1, int(round(prot_ratio * (q_sd + q_sg))))
        dte_s = max(0, short_dte)
        dte_l = max(0, long_dte_eff)
        slip_model = str(p.get("slip_model") or "bucketed")
        slip_mult = float(p.get("slip_mult") or 1.0)

        def make_leg(
            role: str, row: dict[str, Any], qty: int, dte: int, side: str
        ) -> TentLeg:
            m = float(row["mark_price"])
            fill, sf = fill_price(
                m, side, dte=dte, slip_model=slip_model, slip_mult=slip_mult
            )
            fee = option_fee(fill, float(spot), qty)
            return TentLeg(
                role=role,
                symbol=str(row["symbol"]),
                strike=float(row["strike"]),
                opt_type=str(row["option_type"]),
                side=side,
                qty=qty,
                entry_mark=m,
                entry_fill=fill,
                entry_fee=fee,
                entry_slip=sf * 100.0,
            )

        legs = [
            make_leg("atm_call", atm_c, q_sd, dte_s, "sell"),
            make_leg("atm_put", atm_p, q_sd, dte_s, "sell"),
            make_leg("be_call", be_c_row, q_sg, dte_s, "sell"),
            make_leg("be_put", be_p_row, q_sg, dte_s, "sell"),
            make_leg("prot_call", long_c, q_long, dte_l, "buy"),
            make_leg("prot_put", long_p, q_long, dte_l, "buy"),
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
        entry_drag = -(slip_cost + fees)
        tp_pct = float(p["target_pct"]) / 100.0
        sl_mult = float(p["sl_mult"])
        target_usd = max(0.0, net_credit * tp_pct)
        stop_usd = -(sl_mult * tp_pct * net_credit)
        net_theta = self._net_theta_usd(
            legs,
            spot=float(spot),
            entry_ts=entry_ts,
            short_exp=short_exp,
            long_exp=long_exp,
        )

        hh, mm = self._cutoff_hm()
        cutoff_ts = to_unix(ist_dt(long_exp, hh, mm))
        for leg in legs:
            leg.series = self._preload(store, leg.symbol, entry_ts, cutoff_ts + 120)

        self._basket_seq += 1
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
                prot_call=float(long_c["strike"]),
                prot_put=float(long_p["strike"]),
                fees_entry=fees,
                slip_cost_entry=slip_cost,
                entry_drag=entry_drag,
                entry_spot=float(spot),
                net_theta_entry=net_theta,
                qty_straddle=q_sd,
                qty_strangle=q_sg,
                qty_protection=q_long,
                basket_id=self._basket_seq,
            ),
            None,
        )

    def _raw_mtm(self, basket: BasketBuild, ts: int) -> float | None:
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

    def _adj_mtm(self, basket: BasketBuild, ts: int) -> float | None:
        raw = self._raw_mtm(basket, ts)
        if raw is None:
            return None
        return raw - basket.entry_drag

    def _side_short_roles(self, side: str) -> tuple[str, str]:
        if side == "call":
            return "atm_call", "be_call"
        return "atm_put", "be_put"

    def _absorption_check(
        self, basket: BasketBuild, ts: int
    ) -> tuple[bool, dict[str, Any] | None]:
        p = self.params
        trigger_pct = float(p.get("trigger_pct", DEFAULT_TRIGGER_PCT))
        absorb_min = float(p.get("absorb_min", DEFAULT_ABSORB_MIN))

        def marks_sum(roles: tuple[str, str]) -> tuple[float, float] | None:
            now_s = 0.0
            ent_s = 0.0
            for role in roles:
                leg = self._leg_by_role(basket, role)
                m = self._mark_at(leg.series, ts)
                if m is None:
                    return None
                now_s += m
                ent_s += leg.entry_fill
            return now_s, ent_s

        call_sum = marks_sum(("atm_call", "be_call"))
        put_sum = marks_sum(("atm_put", "be_put"))
        if call_sum is None or put_sum is None:
            return False, None
        call_now, call_ent = call_sum
        put_now, put_ent = put_sum
        if call_ent <= 0 or put_ent <= 0:
            return False, None
        ratio_call = call_now / call_ent
        ratio_put = put_now / put_ent
        tested = "call" if ratio_call >= ratio_put else "put"
        tested_now, tested_ent = (
            (call_now, call_ent) if tested == "call" else (put_now, put_ent)
        )
        stress = tested_now / tested_ent * 100.0
        if stress < trigger_pct:
            return False, {
                "ts": ts,
                "tested_side": tested,
                "stress": stress,
                "triggered": False,
            }

        shorts_loss = 0.0
        for role in self._side_short_roles(tested):
            leg = self._leg_by_role(basket, role)
            m = self._mark_at(leg.series, ts)
            if m is None:
                return False, None
            shorts_loss += (leg.entry_fill - m) * qty_btc(leg.qty)

        prot_role = "prot_call" if tested == "call" else "prot_put"
        prot = self._leg_by_role(basket, prot_role)
        pm = self._mark_at(prot.series, ts)
        if pm is None:
            return False, None
        prot_gain = (pm - prot.entry_fill) * qty_btc(prot.qty)

        if shorts_loss < 0:
            a_ratio = (
                prot_gain / abs(shorts_loss) if abs(shorts_loss) > 1e-12 else 999.0
            )
        else:
            a_ratio = 999.0

        event = {
            "ts": ts,
            "ts_ist": _ts_ist_str(ts),
            "tested_side": tested,
            "stress": stress,
            "shorts_loss": shorts_loss,
            "prot_gain": prot_gain,
            "A": a_ratio,
            "triggered": True,
        }
        if a_ratio < absorb_min:
            return True, event
        return False, event
    def _flatten(
        self,
        basket: BasketBuild,
        exit_ts: int,
        spot: float,
        reason: str,
        entry_ts: int,
        entry_day: date,
        mae_usd: float,
        *,
        max_stress_pct: float,
        absorption_at_trigger: float | None,
        trigger_hit_ts: int | None,
        absorption_events: list[dict[str, Any]],
    ) -> CycleResult:
        p = self.params
        slip_model = str(p.get("slip_model") or "bucketed")
        slip_mult = float(p.get("slip_mult") or 1.0)
        realized = 0.0
        shorts_pnl = 0.0
        protection_pnl = 0.0
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
                fill, _sf = fill_price(
                    m, "buy", dte=dte, slip_model=slip_model, slip_mult=slip_mult
                )
                leg_pnl = (leg.entry_fill - fill) * qty_btc(leg.qty)
                shorts_pnl += leg_pnl
            else:
                fill, _sf = fill_price(
                    m, "sell", dte=dte, slip_model=slip_model, slip_mult=slip_mult
                )
                leg_pnl = (fill - leg.entry_fill) * qty_btc(leg.qty)
                protection_pnl += leg_pnl
            fee = option_fee(fill, spot, leg.qty)
            fees += fee
            slip_cost += abs(fill - m) * qty_btc(leg.qty)
            leg.exit_mark = m
            leg.exit_fill = fill
            leg.exit_fee = fee
            leg.leg_pnl = leg_pnl
            realized += leg_pnl

        net = realized - fees
        csv_row = self._basket_csv_row(
            basket,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            exit_spot=spot,
            mae_usd=mae_usd,
            max_stress_pct=max_stress_pct,
            absorption_at_trigger=absorption_at_trigger,
            trigger_hit_ts=trigger_hit_ts,
            shorts_pnl=shorts_pnl,
            protection_pnl=protection_pnl,
            gross=realized,
            fees=fees,
            slippage=slip_cost,
            net=net,
            exit_reason=reason,
        )
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
            net_pnl=net,
            worst_mtm=mae_usd,
            n_adjustments=0,
            arm=str(p.get("arm") or ""),
            meta={
                "net_credit": basket.net_credit,
                "target_usd": basket.target_usd,
                "stop_usd": basket.stop_usd,
                "atm": basket.atm,
                "be_call": basket.be_call,
                "be_put": basket.be_put,
                "prot_call": basket.prot_call,
                "prot_put": basket.prot_put,
                "entry_drag": basket.entry_drag,
                "net_theta_entry": basket.net_theta_entry,
                "mae_usd": mae_usd,
                "max_stress_pct": max_stress_pct,
                "absorption_at_trigger": absorption_at_trigger,
                "trigger_hit_ts": trigger_hit_ts,
                "absorption_events": absorption_events,
                "shorts_pnl": shorts_pnl,
                "protection_pnl": protection_pnl,
                "entry_spot": basket.entry_spot,
                "exit_spot": spot,
                "basket_id": basket.basket_id,
                "csv_row": csv_row,
            },
        )

    def _basket_csv_row(
        self,
        basket: BasketBuild,
        *,
        entry_ts: int,
        exit_ts: int,
        exit_spot: float,
        mae_usd: float,
        max_stress_pct: float,
        absorption_at_trigger: float | None,
        trigger_hit_ts: int | None,
        shorts_pnl: float,
        protection_pnl: float,
        gross: float,
        fees: float,
        slippage: float,
        net: float,
        exit_reason: str,
    ) -> dict[str, Any]:
        p = self.params
        row: dict[str, Any] = {
            "arm": str(p.get("arm") or ""),
            "basket_id": basket.basket_id,
            "entry_ts_ist": _ts_ist_str(entry_ts),
            "exit_ts_ist": _ts_ist_str(exit_ts),
            "hold_hours": max(0.0, (exit_ts - entry_ts) / 3600.0),
            "entry_spot": basket.entry_spot,
            "exit_spot": exit_spot,
            "atm_strike": basket.atm,
            "be_call_strike": basket.be_call,
            "be_put_strike": basket.be_put,
            "prot_call_strike": basket.prot_call,
            "prot_put_strike": basket.prot_put,
            "short_expiry": basket.short_expiry.isoformat(),
            "long_expiry": basket.long_expiry.isoformat(),
            "qty_straddle": basket.qty_straddle,
            "qty_strangle": basket.qty_strangle,
            "qty_protection": basket.qty_protection,
        }
        by_role = {leg.role: leg for leg in basket.legs}
        for role in LEG_ROLES:
            leg = by_role[role]
            row[f"{role}_symbol"] = leg.symbol
            row[f"{role}_side"] = leg.side
            row[f"{role}_strike"] = leg.strike
            row[f"{role}_qty"] = leg.qty
            row[f"{role}_entry_mark"] = leg.entry_mark
            row[f"{role}_entry_fill"] = leg.entry_fill
            row[f"{role}_entry_slip_pct"] = leg.entry_slip
            row[f"{role}_entry_fee"] = leg.entry_fee
            row[f"{role}_exit_mark"] = leg.exit_mark
            row[f"{role}_exit_fill"] = leg.exit_fill
            row[f"{role}_exit_fee"] = leg.exit_fee
            row[f"{role}_leg_pnl_usd"] = leg.leg_pnl
        row.update(
            {
                "net_credit": basket.net_credit,
                "entry_drag": basket.entry_drag,
                "target_usd": basket.target_usd,
                "stop_usd": basket.stop_usd,
                "net_theta_entry": basket.net_theta_entry,
                "mae_usd": mae_usd,
                "max_stress_pct": max_stress_pct,
                "absorption_at_trigger": (
                    absorption_at_trigger
                    if absorption_at_trigger is not None
                    else ""
                ),
                "trigger_hit_ts_ist": (
                    _ts_ist_str(trigger_hit_ts) if trigger_hit_ts else ""
                ),
                "shorts_pnl": shorts_pnl,
                "protection_pnl": protection_pnl,
                "gross_usd": gross,
                "fees_usd": fees,
                "slippage_usd": slippage,
                "net_usd": net,
                "exit_reason": exit_reason,
            }
        )
        return row

    def monitor_basket(
        self,
        store: MarksStore,
        spot_map: dict[int, float],
        basket: BasketBuild,
        entry_ts: int,
        entry_day: date,
    ) -> CycleResult:
        p = self.params
        hh, mm = self._cutoff_hm()
        cutoff = to_unix(ist_dt(basket.long_expiry, hh, mm))
        exit_mode = str(p.get("exit_mode") or DEFAULT_EXIT_MODE).lower()
        no_target = bool(p.get("no_target", DEFAULT_NO_TARGET))
        max_loss = p.get("max_basket_loss_usd", DEFAULT_MAX_BASKET_LOSS)
        if max_loss is not None:
            max_loss = float(max_loss)

        mae = 0.0
        max_stress = 0.0
        absorption_events: list[dict[str, Any]] = []
        absorption_at_trigger: float | None = None
        trigger_hit_ts: int | None = None
        exit_reason = "TIME_CUTOFF"
        exit_ts = cutoff
        ts = entry_ts + MONITOR_STEP
        spot = float(basket.entry_spot)

        adj0 = self._adj_mtm(basket, entry_ts)
        if adj0 is not None:
            mae = min(mae, adj0)

        while ts <= cutoff:
            fwd, _ = resolve_forward(store, spot_map, basket.short_expiry, ts)
            if fwd is not None and fwd > 0:
                spot = float(fwd)
            adj = self._adj_mtm(basket, ts)
            if adj is None:
                ts += MONITOR_STEP
                continue
            mae = min(mae, adj)

            if max_loss is not None and adj <= -abs(max_loss):
                exit_reason = "HARD_FLOOR"
                exit_ts = ts
                break

            if exit_mode == "absorption":
                should_exit, ev = self._absorption_check(basket, ts)
                if ev is not None:
                    if ev.get("triggered"):
                        absorption_events.append(ev)
                        max_stress = max(max_stress, float(ev["stress"]))
                        if trigger_hit_ts is None:
                            trigger_hit_ts = int(ev["ts"])
                            absorption_at_trigger = float(ev["A"])
                        logger.info(
                            "S005 absorb trigger basket=%s ts=%s side=%s "
                            "stress=%.1f A=%.3f",
                            basket.basket_id,
                            ev.get("ts_ist"),
                            ev.get("tested_side"),
                            ev.get("stress"),
                            ev.get("A"),
                        )
                    else:
                        max_stress = max(max_stress, float(ev.get("stress") or 0))
                if should_exit and ev is not None:
                    exit_reason = "ABSORPTION_FAIL"
                    exit_ts = ts
                    absorption_at_trigger = float(ev["A"])
                    break
                if not no_target and adj >= basket.target_usd:
                    exit_reason = "TARGET"
                    exit_ts = ts
                    break
            else:
                if not no_target and adj >= basket.target_usd:
                    exit_reason = "TARGET"
                    exit_ts = ts
                    break
                if adj <= basket.stop_usd:
                    exit_reason = "STOPLOSS"
                    exit_ts = ts
                    break

            ts += MONITOR_STEP
        else:
            exit_reason = "TIME_CUTOFF"
            exit_ts = cutoff
            adj = self._adj_mtm(basket, exit_ts)
            if adj is not None:
                mae = min(mae, adj)

        return self._flatten(
            basket,
            exit_ts,
            spot,
            exit_reason,
            entry_ts,
            entry_day,
            mae,
            max_stress_pct=max_stress,
            absorption_at_trigger=absorption_at_trigger,
            trigger_hit_ts=trigger_hit_ts,
            absorption_events=absorption_events,
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
        self._basket_seq = 0
        cycles: list[CycleResult] = []

        now_ts = to_unix(ist_dt(d0, ENTRY_START_HOUR, ENTRY_START_MINUTE))
        end_ts = to_unix(ist_dt(d1, 23, 59))
        cooldown_until = 0
        cooldown_sec = int(
            float(self.params.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)) * 3600
        )
        adverse = {"STOPLOSS", "ABSORPTION_FAIL", "HARD_FLOOR"}

        while now_ts <= end_ts:
            day = datetime.fromtimestamp(now_ts, tz=UTC).astimezone(IST).date()
            if day > d1:
                break

            if now_ts < cooldown_until:
                self.cooldown_skips += 1
                now_ts = min(cooldown_until, now_ts + 3600)
                continue

            pe = str(
                self.params.get("protection_expiry") or DEFAULT_PROTECTION_EXPIRY
            ).lower()
            short_dte = int(self.params["short_dte"])
            long_dte = int(self.params["long_dte"])
            if pe == "same":
                long_exp = day + timedelta(days=short_dte)
            else:
                long_exp = day + timedelta(days=long_dte)
            if not self._can_enter(now_ts, long_exp):
                nxt = day + timedelta(days=1)
                now_ts = to_unix(ist_dt(nxt, ENTRY_START_HOUR, ENTRY_START_MINUTE))
                continue

            basket, skip = self.build_basket(
                store, spot_map, day=day, entry_ts=now_ts
            )
            if basket is None:
                self.skips.record(skip or "skipped_other", day)
                now_ts += 5 * MONITOR_STEP
                continue

            cyc = self.monitor_basket(store, spot_map, basket, now_ts, day)
            cyc.arm = str(self.params.get("arm") or "")
            cycles.append(cyc)
            self.skips.cycles_entered += 1
            logger.info(
                "S005 %s exit=%s net=%.4f mae=%.4f hold=%.2fh",
                day,
                cyc.exit_reason,
                cyc.net_pnl,
                float((cyc.meta or {}).get("mae_usd") or 0),
                cyc.hold_hours,
            )

            if cyc.exit_reason in adverse:
                cooldown_until = cyc.exit_ts + cooldown_sec
                now_ts = cyc.exit_ts + MONITOR_STEP
            elif cyc.exit_reason == "TARGET":
                now_ts = cyc.exit_ts + MONITOR_STEP
            else:
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
    mix = {
        k: (100.0 * v / n if n else float("nan")) for k, v in sorted(reasons.items())
    }
    holds = [c.hold_hours for c in cycles]
    worst = min(cycles, key=lambda c: c.net_pnl) if cycles else None

    tc = [c for c in cycles if c.exit_reason == "TIME_CUTOFF"]
    time_cutoff_pct = (100.0 * len(tc) / n) if n else float("nan")
    time_cutoff_mean_net = (
        float(statistics.fmean([c.net_pnl for c in tc])) if tc else float("nan")
    )

    worst5 = sorted(cycles, key=lambda c: c.net_pnl)[:5]
    worst5_list = [
        {
            "date": c.entry_date.isoformat(),
            "exit_reason": c.exit_reason,
            "net": c.net_pnl,
        }
        for c in worst5
    ]

    shorts_vals = [float((c.meta or {}).get("shorts_pnl") or 0.0) for c in cycles]
    prot_vals = [float((c.meta or {}).get("protection_pnl") or 0.0) for c in cycles]
    mae_vals = [float((c.meta or {}).get("mae_usd") or 0.0) for c in cycles]
    theta_vals = [float((c.meta or {}).get("net_theta_entry") or 0.0) for c in cycles]
    drag_vals = [float((c.meta or {}).get("entry_drag") or 0.0) for c in cycles]

    mean_shorts_pnl = (
        float(statistics.fmean(shorts_vals)) if shorts_vals else float("nan")
    )
    mean_protection_pnl = (
        float(statistics.fmean(prot_vals)) if prot_vals else float("nan")
    )
    mae_usd = float(min(mae_vals)) if mae_vals else float("nan")
    mean_mae = float(statistics.fmean(mae_vals)) if mae_vals else float("nan")
    mean_theta = float(statistics.fmean(theta_vals)) if theta_vals else float("nan")
    mean_entry_drag = float(statistics.fmean(drag_vals)) if drag_vals else float("nan")

    max_loss = abs(mae_usd) if mae_usd == mae_usd else float("nan")
    units = lots_at_risk_cap(max_loss, 100.0, 3.0) if max_loss == max_loss else 0
    daily_net_at_size = mean_day * units if mean_day == mean_day else float("nan")
    daily_pct = (
        100.0 * daily_net_at_size / 100.0
        if daily_net_at_size == daily_net_at_size
        else float("nan")
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
        "worst5": worst5_list,
        "max_dd": max_drawdown(daily) if daily else float("nan"),
        "hold_med": float(statistics.median(holds)) if holds else float("nan"),
        "exit_mix": mix,
        "time_cutoff_pct": time_cutoff_pct,
        "time_cutoff_mean_net": time_cutoff_mean_net,
        "mean_shorts_pnl": mean_shorts_pnl,
        "mean_protection_pnl": mean_protection_pnl,
        "mae_usd": mae_usd,
        "mean_mae_usd": mean_mae,
        "mean_net_theta_entry": mean_theta,
        "mean_entry_drag": mean_entry_drag,
        "max_loss_per_basket": max_loss,
        "basket_units_at_3pct": units,
        "daily_net_pct_of_capital_at_size": daily_pct,
        "cooldown_entry_skips": cooldown_skips,
        "skips": {
            "days_in_window": skips.days_in_window,
            "cycles_entered": skips.cycles_entered,
            "counts": dict(skips.counts),
            "examples": dict(skips.examples),
        },
        "params": dict(params),
        "n_cycles": n,
        "basket_csv_rows": [
            (c.meta or {}).get("csv_row")
            for c in cycles
            if (c.meta or {}).get("csv_row")
        ],
    }
