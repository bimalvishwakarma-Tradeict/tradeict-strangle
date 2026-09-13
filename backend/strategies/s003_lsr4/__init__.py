# s003_lsr4 — Strategy S003 LSR4 signal engine (signal only, no orders)
#
# Keep this package import light: indicators must load without SQLAlchemy
# (Phase-2 backtest harness). Import lsr4 / config explicitly when needed.

__all__ = ["Candle", "LSR4Engine", "Signal"]


def __getattr__(name: str):
    if name in {"Candle", "LSR4Engine", "Signal"}:
        from backend.strategies.s003_lsr4 import lsr4 as _lsr4

        return getattr(_lsr4, name)
    raise AttributeError(name)
