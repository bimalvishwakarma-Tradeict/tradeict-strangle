import { useCallback, useEffect, useMemo, useState } from 'react'
import { downloadLogFile, getBotLogs } from '../services/api'

const REFRESH_MS = 5000
const PAGE_SIZE = 50
const MAX_ROWS = 200

const EVENT_STYLE = {
  MONITOR_TICK: 'text-gray-500',
  PNL_CHECK: 'text-gray-400',
  TRIGGER_CHECK: 'text-blue-400',
  PRICE_UPDATE: 'text-gray-400',
  SETTLING: 'text-gray-500 italic',
  DECISION_TRIGGER: 'text-violet-400',
  ADJUSTMENT_START: 'text-amber-400',
  ADJUSTMENT_DONE: 'text-green-400',
  ADJUSTMENT_FAIL: 'text-red-400',
  ADJUSTMENT_HOLD: 'text-yellow-300',
  EXIT_TRIGGERED: 'text-orange-400',
  EXIT_DONE: 'text-green-400',
  EXIT_FAIL: 'text-red-400',
  ERROR: 'text-red-500',
}

const EVENT_ICON = {
  DECISION_TRIGGER: '⚖️',
  ADJUSTMENT_START: '⚠️',
  ADJUSTMENT_DONE: '✅',
  ADJUSTMENT_FAIL: '❌',
  ADJUSTMENT_HOLD: '⏸️',
  EXIT_TRIGGERED: '🚨',
  EXIT_DONE: '✅',
  EXIT_FAIL: '❌',
  ERROR: '❌',
}

function formatTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString('en-IN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return String(iso).slice(11, 19)
  }
}

function formatMoneySigned(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return `${n >= 0 ? '+' : '-'}$${abs}`
}

function detailPreview(details, eventType) {
  if (!details || typeof details !== 'object') return '—'
  if (eventType === 'DECISION_TRIGGER') {
    const leg = String(details.leg || '?').toUpperCase()
    const pct = Number(details.trigger_pct)
    const net = Number(details.net_mtm)
    const closing = details.decision === 'CLOSE_PROFITABLE'
    return (
      `DECISION: ${leg} hit ${Number.isFinite(pct) ? pct : '—'}% — ` +
      `Net MTM ${formatMoneySigned(net)} → ` +
      (closing ? 'CLOSING BASKET (profitable)' : 'ADJUSTING (loss)')
    )
  }
  if (eventType === 'TRIGGER_CHECK' && details.trigger_mode === 'premium') {
    const note = details.trigger_pct_note
    if (note) return String(note)
    const callPct = details.call_trigger_pct
    const putPct = details.put_trigger_pct
    const callBand = details.call_premium_band || ''
    const putBand = details.put_premium_band || ''
    return (
      `call ${callPct}% (${callBand}) · put ${putPct}% (${putBand}) · ` +
      `action=${details.action || '—'}`
    )
  }
  const keys = Object.keys(details).slice(0, 4)
  return keys.map((k) => `${k}=${details[k]}`).join(' · ')
}

