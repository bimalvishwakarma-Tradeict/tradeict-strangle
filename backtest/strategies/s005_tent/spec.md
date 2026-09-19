# S005 — Tent Strategy (v2 Absorption Stop)

BTC options on Delta Exchange India. Harness-native (no live bot changes).

**Status:** TESTING  
**Version:** 2.0.0

## Structure (one basket)

1. **SHORT ATM straddle** on `SHORT_DTE` (default 1)  
   ATM = nearest strike to spot; call/put premiums roughly equal.  
   qty = `QTY_STRADDLE` (default 10)

2. **SHORT strangle** same expiry at straddle **breakevens**  
   upper BE = ATM + (`be_mult` × straddle premium), lower BE = ATM − that  
   (`be_mult` default 1.0). Round to nearest listed strike.  
   qty = `QTY_STRANGLE` (default 20)

3. **LONG strangle = protection**  
   - `protection_expiry=calendar` (default): expiry = `LONG_DTE`  
   - `protection_expiry=same`: same expiry as shorts (true condor)  
   Strikes = short strangle ± `protection_offset` steps OTM (default 0).  
   qty = round(`protection_ratio` × total short) — default 1.0

## Entry-drag fix (v2)

Raw MTM uses fills vs marks and subtracts entry fees, so at entry the basket
looks immediately negative by `entry_drag = -(entry_slip + entry_fees)`.
All TARGET / STOP / HARD_FLOOR comparisons use **cost-adjusted** MTM:
`adj = raw_mtm − entry_drag` (starts at ~0). `entry_drag` is logged in meta + CSV.

## Exit modes

### `credit_pct` (default — phase-1 behaviour + entry-drag fix)

- **TARGET:** adj MTM ≥ `TARGET_PCT` × `NET_CREDIT` (unless `--no-target`)
- **STOPLOSS:** adj MTM ≤ −(`SL_MULT` × `TARGET_PCT` × `NET_CREDIT`)
- **TIME_CUTOFF:** LONG expiry day at cutoff (default 17:25 IST)
- **HARD_FLOOR:** adj MTM ≤ −`max_basket_loss_usd` (off by default / `none`)

### `absorption` (v2)

Each poll tick:
1. Pick **tested_side** = call or put whose short premium sum rose more vs entry
2. `stress = tested_now / tested_entry × 100`
3. If `stress ≥ trigger_pct`:  
   `A = prot_gain / |shorts_loss|` (or 999 if shorts not losing)  
   Log every trigger `(ts, A)`  
   If `A < absorb_min` → **ABSORPTION_FAIL**
4. Always: HARD_FLOOR + TIME_CUTOFF (+ optional TARGET if not `--no-target`)

## Metrics (v2)

- **mae_usd** — true max adverse excursion (min cost-adjusted MTM over life)
- **net_theta_entry** — Black-76, IV inverted from mark; shorts θ − longs θ (USD/day)
- Per-run **`_baskets.csv`** — one row per basket with all legs + diagnostics

## Phase-1 default grid (72) — unchanged when CLI omitted

| Axis | Values |
|------|--------|
| expiry_pair | (1,0) \| (2,1) |
| qty_split | 10/20 \| 15/15 \| 20/10 |
| target_pct | 5 \| 10 \| 15 |
| sl_mult | 2 \| 3 \| 4 \| 5 |
| exit_mode | credit_pct |

### New CLI axes (optional)

`--exit-mode`, `--trigger-pcts`, `--absorb-mins`, `--max-basket-loss`, `--no-target`  
plus prior: `--tag`, `--protection-expiry`, `--protection-offset`, `--protection-ratio`,
`--be-mult`, `--cutoff-times`, `--cooldowns`, …

Bootstrap: n=1000, seed **20260919**

> 3-month exploration — CI overlap wale combos ko alag mat maano
