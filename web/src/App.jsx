import { useState, useRef, useEffect } from 'react'
import './App.css'
import Message from './components/Message'
import ThinkingIndicator from './components/ThinkingIndicator'
import { API_URL, MAX_MESSAGE_CHARS, REQUEST_TIMEOUT_MS, SUGGESTED_QUESTIONS } from './constants'

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [apiError, setApiError] = useState(null)
  const listRef = useRef(null)
  // Only autoscroll when the reader is already at the bottom -- otherwise
  // scrolling up to re-read an earlier answer gets yanked back down.
  const pinnedToBottomRef = useRef(true)
  const abortRef = useRef(null)
  const timedOutRef = useRef(false)

  useEffect(() => {
    if (pinnedToBottomRef.current && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [messages, loading])

  function handleListScroll() {
    const el = listRef.current
    if (!el) return
    pinnedToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }

  function stopRequest() {
    abortRef.current?.abort()
  }

  async function sendMessage(text) {
    const trimmed = text.trim()
    if (!trimmed || loading) return
    if (trimmed.length > MAX_MESSAGE_CHARS) {
      setApiError(`Message is too long (${trimmed.length} of ${MAX_MESSAGE_CHARS} characters).`)
      return
    }

    setApiError(null)
    // The backend is stateless, so prior turns go back out with every request.
    const history = messages.map(({ role, content }) => ({ role, content }))
    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: 'user', content: trimmed }])
    setInput('')
    setLoading(true)
    pinnedToBottomRef.current = true

    const controller = new AbortController()
    abortRef.current = controller
    timedOutRef.current = false
    const timeoutId = setTimeout(() => {
      timedOutRef.current = true
      controller.abort()
    }, REQUEST_TIMEOUT_MS)

    const endpoint = `${API_URL}/chat`
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: trimmed, history }),
        signal: controller.signal,
      })
      if (!res.ok) {
        throw new Error(await readErrorDetail(res))
      }
      const data = await res.json()
      setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: 'assistant', content: data.reply }])
    } catch (err) {
      if (err.name === 'AbortError') {
        // A manual Stop is not an error; a timeout is.
        if (timedOutRef.current) {
          setApiError(`No response from ${endpoint} within ${REQUEST_TIMEOUT_MS / 1000}s. It may still be working -- try again.`)
        }
      } else if (err instanceof TypeError) {
        setApiError(`Could not reach the API at ${endpoint}. Is the backend running?`)
      } else {
        setApiError(err.message || `Request to ${endpoint} failed`)
      }
    } finally {
      clearTimeout(timeoutId)
      abortRef.current = null
      setLoading(false)
    }
  }

  function handleSubmit(e) {
    e.preventDefault()
    sendMessage(input)
  }

  function handleKeyDown(e) {
    // isComposing guards IME input: Enter mid-composition commits a candidate,
    // it does not mean "send".
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      sendMessage(input)
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-title">
          <img src="/logo.png" alt="" className="app-logo" />
          <h1>Airport Investment Intelligence Agent</h1>
        </div>
      </header>

      <div
        className="message-list"
        ref={listRef}
        onScroll={handleListScroll}
        role="log"
        aria-live="polite"
        aria-label="Conversation"
      >
        {messages.map((m) => (
          <Message key={m.id} message={m} />
        ))}
        {loading && <ThinkingIndicator />}
      </div>

      {apiError && (
        <div className="api-error" role="alert">
          {apiError}
        </div>
      )}

      {messages.length === 0 && (
        <div className="suggested-questions">
          {SUGGESTED_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              className="suggested-question"
              onClick={() => sendMessage(q)}
              disabled={loading}
            >
              {q}
            </button>
          ))}
        </div>
      )}

      <form className="input-row" onSubmit={handleSubmit}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask about airport expansion candidates..."
          aria-label="Ask about airport expansion candidates"
          maxLength={MAX_MESSAGE_CHARS}
          rows={2}
        />
        {loading ? (
          <button type="button" className="stop-button" onClick={stopRequest}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={!input.trim()}>
            Send
          </button>
        )}
      </form>
    </div>
  )
}

// The API returns {"detail": "..."} for a handled upstream failure; fall back
// to raw text for anything else.
async function readErrorDetail(res) {
  const bodyText = await res.text().catch(() => '')
  try {
    const parsed = JSON.parse(bodyText)
    if (parsed?.detail) return typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail)
  } catch {
    // not JSON -- fall through
  }
  return `API responded with ${res.status}${bodyText ? `: ${bodyText}` : ''}`
}
