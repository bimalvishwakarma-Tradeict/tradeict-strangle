import { useState } from 'react'
import LoadingSpinner from './ui/LoadingSpinner'
import ConfirmDialog from './ui/ConfirmDialog'
import { closeHedge, updateHedgeSettings } from '../services/api'

function fmtMoney(v, digits = 2) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function fmtSigned(v, digits = 2) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${fmtMoney(Math.abs(n), digits)}`
}

function fmtDeduction(v, digits = 3) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return '−$0.000'
  return `−$${fmtMoney(Math.abs(n), digits)}`
}

function fmtAddBack(v, digits = 3) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return `+$${fmtMoney(Math.abs(n), digits)}`
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
  // Stored as fraction (0.36) or percent (36)
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

/**
 * Live long-hedge panel (spec 5.1). Renders above basket cards.
 */
export default function HedgePanel({ hedge, onClosed, onUpdated }) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(null)
  const [error, setError] = useState('')
  const [editField, setEditField] = useState(null) // 'target' | 'sl' | null
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
      : hedge.gross_pnl
  const entrySpread = Number(hedge.entry_spread_usd)
  const estExitSlip = Number(hedge.est_exit_slippage_usd)
  const feesUsd = Number(hedge.fees_usd)
  const exitSpreadPct = Number(hedge.hedge_exit_spread_pct)
  const slBasisUsd = hedge.sl_basis_usd
  const hedgeOnlyForSl = hedge.hedge_only_for_sl
  const openBasketGross = Number(hedge.open_basket_gross)
  const netNum = Number(net)
  const grossNum = Number(gross)
  const pnlBreakdownMismatch =
    Number.isFinite(grossNum) &&
    Number.isFinite(netNum) &&
    Number.isFinite(estExitSlip) &&
    Number.isFinite(feesUsd) &&
    Math.abs(grossNum - estExitSlip - feesUsd - netNum) > 0.01
  const exitSlipUnavailable =
    Number.isFinite(estExitSlip) &&
    estExitSlip === 0 &&
    String(hedge.status || '').toLowerCase() !== 'closed'
  const entryIv = avgIv(hedge.entry_call_iv, hedge.entry_put_iv)
  const liveIv = avgIv(hedge.current_call_iv, hedge.current_put_iv)
  const daysLogged = Number(hedge.days_logged) || 0
  // Long theta decays — show today's theta USD as a cost (negative)
  const todayThetaUsd = Number(hedge.today_theta_usd)
  const todayThetaDisplay = Number.isFinite(todayThetaUsd)
    ? -Math.abs(todayThetaUsd)
    : null
  const accrued = Number(hedge.theta_accrued_estimate)
  const accruedDisplay = Number.isFinite(accrued) ? -Math.abs(accrued) : null

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

  return (
    <>
      <section className="mb-6 rounded-xl border border-emerald-800/50 bg-gradient-to-br from-gray-900 via-gray-900 to-emerald-950/30 px-5 py-4 shadow-lg">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-emerald-300">
            Long Hedge #{hedge.id}
          </h2>
          <div className="text-xs text-gray-400">
            {String(hedge.underlying || 'BTC').toUpperCase()}{' '}
            {fmtStrike(strike)} Straddle · {fmtExpiry(hedge.expiry_date)} ·{' '}
            {fmtTimeLeft(hedge.hours_to_expiry)} · {qty} lot
            {qty === 1 ? '' : 's'}
          </div>
        </div>

        {hedge.roll_pending ? (
          <div
            className="mb-4 rounded-lg border border-amber-600/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-200"
            role="status"
          >
            ROLLING - waiting for basket #
            {hedge.roll_waiting_basket_seq != null
              ? hedge.roll_waiting_basket_seq
              : hedge.roll_waiting_trade_id != null
                ? hedge.roll_waiting_trade_id
                : '—'}{' '}
            to close. Force close at {Number(hedge.hedge_roll_hard_dte) || 5} DTE.
          </div>
        ) : null}

        <div className="mb-4 grid gap-2 text-sm sm:grid-cols-2">
          <div className="rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2 font-mono text-xs sm:text-sm">
            <span className="text-blue-300">CALL {fmtStrike(strike)}</span>
            <span className="mx-2 text-gray-600">·</span>
            <span className="text-gray-400">entry</span>{' '}
            <span className="text-gray-200">${fmtMoney(call.entry_fill)}</span>
            <span className="mx-2 text-gray-600">·</span>
            <span className="text-gray-400">now</span>{' '}
            <span className="text-gray-200">${fmtMoney(call.current_bid)}</span>
            <span className="mx-2 text-gray-600">·</span>
            <span className="text-gray-400">UPL</span>{' '}
            <span className={pnlClass(call.upl)}>{fmtSigned(call.upl, 4)}</span>
          </div>
          <div className="rounded-lg border border-gray-800 bg-gray-950/40 px-3 py-2 font-mono text-xs sm:text-sm">
            <span className="text-amber-300">PUT {fmtStrike(strike)}</span>
            <span className="mx-2 text-gray-600">·</span>
            <span className="text-gray-400">entry</span>{' '}
            <span className="text-gray-200">${fmtMoney(put.entry_fill)}</span>
            <span className="mx-2 text-gray-600">·</span>
            <span className="text-gray-400">now</span>{' '}
            <span className="text-gray-200">${fmtMoney(put.current_bid)}</span>
            <span className="mx-2 text-gray-600">·</span>
            <span className="text-gray-400">UPL</span>{' '}
            <span className={pnlClass(put.upl)}>{fmtSigned(put.upl, 4)}</span>
          </div>
        </div>

        <div className="mb-4 grid gap-3 text-sm sm:grid-cols-2">
          <div className="space-y-1.5">
            <div className="flex justify-between gap-3">
              <span className="text-gray-400">Entry cost</span>
              <span className="font-medium text-gray-100">
                ${fmtMoney(hedge.cost_usd, 3)}
              </span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-gray-400">Current value</span>
              <span className="font-medium text-gray-100">
                ${fmtMoney(hedge.current_value_usd, 3)}
              </span>
            </div>
            <div className="space-y-0.5 border-t border-gray-800 pt-1.5 text-[11px] text-gray-500">
              <div className="flex justify-between gap-3">
                <span>Gross</span>
                <span className={pnlClass(gross)}>{fmtSigned(gross, 4)}</span>
              </div>
              <div className="flex justify-between gap-3 pl-2">
                <span>
                  less est. exit spread
                  {Number.isFinite(exitSpreadPct) ? (
                    <span className="ml-1 text-gray-600">
                      ({fmtMoney(exitSpreadPct, 1)}%)
                    </span>
                  ) : null}
                  {exitSlipUnavailable ? (
                    <span
                      className="ml-1 text-amber-500/90"
                      title="Exit spread estimate unavailable — bid missing or estimate failed; stop basis may be tighter than intended"
                    >
                      ⚠
                    </span>
                  ) : null}
                </span>
                <span>
                  {fmtDeduction(estExitSlip, 3)}
                  {exitSlipUnavailable ? (
                    <span className="ml-1 text-[10px] text-amber-500/90">
                      estimate unavailable
                    </span>
                  ) : null}
                </span>
              </div>
              <div className="flex justify-between gap-3 pl-2">
                <span>less fees</span>
                <span>{fmtDeduction(feesUsd, 3)}</span>
              </div>
              <div className="my-1 border-t border-gray-800/80" />
              <div className="flex justify-between gap-3">
                <span className="font-semibold text-gray-300">
                  Hedge P&L (net)
                  {pnlBreakdownMismatch ? (
                    <span
                      className="ml-1 font-normal text-amber-500/90"
                      title="Gross − exit spread − fees does not match net — check backend payload"
                    >
                      ⚠ mismatch
                    </span>
                  ) : null}
                </span>
                <span className={`font-semibold ${pnlClass(net)}`}>
                  {fmtSigned(net, 4)}
                </span>
              </div>
              <p className="pt-0.5 text-[10px] leading-snug text-gray-600">
                Gross is marked bid-vs-entry, so the entry spread is already
                inside it.
              </p>
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="flex justify-between gap-3">
              <span className="text-gray-400">Today&apos;s theta</span>
              <span className={pnlClass(todayThetaDisplay)}>
                {fmtSigned(todayThetaDisplay, 4)}
              </span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-gray-400">
                Theta accrued{' '}
                <span className="text-[10px] uppercase tracking-wide text-amber-500/90">
                  estimate
                </span>
              </span>
              <span className={pnlClass(accruedDisplay)}>
                {fmtSigned(accruedDisplay, 4)}
                {daysLogged > 0 ? (
                  <span className="ml-1 text-xs text-gray-500">
                    ({daysLogged} day{daysLogged === 1 ? '' : 's'})
                  </span>
                ) : null}
              </span>
            </div>
            <p className="text-[11px] leading-snug text-gray-500">
              Theta accrued is an ESTIMATE (sum of daily snapshots), not a cash
              flow. Real hedge P&L combines theta, vega, and delta.
            </p>
          </div>
        </div>

        <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-gray-800 pt-3 text-xs text-gray-400">
          <div
            className="rounded-md px-1.5 py-0.5 text-left"
            title="Live target uses structure P&L: hedge + booked baskets + open basket gross, with hedge entry spread added back"
          >
            Target{' '}
            <span className="text-gray-500">(structure basis)</span>:{' '}
            <span className="text-green-400">
              +${fmtMoney(hedge.target_usd)}
            </span>
            <span className="ml-1 text-gray-500">
              ({fmtMoney(hedge.hedge_target_multiple ?? 3, 1)}x monthly, held{' '}
              {fmtMoney(hedge.days_held ?? hedge.days_since_entry ?? 0, 0)}/
              {hedge.hedge_min_hold_days ?? 10}d)
            </span>
            {hedge.pct_to_target != null && (
              <span className="ml-1 text-gray-500">
                ({fmtMoney(hedge.pct_to_target, 1)}%)
              </span>
            )}
          </div>
          <div
            className="rounded-md px-1.5 py-0.5 text-left"
            title="Stop uses the whole structure — hedge plus open baskets — with entry and exit spread added back, so execution cost cannot trigger the stop."
          >
            <div>
              Stop{' '}
              <span className="text-gray-500">(structure basis)</span>:{' '}
              <span className="text-red-400">
                −${fmtMoney(
                  hedge.sl_budget != null ? hedge.sl_budget : hedge.stoploss_usd,
                )}
              </span>
              <span className="ml-1 text-gray-500">
                (fixed $
                {fmtMoney(
                  hedge.hedge_fixed_sl_usd != null
                    ? hedge.hedge_fixed_sl_usd
                    : 2,
                )}{' '}
                + booked $
                {fmtMoney(hedge.cum_closed_basket_pnl ?? 0)})
              </span>
              {hedge.pct_to_stop != null && (
                <span className="ml-1 text-gray-500">
                  ({fmtMoney(hedge.pct_to_stop, 1)}%)
                </span>
              )}
            </div>
            <div className="mt-1 space-y-0.5 text-[10px] leading-snug text-gray-500">
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                <span>Hedge P&L (net)</span>
                <span className={pnlClass(net)}>{fmtSigned(net, 4)}</span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 pl-2">
                <span>+ entry spread</span>
                <span>{fmtAddBack(entrySpread, 3)}</span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                <span className="text-gray-400">Hedge only</span>
                <span className={pnlClass(hedgeOnlyForSl)}>
                  {fmtSigned(hedgeOnlyForSl, 4)}
                </span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 pl-2">
                <span>+ est. exit spread</span>
                <span>{fmtAddBack(estExitSlip, 3)}</span>
              </div>
              {Number.isFinite(openBasketGross) ? (
                <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 pl-2">
                  <span>+ open basket (gross)</span>
                  <span>{fmtAddBack(openBasketGross, 3)}</span>
                </div>
              ) : null}
              <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
                <span className="text-gray-400">Structure basis</span>
                <span className={pnlClass(slBasisUsd)}>
                  {fmtSigned(slBasisUsd, 4)}
                </span>
              </div>
            </div>
          </div>
          <span>
            IV entry {fmtIv(entryIv)} · IV now {fmtIv(liveIv)}
          </span>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-gray-500">
            Bracket SL: N/A on hedge (closes via target / stop / expiry)
          </div>
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
        </div>
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
                  This will close BOTH hedge legs (call and put) at market with
                  reduce-only orders.
                </p>
                <p className="mt-2 text-sm text-amber-200">
                  {Number(hedge.open_basket_count) > 0 ? (
                    <>
                      It will also close{' '}
                      <span className="font-semibold">
                        {Number(hedge.open_basket_count)} open basket
                        {Number(hedge.open_basket_count) === 1 ? '' : 's'}
                      </span>{' '}
                      under this hedge and all mirrored slave positions. Short
                      strikes were sized for this hedge — they cannot stay open
                      alone.
                    </>
                  ) : (
                    <>
                      No open baskets are linked to this hedge right now. If any
                      appear before confirm completes, they will be closed too.
                    </>
                  )}
                </p>
                <p className="mt-2 text-sm font-medium text-gray-200">
                  This action cannot be undone.
                </p>
                {error && (
                  <p className="mt-3 rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                    {error}
                  </p>
                )}
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
                    {Number(hedge.open_basket_count) > 0
                      ? ` + ${Number(hedge.open_basket_count)} Basket${
                          Number(hedge.open_basket_count) === 1 ? '' : 's'
                        }`
                      : ''}
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
          editField === 'target' ? 'Edit Hedge Target ($)' : 'Edit Hedge Stop Loss ($)'
        }
        message={
          <label className="block text-left text-sm text-gray-300">
            {editField === 'target'
              ? 'Profit target in USD (must be > 0). Takes effect next monitor cycle.'
              : 'Stop loss in USD (must be > 0). Takes effect next monitor cycle.'}
            <input
              type="number"
              min={0.01}
              step={0.01}
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              className="mt-2 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            {editError && (
              <span className="mt-2 block text-xs text-red-400">{editError}</span>
            )}
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
