const CONTRACT_VALUE = 0.001
const SL_MSG = 'SL % 120 se kam nahi ho sakta. 200 = premium double hone par stop.'

function fmtMoney(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function fmtStrike(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 })
}

export function universalSlPctError(pct) {
  const n = Number(pct)
  if (!Number.isFinite(n) || n < 120 || n > 1000) {
    return SL_MSG
  }
  return null
}

export default function DeltaStopLossSection({
  universalSlPct,
  onChange,
  callStrike,
  putStrike,
  callPremium,
  putPremium,
  quantity,
  currentCallMark,
  currentPutMark,
}) {
  const pct = Number(universalSlPct)
  const qty = Math.abs(Number(quantity) || 1)
  const callPrem = Number(callPremium) || 0
  const putPrem = Number(putPremium) || 0
  const callStop =
    callPrem > 0 && Number.isFinite(pct) ? (callPrem * pct) / 100 : 0
  const putStop = putPrem > 0 && Number.isFinite(pct) ? (putPrem * pct) / 100 : 0
  const dualLoss =
    (Math.max(0, callStop - callPrem) + Math.max(0, putStop - putPrem)) *
    qty *
    CONTRACT_VALUE
  const callMark = Number(currentCallMark ?? callPrem) || 0
  const putMark = Number(currentPutMark ?? putPrem) || 0
  const callClose = callMark > 0 && callStop > 0 && callStop < callMark * 1.1
  const putClose = putMark > 0 && putStop > 0 && putStop < putMark * 1.1
  const err = universalSlPctError(pct)

  return (
    <div className="rounded-lg border border-amber-800/50 bg-amber-950/20 px-3 py-3">
      <div className="mb-2 text-sm font-semibold text-amber-200">Delta Stop-Loss</div>
      <label className="block text-sm text-gray-300">
        Delta SL — premium kitne % tak jaane par stop (200 = double)
        <input
          type="number"
          min={120}
          max={1000}
          step={1}
          value={universalSlPct}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 w-full max-w-xs rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
          placeholder="200"
        />
      </label>
      {err ? <p className="mt-2 text-xs text-red-400">{err}</p> : null}
      <div className="mt-2 space-y-1 text-xs text-gray-400">
        <div>
          Call {fmtStrike(callStrike)}: ~${fmtMoney(callPrem)} par bikega → Delta stop $
          {fmtMoney(callStop)}
        </div>
        <div>
          Put {fmtStrike(putStrike)}: ~${fmtMoney(putPrem)} par bikega → Delta stop $
          {fmtMoney(putStop)}
        </div>
        <div>Dono stop hit → loss ≈ ${fmtMoney(dualLoss)}</div>
      </div>
      {(callClose || putClose) && (
        <p className="mt-2 text-xs text-yellow-300">Stop bahut paas hai</p>
      )}
    </div>
  )
}
