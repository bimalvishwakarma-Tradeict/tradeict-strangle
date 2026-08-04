import { Component } from 'react'

/**
 * React error boundary — must be a class component.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error, info) {
    // Surface in console for debugging; never crash the shell silently
    console.error('ErrorBoundary caught:', error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex min-h-screen items-center justify-center bg-gray-900 px-4">
          <div className="max-w-md rounded-xl border border-red-700/50 bg-gray-800 p-6 text-center">
            <p className="text-lg font-semibold text-white">Something went wrong.</p>
            <p className="mt-2 text-sm text-gray-400">Refresh the page.</p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="mt-4 rounded-md bg-blue-500 px-4 py-2 text-sm font-medium text-white hover:bg-blue-400"
            >
              Refresh
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
