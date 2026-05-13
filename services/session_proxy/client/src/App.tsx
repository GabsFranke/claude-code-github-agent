import { useCallback, useEffect, useRef, useState } from 'react'
import { useSessionSocket } from './hooks/useSessionSocket'
import { AssistantMessage } from './components/AssistantMessage'
import { ToolCall } from './components/ToolCall'
import { StatusBar } from './components/StatusBar'
import { MessageInput } from './components/MessageInput'
import type {
  SessionMeta,
  SessionPath,
  SessionStatus,
  WSMessage,
} from './types/messages'

const RESOLVE_POLL_MS = 3000

// ─── Parse human-readable URL ──────────────────────────────────────────────────

function parseSessionPath(): SessionPath | null {
  const parts = window.location.pathname.split('/').filter(Boolean)
  // /session/{owner}/{repo}/{type}/{number}/{workflow} → 6 parts: ["session", owner, repo, type, number, workflow]
  if (parts.length === 6 && parts[0] === 'session') {
    return {
      owner: parts[1],
      repo: parts[2],
      threadType: parts[3],
      number: parts[4],
      workflow: parts[5],
    }
  }
  return null
}

const SESSION_PATH = parseSessionPath()

// ─── Resolve API call ──────────────────────────────────────────────────────────

async function fetchSessionResolve(
  path: SessionPath,
): Promise<{ status: 'found'; token: string; session: SessionMeta } | { status: 'pending'; message?: string }> {
  const { owner, repo, threadType, number, workflow } = path
  const res = await fetch(
    `/api/resolve/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/${encodeURIComponent(threadType)}/${number}/${encodeURIComponent(workflow)}`,
  )
  return res.json()
}

// ─── Log entry types ──────────────────────────────────────────────────────────

type LogEntry =
  | { id: string; kind: 'assistant'; text: string; streaming: boolean }
  | { id: string; kind: 'tool'; name: string; input: Record<string, unknown>; result?: string; isError?: boolean }
  | { id: string; kind: 'error'; message: string }
  | { id: string; kind: 'user'; text: string }
  | { id: string; kind: 'run_boundary'; runNumber: number }
  | { id: string; kind: 'thinking' }

function uid() {
  return Math.random().toString(36).slice(2)
}

