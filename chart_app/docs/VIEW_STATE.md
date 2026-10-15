# chart_app view + replay state

Single owner of the time scale is `chart_app/static/view.js` (`ViewController`).
All other modules call `view.request(event, payload)`. They must not call
`setVisibleRange`, `setVisibleLogicalRange`, `scrollTo*`, `fitContent`,
`rightOffset` `applyOptions`, or `chart.resize` except as listed below.

## Logical space (current)

View is stored as **logical indices** (`leftLogical`, `rightLogical`) and
applied with `timeScale().setVisibleLogicalRange`. Unix times
(`leftTime`, `rightTime`) are derived via `logicalToTime` / `timeToLogical`.

`timeToLogical(t)` (`view.js`):

- If `t` is after the last series time: **`lastIndex + (t - lastTime) / barSec`**.
  Future empty space is a fractional index past the last bar — not a new
  series point.
- If `t` is before the first series time: `(t - firstTime) / barSec` (can be
  negative).
- Else: binary search the candle times; interpolate between adjacent bars.

`setVisibleRange({ from, to })` is **not used**. LWC clamps a future `to` time
onto the last bar, so a right pad in time-space cannot exist. Logical `to`
past `lastIndex` is the only way to keep empty space on the right.

After `setData` prepend/append, restore **the same logical window**
(`restorePinned` / `restoreTime`). `load-older-end` shifts the pin by
`lastIndex - pinLastIndex` when older bars were prepended.

## rightOffset — allowed spots only

`applyOptions({ timeScale: { rightOffset } })` **resets scroll**. Do not use it
to follow live bars or to keep HISTORY/REPLAY camera.

| Where | Value | When |
|---|---|---|
| `app.js` chart create | `rightOffset: 6` (`LIVE_PAD`) | boot |
| `view.js` `applyLiveRightOffset()` | `LIVE_PAD` (6) | **jump-live** (`>>`), `scrollLive` (first LIVE after-data with no range yet) |
| `indicators.js` `paneChartOpts` | `rightOffset: 48` | pane chart **create** only (legacy). New code must not add more. |

Live follow on an **already-live** edge uses `live-bar`: if the last bar is
on-screen and a **new** bar grew, shift `{from,to}` by `dlt` logical bars.
It does **not** call `rightOffset`. Skip `live-bar` in `HISTORY`, and skip
if the user is dragging with last bar off the right.

## Modes

| Mode | Meaning | Who sets it |
|---|---|---|
| `LIVE` | Last bar on/near the right; WS may run | Default. `setMode` via `syncLiveHistoryMode` when last is **not** off-right; `jump-live`; `last-mid`; `scrollLive`; `app.js` `viewModeSync` when `!historyMode && !replayOn` |
| `HISTORY` | User panned so last bar is **off the right** (`lastOffRight`: lastLogical > rightLogical + 2) | `maybeLeaveLive` → `syncLiveHistoryMode` → `onLeaveLive` (`historyMode = true`). Zoom-only width change does not leave LIVE. |
| `REPLAY_PAUSED` | Bar replay on, play off | `app.js` `viewModeSync` (`replayOn && !replayPlaying`) |
| `REPLAY_PLAYING` | Replay stepping | `viewModeSync` (`replayPlaying`) |

`set-mode` from app.js overwrites `mode` without going through `setMode` (no
log). Replay always wins over LIVE/HISTORY in `viewModeSync`.

`maybeLeaveLive` ignores range changes while muted / `freezeUntil`, and
ignores **zoom** (visible-bar count changed >6% vs `lastRangeWidth`).

## GRID RULE

Every time that goes onto the chart (candles, VWAP, overlay lines, markers)
must:

1. Sit on the **chart TF grid**: `snapToGrid(t) = floor(t / barSec) * barSec`.
2. Not be **ahead of the last visible candle**: `capToLastCandle`.

Off-grid or ahead-of-last times create **phantom logical indices** (LWC
inserts extra time slots). `assertGrid(seriesName, points)` logs and pushes
to `window.__caGridErrors`. `paintOverlay` clears then re-fills that array.

Live candles: `applyLiveCandle` snaps `bar.time` before `series.update`.

VWAP session breaks: **do not** insert `t-1` dummy points. Split the line
onto extra `vwapSegSeries` (`splitLineSegs` on null/gap).

