import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import AdjustmentSlabs from '../components/AdjustmentSlabs'
import OptionChain from '../components/OptionChain'
import PayoffGraph from '../components/PayoffGraph'
import Toast from '../components/ui/Toast'
import LoadingSpinner from '../components/ui/LoadingSpinner'
import { getExpiries, initiateTrade, registerExistingTrade } from '../services/api'
import {
  OPTIONS_CONTRACT_VALUE,
  toUsdPnl,
} from '../utils/contractValue'
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
  const [slabs, setSlabs] = useState(null)

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
  const targetUsdPreview =
    Number.isFinite(tpPctNum) && tpPctNum > 0
      ? (estMaxProfitUsd * tpPctNum) / 100
      : 0
  const slUsdPreview =
    Number.isFinite(slPctNum) && slPctNum > 0
      ? (estMaxProfitUsd * slPctNum) / 100
      : 0

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

  const canPlace =
    selectedCall &&
    selectedPut &&
    qty > 0 &&
    Number.isFinite(tpPctNum) &&
    tpPctNum > 0 &&
    Number.isFinite(slPctNum) &&
    slPctNum > 0 &&
    Number.isFinite(slippagePctNum) &&
    slippagePctNum >= 0 &&
    slippagePctNum <= 10 &&
    estMaxProfitUsd > 0 &&
    slabsValid &&
    !placing &&
    (!emergencyMode || (callPrem > 0 && putPrem > 0))

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

  return (
    <main className="mx-auto max-w-6xl space-y-6 px-4 py-8">
      <h1 className="text-2xl font-semibold text-white">
        {emergencyMode
          ? 'Emergency: Register Existing Trade'
          : 'Place Short Strangle'}
      </h1>
      <p className="text-sm text-gray-400">
        {emergencyMode
          ? 'Use only if positions already exist on Delta. Enter actual fill premiums. No new orders will be placed.'
          : 'Select strikes and parameters — the bot will sell the call and put on Delta Exchange, then monitor the position automatically.'}
      </p>

      {emergencyMode && (
        <div className="rounded-md border border-amber-700/50 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
          Emergency registration mode — orders will NOT be placed on Delta.
        </div>
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
            {OPTIONS_CONTRACT_VALUE}) · Est. max profit{' '}
            <span className="text-green-400">
              $
              {estMaxProfitUsd.toLocaleString('en-US', {
                minimumFractionDigits: 2,
                maximumFractionDigits: 4,
              })}
            </span>
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
        />
      </section>

      {/* 5. Trade parameters */}
      <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
        <h2 className="text-sm font-semibold text-white">Trade Parameters</h2>
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

      {/* 6. Adjustment slabs */}
      <AdjustmentSlabs onChange={onSlabsChange} defaultMode="slab" />

      {/* 7. Summary */}
      {(selectedCall || selectedPut) && (
        <section className="space-y-2 rounded-xl border border-gray-700 bg-gray-800/60 p-4 text-sm text-gray-200">
          <h2 className="font-semibold text-white">Trade Summary</h2>
          <div className="text-gray-400">
            {emergencyMode ? 'Registering:' : 'Will sell:'}
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
                Target: {fmtMoney(tpPctNum)}% ≈ ${fmtMoney(targetUsdPreview)} ·
                SL: {fmtMoney(slPctNum)}% ≈ ${fmtMoney(slUsdPreview)}
              </div>
            </>
          )}
        </section>
      )}

      {/* 8. Place */}
      <button
        type="button"
        disabled={!canPlace}
        onClick={handlePlace}
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
              : PLACE_STEPS[placeStep] || 'Placing…'}
          </>
        ) : emergencyMode ? (
          'Register Existing Trade'
        ) : (
          'Place Strangle on Delta Exchange'
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
    </main>
  )
}
