import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  connectAccount,
  disconnectAccount,
  getAccountStatus,
  updateAccountSettings,
} from '../services/api'
import LoadingSpinner from '../components/ui/LoadingSpinner'
import Toast from '../components/ui/Toast'
import ConfirmDialog from '../components/ui/ConfirmDialog'

function formatBalance(value) {
  const n = Number(value || 0)
  return n.toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function formatLastChecked(iso) {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return (
      d.toLocaleTimeString('en-IN', {
        hour: 'numeric',
        minute: '2-digit',
        hour12: true,
        timeZone: 'Asia/Kolkata',
      }) + ' IST'
    )
  } catch {
    return iso
  }
}

function formatBalanceInr(value) {
  const n = Number(value || 0)
  return n.toLocaleString('en-IN', { maximumFractionDigits: 0 })
}

function notifyAccountUpdated() {
  window.dispatchEvent(new Event('tradeict-account-updated'))
}

export default function Settings() {
  const [loadingStatus, setLoadingStatus] = useState(true)
  const [connected, setConnected] = useState(false)
  const [accountName, setAccountName] = useState('')
  const [balance, setBalance] = useState(0)
  const [balanceInr, setBalanceInr] = useState(0)
  const [usdInrRate, setUsdInrRate] = useState('85')
  const [rateInput, setRateInput] = useState('85')
  const [updatingRate, setUpdatingRate] = useState(false)
  const [lastChecked, setLastChecked] = useState('')

  useEffect(() => {
    document.title = 'Delta Bot — Settings'
  }, [])

  const [name, setName] = useState('main')
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [showSecret, setShowSecret] = useState(false)

  const [connecting, setConnecting] = useState(false)
  const [formError, setFormError] = useState('')
  const [disconnectOpen, setDisconnectOpen] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [toast, setToast] = useState(null)

  const loadStatus = useCallback(async () => {
    setLoadingStatus(true)
    try {
      const status = await getAccountStatus()
      if (status?.connected) {
        setConnected(true)
        setAccountName(status.account_name || '')
        setBalance(status.balance_usdt || 0)
        setBalanceInr(status.balance_inr || 0)
        setLastChecked(status.last_checked || '')
      } else {
        setConnected(false)
        setAccountName('')
        setBalance(0)
        setBalanceInr(0)
        setLastChecked('')
      }
      const rate = status?.usd_inr_rate ?? 85
      setUsdInrRate(String(rate))
      setRateInput(String(rate))
    } catch (err) {
      setConnected(false)
      setToast({ type: 'error', message: err.message || 'Failed to load status' })
    } finally {
      setLoadingStatus(false)
    }
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  const handleConnect = async (e) => {
    e.preventDefault()
    setFormError('')
    if (!name.trim() || !apiKey.trim() || !apiSecret.trim()) {
      setFormError('Account name, API key, and API secret are required.')
      return
    }

    setConnecting(true)
    try {
      const result = await connectAccount({
        name: name.trim(),
        api_key: apiKey.trim(),
        api_secret: apiSecret.trim(),
      })
      setConnected(true)
      setAccountName(result.account_name || name.trim())
      setBalance(result.balance_usdt || 0)
      setLastChecked(new Date().toISOString())
      setApiKey('')
      setApiSecret('')
      setToast({ type: 'success', message: 'Account connected successfully' })
      notifyAccountUpdated()
      // Refresh last_checked from backend status
      await loadStatus()
    } catch (err) {
      setFormError(err.message || 'Connection failed')
      setToast({ type: 'error', message: err.message || 'Connection failed' })
    } finally {
      setConnecting(false)
    }
  }

  const handleDisconnect = async () => {
    setDisconnecting(true)
    try {
      await disconnectAccount()
      setConnected(false)
      setAccountName('')
      setBalance(0)
      setBalanceInr(0)
      setLastChecked('')
      setDisconnectOpen(false)
      setToast({ type: 'info', message: 'Disconnected' })
      notifyAccountUpdated()
    } catch (err) {
      setToast({ type: 'error', message: err.message || 'Disconnect failed' })
    } finally {
      setDisconnecting(false)
    }
  }

  const handleUpdateRate = async () => {
    const newRate = Number(rateInput)
    if (!Number.isFinite(newRate) || newRate <= 0 || newRate > 500) {
      setToast({ type: 'error', message: 'Rate must be between 0 and 500' })
      return
    }
    setUpdatingRate(true)
    try {
      const result = await updateAccountSettings({ usd_inr_rate: newRate })
      const saved = result?.usd_inr_rate ?? newRate
      setUsdInrRate(String(saved))
      setRateInput(String(saved))
      setToast({ type: 'success', message: `✅ Rate updated to ₹${saved}` })
      notifyAccountUpdated()
      await loadStatus()
    } catch (err) {
      setToast({
        type: 'error',
        message: err.message || 'Failed to update rate',
      })
    } finally {
      setUpdatingRate(false)
    }
  }

  return (
    <main className="mx-auto max-w-xl px-4 py-8">
      <h1 className="mb-6 text-2xl font-semibold text-white">⚙️ API Settings</h1>

      {loadingStatus ? (
        <div className="flex items-center gap-3 rounded-xl border border-gray-700 bg-gray-800 px-4 py-6 text-gray-300">
          <LoadingSpinner size="md" color="blue" />
          Checking connection status…
        </div>
      ) : connected ? (
        <div className="rounded-xl border border-green-700/60 bg-green-950/40 p-5">
          <div className="mb-3 text-lg font-semibold text-green-400">✅ Connected</div>
          <dl className="space-y-2 text-sm text-gray-200">
            <div className="flex justify-between gap-4">
              <dt className="text-gray-400">Account</dt>
              <dd className="font-medium">{accountName || '—'}</dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-gray-400">Balance</dt>
              <dd className="font-medium text-right">
                ${formatBalance(balance)} · ₹{formatBalanceInr(balanceInr)}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-gray-400">Last checked</dt>
              <dd className="font-medium">{formatLastChecked(lastChecked)}</dd>
            </div>
          </dl>
          <div className="mt-5 flex justify-end">
            <button
              type="button"
              onClick={() => setDisconnectOpen(true)}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700"
            >
              Disconnect
            </button>
          </div>
        </div>
      ) : (
        <form
          onSubmit={handleConnect}
          className="space-y-4 rounded-xl border border-gray-700 bg-gray-800 p-5"
        >
          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">Account Name</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white outline-none focus:border-blue-500"
              placeholder="main"
              autoComplete="off"
            />
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">API Key</span>
            <div className="relative">
              <input
                type={showKey ? 'text' : 'password'}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 pr-10 text-white outline-none focus:border-blue-500"
                placeholder="Enter API key"
                autoComplete="off"
              />
              <button
                type="button"
                onClick={() => setShowKey((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-200"
              >
                {showKey ? 'Hide' : 'Show'}
              </button>
            </div>
          </label>

          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">API Secret</span>
            <div className="relative">
              <input
                type={showSecret ? 'text' : 'password'}
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
                className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 pr-10 text-white outline-none focus:border-blue-500"
                placeholder="Enter API secret"
                autoComplete="off"
              />
              <button
                type="button"
                onClick={() => setShowSecret((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400 hover:text-gray-200"
              >
                {showSecret ? 'Hide' : 'Show'}
              </button>
            </div>
          </label>

          {formError && (
            <div className="rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
              {formError}
            </div>
          )}

          <button
            type="submit"
            disabled={connecting}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-blue-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-400 disabled:cursor-not-allowed disabled:opacity-70"
          >
            {connecting ? (
              <>
                <LoadingSpinner size="sm" color="white" />
                Connecting...
              </>
            ) : (
              <>🔌 Connect Account</>
            )}
          </button>
        </form>
      )}

      <section className="mt-10 rounded-xl border border-gray-700 bg-gray-800/60 p-5">
        <h2 className="text-sm font-semibold text-white">💱 Currency Settings</h2>
        <p className="mt-1 text-xs text-gray-500">
          Used for balance display in Navbar (USD × rate → INR).
        </p>
        <label className="mt-4 block text-sm text-gray-300">
          USD to INR Rate
          <div className="mt-1 flex max-w-xs items-center gap-2">
            <input
              type="number"
              min={1}
              max={500}
              step={0.01}
              value={rateInput}
              onChange={(e) => setRateInput(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            <span className="shrink-0 text-gray-400">₹</span>
          </div>
          <span className="mt-1 block text-xs text-gray-500">
            Current: ₹{usdInrRate} per $1
          </span>
        </label>
        <button
          type="button"
          disabled={updatingRate}
          onClick={handleUpdateRate}
          className="mt-4 inline-flex items-center gap-2 rounded-md bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {updatingRate ? <LoadingSpinner size="sm" color="white" /> : null}
          Update Rate
        </button>
      </section>

      {/* Emergency recovery */}
      <section className="mt-10 rounded-xl border border-dashed border-gray-700 bg-gray-800/40 p-4">
        <h2 className="text-sm font-semibold text-gray-300">Emergency tools</h2>
        <p className="mt-1 text-xs text-gray-500">
          Only if a strangle is already open on Delta and was not placed through
          this bot (e.g. after a partial fill recovery).
        </p>
        <Link
          to="/new-trade?emergency=1"
          className="mt-3 inline-block text-sm text-amber-400 underline hover:text-amber-300"
        >
          Register existing Delta trade →
        </Link>
      </section>

      <ConfirmDialog
        isOpen={disconnectOpen}
        title="Disconnect account?"
        message="This removes stored API credentials from the local database. You can reconnect anytime."
        confirmLabel={disconnecting ? 'Disconnecting…' : 'Disconnect'}
        onCancel={() => setDisconnectOpen(false)}
        onConfirm={handleDisconnect}
      />

      <Toast
        message={toast?.message}
        type={toast?.type}
        onClose={() => setToast(null)}
      />
    </main>
  )
}
