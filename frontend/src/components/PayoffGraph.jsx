import { useEffect, useMemo, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  OPTIONS_CONTRACT_VALUE,
  positionScale,
} from '../utils/contractValue'

/**
 * scale = lots × OPTIONS_CONTRACT_VALUE (Delta micro-lot USD).
 * Premiums/strikes stay in exchange units; only USD P&L is scaled.
 */
function calcExpiryPnl(
  price,
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  scale,
) {
  const totalPremium = (callPremium + putPremium) * scale
  if (putStrike <= price && price <= callStrike) {
    return totalPremium
  }
  if (price > callStrike) {
    return totalPremium - (price - callStrike) * scale
  }
  return totalPremium - (putStrike - price) * scale
}

/**
 * Intermediate (pre-expiry) MTM approximation — Delta-like shape.
 * tau: 0 = at expiry, 1 = now (full time left).
 * Uses sqrt time decay so the curve visibly flattens toward "now"
 * while Y-axis stays fixed to expiry scale.
 */
function calcTimedPnl(
  price,
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  scale,
  tau,
) {
  const expiryPnl = calcExpiryPnl(
    price,
    callStrike,
    putStrike,
    callPremium,
    putPremium,
    scale,
  )
  if (tau <= 0.001) return expiryPnl
  if (tau >= 0.999) return 0

  // Theta proxy: more time left → closer to flat (just entered ≈ 0 MTM)
  const decay = 1 - Math.sqrt(Math.min(1, Math.max(0, tau)))

  // Soften far wings while time remains (options still have extrinsic value)
  const premium = (callPremium + putPremium) * scale
  const callIntr = Math.max(0, price - callStrike) * scale
  const putIntr = Math.max(0, putStrike - price) * scale
  const intrinsicShortPnl = premium - callIntr - putIntr

  // Blend expiry tent with intrinsic-based MTM; weight by remaining time
  const blended = expiryPnl * decay + intrinsicShortPnl * (1 - decay) * 0.35
  return blended
}

function hoursUntilExpiryIst(expiryDate) {
  if (!expiryDate) return 24
  try {
    const end = new Date(`${expiryDate}T17:30:00+05:30`)
    const hours = (end.getTime() - Date.now()) / 3600000
    return Math.max(0.25, hours)
  } catch {
    return 24
  }
}

function priceStatus(price, putStrike, callStrike, beLow, beHigh) {
  if (price >= putStrike && price <= callStrike) {
    return { label: '✅ In profit zone', className: 'text-green-400' }
  }
  if (price >= beLow && price <= beHigh) {
    return { label: '⚠️ Near breakeven', className: 'text-yellow-400' }
  }
  return { label: '❌ Beyond breakeven', className: 'text-red-400' }
}

/**
 * Build payoff curves. Y-axis ALWAYS based on full expiry P&L in real Delta USD
 * (premium × lots × contract_value) so sliding time changes the orange curve
 * without rescaling axes.
 */
function buildPayoffData({
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  quantity,
  currentPrice,
  tau,
}) {
  const POINTS = 101
  const scale = positionScale(quantity)
  const maxProfitExpiry = (callPremium + putPremium) * scale
  const premiumPerUnit = callPremium + putPremium
  const breakevenUpper = callStrike + premiumPerUnit
  const breakevenLower = putStrike - premiumPerUnit

  const wing = Math.max(callStrike - putStrike, premiumPerUnit * 2, 100)
  let xMin = putStrike - wing * 0.5
  let xMax = callStrike + wing * 0.5
  xMin = Math.min(xMin, breakevenLower, currentPrice) - wing * 0.05
  xMax = Math.max(xMax, breakevenUpper, currentPrice) + wing * 0.05

  // Fixed Y from expiry USD scale — never shrink with tau
  const peak = Math.max(Math.abs(maxProfitExpiry), 0.01)
  const yAxisMax = peak * 1.15
  const yAxisMin = -peak * 1.5

  const step = (xMax - xMin) / (POINTS - 1)
  const points = []
  let timedMax = -Infinity
  let timedMin = Infinity
  for (let i = 0; i < POINTS; i += 1) {
    const price = xMin + step * i
    const expiryPnl = calcExpiryPnl(
      price,
      callStrike,
      putStrike,
      callPremium,
      putPremium,
      scale,
    )
    const timedPnl = calcTimedPnl(
      price,
      callStrike,
      putStrike,
      callPremium,
      putPremium,
      scale,
      tau,
    )
    timedMax = Math.max(timedMax, timedPnl)
    timedMin = Math.min(timedMin, timedPnl)
    const clipped = Math.max(yAxisMin, Math.min(yAxisMax, timedPnl))
    points.push({
      price,
      expiryPnl,
      timedPnl,
      pnlLine: clipped,
      profitFill: clipped >= 0 ? clipped : 0,
      lossFill: clipped < 0 ? clipped : 0,
    })
  }

  return {
    points,
    maxProfit: maxProfitExpiry,
    timedMaxProfit: timedMax,
    timedMinPnl: timedMin,
    yAxisMin,
    yAxisMax,
    xMin,
    xMax,
    breakevenUpper,
    breakevenLower,
  }
}

