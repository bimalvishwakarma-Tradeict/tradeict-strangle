import { useEffect, useState } from 'react'
import { isValidPct } from '../utils/pct'

const DEFAULTS = {
  mode: 'slab',
  flat_pct: 150,
  slab_24h: 200,
  slab_12h: 175,
  slab_6h: 150,
  slab_lt6h: 150,
  premium_slab_300: 150,
  premium_slab_200: 160,
  premium_slab_100: 180,
  premium_slab_lt100: 200,
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

function modeButtonClass(active) {
  return `rounded-md px-3 py-1.5 text-sm font-medium ${
    active
      ? 'bg-blue-500 text-white'
      : 'bg-gray-900 text-gray-300 hover:bg-gray-700'
  }`
}

/**
 * Props:
 *   onChange({ mode, flat_pct, slab_*, premium_slab_* })
 *   defaultMode = 'slab' | 'flat' | 'premium'
 *   initialValues — optional override for edit-in-place
 *   compact — tighter layout for PositionCard modal
 */
export default function AdjustmentSlabs({
  onChange,
  defaultMode = 'slab',
  initialValues = null,
  compact = false,
}) {
  const init = initialValues || {}
  const startMode =
    init.mode ||
    (defaultMode === 'flat' || defaultMode === 'premium' ? defaultMode : 'slab')

  const [mode, setMode] = useState(startMode)
  const [flatPct, setFlatPct] = useState(init.flat_pct ?? DEFAULTS.flat_pct)
  const [slab24h, setSlab24h] = useState(init.slab_24h ?? DEFAULTS.slab_24h)
  const [slab12h, setSlab12h] = useState(init.slab_12h ?? DEFAULTS.slab_12h)
  const [slab6h, setSlab6h] = useState(init.slab_6h ?? DEFAULTS.slab_6h)
  const [slabLt6h, setSlabLt6h] = useState(init.slab_lt6h ?? DEFAULTS.slab_lt6h)
  const [prem300, setPrem300] = useState(
    init.premium_slab_300 ?? DEFAULTS.premium_slab_300,
  )
  const [prem200, setPrem200] = useState(
    init.premium_slab_200 ?? DEFAULTS.premium_slab_200,
  )
  const [prem100, setPrem100] = useState(
    init.premium_slab_100 ?? DEFAULTS.premium_slab_100,
  )
  const [premLt100, setPremLt100] = useState(
    init.premium_slab_lt100 ?? DEFAULTS.premium_slab_lt100,
  )

  useEffect(() => {
    onChange?.({
      mode,
      flat_pct: flatPct,
      slab_24h: slab24h,
      slab_12h: slab12h,
      slab_6h: slab6h,
      slab_lt6h: slabLt6h,
      premium_slab_300: prem300,
      premium_slab_200: prem200,
      premium_slab_100: prem100,
      premium_slab_lt100: premLt100,
    })
  }, [
    mode,
    flatPct,
    slab24h,
    slab12h,
    slab6h,
    slabLt6h,
    prem300,
    prem200,
    prem100,
    premLt100,
    onChange,
  ])

  return (
    <div
      className={
        compact
          ? 'space-y-3'
          : 'space-y-4 rounded-xl border border-gray-700 bg-gray-800/60 p-4'
      }
    >
      {!compact && (
        <h2 className="text-sm font-semibold text-white">Adjustment Trigger</h2>
      )}

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => setMode('flat')}
          className={modeButtonClass(mode === 'flat')}
        >
          Flat %
        </button>
        <button
          type="button"
          onClick={() => setMode('slab')}
          className={modeButtonClass(mode === 'slab')}
        >
          Time-Based Slabs
        </button>
        <button
          type="button"
          onClick={() => setMode('premium')}
          className={modeButtonClass(mode === 'premium')}
        >
          Premium-Based Slabs
        </button>
      </div>

      {mode === 'flat' && (
        <PctInput
          label="Trigger %"
          value={flatPct}
          onChange={setFlatPct}
          placeholder="e.g. 110 (use low % for testing)"
        />
      )}

      {mode === 'slab' && (
        <div className="space-y-2">
          <PctInput label="> 24 hours" value={slab24h} onChange={setSlab24h} />
          <PctInput label="12–24 hours" value={slab12h} onChange={setSlab12h} />
          <PctInput label="6–12 hours" value={slab6h} onChange={setSlab6h} />
          <PctInput label="< 6 hours" value={slabLt6h} onChange={setSlabLt6h} />
        </div>
      )}

      {mode === 'premium' && (
        <div className="space-y-2">
          <PctInput
            label="Premium ≥ $300"
            value={prem300}
            onChange={setPrem300}
          />
          <PctInput
            label="$200 – $300"
            value={prem200}
            onChange={setPrem200}
          />
          <PctInput
            label="$100 – $200"
            value={prem100}
            onChange={setPrem100}
          />
          <PctInput label="< $100" value={premLt100} onChange={setPremLt100} />
          <p className="text-xs text-gray-500">
            Higher premium = tighter trigger. Each leg calculated independently.
          </p>
        </div>
      )}

      <p className="text-xs text-gray-500">Values must be between 1% and 500%.</p>
    </div>
  )
}
