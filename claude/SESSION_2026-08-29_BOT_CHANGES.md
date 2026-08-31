# Session Record — 29 August 2026
## Delta Exchange India Short Strangle Bot (Tradeict)
### B1–B25. Read this BEFORE `docs/HEDGE_MODE_SPEC.md` — hedge SL basis and basket sizing sections there are stale.

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
| B11 | backend only | ✅ | Structure P&L realtime: open_basket reads net_mtm not target. [STRUCTURE_PNL] log every cycle |
| B12 | — | ✅ | Structure P&L 4-column bar above hedge card: Hedge Net / Closed Basket / Open Basket / Structure Net |
| B13 | `fccb9ee` | ✅ | Dashboard redesign: lg:grid-cols-2 side-by-side, PnlSlider.jsx waterfall bars, target/SL progress bars, collapsible Multi-Account Overview, structure_pnl for master |
| B14 | `cbd65da` | ✅ | Real balance columns (actual/blocked/avail/daily-growth), balance_snapshots table, lot qty in legs |
| B15 | `53e0d51` | ✅ | AutoTrade page redesign: 2-col, 8 categorised sections, InfoTooltip, sticky header+nav, mobile responsive |
| B16 | `df3deb8` | ✅ | Balance field mapping fix (USD asset preference), payoff graph restored to dashboard |
| B17 | `a696168` | ✅ | blocked_margin fix: read blocked_margin not position_margin (always 0 in cross-margin mode) |
| B18 | `8ab5341` | ✅ | Free cash + unrealised columns; available_margin computed; bot sizing unchanged on free_cash |
| B19 | `ff8e937`+`3d1cf06`+`3bd80a6`+`270f0fc` | ✅ | Delta WebSocket margins channel: IPv4 forced, bot startup feed, REST seed after subscribe. Cache TTL 60s |
| B20 | `46aedf5` | ✅ | AVAIL BAL column removed — Delta API does not expose mark-price unrealised cashflow |
| B21 | `ac556e1` | ✅ | PnlSlider bar widths fixed: proportional to max absolute value across all bars in slider |
| B22 | `f126217` | ✅ | Frontend gross_mtm uses calculated_pnl (mark-based) not offer-based recompute |
| B23 | `e230f11` | ✅ | Basket per-structure numbering, basket story below card + collapsible, payoff collapsible, structure history pagination 20/page |
| B24 | — | ✅ | Gross MTM = delta_upnl only (Delta UI match), Realized P&L shown separately, Net = UPNL+realized-all_deductions |
| B25 | `26b7050` | ✅ | Dynamic qty at adjustment: re-calculates using B7 formula, untested leg topped up, 50% hedge cap, OFF by default |
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
| `use_dynamic_qty_on_adjustment` | bool | false | Recompute basket qty at each adjustment (B25; requires `basket_qty_dynamic`) |

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
- **B11–B25:** UI + balance + WS margins + P&L display + dashboard UX + dynamic adj qty complete

### SL/Target/Balance — Confirmed Design (B17-B21)

Balance mapping (confirmed live):
  actual_balance    = balance (FNO Wallet settled cash)
  blocked_amount    = blocked_margin (NOT position_margin — always 0 in cross mode)
  free_cash         = available_balance (Delta REST field — bot sizes on this)
  available_margin  = kept in API, removed from UI (Delta internal mark-price not accessible)

WS margins channel (B19):
  Connects at bot startup via _start_margins_feed()
  Authenticates with key-auth HMAC (GET + timestamp + /live)
  Seeds cache from REST+positions after subscribe (Delta pushes only on margin change)
  Cache TTL 60s; fallback to rest_computed if stale
  balance_source field in API: "websocket" | "rest_computed"

Dashboard columns (B20 final):
  ACTUAL BAL | BLOCKED | FREE CASH | DAILY Δ% | STRUCTURE MTM | TARGET

---

### P&L Display Design (B22-B24)

Gross MTM = delta_upnl (mark-price, matches Delta UI)
Realized P&L = separate line item (booked)
Net MTM = delta_upnl + realized_pnl - entry_fees - exit_fees - exit_spread
Target = Net MTM vs profit_target_usd (unchanged)
SL = gross_mtm_for_sl = gross_mtm + entry_spread (unchanged)

### B25 Dynamic Adj Qty

Setting: use_dynamic_qty_on_adjustment (bool, default False)
Only when basket_qty_dynamic=True
Formula: raw_pct=(hedge_theta×mult×100)/new_ask; new_qty=max(1,min(ceil(hedge_qty×pct/100),floor(hedge_qty×0.5)))
Cap: 50% of hedge qty
Triggered leg: new_qty lots at new strike
Untested leg: topped up by (new_qty - current_qty) extra lots
Fail-safe: untested top-up failure does NOT abort adjustment

---

*End of session record — 31 August 2026*
