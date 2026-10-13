import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { enhanceCodeBlocks, renderMarkdown } from '../markdown.js'

// The answer to a /btw side question. It is shown here only: it is not added to the
// conversation, and the next message does not see it.
// Props: side { question, answer: string | null }, onDismiss()
export default function SideAnswer({ side, onDismiss }) {
  const [copied, setCopied] = useState(false)
  const body = useRef(null)
  const markup = useMemo(() => side?.answer ? renderMarkdown(side.answer) : '', [side?.answer])
  useEffect(() => { if (body.current) enhanceCodeBlocks(body.current) }, [markup])
  if (!side) return null
  const copy = async () => {
    try { await navigator.clipboard.writeText(side.answer); setCopied(true) } catch { setCopied(false) }
  }
  return html`<section class="side-answer" role="region" aria-label="Side question">
    <p class="side-question"><strong>/btw</strong> ${side.question}</p>
    ${side.answer === null ? html`<p class="side-pending" role="status">Answering…</p>`
      : html`<div class="markdown side-body" ref=${body} dangerouslySetInnerHTML=${{ __html: markup }} />`}
    <p class="side-note">Not added to the conversation. Uses one model request with the whole conversation as context.</p>
    <div class="side-actions">
      ${side.answer !== null ? html`<button class="outline" aria-label="Copy answer" onClick=${() => void copy()}>${copied ? 'Copied' : 'Copy answer'}</button>` : null}
      <button class="outline" onClick=${onDismiss}>Dismiss</button>
    </div>
  </section>`
}
