# S001 — Which `auto_trade_settings` Fields Actually Govern Behaviour

**Status:** Read-only reference for the S001 simulator  
**Scope audited:** `backend/strategies/s001_short_strangle/*`, `backend/engine/*`, `backend/api/routes_auto_trade.py`, `backend/models.py`  
**Also cited where behaviour is implemented outside that tree:** `backend/core/hedge_theta.py`, `backend/core/time_utils.py`, `backend/core/spread_utils.py`, `backend/core/delta_sl.py` (called from engine / strategy — not alternative products)

This document describes **what the code does today**. It does not change settings or “fix” contradictions.

Line numbers are from `main` at the time of writing. Prefer symbol names if lines drift.

---

## 1. Strike selection (short basket)

### Competing fields

| Field | Role (name implies) |
|---|---|
| `strike_selection_mode` | `fixed_premium` vs `theta_based` |
| `target_premium_per_side` | Fixed $ premium per short side |
| `strangle_premium_mode` | `fixed` vs `pct_of_hedge` |
| `strangle_premium_pct_of_hedge` | % of live hedge mark → target $ |
| `trade_type` | `straddle` (ATM) vs `strangle` (premium match) |
| `theta_multiplier` | Hedge-call-θ × mult → required short θ |

### Which path executes?

Decision tree in `auto_trade_engine.py` (entry):

1. **`hedge_enabled` AND `strike_selection_mode == "theta_based"`**  
   → `select_theta_based_strikes(...)` using `theta_multiplier`.  
   - Gate: `auto_trade_engine.py:1042–1109`  
   - Ignores `trade_type` / `target_premium_per_side` / `strangle_premium_*` for strike pick.

2. **Else if `trade_type == "strangle"`**  
   → `resolve_strangle_target_premium(...)` then `client.find_strangle_by_premium(...)`.  
   - Call site: `auto_trade_engine.py:1176–1221`  
   - Resolver: `auto_trade_engine.py:279–356`

3. **Else** (`trade_type` straddle / default)  
   → ATM `find_atm_straddle` (`auto_trade_engine.py:1222–1231`).

### `fixed_premium` vs `pct_of_hedge` — not alternatives to each other

- `strike_selection_mode = "fixed_premium"` only means **not** the theta path. It does **not** force `target_premium_per_side`.
- Inside the strangle branch, **`strangle_premium_mode`** chooses the dollar target:
  - `"fixed"` (or anything ≠ `pct_of_hedge`) → `target_premium_per_side` (`:293–298`).
  - `"pct_of_hedge"` → `ceil(avg(hedge_call_mark, hedge_put_mark) × strangle_premium_pct_of_hedge / 100)` (`:334–356`).  
    Marks come from live hedge asks via `get_hedge_theta` (`:1179–1198`).

### Fallback when `pct_of_hedge` cannot run

`resolve_strangle_target_premium` falls back to **`target_premium_per_side`** and logs `STRANGLE_PREMIUM_FALLBACK` when:

| Condition | Code |
|---|---|
| `hedge_enabled` is false | `:317–318` |
| Call/put marks missing or ≤ 0 | `:328–332` |
| Computed target ≤ 0 | `:337–345` |

So with the example pair  
`strike_selection_mode=fixed_premium`, `target_premium_per_side=150`,  
`strangle_premium_mode=pct_of_hedge`, `strangle_premium_pct_of_hedge=25`:

- **Live path** (if `trade_type=strangle` and hedge on with valid marks): **% of hedge**, not 150.  
- **150 is the fallback**, not a parallel live target.  
- If `trade_type=straddle`, neither premium field selects strikes (ATM path).

---

## 2. Basket profit target

### Competing fields

| Field | Intended meaning |
|---|---|
| `target_mode` (`payoff_pct` \| `theta_multiplier`) | Legacy UI / preview label |
| `theta_multiplier` | Used for **strike** θ-matching, not basket $ target |
| `basket_target_mode` (`THETA` \| `PCT`) | **Live** target formula selector |
| `basket_target_multiple` | Multiplier on θ (THETA mode) |
| `tp_pct` | % of net credit (PCT mode / THETA fallback) |