export function App() {
  const [meta, setMeta] = useState<SessionMeta | null>(null)
  const [status, setStatus] = useState<SessionStatus>('unknown')
  const [log, setLog] = useState<LogEntry[]>([])
  const [numTurns, setNumTurns] = useState(0)
  const [durationMs, setDurationMs] = useState(0)
  const [resolving, setResolving] = useState(true)
  const [resolveError, setResolveError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Current streaming state
  const streamingIdRef = useRef<string | null>(null)
  const streamingToolRef = useRef<{ id: string; partialJson: string } | null>(null)
  const didStreamRef = useRef(false)

  const handleMessage = useCallback((msg: WSMessage) => {
    switch (msg.type) {
      case 'session_meta':
        setMeta(msg.data)
        setStatus(msg.data.status)
        break

      case 'session_init':
        setStatus('running')
        break

      case 'stream_event': {
        const event = msg.data.event as Record<string, unknown>
        const eventType = event['type'] as string
        if (eventType === 'content_block_start') {
          didStreamRef.current = true
          const cb = event['content_block'] as Record<string, unknown>
          if (cb?.['type'] === 'text') {
            const id = uid()
            streamingIdRef.current = id
            setLog((l) => [...l, { id, kind: 'assistant', text: '', streaming: true }])
          } else if (cb?.['type'] === 'thinking') {
            setLog((l) => [...l, { id: uid(), kind: 'thinking' }])
          } else if (cb?.['type'] === 'tool_use') {
            const id = uid()
            streamingToolRef.current = { id, partialJson: '' }
            setLog((l) => [
              ...l,
              { id, kind: 'tool', name: (cb['name'] as string) || '', input: {} },
            ])
          }
        } else if (eventType === 'content_block_delta') {
          const delta = event['delta'] as Record<string, unknown>
          if (delta?.['type'] === 'text_delta' && streamingIdRef.current) {
            const chunk = (delta['text'] as string) ?? ''
            const id = streamingIdRef.current
            setLog((l) =>
              l.map((e) =>
                e.id === id && e.kind === 'assistant'
                  ? { ...e, text: e.text + chunk }
                  : e,
              ),
            )
          } else if (delta?.['type'] === 'input_json_delta' && streamingToolRef.current) {
            streamingToolRef.current.partialJson += (delta['partial_json'] as string) ?? ''
          }
        } else if (eventType === 'content_block_stop') {
          if (streamingIdRef.current) {
            const id = streamingIdRef.current
            setLog((l) =>
              l.map((e) =>
                e.id === id && e.kind === 'assistant' ? { ...e, streaming: false } : e,
              ),
            )
            streamingIdRef.current = null
          }
          if (streamingToolRef.current) {
            const { id, partialJson } = streamingToolRef.current
            try {
              const input = JSON.parse(partialJson)
              setLog((l) =>
                l.map((e) =>
                  e.id === id && e.kind === 'tool' ? { ...e, input } : e,
                ),
              )
            } catch {
              // partial JSON may not parse — keep the empty input
            }
            streamingToolRef.current = null
          }
          // Remove thinking indicators when a content block stops
          setLog((l) => l.filter((e) => e.kind !== 'thinking'))
        }
        break
      }

      case 'assistant_message': {
        // If we already streamed this content via stream events, skip
        // to avoid duplicate entries
        if (didStreamRef.current) {
          didStreamRef.current = false
          break
        }
        for (const block of msg.data.content) {
          if (block.type === 'text' && block.text) {
            setLog((l) => [
              ...l,
              { id: uid(), kind: 'assistant', text: block.text!, streaming: false },
            ])
          } else if (block.type === 'tool_use' && block.name) {
            setLog((l) => [
              ...l,
              { id: uid(), kind: 'tool', name: block.name!, input: block.input ?? {} },
            ])
          }
          // Skip 'thinking' blocks — not useful for display
        }
        break
      }

      case 'result':
        setStatus(msg.data.is_error ? 'error' : 'completed')
        setNumTurns(msg.data.num_turns)
        setDurationMs(msg.data.duration_ms)
        break

      case 'session_closed':
        setStatus((s) => (s === 'running' ? 'completed' : s))
        break

      case 'session_error':
        setStatus('error')
        setLog((l) => [
          ...l,
          { id: uid(), kind: 'error', message: msg.data.error },
        ])
        break

      case 'user_message':
        // Deduplicate: skip if this content already appears as the last user message
        // (optimistic local render may have already added it)
        setLog((l) => {
          const content = msg.data.content
          const last = l[l.length - 1]
          if (last && last.kind === 'user' && last.text === content) {
            return l // Already shown optimistically
          }
          return [...l, { id: uid(), kind: 'user' as const, text: content }]
        })
        break

      case 'run_start':
        setStatus('running')
        setLog((l) => [
          ...l,
          { id: uid(), kind: 'run_boundary', runNumber: msg.data.run_number },
        ])
        break
    }
  }, [])

  // ─── WebSocket hook (must be called unconditionally — React Rules of Hooks) ──
  // Pass null until resolve succeeds so the hook doesn't connect prematurely.
  const [wsPath, setWsPath] = useState<SessionPath | null>(null)
  const { connected, send } = useSessionSocket(wsPath, handleMessage)

  // ─── Resolve polling ────────────────────────────────────────────────────────

  // Poll /api/resolve until a session is found, then enable the WebSocket
  const resolvedRef = useRef(false)

  useEffect(() => {
    if (!SESSION_PATH) {
      setResolving(false)
      return
    }

    let cancelled = false

    async function poll() {
      while (!cancelled && !resolvedRef.current) {
        try {
          const data = await fetchSessionResolve(SESSION_PATH!)
          if (!cancelled && data.status === 'found') {
            resolvedRef.current = true
            setResolving(false)
            setResolveError(null)
            if (data.session) {
              setMeta(data.session)
              setStatus(data.session.status)
            }
            // Enable WebSocket now that the session is confirmed to exist
            setWsPath(SESSION_PATH)
            return
          }
          // Still pending — wait and retry
          const pendingData = data as { status: 'pending'; message?: string }
          setResolveError(pendingData.message ?? 'Waiting for session…')
        } catch {
          if (!cancelled) {
            setResolveError('Failed to reach session proxy. Retrying…')
          }
        }
        await new Promise((r) => setTimeout(r, RESOLVE_POLL_MS))
      }
    }

    poll()

    return () => {
      cancelled = true
    }
  }, [])

  // Auto-scroll to bottom as new messages arrive
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [log])

  // ─── Invalid URL ─────────────────────────────────────────────────────────────

  if (!SESSION_PATH) {
    return (
      <div className="app">
        <div className="empty-state">
          Invalid session URL.
          <br />
          Open <code>/session/{'{{owner}'}/{'{{repo}'}/{'{{type}'}/{'{{issue}'}/{'{{workflow}}'}</code> to view a streaming session.
        </div>
      </div>
    )
  }

  // ─── Waiting for session ─────────────────────────────────────────────────────

  if (resolving) {
    return (
      <div className="app">
        <div className="empty-state">
          {resolveError ?? 'Connecting…'}
        </div>
      </div>
    )
  }

  // ─── Connected ───────────────────────────────────────────────────────────────

  const canSend = !resolving

  return (
    <div className="app">
      <StatusBar
        meta={meta}
        status={status}
        connected={connected}
        numTurns={numTurns}
        durationMs={durationMs}
      />
      <main className="log-container">
        {log.length === 0 && (
          <div className="empty-state">
            {connected ? 'Waiting for agent output…' : 'Connecting…'}
          </div>
        )}
        {log.map((entry) => {
          if (entry.kind === 'assistant') {
            return (
              <AssistantMessage
                key={entry.id}
                content={entry.text}
                isStreaming={entry.streaming}
              />
            )
          }
          if (entry.kind === 'tool') {
            return (
              <ToolCall
                key={entry.id}
                toolName={entry.name}
                toolInput={entry.input}
                toolResult={entry.result}
                isError={entry.isError}
              />
            )
          }
          if (entry.kind === 'thinking') {
            return (
              <div key={entry.id} className="thinking-indicator">
                <span className="thinking-label">Thinking</span>
                <span className="thinking-dots">
                  <span className="dot" />
                  <span className="dot" />
                  <span className="dot" />
                </span>
              </div>
            )
          }
          if (entry.kind === 'error') {
            return (
              <div key={entry.id} className="log-error">
                ✖ {entry.message}
              </div>
            )
          }
          if (entry.kind === 'user') {
            return (
              <div key={entry.id} className="user-message">
                <div className="message-role">You</div>
                <div className="message-content">{entry.text}</div>
              </div>
            )
          }
          if (entry.kind === 'run_boundary') {
            return (
              <div key={entry.id} className="run-boundary">
                <span>Run #{entry.runNumber}</span>
              </div>
            )
          }
          return null
        })}
        <div ref={bottomRef} />
      </main>
      <MessageInput
        onSend={handleSendMessage}
        onStop={handleStopAgent}
        disabled={!canSend}
        status={status}
      />
    </div>
  )

  function handleSendMessage(content: string) {
    send({ type: 'inject_message', content })
    // Optimistically show the user's message immediately so it's visible
    // even if the server doesn't echo it back (e.g. stuck session, no worker)
    setLog((l) => [...l, { id: uid(), kind: 'user' as const, text: content }])
  }

  function handleStopAgent() {
    send({ type: 'stop_agent', content: '' })
  }
}
