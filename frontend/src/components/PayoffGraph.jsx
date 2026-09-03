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
 * Expiry PnL (USD) — iron condor / short strangle.
 *
 * per-lot pts:
 *   pnl(S) = net_credit
 *            − max(0, S − short_call_K)
 *            + max(0, S − wing_call_K)   // omit if wings off
 *            − max(0, short_put_K − S)
 *            + max(0, wing_put_K − S)    // omit if wings off
 * USD = pts × qty × contract_value  (= scale)
 */
function calcExpiryPnl(
  price,
  callStrike,
  putStrike,
  netCreditPts,
  scale,
  wingCallStrike,
  wingPutStrike,
) {
  let pts = Number(netCreditPts) || 0
  pts -= Math.max(0, price - callStrike)
  pts -= Math.max(0, putStrike - price)
  if (wingCallStrike != null && Number.isFinite(Number(wingCallStrike))) {
    pts += Math.max(0, price - Number(wingCallStrike))
  }
  if (wingPutStrike != null && Number.isFinite(Number(wingPutStrike))) {
    pts += Math.max(0, Number(wingPutStrike) - price)
  }
  return pts * scale
}

/**
 * Intermediate (pre-expiry) MTM approximation — Delta-like shape.
 * Uses current marks (all four legs when wings on) for the intrinsic blend.
 * tau: 0 = at expiry, 1 = now (full time left).
 */
function calcTimedPnl(
  price,
  callStrike,
  putStrike,
  entryNetCreditPts,
  markNetCreditPts,
  scale,
  tau,
  wingCallStrike,
  wingPutStrike,
) {
  const expiryPnl = calcExpiryPnl(
    price,
    callStrike,
    putStrike,
    entryNetCreditPts,
    scale,
    wingCallStrike,
    wingPutStrike,
  )
  if (tau <= 0.001) return expiryPnl
  if (tau >= 0.999) {
    // "If closed now" — flat MTM vs entry using current marks (S-independent)
    return (markNetCreditPts - entryNetCreditPts) * scale
  }

  const decay = 1 - Math.sqrt(Math.min(1, Math.max(0, tau)))
  const intrinsicNow = calcExpiryPnl(
    price,
    callStrike,
    putStrike,
    markNetCreditPts,
    scale,
    wingCallStrike,
    wingPutStrike,
  )
  return expiryPnl * decay + intrinsicNow * (1 - decay) * 0.35
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

function netCreditPts(callPrem, putPrem, wingCallPrem, wingPutPrem, wingsOn) {
  let nc = Number(callPrem || 0) + Number(putPrem || 0)
  if (wingsOn) {
    nc -= Number(wingCallPrem || 0)
    nc -= Number(wingPutPrem || 0)
  }
  return nc
}

/**
 * Build payoff curves. Y-axis ALWAYS based on full expiry P&L in real Delta USD.
 * X-range: spot ±20% (covers wings for typical OTM widths).
 */
function buildPayoffData({
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  quantity,
  currentPrice,
  tau,
  wingCallStrike,
  wingPutStrike,
  wingCallPremium,
  wingPutPremium,
  callMarkPremium,
  putMarkPremium,
  wingCallMarkPremium,
  wingPutMarkPremium,
  maxLossUsdProp,
}) {
  const POINTS = 101
  const scale = positionScale(quantity)
  const wingsOn =
    wingCallStrike != null &&
    wingPutStrike != null &&
    Number.isFinite(Number(wingCallStrike)) &&
    Number.isFinite(Number(wingPutStrike))

  const entryNc = netCreditPts(
    callPremium,
    putPremium,
    wingCallPremium,
    wingPutPremium,
    wingsOn,
  )
  const markNc = netCreditPts(
    callMarkPremium != null ? callMarkPremium : callPremium,
    putMarkPremium != null ? putMarkPremium : putPremium,
    wingCallMarkPremium != null ? wingCallMarkPremium : wingCallPremium,
    wingPutMarkPremium != null ? wingPutMarkPremium : wingPutPremium,
    wingsOn,
  )

  const maxProfitExpiry = entryNc * scale
  const breakevenUpper = callStrike + entryNc
  const breakevenLower = putStrike - entryNc

  // Prefer trade.max_loss_usd from /active — never disagree with PositionCard
  let maxLoss = null
  if (maxLossUsdProp != null && Number.isFinite(Number(maxLossUsdProp))) {
    maxLoss = Number(maxLossUsdProp)
  } else if (wingsOn) {
    const widthCall = Number(wingCallStrike) - callStrike
    const widthPut = putStrike - Number(wingPutStrike)
    const width = Math.max(widthCall, widthPut)
    if (width > 0) {
      maxLoss = width * scale - maxProfitExpiry
    }
  }

  const spot = Number(currentPrice)
  let xMin = spot * 0.8
  let xMax = spot * 1.2
  // Ensure wings + BEs visible
  if (wingsOn) {
    xMin = Math.min(xMin, Number(wingPutStrike) * 0.98, breakevenLower)
    xMax = Math.max(xMax, Number(wingCallStrike) * 1.02, breakevenUpper)
  } else {
    xMin = Math.min(xMin, breakevenLower)
    xMax = Math.max(xMax, breakevenUpper)
  }

  const peak = Math.max(
    Math.abs(maxProfitExpiry),
    maxLoss != null ? Math.abs(maxLoss) : 0,
    0.01,
  )
  const yAxisMax = peak * 1.2
  const yAxisMin = maxLoss != null ? -peak * 1.25 : -peak * 1.5

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
      entryNc,
      scale,
      wingsOn ? wingCallStrike : null,
      wingsOn ? wingPutStrike : null,
    )
    const timedPnl = calcTimedPnl(
      price,
      callStrike,
      putStrike,
      entryNc,
      markNc,
      scale,
      tau,
      wingsOn ? wingCallStrike : null,
      wingsOn ? wingPutStrike : null,
    )
    timedMax = Math.max(timedMax, timedPnl)
    timedMin = Math.min(timedMin, timedPnl)
    const clipped = Math.max(yAxisMin, Math.min(yAxisMax, timedPnl))
    points.push({
      price,
      expiryPnl,
      timedPnl,
      pnlLine: clipped,
      profitFill: expiryPnl >= 0 ? Math.min(expiryPnl, yAxisMax) : 0,
      lossFill: expiryPnl < 0 ? Math.max(expiryPnl, yAxisMin) : 0,
    })
  }

  const riskReward =
    maxLoss != null && maxLoss > 0 && maxProfitExpiry > 0
      ? maxLoss / maxProfitExpiry
      : null

  return {
    points,
    maxProfit: maxProfitExpiry,
    maxLoss,
    riskReward,
    wingsOn,
    timedMaxProfit: timedMax,
    timedMinPnl: timedMin,
    yAxisMin,
    yAxisMax,
    xMin,
    xMax,
    breakevenUpper,
    breakevenLower,
    entryNc,
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
  const digits = abs >= 10 ? 2 : 3
  return `${sign}$${abs.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  })}`
}

