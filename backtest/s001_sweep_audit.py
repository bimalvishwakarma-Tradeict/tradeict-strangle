#!/usr/bin/env python3
"""
S001 sweep distribution audit — read-only summary of the 2112-config grid.

No new simulation, no re-bootstrap. Reads backtest/results/s001_sweep_configs.csv
(or builds it once by parsing the FULL-sample table in s001_income_engine_latest.txt).

Parameter columns in this income-engine sweep (confirmed from report cfg strings):
  time_of_day, dte, strike_mode, wing, fill_package
adjustment_setting is always 'none' (income engine excludes adjustments).
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

_BACKTEST = Path(__file__).resolve().parent
RESULTS_DIR = _BACKTEST / "results"
CSV_PATH = RESULTS_DIR / "s001_sweep_configs.csv"
REPORT_PATH = RESULTS_DIR / "s001_income_engine_latest.txt"
AUDIT_OUT = RESULTS_DIR / "s001_sweep_audit.txt"

# Section 1 FULL sample table (not prints-only)
SECTION_START = "1. ALL CONFIGS ranked by SORTINO (no settle fee, FULL sample"
CFG_RE = re.compile(
    r"t=(?P<tod>\d{2}:\d{2})\s+dte=(?P<dte>\d+)\s+strike=(?P<strike>\S+)\s+"
    r"wing=(?P<wing>\S+)\s+fill=(?P<fill>\S+)\s*$"
)
# rank n mean med ci_lo ci_hi p5 worst date sortino mdd surf% cfg...
ROW_RE = re.compile(
    r"^\s*\d+\s+"
    r"(?P<n>\d+)\s+"
    r"(?P<mean>-?\d+\.\d+)\s+"
    r"(?P<med>-?\d+\.\d+)\s+"
    r"(?P<ci_lo>-?\d+\.\d+)\s+"
    r"(?P<ci_hi>-?\d+\.\d+)\s+"
    r"(?P<p5>-?\d+\.\d+)\s+"
    r"(?P<worst>-?\d+\.\d+)\s+"
    r"(?P<wdate>\S+)\s+"
    r"(?P<sortino>nan|-?\d+\.\d+)\s+"
    r"(?P<mdd>-?\d+\.\d+)\s+"
    r"(?P<surf>-?\d+\.\d+)%\s+"
    r"(?P<cfg>.+)$"
)


def emit(lines: list[str], line: str = "") -> None:
    lines.append(line)


def parse_report_to_rows(report: Path) -> list[dict[str, str]]:
    raw = report.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    lines = text.splitlines()
    in_section = False
    rows: list[dict[str, str]] = []
    for line in lines:
        if SECTION_START in line:
            in_section = True
            continue
        if in_section and line.startswith("=====") and rows:
            break
        if in_section and line.startswith("1b."):
            break
        if not in_section:
            continue
        if line.strip().startswith("rank") or not line.strip():
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        cfg_m = CFG_RE.search(m.group("cfg").strip())
        if not cfg_m:
            raise ValueError(f"Could not parse cfg: {m.group('cfg')!r}")
        rows.append(
            {
                "time_of_day": cfg_m.group("tod"),
                "dte": cfg_m.group("dte"),
                "strike_mode": cfg_m.group("strike"),
                "adjustment_setting": "none",
                "wing": cfg_m.group("wing"),
                "fill_package": cfg_m.group("fill"),
                "n": m.group("n"),
                "mean": m.group("mean"),
                "median": m.group("med"),
                "ci_lo": m.group("ci_lo"),
                "ci_hi": m.group("ci_hi"),
                "p5": m.group("p5"),
                "worst": m.group("worst"),
                "worst_date": m.group("wdate"),
                "sortino_per_cycle": m.group("sortino"),
                "mdd": m.group("mdd"),
                "surf_wing_pct": m.group("surf"),
            }
        )
    return rows


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    if not rows:
        raise RuntimeError("No rows to write")
    fields = list(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def load_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def ensure_csv() -> Path:
    if CSV_PATH.is_file():
        return CSV_PATH
    if not REPORT_PATH.is_file():
        raise FileNotFoundError(
            f"Neither {CSV_PATH} nor {REPORT_PATH} found. "
            "Run s001_income_engine.py or s001_income_decisive.py first."
        )
    rows = parse_report_to_rows(REPORT_PATH)
    if len(rows) != 2112:
        raise RuntimeError(
            f"Expected 2112 configs in FULL table, got {len(rows)} from {REPORT_PATH}"
        )
    write_csv(rows, CSV_PATH)
    return CSV_PATH


def as_float(x: str) -> float:
    return float(x)


def as_int(x: str) -> int:
    return int(x)


def pctile(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(math.floor(k))
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def mean_distribution_block(title: str, rows: list[dict[str, str]], out: list[str]) -> None:
    emit(out, title)
    means = [as_float(r["mean"]) for r in rows]
    n = len(means)
    if n == 0:
        emit(out, "  (empty)")
        emit(out)
        return
    n_pos = sum(1 for m in means if m > 0)
    n_neg = sum(1 for m in means if m < 0)
    n_zero = n - n_pos - n_neg
    std = statistics.stdev(means) if n >= 2 else 0.0
    emit(out, f"  count              = {n}")
    emit(out, f"  mean_of_means      = {statistics.mean(means):.6f}")
    emit(out, f"  median             = {statistics.median(means):.6f}")
    emit(out, f"  std                = {std:.6f}")
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99):
        emit(out, f"  p{p:<2}                = {pctile(means, float(p)):.6f}")
    emit(out, f"  mean > 0           = {n_pos}  ({100.0 * n_pos / n:.2f}%)")
    emit(out, f"  mean < 0           = {n_neg}  ({100.0 * n_neg / n:.2f}%)")
    emit(out, f"  mean == 0          = {n_zero}  ({100.0 * n_zero / n:.2f}%)")
    emit(out)


def marginal_table(
    dim: str,
    all_rows: list[dict[str, str]],
    clear_rows: list[dict[str, str]],
    out: list[str],
) -> None:
    n_all = len(all_rows)
    n_clear = len(clear_rows)
    emit(out, f"Dimension: {dim}  (n_clear={n_clear}, n_all={n_all})")
    emit(
        out,
        f"  {'level':<16} {'in_12':>6} {'in_2112':>8} {'expected':>10} {'ratio':>8}",
    )
    counts_all = Counter(r[dim] for r in all_rows)
    counts_clear = Counter(r[dim] for r in clear_rows)
    levels = sorted(counts_all.keys(), key=lambda x: (str(type(x)), x))
    for level in levels:
        c_all = counts_all[level]
        c_cl = counts_clear.get(level, 0)
        expected = n_clear * (c_all / n_all) if n_all else 0.0
        ratio = (c_cl / expected) if expected > 1e-12 else float("nan")
        ratio_s = f"{ratio:.3f}" if math.isfinite(ratio) else "nan"
        emit(
            out,
            f"  {str(level):<16} {c_cl:6d} {c_all:8d} {expected:10.3f} {ratio_s:>8}",
        )
    emit(out)


def build_audit(rows: list[dict[str, str]]) -> str:
    out: list[str] = []
    emit(out, "=== S001 SWEEP DISTRIBUTION AUDIT ===")
    emit(out, f"source_csv: {CSV_PATH}")
    emit(out, f"n_configs: {len(rows)}")
    emit(
        out,
        "columns: "
        + ", ".join(
            [
                "time_of_day",
                "dte",
                "strike_mode",
                "adjustment_setting",
                "wing",
                "fill_package",
                "n",
                "mean",
                "ci_lo",
                "ci_hi",
                "sortino_per_cycle",
            ]
        ),
    )
    emit(
        out,
        "note: adjustment_setting is always 'none' - income-engine sweep has no adjustments.",
    )
    emit(out)

    # Section 1
    emit(out, "--- SECTION 1: ALL CONFIGS - MEAN DISTRIBUTION ---")
    mean_distribution_block("ALL configs:", rows, out)
    maker = [r for r in rows if r["fill_package"] == "maker"]
    mean_distribution_block("MAKER-only subset:", maker, out)
    n300 = [r for r in rows if as_int(r["n"]) >= 300]
    mean_distribution_block("n>=300 subset:", n300, out)

    # Section 2
    emit(out, "--- SECTION 2: THE CI-CLEARING CONFIGS (ci_lo > 0) ---")
    clear = [r for r in rows if as_float(r["ci_lo"]) > 0]
    clear_sorted = sorted(clear, key=lambda r: -as_float(r["mean"]))
    emit(out, f"count with ci_lo > 0: {len(clear_sorted)}")
    emit(
        out,
        f"{'tod':>5} {'dte':>3} {'strike':>7} {'adj':>4} {'wing':>5} {'fill':>6} "
        f"{'n':>4} {'mean':>8} {'ci_lo':>8} {'ci_hi':>8} {'sortino':>8}",
    )
    for r in clear_sorted:
        emit(
            out,
            f"{r['time_of_day']:>5} {r['dte']:>3} {r['strike_mode']:>7} "
            f"{r['adjustment_setting']:>4} {r['wing']:>5} {r['fill_package']:>6} "
            f"{r['n']:>4} {as_float(r['mean']):8.4f} {as_float(r['ci_lo']):8.4f} "
            f"{as_float(r['ci_hi']):8.4f} {r['sortino_per_cycle']:>8}",
        )
    emit(out)

    # Section 3
    emit(out, "--- SECTION 3: MARGINAL ENRICHMENT (among ci_lo>0 vs all) ---")
    emit(
        out,
        "expected_in_12_if_random = n_clear * (count_in_all / n_all); "
        "ratio = in_clear / expected",
    )
    emit(out)
    for dim in (
        "dte",
        "time_of_day",
        "strike_mode",
        "adjustment_setting",
        "wing",
        "fill_package",
    ):
        marginal_table(dim, rows, clear_sorted, out)

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S001 sweep distribution audit")
    ap.add_argument(
        "--rebuild-csv-from-report",
        action="store_true",
        help="Force re-parse s001_income_engine_latest.txt into CSV",
    )
    args = ap.parse_args(argv)

    if args.rebuild_csv_from_report and CSV_PATH.is_file():
        CSV_PATH.unlink()

    csv_path = ensure_csv()
    rows = load_rows(csv_path)
    if len(rows) != 2112:
        # Allow if CSV was rebuilt from decisive with same grid
        sys.stderr.write(
            f"WARNING: expected 2112 rows, got {len(rows)} from {csv_path}\n"
        )

    text = build_audit(rows)
    AUDIT_OUT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_OUT.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    sys.stdout.write(f"\nWrote {AUDIT_OUT}\n")
    sys.stdout.write(f"CSV {csv_path} rows={len(rows)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
