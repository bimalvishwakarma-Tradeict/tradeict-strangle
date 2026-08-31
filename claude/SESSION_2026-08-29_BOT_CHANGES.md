# Session Record — 29 August 2026
## Delta Exchange India Short Strangle Bot (Tradeict)
### B1–B13. Read this BEFORE `docs/HEDGE_MODE_SPEC.md` — hedge SL basis and basket sizing sections there are stale.

---

# PART 1 — COMMIT INDEX

| Task | Commit | Summary |
|------|--------|---------|
| B1 | `c6beea0` | DB columns: `basket_qty_mode`, `basket_qty_pct_of_hedge`, `hedge_qty_lots` (default `fixed`, no behaviour change) |
| B2 | `1ea6733` | Engine: `pct_of_hedge` mode — hedge qty from `hedge_qty_lots`, basket qty = `ceil(hedge × pct / 100)` |
| B3 | `7cc115b` | ATM-anchored straddle: CALL pinned at ATM, PUT premium-matched over full chain (OTM puts only) |
| B4a | `89c2dd4` | Log-only `structure_gross_for_sl` — hedge + open baskets, spreads neutralised |
| B4b | `0fc7c41` | **SL decision switched to structure-wide basis** — `room = budget + structure_gross_for_sl` |
| B5 | `b34190b` | Auto Trade UI: SHORT BASKET SIZING block; straddle label corrected |
| B6 | `f2dfbeb` | HedgePanel: spread breakdown + structure SL basis in live card |
| B6a | `bfabd7e` | Breakdown math fix — entry spread in SL add-back ladder, no double-count |
| B7 | `b78332c` | Dynamic basket qty % from hedge call theta at entry; audit column on Trade |
| B8 | `72fb8a7` | Auto Trade UI: `basket_qty_dynamic` toggle + `basket_qty_theta_mult` input |
| B9 | `bc368a4` | ✅ live | Structure-wide target: `structure_pnl = hedge_net + entry_spread + booked_closed + open_basket_gross`. Fires when structure_pnl >= target_pnl. UI label updated to "Target (structure basis)". `hedge_lifecycle.py` mein `compute_structure_pnl()` added. 29 tests pass |
| B10 | `c434d01` | ✅ live | SL double-count fix: `booked_closed_pnl` sirf `compute_hedge_sl_budget()` mein (budget shrinks). `compute_structure_gross_for_sl()` basis = hedge_net + entry_spread + open_gross only. `[SL_BASIS]` log added every cycle. Was already correct — B10 made it explicit + tested. 77 tests pass |
| B11 | `bc368a4`(backend)+UI | ✅ live | Structure P&L realtime fix: open_basket reads net_mtm not target. [STRUCTURE_PNL] log every cycle. Card ke andar structure section added |
| B12 | `fccb9ee` partial | ✅ live | Structure P&L: 4-column bar (Hedge Net / Closed Basket / Open Basket / Structure Net) above hedge card |
| B13 | `fccb9ee` | ✅ live | Dashboard full redesign: lg:grid-cols-2 side-by-side hedge+basket, PnlSlider.jsx (gross→exit spread→fees→net waterfall), target/SL mini progress bars, Multi-Account Overview collapsible+below Bot Monitoring, master shows structure_pnl not basket MTM |
| Fix | `c2ba4fc`, `95c6265` | Auto Trade crash: `fmtLotsCount is not defined` (stale frontend bundle) |

---

# PART 2 — WHAT IN OLD DOCS IS NOW OBSOLETE

| Old doc says | Reality after B1–B10 |
|---|---|
| `HEDGE_MODE_SPEC.md` §1.4 "Stop loss is unchanged" | **Wrong for hedge mode.** Hedge SL now uses **structure-wide** basis (B4b), not hedge-only gross. |
| `HEDGE_MODE_SPEC.md` STEP 7 "Keep stoploss_usd exactly as it is computed today" | Hedge bracket SL uses `structure_gross_for_sl`; short-basket SL unchanged. |
| Basket qty drives hedge qty (`quantity × hedge_qty_ratio`) | In `pct_of_hedge` mode the direction is **reversed**: user sets `hedge_qty_lots`, basket qty is derived. |
| Short Straddle = symmetric ATM pair | Straddle mode now **pins CALL at ATM** and premium-matches PUT across the full chain (B3). |
| `basket_qty_pct_of_hedge` is always manual | With `basket_qty_dynamic=True`, pct is computed at entry from hedge call theta (B7). |

