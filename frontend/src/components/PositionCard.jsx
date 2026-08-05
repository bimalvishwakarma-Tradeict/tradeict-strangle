import { useEffect, useMemo, useState } from 'react'
import ConfirmDialog from './ui/ConfirmDialog'
import Toast from './ui/Toast'
import LoadingSpinner from './ui/LoadingSpinner'
import EmergencyExit from './EmergencyExit'
import PayoffGraph from './PayoffGraph'
import AdjustmentSlabs from './AdjustmentSlabs'
import { closeLeg, getAdjustments, updateSettings } from '../services/api'

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function fmtPnl(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  // Delta UPNL is often sub-cent for 1 lot (contract_value 0.001)
  const digits = Math.abs(n) > 0 && Math.abs(n) < 1 ? 4 : 2
  return n.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function fmtStrike(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function fmtSignedMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '-' : ''
  return `${sign}$${fmtPnl(Math.abs(n))}`
}

function fmtPct(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}${n.toFixed(1)}%`
}

function pnlColor(v) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return 'text-gray-300'
  return n > 0 ? 'text-green-400' : 'text-red-400'
}

function triggerBarClass(pct) {
  if (pct >= 90) return 'bg-red-500 animate-pulse'
  if (pct >= 70) return 'bg-orange-500'
  if (pct >= 50) return 'bg-yellow-500'
  return 'bg-green-500'
}

function useSettlingCountdown(settlingEndsAt, isSettlingFlag) {
  const [nowTick, setNowTick] = useState(0)

  useEffect(() => {
    if (!isSettlingFlag && !settlingEndsAt) return undefined
    const id = setInterval(() => setNowTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [isSettlingFlag, settlingEndsAt])

  return useMemo(() => {
    void nowTick
    if (!settlingEndsAt) {
      return { isSettling: Boolean(isSettlingFlag), text: '', minutesLeft: 0 }
    }
    const endMs = new Date(settlingEndsAt).getTime()
    const remainingSec = Math.max(0, Math.floor((endMs - Date.now()) / 1000))
    const isSettling = remainingSec > 0
    const mins = Math.floor(remainingSec / 60)
    const secs = remainingSec % 60
    const text =
      mins > 0 ? `${mins}m ${secs.toString().padStart(2, '0')}s` : `${secs}s`
    return {
      isSettling,
      text,
      minutesLeft: mins > 0 ? mins : remainingSec > 0 ? 1 : 0,
    }
  }, [settlingEndsAt, isSettlingFlag, nowTick])
}

function useCountdown(hoursToExpiry) {
  const [nowTick, setNowTick] = useState(0)
  const [baseMs, setBaseMs] = useState(() => Date.now())
  const [baseHours, setBaseHours] = useState(() => Number(hoursToExpiry) || 0)

  useEffect(() => {
    setBaseMs(Date.now())
    setBaseHours(Number(hoursToExpiry) || 0)
  }, [hoursToExpiry])

  useEffect(() => {
    const id = setInterval(() => setNowTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  return useMemo(() => {
    void nowTick
    const elapsedH = (Date.now() - baseMs) / 3600000
    const hoursLeft = Math.max(0, baseHours - elapsedH)
    const totalSec = Math.floor(hoursLeft * 3600)
    const days = Math.floor(totalSec / 86400)
    const hrs = Math.floor((totalSec % 86400) / 3600)
    const mins = Math.floor((totalSec % 3600) / 60)
    const secs = totalSec % 60

    let text
    if (hoursLeft > 24) text = `${days}d ${hrs}h`
    else if (hoursLeft >= 1) text = `${hrs}h ${mins}m`
    else text = `${mins}m ${secs}s`

    const expiringSoon = hoursLeft > 0 && hoursLeft <= 0.25
    const underOneHour = hoursLeft > 0 && hoursLeft < 1

    return { text, hoursLeft, expiringSoon, underOneHour }
  }, [baseHours, baseMs, nowTick])
}

function normalizeLeg(trade, side) {
  const nested = side === 'call' ? trade.call_leg : trade.put_leg
  const current =
    nested?.current_premium ??
    (side === 'call' ? trade.call_premium : trade.put_premium)
  const initial =
    (side === 'call' ? trade.call_entry_premium : trade.put_entry_premium) ??
    nested?.initial_premium
  const change =
    nested?.change_pct ??
    (side === 'call' ? trade.call_change_pct : trade.put_change_pct)
  // Legs-table only — NEVER fall back to call_upnl / Delta MTM (those are NET MTM)
  const legPnl = nested?.leg_pnl
  const status = (nested?.status || 'open').toLowerCase()
  const entryFee =
    nested?.entry_fee_usd ??
    (side === 'call' ? trade.call_entry_fee : trade.put_entry_fee)
  const estExitFee =
    nested?.est_exit_fee_usd ??
    (side === 'call' ? trade.call_est_exit_fee : trade.put_est_exit_fee)

  return {
    strike:
      nested?.strike ??
      trade[`${side}_strike`] ??
      null,
    symbol:
      nested?.symbol ??
      trade[`${side}_symbol`] ??
      '',
    quantity:
      nested?.quantity ??
      trade[`${side}_quantity`] ??
      trade.quantity ??
      null,
    initial_premium: initial,
    trigger_baseline_premium:
      nested?.trigger_baseline_premium ??
      (side === 'call'
        ? trade.call_trigger_baseline
        : trade.put_trigger_baseline) ??
      initial,
    current_premium: current,
    change_pct: change,
    leg_pnl: legPnl,
    entry_fee_usd: entryFee,
    est_exit_fee_usd: estExitFee,
    status,
    closed: status === 'closed',
  }
}

function formatAdjTime(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString('en-IN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'Asia/Kolkata',
    })
  } catch {
    return iso
  }
}

function LegRow({ label, leg }) {
  return (
    <tr className={leg.closed ? 'opacity-50' : ''}>
      <td className="px-2 py-2 font-medium text-white">{label}</td>
      <td className="px-2 py-2">${fmtStrike(leg.strike)}</td>
      <td className="px-2 py-2">${fmtMoney(leg.initial_premium)}</td>
      <td className="px-2 py-2">${fmtMoney(leg.current_premium)}</td>
      <td className={`px-2 py-2 ${pnlColor(-(Number(leg.change_pct) || 0))}`}>
        {fmtPct(leg.change_pct)}
      </td>
      <td className={`px-2 py-2 font-medium ${pnlColor(leg.leg_pnl)}`}>
        {fmtSignedMoney(leg.leg_pnl)}
      </td>
      <td className="px-2 py-2 text-amber-200/90">
        {leg.entry_fee_usd != null ? `$${fmtMoney(leg.entry_fee_usd)}` : '—'}
      </td>
      <td className="px-2 py-2 text-amber-200/90">
        {leg.closed
          ? '—'
          : leg.est_exit_fee_usd != null
            ? `$${fmtMoney(leg.est_exit_fee_usd)}`
            : '—'}
      </td>
    </tr>
  )
}

function premiumBandHint(premium) {
  const px = Number(premium) || 0
  if (px >= 300) return '≥ $300'
  if (px >= 200) return '$200–$300'
  if (px >= 100) return '$100–$200'
  return '< $100'
}

function TriggerWatch({
  title,
  entry,
  baseline,
  trigger,
  current,
  distance,
  progressPct,
  triggerPct,
  triggerMode,
}) {
  const pct = Math.max(0, Math.min(120, Number(progressPct) || 0))
  const warn = pct > 70
  const danger = pct > 90
  const entryN = Number(entry) || 0
  const baselineN = Number(baseline) || 0
  const currentN = Number(current) || 0
  const showAdjBaseline =
    baselineN > 0 && Math.abs(baselineN - entryN) > 0.005
  const isPremium = triggerMode === 'premium'
  return (
    <div className="rounded-lg border border-gray-700 bg-gray-900/50 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
        {title}
      </div>
      <div className="space-y-1 text-xs text-gray-300">
        <div className="flex justify-between">
          <span>Entry (original)</span>
          <span>${fmtMoney(entryN)}</span>
        </div>
        {showAdjBaseline && (
          <div className="flex justify-between text-orange-200/90">
            <span>Price at Last Adj</span>
            <span>${fmtMoney(baselineN)}</span>
          </div>
        )}
        {isPremium && (
          <div className="flex justify-between">
            <span>Current Premium</span>
            <span>
              ${fmtMoney(currentN)}{' '}
              <span className="text-gray-500">
                ({premiumBandHint(currentN)} → {fmtMoney(triggerPct)}%)
              </span>
            </span>
          </div>
        )}
        <div className="flex justify-between">
          <span>Trigger ({fmtMoney(triggerPct)}%)</span>
          <span className="text-amber-300">${fmtMoney(trigger)}</span>
        </div>
        <div className="flex justify-between">
          <span>Offer</span>
          <span>${fmtMoney(currentN)}</span>
        </div>
        <div className="flex justify-between">
          <span>To trigger</span>
          <span className={distance > 0 ? 'text-gray-300' : 'text-red-400'}>
            {distance > 0 ? `+$${fmtMoney(distance)}` : 'TRIGGERED'}
          </span>
        </div>
      </div>
      <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-gray-700">
        <div
          className={`h-full rounded-full transition-all ${triggerBarClass(pct)}`}
          style={{ width: `${Math.min(100, pct)}%` }}
        />
      </div>
      <div
        className={`mt-1 text-xs ${
          danger
            ? 'font-semibold text-red-400'
            : warn
              ? 'text-orange-300'
              : 'text-gray-400'
        }`}
      >
        {pct.toFixed(1)}% to trigger
        {warn && !danger ? ' ⚠️' : ''}
        {danger ? ' 🔴' : ''}
      </div>
    </div>
  )
}

/**
 * Props: { trade, recentAdjustments? }
 */
export default function PositionCard({ trade, recentAdjustments = [] }) {
  const call = normalizeLeg(trade, 'call')
  const put = normalizeLeg(trade, 'put')
  const countdown = useCountdown(trade.hours_to_expiry)
  const settling = useSettlingCountdown(
    trade.settling_ends_at,
    Boolean(trade.is_settling),
  )

  // NET MTM — ONLY server TRADE_UPDATE / /active fields (never leg_pnl / offer math)
  const n = (v) => {
    const x = Number(v)
    return Number.isFinite(x) ? x : 0
  }
  const realized = n(trade.realized_pnl)
  const callUpnl = n(trade.call_upnl)
  const putUpnl = n(trade.put_upnl)
  const unrealized = n(
    trade.combined_upnl ?? trade.delta_upnl ?? trade.delta_mtm_pnl,
  )
  const feesPaid = n(trade.fees_paid)
  const estExitFees = n(trade.est_exit_fees)
  const slippagePct = n(trade.slippage_pct ?? 2)
  const slippageAmount = n(
    trade.slippage_amount ?? Math.abs(n(trade.gross_mtm)) * (slippagePct / 100),
  )
  const totalFees = n(trade.total_expected_fees ?? feesPaid + estExitFees)
  const totalDeductions = n(
    trade.total_deductions ?? totalFees + slippageAmount,
  )
  const grossMtm = n(trade.gross_mtm)
  const netMtm = n(trade.net_mtm)
  const totalMtm = grossMtm
  const lastMtmUpdate = trade.last_mtm_update || null
  const target = n(trade.profit_target_usd)
  const stoploss = n(trade.stoploss_usd)
  const initialMax = n(trade.initial_max_profit)
  const tpPctLocked = n(trade.tp_pct || 50)
  const slPctLocked = n(trade.sl_pct || 100)
  const progressPct =
    target > 0 ? Math.min(100, Math.abs((totalMtm / target) * 100)) : 0
  const progressPositive = totalMtm >= 0
  const displayPct = target > 0 ? Math.round(Math.abs((totalMtm / target) * 100)) : 0

  // Bot Monitoring Plan — entry vs trigger baseline are separate
  const triggerMode = String(trade.trigger_mode || 'slab').toLowerCase()
  const triggerPct = Number(trade.current_trigger_pct || 0)
  const callTriggerPct = Number(
    trade.call_trigger_pct ?? trade.current_trigger_pct ?? 0,
  )
  const putTriggerPct = Number(
    trade.put_trigger_pct ?? trade.current_trigger_pct ?? 0,
  )
  const callEntry = Number(trade.call_entry_premium ?? call.initial_premium ?? 0)
  const putEntry = Number(trade.put_entry_premium ?? put.initial_premium ?? 0)
  const callBaseline = Number(
    trade.call_trigger_baseline ??
      call.trigger_baseline_premium ??
      callEntry,
  )
  const putBaseline = Number(
    trade.put_trigger_baseline ??
      put.trigger_baseline_premium ??
      putEntry,
  )
  const callTrigger = Number(trade.call_trigger_price ?? 0)
  const putTrigger = Number(trade.put_trigger_price ?? 0)
  const callOfferLive = Number(call.current_premium ?? 0)
  const putOfferLive = Number(put.current_premium ?? 0)
  const callProgress =
    callTrigger > 0
      ? (callOfferLive / callTrigger) * 100
      : Number(trade.call_pct_to_trigger ?? 0)
  const putProgress =
    putTrigger > 0
      ? (putOfferLive / putTrigger) * 100
      : Number(trade.put_pct_to_trigger ?? 0)
  const callDistance = callTrigger > 0 ? callTrigger - callOfferLive : 0
  const putDistance = putTrigger > 0 ? putTrigger - putOfferLive : 0

  const callRepl = trade.estimated_call_replacement
  const putRepl = trade.estimated_put_replacement

  const [confirmLeg, setConfirmLeg] = useState(null)
  const [closingLeg, setClosingLeg] = useState(null)
  const [toast, setToast] = useState(null)
  const [adjHistory, setAdjHistory] = useState([])
  const [showHistory, setShowHistory] = useState(false)
  const [editField, setEditField] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [savingEdit, setSavingEdit] = useState(false)
  const [triggerDraft, setTriggerDraft] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function loadAdj() {
      try {
        const rows = await getAdjustments(trade.trade_id)
        if (cancelled) return
        const list = Array.isArray(rows) ? rows : rows?.adjustments || []
        setAdjHistory(list)
      } catch {
        if (!cancelled) setAdjHistory([])
      }
    }
    loadAdj()
    return () => {
      cancelled = true
    }
  }, [trade.trade_id, trade.adjustment_count, trade.last_adjustment])

  const mergedAdj = useMemo(() => {
    const fromWs = (recentAdjustments || [])
      .filter((a) => a.trade_id === trade.trade_id)
      .map((a) => ({
        timestamp: a.timestamp,
        leg_type: a.leg_type,
        old_strike: a.old_strike,
        new_strike: a.new_strike,
        trigger_pct_reached: a.trigger_pct,
      }))
    const combined = [...fromWs, ...adjHistory]
    const seen = new Set()
    const unique = []
    for (const row of combined) {
      const key = `${row.timestamp}-${row.leg_type}-${row.old_strike}-${row.new_strike}`
      if (seen.has(key)) continue
      seen.add(key)
      unique.push(row)
    }
    return unique
  }, [recentAdjustments, adjHistory, trade.trade_id])

  const lastAdj = trade.last_adjustment || mergedAdj[0] || null

  const expiryLabel = trade.expiry_label
    ? `${trade.expiry_date || ''} (${trade.expiry_label})`.replace(/^\s/, '')
    : trade.expiry_date || '—'

  const handleConfirmClose = async () => {
    if (!confirmLeg) return
    const leg = confirmLeg
    setConfirmLeg(null)
    setClosingLeg(leg)
    try {
      const result = await closeLeg(trade.trade_id, leg)
      const remaining = result?.open_legs_remaining
      setToast({
        type: 'success',
        message: result?.basket_closed
          ? `${leg === 'call' ? 'Call' : 'Put'} closed — basket finished`
          : `${leg === 'call' ? 'Call' : 'Put'} closed — ${remaining ?? 1} leg still open`,
      })
    } catch (err) {
      setToast({
        type: 'error',
        message: `Failed: ${err.message || 'unknown error'}`,
      })
    } finally {
      setClosingLeg(null)
    }
  }

  const openEdit = (field) => {
    if (field === 'target') setEditValue(String(tpPctLocked || 50))
    else if (field === 'sl') setEditValue(String(slPctLocked || 100))
    else if (field === 'slippage') setEditValue(String(slippagePct || 2))
    else if (field === 'trigger') {
      setEditValue(String(triggerPct || ''))
      setTriggerDraft({
        mode: triggerMode || 'slab',
        flat_pct: triggerPct || 150,
        slab_24h: 200,
        slab_12h: 175,
        slab_6h: 150,
        slab_lt6h: 150,
        premium_slab_300: Number(trade.premium_slab_300 ?? 150),
        premium_slab_200: Number(trade.premium_slab_200 ?? 160),
        premium_slab_100: Number(trade.premium_slab_100 ?? 180),
        premium_slab_lt100: Number(trade.premium_slab_lt100 ?? 200),
      })
    }
    setEditField(field)
  }

  const saveEdit = async () => {
    if (!editField) return
    setSavingEdit(true)
    try {
      const payload = {}
      if (editField === 'target') {
        const val = Number(editValue)
        if (!Number.isFinite(val) || val <= 0) {
          setToast({ type: 'error', message: 'Enter a valid number' })
          setSavingEdit(false)
          return
        }
        payload.tp_pct = val
      } else if (editField === 'sl') {
        const val = Number(editValue)
        if (!Number.isFinite(val) || val <= 0) {
          setToast({ type: 'error', message: 'Enter a valid number' })
          setSavingEdit(false)
          return
        }
        payload.sl_pct = val
      } else if (editField === 'slippage') {
        const val = Number(editValue)
        if (!Number.isFinite(val) || val < 0 || val > 10) {
          setToast({ type: 'error', message: 'Slippage must be 0–10%' })
          setSavingEdit(false)
          return
        }
        payload.slippage_pct = val
      } else if (editField === 'trigger') {
        const d = triggerDraft
        if (!d) {
          setToast({ type: 'error', message: 'No trigger settings' })
          setSavingEdit(false)
          return
        }
        payload.trigger_mode = d.mode
        if (d.mode === 'flat') {
          if (!Number.isFinite(d.flat_pct) || d.flat_pct < 1) {
            setToast({ type: 'error', message: 'Enter a valid flat %' })
            setSavingEdit(false)
            return
          }
          payload.flat_trigger_pct = d.flat_pct
        } else if (d.mode === 'premium') {
          payload.premium_slab_300 = d.premium_slab_300
          payload.premium_slab_200 = d.premium_slab_200
          payload.premium_slab_100 = d.premium_slab_100
          payload.premium_slab_lt100 = d.premium_slab_lt100
        } else {
          payload.slab_24h = d.slab_24h
          payload.slab_12h = d.slab_12h
          payload.slab_6h = d.slab_6h
          payload.slab_lt6h = d.slab_lt6h
        }
      }
      await updateSettings(trade.trade_id, payload)
      setToast({ type: 'success', message: 'Settings updated' })
      setEditField(null)
      setTriggerDraft(null)
    } catch (err) {
      setToast({ type: 'error', message: err.message || 'Update failed' })
    } finally {
      setSavingEdit(false)
    }
  }

  const anyOpen = !call.closed || !put.closed
  const basketNo = trade.basket_number ?? trade.trade_id
  const legHistory = Array.isArray(trade.leg_history) ? trade.leg_history : []
  const closedHistory = legHistory.filter(
    (row) => String(row.status || '').toLowerCase() === 'closed',
  )

  return (
    <article className="overflow-hidden rounded-xl border border-gray-700 bg-gray-800 shadow-lg">
      {/* Header */}
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-700 bg-gray-900/60 px-4 py-3 text-sm">
        <div className="font-semibold text-white">
          🟠 Basket #{basketNo} · {trade.underlying || '—'} Short Strangle
          {trade.open_leg_count === 1 && (
            <span className="ml-2 rounded bg-amber-900/60 px-2 py-0.5 text-xs font-normal text-amber-200">
              1 leg open
            </span>
          )}
        </div>
        <div className="text-gray-300">Exp: {expiryLabel}</div>
        <div
          className={
            countdown.expiringSoon
              ? 'animate-pulse font-semibold text-red-400'
              : countdown.underOneHour
                ? 'font-medium text-red-400'
                : 'text-gray-300'
          }
        >
          {countdown.expiringSoon
            ? `🔴 ${countdown.text} EXPIRING SOON`
            : `⏱ ${countdown.text}`}
        </div>
      </header>

      {settling.isSettling && (
        <div className="border-b border-amber-800/60 bg-amber-950/40 px-4 py-2 text-sm text-amber-200">
          Settling… P&L checks start in {settling.text}
        </div>
      )}

      {/* Legs table */}
      <div className="overflow-x-auto px-2 py-2">
        <table className="min-w-full text-left text-xs text-gray-200 sm:text-sm">
          <thead className="text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-2 py-2">Leg</th>
              <th className="px-2 py-2">Strike</th>
              <th className="px-2 py-2">Entry $</th>
              <th className="px-2 py-2">Offer $</th>
              <th className="px-2 py-2">Change</th>
              <th className="px-2 py-2">Leg P&L*</th>
              <th className="px-2 py-2">Entry Fee</th>
              <th className="px-2 py-2">Est Exit Fee</th>
            </tr>
          </thead>
          <tbody>
            <LegRow label="📞 CALL" leg={call} />
            <LegRow label="📉 PUT" leg={put} />
          </tbody>
        </table>
        <div className="px-2 pb-1 text-[10px] text-gray-500">
          * Leg P&L = live offer estimate only. Official UPL is in NET MTM below
          (updates each bot cycle).
        </div>
      </div>

      {closedHistory.length > 0 && (
        <div className="border-t border-gray-700 px-4 py-3">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
            Closed legs in this basket
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-xs text-gray-300">
              <thead className="text-[10px] uppercase text-gray-500">
                <tr>
                  <th className="px-1 py-1">Type</th>
                  <th className="px-1 py-1">Strike</th>
                  <th className="px-1 py-1">Qty</th>
                  <th className="px-1 py-1">Entry</th>
                  <th className="px-1 py-1">Exit</th>
                  <th className="px-1 py-1">Entry Fee</th>
                  <th className="px-1 py-1">Exit Fee</th>
                  <th className="px-1 py-1">Time</th>
                  <th className="px-1 py-1">Realized</th>
                </tr>
              </thead>
              <tbody>
                {closedHistory.map((row) => (
                  <tr key={row.id} className="border-t border-gray-800">
                    <td className="px-1 py-1 uppercase">{row.leg_type}</td>
                    <td className="px-1 py-1">${fmtStrike(row.strike)}</td>
                    <td className="px-1 py-1">{row.quantity}</td>
                    <td className="px-1 py-1">${fmtMoney(row.entry_premium)}</td>
                    <td className="px-1 py-1">${fmtMoney(row.exit_premium)}</td>
                    <td className="px-1 py-1 text-amber-200/80">
                      {row.entry_fee_usd != null
                        ? `$${fmtMoney(row.entry_fee_usd)}`
                        : '—'}
                    </td>
                    <td className="px-1 py-1 text-amber-200/80">
                      {row.exit_fee_usd != null
                        ? `$${fmtMoney(row.exit_fee_usd)}`
                        : '—'}
                    </td>
                    <td className="px-1 py-1">{formatAdjTime(row.exit_time)}</td>
                    <td className={`px-1 py-1 ${pnlColor(row.realized_pnl)}`}>
                      {fmtSignedMoney(row.realized_pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* NET MTM */}
      <div
        className={`space-y-2 border-t border-gray-700 px-4 py-3 ${
          settling.isSettling ? 'opacity-50' : ''
        }`}
      >
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Net MTM P&L (gross matches Delta UPL @offer; fees separate)
        </div>
        {lastMtmUpdate && (
          <div className="text-[11px] text-gray-500">
            MTM Updated: {lastMtmUpdate}
          </div>
        )}
        <div className="space-y-1 rounded-lg border border-gray-700 bg-gray-900/40 px-3 py-2 text-sm text-gray-300">
          <div className="flex justify-between">
            <span>CALL UPL</span>
            <span className={pnlColor(callUpnl)}>{fmtSignedMoney(callUpnl)}</span>
          </div>
          <div className="flex justify-between">
            <span>PUT UPL</span>
            <span className={pnlColor(putUpnl)}>{fmtSignedMoney(putUpnl)}</span>
          </div>
          <div className="flex justify-between border-t border-gray-700 pt-1">
            <span>Combined UPNL</span>
            <span className={pnlColor(unrealized)}>
              {fmtSignedMoney(unrealized)}
            </span>
          </div>
          <div className="flex justify-between">
            <span>Realized P&L</span>
            <span className={pnlColor(realized)}>{fmtSignedMoney(realized)}</span>
          </div>
          <div className="flex justify-between border-t border-gray-700 pt-1 font-medium text-white">
            <span>Gross MTM</span>
            <span className={pnlColor(grossMtm)}>{fmtSignedMoney(grossMtm)}</span>
          </div>
          <div className="flex justify-between text-amber-200/90">
            <span>Fees Paid</span>
            <span>-${fmtMoney(feesPaid)}</span>
          </div>
          <div className="flex justify-between text-amber-200/90">
            <span>Est. Exit Fees</span>
            <span>-${fmtMoney(estExitFees)}</span>
          </div>
          <div className="flex justify-between text-amber-200/90">
            <span>Slippage ({fmtMoney(slippagePct)}%)</span>
            <span>-${fmtMoney(slippageAmount)}</span>
          </div>
          <div className="flex justify-between text-amber-100">
            <span>Total Deductions</span>
            <span>-${fmtMoney(totalDeductions)}</span>
          </div>
          <div className="flex justify-between border-t border-gray-600 pt-1 text-base font-semibold text-white">
            <span>NET MTM</span>
            <span className={pnlColor(netMtm)}>{fmtSignedMoney(netMtm)}</span>
          </div>
        </div>
        <div className="flex items-center gap-2 text-lg font-bold">
          <span className="text-gray-300">GROSS:</span>
          <span className={pnlColor(grossMtm)}>{fmtSignedMoney(grossMtm)}</span>
          <span className="text-xs font-normal text-gray-500">← Delta Total UPNL</span>
        </div>
        <div className="h-2.5 w-full overflow-hidden rounded-full bg-gray-700">
          <div
            className={`h-full rounded-full transition-all ${
              progressPositive ? 'bg-green-500' : 'bg-red-500'
            }`}
            style={{ width: `${progressPct}%` }}
          />
        </div>
        <div className="flex flex-wrap justify-between gap-2 text-xs text-gray-400">
          <span>
            Target: ${fmtMoney(target)}{' '}
            <span className="text-gray-300">
              [{fmtMoney(tpPctLocked)}% of ${fmtMoney(initialMax)} max]
            </span>{' '}
            <span className="text-gray-500">[{displayPct}% reached]</span>
          </span>
          <span>
            Stop Loss: ${fmtMoney(stoploss)}{' '}
            <span className="text-gray-300">
              [{fmtMoney(slPctLocked)}% of ${fmtMoney(initialMax)} max]
            </span>
          </span>
        </div>
      </div>

      {/* Live payoff — same as Trade Initiator (hourly slider) */}
      {anyOpen && call.strike != null && put.strike != null && (
        <div className="border-t border-gray-700 px-4 py-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold text-white">
              Live Payoff Graph
            </div>
            <div className="text-xs text-gray-400">
              BTC{' '}
              <span className="font-medium text-orange-300">
                $
                {Number(trade.underlying_price || 0).toLocaleString('en-US', {
                  maximumFractionDigits: 0,
                }) || '—'}
              </span>
              <span className="text-gray-600"> · tick updates</span>
            </div>
          </div>
          <PayoffGraph
            callStrike={Number(call.strike)}
            putStrike={Number(put.strike)}
            callPremium={Number(call.initial_premium)}
            putPremium={Number(put.initial_premium)}
            quantity={Number(
              call.quantity ??
                put.quantity ??
                trade.call_quantity ??
                trade.put_quantity ??
                1,
            )}
            currentPrice={
              Number(trade.underlying_price) > 0
                ? Number(trade.underlying_price)
                : undefined
            }
            expiryDate={trade.expiry_date || undefined}
            initialHoursRemaining={Number(trade.hours_to_expiry) || undefined}
            emptyMessage="Waiting for BTC price…"
            compact
          />
        </div>
      )}

      {/* Bot plan */}
      <div className="border-t border-gray-700 px-4 py-3">
        <div className="mb-3 text-sm font-semibold text-white">
          🤖 Bot Monitoring Plan
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <TriggerWatch
            title="Call Leg Watch"
            entry={callEntry}
            baseline={callBaseline}
            trigger={callTrigger}
            current={call.current_premium}
            distance={callDistance}
            progressPct={callProgress}
            triggerPct={callTriggerPct}
            triggerMode={triggerMode}
          />
          <TriggerWatch
            title="Put Leg Watch"
            entry={putEntry}
            baseline={putBaseline}
            trigger={putTrigger}
            current={put.current_premium}
            distance={putDistance}
            progressPct={putProgress}
            triggerPct={putTriggerPct}
            triggerMode={triggerMode}
          />
        </div>

        <div className="mt-3 space-y-2 rounded-lg border border-gray-700 bg-gray-900/40 p-3 text-xs text-gray-300">
          <div>
            <span className="font-semibold text-gray-200">If CALL triggers:</span>
            <div className="mt-0.5 text-gray-400">
              Bot will buy back CALL @ ~${fmtMoney(callTrigger)}
            </div>
            <div className="text-gray-400">
              Sell new CALL near PUT premium (~${fmtMoney(put.current_premium)})
            </div>
            {callRepl ? (
              <div className="text-green-300/90">
                Nearest match: {callRepl.symbol} @ ${fmtMoney(callRepl.premium)}{' '}
                (est.)
              </div>
            ) : (
              <div className="text-gray-500">Nearest match: estimating…</div>
            )}
          </div>
          <div className="border-t border-gray-700 pt-2">
            <span className="font-semibold text-gray-200">If PUT triggers:</span>
            <div className="mt-0.5 text-gray-400">
              Bot will buy back PUT @ ~${fmtMoney(putTrigger)}
            </div>
            <div className="text-gray-400">
              Sell new PUT near CALL premium (~${fmtMoney(call.current_premium)})
            </div>
            {putRepl ? (
              <div className="text-green-300/90">
                Nearest match: {putRepl.symbol} @ ${fmtMoney(putRepl.premium)}{' '}
                (est.)
              </div>
            ) : (
              <div className="text-gray-500">Nearest match: estimating…</div>
            )}
          </div>
        </div>
      </div>

      {/* Adjustment history */}
      <div className="border-t border-gray-700 px-4 py-2 text-xs text-gray-400">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            Last adj:{' '}
            {lastAdj ? (
              <span className="text-gray-300">
                {formatAdjTime(lastAdj.timestamp)} —{' '}
                {(lastAdj.leg_type || '').toUpperCase()} $
                {fmtStrike(lastAdj.old_strike)} → ${fmtStrike(lastAdj.new_strike)}
              </span>
            ) : (
              <span>— (none yet)</span>
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            className="rounded border border-gray-600 px-2 py-1 text-gray-300 hover:bg-gray-700"
          >
            {showHistory ? 'Hide History' : 'View Full History'}
          </button>
        </div>
        {showHistory && (
          <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto">
            {mergedAdj.length === 0 && <li>— no adjustments —</li>}
            {mergedAdj.map((row, i) => (
              <li key={`${row.timestamp}-${i}`}>
                • {formatAdjTime(row.timestamp)} —{' '}
                {(row.leg_type || '').toUpperCase()} ${fmtStrike(row.old_strike)} → $
                {fmtStrike(row.new_strike)}
                {row.trigger_pct_reached != null
                  ? ` (trigger: ${Number(row.trigger_pct_reached).toFixed(0)}%)`
                  : ''}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Controls */}
      <div className="space-y-2 border-t border-gray-700 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={call.closed || closingLeg === 'call'}
              onClick={() => setConfirmLeg('call')}
              className="inline-flex items-center gap-1 rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {closingLeg === 'call' && <LoadingSpinner size="sm" />}
              Close Call
            </button>
            <button
              type="button"
              disabled={put.closed || closingLeg === 'put'}
              onClick={() => setConfirmLeg('put')}
              className="inline-flex items-center gap-1 rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {closingLeg === 'put' && <LoadingSpinner size="sm" />}
              Close Put
            </button>
          </div>
          {anyOpen && (
            <EmergencyExit
              tradeId={trade.trade_id}
              finalMtmHint={totalMtm}
              onSuccess={() =>
                setToast({
                  type: 'success',
                  message: 'Trade closed via emergency exit',
                })
              }
            />
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => openEdit('target')}
            className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700"
          >
            Edit Target %
          </button>
          <button
            type="button"
            onClick={() => openEdit('sl')}
            className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700"
          >
            Edit SL %
          </button>
          <button
            type="button"
            onClick={() => openEdit('slippage')}
            className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700"
          >
            Edit Slippage %
          </button>
          <button
            type="button"
            onClick={() => openEdit('trigger')}
            className="rounded-md border border-gray-600 px-3 py-1.5 text-xs text-gray-300 hover:bg-gray-700"
          >
            Edit Trigger
          </button>
        </div>
      </div>

      <ConfirmDialog
        isOpen={Boolean(confirmLeg)}
        title={`Close ${confirmLeg === 'call' ? 'CALL' : 'PUT'} leg?`}
        message={
          confirmLeg === 'call'
            ? `Close CALL leg at $${fmtStrike(call.strike)} strike at market price?`
            : `Close PUT leg at $${fmtStrike(put.strike)} strike at market price?`
        }
        confirmLabel="Close at Market"
        onCancel={() => setConfirmLeg(null)}
        onConfirm={handleConfirmClose}
      />

      <ConfirmDialog
        isOpen={Boolean(editField)}
        title={
          editField === 'target'
            ? 'Edit Profit Target %'
            : editField === 'sl'
              ? 'Edit Stop Loss %'
              : editField === 'slippage'
                ? 'Edit Slippage %'
                : 'Edit Trigger Settings'
        }
        message={
          editField === 'trigger' ? (
            <div className="text-left">
              <AdjustmentSlabs
                compact
                defaultMode={triggerMode}
                initialValues={triggerDraft}
                onChange={setTriggerDraft}
              />
            </div>
          ) : (
            <label className="block text-left text-sm text-gray-300">
              {editField === 'target' || editField === 'sl'
                ? `${editField === 'target' ? 'Target' : 'Stop Loss'} % of initial max ($${fmtMoney(initialMax)})`
                : 'Slippage % of |Gross MTM| (0–10)'}
              <input
                type="number"
                min={0}
                max={editField === 'slippage' ? 10 : undefined}
                step={editField === 'slippage' ? 0.1 : 1}
                value={editValue}
                onChange={(e) => setEditValue(e.target.value)}
                className="mt-2 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
              />
              {(editField === 'target' || editField === 'sl') && (
                <span className="mt-2 block text-xs text-gray-400">
                  = $
                  {fmtMoney(
                    initialMax > 0 && Number(editValue) > 0
                      ? (initialMax * Number(editValue)) / 100
                      : 0,
                  )}{' '}
                  ({fmtMoney(Number(editValue) || 0)}% of $
                  {fmtMoney(initialMax)} initial max)
                </span>
              )}
              {editField === 'slippage' && (
                <span className="mt-2 block text-xs text-gray-400">
                  = $
                  {fmtMoney(
                    (Math.abs(grossMtm) * (Number(editValue) || 0)) / 100,
                  )}{' '}
                  on current ${fmtMoney(grossMtm)} gross MTM
                </span>
              )}
            </label>
          )
        }
        confirmLabel={savingEdit ? 'Saving…' : 'Save'}
        confirmDisabled={savingEdit}
        onCancel={() => {
          setEditField(null)
          setTriggerDraft(null)
        }}
        onConfirm={saveEdit}
      />

      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          onClose={() => setToast(null)}
        />
      )}
    </article>
  )
}
