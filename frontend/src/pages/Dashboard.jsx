import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTrades } from '../hooks/useTrades'
import HedgePanel from '../components/HedgePanel'
import StructurePnlBar from '../components/StructurePnlBar'
import PositionCard, { BasketStorySection } from '../components/PositionCard'
import PayoffGraph from '../components/PayoffGraph'
import InfoTooltip from '../components/InfoTooltip'
import {
  checkHealth,
  enableAutoTrade,
  getAccountStatus,
  getAutoTradeStatus,
  getHedgeStructures,
  getStructureLedger,
  getSlaveOverview,
  closeSlaveStructure,
} from '../services/api'
import { formatNextEntryWait } from '../utils/nextEntryLabel'

const AUTO_STATUS_POLL_MS = 5000
const SLAVE_OVERVIEW_POLL_MS = 30000
const STRUCTURES_PER_PAGE = 20

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

function fmtLegRole(role) {
  return String(role || '')
    .replace(/_/g, ' ')
    .trim()
}

function fmtAllotmentOpen(iso) {
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

function fmtAllotmentWindow(openedAt, closedAt) {
  if (!openedAt) return '—'
  const open = fmtAllotmentOpen(openedAt)
  if (!closedAt) return `${open} → open IST`
  try {
    const close = new Date(closedAt).toLocaleString('en-IN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'Asia/Kolkata',
    })
    return `${open} → ${close} IST`
  } catch {
    return `${open} → — IST`
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text))
  } catch {
    // fallback for older browsers
    const ta = document.createElement('textarea')
    ta.value = String(text)
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
}

function AllotmentLine({ leg }) {
  const adj =
    Number(leg?.adj_seq) > 0 ? (
      <span className="text-gray-600"> · adj {leg.adj_seq}</span>
    ) : null
  return (
    <div className="font-mono text-[11px] leading-relaxed text-gray-500">
      {fmtLegRole(leg.leg_role)}
      {adj}
      <span className="text-gray-600"> · </span>
      <span className="text-gray-400">{leg.symbol || '—'}</span>
      <span className="text-gray-600"> · </span>
      <button
        type="button"
        title="Click to copy product_id"
        onClick={(e) => {
          e.stopPropagation()
          copyText(leg.product_id)
        }}
        className="cursor-copy text-gray-400 underline decoration-dotted underline-offset-2 hover:text-gray-200"
      >
        id {leg.product_id}
      </button>
      <span className="text-gray-600"> · </span>
      {fmtAllotmentWindow(leg.opened_at, leg.closed_at)}
    </div>
  )
}

function AllotmentBlock({ legs, defaultOpen = false }) {
  const [show, setShow] = useState(defaultOpen)
  if (!legs?.length) return null
  return (
    <div className="mt-2 border-t border-gray-800/80 pt-2">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          setShow((v) => !v)
        }}
        className="text-[10px] font-medium uppercase tracking-wide text-gray-500 hover:text-gray-300"
      >
        {show ? 'Hide allotment IDs' : 'Show allotment IDs'}
        <span className="ml-1 normal-case text-gray-600">({legs.length})</span>
      </button>
      {show ? (
        <div className="mt-1.5 space-y-0.5">
          {legs.map((leg) => (
            <AllotmentLine key={leg.id} leg={leg} />
          ))}
        </div>
      ) : null}
    </div>
  )
}

function syncAgeSeconds(iso) {
  if (!iso) return null
  try {
    const t = new Date(iso).getTime()
    if (!Number.isFinite(t)) return null
    return Math.max(0, Math.floor((Date.now() - t) / 1000))
  } catch {
    return null
  }
}

function formatSyncAge(iso) {
  const secs = syncAgeSeconds(iso)
  if (secs == null) return null
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  return `${Math.floor(secs / 3600)}h ago`
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

function formatInr(value) {
  return Number(value || 0).toLocaleString('en-IN', {
    maximumFractionDigits: 0,
  })
}

function truncateName(name, max = 18) {
  const s = String(name || '')
  if (s.length <= max) return s
  return `${s.slice(0, max - 1)}…`
}

function mtmClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'text-gray-500'
  return n > 0 ? 'text-green-400' : 'text-red-400'
}

function formatExecTs(ts) {
  if (ts == null || ts === '') return '—'
  try {
    const raw = typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts
    const d = new Date(raw)
    if (Number.isNaN(d.getTime())) return String(ts)
    return (
      d.toLocaleTimeString('en-IN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
        timeZone: 'Asia/Kolkata',
      }) + ' IST'
    )
  } catch {
    return String(ts)
  }
}

function execActionLabel(evt) {
  const status = String(evt?.status || '').toLowerCase()
  const kind = String(evt?.order_kind || '').toLowerCase()
  if (status === 'filled') return 'filled'
  if (status === 'timeout_market' || kind === 'market') return 'market fallback'
  return 'attempting'
}

function execPriceLabel(evt) {
  const px = evt?.fill_price ?? evt?.limit ?? evt?.mid
  if (px == null || !Number.isFinite(Number(px))) return '—'
  return `$${fmtMoney(px)}`
}

