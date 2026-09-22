import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface Props {
  content: string
  isStreaming?: boolean
}

export function AssistantMessage({ content, isStreaming }: Props) {
  return (
    <div className={`assistant-message ${isStreaming ? 'streaming' : ''}`}>
      <div className="message-role">Claude</div>
      <div className="message-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        {isStreaming && <span className="cursor-blink">▋</span>}
      </div>
    </div>
  )
}
