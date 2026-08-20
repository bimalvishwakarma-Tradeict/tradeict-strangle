# System Verification Protocol
## Delta Exchange India Short Strangle Bot — Master + Slave
### Purpose: prove the system behaves exactly as strategy S001 specifies, one subsystem at a time

Run stages in order. Do NOT move to the next stage until the current one passes.
Fix only what the current stage exposes. Record results per stage.

---

## STEP 0 — PREREQUISITE: observability

Nothing below can be verified while these log tags are missing.

Confirm before starting:
```bash
grep -c "EXIT_FUNNEL" /home/botuser/trading-bot/logs/bot_activity.log
grep -c "MIRROR_EXIT"  /home/botuser/trading-bot/logs/bot_activity.log
grep -c "SLAVE_SWEEP"  /home/botuser/trading-bot/logs/bot_activity.log
```
All three must be > 0 after one exit and ~5 minutes of uptime.
If any is 0, Task 2.2 has not taken effect — stop and fix that first.

Also confirm the consistency fixer no longer relabels completed exits:
```bash
sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT id, exit_reason, exit_time FROM trades WHERE exit_reason LIKE 'DB_CONSISTENCY%' ORDER BY id DESC LIMIT 5;"
```
No NEW rows should appear with this reason after Task 2.2.

---

## STAGE 1 — ENTRY SYSTEM

### What the spec says
- Auto trade selects OTM call + OTM put, each nearest to `target_premium_per_side`
- Six entry guards must all pass: DB, tracker, Delta positions, settling, expiry <1h, adjusting
- Orphan positions (not bot-tracked) are auto-closed; bot-tracked positions block entry
- Actual FILL prices are saved to DB, not the user-entered target
- 2-minute settling period before P&L checks begin
- `initial_max_profit = (call_fill + put_fill) x quantity x 0.001`
- Each slave is sized by `_calc_qty`, capped by live balance, and mirrored with both legs

### Test settings
```
Underlying: BTC     Trade type: Short Strangle     Quantity: 10
Expiry: 0DTE or 1DTE
Target premium: whatever gives ~$10/side (fast, cheap)
Trigger %: 300      <- deliberately HIGH so no adjustment fires during this stage
TP %: 50            SL %: 200   <- both far away so no exit fires either
Re-entry delay: high, or auto-trade OFF after the first entry
```
Goal: isolate entry. Nothing else should trigger.

### Evidence to capture
```bash
tail -f /home/botuser/trading-bot/logs/bot_activity.log | grep -E \
"ENTRY_GUARD_PASS|ENTRY_GUARD_BLOCK|AUTO_TRADE_PLACED|ORPHAN_|SLAVE_SIZING|MIRROR_|CRITICAL"
```
```bash
sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT id, underlying, expiry_date, status, total_premium_collected, initial_max_profit,
        profit_target_usd, stoploss_usd, monitoring_starts_at
 FROM trades ORDER BY id DESC LIMIT 1;"

sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT trade_id, leg_type, strike, symbol, initial_premium, trigger_baseline_premium,
        quantity, status, delta_order_id, delta_sl_order_id
 FROM legs WHERE trade_id=(SELECT MAX(id) FROM trades) ORDER BY leg_type;"

sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT id, master_trade_id, slave_account_id, actual_quantity, status,
        call_fill_price, put_fill_price, call_order_id, put_order_id, last_error
 FROM slave_trades WHERE master_trade_id=(SELECT MAX(id) FROM trades);"
```

### PASS criteria
- [ ] Master has exactly 2 legs, both `status='open'`, both with a `delta_order_id`
- [ ] Call strike > spot, put strike < spot (both OTM)
- [ ] `initial_premium` equals the ACTUAL fill, not the target premium
- [ ] `trigger_baseline_premium` is set on both legs at entry
- [ ] `initial_max_profit` = (call_fill + put_fill) x qty x 0.001, verify by hand
- [ ] `monitoring_starts_at` is ~2 minutes after entry_time
- [ ] One `slave_trades` row per ACTIVE slave, `status='active'`
- [ ] Every slave row has BOTH `call_order_id` and `put_order_id` non-null
- [ ] Every slave row has both fill prices non-null and NOT identical to master's fills
      (identical values indicate the master-fill fallback fired — see defect below)
