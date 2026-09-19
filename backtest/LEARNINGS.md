# Tradeict Backtest Learnings

Global file — new strategies must read this before DESIGN tuning.

## 2026-09-19 (harness bootstrap)

- [S001] May 2026: 100% exits MAX_ADJUSTMENTS — TP/path tests must be unit-proven separately.
- [S001] fixed $150 enters fewer days than b25; always report skip accounting with mean/day.
- [S001] slip_mult 1.5 worsens mean/day ~5% — model understates live market-order cost.
- [S001] Do not tune on best-of-16 matrix; final selection only on OOS.
- [S002] Old CSV print backtests overstated fills — always calibrate mark-to-fill before claiming edge.
- [S003] Perfect-signal upper bounds still fail cost gates often — signal quality ≠ tradable edge.
- [S003] Always compare observed win rate to breakeven win rate from width/credit.
- [S004] Live fills in 300–600 bucket ~1.70% vs model ~0.75% — always stress slip_mult.
- [S004] Empty parity points must hard-fail, not silently score zero.
- Harness stages are fixed: KILL (same 3 months for all) → DESIGN (IS) → CONFIRM (OOS once).
- Never modify `backend/strategies` or `backend/engine` from backtest work.
