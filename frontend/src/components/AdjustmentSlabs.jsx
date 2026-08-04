import { useEffect, useState } from 'react'
import { isValidPct } from '../utils/pct'

const DEFAULTS = {
  mode: 'slab',
  flat_pct: 150,
  slab_24h: 200,
  slab_12h: 175,
  slab_6h: 150,
  slab_lt6h: 150,
}

function PctInput({ label, value, onChange, placeholder }) {
  const valid = isValidPct(value)
  return (
    <label className="flex items-center justify-between gap-3 text-sm text-gray-300">
      <span>{label}</span>
      <div className="flex items-center gap-1">
        <input
          type="number"
          min={1}
          max={500}
          step={1}
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(Number(e.target.value))}
          className={`w-24 rounded-md border bg-gray-900 px-2 py-1.5 text-right text-white ${
            valid ? 'border-gray-600' : 'border-red-500'
          }`}
        />
        <span className="text-gray-400">%</span>
      </div>
    </label>
  )
}

/**
 * Props: { onChange, defaultMode='slab' }
 * onChange({ mode, flat_pct, slab_24h, slab_12h, slab_6h, slab_lt6h })
 */
export default function AdjustmentSlabs({ onChange, defaultMode = 'slab' }) {
  const [mode, setMode] = useState(defaultMode === 'flat' ? 'flat' : 'slab')
  const [flatPct, setFlatPct] = useState(DEFAULTS.flat_pct)
  const [slab24h, setSlab24h] = useState(DEFAULTS.slab_24h)
  const [slab12h, setSlab12h] = useState(DEFAULTS.slab_12h)
  const [slab6h, setSlab6h] = useState(DEFAULTS.slab_6h)
  const [slabLt6h, setSlabLt6h] = useState(DEFAULTS.slab_lt6h)

  useEffect(() => {
    onChange?.({
      mode,
      flat_pct: flatPct,
      slab_24h: slab24h,
      slab_12h: slab12h,
      slab_6h: slab6h,
      slab_lt6h: slabLt6h,
    })
  }, [mode, flatPct, slab24h, slab12h, slab6h, slabLt6h, onChange])

  return (
    <div className="space-y-4 rounded-xl border border-gray-700 bg-gray-800/60 p-4">
      <h2 className="text-sm font-semibold text-white">Adjustment Trigger</h2>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => setMode('flat')}
          className={`rounded-md px-3 py-1.5 text-sm font-medium ${
            mode === 'flat'
              ? 'bg-blue-500 text-white'
              : 'bg-gray-900 text-gray-300 hover:bg-gray-700'
          }`}
        >
          Flat %
        </button>
        <button
          type="button"
          onClick={() => setMode('slab')}
          className={`rounded-md px-3 py-1.5 text-sm font-medium ${
            mode === 'slab'
              ? 'bg-blue-500 text-white'
              : 'bg-gray-900 text-gray-300 hover:bg-gray-700'
          }`}
        >
          Time-Based Slabs
        </button>
      </div>

      {mode === 'flat' ? (
        <PctInput
          label="Trigger %"
          value={flatPct}
          onChange={setFlatPct}
          placeholder="e.g. 110 (use low % for testing)"
        />
      ) : (
        <div className="space-y-2">
          <PctInput label="> 24 hours" value={slab24h} onChange={setSlab24h} />
          <PctInput label="12–24 hours" value={slab12h} onChange={setSlab12h} />
          <PctInput label="6–12 hours" value={slab6h} onChange={setSlab6h} />
          <PctInput label="< 6 hours" value={slabLt6h} onChange={setSlabLt6h} />
        </div>
      )}

      <p className="text-xs text-gray-500">Values must be between 1% and 500%.</p>
    </div>
  )
}