function formatPriceTick(v) {
  return `$${(Number(v) / 1000).toFixed(1)}K`
}

function formatPnlTick(v) {
  const n = Number(v)
  const sign = n > 0 ? '+' : ''
  if (Math.abs(n) >= 1000) {
    return `${sign}$${(n / 1000).toFixed(1)}K`
  }
  if (Math.abs(n) >= 10) {
    return `${sign}$${n.toFixed(1)}`
  }
  return `${sign}$${n.toFixed(2)}`
}

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '-' : ''
  const abs = Math.abs(n)
  const digits = abs >= 10 ? 2 : 4
  return `${sign}$${abs.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  })}`
}

function CustomTooltip({ active, payload, timeLabel }) {
  if (!active || !payload?.length) return null
  const row = payload[0]?.payload
  if (!row) return null
  const timed = Number(row.timedPnl || 0)
  const expiry = Number(row.expiryPnl || 0)
  return (
    <div className="rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-xs text-gray-100 shadow-lg">
      <div>
        Price: $
        {Number(row.price).toLocaleString('en-US', { maximumFractionDigits: 0 })}
      </div>
      <div>
        {timeLabel}:{' '}
        <span className={timed >= 0 ? 'text-orange-300' : 'text-red-400'}>
          {timed >= 0 ? '+' : ''}${timed.toFixed(2)}
        </span>
      </div>
      <div>
        At Expiry:{' '}
        <span className={expiry >= 0 ? 'text-green-400' : 'text-red-400'}>
          {expiry >= 0 ? '+' : ''}${expiry.toFixed(2)}
        </span>
      </div>
    </div>
  )
}

/**
 * Props: callStrike, putStrike, callPremium, putPremium, quantity,
 *        currentPrice, expiryDate (YYYY-MM-DD optional)
 */
