"""Single Delta India REST entry for chart_app.

Concurrency cap 2, 429 Retry-After / exponential backoff, closed-page disk cache,
optional CHART_APP_FIXTURES=1 (no network).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / ".cache" / "candles"
FIXTURE_DIR = APP_DIR / "tests" / "fixtures"
DELTA_BASE = "https://api.india.delta.exchange"
CANDLES_PATH = "/v2/history/candles"
PAGE_CAP = 4000
RES_SEC = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}

logger = logging.getLogger("chart_app.delta")
_SEM = asyncio.Semaphore(2)
_REQ_N = 0
_CLIENT: httpx.AsyncClient | None = None
_FIXTURES: dict[str, list[dict[str, Any]]] | None = None
_FIXTURE_NOW: int | None = None


def fixtures_enabled() -> bool:
    return os.environ.get("CHART_APP_FIXTURES", "").strip().lower() in ("1", "true", "yes")


def chart_now() -> int:
    if fixtures_enabled():
        _load_fixtures()
        if _FIXTURE_NOW:
            return int(_FIXTURE_NOW)
    return int(time.time())


def fixture_now() -> int | None:
    if not fixtures_enabled():
        return None
    _load_fixtures()
    return _FIXTURE_NOW


def request_count() -> int:
    return _REQ_N


def _load_fixtures() -> None:
    global _FIXTURES, _FIXTURE_NOW
    if _FIXTURES is not None:
        return
    _FIXTURES = {}
    latest = 0
    for path in sorted(FIXTURE_DIR.glob("BTCUSD_*.json.gz")):
        tf = path.name.replace("BTCUSD_", "").replace(".json.gz", "")
        with gzip.open(path, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        bars_raw = payload.get("bars") if isinstance(payload, dict) else payload
        rows: list[dict[str, Any]] = []
        if isinstance(bars_raw, list):
            for item in bars_raw:
                row = _row_from_any(item)
                if row is not None:
                    rows.append(row)
                    if int(row["time"]) > latest:
                        latest = int(row["time"])
        rows.sort(key=lambda r: int(r["time"]))
        _FIXTURES[tf] = rows
        logger.info("fixtures tf=%s bars=%s file=%s", tf, len(rows), path.name)
    _FIXTURE_NOW = latest or None
    if not _FIXTURES:
        logger.warning("CHART_APP_FIXTURES=1 but no files in %s", FIXTURE_DIR)


def _row_from_any(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict) and "time" in item:
        try:
            return {
                "time": int(item["time"]),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume") or 0.0),
            }
        except (TypeError, ValueError, KeyError):
            return None
    if isinstance(item, (list, tuple)) and len(item) >= 5:
        try:
            return {
                "time": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]) if len(item) > 5 else 0.0,
            }
        except (TypeError, ValueError):
            return None
    return None


def _parse_candles_body(raw: bytes) -> list[dict[str, Any]]:
    payload = json.loads(raw.decode("utf-8"))
    result = payload.get("result") or []
    if not isinstance(result, list):
        return []
    out: list[dict[str, Any]] = []
    for item in result:
        row = _row_from_any(item) if isinstance(item, dict) else None
        if row is not None:
            out.append(row)
    return out


def _cache_path(symbol: str, resolution: str, start: int, end: int) -> Path:
    return CACHE_DIR / f"{symbol}_{resolution}_{int(start)}_{int(end)}.json"


def _read_disk(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("disk cache read fail %s: %s", path.name, exc)
        return None
    rows = raw.get("bars") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for item in rows:
        row = _row_from_any(item)
        if row is not None:
            out.append(row)
    return out


def _write_disk(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        compact = [
            [int(r["time"]), r["open"], r["high"], r["low"], r["close"], r["volume"]]
            for r in rows
        ]
        path.write_text(json.dumps({"bars": compact}, separators=(",", ":")), encoding="utf-8")
    except OSError as exc:
        logger.warning("disk cache write fail %s: %s", path.name, exc)


def _fixture_slice(resolution: str, start: int, end: int) -> list[dict[str, Any]]:
    _load_fixtures()
    assert _FIXTURES is not None
    rows = _FIXTURES.get(resolution) or []
    if not rows and resolution != "1m":
        rows = _FIXTURES.get("1m") or []
    return [r for r in rows if int(start) <= int(r["time"]) <= int(end)]


async def _client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(timeout=30.0)
    return _CLIENT


async def fetch_history_candles(
    symbol: str,
    resolution: str,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    """Fetch one Delta history page (or fixture/disk equivalent)."""
    start_i = int(start)
    end_i = int(end)
    if end_i <= start_i:
        return []
    if fixtures_enabled():
        return _fixture_slice(resolution, start_i, end_i)

    res = RES_SEC.get(resolution, 60)
    closed_before = chart_now() - res
    page_closed = end_i <= closed_before
    path = _cache_path(symbol, resolution, start_i, end_i)
    if page_closed:
        cached = await asyncio.to_thread(_read_disk, path)
        if cached is not None:
            logger.info(
                "delta cache-hit %s %s %s..%s n=%s",
                symbol,
                resolution,
                start_i,
                end_i,
                len(cached),
            )
            return cached

    rows = await _http_candles(symbol, resolution, start_i, end_i)
    if page_closed:
        closed_rows = [r for r in rows if int(r["time"]) < closed_before]
        await asyncio.to_thread(_write_disk, path, closed_rows)
        return closed_rows
    return rows


async def _http_candles(
    symbol: str,
    resolution: str,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    global _REQ_N
    params = {
        "resolution": resolution,
        "symbol": symbol,
        "start": start,
        "end": end,
    }
    url = f"{DELTA_BASE}{CANDLES_PATH}?{urlencode(params)}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "chart_app-delta-rest/1.0",
    }
    client = await _client()
    last_err: Exception | None = None
    async with _SEM:
        for attempt in range(5):
            t0 = time.perf_counter()
            _REQ_N += 1
            n = _REQ_N
            try:
                resp = await client.get(
                    f"{DELTA_BASE}{CANDLES_PATH}",
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                last_err = exc
                wait = min(8.0, 0.5 * (2 ** attempt))
                logger.warning(
                    "delta HTTP GET %s error=%s ms=%.0f retry=%s wait=%.1f n=%s",
                    url,
                    exc.__class__.__name__,
                    (time.perf_counter() - t0) * 1000.0,
                    attempt,
                    wait,
                    n,
                )
                await asyncio.sleep(wait)
                continue
            ms = (time.perf_counter() - t0) * 1000.0
            retry_after = resp.headers.get("Retry-After")
            logger.info(
                "delta HTTP GET %s status=%s ms=%.0f retry=%s retry_after=%s n=%s",
                url,
                resp.status_code,
                ms,
                attempt,
                retry_after,
                n,
            )
            if resp.status_code == 429:
                wait = float(retry_after) if retry_after else min(16.0, 1.0 * (2 ** attempt))
                try:
                    wait = float(wait)
                except (TypeError, ValueError):
                    wait = min(16.0, 1.0 * (2 ** attempt))
                logger.warning("delta 429 wait=%.1f n=%s", wait, n)
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = min(8.0, 0.75 * (2 ** attempt))
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"delta {resp.status_code} {url}")
            raw = resp.content
            out = await asyncio.to_thread(_parse_candles_body, raw)
            return out
    raise RuntimeError(f"candles fetch failed start={start} end={end}: {last_err}")
