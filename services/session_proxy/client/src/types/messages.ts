/** WebSocket message types from session_proxy / Redis. */

export type SessionStatus = 'running' | 'completed' | 'error' | 'unknown'

export interface SessionMeta {
  token: string
  repo: string
  issue_number: string
  workflow: string
  thread_type: string
  status: SessionStatus
  session_proxy_url?: string
}

// ─── Message payloads ────────────────────────────────────────────────────────

export interface StreamEventData {
  event: Record<string, unknown>
  session_id: string
}

export interface AssistantMessageData {
  content: ContentBlock[]
}

export interface ContentBlock {
  type: 'text' | 'tool_use' | 'thinking' | string
  text?: string
  id?: string
  name?: string
  input?: Record<string, unknown>
}

export interface ResultData {
  num_turns: number
  duration_ms: number
  is_error: boolean
  session_id: string | null
  subtype: string | null
}

export interface SessionInitData {
  repo: string
  issue_number: number
  workflow: string
}

export interface SessionErrorData {
  error: string
}

export interface UserMessageData {
  content: string
}

export interface RunStartData {
  run_number: number
  session_id: string | null
}

// ─── Discriminated union ─────────────────────────────────────────────────────

export type WSMessage =
  | { type: 'session_meta'; data: SessionMeta; ts: string }
  | { type: 'session_init'; data: SessionInitData; ts: string }
  | { type: 'stream_event'; data: StreamEventData; ts: string }
  | { type: 'assistant_message'; data: AssistantMessageData; ts: string }
  | { type: 'result'; data: ResultData; ts: string }
  | { type: 'session_closed'; data: Record<string, never>; ts: string }
  | { type: 'session_error'; data: SessionErrorData; ts: string }
  | { type: 'user_message'; data: UserMessageData; ts: string }
  | { type: 'run_start'; data: RunStartData; ts: string }

// ─── Resolve API response ────────────────────────────────────────────────────

export interface ResolveResponse {
  status: 'found' | 'pending'
  token?: string
  session?: SessionMeta
  message?: string
}

// ─── Session path (human-readable URL) ────────────────────────────────────────

export interface SessionPath {
  owner: string
  repo: string
  threadType: string
  number: string
  workflow: string
}

// ─── Control messages (browser → server) ────────────────────────────────────

export interface InjectMessageControl {
  type: 'inject_message'
  content: string
}
