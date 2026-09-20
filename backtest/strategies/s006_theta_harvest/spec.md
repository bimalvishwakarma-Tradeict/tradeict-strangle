# S006 — Daily Theta Harvesting

BTC options on Delta Exchange India. Harness-native (no live bot changes).

**Status:** TESTING  
**Version:** 1.0.0

## Structure (one basket / day)

Entry **09:00 IST** each day:

1. **SHORT `short_dte` strangle** (default 1; grid via `--short-dtes`)  
   Target strikes = ATM ± `short_offset` (default 2000).  
   Premium-match within ±2 listed strikes. qty = `qty_short` (default 100) per side.

2. **LONG 0DTE protection** at short strikes ± `protection_offset` (default 0).  
   qty = `protection_ratio` × `qty_short` per side (grid 2/3/4/5).
   Protection expiry is always 0DTE regardless of short DTE.

## Exit (whole basket; never leave a naked short)

- **TARGET:** cost-adjusted MTM ≥ `target_pct` × `NET_CREDIT`  
  `NET_CREDIT` locked at entry from slipped fills; comparisons use `mtm − entry_drag`.
- **STOPLOSS:** cost-adjusted MTM ≤ −(`max_dd_pct`/100 × $100)  
  `max_dd_pct` ∈ {10, 20, none}.
- **TIME_CUTOFF:** 17:29 IST (configurable).

### Protection at cutoff (`expire_protection_at_cutoff`, default on)

- TIME_CUTOFF: close shorts normally; **expire** 0DTE longs at 17:30 intrinsic  
  (exit fee = 0, exit slip = 0).
- TARGET / STOPLOSS (intraday): sell protection at market (full exit costs).

## Costs

Harness bucketed slippage + `option_fee`. No new cost model.

## Outputs

- `*_baskets.csv` — one row per basket (4 legs + diagnostics)
- `*_intraday.csv` — hourly MTM snapshots 09:00 → exit (terminal collapse visibility)

## Default grid

| Axis | Values |
|------|--------|
| short_dte | 1 (default; CLI `--short-dtes 1,2`) |
| protection_ratio | 2, 3, 4, 5 |
| target_pct | 10..70 step 10 |
| max_dd_pct | 10, 20, none |
| qty_short | 100 |
| short_offset | 2000 |
| protection_offset | 0 |
| cutoff | 17:29 |

Default (sdte=1 only) = 4 × 7 × 3 = **84** combos.  
Report shows **mean_net_credit** and **mean_target_usd** (absolute $) so DTE arms are comparable.

Bootstrap: n=1000, seed **20260919**
