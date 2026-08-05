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
    EXPIRY_HOUR,
    EXPIRY_MINUTE,
    IST,
    PRE_EXPIRY_MINUTES,
    SETTLING_PERIOD_AFTER_PLACE_MINUTES,
    SETTLING_PERIOD_MINUTES,
)


def get_ist_now() -> datetime:
    """Return the current time in IST timezone."""
    return datetime.now(IST)


def settling_ends_at(
    from_time: datetime | None = None,
    *,
    minutes: int | None = None,
) -> datetime:
    """Return IST timestamp when P&L monitoring may begin."""
    base = from_time if from_time is not None else get_ist_now()
    if base.tzinfo is None:
        base = IST.localize(base)
    else:
        base = base.astimezone(IST)
    wait = SETTLING_PERIOD_MINUTES if minutes is None else int(minutes)
    return base + timedelta(minutes=wait)


def settling_ends_at_after_place(from_time: datetime | None = None) -> datetime:
    """Shorter settle window after bot-placed fills (accurate premiums)."""
    return settling_ends_at(
        from_time, minutes=SETTLING_PERIOD_AFTER_PLACE_MINUTES
    )


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


def get_dte_label(expiry_date: date) -> str:
    """Return DTE label based on calendar days to expiry_date (e.g. '1DTE', '2DTE')."""
    days = (expiry_date - date.today()).days
    return f"{days}DTE"


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

    assert get_dte_label(tomorrow) == "1DTE"
    assert get_dte_label(date.today() + timedelta(days=2)) == "2DTE"

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
