import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { enhanceCodeBlocks, renderMarkdown } from '../markdown.js'

// The answer to a /btw side question. It is shown here only: it is not added to the
// conversation, and the next message does not see it.
// Props: side { question, answer: string | null }, onDismiss(), onError(message)
export default function SideAnswer({ side, onDismiss, onError }) {
  const [copied, setCopied] = useState(false)
  const body = useRef(null)
  const markup = useMemo(() => side?.answer ? renderMarkdown(side.answer) : '', [side?.answer])
  useEffect(() => { if (body.current) enhanceCodeBlocks(body.current) }, [markup])
  useEffect(() => setCopied(false), [side?.answer])
  if (!side) return null
  const copy = async () => {
    try { await navigator.clipboard.writeText(side.answer); setCopied(true) }
    catch { setCopied(false); onError?.('Could not copy the answer. Select its text and copy it instead.') }
  }
  // One live region for both states, so screen readers also hear that the answer arrived.
  return html`<section class="side-answer" role="region" aria-label="Side question">
    <p class="side-question"><strong>/btw</strong> ${side.question}</p>
    <p class=${side.answer === null ? 'side-pending' : 'sr-only'} role="status">${side.answer === null ? 'Answering…' : 'Side answer ready.'}</p>
    ${side.answer !== null ? html`<div class="markdown side-body" ref=${body} dangerouslySetInnerHTML=${{ __html: markup }} />` : null}
    <p class="side-note">Not added to the conversation. Uses one model request with the whole conversation as context.</p>
    <div class="side-actions">
      ${side.answer !== null ? html`<button class="outline" onClick=${() => void copy()}>${copied ? 'Copied' : 'Copy answer'}</button>` : null}
      <button class="outline" onClick=${onDismiss}>Dismiss</button>
    </div>
  </section>`
}
