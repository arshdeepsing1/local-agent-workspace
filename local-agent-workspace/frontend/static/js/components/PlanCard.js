import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { Check } from '../icons.js'
import { api } from '../api.js'
import { enhanceCodeBlocks, renderMarkdown } from '../markdown.js'
import { expiryTime } from './Conversation.js'

const MAX_FEEDBACK = 2000

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
  const markup = useMemo(() => renderMarkdown(plan), [plan])
  const body = useRef(null)
  const [feedback, setFeedback] = useState('')
  const [sending, setSending] = useState(false)
  const pending = event.state === 'pending'
  useEffect(() => { if (body.current) enhanceCodeBlocks(body.current) }, [markup])
  const review = async choice => {
    setSending(true)
    const note = feedback.trim()
    try { await api(`/sessions/${encodeURIComponent(sessionId)}/plans/${encodeURIComponent(event.id)}`, 'POST', { choice, ...(note ? { feedback: note } : {}) }) }
    catch (e) { onError(e.message) }
    finally { setSending(false) }
  }
  const outcome = pending ? '' : outcomeText(event)
  return html`<section class=${`plan-card ${pending ? 'needs-answer' : ''}`} aria-label=${title}>
    <h3>${title}</h3>
    ${pending && event.approval_expires ? html`<p class="muted">If you do not review the plan by ${expiryTime(event.approval_expires)}, the agent continues without your review.</p>` : null}
    ${refused ? html`<details class="plan-refused"><summary>Show the plan</summary>
        <div class="plan-body markdown" ref=${body} dangerouslySetInnerHTML=${{ __html: markup }} /></details>`
      : html`<div class="plan-body markdown" ref=${body} dangerouslySetInnerHTML=${{ __html: markup }} />`}
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
