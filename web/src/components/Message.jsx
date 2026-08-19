import ReactMarkdown from 'react-markdown'
import ReasoningPanel from './ReasoningPanel'

export default function Message({ message }) {
  const isUser = message.role === 'user'
  return (
    <div className={`message-row ${isUser ? 'from-user' : 'from-assistant'}`}>
      <div className="message-bubble">
        <div className="message-content">
          {isUser ? (
            message.content
          ) : (
            <div className="markdown-content">
              <ReactMarkdown>{message.content}</ReactMarkdown>
            </div>
          )}
        </div>
        {!isUser && message.asOf && message.toolCalls?.length > 0 && (
          <div className="message-as-of">as of {message.asOf}</div>
        )}
        {!isUser && <ReasoningPanel toolCalls={message.toolCalls} />}
      </div>
    </div>
  )
}
