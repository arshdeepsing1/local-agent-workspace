import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { Check, ChevronDown, ChevronRight, Copy, FileText, LoaderCircle, Terminal, X } from '../icons.js'
import { api } from '../api.js'
import { renderMarkdown } from '../markdown.js'
import { Brand } from './Sidebar.js'
import RequestDetails from './RequestDetails.js'

// Returns Map<event id, { subject, taskStatus? }> for every tool event.
function summarizeTools(events) {
  const taskTitles = new Map()
  const summaries = new Map()
  for (const event of events) {
    if (event.type !== 'tool') continue
    const input = event.input || {}
    let subject = String(input.path || input.file_path || input.command || input.query || input.task || input.title || '')
    let taskStatus
    if (event.name === 'create_task' || event.name === 'update_task') {
      let task = {}
      try {
        const result = JSON.parse(event.output || '{}')
        if (result && typeof result === 'object' && !Array.isArray(result)) task = result
      } catch { /* Older or failed events can contain plain-text output. */ }
      subject = String(task.title || input.title || taskTitles.get(String(input.task_id)) || input.task_id || '')
      if (event.state === 'completed') {
        if (typeof task.id === 'string' && subject) taskTitles.set(task.id, subject)
        const status = task.status || input.status
        if (typeof status === 'string') taskStatus = status.replace(/_/g, ' ')
      }
    }
    summaries.set(event.id, { subject: subject.slice(0, 240).replace(/\s+/g, ' '), taskStatus })
  }
  return summaries
}

function ToolDetails({ event, pending }) {
  const { command, ...options } = event.input || {}
  const hasCommand = typeof command === 'string'
  return html`
    <p class="tool-detail-label">${hasCommand ? 'Command' : 'Input'}</p>
    <pre aria-label=${hasCommand ? 'Command' : 'Input'}>${hasCommand ? (pending && event.preview ? event.preview : command) : JSON.stringify(event.input || {}, null, 2)}</pre>
    ${hasCommand && Object.keys(options).length && !(pending && event.preview) ? html`<p class="tool-detail-label">Options</p><pre aria-label="Options">${JSON.stringify(options, null, 2)}</pre>` : null}
    ${pending && !hasCommand && event.preview ? html`<p class="tool-detail-label">Approval preview</p><pre aria-label="Approval preview">${event.preview}</pre>` : null}
    ${!pending || event.output ? html`<p class="tool-detail-label">Output</p><pre aria-label="Output">${event.output || (event.state === 'running' ? 'Waiting for output…' : 'No output.')}</pre>` : null}`
}

function toolOutcome(event) {
  if (event.state === 'rejected') return 'Action declined'
  if (event.state === 'cancelled') return 'Action cancelled'
  const output = (event.output || '').trim().replace(/\s+/g, ' ')
  return output ? output.slice(0, 240) + (output.length > 240 ? '…' : '') : 'Tool returned an error'
}

function DelegationStatus({ event, onSelectSession }) {
  const progress = event.delegation
  if (!progress) return null
  const status = progress.status === 'awaiting_approval' ? 'Waiting for approval' : progress.status.replace(/_/g, ' ')
  return html`<div class="delegation-progress" aria-label="Subagent progress">
    <span>Subagent: ${status}</span><span>${progress.completed_tools} completed ${progress.completed_tools === 1 ? 'action' : 'actions'}</span>
    ${progress.last_tool ? html`<span>Last tool: ${progress.last_tool.replace(/_/g, ' ')}</span>` : null}
    ${progress.terminal_reason ? html`<span>Reason: ${progress.terminal_reason.replace(/_/g, ' ')}</span>` : null}
    ${event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}
  </div>`
}

function ToolCard({ event, summary, sessionId, onError, onSelectSession }) {
  const [expanded, setExpanded] = useState(false)
  const [deciding, setDeciding] = useState(false)
  const pending = event.state === 'pending'
  const title = (event.name || 'Tool').replace(/_/g, ' ')
  const isTask = event.name === 'create_task' || event.name === 'update_task'
  const decide = async allowed => {
    setDeciding(true)
    try { await api(`/sessions/${sessionId}/approvals/${event.id}`, 'POST', { allowed }) }
    catch (e) { onError(e.message) }
    finally { setDeciding(false) }
  }
  return html`<div class=${`tool-card ${isTask ? 'tool-task' : ''} ${pending ? 'needs-approval' : ''}`}>
    <button class="tool-heading" onClick=${() => setExpanded(v => !v)} aria-expanded=${expanded || pending}
      aria-label=${[title, summary.subject, summary.taskStatus ? `Task: ${summary.taskStatus}` : ''].filter(Boolean).join(' ')}>
      ${event.name?.toLowerCase().includes('command') || event.name === 'Bash' ? html`<${Terminal} size=${16} />` : html`<${FileText} size=${16} />`}
      <span class="tool-name">${title}</span><span class="tool-subject" title=${summary.subject}>${summary.subject}</span>
      ${summary.taskStatus ? html`<span class="tool-task-status">Task: ${summary.taskStatus}</span>` : null}
      ${event.state === 'running' ? html`<${LoaderCircle} size=${14} className="spin" />` : event.state === 'completed' ? html`<${Check} size=${14} className="success-icon" />` : null}
      ${expanded || pending ? html`<${ChevronDown} size=${14} />` : html`<${ChevronRight} size=${14} />`}
    </button>
    <${DelegationStatus} event=${event} onSelectSession=${onSelectSession} />
    ${expanded || pending ? html`<div class="tool-body">
      ${pending ? html`<p>Local needs your approval to ${title.toLowerCase()}.</p>
        <${ToolDetails} event=${event} pending=${true} />
        <div class="approval-buttons"><button class="outline" disabled=${deciding} onClick=${() => void decide(false)}><${X} size=${15} />Decline</button>
          <button class="primary" disabled=${deciding} onClick=${() => void decide(true)}><${Check} size=${15} />Approve</button></div>`
        : html`<${ToolDetails} event=${event} pending=${false} /><small class="muted">Action: ${event.state}</small>`}
    </div>` : null}
    ${!expanded && (event.state === 'rejected' || event.state === 'cancelled' || event.state === 'error') ? html`<p class="tool-outcome">${toolOutcome(event)}</p>` : null}
  </div>`
}

