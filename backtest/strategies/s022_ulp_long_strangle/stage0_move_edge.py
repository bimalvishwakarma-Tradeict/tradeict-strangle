#!/usr/bin/env python3
"""S022 Stage 0: does ULP fresh-touch predict a bigger BTC move than random times?

Spot only. No option sim.

python backtest/strategies/s022_ulp_long_strangle/stage0_move_edge.py
"""

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
from zoneinfo import ZoneInfo

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

IST = ZoneInfo("Asia/Kolkata")
IST_OFF = 19800
SEED = 22
N_CTRL = 20
N_PERM = 10_000
MIN_SEP = 3600
TFS = ("5m", "15m", "30m")
MODES = ("wick", "full")
H_KEYS = ("1h", "2h", "4h", "8h", "1730")
H_SEC = {"1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800}
METRICS = ("mfe_abs", "end_abs", "rv", "hit_1000", "hit_1500", "hit_2000", "zone_crossed")
RV_ANN = math.sqrt(365.25 * 24.0 * 60.0)
PKG_DIR = Path(__file__).resolve().parent
RUNS_DIR = PKG_DIR / "runs"
RANGE_YM_LO = 202407
RANGE_YM_HI = 202609


def next_1730_ist(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST)
    cand = dt.replace(hour=17, minute=30, second=0, microsecond=0)
    unix = int(cand.astimezone(timezone.utc).timestamp())
    if unix > int(ts):
        return unix
    return int((cand + timedelta(days=1)).astimezone(timezone.utc).timestamp())


def window_end(signal_ts: int, h_key: str) -> int:
    if h_key == "1730":
        return next_1730_ist(int(signal_ts))
    return int(signal_ts) + int(H_SEC[h_key])


def ist_hod(ts: int) -> int:
    return int(((int(ts) + IST_OFF) // 3600) % 24)


def ist_ym(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts) + IST_OFF, tz=timezone.utc)
    return int(dt.year) * 100 + int(dt.month)


def ist_ym_arr(ts: np.ndarray) -> np.ndarray:
    shifted = (ts.astype(np.int64) + IST_OFF).astype("datetime64[s]")
    months = shifted.astype("datetime64[M]").astype(np.int64)
    year = months // 12 + 1970
    month = months % 12 + 1
    return (year * 100 + month).astype(np.int32)


def ist_hod_arr(ts: np.ndarray) -> np.ndarray:
    return (((ts.astype(np.int64) + IST_OFF) // 3600) % 24).astype(np.int16)


def collapse_events(sigs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One event per (tf, mode, signal_ts); keep every touched zone for crossing."""
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    order: list[tuple[str, str, int]] = []
    for s in sigs:
        if str(s.get("rule")) != "fresh":
            continue
        key = (str(s["tf"]), str(s["mode"]), int(s["signal_ts"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(s)
    out: list[dict[str, Any]] = []
    for key in order:
        rows = groups[key]
        zones = [
            (str(r["zone_kind"]), float(r["zone_top"]), float(r["zone_bottom"]))
            for r in rows
        ]
        kinds = {z[0] for z in zones}
        r0 = rows[0]
        sts = int(r0["signal_ts"])
        out.append(
            {
                "tf": str(r0["tf"]),
                "mode": str(r0["mode"]),
                "signal_ts": sts,
                "signal_ist": str(r0.get("signal_ist") or ""),
                "entry": float(r0["spot_close"]),
                "zone_kind": "both" if len(kinds) > 1 else str(r0["zone_kind"]),
                "zone_top": float(r0["zone_top"]),
                "zone_bottom": float(r0["zone_bottom"]),
                "zones": zones,
                "n_raw": len(rows),
                "hod": ist_hod(sts),
                "ym": ist_ym(sts),
            }
        )
    return out


def forbidden_mask(ts_1m: np.ndarray, sig_times: np.ndarray, sep: int = MIN_SEP) -> np.ndarray:
    """True where a 1m timestamp is within ±sep of any signal time."""
    n = int(ts_1m.size)
    if n == 0:
        return np.zeros(0, dtype=bool)
    sig = np.unique(np.asarray(sig_times, dtype=np.int64))
    if sig.size == 0:
        return np.zeros(n, dtype=bool)
    t = ts_1m.astype(np.int64)
    idx = np.searchsorted(sig, t)
    prev_i = np.clip(idx - 1, 0, sig.size - 1)
    next_i = np.clip(idx, 0, sig.size - 1)
    dist = np.minimum(np.abs(t - sig[prev_i]), np.abs(sig[next_i] - t))
    return dist <= int(sep)


def sample_control_times(
    pool_ts: np.ndarray,
    n: int,
    rng: np.random.Generator,
) -> np.ndarray:
    if pool_ts.size == 0 or n <= 0:
        return np.zeros(0, dtype=np.int64)
    if pool_ts.size <= n:
        return pool_ts.astype(np.int64)
    pick = rng.choice(pool_ts.size, size=int(n), replace=False)
    return pool_ts[pick].astype(np.int64)


def _zone_crossed(hi: np.ndarray, lo: np.ndarray, zones: list[tuple[str, float, float]]) -> float:
    if hi.size == 0 or not zones:
        return 0.0
    for kind, top, bot in zones:
        if kind == "high" and float(np.max(hi)) >= float(top):
            return 1.0
        if kind == "low" and float(np.min(lo)) <= float(bot):
            return 1.0
    return 0.0


def window_metrics(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    cl: np.ndarray,
    t0: int,
    t1: int,
    entry: float,
    zones: list[tuple[str, float, float]] | None,
) -> dict[str, float]:
    """Metrics on 1m bars with t0 < ts <= t1. Spike AT t0 must not count."""
    nan = float("nan")
    empty = {
        "mfe_abs": nan,
        "end_abs": nan,
        "rv": nan,
        "hit_1000": nan,
        "hit_1500": nan,
        "hit_2000": nan,
        "zone_crossed": nan,
        "n_bars": 0.0,
    }
    i0 = int(np.searchsorted(ts, int(t0), side="right"))
    i1 = int(np.searchsorted(ts, int(t1), side="right"))
    if i1 <= i0:
        return empty
    h = hi[i0:i1]
    l = lo[i0:i1]
    c = cl[i0:i1]
    mx = float(np.max(h))
    mn = float(np.min(l))
    mfe = max(mx - float(entry), float(entry) - mn)
    end_abs = abs(float(c[-1]) - float(entry))
    if c.size >= 2:
        rets = np.diff(np.log(np.clip(c.astype(np.float64), 1e-12, None)))
        sd = float(np.std(rets, ddof=1)) if rets.size >= 2 else (
            float(np.std(rets, ddof=0)) if rets.size == 1 else nan
        )
        rv = sd * RV_ANN if math.isfinite(sd) else nan
    else:
        rv = nan
    zc = _zone_crossed(h, l, zones or [])
    return {
        "mfe_abs": mfe,
        "end_abs": end_abs,
        "rv": rv,
        "hit_1000": 1.0 if mfe >= 1000.0 else 0.0,
        "hit_1500": 1.0 if mfe >= 1500.0 else 0.0,
        "hit_2000": 1.0 if mfe >= 2000.0 else 0.0,
        "zone_crossed": zc,
        "n_bars": float(c.size),
    }


def metrics_all_h(
    ts: np.ndarray,
    hi: np.ndarray,
    lo: np.ndarray,
    cl: np.ndarray,
    t0: int,
    entry: float,
    zones: list[tuple[str, float, float]] | None,
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for hk in H_KEYS:
        out[hk] = window_metrics(ts, hi, lo, cl, t0, window_end(t0, hk), entry, zones)
    return out


def _mean(xs: list[float]) -> float:
    v = [float(x) for x in xs if x == x]
    return float(np.mean(v)) if v else float("nan")


def _median(xs: list[float]) -> float:
    v = [float(x) for x in xs if x == x]
    return float(np.median(v)) if v else float("nan")


def perm_pvalue(
    signal_mean: float,
    control_vals: np.ndarray,
    n_events: int,
    rng: np.random.Generator,
    n_perm: int = N_PERM,
) -> float:
    if not math.isfinite(signal_mean) or n_events <= 0 or control_vals.size == 0:
        return float("nan")
    pool = control_vals[np.isfinite(control_vals)]
    if pool.size == 0:
        return float("nan")
    draws = rng.choice(pool, size=(int(n_perm), int(n_events)), replace=True)
    means = draws.mean(axis=1)
    return float(np.mean(means >= float(signal_mean)))


def _fmt(x: Any) -> str:
    if isinstance(x, float):
        if not math.isfinite(x):
            return "nan"
        return f"{x:.6g}"
    return str(x)


def _unique_folder(root: Path, stamp: str) -> Path:
    folder = root / f"{stamp}_stage0"
    n = 0
    while folder.exists():
        n += 1
        folder = root / f"{stamp}_stage0_{n}"
    return folder


def _bars_from_ohlc(
    tts: Any, to_: Any, th: Any, tl: Any, tc: Any,
) -> list[dict[str, Any]]:
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


def main() -> None:
    t_run = time.perf_counter()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    from backtest.harness.run_archive import RunArchive
    from backtest.strategies.s018_4h_trend import run_s018 as s018
    from backtest.strategies.s020_vwap_touch import run_s020 as s020
    from backtest.strategies.s020_vwap_touch import run_s020_dev as d
    from backtest.strategies.s022_ulp_long_strangle.signals import iter_ulp_signals

    csv_path = str(args.csv or s018.SPOT_CSV)
    args.csv = csv_path
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = _unique_folder(RUNS_DIR, stamp)
    archive = RunArchive(
        "s022",
        "stage0",
        args,
        month="2024-07_2026-09",
        prereg_note="Stage 0 move-edge vs hour-matched random control; spot only",
        folder=folder,
    )
    print(f"archive folder={archive.folder}", flush=True)

    spot = s018.load_spot_1m(csv_path)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    print(f"1m bars={len(ts)} csv={csv_path}", flush=True)
    hod_1m = ist_hod_arr(ts)
    ym_1m = ist_ym_arr(ts)
    entry_close = {int(t): float(px) for t, px in zip(ts, c)}

    rng = np.random.default_rng(SEED)
    perm_rng = np.random.default_rng(SEED)

    event_rows: list[dict[str, Any]] = []
    ctrl_fp = (archive.folder / "controls.csv").open("w", newline="", encoding="utf-8")
    ctrl_cols = ["event_id", "tf", "mode", "ctrl_i", "ctrl_ts", "hod", "ym"]
    for hk in H_KEYS:
        for m in METRICS:
            ctrl_cols.append(f"{m}_{hk}")
    ctrl_w = csv.DictWriter(ctrl_fp, fieldnames=ctrl_cols, extrasaction="ignore")
    ctrl_w.writeheader()

    sig_vals: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    ctrl_vals: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    month_sig: dict[tuple[int, str, str, str], list[float]] = defaultdict(list)
    month_ctrl: dict[tuple[int, str, str, str], list[float]] = defaultdict(list)
    hour_hist: dict[tuple[str, str, int], int] = defaultdict(int)
    n_dup_collapsed = 0
    n_events_tot = 0
    event_id = 0

    for tf in TFS:
        tf_sec = int(d.LINE_TF[tf])
        tts, to_, th, tl, tc, _tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
        bars = _bars_from_ohlc(tts, to_, th, tl, tc)
        print(f"tf={tf} bars={len(bars)}", flush=True)
        for mode in MODES:
            raw = iter_ulp_signals(bars, mode, tf)
            fresh = [s for s in raw if s["rule"] == "fresh"]
            events = collapse_events(fresh)
            n_dup_collapsed += max(0, len(fresh) - len(events))
            events = [
                e for e in events
                if RANGE_YM_LO <= int(e["ym"]) <= RANGE_YM_HI
            ]
            print(f"  mode={mode} fresh={len(fresh)} events={len(events)}", flush=True)
            sig_arr = np.array([int(e["signal_ts"]) for e in events], dtype=np.int64)
            near = forbidden_mask(ts, sig_arr, MIN_SEP)
            for e in events:
                hour_hist[(tf, mode, int(e["hod"]))] += 1
            by_pool: dict[tuple[int, int], np.ndarray] = {}

            def pool_for(hod: int, ym: int) -> np.ndarray:
                k = (int(hod), int(ym))
                if k not in by_pool:
                    m = (hod_1m == k[0]) & (ym_1m == k[1]) & (~near)
                    by_pool[k] = ts[m]
                return by_pool[k]

            for e in events:
                t0 = int(e["signal_ts"])
                entry = float(e["entry"])
                zones = list(e["zones"])
                sm = metrics_all_h(ts, h, l, c, t0, entry, zones)
                if all(sm[hk]["n_bars"] <= 0 for hk in H_KEYS):
                    continue
                event_id += 1
                n_events_tot += 1
                rec: dict[str, Any] = {
                    "event_id": event_id,
                    "tf": tf,
                    "mode": mode,
                    "signal_ts": t0,
                    "signal_ist": e["signal_ist"],
                    "hod": e["hod"],
                    "ym": e["ym"],
                    "entry": entry,
                    "zone_kind": e["zone_kind"],
                    "zone_top": e["zone_top"],
                    "zone_bottom": e["zone_bottom"],
                    "n_raw": e["n_raw"],
                }
                for hk in H_KEYS:
                    for mk in METRICS:
                        rec[f"{mk}_{hk}"] = sm[hk][mk]
                    rec[f"n_bars_{hk}"] = sm[hk]["n_bars"]
                    for mk in METRICS:
                        sig_vals[(tf, mode, hk, mk)].append(float(sm[hk][mk]))
                    if hk in ("2h", "4h"):
                        month_sig[(int(e["ym"]), tf, mode, hk)].append(float(sm[hk]["mfe_abs"]))
                event_rows.append(rec)

                pool = pool_for(int(e["hod"]), int(e["ym"]))
                ctrls = sample_control_times(pool, N_CTRL, rng)
                for ci, ct in enumerate(ctrls):
                    ct = int(ct)
                    c_entry = float(entry_close.get(ct, float("nan")))
                    if not math.isfinite(c_entry):
                        continue
                    cm = metrics_all_h(ts, h, l, c, ct, c_entry, None)
                    crow: dict[str, Any] = {
                        "event_id": event_id,
                        "tf": tf,
                        "mode": mode,
                        "ctrl_i": ci,
                        "ctrl_ts": ct,
                        "hod": ist_hod(ct),
                        "ym": ist_ym(ct),
                    }
                    for hk in H_KEYS:
                        for mk in METRICS:
                            val = float(cm[hk][mk])
                            crow[f"{mk}_{hk}"] = val
                            ctrl_vals[(tf, mode, hk, mk)].append(val)
                        if hk in ("2h", "4h"):
                            month_ctrl[(int(e["ym"]), tf, mode, hk)].append(
                                float(cm[hk]["mfe_abs"])
                            )
                    ctrl_w.writerow(crow)
                if n_events_tot % 2000 == 0:
                    print(f"    events done={n_events_tot}", flush=True)

    ctrl_fp.close()

    ev_cols = [
        "event_id", "tf", "mode", "signal_ts", "signal_ist", "hod", "ym",
        "entry", "zone_kind", "zone_top", "zone_bottom", "n_raw",
    ]
    for hk in H_KEYS:
        for mk in METRICS:
            ev_cols.append(f"{mk}_{hk}")
        ev_cols.append(f"n_bars_{hk}")
    ev_path = archive.folder / "events.csv"
    with ev_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ev_cols, extrasaction="ignore")
        w.writeheader()
        for r in event_rows:
            w.writerow(r)

    stats_rows: list[dict[str, Any]] = []
    for tf in TFS:
        for mode in MODES:
            for hk in H_KEYS:
                n_ev = 0
                for mk in METRICS:
                    sv = sig_vals[(tf, mode, hk, mk)]
                    cv = np.asarray(ctrl_vals[(tf, mode, hk, mk)], dtype=np.float64)
                    n_ev = sum(1 for x in sv if x == x)
                    smn, smd = _mean(sv), _median(sv)
                    cmn, cmd = (
                        float(np.nanmean(cv)) if cv.size else float("nan"),
                        float(np.nanmedian(cv)) if cv.size else float("nan"),
                    )
                    ratio = (smn / cmn) if (math.isfinite(smn) and math.isfinite(cmn) and cmn != 0) else float("nan")
                    p = perm_pvalue(smn, cv, n_ev, perm_rng, N_PERM)
                    stats_rows.append({
                        "tf": tf, "mode": mode, "H": hk, "metric": mk,
                        "n_events": n_ev,
                        "signal_mean": smn, "signal_median": smd,
                        "control_mean": cmn, "control_median": cmd,
                        "ratio": ratio, "p_ge_signal_mean": p,
                    })

    st_cols = [
        "tf", "mode", "H", "metric", "n_events",
        "signal_mean", "signal_median", "control_mean", "control_median",
        "ratio", "p_ge_signal_mean",
    ]
    st_path = archive.folder / "stats.csv"
    with st_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=st_cols)
        w.writeheader()
        for r in stats_rows:
            w.writerow(r)

    month_rows: list[dict[str, Any]] = []
    for key in sorted(month_sig):
        ym, tf, mode, hk = key
        smn = _mean(month_sig[key])
        cmn = _mean(month_ctrl.get(key, []))
        ratio = (smn / cmn) if (math.isfinite(smn) and math.isfinite(cmn) and cmn != 0) else float("nan")
        month_rows.append({
            "ym": ym, "tf": tf, "mode": mode, "H": hk,
            "n_signal": sum(1 for x in month_sig[key] if x == x),
            "signal_mean_mfe": smn, "control_mean_mfe": cmn, "ratio": ratio,
        })
    mo_path = archive.folder / "per_month.csv"
    with mo_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["ym", "tf", "mode", "H", "n_signal", "signal_mean_mfe", "control_mean_mfe", "ratio"],
        )
        w.writeheader()
        for r in month_rows:
            w.writerow(r)

    hr_rows: list[dict[str, Any]] = []
    for tf in TFS:
        for mode in MODES:
            for hod in range(24):
                hr_rows.append({
                    "tf": tf, "mode": mode, "hod_ist": hod,
                    "n": hour_hist.get((tf, mode, hod), 0),
                })
    hr_path = archive.folder / "hours.csv"
    with hr_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tf", "mode", "hod_ist", "n"])
        w.writeheader()
        for r in hr_rows:
            w.writerow(r)

    from openpyxl import Workbook

    wb = Workbook()

    def _sheet(title: str, rows: list[dict[str, Any]], cols: list[str]) -> None:
        ws = wb.active if title == "stats" else wb.create_sheet(title)
        if title == "stats":
            ws.title = "stats"
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])

    _sheet("stats", stats_rows, st_cols)
    _sheet("per_month", month_rows, ["ym", "tf", "mode", "H", "n_signal", "signal_mean_mfe", "control_mean_mfe", "ratio"])
    _sheet("hours", hr_rows, ["tf", "mode", "hod_ist", "n"])
    _sheet("events", event_rows, ev_cols)
    xlsx_path = archive.folder / "stage0.xlsx"
    wb.save(xlsx_path)

    elapsed = time.perf_counter() - t_run
    lines: list[str] = [
        "S022 Stage 0 — ULP fresh-touch vs hour-matched random control (spot only)",
        f"folder={archive.folder}",
        f"range IST months {RANGE_YM_LO}..{RANGE_YM_HI}  csv={csv_path}",
        f"seed={SEED}  n_ctrl={N_CTRL}  n_perm={N_PERM}  min_sep_sec={MIN_SEP}",
        f"n_events={n_events_tot}  collapsed_raw_dups={n_dup_collapsed}",
        f"runtime_sec={elapsed:.1f}",
        "",
        "ratio = signal_mean / control_mean. p = share of 10k control resamples with mean >= signal mean.",
        "H=1730 is path until next 17:30 IST after signal_ts.",
        "",
    ]
    for tf in TFS:
        for mode in MODES:
            lines.append(f"=== {tf} {mode} ===")
            n_show = next((r["n_events"] for r in stats_rows if r["tf"] == tf and r["mode"] == mode and r["H"] == "1h" and r["metric"] == "mfe_abs"), 0)
            lines.append(f"  n_events={n_show}")
            for hk in H_KEYS:
                block = [r for r in stats_rows if r["tf"] == tf and r["mode"] == mode and r["H"] == hk]
                by_m = {r["metric"]: r for r in block}
                mfe = by_m.get("mfe_abs", {})
                end = by_m.get("end_abs", {})
                rv = by_m.get("rv", {})
                h1 = by_m.get("hit_1000", {})
                zc = by_m.get("zone_crossed", {})
                lines.append(
                    f"  [{hk}] mfe_abs sig={_fmt(mfe.get('signal_mean', float('nan')))} "
                    f"ctrl={_fmt(mfe.get('control_mean', float('nan')))} "
                    f"ratio={_fmt(mfe.get('ratio', float('nan')))} "
                    f"p={_fmt(mfe.get('p_ge_signal_mean', float('nan')))}"
                )
                lines.append(
                    f"        end_abs ratio={_fmt(end.get('ratio', float('nan')))} "
                    f"rv ratio={_fmt(rv.get('ratio', float('nan')))} "
                    f"hit_1000 sig={_fmt(h1.get('signal_mean', float('nan')))} "
                    f"ctrl={_fmt(h1.get('control_mean', float('nan')))} "
                    f"zone_crossed sig={_fmt(zc.get('signal_mean', float('nan')))}"
                )
            lines.append("")
    lines.append("--- mfe_abs ratio by month (2h / 4h), wick 15m ---")
    for r in month_rows:
        if r["tf"] == "15m" and r["mode"] == "wick":
            lines.append(
                f"  {r['ym']} H={r['H']} n={r['n_signal']} ratio={_fmt(r['ratio'])}"
            )
    lines.append("")
    lines.append("--- IST hour-of-day histogram (15m wick) ---")
    for hod in range(24):
        n = hour_hist.get(("15m", "wick", hod), 0)
        lines.append(f"  hod={hod:02d} n={n}")
    lines.append("")
    text = "\n".join(lines)
    sum_path = archive.folder / "summary.txt"
    sum_path.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    print(f"wrote {ev_path}", flush=True)
    print(f"wrote {archive.folder / 'controls.csv'}", flush=True)
    print(f"wrote {st_path}", flush=True)
    print(f"wrote {xlsx_path}", flush=True)
    print(f"wrote {sum_path}", flush=True)
    print(f"runtime_sec={elapsed:.1f}", flush=True)

    sys.stdout = archive._orig_stdout
    archive._log_fp.close()


if __name__ == "__main__":
    main()
