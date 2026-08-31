import { useState } from 'react'
import LoadingSpinner from './ui/LoadingSpinner'
import ConfirmDialog from './ui/ConfirmDialog'
import PnlSlider from './PnlSlider'
import { closeHedge, updateHedgeSettings } from '../services/api'

function fmtMoney(v, digits = 2) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function fmtSigned(v, digits = 4) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${fmtMoney(Math.abs(n), digits)}`
}

function pnlClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return 'text-gray-400'
  if (n > 0) return 'text-green-400'
  if (n < 0) return 'text-red-400'
  return 'text-gray-300'
}

function fmtStrike(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function fmtExpiry(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(`${iso}T00:00:00`)
    return d.toLocaleDateString('en-GB', {
      day: '2-digit',
      month: 'short',
      year: '2-digit',
      timeZone: 'Asia/Kolkata',
    })
  } catch {
    return String(iso)
  }
}

function fmtTimeLeft(hours) {
  const h = Number(hours)
  if (!Number.isFinite(h) || h < 0) return '—'
  const days = Math.floor(h / 24)
  const remH = Math.floor(h % 24)
  if (days <= 0) return `${remH}h left`
  return `${days}d ${remH}h left`
}

function fmtIv(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return '—'
  const pct = n <= 2 ? n * 100 : n
  return `${pct.toFixed(1)}%`
}

function avgIv(a, b) {
  const x = Number(a)
  const y = Number(b)
  const vals = [x, y].filter((n) => Number.isFinite(n) && n > 0)
  if (!vals.length) return null
  return vals.reduce((s, n) => s + n, 0) / vals.length
}

function MiniProgress({ label, pct, barClass, suffix }) {
  const width = Math.min(100, Math.max(0, Math.abs(Number(pct) || 0)))
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-baseline justify-between gap-1 text-[11px] text-gray-400">
        <span>{label}</span>
        {suffix ? <span>{suffix}</span> : null}
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-800">
        <div
          className={`h-full rounded-full transition-all ${barClass}`}
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  )
}

/**
 * Live long-hedge panel — dashboard left column (B13).
 */
export default function HedgePanel({ hedge, onClosed, onUpdated }) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(null)
  const [error, setError] = useState('')
  const [editField, setEditField] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [editSaving, setEditSaving] = useState(false)
  const [editError, setEditError] = useState('')

  if (!hedge) return null

  const call = hedge.call || {}
  const put = hedge.put || {}
  const strike = hedge.strike
  const qty = Number(hedge.quantity) || 1
  const net =
    hedge.hedge_net_mtm != null && Number.isFinite(Number(hedge.hedge_net_mtm))
      ? Number(hedge.hedge_net_mtm)
      : hedge.net_pnl
  const gross =
    hedge.gross_upnl != null && Number.isFinite(Number(hedge.gross_upnl))
      ? Number(hedge.gross_upnl)
      : hedge.gross_pnl != null && Number.isFinite(Number(hedge.gross_pnl))
        ? Number(hedge.gross_pnl)
        : null
  const estExitSlip = Number(hedge.est_exit_slippage_usd)
  const feesUsd = Number(hedge.fees_usd)
  const exitSlipUnavailable =
    Number.isFinite(estExitSlip) &&
    estExitSlip === 0 &&
    String(hedge.status || '').toLowerCase() !== 'closed'
  const entryIv = avgIv(hedge.entry_call_iv, hedge.entry_put_iv)
  const liveIv = avgIv(hedge.current_call_iv, hedge.current_put_iv)
  const daysLogged = Number(hedge.days_logged) || 0
  const todayThetaUsd = Number(hedge.today_theta_usd)
  const todayThetaDisplay = Number.isFinite(todayThetaUsd)
    ? -Math.abs(todayThetaUsd)
    : null
  const accrued = Number(hedge.theta_accrued_estimate)
  const accruedDisplay = Number.isFinite(accrued) ? -Math.abs(accrued) : null
  const targetUsd = Number(hedge.target_usd)
  const slBudget =
    hedge.sl_budget != null ? Number(hedge.sl_budget) : Number(hedge.stoploss_usd)
  const pctTarget = hedge.pct_to_target
  const pctStop = hedge.pct_to_stop
  const daysHeld = hedge.days_held ?? hedge.days_since_entry ?? 0
  const minHold = hedge.hedge_min_hold_days ?? 10

  const openEdit = (field) => {
    setEditError('')
    setEditField(field)
    if (field === 'target') {
      setEditValue(
        hedge.target_usd != null && Number.isFinite(Number(hedge.target_usd))
          ? String(hedge.target_usd)
          : '',
      )
    } else {
      setEditValue(
        hedge.stoploss_usd != null &&
          Number.isFinite(Number(hedge.stoploss_usd))
          ? String(hedge.stoploss_usd)
          : '',
      )
    }
  }

  const saveEdit = async () => {
    if (!editField || editSaving) return
    const val = Number(editValue)
    if (!Number.isFinite(val) || val <= 0) {
      setEditError('Value must be greater than 0')
      return
    }
    setEditSaving(true)
    setEditError('')
    try {
      const payload =
        editField === 'target'
          ? { target_usd: val }
          : { stoploss_usd: val }
      await updateHedgeSettings(hedge.id, payload)
      setEditField(null)
      onUpdated?.()
    } catch (err) {
      setEditError(err.message || 'Failed to save')
    } finally {
      setEditSaving(false)
    }
  }

  const handleClose = async () => {
    if (loading) return
    setLoading(true)
    setError('')
    try {
      const result = await closeHedge(hedge.id, 'HEDGE_MANUAL')
      setDone({
        realizedPnl: result?.realized_pnl,
        exitReason: result?.exit_reason || 'HEDGE_MANUAL',
        message: result?.message,
      })
      onClosed?.(result)
    } catch (err) {
      setError(err.message || 'Failed to close hedge')
    } finally {
      setLoading(false)
    }
  }

  const closeModal = () => {
    if (loading) return
    setConfirmOpen(false)
    setError('')
    if (done) {
      setDone(null)
      onClosed?.(done)
    }
  }

  const hedgeDeductions = [
    {
      label: 'Exit spread',
      amount: estExitSlip,
      title: exitSlipUnavailable
        ? 'Exit spread estimate unavailable'
        : 'Estimated exit spread on close',
    },
    { label: 'Fees', amount: feesUsd, title: 'Round-trip fees' },
  ]

  return (
    <>
      <section className="flex h-full flex-col rounded-xl border border-emerald-800/50 bg-gray-900 shadow-lg">
        {/* Header */}
        <header className="border-b border-gray-800 px-4 py-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h2 className="text-base font-bold text-white">
                <span className="mr-1.5 text-green-400">●</span>
                LONG HEDGE #{hedge.id}
              </h2>
              <p className="mt-0.5 text-xs text-gray-400">
                {String(hedge.underlying || 'BTC').toUpperCase()} ·{' '}
                {fmtStrike(strike)} Straddle
              </p>
            </div>
            <div className="text-right text-xs text-gray-400">
              <div>
                {fmtExpiry(hedge.expiry_date)} · {fmtTimeLeft(hedge.hours_to_expiry)}
              </div>
              <div className="mt-0.5 font-medium text-gray-300">
                {qty} lot{qty === 1 ? '' : 's'}
              </div>
            </div>
          </div>
        </header>

        {hedge.roll_pending ? (
          <div
            className="mx-4 mt-3 rounded-lg border border-amber-600/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-200"
            role="status"
          >
            ROLLING — waiting for basket #
            {hedge.roll_waiting_basket_seq != null
              ? hedge.roll_waiting_basket_seq
              : hedge.roll_waiting_trade_id ?? '—'}{' '}
            to close.
          </div>
        ) : null}

        <div className="flex-1 space-y-4 px-4 py-3">
          {/* Legs table */}
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-xs">
              <thead className="text-[10px] uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="pb-1 pr-2">Type</th>
                  <th className="pb-1 pr-2">Strike</th>
                  <th className="pb-1 pr-2">Entry</th>
                  <th className="pb-1 pr-2">Now</th>
                  <th className="pb-1 pr-2">UPL</th>
                  <th className="pb-1">Qty</th>
                </tr>
              </thead>
              <tbody className="font-mono text-gray-200">
                <tr>
                  <td className="py-1 pr-2 font-medium text-blue-300">CALL</td>
                  <td className="py-1 pr-2">{fmtStrike(strike)}</td>
                  <td className="py-1 pr-2">${fmtMoney(call.entry_fill)}</td>
                  <td className="py-1 pr-2">${fmtMoney(call.current_bid)}</td>
                  <td className={`py-1 pr-2 font-medium ${pnlClass(call.upl)}`}>
                    {fmtSigned(call.upl, 4)}
                  </td>
                  <td className="py-1 text-gray-300">{qty}</td>
                </tr>
                <tr>
                  <td className="py-1 pr-2 font-medium text-amber-300">PUT</td>
                  <td className="py-1 pr-2">{fmtStrike(strike)}</td>
                  <td className="py-1 pr-2">${fmtMoney(put.entry_fill)}</td>
                  <td className="py-1 pr-2">${fmtMoney(put.current_bid)}</td>
                  <td className={`py-1 pr-2 font-medium ${pnlClass(put.upl)}`}>
                    {fmtSigned(put.upl, 4)}
                  </td>
                  <td className="py-1 text-gray-300">{qty}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            {/* P&L block */}
            <div className="space-y-2">
              <div className="flex justify-between text-xs">
                <span className="text-gray-400">Entry cost</span>
                <span className="font-medium text-gray-100">
                  ${fmtMoney(hedge.cost_usd, 3)}
                </span>
              </div>
              <div className="flex justify-between text-xs">
                <span className="text-gray-400">Current value</span>
                <span className="font-medium text-gray-100">
                  ${fmtMoney(hedge.current_value_usd, 3)}
                </span>
              </div>
              <PnlSlider
                grossLabel="Gross"
                gross={gross}
                net={net}
                netLabel="Net MTM"
                deductions={hedgeDeductions}
              />
            </div>

            {/* Theta + IV */}
            <div className="space-y-2 text-xs">
              <div className="flex justify-between gap-2">
                <span className="text-gray-400">Today&apos;s theta</span>
                <span className={pnlClass(todayThetaDisplay)}>
                  {fmtSigned(todayThetaDisplay, 4)}
                </span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-gray-400">
                  Theta accrued{' '}
                  <span className="rounded bg-amber-950/60 px-1 text-[9px] uppercase text-amber-400">
                    estimate
                  </span>
                </span>
                <span className={pnlClass(accruedDisplay)}>
                  {fmtSigned(accruedDisplay, 4)}
                  {daysLogged > 0 ? (
                    <span className="ml-1 text-gray-500">({daysLogged}d)</span>
                  ) : null}
                </span>
              </div>
              <div className="flex justify-between gap-2 border-t border-gray-800 pt-2">
                <span className="text-gray-400">IV entry / now</span>
                <span className="text-gray-200">
                  {fmtIv(entryIv)} · {fmtIv(liveIv)}
                </span>
              </div>
            </div>
          </div>

          {/* Target / SL compact */}
          <div className="space-y-3 rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-3 text-xs">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="text-green-400">
                🎯 Target (structure):{' '}
                <span className="font-bold">+${fmtMoney(targetUsd)}</span>
              </span>
              <span className="text-gray-500">
                held {fmtMoney(daysHeld, 0)}/{minHold}d
                {pctTarget != null ? ` · ${fmtMoney(pctTarget, 1)}% progress` : ''}
              </span>
            </div>
            <MiniProgress
              label="Target progress"
              pct={pctTarget}
              barClass="bg-green-500/80"
              suffix={
                pctTarget != null
                  ? `${fmtMoney(pctTarget, 1)}% of target reached`
                  : null
              }
            />

            <div className="flex flex-wrap items-baseline justify-between gap-2 pt-1">
              <span className="text-red-400">
                🛑 Stop (structure):{' '}
                <span className="font-bold">−${fmtMoney(slBudget)}</span>
              </span>
              {pctStop != null ? (
                <span className="text-gray-500">{fmtMoney(pctStop, 1)}% used</span>
              ) : null}
            </div>
            <MiniProgress
              label="SL consumed"
              pct={pctStop}
              barClass="bg-red-500/80"
              suffix={
                pctStop != null
                  ? `${fmtMoney(pctStop, 1)}% of SL consumed`
                  : null
              }
            />
          </div>
        </div>

        {/* Footer */}
        <footer className="mt-auto flex flex-wrap items-center justify-between gap-3 border-t border-gray-800 px-4 py-3">
          <p className="text-[11px] text-gray-500">
            Bracket SL: N/A — closes via target / stop / expiry
          </p>
          <button
            type="button"
            disabled={loading}
            onClick={() => {
              setDone(null)
              setError('')
              setConfirmOpen(true)
            }}
            className="inline-flex items-center gap-2 rounded-md bg-red-700 px-4 py-2 text-sm font-semibold text-white hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading && <LoadingSpinner size="sm" color="white" />}
            Close Hedge
          </button>
        </footer>
      </section>

      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4">
          <div className="w-full max-w-md rounded-xl border border-red-700/60 bg-gray-900 p-5 shadow-2xl">
            {done ? (
              <div className="space-y-3 text-sm text-gray-200">
                <h3 className="text-lg font-semibold text-green-400">
                  Hedge Closed
                </h3>
                <p>{done.message || `Hedge #${hedge.id} closed.`}</p>
                <p>
                  Realized P&L:{' '}
                  <span className={pnlClass(done.realizedPnl)}>
                    {fmtSigned(done.realizedPnl, 4)}
                  </span>
                </p>
                <div className="flex justify-end pt-2">
                  <button
                    type="button"
                    onClick={closeModal}
                    className="rounded-md bg-gray-700 px-3 py-1.5 text-sm text-white hover:bg-gray-600"
                  >
                    Close
                  </button>
                </div>
              </div>
            ) : (
              <>
                <h3 className="text-lg font-semibold text-red-400">
                  Close Hedge #{hedge.id}?
                </h3>
                <p className="mt-3 text-sm text-gray-300">
                  This will close BOTH hedge legs at market with reduce-only
                  orders.
                </p>
                <p className="mt-2 text-sm text-amber-200">
                  {Number(hedge.open_basket_count) > 0 ? (
                    <>
                      Also closes{' '}
                      <span className="font-semibold">
                        {Number(hedge.open_basket_count)} open basket
                        {Number(hedge.open_basket_count) === 1 ? '' : 's'}
                      </span>{' '}
                      and mirrored slaves.
                    </>
                  ) : (
                    <>No open baskets linked right now.</>
                  )}
                </p>
                {error ? (
                  <p className="mt-3 rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                    {error}
                  </p>
                ) : null}
                <div className="mt-5 flex justify-end gap-2">
                  <button
                    type="button"
                    disabled={loading}
                    onClick={closeModal}
                    className="rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    disabled={loading}
                    onClick={handleClose}
                    className="inline-flex items-center gap-2 rounded-md bg-red-600 px-3 py-1.5 text-sm font-bold text-white hover:bg-red-500 disabled:opacity-50"
                  >
                    {loading && <LoadingSpinner size="sm" color="white" />}
                    Yes, Close Hedge
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        isOpen={Boolean(editField)}
        title={
          editField === 'target'
            ? 'Edit Hedge Target ($)'
            : 'Edit Hedge Stop Loss ($)'
        }
        message={
          <label className="block text-left text-sm text-gray-300">
            {editField === 'target'
              ? 'Profit target in USD (must be > 0).'
              : 'Stop loss in USD (must be > 0).'}
            <input
              type="number"
              min={0.01}
              step={0.01}
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              className="mt-2 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            {editError ? (
              <span className="mt-2 block text-xs text-red-400">{editError}</span>
            ) : null}
          </label>
        }
        confirmLabel={editSaving ? 'Saving…' : 'Save'}
        confirmDisabled={editSaving}
        onCancel={() => {
          if (editSaving) return
          setEditField(null)
          setEditError('')
        }}
        onConfirm={saveEdit}
      />
    </>
  )
}
