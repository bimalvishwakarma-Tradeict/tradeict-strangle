function formatIst(unix) {
  return new Date(Number(unix) * 1000).toLocaleString('en-GB', {
    timeZone: 'Asia/Kolkata',
    hourCycle: 'h23',
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatIstRange(armUnix, confirmUnix) {
  return `${formatIst(armUnix)} → ${formatIst(confirmUnix)}`
}

function fmt2(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(2)
}

export default function S003SignalTable({
  signals = [],
  selectedIndex,
  onSelect,
}) {
  if (!signals.length) {
    return (
      <div className="rounded-lg border border-gray-800 bg-gray-950 px-4 py-6 text-sm text-gray-500">
        No signals in this window.
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-gray-800 bg-gray-950">
      <table className="min-w-full text-left text-sm">
        <thead className="border-b border-gray-800 bg-gray-900 text-xs uppercase tracking-wide text-gray-400">
          <tr>
            <th className="px-3 py-2">#</th>
            <th className="px-3 py-2">IST (arm → confirm)</th>
            <th className="px-3 py-2">Direction</th>
            <th className="px-3 py-2">Mode</th>
            <th className="px-3 py-2">Score</th>
            <th className="px-3 py-2">Confirm</th>
            <th className="px-3 py-2">Extreme</th>
            <th className="px-3 py-2">Distance</th>
            <th className="px-3 py-2">ATR arm</th>
            <th className="px-3 py-2">ADX</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s, i) => {
            const long = String(s.direction).toUpperCase() === 'LONG'
            const selected = selectedIndex === i
            const distance = Math.abs(
              Number(s.confirm_price) - Number(s.signal_extreme),
            )
            return (
              <tr
                key={`${s.time}-${s.arm_time}-${i}`}
                onClick={() => onSelect?.(i, s)}
                className={`cursor-pointer border-b border-gray-900 transition-colors ${
                  selected
                    ? 'bg-blue-950/60'
                    : long
                      ? 'hover:bg-emerald-950/40'
                      : 'hover:bg-rose-950/40'
                }`}
              >
                <td className="px-3 py-2 text-gray-400">{i + 1}</td>
                <td className="px-3 py-2 whitespace-nowrap text-gray-200">
                  {formatIstRange(s.arm_time, s.time)}
                </td>
                <td
                  className={`px-3 py-2 font-medium ${
                    long ? 'text-emerald-400' : 'text-rose-400'
                  }`}
                >
                  {s.direction}
                </td>
                <td className="px-3 py-2 text-gray-300">{s.mode}</td>
                <td className="px-3 py-2 text-gray-200">{s.score}</td>
                <td className="px-3 py-2 text-gray-200">
                  {Number(s.confirm_price).toFixed(2)}
                </td>
                <td className="px-3 py-2 text-gray-200">
                  {Number(s.signal_extreme).toFixed(2)}
                </td>
                <td className="px-3 py-2 text-amber-300">
                  {distance.toFixed(1)}
                </td>
                <td className="px-3 py-2 text-gray-400">{fmt2(s.atr_at_arm)}</td>
                <td className="px-3 py-2 text-gray-400">
                  {fmt2(s.adx_at_signal)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
