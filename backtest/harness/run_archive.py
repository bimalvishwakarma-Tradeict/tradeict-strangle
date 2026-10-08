"""Permanent per-run archive: trades, legs, greeks, Excel report.

Shared across strategies. Does not place orders or touch TRAIN caches.
"""

from __future__ import annotations

import csv
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from backtest.iv_surface import black_vega, norm_pdf
from backtest.s004_gate import black76_abs_delta, implied_vol_bisection

ARCHIVE_ROOT = Path("backtest/archive")
CLAUDE_RUNS = Path("Claude outputs") / "runs"
GREEKS_CSV_MAX = 500_000
GREEKS_XLSX_MAX = 1_000_000
SNAPSHOT_SEC = 15 * 60

CONFIG_COLS: tuple[str, ...] = (
    "month", "tf", "variant", "band", "band_mode", "arm", "n_legs", "dte",
    "tgt", "sl", "n", "mean", "gross", "brokerage", "slippage", "win",
    "C2", "C3", "n_sig", "n_stale", "avg_net_delta", "key",
)
TRADE_COLS: tuple[str, ...] = (
    "trade_id", "month", "tf", "variant", "band", "band_mode", "arm", "n_legs", "dte",
    "side", "entry_ist", "exit_ist", "exit_reason", "hold_hrs", "hrs_to_exp",
    "spot_entry", "spot_exit", "line_level", "vwap_dist",
    "gross", "brokerage", "slippage", "net", "mfe", "mfe_time", "mae", "mae_time",
)
LEG_COLS: tuple[str, ...] = (
    "trade_id", "symbol", "strike", "type", "expiry", "qty", "long_short",
    "entry_mark", "entry_fill", "exit_mark", "exit_fill",
    "fee_entry", "fee_exit", "slip_entry", "slip_exit",
    "iv_entry", "delta_entry", "gamma_entry", "theta_entry", "vega_entry",
    "iv_exit", "delta_exit", "gamma_exit", "theta_exit", "vega_exit",
)
GREEK_COLS: tuple[str, ...] = (
    "trade_id", "ts_ist", "kind", "spot",
    "leg1_mark", "leg1_iv", "leg2_mark", "leg2_iv", "leg3_mark", "leg3_iv",
    "basket_delta", "basket_gamma", "basket_theta", "basket_vega", "basket_mark_pnl",
)


def git_commit_hash() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return str(out).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _args_dict(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, dict):
        raw = args
    else:
        raw = vars(args)
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, Path):
            out[k] = str(v)
        else:
            try:
                json.dumps(v)
                out[k] = v
            except TypeError:
                out[k] = str(v)
    return out


def _nan(x: Any) -> Any:
    if x is None:
        return ""
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return ""
    except TypeError:
        pass
    return x


def black76_gamma(f: float, k: float, t: float, sigma: float) -> float:
    if f <= 0 or k <= 0 or t <= 1e-12 or sigma <= 1e-12:
        return 0.0
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    return float(norm_pdf(d1) / (f * sigma * math.sqrt(t)))


def black76_theta_per_day(f: float, k: float, t: float, sigma: float) -> float:
    if f <= 0 or k <= 0 or t <= 1e-12 or sigma <= 1e-12:
        return 0.0
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    d_v_d_t = f * float(norm_pdf(d1)) * sigma / (2.0 * math.sqrt(t))
    return float(-d_v_d_t / 365.0)


def greeks_from_mark(
    mark: float, spot: float, strike: float, t_yr: float, is_call: bool
) -> dict[str, float]:
    iv = implied_vol_bisection(float(mark), float(spot), float(strike), float(t_yr), bool(is_call))
    nan = float("nan")
    if iv is None or not math.isfinite(iv):
        return {"iv": nan, "delta": nan, "gamma": nan, "theta": nan, "vega": nan}
    ad = black76_abs_delta(float(spot), float(strike), float(t_yr), float(iv), bool(is_call))
    delta = float(ad) if is_call else -float(ad)
    return {
        "iv": float(iv),
        "delta": delta,
        "gamma": black76_gamma(float(spot), float(strike), float(t_yr), float(iv)),
        "theta": black76_theta_per_day(float(spot), float(strike), float(t_yr), float(iv)),
        "vega": float(black_vega(float(spot), float(strike), float(t_yr), float(iv))),
    }


class _Tee:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        n = 0
        for s in self.streams:
            n = s.write(data)
            s.flush()
        return n

    def flush(self) -> None:
        for s in self.streams:
            s.flush()

    def isatty(self) -> bool:
        return False


