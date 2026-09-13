#!/usr/bin/env python3
"""
S003 Phase 2.1 — Download historical OHLCV candles from Delta Exchange India.

Public endpoint only (no HMAC). Standalone under backtest/ — does not import backend.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://api.india.delta.exchange"
CANDLES_PATH = "/v2/history/candles"
IST = ZoneInfo("Asia/Kolkata")
MAX_PER_REQUEST = 4000
SLEEP_BETWEEN_S = 0.25
FORMING_BUFFER_S = 10
DATA_DIR = Path(__file__).resolve().parent / "data_1m"

RESOLUTION_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
}

CSV_COLUMNS = [
    "open_time_unix",
    "open_time_utc",
    "open_time_ist",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


def _fmt_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _date_tag(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _is_closed(open_unix: int, res_sec: int, now: float) -> bool:
    return open_unix + res_sec <= now - FORMING_BUFFER_S


def _row_from_api(raw: dict[str, Any]) -> dict[str, Any] | None:
    if "time" not in raw:
        return None
    ts = int(raw["time"])
    return {
        "open_time_unix": ts,
        "open_time_utc": _fmt_utc(ts),
        "open_time_ist": _fmt_ist(ts),
        "open": float(raw["open"]),
        "high": float(raw["high"]),
        "low": float(raw["low"]),
        "close": float(raw["close"]),
        "volume": float(raw.get("volume") or 0.0),
    }


def _output_stem(symbol: str, resolution: str, first_ts: int, last_ts: int) -> str:
    return f"{symbol}_{resolution}_{_date_tag(first_ts)}_{_date_tag(last_ts)}"


def _find_existing_csv(symbol: str, resolution: str) -> Path | None:
    if not DATA_DIR.is_dir():
        return None
    matches = sorted(DATA_DIR.glob(f"{symbol}_{resolution}_*.csv"))
    return matches[-1] if matches else None


def _load_csv(path: Path) -> dict[int, dict[str, Any]]:
    by_ts: dict[int, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = int(row["open_time_unix"])
            by_ts[ts] = {
                "open_time_unix": ts,
                "open_time_utc": row["open_time_utc"],
                "open_time_ist": row["open_time_ist"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
    return by_ts


def _write_csv(path: Path, by_ts: dict[int, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [by_ts[k] for k in sorted(by_ts)]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _gap_report(
    by_ts: dict[int, dict[str, Any]],
    res_sec: int,
) -> str:
    if not by_ts:
        return "No candles downloaded.\n"

    times = sorted(by_ts)
    first, last = times[0], times[-1]
    actual = len(times)
    span_minutes = (last - first) // res_sec
    expected = span_minutes + 1
    missing = expected - actual

    gaps: list[tuple[int, int, int]] = []
    for i in range(1, len(times)):
        prev, cur = times[i - 1], times[i]
        step = (cur - prev) // res_sec
        if step > 1:
            gap_start = prev + res_sec
            gap_end = cur - res_sec
            gap_len = step - 1
            gaps.append((gap_start, gap_end, gap_len))

    gaps.sort(key=lambda g: g[2], reverse=True)
    top10 = gaps[:10]

    # Days (IST calendar) with >60 missing minutes
    missing_by_day: dict[str, int] = {}
    time_set = set(times)
    t = first
    while t <= last:
        if t not in time_set:
            day = datetime.fromtimestamp(t, tz=IST).strftime("%Y-%m-%d")
            missing_by_day[day] = missing_by_day.get(day, 0) + 1
        t += res_sec
    bad_days = sorted(
        ((d, n) for d, n in missing_by_day.items() if n > 60),
        key=lambda x: x[0],
    )

    lines: list[str] = []
    lines.append("=== S003 candle download gap report ===")
    lines.append(f"total candles downloaded: {actual}")
    lines.append(f"first candle: {_fmt_utc(first)} | {_fmt_ist(first)}")
    lines.append(f"last  candle: {_fmt_utc(last)} | {_fmt_ist(last)}")
    lines.append(f"expected candles for span: {expected}")
    lines.append(f"actual candles: {actual}")
    lines.append(f"missing minutes (bars): {missing}")
    lines.append("")
    lines.append("10 largest gaps:")
    if not top10:
        lines.append("  (none)")
    else:
        for i, (gs, ge, glen) in enumerate(top10, 1):
            lines.append(
                f"  {i}. {_fmt_ist(gs)} → {_fmt_ist(ge)}  ({glen} minutes)"
            )
    lines.append("")
    lines.append("Days with >60 missing minutes (IST):")
    if not bad_days:
        lines.append("  (none)")
    else:
        for day, n in bad_days:
            lines.append(f"  {day}: {n} missing minutes")
    lines.append("")
    return "\n".join(lines)


async def _fetch_page(
    client: httpx.AsyncClient,
    *,
    symbol: str,
    resolution: str,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": start,
        "end": end,
    }
    url = f"{BASE_URL}{CANDLES_PATH}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Tradeict-S003-CandleDownloader/1.0",
    }
    for attempt in range(4):
        try:
            resp = await client.get(url, params=params, headers=headers, timeout=60.0)
            resp.raise_for_status()
            payload = resp.json()
            result = payload.get("result") or []
            if not isinstance(result, list):
                return []
            return [r for r in result if isinstance(r, dict)]
        except (httpx.HTTPError, ValueError) as exc:
            if attempt >= 3:
                raise RuntimeError(
                    f"candles fetch failed start={start} end={end}: {exc}"
                ) from exc
            await asyncio.sleep(1.0 * (attempt + 1))
    return []


async def download(
    *,
    months: int,
    symbol: str,
    resolution: str,
) -> Path:
    if resolution not in RESOLUTION_SECONDS:
        raise ValueError(f"resolution must be one of {sorted(RESOLUTION_SECONDS)}")
    res_sec = RESOLUTION_SECONDS[resolution]
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now = time.time()
    target_start = int(
        (
            datetime.now(timezone.utc)
            - timedelta(days=int(round(months * 365.25 / 12)))
        ).timestamp()
    )
    # Align target_start down to resolution boundary
    target_start = target_start - (target_start % res_sec)

    by_ts: dict[int, dict[str, Any]] = {}
    existing = _find_existing_csv(symbol, resolution)
    if existing is not None:
        print(f"Resuming from existing file: {existing}")
        by_ts = _load_csv(existing)
        print(f"  loaded {len(by_ts)} candles; oldest={min(by_ts) if by_ts else 'n/a'}")

    # Page end: if resuming, continue from oldest already stored
    if by_ts:
        page_end = min(by_ts) - res_sec
    else:
        page_end = int(now)

    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(transport=transport, timeout=60.0) as client:
        pages = 0
        while page_end > target_start:
            page_start = max(target_start, page_end - MAX_PER_REQUEST * res_sec)
            raw_rows = await _fetch_page(
                client,
                symbol=symbol,
                resolution=resolution,
                start=page_start,
                end=page_end,
            )
            pages += 1

            new_count = 0
            oldest_in_page: int | None = None
            for raw in raw_rows:
                row = _row_from_api(raw)
                if row is None:
                    continue
                ts = int(row["open_time_unix"])
                if ts < target_start:
                    continue
                if not _is_closed(ts, res_sec, now):
                    continue
                if ts not in by_ts:
                    new_count += 1
                by_ts[ts] = row
                if oldest_in_page is None or ts < oldest_in_page:
                    oldest_in_page = ts

            print(
                f"page {pages}: start={page_start} end={page_end} "
                f"api={len(raw_rows)} new={new_count} total={len(by_ts)}"
            )

            if oldest_in_page is None:
                print("No candles in page — stopping.")
                break
            if new_count == 0 and oldest_in_page >= page_start:
                # Overlap-only page while still above target — still step back
                pass

            next_end = oldest_in_page - res_sec
            if next_end >= page_end:
                print("Pagination did not move backwards — stopping.")
                break
            page_end = next_end

            if page_end < target_start:
                break

            # Checkpoint every page so an interrupt is resumable
            if by_ts:
                ck_path = DATA_DIR / f"{symbol}_{resolution}_partial.csv"
                _write_csv(ck_path, by_ts)

            await asyncio.sleep(SLEEP_BETWEEN_S)

    if not by_ts:
        raise RuntimeError("Download produced zero candles")

    # Drop any still-forming bar that slipped in
    now = time.time()
    by_ts = {
        ts: row
        for ts, row in by_ts.items()
        if _is_closed(ts, res_sec, now) and ts >= target_start
    }

    first_ts, last_ts = min(by_ts), max(by_ts)
    final_path = DATA_DIR / f"{_output_stem(symbol, resolution, first_ts, last_ts)}.csv"
    _write_csv(final_path, by_ts)

    # Clean partial / superseded dated files for this symbol+resolution
    for p in DATA_DIR.glob(f"{symbol}_{resolution}_*.csv"):
        if p.resolve() != final_path.resolve():
            try:
                p.unlink()
            except OSError:
                pass
    partial = DATA_DIR / f"{symbol}_{resolution}_partial.csv"
    if partial.exists() and partial.resolve() != final_path.resolve():
        try:
            partial.unlink()
        except OSError:
            pass

    report = _gap_report(by_ts, res_sec)
    report_path = final_path.with_suffix(".txt")
    report_path.write_text(report, encoding="utf-8")
    print()
    print(report)
    print(f"CSV:    {final_path}")
    print(f"Report: {report_path}")
    return final_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download Delta India historical candles for S003 backtest"
    )
    p.add_argument("--months", type=int, default=12, help="Months of history (default 12)")
    p.add_argument("--symbol", type=str, default="BTCUSD")
    p.add_argument(
        "--resolution",
        type=str,
        default="1m",
        choices=sorted(RESOLUTION_SECONDS),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.months < 1:
        print("--months must be >= 1", file=sys.stderr)
        return 2
    try:
        asyncio.run(
            download(
                months=int(args.months),
                symbol=str(args.symbol).upper(),
                resolution=str(args.resolution),
            )
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
