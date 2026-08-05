import { useCallback, useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import {
  getAccountStatus,
  getActiveTrades,
  getAutoTradeStatus,
} from '../services/api'
import { useWebSocket } from '../hooks/useWebSocket'

const WS_URL = `${import.meta.env.VITE_WS_URL || 'ws://localhost:8000'}/ws/trades`
const BALANCE_POLL_MS = 60000
const AUTO_TRADE_POLL_MS = 5000

const linkClass = ({ isActive }) =>
  `block border-b-2 px-3 py-2 text-sm font-medium ${
    isActive
      ? 'border-blue-500 text-blue-400'
      : 'border-transparent text-gray-400 hover:text-gray-200'
  }`

function formatBalanceUsd(value) {
  return Number(value || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function formatBalanceInr(value) {
  return Number(value || 0).toLocaleString('en-IN', {
    maximumFractionDigits: 0,
  })
}

export default function Navbar() {
  const { status: wsStatus } = useWebSocket(WS_URL)
  const [connected, setConnected] = useState(false)
  const [balanceUsd, setBalanceUsd] = useState(0)
  const [balanceInr, setBalanceInr] = useState(0)
  const [accountName, setAccountName] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  /** null | 'active' | 'waiting' */
  const [autoTradeDot, setAutoTradeDot] = useState(null)

  const refreshAccount = useCallback(async () => {
    try {
      const status = await getAccountStatus()
      setConnected(Boolean(status?.connected))
      if (status?.connected) {
        setBalanceUsd(Number(status?.balance_usdt || 0))
        setBalanceInr(Number(status?.balance_inr || 0))
        setAccountName(status?.account_name || '')
      }
      // On failure path below: keep last known balance (no crash)
    } catch {
      // Keep last known balance / name — do not zero out
    }
  }, [])

  const refreshAutoTradeDot = useCallback(async () => {
    try {
      const [status, activeRes] = await Promise.all([
        getAutoTradeStatus(),
        getActiveTrades().catch(() => ({ trades: [] })),
      ])
      if (!status?.is_enabled) {
        setAutoTradeDot(null)
        return
      }
      const und = String(status.underlying || '').toUpperCase()
      const trades = activeRes?.trades || []
      const hasActive = trades.some(
        (t) => String(t.underlying || '').toUpperCase() === und,
      )
      if (hasActive) {
        setAutoTradeDot('active')
        return
      }
      setAutoTradeDot('waiting')
    } catch {
      // Keep last indicator on failure
    }
  }, [])

  useEffect(() => {
    refreshAccount()
    const interval = setInterval(refreshAccount, BALANCE_POLL_MS)
    const onUpdate = () => refreshAccount()
    window.addEventListener('tradeict-account-updated', onUpdate)
    return () => {
      clearInterval(interval)
      window.removeEventListener('tradeict-account-updated', onUpdate)
    }
  }, [refreshAccount])

  useEffect(() => {
    refreshAutoTradeDot()
    const interval = setInterval(refreshAutoTradeDot, AUTO_TRADE_POLL_MS)
    return () => clearInterval(interval)
  }, [refreshAutoTradeDot])

  const wsDot =
    wsStatus === 'connected'
      ? 'bg-green-500'
      : wsStatus === 'connecting'
        ? 'bg-yellow-400'
        : 'bg-red-500'

  const wsLabel =
    wsStatus === 'connected'
      ? 'Connected'
      : wsStatus === 'connecting'
        ? 'Connecting...'
        : 'Disconnected'

  const autoDotClass =
    autoTradeDot === 'active'
      ? 'bg-green-500'
      : autoTradeDot === 'waiting'
        ? 'bg-yellow-400'
        : null

  const navLinks = (
    <>
      <NavLink to="/" className={linkClass} end onClick={() => setMenuOpen(false)}>
        Dashboard
      </NavLink>
      <NavLink
        to="/new-trade"
        className={linkClass}
        onClick={() => setMenuOpen(false)}
      >
        New Trade
      </NavLink>
      <NavLink
        to="/auto-trade"
        className={linkClass}
        onClick={() => setMenuOpen(false)}
      >
        <span className="inline-flex items-center gap-1.5">
          Auto Trade
          {autoDotClass ? (
            <span
              className={`inline-block h-2 w-2 rounded-full ${autoDotClass}`}
              aria-hidden
            />
          ) : null}
        </span>
      </NavLink>
      <NavLink to="/logs" className={linkClass} onClick={() => setMenuOpen(false)}>
        Logs
      </NavLink>
      <NavLink
        to="/accounts"
        className={linkClass}
        onClick={() => setMenuOpen(false)}
      >
        Accounts
      </NavLink>
      <NavLink
        to="/settings"
        className={linkClass}
        onClick={() => setMenuOpen(false)}
      >
        Settings
      </NavLink>
    </>
  )

  return (
    <header className="border-b border-gray-700 bg-gray-900">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
        <div className="flex items-center gap-4 md:gap-6">
          <div className="text-lg font-semibold text-white">🤖 Delta Bot</div>
          <nav className="hidden items-center gap-1 md:flex">{navLinks}</nav>
        </div>

        <div className="flex items-center gap-3">
          <div className="hidden items-center gap-2 text-sm text-gray-300 sm:flex">
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${wsDot}`} />
            {connected ? (
              <span title={accountName || undefined} className="text-green-400">
                {wsLabel} • ${formatBalanceUsd(balanceUsd)} · ₹
                {formatBalanceInr(balanceInr)}
              </span>
            ) : (
              <span>{wsLabel}</span>
            )}
          </div>

          <button
            type="button"
            className="rounded-md border border-gray-600 p-2 text-gray-300 hover:bg-gray-800 md:hidden"
            aria-label="Toggle menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((o) => !o)}
          >
            <span className="block h-0.5 w-5 bg-current" />
            <span className="mt-1 block h-0.5 w-5 bg-current" />
            <span className="mt-1 block h-0.5 w-5 bg-current" />
          </button>
        </div>
      </div>

      {menuOpen && (
        <nav className="border-t border-gray-800 px-4 py-2 md:hidden">
          <div className="flex flex-col gap-1">{navLinks}</div>
          <div className="mt-3 flex items-center gap-2 border-t border-gray-800 pt-3 text-sm text-gray-300">
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${wsDot}`} />
            {connected ? (
              <span className="text-green-400">
                {wsLabel} • ${formatBalanceUsd(balanceUsd)} · ₹
                {formatBalanceInr(balanceInr)}
              </span>
            ) : (
              <span>{wsLabel}</span>
            )}
          </div>
        </nav>
      )}
    </header>
  )
}