---

# PART 3 — BASKET SIZING (B1, B2, B7, B8)

## 3.1 Old model (still available as `basket_qty_mode='fixed'`)

```
basket_qty = max(1, settings.quantity)
hedge_qty  = max(1, round(basket_qty × hedge_qty_ratio))   # default ratio 1.0
```

## 3.2 New model (`basket_qty_mode='pct_of_hedge'`)

User sets the **hedge long-straddle lot count**. Short basket lots are a percentage of that hedge qty, rounded **up**:

```
hedge_qty  = settings.hedge_qty_lots
basket_qty = ceil(hedge_qty × basket_qty_pct_of_hedge / 100)
```

- Falls back to `fixed` with `[BASKET_SIZING] WARNING` if hedge is OFF or `hedge_qty_lots` is null.
- `basket_qty == 0` → entry blocked with `ENTRY_GUARD_BLOCK guard=basket_qty_zero`.
- At entry in `pct_of_hedge`, live `hedge_row.quantity` is used (not just settings) so mirror drift is respected.

## 3.3 Dynamic pct (B7 + B8)

When `basket_qty_dynamic=True` (only meaningful in `pct_of_hedge`):

```
basket_qty_pct = (hedge_call_theta × basket_qty_theta_mult × 100) / selected_call_ask_premium
basket_qty     = ceil(hedge_qty × basket_qty_pct / 100)
```

- `hedge_call_theta` from `get_hedge_theta()` (same source as theta-based strike selection).
- `basket_qty_theta_mult` default **2.0**, user-editable 0.1–10.0.
- Manual `basket_qty_pct_of_hedge` still used when dynamic is OFF.
- Computed pct stored on Trade as `basket_qty_computed_pct` for audit.

## 3.4 New AutoTradeSettings columns

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `basket_qty_mode` | `'fixed'` \| `'pct_of_hedge'` | `'fixed'` | Sizing direction |
| `basket_qty_pct_of_hedge` | float | 20.0 | Manual pct when not dynamic |
| `hedge_qty_lots` | int \| null | null | Fixed hedge straddle lots in pct mode |
| `basket_qty_dynamic` | bool | false | Theta-derived pct at entry |
| `basket_qty_theta_mult` | float | 2.0 | Multiplier in dynamic formula |

## 3.5 UI (B5, B8)

Auto Trade page → **SHORT BASKET SIZING** block (visible when hedge enabled):
- Mode toggle: fixed vs `% of hedge quantity`
- Hedge qty lots input (pct mode)
- Manual pct input (dimmed when dynamic ON)
- Dynamic % checkbox + theta multiplier (B8)
- Live preview of resulting basket lots

Straddle label corrected: no longer implies both legs are symmetric ATM.

---

# PART 4 — ATM-ANCHORED STRADDLE (B3)

When `position_type='straddle'` in auto trade:

1. **CALL** strike = nearest ATM (pinned).
2. **PUT** strike = premium-matched to CALL ask, scanning **full chain** (ATM and below, OTM puts only).

Function: `select_atm_anchored_pair()` in `delta_client.py`.

Replaces the old symmetric ATM straddle picker for auto-trade entries.

---

# PART 5 — HEDGE STOP LOSS — STRUCTURE-WIDE BASIS (B4a, B4b)

## 5.1 Problem (live proof, Hedge#20, 19:21 IST)

Old decision used hedge-only gross:

```
gross_for_sl           = hedge_net_mtm + entry_spread     # exit spread still deducted in net
room                   = budget + gross_for_sl
room_today             = 0.126118   ← SL $0.13 away
```

~$0.31 of the loss was **spread estimate**, not market move. Open basket P&L was also excluded.

## 5.2 Structure-wide basis (now live since B4b)

