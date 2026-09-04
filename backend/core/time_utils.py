# time_utils.py — IST time helpers, expiry calculations, and trigger slab logic

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    TZ_CUTOVER_UTC,
)

logger = logging.getLogger(__name__)

# Runtime TZ audit counters (flush guard + get_utc_now)
_writers_utc: int = 0
_writers_ist: int = 0
_naive_blocked: int = 0

_UTC = timezone.utc
try:
    _IST_ZONE = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover
    _IST_ZONE = None


def get_utc_now() -> datetime:
    """
    Single DB-write clock — always timezone-aware UTC.

    All ORM DateTime columns must be set from this helper (or from
    ``to_utc_for_db`` converting an existing aware instant).
    """
    global _writers_utc
    _writers_utc += 1
    return datetime.now(_UTC)


def get_ist_now() -> datetime:
    """Current time in IST — logs and display ONLY. Never write to the DB."""
    return datetime.now(IST)


def tz_audit_counters() -> tuple[int, int, int]:
    """Return (writers_utc, writers_ist, naive_blocked) runtime counters."""
    return _writers_utc, _writers_ist, _naive_blocked


def reset_tz_audit_counters() -> None:
    """Test helper — reset runtime counters."""
    global _writers_utc, _writers_ist, _naive_blocked
    _writers_utc = 0
    _writers_ist = 0
    _naive_blocked = 0


def _offset_looks_like_ist(dt: datetime) -> bool:
    if dt.tzinfo is None:
        return False
    try:
        off = dt.utcoffset()
    except Exception:
        return False
    if off is None:
        return False
    return int(off.total_seconds()) == 19800  # UTC+5:30


def to_utc_for_db(
    dt: datetime | None,
    *,
    context: str = "",
    source: str = "app",
) -> datetime | None:
    """
    Coerce a datetime to timezone-aware UTC for DB storage.

    - None → None
    - Aware non-UTC → convert to UTC (IST writes counted as writers_ist)
    - Naive → count naive_blocked; log level depends on ``source``:
        * ``orm_flush`` — SQLite loads DateTime(timezone=True) as naive;
          before_flush re-stamps them on every dirty flush. Expected → DEBUG.
        * ``app`` (default) — application code handed us a naive datetime
          (real bug risk) → WARNING.
    """
    global _writers_ist, _naive_blocked
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        return dt  # type: ignore[return-value]
    if dt.tzinfo is None:
        _naive_blocked += 1
        # ORM flush path = expected SQLite TZ-less read; app path = suspect write.
        src = str(source or "app").lower().strip()
        log_fn = logger.debug if src == "orm_flush" else logger.warning
        log_fn(
            "[TZ_NAIVE] coerced naive datetime to UTC | source=%s | "
            "context=%s | value=%s",
            src,
            context or "unknown",
            dt.isoformat(sep=" ", timespec="seconds"),
        )
        return dt.replace(tzinfo=_UTC)
    if _offset_looks_like_ist(dt):
        _writers_ist += 1
        logger.error(
            "[TZ_IST_WRITE] IST-aware datetime coerced to UTC for DB | "
            "context=%s | value=%s",
            context or "unknown",
            dt.isoformat(sep=" ", timespec="seconds"),
        )
    return dt.astimezone(_UTC)


def log_tz_audit(prefix: str = "[TZ_AUDIT]") -> None:
    """
    Log runtime counters plus a static source scan of DB timestamp writers.

    Static scan: assignment lines to known DateTime columns using
    get_utc_now / get_ist_now / datetime.now(timezone.utc).
    """
    utc_rt, ist_rt, naive_rt = tz_audit_counters()
    utc_static, ist_static = scan_db_timestamp_writers()
    # Prefer static inventory for "which helper writers use"; include runtime
    # naive/ist catches so regressions after startup are visible in the same tag.
    logger.info(
        "%s writers_utc=%s | writers_ist=%s | naive_blocked=%s",
        prefix,
        utc_static + utc_rt,
        ist_static + ist_rt,
        naive_rt,
    )


