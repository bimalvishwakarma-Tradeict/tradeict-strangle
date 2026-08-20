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
  getHedgePreview,
  getTargetPreview,
  getThetaPreview,
  saveAutoTradeSettings,
} from '../services/api'

const WS_URL = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`
const STATUS_POLL_MS = 5000
const PREVIEW_POLL_MS = 30000
const PREVIEW_DEBOUNCE_MS = 500
const UNDERLYINGS = ['BTC', 'ETH', 'XAU']

const PREVIEW_FAIL = {
  success: false,
  unavailable: true,
  message: 'unavailable - chain fetch failed',
}

function formatIvPct(iv) {
  const n = Number(iv)
  if (!Number.isFinite(n) || n <= 0) return '--'
  const pct = n > 5 ? n : n * 100
  return `${pct.toFixed(1)}%`
}

function formatMoney(n, digits = 2) {
  const v = Number(n)
  if (!Number.isFinite(v)) return '--'
  return `$${v.toFixed(digits)}`
}

function formatExpiryShort(iso) {
  if (!iso) return '--'
  try {
    const d = new Date(`${String(iso).slice(0, 10)}T12:00:00Z`)
    return d.toLocaleDateString('en-GB', {
      day: '2-digit',
      month: 'short',
      year: '2-digit',
      timeZone: 'UTC',
    })
  } catch {
    return String(iso)
  }
}

function applyStatusToForm(data, setters) {
  if (!data) return
  setters.setUnderlying(data.underlying || 'BTC')
  setters.setExpiryDte(Number(data.expiry_dte ?? 1))
  if (data.expiry_date_override) {
    setters.setSelectedExpiryDate(data.expiry_date_override)
  }
  setters.setQuantity(Number(data.quantity ?? 1))
  setters.setReEntryDelay(Number(data.re_entry_delay_minutes ?? 1))
  setters.setEntrySettlingSeconds(String(data.entry_settling_seconds ?? 60))
  setters.setAdjustmentSettlingSeconds(
    String(data.adjustment_settling_seconds ?? 20),
  )
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
  setters.setConversionModeEnabled(
    data.conversion_mode_enabled == null
      ? true
      : Boolean(data.conversion_mode_enabled),
  )
  setters.setMaxAdjustmentsPerBasket(
    data.max_adjustments_per_basket == null || data.max_adjustments_per_basket === ''
      ? ''
      : String(data.max_adjustments_per_basket),
  )
  setters.setPremiumCoverLossEnabled(
    Boolean(data.premium_cover_loss_enabled),
  )
  setters.setCombinedTriggerMode(
    Boolean(data.combined_trigger_mode),
  )
  setters.setHedgeEnabled(Boolean(data.hedge_enabled))
  setters.setHedgeExpiryMode(data.hedge_expiry_mode || 'monthly')
  setters.setHedgeExpiryDateOverride(data.hedge_expiry_date_override || '')
  setters.setHedgeExpiryDte(
    data.hedge_expiry_dte == null ? '' : String(data.hedge_expiry_dte),
  )
  setters.setHedgeTargetUsd(
    data.hedge_target_usd == null ? '' : String(data.hedge_target_usd),
  )
  setters.setHedgeStoplossUsd(
    data.hedge_stoploss_usd == null ? '' : String(data.hedge_stoploss_usd),
  )
  setters.setMarginBufferPct(String(data.margin_buffer_pct ?? 50))
  setters.setStrikeSelectionMode(data.strike_selection_mode || 'fixed_premium')
  setters.setThetaMultiplier(String(data.theta_multiplier ?? 3))
  setters.setTargetMode(data.target_mode || 'payoff_pct')
  setters.setTargetThetaPct(String(data.target_theta_pct ?? 150))
  setters.setCooldownAfterLossMinutes(
    String(data.cooldown_after_loss_minutes ?? 120),
  )
  setters.setOrderMarginPerLot(
    data.order_margin_per_lot == null || data.order_margin_per_lot === ''
      ? null
      : Number(data.order_margin_per_lot),
  )
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
  const [entrySettlingSeconds, setEntrySettlingSeconds] = useState('60')
  const [adjustmentSettlingSeconds, setAdjustmentSettlingSeconds] =
    useState('20')
  const [tpPct, setTpPct] = useState('50')
  const [slPct, setSlPct] = useState('100')
  const [universalSlPct, setUniversalSlPct] = useState('200')
  const [slippagePct, setSlippagePct] = useState('2')
  const [tradeType, setTradeType] = useState('straddle')
  const [targetPremium, setTargetPremium] = useState(150)
  const [adjLowPremiumExitEnabled, setAdjLowPremiumExitEnabled] =
    useState(false)
  const [adjLowPremiumMinUsd, setAdjLowPremiumMinUsd] = useState(150)
  const [conversionModeEnabled, setConversionModeEnabled] = useState(true)
  const [maxAdjustmentsPerBasket, setMaxAdjustmentsPerBasket] = useState('')
  const [premiumCoverLossEnabled, setPremiumCoverLossEnabled] = useState(false)
  const [combinedTriggerMode, setCombinedTriggerMode] = useState(false)
  const [hedgeEnabled, setHedgeEnabled] = useState(false)
  const [hedgeExpiryMode, setHedgeExpiryMode] = useState('monthly')
  const [hedgeExpiryDateOverride, setHedgeExpiryDateOverride] = useState('')
  const [hedgeExpiryDte, setHedgeExpiryDte] = useState('')
  const [hedgeTargetUsd, setHedgeTargetUsd] = useState('')
  const [hedgeStoplossUsd, setHedgeStoplossUsd] = useState('')
  const [marginBufferPct, setMarginBufferPct] = useState('50')
  const [strikeSelectionMode, setStrikeSelectionMode] =
    useState('fixed_premium')
  const [thetaMultiplier, setThetaMultiplier] = useState('3')
  const [targetMode, setTargetMode] = useState('payoff_pct')
  const [targetThetaPct, setTargetThetaPct] = useState('150')
  const [cooldownAfterLossMinutes, setCooldownAfterLossMinutes] =
    useState('120')
  const [orderMarginPerLot, setOrderMarginPerLot] = useState(null)
  const [hedgePreview, setHedgePreview] = useState(null)
  const [thetaPreview, setThetaPreview] = useState(null)
  const [targetPreview, setTargetPreview] = useState(null)
  const [previewLoading, setPreviewLoading] = useState(false)
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
      setEntrySettlingSeconds,
      setAdjustmentSettlingSeconds,
      setTpPct,
      setSlPct,
      setUniversalSlPct,
      setSlippagePct,
      setTradeType,
      setTargetPremium,
      setAdjLowPremiumExitEnabled,
      setAdjLowPremiumMinUsd,
      setConversionModeEnabled,
      setMaxAdjustmentsPerBasket,
      setPremiumCoverLossEnabled,
      setCombinedTriggerMode,
      setHedgeEnabled,
      setHedgeExpiryMode,
      setHedgeExpiryDateOverride,
      setHedgeExpiryDte,
      setHedgeTargetUsd,
      setHedgeStoplossUsd,
      setMarginBufferPct,
      setStrikeSelectionMode,
      setThetaMultiplier,
      setTargetMode,
      setTargetThetaPct,
      setCooldownAfterLossMinutes,
      setOrderMarginPerLot,
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

  const fetchExpiries = useCallback(
    async (und, preferredDate = null, preferredDte = null) => {
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
          // Daily DTE settings have no date override — match by days-to-expiry
          if (
            preferredDte != null &&
            Number.isFinite(Number(preferredDte)) &&
            Number(preferredDte) <= 2
          ) {
            const today = new Date()
            today.setHours(0, 0, 0, 0)
            const match = rows.find((e) => {
              const exp = new Date(`${e.date}T00:00:00`)
              exp.setHours(0, 0, 0, 0)
              const d = Math.round((exp - today) / (1000 * 60 * 60 * 24))
              return d === Number(preferredDte)
            })
            if (match) return match.date
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
    },
    [],
  )

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
        await fetchExpiries(
          und,
          data?.expiry_date_override || null,
          data?.expiry_dte ?? null,
        )
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
    let dteDays = Number(expiryDte) || 1
    if (selectedExpiryDate) {
      try {
        const today = new Date()
        today.setHours(0, 0, 0, 0)
        const expDate = new Date(`${selectedExpiryDate}T00:00:00`)
        expDate.setHours(0, 0, 0, 0)
        const diff = Math.round((expDate - today) / (1000 * 60 * 60 * 24))
        if (Number.isFinite(diff) && diff >= 0) {
          dteDays = Math.min(90, diff)
        }
      } catch {
        dteDays = 1
      }
    }
    // Daily (0/1/2DTE): store integer only — always relative to NOW at entry.
    // Weekly/Monthly: keep exact date override (user chose that week/month).
    const payloadExpiry =
      dteDays <= 2
        ? { expiry_dte: dteDays, expiry_date_override: null }
        : {
            expiry_dte: dteDays,
            expiry_date_override: selectedExpiryDate || null,
          }
    return {
      underlying,
      ...payloadExpiry,
      quantity: Math.max(1, Number(quantity) || 1),
      re_entry_delay_minutes: Math.max(0, Number(reEntryDelay) || 0),
      entry_settling_seconds: Math.min(
        300,
        Math.max(0, Number(entrySettlingSeconds) || 0),
      ),
      adjustment_settling_seconds: Math.min(
        300,
        Math.max(0, Number(adjustmentSettlingSeconds) || 0),
      ),
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
      conversion_mode_enabled: Boolean(conversionModeEnabled),
      max_adjustments_per_basket: conversionModeEnabled
        ? null
        : maxAdjustmentsPerBasket === '' || maxAdjustmentsPerBasket == null
          ? null
          : Math.max(1, Math.min(50, Number(maxAdjustmentsPerBasket) || 1)),
      premium_cover_loss_enabled: Boolean(premiumCoverLossEnabled),
      combined_trigger_mode: Boolean(combinedTriggerMode),
      hedge_enabled: Boolean(hedgeEnabled),
      hedge_expiry_mode: hedgeExpiryMode || 'monthly',
      hedge_expiry_date_override:
        hedgeExpiryMode === 'date' && hedgeExpiryDateOverride
          ? hedgeExpiryDateOverride
          : null,
      hedge_expiry_dte:
        hedgeExpiryMode === 'dte' && hedgeExpiryDte !== ''
          ? Math.max(0, Math.min(365, Number(hedgeExpiryDte) || 0))
          : null,
      hedge_target_usd:
        hedgeTargetUsd === '' || hedgeTargetUsd == null
          ? null
          : Number(hedgeTargetUsd),
      hedge_stoploss_usd:
        hedgeStoplossUsd === '' || hedgeStoplossUsd == null
          ? null
          : Number(hedgeStoplossUsd),
      margin_buffer_pct: Math.min(
        200,
        Math.max(0, Number(marginBufferPct) || 0),
      ),
      strike_selection_mode: hedgeEnabled
        ? strikeSelectionMode || 'fixed_premium'
        : 'fixed_premium',
      theta_multiplier: Math.min(
        20,
        Math.max(0.01, Number(thetaMultiplier) || 3),
      ),
      target_mode: hedgeEnabled
        ? targetMode || 'payoff_pct'
        : 'payoff_pct',
      target_theta_pct: Math.min(
        1000,
        Math.max(10, Number(targetThetaPct) || 150),
      ),
      cooldown_after_loss_minutes: Math.min(
        1440,
        Math.max(0, Number(cooldownAfterLossMinutes) || 0),
      ),
    }
  }

  const capitalPerLotDisplay = useMemo(() => {
    if (
      hedgePreview?.capital_per_lot != null &&
      Number.isFinite(Number(hedgePreview.capital_per_lot))
    ) {
      return `$${Number(hedgePreview.capital_per_lot).toFixed(2)}`
    }
    if (orderMarginPerLot == null || !Number.isFinite(Number(orderMarginPerLot))) {
      return '--'
    }
    const buf = Math.min(200, Math.max(0, Number(marginBufferPct) || 0))
    const cpl = Number(orderMarginPerLot) * (1 + buf / 100)
    return Number.isFinite(cpl) ? `$${cpl.toFixed(2)}` : '--'
  }, [orderMarginPerLot, marginBufferPct, hedgePreview])

  const buildPreviewParams = useCallback(() => {
    const dte = Math.max(0, Number(expiryDte) || 1)
    const params = {
      underlying,
      quantity: Math.max(1, Number(quantity) || 1),
      hedge_expiry_mode: hedgeExpiryMode || 'monthly',
      margin_buffer_pct: Math.min(200, Math.max(0, Number(marginBufferPct) || 50)),
      theta_multiplier: Math.min(20, Math.max(0.01, Number(thetaMultiplier) || 3)),
      target_theta_pct: Math.min(1000, Math.max(10, Number(targetThetaPct) || 150)),
      expiry_dte: dte,
    }
    if (hedgeExpiryDateOverride) {
      params.hedge_expiry_date_override = hedgeExpiryDateOverride
    }
    if (hedgeExpiryDte !== '' && hedgeExpiryDte != null) {
      params.hedge_expiry_dte = Number(hedgeExpiryDte)
    }
    // Match auto-trade save rules: daily 0/1/2 DTE never send a calendar override
    if (dte > 2 && selectedExpiryDate) {
      params.expiry_date_override = selectedExpiryDate
    }
    return params
  }, [
    underlying,
    quantity,
    hedgeExpiryMode,
    hedgeExpiryDateOverride,
    hedgeExpiryDte,
    marginBufferPct,
    thetaMultiplier,
    targetThetaPct,
    expiryDte,
    selectedExpiryDate,
  ])

  const refreshPreviews = useCallback(async () => {
    setPreviewLoading(true)
    try {
      const params = buildPreviewParams()
      const [hp, tp, tgp] = await Promise.all([
        getHedgePreview(params),
        getThetaPreview(params),
        getTargetPreview(params),
      ])
      // Never keep stale success data — replace with unavailable on failure
      setHedgePreview(hp?.unavailable || hp?.success === false ? { ...PREVIEW_FAIL, ...hp } : hp)
      setThetaPreview(tp?.unavailable || tp?.success === false ? { ...PREVIEW_FAIL, ...tp } : tp)
      setTargetPreview(
        tgp?.unavailable || tgp?.success === false ? { ...PREVIEW_FAIL, ...tgp } : tgp,
      )
      if (hp?.order_margin_per_lot != null && !hp?.unavailable) {
        setOrderMarginPerLot(Number(hp.order_margin_per_lot))
      } else {
        setOrderMarginPerLot(null)
      }
    } catch {
      setHedgePreview(PREVIEW_FAIL)
      setThetaPreview(PREVIEW_FAIL)
      setTargetPreview(PREVIEW_FAIL)
      setOrderMarginPerLot(null)
    } finally {
      setPreviewLoading(false)
    }
  }, [buildPreviewParams])

  useEffect(() => {
    if (loading) return undefined
    const debounceId = setTimeout(refreshPreviews, PREVIEW_DEBOUNCE_MS)
    const pollId = setInterval(refreshPreviews, PREVIEW_POLL_MS)
    return () => {
      clearTimeout(debounceId)
      clearInterval(pollId)
    }
  }, [loading, refreshPreviews])

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

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm text-gray-300">
            Entry Settling (seconds)
            <input
              type="number"
              min={0}
              max={300}
              step={1}
              value={entrySettlingSeconds}
              onChange={(e) => setEntrySettlingSeconds(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            <span className="mt-1 block text-xs text-gray-500">
              Pause profit-target and adjustment checks after a new entry.
              Stop loss is never paused. 0–300.
            </span>
          </label>
          <label className="block text-sm text-gray-300">
            Adjustment Settling (seconds)
            <input
              type="number"
              min={0}
              max={300}
              step={1}
              value={adjustmentSettlingSeconds}
              onChange={(e) => setAdjustmentSettlingSeconds(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            <span className="mt-1 block text-xs text-gray-500">
              Pause profit-target and adjustment checks for this long after an
              adjustment, while the new leg settles and slaves finish mirroring.
              Stop loss is never paused.
            </span>
          </label>
        </div>
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

      {/* Combined Premium Trigger */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">
          Combined Trigger Mode
        </h2>
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={combinedTriggerMode}
            onChange={async (e) => {
              const on = e.target.checked
              setCombinedTriggerMode(on)
              // Persist immediately — toggle alone used to leave DB at False
              // until Save, so on_tick kept using individual triggers.
              try {
                const updated = await saveAutoTradeSettings({
                  ...buildPayload(),
                  combined_trigger_mode: on,
                })
                applyStatusToForm(updated, formSetters)
                setToast({
                  type: 'success',
                  message: on
                    ? '✅ Combined trigger ON'
                    : '✅ Combined trigger OFF',
                })
              } catch (err) {
                setCombinedTriggerMode(!on)
                setToast({
                  type: 'error',
                  message:
                    err.message || 'Failed to save combined trigger mode',
                })
              }
            }}
            className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-blue-500"
            title="Adjustment triggers when TOTAL premium (call+put) reaches trigger%, not individual legs"
          />
          <span className="text-sm text-gray-300">
            Combined Premium Trigger{' '}
            <span className="text-gray-500">
              ({combinedTriggerMode ? 'ON' : 'OFF'})
            </span>
          </span>
        </label>
        <p className="text-xs text-gray-500">
          When ON: adjustment triggers when TOTAL premium (call + put)
          reaches trigger %, not individual legs. The leg with the higher
          % increase is adjusted. Default OFF = per-leg triggers.
        </p>
      </section>

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
              {adjLowPremiumMinUsd}, special handling applies (conversion or
              exit — see Conversion Mode below).
            </p>
          </div>
        )}
      </section>

      {/* Conversion Mode + Max Adjustments */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">Conversion Mode</h2>
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={conversionModeEnabled}
            onChange={(e) => {
              const on = e.target.checked
              setConversionModeEnabled(on)
              if (on) setMaxAdjustmentsPerBasket('')
            }}
            className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-blue-500"
          />
          <span className="text-sm text-gray-300">
            Conversion Mode{' '}
            <span className="text-gray-500">
              ({conversionModeEnabled ? 'ON' : 'OFF'})
            </span>
          </span>
        </label>
        <p className="text-xs text-gray-500">
          When ON: bot buys a hedge and restructures basket if replacement
          premium is too low. When OFF: bot exits the basket instead.
        </p>
        {!conversionModeEnabled && (
          <div className="rounded-lg border border-orange-500/30 bg-gray-900/60 p-4">
            <label className="mb-2 block text-sm text-gray-400">
              Max Adjustments Per Basket
            </label>
            <select
              value={maxAdjustmentsPerBasket === '' ? 'unlimited' : maxAdjustmentsPerBasket}
              onChange={(e) => {
                const v = e.target.value
                setMaxAdjustmentsPerBasket(v === 'unlimited' ? '' : v)
              }}
              className="w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            >
              <option value="unlimited">Unlimited</option>
              <option value="1">1</option>
              <option value="2">2</option>
              <option value="3">3</option>
            </select>
            <p className="mt-2 text-xs text-gray-500">
              Bot will exit the basket after this many adjustments instead of
              adjusting again. Only active when Conversion Mode is OFF.
            </p>
          </div>
        )}
      </section>

      {/* Premium Cover Loss */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">Premium Cover Loss</h2>
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={premiumCoverLossEnabled}
            onChange={(e) => setPremiumCoverLossEnabled(e.target.checked)}
            className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-blue-500"
          />
          <span className="text-sm text-gray-300">
            Premium Cover Loss{' '}
            <span className="text-gray-500">
              ({premiumCoverLossEnabled ? 'ON' : 'OFF'})
            </span>
          </span>
        </label>
        <p className="text-xs text-gray-500">
          When ON: bot targets new strike premium equal to the realized loss
          on the triggered leg (e.g. if CALL lost $150 in premium points,
          bot finds new CALL with ~$150 premium). Helps recover the loss if
          both legs expire worthless, and gives more breathing room before
          next trigger.
        </p>
      </section>

      {/* ===== HEDGE MODE (config only — engine not wired yet) ===== */}
      <section className="space-y-3 rounded-xl border border-emerald-700/40 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">HEDGE MODE</h2>
        <p className="text-xs text-gray-500">
          A permanent long ATM straddle held alongside daily short baskets.
          The hedge is NOT closed when a basket closes — it outlives many
          baskets and has its own target / stop / lifecycle.
        </p>
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            checked={hedgeEnabled}
            onChange={(e) => {
              const on = e.target.checked
              setHedgeEnabled(on)
              if (!on) {
                setStrikeSelectionMode('fixed_premium')
                setTargetMode('payoff_pct')
              }
            }}
            className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500"
          />
          <span className="text-sm text-gray-300">
            Enable Hedge Mode{' '}
            <span className="text-gray-500">
              ({hedgeEnabled ? 'ON' : 'OFF'})
            </span>
          </span>
        </label>

        <div
          className={`grid gap-3 sm:grid-cols-2 ${
            hedgeEnabled ? '' : 'pointer-events-none opacity-40'
          }`}
        >
          <label className="block text-sm text-gray-300">
            Hedge expiry
            <select
              value={hedgeExpiryMode}
              onChange={(e) => setHedgeExpiryMode(e.target.value)}
              disabled={!hedgeEnabled}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
            >
              <option value="monthly">Nearest monthly</option>
              <option value="date">Fixed calendar date</option>
              <option value="dte">Fixed DTE</option>
            </select>
          </label>
          {hedgeExpiryMode === 'date' && (
            <label className="block text-sm text-gray-300">
              Hedge expiry date
              <input
                type="date"
                value={hedgeExpiryDateOverride}
                onChange={(e) => setHedgeExpiryDateOverride(e.target.value)}
                disabled={!hedgeEnabled}
                className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
              />
            </label>
          )}
          {hedgeExpiryMode === 'dte' && (
            <label className="block text-sm text-gray-300">
              Hedge DTE
              <input
                type="number"
                min={0}
                max={365}
                value={hedgeExpiryDte}
                onChange={(e) => setHedgeExpiryDte(e.target.value)}
                disabled={!hedgeEnabled}
                className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
              />
            </label>
          )}
          <label className="block text-sm text-gray-300">
            Hedge target ($)
            <input
              type="number"
              min={0.01}
              step={1}
              value={hedgeTargetUsd}
              onChange={(e) => setHedgeTargetUsd(e.target.value)}
              disabled={!hedgeEnabled}
              placeholder="Required when ON"
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
            />
          </label>
          <label className="block text-sm text-gray-300">
            Hedge stop loss ($)
            <input
              type="number"
              min={0.01}
              step={1}
              value={hedgeStoplossUsd}
              onChange={(e) => setHedgeStoplossUsd(e.target.value)}
              disabled={!hedgeEnabled}
              placeholder="Required when ON"
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
            />
          </label>
          <label className="block text-sm text-gray-300 sm:col-span-2">
            Margin buffer (%)
            <div className="mt-1 flex flex-wrap items-center gap-3">
              <input
                type="number"
                min={0}
                max={200}
                step={1}
                value={marginBufferPct}
                onChange={(e) => setMarginBufferPct(e.target.value)}
                disabled={!hedgeEnabled}
                className="w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
              />
              <span className="text-xs text-gray-400">
                Capital per lot:{' '}
                <span className="font-mono text-gray-200">
                  {capitalPerLotDisplay}
                </span>
              </span>
            </div>
            <span className="mt-1 block text-xs text-gray-500">
              capital_per_lot = order_margin × (1 + buffer%). Shows &quot;--&quot;
              until live order margin is available.
            </span>
          </label>
        </div>

        {/* Live hedge preview — always visible (hypothetical before any hedge) */}
        <div className="mt-2 rounded-lg border border-gray-700/80 bg-gray-900/50 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-emerald-400/90">
              Live preview (updates with spot)
            </p>
            {previewLoading ? (
              <span className="text-[10px] text-gray-500">Refreshing…</span>
            ) : null}
          </div>
          {hedgePreview?.unavailable || hedgePreview?.success === false ? (
            <p className="text-sm text-amber-400">
              {hedgePreview?.message || 'unavailable - chain fetch failed'}
            </p>
          ) : hedgePreview?.success ? (
            <div className="space-y-1.5 font-mono text-xs text-gray-300">
              <p className="text-sm text-white">
                Hedge would be:{' '}
                <span className="text-emerald-300">
                  {hedgePreview.underlying || underlying}{' '}
                  {Math.round(Number(hedgePreview.strike))} straddle
                </span>
                , {formatExpiryShort(hedgePreview.expiry_date)},{' '}
                {hedgePreview.quantity} lot
                {Number(hedgePreview.quantity) === 1 ? '' : 's'}
              </p>
              <p>
                Estimated cost{' '}
                <span className="text-white">
                  {formatMoney(hedgePreview.cost_usd)}
                </span>
              </p>
              <p>
                Estimated daily theta{' '}
                <span className="text-rose-300">
                  -{formatMoney(Math.abs(Number(hedgePreview.daily_theta_usd)))}
                </span>
              </p>
              <p>
                Current IV{' '}
                <span className="text-white">
                  {formatIvPct(hedgePreview.call_iv)} /{' '}
                  {formatIvPct(hedgePreview.put_iv)}
                </span>{' '}
                <span className="text-gray-500">
                  (
                  {hedgePreview.iv_percentile?.message ||
                    'percentile: collecting data'}
                  )
                </span>
              </p>
              {hedgePreview.iv_ok ? (
                <p className="text-emerald-400">
                  [ok] IV is in the lower range — reasonable time to buy long
                  vol
                </p>
              ) : hedgePreview.iv_percentile?.percentile != null ? (
                <p className="text-amber-400">
                  [!] IV is elevated vs recent history — long vol may be
                  expensive
                </p>
              ) : (
                <p className="text-gray-500">
                  Percentile: collecting data (need 30+ daily IV samples)
                </p>
              )}
            </div>
          ) : (
            <p className="text-sm text-gray-500">Loading preview…</p>
          )}
        </div>
      </section>

      <section className="space-y-3 rounded-xl border border-emerald-700/40 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">
          SHORT STRIKE SELECTION
        </h2>
        <p className="text-xs text-gray-500">
          How daily short strikes are chosen when hedge mode is on. Theta-based
          preview uses a hypothetical ATM hedge from the settings above — no
          live hedge required.
        </p>
        <div className="space-y-2">
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="radio"
              name="strike_selection_mode"
              checked={strikeSelectionMode === 'fixed_premium'}
              onChange={() => setStrikeSelectionMode('fixed_premium')}
              className="mt-1"
            />
            <span className="text-sm text-gray-300">
              Fixed premium (current behaviour)
            </span>
          </label>
          <label
            className={`flex items-start gap-3 ${
              hedgeEnabled
                ? 'cursor-pointer'
                : 'cursor-not-allowed opacity-40'
            }`}
            title={
              hedgeEnabled
                ? undefined
                : 'Enable Hedge Mode first — theta-based strike selection needs hedge mode'
            }
          >
            <input
              type="radio"
              name="strike_selection_mode"
              checked={strikeSelectionMode === 'theta_based'}
              disabled={!hedgeEnabled}
              onChange={() => setStrikeSelectionMode('theta_based')}
              className="mt-1 disabled:cursor-not-allowed"
            />
            <span className="text-sm text-gray-300">
              Theta-based{' '}
              <span className="text-gray-500">
                (short premium ≈ hedge theta × multiplier)
              </span>
            </span>
          </label>
        </div>
        <label
          className={`block text-sm text-gray-300 ${
            hedgeEnabled && strikeSelectionMode === 'theta_based'
              ? ''
              : 'opacity-40'
          }`}
          title={
            !hedgeEnabled
              ? 'Enable Hedge Mode first — theta multiplier needs hedge mode'
              : undefined
          }
        >
          Theta multiplier
          <input
            type="number"
            min={0.01}
            max={20}
            step={0.1}
            value={thetaMultiplier}
            disabled={!hedgeEnabled || strikeSelectionMode !== 'theta_based'}
            onChange={(e) => setThetaMultiplier(e.target.value)}
            className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
          />
        </label>

        <div className="rounded-lg border border-gray-700/80 bg-gray-900/50 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-emerald-400/90">
              Live preview
            </p>
            {previewLoading ? (
              <span className="text-[10px] text-gray-500">Refreshing…</span>
            ) : null}
          </div>
          {thetaPreview?.unavailable || thetaPreview?.success === false ? (
            <p className="text-sm text-amber-400">
              {thetaPreview?.message || 'unavailable - chain fetch failed'}
            </p>
          ) : thetaPreview?.success ? (
            <div className="space-y-1.5 font-mono text-xs text-gray-300">
              <p>
                Short expiry{' '}
                <span className="text-white">
                  {formatExpiryShort(
                    thetaPreview.short_expiry || thetaPreview.short_expiry_date,
                  )}
                </span>
                <span className="text-gray-500">
                  {' '}
                  (spot {Number(thetaPreview.spot).toFixed(0)})
                </span>
              </p>
              <p>
                Hedge call θ{' '}
                <span className="text-white">
                  {Number(
                    thetaPreview.hedge_call_theta ?? thetaPreview.hedge_total_theta,
                  ).toFixed(2)}
                </span>
                <span className="text-gray-500">
                  {' '}
                  × {Number(thetaPreview.theta_multiplier ?? thetaPreview.multiplier).toFixed(2)}
                </span>
              </p>
              <p>
                Required per call{' '}
                <span className="text-white">
                  {Number(thetaPreview.required_theta).toFixed(2)}
                </span>
              </p>
              <p>
                Would pick CALL{' '}
                <span className="text-emerald-300">
                  {Math.round(Number(thetaPreview.call?.strike))}
                </span>{' '}
                (theta {Number(thetaPreview.call?.theta).toFixed(2)},{' '}
                {formatMoney(thetaPreview.call?.premium)})
                {thetaPreview.call?.chain_limit ? (
                  <span className="text-amber-400"> [chain limit]</span>
                ) : null}
              </p>
              <p>
                {'              '}PUT{' '}
                <span className="text-emerald-300">
                  {Math.round(Number(thetaPreview.put?.strike))}
                </span>{' '}
                (premium-matched, {formatMoney(thetaPreview.put?.premium)}
                {thetaPreview.put?.theta != null
                  ? `, θ ${Number(thetaPreview.put.theta).toFixed(2)}`
                  : ''}
                )
              </p>
              <p>
                Coverage{' '}
                <span className="text-white">
                  {Number(thetaPreview.coverage).toFixed(1)}x
                </span>
              </p>
            </div>
          ) : (
            <p className="text-sm text-gray-500">Loading preview…</p>
          )}
        </div>
      </section>

      <section className="space-y-3 rounded-xl border border-emerald-700/40 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">TARGET</h2>
        <p className="text-xs text-gray-500">
          Basket profit target source. Theta-multiplier preview uses
          hypothetical hedge theta — no live hedge required to preview.
        </p>
        <div className="space-y-2">
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="radio"
              name="target_mode"
              checked={targetMode === 'payoff_pct'}
              onChange={() => setTargetMode('payoff_pct')}
              className="mt-1"
            />
            <span className="text-sm text-gray-300">
              Payoff % of max premium (current behaviour)
            </span>
          </label>
          <label
            className={`flex items-start gap-3 ${
              hedgeEnabled
                ? 'cursor-pointer'
                : 'cursor-not-allowed opacity-40'
            }`}
            title={
              hedgeEnabled
                ? undefined
                : 'Enable Hedge Mode first — theta-multiplier target needs hedge mode'
            }
          >
            <input
              type="radio"
              name="target_mode"
              checked={targetMode === 'theta_multiplier'}
              disabled={!hedgeEnabled}
              onChange={() => setTargetMode('theta_multiplier')}
              className="mt-1 disabled:cursor-not-allowed"
            />
            <span className="text-sm text-gray-300">
              Theta multiplier target
            </span>
          </label>
        </div>
        <label
          className={`block text-sm text-gray-300 ${
            hedgeEnabled && targetMode === 'theta_multiplier' ? '' : 'opacity-40'
          }`}
          title={
            !hedgeEnabled
              ? 'Enable Hedge Mode first — target theta % needs hedge mode'
              : undefined
          }
        >
          Target theta %
          <input
            type="number"
            min={10}
            max={1000}
            step={1}
            value={targetThetaPct}
            disabled={!hedgeEnabled || targetMode !== 'theta_multiplier'}
            onChange={(e) => setTargetThetaPct(e.target.value)}
            className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
          />
          <span className="mt-1 block text-xs text-gray-500">
            Exit when basket P&amp;L reaches this % of daily hedge theta income.
          </span>
        </label>

        <div className="rounded-lg border border-gray-700/80 bg-gray-900/50 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-wide text-emerald-400/90">
              Live preview
            </p>
            {previewLoading ? (
              <span className="text-[10px] text-gray-500">Refreshing…</span>
            ) : null}
          </div>
          {targetPreview?.unavailable || targetPreview?.success === false ? (
            <p className="text-sm text-amber-400">
              {targetPreview?.message || 'unavailable - chain fetch failed'}
            </p>
          ) : targetPreview?.success ? (
            <div className="space-y-1.5 font-mono text-xs text-gray-300">
              <p className="text-gray-500">
                Short {formatExpiryShort(targetPreview.short_expiry)} · CALL{' '}
                {Math.round(Number(targetPreview.call_strike))} @{' '}
                {formatMoney(targetPreview.call_premium)} · PUT{' '}
                {Math.round(Number(targetPreview.put_strike))} @{' '}
                {formatMoney(targetPreview.put_premium)}
              </p>
              <p className="text-sm text-white">
                = {formatMoney(targetPreview.target_usd)} ={' '}
                {Number(targetPreview.pct_of_max).toFixed(0)}% of max profit
                <span className="text-gray-500">
                  {' '}
                  ({formatMoney(targetPreview.max_profit_usd)})
                </span>
              </p>
              <p
                className={
                  (targetPreview.reachability || targetPreview.band) === 'reachable'
                    ? 'text-emerald-400'
                    : (targetPreview.reachability || targetPreview.band) === 'tight'
                      ? 'text-amber-400'
                      : 'text-rose-400'
                }
              >
                [
                {(targetPreview.reachability || targetPreview.band) === 'reachable'
                  ? 'ok'
                  : '!'}
                ] {targetPreview.band_label || targetPreview.reachability}
              </p>
            </div>
          ) : (
            <p className="text-sm text-gray-500">Loading preview…</p>
          )}
        </div>
      </section>

      <section className="space-y-3 rounded-xl border border-emerald-700/40 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">COOLDOWN</h2>
        <label className="block text-sm text-gray-300">
          Cooldown after loss (minutes)
          <input
            type="number"
            min={0}
            max={1440}
            step={1}
            value={cooldownAfterLossMinutes}
            onChange={(e) => setCooldownAfterLossMinutes(e.target.value)}
            className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
          />
          <span className="mt-1 block text-xs text-gray-500">
            After a basket stop-loss, wait this long before auto re-entry.
            0 = no extra cooldown. Independent of the hedge — the hedge stays
            open during cooldown.
          </span>
        </label>
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
