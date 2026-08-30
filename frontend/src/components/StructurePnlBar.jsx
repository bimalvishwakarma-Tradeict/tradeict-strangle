function fmtUsd(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}$${Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 4,
    maximumFractionDigits: 4,
  })}`
}

function pnlClass(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 'text-gray-400'
  if (n > 0) return 'text-green-400'
  if (n < 0) return 'text-red-400'
  return 'text-gray-300'
}

const COLUMNS = [
  { key: 'hedgeNetPnl', label: 'Hedge P&L (Net)', emphasize: false },
  { key: 'closedBasketPnl', label: 'Closed Basket (Net)', emphasize: false },
  { key: 'openBasketNetPnl', label: 'Open Basket (Net)', emphasize: false },
  { key: 'structurePnl', label: 'Structure P&L (net)', emphasize: true },
]

/**
 * 4-column structure P&L summary bar — sits above the hedge card.
 */
export default function StructurePnlBar({
  hedgeNetPnl,
  closedBasketPnl,
  openBasketNetPnl,
  structurePnl,
}) {
  const values = {
    hedgeNetPnl,
    closedBasketPnl,
    openBasketNetPnl,
    structurePnl,
  }

  return (
    <div
      className="mb-2 grid grid-cols-4 gap-0 rounded-lg border border-[#1e2a3a] bg-[#0d1117]"
      role="region"
      aria-label="Structure P and L summary"
    >
      {COLUMNS.map(({ key, label, emphasize }) => {
        const value = values[key]
        return (
          <div
            key={key}
            className="flex flex-col items-center border-r border-gray-800 px-3 py-4 last:border-r-0"
          >
            <span
              className={`mb-2 text-center font-bold text-white ${
                emphasize ? 'text-base' : 'text-sm'
              }`}
            >
              {label}
            </span>
            <span
              className={`font-bold ${pnlClass(value)} ${
                emphasize ? 'text-2xl' : 'text-xl'
              }`}
            >
              {fmtUsd(value)}
            </span>
          </div>
        )
      })}
    </div>
  )
}