def scan_db_timestamp_writers() -> tuple[int, int]:
    """
    Count source lines that assign known DB DateTime columns via UTC vs IST helpers.

    Returns (writers_utc, writers_ist). After phase-1 fix, writers_ist should be 0.
    """
    import re

    col_re = re.compile(
        r"(entry_time|exit_time|created_at|updated_at|last_updated|"
        r"last_connected_at|monitoring_starts_at|adjust_settling_until|"
        r"last_exit_time|next_entry_time|timestamp)\s*="
    )
    utc_re = re.compile(
        r"get_utc_now\s*\(|datetime\.now\s*\(\s*timezone\.utc\s*\)|_utc_now\s*\("
    )
    ist_re = re.compile(r"get_ist_now\s*\(")
    # Settling helpers return IST — must be converted via to_utc_for_db before write
    ist_settle_re = re.compile(
        r"settling_ends_at(?:_after_place)?\s*\("
    )

    backend_root = Path(__file__).resolve().parent.parent
    utc_n = 0
    ist_n = 0
    skip_dirs = {"tests", "__pycache__", ".venv", "venv"}
    for path in backend_root.rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not col_re.search(line):
                continue
            if "isoformat" in line or "strftime" in line:
                continue  # display / payload, not ORM write
            if ist_re.search(line):
                ist_n += 1
            elif utc_re.search(line):
                utc_n += 1
            elif ist_settle_re.search(line):
                # monitoring_starts_at=settling_ends_at... without to_utc → IST write
                if "to_utc_for_db" not in line:
                    ist_n += 1
                else:
                    utc_n += 1
    return utc_n, ist_n


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


def settling_ends_at_after_place(
    from_time: datetime | None = None,
    *,
    seconds: int | None = None,
) -> datetime:
    """Entry settling window after bot-placed fills.

    ``seconds`` overrides the config fallback (e.g. AutoTradeSettings value).
    """
    secs = ENTRY_SETTLING_SECONDS if seconds is None else int(seconds)
    return settling_ends_at(from_time, seconds=max(0, secs))


def _as_ist(dt: datetime | None) -> datetime | None:
    """Convert a DB timestamp to IST for display / IST-clock comparisons.

    Naive values are treated as UTC wall-clock (DB storage contract).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    else:
        dt = dt.astimezone(_UTC)
    return dt.astimezone(IST)


def as_utc(dt: datetime | None) -> datetime | None:
    """Interpret a DB timestamp as UTC (naive = UTC wall-clock)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def get_tz_cutover_utc() -> datetime:
    """Timezone writer cutover instant (aware UTC)."""
    raw = str(TZ_CUTOVER_UTC or "").strip()
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def is_pre_tz_cutover(dt: datetime | None) -> bool:
    """True when ``dt`` is strictly before TZ_CUTOVER_UTC (legacy / mixed zone)."""
    aware = as_utc(dt)
    if aware is None:
        return False
    return aware < get_tz_cutover_utc()


def warn_tz_legacy_row(
    table: str,
    row_id: Any,
    timestamp: datetime | None,
) -> bool:
    """
    Log WARNING if timestamp is pre-cutover. Returns True when legacy.

    Call before any duration or cross-table ordering that uses this stamp.
    """
    if timestamp is None or not is_pre_tz_cutover(timestamp):
        return False
    ts = as_utc(timestamp)
    logger.warning(
        "[TZ_LEGACY_ROW] table=%s | id=%s | timestamp=%s",
        table,
        row_id,
        ts.isoformat() if ts is not None else None,
    )
    return True


def duration_seconds_since(
    start: datetime | None,
    *,
    table: str,
    row_id: Any,
    end: datetime | None = None,
    skip_if_legacy: bool = False,
) -> tuple[float | None, bool]:
    """
    Seconds from ``start`` to ``end`` (default: now UTC).

    Returns ``(seconds, unreliable)``. When ``skip_if_legacy`` and start is
    pre-cutover, returns ``(None, True)`` after logging [TZ_LEGACY_ROW].
    Trading call sites should pass ``skip_if_legacy=False`` (log only) so
    behaviour is unchanged; reports/analytics should skip.
    """
    if start is None:
        return None, False
    legacy = warn_tz_legacy_row(table, row_id, start)
    if legacy and skip_if_legacy:
        return None, True
    start_utc = as_utc(start)
    end_utc = as_utc(end) if end is not None else get_utc_now()
    if start_utc is None or end_utc is None:
        return None, legacy
    return max(0.0, (end_utc - start_utc).total_seconds()), legacy


