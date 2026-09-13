# lsr4.py — S003 LSR4 signal engine (closed-candle evaluation only)

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.strategies.s003_lsr4.config import Strategy3Config, timeframe_seconds
from backend.strategies.s003_lsr4.indicators import (
    ATR,
    DMI_ADX,
    RSI,
    SMA,
    Rolling,
    SessionVWAP,
)


def _log(event_type: str, details: dict[str, Any]) -> None:
    try:
        from backend.core.bot_logger import log_and_buffer

        log_and_buffer(event_type, 0, details)
    except Exception:
        pass


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class Candle:
    open_time: datetime  # UTC, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    direction: str  # "LONG" | "SHORT"
    mode: str  # "RANGE" | "EXHAUSTION"
    score: int
    signal_candle_time: datetime
    confirm_candle_time: datetime
    confirm_price: float
    signal_extreme: float
    atr_at_arm: float
    atr_at_confirm: float
    adx_at_signal: float
    confirm_level: float = 0.0


@dataclass
class ChartTrace:
    """
    Read-only observation of the engine's own indicator values and arm events.
    Never influences decisions — only records what the engine already computed.
    """

    indicators: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            "vwap": [],
            "atr": [],
            "rsi": [],
            "adx": [],
            "plus_di": [],
            "minus_di": [],
            "vol_sma": [],
        }
    )
    arm_events: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)

    def record_bar(
        self,
        *,
        time_unix: int,
        vwap: float | None,
        atr: float | None,
        rsi: float | None,
        adx: float | None,
        plus_di: float | None,
        minus_di: float | None,
        vol_sma: float | None,
    ) -> None:
        def _pt(value: float | None) -> dict[str, Any]:
            return {"time": int(time_unix), "value": value}

        self.indicators["vwap"].append(_pt(vwap))
        self.indicators["atr"].append(_pt(atr))
        self.indicators["rsi"].append(_pt(rsi))
        self.indicators["adx"].append(_pt(adx))
        self.indicators["plus_di"].append(_pt(plus_di))
        self.indicators["minus_di"].append(_pt(minus_di))
        self.indicators["vol_sma"].append(_pt(vol_sma))

    def record_arm_event(
        self,
        *,
        time_unix: int,
        side: str,
        event_type: str,
        level: float,
    ) -> None:
        self.arm_events.append(
            {
                "time": int(time_unix),
                "side": side,
                "type": event_type,
                "level": float(level),
            }
        )

    def record_signal(self, signal: Signal) -> None:
        self.signals.append(
            {
                "time": int(signal.confirm_candle_time.timestamp()),
                "arm_time": int(signal.signal_candle_time.timestamp()),
                "direction": signal.direction,
                "mode": signal.mode,
                "score": int(signal.score),
                "signal_extreme": float(signal.signal_extreme),
                "confirm_price": float(signal.confirm_price),
                "confirm_level": float(signal.confirm_level),
                "atr_at_arm": float(signal.atr_at_arm),
                "adx_at_signal": float(signal.adx_at_signal),
            }
        )


@dataclass
class _ArmState:
    side: str  # TOP | BOTTOM
    signal_extreme: float
    confirm_level: float
    arm_candle_time: datetime
    bars_elapsed: int
    mode: str
    score: int
    atr_at_arm: float
    adx_at_arm: float


