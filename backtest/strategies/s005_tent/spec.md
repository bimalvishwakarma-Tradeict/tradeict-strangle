# S005 — Tent Strategy

BTC options on Delta Exchange India. Harness-native (no live bot changes).

**Status:** TESTING

## Structure (one basket)

1. **SHORT ATM straddle** on `SHORT_DTE` (default 1)  
   ATM = nearest strike to spot; call/put premiums roughly equal.  
   qty = `QTY_STRADDLE` (default 10)

2. **SHORT strangle** same expiry at straddle **breakevens**  
   upper BE = ATM + (`be_mult` × straddle premium), lower BE = ATM − that  
   (`be_mult` default 1.0). Round to nearest listed strike.  
   qty = `QTY_STRANGLE` (default 20)

3. **LONG strangle = protection**  
   - `protection_expiry=calendar` (default): expiry = `LONG_DTE` (one DTE before short)  
   - `protection_expiry=same`: same expiry as shorts (true condor, no calendar)  
   Strikes = short strangle ± `protection_offset` steps OTM (default 0; else one more step).  
   Premiums roughly matched.  
   qty = round(`protection_ratio` × total short) — default ratio 1.0 (= total short)

## Entry

- Continuous: open a new basket as soon as one closes  
- After **TARGET** → immediate re-entry  
- After **STOPLOSS** → `cooldown_hours` COOLDOWN (default **2**), then entry  
- No new entry if insufficient time remains before LONG expiry **cutoff** (default **17:25 IST**)

## Exit (whole basket together — never a single leg)

- **TARGET:** net MTM ≥ `TARGET_PCT` × `NET_CREDIT`  
  `NET_CREDIT` = (sum short premiums − sum long premiums) × CV, locked at entry (slipped fills)
- **STOPLOSS:** net MTM ≤ −(`SL_MULT` × `TARGET_PCT` × `NET_CREDIT`)
- **TIME_CUTOFF:** LONG expiry day at configured cutoff (default **17:25 IST**) — flatten all legs  
  (no short left without protection)

## Adjustment

None in this version.

## Costs

Harness **bucketed** slippage + `option_fee`. No flat slippage.

## Phase-1 variant grid (72 combos) — defaults when CLI omitted

| Axis | Values |
|------|--------|
| expiry_pair | (1,0) \| (2,1) |
| qty_split (straddle/strangle) | 10/20 \| 15/15 \| 20/10 (protection = 30) |
| target_pct | 5% \| 10% \| 15% of net credit |
| sl_mult | 2× \| 3× \| 4× \| 5× of target |

New optional axes (CLI; defaults preserve phase-1 behaviour):

| Axis | Flag | Default |
|------|------|---------|
| protection_expiry | `--protection-expiry` | calendar |
| protection_offset | `--protection-offset` | 0 |
| protection_ratio | `--protection-ratio` | 1.0 |
| be_mult | `--be-mult` | 1.0 |
| cutoff_times | `--cutoff-times` | 17:25 |
| cooldowns | `--cooldowns` | 2 |
| tag | `--tag` | (none; output `S005_<stage>_...`) |

Grid = cross product of all provided lists. Combo count printed at start.

KILL exploration window (decision later): `2026-06-01 .. 2026-08-31`  
Bootstrap: n=1000, seed **20260919**

## Report extras

- TIME_CUTOFF % + mean net (exits where SL never fired)
- Worst 5 baskets (date, reason, net)
- Mean shorts PnL vs mean protection PnL (how expensive is the hedge)

> 3-month exploration — CI overlap wale combos ko alag mat maano
