import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { login, storeAuthSession } from '../services/api'
import { useAuth } from '../auth/AuthContext'
import LoadingSpinner from '../components/ui/LoadingSpinner'

export default function Login() {
  const { token, mustChangePassword, setSession } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  if (token && mustChangePassword) {
    return <Navigate to="/change-password" replace />
  }
  if (token && !mustChangePassword) {
    return <Navigate to="/" replace />
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await login({ email: email.trim(), password })
      const data = res?.data || res
      if (!data?.token) {
        throw new Error('Login response missing token')
      }
      storeAuthSession({
        token: data.token,
        email: data.email,
        role: data.role,
        must_change_password: Boolean(data.must_change_password),
      })
      setSession({
        token: data.token,
        email: data.email,
        role: data.role,
        must_change_password: Boolean(data.must_change_password),
      })
      if (data.must_change_password) {
        navigate('/change-password', { replace: true })
      } else {
        navigate('/', { replace: true })
      }
    } catch (err) {
      setError(err?.message || 'Invalid email or password')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-900 px-4">
      <div className="w-full max-w-md rounded-xl border border-gray-700 bg-gray-800 p-6 shadow-xl">
        <h1 className="text-center text-xl font-semibold text-white">
          Tradeict Delta Bot
        </h1>
        <p className="mt-1 text-center text-sm text-gray-400">Sign in to continue</p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">Email</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="username"
              className="w-full rounded-md border border-gray-600 bg-gray-900 px-3 py-2 text-white outline-none focus:border-blue-500"
            />
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-gray-300">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
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
                Signing in...
              </>
            ) : (
              'Sign in'
            )}
          </button>
        </form>
      </div>
    </div>
  )
}
