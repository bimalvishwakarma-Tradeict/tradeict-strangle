> Mirror copy. Authoritative version Claude project mein: claude/SESSION_HANDOFF_2026-09-16.md

# Session handoff — 2026-09-16

**Audience:** next Cursor / Claude session, or any engineer picking up the bot.  
**Scope:** Short Strangle bot (`trading-bot/`) + S001 backtest / mark pipeline.  
**Status date:** 2026-09-16 (IST).

Where something is not verified in-repo or in this Cursor session, it is marked **TBD** — do not invent.

---

## 1. Working rules

| Role | Responsibility |
|------|----------------|
| **Claude (PM)** | Specs, priorities, acceptance criteria, “what / why”. Does not silently expand scope. |
| **Cursor (coder)** | Implements exactly the task prompt. Reads before writing. Commits only when asked. |

**Prompt format (current habit):**

1. `git pull` first  
2. Task body (scope + do-not-touch)  
3. Optional verify command (tests / dry-run)  
4. `git add` listed files → `commit` → `push` (only if STEP 4 says so)

**Long-running work:** run in Cursor’s terminal (PowerShell on the Windows workstation). Prefer `emit()` / logging over `print()` in backtest scripts. Do not block the session on multi-hour downloads without backgrounding.

**Hard rules (also in `CLAUDE.md`):**

- Never guess schema / paths — read models first.  
- Server **only pulls** — never `git add -A` on the server.  
- Only `log_and_buffer` lands in `bot_activity.log`; plain `logger.*` → stderr / `error.log`.  
- Adjustment path: market orders only (no mid-price).  
- UTC in DB; IST for display only.

---

## 2. Paths

### Local (Windows workstation)

| Item | Path |
|------|------|
| Bot repo | `d:\Tradeict Short (final)\New Setup\Tradeict Short Strangle\trading-bot` |
| Marks cache | `trading-bot\backtest\cache\option_marks\` |
| Products DB | `trading-bot\backtest\cache\products_btc_options.sqlite` |
| Backtest results | `trading-bot\backtest\results\` |
| Parent knowledge (older) | `d:\Tradeict Short (final)\PROJECT_KNOWLEDGE.md` — **S001 bot rules win over Tradeict-AI optimizer docs when they conflict** |

### Server (live bot)

| Item | Path |
|------|------|
| Repo | `/home/botuser/trading-bot` |
| DB | `/home/botuser/trading-bot/trading_bot.db` |
| Python | `/home/botuser/.venv/bin/python3` (venv **outside** repo) |
| Process | supervisord program `trading-bot` → uvicorn `127.0.0.1:8000` |
| Activity log | `/home/botuser/trading-bot/logs/bot_activity.log` |
| Error / plain logger | `/var/log/trading-bot/error.log` |

Import check before restart:

```bash
cd /home/botuser/trading-bot && \
PYTHONPATH=/home/botuser/trading-bot /home/botuser/.venv/bin/python3 \
  -c "import backend.main; print('IMPORT OK')"