### What is compared at runtime?

Monitor loop / `on_tick` compares:

- **Quantity:** `decision_pnl` = **Net MTM** (gross − fees − slip)  
- **Against:** `trade.profit_target_usd` (locked at entry)  
- Gate: `logic.py:625–629`, `698+`  
- Priority: after STOPLOSS; skipped while settling (`:687–696`).

`profit_target_usd` is computed once at auto-entry by  
`compute_basket_profit_target_at_entry` (`core/hedge_theta.py:449+`), called from  
`auto_trade_engine.py:1940–1973`.

### Order of formulas (`hedge_theta.py:496–638`)

1. Read `basket_target_mode` → normalize to `THETA` or else `PCT` (`:496–499`).
2. Seed `raw_target = credit_usd × tp_pct / 100` (`:572`) — always computed first.
3. **If THETA + wings on:** try net option θ (shorts − wings); if successful, overwrite with  
   `basket_target_multiple × |net_θ| × qty × CONTRACT_SIZE` (`:574–612`).
4. **Else if THETA and hedge present:**  
   `basket_target_multiple × |hedge total_θ| × qty × CONTRACT_SIZE` (`:619–628`).
5. **If THETA fails** (no θ / hedge fail): keep PCT seed (`:629–638`).
6. Cap: if raw > `max_achievable`, set target to `0.9 × max_achievable` (`:645–647`).

### Verdict for the example triple

`target_mode=theta_multiplier`, `theta_multiplier=4.0`,  
`basket_target_mode=PCT`, `basket_target_multiple=2.0`, `tp_pct=40`:

| Field | Live basket TP? |
|---|---|
| `basket_target_mode=PCT` | **Yes — selects PCT path** |
| `tp_pct=40` | **Yes — `target = credit × 0.40`** |
| `basket_target_multiple=2.0` | Read but **unused in PCT mode** (still returned in audit dict) |
| `target_mode` | **DEAD for live TP** (persisted + API only; not read in `compute_basket_profit_target_at_entry`) |
| `theta_multiplier=4.0` | **Not used for TP**; only strike path when `strike_selection_mode=theta_based` |

`stoploss_usd` is separate: always `initial_max_profit × sl_pct / 100` at entry  
(`auto_trade_engine.py:1908`) — not routed through `basket_target_mode`.

---

## 3. Stoploss — `sl_pct` vs `universal_sl_pct`

These are **different instruments**, not two candidates for the same check.

### `sl_pct` → basket $ stop (`stoploss_usd`)

| | |
|---|---|
| Set at entry | `stoploss_usd = initial_max_profit × sl_pct / 100` — `auto_trade_engine.py:1908` |
| Measured against | **Basket Gross MTM for SL** = gross PnL + latest entry-event spread add-back (`logic.py:605–631`; built in `bot_engine.py` ~3239–3278) |
| Scope | Whole short basket (shorts ± wings in PnL); **not** per-leg exchange stop |
| Trigger | `sl_mtm <= -stoploss_usd` → `EXIT STOPLOSS` (`logic.py:631, 645–685`) |
| Settling | **Never suppressed** by settling window |

### `universal_sl_pct` → per-leg Delta bracket stop

| | |
|---|---|
| Copied onto trade | `auto_trade_engine.py:1372, 2025` |
| Formula | `stop = baseline_premium × (universal_sl_pct / 100)` — `core/delta_sl.py:16–35, 102–106` |
| Scope | **Each short leg** exchange stop-trigger / bracket |
| Placement | Entry brackets `auto_trade_engine.py:1827–1860`; re-armed on adjust `adjustment.py:2050, 2404+` |

### Which triggers first?

- **Independent.** Soft basket SL (`sl_pct` → `stoploss_usd`) is evaluated every monitor tick on Net/Gross MTM.  
- Exchange SL (`universal_sl_pct`) can fill on Delta whenever that leg’s mark hits the bracket — may close a leg **before** basket MTM hits `stoploss_usd`, or never fire if MTM exit wins first.  
- Example `sl_pct=100` vs `universal_sl_pct=250`: basket soft-stop at **100% of entry credit** (loss); exchange leg stop at **2.5× leg baseline premium**. Soft basket SL is usually “tighter” in $ terms for a full structure, but a single exploding leg can still hit the exchange stop first.

