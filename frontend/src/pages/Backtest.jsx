import { Fragment, useCallback, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { runBacktest, runBacktestLocal } from '../services/api'

const DEFAULT_PARAMS = {
  trade_type: 'straddle',
  expiry_type: '1DTE',
  entry_hour_ist: 10,
  entry_minute_ist: 0,
  quantity: 100,
  trigger_pct: 150,
  profit_target_pct: 50,
  stoploss_pct: 100,
  min_replacement_premium: 150,
  conversion_equality_pct: 10,
  target_premium_per_side: 350,
  fee_per_leg_usd: 0.75,
  slippage_pct: 2,
}

function fmtMoney(v, signed = false) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  if (!signed) return `$${abs}`
  if (n > 0) return `+$${abs}`
  if (n < 0) return `-$${abs}`
  return `$${abs}`
}

function moneyClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'text-gray-300'
  return n > 0 ? 'text-green-400' : 'text-red-400'
}

function countCsvRows(text) {
  if (!text) return 0
  let lines = 0
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) lines += 1
  }
  if (text.length > 0 && text.charCodeAt(text.length - 1) !== 10) lines += 1
  return Math.max(0, lines - 1)
}

function formatRowCount(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M rows`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K rows`
  return `${n} rows`
}

function textToBase64(text) {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(binary)
}

