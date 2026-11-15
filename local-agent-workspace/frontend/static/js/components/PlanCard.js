import { html, HIDDEN_NOTE, RTL_NOTE, countHidden, hiddenNote, visibleParts, visibleText, useLayoutEffect, useMemo, useRef, useState } from '../lib.js'
import { Check } from '../icons.js'
import { api } from '../api.js'
import { enhanceCodeBlocks, renderMarkdown, showHiddenCharacters } from '../markdown.js'
import { expiryTime } from './Conversation.js'

const MAX_FEEDBACK = 2000
// A plan with more hidden characters than this is not a plan: it is shown as plain escaped text, without Markdown.
const MAX_HIDDEN = 5000

function outcomeText(event) {
  if (event.plan_outcome === 'approved') return `Approved · switched to ${event.plan_mode === 'manual' ? 'Manual' : 'Accept edits'}`
  if (event.plan_outcome === 'keep_planning') return 'You asked to keep planning'
  if (event.plan_outcome === 'expired') return 'Not reviewed in time'
  if (event.plan_outcome === 'superseded') return 'Not reviewed: you switched modes'
  if (event.state === 'cancelled') return 'Plan review cancelled.'
  if (event.state === 'error' || event.state === 'rejected') return event.output || 'The plan could not be shown.'
  return ''
}

// A submit_plan tool event: the plan (sanitized Markdown) and, while it waits, the three review choices.
export default function PlanCard({ event, sessionId, onError }) {
  const plan = typeof event.input?.plan === 'string' ? event.input.plan : ''
  const refused = event.state === 'error' || event.state === 'rejected'
  const title = refused ? 'Plan not submitted'
    : typeof event.input?.title === 'string' && event.input.title.trim() ? event.input.title.trim() : 'Plan ready for review'
  const pending = event.state === 'pending'
  // A plan that waits for you is shown with hidden and direction-changing characters as visible escapes (title, text,
  // code blocks and code spans); once answered it is shown as written. The layout effect runs before the first paint,
  // so the raw characters are never on screen; the body is rebuilt when the plan stops waiting. The escaping fails
  // closed: with too many hidden characters, or when anything throws, the body is the escaped Markdown source as text.
  const tooMany = useMemo(() => pending && countHidden(plan, MAX_HIDDEN) > MAX_HIDDEN, [plan, pending])
  const markup = useMemo(() => tooMany ? '' : renderMarkdown(plan), [plan, tooMany])
  const body = useRef(null)
  const [feedback, setFeedback] = useState('')
  const [sending, setSending] = useState(false)
  const [escaped, setEscaped] = useState({ plan: null, count: 0, rtl: false, failed: false })
  const done = escaped.plan === plan
  const plainSource = pending && (tooMany || (done && escaped.failed))
  useLayoutEffect(() => {
    if (!body.current || tooMany) return
    try {
      enhanceCodeBlocks(body.current)
      if (!pending) return
      const count = showHiddenCharacters(body.current)
      setEscaped({ plan, count, rtl: !!body.current.querySelector('bdi.rtl-run'), failed: false })
    } catch {
      // Whatever was half done is replaced at once, so the raw plan is never left on screen.
      const source = document.createElement('pre')
      source.className = 'plan-plain logical-order'
      source.textContent = visibleText(plan)
      body.current.replaceChildren(source)
      setEscaped({ plan, count: 0, rtl: false, failed: true })
    }
  }, [markup, pending])
  const titleNote = pending ? hiddenNote(title) : null
  const note = !pending ? null : tooMany || (done && escaped.failed) ? (hiddenNote(plan) || HIDDEN_NOTE)
    : titleNote === RTL_NOTE || (done && escaped.rtl) ? RTL_NOTE : titleNote || (done && escaped.count > 0 ? HIDDEN_NOTE : null)
  const review = async choice => {
    setSending(true)
    const note = feedback.trim()
    try { await api(`/sessions/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(event.id)}`, 'POST', { choice, ...(note ? { feedback: note } : {}) }) }
    catch (e) { onError(e.message) }
    finally { setSending(false) }
  }
  const outcome = pending ? '' : outcomeText(event)
  return html`<section class=${`plan-card ${pending ? 'needs-answer' : ''}`} aria-label=${pending ? visibleText(title) : title}>
    <h3 class=${pending ? 'logical-order' : undefined}>${pending ? visibleParts(title) : title}</h3>
    ${note ? html`<p class="tool-uncertain" role="note">${note}</p>` : null}
    ${pending && event.approval_expires ? html`<p class="muted">If you do not review the plan by ${expiryTime(event.approval_expires)}, the agent continues without your review.</p>` : null}
    ${refused ? html`<details class="plan-refused"><summary>Show the plan</summary>
        <div class="plan-body markdown" ref=${body} dangerouslySetInnerHTML=${{ __html: markup }} /></details>`
      : plainSource ? html`<div class="plan-body markdown" key="plain"><pre class="plan-plain logical-order">${visibleText(plan)}</pre></div>`
      : html`<div class=${`plan-body markdown${pending ? ' logical-order' : ''}`} key=${pending ? 'waiting' : 'answered'} ref=${body} dangerouslySetInnerHTML=${{ __html: markup }} />`}
    ${pending ? html`<label class="decline-note"><span>Feedback for the agent (optional)</span>
        <textarea rows=${2} maxLength=${MAX_FEEDBACK} spellcheck=${false} value=${feedback} disabled=${sending}
          onInput=${e => setFeedback(e.currentTarget.value)} /></label>
      <div class="plan-actions">
        <button class="outline" disabled=${sending} onClick=${() => void review('keep_planning')}>Keep planning</button>
        <button class="outline" disabled=${sending} onClick=${() => void review('manual')}>Approve and use Manual</button>
        <button class="primary" disabled=${sending} onClick=${() => void review('acceptEdits')}><${Check} size=${15} />Approve and use Accept edits</button>
      </div>` : null}
    ${outcome ? html`<p class="plan-outcome">${outcome}</p>` : null}
    ${!pending && event.feedback ? html`<p class="muted">Your feedback: ${event.feedback}</p>` : null}
  </section>`
}
