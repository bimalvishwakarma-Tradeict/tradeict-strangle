import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { changePassword, storeAuthSession } from '../services/api'
import { useAuth } from '../auth/AuthContext'
import LoadingSpinner from '../components/ui/LoadingSpinner'

export default function ChangePassword() {
  const { token, email, role, mustChangePassword, setSession, logoutLocal } =
    useAuth()
  const navigate = useNavigate()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  if (!token) {
    return <Navigate to="/login" replace />
  }
  if (!mustChangePassword) {
    return <Navigate to="/" replace />
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    if (newPassword.length < 8) {
      setError('New password must be at least 8 characters.')
      return
    }
    if (newPassword !== confirmPassword) {
      setError('New password and confirmation do not match.')
      return
    }
    setLoading(true)
    try {
      await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      })
      storeAuthSession({
        token,
        email,
        role,
        must_change_password: false,
      })
      setSession({
        token,
        email,
        role,
        must_change_password: false,
      })
      navigate('/', { replace: true })
    } catch (err) {
      setError(err?.message || 'Failed to change password')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-900 px-4">
      <div className="w-full max-w-md rounded-xl border border-gray-700 bg-gray-800 p-6 shadow-xl">
        <h1 className="text-center text-xl font-semibold text-white">
          Change password
        </h1>
        <p className="mt-1 text-center text-sm text-gray-400">
          You must set a new password before using the dashboard.
        </p>
        <p className="mt-2 text-center text-xs text-gray-500">{email}</p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">Current password</span>
            <input
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
              autoComplete="current-password"
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white outline-none focus:border-blue-500"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">New password</span>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white outline-none focus:border-blue-500"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">Confirm new password</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white outline-none focus:border-blue-500"
            />
          </label>

          {error ? (
            <div className="rounded-md border border-red-700/50 bg-red-950/40 px-3 py-2 text-sm text-red-300">
              {error}
            </div>
          ) : null}

          <button
            type="submit"
            disabled={loading}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-blue-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-400 disabled:cursor-not-allowed disabled:opacity-70"
          >
            {loading ? (
              <>
                <LoadingSpinner size="sm" color="white" />
                Saving...
              </>
            ) : (
              'Update password'
            )}
          </button>
        </form>

        <button
          type="button"
          onClick={() => {
            logoutLocal()
            navigate('/login', { replace: true })
          }}
          className="mt-4 w-full text-center text-sm text-gray-400 hover:text-gray-200"
        >
          Sign out
        </button>
      </div>
    </div>
  )
}
