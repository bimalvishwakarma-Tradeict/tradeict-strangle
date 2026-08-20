# Session Record — 16 August 2026
## Delta Exchange India Short Strangle Bot (Tradeict)
### 19 commits. Read this BEFORE PROJECT_KNOWLEDGE_UPDATED.md — several sections of that document are now WRONG.

---

# PART 1 — WHAT IN THE OLD DOC IS NOW OBSOLETE

| Old doc says | Reality after this session |
|---|---|
| §3.4 "2-minute settling period" | Entry settling is now **60s, user-configurable**. Adjustment settling is **20s, user-configurable**. |
| §3.4 "STOPLOSS uses Gross MTM" | Still true, BUT the added-back spread is no longer cumulative — it **resets on every adjustment**. |
| §3.5 `net_mtm = gross - fees - est_exit_fees - slippage` | Incomplete. Actual formula also subtracts `expected_exit_spread_usd`. |
| §3.8 "premium-match to other leg's offer" | Target is now `untouched_leg_offer + basket_net_loss`, where loss comes from **trigger baselines of both open legs**. |
| Bug list "#6 bracket orders not separate stop orders" | Was regressed mid-session and re-fixed. Bracket is now attached inline to the opening order and is the ONLY mechanism. |
| §3.11 "Manual Individual Leg Close — user can close one specific leg" | **Changed by owner decision.** Closing one leg now exits the ENTIRE basket. A one-legged basket is never a valid state. |
| Slave MTM = Master Net MTM × qty_multiplier | This is a **known bug**, not intended behaviour. See Part 4. |

---

# PART 2 — CURRENT BEHAVIOUR (verified in production today)

## 2.1 Exit — single funnel

Every path that closes a master trade now goes through ONE function:

```
close_master_trade(trade_id, reason, db, skip_master_legs=False)
    1. await mirror_exit(...)        <- slaves close FIRST
    2. close master legs
    3. set Trade.status / exit_reason / exit_time
    4. position_tracker.mark_closed()
    5. log [EXIT_FUNNEL] slaves_total / slaves_closed / slaves_failed / master_legs_closed
```

Routed through it: `_exit_trade`, `_handle_manual_close`, `_emergency_close_remaining_leg`,
reconcile-detected closes, zombie heal, emergency-exit route (all branches),
`close_single_leg`.

`trade_reconcile.py` is now a **detector only** — it returns `fully_closed` +
`close_reasons` and never sets `Trade.status`.

**Per-trade `asyncio.Lock`.** A second exit for the same trade waits, re-checks status,
and returns `[EXIT_SKIP]` with `existing_reason` — no orders, no DB writes.

## 2.2 Leg booking — never fabricate a price

- `book_leg_close` never overwrites a leg already `status='closed'` → `[LEG_BOOK_SKIP]`
- `exit_premium = 0.0` is never written. Exchange-close paths resolve the real fill from
  Delta order/fill history. If unresolvable: `NULL` + `trade.notes='PNL_UNRESOLVED_<leg>'`
  + CRITICAL log.
- `realized_pnl` is recomputed once from the closed legs at the end of the funnel.
- `[PNL_SANITY_FAIL]` fires when booked realized P&L sign disagrees with last gross MTM.

**Why this exists:** Trade #66 booked `realized_pnl = +0.11` when the true value was
`-0.019`. Two exits raced; the second used a hardcoded `exit_premium=0.0`, which reads as
"bought back for free". Delta's own records showed the real fills (put 3.0, call 9.9).
Repair script: `deploy/repair_zero_exit_premiums.py --trade-id N --dry-run|--apply`

## 2.3 Adjustment target — basket net loss

```
combined_baseline = sum of trigger_baseline_premium of ALL open SHORT legs
combined_current  = sum of current OFFER prices of those legs
loss              = max(0, combined_current - combined_baseline)
target_new_premium = untouched_leg_offer + loss
```

`trigger_baseline_premium` carries the history automatically:
- at entry, baseline == entry fill
- after each adjustment, triggered leg's baseline = its NEW fill; untouched leg's baseline
  = its OFFER at that moment
- realized losses from previous adjustments are NOT carried forward

Owner's worked example:
```
entry:        call 200, put 200          -> combined_baseline 400
adjustment 1: call 300, put 110          -> combined_current 410
              loss = 10  (NOT 300-200=100)
              target = 110 + 10 = 120
new baselines: call 120, put 110
adjustment 2: combined_baseline = 230
```