- [ ] `[SLAVE_SIZING]` logged for each slave with allocated / live_balance / final_qty
- [ ] No `last_error`, no `error` status, no CRITICAL lines

### Known open defects in this stage (expected failures, Phase 2)
- If the put order fails after the call fills, the filled call is NOT unwound and
  the row is saved as `error` with no order ids. Customer left with a naked short.
- Post-entry verification only checks "a position exists" — not side, not size.
  A leftover 1-lot can make a rejected 5-lot sell look verified.
- If fill-price resolution returns 0, the slave silently stores the MASTER's fill price.
- One slave with a bad encrypted key aborts mirroring for all remaining slaves.
- `master_put_qty` is ignored; both legs are sized from the call quantity.

---

## STAGE 2 — MONITORING SYSTEM

### What the spec says
- 30-second loop
- UPNL = `(entry_price - best_ask) x abs(size) x 0.001` (UPL@Offer). Never Delta's
  `unrealized_pnl` field, never mark price
- `gross_mtm = combined_upnl + realized_pnl + hedge_upnl`
- `net_mtm = gross_mtm - fees_paid - est_exit_fees - slippage_amount`
- TARGET is evaluated on **net** MTM; STOPLOSS on **gross** MTM (deliberate, BUG-C)
- Trigger baseline uses OFFER price only, never mark
- Slave MTM comes from each slave's own Delta positions

### Test settings
Same as Stage 1. Let it run 20-30 minutes with no adjustment and no exit.

### Evidence to capture
```bash
grep -E "PNL_CHECK|TRIGGER_CALC|MONITOR_TICK" /home/botuser/trading-bot/logs/bot_activity.log | tail -20
grep -E "Slave .* MTM updated" /home/botuser/trading-bot/logs/bot_activity.log | tail -10
```

### PASS criteria
- [ ] `MONITOR_TICK` roughly every 30 seconds, no gaps > 90s
- [ ] `mtm_source=delta_position` in PNL_CHECK (not a mark-price fallback)
- [ ] Hand-check one PNL_CHECK line:
      `gross_mtm == realized_pnl + delta_upnl` (+ hedge_upnl if a hedge exists)
      `net_mtm == gross_mtm - fees_paid - est_exit_fees - slippage_amount`
- [ ] `profit_target` and `stoploss` stay CONSTANT across ticks
      (they only change if you edit settings mid-trade — do not edit during this test)
- [ ] `will_exit_profit` / `will_exit_stoploss` are False the whole time
- [ ] TRIGGER_CALC: `call_baseline` / `put_baseline` equal the entry fills and do not drift
- [ ] `call_trigger_at == call_baseline x trigger_pct / 100` — verify by hand
- [ ] Slave MTM log lines appear and the values are NOT simply master MTM x 1.0

### Known open defects in this stage
- `/api/slave/overview` recomputes slave MTM as `master_net_mtm x qty_multiplier` and
  OVERWRITES the engine's correct Delta-fetched value. For capital-based slaves
  (qty 5 or 7 vs master 10) this is 2.00x / 1.43x inflated. Deferred to Phase 5,
  but be aware the dashboard number and the log number will disagree.

---

## STAGE 3 — ADJUSTMENT SYSTEM

### What the spec says
- Trigger fires when a leg's OFFER >= `trigger_baseline x trigger_pct / 100`
- Decision at trigger: `net_mtm > 0` -> close basket (DECISION_PROFIT_AT_TRIGGER);
  `net_mtm <= 0` -> adjust
- `target_new_premium = untouched_leg_offer + max(0, triggered_offer - triggered_baseline)`
- New strike = nearest premium at or above target
- Sequence: verify on Delta -> close triggered leg -> verify gone -> open new leg
  with bracket SL -> verify exists