function ToggleGroup({ options, value, onChange }) {
  return (
    <div className="inline-flex overflow-hidden rounded-md border border-gray-600">
      {options.map((opt) => {
        const active = value === opt.value
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={`px-3 py-1.5 text-sm font-medium transition-colors ${
              active
                ? 'bg-blue-600 text-white'
                : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
            }`}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}

function SummaryCard({ label, value, valueClass = 'text-white' }) {
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800/80 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-gray-400">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${valueClass}`}>
        {value}
      </div>
    </div>
  )
}

function ExitBar({ label, count, total, color }) {
  const pct = total > 0 ? (count / total) * 100 : 0
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-sm text-gray-300">
        <span>
          {label}: {count} ({pct.toFixed(0)}%)
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded bg-gray-700">
        <div
          className={`h-full ${color}`}
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
    </div>
  )
}

function DayAdjLog({ adjLog }) {
  if (!adjLog?.length) {
    return (
      <p className="px-4 py-3 text-sm text-gray-500">No adjustments this day.</p>
    )
  }
  return (
    <div className="overflow-x-auto px-2 pb-3">
      <table className="min-w-full text-left text-xs text-gray-300">
        <thead className="text-gray-500">
          <tr>
            <th className="px-2 py-1">Time</th>
            <th className="px-2 py-1">Type</th>
            <th className="px-2 py-1">Triggered</th>
            <th className="px-2 py-1">Strike Change</th>
            <th className="px-2 py-1">Premium Change</th>
            <th className="px-2 py-1">Trigger%</th>
            <th className="px-2 py-1">Net MTM</th>
          </tr>
        </thead>
        <tbody>
          {adjLog.map((adj, idx) => (
            <tr key={idx} className="border-t border-gray-800">
              <td className="px-2 py-1 tabular-nums">
                {adj.time || String(adj.minute || '').slice(11, 16) || '—'}
              </td>
              <td className="px-2 py-1">{adj.adj_type}</td>
              <td className="px-2 py-1 uppercase">{adj.triggered_leg}</td>
              <td className="px-2 py-1 tabular-nums">
                {Number(adj.old_strike).toFixed(0)}→
                {Number(adj.new_strike).toFixed(0)}
              </td>
              <td className="px-2 py-1 tabular-nums">
                {Number(adj.old_premium).toFixed(0)}→
                {Number(adj.new_premium).toFixed(0)}
              </td>
              <td className="px-2 py-1 tabular-nums">
                {Number(adj.trigger_pct_reached).toFixed(0)}%
              </td>
              <td className={`px-2 py-1 tabular-nums ${moneyClass(adj.net_pnl_at_adj)}`}>
                {fmtMoney(adj.net_pnl_at_adj, true)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Backtest() {
  const [mode, setMode] = useState('local') // 'local' | 'server'
  const [dataDir, setDataDir] = useState('backtest/data')
  const [params, setParams] = useState({ ...DEFAULT_PARAMS })
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [files, setFiles] = useState([]) // { name, text, rows }
  const [dragOver, setDragOver] = useState(false)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState(null) // { current, total, date }
  const [error, setError] = useState(null)
  const [results, setResults] = useState(null)
  const [expandedDate, setExpandedDate] = useState(null)

  const setParam = useCallback((key, value) => {
    setParams((prev) => ({ ...prev, [key]: value }))
  }, [])

  const ingestFiles = useCallback(async (fileList) => {
    const list = Array.from(fileList || []).filter((f) =>
      f.name.toLowerCase().endsWith('.csv'),
    )
    if (!list.length) return
    const loaded = await Promise.all(
      list.map(
        (file) =>
          new Promise((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => {
              const text = String(reader.result || '')
              resolve({
                name: file.name,
                text,
                rows: countCsvRows(text),
              })
            }
            reader.onerror = () => reject(reader.error || new Error('Read failed'))
            reader.readAsText(file)
          }),
      ),
    )
    setFiles((prev) => {
      const byName = new Map(prev.map((f) => [f.name, f]))
      for (const f of loaded) byName.set(f.name, f)
      return Array.from(byName.values())
    })
    setError(null)
  }, [])

  const onDrop = useCallback(
    (e) => {
      e.preventDefault()
      setDragOver(false)
      ingestFiles(e.dataTransfer?.files)
    },
    [ingestFiles],
  )

  const removeFile = useCallback((name) => {
    setFiles((prev) => prev.filter((f) => f.name !== name))
  }, [])

  const canRun =
    mode === 'local' ? Boolean(dataDir.trim()) : files.length > 0

  const handleRun = useCallback(async () => {
    if (!canRun || running) return
    setRunning(true)
    setError(null)
    setResults(null)
    setExpandedDate(null)
    setProgress(null)
    try {
      if (mode === 'local') {
        const data = await runBacktestLocal(
          { data_dir: dataDir.trim(), params },
          (msg) => {
            if (msg?.type === 'progress') {
              setProgress({
                current: msg.current,
                total: msg.total,
                date: msg.date,
              })
            }
          },
        )
        setResults(data)
      } else {
        const csv_files = files.map((f) => ({
          filename: f.name,
          content_b64: textToBase64(f.text),
        }))
        const data = await runBacktest({ csv_files, params })
        setResults(data)
      }
    } catch (err) {
      setError(err?.message || 'Backtest failed')
    } finally {
      setRunning(false)
      setProgress(null)
    }
  }, [canRun, dataDir, files, mode, params, running])

  const summary = results?.summary
  const days = useMemo(
    () => (results?.days || []).filter((d) => d.data_ok !== false || d.net_pnl != null),
    [results],
  )
  const okDays = useMemo(() => days.filter((d) => d.data_ok), [days])

  const chartData = useMemo(
    () =>
      okDays.map((d) => ({
        date: d.trade_date,
        net_pnl: Number(d.net_pnl) || 0,
      })),
    [okDays],
  )

  const exitTotal =
    (summary?.profit_target_count || 0) +
    (summary?.stoploss_count || 0) +
    (summary?.pre_expiry_count || 0)

  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-white">Backtest</h1>
        <p className="mt-1 text-sm text-gray-400">
          Simulate short straddle / strangle days from Delta options trade CSVs.
        </p>
        <div className="mt-4">
          <ToggleGroup
            value={mode}
            onChange={setMode}
            options={[
              { value: 'local', label: '💻 Local Mode' },
              { value: 'server', label: '🌐 Server Mode' },
            ]}
          />
        </div>
        <p className="mt-2 text-xs text-gray-500">
          {mode === 'local'
            ? 'Reads CSV files from disk via the local API (no upload). Start backend with: python backtest/run_local_server.py'
            : 'Uploads CSV content as base64 to the API (slower for large files).'}
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Parameters */}
        <section className="rounded-lg border border-gray-700 bg-gray-800/50 p-4">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-gray-400">
            Parameters
          </h2>

          <div className="space-y-4">
            <div>
              <div className="mb-1.5 text-sm text-gray-300">Trade Type</div>
              <ToggleGroup
                value={params.trade_type}
                onChange={(v) => setParam('trade_type', v)}
                options={[
                  { value: 'straddle', label: 'Straddle' },
                  { value: 'strangle', label: 'Strangle' },
                ]}
              />
            </div>

            <div>
              <div className="mb-1.5 text-sm text-gray-300">Expiry</div>
              <ToggleGroup
                value={params.expiry_type}
                onChange={(v) => setParam('expiry_type', v)}
                options={[
                  { value: '0DTE', label: '0DTE' },
                  { value: '1DTE', label: '1DTE' },
                ]}
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="block text-sm text-gray-300">
                Entry Hour IST
                <input
                  type="number"
                  min={0}
                  max={23}
                  value={params.entry_hour_ist}
                  onChange={(e) =>
                    setParam('entry_hour_ist', Number(e.target.value))
                  }
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              <label className="block text-sm text-gray-300">
                Entry Minute
                <input
                  type="number"
                  min={0}
                  max={59}
                  value={params.entry_minute_ist}
                  onChange={(e) =>
                    setParam('entry_minute_ist', Number(e.target.value))
                  }
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
            </div>

            <label className="block text-sm text-gray-300">
              Quantity (lots)
              <input
                type="number"
                min={1}
                value={params.quantity}
                onChange={(e) => setParam('quantity', Number(e.target.value))}
                className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
              />
            </label>

            <div className="grid grid-cols-3 gap-3">
              <label className="block text-sm text-gray-300">
                Trigger %
                <input
                  type="number"
                  min={100}
                  max={500}
                  value={params.trigger_pct}
                  onChange={(e) =>
                    setParam('trigger_pct', Number(e.target.value))
                  }
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              <label className="block text-sm text-gray-300">
                Profit Target %
                <input
                  type="number"
                  min={1}
                  value={params.profit_target_pct}
                  onChange={(e) =>
                    setParam('profit_target_pct', Number(e.target.value))
                  }
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              <label className="block text-sm text-gray-300">
                Stop Loss %
                <input
                  type="number"
                  min={1}
                  value={params.stoploss_pct}
                  onChange={(e) =>
                    setParam('stoploss_pct', Number(e.target.value))
                  }
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
            </div>

            {params.trade_type === 'strangle' && (
              <label className="block text-sm text-gray-300">
                Target Premium / Side $
                <input
                  type="number"
                  min={1}
                  value={params.target_premium_per_side}
                  onChange={(e) =>
                    setParam('target_premium_per_side', Number(e.target.value))
                  }
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
            )}

            <button
              type="button"
              onClick={() => setShowAdvanced((v) => !v)}
              className="text-sm text-blue-400 hover:text-blue-300"
            >
              {showAdvanced ? '▾ Hide Advanced' : '⚙ Advanced'}
            </button>

            {showAdvanced && (
              <div className="grid grid-cols-2 gap-3 rounded-md border border-gray-700 bg-gray-900/50 p-3">
                <label className="block text-sm text-gray-300">
                  Min Replacement Premium $
                  <input
                    type="number"
                    min={0}
                    value={params.min_replacement_premium}
                    onChange={(e) =>
                      setParam(
                        'min_replacement_premium',
                        Number(e.target.value),
                      )
                    }
                    className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  />
                </label>
                <label className="block text-sm text-gray-300">
                  Conversion Equality %
                  <input
                    type="number"
                    min={0}
                    value={params.conversion_equality_pct}
                    onChange={(e) =>
                      setParam(
                        'conversion_equality_pct',
                        Number(e.target.value),
                      )
                    }
                    className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  />
                </label>
                <label className="block text-sm text-gray-300">
                  Fee per leg $
                  <input
                    type="number"
                    min={0}
                    step={0.01}
                    value={params.fee_per_leg_usd}
                    onChange={(e) =>
                      setParam('fee_per_leg_usd', Number(e.target.value))
                    }
                    className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  />
                </label>
                <label className="block text-sm text-gray-300">
                  Slippage %
                  <input
                    type="number"
                    min={0}
                    step={0.1}
                    value={params.slippage_pct}
                    onChange={(e) =>
                      setParam('slippage_pct', Number(e.target.value))
                    }
                    className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  />
                </label>
              </div>
            )}
          </div>
        </section>

        {/* Data source + Run */}
        <section className="space-y-4">
          {mode === 'local' ? (
            <div className="rounded-lg border border-gray-700 bg-gray-800/50 p-6">
              <div className="text-3xl">💻</div>
              <p className="mt-2 text-sm text-gray-300">
                Local Mode reads <code className="text-blue-300">BTC_*.csv</code>{' '}
                directly from disk — no upload.
              </p>
              <label className="mt-4 block text-sm text-gray-300">
                Data directory
                <input
                  type="text"
                  value={dataDir}
                  onChange={(e) => setDataDir(e.target.value)}
                  placeholder="backtest/data"
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 font-mono text-sm text-white"
                />
              </label>
              <p className="mt-2 text-xs text-gray-500">
                Relative to project root. Example: backtest/data
              </p>
            </div>
          ) : (
            <div
              onDragOver={(e) => {
                e.preventDefault()
                setDragOver(true)
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              className={`rounded-lg border-2 border-dashed p-6 text-center transition-colors ${
                dragOver
                  ? 'border-blue-500 bg-blue-500/10'
                  : 'border-gray-600 bg-gray-800/50'
              }`}
            >
              <div className="text-3xl">📁</div>
              <p className="mt-2 text-sm text-gray-300">
                Drop CSV files here or click to browse
              </p>
              <p className="mt-1 text-xs text-gray-500">
                Large files may take 30–60 seconds to process
              </p>
              <label className="mt-4 inline-block cursor-pointer rounded-md bg-gray-700 px-4 py-2 text-sm text-white hover:bg-gray-600">
                Browse files
                <input
                  type="file"
                  accept=".csv,text/csv"
                  multiple
                  className="hidden"
                  onChange={(e) => {
                    ingestFiles(e.target.files)
                    e.target.value = ''
                  }}
                />
              </label>

              {files.length > 0 && (
                <ul className="mt-4 space-y-2 text-left">
                  {files.map((f) => (
                    <li
                      key={f.name}
                      className="flex items-center justify-between gap-2 rounded-md border border-gray-700 bg-gray-900/60 px-3 py-2 text-sm"
                    >
                      <span className="truncate text-green-400">
                        ✅ {f.name}{' '}
                        <span className="text-gray-400">
                          {formatRowCount(f.rows)}
                        </span>
                      </span>
                      <button
                        type="button"
                        onClick={() => removeFile(f.name)}
                        className="shrink-0 text-xs text-red-400 hover:text-red-300"
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <button
            type="button"
            disabled={!canRun || running}
            onClick={handleRun}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-blue-600 px-4 py-3 text-sm font-semibold text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {running ? (
              <>
                <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                {progress
                  ? `Processing ${progress.date}... (${progress.current}/${progress.total})`
                  : 'Running backtest…'}
              </>
            ) : (
              <>▶ Run Backtest</>
            )}
          </button>

          {progress && (
            <p className="text-center text-sm text-blue-300">
              Processing {progress.date}... ({progress.current}/{progress.total})
            </p>
          )}

          {error && (
            <div className="rounded-md border border-red-700 bg-red-900/30 px-3 py-2 text-sm text-red-300">
              {error}
            </div>
          )}
        </section>
      </div>

      {/* Results */}
      {results && summary && (
        <section className="mt-8 space-y-6">
          <h2 className="text-lg font-semibold text-white">Results</h2>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <SummaryCard
              label="Win Rate"
              value={`${Number(summary.win_rate).toFixed(1)}%`}
            />
            <SummaryCard
              label="Net P&L"
              value={fmtMoney(summary.total_net_pnl, true)}
              valueClass={moneyClass(summary.total_net_pnl)}
            />
            <SummaryCard
              label="Avg Win"
              value={fmtMoney(summary.avg_win, true)}
              valueClass="text-green-400"
            />
            <SummaryCard
              label="Avg Loss"
              value={fmtMoney(summary.avg_loss, true)}
              valueClass="text-red-400"
            />
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <SummaryCard
              label="Max Drawdown"
              value={fmtMoney(summary.max_drawdown, true)}
              valueClass="text-red-400"
            />
            <SummaryCard label="Total Adj" value={String(summary.total_adjustments)} />
            <SummaryCard
              label="Conv Mode"
              value={String(summary.total_conversions)}
            />
          </div>

          <div className="rounded-lg border border-gray-700 bg-gray-800/50 p-4 space-y-3">
            <h3 className="text-sm font-semibold text-gray-300">Exit reasons</h3>
            <ExitBar
              label="Profit Target"
              count={summary.profit_target_count || 0}
              total={exitTotal}
              color="bg-green-500"
            />
            <ExitBar
              label="Stop Loss"
              count={summary.stoploss_count || 0}
              total={exitTotal}
              color="bg-red-500"
            />
            <ExitBar
              label="Pre-Expiry"
              count={summary.pre_expiry_count || 0}
              total={exitTotal}
              color="bg-amber-500"
            />
            <p className="text-xs text-gray-500">
              {summary.data_ok_days}/{summary.total_days} days with usable data
            </p>
          </div>

          <div className="rounded-lg border border-gray-700 bg-gray-800/50 p-4">
            <h3 className="mb-3 text-sm font-semibold text-gray-300">
              Day-by-day Net P&L
            </h3>
            <div className="h-64 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                  <XAxis
                    dataKey="date"
                    tick={{ fill: '#9ca3af', fontSize: 11 }}
                    tickFormatter={(v) => String(v).slice(5)}
                  />
                  <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
                  <Tooltip
                    contentStyle={{
                      background: '#111827',
                      border: '1px solid #374151',
                      borderRadius: 8,
                    }}
                    formatter={(v) => [fmtMoney(v, true), 'Net P&L']}
                  />
                  <ReferenceLine y={0} stroke="#6b7280" />
                  <Bar dataKey="net_pnl" radius={[2, 2, 0, 0]}>
                    {chartData.map((entry, i) => (
                      <Cell
                        key={i}
                        fill={entry.net_pnl >= 0 ? '#22c55e' : '#ef4444'}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="overflow-x-auto rounded-lg border border-gray-700 bg-gray-800/50">
            <table className="min-w-full text-left text-sm">
              <thead className="border-b border-gray-700 text-xs uppercase text-gray-400">
                <tr>
                  <th className="px-3 py-2">Date</th>
                  <th className="px-3 py-2">Strike</th>
                  <th className="px-3 py-2">Entry Prem</th>
                  <th className="px-3 py-2">Exit Reason</th>
                  <th className="px-3 py-2">Adj</th>
                  <th className="px-3 py-2">Net P&L</th>
                  <th className="px-3 py-2">Max DD</th>
                </tr>
              </thead>
              <tbody>
                {days.map((d) => {
                  const open = expandedDate === d.trade_date
                  const strikeLabel =
                    d.call_strike === d.put_strike
                      ? Number(d.call_strike).toFixed(0)
                      : `${Number(d.call_strike).toFixed(0)} / ${Number(d.put_strike).toFixed(0)}`
                  return (
                    <Fragment key={d.trade_date}>
                      <tr
                        onClick={() =>
                          setExpandedDate(open ? null : d.trade_date)
                        }
                        className={`cursor-pointer border-b border-gray-800 hover:bg-gray-700/40 ${
                          !d.data_ok ? 'opacity-50' : ''
                        }`}
                      >
                        <td className="px-3 py-2 tabular-nums text-gray-200">
                          {open ? '▾ ' : '▸ '}
                          {d.trade_date}
                        </td>
                        <td className="px-3 py-2 tabular-nums text-gray-300">
                          {d.data_ok ? strikeLabel : '—'}
                        </td>
                        <td className="px-3 py-2 tabular-nums text-gray-300">
                          {d.data_ok
                            ? Number(d.initial_premium).toFixed(0)
                            : '—'}
                        </td>
                        <td className="px-3 py-2 text-gray-300">
                          {d.exit_reason}
                          {d.exit_time ? ` @ ${d.exit_time}` : ''}
                        </td>
                        <td className="px-3 py-2 tabular-nums text-gray-300">
                          {d.adjustments ?? 0}
                        </td>
                        <td
                          className={`px-3 py-2 tabular-nums font-medium ${moneyClass(d.net_pnl)}`}
                        >
                          {fmtMoney(d.net_pnl, true)}
                        </td>
                        <td className="px-3 py-2 tabular-nums text-red-400">
                          {fmtMoney(d.max_drawdown, true)}
                        </td>
                      </tr>
                      {open && (
                        <tr className="bg-gray-900/80">
                          <td colSpan={7} className="px-0 py-0">
                            <DayAdjLog adjLog={d.adj_log} />
                            {d.notes ? (
                              <p className="px-4 pb-3 text-xs text-amber-400">
                                {d.notes}
                              </p>
                            ) : null}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </main>
  )
}