class RunArchive:
    def __init__(
        self,
        strategy: str,
        mode: str,
        args: Any,
        month: str = "",
        prereg_note: str = "",
        folder: Path | None = None,
    ) -> None:
        self.strategy = str(strategy)
        self.mode = str(mode)
        self.month = str(month)
        self.prereg_note = str(prereg_note or "")
        self.started = datetime.now(timezone.utc)
        stamp = self.started.strftime("%Y%m%dT%H%M%SZ")
        self.stamp = stamp
        if folder is not None:
            self.folder = Path(folder)
        else:
            self.folder = ARCHIVE_ROOT / self.strategy / f"{stamp}_{self.mode}_{self.month or 'na'}"
        self.folder.mkdir(parents=True, exist_ok=True)
        self._orig_stdout = sys.stdout
        self._log_fp = (self.folder / "console.log").open("a", encoding="utf-8")
        sys.stdout = _Tee(self._orig_stdout, self._log_fp)  # type: ignore[assignment]
        self._seen_trades: set[str] = set()
        self._seen_configs: set[str] = set()
        self._n_greeks = 0
        self._load_seen()
        meta = {
            "strategy": self.strategy,
            "mode": self.mode,
            "month": self.month,
            "stamp": stamp,
            "git_commit": git_commit_hash(),
            "cli_args": _args_dict(args),
            "start_utc": self.started.isoformat(),
            "end_utc": None,
            "prereg_note": self.prereg_note,
            "python_version": sys.version,
            "folder": str(self.folder).replace("\\", "/"),
            "net_formula": "net = gross - brokerage (slippage tracked separately)",
        }
        (self.folder / "run_meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        self._ensure_headers()

    def _load_seen(self) -> None:
        fp = self.folder / "trades.csv"
        if not fp.exists():
            return
        with fp.open("r", encoding="utf-8", newline="") as f:
            for rec in csv.DictReader(f):
                tid = str(rec.get("trade_id", ""))
                if tid:
                    self._seen_trades.add(tid)
        cf = self.folder / "configs.csv"
        if cf.exists():
            with cf.open("r", encoding="utf-8", newline="") as f:
                for rec in csv.DictReader(f):
                    ck = str(rec.get("key", ""))
                    if ck:
                        self._seen_configs.add(ck)
        gf = self.folder / "greeks.csv"
        if gf.exists():
            with gf.open("r", encoding="utf-8", newline="") as f:
                self._n_greeks = max(0, sum(1 for _ in f) - 1)

    def _ensure_headers(self) -> None:
        for name, cols in (
            ("configs.csv", CONFIG_COLS),
            ("trades.csv", TRADE_COLS),
            ("legs.csv", LEG_COLS),
            ("greeks.csv", GREEK_COLS),
        ):
            p = self.folder / name
            if p.exists() and p.stat().st_size > 0:
                continue
            with p.open("w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=list(cols)).writeheader()

    def _append(self, name: str, cols: tuple[str, ...], row: dict[str, Any]) -> None:
        p = self.folder / name
        with p.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(cols), extrasaction="ignore")
            w.writerow({c: _nan(row.get(c, "")) for c in cols})
            f.flush()

    def add_config_result(self, rec: dict[str, Any]) -> None:
        ck = str(rec.get("key", ""))
        if ck and ck in self._seen_configs:
            return
        if ck:
            self._seen_configs.add(ck)
        row = dict(rec)
        if "brokerage" not in row and "fee" in row:
            row["brokerage"] = rec.get("fee")
        if "slippage" not in row and "slip" in row:
            row["slippage"] = rec.get("slip")
        self._append("configs.csv", CONFIG_COLS, row)

    def add_trade(
        self,
        trade_dict: dict[str, Any],
        legs: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> None:
        tid = str(trade_dict.get("trade_id", ""))
        if not tid or tid in self._seen_trades:
            return
        self._seen_trades.add(tid)
        self._append("trades.csv", TRADE_COLS, trade_dict)
        for lg in legs:
            lg2 = dict(lg)
            lg2["trade_id"] = tid
            self._append("legs.csv", LEG_COLS, lg2)
        for sn in snapshots:
            sn2 = dict(sn)
            sn2["trade_id"] = tid
            self._append("greeks.csv", GREEK_COLS, sn2)
            self._n_greeks += 1

    def _write_parquet(self) -> None:
        src = self.folder / "greeks.csv"
        dest = self.folder / "greeks.parquet"
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq

        if (not src.exists()) or src.stat().st_size < 8:
            import pyarrow as pa

            table = pa.table({c: [] for c in GREEK_COLS})
            pq.write_table(table, dest)
            return
        table = pacsv.read_csv(src)
        pq.write_table(table, dest)
        if self._n_greeks >= GREEKS_CSV_MAX:
            src.unlink(missing_ok=True)

    def _xlsx_sheet(self, wb: Any, title: str, path: Path, limit: int | None = None) -> None:
        ws = wb.create_sheet(title)
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", newline="") as f:
            rdr = csv.reader(f)
            for i, row in enumerate(rdr):
                if limit is not None and i > limit:
                    ws.append([f"truncated after {limit} data rows"])
                    break
                ws.append(row)

    def _write_xlsx(self) -> Path:
        from openpyxl import Workbook

        wb = Workbook()
        meta = json.loads((self.folder / "run_meta.json").read_text(encoding="utf-8"))
        ws = wb.active
        ws.title = "Summary"
        ws.append(["strategy", self.strategy])
        ws.append(["mode", self.mode])
        ws.append(["month", self.month])
        ws.append(["stamp", self.stamp])
        ws.append(["git_commit", meta.get("git_commit", "")])
        ws.append(["python", meta.get("python_version", "")])
        ws.append(["start_utc", meta.get("start_utc", "")])
        ws.append(["end_utc", meta.get("end_utc", "")])
        ws.append(["prereg_note", self.prereg_note])
        ws.append(["folder", str(self.folder)])
        ws.append([])
        ws.append(["configs (top)"])
        cfgp = self.folder / "configs.csv"
        if cfgp.exists():
            with cfgp.open("r", encoding="utf-8", newline="") as f:
                for i, row in enumerate(csv.reader(f)):
                    if i > 80:
                        break
                    ws.append(row)
        self._xlsx_sheet(wb, "Configs", cfgp)
        self._xlsx_sheet(wb, "Trades", self.folder / "trades.csv")
        self._xlsx_sheet(wb, "Legs", self.folder / "legs.csv")
        gk = self.folder / "greeks.csv"
        notes = wb.create_sheet("Notes")
        notes.append(["net = gross - brokerage; slippage is a separate column (not subtracted from net)."])
        notes.append(["fees=estimate_option_fee*1.18 (GST); slip=slip_pct on fill vs mark."])
        notes.append(["C2/C3: summary only on Configs; trade rows are main-arm fills."])
        notes.append(["IV = Black-76 implied vol from mark (r=0) via implied_vol_bisection."])
        notes.append(["Greeks reuse s004_gate + iv_surface.black_vega; gamma/theta from same d1."])
        notes.append(["Filters: lunch 05:30-08:30 IST; Thu 17:30-Sat 17:30 IST skip."])
        notes.append(["prereg_note", self.prereg_note])
        if gk.exists() and self._n_greeks < GREEKS_XLSX_MAX:
            self._xlsx_sheet(wb, "Greeks", gk)
        else:
            gws = wb.create_sheet("Greeks")
            gws.append(["Full 15m greeks omitted (>1M rows). Entry/exit/MFE/MAE only below."])
            gws.append(list(GREEK_COLS))
            if gk.exists():
                with gk.open("r", encoding="utf-8", newline="") as f:
                    rdr = csv.DictReader(f)
                    for rec in rdr:
                        if str(rec.get("kind", "")) in ("entry", "exit", "mfe", "mae"):
                            gws.append([rec.get(c, "") for c in GREEK_COLS])
            notes.append(["Greeks sheet: entry/exit/MFE/MAE only because row count exceeded 1M."])
        xlsx = self.folder / "report.xlsx"
        wb.save(xlsx)
        CLAUDE_RUNS.mkdir(parents=True, exist_ok=True)
        copy_name = f"{self.strategy}_{self.mode}_{self.month}_{self.stamp}.xlsx"
        dest = CLAUDE_RUNS / copy_name
        dest.write_bytes(xlsx.read_bytes())
        print(f"archive xlsx copy {dest}", flush=True)
        return xlsx

    def finalize(self) -> None:
        ended = datetime.now(timezone.utc)
        meta_p = self.folder / "run_meta.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        meta["end_utc"] = ended.isoformat()
        meta["n_trades"] = len(self._seen_trades)
        meta["n_greeks"] = self._n_greeks
        meta_p.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        self._write_parquet()
        self._write_xlsx()
        print(
            f"archive {self.folder} trades={len(self._seen_trades)} greeks={self._n_greeks}",
            flush=True,
        )
        sys.stdout = self._orig_stdout
        self._log_fp.close()
