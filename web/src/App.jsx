import { useState, useRef, useEffect, useCallback } from 'react'
import './App.css'
import Message from './components/Message'
import ThinkingIndicator from './components/ThinkingIndicator'
import {
  API_URL,
  HEALTH_TIMEOUT_MS,
  MAX_MESSAGE_CHARS,
  REQUEST_TIMEOUT_MS,
  SUGGESTED_QUESTIONS,
} from './constants'

export default function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [apiError, setApiError] = useState(null)
  // 'checking' | 'up' | 'down' -- resolved on load, so an unreachable backend
  // is visible before the user types a question rather than after.
  const [backend, setBackend] = useState('checking')
  const listRef = useRef(null)
  // Only autoscroll when the reader is already at the bottom -- otherwise
  // scrolling up to re-read an earlier answer gets yanked back down.
  const pinnedToBottomRef = useRef(true)
  const abortRef = useRef(null)
  const timedOutRef = useRef(false)

  // No synchronous setState here: `backend` already starts at 'checking', so
  // the probe only reports its outcome. Retry resets the state itself.
  const probeHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/health`, {
        signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
      })
      setBackend(res.ok ? 'up' : 'down')
    } catch {
      setBackend('down')
    }
  }, [])

  useEffect(() => {
    // Deliberate: probing the backend on load is synchronising with an
    // external system, which is what an effect is for. The setState happens
    // after the await, never synchronously, but the rule cannot see that.
    // oxlint-disable-next-line react/set-state-in-effect
    probeHealth()
  }, [probeHealth])

  function retryHealth() {
    setBackend('checking')
    probeHealth()
  }

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
      setBackend('up')
    } catch (err) {
      if (err.name === 'AbortError') {
        // A manual Stop is not an error; a timeout is.
        if (timedOutRef.current) {
          setApiError(`No response from ${endpoint} within ${REQUEST_TIMEOUT_MS / 1000}s. It may still be working -- try again.`)
        }
      } else if (err instanceof TypeError) {
        // Network-level failure: the backend went away mid-session.
        setBackend('down')
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

  const backendDown = backend === 'down'
  const composerDisabled = loading || backendDown

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

      {backendDown && (
        <div className="api-error backend-down" role="alert">
          <div>
            Can&apos;t reach the backend at <code>{API_URL}</code>. Start it with{' '}
            <code>python -m uvicorn api.main:app --port 8000</code>.
          </div>
          <button type="button" className="retry-button" onClick={retryHealth}>
            Retry
          </button>
        </div>
      )}

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
              disabled={composerDisabled}
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
          placeholder={backendDown ? 'Backend unavailable' : 'Ask about airport expansion candidates...'}
          aria-label="Ask about airport expansion candidates"
          maxLength={MAX_MESSAGE_CHARS}
          disabled={backendDown}
          rows={2}
        />
        {loading ? (
          <button type="button" className="stop-button" onClick={stopRequest}>
            Stop
          </button>
        ) : (
          <button type="submit" disabled={composerDisabled || !input.trim()}>
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
