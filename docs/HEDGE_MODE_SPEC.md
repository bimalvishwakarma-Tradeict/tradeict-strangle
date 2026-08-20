# Hedge Mode — Design Spec & Implementation Plan
## Delta Exchange India Short Strangle Bot (Tradeict)
### Strategy S002: Long straddle core + theta-sized short income overlay
### Status: DESIGN — not implemented. Read PART 0 before building anything.

---

# PART 0 — OPEN DECISIONS (must be answered before coding)

| # | Decision | Status |
|---|---|---|
| D1 | Theta base | **RESOLVED** — use the long straddle's TOTAL theta (both legs summed, ~82), multiplied by `theta_multiplier`. |
| D2 | Hedge quantity vs short quantity | **RESOLVED** — 1:1. The whole 4-leg structure is sized as one unit per lot. |
| D3 | Slave that cannot afford the hedge | **RESOLVED by the new sizing model** — slaves fund in multiples of `capital_per_lot`. A slave funding less than 1 lot simply gets 0 lots and is skipped with a clear log. |
| D4 | Hedge strike | ATM only for now. OTM variants later. |
| D5 | Backtest before live | **RESOLVED — NO.** Owner decision: go live with small size, verify each step. Recorded consequence: all learning is live. |
| D6 | Short leg expiry | 0-2DTE. Note the chain-limit finding below: on 1DTE the theta rule almost never binds. |

---

# PART 1 — STRATEGY LOGIC

## 1.1 Two layers

**Long hedge (permanent core)**
- ATM long straddle: buy call + buy put, same strike, same expiry
- Expiry chosen by user (monthly default)
- Bought ONCE. Survives every short basket open/close cycle.
- Has its OWN target and stoploss, set by the user when enabling hedge mode
- Closes only on: its own target, its own stoploss, its expiry, or manual user action

**Short basket (daily income)**
- Existing S001 short strangle/straddle, unchanged in mechanics
- Strike selection and profit target may now be DERIVED from the hedge's theta
- Stop loss logic unchanged from current behaviour

## 1.2 Entry sequence

```
auto trade fires
  |
  +-- hedge_enabled == False  -> place short basket as today (S001)
  |
  +-- hedge_enabled == True
        |
        +-- active hedge exists for this underlying?
              |
              +-- YES -> place short basket only
              |
              +-- NO  -> 1. buy long straddle (call + put)
                         2. verify both legs on Delta
                         3. mirror hedge to every eligible slave
                         4. only then place the short basket
```

If the hedge purchase fails, the short basket must NOT be placed. Running the short
leg unhedged when the user asked for hedge mode is a silent strategy change.

## 1.3 Strike selection modes

| Mode | Behaviour |
|---|---|
| `fixed_premium` (current) | User sets target premium per side; bot finds nearest strike |
| `theta_based` (new) | Select by the SHORT leg's own theta, not by premium |

**theta_based rule:**
```
required_theta = long_straddle_TOTAL_theta x theta_multiplier

For each side, scan strikes outward from ATM and take the FURTHEST OTM strike
whose own |theta| is still >= required_theta.
```
Theta falls as you move OTM, so this picks the safest strike that still earns enough
theta to cover the hedge.

**Chain-limit fallback.** BTC option chains often do not extend far enough OTM for the
theta rule to bind. If a side runs out of strikes before the theta requirement stops
being met:
```
1. take the furthest available strike on that side
2. select the OTHER side by PREMIUM MATCHING to that strike, not by theta
3. log [STRIKE_CHAIN_LIMIT] side, strike, premium, theta
```

**Worked example** (BTC 71,620 · short expiry 21 Aug, 1d 2h · hedge total theta 82 · multiplier 3):
```
required_theta = 82 x 3 = 246

call side, scanning outward:
  71800 theta 429.09  ok
  72000       433.77  ok
  72400       435.09  ok
  72800       426.84  ok
  73200       413.37  ok
  73600       397.37  ok
  74000       376.20  ok
  74400       355.58  ok   <- last strike in the chain

every strike qualifies -> chain limit binds -> take 74400 (bid 295)
put side -> premium-match to ~295
```