```
structure_gross_for_sl = hedge_net_mtm + entry_spread + est_exit_slip + open_basket_gross
room                   = budget + structure_gross_for_sl
fire when              room <= 0
```

- Fees stay deducted inside `hedge_net_mtm`.
- Entry spread and estimated exit slippage are **added back** (neutralised for SL decision).
- `open_basket_gross` = sum of `last_pnl` (gross) for active baskets under this hedge.
- `cum_closed_basket_pnl` remains in **budget only**, not in the basis.

## 5.3 Persisted / logged fields

On `HedgePosition`:
- `structure_gross_for_sl`
- `hedge_est_exit_slippage_usd`
- `hedge_gross_for_sl` (legacy hedge-only, kept for comparison)

Logs:
- `[STRUCTURE_PNL]` — both bases side by side
- `[HEDGE_SL_CHECK]` — `sl_basis=structure`, `room`, `room_old`, `would_have_fired_old`

## 5.4 UI (B6, B6a)

Hedge card now shows:
- Net P&L vs gross P&L with **exit spread deduction** visible
- SL basis label: **structure** (not hedge-only)
- Stop ladder: budget, floor, cum closed baskets, entry spread add-back
- B6a fix: breakdown lines sum correctly; entry spread appears once in SL add-back ladder

Backend helper: `_live_sl_budget_fields()` returns `sl_basis_usd`, `hedge_only_for_sl`.

---

# PART 5b — SL vs Target basis — confirmed design (B9/B10)

### SL vs Target basis — confirmed design (B9/B10)

SL basis (`compute_structure_gross_for_sl`):
  hedge_net + entry_spread + est_exit_slip + open_basket_gross
  booked_closed: NAHI (via budget only)

SL budget (`compute_hedge_sl_budget`):
  budget = fixed_sl + cum_closed_basket_pnl
  (booked losses shrink the budget — single-count)

SL fires when: basis <= -budget

Target basis (`compute_structure_pnl`):
  hedge_net + entry_spread + booked_closed + open_basket_gross
  (no exit_slip — actual realized)

Target fires when: structure_pnl >= live_target_usd

UI: hedge card Target line shows `(structure basis)` next to SL.

Logs:
- `[STRUCTURE_TARGET_CHECK]` — hedge_net, entry_spread, booked, open_gross, structure_pnl, target, room_to_target
- `[SL_BASIS]` — hedge_net, entry_spread, open_gross, basis (booked not on this line)

---

# PART 6 — KEY FILES TOUCHED

| Area | Files |
|------|-------|
| Models / DB | `backend/models.py`, `backend/database.py`, `backend/api/routes_auto_trade.py` |
| Basket sizing | `backend/engine/auto_trade_engine.py` |
| ATM straddle | `backend/core/delta_client.py` |
| Structure SL | `backend/engine/hedge_lifecycle.py` |
| UI | `frontend/src/pages/AutoTrade.jsx`, `frontend/src/components/HedgePanel.jsx`, `frontend/src/components/StructurePnlBar.jsx`, `frontend/src/components/PnlSlider.jsx`, `frontend/src/pages/Dashboard.jsx` |
| Tests | `backend/tests/test_basket_sizing.py`, `test_atm_anchored_pair.py`, `test_structure_sl_basis.py`, `test_structure_target.py` |

---

# PART 7 — DEPLOY NOTE

After pulling B5/B8 frontend changes on the server:

```bash
cd /home/botuser/trading-bot && git pull && cd frontend && npm run build
```

Stale bundle (`index-CwamrdqJ.js`) caused `fmtLotsCount is not defined` even after backend fix was pushed.

---

# PART 8 — RECOMMENDED DOC UPDATES (done in this session)

- `docs/HEDGE_MODE_SPEC.md` — §1.4 SL basis, STEP 2 settings columns
- `README.md` — link to this file under Documentation

---

# PART 9 — WHAT REMAINS — BOT SIDE

### Immediate
- **B7 dynamic qty:** `basket_qty_dynamic = TRUE`, live since 30 Aug
- **Next verification:** BASKET_SIZING log mein `dynamic=True` dikhna chahiye
- **B11–B13:** UI complete

---

*End of session record — 30 August 2026*
