# Slave Management — Full Audit Report
## Delta Exchange India Short Strangle Bot (Tradeict)
### Audit date: 16 Aug 2026 | Codebase: commit before `a789181` / `0d45756`

Scope: every slave (customer) mirroring path — entry, adjustment, conversion/hedge,
exit, integrity, recovery, and lifecycle wiring. Reviewed against master-side
reference implementations and the documented S001 strategy spec.

Already fixed and **excluded** from this report:
- `a789181` — `_calc_qty` live-balance capping, MAX_SLAVE_QTY, margin precheck, conflict auto-recovery
- `0d45756` — `_mirror_adjustment_to_slave` atomicity, bracket SL from `universal_sl_pct` (entry/adjust/conversion), old-leg fill fallback removed

---

# THE ROOT CAUSE

**There are 8 code paths that close a master trade. Only 2 of them place close orders on slave accounts.**

The other 6 flip `SlaveTrade.status = "closed"` in the database and place **no orders at all**.
The customer's real position stays open on their own Delta account, now invisible to
every monitoring, MTM, integrity and exit query in the system — all of which filter on
`status == "active"`.

This single defect explains:
- the observed "Conflicting positions already exist on slave" cascade (7 stuck rows across 4 consecutive master trades)
- how a customer can hold an unmanaged short strangle to expiry with no stop-loss supervision
- why the bot reports everything as healthy while it happens

## Master-close paths vs slave mirroring

| Master close path | File:Line | Places slave close orders? |
|---|---|---|
| `_exit_trade` (TP/SL/PRE_EXPIRY/DECISION/EMERGENCY) | `bot_engine.py:2102` | YES — awaited |
| Manual emergency exit (main branch) | `routes_trade.py:2085` | YES — awaited |
| `_handle_manual_close` (exchange-detected) | `bot_engine.py:845-875` | **NO — DB only** |
| `_emergency_close_remaining_leg` (naked risk) | `bot_engine.py:1153-1179` | **NO — DB only** |
| `reconcile_open_legs_with_delta` → `fully_closed` | `trade_reconcile.py:330-345` | **NO — none** |
| `heal_zombie_active_trades` | `trade_reconcile.py:103-135` | **NO — none** |
| Startup DB audit CHECK 4 | `db_audit.py:136-181` | **NO — DB only** |
| Manual single-leg close | `routes_trade.py:2253-2425` | **NO — none** |
| Emergency exit "leftovers only" branch | `routes_trade.py:2027-2050` | **NO — returns early** |
| Partial adjustment on master | `adjustment.py:1092` / `bot_engine.py:3211` | **NO — mirror only in `success` branch** |

Plus: even the working exit path marks the row closed when **zero** close orders succeeded.

---

# THEME 1 — Abandoned customer positions (CRITICAL)

### 1.1 Exchange-detected master close abandons all slaves
`bot_engine.py:845-875` — `_handle_manual_close`
```python
# Mark active slave trades closed (no close orders — likely gone too)
st.status = "closed"
```
The comment's assumption is wrong: slaves are **separate Delta accounts**. Master's
positions vanishing says nothing about theirs.

**Failure:** Master strangle closed on Delta at 02:00 (manual, bracket SL, liquidation).
Master booked at `exit_premium=0.0`, slaves flipped to closed. Customers' strangles stay
live with zero monitoring — no MTM (`update_all_slave_mtm` filters `active`), no SL
supervision, no pre-expiry close, no future mirror. Runs to expiry unmanaged.

**Fix:** `await mirror_exit(...)` first; mark closed only per-slave after that slave's
live option book is verified flat.

### 1.2 Naked-risk emergency close — same defect
`bot_engine.py:1153-1179` — `_emergency_close_remaining_leg`. Buys back master's
remaining leg, flips all slaves to closed, places no slave orders. The slave's bracket SL
may not have fired (different fill/qty), so the customer keeps one or both legs — the exact
naked state the master treats as an emergency.

### 1.3 Reconcile / zombie-heal bypass mirroring entirely
`trade_reconcile.py:330-345` consumed at `bot_engine.py:386-401`:
```python
closed_ids = list(recon.get("fully_closed") or [])
for tid in closed_ids:
    await ws_manager.broadcast({"type": "TRADE_CLOSED", ...})   # broadcast only
```
Master goes CLOSED and leaves the position tracker. `SlaveTrade` rows stay `active`, but
`check_slave_integrity` is driven **only** from `position_tracker.get_all_active()`
(`bot_engine.py:483-487`) — so its own "master not active → force close" branch is
unreachable. Permanent desync.

