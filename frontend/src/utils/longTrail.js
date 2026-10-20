/**
 * Long-trade trailing SL: -sl + amt × floor(peak / step).
 * No trail (step/amt missing or ≤0) → -sl.
 */
export function longTrailSlLevel({ slUsd, peak, step, amt }) {
  const sl = Math.abs(Number(slUsd) || 0)
  const base = -sl
  const st = Number(step)
  const am = Number(amt)
  const pk = Math.max(0, Number(peak) || 0)
  if (!(st > 0 && am > 0)) return base
  const n = Math.floor(pk / st)
  return base + am * n
}
