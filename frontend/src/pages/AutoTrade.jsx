import { useCallback, useEffect, useMemo, useState } from 'react'
import AdjustmentSlabs from '../components/AdjustmentSlabs'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import LoadingSpinner from '../components/ui/LoadingSpinner'
import Toast from '../components/ui/Toast'
import { useWebSocket } from '../hooks/useWebSocket'
import {
  disableAutoTrade,
  enableAutoTrade,
  getActiveTrades,
  getAutoTradeSettings,
  getAutoTradeStatus,
  getExpiries,
  saveAutoTradeSettings,
} from '../services/api'

const WS_URL = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`
const STATUS_POLL_MS = 5000
const UNDERLYINGS = ['BTC', 'ETH', 'XAU']

function applyStatusToForm(data, setters) {
  if (!data) return
  setters.setUnderlying(data.underlying || 'BTC')
  setters.setExpiryDte(Number(data.expiry_dte ?? 1))
  if (data.expiry_date_override) {
    setters.setSelectedExpiryDate(data.expiry_date_override)
  }
  setters.setQuantity(Number(data.quantity ?? 1))
  setters.setReEntryDelay(Number(data.re_entry_delay_minutes ?? 1))
  setters.setTpPct(String(data.tp_pct ?? 50))
  setters.setSlPct(String(data.sl_pct ?? 100))
  setters.setUniversalSlPct(String(data.universal_sl_pct ?? 200))
  setters.setSlippagePct(String(data.slippage_pct ?? 2))
  setters.setTradeType(data.trade_type || 'straddle')
  setters.setTargetPremium(Number(data.target_premium_per_side ?? 150))
  setters.setAdjLowPremiumExitEnabled(
    Boolean(data.adj_low_premium_exit_enabled),
  )
  setters.setAdjLowPremiumMinUsd(Number(data.adj_low_premium_min_usd ?? 150))
  setters.setIsEnabled(Boolean(data.is_enabled))
  setters.setLastError(data.last_error || null)
  setters.setLastTradeId(data.last_trade_id ?? null)
  const secs =
    data.seconds_until_entry != null && Number.isFinite(Number(data.seconds_until_entry))
      ? Math.max(0, Number(data.seconds_until_entry))
      : null
  setters.setSecondsUntilEntry(secs)
  setters.setSlabsInitial({
    mode: data.trigger_mode || 'slab',
    flat_pct: data.flat_trigger_pct ?? 150,
    slab_24h: data.slab_24h ?? 200,
    slab_12h: data.slab_12h ?? 175,
    slab_6h: data.slab_6h ?? 150,
    slab_lt6h: data.slab_lt6h ?? 150,
    premium_slab_300: data.premium_slab_300 ?? 150,
    premium_slab_200: data.premium_slab_200 ?? 160,
    premium_slab_100: data.premium_slab_100 ?? 180,
    premium_slab_lt100: data.premium_slab_lt100 ?? 200,
  })
}

export default function AutoTrade() {
  const { lastMessage } = useWebSocket(WS_URL)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [toggling, setToggling] = useState(false)
  const [disableOpen, setDisableOpen] = useState(false)
  const [toast, setToast] = useState(null)

  const [isEnabled, setIsEnabled] = useState(false)
  const [lastError, setLastError] = useState(null)
  const [lastTradeId, setLastTradeId] = useState(null)
  const [secondsUntilEntry, setSecondsUntilEntry] = useState(null)
  const [activeTrade, setActiveTrade] = useState(null)

  const [underlying, setUnderlying] = useState('BTC')
  const [expiryDte, setExpiryDte] = useState(1)
  const [expiryOptions, setExpiryOptions] = useState([])
  const [expiryLoading, setExpiryLoading] = useState(false)
  const [expiryError, setExpiryError] = useState(null)
  const [selectedExpiryDate, setSelectedExpiryDate] = useState(null)
  const [expiriesReady, setExpiriesReady] = useState(false)
  const [quantity, setQuantity] = useState(1)
  const [reEntryDelay, setReEntryDelay] = useState(1)
  const [tpPct, setTpPct] = useState('50')
  const [slPct, setSlPct] = useState('100')
  const [universalSlPct, setUniversalSlPct] = useState('200')
  const [slippagePct, setSlippagePct] = useState('2')
  const [tradeType, setTradeType] = useState('straddle')
  const [targetPremium, setTargetPremium] = useState(150)
  const [adjLowPremiumExitEnabled, setAdjLowPremiumExitEnabled] =
    useState(false)
  const [adjLowPremiumMinUsd, setAdjLowPremiumMinUsd] = useState(150)
  const [slabs, setSlabs] = useState(null)
  const [slabsInitial, setSlabsInitial] = useState(null)
  const [slabsKey, setSlabsKey] = useState(0)

  useEffect(() => {
    document.title = 'Delta Bot — Auto Trade'
  }, [])

  const formSetters = useMemo(
    () => ({
      setUnderlying,
      setExpiryDte,
      setSelectedExpiryDate,
      setQuantity,
      setReEntryDelay,
      setTpPct,
      setSlPct,
      setUniversalSlPct,
      setSlippagePct,
      setTradeType,
      setTargetPremium,
      setAdjLowPremiumExitEnabled,
      setAdjLowPremiumMinUsd,
      setIsEnabled,
      setLastError,
      setLastTradeId,
      setSecondsUntilEntry,
      setSlabsInitial,
    }),
    [],
  )

  const refreshStatus = useCallback(async () => {
    try {
      const [status, activeRes] = await Promise.all([
        getAutoTradeStatus(),
        getActiveTrades().catch(() => ({ trades: [] })),
      ])
      setIsEnabled(Boolean(status?.is_enabled))
      setLastError(status?.last_error || null)
      setLastTradeId(status?.last_trade_id ?? null)
      const secs =
        status?.seconds_until_entry != null &&
        Number.isFinite(Number(status.seconds_until_entry))
          ? Math.max(0, Number(status.seconds_until_entry))
          : null
      setSecondsUntilEntry(secs)

      const trades = activeRes?.trades || []
      const und = status?.underlying || underlying
      const match =
        trades.find((t) => String(t.underlying || '').toUpperCase() === und) ||
        trades[0] ||
        null
      setActiveTrade(match)
    } catch {
      // Keep last known status on poll failure
    }
  }, [underlying])

  const fetchExpiries = useCallback(async (und, preferredDate = null) => {
    const u = und || 'BTC'
    setExpiryLoading(true)
    setExpiryError(null)
    try {
      const data = await getExpiries(u)
      const rows = Array.isArray(data) ? data : []
      setExpiryOptions(rows)
      setSelectedExpiryDate((prev) => {
        const saved = preferredDate || prev
        if (saved && rows.find((e) => e.date === saved)) {
          return saved
        }
        if (rows.length > 0) {
          return rows[0].date
        }
        return null
      })
    } catch {
      setExpiryError('Could not load expiries from Delta Exchange')
    } finally {
      setExpiryLoading(false)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const data = await getAutoTradeSettings()
        if (cancelled) return
        applyStatusToForm(data, formSetters)
        setSlabsKey((k) => k + 1)
        const und = data?.underlying || 'BTC'
        await fetchExpiries(und, data?.expiry_date_override || null)
        if (cancelled) return
        const activeRes = await getActiveTrades().catch(() => ({ trades: [] }))
        if (cancelled) return
        const trades = activeRes?.trades || []
        setActiveTrade(
          trades.find((t) => String(t.underlying || '').toUpperCase() === und) ||
            trades[0] ||
            null,
        )
      } catch (err) {
        if (!cancelled) {
          setToast({
            type: 'error',
            message: err.message || 'Failed to load auto trade settings',
          })
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [formSetters, fetchExpiries])

  // Refetch when underlying changes (skip until after first load finishes)
  useEffect(() => {
    if (loading) return
    if (!expiriesReady) {
      setExpiriesReady(true)
      return
    }
    fetchExpiries(underlying)
  }, [underlying, loading, expiriesReady, fetchExpiries])

  useEffect(() => {
    const id = setInterval(() => {
      refreshStatus()
    }, STATUS_POLL_MS)
    return () => clearInterval(id)
  }, [refreshStatus])

  const countdownActive =
    secondsUntilEntry != null && secondsUntilEntry > 0

  // Local 1s countdown synced by 5s API poll / WS
  useEffect(() => {
    if (!countdownActive) return undefined
    const id = setInterval(() => {
      setSecondsUntilEntry((prev) => {
        if (prev == null || prev <= 0) return prev
        return prev - 1
      })
    }, 1000)
    return () => clearInterval(id)
  }, [countdownActive])

  useEffect(() => {
    if (!lastMessage?.type) return
    const t = lastMessage.type
    if (t === 'AUTO_TRADE_PLACED') {
      // Global toast handled in App.jsx
      refreshStatus()
    } else if (t === 'AUTO_TRADE_FAILED') {
      refreshStatus()
    } else if (t === 'AUTO_TRADE_WAITING') {
      const secs = Number(lastMessage.seconds_remaining)
      if (Number.isFinite(secs)) {
        setSecondsUntilEntry(Math.max(0, secs))
      }
      setIsEnabled(true)
    } else if (t === 'TRADE_UPDATE' || t === 'TRADE_CLOSED') {
      refreshStatus()
    }
  }, [lastMessage, refreshStatus])

  const onSlabsChange = useCallback((next) => {
    setSlabs(next)
  }, [])

  const buildPayload = () => {
    const s = slabs || slabsInitial || {}
    let dteFallback = Number(expiryDte) || 1
    if (selectedExpiryDate) {
      try {
        const today = new Date()
        today.setHours(0, 0, 0, 0)
        const exp = new Date(`${selectedExpiryDate}T00:00:00`)
        const diff = Math.round((exp - today) / 86400000)
        if (Number.isFinite(diff) && diff >= 0) {
          dteFallback = Math.min(30, diff)
        }
      } catch {
        dteFallback = 1
      }
    }
    return {
      underlying,
      expiry_dte: dteFallback,
      expiry_date_override: selectedExpiryDate || null,
      quantity: Math.max(1, Number(quantity) || 1),
      re_entry_delay_minutes: Math.max(0, Number(reEntryDelay) || 0),
      tp_pct: Number(tpPct) || 50,
      sl_pct: Number(slPct) || 100,
      universal_sl_pct: Number(universalSlPct) || 200,
      slippage_pct: Number(slippagePct) || 2,
      trigger_mode: s.mode || 'slab',
      flat_trigger_pct: Number(s.flat_pct) || 150,
      slab_24h: Number(s.slab_24h) || 200,
      slab_12h: Number(s.slab_12h) || 175,
      slab_6h: Number(s.slab_6h) || 150,
      slab_lt6h: Number(s.slab_lt6h) || 150,
      premium_slab_300: Number(s.premium_slab_300) || 150,
      premium_slab_200: Number(s.premium_slab_200) || 160,
      premium_slab_100: Number(s.premium_slab_100) || 180,
      premium_slab_lt100: Number(s.premium_slab_lt100) || 200,
      trade_type: tradeType,
      target_premium_per_side:
        tradeType === 'strangle' ? Number(targetPremium) || 150 : 150.0,
      adj_low_premium_exit_enabled: Boolean(adjLowPremiumExitEnabled),
      adj_low_premium_min_usd: Math.min(
        500,
        Math.max(10, Number(adjLowPremiumMinUsd) || 150),
      ),
    }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const updated = await saveAutoTradeSettings(buildPayload())
      applyStatusToForm(updated, formSetters)
      setSlabsKey((k) => k + 1)
      setToast({ type: 'success', message: '✅ Settings saved' })
    } catch (err) {
      setToast({
        type: 'error',
        message: err.message || 'Failed to save settings',
      })
    } finally {
      setSaving(false)
    }
  }

  const handleEnable = async () => {
    setToggling(true)
    try {
      // Persist current form first so enable uses latest params
      await saveAutoTradeSettings(buildPayload())
      const res = await enableAutoTrade()
      const settings = res?.settings || res
      if (settings) {
        applyStatusToForm(settings, formSetters)
        setSlabsKey((k) => k + 1)
      } else {
        setIsEnabled(true)
      }
      setToast({ type: 'success', message: '✅ Auto trade enabled!' })
      await refreshStatus()
    } catch (err) {
      setToast({
        type: 'error',
        message: err.message || 'Failed to enable auto trade',
      })
    } finally {
      setToggling(false)
    }
  }

  const handleDisableConfirm = async () => {
    setToggling(true)
    try {
      await disableAutoTrade()
      setIsEnabled(false)
      setSecondsUntilEntry(null)
      setDisableOpen(false)
      setToast({ type: 'info', message: '⏹ Auto trade disabled' })
      await refreshStatus()
    } catch (err) {
      setToast({
        type: 'error',
        message: err.message || 'Failed to disable auto trade',
      })
    } finally {
      setToggling(false)
    }
  }

  const tradeLabel = useMemo(() => {
    if (!activeTrade) return null
    const n = activeTrade.basket_number ?? activeTrade.trade_id ?? lastTradeId
    return n != null ? n : null
  }, [activeTrade, lastTradeId])

  const tradeTypeLabel =
    tradeType === 'strangle'
      ? `STRANGLE $${Number(targetPremium) || 150}/side`
      : 'STRADDLE ATM'

  const statusView = useMemo(() => {
    if (!isEnabled) {
      return {
        color: 'text-gray-400',
        bg: 'border-gray-600 bg-gray-900/60',
        text: '⚪ Disabled',
      }
    }
    if (activeTrade && tradeLabel != null) {
      return {
        color: 'text-green-400',
        bg: 'border-green-700/50 bg-green-950/30',
        text: `🟢 Active — ${tradeTypeLabel} — monitoring trade #${tradeLabel}`,
      }
    }
    if (lastError) {
      const retry =
        secondsUntilEntry != null && secondsUntilEntry > 0
          ? ` (retry in ${secondsUntilEntry}s)`
          : ''
      return {
        color: 'text-red-400',
        bg: 'border-red-700/50 bg-red-950/30',
        text: `🔴 Error: "${lastError}"${retry}`,
      }
    }
    if (secondsUntilEntry != null && secondsUntilEntry > 0) {
      return {
        color: 'text-yellow-300',
        bg: 'border-yellow-700/50 bg-yellow-950/20',
        text: `🔄 Auto Trade ON · ${tradeTypeLabel} · Next entry in ${secondsUntilEntry}s`,
      }
    }
    return {
      color: 'text-blue-300',
      bg: 'border-blue-700/50 bg-blue-950/30',
      text: `🔄 Auto Trade ON · ${tradeTypeLabel} · Ready to enter…`,
    }
  }, [
    isEnabled,
    activeTrade,
    tradeLabel,
    lastError,
    secondsUntilEntry,
    tradeTypeLabel,
  ])

  if (loading) {
    return (
      <main className="mx-auto flex max-w-3xl items-center justify-center px-4 py-20">
        <LoadingSpinner />
      </main>
    )
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 px-4 py-6">
      <h1 className="text-xl font-semibold text-white">🔄 Auto Trade</h1>

      {/* Mode / status */}
      <section
        className={`space-y-3 rounded-xl border p-4 ${statusView.bg}`}
      >
        <div className="text-xs font-semibold tracking-wide text-gray-400">
          AUTO TRADE MODE
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <span
            className={`rounded-full px-3 py-1 text-sm font-semibold ${
              isEnabled
                ? 'bg-green-600 text-white'
                : 'bg-gray-700 text-gray-300'
            }`}
          >
            {isEnabled ? '● ENABLED' : 'DISABLED ●'}
          </span>
        </div>
        <p className={`text-sm ${statusView.color}`}>{statusView.text}</p>
      </section>

      {/* Trade structure */}
      <section className="space-y-4 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">Trade Structure</h2>

        <div>
          <div className="mb-2 text-sm text-gray-300">Underlying</div>
          <div className="flex flex-wrap gap-2">
            {UNDERLYINGS.map((u) => (
              <button
                key={u}
                type="button"
                onClick={() => setUnderlying(u)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  underlying === u
                    ? 'bg-blue-500 text-white'
                    : 'bg-gray-900 text-gray-300 hover:bg-gray-700'
                }`}
              >
                {u}
              </button>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-2 text-sm text-gray-300">Expiry</div>
          <div className="flex max-w-xs items-center gap-2">
            <select
              value={selectedExpiryDate || ''}
              onChange={(e) => setSelectedExpiryDate(e.target.value || null)}
              disabled={expiryLoading}
              className="mt-0 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            >
              {expiryLoading && (
                <option value="">Loading expiries...</option>
              )}
              {!expiryLoading && expiryOptions.length === 0 && (
                <option value="">No expiries available</option>
              )}
              {expiryOptions.map((opt) => (
                <option key={opt.date} value={opt.date}>
                  {opt.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => fetchExpiries(underlying, selectedExpiryDate)}
              title="Refresh expiries"
              className="shrink-0 px-2 text-sm text-gray-400 hover:text-white"
            >
              ↻
            </button>
          </div>
          {expiryError && (
            <p className="mt-1 text-xs text-red-400">{expiryError}</p>
          )}
          {!expiryLoading && !expiryError && (
            <p className="mt-1 text-xs text-gray-500">Live from Delta Exchange</p>
          )}
        </div>

        <label className="block text-sm text-gray-300">
          Quantity
          <input
            type="number"
            min={1}
            step={1}
            value={quantity}
            onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
            className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
          />
        </label>

        <div>
          <div className="mb-2 text-sm text-gray-300">Trade Type</div>
          <div className="grid grid-cols-2 gap-3">
            <button
              type="button"
              onClick={() => setTradeType('straddle')}
              className={`rounded-lg border p-3 text-left transition-all ${
                tradeType === 'straddle'
                  ? 'border-blue-500 bg-blue-500/10 text-blue-400'
                  : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
              }`}
            >
              <div className="font-medium">Short Straddle</div>
              <div className="mt-1 text-xs opacity-70">
                ATM strike, same for Call & Put
              </div>
            </button>
            <button
              type="button"
              onClick={() => setTradeType('strangle')}
              className={`rounded-lg border p-3 text-left transition-all ${
                tradeType === 'strangle'
                  ? 'border-purple-500 bg-purple-500/10 text-purple-400'
                  : 'border-gray-700 bg-gray-800 text-gray-400 hover:border-gray-600'
              }`}
            >
              <div className="font-medium">Short Strangle</div>
              <div className="mt-1 text-xs opacity-70">
                OTM strikes, premium matching
              </div>
            </button>
          </div>
        </div>

        {tradeType === 'strangle' && (
          <div className="rounded-lg border border-purple-500/30 bg-gray-800 p-4">
            <label className="mb-2 block text-sm text-gray-400">
              Target Premium per Side ($)
            </label>
            <input
              type="number"
              value={targetPremium}
              onChange={(e) =>
                setTargetPremium(parseFloat(e.target.value) || 0)
              }
              className="w-full rounded bg-gray-700 px-3 py-2 text-white"
              placeholder="e.g. 150"
              min={1}
              max={10000}
            />
            <p className="mt-2 text-xs text-gray-500">
              Bot finds OTM Call & Put where premium ≈ ${targetPremium}
              <br />
              Strikes may be different for Call and Put
            </p>
          </div>
        )}

        <label className="block text-sm text-gray-300">
          Re-entry delay
          <div className="mt-1 flex max-w-xs items-center gap-2">
            <input
              type="number"
              min={0}
              step={1}
              value={reEntryDelay}
              onChange={(e) =>
                setReEntryDelay(Math.max(0, Number(e.target.value) || 0))
              }
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            <span className="shrink-0 text-gray-400">minutes</span>
          </div>
          <span className="mt-1 block text-xs text-gray-500">
            0 = immediate re-entry after exit
          </span>
        </label>
      </section>

      {/* Risk */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">Risk Settings</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm text-gray-300">
            Profit Target (% of max premium)
            <input
              type="number"
              min={1}
              max={500}
              step={1}
              value={tpPct}
              onChange={(e) => setTpPct(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
          </label>
          <label className="text-sm text-gray-300">
            Stop Loss (% of max premium)
            <input
              type="number"
              min={1}
              max={1000}
              step={1}
              value={slPct}
              onChange={(e) => setSlPct(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
          </label>
          <label className="text-sm text-gray-300">
            Delta SL (%)
            <input
              type="number"
              min={100}
              max={1000}
              step={1}
              value={universalSlPct}
              onChange={(e) => setUniversalSlPct(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
          </label>
          <label className="text-sm text-gray-300">
            Slippage Est (%)
            <input
              type="number"
              min={0}
              max={10}
              step={0.1}
              value={slippagePct}
              onChange={(e) => setSlippagePct(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
          </label>
        </div>
      </section>

      {/* Triggers */}
      {slabsInitial && (
        <AdjustmentSlabs
          key={slabsKey}
          onChange={onSlabsChange}
          defaultMode={slabsInitial.mode || 'slab'}
          initialValues={slabsInitial}
        />
      )}

      {/* Low-premium adjustment exit */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">
          Adjustment Exit on Low Premium
        </h2>
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={adjLowPremiumExitEnabled}
            onChange={(e) => setAdjLowPremiumExitEnabled(e.target.checked)}
            className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-blue-500"
          />
          <span className="text-sm text-gray-300">
            Close basket if replacement premium is too low
          </span>
        </label>
        {adjLowPremiumExitEnabled && (
          <div className="rounded-lg border border-amber-500/30 bg-gray-900/60 p-4">
            <label className="mb-2 block text-sm text-gray-400">
              Minimum replacement premium ($)
            </label>
            <input
              type="number"
              min={10}
              max={500}
              step={10}
              value={adjLowPremiumMinUsd}
              onChange={(e) =>
                setAdjLowPremiumMinUsd(Number(e.target.value) || 150)
              }
              className="w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            <p className="mt-2 text-xs text-gray-500">
              If an adjustment would require shorting a new leg below $
              {adjLowPremiumMinUsd}, the entire basket will be closed instead.
            </p>
          </div>
        )}
      </section>

      {/* Actions */}
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          disabled={saving || toggling}
          onClick={handleSave}
          className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {saving ? <LoadingSpinner size="sm" /> : null}
          💾 Save Settings
        </button>

        {!isEnabled ? (
          <button
            type="button"
            disabled={saving || toggling}
            onClick={handleEnable}
            className="inline-flex items-center gap-2 rounded-lg bg-green-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-green-500 disabled:opacity-50"
          >
            {toggling ? <LoadingSpinner size="sm" /> : null}
            🔄 Enable Auto Trade
          </button>
        ) : (
          <button
            type="button"
            disabled={saving || toggling}
            onClick={() => setDisableOpen(true)}
            className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-red-500 disabled:opacity-50"
          >
            ⏹ Disable Auto Trade
          </button>
        )}
      </div>

      <ConfirmDialog
        isOpen={disableOpen}
        title="Disable Auto Trade?"
        message="Disable auto trade? Current active trade will continue but no new trade will be placed after exit."
        confirmLabel="Yes, Disable"
        cancelLabel="Cancel"
        confirmDisabled={toggling}
        onConfirm={handleDisableConfirm}
        onCancel={() => setDisableOpen(false)}
      />

      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          onClose={() => setToast(null)}
        />
      )}
    </main>
  )
}