There is **no** code path that compares `sl_pct` and `universal_sl_pct` against the same quantity and picks a winner.

---

## 4. Wings — which knobs are live in `points` mode?

Selector: `wing_select.resolve_wing_strikes` (`wing_select.py:277–359`).

`wing_strike_mode` normalized (`:15–20`); then:

| Mode | Live inputs | Dead in that mode |
|---|---|---|
| **`points`** (default / unknown) | `wing_points_away` only (`:329–335`, `_pick_points_*`) | `wing_delta_min/max`, `wing_pct_of_premium` |
| `delta` | `wing_delta_min`, `wing_delta_max` | `wing_points_away`, `wing_pct_of_premium` |
| `pct_of_premium` | `wing_pct_of_premium` (+ short premiums) | `wing_points_away`, deltas |

Call site still **passes all four** into `resolve_wing_strikes` (`auto_trade_engine.py:1449–1462`; adjust path `adjustment.py:1641–1654`), but unused args are ignored by mode branch.

With `wing_strike_mode=points`, `wing_points_away=2000`: **live**.  
`wing_delta_min/max` and `wing_pct_of_premium=30`: **dead for selection** until mode changes.

Master switch: `basket_wings_enabled` must be true or wings are not placed (`auto_trade_engine.py:1409–1428`).

---

## 5. Adjustment trigger — premium mode, time slabs, Adj A vs Adj B

### Time slabs vs premium slabs

`get_trigger_for_leg` (`logic.py:406–430`):

| `trigger_mode` | Source |
|---|---|
| `flat` | `flat_trigger_pct` |
| `slab` | Time slabs via `get_trigger_pct(hours_left, slabs)` (`time_utils.py:411–426`) |
| `premium` | Premium slabs via `get_premium_trigger_pct(leg_premium, slabs)` (`time_utils.py:429+`) |

**In `trigger_mode=premium`, `slab_24h` / `_12h` / `_6h` / `_lt6h` do not affect the trigger %.**  
They are still **copied onto the trade’s `settings` rows at entry** always (`auto_trade_engine.py:2324–2331`), alongside premium slabs only when mode is premium (`:2334–2341`). Stored ≠ used.

### `adjustment_mode = BOTH` — Adj A vs Adj B precedence

Flags (`logic.py:812–814`):

- `allow_adj_a` if mode ∈ `{A_ONLY, BOTH}`
- `allow_adj_b` if mode ∈ `{B_ONLY, BOTH}`

**Same monitor cycle — exact order:**

1. Exits (SL → settling skip → TP → pre-expiry → decay) first.  
2. Combined-trigger branch (if on): Adj A on combined hit (`:1053–1064`); if under threshold, **only then** Adj B (`:1075–1102`). Combined mode **suppresses** individual-leg Adj A (`:1065–1066`).  
3. Individual mode: check **CALL Adj A**, then **PUT Adj A** (`:1110–1231`). Each successful Adj A **`return`s immediately**.  
4. **Only if no Adj A fired:** try Adj B (`:1233–1260`).

**Precedence when BOTH would be eligible:** **Adj A wins** — Adj B is never evaluated on that tick once Adj A returns.  
Adj B can still fire on ticks where Adj A has not crossed its trigger % yet (Adj B pressure uses ≥100% of baseline — `_try_adj_b_action` `:142–146` — which can be below Adj A’s premium/% slab threshold).

---

## 6. Hedge lifecycle (simulator must match)

### `hedge_expiry_mode = month_1` → concrete expiry

`resolve_hedge_expiry_date` (`core/hedge_theta.py:128–201`):

1. Migrate legacy keys (`migrate_hedge_expiry_mode`).  
2. Fetch Delta expiries; pick the row whose `key == "month_1"` (`:191–201`).  
3. Optionally advance if calendar DTE &lt; `min_hedge_dte` when `min_hedge_dte_enabled` (`enforce_min_hedge_dte`, `:204+`; wired from `hedge_lifecycle.py` open path ~659–682).

