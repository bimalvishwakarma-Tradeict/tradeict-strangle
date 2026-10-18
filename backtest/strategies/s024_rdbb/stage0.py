#!/usr/bin/env python3
"""S024 RDBB stage0: underlying signal quality vs VOLCTRL/C2 (no occupancy)."""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.harness.data import ist_dt, to_unix  # noqa: E402
from backtest.harness.run_archive import RunArchive  # noqa: E402
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s022_ulp_long_strangle.run_s022 import (  # noqa: E402
    _c2_events,
    _volctrl_events,
)
from backtest.strategies.s022_ulp_long_strangle.stage0_filter import (  # noqa: E402
    attach_metrics,
    ratio_means,
    welch_t,
)
from backtest.strategies.s022_ulp_long_strangle.stage0_move_edge import (  # noqa: E402
    SEED,
    assign_quantile_bins,
    ffill_1m_grid,
    forbidden_mask,
    ist_hod,
    ist_hod_arr,
    ist_ym,
    rolling_pre_range_60,
)
from backtest.strategies.s024_rdbb.signals import (  # noqa: E402
    FILTERS,
    RSI_SETS,
    TFS,
    apply_move_filter,
    iter_rdbb_signals,
    parse_filter,
    weekend_bucket,
)

PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
HORIZONS = ("1h", "2h", "4h")
METS = ("mfe", "absmove")


def _window(month: str) -> tuple[int, int]:
    win_from, win_to = d.month_bounds(month)
    start = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    return start, cutoff


def _bars_from_ohlc(tts: Any, to_: Any, th: Any, tl: Any, tc: Any) -> list[dict[str, Any]]:
    return [
        {
            "time": int(t),
            "open": float(o),
            "high": float(h),
            "low": float(lo),
            "close": float(c),
        }
        for t, o, h, lo, c in zip(tts, to_, th, tl, tc)
    ]


def _px_at(close_1m: dict[int, float], t: int) -> float:
    px = close_1m.get(int(t))
    if px is None:
        return float("nan")
    return float(px)


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return "nan"
        return f"{x:.4f}"
    return str(x)


