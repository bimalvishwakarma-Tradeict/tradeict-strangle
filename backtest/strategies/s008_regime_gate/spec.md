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

## Tests
1. Look-ahead: truncate-at-T recomputes identical `sig`
2. Exit cost zero
3. Settlement timestamp is 17:29/17:30 that day
