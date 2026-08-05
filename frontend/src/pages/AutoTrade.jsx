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
  saveAutoTradeSettings,
} from '../services/api'

const WS_URL = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`
const STATUS_POLL_MS = 5000
const UNDERLYINGS = ['BTC', 'ETH', 'XAU']

const EXPIRY_OPTIONS = [
  { value: 0, label: '0DTE (Today)' },
  { value: 1, label: '1DTE (Tomorrow)' },
  { value: 2, label: '2DTE' },
  { value: 7, label: '7DTE' },
  { value: 30, label: '30DTE' },
]

function applyStatusToForm(data, setters) {
  if (!data) return
  setters.setUnderlying(data.underlying || 'BTC')
  setters.setExpiryDte(Number(data.expiry_dte ?? 1))
  setters.setQuantity(Number(data.quantity ?? 1))
  setters.setReEntryDelay(Number(data.re_entry_delay_minutes ?? 1))
  setters.setTpPct(String(data.tp_pct ?? 50))
  setters.setSlPct(String(data.sl_pct ?? 100))
  setters.setUniversalSlPct(String(data.universal_sl_pct ?? 200))
  setters.setSlippagePct(String(data.slippage_pct ?? 2))
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
  const [quantity, setQuantity] = useState(1)
  const [reEntryDelay, setReEntryDelay] = useState(1)
  const [tpPct, setTpPct] = useState('50')
  const [slPct, setSlPct] = useState('100')
  const [universalSlPct, setUniversalSlPct] = useState('200')
  const [slippagePct, setSlippagePct] = useState('2')
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
      setQuantity,
      setReEntryDelay,
      setTpPct,
      setSlPct,
      setUniversalSlPct,
      setSlippagePct,
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

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const data = await getAutoTradeSettings()
        if (cancelled) return
        applyStatusToForm(data, formSetters)
        setSlabsKey((k) => k + 1)
        const activeRes = await getActiveTrades().catch(() => ({ trades: [] }))
        if (cancelled) return
        const trades = activeRes?.trades || []
        const und = data?.underlying || 'BTC'
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
  }, [formSetters])

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
    return {
      underlying,
      expiry_dte: Number(expiryDte),
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
        text: `🟢 Active — monitoring trade #${tradeLabel}`,
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
        text: `🟡 Waiting — next entry in ${secondsUntilEntry}s`,
      }
    }
    return {
      color: 'text-blue-300',
      bg: 'border-blue-700/50 bg-blue-950/30',
      text: '🔵 Ready to enter…',
    }
  }, [isEnabled, activeTrade, tradeLabel, lastError, secondsUntilEntry])

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

        <label className="block text-sm text-gray-300">
          Expiry
          <select
            value={expiryDte}
            onChange={(e) => setExpiryDte(Number(e.target.value))}
            className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
          >
            {EXPIRY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </label>

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
