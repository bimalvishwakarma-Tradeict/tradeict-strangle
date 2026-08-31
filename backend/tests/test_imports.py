# test_imports.py — Import smoke tests (catch SyntaxError / IndentationError at collection)

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_app_imports() -> None:
    import backend.main  # noqa: F401


_ENGINE_MODULES = [
    "backend.engine.bot_engine",
    "backend.engine.auto_trade_engine",
    "backend.engine.hedge_lifecycle",
    "backend.engine.mirror_engine",
    "backend.engine.trade_reconcile",
]

_STRATEGY_MODULES = [
    "backend.strategies.s001_short_strangle.logic",
    "backend.strategies.s001_short_strangle.adjustment",
    "backend.strategies.s001_short_strangle.premium_decay",
]

_CORE_MODULES = [
    "backend.core.realized_booking",
    "backend.core.backfill_realized",
    "backend.core.entry_basis",
]

_API_MODULES = [
    "backend.api.routes_account",
    "backend.api.routes_auto_trade",
    "backend.api.routes_backtest",
    "backend.api.routes_hedge",
    "backend.api.routes_logs",
    "backend.api.routes_slave",
    "backend.api.routes_strategy",
    "backend.api.routes_structures",
    "backend.api.routes_trade",
    "backend.api.routes_ws",
]

_ALL_IMPORT_MODULES = (
    _ENGINE_MODULES + _STRATEGY_MODULES + _CORE_MODULES + _API_MODULES
)


@pytest.mark.parametrize("module_name", _ALL_IMPORT_MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
