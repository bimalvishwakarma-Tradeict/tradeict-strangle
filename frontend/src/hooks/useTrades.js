import { useCallback, useEffect, useMemo, useState } from 'react'
import { getActiveTrades } from '../services/api'
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
      const gross = Number(trade.gross_mtm ?? trade.total_pnl ?? 0) || 0
      const slipPct =
        trade.slippage_pct != null && Number.isFinite(Number(trade.slippage_pct))
          ? Number(trade.slippage_pct)
          : 2.0
      const slipAmt =
        trade.slippage_amount != null &&
        Number.isFinite(Number(trade.slippage_amount))
          ? Number(trade.slippage_amount)
          : Math.abs(gross) * (slipPct / 100)
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
          : gross - deductions
      next.set(id, {
        ...trade,
        slippage_pct: slipPct,
        slippage_amount: slipAmt,
        total_deductions: deductions,
        net_mtm: net,
        gross_mtm: gross,
      })
    }
    setTradeMap(next)
  }, [])

  const refetch = useCallback(async () => {
    try {
      const data = await getActiveTrades()
      applyTradesList(data?.trades || [])
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
        const gross =
          msg.gross_mtm != null
            ? Number(msg.gross_mtm)
            : msg.total_pnl != null
              ? Number(msg.total_pnl)
              : existing.gross_mtm
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
            : Math.abs(Number(gross) || 0) * (slipPct / 100)
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
              : (Number(gross) || 0) - deductions

        next.set(msg.trade_id, {
          ...existing,
          ...msg,
          call_leg: mergedCallLeg,
          put_leg: mergedPutLeg,
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
          gross_mtm: gross,
          total_pnl: gross,
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
        if (!isCall && !isPut) return prev

        const patchLeg = (leg) => {
          if (!leg) return leg
          const entry = Number(
            leg.initial_premium ??
              (isCall ? trade.call_entry_premium : trade.put_entry_premium) ??
              0,
          )
          const qty = Math.abs(Number(leg.quantity ?? 0))
          const changePct = entry > 0 ? (tickPx / entry - 1) * 100 : 0
          return {
            ...leg,
            current_premium: tickPx,
            change_pct: changePct,
            // Legs-table display only — must never feed NET MTM
            leg_pnl: shortLegUpnl(entry, tickPx, qty),
          }
        }

        next.set(msg.trade_id, {
          ...trade,
          call_leg: isCall ? patchLeg(trade.call_leg) : trade.call_leg,
          put_leg: isPut ? patchLeg(trade.put_leg) : trade.put_leg,
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

    if (msg.type === 'TRADE_CLOSED') {
      setTradeMap((prev) => {
        const next = new Map(prev)
        next.delete(msg.trade_id)
        return next
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
    wsStatus,
    errors,
    adjustments,
    loading,
    refetch,
  }
}
