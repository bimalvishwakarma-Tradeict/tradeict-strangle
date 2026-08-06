import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useTrades } from '../hooks/useTrades'
import PositionCard from '../components/PositionCard'
import {
  checkHealth,
  enableAutoTrade,
  getAccountStatus,
  getAutoTradeStatus,
  getSlaveOverview,
  getTradeHistory,
} from '../services/api'

const AUTO_STATUS_POLL_MS = 5000
const SLAVE_OVERVIEW_POLL_MS = 30000

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

function formatSignedMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}$${fmtMoney(n)}`
}

function AccountOverviewRow({
  rowKey,
  role,
  name,
  multiplier,
  statusKind,
  statusText,
  balanceUsd,
  balanceInr,
  availableUsd,
  availableInr,
  netMtm,
  mtmSyncIso,
  targetUsd,
  isExpanded,
  onToggle,
  borderClass,
  dimmed,
  expandContent,
}) {
  const syncAge = formatSyncAge(mtmSyncIso)
  const syncSecs = syncAgeSeconds(mtmSyncIso)
  const syncStale = syncSecs != null && syncSecs > 60

  return (
    <>
      <tr
        onClick={onToggle}
        className={`cursor-pointer border-b border-gray-800 hover:bg-gray-800/80 ${
          dimmed ? 'opacity-50 italic' : ''
        } ${isExpanded ? 'bg-gray-800/50' : ''}`}
      >
        <td className={`px-3 py-3 text-sm ${borderClass}`}>
          {role === 'master' ? (
            <span className="font-medium text-amber-200">⭐ Master</span>
          ) : (
            <span className="font-medium text-blue-300">
              📋 Slave
              {multiplier != null ? (
                <span className="ml-1 text-xs text-gray-400">
                  ({Number(multiplier)}×)
                </span>
              ) : null}
            </span>
          )}
        </td>
        <td className="px-3 py-3 text-sm text-white" title={name}>
          {truncateName(name)}
        </td>
        <td className="px-3 py-3 text-sm">
          {statusKind === 'live' && (
            <span className="text-green-400">🟢 Live</span>
          )}
          {statusKind === 'ready' && (
            <span className="text-yellow-300">🟡 Ready</span>
          )}
          {statusKind === 'error' && (
            <span className="text-red-400" title={statusText}>
              🔴 {truncateName(statusText || 'Error', 16)}
            </span>
          )}
          {statusKind === 'paused' && (
            <span className="text-gray-400">⚪ Paused</span>
          )}
          {statusKind === 'offline' && (
            <span className="text-gray-500">⚪ Offline</span>
          )}
        </td>
        <td className="px-3 py-3 text-sm text-gray-200">
          {balanceUsd != null ? (
            <>
              <div>${formatBalance(balanceUsd)}</div>
              <div className="text-xs text-gray-500">₹{formatInr(balanceInr)}</div>
            </>
          ) : (
            <span className="text-gray-500">—</span>
          )}
        </td>
        <td className="px-3 py-3 text-sm text-gray-200">
          {availableUsd != null ? (
            <>
              <div>${formatBalance(availableUsd)}</div>
              <div className="text-xs text-gray-500">
                ₹{formatInr(availableInr)}
              </div>
            </>
          ) : (
            <span className="text-gray-500">—</span>
          )}
        </td>
        <td className={`px-3 py-3 text-sm font-medium ${mtmClass(netMtm)}`}>
          {netMtm == null || !Number.isFinite(Number(netMtm)) ? (
            '—'
          ) : (
            <>
              <div>{formatSignedMoney(netMtm)}</div>
              {syncAge ? (
                <div
                  className={`text-[10px] font-normal ${
                    syncStale ? 'text-yellow-300' : 'text-gray-500'
                  }`}
                  title={
                    syncStale
                      ? 'MTM sync older than 60s — may be stale'
                      : undefined
                  }
                >
                  {syncStale ? '⚠️ ' : ''}last sync: {syncAge}
                </div>
              ) : null}
            </>
          )}
        </td>
        <td className="px-3 py-3 text-sm text-gray-300">
          {targetUsd != null && Number.isFinite(Number(targetUsd))
            ? `$${fmtMoney(targetUsd)}`
            : '—'}
        </td>
      </tr>
      {isExpanded && (
        <tr className="border-b border-gray-800 bg-gray-800/40">
          <td colSpan={7} className="px-4 py-3 text-xs text-gray-300">
            {expandContent}
          </td>
        </tr>
      )}
    </>
  )
}

function MultiAccountOverview({ overview }) {
  const [expanded, setExpanded] = useState({})

  if (!overview?.has_slaves) return null

  const master = overview.master || {}
  const slaves = overview.slaves || []
  const combined = Number(overview.combined_mtm || 0)

  const toggle = (key) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  const masterTrade = master.active_trade
  let masterStatus = 'offline'
  let masterStatusText = ''
  if (!master.connected) {
    masterStatus = 'offline'
  } else if (masterTrade) {
    masterStatus = 'live'
  } else {
    masterStatus = 'ready'
  }

  // Prefer live net_mtm from dashboard trades when available via expand only —
  // overview already carries tracker MTM.
  const masterExpand = masterTrade ? (
    <div className="space-y-1 font-mono">
      <div>
        CALL ${fmtStrike(masterTrade.call_strike)} @ $
        {fmtMoney(masterTrade.call_entry)} | Current $
        {fmtMoney(masterTrade.call_premium)}
      </div>
      <div>
        PUT ${fmtStrike(masterTrade.put_strike)} @ $
        {fmtMoney(masterTrade.put_entry)} | Current $
        {fmtMoney(masterTrade.put_premium)}
      </div>
      <div className="pt-1 text-gray-400">
        Net MTM:{' '}
        <span className={mtmClass(masterTrade.net_mtm)}>
          {formatSignedMoney(masterTrade.net_mtm)}
        </span>
        {' · '}
        Target: ${fmtMoney(masterTrade.profit_target_usd)} · SL: $
        {fmtMoney(masterTrade.stoploss_usd)}
      </div>
    </div>
  ) : (
    <span className="text-gray-500">No active master trade</span>
  )

  return (
    <section className="mb-8 overflow-hidden rounded-xl border border-gray-700 bg-gray-900">
      <div className="border-b border-gray-700 px-4 py-3">
        <h2 className="text-sm font-semibold text-white">
          📊 Multi-Account Overview
        </h2>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left">
          <thead className="bg-gray-800 text-[10px] uppercase tracking-wide text-gray-400">
            <tr>
              <th className="px-3 py-2">Role</th>
              <th className="px-3 py-2">Account</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Capital</th>
              <th className="px-3 py-2">Avail.</th>
              <th className="px-3 py-2">Net MTM</th>
              <th className="px-3 py-2">Target</th>
            </tr>
          </thead>
          <tbody>
            <AccountOverviewRow
              rowKey="master"
              role="master"
              name={master.name || 'Master'}
              statusKind={masterStatus}
              statusText={masterStatusText}
              balanceUsd={master.connected ? master.balance_usd : null}
              balanceInr={master.balance_inr}
              availableUsd={
                master.connected ? master.available_usd ?? null : null
              }
              availableInr={master.available_inr}
              netMtm={masterTrade?.net_mtm ?? null}
              targetUsd={masterTrade?.profit_target_usd ?? null}
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
              const slaveMtm =
                st?.net_mtm ?? st?.last_mtm ?? null
              const slaveMtmUpdated =
                st?.last_mtm_updated || st?.net_mtm_updated || st?.last_updated
              const syncAge = formatSyncAge(slaveMtmUpdated)
              const syncSecs = syncAgeSeconds(slaveMtmUpdated)
              const syncStale = syncSecs != null && syncSecs > 60

              const expand = st ? (
                <div className="space-y-1 font-mono">
                  <div>
                    CALL ${fmtStrike(st.call_strike)} qty={st.actual_quantity}{' '}
                    | Fill ${fmtMoney(st.call_fill_price)}
                  </div>
                  <div>
                    PUT ${fmtStrike(st.put_strike)} qty={st.actual_quantity}{' '}
                    | Fill ${fmtMoney(st.put_fill_price)}
                  </div>
                  <div className="pt-1 text-gray-400">
                    Net MTM (Delta):{' '}
                    <span className={mtmClass(slaveMtm)}>
                      {formatSignedMoney(slaveMtm)}
                    </span>
                    {syncAge ? (
                      <span
                        className={
                          syncStale ? ' text-yellow-300' : ' text-gray-500'
                        }
                      >
                        {' '}
                        (last sync: {syncAge}
                        {syncStale ? ' ⚠️' : ''})
                      </span>
                    ) : null}
                    {' · '}
                    Status: {st.status}
                  </div>
                </div>
              ) : (
                <span className="text-gray-500">
                  {slave.is_active
                    ? 'No active mirrored trade'
                    : 'Account paused — not mirroring'}
                </span>
              )

              return (
                <AccountOverviewRow
                  key={key}
                  rowKey={key}
                  role="slave"
                  name={slave.name}
                  multiplier={slave.qty_multiplier}
                  statusKind={statusKind}
                  statusText={statusText}
                  balanceUsd={
                    statusKind === 'error' ? null : slave.balance_usd
                  }
                  balanceInr={slave.balance_inr}
                  availableUsd={
                    statusKind === 'error'
                      ? null
                      : slave.available_usd ?? null
                  }
                  availableInr={slave.available_inr}
                  netMtm={slaveMtm}
                  mtmSyncIso={slaveMtmUpdated}
                  targetUsd={st?.profit_target_usd ?? null}
                  isExpanded={Boolean(expanded[key])}
                  onToggle={() => toggle(key)}
                  borderClass={
                    statusKind === 'error'
                      ? 'border-l-2 border-l-red-500'
                      : 'border-l-2 border-l-blue-500'
                  }
                  dimmed={!slave.is_active}
                  expandContent={expand}
                />
              )
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-gray-600 bg-gray-800/60">
              <td
                colSpan={5}
                className="px-3 py-3 text-right text-sm font-semibold text-gray-300"
              >
                Combined MTM:
              </td>
              <td
                colSpan={2}
                className={`px-3 py-3 text-base font-bold ${mtmClass(combined)}`}
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
  }, [status?.is_enabled, status?.seconds_until_entry, status?.next_entry_time])

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
            ? `Retry in ${secondsLeft}s`
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
            ? `Next entry in ${secondsLeft}s ⏱`
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
  const { trades, wsStatus, loading, errors, adjustments, autoTradeStatus } =
    useTrades()
  const [updatedAt, setUpdatedAt] = useState(() => formatIstTime())
  const [backendOnline, setBackendOnline] = useState(true)
  const [accountName, setAccountName] = useState('')
  const [balance, setBalance] = useState(0)
  const [accountConnected, setAccountConnected] = useState(false)
  const [baskets, setBaskets] = useState([])
  const [autoStatus, setAutoStatus] = useState(null)
  const [slaveOverview, setSlaveOverview] = useState(null)

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

      <AutoTradeBanner
        status={autoStatus}
        activeTrade={bannerActiveTrade}
        onEnterNow={handleEnterNow}
      />

      <MultiAccountOverview overview={slaveOverview} />

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
