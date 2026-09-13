# candles.py — Delta India candle feed for S003 (closed candles only)

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.strategies.s003_lsr4.config import Strategy3Config, timeframe_seconds
from backend.strategies.s003_lsr4.lsr4 import Candle

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15


def _as_utc_from_unix(ts: int | float) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def raw_to_candle(row: dict[str, Any]) -> Candle:
    return Candle(
        open_time=_as_utc_from_unix(row["time"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume") or 0.0),
    )


async def fetch_product_tick_size(client: Any, symbol: str) -> float:
    """
    Read tick_size from Delta /v2/products using the client's IPv4 httpx session.
    Not a DeltaClient public method — keeps delta_client.py to one new candles API.
    """
    path = "/v2/products"
    params = {"contract_types": "perpetual_futures"}
    qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{client.base_url.rstrip('/')}{path}{qs}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Tradeict-Short-Strangle-Bot/1.0",
    }
    response = await client.client.request(
        method="GET", url=url, headers=headers, timeout=30.0
    )
    if response.status_code != 200:
        raise RuntimeError(f"products tick_size HTTP {response.status_code}")
    products = response.json().get("result") or []
    wanted = str(symbol).upper()
    for product in products:
        if str(product.get("symbol") or "").upper() == wanted:
            return float(product.get("tick_size") or 0.5)
    raise RuntimeError(f"tick_size not found for {symbol}")


class CandleFeed:
    """
    Fetches BTCUSD (or configured) candles via DeltaClient.get_historical_candles.

    Deduplicates by open_time. Returns only final (closed) candles.
    """

    def __init__(self, client: Any, cfg: Strategy3Config) -> None:
        self.client = client
        self.cfg = cfg
        self._tf = timeframe_seconds(cfg.timeframe)
        self._seen: set[datetime] = set()
        self._last_open: datetime | None = None

    def apply_config(self, cfg: Strategy3Config) -> None:
        if (
            cfg.symbol != self.cfg.symbol
            or cfg.timeframe != self.cfg.timeframe
        ):
            self._seen.clear()
            self._last_open = None
        self.cfg = cfg
        self._tf = timeframe_seconds(cfg.timeframe)

    def _is_final(self, candle: Candle, now: datetime | None = None) -> bool:
        now_utc = now or datetime.now(timezone.utc)
        close_ts = candle.open_time + timedelta(seconds=self._tf)
        return close_ts <= (now_utc - timedelta(seconds=10))

    async def _fetch_range(self, start: int, end: int) -> list[Candle]:
        rows = await self.client.get_historical_candles(
            symbol=self.cfg.symbol,
            resolution=self.cfg.timeframe,
            start=start,
            end=end,
        )
        candles = [raw_to_candle(r) for r in rows if "time" in r]
        candles.sort(key=lambda c: c.open_time)
        # Deduplicate within batch
        out: list[Candle] = []
        seen_local: set[datetime] = set()
        for c in candles:
            if c.open_time in seen_local:
                continue
            seen_local.add(c.open_time)
            out.append(c)
        return out

    async def bootstrap(self, n: int = 500) -> list[Candle]:
        """Load the last ~n closed candles (oldest first)."""
        now = datetime.now(timezone.utc)
        end = int(now.timestamp())
        start = end - (int(n) + 5) * self._tf
        candles = await self._fetch_range(start, end)
        closed = [c for c in candles if self._is_final(c, now)]
        # Keep the last n
        if len(closed) > n:
            closed = closed[-n:]
        self._seen = {c.open_time for c in closed}
        self._last_open = closed[-1].open_time if closed else None
        return closed

    async def poll(self) -> list[Candle]:
        """
        Return new CLOSED candles since last poll, oldest first.

        If the exchange returns candles newer than expected, backfill the
        missing range (only what the exchange actually returns — never invent).
        """
        now = datetime.now(timezone.utc)
        end = int(now.timestamp())
        if self._last_open is not None:
            # Start slightly before last to catch overlaps; filter by seen
            start = int(self._last_open.timestamp()) - self._tf
            expected_next = self._last_open + timedelta(seconds=self._tf)
            # If we appear behind, widen the window for backfill
            lag = now - expected_next
            if lag > timedelta(seconds=self._tf * 2):
                start = int((now - timedelta(seconds=self._tf * 120)).timestamp())
        else:
            start = end - self._tf * 30

        candles = await self._fetch_range(start, end)
        new: list[Candle] = []
        for c in candles:
            if not self._is_final(c, now):
                continue
            if c.open_time in self._seen:
                continue
            if self._last_open is not None and c.open_time <= self._last_open:
                continue
            new.append(c)
            self._seen.add(c.open_time)
            self._last_open = c.open_time
        # Bound seen set growth
        if len(self._seen) > 5000:
            keep = sorted(self._seen)[-2000:]
            self._seen = set(keep)
        return new

    async def fetch_last_n(self, n: int) -> list[Candle]:
        """One-shot fetch for backfill API (does not mutate live cursor)."""
        now = datetime.now(timezone.utc)
        end = int(now.timestamp())
        start = end - (int(n) + 5) * self._tf
        candles = await self._fetch_range(start, end)
        closed = [c for c in candles if self._is_final(c, now)]
        if len(closed) > n:
            closed = closed[-n:]
        return closed
