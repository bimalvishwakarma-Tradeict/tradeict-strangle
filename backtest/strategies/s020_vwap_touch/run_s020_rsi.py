#!/usr/bin/env python3
"""S020 RSI Div Signal runner. Does not modify TRAIN/DEV modules.

python backtest\\strategies\\s020_vwap_touch\\run_s020_rsi.py --month 2025-06 --rsi-grid --max-days 3
python backtest\\strategies\\s020_vwap_touch\\run_s020_rsi.py --month 2025-06 --rsi-hedge-grid --tfs 1m,15m --max-days 3
python backtest\\strategies\\s020_vwap_touch\\run_s020_rsi.py --month 2025-06 --rsi-straddle-grid --tfs 15m --straddle-expiries E_1D --max-days 3
"""

from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.fees_sim import OPTIONS_CONTRACT_VALUE  # noqa: E402
from backtest.harness.data import MarksStore, ist_dt, to_unix  # noqa: E402
from backtest.harness.mark_cache import reset_mark_cache  # noqa: E402
from backtest.harness.run_archive import (  # noqa: E402
    CONFIG_COLS,
    GREEK_COLS,
    LEG_COLS,
    TRADE_COLS,
    RunArchive,
    greeks_from_mark,
)
from backtest.slippage_model import load_slip_table  # noqa: E402
from backtest.strategies.s012_trend_follow.engine import (  # noqa: E402
    intrinsic,
    ist_date,
    t_years,
)
from backtest.strategies.s018_4h_trend import run_s018 as s018  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020 as s020  # noqa: E402
from backtest.strategies.s020_vwap_touch import run_s020_dev as d  # noqa: E402
from backtest.strategies.s020_vwap_touch.rsi_div import (  # noqa: E402
    RSI_EXP_OBH,
    RSI_EXP_OBL,
    detect_rsi_div,
)

logger = logging.getLogger("s020_rsi")

OUT_DIR = Path("backtest/strategies/s020_vwap_touch/runs")
CKPT = OUT_DIR / "s020_rsi_ckpt.jsonl"
ARCHIVE_PTR = OUT_DIR / "s020_rsi_archive_ptr.json"
SIGNAL_NAME = "RSI Div Signal"
CODE_VER = "rsi_v4"
RSI_TFS = ("1m", "3m", "5m", "15m", "30m")
A0_T = (100, 150, 200, 250, 300, 350)
A0_SL = (100, 150, 200, 250, 300, 350)
RSI_LEG_COLS = tuple(list(LEG_COLS) + ["add_no", "entry_ts"])
RSI_TRADE_COLS = tuple(
    list(TRADE_COLS)
    + [
        "qty",
        "avg_entry",
        "strike",
        "debit",
        "iv_call",
        "iv_put",
        "implied_1h",
        "realized_1h",
        "weekday",
    ]
)
WING_NONE = -1.0
_SCALE_SAMPLE_PRINTED = False
STRADDLE_TFS = ("5m", "15m", "30m")
STRADDLE_QTY = 1000
STRADDLE_PRE_EXP_SEC = 30 * 60
STRADDLE_EXP_CODES = ("E_1D", "E_2D", "E_WK")
_STRADDLE_SAMPLES: list[dict[str, Any]] = []
_RATCHET_TRACE_PRINTED = False


class RsiArchive(RunArchive):
    """RunArchive with legs.add_no for SCALE adds. Does not edit run_archive.py."""

    def _ensure_headers(self) -> None:
        import csv as _csv

        for name, cols in (
            ("configs.csv", CONFIG_COLS),
            ("trades.csv", RSI_TRADE_COLS),
            ("legs.csv", RSI_LEG_COLS),
            ("greeks.csv", GREEK_COLS),
        ):
            p = self.folder / name
            if p.exists() and p.stat().st_size > 0:
                continue
            with p.open("w", newline="", encoding="utf-8") as f:
                _csv.DictWriter(f, fieldnames=list(cols)).writeheader()

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
        self._append("trades.csv", RSI_TRADE_COLS, trade_dict)
        for lg in legs:
            lg2 = dict(lg)
            lg2["trade_id"] = tid
            if "add_no" not in lg2:
                lg2["add_no"] = 0
            if "entry_ts" not in lg2:
                lg2["entry_ts"] = ""
            self._append("legs.csv", RSI_LEG_COLS, lg2)
        for sn in snapshots:
            sn2 = dict(sn)
            sn2["trade_id"] = tid
            self._append("greeks.csv", GREEK_COLS, sn2)
            self._n_greeks += 1


def rsi_tag(tf: str) -> str:
    return f"RSI_{tf}"


def _exp_key_part(x: float) -> str:
    v = float(x)
    if v == int(v):
        return str(int(v))
    return str(v)


def rsi_cell_key(
    month: str,
    w: dict[str, Any],
    max_days: int,
    start_ts: int,
    cutoff: int,
    exp_obh: float = RSI_EXP_OBH,
    exp_obl: float = RSI_EXP_OBL,
) -> str:
    if bool(w.get("straddle")):
        return straddle_cell_key(month, w, max_days, start_ts, cutoff, exp_obh, exp_obl)
    base = d.cell_key(
        month, str(w["tf"]), "RSI", int(w["tgt"]), int(w["sl"]), 0, "near",
        legs=int(w["legs"]), dte=int(w["dte"]), trail_arm=float(w["trail_arm"]),
        trail_give=float(w["trail_give"]), trail_cap=float(w["trail_cap"]),
        arm_id=str(w["arm_id"]), basket=str(w["basket"]),
    )
    exp = f"exp={_exp_key_part(exp_obh)}-{_exp_key_part(exp_obl)}"
    return (
        f"{base}|{CODE_VER}|{exp}|md={int(max_days)}|{int(start_ts)}|{int(cutoff)}"
    )


def ckpt_window_ok(
    done: dict[str, dict[str, Any]],
    month: str,
    max_days: int,
    start_ts: int,
    cutoff: int,
) -> bool:
    if not done:
        return True
    for rec in done.values():
        if str(rec.get("month", "")) != month:
            return False
        if str(rec.get("code_ver", "")) != CODE_VER:
            return False
        try:
            if int(rec.get("max_days", -1)) != int(max_days):
                return False
            if int(rec.get("win_start", -1)) != int(start_ts):
                return False
            if int(rec.get("win_cutoff", -1)) != int(cutoff):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _px_idx_for_symbol(path: dict[str, Any], symbol: str, fallback: int) -> int:
    for i, lg in enumerate(list(path.get("legs") or [])[:3]):
        if str(lg.get("symbol", "")) == str(symbol):
            return i
    return min(max(int(fallback), 0), 2)


