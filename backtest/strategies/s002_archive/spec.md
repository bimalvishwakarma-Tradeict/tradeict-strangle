# S002 Archive (LSR / scan entry)

Archived from `backtest/s002_sim.py` + `s002_config.json`. Not ported to harness yet.

**Status:** CLOSED / ARCHIVE

## Rules (from s002_config.json)

- Scan start 12:00 IST, entry cutoff 15:30
- Strike offset mode=strikes value=1
- Entry max diff $10 absolute, max premium $70
- Target 40% of capital used; SL multiplier 0.5
- Max 3 trades/day, min 1 lot
- Quote max age 60s
- Trail optional (disabled in config)

## Tests done

- Historical CSV sim runs under `backtest/s002_*` (pre-harness era)

## Open questions

- Was edge real after mark-to-fill + fees, or print-data artifact?

## Next steps

- Do not rebuild as new strategy without harness KILL + OOS.
