import { useCallback, useEffect, useMemo, useState } from 'react'
import { getActiveHedge, getActiveTrades } from '../services/api'
import { OPTIONS_CONTRACT_VALUE } from '../utils/contractValue'
import { useWebSocket } from './useWebSocket'

const WS_URL = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`
const POLL_INTERVAL_MS = 10000

/** Short-option UPNL @ offer — same formula as Delta / backend. */
function shortLegUpnl(entry, offer, qty) {
  const e = Number(entry) || 0
  const o = Number(offer) || 0
  const q = Math.abs(Number(qty) || 0)
  if (e <= 0 || o <= 0 || q <= 0) return 0
  return (o - e) * -q * OPTIONS_CONTRACT_VALUE
}

/** Long wing UPNL — (mark − entry) × qty × CV */
function longLegUpnl(entry, mark, qty) {
  const e = Number(entry) || 0
  const m = Number(mark) || 0
  const q = Math.abs(Number(qty) || 0)
  if (e <= 0 || m <= 0 || q <= 0) return 0
  return (m - e) * q * OPTIONS_CONTRACT_VALUE
}

function mergeWingLeg(existing, msgLeg, msgPrem) {
  if (!msgLeg && !existing) return existing || null
  if (!msgLeg) {
    if (!existing) return null
    if (msgPrem == null) return existing
    const entry = Number(existing.initial_premium || 0)
    const qty = Math.abs(Number(existing.quantity || 0))
    const px = Number(msgPrem)
    return {
      ...existing,
      current_premium: px,
      change_pct: entry > 0 ? (px / entry - 1) * 100 : existing.change_pct,
      leg_pnl: longLegUpnl(entry, px, qty),
    }
  }
  return {
    ...(existing || {}),
    ...msgLeg,
    current_premium:
      existing?.current_premium ?? msgPrem ?? msgLeg.current_premium,
    leg_pnl: existing?.leg_pnl ?? msgLeg.leg_pnl,
  }
}

/** Live mark-based UPNL only — matches Delta UI gross display (B24). */
function resolveDisplayGrossUpnl(trade) {
  const upnl = Number(
    trade?.delta_upnl ?? trade?.unrealized_pnl ?? trade?.combined_upnl,
  )
  if (Number.isFinite(upnl)) return upnl
  const gross = Number(trade?.gross_mtm ?? trade?.total_pnl)
  return Number.isFinite(gross) ? gross : 0
}

/** Total position value (realized + live UPNL) — used for net MTM math. */
function resolveCalculatedPnl(trade, deltaUpnl) {
  const calc = Number(trade?.calculated_pnl)
  if (Number.isFinite(calc)) return calc
  const realized = Number(trade?.realized_pnl)
  const realizedNum = Number.isFinite(realized) ? realized : 0
  return deltaUpnl + realizedNum
}

/**
 * Live trade state for Dashboard.
 * Prefers WebSocket; falls back to REST polling when disconnected.
 */
export function useTrades() {
  const { lastMessage, status: wsStatus } = useWebSocket(WS_URL)
  const [tradeMap, setTradeMap] = useState(() => new Map())
  const [errors, setErrors] = useState({})
  const [adjustments, setAdjustments] = useState([])
  const [loading, setLoading] = useState(true)
  /** Latest AUTO_TRADE_* snapshot from WS (Dashboard banner can sync). */
  const [autoTradeStatus, setAutoTradeStatus] = useState(null)
  /** Live long hedge panel payload (null when none). */
  const [activeHedge, setActiveHedge] = useState(null)

  const applyTradesList = useCallback((list) => {
    const next = new Map()
    for (const trade of list || []) {
      const id = trade.trade_id
      if (id == null) continue
      const openCount = Number(trade.open_leg_count)
      const status = String(trade.status || '').toLowerCase()
      // Never keep flat/closed baskets on the live dashboard
      if (status && status !== 'active') continue
      if (Number.isFinite(openCount) && openCount <= 0) continue
      const grossDisplay = resolveDisplayGrossUpnl(trade)
      const calculated = resolveCalculatedPnl(trade, grossDisplay)
      const slipPct =
        trade.slippage_pct != null && Number.isFinite(Number(trade.slippage_pct))
          ? Number(trade.slippage_pct)
          : 2.0
      const slipAmt =
        trade.slippage_amount != null &&
        Number.isFinite(Number(trade.slippage_amount))
          ? Number(trade.slippage_amount)
          : Math.abs(calculated) * (slipPct / 100)
      const fees = Number(trade.fees_paid) || 0
      const estExit = Number(trade.est_exit_fees) || 0
      const deductions =
        trade.total_deductions != null &&
        Number.isFinite(Number(trade.total_deductions))
          ? Number(trade.total_deductions)
          : fees + estExit + slipAmt
      const net =
        trade.net_mtm != null && Number.isFinite(Number(trade.net_mtm))
          ? Number(trade.net_mtm)
          : calculated - deductions
      next.set(id, {
        ...trade,
        slippage_pct: slipPct,
        slippage_amount: slipAmt,
        total_deductions: deductions,
        net_mtm: net,
        gross_mtm: grossDisplay,
        calculated_pnl: calculated,
        total_pnl: calculated,
      })
    }
    setTradeMap(next)
  }, [])

  const refetch = useCallback(async () => {
    try {
      const [tradeData, hedgeResult] = await Promise.all([
        getActiveTrades(),
        getActiveHedge()
          .then((data) => ({ ok: true, data }))
          .catch(() => ({ ok: false, data: null })),
      ])
      applyTradesList(tradeData?.trades || [])
      if (hedgeResult.ok) {
        setActiveHedge(hedgeResult.data?.hedge ?? null)
      }
      setLoading(false)
    } catch (err) {
      setLoading(false)
      setErrors((prev) => ({
        ...prev,
        global: err.message || 'Failed to fetch trades',
      }))
    }
  }, [applyTradesList])

  // Handle WS messages
  useEffect(() => {
    if (!lastMessage || !lastMessage.type) return
    const msg = lastMessage

    if (msg.type === 'INITIAL_STATE') {
      applyTradesList(msg.trades || [])
      setLoading(false)
      // REST has full leg snapshots; merge after sparse WS snapshot
      refetch()
      return
    }

    if (msg.type === 'TRADE_UPDATE') {
      const openCount = Number(msg.open_leg_count)
      const status = String(msg.status || '').toLowerCase()
      if (
        (status && status !== 'active') ||
        (Number.isFinite(openCount) && openCount <= 0)
      ) {
        setTradeMap((prev) => {
          const next = new Map(prev)
          next.delete(msg.trade_id)
          return next
        })
        setLoading(false)
        return
      }
      setTradeMap((prev) => {
        const next = new Map(prev)
        const existing = next.get(msg.trade_id) || { trade_id: msg.trade_id }
        const callEntry =
          msg.call_entry_premium != null
            ? Number(msg.call_entry_premium)
            : existing.call_entry_premium
        const putEntry =
          msg.put_entry_premium != null
            ? Number(msg.put_entry_premium)
            : existing.put_entry_premium

        // Prefer server leg snapshots for strike/symbol; keep live offer/leg_pnl from ticks
        const mergedCallLeg = msg.call_leg
          ? {
              ...(existing.call_leg || {}),
              ...msg.call_leg,
              initial_premium:
                callEntry != null
                  ? callEntry
                  : msg.call_leg.initial_premium,
              // Do not overwrite live offer / display leg_pnl with UPL
              current_premium:
                existing.call_leg?.current_premium ??
                msg.call_premium ??
                msg.call_leg.current_premium,
              leg_pnl: existing.call_leg?.leg_pnl,
            }
          : existing.call_leg
            ? {
                ...existing.call_leg,
                initial_premium:
                  callEntry != null
                    ? callEntry
                    : existing.call_leg.initial_premium,
                current_premium:
                  msg.call_premium ?? existing.call_leg.current_premium,
                change_pct:
                  msg.call_change_pct ?? existing.call_leg.change_pct,
              }
            : existing.call_leg

        const mergedPutLeg = msg.put_leg
          ? {
              ...(existing.put_leg || {}),
              ...msg.put_leg,
              initial_premium:
                putEntry != null ? putEntry : msg.put_leg.initial_premium,
              current_premium:
                existing.put_leg?.current_premium ??
                msg.put_premium ??
                msg.put_leg.current_premium,
              leg_pnl: existing.put_leg?.leg_pnl,
            }
          : existing.put_leg
            ? {
                ...existing.put_leg,
                initial_premium:
                  putEntry != null
                    ? putEntry
                    : existing.put_leg.initial_premium,
                current_premium:
                  msg.put_premium ?? existing.put_leg.current_premium,
                change_pct: msg.put_change_pct ?? existing.put_leg.change_pct,
              }
            : existing.put_leg

        const callUpnl =
          msg.call_upnl != null
            ? Number(msg.call_upnl)
            : msg.call_delta_mtm != null
              ? Number(msg.call_delta_mtm)
              : existing.call_upnl
        const putUpnl =
          msg.put_upnl != null
            ? Number(msg.put_upnl)
            : msg.put_delta_mtm != null
              ? Number(msg.put_delta_mtm)
              : existing.put_upnl
        const deltaUpnl =
          msg.delta_upnl != null
            ? Number(msg.delta_upnl)
            : msg.delta_mtm_pnl != null
              ? Number(msg.delta_mtm_pnl)
              : existing.delta_upnl
        const realized =
          msg.realized_pnl != null
            ? Number(msg.realized_pnl)
            : existing.realized_pnl
        const grossDisplay =
          deltaUpnl != null && Number.isFinite(Number(deltaUpnl))
            ? Number(deltaUpnl)
            : resolveDisplayGrossUpnl({ ...existing, ...msg })
        const calculated =
          msg.calculated_pnl != null && Number.isFinite(Number(msg.calculated_pnl))
            ? Number(msg.calculated_pnl)
            : resolveCalculatedPnl(
                { ...existing, ...msg, delta_upnl: grossDisplay, realized_pnl: realized },
                grossDisplay,
              )
        const feesPaid =
          msg.fees_paid != null ? Number(msg.fees_paid) : existing.fees_paid
        const estExit =
          msg.est_exit_fees != null
            ? Number(msg.est_exit_fees)
            : existing.est_exit_fees
        const slipPctRaw =
          msg.slippage_pct != null
            ? Number(msg.slippage_pct)
            : existing.slippage_pct
        const slipPct =
          slipPctRaw != null && Number.isFinite(Number(slipPctRaw))
            ? Number(slipPctRaw)
            : 2.0
        const slipAmtRaw =
          msg.slippage_amount != null
            ? Number(msg.slippage_amount)
            : existing.slippage_amount
        const slipAmt =
          slipAmtRaw != null && Number.isFinite(Number(slipAmtRaw))
            ? Number(slipAmtRaw)
            : Math.abs(Number(calculated) || 0) * (slipPct / 100)
        const deductionsRaw =
          msg.total_deductions != null
            ? Number(msg.total_deductions)
            : existing.total_deductions
        const deductions =
          deductionsRaw != null && Number.isFinite(Number(deductionsRaw))
            ? Number(deductionsRaw)
            : (Number(feesPaid) || 0) + (Number(estExit) || 0) + slipAmt
        const net =
          msg.net_mtm != null
            ? Number(msg.net_mtm)
            : existing.net_mtm != null
              ? Number(existing.net_mtm)
              : (Number(calculated) || 0) - deductions

        next.set(msg.trade_id, {
          ...existing,
          ...msg,
          call_leg: mergedCallLeg,
          put_leg: mergedPutLeg,
          wing_call: mergeWingLeg(
            existing.wing_call,
            msg.wing_call,
            msg.wing_call_premium,
          ),
          wing_put: mergeWingLeg(
            existing.wing_put,
            msg.wing_put,
            msg.wing_put_premium,
          ),
          net_credit_entry:
            msg.net_credit_entry != null
              ? Number(msg.net_credit_entry)
              : existing.net_credit_entry,
          net_credit_now:
            msg.net_credit_now != null
              ? Number(msg.net_credit_now)
              : existing.net_credit_now,
          wing_premium_paid_usd:
            msg.wing_premium_paid_usd != null
              ? Number(msg.wing_premium_paid_usd)
              : existing.wing_premium_paid_usd,
          max_loss_usd:
            msg.max_loss_usd != null
              ? Number(msg.max_loss_usd)
              : existing.max_loss_usd,
          call_entry_premium:
            callEntry != null ? callEntry : existing.call_entry_premium,
          put_entry_premium:
            putEntry != null ? putEntry : existing.put_entry_premium,
          call_trigger_baseline:
            msg.call_trigger_baseline != null
              ? Number(msg.call_trigger_baseline)
              : existing.call_trigger_baseline,
          put_trigger_baseline:
            msg.put_trigger_baseline != null
              ? Number(msg.put_trigger_baseline)
              : existing.put_trigger_baseline,
          call_trigger_price:
            msg.call_trigger_price != null
              ? Number(msg.call_trigger_price)
              : existing.call_trigger_price,
          put_trigger_price:
            msg.put_trigger_price != null
              ? Number(msg.put_trigger_price)
              : existing.put_trigger_price,
          call_pct_to_trigger:
            msg.call_pct_to_trigger != null
              ? Number(msg.call_pct_to_trigger)
              : existing.call_pct_to_trigger,
          put_pct_to_trigger:
            msg.put_pct_to_trigger != null
              ? Number(msg.put_pct_to_trigger)
              : existing.put_pct_to_trigger,
          call_distance_to_trigger:
            msg.call_distance_to_trigger != null
              ? Number(msg.call_distance_to_trigger)
              : existing.call_distance_to_trigger,
          put_distance_to_trigger:
            msg.put_distance_to_trigger != null
              ? Number(msg.put_distance_to_trigger)
              : existing.put_distance_to_trigger,
          current_trigger_pct:
            msg.current_trigger_pct != null
              ? Number(msg.current_trigger_pct)
              : existing.current_trigger_pct,
          call_trigger_pct:
            msg.call_trigger_pct != null
              ? Number(msg.call_trigger_pct)
              : existing.call_trigger_pct,
          put_trigger_pct:
            msg.put_trigger_pct != null
              ? Number(msg.put_trigger_pct)
              : existing.put_trigger_pct,
          trigger_mode: msg.trigger_mode ?? existing.trigger_mode,
          combined_trigger_mode:
            msg.combined_trigger_mode != null
              ? Boolean(msg.combined_trigger_mode)
              : existing.combined_trigger_mode,
          combined_entry_premium:
            msg.combined_entry_premium != null
              ? Number(msg.combined_entry_premium)
              : existing.combined_entry_premium,
          combined_current_premium:
            msg.combined_current_premium != null
              ? Number(msg.combined_current_premium)
              : existing.combined_current_premium,
          combined_trigger_pct:
            msg.combined_trigger_pct != null
              ? Number(msg.combined_trigger_pct)
              : existing.combined_trigger_pct,
          combined_trigger_threshold:
            msg.combined_trigger_threshold != null
              ? Number(msg.combined_trigger_threshold)
              : existing.combined_trigger_threshold,
          combined_pct_to_trigger:
            msg.combined_pct_to_trigger != null
              ? Number(msg.combined_pct_to_trigger)
              : existing.combined_pct_to_trigger,
          combined_triggered_leg:
            msg.combined_triggered_leg ?? existing.combined_triggered_leg,
          premium_slab_300: msg.premium_slab_300 ?? existing.premium_slab_300,
          premium_slab_200: msg.premium_slab_200 ?? existing.premium_slab_200,
          premium_slab_100: msg.premium_slab_100 ?? existing.premium_slab_100,
          premium_slab_lt100:
            msg.premium_slab_lt100 ?? existing.premium_slab_lt100,
          leg_history: msg.leg_history || existing.leg_history,
          // Server MTM only — never undefined-overwrite
          call_upnl: callUpnl,
          put_upnl: putUpnl,
          call_delta_mtm: callUpnl,
          put_delta_mtm: putUpnl,
          delta_upnl: deltaUpnl,
          delta_mtm_pnl: deltaUpnl,
          combined_upnl: deltaUpnl,
          unrealized_pnl: deltaUpnl,
          realized_pnl: realized,
          calculated_pnl: calculated,
          gross_mtm: grossDisplay,
          total_pnl: calculated,
          net_mtm: net,
          fees_paid: feesPaid,
          est_exit_fees: estExit,
          total_expected_fees:
            msg.total_expected_fees != null
              ? Number(msg.total_expected_fees)
              : existing.total_expected_fees,
          slippage_pct: slipPct,
          slippage_amount: slipAmt,
          total_deductions: deductions,
          universal_sl_pct:
            msg.universal_sl_pct != null
              ? Number(msg.universal_sl_pct)
              : existing.universal_sl_pct,
          call_sl_trigger_price:
            msg.call_sl_trigger_price != null
              ? Number(msg.call_sl_trigger_price)
              : existing.call_sl_trigger_price,
          put_sl_trigger_price:
            msg.put_sl_trigger_price != null
              ? Number(msg.put_sl_trigger_price)
              : existing.put_sl_trigger_price,
          call_sl_order_id:
            msg.call_sl_order_id != null
              ? msg.call_sl_order_id
              : existing.call_sl_order_id,
          put_sl_order_id:
            msg.put_sl_order_id != null
              ? msg.put_sl_order_id
              : existing.put_sl_order_id,
          delta_sl_active:
            msg.delta_sl_active != null
              ? Boolean(msg.delta_sl_active)
              : existing.delta_sl_active,
          last_mtm_update: msg.last_mtm_update ?? existing.last_mtm_update,
          underlying_price:
            Number(msg.underlying_price) > 0
              ? Number(msg.underlying_price)
              : existing.underlying_price,
          // New spread/SL fields from backend
          gross_mtm_for_stoploss:
            msg.gross_mtm_for_stoploss != null
              ? Number(msg.gross_mtm_for_stoploss)
              : existing.gross_mtm_for_stoploss,
          entry_spread_for_sl:
            msg.entry_spread_for_sl != null
              ? Number(msg.entry_spread_for_sl)
              : msg.cumulative_entry_spread != null
                ? Number(msg.cumulative_entry_spread)
                : existing.entry_spread_for_sl,
          expected_exit_spread_usd:
            msg.expected_exit_spread_usd != null
              ? Number(msg.expected_exit_spread_usd)
              : existing.expected_exit_spread_usd,
          next_action_plan: msg.next_action_plan ?? existing.next_action_plan,
          bot_next_action: msg.bot_next_action ?? existing.bot_next_action,
          bot_closer_leg: msg.bot_closer_leg ?? existing.bot_closer_leg,
          bot_call_pct_to_trigger:
            msg.bot_call_pct_to_trigger != null
              ? Number(msg.bot_call_pct_to_trigger)
              : existing.bot_call_pct_to_trigger,
          bot_put_pct_to_trigger:
            msg.bot_put_pct_to_trigger != null
              ? Number(msg.bot_put_pct_to_trigger)
              : existing.bot_put_pct_to_trigger,
          adjustment_count:
            msg.adjustment_count != null
              ? Number(msg.adjustment_count)
              : existing.adjustment_count,
          max_adjustments_per_basket:
            msg.max_adjustments_per_basket !== undefined
              ? msg.max_adjustments_per_basket
              : existing.max_adjustments_per_basket,
          adjustments_remaining:
            msg.adjustments_remaining !== undefined
              ? msg.adjustments_remaining
              : existing.adjustments_remaining,
          conversion_mode_enabled:
            msg.conversion_mode_enabled != null
              ? Boolean(msg.conversion_mode_enabled)
              : existing.conversion_mode_enabled,
        })
        return next
      })
      setLoading(false)
      // Only full refresh when leg set changed (partial close / reconcile)
      if (msg.leg_history) {
        refetch()
      }
      return
    }

    if (msg.type === 'PRICE_TICK') {
      // Offer / leg-table display ONLY. Never touch basket MTM / UPL fields.
      if (msg.price_type !== 'ask') return
      const tickPx = Number(msg.price)
      if (!Number.isFinite(tickPx) || tickPx <= 0) return

      setTradeMap((prev) => {
        const next = new Map(prev)
        const trade = next.get(msg.trade_id)
        if (!trade) return prev

        const isCall = trade.call_leg?.symbol === msg.symbol
        const isPut = trade.put_leg?.symbol === msg.symbol
        const isWingCall = trade.wing_call?.symbol === msg.symbol
        const isWingPut = trade.wing_put?.symbol === msg.symbol
        if (!isCall && !isPut && !isWingCall && !isWingPut) return prev

        const patchShort = (leg) => {
          if (!leg) return leg
          const entry = Number(leg.initial_premium ?? 0)
          const qty = Math.abs(Number(leg.quantity ?? 0))
          const changePct = entry > 0 ? (tickPx / entry - 1) * 100 : 0
          return {
            ...leg,
            current_premium: tickPx,
            change_pct: changePct,
            leg_pnl: shortLegUpnl(entry, tickPx, qty),
          }
        }
        const patchLong = (leg) => {
          if (!leg) return leg
          const entry = Number(leg.initial_premium ?? 0)
          const qty = Math.abs(Number(leg.quantity ?? 0))
          const changePct = entry > 0 ? (tickPx / entry - 1) * 100 : 0
          return {
            ...leg,
            current_premium: tickPx,
            change_pct: changePct,
            leg_pnl: longLegUpnl(entry, tickPx, qty),
          }
        }

        next.set(msg.trade_id, {
          ...trade,
          call_leg: isCall ? patchShort(trade.call_leg) : trade.call_leg,
          put_leg: isPut ? patchShort(trade.put_leg) : trade.put_leg,
          wing_call: isWingCall
            ? patchLong(trade.wing_call)
            : trade.wing_call,
          wing_put: isWingPut ? patchLong(trade.wing_put) : trade.wing_put,
          wing_call_premium: isWingCall
            ? tickPx
            : trade.wing_call_premium,
          wing_put_premium: isWingPut ? tickPx : trade.wing_put_premium,
          // Pin server MTM — PRICE_TICK must never alter these
          call_upnl: trade.call_upnl,
          put_upnl: trade.put_upnl,
          combined_upnl: trade.combined_upnl,
          delta_upnl: trade.delta_upnl,
          delta_mtm_pnl: trade.delta_mtm_pnl,
          unrealized_pnl: trade.unrealized_pnl,
          call_delta_mtm: trade.call_delta_mtm,
          put_delta_mtm: trade.put_delta_mtm,
          realized_pnl: trade.realized_pnl,
          gross_mtm: trade.gross_mtm,
          net_mtm: trade.net_mtm,
          total_pnl: trade.total_pnl,
          fees_paid: trade.fees_paid,
          est_exit_fees: trade.est_exit_fees,
          total_expected_fees: trade.total_expected_fees,
          slippage_pct: trade.slippage_pct,
          slippage_amount: trade.slippage_amount,
          total_deductions: trade.total_deductions,
          gross_mtm_for_stoploss: trade.gross_mtm_for_stoploss,
          entry_spread_for_sl: trade.entry_spread_for_sl,
          expected_exit_spread_usd: trade.expected_exit_spread_usd,
          last_mtm_update: trade.last_mtm_update,
        })
        return next
      })
      return
    }

    if (msg.type === 'ADJUSTMENT') {
      setAdjustments((prev) => [msg, ...prev])
      setTradeMap((prev) => {
        const next = new Map(prev)
        const existing = next.get(msg.trade_id)
        if (existing) {
          const callLeg = msg.call_leg
            ? { ...(existing.call_leg || {}), ...msg.call_leg }
            : existing.call_leg
          const putLeg = msg.put_leg
            ? { ...(existing.put_leg || {}), ...msg.put_leg }
            : existing.put_leg
          next.set(msg.trade_id, {
            ...existing,
            ...msg,
            call_leg: callLeg,
            put_leg: putLeg,
            leg_history: msg.leg_history || existing.leg_history,
            last_adjustment: msg,
            adjustment_count:
              msg.adjustment_count ?? (existing.adjustment_count || 0) + 1,
            // Baselines + triggers from post-adjustment plan (overwrite cache)
            call_entry_premium:
              msg.call_entry_premium != null
                ? Number(msg.call_entry_premium)
                : callLeg?.initial_premium ?? existing.call_entry_premium,
            put_entry_premium:
              msg.put_entry_premium != null
                ? Number(msg.put_entry_premium)
                : putLeg?.initial_premium ?? existing.put_entry_premium,
            call_trigger_baseline:
              msg.call_trigger_baseline != null
                ? Number(msg.call_trigger_baseline)
                : existing.call_trigger_baseline,
            put_trigger_baseline:
              msg.put_trigger_baseline != null
                ? Number(msg.put_trigger_baseline)
                : existing.put_trigger_baseline,
            call_trigger_price:
              msg.call_trigger_price != null
                ? Number(msg.call_trigger_price)
                : existing.call_trigger_price,
            put_trigger_price:
              msg.put_trigger_price != null
                ? Number(msg.put_trigger_price)
                : existing.put_trigger_price,
            call_pct_to_trigger:
              msg.call_pct_to_trigger != null
                ? Number(msg.call_pct_to_trigger)
                : existing.call_pct_to_trigger,
            put_pct_to_trigger:
              msg.put_pct_to_trigger != null
                ? Number(msg.put_pct_to_trigger)
                : existing.put_pct_to_trigger,
            current_trigger_pct:
              msg.current_trigger_pct != null
                ? Number(msg.current_trigger_pct)
                : existing.current_trigger_pct,
          })
        }
        return next
      })
      // Always refetch — strike/symbol must match DB without manual refresh
      refetch()
      return
    }

    if (msg.type === 'HEDGE_UPDATE') {
      setActiveHedge((prev) => {
        if (!prev || Number(prev.id) !== Number(msg.hedge_id)) return prev
        return {
          ...prev,
          hedge_net_mtm:
            msg.hedge_net_mtm != null ? Number(msg.hedge_net_mtm) : prev.hedge_net_mtm,
          cum_closed_basket_pnl:
            msg.cum_closed_basket_pnl != null
              ? Number(msg.cum_closed_basket_pnl)
              : prev.cum_closed_basket_pnl,
          open_basket_net_mtm:
            msg.open_basket_net_mtm != null
              ? Number(msg.open_basket_net_mtm)
              : prev.open_basket_net_mtm,
          structure_pnl:
            msg.structure_pnl != null ? Number(msg.structure_pnl) : prev.structure_pnl,
          pct_to_target:
            msg.pct_to_target != null ? Number(msg.pct_to_target) : prev.pct_to_target,
        }
      })
      return
    }

    if (msg.type === 'HEDGE_CLOSED') {
      setActiveHedge((prev) => {
        if (prev && Number(prev.id) === Number(msg.hedge_id)) return null
        return prev
      })
      refetch()
      return
    }

    if (msg.type === 'TRADE_CLOSED') {
      setTradeMap((prev) => {
        const next = new Map(prev)
        next.delete(msg.trade_id)
        return next
      })
      return
    }

    if (msg.type === 'AUTO_TRADE_PLACED') {
      setAutoTradeStatus({
        type: 'AUTO_TRADE_PLACED',
        trade_id: msg.trade_id,
        underlying: msg.underlying,
        strike: msg.strike,
        at: Date.now(),
      })
      refetch()
      return
    }

    if (msg.type === 'AUTO_TRADE_WAITING') {
      const secs = Number(msg.seconds_remaining)
      setAutoTradeStatus({
        type: 'AUTO_TRADE_WAITING',
        underlying: msg.underlying,
        seconds_remaining: Number.isFinite(secs) ? Math.max(0, secs) : null,
        next_entry_time: msg.next_entry_time || null,
        at: Date.now(),
      })
      return
    }

    if (msg.type === 'AUTO_TRADE_FAILED') {
      setAutoTradeStatus({
        type: 'AUTO_TRADE_FAILED',
        underlying: msg.underlying,
        error: msg.error,
        retry_in_seconds: msg.retry_in_seconds,
        message: msg.message,
        at: Date.now(),
      })
      return
    }

    if (msg.type === 'ERROR') {
      setErrors((prev) => ({
        ...prev,
        [msg.trade_id ?? 'global']: msg.message || 'Unknown error',
      }))
    }
  }, [lastMessage, applyTradesList, refetch])

  // Fallback polling when WS disconnected
  useEffect(() => {
    if (wsStatus !== 'disconnected') return undefined

    refetch()
    const id = setInterval(refetch, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [wsStatus, refetch])

  // While connected: light REST refresh so MTM/fees stay fresh if WS ticks stall
  useEffect(() => {
    if (wsStatus !== 'connected') return undefined
    const id = setInterval(refetch, 5000)
    return () => clearInterval(id)
  }, [wsStatus, refetch])

  const trades = useMemo(() => Array.from(tradeMap.values()), [tradeMap])

  return {
    trades,
    activeHedge,
    wsStatus,
    errors,
    adjustments,
    loading,
    refetch,
    autoTradeStatus,
  }
}
