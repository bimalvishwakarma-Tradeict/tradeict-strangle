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
import Strategy3 from './pages/Strategy3'
import Strategies from './pages/Strategies'
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

function GlobalLiveAlerts({ wsUrl }) {
  const { lastMessage } = useWebSocket(wsUrl)
  const [toast, setToast] = useState(null)
  const [alerts, setAlerts] = useState({})

  useEffect(() => {
    if (!lastMessage?.type && !lastMessage?.event) return
    const event = lastMessage.event || lastMessage.type
    if (event === 'DELTA_SL_MISSING' || lastMessage.sticky) {
      const key = `${lastMessage.trade_id || 0}:${lastMessage.leg || 'leg'}`
      setAlerts((prev) => ({
        ...prev,
        [key]: lastMessage.message || '⚠ DELTA STOP-LOSS NAHI LAGA',
      }))
    }
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

  const rows = Object.entries(alerts)
  return (
    <>
      {rows.length > 0 ? (
        <div className="sticky top-0 z-50 space-y-1 p-2">
          {rows.map(([key, message]) => (
            <div
              key={key}
              className="rounded border-2 border-red-600 bg-red-950 px-3 py-2 text-sm font-semibold text-red-100"
            >
              {message}
            </div>
          ))}
        </div>
      ) : null}
      {toast ? (
        <Toast
          message={toast.message}
          type={toast.type}
          durationMs={toast.durationMs}
          onClose={() => setToast(null)}
        />
      ) : null}
    </>
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
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/strategy3" element={<Strategy3 />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        {wsUrl ? <GlobalLiveAlerts wsUrl={wsUrl} /> : null}
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
