"""S013 Smith signal + S012 basket — constants."""

from __future__ import annotations

from datetime import date

DATA_START = date(2025, 1, 1)
H1_FROM = date(2025, 1, 1)
H1_TO = date(2025, 10, 31)
H2_FROM = date(2025, 11, 1)
H2_TO = date(2026, 9, 30)

TF_MIN = 5
SMITH_LEN = 60
SMITH_K = 2.0
RSI_PERIODS = (7, 14, 21, 28)
RSI_LONG = 30.0
RSI_SHORT = 70.0
N_RATIOS = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5)
COOLDOWN_SEC = 60 * 60

ST_LEN = 10
ST_MULT = 2.5

RANDOM_MULT = 3
RANDOM_SEED = 20261003
BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261003

DEFAULT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
CKPT_NAME = "s013_ckpt.json"
