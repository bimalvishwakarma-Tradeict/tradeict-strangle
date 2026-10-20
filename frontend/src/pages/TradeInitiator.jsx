import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import AdjustmentSlabs from '../components/AdjustmentSlabs'
import LiveTradesPanel from '../components/LiveTradesPanel'
import OptionChain from '../components/OptionChain'
import PayoffGraph from '../components/PayoffGraph'
import Toast from '../components/ui/Toast'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import LoadingSpinner from '../components/ui/LoadingSpinner'
import { getAccountStatus, getExpiries, initiateTrade, registerExistingTrade } from '../services/api'
import {
  OPTIONS_CONTRACT_VALUE,
  toUsdPnl,
} from '../utils/contractValue'
import { longTrailSlLevel } from '../utils/longTrail'
import { isValidPct } from '../utils/pct'

const UNDERLYINGS = ['BTC', 'ETH', 'XAU']

const PLACE_STEPS = [
  'Placing Call order on Delta…',
  'Placing Put order on Delta…',
  'Registering with bot…',
]

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function fmtStrike(v) {
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function formatExpiryLabel(row) {
  if (!row) return ''
  try {
    const d = new Date(`${row.date}T00:00:00`)
    const pretty = d.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
    })
    return `${pretty} (${row.label})`
  } catch {
    return `${row.date} (${row.label})`
  }
}

