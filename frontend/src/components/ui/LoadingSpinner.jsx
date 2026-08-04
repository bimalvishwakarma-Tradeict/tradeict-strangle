const SIZE_CLASS = {
  sm: 'h-4 w-4 border-2',
  md: 'h-6 w-6 border-2',
  lg: 'h-10 w-10 border-4',
}

const COLOR_CLASS = {
  blue: 'border-blue-500 border-t-transparent',
  white: 'border-white border-t-transparent',
  green: 'border-green-500 border-t-transparent',
  gray: 'border-gray-400 border-t-transparent',
}

export default function LoadingSpinner({ size = 'md', color = 'blue' }) {
  return (
    <span
      className={`inline-block animate-spin rounded-full ${SIZE_CLASS[size] || SIZE_CLASS.md} ${COLOR_CLASS[color] || COLOR_CLASS.blue}`}
      role="status"
      aria-label="Loading"
    />
  )
}
