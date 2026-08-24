import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'

/**
 * Subscribes to the server's broadcast topic.
 *
 * Messages carry the changed record and freshly computed statistics, so the
 * caller patches local state rather than refetching the table. Reconnects with
 * a capped backoff if the socket drops.
 */
export function useLiveUpdates(onMessage, enabled = true) {
  const [connected, setConnected] = useState(false)
  const [clients, setClients] = useState(0)
  const handler = useRef(onMessage)
  handler.current = onMessage

  useEffect(() => {
    let socket
    let retry
    let attempts = 0
    let closed = false

    const open = async () => {
      if (closed) return
      let ticket
      try {
        ticket = (await api.wsTicket()).ticket
      } catch {
        // Not signed in, or the session expired. Retry with backoff.
        attempts += 1
        retry = setTimeout(open, Math.min(1000 * attempts, 8000))
        return
      }
      if (closed) return

      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`)

      socket.onopen = () => { attempts = 0; setConnected(true) }
      socket.onclose = () => {
        setConnected(false)
        if (closed) return
        attempts += 1
        retry = setTimeout(open, Math.min(1000 * attempts, 8000))
      }
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data)
        if (message.type === 'presence') setClients(message.clients)
        handler.current?.(message)
      }
    }

    if (enabled) open()
    return () => { closed = true; clearTimeout(retry); socket?.close() }
  }, [enabled])

  return { connected, clients }
}