function LiveExecPanel({ execEvents }) {
  const [open, setOpen] = useState(false)
  if (!execEvents?.length) return null
  return (
    <section className="mb-4 overflow-hidden rounded-xl border border-cyan-800/50 bg-gray-900">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-gray-800/50"
      >
        <h2 className="text-sm font-semibold text-white">
          Live Execution
          <span className="ml-2 text-xs font-normal text-gray-400">
            ({execEvents.length})
          </span>
        </h2>
        <span className="text-xs text-gray-400">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="max-h-64 overflow-y-auto border-t border-gray-800 px-4 py-2">
          <ul className="space-y-1.5 font-mono text-[11px] text-gray-300">
            {execEvents.map((evt, i) => (
              <li
                key={`${evt.ts ?? ''}-${evt.leg ?? ''}-${evt.attempt ?? ''}-${i}`}
                className="flex flex-wrap gap-x-3 gap-y-0.5 border-b border-gray-800/80 pb-1.5 last:border-0"
              >
                <span className="text-gray-500">{formatExecTs(evt.ts)}</span>
                <span className="uppercase text-cyan-300">
                  {evt.leg || '—'}
                </span>
                <span className="text-amber-200">{execActionLabel(evt)}</span>
                <span className="text-gray-200">{execPriceLabel(evt)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function formatSignedMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}$${fmtMoney(n)}`
}

function ForceCloseSlaveAction({ slave, onComplete }) {
  const [step, setStep] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [lastResult, setLastResult] = useState(null)

  const hasActiveTrade = Boolean(slave.active_slave_trade)
  const structureClosed = Boolean(slave.structure_closed_at) && !hasActiveTrade
  const canForceClose =
    !structureClosed &&
    (hasActiveTrade ||
      slave.is_active ||
      slave.connection_status === 'error')

  const handleClick = async (e) => {
    e.stopPropagation()
    if (busy) return
    if (step === 0) {
      setError(null)
      setStep(1)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await closeSlaveStructure(slave.id, 'ADMIN_FORCE')
      setLastResult(result)
      setStep(0)
      if (onComplete) await onComplete()
    } catch (err) {
      setError(err.message || 'Force close failed')
      setStep(0)
    } finally {
      setBusy(false)
    }
  }

  const handleCancel = (e) => {
    e.stopPropagation()
    setStep(0)
    setError(null)
  }

  if (structureClosed) {
    return (
      <div className="mt-2 rounded border border-gray-700 bg-gray-800/60 px-2 py-1.5 text-[11px] text-gray-400">
        Structure closed
        {slave.structure_close_reason ? (
          <span className="text-gray-300"> · {slave.structure_close_reason}</span>
        ) : null}
        {slave.structure_closed_at ? (
          <span className="text-gray-500"> · {slave.structure_closed_at}</span>
        ) : null}
      </div>
    )
  }

  return (
    <div
      className="mt-2 space-y-1"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      {step === 0 ? (
        <button
          type="button"
          disabled={!canForceClose || busy}
          onClick={handleClick}
          className="rounded border border-red-700/60 bg-red-950/40 px-2 py-1 text-[11px] font-medium text-red-200 hover:bg-red-900/50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Force close structure
        </button>
      ) : (
        <div className="rounded border border-red-700/50 bg-red-950/30 px-2 py-2 text-[11px] text-red-100">
          <p className="mb-2">
            Close all baskets on <strong>{slave.name}</strong>, then the hedge?
            This cannot be undone.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={handleClick}
              className="rounded bg-red-700 px-2 py-1 font-medium text-white hover:bg-red-600 disabled:opacity-50"
            >
              {busy ? 'Closing…' : 'Yes, force close'}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={handleCancel}
              className="rounded border border-gray-600 px-2 py-1 text-gray-300 hover:bg-gray-800"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {error ? (
        <p className="text-[11px] text-red-400">{error}</p>
      ) : null}
      {lastResult?.already_closed ? (
        <p className="text-[11px] text-gray-500">Already flat — no positions to close.</p>
      ) : null}
    </div>
  )
}

function overviewRoleLabel(role) {
  switch (String(role || '').trim().toLowerCase()) {
    case 'short_call':
      return 'Short Call'
    case 'short_put':
      return 'Short Put'
    case 'wing_call':
      return 'Wing Call'
    case 'wing_put':
      return 'Wing Put'
    case 'hedge_call':
      return 'Hedge Call'
    case 'hedge_put':
      return 'Hedge Put'
    default:
      return String(role || '—').replace(/_/g, ' ')
  }
}

function overviewLegGroup(role) {
  const r = String(role || '').trim().toLowerCase()
  if (r.startsWith('short_')) return 'short'
  if (r.startsWith('wing_')) return 'protection'
  if (r.startsWith('hedge_')) return 'hedge'
  return 'other'
}

function overviewGroupMeta(id) {
  switch (id) {
    case 'short':
      return { title: 'SHORT LEGS', hint: 'premium collected' }
    case 'protection':
      return { title: 'PROTECTION', hint: 'wings — long, tail cover' }
    case 'hedge':
      return { title: 'HEDGE', hint: 'long straddle' }
    default:
      return { title: 'OTHER', hint: '' }
  }
}

function sumLegGross(legs) {
  let total = 0
  let any = false
  for (const leg of legs || []) {
    const n = Number(leg?.leg_pnl)
    if (Number.isFinite(n)) {
      total += n
      any = true
    }
  }
  return any ? total : null
}

function formatStaleUpdated(staleSeconds) {
  const n = Number(staleSeconds)
  if (!Number.isFinite(n) || n <= 15) return null
  const secs = Math.max(0, Math.round(n))
  return `updated ${secs}s ago`
}

/** Expand panel: 6 legs in 3 groups + Hedge/Basket/Structure strip from pnl. */
function StructureOverviewExpand({ trade, emptyLabel = 'No open structure' }) {
  if (!trade) {
    return <span className="text-gray-500">{emptyLabel}</span>
  }

  const legs = Array.isArray(trade.legs) ? trade.legs : []
  const pnl = trade.pnl || null
  const staleLabel = formatStaleUpdated(pnl?.stale_seconds)

  const ordered = ['short', 'protection', 'hedge', 'other']
  const groups = ordered
    .map((id) => {
      const rows = legs.filter((l) => overviewLegGroup(l.role) === id)
      return {
        id,
        meta: overviewGroupMeta(id),
        rows,
        gross: sumLegGross(rows),
      }
    })
    .filter((g) => g.rows.length > 0)

  if (groups.length === 0) {
    return <span className="text-gray-500">{emptyLabel}</span>
  }

  return (
    <div className="space-y-3 text-xs">
      {pnl ? (
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mono text-[11px]">
          <span className="text-gray-500">
            Hedge{' '}
            <span className={`tabular-nums font-medium ${mtmClass(pnl.hedge_net)}`}>
              {formatSignedMoney(pnl.hedge_net)}
            </span>
          </span>
          <span className="text-gray-500">
            Basket{' '}
            <span className={`tabular-nums font-medium ${mtmClass(pnl.basket_net)}`}>
              {formatSignedMoney(pnl.basket_net)}
            </span>
          </span>
          <span className="text-gray-500">
            Structure{' '}
            <span className={`tabular-nums font-semibold ${mtmClass(pnl.structure_net)}`}>
              {formatSignedMoney(pnl.structure_net)}
            </span>
          </span>
          {staleLabel ? (
            <span className="text-[10px] text-gray-500">{staleLabel}</span>
          ) : null}
        </div>
      ) : null}

      <div className="overflow-x-auto rounded border border-gray-700/80">
        <table className="min-w-full font-mono text-[11px]">
          <thead className="bg-gray-800/80 text-[10px] uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-2 py-1.5 text-left font-medium">Leg</th>
              <th className="px-2 py-1.5 text-right font-medium tabular-nums">Strike</th>
              <th className="px-2 py-1.5 text-right font-medium tabular-nums">Entry</th>
              <th className="px-2 py-1.5 text-right font-medium tabular-nums">Current</th>
              <th className="px-2 py-1.5 text-right font-medium tabular-nums">Qty</th>
              <th className="min-w-[5.5rem] px-2 py-1.5 text-right font-medium tabular-nums">
                P&amp;L (gross)
              </th>
            </tr>
          </thead>
          <tbody>
            {groups.flatMap((group) => {
              const header = (
                <tr
                  key={`g-${group.id}`}
                  className="border-t border-gray-700 bg-gray-800/50"
                >
                  <td colSpan={5} className="px-2 py-1.5">
                    <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-300">
                      {group.meta.title}
                    </span>
                    {group.meta.hint ? (
                      <span className="ml-2 text-[10px] font-normal normal-case tracking-normal text-gray-500">
                        ({group.meta.hint})
                      </span>
                    ) : null}
                    <span className="ml-2 text-[10px] font-normal normal-case text-gray-500">
                      gross (legs)
                    </span>
                  </td>
                  <td
                    className={`min-w-[5.5rem] px-2 py-1.5 text-right text-[11px] font-semibold tabular-nums ${mtmClass(group.gross)}`}
                  >
                    {formatSignedMoney(group.gross)}
                  </td>
                </tr>
              )
              const body = group.rows.map((leg, i) => {
                const closed = String(leg.status || '').toLowerCase() === 'closed'
                const isWing = overviewLegGroup(leg.role) === 'protection'
                const isHedge = overviewLegGroup(leg.role) === 'hedge'
                return (
                  <tr
                    key={`${group.id}-${leg.role}-${i}`}
                    className={`border-t border-gray-800/80 ${
                      isWing
                        ? 'bg-violet-950/20'
                        : isHedge
                          ? 'bg-sky-950/20'
                          : ''
                    } ${closed ? 'opacity-50' : ''}`}
                  >
                    <td
                      className={`px-2 py-1.5 text-left ${
                        closed ? 'text-gray-500 line-through' : 'text-gray-200'
                      }`}
                    >
                      {overviewRoleLabel(leg.role)}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-white">
                      {leg.strike != null && Number.isFinite(Number(leg.strike))
                        ? `$${fmtStrike(leg.strike)}`
                        : '—'}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-gray-300">
                      {leg.entry_price != null
                        ? `$${fmtMoney(leg.entry_price)}`
                        : '—'}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-gray-300">
                      {leg.current_price != null
                        ? `$${fmtMoney(leg.current_price)}`
                        : '—'}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-gray-300">
                      {leg.quantity != null ? leg.quantity : '—'}
                    </td>
                    <td
                      className={`min-w-[5.5rem] px-2 py-1.5 text-right font-medium tabular-nums ${mtmClass(leg.leg_pnl)}`}
                    >
                      {formatSignedMoney(leg.leg_pnl)}
                    </td>
                  </tr>
                )
              })
              return [header, ...body]
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function growthClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return 'text-gray-500'
  if (n > 0) return 'text-green-400'
  if (n < 0) return 'text-red-400'
  return 'text-gray-300'
}

function fmtPct1(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(1)}%`
}

function BalanceCell({ usd, inr, className = 'text-gray-200' }) {
  if (usd == null || !Number.isFinite(Number(usd))) {
    return <span className="text-gray-500">—</span>
  }
  return (
    <div className={`text-right ${className}`}>
      <div>${formatBalance(usd)}</div>
      {inr != null ? (
        <div className="text-[10px] text-gray-500">₹{formatInr(inr)}</div>
      ) : null}
    </div>
  )
}

function HeaderCell({ children, tooltip, align = 'right' }) {
  const alignClass = align === 'right' ? 'text-right' : 'text-left'
  const flexClass = align === 'right' ? 'justify-end' : ''
  return (
    <th className={`px-2 py-2 ${alignClass}`}>
      <span className={`inline-flex items-center gap-0.5 ${flexClass}`}>
        {children}
        {tooltip ? <InfoTooltip text={tooltip} /> : null}
      </span>
    </th>
  )
}

function AccountOverviewRow({
  role,
  name,
  multiplier,
  statusKind,
  statusText,
  actualBalance,
  actualBalanceInr,
  blockedAmount,
  blockedAmountInr,
  freeCash,
  freeCashInr,
  dailyGrowthPct,
  netMtm,
  mtmLabel,
  mtmSource,
  staleSeconds,
  targetUsd,
  isExpanded,
  onToggle,
  borderClass,
  dimmed,
  expandContent,
}) {
  const staleLabel = formatStaleUpdated(staleSeconds)
  const blockedHigh =
    actualBalance != null &&
    blockedAmount != null &&
    Number(actualBalance) > 0 &&
    Number(blockedAmount) / Number(actualBalance) > 0.5

  return (
    <>
      <tr
        onClick={onToggle}
        className={`cursor-pointer border-b border-gray-800 hover:bg-gray-800/80 ${
          dimmed ? 'opacity-60' : ''
        } ${isExpanded ? 'bg-gray-800/50' : ''}`}
      >
        <td className={`px-2 py-2.5 text-xs ${borderClass}`}>
          {role === 'master' ? (
            <span className="font-semibold text-amber-200">⭐ Master</span>
          ) : (
            <span className="font-medium text-blue-300">
              📋 Slave
              {multiplier != null ? (
                <span className="ml-1 text-[10px] text-gray-400">
                  ({Number(multiplier)}×)
                </span>
              ) : null}
            </span>
          )}
        </td>
        <td className="px-2 py-2.5 text-xs text-white" title={name}>
          {truncateName(name, 14)}
        </td>
        <td className="px-2 py-2.5 text-xs">
          {statusKind === 'live' && (
            <span className="text-green-400">🟢 Live</span>
          )}
          {statusKind === 'ready' && (
            <span className="text-yellow-300">🟡 Ready</span>
          )}
          {statusKind === 'error' && (
            <span className="text-red-400" title={statusText}>
              🔴 {truncateName(statusText || 'Error', 12)}
            </span>
          )}
          {statusKind === 'paused' && (
            <span className="text-gray-400">⚪ Paused</span>
          )}
          {statusKind === 'offline' && (
            <span className="text-gray-500">⚪ Offline</span>
          )}
        </td>
        <td className="px-2 py-2.5 text-xs">
          <BalanceCell usd={actualBalance} inr={actualBalanceInr} />
        </td>
        <td className="px-2 py-2.5 text-xs">
          <BalanceCell
            usd={blockedAmount}
            inr={blockedAmountInr}
            className={blockedHigh ? 'text-red-300' : 'text-gray-300'}
          />
        </td>
        <td className="px-2 py-2.5 text-xs">
          <BalanceCell usd={freeCash} inr={freeCashInr} />
        </td>
        <td
          className={`px-2 py-2.5 text-right text-xs font-medium ${growthClass(dailyGrowthPct)}`}
          title="vs yesterday 12pm IST"
        >
          {dailyGrowthPct != null && Number.isFinite(Number(dailyGrowthPct)) ? (
            fmtPct1(dailyGrowthPct)
          ) : (
            <span className="font-normal text-gray-500">No snapshot</span>
          )}
        </td>
        <td className={`px-2 py-2.5 text-right text-xs font-medium ${mtmClass(netMtm)}`}>
          {netMtm == null || !Number.isFinite(Number(netMtm)) ? (
            '—'
          ) : (
            <>
              <div className="tabular-nums">{formatSignedMoney(netMtm)}</div>
              {mtmLabel ? (
                <div className="text-[10px] font-normal text-gray-500">{mtmLabel}</div>
              ) : null}
              {mtmSource === 'copied' ? (
                <div className="text-[10px] font-normal text-yellow-300">copied</div>
              ) : null}
              {staleLabel ? (
                <div className="text-[10px] font-normal text-gray-500">{staleLabel}</div>
              ) : null}
            </>
          )}
        </td>
        <td className="px-2 py-2.5 text-right text-xs text-gray-300">
          {targetUsd != null && Number.isFinite(Number(targetUsd))
            ? `$${fmtMoney(targetUsd)}`
            : '—'}
        </td>
      </tr>
      {isExpanded && (
        <tr className="border-b border-gray-800 bg-gray-800/40">
          <td colSpan={9} className="px-4 py-3 text-xs text-gray-300">
            {expandContent}
          </td>
        </tr>
      )}
    </>
  )
}

function MultiAccountOverview({ overview, onRefresh, activeHedge }) {
  const [expanded, setExpanded] = useState({})
  const [sectionOpen, setSectionOpen] = useState(false)

  if (!overview?.has_slaves) return null

  const master = overview.master || {}
  const slaves = overview.slaves || []
  const masterTrade = master.active_trade

  const masterStructureMtm = (() => {
    const fromPnl = masterTrade?.pnl?.structure_net
    if (fromPnl != null && Number.isFinite(Number(fromPnl))) return Number(fromPnl)
    if (
      master.structure_net_mtm != null &&
      Number.isFinite(Number(master.structure_net_mtm))
    ) {
      return Number(master.structure_net_mtm)
    }
    if (
      activeHedge?.structure_pnl != null &&
      Number.isFinite(Number(activeHedge.structure_pnl))
    ) {
      return Number(activeHedge.structure_pnl)
    }
    return null
  })()

  let combined = Number(
    overview.combined_structure_mtm ?? overview.combined_mtm ?? NaN,
  )
  if (!Number.isFinite(combined)) {
    combined = 0
    let any = false
    if (masterStructureMtm != null) {
      combined += masterStructureMtm
      any = true
    }
    for (const s of slaves) {
      const sn = s.active_slave_trade?.pnl?.structure_net
      if (sn != null && Number.isFinite(Number(sn))) {
        combined += Number(sn)
        any = true
      }
    }
    if (!any) combined = 0
  }

  const toggle = (key) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  let masterStatus = 'offline'
  let masterStatusText = ''
  if (!master.connected) {
    masterStatus = 'offline'
  } else if (masterTrade) {
    masterStatus = 'live'
  } else {
    masterStatus = 'ready'
  }

  const masterExpand = (
    <StructureOverviewExpand
      trade={masterTrade}
      emptyLabel="No open structure"
    />
  )

  return (
    <section className="mb-8 overflow-hidden rounded-xl border border-gray-700 bg-gray-900">
      <button
        type="button"
        onClick={() => setSectionOpen((v) => !v)}
        className="flex w-full items-center justify-between border-b border-gray-700 px-4 py-3 text-left hover:bg-gray-800/50"
      >
        <h2 className="text-sm font-semibold text-white">
          📊 Multi-Account Overview
        </h2>
        <span className="text-xs text-gray-400">{sectionOpen ? '▼' : '▶'}</span>
      </button>
      {sectionOpen && (
      <>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left">
          <thead className="bg-gray-800 text-[10px] uppercase tracking-wide text-gray-400">
            <tr>
              <th className="px-2 py-2 text-left">Role</th>
              <th className="px-2 py-2 text-left">Account</th>
              <th className="px-2 py-2 text-left">Status</th>
              <HeaderCell
                tooltip="Settled cash in the wallet. Matches Wallet Balance on Delta."
              >
                Actual Bal
              </HeaderCell>
              <HeaderCell
                tooltip="Margin held against open positions and orders. Matches Blocked Amount on Delta."
              >
                Blocked
              </HeaderCell>
              <HeaderCell
                tooltip="Settled cash the bot can size new trades against. Excludes unrealised P&L, so it is lower than Available Margin — this is deliberate."
              >
                Free Cash
              </HeaderCell>
              <HeaderCell tooltip="vs yesterday 12pm IST">Daily Δ%</HeaderCell>
              <HeaderCell tooltip="Hedge + closed baskets + open basket (structure_net from overview)">
                Structure MTM
              </HeaderCell>
              <HeaderCell>Target</HeaderCell>
            </tr>
          </thead>
          <tbody>
            <AccountOverviewRow
              role="master"
              name={master.name || 'Master'}
              statusKind={masterStatus}
              statusText={masterStatusText}
              actualBalance={master.actual_balance ?? master.balance_usd}
              actualBalanceInr={master.actual_balance_inr ?? master.balance_inr}
              blockedAmount={master.blocked_amount ?? master.blocked_usd}
              blockedAmountInr={master.blocked_amount_inr ?? master.blocked_inr}
              freeCash={master.free_cash ?? master.available_balance ?? master.available_usd}
              freeCashInr={
                master.free_cash_inr ??
                master.available_balance_inr ??
                master.available_inr
              }
              dailyGrowthPct={master.daily_growth_pct}
              netMtm={masterTrade ? masterStructureMtm : null}
              mtmLabel="Structure MTM"
              staleSeconds={masterTrade?.pnl?.stale_seconds ?? masterTrade?.stale_seconds}
              targetUsd={master.target ?? masterTrade?.profit_target_usd ?? null}
              isExpanded={Boolean(expanded.master)}
              onToggle={() => toggle('master')}
              borderClass="border-l-2 border-l-amber-500"
              dimmed={false}
              expandContent={masterExpand}
            />

            {slaves.map((slave) => {
              const st = slave.active_slave_trade
              let statusKind = 'ready'
              let statusText = ''
              if (!slave.is_active) {
                statusKind = 'paused'
              } else if (slave.connection_status === 'error') {
                statusKind = 'error'
                statusText = slave.last_error || 'Error'
              } else if (st) {
                statusKind = 'live'
              }

              const key = `slave-${slave.id}`
              const slaveStructureMtm =
                st?.pnl?.structure_net != null &&
                Number.isFinite(Number(st.pnl.structure_net))
                  ? Number(st.pnl.structure_net)
                  : null

              const expandWithActions = (
                <div className="space-y-0">
                  <StructureOverviewExpand
                    trade={st}
                    emptyLabel={
                      slave.is_active
                        ? 'No open structure'
                        : 'Account paused — not mirroring'
                    }
                  />
                  <ForceCloseSlaveAction
                    slave={slave}
                    onComplete={onRefresh}
                  />
                </div>
              )

              return (
                <AccountOverviewRow
                  key={key}
                  role="slave"
                  name={slave.name}
                  multiplier={slave.qty_multiplier}
                  statusKind={statusKind}
                  statusText={statusText}
                  actualBalance={
                    statusKind === 'error' || !slave.is_active
                      ? null
                      : slave.actual_balance ?? slave.balance_usd
                  }
                  actualBalanceInr={
                    statusKind === 'error' || !slave.is_active
                      ? null
                      : slave.actual_balance_inr ?? slave.balance_inr
                  }
                  blockedAmount={
                    statusKind === 'error' || !slave.is_active
                      ? null
                      : slave.blocked_amount ?? slave.blocked_usd
                  }
                  blockedAmountInr={
                    statusKind === 'error' || !slave.is_active
                      ? null
                      : slave.blocked_amount_inr ?? slave.blocked_inr
                  }
                  freeCash={
                    statusKind === 'error' || !slave.is_active
                      ? null
                      : slave.free_cash ??
                        slave.available_balance ??
                        slave.available_usd
                  }
                  freeCashInr={
                    statusKind === 'error' || !slave.is_active
                      ? null
                      : slave.free_cash_inr ??
                        slave.available_balance_inr ??
                        slave.available_inr
                  }
                  dailyGrowthPct={slave.daily_growth_pct}
                  netMtm={st ? slaveStructureMtm : null}
                  mtmLabel="Structure MTM"
                  mtmSource={st?.mtm_source ?? null}
                  staleSeconds={st?.pnl?.stale_seconds ?? st?.stale_seconds}
                  targetUsd={st?.profit_target_usd ?? null}
                  isExpanded={Boolean(expanded[key])}
                  onToggle={() => toggle(key)}
                  borderClass={
                    statusKind === 'error'
                      ? 'border-l-2 border-l-red-500'
                      : 'border-l-2 border-l-blue-500'
                  }
                  dimmed={!slave.is_active || statusKind === 'ready'}
                  expandContent={expandWithActions}
                />
              )
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-gray-600 bg-gray-800/60">
              <td
                colSpan={7}
                className="px-2 py-2.5 text-right text-xs font-semibold text-gray-300"
              >
                Combined Structure MTM:
              </td>
              <td
                colSpan={2}
                className={`px-2 py-2.5 text-right text-sm font-bold tabular-nums ${mtmClass(combined)}`}
              >
                {formatSignedMoney(combined)}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
      <div className="border-t border-gray-800 px-4 py-2 text-right text-[11px] text-gray-500">
        <Link to="/accounts" className="text-blue-400 hover:underline">
          Manage accounts →
        </Link>
      </div>
      </>
      )}
    </section>
  )
}

function AutoTradeBanner({ status, activeTrade, onEnterNow }) {
  const [secondsLeft, setSecondsLeft] = useState(null)
  const [entering, setEntering] = useState(false)

  useEffect(() => {
    if (!status?.is_enabled) {
      setSecondsLeft(null)
      return
    }
    const secs = status.seconds_until_entry
    if (secs != null && Number.isFinite(Number(secs))) {
      setSecondsLeft(Math.max(0, Number(secs)))
    } else {
      setSecondsLeft(null)
    }
  }, [status?.is_enabled, status?.seconds_until_entry, status?.next_entry_time, status?.next_entry_source])

  const countdownActive = secondsLeft != null && secondsLeft > 0
  useEffect(() => {
    if (!countdownActive) return undefined
    const id = setInterval(() => {
      setSecondsLeft((prev) => {
        if (prev == null || prev <= 0) return prev
        return prev - 1
      })
    }, 1000)
    return () => clearInterval(id)
  }, [countdownActive])

  if (!status?.is_enabled) return null

  const tradeLabel =
    activeTrade?.basket_number ??
    activeTrade?.trade_id ??
    status.last_trade_id
  const delayMin = Number(status.re_entry_delay_minutes ?? 1)
  const hasActive = Boolean(activeTrade)
  const hasError = Boolean(status.last_error)
  const tradeTypeLabel =
    status.trade_type === 'strangle'
      ? `Strangle $${status.target_premium_per_side ?? 150}/side`
      : 'Straddle (ATM)'
  const nextEntryLabel = formatNextEntryWait(
    secondsLeft,
    status.next_entry_source,
    status.next_entry_reason,
  )

  const handleEnterNow = async () => {
    setEntering(true)
    try {
      await onEnterNow?.()
    } finally {
      setEntering(false)
    }
  }

  if (hasActive) {
    return (
      <div className="mb-4 rounded-xl border border-green-700/60 bg-green-950/40 px-4 py-3 text-sm text-green-100">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="font-medium">
            🔄 Auto Trade ON · {tradeTypeLabel} · Monitoring trade #
            {tradeLabel != null ? tradeLabel : '—'}
          </div>
          <Link
            to="/auto-trade"
            className="text-xs font-medium text-green-300 underline hover:text-green-200"
          >
            Settings →
          </Link>
        </div>
        <div className="mt-1 text-green-200/80">
          Will re-enter in {delayMin} min after exit
        </div>
      </div>
    )
  }

  if (hasError) {
    return (
      <div className="mb-4 rounded-xl border border-red-700/60 bg-red-950/40 px-4 py-3 text-sm text-red-100">
        <div className="font-medium">
          ⚠️ Auto Trade ON · {tradeTypeLabel} ·{' '}
          {secondsLeft != null && secondsLeft > 0
            ? formatNextEntryWait(
                secondsLeft,
                status.next_entry_source || 'retry',
                status.next_entry_reason,
              )
            : 'Retrying…'}
        </div>
        <div className="mt-1 text-red-200/90">
          Last error: &quot;{status.last_error}&quot;
        </div>
        <div className="mt-2">
          <Link
            to="/auto-trade"
            className="text-xs font-medium text-red-300 underline hover:text-red-200"
          >
            Settings →
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="mb-4 rounded-xl border border-amber-600/60 bg-amber-950/40 px-4 py-3 text-sm text-amber-100">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-medium">
          🔄 Auto Trade ON · {tradeTypeLabel} ·{' '}
          {secondsLeft != null && secondsLeft > 0
            ? `${nextEntryLabel} ⏱`
            : 'Ready to enter…'}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Link
            to="/auto-trade"
            className="text-xs font-medium text-amber-300 underline hover:text-amber-200"
          >
            Settings →
          </Link>
          <button
            type="button"
            disabled={entering}
            onClick={handleEnterNow}
            className="rounded-md bg-amber-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-amber-500 disabled:opacity-50"
          >
            {entering ? 'Entering…' : 'Enter Now →'}
          </button>
        </div>
      </div>
      <div className="mt-1 text-amber-200/80">
        {status.underlying || 'BTC'} {Number(status.expiry_dte ?? 1)}DTE · Qty{' '}
        {status.quantity ?? 1}
      </div>
    </div>
  )
}

function formatSignedMoney2(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

function formatDeduction2(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return '−$0.00'
  return `−$${Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

function BasketRealizedBreakdown({ basket }) {
  const gross = basket.gross_realized
  const entryFees = basket.entry_fees_usd
  const exitFees = basket.exit_fees_usd
  const entrySpread = basket.entry_spread_usd
  const net = basket.net_realized
  const unresolved = Number(basket.legs_unresolved || 0)
  const hasBreakdown =
    gross != null ||
    entryFees != null ||
    exitFees != null ||
    entrySpread != null ||
    net != null

  if (!hasBreakdown) return null

  const rows = [
    { label: 'Gross realized', value: formatSignedMoney2(gross), className: pnlColor(gross) },
    { label: 'Entry fees', value: formatDeduction2(entryFees), className: 'text-yellow-400/90' },
    { label: 'Exit fees', value: formatDeduction2(exitFees), className: 'text-yellow-400/90' },
    { label: 'Entry spread', value: formatDeduction2(entrySpread), className: 'text-yellow-400/90' },
  ]

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900/50 px-3 py-2 text-xs">
      {rows.map(({ label, value, className }) => (
        <div
          key={label}
          className="flex items-baseline justify-between gap-2 py-0.5"
        >
          <span className="text-gray-500">{label}</span>
          <span className={className}>{value}</span>
        </div>
      ))}
      <div className="my-1.5 border-t border-gray-700" />
      <div className="flex items-baseline justify-between gap-2 font-semibold">
        <span className="text-gray-300">Net</span>
        <span className={pnlColor(net)}>{formatSignedMoney2(net)}</span>
      </div>
      {unresolved > 0 && (
        <p className="mt-2 text-[11px] text-amber-400">
          ⚠ {unresolved} leg(s) unresolved — P&amp;L incomplete
        </p>
      )}
    </div>
  )
}

function StructureBasketRow({ basket, ledgerLegs = [] }) {
  const [open, setOpen] = useState(false)
  const seq = basket.basket_seq_in_structure ?? '—'
  const basketLegs = ledgerLegs.filter(
    (leg) =>
      Number(leg.basket_seq) === Number(basket.basket_seq_in_structure) &&
      String(leg.leg_role || '').startsWith('BASKET_'),
  )
  const pnl =
    basket.net_mtm != null
      ? Number(basket.net_mtm)
      : Number(basket.realized_pnl || 0)
  const status = String(basket.status || '').toLowerCase()
  const isActive = status === 'active'

  return (
    <div className="rounded-lg border border-gray-700/80 bg-gray-900/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center justify-between gap-2 px-3 py-2 text-left text-sm"
      >
        <div className="font-medium text-white">
          Basket {seq}
          <span
            className={`ml-2 rounded px-2 py-0.5 text-xs ${
              isActive
                ? 'bg-green-900/50 text-green-300'
                : 'bg-gray-700 text-gray-300'
            }`}
          >
            {status}
          </span>
          <span className="ml-2 text-[10px] font-normal text-gray-500">
            trade #{basket.trade_id}
          </span>
        </div>
        <div className="text-right text-xs text-gray-400">
          C {fmtStrike(basket.call_strike)} / P {fmtStrike(basket.put_strike)}
          <span className={`ml-2 ${pnlColor(pnl)}`}>
            {formatSignedMoney(pnl)}
          </span>
          <span className="ml-2 text-gray-500">{open ? '▲' : '▼'}</span>
        </div>
      </button>
      {open && (
        <div className="space-y-3 border-t border-gray-700 px-3 py-3">
          <div className="text-xs text-gray-400">
            Entry {formatAdjTime(basket.entry_time)}
            {basket.exit_time ? ` · Exit ${formatAdjTime(basket.exit_time)}` : ''}
            {basket.exit_reason ? ` · ${basket.exit_reason}` : ''}
            {basket.call_entry_premium != null || basket.put_entry_premium != null
              ? ` · Entry prem C $${fmtMoney(basket.call_entry_premium)} / P $${fmtMoney(basket.put_entry_premium)}`
              : ''}
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
                    <td className="px-2 py-1">{leg.status}</td>
                    <td className={`px-2 py-1 ${pnlColor(leg.realized_pnl)}`}>
                      {leg.realized_pnl != null
                        ? formatSignedMoney(leg.realized_pnl)
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <BasketRealizedBreakdown basket={basket} />
          {(basket.adjustments || []).length > 0 && (
            <div>
              <div className="mb-1 text-xs font-semibold uppercase text-gray-500">
                Adjustments
              </div>
              <ul className="space-y-1 text-xs text-gray-400">
                {basket.adjustments.map((a, i) => (
                  <li key={`${a.timestamp}-${i}`}>
                    {formatAdjTime(a.timestamp)} ·{' '}
                    {String(a.leg || a.leg_type || '').toUpperCase()} $
                    {fmtStrike(a.old_strike)} → ${fmtStrike(a.new_strike)} · exit $
                    {fmtMoney(a.old_premium ?? a.old_exit_premium)} · entry $
                    {fmtMoney(a.new_premium ?? a.new_entry_premium)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <AllotmentBlock legs={basketLegs} />
        </div>
      )}
    </div>
  )
}

function StructureHistoryCard({ structure, ledger }) {
  const [open, setOpen] = useState(false)
  const hedge = structure.hedge || {}
  const structurePnl = Number(structure.structure_pnl || 0)
  const status = String(hedge.status || '').toLowerCase()
  const isActive = status === 'active'
  const basketCount = Number(structure.basket_count ?? (structure.baskets || []).length)
  const ledgerLegs = Array.isArray(ledger?.legs) ? ledger.legs : []
  const ledgerLegCount = ledgerLegs.length
  const hedgeLegs = ledgerLegs.filter((leg) =>
    String(leg.leg_role || '').startsWith('HEDGE_'),
  )

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/80">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center justify-between gap-2 px-4 py-3 text-left text-sm"
      >
        <div className="font-medium text-white">
          Structure #{hedge.id}
          <span
            className={`ml-2 rounded px-2 py-0.5 text-xs ${
              isActive
                ? 'bg-emerald-900/50 text-emerald-300'
                : 'bg-gray-700 text-gray-300'
            }`}
          >
            {status}
          </span>
          <span className="ml-2 text-xs font-normal text-gray-400">
            {fmtStrike(hedge.strike)} · {hedge.expiry || '—'} · {basketCount}{' '}
            basket{basketCount === 1 ? '' : 's'}
          </span>
          <span
            className={`ml-2 rounded px-1.5 py-0.5 text-[10px] font-normal ${
              ledgerLegCount > 0
                ? 'bg-gray-700/80 text-gray-300'
                : 'bg-amber-950/60 text-amber-400'
            }`}
            title="Bot-recorded structure ledger legs"
          >
            Ledger: {ledgerLegCount} leg{ledgerLegCount === 1 ? '' : 's'}
          </span>
        </div>
        <div className="text-right">
          <div className={`font-semibold ${pnlColor(structurePnl)}`}>
            {formatSignedMoney(structurePnl)}
          </div>
          <div className="text-[10px] uppercase text-gray-500">
            STRUCTURE P&amp;L {open ? '▲' : '▼'}
          </div>
        </div>
      </button>
      {open && (
        <div className="space-y-3 border-t border-gray-700 px-4 py-3">
          <div className="grid gap-2 rounded-lg border border-gray-700 bg-gray-900/40 px-3 py-2 text-xs text-gray-300 sm:grid-cols-2">
            <div>
              Hedge net MTM:{' '}
              <span className={pnlColor(hedge.hedge_net_mtm)}>
                {formatSignedMoney(hedge.hedge_net_mtm)}
              </span>
              {(() => {
                const src = String(hedge.hedge_net_source || '').toLowerCase()
                if (src === 'reconstructed') {
                  return (
                    <span className="ml-2 text-[10px] font-normal text-gray-500">
                      reconstructed from fills - exit spread not captured
                    </span>
                  )
                }
                if (src === 'realized') {
                  return (
                    <span className="ml-2 text-[10px] font-normal text-gray-500">
                      from actual fills
                    </span>
                  )
                }
                return null
              })()}
            </div>
            <div>
              Entry cost:{' '}
              {hedge.entry_cost != null ? `$${fmtMoney(hedge.entry_cost)}` : '—'}
            </div>
            <div>
              Open basket MTM:{' '}
              <span className={pnlColor(structure.open_basket_net_mtm)}>
                {formatSignedMoney(structure.open_basket_net_mtm)}
              </span>
            </div>
            <div>
              Cum closed baskets:{' '}
              <span className={pnlColor(structure.cum_closed_basket_pnl)}>
                {formatSignedMoney(structure.cum_closed_basket_pnl)}
              </span>
            </div>
            <div className="sm:col-span-2 text-gray-500">
              Entry {formatAdjTime(hedge.entry_time)}
              {hedge.exit_time ? ` · Exit ${formatAdjTime(hedge.exit_time)}` : ''}
              {hedge.exit_reason ? ` · ${hedge.exit_reason}` : ''}
            </div>
            {(hedge.call_symbol || hedge.put_symbol) && (
              <div className="sm:col-span-2 text-gray-400">
                Legs: {hedge.call_symbol || '—'} @ $
                {fmtMoney(hedge.call_fill_price)} · {hedge.put_symbol || '—'} @ $
                {fmtMoney(hedge.put_fill_price)}
              </div>
            )}
            <AllotmentBlock legs={hedgeLegs} />
          </div>
          {(structure.baskets || []).length === 0 ? (
            <p className="text-xs text-gray-500">No baskets under this structure</p>
          ) : (
            <div className="space-y-2">
              {(structure.baskets || []).map((b) => (
                <StructureBasketRow
                  key={b.trade_id}
                  basket={b}
                  ledgerLegs={ledgerLegs}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function Dashboard() {
  const {
    trades,
    activeHedge,
    wsStatus,
    loading,
    errors,
    adjustments,
    autoTradeStatus,
    refetch,
    execEvents,
    qtyMismatches,
  } = useTrades()
  const [updatedAt, setUpdatedAt] = useState(() => formatIstTime())
  const [backendOnline, setBackendOnline] = useState(true)
  const [accountName, setAccountName] = useState('')
  const [balance, setBalance] = useState(0)
  const [accountConnected, setAccountConnected] = useState(false)
  const [structures, setStructures] = useState([])
  const [ledgerByHedgeId, setLedgerByHedgeId] = useState({})
  const [autoStatus, setAutoStatus] = useState(null)
  const [slaveOverview, setSlaveOverview] = useState(null)
  const [basketStoryOpen, setBasketStoryOpen] = useState(false)
  const [payoffOpen, setPayoffOpen] = useState(false)
  const [structurePage, setStructurePage] = useState(0)

  useEffect(() => {
    document.title = 'Delta Bot — Dashboard'
  }, [])

  const refreshSlaveOverview = useCallback(async () => {
    try {
      const data = await getSlaveOverview()
      setSlaveOverview(data)
    } catch {
      // keep last known — section hidden if never loaded / no slaves
    }
  }, [])

  useEffect(() => {
    refreshSlaveOverview()
    const id = setInterval(refreshSlaveOverview, SLAVE_OVERVIEW_POLL_MS)
    return () => clearInterval(id)
  }, [refreshSlaveOverview])

  // Refresh overview when live trades change (new entry / exit)
  useEffect(() => {
    if (trades?.length != null) {
      refreshSlaveOverview()
    }
  }, [trades?.length, refreshSlaveOverview])

  const refreshAutoStatus = useCallback(async () => {
    try {
      const data = await getAutoTradeStatus()
      setAutoStatus(data)
    } catch {
      // keep last known
    }
  }, [])

  useEffect(() => {
    refreshAutoStatus()
    const id = setInterval(refreshAutoStatus, AUTO_STATUS_POLL_MS)
    return () => clearInterval(id)
  }, [refreshAutoStatus])

  // Sync countdown from WS AUTO_TRADE_WAITING without waiting for poll
  useEffect(() => {
    if (!autoTradeStatus) return
    if (autoTradeStatus.type === 'AUTO_TRADE_WAITING') {
      setAutoStatus((prev) =>
        prev
          ? {
              ...prev,
              is_enabled: true,
              seconds_until_entry: autoTradeStatus.seconds_remaining,
              next_entry_time: autoTradeStatus.next_entry_time,
              next_entry_source:
                autoTradeStatus.next_entry_source ?? prev.next_entry_source,
              last_error: null,
            }
          : prev,
      )
    } else if (autoTradeStatus.type === 'AUTO_TRADE_PLACED') {
      refreshAutoStatus()
    } else if (autoTradeStatus.type === 'AUTO_TRADE_FAILED') {
      setAutoStatus((prev) =>
        prev
          ? {
              ...prev,
              is_enabled: true,
              last_error: autoTradeStatus.error || prev.last_error,
              seconds_until_entry: autoTradeStatus.retry_in_seconds ?? 60,
              next_entry_source: 'retry',
            }
          : prev,
      )
    }
  }, [autoTradeStatus, refreshAutoStatus])

  const handleEnterNow = useCallback(async () => {
    await enableAutoTrade()
    await refreshAutoStatus()
  }, [refreshAutoStatus])

  const bannerActiveTrade = useMemo(() => {
    if (!autoStatus?.is_enabled || !trades?.length) return null
    const und = String(autoStatus.underlying || '').toUpperCase()
    return (
      trades.find((t) => String(t.underlying || '').toUpperCase() === und) ||
      trades[0] ||
      null
    )
  }, [autoStatus, trades])

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

  // Structure history (hedge + linked baskets) + ledger allotments
  useEffect(() => {
    let cancelled = false
    async function loadStructures() {
      try {
        const [histData, ledgerData] = await Promise.all([
          getHedgeStructures(200),
          getStructureLedger({ account_kind: 'MASTER', limit: 200 }),
        ])
        if (cancelled) return
        setStructures(Array.isArray(histData?.structures) ? histData.structures : [])
        const map = {}
        for (const row of ledgerData?.structures || []) {
          const hid = Number(row.hedge_position_id)
          if (!Number.isFinite(hid)) continue
          if (!map[hid] || Number(row.id) > Number(map[hid].id)) {
            map[hid] = row
          }
        }
        setLedgerByHedgeId(map)
      } catch {
        if (!cancelled) {
          setStructures([])
          setLedgerByHedgeId({})
        }
      }
    }
    loadStructures()
    const id = setInterval(loadStructures, 10000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [trades, adjustments, activeHedge])

  const topStructureCards = useMemo(() => {
    if (!activeHedge) return null

    const hedgeNetPnl =
      activeHedge.hedge_net_mtm != null &&
      Number.isFinite(Number(activeHedge.hedge_net_mtm))
        ? Number(activeHedge.hedge_net_mtm)
        : Number(activeHedge.net_pnl) || 0

    const closedBasketPnl = Number(activeHedge.cum_closed_basket_pnl ?? 0) || 0

    // Same live net_mtm source as the basket PositionCard (WS /poll trades).
    // Structures payload open_basket_net_mtm is only a fallback.
    const liveTrades = Array.isArray(trades) ? trades : []
    let openBasketNetPnl
    let openBasketStaleSeconds = null
    if (liveTrades.length > 0) {
      openBasketNetPnl = liveTrades.reduce((sum, t) => {
        const n = t?.net_mtm != null ? Number(t.net_mtm) : 0
        return sum + (Number.isFinite(n) ? n : 0)
      }, 0)
      const stales = liveTrades
        .map((t) =>
          t?.stale_seconds != null ? Number(t.stale_seconds) : null,
        )
        .filter((n) => n != null && Number.isFinite(n))
      if (stales.length > 0) {
        openBasketStaleSeconds = Math.max(...stales)
      }
    } else {
      openBasketNetPnl = Number(activeHedge.open_basket_net_mtm ?? 0) || 0
      if (
        activeHedge.open_basket_stale_seconds != null &&
        Number.isFinite(Number(activeHedge.open_basket_stale_seconds))
      ) {
        openBasketStaleSeconds = Number(activeHedge.open_basket_stale_seconds)
      }
    }

    const structurePnl = hedgeNetPnl + closedBasketPnl + openBasketNetPnl

    return {
      hedgeNetPnl,
      closedBasketPnl,
      openBasketNetPnl,
      structurePnl,
      openBasketStaleSeconds,
    }
  }, [activeHedge, trades])

  const wsLabel = useMemo(() => {
    if (wsStatus === 'connected') return { text: 'connected', className: 'text-green-400' }
    if (wsStatus === 'connecting') {
      return { text: 'reconnecting...', className: 'text-yellow-400' }
    }
    return { text: 'disconnected', className: 'text-yellow-400' }
  }, [wsStatus])

  const structurePageCount = Math.max(
    1,
    Math.ceil(structures.length / STRUCTURES_PER_PAGE),
  )

  useEffect(() => {
    if (structurePage > 0 && structurePage >= structurePageCount) {
      setStructurePage(Math.max(0, structurePageCount - 1))
    }
  }, [structurePage, structurePageCount])

  const paginatedStructures = useMemo(() => {
    const start = structurePage * STRUCTURES_PER_PAGE
    return structures.slice(start, start + STRUCTURES_PER_PAGE)
  }, [structures, structurePage])

  const structureRangeStart =
    structures.length === 0 ? 0 : structurePage * STRUCTURES_PER_PAGE + 1
  const structureRangeEnd = Math.min(
    (structurePage + 1) * STRUCTURES_PER_PAGE,
    structures.length,
  )

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

      <AutoTradeBanner
        status={autoStatus}
        activeTrade={bannerActiveTrade}
        onEnterNow={handleEnterNow}
      />

      <LiveExecPanel execEvents={execEvents} />

      {!loading && topStructureCards && (
        <StructurePnlBar
          hedgeNetPnl={topStructureCards.hedgeNetPnl}
          closedBasketPnl={topStructureCards.closedBasketPnl}
          openBasketNetPnl={topStructureCards.openBasketNetPnl}
          structurePnl={topStructureCards.structurePnl}
          openBasketStaleSeconds={topStructureCards.openBasketStaleSeconds}
        />
      )}

      {loading ? (
        <div className="space-y-4">
          <SkeletonCard />
          <SkeletonCard />
        </div>
      ) : activeHedge || trades.length > 0 ? (
        <>
          <div className="mb-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
            {activeHedge ? (
              <HedgePanel
                hedge={activeHedge}
                onClosed={() => refetch()}
                onUpdated={() => refetch()}
              />
            ) : (
              <div className="hidden lg:block" />
            )}
            {trades.length > 0 ? (
              <PositionCard
                trade={trades[0]}
                recentAdjustments={adjustments}
                qtyMismatches={qtyMismatches}
                compact
              />
            ) : (
              <div className="rounded-xl border border-dashed border-gray-700 bg-gray-800/40 px-6 py-10 text-center text-sm text-gray-400">
                No active short baskets — hedge is open, waiting for basket entry
              </div>
            )}
          </div>

          {trades.length > 0 && (
            <BasketStorySection
              trade={trades[0]}
              recentAdjustments={adjustments}
              isOpen={basketStoryOpen}
              onToggle={() => setBasketStoryOpen((v) => !v)}
            />
          )}

          {trades.length > 0 && (
            <div className="mb-4">
              <PositionCard
                trade={trades[0]}
                recentAdjustments={adjustments}
                qtyMismatches={qtyMismatches}
                monitoringOnly
              />
            </div>
          )}

          {trades.length > 1 && (
            <div className="mb-4 space-y-4">
              {trades.slice(1).map((trade) => (
                <PositionCard
                  key={trade.trade_id}
                  trade={trade}
                  recentAdjustments={adjustments}
                  qtyMismatches={qtyMismatches}
                />
              ))}
            </div>
          )}
        </>
      ) : (
        <div className="rounded-xl border border-dashed border-gray-700 bg-gray-800/50 px-6 py-12 text-center">
          <div className="mb-3 text-4xl">🤖</div>
          <p className="text-lg font-medium text-white">No active trades</p>
          <p className="mt-1 text-sm text-gray-400">Bot is ready and monitoring</p>
          <Link
            to="/new-trade"
            className="mt-6 inline-block rounded-md bg-blue-500 px-4 py-2 text-sm font-medium text-white hover:bg-blue-400"
          >
            → Place New Strangle
          </Link>
          {accountConnected && (
            <div className="mt-6 space-y-1 text-sm text-gray-400">
              <div>Connected: {accountName || '—'}</div>
              <div>Balance: ${formatBalance(balance)}</div>
            </div>
          )}
        </div>
      )}

      <MultiAccountOverview
        overview={slaveOverview}
        onRefresh={refreshSlaveOverview}
        activeHedge={activeHedge}
      />

      {trades.length > 0 && (() => {
        const activeTrade = trades[0]
        const callStrike = Number(
          activeTrade.call_leg?.strike ?? activeTrade.call_strike,
        )
        const putStrike = Number(
          activeTrade.put_leg?.strike ?? activeTrade.put_strike,
        )
        const callPremium = Number(
          activeTrade.call_entry_premium ??
            activeTrade.call_leg?.initial_premium ??
            activeTrade.call_premium,
        )
        const putPremium = Number(
          activeTrade.put_entry_premium ??
            activeTrade.put_leg?.initial_premium ??
            activeTrade.put_premium,
        )
        const quantity = Number(
          activeTrade.call_leg?.quantity ??
            activeTrade.put_leg?.quantity ??
            activeTrade.call_quantity ??
            activeTrade.put_quantity ??
            activeTrade.quantity ??
            1,
        )
        const currentPrice = Number(activeTrade.underlying_price)
        const wingCall = activeTrade.wing_call
        const wingPut = activeTrade.wing_put
        const bothWings =
          wingCall?.strike != null && wingPut?.strike != null
        const maxLossUsd =
          activeTrade.max_loss_usd != null
            ? Number(activeTrade.max_loss_usd)
            : null
        return (
          <div className="mt-4 overflow-hidden rounded-xl border border-gray-700 bg-gray-800">
            <button
              type="button"
              onClick={() => setPayoffOpen((v) => !v)}
              className="flex w-full items-center justify-between bg-gray-800 px-4 py-3 text-sm font-semibold text-gray-300 hover:bg-gray-700"
            >
              <span>📈 Payoff Graph</span>
              <span className="text-gray-400">{payoffOpen ? '▲' : '▼'}</span>
            </button>
            {payoffOpen && (
              <div className="border-t border-gray-700 p-4">
                <PayoffGraph
                  callStrike={callStrike}
                  putStrike={putStrike}
                  callPremium={callPremium}
                  putPremium={putPremium}
                  quantity={quantity}
                  currentPrice={currentPrice > 0 ? currentPrice : undefined}
                  expiryDate={activeTrade.expiry_date || undefined}
                  initialHoursRemaining={
                    Number(activeTrade.hours_to_expiry) || undefined
                  }
                  wingCallStrike={
                    bothWings ? Number(wingCall.strike) : null
                  }
                  wingPutStrike={bothWings ? Number(wingPut.strike) : null}
                  wingCallPremium={
                    bothWings && wingCall?.initial_premium != null
                      ? Number(wingCall.initial_premium)
                      : null
                  }
                  wingPutPremium={
                    bothWings && wingPut?.initial_premium != null
                      ? Number(wingPut.initial_premium)
                      : null
                  }
                  callMarkPremium={
                    activeTrade.call_leg?.current_premium != null
                      ? Number(activeTrade.call_leg.current_premium)
                      : activeTrade.call_premium != null
                        ? Number(activeTrade.call_premium)
                        : null
                  }
                  putMarkPremium={
                    activeTrade.put_leg?.current_premium != null
                      ? Number(activeTrade.put_leg.current_premium)
                      : activeTrade.put_premium != null
                        ? Number(activeTrade.put_premium)
                        : null
                  }
                  wingCallMarkPremium={
                    bothWings && wingCall?.current_premium != null
                      ? Number(wingCall.current_premium)
                      : null
                  }
                  wingPutMarkPremium={
                    bothWings && wingPut?.current_premium != null
                      ? Number(wingPut.current_premium)
                      : null
                  }
                  maxLossUsd={maxLossUsd}
                  emptyMessage="Waiting for BTC price…"
                />
              </div>
            )}
          </div>
        )
      })()}

      <section className="mt-10">
        <h2 className="mb-3 text-lg font-semibold text-white">
          Structure History
        </h2>
        <p className="mb-3 text-xs text-gray-500">
          Each long hedge is a structure. Baskets under it are numbered 1…N for
          that structure only. Stored STRUCTURE P&amp;L is shown — not recomputed
          in the browser.
        </p>
        {structures.length === 0 ? (
          <div className="rounded-xl border border-dashed border-gray-700 px-4 py-8 text-center text-sm text-gray-500">
            No structures yet
          </div>
        ) : (
          <>
            <div className="space-y-3">
              {paginatedStructures.map((s) => (
                <StructureHistoryCard
                  key={s.hedge?.id}
                  structure={s}
                  ledger={ledgerByHedgeId[Number(s.hedge?.id)]}
                />
              ))}
            </div>
            {structures.length > STRUCTURES_PER_PAGE && (
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-700 bg-gray-800/60 px-4 py-3 text-sm text-gray-400">
                <span>
                  Showing {structureRangeStart}–{structureRangeEnd} of{' '}
                  {structures.length}
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    disabled={structurePage === 0}
                    onClick={() => setStructurePage((p) => Math.max(0, p - 1))}
                    className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    ← Prev
                  </button>
                  <span className="text-xs text-gray-500">
                    Page {structurePage + 1} of {structurePageCount}
                  </span>
                  <button
                    type="button"
                    disabled={structurePage >= structurePageCount - 1}
                    onClick={() =>
                      setStructurePage((p) =>
                        Math.min(structurePageCount - 1, p + 1),
                      )
                    }
                    className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    Next →
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </section>
    </main>
  )
}
