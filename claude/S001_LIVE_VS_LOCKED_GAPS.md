# S001 — Live vs Locked (research) gaps

**Date:** 2026-09-22  
**Scope:** Locked / research mark-engine config used for reverify OOS  
vs **live code + `auto_trade_settings` model defaults** (DB row on server may differ — TBD).  
**Does not** change `backend/`.

**Locked (this reverify OOS CLI):**  
`--premium-mode b25 --profit-mode cost_k --wing-roll on --slip-model bucketed`  
plus engine defaults: entry 11:00 IST, DTE=2, Adj B_only, trigger 70%, max_adj=2,  
dec_pct=40%, wings 2000 pts, qty=8, hedge=off, skip 2025-04-26.

**Size examples** use the 2025-07-15 mark trace (spot ≈ 117468): B25 target ≈ **778.95**/side  
vs live fixed **150** → ~**5.2×**.

---

## Summary table

| Gate | Locked / research behaviour | Live behaviour | Same? |
|------|----------------------------|----------------|-------|
| G1 | Synthetic ATM straddle × 25% **per side** (hedge off still B25) | `hedge_enabled=0` → fallback **fixed $150**/side | **NO** |
| G2 | Dual-gate: pressured (≥100% baseline) **and** decayed (&lt;trig%×baseline) | Same dual-gate in `_try_adj_b_action` | **YES** (code) |
| G3 | `select_adj_b_strike`: prem **&lt;** P_target, then max premium | Same imported helper | **YES** |
| G4 | Wing filter only if status=open **and** qty&gt;0; reject at/beyond wing | Same `resolve_adj_b_wing_strike` + filter | **YES** |
| G5 | No strike inside wing → basket exit `ADJ_B_NO_STRIKE_INSIDE_WING` | Same forced exit path | **YES** |
| G6 | max_adj=2; 3rd trigger → `MAX_ADJUSTMENTS_REACHED` | Default `max_adjustments_per_basket=None` (unlimited) unless DB set | **NO** (default) |
| G7 | `compute_decrease_step_qty` + `floor()`; dec_pct=**40** | Same function; model default dec_pct=**25** | **PARTIAL** |
| G8 | Wings 2000 pts; **wing_roll ON** | Points default 2000; roll default **ON**; wings **default OFF** | **PARTIAL** |
| G9 | Profit target = **cost × k** (wing debit + fees) | `compute_basket_profit_target_at_entry` **THETA/PCT** | **NO** |
| G10 | Pre-expiry close from **17:15 IST** (15 min before 17:30) | Same `is_pre_expiry_window` | **YES** |

---

## Per-gate detail

### G1 — Entry premium target (B25 vs fixed $150)

| | Locked | Live |
|---|--------|------|
| Behaviour | `target = 0.25 × (ATM_C + ATM_P)` marks; pick OTM closest to target | `resolve_strangle_target_premium`: if `pct_of_hedge` but `hedge_enabled=False` → **fixed** `target_premium_per_side` (default 150) |
| Citations | `backtest/s001_mark_engine.py` ~749-761 (`premium_mode=="b25"`) | `backend/engine/auto_trade_engine.py:279-318` (`_fallback("hedge_disabled")`); defaults `models.py:331-337` mode=`fixed`, 150 |
| Size | 2025-07-15: **778.95** vs **150** ≈ **5.2×**; 2026-08-03: **321.68** vs **150** ≈ **2.1×** | |
| To align live → locked | With hedge off: either (a) add synthetic ATM×pct path when hedge disabled, or (b) enable hedge + real `pct_of_hedge` marks and set pct=25 (today default pct=**3.0** `models.py:338-340` — still not B25). Also lock entry clock 11:00 / DTE=2 in `auto_trade_settings` (`expiry_dte` default **1** `models.py:275`). |

### G2 — Adj B trigger

| | Locked | Live |
|---|--------|------|
| Behaviour | Dual: tested ≥ 100% baseline **and** untested &lt; trigger%×baseline; roll **untested** | Identical in live |
| Citations | `s001_mark_engine.py` monitor loop (~953-963) | `logic.py:119-210` esp. `142-153` |
| Size | n/a (same rule). Trigger **value**: locked **70%** vs code/DB default **50%** (`config.py:24`, `models.py:507-508`) — separate knob gap | |
| Note | Older **plan prose** said single-leg ≥ trigger% — that wording was **wrong vs live**. Research mark engine mirrors **live dual-gate**, not the bad plan prose. |
| To align | For trigger **level**: set live `adj_b_trigger_pct=70` and `adjustment_mode=B_ONLY` (live default mode `A_ONLY` `models.py:500-504`). Gate logic itself: nothing to change. |

### G3 — Adj B strike (max premium &lt; P_target)

| | Locked | Live |
|---|--------|------|
| Behaviour | Strict `&lt; P_target`, highest premium survivor | Same |
| Citations | imports `adj_b.select_adj_b_strike` `adj_b.py:173-398` / `309-345` | same |
| Size | — | — |
| Residual | **P_target source:** live uses tested leg **Best Offer** (`adjustment.py:548-554`, never mark); mark engine uses tested **mark** (no L2). Size varies with bid/ask vs mark — **UNKNOWN** magnitude without L2 replay. |
| To align | Feed offer into mark engine or accept mark proxy; live needs no code change for G3 rule. |

