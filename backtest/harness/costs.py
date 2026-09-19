"""Unified fill / fee / slippage helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

_BACKTEST = Path(__file__).resolve().parent.parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s001_income_engine as eng  # noqa: E402
from slippage_model import load_slip_table, slip_pct  # noqa: E402

from backtest.harness.config import CONTRACT_VALUE

SLIP_FLAT = 0.0165


def ensure_slip_table() -> None:
    load_slip_table()


def slip_frac(
    premium: float,
    dte: float | int,
    *,
    slip_model: str = "bucketed",
    slip_mult: float = 1.0,
) -> float:
    if slip_model == "flat165":
        base = SLIP_FLAT
    else:
        base = float(slip_pct(float(premium), int(max(0, round(float(dte)))))) / 100.0
    return max(0.0, float(base) * float(slip_mult))


def option_fee(premium: float, index: float, qty_lots: int) -> float:
    return float(eng.option_fee(premium, index, qty_lots))


def fill(
    mark: float,
    side: Literal["buy", "sell"],
    premium: float | None = None,
    dte: float | int = 0,
    *,
    slip_model: str = "bucketed",
    slip_mult: float = 1.0,
) -> tuple[float, float, float]:
    """
    Return (fill_price, fee_placeholder_index_needed, applied_slip_frac).
    Fee still needs index via option_fee separately — returns slip_frac for caller.
    """
    prem = float(premium if premium is not None else mark)
    sf = slip_frac(prem, dte, slip_model=slip_model, slip_mult=slip_mult)
    m = float(mark)
    if side == "sell":
        return m * (1.0 - sf), sf, sf
    return m * (1.0 + sf), sf, sf


def fill_price(
    mark: float,
    side: Literal["buy", "sell"],
    *,
    dte: float | int = 0,
    slip_model: str = "bucketed",
    slip_mult: float = 1.0,
) -> tuple[float, float]:
    """Return (fill_price, slip_frac)."""
    sf = slip_frac(mark, dte, slip_model=slip_model, slip_mult=slip_mult)
    if side == "sell":
        return float(mark) * (1.0 - sf), sf
    return float(mark) * (1.0 + sf), sf


def qty_btc(qty: int) -> float:
    return abs(int(qty)) * CONTRACT_VALUE


def signed_pnl(entry_fill: float, exit_mark_or_fill: float, qty: int, *, is_long: bool) -> float:
    q = qty_btc(qty)
    if is_long:
        return (exit_mark_or_fill - entry_fill) * q
    return (entry_fill - exit_mark_or_fill) * q
