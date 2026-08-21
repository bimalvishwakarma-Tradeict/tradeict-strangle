# bot_logger.py — Structured bot activity logging (file + in-memory buffer)

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.time_utils import get_ist_now

bot_log = logging.getLogger("bot_activity")

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "bot_activity.log"

_log_buffer: list[dict[str, Any]] = []
MAX_BUFFER = 500
_IMPORTANT_EVENTS = frozenset(
    {
        "ADJUSTMENT_START",
        "ADJUSTMENT_DONE",
        "ADJUSTMENT_FAIL",
        "ADJUSTMENT_HOLD",
        "ADJUSTMENT_DELTA_VERIFY",
        "BASELINE_RESET",
        "PARTIAL_ADJUSTMENT",
        "DECISION_TRIGGER",
        "EXIT_TRIGGERED",
        "EXIT_START",
        "EXIT_VERIFY",
        "EXIT_CLOSE",
        "EXIT_CLEANUP",
        "EXIT_COMPLETE",
        "EXIT_DONE",
        "EXIT_FAIL",
        "EXIT_FUNNEL",
        "LEG_CLOSE_RESULT",
        "ERROR",
        "NAKED_POSITION",
        "EMERGENCY_CLOSE",
        "MANUAL_EXCHANGE_CLOSE",
        "POSITION_CHECK",
        "INTEGRITY_MANUAL_CLOSE",
        "INTEGRITY_NAKED",
        "TP_SL_LOCKED",
        "ENTRY_GUARD_PASS",
        "ENTRY_GUARD_BLOCK",
        "ORPHAN_DETECTED",
        "ORPHAN_AUTO_CLOSED",
        "ORPHAN_SL_CANCELLED",
        "ENTRY_SPREAD_RESET",
        "PARTIAL_ENTRY_CLEANUP",
        "PARTIAL_ENTRY_CLEANUP_FAILED",
        "CONVERSION_HOLD",
        "POSITION_VERIFIED",
        "POSITION_WARNING",
        "MIRROR_ADJ_DEBUG",
        "MIRROR_ADJ_PRE",
        "MIRROR_ADJ_CALLED",
        "MIRROR_ADJ_SKIP",
        "MIRROR_ADJ_FAIL",
        "MIRROR_ADJ_ENGINE",
        "MIRROR_ADJ_VERIFY",
        "MIRROR_EXIT",
        "SLAVE_SWEEP",
        "DB_AUDIT_SKIP",
        "BRACKET_SL",
        "BRACKET_SL_ANOMALY",
        "EXIT_SKIP",
        "LEG_BOOK_SKIP",
        "PNL_SANITY_FAIL",
        "SETTLING",
        "SETTLING_BYPASS",
        "HEDGE_OPEN_START",
        "HEDGE_OPEN_DONE",
        "HEDGE_OPEN_FAIL",
        "HEDGE_VERIFY",
        "HEDGE_CLOSE_START",
        "HEDGE_CLOSE_DONE",
        "HEDGE_CLOSE_FAIL",
        "HEDGE_CLOSE_SKIP",
        "HEDGE_CLOSE_BLOCKED",
        "HEDGE_CASCADE",
        "HEDGE_PNL",
        "HEDGE_THETA_LOG",
        "HEDGE_GATE",
        "HEDGE_GATE_BLOCK",
        "HEDGE_GATE_BACKOFF",
        "HEDGE_AFFORD_BLOCK",
        "HEDGE_UNWIND",
        "HEDGE_SETTINGS_UPDATE",
    }
)

_setup_done = False


def setup_bot_logger() -> None:
    """Setup rotating file logger for bot activity (daily, keep 7 days)."""
    global _setup_done
    if _setup_done:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        str(_LOG_FILE),
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    bot_log.handlers.clear()
    bot_log.addHandler(file_handler)
    bot_log.addHandler(console)
    bot_log.setLevel(logging.DEBUG)
    bot_log.propagate = False
    _setup_done = True
    bot_log.info("Bot activity logger ready → %s", _LOG_FILE)


