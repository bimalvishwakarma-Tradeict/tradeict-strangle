function FunnelRow({ label, value, hint }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-gray-900 py-1.5 text-sm">
      <div>
        <span className="text-gray-200">{label}</span>
        {hint ? (
          <span className="ml-2 text-xs text-gray-500">{hint}</span>
        ) : null}
      </div>
      <span className="font-mono text-gray-100">{value}</span>
    </div>
  )
}

function ScoreHist({ hist, minScore }) {
  const entries = [0, 1, 2, 3, 4, 5].map((k) => ({
    score: k,
    count: Number(hist?.[String(k)] ?? hist?.[k] ?? 0),
  }))
  const max = Math.max(1, ...entries.map((e) => e.count))
  return (
    <div className="space-y-1 py-2">
      <div className="text-xs uppercase tracking-wide text-gray-500">
        Score histogram (sweep candles only)
      </div>
      {entries.map((e) => {
        const pct = (e.count / max) * 100
        const below = e.score < Number(minScore)
        return (
          <div key={e.score} className="flex items-center gap-2 text-xs">
            <span className="w-10 text-gray-400">s{e.score}</span>
            <div className="h-2 flex-1 overflow-hidden rounded bg-gray-900">
              <div
                className={`h-full ${below ? 'bg-gray-600' : 'bg-blue-500'}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="w-10 text-right font-mono text-gray-300">
              {e.count}
            </span>
            {e.score === Number(minScore) ? (
              <span className="text-[10px] text-amber-400">min</span>
            ) : (
              <span className="w-6" />
            )}
          </div>
        )
      })}
    </div>
  )
}

export default function S003Diagnostics({
  diagnostics,
  warmFromIndex,
  candleCount,
  minScore = 3,
  open,
  onToggle,
}) {
  const d = diagnostics || {}
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-950">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-medium text-gray-200 hover:bg-gray-900"
      >
        <span>Diagnostics funnel</span>
        <span className="text-gray-500">{open ? '▾' : '▸'}</span>
      </button>
      {open ? (
        <div className="space-y-1 border-t border-gray-800 px-4 pb-4 pt-2">
          <FunnelRow label="Candles" value={candleCount ?? '—'} />
          <FunnelRow
            label="Warm from index"
            value={warmFromIndex == null ? 'null' : warmFromIndex}
            hint="bars before this are incomplete VWAP"
          />
          <FunnelRow
            label="Sweeps up / dn"
            value={`${d.sweep_up_count ?? 0} / ${d.sweep_dn_count ?? 0}`}
          />
          <ScoreHist hist={d.score_hist} minScore={minScore} />
          <FunnelRow
            label="Arms top / bottom"
            value={`${d.arms_top ?? 0} / ${d.arms_bottom ?? 0}`}
          />
          <FunnelRow
            label="Confirms / invalidates / expires"
            value={`${d.confirms ?? 0} / ${d.invalidates ?? 0} / ${d.expires ?? 0}`}
          />
          <FunnelRow
            label="Cooldown blocks"
            value={d.cooldown_blocks ?? 0}
          />
          <FunnelRow
            label="Conflicts / gaps"
            value={`${d.conflicts ?? 0} / ${d.gaps ?? 0}`}
          />
          <FunnelRow label="Emitted" value={d.emitted ?? 0} />
          <FunnelRow
            label="ADX below / at-or-above trend"
            value={`${d.adx_below_trend ?? 0} / ${d.adx_at_or_above_trend ?? 0}`}
          />
        </div>
      ) : null}
    </div>
  )
}