**Economics of that example:**
```
short collects  ~590 points/day (295 x 2 sides)
hedge bleeds      82 points/day
coverage         7.2x
```
Compare with the earlier premium-based sizing at $10/side, which gave 0.24x coverage.
The theta-based rule is what makes this structure viable.

**Important practical note.** On 1DTE, theta is so large that even multiplier 3 leaves
every strike qualifying, so the chain limit is the binding constraint and the multiplier
has little effect. The multiplier only starts to matter on 2DTE / weekly short legs where
the chain is wide relative to the expected move. Do not be surprised when 1DTE entries
always land on the furthest available strike.

## 1.4 Target modes

| Mode | Behaviour |
|---|---|
| `payoff_pct` (current) | `profit_target_usd = initial_max_profit × tp_pct / 100` |
| `theta_multiplier` (new) | `profit_target_usd = hedge_total_theta × target_theta_pct / 100 × qty × 0.001`, default `target_theta_pct = 150` |

**Interaction warning to surface in the UI.** The two multipliers are coupled:
```
strike multiplier 2 -> max profit 165.78 pts, target at 150% = 124.34 -> 75% of max  (rarely reached)
strike multiplier 3 -> max profit 248.70 pts, target at 150% = 124.34 -> 50% of max  (realistic)
```
Show the implied "% of max profit" next to the target input so the user sees this.

**Stop loss is unchanged.** Existing gross-MTM logic, `sl_pct`, and the entry-spread
reset all stay exactly as they are.

## 1.5 Cooldown

After a short basket exits with `STOPLOSS` or `MAX_ADJUSTMENTS_REACHED`:
- start a cooldown (default 120 minutes, configurable)
- no new short basket until it expires
- the long hedge stays open and untouched throughout
- other exit reasons (PROFIT_TARGET, PRE_EXPIRY, MANUAL) use the normal re-entry delay

## 1.6 P&L separation

Three numbers, always distinct, never summed into one figure without a label:
```
short_basket_pnl    per basket, and cumulative across baskets since the hedge opened
hedge_pnl           the long straddle's own realized + unrealized
combined_pnl        the two together — the number that actually matters
```
The hedge's cumulative theta cost must be visible, because that is what the short
baskets are meant to be covering.

## 1.7 Capital and lot sizing (replaces the current master-ratio model)

Delta's Strategy Builder prices the full 4-leg structure at **1 lot = $7.45 order margin**
(buy call + buy put on the monthly, sell call + sell put on the short expiry).

```
capital_per_lot = order_margin_per_lot x (1 + margin_buffer_pct)
                = 7.45 x 1.5  =  ~$11.20   ->  round up to $12

slave_lots = floor(slave_allocated_capital / capital_per_lot)
```

Example: a slave allocating $1,200 gets `1200 / 12 = 100 lots`.

`order_margin_per_lot` is read from the MASTER's actual margin requirement for the
structure, not hardcoded, and refreshed each time a hedge or basket is placed.

This REPLACES the existing `_calc_qty` capital-based path for hedge mode. The old
master-margin-ratio calculation stays only for non-hedge (S001) trades.

A slave whose allocated capital is below one lot gets 0 lots, is skipped, and is logged
as `skipped_below_one_lot`. It must NOT be given an unhedged short basket.

---

---

# PART 2 — RISK NOTES (recorded so they are not forgotten)

**Net long vega.** Long 36DTE ATM straddle vega ≈ 175/lot. Short 1DTE strangle vega
≈ 5-10/lot. Net ≈ +165/lot, or +1,650 for 10 lots. A 5-point IV drop costs ~$82.50 —
more than a full month of theta. Buying the hedge when IV is elevated is the single
biggest way this structure loses money, and no amount of theta arithmetic prevents it.
Screenshot IV was ~36%, which is low for BTC, so the timing shown was favourable.
**Suggested guard:** show IV rank/percentile at hedge entry and warn above the 70th
percentile.

