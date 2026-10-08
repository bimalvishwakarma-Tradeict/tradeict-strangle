"""RSI OBH/OBL divergence signals (Bimal rules). Evaluate on TF bar close."""

from __future__ import annotations

from typing import Any

import numpy as np

RSI_LEN = 14
RSI_OB = 70.0
RSI_OS = 30.0


def rsi_wilder(close: np.ndarray, n: int = RSI_LEN) -> np.ndarray:
    """Wilder RMA RSI (Pine ta.rsi). First seed = SMA of n changes."""
    c = np.asarray(close, dtype=np.float64)
    out = np.full(c.shape[0], np.nan, dtype=np.float64)
    nn = int(n)
    if nn < 1 or c.size < nn + 1:
        return out
    delta = np.diff(c)
    gain = np.where(delta > 0.0, delta, 0.0)
    loss = np.where(delta < 0.0, -delta, 0.0)
    avg_g = float(np.mean(gain[:nn]))
    avg_l = float(np.mean(loss[:nn]))
    if avg_l <= 1e-18:
        out[nn] = 100.0 if avg_g > 1e-18 else 50.0
    else:
        out[nn] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(nn, len(delta)):
        avg_g = (avg_g * (nn - 1) + float(gain[i])) / nn
        avg_l = (avg_l * (nn - 1) + float(loss[i])) / nn
        if avg_l <= 1e-18:
            out[i + 1] = 100.0 if avg_g > 1e-18 else 50.0
        else:
            out[i + 1] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def _close_zone(z: dict[str, Any], end_ts: int, reason: str, zones: list[dict[str, Any]]) -> None:
    z["end_ts"] = int(end_ts)
    z["end_reason"] = str(reason)
    zones.append(dict(z))


def detect_from_rsi(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    rsi: np.ndarray,
    tf_sec: int,
    ob: float = RSI_OB,
    os: float = RSI_OS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """State machine on already-closed TF bars. Signal ts = bar close (ts + tf_sec).

    Rev3: divergence vs confirmed OBH/OBL is checked first (even if RSI is
    still >70 / <30). A new forming extreme does not replace the confirmed
    one until RSI leaves the band and the new zone confirms.
    """
    n = int(len(ts))
    signals: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    tf_sec = int(tf_sec)

    forming_h: dict[str, Any] | None = None
    conf_h: dict[str, Any] | None = None
    forming_l: dict[str, Any] | None = None
    conf_l: dict[str, Any] | None = None

    def close_ts(i: int) -> int:
        return int(ts[i]) + tf_sec

    def _new_obh(i: int, r: float, hi: float, cts: int) -> dict[str, Any]:
        return {
            "kind": "OBH",
            "start_ts": cts,
            "confirm_ts": 0,
            "level": hi,
            "rsi": r,
            "end_ts": 0,
            "end_reason": "",
            "ob_candle_ts": int(ts[i]),
        }

    def _new_obl(i: int, r: float, lo: float, cts: int) -> dict[str, Any]:
        return {
            "kind": "OBL",
            "start_ts": cts,
            "confirm_ts": 0,
            "level": lo,
            "rsi": r,
            "end_ts": 0,
            "end_reason": "",
            "ob_candle_ts": int(ts[i]),
        }

    for i in range(n):
        r = float(rsi[i])
        if not np.isfinite(r):
            continue
        hi = float(h[i])
        lo = float(l[i])
        cl = float(c[i])
        cts = close_ts(i)

        # --- OBH (short). Rev3: divergence first, even if RSI > 70. ---
        if conf_h is not None and cl > float(conf_h["level"]) and r < float(conf_h["rsi"]):
            signals.append(
                {
                    "ts": cts,
                    "side": "short",
                    "ob_level": float(conf_h["level"]),
                    "ob_rsi": float(conf_h["rsi"]),
                    "sig_rsi": r,
                    "ob_candle_ts": int(conf_h["ob_candle_ts"]),
                }
            )
            _close_zone(conf_h, cts, "signal", zones)
            conf_h = None
        if conf_h is not None and r < os:
            _close_zone(conf_h, cts, "opposite_extreme", zones)
            conf_h = None
        if r > ob:
            if forming_h is None:
                forming_h = _new_obh(i, r, hi, cts)
            elif hi >= float(forming_h["level"]):
                forming_h["level"] = hi
                forming_h["rsi"] = r
                forming_h["ob_candle_ts"] = int(ts[i])
        elif forming_h is not None:
            if conf_h is not None:
                _close_zone(conf_h, cts, "replaced", zones)
            forming_h["confirm_ts"] = cts
            conf_h = forming_h
            forming_h = None

        # --- OBL (long). Mirror: divergence first, even if RSI < 30. ---
        if conf_l is not None and cl < float(conf_l["level"]) and r > float(conf_l["rsi"]):
            signals.append(
                {
                    "ts": cts,
                    "side": "long",
                    "ob_level": float(conf_l["level"]),
                    "ob_rsi": float(conf_l["rsi"]),
                    "sig_rsi": r,
                    "ob_candle_ts": int(conf_l["ob_candle_ts"]),
                }
            )
            _close_zone(conf_l, cts, "signal", zones)
            conf_l = None
        if conf_l is not None and r > ob:
            _close_zone(conf_l, cts, "opposite_extreme", zones)
            conf_l = None
        if r < os:
            if forming_l is None:
                forming_l = _new_obl(i, r, lo, cts)
            elif lo <= float(forming_l["level"]):
                forming_l["level"] = lo
                forming_l["rsi"] = r
                forming_l["ob_candle_ts"] = int(ts[i])
        elif forming_l is not None:
            if conf_l is not None:
                _close_zone(conf_l, cts, "replaced", zones)
            forming_l["confirm_ts"] = cts
            conf_l = forming_l
            forming_l = None

    return signals, zones


def detect_rsi_div(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    tf_sec: int,
    n: int = RSI_LEN,
    ob: float = RSI_OB,
    os: float = RSI_OS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rsi = rsi_wilder(c, int(n))
    return detect_from_rsi(ts, o, h, l, c, rsi, int(tf_sec), float(ob), float(os))