def emit_archive_trade_scale(
    archive: RsiArchive,
    cfg: dict[str, Any],
    r: dict[str, Any],
    path: dict[str, Any],
) -> None:
    """SCALE archive rows: each add uses its own path px{base-leg-index}."""
    tid = f"{cfg.get('key','')}|{int(r['entry_ts'])}|{r['side']}"
    tranches = list(path.get("scale_tranches") or [])
    qty_base = int(s018.QTY)
    pos = -1.0
    exit_ts = int(r["exit_ts"])
    entry_ts = int(r["entry_ts"])
    reason = str(r.get("reason", r.get("exit_reason", "")))
    spot_e = float(r.get("spot", 0.0))
    spot_x = float(r.get("spot_exit", 0.0))
    if spot_x <= 0:
        p0 = tranches[0]["path"] if tranches else path
        xi0 = d._path_i_at(p0, exit_ts)
        if xi0 is not None:
            spot_x = float(p0["spot"][xi0])
    dte = int(path.get("dte", cfg.get("dte", 1)))
    exp = str(path.get("exp", r.get("exp", "")))
    exp_ts = int(path.get("exp_ts") or s018.expiry_unix(date.fromisoformat(exp))) if exp else 0
    legs_out: list[dict[str, Any]] = []
    fee_e_tot = 0.0
    slip_e_tot = 0.0
    fee_x_tot = 0.0
    slip_x_tot = 0.0
    qty_tot = 0
    fill_w = 0.0
    if not tranches:
        tranches = [{"path": path, "qty": 1.0, "entry": entry_ts, "add_no": 0}]
    tr_by = {int(tr.get("add_no", 0)): tr for tr in tranches}
    legs_src = list(r.get("legs") or path.get("legs") or [])
    for k, lg in enumerate(legs_src):
        add_no = int(lg.get("add_no", 0))
        tr = tr_by.get(add_no, tranches[min(add_no, len(tranches) - 1)])
        tr_path = tr["path"]
        add_entry = int(tr.get("entry", entry_ts))
        qf = float(tr.get("qty", 0.25))
        xi = d._path_i_at(tr_path, exit_ts)
        exp_tr = str(tr_path.get("exp", exp))
        exp_ts_tr = int(tr_path.get("exp_ts") or exp_ts)
        dte_tr = max(0, (date.fromisoformat(str(exp_tr)) - ist_date(exit_ts)).days) if exp_tr else dte
        qlg = int(lg.get("qty") or int(round(qf * float(qty_base))))
        em = float(lg["mark"])
        ef = float(lg["fill"])
        fee_e = float(lg.get("fee", 0.0))
        slip_e = float(lg.get("slip", 0.0))
        idx = _px_idx_for_symbol(tr_path, str(lg.get("symbol", "")), k % 3)
        if xi is None:
            xm = float("nan")
            xf = float("nan")
            fee_x = 0.0
            slip_x = 0.0
        elif reason == "EXPIRY":
            xm = intrinsic(bool(lg["is_call"]), float(lg["strike"]), spot_x)
            xf = xm
            fee_x = d.fee_gst_qty(xm, spot_x, qlg) if xm > 0 else 0.0
            slip_x = 0.0
        else:
            xm = float(tr_path[f"px{idx}"][xi])
            xf, _ = s018.buy_fill(xm, dte_tr)
            fee_x = d.fee_gst_qty(xm, spot_x if spot_x else 1.0, qlg)
            slip_x = (xf - xm) * qlg * OPTIONS_CONTRACT_VALUE
        t_e_lg = t_years(add_entry, exp_ts_tr)
        t_x_lg = t_years(exit_ts, exp_ts_tr)
        ge = greeks_from_mark(em, spot_e, float(lg["strike"]), t_e_lg, bool(lg["is_call"]))
        gx = greeks_from_mark(xm, spot_x, float(lg["strike"]), t_x_lg, bool(lg["is_call"]))
        d_ent = float(lg.get("delta", ge["delta"]))
        legs_out.append(
            {
                "symbol": str(lg.get("symbol", "")),
                "strike": float(lg["strike"]),
                "type": "call" if bool(lg["is_call"]) else "put",
                "expiry": exp_tr,
                "qty": qlg,
                "long_short": str(lg.get("long_short", "short")),
                "entry_mark": em,
                "entry_fill": ef,
                "exit_mark": xm,
                "exit_fill": xf,
                "fee_entry": fee_e,
                "fee_exit": fee_x,
                "slip_entry": slip_e,
                "slip_exit": slip_x,
                "iv_entry": ge["iv"],
                "delta_entry": d_ent,
                "gamma_entry": ge["gamma"],
                "theta_entry": ge["theta"],
                "vega_entry": ge["vega"],
                "iv_exit": gx["iv"],
                "delta_exit": gx["delta"],
                "gamma_exit": gx["gamma"],
                "theta_exit": gx["theta"],
                "vega_exit": gx["vega"],
                "add_no": add_no,
                "entry_ts": add_entry,
            }
        )
        fee_e_tot += fee_e
        slip_e_tot += slip_e
        fee_x_tot += fee_x
        slip_x_tot += slip_x
        qty_tot += qlg
        fill_w += ef * qlg
    avg_entry = (fill_w / qty_tot) if qty_tot else float("nan")
    brokerage = fee_e_tot + fee_x_tot
    slippage = slip_e_tot + slip_x_tot
    trade = {
        "trade_id": tid,
        "month": cfg.get("month"),
        "tf": cfg.get("tf"),
        "variant": cfg.get("variant"),
        "band": cfg.get("band"),
        "band_mode": cfg.get("band_mode"),
        "arm": cfg.get("arm_id"),
        "n_legs": len(legs_out),
        "dte": dte,
        "side": r["side"],
        "entry_ist": s018.ist_str(entry_ts),
        "exit_ist": s018.ist_str(exit_ts),
        "exit_reason": r.get("exit_reason", reason),
        "hold_hrs": r.get("hold_hrs"),
        "hrs_to_exp": r.get("hrs_to_exp"),
        "spot_entry": spot_e,
        "spot_exit": spot_x,
        "line_level": r.get("level"),
        "vwap_dist": r.get("vwap_dist"),
        "gross": r.get("gross"),
        "brokerage": brokerage,
        "slippage": slippage,
        "net": r.get("net"),
        "long_pnl": r.get("long_pnl", ""),
        "short_pnl": r.get("short_pnl", ""),
        "mfe": r.get("mfe"),
        "mfe_time": s018.ist_str(int(r["mfe_ts"])) if int(r.get("mfe_ts") or 0) else "",
        "mae": r.get("mae"),
        "mae_time": s018.ist_str(int(r["mae_ts"])) if int(r.get("mae_ts") or 0) else "",
        "qty": qty_tot,
        "avg_entry": avg_entry,
        "strike": "",
        "debit": "",
        "iv_call": "",
        "iv_put": "",
        "implied_1h": "",
        "realized_1h": "",
        "weekday": "",
    }
    snaps: list[dict[str, Any]] = []
    p0 = tranches[0]["path"] if tranches else path
    for ts_i, kind in d._snap_times(
        entry_ts, exit_ts, int(r.get("mfe_ts") or 0), int(r.get("mae_ts") or 0)
    ):
        if ts_i == entry_ts:
            sp = spot_e
            marks = [float(lg["mark"]) for lg in list(p0.get("legs") or [])[:3]]
            pnl = 0.0
        else:
            pi = d._path_i_at(p0, ts_i)
            if pi is None or not bool(p0["ok"][pi]):
                continue
            sp = float(p0["spot"][pi]) or spot_x
            marks = [float(p0[f"px{k}"][pi]) for k in range(min(3, len(list(p0.get("legs") or []))))]
            pnl = 0.0
            for tr in tranches:
                tpi = d._path_i_at(tr["path"], ts_i)
                if tpi is None or not bool(tr["path"]["ok"][tpi]):
                    continue
                pnl += float(tr["path"]["pnl"][tpi]) * float(tr.get("qty", 0.25))
        t_yr = t_years(ts_i, int(p0.get("exp_ts") or exp_ts))
        row: dict[str, Any] = {
            "ts_ist": s018.ist_str(ts_i),
            "kind": kind,
            "spot": sp,
            "leg1_mark": "", "leg1_iv": "",
            "leg2_mark": "", "leg2_iv": "",
            "leg3_mark": "", "leg3_iv": "",
        }
        bd = bg = bt = bv = 0.0
        nfin = 0
        for k, lg in enumerate(list(p0.get("legs") or [])[:3]):
            mk = float(marks[k]) if k < len(marks) else float("nan")
            g = greeks_from_mark(mk, sp, float(lg["strike"]), t_yr, bool(lg["is_call"]))
            row[f"leg{k+1}_mark"] = mk
            row[f"leg{k+1}_iv"] = g["iv"]
            if np.isfinite(g["delta"]):
                bd += pos * float(g["delta"])
                bg += pos * float(g["gamma"])
                bt += pos * float(g["theta"])
                bv += pos * float(g["vega"])
                nfin += 1
        row["basket_delta"] = bd if nfin else float("nan")
        row["basket_gamma"] = bg if nfin else float("nan")
        row["basket_theta"] = bt if nfin else float("nan")
        row["basket_vega"] = bv if nfin else float("nan")
        row["basket_mark_pnl"] = pnl
        row["long_delta"] = row["basket_delta"]
        row["long_gamma"] = row["basket_gamma"]
        row["long_theta"] = row["basket_theta"]
        row["long_vega"] = row["basket_vega"]
        row["long_mark_pnl"] = pnl
        snaps.append(row)
    archive.add_trade(trade, legs_out, snaps)


def rsi_grid_work() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tf in RSI_TFS:
        for tgt in A0_T:
            for slv in A0_SL:
                out.append(
                    {
                        "tf": tf, "arm_id": "A0", "legs": 3, "dte": 1,
                        "trail_arm": 0.0, "trail_give": 0.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                        "tgt": int(tgt), "sl": int(slv), "basket": "std",
                        "prem_pct": 0.0, "strangle_exit": False, "scale": False,
                        "hedge": False, "do_c2": int(tgt) == 250 and int(slv) == 250,
                        "do_c3": False,
                    }
                )
        out.append(
            {
                "tf": tf, "arm_id": "A1", "legs": 3, "dte": 1,
                "trail_arm": 100.0, "trail_give": 75.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                "tgt": d.PRIMARY_T, "sl": 250, "basket": "std",
                "prem_pct": 0.0, "strangle_exit": False, "scale": False,
                "hedge": False, "do_c2": False, "do_c3": False,
            }
        )
        out.append(
            {
                "tf": tf, "arm_id": "A2", "legs": 2, "dte": 1,
                "trail_arm": 80.0, "trail_give": 60.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                "tgt": d.PRIMARY_T, "sl": d.PRIMARY_SL, "basket": "std",
                "prem_pct": 0.0, "strangle_exit": False, "scale": False,
                "hedge": False, "do_c2": True, "do_c3": False,
            }
        )
        out.append(
            {
                "tf": tf, "arm_id": "A3", "legs": 2, "dte": 2,
                "trail_arm": 80.0, "trail_give": 60.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                "tgt": d.PRIMARY_T, "sl": d.PRIMARY_SL, "basket": "std",
                "prem_pct": 0.0, "strangle_exit": False, "scale": False,
                "hedge": False, "do_c2": False, "do_c3": False,
            }
        )
        out.append(
            {
                "tf": tf, "arm_id": "A4", "legs": 2, "dte": 1,
                "trail_arm": 80.0, "trail_give": 60.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                "tgt": d.PRIMARY_T, "sl": d.PRIMARY_SL, "basket": "a4",
                "prem_pct": 0.0, "strangle_exit": False, "scale": False,
                "hedge": False, "do_c2": False, "do_c3": False,
            }
        )
        for aid, bsk, sx in (("B1", "sg150", 150.0), ("B2", "sg300", 300.0)):
            out.append(
                {
                    "tf": tf, "arm_id": aid, "legs": 2, "dte": 1,
                    "trail_arm": 0.0, "trail_give": 0.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                    "tgt": d.PRIMARY_T, "sl": d.PRIMARY_SL, "basket": bsk,
                    "prem_pct": 0.0, "strangle_exit": True, "sx": sx, "scale": False,
                    "hedge": False, "do_c2": False, "do_c3": False,
                }
            )
        out.append(
            {
                "tf": tf, "arm_id": "B_SCALE", "legs": 3, "dte": 1,
                "trail_arm": 0.0, "trail_give": 0.0, "trail_cap": d.DEFAULT_TRAIL_CAP,
                "tgt": d.PRIMARY_T, "sl": 250, "basket": "sgscale",
                "prem_pct": 0.0, "strangle_exit": False, "scale": True,
                "hedge": False, "do_c2": False, "do_c3": False,
            }
        )
    return out


