# S001 reverify — locked OOS + IS (run in EXTERNAL PowerShell, not Cursor)
# After both finish, run the P1-P5 gate at the bottom.

$ErrorActionPreference = "Stop"
Set-Location "D:\Tradeict Short (final)\New Setup\Tradeict Short Strangle\trading-bot"

Write-Host "=== OOS locked 2024-09-16 .. 2025-06-30 ===" -ForegroundColor Cyan
python -m backtest.s001_mark_engine `
  --start 2024-09-16 --end 2025-06-30 `
  --config locked `
  --premium-mode b25 --profit-mode cost_k --wing-roll on `
  --slip-model bucketed `
  --skip-dates 2025-04-26 `
  --tag s001_oos_locked `
  --with-baseline

Write-Host "=== IS locked 2025-07-01 .. 2026-09-13 ===" -ForegroundColor Cyan
python -m backtest.s001_mark_engine `
  --start 2025-07-01 --end 2026-09-13 `
  --config locked `
  --premium-mode b25 --profit-mode cost_k --wing-roll on `
  --slip-model bucketed `
  --tag s001_is_locked

Write-Host "=== P1-P5 gate ===" -ForegroundColor Cyan
python -m backtest.s001_p1_p5_gate `
  --oos-csv backtest/results/s001_oos_locked_cycles.csv `
  --is-csv  backtest/results/s001_is_locked_cycles.csv `
  --oos-baseline-csv backtest/results/s001_oos_locked_baseline_cycles.csv `
  --out backtest/results/s001_p1_p5_gate.txt

Write-Host "Done. CSVs under backtest/results/s001_*_cycles.csv" -ForegroundColor Green
