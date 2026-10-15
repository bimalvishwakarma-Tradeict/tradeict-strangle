# Strategy Research Protocol — implementer edition (MANDATORY)

Adopted 2026-10-15 by Bimal. Applies to every backtest, research, optimization and validation task
under backtest/** and chart_app/strategies/**. Full directive: Claude project doc
claude/STRATEGY_RESEARCH_PROTOCOL.md (Claude designs and verifies; Cursor implements; Bimal runs).

## 1. Baseline first, immutable
- Implement the user's rules EXACTLY as the baseline. Never "improve" them silently.
- Every optimization variant is a separate, named arm/flag; the baseline must stay reproducible
  bit-for-bit with its commit hash and parameters.

## 2. Controlled experiments
- One logical change per experiment unless an ablation design is requested.
- When variants are combined, the code must allow switching each component on/off (ablation).
- Every run records: commit hash, parameters, data period, cost assumptions, number of cells/configs
  evaluated.

## 3. Integrity (non-negotiable)
- No look-ahead: signals only from CLOSED bars; entry/trigger price from the same timestamp as the
  trigger; add a truncation test (data cut at T must give identical signals up to T).
- Every price is tagged by source (real / stale / parity / none). Never let "> 0" stand in for
  "present"; never silently forward-fill a leg; report shares.
- Cash conservation test on every exit path (target, SL, expiry settle, after adjustments).
- A variant that changes net credit, qty or leg count gets its OWN run — never derived by removing legs.
- Matched controls for every signal: random entry (same hour/month, same count) and, where relevant,
  volatility-matched random. Report them side by side with the signal arm.
- Respect the train/test split given in the task: out-of-sample months are run ONCE per candidate and
  never used to choose parameters.

## 4. Costs
- Delta option fee per fill = min(0.01% notional, 3.5% premium) x 1.18 GST.
- Slippage is modelled and reported SEPARATELY from fees. Report gross, fees, slippage, net.

## 5. Metrics (never win rate alone)
- Per cell: n, mean, median, worst, p5, win%, sum, maxDD, profit factor, avg costs, exits by reason,
  and the count of skipped events with reasons.
- Loss anatomy: share of losing trades that were in profit first (peak > 0), median peak.

## 6. Persistence
- Every run goes through RunArchive into its own timestamped folder with an Excel file and trade-level,
  leg-level and Greeks/IV data from real marks. Never overwrite a run.

## 7. Honesty
- Never report a test, run or number that was not actually produced. If a step could not run, say so.
- Paste real pytest output and real summary text; label anything estimated as an estimate.

## 8. Scope
- Never modify backend/ (live bot), .env, or production settings as part of research.
- Never change a strategy's core construction without an explicit instruction naming the change.