@dataclass
class BackfillDiagnostics:
    """Observation-only counters for POST /backfill. Never affects decisions."""

    warm_from_index: int | None = None
    warm_blocked_reason: str | None = None
    vwap_session_resets: int = 0
    adx_below_trend: int = 0
    adx_at_or_above_trend: int = 0
    sweep_up_count: int = 0
    sweep_dn_count: int = 0
    score_hist: dict[int, int] = field(
        default_factory=lambda: {i: 0 for i in range(6)}
    )
    score_hist_top: dict[int, int] = field(
        default_factory=lambda: {i: 0 for i in range(6)}
    )
    score_hist_bottom: dict[int, int] = field(
        default_factory=lambda: {i: 0 for i in range(6)}
    )
    arms_top: int = 0
    arms_bottom: int = 0
    invalidates: int = 0
    expires: int = 0
    confirms: int = 0
    cooldown_blocks: int = 0
    conflicts: int = 0
    gaps: int = 0
    emitted: int = 0

    @staticmethod
    def _hist_dict(hist: dict[int, int]) -> dict[str, int]:
        return {str(k): int(v) for k, v in sorted(hist.items())}

    def to_dict(self) -> dict[str, Any]:
        return {
            "warm_from_index": self.warm_from_index,
            "warm_blocked_reason": self.warm_blocked_reason,
            "vwap_session_resets": self.vwap_session_resets,
            "counters": {
                "adx_below_trend": self.adx_below_trend,
                "adx_at_or_above_trend": self.adx_at_or_above_trend,
                "sweep_up_count": self.sweep_up_count,
                "sweep_dn_count": self.sweep_dn_count,
                "score_hist": self._hist_dict(self.score_hist),
                "score_hist_top": self._hist_dict(self.score_hist_top),
                "score_hist_bottom": self._hist_dict(self.score_hist_bottom),
                "arms_top": self.arms_top,
                "arms_bottom": self.arms_bottom,
                "invalidates": self.invalidates,
                "expires": self.expires,
                "confirms": self.confirms,
                "cooldown_blocks": self.cooldown_blocks,
                "conflicts": self.conflicts,
                "gaps": self.gaps,
                "emitted": self.emitted,
            },
        }