### 1.4 Startup audit destroys the evidence
`db_audit.py:136-181` CHECK 4 — on every boot, `active` slaves under a non-ACTIVE master
are flipped to `closed` with no Delta query. If the process died between master exit and a
failed slave mirror, restart erases the only record that the customer holds a position.
Also skips `error` rows entirely — the exact rows from the observed cascade.

### 1.5 Manual single-leg close never mirrors
`routes_trade.py:2253-2425` — `close_single_leg` has zero `mirror_engine` references.
Operator clicks "Close Call"; if the put was already closed, `finalize_trade_if_flat`
marks the master CLOSED. Customers keep live positions, master leaves the tracker,
integrity never runs again.

### 1.6 Emergency-exit "leftovers only" branch returns before mirroring
`routes_trade.py:2027-2050` — sets `EMERGENCY_CLOSED`, commits, `mark_closed()`, returns —
never reaching the `mirror_exit` call below. Hits exactly when master is in conversion mode
with shorts already closed externally.

### 1.7 Partial adjustment on master is never mirrored
`adjustment.py:1092-1118` returns `AdjustmentResult(success=False, is_partial=True)`.
`bot_engine.py:3211` only mirrors inside `if result.success:`.

**Failure:** Master buys back the runaway call, new strike entry fails. Master is
one-legged and protected. Every slave still holds the runaway short call at the original
strike. Customer eats the full continued loss on a leg the master already abandoned.

### 1.8 Exit marks slave closed even when zero closes succeeded
`mirror_engine.py:1652-1665`
```python
# Always mark closed in DB (positions may already be gone)
self._close_slave_trade(slave, slave_trade, reason=f"mirror_exit:{reason}", ...)
```
Slave API rate-limited/IP-blocked/margin-locked at exit → both the close and the retry
throw → only `logger.error` → row marked `closed`.

**This is the cascade generator.** On the next entry, `_resolve_entry_conflicts` builds its
"bot-owned" set from `status.in_(("active","error","partial","blocked_foreign_position"))`.
A wrongly-closed row is **excluded**, so the leftover real position is classified `foreign`
→ `blocked_foreign_position` → entry skipped. Each block writes another non-closed row.
Self-sustaining across every subsequent master trade until manual intervention.

---

# THEME 2 — Non-atomic operations: naked legs and position flips (CRITICAL)

### 2.1 Entry: put fails after call fills, no unwind
`mirror_engine.py:808-939`
```python
except Exception as exc:
    failed_trade = SlaveTrade(..., status="error", last_error=str(exc)[:500])
    db.add(failed_trade); db.commit()
```
No `close_position` of the filled call; `call_order_id` isn't even persisted. Every
exit/adjust/emergency path filters `status == "active"`, so the row is invisible. Customer
holds an unhedged short call protected only by a bracket SL, never exited when master exits.

Made worse by the integrity retry (2.6 below).

### 2.2 Manual attach: filled call with no DB record at all
`routes_slave.py:505-610` — `slave_trade = SlaveTrade(...)` is only reached after **both**
orders succeed. Put fails → HTTP 502 raised, no row created. A real filled short call
exists on the customer account with zero DB record anywhere in the system.

### 2.3 Conversion mode: three steps, no checks, no compensation
`mirror_engine.py:1210-1279`
```python
hedge_order = await client.place_order(..., side="buy")
await client.place_order(product_id=int(old_other_product_id), size=qty, side="buy")  # result discarded
new_order = await client.place_order(..., side="sell", bracket_stop_loss_price=new_sl)
except Exception as exc:
    slave_trade.last_error = str(exc)[:500]   # status stays "active"
```
Step 3 fails → slave holds triggered short + long hedge, **no other short leg** — a
directional position the strategy never intends, still marked `active`, still mirrored for
exits as healthy. Nothing retries.

**Backtest shows 229 of 333 baskets entered conversion mode. This is not an edge case.**

### 2.4 Hedge close without `reduce_only` — flips into a naked short
`mirror_engine.py:1326-1332`
```python
exists = await client.verify_position_exists(int(hedge_product_id))
if exists:
    await client.place_order(product_id=int(hedge_product_id), size=qty, side="sell")
```
Hedge long is 3 lots (partial IOC fill) but `actual_quantity` is 5 → SELL 5 → 3 close,
**2 open a new naked SHORT** at a near-ATM strike, untracked, no bracket SL. Same flip on
the benign race where the hedge closed between verify and order.
Contrast `delta_client.py:1801`: *"Close any position safely with reduce_only=True."*

