"""S007-B constants. No hardcoded values in engine/run — everything lives here."""

from __future__ import annotations

# ---- Position structure -------------------------------------------------
LOT_BTC = 0.001            # 1 lot = 0.001 BTC
OPTION_QTY_LOTS = 200      # long option leg
FUT_QTY_LOTS = 100         # futures hedge leg
TARGET_ABS_DELTA = 0.90    # pick strike whose |delta| is nearest this

# ---- Signal -------------------------------------------------------------
BAND_LENGTH = 60           # rolling window bars for Smith VWAP band
BAND_K = 2.0               # band width in volume-weighted mean-abs-dev units
COOLDOWN_BARS = 60         # global cooldown after any accepted signal (minutes)
RSI_OVERSOLD = 30.0        # bullish gate
RSI_OVERBOUGHT = 70.0      # bearish gate
RSI_PERIODS = (7, 14, 21, 28)

# ---- Exit grid ----------------------------------------------------------
TARGET_PTS = (150, 200, 250, 300, 350, 400, 450, 500, 1000)
STOP_PCTS = (0.03, 0.05, 0.07, 0.10)
TIME_EXIT_HOUR_IST = 17    # 1DTE option → forced exit 15 min before 17:30 expiry
TIME_EXIT_MINUTE_IST = 15

# ---- Expiry -------------------------------------------------------------
DTE_DAYS = 1               # 1DTE: expiry = entry calendar date + 1
EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30
SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0

# ---- Fees (verified — do not re-derive) ---------------------------------
# option_fee = min(0.010% * qty*LOT*index, 3.5% * qty*LOT*premium) * 1.18
OPTION_FEE_INDEX_PCT = 0.010 / 100.0
OPTION_FEE_PREMIUM_PCT = 3.5 / 100.0
OPTION_FEE_GST_MULT = 1.18
# futures fee = 0.059% of order value, each side
FUT_FEE_PCT = 0.059 / 100.0

# ---- Slippage -----------------------------------------------------------
# Option slippage as fraction of premium, bucketed by premium (USD).
# (lo_inclusive, hi_exclusive, fraction)
OPTION_SLIP_BUCKETS = (
    (0.0, 100.0, 5.6636 / 100.0),
    (100.0, 300.0, 1.1886 / 100.0),
    (300.0, 600.0, 0.7525 / 100.0),
    (600.0, 900.0, 0.6351 / 100.0),
    (900.0, float("inf"), 0.5681 / 100.0),
)
# Futures slippage: fixed points per side (config constant, never hardcoded).
FUT_SLIP_PTS = 0.5

# ---- Capital ------------------------------------------------------------
# capital_used = option premium paid + futures initial margin (fut mode ON).
# Per project decision the futures initial margin rate is 0, so capital_used is
# the option premium paid in BOTH modes. Change here if a margin model is added.
FUT_INITIAL_MARGIN_PCT = 0.0

# ---- Bootstrap ----------------------------------------------------------
BOOTSTRAP_N = 1000
BOOTSTRAP_SEED = 20260922

# ---- Mark lookup --------------------------------------------------------
MARK_TOL_SEC = 60          # nearest-bar tolerance for option marks

# ---- CSV dump arms (rsi_period, target_pts, stop_pct) -------------------
CSV_DUMP_ARMS = (
    (14, 200, 0.05),       # user's original choice
    (7, 500, 0.07),
    (14, 500, 0.10),
    (28, 1000, 0.05),
)