function CopyAction({ text, label }) {
  const [copied, setCopied] = useState(false)
  return html`<button class="copy-button icon-button" aria-label=${label} onClick=${() => {
    void navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) })
  }}>${copied ? html`<${Check} size=${14} />` : html`<${Copy} size=${14} />`}</button>`
}

function Markdown({ text }) {
  // Parsing is the expensive part of a streamed reply, so parse only on change.
  const markup = useMemo(() => renderMarkdown(text), [text])
  return html`<div class="markdown" dangerouslySetInnerHTML=${{ __html: markup }} />`
}

function Reply({ text, reasoning, truncated, requestInfo }) {
  return html`<div class="assistant-reply"><${Brand} small=${true} />
    <div class="reply-content">${reasoning ? html`<details class="reasoning-summary"><summary>Provider reasoning summary</summary>
      <p class="muted">Supplied by the model endpoint.</p><${Markdown} text=${reasoning} />
      ${truncated ? html`<p class="muted">Summary display limit reached.</p>` : null}</details>` : null}<${Markdown} text=${text} />
      ${text ? html`<${CopyAction} text=${text} label="Copy response" />` : null}
      ${requestInfo ? html`<${RequestDetails} info=${requestInfo} />` : null}
    </div></div>`
}

export default function Conversation({ session, onError, onSelectSession }) {
  const container = useRef(null)
  const bottom = useRef(null)
  const follow = useRef(true)
  const toolSummaries = useMemo(() => summarizeTools(session.events), [session.events])
  const last = session.events[session.events.length - 1]
  useEffect(() => { if (follow.current) bottom.current?.scrollIntoView({ block: 'end' }) }, [session.events.length, last?.text, last?.state])
  useEffect(() => { follow.current = true; bottom.current?.scrollIntoView({ block: 'end' }) }, [session.id])
  const tools = session.events.filter(event => event.type === 'tool')
  return html`<div class="transcript" ref=${container} onScroll=${() => {
    const el = container.current
    if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }}><div class="transcript-inner">
    ${tools.length ? html`<details class="activity-summary"><summary>Activity · ${tools.length} actions</summary>
      <ol>${tools.map(event => {
        const summary = toolSummaries.get(event.id)
        return html`<li key=${event.id}><span>${(event.name || 'Tool').replace(/_/g, ' ')}</span>${summary.subject ? html`<span class="activity-subject"> · ${summary.subject}</span>` : null}
          ${summary.taskStatus ? html`<small>Task: ${summary.taskStatus}</small>` : null}<small>Action: ${event.state}</small></li>`
      })}</ol>
    </details>` : null}
    ${session.parent_session_id && onSelectSession ? html`<button class="outline parent-conversation" onClick=${() => onSelectSession(session.parent_session_id)}>Back to parent conversation</button>` : null}
    ${session.events.map(event => {
      if (event.type === 'user') return html`<div class="user-message" key=${event.id}>${event.origin?.kind === 'delegated' ? html`<small class="delegation-origin">Delegated by parent conversation</small>` : null}${event.text}${event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}${event.text ? html`<${CopyAction} text=${event.text} label="Copy message" />` : null}</div>`
      if (event.type === 'tool') return html`<${ToolCard} key=${event.id} event=${event} summary=${toolSummaries.get(event.id)} sessionId=${session.id} onError=${onError} onSelectSession=${onSelectSession} />`
      if (event.type === 'assistant') return event.text || event.reasoning_summary || event.request_info ? html`<${Reply} key=${event.id} text=${event.text || ''} reasoning=${event.reasoning_summary} truncated=${event.reasoning_truncated} requestInfo=${event.request_info} />` : null
      return html`<div class=${`conversation-notice ${event.type === 'error' ? 'error' : ''}`} key=${event.id}>${event.text}<${DelegationStatus} event=${event} onSelectSession=${onSelectSession} />${!event.delegation && event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}</div>`
    })}
    ${['running', 'delegating'].includes(session.status) ? html`<div class="working-indicator"><span /><span /><span /><span class="sr-only">Working</span></div>` : null}
    <div ref=${bottom} />
  </div></div>`
}
