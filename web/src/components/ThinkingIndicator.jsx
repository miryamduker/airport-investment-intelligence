import { useEffect, useState } from 'react'

// Cycles "Thinking." -> "Thinking.." -> "Thinking..." so a slow tool call
// doesn't read as a stuck/frozen UI.
export default function ThinkingIndicator() {
  const [dots, setDots] = useState(1)

  useEffect(() => {
    const id = setInterval(() => {
      setDots((d) => (d % 3) + 1)
    }, 450)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="message-row from-assistant">
      <div className="message-bubble message-loading">
        Thinking
        <span className="thinking-dots" aria-hidden="true">
          {[1, 2, 3].map((i) => (
            <span key={i} className={i <= dots ? 'dot dot-on' : 'dot'}>
              .
            </span>
          ))}
        </span>
      </div>
    </div>
  )
}