### Roll sequence (`hedge_lifecycle.py` ~3183–3263)

Requires `hedge_roll_enabled` (and force path uses `hedge_force_roll_enabled`):

| Stage | Condition | Action |
|---|---|---|
| Soft roll | `status=active` and `calendar_dte ≤ hedge_roll_dte` | Set `pending_close` (`:3209–3227`) |
| Hard roll | `pending_close` and `calendar_dte ≤ hedge_roll_hard_dte` and force enabled | Close reason `HEDGE_ROLL` (`:3230–3247`) |
| Soft execute | `pending_close` and **no** open baskets under hedge | Close `HEDGE_ROLL` (`:3251–3263`) |
| Wait | `pending_close` with open baskets and DTE &gt; hard | Log `HEDGE_ROLL_WAIT`, do not close yet |

If `hard_dte >= roll_dte`, hard is clamped to `roll_dte - 1` (`:3197–3198`).

Example values `hedge_roll_dte=3`, `hedge_roll_hard_dte=2`: pending at DTE≤3; force-close at DTE≤2 if baskets still open.

### `hedge_min_hold_days`

Blocks **structure target** booking until `days_held >= min_hold` (`hedge_lifecycle.py:2841–2890`).  
Does **not** block stoploss / expiry / roll closes (roll path is separate; SL/target/expiry close immediately — comment `:3179`).

### `hedge_auto_reopen_after_roll`

After a successful close with reason `HEDGE_ROLL`, `maybe_auto_reopen_after_roll` (`:1937–2013`) opens the next hedge **iff** `hedge_auto_reopen_after_roll` and `hedge_enabled`. Failure does **not** retry; leaves no active hedge (basket entry stays blocked).

---

## 7. Basket quantity

### Entry: `basket_qty_mode=pct_of_hedge`, `hedge_qty_lots=4`, `basket_qty_pct_of_hedge=200`

`resolve_sizing_mode` (`auto_trade_engine.py:371+`): returns `pct_of_hedge` only if mode requested **and** `hedge_enabled` **and** `hedge_qty_lots > 0`; else falls back to `fixed`.

When live:

```
hedge_qty = active hedge row quantity  # normally equals hedge_qty_lots at open
basket_qty = ceil(hedge_qty × basket_qty_pct_of_hedge / 100)   # resolve_basket_qty_from_hedge :39–48
```

Example: 4 × 200% → `ceil(8)` = **8 lots** (`:1293`).

Optional: if `basket_qty_dynamic`, pct may be overwritten by θ formula (`resolve_entry_basket_pct` `:72–107`) before the ceil.

`fixed` mode uses `settings.quantity` and `hedge_qty_ratio` instead (`~2996–3010`).

### Adjustments: `adjustment_qty_decrease_pct=20`, mode `decrease_step`

`resolve_adjustment_basket_qty` (`:110–177`) + `compute_decrease_step_qty` (`wing_entry.py:332–353`):

```
remaining = 1 − (decrease_pct/100) × adj_n
new_qty   = max(1, floor(original_basket_qty × remaining))
# remaining ≤ 0 → close basket (no adjust)
```

**Critical (CLAUDE.md + code):**

- `original_qty` = fresh SQL `trades.original_basket_qty` (`adjustment.py:1390–1409`) — never current leg qty.  
- `adj_n` = fresh SQL `adjustment_count + 1` (`:1429–1442`) — never stale in-memory `trade.adjustment_count`.

Example orig=8, pct=20: adj1 → floor(8×0.8)=6; adj2 → floor(8×0.6)=4; adj3 → floor(8×0.4)=3; … until remaining≤0 closes.

Default `adjustment_qty_mode=unchanged` leaves qty alone unless set to `decrease_step` / `increase_dynamic`.

---

## 8. Wing roll + cross guard

### When does a wing roll? (`wing_roll_with_short_enabled=1`)

On Adj A strike pick (`logic.py:1532–1579`) and Adj B plan (`adjustment.py:3150–3173`):

