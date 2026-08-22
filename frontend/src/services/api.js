import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000',
})

function extractError(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join(', ')
  }
  if (detail && typeof detail === 'object') {
    return JSON.stringify(detail)
  }
  return err?.message || fallback
}

export const connectAccount = async (data) => {
  try {
    const res = await api.post('/api/account/connect', data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Connection failed'))
  }
}

export const getAccountStatus = async () => {
  try {
    const res = await api.get('/api/account/status')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch account status'))
  }
}

export const updateAccountSettings = async (data) => {
  try {
    const res = await api.patch('/api/account/settings', data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to update account settings'))
  }
}

export const disconnectAccount = async () => {
  try {
    const res = await api.delete('/api/account/disconnect')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Disconnect failed'))
  }
}

export const getExpiries = async (underlying, options = {}) => {
  try {
    const params = { underlying }
    if (options.limit != null) params.limit = options.limit
    const res = await api.get('/api/strategy/expiries', { params })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch expiries'))
  }
}

export const getOptionChain = async (underlying, expiry) => {
  try {
    const res = await api.get(
      `/api/strategy/option-chain?underlying=${underlying}&expiry=${expiry}`,
    )
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch option chain'))
  }
}

export const getPayoff = async (params) => {
  try {
    const res = await api.get('/api/strategy/payoff', { params })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to calculate payoff'))
  }
}

const PREVIEW_UNAVAILABLE = (detail) => ({
  success: false,
  unavailable: true,
  message: 'unavailable - chain fetch failed',
  detail,
})

/** Strip undefined / empty so FastAPI Query defaults fall through to saved settings. */
const cleanPreviewParams = (params = {}) => {
  const out = {}
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    out[key] = value
  })
  return out
}

export const getHedgePreview = async (params = {}) => {
  try {
    const res = await api.get('/api/strategy/hedge-preview', {
      params: cleanPreviewParams(params),
    })
    return res.data
  } catch (err) {
    return PREVIEW_UNAVAILABLE(
      extractError(err, 'Failed to fetch hedge preview'),
    )
  }
}

export const getThetaPreview = async (params = {}) => {
  try {
    const res = await api.get('/api/strategy/theta-preview', {
      params: cleanPreviewParams(params),
    })
    return res.data
  } catch (err) {
    return PREVIEW_UNAVAILABLE(
      extractError(err, 'Failed to fetch theta preview'),
    )
  }
}

export const getTargetPreview = async (params = {}) => {
  try {
    const res = await api.get('/api/strategy/target-preview', {
      params: cleanPreviewParams(params),
    })
    return res.data
  } catch (err) {
    return PREVIEW_UNAVAILABLE(
      extractError(err, 'Failed to fetch target preview'),
    )
  }
}

