# S008 — Regime-gated 0DTE short strangle (theta harvest)

**Status:** engine build (rev 2)  
**Not** a live strategy. Backtest only under `backtest/strategies/s008_regime_gate/`.

## Idea
Gate a naked 0DTE ATM±2000 short strangle with an expanding-window stress signal
(`prev_rvol` + `overnight`). Hold to settlement — **no** intraday exit.

## Windows (hard-locked)
| | Range |
|--|-------|
| IS | 2025-07-04 .. 2026-03-31 |
| OOS | 2026-04-01 .. 2026-09-20 |

Threshold is chosen on **IS only**. `--window oos` **requires** `--threshold`
(no in-process search).

## Arms
| Arm | `--gate` | Behaviour |
|-----|----------|-----------|
| A | `none` | always SELL |
| B | `switch` | `sig >= thr` → BUY, else SELL |
| C | `flat` | `sig >= thr` → skip, else SELL |

## Settlement (critical)
Payoff uses **settlement spot** at 17:30 IST (fallback 17:29).  
**Exit fee = 0, exit slip = 0.** Intrinsic only at settle.

## Strike selection (`--strike-mode`)
Targets are derived from **entry spot**, never from the chain centre:
`target_call_K = round((spot+2000)/200)*200`, `target_put_K = round((spot-2000)/200)*200`.
Call must be `> spot`, put `< spot` — the OTM guard runs before the gap guard.

| Mode | Rule | Skip reason when unmet |
|------|------|------------------------|
| `points` | nearest strike to target, `--max-strike-gap` (default 400) | `STRIKE_UNAVAILABLE` |
| `premium` | mark closest to `--premium-target-pct` % of spot (default 0.034) | `CHAIN_ONE_SIDED` |
| `delta` | `\|delta\|` closest to `--target-delta` (default 0.12), Black-76 IV from mark | `CHAIN_ONE_SIDED` |

`--premium-target-pct` is **percent of spot**. The old 0.28571% value was the 1DTE
fee cap, roughly 8x too rich for 0DTE, and is not used.

All three modes then pass a leg premium band: if either chosen leg's mark exceeds
`--max-leg-premium-pct` % of spot (default 0.10, about 3x a normal ATM±2000 leg at
~0.034%), the basket is skipped with `LEG_PREMIUM_OUT_OF_BAND`. This catches thin
chains where the only OTM strike left sits far too close to the money — e.g.
2025-11-05, whose put mark was 0.6636% of spot.

## Stats reported per arm
`n`, mean, median, win%, worst, p5, maxDD, capital at the 3% worst-basket rule,
return%/day on that capital, and a day-clustered bootstrap 95% CI.
Gate arms additionally report a paired bootstrap on **only the days the gate
actually acted** (available days where `flat` sat out or `switch` flipped to buy);
CIs from fewer than 10 acted days are labelled untrustworthy.

## Performance
Chains are loaded once per `(expiry, entry_ts, loader)` by a `ChainCache` created
outside the arm loops, so a 36-arm grid over 196 days does ~200 chain reads, not
~7,000. Each arm prints elapsed seconds and traded count, and a `TIMING (per arm)`
block lands in the run text file.

Chain reads filter on `expiry` + `ts`. The marks files ship only
`idx_marks_expiry ON marks(expiry)`, so each read seeks that index and then does a
rowid lookup for every row of that expiry — 185k rows for 2026-05-12, 385k for
2025-11-05 — to return ~50. `backtest/tools/build_marks_index.py` adds the
composite `(expiry, ts)` index that turns this into a direct range seek; it is a
one-time write-mode script and requires a backup plus `--yes`.

## Tests
1. Look-ahead: truncate-at-T recomputes identical `sig`
2. Exit cost zero
3. Settlement timestamp is 17:29/17:30 that day
4. Strike gap guard: traded baskets within `--max-strike-gap`, targets spot-based
5. OTM only: no traded leg is ITM in any strike mode
6. Premium band: no traded leg exceeds `--max-leg-premium-pct` in any strike mode
7. OOS lock: `--window oos` rejects a missing or swept threshold
