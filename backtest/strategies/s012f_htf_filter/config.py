"""S012F — S012 + 1h/4h Supertrend agreement filter."""

from __future__ import annotations

from datetime import date

TF_MIN = 5
ST_MULTS = (3.0, 3.5, 4.0, 4.5, 5.0, 6.0)
TGT_MULTS = (1.5, 2.0, 2.5, 3.0)

HTF_ST_LEN = 10
HTF_ST_MULT = 3.0

P2025_FROM = date(2025, 1, 1)
P2025_TO = date(2025, 12, 31)
P2026_FROM = date(2026, 1, 1)
P2026_TO = date(2026, 9, 30)

PASS_ST = 4.0
PASS_TGT = 2.5
PASS_ST_NONNEG = (4.5, 5.0)

CKPT_NAME = "s012f_ckpt.json"
DEFAULT_OUT = "backtest/strategies/s012f_htf_filter/runs"