**Slave capital — resolved by the per-lot sizing model.** See section 1.7. Slaves fund in
multiples of `capital_per_lot`; a slave funding less than one lot gets zero lots and is
skipped explicitly. Current slave balances ($16.48, $22.15) support 1 lot each at ~$12/lot
while the master runs 10 — proportional and safe.

**The hedge does not fix a negative-expectancy short leg.** Backtest expectancy was
−$4.04/basket (54.9% win, avg win $13.66, avg loss $25.58). If that persists, the hedge
is paid for out of losses, not theta.

**But the hedge may fix it indirectly** — by allowing a WIDER stop loss. The 1.87:1
loss/win ratio came from tight stops causing whipsaw exits. With catastrophic risk
capped by the hedge, a looser stop becomes affordable. This is the most valuable thing
to test: `hedge + wide SL` vs `no hedge + tight SL`.

**Delta and gamma mismatch.** Short 0-1DTE legs have high gamma; the 36DTE hedge has
almost none. Net delta will swing fast intraday even though both legs start near neutral.

---

# PART 3 — IMPLEMENTATION PLAN

Ordered. Each step is one Cursor task, one commit. Do not merge steps — the slave side
is where things break silently.

## STEP 1 — Data model (nothing works without this)

New table `hedge_positions` — deliberately NOT part of `trades`, because a hedge
outlives many baskets:
```
id, account_id, underlying, expiry_date, strike,
call_product_id, call_symbol, call_order_id, call_fill_price, call_entry_fee,
put_product_id,  put_symbol,  put_order_id,  put_fill_price,  put_entry_fee,
quantity, status,                      -- active | closed | partial | error
entry_time, exit_time,
call_exit_price, put_exit_price, realized_pnl,
target_usd, stoploss_usd,
entry_total_theta,                     -- theta at purchase, for reporting
is_bot_managed
```

New table `slave_hedge_positions` — same shape plus `slave_account_id` and
`master_hedge_id`. Do NOT try to reuse `slave_trades`; the lifecycles differ.

`trades` gains `hedge_position_id` (nullable FK) so every basket records which hedge was
live while it ran.

Include the migration in `database.py` using the existing column-add pattern.

**Verify:** tables created on restart, existing trades unaffected, `hedge_position_id`
NULL on historical rows.

## STEP 2 — Settings and API

Add to `AutoTradeSettings`:
```
hedge_enabled                bool    default False
hedge_expiry_mode            str     'monthly' | 'date' | 'dte'
hedge_expiry_date_override   str|None
hedge_expiry_dte             int|None
hedge_qty_ratio              float   default 1.0     (hedge qty = short qty × ratio)
hedge_target_usd             float|None
hedge_stoploss_usd           float|None
strike_selection_mode        str     'fixed_premium' | 'theta_based'
theta_multiplier             float   default 3.0
target_mode                  str     'payoff_pct' | 'theta_multiplier'
target_theta_pct             float   default 150.0
cooldown_after_loss_minutes  int     default 120
```
Expose in the auto-trade GET/PATCH with validation. No engine behaviour yet.

**Verify:** values persist, defaults correct, existing settings untouched.

## STEP 3 — Theta reader

A single helper that returns the hedge's live total theta:
```
get_hedge_theta(hedge_position) -> {call_theta, put_theta, total_theta, fetched_at}
```
Read from Delta's option chain for the hedge's two symbols. Never estimate, never cache
across cycles. If the fetch fails, the caller must abort whatever it was doing rather
than fall back to a guess.

Log `[HEDGE_THETA] hedge_id, call_theta, put_theta, total_theta`.

**Verify:** values match the Delta UI option chain for the same strike/expiry.

## STEP 4 — Master hedge lifecycle (no slaves yet)

- `open_hedge()` — resolve ATM strike from spot, resolve expiry per settings, buy call
  then put, verify BOTH on Delta, persist the row. If the second leg fails, close the
  first with `reduce_only=True` and mark `error`. Never leave a one-legged hedge.