def log_tp_sl_locked(
    trade_id: int,
    initial_max_profit: float,
    profit_target_usd: float,
    stoploss_usd: float,
    tp_pct: float,
    sl_pct: float,
) -> None:
    """
    Log locked TP/SL at trade entry.

    TP/SL locked to initial deployment premium.
    initial_max_profit never changes after trade entry.
    adjustments do NOT affect TP/SL.
    """
    bot_log.info(
        "Trade %s locked: "
        "initial_max_profit=%s "
        "TP=%s (%s%%) "
        "SL=%s (%s%%) "
        "— will NOT change on adjustments",
        trade_id,
        initial_max_profit,
        profit_target_usd,
        tp_pct,
        stoploss_usd,
        sl_pct,
    )
    # Buffer only (message already logged above — avoid duplicate file lines)
    entry = {
        "timestamp": get_ist_now().isoformat(),
        "event_type": "TP_SL_LOCKED",
        "trade_id": trade_id,
        "details": {
            "initial_max_profit": initial_max_profit,
            "profit_target_usd": profit_target_usd,
            "stoploss_usd": stoploss_usd,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
        },
        "message": (
            f"Trade {trade_id} locked: "
            f"initial_max_profit={initial_max_profit} "
            f"TP={profit_target_usd} ({tp_pct}%) "
            f"SL={stoploss_usd} ({sl_pct}%) "
            f"— will NOT change on adjustments"
        ),
    }
    _log_buffer.append(entry)
    if len(_log_buffer) > MAX_BUFFER:
        del _log_buffer[0 : len(_log_buffer) - MAX_BUFFER]


def log_event(event_type: str, trade_id: int, details: dict[str, Any]) -> str:
    """Log a structured bot event to file/console."""
    now = get_ist_now().strftime("%H:%M:%S IST")
    detail_str = " | ".join(f"{k}={v}" for k, v in details.items())
    # Hedge lifecycle passes hedge_position.id — never label those as Trade#
    if str(event_type).startswith("HEDGE_"):
        entity = f"Hedge#{trade_id}"
    else:
        entity = f"Trade#{trade_id}"
    msg = f"[{event_type}] {entity} @ {now} | {detail_str}"

    if event_type in (
        "ERROR",
        "ADJUSTMENT_FAIL",
        "EXIT_FAIL",
        "NAKED_POSITION",
        "EMERGENCY_CLOSE",
        "ENTRY_GUARD_BLOCK",
        "PARTIAL_ADJUSTMENT",
        "INTEGRITY_NAKED",
        "BRACKET_SL_ANOMALY",
        "PNL_SANITY_FAIL",
        "HEDGE_OPEN_FAIL",
        "HEDGE_CLOSE_FAIL",
        "HEDGE_CLOSE_BLOCKED",
        "HEDGE_AFFORD_BLOCK",
        "HEDGE_GATE_BLOCK",
        "HEDGE_GATE_BACKOFF",
    ):
        bot_log.error(msg)
    elif event_type in (
        "ADJUSTMENT_START",
        "EXIT_TRIGGERED",
        "EXIT_START",
        "ADJUSTMENT_HOLD",
        "DECISION_TRIGGER",
        "MANUAL_EXCHANGE_CLOSE",
        "POSITION_WARNING",
        "BASELINE_RESET",
        "EXIT_CLEANUP",
        "INTEGRITY_MANUAL_CLOSE",
        "ORPHAN_DETECTED",
        "CONVERSION_HOLD",
        "PARTIAL_ENTRY_CLEANUP_FAILED",
        "DB_AUDIT_SKIP",
    ):
        bot_log.warning(msg)
    elif event_type in (
        "ADJUSTMENT_DONE",
        "EXIT_DONE",
        "EXIT_COMPLETE",
        "EXIT_VERIFY",
        "EXIT_CLOSE",
        "EXIT_FUNNEL",
        "LEG_CLOSE_RESULT",
        "POSITION_CHECK",
        "SETTLING",
        "SETTLING_BYPASS",
        "TP_SL_LOCKED",
        "ENTRY_GUARD_PASS",
        "POSITION_VERIFIED",
        "ADJUSTMENT_DELTA_VERIFY",
        "ORPHAN_AUTO_CLOSED",
        "ORPHAN_SL_CANCELLED",
        "ENTRY_SPREAD_RESET",
        "EXIT_SKIP",
        "LEG_BOOK_SKIP",
        "PNL_SANITY_FAIL",
        "PARTIAL_ENTRY_CLEANUP",
        "MIRROR_ADJ_DEBUG",
        "MIRROR_ADJ_PRE",
        "MIRROR_ADJ_CALLED",
        "MIRROR_ADJ_ENGINE",
        "MIRROR_ADJ_VERIFY",
        "MIRROR_EXIT",
        "SLAVE_SWEEP",
        "BRACKET_SL",
        # Hedge audit trail — must survive INFO-level production logging
        "HEDGE_CASCADE",
        "HEDGE_OPEN_START",
        "HEDGE_OPEN_DONE",
        "HEDGE_CLOSE_START",
        "HEDGE_CLOSE_DONE",
        "HEDGE_CLOSE_SKIP",
        "HEDGE_VERIFY",
        "HEDGE_UNWIND",
        "HEDGE_PNL",
        "HEDGE_GATE",
        "HEDGE_THETA_LOG",
        "HEDGE_SETTINGS_UPDATE",
    ):
        bot_log.info(msg)
    elif event_type in (
        "MIRROR_ADJ_SKIP",
        "MIRROR_ADJ_FAIL",
    ):
        bot_log.warning(msg)
    else:
        # INTEGRITY_OK and other high-frequency events stay at debug
        bot_log.debug(msg)
    return msg


