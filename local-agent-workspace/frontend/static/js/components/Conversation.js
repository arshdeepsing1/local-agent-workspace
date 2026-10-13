import { html, useEffect, useMemo, useRef, useState } from '../lib.js'
import { Check, ChevronDown, ChevronRight, Copy, FileText, GitBranch, LoaderCircle, Pencil, RefreshCw, Terminal, Undo2, X } from '../icons.js'
import { api } from '../api.js'
import { enhanceCodeBlocks, linkPaths, renderMarkdown } from '../markdown.js'
import QuestionCard from './QuestionCard.js'
import PlanCard from './PlanCard.js'
import { Brand } from './Sidebar.js'
import RequestDetails from './RequestDetails.js'
import ApprovalOptions, { ruleLine } from './ApprovalOptions.js'
import EditMessage from './EditMessage.js'
import RewindDialog from './RewindDialog.js'
import ForkDialog from './ForkDialog.js'
import { backupTitle, RewindNotice, setRewindNotice } from '../rewindNotice.js'

// A long file is read in pages. Naming the lines each read covered keeps the
// pages of one file from looking like the same read repeated.
function readRange(event) {
  let result = {}
  try {
    const parsed = JSON.parse(event.output || '{}')
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) result = parsed
  } catch { /* A failed read returns plain text. */ }
  const start = Number.isInteger(result.start_line) ? result.start_line : event.input?.start_line
  if (Number.isInteger(start) && Number.isInteger(result.end_line) && result.end_line >= start) return ` · lines ${start}–${result.end_line}`
  return Number.isInteger(start) && start > 1 ? ` · from line ${start}` : ''
}

