#!/usr/bin/env python3
"""
Read-only integrity check for option mark shards.

No download, no HTTP. Opens marks_*.sqlite with mode=ro only.
Does not modify download_option_marks.py or live bot code.

Output: console + backtest/results/verify_marks.txt
"""

from __future__ import annotations

import argparse
import calendar
import csv
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BACKTEST = Path(__file__).resolve().parent
_ROOT = _BACKTEST.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_BACKTEST) not in sys.path:
    sys.path.insert(0, str(_BACKTEST))

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

MARKS_DIR = _BACKTEST / "cache" / "option_marks"
DATA_1M_DIR = _BACKTEST / "data_1m"
OUT_PATH = _BACKTEST / "results" / "verify_marks.txt"

ENTRY_HOUR_IST = 11
ENTRY_MINUTE_IST = 0
SETTLE_HOUR_UTC = 12
SHORT_DATED_MAX_LIFE_DAYS = 14
TAIL_COVERAGE_DAYS = 5
ATM_STRIKE_COUNT = 10
ENTRY_TS_TOL_SEC = 120
# Shard named by expiry month: marks can start up to ~life+tail before month 1
MIN_TS_SLACK_DAYS = 16

logger = logging.getLogger("verify_marks")


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def parse_year_month(s: str) -> tuple[int, int]:
    parts = s.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"expected YYYY-MM, got {s!r}")
    year, month = int(parts[0]), int(parts[1])
    if not (1 <= month <= 12):
        raise ValueError(f"invalid month in {s!r}")
    return year, month


def settle_ts(expiry: date) -> int:
    return int(
        datetime(
            expiry.year, expiry.month, expiry.day, SETTLE_HOUR_UTC, 0, 0, tzinfo=UTC
        ).timestamp()
    )


def ist_dt(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=IST)


def to_unix(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp())


def ts_to_ist_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M IST"
    )


def ts_to_ist_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(IST).date()


def month_calendar_days(year: int, month: int) -> list[date]:
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)]


def month_bounds_unix(year: int, month: int) -> tuple[int, int]:
    """IST month start .. last day 23:59:59 IST as unix."""
    start = to_unix(ist_dt(date(year, month, 1), 0, 0))
    last = date(year, month, calendar.monthrange(year, month)[1])
    end = to_unix(ist_dt(last, 23, 59)) + 59
    return start, end


@dataclass
class SanityResult:
    name: str
    ok: bool
    detail: str


@dataclass
class ShardReport:
    ym: str
    path: Path
    size_gb: float
    n_rows: int
    n_symbols: int
    n_expiries: int
    min_ts: int | None
    max_ts: int | None
    expiries: list[str]
    missing_calendar_expiries: list[str]
    n_short_symbols: int
    n_long_symbols: int
    expiry_details: list[str]
    bad_close_rows: int
    dup_rows: int
    pcp_pct: float
    days_with_0dte_1100: list[str]
    days_missing_0dte_1100: list[str]
    sanities: list[SanityResult] = field(default_factory=list)
    marks_ist_days: set[date] = field(default_factory=set)


def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def list_shards(months_filter: list[str] | None) -> list[Path]:
    all_paths = sorted(MARKS_DIR.glob("marks_*.sqlite"))
    if not months_filter:
        return all_paths
    want = set(months_filter)
    out = []
    for p in all_paths:
        ym = p.stem.replace("marks_", "")
        if ym in want:
            out.append(p)
    return out


def load_spot_days() -> tuple[set[date], date | None, date | None]:
    """IST dates covered by BTCUSD_1m CSV (latest file)."""
    matches = sorted(DATA_1M_DIR.glob("BTCUSD_1m_*.csv"))
    if not matches:
        return set(), None, None
    path = matches[-1]
    days: set[date] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = int(row["open_time_unix"])
            days.add(ts_to_ist_date(ts))
    if not days:
        return set(), None, None
    return days, min(days), max(days)


def classify_symbols_short_long(
    conn: sqlite3.Connection,
) -> tuple[int, int]:
    """
    short: settle - MIN(ts) <= 14d; long (monthly): > 14d.
    Uses per-symbol MIN(ts) and expiry settle.
    """
    rows = conn.execute(
        """
        SELECT symbol, expiry, MIN(ts) AS mn
        FROM marks
        GROUP BY symbol
        """
    ).fetchall()
    n_short = 0
    n_long = 0
    cutoff = SHORT_DATED_MAX_LIFE_DAYS * 86400
    for _sym, exp_s, mn in rows:
        try:
            exp = date.fromisoformat(str(exp_s))
        except ValueError:
            n_short += 1
            continue
        life = settle_ts(exp) - int(mn)
        if life > cutoff:
            n_long += 1
        else:
            n_short += 1
    return n_short, n_long