def rsi_hedge_work(tfs: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    wings: tuple[float, ...] = (WING_NONE, 500.0, 1000.0)
    for tf in tfs:
        for lk in d.HEDGE_LONG:
            nlong = 2 if lk == "L2" else 3
            variants: list[tuple[int, float, float, float]] = [(-1, 0.0, 0.0, 0.0)]
            for e in d.HEDGE_E:
                for off in d.HEDGE_OFF:
                    for wing in wings:
                        for q in d.HEDGE_Q:
                            variants.append((int(e), float(off), float(wing), float(q)))
            for e, off, wing, q in variants:
                if e < 0:
                    aid = f"{lk}_REF"
                    wlab = "REF"
                else:
                    wlab = "none" if wing < 0 else str(int(wing))
                    aid = f"{lk}_E{e}_o{int(off)}_W{wlab}_q{int(round(q * 100))}"
                out.append(
                    {
                        "tf": tf, "arm_id": aid, "legs": nlong, "dte": 2,
                        "trail_arm": d.HEDGE_TRAIL_ARM, "trail_give": d.HEDGE_TRAIL_GIVE,
                        "trail_cap": d.HEDGE_TRAIL_CAP, "tgt": d.PRIMARY_T, "sl": d.HEDGE_LONG_SL,
                        "basket": f"hdg_{aid}", "prem_pct": 0.0, "strangle_exit": False,
                        "scale": False, "hedge": True, "hedge_e": int(e), "hedge_off": float(off),
                        "hedge_w": float(wing), "hedge_q": float(q), "long_kind": lk,
                        "do_c2": False, "do_c3": False,
                    }
                )
    return out


def expiry_weekly_bimal(t: int) -> date:
    """Nearest Friday 17:30 IST at least 2 days away, else the next Friday."""
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    d0 = dt.date()
    days_to_fri = (4 - int(d0.weekday())) % 7
    fri = d0 + timedelta(days=days_to_fri)
    for _ in range(4):
        if int(s018.expiry_unix(fri)) - int(t) >= 2 * 86400:
            return fri
        fri = fri + timedelta(days=7)
    return fri


def straddle_expiry(t: int, code: str) -> date:
    c = str(code)
    if c == "E_2D":
        return d.expiry_1dte_bimal(t) + timedelta(days=1)
    if c == "E_WK":
        return expiry_weekly_bimal(t)
    return d.expiry_1dte_bimal(t)


def straddle_exit_specs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tgt in (150, 250, 350):
        for slv in (150, 250, 350):
            out.append(
                {
                    "exit_id": f"E1_T{tgt}_SL{slv}",
                    "family": "E1",
                    "tgt": int(tgt),
                    "sl": int(slv),
                    "trail_arm": 0.0,
                    "trail_give": 0.0,
                    "trail_cap": 0.0,
                    "time_stop_sec": None,
                    "ratchet": False,
                    "do_c2": int(tgt) == 250 and int(slv) == 250,
                }
            )
    out.append(
        {
            "exit_id": "E2_TRAIL",
            "family": "E2",
            "tgt": 250,
            "sl": 250,
            "trail_arm": 80.0,
            "trail_give": 60.0,
            "trail_cap": 400.0,
            "time_stop_sec": None,
            "ratchet": False,
            "do_c2": True,
        }
    )
    for tgt, slv in ((250, 250), (350, 250), (250, 150)):
        out.append(
            {
                "exit_id": f"E3_T{tgt}_SL{slv}",
                "family": "E3",
                "tgt": int(tgt),
                "sl": int(slv),
                "trail_arm": 0.0,
                "trail_give": 0.0,
                "trail_cap": 0.0,
                "time_stop_sec": None,
                "ratchet": True,
                "do_c2": int(tgt) == 250 and int(slv) == 250,
            }
        )
    for hrs in (1, 2, 4):
        out.append(
            {
                "exit_id": f"E4_H{hrs}",
                "family": "E4",
                "tgt": 250,
                "sl": 250,
                "trail_arm": 0.0,
                "trail_give": 0.0,
                "trail_cap": 0.0,
                "time_stop_sec": int(hrs) * 3600,
                "ratchet": False,
                "do_c2": int(hrs) == 2,
            }
        )
    return out


def rsi_straddle_work(tfs: list[str], exp_codes: list[str]) -> list[dict[str, Any]]:
    specs = straddle_exit_specs()
    out: list[dict[str, Any]] = []
    for tf in tfs:
        for exp_code in exp_codes:
            for sp in specs:
                dte = 1 if exp_code == "E_1D" else (2 if exp_code == "E_2D" else 7)
                out.append(
                    {
                        "tf": tf,
                        "arm_id": str(sp["exit_id"]),
                        "legs": 2,
                        "dte": dte,
                        "trail_arm": float(sp["trail_arm"]),
                        "trail_give": float(sp["trail_give"]),
                        "trail_cap": float(sp["trail_cap"]),
                        "tgt": int(sp["tgt"]),
                        "sl": int(sp["sl"]),
                        "basket": str(exp_code),
                        "prem_pct": 0.0,
                        "strangle_exit": False,
                        "scale": False,
                        "hedge": False,
                        "straddle": True,
                        "exp_code": str(exp_code),
                        "exit_id": str(sp["exit_id"]),
                        "family": str(sp["family"]),
                        "time_stop_sec": sp["time_stop_sec"],
                        "ratchet": bool(sp["ratchet"]),
                        "do_c2": bool(sp["do_c2"]),
                        "do_c3": False,
                    }
                )
    return out


def straddle_cell_key(
    month: str,
    w: dict[str, Any],
    max_days: int,
    start_ts: int,
    cutoff: int,
    exp_obh: float = RSI_EXP_OBH,
    exp_obl: float = RSI_EXP_OBL,
) -> str:
    exp = f"exp={_exp_key_part(exp_obh)}-{_exp_key_part(exp_obl)}"
    return (
        f"{month}|{w['tf']}|straddle|{w['exp_code']}|{w['exit_id']}|{exp}"
        f"|md={int(max_days)}|{int(start_ts)}|{int(cutoff)}"
    )


def pick_straddle_legs(
    store: Any, t: int, exp: date, spot: float
) -> list[s018.Leg] | None:
    packed = s018.load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _ = packed
    pairs: dict[float, dict[str, dict[str, Any]]] = {}
    for r in rows:
        k = float(r["strike"])
        slot = pairs.setdefault(k, {})
        if bool(r["is_call"]):
            slot["c"] = r
        else:
            slot["p"] = r
    best: tuple[float, float, float] | None = None
    chosen: tuple[dict[str, Any], dict[str, Any]] | None = None
    for k, slot in pairs.items():
        if "c" not in slot or "p" not in slot:
            continue
        diff = abs(float(slot["c"]["mark"]) - float(slot["p"]["mark"]))
        key = (diff, abs(k - float(spot)), k)
        if best is None or key < best:
            best = key
            chosen = (slot["c"], slot["p"])
    if chosen is None:
        return None
    dte = max(0, (exp - ist_date(t)).days)
    t_yr = t_years(t, s018.expiry_unix(exp))
    legs: list[s018.Leg] = []
    for r, role in ((chosen[0], "scall"), (chosen[1], "sput")):
        q = s018.mark_le(store, exp, str(r["symbol"]), t)
        if q is None:
            return None
        fill, _ = s018.buy_fill(q.px, dte)
        dlt = s018.signed_delta(q.px, float(spot), float(r["strike"]), t_yr, bool(r["is_call"]))
        legs.append(
            s018.Leg(
                symbol=str(r["symbol"]),
                strike=float(r["strike"]),
                is_call=bool(r["is_call"]),
                role=role,
                mark=q.px,
                fill=fill,
                src=q.src,
                delta=dlt,
                ts=q.ts,
            )
        )
    return legs


def get_or_build_straddle(
    store: Any,
    spot_c: dict[int, float],
    t: int,
    exp: date,
    tag: str,
) -> tuple[dict[str, Any] | None, bool]:
    fp = d.cache_file(t, "S", exp, d.path_tag(tag, 1, basket=f"strdl_{exp.isoformat()}"))
    path = s020.load_path(fp)
    if path is not None:
        return path, False
    sp = spot_c.get(int(t))
    if sp is None:
        return None, False
    legs = pick_straddle_legs(store, t, exp, float(sp))
    if legs is None:
        return None, False
    path = s020.build_path(store, spot_c, legs, t, exp)
    if path is None:
        return None, False
    for lg, src in zip(path["legs"], legs):
        lg["qty"] = int(STRADDLE_QTY)
        lg["long_short"] = "long"
        lg["fee"] = d.fee_gst_qty(float(src.mark), float(sp), int(STRADDLE_QTY))
        lg["slip"] = (float(src.fill) - float(src.mark)) * int(STRADDLE_QTY) * OPTIONS_CONTRACT_VALUE
        lg["expiry"] = exp.isoformat()
        lg["exp_ts"] = s018.expiry_unix(exp)
    s020.save_path(fp, path)
    return path, True


def _straddle_exit(
    path: dict[str, Any],
    i: int,
    reason: str,
    spot_c: dict[int, float],
) -> dict[str, Any]:
    t = int(path["ts"][i])
    legs = list(path["legs"])[:2]
    dte = max(0, (date.fromisoformat(str(path["exp"])) - ist_date(t)).days)
    idx = float(path["spot"][i]) or float(spot_c.get(t, 0.0))
    pxs = [float(path["px0"][i]), float(path["px1"][i])]
    qty = int(STRADDLE_QTY)
    gross = 0.0
    fees = sum(float(lg.get("fee", 0.0)) for lg in legs)
    slip = sum(float(lg.get("slip", 0.0)) for lg in legs)
    srcs = [str(lg.get("src", "")) for lg in legs]
    if str(reason) == "EXPIRY":
        for lg in legs:
            px = intrinsic(bool(lg["is_call"]), float(lg["strike"]), idx if idx else 0.0)
            gross += (px - float(lg["fill"])) * qty * OPTIONS_CONTRACT_VALUE
            if px > 0 and idx > 0:
                fees += d.fee_gst_qty(px, idx, qty)
            srcs.append("settle")
    else:
        for lg, mark in zip(legs, pxs):
            xf, _ = s018.sell_fill(mark, dte)
            gross += (xf - float(lg["fill"])) * qty * OPTIONS_CONTRACT_VALUE
            fees += d.fee_gst_qty(mark, idx if idx else 1.0, qty)
            slip += (mark - xf) * qty * OPTIONS_CONTRACT_VALUE
            srcs.append("mark")
    return {
        "exit_ts": t,
        "reason": reason,
        "gross": gross,
        "fees": fees,
        "slip": slip,
        "net": gross - fees,
        "srcs": srcs,
    }


def scan_straddle(
    path: dict[str, Any],
    w: dict[str, Any],
    spot_c: dict[int, float],
) -> dict[str, Any] | None:
    ts = path["ts"]
    ok = path["ok"]
    pnl_a = path["pnl"]
    exp_ts = int(path["exp_ts"])
    entry = int(path["entry_ts"])
    n = int(ts.size)
    tgt = float(w["tgt"])
    slv = float(w["sl"])
    trail_arm = float(w.get("trail_arm") or 0.0)
    trail_give = float(w.get("trail_give") or 0.0)
    trail_cap = float(w.get("trail_cap") or 0.0)
    trail_on = trail_arm > 0.0
    ratchet = bool(w.get("ratchet"))
    tstop = w.get("time_stop_sec")
    stop_at = int(entry) + int(tstop) if tstop else None
    hard = exp_ts - int(STRADDLE_PRE_EXP_SEC)
    armed = False
    peak = 0.0
    trace: list[tuple[int, float, float, float]] = []
    for i in range(n):
        t = int(ts[i])
        if t >= hard:
            if bool(ok[i]) and np.isfinite(pnl_a[i]):
                out = _straddle_exit(path, i, "PRE_EXPIRY", spot_c)
            else:
                continue
            if ratchet:
                out["ratchet_trace"] = trace[-5:] if len(trace) > 5 else trace
            return out
        if not bool(ok[i]) or not np.isfinite(pnl_a[i]):
            if t == exp_ts:
                return _straddle_exit(path, i, "EXPIRY", spot_c)
            continue
        gp = float(pnl_a[i])
        if ratchet:
            peak = max(peak, gp)
            stop = max(-slv, peak - slv)
            trace.append((t, gp, peak, stop))
            if gp <= stop:
                out = _straddle_exit(path, i, "SL", spot_c)
                out["ratchet_trace"] = trace[:: max(1, len(trace) // 4)][:4] + trace[-1:]
                return out
            if gp >= tgt:
                out = _straddle_exit(path, i, "TARGET", spot_c)
                out["ratchet_trace"] = trace[:: max(1, len(trace) // 4)][:4] + trace[-1:]
                return out
        else:
            hit_tgt = trail_cap if trail_on and trail_cap > 0.0 else tgt
            if gp <= -slv:
                return _straddle_exit(path, i, "SL", spot_c)
            if gp >= hit_tgt:
                return _straddle_exit(path, i, "TARGET", spot_c)
            if trail_on:
                if gp >= trail_arm:
                    armed = True
                if armed:
                    peak = max(peak, gp)
                    if gp <= peak - trail_give:
                        return _straddle_exit(path, i, "TRAIL", spot_c)
            if stop_at is not None and t >= stop_at:
                return _straddle_exit(path, i, "TIME", spot_c)
        if t == exp_ts:
            return _straddle_exit(path, i, "EXPIRY", spot_c)
    return None


def weekday_ist(t: int) -> str:
    dt = datetime.fromtimestamp(int(t), tz=s018.UTC).astimezone(s018.IST)
    return "weekday" if int(dt.weekday()) < 5 else "weekend"


def implied_1h_move(debit_pts: float, tte_sec: float) -> float:
    hrs = float(tte_sec) / 3600.0
    if hrs <= 1e-9 or not np.isfinite(debit_pts):
        return float("nan")
    return float(debit_pts) * float(np.sqrt(1.0 / hrs))


def weekly_chain_coverage(
    store: Any, plan: list[tuple[int, str]]
) -> tuple[int, int]:
    ok_n = 0
    need = 0
    seen: set[tuple[int, str]] = set()
    for t, _side in plan:
        exp = expiry_weekly_bimal(int(t))
        key = (int(t), exp.isoformat())
        if key in seen:
            continue
        seen.add(key)
        need += 1
        if s018.load_chain(store, exp, int(t)) is not None:
            ok_n += 1
    return ok_n, need


def simulate_straddle(
    store: Any,
    spot_c: dict[int, float],
    plan: list[tuple[int, str]],
    w: dict[str, Any],
    tag: str,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    extra_by: dict[tuple[int, str], dict[str, Any]] | None,
    label: str = "",
    on_trade: Any | None = None,
    smoke: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    global _RATCHET_TRACE_PRINTED
    busy = -1
    rows: list[dict[str, Any]] = []
    n_missing = 0
    built = 0
    t0 = time.perf_counter()
    st_prog: dict[str, int] = {"mark": -1}
    nplan = len(plan)
    exp_code = str(w["exp_code"])
    for j, (t, side) in enumerate(plan, start=1):
        if label:
            s020.progress_every_2pct(label, j, nplan, built, t0, st_prog)
        if t < start_ts or t >= cutoff or t <= busy:
            continue
        if not s020.in_window(t, win_from, win_to):
            continue
        extra: dict[str, Any] = {}
        if extra_by is not None:
            extra = dict(extra_by.get((int(t), str(side)), {}))
        if extra:
            if not d.keep_signal(extra, 0, "near"):
                continue
        elif not d.entry_allowed(t):
            continue
        exp = straddle_expiry(t, exp_code)
        path, newp = get_or_build_straddle(store, spot_c, t, exp, tag)
        if newp:
            built += 1
        if path is None:
            n_missing += 1
            continue
        walked = scan_straddle(path, w, spot_c)
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        hrs_exp = (s018.expiry_unix(exp) - int(t)) / 3600.0
        mf, ma, mfe_ts, mae_ts = d.mfe_mae_with_ts(path, 2, int(walked["exit_ts"]))
        legs = list(path["legs"])[:2]
        spot_e = float(spot_c.get(t, 0.0))
        t_yr = t_years(t, int(path["exp_ts"]))
        ivs: list[float] = []
        iv_c = float("nan")
        iv_p = float("nan")
        for lg in legs:
            g = greeks_from_mark(
                float(lg["mark"]), spot_e, float(lg["strike"]), t_yr, bool(lg["is_call"])
            )
            ivs.append(float(g["iv"]))
            if bool(lg["is_call"]):
                iv_c = float(g["iv"])
            else:
                iv_p = float(g["iv"])
        debit_pts = float(sum(float(lg["mark"]) for lg in legs))
        debit_usd = float(sum(float(lg["fill"]) for lg in legs)) * int(STRADDLE_QTY) * OPTIONS_CONTRACT_VALUE
        strike = float(legs[0]["strike"]) if legs else float("nan")
        impl = implied_1h_move(debit_pts, float(path["exp_ts"]) - float(t))
        sp1 = spot_c.get(int(t) + 3600)
        realized = abs(float(sp1) - spot_e) if sp1 is not None and spot_e > 0 else float("nan")
        eiv = float(np.mean([x for x in ivs if np.isfinite(x)])) if any(np.isfinite(x) for x in ivs) else float("nan")
        nd = 0.0
        nfin = 0
        for lg in legs:
            dv = float(lg.get("delta", float("nan")))
            if np.isfinite(dv):
                nd += dv
                nfin += 1
        rec = {
            "entry_ts": t,
            "side": side,
            "hod": s018.hod_ist(t),
            "hold_hrs": hold,
            "hrs_to_exp": hrs_exp,
            "legs": legs,
            "exp": exp.isoformat(),
            "mfe": mf,
            "mfe_ts": mfe_ts,
            "mae": ma,
            "mae_ts": mae_ts,
            "exit_reason": walked.get("reason"),
            "spot": spot_e,
            "spot_exit": float(spot_c.get(int(walked["exit_ts"]), 0.0)),
            "entry_iv": eiv,
            "iv_call": iv_c,
            "iv_put": iv_p,
            "net_delta": nd if nfin else float("nan"),
            "strike": strike,
            "debit": debit_usd,
            "debit_pts": debit_pts,
            "implied_1h": impl,
            "realized_1h": realized,
            "weekday": weekday_ist(t),
            "call_mark": float(next((lg["mark"] for lg in legs if lg["is_call"]), float("nan"))),
            "put_mark": float(next((lg["mark"] for lg in legs if not lg["is_call"]), float("nan"))),
            **extra,
            **walked,
        }
        rows.append(rec)
        if smoke and len(_STRADDLE_SAMPLES) < 3:
            _STRADDLE_SAMPLES.append(rec)
        if smoke and bool(w.get("ratchet")) and not _RATCHET_TRACE_PRINTED:
            _RATCHET_TRACE_PRINTED = True
            pts = list(walked.get("ratchet_trace") or [])
            print(
                f"RATCHET check entry={s018.ist_str(int(t))} exit={s018.ist_str(int(walked['exit_ts']))} "
                f"reason={walked.get('reason')} net={walked.get('net')}",
                flush=True,
            )
            for ts_i, gp, pk, st in pts[:5]:
                print(
                    f"  {s018.ist_str(int(ts_i))} gp={gp:.2f} peak={pk:.2f} stop={st:.2f}",
                    flush=True,
                )
        if on_trade is not None:
            on_trade(rec, path)
        busy = int(walked["exit_ts"])
    return rows, n_missing


def emit_archive_trade_straddle(
    archive: RsiArchive,
    cfg: dict[str, Any],
    r: dict[str, Any],
    path: dict[str, Any],
) -> None:
    orig = archive.add_trade

    def wrapped(
        trade_dict: dict[str, Any],
        legs: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
    ) -> None:
        trade_dict["qty"] = int(STRADDLE_QTY) * 2
        trade_dict["avg_entry"] = float(r.get("debit_pts", float("nan"))) / 2.0
        trade_dict["strike"] = r.get("strike")
        trade_dict["debit"] = r.get("debit")
        trade_dict["iv_call"] = r.get("iv_call")
        trade_dict["iv_put"] = r.get("iv_put")
        trade_dict["implied_1h"] = r.get("implied_1h")
        trade_dict["realized_1h"] = r.get("realized_1h")
        trade_dict["weekday"] = r.get("weekday")
        orig(trade_dict, legs, snapshots)

    archive.add_trade = wrapped  # type: ignore[method-assign]
    try:
        d.emit_archive_trade(archive, cfg, r, path, 2, "long")
    finally:
        archive.add_trade = orig  # type: ignore[method-assign]


def sigs_to_entry(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in raw:
        e = dict(s)
        e["signal_ts"] = int(s["ts"])
        e["ts"] = int(s["ts"]) + 60
        e["level"] = float(s.get("ob_level", 0.0))
        e["vwap_dist"] = 0.0
        out.append(e)
    return out


def pick_short_nowing(
    store: Any, t: int, spot: float, e: int, off: float
) -> tuple[list[tuple[dict[str, Any], str]], date] | None:
    exp = d.expiry_0dte_bimal(t) if int(e) == 0 else d.expiry_1dte_bimal(t)
    packed = s018.load_chain(store, exp, t)
    if packed is None:
        return None
    rows, _ = packed
    ks = d._strikes_sorted(rows)
    atm = d.atm_strike_of(rows, spot)
    if atm is None:
        return None
    sc = d.nearest_listed(ks, float(atm) + float(off))
    spu = d.nearest_listed(ks, float(atm) - float(off))
    if sc is None or spu is None:
        return None
    chosen: list[tuple[dict[str, Any], str]] = []
    for is_call, k, role in ((True, float(sc), "scall"), (False, float(spu), "sput")):
        r = d.row_strike(rows, is_call, k)
        if r is None:
            return None
        chosen.append((r, role))
    return chosen, exp


def get_or_build_hedge_rsi(
    store: Any,
    spot_c: dict[int, float],
    t: int,
    side: str,
    tag: str,
    long_kind: str,
    hedge_e: int,
    hedge_off: float,
    hedge_w: float,
    hedge_q: float,
) -> tuple[dict[str, Any] | None, bool]:
    if float(hedge_w) >= 0:
        return d.get_or_build_hedge_path(
            store, spot_c, t, side, tag, long_kind, hedge_e, hedge_off, hedge_w, hedge_q
        )
    nlong = 2 if str(long_kind) == "L2" else 3
    bsk = f"hdg_{long_kind}_REF" if float(hedge_q) <= 0 else (
        f"hdg_{long_kind}_E{int(hedge_e)}_o{int(hedge_off)}_Wnone_q{int(round(float(hedge_q) * 100))}"
    )
    long_exp = d.expiry_2dte_bimal(t)
    fp = d.cache_file(t, side, long_exp, d.path_tag(tag, 2, basket=bsk))
    path = d.load_path_hedge(fp)
    if path is not None:
        return path, False
    sp = spot_c.get(t)
    if sp is None:
        return None, False
    long_obj = d.pick_basket_dev(store, side, t, float(sp), False, dte_mode=2, basket="std")
    if long_obj is None:
        return None, False
    qty_l = int(s018.QTY)
    legs: list[dict[str, Any]] = []
    for lg in long_obj[:nlong]:
        fee = d.fee_gst_qty(float(lg.mark), float(sp), qty_l)
        slip = (float(lg.fill) - float(lg.mark)) * qty_l * OPTIONS_CONTRACT_VALUE
        legs.append(
            {
                "symbol": lg.symbol, "strike": lg.strike, "is_call": lg.is_call,
                "role": lg.role, "mark": lg.mark, "fill": lg.fill, "src": lg.src,
                "delta": lg.delta, "ts": lg.ts, "fee": fee, "slip": slip, "qty": qty_l,
                "long_short": "long", "expiry": long_exp.isoformat(),
                "exp_ts": s018.expiry_unix(long_exp),
            }
        )
    if float(hedge_q) > 0:
        packed = pick_short_nowing(store, t, float(sp), int(hedge_e), float(hedge_off))
        if packed is None:
            return None, False
        chosen, sexp = packed
        qty_s = int(round(float(hedge_q) * float(s018.QTY)))
        dte_s = max(0, (sexp - ist_date(t)).days)
        t_yr_s = t_years(t, s018.expiry_unix(sexp))
        for r, role in chosen:
            q = s018.mark_le(store, sexp, str(r["symbol"]), t)
            if q is None:
                return None, False
            fill, _ = s018.sell_fill(q.px, dte_s)
            dlt = s018.signed_delta(q.px, float(sp), float(r["strike"]), t_yr_s, bool(r["is_call"]))
            fee = d.fee_gst_qty(float(q.px), float(sp), qty_s)
            slip = (float(fill) - float(q.px)) * qty_s * OPTIONS_CONTRACT_VALUE
            legs.append(d._leg_dict(r, q, fill, dlt, role, "short", qty_s, sexp, fee, slip))
    path = d.build_path_hedge(store, spot_c, legs, t, long_exp, nlong)
    if path is None:
        return None, False
    d.save_path_hedge(fp, path)
    return path, True


def simulate_hedge_rsi(
    store: Any,
    spot_c: dict[int, float],
    plan: list[tuple[int, str]],
    tag: str,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    extra_by: dict[tuple[int, str], dict[str, Any]] | None,
    long_kind: str,
    hedge_e: int,
    hedge_off: float,
    hedge_w: float,
    hedge_q: float,
    label: str = "",
    on_trade: Any | None = None,
    do_sanity: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    busy = -1
    rows: list[dict[str, Any]] = []
    n_stale = 0
    built = 0
    t0 = time.perf_counter()
    st_prog: dict[str, int] = {"mark": -1}
    nplan = len(plan)
    nlong = 2 if str(long_kind) == "L2" else 3
    for j, (t, side) in enumerate(plan, start=1):
        if label:
            s020.progress_every_2pct(label, j, nplan, built, t0, st_prog)
        if t < start_ts or t >= cutoff or t <= busy:
            continue
        if not s020.in_window(t, win_from, win_to):
            continue
        extra: dict[str, Any] = {}
        if extra_by is not None:
            extra = dict(extra_by.get((int(t), str(side)), {}))
        if extra:
            if not d.keep_signal(extra, 0, "near"):
                continue
        elif not d.entry_allowed(t):
            continue
        path, newp = get_or_build_hedge_rsi(
            store, spot_c, t, side, tag, long_kind, hedge_e, hedge_off, hedge_w, hedge_q
        )
        if newp:
            built += 1
        if path is None:
            n_stale += 1
            continue
        walked = d.scan_hedge_path(path, spot_c)
        if walked is None:
            continue
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        exp = date.fromisoformat(str(path["exp"]))
        hrs_exp = (s018.expiry_unix(exp) - int(t)) / 3600.0
        mf, ma, mfe_ts, mae_ts = d.mfe_mae_with_ts(path, nlong, int(walked["exit_ts"]))
        eiv = d.entry_iv_avg(path, float(spot_c.get(t, 0.0)), t)
        if do_sanity and float(hedge_w) > 0:
            d.hedge_sanity_check(path, walked, t, side)
        sel = list(path["legs"])
        nd = 0.0
        nfin = 0
        for lg in sel:
            dv = float(lg.get("delta", float("nan")))
            pos = 1.0 if str(lg.get("long_short")) == "long" else -1.0
            if np.isfinite(dv):
                nd += pos * dv
                nfin += 1
        rows.append(
            {
                "entry_ts": t, "side": side, "hod": s018.hod_ist(t),
                "hold_hrs": hold, "hrs_to_exp": hrs_exp, "legs": sel,
                "exp": exp.isoformat(), "mfe": mf, "mfe_ts": mfe_ts, "mae": ma,
                "mae_ts": mae_ts, "exit_reason": walked.get("reason"),
                "spot": float(spot_c.get(t, 0.0)), "entry_iv": eiv,
                "net_delta": nd if nfin else float("nan"),
                "long_pnl": walked.get("long_pnl"), "short_pnl": walked.get("short_pnl"),
                **extra, **walked,
            }
        )
        if on_trade is not None:
            on_trade(rows[-1], path)
        busy = int(walked["exit_ts"])
    return rows, n_stale


def simulate_scale(
    store: Any,
    spot_c: dict[int, float],
    plan: list[tuple[int, str]],
    tag: str,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    extra_by: dict[tuple[int, str], dict[str, Any]] | None,
    label: str = "",
    on_trade: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    global _SCALE_SAMPLE_PRINTED
    busy = -1
    rows: list[dict[str, Any]] = []
    n_stale = 0
    built = 0
    t0 = time.perf_counter()
    st_prog: dict[str, int] = {"mark": -1}
    nplan = len(plan)
    step = 0.25
    max_q = 1.0
    sl_pos = 250.0
    for j, (t, side) in enumerate(plan, start=1):
        if label:
            s020.progress_every_2pct(label, j, nplan, built, t0, st_prog)
        if t < start_ts or t >= cutoff or t <= busy:
            continue
        if not s020.in_window(t, win_from, win_to):
            continue
        extra: dict[str, Any] = {}
        if extra_by is not None:
            extra = dict(extra_by.get((int(t), str(side)), {}))
        if extra:
            if not d.keep_signal(extra, 0, "near"):
                continue
        elif not d.entry_allowed(t):
            continue
        p0, newp = d.get_or_build_path(store, spot_c, t, side, tag, False, dte_mode=1, basket="std")
        if newp:
            built += 1
        if p0 is None:
            n_stale += 1
            continue
        tranches: list[dict[str, Any]] = [{"path": p0, "qty": step, "entry": t, "add_no": 0}]
        qty = step
        next_add = -50.0
        exp_ts = int(p0["exp_ts"])
        walked: dict[str, Any] | None = None
        cur = int(t)
        while cur <= exp_ts:
            pos = 0.0
            ok_all = True
            for tr in tranches:
                pi = d._path_i_at(tr["path"], cur)
                if pi is None or not bool(tr["path"]["ok"][pi]):
                    ok_all = False
                    break
                pos += float(tr["path"]["pnl"][pi]) * float(tr["qty"])
            if not ok_all:
                cur += 60
                continue
            tgt = 250.0 * qty
            if pos >= tgt:
                walked = {"exit_ts": cur, "reason": "TARGET", "gross": pos, "fees": 0.0, "slip": 0.0, "net": pos}
                break
            if pos <= -sl_pos:
                walked = {"exit_ts": cur, "reason": "STOPLOSS", "gross": pos, "fees": 0.0, "slip": 0.0, "net": pos}
                break
            if qty + 1e-9 < max_q and pos <= next_add:
                p_add, newa = d.get_or_build_path(
                    store, spot_c, cur, side, tag, False, dte_mode=1, basket="std"
                )
                if newa:
                    built += 1
                if p_add is not None:
                    tranches.append(
                        {"path": p_add, "qty": step, "entry": cur, "add_no": len(tranches)}
                    )
                    qty += step
                    next_add -= 50.0
            if cur == exp_ts:
                walked = {"exit_ts": cur, "reason": "EXPIRY", "gross": pos, "fees": 0.0, "slip": 0.0, "net": pos}
                break
            cur += 60
        if walked is None:
            continue
        fees = 0.0
        slip = 0.0
        gross = 0.0
        all_legs: list[dict[str, Any]] = []
        for tr in tranches:
            qf = float(tr["qty"])
            xi = d._path_i_at(tr["path"], int(walked["exit_ts"]))
            if xi is None:
                continue
            g1 = float(tr["path"]["pnl"][xi]) * qf
            gross += g1
            for lg in list(tr["path"]["legs"])[:3]:
                lg2 = dict(lg)
                lg2["qty"] = int(round(qf * float(s018.QTY)))
                lg2["fee"] = float(lg.get("fee", 0.0)) * qf
                lg2["slip"] = float(lg.get("slip", 0.0)) * qf
                lg2["add_no"] = int(tr["add_no"])
                all_legs.append(lg2)
                fees += float(lg2["fee"])
                slip += float(lg2["slip"])
        walked["gross"] = gross
        walked["fees"] = fees
        walked["slip"] = slip
        walked["net"] = gross - fees
        hold = (int(walked["exit_ts"]) - int(t)) / 3600.0
        rec = {
            "entry_ts": t, "side": side, "hod": s018.hod_ist(t),
            "hold_hrs": hold, "hrs_to_exp": (exp_ts - int(t)) / 3600.0,
            "legs": all_legs, "exp": str(p0["exp"]),
            "mfe": float("nan"), "mfe_ts": 0, "mae": float("nan"), "mae_ts": 0,
            "exit_reason": walked.get("reason"), "spot": float(spot_c.get(t, 0.0)),
            "entry_iv": d.entry_iv_avg(p0, float(spot_c.get(t, 0.0)), t),
            "net_delta": float("nan"), **extra, **walked,
        }
        rows.append(rec)
        if not _SCALE_SAMPLE_PRINTED:
            _SCALE_SAMPLE_PRINTED = True
            print(
                f"SCALE sample trade entry={s018.ist_str(int(t))} side={side} "
                f"n_adds={len(tranches)} n_leg_rows={len(all_legs)} "
                f"qty={qty} reason={walked.get('reason')}",
                flush=True,
            )
            for lg in all_legs:
                print(
                    f"  add_no={lg.get('add_no')} qty={lg.get('qty')} "
                    f"sym={lg.get('symbol')} fill={lg.get('fill')} "
                    f"fee={lg.get('fee')} slip={lg.get('slip')}",
                    flush=True,
                )
        if on_trade is not None:
            p_emit = dict(p0)
            p_emit["legs"] = all_legs
            p_emit["scale_tranches"] = tranches
            on_trade(rec, p_emit)
        busy = int(walked["exit_ts"])
    return rows, n_stale


def run_one(
    store: Any,
    spot_c: dict[int, float],
    ts: np.ndarray,
    sigs: list[dict[str, Any]],
    w: dict[str, Any],
    month: str,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    n_days: float,
    n_raw: int,
    done: dict[str, dict[str, Any]],
    cell_rows: dict[str, list[dict[str, Any]]],
    t_all: float,
    arm_i: int,
    arm_n: int,
    max_days: int,
    archive: RsiArchive | None,
    vwap_1m: np.ndarray,
    c: np.ndarray,
    exp_obh: float = RSI_EXP_OBH,
    exp_obl: float = RSI_EXP_OBL,
) -> None:
    tf = str(w["tf"])
    variant = "RSI"
    band, band_mode = 0, "near"
    nlegs = int(w["legs"])
    dte_mode = int(w["dte"])
    trail_arm = float(w["trail_arm"])
    trail_give = float(w["trail_give"])
    trail_cap = float(w["trail_cap"])
    arm_id = str(w["arm_id"])
    basket = str(w["basket"])
    tgt = int(w["tgt"])
    slv = int(w["sl"])
    strangle_exit = bool(w.get("strangle_exit", False))
    hedge = bool(w.get("hedge", False))
    extra_by = {(int(s["ts"]), str(s["side"])): s for s in sigs}
    plan_all = [(int(s["ts"]), str(s["side"])) for s in sigs if d.keep_signal(s, 0, "near")]
    key = rsi_cell_key(month, w, max_days, start_ts, cutoff, exp_obh, exp_obl)
    if key in done:
        print(f"done SKIP {key}", flush=True)
        return
    print(
        f"[{arm_i}/{arm_n}] {SIGNAL_NAME} {key} RSS={s020.rss_mb() or 0:.0f}MB "
        f"elapsed={time.perf_counter() - t_all:.0f}s",
        flush=True,
    )
    tag = rsi_tag(tf)
    cfg_now = {
        "month": month, "tf": tf, "variant": variant, "key": key,
        "band": band, "band_mode": band_mode, "arm_id": arm_id, "dte": dte_mode,
    }
    straddle = bool(w.get("straddle"))
    ls = "long" if (strangle_exit or hedge or straddle) else "short"
    on_tr = None
    if archive is not None:
        if bool(w.get("scale")):
            on_tr = lambda r, p, _c=cfg_now: emit_archive_trade_scale(
                archive, _c, r, p
            )
        elif straddle:
            on_tr = lambda r, p, _c=cfg_now: emit_archive_trade_straddle(
                archive, _c, r, p
            )
        else:
            on_tr = lambda r, p, _c=cfg_now, _n=nlegs, _ls=ls: d.emit_archive_trade(
                archive, _c, r, p, _n, _ls
            )
    if bool(w.get("scale")):
        rows, n_stale = simulate_scale(
            store, spot_c, plan_all, tag, start_ts, cutoff, win_from, win_to,
            extra_by, label=f"{tf} SCALE", on_trade=on_tr,
        )
    elif straddle:
        rows, n_stale = simulate_straddle(
            store, spot_c, plan_all, w, tag, start_ts, cutoff, win_from, win_to,
            extra_by, label=f"{tf} {arm_id}", on_trade=on_tr,
            smoke=bool(max_days),
        )
    elif hedge:
        rows, n_stale = simulate_hedge_rsi(
            store, spot_c, plan_all, tag, start_ts, cutoff, win_from, win_to,
            extra_by, str(w.get("long_kind", "L2")), int(w.get("hedge_e", -1)),
            float(w.get("hedge_off", 0.0)), float(w.get("hedge_w", 0.0)),
            float(w.get("hedge_q", 0.0)), label=f"{tf} {arm_id}", on_trade=on_tr,
            do_sanity=float(w.get("hedge_w", 0.0)) > 0,
        )
    else:
        rows, n_stale = d.simulate_plan(
            store, spot_c, plan_all, float(tgt), float(slv), tag, False,
            start_ts, cutoff, win_from, win_to,
            d.TIME_STOP_SEC if False else None,
            label=f"{tf} {arm_id}", extra_by=extra_by, band=0, band_mode="near",
            nlegs=nlegs, dte_mode=dte_mode, trail_arm=trail_arm, trail_give=trail_give,
            trail_cap=trail_cap, basket=basket, prem_pct=0.0, strangle_exit=strangle_exit,
            on_trade=on_tr,
        )
    stt = d.stats_dev(rows)
    stt.update(
        {
            "key": key, "month": month, "tf": tf, "variant": variant, "tgt": tgt, "sl": slv,
            "max_days": int(max_days), "n_stale": n_stale, "n_sig": len(plan_all),
            "n_sig_raw": int(n_raw), "n_days": n_days, "band": 0, "band_mode": "near",
            "legs": nlegs, "dte": dte_mode, "trail_arm": trail_arm, "trail_give": trail_give,
            "trail_cap": trail_cap, "arm_id": arm_id, "basket": basket,
            "signal": SIGNAL_NAME, "code_ver": CODE_VER,
            "win_start": int(start_ts), "win_cutoff": int(cutoff),
            "exp_obh": float(exp_obh), "exp_obl": float(exp_obl),
            "n_missing": n_stale if straddle else 0,
        }
    )
    if hedge and float(w.get("hedge_w", 0.0)) < 0:
        nets = [float(r.get("net", float("nan"))) for r in rows]
        nets = [x for x in nets if np.isfinite(x)]
        shorts = [float(r.get("short_pnl", float("nan"))) for r in rows]
        shorts = [x for x in shorts if np.isfinite(x)]
        stt["worst"] = float(min(nets)) if nets else float("nan")
        stt["short_worst"] = float(min(shorts)) if shorts else float("nan")
    c2m = float("nan")
    if bool(w.get("do_c2")) and not (straddle and int(max_days) > 0):
        hour_idx = d.hour_idx_band(
            ts, c, vwap_1m, start_ts, cutoff, win_from, win_to, 0, "near"
        )
        c2s: list[float] = []
        c2_pool: list[dict[str, Any]] = []
        seeds = d.RANDOM_SEEDS_20
        for si, seed in enumerate(seeds, start=1):
            print(f"C2 {key} seed {si}/{len(seeds)}", flush=True)
            forced = s020.random_c2(rows, ts, hour_idx, seed)
            if straddle:
                rr, _ = simulate_straddle(
                    store, spot_c, forced, w, f"C2{tag}", start_ts, cutoff,
                    win_from, win_to, None, label=f"C2 {key} seed={si}",
                    on_trade=None, smoke=False,
                )
            else:
                rr, _ = d.simulate_plan(
                    store, spot_c, forced, float(tgt), float(slv), f"C2{tag}", False,
                    start_ts, cutoff, win_from, win_to, None,
                    label=f"C2 {key} seed={si}",
                    nlegs=nlegs, dte_mode=dte_mode, trail_arm=trail_arm,
                    trail_give=trail_give, trail_cap=trail_cap, basket=basket,
                    prem_pct=0.0, strangle_exit=strangle_exit,
                )
            c2_pool.extend(rr)
            if rr:
                c2s.append(float(np.mean([x["net"] for x in rr])))
        c2m = float(np.mean(c2s)) if c2s else float("nan")
        stt["c2"] = c2m
        stt["c2_wd"] = s020.stats_ww(s020.split_wd_we(c2_pool)[0])
        stt["c2_we"] = s020.stats_ww(s020.split_wd_we(c2_pool)[1])
    rec = {k: v for k, v in stt.items() if k != "exits"}
    rec["exits"] = stt.get("exits", {})
    if archive is not None:
        archive.add_config_result(
            {
                "month": month, "tf": tf, "variant": variant, "band": 0, "band_mode": "near",
                "arm": arm_id, "n_legs": nlegs, "dte": dte_mode, "tgt": tgt, "sl": slv,
                "n": stt.get("n", 0), "mean": stt.get("mean"), "gross": stt.get("gross"),
                "brokerage": stt.get("fee"), "slippage": stt.get("slip"), "win": stt.get("win"),
                "C2": "-" if not np.isfinite(c2m) else c2m, "C3": "-",
                "n_sig": stt.get("n_sig"), "n_stale": n_stale,
                "avg_net_delta": stt.get("avg_net_delta"), "key": key,
            }
        )
    s020.append_ckpt(CKPT, rec)
    done[key] = stt
    cell_rows[key] = rows
    s018._CHAIN.clear()
    gc.collect()


def collect_rsi_tf(
    ts: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    vol: np.ndarray,
    tf: str,
    start_ts: int,
    cutoff: int,
    win_from: date,
    win_to: date,
    exp_obh: float = RSI_EXP_OBH,
    exp_obl: float = RSI_EXP_OBL,
) -> tuple[list[dict[str, Any]], int, int, int]:
    tf_sec = int(d.LINE_TF[tf])
    tts, to_, th, tl, tc, tv = d.resample_tf(ts, o, h, l, c, vol, tf_sec)
    raw, _zones = detect_rsi_div(
        tts, to_, th, tl, tc, tf_sec, exp_obh=float(exp_obh), exp_obl=float(exp_obl)
    )
    n_raw = len(raw)
    entered = sigs_to_entry(raw)
    win_sigs = [
        s
        for s in entered
        if start_ts <= int(s["ts"]) < cutoff and s020.in_window(int(s["ts"]), win_from, win_to)
    ]
    kept = [s for s in win_sigs if d.entry_allowed(int(s["ts"]))]
    n_long = sum(1 for s in kept if s["side"] == "long")
    n_short = sum(1 for s in kept if s["side"] == "short")
    return kept, n_raw, n_long, n_short


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=s018.SPOT_CSV)
    ap.add_argument("--month", default="2025-06")
    ap.add_argument("--rsi-grid", action="store_true")
    ap.add_argument("--rsi-hedge-grid", action="store_true")
    ap.add_argument("--rsi-straddle-grid", action="store_true")
    ap.add_argument("--straddle-expiries", default="E_1D,E_2D,E_WK")
    ap.add_argument("--tfs", default="1m,15m")
    ap.add_argument("--max-days", type=int, default=0)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--prereg-note", default="")
    ap.add_argument("--cache-gb", type=float, default=1.0)
    ap.add_argument("--exp-obh", type=float, default=RSI_EXP_OBH)
    ap.add_argument("--exp-obl", type=float, default=RSI_EXP_OBL)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("Disable PC sleep")
    print(f"signal={SIGNAL_NAME} (old=VWAP Signal)", flush=True)
    print(f"exp_obh={float(args.exp_obh)} exp_obl={float(args.exp_obl)} code_ver={CODE_VER}", flush=True)
    load_slip_table()
    reset_mark_cache(max_bytes=int(float(args.cache_gb) * 1024**3))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh and CKPT.exists():
        CKPT.unlink()
        print(f"fresh: dropped {CKPT.name} (DEV/TRAIN ckpt untouched)", flush=True)

    month = str(args.month)
    win_from, win_to = d.month_bounds(month)
    max_days = int(args.max_days or 0)
    start_ts = to_unix(ist_dt(win_from, 0, 0))
    cutoff = to_unix(ist_dt(win_to + timedelta(days=1), 0, 0))
    if max_days:
        cutoff = start_ts + max_days * 86400
        win_to = min(win_to, ist_date(cutoff - 1))
    if not args.fresh:
        preexisting = s020.load_ckpt(CKPT)
        if preexisting and not ckpt_window_ok(
            preexisting, month, max_days, start_ts, cutoff
        ):
            print("checkpoint window mismatch -> use --fresh", flush=True)
            sys.exit(1)

    hedge_grid = bool(args.rsi_hedge_grid)
    straddle_grid = bool(args.rsi_straddle_grid)
    tfs = [x.strip() for x in str(args.tfs).split(",") if x.strip()]
    if straddle_grid:
        if tfs == ["1m", "15m"]:
            tfs = list(STRADDLE_TFS)
        exp_codes = [
            x.strip()
            for x in str(args.straddle_expiries).split(",")
            if x.strip() in STRADDLE_EXP_CODES
        ]
        if not exp_codes:
            print("need --straddle-expiries in E_1D,E_2D,E_WK", flush=True)
            sys.exit(2)
        work = rsi_straddle_work(tfs, exp_codes)
        mode = "rsi-straddle-grid"
        print(f"straddle expiries={exp_codes} tfs={tfs} cells={len(work)}", flush=True)
    elif hedge_grid:
        work = rsi_hedge_work(tfs)
        mode = "rsi-hedge-grid"
    elif bool(args.rsi_grid):
        work = rsi_grid_work()
        mode = "rsi-grid"
    else:
        print("need --rsi-grid or --rsi-hedge-grid or --rsi-straddle-grid", flush=True)
        sys.exit(2)
    print(f"{mode} combos={len(work)}", flush=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spot = s018.load_spot_1m(args.csv)
    ts, o, h, l, c, vol = s020.bars_1m_vol(spot)
    pad_d = 14 if straddle_grid else 5
    lo = start_ts - 3 * 86400
    hi = cutoff + pad_d * 86400
    sel = (ts >= lo) & (ts < hi)
    ts, o, h, l, c, vol = ts[sel], o[sel], h[sel], l[sel], c[sel], vol[sel]
    if max_days:
        print(f"SMOKE max-days={max_days} month={month} cutoff_ts={cutoff}", flush=True)
    spot_c = {int(t): float(x) for t, x in zip(ts, c)}
    vwap_1m = s020.session_vwap(ts, h, l, c, vol)
    n_days = float(max_days) if max_days else float((win_to - win_from).days + 1)
    inner = MarksStore()
    store = d.MonthGuardStore(inner, win_from - timedelta(days=1), win_to + timedelta(days=pad_d))
    done: dict[str, dict[str, Any]] = {} if args.fresh else s020.load_ckpt(CKPT)
    cell_rows: dict[str, list[dict[str, Any]]] = {}
    d.print_expiry_check()
    archive = RsiArchive(
        "s020",
        mode,
        args,
        month=month,
        prereg_note=(
            f"signal={SIGNAL_NAME}; exp_obh={float(args.exp_obh)} "
            f"exp_obl={float(args.exp_obl)}; {args.prereg_note}"
        ).strip(),
    )
    ARCHIVE_PTR.write_text(str(archive.folder), encoding="utf-8")
    print(f"archive folder={archive.folder}", flush=True)

    need_tfs = sorted({str(w["tf"]) for w in work})
    sig_cache: dict[str, tuple[list[dict[str, Any]], int, int, int]] = {}
    for tf in need_tfs:
        kept, n_raw, n_long, n_short = collect_rsi_tf(
            ts, o, h, l, c, vol, tf, start_ts, cutoff, win_from, win_to,
            exp_obh=float(args.exp_obh), exp_obl=float(args.exp_obl),
        )
        sig_cache[tf] = (kept, n_raw, n_long, n_short)
        print(
            f"TF {tf} {SIGNAL_NAME} raw={n_raw} after_filters={len(kept)} "
            f"long={n_long} short={n_short}",
            flush=True,
        )
        if max_days:
            print(f"  first 5 signals {tf}:", flush=True)
            for s in kept[:5]:
                print(
                    f"    {s018.ist_str(int(s['signal_ts']))} {s['side']} "
                    f"level={s.get('ob_level')} ob_rsi={s.get('ob_rsi')} sig_rsi={s.get('sig_rsi')}",
                    flush=True,
                )

    if straddle_grid:
        wk_plan: list[tuple[int, str]] = []
        for tf in need_tfs:
            wk_plan.extend((int(s["ts"]), str(s["side"])) for s in sig_cache[tf][0])
        wok, wneed = weekly_chain_coverage(store, wk_plan)
        wmiss = max(0, int(wneed) - int(wok))
        wpct = (100.0 * wmiss / float(wneed)) if wneed else 0.0
        print(
            f"weekly chain entries ok={wok}/{wneed} missing={wmiss} ({wpct:.1f}%)",
            flush=True,
        )

    arm_n = max(1, len(work))
    t_all = time.perf_counter()
    for arm_i, w in enumerate(work, start=1):
        tf = str(w["tf"])
        kept, n_raw, _, _ = sig_cache[tf]
        run_one(
            store, spot_c, ts, kept, w, month, start_ts, cutoff, win_from, win_to,
            n_days, n_raw, done, cell_rows, t_all, arm_i, arm_n, max_days,
            archive, vwap_1m, c,
            exp_obh=float(args.exp_obh), exp_obl=float(args.exp_obl),
        )
        st = done.get(
            rsi_cell_key(
                month, w, max_days, start_ts, cutoff,
                float(args.exp_obh), float(args.exp_obl),
            ),
            {},
        )
        extra = ""
        if straddle_grid:
            extra = f" missing={int(st.get('n_missing', 0))}"
        if hedge_grid and "Wnone" in str(w.get("arm_id", "")):
            extra = (
                f" worst={s020._fnum(st.get('worst', float('nan')), 2)} "
                f"short_worst={s020._fnum(st.get('short_worst', float('nan')), 2)}"
            )
        print(
            f"[{arm_i}/{arm_n}] {tf} {w['arm_id']} n={int(st.get('n', 0))} "
            f"mean={s020._fnum(st.get('mean', float('nan')), 2)}{extra} "
            f"elapsed={time.perf_counter() - t_all:.0f}s",
            flush=True,
        )

    if straddle_grid and _STRADDLE_SAMPLES:
        print("first 3 straddle trades:", flush=True)
        for rec in _STRADDLE_SAMPLES[:3]:
            print(
                f"  {s018.ist_str(int(rec['entry_ts']))} {rec['side']} "
                f"strike={rec.get('strike')} call={rec.get('call_mark')} "
                f"put={rec.get('put_mark')} debit={rec.get('debit')} "
                f"iv_c={rec.get('iv_call')} iv_p={rec.get('iv_put')} "
                f"reason={rec.get('exit_reason')} net={rec.get('net')}",
                flush=True,
            )

    report = [
        f"S020 RSI month={month} {win_from}..{win_to} stamp={stamp}",
        f"signal={SIGNAL_NAME}",
        f"code_ver={CODE_VER} exp_obh={float(args.exp_obh)} exp_obl={float(args.exp_obl)}",
        f"mode={mode} combos={len(work)}",
    ]
    for tf in need_tfs:
        kept, n_raw, n_long, n_short = sig_cache[tf]
        report.append(
            f"TF {tf} raw={n_raw} after_filters={len(kept)} long={n_long} short={n_short}"
        )
    if straddle_grid:
        report.append(
            "straddle: LONG call+put same strike qty=1000; E1-E4; hard PRE_EXPIRY 30m"
        )
    if hedge_grid:
        report.append("NONE-wing arms: worst + short_worst in combo lines")
        report.append(
            f"sanity winged: {d.HEDGE_SANITY['checked']} checked, "
            f"{d.HEDGE_SANITY['violations']} violations"
        )
    for w in work:
        k = rsi_cell_key(
            month, w, max_days, start_ts, cutoff,
            float(args.exp_obh), float(args.exp_obl),
        )
        st = done.get(k)
        if st is None:
            continue
        line = f"{k} ALL {d.fmt_dev(st)}"
        if hedge_grid and "Wnone" in str(w.get("arm_id", "")):
            line += (
                f" worst_trade={s020._fnum(st.get('worst', float('nan')), 2)}"
                f" short_side_worst={s020._fnum(st.get('short_worst', float('nan')), 2)}"
            )
        report.append(line)
        if isinstance(st.get("wd"), dict):
            report.append(f"  WEEKDAY {s020.fmt_ww(st['wd'])}")
            report.append(f"  WEEKEND {s020.fmt_ww(st['we'])}")
        if np.isfinite(st.get("c2", float("nan"))):
            report.append(f"  C2mean={s020._fnum(st.get('c2'), 2)}")
    txtp = OUT_DIR / f"s020_rsi_{month}_{stamp}.txt"
    txtp.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"wrote {txtp}")
    print(f"TOTAL elapsed={time.perf_counter() - t_all:.0f}s", flush=True)
    archive.finalize()
    store.close()


if __name__ == "__main__":
    main()
