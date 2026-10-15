# chart_app decisions (root causes)

Each entry: symptom → cause → fix + commit. Test numbers are e2e in
`chart_app/tests/e2e/test_view.py` unless noted.

## TF hang (overlay on the event loop)

- **Symptom:** Switching 1m → 15m/1h with S020 on froze the UI (~312s). WS
  ticks stopped. Overlay JSON for 1h/4h was huge.
- **Cause:** Overlay CPU (`_overlay_cpu`, VWAP, signals) ran on the asyncio
  loop. S020 always pulled a long 1m window even on HTF. VWAP points were
  not downsampled to chart TF. Candle pages were sequential.
- **Fix:** `asyncio.to_thread` for overlay/indicators; `max_days` cap (default
  7, +2d warmup); VWAP/line points on chart TF; parallel Delta pages;
  payload size check. Commit **e203ee2** (also **4ec827a** rate-limit /
  fixtures).
- **Guards:** e2e **19** LIVE S020+ULP+ST TF overlay (WS not blocked);
  **21** 1h/4h overlay payload &lt;1.5MB; **2** / **18** TF cycle.

## Right snap (`rightOffset` / `setVisibleRange`)

- **Symptom:** `>>` / live follow yanked the camera to the last bar. HISTORY
  drag jumped back to LIVE. Future pad (empty right) disappeared. Replay
  Follow OFF still scrolled.
- **Cause:** `applyOptions({ rightOffset })` **resets scroll**.
  `setVisibleRange` with a `to` **after** the last candle **clamps to the
  last bar** — future time cannot exist in time-space. Logical indices after
  `setData` prepend were treated as stable.
- **Fix:** Own the camera in **logical** space
  (`setVisibleLogicalRange`). Future pad = `lastIndex + (t-lastTime)/barSec`.
  `rightOffset` only at chart create, `jump-live` / `scrollLive`. Live edge
  uses `live-bar` +1 (skip HISTORY / user-drag off-right). Replay Follow OFF
  restores the pin. Commits **41b2b9d** (ViewController), **c3ce762**
  (replay step), **e203ee2** (live-bar offset), **4ec827a** (TF keep spacing).
- **Guards:** e2e **9** jump real-time; **12** Follow OFF/ON; **17** LIVE drag
  then `>>`; **20** last-bar X on new 1m; **22/23** LIVE mid last X (1m / 15m
  / 1h); **24** HISTORY off-right then `>>`; **27/28** replay Follow ON/OFF.

## Time axis (IST labels vs day ticks)

- **Symptom:** Axis showed UTC-looking hours, or “day” marks at 05:30 IST
  instead of midnight IST.
- **Cause:** Default LWC formatter is UTC. **Day** tick marks are still
  UTC-midnight instants (= 05:30 IST). Candle `time` must stay unix seconds
  (UTC); shifting times would break the grid and Delta bars.
- **Fix:** `tickMarkFormatter` / `timeFormatter` in IST (`caTickFmt` in
  `app.js`). Leave day-tick **05:30 IST** as a known LWC limit. Footer
  `build <git short hash>` so a screenshot names the binary. Commits
  **c3ce762** (footer), formatter covered in later view work on **4ec827a**
  line.
- **Guards:** e2e **25** tickMarkFormatter IST year/month/day/time.

## Off-grid phantom indices

- **Symptom:** After overlay paint or TF change, logical range jumped
  (e.g. `from ≈ -79`), VWAP “hooks” at session reset, gaps between candles
  on 15m/1h, live last-bar X drifted without a new bar.
- **Cause:** Overlay/VWAP/marker times not on the **chart TF** grid (session
  VWAP used `t-1`, HTF lines used 1m times). LWC treats unique times as
  extra indices → phantom slots. `setData` on overlay then reset the
  visible window.
- **Fix:** `snapToGrid` + `capToLastCandle` on every series time;
  `assertGrid` → `window.__caGridErrors`. VWAP session split via extra
  `vwapSegSeries`, not `t-1`. Overlay `setData` captures logical range and
  `restore-logical` if needed. Commits **e203ee2** (VWAP TF downsample),
  grid/clip in working `app.js` (document when that lands; fixtures **26**).
- **Guards:** e2e **13** VWAP 05:30 IST reset (break, not a hook);
  **16** VWAP warmup vs backtest; **26** 15m/1h S020 grid + candle gaps;
  `__caGridErrors` empty.

## Replay look-ahead (related)

- **Symptom:** Replay 15m last candle / EMA / ULP / RSI used future 1m
  inside the HTF bucket. Signals disagreed with `backtest/`.
- **Cause:** Exchange HTF bar or overlay `to` past the 1m cursor.
- **Fix:** Server partial HTF from 1m `<= cursor`; closed-bar signals only.
  Commits **3b94da5**, **c23df04**, **2f5f998**, **64449e8**.
- **Guards:** e2e **5** TF partial no leak; **10** 15m 09:37 EMA+ULP;
  **11** RSI Div chart==backtest; **14** series no leak; **29** Candle step
  OHLC = 1m aggregate.
