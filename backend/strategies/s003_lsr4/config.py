# config.py — S003 Strategy3Config dataclass + DB load/save helpers

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

import pytz

# NOTE: Do not import sqlalchemy / backend.models at module top-level.
# indicators + lsr4 must stay usable from the Phase-2 backtest harness.


RESET_KEYS = frozenset(
    {
        "timeframe",
        "vwap_anchor_tz",
        "sweep_len",
        "adx_len",
        "atr_len",
        "rsi_len",
        "vol_len",
    }
)


@dataclass
class Strategy3Config:
    """Runtime config mirror of strategy3_config singleton row."""

    enabled: bool = False
    symbol: str = "BTCUSD"
    timeframe: str = "1m"
    vwap_anchor_tz: str = "Asia/Kolkata"
    sweep_len: int = 20
    max_wait: int = 6
    confirm_frac: float = 0.5
    adx_len: int = 14
    adx_trend: float = 28.0
    exh_vol_mult: float = 2.0
    exh_rng_mult: float = 1.8
    adx_fall: bool = True
    wick_pct: float = 0.45
    vol_len: int = 20
    vol_mult: float = 1.5
    atr_len: int = 14
    ext_mult: float = 1.2
    rsi_len: int = 14
    rsi_ob: float = 62.0
    rsi_os: float = 38.0
    min_score: int = 3
    cooldown_bars: int = 10
    cooldown_atr: float = 1.0
    allow_long: bool = True
    allow_short: bool = True
    allow_range_mode: bool = True
    allow_exhaustion_mode: bool = True
    # Runtime-only (not a DB column): product tick size from Delta.
    tick_size: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("tick_size", None)
        return d

    def fingerprint_reset(self) -> tuple[Any, ...]:
        return tuple(getattr(self, k) for k in sorted(RESET_KEYS))


def timeframe_seconds(tf: str) -> int:
    mapping = {
        "5s": 5,
        "1m": 60,
        "3m": 180,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "2h": 7200,
        "4h": 14400,
        "6h": 21600,
        "1d": 86400,
        "1w": 604800,
    }
    key = str(tf).strip().lower()
    if key not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf}")
    return mapping[key]