def _tag_controls(
    rows: list[dict[str, Any]],
    rsi_set: str,
    filt: str,
    close_1m: dict[int, float],
    ts: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    arm: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        t0 = int(r["signal_ts"])
        px = _px_at(close_1m, t0)
        row = {
            "arm": arm,
            "event_id": r.get("event_id", ""),
            "tf": r.get("tf", ""),
            "mode": rsi_set,
            "rsi_set": rsi_set,
            "filter": filt,
            "side": "",
            "bucket": weekend_bucket(t0),
            "signal_ts": t0,
            "entry_time": t0,
            "entry_ts": t0,
            "signal_price": px,
            "signal_ist": r.get("signal_ist", s018.ist_str(t0)),
            "hod": int(r.get("hod") or ist_hod(t0)),
            "ym": int(r.get("ym") or ist_ym(t0)),
            "src_event": r.get("src_event", ""),
        }
        attach_metrics(row, ts, h, l, c, px)
        out.append(row)
    return out


def _cell_stats(f_rows: list[dict[str, Any]], v_rows: list[dict[str, Any]], c2_rows: list[dict[str, Any]], months: list[str]) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "n": len(f_rows),
        "n_WEEKDAY": sum(1 for e in f_rows if e.get("bucket") == "WEEKDAY"),
        "n_WEEKEND": sum(1 for e in f_rows if e.get("bucket") == "WEEKEND"),
        "n_C2": len(c2_rows),
        "n_VOLCTRL": len(v_rows),
    }
    for ym in months:
        ymi = int(ym.replace("-", ""))
        cell[f"n_{ym}"] = sum(1 for e in f_rows if int(e["ym"]) == ymi)
        cell[f"n_WEEKDAY_{ym}"] = sum(
            1 for e in f_rows if int(e["ym"]) == ymi and e.get("bucket") == "WEEKDAY"
        )
        cell[f"n_WEEKEND_{ym}"] = sum(
            1 for e in f_rows if int(e["ym"]) == ymi and e.get("bucket") == "WEEKEND"
        )
    for met, col in (("mfe", "mfe"), ("absmove", "absmove")):
        for hk in HORIZONS:
            key = f"{col}_{hk}"
            fv = [float(e.get(key, float("nan"))) for e in f_rows]
            vv = [float(e.get(key, float("nan"))) for e in v_rows]
            cv = [float(e.get(key, float("nan"))) for e in c2_rows]
            cell[f"F_{met}_{hk}"] = float(np.nanmean(fv)) if fv else float("nan")
            cell[f"VOLCTRL_{met}_{hk}"] = float(np.nanmean(vv)) if vv else float("nan")
            cell[f"C2_{met}_{hk}"] = float(np.nanmean(cv)) if cv else float("nan")
            cell[f"F_VOLCTRL_{met}_{hk}"] = ratio_means(fv, vv)
            cell[f"t_VOLCTRL_{met}_{hk}"] = welch_t(fv, vv)
            cell[f"F_C2_{met}_{hk}"] = ratio_means(fv, cv)
            cell[f"t_C2_{met}_{hk}"] = welch_t(fv, cv)
    return cell


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", default="2025-06,2025-07")
    ap.add_argument("--tf", default="")
    ap.add_argument("--rsi-set", default="")
    ap.add_argument("--filter", default="")
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    args = ap.parse_args()
    months = [m.strip() for m in str(args.months).split(",") if m.strip()]
    for m in months:
        if m in ("2025-08", "2025-09"):
            raise SystemExit("Aug/Sep are OOS — do not run stage0 on them")
    tfs = [args.tf] if args.tf else list(TFS)
    rsi_sets = [args.rsi_set] if args.rsi_set else list(RSI_SETS)
    filts = [args.filter] if args.filter else list(FILTERS)
    for f in filts:
        parse_filter(f)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = RUNS_DIR / f"{stamp}_s024_stage0"
    n = 0
    while folder.exists():
        n += 1
        folder = RUNS_DIR / f"{stamp}_s024_stage0_{n}"
    archive = RunArchive(
        "s024",
        "stage0",
        args,
        month=",".join(months),
        prereg_note="S024 RDBB stage0; RSI-div + BB break; pre-move filter grid; vs VOLCTRL/C2; no occupancy",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    t_all0 = time.perf_counter()
    w0, _ = _window(months[0])
    _, w1 = _window(months[-1])
    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    lo = w0 - 7 * 86400
    hi = w1 + 5 * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    close_1m = {int(t): float(px) for t, px in zip(ts, c)}
    print(f"1m bars={len(ts)} months={months}", flush=True)

    grid, _go, gh, gl, gc = ffill_1m_grid(ts, o, h, l, c)
    pre = rolling_pre_range_60(gh, gl)
    bins, _edges = assign_quantile_bins(pre)
    hod_g = ist_hod_arr(grid)
    hod_1m = ist_hod_arr(ts)
    cell_id = np.full(grid.shape, -1, dtype=np.int32)
    okb = bins >= 0
    cell_id[okb] = bins[okb].astype(np.int32) * 24 + hod_g[okb].astype(np.int32)

    by_cell_f: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_cell_v: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    by_cell_c2: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    event_rows: list[dict[str, Any]] = []
    rng = np.random.default_rng(SEED)

    month_bounds = {ym: _window(ym) for ym in months}
    for tf in tfs:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        for rsi_set in rsi_sets:
            raw = iter_rdbb_signals(bars, tf, rsi_set, "nofilter")
            print(f"{tf} {rsi_set} nofilter={len(raw)}", flush=True)
            for filt in filts:
                sigs = apply_move_filter(bars, raw, filt)
                for ym in months:
                    start, cutoff = month_bounds[ym]
                    kept = [e for e in sigs if start <= int(e["signal_ts"]) < cutoff]
                    f_ev: list[dict[str, Any]] = []
                    for i, e in enumerate(kept):
                        t0 = int(e["signal_ts"])
                        px = float(e["signal_price"])
                        row = {
                            "arm": "F",
                            "event_id": f"{tf}|{rsi_set}|{filt}|{ym}|{i}|{t0}",
                            "tf": tf,
                            "mode": rsi_set,
                            "rsi_set": rsi_set,
                            "filter": filt,
                            "side": e["side"],
                            "bucket": e["bucket"],
                            "signal_ts": t0,
                            "entry_time": t0,
                            "entry_ts": t0,
                            "signal_price": px,
                            "signal_ist": s018.ist_str(t0),
                            "hod": ist_hod(t0),
                            "ym": ist_ym(t0),
                            "src_event": "",
                            "bar_j": e.get("bar_j"),
                            "bb_upper": e.get("bb_upper"),
                            "bb_lower": e.get("bb_lower"),
                        }
                        attach_metrics(row, ts, h, l, c, px)
                        f_ev.append(row)
                    sig_arr = np.array([int(e["signal_ts"]) for e in f_ev], dtype=np.int64)
                    near_g = forbidden_mask(grid, sig_arr, 3600)
                    near_1 = forbidden_mask(ts, sig_arr, 3600)
                    c2_raw, _miss = _c2_events(f_ev, ts, near_1, hod_1m, rng, start, cutoff)
                    vol_raw, _st = _volctrl_events(
                        f_ev, grid, cell_id, hod_g, rng, start, cutoff, near_g,
                    )
                    c2_ev = _tag_controls(c2_raw, rsi_set, filt, close_1m, ts, h, l, c, "C2")
                    v_ev = _tag_controls(vol_raw, rsi_set, filt, close_1m, ts, h, l, c, "VOLCTRL")
                    key = (tf, rsi_set, filt)
                    by_cell_f[key].extend(f_ev)
                    by_cell_c2[key].extend(c2_ev)
                    by_cell_v[key].extend(v_ev)
                    event_rows.extend(f_ev)
                    event_rows.extend(c2_ev)
                    event_rows.extend(v_ev)

    cells: list[dict[str, Any]] = []
    for tf in tfs:
        for rsi_set in rsi_sets:
            for filt in filts:
                key = (tf, rsi_set, filt)
                row = {"tf": tf, "rsi_set": rsi_set, "filter": filt}
                row.update(_cell_stats(by_cell_f[key], by_cell_v[key], by_cell_c2[key], months))
                cells.append(row)

    ev_cols = [
        "arm", "event_id", "tf", "rsi_set", "filter", "side", "bucket", "ym",
        "signal_ts", "signal_ist", "signal_price", "hod",
        "mfe_1h", "absmove_1h", "mfe_2h", "absmove_2h", "mfe_4h", "absmove_4h",
        "src_event",
    ]
    with (archive.folder / "events.csv").open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=ev_cols, extrasaction="ignore")
        w.writeheader()
        for e in event_rows:
            w.writerow(e)

    sum_cols = list(cells[0].keys()) if cells else ["tf", "rsi_set", "filter"]
    with (archive.folder / "summary.csv").open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=sum_cols, extrasaction="ignore")
        w.writeheader()
        for row in cells:
            w.writerow(row)

    lines: list[str] = [
        "S024 RDBB stage0 (underlying, raw counts, no occupancy)",
        f"months={','.join(months)} cells={len(cells)} tfs={tfs} rsi={rsi_sets} filters={len(filts)}",
        f"folder={archive.folder}",
        "",
        "=== nofilter cells ===",
    ]
    hdr = (
        f"{'tf':>5} {'rsi':>4} {'n':>5} "
        f"{'n06':>5} {'n07':>5} {'WD':>5} {'WE':>5} "
        f"{'F/V mfe2h':>11} {'tV 2h':>8} {'F/C2 mfe2h':>12} {'tC2 2h':>8} "
        f"{'F/V mfe4h':>11} {'tV 4h':>8}"
    )
    lines.append(hdr)
    for row in cells:
        if row["filter"] != "nofilter":
            continue
        lines.append(
            f"{row['tf']:>5} {row['rsi_set']:>4} {int(row['n']):>5} "
            f"{int(row.get('n_2025-06', 0)):>5} {int(row.get('n_2025-07', 0)):>5} "
            f"{int(row['n_WEEKDAY']):>5} {int(row['n_WEEKEND']):>5} "
            f"{_fmt(row['F_VOLCTRL_mfe_2h']):>11} {_fmt(row['t_VOLCTRL_mfe_2h']):>8} "
            f"{_fmt(row['F_C2_mfe_2h']):>12} {_fmt(row['t_C2_mfe_2h']):>8} "
            f"{_fmt(row['F_VOLCTRL_mfe_4h']):>11} {_fmt(row['t_VOLCTRL_mfe_4h']):>8}"
        )
    lines.append("")
    lines.append("=== all cells (n) ===")
    lines.append(f"{'tf':>5} {'rsi':>4} {'filter':>10} {'n':>5} {'WD':>5} {'WE':>5} {'F/V2h':>8} {'tV2h':>8}")
    for row in cells:
        lines.append(
            f"{row['tf']:>5} {row['rsi_set']:>4} {row['filter']:>10} "
            f"{int(row['n']):>5} {int(row['n_WEEKDAY']):>5} {int(row['n_WEEKEND']):>5} "
            f"{_fmt(row['F_VOLCTRL_mfe_2h']):>8} {_fmt(row['t_VOLCTRL_mfe_2h']):>8}"
        )
    txt = "\n".join(lines) + "\n"
    (archive.folder / "summary.txt").write_text(txt, encoding="utf-8")
    print(txt, flush=True)
    elapsed = time.perf_counter() - t_all0
    print(f"done folder={archive.folder} elapsed={elapsed:.1f}s events={len(event_rows)}", flush=True)
    archive.finalize()


if __name__ == "__main__":
    main()
