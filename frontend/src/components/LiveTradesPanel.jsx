import { useMemo, useState } from 'react'
import ConfirmDialog from './ui/ConfirmDialog'
import LoadingSpinner from './ui/LoadingSpinner'
import Toast from './ui/Toast'
import { useTrades } from '../hooks/useTrades'
import { closeLeg, exitTrade, updateSettings } from '../services/api'

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function fmtStrike(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function fmtSigned(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const abs = fmtMoney(Math.abs(n))
  if (n > 0) return `+$${abs}`
  if (n < 0) return `−$${abs}`
  return `$${abs}`
}

function fmtDash(v) {
  if (v == null || v === '') return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return fmtMoney(n)
}

function pnlClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'text-gray-300'
  return n > 0 ? 'text-green-400' : 'text-red-400'
}

function fmtCountdown(hours) {
  const h = Number(hours)
  if (!Number.isFinite(h) || h <= 0) return 'expired'
  const total = Math.max(0, Math.floor(h * 3600))
  const hh = Math.floor(total / 3600)
  const mm = Math.floor((total % 3600) / 60)
  const ss = total % 60
  return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
}

function validateLongForm({ target, sl, step, amt }) {
  const t = Number(target)
  const s = Number(sl)
  if (!Number.isFinite(t) || t <= 0 || !Number.isFinite(s) || s <= 0) {
    return 'target_usd and sl_usd must be > 0'
  }
  const stepEmpty = step === '' || step == null
  const amtEmpty = amt === '' || amt == null
  if (stepEmpty !== amtEmpty) {
    return 'trail_step_usd and trail_amt_usd must both be set or both empty'
  }
  if (!stepEmpty) {
    const st = Number(step)
    const am = Number(amt)
    if (!Number.isFinite(st) || !Number.isFinite(am) || !(am > 0 && am <= st)) {
      return 'require 0 < trail_amt_usd <= trail_step_usd'
    }
  }
  return null
}

function validateShortForm({ target, sl }) {
  const t = Number(target)
  const s = Number(sl)
  if (!Number.isFinite(t) || t <= 0 || !Number.isFinite(s) || s <= 0) {
    return 'profit_target_usd and stoploss_usd must be > 0'
  }
  return null
}

