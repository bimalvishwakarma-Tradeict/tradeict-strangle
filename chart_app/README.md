# chart_app — Delta India BTCUSD (Phase 1)

Local TradingView-style chart. Public market data only (no API keys, no orders).

Does **not** import or modify `backend/`.

## Docs used

- REST history: `GET https://api.india.delta.exchange/v2/history/candles` (`resolution`, `symbol`, `start`, `end`) — same as `backtest/download_candles.py`
- Live: `wss://socket.india.delta.exchange` channel `candlestick_{tf}` (e.g. `candlestick_1m`) with `{ "type":"subscribe", "payload": { "channels": [{ "name":"candlestick_1m", "symbols":["BTCUSD"] }] } }`

## Run (PowerShell)

From the git repo root (`trading-bot`):

```powershell
cd "d:\Tradeict Short (final)\New Setup\Tradeict Short Strangle\trading-bot"
python -m pip install -r chart_app\requirements.txt
python chart_app\server.py
```

Open http://localhost:8700

## Notes

- Initial load: last 1500 candles. Scroll left to page older bars.
- Times in the UI are IST.
- S020 overlay imports `session_vwap` / `detect_swings` from `run_s020.py` and TF resample / `collect_signals` from `run_s020_dev.py`.
- Backtest trades: pick a `*_trades.csv` under `backtest/strategies/*/runs/`.
