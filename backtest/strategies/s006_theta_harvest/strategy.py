"""S006 Daily Theta Harvesting â€” 1DTE short strangle + 0DTE ratio protection."""

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

logger = logging.getLogger("strategies.s006")

CAPITAL_USD = 100.0
ENTRY_HOUR = 9
ENTRY_MINUTE = 0
DEFAULT_ENTRY_MODE = "fixed"
DEFAULT_ENTRY_WINDOW_START = "09:00"
DEFAULT_ENTRY_WINDOW_END = "15:00"
DEFAULT_CUTOFF_HOUR = 17
DEFAULT_CUTOFF_MINUTE = 29
DEFAULT_QTY_SHORT = 100
DEFAULT_PROTECTION_RATIO = 3.0
DEFAULT_TARGET_PCT = 30.0
DEFAULT_MAX_DD_PCT: float | None = 10.0
DEFAULT_SHORT_OFFSET = 2000.0
DEFAULT_PROTECTION_OFFSET = 0.0
DEFAULT_EXPIRE_PROT_AT_CUTOFF = True
DEFAULT_SHORT_DTE = 1
MONITOR_STEP = 60
INTRADAY_STEP = 3600
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260919
IV_LO = 0.01
IV_HI = 5.0

LEG_ROLES = ("short_call", "short_put", "prot_call", "prot_put")


def default_params() -> dict[str, Any]:
    return {
        "qty_short": DEFAULT_QTY_SHORT,
        "protection_ratio": DEFAULT_PROTECTION_RATIO,
        "target_pct": DEFAULT_TARGET_PCT,
        "max_dd_pct": DEFAULT_MAX_DD_PCT,
        "short_offset": DEFAULT_SHORT_OFFSET,
        "protection_offset": DEFAULT_PROTECTION_OFFSET,
        "short_dte": DEFAULT_SHORT_DTE,
        "cutoff_hour": DEFAULT_CUTOFF_HOUR,
        "cutoff_minute": DEFAULT_CUTOFF_MINUTE,
        "expire_protection_at_cutoff": DEFAULT_EXPIRE_PROT_AT_CUTOFF,
        "entry_mode": DEFAULT_ENTRY_MODE,
        "entry_hour": ENTRY_HOUR,
        "entry_minute": ENTRY_MINUTE,
        "entry_window_start": DEFAULT_ENTRY_WINDOW_START,
        "entry_window_end": DEFAULT_ENTRY_WINDOW_END,
        "slip_model": "bucketed",
        "slip_mult": 1.0,
        "arm": "q100_pr3_tp30_dd10_off2000_po0_cut1729_sdte1_emfixed_e0900",
    }


def parse_cutoff_time(s: str) -> tuple[int, int]:
    parts = str(s).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad cutoff time: {s!r}")
    return int(parts[0]), int(parts[1])


def parse_max_dd_pct(s: str) -> float | None:
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
class HarvestLeg:
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
    expired_flag: bool = False
    leg_pnl: float = 0.0


@dataclass
class BasketBuild:
    legs: list[HarvestLeg]
    net_credit: float
    short_credit: float
    long_debit: float
    target_usd: float
    stop_usd: float | None
    short_expiry: date
    long_expiry: date
    atm: float
    short_call: float
    short_put: float
    prot_call: float
    prot_put: float
    fees_entry: float
    slip_cost_entry: float
    entry_drag: float
    entry_spot: float
    net_theta_entry: float
    qty_short: int
    qty_protection: int
    protection_ratio: float
    short_dte: int = 1
    short_premium_entry_points: float = 0.0
    prot_call_otm_pts: float = 0.0
    prot_put_otm_pts: float = 0.0
    exact_strike_available: int = 0
    strike_gap_pts: float = 0.0
    entry_mode: str = "fixed"
    entry_time_ist: str = ""
    vwap_mid_at_entry: float | None = None
    swing_high: float | None = None
    swing_low: float | None = None
    swing_high_vwap: float | None = None
    swing_low_vwap: float | None = None
    bars_since_day_reset: int = 0
    signals_found_today: int = 0
    signal_rank_used: int = 0
    basket_id: int = 0


