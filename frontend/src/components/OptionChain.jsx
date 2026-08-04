import { useEffect, useMemo, useRef, useState } from 'react'
import {
  OPTIONS_CONTRACT_VALUE,
  toUsdPnl,
} from '../utils/contractValue'

const WS_BASE = import.meta.env.VITE_WS_URL || 'ws://localhost:8000'
const FLASH_MS = 500

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function fmtStrike(v) {
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function fmtDelta(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toFixed(2)
}

function flashClass(dir) {
  if (dir === 'up') return 'bg-green-500/30 transition-colors duration-300'
  if (dir === 'down') return 'bg-red-500/30 transition-colors duration-300'
  return 'transition-colors duration-300'
}

function SkeletonRows() {
  return Array.from({ length: 9 }).map((_, i) => (
    <tr key={i} className="animate-pulse border-b border-gray-800">
      {Array.from({ length: 9 }).map((__, j) => (
        <td key={j} className="px-2 py-3">
          <div className="h-3 rounded bg-gray-700/70" />
        </td>
      ))}
    </tr>
  ))
}

function markAtm(chain, price) {
  if (!chain.length || !Number.isFinite(price) || price <= 0) {
    return chain.map((row) => ({ ...row, atm: false }))
  }
  let best = chain[0]
  let bestDist = Math.abs(Number(best.strike) - price)
  for (const row of chain) {
    const dist = Math.abs(Number(row.strike) - price)
    if (dist < bestDist) {
      best = row
      bestDist = dist
    }
  }
  return chain.map((row) => ({
    ...row,
    atm: Number(row.strike) === Number(best.strike),
  }))
}

/**
 * Live option chain via backend /ws/option-chain (Delta tickers forwarded).
 */
function useOptionChainWS(underlying, expiry, reloadKey) {
  const [chain, setChain] = useState([])
  const [currentPrice, setCurrentPrice] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [flashMap, setFlashMap] = useState({})
  const flashTimers = useRef({})
  const gotSnapshotRef = useRef(false)

  useEffect(() => {
    if (!underlying || !expiry) {
      setChain([])
      setCurrentPrice(null)
      setError('')
      setLoading(false)
      return undefined
    }

    setLoading(true)
    setError('')
    setChain([])
    setFlashMap({})
    gotSnapshotRef.current = false

    const url = `${WS_BASE}/ws/option-chain?underlying=${encodeURIComponent(underlying)}&expiry=${encodeURIComponent(expiry)}`
    // Temporary debug — helps confirm VITE_WS_URL / path
    // eslint-disable-next-line no-console
    console.info('Connecting to option-chain WS:', url)

    const ws = new WebSocket(url)
    let closed = false

    const triggerFlash = (symbol, prev, next) => {
      if (!Number.isFinite(prev) || !Number.isFinite(next) || prev === next) return
      const dir = next > prev ? 'up' : 'down'
      setFlashMap((m) => ({ ...m, [symbol]: dir }))
      if (flashTimers.current[symbol]) clearTimeout(flashTimers.current[symbol])
      flashTimers.current[symbol] = setTimeout(() => {
        setFlashMap((m) => {
          const copy = { ...m }
          delete copy[symbol]
          return copy
        })
      }, FLASH_MS)
    }

    ws.onmessage = (event) => {
      let msg
      try {
        msg = JSON.parse(event.data)
      } catch {
        return
      }
      if (!msg?.type || msg.type === 'ping') return

      if (msg.type === 'ERROR') {
        // Prefer server message; do not clobber if we already have a live chain
        if (!gotSnapshotRef.current) {
          setError(msg.message || 'Option chain WebSocket error')
          setLoading(false)
        }
        return
      }

      if (msg.type === 'CHAIN_SNAPSHOT') {
        const rows = Array.isArray(msg.chain) ? msg.chain : []
        gotSnapshotRef.current = true
        setChain(rows)
        const px = Number(msg.current_price)
        setCurrentPrice(Number.isFinite(px) && px > 0 ? px : null)
        setLoading(false)
        setError('')
        return
      }

      if (msg.type === 'TICK_UPDATE') {
        const symbol = msg.symbol
        const mark = parseFloat(msg.mark_price)
        const bid = parseFloat(msg.bid)
        const ask = parseFloat(msg.ask)
        const delta = parseFloat(msg.delta)
        setChain((prev) =>
          prev.map((row) => {
            if (row.call_symbol === symbol) {
              triggerFlash(symbol, Number(row.call_mark_price), mark)
              return {
                ...row,
                call_mark_price: mark,
                call_bid: bid,
                call_ask: ask,
                call_delta: delta,
              }
            }
            if (row.put_symbol === symbol) {
              triggerFlash(symbol, Number(row.put_mark_price), mark)
              return {
                ...row,
                put_mark_price: mark,
                put_bid: bid,
                put_ask: ask,
                put_delta: Math.abs(delta),
              }
            }
            return row
          }),
        )
        return
      }

      if (msg.type === 'PRICE_UPDATE') {
        const price = Number(msg.price)
        if (!Number.isFinite(price) || price <= 0) return
        setCurrentPrice(price)
        setChain((prev) => markAtm(prev, price))
      }
    }

    ws.onerror = () => {
      // Browsers fire onerror on many closes — only fail if we never got data
      if (!closed && !gotSnapshotRef.current) {
        setError('Live connection failed. Retry?')
        setLoading(false)
      }
    }

    ws.onclose = (event) => {
      if (closed) return
      // eslint-disable-next-line no-console
      console.info('Chain WS closed:', event.code, event.reason || '')
      if (!gotSnapshotRef.current && event.code !== 1000) {
        setError('Live connection lost. Click retry to reconnect.')
        setLoading(false)
      }
    }

    return () => {
      closed = true
      ws.close()
      Object.values(flashTimers.current).forEach((t) => clearTimeout(t))
      flashTimers.current = {}
    }
    // reloadKey forces reconnect
  }, [underlying, expiry, reloadKey])

  return { chain, currentPrice, loading, error, flashMap }
}

/**
 * Props:
 * - underlying, expiry
 * - selectedCall, selectedPut
 * - onCallSelect, onPutSelect
 * - onChainMeta({ currentPrice })
 * - quantity (lots) — for real Delta USD max-profit estimate
 */
export default function OptionChain({
  underlying,
  expiry,
  selectedCall,
  selectedPut,
  onCallSelect,
  onPutSelect,
  onChainMeta,
  quantity = 1,
}) {
  const [reloadKey, setReloadKey] = useState(0)
  const { chain, currentPrice, loading, error, flashMap } = useOptionChainWS(
    underlying,
    expiry,
    reloadKey,
  )
  const atmRef = useRef(null)
  const didScrollRef = useRef(false)

  useEffect(() => {
    didScrollRef.current = false
  }, [underlying, expiry, reloadKey])

  useEffect(() => {
    onChainMeta?.({ currentPrice: Number(currentPrice || 0) })
  }, [currentPrice, onChainMeta])

  // Keep parent selection premiums in sync with live ticks
  useEffect(() => {
    if (selectedCall) {
      const live = chain.find((r) => r.strike === selectedCall.strike)
      if (
        live &&
        (live.call_mark_price !== selectedCall.call_mark_price ||
          live.call_bid !== selectedCall.call_bid ||
          live.call_ask !== selectedCall.call_ask)
      ) {
        onCallSelect?.(live)
      }
    }
    if (selectedPut) {
      const live = chain.find((r) => r.strike === selectedPut.strike)
      if (
        live &&
        (live.put_mark_price !== selectedPut.put_mark_price ||
          live.put_bid !== selectedPut.put_bid ||
          live.put_ask !== selectedPut.put_ask)
      ) {
        onPutSelect?.(live)
      }
    }
  }, [chain, selectedCall, selectedPut, onCallSelect, onPutSelect])

  useEffect(() => {
    if (didScrollRef.current) return
    if (!atmRef.current) return
    atmRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    didScrollRef.current = true
  }, [chain])

  const handleCallClick = (row) => {
    if (selectedCall?.strike === row.strike) onCallSelect?.(null)
    else onCallSelect?.(row)
  }

  const handlePutClick = (row) => {
    if (selectedPut?.strike === row.strike) onPutSelect?.(null)
    else onPutSelect?.(row)
  }

  const memoRows = useMemo(() => chain, [chain])
  const liveCall =
    selectedCall &&
    (memoRows.find((r) => r.strike === selectedCall.strike) || selectedCall)
  const livePut =
    selectedPut &&
    (memoRows.find((r) => r.strike === selectedPut.strike) || selectedPut)

  const totalPremium =
    (liveCall ? Number(liveCall.call_mark_price || 0) : 0) +
    (livePut ? Number(livePut.put_mark_price || 0) : 0)
  const qty = Math.max(1, Number(quantity) || 1)
  const maxProfitUsd = toUsdPnl(totalPremium, qty)

  const atmStrike = memoRows.find((r) => r.atm)?.strike

  if (!underlying || !expiry) {
    return (
      <div className="rounded-xl border border-gray-700 bg-gray-800/50 px-4 py-8 text-center text-sm text-gray-400">
        Select underlying and expiry to load the option chain.
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-amber-700/40 bg-gray-900 px-4 py-3 text-sm">
        <div className="flex flex-wrap items-center gap-3 text-gray-200">
          <span className="font-semibold text-white">
            {underlying}: $
            {currentPrice != null ? fmtMoney(currentPrice) : '—'}
          </span>
          <span className="text-green-400">↑ Live</span>
          <span className="text-amber-400">
            ATM Strike:{' '}
            {atmStrike != null ? `$${fmtStrike(atmStrike)}` : '—'}
          </span>
        </div>
      </div>

      <div className="max-h-[32rem] overflow-auto rounded-xl border border-gray-700 bg-gray-900">
        <table className="min-w-full text-xs text-gray-200">
          <thead className="sticky top-0 z-10 bg-gray-800 text-[11px] uppercase tracking-wide text-gray-400">
            <tr>
              <th
                colSpan={4}
                className="border-b border-gray-700 px-2 py-2 text-center text-green-400"
              >
                Call
              </th>
              <th className="border-b border-gray-700 px-2 py-2 text-center text-white">
                Strike
              </th>
              <th
                colSpan={4}
                className="border-b border-gray-700 px-2 py-2 text-center text-red-400"
              >
                Put
              </th>
            </tr>
            <tr>
              <th className="px-2 py-2 text-left">Bid</th>
              <th className="px-2 py-2 text-left">Ask</th>
              <th className="px-2 py-2 text-left">Delta</th>
              <th className="px-2 py-2 text-left">Mark$</th>
              <th className="px-2 py-2 text-center">Strike</th>
              <th className="px-2 py-2 text-right">Mark$</th>
              <th className="px-2 py-2 text-right">Delta</th>
              <th className="px-2 py-2 text-right">Bid</th>
              <th className="px-2 py-2 text-right">Ask</th>
            </tr>
          </thead>
          <tbody>
            {loading && <SkeletonRows />}
            {!loading && error && (
              <tr>
                <td colSpan={9} className="px-4 py-8 text-center">
                  <p className="mb-3 text-red-300">{error}</p>
                  <button
                    type="button"
                    onClick={() => setReloadKey((k) => k + 1)}
                    className="rounded-md bg-blue-500 px-3 py-1.5 text-sm text-white hover:bg-blue-400"
                  >
                    Failed to load chain. Retry?
                  </button>
                </td>
              </tr>
            )}
            {!loading &&
              !error &&
              memoRows.map((row) => {
                const isCallSel = selectedCall?.strike === row.strike
                const isPutSel = selectedPut?.strike === row.strike
                const isAtm = Boolean(row.atm)

                let rowBg = 'hover:bg-gray-800/80'
                if (row.highlight) rowBg = 'bg-yellow-500/10'
                if (isAtm) rowBg = 'bg-amber-900/20'
                if (isCallSel) rowBg = 'bg-green-500/10'
                if (isPutSel) rowBg = 'bg-red-500/10'
                if (isCallSel && isPutSel) rowBg = 'bg-blue-500/10'

                let border = 'border-b border-gray-800'
                if (isAtm) {
                  border = 'border-b border-gray-800 border-l-4 border-l-amber-500'
                }
                if (isCallSel) {
                  border = 'border-b border-gray-800 border-l-4 border-l-green-500'
                }
                if (isPutSel) {
                  border = 'border-b border-gray-800 border-l-4 border-l-red-500'
                }
                if (isCallSel && isPutSel) {
                  border = 'border-b border-gray-800 border-l-4 border-l-blue-500'
                }

                return (
                  <tr
                    key={row.strike}
                    ref={isAtm ? atmRef : undefined}
                    className={`${rowBg} ${border}`}
                    style={isAtm && !isCallSel && !isPutSel ? { borderLeftColor: '#f59e0b' } : undefined}
                  >
                    <td
                      colSpan={4}
                      className="cursor-pointer px-0"
                      onClick={() => handleCallClick(row)}
                    >
                      <div className="grid grid-cols-4 gap-0 px-2 py-2 text-left">
                        <span className={flashClass(flashMap[row.call_symbol])}>
                          {fmtMoney(row.call_bid)}
                        </span>
                        <span className={flashClass(flashMap[row.call_symbol])}>
                          {fmtMoney(row.call_ask)}
                        </span>
                        <span>{fmtDelta(row.call_delta)}</span>
                        <span
                          className={`font-medium text-green-300 ${flashClass(flashMap[row.call_symbol])}`}
                        >
                          {fmtMoney(row.call_mark_price)}
                        </span>
                      </div>
                    </td>
                    <td className="px-2 py-2 text-center text-sm font-bold text-white">
                      {fmtStrike(row.strike)}
                      {isAtm && (
                        <span className="ml-1 text-[10px] font-semibold text-amber-400">
                          ◀ ATM
                        </span>
                      )}
                    </td>
                    <td
                      colSpan={4}
                      className="cursor-pointer px-0"
                      onClick={() => handlePutClick(row)}
                    >
                      <div className="grid grid-cols-4 gap-0 px-2 py-2 text-right">
                        <span
                          className={`font-medium text-red-300 ${flashClass(flashMap[row.put_symbol])}`}
                        >
                          {fmtMoney(row.put_mark_price)}
                        </span>
                        <span>{fmtDelta(Math.abs(Number(row.put_delta || 0)))}</span>
                        <span className={flashClass(flashMap[row.put_symbol])}>
                          {fmtMoney(row.put_bid)}
                        </span>
                        <span className={flashClass(flashMap[row.put_symbol])}>
                          {fmtMoney(row.put_ask)}
                        </span>
                      </div>
                    </td>
                  </tr>
                )
              })}
            {!loading && !error && memoRows.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-8 text-center text-gray-400">
                  No option rows for this expiry.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {(liveCall || livePut) && (
        <div className="rounded-xl border border-gray-700 bg-gray-800 px-4 py-3 text-sm text-gray-200">
          {liveCall && (
            <div>
              📞 CALL: ${fmtStrike(liveCall.strike)} strike @ $
              {fmtMoney(liveCall.call_mark_price)} | Δ{' '}
              {fmtDelta(liveCall.call_delta)}
            </div>
          )}
          {livePut && (
            <div>
              📉 PUT: ${fmtStrike(livePut.strike)} strike @ $
              {fmtMoney(livePut.put_mark_price)} | Δ{' '}
              {fmtDelta(Math.abs(Number(livePut.put_delta || 0)))}
            </div>
          )}
          {liveCall && livePut && (
            <div className="mt-1 text-gray-400">
              Mark premium: ${fmtMoney(totalPremium)} × {qty} lot
              {qty > 1 ? 's' : ''} | Est. Max Profit:{' '}
              <span className="text-green-400">
                ${fmtMoney(maxProfitUsd)} USD
              </span>{' '}
              <span className="text-gray-500">
                (×{OPTIONS_CONTRACT_VALUE} contract value)
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