def get_settling_info(
    monitoring_starts_at: datetime | None,
    adjust_settling_until: datetime | None = None,
) -> dict[str, Any]:
    """
    Combined entry + adjustment settling status.

    is_settling when now < monitoring_starts_at OR now < adjust_settling_until.
    settling_source: "entry" | "adjustment" | "none"
    STOPLOSS callers must ignore is_settling.
    """
    now = get_ist_now()
    entry_end = _as_ist(monitoring_starts_at)
    adjust_end = _as_ist(adjust_settling_until)

    entry_remaining = (entry_end - now).total_seconds() if entry_end else 0.0
    adjust_remaining = (adjust_end - now).total_seconds() if adjust_end else 0.0
    entry_active = entry_remaining > 0
    adjust_active = adjust_remaining > 0
    is_settling = entry_active or adjust_active

    if entry_active:
        settling_source = "entry"
        ends = entry_end
        remaining = entry_remaining
        # If adjust ends later, surface the later end for UI countdown
        if adjust_active and adjust_end is not None and adjust_end > entry_end:
            ends = adjust_end
            remaining = adjust_remaining
    elif adjust_active:
        settling_source = "adjustment"
        ends = adjust_end
        remaining = adjust_remaining
    else:
        settling_source = "none"
        ends = None
        remaining = 0.0

    minutes_left = max(0, int(remaining // 60)) if is_settling else 0
    if is_settling and minutes_left == 0:
        minutes_left = 1  # still settling; show at least 1m

    return {
        "is_settling": is_settling,
        "settling_ends_at": ends.isoformat() if ends is not None else None,
        "settling_minutes_left": minutes_left,
        "settling_source": settling_source,
    }


def get_settling_info_for_trade(trade: Any) -> dict[str, Any]:
    """Convenience wrapper: entry + adjust windows from a Trade-like object."""
    return get_settling_info(
        getattr(trade, "monitoring_starts_at", None),
        getattr(trade, "adjust_settling_until", None),
    )


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
    key = get_expiry_label_key(expiry_date, all_expiry_dates)
    return _format_expiry_label(expiry_date, key)


def get_expiry_label_key(
    expiry_date: date, all_expiry_dates: list | None = None
) -> str:
    """
    Stable relative key for an expiry (never a calendar date).

    Examples: 0dte, 1dte, 2dte, week_1, week_2, month_1, month_2, 6dte
    """
    today = get_ist_now().date()
    days = (expiry_date - today).days

    if days <= 2:
        return f"{max(0, days)}dte"

    def _last_friday(d: date) -> date:
        if d.month == 12:
            last_day = date(d.year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(d.year, d.month + 1, 1) - timedelta(days=1)
        days_back = (last_day.weekday() - 4) % 7
        return last_day - timedelta(days=days_back)

    is_friday = expiry_date.weekday() == 4
    is_monthly = is_friday and expiry_date == _last_friday(expiry_date)
    ref = sorted(set(all_expiry_dates)) if all_expiry_dates else [expiry_date]

    if is_monthly:
        month_num = sum(
            1
            for d in ref
            if d.weekday() == 4
            and d == _last_friday(d)
            and d < expiry_date
        ) + 1
        return f"month_{month_num}"

    if is_friday:
        week_num = sum(
            1
            for d in ref
            if d.weekday() == 4
            and d != _last_friday(d)
            and d < expiry_date
        ) + 1
        return f"week_{week_num}"

    return f"{days}dte"


def _format_expiry_label(expiry_date: date, key: str) -> str:
    """Human label for a key + date (matches historical get_dte_label text)."""
    if key.endswith("dte") and key[:-3].isdigit():
        prefix = f"{key[:-3]}DTE"
        return f"{prefix} ({expiry_date.strftime('%d %b')})"
    if key.startswith("month_"):
        return f"Month {key.split('_', 1)[1]} ({expiry_date.strftime('%b %Y')})"
    if key.startswith("week_"):
        return f"Week {key.split('_', 1)[1]} ({expiry_date.strftime('%d %b')})"
    return f"{key} ({expiry_date.strftime('%d %b')})"


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

    # Combined settling: entry + adjust
    info = get_settling_info(None, None)
    assert info["is_settling"] is False and info["settling_source"] == "none"
    info = get_settling_info(get_ist_now() + timedelta(seconds=30), None)
    assert info["is_settling"] is True and info["settling_source"] == "entry"
    info = get_settling_info(None, get_ist_now() + timedelta(seconds=10))
    assert info["is_settling"] is True and info["settling_source"] == "adjustment"
    info = get_settling_info(None, get_ist_now())  # seconds=0 equivalent
    assert info["is_settling"] is False

    print("✅ TIME UTILS TEST PASSED")