// Returns Map<event id, { subject, taskStatus? }> for every tool event.
function summarizeTools(events) {
  const taskTitles = new Map()
  const summaries = new Map()
  for (const event of events) {
    if (event.type !== 'tool') continue
    const input = event.input || {}
    let subject = String(input.path || input.file_path || input.command || input.query || input.task || input.title || '')
    if (event.name === 'read_file') subject += readRange(event)
    if (event.name === 'find_files') subject = `${input.pattern ?? ''}` + (input.path && input.path !== '.' ? ` in ${input.path}` : '')
    if (event.name === 'wait_job') subject = String(input.job_id ?? '').slice(0, 8) + (input.until_regex ? ` until /${input.until_regex}/` : '')
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

const clip = text => {
  const flat = String(text || '').trim().replace(/\s+/g, ' ')
  return flat.slice(0, 240) + (flat.length > 240 ? '…' : '')
}

function toolOutcome(event) {
  const clipped = clip(event.output)
  if (event.state === 'rejected') {
    if (event.approval === 'expired') return 'Not answered in time; the action did not run'
    if (event.approval === 'declined') return event.decline_note ? `Action declined: ${clip(event.decline_note)}` : 'Action declined'
    // Blocked by a rule, Don't ask or a mode change; the reason may already start with "Not run:".
    if (event.approval === 'blocked') {
      const reason = clip(event.blocked_reason || event.output).replace(/^Not run:\s*/i, '')
      return reason ? `Not run: ${reason}` : 'Action not run'
    }
    // Blocked without an approval card (Plan mode, changed instructions, a tool profile), or an older event.
    // Don't ask and protected-file refusals already start with "Not run:".
    const reason = clipped.replace(/^Not run:\s*/i, '')
    return reason ? `Not run: ${reason}` : 'Action not run'
  }
  if (event.state === 'cancelled') return 'Action cancelled'
  return clipped || 'Tool returned an error'
}

// The "Allow for this chat" button of a pending card. A folder-access card has none:
// its grant already lasts for this chat.
export function allowLabel(event) {
  const name = event.name || ''
  if (event.protected) return null  // Protected files are always confirmed.
  // Always ask rules, uncertain commands and commands naming credentials offer no chat rule (older events lack the list).
  if (Array.isArray(event.rememberable) && !event.rememberable.includes('chat')) return null
  if (name === 'run_command') return typeof event.input?.command === 'string' && event.input.command.trim() ? 'Always allow this command in this chat' : null
  if (['write_file', 'edit_file', 'insert_activity_log', 'move_path', 'delete_path', 'notebook_edit'].includes(name)) return 'Allow edits in this chat'
  if (name.startsWith('mcp__')) return 'Allow this tool in this chat'
  if (name.startsWith('hook_')) return 'Allow this hook in this chat'
  if (name === 'delegate_task') return 'Allow subagents in this chat'
  if (name === 'stop_job') return 'Allow stopping jobs in this chat'
  return null
}

// The time an approval card expires, with the weekday when that is not today (limits reach 24 hours).
export function expiryTime(seconds, now = new Date()) {
  const when = new Date(seconds * 1000)
  const options = { hour: '2-digit', minute: '2-digit' }
  return when.toDateString() === now.toDateString() ? when.toLocaleTimeString([], options)
    : when.toLocaleString([], { weekday: 'short', ...options })
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

// Tools whose input names one file: the file the card's Open file button opens.
const FILE_INPUTS = { read_file: 'path', write_file: 'path', edit_file: 'path', insert_activity_log: 'path',
  notebook_read: 'path', notebook_edit: 'path', move_path: 'destination' }

function ToolCard({ event, summary, sessionId, onError, onSelectSession, onOpenPath }) {
  const [expanded, setExpanded] = useState(false)
  const filePath = event.input?.[FILE_INPUTS[event.name]]
  const startLine = Number.isInteger(event.input?.start_line) && event.input.start_line > 0 ? event.input.start_line : undefined
  const [deciding, setDeciding] = useState(false)
  const [note, setNote] = useState('')
  const pending = event.state === 'pending'
  const title = (event.name || 'Tool').replace(/_/g, ' ')
  const isTask = event.name === 'create_task' || event.name === 'update_task'
  const allowInChat = pending ? allowLabel(event) : null
  const rule = ruleLine(event)
  const decide = async (allowed, extra = {}) => {
    setDeciding(true)
    // The note goes to the model with a Decline; extra is { remember: true } (an exact rule for this chat) or { scope, rule? }.
    const body = { allowed, ...(allowed ? extra : {}), ...(!allowed && note.trim() ? { note: note.trim() } : {}) }
    try { await api(`/sessions/${sessionId}/approvals/${event.id}`, 'POST', body) }
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
    ${rule ? html`<p class="tool-rule">${rule}</p>` : null}
    ${expanded || pending ? html`<div class="tool-body">
      ${pending ? html`<p>Local needs your approval to ${title.toLowerCase()}.${event.approval_expires
          ? ` If you do not answer by ${expiryTime(event.approval_expires)}, it will not run.` : ''}</p>
        <${ApprovalOptions} event=${event} sessionId=${sessionId} onError=${onError} deciding=${deciding} onDecide=${decide} allowInChat=${allowInChat}
          details=${html`<${ToolDetails} event=${event} pending=${true} />`}
          declineNote=${html`<label class="decline-note"><span>Tell the agent why (optional)</span>
            <textarea rows=${2} maxLength=${2000} spellcheck=${false} value=${note} disabled=${deciding}
              onInput=${e => setNote(e.currentTarget.value)} /></label>`} />`
        : html`<${ToolDetails} event=${event} pending=${false} /><small class="muted">Action: ${event.state}</small>
          ${onOpenPath && typeof filePath === 'string' && filePath ? html`<button class="outline tool-open-file"
            onClick=${() => onOpenPath(startLine ? { path: filePath, line: startLine } : { path: filePath })}><${FileText} size=${14} />Open file</button>` : null}`}
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

// onOpenPath({ path, line? }): file paths in the text become links that call it.
function Markdown({ text, streaming = false, onOpenPath }) {
  // Parsing is the expensive part of a streamed reply, so parse only on change.
  const markup = useMemo(() => renderMarkdown(text), [text])
  const element = useRef(null)
  const linked = !!onOpenPath
  // Highlight code once the reply is complete; the page keeps unchanged markup,
  // so other replies are not highlighted again on each streamed delta.
  useEffect(() => {
    if (streaming || !element.current) return
    enhanceCodeBlocks(element.current)
    if (linked) linkPaths(element.current)
  }, [markup, streaming, linked])
  const open = event => {
    const link = event.target.closest?.('a.path-link')
    if (!link || !onOpenPath || !element.current?.contains(link)) return
    event.preventDefault()
    onOpenPath(link.dataset.line ? { path: link.dataset.path, line: Number(link.dataset.line) } : { path: link.dataset.path })
  }
  return html`<div class="markdown" ref=${element} onClick=${open} dangerouslySetInnerHTML=${{ __html: markup }} />`
}

// Chips under a user message for its @ file snapshots and ! command outputs (metadata only).
export function attachmentLabel(item) {
  if (item?.kind === 'shell') {
    const command = String(item.command || '')
    return `! ${command.length > 60 ? `${command.slice(0, 59)}…` : command} · exit ${item.exit_code ?? '?'}`
  }
  const lines = Number.isInteger(item?.start_line) && Number.isInteger(item?.end_line) ? ` · lines ${item.start_line}–${item.end_line}` : ''
  return `@${item?.path || ''}${lines}${item?.truncated ? ' · shortened' : ''}`
}

function Attachments({ items }) {
  if (!Array.isArray(items) || !items.length) return null
  return html`<ul class="attachment-chips" aria-label="Attachments">${items.map((item, index) => html`<li key=${index} class="attachment-chip">${attachmentLabel(item)}</li>`)}</ul>`
}

function ActionButton({ label, icon: Icon, onClick, disabled = false, target }) {
  return html`<button class="icon-button message-action" aria-label=${label} title=${label} disabled=${disabled} data-target=${target} onClick=${onClick}><${Icon} size=${14} /></button>`
}

function Reply({ text, reasoning, truncated, requestInfo, onOpenPath, actions = null }) {
  const streaming = requestInfo?.status === 'running'
  return html`<div class="assistant-reply"><${Brand} small=${true} />
    <div class="reply-content">${reasoning ? html`<details class="reasoning-summary"><summary>Provider reasoning summary</summary>
      <p class="muted">Supplied by the model endpoint.</p><${Markdown} text=${reasoning} streaming=${streaming} />
      ${truncated ? html`<p class="muted">Summary display limit reached.</p>` : null}</details>` : null}<${Markdown} text=${text} streaming=${streaming} onOpenPath=${onOpenPath} />
      ${text || actions ? html`<div class="message-actions">${text ? html`<${CopyAction} text=${text} label="Copy response" />` : null}${actions}</div>` : null}
      ${requestInfo ? html`<${RequestDetails} info=${requestInfo} />` : null}
    </div></div>`
}

// onOpenPath({ path, line? }) opens a file named in a reply or a tool card in the Workspace panel.
// busy: the chat is not idle (message actions other than Copy are hidden).
// onPrefill(text) puts text in an empty composer and returns false when a typed draft was kept.
export default function Conversation({ session, onError, onSelectSession, onOpenPath, busy = false, onPrefill }) {
  const container = useRef(null)
  const bottom = useRef(null)
  const follow = useRef(true)
  const toolSummaries = useMemo(() => summarizeTools(session.events), [session.events])
  const last = session.events[session.events.length - 1]
  useEffect(() => { if (follow.current) bottom.current?.scrollIntoView({ block: 'end' }) }, [session.events.length, last?.text, last?.state])
  useEffect(() => { follow.current = true; bottom.current?.scrollIntoView({ block: 'end' }) }, [session.id])
  const tools = session.events.filter(event => event.type === 'tool')
  // Edit and resend, Retry, Rewind and Fork from here (history.py on the server).
  const [editing, setEditing] = useState(null)  // the user event id being edited
  const [rewinding, setRewinding] = useState(null)  // the user event to rewind to
  const [forking, setForking] = useState(undefined)  // { beforeEventId } of the reply to fork from
  const [retrying, setRetrying] = useState(false)
  const editReturn = useRef(null)  // the user event whose 'Edit message' button gets focus after Cancel
  useEffect(() => {
    if (editing !== null || editReturn.current === null) return
    const id = editReturn.current
    editReturn.current = null
    const button = [...(container.current?.querySelectorAll('[data-target]') ?? [])]
      .find(item => item.dataset.target === `edit:${id}`)
    button?.focus()
  }, [editing])
  const idle = !busy && session.status === 'idle'
  const cuts = useMemo(() => {
    const events = session.events
    // The message that started the running (or latest) turn: the last one that was not steered.
    const turnStart = events.findLastIndex(event => event.type === 'user' && !event.steered)
    const nextUser = new Map()  // reply id -> index of the first user event after it
    const turnEnds = new Set()  // replies that end a turn (no later reply before the next message)
    let next = -1, replyAfter = false
    for (let index = events.length - 1; index >= 0; index--) {
      const event = events[index]
      if (event.type === 'user') { next = index; replyAfter = false }
      else if (event.type === 'assistant' && event.text) {
        nextUser.set(event.id, next)
        if (!replyAfter) turnEnds.add(event.id)
        replyAfter = true
      }
    }
    const lastReplyIndex = events.findLastIndex(event => event.type === 'assistant' && event.text)
    const latestUserIndex = events.findLastIndex(event => event.type === 'user')
    const steered = events.slice(turnStart + 1).some(event => event.type === 'user' && event.steered)
    // Retry resends the latest message, so it is offered only on a reply to that message: after a turn
    // that failed or was stopped before any reply, the last reply belongs to an earlier turn.
    const retry = latestUserIndex >= 0 && turnStart >= 0 && !steered && lastReplyIndex > latestUserIndex
    return { nextUser, turnEnds, lastReply: events[lastReplyIndex], latestUser: events[latestUserIndex], retry }
  }, [session.events])
  const rewound = (result, draftKept = false) => {
    if (result.backupId) setRewindNotice(session.id, { title: backupTitle(session.title), backupId: result.backupId, draftKept })
  }
  const retry = async () => {
    if (retrying || !cuts.latestUser) return
    setRetrying(true)
    try {
      const result = await api(`/sessions/${encodeURIComponent(session.id)}/rewind`, 'POST',
        { before_event_id: cuts.latestUser.id, conversation: true, resend_text: cuts.latestUser.text })
      rewound({ backupId: result.backup_session_id })
      if (result.resend_error) onError(result.resend_error)
    } catch (e) { onError(e) }
    finally { setRetrying(false) }
  }
  const replyActions = event => {
    const next = cuts.nextUser.get(event.id) ?? -1
    const fork = idle && cuts.turnEnds.has(event.id)
    const canRetry = idle && cuts.retry && cuts.lastReply?.id === event.id
    if (!fork && !canRetry) return null
    return html`${fork ? html`<${ActionButton} label="Fork from here" icon=${GitBranch}
        onClick=${() => setForking({ beforeEventId: next >= 0 ? session.events[next].id : null })} />` : null}
      ${canRetry ? html`<${ActionButton} label="Retry response" icon=${RefreshCw} disabled=${retrying} onClick=${() => void retry()} />` : null}`
  }
  return html`<div class="transcript" ref=${container} onScroll=${() => {
    const el = container.current
    if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }}><div class="transcript-inner">
    ${tools.length ? html`<details class="activity-summary"><summary>Activity · ${tools.length} ${tools.length === 1 ? 'action' : 'actions'}</summary>
      <ol>${tools.map(event => {
        const summary = toolSummaries.get(event.id)
        return html`<li key=${event.id}><span>${(event.name || 'Tool').replace(/_/g, ' ')}</span>${summary.subject ? html`<span class="activity-subject"> · ${summary.subject}</span>` : null}
          ${summary.taskStatus ? html`<small>Task: ${summary.taskStatus}</small>` : null}<small>Action: ${event.state}</small></li>`
      })}</ol>
    </details>` : null}
    ${session.parent_session_id && onSelectSession ? html`<button class="outline parent-conversation" onClick=${() => onSelectSession(session.parent_session_id)}>Back to parent conversation</button>` : null}
    <${RewindNotice} sessionId=${session.id} onSelectSession=${onSelectSession} />
    ${session.events.map(event => {
      if (event.type === 'user' && editing === event.id && idle) return html`<${EditMessage} key=${event.id} sessionId=${session.id} event=${event}
        onSelectSession=${onSelectSession}
        onCancel=${() => { editReturn.current = event.id; setEditing(null) }} onSent=${result => {
          setEditing(null)
          rewound({ backupId: result.backup_session_id })
          if (result.resend_error) onError(result.resend_error)
          // The edited message is gone from the transcript; the message box is where to go on.
          document.querySelector('textarea[aria-label="Message"]')?.focus()
        }} />`
      if (event.type === 'user') return html`<div class=${`user-message${event.steered ? ' steered' : ''}`} key=${event.id}>${event.steered ? html`<small class="steered-label">Sent while the agent was working</small>` : null}${event.origin?.kind === 'delegated' ? html`<small class="delegation-origin">Delegated by parent conversation</small>` : null}${event.text}<${Attachments} items=${event.attachments} />${event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}${event.text || idle ? html`<div class="message-actions">${event.text ? html`<${CopyAction} text=${event.text} label="Copy message" />` : null}${idle ? html`<${ActionButton} label="Edit message" icon=${Pencil} target=${`edit:${event.id}`} onClick=${() => setEditing(event.id)} /><${ActionButton} label="Rewind to here" icon=${Undo2} onClick=${() => setRewinding(event)} />` : null}</div>` : null}</div>`
      if (event.type === 'tool' && event.name === 'ask_user') return html`<${QuestionCard} key=${event.id} event=${event} sessionId=${session.id} onError=${onError} />`
      if (event.type === 'tool' && event.name === 'submit_plan') return html`<${PlanCard} key=${event.id} event=${event} sessionId=${session.id} onError=${onError} />`
      if (event.type === 'tool') return html`<${ToolCard} key=${event.id} event=${event} summary=${toolSummaries.get(event.id)} sessionId=${session.id} onError=${onError} onSelectSession=${onSelectSession} onOpenPath=${onOpenPath} />`
      if (event.type === 'assistant') return event.text || event.reasoning_summary || event.request_info ? html`<${Reply} key=${event.id} text=${event.text || ''} reasoning=${event.reasoning_summary} truncated=${event.reasoning_truncated} requestInfo=${event.request_info} onOpenPath=${onOpenPath} actions=${event.text ? replyActions(event) : null} />` : null
      return html`<div class=${`conversation-notice ${event.type === 'error' ? 'error' : ''}`} key=${event.id}>${event.text}<${DelegationStatus} event=${event} onSelectSession=${onSelectSession} />${!event.delegation && event.child_session_id && onSelectSession ? html`<button class="outline" onClick=${() => onSelectSession(event.child_session_id)}>Open subagent</button>` : null}</div>`
    })}
    ${['running', 'delegating'].includes(session.status) ? html`<div class="working-indicator"><span /><span /><span /><span class="sr-only">Working</span></div>` : null}
    ${rewinding && idle ? html`<${RewindDialog} session=${session} event=${rewinding} onClose=${() => setRewinding(null)}
      onSelectSession=${onSelectSession}
      onRewound=${result => {
        setRewinding(null)
        rewound(result, result.conversation && onPrefill ? onPrefill(rewinding.text || '') === false : false)
      }} />` : null}
    ${forking !== undefined && onSelectSession ? html`<${ForkDialog} session=${session} beforeEventId=${forking.beforeEventId}
      onSelectSession=${onSelectSession} onClose=${() => setForking(undefined)} />` : null}
    <div ref=${bottom} />
  </div></div>`
}