function fmtStrike(v) {
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
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
 * Optional wings: wingCallStrike, wingPutStrike, wingCallPremium, wingPutPremium
 * Optional marks (for "close now"): callMarkPremium, putMarkPremium, …
 * maxLossUsd — from /api/trade/active (do not recompute when set)
 */
export default function PayoffGraph({
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  quantity = 1,
  currentPrice,
  expiryDate,
  wingCallStrike = null,
  wingPutStrike = null,
  wingCallPremium = null,
  wingPutPremium = null,
  callMarkPremium = null,
  putMarkPremium = null,
  wingCallMarkPremium = null,
  wingPutMarkPremium = null,
  maxLossUsd = null,
  /** If set, slider starts here (e.g. hours_to_expiry = "now"). Default 0 = expiry. */
  initialHoursRemaining = null,
  emptyMessage = 'Select both Call and Put strikes to see payoff',
  compact = false,
}) {
  const hoursToExpiry = useMemo(
    () => hoursUntilExpiryIst(expiryDate),
    [expiryDate],
  )

  const [hoursRemaining, setHoursRemaining] = useState(null)

  useEffect(() => {
    const total = Math.max(hoursToExpiry, 0.25)
    const start =
      initialHoursRemaining != null
        ? Math.min(Math.max(Number(initialHoursRemaining), 0), total)
        : 0
    setHoursRemaining(start)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- lock slider until position identity changes
  }, [expiryDate, callStrike, putStrike, wingCallStrike, wingPutStrike])

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
      wingCallStrike:
        wingCallStrike != null ? Number(wingCallStrike) : null,
      wingPutStrike: wingPutStrike != null ? Number(wingPutStrike) : null,
      wingCallPremium:
        wingCallPremium != null ? Number(wingCallPremium) : null,
      wingPutPremium: wingPutPremium != null ? Number(wingPutPremium) : null,
      callMarkPremium:
        callMarkPremium != null ? Number(callMarkPremium) : null,
      putMarkPremium: putMarkPremium != null ? Number(putMarkPremium) : null,
      wingCallMarkPremium:
        wingCallMarkPremium != null ? Number(wingCallMarkPremium) : null,
      wingPutMarkPremium:
        wingPutMarkPremium != null ? Number(wingPutMarkPremium) : null,
      maxLossUsdProp: maxLossUsd,
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
    wingCallStrike,
    wingPutStrike,
    wingCallPremium,
    wingPutPremium,
    callMarkPremium,
    putMarkPremium,
    wingCallMarkPremium,
    wingPutMarkPremium,
    maxLossUsd,
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

  const contractNotional = qty * OPTIONS_CONTRACT_VALUE
  const rrLabel =
    chart.riskReward != null
      ? `${chart.riskReward.toFixed(1)} : 1`
      : '—'

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-900 p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-white">
          Payoff — {timeLabel}
          {chart.wingsOn ? (
            <span className="ml-2 text-xs font-normal text-sky-400">
              Iron Condor
            </span>
          ) : null}
        </h3>
        <div className="flex flex-wrap items-center gap-3 text-xs text-gray-400">
          <span>{hoursToExpiry.toFixed(1)}h to expiry</span>
          <span className="rounded bg-gray-800 px-2 py-0.5 text-orange-300">
            × {qty} lot{qty > 1 ? 's' : ''} (= {contractNotional.toFixed(3)} Delta
            size)
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
            <span>Now</span>
          </div>
        </label>
        <div className="flex flex-wrap gap-3 text-[11px] text-gray-500">
          <span className="inline-flex items-center gap-1">
            <span className="inline-block h-0.5 w-4 bg-green-500" /> Expiry
            (solid)
          </span>
          <span className="inline-flex items-center gap-1">
            <span
              className="inline-block h-0.5 w-4 border-t-2 border-dashed border-orange-500"
            />{' '}
            {timeLabel} (dashed)
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

            {/* Max profit / max loss horizontals */}
            <ReferenceLine
              y={chart.maxProfit}
              stroke="#22c55e"
              strokeDasharray="6 3"
              strokeWidth={1}
              label={{
                value: 'Max profit',
                position: 'insideTopLeft',
                fill: '#86efac',
                fontSize: 10,
              }}
            />
            {chart.maxLoss != null ? (
              <ReferenceLine
                y={-Math.abs(chart.maxLoss)}
                stroke="#ef4444"
                strokeDasharray="6 3"
                strokeWidth={1}
                label={{
                  value: 'Max loss (capped)',
                  position: 'insideBottomLeft',
                  fill: '#fca5a5',
                  fontSize: 10,
                }}
              />
            ) : null}

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

            {/* Short strikes */}
            <ReferenceLine
              x={Number(callStrike)}
              stroke="#a78bfa"
              strokeWidth={1}
              label={{
                value: 'SC',
                position: 'insideTop',
                fill: '#c4b5fd',
                fontSize: 9,
              }}
            />
            <ReferenceLine
              x={Number(putStrike)}
              stroke="#a78bfa"
              strokeWidth={1}
              label={{
                value: 'SP',
                position: 'insideTop',
                fill: '#c4b5fd',
                fontSize: 9,
              }}
            />
            {/* Wing strikes — distinct colour */}
            {chart.wingsOn ? (
              <>
                <ReferenceLine
                  x={Number(wingCallStrike)}
                  stroke="#38bdf8"
                  strokeWidth={1.5}
                  strokeDasharray="2 2"
                  label={{
                    value: 'WC',
                    position: 'insideBottom',
                    fill: '#7dd3fc',
                    fontSize: 9,
                  }}
                />
                <ReferenceLine
                  x={Number(wingPutStrike)}
                  stroke="#38bdf8"
                  strokeWidth={1.5}
                  strokeDasharray="2 2"
                  label={{
                    value: 'WP',
                    position: 'insideBottom',
                    fill: '#7dd3fc',
                    fontSize: 9,
                  }}
                />
              </>
            ) : null}

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

            {/* Expiry — always solid */}
            <Line
              type="linear"
              dataKey="expiryPnl"
              stroke="#22c55e"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
              name="On Expiry"
            />

            {/* Timed / close-now — dashed */}
            <Line
              type="monotone"
              dataKey="timedPnl"
              stroke="#f97316"
              strokeWidth={2}
              strokeDasharray="6 4"
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
          Max profit:{' '}
          <span className="font-medium text-green-400">
            {fmtMoney(chart.maxProfit)}
          </span>
          <span className="text-gray-500"> (net credit)</span>
        </div>
        <div className="text-gray-300">
          Max loss:{' '}
          {chart.maxLoss != null ? (
            <>
              <span className="font-medium text-red-400">
                {fmtMoney(Math.abs(chart.maxLoss))}
              </span>
              <span className="text-gray-500"> (capped by wings)</span>
            </>
          ) : (
            <span className="font-semibold text-red-500">Unlimited</span>
          )}
        </div>
        <div className="text-gray-300">
          Breakevens:{' '}
          <span className="font-medium text-white">
            {fmtStrike(chart.breakevenLower)} / {fmtStrike(chart.breakevenUpper)}
          </span>
        </div>
        <div className="text-gray-300">
          Risk:reward{' '}
          <span className="font-medium text-white">{rrLabel}</span>
        </div>
        <div className="text-gray-300 sm:col-span-2">
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

/** Exported for unit-style checks in browser / future Vitest */
export { calcExpiryPnl, netCreditPts }
