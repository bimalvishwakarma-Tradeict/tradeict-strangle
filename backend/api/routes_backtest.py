# routes_backtest.py — /api/backtest/* endpoints for CSV strategy backtests

from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import sys
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Project root so `backtest.*` imports resolve (standalone package next to backend/)
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.data_loader import DataLoader
from backtest.engine import (
    BacktestEngine,
    day_result_to_dict,
    summary_for_api,
)
from backtest.strategy_sim import DayResult, StrategySimulator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["backtest"])


class CsvFilePayload(BaseModel):
    filename: str
    content_b64: str


class BacktestParams(BaseModel):
    trade_type: str = "straddle"
    expiry_type: str = "1DTE"
    entry_hour_ist: int = 10
    entry_minute_ist: int = 0
    quantity: int = 100
    trigger_pct: float = 150.0
    profit_target_pct: float = 50.0
    stoploss_pct: float = 100.0
    min_replacement_premium: float = 150.0
    conversion_equality_pct: float = 10.0
    target_premium_per_side: float = 350.0
    fee_per_leg_usd: float = 0.75
    slippage_pct: float = 2.0


class BacktestRunRequest(BaseModel):
    csv_files: list[CsvFilePayload] = Field(min_length=1)
    params: BacktestParams = Field(default_factory=BacktestParams)


class BacktestLocalRunRequest(BaseModel):
    data_dir: str = "backtest/data"
    params: BacktestParams = Field(default_factory=BacktestParams)


def _serialize_adj_log(adj_log: list) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for adj in adj_log or []:
        if not isinstance(adj, dict):
            continue
        row = dict(adj)
        minute = row.get("minute")
        if isinstance(minute, datetime):
            row["minute"] = minute.isoformat(sep=" ", timespec="minutes")
            row["time"] = minute.strftime("%H:%M")
        elif minute is not None:
            row["minute"] = str(minute)
            row["time"] = str(minute)[11:16] if len(str(minute)) >= 16 else str(minute)
        out.append(row)
    return out


def _day_to_dict(result: DayResult) -> dict[str, Any]:
    return day_result_to_dict(result)


def _build_summary(day_rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_days = len(day_rows)
    ok_days = [d for d in day_rows if d.get("data_ok")]
    data_ok_days = len(ok_days)

    wins = [d for d in ok_days if float(d.get("net_pnl") or 0) > 0]
    losses = [d for d in ok_days if float(d.get("net_pnl") or 0) < 0]

    win_rate = (len(wins) / data_ok_days * 100.0) if data_ok_days else 0.0
    avg_win = (
        sum(float(d["net_pnl"]) for d in wins) / len(wins) if wins else 0.0
    )
    avg_loss = (
        sum(float(d["net_pnl"]) for d in losses) / len(losses) if losses else 0.0
    )
    total_net = sum(float(d.get("net_pnl") or 0) for d in ok_days)
    max_dd = min((float(d.get("max_drawdown") or 0) for d in ok_days), default=0.0)
    cum = 0.0
    peak = 0.0
    equity_dd = 0.0
    for d in sorted(ok_days, key=lambda x: x.get("trade_date") or ""):
        cum += float(d.get("net_pnl") or 0)
        peak = max(peak, cum)
        equity_dd = min(equity_dd, cum - peak)
    if equity_dd < max_dd:
        max_dd = equity_dd

    return {
        "total_days": total_days,
        "data_ok_days": data_ok_days,
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_net_pnl": round(total_net, 2),
        "max_drawdown": round(max_dd, 2),
        "total_adjustments": sum(int(d.get("adjustments") or 0) for d in ok_days),
        "total_conversions": sum(int(d.get("conversions") or 0) for d in ok_days),
        "profit_target_count": sum(
            1 for d in ok_days if d.get("exit_reason") == "PROFIT_TARGET"
        ),
        "stoploss_count": sum(
            1 for d in ok_days if d.get("exit_reason") == "STOPLOSS"
        ),
        "pre_expiry_count": sum(
            1 for d in ok_days if d.get("exit_reason") == "PRE_EXPIRY"
        ),
    }


def _run_backtest_sync(
    csv_payloads: list[tuple[str, bytes]],
    params: BacktestParams,
) -> dict[str, Any]:
    """Blocking: decode CSVs, simulate each unique trade date, return payload."""
    loader = DataLoader()
    frames = []
    temp_paths: list[Path] = []

    try:
        for filename, raw_bytes in csv_payloads:
            suffix = Path(filename).suffix or ".csv"
            tmp = tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, prefix="bt_"
            )
            tmp.write(raw_bytes)
            tmp.flush()
            tmp.close()
            path = Path(tmp.name)
            temp_paths.append(path)
            logger.info("Loading backtest CSV %s (%s bytes)", filename, len(raw_bytes))
            frames.append(loader.load_csv(path))

        if not frames:
            raise ValueError("No CSV data loaded")

        import pandas as pd

        df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        trade_dates = sorted({d for d in df["ist_date"].unique() if d is not None})

        sim = StrategySimulator(
            trade_type=params.trade_type,
            expiry_type=params.expiry_type,
            entry_hour_ist=params.entry_hour_ist,
            entry_minute_ist=params.entry_minute_ist,
            quantity=params.quantity,
            trigger_pct=params.trigger_pct,
            profit_target_pct=params.profit_target_pct,
            stoploss_pct=params.stoploss_pct,
            min_replacement_premium=params.min_replacement_premium,
            conversion_equality_pct=params.conversion_equality_pct,
            target_premium_per_side=params.target_premium_per_side,
            fee_per_leg_usd=params.fee_per_leg_usd,
            slippage_pct=params.slippage_pct,
        )

        day_rows: list[dict[str, Any]] = []
        for trade_date in trade_dates:
            if not isinstance(trade_date, date):
                continue
            logger.info("Simulating trade_date=%s", trade_date)
            result = sim.simulate_day(df, trade_date)
            day_rows.append(_day_to_dict(result))

        summary = _build_summary(day_rows)
        return {"summary": summary, "days": day_rows}
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to delete temp CSV %s: %s", path, exc)


