"""S010 weekend-theta engine constants. Nothing hardcoded in engine/run."""

from __future__ import annotations

from datetime import date

# ---- Data window --------------------------------------------------------
# 2024 D+3 chains listed ~66.8% of days; 2025/2026 ~97.8%. Skip 2024 entirely.
DATA_START = date(2025, 1, 1)

# ---- Clock --------------------------------------------------------------
ENTRY_HOUR_IST = 18
ENTRY_MINUTE_IST = 0
EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30

# ---- Structure (ratio 1 : 1 : 2) ----------------------------------------
LOT_BTC = 0.001
STRADDLE_QTY = 1000       # short D+2 ATM straddle
STRANGLE_QTY = 1000       # short D+2 OTM strangle
PROTECTION_QTY = 2000     # long ATM straddle (D+3 arm A / D+2 arm B)
ATM_MAX_GAP = 500.0       # skip if nearest ATM strike is farther than this

# ---- Target / stop (assumption — not Delta's real margin engine) --------
MARGIN_MULT = 1.0
TARGET_PCT = 0.10         # +10% of capital_used
STOP_PCT = 0.10           # -10% of capital_used
# Floor so a never-losing expiry shape still has a non-zero target/stop.
CAPITAL_FLOOR_USD = 1.0

# ---- Fees (verified — do not re-derive) ---------------------------------
OPTION_FEE_INDEX_PCT = 0.010 / 100.0
OPTION_FEE_PREMIUM_PCT = 3.5 / 100.0
OPTION_FEE_GST_MULT = 1.18

# ---- Slippage as fraction of premium ------------------------------------
OPTION_SLIP_BUCKETS = (
    (0.0, 100.0, 5.6636 / 100.0),
    (100.0, 300.0, 1.1886 / 100.0),
    (300.0, 600.0, 0.7525 / 100.0),
    (600.0, 900.0, 0.6351 / 100.0),
    (900.0, float("inf"), 0.5681 / 100.0),
)

# ---- Bootstrap ----------------------------------------------------------
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260930

# ---- Marks --------------------------------------------------------------
MARK_TOL_SEC = 60

DOW_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PROGRESS_EVERY = 50
