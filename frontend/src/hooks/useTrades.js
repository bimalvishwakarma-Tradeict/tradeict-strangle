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
      next.set(id, trade)
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
        const callLeg = existing.call_leg
          ? {
              ...existing.call_leg,
              current_premium: msg.call_premium ?? existing.call_leg.current_premium,
              change_pct: msg.call_change_pct ?? existing.call_leg.change_pct,
              leg_pnl:
                msg.call_delta_mtm != null
                  ? msg.call_delta_mtm
                  : existing.call_leg.leg_pnl,
            }
          : existing.call_leg
        const putLeg = existing.put_leg
          ? {
              ...existing.put_leg,
              current_premium: msg.put_premium ?? existing.put_leg.current_premium,
              change_pct: msg.put_change_pct ?? existing.put_leg.change_pct,
              leg_pnl:
                msg.put_delta_mtm != null
                  ? msg.put_delta_mtm
                  : existing.put_leg.leg_pnl,
            }
          : existing.put_leg
        next.set(msg.trade_id, {
          ...existing,
          ...msg,
          call_leg: msg.call_leg
            ? { ...(existing.call_leg || {}), ...msg.call_leg }
            : callLeg
              ? {
                  ...callLeg,
                  leg_pnl:
                    msg.call_upnl ?? msg.call_delta_mtm ?? callLeg.leg_pnl,
                }
              : existing.call_leg,
          put_leg: msg.put_leg
            ? { ...(existing.put_leg || {}), ...msg.put_leg }
            : putLeg
              ? {
                  ...putLeg,
                  leg_pnl: msg.put_upnl ?? msg.put_delta_mtm ?? putLeg.leg_pnl,
                }
              : existing.put_leg,
          leg_history: msg.leg_history || existing.leg_history,
          call_upnl: msg.call_upnl ?? msg.call_delta_mtm,
          put_upnl: msg.put_upnl ?? msg.put_delta_mtm,
          delta_upnl: msg.delta_upnl ?? msg.delta_mtm_pnl,
          delta_mtm_pnl: msg.delta_mtm_pnl ?? msg.delta_upnl,
          calculated_pnl: msg.calculated_pnl,
          // Keep fee/net fields coherent with live MTM (don't leave stale gross_mtm)
          gross_mtm:
            msg.gross_mtm ??
            msg.total_pnl ??
            (Number(msg.realized_pnl ?? existing.realized_pnl ?? 0) +
              Number(msg.delta_upnl ?? msg.delta_mtm_pnl ?? 0)),
          net_mtm:
            msg.net_mtm ??
            (Number(
              msg.gross_mtm ??
                msg.total_pnl ??
                Number(msg.realized_pnl ?? existing.realized_pnl ?? 0) +
                  Number(msg.delta_upnl ?? msg.delta_mtm_pnl ?? 0),
            ) -
              Number(msg.fees_paid ?? existing.fees_paid ?? 0) -
              Number(msg.est_exit_fees ?? existing.est_exit_fees ?? 0)),
          fees_paid: msg.fees_paid ?? existing.fees_paid,
          est_exit_fees: msg.est_exit_fees ?? existing.est_exit_fees,
          total_expected_fees:
            msg.total_expected_fees ?? existing.total_expected_fees,
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
      // Ignore mark/mid ticks — only Best Offer drives short UPNL
      if (msg.price_type !== 'ask') return
      const tickPx = Number(msg.price)
      if (!Number.isFinite(tickPx) || tickPx <= 0) return

      setTradeMap((prev) => {
        const next = new Map(prev)
        const existing = next.get(msg.trade_id)
        if (!existing) return prev

        const callOpen =
          String(existing.call_leg?.status || 'open').toLowerCase() === 'open'
        const putOpen =
          String(existing.put_leg?.status || 'open').toLowerCase() === 'open'

        const callLeg = existing.call_leg
          ? {
              ...existing.call_leg,
              current_premium:
                callOpen && existing.call_leg.symbol === msg.symbol
                  ? tickPx
                  : Number(
                      msg.call_premium ?? existing.call_leg.current_premium ?? 0,
                    ),
            }
          : existing.call_leg
        const putLeg = existing.put_leg
          ? {
              ...existing.put_leg,
              current_premium:
                putOpen && existing.put_leg.symbol === msg.symbol
                  ? tickPx
                  : Number(
                      msg.put_premium ?? existing.put_leg.current_premium ?? 0,
                    ),
            }
          : existing.put_leg

        const callEntry = Number(
          callLeg?.initial_premium ?? existing.call_entry_premium ?? 0,
        )
        const putEntry = Number(
          putLeg?.initial_premium ?? existing.put_entry_premium ?? 0,
        )
        const callPrem = Number(callLeg?.current_premium ?? 0)
        const putPrem = Number(putLeg?.current_premium ?? 0)
        const callQty = Math.abs(
          Number(
            msg.call_quantity ??
              existing.call_quantity ??
              callLeg?.quantity ??
              existing.quantity ??
              0,
          ),
        )
        const putQty = Math.abs(
          Number(
            msg.put_quantity ??
              existing.put_quantity ??
              putLeg?.quantity ??
              existing.quantity ??
              0,
          ),
        )
        const callChg = callEntry > 0 ? (callPrem / callEntry - 1) * 100 : 0
        const putChg = putEntry > 0 ? (putPrem / putEntry - 1) * 100 : 0

        const callMtm = callOpen ? shortLegUpnl(callEntry, callPrem, callQty) : 0
        const putMtm = putOpen ? shortLegUpnl(putEntry, putPrem, putQty) : 0
        const unrealized = callMtm + putMtm
        const realized = Number(existing.realized_pnl ?? 0)
        const gross = realized + unrealized
        const feesPaid = Number(existing.fees_paid ?? 0)
        const estExit = Number(existing.est_exit_fees ?? 0)
        const totalFees = Number(
          existing.total_expected_fees ?? feesPaid + estExit,
        )
        const net = gross - feesPaid - estExit

        // Bot plan: live offer vs trigger
        const callTrigger = Number(existing.call_trigger_price || 0)
        const putTrigger = Number(existing.put_trigger_price || 0)

        next.set(msg.trade_id, {
          ...existing,
          call_premium: callPrem,
          put_premium: putPrem,
          call_offer: callPrem,
          put_offer: putPrem,
          call_change_pct: callChg,
          put_change_pct: putChg,
          call_upnl: callMtm,
          put_upnl: putMtm,
          call_delta_mtm: callMtm,
          put_delta_mtm: putMtm,
          delta_upnl: unrealized,
          delta_mtm_pnl: unrealized,
          unrealized_pnl: unrealized,
          total_pnl: gross,
          gross_mtm: gross,
          net_mtm: net,
          fees_paid: feesPaid,
          est_exit_fees: estExit,
          total_expected_fees: totalFees,
          call_pct_to_trigger:
            callTrigger > 0 ? (callPrem / callTrigger) * 100 : existing.call_pct_to_trigger,
          put_pct_to_trigger:
            putTrigger > 0 ? (putPrem / putTrigger) * 100 : existing.put_pct_to_trigger,
          call_distance_to_trigger:
            callTrigger > 0 ? callTrigger - callPrem : existing.call_distance_to_trigger,
          put_distance_to_trigger:
            putTrigger > 0 ? putTrigger - putPrem : existing.put_distance_to_trigger,
          underlying_price:
            Number(msg.underlying_price) > 0
              ? Number(msg.underlying_price)
              : existing.underlying_price,
          call_leg: callLeg
            ? {
                ...callLeg,
                change_pct: callChg,
                leg_pnl: callOpen ? callMtm : callLeg.leg_pnl,
                current_premium: callPrem,
              }
            : callLeg,
          put_leg: putLeg
            ? {
                ...putLeg,
                change_pct: putChg,
                leg_pnl: putOpen ? putMtm : putLeg.leg_pnl,
                current_premium: putPrem,
              }
            : putLeg,
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
          // Prefer full leg snapshots from backend when present
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
