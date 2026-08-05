import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import LoadingSpinner from '../components/ui/LoadingSpinner'
import Toast from '../components/ui/Toast'
import {
  addSlaveAccount,
  deleteSlaveAccount,
  getAccountStatus,
  getSlaveAccounts,
  testSlaveConnection,
  toggleSlaveAccount,
  updateSlaveAccount,
} from '../services/api'

const MULTIPLIER_PRESETS = [0.5, 1, 1.5, 2, 3]
const SERVER_IP = '169.58.123.144'

function formatUsd(value) {
  return Number(value || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function formatInr(value) {
  return Number(value || 0).toLocaleString('en-IN', {
    maximumFractionDigits: 0,
  })
}

function statusBadge(status) {
  const s = String(status || 'unknown').toLowerCase()
  if (s === 'connected') {
    return { label: '🟢 Connected', className: 'text-green-400' }
  }
  if (s === 'error') {
    return { label: '🔴 Error', className: 'text-red-400' }
  }
  return { label: '⚪ Unknown', className: 'text-gray-400' }
}

function SlaveModal({
  open,
  mode,
  initial,
  saving,
  formError,
  onClose,
  onSubmit,
}) {
  const [name, setName] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [apiSecret, setApiSecret] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [showSecret, setShowSecret] = useState(false)
  const [multiplier, setMultiplier] = useState('1')
  const [isActive, setIsActive] = useState(true)

  useEffect(() => {
    if (!open) return
    setName(initial?.name || '')
    setApiKey('')
    setApiSecret('')
    setShowKey(false)
    setShowSecret(false)
    setMultiplier(String(initial?.qty_multiplier ?? 1))
    setIsActive(initial?.is_active !== false)
  }, [open, initial])

  const multNum = Number(multiplier) || 0

  if (!open) return null

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({
      name: name.trim(),
      api_key: apiKey.trim(),
      api_secret: apiSecret.trim(),
      qty_multiplier: multNum,
      is_active: isActive,
    })
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md rounded-xl border border-gray-700 bg-gray-800 p-5 shadow-xl"
      >
        <h3 className="text-lg font-semibold text-white">
          {mode === 'edit' ? 'Edit Slave Account' : 'Add Slave Account'}
        </h3>

        <div className="mt-3 rounded-lg border border-amber-700/50 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠️ Make sure to whitelist server IP ({SERVER_IP}) in this account&apos;s
          Delta Exchange API settings.
        </div>

        <label className="mt-4 block text-sm text-gray-300">
          Account Name
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1 w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            placeholder="account-B"
            autoComplete="off"
          />
        </label>

        <label className="mt-3 block text-sm text-gray-300">
          API Key
          <div className="relative mt-1">
            <input
              required={mode === 'add'}
              type={showKey ? 'text' : 'password'}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 pr-10 text-white"
              placeholder={mode === 'edit' ? 'Leave blank to keep current' : 'Enter API key'}
              autoComplete="off"
            />
            <button
              type="button"
              onClick={() => setShowKey((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400"
            >
              {showKey ? 'Hide' : '👁'}
            </button>
          </div>
        </label>

        <label className="mt-3 block text-sm text-gray-300">
          API Secret
          <div className="relative mt-1">
            <input
              required={mode === 'add'}
              type={showSecret ? 'text' : 'password'}
              value={apiSecret}
              onChange={(e) => setApiSecret(e.target.value)}
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 pr-10 text-white"
              placeholder={
                mode === 'edit' ? 'Leave blank to keep current' : 'Enter API secret'
              }
              autoComplete="off"
            />
            <button
              type="button"
              onClick={() => setShowSecret((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400"
            >
              {showSecret ? 'Hide' : '👁'}
            </button>
          </div>
        </label>

        <label className="mt-3 block text-sm text-gray-300">
          Qty Multiplier
          <div className="mt-1 flex items-center gap-2">
            <input
              type="number"
              min={0.1}
              max={100}
              step={0.1}
              value={multiplier}
              onChange={(e) => setMultiplier(e.target.value)}
              className="w-28 rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white"
            />
            <span className="text-gray-400">×</span>
          </div>
          <p className="mt-1 text-xs text-gray-500">
            Master places 1 lot → This account places {multNum || '—'} lot(s)
            <br />
            Master places 2 lots → This account places{' '}
            {multNum ? (multNum * 2).toFixed(1).replace(/\.0$/, '') : '—'} lot(s)
          </p>
        </label>

        <div className="mt-2 flex flex-wrap gap-2">
          {MULTIPLIER_PRESETS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setMultiplier(String(p))}
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                Number(multiplier) === p
                  ? 'bg-blue-500 text-white'
                  : 'bg-gray-900 text-gray-300 hover:bg-gray-700'
              }`}
            >
              {p}×
            </button>
          ))}
        </div>

        <label className="mt-4 flex items-center gap-3 text-sm text-gray-300">
          <button
            type="button"
            onClick={() => setIsActive((v) => !v)}
            className={`relative h-6 w-11 rounded-full transition ${
              isActive ? 'bg-green-600' : 'bg-gray-600'
            }`}
            aria-pressed={isActive}
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition ${
                isActive ? 'left-5' : 'left-0.5'
              }`}
            />
          </button>
          Active: {isActive ? '● ON' : '○ OFF'}
        </label>

        {formError && (
          <div className="mt-3 rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
            {formError}
          </div>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="rounded-md border border-gray-600 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-700 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="inline-flex items-center gap-2 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {saving ? (
              <>
                <LoadingSpinner size="sm" color="white" />
                Testing connection…
              </>
            ) : (
              'Test & Save'
            )}
          </button>
        </div>
      </form>
    </div>
  )
}

function SlaveCard({
  slave,
  onTest,
  onToggle,
  onEdit,
  onDelete,
  testingId,
  togglingId,
}) {
  const badge = statusBadge(slave.connection_status)
  const paused = !slave.is_active
  const testing = testingId === slave.id
  const toggling = togglingId === slave.id

  return (
    <div
      className={`rounded-xl border p-4 ${
        paused
          ? 'border-gray-700 bg-gray-900/40 opacity-70'
          : 'border-gray-700 bg-gray-800/60'
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-white">📋 {slave.name}</span>
            <span className={`text-sm ${badge.className}`}>{badge.label}</span>
            {paused ? (
              <span className="rounded bg-gray-700 px-2 py-0.5 text-xs text-gray-300">
                PAUSED
              </span>
            ) : (
              <span className="rounded bg-green-900/50 px-2 py-0.5 text-xs text-green-300">
                ● Active
              </span>
            )}
          </div>
          <div className="mt-1 text-sm text-gray-300">
            Balance: ${formatUsd(slave.balance_usd)} · ₹
            {formatInr(slave.balance_inr)} · Multiplier:{' '}
            {Number(slave.qty_multiplier || 1)}×
          </div>
          <div className="mt-0.5 text-xs text-gray-500">
            Active trades: {slave.active_trade_count ?? 0}
          </div>
          {slave.connection_status === 'error' && slave.last_error ? (
            <div className="mt-1 text-xs text-red-400">❌ {slave.last_error}</div>
          ) : null}
          {slave._testMessage ? (
            <div
              className={`mt-1 text-xs ${
                slave._testOk ? 'text-green-400' : 'text-red-400'
              }`}
            >
              {slave._testMessage}
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={testing}
            onClick={() => onTest(slave)}
            className="rounded-md border border-gray-600 px-2.5 py-1 text-xs text-gray-200 hover:bg-gray-700 disabled:opacity-50"
          >
            {testing ? 'Testing…' : 'Test'}
          </button>
          <button
            type="button"
            disabled={toggling}
            onClick={() => onToggle(slave)}
            className={`rounded-md px-2.5 py-1 text-xs font-medium ${
              paused
                ? 'bg-green-700 text-white hover:bg-green-600'
                : 'bg-gray-700 text-gray-200 hover:bg-gray-600'
            } disabled:opacity-50`}
          >
            {paused ? '○ Resume' : '● Pause'}
          </button>
          <button
            type="button"
            onClick={() => onEdit(slave)}
            className="rounded-md border border-gray-600 px-2.5 py-1 text-xs text-gray-200 hover:bg-gray-700"
          >
            Edit
          </button>
          <button
            type="button"
            onClick={() => onDelete(slave)}
            className="rounded-md border border-red-800 px-2.5 py-1 text-xs text-red-300 hover:bg-red-950"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Accounts() {
  const [loading, setLoading] = useState(true)
  const [master, setMaster] = useState(null)
  const [slaves, setSlaves] = useState([])
  const [toast, setToast] = useState(null)

  const [modalOpen, setModalOpen] = useState(false)
  const [modalMode, setModalMode] = useState('add')
  const [editingSlave, setEditingSlave] = useState(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  const [testingId, setTestingId] = useState(null)
  const [togglingId, setTogglingId] = useState(null)
  const [deleteTarget, setDeleteTarget] = useState(null)
  const [deleting, setDeleting] = useState(false)

  useEffect(() => {
    document.title = 'Delta Bot — Accounts'
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [status, list] = await Promise.all([
        getAccountStatus(),
        getSlaveAccounts(),
      ])
      setMaster(status)
      setSlaves(Array.isArray(list) ? list : [])
    } catch (err) {
      setToast({
        type: 'error',
        message: err.message || 'Failed to load accounts',
      })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  const masterBadge = useMemo(
    () => statusBadge(master?.connected ? 'connected' : 'unknown'),
    [master],
  )

  const openAdd = () => {
    setModalMode('add')
    setEditingSlave(null)
    setFormError('')
    setModalOpen(true)
  }

  const openEdit = (slave) => {
    setModalMode('edit')
    setEditingSlave(slave)
    setFormError('')
    setModalOpen(true)
  }

  const handleSave = async (form) => {
    if (!form.name) {
      setFormError('Account name is required.')
      return
    }
    if (modalMode === 'add' && (!form.api_key || !form.api_secret)) {
      setFormError('API key and secret are required.')
      return
    }
    if (modalMode === 'edit' && ((form.api_key && !form.api_secret) || (!form.api_key && form.api_secret))) {
      setFormError('Provide both API key and secret to rotate credentials.')
      return
    }

    setSaving(true)
    setFormError('')
    try {
      if (modalMode === 'add') {
        await addSlaveAccount(form)
        setToast({ type: 'success', message: '✅ Slave added!' })
      } else {
        const payload = {
          name: form.name,
          qty_multiplier: form.qty_multiplier,
          is_active: form.is_active,
        }
        if (form.api_key) payload.api_key = form.api_key
        if (form.api_secret) payload.api_secret = form.api_secret
        await updateSlaveAccount(editingSlave.id, payload)
        setToast({ type: 'success', message: '✅ Slave updated!' })
      }
      setModalOpen(false)
      await refresh()
    } catch (err) {
      setFormError(err.message || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async (slave) => {
    setTestingId(slave.id)
    try {
      const res = await testSlaveConnection(slave.id)
      const msg = `✅ Connected: ${res.account_name || slave.name} ($${formatUsd(res.balance_usd)})`
      setSlaves((prev) =>
        prev.map((s) =>
          s.id === slave.id
            ? {
                ...s,
                connection_status: 'connected',
                balance_usd: res.balance_usd ?? s.balance_usd,
                balance_inr: res.balance_inr ?? s.balance_inr,
                last_error: null,
                _testOk: true,
                _testMessage: msg,
              }
            : s,
        ),
      )
      setToast({ type: 'success', message: msg })
    } catch (err) {
      const msg = `❌ Error: ${err.message || 'Connection failed'}`
      setSlaves((prev) =>
        prev.map((s) =>
          s.id === slave.id
            ? {
                ...s,
                connection_status: 'error',
                last_error: err.message,
                _testOk: false,
                _testMessage: msg,
              }
            : s,
        ),
      )
      setToast({ type: 'error', message: msg })
    } finally {
      setTestingId(null)
    }
  }

  const handleToggle = async (slave) => {
    setTogglingId(slave.id)
    try {
      const res = await toggleSlaveAccount(slave.id)
      setSlaves((prev) =>
        prev.map((s) =>
          s.id === slave.id ? { ...s, is_active: res.is_active } : s,
        ),
      )
      setToast({
        type: 'info',
        message: res.message || (res.is_active ? 'Slave enabled' : 'Slave paused'),
      })
    } catch (err) {
      setToast({ type: 'error', message: err.message || 'Toggle failed' })
    } finally {
      setTogglingId(null)
    }
  }

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await deleteSlaveAccount(deleteTarget.id)
      setSlaves((prev) => prev.filter((s) => s.id !== deleteTarget.id))
      setDeleteTarget(null)
      setToast({ type: 'info', message: 'Slave account deleted' })
    } catch (err) {
      setToast({ type: 'error', message: err.message || 'Delete failed' })
    } finally {
      setDeleting(false)
    }
  }

  if (loading) {
    return (
      <main className="mx-auto flex max-w-3xl items-center justify-center px-4 py-20">
        <LoadingSpinner />
      </main>
    )
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 px-4 py-6">
      <h1 className="text-xl font-semibold text-white">👥 Account Management</h1>

      <section className="space-y-3">
        <h2 className="text-xs font-semibold tracking-wide text-gray-400">
          MASTER ACCOUNT
        </h2>
        {master?.connected ? (
          <div className="rounded-xl border border-green-700/50 bg-green-950/30 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-semibold text-white">
                ⭐ {master.account_name || 'Master'}
              </div>
              <span className={`text-sm ${masterBadge.className}`}>
                {masterBadge.label}
              </span>
            </div>
            <div className="mt-2 text-sm text-gray-300">
              Balance: ${formatUsd(master.balance_usdt)} · ₹
              {formatInr(master.balance_inr)} · Role: Master
            </div>
            <Link
              to="/settings"
              className="mt-3 inline-block text-sm text-blue-400 underline hover:text-blue-300"
            >
              Change API Keys →
            </Link>
          </div>
        ) : (
          <div className="rounded-xl border border-gray-700 bg-gray-800/50 p-4 text-sm text-gray-400">
            No master account connected.{' '}
            <Link to="/settings" className="text-blue-400 underline">
              Connect in Settings
            </Link>
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-xs font-semibold tracking-wide text-gray-400">
            SLAVE ACCOUNTS
          </h2>
          <button
            type="button"
            onClick={openAdd}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-blue-500"
          >
            + Add Slave
          </button>
        </div>

        {slaves.length === 0 ? (
          <div className="rounded-xl border border-dashed border-gray-700 bg-gray-800/40 px-6 py-10 text-center">
            <p className="text-sm font-medium text-gray-300">
              No slave accounts added yet.
            </p>
            <p className="mt-1 text-xs text-gray-500">
              Add a slave account to mirror trades automatically.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {slaves.map((slave) => (
              <SlaveCard
                key={slave.id}
                slave={slave}
                onTest={handleTest}
                onToggle={handleToggle}
                onEdit={openEdit}
                onDelete={setDeleteTarget}
                testingId={testingId}
                togglingId={togglingId}
              />
            ))}
          </div>
        )}
      </section>

      <SlaveModal
        open={modalOpen}
        mode={modalMode}
        initial={editingSlave}
        saving={saving}
        formError={formError}
        onClose={() => !saving && setModalOpen(false)}
        onSubmit={handleSave}
      />

      <ConfirmDialog
        isOpen={Boolean(deleteTarget)}
        title="Delete slave account?"
        message={
          deleteTarget
            ? `Delete slave account '${deleteTarget.name}'? This will NOT close any open trades on this account. You must close them manually on Delta Exchange.`
            : ''
        }
        confirmLabel={deleting ? 'Deleting…' : 'Delete'}
        confirmDisabled={deleting}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={handleDeleteConfirm}
      />

      {toast && (
        <Toast
          message={toast.message}
          type={toast.type}
          onClose={() => setToast(null)}
        />
      )}
    </main>
  )
}
