import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import {
  connectAccount,
  createUser,
  deleteUser,
  disconnectAccount,
  getAccountStatus,
  listUsers,
  patchUser,
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

function formatCreated(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      dateStyle: 'medium',
      timeStyle: 'short',
    })
  } catch {
    return iso
  }
}

function notifyAccountUpdated() {
  window.dispatchEvent(new Event('tradeict-account-updated'))
}

function UserManagementSection({ onToast }) {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [editUser, setEditUser] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [busy, setBusy] = useState(false)

  const [formEmail, setFormEmail] = useState('')
  const [formPassword, setFormPassword] = useState('')
  const [formRole, setFormRole] = useState('user')
  const [formActive, setFormActive] = useState(true)

  const loadUsers = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await listUsers()
      setUsers(res?.data || [])
    } catch (err) {
      setError(err?.message || 'Failed to load users')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadUsers()
  }, [loadUsers])

  const resetForm = () => {
    setFormEmail('')
    setFormPassword('')
    setFormRole('user')
    setFormActive(true)
  }

  const openAdd = () => {
    resetForm()
    setAddOpen(true)
  }

  const openEdit = (user) => {
    setEditUser(user)
    setFormEmail(user.email || '')
    setFormPassword('')
    setFormRole(user.role || 'user')
    setFormActive(Boolean(user.is_active))
  }

  const handleCreate = async () => {
    setBusy(true)
    try {
      await createUser({
        email: formEmail.trim(),
        password: formPassword,
        role: formRole,
      })
      onToast?.({ type: 'success', message: 'User created' })
      setAddOpen(false)
      resetForm()
      await loadUsers()
    } catch (err) {
      onToast?.({ type: 'error', message: err?.message || 'Create failed' })
    } finally {
      setBusy(false)
    }
  }

  const handlePatch = async () => {
    if (!editUser) return
    setBusy(true)
    try {
      const payload = {
        email: formEmail.trim(),
        role: formRole,
        is_active: formActive,
      }
      if (formPassword.trim()) {
        payload.new_password = formPassword
      }
      await patchUser(editUser.id, payload)
      onToast?.({ type: 'success', message: 'User updated' })
      setEditUser(null)
      resetForm()
      await loadUsers()
    } catch (err) {
      onToast?.({ type: 'error', message: err?.message || 'Update failed' })
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    setBusy(true)
    try {
      await deleteUser(deleteTarget.id)
      onToast?.({ type: 'success', message: 'User deleted' })
      setDeleteTarget(null)
      await loadUsers()
    } catch (err) {
      onToast?.({ type: 'error', message: err?.message || 'Delete failed' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-10 rounded-xl border border-gray-700 bg-gray-800/60 p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-white">👤 User Management</h2>
          <p className="mt-1 text-xs text-gray-500">
            Admin only — add, edit, or remove dashboard logins.
          </p>
        </div>
        <button
          type="button"
          onClick={openAdd}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-500"
        >
          Add user
        </button>
      </div>

      {error ? (
        <div className="mt-3 rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      ) : null}

      {loading ? (
        <div className="mt-4 flex justify-center py-6">
          <LoadingSpinner />
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="min-w-full text-left text-sm text-gray-200">
            <thead className="text-[11px] uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-2 py-2">Email</th>
                <th className="px-2 py-2">Role</th>
                <th className="px-2 py-2">Active</th>
                <th className="px-2 py-2">Created</th>
                <th className="px-2 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-t border-gray-700/80">
                  <td className="px-2 py-2">{u.email}</td>
                  <td className="px-2 py-2 capitalize">{u.role}</td>
                  <td className="px-2 py-2">
                    {u.is_active ? (
                      <span className="text-green-400">Yes</span>
                    ) : (
                      <span className="text-red-400">No</span>
                    )}
                  </td>
                  <td className="px-2 py-2 text-xs text-gray-400">
                    {formatCreated(u.created_at)}
                  </td>
                  <td className="px-2 py-2">
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        onClick={() => openEdit(u)}
                        className="rounded border border-gray-600 px-2 py-0.5 text-xs text-gray-200 hover:bg-gray-700"
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => setDeleteTarget(u)}
                        className="rounded border border-red-800 px-2 py-0.5 text-xs text-red-300 hover:bg-red-950/50"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {users.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-2 py-4 text-center text-gray-500">
                    No users found
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      )}

      {(addOpen || editUser) && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
          <div className="w-full max-w-md rounded-xl border border-gray-700 bg-gray-800 p-5 shadow-xl">
            <h3 className="text-lg font-semibold text-white">
              {addOpen ? 'Add user' : 'Edit user'}
            </h3>
            <div className="mt-4 space-y-3">
              <label className="block text-sm">
                <span className="mb-1 block text-gray-300">Email</span>
                <input
                  type="email"
                  value={formEmail}
                  onChange={(e) => setFormEmail(e.target.value)}
                  className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block text-gray-300">
                  {addOpen ? 'Password' : 'Reset password (optional)'}
                </span>
                <input
                  type="password"
                  value={formPassword}
                  onChange={(e) => setFormPassword(e.target.value)}
                  minLength={addOpen ? 8 : undefined}
                  className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                  placeholder={addOpen ? 'Min 8 characters' : 'Leave blank to keep'}
                />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block text-gray-300">Role</span>
                <select
                  value={formRole}
                  onChange={(e) => setFormRole(e.target.value)}
                  className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
                >
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              {editUser ? (
                <label className="flex items-center gap-2 text-sm text-gray-300">
                  <input
                    type="checkbox"
                    checked={formActive}
                    onChange={(e) => setFormActive(e.target.checked)}
                    className="rounded border-gray-600"
                  />
                  Active
                </label>
              ) : null}
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setAddOpen(false)
                  setEditUser(null)
                  resetForm()
                }}
                className="rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={addOpen ? handleCreate : handlePatch}
                className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {busy ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        isOpen={Boolean(deleteTarget)}
        title="Delete user?"
        message={
          deleteTarget
            ? `Permanently delete ${deleteTarget.email}? This cannot be undone.`
            : ''
        }
        confirmLabel={busy ? 'Deleting…' : 'Delete'}
        confirmDisabled={busy}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDelete}
      />
    </section>
  )
}

export default function Settings() {
  const { isAdmin } = useAuth()
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

      {isAdmin ? (
        <UserManagementSection onToast={(t) => setToast(t)} />
      ) : null}

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
