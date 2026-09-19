# S004 Trend Rider

Signal-agnostic / trend-rider cost gate on marks. Gate failed — parked.

**Status:** PARKED

## Rules

- Directional option entries gated by cost model (bucketed slip)
- Parity mode vs historical prints (`s004_parity`, `s004_gate`)

## Tests done

- `s004_gate` flat165 regression (matched)
- Parity guards for empty points
- Signal sweep scripts (no auto run required)

## Open questions

- Can a different DTE/premium bucket clear the gate with slip_mult stress?

## Next steps

- Stay PARKED until cost gate passes with slip_mult≥1.5 on KILL window.
