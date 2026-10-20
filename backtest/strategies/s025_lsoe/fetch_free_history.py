#!/usr/bin/env python3
"""Count (HEAD) then optionally download free Tardis + Binance history.

Without --go: print counts and total size, then exit.
With --go: download files that are missing locally.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import httpx

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "history"
TARDIS_DIR = DATA / "tardis_deribit_options_chain"
BINANCE_DIR = DATA / "binance_um"
UTC = timezone.utc
TARDIS_START = date(2024, 9, 1)
TARDIS_END = date(2026, 10, 1)
BINANCE_START = date(2024, 9, 1)
TIMEOUT = 60.0
USER_AGENT = "Tradeict-S025-Fetch/1.0"
TARDIS_BASE = "https://datasets.tardis.dev/v1/deribit/options_chain"
BINANCE_BASE = "https://data.binance.vision/data/futures/um/daily"

logger = logging.getLogger("s025.fetch")


def month_firsts(start: date, end: date) -> list[date]:
    cur = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    out: list[date] = []
    while cur <= last:
        out.append(cur)
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def daterange(start: date, end: date) -> Iterable[date]:
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def tardis_url(day: date) -> str:
    return f"{TARDIS_BASE}/{day.year:04d}/{day.month:02d}/01/OPTIONS.csv.gz"


def tardis_local(day: date) -> Path:
    return TARDIS_DIR / f"{day.year:04d}-{day.month:02d}-01_OPTIONS.csv.gz"


def binance_url(kind: str, day: date) -> str:
    tag = day.isoformat()
    if kind == "metrics":
        return f"{BINANCE_BASE}/metrics/BTCUSDT/BTCUSDT-metrics-{tag}.zip"
    return f"{BINANCE_BASE}/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{tag}.zip"


def binance_local(kind: str, day: date) -> Path:
    return BINANCE_DIR / kind / f"BTCUSDT-{kind}-{day.isoformat()}.zip"


def today_utc() -> date:
    return datetime.now(tz=UTC).date()


def plan() -> list[tuple[str, str, Path]]:
    jobs: list[tuple[str, str, Path]] = []
    for day in month_firsts(TARDIS_START, TARDIS_END):
        jobs.append(("tardis", tardis_url(day), tardis_local(day)))
    end = today_utc()
    for day in daterange(BINANCE_START, end):
        jobs.append(("binance_metrics", binance_url("metrics", day), binance_local("metrics", day)))
        jobs.append(("binance_funding", binance_url("funding", day), binance_local("funding", day)))
    return jobs


def head_size(client: httpx.Client, url: str) -> tuple[int, int | None]:
    try:
        resp = client.head(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.warning("HEAD fail %s %s", url, exc)
        return 0, None
    length = resp.headers.get("content-length")
    size = int(length) if length and str(length).isdigit() else None
    return resp.status_code, size


def fmt_bytes(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{n} B"


def download(client: httpx.Client, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1024 * 256):
                fh.write(chunk)
    tmp.replace(dest)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="S025 free history count/download")
    parser.add_argument(
        "--go",
        action="store_true",
        help="download after counting (default: count + size only)",
    )
    args = parser.parse_args(argv)

    jobs = plan()
    print(
        f"plan tardis_1st={len(month_firsts(TARDIS_START, TARDIS_END))} "
        f"binance_days={(today_utc() - BINANCE_START).days + 1} "
        f"total_urls={len(jobs)}"
    )
    client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    sizes: dict[str, int] = {}
    counts: dict[str, int] = {}
    missing: dict[str, int] = {}
    try:
        for kind, url, _dest in jobs:
            status, size = head_size(client, url)
            if status == 200 and size is not None:
                counts[kind] = counts.get(kind, 0) + 1
                sizes[kind] = sizes.get(kind, 0) + size
            else:
                missing[kind] = missing.get(kind, 0) + 1
                logger.info("not 200 kind=%s status=%s size=%s url=%s", kind, status, size, url)
    finally:
        if not args.go:
            client.close()

    grand = sum(sizes.values())
    print("=== COUNT / SIZE (HEAD only) ===")
    for kind in ("tardis", "binance_metrics", "binance_funding"):
        print(
            f"{kind}: ok={counts.get(kind, 0)} missing={missing.get(kind, 0)} "
            f"bytes={sizes.get(kind, 0)} ({fmt_bytes(sizes.get(kind, 0))})"
        )
    print(f"TOTAL ok_bytes={grand} ({fmt_bytes(grand)}) urls={len(jobs)}")
    if not args.go:
        print("Stopped. Pass --go to download.")
        return 0

    print("Downloading missing files (--go)…")
    n_ok = 0
    n_skip = 0
    n_fail = 0
    try:
        for kind, url, dest in jobs:
            if dest.exists() and dest.stat().st_size > 0:
                n_skip += 1
                continue
            try:
                download(client, url, dest)
                n_ok += 1
                logger.info("saved %s %s", kind, dest)
            except (httpx.HTTPError, OSError) as exc:
                n_fail += 1
                logger.error("download fail %s %s %s", kind, url, exc)
    finally:
        client.close()
    print(f"download saved={n_ok} skipped_existing={n_skip} fail={n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
