import { useState, useRef, useEffect } from 'react'
import './App.css'
import Message from './components/Message'
import { API_URL, SUGGESTED_QUESTIONS } from './constants'

let nextId = 1
function newId() {
  return nextId++
}

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [apiError, setApiError] = useState(null)
  const listRef = useRef(null)

  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [messages, loading])

  async function sendMessage(text) {
    const trimmed = text.trim()
    if (!trimmed || loading) return

    setApiError(null)
    // The backend is stateless, so prior turns go back out with every request.
    const history = messages.map(({ role, content }) => ({ role, content }))
    setMessages((prev) => [...prev, { id: newId(), role: 'user', content: trimmed }])
    setInput('')
    setLoading(true)

    const endpoint = `${API_URL}/chat`
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: trimmed, history }),
      })
      if (!res.ok) {
        const bodyText = await res.text().catch(() => '')
        throw new Error(`API responded with ${res.status}${bodyText ? `: ${bodyText}` : ''}`)
      }
      const data = await res.json()
      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: 'assistant',
          content: data.reply,
          toolCalls: data.tool_calls || [],
          asOf: data.as_of,
        },
      ])
    } catch (err) {
      const message =
        err instanceof TypeError
          ? `Could not reach the API at ${endpoint}`
          : err.message || `Request to ${endpoint} failed`
      setApiError(message)
    } finally {
      setLoading(false)
    }
  }

  function handleSubmit(e) {
    e.preventDefault()
    sendMessage(input)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-title">
          <img src="/logo.png" alt="Airport Investment logo" className="app-logo" />
          <h1>Airport Investment Intelligence Agent</h1>
        </div>
        <p className="app-subtitle">API: {API_URL}</p>
      </header>

      <div className="message-list" ref={listRef}>
        {messages.length === 0 && (
          <div className="empty-state">
            Ask about airport expansion candidates, or try a suggested question below.
          </div>
        )}
        {messages.map((m) => (
          <Message key={m.id} message={m} />
        ))}
        {loading && (
          <div className="message-row from-assistant">
            <div className="message-bubble message-loading">Thinking&hellip;</div>
          </div>
        )}
      </div>

      {apiError && <div className="api-error">{apiError}</div>}

      <div className="suggested-questions">
        {SUGGESTED_QUESTIONS.map((q) => (
          <button
            key={q}
            type="button"
            className="suggested-question"
            onClick={() => setInput(q)}
            disabled={loading}
          >
            {q}
          </button>
        ))}
      </div>

      <form className="input-row" onSubmit={handleSubmit}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask about airport expansion candidates..."
          disabled={loading}
          rows={2}
        />
        <button type="submit" disabled={loading || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  )
}