- Baselines reset after: triggered leg -> its new fill; untouched leg -> its OFFER
  at that moment (never mark)
- `profit_target_usd` / `stoploss_usd` are NOT recalculated (locked at deployment)
- Slaves mirror with the same 5-stage verification
- Cooldown between adjustments is respected

### Test settings
```
Trigger %: 130-150   <- fires within minutes but NOT on bid-ask noise
TP %: 50   SL %: 200 <- still far away, so the trade survives several adjustments
Quantity: 10
```
Let it run through at least 3 adjustments.

### Evidence to capture
```bash
tail -f /home/botuser/trading-bot/logs/bot_activity.log | grep -E \
"TRIGGER_CALC|DECISION_TRIGGER|ADJUSTMENT_TARGET|ADJUSTMENT_START|ADJUSTMENT_DONE|ADJUSTMENT_FAIL|BASELINE_RESET|ADJUSTMENT_DELTA_VERIFY|MIRROR_ADJ_VERIFY|PARTIAL"
```
```bash
sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT id, trade_id, leg_type, trigger_pct_reached, old_strike, old_exit_premium,
        new_strike, new_entry_premium, decision_type, timestamp
 FROM adjustments WHERE trade_id=(SELECT MAX(id) FROM trades) ORDER BY id;"
```

### PASS criteria
- [ ] `[ADJUSTMENT_TARGET]` arithmetic: `target == untouched_offer + loss`, hand-verified
- [ ] Master logs 3 x `ADJUSTMENT_DELTA_VERIFY` (pre-close, post-close, post-entry)
- [ ] Each slave logs `MIRROR_ADJ_VERIFY` at pre_close / post_close / post_entry
- [ ] `pre_close` live_size equals stored_qty for every slave
- [ ] `post_entry` actual_size equals expected_qty for every slave
- [ ] Master's new strike and each slave's new product_id refer to the SAME contract
- [ ] `BASELINE_RESET` appears for the untouched leg with `source=offer`
- [ ] The triggered leg's baseline is set to its NEW fill price
- [ ] `profit_target_usd` and `stoploss_usd` unchanged in the trades row
- [ ] Cooldown honoured — no two adjustments closer than `cooldown_minutes`
- [ ] No `partial_adjustment` / `adjust_close_failed` slave statuses
- [ ] The new strike is NOT a strike this leg occupied in the previous adjustment
      (round-trip check — see defect below)

### Known open defects in this stage
- **Round-trip:** `exclude_strike` only excludes the strike being closed, not one
  recently vacated. Observed on Trade#61: 64400 -> 64000 -> 64400 in 3m47s, zero net
  position change, four legs of fees. Phase 2.
- **No minimum absolute move:** the trigger is a pure percentage. At ~$9 premiums a
  115% trigger is a $1.35 move, i.e. inside the bid-ask spread, so it fires on noise.
  Less visible at production premiums but the guard is still missing. Phase 2.
- **Conversion mode has ZERO verification.** If the replacement premium falls below
  `adj_low_premium_min_usd` the bot enters conversion mode, and on the slave side all
  three steps (hedge buy, other-leg buyback, new short) run with no result checks and
  no compensation. Backtests show 69% of baskets enter this path. Also: the hedge is
  closed WITHOUT `reduce_only`, which can flip the slave into a naked short, and
  `SlaveTrade` has no columns to record hedge state at all. Phase 2 + Phase 3.
  **Set `adj_low_premium_min_usd` LOW during Stage 3 so conversion does NOT fire** —
  test it separately once Phase 2 lands.

---

## STAGE 4 — EXIT SYSTEM

### What the spec says
Exit priority:
1. `net_mtm >= profit_target_usd` -> PROFIT_TARGET
2. `gross_mtm <= -stoploss_usd` -> STOPLOSS
3. within 15 min of 5:30 PM IST -> PRE_EXPIRY
4. decision at trigger with `net_mtm > 0` -> DECISION_PROFIT_AT_TRIGGER
5. user action -> MANUAL_EMERGENCY / MANUAL_LEG_CLOSE

