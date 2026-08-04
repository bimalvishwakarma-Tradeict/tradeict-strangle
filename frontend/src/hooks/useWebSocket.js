import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Auto-reconnecting WebSocket hook.
 * Ignores ping messages. Parses JSON automatically.
 *
 * @returns {{ lastMessage: object|null, status: 'connecting'|'connected'|'disconnected', sendMessage: Function }}
 */
export function useWebSocket(url) {
  const [lastMessage, setLastMessage] = useState(null)
  const [status, setStatus] = useState('connecting')
  const wsRef = useRef(null)
  const reconnectTimerRef = useRef(null)
  const unmountedRef = useRef(false)

  const clearReconnectTimer = () => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
  }

  const connect = useCallback(() => {
    if (!url || unmountedRef.current) return

    clearReconnectTimer()
    setStatus('connecting')

    try {
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        if (unmountedRef.current) {
          ws.close()
          return
        }
        setStatus('connected')
      }

      ws.onmessage = (event) => {
        if (unmountedRef.current) return
        try {
          const data = JSON.parse(event.data)
          if (data?.type === 'ping') {
            // Frontend ignores ping — connection stays alive
            return
          }
          setLastMessage(data)
        } catch {
          // ignore non-JSON payloads
        }
      }

      ws.onerror = () => {
        // onclose will handle reconnect
      }

      ws.onclose = () => {
        if (unmountedRef.current) return
        setStatus('disconnected')
        reconnectTimerRef.current = setTimeout(() => {
          connect()
        }, 3000)
      }
    } catch {
      setStatus('disconnected')
      reconnectTimerRef.current = setTimeout(() => {
        connect()
      }, 3000)
    }
  }, [url])

  useEffect(() => {
    unmountedRef.current = false
    connect()
    return () => {
      unmountedRef.current = true
      clearReconnectTimer()
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [connect])

  const sendMessage = useCallback((payload) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(
        typeof payload === 'string' ? payload : JSON.stringify(payload),
      )
    }
  }, [])

  return { lastMessage, status, sendMessage }
}