class LSR4Engine:
    """Incremental LSR4 range + exhaustion signal engine."""

    def __init__(
        self,
        cfg: Strategy3Config,
        *,
        ignore_warmup: bool = False,
        diagnostics: BackfillDiagnostics | None = None,
        chart_trace: ChartTrace | None = None,
    ) -> None:
        self.cfg = cfg
        self._tf_seconds = timeframe_seconds(cfg.timeframe)
        self._ignore_warmup = bool(ignore_warmup)
        self._diag = diagnostics
        self._chart = chart_trace
        self._reset_indicators()
        self._top: _ArmState | None = None
        self._bottom: _ArmState | None = None
        self._last_processed: datetime | None = None
        self._last_signal_bar: int | None = None
        self._last_signal_price: float | None = None
        self._last_signal_candle_time: datetime | None = None
        self._candles_processed = 0
        self._warm = False
        self._warmup_logged = False
        self._last_error: str | None = None
        self._prev_close: float | None = None
        self._prev_adx: float | None = None
        self._prev_adx_2: float | None = None
        self._expected_next: datetime | None = None
        self._rewarm_remaining = 0
        self._natural_warm_ever = False
        if self._ignore_warmup and self._diag is not None:
            self._diag.warm_from_index = 0
            self._diag.warm_blocked_reason = None

    def _reset_indicators(self) -> None:
        c = self.cfg
        self._atr = ATR(c.atr_len)
        self._rsi = RSI(c.rsi_len)
        self._dmi = DMI_ADX(c.adx_len)
        self._vol_sma = SMA(c.vol_len)
        self._high_roll = Rolling(c.sweep_len)  # prior highs (excludes current)
        self._low_roll = Rolling(c.sweep_len)
        self._rsi_high3 = Rolling(3)  # includes current
        self._rsi_low3 = Rolling(3)
        self._vwap = SessionVWAP(c.vwap_anchor_tz)

    def apply_config(self, cfg: Strategy3Config, *, full_reset: bool) -> None:
        self.cfg = cfg
        self._tf_seconds = timeframe_seconds(cfg.timeframe)
        if full_reset:
            self._reset_indicators()
            self._top = None
            self._bottom = None
            self._last_processed = None
            self._last_signal_bar = None
            self._last_signal_price = None
            self._last_signal_candle_time = None
            self._candles_processed = 0
            self._warm = False
            self._warmup_logged = False
            self._prev_close = None
            self._prev_adx = None
            self._prev_adx_2 = None
            self._expected_next = None

    def load_state(self, arm_rows: list[dict], engine_row: dict) -> None:
        self._top = None
        self._bottom = None
        for row in arm_rows:
            if not row.get("armed"):
                continue
            side = str(row.get("side") or "").upper()
            arm = _ArmState(
                side=side,
                signal_extreme=float(row["signal_extreme"]),
                confirm_level=float(row["confirm_level"]),
                arm_candle_time=_as_utc(row["arm_candle_time"]),
                bars_elapsed=int(row.get("bars_elapsed") or 0),
                mode=str(row.get("mode") or "RANGE"),
                score=int(row.get("score") or 0),
                atr_at_arm=float(row.get("atr_at_arm") or 0.0),
                adx_at_arm=float(row.get("adx_at_arm") or 0.0),
            )
            if side == "TOP":
                self._top = arm
            elif side == "BOTTOM":
                self._bottom = arm

        lp = engine_row.get("last_processed_candle_time")
        self._last_processed = _as_utc(lp) if lp is not None else None
        ls = engine_row.get("last_signal_candle_time")
        self._last_signal_candle_time = _as_utc(ls) if ls is not None else None
        self._last_signal_price = (
            float(engine_row["last_signal_price"])
            if engine_row.get("last_signal_price") is not None
            else None
        )
        self._candles_processed = int(engine_row.get("candles_processed") or 0)
        self._warm = bool(engine_row.get("warm", False))
        self._warmup_logged = self._warm
        self._last_error = engine_row.get("last_error")
        if self._last_processed is not None:
            self._expected_next = self._last_processed + timedelta(
                seconds=self._tf_seconds
            )

    def dump_state(self) -> tuple[list[dict], dict]:
        arms: list[dict] = []
        for arm in (self._top, self._bottom):
            if arm is None:
                continue
            arms.append(
                {
                    "side": arm.side,
                    "armed": True,
                    "signal_extreme": arm.signal_extreme,
                    "confirm_level": arm.confirm_level,
                    "arm_candle_time": arm.arm_candle_time,
                    "bars_elapsed": arm.bars_elapsed,
                    "mode": arm.mode,
                    "score": arm.score,
                    "atr_at_arm": arm.atr_at_arm,
                    "adx_at_arm": arm.adx_at_arm,
                }
            )
        # Always include cleared sides for persistence
        present = {a["side"] for a in arms}
        for side in ("TOP", "BOTTOM"):
            if side not in present:
                arms.append(
                    {
                        "side": side,
                        "armed": False,
                        "signal_extreme": None,
                        "confirm_level": None,
                        "arm_candle_time": None,
                        "bars_elapsed": 0,
                        "mode": None,
                        "score": None,
                        "atr_at_arm": None,
                        "adx_at_arm": None,
                    }
                )
        engine = {
            "last_processed_candle_time": self._last_processed,
            "last_signal_candle_time": self._last_signal_candle_time,
            "last_signal_price": self._last_signal_price,
            "candles_processed": self._candles_processed,
            "vwap_session_date": (
                self._vwap.session_date.isoformat()
                if self._vwap.session_date is not None
                else None
            ),
            "warm": self._warm,
            "last_error": self._last_error,
        }
        return arms, engine

    def expire_stale_arms_after_restart(self, missed_bars: int) -> None:
        """If downtime exceeded max_wait, expire arms and log STRAT3_ARM_LOST."""
        if missed_bars <= self.cfg.max_wait:
            return
        for arm in (self._top, self._bottom):
            if arm is None:
                continue
            _log(
                "STRAT3_ARM_LOST",
                {
                    "side": arm.side,
                    "bars_elapsed": arm.bars_elapsed,
                    "missed_bars": missed_bars,
                    "max_wait": self.cfg.max_wait,
                    "arm_candle_time": arm.arm_candle_time.isoformat(),
                },
            )
        self._top = None
        self._bottom = None

    def is_candle_final(self, candle: Candle, now_utc: datetime | None = None) -> bool:
        now = _as_utc(now_utc or datetime.now(timezone.utc))
        close_ts = _as_utc(candle.open_time) + timedelta(seconds=self._tf_seconds)
        return close_ts <= (now - timedelta(seconds=10))

    def process_closed_candle(self, candle: Candle) -> Signal | None:
        open_time = _as_utc(candle.open_time)
        # Idempotency
        if self._last_processed is not None and open_time <= self._last_processed:
            return None

        # Gap detection
        if self._expected_next is not None and open_time > self._expected_next:
            missing = int(
                round(
                    (open_time - self._expected_next).total_seconds()
                    / float(self._tf_seconds)
                )
            )
            if self._diag is not None:
                self._diag.gaps += 1
            _log(
                "STRAT3_GAP",
                {
                    "expected": self._expected_next.isoformat(),
                    "got": open_time.isoformat(),
                    "missing_bars": missing,
                },
            )
            if missing > 3:
                self._top = None
                self._bottom = None
                self._warm = False
                self._warmup_logged = False
                self._rewarm_remaining = 100

        # Update indicators (prior sweep windows exclude current bar)
        prior_high = self._high_roll.highest()
        prior_low = self._low_roll.lowest()

        atr = None
        if self._prev_close is not None:
            atr = self._atr.update(candle.high, candle.low, self._prev_close)
        rsi = self._rsi.update(candle.close)
        dmi = self._dmi.update(candle.high, candle.low, candle.close)
        adx = dmi[2] if dmi is not None else None
        plus_di = dmi[0] if dmi is not None else None
        minus_di = dmi[1] if dmi is not None else None
        vol_sma = self._vol_sma.update(candle.volume)
        prev_session = self._vwap.session_date
        vwap = self._vwap.update(
            open_time, candle.high, candle.low, candle.close, candle.volume
        )
        if (
            self._diag is not None
            and prev_session is not None
            and self._vwap.session_date is not None
            and self._vwap.session_date != prev_session
        ):
            self._diag.vwap_session_resets += 1

        if rsi is not None:
            self._rsi_high3.update(rsi)
            self._rsi_low3.update(rsi)

        # Chart observation — engine's own values (None while indicator warming)
        if self._chart is not None:
            self._chart.record_bar(
                time_unix=int(open_time.timestamp()),
                vwap=float(vwap) if vwap is not None else None,
                atr=float(atr) if atr is not None else None,
                rsi=float(rsi) if rsi is not None else None,
                adx=float(adx) if adx is not None else None,
                plus_di=float(plus_di) if plus_di is not None else None,
                minus_di=float(minus_di) if minus_di is not None else None,
                vol_sma=float(vol_sma) if vol_sma is not None else None,
            )

        tick = float(self.cfg.tick_size)
        if tick <= 0:
            tick = 0.5
        bar_range = max(candle.high - candle.low, tick)
        upper_wick = candle.high - max(candle.open, candle.close)
        lower_wick = min(candle.open, candle.close) - candle.low

        self._candles_processed += 1
        self._last_processed = open_time
        self._expected_next = open_time + timedelta(seconds=self._tf_seconds)
        if self._rewarm_remaining > 0:
            self._rewarm_remaining -= 1

        # Observation: ADX regime + sweeps/scores (does not affect decisions)
        if self._diag is not None and adx is not None:
            if adx < self.cfg.adx_trend:
                self._diag.adx_below_trend += 1
            else:
                self._diag.adx_at_or_above_trend += 1
            sweep_up_obs = (
                prior_high is not None
                and candle.high > prior_high
                and candle.close < prior_high
            )
            sweep_dn_obs = (
                prior_low is not None
                and candle.low < prior_low
                and candle.close > prior_low
            )
            if sweep_up_obs:
                self._diag.sweep_up_count += 1
            if sweep_dn_obs:
                self._diag.sweep_dn_count += 1
            if (
                (sweep_up_obs or sweep_dn_obs)
                and atr is not None
                and vol_sma is not None
                and rsi is not None
            ):
                if sweep_up_obs:
                    sc = self._score_top(
                        sweep_up=True,
                        upper_wick=upper_wick,
                        bar_range=bar_range,
                        volume=candle.volume,
                        vol_sma=vol_sma,
                        close=candle.close,
                        vwap=vwap,
                        atr=atr,
                        rsi=rsi,
                    )
                    sc_i = int(sc)
                    self._diag.score_hist[sc_i] = (
                        self._diag.score_hist.get(sc_i, 0) + 1
                    )
                    self._diag.score_hist_top[sc_i] = (
                        self._diag.score_hist_top.get(sc_i, 0) + 1
                    )
                if sweep_dn_obs:
                    sc = self._score_bottom(
                        sweep_dn=True,
                        lower_wick=lower_wick,
                        bar_range=bar_range,
                        volume=candle.volume,
                        vol_sma=vol_sma,
                        close=candle.close,
                        vwap=vwap,
                        atr=atr,
                        rsi=rsi,
                    )
                    sc_i = int(sc)
                    self._diag.score_hist[sc_i] = (
                        self._diag.score_hist.get(sc_i, 0) + 1
                    )
                    self._diag.score_hist_bottom[sc_i] = (
                        self._diag.score_hist_bottom.get(sc_i, 0) + 1
                    )

        # Warmup gate
        warm_ready = (
            self._candles_processed >= 100
            and self._vwap.session_boundary_crossed
            and self._rewarm_remaining <= 0
        )
        if warm_ready:
            self._natural_warm_ever = True
            if self._diag is not None and self._diag.warm_from_index is None:
                self._diag.warm_from_index = self._candles_processed - 1
            if not self._warm:
                self._warm = True
                if not self._warmup_logged:
                    _log(
                        "STRAT3_WARMUP",
                        {
                            "candles_processed": self._candles_processed,
                            "vwap_session_date": (
                                self._vwap.session_date.isoformat()
                                if self._vwap.session_date
                                else None
                            ),
                        },
                    )
                    self._warmup_logged = True
        elif self._ignore_warmup:
            # Diagnosis only: skip warmup guard; indicators still update normally
            self._warm = True
        else:
            self._warm = False

        # Confirm existing arms first (on later candles)
        confirm_top = self._check_top_confirm(candle, atr)
        confirm_bot = self._check_bottom_confirm(candle, atr)

        emitted: Signal | None = None
        if confirm_top is not None and confirm_bot is not None:
            if self._diag is not None:
                self._diag.conflicts += 1
            _log(
                "STRAT3_CONFLICT",
                {
                    "candle": open_time.isoformat(),
                    "top_direction": confirm_top.direction,
                    "bottom_direction": confirm_bot.direction,
                    "top_signal_candle_time": confirm_top.signal_candle_time.isoformat(),
                    "bottom_signal_candle_time": confirm_bot.signal_candle_time.isoformat(),
                },
            )
            self._top = None
            self._bottom = None
            emitted = None
        elif confirm_top is not None:
            emitted = self._maybe_emit(confirm_top, candle, atr)
        elif confirm_bot is not None:
            emitted = self._maybe_emit(confirm_bot, candle, atr)

        # Arming (only if indicators ready) — does not emit
        if (
            self._warm
            and atr is not None
            and adx is not None
            and vol_sma is not None
            and rsi is not None
        ):
            self._try_arm(
                candle=candle,
                prior_high=prior_high,
                prior_low=prior_low,
                bar_range=bar_range,
                upper_wick=upper_wick,
                lower_wick=lower_wick,
                atr=atr,
                adx=adx,
                vol_sma=vol_sma,
                vwap=vwap,
                rsi=rsi,
            )

        # Advance rolling windows AFTER scoring (so next bar excludes current)
        self._high_roll.update(candle.high)
        self._low_roll.update(candle.low)
        self._prev_close = candle.close
        self._prev_adx_2 = self._prev_adx
        self._prev_adx = adx

        if emitted is not None and self._diag is not None:
            self._diag.emitted += 1
        return emitted

    def finalize_warmup_diagnostics(self) -> None:
        """Set warm_blocked_reason after a backfill run (observation only)."""
        if self._diag is None or self._ignore_warmup:
            return
        if self._natural_warm_ever:
            self._diag.warm_blocked_reason = None
            return
        if self._candles_processed < 100 or self._rewarm_remaining > 0:
            self._diag.warm_blocked_reason = "candle_count"
        elif not self._vwap.session_boundary_crossed:
            self._diag.warm_blocked_reason = "no_vwap_session_boundary"
        else:
            self._diag.warm_blocked_reason = "candle_count"

    def _score_top(
        self,
        *,
        sweep_up: bool,
        upper_wick: float,
        bar_range: float,
        volume: float,
        vol_sma: float,
        close: float,
        vwap: float,
        atr: float,
        rsi: float,
    ) -> int:
        c = self.cfg
        score = 0
        if sweep_up:
            score += 1
        if upper_wick / bar_range >= c.wick_pct:
            score += 1
        if volume > vol_sma * c.vol_mult:
            score += 1
        if (close - vwap) > c.ext_mult * atr:
            score += 1
        rh = self._rsi_high3.highest()
        if rh is not None and rh >= c.rsi_ob:
            score += 1
        return score

    def _score_bottom(
        self,
        *,
        sweep_dn: bool,
        lower_wick: float,
        bar_range: float,
        volume: float,
        vol_sma: float,
        close: float,
        vwap: float,
        atr: float,
        rsi: float,
    ) -> int:
        c = self.cfg
        score = 0
        if sweep_dn:
            score += 1
        if lower_wick / bar_range >= c.wick_pct:
            score += 1
        if volume > vol_sma * c.vol_mult:
            score += 1
        if (vwap - close) > c.ext_mult * atr:
            score += 1
        rl = self._rsi_low3.lowest()
        if rl is not None and rl <= c.rsi_os:
            score += 1
        return score

    def _adx_rolling_down(self, adx: float) -> bool:
        if not self.cfg.adx_fall:
            return True
        if self._prev_adx is None or self._prev_adx_2 is None:
            return False
        return adx < self._prev_adx and self._prev_adx < self._prev_adx_2

    def _try_arm(
        self,
        *,
        candle: Candle,
        prior_high: float | None,
        prior_low: float | None,
        bar_range: float,
        upper_wick: float,
        lower_wick: float,
        atr: float,
        adx: float,
        vol_sma: float,
        vwap: float,
        rsi: float,
    ) -> None:
        c = self.cfg
        sweep_up = (
            prior_high is not None
            and candle.high > prior_high
            and candle.close < prior_high
        )
        sweep_dn = (
            prior_low is not None
            and candle.low < prior_low
            and candle.close > prior_low
        )
        score_top = self._score_top(
            sweep_up=sweep_up,
            upper_wick=upper_wick,
            bar_range=bar_range,
            volume=candle.volume,
            vol_sma=vol_sma,
            close=candle.close,
            vwap=vwap,
            atr=atr,
            rsi=rsi,
        )
        score_bot = self._score_bottom(
            sweep_dn=sweep_dn,
            lower_wick=lower_wick,
            bar_range=bar_range,
            volume=candle.volume,
            vol_sma=vol_sma,
            close=candle.close,
            vwap=vwap,
            atr=atr,
            rsi=rsi,
        )

        # PATH A: RANGE
        if c.allow_range_mode and adx < c.adx_trend:
            if (
                c.allow_short
                and self._top is None
                and sweep_up
                and score_top >= c.min_score
            ):
                self._arm_top(
                    candle, score_top, atr, adx, bar_range, mode="RANGE"
                )
            if (
                c.allow_long
                and self._bottom is None
                and sweep_dn
                and score_bot >= c.min_score
            ):
                self._arm_bottom(
                    candle, score_bot, atr, adx, bar_range, mode="RANGE"
                )

        # PATH B: EXHAUSTION
        if c.allow_exhaustion_mode and adx >= c.adx_trend:
            climax = (
                candle.volume > vol_sma * c.exh_vol_mult
                and bar_range > atr * c.exh_rng_mult
            )
            adx_roll = self._adx_rolling_down(adx)
            if (
                c.allow_short
                and self._top is None
                and climax
                and adx_roll
                and upper_wick / bar_range >= 0.33
                and self._prev_close is not None
                and candle.close < self._prev_close
            ):
                self._arm_top(
                    candle, score_top, atr, adx, bar_range, mode="EXHAUSTION"
                )
            if (
                c.allow_long
                and self._bottom is None
                and climax
                and adx_roll
                and lower_wick / bar_range >= 0.33
                and self._prev_close is not None
                and candle.close > self._prev_close
            ):
                self._arm_bottom(
                    candle, score_bot, atr, adx, bar_range, mode="EXHAUSTION"
                )

    def _arm_top(
        self,
        candle: Candle,
        score: int,
        atr: float,
        adx: float,
        bar_range: float,
        *,
        mode: str,
    ) -> None:
        confirm_level = candle.high - (bar_range * self.cfg.confirm_frac)
        self._top = _ArmState(
            side="TOP",
            signal_extreme=float(candle.high),
            confirm_level=float(confirm_level),
            arm_candle_time=_as_utc(candle.open_time),
            bars_elapsed=0,
            mode=mode,
            score=int(score),
            atr_at_arm=float(atr),
            adx_at_arm=float(adx),
        )
        if self._diag is not None:
            self._diag.arms_top += 1
        if self._chart is not None:
            self._chart.record_arm_event(
                time_unix=int(_as_utc(candle.open_time).timestamp()),
                side="TOP",
                event_type="ARM",
                level=float(confirm_level),
            )
        _log(
            "STRAT3_ARM",
            {
                "side": "TOP",
                "mode": mode,
                "score": score,
                "signal_extreme": self._top.signal_extreme,
                "confirm_level": self._top.confirm_level,
                "arm_candle_time": self._top.arm_candle_time.isoformat(),
                "atr": atr,
                "adx": adx,
            },
        )

    def _arm_bottom(
        self,
        candle: Candle,
        score: int,
        atr: float,
        adx: float,
        bar_range: float,
        *,
        mode: str,
    ) -> None:
        confirm_level = candle.low + (bar_range * self.cfg.confirm_frac)
        self._bottom = _ArmState(
            side="BOTTOM",
            signal_extreme=float(candle.low),
            confirm_level=float(confirm_level),
            arm_candle_time=_as_utc(candle.open_time),
            bars_elapsed=0,
            mode=mode,
            score=int(score),
            atr_at_arm=float(atr),
            adx_at_arm=float(adx),
        )
        if self._diag is not None:
            self._diag.arms_bottom += 1
        if self._chart is not None:
            self._chart.record_arm_event(
                time_unix=int(_as_utc(candle.open_time).timestamp()),
                side="BOTTOM",
                event_type="ARM",
                level=float(confirm_level),
            )
        _log(
            "STRAT3_ARM",
            {
                "side": "BOTTOM",
                "mode": mode,
                "score": score,
                "signal_extreme": self._bottom.signal_extreme,
                "confirm_level": self._bottom.confirm_level,
                "arm_candle_time": self._bottom.arm_candle_time.isoformat(),
                "atr": atr,
                "adx": adx,
            },
        )

    def _check_top_confirm(
        self, candle: Candle, atr: float | None
    ) -> Signal | None:
        arm = self._top
        if arm is None:
            return None
        # Arm candle itself does not confirm
        if _as_utc(candle.open_time) <= arm.arm_candle_time:
            return None
        arm.bars_elapsed += 1
        if candle.high > arm.signal_extreme:
            if self._diag is not None:
                self._diag.invalidates += 1
            if self._chart is not None:
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side="TOP",
                    event_type="INVALIDATE",
                    level=float(arm.signal_extreme),
                )
            _log(
                "STRAT3_INVALIDATE",
                {
                    "side": "TOP",
                    "broken": "signal_high",
                    "signal_extreme": arm.signal_extreme,
                    "high": candle.high,
                    "bars_elapsed": arm.bars_elapsed,
                },
            )
            self._top = None
            return None
        if candle.close < arm.confirm_level:
            sig = Signal(
                direction="SHORT",
                mode=arm.mode,
                score=arm.score,
                signal_candle_time=arm.arm_candle_time,
                confirm_candle_time=_as_utc(candle.open_time),
                confirm_price=float(candle.close),
                signal_extreme=arm.signal_extreme,
                atr_at_arm=arm.atr_at_arm,
                atr_at_confirm=float(atr if atr is not None else arm.atr_at_arm),
                adx_at_signal=arm.adx_at_arm,
                confirm_level=float(arm.confirm_level),
            )
            if self._diag is not None:
                self._diag.confirms += 1
            if self._chart is not None:
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side="TOP",
                    event_type="CONFIRM",
                    level=float(arm.confirm_level),
                )
            self._top = None
            return sig
        if arm.bars_elapsed > self.cfg.max_wait:
            if self._diag is not None:
                self._diag.expires += 1
            if self._chart is not None:
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side="TOP",
                    event_type="EXPIRE",
                    level=float(arm.confirm_level),
                )
            _log(
                "STRAT3_EXPIRE",
                {
                    "side": "TOP",
                    "bars_elapsed": arm.bars_elapsed,
                    "max_wait": self.cfg.max_wait,
                },
            )
            self._top = None
            return None
        return None

    def _check_bottom_confirm(
        self, candle: Candle, atr: float | None
    ) -> Signal | None:
        arm = self._bottom
        if arm is None:
            return None
        if _as_utc(candle.open_time) <= arm.arm_candle_time:
            return None
        arm.bars_elapsed += 1
        if candle.low < arm.signal_extreme:
            if self._diag is not None:
                self._diag.invalidates += 1
            if self._chart is not None:
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side="BOTTOM",
                    event_type="INVALIDATE",
                    level=float(arm.signal_extreme),
                )
            _log(
                "STRAT3_INVALIDATE",
                {
                    "side": "BOTTOM",
                    "broken": "signal_low",
                    "signal_extreme": arm.signal_extreme,
                    "low": candle.low,
                    "bars_elapsed": arm.bars_elapsed,
                },
            )
            self._bottom = None
            return None
        if candle.close > arm.confirm_level:
            sig = Signal(
                direction="LONG",
                mode=arm.mode,
                score=arm.score,
                signal_candle_time=arm.arm_candle_time,
                confirm_candle_time=_as_utc(candle.open_time),
                confirm_price=float(candle.close),
                signal_extreme=arm.signal_extreme,
                atr_at_arm=arm.atr_at_arm,
                atr_at_confirm=float(atr if atr is not None else arm.atr_at_arm),
                adx_at_signal=arm.adx_at_arm,
                confirm_level=float(arm.confirm_level),
            )
            if self._diag is not None:
                self._diag.confirms += 1
            if self._chart is not None:
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side="BOTTOM",
                    event_type="CONFIRM",
                    level=float(arm.confirm_level),
                )
            self._bottom = None
            return sig
        if arm.bars_elapsed > self.cfg.max_wait:
            if self._diag is not None:
                self._diag.expires += 1
            if self._chart is not None:
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side="BOTTOM",
                    event_type="EXPIRE",
                    level=float(arm.confirm_level),
                )
            _log(
                "STRAT3_EXPIRE",
                {
                    "side": "BOTTOM",
                    "bars_elapsed": arm.bars_elapsed,
                    "max_wait": self.cfg.max_wait,
                },
            )
            self._bottom = None
            return None
        return None

    def _cooldown_allows(self, candle: Candle, atr: float | None) -> bool:
        if self._last_signal_bar is None or self._last_signal_price is None:
            return True
        bars_since = self._candles_processed - self._last_signal_bar
        if bars_since >= self.cfg.cooldown_bars:
            return True
        if atr is not None and atr > 0:
            if abs(candle.close - self._last_signal_price) > (
                self.cfg.cooldown_atr * atr
            ):
                return True
        return False

    def _maybe_emit(
        self, signal: Signal, candle: Candle, atr: float | None
    ) -> Signal | None:
        if not self._warm:
            return None
        if not self._cooldown_allows(candle, atr):
            if self._diag is not None:
                self._diag.cooldown_blocks += 1
            if self._chart is not None:
                side = "TOP" if signal.direction == "SHORT" else "BOTTOM"
                self._chart.record_arm_event(
                    time_unix=int(_as_utc(candle.open_time).timestamp()),
                    side=side,
                    event_type="COOLDOWN_BLOCK",
                    level=float(signal.confirm_level),
                )
            _log(
                "STRAT3_COOLDOWN_BLOCK",
                {
                    "direction": signal.direction,
                    "confirm_candle_time": signal.confirm_candle_time.isoformat(),
                    "close": candle.close,
                    "last_signal_price": self._last_signal_price,
                    "candles_processed": self._candles_processed,
                    "last_signal_bar": self._last_signal_bar,
                },
            )
            return None
        self._last_signal_bar = self._candles_processed
        self._last_signal_price = float(candle.close)
        self._last_signal_candle_time = signal.confirm_candle_time
        if self._chart is not None:
            self._chart.record_signal(signal)
        _log(
            "STRAT3_SIGNAL",
            {
                "direction": signal.direction,
                "mode": signal.mode,
                "score": signal.score,
                "signal_candle_time": signal.signal_candle_time.isoformat(),
                "confirm_candle_time": signal.confirm_candle_time.isoformat(),
                "confirm_price": signal.confirm_price,
                "signal_extreme": signal.signal_extreme,
                "atr_at_arm": signal.atr_at_arm,
                "atr_at_confirm": signal.atr_at_confirm,
                "adx_at_signal": signal.adx_at_signal,
            },
        )
        return signal

    # --- introspection for API / tests ---
    @property
    def warm(self) -> bool:
        return self._warm

    @property
    def candles_processed(self) -> int:
        return self._candles_processed

    @property
    def last_processed(self) -> datetime | None:
        return self._last_processed

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def set_last_error(self, msg: str | None) -> None:
        self._last_error = msg

    def arm_snapshot(self) -> dict[str, Any]:
        def _arm(a: _ArmState | None) -> dict[str, Any] | None:
            if a is None:
                return None
            return {
                "side": a.side,
                "signal_extreme": a.signal_extreme,
                "confirm_level": a.confirm_level,
                "arm_candle_time": a.arm_candle_time.isoformat(),
                "bars_elapsed": a.bars_elapsed,
                "mode": a.mode,
                "score": a.score,
                "atr_at_arm": a.atr_at_arm,
                "adx_at_arm": a.adx_at_arm,
            }

        return {"top": _arm(self._top), "bottom": _arm(self._bottom)}