- `close_hedge(reason)` — close both legs with `reduce_only=True`, verify flat, record
  real fills, compute `realized_pnl`. Reasons: `HEDGE_TARGET`, `HEDGE_STOPLOSS`,
  `HEDGE_EXPIRY`, `HEDGE_MANUAL`.
- Monitor the hedge every cycle for its own target/stoploss, independently of any basket.
- Log `[HEDGE_OPEN]`, `[HEDGE_CLOSE]`, `[HEDGE_PNL]`.

Reuse the existing safety patterns: `reduce_only=True`, verify-before/verify-after, never
book `exit_premium=0.0`, and the per-trade lock pattern applied per hedge.

**Verify on master only:** hedge opens, survives a basket opening and closing, hits its
own target/SL correctly, and its P&L matches Delta's order history.

## STEP 5 — Entry gating

The auto-trade entry path becomes:
```
if hedge_enabled and no active hedge:
    open_hedge()
    if failed -> log CRITICAL, DO NOT place the short basket, retry next cycle
place short basket, stamping trades.hedge_position_id
```
Add a hedge guard to the existing entry-guard chain so it is visible in
`ENTRY_GUARD_PASS` / `ENTRY_GUARD_BLOCK`.

**Verify:** with hedge mode on and no hedge, the basket waits for the hedge. With a hedge
present, only the basket is placed.

## STEP 6 — Theta-based strike selection

Implement `strike_selection_mode == 'theta_based'`:
```
theta = get_hedge_theta(active_hedge).total_theta
per_side_target = theta_multiplier × (theta / 2)
call: nearest OTM strike with premium >= per_side_target
put:  nearest OTM strike with premium >= per_side_target
```
Log `[STRIKE_SELECT_THETA] hedge_theta, multiplier, per_side_target, call_strike,
call_premium, put_strike, put_premium`.

If no strike on a side reaches the target premium, do NOT silently take the closest
lower one — log a WARNING with the best available and skip the entry. Silently selling
less premium than required breaks the whole theta-coverage design.

**Verify:** hand-check one entry against the Delta option chain.

## STEP 7 — Theta-based target

Implement `target_mode == 'theta_multiplier'`:
```
profit_target_usd = hedge_total_theta × (target_theta_pct/100) × qty × CONTRACT_SIZE
```
Keep `stoploss_usd` exactly as it is computed today. Log both the resulting target and
its implied percentage of `initial_max_profit`, so an unreachable target is obvious:
`[TARGET_THETA] hedge_theta, target_theta_pct, target_usd, pct_of_max_profit`.

**Verify:** target matches hand calculation; the implied % of max is sane.

## STEP 8 — Cooldown

On basket exit with `STOPLOSS` or `MAX_ADJUSTMENTS_REACHED`, set
`AutoTradeSettings.next_entry_time = now + cooldown_after_loss_minutes`.
The hedge is untouched. Add a `cooldown` entry guard visible in `ENTRY_GUARD_BLOCK` with
the remaining minutes. Other exit reasons keep the existing re-entry delay.

**Verify:** force a stop loss, confirm no re-entry for the full period, and that the hedge
stays open the whole time.

## STEP 9 — Slave hedge mirroring (the hard part — do it alone)

- Size the slave hedge using the same live-balance rules as `_calc_qty`, plus an explicit
  affordability check: the slave must afford BOTH the hedge premium AND the short basket
  margin. If it cannot: `status='skipped_insufficient_capital'`, skip that slave for hedge
  mode entirely, and log it loudly. Do not run an unhedged short basket on that account.
- Mirror `open_hedge` with verify-before/verify-after on each leg, `reduce_only` on any
  unwind, and the master's absolute bracket prices if brackets are used on the hedge.
- **The hedge must NOT be closed by `mirror_exit`.** Basket exits must leave slave hedges
  untouched. Add an explicit test for this — it is the single easiest thing to get wrong.