- Open wing strike exists for that side.  
- New short would **cross** the wing (call: `new >= wing`; put: `new <= wing`).  
- `wing_roll_with_short_enabled` true → set `wing_roll=True`, **skip clamp**, executor closes/reopens wing at entry distance.

If roll flag is **false** and short would cross → `clamp_short_strike_inside_wing` (`wing_exit.py:378–443`):

- Keeps short **strictly inside** wing (call: short &lt; wing_call; put: short &gt; wing_put).  
- Picks farthest legal strike toward wing; `dead_end` if none → adjustment cannot place that strike.

---

## 9. Exit / entry order (confirmed from code)

### Entry (live auto path) — **hedge → wings → shorts**

Documented in-engine (`auto_trade_engine.py:937–1022`, `:1574–1600`):

1. Structure hedge open (position 1) if `hedge_enabled`.  
2. Wing pair group (position 2).  
3. Short pair group (position 3).

Wings before shorts inside the basket plan (`build_entry_order_plan` + group loop).

### Exit (basket close via `order_executor`) — **shorts → wings** (→ conversion hedge)

`EXIT_SEQUENCE = ("short", "wing", "hedge")` (`logic.py:1848`).  
Executor walks phases (`order_executor.py:388–495`): shorts first; wings blocked until both shorts closed; then conversion-hedge legs if present.

STOPLOSS may take a **parallel market** path that closes all at once (`:396–413`) — sequencing skipped.

### Surprise vs comment

`logic.py:1844–1847` comment / `ENTRY_SEQUENCE` say **Wings → Shorts → Hedges**.  
That tuple is **not** what auto-entry does for the **structure** hedge (hedge is opened first, outside that tuple). Treat **`auto_trade_engine` + `order_executor` as source of truth**, not `ENTRY_SEQUENCE` for structure hedge ordering.

---

## 10. Read map — every `auto_trade_settings` column

Legend:

- **LIVE** — read on a hot path that changes positions / exits / sizing / triggers  
- **PREVIEW** — used in strategy/API preview only (not monitor loop)  
- **PERSIST** — written/returned by `routes_auto_trade.py` only; no behavioural read found in engine/strategy  
- **DEAD** — no behavioural consumer found in scoped code (+ core helpers used by engine)