**This changed the strategy's behaviour materially.** With the old (per-leg) loss the
target was inflated and the bot rolled TOWARD ATM into more expensive strikes. With the
correct basket loss it rolls AWAY from ATM into cheaper strikes:

```
Adj 1 (old formula): target 10.0 -> 64200 to 64000, premium 9.0 -> 15.0   (toward ATM)
Adj 2 (new formula): target  6.8 -> 64000 to 64400, premium 15.0 -> 7.0   (away from ATM)
```

Any conclusion drawn from trading sessions before 16 Aug used the buggy formula.

`premium_cover_loss` override is disabled — the basket formula is the only rule.
**The UI toggle "Premium Cover Loss" is still shown as ON but has no effect. Unresolved.**

## 2.4 Bracket stop loss — one canonical price

- `compute_bracket_sl(master_fill, uni_sl_pct)` in `core/delta_sl.py` is the single source
- Sent **inline with the opening order** (`bracket_stop_loss_price` +
  `bracket_stop_loss_limit_price`). No standalone stop orders anywhere.
- Master and every slave use the **same absolute price** (`source=master_absolute`)
- Chicken-and-egg: bracket must go with the order, before any fill exists. So the price is
  `mark × uni_sl_pct` at placement, then an amend to the fill-derived price is attempted.
  **Delta rejects the amend on IOC-filled parents** (`open_order_not_found`), so in practice
  the mark-derived price stays canonical for master and slaves alike.
- Consequence: effective stop sits around **240-250% of fill** while `universal_sl_pct` is
  220. Looser than configured, but identical across all accounts.
- Anomaly guard: if `|fill - mark| / mark > 0.35`, log `[BRACKET_SL_ANOMALY]` and fall back.
- Delta auto-cancels the bracket when the position closes — verified in Delta's order
  history, every `Bracket - SL` row shows `Cancelled` with `Reduce-Only ✓`.

## 2.5 Entry spread for stop loss — resets, not cumulative

```
gross_mtm_for_stoploss = total_pnl + entry_spread_for_sl
```
`entry_spread_for_sl_usd` is **set** (not added) on each adjustment/conversion to the new
leg's spread only. At original entry it is the sum of both opening legs.

**Why:** it previously accumulated, so the stop got looser with every adjustment —
exactly when the trade was in the most trouble.
```
0 adjustments : ~0.02 -> effective SL threshold -0.11
2 adjustments :  0.04 -> effective SL threshold -0.13
```
`net_mtm` does NOT include this field and must never include it. Regression test locks
Trade#64 at `-0.0542`.

## 2.6 Settling — configurable, stop loss always runs

| Window | Field | Default | Applies to |
|---|---|---|---|
| Entry | `AutoTradeSettings.entry_settling_seconds` → `Trade.monitoring_starts_at` | 60s | TP, adjustment trigger |
| Adjustment / conversion | `AutoTradeSettings.adjustment_settling_seconds` → `Trade.adjust_settling_until` | 20s | TP, adjustment trigger |

- **STOPLOSS is never suppressed** by either window → `[SETTLING_BYPASS] check=stoploss`
- `monitoring_starts_at` is ENTRY-ONLY. Adjustments never move it.
- `MONITOR_TICK` logs `settling_source=entry|adjustment|none`
- Setting either to 0 disables that window
- Both editable on the Auto Trade page (0-300)

**Why the adjustment window exists:** the slave mirror runs after the master adjustment and
takes time — Trade#66 master done 20:13:13, last slave verify 20:13:28 (15 seconds). An exit
inside that window would race the mirror.

**Why STOPLOSS bypasses:** Trade#67 had `will_exit_stoploss=True` at 23:16:59 but settling
blocked the exit until 23:18:23 — 84 seconds unprotected while already 5x past the threshold.

## 2.7 Single-leg close = whole basket exit

Owner decision, changed from the old spec. Clicking "Exit Basket (Call)" or
"Exit Basket (Put)" calls `close_master_trade(reason=MANUAL_LEG_CLOSE)`. Both legs and all
slave positions close under the lock. The clicked leg is audited in
`trade.notes` as `user_exit_via=call|put`.

Consequence: a one-legged basket is never valid, so naked-risk detection is strict —
a leg missing on Delta while its DB row says `status='open'` is always naked risk, and it
now exits with `INTEGRITY_NAKED_CLOSE` (previously mislabelled
`SL_TRIGGERED_EMERGENCY_CLOSE`).