Ordering: mirror slaves FIRST, then close master legs, then mark the trade closed.
All legs close including any long hedge (`reduce_only=True`).
A SlaveTrade is marked closed only after its positions are verified flat.

### Test — run each exit reason separately
Do them one at a time, fresh trade each time:

**4a. PROFIT_TARGET** — set TP % very low so net_mtm crosses it
**4b. STOPLOSS** — set SL % very low so gross_mtm crosses it
**4c. PRE_EXPIRY** — run a 0DTE trade into 5:15 PM IST
**4d. MANUAL_EMERGENCY** — click Emergency Exit
**4e. MANUAL_LEG_CLOSE** — close one leg from the UI, then the other

### Evidence to capture
```bash
tail -f /home/botuser/trading-bot/logs/bot_activity.log | grep -E \
"EXIT_START|EXIT_VERIFY|EXIT_CLOSE|EXIT_CLEANUP|EXIT_COMPLETE|EXIT_FUNNEL|MIRROR_EXIT|MIRROR_LEG_CLOSE|exit_failed|CRITICAL"
```
```bash
sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT id, status, exit_reason, exit_time, realized_pnl FROM trades ORDER BY id DESC LIMIT 3;"

sqlite3 -header -column /home/botuser/trading-bot/trading_bot.db \
"SELECT status, COUNT(*) FROM slave_trades GROUP BY status;"
```
```bash
curl -s localhost:8000/api/slave/overview | python3 -m json.tool | grep -E "\"name\"|active_slave_trade|last_error"
```

### PASS criteria (for EVERY exit reason tested)
- [ ] `[EXIT_START]` shows the CORRECT reason for what you triggered
- [ ] `[EXIT_VERIFY] stage=pre_exit` lists both legs as `exists: True`
- [ ] `[EXIT_CLOSE]` for each leg with `ok=True` and a REAL fill price (not 0.0)
- [ ] `[EXIT_VERIFY] stage=post_exit` shows all legs `False`
- [ ] `[EXIT_CLEANUP]` reports 0 orphans
- [ ] `[MIRROR_EXIT]` runs BEFORE the master legs close
- [ ] `[EXIT_FUNNEL]` shows `slaves_total == slaves_closed`, `slaves_failed=0`
- [ ] DB `exit_reason` matches `[EXIT_START]` — NOT relabelled afterwards
- [ ] DB `realized_pnl` is derived from the actual fills, not a stale MTM snapshot
- [ ] Zero `slave_trades` rows left `active` / `exit_failed` / `partial` for that master
- [ ] `/api/slave/overview` shows `active_slave_trade: null` for every slave
- [ ] `[SLAVE_SWEEP]` appears at least once and reports 0 problem rows

### Known open defects in this stage
- Exit can close the customer's OWN unrelated option positions: unmatched shorts land
  in `extras`, and if nothing matches at all the code closes the entire option book.
  Only test on slave accounts with no other positions until Phase 3.
- Slave exit fills and realized P&L are discarded — `SlaveTrade` has no columns for
  them. Customer P&L is not auditable yet. Phase 3.
- SL-cancel failure is swallowed and the order id is erased anyway, leaving an
  untracked live stop order on the customer's account. Phase 2.
- On close-order retry the code deliberately drops `reduce_only`, which can OPEN a
  position instead of closing it. Phase 2.

---

## RESULT LOG

| Stage | Date | Result | Failures found | Fixed in |
|---|---|---|---|---|
| 0 Observability | | | | |
| 1 Entry | | | | |
| 2 Monitoring | | | | |
| 3 Adjustment | | | | |
| 4a PROFIT_TARGET | | | | |
| 4b STOPLOSS | | | | |
| 4c PRE_EXPIRY | | | | |
| 4d MANUAL_EMERGENCY | | | | |
| 4e MANUAL_LEG_CLOSE | | | | |

Rule: a stage is only PASS when every checkbox is ticked with evidence pasted.
A stage with a known open defect that did NOT fire is still PASS — note it and move on.
