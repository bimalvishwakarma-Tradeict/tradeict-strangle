# S005 — Tent Strategy

BTC options on Delta Exchange India. Harness-native (no live bot changes).

**Status:** TESTING

## Structure (one basket)

1. **SHORT ATM straddle** on `SHORT_DTE` (default 1)  
   ATM = nearest strike to spot; call/put premiums roughly equal.  
   qty = `QTY_STRADDLE` (default 10)

2. **SHORT strangle** same expiry at straddle **breakevens**  
   upper BE = ATM + (call_mark + put_mark), lower BE = ATM − that sum  
   Round to nearest listed strike. qty = `QTY_STRANGLE` (default 20)

3. **LONG strangle = protection** on `LONG_DTE` (default 0 = one DTE before short)  
   Same strikes as step-2 (else one step OTM). Premiums roughly matched.  
   qty = `QTY_STRADDLE + QTY_STRANGLE` (always equals total short lots)

## Entry

- Continuous: open a new basket as soon as one closes  
- After **TARGET** → immediate re-entry  
- After **STOPLOSS** → **2 hour COOLDOWN**, then entry  
- No new entry if insufficient time remains before LONG expiry **17:25 IST**

## Exit (whole basket together — never a single leg)

- **TARGET:** net MTM ≥ `TARGET_PCT` × `NET_CREDIT`  
  `NET_CREDIT` = (sum short premiums − sum long premiums) × CV, locked at entry (slipped fills)
- **STOPLOSS:** net MTM ≤ −(`SL_MULT` × `TARGET_PCT` × `NET_CREDIT`)
- **TIME_CUTOFF:** LONG expiry day **17:25 IST** — flatten all legs  
  (no short left without protection)

## Adjustment

None in this version.

## Costs

Harness **bucketed** slippage + `option_fee`. No flat slippage.

## Phase-1 variant grid (72 combos)

| Axis | Values |
|------|--------|
| expiry_pair | (1,0) \| (2,1) |
| qty_split (straddle/strangle) | 10/20 \| 15/15 \| 20/10 (protection = 30) |
| target_pct | 5% \| 10% \| 15% of net credit |
| sl_mult | 2× \| 3× \| 4× \| 5× of target |

KILL exploration window (decision later): `2026-06-01 .. 2026-08-31`  
Bootstrap: n=1000, seed **20260919**

> 3-month exploration — CI overlap wale combos ko alag mat maano