| Setting | Status | Primary read(s) → controls |
|---|---|---|
| `is_enabled` | LIVE | `auto_trade_engine` entry loop — arm/disarm auto entry |
| `underlying` | LIVE | Entry / hedge / chain underlying |
| `expiry_dte` | LIVE | Short-basket expiry resolution |
| `expiry_date_override` | LIVE | Weekly/monthly short expiry override |
| `quantity` | LIVE | Fixed basket sizing mode |
| `re_entry_delay_minutes` | LIVE | Delay after exit before next entry |
| `entry_settling_seconds` | LIVE | `monitoring_starts_at` after entry (`auto_trade_engine.py:1991–1997`) |
| `adjustment_settling_seconds` | LIVE | Post-adjust settling window |
| `tp_pct` | LIVE | PCT basket target (+ THETA fallback seed) |
| `sl_pct` | LIVE | `stoploss_usd` at entry |
| `universal_sl_pct` | LIVE | Per-leg Delta bracket stop |
| `slippage_pct` | LIVE | Net MTM slip assumption |
| `trigger_mode` | LIVE | flat / slab / premium trigger source |
| `combined_trigger_mode` | LIVE | Combined vs per-leg Adj A (`logic.py:909+`) |
| `flat_trigger_pct` | LIVE | When `trigger_mode=flat` |
| `slab_24h` / `slab_12h` / `slab_6h` / `slab_lt6h` | LIVE if `trigger_mode=slab`; **dead for trigger if `premium`** | Time trigger %; still persisted onto trade settings at entry |
| `premium_slab_300` … `premium_slab_lt100` | LIVE if `trigger_mode=premium` | Premium-band trigger % |
| `trade_type` | LIVE | straddle vs strangle entry |
| `target_premium_per_side` | LIVE | Fixed strangle target / pct_of_hedge fallback |
| `strangle_premium_mode` | LIVE | fixed vs pct_of_hedge |
| `strangle_premium_pct_of_hedge` | LIVE | Dynamic strangle $ target |
| `adj_low_premium_exit_enabled` | LIVE | Conversion / low-prem exit gate (adjust path) |
| `adj_low_premium_min_usd` | LIVE | Threshold for that gate |
| `conversion_equality_pct` | LIVE | Conversion unwind equality (`bot_engine.py:5259, 6937`) |
| `conversion_mode_enabled` | LIVE | Allow conversion vs force exit |
| `max_adjustments_per_basket` | LIVE | Cap when conversion off |
| `premium_cover_loss_enabled` | **DEAD** | Persisted in routes; only a comment in `adjustment.py:538` |
| `hedge_enabled` | LIVE | Structure hedge on/off; gates sizing / premium / θ |
| `hedge_expiry_mode` | LIVE | Relative expiry key (`month_1`, …) |
| `hedge_expiry_date_override` | LIVE | Display / legacy date mode |
| `hedge_expiry_dte` | LIVE | Legacy dte migrate input |
| `min_hedge_dte` | LIVE | Floor before accepting hedge expiry |
| `min_hedge_dte_enabled` | LIVE | Enable that floor |
| `hedge_target_usd` / `hedge_stoploss_usd` | LIVE (legacy display / some hedge paths) | Prefer budget formula fields below for structure target/SL |
| `hedge_fixed_sl_usd` | LIVE | Hedge SL budget floor inputs |
| `hedge_sl_floor_pct` | LIVE | Min budget as % of fixed SL |
| `hedge_roll_dte` | LIVE | Soft roll → `pending_close` |
| `hedge_roll_hard_dte` | LIVE | Hard force roll close |
| `hedge_roll_enabled` | LIVE | Soft roll arm |
| `hedge_force_roll_enabled` | LIVE | Hard roll arm |
| `hedge_close_at_expiry_enabled` | LIVE | Pre-expiry hedge close |
| `hedge_auto_reopen_after_roll` | LIVE | Open next monthly after roll |
| `hedge_target_multiple` | LIVE | Structure target = monthly × multiple |
| `hedge_expected_monthly_pct` | LIVE | Monthly $ from entry cost |
| `hedge_min_hold_days` | LIVE | Blocks structure **target** fire only |
| `spread_mode` | LIVE (via `core/spread_utils.py`) | MANUAL vs AUTO exit-spread estimate |
| `basket_exit_spread_pct` | LIVE (spread_utils + target friction fallback) | Basket exit-spread % / TP friction |
| `hedge_exit_spread_pct` | LIVE | Hedge exit-spread estimate |
| `spread_cap_pct` | LIVE (`spread_utils._spread_cap_pct`) | Cap on AUTO measured spread |
| `margin_buffer_pct` | **PREVIEW** | `routes_strategy.py` margin preview — not monitor loop |
| `strike_selection_mode` | LIVE | theta_based vs premium/ATM branch |
| `theta_multiplier` | LIVE | Theta strike selection only |
| `target_mode` | **DEAD (live TP)** | Persisted + validated in routes; not read by entry TP |
| `target_theta_pct` | **PREVIEW** | `routes_strategy.py` TARGET_THETA preview |
| `basket_target_mode` | LIVE | THETA vs PCT at entry (`hedge_theta.py:497`) |
| `basket_target_multiple` | LIVE | THETA target multiple |
| `hedge_qty_ratio` | LIVE | Fixed-mode hedge lots from basket qty |
| `basket_qty_mode` | LIVE | fixed vs pct_of_hedge |
| `basket_qty_pct_of_hedge` | LIVE | Basket lots = ceil(hedge × pct/100) |
| `hedge_qty_lots` | LIVE | Fixed hedge size for pct_of_hedge |
| `basket_qty_dynamic` | LIVE | θ-derived basket % |
| `basket_qty_theta_mult` | LIVE | Dynamic % formula |
| `use_dynamic_qty_on_adjustment` | LIVE (deprecated) | Migrated into `adjustment_qty_mode` resolver |
| `adjustment_qty_mode` | LIVE | unchanged / increase_dynamic / decrease_step |
| `adjustment_qty_decrease_pct` | LIVE | Step-down pct for decrease_step |
| `adjustment_mode` | LIVE | A_ONLY / B_ONLY / BOTH |
| `adj_b_trigger_pct` | LIVE | Untested-side decay threshold for Adj B |
| `min_short_gap_points` | LIVE | Adj B gap guard (`adj_b.py` / `adjustment.py:3018`) |
| `basket_decay_exit_enabled` / `_pct` / `_mode` | LIVE | Premium-decay exit (`logic.py:731–741`) |
| `cooldown_after_loss_minutes` | LIVE | Post-loss re-entry cooldown |
| `adjustment_premium_tolerance_pct` | LIVE | Adj strike premium match tolerance |
| `entry_premium_match_tolerance_pct` | LIVE | Entry θ / ATM match warn tolerance |
| `basket_wings_enabled` | LIVE | Place long wings |
| `wing_strike_mode` | LIVE | points / delta / pct_of_premium |
| `wing_points_away` | LIVE if mode=points | Points OTM from short |
| `wing_delta_min` / `wing_delta_max` | LIVE if mode=delta | Else unused |
| `wing_pct_of_premium` | LIVE if mode=pct_of_premium | Else unused |
| `wing_roll_with_short_enabled` | LIVE | Roll wing vs clamp on cross |
| `midprice_enabled` + chase/hold/partner | LIVE | Mid-price entry path |
| `is_demo` | LIVE | Virtual fills |
| `last_trade_id` / `last_exit_time` / `next_entry_time` / `next_entry_source` / `retry_count` / `last_error` | LIVE | Auto-entry state machine |
| `usd_inr_rate` | LIVE | Display / INR conversion helper |
| `updated_at` | LIVE | Row timestamp |