export default function Logs() {
  const [logs, setLogs] = useState([])
  const [level, setLevel] = useState('all')
  const [tradeFilter, setTradeFilter] = useState('')
  const [live, setLive] = useState(true)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())
  const [visible, setVisible] = useState(PAGE_SIZE)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    document.title = 'Delta Bot — Logs'
  }, [])

  const fetchLogs = useCallback(async () => {
    try {
      const tradeId = tradeFilter.trim() ? Number(tradeFilter) : null
      const data = await getBotLogs({
        trade_id: Number.isFinite(tradeId) && tradeId > 0 ? tradeId : undefined,
        limit: MAX_ROWS,
        level,
      })
      setLogs(data?.logs || [])
      setError('')
      setLoading(false)
    } catch (err) {
      setError(err.message || 'Failed to load logs')
      setLoading(false)
    }
  }, [level, tradeFilter])

  useEffect(() => {
    fetchLogs()
  }, [fetchLogs])

  useEffect(() => {
    if (!live) return undefined
    const id = setInterval(fetchLogs, REFRESH_MS)
    return () => clearInterval(id)
  }, [live, fetchLogs])

  const tradeIds = useMemo(() => {
    const ids = new Set()
    for (const row of logs) {
      if (row.trade_id != null) ids.add(row.trade_id)
    }
    return Array.from(ids).sort((a, b) => b - a)
  }, [logs])

  const shown = logs.slice(0, visible)

  const toggleExpand = (key) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const onDownload = async () => {
    try {
      await downloadLogFile()
    } catch (err) {
      setError(err.message || 'Download failed')
    }
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-white">🤖 Bot Activity Log</h1>
        <button
          type="button"
          onClick={onDownload}
          className="rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800"
        >
          Download Log File
        </button>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2 text-gray-300">
          Filter
          <select
            value={level}
            onChange={(e) => {
              setLevel(e.target.value)
              setVisible(PAGE_SIZE)
            }}
            className="rounded border border-gray-600 bg-gray-800 px-2 py-1 text-gray-100"
          >
            <option value="all">All Events</option>
            <option value="important">Important Only</option>
          </select>
        </label>

        <label className="flex items-center gap-2 text-gray-300">
          Trade#
          <select
            value={tradeFilter}
            onChange={(e) => {
              setTradeFilter(e.target.value)
              setVisible(PAGE_SIZE)
            }}
            className="rounded border border-gray-600 bg-gray-800 px-2 py-1 text-gray-100"
          >
            <option value="">All</option>
            {tradeIds.map((id) => (
              <option key={id} value={String(id)}>
                #{id}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          onClick={() => setLive((v) => !v)}
          className={`rounded-md border px-3 py-1 ${
            live
              ? 'border-green-700 bg-green-950/40 text-green-300'
              : 'border-gray-600 text-gray-300'
          }`}
        >
          {live ? '🔄 Live' : '⏸ Paused'}
        </button>

        <button
          type="button"
          onClick={fetchLogs}
          className="rounded-md border border-gray-600 px-3 py-1 text-gray-300 hover:bg-gray-800"
        >
          Refresh
        </button>
      </div>

      {live && (
        <div className="mb-3 flex items-center gap-2 text-xs text-green-400">
          <span className="relative flex h-2.5 w-2.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-60" />
            <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-green-500" />
          </span>
          LIVE — auto-refreshing every 5s
        </div>
      )}

      {error && (
        <div className="mb-3 rounded border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-gray-700">
        <table className="min-w-full text-left text-sm">
          <thead className="bg-gray-800/80 text-xs uppercase tracking-wide text-gray-400">
            <tr>
              <th className="px-3 py-2">Time</th>
              <th className="px-3 py-2">Event</th>
              <th className="px-3 py-2">Trade</th>
              <th className="px-3 py-2">Details</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && shown.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-gray-500">
                  No log events yet — place a trade or wait for the next monitor tick
                </td>
              </tr>
            )}
            {shown.map((row, idx) => {
              const key = `${row.timestamp}-${row.event_type}-${idx}`
              const style = EVENT_STYLE[row.event_type] || 'text-gray-300'
              const icon = EVENT_ICON[row.event_type] || ''
              const open = expanded.has(key)
              return (
                <tr
                  key={key}
                  className="cursor-pointer border-t border-gray-800 hover:bg-gray-800/50"
                  onClick={() => toggleExpand(key)}
                >
                  <td className="whitespace-nowrap px-3 py-2 align-top text-gray-400">
                    {formatTime(row.timestamp)}
                  </td>
                  <td className={`whitespace-nowrap px-3 py-2 align-top font-medium ${style}`}>
                    {icon ? `${icon} ` : ''}
                    {row.event_type}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2 align-top text-gray-300">
                    #{row.trade_id}
                  </td>
                  <td className="px-3 py-2 align-top text-gray-400">
                    {open ? (
                      <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-gray-950/60 p-2 text-xs text-gray-300">
                        {JSON.stringify(row.details, null, 2)}
                      </pre>
                    ) : (
                      detailPreview(row.details, row.event_type)
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {visible < logs.length && (
        <div className="mt-3 text-center">
          <button
            type="button"
            onClick={() => setVisible((v) => Math.min(v + PAGE_SIZE, MAX_ROWS, logs.length))}
            className="rounded-md border border-gray-600 px-4 py-1.5 text-sm text-gray-300 hover:bg-gray-800"
          >
            Load More ({Math.min(logs.length, MAX_ROWS) - visible} remaining)
          </button>
        </div>
      )}
    </main>
  )
}
