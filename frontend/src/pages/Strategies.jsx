import { useCallback, useEffect, useMemo, useState } from 'react'
import { getStrategiesRegistry } from '../services/api'
import LoadingSpinner from '../components/ui/LoadingSpinner'

const STATUS_STYLES = {
  LIVE: 'bg-green-900/60 text-green-300 border-green-700',
  TESTING: 'bg-blue-900/60 text-blue-300 border-blue-700',
  PARKED: 'bg-yellow-900/60 text-yellow-200 border-yellow-700',
  CLOSED: 'bg-gray-700 text-gray-300 border-gray-600',
}

const TABS = ['Rules', 'Results', 'Tests done', 'Open questions', 'Next steps']

function fmt(v, digits = 4) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function StatusBadge({ status }) {
  const key = String(status || 'TESTING').toUpperCase()
  const cls = STATUS_STYLES[key] || STATUS_STYLES.TESTING
  return (
    <span className={`rounded border px-2 py-0.5 text-xs font-semibold ${cls}`}>
      {key}
    </span>
  )
}

function StrategyDetail({ strategy }) {
  const [tab, setTab] = useState('Rules')
  const tests = strategy.tests_done || []

  return (
    <div className="mt-4 border-t border-gray-700 pt-4">
      <div className="mb-3 flex flex-wrap gap-2">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`rounded px-3 py-1 text-sm ${
              tab === t
                ? 'bg-blue-600 text-white'
                : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'Rules' && (
        <pre className="whitespace-pre-wrap rounded bg-gray-950/60 p-3 text-sm text-gray-300">
          {strategy.rules_summary || '—'}
        </pre>
      )}

      {tab === 'Results' && (
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="text-gray-400">
              <tr>
                <th className="px-2 py-1">Stage</th>
                <th className="px-2 py-1">Window</th>
                <th className="px-2 py-1">n</th>
                <th className="px-2 py-1">win%</th>
                <th className="px-2 py-1">net/day</th>
                <th className="px-2 py-1">CI</th>
                <th className="px-2 py-1">Worst</th>
                <th className="px-2 py-1">Max DD</th>
                <th className="px-2 py-1">Verdict</th>
              </tr>
            </thead>
            <tbody>
              {tests.length === 0 ? (
                <tr>
                  <td colSpan={9} className="px-2 py-3 text-gray-500">
                    No runs yet
                  </td>
                </tr>
              ) : (
                tests.map((t, i) => (
                  <tr key={i} className="border-t border-gray-800">
                    <td className="px-2 py-1">{t.stage || '—'}</td>
                    <td className="px-2 py-1">{t.window || '—'}</td>
                    <td className="px-2 py-1">{t.n ?? '—'}</td>
                    <td className="px-2 py-1">{fmt(t.win_pct, 1)}</td>
                    <td className="px-2 py-1">{fmt(t.mean_day)}</td>
                    <td className="px-2 py-1">
                      [{fmt(t.ci_lo)}, {fmt(t.ci_hi)}]
                    </td>
                    <td className="px-2 py-1">{fmt(t.worst_net)}</td>
                    <td className="px-2 py-1">{fmt(t.max_dd)}</td>
                    <td className="px-2 py-1">{t.verdict || '—'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'Tests done' && (
        <ul className="list-disc space-y-1 pl-5 text-sm text-gray-300">
          {tests.length === 0 && <li>None recorded</li>}
          {tests.map((t, i) => (
            <li key={i}>
              {t.stage}: {t.window} — {t.verdict || '—'}
              {t.date ? ` (${t.date})` : ''}
            </li>
          ))}
        </ul>
      )}

      {tab === 'Open questions' && (
        <ul className="list-disc space-y-1 pl-5 text-sm text-gray-300">
          {(strategy.open_questions || []).length === 0 && <li>None</li>}
          {(strategy.open_questions || []).map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      )}

      {tab === 'Next steps' && (
        <ul className="list-disc space-y-1 pl-5 text-sm text-gray-300">
          {(strategy.next_steps || []).length === 0 && <li>None</li>}
          {(strategy.next_steps || []).map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default function Strategies() {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [strategies, setStrategies] = useState([])
  const [learnings, setLearnings] = useState([])
  const [statusFilter, setStatusFilter] = useState('ALL')
  const [search, setSearch] = useState('')
  const [openId, setOpenId] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await getStrategiesRegistry()
      const data = res?.data || res
      setStrategies(data?.strategies || [])
      setLearnings(data?.learnings || [])
    } catch (e) {
      setError(e?.message || 'Failed to load registry')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (strategies || []).filter((s) => {
      const st = String(s.status || '').toUpperCase()
      if (statusFilter !== 'ALL' && st !== statusFilter) return false
      if (!q) return true
      const blob = `${s.id || ''} ${s.name || ''} ${s.one_line || ''}`.toLowerCase()
      return blob.includes(q)
    })
  }, [strategies, statusFilter, search])

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <LoadingSpinner />
      </div>
    )
  }

  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-white">Strategies</h1>
        <p className="mt-1 text-sm text-gray-400">
          Research registry — what each strategy does, what was tested, what
          failed. Prevents rebuilding the same idea.
        </p>
      </div>

      {error ? (
        <div className="mb-4 rounded border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      ) : null}

      <section className="mb-8 rounded-lg border border-gray-700 bg-gray-850/30 bg-gray-800/40 p-4">
        <h2 className="mb-2 text-lg font-medium text-white">Learnings</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-gray-300">
          {learnings.length === 0 && <li>No learnings yet</li>}
          {learnings.map((L, i) => (
            <li key={i}>{L}</li>
          ))}
        </ul>
      </section>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search name…"
          className="rounded border border-gray-600 bg-gray-900 px-3 py-2 text-sm text-gray-100"
        />
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded border border-gray-600 bg-gray-900 px-3 py-2 text-sm text-gray-100"
        >
          <option value="ALL">All statuses</option>
          <option value="LIVE">LIVE</option>
          <option value="TESTING">TESTING</option>
          <option value="PARKED">PARKED</option>
          <option value="CLOSED">CLOSED</option>
        </select>
        <button
          type="button"
          onClick={load}
          className="rounded bg-gray-700 px-3 py-2 text-sm text-gray-100 hover:bg-gray-600"
        >
          Refresh
        </button>
      </div>

      <div className="space-y-3">
        {filtered.map((s) => {
          const open = openId === s.id
          return (
            <div
              key={s.id}
              className="rounded-lg border border-gray-700 bg-gray-800/50 p-4"
            >
              <button
                type="button"
                className="flex w-full items-start justify-between gap-3 text-left"
                onClick={() => setOpenId(open ? null : s.id)}
              >
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm text-blue-300">{s.id}</span>
                    <span className="text-lg font-medium text-white">{s.name}</span>
                    <StatusBadge status={s.status} />
                  </div>
                  <p className="mt-1 text-sm text-gray-400">{s.one_line}</p>
                </div>
                <span className="text-gray-500">{open ? '▾' : '▸'}</span>
              </button>
              {open ? <StrategyDetail strategy={s} /> : null}
            </div>
          )
        })}
        {filtered.length === 0 && (
          <p className="text-sm text-gray-500">No strategies match filters.</p>
        )}
      </div>
    </main>
  )
}