export default function TradeInitiator() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const emergencyMode = searchParams.get('emergency') === '1'

  const [underlying, setUnderlying] = useState('BTC')
  const [expiries, setExpiries] = useState([])
  const [expiry, setExpiry] = useState('')
  const [expiryError, setExpiryError] = useState('')
  const [loadingExpiries, setLoadingExpiries] = useState(false)

  const [selectedCall, setSelectedCall] = useState(null)
  const [selectedPut, setSelectedPut] = useState(null)
  const [currentPrice, setCurrentPrice] = useState(0)

  const [quantity, setQuantity] = useState(1)
  const [tpPct, setTpPct] = useState('50')
  const [slPct, setSlPct] = useState('100')
  const [slippagePct, setSlippagePct] = useState('2')
  const [universalSlPct, setUniversalSlPct] = useState('200')
  const [slabs, setSlabs] = useState({
    mode: 'slab',
    flat_pct: 150,
    slab_24h: 200,
    slab_12h: 175,
    slab_6h: 150,
    slab_lt6h: 150,
    premium_slab_300: 150,
    premium_slab_200: 160,
    premium_slab_100: 180,
    premium_slab_lt100: 200,
  })
  const [isDemo, setIsDemo] = useState(false)
  const [positionSide, setPositionSide] = useState('short')
  const [longTradesEnabled, setLongTradesEnabled] = useState(false)
  const [adjustmentEnabled, setAdjustmentEnabled] = useState(true)
  const [targetUsd, setTargetUsd] = useState('150')
  const [slUsd, setSlUsd] = useState('100')
  const [trailStepUsd, setTrailStepUsd] = useState('')
  const [trailAmtUsd, setTrailAmtUsd] = useState('')
  const [confirmLongOpen, setConfirmLongOpen] = useState(false)

  // Emergency-only: manual fill prices
  const [callEntry, setCallEntry] = useState('')
  const [putEntry, setPutEntry] = useState('')

  const [placing, setPlacing] = useState(false)
  const [placeStep, setPlaceStep] = useState(0)
  const [toast, setToast] = useState(null)
  const [successResult, setSuccessResult] = useState(null)
  const [partialError, setPartialError] = useState(null)

  useEffect(() => {
    document.title = emergencyMode
      ? 'Delta Bot — Emergency Register'
      : 'Delta Bot — Place Strangle'
  }, [emergencyMode])

  useEffect(() => {
    let cancelled = false
    getAccountStatus()
      .then((st) => {
        if (!cancelled) {
          setLongTradesEnabled(Boolean(st?.long_trades_enabled))
        }
      })
      .catch(() => {
        if (!cancelled) setLongTradesEnabled(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (positionSide === 'long') {
      setAdjustmentEnabled(false)
    }
  }, [positionSide])

  useEffect(() => {
    let cancelled = false
    async function loadExpiries() {
      setLoadingExpiries(true)
      setExpiryError('')
      setSelectedCall(null)
      setSelectedPut(null)
      try {
        const rows = await getExpiries(underlying)
        if (cancelled) return
        setExpiries(rows || [])
        setExpiry(rows?.[0]?.date || '')
      } catch (err) {
        if (cancelled) return
        setExpiries([])
        setExpiry('')
        setExpiryError(err.message || 'Failed to load expiries')
      } finally {
        if (!cancelled) setLoadingExpiries(false)
      }
    }
    loadExpiries()
    return () => {
      cancelled = true
    }
  }, [underlying])

  const onChainMeta = useCallback(({ currentPrice: px }) => {
    setCurrentPrice(Number(px || 0))
  }, [])

  const onSlabsChange = useCallback((next) => {
    setSlabs(next)
  }, [])

  // Sync emergency entry fields from marks when strikes selected
  useEffect(() => {
    if (!emergencyMode) return
    if (selectedCall?.call_mark_price != null) {
      setCallEntry(String(selectedCall.call_mark_price))
    }
    if (selectedPut?.put_mark_price != null) {
      setPutEntry(String(selectedPut.put_mark_price))
    }
  }, [emergencyMode, selectedCall, selectedPut])

  const callPrem = emergencyMode
    ? Number(callEntry) || 0
    : Number(selectedCall?.call_mark_price || 0)
  const putPrem = emergencyMode
    ? Number(putEntry) || 0
    : Number(selectedPut?.put_mark_price || 0)
  const qty = Number(quantity) || 0
  // Mark premium points × lots (display only — not real USD)
  const totalPremiumPoints = (callPrem + putPrem) * qty
  // Real Delta USD: premium × lots × 0.001 — locked as Initial Max Profit
  const estMaxProfitUsd = toUsdPnl(callPrem + putPrem, qty)
  const tpPctNum = Number(tpPct)
  const slPctNum = Number(slPct)
  const slippagePctNum = Number(slippagePct)
  const universalSlPctNum = Number(universalSlPct)
  const targetUsdPreview =
    Number.isFinite(tpPctNum) && tpPctNum > 0
      ? (estMaxProfitUsd * tpPctNum) / 100
      : 0
  const slUsdPreview =
    Number.isFinite(slPctNum) && slPctNum > 0
      ? (estMaxProfitUsd * slPctNum) / 100
      : 0

  const isLong = positionSide === 'long'
  const targetUsdNum = Number(targetUsd)
  const slUsdNum = Number(slUsd)
  const trailStepNum = trailStepUsd === '' ? null : Number(trailStepUsd)
  const trailAmtNum = trailAmtUsd === '' ? null : Number(trailAmtUsd)
  const trailBothEmpty = trailStepUsd === '' && trailAmtUsd === ''
  const trailBothSet = trailStepUsd !== '' && trailAmtUsd !== ''
  const trailValid =
    trailBothEmpty ||
    (trailBothSet &&
      Number.isFinite(trailStepNum) &&
      Number.isFinite(trailAmtNum) &&
      trailAmtNum > 0 &&
      trailAmtNum <= trailStepNum)
  const callAsk = Number(selectedCall?.call_ask) || 0
  const putAsk = Number(selectedPut?.put_ask) || 0
  const longDebitUsd = (callAsk + putAsk) * qty * Number(OPTIONS_CONTRACT_VALUE)
  const longTargetFar =
    Number.isFinite(targetUsdNum) &&
    longDebitUsd > 0 &&
    targetUsdNum > 5 * longDebitUsd
  const longParamsValid =
    Number.isFinite(targetUsdNum) &&
    targetUsdNum > 0 &&
    Number.isFinite(slUsdNum) &&
    slUsdNum > 0 &&
    trailValid
  const longSlNow = isLong && Number.isFinite(slUsdNum) ? -Math.abs(slUsdNum) : null
  const longTrailPreview = [50, 100, 150].map((peak) => ({
    peak,
    sl: longTrailSlLevel({
      slUsd: slUsdNum,
      peak,
      step: trailStepNum,
      amt: trailAmtNum,
    }),
  }))
  const livePeakExample = 37
  const livePeakSl = longTrailSlLevel({
    slUsd: slUsdNum,
    peak: livePeakExample,
    step: trailStepNum,
    amt: trailAmtNum,
  })

  const callPremPreview = emergencyMode
    ? Number(callEntry) || 0
    : Number(selectedCall?.call_mark_price) || 0
  const putPremPreview = emergencyMode
    ? Number(putEntry) || 0
    : Number(selectedPut?.put_mark_price) || 0
  const callDeltaSlPreview =
    callPremPreview > 0 && Number.isFinite(universalSlPctNum)
      ? (callPremPreview * universalSlPctNum) / 100
      : 0
  const putDeltaSlPreview =
    putPremPreview > 0 && Number.isFinite(universalSlPctNum)
      ? (putPremPreview * universalSlPctNum) / 100
      : 0

  const maxTriggerPct = useMemo(() => {
    if (!slabs) return 150
    if (slabs.mode === 'flat') return Number(slabs.flat_pct) || 150
    if (slabs.mode === 'premium') {
      return Math.max(
        Number(slabs.premium_slab_300) || 0,
        Number(slabs.premium_slab_200) || 0,
        Number(slabs.premium_slab_100) || 0,
        Number(slabs.premium_slab_lt100) || 0,
      )
    }
    return Math.max(
      Number(slabs.slab_24h) || 0,
      Number(slabs.slab_12h) || 0,
      Number(slabs.slab_6h) || 0,
      Number(slabs.slab_lt6h) || 0,
    )
  }, [slabs])

  const deltaSlBelowTrigger =
    Number.isFinite(universalSlPctNum) &&
    universalSlPctNum > 0 &&
    universalSlPctNum <= maxTriggerPct

  const slabsValid = useMemo(() => {
    if (!slabs) return false
    if (slabs.mode === 'flat') return isValidPct(slabs.flat_pct)
    if (slabs.mode === 'premium') {
      return (
        isValidPct(slabs.premium_slab_300) &&
        isValidPct(slabs.premium_slab_200) &&
        isValidPct(slabs.premium_slab_100) &&
        isValidPct(slabs.premium_slab_lt100)
      )
    }
    return (
      isValidPct(slabs.slab_24h) &&
      isValidPct(slabs.slab_12h) &&
      isValidPct(slabs.slab_6h) &&
      isValidPct(slabs.slab_lt6h)
    )
  }, [slabs])

  const canPlaceCommon =
    selectedCall &&
    selectedPut &&
    qty > 0 &&
    Number.isFinite(slippagePctNum) &&
    slippagePctNum >= 0 &&
    slippagePctNum <= 10 &&
    estMaxProfitUsd > 0 &&
    !placing &&
    (!emergencyMode || (callPrem > 0 && putPrem > 0))
  const canPlace = isLong
    ? Boolean(canPlaceCommon && longParamsValid)
    : Boolean(
        canPlaceCommon &&
          Number.isFinite(tpPctNum) &&
          tpPctNum > 0 &&
          Number.isFinite(slPctNum) &&
          slPctNum > 0 &&
          Number.isFinite(universalSlPctNum) &&
          universalSlPctNum >= 100 &&
          universalSlPctNum <= 1000 &&
          slabsValid,
      )

  const buildPayload = () => {
    const base = {
      underlying,
      expiry_date: expiry,
      call_strike: Number(selectedCall.strike),
      call_product_id: Number(selectedCall.call_product_id),
      call_symbol: selectedCall.call_symbol,
      put_strike: Number(selectedPut.strike),
      put_product_id: Number(selectedPut.put_product_id),
      put_symbol: selectedPut.put_symbol,
      quantity: qty,
      tp_pct: tpPctNum,
      sl_pct: slPctNum,
      slippage_pct: slippagePctNum,
      universal_sl_pct: universalSlPctNum,
      trigger_mode: slabs.mode,
      flat_trigger_pct: slabs.mode === 'flat' ? slabs.flat_pct : null,
      slab_24h: slabs.slab_24h,
      slab_12h: slabs.slab_12h,
      slab_6h: slabs.slab_6h,
      slab_lt6h: slabs.slab_lt6h,
      premium_slab_300: slabs.premium_slab_300,
      premium_slab_200: slabs.premium_slab_200,
      premium_slab_100: slabs.premium_slab_100,
      premium_slab_lt100: slabs.premium_slab_lt100,
      call_delta_at_entry: selectedCall.call_delta ?? null,
      put_delta_at_entry: selectedPut.put_delta ?? null,
      is_demo: Boolean(isDemo) && !emergencyMode,
      position_side: isLong ? 'long' : 'short',
      adjustment_enabled: isLong ? false : Boolean(adjustmentEnabled),
    }
    if (isLong) {
      base.target_usd = targetUsdNum
      base.sl_usd = slUsdNum
      base.trail_step_usd = trailBothSet ? trailStepNum : null
      base.trail_amt_usd = trailBothSet ? trailAmtNum : null
    }
    if (emergencyMode) {
      return {
        ...base,
        call_entry_premium: callPrem,
        put_entry_premium: putPrem,
      }
    }
    return base
  }

  const handlePlace = async () => {
    if (!canPlace || !slabs) return
    setConfirmLongOpen(false)
    setPlacing(true)
    setPartialError(null)
    setSuccessResult(null)
    setPlaceStep(0)
    try {
      const payload = buildPayload()
      if (!emergencyMode) {
        setPlaceStep(0)
        // Brief UI step animation while single request runs
        const stepTimer = setInterval(() => {
          setPlaceStep((s) => Math.min(s + 1, PLACE_STEPS.length - 1))
        }, 1200)
        try {
          const result = await initiateTrade(payload)
          clearInterval(stepTimer)
          setPlaceStep(PLACE_STEPS.length - 1)
          setSuccessResult(result)
        } finally {
          clearInterval(stepTimer)
        }
      } else {
        const result = await registerExistingTrade(payload)
        setSuccessResult(result)
      }
    } catch (err) {
      const msg = err.message || 'Failed to place trade'
      if (msg.includes('PARTIAL FILL')) {
        setPartialError(msg)
      } else {
        setToast({ type: 'error', message: msg })
      }
    } finally {
      setPlacing(false)
    }
  }

  if (successResult) {
    const callFill = successResult.call_filled_at
    const putFill = successResult.put_filled_at
    const settleMins =
      successResult.settling_period_minutes ?? (emergencyMode ? 5 : 2)
    return (
      <main className="mx-auto max-w-xl space-y-6 px-4 py-12">
        <div className="rounded-xl border border-green-700/50 bg-green-950/30 p-6 text-center">
          <div className="text-3xl">✅</div>
          <h1 className="mt-2 text-xl font-semibold text-white">
            {emergencyMode
              ? 'Trade Registered Successfully'
              : successResult.is_demo
                ? 'Demo Strangle Registered!'
                : 'Strangle Placed Successfully!'}
          </h1>
          <div className="mt-4 space-y-1 text-sm text-gray-300">
            <div>
              Call: {selectedCall?.call_symbol} sold @ ${fmtMoney(callFill)}
            </div>
            <div>
              Put: {selectedPut?.put_symbol} sold @ ${fmtMoney(putFill)}
            </div>
            <div className="pt-2 font-medium text-green-300">
              Total Premium (per lot): ${fmtMoney((callFill || 0) + (putFill || 0))}
            </div>
            <div className="text-gray-400">
              Bot monitoring starts in {settleMins} minutes.
            </div>
            {successResult.delta_sl_active ? (
              <div className="pt-2 text-green-400">
                🛡️ Delta SL orders placed (Call @ $
                {fmtMoney(successResult.call_sl_trigger_price)} / Put @ $
                {fmtMoney(successResult.put_sl_trigger_price)})
              </div>
            ) : successResult.delta_sl_warning ? (
              <div className="pt-2 text-amber-300">
                ⚠️ {successResult.delta_sl_warning}
              </div>
            ) : !emergencyMode ? (
              <div className="pt-2 text-amber-300">
                ⚠️ Delta SL: Not confirmed — bot monitoring only
              </div>
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => navigate('/')}
            className="mt-6 inline-flex rounded-md bg-blue-500 px-4 py-2 text-sm font-medium text-white hover:bg-blue-400"
          >
            → Go to Dashboard
          </button>
        </div>
      </main>
    )
  }

  const isStraddle =
    selectedCall &&
    selectedPut &&
    Number(selectedCall.strike) === Number(selectedPut.strike)
  const longConfirmMessage = `BUY call ${fmtStrike(selectedCall?.strike)} + BUY put ${fmtStrike(selectedPut?.strike)}, qty ${qty}, debit ≈ $${fmtMoney(estMaxProfitUsd)}, master only — slaves copy nahi karenge.`
  const placeButtonLabel = emergencyMode
    ? 'Register Existing Trade'
    : isDemo
      ? '🎮 Place Demo Trade'
      : isLong
        ? isStraddle
          ? 'Buy Straddle'
          : 'Buy Strangle'
        : 'Place Strangle on Delta Exchange'

  return (
    <main className="mx-auto max-w-6xl space-y-6 px-4 py-8">
      <h1 className="text-2xl font-semibold text-white">
        {emergencyMode
          ? 'Emergency: Register Existing Trade'
          : isLong
            ? 'Place Long Strangle'
            : 'Place Short Strangle'}
      </h1>
      <p className="text-sm text-gray-400">
        {emergencyMode
          ? 'Use only if positions already exist on Delta. Enter actual fill premiums. No new orders will be placed.'
          : isLong
            ? 'Select strikes — the bot will buy the call and put (debit), then monitor target / trailing SL. Slaves will not copy.'
            : 'Select strikes and parameters — the bot will sell the call and put on Delta Exchange, then monitor the position automatically.'}
      </p>
      <LiveTradesPanel />

      {!emergencyMode && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-gray-300">Position</h2>
          <div className="inline-flex rounded-lg border border-gray-700 bg-gray-900/60 p-1">
            <button
              type="button"
              onClick={() => {
                setPositionSide('short')
                setAdjustmentEnabled(true)
              }}
              className={`rounded-md px-4 py-1.5 text-sm font-medium ${
                !isLong
                  ? 'bg-blue-600 text-white'
                  : 'text-gray-400 hover:text-gray-200'
              }`}
            >
              Short
            </button>
            <button
              type="button"
              disabled={!longTradesEnabled}
              title={longTradesEnabled ? '' : 'Long trades disabled'}
              onClick={() => {
                if (!longTradesEnabled) return
                setPositionSide('long')
                setAdjustmentEnabled(false)
              }}
              className={`rounded-md px-4 py-1.5 text-sm font-medium ${
                isLong
                  ? 'bg-emerald-600 text-white'
                  : longTradesEnabled
                    ? 'text-gray-400 hover:text-gray-200'
                    : 'cursor-not-allowed text-gray-600'
              }`}
            >
              Long
            </button>
          </div>
          {!longTradesEnabled && (
            <p className="text-xs text-gray-500">Long trades disabled</p>
          )}
        </section>
      )}

      {emergencyMode && (
        <div className="rounded-md border border-amber-700/50 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
          Emergency registration mode — orders will NOT be placed on Delta.
        </div>
      )}

      {!emergencyMode && (
        <section className="space-y-2 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={isDemo}
              onChange={(e) => setIsDemo(e.target.checked)}
              className="mt-1 h-4 w-4 rounded border-gray-600 bg-gray-900 text-blue-500"
            />
            <span className="text-sm text-gray-300">
              🎮 Demo Trade (Virtual — no real orders){' '}
              <span className="text-gray-500">
                ({isDemo ? 'ON' : 'OFF'})
              </span>
            </span>
          </label>
          {isDemo && (
            <div className="rounded-md border border-yellow-600/50 bg-yellow-950/40 px-3 py-2 text-sm text-yellow-200">
              This is a virtual trade. No real money will be used.
            </div>
          )}
        </section>
      )}

      {partialError && (
        <div className="rounded-xl border border-red-600 bg-red-950/50 px-4 py-3 text-sm text-red-200">
          <div className="font-semibold">⚠️ PARTIAL FILL</div>
          <p className="mt-1 whitespace-pre-wrap">{partialError}</p>
          <p className="mt-2 text-red-300">
            Please check Delta Exchange and close the open leg manually if needed.
          </p>
        </div>
      )}

      {/* 1. Underlying */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-gray-300">Underlying</h2>
        <div className="flex flex-wrap gap-2">
          {UNDERLYINGS.map((u) => (
            <button
              key={u}
              type="button"
              onClick={() => setUnderlying(u)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                underlying === u
                  ? 'bg-blue-500 text-white'
                  : 'bg-gray-800 text-gray-300 hover:bg-gray-700'
              }`}
            >
              {u}
            </button>
          ))}
        </div>
      </section>

      {/* 2. Expiry */}
      <section className="max-w-sm space-y-2">
        <label className="block text-sm font-semibold text-gray-300">
          Expiry
          <select
            value={expiry}
            disabled={loadingExpiries}
            onChange={(e) => {
              setExpiry(e.target.value)
              setSelectedCall(null)
              setSelectedPut(null)
            }}
            className="mt-1 w-full rounded-md border border-gray-700 bg-gray-900 px-3 py-2 text-white"
          >
            {expiries.length === 0 && (
              <option value="">{loadingExpiries ? 'Loading…' : 'No expiries'}</option>
            )}
            {expiries.map((row) => (
              <option key={row.date} value={row.date}>
                {formatExpiryLabel(row)}
              </option>
            ))}
          </select>
        </label>
        {expiryError && (
          <div className="rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
            {expiryError}
          </div>
        )}
      </section>

      {/* 3. Option chain */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-gray-300">Option Chain</h2>
        <OptionChain
          underlying={underlying}
          expiry={expiry}
          selectedCall={selectedCall}
          selectedPut={selectedPut}
          onCallSelect={setSelectedCall}
          onPutSelect={setSelectedPut}
          onChainMeta={onChainMeta}
          quantity={qty || 1}
        />
      </section>

      {/* 4. Payoff (lot size above chart so P&L scales visibly) */}
      <section className="space-y-2">
        <div className="flex flex-wrap items-end gap-3 rounded-xl border border-gray-700 bg-gray-800/60 px-4 py-3">
          <label className="text-sm text-gray-300">
            Quantity (Lots)
            <input
              type="number"
              min={1}
              step={1}
              value={quantity}
              onChange={(e) => setQuantity(Number(e.target.value))}
              className="mt-1 w-32 rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
          </label>
          <p className="pb-2 text-xs text-gray-500">
            Graph shows real Delta USD (premium × lots ×{' '}
            {OPTIONS_CONTRACT_VALUE})
            {isLong ? (
              <>
                {' '}
                · debit{' '}
                <span className="text-amber-300">
                  $
                  {estMaxProfitUsd.toLocaleString('en-US', {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 4,
                  })}
                </span>
              </>
            ) : (
              <>
                {' '}
                · Est. max profit{' '}
                <span className="text-green-400">
                  $
                  {estMaxProfitUsd.toLocaleString('en-US', {
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 4,
                  })}
                </span>
              </>
            )}
            <span className="text-gray-600">
              {' '}
              · mark pts ${totalPremiumPoints.toFixed(2)}
            </span>
          </p>
        </div>
        <PayoffGraph
          callStrike={selectedCall?.strike}
          putStrike={selectedPut?.strike}
          callPremium={
            emergencyMode ? callPrem : selectedCall?.call_mark_price
          }
          putPremium={emergencyMode ? putPrem : selectedPut?.put_mark_price}
          quantity={qty || 1}
          currentPrice={currentPrice || undefined}
          expiryDate={expiry || undefined}
          mode={isLong ? 'long' : 'short'}
        />
      </section>

      {/* 5. Trade parameters */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">Trade Parameters</h2>
        {!isLong && (
        <div className="rounded-lg border border-gray-700 bg-gray-900/50 px-3 py-2 text-sm text-gray-300">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>Est. Max Profit (locked at deploy)</span>
            <span className="font-semibold text-green-400">
              ${fmtMoney(estMaxProfitUsd)}
            </span>
          </div>
          <p className="mt-1 text-[11px] text-gray-500">
            (call + put premium) × lots × {OPTIONS_CONTRACT_VALUE} · updates live
            with strikes / qty · TP/SL $ never change after adjustments
          </p>
        </div>
        )}
        {isLong ? (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-sm text-gray-300">
                Target $
                <input
                  type="number"
                  min={0.01}
                  step={1}
                  value={targetUsd}
                  onChange={(e) => setTargetUsd(e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              <label className="text-sm text-gray-300">
                SL $
                <input
                  type="number"
                  min={0.01}
                  step={1}
                  value={slUsd}
                  onChange={(e) => setSlUsd(e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              <label className="text-sm text-gray-300">
                Trail step $
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={trailStepUsd}
                  onChange={(e) => setTrailStepUsd(e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  placeholder="optional"
                />
              </label>
              <label className="text-sm text-gray-300">
                Trail amount $
                <input
                  type="number"
                  min={0}
                  step={1}
                  value={trailAmtUsd}
                  onChange={(e) => setTrailAmtUsd(e.target.value)}
                  className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  placeholder="optional"
                />
              </label>
            </div>
            <p className="text-xs text-gray-400">
              Ye poore trade ke $ hain. Is qty par max loss (debit) ≈ $
              {fmtMoney(longDebitUsd)}.
            </p>
            {longTargetFar && (
              <p className="text-xs text-amber-300">
                Target bahut door hai is qty ke liye.
              </p>
            )}
            {!trailValid && (
              <p className="text-xs text-amber-300">
                Trail fields must both be empty or both filled, with 0 &lt; amount ≤
                step.
              </p>
            )}
            <div className="rounded-lg border border-gray-700 bg-gray-900/50 px-3 py-2 text-xs text-gray-300">
              <p>
                SL abhi {longSlNow != null ? `−${fmtMoney(Math.abs(longSlNow))}` : '—'}.
                Peak {livePeakExample} par SL = {fmtSignedTrail(livePeakSl)}.
              </p>
              <table className="mt-2 w-full max-w-xs text-left text-[11px] text-gray-400">
                <thead>
                  <tr>
                    <th className="py-1">Peak</th>
                    <th className="py-1">SL level</th>
                  </tr>
                </thead>
                <tbody>
                  {longTrailPreview.map((row) => (
                    <tr key={row.peak}>
                      <td className="py-0.5">{row.peak}</td>
                      <td className="py-0.5">{fmtSignedTrail(row.sl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm text-gray-300">
            Profit Target (%)
            <input
              type="number"
              min={1}
              max={500}
              step={1}
              value={tpPct}
              onChange={(e) => setTpPct(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
              placeholder="50"
            />
            <span className="mt-1 block text-xs text-gray-400">
              = ~${fmtMoney(targetUsdPreview)} ({fmtMoney(tpPctNum) || '—'}% of $
              {fmtMoney(estMaxProfitUsd)} max)
            </span>
          </label>
          <label className="text-sm text-gray-300">
            Stop Loss (%)
            <input
              type="number"
              min={1}
              max={500}
              step={1}
              value={slPct}
              onChange={(e) => setSlPct(e.target.value)}
              className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
              placeholder="100"
            />
            <span className="mt-1 block text-xs text-gray-400">
              = ~${fmtMoney(slUsdPreview)} ({fmtMoney(slPctNum) || '—'}% of $
              {fmtMoney(estMaxProfitUsd)} max)
            </span>
          </label>
        </div>
        )}
        <div className="rounded-lg border border-gray-700 bg-gray-900/50 px-3 py-3">
          <label className="block text-sm text-gray-300">
            Slippage Estimate (%)
            <input
              type="number"
              min={0}
              max={10}
              step={0.1}
              value={slippagePct}
              onChange={(e) => setSlippagePct(e.target.value)}
              className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
              placeholder="2"
            />
            <span className="mt-1 block text-xs text-gray-500">
              Applied to Net MTM calculation (0–10%). Default 2%.
            </span>
          </label>
        </div>
        {!isLong && (
        <div className="rounded-lg border border-amber-800/50 bg-amber-950/20 px-3 py-3">
          <div className="mb-2 text-sm font-semibold text-amber-200">
            SAFETY STOP LOSS (Delta Exchange Order)
          </div>
          <label className="block text-sm text-gray-300">
            Stop Loss at (% of each leg entry)
            <input
              type="number"
              min={100}
              max={1000}
              step={1}
              value={universalSlPct}
              onChange={(e) => setUniversalSlPct(e.target.value)}
              className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
              placeholder="200"
            />
          </label>
          <div className="mt-2 space-y-1 text-xs text-gray-400">
            <div>
              Call SL triggers at: ~${fmtMoney(callDeltaSlPreview)} (
              {fmtMoney(universalSlPctNum) || '—'}% of $
              {fmtMoney(callPremPreview)})
            </div>
            <div>
              Put SL triggers at: ~${fmtMoney(putDeltaSlPreview)} (
              {fmtMoney(universalSlPctNum) || '—'}% of $
              {fmtMoney(putPremPreview)})
            </div>
          </div>
          {deltaSlBelowTrigger && (
            <p className="mt-2 text-xs text-amber-300">
              Warning: Delta SL % ({fmtMoney(universalSlPctNum)}) is ≤ max
              adjustment trigger ({fmtMoney(maxTriggerPct)}%). Delta may close
              a leg before the bot can adjust.
            </p>
          )}
          <p className="mt-2 text-xs text-gray-500">
            These are placed as stop orders on Delta as backup if the bot
            misses an exit. Updated automatically on each adjustment.
          </p>
        </div>
        )}
        {emergencyMode && (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm text-gray-300">
              Call Fill Premium ($)
              <input
                type="number"
                min={0}
                step={0.01}
                value={callEntry}
                onChange={(e) => setCallEntry(e.target.value)}
                className="mt-1 w-full rounded-md border border-amber-700/50 bg-gray-900 px-3 py-2 text-white"
              />
            </label>
            <label className="text-sm text-gray-300">
              Put Fill Premium ($)
              <input
                type="number"
                min={0}
                step={0.01}
                value={putEntry}
                onChange={(e) => setPutEntry(e.target.value)}
                className="mt-1 w-full rounded-md border border-amber-700/50 bg-gray-900 px-3 py-2 text-white"
              />
            </label>
          </div>
        )}
      </section>

      {/* 6. Adjustment */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <label
          className={`flex items-center gap-3 text-sm text-gray-200 ${
            isLong ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'
          }`}
          title={isLong ? 'Long adjustment abhi available nahi' : undefined}
        >
          <input
            type="checkbox"
            disabled={isLong}
            checked={isLong ? false : adjustmentEnabled}
            onChange={(e) => {
              if (isLong) return
              setAdjustmentEnabled(e.target.checked)
            }}
            className="h-4 w-4 rounded border-gray-600 bg-gray-900 text-blue-500 disabled:cursor-not-allowed"
          />
          Adjustment {isLong || !adjustmentEnabled ? 'OFF' : 'ON'}
        </label>
        {!isLong && adjustmentEnabled && (
          <AdjustmentSlabs onChange={onSlabsChange} defaultMode="slab" />
        )}
      </section>

      {/* 7. Summary */}
      {(selectedCall || selectedPut) && (
        <section className="space-y-2 rounded-xl border border-gray-700 bg-gray-800/60 p-4 text-sm text-gray-200">
          <h2 className="font-semibold text-white">Trade Summary</h2>
          <div className="text-gray-400">
            {emergencyMode ? 'Registering:' : isLong ? 'Will buy:' : 'Will sell:'}
          </div>
          {selectedCall && (
            <div>
              CALL ${fmtStrike(selectedCall.strike)}
              {!emergencyMode && (
                <>
                  {' '}
                  ~mark ${fmtMoney(selectedCall.call_mark_price)} × {qty}
                </>
              )}
              {emergencyMode && (
                <>
                  {' '}
                  @ ${fmtMoney(callPrem)} × {qty}
                </>
              )}
            </div>
          )}
          {selectedPut && (
            <div>
              PUT ${fmtStrike(selectedPut.strike)}
              {!emergencyMode && (
                <>
                  {' '}
                  ~mark ${fmtMoney(selectedPut.put_mark_price)} × {qty}
                </>
              )}
              {emergencyMode && (
                <>
                  {' '}
                  @ ${fmtMoney(putPrem)} × {qty}
                </>
              )}
            </div>
          )}
          {selectedCall && selectedPut && (
            <>
              {!emergencyMode && (
                <div className="pt-1 text-gray-400">
                  Est. premium (marks): ${fmtMoney(totalPremiumPoints)} · Real
                  max profit:{' '}
                  <span className="text-green-400">
                    ${fmtMoney(estMaxProfitUsd)} USD
                  </span>{' '}
                  ({qty} lots × {OPTIONS_CONTRACT_VALUE})
                </div>
              )}
              {emergencyMode && (
                <div className="pt-1">
                  Premium points: ${fmtMoney(totalPremiumPoints)} · Real USD:{' '}
                  ${fmtMoney(estMaxProfitUsd)}
                </div>
              )}
              <div>
                {isLong
                  ? `Target: $${fmtMoney(targetUsdNum)} · SL: $${fmtMoney(slUsdNum)}`
                  : `Target: ${fmtMoney(tpPctNum)}% ≈ $${fmtMoney(targetUsdPreview)} · SL: ${fmtMoney(slPctNum)}% ≈ $${fmtMoney(slUsdPreview)}`}
              </div>
            </>
          )}
        </section>
      )}

      {/* 8. Place */}
      <button
        type="button"
        disabled={!canPlace}
        onClick={() => {
          if (isLong && !emergencyMode) setConfirmLongOpen(true)
          else handlePlace()
        }}
        className={`flex w-full items-center justify-center gap-2 rounded-lg px-4 py-3 text-sm font-semibold transition ${
          canPlace
            ? 'bg-green-600 text-white hover:bg-green-500'
            : 'cursor-not-allowed bg-gray-700 text-gray-400'
        }`}
      >
        {placing ? (
          <>
            <LoadingSpinner size="sm" />
            {emergencyMode
              ? 'Registering…'
              : isDemo
                ? 'Registering demo trade…'
                : PLACE_STEPS[placeStep] || 'Placing…'}
          </>
        ) : (
          placeButtonLabel
        )}
      </button>

      {!emergencyMode && (
        <p className="text-center text-xs text-gray-500">
          Already have open legs on Delta?{' '}
          <Link
            to="/new-trade?emergency=1"
            className="text-gray-400 underline hover:text-gray-200"
          >
            Emergency register
          </Link>{' '}
          (Settings also links here)
        </p>
      )}

      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          onClose={() => setToast(null)}
        />
      )}

      <ConfirmDialog
        isOpen={confirmLongOpen}
        title="Confirm long entry"
        message={longConfirmMessage}
        confirmLabel={isStraddle ? 'Buy Straddle' : 'Buy Strangle'}
        onConfirm={handlePlace}
        onCancel={() => setConfirmLongOpen(false)}
        confirmDisabled={placing}
      />
    </main>
  )
}

function fmtSignedTrail(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  if (n < 0) return `−${fmtMoney(Math.abs(n))}`
  if (n > 0) return `+${fmtMoney(n)}`
  return fmtMoney(0)
}