### 2.5 Conversion old-leg buyback: no `reduce_only`, no existence check
`mirror_engine.py:1226-1231` — if that short is already gone (bracket SL fired, or an earlier
failed mirror), the unconditional BUY opens a **new long**, then a fresh short is sold in a
different strike. Slave ends long one option and short two.

### 2.6 `reduce_only` deliberately dropped on retry — two places
`mirror_engine.py:376-382` (conflict close) and `1623-1640` (exit close):
```python
except Exception as close_exc:
    # Retry without reduce_only (some accounts reject it)
    await client.place_order(product_id=pid, size=close_size, side=side)
```
`reduce_only` is the **only** guard against a close opening a new position. Rejection
usually means "no position to reduce" — i.e. already flat — so the retry reliably creates
exactly the position it was meant to remove. On a long hedge (`side="sell"`) it creates a
naked short.

### 2.7 IOC "no fill" is treated as success
`mirror_engine.py:1214-1232` — unfilled/partially-filled IOC returns a normal dict
(`state=cancelled`, `avg_fill_price=0`), not an exception. `place_order` returns
`{order_id, status, avg_fill_price, size, raw}` — the caller is expected to check, and does not.

Hedge BUY unfilled in an illiquid inside strike → execution continues → slave runs the
conversion structure **without the hedge that makes it safe**, while master reports it healthy.

### 2.8 Post-entry verification is too weak to catch a flip
`mirror_engine.py:840-860` + `delta_client.py:501-536`
```python
size = float(pos.get("size") or 0)
return size != 0
```
Checks existence only — not side, not size. A leftover 1-lot from a partial conflict-close
passes as "verified" for a 5-lot sell that was actually rejected. Bot then believes the slave
is short 5 and sends a 5-lot close on exit → **flips the customer long 4 lots**.

---

# THEME 3 — Fail-open error handling (HIGH)

### 3.1 Conflict check swallowed, entry proceeds anyway
`mirror_engine.py:763-768`
```python
except Exception as exc:
    logger.warning("Slave '%s' position check failed: %s — continuing", slave.name, exc)
```
Any failure inside `_resolve_entry_conflicts` (including its `db.commit()`) → falls through
to `place_order`. The bot stacks size onto a position that may be the **customer's own**;
the later exit closes the combined size, wiping out the customer's own trade. The `foreign`
guard is bypassed entirely. Must fail **closed**.

### 3.2 Position-fetch failure indistinguishable from a flat account
`mirror_engine.py:1557-1590`
```python
except Exception as pos_exc:
    live_positions = []      # error and "genuinely flat" collapse to the same state
```
Then the code fabricates targets from stale hint product_ids and (via 2.6) may open
brand-new positions. Return `None` vs `[]` and abort with an alert.

### 3.3 One bad slave aborts mirroring for all remaining slaves
`mirror_engine.py:685` (entry), `1722` (balances) — `self._get_slave_client(slave)`, which
calls `decrypt()`, sits **outside** the `try`, and `mirror_trade_entry`'s loop has no
per-slave guard. One rotated/corrupt Fernet key → `InvalidToken` unwinds the whole loop.
Slaves B, C, D get no orders, no rows, no error surfaced — silently flat while master is short.

### 3.4 SL cancel failure swallowed, order id then erased
`mirror_engine.py:1474-1482`, `1658-1660` — cancel fails → `logger.warning` → field set to
`None` regardless. A live stop order remains on the customer's account with the id
unrecoverable by the bot. Days later it fires on an unrelated position.

---

# THEME 4 — Fire-and-forget mirroring and races (HIGH)

### 4.1 Entry, conversion and hedge-close mirrors are unreferenced tasks
`routes_trade.py:1058`, `auto_trade_engine.py:867`, `adjustment.py:909`, `bot_engine.py:2828`
```python
asyncio.create_task(mirror_module.mirror_engine.mirror_trade_entry(...))
logger.info("Mirror task queued for trade %s", trade.id)
```
No strong reference, no `add_done_callback`. Any exception is swallowed by the event loop
and surfaces only as an unretrieved-task warning at interpreter shutdown. A brief DB lock
→ zero slaves get positions → master reported healthy → nobody alerted.

