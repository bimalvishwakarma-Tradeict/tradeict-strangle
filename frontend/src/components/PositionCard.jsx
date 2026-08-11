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
    entry_time: nested?.entry_time ?? null,
    exit_time: nested?.exit_time ?? null,
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
  deltaSlPrice,
  universalSlPct,
}) {
  const pct = Math.max(0, Math.min(120, Number(progressPct) || 0))
  const warn = pct > 70
  const danger = pct > 90
  const entryN = Number(entry) || 0
  const baselineN = Number(baseline) || 0
  const currentN = Number(current) || 0
  const deltaSlN = Number(deltaSlPrice) || 0
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
        <div className="flex justify-between text-red-300/90">
          <span title="Attached to Delta position — no separate stop order">
            🔒 Bracket SL
            {universalSlPct != null
              ? ` (${fmtMoney(universalSlPct)}%)`
              : ''}
          </span>
          <span className="font-medium">
            {deltaSlN > 0 ? `$${fmtMoney(deltaSlN)}` : '—'}
          </span>
        </div>
        {deltaSlN > 0 && (
          <div className="text-[10px] text-gray-500">
            auto-cancels on close
          </div>
        )}
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

const NEXT_ACTION_BADGE = {
  HOLD: {
    label: 'Monitoring — No Action Needed',
    className: 'bg-green-900/50 text-green-300 border-green-700',
  },
  ADJUST_CALL: {
    label: 'Call Adjustment Expected',
    className: 'bg-orange-900/50 text-orange-300 border-orange-700',
  },
  ADJUST_PUT: {
    label: 'Put Adjustment Expected',
    className: 'bg-orange-900/50 text-orange-300 border-orange-700',
  },
  CONVERSION_LIKELY: {
    label: 'Conversion Mode Likely on Next Trigger',
    className: 'bg-red-900/50 text-red-300 border-red-700',
  },
  CONVERSION_ACTIVE: {
    label: 'Conversion Mode Active',
    className: 'bg-purple-900/50 text-purple-300 border-purple-700',
  },
  REVERSAL_WATCH: {
    label: 'Watching for Hedge Reversal',
    className: 'bg-blue-900/50 text-blue-300 border-blue-700',
  },
  PROFIT_TARGET_NEAR: {
    label: 'Near Profit Target',
    className: 'bg-green-900/50 text-green-300 border-green-700',
  },
  STOPLOSS_NEAR: {
    label: 'Near Stop Loss — Warning',
    className: 'bg-red-900/50 text-red-300 border-red-700',
  },
}

function BotNextActionPlan({ trade, call, put }) {
  const [expanded, setExpanded] = useState(true)
  const plan = trade.next_action_plan || {}
  const action = String(trade.bot_next_action || plan.next_action || 'HOLD')
  const badge = NEXT_ACTION_BADGE[action] || NEXT_ACTION_BADGE.HOLD
  const exitWatch = plan.exit_conditions_watch || {}
  const callTrigger = Number(trade.call_trigger_price || 0)
  const putTrigger = Number(trade.put_trigger_price || 0)
  const callOffer = Number(call.current_premium ?? trade.call_premium ?? 0)
  const putOffer = Number(put.current_premium ?? trade.put_premium ?? 0)
  const callPct = Number(
    trade.bot_call_pct_to_trigger ?? plan.call_pct_to_trigger ?? 0,
  )
  const putPct = Number(
    trade.bot_put_pct_to_trigger ?? plan.put_pct_to_trigger ?? 0,
  )
  const callBar = Math.min(100, Math.max(0, callPct))
  const putBar = Math.min(100, Math.max(0, putPct))
  const tpUsd = Number(exitWatch.profit_target_usd ?? trade.profit_target_usd ?? 0)
  const slUsd = Number(exitWatch.stoploss_usd ?? trade.stoploss_usd ?? 0)
  const netNow = Number(exitWatch.current_net_mtm ?? trade.net_mtm ?? 0)
  const grossSl = Number(
    exitWatch.current_gross_for_sl ?? trade.gross_mtm_for_stoploss ?? 0,
  )
  const pctTp = Number(exitWatch.pct_to_profit_target ?? 0)
  const pctSl = Number(exitWatch.pct_to_stoploss ?? 0)
  const convMin = Number(plan.conversion_min_premium ?? 150)
  const otherOffer = Number(plan.other_leg_current_offer ?? 0)

  return (
    <div className="border-t border-gray-700 px-4 py-3">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="mb-2 flex w-full items-center justify-between text-left"
      >
        <div className="text-sm font-semibold text-white">
          🤖 Bot&apos;s Next Action Plan
        </div>
        <span className="text-xs text-gray-400">
          {expanded ? 'Collapse ▲' : 'Expand ▼'}
        </span>
      </button>
      {!expanded ? null : (
        <div className="space-y-3">
          <div
            className={`inline-flex rounded-full border px-3 py-1 text-xs font-semibold ${badge.className}`}
          >
            {badge.label}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="mb-1 flex justify-between text-xs text-gray-400">
                <span>CALL → trigger</span>
                <span className={callPct >= 80 ? 'text-red-400' : 'text-gray-300'}>
                  {callPct.toFixed(1)}%
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-gray-700">
                <div
                  className={`h-full ${callPct >= 80 ? 'bg-red-500' : 'bg-green-500'}`}
                  style={{ width: `${callBar}%` }}
                />
              </div>
              <div className="mt-0.5 text-[11px] text-gray-500">
                ${fmtMoney(callOffer)} / ${fmtMoney(callTrigger)}
              </div>
            </div>
            <div>
              <div className="mb-1 flex justify-between text-xs text-gray-400">
                <span>PUT → trigger</span>
                <span className={putPct >= 80 ? 'text-red-400' : 'text-gray-300'}>
                  {putPct.toFixed(1)}%
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-gray-700">
                <div
                  className={`h-full ${putPct >= 80 ? 'bg-red-500' : 'bg-green-500'}`}
                  style={{ width: `${putBar}%` }}
                />
              </div>
              <div className="mt-0.5 text-[11px] text-gray-500">
                ${fmtMoney(putOffer)} / ${fmtMoney(putTrigger)}
              </div>
            </div>
          </div>

          {action === 'HOLD' && (
            <div className="space-y-1 rounded-lg border border-gray-700 bg-gray-900/40 px-3 py-2 text-xs text-gray-300">
              <div className="flex justify-between">
                <span>Call leg</span>
                <span>
                  current ${fmtMoney(callOffer)} → trigger ${fmtMoney(callTrigger)}{' '}
                  ({Math.max(0, 100 - callPct).toFixed(1)}% away)
                </span>
              </div>
              <div className="flex justify-between">
                <span>Put leg</span>
                <span>
                  current ${fmtMoney(putOffer)} → trigger ${fmtMoney(putTrigger)}{' '}
                  ({Math.max(0, 100 - putPct).toFixed(1)}% away)
                </span>
              </div>
              <div className="flex justify-between">
                <span>Profit target</span>
                <span>
                  Net MTM {fmtSignedMoney(netNow)} → Target ${fmtMoney(tpUsd)} (
                  {pctTp.toFixed(1)}% reached)
                </span>
              </div>
              <div className="flex justify-between">
                <span>Stop loss</span>
                <span>
                  Gross for SL {fmtSignedMoney(grossSl)} → SL at -$
                  {fmtMoney(slUsd)} ({pctSl.toFixed(1)}% used)
                </span>
              </div>
            </div>
          )}

          {(action === 'ADJUST_CALL' || action === 'ADJUST_PUT') && (
            <div className="space-y-1 rounded-lg border border-orange-800/50 bg-orange-950/20 px-3 py-2 text-xs text-gray-300">
              <div>
                Triggered leg:{' '}
                <span className="font-semibold uppercase text-orange-300">
                  {plan.triggered_leg || trade.bot_closer_leg || '—'}
                </span>{' '}
                @ current $
                {fmtMoney(
                  (plan.triggered_leg || trade.bot_closer_leg) === 'put'
                    ? putOffer
                    : callOffer,
                )}{' '}
                (trigger was $
                {fmtMoney(
                  (plan.triggered_leg || trade.bot_closer_leg) === 'put'
                    ? putTrigger
                    : callTrigger,
                )}
                )
              </div>
              <div>
                Other leg current offer: ${fmtMoney(otherOffer)}
              </div>
              <div>
                Expected new strike:{' '}
                {plan.estimated_new_strike == null ||
                plan.estimated_new_strike === 'calculating...'
                  ? 'Calculating...'
                  : `${fmtStrike(plan.estimated_new_strike)} @ ~$${fmtMoney(plan.estimated_new_premium)}`}
              </div>
              <div>
                Adjustment type:{' '}
                <span className="text-green-300">Normal Roll</span>
              </div>
              <div className="text-gray-400">
                Other leg ${fmtMoney(otherOffer)} is{' '}
                {otherOffer >= convMin ? 'above' : 'below'} conversion minimum $
                {fmtMoney(convMin)}
              </div>
            </div>
          )}

          {action === 'CONVERSION_LIKELY' && (
            <div className="space-y-3 rounded-lg border border-red-700/50 bg-red-950/20 px-3 py-3 text-xs text-gray-300">
              <div className="text-sm font-semibold text-red-300">
                ⚠️ Conversion Mode Will Activate on Trigger
              </div>
              {(() => {
                const cp = plan.conversion_plan
                if (!cp) {
                  return (
                    <div className="text-gray-500">
                      Conversion plan details: --
                    </div>
                  )
                }
                const trig = String(cp.triggered_leg || 'call').toUpperCase()
                const otherSide = String(
                  cp.new_other_leg_side || cp.close_other_leg || 'put',
                ).toUpperCase()
                const hedgeSide = String(cp.hedge_action || 'BUY')
                  .replace('BUY ', '')
                  .trim() || trig
                return (
                  <div className="space-y-2">
                    <div className="rounded border border-amber-700/40 bg-amber-950/30 px-2.5 py-2">
                      <div className="mb-0.5 font-semibold text-amber-300">
                        STEP 1 — Buy Hedge
                      </div>
                      <div className="text-amber-100">
                        BUY {hedgeSide} @ strike $
                        {fmtStrike(cp.hedge_strike)}
                      </div>
                      <div className="text-[11px] text-amber-200/70">
                        One strike toward ATM from triggered {trig} @ $
                        {fmtStrike(cp.triggered_strike)}
                        {cp.hedge_symbol_expected
                          ? ` · ${cp.hedge_symbol_expected}`
                          : ''}
                      </div>
                    </div>
                    <div className="rounded border border-blue-700/40 bg-blue-950/30 px-2.5 py-2">
                      <div className="mb-0.5 font-semibold text-blue-300">
                        STEP 2 — Keep Triggered Short + Close Other
                      </div>
                      <div className="text-blue-200">
                        KEEP short {trig} @ $
                        {fmtStrike(cp.triggered_strike)} (stays open)
                      </div>
                      <div className="text-red-300">
                        CLOSE short {otherSide} @ $
                        {fmtStrike(cp.other_leg_strike)} (current offer: $
                        {fmtMoney(cp.other_leg_current_offer)})
                      </div>
                    </div>
                    <div className="rounded border border-green-700/40 bg-green-950/30 px-2.5 py-2">
                      <div className="mb-0.5 font-semibold text-green-300">
                        STEP 3 — Sell New {otherSide}
                      </div>
                      <div className="text-green-200">
                        SELL new {otherSide} targeting ~$
                        {fmtMoney(cp.new_other_target_premium)}
                      </div>
                      <div className="text-[11px] text-green-200/70">
                        (estimated: triggered offer $
                        {fmtMoney(cp.triggered_current_offer)} / 2 — actual will
                        be hedge fill / 2)
                      </div>
                    </div>
                    <div className="text-gray-300">
                      Why conversion: {cp.why_conversion || '--'}
                    </div>
                    <div className="text-[11px] text-gray-500">
                      After conversion: bot monitors for premium equality to
                      close hedge
                    </div>
                  </div>
                )
              })()}
            </div>
          )}

          {(action === 'CONVERSION_ACTIVE' || action === 'REVERSAL_WATCH') && (
            <div className="space-y-2 rounded-lg border border-purple-700/50 bg-purple-950/20 px-3 py-2 text-xs text-gray-300">
              <div>
                Hedge: {plan.hedge_symbol || trade.conversion_hedge_symbol || '—'}{' '}
                bought at $
                {fmtMoney(
                  plan.hedge_entry_price ?? trade.conversion_hedge_entry_price,
                )}
              </div>
              <div>
                Short Call: ${fmtMoney(plan.short_call_premium ?? callOffer)} | Short
                Put: ${fmtMoney(plan.short_put_premium ?? putOffer)}
              </div>
              <div>
                Premium difference: {Number(plan.premium_equality_pct || 0).toFixed(1)}
                % (target: ≤ {Number(plan.equality_threshold_pct || 10).toFixed(1)}%)
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-gray-700">
                <div
                  className={`h-full ${
                    Number(plan.premium_equality_pct || 0) <=
                    Number(plan.equality_threshold_pct || 10)
                      ? 'bg-green-500'
                      : 'bg-yellow-500'
                  }`}
                  style={{
                    width: `${Math.min(
                      100,
                      Math.max(
                        5,
                        (Number(plan.equality_threshold_pct || 10) /
                          Math.max(Number(plan.premium_equality_pct) || 0.01, 0.01)) *
                          100,
                      ),
                    )}%`,
                  }}
                />
              </div>
              <div className="text-blue-300">
                {plan.reversal_condition ||
                  `Reversal will trigger when: |call - put| / avg ≤ ${Number(plan.equality_threshold_pct || 10).toFixed(1)}%`}
              </div>
            </div>
          )}

          <div className="rounded border border-gray-700 bg-gray-900/30 px-3 py-2 text-xs text-gray-400">
            Exit conditions: TP at +${fmtMoney(tpUsd)} ({pctTp.toFixed(1)}% reached) |
            SL at -${fmtMoney(slUsd)} ({pctSl.toFixed(1)}% used)
          </div>
        </div>
      )}
    </div>
  )
}

function BasketStory({ trade, call, put, mergedAdj }) {
  const isClosed = String(trade.status || '').toLowerCase() !== 'active'
  const [expanded, setExpanded] = useState(isClosed)
  const legHistory = Array.isArray(trade.leg_history) ? trade.leg_history : []

  const events = useMemo(() => {
    const rows = []
    const entryTime =
      call.entry_time ||
      put.entry_time ||
      trade.entry_time ||
      (legHistory[0] && legHistory[0].entry_time) ||
      null
    if (entryTime) {
      rows.push({
        key: 'entry',
        time: entryTime,
        icon: '📥',
        type: 'Entry',
        what: `Sold CALL@${fmtStrike(call.strike)} $${fmtMoney(call.initial_premium)} + PUT@${fmtStrike(put.strike)} $${fmtMoney(put.initial_premium)}`,
        why: 'Basket initiated',
        pnl: null,
      })
    }
    for (const adj of mergedAdj || []) {
      const isConv =
        String(adj.decision_type || adj.slab_used || '')
          .toLowerCase()
          .includes('conversion') || Boolean(adj.conversion_mode)
      rows.push({
        key: `adj-${adj.timestamp}-${adj.leg_type}-${adj.old_strike}`,
        time: adj.timestamp,
        icon: isConv ? '🔀' : '🔄',
        type: isConv ? 'Conversion' : 'Adjustment',
        what: `${String(adj.leg_type || '').toUpperCase()} $${fmtStrike(adj.old_strike)} → $${fmtStrike(adj.new_strike)}`,
        why: isConv
          ? `Replacement premium below minimum → hedge bought`
          : `${String(adj.leg_type || '').toUpperCase()} hit ${fmtMoney(adj.trigger_pct_reached ?? adj.trigger_pct ?? 0)}% of baseline (trigger was ${fmtMoney(adj.trigger_pct_reached ?? adj.trigger_pct ?? 0)}%)`,
        pnl: adj.realized_pnl ?? null,
      })
    }
    for (const leg of legHistory) {
      if (String(leg.status || '').toLowerCase() !== 'closed') continue
      if (!leg.exit_time) continue
      const lt = String(leg.leg_type || '').toLowerCase()
      if (lt.startsWith('hedge')) {
        rows.push({
          key: `hedge-exit-${leg.id}`,
          time: leg.exit_time,
          icon: '🔀',
          type: 'Conversion',
          what: `Closed hedge ${leg.symbol || ''} @ $${fmtMoney(leg.exit_premium)}`,
          why: 'Hedge closed (reversal or exit)',
          pnl: leg.realized_pnl,
        })
      }
    }
    if (isClosed) {
      const reason = String(trade.exit_reason || 'EXIT').toUpperCase()
      const exitTime =
        trade.exit_time ||
        call.exit_time ||
        put.exit_time ||
        (legHistory.find((l) => l.exit_time) || {}).exit_time
      let why = reason
      if (reason.includes('STOP')) {
        why = `STOPLOSS: Gross MTM ${fmtSignedMoney(trade.gross_mtm ?? trade.total_pnl)} exceeded -$${fmtMoney(trade.stoploss_usd)}`
      } else if (reason.includes('PROFIT')) {
        why = `PROFIT_TARGET: Net MTM ${fmtSignedMoney(trade.net_mtm)} reached +$${fmtMoney(trade.profit_target_usd)}`
      }
      rows.push({
        key: 'exit',
        time: exitTime || new Date().toISOString(),
        icon: '✅',
        type: 'Exit',
        what: `Basket closed — ${reason}`,
        why,
        pnl: trade.realized_pnl ?? trade.net_mtm ?? null,
      })
    }
    return rows.sort((a, b) => {
      const ta = new Date(a.time || 0).getTime()
      const tb = new Date(b.time || 0).getTime()
      return ta - tb
    })
  }, [trade, call, put, mergedAdj, legHistory, isClosed])

  return (
    <div className="border-t border-gray-700 px-4 py-2 text-xs text-gray-400">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between text-left"
      >
        <div className="text-sm font-semibold text-white">📋 Basket Story</div>
        <span className="text-xs text-gray-400">
          {expanded ? 'Collapse ▲' : 'Expand ▼'}
        </span>
      </button>
      {expanded && (
        <div className="mt-2 max-h-64 space-y-2 overflow-y-auto">
          {events.length === 0 && (
            <div className="text-gray-500">— no events yet —</div>
          )}
          {events.map((ev) => (
            <div
              key={ev.key}
              className="rounded-lg border border-gray-700 bg-gray-900/40 px-3 py-2"
            >
              <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium text-gray-200">
                  {ev.icon} {ev.type}
                </span>
                <span className="text-[11px] text-gray-500">
                  {formatAdjTime(ev.time)}
                </span>
              </div>
              <div className="text-gray-300">{ev.what}</div>
              <div className="mt-0.5 text-gray-500">{ev.why}</div>
              {ev.pnl != null && Number.isFinite(Number(ev.pnl)) && (
                <div className={`mt-0.5 ${pnlColor(ev.pnl)}`}>
                  P&L {fmtSignedMoney(ev.pnl)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
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

  // NET MTM — server fields preferred; always compute slippage locally as fallback
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
  const grossMtm = n(trade.gross_mtm)
  const cumulativeEntrySpread =
    trade.cumulative_entry_spread != null && trade.cumulative_entry_spread !== ''
      ? n(trade.cumulative_entry_spread)
      : null
  const grossMtmForSl =
    trade.gross_mtm_for_stoploss != null && trade.gross_mtm_for_stoploss !== ''
      ? n(trade.gross_mtm_for_stoploss)
      : cumulativeEntrySpread != null
        ? grossMtm + cumulativeEntrySpread
        : null
  const expectedExitSpread =
    trade.expected_exit_spread_usd != null && trade.expected_exit_spread_usd !== ''
      ? n(trade.expected_exit_spread_usd)
      : null
  const hedgeUpnl =
    trade.hedge_upnl != null && trade.hedge_upnl !== ''
      ? n(trade.hedge_upnl)
      : null
  // Always show slippage row — default 2% if API omitted fields
  const slippagePct =
    trade.slippage_pct != null && trade.slippage_pct !== ''
      ? n(trade.slippage_pct)
      : 2.0
  const slippageAmountComputed = Math.abs(grossMtm) * (slippagePct / 100)
  const slippageAmount =
    trade.slippage_amount != null && trade.slippage_amount !== ''
      ? n(trade.slippage_amount)
      : slippageAmountComputed
  const totalDeductions =
    trade.total_deductions != null && trade.total_deductions !== ''
      ? n(trade.total_deductions)
      : feesPaid +
        estExitFees +
        slippageAmount +
        (expectedExitSpread != null ? expectedExitSpread : 0)
  const computedNet =
    grossMtm -
    feesPaid -
    estExitFees -
    slippageAmount -
    (expectedExitSpread != null ? expectedExitSpread : 0)
  const netMtm =
    trade.net_mtm != null && trade.net_mtm !== '' ? n(trade.net_mtm) : computedNet
  const totalMtm = netMtm
  const lastMtmUpdate = trade.last_mtm_update || null
  const target = n(trade.profit_target_usd)
  const stoploss = n(trade.stoploss_usd)
  const nearSl =
    grossMtmForSl != null &&
    stoploss > 0 &&
    grossMtmForSl <= -stoploss * 0.8
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
  const callDeltaSl = Number(trade.call_sl_trigger_price ?? 0)
  const putDeltaSl = Number(trade.put_sl_trigger_price ?? 0)
  const universalSlPct = Number(trade.universal_sl_pct ?? 200)
  const deltaSlActive = Boolean(trade.delta_sl_active)
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
        <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm">
          <span className="font-semibold text-green-400">
            🎯 Target{' '}
            <span className="text-base font-bold">+${fmtMoney(target)}</span>
          </span>
          <span className="font-semibold text-red-400">
            🛑 SL{' '}
            <span className="text-base font-bold">-${fmtMoney(stoploss)}</span>
          </span>
          <span className={pnlColor(netMtm)}>
            Net {fmtSignedMoney(netMtm)}
          </span>
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
          {trade.in_conversion_mode && (
            <div className="flex justify-between">
              <span>Hedge UPL</span>
              <span className={hedgeUpnl == null ? 'text-gray-500' : pnlColor(hedgeUpnl)}>
                {hedgeUpnl == null ? '--' : fmtSignedMoney(hedgeUpnl)}
              </span>
            </div>
          )}
          <div className="flex justify-between">
            <span>Combined UPNL</span>
            <span className={pnlColor(unrealized)}>
              {fmtSignedMoney(unrealized)}
            </span>
          </div>
          <div className="flex justify-between">
            <span>Realized P&L</span>
            <span className={pnlColor(realized)}>{fmtSignedMoney(realized)}</span>
          </div>
          <div className="my-1 border-t border-gray-600" />
          <div className="flex justify-between font-medium text-white">
            <span>Gross MTM</span>
            <span className={pnlColor(grossMtm)}>{fmtSignedMoney(grossMtm)}</span>
          </div>
          <div className="flex justify-between text-yellow-300">
            <span>Entry Spread Diff (cumulative)</span>
            <span>
              {cumulativeEntrySpread == null
                ? '--'
                : `-${fmtMoney(Math.abs(cumulativeEntrySpread))}`}
            </span>
          </div>
          <div
            className={`flex justify-between font-bold ${
              nearSl ? 'text-red-400' : 'text-white'
            }`}
          >
            <span>Gross MTM for SL</span>
            <span>
              {grossMtmForSl == null
                ? '--'
                : fmtSignedMoney(grossMtmForSl)}
            </span>
          </div>
          <div className="my-1 border-t border-gray-600" />
          <div className="flex justify-between text-amber-200/90">
            <span>Fees Paid</span>
            <span>-${fmtMoney(feesPaid)}</span>
          </div>
          <div className="flex justify-between text-amber-200/90">
            <span>Est. Exit Fees</span>
            <span>-${fmtMoney(estExitFees)}</span>
          </div>
          {/* Always rendered — never gated on trade.slippage_pct */}
          <div
            className="flex justify-between rounded bg-yellow-950/40 px-1 py-0.5 font-medium text-yellow-300"
            data-testid="slippage-row"
          >
            <span>Slippage ({fmtMoney(slippagePct)}%)</span>
            <span>-${fmtMoney(slippageAmount)}</span>
          </div>
          <div className="flex justify-between text-yellow-300">
            <span>Expected Exit Spread</span>
            <span>
              {expectedExitSpread == null
                ? '--'
                : `-$${fmtMoney(expectedExitSpread)}`}
            </span>
          </div>
          <div className="my-1 border-t border-gray-600" />
          <div className="flex justify-between text-amber-100">
            <span>Total Deductions</span>
            <span>-${fmtMoney(totalDeductions)}</span>
          </div>
          <div className="flex justify-between pt-0.5 text-base font-semibold text-white">
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
        <div className="text-xs text-gray-400">{displayPct}% of target</div>
        <div className="mt-2 space-y-1.5 rounded-lg border border-gray-700 bg-gray-900/50 px-3 py-2.5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-sm text-green-400">🎯 Target</span>
            <span className="text-right">
              <span className="text-base font-bold text-green-400">
                +${fmtMoney(target)}
              </span>
              <span className="ml-2 text-xs text-gray-400">
                [{fmtMoney(tpPctLocked)}% of ${fmtMoney(initialMax)} max]
              </span>
            </span>
          </div>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="text-sm text-red-400">🛑 Stop Loss</span>
            <span className="text-right">
              <span className="text-base font-bold text-red-400">
                -${fmtMoney(stoploss)}
              </span>
              <span className="ml-2 text-xs text-gray-400">
                [{fmtMoney(slPctLocked)}% of ${fmtMoney(initialMax)} max]
              </span>
            </span>
          </div>
          <div className="flex flex-wrap items-baseline justify-between gap-2 border-t border-gray-700 pt-1.5">
            <span className="text-sm text-gray-300">📊 Net MTM</span>
            <span className={`text-base font-bold ${pnlColor(netMtm)}`}>
              {fmtSignedMoney(netMtm)}
            </span>
          </div>
        </div>
      </div>

      {/* Bot's Next Action Plan */}
      <BotNextActionPlan trade={trade} call={call} put={put} />

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
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-semibold text-white">
            🤖 Bot Monitoring Plan
          </div>
          <div
            className={`text-xs ${
              deltaSlActive ? 'text-green-400' : 'text-amber-300'
            }`}
            title="Attached to Delta position — no separate stop order"
          >
            {deltaSlActive
              ? '🔒 Bracket SL: Active (auto-cancels on close)'
              : '⚠️ Bracket SL: Not set / incomplete'}
          </div>
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
            deltaSlPrice={callDeltaSl}
            universalSlPct={universalSlPct}
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
            deltaSlPrice={putDeltaSl}
            universalSlPct={universalSlPct}
          />
        </div>

        {trade.in_conversion_mode ? (
          <div className="mt-3 space-y-3">
            <div className="rounded-lg border border-yellow-500/50 bg-yellow-500/10 p-4">
              <div className="mb-3 flex items-center gap-2">
                <span className="text-lg text-yellow-400">⚡</span>
                <span className="text-sm font-bold uppercase tracking-wide text-yellow-300">
                  Conversion Mode Active
                </span>
                <span className="ml-auto rounded-full bg-yellow-500/20 px-2 py-0.5 text-xs text-yellow-500">
                  Normal adjustment suspended
                </span>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-3 text-xs">
                <div className="rounded bg-black/20 p-2">
                  <div className="mb-1 text-gray-400">Triggered leg</div>
                  <div className="font-mono font-bold uppercase text-white">
                    {trade.conversion_triggered_leg || '—'}
                  </div>
                </div>
                <div className="rounded bg-black/20 p-2">
                  <div className="mb-1 text-gray-400">Hedge leg (Long)</div>
                  <div className="font-mono text-xs text-green-300">
                    {trade.conversion_hedge_symbol || '—'}
                  </div>
                  <div className="mt-0.5 text-gray-300">
                    Entry: $
                    {fmtMoney(trade.conversion_hedge_entry_price || 0)}
                  </div>
                </div>
              </div>

              <div className="space-y-2 rounded bg-black/20 p-3">
                <div className="mb-2 text-xs font-medium text-gray-400">
                  🎯 Reversal Detection — Hedge closes when:
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-gray-400">Short CALL premium</span>
                  <span className="font-mono font-bold text-red-300">
                    ${fmtMoney(callOfferLive)}
                  </span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-gray-400">Short PUT premium</span>
                  <span className="font-mono font-bold text-blue-300">
                    ${fmtMoney(putOfferLive)}
                  </span>
                </div>
                {(() => {
                  const callP = callOfferLive
                  const putP = putOfferLive
                  const maxP = Math.max(callP, putP)
                  const diffPct =
                    maxP > 0 ? (Math.abs(callP - putP) / maxP) * 100 : 0
                  const eqThreshold = Number(
                    trade.conversion_equality_pct ?? 10,
                  )
                  const higherLeg = callP >= putP ? 'CALL' : 'PUT'
                  const targetLower =
                    maxP > 0 ? maxP * (1 - eqThreshold / 100) : 0
                  const minP = Math.min(callP, putP)
                  const targetHigher =
                    minP > 0 && eqThreshold < 100
                      ? minP / (1 - eqThreshold / 100)
                      : 0
                  const convergePct = Math.max(
                    5,
                    Math.min(
                      100,
                      eqThreshold > 0
                        ? Math.min(100, (eqThreshold / Math.max(diffPct, 0.01)) * 100)
                        : 5,
                    ),
                  )

                  return (
                    <div className="space-y-2 border-t border-gray-700 pt-1">
                      <div className="flex justify-between text-xs">
                        <span className="text-gray-400">Current difference</span>
                        <span
                          className={
                            diffPct <= eqThreshold
                              ? 'font-bold text-green-400'
                              : 'text-yellow-300'
                          }
                        >
                          {diffPct.toFixed(1)}%
                          {diffPct <= eqThreshold ? ' ← CLOSE NOW!' : ''}
                        </span>
                      </div>
                      <div className="flex justify-between text-xs">
                        <span className="text-gray-400">Equality threshold</span>
                        <span className="text-white">
                          within {eqThreshold}%
                        </span>
                      </div>
                      <div className="flex justify-between text-xs">
                        <span className="text-gray-400">
                          {higherLeg} needs to reach
                        </span>
                        <span className="font-mono text-orange-300">
                          ~${fmtMoney(targetLower)} – ${fmtMoney(targetHigher)}
                        </span>
                      </div>
                      <div className="mt-2">
                        <div className="mb-1 flex justify-between text-xs text-gray-500">
                          <span>Far apart</span>
                          <span>Equal ✓</span>
                        </div>
                        <div className="h-2 w-full rounded-full bg-gray-700">
                          <div
                            className={`h-2 rounded-full transition-all ${
                              diffPct <= eqThreshold
                                ? 'bg-green-500'
                                : diffPct <= eqThreshold * 3
                                  ? 'bg-yellow-500'
                                  : 'bg-red-500'
                            }`}
                            style={{ width: `${convergePct}%` }}
                          />
                        </div>
                        <div className="mt-1 text-center text-xs text-gray-500">
                          {diffPct <= eqThreshold
                            ? '✅ Hedge closing...'
                            : `${(diffPct - eqThreshold).toFixed(1)}% more to converge`}
                        </div>
                      </div>
                    </div>
                  )
                })()}
              </div>

              <div className="mt-3 border-t border-gray-700 pt-3 text-xs text-gray-400">
                <span className="font-medium text-gray-300">
                  When equality reached:{' '}
                </span>
                Hedge ({trade.conversion_hedge_symbol || '—'}) closes → Normal
                150% adjustment monitoring resumes on both short legs
              </div>
            </div>
          </div>
        ) : (
          <div className="mt-3 space-y-2 rounded-lg border border-gray-700 bg-gray-900/40 p-3 text-xs text-gray-300">
            <div>
              <span className="font-semibold text-gray-200">
                If CALL triggers:
              </span>
              <div className="mt-0.5 text-gray-400">
                Bot will buy back CALL @ ~${fmtMoney(callTrigger)}
              </div>
              <div className="text-gray-400">
                Sell new CALL near PUT premium (~$
                {fmtMoney(put.current_premium)})
              </div>
              {callRepl ? (
                <div className="text-green-300/90">
                  Nearest match: {callRepl.symbol} @ $
                  {fmtMoney(callRepl.premium)} (est.)
                </div>
              ) : (
                <div className="text-gray-500">Nearest match: estimating…</div>
              )}
            </div>
            <div className="border-t border-gray-700 pt-2">
              <span className="font-semibold text-gray-200">
                If PUT triggers:
              </span>
              <div className="mt-0.5 text-gray-400">
                Bot will buy back PUT @ ~${fmtMoney(putTrigger)}
              </div>
              <div className="text-gray-400">
                Sell new PUT near CALL premium (~$
                {fmtMoney(call.current_premium)})
              </div>
              {putRepl ? (
                <div className="text-green-300/90">
                  Nearest match: {putRepl.symbol} @ $
                  {fmtMoney(putRepl.premium)} (est.)
                </div>
              ) : (
                <div className="text-gray-500">Nearest match: estimating…</div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Basket Story timeline */}
      <BasketStory
        trade={trade}
        call={call}
        put={put}
        mergedAdj={mergedAdj}
      />

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