function LiveTradeCard({ trade, onToast }) {
  const isLong = String(trade.position_side || '').toLowerCase() === 'long'
  const call = trade.call_leg || {}
  const put = trade.put_leg || {}
  const qty = Math.abs(Number(trade.lots || call.quantity || put.quantity || 1))
  const sideVerb = isLong ? 'SELL' : 'BUY'
  const [busy, setBusy] = useState('')
  const [modifyOpen, setModifyOpen] = useState(false)
  const [confirm, setConfirm] = useState(null)
  const [form, setForm] = useState({
    target: String(trade.target_usd ?? trade.profit_target_usd ?? ''),
    sl: String(trade.sl_usd ?? trade.stoploss_usd ?? ''),
    step: trade.trail_step_usd == null ? '' : String(trade.trail_step_usd),
    amt: trade.trail_amt_usd == null ? '' : String(trade.trail_amt_usd),
  })

  const target = Number(trade.target_usd ?? trade.profit_target_usd)
  const slUsd = Number(trade.sl_usd ?? trade.stoploss_usd)
  const gross = Number(trade.gross ?? trade.gross_mtm)
  const net = Number(trade.net ?? trade.net_mtm)
  const remaining = Number(trade.target_remaining)
  const slDistance = Number(trade.sl_distance)
  const hasTrail =
    isLong && (trade.trail_step_usd != null || trade.trail_sl_level != null)
  const progress = useMemo(() => {
    if (!Number.isFinite(target) || target <= 0) {
      return 0
    }
    const n = Number.isFinite(net) ? net : 0
    return Math.max(0, Math.min(100, (n / target) * 100))
  }, [target, net])

  const run = async (key, fn, okMsg) => {
    setBusy(key)
    try {
      const res = await fn()
      onToast({ type: 'success', message: okMsg || JSON.stringify(res) })
    } catch (err) {
      onToast({ type: 'error', message: err.message || String(err) })
    } finally {
      setBusy('')
      setConfirm(null)
    }
  }

  const applyModify = async () => {
    const err = isLong ? validateLongForm(form) : validateShortForm(form)
    if (err) {
      onToast({ type: 'error', message: err })
      return
    }
    let payload
    if (isLong) {
      payload = {
        target_usd: Number(form.target),
        sl_usd: Number(form.sl),
      }
      if (form.step === '' && form.amt === '') {
        payload.trail_step_usd = null
        payload.trail_amt_usd = null
      } else {
        payload.trail_step_usd = Number(form.step)
        payload.trail_amt_usd = Number(form.amt)
      }
    } else {
      payload = {
        profit_target_usd: Number(form.target),
        stoploss_usd: Number(form.sl),
      }
    }
    await run('modify', () => updateSettings(trade.trade_id, payload), 'Settings updated')
    setModifyOpen(false)
  }

  return (
    <article className="rounded-xl border border-gray-700 bg-gray-900/70 p-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span
            className={`rounded px-2 py-0.5 text-xs font-semibold ${
              isLong ? 'bg-emerald-900 text-emerald-200' : 'bg-amber-900 text-amber-200'
            }`}
          >
            {isLong ? 'LONG' : 'SHORT'}
          </span>
          <span className="text-sm text-white">
            C {fmtStrike(call.strike)} / P {fmtStrike(put.strike)}
          </span>
          <span className="text-xs text-gray-300">{qty} lots</span>
        </div>
        <div className="text-xs text-gray-400">
          {trade.expiry_date} · {fmtCountdown(trade.hours_to_expiry)}
        </div>
      </header>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <div className="text-xs text-gray-400">Net PnL</div>
          <div className={`text-2xl font-semibold ${pnlClass(net)}`}>
            {fmtSigned(net)}
          </div>
        </div>
        <div>
          <div className="text-xs text-gray-400">Gross PnL</div>
          <div className={`text-2xl font-semibold ${pnlClass(gross)}`}>
            {fmtSigned(gross)}
          </div>
        </div>
      </div>
      <div className="mt-1 text-xs text-gray-500">
        Fees: paid ${fmtDash(trade.fees_paid)} + exit est ${fmtDash(trade.est_exit_fees)}
        {' · '}
        Slippage: entry ${fmtDash(trade.entry_slip_usd)}, abhi ≈ ${fmtDash(trade.slip_now)}{' '}
        (gross me shaamil)
      </div>

      <table className="mt-3 w-full text-left text-xs text-gray-300">
        <thead>
          <tr className="text-gray-500">
            <th className="py-1">Leg</th>
            <th>Entry</th>
            <th>Bid / Mark</th>
            <th>Chg %</th>
            <th>PnL</th>
          </tr>
        </thead>
        <tbody>
          {[
            ['Call', call],
            ['Put', put],
          ].map(([label, leg]) => (
            <tr key={label} className="border-t border-gray-800">
              <td className="py-1">{label} {fmtStrike(leg.strike)}</td>
              <td>${fmtMoney(leg.initial_premium)}</td>
              <td>
                ${fmtMoney(leg.current_bid ?? leg.current_premium)} / $
                {fmtMoney(leg.current_mark ?? leg.current_premium)}
              </td>
              <td>{Number(leg.change_pct).toFixed(2)}%</td>
              <td className={pnlClass(leg.leg_pnl)}>{fmtSigned(leg.leg_pnl)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="mt-3 space-y-2 text-xs text-gray-300">
        <div>
          Target ${fmtDash(target)} · Abhi Net {fmtSigned(net)} · Baaki{' '}
          {fmtSigned(Number.isFinite(remaining) ? remaining : target - net)}
        </div>
        <div className="h-2 overflow-hidden rounded bg-gray-800">
          <div
            className="h-full bg-emerald-500"
            style={{ width: `${progress}%` }}
          />
        </div>
        <div>
          SL −${fmtDash(slUsd)} · Abhi Gross {fmtSigned(gross)} · SL tak{' '}
          {fmtSigned(Number.isFinite(slDistance) ? slDistance : gross + slUsd)}
          {hasTrail ? ` · trail ${fmtSigned(trade.trail_sl_level)}` : ''}
        </div>
        {isLong && (
          <div className="text-gray-500">
            BE call {fmtStrike(trade.call_breakeven)} · put {fmtStrike(trade.put_breakeven)}
          </div>
        )}
      </div>

      {modifyOpen && (
        <div className="mt-3 grid gap-2 rounded-md border border-gray-700 p-3 sm:grid-cols-2">
          {(isLong
            ? [
                ['target', 'Target (Net $)'],
                ['sl', 'SL (Gross $)'],
                ['step', 'Trail step'],
                ['amt', 'Trail amount'],
              ]
            : [
                ['target', 'Target (Net $)'],
                ['sl', 'SL (Gross $)'],
              ]
          ).map(([key, label]) => (
            <label key={key} className="text-xs text-gray-400">
              {label}
              <input
                className="mt-1 w-full rounded border border-gray-600 bg-gray-950 px-2 py-1 text-white"
                value={form[key]}
                onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
              />
            </label>
          ))}
          <button
            type="button"
            disabled={Boolean(busy)}
            onClick={() =>
              setConfirm({
                title: 'Apply settings?',
                message: isLong
                  ? `target ${form.target}, SL ${form.sl}, trail ${form.step || '—'} / ${form.amt || '—'}`
                  : `profit_target_usd ${form.target}, stoploss_usd ${form.sl}`,
                onConfirm: applyModify,
              })
            }
            className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white disabled:opacity-50"
          >
            {busy === 'modify' ? 'Saving…' : 'Apply'}
          </button>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          className="rounded border border-gray-600 px-3 py-1.5 text-xs text-gray-200"
          onClick={() => setModifyOpen((v) => !v)}
        >
          Modify
        </button>
        <button
          type="button"
          disabled={Boolean(busy)}
          onClick={() =>
            setConfirm({
              title: 'Exit all?',
              message: `${sideVerb} call ${fmtStrike(call.strike)}, ${sideVerb} put ${fmtStrike(put.strike)}, qty ${qty}`,
              onConfirm: () =>
                run(
                  'exit',
                  () => exitTrade(trade.trade_id),
                  'Exit submitted',
                ),
            })
          }
          className="inline-flex items-center gap-1 rounded bg-red-700 px-3 py-1.5 text-xs text-white disabled:opacity-50"
        >
          {busy === 'exit' && <LoadingSpinner size="sm" color="white" />}
          Exit All
        </button>
        <button
          type="button"
          disabled={Boolean(busy)}
          onClick={() =>
            setConfirm({
              title: 'Close call?',
              message: `${sideVerb} call ${fmtStrike(call.strike)}, qty ${qty}`,
              onConfirm: () =>
                run('call', () => closeLeg(trade.trade_id, 'call'), 'Call close submitted'),
            })
          }
          className="inline-flex items-center gap-1 rounded border border-gray-600 px-3 py-1.5 text-xs text-gray-200 disabled:opacity-50"
        >
          {busy === 'call' && <LoadingSpinner size="sm" />}
          Close Call
        </button>
        <button
          type="button"
          disabled={Boolean(busy)}
          onClick={() =>
            setConfirm({
              title: 'Close put?',
              message: `${sideVerb} put ${fmtStrike(put.strike)}, qty ${qty}`,
              onConfirm: () =>
                run('put', () => closeLeg(trade.trade_id, 'put'), 'Put close submitted'),
            })
          }
          className="inline-flex items-center gap-1 rounded border border-gray-600 px-3 py-1.5 text-xs text-gray-200 disabled:opacity-50"
        >
          {busy === 'put' && <LoadingSpinner size="sm" />}
          Close Put
        </button>
      </div>

      <ConfirmDialog
        isOpen={Boolean(confirm)}
        title={confirm?.title}
        message={confirm?.message}
        confirmDisabled={Boolean(busy)}
        onConfirm={confirm?.onConfirm}
        onCancel={() => setConfirm(null)}
      />
    </article>
  )
}

export default function LiveTradesPanel() {
  const { trades, loading } = useTrades()
  const [toast, setToast] = useState(null)
  const live = (trades || []).filter(
    (t) => t && t.error !== 'state_unreadable' && String(t.status || 'active') === 'active',
  )

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-950/40 p-4">
      <h2 className="text-lg font-semibold text-white">Live Trades</h2>
      {loading && live.length === 0 ? (
        <div className="mt-3">
          <LoadingSpinner />
        </div>
      ) : live.length === 0 ? (
        <p className="mt-2 text-sm text-gray-500">Koi live trade nahi</p>
      ) : (
        <div className="mt-3 space-y-3">
          {live.map((trade) => (
            <LiveTradeCard
              key={trade.trade_id}
              trade={trade}
              onToast={setToast}
            />
          ))}
        </div>
      )}
      <Toast
        message={toast?.message}
        type={toast?.type}
        onClose={() => setToast(null)}
      />
    </section>
  )
}
