import { useEffect, useMemo, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext'
import ErrorBoundary from './components/ErrorBoundary'
import Navbar from './components/Navbar'
import Toast from './components/ui/Toast'
import { useWebSocket } from './hooks/useWebSocket'
import AutoTrade from './pages/AutoTrade'
import Accounts from './pages/Accounts'
import Backtest from './pages/Backtest'
import ChangePassword from './pages/ChangePassword'
import Dashboard from './pages/Dashboard'
import Login from './pages/Login'
import Logs from './pages/Logs'
import TradeInitiator from './pages/TradeInitiator'
import Settings from './pages/Settings'

const WS_BASE = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`

function fmtStrike(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function ProtectedRoute({ children }) {
  const { token, mustChangePassword } = useAuth()
  if (!token) {
    return <Navigate to="/login" replace />
  }
  if (mustChangePassword) {
    return <Navigate to="/change-password" replace />
  }
  return children
}

function GlobalAutoTradeToasts({ wsUrl }) {
  const { lastMessage } = useWebSocket(wsUrl)
  const [toast, setToast] = useState(null)

  useEffect(() => {
    if (!lastMessage?.type) return
    if (lastMessage.type === 'AUTO_TRADE_PLACED') {
      const und = lastMessage.underlying || 'BTC'
      const strike = fmtStrike(lastMessage.strike)
      setToast({
        type: 'success',
        durationMs: 5000,
        message: `✅ Auto trade placed! ${und} straddle @ $${strike}`,
      })
    } else if (lastMessage.type === 'AUTO_TRADE_FAILED') {
      const err = lastMessage.error || lastMessage.message || 'Unknown error'
      const retry = lastMessage.retry_in_seconds ?? 60
      setToast({
        type: 'warning',
        durationMs: 8000,
        message: `⚠️ Auto trade failed: ${err}. Retrying in ${retry}s.`,
      })
    }
  }, [lastMessage])

  if (!toast) return null
  return (
    <Toast
      message={toast.message}
      type={toast.type}
      durationMs={toast.durationMs}
      onClose={() => setToast(null)}
    />
  )
}

function AuthenticatedShell() {
  const { token } = useAuth()
  const wsUrl = useMemo(() => {
    if (!token) return ''
    return `${WS_BASE}?token=${encodeURIComponent(token)}`
  }, [token])

  return (
    <ProtectedRoute>
      <div className="min-h-screen bg-gray-900 text-gray-100">
        <Navbar wsUrl={wsUrl} />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/new-trade" element={<TradeInitiator />} />
          <Route path="/auto-trade" element={<AutoTrade />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        {wsUrl ? <GlobalAutoTradeToasts wsUrl={wsUrl} /> : null}
      </div>
    </ProtectedRoute>
  )
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/change-password" element={<ChangePassword />} />
      <Route path="/*" element={<AuthenticatedShell />} />
    </Routes>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </ErrorBoundary>
  )
}
