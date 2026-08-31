export default function InfoTooltip({ text }) {
  if (!text) return null
  return (
    <div className="relative ml-1 inline-block group">
      <span className="cursor-help text-xs text-gray-500 hover:text-gray-300">
        ⓘ
      </span>
      <div className="absolute bottom-6 left-0 z-50 hidden w-64 rounded-lg border border-gray-600 bg-gray-800 p-3 text-xs text-gray-300 shadow-xl group-hover:block">
        {text}
      </div>
    </div>
  )
}