def _resolve_local_data_dir(data_dir: str) -> Path:
    """Resolve data_dir under project root; reject path escape."""
    raw = (data_dir or "backtest/data").strip() or "backtest/data"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()

    try:
        candidate.relative_to(_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="data_dir must be inside the project directory",
        ) from exc

    if not candidate.exists() or not candidate.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Data directory not found: {raw}",
        )
    return candidate


def _params_to_config(params: BacktestParams) -> dict[str, Any]:
    return params.model_dump()


@router.get("/status")
async def backtest_status() -> dict[str, str]:
    """Health ping for the backtest module."""
    return {"status": "ready"}


@router.post("/run")
async def run_backtest(payload: BacktestRunRequest) -> dict[str, Any]:
    """
    Run strategy backtest on one or more uploaded CSV files (base64).

    Heavy pandas work runs in a worker thread via asyncio.to_thread.
    """
    csv_payloads: list[tuple[str, bytes]] = []
    try:
        for item in payload.csv_files:
            name = (item.filename or "data.csv").strip() or "data.csv"
            try:
                raw = base64.b64decode(item.content_b64, validate=False)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid base64 for file '{name}': {exc}",
                ) from exc
            if not raw:
                raise HTTPException(
                    status_code=400,
                    detail=f"Empty content for file '{name}'",
                )
            csv_payloads.append((name, raw))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Backtest request parse failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await asyncio.to_thread(
            _run_backtest_sync, csv_payloads, payload.params
        )
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        logger.error("Backtest validation error: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.critical("Backtest run failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Backtest failed: {exc}"
        ) from exc


@router.post("/run-local")
async def run_backtest_local(payload: BacktestLocalRunRequest) -> StreamingResponse:
    """
    Run backtest from a local data_dir (no CSV upload).

    Streams NDJSON lines:
      {"type":"progress","current":1,"total":29,"date":"2026-06-01"}
      {"type":"complete","summary":{...},"days":[...]}
      {"type":"error","message":"..."}
    """
    data_path = _resolve_local_data_dir(payload.data_dir)
    config = _params_to_config(payload.params)
    config["data_dir"] = str(data_path)

    def event_stream() -> Iterator[str]:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue()

        def worker() -> None:
            try:
                engine = BacktestEngine(config)

                def on_progress(current: int, total: int, date_str: str) -> None:
                    q.put(
                        {
                            "type": "progress",
                            "current": current,
                            "total": total,
                            "date": date_str,
                        }
                    )

                results, summary = engine.run(
                    str(data_path), progress_callback=on_progress
                )
                day_rows = [day_result_to_dict(r) for r in results]
                q.put(
                    {
                        "type": "complete",
                        "summary": summary_for_api(summary),
                        "days": day_rows,
                    }
                )
            except Exception as exc:
                logger.critical("Local backtest failed: %s", exc, exc_info=True)
                q.put({"type": "error", "message": str(exc)})
            finally:
                q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = q.get()
            if item is None:
                break
            yield json.dumps(item, default=str) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