export default function PayoffGraph({
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  quantity = 1,
  currentPrice,
  expiryDate,
  /** If set, slider starts here (e.g. hours_to_expiry = "now"). Default 0 = expiry. */
  initialHoursRemaining = null,
  emptyMessage = 'Select both Call and Put strikes to see payoff',
  compact = false,
}) {
  const hoursToExpiry = useMemo(
    () => hoursUntilExpiryIst(expiryDate),
    [expiryDate],
  )

  // hoursRemaining: 0 = Expiry, hoursToExpiry = Now
  // Only reset when the position identity changes — not on every hours_to_expiry tick
  const [hoursRemaining, setHoursRemaining] = useState(null)

  useEffect(() => {
    const total = Math.max(hoursToExpiry, 0.25)
    const start =
      initialHoursRemaining != null
        ? Math.min(Math.max(Number(initialHoursRemaining), 0), total)
        : 0
    setHoursRemaining(start)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- lock slider until position identity changes
  }, [expiryDate, callStrike, putStrike])

  const qty = Math.max(1, Number(quantity) || 1)

  const ready =
    callStrike != null &&
    putStrike != null &&
    callPremium != null &&
    putPremium != null &&
    currentPrice != null &&
    Number(currentPrice) > 0

  const effectiveHours =
    hoursRemaining != null
      ? hoursRemaining
      : initialHoursRemaining != null
        ? Number(initialHoursRemaining)
        : 0

  // tau: 0 = expiry, 1 = now
  const tau = useMemo(() => {
    const total = Math.max(hoursToExpiry, 0.25)
    const remaining = Math.min(Math.max(Number(effectiveHours), 0), total)
    return remaining / total
  }, [effectiveHours, hoursToExpiry])

  const chart = useMemo(() => {
    if (!ready) return null
    return buildPayoffData({
      callStrike: Number(callStrike),
      putStrike: Number(putStrike),
      callPremium: Number(callPremium),
      putPremium: Number(putPremium),
      quantity: qty,
      currentPrice: Number(currentPrice),
      tau,
    })
  }, [
    ready,
    callStrike,
    putStrike,
    callPremium,
    putPremium,
    qty,
    currentPrice,
    tau,
  ])

  const status = useMemo(() => {
    if (!chart) return null
    return priceStatus(
      Number(currentPrice),
      Number(putStrike),
      Number(callStrike),
      chart.breakevenLower,
      chart.breakevenUpper,
    )
  }, [chart, currentPrice, putStrike, callStrike])

  if (!ready || !chart) {
    return (
      <div
        className={`flex items-center justify-center rounded-xl border border-dashed border-gray-700 bg-gray-900 text-sm text-gray-400 ${
          compact ? 'h-48' : 'h-80'
        }`}
      >
        {emptyMessage}
      </div>
    )
  }

  const profitZoneWidth = Number(callStrike) - Number(putStrike)
  const isExpiryView = Number(effectiveHours) <= 0.01
  const timeLabel = isExpiryView
    ? 'At Expiry'
    : Number(effectiveHours) >= hoursToExpiry - 0.05
      ? 'If Closed Now'
      : `In ${Number(effectiveHours).toFixed(1)}h`

  const nowLabel = `Now: $${(Number(currentPrice) / 1000).toFixed(1)}K`
  const beLowLabel = `BE: $${(chart.breakevenLower / 1000).toFixed(1)}K`
  const beHighLabel = `BE: $${(chart.breakevenUpper / 1000).toFixed(1)}K`

  const presetHours = [
    { label: 'Expiry', value: 0 },
    { label: '1h', value: Math.min(1, hoursToExpiry) },
    { label: '3h', value: Math.min(3, hoursToExpiry) },
    { label: '6h', value: Math.min(6, hoursToExpiry) },
    { label: '12h', value: Math.min(12, hoursToExpiry) },
    { label: 'Now', value: hoursToExpiry },
  ]

  const totalPremiumUsd =
    (Number(callPremium) + Number(putPremium)) * positionScale(qty)
  const contractNotional = qty * OPTIONS_CONTRACT_VALUE

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-900 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-white">
          Payoff — {timeLabel}
        </h3>
        <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400">
          <span>{hoursToExpiry.toFixed(1)}h to expiry</span>
          <span className="rounded bg-gray-800 px-2 py-0.5 text-orange-300">
            × {qty} lot{qty > 1 ? 's' : ''} (= {contractNotional.toFixed(3)} Delta
            size) · max ${totalPremiumUsd.toFixed(2)} USD
          </span>
        </div>
      </div>

      <div className="mb-4 space-y-2 rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-3">
        <div className="flex flex-wrap gap-1.5">
          {presetHours.map((p) => (
            <button
              key={p.label}
              type="button"
              onClick={() => setHoursRemaining(p.value)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                Math.abs(effectiveHours - p.value) < 0.05
                  ? 'bg-orange-500 text-white'
                  : 'bg-gray-700 text-gray-300 hover:bg-gray-600'
              }`}
            >
              {p.label}
            </button>
          ))}
        </div>
        <label className="block text-xs text-gray-400">
          Hours remaining until target
          <input
            type="range"
            min={0}
            max={Number(hoursToExpiry.toFixed(2))}
            step={0.25}
            value={Math.min(Number(effectiveHours) || 0, hoursToExpiry)}
            onChange={(e) => setHoursRemaining(Number(e.target.value))}
            className="mt-1 w-full accent-orange-500"
          />
          <div className="mt-1 flex justify-between text-[11px] text-gray-500">
            <span>Expiry (full tent)</span>
            <span className="text-orange-300">{timeLabel}</span>
            <span>Now (flat)</span>
          </div>
        </label>
        <div className="flex flex-wrap gap-3 text-[11px] text-gray-500">
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-0.5 w-4 bg-green-500" /> On Expiry
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-0.5 w-4 bg-orange-500" /> {timeLabel}
          </span>
        </div>
      </div>

      <div className={`w-full ${compact ? 'h-64' : 'h-80'}`}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart
            data={chart.points}
            margin={{ top: 18, right: 16, left: 8, bottom: 8 }}
          >
            <CartesianGrid stroke="#374151" strokeDasharray="3 3" />
            <XAxis
              dataKey="price"
              type="number"
              domain={[chart.xMin, chart.xMax]}
              tickFormatter={formatPriceTick}
              stroke="#9ca3af"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
            />
            <YAxis
              domain={[chart.yAxisMin, chart.yAxisMax]}
              ticks={[
                chart.yAxisMin,
                chart.yAxisMin / 2,
                0,
                chart.yAxisMax / 2,
                chart.yAxisMax,
              ]}
              tickFormatter={formatPnlTick}
              stroke="#9ca3af"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              allowDataOverflow
              width={56}
            />
            <Tooltip content={<CustomTooltip timeLabel={timeLabel} />} />

            <Area
              type="monotone"
              dataKey="profitFill"
              fill="#22c55e"
              fillOpacity={0.25}
              stroke="none"
              isAnimationActive={false}
            />
            <Area
              type="monotone"
              dataKey="lossFill"
              fill="#ef4444"
              fillOpacity={0.2}
              stroke="none"
              isAnimationActive={false}
            />

            <ReferenceLine y={0} stroke="#9ca3af" strokeWidth={1} />
            <ReferenceLine
              x={chart.breakevenLower}
              stroke="#ef4444"
              strokeDasharray="3 3"
              label={{
                value: beLowLabel,
                position: 'insideTopLeft',
                fill: '#ef4444',
                fontSize: 10,
              }}
            />
            <ReferenceLine
              x={chart.breakevenUpper}
              stroke="#ef4444"
              strokeDasharray="3 3"
              label={{
                value: beHighLabel,
                position: 'insideTopRight',
                fill: '#ef4444',
                fontSize: 10,
              }}
            />
            <ReferenceLine
              x={Number(currentPrice)}
              stroke="#22c55e"
              strokeWidth={1.5}
              label={{
                value: nowLabel,
                position: 'insideBottom',
                fill: '#86efac',
                fontSize: 11,
              }}
            />

            {/* Green = full expiry payoff (reference, like Delta) */}
            <Line
              type="linear"
              dataKey="expiryPnl"
              stroke="#22c55e"
              strokeWidth={1.5}
              strokeDasharray={isExpiryView ? undefined : '4 4'}
              dot={false}
              isAnimationActive={false}
              name="On Expiry"
            />

            {/* Orange = selected time target */}
            <Line
              type="monotone"
              dataKey="timedPnl"
              stroke="#f97316"
              strokeWidth={2.5}
              dot={false}
              isAnimationActive={false}
              name={timeLabel}
              activeDot={{ r: 4, fill: '#f97316' }}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="mt-3 grid grid-cols-1 gap-2 border-t border-gray-700 pt-3 text-xs sm:grid-cols-2">
        <div className="text-gray-300">
          Max Profit (real USD × {qty} × {OPTIONS_CONTRACT_VALUE}):{' '}
          <span className="font-medium text-green-400">
            {fmtMoney(chart.maxProfit)}
          </span>
          {!isExpiryView && (
            <span className="text-gray-500">
              {' '}
              · at target ~{fmtMoney(chart.timedMaxProfit)}
            </span>
          )}
        </div>
        <div className="text-gray-300">
          Profit Zone Width:{' '}
          <span className="font-medium text-white">
            ${profitZoneWidth.toLocaleString('en-US', { maximumFractionDigits: 0 })}
          </span>
        </div>
        <div className="text-gray-300">
          Breakeven:{' '}
          <span className="font-medium text-white">
            ${chart.breakevenLower.toLocaleString('en-US', { maximumFractionDigits: 0 })}{' '}
            – $
            {chart.breakevenUpper.toLocaleString('en-US', { maximumFractionDigits: 0 })}
          </span>
        </div>
        <div className="text-gray-300">
          Now:{' '}
          <span className="font-medium text-white">
            $
            {Number(currentPrice).toLocaleString('en-US', {
              maximumFractionDigits: 0,
            })}
          </span>{' '}
          <span className={status.className}>{status.label}</span>
        </div>
      </div>
    </div>
  )
}
