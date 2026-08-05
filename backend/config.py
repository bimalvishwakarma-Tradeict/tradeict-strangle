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
# After registration, skip target/SL/adjustment until this many minutes pass
SETTLING_PERIOD_MINUTES = 5
# After bot-placed orders, fill prices are accurate — shorter settle window
SETTLING_PERIOD_AFTER_PLACE_MINUTES = 2
# After an adjustment, pause target/SL/adjust so premiums settle (stops cascades)
ADJUSTMENT_COOLDOWN_MINUTES = 3
# Delta India BTC options: PnL USD = premium_diff * size * contract_value
# (API unrealized_pnl for short options is WRONG — see compute_signed_upnl)
OPTIONS_CONTRACT_VALUE: float = float(os.getenv("OPTIONS_CONTRACT_VALUE", "0.001"))
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
