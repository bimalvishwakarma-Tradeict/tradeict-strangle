"""Pine v4 ADX/DI port (Bimal TV script). Do not use s012g adx_wilder here."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def adx_pine_v4(
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    length: int = 14,
    warmup: int = 500,
    return_debug: bool = False,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Wilder sum-smoothing DI, ADX = SMA(DX, length). Causal. warmup bars of ADX NaN."""
    hh = np.asarray(h, dtype=np.float64)
    ll = np.asarray(l, dtype=np.float64)
    cc = np.asarray(c, dtype=np.float64)
    n = int(hh.size)
    length = int(length)
    di_p = np.full(n, np.nan, dtype=np.float64)
    di_m = np.full(n, np.nan, dtype=np.float64)
    dx = np.full(n, np.nan, dtype=np.float64)
    adx = np.full(n, np.nan, dtype=np.float64)
    s_tr = np.full(n, np.nan, dtype=np.float64)
    s_p = np.full(n, np.nan, dtype=np.float64)
    s_m = np.full(n, np.nan, dtype=np.float64)
    if n == 0 or length < 1:
        if return_debug:
            return di_p, di_m, adx, {"s_tr": s_tr, "s_p": s_p, "s_m": s_m, "dx": dx}
        return di_p, di_m, adx

    s_tr_prev = 0.0
    s_p_prev = 0.0
    s_m_prev = 0.0
    k = float(length)
    for i in range(n):
        hp = float(hh[i - 1]) if i > 0 else 0.0
        lp = float(ll[i - 1]) if i > 0 else 0.0
        cp = float(cc[i - 1]) if i > 0 else 0.0
        hi = float(hh[i])
        lo = float(ll[i])
        tr = max(hi - lo, abs(hi - cp), abs(lo - cp))
        up = hi - hp
        dn = lp - lo
        dmp = max(up, 0.0) if up > dn else 0.0
        dmm = max(dn, 0.0) if dn > up else 0.0
        s_tr_prev = s_tr_prev - s_tr_prev / k + tr
        s_p_prev = s_p_prev - s_p_prev / k + dmp
        s_m_prev = s_m_prev - s_m_prev / k + dmm
        s_tr[i] = s_tr_prev
        s_p[i] = s_p_prev
        s_m[i] = s_m_prev
        if s_tr_prev == 0.0 or not math.isfinite(s_tr_prev):
            continue
        dip = s_p_prev / s_tr_prev * 100.0
        dim = s_m_prev / s_tr_prev * 100.0
        di_p[i] = dip
        di_m[i] = dim
        den = dip + dim
        if den == 0.0 or not math.isfinite(den):
            continue
        dx[i] = abs(dip - dim) / den * 100.0

    for i in range(n):
        if i < length - 1:
            continue
        slc = dx[i - length + 1 : i + 1]
        if np.any(~np.isfinite(slc)):
            continue
        adx[i] = float(np.mean(slc))

    w = min(int(warmup), n)
    if w > 0:
        adx[:w] = np.nan

    if return_debug:
        return di_p, di_m, adx, {"s_tr": s_tr, "s_p": s_p, "s_m": s_m, "dx": dx}
    return di_p, di_m, adx


def adx_rising(adx: np.ndarray, j: int, k: int = 5) -> bool:
    """True iff adx[j] > adx[j-1] > ... > adx[j-k] with no NaN."""
    a = np.asarray(adx, dtype=np.float64)
    jj = int(j)
    kk = int(k)
    if kk < 1 or jj < kk or jj >= int(a.size):
        return False
    prev = float(a[jj])
    if not math.isfinite(prev):
        return False
    for t in range(1, kk + 1):
        cur = float(a[jj - t])
        if not math.isfinite(cur) or not (prev > cur):
            return False
        prev = cur
    return True