### G4 — Wing open+qty&gt;0 filter (D4)

| | Locked | Live |
|---|--------|------|
| Behaviour | Wing counts only if open and qty&gt;0; reject call≥wing / put≤wing | Same |
| Citations | `adj_b.py:17-40`, `319-334` | same |
| Same? | **YES** (research-parity tested). Production deploy of D4 still **TBD** on server. |

### G5 — No strike inside wing → exit

| | Locked | Live |
|---|--------|------|
| Behaviour | `is_adj_b_no_strike_inside_wing` → exit reason `ADJ_B_NO_STRIKE_INSIDE_WING` | `adjustment.py:576-598` |
| Citations | `adj_b.py:43-66`; mark engine mirror | same |
| Same? | **YES** (code). Live-vs-research on real days still **UNKNOWN** until ledger compare. |

### G6 — Max adjustments

| | Locked | Live |
|---|--------|------|
| Behaviour | max=**2**; next trigger force-exits | Gate exists `logic.py:1284-1372`; default max=**None** = unlimited |
| Citations | mark engine `max_adj=2` | `models.py:360-362` |
| Size | Locked caps at 2 adjs; live may place 3+ or never force-exit | |
| To align | Set `max_adjustments_per_basket=2` (and usually `conversion_mode_enabled` semantics per comments). |

### G7 — Qty decrease `floor()` (D2)

| | Locked | Live |
|---|--------|------|
| Behaviour | `floor(orig × (1 − pct/100 × n))`; D2 **not** fixed | Same function |
| Citations | `backend/engine/wing_entry.py:332-353` | same |
| Size | Locked **dec_pct=40** → 8→4→1→close; live default **25** → 8→6→4→… (`models.py:496-498`) | |
| To align | Set `adjustment_qty_mode=decrease_step` and `adjustment_qty_decrease_pct=40`. Keep `floor()` (D2 open by design for reverify). |

### G8 — Wings 2000 + roll

| | Locked | Live |
|---|--------|------|
| Behaviour | Wings **on**, 2000 pts, **roll ON** | Roll default **ON** (`models.py:555-557`); points 2000; **`basket_wings_enabled` default OFF** (`models.py:535-537`) |
| Citations | mark engine `pick_wing_strikes` + `wing_roll` | `wing_select` / settings |
| Size | Locked always carries long wings; live may run short-only if wings off | |
| Note | Older `s001_final_validation.py` CFG had `wing_roll_with_short_enabled=0` — **conflicts** with this reverify CLI (`--wing-roll on`). |
| To align | Enable wings, points=2000, roll=1 in live settings. |

### G9 — Profit target

| | Locked | Live |
|---|--------|------|
| Behaviour | `cost_k`: `(wing_debit + entry_fees) × k` (k=1.0); short credit **excluded** | `compute_basket_profit_target_at_entry` — mode **THETA** (default) or **PCT** of net credit (`hedge_theta.py:449-505`) |
| Citations | `s001_mark_engine.lock_profit_target_usd` ~261-296 | `auto_trade_engine.py:1910-1974`; `hedge_theta.py:449+`; `basket_target_mode` `models.py:456` |
| Size | Different **units and formula** — not a simple multiple. cost_k often small $ (fees+wings); THETA/PCT tracks credit or hedge theta. On 2025-07-15 mark trace locked TP ≈ **6.56** USD for 8 lots — live THETA/PCT would typically be another order of magnitude if sized off credit. |
| Same-day re-entry | Locked: after PROFIT_TARGET only, max 3/day | Live: `next_entry_time` / reentry delay — not hard-coded 11:00 wall clock |
| To align | Add `cost_k` target mode to live **or** stop claiming live matches locked research TP. |

### G10 — Pre-expiry 17:15 IST

| | Locked | Live |
|---|--------|------|
| Behaviour | Exit when hours_to_expiry in (0, 0.25] or ≤0 | Same |
| Citations | `s001_mark_engine.is_pre_expiry`; `time_utils.py:404-408`; `logic.py:716-722` | same |
| Same? | **YES** |

---

## Extra residual divergences (not G1–G10 labels)

| Item | Locked mark engine | Live | Impact |
|------|-------------------|------|--------|
| Trigger baseline | Entry **mark** | Fill → `trigger_baseline_premium` | Slip can auto-pressure live shorts |
| Adj B P_target | Tested **mark** | Tested **offer** | Strike choice |
| Fill model | mark ± `slip_pct` buckets | Exchange fills / mid chase optional | PnL |
| Entry schedule | Fixed 11:00 IST grid | Continuous / `next_entry_time` | Sample path |

---

## What this OOS measures

OOS with `--config locked` answers: **“Does the research-locked policy work on marks?”**  
It does **not** answer: **“Does current live default settings match that policy?”**  
Use this gap list before promoting locked knobs to `auto_trade_settings`.
