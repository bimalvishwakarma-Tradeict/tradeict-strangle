import { useEffect } from 'react'

const TYPE_STYLES = {
  success: 'border-green-600 bg-green-900/90 text-green-100',
  error: 'border-red-600 bg-red-900/90 text-red-100',
  info: 'border-blue-600 bg-blue-900/90 text-blue-100',
}

export default function Toast({ message, type = 'info', onClose }) {
  useEffect(() => {
    if (!message) return undefined
    const timer = setTimeout(() => {
      onClose?.()
    }, 4000)
    return () => clearTimeout(timer)
  }, [message, onClose])

  if (!message) return null

  return (
    <div
      className={`fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border px-4 py-3 shadow-lg transition-all duration-300 ${TYPE_STYLES[type] || TYPE_STYLES.info}`}
      style={{ animation: 'toast-slide-in 0.25s ease-out' }}
    >
      <div className="flex items-start gap-3">
        <p className="flex-1 text-sm">{message}</p>
        <button
          type="button"
          onClick={onClose}
          className="text-sm opacity-70 hover:opacity-100"
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <style>{`
        @keyframes toast-slide-in {
          from { transform: translateX(120%); opacity: 0; }
          to { transform: translateX(0); opacity: 1; }
        }
      `}</style>
    </div>
  )
}