export const initiateTrade = async (data) => {
  try {
    const res = await api.post('/api/trade/initiate', data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to place strangle on Delta'))
  }
}

export const registerExistingTrade = async (data) => {
  try {
    const res = await api.post('/api/trade/register-existing', data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to register existing trade'))
  }
}

export const getActiveTrades = async () => {
  try {
    const res = await api.get('/api/trade/active')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch active trades'))
  }
}

export const getActiveHedge = async () => {
  try {
    const res = await api.get('/api/hedge/active')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch active hedge'))
  }
}

export const closeHedge = async (hedgeId, reason = 'HEDGE_MANUAL') => {
  try {
    const res = await api.post(`/api/hedge/${hedgeId}/close`, { reason })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to close hedge'))
  }
}

export const updateHedgeSettings = async (hedgeId, data) => {
  try {
    const res = await api.patch(`/api/hedge/${hedgeId}/settings`, data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to update hedge settings'))
  }
}

export const getTrade = async (id) => {
  try {
    const res = await api.get(`/api/trade/${id}`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch trade'))
  }
}

export const exitTrade = async (id) => {
  try {
    const res = await api.post(`/api/trade/${id}/exit`, {
      reason: 'MANUAL_EMERGENCY',
    })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Emergency exit failed'))
  }
}

export const closeLeg = async (id, legType) => {
  try {
    const res = await api.post(`/api/trade/${id}/leg/${legType}/close`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, `Failed to close ${legType} leg`))
  }
}

export const updateSettings = async (id, data) => {
  try {
    const res = await api.patch(`/api/trade/${id}/settings`, data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to update settings'))
  }
}

export const getAdjustments = async (id) => {
  try {
    const res = await api.get(`/api/trade/${id}/adjustments`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch adjustments'))
  }
}

export const getTradeHistory = async (limit = 30) => {
  try {
    const res = await api.get('/api/trade/history', { params: { limit } })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch trade history'))
  }
}

export const getHedgeStructures = async (limit = 40) => {
  try {
    const res = await api.get('/api/hedge/structures', { params: { limit } })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch structure history'))
  }
}

/** Structure ledger — identifiers + leg windows only (no P&L). */
export const getStructureLedger = async (params = {}) => {
  try {
    const res = await api.get('/api/structures', { params })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch structure ledger'))
  }
}

export const getStructureLedgerChanges = async (since, params = {}) => {
  try {
    const res = await api.get('/api/structures/changes', {
      params: { since, ...params },
    })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch structure ledger changes'))
  }
}

export const getBotLogs = async ({ trade_id, limit = 100, level = 'all' } = {}) => {
  try {
    const params = { limit, level }
    if (trade_id != null) params.trade_id = trade_id
    const res = await api.get('/api/logs', { params })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch bot logs'))
  }
}

export const downloadLogFile = async (date) => {
  try {
    const params = date ? { date } : {}
    const res = await api.get('/api/logs/file', {
      params,
      responseType: 'blob',
    })
    const blob = new Blob([res.data], { type: 'text/plain;charset=utf-8' })
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    const stamp =
      date || new Date().toISOString().slice(0, 10)
    a.href = url
    a.download = `bot_activity_${stamp}.log`
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.URL.revokeObjectURL(url)
  } catch (err) {
    throw new Error(extractError(err, 'Failed to download log file'))
  }
}

export const checkHealth = async () => {
  try {
    const res = await api.get('/health', { timeout: 4000 })
    return res.data?.status === 'ok'
  } catch {
    try {
      const res = await api.get('/', { timeout: 4000 })
      return res.data?.status === 'ok'
    } catch {
      return false
    }
  }
}

export const getAutoTradeSettings = async () => {
  try {
    const res = await api.get('/api/auto-trade/settings')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch auto trade settings'))
  }
}

export const saveAutoTradeSettings = async (data) => {
  try {
    const res = await api.post('/api/auto-trade/settings', data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to save auto trade settings'))
  }
}

export const enableAutoTrade = async () => {
  try {
    const res = await api.post('/api/auto-trade/enable')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to enable auto trade'))
  }
}

export const disableAutoTrade = async () => {
  try {
    const res = await api.post('/api/auto-trade/disable')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to disable auto trade'))
  }
}

export const getAutoTradeStatus = async () => {
  try {
    const res = await api.get('/api/auto-trade/status')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch auto trade status'))
  }
}

export const getSlaveAccounts = async () => {
  try {
    const res = await api.get('/api/slave/accounts')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch slave accounts'))
  }
}

export const addSlaveAccount = async (data) => {
  try {
    const res = await api.post('/api/slave/accounts', data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to add slave account'))
  }
}

export const updateSlaveAccount = async (id, data) => {
  try {
    const res = await api.patch(`/api/slave/accounts/${id}`, data)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to update slave account'))
  }
}

export const deleteSlaveAccount = async (id) => {
  try {
    const res = await api.delete(`/api/slave/accounts/${id}`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to delete slave account'))
  }
}

export const testSlaveConnection = async (id) => {
  try {
    const res = await api.post(`/api/slave/accounts/${id}/test`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Slave connection test failed'))
  }
}

export const toggleSlaveAccount = async (id) => {
  try {
    const res = await api.post(`/api/slave/accounts/${id}/toggle`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to toggle slave account'))
  }
}

export const copyMasterTradeToSlave = async (slaveId) => {
  try {
    const res = await api.post(`/api/slave/accounts/${slaveId}/copy-master-trade`)
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to copy master trade to slave'))
  }
}

export const getSlaveOverview = async () => {
  try {
    const res = await api.get('/api/slave/overview')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch slave overview'))
  }
}

export const closeSlaveStructure = async (slaveId, reason = 'ADMIN_FORCE') => {
  try {
    const res = await api.post(`/api/slave/${slaveId}/close-structure`, {
      reason,
    })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to force-close slave structure'))
  }
}

export const runBacktest = async (payload) => {
  try {
    const res = await api.post('/api/backtest/run', payload, {
      timeout: 600000, // 10 min — large CSVs + multi-day sims
    })
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Backtest failed'))
  }
}

/**
 * Stream local-disk backtest progress (NDJSON).
 * onEvent({type, ...}) called for each line; returns final {summary, days}.
 */
export const runBacktestLocal = async (payload, onEvent) => {
  const base = import.meta.env.VITE_API_URL || 'http://localhost:8000'
  const res = await fetch(`${base}/api/backtest/run-local`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!res.ok) {
    let detail = `Backtest failed (${res.status})`
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      // ignore parse errors
    }
    throw new Error(detail)
  }
  if (!res.body) {
    throw new Error('No response body from backtest server')
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalResult = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) {
      const trimmed = line.trim()
      if (!trimmed) continue
      let msg
      try {
        msg = JSON.parse(trimmed)
      } catch {
        continue
      }
      if (typeof onEvent === 'function') onEvent(msg)
      if (msg.type === 'complete') {
        finalResult = { summary: msg.summary, days: msg.days }
      } else if (msg.type === 'error') {
        throw new Error(msg.message || 'Backtest failed')
      }
    }
  }

  if (buffer.trim()) {
    try {
      const msg = JSON.parse(buffer.trim())
      if (typeof onEvent === 'function') onEvent(msg)
      if (msg.type === 'complete') {
        finalResult = { summary: msg.summary, days: msg.days }
      } else if (msg.type === 'error') {
        throw new Error(msg.message || 'Backtest failed')
      }
    } catch (err) {
      if (err instanceof SyntaxError) {
        // ignore incomplete trailing junk
      } else {
        throw err
      }
    }
  }

  if (!finalResult) {
    throw new Error('Backtest finished without results')
  }
  return finalResult
}

export const getBacktestStatus = async () => {
  try {
    const res = await api.get('/api/backtest/status')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch backtest status'))
  }
}
