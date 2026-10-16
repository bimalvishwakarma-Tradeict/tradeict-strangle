"""Invariant checker for S023 trades.csv step strikes / qty."""

from __future__ import annotations

import csv
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backtest.strategies.s023_obps.sim import (
    is_late_window,
    protection_qty,
    step_qty,
)

IST = ZoneInfo("Asia/Kolkata")


def _parse_ist_unix(s: str) -> int:
    dt = datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp())


def _legs(s: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in str(s or "").split(";"):
        part = part.strip()
        if not part:
            continue
        bits = part.split("/")
        if len(bits) < 6:
            continue
        out.append(
            {
                "cp": bits[0],
                "strike": float(bits[1]),
                "expiry": date.fromisoformat(bits[2]),
                "qty": int(float(bits[3])),
                "fill": float(bits[4]),
                "role": bits[5],
            }
        )
    return out


def _blob(raw: str) -> dict[str, Any]:
    if not str(raw or "").strip():
        return {}
    try:
        return dict(json.loads(raw))
    except json.JSONDecodeError:
        return {}


def _dte0_at(t: int) -> date:
    loc = datetime.fromtimestamp(int(t), tz=IST)
    exp_today = loc.replace(hour=17, minute=30, second=0, microsecond=0)
    if int(t) < int(exp_today.timestamp()):
        return loc.date()
    return (loc + timedelta(days=1)).date()


def check_trades(path: str | Path, qty: int = 1000) -> list[str]:
    p = Path(path)
    violations: list[str] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, start=2):
        legs = _legs(row.get("legs") or "")
        by_role = {str(lg["role"]): lg for lg in legs}
        mc = by_role.get("main_call")
        mp = by_role.get("main_put")
        sc = by_role.get("short_call")
        sp = by_role.get("short_put")
        if mc is None or mp is None or sc is None or sp is None:
            violations.append(f"L{i} missing core legs")
            continue
        q_main = int(mc["qty"])
        avg = 0.5 * (float(sc["fill"]) + float(sp["fill"]))
        want_prot = protection_qty(q_main, avg)
        if int(sc["qty"]) != want_prot or int(sp["qty"]) != want_prot:
            violations.append(
                f"L{i} protection qty {sc['qty']}/{sp['qty']} != floor(70*{q_main}/{avg}*3)={want_prot}"
            )
        e1 = mc["expiry"]
        gap_col = float(row.get("step_strike_gap") or 0.0 or 0)
        for col in ("max_short_call", "max_short_put"):
            rawv = row.get(col)
            if rawv is None or str(rawv).strip() == "":
                continue
            if float(rawv) > float(q_main) + 1e-9:
                violations.append(f"L{i} {col} {rawv} > Q={q_main}")
        for name in ("step1", "step2"):
            blob = _blob(row.get(name) or "")
            if not blob:
                continue
            fill_ts = int(blob["ts"])
            trig = fill_ts - 60
            e0 = _dte0_at(trig)
            late = is_late_window(e0, trig)
            is_call = bool(blob.get("is_call"))
            strike = float(blob["strike"])
            exp = date.fromisoformat(str(blob["expiry"]))
            prem = float(blob.get("prem") or 0.0)
            sq = int(blob.get("qty") or 0)
            skip_why = str(blob.get("skip") or row.get(f"{name}_skip") or "")
            if skip_why:
                if sq != 0:
                    violations.append(f"L{i} {name} skip={skip_why} but qty={sq}")
            elif prem > 0:
                want_s = step_qty(q_main, prem)
                if sq > want_s:
                    violations.append(f"L{i} {name} qty {sq} > raw {want_s}")
            json_gap = float(blob.get("strike_gap") or 0.0)
            logged = max(gap_col, json_gap)
            if late:
                want_k = float(sc["strike"] if is_call else sp["strike"])
                if exp != e1:
                    violations.append(f"L{i} {name} late expiry {exp} != E1 {e1}")
                if abs(strike - want_k) > 1e-6 and logged <= 0:
                    violations.append(
                        f"L{i} {name} late strike {strike} != straddle {want_k} (gap={logged})"
                    )
            else:
                want_k = float(mc["strike"] if is_call else mp["strike"])
                if exp != e0:
                    violations.append(f"L{i} {name} normal expiry {exp} != 0DTE {e0}")
                if abs(strike - want_k) > 1e-6 and logged <= 0:
                    violations.append(
                        f"L{i} {name} normal strike {strike} != main {want_k} (gap={logged})"
                    )
    print(f"check_trades {p} violations={len(violations)}")
    for v in violations:
        print(v)
    return violations
