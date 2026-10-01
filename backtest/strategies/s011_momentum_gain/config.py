"""S011 Momentum Gain constants. Nothing hardcoded outside this file."""

from __future__ import annotations

from datetime import date

# ---- Data window --------------------------------------------------------
DATA_START = date(2025, 1, 1)
H1_FROM = date(2025, 1, 1)
H1_TO = date(2025, 10, 31)
H2_FROM = date(2025, 11, 1)
H2_TO = date(2026, 9, 30)

# ---- Signal -------------------------------------------------------------
GAP_POINTS = (100, 150, 200, 250, 300)
RSI_LEN = 14
RSI_UP = 60.0
RSI_DN = 40.0

# ---- Clock / expiry -----------------------------------------------------
EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30
MIN_0DTE_HOURS = 2.0
DTE_LABELS = ("0DTE", "1DTE", "2DTE")
DTE_OFFSETS = {"0DTE": 0, "1DTE": 1, "2DTE": 2}
HORIZONS = ("1h", "2h", "4h", "to_expiry")
HORIZON_SEC = {"1h": 3600, "2h": 7200, "4h": 14400, "to_expiry": None}

# ---- Marks / Greeks -----------------------------------------------------
MARK_TOL_SEC = 60
DELTA_TARGETS = (0.75, 0.90)
SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0

# ---- Random baseline + bootstrap ----------------------------------------
RANDOM_N = 5
RANDOM_SEEDS = (20261001, 20261002)
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261001
EDGE_PASS_PP = 5.0

PROGRESS_EVERY = 25
DEFAULT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
