import { useEffect, useState } from 'react'

interface Props {
  toolName: string
  toolUseId: string
  toolInput: Record<string, unknown>
  timeoutSecs: number
  onDecision: (toolUseId: string, approved: boolean) => void
}

export function ToolApproval({ toolName, toolUseId, toolInput, timeoutSecs, onDecision }: Props) {
  const [remaining, setRemaining] = useState(timeoutSecs)
  const [decided, setDecided] = useState(false)

  useEffect(() => {
    if (decided) return
    const interval = setInterval(() => {
      setRemaining((r) => {
        if (r <= 1) {
          clearInterval(interval)
          // Auto-approve on timeout (matches server behaviour)
          onDecision(toolUseId, true)
          setDecided(true)
          return 0
        }
        return r - 1
      })
    }, 1000)
    return () => clearInterval(interval)
  }, [decided, toolUseId, onDecision])

  function decide(approved: boolean) {
    if (decided) return
    setDecided(true)
    onDecision(toolUseId, approved)
  }

  return (
    <div className="tool-approval">
      <div className="approval-header">
        <span className="approval-icon">🛑</span>
        <span className="approval-title">Tool call requires approval</span>
        <span className="approval-timer">{remaining}s</span>
      </div>
      <div className="approval-tool">
        <span className="approval-tool-name">{toolName}</span>
      </div>
      <pre className="approval-input">{JSON.stringify(toolInput, null, 2)}</pre>
      {!decided ? (
        <div className="approval-buttons">
          <button className="btn-approve" onClick={() => decide(true)}>
            ✔ Allow
          </button>
          <button className="btn-deny" onClick={() => decide(false)}>
            ✖ Deny
          </button>
        </div>
      ) : (
        <div className="approval-decided">Decision sent</div>
      )}
    </div>
  )
}
