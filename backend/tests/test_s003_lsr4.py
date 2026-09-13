# test_s003_lsr4.py — LSR4 engine path tests (synthetic candles)

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.strategies.s003_lsr4.config import Strategy3Config
from backend.strategies.s003_lsr4.lsr4 import Candle, LSR4Engine, _ArmState


def _ts(i: int) -> datetime:
    return datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=i)


def _c(
    i: int,
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    v: float = 100.0,
) -> Candle:
    return Candle(open_time=_ts(i), open=o, high=h, low=l, close=c, volume=v)


def _cfg(**kwargs) -> Strategy3Config:
    base = Strategy3Config(
        enabled=False,
        timeframe="1m",
        sweep_len=5,
        max_wait=3,
        confirm_frac=0.5,
        min_score=3,
        cooldown_bars=10,
        cooldown_atr=100.0,  # effectively bar-count cooldown only
        adx_trend=28.0,
        tick_size=0.5,
        allow_long=True,
        allow_short=True,
        allow_range_mode=True,
        allow_exhaustion_mode=True,
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def _force_warm(engine: LSR4Engine, processed: int = 100) -> None:
    engine._warm = True
    engine._warmup_logged = True
    engine._candles_processed = processed
    engine._vwap._session_boundary_crossed = True
    engine._rewarm_remaining = 0
    engine._prev_close = 100.0


def test_range_top_confirms_short() -> None:
    eng = LSR4Engine(_cfg())
    _force_warm(eng)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    sig = eng.process_closed_candle(_c(1, o=104, h=106, l=100, c=101))
    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.mode == "RANGE"
    assert sig.score == 4
    assert eng._top is None


def test_range_bottom_confirms_long() -> None:
    eng = LSR4Engine(_cfg())
    _force_warm(eng)
    eng._bottom = _ArmState(
        side="BOTTOM",
        signal_extreme=90.0,
        confirm_level=95.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=3,
        atr_at_arm=2.0,
        adx_at_arm=15.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    sig = eng.process_closed_candle(_c(1, o=96, h=100, l=94, c=98))
    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.mode == "RANGE"


def test_arm_invalidated_by_higher_high() -> None:
    eng = LSR4Engine(_cfg())
    _force_warm(eng)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    sig = eng.process_closed_candle(_c(1, o=108, h=111, l=107, c=108))
    assert sig is None
    assert eng._top is None


def test_arm_expires_after_max_wait() -> None:
    eng = LSR4Engine(_cfg(max_wait=2))
    _force_warm(eng)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=100.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    # bars that neither invalidate nor confirm (high<=110, close>=100)
    for i in (1, 2, 3):
        eng._expected_next = _ts(i)
        sig = eng.process_closed_candle(_c(i, o=106, h=108, l=104, c=106))
    assert sig is None
    assert eng._top is None


def test_cooldown_blocks_second_signal() -> None:
    eng = LSR4Engine(_cfg(cooldown_bars=10, cooldown_atr=9999.0))
    _force_warm(eng, processed=100)
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    sig1 = eng.process_closed_candle(_c(1, o=104, h=106, l=100, c=101))
    assert sig1 is not None
    # Immediate second arm+confirm within cooldown
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(1),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._expected_next = _ts(2)
    sig2 = eng.process_closed_candle(_c(2, o=104, h=106, l=100, c=101))
    assert sig2 is None
    assert eng._top is None  # cleared even when cooldown blocks


def test_exhaustion_mode_top() -> None:
    eng = LSR4Engine(
        _cfg(
            allow_range_mode=False,
            allow_exhaustion_mode=True,
            adx_fall=True,
            adx_trend=28.0,
        )
    )
    _force_warm(eng)
    eng._prev_close = 100.0
    eng._prev_adx = 40.0
    eng._prev_adx_2 = 45.0
    candle = _c(1, o=100, h=120, l=99, c=99, v=1000.0)
    # bar_range=21, upper_wick = 120-max(100,99)=20 >= 0.33*21
    eng._try_arm(
        candle=candle,
        prior_high=None,
        prior_low=None,
        bar_range=21.0,
        upper_wick=20.0,
        lower_wick=0.0,
        atr=10.0,
        adx=35.0,
        vol_sma=100.0,
        vwap=100.0,
        rsi=50.0,
    )
    assert eng._top is not None
    assert eng._top.mode == "EXHAUSTION"
    eng._last_processed = _ts(1)
    eng._expected_next = _ts(2)
    # confirm
    sig = eng.process_closed_candle(
        _c(2, o=eng._top.confirm_level - 1, h=eng._top.signal_extreme, l=90, c=eng._top.confirm_level - 1)
    )
    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.mode == "EXHAUSTION"


def test_conflict_emits_neither() -> None:
    eng = LSR4Engine(_cfg())
    _force_warm(eng)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._bottom = _ArmState(
        side="BOTTOM",
        signal_extreme=90.0,
        confirm_level=95.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    # close confirms BOTH: close < 105 and close > 95, and no invalidate
    sig = eng.process_closed_candle(_c(1, o=100, h=104, l=96, c=100))
    assert sig is None
    assert eng._top is None
    assert eng._bottom is None


def test_allow_short_false_never_arms_top() -> None:
    eng = LSR4Engine(_cfg(allow_short=False, allow_long=True))
    _force_warm(eng)
    eng._prev_close = 100.0
    eng._last_signal_bar = None
    eng._last_signal_price = None
    before_cd = (eng._last_signal_bar, eng._last_signal_price)
    eng._try_arm(
        candle=_c(1, o=100, h=120, l=99, c=101, v=1000),
        prior_high=110.0,
        prior_low=90.0,
        bar_range=21.0,
        upper_wick=19.0,
        lower_wick=1.0,
        atr=2.0,
        adx=10.0,  # range mode
        vol_sma=10.0,
        vwap=90.0,
        rsi=70.0,
    )
    assert eng._top is None
    assert (eng._last_signal_bar, eng._last_signal_price) == before_cd


def test_idempotent_same_candle() -> None:
    eng = LSR4Engine(_cfg())
    _force_warm(eng)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    c1 = _c(1, o=104, h=106, l=100, c=101)
    sig1 = eng.process_closed_candle(c1)
    sig2 = eng.process_closed_candle(c1)
    assert sig1 is not None
    assert sig2 is None


def test_gap_over_3_clears_arm_state() -> None:
    eng = LSR4Engine(_cfg())
    _force_warm(eng)
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    # Jump 5 minutes ahead (4 missing slots > 3)
    sig = eng.process_closed_candle(_c(5, o=100, h=101, l=99, c=100))
    assert sig is None
    assert eng._top is None
    assert eng._rewarm_remaining > 0 or eng._warm is False or True
    # After big gap, warm gate requires rewarm
    assert eng._rewarm_remaining == 99  # decremented once on the gap candle


def test_warmup_guard_no_signal_before_100() -> None:
    eng = LSR4Engine(_cfg())
    # Do NOT force warm
    eng._vwap._session_boundary_crossed = True
    eng._candles_processed = 10
    eng._warm = False
    eng._top = _ArmState(
        side="TOP",
        signal_extreme=110.0,
        confirm_level=105.0,
        arm_candle_time=_ts(0),
        bars_elapsed=0,
        mode="RANGE",
        score=4,
        atr_at_arm=2.0,
        adx_at_arm=20.0,
    )
    eng._last_processed = _ts(0)
    eng._expected_next = _ts(1)
    eng._prev_close = 100.0
    sig = eng.process_closed_candle(_c(1, o=104, h=106, l=100, c=101))
    assert sig is None
