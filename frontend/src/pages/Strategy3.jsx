import { useCallback, useEffect, useRef, useState } from 'react'
import S003Chart from '../components/S003Chart'
import S003Diagnostics from '../components/S003Diagnostics'
import S003SignalTable from '../components/S003SignalTable'
import { getStrategy3Chart, getStrategy3Config } from '../services/api'

const TIMEFRAMES = ['1m', '3m', '5m', '15m']
const CANDLE_COUNTS = [500, 1000, 2000, 4000]

export default function Strategy3() {
  const [timeframe, setTimeframe] = useState('1m')
  const [candleCount, setCandleCount] = useState(1000)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [chartData, setChartData] = useState(null)
  const [config, setConfig] = useState(null)
  const [selectedIndex, setSelectedIndex] = useState(null)
  const [diagOpen, setDiagOpen] = useState(true)
  const chartRef = useRef(null)
  const fetchGen = useRef(0)

  useEffect(() => {
    document.title = 'Delta Bot — S003 Chart'
  }, [])

  const load = useCallback(async () => {
    const gen = ++fetchGen.current
    setLoading(true)
    setError('')
    try {
      const [cfg, data] = await Promise.all([
        getStrategy3Config().catch(() => null),
        getStrategy3Chart({ candles: candleCount, timeframe }),
      ])
      if (gen !== fetchGen.current) return
      setConfig(cfg)
      setChartData(data)
      setSelectedIndex(null)
    } catch (err) {
      if (gen !== fetchGen.current) return
      setError(err?.message || 'Failed to load chart')
      setChartData(null)
    } finally {
      if (gen === fetchGen.current) setLoading(false)
    }
  }, [candleCount, timeframe])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!autoRefresh) return undefined
    const id = setInterval(() => {
      load()
    }, 30_000)
    return () => clearInterval(id)
  }, [autoRefresh, load])

  const selectedSignal =
    selectedIndex != null && chartData?.signals?.[selectedIndex]
      ? chartData.signals[selectedIndex]
      : null

  const handleSelect = (index, signal) => {
    setSelectedIndex(index)
    chartRef.current?.focusSignal?.(signal)
  }

  const enabled = Boolean(config?.enabled)
  const savedTf = config?.timeframe || '—'

  return (
    <div className="mx-auto max-w-[1600px] space-y-4 px-4 py-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-white">S003 Chart</h1>
          <p className="mt-1 text-sm text-gray-400">
            Engine’s own candles, indicators and signals — compare side-by-side
            with TradingView.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`rounded-full px-3 py-1 text-xs font-medium ${
              enabled
                ? 'bg-emerald-900/60 text-emerald-300'
                : 'bg-gray-800 text-gray-400'
            }`}
          >
            engine {enabled ? 'ENABLED' : 'disabled'} · saved TF {savedTf}
          </span>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-gray-800 bg-gray-950 px-3 py-3">
        <div className="flex items-center gap-1">
          <span className="mr-1 text-xs text-gray-500">TF</span>
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              type="button"
              onClick={() => setTimeframe(tf)}
              className={`rounded px-2.5 py-1 text-xs font-medium ${
                timeframe === tf
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-900 text-gray-300 hover:bg-gray-800'
              }`}
            >
              {tf}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <span className="mr-1 text-xs text-gray-500">Candles</span>
          {CANDLE_COUNTS.map((n) => (
            <button
              key={n}
              type="button"
              onClick={() => setCandleCount(n)}
              className={`rounded px-2.5 py-1 text-xs font-medium ${
                candleCount === n
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-900 text-gray-300 hover:bg-gray-800'
              }`}
            >
              {n}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => load()}
          disabled={loading}
          className="rounded bg-gray-800 px-3 py-1.5 text-xs font-medium text-gray-100 hover:bg-gray-700 disabled:opacity-50"
        >
          {loading ? 'Loading…' : 'Refresh'}
        </button>
        <label className="flex items-center gap-2 text-xs text-gray-300">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
            className="rounded border-gray-700 bg-gray-900"
          />
          Auto-refresh 30s
        </label>
        {chartData?.fetch_note ? (
          <span className="text-xs text-amber-400">{chartData.fetch_note}</span>
        ) : null}
      </div>

      {error ? (
        <div className="rounded-lg border border-rose-900 bg-rose-950/50 px-4 py-3 text-sm text-rose-200">
          {error}
        </div>
      ) : null}

      {loading && !chartData ? (
        <div className="rounded-lg border border-gray-800 bg-gray-950 px-4 py-16 text-center text-sm text-gray-500">
          Loading chart data…
        </div>
      ) : null}

      {chartData ? (
        <>
          <div className="text-xs text-gray-500">
            Rendered {chartData.candles?.length ?? 0} candles ·{' '}
            {chartData.signals?.length ?? 0} signals ·{' '}
            {chartData.arm_events?.length ?? 0} arm events · warm_from_index=
            {chartData.warm_from_index == null
              ? 'null'
              : chartData.warm_from_index}
          </div>
          <S003Chart
            ref={chartRef}
            data={chartData}
            config={config}
            selectedSignal={selectedSignal}
          />
          <S003SignalTable
            signals={chartData.signals || []}
            selectedIndex={selectedIndex}
            onSelect={handleSelect}
          />
          <S003Diagnostics
            diagnostics={chartData.diagnostics}
            warmFromIndex={chartData.warm_from_index}
            candleCount={chartData.candles?.length}
            minScore={config?.min_score ?? 3}
            open={diagOpen}
            onToggle={() => setDiagOpen((v) => !v)}
          />
        </>
      ) : null}
    </div>
  )
}
