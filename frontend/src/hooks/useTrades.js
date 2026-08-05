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
        const callEntry =
          msg.call_entry_premium != null
            ? Number(msg.call_entry_premium)
            : existing.call_entry_premium
        const putEntry =
          msg.put_entry_premium != null
            ? Number(msg.put_entry_premium)
            : existing.put_entry_premium
        const callLeg = existing.call_leg
          ? {
              ...existing.call_leg,
              initial_premium:
                callEntry != null
                  ? callEntry
                  : existing.call_leg.initial_premium,
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
              initial_premium:
                putEntry != null ? putEntry : existing.put_leg.initial_premium,
              current_premium: msg.put_premium ?? existing.put_leg.current_premium,
              change_pct: msg.put_change_pct ?? existing.put_leg.change_pct,
              leg_pnl:
                msg.put_delta_mtm != null
                  ? msg.put_delta_mtm
                  : existing.put_leg.leg_pnl,
            }
          : existing.put_leg
        const mergedCallLeg = msg.call_leg
          ? {
              ...(existing.call_leg || {}),
              ...msg.call_leg,
              initial_premium:
                callEntry != null
                  ? callEntry
                  : msg.call_leg.initial_premium,
            }
          : callLeg
            ? {
                ...callLeg,
                leg_pnl: msg.call_upnl ?? msg.call_delta_mtm ?? callLeg.leg_pnl,
              }
            : existing.call_leg
        const mergedPutLeg = msg.put_leg
          ? {
              ...(existing.put_leg || {}),
              ...msg.put_leg,
              initial_premium:
                putEntry != null ? putEntry : msg.put_leg.initial_premium,
            }
          : putLeg
            ? {
                ...putLeg,
                leg_pnl: msg.put_upnl ?? msg.put_delta_mtm ?? putLeg.leg_pnl,
              }
            : existing.put_leg
        next.set(msg.trade_id, {
          ...existing,
          ...msg,
          call_leg: mergedCallLeg,
          put_leg: mergedPutLeg,
          // Explicit overwrite — never keep stale post-adjustment baselines
          call_entry_premium:
            msg.call_entry_premium != null
              ? Number(msg.call_entry_premium)
              : existing.call_entry_premium,
          put_entry_premium:
            msg.put_entry_premium != null
              ? Number(msg.put_entry_premium)
              : existing.put_entry_premium,
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
          leg_history: msg.leg_history || existing.leg_history,
          call_upnl: msg.call_upnl ?? msg.call_delta_mtm,
          put_upnl: msg.put_upnl ?? msg.put_delta_mtm,
          delta_upnl: msg.delta_upnl ?? msg.delta_mtm_pnl,
          delta_mtm_pnl: msg.delta_mtm_pnl ?? msg.delta_upnl,
          calculated_pnl: msg.calculated_pnl,
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
      // Offer display only — NEVER recalculate basket MTM here.
      // MTM comes exclusively from TRADE_UPDATE / /active (realized + Delta UPL).
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

        const callPrem =
          callOpen && existing.call_leg?.symbol === msg.symbol
            ? tickPx
            : Number(
                msg.call_premium ?? existing.call_leg?.current_premium ?? existing.call_premium ?? 0,
              )
        const putPrem =
          putOpen && existing.put_leg?.symbol === msg.symbol
            ? tickPx
            : Number(
                msg.put_premium ?? existing.put_leg?.current_premium ?? existing.put_premium ?? 0,
              )

        const callEntry = Number(
          existing.call_entry_premium ?? existing.call_leg?.initial_premium ?? 0,
        )
        const putEntry = Number(
          existing.put_entry_premium ?? existing.put_leg?.initial_premium ?? 0,
        )
        const callQty = Math.abs(
          Number(
            msg.call_quantity ??
              existing.call_quantity ??
              existing.call_leg?.quantity ??
              0,
          ),
        )
        const putQty = Math.abs(
          Number(
            msg.put_quantity ??
              existing.put_quantity ??
              existing.put_leg?.quantity ??
              0,
          ),
        )
        const callChg = callEntry > 0 ? (callPrem / callEntry - 1) * 100 : 0
        const putChg = putEntry > 0 ? (putPrem / putEntry - 1) * 100 : 0
        // Per-leg display only (not basket NET MTM)
        const callLegPnl = callOpen
          ? shortLegUpnl(callEntry, callPrem, callQty)
          : existing.call_leg?.leg_pnl
        const putLegPnl = putOpen
          ? shortLegUpnl(putEntry, putPrem, putQty)
          : existing.put_leg?.leg_pnl

        const callTrigger = Number(existing.call_trigger_price || 0)
        const putTrigger = Number(existing.put_trigger_price || 0)

        next.set(msg.trade_id, {
          ...existing,
          // Live offers + change% only — leave gross_mtm/net_mtm/upnl untouched
          call_premium: callPrem,
          put_premium: putPrem,
          call_offer: callPrem,
          put_offer: putPrem,
          call_change_pct: callChg,
          put_change_pct: putChg,
          call_pct_to_trigger:
            callTrigger > 0
              ? (callPrem / callTrigger) * 100
              : existing.call_pct_to_trigger,
          put_pct_to_trigger:
            putTrigger > 0
              ? (putPrem / putTrigger) * 100
              : existing.put_pct_to_trigger,
          call_distance_to_trigger:
            callTrigger > 0
              ? callTrigger - callPrem
              : existing.call_distance_to_trigger,
          put_distance_to_trigger:
            putTrigger > 0
              ? putTrigger - putPrem
              : existing.put_distance_to_trigger,
          underlying_price:
            Number(msg.underlying_price) > 0
              ? Number(msg.underlying_price)
              : existing.underlying_price,
          call_leg: existing.call_leg
            ? {
                ...existing.call_leg,
                current_premium: callPrem,
                change_pct: callChg,
                leg_pnl: callLegPnl,
              }
            : existing.call_leg,
          put_leg: existing.put_leg
            ? {
                ...existing.put_leg,
                current_premium: putPrem,
                change_pct: putChg,
                leg_pnl: putLegPnl,
              }
            : existing.put_leg,
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