class S006ThetaHarvestStrategy:
    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = default_params()
        if params:
            self.params.update(params)
        self.skips = SkipAccount()
        self._basket_seq = 0
        self._strike_exact_hit = 0
        self._strike_exact_miss = 0
        self._strike_miss_gaps: list[float] = []
        self._vwap_accept_nets: list[float] = []
        self._vwap_reject_cf_nets: list[float] = []
        self._df_1m: Any = None
        self._pending_entry_meta: dict[str, Any] = {}

    def meta(self) -> StrategyMeta:
        return StrategyMeta(
            id="S006",
            name="S006 Daily Theta Harvesting",
            version="1.0.0",
            description=(
                "1DTE short strangle (ATMÂ±offset, premium-matched) + 0DTE "
                "ratio protection; daily 09:00 entry; TP/SL/17:29 cutoff; "
                "optional expire-protection at cutoff."
            ),
            status="TESTING",
        )

    def entry_times(self, day: date) -> list[datetime]:
        return [ist_dt(day, ENTRY_HOUR, ENTRY_MINUTE)]

    def build(self, ctx: Any, t: datetime) -> list[Leg] | None:
        return None

    def manage(self, ctx: Any, state: PositionState, t: datetime) -> Action | None:
        return Action(kind="hold")

    def _nearest_strike(self, strikes: list[float], target: float) -> float | None:
        if not strikes:
            return None
        return min(strikes, key=lambda k: (abs(k - target), k))

    def _nearest_otm_strike(
        self,
        strikes: list[float],
        target: float,
        spot: float,
        *,
        is_call: bool,
    ) -> float | None:
        if is_call:
            cands = [k for k in strikes if k > spot]
        else:
            cands = [k for k in strikes if k < spot]
        if not cands:
            return None
        return min(cands, key=lambda k: (abs(k - target), k))

    def _record_short_strike_on_0dte(
        self,
        *,
        day: date,
        target: float,
        chain_by: dict[float, dict[str, Any]],
        spot: float,
        is_call: bool,
    ) -> tuple[bool, float]:
        """Count exact short-strike presence on 0DTE; log gap when missing.

        Returns (exact_available, gap_pts_to_nearest_otm).
        """
        exact = target in chain_by
        nearest_otm = self._nearest_otm_strike(
            sorted(chain_by.keys()), target, spot, is_call=is_call
        )
        side = "call" if is_call else "put"
        if exact:
            self._strike_exact_hit += 1
            return True, 0.0
        self._strike_exact_miss += 1
        if nearest_otm is None:
            logger.info(
                "S006 exact strike miss day=%s side=%s target=%.0f "
                "nearest_otm=None (no OTM on 0DTE chain) spot=%.2f",
                day,
                side,
                target,
                spot,
            )
            return False, float("nan")
        gap = abs(nearest_otm - target)
        self._strike_miss_gaps.append(gap)
        pct = 100.0 * gap / spot if spot > 0 else float("nan")
        logger.info(
            "S006 exact strike miss day=%s side=%s target=%.0f "
            "nearest_otm=%.0f gap=%.0f pts (%.3f%% of spot=%.2f)",
            day,
            side,
            target,
            nearest_otm,
            gap,
            pct,
            spot,
        )
        return False, gap

    def strike_availability_summary(self) -> dict[str, Any]:
        hit = int(self._strike_exact_hit)
        miss = int(self._strike_exact_miss)
        total = hit + miss
        gaps = list(self._strike_miss_gaps)
        return {
            "exact_hit": hit,
            "exact_miss": miss,
            "n_checks": total,
            "exact_hit_pct": (100.0 * hit / total) if total else float("nan"),
            "exact_miss_pct": (100.0 * miss / total) if total else float("nan"),
            "median_miss_gap_pts": (
                float(statistics.median(gaps)) if gaps else float("nan")
            ),
        }

    def _strike_step(self, strikes: list[float]) -> float:
        if len(strikes) < 2:
            return 500.0
        diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
        diffs = [d for d in diffs if d > 0]
        return float(statistics.median(diffs)) if diffs else 500.0

    def _by_strike(self, chain: list[dict[str, Any]]) -> dict[float, dict[str, Any]]:
        return {float(r["strike"]): r for r in chain}

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

    def _leg_by_role(self, basket: BasketBuild, role: str) -> HarvestLeg:
        for leg in basket.legs:
            if leg.role == role:
                return leg
        raise KeyError(role)

    def _pick_premium_matched_strangle(
        self,
        calls: list[dict[str, Any]],
        puts: list[dict[str, Any]],
        spot: float,
        offset: float,
    ) -> tuple[float, dict[str, Any], dict[str, Any]] | None:
        """ATMÂ±offset, then Â±2 strikes â€” pick pair with closest premiums."""
        c_by = self._by_strike(calls)
        p_by = self._by_strike(puts)
        all_k = sorted(set(c_by) | set(p_by))
        if not all_k:
            return None
        atm = self._nearest_strike(all_k, spot)
        if atm is None:
            return None
        step = self._strike_step(all_k)
        call_target = atm + offset
        put_target = atm - offset
        call_base = self._nearest_strike([k for k in all_k if k in c_by], call_target)
        put_base = self._nearest_strike([k for k in all_k if k in p_by], put_target)
        if call_base is None or put_base is None:
            return None

        def neighbors(base: float, in_map: dict[float, dict[str, Any]]) -> list[float]:
            listed = sorted(in_map.keys())
            if base not in listed:
                return []
            i = listed.index(base)
            out: list[float] = []
            for j in range(max(0, i - 2), min(len(listed), i + 3)):
                out.append(listed[j])
            return out

        call_cands = neighbors(call_base, c_by)
        put_cands = neighbors(put_base, p_by)
        best = None
        for ck in call_cands:
            for pk in put_cands:
                if ck <= pk:
                    continue
                cr, pr = c_by[ck], p_by[pk]
                cm, pm = float(cr["mark_price"]), float(pr["mark_price"])
                if cm <= 0 or pm <= 0:
                    continue
                score = (abs(cm - pm), abs(ck - call_target) + abs(pk - put_target))
                if best is None or score < best[0]:
                    best = (score, atm, cr, pr)
        if best is None:
            return None
        return best[1], best[2], best[3]

    def _net_theta_usd(
        self,
        legs: list[HarvestLeg],
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
                short_th += -th_usd
            else:
                long_th += -th_usd
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
        short_dte = max(1, int(p.get("short_dte", DEFAULT_SHORT_DTE)))
        short_exp = day + timedelta(days=short_dte)
        long_exp = day
        if short_exp in SKIP_EXPIRIES or long_exp in SKIP_EXPIRIES:
            return None, "skipped_expiry_blocklist"

        hh, mm = self._cutoff_hm()
        cutoff_ts = to_unix(ist_dt(day, hh, mm))
        if entry_ts >= cutoff_ts:
            return None, "skipped_no_time_before_cutoff"

        conn = store.conn(day)
        if conn is None:
            return None, "skipped_no_mark"

        # Short N-DTE chain (protection always 0DTE)
        conn_s = store.conn(short_exp) or conn
        cts_s = resolve_mark_ts(conn_s, short_exp, entry_ts)
        if cts_s is None:
            return None, "skipped_no_mark"
        spot, _ = resolve_forward(store, spot_map, short_exp, entry_ts)
        if spot is None or spot <= 0:
            return None, "skipped_no_spot"

        calls_s = load_chain(conn_s, short_exp, cts_s, "call")
        puts_s = load_chain(conn_s, short_exp, cts_s, "put")
        if not calls_s or not puts_s:
            return None, "skipped_no_chain"

        offset = float(p.get("short_offset", DEFAULT_SHORT_OFFSET))
        pick = self._pick_premium_matched_strangle(
            calls_s, puts_s, float(spot), offset
        )
        if pick is None:
            return None, "skipped_no_strike"
        atm_k, sc_row, sp_row = pick
        sc_k = float(sc_row["strike"])
        sp_k = float(sp_row["strike"])
        spot_f = float(spot)

        # Short-leg OTM hard check
        if sc_k <= spot_f or sp_k >= spot_f:
            return None, "short_itm"

        q_short = int(p.get("qty_short", DEFAULT_QTY_SHORT))
        pr = float(p.get("protection_ratio", DEFAULT_PROTECTION_RATIO))
        naked = pr <= 0
        slip_model = str(p.get("slip_model") or "bucketed")
        slip_mult = float(p.get("slip_mult") or 1.0)

        pc_k = float("nan")
        pp_k = float("nan")
        prot_call_otm_pts = float("nan")
        prot_put_otm_pts = float("nan")
        exact_strike_available = 0
        strike_gap_pts = 0.0
        lc_row: dict[str, Any] | None = None
        lp_row: dict[str, Any] | None = None
        q_prot = 0

        if not naked:
            # Long 0DTE protection at short strikes +/- protection_offset
            conn_l = store.conn(long_exp) or conn
            cts_l = resolve_mark_ts(conn_l, long_exp, entry_ts)
            if cts_l is None:
                return None, "skipped_no_protection"
            calls_l = load_chain(conn_l, long_exp, cts_l, "call")
            puts_l = load_chain(conn_l, long_exp, cts_l, "put")
            if not calls_l or not puts_l:
                return None, "skipped_no_protection"
            c_by_l = self._by_strike(calls_l)
            p_by_l = self._by_strike(puts_l)

            exact_c, gap_c = self._record_short_strike_on_0dte(
                day=day,
                target=sc_k,
                chain_by=c_by_l,
                spot=spot_f,
                is_call=True,
            )
            exact_p, gap_p = self._record_short_strike_on_0dte(
                day=day,
                target=sp_k,
                chain_by=p_by_l,
                spot=spot_f,
                is_call=False,
            )
            exact_strike_available = 1 if (exact_c and exact_p) else 0
            gap_vals = [g for g in (gap_c, gap_p) if g == g]
            strike_gap_pts = float(max(gap_vals)) if gap_vals else 0.0

            po = float(p.get("protection_offset", DEFAULT_PROTECTION_OFFSET))
            if po == 0:
                pc_target, pp_target = sc_k, sp_k
            else:
                pc_target = sc_k + po
                pp_target = sp_k - po
            pc_k_opt = self._nearest_strike(sorted(c_by_l), pc_target)
            pp_k_opt = self._nearest_strike(sorted(p_by_l), pp_target)
            if pc_k_opt is None or pp_k_opt is None:
                return None, "skipped_no_protection"
            if pc_k_opt not in c_by_l or pp_k_opt not in p_by_l:
                return None, "skipped_no_protection"
            if pc_k_opt == pp_k_opt:
                return None, "protection_same_strike"
            if pc_k_opt <= spot_f or pp_k_opt >= spot_f:
                return None, "protection_itm"
            pc_k = float(pc_k_opt)
            pp_k = float(pp_k_opt)
            lc_row, lp_row = c_by_l[pc_k], p_by_l[pp_k]
            prot_call_otm_pts = pc_k - spot_f
            prot_put_otm_pts = spot_f - pp_k
            q_prot = max(1, int(round(pr * q_short)))

        def make_leg(
            role: str, row: dict[str, Any], qty: int, dte: int, side: str
        ) -> HarvestLeg:
            m = float(row["mark_price"])
            fill, sf = fill_price(
                m, side, dte=dte, slip_model=slip_model, slip_mult=slip_mult
            )
            fee = option_fee(fill, float(spot), qty)
            return HarvestLeg(
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
            make_leg("short_call", sc_row, q_short, short_dte, "sell"),
            make_leg("short_put", sp_row, q_short, short_dte, "sell"),
        ]
        if not naked and lc_row is not None and lp_row is not None:
            legs.append(make_leg("prot_call", lc_row, q_prot, 0, "buy"))
            legs.append(make_leg("prot_put", lp_row, q_prot, 0, "buy"))

        short_credit = sum(
            leg.entry_fill * qty_btc(leg.qty) for leg in legs if leg.side == "sell"
        )
        long_debit = sum(
            leg.entry_fill * qty_btc(leg.qty) for leg in legs if leg.side == "buy"
        )
        short_premium_entry_points = sum(
            leg.entry_mark for leg in legs if leg.side == "sell"
        )
        fees = sum(leg.entry_fee for leg in legs)
        slip_cost = sum(
            abs(leg.entry_fill - leg.entry_mark) * qty_btc(leg.qty) for leg in legs
        )
        net_credit = short_credit - long_debit
        entry_drag = -(slip_cost + fees)
        tp_pct = float(p.get("target_pct", DEFAULT_TARGET_PCT)) / 100.0
        target_usd = max(0.0, net_credit * tp_pct)
        max_dd = p.get("max_dd_pct", DEFAULT_MAX_DD_PCT)
        if max_dd is None:
            stop_usd: float | None = None
        else:
            stop_usd = -(float(max_dd) / 100.0) * CAPITAL_USD

        net_theta = self._net_theta_usd(
            legs,
            spot=float(spot),
            entry_ts=entry_ts,
            short_exp=short_exp,
            long_exp=long_exp,
        )

        settle_ts = to_unix(ist_dt(long_exp, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST))
        preload_end = max(cutoff_ts, settle_ts) + 120
        for leg in legs:
            leg.series = load_symbol_series(store, leg.symbol, entry_ts, preload_end)

        emeta = dict(self._pending_entry_meta or {})
        self._basket_seq += 1
        return (
            BasketBuild(
                legs=legs,
                net_credit=net_credit,
                short_credit=short_credit,
                long_debit=long_debit,
                target_usd=target_usd,
                stop_usd=stop_usd,
                short_expiry=short_exp,
                long_expiry=long_exp,
                atm=atm_k,
                short_call=sc_k,
                short_put=sp_k,
                prot_call=pc_k,
                prot_put=pp_k,
                fees_entry=fees,
                slip_cost_entry=slip_cost,
                entry_drag=entry_drag,
                entry_spot=float(spot),
                net_theta_entry=net_theta,
                qty_short=q_short,
                qty_protection=q_prot,
                protection_ratio=pr,
                short_dte=short_dte,
                short_premium_entry_points=short_premium_entry_points,
                prot_call_otm_pts=prot_call_otm_pts,
                prot_put_otm_pts=prot_put_otm_pts,
                exact_strike_available=exact_strike_available,
                strike_gap_pts=strike_gap_pts,
                entry_mode=str(emeta.get("entry_mode") or p.get("entry_mode") or "fixed"),
                entry_time_ist=str(emeta.get("entry_time_ist") or _ts_ist_str(entry_ts)),
                vwap_mid_at_entry=emeta.get("vwap_mid_at_entry"),
                swing_high=emeta.get("swing_high"),
                swing_low=emeta.get("swing_low"),
                swing_high_vwap=emeta.get("swing_high_vwap"),
                swing_low_vwap=emeta.get("swing_low_vwap"),
                bars_since_day_reset=int(emeta.get("bars_since_day_reset") or 0),
                signals_found_today=int(emeta.get("signals_found_today") or 0),
                signal_rank_used=int(emeta.get("signal_rank_used") or 0),
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

    def _side_mtm(
        self, basket: BasketBuild, ts: int, side: str
    ) -> float | None:
        """Gross mark MTM for sell or buy legs (no fees)."""
        total = 0.0
        for leg in basket.legs:
            if leg.side != side:
                continue
            m = self._mark_at(leg.series, ts)
            if m is None:
                return None
            if side == "sell":
                total += (leg.entry_fill - m) * qty_btc(leg.qty)
            else:
                total += (m - leg.entry_fill) * qty_btc(leg.qty)
        return total
    def _intrinsic(self, strike: float, spot: float, is_call: bool) -> float:
        if is_call:
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)

    def _flatten(
        self,
        store: MarksStore,
        spot_map: dict[int, float],
        basket: BasketBuild,
        exit_ts: int,
        spot: float,
        reason: str,
        entry_ts: int,
        entry_day: date,
        mae_usd: float,
        intraday_rows: list[dict[str, Any]],
    ) -> CycleResult:
        p = self.params
        slip_model = str(p.get("slip_model") or "bucketed")
        slip_mult = float(p.get("slip_mult") or 1.0)
        expire_prot = bool(
            p.get("expire_protection_at_cutoff", DEFAULT_EXPIRE_PROT_AT_CUTOFF)
        )
        expire_longs = reason == "TIME_CUTOFF" and expire_prot

        settle_spot = spot
        if expire_longs:
            settle_ts = to_unix(
                ist_dt(basket.long_expiry, EXPIRY_HOUR_IST, EXPIRY_MINUTE_IST)
            )
            fwd, _ = resolve_forward(
                store, spot_map, basket.long_expiry, settle_ts
            )
            if fwd is not None and fwd > 0:
                settle_spot = float(fwd)

        realized = 0.0
        shorts_pnl = 0.0
        protection_pnl = 0.0
        fees = basket.fees_entry
        slip_cost = basket.slip_cost_entry

        for leg in basket.legs:
            is_long = leg.side == "buy"
            if expire_longs and is_long:
                is_call = leg.opt_type.lower().startswith("c")
                fill = self._intrinsic(leg.strike, settle_spot, is_call)
                m = fill
                fee = 0.0
                leg.expired_flag = True
                leg_pnl = (fill - leg.entry_fill) * qty_btc(leg.qty)
                protection_pnl += leg_pnl
            else:
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
                leg.expired_flag = False

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
            exit_spot=spot if not expire_longs else settle_spot,
            mae_usd=mae_usd,
            shorts_pnl=shorts_pnl,
            protection_pnl=protection_pnl,
            gross=realized,
            fees=fees,
            slippage=slip_cost,
            net=net,
            exit_reason=reason,
        )
        return CycleResult(
            strategy_id="S006",
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
                "short_credit": basket.short_credit,
                "long_debit": basket.long_debit,
                "target_usd": basket.target_usd,
                "target_usd_absolute": basket.target_usd,
                "target_pct": float(p.get("target_pct", DEFAULT_TARGET_PCT)),
                "stop_usd": basket.stop_usd,
                "stop_usd_absolute": basket.stop_usd,
                "short_dte": basket.short_dte,
                "short_premium_entry_points": basket.short_premium_entry_points,
                "entry_drag": basket.entry_drag,
                "net_theta_entry": basket.net_theta_entry,
                "mae_usd": mae_usd,
                "shorts_pnl": shorts_pnl,
                "protection_pnl": protection_pnl,
                "entry_spot": basket.entry_spot,
                "exit_spot": spot if not expire_longs else settle_spot,
                "basket_id": basket.basket_id,
                "csv_row": csv_row,
                "intraday_rows": intraday_rows,
                "expire_protection": expire_longs,
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
            "short_call_strike": basket.short_call,
            "short_put_strike": basket.short_put,
            "prot_call_strike": basket.prot_call,
            "prot_put_strike": basket.prot_put,
            "prot_call_otm_pts": basket.prot_call_otm_pts,
            "prot_put_otm_pts": basket.prot_put_otm_pts,
            "exact_strike_available": basket.exact_strike_available,
            "strike_gap_pts": basket.strike_gap_pts,
            "qty_short": basket.qty_short,
            "qty_protection": basket.qty_protection,
            "protection_ratio": basket.protection_ratio,
            "short_dte": basket.short_dte,
        }
        by_role = {leg.role: leg for leg in basket.legs}
        for role in LEG_ROLES:
            leg = by_role.get(role)
            if leg is None:
                row[f"{role}_symbol"] = ""
                row[f"{role}_side"] = ""
                row[f"{role}_strike"] = ""
                row[f"{role}_qty"] = 0
                row[f"{role}_entry_mark"] = ""
                row[f"{role}_entry_fill"] = ""
                row[f"{role}_entry_slip_pct"] = ""
                row[f"{role}_entry_fee"] = ""
                row[f"{role}_exit_mark"] = ""
                row[f"{role}_exit_fill"] = ""
                row[f"{role}_exit_fee"] = ""
                row[f"{role}_expired_flag"] = 0
                row[f"{role}_leg_pnl_usd"] = ""
                continue
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
            row[f"{role}_expired_flag"] = int(leg.expired_flag)
            row[f"{role}_leg_pnl_usd"] = leg.leg_pnl
        row.update(
            {
                "short_credit": basket.short_credit,
                "long_debit": basket.long_debit,
                "net_credit": basket.net_credit,
                "entry_drag": basket.entry_drag,
                "target_pct": float(p.get("target_pct", DEFAULT_TARGET_PCT)),
                "target_usd": basket.target_usd,
                "target_usd_absolute": basket.target_usd,
                "stop_usd": basket.stop_usd if basket.stop_usd is not None else "",
                "stop_usd_absolute": (
                    basket.stop_usd if basket.stop_usd is not None else ""
                ),
                "short_premium_entry_points": basket.short_premium_entry_points,
                "mae_usd": mae_usd,
                "net_theta_entry": basket.net_theta_entry,
                "shorts_pnl": shorts_pnl,
                "protection_pnl": protection_pnl,
                "gross_usd": gross,
                "fees_usd": fees,
                "slippage_usd": slippage,
                "net_usd": net,
                "exit_reason": exit_reason,
                "entry_mode": basket.entry_mode,
                "entry_time_ist": basket.entry_time_ist,
                "vwap_mid_at_entry": (
                    "" if basket.vwap_mid_at_entry is None else basket.vwap_mid_at_entry
                ),
                "swing_high": "" if basket.swing_high is None else basket.swing_high,
                "swing_low": "" if basket.swing_low is None else basket.swing_low,
                "swing_high_vwap": (
                    "" if basket.swing_high_vwap is None else basket.swing_high_vwap
                ),
                "swing_low_vwap": (
                    "" if basket.swing_low_vwap is None else basket.swing_low_vwap
                ),
                "bars_since_day_reset": basket.bars_since_day_reset,
                "signals_found_today": basket.signals_found_today,
                "signal_rank_used": basket.signal_rank_used,
            }
        )
        return row

    def _intraday_snapshot(
        self, basket: BasketBuild, ts: int, spot: float
    ) -> dict[str, Any] | None:
        by_role = {leg.role: leg for leg in basket.legs}
        marks: dict[str, float] = {}
        for role in ("short_call", "short_put"):
            leg = by_role.get(role)
            if leg is None:
                return None
            m = self._mark_at(leg.series, ts)
            if m is None:
                return None
            marks[role] = m
        for role in ("prot_call", "prot_put"):
            leg = by_role.get(role)
            if leg is None:
                marks[role] = float("nan")
                continue
            m = self._mark_at(leg.series, ts)
            if m is None:
                return None
            marks[role] = m
        shorts_mtm = self._side_mtm(basket, ts, "sell")
        prot_mtm = self._side_mtm(basket, ts, "buy")
        adj = self._adj_mtm(basket, ts)
        if shorts_mtm is None or prot_mtm is None or adj is None:
            return None
        move = (
            100.0 * (spot - basket.entry_spot) / basket.entry_spot
            if basket.entry_spot
            else float("nan")
        )
        pct_nc = (
            100.0 * adj / basket.net_credit if basket.net_credit else float("nan")
        )
        return {
            "basket_id": basket.basket_id,
            "arm": str(self.params.get("arm") or ""),
            "ts_ist": _ts_ist_str(ts),
            "spot": spot,
            "spot_move_pct_from_entry": move,
            "short_call_mark": marks["short_call"],
            "short_put_mark": marks["short_put"],
            "prot_call_mark": marks["prot_call"],
            "prot_put_mark": marks["prot_put"],
            "shorts_mtm": shorts_mtm,
            "protection_mtm": prot_mtm,
            "net_mtm_cost_adjusted": adj,
            "pct_of_net_credit": pct_nc,
        }

    def monitor_basket(
        self,
        store: MarksStore,
        spot_map: dict[int, float],
        basket: BasketBuild,
        entry_ts: int,
        entry_day: date,
    ) -> CycleResult:
        hh, mm = self._cutoff_hm()
        cutoff = to_unix(ist_dt(entry_day, hh, mm))
        mae = 0.0
        exit_reason = "TIME_CUTOFF"
        exit_ts = cutoff
        spot = float(basket.entry_spot)
        intraday: list[dict[str, Any]] = []

        # entry snapshot
        snap0 = self._intraday_snapshot(basket, entry_ts, spot)
        if snap0 is not None:
            intraday.append(snap0)
        adj0 = self._adj_mtm(basket, entry_ts)
        if adj0 is not None:
            mae = min(mae, adj0)

        next_hour = entry_ts + INTRADAY_STEP
        ts = entry_ts + MONITOR_STEP
        while ts <= cutoff:
            fwd, _ = resolve_forward(store, spot_map, basket.short_expiry, ts)
            if fwd is not None and fwd > 0:
                spot = float(fwd)

            if ts >= next_hour:
                snap = self._intraday_snapshot(basket, ts, spot)
                if snap is not None:
                    intraday.append(snap)
                next_hour += INTRADAY_STEP

            adj = self._adj_mtm(basket, ts)
            if adj is None:
                ts += MONITOR_STEP
                continue
            mae = min(mae, adj)

            if adj >= basket.target_usd:
                exit_reason = "TARGET"
                exit_ts = ts
                break
            if basket.stop_usd is not None and adj <= basket.stop_usd:
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

        # final snapshot at exit if not already
        snap_x = self._intraday_snapshot(basket, exit_ts, spot)
        if snap_x is not None:
            if not intraday or intraday[-1].get("ts_ist") != snap_x["ts_ist"]:
                intraday.append(snap_x)

        return self._flatten(
            store,
            spot_map,
            basket,
            exit_ts,
            spot,
            exit_reason,
            entry_ts,
            entry_day,
            mae,
            intraday,
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
        self._basket_seq = 0
        self._strike_exact_hit = 0
        self._strike_exact_miss = 0
        self._strike_miss_gaps = []
        self._vwap_accept_nets = []
        self._vwap_reject_cf_nets = []
        cycles: list[CycleResult] = []

        p = self.params
        entry_mode = str(p.get("entry_mode") or DEFAULT_ENTRY_MODE).lower()
        if entry_mode not in ("fixed", "vwap"):
            raise ValueError(f"bad entry_mode={entry_mode!r}")

        df_1m = None
        if entry_mode == "vwap":
            from backtest.strategies.s006_theta_harvest.vwap_filter import (
                diagnose_no_signal,
                get_vwap_signals,
                load_1m_csv,
            )

            if self._df_1m is None:
                self._df_1m = load_1m_csv()
            df_1m = self._df_1m

        day = d0
        while day <= d1:
            entry_jobs: list[tuple[int, dict[str, Any]]] = []
            if entry_mode == "fixed":
                eh = int(p.get("entry_hour", ENTRY_HOUR))
                em = int(p.get("entry_minute", ENTRY_MINUTE))
                ets = to_unix(ist_dt(day, eh, em))
                entry_jobs.append(
                    (
                        ets,
                        {
                            "entry_mode": "fixed",
                            "entry_time_ist": _ts_ist_str(ets),
                            "vwap_mid_at_entry": None,
                            "swing_high": None,
                            "swing_low": None,
                            "swing_high_vwap": None,
                            "swing_low_vwap": None,
                            "bars_since_day_reset": 0,
                            "signals_found_today": 0,
                            "signal_rank_used": 0,
                        },
                    )
                )
            else:
                from backtest.strategies.s006_theta_harvest.vwap_filter import (
                    diagnose_no_signal,
                    get_vwap_signals,
                )

                ws = str(p.get("entry_window_start") or DEFAULT_ENTRY_WINDOW_START)
                we = str(p.get("entry_window_end") or DEFAULT_ENTRY_WINDOW_END)
                # normalize "0900" -> "09:00" for filter
                def _norm_win(s: str) -> str:
                    s = s.strip().replace(":", "")
                    if len(s) == 3:
                        s = "0" + s
                    if len(s) == 4 and s.isdigit():
                        return f"{s[:2]}:{s[2:]}"
                    return s

                ws_n, we_n = _norm_win(ws), _norm_win(we)
                sigs = get_vwap_signals(df_1m, day, ws_n, we_n)
                if not sigs:
                    diag = diagnose_no_signal(df_1m, day, ws_n, we_n)
                    reason = f"skipped_no_vwap_signal:{diag.get('reason')}"
                    self.skips.record(reason, day)
                    logger.info("S006 VWAP no signal %s reason=%s", day, diag)
                    # Control: counterfactual entry at window start
                    wh, wm = int(ws_n[:2]), int(ws_n[3:5])
                    cf_ts = to_unix(ist_dt(day, wh, wm))
                    self._pending_entry_meta = {
                        "entry_mode": "vwap_reject_cf",
                        "entry_time_ist": _ts_ist_str(cf_ts),
                        "signals_found_today": 0,
                        "signal_rank_used": 0,
                    }
                    cf_basket, cf_skip = self.build_basket(
                        store, spot_map, day=day, entry_ts=cf_ts
                    )
                    if cf_basket is not None:
                        cf_cyc = self.monitor_basket(
                            store, spot_map, cf_basket, cf_ts, day
                        )
                        self._vwap_reject_cf_nets.append(cf_cyc.net_pnl)
                    else:
                        logger.info(
                            "S006 VWAP reject CF skip %s: %s", day, cf_skip
                        )
                    day += timedelta(days=1)
                    continue

                for extra in sigs[1:]:
                    logger.info(
                        "S006 VWAP extra signal ignored day=%s ts=%s",
                        day,
                        extra["ts_ist"],
                    )
                sig = sigs[0]
                ts_dt = sig["ts_ist"]
                if hasattr(ts_dt, "to_pydatetime"):
                    ts_dt = ts_dt.to_pydatetime()
                ets = to_unix(ts_dt)
                entry_jobs.append(
                    (
                        ets,
                        {
                            "entry_mode": "vwap",
                            "entry_time_ist": _ts_ist_str(ets),
                            "vwap_mid_at_entry": float(sig["mid"]),
                            "swing_high": float(sig["swing_high"]),
                            "swing_low": float(sig["swing_low"]),
                            "swing_high_vwap": float(sig["swing_high_vwap"]),
                            "swing_low_vwap": float(sig["swing_low_vwap"]),
                            "bars_since_day_reset": int(sig["bars_since_reset"]),
                            "signals_found_today": len(sigs),
                            "signal_rank_used": 1,
                        },
                    )
                )

            for entry_ts, emeta in entry_jobs:
                self._pending_entry_meta = emeta
                basket, skip = self.build_basket(
                    store, spot_map, day=day, entry_ts=entry_ts
                )
                if basket is None:
                    self.skips.record(skip or "skipped_other", day)
                    continue

                cyc = self.monitor_basket(store, spot_map, basket, entry_ts, day)
                cyc.arm = str(self.params.get("arm") or "")
                cycles.append(cyc)
                self.skips.cycles_entered += 1
                if entry_mode == "vwap":
                    self._vwap_accept_nets.append(cyc.net_pnl)
                logger.info(
                    "S006 %s exit=%s net=%.4f mae=%.4f hold=%.2fh mode=%s",
                    day,
                    cyc.exit_reason,
                    cyc.net_pnl,
                    float((cyc.meta or {}).get("mae_usd") or 0),
                    cyc.hold_hours,
                    entry_mode,
                )

            day += timedelta(days=1)

        if own_store:
            store.close()

        stats = summarize_s006(cycles, d0, d1, self.skips, self.params)
        stats["strike_availability"] = self.strike_availability_summary()
        if entry_mode == "vwap":
            acc = self._vwap_accept_nets
            rej = self._vwap_reject_cf_nets
            stats["vwap_control"] = {
                "n_signal_days": len(acc),
                "n_reject_days_with_cf": len(rej),
                "mean_net_signal_days": (
                    float(statistics.fmean(acc)) if acc else float("nan")
                ),
                "mean_net_reject_cf_days": (
                    float(statistics.fmean(rej)) if rej else float("nan")
                ),
            }
        return cycles, self.skips, stats


def summarize_s006(
    cycles: list[CycleResult],
    d0: date,
    d1: date,
    skips: SkipAccount,
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
    reason_nets: dict[str, list[float]] = {}
    for c in cycles:
        reasons[c.exit_reason] = reasons.get(c.exit_reason, 0) + 1
        reason_nets.setdefault(c.exit_reason, []).append(c.net_pnl)
    mix = {
        k: (100.0 * v / n if n else float("nan")) for k, v in sorted(reasons.items())
    }
    mean_net_by_reason = {
        k: float(statistics.fmean(vs)) for k, vs in sorted(reason_nets.items())
    }

    holds = [c.hold_hours for c in cycles]
    worst = min(cycles, key=lambda c: c.net_pnl) if cycles else None
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
    target_usds = [float((c.meta or {}).get("target_usd") or 0.0) for c in cycles]
    net_credits = [float((c.meta or {}).get("net_credit") or 0.0) for c in cycles]

    mean_shorts = (
        float(statistics.fmean(shorts_vals)) if shorts_vals else float("nan")
    )
    mean_prot = float(statistics.fmean(prot_vals)) if prot_vals else float("nan")
    mae_usd = float(min(mae_vals)) if mae_vals else float("nan")
    mean_mae = float(statistics.fmean(mae_vals)) if mae_vals else float("nan")
    med_mae = float(statistics.median(mae_vals)) if mae_vals else float("nan")
    mean_theta = float(statistics.fmean(theta_vals)) if theta_vals else float("nan")
    mean_drag = float(statistics.fmean(drag_vals)) if drag_vals else float("nan")
    mean_target_usd = (
        float(statistics.fmean(target_usds)) if target_usds else float("nan")
    )
    mean_net_credit = (
        float(statistics.fmean(net_credits)) if net_credits else float("nan")
    )
    mean_net = (
        float(statistics.fmean([c.net_pnl for c in cycles])) if cycles else float("nan")
    )
    median_net = (
        float(statistics.median([c.net_pnl for c in cycles])) if cycles else float("nan")
    )

    max_loss = abs(mae_usd) if mae_usd == mae_usd else float("nan")
    units = lots_at_risk_cap(max_loss, CAPITAL_USD, 3.0) if max_loss == max_loss else 0
    daily_net_at_size = mean_day * units if mean_day == mean_day else float("nan")
    daily_pct = (
        100.0 * daily_net_at_size / CAPITAL_USD
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
        "mean_net": mean_net,
        "median_net": median_net,
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
        "mean_net_by_exit_reason": mean_net_by_reason,
        "mean_shorts_pnl": mean_shorts,
        "mean_protection_pnl": mean_prot,
        "mae_usd": mae_usd,
        "mean_mae_usd": mean_mae,
        "median_mae_usd": med_mae,
        "mean_net_theta_entry": mean_theta,
        "mean_entry_drag": mean_drag,
        "mean_target_usd": mean_target_usd,
        "mean_net_credit": mean_net_credit,
        "target_pct": float(params.get("target_pct", DEFAULT_TARGET_PCT)),
        "max_loss_per_basket": max_loss,
        "basket_units_at_3pct": units,
        "daily_net_pct_of_capital_at_size": daily_pct,
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
        "intraday_csv_rows": [
            row
            for c in cycles
            for row in ((c.meta or {}).get("intraday_rows") or [])
        ],
    }
