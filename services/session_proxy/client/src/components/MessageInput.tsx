import { useState, type KeyboardEvent } from 'react'
import type { SessionStatus } from '../types/messages'

interface Props {
  onSend: (content: string) => void
  disabled: boolean
  status: SessionStatus
}

const PLACEHOLDER: Record<SessionStatus, string> = {
  running: 'Send a message…',
  completed: 'Type a message to resume the agent…',
  error: 'Type a message to retry…',
  unknown: 'Connecting…',
}

export function MessageInput({ onSend, disabled, status }: Props) {
  const [text, setText] = useState('')

  function submit() {
    const trimmed = text.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setText('')
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="message-input-bar">
      <input
        className="message-input"
        type="text"
        placeholder={PLACEHOLDER[status]}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={disabled}
      />
      <button
        className="btn-send"
        onClick={submit}
        disabled={disabled || !text.trim()}
      >
        Send
      </button>
    </div>
  )
}