def log_and_buffer(
    event_type: str, trade_id: int, details: dict[str, Any]
) -> dict[str, Any]:
    """Log event and append to in-memory ring buffer (newest last)."""
    msg = log_event(event_type, trade_id, details)
    entry = {
        "timestamp": get_ist_now().isoformat(),
        "event_type": event_type,
        "trade_id": trade_id,
        "details": details,
        "message": msg,
    }
    _log_buffer.append(entry)
    if len(_log_buffer) > MAX_BUFFER:
        del _log_buffer[0 : len(_log_buffer) - MAX_BUFFER]
    return entry


def get_recent_logs(
    trade_id: int | None = None,
    limit: int = 100,
    level: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent buffered logs, newest first."""
    logs = list(_log_buffer)
    if trade_id is not None:
        logs = [row for row in logs if row.get("trade_id") == trade_id]
    if level and str(level).lower() in {"important", "important_only"}:
        logs = [row for row in logs if row.get("event_type") in _IMPORTANT_EVENTS]
    limit = max(1, min(int(limit), MAX_BUFFER))
    logs = logs[-limit:]
    return list(reversed(logs))


def get_log_file_path(date_str: str | None = None) -> Path | None:
    """
    Resolve log file for a date (YYYY-MM-DD).

    Today → bot_activity.log
    Past → bot_activity.log.YYYY-MM-DD (TimedRotatingFileHandler suffix)
    """
    if not date_str:
        date_str = get_ist_now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

    today = get_ist_now().strftime("%Y-%m-%d")
    if date_str == today:
        path = _LOG_FILE
    else:
        path = _LOG_DIR / f"bot_activity.log.{date_str}"
    return path if path.is_file() else None


def read_log_file(date_str: str | None = None) -> str:
    """Return raw log file text for date, or empty string if missing."""
    path = get_log_file_path(date_str)
    if path is None:
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