def validate_strategy3_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate POST /config body; raise ValueError with clear message on failure."""
    out: dict[str, Any] = {}
    allowed_tf = {"1m", "3m", "5m", "15m"}

    def _get(key: str, default: Any = None) -> Any:
        return payload[key] if key in payload else default

    if "timeframe" in payload:
        tf = str(payload["timeframe"]).strip()
        if tf not in allowed_tf:
            raise ValueError(f"timeframe must be one of {sorted(allowed_tf)}")
        out["timeframe"] = tf

    if "vwap_anchor_tz" in payload:
        tz = str(payload["vwap_anchor_tz"]).strip()
        try:
            pytz.timezone(tz)
        except Exception as exc:
            raise ValueError(f"vwap_anchor_tz is not a valid pytz timezone: {tz}") from exc
        out["vwap_anchor_tz"] = tz

    int_ranges = {
        "sweep_len": (2, 200),
        "max_wait": (1, 100),
        "adx_len": (2, 100),
        "vol_len": (1, 500),
        "atr_len": (2, 100),
        "rsi_len": (2, 100),
        "min_score": (1, 5),
        "cooldown_bars": (0, 500),
    }
    float_ranges = {
        "confirm_frac": (0.0, 1.0),
        "adx_trend": (0.0, 100.0),
        "exh_vol_mult": (0.0, 20.0),
        "exh_rng_mult": (0.0, 20.0),
        "wick_pct": (0.0, 1.0),
        "vol_mult": (0.0, 20.0),
        "ext_mult": (0.0, 20.0),
        "rsi_ob": (50.0, 100.0),
        "rsi_os": (0.0, 50.0),
        "cooldown_atr": (0.0, 20.0),
    }
    bool_keys = (
        "enabled",
        "adx_fall",
        "allow_long",
        "allow_short",
        "allow_range_mode",
        "allow_exhaustion_mode",
    )
    str_keys = ("symbol",)

    for key, (lo, hi) in int_ranges.items():
        if key not in payload:
            continue
        try:
            val = int(payload[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if val < lo or val > hi:
            raise ValueError(f"{key} must be in [{lo}, {hi}]")
        out[key] = val

    for key, (lo, hi) in float_ranges.items():
        if key not in payload:
            continue
        try:
            val = float(payload[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a number") from exc
        if val < lo or val > hi:
            raise ValueError(f"{key} must be in [{lo}, {hi}]")
        out[key] = val

    for key in bool_keys:
        if key not in payload:
            continue
        out[key] = bool(payload[key])

    for key in str_keys:
        if key not in payload:
            continue
        val = str(payload[key]).strip()
        if not val:
            raise ValueError(f"{key} must be non-empty")
        out[key] = val

    # Reject unknown keys that look like config mistakes (allow only known fields)
    known = {f.name for f in fields(Strategy3Config)} - {"tick_size"}
    known |= {"enabled", "symbol", "timeframe"}
    unknown = sorted(k for k in payload.keys() if k not in known)
    if unknown:
        raise ValueError(f"Unknown config keys: {unknown}")

    return out


def config_from_row(row: Any, *, tick_size: float | None = None) -> Strategy3Config:
    cfg = Strategy3Config(
        enabled=bool(row.enabled),
        symbol=str(row.symbol or "BTCUSD"),
        timeframe=str(row.timeframe or "1m"),
        vwap_anchor_tz=str(row.vwap_anchor_tz or "Asia/Kolkata"),
        sweep_len=int(row.sweep_len),
        max_wait=int(row.max_wait),
        confirm_frac=float(row.confirm_frac),
        adx_len=int(row.adx_len),
        adx_trend=float(row.adx_trend),
        exh_vol_mult=float(row.exh_vol_mult),
        exh_rng_mult=float(row.exh_rng_mult),
        adx_fall=bool(row.adx_fall),
        wick_pct=float(row.wick_pct),
        vol_len=int(row.vol_len),
        vol_mult=float(row.vol_mult),
        atr_len=int(row.atr_len),
        ext_mult=float(row.ext_mult),
        rsi_len=int(row.rsi_len),
        rsi_ob=float(row.rsi_ob),
        rsi_os=float(row.rsi_os),
        min_score=int(row.min_score),
        cooldown_bars=int(row.cooldown_bars),
        cooldown_atr=float(row.cooldown_atr),
        allow_long=bool(row.allow_long),
        allow_short=bool(row.allow_short),
        allow_range_mode=bool(row.allow_range_mode),
        allow_exhaustion_mode=bool(row.allow_exhaustion_mode),
    )
    if tick_size is not None:
        cfg.tick_size = float(tick_size)
    return cfg


def get_or_create_strategy3_config(db: Any) -> Any:
    from backend.models import Strategy3ConfigRow

    row = db.query(Strategy3ConfigRow).filter(Strategy3ConfigRow.id == 1).first()
    if row is None:
        row = Strategy3ConfigRow(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def get_or_create_strategy3_engine_state(db: Any) -> Any:
    from backend.models import Strategy3EngineState

    row = db.query(Strategy3EngineState).filter(Strategy3EngineState.id == 1).first()
    if row is None:
        row = Strategy3EngineState(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def load_arm_rows(db: Any) -> list[dict[str, Any]]:
    from backend.models import Strategy3ArmState

    rows = db.query(Strategy3ArmState).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "side": r.side,
                "armed": bool(r.armed),
                "signal_extreme": r.signal_extreme,
                "confirm_level": r.confirm_level,
                "arm_candle_time": r.arm_candle_time,
                "bars_elapsed": int(r.bars_elapsed or 0),
                "mode": r.mode,
                "score": r.score,
                "atr_at_arm": r.atr_at_arm,
                "adx_at_arm": r.adx_at_arm,
            }
        )
    return out


def persist_arm_state(db: Any, arm_rows: list[dict[str, Any]]) -> None:
    from backend.core.time_utils import get_utc_now
    from backend.models import Strategy3ArmState

    now = get_utc_now()
    by_side = {str(r["side"]).upper(): r for r in arm_rows}
    for side in ("TOP", "BOTTOM"):
        row = (
            db.query(Strategy3ArmState)
            .filter(Strategy3ArmState.side == side)
            .first()
        )
        data = by_side.get(side)
        if data is None:
            if row is None:
                row = Strategy3ArmState(side=side, armed=False)
                db.add(row)
            row.armed = False
            row.signal_extreme = None
            row.confirm_level = None
            row.arm_candle_time = None
            row.bars_elapsed = 0
            row.mode = None
            row.score = None
            row.atr_at_arm = None
            row.adx_at_arm = None
            row.updated_at = now
            continue
        if row is None:
            row = Strategy3ArmState(side=side)
            db.add(row)
        row.armed = bool(data.get("armed"))
        row.signal_extreme = data.get("signal_extreme")
        row.confirm_level = data.get("confirm_level")
        row.arm_candle_time = data.get("arm_candle_time")
        row.bars_elapsed = int(data.get("bars_elapsed") or 0)
        row.mode = data.get("mode")
        row.score = data.get("score")
        row.atr_at_arm = data.get("atr_at_arm")
        row.adx_at_arm = data.get("adx_at_arm")
        row.updated_at = now
    db.commit()


def persist_engine_state(db: Any, engine_row: dict[str, Any]) -> None:
    from backend.core.time_utils import get_utc_now

    row = get_or_create_strategy3_engine_state(db)
    row.last_processed_candle_time = engine_row.get("last_processed_candle_time")
    row.last_signal_candle_time = engine_row.get("last_signal_candle_time")
    row.last_signal_price = engine_row.get("last_signal_price")
    row.candles_processed = int(engine_row.get("candles_processed") or 0)
    row.vwap_session_date = engine_row.get("vwap_session_date")
    row.warm = bool(engine_row.get("warm", False))
    row.last_error = engine_row.get("last_error")
    row.updated_at = get_utc_now()
    db.commit()


def insert_signal(db: Any, signal: Any, notes: str | None = None) -> bool:
    """Insert signal; return False if unique constraint blocked a duplicate."""
    from sqlalchemy.exc import IntegrityError

    from backend.core.time_utils import get_utc_now
    from backend.models import Strategy3Signal

    row = Strategy3Signal(
        direction=signal.direction,
        mode=signal.mode,
        score=int(signal.score),
        signal_candle_time=signal.signal_candle_time,
        confirm_candle_time=signal.confirm_candle_time,
        confirm_price=float(signal.confirm_price),
        signal_extreme=float(signal.signal_extreme),
        atr_at_arm=float(signal.atr_at_arm),
        atr_at_confirm=float(signal.atr_at_confirm),
        adx_at_signal=float(signal.adx_at_signal),
        acted_on=False,
        notes=notes,
        created_at=get_utc_now(),
    )
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