## 2.8 Slave mirroring

**Sizing** (`_calc_qty`, capital-based mode):
```
effective_capital  = min(user_allocated_capital, live_balance)   <- live fetch REQUIRED
master_ratio       = master_margin_used / master_total_capital
per_lot_cost_usd   = master_margin_used / master_qty
slave_qty          = round(effective_capital * master_ratio / per_lot_cost_usd)
```
- If the live balance fetch fails, the mirror is ABORTED. It never falls back to the
  declared allocation (that fallback previously produced qty 137/139/2746).
- `MAX_SLAVE_QTY = 100` ceiling, margin precheck to ≤90% of live balance
- `[SLAVE_SIZING]` logs allocated / live_balance / effective / per_lot / final_qty
- Note: results can flip between adjacent integers at the rounding boundary (e.g. 6 vs 7)
  as balances drift. Expected, not a bug.

**Adjustment** — 5-stage verified, same as master:
```
pre_close (live size == stored qty) -> close_position(reduce_only=True)
-> post_close (position gone) -> open new leg -> post_entry (size matches)
```
Failure states: `adjust_close_failed`, `partial_adjustment` — both bot-owned, both picked up
by the sweep and by `mirror_exit`.

**Exit** — targets every non-`closed` SlaveTrade, closes from live positions, and marks the
row `closed` ONLY after re-fetching and confirming flat. Otherwise `exit_failed` (a
non-terminal, still-bot-owned status included in `_resolve_entry_conflicts`).

**Sweep** — DB-driven (`SlaveTrade.status != 'closed'`), runs every 5th cycle regardless of
the position tracker. Logs `[SLAVE_SWEEP] rows_scanned / closed_ok / close_failed /
unreachable / skipped_backoff / generation` on every pass, including zero rows.

**Startup audit CHECK 4** queries each slave's live option book before changing any status,
and covers `error` / `partial` / `exit_failed` rows. It cleaned the 10 stuck rows from the
observed cascade on first restart.

---

# PART 3 — NEW LOG TAGS AND FIELDS

## Log tags added today
```
EXIT_FUNNEL             trade_id, reason, slaves_total, slaves_closed, slaves_failed, master_legs_closed
EXIT_SKIP               second exit blocked by the lock; shows existing_reason
LEG_BOOK_SKIP           leg already closed; shows existing vs attempted exit_premium
PNL_SANITY_FAIL         booked realized sign disagrees with last gross MTM
MIRROR_EXIT             stage=start|awaited_complete|no_slaves
MIRROR_ADJ_VERIFY       stage=pre_close|post_close|post_entry, live_size, stored_qty
MIRROR_PARTIAL_ADJ      partial adjustment mirrored to slaves
SLAVE_SWEEP             every sweep pass, including zero rows
SLAVE_SIZING            per-slave sizing inputs and result
BRACKET_SL              leg, master_fill, master_mark, uni_sl_pct, stop_price, anomaly_fallback
BRACKET_SL_ANOMALY      fill/mark divergence > 35%
ADJUSTMENT_TARGET       combined_baseline, combined_current, loss, untouched_offer, target
ENTRY_SPREAD_RESET      old_value, new_value on each adjustment
SETTLING_BYPASS         check=stoploss during a settling window
LEG_CLOSE_RESULT        trade_id, clicked_leg, ok, message
ORPHAN_SL_CANCELLED     stale standalone stop order removed
DB_AUDIT_SKIP           consistency fixer preserved an existing exit_reason
INTEGRITY_NAKED_CLOSE   (exit reason) replaces the mislabelled SL_TRIGGERED_EMERGENCY_CLOSE
```

## LOG FILE LOCATIONS — IMPORTANT
Two destinations. Grepping the wrong one wastes time.
```
/home/botuser/trading-bot/logs/bot_activity.log
    tags written via log_and_buffer, format: [TAG] Trade#N @ HH:MM:SS IST | ...

/var/log/trading-bot/error.log   (and error.log.N rotations)
    plain logger.* calls — SLAVE_SIZING, "Slave '...' CALL placed", delta_client output,
    HTTP requests. Python logging writes to stderr, which supervisor sends here.
    NOT output.log.

Rotation: error.log fills ~10MB/day with 10 rotations kept. Forensic window is short.
```

