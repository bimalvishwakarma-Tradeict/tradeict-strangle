/** Human labels for AutoTradeSettings.next_entry_source */
const NEXT_ENTRY_SOURCE_LABELS = {
  reentry_delay: 're-entry delay',
  cooldown_after_loss: 'cooldown after loss',
  retry: 'retry backoff',
  hedge_gate: 'hedge gate backoff',
  expiry_too_close: 'expiry too close',
}

/**
 * Format countdown + reason, e.g. "Next entry in 45s (re-entry delay)".
 * @param {number|null|undefined} seconds
 * @param {string|null|undefined} source
 * @param {string|null|undefined} reasonLabel  optional pre-resolved label from API
 * @returns {string}
 */
export function formatNextEntryWait(seconds, source, reasonLabel) {
  const secs = Math.max(0, Math.floor(Number(seconds) || 0))
  let timeStr
  if (secs >= 3600) {
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    timeStr = m > 0 ? `${h}h ${m}m` : `${h}h`
  } else if (secs >= 60) {
    timeStr = `${Math.ceil(secs / 60)}m`
  } else {
    timeStr = `${secs}s`
  }
  const label =
    reasonLabel ||
    NEXT_ENTRY_SOURCE_LABELS[String(source || '')] ||
    (source ? String(source).replace(/_/g, ' ') : null)
  if (label) {
    return `Next entry in ${timeStr} (${label})`
  }
  return `Next entry in ${timeStr}`
}
