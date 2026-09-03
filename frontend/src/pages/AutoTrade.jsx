import { useCallback, useEffect, useMemo, useState } from 'react'
import AdjustmentSlabs from '../components/AdjustmentSlabs'
import InfoTooltip from '../components/InfoTooltip'
import {
  AutoTradeStickyHeader,
  AutoTradeStickyNav,
  FieldLabel,
  SectionCard,
  SectionDivider,
} from '../components/AutoTradeUi'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import LoadingSpinner from '../components/ui/LoadingSpinner'
import Toast from '../components/ui/Toast'
import { useWebSocket } from '../hooks/useWebSocket'
import {
  disableAutoTrade,
  enableAutoTrade,
  getActiveHedge,
  getActiveTrades,
  getAutoTradeSettings,
  getAutoTradeStatus,
  getExpiries,
  getHedgePreview,
  getTargetPreview,
  getThetaPreview,
  getWingPreview,
  saveAutoTradeSettings,
} from '../services/api'
import { formatNextEntryWait } from '../utils/nextEntryLabel'

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
  setters.setBasketQtyMode(data.basket_qty_mode || 'fixed')
  setters.setBasketQtyPctOfHedge(String(data.basket_qty_pct_of_hedge ?? 20))
  setters.setHedgeQtyLots(
    data.hedge_qty_lots == null ? '' : String(data.hedge_qty_lots),
  )
  setters.setBasketQtyDynamic(!!data.basket_qty_dynamic)
  setters.setUseDynamicQtyOnAdj(!!data.use_dynamic_qty_on_adjustment)
  setters.setBasketDecayExitEnabled(!!data.basket_decay_exit_enabled)
  setters.setBasketDecayExitPct(String(data.basket_decay_exit_pct ?? 50))
  setters.setBasketDecayExitMode(
    data.basket_decay_exit_mode === 'combined' ? 'combined' : 'both_legs',
  )
  setters.setBasketQtyThetaMult(String(data.basket_qty_theta_mult ?? 2.0))
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
  setters.setStranglePremiumMode(data.strangle_premium_mode || 'fixed')
  setters.setStranglePremiumPct(
    String(data.strangle_premium_pct_of_hedge ?? 3),
  )
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
  setters.setHedgeExpiryMode(data.hedge_expiry_mode || 'month_1')
  setters.setHedgeExpiryDateOverride(data.hedge_expiry_date_override || '')
  setters.setHedgeExpiryNeedsRepick(Boolean(data.hedge_expiry_needs_repick))
  setters.setHedgeExpiryDte(
    data.hedge_expiry_dte == null ? '' : String(data.hedge_expiry_dte),
  )
  setters.setMinHedgeDte(String(data.min_hedge_dte ?? 15))
  setters.setMinHedgeDteEnabled(
    data.min_hedge_dte_enabled == null
      ? true
      : Boolean(data.min_hedge_dte_enabled),
  )
  setters.setHedgeRollDte(String(data.hedge_roll_dte ?? 10))
  setters.setHedgeRollEnabled(
    data.hedge_roll_enabled == null ? true : Boolean(data.hedge_roll_enabled),
  )
  setters.setHedgeRollHardDte(String(data.hedge_roll_hard_dte ?? 5))
  setters.setHedgeForceRollEnabled(
    data.hedge_force_roll_enabled == null
      ? true
      : Boolean(data.hedge_force_roll_enabled),
  )
  setters.setHedgeCloseAtExpiryEnabled(
    data.hedge_close_at_expiry_enabled == null
      ? true
      : Boolean(data.hedge_close_at_expiry_enabled),
  )
  setters.setBasketWingsEnabled(Boolean(data.basket_wings_enabled))
  setters.setWingStrikeMode(data.wing_strike_mode || 'points')
  setters.setWingPointsAway(String(data.wing_points_away ?? 2000))
  setters.setWingDeltaMin(String(data.wing_delta_min ?? 0.05))
  setters.setWingDeltaMax(String(data.wing_delta_max ?? 0.07))
  setters.setWingPctOfPremium(String(data.wing_pct_of_premium ?? 20))
  setters.setHedgeAutoReopenAfterRoll(
    data.hedge_auto_reopen_after_roll == null
      ? true
      : Boolean(data.hedge_auto_reopen_after_roll),
  )
  setters.setHedgeTargetUsd(
    data.hedge_target_usd == null ? '' : String(data.hedge_target_usd),
  )
  setters.setHedgeStoplossUsd(
    data.hedge_stoploss_usd == null ? '' : String(data.hedge_stoploss_usd),
  )
  setters.setHedgeFixedSlUsd(String(data.hedge_fixed_sl_usd ?? 2))
  setters.setHedgeSlFloorPct(String(data.hedge_sl_floor_pct ?? 25))
  setters.setHedgeTargetMultiple(String(data.hedge_target_multiple ?? 3))
  setters.setHedgeExpectedMonthlyPct(
    String(data.hedge_expected_monthly_pct ?? 30),
  )
  setters.setHedgeMinHoldDays(String(data.hedge_min_hold_days ?? 10))
  setters.setSpreadMode(
    String(data.spread_mode || 'MANUAL').toUpperCase() === 'AUTO'
      ? 'AUTO'
      : 'MANUAL',
  )
  setters.setBasketExitSpreadPct(String(data.basket_exit_spread_pct ?? 4))
  setters.setHedgeExitSpreadPct(String(data.hedge_exit_spread_pct ?? 4))
  setters.setSpreadCapPct(String(data.spread_cap_pct ?? 8))
  setters.setMarginBufferPct(String(data.margin_buffer_pct ?? 50))
  setters.setStrikeSelectionMode(data.strike_selection_mode || 'fixed_premium')
  setters.setThetaMultiplier(String(data.theta_multiplier ?? 3))
  setters.setEntryPremiumMatchTolerancePct(
    String(data.entry_premium_match_tolerance_pct ?? 25),
  )
  setters.setTargetMode(data.target_mode || 'payoff_pct')
  setters.setTargetThetaPct(String(data.target_theta_pct ?? 150))
  setters.setBasketTargetMode(
    String(data.basket_target_mode || 'THETA').toUpperCase() === 'PCT'
      ? 'PCT'
      : 'THETA',
  )
  setters.setBasketTargetMultiple(String(data.basket_target_multiple ?? 1.5))
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
  if (setters.setNextEntrySource) {
    setters.setNextEntrySource(data.next_entry_source || null)
  }
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
  const [legacyHedgeOpen, setLegacyHedgeOpen] = useState(false)
  const [toast, setToast] = useState(null)

  const [isEnabled, setIsEnabled] = useState(false)
  const [lastError, setLastError] = useState(null)
  const [lastTradeId, setLastTradeId] = useState(null)
  const [secondsUntilEntry, setSecondsUntilEntry] = useState(null)
  const [nextEntrySource, setNextEntrySource] = useState(null)
  const [activeTrade, setActiveTrade] = useState(null)

  const [underlying, setUnderlying] = useState('BTC')
  const [expiryDte, setExpiryDte] = useState(1)
  const [expiryOptions, setExpiryOptions] = useState([])
  const [expiryLoading, setExpiryLoading] = useState(false)
  const [expiryError, setExpiryError] = useState(null)
  const [selectedExpiryDate, setSelectedExpiryDate] = useState(null)
  const [expiriesReady, setExpiriesReady] = useState(false)
  const [quantity, setQuantity] = useState(1)
  const [basketQtyMode, setBasketQtyMode] = useState('fixed')
  const [basketQtyPctOfHedge, setBasketQtyPctOfHedge] = useState('20')
  const [hedgeQtyLots, setHedgeQtyLots] = useState('')
  const [basketQtyDynamic, setBasketQtyDynamic] = useState(false)
  const [useDynamicQtyOnAdj, setUseDynamicQtyOnAdj] = useState(false)
  const [basketDecayExitEnabled, setBasketDecayExitEnabled] = useState(false)
  const [basketDecayExitPct, setBasketDecayExitPct] = useState('50')
  const [basketDecayExitMode, setBasketDecayExitMode] = useState('both_legs')
  const [basketQtyThetaMult, setBasketQtyThetaMult] = useState('2.0')
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
  const [stranglePremiumMode, setStranglePremiumMode] = useState('fixed')
  const [stranglePremiumPct, setStranglePremiumPct] = useState('3')
  const [adjLowPremiumExitEnabled, setAdjLowPremiumExitEnabled] =
    useState(false)
  const [adjLowPremiumMinUsd, setAdjLowPremiumMinUsd] = useState(150)
  const [conversionModeEnabled, setConversionModeEnabled] = useState(true)
  const [maxAdjustmentsPerBasket, setMaxAdjustmentsPerBasket] = useState('')
  const [premiumCoverLossEnabled, setPremiumCoverLossEnabled] = useState(false)
  const [combinedTriggerMode, setCombinedTriggerMode] = useState(false)
  const [hedgeEnabled, setHedgeEnabled] = useState(false)
  const [hedgeExpiryMode, setHedgeExpiryMode] = useState('month_1')
  const [hedgeExpiryDateOverride, setHedgeExpiryDateOverride] = useState('')
  const [hedgeExpiryNeedsRepick, setHedgeExpiryNeedsRepick] = useState(false)
  const [hedgeExpiryDte, setHedgeExpiryDte] = useState('')
  const [minHedgeDte, setMinHedgeDte] = useState('15')
  const [minHedgeDteEnabled, setMinHedgeDteEnabled] = useState(true)
  const [hedgeRollDte, setHedgeRollDte] = useState('10')
  const [hedgeRollEnabled, setHedgeRollEnabled] = useState(true)
  const [hedgeRollHardDte, setHedgeRollHardDte] = useState('5')
  const [hedgeForceRollEnabled, setHedgeForceRollEnabled] = useState(true)
  const [hedgeCloseAtExpiryEnabled, setHedgeCloseAtExpiryEnabled] =
    useState(true)
  const [basketWingsEnabled, setBasketWingsEnabled] = useState(false)
  const [wingStrikeMode, setWingStrikeMode] = useState('points')
  const [wingPointsAway, setWingPointsAway] = useState('2000')
  const [wingDeltaMin, setWingDeltaMin] = useState('0.05')
  const [wingDeltaMax, setWingDeltaMax] = useState('0.07')
  const [wingPctOfPremium, setWingPctOfPremium] = useState('20')
  const [wingPreview, setWingPreview] = useState(null)
  const [hedgeAutoReopenAfterRoll, setHedgeAutoReopenAfterRoll] = useState(true)
  const [hedgeTargetUsd, setHedgeTargetUsd] = useState('')
  const [hedgeStoplossUsd, setHedgeStoplossUsd] = useState('')
  const [hedgeFixedSlUsd, setHedgeFixedSlUsd] = useState('2')
  const [hedgeSlFloorPct, setHedgeSlFloorPct] = useState('25')
  const [hedgeTargetMultiple, setHedgeTargetMultiple] = useState('3')
  const [hedgeExpectedMonthlyPct, setHedgeExpectedMonthlyPct] = useState('30')
  const [hedgeMinHoldDays, setHedgeMinHoldDays] = useState('10')
  const [spreadMode, setSpreadMode] = useState('MANUAL')
  const [basketExitSpreadPct, setBasketExitSpreadPct] = useState('4')
  const [hedgeExitSpreadPct, setHedgeExitSpreadPct] = useState('4')
  const [spreadCapPct, setSpreadCapPct] = useState('8')
  const [marginBufferPct, setMarginBufferPct] = useState('50')
  const [strikeSelectionMode, setStrikeSelectionMode] =
    useState('fixed_premium')
  const [thetaMultiplier, setThetaMultiplier] = useState('3')
  const [entryPremiumMatchTolerancePct, setEntryPremiumMatchTolerancePct] =
    useState('25')
  const [targetMode, setTargetMode] = useState('payoff_pct')
  const [targetThetaPct, setTargetThetaPct] = useState('150')
  const [basketTargetMode, setBasketTargetMode] = useState('THETA')
  const [basketTargetMultiple, setBasketTargetMultiple] = useState('1.5')
  const [cooldownAfterLossMinutes, setCooldownAfterLossMinutes] =
    useState('120')
  const [orderMarginPerLot, setOrderMarginPerLot] = useState(null)
  const [hedgePreview, setHedgePreview] = useState(null)
  const [activeHedgePanel, setActiveHedgePanel] = useState(null)
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
      setBasketQtyMode,
      setBasketQtyPctOfHedge,
      setHedgeQtyLots,
      setBasketQtyDynamic,
      setUseDynamicQtyOnAdj,
      setBasketDecayExitEnabled,
      setBasketDecayExitPct,
      setBasketDecayExitMode,
      setBasketQtyThetaMult,
      setReEntryDelay,
      setEntrySettlingSeconds,
      setAdjustmentSettlingSeconds,
      setTpPct,
      setSlPct,
      setUniversalSlPct,
      setSlippagePct,
      setTradeType,
      setTargetPremium,
      setStranglePremiumMode,
      setStranglePremiumPct,
      setAdjLowPremiumExitEnabled,
      setAdjLowPremiumMinUsd,
      setConversionModeEnabled,
      setMaxAdjustmentsPerBasket,
      setPremiumCoverLossEnabled,
      setCombinedTriggerMode,
      setHedgeEnabled,
      setHedgeExpiryMode,
      setHedgeExpiryDateOverride,
      setHedgeExpiryNeedsRepick,
      setHedgeExpiryDte,
      setMinHedgeDte,
      setMinHedgeDteEnabled,
      setHedgeRollDte,
      setHedgeRollEnabled,
      setHedgeRollHardDte,
      setHedgeForceRollEnabled,
      setHedgeCloseAtExpiryEnabled,
      setBasketWingsEnabled,
      setWingStrikeMode,
      setWingPointsAway,
      setWingDeltaMin,
      setWingDeltaMax,
      setWingPctOfPremium,
      setHedgeAutoReopenAfterRoll,
      setHedgeTargetUsd,
      setHedgeStoplossUsd,
      setHedgeFixedSlUsd,
      setHedgeSlFloorPct,
      setHedgeTargetMultiple,
      setHedgeExpectedMonthlyPct,
      setHedgeMinHoldDays,
      setSpreadMode,
      setBasketExitSpreadPct,
      setHedgeExitSpreadPct,
      setSpreadCapPct,
      setMarginBufferPct,
      setStrikeSelectionMode,
      setThetaMultiplier,
      setEntryPremiumMatchTolerancePct,
      setTargetMode,
      setTargetThetaPct,
      setBasketTargetMode,
      setBasketTargetMultiple,
      setCooldownAfterLossMinutes,
      setOrderMarginPerLot,
      setIsEnabled,
      setLastError,
      setLastTradeId,
      setSecondsUntilEntry,
      setNextEntrySource,
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
      setNextEntrySource(status?.next_entry_source || null)

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
        const data = await getExpiries(u, { limit: 60 })
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
      if (lastMessage.next_entry_source != null) {
        setNextEntrySource(lastMessage.next_entry_source || null)
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
      strangle_premium_mode:
        tradeType === 'strangle' ? stranglePremiumMode || 'fixed' : 'fixed',
      strangle_premium_pct_of_hedge: Math.min(
        100,
        Math.max(0.01, Number(stranglePremiumPct) || 3),
      ),
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
      hedge_expiry_mode: hedgeExpiryMode || 'month_1',
      // Display-only resolved date — backend re-resolves the label on save
      hedge_expiry_date_override:
        expiryOptions.find((o) => o.key === hedgeExpiryMode)?.date ||
        hedgeExpiryDateOverride ||
        null,
      hedge_expiry_dte: null,
      min_hedge_dte: Math.min(60, Math.max(0, Number(minHedgeDte) || 15)),
      min_hedge_dte_enabled: Boolean(minHedgeDteEnabled),
      hedge_roll_dte: Math.min(60, Math.max(1, Number(hedgeRollDte) || 10)),
      hedge_roll_enabled: Boolean(hedgeRollEnabled),
      hedge_roll_hard_dte: Math.min(
        60,
        Math.max(1, Number(hedgeRollHardDte) || 5),
      ),
      hedge_force_roll_enabled: Boolean(hedgeForceRollEnabled),
      hedge_close_at_expiry_enabled: Boolean(hedgeCloseAtExpiryEnabled),
      basket_wings_enabled: Boolean(basketWingsEnabled),
      wing_strike_mode: wingStrikeMode || 'points',
      wing_points_away: Math.max(1, Number(wingPointsAway) || 2000),
      wing_delta_min: Math.min(
        0.99,
        Math.max(0.001, Number(wingDeltaMin) || 0.05),
      ),
      wing_delta_max: Math.min(
        0.99,
        Math.max(0.001, Number(wingDeltaMax) || 0.07),
      ),
      wing_pct_of_premium: Math.min(
        99.99,
        Math.max(0.01, Number(wingPctOfPremium) || 20),
      ),
      hedge_auto_reopen_after_roll: Boolean(hedgeAutoReopenAfterRoll),
      hedge_target_usd:
        hedgeTargetUsd === '' || hedgeTargetUsd == null
          ? null
          : Number(hedgeTargetUsd),
      hedge_stoploss_usd:
        hedgeStoplossUsd === '' || hedgeStoplossUsd == null
          ? null
          : Number(hedgeStoplossUsd),
      hedge_fixed_sl_usd: Math.min(
        1000,
        Math.max(0.1, Number(hedgeFixedSlUsd) || 2),
      ),
      hedge_sl_floor_pct: Number(hedgeSlFloorPct),
      hedge_target_multiple: Number(hedgeTargetMultiple),
      hedge_expected_monthly_pct: Math.min(
        200,
        Math.max(1, Number(hedgeExpectedMonthlyPct) || 30),
      ),
      hedge_min_hold_days: Math.min(
        60,
        Math.max(0, Number(hedgeMinHoldDays) || 10),
      ),
      spread_mode: spreadMode === 'MANUAL' ? 'MANUAL' : 'AUTO',
      basket_exit_spread_pct: Number(basketExitSpreadPct),
      hedge_exit_spread_pct: Number(hedgeExitSpreadPct),
      spread_cap_pct: Number(spreadCapPct),
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
      entry_premium_match_tolerance_pct: Math.min(
        100,
        Math.max(5, Number(entryPremiumMatchTolerancePct) || 25),
      ),
      target_mode: hedgeEnabled
        ? targetMode || 'payoff_pct'
        : 'payoff_pct',
      target_theta_pct: Math.min(
        1000,
        Math.max(10, Number(targetThetaPct) || 150),
      ),
      basket_target_mode: basketTargetMode === 'PCT' ? 'PCT' : 'THETA',
      basket_target_multiple: Math.min(
        10,
        Math.max(0.1, Number(basketTargetMultiple) || 1.5),
      ),
      cooldown_after_loss_minutes: Math.min(
        1440,
        Math.max(0, Number(cooldownAfterLossMinutes) || 0),
      ),
      basket_qty_mode:
        hedgeEnabled && basketQtyMode === 'pct_of_hedge'
          ? 'pct_of_hedge'
          : 'fixed',
      basket_qty_pct_of_hedge: Math.min(
        100,
        Math.max(1, Number(basketQtyPctOfHedge) || 20),
      ),
      hedge_qty_lots:
        hedgeEnabled && basketQtyMode === 'pct_of_hedge'
          ? Math.max(1, Number(hedgeQtyLots) || 1)
          : null,
      basket_qty_dynamic: pctOfHedgeSizingActive ? basketQtyDynamic : false,
      use_dynamic_qty_on_adjustment:
        pctOfHedgeSizingActive && basketQtyDynamic
          ? useDynamicQtyOnAdj
          : false,
      basket_qty_theta_mult: Math.min(
        10,
        Math.max(0.1, Number(basketQtyThetaMult) || 2.0),
      ),
      basket_decay_exit_enabled: Boolean(basketDecayExitEnabled),
      basket_decay_exit_pct: Math.min(
        99,
        Math.max(1, Number(basketDecayExitPct) || 50),
      ),
      basket_decay_exit_mode:
        basketDecayExitMode === 'combined' ? 'combined' : 'both_legs',
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

  const hedgeSlFloorPctError = useMemo(() => {
    const n = Number(hedgeSlFloorPct)
    if (hedgeSlFloorPct === '' || Number.isNaN(n)) {
      return 'Enter a number between 0 and 100'
    }
    if (n < 0 || n > 100) {
      return 'Must be between 0 and 100'
    }
    return null
  }, [hedgeSlFloorPct])

  const hedgeTargetMultipleError = useMemo(() => {
    const n = Number(hedgeTargetMultiple)
    if (hedgeTargetMultiple === '' || Number.isNaN(n)) {
      return 'Enter a number between 0.5 and 20'
    }
    if (n < 0.5 || n > 20) {
      return 'Must be between 0.5 and 20'
    }
    return null
  }, [hedgeTargetMultiple])

  const hedgeExpectedMonthlyPctError = useMemo(() => {
    const n = Number(hedgeExpectedMonthlyPct)
    if (hedgeExpectedMonthlyPct === '' || Number.isNaN(n)) {
      return 'Enter a number between 1 and 200'
    }
    if (n < 1 || n > 200) {
      return 'Must be between 1 and 200'
    }
    return null
  }, [hedgeExpectedMonthlyPct])

  const hedgeMinHoldDaysError = useMemo(() => {
    const n = Number(hedgeMinHoldDays)
    if (hedgeMinHoldDays === '' || Number.isNaN(n)) {
      return 'Enter a number between 0 and 60'
    }
    if (n < 0 || n > 60) {
      return 'Must be between 0 and 60'
    }
    return null
  }, [hedgeMinHoldDays])

  const minHedgeDteError = useMemo(() => {
    if (!minHedgeDteEnabled) return null
    const n = Number(minHedgeDte)
    if (minHedgeDte === '' || Number.isNaN(n)) {
      return 'Must be between 0 and 60'
    }
    if (n < 0 || n > 60) return 'Must be between 0 and 60'
    return null
  }, [minHedgeDte, minHedgeDteEnabled])

  const hedgeRollDteError = useMemo(() => {
    if (!hedgeRollEnabled) return null
    const n = Number(hedgeRollDte)
    if (hedgeRollDte === '' || Number.isNaN(n)) {
      return 'Must be between 1 and 60'
    }
    if (n < 1 || n > 60) return 'Must be between 1 and 60'
    return null
  }, [hedgeRollDte, hedgeRollEnabled])

  const hedgeRollHardDteError = useMemo(() => {
    if (!hedgeForceRollEnabled) return null
    const n = Number(hedgeRollHardDte)
    if (hedgeRollHardDte === '' || Number.isNaN(n)) {
      return 'Must be between 1 and 60'
    }
    if (n < 1 || n > 60) return 'Must be between 1 and 60'
    return null
  }, [hedgeRollHardDte, hedgeForceRollEnabled])

  const hedgeDteOrderingError = useMemo(() => {
    if (
      !minHedgeDteEnabled ||
      !hedgeRollEnabled ||
      !hedgeForceRollEnabled
    ) {
      return null
    }
    if (minHedgeDteError || hedgeRollDteError || hedgeRollHardDteError) {
      return null
    }
    const minN = Number(minHedgeDte)
    const rollN = Number(hedgeRollDte)
    const hardN = Number(hedgeRollHardDte)
    if (!(hardN < rollN && rollN < minN)) {
      return (
        'Require Force roll < Roll < Minimum hedge DTE. ' +
        'Roll DTE must be below Minimum hedge DTE, otherwise a newly opened ' +
        'hedge would immediately start rolling.'
      )
    }
    return null
  }, [
    minHedgeDte,
    hedgeRollDte,
    hedgeRollHardDte,
    minHedgeDteEnabled,
    hedgeRollEnabled,
    hedgeForceRollEnabled,
    minHedgeDteError,
    hedgeRollDteError,
    hedgeRollHardDteError,
  ])

  const wingDeltaOrderingError = useMemo(() => {
    if (!basketWingsEnabled || wingStrikeMode !== 'delta') return null
    const lo = Number(wingDeltaMin)
    const hi = Number(wingDeltaMax)
    if (Number.isNaN(lo) || Number.isNaN(hi)) return null
    if (hi < lo) {
      return 'Wing delta max must be ≥ min'
    }
    return null
  }, [basketWingsEnabled, wingStrikeMode, wingDeltaMin, wingDeltaMax])

  const allHedgeDteGuardsOff = useMemo(
    () =>
      !minHedgeDteEnabled &&
      !hedgeRollEnabled &&
      !hedgeForceRollEnabled,
    [minHedgeDteEnabled, hedgeRollEnabled, hedgeForceRollEnabled],
  )

  const basketExitSpreadPctError = useMemo(() => {
    const n = Number(basketExitSpreadPct)
    if (basketExitSpreadPct === '' || Number.isNaN(n)) {
      return 'Must be between 0 and 20'
    }
    if (n < 0 || n > 20) return 'Must be between 0 and 20'
    return null
  }, [basketExitSpreadPct])

  const hedgeExitSpreadPctError = useMemo(() => {
    const n = Number(hedgeExitSpreadPct)
    if (hedgeExitSpreadPct === '' || Number.isNaN(n)) {
      return 'Must be between 0 and 20'
    }
    if (n < 0 || n > 20) return 'Must be between 0 and 20'
    return null
  }, [hedgeExitSpreadPct])

  const spreadCapPctError = useMemo(() => {
    const n = Number(spreadCapPct)
    if (spreadCapPct === '' || Number.isNaN(n)) {
      return 'Must be between 0 and 20'
    }
    if (n < 0 || n > 20) return 'Must be between 0 and 20'
    return null
  }, [spreadCapPct])

  const spreadSettingsError =
    spreadCapPctError || basketExitSpreadPctError || hedgeExitSpreadPctError

  const basketQtyThetaMultError = useMemo(() => {
    const n = Number(basketQtyThetaMult)
    if (basketQtyThetaMult === '' || Number.isNaN(n)) {
      return 'Must be between 0.1 and 10'
    }
    if (n < 0.1 || n > 10) {
      return 'Must be between 0.1 and 10'
    }
    return null
  }, [basketQtyThetaMult])

  const pctOfHedgeSizingActive =
    hedgeEnabled && basketQtyMode === 'pct_of_hedge'

  const basketSizingChoice =
    basketQtyMode !== 'pct_of_hedge'
      ? 'fixed'
      : basketQtyDynamic
        ? 'dynamic_pct'
        : 'manual_pct'

  const setBasketSizingChoice = (choice) => {
    if (choice === 'fixed') {
      setBasketQtyMode('fixed')
      setBasketQtyDynamic(false)
    } else if (choice === 'manual_pct') {
      setBasketQtyMode('pct_of_hedge')
      setBasketQtyDynamic(false)
    } else {
      setBasketQtyMode('pct_of_hedge')
      setBasketQtyDynamic(true)
    }
  }

  const dynamicBasketSizingActive =
    pctOfHedgeSizingActive && basketQtyDynamic

  const basketSizingPreview = useMemo(() => {
    if (basketSizingChoice !== 'manual_pct' || !hedgeEnabled) return null
    const hedgeLots = Math.max(1, Number(hedgeQtyLots) || 1)
    const pct = Math.min(100, Math.max(1, Number(basketQtyPctOfHedge) || 20))
    const basketLots = Math.ceil((hedgeLots * pct) / 100)
    return { hedgeLots, pct, basketLots }
  }, [basketSizingChoice, hedgeEnabled, hedgeQtyLots, basketQtyPctOfHedge])

  const hedgeMarksForPreview = useMemo(() => {
    if (
      activeHedgePanel?.call_mark_price > 0 &&
      activeHedgePanel?.put_mark_price > 0
    ) {
      return {
        call: Number(activeHedgePanel.call_mark_price),
        put: Number(activeHedgePanel.put_mark_price),
      }
    }
    if (
      hedgePreview?.call_mark_price > 0 &&
      hedgePreview?.put_mark_price > 0
    ) {
      return {
        call: Number(hedgePreview.call_mark_price),
        put: Number(hedgePreview.put_mark_price),
      }
    }
    return null
  }, [activeHedgePanel, hedgePreview])

  const stranglePremiumPreview = useMemo(() => {
    if (stranglePremiumMode !== 'pct_of_hedge' || !hedgeMarksForPreview) {
      return null
    }
    const pct = Math.min(100, Math.max(0.01, Number(stranglePremiumPct) || 3))
    const avg =
      (hedgeMarksForPreview.call + hedgeMarksForPreview.put) / 2
    return Math.ceil(avg * (pct / 100))
  }, [stranglePremiumMode, stranglePremiumPct, hedgeMarksForPreview])

  const stranglePremiumHighPctWarning =
    stranglePremiumMode === 'pct_of_hedge' &&
    Number(stranglePremiumPct) > 10 &&
    stranglePremiumPreview != null

  const buildPreviewParams = useCallback(() => {
    const dte = Math.max(0, Number(expiryDte) || 1)
    const effectiveBasketQtyMode =
      hedgeEnabled && basketQtyMode === 'pct_of_hedge' ? 'pct_of_hedge' : 'fixed'
    const params = {
      underlying,
      quantity: Math.max(1, Number(quantity) || 1),
      basket_qty_mode: effectiveBasketQtyMode,
      basket_qty_pct_of_hedge: Math.min(
        100,
        Math.max(1, Number(basketQtyPctOfHedge) || 20),
      ),
      hedge_qty_lots:
        effectiveBasketQtyMode === 'pct_of_hedge'
          ? Math.max(1, Number(hedgeQtyLots) || 1)
          : null,
      basket_qty_dynamic:
        effectiveBasketQtyMode === 'pct_of_hedge' ? basketQtyDynamic : false,
      basket_qty_theta_mult: Math.min(
        10,
        Math.max(0.1, Number(basketQtyThetaMult) || 2.0),
      ),
      hedge_expiry_mode: hedgeExpiryMode || 'month_1',
      margin_buffer_pct: Math.min(200, Math.max(0, Number(marginBufferPct) || 50)),
      theta_multiplier: Math.min(20, Math.max(0.01, Number(thetaMultiplier) || 3)),
      target_theta_pct: Math.min(1000, Math.max(10, Number(targetThetaPct) || 150)),
      expiry_dte: dte,
      trade_type: tradeType || 'straddle',
      target_premium_per_side:
        tradeType === 'strangle' ? Number(targetPremium) || 150 : 150,
      wing_strike_mode: wingStrikeMode || 'points',
      wing_points_away: Math.max(1, Number(wingPointsAway) || 2000),
      wing_delta_min: Math.min(
        0.99,
        Math.max(0.001, Number(wingDeltaMin) || 0.05),
      ),
      wing_delta_max: Math.min(
        0.99,
        Math.max(0.001, Number(wingDeltaMax) || 0.07),
      ),
      wing_pct_of_premium: Math.min(
        99.99,
        Math.max(0.01, Number(wingPctOfPremium) || 20),
      ),
      strike_selection_mode: hedgeEnabled
        ? strikeSelectionMode || 'fixed_premium'
        : 'fixed_premium',
    }
    // Match auto-trade save rules: daily 0/1/2 DTE never send a calendar override
    if (dte > 2 && selectedExpiryDate) {
      params.expiry_date_override = selectedExpiryDate
    }
    return params
  }, [
    underlying,
    quantity,
    hedgeEnabled,
    basketQtyMode,
    basketQtyPctOfHedge,
    hedgeQtyLots,
    basketQtyDynamic,
    basketQtyThetaMult,
    hedgeExpiryMode,
    marginBufferPct,
    thetaMultiplier,
    targetThetaPct,
    expiryDte,
    selectedExpiryDate,
    tradeType,
    targetPremium,
    wingStrikeMode,
    wingPointsAway,
    wingDeltaMin,
    wingDeltaMax,
    wingPctOfPremium,
    strikeSelectionMode,
  ])

  const refreshPreviews = useCallback(async () => {
    setPreviewLoading(true)
    try {
      const params = buildPreviewParams()
      const [hp, tp, tgp, wp, activeHedgeRes] = await Promise.all([
        getHedgePreview(params),
        getThetaPreview(params),
        getTargetPreview(params),
        basketWingsEnabled
          ? getWingPreview(params)
          : Promise.resolve(null),
        hedgeEnabled
          ? getActiveHedge().catch(() => ({ hedge: null }))
          : Promise.resolve({ hedge: null }),
      ])
      // Never keep stale success data — replace with unavailable on failure
      setHedgePreview(hp?.unavailable || hp?.success === false ? { ...PREVIEW_FAIL, ...hp } : hp)
      setThetaPreview(tp?.unavailable || tp?.success === false ? { ...PREVIEW_FAIL, ...tp } : tp)
      setTargetPreview(
        tgp?.unavailable || tgp?.success === false ? { ...PREVIEW_FAIL, ...tgp } : tgp,
      )
      if (basketWingsEnabled) {
        setWingPreview(
          wp?.unavailable || wp?.success === false
            ? { ...PREVIEW_FAIL, ...wp }
            : wp,
        )
      } else {
        setWingPreview(null)
      }
      setActiveHedgePanel(activeHedgeRes?.hedge ?? null)
      if (hp?.order_margin_per_lot != null && !hp?.unavailable) {
        setOrderMarginPerLot(Number(hp.order_margin_per_lot))
      } else {
        setOrderMarginPerLot(null)
      }
    } catch {
      setHedgePreview(PREVIEW_FAIL)
      setThetaPreview(PREVIEW_FAIL)
      setTargetPreview(PREVIEW_FAIL)
      setWingPreview(basketWingsEnabled ? PREVIEW_FAIL : null)
      setOrderMarginPerLot(null)
    } finally {
      setPreviewLoading(false)
    }
  }, [buildPreviewParams, hedgeEnabled, basketWingsEnabled])

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
    if (
      hedgeSlFloorPctError ||
      hedgeTargetMultipleError ||
      hedgeExpectedMonthlyPctError ||
      hedgeMinHoldDaysError ||
      minHedgeDteError ||
      hedgeRollDteError ||
      hedgeRollHardDteError ||
      hedgeDteOrderingError ||
      wingDeltaOrderingError ||
      spreadSettingsError ||
      basketQtyThetaMultError
    ) {
      setToast({
        type: 'error',
        message:
          hedgeSlFloorPctError ||
          hedgeTargetMultipleError ||
          hedgeExpectedMonthlyPctError ||
          hedgeMinHoldDaysError ||
          minHedgeDteError ||
          hedgeRollDteError ||
          hedgeRollHardDteError ||
          hedgeDteOrderingError ||
          wingDeltaOrderingError ||
          spreadSettingsError ||
          basketQtyThetaMultError ||
          'Fix validation errors before saving',
      })
      return
    }
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
    if (
      hedgeSlFloorPctError ||
      hedgeTargetMultipleError ||
      hedgeExpectedMonthlyPctError ||
      hedgeMinHoldDaysError ||
      minHedgeDteError ||
      hedgeRollDteError ||
      hedgeRollHardDteError ||
      hedgeDteOrderingError ||
      wingDeltaOrderingError ||
      spreadSettingsError ||
      basketQtyThetaMultError
    ) {
      setToast({
        type: 'error',
        message:
          hedgeSlFloorPctError ||
          hedgeTargetMultipleError ||
          hedgeExpectedMonthlyPctError ||
          hedgeMinHoldDaysError ||
          minHedgeDteError ||
          hedgeRollDteError ||
          hedgeRollHardDteError ||
          hedgeDteOrderingError ||
          wingDeltaOrderingError ||
          spreadSettingsError ||
          basketQtyThetaMultError ||
          'Fix validation errors before enabling',
      })
      return
    }
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
      ? stranglePremiumMode === 'pct_of_hedge' && stranglePremiumPreview != null
        ? `STRANGLE ≈$${stranglePremiumPreview}/side (${stranglePremiumPct}% hedge)`
        : `STRANGLE $${Number(targetPremium) || 150}/side`
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
          ? ` (${formatNextEntryWait(secondsUntilEntry, nextEntrySource || 'retry')})`
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
        text: `🔄 Auto Trade ON · ${tradeTypeLabel} · ${formatNextEntryWait(secondsUntilEntry, nextEntrySource)}`,
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
    nextEntrySource,
    tradeTypeLabel,
  ])

  const headerStatusText = useMemo(() => {
    if (!isEnabled) return 'Disabled'
    if (activeTrade && tradeLabel != null) {
      return `Active — ${tradeTypeLabel} — trade #${tradeLabel}`
    }
    if (lastError) return `Error — ${lastError}`
    if (secondsUntilEntry != null && secondsUntilEntry > 0) {
      return `${tradeTypeLabel} — ${formatNextEntryWait(secondsUntilEntry, nextEntrySource)}`
    }
    return `${tradeTypeLabel} — ready`
  }, [
    isEnabled,
    activeTrade,
    tradeLabel,
    lastError,
    secondsUntilEntry,
    nextEntrySource,
    tradeTypeLabel,
  ])

  if (loading) {
    return (
      <main className="mx-auto flex max-w-7xl items-center justify-center px-4 py-20">
        <LoadingSpinner />
      </main>
    )
  }

  return (
    <main className="mx-auto max-w-7xl bg-gray-900">
      <AutoTradeStickyHeader
        isEnabled={isEnabled}
        statusText={headerStatusText}
        saving={saving}
        toggling={toggling}
        onSave={handleSave}
        onEnable={handleEnable}
        onDisable={() => setDisableOpen(true)}
      />
      <AutoTradeStickyNav />

      {isEnabled && statusView.text !== '⚪ Disabled' ? (
        <div
          className={`mx-4 mt-3 rounded-lg border px-4 py-2 text-sm sm:mx-6 ${statusView.bg} ${statusView.color}`}
        >
          {statusView.text}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-6 p-4 sm:p-6 xl:grid-cols-2">
        {/* LEFT COLUMN */}
        <div className="space-y-6">
          {/* A — Trade Setup */}
          <SectionCard id="trade-setup" icon="📊" title="Trade Setup">
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <FieldLabel tooltip="Which crypto asset's options to trade">
                  Underlying
                </FieldLabel>
                <div className="mt-2 flex flex-wrap gap-2">
                  {UNDERLYINGS.map((u) => (
                    <button
                      key={u}
                      type="button"
                      onClick={() => setUnderlying(u)}
                      className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                        underlying === u
                          ? 'bg-blue-500 text-white'
                          : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                      }`}
                    >
                      {u}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <FieldLabel tooltip="Straddle = ATM Call + premium-matched Put at same/nearby strike. Strangle = OTM Call + OTM Put, strikes chosen by premium target">
                  Trade Type
                </FieldLabel>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => setTradeType('straddle')}
                    className={`rounded-lg border p-2 text-left text-xs transition-all ${
                      tradeType === 'straddle'
                        ? 'border-blue-500 bg-blue-500/10 text-blue-400'
                        : 'border-gray-700 bg-gray-700/50 text-gray-400 hover:border-gray-600'
                    }`}
                  >
                    <div className="font-medium">Short Straddle</div>
                  </button>
                  <button
                    type="button"
                    onClick={() => setTradeType('strangle')}
                    className={`rounded-lg border p-2 text-left text-xs transition-all ${
                      tradeType === 'strangle'
                        ? 'border-purple-500 bg-purple-500/10 text-purple-400'
                        : 'border-gray-700 bg-gray-700/50 text-gray-400 hover:border-gray-600'
                    }`}
                  >
                    <div className="font-medium">Short Strangle</div>
                  </button>
                </div>
              </div>
            </div>

            <div>
              <FieldLabel tooltip="Option expiry date for each new short basket">
                Expiry
              </FieldLabel>
              <div className="mt-2 flex max-w-full items-center gap-2">
                <select
                  value={selectedExpiryDate || ''}
                  onChange={(e) => setSelectedExpiryDate(e.target.value || null)}
                  disabled={expiryLoading}
                  className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
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

            <label
              className={`block ${
                pctOfHedgeSizingActive ? 'opacity-40' : ''
              }`}
            >
              <FieldLabel tooltip="Lots per basket. Not used in % of hedge mode — basket qty is derived from hedge">
                Quantity
              </FieldLabel>
              <input
                type="number"
                min={1}
                step={1}
                value={quantity}
                onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
                className="mt-2 w-full max-w-xs rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
              />
              {pctOfHedgeSizingActive && (
                <p className="mt-1 text-xs text-gray-500">
                  Not used in % of hedge mode.
                </p>
              )}
            </label>

            {tradeType === 'strangle' && (
              <div className="rounded-lg border border-purple-500/30 bg-gray-700/40 p-4 space-y-3">
                <FieldLabel>Target Premium per Side</FieldLabel>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => setStranglePremiumMode('fixed')}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                      stranglePremiumMode === 'fixed'
                        ? 'bg-purple-600 text-white'
                        : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                    }`}
                  >
                    Fixed $
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      hedgeEnabled && setStranglePremiumMode('pct_of_hedge')
                    }
                    disabled={!hedgeEnabled}
                    className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                      stranglePremiumMode === 'pct_of_hedge'
                        ? 'bg-purple-600 text-white'
                        : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
                    } disabled:cursor-not-allowed disabled:opacity-40`}
                  >
                    % of hedge
                  </button>
                </div>
                {!hedgeEnabled && stranglePremiumMode === 'pct_of_hedge' && (
                  <p className="text-xs text-amber-400/90">
                    Requires Hedge Mode — falls back to fixed $ on entry.
                  </p>
                )}
                {stranglePremiumMode === 'fixed' ? (
                  <>
                    <input
                      type="number"
                      value={targetPremium}
                      onChange={(e) =>
                        setTargetPremium(parseFloat(e.target.value) || 0)
                      }
                      className="w-full max-w-xs rounded-md bg-gray-700 px-3 py-2 text-white"
                      placeholder="e.g. 150"
                      min={1}
                      max={10000}
                    />
                    <p className="text-xs text-gray-500">
                      Bot finds OTM Call & Put where premium ≈ ${targetPremium}
                    </p>
                  </>
                ) : (
                  <>
                    <label className="block text-sm text-gray-300">
                      % of live hedge premium (mark, per side)
                      <input
                        type="number"
                        min={0.01}
                        max={100}
                        step={0.1}
                        value={stranglePremiumPct}
                        onChange={(e) =>
                          setStranglePremiumPct(
                            String(
                              Math.min(
                                100,
                                Math.max(0.01, Number(e.target.value) || 3),
                              ),
                            ),
                          )
                        }
                        className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                      />
                    </label>
                    {stranglePremiumPreview != null ? (
                      <p className="text-xs text-gray-400">
                        ≈ ${stranglePremiumPreview} per side
                        {hedgeMarksForPreview && (
                          <>
                            {' '}
                            (call ${Math.round(hedgeMarksForPreview.call)} /
                            put ${Math.round(hedgeMarksForPreview.put)} marks)
                          </>
                        )}
                      </p>
                    ) : (
                      <p className="text-xs text-gray-500">
                        Live hedge marks unavailable — preview will appear when
                        hedge data loads.
                      </p>
                    )}
                    {stranglePremiumHighPctWarning && (
                      <p className="text-xs text-amber-400">
                        ⚠ At current hedge premium this is ~$
                        {stranglePremiumPreview} per side vs $
                        {Number(targetPremium) || 150} fixed today
                      </p>
                    )}
                  </>
                )}
              </div>
            )}
          </SectionCard>

          {/* B — Basket Sizing */}
          <SectionCard id="basket-sizing" icon="📐" title="Basket Sizing">
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setBasketSizingChoice('fixed')}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                basketSizingChoice === 'fixed'
                  ? 'bg-blue-500 text-white'
                  : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              }`}
            >
              Fixed quantity
            </button>
            <button
              type="button"
              onClick={() =>
                hedgeEnabled && setBasketSizingChoice('manual_pct')
              }
              disabled={!hedgeEnabled}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                basketSizingChoice === 'manual_pct'
                  ? 'bg-blue-500 text-white'
                  : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              } disabled:cursor-not-allowed disabled:opacity-40`}
            >
              % of hedge — manual
            </button>
            <button
              type="button"
              onClick={() =>
                hedgeEnabled && setBasketSizingChoice('dynamic_pct')
              }
              disabled={!hedgeEnabled}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                basketSizingChoice === 'dynamic_pct'
                  ? 'bg-blue-500 text-white'
                  : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              } disabled:cursor-not-allowed disabled:opacity-40`}
            >
              % of hedge — dynamic (theta)
            </button>
          </div>
          {!hedgeEnabled && (
            <p className="text-xs text-amber-400/90">
              Requires Hedge Mode. With hedge off, sizing falls back to Fixed
              quantity.
            </p>
          )}
          {pctOfHedgeSizingActive && (
            <div className="space-y-3 border-t border-gray-700/60 pt-3">
              <label className="block text-sm text-gray-300">
                Hedge quantity (lots)
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={hedgeQtyLots}
                  onChange={(e) =>
                    setHedgeQtyLots(
                      e.target.value === ''
                        ? ''
                        : String(Math.max(1, Number(e.target.value) || 1)),
                    )
                  }
                  className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              {basketSizingChoice === 'manual_pct' && (
                <>
                  <label className="block text-sm text-gray-300">
                    Short basket % of hedge
                    <input
                      type="number"
                      min={1}
                      max={100}
                      step={1}
                      value={basketQtyPctOfHedge}
                      onChange={(e) =>
                        setBasketQtyPctOfHedge(
                          String(
                            Math.min(
                              100,
                              Math.max(1, Number(e.target.value) || 1),
                            ),
                          ),
                        )
                      }
                      className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                    />
                  </label>
                  {basketSizingPreview && (
                    <p
                      className={`text-xs ${
                        basketSizingPreview.basketLots === 0
                          ? 'text-red-400'
                          : 'text-gray-400'
                      }`}
                    >
                      {basketSizingPreview.basketLots === 0 ? (
                        <>
                          Short basket would be 0 lots — entry will be skipped.
                        </>
                      ) : (
                        <>
                          {basketSizingPreview.hedgeLots} lots ×{' '}
                          {basketSizingPreview.pct}% →{' '}
                          {basketSizingPreview.basketLots} lot
                          {Number(basketSizingPreview.basketLots) === 1
                            ? ''
                            : 's'}{' '}
                          (round up)
                        </>
                      )}
                    </p>
                  )}
                </>
              )}
              {basketSizingChoice === 'dynamic_pct' && (
                <div className="space-y-2 rounded-lg border border-gray-700/60 bg-gray-900/40 p-3">
                  <p className="text-sm text-gray-300">
                    Dynamic % (theta-based)
                    <span className="mt-0.5 block text-xs text-gray-500">
                      Formula: (hedge_call_theta × mult × 100) /
                      call_ask_at_entry
                    </span>
                  </p>
                  <label className="block text-sm text-gray-300">
                    Multiplier
                    <input
                      type="number"
                      min={0.1}
                      max={10}
                      step={0.1}
                      value={basketQtyThetaMult}
                      onChange={(e) => setBasketQtyThetaMult(e.target.value)}
                      className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                    />
                    {basketQtyThetaMultError && (
                      <p className="mt-1 text-xs text-red-400">
                        {basketQtyThetaMultError}
                      </p>
                    )}
                  </label>
                  <div className="mt-2">
                    <label className="flex cursor-pointer items-center gap-2">
                      <input
                        type="checkbox"
                        checked={useDynamicQtyOnAdj}
                        onChange={(e) =>
                          setUseDynamicQtyOnAdj(e.target.checked)
                        }
                      />
                      <span className="text-sm text-white">
                        Re-calculate qty at each adjustment
                      </span>
                      <InfoTooltip text="At adjustment, bot recalculates basket qty using live hedge theta and new strike ask price. Untested leg qty increases to match. Hard cap: max 50% of hedge qty (e.g. max 2 for 5-lot hedge). Entry target ($) stays unchanged — higher qty helps reach it faster." />
                    </label>
                    <p className="ml-6 mt-1 text-xs text-gray-400">
                      Uses dynamic % formula at adjustment time. Max qty: 50%
                      of hedge qty.
                    </p>
                  </div>
                  <p className="text-xs text-gray-400">
                    Dynamic: (hedge_theta ×{' '}
                    {Number(basketQtyThetaMult) || 2.0} × 100) / call_ask →
                    computed at entry
                  </p>
                </div>
              )}
            </div>
          )}
          </SectionCard>

          {/* C — Adjustment Trigger */}
          <SectionCard id="adjustment-trigger" icon="⚡" title="Adjustment Trigger">
            {slabsInitial && (
              <AdjustmentSlabs
                key={slabsKey}
                onChange={onSlabsChange}
                defaultMode={slabsInitial.mode || 'slab'}
                initialValues={slabsInitial}
              />
            )}
            <label className="flex cursor-pointer items-start gap-3">
              <input
                type="checkbox"
                checked={combinedTriggerMode}
                onChange={async (e) => {
                  const on = e.target.checked
                  setCombinedTriggerMode(on)
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
                className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-500"
              />
              <span className="text-sm text-gray-300">
                <FieldLabel
                  as="span"
                  tooltip="Instead of per-leg trigger, fire when CALL+PUT combined premium rises by trigger %. Adjusts the leg with higher % increase"
                >
                  Combined Premium Trigger
                </FieldLabel>{' '}
                <span className="text-gray-500">
                  ({combinedTriggerMode ? 'ON' : 'OFF'})
                </span>
              </span>
            </label>
          </SectionCard>

          {/* D — Adjustment Behaviour */}
          <SectionCard id="adjustment-behaviour" icon="🔄" title="Adjustment Behaviour">
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={adjLowPremiumExitEnabled}
                  onChange={(e) => setAdjLowPremiumExitEnabled(e.target.checked)}
                  className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-500"
                />
                <span className="text-sm text-gray-300">
                  Adjustment Exit on Low Premium
                </span>
              </label>
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={premiumCoverLossEnabled}
                  onChange={(e) => setPremiumCoverLossEnabled(e.target.checked)}
                  className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-500"
                />
                <span className="text-sm text-gray-300">
                  Premium Cover Loss ({premiumCoverLossEnabled ? 'ON' : 'OFF'})
                </span>
              </label>
            </div>
            {adjLowPremiumExitEnabled && (
              <div className="rounded-lg border border-amber-500/30 bg-gray-700/40 p-4">
                <FieldLabel>Minimum replacement premium ($)</FieldLabel>
                <input
                  type="number"
                  min={10}
                  max={500}
                  step={10}
                  value={adjLowPremiumMinUsd}
                  onChange={(e) =>
                    setAdjLowPremiumMinUsd(Number(e.target.value) || 150)
                  }
                  className="mt-2 w-full max-w-xs rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </div>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={conversionModeEnabled}
                  onChange={(e) => {
                    const on = e.target.checked
                    setConversionModeEnabled(on)
                    if (on) setMaxAdjustmentsPerBasket('')
                  }}
                  className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-700 text-blue-500"
                />
                <span className="text-sm text-gray-300">
                  Conversion Mode ({conversionModeEnabled ? 'ON' : 'OFF'})
                </span>
              </label>
              {!conversionModeEnabled ? (
                <label className="block text-sm text-gray-300">
                  <FieldLabel>Max Adjustments</FieldLabel>
                  <select
                    value={
                      maxAdjustmentsPerBasket === ''
                        ? 'unlimited'
                        : maxAdjustmentsPerBasket
                    }
                    onChange={(e) => {
                      const v = e.target.value
                      setMaxAdjustmentsPerBasket(v === 'unlimited' ? '' : v)
                    }}
                    className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                  >
                    <option value="unlimited">Unlimited</option>
                    <option value="1">1</option>
                    <option value="2">2</option>
                    <option value="3">3</option>
                  </select>
                </label>
              ) : (
                <div />
              )}
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-sm text-gray-300">
                <FieldLabel>Entry settling (sec)</FieldLabel>
                <input
                  type="number"
                  min={0}
                  max={300}
                  step={1}
                  value={entrySettlingSeconds}
                  onChange={(e) => setEntrySettlingSeconds(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
              <label className="block text-sm text-gray-300">
                <FieldLabel>Adjustment settling (sec)</FieldLabel>
                <input
                  type="number"
                  min={0}
                  max={300}
                  step={1}
                  value={adjustmentSettlingSeconds}
                  onChange={(e) => setAdjustmentSettlingSeconds(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
            </div>
            <label className="block text-sm text-gray-300">
              <FieldLabel>Re-entry delay (min)</FieldLabel>
              <div className="mt-2 flex max-w-xs items-center gap-2">
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={reEntryDelay}
                  onChange={(e) =>
                    setReEntryDelay(Math.max(0, Number(e.target.value) || 0))
                  }
                  className="w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
                <span className="shrink-0 text-gray-400">minutes</span>
              </div>
            </label>
          </SectionCard>
        </div>

        <div className="space-y-6">
          <SectionCard id="risk-target" icon="🎯" title="Risk & Target">
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="text-sm text-gray-300">
                <FieldLabel>Profit Target % of max premium</FieldLabel>
                <input
                  type="number"
                  min={1}
                  max={500}
                  step={1}
                  value={tpPct}
                  onChange={(e) => setTpPct(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
              <label className="text-sm text-gray-300">
                <FieldLabel>Stop Loss % of max premium</FieldLabel>
                <input
                  type="number"
                  min={1}
                  max={1000}
                  step={1}
                  value={slPct}
                  onChange={(e) => setSlPct(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
              <label className="text-sm text-gray-300">
                <FieldLabel>Delta SL (%)</FieldLabel>
                <input
                  type="number"
                  min={100}
                  max={1000}
                  step={1}
                  value={universalSlPct}
                  onChange={(e) => setUniversalSlPct(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
              <label className="text-sm text-gray-300">
                <FieldLabel>Slippage Est (%)</FieldLabel>
                <input
                  type="number"
                  min={0}
                  max={10}
                  step={0.1}
                  value={slippagePct}
                  onChange={(e) => setSlippagePct(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
            </div>
            <SectionDivider>Premium Decay Exit</SectionDivider>
            <div className="space-y-3">
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={basketDecayExitEnabled}
                  onChange={(e) => setBasketDecayExitEnabled(e.target.checked)}
                  className="mt-1"
                />
                <span className="text-sm text-gray-300">
                  Exit basket on premium decay
                </span>
              </label>
              {basketDecayExitEnabled && (
                <>
                  <label className="block text-sm text-gray-300">
                    <FieldLabel tooltip="Books the theta already collected instead of holding the gamma-heavy tail. Basis is each leg's entry price, or its blended price after an adjustment top-up.">
                      Exit when premium falls to (% of entry)
                    </FieldLabel>
                    <input
                      type="number"
                      min={1}
                      max={99}
                      step={1}
                      value={basketDecayExitPct}
                      onChange={(e) => setBasketDecayExitPct(e.target.value)}
                      className="mt-2 w-full max-w-xs rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                    />
                    <p className="mt-1 text-xs text-gray-500">
                      Remaining premium % — e.g. 50 means exit when premium is
                      at or below 50% of entry.
                    </p>
                  </label>
                  <div className="space-y-2">
                    <label className="flex cursor-pointer items-start gap-3">
                      <input
                        type="radio"
                        name="basket_decay_exit_mode"
                        checked={basketDecayExitMode === 'both_legs'}
                        onChange={() => setBasketDecayExitMode('both_legs')}
                        className="mt-1"
                      />
                      <span className="text-sm text-gray-300">Both legs</span>
                    </label>
                    <label className="flex cursor-pointer items-start gap-3">
                      <input
                        type="radio"
                        name="basket_decay_exit_mode"
                        checked={basketDecayExitMode === 'combined'}
                        onChange={() => setBasketDecayExitMode('combined')}
                        className="mt-1"
                      />
                      <span className="text-sm text-gray-300">
                        Combined premium
                      </span>
                    </label>
                  </div>
                </>
              )}
            </div>
            <SectionDivider>Basket Profit Target Mode</SectionDivider>
            <div className="space-y-2">
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="radio"
                  name="basket_target_mode"
                  checked={basketTargetMode === 'THETA'}
                  onChange={() => setBasketTargetMode('THETA')}
                  className="mt-1"
                />
                <span className="text-sm text-gray-300">
                  Theta — multiple of hedge daily theta
                </span>
              </label>
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="radio"
                  name="basket_target_mode"
                  checked={basketTargetMode === 'PCT'}
                  onChange={() => setBasketTargetMode('PCT')}
                  className="mt-1"
                />
                <span className="text-sm text-gray-300">
                  Percent — % of basket credit (legacy)
                </span>
              </label>
            </div>
            {basketTargetMode === 'THETA' ? (
              <label className="block text-sm text-gray-300">
                Basket target multiple
                <input
                  type="number"
                  min={0.1}
                  max={10}
                  step={0.1}
                  value={basketTargetMultiple}
                  onChange={(e) => setBasketTargetMultiple(e.target.value)}
                  className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
                />
              </label>
            ) : null}
            <SectionDivider>Target Calculation</SectionDivider>
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
                  Payoff % of max premium
                </span>
              </label>
              <label
                className={`flex items-start gap-3 ${
                  hedgeEnabled ? 'cursor-pointer' : 'cursor-not-allowed opacity-40'
                }`}
              >
                <input
                  type="radio"
                  name="target_mode"
                  checked={targetMode === 'theta_multiplier'}
                  disabled={!hedgeEnabled}
                  onChange={() => setTargetMode('theta_multiplier')}
                  className="mt-1 disabled:cursor-not-allowed"
                />
                <span className="text-sm text-gray-300">Theta multiplier target</span>
              </label>
            </div>
            <label
              className={`block text-sm text-gray-300 ${
                hedgeEnabled && targetMode === 'theta_multiplier'
                  ? ''
                  : 'opacity-40'
              }`}
            >
              <FieldLabel>Target theta %</FieldLabel>
              <input
                type="number"
                min={10}
                max={1000}
                step={1}
                value={targetThetaPct}
                disabled={!hedgeEnabled || targetMode !== 'theta_multiplier'}
                onChange={(e) => setTargetThetaPct(e.target.value)}
                className="mt-2 w-full max-w-xs rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white disabled:cursor-not-allowed"
              />
            </label>
            <div className="rounded-lg border border-green-700/50 bg-gray-900/50 p-3">
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
                  </p>
                </div>
              ) : (
                <p className="text-sm text-gray-500">Loading preview…</p>
              )}
            </div>
          </SectionCard>

          <SectionCard
            id="basket-wings"
            icon="🛡"
            title="BASKET WINGS (tail protection)"
          >
            <label className="flex cursor-pointer items-start gap-3">
              <input
                type="checkbox"
                checked={basketWingsEnabled}
                onChange={(e) => setBasketWingsEnabled(e.target.checked)}
                className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500"
              />
              <span className="text-sm text-gray-300">
                Enable wings
                <span className="mt-1 block text-xs text-gray-500">
                  Adds a long far-OTM call + put to every basket. Turns the
                  short strangle into a defined-risk condor so a gap or spike
                  cannot run unbounded.
                </span>
              </span>
            </label>

            <div
              className={`mt-4 space-y-4 ${
                basketWingsEnabled ? '' : 'pointer-events-none opacity-40'
              }`}
            >
              <fieldset className="space-y-3">
                <legend className="text-sm text-gray-300">
                  Strike selection rule
                </legend>
                <label className="flex items-center gap-2 text-sm text-gray-300">
                  <input
                    type="radio"
                    name="wingStrikeMode"
                    checked={wingStrikeMode === 'points'}
                    onChange={() => setWingStrikeMode('points')}
                    disabled={!basketWingsEnabled}
                    className="h-4 w-4 border-gray-600 bg-gray-900 text-emerald-500"
                  />
                  Points away
                  <input
                    type="number"
                    min={1}
                    step={100}
                    value={wingPointsAway}
                    onChange={(e) => setWingPointsAway(e.target.value)}
                    disabled={
                      !basketWingsEnabled || wingStrikeMode !== 'points'
                    }
                    className="ml-2 w-28 rounded-md border border-gray-600 bg-gray-900 px-2 py-1 text-white disabled:opacity-40"
                  />
                  <span className="text-xs text-gray-500">
                    points further OTM
                  </span>
                </label>
                <label className="flex flex-wrap items-center gap-2 text-sm text-gray-300">
                  <input
                    type="radio"
                    name="wingStrikeMode"
                    checked={wingStrikeMode === 'delta'}
                    onChange={() => setWingStrikeMode('delta')}
                    disabled={!basketWingsEnabled}
                    className="h-4 w-4 border-gray-600 bg-gray-900 text-emerald-500"
                  />
                  Delta band
                  <input
                    type="number"
                    min={0.001}
                    max={0.99}
                    step={0.01}
                    value={wingDeltaMin}
                    onChange={(e) => setWingDeltaMin(e.target.value)}
                    disabled={
                      !basketWingsEnabled || wingStrikeMode !== 'delta'
                    }
                    className="ml-2 w-20 rounded-md border border-gray-600 bg-gray-900 px-2 py-1 text-white disabled:opacity-40"
                  />
                  <span className="text-xs text-gray-500">to</span>
                  <input
                    type="number"
                    min={0.001}
                    max={0.99}
                    step={0.01}
                    value={wingDeltaMax}
                    onChange={(e) => setWingDeltaMax(e.target.value)}
                    disabled={
                      !basketWingsEnabled || wingStrikeMode !== 'delta'
                    }
                    className="w-20 rounded-md border border-gray-600 bg-gray-900 px-2 py-1 text-white disabled:opacity-40"
                  />
                </label>
                {wingDeltaOrderingError ? (
                  <span className="block text-xs text-red-400">
                    {wingDeltaOrderingError}
                  </span>
                ) : null}
                <label className="flex flex-wrap items-center gap-2 text-sm text-gray-300">
                  <input
                    type="radio"
                    name="wingStrikeMode"
                    checked={wingStrikeMode === 'pct_of_premium'}
                    onChange={() => setWingStrikeMode('pct_of_premium')}
                    disabled={!basketWingsEnabled}
                    className="h-4 w-4 border-gray-600 bg-gray-900 text-emerald-500"
                  />
                  % of short premium
                  <input
                    type="number"
                    min={0.01}
                    max={99.99}
                    step={1}
                    value={wingPctOfPremium}
                    onChange={(e) => setWingPctOfPremium(e.target.value)}
                    disabled={
                      !basketWingsEnabled ||
                      wingStrikeMode !== 'pct_of_premium'
                    }
                    className="ml-2 w-20 rounded-md border border-gray-600 bg-gray-900 px-2 py-1 text-white disabled:opacity-40"
                  />
                  <span className="text-xs text-gray-500">
                    % → short $200 → wing ≈ $
                    {((200 * (Number(wingPctOfPremium) || 20)) / 100).toFixed(
                      0,
                    )}
                  </span>
                </label>
              </fieldset>

              <div className="rounded-md border border-gray-700/80 bg-gray-950/50 px-3 py-3 font-mono text-xs text-gray-300">
                <p className="mb-2 text-[11px] uppercase tracking-wide text-gray-500">
                  Live wing preview
                </p>
                {!basketWingsEnabled ? (
                  <p className="text-gray-500">Enable wings to preview.</p>
                ) : wingPreview?.unavailable ||
                  wingPreview?.success === false ? (
                  <p className="text-amber-400">
                    {wingPreview?.message ||
                      'unavailable - chain fetch failed'}
                  </p>
                ) : wingPreview?.success ? (
                  <div className="space-y-1.5">
                    <p>
                      Short C {Math.round(Number(wingPreview.short_call?.strike))}{' '}
                      @ {formatMoney(wingPreview.short_call?.premium)}
                      {'  →  '}
                      Wing C{' '}
                      {wingPreview.wing_call
                        ? `${Math.round(Number(wingPreview.wing_call.strike))} @ ${formatMoney(wingPreview.wing_call.premium)} (+${Math.round(Number(wingPreview.call_gap_points) || 0)} pts)`
                        : '— none'}
                    </p>
                    <p>
                      Short P {Math.round(Number(wingPreview.short_put?.strike))}{' '}
                      @ {formatMoney(wingPreview.short_put?.premium)}
                      {'  →  '}
                      Wing P{' '}
                      {wingPreview.wing_put
                        ? `${Math.round(Number(wingPreview.wing_put.strike))} @ ${formatMoney(wingPreview.wing_put.premium)} (−${Math.round(Number(wingPreview.put_gap_points) || 0)} pts)`
                        : '— none'}
                    </p>
                    <div className="my-2 border-t border-gray-700" />
                    <p>
                      Net credit{' '}
                      {formatMoney(wingPreview.net_credit_usd_per_lot, 3)} / lot
                    </p>
                    <p>
                      Est. round-trip cost{' '}
                      {formatMoney(
                        wingPreview.est_round_trip_cost_usd_per_lot,
                        3,
                      )}{' '}
                      / lot
                    </p>
                    <p>
                      Net credit after cost{' '}
                      {formatMoney(
                        wingPreview.net_credit_after_cost_usd_per_lot,
                        3,
                      )}{' '}
                      / lot
                      {wingPreview.cost_consumed_pct != null
                        ? ` (${Number(wingPreview.cost_consumed_pct).toFixed(0)}% consumed)`
                        : ''}
                    </p>
                  </div>
                ) : (
                  <p className="text-gray-500">
                    {previewLoading ? 'Loading preview…' : 'Waiting for chain…'}
                  </p>
                )}
              </div>
            </div>
          </SectionCard>

          <SectionCard id="hedge-mode" icon="🛡️" title="Hedge Mode" accent>
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
          <label className="block text-sm text-gray-300 sm:col-span-2">
            Hedge expiry
            <div className="mt-1 flex flex-wrap items-center gap-3">
              <select
                value={
                  expiryOptions.some((o) => o.key === hedgeExpiryMode)
                    ? hedgeExpiryMode
                    : hedgeExpiryMode === 'date'
                      ? 'date'
                      : ''
                }
                onChange={(e) => {
                  const key = e.target.value
                  setHedgeExpiryMode(key)
                  setHedgeExpiryNeedsRepick(false)
                  const row = expiryOptions.find((o) => o.key === key)
                  setHedgeExpiryDateOverride(row?.date || '')
                }}
                disabled={!hedgeEnabled || expiryLoading}
                className="w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
              >
                {expiryLoading && (
                  <option value="">Loading expiries…</option>
                )}
                {!expiryLoading && expiryOptions.length === 0 && (
                  <option value="">No expiries available</option>
                )}
                {hedgeExpiryNeedsRepick || hedgeExpiryMode === 'date' ? (
                  <option value="date" disabled>
                    Re-pick required (fixed date is stale)
                  </option>
                ) : null}
                {expiryOptions.map((opt) => (
                  <option key={opt.key || opt.date} value={opt.key || opt.date}>
                    {opt.label}
                  </option>
                ))}
              </select>
              <span className="text-xs text-gray-400">
                → resolves to{' '}
                <span className="font-mono text-gray-200">
                  {expiryOptions.find((o) => o.key === hedgeExpiryMode)?.date ||
                    hedgePreview?.expiry_date ||
                    hedgeExpiryDateOverride ||
                    '--'}
                </span>
              </span>
            </div>
            <span className="mt-1 block text-xs text-gray-500">
              Same labelled list as Trade Structure. Stored as a relative key
              (e.g. month_2) so it never goes stale.
            </span>
            {hedgeExpiryNeedsRepick || hedgeExpiryMode === 'date' ? (
              <span className="mt-1 block text-xs text-amber-400">
                Your previous hedge expiry was a fixed calendar date. Choose a
                labelled option (Month / Week / DTE) before enabling hedge mode.
              </span>
            ) : null}
          </label>
          <label className="block text-sm text-gray-300">
            <span className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={minHedgeDteEnabled}
                onChange={(e) => setMinHedgeDteEnabled(e.target.checked)}
                disabled={!hedgeEnabled}
                className="h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500 disabled:cursor-not-allowed"
              />
              Minimum hedge DTE
            </span>
            <input
              type="number"
              min={0}
              max={60}
              step={1}
              value={minHedgeDte}
              onChange={(e) => setMinHedgeDte(e.target.value)}
              disabled={!hedgeEnabled || !minHedgeDteEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed disabled:opacity-50 ${
                minHedgeDteError || hedgeDteOrderingError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {minHedgeDteError ? (
              <span className="mt-1 block text-xs text-red-400">
                {minHedgeDteError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                If the selected expiry is closer than this, the next monthly is
                used instead. Disable to allow hedge and basket on the same
                expiry (0DTE / 2DTE).
              </span>
            )}
          </label>
          <label className="block text-sm text-gray-300">
            <span className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={hedgeRollEnabled}
                onChange={(e) => setHedgeRollEnabled(e.target.checked)}
                disabled={!hedgeEnabled}
                className="h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500 disabled:cursor-not-allowed"
              />
              Roll at DTE
            </span>
            <input
              type="number"
              min={1}
              max={60}
              step={1}
              value={hedgeRollDte}
              onChange={(e) => setHedgeRollDte(e.target.value)}
              disabled={!hedgeEnabled || !hedgeRollEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed disabled:opacity-50 ${
                hedgeRollDteError || hedgeDteOrderingError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {hedgeRollDteError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeRollDteError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Start the roll countdown when the hedge reaches this DTE. The
                hedge waits for the open basket to close, then closes.
              </span>
            )}
          </label>
          <label className="block text-sm text-gray-300">
            <span className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={hedgeForceRollEnabled}
                onChange={(e) => setHedgeForceRollEnabled(e.target.checked)}
                disabled={!hedgeEnabled}
                className="h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500 disabled:cursor-not-allowed"
              />
              Force roll at DTE
            </span>
            <input
              type="number"
              min={1}
              max={60}
              step={1}
              value={hedgeRollHardDte}
              onChange={(e) => setHedgeRollHardDte(e.target.value)}
              disabled={!hedgeEnabled || !hedgeForceRollEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed disabled:opacity-50 ${
                hedgeRollHardDteError || hedgeDteOrderingError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {hedgeRollHardDteError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeRollHardDteError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Hard deadline. At this DTE the hedge closes even if a basket is
                still open — the cascade closes the basket first.
              </span>
            )}
          </label>
          <label className="flex cursor-pointer items-start gap-3 sm:col-span-2">
            <input
              type="checkbox"
              checked={hedgeCloseAtExpiryEnabled}
              onChange={(e) =>
                setHedgeCloseAtExpiryEnabled(e.target.checked)
              }
              disabled={!hedgeEnabled}
              className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500 disabled:cursor-not-allowed"
            />
            <span className="text-sm text-gray-300">
              Close hedge before expiry (pre-expiry window)
              <span className="mt-1 block text-xs text-gray-500">
                Closes the hedge in the same 15-minute pre-expiry window as
                baskets — baskets first, then hedge. Prevents unsettled long
                options and missing exit P&amp;L.
              </span>
              {!hedgeCloseAtExpiryEnabled ? (
                <span className="mt-2 block text-xs text-red-400">
                  ⚠ Hedge will settle on the exchange without a recorded exit.
                  P&amp;L for this structure will be incomplete.
                </span>
              ) : null}
            </span>
          </label>
          <div className="sm:col-span-2 rounded-md border border-gray-700/80 bg-gray-950/40 px-3 py-2 text-xs text-gray-400">
            Opens at{' '}
            {minHedgeDteEnabled
              ? `>= ${Number(minHedgeDte) || '—'} DTE`
              : 'off'}{' '}
            &nbsp;→&nbsp; rolls at{' '}
            {hedgeRollEnabled
              ? `${Number(hedgeRollDte) || '—'} DTE`
              : 'off'}{' '}
            &nbsp;→&nbsp; forced at{' '}
            {hedgeForceRollEnabled
              ? `${Number(hedgeRollHardDte) || '—'} DTE`
              : 'off'}
            {hedgeDteOrderingError ? (
              <span className="mt-1 block text-red-400">
                {hedgeDteOrderingError}
              </span>
            ) : null}
            {allHedgeDteGuardsOff ? (
              <span className="mt-2 block text-amber-400">
                ⚠ Hedge can now share the basket&apos;s expiry. Net structure
                theta may turn negative — check theta preview before entry.
              </span>
            ) : null}
          </div>
          <label className="flex cursor-pointer items-start gap-3 sm:col-span-2">
            <input
              type="checkbox"
              checked={hedgeAutoReopenAfterRoll}
              onChange={(e) => setHedgeAutoReopenAfterRoll(e.target.checked)}
              disabled={!hedgeEnabled}
              className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-emerald-500 disabled:cursor-not-allowed"
            />
            <span className="text-sm text-gray-300">
              Auto-open next hedge after roll
              <span className="mt-1 block text-xs text-gray-500">
                When a hedge rolls at its DTE threshold, immediately open the
                next monthly. Stoploss and target closes always stay manual.
              </span>
            </span>
          </label>
          <SectionDivider>Stop Loss</SectionDivider>
          <label className="block text-sm text-gray-300">
            <input
              type="number"
              min={0.1}
              max={1000}
              step={0.1}
              value={hedgeFixedSlUsd}
              onChange={(e) => setHedgeFixedSlUsd(e.target.value)}
              disabled={!hedgeEnabled}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
            />
            <span className="mt-1 block text-xs text-gray-500">
              Structure stop budget starts here. Booked basket profit raises the
              budget; floor % keeps it from going negative after losses.
            </span>
          </label>
          <label className="block text-sm text-gray-300">
            Hedge SL Floor (%)
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              value={hedgeSlFloorPct}
              onChange={(e) => setHedgeSlFloorPct(e.target.value)}
              disabled={!hedgeEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed ${
                hedgeSlFloorPctError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {hedgeSlFloorPctError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeSlFloorPctError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Minimum hedge stoploss as % of the fixed SL. Prevents the SL
                budget going negative after basket losses.
              </span>
            )}
          </label>
          <SectionDivider>Target</SectionDivider>
          <label className="block text-sm text-gray-300">
            Expected monthly earnings (%)
            <input
              type="number"
              min={1}
              max={200}
              step={1}
              value={hedgeExpectedMonthlyPct}
              onChange={(e) => setHedgeExpectedMonthlyPct(e.target.value)}
              disabled={!hedgeEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed ${
                hedgeExpectedMonthlyPctError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {hedgeExpectedMonthlyPctError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeExpectedMonthlyPctError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Assumed monthly return on hedge entry cost. Target USD =
                multiple × entry_cost × this %.
              </span>
            )}
          </label>
          <label className="block text-sm text-gray-300">
            Hedge Target (x monthly)
            <input
              type="number"
              min={0.5}
              max={20}
              step={0.1}
              value={hedgeTargetMultiple}
              onChange={(e) => setHedgeTargetMultiple(e.target.value)}
              disabled={!hedgeEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed ${
                hedgeTargetMultipleError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {hedgeTargetMultipleError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeTargetMultipleError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Close the whole structure when structure P&amp;L reaches this
                multiple of expected monthly earnings. Keep this large — a small
                target forces frequent hedge rolls and spread eats the profit.
              </span>
            )}
          </label>
          <label className="block text-sm text-gray-300">
            Min hold days (target)
            <input
              type="number"
              min={0}
              max={60}
              step={1}
              value={hedgeMinHoldDays}
              onChange={(e) => setHedgeMinHoldDays(e.target.value)}
              disabled={!hedgeEnabled}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed ${
                hedgeMinHoldDaysError ? 'border-red-500' : 'border-gray-600'
              }`}
            />
            {hedgeMinHoldDaysError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeMinHoldDaysError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Do not book a target close until the hedge has been held this
                many days (amortises ~5% round-trip spread).
              </span>
            )}
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
          <button
            type="button"
            onClick={() => setLegacyHedgeOpen((o) => !o)}
            className="text-xs text-purple-400 hover:text-purple-300"
          >
            {legacyHedgeOpen ? 'Hide legacy fields ▲' : 'Show legacy fields ▼'}
          </button>
          {legacyHedgeOpen ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-sm text-gray-300">
                Hedge target ($) — legacy
                <input
                  type="number"
                  min={0.01}
                  step={1}
                  value={hedgeTargetUsd}
                  onChange={(e) => setHedgeTargetUsd(e.target.value)}
                  disabled={!hedgeEnabled}
                  placeholder="Optional"
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white disabled:cursor-not-allowed"
                />
              </label>
              <label className="block text-sm text-gray-300">
                Hedge stop loss ($) — legacy
                <input
                  type="number"
                  min={0.01}
                  step={1}
                  value={hedgeStoplossUsd}
                  onChange={(e) => setHedgeStoplossUsd(e.target.value)}
                  disabled={!hedgeEnabled}
                  placeholder="Optional"
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white disabled:cursor-not-allowed"
                />
              </label>
            </div>
          ) : null}
        </div>

        <div className="mt-2 rounded-lg border border-green-700/50 bg-gray-900/50 p-3">
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
          </SectionCard>

          <SectionCard id="strike-selection" icon="🎯" title="Strike Selection">
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
        <label
          className={`block text-sm text-gray-300 ${
            hedgeEnabled && strikeSelectionMode === 'theta_based'
              ? ''
              : 'opacity-40'
          }`}
        >
          Entry premium match tolerance (%)
          <input
            type="number"
            min={5}
            max={100}
            step={1}
            value={entryPremiumMatchTolerancePct}
            disabled={!hedgeEnabled || strikeSelectionMode !== 'theta_based'}
            onChange={(e) => setEntryPremiumMatchTolerancePct(e.target.value)}
            className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed"
          />
          <span className="mt-1 block text-xs text-gray-500">
            Warns in logs when the put premium diverges from the
            premium-selected call by more than this %. Entry still proceeds —
            default 25%.
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
                Required call premium{' '}
                <span className="text-white">
                  {Number(
                    thetaPreview.required_call_premium ??
                      thetaPreview.required_theta,
                  ).toFixed(2)}
                </span>
                {thetaPreview.premium_margin_pct != null ? (
                  <span className="text-gray-500">
                    {' '}
                    (margin {Number(thetaPreview.premium_margin_pct).toFixed(1)}
                    %)
                  </span>
                ) : null}
              </p>
              {thetaPreview.premium_fallback_used ? (
                <p className="rounded border border-rose-600/50 bg-rose-950/40 px-2 py-2 text-xs font-semibold text-amber-300">
                  PREMIUM TARGET UNREACHABLE - required{' '}
                  {Number(
                    thetaPreview.required_call_premium ??
                      thetaPreview.required_theta,
                  ).toFixed(2)}
                  , best available{' '}
                  {Number(
                    thetaPreview.selected_call_premium ??
                      thetaPreview.call?.premium ??
                      0,
                  ).toFixed(2)}
                  . Falling back to nearest OTM call.
                </p>
              ) : null}
              {thetaPreview.fallback_used ? (
                <p className="rounded border border-rose-600/50 bg-rose-950/40 px-2 py-2 text-xs font-semibold text-amber-300">
                  THETA TARGET UNREACHABLE - required{' '}
                  {Number(thetaPreview.required_theta).toFixed(2)}, chain max{' '}
                  {Number(thetaPreview.max_available_theta ?? 0).toFixed(2)}.
                  Max usable multiplier right now:{' '}
                  {Number(thetaPreview.max_usable_multiplier ?? 0).toFixed(2)}
                </p>
              ) : null}
              <p>
                Would pick CALL{' '}
                <span className="text-emerald-300">
                  {Math.round(Number(thetaPreview.call?.strike))}
                </span>{' '}
                (premium{' '}
                {formatMoney(
                  thetaPreview.selected_call_premium ??
                    thetaPreview.call?.premium,
                )}
                , θ {Number(thetaPreview.call?.theta).toFixed(2)})
                {thetaPreview.strikes_above_selected != null ? (
                  <span className="text-gray-500">
                    {' '}
                    · {Number(thetaPreview.strikes_above_selected)} strikes
                    above
                  </span>
                ) : null}
                {thetaPreview.premium_fallback_used ? (
                  <span className="text-rose-400"> [PREMIUM FALLBACK]</span>
                ) : thetaPreview.call?.chain_limit ? (
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
          </SectionCard>

          <SectionCard id="advanced" icon="⚙️" title="Advanced">
            <label className="block text-sm text-gray-300 sm:max-w-xs">
              <FieldLabel tooltip="After a basket stop-loss, wait this long before auto re-entry. 0 = no extra cooldown. Hedge stays open during cooldown">
                Cooldown after loss (min)
              </FieldLabel>
              <input
                type="number"
                min={0}
                max={1440}
                step={1}
                value={cooldownAfterLossMinutes}
                onChange={(e) => setCooldownAfterLossMinutes(e.target.value)}
                className="mt-2 w-full rounded-md border border-gray-600 bg-gray-700 px-3 py-2 text-white"
              />
            </label>

            <SectionDivider>Spread Estimation</SectionDivider>
            <fieldset className="space-y-2">
              <legend className="sr-only">Spread mode</legend>
          <label className="flex items-start gap-2 text-sm text-gray-200">
            <input
              type="radio"
              name="spreadMode"
              checked={spreadMode === 'MANUAL'}
              onChange={() => setSpreadMode('MANUAL')}
              className="mt-1 accent-amber-500"
            />
            <span>Manual</span>
          </label>
          <label className="flex items-start gap-2 text-sm text-gray-200">
            <input
              type="radio"
              name="spreadMode"
              checked={spreadMode === 'AUTO'}
              onChange={() => setSpreadMode('AUTO')}
              className="mt-1 accent-amber-500"
            />
            <span>
              Auto (from live order book)
              <span className="mt-1 block text-xs font-normal text-amber-400/90">
                Auto reads top-of-book only. On thin books the real fill is
                worse, so Auto tends to under-estimate cost. Manual is
                recommended.
              </span>
            </span>
          </label>
        </fieldset>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block text-sm text-gray-300">
            Basket exit spread %
            <input
              type="number"
              min={0}
              max={20}
              step={0.1}
              value={basketExitSpreadPct}
              onChange={(e) => setBasketExitSpreadPct(e.target.value)}
              disabled={spreadMode !== 'MANUAL'}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed disabled:opacity-50 ${
                spreadMode === 'MANUAL' && basketExitSpreadPctError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {spreadMode === 'MANUAL' && basketExitSpreadPctError ? (
              <span className="mt-1 block text-xs text-red-400">
                {basketExitSpreadPctError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Applied when Manual is selected (also used as AUTO fallback).
                Default 4%.
              </span>
            )}
          </label>
          <label className="block text-sm text-gray-300">
            Hedge exit spread %
            <input
              type="number"
              min={0}
              max={20}
              step={0.1}
              value={hedgeExitSpreadPct}
              onChange={(e) => setHedgeExitSpreadPct(e.target.value)}
              disabled={spreadMode !== 'MANUAL'}
              className={`mt-1 w-full rounded-md border bg-gray-900 px-3 py-2 text-white disabled:cursor-not-allowed disabled:opacity-50 ${
                spreadMode === 'MANUAL' && hedgeExitSpreadPctError
                  ? 'border-red-500'
                  : 'border-gray-600'
              }`}
            />
            {spreadMode === 'MANUAL' && hedgeExitSpreadPctError ? (
              <span className="mt-1 block text-xs text-red-400">
                {hedgeExitSpreadPctError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Applied when Manual is selected (also used as AUTO fallback).
                Default 4%.
              </span>
            )}
          </label>
          <label className="block text-sm text-gray-300 sm:col-span-2">
            Spread cap %
            <input
              type="number"
              min={0}
              max={20}
              step={0.1}
              value={spreadCapPct}
              onChange={(e) => setSpreadCapPct(e.target.value)}
              className={`mt-1 w-full max-w-xs rounded-md border bg-gray-900 px-3 py-2 text-white ${
                spreadCapPctError ? 'border-red-500' : 'border-gray-600'
              }`}
            />
            {spreadCapPctError ? (
              <span className="mt-1 block text-xs text-red-400">
                {spreadCapPctError}
              </span>
            ) : (
              <span className="mt-1 block text-xs text-gray-500">
                Safety ceiling. Applies in both modes - protects against a
                momentarily empty order book blowing up the estimate.
              </span>
            )}
          </label>
        </div>
          </SectionCard>
        </div>
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
