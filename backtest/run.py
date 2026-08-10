# run.py — CLI backtest runner with self-contained HTML report

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

# Ensure project root is on path when run as `python backtest/run.py`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backtest.engine import BacktestEngine, day_result_to_dict, summary_for_api


DEFAULTS = {
    "data_dir": "backtest/data",
    "trade_type": "strangle",
    "expiry_type": "1DTE",
    "quantity": 100,
    "entry_hour_ist": 17,
    "entry_minute_ist": 32,
    "trigger_pct": 160.0,
    "profit_target_pct": 25.0,
    "stoploss_pct": 50.0,
    "min_replacement_premium": 150.0,
    "conversion_equality_pct": 10.0,
    "target_premium_per_side": 250.0,
    "fee_per_leg_usd": 0.75,
    "slippage_pct": 2.0,
    "mode": "continuous",
}


def _parse_entry_time(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("entry-time must be HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError("entry-time out of range")
    return hour, minute


def build_config(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(
        description="Run local short-straddle/strangle backtest and open HTML report"
    )
    parser.add_argument("--config", type=str, default=None, help="JSON config file")
    parser.add_argument("--data-dir", type=str, default=None)
    parser.add_argument("--trade-type", choices=["straddle", "strangle"], default=None)
    parser.add_argument("--expiry", dest="expiry_type", choices=["0DTE", "1DTE"], default=None)
    parser.add_argument("--quantity", type=int, default=None)
    parser.add_argument("--trigger", dest="trigger_pct", type=float, default=None)
    parser.add_argument("--tp", dest="profit_target_pct", type=float, default=None)
    parser.add_argument("--sl", dest="stoploss_pct", type=float, default=None)
    parser.add_argument("--min-premium", dest="min_replacement_premium", type=float, default=None)
    parser.add_argument("--conv-eq", dest="conversion_equality_pct", type=float, default=None)
    parser.add_argument("--target-premium", dest="target_premium_per_side", type=float, default=None)
    parser.add_argument("--fee", dest="fee_per_leg_usd", type=float, default=None)
    parser.add_argument("--slippage", dest="slippage_pct", type=float, default=None)
    parser.add_argument("--entry-time", type=str, default=None, help="IST HH:MM")
    parser.add_argument(
        "--mode",
        choices=["simple", "continuous"],
        default=None,
        help="simple=one basket/day; continuous=re-enter 2m after exit (default)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open the HTML report in a browser",
    )
    args = parser.parse_args(argv)

    cfg = dict(DEFAULTS)
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            file_cfg = json.load(f)
        if not isinstance(file_cfg, dict):
            raise SystemExit("Config JSON must be an object")
        cfg.update(file_cfg)

    cli_map = {
        "data_dir": args.data_dir,
        "trade_type": args.trade_type,
        "expiry_type": args.expiry_type,
        "quantity": args.quantity,
        "trigger_pct": args.trigger_pct,
        "profit_target_pct": args.profit_target_pct,
        "stoploss_pct": args.stoploss_pct,
        "min_replacement_premium": args.min_replacement_premium,
        "conversion_equality_pct": args.conversion_equality_pct,
        "target_premium_per_side": args.target_premium_per_side,
        "fee_per_leg_usd": args.fee_per_leg_usd,
        "slippage_pct": args.slippage_pct,
        "mode": args.mode,
    }
    for key, val in cli_map.items():
        if val is not None:
            cfg[key] = val

    if args.entry_time:
        hour, minute = _parse_entry_time(args.entry_time)
        cfg["entry_hour_ist"] = hour
        cfg["entry_minute_ist"] = minute

    cfg["_no_open"] = bool(args.no_open)
    return cfg


def _esc(value: object) -> str:
    return html.escape(str(value))


def _money(v: float, signed: bool = False) -> str:
    n = float(v or 0)
    abs_s = f"{abs(n):,.2f}"
    if signed:
        if n > 0:
            return f"+${abs_s}"
        if n < 0:
            return f"-${abs_s}"
    return f"${abs_s}"


def generate_html_report(
    config: dict,
    day_dicts: list[dict],
    summary: dict,
) -> str:
    """Build a self-contained HTML report (Chart.js CDN only)."""
    api_summary = summary_for_api(summary)
    dates = api_summary.get("daily_dates") or []
    pnls = [float(x) for x in (api_summary.get("daily_pnl") or [])]
    colors = ["#22c55e" if p >= 0 else "#ef4444" for p in pnls]

    exit_counts = api_summary.get("exit_counts") or {}
    pt = int(exit_counts.get("PROFIT_TARGET") or 0)
    sl = int(exit_counts.get("STOPLOSS") or 0)
    pe = int(exit_counts.get("PRE_EXPIRY") or 0)
    exit_total = max(pt + sl + pe, 1)

    def bar_pct(n: int) -> float:
        return 100.0 * n / exit_total

    config_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in sorted(config.items())
        if not str(k).startswith("_")
    )

    day_rows_html: list[str] = []
    for d in day_dicts:
        pnl = float(d.get("net_pnl") or 0)
        pnl_cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "")
        if d.get("put_strike") != d.get("call_strike"):
            strike = f"{d.get('call_strike')} / {d.get('put_strike')}"
        else:
            strike = d.get("call_strike")
        prem = float(d.get("initial_premium") or 0)
        exit_label = str(d.get("exit_reason") or "")
        if d.get("exit_time"):
            exit_label += f" @ {d.get('exit_time')}"
        day_rows_html.append(
            "<tr>"
            f"<td>{_esc(d.get('trade_date'))}</td>"
            f"<td>{_esc(strike)}</td>"
            f"<td>{_esc(f'{prem:.0f}')}</td>"
            f"<td>{_esc(exit_label)}</td>"
            f"<td>{_esc(d.get('adjustments'))}</td>"
            f"<td class='{pnl_cls}'>{_esc(_money(pnl, True))}</td>"
            f"<td class='neg'>{_esc(_money(float(d.get('max_drawdown') or 0), True))}</td>"
            "</tr>"
        )

    adj_rows_html: list[str] = []
    for d in day_dicts:
        for adj in d.get("adj_log") or []:
            old_s = float(adj.get("old_strike") or 0)
            new_s = float(adj.get("new_strike") or 0)
            old_p = float(adj.get("old_premium") or 0)
            new_p = float(adj.get("new_premium") or 0)
            trig = float(adj.get("trigger_pct_reached") or 0)
            mtm = float(adj.get("net_pnl_at_adj") or 0)
            adj_rows_html.append(
                "<tr>"
                f"<td>{_esc(d.get('trade_date'))}</td>"
                f"<td>{_esc(adj.get('time') or adj.get('minute'))}</td>"
                f"<td>{_esc(adj.get('adj_type'))}</td>"
                f"<td>{_esc(str(adj.get('triggered_leg') or '').upper())}</td>"
                f"<td>{_esc(f'{old_s:.0f}→{new_s:.0f}')}</td>"
                f"<td>{_esc(f'{old_p:.0f}→{new_p:.0f}')}</td>"
                f"<td>{_esc(f'{trig:.0f}')}%</td>"
                f"<td>{_esc(_money(mtm, True))}</td>"
                "</tr>"
            )

    chart_labels = json.dumps(dates)
    chart_values = json.dumps(pnls)
    chart_colors = json.dumps(colors)

    net = float(api_summary.get("total_net_pnl") or 0)
    net_cls = "pos" if net > 0 else ("neg" if net < 0 else "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Backtest Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; font-family: ui-sans-serif, system-ui, Segoe UI, sans-serif;
    background: #0f172a; color: #e2e8f0; line-height: 1.45;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
  h1 {{ margin: 0 0 8px; font-size: 1.6rem; }}
  h2 {{ margin: 28px 0 12px; font-size: 1.1rem; color: #93c5fd; }}
  .muted {{ color: #94a3b8; font-size: 0.9rem; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 12px; }}
  .card .label {{ font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; }}
  .card .value {{ font-size: 1.25rem; font-weight: 700; margin-top: 4px; }}
  .pos {{ color: #4ade80; }}
  .neg {{ color: #f87171; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #334155; text-align: left; }}
  th {{ color: #94a3b8; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; }}
  .panel {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 14px; overflow-x: auto; }}
  .bar-row {{ margin: 8px 0; }}
  .bar-label {{ display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px; }}
  .bar-track {{ height: 8px; background: #334155; border-radius: 999px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 999px; }}
  .chart-box {{ height: 320px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Backtest Report</h1>
  <p class="muted">Generated { _esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S')) }</p>

  <h2>Config</h2>
  <div class="panel"><table><tbody>{config_rows}</tbody></table></div>

  <h2>Summary</h2>
  <div class="cards">
    <div class="card"><div class="label">Win Rate</div><div class="value">{_esc(f"{float(api_summary.get('win_rate') or 0):.1f}")}%</div></div>
    <div class="card"><div class="label">Net P&amp;L</div><div class="value {net_cls}">{_esc(_money(net, True))}</div></div>
    <div class="card"><div class="label">Avg Win</div><div class="value pos">{_esc(_money(float(api_summary.get('avg_win') or 0), True))}</div></div>
    <div class="card"><div class="label">Avg Loss</div><div class="value neg">{_esc(_money(float(api_summary.get('avg_loss') or 0), True))}</div></div>
    <div class="card"><div class="label">Max Drawdown</div><div class="value neg">{_esc(_money(float(api_summary.get('max_drawdown') or 0), True))}</div></div>
    <div class="card"><div class="label">Adjustments</div><div class="value">{_esc(api_summary.get('total_adjustments'))}</div></div>
    <div class="card"><div class="label">Conversions</div><div class="value">{_esc(api_summary.get('total_conversions'))}</div></div>
    <div class="card"><div class="label">Valid Days</div><div class="value">{_esc(api_summary.get('data_ok_days'))}/{_esc(api_summary.get('total_days'))}</div></div>
  </div>

  <h2>Exit Reasons</h2>
  <div class="panel">
    <div class="bar-row">
      <div class="bar-label"><span>Profit Target</span><span>{pt} ({bar_pct(pt):.0f}%)</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:{bar_pct(pt):.1f}%;background:#22c55e"></div></div>
    </div>
    <div class="bar-row">
      <div class="bar-label"><span>Stop Loss</span><span>{sl} ({bar_pct(sl):.0f}%)</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:{bar_pct(sl):.1f}%;background:#ef4444"></div></div>
    </div>
    <div class="bar-row">
      <div class="bar-label"><span>Pre-Expiry</span><span>{pe} ({bar_pct(pe):.0f}%)</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:{bar_pct(pe):.1f}%;background:#f59e0b"></div></div>
    </div>
  </div>

  <h2>Daily Net P&amp;L</h2>
  <div class="panel chart-box"><canvas id="pnlChart"></canvas></div>

  <h2>Day-by-day Results</h2>
  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>Date</th><th>Strike</th><th>Entry Prem</th><th>Exit</th>
          <th>Adj</th><th>Net P&amp;L</th><th>Max DD</th>
        </tr>
      </thead>
      <tbody>
        {''.join(day_rows_html) if day_rows_html else '<tr><td colspan="7">No days</td></tr>'}
      </tbody>
    </table>
  </div>

  <h2>Adjustment Events</h2>
  <div class="panel">
    <table>
      <thead>
        <tr>
          <th>Date</th><th>Time</th><th>Type</th><th>Triggered</th>
          <th>Strike</th><th>Premium</th><th>Trigger%</th><th>Net MTM</th>
        </tr>
      </thead>
      <tbody>
        {''.join(adj_rows_html) if adj_rows_html else '<tr><td colspan="8">No adjustments</td></tr>'}
      </tbody>
    </table>
  </div>
</div>
<script>
const labels = {chart_labels};
const values = {chart_values};
const colors = {chart_colors};
new Chart(document.getElementById('pnlChart'), {{
  type: 'bar',
  data: {{
    labels,
    datasets: [{{
      label: 'Net P&L',
      data: values,
      backgroundColor: colors,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8', maxRotation: 60, minRotation: 0 }}, grid: {{ color: '#334155' }} }},
      y: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }}
    }}
  }}
}});
</script>
</body>
</html>
"""


def print_summary(summary: dict) -> None:
    api = summary_for_api(summary)
    print("\n========== SUMMARY ==========")
    if summary.get("error"):
        print(f"Error: {summary['error']}")
        return
    print(f"Days:        {api['data_ok_days']}/{api['total_days']}")
    print(f"Win rate:    {api['win_rate']:.1f}%")
    print(f"Net P&L:     ${api['total_net_pnl']:+.2f}")
    print(f"Avg win:     ${api['avg_win']:+.2f}")
    print(f"Avg loss:    ${api['avg_loss']:+.2f}")
    print(f"Max DD:      ${api['max_drawdown']:+.2f}")
    print(f"Adjustments: {api['total_adjustments']}")
    print(f"Conversions: {api['total_conversions']}")
    print(f"Exits:       {api.get('exit_counts')}")
    print("=============================\n")


def main(argv: list[str] | None = None) -> int:
    cfg = build_config(argv)
    data_dir = str(cfg.get("data_dir") or "backtest/data")
    no_open = bool(cfg.pop("_no_open", False))

    print("Config:")
    for k, v in sorted(cfg.items()):
        print(f"  {k}: {v}")
    print()

    engine = BacktestEngine(cfg)
    mode = str(cfg.get("mode") or "continuous").strip().lower()

    def on_progress(current: int, total: int | None, date_str: str) -> None:
        if total:
            print(f"Processing {date_str}... ({current}/{total})")
        else:
            print(f"Basket {current}: starting {date_str}...")

    if mode == "simple":
        results, summary = engine.run(data_dir, progress_callback=on_progress)
    else:
        results, summary = engine.run_continuous(
            data_dir, progress_callback=on_progress
        )
    print_summary(summary)

    day_dicts = [day_result_to_dict(r) for r in results]
    report_html = generate_html_report(cfg, day_dicts, summary)

    out_dir = Path("backtest/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"report_{stamp}.html"
    report_path.write_text(report_html, encoding="utf-8")
    abs_path = report_path.resolve()
    print(f"Report saved: {abs_path}")

    if not no_open:
        webbrowser.open(abs_path.as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
