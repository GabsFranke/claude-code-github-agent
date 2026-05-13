import { useState } from 'react'

interface Props {
  toolName: string
  toolInput: Record<string, unknown>
  toolResult?: string
  isError?: boolean
}

export function ToolCall({ toolName, toolInput, toolResult, isError }: Props) {
  const [open, setOpen] = useState(false)
  const inputStr = JSON.stringify(toolInput, null, 2)
  const preview = inputStr.length > 120 ? inputStr.slice(0, 117) + '…' : inputStr

  return (
    <div className={`tool-call ${isError ? 'tool-error' : ''}`}>
      <button className="tool-header" onClick={() => setOpen((o) => !o)}>
        <span className="tool-icon">{isError ? '✖' : '⚙'}</span>
        <span className="tool-name">{toolName}</span>
        <span className="tool-preview">{!open && preview}</span>
        <span className="tool-chevron">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="tool-body">
          <div className="tool-section-label">Input</div>
          <pre className="tool-code">{inputStr}</pre>
          {toolResult !== undefined && (
            <>
              <div className={`tool-section-label ${isError ? 'label-error' : 'label-result'}`}>
                {isError ? 'Error' : 'Result'}
              </div>
              <pre className={`tool-code ${isError ? 'result-error' : 'result-ok'}`}>
                {toolResult}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  )
}
