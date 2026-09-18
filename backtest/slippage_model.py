#!/usr/bin/env python3
"""
Shared premium-bucketed slippage lookup for backtests.

Reads calibrate_slippage.csv — never hardcodes bucket rates.
Raises if CSV is missing (no silent 1.65% fallback).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

_BACKTEST = Path(__file__).resolve().parent
DEFAULT_CSV = _BACKTEST / "results" / "calibrate_slippage.csv"

# DTE×premium override only when that cell has at least this many prints
MIN_CROSS_N = 50_000

_PREM_ORDER = ("<100", "100-300", "300-600", "600-900", "900+")

# Module cache set by load_slip_table
_TABLE: dict[str, Any] | None = None


class SlippageTableError(FileNotFoundError):
    """Raised when the calibration CSV is missing or unusable."""


def prem_bucket(premium: float) -> str:
    if premium < 100:
        return "<100"
    if premium < 300:
        return "100-300"
    if premium < 600:
        return "300-600"
    if premium < 900:
        return "600-900"
    return "900+"


def dte_bucket(dte: int) -> str:
    if dte <= 0:
        return "0"
    if dte == 1:
        return "1"
    if dte == 2:
        return "2"
    if dte <= 7:
        return "3-7"
    return "8+"


def _parse_highlight_note(note: str) -> list[tuple[str, str]]:
    """
    Parse HIGHLIGHT note like 'DTE 2, premium 100-300' or 'DTE 0-1, premium 600-900'
    into list of (dte_bucket, prem_bucket).
    """
    note = (note or "").strip().strip('"')
    if not note:
        return []
    # expected: "DTE X, premium Y" or "DTE X-Y, premium Z"
    dte_part = ""
    prem_part = ""
    for chunk in note.split(","):
        c = chunk.strip().lower()
        if c.startswith("dte "):
            dte_part = c[4:].strip()
        elif c.startswith("premium "):
            prem_part = c[len("premium ") :].strip()
    if not dte_part or not prem_part:
        return []
    if prem_part not in _PREM_ORDER:
        return []
    dtes: list[str] = []
    if "-" in dte_part and not dte_part.startswith("3-") and not dte_part.startswith("8"):
        # e.g. 0-1
        a, b = dte_part.split("-", 1)
        try:
            lo, hi = int(a), int(b)
            for d in range(lo, hi + 1):
                dtes.append(dte_bucket(d))
        except ValueError:
            return []
    else:
        # single like "2" or already "3-7"
        if dte_part in ("0", "1", "2", "3-7", "8+"):
            dtes.append(dte_part)
        else:
            try:
                dtes.append(dte_bucket(int(dte_part)))
            except ValueError:
                return []
    return [(d, prem_part) for d in dtes]


def load_slip_table(
    path: Path | str | None = None,
) -> dict[str, Any]:
    """
    Load calibration CSV into a lookup dict.

    Keys:
      premium: {bucket: recommended_slip_pct}
      cross_n: {(dte_bucket, prem_bucket): n}
      override: {(dte_bucket, prem_bucket): recommended_slip_pct}  # HIGHLIGHT
      path: str
    """
    global _TABLE
    csv_path = Path(path) if path is not None else DEFAULT_CSV
    if not csv_path.is_file():
        raise SlippageTableError(
            f"slippage calibration CSV missing: {csv_path} "
            "(run calibrate_slippage.py first; no silent 1.65% fallback)"
        )

    premium: dict[str, float] = {}
    cross_n: dict[tuple[str, str], int] = {}
    override: dict[tuple[str, str], float] = {}

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            section = (row.get("section") or "").strip()
            key = (row.get("key") or "").strip()
            try:
                n = int(float(row.get("n") or 0))
            except ValueError:
                n = 0
            rec_raw = row.get("recommended_slip_pct") or ""
            try:
                rec = float(rec_raw) if rec_raw not in ("", "nan", "NaN") else float("nan")
            except ValueError:
                rec = float("nan")

            if section == "B_premium" and key.startswith("prem="):
                bucket = key[len("prem=") :]
                if bucket in _PREM_ORDER and rec == rec:  # not NaN
                    premium[bucket] = float(rec)
            elif section == "D_cross" and key.startswith("dte=") and "|prem=" in key:
                # dte=0|prem=100-300
                left, right = key.split("|prem=", 1)
                dte_b = left[len("dte=") :]
                prem_b = right
                cross_n[(dte_b, prem_b)] = n
            elif section == "HIGHLIGHT":
                note = row.get("note") or ""
                if rec != rec:
                    continue
                for pair in _parse_highlight_note(note):
                    override[pair] = float(rec)

    missing = [b for b in _PREM_ORDER if b not in premium]
    if missing:
        raise SlippageTableError(
            f"slippage CSV incomplete — missing premium buckets {missing} in {csv_path}"
        )

    table: dict[str, Any] = {
        "premium": premium,
        "cross_n": cross_n,
        "override": override,
        "path": str(csv_path),
        "min_cross_n": MIN_CROSS_N,
    }
    _TABLE = table
    return table


def slip_pct(
    premium: float,
    dte: int,
    table: dict[str, Any] | None = None,
) -> float:
    """
    Recommended symmetric slippage percent (e.g. 0.63 means 0.63%).

    Primary: premium bucket.
    DTE override: only if HIGHLIGHT/override exists for (dte,prem) AND
    D_cross cell n >= MIN_CROSS_N.
    """
    t = table if table is not None else _TABLE
    if t is None:
        raise SlippageTableError(
            "slip table not loaded — call load_slip_table() first "
            "(no silent 1.65% fallback)"
        )
    prem_b = prem_bucket(float(premium))
    dte_b = dte_bucket(int(dte))
    base = float(t["premium"][prem_b])

    pair = (dte_b, prem_b)
    override: dict[tuple[str, str], float] = t["override"]
    cross_n: dict[tuple[str, str], int] = t["cross_n"]
    if pair in override:
        n = int(cross_n.get(pair, 0))
        # If cross_n missing for this exact cell, use override n eligibility via MIN
        # only when we have a count; HIGHLIGHT always had large n — require cross_n
        if n >= int(t["min_cross_n"]):
            return float(override[pair])
    return base
