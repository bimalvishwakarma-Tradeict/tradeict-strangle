"""Partial HTF bars from 1m — last bucket uses only minutes with time <= cap."""

from __future__ import annotations

from typing import Any

import numpy as np

RES_SEC = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}


def aggregate_1m(rows_1m: list[dict[str, Any]], tf: str, to_u: int) -> list[dict[str, Any]]:
    """OHLC buckets. Last HTF candle is a partial aggregate of 1m bars with time <= to_u."""
    res = RES_SEC.get(tf, 60)
    cap = int(to_u)
    if res <= 60:
        return [b for b in rows_1m if int(b["time"]) <= cap]
    buckets: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for raw in rows_1m:
        t = int(raw["time"])
        if t > cap:
            continue
        k = (t // res) * res
        if k not in buckets:
            buckets[k] = {
                "time": k,
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw.get("volume") or 0.0),
            }
            order.append(k)
        else:
            o = buckets[k]
            o["high"] = max(o["high"], float(raw["high"]))
            o["low"] = min(o["low"], float(raw["low"]))
            o["close"] = float(raw["close"])
            o["volume"] += float(raw.get("volume") or 0.0)
    return [buckets[k] for k in order]


def resample_tf_partial(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    tf_sec: int,
) -> tuple[np.ndarray, ...]:
    """Like backtest resample_tf, but the last bucket is always emitted (partial OK)."""
    if tf_sec <= 60:
        return ts, o, h, l, c, vol
    n = len(ts)
    need = tf_sec // 60
    buckets = (ts // tf_sec) * tf_sec
    ots: list[int] = []
    oo: list[float] = []
    hh: list[float] = []
    ll: list[float] = []
    cc: list[float] = []
    vv: list[float] = []
    i = 0
    while i < n:
        b = int(buckets[i])
        j = i + 1
        while j < n and int(buckets[j]) == b:
            j += 1
        is_last = j >= n
        if (j - i) < need and not is_last:
            i = j
            continue
        ots.append(b)
        oo.append(float(o[i]))
        hh.append(float(np.max(h[i:j])))
        ll.append(float(np.min(l[i:j])))
        cc.append(float(c[j - 1]))
        vv.append(float(np.sum(vol[i:j])))
        i = j
    return (
        np.asarray(ots, dtype=np.int64),
        np.asarray(oo, dtype=np.float64),
        np.asarray(hh, dtype=np.float64),
        np.asarray(ll, dtype=np.float64),
        np.asarray(cc, dtype=np.float64),
        np.asarray(vv, dtype=np.float64),
    )
