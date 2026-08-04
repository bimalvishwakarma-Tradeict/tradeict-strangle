export function isValidPct(value) {
  const n = Number(value)
  return Number.isFinite(n) && n >= 1 && n <= 500
}
