function fmtSigned(v, digits = 4) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

function fmtDeduction(v, digits = 4) {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return '−$0.0000'
  return `−$${Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

function pnlClass(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return 'text-gray-400'
  if (n > 0) return 'text-green-400'
  if (n < 0) return 'text-red-400'
  return 'text-gray-300'
}

function barColor(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return 'bg-gray-500'
  if (n > 0) return 'bg-green-500'
  if (n < 0) return 'bg-red-500'
  return 'bg-gray-500'
}

/**
 * Gross-to-net P&L waterfall with proportional bar widths.
 * deductions: [{ label, amount, title? }]
 */
export default function PnlSlider({
  grossLabel = 'Gross',
  gross,
  net,
  netLabel = 'Net MTM',
  deductions = [],
  targetPct,
  targetUsd,
  slPct,
  slUsd,
  className = '',
}) {
  const grossNum = Number(gross)
  const netNum = Number(net)
  const grossAbs = Number.isFinite(grossNum) ? Math.abs(grossNum) : 0
  const netAbs = Number.isFinite(netNum) ? Math.abs(netNum) : 0
  const netPctOfGross =
    grossAbs > 0 ? Math.min((netAbs / grossAbs) * 100, 100) : 0

  const targetProgress =
    targetPct != null && Number.isFinite(Number(targetPct))
      ? Math.min(100, Math.abs(Number(targetPct)))
      : null
  const slProgress =
    slPct != null && Number.isFinite(Number(slPct))
      ? Math.min(100, Math.max(0, Number(slPct)))
      : null

  return (
    <div
      className={`rounded-lg border border-gray-700 bg-gray-900/50 px-3 py-3 ${className}`}
    >
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          {grossLabel}
        </span>
        <span className={`text-sm font-bold ${pnlClass(gross)}`}>
          {fmtSigned(gross, 4)}
        </span>
      </div>

      <div className="mb-1 h-2 w-full overflow-hidden rounded-full bg-gray-800">
        <div
          className={`h-full rounded-full transition-all ${barColor(gross)}`}
          style={{ width: '100%' }}
        />
      </div>

      {deductions.map(({ label, amount, title }) => (
        <div key={label} className="mt-2">
          <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
            <span className="text-gray-500" title={title}>
              {label}
            </span>
            <span className="text-yellow-400/90">{fmtDeduction(amount, 4)}</span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-800">
            <div
              className="h-full rounded-full bg-yellow-600/70 transition-all"
              style={{
                width:
                  grossAbs > 0
                    ? `${Math.min(100, (Math.abs(Number(amount) || 0) / grossAbs) * 100)}%`
                    : '0%',
              }}
            />
          </div>
        </div>
      ))}

      <div className="my-2 border-t border-gray-700" />

      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-300">
          {netLabel}
        </span>
        <span className={`text-base font-bold ${pnlClass(net)}`}>
          {fmtSigned(net, 4)}
        </span>
      </div>
      <div className="mt-1.5 h-2.5 w-full overflow-hidden rounded-full bg-gray-800">
        <div
          className={`h-full rounded-full transition-all ${barColor(net)}`}
          style={{ width: `${netPctOfGross}%` }}
        />
      </div>

      {(targetProgress != null || slProgress != null) && (
        <div className="mt-3 space-y-2 border-t border-gray-800 pt-2">
          {targetProgress != null && (
            <div>
              <div className="mb-1 flex justify-between text-[11px] text-gray-400">
                <span>
                  {Number(targetPct) >= 0 ? '' : '−'}
                  {targetProgress.toFixed(1)}% of target
                  {targetUsd != null ? ` (+$${Number(targetUsd).toFixed(2)})` : ''}
                </span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-800">
                <div
                  className="h-full rounded-full bg-green-500/80"
                  style={{ width: `${targetProgress}%` }}
                />
              </div>
            </div>
          )}
          {slProgress != null && (
            <div>
              <div className="mb-1 flex justify-between text-[11px] text-gray-400">
                <span>
                  SL: {slUsd != null ? `−$${Number(slUsd).toFixed(2)}` : ''} ·{' '}
                  {slProgress.toFixed(1)}% consumed
                </span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-800">
                <div
                  className="h-full rounded-full bg-red-500/80"
                  style={{ width: `${slProgress}%` }}
                />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