## New DB columns
```
trades.entry_spread_for_sl_usd     replaces cumulative_entry_spread_usd
trades.adjust_settling_until       adjustment settling deadline (UTC)
auto_trade_settings.entry_settling_seconds        default 60
auto_trade_settings.adjustment_settling_seconds   default 20
auto_trade_settings.max_adjustments_per_basket    (pre-existing, only active when
                                                   Conversion Mode is OFF)
```

## New SlaveTrade statuses
```
exit_failed             exit ran but positions could not be verified flat — bot-owned, retried
adjust_close_failed     old leg close failed; no new leg was opened
partial_adjustment      old leg closed but new leg entry failed
skipped_low_capital     sized below 1 lot; no order placed (not an error)
blocked_foreign_position  conflicting position is NOT bot-owned; left untouched
```

---

# PART 4 — KNOWN OPEN ISSUES (not fixed today)

Ranked. The first is the only one that touches customer money directly.

**1. Slave MTM overwritten with the wrong multiplier — CONFIRMED IN PRODUCTION**
`routes_slave.py:871-886` recomputes `slave_net_mtm = master_net_mtm × qty_multiplier` on
every overview API call and OVERWRITES the engine's correct per-slave Delta-fetched value.
For capital-based slaves `qty_multiplier` is only a fallback, so this is simply wrong.

