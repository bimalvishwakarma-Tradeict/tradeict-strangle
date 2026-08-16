# time_utils.py — IST time helpers, expiry calculations, and trigger slab logic

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Allow `python backend/core/time_utils.py` from trading-bot/ root
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.config import (
    ENTRY_SETTLING_SECONDS,
    EXPIRY_HOUR,
    EXPIRY_MINUTE,
    IST,
    PRE_EXPIRY_MINUTES,
    SETTLING_PERIOD_MINUTES,
)


def get_ist_now() -> datetime:
    """Return the current time in IST timezone."""
    return datetime.now(IST)


def settling_ends_at(
    from_time: datetime | None = None,
    *,
    minutes: int | None = None,
    seconds: int | None = None,
) -> datetime:
    """Return IST timestamp when P&L monitoring may begin (TP / adjust only)."""
    base = from_time if from_time is not None else get_ist_now()
    if base.tzinfo is None:
        base = IST.localize(base)
    else:
        base = base.astimezone(IST)
    if seconds is not None:
        return base + timedelta(seconds=int(seconds))
    wait = SETTLING_PERIOD_MINUTES if minutes is None else int(minutes)
    return base + timedelta(minutes=wait)


def settling_ends_at_after_place(from_time: datetime | None = None) -> datetime:
    """Entry settling window after bot-placed fills (ENTRY_SETTLING_SECONDS)."""
    return settling_ends_at(from_time, seconds=ENTRY_SETTLING_SECONDS)


