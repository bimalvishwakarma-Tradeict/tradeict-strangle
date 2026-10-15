# chart_app — Delta India BTCUSD (Phase 1)

Local TradingView-style chart. Public market data only (no API keys, no orders).

Does **not** import or modify `backend/`. Strategy **signal logic** lives in
`backtest/`; chart overlays import those functions. Do not copy or fork signal
code inside `chart_app/`.

## Docs

- `chart_app/docs/VIEW_STATE.md` — logical view, modes, grid rule, replay, events
- `chart_app/docs/DECISIONS.md` — root-cause log (symptom → cause → fix + commit)

## Data

- REST history: `GET https://api.india.delta.exchange/v2/history/candles`
  (`resolution`, `symbol`, `start`, `end`) — same as `backtest/download_candles.py`
- Live: `wss://socket.india.delta.exchange` channel `candlestick_{tf}`
  (proxied by `chart_app` at `/ws/live`)

## Run (PowerShell)

From the git repo root (`trading-bot`):

```powershell
cd "d:\Tradeict Short (final)\New Setup\Tradeict Short Strangle\trading-bot"
python -m pip install -r chart_app\requirements.txt
python chart_app\server.py
```

Open http://localhost:8700 (`CHART_APP_PORT` overrides, default 8700).

Footer shows `build <short git hash>` (`server.py` `_build_id()` replaces
`__BUILD__` in `index.html`). If git is unavailable, it falls back to
`app.js` mtime.

## E2E

Playwright runner starts a **fresh** server on port **8722**
(`CHART_APP_E2E_PORT`). It refuses if that port is already up.

```powershell
# Offline: disk fixtures + mocked WS (CHART_APP_FIXTURES=1, CHART_APP_WS_MOCK=1)
python chart_app\tests\run_e2e.py

# Live Delta REST + real WS (no fixtures). Also runs overlay perf + view-logic smoke.
python chart_app\tests\run_e2e.py --live
```

Indicator unit tests:

```powershell
cd chart_app
$env:PYTHONPATH = (Resolve-Path ..).Path
python -m pytest indicators -q
cd ..
```

Record fixtures (network): `python chart_app/tests/record_fixtures.py`

## Settings that matter

### `max_days` (S020 / S020_RSI on HTF)

S020 overlay still **fetches 1m** bars, even when the chart TF is 15m/1h/4h.
`max_days` (default **7**, clamp **1–30**) plus a **2-day warmup** caps that
1m window (`server.py` overlay). Put it in strategy params. Large HTF views
without a cap used to pull months of 1m and hang the event loop.

### Delta REST / disk cache (`chart_app/delta_rest.py`)

- Concurrency **2** (`asyncio.Semaphore`)
- **429** respects `Retry-After`, else exponential backoff (up to 5 attempts)
- Closed pages written under `chart_app/.cache/candles/` (gitignored)
- `CHART_APP_FIXTURES=1` serves `chart_app/tests/fixtures/BTCUSD_*.json.gz` with
  **no network**

Heavy overlay / indicator CPU runs in `asyncio.to_thread` so the WS loop
stays alive.

## Notes

- Initial load: last 1500 candles. Scroll left to page older bars.
- Times in the UI are IST (`tickMarkFormatter` + `timeFormatter`).
- S020 overlay imports `session_vwap` / `detect_swings` from `run_s020.py` and
  TF resample / `collect_signals` from `run_s020_dev.py`.
- Backtest trades: pick a `*_trades.csv` under `backtest/strategies/*/runs/`.

## Known limits

- **Day tick on the time axis is 05:30 IST.** Lightweight Charts places
  “day” marks on UTC midnight, which is 05:30 Asia/Kolkata. Hour/minute
  labels are IST; do not “fix” this by shifting candle times.
- **History “tick” replay is 1 minute.** The history store is 1m candles.
  Tick step = next 1m bar. Candle step = last 1m of the chart-TF bucket
  (see `VIEW_STATE.md`). There is no sub-minute tick tape in history.
- Live `--live` e2e 22/23/24 (last-bar X / HISTORY `>>`) can flake on real
  WS; fixtures 1–29 are the commit gate. Re-run `--live` after view changes.
