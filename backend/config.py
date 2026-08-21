# config.py — App configuration loaded from .env, constants, and shared enums

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

import pytz
from dotenv import load_dotenv

_ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT_DIR / ".env")

APP_SECRET_KEY: str = os.getenv("APP_SECRET_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./trading_bot.db")
BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8000"))
FRONTEND_PORT: int = int(os.getenv("FRONTEND_PORT", "5173"))
DELTA_EXCHANGE_BASE_URL: str = os.getenv(
    "DELTA_EXCHANGE_BASE_URL",
    "https://api.india.delta.exchange",
)

IST = pytz.timezone("Asia/Kolkata")
EXPIRY_HOUR = 17
EXPIRY_MINUTE = 30
PRE_EXPIRY_MINUTES = 15
MONITORING_INTERVAL_SECONDS = 30
# Fallback defaults only — live values live on AutoTradeSettings
# (entry_settling_seconds / adjustment_settling_seconds). STOPLOSS ignores both.
ENTRY_SETTLING_SECONDS: int = int(os.getenv("ENTRY_SETTLING_SECONDS", "60"))
ADJUSTMENT_SETTLING_SECONDS: int = int(
    os.getenv("ADJUSTMENT_SETTLING_SECONDS", "20")
)
# Legacy aliases (minutes) — prefer ENTRY_SETTLING_SECONDS for bot-placed entries
SETTLING_PERIOD_MINUTES = max(1, (ENTRY_SETTLING_SECONDS + 59) // 60)
SETTLING_PERIOD_AFTER_PLACE_MINUTES = max(1, (ENTRY_SETTLING_SECONDS + 59) // 60)
# Separate longer post-adjust guard (logging / future use) — not the settling window
ADJUSTMENT_COOLDOWN_MINUTES = 3
# Delta India BTC options: PnL USD = premium_diff * size * contract_value
# (API unrealized_pnl for short options is WRONG — see compute_signed_upnl)
OPTIONS_CONTRACT_VALUE: float = float(os.getenv("OPTIONS_CONTRACT_VALUE", "0.001"))
# Hard ceiling on mirrored slave lot size (prevents runaway capital-based qty)
MAX_SLAVE_QTY: int = int(os.getenv("MAX_SLAVE_QTY", "100"))
# PNL sanity: flag when booked realized_pnl sign disagrees with last gross_mtm
# beyond this absolute USD tolerance (near-zero values are ignored)
PNL_SANITY_ABS_TOLERANCE_USD: float = float(
    os.getenv("PNL_SANITY_ABS_TOLERANCE_USD", "0.01")
)
# Delta India options trading fee (market/taker) — estimate only; paid fees come from API
OPTION_FEE_RATE: float = float(os.getenv("OPTION_FEE_RATE", "0.00010"))  # 0.010% of notional
PREMIUM_CAP_RATE: float = float(os.getenv("PREMIUM_CAP_RATE", "0.035"))  # 3.5% of premium
GST_RATE: float = float(os.getenv("GST_RATE", "0.18"))  # 18% GST on base fee


class TradeStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"
    EMERGENCY_CLOSED = "emergency_closed"


class LegType(str, Enum):
    CALL = "call"
    PUT = "put"


class TriggerMode(str, Enum):
    FLAT = "flat"
    SLAB = "slab"
    PREMIUM = "premium"


class ExitReason(str, Enum):
    PROFIT_TARGET = "PROFIT_TARGET"
    STOPLOSS = "STOPLOSS"
    PRE_EXPIRY = "PRE_EXPIRY"
    MANUAL_EMERGENCY = "MANUAL_EMERGENCY"
    MANUAL_LEG_CLOSE = "MANUAL_LEG_CLOSE"
    DECISION_PROFIT_AT_TRIGGER = "DECISION_PROFIT_AT_TRIGGER"
    MANUAL_CLOSE_ON_EXCHANGE = "MANUAL_CLOSE_ON_EXCHANGE"
    SL_TRIGGERED_EMERGENCY_CLOSE = "SL_TRIGGERED_EMERGENCY_CLOSE"
    # Naked risk: DB says open but leg missing on Delta (not a stop-loss event)
    INTEGRITY_NAKED_CLOSE = "INTEGRITY_NAKED_CLOSE"
    MAX_ADJUSTMENTS_REACHED = "MAX_ADJUSTMENTS_REACHED"
    ADJ_LOW_PREMIUM_EXIT = "ADJ_LOW_PREMIUM_EXIT"
    NO_STRIKE_AVAILABLE = "NO_STRIKE_AVAILABLE"
    NO_HEDGE_STRIKE_AVAILABLE = "NO_HEDGE_STRIKE_AVAILABLE"
    NO_OTHER_STRIKE_IN_CONVERSION = "NO_OTHER_STRIKE_IN_CONVERSION"
    # Baskets closed because their linked long hedge was closed
    HEDGE_CLOSED = "HEDGE_CLOSED"
