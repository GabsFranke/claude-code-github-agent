import { useEffect, useRef, useCallback, useState } from 'react'
import type { WSMessage, SessionPath } from '../types/messages'

type MessageHandler = (msg: WSMessage) => void

const RECONNECT_DELAY_MS = 2000
const MAX_RECONNECT_ATTEMPTS = 20

export function useSessionSocket(
  sessionPath: SessionPath | null,
  onMessage: MessageHandler,
) {
  const ws = useRef<WebSocket | null>(null)
  const attemptRef = useRef(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [connected, setConnected] = useState(false)
  const onMessageRef = useRef(onMessage)
  onMessageRef.current = onMessage

  const connect = useCallback(() => {
    if (!sessionPath) return
    if (ws.current?.readyState === WebSocket.OPEN) return

    const { owner, repo, threadType, number, workflow } = sessionPath
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/${owner}/${repo}/${threadType}/${number}/${workflow}`
    const socket = new WebSocket(url)

    socket.onopen = () => {
      setConnected(true)
      attemptRef.current = 0
    }

    socket.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data) as WSMessage
        onMessageRef.current(msg)
      } catch {
        console.warn('[WS] Failed to parse message', ev.data)
      }
    }

    socket.onclose = () => {
      setConnected(false)
      ws.current = null
      if (attemptRef.current < MAX_RECONNECT_ATTEMPTS) {
        const delay = RECONNECT_DELAY_MS * Math.min(attemptRef.current + 1, 5)
        attemptRef.current++
        timerRef.current = setTimeout(connect, delay)
      }
    }

    socket.onerror = () => {
      socket.close()
    }

    ws.current = socket
  }, [sessionPath])

  // Send a control message to the server
  const send = useCallback((msg: object) => {
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify(msg))
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
      ws.current?.close()
    }
  }, [connect])

  return { connected, send }
}
