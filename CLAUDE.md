# Tradeict Short Strangle Bot — Claude / Agent Notes

Full project knowledge: `../PROJECT_KNOWLEDGE.md`
Session records live under `claude/`.

Read this file before touching anything. It exists because several fixes here
were made twice — once wrongly, on an assumption.

---

## HARD RULES

1. **Never assume a cause. Read the code or run a command first.** Fixing on an
   assumption both misses the real bug and damages working code.
2. **Never guess a column, path, table or field name.** Read the model. Names that
   were guessed and do not exist: `adjustment_trigger_pct`, `slave_accounts.capital_usd`,
   a global `basket_seq`, `/root/trading-bot`.
3. **`settings` is a PER-TRADE key-value table** (`trade_id` FK). Global config is
   `auto_trade_settings`, singleton `id=1`, stored **as columns**.
4. **The server only ever pulls. Never `git add -A` on the server.**
5. **Only `log_and_buffer` reaches `bot_activity.log`.** Plain `logger.*` does not.
6. **Adjustment must never use mid-price.** It is an emergency path — market orders
   only. `MIDPRICE_ALLOWED_REASONS` is deny-by-default and deliberately excludes it.
7. **Pin every new dependency.** An unpinned `passlib[bcrypt]` pulled bcrypt 5.0 and
   took the server down at startup with a live trade open.
8. **Never restart the bot mid-adjustment.** Otherwise restart is safe — state
   reloads from the DB.
9. **All timestamps UTC in the DB, IST only for display.** Expiry is 17:30 IST.
10. Async throughout. Type hints on every function. No bare `except`.
11. **Never date a past event from a present snapshot, and never run raw SQL against a
    table you are still investigating — it bypasses ORM `updated_at` and destroys the
    timeline.**

---

## CORRECTION to c592c97's commit message

c592c97 claims "paused slaves received real orders" and cites slave 3 receiving
an 11-lot hedge on 2026-09-06. **That did not happen.** The claim was built from a
slave_accounts snapshot taken on 2026-09-08, two days after the event, and the
investigator then ran a raw SQL `UPDATE slave_accounts SET is_active=0` which
bypassed the ORM's `updated_at` and destroyed the only column that could have
dated the change.

The code settles it: `database.py:1201 get_active_slave_accounts()` filters
`SlaveAccount.is_active.is_(True)`, and `mirror_engine.py:4526` uses it for the
hedge-open path with `slaves_total = len(slaves)`. The 2026-09-06 log recorded
`slaves_total=3`, so three slaves genuinely were active at that moment. The
open-path pause gate was already correct.

What WAS real, and is what c592c97 actually fixes:
- a paused slave could not CLOSE an existing position (cascade close, leg close and
  conversion-hedge close all skipped paused slaves) — this trapped customers in
  positions they could not exit, and is the more dangerous defect
- MTM skipped paused slaves, so a paused account holding a live hedge went dark
- `_active_trade_count` counted only SlaveTrade, so a hedge-only position read as
  "No active trade"
- externally-closed slave positions stayed `active` in the DB indefinitely: slave 1's
  whole structure was closed on Delta at 15:21:48 IST on 2026-09-06 and the DB still
  read `qty=29 active` on 2026-09-08, with SLAVE_HEDGE_MTM updating phantom positions
  for two days

The `is_active` open-gates c592c97 added are harmless duplicates of an existing
check; keep them, but do not cite them as a fix for a real incident.

---

## ENVIRONMENT

| Thing | Value |
|---|---|
| Server repo | `/home/botuser/trading-bot` |
| DB | `/home/botuser/trading-bot/trading_bot.db` (a stale copy at `frontend/trading_bot.db` — never use it) |
| Python | `/home/botuser/.venv/bin/python3` — **the venv is outside the repo**; system `python3` lacks deps |
| Process | supervisord, program `trading-bot`, uvicorn on 127.0.0.1:8000 |
| Log | `/var/log/trading-bot/error.log` |

Import check before any restart:

```bash
cd /home/botuser/trading-bot && \
PYTHONPATH=/home/botuser/trading-bot /home/botuser/.venv/bin/python3 \
  -c "import backend.main; print('IMPORT OK')"
```

A `&&` chain stops at the first failure and the rest silently does not run. A
`python -c ... && supervisorctl restart` once looked like a deploy — `python` did not
exist, so nothing restarted and the bot kept running old code.

---

## STRUCTURE

One structure = long ATM straddle hedge + short strangle basket + long wings
(iron condor). Entry order is **sequential by group** for the margin rule —
hedge, then wings, then shorts — and **parallel within a pair**
(`execute_paired_legs`, `asyncio.gather`). Exit is shorts first, wings second.

`trades.status` is lowercase: `active` / `closed` / `emergency_closed`.

### Basket qty is NOT `settings.quantity`

With `basket_qty_mode='pct_of_hedge'`:

```python
hedge_qty  = max(1, int(settings.hedge_qty_lots))
basket_qty = resolve_basket_qty_from_hedge(hedge_qty, pct)   # 2 x 200% = 4
```

`settings.quantity` is ignored in that mode.

### Adjustment qty — `decrease_step`

```python
remaining = 1.0 - (pct / 100.0) * float(adj_n)
new_qty   = max(1, int(math.floor(orig * remaining)))
```

`adj_n` MUST come from a fresh DB read of `adjustment_count`. Reading it from the
in-memory `trade` object gave `adj_n=1` on two consecutive adjustments, so the qty
never stepped down and the reconciler repaired it with wasted orders (`cec5370`).

**Both legs are reduced by the adjustment itself, together with the new short entry
— not afterwards, and never by the reconciler.** If `QTY_RECONCILE_CORRECTED` appears
during an adjustment, the adjustment did not finish its job. That is the bug signal.

### Trigger

`trigger_mode` is one of `flat` / `slab` (time-based) / `premium`. Premium bands
(`core/time_utils.py:429`): `>=300 / >=200 / >=100 / <100` map to
`premium_slab_300 / _200 / _100 / _lt100`.

**Never set a trigger below 100%.** The baseline resets to each new leg's entry
premium and the fresh offer is ~101% of it, so the leg re-triggers forever.

### Wings

`wing_strike_mode` is `points` or `delta`. The cross guard lives in
`strategies/s001_short_strangle/logic.py:1321` — `clamp_short_strike_inside_wing`,
logging `WING_CROSS_GUARD` / `_TOLERANCE_BYPASS` / `_ABORT`. It aborts; it does not
warn and continue.

**Wing roll has no ON/OFF setting.** It is driven by `plan.wing_roll` from the clamp
logic — whenever a short crosses its wing, the roll runs. It cannot be disabled from
settings.

### Net MTM — one canonical source

`core/fees.py:205 compute_net_mtm`:
