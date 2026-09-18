#!/usr/bin/env python3
"""
Verify slippage_model predictions against real Delta order-history fills.

Read-only: opens marks SQLite with mode=ro. Does not modify live bot code.

Usage:
  python backtest/verify_my_fills.py --csv "C:/Users/.../Delta-TransactionLog-OrderHistory.csv"

No print() — logging + console write via logger/sys.stdout after report build,
and file write to backtest/results/verify_my_fills.txt.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

import s001_income_engine as eng  # noqa: E402
from options_trades import parse_symbol  # noqa: E402
from slippage_model import (  # noqa: E402
    dte_bucket,
    load_slip_table,
    prem_bucket,
    slip_pct,
)

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
MARKS_DIR = _BACKTEST / "cache" / "option_marks"
DATA_1M_DIR = _BACKTEST / "data_1m"
OUT_PATH = _BACKTEST / "results" / "verify_my_fills.txt"
MARK_TOL_SEC = 60

OPTION_RE = re.compile(r"^[CP]-BTC-\d+-\d{6}$", re.IGNORECASE)
FUTURES_RE = re.compile(r"^BTCUSD$", re.IGNORECASE)

logger = logging.getLogger("verify_my_fills")


@dataclass
class FillRow:
    time_raw: str
    ts: int
    fill_date_ist: date
    symbol: str
    side: str  # buy|sell
    qty: float
    filled: float
    exec_price: float
    order_type: str
    fee_actual: float
    strike: float
    expiry: date
    dte: int
    mark: float | None
    mark_ts: int | None
    miss_reason: str
    diff_pct: float | None
    cost_pct: float | None  # + = adverse (kharcha)
    prem_b: str
    dte_b: str
    model_slip: float | None
    fee_model: float | None
    fee_err_pct: float | None
    spot: float | None


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def median_or_nan(vals: list[float]) -> float:
    return float(statistics.median(vals)) if vals else float("nan")


def mean_or_nan(vals: list[float]) -> float:
    return float(statistics.fmean(vals)) if vals else float("nan")


def parse_time_ist(raw: str) -> datetime:
    """
    Examples:
      2026-09-16 17:39:08.114807+05:30 IST Asia/Kolkata
      2026-09-16 17:39:08+05:30
    """
    s = raw.strip()
    # drop trailing ' IST Asia/Kolkata' etc.
    if " IST" in s:
        s = s.split(" IST", 1)[0].strip()
    # try fromisoformat (handles +05:30)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except ValueError:
            continue
    raise ValueError(f"unparseable Time: {raw!r}")


def parse_filled_qty(filled_remaining: str) -> float:
    """'60.00000000/0.00000000' → 60.0 (filled part)."""
    s = (filled_remaining or "").strip()
    if not s or "/" not in s:
        return 0.0
    left = s.split("/", 1)[0].strip()
    try:
        return float(left)
    except ValueError:
        return 0.0


def parse_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_spot_map() -> dict[int, float]:
    files = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    if not files:
        return {}
    out: dict[int, float] = {}
    with files[-1].open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[int(row["open_time_unix"])] = float(row["close"])
    return out


def spot_at(spot_map: dict[int, float], ts: int) -> float | None:
    minute = (ts // 60) * 60
    if minute in spot_map:
        return spot_map[minute]
    for d in range(-MARK_TOL_SEC, MARK_TOL_SEC + 1, 60):
        if minute + d in spot_map:
            return spot_map[minute + d]
    return None


class MarksLookup:
    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self.available = sorted(
            p.stem.replace("marks_", "")
            for p in MARKS_DIR.glob("marks_*.sqlite")
        )

    def _conn(self, d: date) -> sqlite3.Connection | None:
        ym = f"{d.year:04d}-{d.month:02d}"
        if ym not in self.available:
            return None
        if ym not in self._conns:
            path = MARKS_DIR / f"marks_{ym}.sqlite"
            self._conns[ym] = sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro", uri=True
            )
        return self._conns[ym]

    def mark_near(
        self, symbol: str, ts: int, expiry: date
    ) -> tuple[float | None, int | None, str]:
        """Return (close, mark_ts, miss_reason). Prefer exact minute, else ±60s."""
        # try expiry month and fill month
        days_try = [
            datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date(),
            expiry,
        ]
        minute = (ts // 60) * 60
        seen: set[str] = set()
        for d in days_try:
            ym = f"{d.year:04d}-{d.month:02d}"
            if ym in seen:
                continue
            seen.add(ym)
            conn = self._conn(d)
            if conn is None:
                continue
            row = conn.execute(
                "SELECT ts, close FROM marks WHERE symbol=? AND ts=? LIMIT 1",
                (symbol, minute),
            ).fetchone()
            if row is not None and row[1] is not None and float(row[1]) > 0:
                return float(row[1]), int(row[0]), ""
            row = conn.execute(
                """
                SELECT ts, close FROM marks
                WHERE symbol=? AND ts BETWEEN ? AND ?
                  AND close IS NOT NULL AND close > 0
                ORDER BY ABS(ts - ?) LIMIT 1
                """,
                (symbol, minute - MARK_TOL_SEC, minute + MARK_TOL_SEC, minute),
            ).fetchone()
            if row is not None:
                return float(row[1]), int(row[0]), ""
        if not any(
            f"{d.year:04d}-{d.month:02d}" in self.available for d in days_try
        ):
            return None, None, "no_marks_shard"
        return None, None, "no_mark_within_60s"

    def close(self) -> None:
        for c in self._conns.values():
            c.close()
        self._conns.clear()


def load_fills(csv_path: Path) -> tuple[list[dict[str, str]], int, int]:
    """
    Returns (option_closed_filled_rows, n_futures_skipped, n_other_skipped).
    """
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header")
        rows = list(reader)

    option_rows: list[dict[str, str]] = []
    n_fut = 0
    n_other = 0
    for row in rows:
        contract = (row.get("Contract") or "").strip()
        status = (row.get("Status") or "").strip().lower()
        filled = parse_filled_qty(row.get("Filled/Remaining") or "")
        if FUTURES_RE.match(contract) or contract.upper() == "BTCUSD":
            n_fut += 1
            continue
        if not OPTION_RE.match(contract):
            n_other += 1
            continue
        if status != "closed":
            n_other += 1
            continue
        if filled <= 0:
            n_other += 1
            continue
        option_rows.append(row)
    return option_rows, n_fut, n_other


def analyze(csv_path: Path) -> list[str]:
    load_slip_table()
    spot_map = load_spot_map()
    marks = MarksLookup()
    raw_rows, n_fut, n_other = load_fills(csv_path)

    lines: list[str] = []
    emit(lines, "===== VERIFY MY FILLS vs MARKS / SLIPPAGE MODEL =====")
    emit(lines, f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    emit(lines, f"csv={csv_path}")
    emit(lines, f"marks_dir={MARKS_DIR}")
    emit(lines, f"marks_months={marks.available}")
    emit(lines, f"spot_bars={len(spot_map)}")
    emit(lines)
    emit(lines, f"futures_BTCUSD_rows_seen={n_fut} (excluded from analysis)")
    emit(lines, f"other_skipped_rows={n_other}")
    emit(lines, f"option_closed_filled_rows={len(raw_rows)}")
    emit(lines)

    fills: list[FillRow] = []
    miss_counts: dict[str, int] = defaultdict(int)

    for row in raw_rows:
        contract = (row.get("Contract") or "").strip()
        parsed = parse_symbol(contract)
        if parsed is None:
            miss_counts["bad_symbol"] += 1
            continue
        try:
            dt_ist = parse_time_ist(row.get("Time") or "")
        except ValueError:
            miss_counts["bad_time"] += 1
            continue
        ts = int(dt_ist.astimezone(UTC).timestamp())
        side = (row.get("Side") or "").strip().lower()
        if side not in ("buy", "sell"):
            miss_counts["bad_side"] += 1
            continue
        exec_px = parse_float(row.get("Exec.Price"))
        if exec_px is None or exec_px <= 0:
            miss_counts["bad_exec_price"] += 1
            continue
        filled = parse_filled_qty(row.get("Filled/Remaining") or "")
        qty = parse_float(row.get("Qty")) or filled
        fee_act = parse_float(row.get("Trading Fees")) or 0.0
        order_type = (row.get("Order Type") or "").strip() or "unknown"

        fill_day = dt_ist.date()
        dte = (parsed.expiry_date - fill_day).days
        if dte < 0:
            dte = 0

        mark, mark_ts, miss = marks.mark_near(contract, ts, parsed.expiry_date)
        diff_pct: float | None = None
        cost_pct: float | None = None
        model: float | None = None
        if mark is None or mark <= 0:
            miss_counts[miss or "no_mark"] += 1
            miss_reason = miss or "no_mark"
        else:
            miss_reason = ""
            diff_pct = (exec_px - mark) / mark * 100.0
            # buy: +diff = kharcha; sell: -diff = kharcha
            cost_pct = diff_pct if side == "buy" else -diff_pct
            try:
                model = float(slip_pct(exec_px, dte))
            except Exception as exc:  # noqa: BLE001
                logger.warning("slip_pct failed: %s", exc)
                model = None

        spot = spot_at(spot_map, ts)
        fee_model: float | None = None
        fee_err: float | None = None
        if spot is not None and spot > 0 and qty > 0:
            fee_model = eng.option_fee(exec_px, spot, int(round(qty)))
            if fee_act > 0 and fee_model is not None:
                fee_err = (fee_act - fee_model) / fee_act * 100.0
            elif fee_act == 0 and fee_model == 0:
                fee_err = 0.0

        fills.append(
            FillRow(
                time_raw=row.get("Time") or "",
                ts=ts,
                fill_date_ist=fill_day,
                symbol=contract,
                side=side,
                qty=float(qty),
                filled=filled,
                exec_price=exec_px,
                order_type=order_type,
                fee_actual=fee_act,
                strike=parsed.strike,
                expiry=parsed.expiry_date,
                dte=dte,
                mark=mark,
                mark_ts=mark_ts,
                miss_reason=miss_reason,
                diff_pct=diff_pct,
                cost_pct=cost_pct,
                prem_b=prem_bucket(exec_px),
                dte_b=dte_bucket(dte),
                model_slip=model,
                fee_model=fee_model,
                fee_err_pct=fee_err,
                spot=spot,
            )
        )

    matched = [f for f in fills if f.mark is not None and f.diff_pct is not None]
    unmatched = [f for f in fills if f.mark is None]

    emit(lines, "----- MATCH SUMMARY -----")
    emit(lines, f"fills_parsed={len(fills)}")
    emit(lines, f"matched_to_mark={len(matched)}")
    emit(lines, f"unmatched={len(unmatched)}")
    if miss_counts:
        emit(lines, "unmatched_reasons:")
        for k, v in sorted(miss_counts.items(), key=lambda x: -x[1]):
            emit(lines, f"  {k}={v}")
    emit(lines)

    buys = [f for f in matched if f.side == "buy"]
    sells = [f for f in matched if f.side == "sell"]
    emit(lines, "----- OVERALL median diff_pct (exec-mark)/mark*100 -----")
    emit(
        lines,
        f"buy  n={len(buys)}  median_diff_pct={median_or_nan([f.diff_pct for f in buys if f.diff_pct is not None]):.4f}  "
        f"median_cost_pct(+adverse)={median_or_nan([f.cost_pct for f in buys if f.cost_pct is not None]):.4f}",
    )
    emit(
        lines,
        f"sell n={len(sells)}  median_diff_pct={median_or_nan([f.diff_pct for f in sells if f.diff_pct is not None]):.4f}  "
        f"median_cost_pct(+adverse)={median_or_nan([f.cost_pct for f in sells if f.cost_pct is not None]):.4f}",
    )
    emit(lines)

    # Premium / DTE buckets vs model
    emit(
        lines,
        "----- MODEL vs ACTUAL (cost_pct median = adverse slip%) -----",
    )
    emit(
        lines,
        f"{'bucket':<22} {'n':>5} {'actual_med':>12} {'model':>10} {'gap(a-m)':>10}",
    )

    def bucket_table(
        key_fn: Any, label: str, order: list[str] | None = None
    ) -> None:
        emit(lines, f"-- by {label} --")
        groups: dict[str, list[FillRow]] = defaultdict(list)
        for f in matched:
            if f.cost_pct is None:
                continue
            groups[key_fn(f)].append(f)
        keys = order if order is not None else sorted(groups)
        for k in keys:
            rows = groups.get(k) or []
            if not rows:
                continue
            actual = median_or_nan([f.cost_pct for f in rows if f.cost_pct is not None])
            models = [f.model_slip for f in rows if f.model_slip is not None]
            # model is per-fill; use median of predictions in bucket
            model_m = median_or_nan([float(m) for m in models if m is not None])
            # also show slip_pct at bucket mid for reference via first row
            gap = (
                actual - model_m
                if not math.isnan(actual) and not math.isnan(model_m)
                else float("nan")
            )
            emit(
                lines,
                f"{k:<22} {len(rows):>5} {actual:>12.4f} {model_m:>10.4f} {gap:>10.4f}",
            )

    prem_order = ["<100", "100-300", "300-600", "600-900", "900+"]
    dte_order = ["0", "1", "2", "3-7", "8+"]
    bucket_table(lambda f: f.prem_b, "premium", prem_order)
    emit(lines)
    bucket_table(lambda f: f.dte_b, "DTE", dte_order)
    emit(lines)

    # Cross: dte|prem like calibrate
    emit(lines, "-- by DTE x premium (actual cost_pct vs model) --")
    emit(
        lines,
        f"{'dte|prem':<22} {'n':>5} {'actual_med':>12} {'model':>10} {'gap(a-m)':>10}",
    )
    cross: dict[str, list[FillRow]] = defaultdict(list)
    for f in matched:
        if f.cost_pct is None:
            continue
        cross[f"{f.dte_b}|{f.prem_b}"].append(f)
    for k in sorted(cross):
        rows = cross[k]
        actual = median_or_nan([f.cost_pct for f in rows if f.cost_pct is not None])
        model_m = median_or_nan(
            [float(f.model_slip) for f in rows if f.model_slip is not None]
        )
        gap = (
            actual - model_m
            if not math.isnan(actual) and not math.isnan(model_m)
            else float("nan")
        )
        emit(
            lines,
            f"{k:<22} {len(rows):>5} {actual:>12.4f} {model_m:>10.4f} {gap:>10.4f}",
        )
    emit(lines)

    # Order type
    emit(lines, "----- ORDER TYPE median cost_pct (adverse) -----")
    by_ot: dict[str, list[FillRow]] = defaultdict(list)
    for f in matched:
        if f.cost_pct is not None:
            by_ot[f.order_type].append(f)
    for ot in sorted(by_ot):
        rows = by_ot[ot]
        buys_ot = [f for f in rows if f.side == "buy"]
        sells_ot = [f for f in rows if f.side == "sell"]
        emit(
            lines,
            f"{ot}: n={len(rows)}  "
            f"median_cost={median_or_nan([f.cost_pct for f in rows if f.cost_pct is not None]):.4f}  "
            f"buy_n={len(buys_ot)} buy_med={median_or_nan([f.cost_pct for f in buys_ot if f.cost_pct is not None]):.4f}  "
            f"sell_n={len(sells_ot)} sell_med={median_or_nan([f.cost_pct for f in sells_ot if f.cost_pct is not None]):.4f}",
        )
    emit(lines)

    # Fees
    fee_rows = [f for f in fills if f.fee_err_pct is not None]
    emit(lines, "----- FEE CHECK: actual Trading Fees vs option_fee() -----")
    emit(lines, f"n_with_spot_and_fee={len(fee_rows)}")
    if fee_rows:
        emit(
            lines,
            f"median_fee_error_pct=(actual-model)/actual*100 = "
            f"{median_or_nan([f.fee_err_pct for f in fee_rows if f.fee_err_pct is not None]):.4f}",
        )
        emit(
            lines,
            f"median_actual_fee={median_or_nan([f.fee_actual for f in fee_rows]):.6f}  "
            f"median_model_fee={median_or_nan([f.fee_model for f in fee_rows if f.fee_model is not None]):.6f}",
        )
    else:
        emit(lines, "no fee comparisons (missing spot and/or fees)")
    emit(lines)

    # Sample unmatched
    if unmatched:
        emit(lines, "----- UNMATCHED SAMPLE (up to 15) -----")
        for f in unmatched[:15]:
            emit(
                lines,
                f"  {f.time_raw[:32]} {f.symbol} {f.side} exec={f.exec_price} "
                f"reason={f.miss_reason}",
            )
        emit(lines)

    marks.close()
    return lines


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(
        description="Verify slippage model vs real Delta option fills"
    )
    ap.add_argument(
        "--csv",
        type=str,
        required=True,
        help="Path to Delta order-history CSV",
    )
    args = ap.parse_args()
    csv_path = Path(args.csv)
    if not csv_path.is_file():
        raise SystemExit(f"CSV not found: {csv_path}")

    lines = analyze(csv_path)
    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()
    logger.info("wrote %s", OUT_PATH)


if __name__ == "__main__":
    main()