def atm_strikes(strikes: list[float], atm: float, n: int = ATM_STRIKE_COUNT) -> list[float]:
    if not strikes:
        return []
    ordered = sorted(strikes, key=lambda k: (abs(k - atm), k))
    return ordered[:n]


def coverage_last_5d(
    conn: sqlite3.Connection,
    expiry: date,
    strikes: list[float],
) -> float:
    """
    Minute coverage % over [settle-5d, settle] for ATM-near strikes
    (any opt_type mark at that strike counts for that minute).
    """
    if not strikes:
        return float("nan")
    settle = settle_ts(expiry)
    w0 = settle - TAIL_COVERAGE_DAYS * 86400
    w1 = settle
    expected = max(0, (w1 - w0) // 60)
    if expected <= 0:
        return float("nan")
    # Count distinct minutes that have at least one mark among selected strikes
    placeholders = ",".join("?" * len(strikes))
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT ts) FROM marks
        WHERE expiry=? AND strike IN ({placeholders})
          AND ts >= ? AND ts <= ?
          AND close IS NOT NULL AND close > 0
        """,
        (expiry.isoformat(), *strikes, w0, w1),
    ).fetchone()
    n = int(row[0]) if row and row[0] is not None else 0
    return 100.0 * n / float(expected)


def has_0dte_chain_at_1100(conn: sqlite3.Connection, day: date) -> bool:
    """True if expiry=day has marks near 11:00 IST."""
    target = to_unix(ist_dt(day, ENTRY_HOUR_IST, ENTRY_MINUTE_IST))
    row = conn.execute(
        """
        SELECT 1 FROM marks
        WHERE expiry=? AND ts BETWEEN ? AND ?
          AND close IS NOT NULL AND close > 0
        LIMIT 1
        """,
        (
            day.isoformat(),
            target - ENTRY_TS_TOL_SEC,
            target + ENTRY_TS_TOL_SEC,
        ),
    ).fetchone()
    return row is not None


def pcp_pair_pct(conn: sqlite3.Connection, year: int, month: int) -> float:
    """
    % of (expiry, strike) at sampled minutes that have BOTH call and put.
    Samples 11:00 IST on day 5/15/25 of the shard month (cheap vs full scan).
    """
    sample_days = []
    last = calendar.monthrange(year, month)[1]
    for d in (5, 15, 25):
        if d <= last:
            sample_days.append(date(year, month, d))
    if not sample_days:
        sample_days = [date(year, month, 1)]

    n_any = 0
    n_both = 0
    for day in sample_days:
        ts = to_unix(ist_dt(day, ENTRY_HOUR_IST, ENTRY_MINUTE_IST))
        rows = conn.execute(
            """
            SELECT expiry, strike,
                   SUM(CASE WHEN opt_type='call' THEN 1 ELSE 0 END) AS hc,
                   SUM(CASE WHEN opt_type='put' THEN 1 ELSE 0 END) AS hp
            FROM marks
            WHERE ts BETWEEN ? AND ?
              AND close IS NOT NULL AND close > 0
            GROUP BY expiry, strike
            """,
            (ts - ENTRY_TS_TOL_SEC, ts + ENTRY_TS_TOL_SEC),
        ).fetchall()
        for _e, _k, hc, hp in rows:
            n_any += 1
            if int(hc or 0) > 0 and int(hp or 0) > 0:
                n_both += 1
    if n_any <= 0:
        return float("nan")
    return 100.0 * n_both / float(n_any)


def analyze_shard(path: Path) -> ShardReport:
    ym = path.stem.replace("marks_", "")
    year, month = parse_year_month(ym)
    size_gb = path.stat().st_size / (1024**3)

    conn = open_ro(path)
    try:
        n_rows = int(conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0])
        n_symbols = int(
            conn.execute("SELECT COUNT(DISTINCT symbol) FROM marks").fetchone()[0]
        )
        n_expiries = int(
            conn.execute("SELECT COUNT(DISTINCT expiry) FROM marks").fetchone()[0]
        )
        mm = conn.execute("SELECT MIN(ts), MAX(ts) FROM marks").fetchone()
        min_ts = int(mm[0]) if mm and mm[0] is not None else None
        max_ts = int(mm[1]) if mm and mm[1] is not None else None

        expiries = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT expiry FROM marks ORDER BY expiry"
            ).fetchall()
        ]
        exp_set = set(expiries)
        cal_days = month_calendar_days(year, month)
        missing_cal = [
            d.isoformat() for d in cal_days if d.isoformat() not in exp_set
        ]

        n_short, n_long = classify_symbols_short_long(conn)

        # Per-expiry: strike count + last-5d ATM coverage
        expiry_details: list[str] = []
        for exp_s in expiries:
            try:
                exp = date.fromisoformat(exp_s)
            except ValueError:
                continue
            strikes = [
                float(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT strike FROM marks WHERE expiry=? ORDER BY strike",
                    (exp_s,),
                ).fetchall()
            ]
            if not strikes:
                expiry_details.append(f"  {exp_s}: strikes=0 coverage=n/a")
                continue
            atm = sorted(strikes)[len(strikes) // 2]
            near = atm_strikes(strikes, atm)
            cov = coverage_last_5d(conn, exp, near)
            cov_s = f"{cov:.1f}%" if cov == cov else "n/a"
            expiry_details.append(
                f"  {exp_s}: strikes={len(strikes)} "
                f"atm≈{atm:.0f} near10_cov_5d={cov_s}"
            )

        bad_close = int(
            conn.execute(
                "SELECT COUNT(*) FROM marks WHERE close IS NULL OR close <= 0"
            ).fetchone()[0]
        )
        # PRIMARY KEY (symbol, ts) should prevent dups; still count via GROUP BY
        dup = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(c - 1), 0) FROM (
                  SELECT COUNT(*) AS c FROM marks
                  GROUP BY symbol, ts HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        )
        pcp = pcp_pair_pct(conn, year, month)

        # 11:00 IST 0DTE chain per calendar day of month (through today / last data)
        today_ist = datetime.now(tz=IST).date()
        last_data_day = ts_to_ist_date(max_ts) if max_ts is not None else today_ist
        check_through = min(today_ist, last_data_day)
        days_ok: list[str] = []
        days_miss: list[str] = []
        for d in cal_days:
            if d > check_through:
                continue  # future / beyond data — not a fail
            if has_0dte_chain_at_1100(conn, d):
                days_ok.append(d.isoformat())
            else:
                days_miss.append(d.isoformat())

        # IST days that have any mark row (for overlap)
        marks_days: set[date] = set()
        if min_ts is not None and max_ts is not None:
            # Sample distinct IST dates via SQL on day buckets (expensive full scan
            # avoided: use daily steps between min/max checking existence)
            day0 = ts_to_ist_date(min_ts)
            day1 = ts_to_ist_date(max_ts)
            cur = day0
            while cur <= day1:
                t0 = to_unix(ist_dt(cur, 0, 0))
                t1 = to_unix(ist_dt(cur, 23, 59)) + 59
                row = conn.execute(
                    "SELECT 1 FROM marks WHERE ts BETWEEN ? AND ? LIMIT 1",
                    (t0, t1),
                ).fetchone()
                if row is not None:
                    marks_days.add(cur)
                cur += timedelta(days=1)

    finally:
        conn.close()

    report = ShardReport(
        ym=ym,
        path=path,
        size_gb=size_gb,
        n_rows=n_rows,
        n_symbols=n_symbols,
        n_expiries=n_expiries,
        min_ts=min_ts,
        max_ts=max_ts,
        expiries=expiries,
        missing_calendar_expiries=missing_cal,
        n_short_symbols=n_short,
        n_long_symbols=n_long,
        expiry_details=expiry_details,
        bad_close_rows=bad_close,
        dup_rows=dup,
        pcp_pct=pcp,
        days_with_0dte_1100=days_ok,
        days_missing_0dte_1100=days_miss,
        marks_ist_days=marks_days,
    )

    # --- Sanity checks ---
    # 1. rows > 0
    report.sanities.append(
        SanityResult(
            "rows_nonzero",
            n_rows > 0,
            f"n_rows={n_rows}",
        )
    )

    # 2. Shard month integrity: expiries in YYYY-MM; MAX not past month;
    #    some rows fall inside the calendar month. Early MIN(ts) is OK for
    #    monthlies (full listing life can start months before expiry month).
    m_start, m_end = month_bounds_unix(year, month)
    detail2_parts: list[str] = []
    ok2 = True
    exp_out = [e for e in expiries if not e.startswith(f"{year:04d}-{month:02d}")]
    if exp_out:
        ok2 = False
        detail2_parts.append(f"expiries_outside_month={exp_out[:5]}")
    if min_ts is None or max_ts is None:
        ok2 = False
        detail2_parts.append("min/max ts missing")
    else:
        if max_ts > m_end + 12 * 3600:
            ok2 = False
            detail2_parts.append(f"MAX(ts)={ts_to_ist_str(max_ts)} after month end")
        if not any(d.year == year and d.month == month for d in marks_days):
            ok2 = False
            detail2_parts.append("no mark rows with IST date inside shard month")
        detail2_parts.append(
            f"MIN={ts_to_ist_str(min_ts)} MAX={ts_to_ist_str(max_ts)}"
        )
    report.sanities.append(
        SanityResult(
            "min_max_in_month",
            ok2,
            "; ".join(detail2_parts),
        )
    )

    # 3. 11:00 IST 0DTE
    report.sanities.append(
        SanityResult(
            "0dte_chain_1100_ist",
            len(days_miss) == 0,
            f"missing_days={len(days_miss)} "
            f"{days_miss[:10]}{'...' if len(days_miss) > 10 else ''}",
        )
    )

    # 4. bad close
    report.sanities.append(
        SanityResult(
            "close_positive",
            bad_close == 0,
            f"bad_close_rows={bad_close}",
        )
    )

    # 5. duplicates
    report.sanities.append(
        SanityResult(
            "no_duplicate_symbol_ts",
            dup == 0,
            f"dup_extra_rows={dup}",
        )
    )

    # 6. PCP % (informational PASS if we could compute; FAIL only if 0 pairs when rows>0)
    pcp_ok = (n_rows == 0) or (pcp == pcp and pcp > 0)
    report.sanities.append(
        SanityResult(
            "put_call_same_minute",
            pcp_ok,
            f"pcp_both_pct={pcp:.2f}%" if pcp == pcp else "pcp=n/a",
        )
    )

    return report


def format_report(
    lines: list[str],
    shards: list[ShardReport],
    spot_days: set[date],
    spot_lo: date | None,
    spot_hi: date | None,
) -> None:
    fails: list[str] = []
    for s in shards:
        for sc in s.sanities:
            if not sc.ok:
                fails.append(f"FAIL [{s.ym}] {sc.name}: {sc.detail}")

    emit(lines, "===== VERIFY MARKS — FAIL SUMMARY =====")
    if not fails:
        emit(lines, "(none — all sanity checks PASS)")
    else:
        for f in fails:
            emit(lines, f)
    emit(lines)
    emit(lines, f"shards_checked={len(shards)}")
    emit(lines)

    # Overlap
    all_mark_days: set[date] = set()
    for s in shards:
        all_mark_days |= s.marks_ist_days
    if spot_lo is None or not all_mark_days or not spot_days:
        emit(lines, "OVERLAP WINDOW (marks + 1m candles): n/a (missing spot or marks days)")
        overlap: set[date] = set()
    else:
        overlap = all_mark_days & spot_days
        if overlap:
            o_lo, o_hi = min(overlap), max(overlap)
            # days in closed range that have marks
            span_days = (o_hi - o_lo).days + 1
            missing_in_span = []
            cur = o_lo
            while cur <= o_hi:
                if cur in spot_days and cur not in all_mark_days:
                    missing_in_span.append(cur.isoformat())
                cur += timedelta(days=1)
            emit(
                lines,
                f"OVERLAP WINDOW (marks + 1m candles): "
                f"{o_lo.isoformat()} se {o_hi.isoformat()}, "
                f"{len(overlap)} days",
            )
            emit(
                lines,
                f"  spot_csv_range={spot_lo.isoformat()} .. {spot_hi.isoformat()} "
                f"({len(spot_days)} days)",
            )
            emit(
                lines,
                f"  marks_days_in_overlap={len(overlap)}  "
                f"span_calendar_days={span_days}  "
                f"spot_days_missing_marks_in_span={len(missing_in_span)}",
            )
            if missing_in_span:
                emit(
                    lines,
                    f"  missing_marks_days_sample={missing_in_span[:20]}"
                    f"{'...' if len(missing_in_span) > 20 else ''}",
                )
        else:
            emit(
                lines,
                "OVERLAP WINDOW (marks + 1m candles): no overlapping IST days",
            )
    emit(lines)

    # Shard-wise table
    emit(lines, "===== SHARD TABLE =====")
    hdr = (
        f"{'ym':>7} {'GB':>5} {'rows':>12} {'syms':>6} {'exps':>4} "
        f"{'short':>6} {'long':>5} {'bad_c':>6} {'dups':>5} {'pcp%':>7} "
        f"{'miss_exp':>8} {'miss_1100':>9}"
    )
    emit(lines, hdr)
    emit(lines, "-" * len(hdr))
    for s in shards:
        pcp_s = f"{s.pcp_pct:6.2f}" if s.pcp_pct == s.pcp_pct else "   n/a"
        emit(
            lines,
            f"{s.ym:>7} {s.size_gb:5.2f} {s.n_rows:12d} {s.n_symbols:6d} "
            f"{s.n_expiries:4d} {s.n_short_symbols:6d} {s.n_long_symbols:5d} "
            f"{s.bad_close_rows:6d} {s.dup_rows:5d} {pcp_s:>7} "
            f"{len(s.missing_calendar_expiries):8d} "
            f"{len(s.days_missing_0dte_1100):9d}",
        )
    emit(lines)

    for s in shards:
        emit(lines, f"===== SHARD {s.ym} =====")
        emit(lines, f"path={s.path}")
        emit(lines, f"size_gb={s.size_gb:.3f}")
        emit(
            lines,
            f"rows={s.n_rows}  symbols={s.n_symbols}  expiries={s.n_expiries}  "
            f"short_syms={s.n_short_symbols}  long_syms={s.n_long_symbols}",
        )
        if s.min_ts is not None and s.max_ts is not None:
            emit(
                lines,
                f"MIN(ts)={ts_to_ist_str(s.min_ts)}  MAX(ts)={ts_to_ist_str(s.max_ts)}",
            )
        else:
            emit(lines, "MIN/MAX(ts)=n/a")
        emit(lines, f"expiries={s.expiries}")
        emit(
            lines,
            f"missing_calendar_expiries ({len(s.missing_calendar_expiries)}): "
            f"{s.missing_calendar_expiries}",
        )
        if "2025-04-26" in s.missing_calendar_expiries or s.ym == "2025-04":
            note = (
                "PRESENT"
                if "2025-04-26" in s.expiries
                else "MISSING"
            )
            emit(lines, f"NOTE 2025-04-26 expiry: {note} (download had spot miss)")
        emit(lines, "per-expiry strike count + last-5d near-ATM coverage:")
        for line in s.expiry_details:
            emit(lines, line)
        emit(
            lines,
            f"0dte@11:00 IST present={len(s.days_with_0dte_1100)} "
            f"missing={s.days_missing_0dte_1100}",
        )
        emit(lines, "sanity:")
        for sc in s.sanities:
            flag = "PASS" if sc.ok else "FAIL"
            emit(lines, f"  [{flag}] {sc.name}: {sc.detail}")
        emit(lines)


def run(months_filter: list[str] | None) -> list[str]:
    lines: list[str] = []
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    emit(lines, "===== VERIFY MARKS (read-only) =====")
    emit(lines, f"generated_utc={datetime.now(tz=UTC).isoformat()}")
    emit(lines, f"marks_dir={MARKS_DIR}")
    emit(lines, f"months_filter={months_filter or 'ALL_ON_DISK'}")
    emit(lines)

    paths = list_shards(months_filter)
    if not paths:
        emit(lines, "ERROR: no marks_*.sqlite matched")
        return lines

    emit(lines, f"shards_found={len(paths)} {[p.name for p in paths]}")
    emit(lines)

    spot_days, spot_lo, spot_hi = load_spot_days()
    if spot_lo is not None and spot_hi is not None:
        emit(
            lines,
            f"spot_1m_days={len(spot_days)} range={spot_lo.isoformat()} .. "
            f"{spot_hi.isoformat()}",
        )
    else:
        emit(lines, "spot_1m_days=0")
    emit(lines)

    reports: list[ShardReport] = []
    for i, path in enumerate(paths, 1):
        logger.info("progress shard %d/%d %s", i, len(paths), path.name)
        rep = analyze_shard(path)
        reports.append(rep)
        logger.info(
            "done %s rows=%d syms=%d fails=%d",
            rep.ym,
            rep.n_rows,
            rep.n_symbols,
            sum(1 for s in rep.sanities if not s.ok),
        )

    format_report(lines, reports, spot_days, spot_lo, spot_hi)
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only option marks integrity check")
    ap.add_argument(
        "--months",
        type=str,
        default=None,
        help="Comma-separated YYYY-MM list (default: all shards on disk)",
    )
    args = ap.parse_args(argv)
    months_filter: list[str] | None = None
    if args.months:
        months_filter = [m.strip() for m in args.months.split(",") if m.strip()]
        for m in months_filter:
            parse_year_month(m)  # validate

    lines = run(months_filter)
    text = "\n".join(lines) + "\n"
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.flush()
    logger.info("wrote %s", OUT_PATH)
    # Exit 1 if any FAIL
    if any(line.startswith("FAIL ") for line in lines):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