---

## AMBIGUITIES AND SURPRISES

1. **`target_mode` / `theta_multiplier` look like basket TP knobs but are not.** Live TP is `basket_target_mode` + (`basket_target_multiple` \| `tp_pct`). `target_mode` is effectively dead for monitor behaviour; `theta_multiplier` is strike-only.

2. **`strike_selection_mode=fixed_premium` does not mean “use `target_premium_per_side`.”** Strangle $ target is owned by `strangle_premium_mode`. Easy to simulate the wrong selector.

3. **Time slabs are written even in premium mode** (`auto_trade_engine.py:2324–2331`) but **never consulted** for triggers when `trigger_mode=premium`. Looks “configured” in DB/trade settings while dead.

4. **`ENTRY_SEQUENCE` / comment disagree with live entry.** Comment says Wings→Shorts→Hedges (`logic.py:1844–1847`); auto entry does **Hedge→Wings→Shorts**. Simulators must follow `auto_trade_engine`, not the constant.

5. **`min_short_gap_points = 0` (model default / common live).** Code treats 0 as “one strike step” minimum (`models.py:510–512`; Adj B gap logic). Suspected ops intent was **2000** points — if so, Adj B can roll much closer than expected. **Not changed.**

6. **`hedge_exit_spread_pct` often far below live L2 spreads.** Configured MANUAL % (defaults 4.0 in model; ops has reported **0.4** while live hedge books show **~1.8–2.5%**). Affects hedge MTM / structure target friction via `spread_utils`, so targets can look reachable on paper while exits pay more. **Not changed.**

7. **`sl_pct` vs `universal_sl_pct` dual stops** can race; no single “structure SL %” unifies them.

8. **`premium_cover_loss_enabled` is DEAD** for behaviour (persist/UI only).

9. **`margin_buffer_pct` is preview-only** in `routes_strategy` — does not resize live orders.

10. **Wing delta/pct fields remain populated in DB while `wing_strike_mode=points`** — inert until mode flips; easy false confidence in config dumps.

11. **Adj A preempts Adj B on the same tick** under `BOTH`; Adj B only runs if Adj A did not return. Not a simultaneous merge.

12. **Basket TP uses Net MTM; basket SL uses Gross MTM + entry-spread add-back.** Simulators that use one PnL series for both will diverge from live.

---

*End of S001_CONFIG_SEMANTICS.md — report only; no settings were modified.*