Live evidence (Trade#64):
```
15:10:16 Slave 5dc MTM updated: -0.0444 -> -0.0200    (correct, from its own Delta)
15:10:17 Slave 688 MTM updated: -0.0444 -> -0.0240    (correct)
15:10:49 Slave 5dc MTM updated: -0.0606 -> -0.0200    <- old value identical for BOTH
15:10:50 Slave 688 MTM updated: -0.0606 -> -0.0234       = master's MTM was written over both
```
Inflation: slave 2 reported 3.03x its real MTM, slave 3 2.53x.

Chain to money:
```
routes_slave.py -> bot API net_mtm -> botSyncService.ts:121 -> tradePnl
-> tradeSettlementService.ts:235 tradeProfit -> subscriptionController.ts:79
   commissionAmount = tradeProfit * profitShare / 100
```
**Must be fixed before real customers are billed.**

**2. Conversion mode has ZERO verification on the slave side**
`mirror_conversion` runs three orders (hedge buy, other-leg buyback, new short) with no
result checks and no compensation. `mirror_hedge_close` sells the hedge **without
`reduce_only`**, which can flip a slave into a naked short. Neither function checks
`is_virtual_slave_trade`, so paper accounts would get real orders. `SlaveTrade` has no
columns for hedge state at all, so a stranded hedge is undiscoverable from the DB.
Backtests show 69% of baskets enter this path. **Conversion Mode is currently OFF — keep it
off until this is fixed.**

**3. `reduce_only` deliberately dropped on close retry — two places**
`mirror_engine.py:376-382` (conflict close) and `~1623-1640` (exit close) retry without
`reduce_only` when the first attempt is rejected. Rejection usually means "no position to
reduce", so the retry reliably OPENS the position it was meant to close.

**4. Entry partial has no unwind**
If the put order fails after the call fills, the filled call is not bought back and the row
is saved as `error` with no order ids — invisible to every `status=='active'` query.
Same in the manual attach route (`routes_slave.py:505-610`), which creates no row at all.

**5. Position-fetch failure is indistinguishable from a flat account**
`live_positions = []` on exception. Combined with #3 this can fabricate positions.

**6. Exit can close a customer's own unrelated positions**
Unmatched shorts land in `extras`; if nothing matches at all, the code closes the entire
option book of that account.

**7. Mirroring is fire-and-forget, and there is no per-slave lock**
Entry / conversion / hedge-close mirrors use unreferenced `asyncio.create_task`. Exceptions
vanish. A conversion mirror can run concurrently with an exit mirror.

**8. `SlaveTrade` cannot represent reality**
No product ids, no strikes, no hedge fields, no exit prices, no realized P&L, no per-leg
state. Slave close results are discarded (`closed_count += 1` and nothing else). Customer
P&L is not auditable. A `SlaveLeg` child table is the fix.

**9. Mixed timezone storage**
`auto_trade_engine.py` uses `get_ist_now()` in most places and
`datetime.now(timezone.utc)` at L667. So `entry_time` is UTC and `monitoring_starts_at` is
IST in the same row. Works by luck; any naive duration calculation is off by 5.5 hours.

**10. Smaller items**
- `round(x, 2)` collapses TP and SL to the same value at test scale (e.g. `initial_max_profit`
  0.121 → both 0.01). Harmless at production premiums.
- "Premium Cover Loss" UI toggle shows ON but the code ignores it.
- UI label still reads "Entry Spread Diff (cumulative)" — no longer cumulative.
- Adjustment round-trip: `exclude_strike` only excludes the strike being closed, not one
  recently vacated. Trade#61 went 64400 → 64000 → 64400 in 3m47s for zero net change.
- Trigger is a pure percentage with no minimum absolute move, so at ~$9 premiums a 115%
  trigger ($1.35) fires on bid-ask noise. Use 130%+ for testing at these premium levels.

---

# PART 5 — TEST RESULTS (SYSTEM_TEST_PROTOCOL)

| Stage | Result | Notes |
|---|---|---|
| 0 Observability | PASS | Required fixing missing log emission + a `exit_exit_db` NameError that was killing the exit funnel mid-run |
| 1 Entry | PASS 12/12 | `initial_max_profit` = (9.0+11.0)×10×0.001 = 0.20 verified by hand |
| 2 Monitoring | PASS | All MTM arithmetic hand-verified; found `expected_exit_spread_usd` missing from the documented formula |
| 3 Adjustment | PASS | `[ADJUSTMENT_TARGET]` verified: baseline 22.0, current 22.9, loss 0.9, target 6.8 |
| 4a PROFIT_TARGET | NOT TESTED | |
| 4b STOPLOSS | PASS | Trade#67; `[EXIT_SKIP]` blocked the racing second exit |
| 4c PRE_EXPIRY | NOT TESTED | Needs a 0DTE trade run to 5:15 PM IST |
| 4d MANUAL_EMERGENCY | PASS | Trade#64 |
| 4e MANUAL_LEG_CLOSE | FAIL → fixed in 4d010d8, RETEST NEEDED | Closing one leg triggered naked-risk emergency and closed the basket by accident |
| 4f MAX_ADJUSTMENTS_REACHED | PASS | Trade#66, limit 2, working as configured |
| 4g INTEGRITY_NAKED | PASS | `[LEG_BOOK_SKIP]` preserved real fills 3.8 / 10.0 |

**Not exercised:** adjustment round-trip guard, adjustment cooldown under back-to-back
triggers, conversion mode (deliberately kept OFF).

---

# PART 6 — COMMITS

```
a789181  slave sizing: live-balance cap, MAX_SLAVE_QTY, margin precheck, conflict recovery
0d45756  slave adjustment atomicity: Delta verification, reduce_only, master SL pct
372b54e  close_master_trade funnel; _exit_trade routed through it
4122226  verify slave positions flat before marking closed; exit_failed status
5d6dcfc  manual-close and naked-risk handlers routed through the funnel
361c904  reconcile becomes detector-only; funnel closes and mirrors
67ce13c  single-leg close and leftovers-only emergency branch mirror to slaves
fbfa93d  DB-driven integrity sweep; startup audit verifies Delta before closing rows
0901f05  mirror leg close on partial master adjustment
4684967  restore loss premium in adjustment target (regression)
bdea3b3  observability: real log emission; consistency fixer guard; exit_exit_db typo
e2c2cd6  canonical bracket SL computed from master fill
90faa60  revert to inline bracket SL; no standalone stop orders
f43ac78  loss premium = basket net loss from trigger baselines
8387921  entry spread for SL resets per adjustment, no longer cumulative
e392b59  per-trade exit lock; never overwrite booked fills; real fills on exchange-close
1cc966b  settling: 60s entry, none on adjustment, stop loss never suppressed
45f1d9c  configurable entry/adjustment settling windows
4d010d8  single-leg click exits whole basket via the funnel
```

---

# PART 7 — NEXT

1. Retest 4e (single-leg close) after `4d010d8`
2. 4a PROFIT_TARGET, 4c PRE_EXPIRY
3. Open issue #1 (slave MTM) — before any real customer billing
4. Open issue #2 (conversion mode) — before Conversion Mode is ever switched ON
5. Open issues #3-#5 (reduce_only retry, entry partial unwind, fetch-failure handling)
6. Open issue #8 (SlaveLeg table) — unblocks auditable customer P&L
7. Re-run the backtest. All results before 16 Aug used the buggy per-leg loss formula and
   are not valid for evaluating the strategy.
