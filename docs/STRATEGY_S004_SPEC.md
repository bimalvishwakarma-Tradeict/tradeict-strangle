> Mirror copy. Authoritative version Claude project mein: claude/STRATEGY_S004_SPEC.md

# Strategy S004 — Spec (DRAFT)

**Status: DRAFT — build is NOT approved.**  
Do not implement live orders, DB tables, or UI for S004 until the owner explicitly approves this document and a build task is issued.

**Last updated:** 2026-09-16  
**Exchange / underlying (assumed):** Delta Exchange India, BTC — **TBD** confirm before build.  
**Relation to S001:** Separate strategy. Do not mix S001 adjustment / wing / hedge logic into S004 unless a later approved spec says so.

---

## 1. Intent (one sentence)

S004 is a **directional BTC + options bracket** style idea: enter long or short with defined BTC stops/targets and option-side brackets, with optional quantity scaling modes.

Exact product (futures vs options legs vs combo) — **TBD**.

---

## 2. Entry rules

### 2.1 Long entry

- **TBD** — signal / filter / session window / confirmation candle rules.  
- Direction: **LONG**.  
- Must not place orders without an approved signal definition.

### 2.2 Short entry

- **TBD** — mirror of long or distinct rules.  
- Direction: **SHORT**.  
- Must not place orders without an approved signal definition.

Until entry rules are filled, treat any “S004 entry” discussion as design-only.

---

## 3. Exit rules (draft numbers)

Units below are **as stated by PM draft**; confirm whether values are **BTC points**, **USD**, or **option premium** before coding.

| Exit | Side | Draft value | Notes |
|------|------|-------------|--------|
| BTC-based stop | Against position | **50** | Hard stop on underlying move |
| Option bracket (stop) | Option leg(s) | **100** | Bracket on option price — **TBD** exact leg mapping |
| BTC target | In favour | **150** | Take-profit on underlying |
| Option bracket (target) | Option leg(s) | **100** | Bracket on option price — **TBD** exact leg mapping |

**Unresolved interactions (do not invent):**

- Which exit wins if BTC stop and option bracket fire in the same poll? **TBD**  
- Partial exits vs flat all? **TBD**  
- Fees / slippage model for research? **TBD**

---

## 4. Quantity modes

Exactly three modes in the draft. Defaults and UI — **TBD**.

### 4.1 Fixed

- Size is constant per entry (lots or USD — **TBD**).  
- No scale-in / scale-out from this mode alone.

### 4.2 Incremental

- Size **increases** on subsequent entries or adds according to a rule — **TBD** (step size, max lots, when to add).  
- Must define risk cap before build.

### 4.3 Reset

- Size **resets** to a base quantity after an exit or after a condition — **TBD** (reset trigger, base qty).  

Do not implement quantity modes until the three rules are fully specified and approved.

---

## 5. Open questions (6)

Exact PM wording was **not available** in the Cursor session that wrote this file. Placeholders only — replace with Claude PM text when available; do not invent answers.

1. **Q1 — TBD** (entry signal / source of truth)  
2. **Q2 — TBD** (instrument: which options / futures / both)  
3. **Q3 — TBD** (exit precedence: BTC vs option brackets)  
4. **Q4 — TBD** (quantity mode defaults + max risk)  
5. **Q5 — TBD** (relation to S001 / shared account / capital)  
6. **Q6 — TBD** (backtest data requirements + go-live criteria)  

---

## 6. Explicit non-goals (until approved)

- No FastAPI routes, no worker loop, no slave mirroring for S004.  
- No reuse of S003 LSR4 as a silent dependency unless a later approved spec links them.  
- No “just wire brackets on S001” shortcut.

---

## 7. Approval gate

Build may start only when:

1. This DRAFT is replaced or explicitly marked **APPROVED** by the owner.  
2. All six open questions are answered in writing.  
3. A Cursor task lists exact files and success criteria.

Until then: **DRAFT only — do not build.**