- Mirror `close_hedge` only when the master hedge closes.
- Extend the DB-driven sweep to cover `slave_hedge_positions`.
- Log `[MIRROR_HEDGE_OPEN]`, `[MIRROR_HEDGE_CLOSE]`, `[MIRROR_HEDGE_VERIFY]`,
  `[HEDGE_SKIP_CAPITAL]`.

**Verify:** open a hedge, run two full basket cycles, confirm slave hedges are still open
and untouched after both baskets closed. Then close the hedge and confirm slaves follow.

## STEP 10 — P&L separation and UI

- Dashboard: a hedge card separate from basket cards — strike, expiry, entry cost, current
  value, unrealized P&L, today's theta, cumulative theta paid since entry.
- Basket cards show short P&L only, as today.
- A combined figure, explicitly labelled, showing `short_cumulative + hedge_pnl`.
- Auto Trade page: hedge section with all STEP 2 settings, plus the implied "% of max
  profit" hint next to the theta target, plus an IV warning at hedge entry.
- Slave overview: hedge row per slave, or a clear "hedge skipped — insufficient capital".

**Verify:** numbers on screen reconcile with Delta for both master and each slave.


---

# PART 5 — UI SPECIFICATION

Every number the bot computes must be visible. The rule: if the bot uses a value to make
a decision, the user must be able to see that value and hand-check the decision.

## 5.1 Dashboard — Live hedge panel (NEW, sits above the basket cards)

```
+-- LONG HEDGE  #3 -------------------------------------------------+
| BTC 71800 Straddle | 25 Sep 26 | 36d 4h left | 10 lots            |
|                                                                    |
| CALL 71800   entry $2935   now $2890   UPL  -$0.45                |
| PUT  71800   entry $2970   now $3010   UPL  +$0.40                |
|                                                                    |
| Entry cost      $59.78     Today's theta      -$0.83              |
| Current value   $58.95     Theta accrued      -$12.45  (15 days)  |
| HEDGE P&L       -$0.83                                             |
|                                                                    |
| Target +$8.00 | Stop -$20.00 | IV entry 36.2% | IV now 38.1%      |
| Bracket SL: Active (auto-cancels on close)                        |
|                            [ Close Hedge ]                        |
+--------------------------------------------------------------------+
```
Backend must supply: both leg entry fills and live offers, current theta per leg and
total, entry IV and live IV, days elapsed, target/stop, cumulative theta accrued.

"Theta accrued" is an ESTIMATE and must be labelled as such — it is the sum of daily
theta snapshots, not a measurable cash flow. Requires a small `hedge_theta_log` table
(hedge_id, date, call_theta, put_theta, total_theta, spot, call_iv, put_iv) written once
per day.

## 5.2 Dashboard — Combined P&L panel (NEW)

```
+-- SINCE HEDGE #3 OPENED  (15 Aug 09:12 - 15 days) ----------------+
| Hedge P&L (unrealized)                              -$0.83        |
| Short baskets realized  (23 closed)                +$18.40        |
| Short basket live       (#72)                       -$0.12        |
| ------------------------------------------------                  |
| COMBINED TOTAL                                     +$17.45        |
|                                                                    |
| Baskets: 23 closed | 14 win | 9 loss | 60.9% win rate             |
| Theta accrued to hedge  -$12.45   Theta earned by shorts +$30.85  |
| Net theta                                          +$18.40        |
| Avg per basket +$0.80 | Best +$2.10 | Worst -$1.40                |
+--------------------------------------------------------------------+
```
Backend: `SELECT ... FROM trades WHERE hedge_position_id = :id` gives everything except
the theta figures, which come from `hedge_theta_log` and the per-basket premium collected.

## 5.3 Dashboard — Basket card (existing, small additions)

