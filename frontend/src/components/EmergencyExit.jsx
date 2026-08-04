import { useState } from 'react'
import LoadingSpinner from './ui/LoadingSpinner'
import { exitTrade } from '../services/api'

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function fmtSigned(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const sign = n > 0 ? '+' : ''
  return `${sign}$${fmtMoney(Math.abs(n))}`
}

/**
 * Props: { tradeId, onSuccess, finalMtmHint, disabled }
 */
export default function EmergencyExit({
  tradeId,
  onSuccess,
  finalMtmHint = 0,
  disabled = false,
}) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(null)
  const [error, setError] = useState('')

  const handleExit = async () => {
    if (loading) return
    setLoading(true)
    setError('')
    try {
      const result = await exitTrade(tradeId)
      const summary = {
        callClosedAt: result?.call_closed_at,
        putClosedAt: result?.put_closed_at,
        finalPnl: result?.final_pnl ?? finalMtmHint,
      }
      setDone(summary)
      onSuccess?.(summary)
    } catch (err) {
      setError(err.message || 'Emergency exit failed')
    } finally {
      setLoading(false)
    }
  }

  const closeModal = () => {
    if (loading) return
    setOpen(false)
    setError('')
    if (done) setDone(null)
  }

  return (
    <>
      <button
        type="button"
        disabled={disabled || loading}
        onClick={() => {
          setDone(null)
          setError('')
          setOpen(true)
        }}
        className="rounded-md bg-red-600 px-3 py-2 text-sm font-semibold text-white hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50"
      >
        🔴 EMERGENCY EXIT
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4">
          <div className="w-full max-w-md rounded-xl border border-red-700/60 bg-gray-900 p-5 shadow-2xl">
            {done ? (
              <div className="space-y-3 text-sm text-gray-200">
                <h3 className="text-lg font-semibold text-green-400">✅ Trade Closed</h3>
                <p>
                  Call closed @ $
                  {done.callClosedAt != null ? fmtMoney(done.callClosedAt) : '—'}
                </p>
                <p>
                  Put closed @ $
                  {done.putClosedAt != null ? fmtMoney(done.putClosedAt) : '—'}
                </p>
                <p>
                  Final MTM P&L:{' '}
                  <span
                    className={
                      Number(done.finalPnl) >= 0 ? 'text-green-400' : 'text-red-400'
                    }
                  >
                    {fmtSigned(done.finalPnl)}
                  </span>
                </p>
                <div className="flex justify-end pt-2">
                  <button
                    type="button"
                    onClick={closeModal}
                    className="rounded-md bg-gray-700 px-3 py-1.5 text-sm text-white hover:bg-gray-600"
                  >
                    Close
                  </button>
                </div>
              </div>
            ) : (
              <>
                <h3 className="text-lg font-semibold text-red-400">⚠️ EMERGENCY EXIT</h3>
                <p className="mt-3 text-sm text-gray-300">
                  This will IMMEDIATELY close ALL open legs at market price.
                </p>
                <p className="mt-2 text-sm font-medium text-gray-200">
                  This action cannot be undone.
                </p>
                {error && (
                  <p className="mt-3 rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                    {error}
                  </p>
                )}
                <div className="mt-5 flex justify-end gap-2">
                  <button
                    type="button"
                    disabled={loading}
                    onClick={closeModal}
                    className="rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800 disabled:opacity-50"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    disabled={loading}
                    onClick={handleExit}
                    className="inline-flex items-center gap-2 rounded-md bg-red-600 px-3 py-1.5 text-sm font-bold text-white hover:bg-red-500 disabled:opacity-50"
                  >
                    {loading && <LoadingSpinner size="sm" color="white" />}
                    🔴 YES, EXIT NOW
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  )
}
