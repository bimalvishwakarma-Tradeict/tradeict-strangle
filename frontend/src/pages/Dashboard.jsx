import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTrades } from '../hooks/useTrades'
import PositionCard from '../components/PositionCard'
import {
  checkHealth,
  getAccountStatus,
  getTradeHistory,
} from '../services/api'

function formatIstTime(date = new Date()) {
  return (
    date.toLocaleTimeString('en-IN', {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true,
      timeZone: 'Asia/Kolkata',
    }) + ' IST'
  )
}

function formatAdjTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('en-IN', {
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'Asia/Kolkata',
    })
  } catch {
    return iso
  }
}

function fmtStrike(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function fmtMoney(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  })
}

function pnlColor(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'text-gray-300'
  return n > 0 ? 'text-green-400' : 'text-red-400'
}

function SkeletonCard() {
  return (
    <div className="animate-pulse rounded-xl border border-gray-700 bg-gray-800 p-4">
      <div className="mb-3 h-4 w-1/3 rounded bg-gray-700" />
      <div className="mb-2 h-3 w-full rounded bg-gray-700/80" />
      <div className="mb-2 h-3 w-5/6 rounded bg-gray-700/80" />
      <div className="mt-4 h-8 w-full rounded bg-gray-700/60" />
    </div>
  )
}

