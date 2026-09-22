import type { SessionMeta, SessionStatus } from '../types/messages'

// Map internal thread_type to GitHub URL path segments
const THREAD_TYPE_URL_SEGMENTS: Record<string, string> = {
  pr: 'pull',
  issue: 'issues',
  discussion: 'discussions',
}

interface Props {
  meta: SessionMeta | null
  status: SessionStatus
  connected: boolean
  numTurns: number
  durationMs: number
}

const STATUS_LABEL: Record<SessionStatus, string> = {
  running: '⬤ Running',
  completed: '✔ Completed — type to resume',
  error: '✖ Error — type to retry',
  unknown: '○ Connecting…',
}

const STATUS_COLOR: Record<SessionStatus, string> = {
  running: '#4ade80',
  completed: '#60a5fa',
  error: '#f87171',
  unknown: '#94a3b8',
}

export function StatusBar({ meta, status, connected, numTurns, durationMs }: Props) {
  const color = STATUS_COLOR[status]
  const label = STATUS_LABEL[status]
  const ghSegment = meta ? (THREAD_TYPE_URL_SEGMENTS[meta.thread_type] || 'issues') : 'issues'

  return (
    <div className="status-bar">
      <div className="status-left">
        <span className="status-dot" style={{ color }}>{label}</span>
        {meta && (
          <span className="status-meta">
            <a
              href={`https://github.com/${meta.repo}/${ghSegment}/${meta.issue_number}`}
              target="_blank"
              rel="noreferrer"
              className="status-link"
            >
              {meta.repo}#{meta.issue_number}
            </a>
            <span className="status-workflow">{meta.workflow}</span>
          </span>
        )}
      </div>
      <div className="status-right">
        {numTurns > 0 && <span className="status-stat">{numTurns} turns</span>}
        {durationMs > 0 && (
          <span className="status-stat">{(durationMs / 1000).toFixed(1)}s</span>
        )}
        <span className={`status-ws ${connected ? 'ws-connected' : 'ws-disconnected'}`}>
          {connected ? 'WS ●' : 'WS ○'}
        </span>
      </div>
    </div>
  )
}