Keep the current card exactly as it is, and add one line at the top:
```
Basket #72   |   under Hedge #3   |   basket 24 of this hedge
```
Plus, in the Bot Monitoring Plan block already on the card, add a hedge-aware section:
```
+-- BOT MONITORING PLAN ---------------------------------------------+
| Strike selection: theta-based, multiplier 3                        |
|   Hedge theta today        82.89  (call 43.67 + put 39.22)         |
|   Required per leg         246.00 (82.89 x 3)                      |
|   Chosen CALL 74400  theta 355.58  premium $295   [CHAIN LIMIT]    |
|   Chosen PUT  69000  theta 341.20  premium $298   [premium-matched]|
|   Coverage                 7.2x the hedge's daily theta            |
|                                                                    |
| Target: theta multiplier 150%                                      |
|   82.89 x 1.50 = 124.34 pts = $1.24                                |
|   = 21% of max profit ($5.93)          <- reachability indicator   |
|                                                                    |
| Next action: HOLD | Cooldown: none                                 |
+--------------------------------------------------------------------+
```
The "% of max profit" line is important: it makes an unreachable target obvious before it
costs a day of trading.

When a cooldown is running, replace "Cooldown: none" with the remaining time and the
reason that started it.

## 5.4 History view — hedge-grouped (NEW)

The basket history list becomes nested under its hedge. Each hedge is a collapsible box.

```
+-- HEDGE #3  BTC 71800  25 Sep 26   [CLOSED] ----------------------+
| Opened 15 Aug 09:12 -> Closed 25 Sep 17:15   (41 days)            |
| Entry $59.78 -> Exit $71.20    Hedge P&L  +$11.42                 |
| Exit reason: HEDGE_EXPIRY                                          |
|                                                                    |
|  Short baskets under this hedge: 38                               |
|  +----+----------+-------------+----------+-----------+---------+  |
|  | #  | Opened   | Strikes     | Exit     | Reason    | P&L     |  |
|  +----+----------+-------------+----------+-----------+---------+  |
|  | 72 | 15/08 09 | 74400/69000 | 15/08 17 | TARGET    | +$0.52  |  |
|  | 73 | 15/08 17 | 74000/69200 | 16/08 03 | STOPLOSS  | -$0.31  |  |
|  | 74 | 16/08 05 | 74400/69000 | 16/08 12 | MAX_ADJ   | -$0.18  |  |
|  | .. |          |             |          |           |         |  |
|  +----+----------+-------------+----------+-----------+---------+  |
|                                                                    |
|  Baskets total   +$24.80   (38 baskets | 22W / 16L | 57.9%)       |
|  Hedge P&L       +$11.42                                           |
|  ----------------------------                                      |
|  OVERALL         +$36.22                                           |
+--------------------------------------------------------------------+
```
Rows are clickable and expand to the existing per-basket detail (legs, adjustments,
exit fills) — reuse what the dashboard already renders.

Baskets with `hedge_position_id IS NULL` (all history before hedge mode) group under a
single "No hedge (S001)" box so nothing disappears from the history.

## 5.5 Auto Trade settings — hedge section (NEW)

```
+-- HEDGE MODE ------------------------------------------------------+
| [x] Enable hedge mode                                              |
|     Bot buys a long ATM straddle and holds it while short baskets  |
|     cycle underneath. The hedge is NOT closed when a basket closes. |
|                                                                    |
| Hedge expiry     ( ) Monthly   ( ) Specific date   ( ) DTE         |
|                  [ 25 Sep 26 v ]                                   |
| Hedge target     [    8.00 ] USD                                   |
| Hedge stop loss  [   20.00 ] USD                                   |
| Margin buffer    [      50 ] %      -> capital per lot ~$11.20     |
|                                                                    |
| --- Live preview (updates with spot) ---                           |
| Hedge would be: BTC 71800 straddle, 25 Sep 26, 10 lots            |
|   Estimated cost         $59.78                                    |
|   Estimated daily theta  -$0.83                                    |
|   Current IV             36.2%   (6-month percentile: 22nd)        |
|   [ok] IV is in the lower range - reasonable time to buy long vol  |
+--------------------------------------------------------------------+

+-- SHORT STRIKE SELECTION ------------------------------------------+
| ( ) Fixed premium   [ 150 ] per side                               |
| (x) Theta based     multiplier [ 3 ]                               |
|                                                                    |
| --- Live preview ---                                               |
|   Hedge theta today    82.89                                       |
|   Required per leg     246.00                                      |
|   Would pick CALL 74400 (theta 355.58, $295)  [chain limit]        |
|              PUT  69000 (premium-matched, $298)                    |
|   Coverage             7.2x                                        |
+--------------------------------------------------------------------+

+-- TARGET ----------------------------------------------------------+
| ( ) Payoff %        [ 50 ] % of max profit                         |
| (x) Theta multiplier[150 ] % of hedge daily theta                  |
|                                                                    |
|   = $1.24  =  21% of max profit                                    |
|   [ok] reachable                                                   |
+--------------------------------------------------------------------+

+-- COOLDOWN --------------------------------------------------------+
| After STOPLOSS or MAX_ADJUSTMENTS, wait [ 120 ] minutes            |
| The long hedge stays open during the cooldown.                     |
+--------------------------------------------------------------------+
```

