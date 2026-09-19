# S001 Wing-Capped Strangle

Short OTM call + put (fixed $150/side or B25), long wings (~2000 pts), Adj B roll of untested side, max 2 adjustments, PCT profit target lock at entry, pre-expiry 17:15 IST.

**Status:** CLOSED (negative expectancy on IS / matrix)

## Rules

- Entry 11:00 IST, 2DTE, qty 8 lots
- Premium: `fixed` ($150) or `b25` (ATM straddle × 25%)
- Wings 2000 pts, wing_roll on
- Adj B: tested ≥100% baseline AND untested < trigger%×baseline (trigger 70)
- Max 2 adj then `MAX_ADJUSTMENTS_REACHED`
- Profit: `pct_of_credit` locked at entry (`tp_pct`)
- Costs: bucketed slippage + `s001_income_engine.option_fee`
- Skip expiry 2025-04-26

## Config (locked DESIGN arm)

```
premium_mode=fixed, tp_pct=25, slip_mult=1.0, dec=40, adj_b=70, B_only, hedge=off
```

## Parity / baseline

Implementation lives in `backtest/s001_mark_engine.py` (kept as baseline).
Harness port: `strategy.run_cycle` → `simulate_cycle`.

Expected DESIGN regression (do not hide mismatch):

| Window | Arm | n | mean/day |
|--------|-----|---|----------|
| 2025-07-01 .. 2026-09-13 | fixed_tp25_slip1 | 144 | -0.0718 |

## Open questions

- May smoke was 100% MAX_ADJUSTMENTS — is TP ever reachable under live costs?
- fixed vs b25: skip asymmetry (14 vs 23 cycles in May)

## Next steps

- None for live deploy. Use as harness reference + learnings only.
