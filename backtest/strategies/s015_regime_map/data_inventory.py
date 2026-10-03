#!/usr/bin/env python3
"""S015 Part A — data inventory.

python backtest\\strategies\\s015_regime_map\\data_inventory.py
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parents[2]
_ROOT = _BACKTEST.parent
for _p in (str(_ROOT), str(_BACKTEST)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SPOT_CSV = _BACKTEST / "data_1m" / "BTCUSD_1m_20240630_20260921.csv"
MARKS_DIR = _BACKTEST / "cache" / "option_marks"
IV_DIR = _BACKTEST / "cache" / "iv_surface"
OUT_DIR = _BACKTEST / "strategies" / "s015_regime_map" / "runs"
UTC = timezone.utc


def _ts(x: int) -> str:
    return datetime.fromtimestamp(int(x), tz=UTC).isoformat()


def inventory_spot(lines: list[str]) -> None:
    lines.append("=== spot 1m CSV ===")
    lines.append(f"path={SPOT_CSV}")
    if not SPOT_CSV.is_file():
        lines.append("MISSING")
        return
    first = last = None
    prev = None
    gaps = 0
    n = 0
    with SPOT_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            n += 1
            if first is None:
                first = ts
            last = ts
            if prev is not None and ts - prev > 5 * 60:
                gaps += 1
            prev = ts
    lines.append(f"rows={n} first_ts={first} ({_ts(first) if first else ''})")
    lines.append(f"last_ts={last} ({_ts(last) if last else ''})")
    lines.append(f"gaps_gt_5min={gaps}")


def inventory_marks(lines: list[str]) -> None:
    lines.append("")
    lines.append("=== option_marks sqlite ===")
    files = sorted(MARKS_DIR.glob("marks_*.sqlite"))
    lines.append(f"dir={MARKS_DIR} months={len(files)}")
    months = [p.stem.replace("marks_", "") for p in files]
    lines.append(f"months_present={months}")
    lines.append(
        "per-month ts min/max: skipped (marks PK is symbol+ts; no ts-only index; "
        "full-table MIN(ts) is too slow). Shard coverage = calendar month in filename."
    )
    for ym in months:
        y, m = int(ym[:4]), int(ym[5:7])
        lines.append(f"  {ym} shard_calendar={y:04d}-{m:02d} (file marks_{ym}.sqlite)")


def inventory_iv(lines: list[str]) -> None:
    lines.append("")
    lines.append("=== cache/iv_surface ===")
    lines.append(f"dir={IV_DIR} exists={IV_DIR.is_dir()}")
    if not IV_DIR.is_dir():
        lines.append("ATM IV: no  25-delta skew: no")
        return
    for p in sorted(IV_DIR.glob("*")):
        lines.append(f"  file={p.name} bytes={p.stat().st_size}")
    try:
        from backtest.iv_surface import load_surface

        loaded = load_surface("full")
    except Exception as e:
        lines.append(f"load_surface error: {type(e).__name__}: {e}")
        loaded = None
    if not loaded:
        lines.append("ATM IV available: unknown (load failed)")
        lines.append("25-delta skew available: no")
        return
    surface, stats = loaded
    buckets = sorted(surface.expiries_by_bucket.keys()) if surface.expiries_by_bucket else []
    lines.append(f"n_smiles={len(surface.smiles)} n_buckets={len(buckets)}")
    if buckets:
        lines.append(
            f"bucket_ts first={int(buckets[0])} ({_ts(int(buckets[0]))}) "
            f"last={int(buckets[-1])} ({_ts(int(buckets[-1]))})"
        )
    lines.append("ATM IV available: yes (IVSurface.iv / smile a,b,c at k=ln(K/F)~0)")
    lines.append(
        "25-delta skew available: yes (compute from smile IV at 25d strikes; "
        "no dedicated 25d column in pickle)"
    )


def inventory_trades(lines: list[str]) -> None:
    lines.append("")
    lines.append("=== options-trades parquet / sqlite ===")
    pqs = sorted((_BACKTEST / "cache").glob("options-trades-*.parquet"))
    lines.append(f"parquet_count={len(pqs)}")
    import re
    import zipfile

    first_d = last_d = None
    for p in pqs:
        lines.append(f"  parquet={p.name} bytes={p.stat().st_size}")
        m = re.search(r"(20\d{2}-\d{2}(?:-\d{2})?)", p.name)
        if m:
            d = m.group(1)
            if first_d is None or d < first_d:
                first_d = d
            if last_d is None or d > last_d:
                last_d = d
    lines.append(f"parquet_filename_date_range={first_d} .. {last_d}")
    lines.append(
        "parquet columns: pyarrow/fastparquet not installed — schema from source zip header + sqlite shards"
    )
    zips = sorted((_BACKTEST / "data_raw").glob("options-trades-*.zip"))
    if zips:
        with zipfile.ZipFile(zips[0]) as zf:
            name = zf.namelist()[0]
            with zf.open(name) as fh:
                header = fh.readline().decode("utf-8", errors="replace").strip()
        cols = [c.strip() for c in header.split(",")]
        lines.append(f"zip_header_file={zips[0].name} columns={cols}")
        has_size = any(c.lower() == "size" for c in cols)
        has_side = any(
            c.lower() in {"side", "buyer_role", "aggressor", "role"} for c in cols
        )
        lines.append(f"has_size={has_size} has_side_or_aggressor={has_side}")
    else:
        cols = []
        lines.append("no data_raw zips")
    shards = sorted((_BACKTEST / "cache" / "options_trades").glob("opt_trades_*.sqlite"))
    lines.append(f"sqlite_shards={len(shards)} {[x.name for x in shards]}")
    if shards:
        c0 = sqlite3.connect(f"file:{shards[0].resolve().as_posix()}?mode=ro", uri=True)
        try:
            info = c0.execute("PRAGMA table_info(trades)").fetchall()
        finally:
            c0.close()
        lines.append(f"sqlite_trades_columns={[r[1] for r in info]}")
        lines.append("sqlite has size=yes buyer_role via role (0=maker,1=taker)=yes")


def inventory_missing_series(lines: list[str]) -> None:
    lines.append("")
    lines.append("=== OI / funding / liquidation / orderbook history ===")
    roots = [_BACKTEST, _ROOT / "docs"]
    needles = (
        "open_interest",
        "funding_rate",
        "liquidation",
        "l2orderbook",
        "orderbook_history",
        "oi_history",
    )
    found: dict[str, list[str]] = {k: [] for k in needles}
    skip_bits = {"node_modules", ".git", "package-lock"}
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            sp = str(p).replace("\\", "/").lower()
            if any(b in sp for b in skip_bits):
                continue
            name = p.name.lower()
            for k in needles:
                if k in name or k in sp:
                    found[k].append(str(p.relative_to(_ROOT)) if _ROOT in p.parents else str(p))
    for k, paths in found.items():
        uniq = sorted(set(paths))[:8]
        if uniq:
            lines.append(f"{k}: YES paths={uniq}")
        else:
            lines.append(f"{k}: NO (no historical files under backtest/ or docs/)")
    lines.append(
        "note: live REST orderbook exists in backend client only — not a history dataset"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = [
        "S015 data inventory",
        f"stamp={stamp}",
        "",
    ]
    inventory_spot(lines)
    print("\n".join(lines[-8:]), flush=True)
    inventory_marks(lines)
    print("marks done", flush=True)
    inventory_iv(lines)
    print("iv done", flush=True)
    inventory_trades(lines)
    print("trades done", flush=True)
    inventory_missing_series(lines)
    text = "\n".join(lines) + "\n"
    out = OUT_DIR / f"s015_inventory_{stamp}.txt"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