def get_settling_info(monitoring_starts_at: datetime | None) -> dict:
    """
    Settling-period status for API / WS payloads.

    Returns is_settling, settling_ends_at (ISO IST), settling_minutes_left.
    """
    if monitoring_starts_at is None:
        return {
            "is_settling": False,
            "settling_ends_at": None,
            "settling_minutes_left": 0,
        }

    now = get_ist_now()
    starts = monitoring_starts_at
    if starts.tzinfo is None:
        starts = IST.localize(starts)
    else:
        starts = starts.astimezone(IST)

    remaining = (starts - now).total_seconds()
    is_settling = remaining > 0
    minutes_left = max(0, int(remaining // 60))
    if is_settling and minutes_left == 0:
        minutes_left = 1  # still settling; show at least 1m

    return {
        "is_settling": is_settling,
        "settling_ends_at": starts.isoformat(),
        "settling_minutes_left": minutes_left,
    }


def get_expiry_datetime(expiry_date: date) -> datetime:
    """Return expiry_date at 17:30:00 IST (Delta India daily options expiry)."""
    naive = datetime(
        expiry_date.year,
        expiry_date.month,
        expiry_date.day,
        EXPIRY_HOUR,
        EXPIRY_MINUTE,
        0,
    )
    return IST.localize(naive)


def get_hours_to_expiry(expiry_date: date) -> float:
    """
    Hours remaining from now until expiry datetime.

    Returns 0 if already past expiry (never negative).
    """
    now = get_ist_now()
    expiry = get_expiry_datetime(expiry_date)
    delta_seconds = (expiry - now).total_seconds()
    return max(0.0, delta_seconds / 3600.0)


def is_pre_expiry_window(expiry_date: date) -> bool:
    """True if within the pre-expiry close window (default: last 15 minutes)."""
    hours_left = get_hours_to_expiry(expiry_date)
    window_hours = PRE_EXPIRY_MINUTES / 60.0
    return 0.0 < hours_left <= window_hours


def get_trigger_pct(hours_left: float, slabs: dict) -> float:
    """
    Look up the adjustment trigger % from time-based slabs.

    hours_left > 24       → slab_24h (default 200)
    12 < hours_left ≤ 24  → slab_12h (default 175)
    6 < hours_left ≤ 12   → slab_6h  (default 150)
    hours_left ≤ 6        → slab_lt6h (default 150)
    """
    if hours_left > 24:
        return float(slabs.get("slab_24h", 200))
    if hours_left > 12:
        return float(slabs.get("slab_12h", 175))
    if hours_left > 6:
        return float(slabs.get("slab_6h", 150))
    return float(slabs.get("slab_lt6h", 150))


def get_premium_trigger_pct(current_premium: float, slabs: dict) -> float:
    """
    Trigger % from current premium value (premium-based slabs).

    Premium >= $300          → premium_slab_300 (default 150)
    $200 <= Premium < $300   → premium_slab_200 (default 160)
    $100 <= Premium < $200   → premium_slab_100 (default 180)
    Premium < $100           → premium_slab_lt100 (default 200)
    """
    px = float(current_premium or 0.0)
    if px >= 300:
        return float(slabs.get("premium_slab_300", 150))
    if px >= 200:
        return float(slabs.get("premium_slab_200", 160))
    if px >= 100:
        return float(slabs.get("premium_slab_100", 180))
    return float(slabs.get("premium_slab_lt100", 200))


def premium_slab_band_label(current_premium: float) -> str:
    """Human-readable premium band for logs / UI hints."""
    px = float(current_premium or 0.0)
    if px >= 300:
        return f"${px:.0f} >= $300"
    if px >= 200:
        return f"$200 <= ${px:.0f} < $300"
    if px >= 100:
        return f"$100 <= ${px:.0f} < $200"
    return f"${px:.0f} < $100"


def get_dte_label(
    expiry_date: date, all_expiry_dates: list | None = None
) -> str:
    """
    Return a stable, human-readable expiry label.

    Rules (Delta Exchange India pattern):
    - 0/1/2 days away  →  0DTE (08 Aug), 1DTE (09 Aug), 2DTE (10 Aug)
    - Friday but NOT last Friday of month  →  Week 1 (14 Aug), Week 2 (21 Aug)
    - Last Friday of calendar month  →  Month 1 (Aug 2026), Month 2 (Sep 2026)
    - Any other day >2 DTE (event expiry etc)  →  6DTE (14 Aug)

    all_expiry_dates: full list of upcoming expiry date objects so that
    Week 1 / Week 2 position can be computed correctly. If None, this
    expiry is treated as Week 1 / Month 1.
    """
    today = date.today()
    days = (expiry_date - today).days

    # Daily expiries: 0, 1, 2 days out
    if days <= 2:
        return f"{days}DTE ({expiry_date.strftime('%d %b')})"

    # Helper: last Friday of a given month
    def _last_friday(d: date) -> date:
        if d.month == 12:
            last_day = date(d.year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(d.year, d.month + 1, 1) - timedelta(days=1)
        days_back = (last_day.weekday() - 4) % 7  # 4 = Friday
        return last_day - timedelta(days=days_back)

    is_friday = expiry_date.weekday() == 4
    is_monthly = is_friday and expiry_date == _last_friday(expiry_date)

    # Build sorted reference list for position counting
    ref = sorted(set(all_expiry_dates)) if all_expiry_dates else [expiry_date]

    if is_monthly:
        month_num = sum(
            1
            for d in ref
            if d.weekday() == 4
            and d == _last_friday(d)
            and d < expiry_date
        ) + 1
        return f"Month {month_num} ({expiry_date.strftime('%b %Y')})"

    if is_friday:
        week_num = sum(
            1
            for d in ref
            if d.weekday() == 4
            and d != _last_friday(d)
            and d < expiry_date
        ) + 1
        return f"Week {week_num} ({expiry_date.strftime('%d %b')})"

    # Non-Friday mid-week (event expiry like US PPI)
    return f"{days}DTE ({expiry_date.strftime('%d %b')})"


def get_expiry_label(
    expiry_date: date, all_expiry_dates: list[date] | None = None
) -> str:
    """Alias for get_dte_label — kept for any prior call sites."""
    return get_dte_label(expiry_date, all_expiry_dates)


def get_expiry_date_for_dte(dte: int) -> date:
    """
    Calendar expiry date for a requested DTE on Delta India (daily 5:30 PM IST).

    After 5:15 PM IST today's expiry is gone, so the soonest date is tomorrow.
    1DTE means ~1 calendar day to expiry — which is still tomorrow after cutoff
    (not day-after-tomorrow).

    Before 5:15 PM IST: 0DTE=today, 1DTE=tomorrow, 2DTE=day+2
    At/after 5:15 PM IST: 0DTE=tomorrow, 1DTE=tomorrow, 2DTE=day+2
    """
    now = get_ist_now()
    cutoff = now.replace(hour=17, minute=15, second=0, microsecond=0)
    dte_n = max(0, int(dte))

    if now >= cutoff:
        # Today's expiry is gone — minimum expiry is tomorrow.
        # dte=0 and dte=1 both map to tomorrow; dte=2 → day after, etc.
        return now.date() + timedelta(days=max(dte_n, 1))

    # Today's expiry still available
    return now.date() + timedelta(days=dte_n)


if __name__ == "__main__":
    from unittest.mock import patch

    now = get_ist_now()
    print(f"IST Now: {now}")

    tomorrow = date.today() + timedelta(days=1)
    hours = get_hours_to_expiry(tomorrow)
    print(f"Hours to tomorrow expiry: {hours:.2f}")
    assert hours > 0, "Should be positive"

    yesterday = date.today() - timedelta(days=1)
    assert get_hours_to_expiry(yesterday) == 0, "Past expiry should be 0"

    slabs = {"slab_24h": 200, "slab_12h": 175, "slab_6h": 150, "slab_lt6h": 125}
    assert get_trigger_pct(30, slabs) == 200
    assert get_trigger_pct(18, slabs) == 175
    assert get_trigger_pct(8, slabs) == 150
    assert get_trigger_pct(3, slabs) == 125

    prem = {
        "premium_slab_300": 150,
        "premium_slab_200": 160,
        "premium_slab_100": 180,
        "premium_slab_lt100": 200,
    }
    assert get_premium_trigger_pct(350, prem) == 150
    assert get_premium_trigger_pct(250, prem) == 160
    assert get_premium_trigger_pct(150, prem) == 180
    assert get_premium_trigger_pct(50, prem) == 200
    assert "$50 < $100" in premium_slab_band_label(50)

    assert get_dte_label(tomorrow).startswith("1DTE (")
    assert get_dte_label(date.today() + timedelta(days=2)).startswith("2DTE (")

    # After-cutoff: 0DTE and 1DTE both = tomorrow; 2DTE = day after
    fake_after = IST.localize(datetime(2026, 8, 5, 18, 0, 0))
    with patch(f"{__name__}.get_ist_now", return_value=fake_after):
        assert get_expiry_date_for_dte(0) == date(2026, 8, 6)
        assert get_expiry_date_for_dte(1) == date(2026, 8, 6)
        assert get_expiry_date_for_dte(2) == date(2026, 8, 7)
        print("After cutoff (6 PM Aug 5):")
        for dte in [0, 1, 2, 7]:
            print(f"  dte={dte} → {get_expiry_date_for_dte(dte)}")

    # Before-cutoff: 0DTE=today, 1DTE=tomorrow, 2DTE=day+2
    fake_before = IST.localize(datetime(2026, 8, 5, 10, 0, 0))
    with patch(f"{__name__}.get_ist_now", return_value=fake_before):
        assert get_expiry_date_for_dte(0) == date(2026, 8, 5)
        assert get_expiry_date_for_dte(1) == date(2026, 8, 6)
        assert get_expiry_date_for_dte(2) == date(2026, 8, 7)
        print("Before cutoff (10 AM Aug 5):")
        for dte in [0, 1, 2, 7]:
            print(f"  dte={dte} → {get_expiry_date_for_dte(dte)}")

    print("Live get_expiry_date_for_dte:")
    for dte in [0, 1, 2, 7]:
        print(f"  dte={dte} → {get_expiry_date_for_dte(dte)}")

    print("✅ TIME UTILS TEST PASSED")
