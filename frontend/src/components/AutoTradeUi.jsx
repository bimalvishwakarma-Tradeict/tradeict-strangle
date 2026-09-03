import InfoTooltip from './InfoTooltip'
import LoadingSpinner from './ui/LoadingSpinner'

export const NAV_SECTIONS = [
  { id: 'trade-setup', label: 'Trade Setup' },
  { id: 'basket-sizing', label: 'Basket Sizing' },
  { id: 'adjustment-trigger', label: 'Adjustment' },
  { id: 'adjustment-behaviour', label: 'Adjustment' },
  { id: 'risk-target', label: 'Risk & Target' },
  { id: 'basket-wings', label: 'Wings' },
  { id: 'hedge-mode', label: 'Hedge' },
  { id: 'strike-selection', label: 'Strike Selection' },
  { id: 'advanced', label: 'Advanced' },
]

/** Dedupe nav labels for display — Adjustment appears once in tab bar */
export const NAV_TABS = [
  { id: 'trade-setup', label: 'Trade Setup' },
  { id: 'basket-sizing', label: 'Basket Sizing' },
  { id: 'adjustment-trigger', label: 'Adjustment' },
  { id: 'risk-target', label: 'Risk & Target' },
  { id: 'basket-wings', label: 'Wings' },
  { id: 'hedge-mode', label: 'Hedge' },
  { id: 'advanced', label: 'Advanced' },
]

export function scrollToSection(id) {
  const el = document.getElementById(id)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

export function SectionCard({
  id,
  icon,
  title,
  children,
  className = '',
  accent = false,
}) {
  return (
    <div
      id={id}
      className={`scroll-mt-36 space-y-4 rounded-xl border bg-gray-800 p-5 ${
        accent ? 'border-purple-900' : 'border-gray-700'
      } ${className}`}
    >
      <h3 className="flex items-center gap-2 text-sm font-semibold uppercase tracking-wider text-gray-300">
        {icon ? <span aria-hidden>{icon}</span> : null}
        {title}
      </h3>
      {children}
    </div>
  )
}

export function FieldLabel({ children, tooltip, className = '', as = 'label' }) {
  const Tag = as
  return (
    <Tag className={`text-sm font-medium text-white ${className}`}>
      {children}
      {tooltip ? <InfoTooltip text={tooltip} /> : null}
    </Tag>
  )
}

export function SectionDivider({ children }) {
  return (
    <div className="border-t border-gray-700 pt-4">
      <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-gray-400">
        {children}
      </p>
    </div>
  )
}

export function AutoTradeStickyHeader({
  isEnabled,
  statusText,
  saving,
  toggling,
  onSave,
  onEnable,
  onDisable,
}) {
  return (
    <div className="sticky top-14 z-20 flex flex-wrap items-center justify-between gap-3 border-b border-gray-700 bg-gray-900 px-4 py-3 sm:px-6">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold text-white">⚙ Auto Trade Settings</h1>
          <span
            className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold ${
              isEnabled
                ? 'bg-green-900/50 text-green-400'
                : 'bg-gray-800 text-gray-400'
            }`}
          >
            <span
              className={`inline-block h-1.5 w-1.5 rounded-full ${
                isEnabled ? 'bg-green-400' : 'bg-gray-500'
              }`}
              aria-hidden
            />
            {isEnabled ? statusText : 'Disabled'}
          </span>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={saving || toggling}
          onClick={onSave}
          className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3 py-2 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {saving ? <LoadingSpinner size="sm" /> : null}
          💾 Save Settings
        </button>
        {!isEnabled ? (
          <button
            type="button"
            disabled={saving || toggling}
            onClick={onEnable}
            className="inline-flex items-center gap-2 rounded-lg bg-green-600 px-3 py-2 text-sm font-semibold text-white hover:bg-green-500 disabled:opacity-50"
          >
            {toggling ? <LoadingSpinner size="sm" /> : null}
            Enable
          </button>
        ) : (
          <button
            type="button"
            disabled={saving || toggling}
            onClick={onDisable}
            className="inline-flex items-center gap-2 rounded-lg bg-red-600 px-3 py-2 text-sm font-semibold text-white hover:bg-red-500 disabled:opacity-50"
          >
            ■ Disable
          </button>
        )}
      </div>
    </div>
  )
}

export function AutoTradeStickyNav() {
  return (
    <nav className="sticky top-[7.25rem] z-10 overflow-x-auto border-b border-gray-700 bg-gray-900 px-4 sm:px-6">
      <div className="flex min-w-max gap-1 py-2">
        {NAV_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => scrollToSection(tab.id)}
            className="whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium text-gray-400 transition-colors hover:bg-gray-800 hover:text-white"
          >
            {tab.label}
          </button>
        ))}
      </div>
    </nav>
  )
}
