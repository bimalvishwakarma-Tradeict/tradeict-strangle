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

export const getExpiries = async (underlying) => {
  try {
    const res = await api.get(`/api/strategy/expiries?underlying=${underlying}`)
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

export const getSlaveOverview = async () => {
  try {
    const res = await api.get('/api/slave/overview')
    return res.data
  } catch (err) {
    throw new Error(extractError(err, 'Failed to fetch slave overview'))
  }
}