### 4.2 Entry task races the exit path
Entry mirror is still placing slave orders when SL fires. `mirror_exit` queries
`status == "active"`, finds none (entry hasn't committed), logs "no active slave_trades",
returns. Master closes. Entry task then commits an `active` row with **real open positions
under a CLOSED master** — later force-"closed" by the startup audit (1.4).

### 4.3 No locking anywhere in `mirror_engine`
No `asyncio.Lock`/semaphore in the file. `mirror_conversion` can run concurrently with
`mirror_exit`: exit closes the slave's positions, the still-running conversion task then buys
a hedge and sells a **new short** on a now-flat account after the strategy exited.

### 4.4 Awaited exit couples master risk to slave connectivity
`bot_engine.py:2098-2128` — `mirror_exit` iterates slaves serially with several blocking
Delta calls each and no timeout. Ten slaves, one hung connection → the master's own
stop-loss exit stalls for tens of seconds while losing money. Fan out with
`asyncio.gather` under `asyncio.wait_for`; on timeout mark stragglers for retry and proceed.

---

# THEME 5 — The data model cannot represent reality (CRITICAL, structural)

`models.py:375-420` — `SlaveTrade` columns are exactly:
`call_order_id, put_order_id, call_sl_order_id, put_sl_order_id, actual_quantity,
call_fill_price, put_fill_price, status, last_mtm, last_error, error_count`

Missing entirely:
- **hedge state** — no `hedge_product_id`, `hedge_order_id`, `hedge_fill_price`,
  `in_conversion_mode`. `Trade` has all five. `mirror_conversion` computes the hedge order
  id only to put it in a log line. A hedge left open after a failed close is
  **undiscoverable from the DB**.
- **product ids / symbols / strikes** — so after an adjustment the DB still describes the
  *original* strike. This is why `mirror_exit` has to guess from live positions, and why
  Theme 6.2's "close ALL positions" fallback exists at all.
- **exit prices, realized P&L, exit time** — slave close results are discarded
  (`closed_count += 1` and nothing else). Reported customer P&L is `last_mtm`, a stale
  whole-account UPNL sum that includes the customer's unrelated positions.
- **per-leg status and per-leg quantity** — so `status="partial"` carries no information
  about *what* is partial.

**Consequence:** divergence between master and slave is structurally unobservable. Fixing
the P&L/revenue layer later is impossible without this.

**Fix direction:** a `SlaveLeg` child table mirroring the master `Leg` model
(leg_type, product_id, symbol, strike, qty, entry/exit price, order ids, status, is_long).

### 5.1 `master_put_qty` accepted and ignored
`mirror_engine.py:485` takes it; sizing uses `master_call_qty` only and places both legs at
the same size. Master call 5 / put 3 → slaves 5/5 → ~66% more put exposure than the master
they mirror.

### 5.2 `master_qty` ignored in conversion
`mirror_engine.py:1167` accepts it, `1210` uses `slave_trade.actual_quantity` instead. A
stale stored qty produces an under- or over-hedged basket, and the parameter that would
reveal the mismatch is discarded.

### 5.3 Fill-price fallback silently adopts the master's price
`mirror_engine.py:788-795`, `817-824`
```python
if call_fill <= 0:
    call_fill = float(master_call_fill or 0.0)
```
Slave fills at 168 in a fast market, master at 150, `get_order` lags → 150 stored. Customer
is reported profitable when they are not. No warning logged.

---

# THEME 6 — Detection and recovery gaps (HIGH)

### 6.1 Integrity sweep is unreachable exactly when it is needed
`bot_engine.py:478-490` — `check_slave_integrity` is called from one place, iterating
`position_tracker.get_all_active()`, every 5th cycle, via unreferenced `create_task`. A
mirror exit fails → row goes `error` → master closes and leaves the tracker in the same
call → no integrity pass ever covers that trade id again. Its own "master no longer active
→ force close" branch is dead code in practice.

**Fix:** drive the sweep from a DB query (`SlaveTrade.status != 'closed'`), not the
in-memory tracker.

### 6.2 Exit can close the customer's own unrelated positions
`mirror_engine.py:1524-1556`
```python
targets = list(live_positions)
logger.warning("[MIRROR_EXIT] Slave '%s' — closing ALL %s live option positions ...")
```
Customer runs their own short call on a different expiry → lands in `extras` (`size < 0`)
→ force-closed at market. If the bot's own legs were already gone, the final branch closes
**every** live option position including the customer's longs. Direct violation of
"only touch positions the bot manages."

### 6.3 Integrity retry double-sells
`mirror_engine.py:1930-1975`
```python
st.status = "closed"  # clear slot so new entry can record
db.commit()
try:
    await self._mirror_entry_to_slave(...)
```
Row said `error, "Partial fill: call=True put=False"` — the call **is** live. Retry marks
the row closed and re-sells **both** legs → customer at 2× intended size, and the closed row
hides the evidence.

### 6.4 Integrity only asks "any position exists?"
`mirror_engine.py:2020-2045` — cannot detect partial closes, wrong strikes, wrong size,
wrong expiry, or foreign positions. Slave's put closed by bracket SL, call remains →
non-empty book → logged "integrity OK". The master-side equivalent
(`reconcile_open_legs_with_delta`) raises `naked_risk` and emergency-closes. Slaves get none
of that protection.

### 6.5 Earner is notified only about the slaves that FAILED
`bot_engine.py:2502-2545` — the Earner payload is built from `status == "active"`, but
`mirror_exit` ran earlier in the same `_exit_trade` call and set every **successfully
closed** row to `"closed"`. A normal profitable exit reports **zero** slaves. The only
slaves ever reported are the ones the mirror failed to close.

---

# THEME 7 — Lifecycle and admin operations (HIGH)

### 7.1 Pausing a slave freezes it at a stale strike
`mirror_engine.py:1001, 1206, 1317` all `continue` when `not slave.is_active`; `mirror_exit`
(`1394-1409`) does not filter. Subscription lapses mid-trade → master adjusts its runaway
call to a farther strike → the paused slave keeps the original short call as the underlying
trends → loss compounds without adjustment until master exits.

`is_active` is overloaded: "accept new entries" and "manage existing positions" are
different decisions. Split into `accepts_new_trades` / `is_managed`.

### 7.2 API key rotation mid-trade orphans the real account
`routes_slave.py:258-286` — PATCH re-encrypts credentials with no check for active
`SlaveTrade`s. On exit, `mirror_exit` authenticates with the **new** credentials, finds no
matching positions, marks the row closed. The original account's strangle is orphaned with
no record of which account holds it. DELETE (`295-318`) blocks on active trades but then
hard-deletes all historical rows.

### 7.3 Mid-trade attach sets the stop from the master's entry baseline
`routes_slave.py:479-512`
```python
call_base = float(getattr(call_leg, "trigger_baseline_premium", None) or ...)
call_sl = round(call_base * (uni_sl / 100.0), 2) if call_base > 0 else None
```
Master sold at $100, SL 200% = $200. Three hours later the call trades at $180 and a new
customer is attached: they sell at $180 with a stop at $200. An 11% adverse move stops them
out at a near-total loss on that leg while the master is merely at trigger. Also no
`is_demo` check — real customer orders can mirror a paper trade.

### 7.4 Virtual/paper guards missing in both conversion paths
`mirror_engine.py:1206-1215`, `1300-1320` — neither checks
`is_virtual_slave_trade()`. Every other mirror path guards this (`1026`, `1424`, `1798`,
`1925`, `2019`). A paper account gets **real** hedge buys, buybacks and shorts — on a path
that runs in 69% of baskets. And `is_virtual_slave_trade` then blocks automatic cleanup
(`75-85`, `2018`), so the real position can never be closed automatically.

### 7.5 Manual copy path places real orders for a virtual slave
`routes_slave.py:396-401` checks `is_virtual` only for balance sourcing, then falls straight
through to `place_order`.

---

# PHASED FIX PLAN

## Phase 1 — Stop abandoning customer positions
Single exit funnel. Every master-close path routes through one awaited function that
mirrors to slaves and verifies before marking closed.
Covers: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 6.1
**This is the phase that stops customers losing money today.**

## Phase 2 — Atomicity and position-flip safety
Covers: 2.1–2.8, 3.1–3.4
Never drop `reduce_only`; fail closed on any position-fetch failure; verify side and size,
not just existence; compensate on partial failure.

## Phase 3 — Data model
Covers: Theme 5, 6.2, 6.3, 6.4
`SlaveLeg` table + hedge fields + exit prices/realized P&L. Unblocks correct P&L, exact
exit targeting (no more "close everything"), and real integrity checking.

## Phase 4 — Concurrency and lifecycle
Covers: 4.1–4.4, 6.5, 7.1–7.5
Per-slave locks, awaited mirrors with error surfacing, `is_active` split, credential-rotation
guard, mid-trade attach rules, virtual guards.

## Phase 5 — Earner P&L and revenue
Only after Phases 1-3. Requires the data model from Phase 3 to be correct.
Known issue already identified: `routes_slave.py:871-886` overwrites the engine's real
Delta-fetched slave MTM with `master_mtm × qty_multiplier`, which is wrong for
capital-based slaves (production: 2.00x and 1.43x inflated) and flows into
`commissionAmount = tradeProfit × profitShare / 100`.
