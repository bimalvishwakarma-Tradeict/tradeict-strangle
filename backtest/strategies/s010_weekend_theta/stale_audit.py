#!/usr/bin/env python3
"""S010 stale-exit-mark audit. Does NOT change P&L.

Classifies, for every tsOFF basket/leg, where the exit mark came from:
  real  = exact-minute close in the series
  near  = no exact minute, but a print within +/- NEAR_MAX_MIN minutes
  stale = no print in that window; engine would forward-fill an older value
  none  = series empty (engine keeps the entry seed)

Expired D+2 legs settle to spot intrinsic — PnL does not use the option print.
Feed class is still logged per-leg.

    python backtest\\strategies\\s010_weekend_theta\\stale_audit.py `
        --csv backtest\\data_1m\\BTCUSD_1m_20240630_20260921.csv `
        --out backtest\\strategies\\s010_weekend_theta\\runs
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, median

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.harness.data import (  # noqa: E402
    MarksStore,
    ist_dt,
    load_symbol_series,
    to_unix,
)
from backtest.strategies.s010_weekend_theta import config as cfg  # noqa: E402
from backtest.strategies.s010_weekend_theta.engine import (  # noqa: E402
    candidate_dates,
    load_spot_ohlc,
    simulate_day,
)
from backtest.strategies.s010_weekend_theta.preflight import ChainCache  # noqa: E402

NEAR_MAX_MIN = max(1, int(cfg.MARK_TOL_SEC) // 60)
PROGRESS_EVERY = 50
DOW = cfg.DOW_NAMES


def _pctile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    i = (len(s) - 1) * p
    lo = int(math.floor(i))
    hi = int(math.ceil(i))
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - i) + s[hi] * (i - lo)


def classify_feed(
    series: dict[int, float], exit_ts: int
) -> tuple[str, int | None]:
    """(class, lag_minutes). lag is 0 for real; +N means N min before exit."""
    minute = (int(exit_ts) // 60) * 60
    m = series.get(minute)
    if m is not None and m > 0:
        return "real", 0
    for lag in range(1, NEAR_MAX_MIN + 1):
        for cand in (minute - lag * 60, minute + lag * 60):
            v = series.get(cand)
            if v is not None and v > 0:
                return "near", lag
    earlier = [
        t for t, v in series.items() if t <= minute and v is not None and v > 0
    ]
    if earlier:
        return "stale", int((minute - max(earlier)) // 60)
    later = [t for t, v in series.items() if t > minute and v is not None and v > 0]
    if later:
        return "stale", -int((min(later) - minute) // 60)
    return "none", None


def basket_pnl_stale(leg_rows: list[dict]) -> bool:
    for r in leg_rows:
        if r["pnl_uses"] == "intrinsic":
            continue
        if r["feed"] in ("stale", "none"):
            return True
    return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ap = argparse.ArgumentParser(description="S010 stale-exit-mark audit")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-days", type=int, default=0)
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("===== STEP 1 — SHARED HARNESS CHECK =====")
    lines.append(
        "_ffill_series is DEFINED LOCALLY in "
        "backtest/strategies/s010_weekend_theta/engine.py "
        "(not in backtest/harness/)."
    )
    lines.append("Does this engine import S010 _ffill_series?")
    lines.append(
        "  s001 mark engine : NO  (own fallback: mark_of() or last series key or entry_mark)"
    )
    lines.append(
        "  s006             : NO  (own _mark_at +/-60s, then `or leg.entry_mark` at exit)"
    )
    lines.append(
        "  s007b            : NO  (own local copy `_ffill_marks`, same seed-on-empty pattern)"
    )
    lines.append(
        "  s008             : NO  (entry mark_at PK lookup +/- MARK_TOL; expiry = settlement spot)"
    )
    lines.append(
        "NOTE: s001 and s006 do NOT share the function, but both have a "
        "similar 'if mark missing, use entry_mark / last known' fallback. "
        "That is a cousin of this bug, not this function. S001 4/5 FAIL "
        "cannot be blamed on S010 ffill, but S001's own last-known fallback "
        "can still stale-mark exits."
    )
    lines.append("")

    t0 = time.monotonic()
    print("loading spot...", flush=True)
    spot = load_spot_ohlc(Path(args.csv))
    dates = candidate_dates(spot)
    if args.max_days and args.max_days < len(dates):
        dates = dates[: args.max_days]
    print(f"entry_dates={len(dates)}", flush=True)

    store = MarksStore()
    cache = ChainCache()
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    basket_rows: list[dict] = []
    leg_rows_all: list[dict] = []
    stale_prot_days: list[dict] = []

    n_dates = len(dates)
    for i, d in enumerate(dates, 1):
        for arm in ("A", "B"):
            res, _reason = simulate_day(
                d=d,
                spot=spot,
                store=store,
                chain_cache=cache,
                arm=arm,  # type: ignore[arg-type]
                ts_on=False,
            )
            if res is None:
                continue
            legs = res.legs
            series_map = [
                load_symbol_series(store, lg.symbol, res.entry_ts, res.exit_ts)
                for lg in legs
            ]
            this_legs: list[dict] = []
            for lg, ser in zip(legs, series_map):
                feed, lag = classify_feed(ser, res.exit_ts)
                pnl_uses = "intrinsic" if lg.expires_d2 else "live_mark"
                row = {
                    "date": d.isoformat(),
                    "dow": res.dow,
                    "arm": arm,
                    "symbol": lg.symbol,
                    "side": lg.side,
                    "opt": lg.opt,
                    "expiry": lg.expiry.isoformat(),
                    "strike": lg.strike,
                    "expires_d2": int(lg.expires_d2),
                    "pnl_uses": pnl_uses,
                    "feed": feed,
                    "lag_min": "" if lag is None else str(lag),
                    "net_pnl": res.net_pnl,
                }
                this_legs.append(row)
                leg_rows_all.append(row)
                if (
                    arm == "A"
                    and lg.side == "long"
                    and not lg.expires_d2
                    and feed in ("stale", "none")
                ):
                    stale_prot_days.append(
                        {
                            "d": d,
                            "dow": res.dow,
                            "opt": lg.opt,
                            "symbol": lg.symbol,
                            "feed": feed,
                            "lag": lag,
                            "series": ser,
                            "entry_ts": res.entry_ts,
                            "exit_ts": res.exit_ts,
                            "spot_entry": res.spot_entry,
                            "spot_exit": res.spot_exit,
                            "net_pnl": res.net_pnl,
                        }
                    )

            any_stale = any(r["feed"] in ("stale", "none") for r in this_legs)
            pnl_stale = basket_pnl_stale(this_legs)
            n_stale_feed = sum(
                1 for r in this_legs if r["feed"] in ("stale", "none")
            )
            n_real = sum(1 for r in this_legs if r["feed"] == "real")
            n_near = sum(1 for r in this_legs if r["feed"] == "near")
            move = (
                abs(res.spot_exit - res.spot_entry) / res.spot_entry * 100.0
                if res.spot_entry > 0
                else float("nan")
            )
            none_any = any(r["feed"] == "none" for r in this_legs)
            basket_rows.append(
                {
                    "date": d.isoformat(),
                    "dow": res.dow,
                    "arm": arm,
                    "net_pnl": res.net_pnl,
                    "spot_entry": res.spot_entry,
                    "spot_exit": res.spot_exit,
                    "move_pct": move,
                    "n_real": n_real,
                    "n_near": n_near,
                    "n_stale_or_none_feed": n_stale_feed,
                    "any_stale": int(any_stale),
                    "pnl_stale": int(pnl_stale),
                    "worst_feed": (
                        "none"
                        if none_any
                        else (
                            "stale"
                            if any_stale
                            else ("near" if n_near else "real")
                        )
                    ),
                }
            )
        if i % PROGRESS_EVERY == 0 or i == n_dates:
            print(
                f"  .. {i}/{n_dates} day={d} baskets={len(basket_rows)}",
                flush=True,
            )

    csv_path = out_dir / f"s010_stale_audit_{stamp}.csv"
    basket_by = {(r["date"], r["arm"]): r for r in basket_rows}
    fields = [
        "date",
        "dow",
        "arm",
        "symbol",
        "side",
        "opt",
        "expiry",
        "strike",
        "expires_d2",
        "pnl_uses",
        "feed",
        "lag_min",
        "any_stale",
        "pnl_stale",
        "worst_feed",
        "net_pnl",
        "spot_entry",
        "spot_exit",
        "move_pct",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for lg in leg_rows_all:
            b = basket_by[(lg["date"], lg["arm"])]
            w.writerow(
                {
                    **{k: lg[k] for k in lg},
                    "any_stale": b["any_stale"],
                    "pnl_stale": b["pnl_stale"],
                    "worst_feed": b["worst_feed"],
                    "spot_entry": b["spot_entry"],
                    "spot_exit": b["spot_exit"],
                    "move_pct": b["move_pct"],
                }
            )

    lines.append("===== STEP 2 — STALE SCOPE (tsOFF, DATA_START onward) =====")
    lines.append(
        f"near_window_min={NEAR_MAX_MIN} (engine MARK_TOL_SEC={cfg.MARK_TOL_SEC})"
    )
    lines.append(
        "any_stale = koi bhi leg feed stale/none (D+2 17:30 print missing counts)"
    )
    lines.append(
        "pnl_stale = live-mark legs only (D+2 expire-to-intrinsic excluded)"
    )
    for arm in ("A", "B"):
        sub = [r for r in basket_rows if r["arm"] == arm]
        n = len(sub)
        n_any = sum(r["any_stale"] for r in sub)
        n_s = sum(r["pnl_stale"] for r in sub)
        pct = 100.0 * n_any / n if n else 0.0
        pct_p = 100.0 * n_s / n if n else 0.0
        lines.append(
            f"  arm {arm}: n={n} any_stale={n_any} ({pct:.1f}%) "
            f"pnl_stale={n_s} ({pct_p:.1f}%)"
        )
        stale_pnl = [r["net_pnl"] for r in sub if r["any_stale"]]
        real_pnl = [r["net_pnl"] for r in sub if not r["any_stale"]]
        if stale_pnl:
            lines.append(
                f"    stale mean_net={fmean(stale_pnl):.2f} n={len(stale_pnl)}"
            )
        else:
            lines.append("    stale mean_net=n/a (0 baskets)")
        if real_pnl:
            lines.append(
                f"    real  mean_net={fmean(real_pnl):.2f} n={len(real_pnl)}"
            )
        if stale_pnl and real_pnl:
            diff = fmean(stale_pnl) - fmean(real_pnl)
            pull = "DOWN" if diff < 0 else "UP"
            lines.append(
                f"    stale - real = {diff:.2f}  -> stale pulls result {pull}"
            )
        for dow in DOW:
            ds = [r for r in sub if r["dow"] == dow]
            if not ds:
                continue
            ns = sum(r["any_stale"] for r in ds)
            lines.append(
                f"    {dow}: {100.0 * ns / len(ds):.1f}% any_stale ({ns}/{len(ds)})"
            )
        months: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in sub:
            ym = r["date"][:7]
            months[ym][1] += 1
            months[ym][0] += r["any_stale"]
        lines.append("    by month (stale/n):")
        for ym in sorted(months):
            a, b = months[ym]
            lines.append(f"      {ym}: {a}/{b} ({100.0 * a / b:.1f}%)")
    lines.append("")

    lines.append("===== STEP 3 — STALE PROT DAYS (arm A live D+3 legs) =====")
    by_date: dict = {}
    for rec in stale_prot_days:
        by_date.setdefault(rec["d"], rec)
    lines.append(
        f"  n_stale_prot_legs={len(stale_prot_days)} n_days={len(by_date)}"
    )

    lags_hold: list[float] = []
    lags_d2: list[float] = []
    day_kind: dict = {}
    for rec in stale_prot_days:
        exp2 = rec["d"] + timedelta(days=2)
        day0 = to_unix(ist_dt(exp2, 0, 0))
        day_ser = load_symbol_series(store, rec["symbol"], day0, rec["exit_ts"])
        n_day = sum(
            1
            for t, v in day_ser.items()
            if day0 <= t <= rec["exit_ts"] and v > 0
        )
        minute = (int(rec["exit_ts"]) // 60) * 60
        at_close = day_ser.get(minute)
        if n_day == 0:
            kind = "WHOLE_D+2_DAY_MISSING"
        elif at_close is None or at_close <= 0:
            kind = "MISSING_ONLY_AT_1730"
        else:
            kind = "HAS_1730"
        earlier_d2 = [
            t
            for t, v in day_ser.items()
            if t <= minute and v is not None and v > 0
        ]
        if earlier_d2:
            lags_d2.append(float((minute - max(earlier_d2)) // 60))
        earlier_hold = [
            t
            for t, v in rec["series"].items()
            if t <= minute and v is not None and v > 0
        ]
        if earlier_hold:
            lags_hold.append(float((minute - max(earlier_hold)) // 60))
        rec["kind"] = kind
        rec["n_day"] = n_day
        rec["move"] = (
            abs(rec["spot_exit"] - rec["spot_entry"]) / rec["spot_entry"] * 100.0
        )
        prev = day_kind.get(rec["d"])
        if prev is None:
            day_kind[rec["d"]] = kind
        elif prev != kind:
            day_kind[rec["d"]] = "MIXED"

    whole_day = sum(1 for k in day_kind.values() if k == "WHOLE_D+2_DAY_MISSING")
    only_close = sum(1 for k in day_kind.values() if k == "MISSING_ONLY_AT_1730")
    mixed = sum(1 for k in day_kind.values() if k == "MIXED")
    lines.append(
        f"  A) days: whole D+2 missing={whole_day}  only-1730-missing={only_close}  mixed={mixed}"
    )
    if lags_d2:
        lines.append(
            f"  B) nearest real mark ON D+2 (min before 17:30): "
            f"n={len(lags_d2)} median={median(lags_d2):.1f} p90={_pctile(lags_d2, 0.90):.1f}"
        )
    else:
        lines.append("  B) no earlier mark on D+2 at all (D+2 lag undefined)")
    if lags_hold:
        lines.append(
            f"     nearest real mark in HOLD window (may be D/D+1): "
            f"n={len(lags_hold)} median={median(lags_hold):.1f} "
            f"p90={_pctile(lags_hold, 0.90):.1f}"
        )

    a_baskets = [r for r in basket_rows if r["arm"] == "A"]
    stale_dates = {rec["d"].isoformat() for rec in by_date.values()}
    stale_mv = [r["move_pct"] for r in a_baskets if r["date"] in stale_dates]
    real_mv = [r["move_pct"] for r in a_baskets if r["date"] not in stale_dates]
    lines.append("  C) |spot move|% entry->exit (arm A):")
    if stale_mv:
        lines.append(
            f"    stale days: n={len(stale_mv)} mean={fmean(stale_mv):.3f} "
            f"median={median(stale_mv):.3f} p90={_pctile(stale_mv, 0.90):.3f}"
        )
    if real_mv:
        lines.append(
            f"    other days: n={len(real_mv)} mean={fmean(real_mv):.3f} "
            f"median={median(real_mv):.3f} p90={_pctile(real_mv, 0.90):.3f}"
        )
    if stale_mv and real_mv and fmean(real_mv) > 0:
        lines.append(
            f"    stale/other mean-move ratio = {fmean(stale_mv) / fmean(real_mv):.2f}x"
        )
    lines.append("  stale prot legs:")
    for rec in sorted(stale_prot_days, key=lambda x: (x["d"], x["opt"])):
        lines.append(
            f"    {rec['d']} {rec['dow']} {rec['opt']} {rec['kind']} "
            f"n_prints_d2={rec['n_day']} lag_hold={rec['lag']} "
            f"move%={rec['move']:.2f} net={rec['net_pnl']:.1f} {rec['symbol']}"
        )
    lines.append("")
    elapsed = time.monotonic() - t0
    lines.append(f"elapsed_s={elapsed:.1f} csv={csv_path.name}")

    tpath = out_dir / f"s010_stale_audit_{stamp}.txt"
    tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for ln in lines:
        print(ln, flush=True)
    print(f"txt={tpath}", flush=True)
    print(f"DONE in {elapsed:.1f}s", flush=True)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