The live previews are the point of this screen. The user must be able to see exactly what
the bot would do RIGHT NOW before enabling anything.

Reachability indicator thresholds: `<= 60%` of max profit shows "reachable",
`60-80%` shows a warning, `> 80%` shows "rarely reached - lower the target or raise the
strike multiplier".

## 5.6 Slave overview — hedge per slave (NEW)

Each slave row gains a hedge block:
```
earner_5dc0105f       allocated $1,200  |  capital/lot $11.20  ->  107 lots
  Hedge  #3   BTC 71800 25Sep  107 lots  entry $639.  now $631.  P&L -$8.90
  Basket #72  74400/69000      107 lots  live P&L -$1.28
  Since hedge opened: baskets +$196.  hedge -$8.90  ->  COMBINED +$187.
```
A slave that cannot fund one lot shows:
```
earner_xxxx     allocated $8.00  |  capital/lot $11.20  ->  0 lots
  [!] SKIPPED - allocated capital is below one lot. No hedge, no basket.
```

## 5.7 Data the backend must expose (API contract)

```
GET /api/hedge/active            -> live hedge + legs + theta + IV + targets
GET /api/hedge/{id}/summary      -> hedge P&L, basket count/W/L, cumulative totals
GET /api/hedge/{id}/baskets      -> list of trades with hedge_position_id = id
GET /api/hedge/history           -> hedges grouped, each with its basket summary
GET /api/strategy/theta-preview  -> what theta_based selection would pick right now
GET /api/strategy/target-preview -> resulting target and its % of max profit
```
`/api/slave/overview` gains a `hedge` block per slave with the same fields.

## 5.8 UI build order

Build the UI alongside the backend step that produces its data, never after:
```
Step 2  -> 5.5 settings screen (without live previews)
Step 3  -> add the theta live preview to 5.5
Step 4  -> 5.1 live hedge panel
Step 5  -> 5.3 basket card "under Hedge #N" line
Step 6  -> 5.3 strike-selection block in the monitoring plan
Step 7  -> 5.3 target block + reachability indicator
Step 8  -> 5.3 cooldown display
Step 9  -> 5.6 slave hedge rows
Step 10 -> 5.2 combined panel + 5.4 history view
```

---

# PART 4 — TESTING ORDER

Mirror the approach that worked for S001: one subsystem at a time, no overlap.

```
T1  hedge open/close on master only, hedge mode ON, auto trade OFF
T2  hedge + one manual short basket; confirm the hedge survives the basket's exit
T3  theta-based strike selection, hand-verified against the option chain
T4  theta-based target, hand-verified
T5  cooldown after a forced stop loss
T6  slave hedge mirroring — the survives-basket-exit test is the critical one
T7  full auto cycle: hedge + repeated baskets + cooldown + slave parity
```

Do not enable Conversion Mode during any of this. It still has zero verification on the
slave side (see SESSION_2026-08-16_CHANGES.md, open issue #2).
