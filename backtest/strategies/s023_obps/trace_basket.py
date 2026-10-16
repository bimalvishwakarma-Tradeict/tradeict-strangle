#!/usr/bin/env python3
"""Replay S023 baskets with event log + 15m snapshots (same simulate_basket path)."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from openpyxl import Workbook

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s021_hedged_long_straddle import run_s021 as s021  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.run_s022 import S021Market  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import ffill_1m_grid  # noqa: E402
from backtest.strategies.s023_obps.run_s023 import _window, collect_listed  # noqa: E402
from backtest.strategies.s023_obps.signals import iter_obps_signals  # noqa: E402
from backtest.strategies.s023_obps.sim import (  # noqa: E402
    DEFAULT_QTY,
    BasketResult,
    _mtm,
    opt_symbol,
    simulate_basket,
    sl_now,
)

IST = ZoneInfo("Asia/Kolkata")
PREFERRED_CELL = "15m|full|ANY"
SNAP_SEC = 15 * 60
EVENT_COLS = [
    "time_ist", "event", "trigger_time_ist", "fill_time_ist",
    "btc_spot", "spot_high", "spot_low", "leg", "delta_symbol", "side",
    "qty", "price", "fee", "slip", "basket_gross_after", "sl_level_after",
    "tf", "ulp_kind", "ulp_top", "ulp_bottom", "pivot_ist", "age_bars",
    "rsi_div_side", "signal_price",
]


def parse_ist(s: str) -> int:
    dt = datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return int(dt.timestamp())


def load_trades(folder: Path) -> list[dict[str, Any]]:
    path = folder / "trades.csv"
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def parse_legs(legs_s: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in str(legs_s or "").split(";"):
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


def _step_blob(raw: str) -> dict[str, Any]:
    if not str(raw or "").strip():
        return {}
    try:
        return dict(json.loads(raw))
    except json.JSONDecodeError:
        return {}


def is_late_day_e1(row: dict[str, Any]) -> bool:
    s1 = _step_blob(row.get("step1") or "")
    if not s1:
        return False
    mains = [lg["expiry"] for lg in parse_legs(row.get("legs") or "") if str(lg["role"]).startswith("main")]
    if not mains:
        return False
    try:
        se = date.fromisoformat(str(s1["expiry"]))
    except (KeyError, TypeError, ValueError):
        return False
    return se == max(mains)


def is_redeploy_row(row: dict[str, Any]) -> bool:
    try:
        return int(float(row.get("redeploys") or 0)) > 0
    except (TypeError, ValueError):
        return False


def pick_auto(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    f_all = [r for r in rows if str(r.get("arm")) == "F"]
    pref = [r for r in f_all if str(r.get("cell")) == PREFERRED_CELL]
    pools = [pref, f_all]

    def first(pred: Any) -> dict[str, Any] | None:
        for pool in pools:
            for r in pool:
                key = (str(r.get("cell")), str(r.get("arm")), str(r.get("entry_ist")))
                if key in keys:
                    continue
                if pred(r):
                    return r
        return None

    picked: list[dict[str, Any]] = []
    keys: set[tuple[str, str, str]] = set()

    def add(r: dict[str, Any] | None) -> None:
        if r is None:
            return
        key = (str(r.get("cell")), str(r.get("arm")), str(r.get("entry_ist")))
        if key in keys:
            return
        keys.add(key)
        picked.append(r)

    add(first(lambda r: str(r.get("exit_reason")) == "TARGET"))
    add(first(lambda r: str(r.get("exit_reason")) == "SL" and not str(r.get("step1") or "").strip()))
    add(
        first(
            lambda r: bool(str(r.get("step1") or "").strip())
            and str(r.get("exit_reason")) == "SIGNAL_PRICE"
        )
    )
    add(
        first(lambda r: is_redeploy_row(r) or is_late_day_e1(r))
        or first(lambda r: bool(str(r.get("step2") or "").strip()))
    )
    while len(picked) < 4:
        extra = first(lambda _r: True)
        if extra is None:
            break
        add(extra)
    return picked


def parse_basket_key(spec: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) < 5:
        raise SystemExit(f"bad --basket {spec!r}; want cell|arm|entry_ist (tf|mode|arm|F|YYYY-mm-dd HH:MM:SS)")
    cell = "|".join(parts[:3])
    arm = parts[3]
    entry = "|".join(parts[4:]) if len(parts) > 5 else parts[4]
    for r in rows:
        if str(r.get("cell")) == cell and str(r.get("arm")) == arm and str(r.get("entry_ist")) == entry:
            return r
    raise SystemExit(f"no trades.csv row for {spec}")


def delta_sym(is_call: bool, strike: float, exp: date, stored: str) -> str:
    if stored and "BTC" in stored:
        return stored
    return opt_symbol(bool(is_call), float(strike), exp)


class RunContext:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        meta = json.loads((folder / "run_meta.json").read_text(encoding="utf-8"))
        cli = meta.get("cli_args") or {}
        months = [m.strip() for m in str(cli.get("months") or meta.get("month") or "2025-06").split(",") if m.strip()]
        csv_path = str(cli.get("csv") or s018.SPOT_CSV)
        self.qty = int(cli.get("qty") or DEFAULT_QTY)
        self.months = months
        w0, _, d0, _ = _window(months[0])
        _, w1, _, d1 = _window(months[-1])
        spot = s018.load_spot_1m(csv_path)
        ts_all, o_all, h_all, l_all, c_all, vol_all = s020.bars_1m_vol(spot)
        lo = w0 - 7 * 86400
        hi = w1 + 4 * 86400
        sel = (ts_all >= lo) & (ts_all < hi)
        self.ts = ts_all[sel]
        self.o = o_all[sel]
        self.h = h_all[sel]
        self.l = l_all[sel]
        self.c = c_all[sel]
        self.vol = vol_all[sel]
        self.spot_c = {int(t): float(px) for t, px in zip(self.ts, self.c)}
        self.ohlc = {int(t): (float(hh), float(ll), float(cc)) for t, hh, ll, cc in zip(self.ts, self.h, self.l, self.c)}
        inner = MarksStore()
        store = d.MonthGuardStore(inner, d0 - timedelta(days=1), d1 + timedelta(days=14))
        s021._BOOK = s021.MarkBook(w0 - 86400, w1 + 4 * 86400)
        s021._CHAIN_S021 = {}
        grid, _go, _gh, _gl, gc = ffill_1m_grid(self.ts, self.o, self.h, self.l, self.c)
        self.mkt = S021Market(store, s021._BOOK, self.spot_c, grid, gc)
        self.listed = collect_listed(store, d0 - timedelta(days=2), d1 + timedelta(days=16))
        self.sig_cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def _bars(self, tf: str) -> list[dict[str, Any]]:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(
            self.ts, self.o, self.h, self.l, self.c, self.vol, tf_sec,
        )
        return [
            {"time": int(t), "open": float(o), "high": float(h), "low": float(lo), "close": float(c)}
            for t, o, h, lo, c in zip(tts, to_, th, tl, tc)
        ]

    def signal_for(self, row: dict[str, Any]) -> dict[str, Any]:
        cell = str(row.get("cell") or "")
        bits = cell.split("|")
        tf, mode, sarm = (bits + ["", "", ""])[:3]
        entry_ts = parse_ist(str(row["entry_ist"]))
        sig_ts = entry_ts - 60
        base = {
            "signal_ts": sig_ts,
            "tf": tf,
            "mode": mode,
            "sig_arm": sarm,
            "side": "",
            "zone_kind": "",
            "zone_top": "",
            "zone_bottom": "",
            "pivot_ts": 0,
            "age_bars": "",
            "signal_price": float(row.get("signal_price") or float("nan")),
        }
        if str(row.get("arm")) != "F" or not tf or not mode:
            return base
        key = (tf, mode, sarm)
        if key not in self.sig_cache:
            self.sig_cache[key] = iter_obps_signals(self._bars(tf), tf, mode, sarm)
        for s in self.sig_cache[key]:
            if int(s["signal_ts"]) == sig_ts:
                return {**base, **s}
        return base


def bar_ohlc(ctx: RunContext, t: int) -> tuple[float, float, float]:
    b = ctx.ohlc.get(int(t))
    if b is None:
        return float("nan"), float("nan"), float("nan")
    hi, lo, cl = b
    return float(hi), float(lo), float(cl)


def event_rows(
    ctx: RunContext,
    row: dict[str, Any],
    sig: dict[str, Any],
    log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sig_ts = int(sig.get("signal_ts") or parse_ist(str(row["entry_ist"])) - 60)
    hi, lo, cl = bar_ohlc(ctx, sig_ts)
    pivot = int(sig.get("pivot_ts") or 0)
    head = {
        "time_ist": s018.ist_str(sig_ts),
        "event": "SIGNAL",
        "trigger_time_ist": s018.ist_str(sig_ts),
        "fill_time_ist": "",
        "btc_spot": ctx.spot_c.get(sig_ts, cl),
        "spot_high": hi,
        "spot_low": lo,
        "leg": "",
        "delta_symbol": "",
        "side": "",
        "qty": "",
        "price": sig.get("signal_price", row.get("signal_price")),
        "fee": "",
        "slip": "",
        "basket_gross_after": "",
        "sl_level_after": "",
        "tf": sig.get("tf") or (str(row.get("cell") or "").split("|")[0]),
        "ulp_kind": sig.get("zone_kind", ""),
        "ulp_top": sig.get("zone_top", ""),
        "ulp_bottom": sig.get("zone_bottom", ""),
        "pivot_ist": s018.ist_str(pivot) if pivot else "",
        "age_bars": sig.get("age_bars", ""),
        "rsi_div_side": sig.get("side", ""),
        "signal_price": sig.get("signal_price", row.get("signal_price")),
    }
    out = [head]
    for ev in log:
        ft = int(ev["fill_ts"])
        tt = int(ev["trigger_ts"])
        hi, lo, cl = bar_ohlc(ctx, ft)
        exp = ev.get("expiry")
        is_call = ev.get("is_call")
        strike = ev.get("strike")
        stored = str(ev.get("symbol") or "")
        dsym = ""
        if is_call is not None and strike is not None and exp is not None:
            dsym = delta_sym(bool(is_call), float(strike), exp, stored)
        elif stored:
            dsym = stored
        out.append(
            {
                "time_ist": s018.ist_str(ft),
                "event": ev["event"],
                "trigger_time_ist": s018.ist_str(tt),
                "fill_time_ist": s018.ist_str(ft),
                "btc_spot": ctx.spot_c.get(ft, cl),
                "spot_high": hi,
                "spot_low": lo,
                "leg": ev.get("leg") or "",
                "delta_symbol": dsym,
                "side": ev.get("side") or "",
                "qty": ev.get("qty"),
                "price": ev.get("price"),
                "fee": ev.get("fee"),
                "slip": ev.get("slip"),
                "basket_gross_after": ev.get("gross_after"),
                "sl_level_after": ev.get("sl_after"),
                "tf": "",
                "ulp_kind": "",
                "ulp_top": "",
                "ulp_bottom": "",
                "pivot_ist": "",
                "age_bars": "",
                "rsi_div_side": "",
                "signal_price": "",
            }
        )
    return out


def snapshots(ctx: RunContext, res: BasketResult, qty: int) -> list[dict[str, Any]]:
    times: list[int] = []
    t = int(res.entry_ts)
    while t <= int(res.exit_ts):
        times.append(t)
        t += SNAP_SEC
    if not times or times[-1] != int(res.exit_ts):
        times.append(int(res.exit_ts))
    rows: list[dict[str, Any]] = []
    for tt in times:
        spot = ctx.mkt.spot(tt)
        rec: dict[str, Any] = {"time_ist": s018.ist_str(tt), "btc_spot": spot}
        gross = 0.0
        for i, lg in enumerate(res.legs):
            if int(lg.fill_ts) > tt:
                rec[f"leg{i}_{lg.role}_mark"] = ""
                rec[f"leg{i}_{lg.role}_pnl"] = ""
                continue
            if lg.exit_ts and int(lg.exit_ts) <= tt:
                px = float(lg.exit_px)
            else:
                ctx.mkt.ensure(lg.symbol, lg.expiry)
                q = ctx.mkt.quote(lg.symbol, tt)
                px = float(q[0]) if q is not None else float("nan")
            pnl = _mtm(lg, px) if px == px else float("nan")
            if pnl == pnl:
                gross += pnl
            rec[f"leg{i}_{lg.role}_mark"] = px
            rec[f"leg{i}_{lg.role}_pnl"] = pnl
        rec["basket_gross"] = gross
        rec["sl_level"] = sl_now(res.steps if tt >= int(res.exit_ts) else res.steps, qty)
        rows.append(rec)
    return rows


def replay_row(ctx: RunContext, row: dict[str, Any]) -> tuple[BasketResult, list[dict[str, Any]], dict[str, Any]]:
    entry_ts = parse_ist(str(row["entry_ist"]))
    sig = ctx.signal_for(row)
    log: list[dict[str, Any]] = []
    res = simulate_basket(
        ctx.mkt,
        ctx.listed,
        entry_ts=entry_ts,
        signal_price=float(row["signal_price"]),
        qty=ctx.qty,
        ohlc=ctx.ohlc,
        event_log=log,
    )
    return res, log, sig


def totals_vs_trade(res: BasketResult, row: dict[str, Any]) -> dict[str, Any]:
    keys = ("gross", "fees", "net", "slip", "net_with_slip")
    out: dict[str, Any] = {}
    for k in keys:
        sim_v = float(getattr(res, k))
        csv_v = float(row[k])
        out[k] = {"sim": sim_v, "trades_csv": csv_v, "diff": sim_v - csv_v}
    return out


def write_md(
    path: Path,
    row: dict[str, Any],
    events: list[dict[str, Any]],
    snaps: list[dict[str, Any]],
    totals: dict[str, Any],
) -> None:
    lines = [
        f"# Trace {row.get('cell')} {row.get('arm')} {row.get('entry_ist')}",
        "",
        f"exit={row.get('exit_reason')}  exit_ist={row.get('exit_ist')}",
        "",
        "## A) Events",
        "",
        "| " + " | ".join(EVENT_COLS[:16]) + " |",
        "| " + " | ".join(["---"] * 16) + " |",
    ]
    for e in events:
        lines.append("| " + " | ".join(str(e.get(c, "")) for c in EVENT_COLS[:16]) + " |")
    lines += ["", "## B) Snapshots (15m)", ""]
    if snaps:
        scols = list(snaps[0].keys())
        lines.append("| " + " | ".join(scols) + " |")
        lines.append("| " + " | ".join(["---"] * len(scols)) + " |")
        for s in snaps:
            lines.append("| " + " | ".join(str(s.get(c, "")) for c in scols) + " |")
    lines += ["", "## C) Totals vs trades.csv", ""]
    lines.append("| field | sim | trades.csv | diff |")
    lines.append("| --- | --- | --- | --- |")
    for k, v in totals.items():
        lines.append(f"| {k} | {v['sim']} | {v['trades_csv']} | {v['diff']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(path: Path, events: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_COLS, extrasaction="ignore")
        w.writeheader()
        for e in events:
            w.writerow(e)


def append_sheet(wb: Workbook, title: str, events: list[dict[str, Any]], snaps: list[dict[str, Any]], totals: dict[str, Any], row: dict[str, Any]) -> None:
    ws = wb.create_sheet(title[:31])
    ws.append(["trades.csv"] + list(row.keys()))
    ws.append(["trades.csv"] + [row.get(k, "") for k in row.keys()])
    ws.append([])
    ws.append(["EVENTS"])
    ws.append(EVENT_COLS)
    for e in events:
        ws.append([e.get(c, "") for c in EVENT_COLS])
    ws.append([])
    ws.append(["SNAPSHOTS_15M"])
    if snaps:
        scols = list(snaps[0].keys())
        ws.append(scols)
        for s in snaps:
            ws.append([s.get(c, "") for c in scols])
    ws.append([])
    ws.append(["TOTALS", "sim", "trades_csv", "diff"])
    for k, v in totals.items():
        ws.append([k, v["sim"], v["trades_csv"], v["diff"]])


def run_trace(folder: Path, pick: str, basket: str) -> list[tuple[dict[str, Any], BasketResult]]:
    rows = load_trades(folder)
    if pick == "auto":
        chosen = pick_auto(rows)
    else:
        chosen = [parse_basket_key(basket, rows)]
    if not chosen:
        raise SystemExit("no baskets picked")
    print(f"picked {len(chosen)} baskets from {folder}", flush=True)
    ctx = RunContext(folder)
    wb = Workbook()
    wb.remove(wb.active)
    done: list[tuple[dict[str, Any], BasketResult]] = []
    for i, row in enumerate(chosen, start=1):
        res, log, sig = replay_row(ctx, row)
        events = event_rows(ctx, row, sig, log)
        snaps = snapshots(ctx, res, ctx.qty)
        tot = totals_vs_trade(res, row)
        reason = str(res.exit_reason or row.get("exit_reason") or "NA")
        stem = folder / f"trace_{i}_{reason}"
        write_csv(Path(str(stem) + ".csv"), events)
        write_md(Path(str(stem) + ".md"), row, events, snaps, tot)
        append_sheet(wb, f"{i}_{reason}", events, snaps, tot, row)
        done.append((row, res))
        print(
            f"  {i} {row['cell']} {row['arm']} {row['entry_ist']} "
            f"exit={res.exit_reason} gross_diff={tot['gross']['diff']}",
            flush=True,
        )
    xlsx = folder / "trace_summary.xlsx"
    wb.save(xlsx)
    print(f"wrote {xlsx}", flush=True)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--pick", default="")
    ap.add_argument("--basket", default="")
    args = ap.parse_args()
    folder = Path(args.run)
    if not folder.is_dir():
        folder = _ROOT / args.run
    if args.pick != "auto" and not args.basket:
        raise SystemExit("need --pick auto or --basket cell|arm|entry_ist")
    run_trace(folder, args.pick, args.basket)


if __name__ == "__main__":
    main()
