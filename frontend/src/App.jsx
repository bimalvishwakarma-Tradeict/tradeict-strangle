import { useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import Navbar from './components/Navbar'
import Toast from './components/ui/Toast'
import { useWebSocket } from './hooks/useWebSocket'
import AutoTrade from './pages/AutoTrade'
import Accounts from './pages/Accounts'
import Dashboard from './pages/Dashboard'
import Logs from './pages/Logs'
import TradeInitiator from './pages/TradeInitiator'
import Settings from './pages/Settings'

const WS_URL = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`

function fmtStrike(v) {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', { maximumFractionDigits: 0 })
}

function GlobalAutoTradeToasts() {
  const { lastMessage } = useWebSocket(WS_URL)
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
    // AUTO_TRADE_WAITING — silent (banner only)
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

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <div className="min-h-screen bg-gray-900 text-gray-100">
          <Navbar />
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/new-trade" element={<TradeInitiator />} />
            <Route path="/auto-trade" element={<AutoTrade />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/accounts" element={<Accounts />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
          <GlobalAutoTradeToasts />
        </div>
      </BrowserRouter>
    </ErrorBoundary>
  )
}
