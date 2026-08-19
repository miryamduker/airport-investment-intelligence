// The collapsed panel under each assistant reply: every tool call that
// produced it, with its raw JSON result. This is what makes "the model never
// produces a number" checkable rather than merely claimed.

function ToolMeta({ result }) {
  if (!result || typeof result !== 'object') return null
  const hasAsOf = result.as_of != null
  const hasConfidence = result.confidence && typeof result.confidence === 'object'
  const hasCaveats = Array.isArray(result.caveats) && result.caveats.length > 0
  if (!hasAsOf && !hasConfidence && !hasCaveats) return null

  return (
    <div className="tool-meta">
      {hasAsOf && (
        <div className="tool-meta-row">
          <span className="tool-meta-label">as_of</span> {result.as_of}
        </div>
      )}
      {hasConfidence && (
        <div className="tool-meta-row">
          <span className="tool-meta-label">confidence</span>{' '}
          {result.confidence.value != null ? result.confidence.value : 'n/a'}
          {result.confidence.reason ? ` — ${result.confidence.reason}` : ''}
        </div>
      )}
      {hasCaveats && (
        <div className="tool-meta-row">
          <span className="tool-meta-label">caveats</span>
          <ul className="tool-caveats">
            {result.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

function ToolCallEntry({ call }) {
  return (
    <div className="tool-call">
      <div className="tool-call-header">
        <span className="tool-call-name">{call.name}</span>
        <code className="tool-call-args">{JSON.stringify(call.arguments)}</code>
      </div>
      <ToolMeta result={call.result} />
      <pre className="tool-call-result">{JSON.stringify(call.result, null, 2)}</pre>
    </div>
  )
}

export default function ReasoningPanel({ toolCalls }) {
  if (!toolCalls || toolCalls.length === 0) return null
  return (
    <details className="reasoning-panel">
      <summary>
        Show reasoning ({toolCalls.length} tool call{toolCalls.length === 1 ? '' : 's'})
      </summary>
      <div className="reasoning-body">
        {toolCalls.map((call, i) => (
          <ToolCallEntry key={i} call={call} />
        ))}
      </div>
    </details>
  )
}
