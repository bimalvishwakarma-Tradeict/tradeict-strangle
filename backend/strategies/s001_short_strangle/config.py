# config.py — S001 Short Strangle default parameters and constants

from __future__ import annotations

STRATEGY_ID = "S001"
STRATEGY_NAME = "Short Strangle with Dynamic Adjustment"

# Premium targeting
TARGET_PREMIUM_PER_SIDE_USD = 150.0
PREMIUM_HIGHLIGHT_RANGE_USD = 20.0  # highlight if within ±$20 of target

# Delta guidance
SUGGESTED_DELTA_1DTE = 0.30
SUGGESTED_DELTA_2DTE = 0.20

# Default trigger slabs (%)
DEFAULT_SLAB_24H = 200.0
DEFAULT_SLAB_12H = 175.0
DEFAULT_SLAB_6H = 150.0
DEFAULT_SLAB_LT6H = 150.0

# Monitoring
MONITORING_INTERVAL_SECONDS = 30
PRE_EXPIRY_CLOSE_MINUTES = 15

# Supported underlyings (UI / trade labels)
SUPPORTED_UNDERLYINGS = ["BTC", "ETH", "XAU"]

# Delta Exchange India product filter symbols
# Used when calling get_option_chain / get_available_expiries
UNDERLYING_SYMBOLS: dict[str, str] = {
    "BTC": "BTCUSD",
    "ETH": "ETHUSD",
    "XAU": "XAUUSD",
}
