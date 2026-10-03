"""S012G — whipsaw-reduction arms on S012F."""

from __future__ import annotations

from datetime import date

TF_MIN = 5
ST_MULTS = (3.5, 4.0, 4.5)
TGT = 2.5
ARMS = ("A", "B", "C", "D", "E", "F", "G")

ADX_LEN = 14
ADX_MIN = 25.0
ATR_LEN = 14
ATR_BUFFER = 0.3
CONFIRM_BARS = 2
COOLDOWN_SEC = 60 * 60

HTF_ST_LEN = 10
HTF_ST_MULT = 3.0

P2025_FROM = date(2025, 1, 1)
P2025_TO = date(2025, 12, 31)
P2026_FROM = date(2026, 1, 1)
P2026_TO = date(2026, 9, 30)

BOOTSTRAP_N = 5000
BOOTSTRAP_SEED = 20261003
RANDOM_MULT = 3
RANDOM_SEED = 20261002  # S012 pool seed; everything else = S012 except listed arms

PASS_ST = 4.0
PASS_NEIGHBOR = (3.5, 4.5)

CKPT_NAME = "s012g_ckpt.json"
DEFAULT_OUT = "backtest/strategies/s012g_whipsaw/runs"
DEFAULT_CSV = "backtest/data_1m/BTCUSD_1m_20240630_20260921.csv"