function formatBalance(value) {
  return Number(value || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function BasketHistoryCard({ basket }) {
  const [open, setOpen] = useState(false)
  const pnl = Number(basket.realized_pnl || 0)
  const feesPaid = Number(basket.fees_paid || 0)
  const netMtm = Number(
    basket.net_mtm != null ? basket.net_mtm : pnl - feesPaid,
  )
  const status = String(basket.status || '').toLowerCase()
  const isActive = status === 'active'

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center justify-between gap-2 px-4 py-3 text-left text-sm"
      >
        <div className="font-medium text-white">
          Basket #{basket.basket_number} · {basket.underlying}
          <span
            className={`ml-2 rounded px-2 py-0.5 text-xs ${
              isActive
                ? 'bg-green-900/50 text-green-300'
                : 'bg-gray-700 text-gray-300'
            }`}
          >
            {status}
          </span>
        </div>
        <div className="text-right text-gray-400">
          <div>
            Exp {basket.expiry_date} · Gross{' '}
            <span className={pnlColor(pnl)}>
              {pnl >= 0 ? '+' : ''}${fmtMoney(pnl)}
            </span>
          </div>
          <div className="text-xs">
            Fees ${fmtMoney(feesPaid)} · Net{' '}
            <span className={pnlColor(netMtm)}>
              {netMtm >= 0 ? '+' : ''}${fmtMoney(netMtm)}
            </span>
            <span className="ml-2 text-gray-500">{open ? '▲' : '▼'}</span>
          </div>
        </div>
      </button>
      {open && (
        <div className="space-y-3 border-t border-gray-700 px-4 py-3">
          <div className="text-xs text-gray-400">
            Entry {formatAdjTime(basket.entry_time)}
            {basket.exit_time ? ` · Exit ${formatAdjTime(basket.exit_time)}` : ''}
            {basket.exit_reason ? ` · ${basket.exit_reason}` : ''}
          </div>
          <div className="grid gap-2 rounded-lg border border-gray-700 bg-gray-900/40 px-3 py-2 text-xs text-gray-300 sm:grid-cols-3">
            <div>
              Gross MTM:{' '}
              <span className={pnlColor(pnl)}>
                {pnl >= 0 ? '+' : ''}${fmtMoney(pnl)}
              </span>
            </div>
            <div className="text-amber-200/90">
              Total Fees: ${fmtMoney(feesPaid)}
            </div>
            <div>
              NET MTM:{' '}
              <span className={pnlColor(netMtm)}>
                {netMtm >= 0 ? '+' : ''}${fmtMoney(netMtm)}
              </span>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-xs text-gray-200">
              <thead className="text-[10px] uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-1">Type</th>
                  <th className="px-2 py-1">Strike</th>
                  <th className="px-2 py-1">Qty</th>
                  <th className="px-2 py-1">Entry $</th>
                  <th className="px-2 py-1">Exit $</th>
                  <th className="px-2 py-1">Entry Fee</th>
                  <th className="px-2 py-1">Exit Fee</th>
                  <th className="px-2 py-1">Entry time</th>
                  <th className="px-2 py-1">Exit time</th>
                  <th className="px-2 py-1">Status</th>
                  <th className="px-2 py-1">Realized</th>
                </tr>
              </thead>
              <tbody>
                {(basket.legs || []).map((leg) => (
                  <tr key={leg.id} className="border-t border-gray-800">
                    <td className="px-2 py-1 uppercase">{leg.leg_type}</td>
                    <td className="px-2 py-1">${fmtStrike(leg.strike)}</td>
                    <td className="px-2 py-1">{leg.quantity}</td>
                    <td className="px-2 py-1">${fmtMoney(leg.entry_premium)}</td>
                    <td className="px-2 py-1">${fmtMoney(leg.exit_premium)}</td>
                    <td className="px-2 py-1 text-amber-200/80">
                      {leg.entry_fee_usd != null
                        ? `$${fmtMoney(leg.entry_fee_usd)}`
                        : '—'}
                    </td>
                    <td className="px-2 py-1 text-amber-200/80">
                      {leg.exit_fee_usd != null
                        ? `$${fmtMoney(leg.exit_fee_usd)}`
                        : '—'}
                    </td>
                    <td className="px-2 py-1">{formatAdjTime(leg.entry_time)}</td>
                    <td className="px-2 py-1">{formatAdjTime(leg.exit_time)}</td>
                    <td className="px-2 py-1">{leg.status}</td>
                    <td className={`px-2 py-1 ${pnlColor(leg.realized_pnl)}`}>
                      {leg.realized_pnl != null
                        ? `${Number(leg.realized_pnl) >= 0 ? '+' : ''}$${fmtMoney(leg.realized_pnl)}`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {(basket.adjustments || []).length > 0 && (
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-gray-500">
                Adjustments
              </div>
              <ul className="space-y-1 text-xs text-gray-400">
                {basket.adjustments.map((a, i) => (
                  <li key={`${a.timestamp}-${i}`}>
                    {formatAdjTime(a.timestamp)} · {String(a.leg_type).toUpperCase()}{' '}
                    ${fmtStrike(a.old_strike)} → ${fmtStrike(a.new_strike)} · exit $
                    {fmtMoney(a.old_exit_premium)} · entry ${fmtMoney(a.new_entry_premium)}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function Dashboard() {
  const { trades, wsStatus, loading, errors, adjustments } = useTrades()
  const [updatedAt, setUpdatedAt] = useState(() => formatIstTime())
  const [backendOnline, setBackendOnline] = useState(true)
  const [accountName, setAccountName] = useState('')
  const [balance, setBalance] = useState(0)
  const [accountConnected, setAccountConnected] = useState(false)
  const [baskets, setBaskets] = useState([])

  useEffect(() => {
    document.title = 'Delta Bot — Dashboard'
  }, [])

  useEffect(() => {
    const id = setInterval(() => setUpdatedAt(formatIstTime()), 1000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    let cancelled = false
    async function loadMeta() {
      try {
        const ok = await checkHealth()
        if (!cancelled) setBackendOnline(ok)
      } catch {
        if (!cancelled) setBackendOnline(false)
      }
      try {
        const status = await getAccountStatus()
        if (cancelled) return
        setAccountConnected(Boolean(status?.connected))
        if (status?.connected) {
          setAccountName(status.account_name || '')
          setBalance(Number(status.balance_usdt || 0))
        }
      } catch {
        // keep last known
      }
    }
    loadMeta()
    const id = setInterval(loadMeta, 60000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  // Basket history (all baskets — not only currently active)
  useEffect(() => {
    let cancelled = false
    async function loadHistory() {
      try {
        const data = await getTradeHistory(40)
        if (cancelled) return
        setBaskets(Array.isArray(data?.baskets) ? data.baskets : [])
      } catch {
        if (!cancelled) setBaskets([])
      }
    }
    loadHistory()
    const id = setInterval(loadHistory, 30000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [trades, adjustments])

  const wsLabel = useMemo(() => {
    if (wsStatus === 'connected') return { text: 'connected', className: 'text-green-400' }
    if (wsStatus === 'connecting') {
      return { text: 'reconnecting...', className: 'text-yellow-400' }
    }
    return { text: 'disconnected', className: 'text-yellow-400' }
  }, [wsStatus])

  const showOffline =
    !backendOnline ||
    (Boolean(errors.global) &&
      /network|failed|ECONNREFUSED|offline|timeout/i.test(errors.global || ''))

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      {wsStatus !== 'connected' && (
        <div className="mb-4 rounded-lg border border-yellow-600/50 bg-yellow-950/40 px-4 py-3 text-sm text-yellow-100">
          <div className="font-medium">
            ⚠️ Live connection lost. Reconnecting...
          </div>
          <div className="mt-0.5 text-yellow-200/80">
            Showing last known data. Auto-reconnects in 3s
          </div>
        </div>
      )}

      {showOffline && (
        <div className="mb-4 rounded-lg border border-red-700/50 bg-red-950/40 px-4 py-3 text-sm text-red-200">
          Backend offline. Start the server and refresh.
        </div>
      )}

      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <h1 className="text-2xl font-semibold text-white">Dashboard</h1>
        <div className="text-right text-sm text-gray-400">
          <div>
            WS:{' '}
            <span className={wsLabel.className}>{wsLabel.text}</span>
          </div>
          <div>Updated: {updatedAt}</div>
        </div>
      </div>

      {errors.global && !showOffline && (
        <div className="mb-4 rounded border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          {errors.global}
        </div>
      )}

      {loading ? (
        <div className="space-y-4">
          <SkeletonCard />
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : trades.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-700 bg-gray-800/50 px-6 py-12 text-center">
          <div className="mb-3 text-4xl">🤖</div>
          <p className="text-lg font-medium text-white">No active trades</p>
          <p className="mt-1 text-sm text-gray-400">
            Bot is ready and monitoring
          </p>
          <Link
            to="/new-trade"
            className="mt-6 inline-block rounded-md bg-blue-500 px-4 py-2 text-sm font-medium text-white hover:bg-blue-400"
          >
            → Place New Strangle
          </Link>
          <p className="mx-auto mt-4 max-w-md text-sm text-gray-400">
            Select strikes in the bot — orders are placed on Delta Exchange
            automatically, then monitored.
          </p>
          {accountConnected && (
            <div className="mt-6 space-y-1 text-sm text-gray-400">
              <div>Connected: {accountName || '—'}</div>
              <div>Balance: ${formatBalance(balance)}</div>
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-4">
          {trades.map((trade) => (
            <PositionCard
              key={trade.trade_id}
              trade={trade}
              recentAdjustments={adjustments}
            />
          ))}
        </div>
      )}

      <section className="mt-10">
        <h2 className="mb-3 text-lg font-semibold text-white">
          Basket History
        </h2>
        <p className="mb-3 text-xs text-gray-500">
          Each new strangle is a numbered basket. Adjustments and closed legs stay
          with that basket — final PnL is the sum of realized legs.
        </p>
        {baskets.length === 0 ? (
          <div className="rounded-xl border border-dashed border-gray-700 px-4 py-8 text-center text-sm text-gray-500">
            No baskets yet
          </div>
        ) : (
          <div className="space-y-3">
            {baskets.map((b) => (
              <BasketHistoryCard key={b.trade_id} basket={b} />
            ))}
          </div>
        )}
      </section>
    </main>
  )
}
