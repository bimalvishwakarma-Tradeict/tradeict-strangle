"""S012 Trend Follow — constants. Nothing hardcoded outside this file."""

from __future__ import annotations

from datetime import date

DATA_START = date(2025, 1, 1)
H1_FROM = date(2025, 1, 1)
H1_TO = date(2025, 10, 31)
H2_FROM = date(2025, 11, 1)
H2_TO = date(2026, 9, 30)

TFS_MIN = (3, 5, 15)
ST_LEN = 10
ST_MULTS = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
TGT_MULTS = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5)

EXPIRY_HOUR_IST = 17
EXPIRY_MINUTE_IST = 30
WEEKDAYS_OK = (0, 1, 2, 3, 4)  # Mon–Fri

QTY = 1000
LOT_BTC = 0.001
PARTIAL_FRAC = 0.60
ATM_DELTA = 0.50
WING_DELTA = 0.25
ATM_MARK_0DTE_MIN = 200.0

MARK_TOL_SEC = 60
STRIKE_BAND = 6000.0  # load_chain: keep strikes within ± this of spot
SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0

RANDOM_MULT = 3
RANDOM_SEED = 20261002
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261002

DEFAULT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
CKPT_NAME = "s012_ckpt.json"
