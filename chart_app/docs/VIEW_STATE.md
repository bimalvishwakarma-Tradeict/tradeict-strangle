# chart_app view + replay state

Single owner of the time scale is `chart_app/static/view.js` (`ViewController`).
All other modules call `view.request(event, payload)` — they must not call
`setVisibleRange`, `setVisibleLogicalRange`, `scrollTo*`, `fitContent`,
`rightOffset` `applyOptions`, or `chart.resize`.

View is stored as **times** (`leftTime`, `rightTime`), never as logical indices.
After any `setData` prepend/append, the same times are restored so a 1500-bar
loadOlder cannot shift the picture.

## Modes

| Mode | Meaning |
|---|---|
| `LIVE` | WS on, last bar on the right |
| `HISTORY` | User panned/jumped away from now; WS off |
| `REPLAY_PAUSED` | Bar replay on, play off |
| `REPLAY_PLAYING` | Bar replay stepping |

Replay **cursor** is always a **1m bar open**. Higher TFs are resampled from 1m
bars with `time <= cursor`. The last HTF candle is a **partial** aggregate of
those 1m bars. Exchange HTF OHLC for the in-progress bucket is never used
(it would include 1m bars after the cursor).

## Call-site inventory (before ViewController)

These were the time-scale / data / resize sites. After the redesign they go
through `view.request` except `setData` on series (price data, not time scale).

| File | Function | Calls | Trigger |
|---|---|---|---|
| `app.js` | chart create | `rightOffset: 48` | boot |
| `app.js` | `applyLogicalRange` | `setVisibleLogicalRange` main+panes | replay center, tests |
| `app.js` | `pushRangePeer` | `setVisibleLogicalRange` | `subscribeVisibleLogicalRangeChange` |
| `app.js` | `centerReplayCursor` | `applyLogicalRange` (logical idx) | replay start/step/follow/overlay |
| `app.js` | `loadInitial` | `setData`, `scrollToRealTime` | first load, Live, TF in live |
| `app.js` | `loadAround` | `setData`, `setVisibleRange` | TF history, goto, jump range, replay TF |
| `app.js` | `loadOlder` | `setData` **no restore** | `subscribeVisibleTimeRangeChange` near left |
| `app.js` | `loadNewer` | `setData` | right edge in HISTORY |
| `app.js` | `startReplayAt` | `setData`, `padRight`, `centerReplayCursor`; prefetch `allowAhead` | Select bar / `ca-cmd start` |
| `app.js` | `replayStep` | `setData`/`update`, **forces follow**, `centerReplayCursor` | Step / Play |
| `app.js` | `exitReplayLive` | `loadInitial` | Jump to real-time / Exit |
| `app.js` | TF `change` | `loadAround` / `loadInitial` | TF buttons |
| `app.js` | `btnJump` | `loadAround` | Go to date |
| `app.js` | trade click | `setVisibleRange` | backtest trade list |
| `app.js` | `sizeCharts` | `resize` + **logical** restore | window/RO/panel |
| `app.js` | `jumpRange` | `loadAround` + `setVisibleRange` | 1D/5D/… |
| `app.js` | `btnGoRt` | `setVisibleLogicalRange`, `scrollToPosition`, `scrollToRealTime` | >> button |
| `app.js` | `ca-cmd` shift/zoom | `setVisibleLogicalRange` | tests |
| `app.js` | `paintOverlay` rAF | `centerReplayCursor` / logical peer | overlay reload |
| `drawings.js` | `padRight` | `applyOptions({ timeScale.rightOffset })` | replay start / Follow |
| `drawings.js` | `setChartInteraction` | `applyOptions({ handleScroll })` | drawing drag (OK — not time scale) |
| `indicators.js` | `paneChartOpts` | `rightOffset: 48` | new pane |
| `indicators.js` | `resizeOne` / pane drag | `chart.resize` / `applyOptions` size | pane create/resize |

`fitContent` was not called from app code.

## Root causes (reproduced from code)

### 1. TF change → mouse-wheel zoom jumps left/right

Three stacked bugs:

1. **`loadOlder` + logical indices.** Prepending 1500 bars then `setData`
   shifts every logical index. LWC keeps the old *logical* window, so the
   *time* window jumps. Wheel zoom-out after a TF change hits
   `range.from <= oldest + 20*barSec` (15m `barSec` is 900s — a huge tripwire)
   and fires `loadOlder` with **no time restore**.
2. **Peer sync by logical range.** Main and RSI panes can have different
   series lengths after TF change / overlay reload. Copying
   `setVisibleLogicalRange` from one to the other yanks the main chart.
3. **`sizeCharts` restores logical range** after resize; a TF change also
   reflows panes → same index-shift class of jump.

Fix: capture/restore **unix time** range; sync panes by time; debounce
loadOlder; mute the subscriber during programmatic updates.

### 2. Replay wrong / tools fight the view

1. **`centerReplayCursor` uses last *logical* index** of `visibleCandles()`.
   After prepend or TF resample the index is a different time. Step always
   recentered, and **forced `replayFollow = true`**, so Follow OFF did not exist
   in practice. Drawing click did not call pad after the last fix, but overlay
   rAF still called `centerReplayCursor`.
2. **Replay TF change used exchange HTF candles** via `loadAround(cursorClose)`.
   A 5m/15m bar whose open is `<= cursor` still includes **future 1m** (the rest
   of that HTF bucket). That is lookahead.
3. **`startReplayAt` prefetched current-TF bars with `allowAhead`** into
   `candleMap`. Display was filtered, but TF rebuild / `newestCandleTime` /
   indicators could see past the cursor.
4. **Step always moved the camera.** Spec: Follow ON scrolls only if the cursor
   is near the right edge; Follow OFF never moves the view.
5. **Drawings `padRight` used to grow `rightOffset` from future `t2`.** Already
   stopped on select; remaining pad is only replay-start / Follow via ViewController.

## Event table (implemented)

| Event | LIVE / HISTORY | REPLAY |
|---|---|---|
| Mouse wheel | Zoom; bar under cursor stays. No loadOlder until zoom settles. Load does not rewrite the zoomed window. | Same. No data after cursor. |
| Drag pan | Free pan. Left edge → loadOlder, restore by time. | Same. Cannot pan past cursor + 48 bars of empty space. |
| TF change | Keep center time; reasonable bar count. | Keep cursor 1m time. New TF last candle = partial from 1m `<= cursor`. No future 1m. |
| Resize / panel toggle | Same time range. | Same; cursor unchanged. |
| Go to date | HISTORY; that time centered. Replay is **not** exited. | Pause; set cursor to that 1m (snapped); rebuild partial candles; center cursor. |
| Replay start (Select bar) | — | Cursor = last 1m inside the clicked candle. Cursor ~1/3 from the right (`rightOffset` 48, ~80 bars of history). |
| Step / Play | — | Append next **1m** bar into the cursor. Follow ON: scroll only if cursor near right edge. Follow OFF: view frozen. |
| Drawing click/drag/edit | View unchanged. | Same. Drawings may extend past the cursor in time (extrapolated x). |
| Indicator/strategy add/remove/settings | View unchanged. | Same; series clipped to cursor. |
| Jump to real-time | LIVE, last bar on the right, WS on. | Exit replay → LIVE. |
| Older data load | Restore previous time window. | Same, still clipped to cursor. |

## Go-to-date choice

If replay is on: **move the replay cursor** to the chosen time (pause play).
Do not silently exit replay. If replay is off: switch to HISTORY and center
that time.
