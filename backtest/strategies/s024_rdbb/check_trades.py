#!/usr/bin/env python3
"""S024 stage1 trade invariants."""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.strategies.s023_obps.sim import expiry_unix, hours_to  # noqa: E402
from backtest.strategies.s024_rdbb.sim import sl_level  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
HOURS_1DTE_CUT = 3.0


def _upcoming_e0(t: int) -> date:
    from datetime import datetime

    dt = datetime.fromtimestamp(int(t), IST)
    d0 = dt.date()
    if int(t) < expiry_unix(d0):
        return d0
    return d0 + timedelta(days=1)


def check_path(path: Path) -> list[str]:
    errs: list[str] = []
    with path.open(newline="", encoding="utf-8") as fp:
        rows = list(csv.DictReader(fp))
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[(r["cell"], r["arm"])].append(r)
        entry = int(r["entry_ts"])
        trig = int(r["exit_ts"])
        fill = int(r["exit_fill_ts"])
        reason = str(r["exit_reason"])
        if reason != "EXPIRY" and fill != trig + 60:
            errs.append(f"row {i} fill_time {fill} != trigger+60 {trig + 60}")
        fees = float(r["fees"])
        if reason == "EXPIRY":
            # settle fee/slip 0; entry fees remain. Flag only if fees==0 would be wrong;
            # user: expiry exit par fees = 0 meaning the settle fill.
            slip = float(r["slip"])
            # two entry legs: slip $5*2=$10 at qty 1000; extra would be settle slip
            if slip > 10.0001:
                errs.append(f"row {i} EXPIRY slip {slip} > entry-only $10")
        peak = float(r["peak"])
        sl = float(r["sl_level"])
        want = sl_level(peak)
        if sl < want - 1e-9:
            errs.append(f"row {i} sl_level {sl} < recomputed {want}")
        if sl + 1e-9 < -100.0:
            errs.append(f"row {i} sl_level decreased below -100: {sl}")
        e0 = _upcoming_e0(entry)
        late = hours_to(e0, entry) <= HOURS_1DTE_CUT
        used = int(float(r.get("used_1dte") or 0))
        if late and used != 1:
            errs.append(f"row {i} hours_left={hours_to(e0, entry):.2f} but used_1dte={used}")
        if (not late) and used == 1:
            errs.append(f"row {i} hours_left={hours_to(e0, entry):.2f} but used_1dte=1")
    for key, rs in groups.items():
        ordered = sorted(rs, key=lambda x: int(x["entry_ts"]))
        busy = 0
        for r in ordered:
            t0 = int(r["entry_ts"])
            if t0 < busy:
                errs.append(f"overlap {key} entry={t0} busy_until={busy}")
            busy = int(r["exit_fill_ts"])
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="")
    ap.add_argument("--folder", default="")
    args = ap.parse_args()
    if args.folder:
        path = Path(args.folder) / "trades.csv"
    elif args.trades:
        path = Path(args.trades)
    else:
        runs = Path(__file__).resolve().parent / "runs"
        cands = sorted(runs.glob("*_s024_stage1*/trades.csv"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("FAIL no trades.csv")
            sys.exit(1)
        path = cands[-1]
    print(f"check {path}", flush=True)
    errs = check_path(path)
    if errs:
        print(f"FAIL n={len(errs)}")
        for e in errs[:50]:
            print(e)
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