## Replay

Cursor is always a **1m bar open** (`replayBarOpen`). HTF candles are
resampled from 1m with `time <= cursor`. The last HTF candle is a **partial**
aggregate. Exchange HTF OHLC for the in-progress bucket is never used.

| Step | What advances |
|---|---|
| Tick | Next **1m** bar (`ensureNextBar`). History feed is 1m only, so a “tick” is one minute. |
| Candle | Jump to last 1m of the current chart-TF bucket, else last 1m of the next bucket (`ensureUpTo`). |
| Keyboard | `→` tick, `Shift+→` candle (`app.js` keydown). |

No look-ahead: `maxShowTime()` = cursor; `visibleCandles` / overlay /
indicators clip to `canShowTime`. `leakCheck` flags last > cursor.

Follow ON (`replayStepView`): hold logical range unless the last bar **grew**
and the gap to the right edge is `< FOLLOW_EDGE` (10), then shift +1.
Follow OFF: `restorePinned()` — camera frozen.

Pan in replay: `clampReplayPan` — right edge ≤ lastLogical + `RIGHT_PAD` (48).

## Event table (current `view.request` + app)

| Event | LIVE / HISTORY | REPLAY |
|---|---|---|
| Mouse wheel | Zoom in logical space; bar under cursor stays. `maybeLeaveLive` skips zoom. `loadOlder` debounced 180ms, restore pin. | Same. No data after cursor. |
| Drag pan | Free pan. Last off-right → HISTORY (`onLeaveLive`). Left edge → `loadOlder`. | Same. Clamp right to cursor + 48 empty bars. Pan off cursor → Follow OFF. |
| TF change | `tf-pending` pin, then `tf-change` / `applyTfKeepSpacing` (keep `barSpacing` + visible bar count; right from time anchor). | Keep 1m cursor. Rebuild partial HTF from 1m `<= cursor`. |
| Resize / panel | `resize` → pin, `doResize`, `restorePinned`, `syncPanes`. | Same; cursor unchanged. |
| Go to date | HISTORY; `goto` centers time (`applyTime`). Replay is **not** exited. | Pause; snap cursor to that 1m; rebuild; `replay-start` layout. |
| Replay start (Select bar) | — | Cursor = last 1m inside clicked candle. `layoutReplay(true)`: lastIndex-80 … lastIndex. |
| Step Tick / Candle / Play | — | Advance cursor as above; `replay-step`. Follow ON/OFF as above. |
| Drawing click/drag/edit | `drawing` is a no-op (view unchanged). | Same. |
| Indicator/strategy add/remove/settings | `indicator`: capture + sync panes (LIVE) or restore pin (HISTORY). Overlay `setData` may `restore-logical` if range was captured. | Restore pin. Series clipped to cursor. |
| New live candle | `live-bar` shift if LIVE and last on-screen. Skip HISTORY / user-drag off-right. | Ignored. |
| Jump to real-time (`>>`) | `jump-live`: LIVE, logical last-80+6 … last+6, then `applyLiveRightOffset`. WS on. | `exitReplayLive` → `loadInitial` → `jump-live`. |
| Older / newer data | pin on start; `restorePinned` on end. Newer also `syncLiveHistoryMode`. | Replay does not page via this path. |
| `last-mid` | Center last bar; `freezeUntil` 450ms; LIVE. | Not used. |
| Trade list click | `trade-range` → `applyTime`. | Same times, still clipped to cursor if replay on. |

## Go-to-date choice

If replay is on: **move the replay cursor** to the chosen time (pause play).
Do not silently exit replay. If replay is off: switch to HISTORY and center
that time.

## Call-site inventory (must stay true)

| File | Allowed time-scale API |
|---|---|
| `view.js` | `setVisibleLogicalRange`, `applyLiveRightOffset` (`rightOffset: 6`), `scrollToRealTime` only inside `scrollLive`, `resize` via `doResize` |
| `app.js` | Chart **create** `rightOffset: 6`. Everything else `view.request`. |
| `indicators.js` | Pane **create** `rightOffset: 48` only. Pane range follows `view` `syncPanes`. |
| `drawings.js` | Must not set `rightOffset` or visible range. |

`fitContent` is not called from app code.
