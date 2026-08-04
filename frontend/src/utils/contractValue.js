/**
 * Delta Exchange India BTC/ETH options: each "lot" size is a micro contract.
 * Real USD PnL = premium_points × lots × OPTIONS_CONTRACT_VALUE
 * Must match backend.config.OPTIONS_CONTRACT_VALUE (default 0.001).
 */
export const OPTIONS_CONTRACT_VALUE = Number(
  import.meta.env.VITE_OPTIONS_CONTRACT_VALUE ?? 0.001,
)

/** Convert mark/premium points × lots into real Delta USD. */
export function toUsdPnl(premiumPoints, quantity = 1) {
  const pts = Number(premiumPoints) || 0
  const qty = Math.max(0, Number(quantity) || 0)
  const cv =
    Number.isFinite(OPTIONS_CONTRACT_VALUE) && OPTIONS_CONTRACT_VALUE > 0
      ? OPTIONS_CONTRACT_VALUE
      : 0.001
  return pts * qty * cv
}

/** Scale factor for payoff curves: lots × contract value. */
export function positionScale(quantity = 1) {
  const qty = Math.max(1, Number(quantity) || 1)
  const cv =
    Number.isFinite(OPTIONS_CONTRACT_VALUE) && OPTIONS_CONTRACT_VALUE > 0
      ? OPTIONS_CONTRACT_VALUE
      : 0.001
  return qty * cv
}
