"""Process-level LRU cache for mark series / option chains (shared across combos)."""

from __future__ import annotations

import logging
import os
import sys
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("harness.mark_cache")

DEFAULT_MAX_BYTES = 4 * 1024**3  # 4 GiB


def _env_max_bytes() -> int:
    raw = os.environ.get("MARK_CACHE_MAX_GB", "").strip()
    if raw:
        try:
            return max(64 * 1024**2, int(float(raw) * 1024**3))
        except ValueError:
            logger.warning("bad MARK_CACHE_MAX_GB=%r — using default 4GB", raw)
    return DEFAULT_MAX_BYTES


def _nbytes_obj(obj: Any) -> int:
    """Rough size for LRU budget — fast heuristics for common payloads."""
    if isinstance(obj, dict):
        # assume int/float values (mark series)
        n = len(obj)
        if n and all(isinstance(k, int) for k in list(obj.keys())[:3]):
            return 64 * n + 256
        # chain-like nested dicts counted below
        return sys.getsizeof(obj) + sum(
            sys.getsizeof(k) + _nbytes_obj(v) for k, v in obj.items()
        )
    if isinstance(obj, list):
        if not obj:
            return 64
        if isinstance(obj[0], dict):
            # option chain rows
            return 256 * len(obj) + 256
        return sys.getsizeof(obj) + sum(sys.getsizeof(x) for x in obj)
    return sys.getsizeof(obj)


class MarkCache:
    """
    Process-wide LRU for:
      - symbol series: key = ("series", symbol, t0, t1) → dict[ts, close]
      - option chain:  key = ("chain", expiry, ts, opt_type) → list[dict]
    Shared across all combos in one run_grid process.
    """

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = int(max_bytes if max_bytes is not None else _env_max_bytes())
        self._data: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._sizes: dict[tuple[Any, ...], int] = {}
        self._nbytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def clear(self) -> None:
        self._data.clear()
        self._sizes.clear()
        self._nbytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._data),
            "nbytes": self._nbytes,
            "max_bytes": self.max_bytes,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": (
                self.hits / (self.hits + self.misses)
                if (self.hits + self.misses)
                else 0.0
            ),
        }

    def get(self, key: tuple[Any, ...]) -> Any | None:
        if key not in self._data:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return self._data[key]

    def put(self, key: tuple[Any, ...], value: Any, nbytes: int | None = None) -> None:
        size = int(nbytes if nbytes is not None else _nbytes_obj(value))
        if key in self._data:
            self._nbytes -= self._sizes.get(key, 0)
            del self._data[key]
            self._sizes.pop(key, None)
        # If a single entry exceeds cap, still store it (avoids empty cache),
        # but evict everything else first.
        while self._data and (self._nbytes + size) > self.max_bytes:
            old_k, _ = self._data.popitem(last=False)
            self._nbytes -= self._sizes.pop(old_k, 0)
            self.evictions += 1
        self._data[key] = value
        self._sizes[key] = size
        self._nbytes += size
        self._data.move_to_end(key)


_PROCESS_CACHE: MarkCache | None = None


def get_mark_cache() -> MarkCache:
    global _PROCESS_CACHE
    if _PROCESS_CACHE is None:
        _PROCESS_CACHE = MarkCache()
        logger.info(
            "mark cache init max_bytes=%d (%.2f GiB)",
            _PROCESS_CACHE.max_bytes,
            _PROCESS_CACHE.max_bytes / (1024**3),
        )
    return _PROCESS_CACHE


def reset_mark_cache(max_bytes: int | None = None) -> MarkCache:
    """Replace process cache (tests / explicit re-profile)."""
    global _PROCESS_CACHE
    _PROCESS_CACHE = MarkCache(max_bytes=max_bytes)
    return _PROCESS_CACHE