```

---

## 3. Mark download in progress (2026-09-16)

**Goal:** MARK:1m candles for BTC options so S001 backtests are not print-starved.

| Setting | Value |
|---------|--------|
| Script | `backtest/download_option_marks.py` |
| Typical command | `python backtest\download_option_marks.py --months 24 --tail-days 5` |
| Resolution | `1m` |
| Strike band | entry spot ±6000 |
| Order | **oldest first** (from 2024-09) |
| Resumable | yes — progress in shard SQLite; required-window coverage check (extra history ≠ incomplete) |
| `--tail-days` | **5** (default): short-dated (life ≤14d) → last 5 days only; monthlies (life >14d) → full listing life |
| Dry-run estimate (2026-09-16) | NEW ≈ **91.4k requests / ~19.0 h** @ sleep 0.75s; OLD full-10d ≈ 145k / ~30 h; **~37% request savings** |
| Dry-run mix | short-dated ~34.2k symbols; long/monthly ~2.2k (after band) |

**Do not** re-download months that already have **more** than the required window — treat as complete for resume.

Pilot month already on disk: `backtest/cache/option_marks/marks_2026-05.sqlite` (earlier full-life pilot).

---

## 4. Data quality problems (why marks matter)

Sources: `backtest/results/iv_surface_pnl_validation_latest.txt`, `print_vs_mark_diagnosis.txt`, `s001_final_validation.txt` / resolution reports.

| Issue | Measured | Implication |
|-------|----------|-------------|
| **IV surface pricing** | 1-day change median \|err\|%entry = **12.99%** | Surface **not** good enough to measure ~3% premium P&L signals |
| **Print coverage** | Print-only cycles **60** usable vs **1446** skipped → ≈ **4%** of candidate days | Print-only backtests are sparse; selection bias risk |
| **Nearest-print gap** | May diagnose: median gap **190.5 s**; only **25%** of fills within 60s | “Nearest print” is often minutes away from decision time |
| **Mark-to-fill calibration** | Symmetric slip ≈ **1.65%** (sell mark−1.65%, buy mark+1.65%); asym sell≈1.59% / buy≈1.71% | Use for mark-path fills until re-calibrated on full mark history |

Print vs mark path can **diverge** on adjustment strikes/times (see May-24 dual ledger in `print_vs_mark_diagnosis.txt`) — index-aligned price Δ alone is misleading.

---

## 5. S001 — locked research config (print-only final)

From `backtest/s001_final_validation.py` `CFG` / results header (research lock, not necessarily live `auto_trade_settings`):

| Knob | Locked value |
|------|----------------|
| Expiry / entry | 2 DTE, **11:00 IST** |
| Premium mode | pct_of_hedge **25%** (B25) |
| Basket qty | **8** |
| Wings | ON, **2000** points away, **roll OFF** |
| Adjustment | **B_only**, trigger **70%**, max adj **2**, qty decrease **40%** |
| Profit target | total cost × **1.0** |
| Hedge | **OFF** |
| Fills | maker-only (print research) |

### “Five config changes” (vs earlier sweep defaults)

Treat these as the research lock deltas that mattered in the final validation sweep:

1. `adjustment_mode` → **B_only** (not A_only / BOTH)  
2. `adj_b_trigger_pct` → **70**  
3. `adjustment_qty_decrease_pct` → **40**  
4. Wings → **2000 pts**, `wing_roll_with_short_enabled` → **0**  
5. `hedge_enabled` → **0**; profit target **k=1.0**

### Sizing 22 USD

**max loss per basket = 22 USD (S001_FINAL_CONFIG §3)**

---

## 6. Code defects (live / shared paths)

| ID | Defect | Status | Commit(s) | Deploy |
|----|--------|--------|-----------|--------|
| **D1** | `db_audit` CHECK 4 closed **all** slave option positions (no bot/trade/hedge scope) | **FIXED** — shared `scope_live_positions_for_closed_master_recovery`; structured `DB_AUDIT_CHECK4_*` logs | `4e12d6f` | **Pending** server `git pull` + import check + restart when safe |
| **D2** | `compute_decrease_step_qty` uses `floor()` — not proportional to remaining qty | **OPEN** | — | — |
| **D3** | Wing `SELL_PARTIAL` uses fixed **2** lots | **OPEN** | — | — |
| **D4** | Adj B wing collision / cross open wing (selection + closed-wing fallback + no-strike forced exit) | **FIXED** | `bfbeeb6` + `0f6ae65` | **Pending** deploy |
| **D6** | `LEDGER_RECONCILE` reports `findings=3` every cycle | **OPEN** | — | — |
| **D7** | `trade_reconcile.py:312-369` does not detect same-product long+short collision | **OPEN** | — | — |

There is **no D5** defect in this numbering.

**Tests:** `backtest/test_adj_b_wing_clamp.py`, `backtest/test_db_audit_check4.py` (run after pull).

**Grep live CHECK4 force-close history (server):**  
`error.log` for `live option positions under closed master` / `DB_AUDIT_CHECK4_` (activity log only after `4e12d6f`).

---

## 7. Pending list

1. **Server deploy** of D1 + D4 (`git pull` → import OK → restart when no mid-adjustment).  
2. **Finish / monitor** 24‑month mark download (`--tail-days 5`); then rebuild multi-month S001 mark path.  
3. Re-run mark-vs-print calibration on fuller history (1.65% may move).  
4. Open live defects: **D2**, **D3**, **D6**, **D7** (see §6).  
5. **S004** — cost gate research (`backtest/s004_gate.py`); live build still needs approved spec.  
6. Live `auto_trade_settings` vs research-locked CFG — confirm what is actually live (**TBD**).  
7. Optional: wipe / ignore stale `frontend/trading_bot.db` on server (never use it).

---

## 8. Strategies that are closed (do not reopen on mark data alone)

| Strategy / idea | Why closed | Why new mark data will not reopen it |
|-----------------|------------|--------------------------------------|
| **S003 LSR4** | Signal edge vs random baseline is tiny; expectancy does **not** clear documented cost floors (~23 hedge-off / ~53 hedge-on BTC points on 1m study) | Marks fix **option fill** fidelity, not the **BTC directional** edge vs cost |
| **ULP range selection** | ULP width ≈ matched-width control (`\|z\|` small) — zones add **no information beyond width** | Same structural result; better option marks do not change BTC zone logic |
| **Stoploss–reversal / half-runner chain** (exit-system line of work) | Control tests showed BTC path behaviour does not justify the chain as a standalone edge (**TBD** full PM write-up pointer) | Depends on BTC path + cost, not option mark density |

**S001** remains the active research + live short-strangle line. Marks are to **improve S001** measurement / future mark-path sims, not to revive S003/ULP/reversal.

---

## 9. Related docs

- `docs/STRATEGY_S004_SPEC.md` — S004 **DRAFT** (not approved to build)  
- `CLAUDE.md` — hard rules + server paths  
- `claude/S001_CONFIG_SEMANTICS.md` — live settings semantics  
- `docs/SESSION_2026-08-16_CHANGES.md` — older live exit/slave session  
- `claude/SESSION_2026-08-29_BOT_CHANGES.md` — B1–B25 basket / hedge UI notes  

---

*End of handoff 2026-09-16.*
